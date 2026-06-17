"""
Agent Loop — 自然语言驱动的工具调用循环。

while turn < max_turns:
    response = LLM.chat(messages, tools)
    if no tool_calls: return response
    for each tool_call: execute → append result
"""

import hashlib
import json
import os
from collections import deque
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

# ── 常量 ──────────────────────────────────────────────────────────

MAX_TURNS = 15
DUPLICATE_NOTICE_THRESHOLD = 3  # 同一 tool+args 连续调用 ≥N 次 → 注入提示


def _build_system_prompt(cwd: str = "") -> str:
    """构建 Agent system prompt — 描述判断标准，不描述操作步骤。"""
    from agent.tools import TOOLS

    tool_descriptions = []
    for t in TOOLS:
        params = t.get("parameters", {}).get("properties", {})
        param_str = ", ".join(
            f"{k}: {v.get('type','str')}" for k, v in params.items()
        )
        tool_descriptions.append(f"- **{t['name']}**({param_str}): {t['description']}")

    return f"""你是 PosAgent，一个奶茶/餐饮行业的 POS 模板自动化助手。

## 工具
{chr(10).join(tool_descriptions)}

## 判断标准
- 对列语义不确定时，通过 ask_user 确认，禁止猜测列名
- 用户确认的映射列数量决定管线选择：多列映射→run_sop_matching，单选项展开→run_option_expansion
- 写入文件前必须通过 ask_user 获得用户明确确认
- 工具返回错误时，自行决定：纠正参数重试、询问用户、或报告失败终止
- 奶底/茶底为空是正常的通配行为，不是错误
- execute_python 只能用于数据分析，禁止尝试写入文件

## 领域知识
- 奶茶规格维度: 糖度/温度/规格/奶底/茶底
- SOP 格式: "T240、B30/80、S4" (时间/配方/糖量)
- 主数据常见列名: 品名/杯型/奶底/做法/糖/SOP/主编码/商品名称
- 模板常见列名: 菜品名称/规格/口味做法组合/配料/商品编码/选项名称

## 当前会话
工作目录: {cwd or os.getcwd()}
当前时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}
"""


class AgentLoop:
    """Agent 主循环。

    用法:
        from agent.agent_loop import AgentLoop
        agent = AgentLoop(llm_client)
        result = agent.run("把主数据匹配到模板")
    """

    def __init__(self, llm_client, cwd: str = ""):
        self.llm = llm_client
        self.cwd = cwd or os.getcwd()
        self.recent_calls: deque = deque(maxlen=DUPLICATE_NOTICE_THRESHOLD + 1)

        from agent.tools import TOOLS
        self.tools: Dict[str, dict] = {t["name"]: t for t in TOOLS}
        self.system_prompt = _build_system_prompt(self.cwd)

    def run(self, user_input: str) -> str:
        """执行 Agent loop，返回最终文本回复。"""
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_input},
        ]

        for turn in range(1, MAX_TURNS + 1):
            response = self._call_llm(messages)

            if not response.get("tool_calls"):
                return response.get("content", "")

            for tc in response["tool_calls"]:
                result = self._execute_tool(tc)
                # 构建符合 OpenAI 格式的 assistant message（含 tool_calls）
                tool_call_msg = {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["_name"],
                        "arguments": json.dumps(tc["_parsed_args"], ensure_ascii=False),
                    },
                }
                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [tool_call_msg],
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                })

        return f"已执行 {MAX_TURNS} 轮工具调用，仍未完成任务。请简化需求后重试。"

    def _call_llm(self, messages: list) -> dict:
        """调用 LLM，返回 {content, tool_calls}。"""
        tool_schemas = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["parameters"],
                },
            }
            for t in self.tools.values()
        ]

        try:
            completion = self.llm.chat.completions.create(
                model=self.llm.model,
                messages=messages,
                tools=tool_schemas,
                temperature=0.1,
            )
            msg = completion.choices[0].message
            tool_calls = []
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    try:
                        args = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        args = {}
                    tool_calls.append({
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": json.dumps(args, ensure_ascii=False),
                        },
                    })
                    # 用于内部 dispatch（保留 parsed args）
                    tool_calls[-1]["_parsed_args"] = args
                    tool_calls[-1]["_name"] = tc.function.name
            return {
                "content": msg.content,
                "tool_calls": tool_calls,
            }
        except Exception as e:
            return {"content": f"LLM 调用失败: {e}", "tool_calls": []}

    def _execute_tool(self, tc: dict) -> dict:
        """执行单个 tool call，含守卫逻辑。"""
        name = tc.get("_name", tc.get("name", ""))
        args = tc.get("_parsed_args", tc.get("arguments", {}))

        # 守卫 1: 工具存在性
        tool = self.tools.get(name)
        if not tool:
            return {"error": f"未知工具 '{name}'，可用: {list(self.tools)}"}

        # 守卫 2: 重复检测 — 告知 Agent，让它自己决定
        call_hash = self._hash_call(name, args)
        self.recent_calls.append(call_hash)
        if self._count_recent(call_hash) >= DUPLICATE_NOTICE_THRESHOLD:
            return {
                "notice": (
                    f"'{name}' 已连续调用 {DUPLICATE_NOTICE_THRESHOLD} 次且参数相同。"
                    f"如果这是预期行为请忽略，否则请换一种策略。"
                ),
                "result": None,
            }

        # 守卫 3: 异常包装
        try:
            return tool["handler"](**args)
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}

    def _hash_call(self, name: str, args: dict) -> str:
        raw = json.dumps({"n": name, "a": args}, sort_keys=True, ensure_ascii=False)
        return hashlib.md5(raw.encode()).hexdigest()

    def _count_recent(self, hash_val: str) -> int:
        return sum(1 for h in self.recent_calls if h == hash_val)


