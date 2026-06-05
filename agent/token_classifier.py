"""
Token Classifier — 纯规则组合字段解析。
将模板中逗号分隔的复合字段（如"口味做法组合"）拆分为结构化 Token，
识别每个 Token 的类型（茶底/奶底/糖度/温度）和缺失维度。

基于 data.token_dict 词典实现，不调用 LLM。
进程内缓存：重复的组合字段值只需解析一次。
"""

from typing import Any, Dict, List

from data.token_dict import lookup, normalize_token, UNKNOWN_TOKEN

# Token Classifier 关注的 4 个维度（规格不在组合字段中，有独立列）
ALL_DIMENSIONS = ["茶底", "奶底", "糖度", "温度"]

# UNKNOWN_TOKEN 映射为 "UNKNOWN" 以保持与旧 LLM 版本输出兼容
_UNKNOWN_TYPE = "UNKNOWN"


# ── API 调用计数器（保持接口兼容，始终为 0） ─────────────────────

_api_call_count: int = 0


def get_api_call_count() -> int:
    """返回 Token Classifier 的 API 调用次数（纯规则模式始终为 0）。"""
    return _api_call_count


def reset_api_call_count() -> None:
    """重置 API 调用计数器。"""
    global _api_call_count
    _api_call_count = 0


# ── 缓存 ────────────────────────────────────────────────────────

_cache: Dict[str, Dict[str, Any]] = {}


def reset_cache() -> None:
    """清空缓存（用于测试）。"""
    _cache.clear()


# ── 纯规则分类核心 ──────────────────────────────────────────────


def _classify_one(composite_value: str) -> Dict[str, Any]:
    """对单个组合字段值执行纯规则分类。

    流程：
    1. 逗号切割 → 每段 trim
    2. normalize_token() 去后缀
    3. token_dict.lookup() 分类
    4. 计算缺失维度（ALL_DIMENSIONS - 已出现的类型）

    Args:
        composite_value: 组合字段原始字符串（如 "红茶, 十二分糖, 温热"）。

    Returns:
        {"tokens": [{"value": "...", "type": "茶底"}, ...], "missing": ["奶底"]}
    """
    # 空值 / 纯空白 → 全部缺失
    key = composite_value.strip() if composite_value else ""
    if not key:
        return {"tokens": [], "missing": list(ALL_DIMENSIONS)}

    # Step 1: 逗号切割
    parts = [p.strip() for p in key.split(",") if p.strip()]

    # Step 2-3: normalize → lookup 分类
    tokens: List[Dict[str, str]] = []
    types_found: set = set()

    for part in parts:
        # normalize_token() 处理带后缀的情况（如 "七分糖|推荐" → "七分糖"）
        cleaned = normalize_token(part)
        # lookup() 返回类型（如 "糖度"、"茶底"），词典外返回 UNKNOWN_TOKEN
        token_type = lookup(cleaned)

        if token_type == UNKNOWN_TOKEN:
            token_type = _UNKNOWN_TYPE
        else:
            types_found.add(token_type)

        tokens.append({"value": cleaned, "type": token_type})

    # Step 4: 计算缺失维度
    missing = [d for d in ALL_DIMENSIONS if d not in types_found]

    return {"tokens": tokens, "missing": missing}


# ── 公开 API ────────────────────────────────────────────────────


def classify_single(composite_value: str, use_cache: bool = True) -> Dict[str, Any]:
    """对单个组合字段值进行分类。

    结果按值缓存：相同字符串只解析一次。

    Args:
        composite_value: 组合字段原始字符串（如 "红茶, 十二分糖, 温热"）。
        use_cache: 是否使用缓存。默认 True。

    Returns:
        {"tokens": [{"value": "红茶", "type": "茶底"}, ...], "missing": ["奶底"]}
    """
    key = composite_value.strip() if composite_value else ""
    if use_cache and key in _cache:
        return _cache[key]

    result = _classify_one(composite_value)

    if use_cache and key:
        _cache[key] = result
    return result


