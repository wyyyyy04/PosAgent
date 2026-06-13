#!/usr/bin/env python
"""
State.xxx 属性访问 → State["xxx"] dict 访问 迁移工具。

用法:
  python tools/migrate_state_access.py          # dry-run，只预览
  python tools/migrate_state_access.py --apply  # 实际写入

迁移规则:
  state.field_name → state["field_name"]       （普通属性读写）
  state.has_error  → state.get("error") is not None
  state.set_error("step", msg) → 两行展开
"""

import argparse
import os
import re
import sys
from typing import List, Tuple

# ── 普通属性映射（属性名 → dict key）─────────────────────────

ATTR_TO_KEY = {
    "master_path": "master_path",
    "template_path": "template_path",
    "output_path": "output_path",
    "report_path": "report_path",
    "target_col": "target_col",
    "master_sheet": "master_sheet",
    "template_sheet": "template_sheet",
    "master_df": "master_df",
    "template_df": "template_df",
    "template_type": "template_type",
    "chowbus_rows": "chowbus_rows",
    "schema_result": "schema_result",
    "token_results": "token_results",
    "master_canonical": "master_canonical",
    "template_canonical": "template_canonical",
    "validated_tokens": "validated_tokens",
    "match_results": "match_results",
    "report": "report",
    "console_summary": "console_summary",
    "error": "error",
    "error_step": "error_step",
    "api_call_count": "api_call_count",
}

# 需要跳过的目录
SKIP_DIRS = {"testdata", ".git", "__pycache__", ".claude", "tools"}

# 普通属性，按名称长度降序排列（report_path 在 report 前处理）
ATTRS_BY_LENGTH = sorted(ATTR_TO_KEY.keys(), key=len, reverse=True)

# ── 替换记录 ──────────────────────────────────────────────


class Replacement:
    """单条替换记录。"""

    def __init__(self, filepath: str, lineno: int, old: str, new: str):
        self.filepath = filepath
        self.lineno = lineno
        self.old = old.strip()
        self.new = new.strip()


# ── 核心扫描逻辑 ───────────────────────────────────────────


def _find_py_files(root: str) -> List[str]:
    """递归查找所有 .py 文件，跳过 SKIP_DIRS。"""
    py_files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in filenames:
            if f.endswith(".py"):
                py_files.append(os.path.join(dirpath, f))
    return py_files


def _extract_set_error_args(line: str, start_pos: int) -> Tuple[str, str, str, int]:
    """从 state.set_error("step", msg) 中提取缩进、step 和 msg。

    返回 (indent, step, msg, end_pos)。
    """
    # 从 start_pos 开始，跳过 state.set_error(
    prefix_end = line.index("(", start_pos) + 1
    indent = line[:start_pos]  # state. 之前的空白

    # 提取第一个参数（字符串字面量 "step_name"）
    quote_start = line.index('"', prefix_end)
    quote_end = line.index('"', quote_start + 1)
    step = line[quote_start + 1 : quote_end]

    # 跳过 ", "
    comma_pos = line.index(",", quote_end)
    msg_start = comma_pos + 1
    # 跳过空白
    while msg_start < len(line) and line[msg_start] == " ":
        msg_start += 1

    # 从 msg_start 开始，计数括号找 msg 的结束位置
    paren_count = 0
    end_pos = msg_start
    for j in range(msg_start, len(line)):
        ch = line[j]
        if ch == "(":
            paren_count += 1
        elif ch == ")":
            if paren_count == 0:
                end_pos = j
                break
            paren_count -= 1

    msg = line[msg_start:end_pos].rstrip()
    return indent, step, msg, end_pos