# ═══════════════════════════════════════════════════════════════════
# 自测（Mock LLM）
# ═══════════════════════════════════════════════════════════════════

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

    print("=== Agent Loop 自测（Mock LLM）===\n")

    # ── Mock LLM client ──
    class MockLLM:
        def __init__(self):
            self.model = "mock"
            self.chat = self

        class completions:
            class create:
                def __init__(self, **kwargs):
                    pass

    # We need a proper mock. Use unittest.mock-style approach.
    from unittest.mock import MagicMock

    # ── 1. 无工具调用 → 直接返回文本 ──
    print("1. 无工具调用 → 直接返回文本")
    mock_llm = MagicMock()
    mock_llm.model = "mock"
    mock_msg = MagicMock()
    mock_msg.content = "匹配完成，共 870 行，775 行 HIGH。"
    mock_msg.tool_calls = None
    mock_llm.chat.completions.create.return_value = MagicMock(choices=[MagicMock(message=mock_msg)])

    agent = AgentLoop(mock_llm, cwd="/tmp")
    result = agent.run("匹配一下")
    check("775 行 HIGH" in result, f"正确返回文本（实际 {result[:50]}...）")
    print()

    # ── 2. 工具调用 → 执行后继续 → 最终文本 ──
    print("2. 工具调用 → 工具执行 → 继续循环")
    import tempfile, pandas as pd
    tmp = tempfile.mkdtemp()
    test_xlsx = os.path.join(tmp, "test.xlsx")
    pd.DataFrame({"A": [1,2], "B": [3,4], "C": [5,6]}).to_excel(test_xlsx, index=False)

    mock2 = MagicMock()
    mock2.model = "mock"
    msg1 = MagicMock()
    msg1.content = None
    tc1 = MagicMock()
    tc1.id = "call_1"
    tc1.function.name = "read_excel_info"
    tc1.function.arguments = json.dumps({"filepath": test_xlsx})
    msg1.tool_calls = [tc1]
    msg2 = MagicMock()
    msg2.content = "文件 test.xlsx 有 3 列: A, B, C"
    msg2.tool_calls = None
    mock2.chat.completions.create.side_effect = [
        MagicMock(choices=[MagicMock(message=msg1)]),
        MagicMock(choices=[MagicMock(message=msg2)]),
    ]

    agent2 = AgentLoop(mock2, cwd=tmp)
    result2 = agent2.run("看看 test.xlsx")
    check("有 3 列" in result2, f"两轮循环后返回文本（实际 {result2[:50]}...）")
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    print()

    # ── 3. 工具不存在 → 返回错误消息给 LLM，LLM 能从中恢复 ──
    print("3. 幻觉工具名 → 返回错误，Agent 恢复后继续")
    mock3 = MagicMock()
    mock3.model = "mock"
    tc3 = MagicMock()
    tc3.id = "call_x"
    tc3.function.name = "nonexistent_tool"
    tc3.function.arguments = json.dumps({})

    msg3a = MagicMock()
    msg3a.content = None
    msg3a.tool_calls = [tc3]
    msg3b = MagicMock()
    msg3b.content = "我调用了不存在的工具但成功恢复了。"
    msg3b.tool_calls = None

    mock3.chat.completions.create.side_effect = [
        MagicMock(choices=[MagicMock(message=msg3a)]),
        MagicMock(choices=[MagicMock(message=msg3b)]),
    ]

    agent3 = AgentLoop(mock3, cwd="/tmp")
    result3 = agent3.run("test")
    check("恢复" in result3, f"Agent 从幻觉工具中恢复（实际 {result3[:60]}）")
    print()

    # ── 4. 重复检测 ± 3次同调用 → 注入提示 ──
    print("4. 重复检测 — 连续3次同调用注入提示")
    mock4 = MagicMock()
    mock4.model = "mock"
    tc4 = MagicMock()
    tc4.id = "call_r"
    tc4.function.name = "read_excel_info"
    tc4.function.arguments = '{"filepath": "same.xlsx"}'

    def make_msg():
        m = MagicMock()
        m.content = None
        m.tool_calls = [tc4]
        return m

    final_msg = MagicMock()
    final_msg.content = "检测到重复，换策略"
    final_msg.tool_calls = None

    mock4.chat.completions.create.side_effect = [
        MagicMock(choices=[MagicMock(message=make_msg())]),
        MagicMock(choices=[MagicMock(message=make_msg())]),
        MagicMock(choices=[MagicMock(message=make_msg())]),
        MagicMock(choices=[MagicMock(message=final_msg)]),
    ]

    agent4 = AgentLoop(mock4, cwd="/tmp")
    agent4.recent_calls.clear()
    result4 = agent4.run("重复测试")
    check("换策略" in result4, f"Agent 收到提示后换了策略（实际 {result4[:50]}）")
    print()

    # ── 5. 超最大轮次 ──
    print("5. 超最大轮次终止")
    mock5 = MagicMock()
    mock5.model = "mock"
    tc5 = MagicMock()
    tc5.id = "c_loop"
    tc5.function.name = "read_excel_info"
    tc5.function.arguments = '{"filepath": "loop.xlsx"}'
    loop_msg = MagicMock()
    loop_msg.content = None
    loop_msg.tool_calls = [tc5]
    mock5.chat.completions.create.return_value = MagicMock(choices=[MagicMock(message=loop_msg)])

    agent5 = AgentLoop(mock5, cwd="/tmp")
    result5 = agent5.run("无限循环")
    check("已执行" in result5 and "轮工具调用" in result5,
          f"超轮次终止（实际 {result5[:80]}）")
    print()

    # ── 汇总 ──
    print(f"=== 结果: {passed} passed, {failed} failed ===")