"""
Token 词典 — 正向查找（词→类型）与反向查找（类型→词列表）。

词典为软约束：LLM Token Classifier 可识别词典外的值并标注为 UNKNOWN_TOKEN。
"""

from typing import Dict, List

# ── 原始词典定义 ──────────────────────────────────────────────

_RAW_TOKENS: Dict[str, List[str]] = {
    "温度": ["热", "温热", "正常冰", "少冰", "去冰", "冰沙"],
    "糖度": ["全糖", "十二分糖", "标准糖", "七分糖", "五分糖", "三分糖", "不另加糖", "无糖"],
    "奶底": ["牛奶", "燕麦奶", "厚乳", "椰乳"],
    "规格": ["大杯", "中杯", "小杯", "五角瓶"],
    "茶底": ["红茶", "绿茶", "乌龙茶", "五角排红茶"],
}

# ── 构建查找结构 ──────────────────────────────────────────────

# 正向：词 → 类型
TOKEN_MAP: Dict[str, str] = {}
for _type, _words in _RAW_TOKENS.items():
    for _w in _words:
        TOKEN_MAP[_w] = _type

# 反向：类型 → 词列表（只读视图）
TOKEN_BY_TYPE: Dict[str, List[str]] = {k: list(v) for k, v in _RAW_TOKENS.items()}

UNKNOWN_TOKEN = "UNKNOWN_TOKEN"


# ── 公开 API ──────────────────────────────────────────────────

def lookup(token: str) -> str:
    """正向查找：返回 token 对应的类型，词典中没有则返回 UNKNOWN_TOKEN。"""
    return TOKEN_MAP.get(token, UNKNOWN_TOKEN)


def get_tokens_by_type(token_type: str) -> List[str]:
    """反向查找：返回指定类型下的所有 token 列表。"""
    return TOKEN_BY_TYPE.get(token_type, [])


def list_types() -> List[str]:
    """返回所有 Token 类型名称。"""
    return list(TOKEN_BY_TYPE.keys())


def is_known(token: str) -> bool:
    """检查 token 是否在词典中。"""
    return token in TOKEN_MAP


# ── 自测 ──────────────────────────────────────────────────────

if __name__ == "__main__":
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

    print("=== Token 词典自测 ===\n")

    # ── 1. 正向查找：正常词 ──
    print("1. 正向查找（正常词）")
    check(lookup("热") == "温度", "'热' → 温度")
    check(lookup("少冰") == "温度", "'少冰' → 温度")
    check(lookup("七分糖") == "糖度", "'七分糖' → 糖度")
    check(lookup("无糖") == "糖度", "'无糖' → 糖度")
    check(lookup("牛奶") == "奶底", "'牛奶' → 奶底")
    check(lookup("椰乳") == "奶底", "'椰乳' → 奶底")
    check(lookup("中杯") == "规格", "'中杯' → 规格")
    check(lookup("五角瓶") == "规格", "'五角瓶' → 规格")
    check(lookup("红茶") == "茶底", "'红茶' → 茶底")
    check(lookup("乌龙茶") == "茶底", "'乌龙茶' → 茶底")
    print()

    # ── 2. 正向查找：未知词 ──
    print("2. 正向查找（未知词 → UNKNOWN_TOKEN）")
    check(lookup("珍珠") == UNKNOWN_TOKEN, "'珍珠' → UNKNOWN_TOKEN")
    check(lookup("布丁") == UNKNOWN_TOKEN, "'布丁' → UNKNOWN_TOKEN")
    check(lookup("") == UNKNOWN_TOKEN, "空字符串 → UNKNOWN_TOKEN")
    print()

    # ── 3. 反向查找 ──
    print("3. 反向查找（类型 → 词列表）")
    temp_tokens = get_tokens_by_type("温度")
    check(len(temp_tokens) == 6, f"温度 包含 6 个词（实际 {len(temp_tokens)}）")
    check("正常冰" in temp_tokens, "温度 包含 '正常冰'")

    sugar_tokens = get_tokens_by_type("糖度")
    check(len(sugar_tokens) == 8, f"糖度 包含 8 个词（实际 {len(sugar_tokens)}）")
    check("标准糖" in sugar_tokens, "糖度 包含 '标准糖'")

    milk_tokens = get_tokens_by_type("奶底")
    check(len(milk_tokens) == 4, f"奶底 包含 4 个词（实际 {len(milk_tokens)}）")

    size_tokens = get_tokens_by_type("规格")
    check(len(size_tokens) == 4, f"规格 包含 4 个词（实际 {len(size_tokens)}）")

    tea_tokens = get_tokens_by_type("茶底")
    check(len(tea_tokens) == 4, f"茶底 包含 4 个词（实际 {len(tea_tokens)}）")

    unknown_tokens = get_tokens_by_type("不存在的类型")
    check(unknown_tokens == [], "不存在的类型 → 空列表")
    print()

    # ── 4. is_known 辅助 ──
    print("4. is_known 辅助函数")
    check(is_known("去冰") is True, "'去冰' is known")
    check(is_known("珍珠") is False, "'珍珠' is NOT known")
    print()

    # ── 5. list_types ──
    print("5. list_types")
    types = list_types()
    check(len(types) == 5, f"共 5 种类型（实际 {len(types)}）")
    check("温度" in types and "茶底" in types, "包含 '温度' 和 '茶底'")
    print()

    # ── 汇总 ──
    total_types = len(TOKEN_BY_TYPE)
    total_tokens = len(TOKEN_MAP)
    print(f"=== 结果: {passed} passed, {failed} failed ===")
    print(f"=== Token 类型: {total_types} 种, Token 总数: {total_tokens} 个 ===")
