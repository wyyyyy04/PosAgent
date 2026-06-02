"""
Token 词典 — 正向查找（词→类型）与反向查找（类型→词列表）。

词典为软约束：LLM Token Classifier 可识别词典外的值并标注为 UNKNOWN_TOKEN。
"""

from typing import Dict, List, Optional

# ── 原始词典定义 ──────────────────────────────────────────────

_RAW_TOKENS: Dict[str, List[str]] = {
    "温度": ["热", "温热", "正常冰", "少冰", "去冰", "冰沙"],
    "糖度": ["全糖", "十二分糖", "标准糖", "七分糖", "五分糖", "三分糖", "不另加糖", "无糖"],
    "奶底": ["牛奶", "燕麦奶", "厚乳", "椰乳"],
    "规格": ["大杯", "中杯", "小杯", "五角瓶"],
    "茶底": ["红茶", "绿茶", "乌龙茶", "五角排红茶", "五黄标准茶"],
}

# ── 已知后缀模式（文档备忘）──────────────────────────────────
# 这些后缀会附加到属性值尾部，导致精确匹配失败。
# normalize_token() 通过分隔符边界检测自动处理，不依赖此列表。
# 列表仅供人工审查，发现新后缀时追加至此。
KNOWN_SUFFIXES: List[str] = [
    "|推荐",   # 主数据糖度列常见：七分糖|推荐
    "/新",     # 模板规格列偶尔出现：大杯/新
]

# 分隔符集合：用于子串边界检测和后缀切割
_SEPARATORS = frozenset({"|", "/", " "})

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


