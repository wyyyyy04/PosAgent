"""
业务编排层 — SOP 匹配管线和选项展开管线的完整执行流程。

从 main.py 提取，负责：
  - 管线前检查（模板类型检测、Schema 预分析、主数据列推断）
  - 交互式列分类（未识别列的 TTY 交互确认）
  - 调用 workflow / expander 执行管线
  - 管线后报告生成

不负责 CLI 参数解析（留给 main.py）。
"""

import os
import sys
import time
from typing import Optional

# Windows 终端 UTF-8（orchestration 作为独立模块也需要）
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


class CLIError(Exception):
    """CLI 可恢复错误，run() 捕获后返回 exit_code=1。"""


# ═══════════════════════════════════════════════════════════════════
# 列分类交互
# ═══════════════════════════════════════════════════════════════════

FIELD_OPTIONS = [
    ("product_name", "商品名"),
    ("size",         "规格"),
    ("milk_base",    "奶底"),
    ("temperature",  "温度/做法"),
    ("sugar",        "糖度"),
    ("tea_base",     "茶底"),
    ("composite_col","口味做法组合"),
    ("sop",          "配料/SOP代码"),
    ("ignore",       "忽略此列"),
]

# 交互 hook（用于测试时注入自定义回调）
_column_prompt_hook = None

# 批量模式标志：CLI 带参数调用时为 True（不进入交互），REPL 模式为 False
_batch_mode = False


def set_column_prompt_hook(hook) -> None:
    """注入自定义列分类回调（用于自动化测试）。设为 None 恢复默认交互式行为。"""
    global _column_prompt_hook
    _column_prompt_hook = hook


def set_batch_mode(enabled: bool) -> None:
    """设置批量模式标志。True 时所有交互提示自动跳过（CLI 模式）。"""
    global _batch_mode
    _batch_mode = enabled


def _interactive_classify_columns(
    unrecognized_cols: list,
    template_df: "pd.DataFrame",
    schema_result: dict,
) -> None:
    """引导用户手动分类未识别列（交互模式）或自动跳过（CLI 模式）。

    交互模式（TTY）：展示列名 + 样例值，用户选择 canonical 字段映射。
    CLI 模式（非 TTY）：打印警告，全部标记为 ignore，不阻塞执行。
    选择结果写入 column_aliases 长期记忆和当前 schema_result。
    """
    # ── 批量模式（CLI 带参数）── 自动跳过，不阻塞 ──
    if _batch_mode:
        col_list = "、".join(str(c) for c in unrecognized_cols)
        print(
            f"[WARNING] 批量模式，以下 {len(unrecognized_cols)} 个列无法自动识别，"
            f"将跳过: {col_list}"
        )
        print("          如需手动指定列映射，请使用 REPL 模式: python main.py（无参数启动）")
        for col in unrecognized_cols:
            schema_result["irrelevant_cols"].append(col)
        schema_result["unrecognized_cols"] = []
        return

    # ── 交互模式（REPL / TTY）──
    import pandas as pd
    from data.memory import add_column_alias

    _skipped_count = 0

    for col in unrecognized_cols:
        sample_vals = (
            template_df[col].dropna().astype(str).unique()[:3]
            if col in template_df.columns else []
        )
        sample_str = ", ".join(sample_vals) if len(sample_vals) > 0 else "(空)"

        if _column_prompt_hook is not None:
            field_name = _column_prompt_hook(col, sample_str)
        else:
            print(f"\n[Schema] 发现未能识别的列：「{col}」")
            print(f"  样例值：{sample_str}")
            for i, (field, label) in enumerate(FIELD_OPTIONS, 1):
                print(f"  [{i}] {field}（{label}）")

            while True:
                try:
                    choice = int(input("  请选择: ").strip()) - 1
                    if 0 <= choice < len(FIELD_OPTIONS):
                        field_name, _ = FIELD_OPTIONS[choice]
                        break
                    print(f"  [错误] 请输入 1-{len(FIELD_OPTIONS)} 之间的数字")
                except EOFError:
                    print(f"  [WARNING] 输入流已关闭，剩余列将全部标记为 ignore")
                    field_name = "ignore"
                    _skipped_count = len(unrecognized_cols) - unrecognized_cols.index(col)
                    break
                except ValueError:
                    print(f"  [错误] 请输入有效数字")

        add_column_alias(col, field_name)

        if field_name == "ignore":
            schema_result["irrelevant_cols"].append(col)
        elif field_name == "composite_col":
            if schema_result.get("composite_col") is None:
                schema_result["composite_col"] = col
            else:
                schema_result["field_mapping"][col] = field_name
        elif field_name == "sop":
            if schema_result.get("target_col") is None:
                schema_result["target_col"] = col
            else:
                schema_result["field_mapping"][col] = field_name
        else:
            schema_result["field_mapping"][col] = field_name

        if _skipped_count > 0:
            for remaining_col in unrecognized_cols[-_skipped_count + 1:]:
                schema_result["irrelevant_cols"].append(remaining_col)
            break

    schema_result["unrecognized_cols"] = []


