"""
Excel 读取模块 — 读取主数据表和模板表，返回 pandas DataFrame。
"""

import unicodedata
from pathlib import Path
from typing import Tuple

import pandas as pd


def _normalize_df(df: pd.DataFrame) -> pd.DataFrame:
    """对 DataFrame 中所有字符串列做 Unicode NFC 标准化。

    防止 Excel 中的特殊 Unicode 编码（如全角/半角混用、NFD 分解字符）
    导致后续匹配失败。对所有 object 类型列执行 unicodedata.normalize('NFC', ...)。
    """
    for col in df.columns:
        if df[col].dtype == object:
            df[col] = df[col].apply(
                lambda x: unicodedata.normalize("NFC", str(x)) if pd.notna(x) else x
            )
    return df

# 主数据表必要字段（SOP 为目标列，允许缺失）
MASTER_REQUIRED_COLUMNS = ["品名", "杯型", "奶底", "做法", "糖"]

# 主数据表可选字段（存在则保留）
MASTER_OPTIONAL_COLUMNS = ["全信息", "SOP"]


def read_excel(filepath: str, sheet_name=0) -> pd.DataFrame:
    """读取 Excel 文件，返回 DataFrame。

    Args:
        filepath: Excel 文件路径。
        sheet_name: 工作表名或索引，默认第一个 sheet。

    Returns:
        pd.DataFrame

    Raises:
        FileNotFoundError: 文件不存在。
        ValueError: 工作表名无效。
    """
    path = Path(filepath)
    if not path.exists():
        raise FileNotFoundError(f"文件不存在: {filepath}")

    with pd.ExcelFile(filepath) as xl:
        if isinstance(sheet_name, str) and sheet_name not in xl.sheet_names:
            raise ValueError(f"工作表 '{sheet_name}' 不存在，可用 sheet: {xl.sheet_names}")

    df = pd.read_excel(filepath, sheet_name=sheet_name)
    # 去除首尾空白列名（空 DataFrame 的列名可能为整数类型）
    if len(df.columns) > 0:
        df.columns = [str(c).strip() for c in df.columns]
    return _normalize_df(df)


