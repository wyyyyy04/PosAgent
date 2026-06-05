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
from data.memory import get_template_rule as mem_get_template_rule
from data.memory import save_template_rule as mem_save_template_rule

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

    # ── 三级缓存：进程内 → 磁盘记忆 → LLM ──
    key = _cache_key(columns)
    if use_cache and key in _cache:
        return _cache[key]

    # 计算模板指纹，查询磁盘记忆
    fingerprint = _template_fingerprint(columns)
    if use_cache:
        cached_rule = mem_get_template_rule(fingerprint)
        if cached_rule is not None:
            # 命中磁盘记忆，同步到进程内缓存
            _cache[key] = cached_rule
            print(f"[Schema] 缓存命中：模板指纹 {fingerprint[:12]}...（跳过 LLM）")
            return cached_rule

    # 未命中，需调用 LLM（或 Mock）
    if config.USE_MOCK_LLM:
        print("[Schema] 新模板，调用 LLM 分析...（Mock 模式）")
        raw = json.dumps(config.MOCK_SCHEMA_RESPONSE, ensure_ascii=False)
    else:
        print("[Schema] 新模板，调用 LLM 分析...")
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

    # 写入进程内缓存
    _cache[key] = data

    # 写入磁盘记忆（持久化）
    if use_cache:
        mem_save_template_rule(fingerprint, data)

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

    # ── 10. 模板指纹持久化缓存（第一次 → Mock 调用，写入记忆） ──
    print("10. 指纹持久化缓存 — 第一次运行（Mock 调用 + 写入记忆）")
    reset_cache()
    from data.memory import reset_memory, get_template_rule as mem_get_rule
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
    columns_b = ["菜品名称", "规格", "口味做法组合", "配料", "备注"]
    fp_b = _template_fingerprint(columns_b)
    check(fp_b != expected_fp, f"不同列名 → 不同指纹 ({fp_b[:12]}... ≠ {expected_fp[:12]}...)")

    cached_b_before = mem_get_rule(fp_b)
    check(cached_b_before is None, "新模板指纹初始无缓存")

    result_b = analyze(columns_b, [], use_cache=True)
    check(isinstance(result_b, dict), "新模板分析成功")

    cached_b_after = mem_get_rule(fp_b)
    check(cached_b_after is not None, "新模板结果已写入记忆")
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

    # 清理
    from data.memory import reset_memory as rm
    rm()

    # ── 汇总 ──
    print(f"=== 结果: {passed} passed, {failed} failed ===")