def _validate_file(path: str, label: str) -> None:
    """验证输入文件存在，失败时抛 CLIError。"""
    if not os.path.exists(path):
        raise CLIError(f"{label} 文件不存在: {path}")
    if not os.path.isfile(path):
        raise CLIError(f"{label} 不是有效文件: {path}")


# ═══════════════════════════════════════════════════════════════════
# SOP 匹配管线
# ═══════════════════════════════════════════════════════════════════

def _resolve_sheet_args(argv: list) -> tuple:
    """预扫描 argv，根据 --sheet 在命令行中的位置决定其归属。

    规则：
      - -t/--template 之后出现的 --sheet N → 模板表 Sheet
      - -m/--master  之后出现的 --sheet N → 主数据表 Sheet
      - 未跟在任何文件参数之后的 --sheet N → 默认属于模板
    """
    master_sheet = 0
    template_sheet = 0
    last_file_arg = None

    filtered = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-m", "--master"):
            last_file_arg = "master"
            filtered.append(arg)
        elif arg in ("-t", "--template"):
            last_file_arg = "template"
            filtered.append(arg)
        elif arg == "--sheet":
            if i + 1 < len(argv):
                try:
                    sheet_val = int(argv[i + 1])
                except (ValueError, TypeError):
                    sheet_val = 0
                if last_file_arg == "master":
                    master_sheet = sheet_val
                else:
                    template_sheet = sheet_val
                i += 1
        else:
            filtered.append(arg)
        i += 1

    return filtered, master_sheet, template_sheet


