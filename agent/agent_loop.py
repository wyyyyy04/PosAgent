"""
Agent Loop — 自然语言驱动的工具调用循环，含会话记忆管理。

while turn < max_turns:
    response = LLM.chat(memory.to_llm_input(), tools)
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
DUPLICATE_NOTICE_THRESHOLD = 3
MAX_MEMORY_TURNS = 20
MAX_MEMORY_TOKENS = 8000


# ═══════════════════════════════════════════════════════════════════
# SessionMemory — 滑动窗口记忆管理
# ═══════════════════════════════════════════════════════════════════

class SessionMemory:
    """滑动窗口消息队列。

    规则：
    - 最多保留 max_turns 轮对话
    - 关键消息（文件路径、确认、列映射）不被驱逐
    - system prompt 永远保留，不参与驱逐
    """

    def __init__(self, max_turns: int = MAX_MEMORY_TURNS, max_tokens: int = MAX_MEMORY_TOKENS):
        self.messages: deque = deque()
        self.max_turns = max_turns
        self.max_tokens = max_tokens
        self.system_prompt: str = ""
        self.task_context: Dict[str, Any] = {}

    def add(self, message: dict):
        self.messages.append(message)
        self._evict()

    def _evict(self):
        while len(self.messages) > self.max_turns * 2:  # *2 因每轮=user+assistant
            oldest = self.messages[0]
            if self._is_critical(oldest):
                break
            self.messages.popleft()

    def _is_critical(self, message: dict) -> bool:
        content = str(message.get("content", ""))
        # 文件路径、确认、列映射 — 这些不能丢
        keywords = [".xlsx", "column_mapping", "yes", "是", "确认", "执行", "output_path",
                     "master_path", "template_path", "run_sop_matching", "run_option_expansion"]
        return any(k in content.lower() for k in keywords)

    def to_llm_input(self) -> list:
        return [
            {"role": "system", "content": self.system_prompt},
            *list(self.messages),
        ]

    def reset_task(self):
        self.messages.clear()
        self.task_context = {}

    @property
    def turn_count(self) -> int:
        return len(self.messages) // 2


def _build_system_prompt(cwd: str = "") -> str:
    from agent.tools import TOOLS

    tool_descriptions = []
    for t in TOOLS:
        params = t.get("parameters", {}).get("properties", {})
        param_str = ", ".join(f"{k}: {v.get('type','str')}" for k, v in params.items())
        tool_descriptions.append(f"- **{t['name']}**({param_str}): {t['description']}")

    return f"""你是 PosAgent，一个奶茶/餐饮行业的 POS 模板自动化助手。

## 工具
{chr(10).join(tool_descriptions)}

## 判断标准
- Schema Analyzer 会自动识别列映射，直接展示结果请用户整体确认即可
- 用户确认的映射列数量决定管线选择：多列映射→run_sop_matching，单选项展开→run_option_expansion
- 工具返回错误时，自行决定：纠正参数重试、询问用户、或报告失败终止
- 奶底/茶底为空是正常的通配行为，不是错误
- execute_python 只能用于数据分析，禁止尝试写入文件

## 交互效率
- 展示信息和确认操作合并为一次 ask_user 调用
- 禁止对同一任务的不同字段分多次 ask_user 确认
- 格式：先展示完整方案，再问"是否执行"
- 用户说 --sheet N → 传 template_sheet=N
- 管线完成后用 execute_python 读取 report_path 指向的 txt 报告
- 以表格展示：商品名 | 不匹配原因 | 行数，表格后给出检查建议

## 领域知识
- 奶茶规格维度: 糖度/温度/规格/奶底/茶底
- SOP 格式: "T240、B30/80、S4" (时间/配方/糖量)
- 主数据常见列名: 品名/杯型/奶底/做法/糖/SOP/主编码/商品名称/代码
- 模板常见列名: 菜品名称/规格/口味做法组合/配料/商品编码/选项名称
- testdata/pos1test.xlsx 的 Sheet 0 是说明，Sheet 1 是数据模板

