"""
CLI 入口 — MenuPilot 智能 POS 模板映射助手。
一条命令完成 SOP 字段自动映射。

用法:
    menupilot --master 主数据表.xlsx --template POS模板.xlsx --output 填充结果.xlsx
    menupilot expand -m 主数据表.xlsx -t 选项模板.xlsx -o 输出.xlsx
"""

import argparse
import os
import sys
from typing import Optional

# Windows 终端默认 GBK，中文容易乱码。切换为 UTF-8 全局输出。
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def build_parser() -> argparse.ArgumentParser:
    """构建 SOP 匹配管线命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="menupilot",
        description="MenuPilot — 智能 POS 模板映射助手",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  menupilot --master 主数据表.xlsx --template POS模板.xlsx --output 结果.xlsx
  menupilot -m 主数据表.xlsx -t POS模板.xlsx -o 结果.xlsx --target-col 配料
  menupilot -m 主数据表.xlsx -t POS模板.xlsx -o 结果.xlsx -r 报告.txt

--sheet 参数（位置语义）:
  -t template.xlsx --sheet 1    → 模板表读取第 2 个 Sheet
  -m master.xlsx --sheet 2      → 主数据表读取第 3 个 Sheet
        """,
    )
    parser.add_argument("-m", "--master", required=True, help="主数据表 Excel 文件路径")
    parser.add_argument("-t", "--template", required=True, help="POS 模板 Excel 文件路径")
    parser.add_argument("-o", "--output", required=True, help="输出 Excel 文件路径")
    parser.add_argument("--target-col", default="配料", help="模板中需要填充 SOP 的目标列名")
    parser.add_argument("-r", "--report", default=None, help="校验报告输出路径")
    parser.add_argument("--langgraph", action="store_true", default=True, help="使用 LangGraph 编排管线")
    parser.add_argument("--no-langgraph", action="store_false", dest="langgraph", help="禁用 LangGraph")
    return parser


