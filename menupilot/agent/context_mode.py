"""
Context Mode — 上下文锁，约束每个阶段允许的操作。

硬性约束（不是给 LLM 的提示）：
- NORMAL:                所有工具可用，Intent Router 判断是否切换 mode
- MAPPING_BUILDING:      仅 mapping 相关工具，跳过 Intent Router
- AWAITING_CONFIRMATION: 禁止一切工具调用，只接受 yes/no
- EXECUTING:             仅执行工具，跳过 Intent Router
"""

from enum import Enum


class ContextMode(str, Enum):
    NORMAL = "NORMAL"
    MAPPING_BUILDING = "MAPPING_BUILDING"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
    EXECUTING = "EXECUTING"


# ── 工具权限表 ──

# 只在 MAPPING_BUILDING 阶段允许的工具
MAPPING_TOOLS = {
    "ask_user",
    "read_excel_info",
    "query_token_dict",
}

# 只在 EXECUTING 阶段允许的工具
EXECUTE_TOOLS = {
    "run_sop_matching",
    "run_option_expansion",
    "ask_user",
}

CONTEXT_MODE_TOOL_PERMISSIONS = {
    ContextMode.NORMAL:                None,          # None = 所有工具
    ContextMode.MAPPING_BUILDING:      MAPPING_TOOLS,
    ContextMode.AWAITING_CONFIRMATION: set(),         # 禁止一切工具调用
    ContextMode.EXECUTING:             EXECUTE_TOOLS,
}


def allowed_tools(context_mode: ContextMode) -> set[str] | None:
    """返回当前 context_mode 下允许的工具名集合，None 表示不限制。"""
    return CONTEXT_MODE_TOOL_PERMISSIONS.get(context_mode, None)


def filter_tools(tools: dict, context_mode: ContextMode) -> dict:
    """根据 context_mode 过滤工具列表。"""
    allowed = CONTEXT_MODE_TOOL_PERMISSIONS.get(context_mode, None)
    if allowed is None:
        return tools
    if not allowed:
        return {}  # AWAITING_CONFIRMATION：空工具集
    return {name: t for name, t in tools.items() if name in allowed}