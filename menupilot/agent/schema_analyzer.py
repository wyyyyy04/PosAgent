"""
Schema Analyzer — LLM 模板字段语义分析。
仅在初始化阶段调用一次，输出字段映射配置供 Rule Engine 使用。
不参与逐行匹配，结果可缓存复用。
"""

import hashlib
import json
import re
from typing import Any, Dict, List, Optional

from menupilot import config
from menupilot.data.canonical_schema import CANONICAL_FIELDS
from menupilot.data.memory import get_template_rule as mem_get_template_rule
from menupilot.data.memory import save_template_rule as mem_save_template_rule

# ── Prompt 模板 ──────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are a schema analysis expert for POS (Point of Sale) templates in the food & beverage industry.
Given the column names and sample data of a POS template spreadsheet, identify the semantic meaning
of each column and produce a field mapping configuration.

## Canonical Fields (internal standard schema)
- product_name: 商品/菜品名称 (product/dish name)
- size: 规格/杯型 (size/cup size, e.g. 大杯/中杯/小杯/五角瓶)
- milk_base: 奶底 (milk base, e.g. 牛奶/燕麦奶/厚乳/椰乳)
- temperature: 温度/做法 (temperature/preparation, e.g. 正常冰/少冰/去冰/热/温热)
- sugar: 糖度 (sugar level, e.g. 全糖/七分糖/五分糖/无糖)
- tea_base: 茶底 (tea base, e.g. 红茶/绿茶/乌龙茶)

## Rules
1. Map each template column to the MOST appropriate canonical field above.
   - Only include columns that semantically match a canonical field; a column named "序号" or "备注" is NOT product_name.
   - A single template column might cover multiple dimensions (composite column) — do NOT map it directly; mark it as composite_col instead.
2. Identify the "composite column" — a column whose cells contain comma-separated combinations
   of multiple dimensions (e.g. "红茶, 十二分糖, 温热" contains tea_base + sugar + temperature).
   If no such column exists, set composite_col to null.
3. Identify the "target column" — the column that needs to be filled with SOP data
   (usually empty cells, named something like 配料/做法/SOP/备注等). If unclear, set to null.
4. Identify irrelevant columns that should be ignored (e.g. 序号, 备注/remarks, 图片, etc.).

## Output Format
Return ONLY a single JSON object. No markdown fences, no extra text:
{"field_mapping": {"template_col": "canonical_field", ...}, "composite_col": "col_name_or_null", "target_col": "col_name_or_null", "irrelevant_cols": ["col1", ...]}"""

USER_PROMPT_TEMPLATE = """\
## Template Columns (with per-column sample values, NOT full rows)
{columns}

## Per-Column Sample Values
{sample_data}

