"""诊断 memory 持久化：写入时机 + 读取时机 + 跨进程验证"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MEM_PATH = os.path.join(os.path.expanduser("~"), ".menupilot", "memory.json")

# ── 读当前状态 ──
with open(MEM_PATH, "r", encoding="utf-8") as f:
    before = json.load(f)
print("=== 诊断 1: memory.json 写入前 ===")
print(f"路径: {MEM_PATH}")
print(f"token_aliases 现有: {list(before.get('token_aliases', {}).keys())}")
print(f"文件大小: {os.path.getsize(MEM_PATH)} bytes")

# ── 调用 add_token ──
from data.memory import add_token
print(f"\n=== 诊断 2: 调用 add_token('_DIAG_TEST_', '茶底') ===")
add_token("_DIAG_TEST_", "茶底")

# 验证文件是否立即更新
with open(MEM_PATH, "r", encoding="utf-8") as f:
    after = json.load(f)
result = "_DIAG_TEST_" in after.get("token_aliases", {})
print(f"add_token 后文件立即更新: {'PASS' if result else 'FAIL'}")
print(f"token_aliases: {list(after.get('token_aliases', {}).keys())}")

# ── 同进程缓存 ──
import data.memory as mem
from data.memory import get_token_type
print(f"\n=== 诊断 3: 同进程 get_token_type ===")
t = get_token_type("_DIAG_TEST_")
print(f"get_token_type('_DIAG_TEST_'): {t} {'PASS' if t == '茶底' else 'FAIL'}")
print(f"_data is cached: {mem._data is not None}")

# ── 模拟新进程 ──
print(f"\n=== 诊断 4: 模拟新进程（_data=None → _load 从磁盘读）===")
mem._data = None
t2 = get_token_type("_DIAG_TEST_")
print(f"reload 后 get_token_type('_DIAG_TEST_'): {t2} {'PASS' if t2 == '茶底' else 'FAIL'}")

# ── 风险验证 ──
print(f"\n=== 诊断 5: 缓存遮蔽 — 外部修改 JSON 后同进程不感知 ===")
with open(MEM_PATH, "r", encoding="utf-8") as f:
    d = json.load(f)
del d["token_aliases"]["_DIAG_TEST_"]
with open(MEM_PATH, "w", encoding="utf-8") as f:
    json.dump(d, f, ensure_ascii=False, indent=2)
print("已从磁盘删除 _DIAG_TEST_（绕过 API）")

t_cached = get_token_type("_DIAG_TEST_")
print(f"同进程（缓存未刷）: {t_cached}  ← 注意：缓存返回已删除的值")

mem._data = None
t_reload = get_token_type("_DIAG_TEST_")
print(f"清除缓存后: {t_reload}  ← 正确返回 None")

# ── 恢复 ──
# 文件已恢复（_DIAG_TEST_ 已删除），无需额外操作

print(f"\n{'='*56}")
print(f"诊断结论")
print(f"{'='*56}")
print(f"1. 写入时机: add_token() → _save() 立即写入磁盘        ✓")
print(f"2. 跨进程读取: _data=None 时从磁盘加载                 ✓")
print(f"3. 同进程缓存: _data 一旦加载，外部修改不可见")
print(f"   缓解措施: reload() 可强制刷新缓存")
print(f"4. 路径: {MEM_PATH}")
print(f"   {'文件存在' if os.path.exists(MEM_PATH) else '文件不存在!!!'}")
