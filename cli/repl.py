"""
交互式 REPL — /指令系统，用于查看和编辑长期记忆。

从 main.py 无参数启动时进入此模式。
提示符: pos-agent>
"""

import os
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple

# ── 类型名称映射 ──────────────────────────────────────────────────

# 内部中文类型名 ↔ 命令行英文类型名
_TYPE_EN_TO_CN: Dict[str, str] = {
    "tea_base": "茶底",
    "milk_base": "奶底",
    "temperature": "温度",
    "sugar": "糖度",
    "size": "规格",
}

_TYPE_CN_TO_EN: Dict[str, str] = {v: k for k, v in _TYPE_EN_TO_CN.items()}

_VALID_TYPES_EN = list(_TYPE_EN_TO_CN.keys())
_VALID_TYPES_CN = list(_TYPE_CN_TO_EN.keys())


def _resolve_type(raw: str) -> Optional[str]:
    """将用户输入的类型名转换为内部中文类型名。

    支持中英文：tea_base→茶底，茶底→茶底。
    返回 None 表示非法类型。
    """
    if raw in _TYPE_CN_TO_EN:
        return raw  # 已是中文
    if raw in _TYPE_EN_TO_CN:
        return _TYPE_EN_TO_CN[raw]
    return None


# ── 命令处理器 ────────────────────────────────────────────────────

# 每个处理器签名为: handler(args: List[str]) -> str
# 对于需要确认的操作，返回以 "[CONFIRM]" 开头的字符串，主循环会跟进确认提示。


def _cmd_help(_args: List[str]) -> str:
    """显示所有可用指令。"""
    return """
══════════════════════════════════════════════════
  POS Agent /指令系统
══════════════════════════════════════════════════

  记忆管理 (/memory):
    /memory list              列出所有 token 别名
    /memory add <词语> <类型>   添加 token 别名
    /memory edit <词语> <类型>  修改已有 token 别名类型
    /memory delete <词语>       删除 token 别名
    /memory reset              清空所有长期记忆

  模板管理 (/template):
    /template list             列出已缓存的模板
    /template show <指纹前N位>   查看模板字段映射配置
    /template clear <指纹前N位>  删除指定模板缓存

  映射任务 (/run):
    /run -m <主数据表> -t <模板> -o <输出> [--target-col <列名>] [-r <报告>]

  通用:
    /help                      显示此帮助信息
    /exit                      退出 REPL

  类型合法值:
    tea_base / milk_base / temperature / sugar / size
══════════════════════════════════════════════════"""


def _cmd_memory_list(_args: List[str]) -> str:
    """列出所有 token 别名。"""
    from data.memory import list_aliases

    aliases = list_aliases()
    if not aliases:
        return "(暂无 token 别名记录)"

    lines = ["  词语              类型        添加时间", "  " + "-" * 48]
    for word, info in sorted(aliases.items()):
        t = info.get("type", "?")
        added = info.get("added", "?")
        # 对齐：中文按 2 字符宽度算
        word_pad = word.ljust(18)
        type_pad = t.ljust(10)
        lines.append(f"  {word_pad}{type_pad}{added}")
    return "\n".join(lines)


def _cmd_memory_add(args: List[str]) -> str:
    """添加 token 别名：/memory add <词语> <类型>"""
    if len(args) < 2:
        return "用法: /memory add <词语> <类型>\n类型合法值: " + ", ".join(_VALID_TYPES_EN)

    word = args[0]
    raw_type = args[1]
    cn_type = _resolve_type(raw_type)

    if cn_type is None:
        return (
            f"非法类型「{raw_type}」\n"
            f"合法值: {', '.join(_VALID_TYPES_EN)}\n"
            f"中文别名: {', '.join(_VALID_TYPES_CN)}"
        )

    from data.memory import add_token

    add_token(word, cn_type)
    en_type = _TYPE_CN_TO_EN.get(cn_type, cn_type)
    return f"已添加: 「{word}」→ {cn_type} ({en_type})"