def run_sop_pipeline(args: Optional[list] = None) -> int:
    """执行 SOP 匹配管线（原 main.run()）。

    Args:
        args: 命令行参数列表，None 时使用 sys.argv[1:]。

    Returns:
        exit code: 0=成功, 1=失败
    """
    import argparse as _argparse
    from agent.workflow import run_pipeline

    # 构建 parser（与 main.py 的 build_parser 一致）
    parser = _argparse.ArgumentParser(
        prog="python main.py",
        description="POS Template Mapping Agent — 自动将主数据表 SOP 映射到 POS 模板",
    )
    parser.add_argument("-m", "--master", required=True, help="主数据表 Excel 文件路径")
    parser.add_argument("-t", "--template", required=True, help="POS 模板 Excel 文件路径")
    parser.add_argument("-o", "--output", required=True, help="输出 Excel 文件路径")
    parser.add_argument("--target-col", default="配料", help="模板中需要填充 SOP 的目标列名")
    parser.add_argument("-r", "--report", default=None, help="校验报告输出路径")
    parser.add_argument("--langgraph", action="store_true", default=True, help="使用 LangGraph")
    parser.add_argument("--no-langgraph", action="store_false", dest="langgraph")

    if args is None:
        args = sys.argv[1:]
    filtered_args, master_sheet, template_sheet = _resolve_sheet_args(list(args))
    opts = parser.parse_args(filtered_args)

    try:
        _validate_file(opts.master, "主数据表")
        _validate_file(opts.template, "模板表")
    except CLIError as e:
        print(f"[ERROR] {e}")
        return 1

    report_path = opts.report or opts.output.replace(".xlsx", "_report.txt")

    print("=" * 56)
    print("  POS Template Mapping Agent")
    print("=" * 56)
    print(f"  主数据表: {opts.master} (Sheet {master_sheet})")
    print(f"  模板表:   {opts.template} (Sheet {template_sheet})")
    print(f"  目标列:   {opts.target_col}")
    print(f"  输出:     {opts.output}")
    print(f"  报告:     {report_path}")
    print("-" * 56)

    import config as _cfg
    print(f"  LLM 模式: {'MOCK' if _cfg.USE_MOCK_LLM else 'REAL'} (模型: {_cfg.DEEPSEEK_MODEL})")

    # ── 模板类型检测 ──
    from excel_io.excel_reader import read_template_raw, read_template
    from agent.template_preprocessor import detect_template_type

    raw_df = read_template_raw(opts.template, sheet_name=template_sheet)
    _template_type = detect_template_type(raw_df)

    if _template_type == "chowbus":
        print(f"[Template] 检测到 chowbus 模板类型，跳过 Schema 分析")
        opts.target_col = "sop_code"
    else:
        # ── Schema 预分析 + 交互兜底 ──
        from agent.schema_analyzer import (
            _template_fingerprint,
            analyze_from_dataframe,
        )
        from data.memory import (
            get_template_rule as mem_get_template_rule,
            save_template_rule as mem_save_template_rule,
        )

        preload_df = read_template(opts.template, sheet_name=template_sheet)
        fingerprint = _template_fingerprint(list(preload_df.columns))

        cached_schema = mem_get_template_rule(fingerprint)
        if cached_schema is not None:
            print(f"[Schema] 模板指纹缓存命中 {fingerprint[:12]}...（跳过 Schema 分析）")
        else:
            schema_result = analyze_from_dataframe(preload_df)
            unrecognized = schema_result.get("unrecognized_cols", [])
            if unrecognized:
                print(f"[Schema] LLM 未能识别 {len(unrecognized)} 个列: {unrecognized}")
                _interactive_classify_columns(unrecognized, preload_df, schema_result)
                mem_save_template_rule(fingerprint, schema_result)
                print(f"[Schema] 完整结果已写入模板指纹缓存 {fingerprint[:12]}...")

    # ── 主数据预加载 + 列推断 ──
    from excel_io.excel_reader import (
        read_master, MASTER_REQUIRED_COLUMNS,
        MASTER_WILDCARD_COLUMNS, MASTER_OPTIONAL_COLUMNS,
    )
    from data.memory import add_column_alias as mem_add_col_alias
    from agent.schema_analyzer import infer_master_columns

    master_df = read_master(opts.master, sheet_name=master_sheet, soft_validation=True)
    missing_master = master_df.attrs.get("_missing_required", [])

    if missing_master:
        already_covered = (
            set(MASTER_REQUIRED_COLUMNS) | set(MASTER_WILDCARD_COLUMNS) | set(MASTER_OPTIONAL_COLUMNS)
        )
        already_covered.difference_update(missing_master)
        candidate_cols = [c for c in master_df.columns if c not in already_covered]

        if candidate_cols:
            import pandas as _pd
            sample_data = {}
            for col in candidate_cols:
                vals = (
                    master_df[col].dropna().astype(str).unique()[:5].tolist()
                    if col in master_df.columns else []
                )
                sample_data[col] = vals

            _MASTER_CN_TO_CANONICAL = {
                "品名": "product_name", "杯型": "size",
                "做法": "temperature", "糖": "sugar",
            }
            canonical_missing = [
                _MASTER_CN_TO_CANONICAL.get(f, f) for f in missing_master
            ]
            inference = infer_master_columns(candidate_cols, sample_data, canonical_missing)

            high_conf = {}
            low_conf = {}
            for col, info in inference.items():
                if info.get("confidence") == "high" and info.get("field"):
                    high_conf[col] = info
                else:
                    low_conf[col] = info

            if high_conf:
                print(f"[Master] LLM 高置信度识别 {len(high_conf)} 列:")
                for col, info in high_conf.items():
                    print(f"         「{col}」→ {info['field']}（{info['reason']}）")
                    mem_add_col_alias(col, info["field"])

            if low_conf:
                if _batch_mode:
                    col_list = "、".join(str(c) for c in low_conf.keys())
                    print(f"[WARNING] 以下 {len(low_conf)} 列未能高置信度识别，批量模式下将跳过: {col_list}")
                    for col, info in low_conf.items():
                        print(f"           「{col}」→ {info.get('reason', '无法判断')}")
                    print("          如需手动指定列映射，请使用 REPL 模式: python main.py（无参数启动）")
                else:
                    print(f"[Master] 以下 {len(low_conf)} 列未能高置信度识别，需手动确认:")
                    _interactive_classify_columns(
                        list(low_conf.keys()), master_df,
                        {"field_mapping": {}, "composite_col": None,
                         "target_col": None, "irrelevant_cols": [],
                         "unrecognized_cols": list(low_conf.keys())},
                    )
        else:
            missing_str = "、".join(str(c) for c in missing_master)
            if _batch_mode:
                print(f"[WARNING] 缺少必要字段 {missing_master}，且无候选列可推断，将跳过: {missing_str}")
                print("          如需手动指定列映射，请使用 REPL 模式: python main.py（无参数启动）")
            else:
                print(f"[Master] 缺少必要字段 {missing_master}，且无候选列，请手动确认")
                _interactive_classify_columns(
                    missing_master, master_df,
                    {"field_mapping": {}, "composite_col": None,
                     "target_col": None, "irrelevant_cols": [],
                     "unrecognized_cols": missing_master},
                )

    # ── 运行管线 ──
    t0 = time.time()
    state = run_pipeline(
        master_path=opts.master,
        template_path=opts.template,
        output_path=opts.output,
        report_path=report_path,
        target_col=opts.target_col,
        master_sheet=master_sheet,
        template_sheet=template_sheet,
        use_langgraph=opts.langgraph,
    )
    elapsed = time.time() - t0

    if state.get("error") is not None:
        print(f"\n[FAIL] 管线在 '{state['error_step']}' 步骤失败:")
        print(f"       {state['error']}")
        print(f"      耗时: {elapsed:.1f}s")
        return 1

    api_calls = state["api_call_count"] if state.get("api_call_count") is not None else "?"
    print(f"\n[OK] 映射完成!  API 调用: {api_calls} 次  总耗时: {elapsed:.1f}s\n")

    summary = state.get("console_summary", "") or state["report"]
    if summary:
        try:
            sys.stdout.buffer.write((summary + "\n").encode("utf-8"))
            sys.stdout.buffer.flush()
        except (UnicodeError, AttributeError):
            print(summary)
    else:
        total = len(state["match_results"])
        high = sum(1 for r in state["match_results"] if r.get("confidence") == "HIGH")
        print(f"     总行数:   {total}")
        print(f"     高置信度: {high} ({100*high/total:.1f}%)")

    from data.memory import get_new_tokens
    new_tokens = get_new_tokens()
    if new_tokens:
        print(f"\n  [记忆] 本次运行新增了 {len(new_tokens)} 个 token 别名:")
        for word, ttype in new_tokens:
            print(f"         「{word}」→ {ttype}")
        print(f"    💡 如有误选，可执行 /memory edit <词语> <新类型> 修正")

    print(f"\n  输出文件: {opts.output}")
    print(f"  校验报告: {report_path}")
    return 0


