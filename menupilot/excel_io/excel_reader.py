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

# 主数据表必要字段（缺一不可，SOP 为目标列允许缺失）
MASTER_REQUIRED_COLUMNS = ["品名", "杯型", "做法", "糖"]

# 主数据表可通配字段（缺失时自动注入空列，触发通配逻辑，不报错）
MASTER_WILDCARD_COLUMNS = ["奶底"]

# 主数据表可选字段（存在则保留）
MASTER_OPTIONAL_COLUMNS = ["全信息", "SOP"]

# ── 选项规格主数据格式 ────────────────────────────────────────────

# 选项规格主数据固定列（必须存在）
OPTION_MASTER_FIXED_COLUMNS = ["主编码", "商品名称"]

# 选项规格主数据维度（每个维度对应 3 列：推荐{dim}, 默认{dim}, {dim}）
OPTION_MASTER_DIMENSIONS = ["糖度", "温度", "规格", "奶底", "茶底"]

# 列名别名：实际数据中常见的异名列 → 标准列名
OPTION_MASTER_COLUMN_ALIASES = {
    "产品名称（中文）": "商品名称",
    "产品名称(中文)": "商品名称",
    "产品名称": "商品名称",
    "推荐糖": "推荐糖度",
    "默认糖": "默认糖度",
    "推荐甜度": "推荐糖度",
    "默认甜度": "默认糖度",
}


# canonical field → 主数据必要字段名 的逆向映射（供列别名匹配使用）
CANONICAL_TO_MASTER_REQUIRED = {
    "product_name": "品名",
    "size": "杯型",
    "temperature": "做法",
    "sugar": "糖",
}


def _apply_column_aliases(df: pd.DataFrame) -> pd.DataFrame:
    """根据 column_aliases 记忆自动重命名 DataFrame 的列。

    流程：对每一列查询 get_column_alias(col_name)：
      - 返回 canonical field 如 "temperature"
      - 再查 CANONICAL_TO_MASTER_REQUIRED["temperature"] → "做法"
      - 若 "做法" 尚不在 df.columns 中 → 将列重命名为 "做法"

    目标：让「温度」这样的异构列名自动对齐到主数据校验所需的「做法」。
    已在 memory 中标记为 "ignore" 的列不会重命名。

    Returns:
        重命名后的 DataFrame（原位修改 + 返回）。
    """
    from menupilot.data.memory import get_column_alias

    for col in list(df.columns):
        alias = get_column_alias(col)
        if alias is None or alias == "ignore":
            continue
        required_name = CANONICAL_TO_MASTER_REQUIRED.get(alias)
        if required_name and required_name not in df.columns:
            df.rename(columns={col: required_name}, inplace=True)

    return df


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


def read_master(filepath: str, sheet_name=0, soft_validation: bool = False) -> pd.DataFrame:
    """读取主数据表，校验必要字段。

    必要字段: 品名, 杯型, 做法, 糖（缺一报错）
    可通配字段: 奶底（缺失时自动注入空列，触发通配逻辑）
    可选字段: 全信息, SOP

    soft_validation=True 时：
      - 先应用 column_aliases 自动重命名
      - 缺列不抛异常，而是标记在 df.attrs['_missing_required'] 上
      这是为了允许上层（main.py）在送入管线前做 LLM 推断 + 交互兜底。

    Args:
        filepath: 主数据表 Excel 路径。
        sheet_name: 工作表名或索引。
        soft_validation: True 时不抛异常，标记缺失列。

    Returns:
        pd.DataFrame，列名已 strip。

    Raises:
        ValueError: soft_validation=False 且缺少必要字段。
    """
    df = read_excel(filepath, sheet_name=sheet_name)

    # ── Step 1: 应用 column_aliases 记忆自动重命名 ──
    _apply_column_aliases(df)

    # ── Step 2: 检测必要字段 ──
    missing_required = [c for c in MASTER_REQUIRED_COLUMNS if c not in df.columns]

    if missing_required:
        if soft_validation:
            df.attrs["_missing_required"] = missing_required
        else:
            raise ValueError(
                f"主数据表缺少必要字段: {missing_required}\n"
                f"当前列名: {list(df.columns)}"
            )

    # ── 检测可通配字段：缺失时注入空列 ──
    for col in MASTER_WILDCARD_COLUMNS:
        if col not in df.columns:
            print(f"[INFO] 主数据表未检测到「{col}」列，该维度将作为通配符处理")
            df[col] = None

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