def _cmd_memory_edit(args: List[str]) -> str:
    """编辑 token 别名类型：/memory edit <词语> <新类型>"""
    if len(args) < 2:
        return "用法: /memory edit <词语> <新类型>\n类型合法值: " + ", ".join(_VALID_TYPES_EN)

    word = args[0]
    raw_type = args[1]
    cn_type = _resolve_type(raw_type)

    if cn_type is None:
        return (
            f"非法类型「{raw_type}」\n"
            f"合法值: {', '.join(_VALID_TYPES_EN)}\n"
            f"中文别名: {', '.join(_VALID_TYPES_CN)}"
        )

    from data.memory import edit_token, get_token_type

    old_type = get_token_type(word)
    if old_type is None:
        return f"词条「{word}」不存在，无法编辑。请先用 /memory add 添加"

    ok = edit_token(word, cn_type)
    if ok:
        en_type = _TYPE_CN_TO_EN.get(cn_type, cn_type)
        return f"已修改: 「{word}」{old_type} → {cn_type} ({en_type})"
    return f"编辑失败：词条「{word}」不存在"


def _cmd_memory_delete(args: List[str]) -> str:
    """删除 token 别名：/memory delete <词语>（需确认）"""
    if len(args) < 1:
        return "用法: /memory delete <词语>"

    word = args[0]
    from data.memory import get_token_type

    if get_token_type(word) is None:
        return f"词条「{word}」不存在"

    return f'[CONFIRM] delete_token {word} 确认删除「{word}」？(y/n)'


def _cmd_memory_reset(_args: List[str]) -> str:
    """清空所有记忆（需二次确认）。"""
    from data.memory import get_stats

    stats = get_stats()
    return (
        f"[CONFIRM] reset_memory 此操作将清空所有长期记忆，"
        f"包括 {stats['aliases']} 条词典别名 "
        f"和 {stats['templates']} 个模板规则，确认？(yes/不可撤销)"
    )


def _cmd_template_list(_args: List[str]) -> str:
    """列出已缓存的模板。"""
    from data.memory import get_template_rules

    rules = get_template_rules()
    if not rules:
        return "(暂无模板缓存)"

    lines = ["  指纹（前8位）    列数    缓存时间", "  " + "-" * 44]
    for fp, entry in sorted(rules.items()):
        fp8 = fp[:8]
        if isinstance(entry, dict):
            rule = entry.get("rule", entry)
            n_cols = len(rule.get("field_mapping", {})) if isinstance(rule, dict) else "?"
            cached = entry.get("cached_at", "?")
        else:
            n_cols = "?"
            cached = "?"
        lines.append(f"  {fp8}          {str(n_cols).ljust(6)}  {cached}")
    return "\n".join(lines)


def _cmd_template_show(args: List[str]) -> str:
    """查看模板字段映射配置：/template show <指纹前N位>"""
    if len(args) < 1:
        return "用法: /template show <指纹前N位>"

    prefix = args[0]
    from data.memory import get_template_rules

    rules = get_template_rules()
    matches = [(fp, entry) for fp, entry in rules.items() if fp.startswith(prefix)]

    if len(matches) == 0:
        return f"未找到指纹以「{prefix}」开头的模板缓存"
    if len(matches) > 1:
        fps = ", ".join(m[0][:8] for m in matches)
        return f"前缀「{prefix}」匹配到多个模板: {fps}\n请提供更长的前缀"

    fp, entry = matches[0]
    if isinstance(entry, dict):
        rule = entry.get("rule", entry)
    else:
        rule = entry

    lines = [
        f"模板指纹: {fp}",
        f"缓存时间: {entry.get('cached_at', '?') if isinstance(entry, dict) else '?'}",
        "",
        "字段映射 (模板列 → 标准字段):",
    ]
    fm = rule.get("field_mapping", {}) if isinstance(rule, dict) else {}
    for tcol, cfield in fm.items():
        lines.append(f"  {tcol} → {cfield}")

    composite = rule.get("composite_col") if isinstance(rule, dict) else None
    target = rule.get("target_col") if isinstance(rule, dict) else None
    irrelevant = rule.get("irrelevant_cols", []) if isinstance(rule, dict) else []

    lines.append(f"\n复合列: {composite or '(无)'}")
    lines.append(f"目标列: {target or '(无)'}")
    if irrelevant:
        lines.append(f"忽略列: {', '.join(irrelevant)}")

    return "\n".join(lines)