def read_master(filepath: str, sheet_name=0) -> pd.DataFrame:
    """读取主数据表，校验必要字段。

    必要字段: 品名, 杯型, 奶底, 做法, 糖
    可选字段: 全信息, SOP

    Args:
        filepath: 主数据表 Excel 路径。
        sheet_name: 工作表名或索引。

    Returns:
        pd.DataFrame，列名已 strip。

    Raises:
        ValueError: 缺少必要字段。
    """
    df = read_excel(filepath, sheet_name=sheet_name)

    missing = [c for c in MASTER_REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(
            f"主数据表缺少必要字段: {missing}\n"
            f"当前列名: {list(df.columns)}"
        )

    return df


def read_template(filepath: str, sheet_name=0) -> pd.DataFrame:
    """读取模板表。

    模板表字段名因来源而异，不做固定校验，由 Schema Analyzer 负责识别。

    Args:
        filepath: 模板表 Excel 路径。
        sheet_name: 工作表名或索引。

    Returns:
        pd.DataFrame，列名已 strip。
    """
    df = read_excel(filepath, sheet_name=sheet_name)

    if df.empty:
        raise ValueError(f"模板表为空: {filepath}")

    return df


# ── 自测 ──────────────────────────────────────────────────────

if __name__ == "__main__":
    import tempfile
    import os

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

    print("=== Excel Reader 自测 ===\n")

    tmpdir = tempfile.mkdtemp()

    # ── 准备测试文件 ──
    master_path = os.path.join(tmpdir, "master_test.xlsx")
    template_path = os.path.join(tmpdir, "template_test.xlsx")
    empty_path = os.path.join(tmpdir, "empty.xlsx")
    multi_sheet_path = os.path.join(tmpdir, "multi_sheet.xlsx")

    pd.DataFrame({
        "品名": ["浅浅清茶", "浅浅清茶"],
        "杯型": ["中杯", "中杯"],
        "奶底": ["牛奶", "牛奶"],
        "做法": ["少冰", "去冰"],
        "糖":   ["七分糖", "标准糖"],
        "全信息": ["", ""],
        "SOP": ["T240", "T265"],
    }).to_excel(master_path, index=False)

    pd.DataFrame({
        "菜品名称": ["五黄高纤慢养瓶", "五黄高纤慢养瓶"],
        "规格":       ["五角瓶", "五角瓶"],
        "口味做法组合": ["红茶, 十二分糖, 温热", "红茶, 十二分糖, 正常冰"],
        "配料":       ["", ""],
    }).to_excel(template_path, index=False)

    pd.DataFrame().to_excel(empty_path, index=False)

    # 多 sheet 文件
    with pd.ExcelWriter(multi_sheet_path) as w:
        pd.DataFrame({"A": [1]}).to_excel(w, sheet_name="Sheet1", index=False)
        pd.DataFrame({"B": [2]}).to_excel(w, sheet_name="Data", index=False)

    try:
        # ── 1. read_excel 基础读取 ──
        print("1. read_excel 基础读取")
        df = read_excel(master_path)
        check(len(df) == 2, f"读取 2 行（实际 {len(df)}）")
        check("品名" in df.columns, "包含 '品名' 列")
        print()

        # ── 2. read_master 校验 ──
        print("2. read_master 必要字段校验")
        m = read_master(master_path)
        check("品名" in m.columns and "杯型" in m.columns, "必要字段完整读取")
        check(m.iloc[0]["品名"] == "浅浅清茶", "第一行品名正确")

        # 缺字段应抛异常
        bad_path = os.path.join(tmpdir, "bad_master.xlsx")
        pd.DataFrame({"品名": ["a"]}).to_excel(bad_path, index=False)
        try:
            read_master(bad_path)
            check(False, "缺少字段应抛 ValueError")
        except ValueError as e:
            check("缺少必要字段" in str(e), f"ValueError 正确抛出: {e}")
        print()

        # ── 3. read_template ──
        print("3. read_template")
        t = read_template(template_path)
        check(len(t) == 2, f"模板读取 2 行（实际 {len(t)}）")
        check("配料" in t.columns, "包含 '配料' 列")

        # 空模板应抛异常
        try:
            read_template(empty_path)
            check(False, "空模板应抛 ValueError")
        except ValueError:
            check(True, "空模板正确抛出 ValueError")
        print()

        # ── 4. 文件不存在 ──
        print("4. 文件不存在")
        try:
            read_excel("不存在的文件.xlsx")
            check(False, "文件不存在应抛异常")
        except FileNotFoundError:
            check(True, "FileNotFoundError 正确抛出")
        print()

        # ── 5. 指定 sheet 名 ──
        print("5. 指定 sheet 名")
        df_sheet = read_excel(multi_sheet_path, sheet_name="Data")
        check("B" in df_sheet.columns, "读取 'Data' sheet 成功")

        # 无效 sheet 名
        try:
            read_excel(multi_sheet_path, sheet_name="NoSuch")
            check(False, "无效 sheet 名应抛异常")
        except ValueError:
            check(True, "无效 sheet 名正确抛出 ValueError")
        print()

        # ── 6. 列名 strip ──
        print("6. 列名空白 strip")
        strip_path = os.path.join(tmpdir, "strip.xlsx")
        pd.DataFrame({" 品名 ": ["a"], " 杯型 ": ["b"]}).to_excel(strip_path, index=False)
        df_strip = read_excel(strip_path)
        check("品名" in df_strip.columns and " 品名 " not in df_strip.columns, "列名空白已去除")
        print()

    finally:
        for f in [master_path, template_path, empty_path, multi_sheet_path,
                  os.path.join(tmpdir, "bad_master.xlsx"),
                  os.path.join(tmpdir, "strip.xlsx")]:
            if os.path.exists(f):
                os.remove(f)
        os.rmdir(tmpdir)

    print(f"=== 结果: {passed} passed, {failed} failed ===")