Analyze the template schema and return the JSON field mapping configuration.
IMPORTANT: Use the column NAMES and sample values ONLY to understand semantic types.
Do NOT modify or rewrite any sample values — they are read-only references."""

# ── API 调用计数器 ─────────────────────────────────────────────

_api_call_count: int = 0


def get_api_call_count() -> int:
    """返回 Schema Analyzer 的真实 API 调用次数。"""
    return _api_call_count


def reset_api_call_count() -> None:
    """重置 API 调用计数器（用于测试）。"""
    global _api_call_count
    _api_call_count = 0


# ── 缓存 ────────────────────────────────────────────────────────

_cache: Dict[str, Dict[str, Any]] = {}


def _cache_key(columns: List[str]) -> str:
    """基于列名列表生成进程内缓存键（SHA256）。"""
    return hashlib.sha256(",".join(sorted(columns)).encode()).hexdigest()


def _template_fingerprint(columns: List[str]) -> str:
    """基于列名列表生成模板指纹（MD5），用于持久化缓存键。

    对模板所有列名排序后用逗号拼接，取 MD5 摘要。
    同一模板的列名无论顺序如何，指纹一致。
    """
    return hashlib.md5(",".join(sorted(columns)).encode()).hexdigest()


def reset_cache() -> None:
    """清空缓存（用于测试）。"""
    _cache.clear()


# ── LLM 客户端 ──────────────────────────────────────────────────

def _get_client():
    """创建 DeepSeek API 客户端（兼容 OpenAI SDK）。延迟导入，Mock 模式下不触发。"""
    from openai import OpenAI  # 延迟导入，Mock 模式下不需要安装

    return OpenAI(
        api_key=config.DEEPSEEK_API_KEY,
        base_url=config.DEEPSEEK_BASE_URL,
        timeout=config.LLM_TIMEOUT_SECONDS,
    )


def _call_llm(columns: List[str], sample_data: List[Dict[str, Any]]) -> str:
    """调用 DeepSeek LLM 分析模板 Schema。

    LLM 只接收列名和每列的去重样例值（最多 3 个），不接收完整行数据，
    防止 LLM 意外改写原始值。

    Args:
        columns: 模板列名列表。
        sample_data: 模板前 N 行数据（仅用于提取每列的样例值）。

    Returns:
        LLM 原始响应文本。
    """
    client = _get_client()

    # 为每列提取去重样例值（不保留行间关联，仅用于语义理解）
    column_samples = []
    for col in columns:
        values = []
        for row in sample_data:
            v = str(row.get(col, "")).strip()
            if v and v not in values:
                values.append(v)
            if len(values) >= 3:
                break
        if values:
            column_samples.append(f"  - {col}: e.g. {', '.join(values)}")
        else:
            column_samples.append(f"  - {col}: (empty)")

    global _api_call_count
    _api_call_count += 1

    user_prompt = USER_PROMPT_TEMPLATE.format(
        columns="\n".join(f"  - {c}" for c in columns),
        n=len(sample_data),
        sample_data="\n".join(column_samples),
    )

    response = client.chat.completions.create(
        model=config.DEEPSEEK_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=config.LLM_TEMPERATURE,
        max_tokens=config.LLM_MAX_TOKENS,
    )

    return response.choices[0].message.content or ""


# ── 列覆盖率检测 ──────────────────────────────────────────────


def _get_unmapped_columns(
    all_cols: List[str],
    field_mapping: Dict[str, str],
    composite_col: Optional[str],
    target_col: Optional[str],
    irrelevant_cols: List[str],
) -> List[str]:
    """计算未被覆盖的列名列表。

    已覆盖 = field_mapping 的 key ∪ {composite_col} ∪ {target_col} ∪ irrelevant_cols
    返回不在已覆盖集合中的列名。
    """
    covered = set(field_mapping.keys())
    if composite_col:
        covered.add(composite_col)
    if target_col:
        covered.add(target_col)
    covered.update(irrelevant_cols)
    return [c for c in all_cols if c not in covered]


# ── 响应解析与验证 ──────────────────────────────────────────────


def _extract_json(text: str) -> str:
    """从 LLM 响应中提取 JSON 字符串。

    处理以下格式：
    - 纯 JSON: {"field_mapping": ...}
    - Markdown 代码块: ```json ... ```
    - 无语言标注代码块: ``` ... ```
    """
    text = text.strip()
    # 尝试匹配 ```json ... ``` 或 ``` ... ```
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text


def _validate_response(data: dict, columns: List[str]) -> None:
    """验证 LLM 返回的 Schema 分析结果。

    Args:
        data: 解析后的 JSON dict。
        columns: 原始模板列名列表。

    Raises:
        ValueError: 必填字段缺失或字段值不合法。
    """
    # 必填顶级字段
    if "field_mapping" not in data:
        raise ValueError("LLM 响应缺少 'field_mapping' 字段")
    if not isinstance(data["field_mapping"], dict):
        raise ValueError("'field_mapping' 必须是 dict")

    # field_mapping 值必须是合法的 canonical 字段
    for tcol, cfield in data["field_mapping"].items():
        if cfield not in CANONICAL_FIELDS:
            raise ValueError(
                f"非法 canonical 字段 '{cfield}'（模板列 '{tcol}'），"
                f"合法值: {CANONICAL_FIELDS}"
            )
        if tcol not in columns:
            raise ValueError(
                f"field_mapping 中的模板列 '{tcol}' 不在实际列名中: {columns}"
            )

    # composite_col 如果非空，必须是实际列名
    composite = data.get("composite_col")
    if composite is not None and composite not in columns:
        raise ValueError(
            f"composite_col '{composite}' 不在实际列名中: {columns}"
        )

    # target_col 如果非空，必须是实际列名
    target = data.get("target_col")
    if target is not None and target not in columns:
        raise ValueError(
            f"target_col '{target}' 不在实际列名中: {columns}"
        )

    # irrelevant_cols 必须是列表且值在 columns 中
    irrelevant = data.get("irrelevant_cols", [])
    if not isinstance(irrelevant, list):
        raise ValueError("'irrelevant_cols' 必须是 list")
    for col in irrelevant:
        if col not in columns:
            raise ValueError(
                f"irrelevant_cols 中的 '{col}' 不在实际列名中: {columns}"
            )

    # composite_col 不应该同时出现在 field_mapping 中
    if composite and composite in data["field_mapping"]:
        raise ValueError(
            f"composite_col '{composite}' 不应同时出现在 field_mapping 中"
        )


# ── 公开 API ────────────────────────────────────────────────────


def analyze(
    columns: List[str],
    sample_data: Optional[List[Dict[str, Any]]] = None,
    use_cache: bool = True,
) -> Dict[str, Any]:
    """分析模板 Schema，输出字段映射配置。

    两阶段处理：
      Stage 1（Pre-LLM）：查 column_aliases 记忆，注入已知列映射。
      Stage 2（Post-LLM）：仅有未知列才送 LLM；合并后检测未覆盖列。

    结果缓存：仅在 unrecognized_cols 为空时才写入磁盘指纹缓存，
    确保二次运行时跳过的不完整结果不会进入持久化。

    Args:
        columns: 模板表列名列表（已 strip）。
        sample_data: 模板前 N 行数据，用于提供语义上下文。
        use_cache: 是否使用缓存。默认 True。

    Returns:
        {
            "field_mapping": {"菜品名称": "product_name", "规格": "size", ...},
            "composite_col": "口味做法组合",     # 或 None
            "target_col": "配料",               # 或 None
            "irrelevant_cols": [],              # 忽略的列名列表
            "unrecognized_cols": [],            # 未识别列（供 CLI 交互）
        }

    Raises:
        ValueError: 模板列为空或 LLM 返回结果不合法。
    """
    if not columns:
        raise ValueError("模板列名列表不能为空")

    sample_data = sample_data or []

    # ── 三级缓存：进程内 → 磁盘记忆 → LLM ──
    key = _cache_key(columns)
    if use_cache and key in _cache:
        return _cache[key]

    fingerprint = _template_fingerprint(columns)
    if use_cache:
        cached_rule = mem_get_template_rule(fingerprint)
        if cached_rule is not None:
            _cache[key] = cached_rule
            print(f"[Schema] 缓存命中：模板指纹 {fingerprint[:12]}...（跳过 LLM）")
            return cached_rule

    # ═══════════════════════════════════════════════════════════════
    # Stage 1: Pre-LLM — 注入已知列别名
    # ═══════════════════════════════════════════════════════════════

    from menupilot.data.memory import get_column_alias

    known_fm: Dict[str, str] = {}
    known_irrelevant: List[str] = []
    known_composite: Optional[str] = None
    known_target: Optional[str] = None
    unknown_cols: List[str] = []

    for col in columns:
        alias = get_column_alias(col)
        if alias is None:
            unknown_cols.append(col)
        elif alias == "ignore":
            known_irrelevant.append(col)
        elif alias == "composite_col":
            known_composite = col
        elif alias == "sop":
            known_target = col
        elif alias in CANONICAL_FIELDS:
            known_fm[col] = alias
        else:
            # 未知别名值，交给 LLM
            unknown_cols.append(col)

    # ═══════════════════════════════════════════════════════════════
    # Stage 2: LLM — 仅对未知列调用
    # ═══════════════════════════════════════════════════════════════

    llm_fm: Dict[str, str] = {}
    llm_composite: Optional[str] = None
    llm_target: Optional[str] = None
    llm_irrelevant: List[str] = []
    llm_error: Optional[str] = None

    if unknown_cols:
        if config.USE_MOCK_LLM:
            alias_count = len(columns) - len(unknown_cols)
            if alias_count > 0:
                print(f"[Schema] 列别名命中 {alias_count} 列，剩余 {len(unknown_cols)} 列送 LLM...（Mock 模式）")
            else:
                print("[Schema] 新模板，调用 LLM 分析...（Mock 模式）")
            raw = json.dumps(config.MOCK_SCHEMA_RESPONSE, ensure_ascii=False)
        else:
            alias_count = len(columns) - len(unknown_cols)
            if alias_count > 0:
                print(f"[Schema] 列别名命中 {alias_count} 列，剩余 {len(unknown_cols)} 列送 LLM...")
            else:
                print("[Schema] 新模板，调用 LLM 分析...")
            try:
                # 仅传递未知列的样本数据
                unknown_sample = [
                    {c: row.get(c, "") for c in unknown_cols}
                    for row in sample_data
                ] if sample_data else []
                raw = _call_llm(unknown_cols, unknown_sample)
            except Exception as e:
                llm_error = f"LLM 调用失败: {e}"
                raw = None

        if raw is not None:
            try:
                llm_data = json.loads(_extract_json(raw))
                llm_data.setdefault("composite_col", None)
                llm_data.setdefault("target_col", None)
                llm_data.setdefault("irrelevant_cols", [])

                # 过滤 field_mapping：只保留未知列范围内的映射
                if "field_mapping" in llm_data:
                    llm_fm = {
                        k: v for k, v in llm_data["field_mapping"].items()
                        if k in unknown_cols
                    }
                # 验证过滤后的结果
                _validate_response(
                    {**llm_data, "field_mapping": llm_fm},
                    unknown_cols,
                )
                llm_composite = llm_data.get("composite_col")
                llm_target = llm_data.get("target_col")
                llm_irrelevant = llm_data.get("irrelevant_cols", [])
            except (json.JSONDecodeError, ValueError) as e:
                llm_error = str(e)
    else:
        print("[Schema] 所有列已在列别名中，跳过 LLM")

    # ═══════════════════════════════════════════════════════════════
    # Stage 3: 合并结果
    # ═══════════════════════════════════════════════════════════════

    # field_mapping：已知别名优先，LLM 补充
    fm = dict(known_fm)
    fm.update(llm_fm)

    # composite / target：已知别名优先，LLM 补充
    composite_col = known_composite or llm_composite
    target_col = known_target or llm_target
    irrelevant_cols = known_irrelevant + llm_irrelevant

    # ═══════════════════════════════════════════════════════════════
    # Stage 4: 检测未覆盖列
    # ═══════════════════════════════════════════════════════════════

    unrecognized = _get_unmapped_columns(
        columns, fm, composite_col, target_col, irrelevant_cols
    )

    # LLM 失败 → 原本归 LLM 处理的未知列全部变为未识别
    if llm_error and unknown_cols:
        unrecognized = sorted(set(unrecognized + unknown_cols))
        if not config.USE_MOCK_LLM:
            print(f"[Schema] {llm_error}，{len(unrecognized)} 列待手动确认")

    result = {
        "field_mapping": fm,
        "composite_col": composite_col,
        "target_col": target_col,
        "irrelevant_cols": irrelevant_cols,
        "unrecognized_cols": unrecognized,
    }

    # ── 缓存 ──
    _cache[key] = result

    # 仅在完全解析时写入磁盘指纹缓存（确保二次运行跳过的是完整结果）
    if use_cache and not unrecognized:
        mem_save_template_rule(fingerprint, result)

    return result


# ── 主数据列推断 ────────────────────────────────────────────────

MASTER_INFERENCE_SYSTEM = """\
You are a data schema matching expert. Given candidate columns from a master spreadsheet and a list of required but unfound canonical fields, determine which candidate column matches which canonical field.

