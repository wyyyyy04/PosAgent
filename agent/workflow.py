"""
LangGraph 工作流定义 — 编排完整 POS 模板映射管线。

节点顺序:
  load_data → preprocess → analyze_schema → classify_tokens → normalize → validate → match → write_output

每个节点读取上一个节点的输出，写入本节点的结果。任一步骤失败可捕获并进入错误处理。
兼容无 langgraph 安装环境，提供 run_pipeline() 作为纯顺序回退方案。
"""

from typing import Any, Dict, List, Optional

import pandas as pd

from agent.matching_engine import generate_console_summary, generate_report as me_generate_report
from agent.matching_engine import match
from agent.rule_engine import (
    check_row_completeness,
    master_to_canonical,
    template_to_canonical,
    validate_tokens,
)
from agent.schema_analyzer import analyze_from_dataframe
from agent.token_classifier import classify_from_dataframe, reset_cache as tc_reset_cache
from excel_io.excel_reader import read_master, read_template
from excel_io.excel_writer import write_result

# ── 工作流状态键 ────────────────────────────────────────────────


class PipelineState:
    """管线状态容器。每个节点读/写此对象上的属性。"""

    def __init__(
        self,
        master_path: str,
        template_path: str,
        output_path: str,
        report_path: Optional[str] = None,
        target_col: str = "配料",
        master_sheet: int = 0,
        template_sheet: int = 0,
    ):
        self.master_path = master_path
        self.template_path = template_path
        self.output_path = output_path
        self.report_path = report_path or output_path.replace(".xlsx", "_report.txt")
        self.target_col = target_col
        self.master_sheet = master_sheet
        self.template_sheet = template_sheet

        # 中间数据
        self.master_df: Optional[pd.DataFrame] = None
        self.template_df: Optional[pd.DataFrame] = None
        self.template_type: str = "standard"  # "standard" | "chowbus"
        self.chowbus_rows: Optional[List[Dict[str, Any]]] = None
        self.schema_result: Optional[Dict[str, Any]] = None
        self.token_results: Optional[List[Dict[str, Any]]] = None
        self.master_canonical: Optional[List[Dict[str, Any]]] = None
        self.template_canonical: Optional[List[Dict[str, Any]]] = None
        self.validated_tokens: Optional[List[Dict[str, Any]]] = None
        self.match_results: Optional[List[Dict[str, Any]]] = None
        self.report: str = ""
        self.console_summary: str = ""

        # 错误信息
        self.error: Optional[str] = None
        self.error_step: Optional[str] = None

        # 统计
        self.api_call_count: int = 0

    def set_error(self, step: str, msg: str) -> None:
        """记录错误并阻止后续步骤。"""
        self.error = msg
        self.error_step = step

    @property
    def has_error(self) -> bool:
        return self.error is not None


# ── 管线节点 ────────────────────────────────────────────────────


def step_load_data(state: PipelineState) -> PipelineState:
    """Step 1: 读取主数据表和模板表 + 模板类型检测。"""
    if state.has_error:
        return state
    try:
        from agent.template_preprocessor import detect_template_type, collect_chowbus_rows

        state.master_df = read_master(state.master_path, sheet_name=state.master_sheet)

        # 先以 header=None 读取模板原始数据，用于类型检测
        from excel_io.excel_reader import read_template_raw
        raw_df = read_template_raw(state.template_path, sheet_name=state.template_sheet)
        state.template_type = detect_template_type(raw_df)

        if state.template_type == "chowbus":
            # chowbus 类型：收集散列字段，跳过 Schema Analyzer
            state.chowbus_rows = collect_chowbus_rows(raw_df)
            state.template_df = None  # chowbus 不使用标准 template_df
            # 目标列：chowbus 模板固定为 sop_code
            if state.target_col == "配料":
                state.target_col = "sop_code"
        else:
            # standard 类型：正常读取
            state.template_df = read_template(
                state.template_path, sheet_name=state.template_sheet
            )
    except Exception as e:
        state.set_error("load_data", str(e))
    return state


