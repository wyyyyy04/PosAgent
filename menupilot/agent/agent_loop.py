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
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from menupilot import config
from menupilot.agent.context_mode import ContextMode, filter_tools
from menupilot.agent.conflict_resolver import FieldBinding, apply_patch
from menupilot.agent.mapping_parser import ParseResult

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


# ═══════════════════════════════════════════════════════════════════
# ProgressTracker — 基于业务语义的卡死检测
# ═══════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ProgressSnapshot:
    """一次工具调用后的进度快照。"""
    stage: str
    confirmed_matches: frozenset
    unresolved_fields: frozenset
    turn: int

    @staticmethod
    def _extract_unresolved(result: dict) -> frozenset:
        """从工具返回值中提取 unresolved 信息。"""
        items = set()
        if not isinstance(result, dict):
            return frozenset()
        err = result.get("error", "")
        if err:
            items.add(str(err)[:80])
        low = result.get("low_conf", 0)
        if low:
            items.add(f"low_conf:{low}")
        if "缺少" in str(err) or "缺失" in str(err):
            items.add("missing_fields")
        return frozenset(items)

    @staticmethod
    def _extract_confirmed(result: dict) -> frozenset:
        """从工具返回值中提取已确认的匹配。"""
        items = set()
        if not isinstance(result, dict):
            return frozenset()
        high = result.get("high_conf", 0)
        if high:
            items.add(f"high_conf:{high}")
        if result.get("ok") is True:
            items.add("ok")
        return frozenset(items)


class ProgressTracker:
    """基于业务语义的进度追踪器，区分「推进中」和「真卡死」。"""

    def __init__(self, patience: int = 3):
        self.history: list[ProgressSnapshot] = []
        self.patience = patience

    def push(self, snap: ProgressSnapshot):
        self.history.append(snap)

    def is_stuck(self) -> bool:
        if len(self.history) < self.patience:
            return False

        recent = self.history[-self.patience:]
        last = recent[-1]

        # 收敛到终态 → 不是 stuck
        if last.stage in ("done", "finalization"):
            return False
        # 初始态（什么都没有）→ 不判断
        if not last.unresolved_fields and not last.confirmed_matches:
            return False

        # unresolved_fields 在缩小 → 有推进，不是 stuck
        fields_shrinking = bool(
            recent[-1].unresolved_fields and
            recent[-1].unresolved_fields < recent[0].unresolved_fields
        )
        if fields_shrinking:
            return False

        # confirmed_matches 单调不减才算推进，回滚/抖动不算
        matches_monotone = all(
            recent[i].confirmed_matches >= recent[i - 1].confirmed_matches
            for i in range(1, len(recent))
        )
        matches_growing = bool(
            recent[-1].confirmed_matches and
            recent[-1].confirmed_matches > recent[0].confirmed_matches
        )

        no_progress = (
            not fields_shrinking
            and not matches_growing
            and not matches_monotone
        )

        # stage 没变 且 业务没推进 → stuck
        return len({r.stage for r in recent}) == 1 and no_progress


