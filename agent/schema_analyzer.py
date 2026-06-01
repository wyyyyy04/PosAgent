"""
Schema Analyzer — LLM 模板字段语义分析。
仅在初始化阶段调用一次，输出字段映射配置供 Rule Engine 使用。
不参与逐行匹配，结果可缓存复用。
"""

import hashlib
import json
import re
from typing import Any, Dict, List, Optional

import config
from data.canonical_schema import CANONICAL_FIELDS

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
## Template Columns
{columns}

## Sample Data (first {n} rows)
{sample_data}

Analyze the template schema and return the JSON field mapping configuration."""

# ── 缓存 ────────────────────────────────────────────────────────

_cache: Dict[str, Dict[str, Any]] = {}


def _cache_key(columns: List[str]) -> str:
    """基于列名列表生成缓存键。"""
    return hashlib.sha256(",".join(sorted(columns)).encode()).hexdigest()


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

    Args:
        columns: 模板列名列表。
        sample_data: 模板前 N 行数据（用于提供语义上下文）。

    Returns:
        LLM 原始响应文本。
    """
    client = _get_client()

    # 格式化样本数据
    sample_lines = []
    for i, row in enumerate(sample_data):
        row_str = " | ".join(f"{col}: {row.get(col, '')}" for col in columns)
        sample_lines.append(f"  Row {i+1}: {row_str}")

    user_prompt = USER_PROMPT_TEMPLATE.format(
        columns="\n".join(f"  - {c}" for c in columns),
        n=len(sample_data),
        sample_data="\n".join(sample_lines),
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

    这是 Schema Analyzer 的唯一公开入口。LLM 仅调用一次，结果缓存复用。

    Args:
        columns: 模板表列名列表（已 strip）。
        sample_data: 模板前 N 行数据，用于提供语义上下文。传入完整行 dict。
        use_cache: 是否使用缓存。默认 True。

    Returns:
        {
            "field_mapping": {"菜品名称": "product_name", "规格": "size", ...},
            "composite_col": "口味做法组合",     # 或 None
            "target_col": "配料",               # 或 None
            "irrelevant_cols": [],              # 忽略的列名列表
        }

    Raises:
        ValueError: 模板列为空或 LLM 返回结果不合法。
        RuntimeError: LLM 调用失败。
    """
    if not columns:
        raise ValueError("模板列名列表不能为空")

    sample_data = sample_data or []

    # 检查缓存
    key = _cache_key(columns)
    if use_cache and key in _cache:
        return _cache[key]

    # Mock 模式（自测用）
    if config.USE_MOCK_LLM:
        raw = json.dumps(config.MOCK_SCHEMA_RESPONSE, ensure_ascii=False)
    else:
        try:
            raw = _call_llm(columns, sample_data)
        except Exception as e:
            raise RuntimeError(f"LLM 调用失败: {e}") from e

    # 解析 JSON
    try:
        data = json.loads(_extract_json(raw))
    except json.JSONDecodeError as e:
        raise ValueError(
            f"LLM 返回内容无法解析为 JSON:\n---\n{raw}\n---\n错误: {e}"
        ) from e

    # 确保必填字段存在并补充默认值
    data.setdefault("composite_col", None)
    data.setdefault("target_col", None)
    data.setdefault("irrelevant_cols", [])

    # 验证
    _validate_response(data, columns)

    # 缓存
    _cache[key] = data
    return data


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

    # 重新导入 schema_analyzer 以获取更新后的 config 引用（import config 是动态访问，不需重新导入本模块）
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

    # ── 汇总 ──
    print(f"=== 结果: {passed} passed, {failed} failed ===")