def step_preprocess(state: PipelineState) -> PipelineState:
    """Step 1.5: chowbus 预处理 — 收集散列字段 → Token 分类 → 标准化为 Canonical。

    对 standard 类型透明跳过。
    """
    if state.has_error or state.template_type != "chowbus":
        return state
    try:
        from agent.token_classifier import (
            classify_single,
            set_prompt_hook as tc_set_prompt_hook,
        )
        from agent.rule_engine import master_to_canonical

        # 注入静默钩子：UNKNOWN 不触发询问
        tc_set_prompt_hook(lambda word, ctx: {"action": "skip"})

        # 对每行 composite_info 做 Token 分类
        for row in state.chowbus_rows:
            composite_str = row.get("composite_info", "")
            if composite_str:
                result = classify_single(composite_str)
                row["_tokens"] = result.get("tokens", [])
                row["_missing"] = result.get("missing", [])
            else:
                row["_tokens"] = []
                row["_missing"] = []

        # 还原静默钩子
        tc_set_prompt_hook(None)

        # 转换为 Canonical Schema 行
        from data.canonical_schema import CANONICAL_FIELDS
        from data.token_dict import normalize_token

        canonical_rows = []
        for row in state.chowbus_rows:
            cr = {f: None for f in CANONICAL_FIELDS}
            cr["product_name"] = str(row.get("product_name", "") or "").strip()
            for token in row.get("_tokens", []):
                ttype = token.get("type", "")
                tvalue = token.get("value", "")
                # 将 token 类型映射到 canonical 字段
                type_map = {
                    "茶底": "tea_base",
                    "奶底": "milk_base",
                    "糖度": "sugar",
                    "温度": "temperature",
                    "规格": "size",
                }
                cfield = type_map.get(ttype)
                if cfield and cfield in cr:
                    cr[cfield] = tvalue
            # 补充：直接收集的中文值可能未被 token 词典识别，
            # 但标准化层接受部分缺失，匹配引擎会处理通配
            canonical_rows.append(cr)

        state.template_canonical = canonical_rows

        # 主数据标准化（复用现有逻辑）
        state.master_canonical = master_to_canonical(state.master_df)

    except Exception as e:
        state.set_error("preprocess", str(e))
    return state


def step_analyze_schema(state: PipelineState) -> PipelineState:
    """Step 2: Schema Analyzer 分析模板字段语义。"""
    if state.has_error:
        return state
    # chowbus 类型跳过
    if state.template_type == "chowbus":
        return state
    try:
        state.schema_result = analyze_from_dataframe(state.template_df)
    except Exception as e:
        state.set_error("analyze_schema", str(e))
    return state


def step_classify_tokens(state: PipelineState) -> PipelineState:
    """Step 3: Token Classifier 解析组合字段。"""
    if state.has_error:
        return state
    if state.template_type == "chowbus":
        return state
    try:
        composite_col = state.schema_result.get("composite_col")
        if composite_col and composite_col in state.template_df.columns:
            state.token_results = classify_from_dataframe(
                state.template_df, composite_col
            )
        else:
            # 无组合字段，提供空 token 结果
            state.token_results = [
                {"tokens": [], "missing": []}
                for _ in range(len(state.template_df))
            ]
    except Exception as e:
        state.set_error("classify_tokens", str(e))
    return state


def step_normalize(state: PipelineState) -> PipelineState:
    """Step 4: Rule Engine — 主数据 + 模板标准化为 Canonical Schema。"""
    if state.has_error:
        return state
    if state.template_type == "chowbus":
        return state  # chowbus 已在 preprocess 中完成标准化
    try:
        fm = state.schema_result.get("field_mapping", {})
        composite_col = state.schema_result.get("composite_col", "")
        state.master_canonical = master_to_canonical(state.master_df)
        state.template_canonical = template_to_canonical(
            state.template_df, fm, composite_col, state.token_results
        )
    except Exception as e:
        state.set_error("normalize", str(e))
    return state


