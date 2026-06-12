# Bug 分析报告：Memory 缓存无法命中

> 日期：2026-06-12  
> 问题：二次运行时，已确认的未知词仍然触发 LLM 调用，长期记忆（memory.json）未能命中

---

## 1. 问题现象

用户运行 `python main.py -m <主数据> -t <模板> -o <输出>`，Token Classifier 遇到未知词后，LLM 猜测类型、用户确认加入词典。但下次运行同一文件时，同一个词仍然触发 LLM 调用，memory.json 中的记录没有被命中。

---

## 2. 排查路径

### 第 1 层：怀疑 normalize_token 词形不一致

**假设**：写入 memory 时的 key 和查询时的 key 不同（例如写入时经过了 normalize，查询时没经过）。

**排查**：追踪 `token_classifier.py` 中 `_classify_one()` 的完整数据流——

```python
# token_classifier.py:274 — 只 normalize 一次
cleaned = normalize_token(part)

# token_classifier.py:281 — 查询 memory，用的同一个 cleaned
mem_type = mem_get_token_type(cleaned)

# token_classifier.py:306 — 写入 memory，用的也是同一个 cleaned
mem_add_token(cleaned, response["type"])
```

**结论**：整个函数只有一个 `cleaned` 变量，写入和查询用的是同一个值，不存在不一致。

---

### 第 2 层：怀疑 normalize_token 输出不稳定

**假设**：同一个输入在不同时间调用 `normalize_token()` 可能返回不同结果。

**排查**：逐行审查 `token_dict.py:54-129` 的 `normalize_token()` 实现——

- Step 1 精确匹配 → 纯 dict lookup，确定性
- Step 2 子串边界匹配 → 基于 `TOKEN_MAP`（常量）和分隔符集合（常量），确定性
- Step 3 分隔符切割 → 纯字符串操作，确定性
- Step 4 返回原值 → 确定性

**结论**：`normalize_token()` 是纯函数，相同输入永远返回相同输出。

---

### 第 3 层：怀疑 add_token / get_token_type 读写路径不一致

**假设**：`add_token()` 写入磁盘后，`get_token_type()` 可能读不到（缓存、时序问题）。

**排查**：阅读 `data/memory.py` 的核心机制——

```python
# memory.py:59-63 — 惰性加载 + 模块级缓存
_data = None  # 模块导入时为 None

def _load():
    global _data
    if _data is not None:   # ← 缓存命中，直接返回
        return _data
    # 首次访问：读文件
    with open(_MEMORY_PATH, "r") as f:
        _data = json.load(f)
```

```python
# memory.py:127-144 — add_token 同步写缓存 + 写磁盘
def add_token(word, token_type):
    data = _load()                    # 拿缓存（已加载则直接返回）
    data[...][word] = {...}           # 改缓存
    _save()                           # 同步写磁盘（open/json.dump，非异步）
```

编写端到端诊断脚本 `diag_memory_e2e.py` 验证——

| 步骤 | 操作 | 结果 |
|------|------|------|
| 1 | 清理测试词 | PASS |
| 2 | `add_token()` → 检查磁盘 | **立即写入** ✓ |
| 3 | 清除缓存模拟新进程 → `get_token_type()` | **从磁盘命中** ✓ |
| 4 | `_classify_one()` → Step 2 查长期记忆 | **hook 未被调用** ✓ |
| 5 | 同进程二次查询 | **会话缓存命中** ✓ |
| 6 | 再次清除缓存模拟新进程 | **长期记忆命中** ✓ |

**结论**：Memory 模块本身读写机制完全正确。

---

### 第 4 层：找到根因 —— batch hook 的 action 分叉

**关键发现**：用户运行 `python main.py -m ... -t ... -o ...`（CLI 带参数）时，`_batch_mode=True`，会注入一个 hook 替代交互式确认——

```python
# main.py:472-480（原代码）
if _batch_mode:
    tc_set_prompt_hook(
        lambda word, context, llm_suggestion: (
            {"action": "add", "type": llm_suggestion}   # LLM 有猜测 → add → 写入 memory
            if llm_suggestion
            else {"action": "unknown"}                   # LLM 无猜测 → unknown → 不写入！
        )
    )
```

**两条路径对比**：

| | LLM 能猜出 | LLM 猜不出 |
|---|---|---|
| hook 返回值 | `{"action": "add", "type": "茶底"}` | `{"action": "unknown"}` |
| 是否进入 `if action == "add"` | ✅ 是 | ❌ 否 |
| 是否调用 `mem_add_token()` | ✅ 写入 memory.json | ❌ **不写入** |
| 下次运行 | 从 memory.json 命中 | **重新调 LLM** |

**根因确认**：

```
Run 1:  unknown word → LLM → None → batch hook → "unknown" → 不写 memory
Run 2:  unknown word → LLM → None → batch hook → "unknown" → 不写 memory
Run N:  （死循环，每次浪费 1 次 API 调用）
```

---

## 3. 修复方案

### 3.1 核心思路

去掉 batch mode 的自动判定 hook，让所有运行路径都走交互式确认（`prompt_user_for_unknown()`），确保用户确认后必然调用 `mem_add_token()` 写入长期记忆。

### 3.2 具体改动

**`main.py`**：注释掉两处

1. `set_batch_mode(True)` — 不再启用批量模式
2. batch hook 注入 — 不再注入自动判定回调

`_batch_mode` 始终保持默认值 `False`，所有 `if _batch_mode: ... else: ...` 分支自动走交互路径。

### 3.3 附带改动

**`agent/workflow.py`**：Human Review 节点暂停调用（保留代码）

- `build_graph()`: 移除 `interrupt_before=["human_review"]`
- LangGraph 路径: 注释掉 `get_state → run_review → update_state → resume`
- 顺序路径: 注释掉 Human Review 检查 + 交互

原因：用户反馈 Human Review 逐个确认低置信度行体验不佳，当前阶段输出报告即可。

---

## 4. 关键教训

### 4.1 排查方法论

```
现象（缓存不命中）
  → 第 1 层：数据一致性（写入 key == 查询 key？）
    → 通过 ✅
  → 第 2 层：函数确定性（normalize 输出稳定？）
    → 通过 ✅
  → 第 3 层：I/O 正确性（写入磁盘 + 读取磁盘？）
    → 通过 ✅
  → 第 4 层：调用链分支（写入被哪个分支跳过了？）
    → 找到根因 🔴
```

**原则**：从数据流源头逐层往下追，每层用代码证据而非猜测排除可能性。

### 4.2 设计教训

- **hook/回调的返回值协议必须穷尽所有分支**：本 bug 的 hook 有三个隐式分支（add / unknown / skip），但 `unknown` 分支缺少持久化逻辑
- **自动化判定不能替代用户确认**：LLM 猜不出的词 → 标记 UNKNOWN → 下次还是猜不出 → 死循环。用户确认一次 → 写入记忆 → 永久解决
- **batch mode 的价值评估**：如果 batch mode 导致数据丢失（长期记忆断裂）且没有真实的无人值守需求，应该去掉

---

## 5. 验证

全量自测通过：`workflow (51) + main (33) = 84 passed, 0 failed`
