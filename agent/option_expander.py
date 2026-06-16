"""
Option Specification Template Expander — 选项规格模板展开器。

将包含选项规格定义的主数据表展开为选项模板的明细行。
每个主数据行按 5 个维度（糖度/温度/规格/奶底/茶底）展开，
每个维度的每个选项值生成一行模板数据。

纯规则引擎，不调用 LLM。
"""

import math
from typing import Any, List

import pandas as pd

# ── 常量 ──────────────────────────────────────────────────────────

# 展开维度（按此顺序遍历）
DIMENSIONS = ["糖度", "温度", "规格", "奶底", "茶底"]

# 模板输出列（固定顺序）
TEMPLATE_COLUMNS = [
    "商品编码", "商品名称", "口味做法组名", "选项名称",
    "最少必选", "最多可选", "推荐项", "默认项",
]

# 固定常量
MIN_REQUIRED = 1
MAX_OPTIONAL = 1
YES = "是"
NO = "否"


# ── 内部辅助 ──────────────────────────────────────────────────────

def _empty(val: Any) -> bool:
    """判断值是否为空（NaN / None / 空字符串 / 纯空白）。"""
    if val is None:
        return True
    if isinstance(val, float) and math.isnan(val):
        return True
    if isinstance(val, str) and val.strip() == "":
        return True
    return False


def _parse_dimension_list(raw_value: Any) -> List[str]:
    """将分隔的维度列表解析为去重、去空白的值列表。

    分隔符：中文分号「；」和 ASCII 分号「;」均支持。
    同时处理行尾多余的分号和逗号分隔。

    Args:
        raw_value: 单元格原始值（字符串 / NaN / None）。

    Returns:
        去重、去空白后的选项值列表（保持首次出现顺序）。
    """
    if _empty(raw_value):
        return []

    s = str(raw_value).strip()
    if not s:
        return []

    # 统一处理：先尝试中文分号，再尝试 ASCII 分号
    # 检测哪种分隔符占主导
    if "；" in s:
        parts = s.split("；")
    elif ";" in s:
        parts = s.split(";")
    elif "，" in s:
        parts = s.split("，")
    elif "," in s:
        parts = s.split(",")
    else:
        # 单值
        PLACEHOLDERS_SINGLE = {"-", "—", "无", "/", "\\"}
        cleaned = s.strip()
        if cleaned and cleaned not in PLACEHOLDERS_SINGLE:
            return [cleaned]
        return []

    # 去空白、去空串、去重、过滤占位符（保持首次出现顺序）
    PLACEHOLDERS = {"-", "—", "无", "/", "\\"}
    seen = set()
    result = []
    for p in parts:
        cleaned = p.strip()
        if cleaned and cleaned not in seen and cleaned not in PLACEHOLDERS:
            seen.add(cleaned)
            result.append(cleaned)

    return result


def _match_to_yes_no(value: str, target: Any) -> str:
    """判断 value 是否等于 target，返回"是"或"否"。

    target 为空（NaN/None/空串）时永远返回"否"。
    """
    if _empty(target):
        return NO
    target_str = str(target).strip()
    if not target_str:
        return NO
    return YES if value == target_str else NO


# ── 核心展开函数 ──────────────────────────────────────────────────