def _cmd_template_clear(args: List[str]) -> str:
    """删除指定模板缓存：/template clear <指纹前N位>（需确认）"""
    if len(args) < 1:
        return "用法: /template clear <指纹前N位>"

    prefix = args[0]
    from data.memory import get_template_rules

    rules = get_template_rules()
    matches = [(fp, entry) for fp, entry in rules.items() if fp.startswith(prefix)]

    if len(matches) == 0:
        return f"未找到指纹以「{prefix}」开头的模板缓存"
    if len(matches) > 1:
        fps = ", ".join(m[0][:8] for m in matches)
        return f"前缀「{prefix}」匹配到多个模板: {fps}\n请提供更长的前缀"

    fp = matches[0][0]
    return f"[CONFIRM] clear_template {fp} {prefix}"


def _cmd_run(args: List[str]) -> str:
    """在 REPL 内执行映射任务：/run -m <主数据表> -t <模板> -o <输出> [...]"""
    if not args:
        return "用法: /run -m <主数据表> -t <模板> -o <输出> [--target-col <列名>] [-r <报告>]"

    # 委托给 main.run()
    from main import run

    exit_code = run(args)
    if exit_code == 0:
        return ""  # run() 自己打印了完整输出，这里只返回空
    else:
        return f"\n[!] 映射任务失败 (exit_code={exit_code})"


def _cmd_exit(_args: List[str]) -> str:
    """退出 REPL。"""
    return "[EXIT]"


# ── 二级路由 ──────────────────────────────────────────────────────


def _cmd_memory_dispatch(args: List[str]) -> str:
    """二级路由：/memory <subcommand> [...]"""
    if not args:
        return "用法: /memory list|add|delete|reset"
    sub = args[0].lower()
    rest = args[1:]
    if sub == "list":
        return _cmd_memory_list(rest)
    elif sub == "add":
        return _cmd_memory_add(rest)
    elif sub == "edit":
        return _cmd_memory_edit(rest)
    elif sub == "delete":
        return _cmd_memory_delete(rest)
    elif sub == "reset":
        return _cmd_memory_reset(rest)
    else:
        return f"未知 /memory 子指令: {sub}\n可用: list, add, edit, delete, reset"


def _cmd_template_dispatch(args: List[str]) -> str:
    """二级路由：/template <subcommand> [...]"""
    if not args:
        return "用法: /template list|show|clear"
    sub = args[0].lower()
    rest = args[1:]
    if sub == "list":
        return _cmd_template_list(rest)
    elif sub == "show":
        return _cmd_template_show(rest)
    elif sub == "clear":
        return _cmd_template_clear(rest)
    else:
        return f"未知 /template 子指令: {sub}\n可用: list, show, clear"


# ── 指令路由表 ────────────────────────────────────────────────────

_COMMANDS: Dict[str, Callable[[List[str]], str]] = {
    "help": _cmd_help,
    "memory": _cmd_memory_dispatch,
    "template": _cmd_template_dispatch,
    "run": _cmd_run,
    "exit": _cmd_exit,
}


# ── 命令解析与分发 ────────────────────────────────────────────────


