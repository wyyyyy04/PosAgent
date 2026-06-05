"""
Token Classifier — LLM 组合字段解析。
将模板中逗号分隔的复合字段（如"口味做法组合"）拆分为结构化 Token，
识别每个 Token 的类型（茶底/奶底/糖度/温度）和缺失维度。

仅在初始化阶段逐行调用一次，结果经 Rule Engine 验证后进入 Matching Engine。
单值缓存：重复的组合字段值只调用一次 LLM。
"""

import json
import re
from typing import Any, Dict, List, Optional

import config

# Token Classifier 关注的 4 个维度（规格不在组合字段中，有独立列）
ALL_DIMENSIONS = ["茶底", "奶底", "糖度", "温度"]
VALID_TOKEN_TYPES = {"茶底", "奶底", "糖度", "温度", "UNKNOWN"}

# ── Prompt 模板 ──────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are a token classifier for composite fields in beverage POS templates.
A composite field contains comma-separated tokens representing different
dimensions of a drink order (tea base, milk base, sugar level, temperature).

## Token Types (with examples)
- 茶底 (tea_base): 红茶, 绿茶, 乌龙茶, 五角排红茶
- 奶底 (milk_base): 牛奶, 燕麦奶, 厚乳, 椰乳
- 糖度 (sugar): 全糖, 十二分糖, 标准糖, 七分糖, 五分糖, 三分糖, 不另加糖, 无糖
- 温度 (temperature): 热, 温热, 正常冰, 少冰, 去冰, 冰沙

## Rules
1. Split each composite value by commas, trim whitespace from each token.
2. Classify each token into exactly ONE of the four types above.
   - If a token does not match any type, still include it but set type to "UNKNOWN".
   - Do NOT guess — if unsure, use "UNKNOWN".
3. For each row, list which of the four types are MISSING (not present in the tokens).
   - All four types (茶底, 奶底, 糖度, 温度) are considered required dimensions;
     if a type is not represented by any token in that row, it goes in "missing".
4. If the composite value is blank/empty, return empty tokens and all four types as missing.

## Output Format
Return ONLY a JSON array (one object per input row). No markdown fences, no extra text:
[
  {
    "tokens": [{"value": "红茶", "type": "茶底"}, {"value": "温热", "type": "温度"}],
    "missing": ["奶底", "糖度"]
  }
]"""

USER_PROMPT_TEMPLATE = """\
Classify each composite field value below into tokens with types, and identify missing dimensions.

{composite_list}