def step_validate(state: PipelineState) -> PipelineState:
    """Step 5: Rule Engine — Token 验证 + 必要维度检查。"""
    if state.has_error:
        return state
    if state.template_type == "chowbus":
        return state  # chowbus 已在 preprocess 中完成验证
    try:
        state.validated_tokens = validate_tokens(state.token_results)

        # 检查每行完整度，在 canonical 行上标记
        for i, trow in enumerate(state.template_canonical):
            missing = check_row_completeness(trow)
            if missing:
                trow["_completeness_issues"] = missing
    except Exception as e:
        state.set_error("validate", str(e))
    return state


def step_match(state: PipelineState) -> PipelineState:
    """Step 6: Matching Engine — 模板行 → 主数据行匹配。"""
    if state.has_error:
        return state
    try:
        # ── 断言：验证商品名称在管线各阶段未被改写 ──
        _assert_product_name_integrity(state)
        state.match_results = match(state.template_canonical, state.master_canonical)
    except Exception as e:
        state.set_error("match", str(e))
    return state


def _assert_product_name_integrity(state: PipelineState) -> None:
    """验证模板商品名称从原始读取到匹配前保持一致。

    对比 state.template_df（原始 Excel 读取值）与
    state.template_canonical（经 Schema Analyzer → Token Classifier → Rule Engine 处理后）
    中的 product_name 字段。任何不一致都立即报错，防止 LLM 静默改写数据。
    """
    # chowbus 类型跳过（template_df 为 None，产品名来自预处理层）
    if state.template_type == "chowbus" or state.template_df is None:
        return

    # 找到模板中映射为 product_name 的列
    fm = state.schema_result.get("field_mapping", {})
    src_col = None
    for tcol, cfield in fm.items():
        if cfield == "product_name":
            src_col = tcol
            break

    if src_col is None or src_col not in state.template_df.columns:
        return  # 无法验证，跳过

    raw_names = state.template_df[src_col].astype(str).str.strip().tolist()
    canonical_names = [
        str(r.get("product_name", "")).strip()
        for r in state.template_canonical
    ]

    mismatches = []
    for i, (raw, canonical) in enumerate(zip(raw_names, canonical_names)):
        if raw != canonical:
            mismatches.append(
                f"  行 {i+1}: 原始='{raw}' -> 改写为='{canonical}'"
            )

    if mismatches:
        raise ValueError(
            f"商品名称在管线中被意外改写！{len(mismatches)} 行不一致:\n"
            + "\n".join(mismatches[:10])
            + ("\n  ..." if len(mismatches) > 10 else "")
        )


def step_write_output(state: PipelineState) -> PipelineState:
    """Step 7: 写入结果 Excel + 校验报告。"""
    if state.has_error:
        return state
    try:
        # 构建结果 DataFrame
        sops = [r.get("sop", "") for r in state.match_results]
        confidences = [r.get("confidence", "") for r in state.match_results]

        result_df = pd.DataFrame({
            state.target_col: sops,
            "匹配置信度": confidences,
        })

        write_result(
            state.template_path,
            state.output_path,
            result_df,
            target_col=state.target_col,
            header_row=1,
            data_start_row=3 if state.template_type == "chowbus" else None,
        )

        # 生成用户友好摘要报告（文件 = 完整日志）
        state.report = me_generate_report(state.match_results)
        state.console_summary = generate_console_summary(
            state.match_results, report_path=state.report_path
        )

        # 写入报告文件（完整日志）
        from pathlib import Path
        Path(state.report_path).write_text(state.report, encoding="utf-8")
    except Exception as e:
        state.set_error("write_output", str(e))
    return state