## 当前会话
工作目录: {cwd or os.getcwd()}
当前时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}
"""


# ═══════════════════════════════════════════════════════════════════
# AgentLoop
# ═══════════════════════════════════════════════════════════════════

class AgentLoop:
    """Agent 主循环，含持久化会话记忆。

    用法:
        agent = AgentLoop(llm_client)
        result = agent.run("匹配 SOPcodemaindata.xlsx 到 pos1test.xlsx")
        result = agent.continue_conversation("yes")  # 继续上一轮会话
    """

    def __init__(self, llm_client, cwd: str = ""):
        self.llm = llm_client
        self.cwd = cwd or os.getcwd()
        self.recent_calls: deque = deque(maxlen=DUPLICATE_NOTICE_THRESHOLD + 1)

        from agent.tools import TOOLS
        self.tools: Dict[str, dict] = {t["name"]: t for t in TOOLS}
        self.memory = SessionMemory()
        self.memory.system_prompt = _build_system_prompt(self.cwd)

    def run(self, user_input: str) -> str:
        """新会话：清空记忆，开始 Agent loop。"""
        self.memory.reset_task()
        self.memory.add({"role": "user", "content": user_input})
        return self._loop()

    def continue_conversation(self, user_input: str) -> str:
        """继续已有会话：追加用户消息，继续 loop。"""
        self.memory.add({"role": "user", "content": user_input})
        return self._loop()

    def _loop(self) -> str:
        """内部循环：LLM ↔ 工具执行。"""
        for turn in range(1, MAX_TURNS + 1):
            response = self._call_llm(self.memory.to_llm_input())

            if not response.get("tool_calls"):
                self.memory.add({"role": "assistant", "content": response.get("content", "")})
                return response.get("content", "")

            for tc in response["tool_calls"]:
                result = self._execute_tool(tc)
                tool_call_msg = {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["_name"],
                        "arguments": json.dumps(tc["_parsed_args"], ensure_ascii=False),
                    },
                }
                self.memory.add({"role": "assistant", "content": None, "tool_calls": [tool_call_msg]})
                self.memory.add({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                })

        return f"已执行 {MAX_TURNS} 轮工具调用，仍未完成任务。请简化需求后重试。"

    def _call_llm(self, messages: list) -> dict:
        tool_schemas = [
            {"type": "function", "function": {
                "name": t["name"], "description": t["description"],
                "parameters": t["parameters"],
            }}
            for t in self.tools.values()
        ]
        try:
            completion = self.llm.chat.completions.create(
                model=self.llm.model, messages=messages,
                tools=tool_schemas, temperature=0.1,
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
                        "id": tc.id, "type": "function",
                        "function": {"name": tc.function.name,
                                     "arguments": json.dumps(args, ensure_ascii=False)},
                        "_parsed_args": args, "_name": tc.function.name,
                    })
            return {"content": msg.content, "tool_calls": tool_calls}
        except Exception as e:
            return {"content": f"LLM 调用失败: {e}", "tool_calls": []}

    def _execute_tool(self, tc: dict) -> dict:
        name = tc.get("_name", tc.get("name", ""))
        args = tc.get("_parsed_args", tc.get("arguments", {}))
        tool = self.tools.get(name)
        if not tool:
            return {"error": f"未知工具 '{name}'，可用: {list(self.tools)}"}
        call_hash = self._hash_call(name, args)
        self.recent_calls.append(call_hash)
        if self._count_recent(call_hash) >= DUPLICATE_NOTICE_THRESHOLD:
            return {
                "notice": f"'{name}' 已连续调用 {DUPLICATE_NOTICE_THRESHOLD} 次且参数相同。"
                          f"如果这是预期行为请忽略，否则请换一种策略。",
                "result": None,
            }
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
# 自测
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

    print("=== Agent Loop 自测 ===\n")

    from unittest.mock import MagicMock

    # ── 1. 新会话 → 返回文本 ──
    print("1. 新会话 run() → 返回文本")
    mock = MagicMock()
    mock.model = "mock"
    m = MagicMock()
    m.content = "匹配完成，870行，783 HIGH。"
    m.tool_calls = None
    mock.chat.completions.create.return_value = MagicMock(choices=[MagicMock(message=m)])
    agent = AgentLoop(mock, cwd="/tmp")
    r = agent.run("匹配")
    check("783 HIGH" in r, f"新会话（实际 {r[:50]}）")
    print()

    # ── 2. 继续会话 → 上下文保持 ──
    print("2. continue_conversation → 上下文保持")
    # Agent 的 memory 里已有上一轮的系统提示和用户输入
    # 模拟 LLM 记得上下文
    m2 = MagicMock()
    m2.content = "好的，执行完成。上次匹配了870行。"
    m2.tool_calls = None
    mock.chat.completions.create.return_value = MagicMock(choices=[MagicMock(message=m2)])
    r2 = agent.continue_conversation("yes")
    check("870" in r2, f"记忆保持（实际 {r2[:50]}）")
    check(len(agent.memory.messages) >= 4, f"消息历史 ≥4 条（实际 {len(agent.memory.messages)}）")
    print()

    # ── 3. 关键消息不被驱逐 ──
    print("3. 关键消息保护 — .xlsx/yes/确认 不丢")
    sm = SessionMemory(max_turns=2)
    sm.add({"role": "user", "content": "用 testdata/SOPcodemaindata.xlsx"})
    sm.add({"role": "assistant", "content": "确认执行？"})
    sm.add({"role": "user", "content": "yes"})
    # 大量填充非关键消息
    for i in range(10):
        sm.add({"role": "tool", "content": f"result {i}"})
    msgs = sm.to_llm_input()
    check(any(".xlsx" in str(m) for m in msgs), "文件路径保留")
    check(any("yes" in str(m) for m in msgs), "确认消息保留")
    print()

    # ── 4. 工具调用 → 多轮循环 → 最终文本 ──
    print("4. 工具调用 → 多轮循环")
    import tempfile, pandas as pd
    tmp = tempfile.mkdtemp()
    test_xlsx = os.path.join(tmp, "test.xlsx")
    pd.DataFrame({"A": [1,2], "B": [3,4]}).to_excel(test_xlsx, index=False)

    mock4 = MagicMock()
    mock4.model = "mock"
    tc = MagicMock()
    tc.id = "c1"; tc.function.name = "read_excel_info"
    tc.function.arguments = json.dumps({"filepath": test_xlsx})
    msg4a = MagicMock(); msg4a.content = None; msg4a.tool_calls = [tc]
    msg4b = MagicMock(); msg4b.content = "文件有2列: A, B"; msg4b.tool_calls = None
    mock4.chat.completions.create.side_effect = [
        MagicMock(choices=[MagicMock(message=msg4a)]),
        MagicMock(choices=[MagicMock(message=msg4b)]),
    ]
    a4 = AgentLoop(mock4, cwd=tmp)
    r4 = a4.run("查看 test.xlsx")
    check("有2列" in r4 or "2 列" in r4 or "A, B" in r4, f"工具调用后返回（实际 {r4[:60]}）")
    import shutil; shutil.rmtree(tmp, ignore_errors=True)
    print()

    # ── 5. reset_task 清空记忆 ──
    print("5. reset_task → 清空记忆")
    agent.memory.reset_task()
    check(len(agent.memory.messages) == 0, f"记忆已清空（实际 {len(agent.memory.messages)}）")
    print()

    # ── 6. 超轮次终止 ──
    print("6. 超轮次终止")
    mock6 = MagicMock(); mock6.model = "mock"
    tc6 = MagicMock(); tc6.id = "loop"; tc6.function.name = "read_excel_info"
    tc6.function.arguments = json.dumps({"filepath": "x.xlsx"})
    lm = MagicMock(); lm.content = None; lm.tool_calls = [tc6]
    mock6.chat.completions.create.return_value = MagicMock(choices=[MagicMock(message=lm)])
    a6 = AgentLoop(mock6, cwd="/tmp")
    r6 = a6.run("test")
    check("已执行" in r6, f"超轮次终止（实际 {r6[:60]}）")
    print()

    print(f"=== 结果: {passed} passed, {failed} failed ===")