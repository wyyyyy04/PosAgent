"""
Rule Engine — 字段标准化、Token 验证、奶底通配逻辑、Canonical Schema 转换。
纯规则逻辑，不调用 LLM。位于 Schema Analyzer / Token Classifier 之后，Matching Engine 之前。
"""

import math
from typing import Any, Dict, List, Optional

import pandas as pd

from data.token_dict import lookup, is_known, UNKNOWN_TOKEN

# ── Canonical Schema 字段 ─────────────────────────────────────

CANONICAL_FIELDS = ["product_name", "size", "milk_base", "temperature", "sugar", "tea_base"]

# 主数据表中文列名 → Canonical（主数据表字段名固定，不需要 LLM 识别）
MASTER_COLUMN_MAP = {
    "品名": "product_name",
    "杯型": "size",
    "奶底": "milk_base",
    "做法": "temperature",
    "糖":   "sugar",
}

# 可通配的维度：主数据中为空的维度可匹配任意值
WILDCARD_DIMENSIONS = {"milk_base", "tea_base"}

# Token 中文类型名 → Canonical 字段名
TOKEN_TYPE_TO_FIELD = {
    "温度": "temperature",
    "糖度": "sugar",
    "奶底": "milk_base",
    "规格": "size",
    "茶底": "tea_base",
}


def _empty(val) -> bool:
    """判断值是否为空（NaN / None / 空字符串 / 纯空白）。"""
    if val is None:
        return True
    if isinstance(val, float) and math.isnan(val):
        return True
    if isinstance(val, str) and val.strip() == "":
        return True
    return False


# ── 主数据标准化 ──────────────────────────────────────────────

def master_to_canonical(master_df: pd.DataFrame) -> List[Dict[str, Any]]:
    """将主数据表转换为 Canonical Schema 行列表。

    主数据表字段名固定为中文（品名/杯型/奶底/做法/糖），
    映射为 product_name / size / milk_base / temperature / sugar。

    Args:
        master_df: 主数据 DataFrame，须含 品名/杯型/奶底/做法/糖 列。

    Returns:
        canonical_rows: 每行一个 dict，包含 canonical 字段 + sop（若有）。
    """
    rows = []
    for _, row in master_df.iterrows():
        cr = {f: None for f in CANONICAL_FIELDS}
        for cn_col, en_col in MASTER_COLUMN_MAP.items():
            val = row.get(cn_col)
            if _empty(val):
                # 奶底和茶底允许空值（通配），其余维度保留 None 以便后续检测
                cr[en_col] = None if en_col in WILDCARD_DIMENSIONS else val
            else:
                cr[en_col] = str(val).strip()

        # 保留 SOP 字段（目标值，匹配时直接引用）
        # 主数据表中 SOP 列可能命名为 "SOP"、"配料" 或 "SOP 代码"
        sop_col = None
        for candidate in ["SOP", "配料", "SOP 代码", "代码"]:
            if candidate in master_df.columns:
                sop_col = candidate
                break
        if sop_col and not _empty(row.get(sop_col)):
            cr["sop"] = str(row[sop_col]).strip()
        else:
            cr["sop"] = None

        rows.append(cr)
    return rows


# ── 模板标准化 ────────────────────────────────────────────────

