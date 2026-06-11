"""将所有 state.xxx 属性访问替换为 state["xxx"] dict 访问。"""
import re
import os


def find_matching_paren(s, start):
    depth = 0
    for j in range(start, len(s)):
        if s[j] == '(':
            depth += 1
        elif s[j] == ')':
            if depth == 0:
                return j
            depth -= 1
    return -1


# 简单属性（长度降序，防止 report 覆盖 report_path）
ATTRS = sorted([
    'template_canonical', 'master_canonical', 'validated_tokens',
    'console_summary', 'chowbus_rows', 'schema_result', 'template_type',
    'template_sheet', 'template_path', 'match_results', 'template_df',
    'token_results', 'api_call_count', 'output_path', 'target_col',
    'master_sheet', 'master_path', 'report_path', 'error_step',
    'master_df', 'report', 'error',
], key=len, reverse=True)


def process_file(fpath):
    with open(fpath, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    out = []
    for line in lines:
        # --- hasattr(state, 'xxx') → state.get("xxx") is not None ---
        line = re.sub(
            r"hasattr\(state,\s*'(\w+)'\)",
            r'state.get("\1") is not None',
            line,
        )
        # --- getattr(state, 'xxx', '') → state.get("xxx", "") ---
        line = re.sub(
            r"getattr\(state,\s*'(\w+)',\s*''\)",
            r'state.get("\1", "")',
            line,
        )

        # --- set_error expansion ---
        pos = line.find('state.set_error(')
        if pos >= 0:
            indent = line[:pos]
            q1 = line.index('"', pos)
            q2 = line.index('"', q1 + 1)
            step = line[q1 + 1 : q2]
            comma = line.index(',', q2)
            ms = comma + 1
            while ms < len(line) and line[ms] == ' ':
                ms += 1
            me = find_matching_paren(line, ms)
            msg = line[ms:me].strip()
            out.append(f'{indent}state["error"] = {msg}\n')
            out.append(f'{indent}state["error_step"] = "{step}"\n')
            continue

        # --- has_error ---
        line = line.replace('state.has_error', 'state.get("error") is not None')

        # --- simple attrs ---
        for attr in ATTRS:
            old = f'state.{attr}'
            new = f'state["{attr}"]'
            line = line.replace(old, new)

        out.append(line)

    with open(fpath, 'w', encoding='utf-8') as f:
        f.writelines(out)

    compile(open(fpath, 'r', encoding='utf-8').read(), fpath, 'exec')


if __name__ == '__main__':
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for f in ['agent/workflow.py', 'main.py']:
        fpath = os.path.join(root, f)
        process_file(fpath)
        print(f'{f}: syntax OK')
    print('Done.')