## Canonical Fields (for reference)
- product_name: 商品/菜品名称
- size: 规格/杯型
- milk_base: 奶底
- temperature: 温度/做法 (ice level, e.g. 正常冰/少冰/去冰/热/温热)
- sugar: 糖度 (sugar level, e.g. 全糖/七分糖/五分糖/无糖)
- tea_base: 茶底

## Output Format
Return ONLY a JSON object. Each candidate column name is a key. Value is an object with:
- field: canonical field name (or null if cannot match)
- confidence: "high" (sure) or "low" (guess)
- reason: brief one-line explanation in Chinese

Example:
{"温度": {"field": "temperature", "confidence": "high", "reason": "样例值为标准冰温描述"}}"""

MASTER_INFERENCE_USER = """\
## Required Canonical Fields (missing from spreadsheet)
{missing_fields}

## Candidate Columns (with up to 5 sample values each)
{candidate_samples}

Match each candidate column to one of the required fields above. If a column clearly doesn't match any field, set field=null and confidence="low". Only match to fields listed in "Required Canonical Fields"."""

# LLM 推断 hook（测试用）
_inference_hook: Optional[callable] = None


def set_inference_hook(hook: Optional[callable]) -> None:
    """注入自定义主数据列推断回调（用于测试）。设为 None 恢复默认。"""
    global _inference_hook
    _inference_hook = hook


