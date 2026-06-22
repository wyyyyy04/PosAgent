"""
LLM Tool 注册表 — 将规则函数注册为 LLM 可调用的 Tool。

每个 Tool 包含 name / description / parameters(JSON Schema) / handler。
Handler 函数均为我们的规则管线，不经过 LLM 生成，防止幻觉。

未来 LangGraph create_react_agent 可直接消费 TOOLS 列表。
"""

from typing import Any, Callable, Dict, List

# ── 类型定义 ──────────────────────────────────────────────────────

ToolDef = Dict[str, Any]  # {name, description, parameters, handler, category}

TOOLS: List[ToolDef] = []


def register(
    name: str,
    description: str,
    parameters: Dict[str, Any],
    category: str = "pipeline",
) -> Callable:
    """装饰器：将一个函数注册为 LLM-callable Tool。

    Args:
        name: Tool 名称（LLM 通过此名称调用）。
        description: 自然语言描述（LLM 据此判断何时使用）。
        parameters: JSON Schema 格式的参数定义。
        category: "pipeline"（核心管线，禁止 LLM 自行实现）
                  或 "supplementary"（辅助操作，LLM 可生成代码）。

    Returns:
        装饰器函数。
    """
    def decorator(handler: Callable) -> Callable:
        TOOLS.append({
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": parameters,
                "required": [
                    k for k, v in parameters.items()
                    if v.get("required", False)
                ],
            },
            "handler": handler,
            "category": category,
        })
        return handler
    return decorator


def get_tools_for_langgraph() -> List[Dict[str, Any]]:
    """返回 LangGraph create_react_agent 兼容的 Tool 列表。

    LangGraph 期望每个 tool 是一个可调用对象（函数），
    因此返回 handler 函数列表。
    """
    return [t["handler"] for t in TOOLS]


def get_tool_by_name(name: str) -> ToolDef:
    """按名称查找 Tool。"""
    for t in TOOLS:
        if t["name"] == name:
            return t
    raise KeyError(f"Tool 不存在: {name}")


# ═══════════════════════════════════════════════════════════════════
# Tool 注册
# ═══════════════════════════════════════════════════════════════════


@register(
    name="run_sop_matching",
    description=(
        "执行 SOP 匹配管线——将主数据表的 SOP 代码映射填充到 POS 模板。"
        "调用前通过 ask_user 一次性展示完整列映射方案让用户整体确认，不要逐列询问。"
        "Schema Analyzer 会自动识别列映射，直接展示结果请用户确认即可。"
        "用户说 --sheet N → 传 template_sheet=N"
    ),
    parameters={
        "master_path": {"type": "string", "description": "主数据表路径"},
        "template_path": {"type": "string", "description": "POS 模板路径"},
        "output_path": {"type": "string", "description": "输出路径"},
        "target_col": {"type": "string", "description": "目标填充列，默认「配料」"},
        "template_sheet": {"type": "integer", "description": "模板 Sheet 序号，用户说 --sheet N 时传 N"},
        "master_sheet": {"type": "integer", "description": "主数据 Sheet 序号，默认 0"},
        "column_mapping": {
            "type": "object",
            "description": "仅当 Schema Analyzer 无法识别列名时才需手动传入",
        },
    },
    category="pipeline",
)
def run_sop_matching(
    master_path: str, template_path: str, output_path: str = "",
    target_col: str = "配料", template_sheet: int = 0,
    master_sheet: int = 0, column_mapping: dict = None,
) -> dict:
    """执行 SOP 匹配管线（Agent 调用入口）。"""
    if not output_path:
        output_path = template_path.replace(".xlsx", "_output.xlsx")
    from menupilot.agent.orchestration import run_sop_pipeline_kwargs
    return run_sop_pipeline_kwargs(
        master_path=master_path, template_path=template_path,
        output_path=output_path, target_col=target_col,
        template_sheet=template_sheet, master_sheet=master_sheet,
        column_mapping=column_mapping,
    )