def _parse_line(line: str) -> Tuple[str, List[str]]:
    """将输入行解析为指令名和参数列表。

    支持双引号包裹的参数（用于含空格的文件路径）。
    示例:
        /run -m "my data.xlsx" -t t.xlsx -o out.xlsx
        → ("run", ["-m", "my data.xlsx", "-t", "t.xlsx", "-o", "out.xlsx"])
    """
    import shlex

    # shlex.split 处理引号内的空格
    try:
        parts = shlex.split(line)
    except ValueError:
        # 引号不匹配，降级为简单 split
        parts = line.split()

    if not parts:
        return ("", [])

    cmd = parts[0]
    if cmd.startswith("/"):
        cmd = cmd[1:]  # 去掉前导 /
    return (cmd.lower(), parts[1:])


def process_command(line: str) -> str:
    """解析并执行一条斜杠指令，返回输出文本。

    此函数是 REPL 的核心入口，也可供外部测试直接调用。

    特殊返回值:
        "[EXIT]"      → 调用方应退出 REPL
        "[CONFIRM] ..." → 调用方应启动确认流程

    Args:
        line: 用户输入的一行文本。

    Returns:
        指令执行结果文本。
    """
    cmd_name, args = _parse_line(line)

    if not cmd_name:
        return ""

    if cmd_name not in _COMMANDS:
        return f"未知指令「/{cmd_name}」，输入 /help 查看可用指令"

    try:
        return _COMMANDS[cmd_name](args)
    except Exception as e:
        return f"[错误] 指令执行失败: {e}"


# ── 确认流程 ──────────────────────────────────────────────────────


def _handle_confirm(action: str, payload: str) -> str:
    """执行确认后的实际操作（delete_token / reset_memory / clear_template）。

    Args:
        action: "delete_token", "reset_memory", "clear_template"
        payload: 附加参数

    Returns:
        操作结果文本。
    """
    if action == "delete_token":
        word = payload
        from data.memory import delete_token as mem_delete_token

        ok = mem_delete_token(word)
        return f"已删除词条「{word}」" if ok else f"删除失败：词条「{word}」不存在"

    elif action == "reset_memory":
        from data.memory import reset_memory as mem_reset

        mem_reset()
        return "已清空所有长期记忆"

    elif action == "clear_template":
        parts = payload.split(" ", 1)
        fp = parts[0]
        prefix = parts[1] if len(parts) > 1 else fp[:8]
        from data.memory import delete_template_rule as mem_delete_template

        deleted = mem_delete_template(fp)
        if deleted:
            return f"已删除模板缓存（指纹: {deleted[:16]}...）"
        else:
            return f"删除失败：未找到指纹为「{prefix}」的模板缓存"

    return f"未知确认操作: {action}"


# ── REPL 主循环 ────────────────────────────────────────────────────


def repl_loop() -> None:
    """REPL 主循环。读取用户输入 → 分发执行 → 打印结果。"""
    print("══════════════════════════════════════════════════")
    print("  POS Template Mapping Agent — 交互模式")
    print("  输入 /help 查看可用指令，/exit 退出")
    print("  不带 / 前缀的参数将执行映射任务")
    print("══════════════════════════════════════════════════")
    print()

    pending_confirm: Optional[Tuple[str, str]] = None  # (action, payload)

    while True:
        try:
            prompt = "pos-agent> " if pending_confirm is None else "  确认? (y/n/yes/不可撤销) > "
            line = input(prompt).strip()
        except (KeyboardInterrupt, EOFError):
            print("\n再见")
            break

        if not line:
            continue

        # ── 处理待确认状态 ──
        if pending_confirm is not None:
            action, payload = pending_confirm
            low = line.lower().strip()

            if action == "reset_memory":
                # /memory reset 需要完整输入 "yes" 或 "不可撤销"
                if low in ("yes", "不可撤销"):
                    print(_handle_confirm(action, payload))
                else:
                    print("已取消")
                pending_confirm = None
                continue
            else:
                # delete / clear 只需要 y/n
                if low in ("y", "yes"):
                    print(_handle_confirm(action, payload))
                elif low in ("n", "no"):
                    print("已取消")
                else:
                    print("请输入 y 或 n")
                    continue
                pending_confirm = None
                continue

        # ── 如果是斜杠指令 ──
        if line.startswith("/"):
            result = process_command(line)

            if result.startswith("[EXIT]"):
                print("再见")
                break

            if result.startswith("[CONFIRM]"):
                # 解析确认元数据
                # 格式: [CONFIRM] <action> <payload...>
                parts = result.split(" ", 2)
                action = parts[1] if len(parts) > 1 else ""
                payload = parts[2] if len(parts) > 2 else ""
                pending_confirm = (action, payload)
                # 打印确认提示
                print(result.split(" ", 2)[2] if len(parts) > 2 else result)
                continue

            if result:
                print(result)
        else:
            # 非斜杠开头 → 当作 /run 指令处理
            # 支持直接输入: -m file -t file -o out
            run_result = _cmd_run(_parse_line(line)[1] if line else [])
            if run_result:
                print(run_result)