def expand_master_to_options(master_df: pd.DataFrame) -> pd.DataFrame:
    """将主数据行展开为选项规格模板行。

    对每一行主数据，遍历 5 个维度（糖度/温度/规格/奶底/茶底），
    将每个维度的选项列表（；分隔）拆分为独立的模板行。

    Args:
        master_df: 主数据 DataFrame，预期包含以下列：
            主编码, 商品名称,
            推荐糖度, 默认糖度, 糖度,
            推荐温度, 默认温度, 温度,
            推荐规格, 默认规格, 规格,
            推荐奶底, 默认奶底, 奶底,
            推荐茶底, 默认茶底, 茶底

    Returns:
        DataFrame with TEMPLATE_COLUMNS，每行一个选项值。

    Raises:
        ValueError: 缺少「主编码」或「商品名称」列。
    """
    # 验证必要列
    missing_fixed = []
    for col in ["主编码", "商品名称"]:
        if col not in master_df.columns:
            missing_fixed.append(col)
    if missing_fixed:
        raise ValueError(
            f"主数据表缺少必要列: {missing_fixed}\n"
            f"当前列名: {list(master_df.columns)}"
        )

    rows = []

    # 按主编码去重：同一产品可能有多行（如不同规格变体），
    # 保留第一行（通常含完整的推荐/默认值），跳过后续重复行。
    seen_codes = set()

    for _, mrow in master_df.iterrows():
        product_code = str(mrow["主编码"]).strip() if not _empty(mrow.get("主编码")) else ""
        if product_code in seen_codes:
            continue
        seen_codes.add(product_code)

        product_name = str(mrow["商品名称"]).strip() if not _empty(mrow.get("商品名称")) else ""

        if not product_code and not product_name:
            # 主编码和商品名称都为空 → 跳过
            print(f"[WARNING] 主数据行缺少主编码和商品名称，已跳过")
            continue

        for dim in DIMENSIONS:
            # 解析维度选项列表
            dim_values = _parse_dimension_list(mrow.get(dim))

            if not dim_values:
                # 维度列表为空 → 跳过
                continue

            # 获取推荐值和默认值
            recommended_val = mrow.get(f"推荐{dim}")
            default_val = mrow.get(f"默认{dim}")

            for value in dim_values:
                rows.append({
                    "商品编码": product_code,
                    "商品名称": product_name,
                    "口味做法组名": dim,
                    "选项名称": value,
                    "最少必选": MIN_REQUIRED,
                    "最多可选": MAX_OPTIONAL,
                    "推荐项": _match_to_yes_no(value, recommended_val),
                    "默认项": _match_to_yes_no(value, default_val),
                })

    return pd.DataFrame(rows, columns=TEMPLATE_COLUMNS)