@register(
    name="run_option_expansion",
    description=(
        "将产品选项规格（糖度/温度/规格/奶底/茶底）展开为模板明细行。"
        "当用户说「展开选项」「生成规格表」「选项展开」时必须调用此工具。"
        "禁止自行生成展开逻辑。"
    ),
    parameters={
        "master_path": {
            "type": "string",
            "description": "选项规格主数据表 Excel 路径",
        },
        "template_path": {
            "type": "string",
            "description": "空白选项模板 Excel 路径（含表头）",
        },
        "output_path": {
            "type": "string",
            "description": "输出 Excel 文件路径",
        },
    },
    category="pipeline",
)
def run_option_expansion(
    master_path: str, template_path: str, output_path: str,
) -> dict:
    """执行选项规格展开管线。"""
    from menupilot.agent.orchestration import run_expand_pipeline
    exit_code = run_expand_pipeline([
        "--master", master_path, "--template", template_path, "--output", output_path,
    ])
    return {"ok": exit_code == 0, "output_path": output_path, "exit_code": exit_code}


# ═══════════════════════════════════════════════════════════════════
# ask_user — Agent 规划落地锚点
# ═══════════════════════════════════════════════════════════════════

@register(
    name="ask_user",
    description=(
        "当对列语义、用户意图或操作确认不确定时，必须调用此工具。"
        "不要猜测列的含义、不要假设用户的意图——不确定就问。"
        "典型场景：列名含糊（如「自定义字段A」）、用户指令有歧义、写入前确认。"
    ),
    parameters={
        "question": {"type": "string", "description": "向用户提出的问题"},
    },
    category="interactive",
)
def ask_user(question: str) -> str:
    """向用户提问并获取回答。"""
    return input(f"\nAgent: {question}\n你: ")


# ═══════════════════════════════════════════════════════════════════
# read_excel_info — Schema 分析入口
# ═══════════════════════════════════════════════════════════════════

@register(
    name="read_excel_info",
    description=(
        "读取 Excel 文件结构——列名、行数、每列前3行样例值和空值数量。"
        "在操作任何 Excel 文件前必须先调用此工具了解其结构，禁止猜测列名。"
    ),
    parameters={
        "filepath": {"type": "string", "description": "Excel 文件路径"},
        "sheet_name": {"type": "integer", "description": "Sheet 序号，默认 0"},
    },
    category="schema",
)
def read_excel_info(filepath: str, sheet_name: int = 0) -> dict:
    """读取 Excel 结构信息。"""
    import pandas as pd
    try:
        df = pd.read_excel(filepath, sheet_name=sheet_name)
        return {
            "filepath": filepath,
            "columns": list(df.columns),
            "row_count": len(df),
            "sample_values": {
                str(c): [str(v) for v in df[c].dropna().head(3).tolist()]
                for c in df.columns
            },
            "null_counts": {str(c): int(df[c].isna().sum()) for c in df.columns},
            "dtypes": {str(c): str(df[c].dtype) for c in df.columns},
        }
    except Exception as e:
        return {"error": str(e)}


@register(
    name="execute_python",
    description=(
        "在安全沙箱中执行 Python 代码以查询或修改 Excel 数据。"
        "仅用于辅助操作（增删改查数据、查看文件结构等）。"
        "严格禁止：在此工具中实现匹配/填充/展开逻辑——"
        "这些必须通过 run_sop_matching 或 run_option_expansion 完成。"
        "可用的 Python 库：pandas, openpyxl, numpy, json, csv, re。"
    ),
    parameters={
        "code": {
            "type": "string",
            "description": (
                "要执行的 Python 代码。可使用 pandas 读取 Excel、"
                "openpyxl 操作工作簿。代码中赋值的变量会返回。"
            ),
        },
    },
    category="supplementary",
)
def execute_python(code: str) -> dict:
    """在沙箱中执行 Python 代码。"""
    from menupilot.agent.sandbox import execute as sandbox_execute
    return sandbox_execute(code)


