"""
Token Classifier — 纯规则组合字段解析 + 未知词兜底。
将模板中逗号分隔的复合字段（如"口味做法组合"）拆分为结构化 Token，
识别每个 Token 的类型（茶底/奶底/糖度/温度）和缺失维度。

三级兜底机制：
  Step 1: data.token_dict 标准词典
  Step 2: data.memory 长期记忆（用户确认过的词）
  Step 3: 交互式询问用户（同一词每进程只问一次）
"""

from typing import Any, Dict, List, Optional

from data.memory import add_token as mem_add_token
from data.memory import get_token_type as mem_get_token_type
from data.token_dict import lookup, normalize_token, UNKNOWN_TOKEN

# Token Classifier 关注的 4 个维度（规格不在组合字段中，有独立列）
ALL_DIMENSIONS = ["茶底", "奶底", "糖度", "温度"]

# UNKNOWN_TOKEN 映射为 "UNKNOWN" 以保持输出兼容
_UNKNOWN_TYPE = "UNKNOWN"

# 合法类型列表（供交互式询问展示）
_VALID_TYPE_NAMES = ["茶底", "奶底", "糖度", "温度", "规格"]

# ── API 调用计数器（纯规则模式始终为 0） ─────────────────────────

_api_call_count: int = 0


def get_api_call_count() -> int:
    return _api_call_count


def reset_api_call_count() -> None:
    global _api_call_count
    _api_call_count = 0


# ── 缓存 ────────────────────────────────────────────────────────

_cache: Dict[str, Dict[str, Any]] = {}

# 进程内「已询问过」的未知词集合（同一词本进程只问一次）
_asked_this_session: set = set()


def reset_cache() -> None:
    """清空缓存（用于测试）。"""
    _cache.clear()
    global _asked_this_session
    _asked_this_session = set()


def reset_session_asked() -> None:
    """清空「已询问」集合（仅测试用）。"""
    global _asked_this_session
    _asked_this_session = set()


# ── 交互式未知词确认 ────────────────────────────────────────────


def prompt_user_for_unknown(word: str, context: str) -> Optional[Dict[str, Any]]:
    """交互式询问用户如何处理未知词。

    此函数可被外部 mock 替换（测试时注入自定义回调）。

    Args:
        word: 未知 token 文本（已 normalize）。
        context: 所在行的组合字段完整值，供用户参考。

    Returns:
        {"action": "add", "type": "茶底"}   → 加入记忆并继续
        {"action": "unknown"}              → 标记为 UNKNOWN 继续
        {"action": "skip"}                 → 跳过此行
    """
    print(f"\n{'='*56}")
    print(f"[未知词] 无法识别的 Token: 「{word}」")
    print(f"  所在行上下文: {context}")
    print(f"{'='*56}")
    print("  请选择处理方式:")
    print("    1. 加入词典（需选择类型）")
    print("    2. 标记为 UNKNOWN（继续处理）")
    print("    3. 跳过此行")

    while True:
        choice = input("  请输入 1/2/3: ").strip()
        if choice == "1":
            while True:
                print(f"  可选类型: {', '.join(_VALID_TYPE_NAMES)}")
                type_choice = input(f"  请选择「{word}」的类型 (1=茶底 2=奶底 3=糖度 4=温度 5=规格): ").strip()
                type_map = {
                    "1": "茶底", "2": "奶底", "3": "糖度",
                    "4": "温度", "5": "规格",
                }
                if type_choice in type_map:
                    return {"action": "add", "type": type_map[type_choice]}
                # 也支持直接输入中文类型名
                if type_choice in _VALID_TYPE_NAMES:
                    return {"action": "add", "type": type_choice}
                print(f"  [错误] 无效类型，请重新选择")
        elif choice == "2":
            return {"action": "unknown"}
        elif choice == "3":
            return {"action": "skip"}
        else:
            print("  [错误] 无效输入，请输入 1、2 或 3")


# 用于测试时注入自定义回调的钩子
_prompt_hook: Optional[callable] = None