def infer_master_columns(
    candidate_cols: List[str],
    sample_data: dict,
    missing_fields: List[str],
) -> Dict[str, Dict[str, str]]:
    """使用 LLM 推断候选列与缺失 canonical 字段的匹配关系。

    Args:
        candidate_cols: 候选列名列表（未被现有校验覆盖的列）。
        sample_data: {col_name: [sample_value, ...]} 每列最多 5 个样例值。
        missing_fields: 缺失的 canonical 字段名列表
            （如 ["temperature"] — 即 MASTER_REQUIRED_COLUMNS 中未找到的）。

    Returns:
        {col_name: {"field": "temperature"|None, "confidence": "high"|"low", "reason": "..."}}
        只有高置信度的列应该自动映射；低置信度/field=null 应交由交互确认。
    """
    if not candidate_cols or not missing_fields:
        return {}

    # ── 测试模式：优先使用注入的 hook ──
    if _inference_hook is not None:
        return _inference_hook(candidate_cols, sample_data, missing_fields)

    # ── 构建候选列样例 ──
    candidate_lines = []
    for col in candidate_cols:
        vals = sample_data.get(col, [])
        val_str = ", ".join(str(v) for v in vals) if vals else "(空)"
        candidate_lines.append(f"  Column「{col}」: {val_str}")

    # ── 构建缺失字段描述 ──
    FIELD_DESCRIPTIONS = {
        "product_name": "product_name: 商品/菜品名称",
        "size": "size: 规格/杯型",
        "milk_base": "milk_base: 奶底（如 牛奶/燕麦奶/厚乳/椰乳）",
        "temperature": "temperature: 温度/做法（如 正常冰/少冰/去冰/热/温热）",
        "sugar": "sugar: 糖度（如 全糖/七分糖/五分糖/无糖）",
        "tea_base": "tea_base: 茶底（如 红茶/绿茶/乌龙茶）",
    }
    missing_lines = [
        f"  - {FIELD_DESCRIPTIONS.get(f, f)}"
        for f in missing_fields
    ]

    # ── 调用 LLM ──
    user_prompt = MASTER_INFERENCE_USER.format(
        missing_fields="\n".join(missing_lines),
        candidate_samples="\n".join(candidate_lines),
    )

    try:
        client = _get_client()
        response = client.chat.completions.create(
            model=config.DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": MASTER_INFERENCE_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
            max_tokens=512,
        )
        raw = response.choices[0].message.content or "{}"
    except Exception:
        # LLM 不可用 → 全部返回低置信度
        result = {}
        for col in candidate_cols:
            result[col] = {"field": None, "confidence": "low", "reason": "LLM 调用失败"}
        return result

    # ── 解析 ──
    try:
        data = json.loads(_extract_json(raw))
    except json.JSONDecodeError:
        # 无法解析 → 全部低置信度
        result = {}
        for col in candidate_cols:
            result[col] = {"field": None, "confidence": "low", "reason": "LLM 返回无法解析"}
        return result

    # ── 标准化输出格式 ──
    result = {}
    for col in candidate_cols:
        entry = data.get(col)
        if isinstance(entry, dict):
            field = entry.get("field")
            confidence = entry.get("confidence", "low")
            reason = entry.get("reason", "")
            # 仅接受 high + 合法 canonical 字段 的组合
            if confidence == "high" and field in missing_fields:
                result[col] = {"field": field, "confidence": "high", "reason": reason}
            elif field in missing_fields:
                result[col] = {"field": field, "confidence": "low", "reason": reason}
            else:
                result[col] = {"field": None, "confidence": "low",
                               "reason": reason or "LLM 未给出有效匹配"}
        else:
            result[col] = {"field": None, "confidence": "low", "reason": "LLM 未返回此列"}

    return result