@register(
    name="query_token_dict",
    description=(
        "查询 Token 词典——查看系统中已注册的属性词及其类型。"
        "可用于确认某个值（如「七分糖」）属于哪个维度（糖度/温度/规格等）。"
    ),
    parameters={
        "action": {
            "type": "string",
            "enum": ["lookup", "list_types", "list_values"],
            "description": "操作类型：lookup=查单个词, list_types=列出所有类型, list_values=列出某类型下所有词",
        },
        "value": {
            "type": "string",
            "description": "要查询的词（action=lookup 时必填）",
        },
        "token_type": {
            "type": "string",
            "description": "类型名（action=list_values 时必填，如「糖度」「温度」）",
        },
    },
    category="supplementary",
)
def query_token_dict(
    action: str,
    value: str = "",
    token_type: str = "",
) -> dict:
    """查询 Token 词典。"""
    from menupilot.data.token_dict import lookup, list_types, get_tokens_by_type

    if action == "lookup":
        if not value:
            return {"error": "lookup 需要提供 value 参数"}
        result_type = lookup(value)
        return {"value": value, "type": result_type, "is_known": result_type != "UNKNOWN_TOKEN"}

    if action == "list_types":
        types = list_types()
        return {"types": types}

    if action == "list_values":
        if not token_type:
            return {"error": "list_values 需要提供 token_type 参数"}
        values = get_tokens_by_type(token_type)
        return {"type": token_type, "values": values}

    return {"error": f"未知 action: {action}"}


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

    print("=== Tool Registry 自测 ===\n")

    # ── 1. Tool 注册数量 ──
    print("1. Tool 注册数量")
    check(len(TOOLS) >= 4, f"至少 4 个 Tool 已注册（实际 {len(TOOLS)}）")
    print()

    # ── 2. 每个 Tool 的完整性 ──
    print("2. 每个 Tool 结构完整性")
    required_keys = {"name", "description", "parameters", "handler", "category"}
    for t in TOOLS:
        missing = required_keys - set(t.keys())
        check(not missing, f"{t['name']}: 结构完整")
        check(callable(t["handler"]), f"{t['name']}: handler 可调用")
        check("type" in t["parameters"], f"{t['name']}: parameters 含 type")
        check("properties" in t["parameters"], f"{t['name']}: parameters 含 properties")
    print()

    # ── 3. 分类正确 ──
    print("3. Tool 分类")
    pipelines = [t for t in TOOLS if t["category"] == "pipeline"]
    supplements = [t for t in TOOLS if t["category"] == "supplementary"]
    check(len(pipelines) >= 2, f"pipeline 类 Tool ≥ 2（实际 {len(pipelines)}）")
    check(len(supplements) >= 2, f"supplementary 类 Tool ≥ 2（实际 {len(supplements)}）")
    print()

    # ── 4. get_tool_by_name ──
    print("4. get_tool_by_name 查找")
    t = get_tool_by_name("run_sop_matching")
    check(t["name"] == "run_sop_matching", "找到 run_sop_matching")
    try:
        get_tool_by_name("nonexistent")
        check(False, "不存在的 Tool 应抛 KeyError")
    except KeyError:
        check(True, "不存在的 Tool 正确抛出 KeyError")
    print()

    # ── 5. query_token_dict ──
    print("5. query_token_dict 功能")
    r = query_token_dict("lookup", value="七分糖")
    check(r["type"] == "糖度", f"七分糖 → 糖度（实际 {r['type']}）")
    check(r["is_known"] is True, "is_known=True")

    r2 = query_token_dict("list_types")
    check("糖度" in r2["types"] and "温度" in r2["types"], "list_types 包含糖度和温度")

    r3 = query_token_dict("lookup", value="不存在的词xyz")
    check(r3["is_known"] is False, "未知词 is_known=False")
    check(r3["type"] == "UNKNOWN_TOKEN", "未知词 type=UNKNOWN_TOKEN")
    print()

    # ── 6. get_tools_for_langgraph ──
    print("6. get_tools_for_langgraph")
    lg_tools = get_tools_for_langgraph()
    check(len(lg_tools) == len(TOOLS), f"数量一致（{len(lg_tools)}）")
    check(all(callable(t) for t in lg_tools), "全部可调用")
    print()

    # ── 7. execute_python 可以执行 ──
    print("7. execute_python 调用沙箱")
    r = execute_python("x = 1 + 2")
    check(r["result"]["x"] == 3, f"x = 3（实际 {r['result']}）")
    check("error" not in r, "无错误")
    print()

    # ── 汇总 ──
    print(f"=== 结果: {passed} passed, {failed} failed ===")