def _build_system_prompt(cwd: str = "") -> str:
    from menupilot.agent.tools import TOOLS

    tool_descriptions = []
    for t in TOOLS:
        params = t.get("parameters", {}).get("properties", {})
        param_str = ", ".join(f"{k}: {v.get('type','str')}" for k, v in params.items())
        tool_descriptions.append(f"- **{t['name']}**({param_str}): {t['description']}")

    return f"""你是 MenuPilot，一个奶茶/餐饮行业的 POS 模板自动化助手。

## 工具
{chr(10).join(tool_descriptions)}

## 判断标准
- Schema Analyzer 会自动识别列映射，直接展示结果请用户整体确认即可
- 用户确认的映射列数量决定管线选择：多列映射→run_sop_matching，单选项展开→run_option_expansion
- 奶底/茶底为空是正常的通配行为，不是错误
- execute_python 只能用于数据分析，禁止尝试写入文件

## 错误处理
- 工具返回 fatal:true → 立即停止，把 error 和 hint 直接展示给用户，不要重试
- 工具返回 retryable:false → 换一种方式，不要用相同参数重试
- 工具返回 retryable:true → 可以纠正参数后重试，最多 2 次
- 所有错误必须告知用户具体原因和建议，禁止静默吞掉

## 工具返回 ok:false 时的处理规则（重要）
当 run_sop_matching / run_option_expansion 返回 ok:false 时，先判断：
1. 我能获取到修复所需的信息吗？
   → 如果能（如调整参数、换 sheet），尝试自主修复，最多 1 次
2. 修复需要用户提供数据或决策吗？
   → 立即用 ask_user 把 error 信息展示给用户，说明问题并告知需要什么
禁止：收到 ok:false 后调用 execute_python 自行调试 —— 你应该问用户，不是自己查数据
禁止：在无法获取新信息的情况下，重复读取同一个 Excel 文件超过 2 次

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

        from menupilot.agent.tools import TOOLS
        self.tools: Dict[str, dict] = {t["name"]: t for t in TOOLS}
        self.memory = SessionMemory()
        self.memory.system_prompt = _build_system_prompt(self.cwd)

        # ── Context Mode 状态机 ──
        self.context_mode: ContextMode = ContextMode.NORMAL
        self.pending_mapping: dict[str, FieldBinding] = {}
        self._current_turn: int = 0

    def run(self, user_input: str) -> str:
        """新会话：清空记忆 + 重置 context_mode，开始 Agent loop。"""
        if config.DEBUG:
            print(f"[DEBUG agent] run() — new session, user_input: {user_input[:200]}")
        self.memory.reset_task()
        self.context_mode = ContextMode.NORMAL
        self.pending_mapping = {}
        self.memory.add({"role": "user", "content": user_input})
        return self._loop()

    def continue_conversation(self, user_input: str) -> str:
        """继续已有会话：根据 context_mode 路由到对应处理逻辑。"""
        if config.DEBUG:
            print(f"[DEBUG agent] continue_conversation() — mode={self.context_mode}, input={user_input[:200]}")

        # ── AWAITING_CONFIRMATION：只接受 yes/no ──
        if self.context_mode == ContextMode.AWAITING_CONFIRMATION:
            return self._handle_awaiting_confirmation(user_input)

        # ── MAPPING_BUILDING：直接进 MappingParser，不走通用 LLM ──
        if self.context_mode == ContextMode.MAPPING_BUILDING:
            return self._handle_mapping_building(user_input)

        # ── EXECUTING：只允许执行工具，跳过 Intent Router ──
        if self.context_mode == ContextMode.EXECUTING:
            self.memory.add({"role": "user", "content": user_input})
            return self._loop()

        # ── NORMAL：先检测意图，可能切换 mode ──
        intent = self._detect_intent(user_input)
        if config.DEBUG:
            print(f"[DEBUG agent] Intent detected: {intent}")

        if intent == "PROVIDE_MAPPING_RULE":
            self.context_mode = ContextMode.MAPPING_BUILDING
            return self._handle_mapping_building(user_input)

        if intent == "EXECUTE_TASK":
            self.context_mode = ContextMode.EXECUTING
            self.memory.add({"role": "user", "content": user_input})
            return self._loop()

        # intent == "ASK_QUESTION" 或其他 → 正常 LLM 回复
        self.memory.add({"role": "user", "content": user_input})
        return self._loop()

    # ═════════════════════════════════════════════════════════════
    # Intent Detection + Context Mode Handlers
    # ═════════════════════════════════════════════════════════════

    def _detect_intent(self, user_input: str) -> str:
        """判断用户意图。动词不参与路由，对象类型是主分类。

        Step 1: classify_object_type → FILE / FIELD / UNKNOWN
        Step 2: 同时命中裁决 — FILE 降级为上下文，FIELD 是真正的操作对象
        """
        # Step 0: 快速 yes/no
        stripped = user_input.strip().lower()
        if stripped in ("是", "yes", "y", "执行", "开始", "确认", "ok", "好的", "可以"):
            return "EXECUTE_TASK"

        # Step 1: 对象分类
        schema_keys = self._get_schema_keys()
        has_file = self._is_file_object(user_input)
        has_field = self._is_field_object(user_input, schema_keys)

        if config.DEBUG:
            print(f"[DEBUG agent] Intent: has_file={has_file}, has_field={has_field}, schema_keys={schema_keys}")

        # Step 3: 同时命中裁决
        # FILE 降级为上下文（"在 mainproduct.xlsx 里…"），FIELD 是操作对象
        if has_file and has_field:
            return "PROVIDE_MAPPING_RULE"
        elif has_file:
            return "EXECUTE_TASK"
        elif has_field:
            return "PROVIDE_MAPPING_RULE"
        else:
            return "ASK_QUESTION"  # 对象不明确，交给 LLM 正常回复

    def _is_file_object(self, text: str) -> bool:
        """判断输入是否涉及文件/路径/数据源。"""
        file_indicators = (
            ".xlsx", ".xls", ".csv", "Sheet", "sheet",
            "testdata", "输出", "文件", "路径",
        )
        # 路径特征：盘符:\ 或 \\
        has_path = ":" in text and "\\" in text
        return has_path or any(k in text for k in file_indicators)

    def _is_field_object(self, text: str, schema_keys: frozenset) -> bool:
        """判断输入是否涉及 schema 字段。

        schema_keys 从 pending_mapping 的 key 集合 + 对话中已知的列名动态生成。
        不用写死关键词。
        """
        if not schema_keys:
            return False
        return any(k in text for k in schema_keys)

    def _get_schema_keys(self) -> frozenset:
        """从 pending_mapping + 对话中 LLM 展示的列名动态提取。"""
        keys = set(self.pending_mapping.keys())

        # 从最近的 assistant 消息中提取列名
        # LLM 展示映射方案时用的格式：
        #   | 规格价格 | → | 规格名称 |  或  规格价格 → 规格名称
        #   | **一级商品分类** | **默认值 AUUUU** |
        import re
        recent_text = ""
        for msg in list(self.memory.messages)[-10:]:
            if msg.get("role") == "assistant":
                recent_text += str(msg.get("content", ""))[:3000]

        # 匹配 markdown 表格中的中文 cell（含可选的 * 标记）
        # "| 规格价格 |"、"| *规格名称_1 |"、"| **一级商品分类** |"
        col_names = set(re.findall(
            r'\|\s*\*{0,2}([一-鿿][一-鿿_a-zA-Z0-9\*]{1,20})\*{0,2}\s*\|',
            recent_text
        ))
        # 也匹配 "中文名 → 中文名" 格式
        arrow_names = set(re.findall(
            r'([一-鿿]{2,12})\s*[→]\s*',
            recent_text
        ))
        keys.update(col_names)
        keys.update(arrow_names)
        # 过滤掉非列名的通用词
        generic = {"是", "否", "说明", "注意", "默认值", "必填项", "模板列", "主数据列",
                    "请问", "是否", "确认", "执行", "输出", "路径", "文件"}
        keys.difference_update(generic)

        return frozenset(keys)

    def _handle_mapping_building(self, user_input: str) -> str:
        """MAPPING_BUILDING 模式：用 MappingParser 解析用户映射规则。"""
        self.memory.add({"role": "user", "content": user_input})

        # 调用 MappingParser（专用 LLM prompt）
        from menupilot.agent.mapping_parser import (
            build_parser_messages, parse_llm_response,
        )
        msgs = build_parser_messages(
            self.pending_mapping,
            user_input,
            list(self.memory.messages),
        )

        try:
            completion = self.llm.chat.completions.create(
                model=self.llm.model,
                messages=msgs,
                temperature=0.1,
                max_tokens=1024,
            )
            raw = completion.choices[0].message.content or ""
            result = parse_llm_response(raw)
        except Exception as e:
            result = ParseResult(is_unambiguous=False, patch=None, ambiguity_reason=str(e))

        if config.DEBUG:
            print(f"[DEBUG agent] MappingParser result: is_unambiguous={result.is_unambiguous}")

        if result.is_unambiguous and result.patch:
            # 通过 Conflict Resolver 应用 patch
            new_mapping, conflicts = apply_patch(self.pending_mapping, result.patch)
            self.pending_mapping = new_mapping

            conflict_msgs = []
            for c in conflicts:
                if c["decision"] == "BLOCK":
                    conflict_msgs.append(
                        f"⚠️ 「{c['field']}」已有用户指定值「{c['existing_value']}」，"
                        f"新值「{c['incoming_value']}」优先级较低，已忽略。"
                    )
                elif c["decision"] == "REQUIRE_CONFIRMATION":
                    conflict_msgs.append(
                        f"❓ 「{c['field']}」之前设为「{c['existing_value']}」，"
                        f"现在改为「{c['incoming_value']}」，需要确认。"
                    )

            summary_lines = ["已更新字段映射："]
            for k, v in result.patch.items():
                if k in new_mapping and new_mapping[k] is v:
                    summary_lines.append(f"  ✅ {k} → {v.value}")
            summary = "\n".join(summary_lines)
            if conflict_msgs:
                summary += "\n\n" + "\n".join(conflict_msgs)

            # 进入等待确认
            self.context_mode = ContextMode.AWAITING_CONFIRMATION

            return (
                f"{summary}\n\n"
                f"当前映射配置：\n{self._format_pending_mapping()}\n\n"
                f"是否确认此映射方案？（输入是/否）"
            )

        # is_unambiguous=False
        self.context_mode = ContextMode.AWAITING_CONFIRMATION
        reason = result.ambiguity_reason or "无法唯一确定映射"
        return (
            f"❓ 无法完全确定您的映射意图。\n\n"
            f"原因：{reason}\n\n"
            f"当前映射配置：\n{self._format_pending_mapping()}\n\n"
            f"请补充说明，或输入「是」确认当前方案、输入「否」放弃。"
        )

    def _handle_awaiting_confirmation(self, user_input: str) -> str:
        """AWAITING_CONFIRMATION 模式：只接受 yes/no。"""
        stripped = user_input.strip().lower()
        is_yes = stripped in ("是", "yes", "y", "确认", "ok", "好的", "可以", "执行")
        is_no = stripped in ("否", "no", "n", "取消", "不要", "不对")

        if is_yes:
            EXECUTE_MIN_TURNS = 4
            turns_remaining = MAX_TURNS - self._current_turn
            if turns_remaining < EXECUTE_MIN_TURNS:
                msg = (
                    f"当前已用 {self._current_turn} 轮，剩余 {turns_remaining} 轮"
                    f"不足以完成执行（至少需要 {EXECUTE_MIN_TURNS} 轮）。"
                    f"请重新发起任务。"
                )
                self.memory.add({"role": "assistant", "content": msg})
                return msg
            self.context_mode = ContextMode.EXECUTING
            self.memory.add({"role": "user", "content": "确认执行。当前映射配置已生效，请使用 run_sop_matching 执行。"})
            return self._loop()

        if is_no:
            self.context_mode = ContextMode.MAPPING_BUILDING
            return "已取消确认。请重新描述映射规则，或输入 /exit 退出。"

        # 不是 yes/no → 当作映射规则补充
        self.context_mode = ContextMode.MAPPING_BUILDING
        return self._handle_mapping_building(user_input)

    def _format_pending_mapping(self) -> str:
        """格式化 pending_mapping 为可读文本。"""
        if not self.pending_mapping:
            return "  （暂无映射配置）"
        lines = []
        for k, v in self.pending_mapping.items():
            src_label = {"explicit_user": "用户指定", "llm_inferred": "LLM推断", "default": "默认"}.get(v.source, v.source)
            lines.append(f"  {k} → {v.value}  ({src_label})")
        return "\n".join(lines)

    def _loop(self) -> str:
        """内部循环：LLM ↔ 工具执行。"""
        last_error = None
        last_tool_ok = True        # 上一个工具是否返回 ok（非 False）
        tracker = ProgressTracker(patience=3)

        for turn in range(1, MAX_TURNS + 1):
            self._current_turn = turn
            if config.DEBUG:
                print(f"\n[DEBUG agent] === Turn {turn}/{MAX_TURNS} ===")
                msgs = self.memory.to_llm_input()
                last_user = ""
                for m in reversed(msgs):
                    if m.get("role") == "user":
                        last_user = str(m.get("content", ""))[:200]
                        break
                if last_user:
                    print(f"[DEBUG agent] Last user msg: {last_user}")

            response = self._call_llm(self.memory.to_llm_input())

            if not response.get("tool_calls"):
                self.memory.add({"role": "assistant", "content": response.get("content", "")})
                if config.DEBUG:
                    print(f"[DEBUG agent] LLM returned text (no tool_calls) → loop ends")
                    print(f"[DEBUG agent] Response: {str(response.get('content',''))[:300]}")
                return response.get("content", "")

            for tc in response["tool_calls"]:
                name = tc.get("_name", "")

                # ── 工程兜底 1：ok:false 之后禁止调用 execute_python ──
                if not last_tool_ok and name == "execute_python":
                    fatal_msg = {
                        "error_type": "debug_loop",
                        "error": "上一个操作未成功（ok:false），禁止继续自我调试。请把问题告知用户。",
                        "hint": "用 ask_user 把上一个工具的 error 信息展示给用户，询问如何处理。",
                        "fatal": True,
                    }
                    self.memory.add({"role": "assistant", "content": None,
                                     "tool_calls": [self._make_tool_msg(tc)]})
                    self.memory.add({"role": "tool", "tool_call_id": tc["id"],
                                     "content": json.dumps(fatal_msg, ensure_ascii=False)})
                    return (
                        f"操作无法继续：{fatal_msg['error']}\n\n"
                        f"💡 {fatal_msg['hint']}"
                    )

                # ── 工程兜底 2：ProgressTracker 卡死检测 ──
                if tracker.is_stuck():
                    fatal_msg = {
                        "error_type": "stuck_loop",
                        "error": f"最近 {tracker.patience} 轮无业务推进（stage 未变，confirmed_matches 无增长，unresolved_fields 无缩小），疑似卡死。",
                        "hint": "请用 ask_user 告知用户当前状态，询问下一步操作。",
                        "fatal": True,
                    }
                    self.memory.add({"role": "assistant", "content": None,
                                     "tool_calls": [self._make_tool_msg(tc)]})
                    self.memory.add({"role": "tool", "tool_call_id": tc["id"],
                                     "content": json.dumps(fatal_msg, ensure_ascii=False)})
                    return (
                        f"操作无法继续：{fatal_msg['error']}\n\n"
                        f"💡 {fatal_msg['hint']}"
                    )

                result = self._execute_tool(tc)

                # ── 更新 last_tool_ok ──
                if isinstance(result, dict) and "ok" in result:
                    last_tool_ok = result["ok"] is not False
                else:
                    last_tool_ok = True

                # ── 推送进度快照 ──
                if isinstance(result, dict) and "ok" in result:
                    snapshot = ProgressSnapshot(
                        stage=name,
                        confirmed_matches=ProgressSnapshot._extract_confirmed(result),
                        unresolved_fields=ProgressSnapshot._extract_unresolved(result),
                        turn=turn,
                    )
                    tracker.push(snapshot)

                # 不可恢复错误 → 立即终止，告知用户
                if isinstance(result, dict) and result.get("fatal"):
                    self.memory.add({"role": "assistant", "content": None,
                                     "tool_calls": [self._make_tool_msg(tc)]})
                    self.memory.add({"role": "tool", "tool_call_id": tc["id"],
                                     "content": json.dumps(self._sanitize(result), ensure_ascii=False, default=str)})
                    return (
                        f"操作无法继续：{result.get('error', '')}\n\n"
                        f"💡 {result.get('hint', '')}"
                    )

                last_error = result if isinstance(result, dict) and "error" in result else None

                self.memory.add({"role": "assistant", "content": None,
                                 "tool_calls": [self._make_tool_msg(tc)]})
                self.memory.add({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps(self._sanitize(result), ensure_ascii=False, default=str),
                })

        # 超时 → 带上最后失败原因
        if config.DEBUG:
            print(f"[DEBUG agent] MAX_TURNS ({MAX_TURNS}) reached! Task incomplete.")
            print(f"[DEBUG agent] last_error: {last_error}")
            print(f"[DEBUG agent] Recent calls: {list(self.recent_calls)[-5:]}")
            history_stages = [s.stage for s in tracker.history[-5:]]
            print(f"[DEBUG agent] Recent stages: {history_stages}")

        # 工程兜底 3：MAX_TURNS 耗尽时，从 tracker 推断原因
        if not last_error and tracker.history:
            if tracker.is_stuck():
                last_error = {
                    "error": "进度追踪器判定卡死：最近几轮无业务推进。",
                    "hint": "用命令行模式直接执行 menupilot -m ... -t ... -o ... 可绕过 Agent 交互避免轮次消耗。",
                }

        if last_error:
            return (
                f"已执行 {MAX_TURNS} 轮，仍未能完成任务。\n\n"
                f"最后一次失败：{last_error.get('error', '未知')}\n"
                f"💡 {last_error.get('hint', '请简化需求后重试')}"
            )
        return f"已执行 {MAX_TURNS} 轮工具调用，仍未完成任务。请简化需求后重试。"

    def _make_tool_msg(self, tc: dict) -> dict:
        return {
            "id": tc["id"], "type": "function",
            "function": {
                "name": tc["_name"],
                "arguments": json.dumps(tc["_parsed_args"], ensure_ascii=False),
            },
        }

    def _call_llm(self, messages: list) -> dict:
        # 根据 context_mode 过滤可用工具
        active_tools = filter_tools(self.tools, self.context_mode)
        if config.DEBUG and active_tools != self.tools:
            removed = set(self.tools) - set(active_tools)
            print(f"[DEBUG agent] Tools filtered by mode={self.context_mode}: removed={removed}")
        tool_schemas = [
            {"type": "function", "function": {
                "name": t["name"], "description": t["description"],
                "parameters": t["parameters"],
            }}
            for t in active_tools.values()
        ]
        if config.DEBUG and messages:
            last = messages[-1]
            print(f"[DEBUG agent] Calling LLM, last msg role={last.get('role')}, "
                  f"content={str(last.get('content',''))[:150]}")
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
            if config.DEBUG:
                tc_names = [t["_name"] for t in tool_calls]
                print(f"[DEBUG agent] LLM returned {len(tool_calls)} tool_calls: {tc_names}")
            return {"content": msg.content, "tool_calls": tool_calls}
        except Exception as e:
            if config.DEBUG:
                print(f"[DEBUG agent] LLM call FAILED: {e}")
            return {"content": f"LLM 调用失败: {e}", "tool_calls": []}

    def _execute_tool(self, tc: dict) -> dict:
        name = tc.get("_name", tc.get("name", ""))
        args = tc.get("_parsed_args", tc.get("arguments", {}))
        if config.DEBUG:
            args_str = json.dumps(args, ensure_ascii=False, default=str)[:200]
            print(f"[DEBUG agent] Executing tool: {name}({args_str})")
        tool = self.tools.get(name)
        if not tool:
            return {
                "error_type": "unknown_tool",
                "error": f"未知工具 '{name}'，可用: {list(self.tools)}",
                "hint": "请使用可用工具列表中的工具",
                "retryable": False,
                "fatal": False,  # 让 LLM 有机会修正
            }
        call_hash = self._hash_call(name, args)
        self.recent_calls.append(call_hash)
        if self._count_recent(call_hash) >= DUPLICATE_NOTICE_THRESHOLD:
            result = {
                "error_type": "duplicate_call",
                "notice": f"'{name}' 已连续调用 {DUPLICATE_NOTICE_THRESHOLD} 次且参数相同。"
                          f"如果这是预期行为请忽略，否则请换一种策略。",
                "retryable": True,
            }
            if config.DEBUG:
                print(f"[DEBUG agent] Tool result (duplicate): {json.dumps(result, ensure_ascii=False)[:200]}")
            return result
        try:
            # ── task_context 注入：LLM 遗漏的 sheet_name/master_sheet 等 ──
            if name in ("run_option_expansion", "run_sop_matching"):
                tc = self.memory.task_context
                if "sheet_name" not in args and "master_sheet" in tc:
                    args = dict(args)
                    args["sheet_name"] = tc["master_sheet"]
                if "template_sheet" not in args and "template_sheet" in tc:
                    args = dict(args)
                    args["template_sheet"] = tc["template_sheet"]
            result = tool["handler"](**args)
            # ── 提取 task_context 信息（read_excel_info 结果） ──
            if name == "read_excel_info" and isinstance(result, dict):
                fp = result.get("filepath", "")
                if fp and "sheet_name" in args:
                    # 记住：此文件在本会话中已读取的 sheet
                    pass
                # 如果有明确的 sheet 分配，写入 task_context
            if config.DEBUG:
                rstr = json.dumps(self._sanitize(result), ensure_ascii=False, default=str)[:300]
                print(f"[DEBUG agent] Tool result: {rstr}")
            return result
        except PermissionError as e:
            result = {
                "error_type": "file_locked",
                "error": f"文件被占用，无法写入: {e}",
                "hint": "请关闭 Excel 中打开的输出文件后重试，或换一个输出文件名",
                "retryable": False,
                "fatal": True,
            }
            if config.DEBUG:
                print(f"[DEBUG agent] Tool result (fatal): {result.get('error')}")
            return result
        except FileNotFoundError as e:
            result = {
                "error_type": "file_not_found",
                "error": f"文件不存在: {e}",
                "hint": "请检查文件路径是否正确，文件是否已被移动或删除",
                "retryable": False,
                "fatal": True,
            }
            if config.DEBUG:
                print(f"[DEBUG agent] Tool result (fatal): {result.get('error')}")
            return result
        except Exception as e:
            result = {
                "error_type": type(e).__name__,
                "error": f"{type(e).__name__}: {e}",
                "hint": "请将此错误展示给用户，询问是否需要帮助排查",
                "retryable": True,
                "fatal": False,
            }
            if config.DEBUG:
                print(f"[DEBUG agent] Tool result (exception): {result.get('error')}")
            return result

    @staticmethod
    def _sanitize(obj):
        """递归转换非字符串 key 为字符串，确保 json.dumps 可用。"""
        if isinstance(obj, dict):
            return {
                k if isinstance(k, (str, int, float, bool, type(None))) else str(k):
                AgentLoop._sanitize(v) for k, v in obj.items()
            }
        if isinstance(obj, (list, tuple, set)):
            return [AgentLoop._sanitize(v) for v in obj]
        return obj

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