def build_expand_parser() -> argparse.ArgumentParser:
    """构建 expand 子命令参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="menupilot expand",
        description="选项规格模板展开器 — 将主数据表的选项值展开为空白模板行",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n  menupilot expand -m 选项主数据.xlsx -t 选项模板.xlsx -o 输出.xlsx",
    )
    parser.add_argument("-m", "--master", required=True, help="选项规格主数据表 Excel 路径")
    parser.add_argument("-t", "--template", required=True, help="空白选项模板 Excel 路径")
    parser.add_argument("-o", "--output", required=True, help="输出 Excel 文件路径")
    parser.add_argument("--sheet", type=int, default=0, help="Sheet 序号（默认 0）")
    parser.add_argument("--template-sheet", type=int, default=None, help="模板表 Sheet 序号")
    parser.add_argument("--master-sheet", type=int, default=None, help="主数据表 Sheet 序号")
    parser.add_argument("--header-row", type=int, default=2, help="模板表头行号（默认 2）")
    return parser


# ═══════════════════════════════════════════════════════════════════
# 入口（委托给 orchestration 层）
# ═══════════════════════════════════════════════════════════════════

def run(args: Optional[list] = None) -> int:
    """执行 SOP 匹配管线（委托给 agent.orchestration.run_sop_pipeline）。"""
    from menupilot.agent.orchestration import run_sop_pipeline
    return run_sop_pipeline(args)


def run_expand(args: Optional[list] = None) -> int:
    """执行选项展开管线（委托给 agent.orchestration.run_expand_pipeline）。"""
    from menupilot.agent.orchestration import run_expand_pipeline
    return run_expand_pipeline(args)


# ═══════════════════════════════════════════════════════════════════
# API Key 检查
# ═══════════════════════════════════════════════════════════════════

def _ensure_api_key():
    """确保已配置 DeepSeek API Key，否则启动配置向导。"""
    from menupilot import config
    if not config.DEEPSEEK_API_KEY:
        from menupilot.wizard import run_wizard
        run_wizard()
        import importlib
        importlib.reload(config)
        if not config.DEEPSEEK_API_KEY:
            print("\n❌ 未配置 API Key，无法启动。"
                  "请设置环境变量 DEEPSEEK_API_KEY 或运行 menupilot 重新配置。")
            sys.exit(1)


# ═══════════════════════════════════════════════════════════════════
# 入口调度
# ═══════════════════════════════════════════════════════════════════

def main():
    """MenuPilot CLI 主入口。"""
    # expand 子命令路由（纯规则引擎，无需 API Key）
    if len(sys.argv) > 1 and sys.argv[1] == "expand":
        sys.exit(run_expand(sys.argv[2:]))

    # 自测模式
    if "--self-test" in sys.argv:
        sys.argv.remove("--self-test")
        import tempfile, shutil
        import pandas as pd

        # ── 备份真实 memory.json（防止自测清空长期记忆）──
        _mem_path = os.path.expanduser("~/.menupilot/memory.json")
        _mem_backup = None
        if os.path.exists(_mem_path):
            _mem_backup_path = _mem_path + ".self_test_backup"
            shutil.copy(_mem_path, _mem_backup_path)
            _mem_backup = _mem_backup_path

        from menupilot.agent.orchestration import (
            set_batch_mode, set_column_prompt_hook,
            _batch_mode,
        )

        os.environ["USE_MOCK_LLM"] = "1"
        import importlib
        from menupilot import config as _cfg
        importlib.reload(_cfg)

        passed = 0
        failed = 0

        def check(condition, msg):
            nonlocal passed, failed
            if condition:
                passed += 1
                print(f"  PASS  {msg}")
            else:
                failed += 1
                print(f"  FAIL  {msg}")

        print("=== main.py CLI 自测（Mock 模式）===\n")

        tmpdir = tempfile.mkdtemp()
        master_path = os.path.join(tmpdir, "master.xlsx")
        template_path = os.path.join(tmpdir, "template.xlsx")
        output_path = os.path.join(tmpdir, "output.xlsx")

        pd.DataFrame({
            "品名": ["浅浅清茶", "珍珠奶茶"],
            "杯型": ["中杯", "中杯"],
            "奶底": ["牛奶", "椰乳"],
            "做法": ["少冰", "热"],
            "糖": ["七分糖", "无糖"],
            "SOP": ["T240", "T180"],
        }).to_excel(master_path, index=False)

        pd.DataFrame({
            "菜品名称": ["浅浅清茶", "珍珠奶茶"],
            "规格": ["中杯", "中杯"],
            "口味做法组合": ["牛奶, 少冰, 七分糖", "椰乳, 热, 无糖"],
            "配料": ["", ""],
        }).to_excel(template_path, index=False)

        try:
            # ── 1. 基本 CLI 运行 ──
            print("1. 基本 CLI 运行（--master --template --output）")
            exit_code = run([
                "--master", master_path,
                "--template", template_path,
                "--output", output_path,
            ])
            check(exit_code == 0, f"exit_code=0（实际 {exit_code}）")
            check(os.path.exists(output_path), "输出文件已生成")
            report_path = output_path.replace(".xlsx", "_report.txt")
            check(os.path.exists(report_path), "报告文件已生成")
            print()

            # ── 2. 自定义参数 ──
            print("2. 自定义 --target-col 和 --report")
            custom_report = os.path.join(tmpdir, "custom_report.txt")
            exit_code2 = run([
                "-m", master_path, "-t", template_path, "-o", output_path,
                "--target-col", "配料", "-r", custom_report,
            ])
            check(exit_code2 == 0, f"exit_code=0（实际 {exit_code2}）")
            check(os.path.exists(custom_report), "自定义报告路径生效")
            print()

            # ── 3. 缺失文件错误 ──
            print("3. 缺失文件错误处理")
            exit_code3 = run(["-m", "不存在的文件.xlsx", "-t", template_path, "-o", output_path])
            check(exit_code3 == 1, f"exit_code=1（实际 {exit_code3}）")
            print()

            # ── 4. --help ──
            print("4. --help 参数解析")
            try:
                help_text = build_parser().format_help()
                check("--master" in help_text, "--master 出现在 help 中")
                check("--target-col" in help_text, "--target-col 出现在 help 中")
            except SystemExit:
                check(False, "--help 不应触发 SystemExit")
            print()

            # ── 5. 简写参数 ──
            print("5. 简写参数 -m -t -o")
            exit_code5 = run(["-m", master_path, "-t", template_path, "-o", output_path])
            check(exit_code5 == 0, "简写参数正常执行")
            print()

            # ── 6. --sheet 位置语义 ──
            print("6. --sheet 位置语义")
            master_multi = os.path.join(tmpdir, "master_multi.xlsx")
            with pd.ExcelWriter(master_multi, engine="openpyxl") as writer:
                pd.DataFrame({
                    "品名": ["浅浅清茶"], "杯型": ["中杯"], "奶底": ["牛奶"],
                    "做法": ["少冰"], "糖": ["七分糖"], "SOP": ["SHEET0_WRONG"],
                }).to_excel(writer, sheet_name="Sheet0", index=False)
                pd.DataFrame({
                    "品名": ["浅浅清茶"], "杯型": ["中杯"], "奶底": ["牛奶"],
                    "做法": ["少冰"], "糖": ["七分糖"], "SOP": ["T240_CORRECT"],
                }).to_excel(writer, sheet_name="Sheet1", index=False)

            template_multi = os.path.join(tmpdir, "template_multi.xlsx")
            with pd.ExcelWriter(template_multi, engine="openpyxl") as writer:
                pd.DataFrame({
                    "菜品名称": ["不相干商品"], "规格": ["大杯"],
                    "口味做法组合": ["红茶, 全糖, 正常冰"], "配料": [""],
                }).to_excel(writer, sheet_name="Sheet0", index=False)
                pd.DataFrame({
                    "菜品名称": ["浅浅清茶"], "规格": ["中杯"],
                    "口味做法组合": ["牛奶, 少冰, 七分糖"], "配料": [""],
                }).to_excel(writer, sheet_name="Sheet1", index=False)

            print("  6a: --sheet 1 在 -t 后 → 模板 Sheet 1")
            exit_code6a = run(["-m", master_multi, "-t", template_multi, "--sheet", "1", "-o", output_path])
            check(exit_code6a == 0, f"exit_code=0（实际 {exit_code6a}）")
            df_6a = pd.read_excel(output_path, sheet_name=1)  # template_sheet=1 → 结果在 Sheet1
            check(df_6a.iloc[0]["配料"] == "SHEET0_WRONG",
                  f"主数据 Sheet 0，SOP=SHEET0_WRONG（实际 {df_6a.iloc[0]['配料']}）")
            print()

            # ── 7. 交互式列分类（Mock hook）──
            print("7. 交互式列分类（_interactive_classify_columns）")
            from menupilot.data.memory import reset_memory as mem_reset, get_column_alias as mem_get_col
            mem_reset()

            df_interactive = pd.DataFrame({
                "菜品名称": ["测试商品"], "原料类型": ["红茶"],
                "规格": ["中杯"], "配料": [""], "备注": [""],
            })
            interactive_path = os.path.join(tmpdir, "interactive.xlsx")
            df_interactive.to_excel(interactive_path, index=False)
            interactive_out = os.path.join(tmpdir, "interactive_out.xlsx")

            hook_calls = []
            def mock_column_hook(col, sample):
                hook_calls.append((col, sample))
                mapping = {"原料类型": "tea_base", "备注": "ignore"}
                return mapping.get(col, "ignore")

            set_column_prompt_hook(mock_column_hook)
            from menupilot import config as cfg_inner
            orig_schema = dict(cfg_inner.MOCK_SCHEMA_RESPONSE)
            cfg_inner.MOCK_SCHEMA_RESPONSE = {
                "field_mapping": {"菜品名称": "product_name", "规格": "size"},
                "composite_col": None, "target_col": "配料", "irrelevant_cols": [],
            }

            exit_code8 = run(["-m", master_path, "-t", interactive_path, "-o", interactive_out])
            check(exit_code8 == 0, f"交互分类后正常执行（实际 {exit_code8}）")
            called_cols = {c for c, _ in hook_calls}
            check("原料类型" in called_cols, "「原料类型」触发交互")
            check(mem_get_col("原料类型") == "tea_base", "别名已持久化")
            cfg_inner.MOCK_SCHEMA_RESPONSE = orig_schema
            set_column_prompt_hook(None)
            mem_reset()
            print()

        finally:
            for f in [master_path, template_path, output_path,
                      output_path.replace(".xlsx", "_report.txt"),
                      os.path.join(tmpdir, "custom_report.txt"),
                      os.path.join(tmpdir, "master_multi.xlsx"),
                      os.path.join(tmpdir, "template_multi.xlsx"),
                      os.path.join(tmpdir, "interactive.xlsx"),
                      os.path.join(tmpdir, "interactive_out.xlsx"),
                      os.path.join(tmpdir, "interactive_out_report.txt")]:
                if os.path.exists(f):
                    os.remove(f)
            os.rmdir(tmpdir)

        from menupilot.agent.token_classifier import reset_cache
        reset_cache()

        # ── 还原真实 memory.json ──
        if _mem_backup:
            from menupilot.data.memory import reload as mem_reload
            shutil.move(_mem_backup, _mem_path)
            mem_reload()

        print(f"=== 结果: {passed} passed, {failed} failed ===")

    elif len(sys.argv) <= 1:
        # 无参数：进入交互 REPL 模式
        _ensure_api_key()
        from menupilot.cli.repl import repl_loop
        repl_loop()
    else:
        # --help / -h 无需 API Key，直接放行
        if not (set(sys.argv) & {"--help", "-h"}):
            _ensure_api_key()
        sys.exit(run())


if __name__ == "__main__":
    main()