# ── LangGraph 工作流（可选）─────────────────────────────────────

def build_graph():
    """构建 LangGraph StateGraph。

    需要 langgraph 已安装。返回编译后的 app 对象。

    Raises:
        ImportError: langgraph 未安装。
    """
    from langgraph.graph import END, StateGraph

    # 由于 PipelineState 不是 TypedDict，使用简单 dict 作为状态载体
    # 实际运行时会传入 PipelineState 实例
    graph = StateGraph(dict)

    graph.add_node("load_data", _dict_node_wrapper(step_load_data))
    graph.add_node("preprocess", _dict_node_wrapper(step_preprocess))
    graph.add_node("analyze_schema", _dict_node_wrapper(step_analyze_schema))
    graph.add_node("classify_tokens", _dict_node_wrapper(step_classify_tokens))
    graph.add_node("normalize", _dict_node_wrapper(step_normalize))
    graph.add_node("validate", _dict_node_wrapper(step_validate))
    graph.add_node("match", _dict_node_wrapper(step_match))
    graph.add_node("write_output", _dict_node_wrapper(step_write_output))

    graph.set_entry_point("load_data")
    graph.add_edge("load_data", "preprocess")
    graph.add_edge("preprocess", "analyze_schema")
    graph.add_edge("analyze_schema", "classify_tokens")
    graph.add_edge("classify_tokens", "normalize")
    graph.add_edge("normalize", "validate")
    graph.add_edge("validate", "match")
    graph.add_edge("match", "write_output")
    graph.add_edge("write_output", END)

    return graph.compile()


def _dict_node_wrapper(node_fn):
    """将基于 PipelineState 的节点函数包装为接受/返回 dict 的形式。"""

    def wrapper(state: dict) -> dict:
        # 从 dict 中恢复 PipelineState
        ps = state.get("_pipeline_state")
        if ps is None:
            return state
        node_fn(ps)
        # 将 PipelineState 序列化回 dict（简化：直接引用）
        state["_pipeline_state"] = ps
        state["error"] = ps.error
        state["match_results"] = ps.match_results
        state["report"] = ps.report
        return state

    return wrapper


# ── 公开 API ────────────────────────────────────────────────────


def run_pipeline(
    master_path: str,
    template_path: str,
    output_path: str,
    report_path: Optional[str] = None,
    target_col: str = "配料",
    master_sheet: int = 0,
    template_sheet: int = 0,
    use_langgraph: bool = False,
) -> PipelineState:
    """运行完整的 POS 模板映射管线。

    这是工作流的主入口。默认使用纯顺序执行（不依赖 langgraph）。
    传入 use_langgraph=True 可启用 LangGraph 编排。

    Args:
        master_path: 主数据表 Excel 路径。
        template_path: POS 模板 Excel 路径。
        output_path: 输出 Excel 路径。
        report_path: 校验报告路径（默认 output_path 同目录 + _report.txt）。
        target_col: 需要填充的目标列名，默认 "配料"。
        master_sheet: 主数据表 Sheet 序号（从 0 开始），默认 0。
        template_sheet: 模板表 Sheet 序号（从 0 开始），默认 0。
        use_langgraph: 是否使用 LangGraph 编排（需安装 langgraph）。

    Returns:
        PipelineState，包含所有中间数据和最终结果。
        检查 state.has_error 判断是否成功。

    Raises:
        ImportError: use_langgraph=True 但 langgraph 未安装。
    """
    state = PipelineState(
        master_path=master_path,
        template_path=template_path,
        output_path=output_path,
        report_path=report_path,
        target_col=target_col,
        master_sheet=master_sheet,
        template_sheet=template_sheet,
    )

    # 重置 API 调用计数器 + 会话新增 token 追踪
    from agent.schema_analyzer import reset_api_call_count as _sa_reset
    from agent.token_classifier import reset_api_call_count as _tc_reset
    from data.memory import reset_new_tokens as _reset_new_tokens
    _sa_reset()
    _tc_reset()
    _reset_new_tokens()

    if use_langgraph:
        app = build_graph()
        result = app.invoke({"_pipeline_state": state})
        # 从结果 dict 中恢复 PipelineState
        ps = result.get("_pipeline_state", state)
        # 收集 API 调用统计
        from agent.schema_analyzer import get_api_call_count as _sa_count
        from agent.token_classifier import get_api_call_count as _tc_count
        ps.api_call_count = _sa_count() + _tc_count()
        return ps

    # 纯顺序执行
    steps = [
        ("load_data", step_load_data),
        ("preprocess", step_preprocess),
        ("analyze_schema", step_analyze_schema),
        ("classify_tokens", step_classify_tokens),
        ("normalize", step_normalize),
        ("validate", step_validate),
        ("match", step_match),
        ("write_output", step_write_output),
    ]

    for step_name, step_fn in steps:
        step_fn(state)
        if state.has_error:
            break

    # 收集 API 调用统计
    from agent.schema_analyzer import get_api_call_count as _sa_count
    from agent.token_classifier import get_api_call_count as _tc_count
    state.api_call_count = _sa_count() + _tc_count()

    return state