def set_prompt_hook(hook: Optional[callable]) -> None:
    """注入自定义未知词处理回调（用于自动化测试）。

    hook 签名应与 prompt_user_for_unknown 一致：
        def hook(word: str, context: str) -> dict
    设为 None 恢复默认交互式行为。
    """
    global _prompt_hook
    _prompt_hook = hook


# ── 纯规则分类核心 ──────────────────────────────────────────────


def _classify_one(composite_value: str) -> Dict[str, Any]:
    """对单个组合字段值执行纯规则分类 + 未知词兜底。

    流程：
    1. 逗号切割 → 每段 trim
    2. normalize_token() 去后缀
    3. token_dict.lookup() 分类（Step 1）
    4. 未命中 → 查 memory.py（Step 2）
    5. 仍未命中 → 交互式询问用户（Step 3，同词仅问一次）

    Args:
        composite_value: 组合字段原始字符串（如 "红茶, 十二分糖, 温热"）。

    Returns:
        {"tokens": [{"value": "...", "type": "茶底"}, ...], "missing": ["奶底"]}
        若用户选择跳过此行，附带 "_skipped": True。
    """
    key = composite_value.strip() if composite_value else ""
    if not key:
        return {"tokens": [], "missing": list(ALL_DIMENSIONS)}

    # Step 1: 逗号切割
    parts = [p.strip() for p in key.split(",") if p.strip()]

    tokens: List[Dict[str, str]] = []
    types_found: set = set()
    skipped = False

    for part in parts:
        # normalize_token() 处理带后缀的情况（如 "七分糖|推荐" → "七分糖"）
        cleaned = normalize_token(part)

        # Step 1: 查标准词典
        token_type = lookup(cleaned)

        if token_type == UNKNOWN_TOKEN:
            # Step 2: 查长期记忆
            mem_type = mem_get_token_type(cleaned)
            if mem_type:
                token_type = mem_type
                types_found.add(token_type)
                tokens.append({"value": cleaned, "type": token_type})
                continue

            # Step 3: 交互式询问（同词仅问一次）
            if cleaned in _asked_this_session:
                token_type = _UNKNOWN_TYPE
            else:
                _asked_this_session.add(cleaned)
                hook = _prompt_hook if _prompt_hook else prompt_user_for_unknown
                response = hook(cleaned, composite_value)

                if response["action"] == "add":
                    mem_add_token(cleaned, response["type"])
                    token_type = response["type"]
                    types_found.add(token_type)
                    tokens.append({"value": cleaned, "type": token_type})
                    continue
                elif response["action"] == "skip":
                    skipped = True
                    token_type = _UNKNOWN_TYPE
                else:  # "unknown"
                    token_type = _UNKNOWN_TYPE
        else:
            types_found.add(token_type)

        tokens.append({"value": cleaned, "type": token_type})

    # Step 5: 计算缺失维度
    missing = [d for d in ALL_DIMENSIONS if d not in types_found]
    result = {"tokens": tokens, "missing": missing}
    if skipped:
        result["_skipped"] = True
    return result


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
    from data.memory import reset_memory, get_token_type as mem_get

    # 自测使用临时记忆，避免污染真实数据
    reset_memory()

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

    print("=== Token Classifier 自测（纯规则 + 记忆兜底）===\n")

    # ── 0. 清空状态 ──
    reset_cache()

    # ── 1. 标准词：直接返回，不触发询问 ──
    print("1. 标准词 — 直接返回，不触发询问")
    result1 = classify_single("红茶, 燕麦奶, 正常冰, 七分糖")
    check(len(result1["tokens"]) == 4, "4 个 token")
    t1 = {t["type"]: t["value"] for t in result1["tokens"]}
    check(t1.get("茶底") == "红茶", "红茶 type=茶底")
    check(t1.get("奶底") == "燕麦奶", "燕麦奶 type=奶底")
    check(t1.get("温度") == "正常冰", "正常冰 type=温度")
    check(t1.get("糖度") == "七分糖", "七分糖 type=糖度")
    check(result1["missing"] == [], "完整四项 → 无缺失")
    print()

    # ── 2. 长期记忆中的词 → 直接返回，不触发询问 ──
    print("2. 长期记忆中的词 — 直接返回（mem_get_token_type）")
    # 预先写入记忆
    from data.memory import add_token as mem_add
    mem_add("黑芝麻仙草", "茶底")
    check(mem_get("黑芝麻仙草") == "茶底", "记忆写入了 '黑芝麻仙草'")

    result2 = classify_single("黑芝麻仙草, 牛奶, 少冰, 全糖")
    check(len(result2["tokens"]) == 4, "4 个 token")
    t2 = {t["type"]: t["value"] for t in result2["tokens"]}
    check(t2.get("茶底") == "黑芝麻仙草", "记忆中 '黑芝麻仙草' type=茶底")
    check(t2.get("奶底") == "牛奶", "牛奶 type=奶底")
    check(result2["missing"] == [], "无缺失")
    print()

    # ── 3. 全新未知词 → 模拟用户选择「加入词典」 ──
    print("3. 全新未知词 — 模拟用户输入 1 + 1（加入茶底）")

    # 模拟 hook：第一次询问 → add as 茶底
    call_count = [0]

    def mock_hook_1(word, context):
        call_count[0] += 1
        print(f"  [MOCK] 询问未知词: '{word}', 上下文: '{context}'")
        print(f"  [MOCK] 用户选择 1 → 加入词典，类型 1（茶底）")
        return {"action": "add", "type": "茶底"}

    set_prompt_hook(mock_hook_1)
    reset_cache()
    reset_session_asked()

    result3 = classify_single("豆乳奶茶, 正常冰, 七分糖")
    check(call_count[0] == 1, "触发了 1 次询问（'豆乳奶茶' 未知）")
    check(mem_get("豆乳奶茶") == "茶底", "写入记忆后可查到 '豆乳奶茶' = 茶底")
    t3 = {t["type"]: t["value"] for t in result3["tokens"]}
    check(t3.get("茶底") == "豆乳奶茶", "分类结果中 '豆乳奶茶' type=茶底")
    check(t3.get("温度") == "正常冰", "正常冰 仍正确")
    check(t3.get("糖度") == "七分糖", "七分糖 仍正确")
    print()

    # ── 4. 同词第二次出现 → 不重复询问（已记忆） ──
    print("4. 已记忆词第二次出现 — 不触发询问")
    call_count[0] = 0
    result4 = classify_single("豆乳奶茶, 牛奶, 温热, 五分糖")
    check(call_count[0] == 0, "不再触发询问（记忆命中）")
    t4 = {t["type"]: t["value"] for t in result4["tokens"]}
    check(t4.get("茶底") == "豆乳奶茶", "记忆命中后 type 正确")
    check(result4["missing"] == [], "无缺失")
    print()

    # ── 5. 同进程内同词不重复询问（_asked_this_session） ──
    print("5. 同进程内同词不重复询问（_asked_this_session 缓存）")
    reset_memory()
    call_count[0] = 0

    def mock_hook_2(word, context):
        call_count[0] += 1
        print(f"  [MOCK] 询问: '{word}' → 用户选 2（标 UNKNOWN）")
        return {"action": "unknown"}

    set_prompt_hook(mock_hook_2)
    reset_cache()
    reset_session_asked()

    # 同一个词出现两次，两次都在不同行
    r5a = classify_single("抹茶粉, 去冰")
    r5b = classify_single("抹茶粉, 少冰")
    check(call_count[0] == 1, f"同词只问 1 次（实际 {call_count[0]}）")
    # 两次结果中该词都应是 UNKNOWN
    types_a = {t["type"]: t["value"] for t in r5a["tokens"]}
    types_b = {t["type"]: t["value"] for t in r5b["tokens"]}
    check(types_a.get("UNKNOWN") == "抹茶粉", "第一次 UNKNOWN")
    check(types_b.get("UNKNOWN") == "抹茶粉", "第二次 UNKNOWN（会话缓存）")
    print()

    # ── 6. 模拟用户选择「跳过此行」 ──
    print("6. 模拟用户选择「跳过此行」")

    def mock_hook_3(word, context):
        print(f"  [MOCK] 询问: '{word}' → 用户选 3（跳过）")
        return {"action": "skip"}

    set_prompt_hook(mock_hook_3)
    reset_cache()
    reset_session_asked()

    result6 = classify_single("未知成分X, 正常冰")
    check(result6.get("_skipped") is True, "结果标记 _skipped=True")
    check(any(t["type"] == "UNKNOWN" for t in result6["tokens"]), "未知词标为 UNKNOWN")
    print()

    # ── 7. 空值 / 纯空白 ──
    print("7. 空值 / 纯空白处理")
    set_prompt_hook(None)
    reset_cache()
    empty_result = classify_single("")
    check(empty_result["tokens"] == [], "空 tokens")
    check(len(empty_result["missing"]) == 4, "4 维全缺失")
    ws_result = classify_single("   ")
    check(ws_result["tokens"] == [], "纯空白 → 空 tokens")
    print()

    # ── 8. classify_batch: 批量（含内存命中） ──
    print("8. classify_batch（批量，含内存命中）")
    reset_cache()
    reset_session_asked()

    # 预置记忆
    reset_memory()
    mem_add("茉莉绿茶", "茶底")

    batch_results = classify_batch([
        "红茶, 十二分糖, 温热",
        "",                                  # 空值
        "茉莉绿茶, 牛奶, 无糖, 去冰",         # 茉莉绿茶在记忆中
    ])
    check(len(batch_results) == 3, f"3 条结果（实际 {len(batch_results)}）")
    check(len(batch_results[0]["tokens"]) == 3, "第 1 行 3 个 token")
    check("奶底" in batch_results[0]["missing"], "第 1 行 missing 奶底")
    check(batch_results[1]["tokens"] == [], "第 2 行（空）→ 空 tokens")
    t8 = {t["type"]: t["value"] for t in batch_results[2]["tokens"]}
    check(t8.get("茶底") == "茉莉绿茶", "记忆中 '茉莉绿茶' → 茶底")
    check(batch_results[2]["missing"] == [], "第 3 行无缺失")
    print()

    # ── 9. classify_from_dataframe ──
    print("9. classify_from_dataframe 便捷方法")
    df = pd.DataFrame({
        "菜品名称": ["测试A", "测试B"],
        "口味做法组合": ["红茶, 温热", "绿茶, 少冰"],
    })
    df_results = classify_from_dataframe(df, "口味做法组合")
    check(len(df_results) == 2, "2 条结果")
    check(df_results[0]["tokens"][0]["value"] == "红茶", "DataFrame 第 1 行正确")

    try:
        classify_from_dataframe(df, "不存在的列")
        check(False, "不存在的列应抛异常")
    except ValueError as e:
        check("不在 DataFrame 列中" in str(e), f"ValueError: {e}")
    print()

    # ── 10. API 调用计数器 ──
    print("10. API 调用计数器始终为 0")
    reset_api_call_count()
    check(get_api_call_count() == 0, "初始 = 0")
    classify_single("红茶, 温热")
    check(get_api_call_count() == 0, "规则执行后仍 = 0")
    print()

    # ── 11. set_prompt_hook(None) 恢复默认 ──
    print("11. set_prompt_hook(None) 恢复默认交互")
    set_prompt_hook(None)
    check(_prompt_hook is None, "hook 已清除")
    print()

    # ── 12. 缓存验证 ──
    print("12. 缓存验证（含记忆命中）")
    reset_cache()
    reset_session_asked()
    reset_memory()
    mem_add("抹茶", "茶底")

    r12a = classify_single("抹茶, 温热")
    r12b = classify_single("抹茶, 温热")
    check(r12a == r12b, "相同值命中缓存，结果一致")
    print()

    # 清理：清除 hook
    set_prompt_hook(None)

    # ── 汇总 ──
    print(f"=== 结果: {passed} passed, {failed} failed ===")