Return the JSON array now."""

# ── API 调用计数器 ─────────────────────────────────────────────

_api_call_count: int = 0


def get_api_call_count() -> int:
    """返回 Token Classifier 的真实 API 调用次数。"""
    return _api_call_count


def reset_api_call_count() -> None:
    """重置 API 调用计数器（用于测试）。"""
    global _api_call_count
    _api_call_count = 0


# ── 缓存 ────────────────────────────────────────────────────────

_cache: Dict[str, Dict[str, Any]] = {}


def reset_cache() -> None:
    """清空缓存（用于测试）。"""
    _cache.clear()


# ── LLM 客户端 ──────────────────────────────────────────────────

def _get_client():
    """创建 DeepSeek API 客户端（延迟导入，Mock 模式下不触发）。"""
    from openai import OpenAI

    return OpenAI(
        api_key=config.DEEPSEEK_API_KEY,
        base_url=config.DEEPSEEK_BASE_URL,
        timeout=config.LLM_TIMEOUT_SECONDS,
    )


def _call_llm(composite_values: List[str]) -> str:
    """调用 DeepSeek LLM 批量分类组合字段。

    Args:
        composite_values: 组合字段值列表（每行一个字符串）。

    Returns:
        LLM 原始响应文本。
    """
    client = _get_client()

    # 构建编号列表
    lines = []
    for i, val in enumerate(composite_values):
        display = val.strip() if val and val.strip() else "(空)"
        lines.append(f"  [{i}] {display}")
    composite_list = "\n".join(lines)

    global _api_call_count
    _api_call_count += 1

    user_prompt = USER_PROMPT_TEMPLATE.format(composite_list=composite_list)

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
    """从 LLM 响应中提取 JSON 字符串。"""
    text = text.strip()
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text


def _validate_result(result: dict, index: int) -> None:
    """验证单行分类结果的结构合法性。

    Args:
        result: 单行分类 dict。
        index: 行号（用于错误信息）。

    Raises:
        ValueError: 结构不合法。
    """
    if not isinstance(result, dict):
        raise ValueError(f"第 {index} 行结果必须是 dict，实际: {type(result)}")

    tokens = result.get("tokens")
    if not isinstance(tokens, list):
        raise ValueError(f"第 {index} 行 'tokens' 必须是 list")

    for j, tok in enumerate(tokens):
        if not isinstance(tok, dict):
            raise ValueError(f"第 {index} 行 token[{j}] 必须是 dict")
        if "value" not in tok:
            raise ValueError(f"第 {index} 行 token[{j}] 缺少 'value'")
        if "type" not in tok:
            raise ValueError(f"第 {index} 行 token[{j}] 缺少 'type'")
        if tok["type"] not in VALID_TOKEN_TYPES:
            raise ValueError(
                f"第 {index} 行 token[{j}] type='{tok['type']}' 不合法，"
                f"合法值: {VALID_TOKEN_TYPES}"
            )

    missing = result.get("missing")
    if not isinstance(missing, list):
        raise ValueError(f"第 {index} 行 'missing' 必须是 list")
    for m in missing:
        if m not in ALL_DIMENSIONS:
            raise ValueError(
                f"第 {index} 行 missing '{m}' 不是合法维度，合法值: {ALL_DIMENSIONS}"
            )

    # 一致性检查：同一类型不应既在 tokens 中又在 missing 中
    present_types = {t["type"] for t in tokens}
    for m in missing:
        if m in present_types:
            raise ValueError(
                f"第 {index} 行: '{m}' 同时出现在 tokens 和 missing 中"
            )


# ── 公开 API ────────────────────────────────────────────────────


def classify_single(composite_value: str, use_cache: bool = True) -> Dict[str, Any]:
    """对单个组合字段值进行分类。

    结果按值缓存：相同字符串只调用一次 LLM。

    Args:
        composite_value: 组合字段原始字符串（如 "红茶, 十二分糖, 温热"）。
        use_cache: 是否使用缓存。默认 True。

    Returns:
        {"tokens": [{"value": "红茶", "type": "茶底"}, ...], "missing": ["奶底"]}
    """
    key = composite_value.strip() if composite_value else ""
    if use_cache and key in _cache:
        return _cache[key]

    # 空值 → 全部缺失
    if not key:
        result = {"tokens": [], "missing": list(ALL_DIMENSIONS)}
        _cache[key] = result
        return result

    # 调用 LLM（批量接口，单条也走批量）
    results = classify_batch([composite_value], use_cache=False)
    result = results[0] if results else {"tokens": [], "missing": list(ALL_DIMENSIONS)}

    if use_cache:
        _cache[key] = result
    return result


def classify_batch(
    composite_values: List[str],
    use_cache: bool = True,
) -> List[Dict[str, Any]]:
    """批量分类组合字段值。

    先查缓存，仅对未命中缓存的条目调用 LLM。

    Args:
        composite_values: 组合字段值列表。
        use_cache: 是否使用缓存。

    Returns:
        分类结果列表，与输入一一对应。每项:
        {"tokens": [{"value": "...", "type": "茶底"}, ...], "missing": ["奶底", ...]}

    Raises:
        ValueError: 输入为空列表或 LLM 返回无法解析。
        RuntimeError: LLM 调用失败。
    """
    if not composite_values:
        raise ValueError("composite_values 不能为空列表")

    # 分离缓存命中与未命中
    results: List[Optional[Dict[str, Any]]] = [None] * len(composite_values)
    uncached_indices: List[int] = []
    uncached_values: List[str] = []

    for i, val in enumerate(composite_values):
        key = val.strip() if val else ""
        if not key:
            results[i] = {"tokens": [], "missing": list(ALL_DIMENSIONS)}
            continue
        if use_cache and key in _cache:
            results[i] = _cache[key]
        else:
            uncached_indices.append(i)
            uncached_values.append(key)

    # 全部命中缓存
    if not uncached_values:
        return [r for r in results if r is not None]  # type: ignore[return-value]

    # 调用 LLM 处理未命中条目（大数据量自动分片）
    _MAX_BATCH = 30  # 每批最多 30 条，防止响应被截断
    if config.USE_MOCK_LLM:
        mock_responses = list(config.MOCK_TOKEN_RESPONSE)
        raw_results = []
        for idx in range(len(uncached_values)):
            raw_results.append(mock_responses[idx % len(mock_responses)])
        # 验证并填充
        for j, (i, raw_item) in enumerate(zip(uncached_indices, raw_results)):
            _validate_result(raw_item, i)
            for tok in raw_item.get("tokens", []):
                tok["value"] = str(tok.get("value", "")).strip()
            results[i] = raw_item
            if use_cache:
                _cache[uncached_values[j]] = raw_item
    else:
        # 分片处理，每片独立调用 LLM
        for chunk_start in range(0, len(uncached_values), _MAX_BATCH):
            chunk_end = min(chunk_start + _MAX_BATCH, len(uncached_values))
            chunk_values = uncached_values[chunk_start:chunk_end]
            chunk_indices = uncached_indices[chunk_start:chunk_end]

            try:
                raw = _call_llm(chunk_values)
            except Exception as e:
                raise RuntimeError(f"LLM 调用失败 (batch {chunk_start}): {e}") from e

            try:
                chunk_results = json.loads(_extract_json(raw))
            except json.JSONDecodeError as e:
                raise ValueError(
                    f"LLM 返回内容无法解析为 JSON (batch {chunk_start}):\n---\n{raw[:500]}...\n---\n错误: {e}"
                ) from e

            if not isinstance(chunk_results, list):
                raise ValueError(
                    f"LLM 返回必须是 JSON 数组 (batch {chunk_start})，实际: {type(chunk_results)}"
                )

            if len(chunk_results) != len(chunk_values):
                raise ValueError(
                    f"LLM 返回 {len(chunk_results)} 条，但请求了 {len(chunk_values)} 条 (batch {chunk_start})"
                )

            # 验证并填充本片结果
            for j, (i, raw_item) in enumerate(zip(chunk_indices, chunk_results)):
                _validate_result(raw_item, i)
                for tok in raw_item.get("tokens", []):
                    tok["value"] = str(tok.get("value", "")).strip()
                results[i] = raw_item
                if use_cache:
                    _cache[uncached_values[chunk_start + j]] = raw_item

    # 安全兜底：任何未被填充的位置返回空结果
    for i in range(len(results)):
        if results[i] is None:
            results[i] = {"tokens": [], "missing": list(ALL_DIMENSIONS)}

    return results  # type: ignore[return-value]


def classify_from_dataframe(
    df: "pd.DataFrame",
    composite_col: str,
) -> List[Dict[str, Any]]:
    """从模板 DataFrame 的组合列直接分类（便捷方法）。

    Args:
        df: 模板 DataFrame。
        composite_col: 组合字段列名。

    Returns:
        同 classify_batch()。

    Raises:
        ValueError: composite_col 不在 DataFrame 中。
    """
    if composite_col not in df.columns:
        raise ValueError(
            f"组合列 '{composite_col}' 不在 DataFrame 列中: {list(df.columns)}"
        )
    values = df[composite_col].astype(str).tolist()
    return classify_batch(values)


# ── 自测 ────────────────────────────────────────────────────────

if __name__ == "__main__":
    import os
    import pandas as pd

    os.environ["USE_MOCK_LLM"] = "1"
    import importlib

    importlib.reload(config)

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

    print("=== Token Classifier 自测（Mock 模式）===\n")

    # ── 1. classify_single: 正常值 ──
    print("1. classify_single（正常组合字段）")
    result = classify_single("红茶, 十二分糖, 温热")
    check(isinstance(result, dict), "返回 dict")
    check("tokens" in result and "missing" in result, "包含 tokens 和 missing")
    tokens = result["tokens"]
    check(len(tokens) == 3, f"3 个 token（实际 {len(tokens)}）")

    # 检查具体 token
    token_values = {t["value"] for t in tokens}
    check("红茶" in token_values, "包含 '红茶'")
    check("十二分糖" in token_values, "包含 '十二分糖'")
    check("温热" in token_values, "包含 '温热'")

    token_types = {t["type"]: t["value"] for t in tokens}
    check(token_types.get("茶底") == "红茶", "红茶 type=茶底")
    check(token_types.get("糖度") == "十二分糖", "十二分糖 type=糖度")
    check(token_types.get("温度") == "温热", "温热 type=温度")

    check("奶底" in result["missing"], "missing 包含奶底")
    check(len(result["missing"]) == 1, f"1 个缺失维度（实际 {len(result['missing'])}）")
    print()

    # ── 2. classify_single: 空值 ──
    print("2. classify_single（空值处理）")
    empty_result = classify_single("")
    check(empty_result["tokens"] == [], "空 tokens")
    check(set(empty_result["missing"]) == {"茶底", "奶底", "糖度", "温度"}, "全部 4 维度缺失")
    print()

    # ── 3. classify_single: 完整四项 ──
    print("3. classify_single（完整四项）")
    original_mock = config.MOCK_TOKEN_RESPONSE
    config.MOCK_TOKEN_RESPONSE = [
        {
            "tokens": [
                {"value": "红茶", "type": "茶底"},
                {"value": "燕麦奶", "type": "奶底"},
                {"value": "五分糖", "type": "糖度"},
                {"value": "少冰", "type": "温度"},
            ],
            "missing": [],
        }
    ]
    reset_cache()
    full_result = classify_single("红茶, 燕麦奶, 五分糖, 少冰")
    check(len(full_result["tokens"]) == 4, f"4 个 token（实际 {len(full_result['tokens'])}）")
    check(full_result["missing"] == [], "无缺失维度")
    config.MOCK_TOKEN_RESPONSE = original_mock
    reset_cache()
    print()

    # ── 4. classify_single: 缓存 ──
    print("4. 缓存测试")
    # 通过两次连续调用验证缓存行为（不依赖模块导入，避免 __main__ vs agent.token_classifier 双实例问题）
    reset_cache()
    # 用 use_cache=False 确保不走缓存
    r_nocache = classify_single("红茶, 温热", use_cache=False)
    check(isinstance(r_nocache, dict), "use_cache=False 正常返回")

    r1 = classify_single("红茶, 温热")
    r2 = classify_single("红茶, 温热")
    check(r1 == r2, "相同值命中缓存，结果一致")

    # 不同值返回不同结果（在 Mock 模式下可能内容相同，但各自独立分类）
    r3 = classify_single("绿茶, 少冰")
    check(isinstance(r3, dict) and "tokens" in r3, "不同值也正常分类")
    reset_cache()
    print()

    # ── 5. classify_batch: 批量分类 ──
    print("5. classify_batch（批量分类）")
    reset_cache()
    config.MOCK_TOKEN_RESPONSE = [
        {
            "tokens": [
                {"value": "红茶", "type": "茶底"},
                {"value": "十二分糖", "type": "糖度"},
                {"value": "温热", "type": "温度"},
            ],
            "missing": ["奶底"],
        },
        {
            "tokens": [
                {"value": "燕麦奶", "type": "奶底"},
                {"value": "正常冰", "type": "温度"},
                {"value": "七分糖", "type": "糖度"},
            ],
            "missing": ["茶底"],
        },
        {
            "tokens": [
                {"value": "乌龙茶", "type": "茶底"},
                {"value": "椰乳", "type": "奶底"},
                {"value": "三分糖", "type": "糖度"},
                {"value": "去冰", "type": "温度"},
            ],
            "missing": [],
        },
    ]
    batch_results = classify_batch([
        "红茶, 十二分糖, 温热",
        "燕麦奶, 正常冰, 七分糖",
        "乌龙茶, 椰乳, 三分糖, 去冰",
    ])
    check(len(batch_results) == 3, f"3 条结果（实际 {len(batch_results)}）")
    check(len(batch_results[0]["tokens"]) == 3, "第 1 行 3 个 token")
    check("奶底" in batch_results[0]["missing"], "第 1 行 missing 奶底")
    check(len(batch_results[1]["tokens"]) == 3, "第 2 行 3 个 token")
    check("茶底" in batch_results[1]["missing"], "第 2 行 missing 茶底")
    check(len(batch_results[2]["tokens"]) == 4, "第 3 行 4 个 token")
    check(batch_results[2]["missing"] == [], "第 3 行无缺失")
    config.MOCK_TOKEN_RESPONSE = original_mock
    reset_cache()
    print()

    # ── 6. classify_batch: 含空值的批量 ──
    print("6. classify_batch（含空值）")
    config.MOCK_TOKEN_RESPONSE = [
        {"tokens": [{"value": "红茶", "type": "茶底"}], "missing": ["奶底", "糖度", "温度"]},
    ]
    mixed_results = classify_batch(["红茶", "", "红茶"])
    check(len(mixed_results) == 3, "3 条结果")
    check(mixed_results[0]["tokens"][0]["value"] == "红茶", "第 1 行正常解析")
    check(mixed_results[1]["tokens"] == [], "第 2 行（空）→ 空 tokens")
    check(len(mixed_results[1]["missing"]) == 4, "第 2 行全部缺失")
    check(mixed_results[2]["tokens"][0]["value"] == "红茶", "第 3 行命中缓存")
    config.MOCK_TOKEN_RESPONSE = original_mock
    reset_cache()
    print()

    # ── 7. classify_from_dataframe ──
    print("7. classify_from_dataframe 便捷方法")
    config.MOCK_TOKEN_RESPONSE = [
        {"tokens": [{"value": "红茶", "type": "茶底"}, {"value": "温热", "type": "温度"}], "missing": ["奶底", "糖度"]},
        {"tokens": [{"value": "绿茶", "type": "茶底"}, {"value": "少冰", "type": "温度"}], "missing": ["奶底", "糖度"]},
    ]
    df = pd.DataFrame({
        "菜品名称": ["测试A", "测试B"],
        "口味做法组合": ["红茶, 温热", "绿茶, 少冰"],
    })
    df_results = classify_from_dataframe(df, "口味做法组合")
    check(len(df_results) == 2, "2 条结果")
    check(df_results[0]["tokens"][0]["value"] == "红茶", "DataFrame 第 1 行正确")
    check(df_results[1]["tokens"][0]["value"] == "绿茶", "DataFrame 第 2 行正确")

    # composite_col 不存在应抛异常
    try:
        classify_from_dataframe(df, "不存在的列")
        check(False, "不存在的列应抛异常")
    except ValueError as e:
        check("不在 DataFrame 列中" in str(e), f"ValueError: {e}")
    config.MOCK_TOKEN_RESPONSE = original_mock
    reset_cache()
    print()

    # ── 8. 空列表异常 ──
    print("8. 空列表异常处理")
    try:
        classify_batch([])
        check(False, "空列表应抛异常")
    except ValueError as e:
        check("不能为空" in str(e), f"ValueError: {e}")
    print()

    # ── 9. 空白字符值 ──
    print("9. 空白字符值处理")
    ws_result = classify_single("   ")
    check(ws_result["tokens"] == [], "纯空白 → 空 tokens")
    check(len(ws_result["missing"]) == 4, "纯空白 → 全部缺失")
    reset_cache()
    print()

    # ── 汇总 ──
    print(f"=== 结果: {passed} passed, {failed} failed ===")