def read_template_raw(filepath: str, sheet_name=0) -> "pd.DataFrame":
    """以 header=None 读取模板表，保留原始行数据。

    用于模板类型检测（chowbus vs standard）和散列字段收集。

    Args:
        filepath: 模板表 Excel 路径。
        sheet_name: 工作表名或索引。

    Returns:
        pd.DataFrame，header=None，所有行列保留原始值。
    """
    import pandas as pd
    from pathlib import Path

    path = Path(filepath)
    if not path.exists():
        raise FileNotFoundError(f"文件不存在: {filepath}")

    df = pd.read_excel(filepath, sheet_name=sheet_name, header=None)
    return df


def read_option_master(filepath: str, sheet_name=0) -> pd.DataFrame:
    """读取选项规格主数据表，校验必要字段。

    选项规格主数据格式（固定列）：
      固定列: 主编码, 商品名称
      维度列（每维度 3 列）: 推荐{dim}, 默认{dim}, {dim}
      其中 dim ∈ {糖度, 温度, 规格, 奶底, 茶底}

    必要字段（缺一报错）: 主编码, 商品名称
    可选字段（缺失时自动注入空列）: 推荐{dim}, 默认{dim}, {dim}

    Args:
        filepath: 选项规格主数据表 Excel 路径。
        sheet_name: 工作表名或索引。

    Returns:
        pd.DataFrame，所有预期列均已就位。

    Raises:
        ValueError: 缺少「主编码」或「商品名称」列。
    """
    df = read_excel(filepath, sheet_name=sheet_name)

    # ── Step 0: 应用列名别名（自动重命名异名列）──
    for col in list(df.columns):
        if col in OPTION_MASTER_COLUMN_ALIASES:
            target = OPTION_MASTER_COLUMN_ALIASES[col]
            if target not in df.columns:
                df.rename(columns={col: target}, inplace=True)
                print(f"[INFO] 列名别名: 「{col}」→「{target}」")

    # 验证固定必要列
    missing_fixed = [c for c in OPTION_MASTER_FIXED_COLUMNS if c not in df.columns]
    if missing_fixed:
        raise ValueError(
            f"选项规格主数据表缺少必要列: {missing_fixed}\n"
            f"当前列名: {list(df.columns)}"
        )

    # 软校验：维度列表列缺失时警告，推荐/默认列缺失时自动注入空列
    for dim in OPTION_MASTER_DIMENSIONS:
        if dim not in df.columns:
            print(f"[WARNING] 主数据表缺少「{dim}」列，该维度将被跳过")
        for prefix in ["推荐", "默认"]:
            col = f"{prefix}{dim}"
            if col not in df.columns:
                df[col] = None

    return df


# ── 自测 ──────────────────────────────────────────────────────

