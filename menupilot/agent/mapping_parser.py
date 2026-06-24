"""
MappingParser — 判断用户到底改了什么。

依赖 LLM，输入用户原文 + 当前 pending_mapping + 近几轮历史。
输出 ParseResult：是否唯一可解析 + patch 或 ambiguity_reason。
"""

import json
from dataclasses import dataclass, field
from typing import Literal

from menupilot.agent.conflict_resolver import FieldBinding


@dataclass
class ParseResult:
    is_unambiguous: bool
    patch: dict[str, FieldBinding] | None  # is_unambiguous=True 时有值
    ambiguity_reason: str | None           # is_unambiguous=False 时有值


MAPPING_PARSER_PROMPT = """你是一个字段映射解析器。用户在配置一张表到另一张表的列映射关系。

你需要判断用户的输入是否可以唯一解析为一组 key→value 的列映射修改。

## 输入格式
你会收到：
- 当前的 pending_mapping（已生效的字段映射）
- 用户的最新输入
- 最近的对话历史

## 输出要求
判断用户意图是否可以唯一解析为一组 key→value 修改。

如果可以唯一解析，输出：
```json
{
  "is_unambiguous": true,
  "patch": {
    "字段名A": {"value": "映射到模板列X", "source": "explicit_user"},
    "字段名B": {"value": "默认值AUUUU", "source": "explicit_user"}
  }
}
```

如果不可以唯一解析，输出：
```json
{
  "is_unambiguous": false,
  "ambiguity_reason": "无法确定「规格」是指主数据的哪个列：有「规格价格」和「推荐规格」两个候选"
}
```

## source 取值
- "explicit_user": 用户明确说出的映射（如「一级分类默认为AUUUU」）
- "llm_inferred": 你根据上下文推断的映射
- "default": 模板默认映射

## 规则
- 不允许输出猜测。如果不确定，输出 is_unambiguous=false。
- patch 中的 key 是用户原文中提到的列名（中文原名），value 是映射目标。
- 注意区分"这个列映射到那个列"和"我想看看这个文件"——后者不是映射。
"""


def build_parser_messages(
    pending_mapping: dict[str, FieldBinding],
    user_input: str,
    recent_history: list[dict],
) -> list[dict]:
    """构建 MappingParser 的 messages。"""
    mapping_desc = {}
    for k, v in pending_mapping.items():
        mapping_desc[k] = {"value": v.value, "source": v.source}

    # 只取最近 5 轮对话（user + assistant），避免上下文过长
    recent = []
    count = 0
    for msg in reversed(recent_history):
        if msg.get("role") in ("user", "assistant"):
            recent.insert(0, {"role": msg["role"], "content": str(msg.get("content", ""))[:500]})
            count += 1
            if count >= 10:
                break

    context = json.dumps({
        "pending_mapping": mapping_desc,
        "user_input": user_input,
    }, ensure_ascii=False, indent=2)

    return [
        {"role": "system", "content": MAPPING_PARSER_PROMPT},
        *recent,
        {"role": "user", "content": context},
    ]


def parse_llm_response(raw: str) -> ParseResult:
    """解析 LLM 返回的 JSON，提取 ParseResult。"""
    try:
        # 提取 JSON 块
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start == -1 or end <= start:
            return ParseResult(
                is_unambiguous=False,
                patch=None,
                ambiguity_reason=f"LLM 未返回有效 JSON: {raw[:200]}",
            )
        data = json.loads(raw[start:end])

        if not data.get("is_unambiguous"):
            return ParseResult(
                is_unambiguous=False,
                patch=None,
                ambiguity_reason=data.get("ambiguity_reason", "未给出原因"),
            )

        patch = {}
        for key, val in data.get("patch", {}).items():
            patch[key] = FieldBinding(
                value=str(val.get("value", "")),
                source=val.get("source", "llm_inferred"),
                confidence_type="deterministic",
            )
        return ParseResult(is_unambiguous=True, patch=patch, ambiguity_reason=None)

    except (json.JSONDecodeError, KeyError, TypeError) as e:
        return ParseResult(
            is_unambiguous=False,
            patch=None,
            ambiguity_reason=f"解析失败: {e}",
        )