def run_sop_pipeline_kwargs(
    master_path: str,
    template_path: str,
    output_path: str,
    target_col: str = "配料",
    report_path: str = "",
    column_mapping: Optional[dict] = None,
) -> dict:
    """Agent 直接调用入口 — 接受 keyword args，返回结构化结果。

    与 run_sop_pipeline 的区别：不走 argparse，直接调用 workflow。
    column_mapping 允许 Agent 传入动态列映射，适配任意列名的模板。

    Args:
        master_path: 主数据表路径。
        template_path: 模板表路径。
        output_path: 输出路径。
        target_col: 目标填充列名。
        report_path: 报告路径（可选）。
        column_mapping: 列映射 dict，如 {'温度':'做法', '产品名称':'品名'}。

    Returns:
        {"ok": bool, "total_rows": int, "high_conf": int, "low_conf": int,
         "report": str, "api_calls": int, "elapsed": float, "error": str}
    """
    import time

    # 注入列映射到 column_aliases 记忆（Agent 分析 schema 后传入）
    if column_mapping:
        from data.memory import add_column_alias
        for col_name, canonical in column_mapping.items():
            add_column_alias(str(col_name), str(canonical))

    from agent.workflow import run_pipeline

    t0 = time.time()
    try:
        state = run_pipeline(
            master_path=master_path,
            template_path=template_path,
            output_path=output_path,
            report_path=report_path or output_path.replace(".xlsx", "_report.txt"),
            target_col=target_col,
        )
        elapsed = time.time() - t0

        if state.get("error"):
            return {
                "ok": False,
                "error": state["error"],
                "error_step": state.get("error_step", ""),
                "elapsed": elapsed,
            }

        results = state.get("match_results", [])
        return {
            "ok": True,
            "total_rows": len(results),
            "high_conf": sum(1 for r in results if r.get("confidence") == "HIGH"),
            "low_conf": sum(1 for r in results if r.get("confidence") == "LOW_CONFIDENCE"),
            "report": state.get("console_summary", ""),
            "api_calls": state.get("api_call_count", 0),
            "elapsed": elapsed,
            "output_path": output_path,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "elapsed": time.time() - t0}