def template_to_canonical(
    template_df: pd.DataFrame,
    field_mapping: Dict[str, str],
    composite_col: str,
    token_results: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """将模板表转换为 Canonical Schema 行列表。

    分为两步：
    1. 直接列映射：根据 field_mapping 将模板各列映射到 canonical 字段。
    2. 组合字段注入：将 Token Classifier 的分类结果注入 canonical 行，
       覆盖/补充对应的 canonical 字段。

    Args:
        template_df: 模板 DataFrame。
        field_mapping: 模板列名 → canonical 字段名 的映射。
        composite_col: 组合字段列名（如 "口味做法组合"）。
        token_results: Token Classifier 输出的逐行分类结果列表，每行为:
            {"tokens": [{"value": "红茶", "type": "茶底"}, ...], "missing": ["奶底"]}

    Returns:
        canonical_rows: 每行一个 dict，包含 canonical 字段。
    """
    rows = []
    for i, (_, trow) in enumerate(template_df.iterrows()):
        cr = {f: None for f in CANONICAL_FIELDS}

        # Step 1: 直接列映射
        for tcol, cfield in field_mapping.items():
            if tcol in template_df.columns:
                val = trow[tcol]
                if not _empty(val):
                    cr[cfield] = str(val).strip()

        # Step 2: 组合字段注入
        if i < len(token_results):
            tr = token_results[i]
            for tok in tr.get("tokens", []):
                token_val = tok.get("value", "")
                token_type = tok.get("type", "")
                cfield = TOKEN_TYPE_TO_FIELD.get(token_type)
                if cfield and cfield in CANONICAL_FIELDS:
                    cr[cfield] = token_val.strip()

            # 记录缺失维度
            cr["_missing_dimensions"] = tr.get("missing", [])

        rows.append(cr)
    return rows


# ── Token 验证 ────────────────────────────────────────────────

def validate_tokens(token_results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """验证 Token Classifier 的分类结果，标注未知 token。

    对每个 token 调用 token_dict.lookup，词典中不存在的 token
    在其结果中追加 `verified_type: "UNKNOWN_TOKEN"` 标记。

    Args:
        token_results: Token Classifier 原始输出。

    Returns:
        验证后的 token_results，每个 token 新增 verified_type 和 is_known 字段。
    """
    validated = []
    for tr in token_results:
        new_tokens = []
        for tok in tr.get("tokens", []):
            val = tok.get("value", "")
            llm_type = tok.get("type", "")
            verified = lookup(val)
            new_tokens.append({
                **tok,
                "verified_type": verified,
                "is_known": verified != UNKNOWN_TOKEN,
                # 如果 LLM 分类和词典不一致，以词典为准
                "type_conflict": (verified != UNKNOWN_TOKEN and verified != llm_type),
            })
        validated.append({
            **tr,
            "tokens": new_tokens,
        })
    return validated


def check_row_completeness(canonical_row: Dict[str, Any]) -> List[str]:
    """检查 canonical 行的必要维度是否缺失。

    规格/温度/糖度 必须存在，缺失时返回维度名列表。

    Args:
        canonical_row: 单行 canonical dict。

    Returns:
        缺失的必要维度列表。
    """
    required = ["size", "temperature", "sugar"]
    return [f for f in required if canonical_row.get(f) is None]


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

    print("=== Rule Engine 自测 ===\n")

    # ── 1. master_to_canonical: 正常行 ──
    print("1. master_to_canonical（正常行）")
    master_df = pd.DataFrame([
        {"品名": "浅浅清茶", "杯型": "中杯", "奶底": "牛奶", "做法": "少冰", "糖": "七分糖", "SOP": "T240、B30/80、S4"},
        {"品名": "浅浅清茶", "杯型": "中杯", "奶底": "牛奶", "做法": "去冰", "糖": "标准糖", "SOP": "T265、B30/105、S5"},
    ])
    m_rows = master_to_canonical(master_df)
    check(len(m_rows) == 2, f"2 行 → {len(m_rows)} 行")
    check(m_rows[0]["product_name"] == "浅浅清茶", "品名 → product_name")
    check(m_rows[0]["size"] == "中杯", "杯型 → size")
    check(m_rows[0]["milk_base"] == "牛奶", "奶底 → milk_base")
    check(m_rows[0]["temperature"] == "少冰", "做法 → temperature")
    check(m_rows[0]["sugar"] == "七分糖", "糖 → sugar")
    check(m_rows[0]["tea_base"] is None, "茶底 为 None（主数据无此字段）")
    check(m_rows[0]["sop"] == "T240、B30/80、S4", "SOP 保留")
    print()

    # ── 2. master_to_canonical: 奶底为空（通配） ──
    print("2. master_to_canonical（奶底为空 → 通配符）")
    master_empty_milk = pd.DataFrame([
        {"品名": "黑糖波波", "杯型": "大杯", "奶底": "", "做法": "正常冰", "糖": "全糖"},
    ])
    m2 = master_to_canonical(master_empty_milk)
    check(m2[0]["milk_base"] is None, "奶底空字符串 → None（通配）")
    check(m2[0]["product_name"] == "黑糖波波", "品名正常")
    print()

    # ── 3. master_to_canonical: NaN 处理 ──
    print("3. master_to_canonical（NaN 处理）")
    master_nan = pd.DataFrame([
        {"品名": "测试", "杯型": "大杯", "奶底": float("nan"), "做法": "热", "糖": "无糖"},
    ])
    m3 = master_to_canonical(master_nan)
    check(m3[0]["milk_base"] is None, "奶底 NaN → None（通配）")
    check(m3[0]["temperature"] == "热", "做法正常")
    print()

    # ── 4. template_to_canonical ──
    print("4. template_to_canonical（字段映射 + Token 注入）")
    template_df = pd.DataFrame([
        {"菜品名称": "五黄高纤慢养瓶", "规格": "五角瓶", "口味做法组合": "红茶, 十二分糖, 温热", "配料": ""},
        {"菜品名称": "五黄高纤慢养瓶", "规格": "五角瓶", "口味做法组合": "燕麦奶, 正常冰, 七分糖", "配料": ""},
    ])
    field_mapping = {
        "菜品名称": "product_name",
        "规格": "size",
    }
    token_results = [
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
    ]
    t_rows = template_to_canonical(template_df, field_mapping, "口味做法组合", token_results)
    check(len(t_rows) == 2, f"2 行 → {len(t_rows)} 行")

    # 第 1 行：直接映射 + Token 注入
    check(t_rows[0]["product_name"] == "五黄高纤慢养瓶", "product_name 来自直接映射")
    check(t_rows[0]["size"] == "五角瓶", "size 来自直接映射")
    check(t_rows[0]["tea_base"] == "红茶", "tea_base 来自 Token 注入")
    check(t_rows[0]["sugar"] == "十二分糖", "sugar 来自 Token 注入")
    check(t_rows[0]["temperature"] == "温热", "temperature 来自 Token 注入")
    check(t_rows[0]["milk_base"] is None, "milk_base 缺失 → None")
    check("奶底" in t_rows[0]["_missing_dimensions"], "missing 记录: 奶底")

    # 第 2 行：缺茶底
    check(t_rows[1]["product_name"] == "五黄高纤慢养瓶", "第2行 product_name 正确")
    check(t_rows[1]["milk_base"] == "燕麦奶", "第2行 milk_base 来自 Token")
    check(t_rows[1]["tea_base"] is None, "第2行 tea_base 缺失 → None")
    check("茶底" in t_rows[1]["_missing_dimensions"], "missing 记录: 茶底")
    print()

    # ── 5. validate_tokens ──
    print("5. validate_tokens（Token 验证）")
    sample_results = [
        {"tokens": [
            {"value": "红茶", "type": "茶底"},
            {"value": "珍珠", "type": "配料"},   # 词典外
            {"value": "温热", "type": "温度"},
        ]},
    ]
    v = validate_tokens(sample_results)
    tokens_v = v[0]["tokens"]
    check(len(tokens_v) == 3, "3 个 token 全部保留")
    check(tokens_v[0]["is_known"] is True, "'红茶' is_known=True")
    check(tokens_v[0]["verified_type"] == "茶底", "'红茶' verified_type=茶底")
    check(tokens_v[1]["is_known"] is False, "'珍珠' is_known=False")
    check(tokens_v[1]["verified_type"] == "UNKNOWN_TOKEN", "'珍珠' verified_type=UNKNOWN_TOKEN")
    check(tokens_v[2]["is_known"] is True, "'温热' is_known=True")

    # type_conflict 检测
    conflict_result = [
        {"tokens": [{"value": "牛奶", "type": "糖度"}]},  # LLM 说牛奶是糖度，词典说奶底
    ]
    vc = validate_tokens(conflict_result)
    check(vc[0]["tokens"][0]["type_conflict"] is True, "牛奶被 LLM 标为糖度 → type_conflict=True")
    print()

    # ── 6. check_row_completeness ──
    print("6. check_row_completeness（必要维度检查）")
    complete_row = {"size": "中杯", "temperature": "少冰", "sugar": "七分糖"}
    check(check_row_completeness(complete_row) == [], "完整行 → 空列表")

    missing_temp = {"size": "大杯", "temperature": None, "sugar": "全糖"}
    missing_list = check_row_completeness(missing_temp)
    check("temperature" in missing_list, "缺 temperature 被检测到")

    missing_all = {"size": None, "temperature": None, "sugar": None}
    ma = check_row_completeness(missing_all)
    check(len(ma) == 3, "缺全部 3 项被检测到")
    print()

    # ── 汇总 ──
    print(f"=== 结果: {passed} passed, {failed} failed ===")