if __name__ == "__main__":
    import tempfile
    import os, shutil as _shutil

    # ── 备份真实 memory.json ──
    _mem_path = os.path.expanduser("~/.menupilot/memory.json")
    _mem_backup = None
    if os.path.exists(_mem_path):
        _mem_backup_path = _mem_path + ".self_test_backup"
        _shutil.copy(_mem_path, _mem_backup_path)
        _mem_backup = _mem_backup_path

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
    opt_master_path = os.path.join(tmpdir, "opt_master.xlsx")
    opt_no_code_path = os.path.join(tmpdir, "opt_no_code.xlsx")
    opt_minimal_path = os.path.join(tmpdir, "opt_minimal.xlsx")
    opt_no_dim_path = os.path.join(tmpdir, "opt_no_dim.xlsx")

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

        # ── 7. 缺可通配字段（奶底）→ 自动注入空列 ──
        print("7. 缺可通配字段（奶底）→ 自动注入空列")
        no_milk_path = os.path.join(tmpdir, "no_milk_master.xlsx")
        pd.DataFrame({
            "品名": ["测试商品"],
            "杯型": ["中杯"],
            "做法": ["少冰"],
            "糖":   ["七分糖"],
        }).to_excel(no_milk_path, index=False)
        df_no_milk = read_master(no_milk_path)
        check("奶底" in df_no_milk.columns, "奶底 列被自动注入")
        check(df_no_milk["奶底"].isna().all(), "注入的奶底列全部为 None")
        check(df_no_milk.iloc[0]["品名"] == "测试商品", "其他列正常")
        print()

        # ── 9. column_aliases 自动重命名 ──
        print("9. column_aliases 自动重命名（温度 → 做法）")
        from menupilot.data.memory import reset_memory, add_column_alias
        reset_memory()
        alias_path = os.path.join(tmpdir, "alias_master.xlsx")
        pd.DataFrame({
            "品名": ["测试商品"],
            "杯型": ["中杯"],
            "温度": ["少冰"],     # 异构列名：用户写了「温度」而非「做法」
            "糖":   ["七分糖"],
        }).to_excel(alias_path, index=False)
        # 预热记忆：告诉系统「温度」→ canonical temperature → 映射到主数据「做法」
        add_column_alias("温度", "temperature")
        df_alias = read_master(alias_path)
        check("做法" in df_alias.columns, "「温度」被自动重命名为「做法」")
        check("温度" not in df_alias.columns, "原列名「温度」不再存在")
        check(df_alias.iloc[0]["做法"] == "少冰", "重命名后数据保留正确")
        reset_memory()
        print()

        # ── 10. soft_validation 模式 → 不抛异常，标记缺失列 ──
        print("10. soft_validation 模式 → 标记缺失列，不抛异常")
        reset_memory()
        soft_path = os.path.join(tmpdir, "soft_master.xlsx")
        pd.DataFrame({
            "品名": ["测试"],
            "杯型": ["中杯"],
            "Unnamed: 2": [""],
            "温度": ["少冰"],
            "糖":   ["七分糖"],
            "代码": ["T240"],
        }).to_excel(soft_path, index=False)
        df_soft = read_master(soft_path, soft_validation=True)
        check("做法" not in df_soft.columns, "「做法」列确实不存在")
        check(df_soft.attrs.get("_missing_required") == ["做法"],
              f"标记缺失字段: {df_soft.attrs.get('_missing_required')}")
        # 硬校验模式仍抛异常
        try:
            read_master(soft_path, soft_validation=False)
            check(False, "硬校验模式下缺列应抛 ValueError")
        except ValueError as e:
            check("做法" in str(e), f"硬校验仍报错: {e}")
        reset_memory()
        print()

        # ── 11. 缺必要字段（做法）→ 仍抛异常 ──
        print("11. 缺必要字段（做法）→ 仍抛异常（硬校验）")
        bad_required_path = os.path.join(tmpdir, "bad_required.xlsx")
        pd.DataFrame({
            "品名": ["测试"],
            "杯型": ["中杯"],
            "奶底": ["牛奶"],
            "糖":   ["七分糖"],
        }).to_excel(bad_required_path, index=False)
        try:
            read_master(bad_required_path)
            check(False, "缺做法应抛 ValueError")
        except ValueError as e:
            check("做法" in str(e), f"报错信息包含「做法」（实际: {e}）")
        print()

        # ── 12. read_option_master 正常读取 ──
        print("12. read_option_master 正常读取")
        pd.DataFrame({
            "主编码": ["A001", "A002"],
            "商品名称": ["茉莉绿茶", "珍珠奶茶"],
            "推荐糖度": ["七分糖", "全糖"],
            "默认糖度": ["五分糖", "标准糖"],
            "糖度": ["七分糖；五分糖；三分糖", "全糖；标准糖"],
            "推荐温度": ["正常冰", "热"],
            "默认温度": ["少冰", "热"],
            "温度": ["正常冰；少冰；去冰", "热；正常冰"],
            "推荐规格": ["中杯", "大杯"],
            "默认规格": ["中杯", "大杯"],
            "规格": ["中杯；大杯", "大杯；中杯"],
            "推荐奶底": ["牛奶", ""],
            "默认奶底": ["燕麦奶", ""],
            "奶底": ["牛奶；燕麦奶", ""],
            "推荐茶底": ["绿茶", ""],
            "默认茶底": ["绿茶", ""],
            "茶底": ["绿茶；乌龙茶", ""],
        }).to_excel(opt_master_path, index=False)
        om = read_option_master(opt_master_path)
        check(len(om) == 2, f"读取 2 行（实际 {len(om)}）")
        check("主编码" in om.columns, "包含「主编码」列")
        check(om.iloc[0]["主编码"] == "A001", "第一行主编码 = A001")
        check(om.iloc[0]["糖度"] == "七分糖；五分糖；三分糖", "糖度列正确")
        print()

        # ── 13. read_option_master 缺少主编码 → ValueError ──
        print("13. read_option_master 缺少主编码 → ValueError")
        pd.DataFrame({"商品名称": ["测试"]}).to_excel(opt_no_code_path, index=False)
        try:
            read_option_master(opt_no_code_path)
            check(False, "缺少主编码应抛 ValueError")
        except ValueError as e:
            check("主编码" in str(e), f"报错含「主编码」（实际: {e}）")
        print()

        # ── 14. read_option_master 缺少维度列 → 注入空列 ──
        print("14. read_option_master 缺少维度列 → 自动注入空列")
        pd.DataFrame({
            "主编码": ["A001"],
            "商品名称": ["测试"],
            "糖度": ["七分糖"],
        }).to_excel(opt_minimal_path, index=False)
        om_min = read_option_master(opt_minimal_path)
        check("推荐糖度" in om_min.columns, "「推荐糖度」被自动注入")
        check("默认温度" in om_min.columns, "「默认温度」被自动注入")
        check("规格" not in om_min.columns, "「规格」列不存在（维度列表列不注入，仅警告）")
        check(om_min["推荐糖度"].isna().all(), "注入的推荐糖度列为 None")
        print()

        # ── 15. read_option_master 缺少维度列表列 → 警告 ──
        print("15. read_option_master 缺少维度列表列 → 警告（不报错）")
        pd.DataFrame({
            "主编码": ["A001"],
            "商品名称": ["测试"],
        }).to_excel(opt_no_dim_path, index=False)
        om_no_dim = read_option_master(opt_no_dim_path)
        check(len(om_no_dim) == 1, "仍然正常读取 1 行")
        check("主编码" in om_no_dim.columns, "主编码列正常")
        print()

    finally:
        for f in [master_path, template_path, empty_path, multi_sheet_path,
                  os.path.join(tmpdir, "bad_master.xlsx"),
                  os.path.join(tmpdir, "strip.xlsx"),
                  os.path.join(tmpdir, "no_milk_master.xlsx"),
                  os.path.join(tmpdir, "bad_required.xlsx"),
                  os.path.join(tmpdir, "alias_master.xlsx"),
                  os.path.join(tmpdir, "soft_master.xlsx"),
                  opt_master_path, opt_no_code_path, opt_minimal_path, opt_no_dim_path]:
            if os.path.exists(f):
                os.remove(f)
        os.rmdir(tmpdir)

    # ── 还原真实 memory.json ──
    if _mem_backup:
        from menupilot.data.memory import reload as _mem_reload
        _shutil.move(_mem_backup, _mem_path)
        _mem_reload()

    print(f"=== 结果: {passed} passed, {failed} failed ===")
