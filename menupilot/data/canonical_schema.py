"""
Canonical Schema — 所有模板字段统一转换为此内部标准结构。

位于 Rule Engine 层的核心数据模型，被 Matching Engine / workflow 引用。
"""

from typing import Dict, List

# ── Canonical 字段列表 ────────────────────────────────────────
# 所有字段（含 tea_base 扩展字段 + composite_col/sop 特殊字段）
CANONICAL_FIELDS = [
    "product_name", "size", "milk_base", "temperature", "sugar", "tea_base",
    "composite_col", "sop",
]

# 必要维度：匹配时这些字段必须存在
REQUIRED_DIMENSIONS = ["size", "temperature", "sugar"]

# 可通配维度：主数据中为空时匹配任意值
WILDCARD_DIMENSIONS = ["milk_base", "tea_base"]

# ── 主数据表固定列映射 ────────────────────────────────────────
# 主数据表字段名固定为中文，不需要 LLM 识别
MASTER_COLUMN_MAP: Dict[str, str] = {
    "品名": "product_name",
    "杯型": "size",
    "奶底": "milk_base",
    "做法": "temperature",
    "糖":   "sugar",
}

# ── Token 中文类型名 → Canonical 字段名 ────────────────────────
TOKEN_TYPE_TO_FIELD: Dict[str, str] = {
    "温度": "temperature",
    "糖度": "sugar",
    "奶底": "milk_base",
    "规格": "size",
    "茶底": "tea_base",
}

# 反向映射：Canonical 字段名 → Token 中文类型名
FIELD_TO_TOKEN_TYPE: Dict[str, str] = {v: k for k, v in TOKEN_TYPE_TO_FIELD.items()}


def create_canonical_row(**kwargs) -> Dict:
    """创建一条 Canonical Schema 行，缺失字段默认为 None。"""
    row = {f: None for f in CANONICAL_FIELDS}
    row.update(kwargs)
    return row


def get_missing_required(row: Dict) -> List[str]:
    """返回行中缺失的必要维度列表。"""
    return [f for f in REQUIRED_DIMENSIONS if row.get(f) is None]


def is_wildcard(field: str) -> bool:
    """检查字段是否允许通配（主数据为空时可匹配任意值）。"""
    return field in WILDCARD_DIMENSIONS


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

    print("=== Canonical Schema 自测 ===\n")

    # 1. 字段数量
    print("1. 字段定义")
    check(len(CANONICAL_FIELDS) == 8, f"CANONICAL_FIELDS 包含 8 个字段（实际 {len(CANONICAL_FIELDS)}）")
    check("tea_base" in CANONICAL_FIELDS, "包含 tea_base 扩展字段")
    check("composite_col" in CANONICAL_FIELDS, "包含 composite_col 特殊字段")
    check("sop" in CANONICAL_FIELDS, "包含 sop 特殊字段")
    check(len(REQUIRED_DIMENSIONS) == 3, "3 个必要维度")
    check(len(WILDCARD_DIMENSIONS) == 2, "2 个通配维度")
    print()

    # 2. 主数据列映射
    print("2. 主数据列映射")
    check(MASTER_COLUMN_MAP["品名"] == "product_name", "品名 → product_name")
    check(MASTER_COLUMN_MAP["做法"] == "temperature", "做法 → temperature")
    check(MASTER_COLUMN_MAP["糖"] == "sugar", "糖 → sugar")
    check(len(MASTER_COLUMN_MAP) == 5, "共 5 个映射")
    print()

    # 3. Token 类型映射
    print("3. Token 类型映射")
    check(TOKEN_TYPE_TO_FIELD["温度"] == "temperature", "温度 → temperature")
    check(TOKEN_TYPE_TO_FIELD["茶底"] == "tea_base", "茶底 → tea_base")
    check(FIELD_TO_TOKEN_TYPE["temperature"] == "温度", "反向: temperature → 温度")
    print()

    # 4. create_canonical_row
    print("4. create_canonical_row")
    row = create_canonical_row(product_name="测试", size="中杯")
    check(row["product_name"] == "测试", "自定义字段生效")
    check(row["size"] == "中杯", "自定义字段生效")
    check(row["milk_base"] is None, "未指定字段默认 None")
    check(len(row) == 8, f"始终包含 8 个字段（实际 {len(row)}）")
    print()

    # 5. get_missing_required
    print("5. get_missing_required")
    complete = {"size": "中杯", "temperature": "少冰", "sugar": "七分糖"}
    check(get_missing_required(complete) == [], "完整行 → 空列表")

    partial = {"size": "大杯", "temperature": None, "sugar": "全糖"}
    missing = get_missing_required(partial)
    check("temperature" in missing and len(missing) == 1, "缺 1 个维度被检测到")

    empty = {"size": None, "temperature": None, "sugar": None}
    check(len(get_missing_required(empty)) == 3, "缺 3 个维度全部检测到")
    print()

    # 6. is_wildcard
    print("6. is_wildcard")
    check(is_wildcard("milk_base") is True, "milk_base 是通配维度")
    check(is_wildcard("tea_base") is True, "tea_base 是通配维度")
    check(is_wildcard("size") is False, "size 不是通配维度")
    check(is_wildcard("temperature") is False, "temperature 不是通配维度")
    print()

    # ── 汇总 ──
    print(f"=== 结果: {passed} passed, {failed} failed ===")