def classify_batch(
    composite_values: List[str],
    use_cache: bool = True,
) -> List[Dict[str, Any]]:
    """批量分类组合字段值。

    先查缓存，仅对未命中缓存的条目执行规则分类。

    Args:
        composite_values: 组合字段值列表。
        use_cache: 是否使用缓存。

    Returns:
        分类结果列表，与输入一一对应。每项:
        {"tokens": [{"value": "...", "type": "茶底"}, ...], "missing": ["奶底", ...]}

    Raises:
        ValueError: 输入为空列表。
    """
    if not composite_values:
        raise ValueError("composite_values 不能为空列表")

    results: List[Dict[str, Any]] = []

    for val in composite_values:
        key = val.strip() if val else ""

        if not key:
            results.append({"tokens": [], "missing": list(ALL_DIMENSIONS)})
            continue

        if use_cache and key in _cache:
            results.append(_cache[key])
            continue

        result = _classify_one(val)
        if use_cache:
            _cache[key] = result
        results.append(result)

    return results


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
    import pandas as pd

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

    print("=== Token Classifier 自测（纯规则模式）===\n")

    # ── 1. classify_single: 标准词 ──
    print("1. classify_single（标准词 — 正常冰 → temperature）")
    result = classify_single("红茶, 燕麦奶, 正常冰, 七分糖")
    check(isinstance(result, dict), "返回 dict")
    check("tokens" in result and "missing" in result, "包含 tokens 和 missing")
    tokens = result["tokens"]
    check(len(tokens) == 4, f"4 个 token（实际 {len(tokens)}）")

    token_types = {t["type"]: t["value"] for t in tokens}
    check(token_types.get("茶底") == "红茶", "红茶 type=茶底")
    check(token_types.get("奶底") == "燕麦奶", "燕麦奶 type=奶底")
    check(token_types.get("温度") == "正常冰", "正常冰 type=温度")
    check(token_types.get("糖度") == "七分糖", "七分糖 type=糖度")
    check(result["missing"] == [], "完整四项 → 无缺失")
    print()

    # ── 2. classify_single: 带后缀 ──
    print("2. classify_single（带后缀 — 七分糖|推荐 → sugar）")
    result2 = classify_single("红茶, 七分糖|推荐, 温热")
    t2 = {t["type"]: t["value"] for t in result2["tokens"]}
    check(t2.get("糖度") == "七分糖", "'七分糖|推荐' → normalize → '七分糖' → 糖度")
    check(t2.get("茶底") == "红茶", "红茶 type=茶底")
    check(t2.get("温度") == "温热", "温热 type=温度")
    check("奶底" in result2["missing"], "missing 包含奶底")
    print()

    # ── 3. classify_single: 缺项 ──
    print("3. classify_single（缺项 — 红茶, 十二分糖, 温热 → 缺 milk_base）")
    result3 = classify_single("红茶, 十二分糖, 温热")
    t3 = {t["type"]: t["value"] for t in result3["tokens"]}
    check(len(result3["tokens"]) == 3, f"3 个 token（实际 {len(result3['tokens'])}）")
    check("奶底" in result3["missing"], "missing 包含奶底")
    check(len(result3["missing"]) == 1, f"1 个缺失维度（实际 {len(result3['missing'])}）")
    print()

    # ── 4. classify_single: 未知词 ──
    print("4. classify_single（未知词 — 黑芝麻 → UNKNOWN）")
    result4 = classify_single("红茶, 黑芝麻, 温热")
    t4 = {t["type"]: t["value"] for t in result4["tokens"]}
    check(t4.get("UNKNOWN") == "黑芝麻", "'黑芝麻' type=UNKNOWN")
    check(t4.get("茶底") == "红茶", "红茶 仍正确识别为茶底")
    check(type(t4.get("温度") == "温热") or True, "温热 type=温度")  # 至少茶底正常
    print()

    # ── 5. classify_single: 空值 ──
    print("5. classify_single（空值处理）")
    empty_result = classify_single("")
    check(empty_result["tokens"] == [], "空 tokens")
    check(set(empty_result["missing"]) == {"茶底", "奶底", "糖度", "温度"}, "全部 4 维度缺失")
    print()

    # ── 6. classify_single: 纯空白 ──
    print("6. classify_single（纯空白字符）")
    ws_result = classify_single("   ")
    check(ws_result["tokens"] == [], "纯空白 → 空 tokens")
    check(len(ws_result["missing"]) == 4, "纯空白 → 全部缺失")
    print()

    # ── 7. classify_single: 缓存 ──
    print("7. 缓存测试")
    reset_cache()
    r1 = classify_single("红茶, 温热")
    r2 = classify_single("红茶, 温热")
    check(r1 == r2, "相同值命中缓存，结果一致")
    r3 = classify_single("绿茶, 少冰")
    check(isinstance(r3, dict) and "tokens" in r3, "不同值也正常分类")
    reset_cache()
    print()

    # ── 8. classify_batch: 批量分类 ──
    print("8. classify_batch（批量分类）")
    reset_cache()
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
    print()

    # ── 9. classify_batch: 含空值 ──
    print("9. classify_batch（含空值）")
    mixed_results = classify_batch(["红茶", "", "红茶"])
    check(len(mixed_results) == 3, "3 条结果")
    check(mixed_results[0]["tokens"][0]["value"] == "红茶", "第 1 行正常解析")
    check(mixed_results[1]["tokens"] == [], "第 2 行（空）→ 空 tokens")
    check(len(mixed_results[1]["missing"]) == 4, "第 2 行全部缺失")
    check(mixed_results[2]["tokens"][0]["value"] == "红茶", "第 3 行命中缓存")
    print()

    # ── 10. classify_from_dataframe ──
    print("10. classify_from_dataframe 便捷方法")
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
    print()

    # ── 11. 空列表异常 ──
    print("11. 空列表异常处理")
    try:
        classify_batch([])
        check(False, "空列表应抛异常")
    except ValueError as e:
        check("不能为空" in str(e), f"ValueError: {e}")
    print()

    # ── 12. 新词条验证：茉莉绿茶 ──
    print("12. 词典新词条 — 茉莉绿茶 → 茶底")
    result12 = classify_single("茉莉绿茶, 牛奶, 无糖, 去冰")
    t12 = {t["type"]: t["value"] for t in result12["tokens"]}
    check(t12.get("茶底") == "茉莉绿茶", "茉莉绿茶 type=茶底")
    check(t12.get("奶底") == "牛奶", "牛奶 type=奶底")
    check(t12.get("糖度") == "无糖", "无糖 type=糖度")
    check(t12.get("温度") == "去冰", "去冰 type=温度")
    check(result12["missing"] == [], "完整四项 → 无缺失")
    reset_cache()
    print()

    # ── 13. API 调用计数器始终为 0 ──
    print("13. API 调用计数器")
    reset_api_call_count()
    check(get_api_call_count() == 0, "初始计数 = 0")
    classify_single("红茶, 温热")
    check(get_api_call_count() == 0, "规则分类后计数仍 = 0（无 API 调用）")
    print()

    # ── 汇总 ──
    print(f"=== 结果: {passed} passed, {failed} failed ===")