# ═══════════════════════════════════════════════════════════════════
# 选项展开管线
# ═══════════════════════════════════════════════════════════════════

def run_expand_pipeline(args: Optional[list] = None) -> int:
    """执行选项规格模板展开管线（原 main.run_expand()）。

    Args:
        args: 命令行参数列表，None 时使用 sys.argv[2:]（跳过 "expand" 子命令名）。

    Returns:
        exit code: 0=成功, 1=失败
    """
    import argparse as _argparse

    parser = _argparse.ArgumentParser(
        prog="python main.py expand",
        description="选项规格模板展开器 — 将主数据表的选项值展开为空白模板行",
    )
    parser.add_argument("-m", "--master", required=True, help="选项规格主数据表 Excel 路径")
    parser.add_argument("-t", "--template", required=True, help="空白选项模板 Excel 路径")
    parser.add_argument("-o", "--output", required=True, help="输出 Excel 文件路径")
    parser.add_argument("--sheet", type=int, default=0, help="Sheet 序号（默认 0）")
    parser.add_argument("--template-sheet", type=int, default=None, help="模板表 Sheet 序号")
    parser.add_argument("--master-sheet", type=int, default=None, help="主数据表 Sheet 序号")
    parser.add_argument("--header-row", type=int, default=2, help="模板表头行号（默认 2）")

    if args is None:
        args = sys.argv[2:]
    opts = parser.parse_args(args)

    master_sheet = opts.master_sheet if opts.master_sheet is not None else opts.sheet
    template_sheet = opts.template_sheet if opts.template_sheet is not None else opts.sheet

    try:
        _validate_file(opts.master, "选项主数据表")
        _validate_file(opts.template, "选项模板表")
    except CLIError as e:
        print(f"[ERROR] {e}")
        return 1

    from excel_io.excel_reader import read_option_master
    from excel_io.excel_writer import write_expanded_template
    from agent.option_expander import expand_master_to_options, DIMENSIONS

    print("=" * 56)
    print("  Option Specification Template Expander")
    print("=" * 56)
    print(f"  主数据表: {opts.master} (Sheet {master_sheet})")
    print(f"  模板表:   {opts.template} (Sheet {template_sheet})")
    print(f"  输出:     {opts.output}")
    print("-" * 56)

    master_df = read_option_master(opts.master, sheet_name=master_sheet)
    expanded_df = expand_master_to_options(master_df)

    write_expanded_template(
        opts.template, opts.output, expanded_df,
        header_row=opts.header_row,
    )

    print(f"\n[OK] 展开完成!")
    print(f"  主数据行数: {len(master_df)}")
    print(f"  生成模板行数: {len(expanded_df)}")

    if not expanded_df.empty:
        dim_counts = expanded_df["口味做法组名"].value_counts()
        dim_parts = []
        for dim in DIMENSIONS:
            count = int(dim_counts.get(dim, 0))
            if count == 0:
                dim_parts.append(f"{dim}=0(无数据)")
            else:
                dim_parts.append(f"{dim}={count}")
        print(f"  维度分布: {', '.join(dim_parts)}")
    else:
        print("  维度分布: 无数据")

    print(f"\n  输出文件: {opts.output}")
    return 0