# ── 自测 ────────────────────────────────────────────────────────

if __name__ == "__main__":
    import os
    import tempfile

    os.environ["USE_MOCK_LLM"] = "1"
    import importlib

    importlib.reload(__import__("config"))

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

    print("=== Workflow 自测（Mock LLM 模式）===\n")

    tmpdir = tempfile.mkdtemp()
    master_path = os.path.join(tmpdir, "master.xlsx")
    template_path = os.path.join(tmpdir, "template.xlsx")
    output_path = os.path.join(tmpdir, "output.xlsx")

    # ── 覆盖 Mock 响应以匹配测试数据 ──
    import config as cfg

    original_mock_schema = dict(cfg.MOCK_SCHEMA_RESPONSE)
    tc_reset_cache()

    # ── 准备测试用主数据表和模板表 ──
    pd.DataFrame({
        "品名": ["浅浅清茶", "浅浅清茶", "黑糖波波牛乳", "珍珠奶茶"],
        "杯型": ["中杯", "中杯", "大杯", "中杯"],
        "奶底": ["牛奶", "牛奶", "", "椰乳"],
        "做法": ["少冰", "去冰", "正常冰", "热"],
        "糖": ["七分糖", "标准糖", "标准糖", "无糖"],
        "SOP": [
            "T240、B30/80、S4",
            "T265、B30/105、S5",
            "T200、B50/100、S5",
            "T180、B40/80、S2",
        ],
    }).to_excel(master_path, index=False)

    pd.DataFrame({
        "菜品名称": ["浅浅清茶", "浅浅清茶", "黑糖波波牛乳"],
        "规格": ["中杯", "中杯", "大杯"],
        "口味做法组合": [
            "牛奶, 少冰, 七分糖",
            "牛奶, 去冰, 标准糖",
            "正常冰, 标准糖",
        ],
        "配料": ["", "", ""],
    }).to_excel(template_path, index=False)

    try:
        # ── 1. 完整管线运行 ──
        print("1. 完整管线 run_pipeline()")
        state = run_pipeline(master_path, template_path, output_path)

        check(not state.has_error, f"管线无错误（错误: {state.error}）")
        check(state.master_df is not None, "master_df 已加载")
        check(state.template_df is not None, "template_df 已加载")
        check(state.schema_result is not None, "schema_result 已生成")
        check(state.token_results is not None, "token_results 已生成")
        check(state.master_canonical is not None, "master_canonical 已转换")
        check(state.template_canonical is not None, "template_canonical 已转换")
        check(state.validated_tokens is not None, "validated_tokens 已验证")
        check(state.match_results is not None, "match_results 已生成")
        check(len(state.match_results) == 3, f"3 条匹配结果（实际 {len(state.match_results)}）")
        print()

        # ── 2. 匹配结果验证 ──
        print("2. 匹配结果验证")
        check(
            state.match_results[0]["confidence"] == "HIGH",
            f"第 1 行 HIGH（实际 {state.match_results[0]['confidence']}）",
        )
        check(
            state.match_results[1]["confidence"] == "HIGH",
            f"第 2 行 HIGH（实际 {state.match_results[1]['confidence']}）",
        )
        check(
            state.match_results[2]["product_score"] >= 90,
            f"第 3 行商品名分数 ≥ 90（实际 {state.match_results[2]['product_score']}）",
        )
        print()

        # ── 3. 输出文件验证 ──
        print("3. 输出文件验证")
        check(os.path.exists(output_path), "输出 Excel 文件已生成")
        report_path = output_path.replace(".xlsx", "_report.txt")
        check(os.path.exists(report_path), "校验报告已生成")

        # 读取输出 Excel 验证内容
        df_out = pd.read_excel(output_path)
        check("配料" in df_out.columns, "输出包含 '配料' 列")
        check("匹配置信度" in df_out.columns, "输出包含 '匹配置信度' 列")
        check(
            df_out.iloc[0]["配料"] == "T240、B30/80、S4",
            f"第 1 行 SOP 正确（实际 {df_out.iloc[0]['配料']}）",
        )

        # 读取报告验证
        report_text = open(report_path, encoding="utf-8").read()
        check("本次映射完成" in report_text, "报告包含标题")
        print()

        # ── 4. 多候选精确匹配 ──
        print("4. 多候选精确属性选择")
        tc_reset_cache()

        # 同产品名三个主数据行（不同属性），应精确选择对的属性
        pd.DataFrame({
            "品名": ["测试茶", "测试茶", "测试茶"],
            "杯型": ["大杯", "中杯", "小杯"],
            "奶底": ["牛奶", "燕麦奶", ""],
            "做法": ["正常冰", "少冰", "去冰"],
            "糖": ["全糖", "七分糖", "三分糖"],
            "SOP": ["SOP-A", "SOP-B", "SOP-C"],
        }).to_excel(master_path, index=False)

        pd.DataFrame({
            "菜品名称": ["测试茶"],
            "规格": ["中杯"],
            "口味做法组合": ["燕麦奶, 少冰, 七分糖"],
            "配料": [""],
        }).to_excel(template_path, index=False)

        state2 = run_pipeline(master_path, template_path, output_path)
        check(not state2.has_error, "二次运行无错误")
        check(len(state2.match_results) == 1, "1 条匹配")
        check(
            state2.match_results[0]["sop"] == "SOP-B",
            f"选中 SOP-B（中杯/燕麦奶/少冰/七分糖）（实际 {state2.match_results[0]['sop']}）",
        )
        check(state2.match_results[0]["confidence"] == "HIGH", "置信度 HIGH")
        print()

        # ── 5. 错误处理：文件不存在 ──
        print("5. 错误处理：文件不存在")
        state_err = run_pipeline(
            "不存在的文件.xlsx", template_path, output_path
        )
        check(state_err.has_error, "文件不存在 → has_error=True")
        check(state_err.error_step == "load_data", "错误发生在 load_data 步骤")
        print()

        # ── 6. 报告内容验证 ──
        print("6. 报告内容")
        check(
            "高置信匹配" in state.report,
            "报告包含高置信匹配统计",
        )
        check(
            "需要确认" in state.report or "高置信匹配" in state.report,
            "报告包含置信度分级",
        )
        print()

        # ── 7. 无匹配商品 → LOW_CONFIDENCE ──
        print("7. 无匹配商品 → LOW_CONFIDENCE")
        tc_reset_cache()
        pd.DataFrame({
            "品名": ["产品A"],
            "杯型": ["中杯"],
            "奶底": [""],
            "做法": ["正常冰"],
            "糖": ["标准糖"],
            "SOP": ["SOP-X"],
        }).to_excel(master_path, index=False)

        pd.DataFrame({
            "菜品名称": ["完全不存在的商品"],
            "规格": ["中杯"],
            "口味做法组合": ["正常冰, 标准糖"],
            "配料": [""],
        }).to_excel(template_path, index=False)

        state3 = run_pipeline(master_path, template_path, output_path)
        check(not state3.has_error, "无错误")
        check(len(state3.match_results) == 1, "1 条结果")
        check(
            state3.match_results[0]["confidence"] == "LOW_CONFIDENCE",
            f"无匹配 → LOW_CONFIDENCE（实际 {state3.match_results[0]['confidence']}）",
        )
        check(
            state3.match_results[0]["match_type"] == "best_guess",
            f"匹配类型 best_guess（实际 {state3.match_results[0]['match_type']}）",
        )
        print()

        # ── 8. LangGraph 路径和顺序路径结果一致 ──
        print("8. LangGraph 路径和顺序路径结果一致")
        # 重新写入最初的标准测试数据
        pd.DataFrame({
            "品名": ["浅浅清茶", "浅浅清茶", "黑糖波波牛乳", "珍珠奶茶"],
            "杯型": ["中杯", "中杯", "大杯", "中杯"],
            "奶底": ["牛奶", "牛奶", "", "椰乳"],
            "做法": ["少冰", "去冰", "正常冰", "热"],
            "糖": ["七分糖", "标准糖", "标准糖", "无糖"],
            "SOP": [
                "T240、B30/80、S4",
                "T265、B30/105、S5",
                "T200、B50/100、S5",
                "T180、B40/80、S2",
            ],
        }).to_excel(master_path, index=False)

        pd.DataFrame({
            "菜品名称": ["浅浅清茶", "浅浅清茶", "黑糖波波牛乳"],
            "规格": ["中杯", "中杯", "大杯"],
            "口味做法组合": [
                "牛奶, 少冰, 七分糖",
                "牛奶, 去冰, 标准糖",
                "正常冰, 标准糖",
            ],
            "配料": ["", "", ""],
        }).to_excel(template_path, index=False)
        tc_reset_cache()

        # 顺序执行
        state_seq = run_pipeline(master_path, template_path, output_path, use_langgraph=False)
        check(not state_seq.has_error, f"顺序执行无错误（错误: {state_seq.error}）")
        seq_sops = [r.get("sop", "") for r in state_seq.match_results]
        seq_confs = [r.get("confidence", "") for r in state_seq.match_results]

        # LangGraph 执行
        state_lg = run_pipeline(master_path, template_path, output_path, use_langgraph=True)
        check(not state_lg.has_error, f"LangGraph 执行无错误（错误: {state_lg.error}）")

        if not state_lg.has_error:
            lg_sops = [r.get("sop", "") for r in state_lg.match_results]
            lg_confs = [r.get("confidence", "") for r in state_lg.match_results]
            check(seq_sops == lg_sops, f"SOP 结果一致（顺序={len(seq_sops)}, LG={len(lg_sops)}）")
            check(seq_confs == lg_confs, f"置信度结果一致")
            check(
                len(state_seq.match_results) == len(state_lg.match_results),
                f"匹配行数一致（{len(state_seq.match_results)}）",
            )
        print()

    finally:
        # 清理临时文件
        for f in [master_path, template_path, output_path,
                  output_path.replace(".xlsx", "_report.txt")]:
            if os.path.exists(f):
                os.remove(f)
        os.rmdir(tmpdir)

    # 还原 Mock 设置
    cfg.MOCK_SCHEMA_RESPONSE = original_mock_schema
    tc_reset_cache()

    print(f"=== 结果: {passed} passed, {failed} failed ===")