# ── 自测 ──────────────────────────────────────────────────────────

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

    print("=== Option Expander 自测 ===\n")

    # ── 1. 基本展开（1行主数据，2个维度） ──
    print("1. 基本展开（1 行主数据，糖度 3 值 + 温度 3 值）")
    master_basic = pd.DataFrame([{
        "主编码": "A001",
        "商品名称": "茉莉绿茶",
        "推荐糖度": "七分糖", "默认糖度": "五分糖",
        "糖度": "七分糖；五分糖；三分糖",
        "推荐温度": "正常冰", "默认温度": "少冰",
        "温度": "正常冰；少冰；去冰",
        "推荐规格": "", "默认规格": "", "规格": "",
        "推荐奶底": "", "默认奶底": "", "奶底": "",
        "推荐茶底": "", "默认茶底": "", "茶底": "",
    }])
    result = expand_master_to_options(master_basic)
    check(len(result) == 6, f"生成 6 行（实际 {len(result)}）")
    check(result.iloc[0]["商品编码"] == "A001", "商品编码 = A001")
    check(result.iloc[0]["商品名称"] == "茉莉绿茶", "商品名称 = 茉莉绿茶")
    check(result.iloc[0]["口味做法组名"] == "糖度", "第 1 组 = 糖度")
    check(result.iloc[2]["口味做法组名"] == "糖度", "第 3 组 = 糖度")
    check(result.iloc[3]["口味做法组名"] == "温度", "第 4 组 = 温度")
    print()

    # ── 2. 中文分号分隔 ──
    print("2. 中文分号分隔")
    values = _parse_dimension_list("七分糖；五分糖；三分糖")
    check(values == ["七分糖", "五分糖", "三分糖"], f"3 个值（实际 {values}）")
    print()

    # ── 3. 空维度跳过 ──
    print("3. 空维度跳过")
    master_empty_dim = pd.DataFrame([{
        "主编码": "A001", "商品名称": "茉莉绿茶",
        "推荐糖度": "七分糖", "默认糖度": "五分糖",
        "糖度": "七分糖；五分糖",
        "推荐温度": "", "默认温度": "", "温度": "",
        "推荐规格": "", "默认规格": "", "规格": "",
        "推荐奶底": "", "默认奶底": "", "奶底": "",
        "推荐茶底": "", "默认茶底": "", "茶底": "",
    }])
    result3 = expand_master_to_options(master_empty_dim)
    check(len(result3) == 2, f"仅糖度 2 行（实际 {len(result3)}）")
    check(all(r["口味做法组名"] == "糖度" for _, r in result3.iterrows()), "全部是糖度行")
    print()

    # ── 4. 单值维度（无分号） ──
    print("4. 单值维度")
    master_single = pd.DataFrame([{
        "主编码": "A001", "商品名称": "茉莉绿茶",
        "推荐糖度": "七分糖", "默认糖度": "七分糖",
        "糖度": "七分糖",
        "推荐温度": "", "默认温度": "", "温度": "",
        "推荐规格": "", "默认规格": "", "规格": "",
        "推荐奶底": "", "默认奶底": "", "奶底": "",
        "推荐茶底": "", "默认茶底": "", "茶底": "",
    }])
    result4 = expand_master_to_options(master_single)
    check(len(result4) == 1, f"1 行（实际 {len(result4)}）")
    check(result4.iloc[0]["选项名称"] == "七分糖", "选项名称 = 七分糖")
    print()

    # ── 5. 推荐项/默认项匹配 ──
    print("5. 推荐项/默认项匹配")
    row_0 = result.iloc[0]   # 七分糖：推荐
    row_1 = result.iloc[1]   # 五分糖：默认
    row_2 = result.iloc[2]   # 三分糖：都不
    check(row_0["推荐项"] == "是" and row_0["默认项"] == "否",
          f"七分糖 → 推荐项=是, 默认项=否 (实际 {row_0['推荐项']}/{row_0['默认项']})")
    check(row_1["推荐项"] == "否" and row_1["默认项"] == "是",
          f"五分糖 → 推荐项=否, 默认项=是 (实际 {row_1['推荐项']}/{row_1['默认项']})")
    check(row_2["推荐项"] == "否" and row_2["默认项"] == "否",
          f"三分糖 → 推荐项=否, 默认项=否 (实际 {row_2['推荐项']}/{row_2['默认项']})")
    print()

    # ── 6. 全部 5 个维度 ──
    print("6. 全部 5 个维度（每个 2 值）")
    master_all5 = pd.DataFrame([{
        "主编码": "B001", "商品名称": "招牌奶茶",
        "推荐糖度": "全糖", "默认糖度": "七分糖", "糖度": "全糖；七分糖",
        "推荐温度": "正常冰", "默认温度": "少冰", "温度": "正常冰；少冰",
        "推荐规格": "中杯", "默认规格": "中杯", "规格": "中杯；大杯",
        "推荐奶底": "牛奶", "默认奶底": "燕麦奶", "奶底": "牛奶；燕麦奶",
        "推荐茶底": "红茶", "默认茶底": "绿茶", "茶底": "红茶；绿茶",
    }])
    result6 = expand_master_to_options(master_all5)
    check(len(result6) == 10, f"10 行（实际 {len(result6)}）")
    # 检查维度分布
    dim_counts = result6["口味做法组名"].value_counts().to_dict()
    check(dim_counts.get("糖度", 0) == 2, f"糖度 2 行（实际 {dim_counts.get('糖度', 0)}）")
    check(dim_counts.get("温度", 0) == 2, f"温度 2 行")
    check(dim_counts.get("规格", 0) == 2, f"规格 2 行")
    check(dim_counts.get("奶底", 0) == 2, f"奶底 2 行")
    check(dim_counts.get("茶底", 0) == 2, f"茶底 2 行")
    print()

    # ── 7. 多行主数据 ──
    print("7. 多行主数据（2 行 × 2 维度 × 2 值）")
    master_multi = pd.DataFrame([
        {"主编码": "A001", "商品名称": "茉莉绿茶",
         "推荐糖度": "七分糖", "默认糖度": "五分糖", "糖度": "七分糖；五分糖",
         "推荐温度": "正常冰", "默认温度": "少冰", "温度": "正常冰；少冰",
         "推荐规格": "", "默认规格": "", "规格": "",
         "推荐奶底": "", "默认奶底": "", "奶底": "",
         "推荐茶底": "", "默认茶底": "", "茶底": ""},
        {"主编码": "A002", "商品名称": "珍珠奶茶",
         "推荐糖度": "全糖", "默认糖度": "标准糖", "糖度": "全糖；标准糖",
         "推荐温度": "热", "默认温度": "热", "温度": "热；正常冰",
         "推荐规格": "", "默认规格": "", "规格": "",
         "推荐奶底": "", "默认奶底": "", "奶底": "",
         "推荐茶底": "", "默认茶底": "", "茶底": ""},
    ])
    result7 = expand_master_to_options(master_multi)
    check(len(result7) == 8, f"8 行（实际 {len(result7)}）")
    check(len(result7[result7["商品编码"] == "A001"]) == 4, "A001 有 4 行")
    check(len(result7[result7["商品编码"] == "A002"]) == 4, "A002 有 4 行")
    print()

    # ── 8. 空主数据 ──
    print("8. 空主数据")
    master_empty = pd.DataFrame(columns=[
        "主编码", "商品名称",
        "推荐糖度", "默认糖度", "糖度",
        "推荐温度", "默认温度", "温度",
        "推荐规格", "默认规格", "规格",
        "推荐奶底", "默认奶底", "奶底",
        "推荐茶底", "默认茶底", "茶底",
    ])
    result8 = expand_master_to_options(master_empty)
    check(len(result8) == 0, f"0 行（实际 {len(result8)}）")
    check(list(result8.columns) == TEMPLATE_COLUMNS, "列名正确")
    print()

    # ── 9. 缺列检测 ──
    print("9. 缺列检测")
    master_no_code = pd.DataFrame([{"商品名称": "测试"}])
    try:
        expand_master_to_options(master_no_code)
        check(False, "应抛出 ValueError")
    except ValueError as e:
        check("主编码" in str(e), f"报错含「主编码」（实际: {e}）")

    master_no_name = pd.DataFrame([{"主编码": "A001"}])
    try:
        expand_master_to_options(master_no_name)
        check(False, "应抛出 ValueError")
    except ValueError as e:
        check("商品名称" in str(e), f"报错含「商品名称」（实际: {e}）")
    print()

    # ── 10. 推荐/默认不在列表中 ──
    print("10. 推荐/默认不在选项列表中 → 全部「否」")
    master_no_match = pd.DataFrame([{
        "主编码": "A001", "商品名称": "测试",
        "推荐糖度": "全糖", "默认糖度": "无糖",
        "糖度": "七分糖；五分糖",
        "推荐温度": "", "默认温度": "", "温度": "",
        "推荐规格": "", "默认规格": "", "规格": "",
        "推荐奶底": "", "默认奶底": "", "奶底": "",
        "推荐茶底": "", "默认茶底": "", "茶底": "",
    }])
    result10 = expand_master_to_options(master_no_match)
    check(all(r["推荐项"] == "否" for _, r in result10.iterrows()),
          "所有行推荐项 = 否")
    check(all(r["默认项"] == "否" for _, r in result10.iterrows()),
          "所有行默认项 = 否")
    print()

    # ── 11. 推荐 == 默认（同一值） ──
    print("11. 推荐 == 默认 → 该行两列都填「是」")
    master_same = pd.DataFrame([{
        "主编码": "A001", "商品名称": "测试",
        "推荐糖度": "七分糖", "默认糖度": "七分糖",
        "糖度": "七分糖；五分糖",
        "推荐温度": "", "默认温度": "", "温度": "",
        "推荐规格": "", "默认规格": "", "规格": "",
        "推荐奶底": "", "默认奶底": "", "奶底": "",
        "推荐茶底": "", "默认茶底": "", "茶底": "",
    }])
    result11 = expand_master_to_options(master_same)
    row_7 = result11[result11["选项名称"] == "七分糖"].iloc[0]
    check(row_7["推荐项"] == "是" and row_7["默认项"] == "是",
          f"七分糖 → 推荐项=是, 默认项=是 (实际 {row_7['推荐项']}/{row_7['默认项']})")
    row_5 = result11[result11["选项名称"] == "五分糖"].iloc[0]
    check(row_5["推荐项"] == "否" and row_5["默认项"] == "否",
          f"五分糖 → 推荐项=否, 默认项=否")
    print()

    # ── 12. 重复值去重 ──
    print("12. 重复值去重")
    master_dup = pd.DataFrame([{
        "主编码": "A001", "商品名称": "测试",
        "推荐糖度": "七分糖", "默认糖度": "五分糖",
        "糖度": "七分糖；五分糖；七分糖",
        "推荐温度": "", "默认温度": "", "温度": "",
        "推荐规格": "", "默认规格": "", "规格": "",
        "推荐奶底": "", "默认奶底": "", "奶底": "",
        "推荐茶底": "", "默认茶底": "", "茶底": "",
    }])
    result12 = expand_master_to_options(master_dup)
    check(len(result12) == 2, f"去重后 2 行（实际 {len(result12)}）")
    vals = set(result12["选项名称"].tolist())
    check(vals == {"七分糖", "五分糖"}, f"值 = 七分糖/五分糖（实际 {vals}）")
    print()

    # ── 13. 空白值在列表中 ──
    print("13. 列表中间空白值跳过")
    master_blank_mid = pd.DataFrame([{
        "主编码": "A001", "商品名称": "测试",
        "推荐糖度": "七分糖", "默认糖度": "五分糖",
        "糖度": "七分糖； ；五分糖",
        "推荐温度": "", "默认温度": "", "温度": "",
        "推荐规格": "", "默认规格": "", "规格": "",
        "推荐奶底": "", "默认奶底": "", "奶底": "",
        "推荐茶底": "", "默认茶底": "", "茶底": "",
    }])
    result13 = expand_master_to_options(master_blank_mid)
    check(len(result13) == 2, f"跳过空白后 2 行（实际 {len(result13)}）")
    print()

    # ── 14. 推荐/默认值为 NaN ──
    print("14. 推荐/默认值为 NaN → 全部「否」")
    master_nan = pd.DataFrame([{
        "主编码": "A001", "商品名称": "测试",
        "推荐糖度": float("nan"), "默认糖度": None,
        "糖度": "七分糖；五分糖",
        "推荐温度": "", "默认温度": "", "温度": "",
        "推荐规格": "", "默认规格": "", "规格": "",
        "推荐奶底": "", "默认奶底": "", "奶底": "",
        "推荐茶底": "", "默认茶底": "", "茶底": "",
    }])
    result14 = expand_master_to_options(master_nan)
    check(all(r["推荐项"] == "否" for _, r in result14.iterrows()), "推荐项 全部 = 否")
    check(all(r["默认项"] == "否" for _, r in result14.iterrows()), "默认项 全部 = 否")
    print()

    # ── 15. 所有维度列表为空（跳过整行） ──
    print("15. 所有维度列表为空 → 0 行")
    master_all_empty = pd.DataFrame([{
        "主编码": "A001", "商品名称": "测试",
        "推荐糖度": "", "默认糖度": "", "糖度": "",
        "推荐温度": "", "默认温度": "", "温度": "",
        "推荐规格": "", "默认规格": "", "规格": "",
        "推荐奶底": "", "默认奶底": "", "奶底": "",
        "推荐茶底": "", "默认茶底": "", "茶底": "",
    }])
    result15 = expand_master_to_options(master_all_empty)
    check(len(result15) == 0, f"0 行（实际 {len(result15)}）")
    print()

    # ── 16. "是"/"否" 常量正确（中文） ──
    print("16. 「是」/「否」常量验证")
    check(YES == "是", f"YES = '是'（实际 {YES!r}）")
    check(NO == "否", f"NO = '否'（实际 {NO!r}）")
    print()

    # ── 17. 最少必选/最多可选始终为 1 ──
    print("17. 最少必选/最多可选始终为 1")
    check(MIN_REQUIRED == 1, f"最少必选 = 1（实际 {MIN_REQUIRED}）")
    check(MAX_OPTIONAL == 1, f"最多可选 = 1（实际 {MAX_OPTIONAL}）")
    for _, r in result.iterrows():
        check(r["最少必选"] == 1 and r["最多可选"] == 1,
              f"行「{r['选项名称']}」: 最少必选={r['最少必选']}, 最多可选={r['最多可选']}")
    print()

    # ── 18. 列顺序验证 ──
    print("18. 模板列顺序验证")
    check(list(result.columns) == TEMPLATE_COLUMNS,
          f"列顺序正确（实际 {list(result.columns)}）")
    print()

    # ── 19. ASCII 分号「;」和中文分号「；」均支持拆分 ──
    print("19. ASCII 分号和中文分号均支持拆分")
    vals_semicolon = _parse_dimension_list("七分糖;五分糖;三分糖")
    check(vals_semicolon == ["七分糖", "五分糖", "三分糖"],
          f"ASCII 分号被正确拆分（实际 {vals_semicolon}）")
    vals_cn = _parse_dimension_list("正常冰；少冰；去冰")
    check(vals_cn == ["正常冰", "少冰", "去冰"],
          f"中文分号被正确拆分（实际 {vals_cn}）")
    print()

    # ── 汇总 ──
    print(f"=== 结果: {passed} passed, {failed} failed ===")
