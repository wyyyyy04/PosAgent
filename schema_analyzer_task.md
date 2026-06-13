# Task: Schema Analyzer — Column Alias Memory + Template Fingerprint Cache

## Goal

When the Schema Analyzer fails to map a column, prompt the user to classify it manually.
Persist the result in two ways:
1. **Column aliases** — cross-template, by column name
2. **Template fingerprint cache** — per-template, skips re-classification on repeat runs

Architecture rule: **all user interaction stays in the CLI layer (`main.py`)**. `schema_analyzer.py` never prompts the user directly.

---

## Files to Modify

### 1. `data/memory.py`

Add two new storage sections to the existing memory JSON.

#### Column Aliases

Stores a mapping from raw column name → canonical field name, shared across all templates.

```python
# New methods to add:

def get_column_alias(self, col_name: str) -> Optional[str]:
    """Return the canonical field for col_name, or None if not known."""

def add_column_alias(self, col_name: str, canonical_field: str) -> None:
    """Persist col_name → canonical_field into memory.json under 'column_aliases'."""

def list_column_aliases(self) -> dict[str, str]:
    """Return the full column_aliases dict."""
```

`memory.json` shape after this change:

```json
{
  "column_aliases": {
    "菜品名称": "product_name",
    "配料代码": "sop",
    "门店编号": "ignore"
  },
  "template_fingerprints": {
    "<fingerprint_hash>": {
      "field_mapping": { "列A": "product_name", "列B": "size" },
      "irrelevant_cols": ["备注"],
      "composite_col": "口味做法",
      "target_col": "销量"
    }
  }
}
```

#### Template Fingerprint Cache

Keyed by a hash of the template's column names (order-insensitive). Stores the **complete** resolved mapping so repeat runs skip both LLM and user interaction.

```python
def _fingerprint(self, col_names: list[str]) -> str:
    """SHA256 of sorted column names joined by '|'."""

def get_template_cache(self, col_names: list[str]) -> Optional[dict]:
    """Return cached schema result for this column set, or None."""

def set_template_cache(self, col_names: list[str], schema_result: dict) -> None:
    """Write the complete schema result under its fingerprint hash."""
```

---

### 2. `agent/schema_analyzer.py`

Modify `analyze_from_dataframe()` (or `analyze()`) to run in two stages.

#### Stage 1 — Pre-LLM: inject known aliases

Before calling the LLM, check each column against `memory.get_column_alias()`.
Inject hits directly into `field_mapping`; pass only the remaining unknown columns to the LLM.

```python
# Pseudocode inside analyze_from_dataframe():

known_mappings = {}
unknown_cols = []

for col in dataframe.columns:
    alias = memory.get_column_alias(col)
    if alias == "ignore":
        irrelevant_cols.append(col)
    elif alias is not None:
        known_mappings[col] = alias
    else:
        unknown_cols.append(col)

# Only send unknown_cols to the LLM
llm_result = call_llm(unknown_cols, ...)
field_mapping = {**known_mappings, **llm_result.field_mapping}
```

#### Stage 2 — Post-LLM: compute unrecognized columns

After merging LLM results, find any columns still not accounted for.

```python
def _get_unmapped_columns(
    all_cols: list[str],
    field_mapping: dict,
    composite_col: str | None,
    target_col: str | None,
    irrelevant_cols: list[str],
) -> list[str]:
    covered = (
        set(field_mapping.keys())
        | ({composite_col} if composite_col else set())
        | ({target_col} if target_col else set())
        | set(irrelevant_cols)
    )
    return [c for c in all_cols if c not in covered]
```

#### Return shape

Add `unrecognized_cols` to the return value so the CLI layer knows what to ask about:

```python
return {
    "field_mapping": field_mapping,
    "composite_col": composite_col,
    "target_col": target_col,
    "irrelevant_cols": irrelevant_cols,
    "unrecognized_cols": unrecognized_cols,   # NEW — empty list if fully resolved
}
```

Do **not** prompt the user here. Do **not** write to memory here.

---

### 3. `main.py`

Add a pre-pipeline step that handles `unrecognized_cols` before calling `run_pipeline()`.

#### Step A — Check template fingerprint cache first

```python
schema_result = schema_analyzer.analyze_from_dataframe(df)

# Short-circuit: if this exact template was fully resolved before, use cached result
cached = memory.get_template_cache(list(df.columns))
if cached:
    schema_result = cached
    # skip to run_pipeline()
```

#### Step B — Interact for unrecognized columns

```python
FIELD_OPTIONS = [
    ("product_name", "商品名"),
    ("size",         "规格"),
    ("composite_col","口味做法组合"),
    ("sop",          "配料/SOP代码"),
    ("ignore",       "忽略此列"),
]

for col in schema_result["unrecognized_cols"]:
    sample_values = ", ".join(str(v) for v in df[col].dropna().unique()[:3])
    print(f"\n[Schema] 发现未能识别的列：「{col}」")
    print(f"  样例值：{sample_values or '(空)'}")
    for i, (field, label) in enumerate(FIELD_OPTIONS, 1):
        print(f"  [{i}] {field}（{label}）")

    choice = int(input("请选择: ").strip()) - 1
    field_name, _ = FIELD_OPTIONS[choice]

    # Persist to cross-template alias memory
    memory.add_column_alias(col, field_name)

    # Apply to current run
    if field_name == "ignore":
        schema_result["irrelevant_cols"].append(col)
    else:
        schema_result["field_mapping"][col] = field_name

schema_result["unrecognized_cols"] = []
```

#### Step C — Write complete result to template fingerprint cache

```python
memory.set_template_cache(list(df.columns), schema_result)
```

#### Step D — Proceed normally

```python
run_pipeline(schema_result, ...)
```

---

## Interaction Trigger Conditions

| Situation | Behavior |
|-----------|----------|
| Template fingerprint cache hit | Skip LLM + skip interaction entirely |
| All columns resolved by aliases + LLM | Skip interaction, write fingerprint cache |
| LLM leaves columns unresolved | Prompt user for each unresolved column |
| LLM call fails (timeout / error) | Treat all columns as unresolved, prompt for each |

---

## Tests to Add

| File | Cases |
|------|-------|
| `data/memory.py` | `get/add/list_column_aliases`; `get/set_template_cache`; fingerprint is order-insensitive |
| `agent/schema_analyzer.py` | alias pre-injection skips LLM for known cols; `_get_unmapped_columns` returns correct set; `unrecognized_cols` present in return value |
| `main.py` / integration | fingerprint cache hit skips interaction; user input correctly written to alias + fingerprint; LLM failure routes all cols to manual classification |