# ── 自测 ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import os as _os

    _os.environ["USE_MOCK_LLM"] = "1"
    import importlib
    importlib.reload(__import__("config"))

    # ── 备份真实 memory.json ──
    import shutil as _shutil
    _mem_path = _os.path.expanduser("~/.pos_agent/memory.json")
    _mem_backup = None
    if _os.path.exists(_mem_path):
        _mem_backup_path = _mem_path + ".self_test_backup"
        _shutil.copy(_mem_path, _mem_backup_path)
        _mem_backup = _mem_backup_path

    from data.memory import reset_memory

    reset_memory()

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

    print("=== /指令系统 REPL 自测 ===\n")

    # ── 1. /help ──
    print("1. /help 输出完整指令列表")
    help_out = process_command("/help")
    check("记忆管理" in help_out, "含「记忆管理」")
    check("模板管理" in help_out, "含「模板管理」")
    check("映射任务" in help_out, "含「映射任务」")
    check("/exit" in help_out, "含 /exit")
    check("tea_base" in help_out, "含类型合法值")
    print()

    # ── 2. /memory add 合法类型 ──
    print("2. /memory add 合法/非法类型")
    r = process_command("/memory add 珍珠奶茶 tea_base")
    check("已添加" in r and "茶底" in r, f"合法 tea_base → 成功: {r[:50]}")
    r2 = process_command("/memory add 豆乳奶茶 milk_base")
    check("已添加" in r2 and "奶底" in r2, f"合法 milk_base → 成功: {r2[:50]}")
    print()

    # ── 3. /memory add 非法类型 ──
    print("3. /memory add 非法类型 → 给出合法值提示")
    r3 = process_command("/memory add 测试 invalid_type")
    check("非法类型" in r3, "返回「非法类型」")
    check("tea_base" in r3, "含英文合法值列表")
    print()

    # ── 4. /memory add 参数不足 ──
    print("4. /memory add 参数不足 → 用法提示")
    r4 = process_command("/memory add")
    check("用法" in r4, "参数不足时返回用法")
    r4b = process_command("/memory add 只有一个参数")
    check("用法" in r4b, "只有一个参数也返回用法")
    print()

    # ── 5. /memory list ──
    print("5. /memory list 展示所有词条")
    r5 = process_command("/memory list")
    check("珍珠奶茶" in r5, "包含刚添加的「珍珠奶茶」")
    check("豆乳奶茶" in r5, "包含刚添加的「豆乳奶茶」")
    check("茶底" in r5, "包含类型信息")
    print()

    # ── 6. /memory delete 存在 → CONFIRM，然后确认 ──
    print("6. /memory delete 确认流程")
    r6 = process_command("/memory delete 珍珠奶茶")
    check(r6.startswith("[CONFIRM]"), f"返回 CONFIRM: {r6[:60]}")
    # 模拟确认
    confirm_result = _handle_confirm("delete_token", "珍珠奶茶")
    check("已删除" in confirm_result, f"确认后删除成功: {confirm_result}")
    # 验证已删除
    from data.memory import get_token_type
    check(get_token_type("珍珠奶茶") is None, "删除后词条不存在")
    print()

    # ── 7. /memory delete 不存在 ──
    print("7. /memory delete 不存在的词")
    r7 = process_command("/memory delete 不存在的词")
    check("不存在" in r7, "提示词条不存在")
    print()

    # ── 8. /memory edit 修改已有词条类型 ──
    print("8. /memory edit 修改已有词条类型")
    # 先添加一个词
    process_command("/memory add 选错的词 茶底")
    check(get_token_type("选错的词") == "茶底", "初始类型为茶底")
    # 编辑修改
    r8a = process_command("/memory edit 选错的词 milk_base")
    check("已修改" in r8a, f"编辑成功: {r8a[:50]}")
    check(get_token_type("选错的词") == "奶底", "类型已改为奶底")
    # 编辑不存在的词
    r8b = process_command("/memory edit 不存在的词 茶底")
    check("不存在" in r8b, "不存在的词提示错误")
    # 编辑非法类型
    r8c = process_command("/memory edit 选错的词 invalid")
    check("非法类型" in r8c, "非法类型提示错误")
    # 参数不足
    r8d = process_command("/memory edit")
    check("用法" in r8d, "参数不足提示用法")
    r8e = process_command("/memory edit 只有一个参数")
    check("用法" in r8e, "只有一个参数也提示用法")
    print()

    # ── 9. /memory reset 确认流程 ──
    print("9. /memory reset 确认流程")
    r8 = process_command("/memory reset")
    check(r8.startswith("[CONFIRM]"), f"返回 CONFIRM: {r8[:60]}")
    check("yes/不可撤销" in r8, "提示需要完整确认词")
    # 模拟取消: 只返回 y 不够
    # 模拟确认
    reset_result = _handle_confirm("reset_memory", "")
    check("已清空" in reset_result, f"确认后清空成功: {reset_result}")
    print()

    # ── 10. /memory list 空列表 ──
    print("10. /memory list 空列表")
    r9 = process_command("/memory list")
    check("暂无" in r9, "空列表时提示「暂无」")
    print()

    # ── 11. /template list 有缓存 ──
    print("11. /template list 有缓存时展示")
    from data.memory import save_template_rule
    save_template_rule("a1b2c3d4e5f6a7b8", {
        "field_mapping": {"菜品名称": "product_name", "规格": "size"},
        "composite_col": "口味做法组合",
        "target_col": "配料",
        "irrelevant_cols": [],
    })
    r10 = process_command("/template list")
    check("a1b2c3d4" in r10, "显示指纹前8位")
    check("2" in r10, "显示列数")
    print()

    # ── 12. /template list 无缓存 ──
    print("12. /template list 无缓存")
    reset_memory()
    r11 = process_command("/template list")
    check("暂无" in r11, "无缓存时提示「暂无」")
    # 恢复一个缓存供后续测试
    save_template_rule("a1b2c3d4e5f6a7b8", {
        "field_mapping": {"菜品名称": "product_name", "规格": "size"},
        "composite_col": "口味做法组合",
        "target_col": "配料",
        "irrelevant_cols": [],
    })
    print()

    # ── 13. /template show ──
    print("13. /template show")
    r12 = process_command("/template show a1b2c3d4")
    check("菜品名称" in r12, "显示字段映射中的模板列")
    check("product_name" in r12, "显示标准字段名")
    check("口味做法组合" in r12, "显示 composite_col")
    check("配料" in r12, "显示 target_col")
    print()
    r12b = process_command("/template show 不存在的指纹")
    check("未找到" in r12b, "不存在的指纹提示未找到")
    print()
    r12c = process_command("/template show")
    check("用法" in r12c, "无参数时提示用法")
    print()

    # ── 14. /template clear 确认流程 ──
    print("14. /template clear 确认流程")
    r13 = process_command("/template clear a1b2c3d4")
    check(r13.startswith("[CONFIRM]"), f"返回 CONFIRM: {r13[:60]}")
    # 模拟确认
    parts = r13.split(" ", 2)
    clear_action = parts[1]
    clear_payload = parts[2]
    clear_confirm = _handle_confirm(clear_action, clear_payload)
    check("已删除" in clear_confirm, f"确认后删除成功: {clear_confirm}")
    # 验证已删除
    from data.memory import get_template_rules
    check(len(get_template_rules()) == 0, "缓存已清空")
    print()

    # ── 15. 未知指令 ──
    print("15. 未知指令")
    r14 = process_command("/unknown_command")
    check("未知指令" in r14, "提示未知指令")
    check("/help" in r14, "建议查看 /help")
    print()

    # ── 16. /exit ──
    print("16. /exit")
    r15 = process_command("/exit")
    check(r15 == "[EXIT]", "返回 [EXIT] 信号")
    print()

    # ── 17. /run 在 REPL 内执行映射任务 ──
    print("17. /run 在 REPL 内执行映射任务")
    import tempfile
    import pandas as pd

    tmpdir = tempfile.mkdtemp()
    master_path = os.path.join(tmpdir, "master.xlsx")
    template_path = os.path.join(tmpdir, "template.xlsx")
    output_path = os.path.join(tmpdir, "output.xlsx")

    pd.DataFrame({
        "品名": ["浅浅清茶", "珍珠奶茶"],
        "杯型": ["中杯", "中杯"],
        "奶底": ["牛奶", "椰乳"],
        "做法": ["少冰", "热"],
        "糖": ["七分糖", "无糖"],
        "SOP": ["T240", "T180"],
    }).to_excel(master_path, index=False)

    pd.DataFrame({
        "菜品名称": ["浅浅清茶", "珍珠奶茶"],
        "规格": ["中杯", "中杯"],
        "口味做法组合": ["牛奶, 少冰, 七分糖", "椰乳, 热, 无糖"],
        "配料": ["", ""],
    }).to_excel(template_path, index=False)

    from agent.token_classifier import reset_cache
    reset_cache()

    run_cmd = f'/run -m "{master_path}" -t "{template_path}" -o "{output_path}"'
    r16 = process_command(run_cmd)
    check(os.path.exists(output_path), "输出文件已生成")
    print()

    # cleanup
    for f in [master_path, template_path, output_path,
              output_path.replace(".xlsx", "_report.txt")]:
        if os.path.exists(f):
            os.remove(f)
    os.rmdir(tmpdir)
    reset_cache()

    # ── 18. /run 参数不足 ──
    print("18. /run 参数不足 → 用法提示")
    r17 = process_command("/run")
    check("用法" in r17, "返回用法提示")
    print()

    # ── 19. /memory add 支持中文类型 ──
    print("19. /memory add 支持中文类型名")
    r18 = process_command("/memory add 黑芝麻仙草 茶底")
    check("已添加" in r18, f"中文类型名有效: {r18[:50]}")
    check(get_token_type("黑芝麻仙草") == "茶底", "中文类型正确存储")
    print()

    # ── 20. 非斜杠输入 → /run ──
    print("20. 空指令/边角情况")
    r19a = process_command("")
    check(r19a == "", "空输入返回空")
    r19b = process_command("/memory")
    check("用法" in r19b, "/memory 无子指令 → 用法")
    r19c = process_command("/template")
    check("用法" in r19c, "/template 无子指令 → 用法")
    r19d = process_command("/memory unknown_sub")
    check("未知" in r19d, "未知 memory 子指令 → 提示")
    r19e = process_command("/template unknown_sub")
    check("未知" in r19e, "未知 template 子指令 → 提示")
    print()

    # cleanup
    reset_memory()

    # ── 还原真实 memory.json ──
    if _mem_backup:
        from data.memory import reload as _mem_reload
        _shutil.move(_mem_backup, _mem_path)
        _mem_reload()

    print(f"=== 结果: {passed} passed, {failed} failed ===")
