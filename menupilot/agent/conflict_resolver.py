"""
Conflict Resolver — 基于来源优先级的覆盖决策。

不比较值本身，只看来源优先级。
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, IntEnum
from typing import Literal


# ── 来源优先级 ──

class SourcePriority(IntEnum):
    default = 1
    llm_inferred = 2
    explicit_user = 3


SOURCE_PRIORITY = {
    "explicit_user": 3,
    "llm_inferred":  2,
    "default":       1,
}


# ── 覆盖决策 ──

class OverrideDecision(str, Enum):
    SILENT_OVERRIDE = "SILENT_OVERRIDE"         # 高覆盖低，静默生效
    REQUIRE_CONFIRMATION = "REQUIRE_CONFIRMATION"  # 同级覆盖同级，要求确认
    BLOCK = "BLOCK"                              # 低覆盖高，直接拦截


# ── 数据结构 ──

@dataclass
class FieldBinding:
    value: str
    source: Literal["explicit_user", "llm_inferred", "default"]
    confidence_type: Literal["deterministic", "ambiguous"]
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


# ── 核心逻辑 ──

def can_override(existing: FieldBinding | None, incoming: FieldBinding) -> OverrideDecision:
    """判断 incoming 是否可以覆盖 existing。

    如果 existing 为 None（新字段），返回 SILENT_OVERRIDE。
    """
    if existing is None:
        return OverrideDecision.SILENT_OVERRIDE

    existing_p = SOURCE_PRIORITY.get(existing.source, 0)
    incoming_p = SOURCE_PRIORITY.get(incoming.source, 0)

    if incoming_p > existing_p:
        return OverrideDecision.SILENT_OVERRIDE
    elif incoming_p == existing_p:
        return OverrideDecision.REQUIRE_CONFIRMATION
    else:
        return OverrideDecision.BLOCK


def apply_patch(
    current: dict[str, FieldBinding],
    patch: dict[str, FieldBinding],
) -> tuple[dict[str, FieldBinding], list[dict]]:
    """将 patch 应用到 current mapping，返回 (new_mapping, conflicts)。

    conflicts 列表每项为 {field, existing_value, incoming_value, decision}。
    """
    conflicts = []
    result = dict(current)

    for field, incoming in patch.items():
        existing = result.get(field)
        decision = can_override(existing, incoming)

        if decision == OverrideDecision.SILENT_OVERRIDE:
            result[field] = incoming
        elif decision == OverrideDecision.REQUIRE_CONFIRMATION:
            conflicts.append({
                "field": field,
                "existing_value": existing.value if existing else None,
                "incoming_value": incoming.value,
                "decision": decision,
            })
            result[field] = incoming  # 应用但标记需确认
        else:
            conflicts.append({
                "field": field,
                "existing_value": existing.value if existing else None,
                "incoming_value": incoming.value,
                "decision": decision,
            })
            # BLOCK：不应用

    return result, conflicts