def analyze_from_dataframe(
    df: "pd.DataFrame",
    sample_rows: int = 3,
) -> Dict[str, Any]:
    """从模板 DataFrame 直接分析 Schema（便捷方法）。

    Args:
        df: 模板 DataFrame。
        sample_rows: 提供给 LLM 的样本行数。

    Returns:
        同 analyze()。
    """
    columns = list(df.columns)
    sample = df.head(sample_rows).to_dict(orient="records")
    return analyze(columns, sample)


# ── 自测 ────────────────────────────────────────────────────────

if __name__ == "__main__":
    import os
    import pandas as pd

    # 强制使用 Mock 模式进行自测
    os.environ["USE_MOCK_LLM"] = "1"
    import importlib
    importlib.reload(config)

    # 重新导入 schema_analyzer 以获取更新后的 config 引用（from menupilot import config 是动态访问，不需重新导入本模块）
    # 但 config 模块本身需要 reload 以更新 USE_MOCK_LLM

    passed = 0
    failed = 0

    def check(condition, msg):
        global passed, failed
        if condition:
            passed += 1
            print(f"  PASS  {msg}")
        else:
            failed += 1
            print(f"  FAIL  {msg}")

    print("=== Schema Analyzer 自测（Mock 模式）===\n")

    # ── 准备测试数据 ──
    # 模拟真实模板：4 列
    template_df = pd.DataFrame({
        "菜品名称": ["五黄高纤慢养瓶", "五黄高纤慢养瓶"],
        "规格": ["五角瓶", "五角瓶"],
        "口味做法组合": ["红茶, 十二分糖, 温热", "红茶, 十二分糖, 正常冰"],
        "配料": ["", ""],
    })

    columns = list(template_df.columns)
    sample_data = template_df.head(3).to_dict(orient="records")

    # ── 1. 基本分析 ──
    print("1. analyze 基本分析（Mock）")
    result = analyze(columns, sample_data)
    check(isinstance(result, dict), "返回 dict")
    check("field_mapping" in result, "包含 field_mapping")
    check("composite_col" in result, "包含 composite_col")
    check("target_col" in result, "包含 target_col")
    check("irrelevant_cols" in result, "包含 irrelevant_cols")
    print()

    # ── 2. field_mapping 内容 ──
    print("2. field_mapping 内容验证")
    fm = result["field_mapping"]
    check(fm.get("菜品名称") == "product_name", "菜品名称 → product_name")
    check(fm.get("规格") == "size", "规格 → size")
    # composite_col 不应在 field_mapping 中
    check("口味做法组合" not in fm, "复合列不在 field_mapping 中")
    print()

    # ── 3. composite_col / target_col ──
    print("3. composite_col / target_col")
    check(result["composite_col"] == "口味做法组合", "composite_col = 口味做法组合")
    check(result["target_col"] == "配料", "target_col = 配料")
    print()

    # ── 4. irrelevant_cols ──
    print("4. irrelevant_cols")
    check(isinstance(result["irrelevant_cols"], list), "irrelevant_cols 是 list")
    check(len(result["irrelevant_cols"]) == 0, "本次无无关列")
    print()

    # ── 5. 缓存测试 ──
    print("5. 缓存测试")
    reset_cache()
    result1 = analyze(columns, sample_data)
    result2 = analyze(columns, sample_data)  # 第二次应从缓存读取
    check(result1 == result2, "相同 columns 命中缓存（结果一致）")

    # 不同 columns 不应命中缓存
    result3 = analyze(columns + ["额外列"], [])
    check(result3 is not result1, "不同 columns 不命中缓存")
    reset_cache()
    print()

    # ── 6. analyze_from_dataframe ──
    print("6. analyze_from_dataframe 便捷方法")
    result_df = analyze_from_dataframe(template_df)
    check(result_df["composite_col"] == "口味做法组合", "DataFrame 输入正常")
    print()

    # ── 7. 空 columns 应抛异常 ──
    print("7. 空 columns 异常处理")
    try:
        analyze([])
        check(False, "空 columns 应抛异常")
    except ValueError as e:
        check("不能为空" in str(e), f"ValueError: {e}")
    print()

    # ── 8. 含无关列的模板 ──
    print("8. 含无关列的模板分析")
    df_with_extra = pd.DataFrame({
        "序号": [1, 2],
        "商品名": ["珍珠奶茶", "椰果奶茶"],
        "杯型": ["大杯", "中杯"],
        "配料": ["", ""],
        "备注": ["", ""],
    })
    # 覆盖 mock 响应以测试含 irrelevant_cols 的场景
    original_mock = config.MOCK_SCHEMA_RESPONSE
    config.MOCK_SCHEMA_RESPONSE = {
        "field_mapping": {"商品名": "product_name", "杯型": "size"},
        "composite_col": None,
        "target_col": "配料",
        "irrelevant_cols": ["序号", "备注"],
    }
    result_extra = analyze_from_dataframe(df_with_extra)
    check("序号" in result_extra["irrelevant_cols"], "序号 被标为无关列")
    check("备注" in result_extra["irrelevant_cols"], "备注 被标为无关列")
    check(result_extra["composite_col"] is None, "无复合列 → None")
    config.MOCK_SCHEMA_RESPONSE = original_mock
    print()

    # ── 9. 没有复合列的模板 ──
    print("9. 无复合列的模板")
    config.MOCK_SCHEMA_RESPONSE = {
        "field_mapping": {
            "品名": "product_name",
            "杯型": "size",
            "奶底": "milk_base",
            "温度": "temperature",
            "糖度": "sugar",
        },
        "composite_col": None,
        "target_col": "SOP",
        "irrelevant_cols": [],
    }
    result_no_composite = analyze(
        ["品名", "杯型", "奶底", "温度", "糖度", "SOP"],
        [{"品名": "测试", "杯型": "中杯", "奶底": "牛奶", "温度": "少冰", "糖度": "七分糖", "SOP": ""}],
    )
    check(result_no_composite["composite_col"] is None, "无复合列 → composite_col=None")
    check(len(result_no_composite["field_mapping"]) == 5, "5 个字段被映射")
    check(result_no_composite["target_col"] == "SOP", "target_col = SOP")
    check(result_no_composite["irrelevant_cols"] == [], "无无关列")
    config.MOCK_SCHEMA_RESPONSE = original_mock
    print()

    # ── 10. 模板指纹持久化缓存（第一次 → Mock 调用，写入记忆） ──
    print("10. 指纹持久化缓存 — 第一次运行（Mock 调用 + 写入记忆）")
    reset_cache()

    # ── 备份真实 memory.json ──
    import shutil as _shutil
    _mem_path = os.path.expanduser("~/.menupilot/memory.json")
    _mem_backup = None
    if os.path.exists(_mem_path):
        _mem_backup_path = _mem_path + ".self_test_backup"
        _shutil.copy(_mem_path, _mem_backup_path)
        _mem_backup = _mem_backup_path

    from menupilot.data.memory import reset_memory, get_template_rule as mem_get_rule
    reset_memory()

    columns_a = ["菜品名称", "规格", "口味做法组合", "配料"]
    # 计算预期指纹
    expected_fp = _template_fingerprint(columns_a)

    # 第一次：未命中记忆，走 Mock 流程
    result_a1 = analyze(columns_a, [], use_cache=True)
    check(isinstance(result_a1, dict), "第一次返回 dict")
    check(result_a1.get("composite_col") == "口味做法组合", "内容正确")

    # 验证已写入记忆
    cached_a = mem_get_rule(expected_fp)
    check(cached_a is not None, "第一次运行后记忆中有缓存")
    check(cached_a.get("composite_col") == "口味做法组合", "记忆缓存内容正确")
    print()

    # ── 11. 第二次运行同一模板 → 缓存命中，不调用 Mock ──
    print("11. 第二次运行同一模板 → 缓存命中（免 LLM）")
    # 改变 Mock 响应，如果走了 Mock 流程结果会不同 → 可验证是否真正命中缓存
    original_mock = config.MOCK_SCHEMA_RESPONSE
    config.MOCK_SCHEMA_RESPONSE = {
        "field_mapping": {"X": "product_name"},  # 不同的响应
        "composite_col": None,
        "target_col": None,
        "irrelevant_cols": [],
    }

    # 清进程内缓存以仅依赖记忆缓存
    reset_cache()
    result_a2 = analyze(columns_a, [], use_cache=True)

    # 应命中记忆缓存，返回与第一次一致的结果（不是改过的 Mock）
    check(result_a2.get("composite_col") == "口味做法组合",
          "第二次命中记忆缓存 → composite_col 与第一次一致")
    check(result_a2.get("field_mapping") == result_a1.get("field_mapping"),
          "field_mapping 与第一次完全一致")
    config.MOCK_SCHEMA_RESPONSE = original_mock
    print()

    # ── 12. 模板列名变化 → 指纹不同，重新调用 ──
    print("12. 模板列名变化 → 不同指纹 → 重新调用")
    # 覆盖 mock 以包含新列 "备注"（否则 unrecognized_cols 非空 → 不写缓存）
    config.MOCK_SCHEMA_RESPONSE = {
        "field_mapping": {"菜品名称": "product_name", "规格": "size"},
        "composite_col": "口味做法组合",
        "target_col": "配料",
        "irrelevant_cols": ["备注"],
    }
    columns_b = ["菜品名称", "规格", "口味做法组合", "配料", "备注"]
    fp_b = _template_fingerprint(columns_b)
    check(fp_b != expected_fp, f"不同列名 → 不同指纹 ({fp_b[:12]}... ≠ {expected_fp[:12]}...)")

    cached_b_before = mem_get_rule(fp_b)
    check(cached_b_before is None, "新模板指纹初始无缓存")

    result_b = analyze(columns_b, [], use_cache=True)
    check(isinstance(result_b, dict), "新模板分析成功")
    check(result_b["unrecognized_cols"] == [], "全识别 → unrecognized_cols 为空")

    cached_b_after = mem_get_rule(fp_b)
    check(cached_b_after is not None, "新模板结果已写入记忆")
    config.MOCK_SCHEMA_RESPONSE = original_mock
    print()

    # ── 13. 手动删除 memory → 退化为首次运行 ──
    print("13. 手动清空记忆 → 退化为首次运行")
    reset_memory()
    check(mem_get_rule(expected_fp) is None, "清空后原指纹无缓存")

    reset_cache()
    result_a3 = analyze(columns_a, [], use_cache=True)
    check(isinstance(result_a3, dict), "清空记忆后仍正常运行（Mock 兜底）")
    # 清空后重新写入
    cached_a3 = mem_get_rule(expected_fp)
    check(cached_a3 is not None, "清空后再运行 → 重新写入记忆")
    check(cached_a3.get("composite_col") == "口味做法组合", "重新写入内容正确")
    print()

    # ── 14. _template_fingerprint 确定性 ──
    print("14. _template_fingerprint 确定性验证")
    cols_unsorted = ["配料", "规格", "口味做法组合", "菜品名称"]
    fp_unsorted = _template_fingerprint(cols_unsorted)
    check(fp_unsorted == expected_fp, f"列名顺序不影响指纹 ({fp_unsorted[:12]}... = {expected_fp[:12]}...)")
    print()

    # ── 15. _get_unmapped_columns ──
    print("15. _get_unmapped_columns 覆盖率检测")
    check(_get_unmapped_columns(
        ["A", "B", "C"], {"A": "product_name"}, None, None, []
    ) == ["B", "C"], "仅 A 被覆盖 → B, C 未识别")
    check(_get_unmapped_columns(
        ["A", "B", "C"], {"A": "product_name"}, "B", None, ["C"]
    ) == [], "A(fm) + B(composite) + C(irrelevant) → 全覆盖")
    check(_get_unmapped_columns(
        [], {}, None, None, []
    ) == [], "空列名 → 空未识别")
    print()

    # ── 16. pre-LLM 列别名注入 ──
    print("16. pre-LLM 列别名注入（column_aliases 记忆）")
    reset_memory()
    reset_cache()
    from menupilot.data.memory import add_column_alias, get_column_alias as mem_get_col

    # 预设列别名
    add_column_alias("菜品名称", "product_name")
    add_column_alias("备注", "ignore")

    # Mock 响应只覆盖 LLM 看到的未知列
    original_mock = config.MOCK_SCHEMA_RESPONSE
    config.MOCK_SCHEMA_RESPONSE = {
        "field_mapping": {"规格": "size"},
        "composite_col": "口味做法组合",
        "target_col": None,
        "irrelevant_cols": [],
    }

    result16 = analyze(
        ["菜品名称", "规格", "口味做法组合", "备注", "配料"],
        [{"菜品名称": "测试", "规格": "中杯", "口味做法组合": "牛奶,少冰",
          "备注": "备注内容", "配料": ""}],
    )
    # "菜品名称" 已被别名覆盖 → 不送 LLM
    # "备注" → ignore → irrelevant_cols
    # 剩余 ["规格", "口味做法组合", "配料"] → 送 LLM（Mock 覆盖了前两个）
    check(result16["field_mapping"].get("菜品名称") == "product_name",
          "别名注入: 菜品名称 → product_name")
    check(result16["field_mapping"].get("规格") == "size",
          "LLM 补充: 规格 → size")
    check(result16["composite_col"] == "口味做法组合",
          "LLM 补充: composite_col")
    check("备注" in result16["irrelevant_cols"],
          "别名注入: 备注 → ignore → irrelevant_cols")
    # "配料" 既未被别名覆盖，也未被 LLM 映射 → unrecognized
    check("配料" in result16["unrecognized_cols"],
          f"配料 未被识别（实际 unrecognized: {result16['unrecognized_cols']}）")
    check(result16["unrecognized_cols"] == ["配料"],
          "仅 配料 未识别")
    print()

    # ── 17. 不完整结果不写入磁盘缓存 ──
    print("17. 不完整结果（unrecognized_cols 非空）→ 不写入磁盘指纹缓存")
    fp17 = _template_fingerprint(["菜品名称", "规格", "口味做法组合", "备注", "配料"])
    cached17 = mem_get_rule(fp17)
    check(cached17 is None,
          f"存在未识别列时不写入缓存（实际: {cached17 is not None}）")

    # 全识别结果应写入缓存 — 先用 column_aliases 覆盖全部列
    reset_memory()
    reset_cache()
    for col, field in [
        ("菜品名称", "product_name"),
        ("规格", "size"),
        ("口味做法组合", "composite_col"),
        ("备注", "ignore"),
        ("配料", "sop"),
    ]:
        add_column_alias(col, field)
    config.MOCK_SCHEMA_RESPONSE = {}
    result17 = analyze(
        ["菜品名称", "规格", "口味做法组合", "备注", "配料"],
        [{"菜品名称": "测试", "规格": "中杯", "口味做法组合": "牛奶,少冰",
          "备注": "备注内容", "配料": ""}],
    )
    check(result17["unrecognized_cols"] == [],
          f"列别名全覆盖 → unrecognized 为空（实际: {result17['unrecognized_cols']}）")
    check(result17["target_col"] == "配料",
          "别名 '配料' → sop → target_col")
    check(result17["irrelevant_cols"] == ["备注"],
          "别名 '备注' → ignore → irrelevant_cols")
    cached17b = mem_get_rule(fp17)
    check(cached17b is not None,
          "全识别后写入缓存")
    print()

    # ── 18. unrecognized_cols 在返回值中 ──
    print("18. unrecognized_cols 返回值验证")
    reset_memory()
    reset_cache()
    config.MOCK_SCHEMA_RESPONSE = {
        "field_mapping": {"A": "product_name"},
        "composite_col": None,
        "target_col": None,
        "irrelevant_cols": [],
    }
    result18 = analyze(["A", "B", "C"], [{"A": "x", "B": "y", "C": "z"}])
    check("unrecognized_cols" in result18, "返回值含 unrecognized_cols")
    check(isinstance(result18["unrecognized_cols"], list),
          "unrecognized_cols 是 list")
    check(set(result18["unrecognized_cols"]) == {"B", "C"},
          f"B, C 未识别（实际 {result18['unrecognized_cols']}）")
    print()

    # ── 19. infer_master_columns（Mock hook 模式）──
    print("19. infer_master_columns（LLM 主数据列推断）")

    def mock_inference(candidate_cols, sample_data, missing_fields):
        """Mock LLM: 温度→temperature(high), Unnamed→null(low)"""
        result = {}
        for col in candidate_cols:
            if "温度" in col or "做法" in col:
                result[col] = {"field": "temperature", "confidence": "high",
                               "reason": "样例值为冰温描述"}
            elif "Unnamed" in col:
                result[col] = {"field": None, "confidence": "low",
                               "reason": "列数据为空"}
            else:
                result[col] = {"field": None, "confidence": "low",
                               "reason": "无法判断"}
        return result

    set_inference_hook(mock_inference)

    sample = {"温度": ["少冰", "去冰", "正常冰", "热", "温热"],
              "Unnamed: 2": ["", ""],
              "代码": ["T240", "T265"]}

    result19 = infer_master_columns(
        ["温度", "Unnamed: 2", "代码"],
        sample,
        ["temperature"],
    )
    check(result19["温度"]["confidence"] == "high",
          f"温度→high（实际 {result19['温度']['confidence']}）")
    check(result19["温度"]["field"] == "temperature",
          f"温度→temperature（实际 {result19['温度']['field']}）")
    check(result19["Unnamed: 2"]["confidence"] == "low",
          f"Unnamed→low（实际 {result19['Unnamed: 2']['confidence']}）")
    check(result19["Unnamed: 2"]["field"] is None,
          "Unnamed: 2 → field=None")
    check(result19["代码"]["confidence"] == "low",
          "无法判断 → low")
    # 空输入
    check(infer_master_columns([], {}, []) == {}, "空候选/空缺失 → 空结果")
    check(infer_master_columns(["A"], {"A": ["x"]}, []) == {}, "空缺失字段 → 空结果")

    set_inference_hook(None)
    print()

    # 清理
    config.MOCK_SCHEMA_RESPONSE = original_mock
    from menupilot.data.memory import reset_memory as rm
    rm()

    # ── 还原真实 memory.json ──
    if _mem_backup:
        from menupilot.data.memory import reload as _mem_reload
        _shutil.move(_mem_backup, _mem_path)
        _mem_reload()

    # ── 汇总 ──
    print(f"=== 结果: {passed} passed, {failed} failed ===")
