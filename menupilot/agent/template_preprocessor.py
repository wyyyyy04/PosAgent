"""
模板预处理层 — 识别模板类型并标准化行结构。

在 Excel 读取后、Schema Analyzer 之前执行。
- standard 类型：透明通过，不影响现有流程
- chowbus 类型：收集散列字段，输出标准行结构
"""

import re
from typing import Any, Dict, List, Optional

import pandas as pd


def contains_chinese(value: Any) -> bool:
    """判断值是否包含中文字符。

    Args:
        value: 任意值（None / 数字 / 字符串）。

    Returns:
        True 当值包含至少一个中文字符（Unicode 一-鿿）。
    """
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return False
    s = str(value).strip()
    if not s:
        return False
    return bool(re.search(r"[一-鿿]", s))


def _clean_value(value: Any) -> str:
    """清洗值：去除 |推荐 等后缀标记，返回纯文本。

    Args:
        value: 原始单元格值。

    Returns:
        清洗后的字符串。
    """
    if value is None:
        return ""
    s = str(value).strip()
    # 去除 |推荐、|必选 等后缀
    if "|" in s:
        s = s.split("|")[0].strip()
    return s


def detect_template_type(df: pd.DataFrame) -> str:
    """识别模板类型。

    检测条件：
      1. 模板第一行为英文字段名（至少 3 个纯英文列名）
      2. 第二行包含中文字段注释
      3. 存在 item_cn 列
      4. 存在至少一个 customization{N}_id 列（N>=1）

    Args:
        df: 模板 DataFrame（header=None 读取）。

    Returns:
        "chowbus" 或 "standard"。
    """
    if df.shape[0] < 3:
        return "standard"

    row0 = [str(df.iloc[0, c]) if pd.notna(df.iloc[0, c]) else "" for c in range(df.shape[1])]
    row1 = [str(df.iloc[1, c]) if pd.notna(df.iloc[1, c]) else "" for c in range(df.shape[1])]

    # 条件 1: 第一行至少有 3 个纯英文列名
    english_cols = [v for v in row0 if re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", v)]
    if len(english_cols) < 3:
        return "standard"

    # 条件 2: 第二行包含中文字符
    row1_str = "".join(row1)
    if not contains_chinese(row1_str):
        return "standard"

    # 条件 3: 存在 item_cn 列
    has_item_cn = any("item_cn" == v for v in row0)
    if not has_item_cn:
        return "standard"

    # 条件 4: 存在 customization{N}_id 列
    customization_cols = [v for v in row0 if re.match(r"^customization\d+_id$", v)]
    if len(customization_cols) < 1:
        return "standard"

    return "chowbus"


def collect_chowbus_rows(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """收集 chowbus 模板的散列字段，输出标准行结构。

    对每一行：
      1. 取 item_cn 列值作为 product_name（空则跳过）
      2. 找到 item_cn 的列索引
      3. 向右扫描所有后续列，收集中文值
      4. 拼接为 composite_info

    Args:
        df: chowbus 模板 DataFrame（header=None 读取）。

    Returns:
        [{"product_name": "...", "composite_info": "红茶 燕麦奶 正常冰 标准糖"}, ...]
    """
    # 定位 item_cn 列
    row0 = [str(df.iloc[0, c]) if pd.notna(df.iloc[0, c]) else "" for c in range(df.shape[1])]
    item_cn_col = None
    for c, v in enumerate(row0):
        if v == "item_cn":
            item_cn_col = c
            break

    if item_cn_col is None:
        raise ValueError("chowbus 模板缺少 item_cn 列")

    rows = []
    for row_idx in range(2, df.shape[0]):  # 从第 3 行开始（跳过英文表头 + 中文注释）
        product_name = df.iloc[row_idx, item_cn_col]
        if pd.isna(product_name) or str(product_name).strip() == "":
            continue

        product_name = str(product_name).strip()

        # 向右扫描收集中文值
        chinese_values = []
        for c in range(item_cn_col + 1, df.shape[1]):
            val = df.iloc[row_idx, c]
            if pd.isna(val):
                continue
            cleaned = _clean_value(val)
            if contains_chinese(cleaned):
                chinese_values.append(cleaned)

        composite_info = ", ".join(chinese_values)
        rows.append({
            "product_name": product_name,
            "composite_info": composite_info,
        })

    return rows


# ── 自测 ────────────────────────────────────────────────────────

if __name__ == "__main__":
    import os
    import tempfile

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

    print("=== Template Preprocessor 自测 ===\n")

    # ── 1. contains_chinese ──
    print("1. contains_chinese()")
    check(contains_chinese("红茶"), "红茶 → True")
    check(contains_chinese("正常冰"), "正常冰 → True")
    check(not contains_chinese("abc123"), "abc123 → False")
    check(not contains_chinese(None), "None → False")
    check(not contains_chinese(123), "数字 123 → False")
    check(not contains_chinese(""), "空字符串 → False")
    check(not contains_chinese("cpid_250164"), "cpid_250164 → False")
    check(contains_chinese("五角瓶|推荐"), "五角瓶|推荐 → True")
    print()

    # ── 2. _clean_value ──
    print("2. _clean_value()")
    check(_clean_value("五角瓶|推荐") == "五角瓶", "去除|推荐后缀")
    check(_clean_value("正常冰") == "正常冰", "正常值不变")
    check(_clean_value(None) == "", "None → 空字符串")
    print()

    # ── 3. detect_template_type ──
    print("3. detect_template_type()")
    # 标准模板（单行表头）
    df_standard = pd.DataFrame({
        "菜品名称": ["浅浅清茶"],
        "规格": ["中杯"],
        "口味做法组合": ["牛奶, 少冰, 七分糖"],
        "配料": [""],
    })
    check(detect_template_type(df_standard) == "standard", "标准模板 → standard")

    # chowbus 模板（两行表头）
    df_chowbus = pd.DataFrame([
        ["terminal_en", "terminal_cn", "item_cn", "customization1_id", "customization1_option_id"],
        ["// 英文", "// 中文", "// 商品", "// 定制ID", "// 定制选项"],
        ["POS", "POS", "五黄高纤慢养瓶", "cpid_250164", None],
    ])
    check(detect_template_type(df_chowbus) == "chowbus", "chowbus 模板 → chowbus")

    # 缺少 customization 列
    df_no_cust = pd.DataFrame([
        ["terminal_en", "terminal_cn", "item_cn"],
        ["// 英文", "// 中文", "// 商品"],
        ["POS", "POS", "测试商品"],
    ])
    check(detect_template_type(df_no_cust) == "standard", "缺少 customization → standard")

    # 缺少 item_cn
    df_no_item = pd.DataFrame([
        ["terminal_en", "terminal_cn", "customization1_id"],
        ["// 英文", "// 中文", "// 定制"],
        ["POS", "POS", "cpid_123"],
    ])
    check(detect_template_type(df_no_item) == "standard", "缺少 item_cn → standard")
    print()

    # ── 4. collect_chowbus_rows ──
    print("4. collect_chowbus_rows()")
    df_data = pd.DataFrame([
        ["terminal_en", "item_cn", "customization1_id", "customization1_option_id",
         "customization2_id", "customization2_option_id", "other_en"],
        ["// 英文", "// 商品名", "// 尺寸ID", "// 尺寸选项",
         "// 温度ID", "// 温度选项", "// 其他"],
        ["POS", "五黄高纤慢养瓶", "id_123", None, "id_456", "正常冰|推荐", "abc"],
        ["POS", "五黄高纤慢养瓶", "id_123", "五角瓶", "id_456", "少冰", "def"],
        ["POS", None, "id_789", "中杯", "id_012", "去冰", "ghi"],  # item_cn 为空 → 跳过
        ["POS", "珍珠奶茶", "id_789", "大杯", "id_012", "热", "ghi"],
    ])

    rows = collect_chowbus_rows(df_data)
    check(len(rows) == 3, f"收集 3 行（实际 {len(rows)}）")
    check(rows[0]["product_name"] == "五黄高纤慢养瓶", f"第1行 product_name 正确")
    check("正常冰" in rows[0]["composite_info"], f"第1行 composite_info 含 正常冰")
    check("五角瓶" in rows[1]["composite_info"], f"第2行 composite_info 含 五角瓶")
    check(rows[2]["product_name"] == "珍珠奶茶", f"第3行 product_name 正确")
    print()

    # ── 5. item_cn 之前的中文值不被收集 ──
    print("5. item_cn 之前的中文值不被收集")
    df_before = pd.DataFrame([
        ["menu_cn", "item_cn", "customization1_option_id"],
        ["// 菜单名", "// 商品", "// 尺寸"],
        ["超级菜单", "测试商品", "大杯"],
    ])
    rows_before = collect_chowbus_rows(df_before)
    check(len(rows_before) == 1, "1 行")
    check("超级菜单" not in rows_before[0]["composite_info"],
          "item_cn 之前的 '超级菜单' 未被收集")
    check("大杯" in rows_before[0]["composite_info"],
          "item_cn 之后的 '大杯' 被收集")
    print()

    # ── 6. 纯数字/纯英文 → 跳过 ──
    print("6. 纯数字/纯英文值被跳过")
    df_skip = pd.DataFrame([
        ["item_cn", "custom1_opt", "custom2_opt", "custom3_opt", "custom4_opt", "custom5_opt"],
        ["// 商品", "// 尺寸", "// 温度", "// 糖度", "// 奶底", "// 选项ID"],
        ["测试商品", "大杯", "cpid_250164", "正常冰", "12345", "燕麦奶"],
    ])
    rows_skip = collect_chowbus_rows(df_skip)
    check(len(rows_skip) == 1, "1 行")
    info = rows_skip[0]["composite_info"]
    check("大杯" in info, "中文 '大杯' 被收集")
    check("正常冰" in info, "中文 '正常冰' 被收集")
    check("燕麦奶" in info, "中文 '燕麦奶' 被收集")
    check("cpid_250164" not in info, "纯英文 'cpid_250164' 被跳过")
    check("12345" not in info, "纯数字 '12345' 被跳过")
    check("测试商品" not in info, "item_cn 列本身不被收集")
    print()

    # ── 7. 空行跳过 ──
    print("7. item_cn 为空的行被跳过")
    df_empty = pd.DataFrame([
        ["item_cn", "custom1_opt"],
        ["// 商品", "// 尺寸"],
        [None, "大杯"],
        ["", "中杯"],
        ["有效商品", "小杯"],
    ])
    rows_empty = collect_chowbus_rows(df_empty)
    check(len(rows_empty) == 1, f"仅 1 行有效（实际 {len(rows_empty)}）")
    check(rows_empty[0]["product_name"] == "有效商品", "有效商品行被收集")
    print()

    # ── 汇总 ──
    print(f"=== 结果: {passed} passed, {failed} failed ===")
