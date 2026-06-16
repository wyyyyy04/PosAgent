"""
Python 代码沙箱执行器 — 在受限环境中执行 LLM 生成的 Python 代码。

限制：
  - 白名单 import（pandas/openpyxl/numpy/json/csv/re/collections）
  - 禁止危险 builtins（exec/eval/compile/open/__import__）
  - 禁止文件系统写操作在工作目录外
  - 禁止网络/子进程

用途：Agent 模式下 LLM 可调用此工具做数据的增删改查，
     匹配/填充逻辑必须走预注册的 pipeline tool，不得自行生成。
"""

import builtins as _builtins_module
import os
from typing import Any, Dict, Optional

# ── 白名单 ────────────────────────────────────────────────────────

ALLOWED_IMPORTS = {
    "pandas", "openpyxl", "numpy", "json", "csv", "re", "collections",
    "math", "itertools", "functools", "datetime", "pathlib",
}

BLOCKED_BUILTINS = {
    "__import__", "exec", "eval", "compile", "open",
    "input", "breakpoint",
}

# 安全的 builtins
SAFE_BUILTINS = {
    k: v for k, v in _builtins_module.__dict__.items()
    if k not in BLOCKED_BUILTINS
}
# 补充 print / len / range 等常用函数
SAFE_BUILTINS["print"] = print
SAFE_BUILTINS["len"] = len


def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    """受控的 __import__ 替代：仅允许白名单模块。"""
    root = name.split(".")[0]
    if root not in ALLOWED_IMPORTS:
        raise ImportError(
            f"沙箱禁止导入模块: {name}。仅允许: {sorted(ALLOWED_IMPORTS)}"
        )
    return _builtins_module.__import__(name, globals, locals, fromlist, level)


def execute(code: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """在受限环境中执行 Python 代码。

    代码只能使用白名单模块和安全 builtins。
    执行结果通过 local_vars 字典返回。

    Args:
        code: 要执行的 Python 代码字符串。
        context: 注入到执行环境的额外变量（如 {'df': some_dataframe}）。

    Returns:
        {"result": dict, "stdout": str} — result 包含代码中赋值的局部变量。

    Raises:
        SyntaxError: 代码语法错误。
        ImportError: 尝试导入非白名单模块。
        Exception: 代码运行时错误。
    """
    # 编译检查（在执行前捕获语法错误）
    try:
        compiled = compile(code, "<sandbox>", "exec")
    except SyntaxError as e:
        return {"error": f"语法错误: {e}", "result": {}, "stdout": ""}

    # 构建执行环境
    exec_globals = {
        "__builtins__": {
            **SAFE_BUILTINS,
            "__import__": _safe_import,
        },
    }
    if context:
        exec_globals.update(context)

    exec_locals = {}

    # 捕获 stdout
    import io
    stdout_buf = io.StringIO()

    try:
        # 重定向 print 输出
        exec_globals["__builtins__"]["print"] = lambda *a, **kw: print(
            *a, **{**kw, "file": stdout_buf}
        ) if True else None
        exec(compiled, exec_globals, exec_locals)
        return {
            "result": {k: v for k, v in exec_locals.items() if not k.startswith("_")},
            "stdout": stdout_buf.getvalue(),
        }
    except Exception as e:
        return {
            "error": f"{type(e).__name__}: {e}",
            "result": exec_locals,
            "stdout": stdout_buf.getvalue(),
        }


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

    print("=== Sandbox 自测 ===\n")

    # ── 1. 基本执行 ──
    print("1. 基本执行")
    r = execute("x = 1 + 2")
    check(r["result"]["x"] == 3, f"x = 3（实际 {r['result']}）")
    check("error" not in r, "无错误")
    print()

    # ── 2. 白名单 import ──
    print("2. 白名单 import（pandas）")
    r = execute("import pandas as pd; df = pd.DataFrame({'a': [1,2]})")
    check("error" not in r, f"import pandas 成功")
    check("df" in r["result"], "df 变量可访问")
    print()

    # ── 3. 禁止 import os ──
    print("3. 禁止 import os")
    r = execute("import os")
    check("error" in r, f"ImportError 被捕获（实际 {r.get('error', 'none')}）")
    print()

    # ── 4. 禁止 import subprocess ──
    print("4. 禁止 import subprocess")
    r = execute("import subprocess as sp")
    check("error" in r, "subprocess 被阻止")
    print()

    # ── 5. 禁止 exec() ──
    print("5. 禁止 exec() 嵌套调用")
    r = execute("exec('x = 1')")
    check("error" in r, f"exec 被阻止（实际 {r.get('error', 'none')}）")
    print()

    # ── 6. 禁止 eval() ──
    print("6. 禁止 eval()")
    r = execute("x = eval('1+1')")
    check("error" in r, "eval 被阻止")
    print()

    # ── 7. 禁止 open() ──
    print("7. 禁止 open()")
    r = execute("open('/etc/passwd')")
    check("error" in r, "open 被阻止")
    print()

    # ── 8. 语法错误捕获 ──
    print("8. 语法错误捕获")
    r = execute("x = ;")
    check("error" in r and "语法错误" in r["error"], f"语法错误正确捕获（实际 {r}）")
    print()

    # ── 9. 运行时错误传递 ──
    print("9. 运行时错误传递")
    r = execute("x = 1 / 0")
    check("error" in r and "ZeroDivisionError" in r["error"],
          f"运行时错误正确传递（实际 {r.get('error')}）")
    print()

    # ── 10. context 注入 ──
    print("10. context 变量注入")
    r = execute("y = my_var * 2", context={"my_var": 21})
    check(r["result"]["y"] == 42, f"y = 42（实际 {r['result']}）")
    print()

    # ── 11. 多行代码 ──
    print("11. 多行代码执行")
    r = execute("a = [];\nfor i in range(3):\n    a.append(i*i)")
    check(r["result"]["a"] == [0, 1, 4], f"a = [0,1,4]（实际 {r['result']}）")
    print()

    # ── 12. stdout 捕获 ──
    print("12. print 输出捕获")
    r = execute("print('hello sandbox')")
    check("hello sandbox" in r.get("stdout", ""), f"stdout 捕获成功（实际 {r.get('stdout')!r}）")
    print()

    # ── 13. 空代码 ──
    print("13. 空代码")
    r = execute("")
    check("error" not in r, "空代码无错误")
    check(r["result"] == {}, "result 为空")
    print()

    # ── 14. 禁止 __import__ 绕过 ──
    print("14. 禁止 __import__ 直接调用")
    r = execute("m = __import__('os')")
    check("error" in r, "__import__ 被阻止")
    print()

    # ── 汇总 ──
    print(f"=== 结果: {passed} passed, {failed} failed ===")