def _process_file(filepath: str) -> List[Replacement]:
    """扫描单个文件，返回所有需要替换的记录。"""
    with open(filepath, "r", encoding="utf-8") as f:
        lines = f.readlines()

    replacements: List[Replacement] = []

    # ── Pass 1: set_error 方法调用 ──
    set_error_marker = "state.set_error("

    for i, line in enumerate(lines):
        pos = line.find(set_error_marker)
        if pos < 0 or _is_comment_or_string_only(line, pos):
            continue
        indent, step, msg, _ = _extract_set_error_args(line, pos)
        new = f'{indent}state["error"] = {msg}\n{indent}state["error_step"] = "{step}"'
        replacements.append(
            Replacement(filepath, i + 1, line.rstrip("\n"), new)
        )

    # 收集被 set_error 替换的行号（这些行的其他替换需要跳过）
    set_error_lines = {r.lineno for r in replacements}

    # ── Pass 2: has_error ──
    has_error_pattern = re.compile(r"state\.has_error\b")

    for i, line in enumerate(lines):
        if i + 1 in set_error_lines:
            continue
        if has_error_pattern.search(line) and not _is_comment_or_string_only(
            line, has_error_pattern.search(line).start()
        ):
            new_line = has_error_pattern.sub(
                'state.get("error") is not None', line
            )
            if new_line != line:
                replacements.append(
                    Replacement(filepath, i + 1, line.rstrip("\n"), new_line.rstrip("\n"))
                )

    # ── Pass 3: 普通属性（has_error 和 set_error 已经处理过） ──
    # state.xxx → state["xxx"]
    # 按属性名长度降序处理，防止 report 覆盖 report_path
    for i, line in enumerate(lines):
        if i + 1 in set_error_lines:
            continue
        new_line = line
        for attr in ATTRS_BY_LENGTH:
            if attr in ("has_error",):
                continue
            old = f"state.{attr}"
            key = ATTR_TO_KEY[attr]
            new = f'state["{key}"]'
            # 只替换不在 dict 访问中、不在方法调用中的
            # 用负向前瞻：state.xxx 后面不能跟 ( 或 " 或 _（排除 .get, ["xxx"], .xxx_）
            pattern = re.compile(
                r'(?<!\")state\.' + attr + r'\b(?!\s*\(|")'
            )
            if pattern.search(new_line):
                new_line = pattern.sub(new, new_line)

        if new_line != line:
            replacements.append(
                Replacement(filepath, i + 1, line.rstrip("\n"), new_line.rstrip("\n"))
            )

    # ── 去重：同一行可能有多次命中 ──
    seen = set()
    unique = []
    for r in replacements:
        key = (r.filepath, r.lineno)
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique


def _is_comment_or_string_only(line: str, pos: int) -> bool:
    """简单判断匹配位置是否在注释或字符串内（启发式）。"""
    before = line[:pos]
    # 检查单引号/双引号是否未闭合
    single_count = before.count("'") - before.count("\\'")
    double_count = before.count('"') - before.count('\\"')
    if single_count % 2 != 0 or double_count % 2 != 0:
        return True
    # 检查是否在 # 注释后
    if "#" in before:
        return True
    return False


# ── 主入口 ────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="State.xxx → State[xxx] 迁移工具")
    parser.add_argument(
        "--apply", action="store_true", help="实际写入文件（默认 dry-run）"
    )
    args = parser.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    py_files = _find_py_files(root)

    all_replacements: List[Replacement] = []
    for fp in sorted(py_files):
        reps = _process_file(fp)
        all_replacements.extend(reps)

    if not all_replacements:
        print("没有发现需要替换的 state.xxx 属性访问。")
        return 0

    if args.apply:
        # 按文件分组，逆序应用（行号从后往前）
        from collections import defaultdict

        by_file: dict = defaultdict(list)
        for r in all_replacements:
            by_file[r.filepath].append(r)

        for fp, reps in by_file.items():
            with open(fp, "r", encoding="utf-8") as f:
                lines = f.readlines()

            # 构造行号 → 新内容的映射（一行可能被多次修改，只生效最后一次）
            line_map: dict = {}
            for r in reps:
                line_map[r.lineno] = r.new

            new_lines = []
            for i, line in enumerate(lines):
                lineno = i + 1
                if lineno in line_map:
                    new_lines.append(line_map[lineno] + "\n")
                else:
                    new_lines.append(line)

            with open(fp, "w", encoding="utf-8") as f:
                f.writelines(new_lines)

        print(f"已应用 {len(all_replacements)} 处替换。")
        return 0

    # Dry-run: 预览
    print(f"共 {len(all_replacements)} 处需要替换:\n")
    for r in all_replacements:
        relpath = os.path.relpath(r.filepath, root)
        print(f"  {relpath}:{r.lineno}")
        print(f"    - {r.old}")
        print(f"    + {r.new}")
        print()
    print(f"共 {len(all_replacements)} 处。使用 --apply 实际执行。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