def normalize_token(raw_value: str) -> str:
    """按四级优先级从词典中清洗带后缀的属性值。

    四级优先级：
      Step 1 精确匹配：直接在词典中查找，命中即返回原值
      Step 2 子串匹配：词典词作为 raw_value 的子串出现，且子串的
              左右边界均为字符串端点或分隔符（|、/、空格）。
              多个匹配时取最长词，防止"冰"误匹配"正常冰沙"。
      Step 3 分隔符切割：按 |、/、空格依次切分，取第一个 token 精确匹配
      Step 4 全部失败：返回原值（调用方通过 lookup/is_known 兜底）

    Args:
        raw_value: 原始属性值，可能带后缀，如 "正常冰|推荐"、"大杯/新"。

    Returns:
        清洗后的 token 值（词典中存在时）或原始值（无法匹配时）。
        注意：返回值可能不在词典中，调用方应配合 lookup() 使用。

    Examples:
        >>> normalize_token("正常冰")
        "正常冰"                          # Step 1 精确命中
        >>> normalize_token("正常冰|推荐")
        "正常冰"                          # Step 2 子串边界匹配
        >>> normalize_token("大杯/新")
        "大杯"                            # Step 2 子串边界匹配
        >>> normalize_token("七分糖|推荐")
        "七分糖"                          # Step 2 子串边界匹配
        >>> normalize_token("红茶, 十二分糖")
        "红茶, 十二分糖"                  # Step 4 无法匹配，返回原值
    """
    if not raw_value or not isinstance(raw_value, str):
        return raw_value if raw_value is not None else ""

    val = raw_value.strip()
    if not val:
        return raw_value

    # ── Step 1: 精确匹配 ──
    if val in TOKEN_MAP:
        return val

    # ── Step 2: 子串匹配（分隔符边界检测） ──
    # 遍历所有词典词，找出所有满足边界条件的匹配，取最长者。
    # 边界条件：word 左侧为字符串头或分隔符，右侧为字符串尾或分隔符。
    best_match: Optional[str] = None
    for word in TOKEN_MAP:
        idx = val.find(word)
        if idx == -1:
            continue
        # 左侧边界：字符串开头 或 前一个字符是分隔符
        left_ok = (idx == 0) or (val[idx - 1] in _SEPARATORS)
        # 右侧边界：字符串结尾 或 后一个字符是分隔符
        right_ok = (idx + len(word) == len(val)) or (val[idx + len(word)] in _SEPARATORS)
        if left_ok and right_ok:
            if best_match is None or len(word) > len(best_match):
                best_match = word

    if best_match is not None:
        return best_match

    # ── Step 3: 分隔符切割，取第一个 token 精确匹配 ──
    for sep in ("|", "/"):
        parts = val.split(sep)
        first = parts[0].strip()
        if first in TOKEN_MAP:
            return first

    # 空格切割
    parts = val.split()
    if parts:
        first = parts[0].strip()
        if first in TOKEN_MAP:
            return first

    # ── Step 4: 全部失败，返回原值 ──
    return raw_value


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

    # ── 6. normalize_token: Step 1 精确匹配 ──
    print("6. normalize_token（Step 1: 精确匹配 → 返回原值）")
    check(normalize_token("正常冰") == "正常冰", "clean '正常冰' → '正常冰'")
    check(normalize_token("七分糖") == "七分糖", "clean '七分糖' → '七分糖'")
    check(normalize_token("大杯") == "大杯", "clean '大杯' → '大杯'")
    check(normalize_token("燕麦奶") == "燕麦奶", "clean '燕麦奶' → '燕麦奶'")
    print()

    # ── 7. normalize_token: Step 2 子串边界匹配 ──
    print("7. normalize_token（Step 2: 子串边界匹配，去后缀）")
    check(normalize_token("正常冰|推荐") == "正常冰", "'正常冰|推荐' → '正常冰'")
    check(normalize_token("七分糖|推荐") == "七分糖", "'七分糖|推荐' → '七分糖'")
    check(normalize_token("大杯/新") == "大杯", "'大杯/新' → '大杯'")
    check(normalize_token("标准糖|推荐") == "标准糖", "'标准糖|推荐' → '标准糖'")
    check(normalize_token("去冰|推荐") == "去冰", "'去冰|推荐' → '去冰'")
    # 空格边界
    check(normalize_token("少冰 推荐") == "少冰", "'少冰 推荐' → '少冰'")
    print()

    # ── 8. normalize_token: Step 2 防误匹配 ──
    print("8. normalize_token（Step 2: 防误匹配 — 禁止任意位置子串）")
    check(normalize_token("正常冰沙") == "正常冰沙", "'正常冰沙' ≠ '冰'（左边界不是分隔符）")
    check(normalize_token("冰沙") == "冰沙", "'冰沙' 本身在词典中 → 返回原值（Step 1）")
    # 多词同存取最长
    check(normalize_token("五黄标准茶|推荐") == "五黄标准茶",
          "'五黄标准茶|推荐' → '五黄标准茶'（3字，优先于'茶'1字）")
    print()

    # ── 9. normalize_token: Step 3 分隔符切割 ──
    print("9. normalize_token（Step 3: 分隔符切割后首 token 匹配）")
    check(normalize_token("全糖/新/尝鲜") == "全糖", "'全糖/新/尝鲜' → '全糖'")
    check(normalize_token("牛奶|推荐|热销") == "牛奶", "'牛奶|推荐|热销' → '牛奶'")
    # 空格切割
    check(normalize_token("大杯 热门") == "大杯", "'大杯 热门' → '大杯'")
    print()

    # ── 10. normalize_token: Step 4 无法匹配 ──
    print("10. normalize_token（Step 4: 无法匹配 → 返回原值）")
    check(normalize_token("珍珠奶茶") == "珍珠奶茶", "未知值 '珍珠奶茶' 原样返回")
    check(normalize_token("") == "", "空字符串 → ''")
    check(normalize_token("  全糖  ") == "全糖", "'  全糖  ' → '全糖'（去空白后 Step 1）")
    print()

    # ── 11. KNOWN_SUFFIXES 文档验证 ──
    print("11. KNOWN_SUFFIXES 文档")
    check(len(KNOWN_SUFFIXES) == 2, f"记录了 2 个已知后缀模式: {KNOWN_SUFFIXES}")
    check("|推荐" in KNOWN_SUFFIXES, "包含 '|推荐'")
    check("/新" in KNOWN_SUFFIXES, "包含 '/新'")
    print()

    # ── 汇总 ──
    total_types = len(TOKEN_BY_TYPE)
    total_tokens = len(TOKEN_MAP)
    print(f"=== 结果: {passed} passed, {failed} failed ===")
    print(f"=== Token 类型: {total_types} 种, Token 总数: {total_tokens} 个 ===")
