"""
真实 API 冒烟测试 — 验证 DeepSeek API 连通性及 LLM 返回格式是否可被正确解析。

Usage:
    # 默认（运行全部）
    python tests/smoke_test_api.py

    # 仅测试 Schema Analyzer
    python tests/smoke_test_api.py --schema-only

    # 仅测试 Token Classifier
    python tests/smoke_test_api.py --token-only

与模块自测的区别：模块自测使用 Mock 模式（USE_MOCK_LLM=1），
本测试直接调用真实 DeepSeek API，验证端到端格式兼容性。
"""

import os
import sys
import time
import argparse

# 确保项目根在 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

# 强制关闭 Mock 模式
config.USE_MOCK_LLM = False
os.environ["USE_MOCK_LLM"] = "0"


# ── 工具函数 ──────────────────────────────────────────────────────

def green(s):
    return f"\033[92m{s}\033[0m"


def red(s):
    return f"\033[91m{s}\033[0m"


def yellow(s):
    return f"\033[93m{s}\033[0m"


def bold(s):
    return f"\033[1m{s}\033[0m"


passed = 0
failed = 0


def check(condition, msg):
    global passed, failed
    if condition:
        passed += 1
        print(f"  {green('[PASS]')}  {msg}")
    else:
        failed += 1
        print(f"  {red('[FAIL]')}  {msg}")


def section(title):
    print(f"\n{bold('=== ' + title + ' ===')}\n")


# ── Schema Analyzer 冒烟 ─────────────────────────────────────────

def test_schema_analyzer():
    """用真实 API 测试 Schema Analyzer 的完整链路：调用 -> 解析 -> 验证。"""
    global failed, passed

    from agent.schema_analyzer import analyze, reset_cache

    section("Schema Analyzer 真实 API 冒烟")

    reset_cache()

    # 模拟真实 POS 模板列名
    columns = ["菜品名称", "规格", "口味做法组合", "配料"]
    sample_data = [
        {"菜品名称": "珍珠奶茶", "规格": "大杯", "口味做法组合": "红茶, 燕麦奶, 七分糖, 少冰", "配料": ""},
        {"菜品名称": "椰果奶茶", "规格": "中杯", "口味做法组合": "绿茶, 牛奶, 全糖, 去冰", "配料": ""},
    ]

    t0 = time.time()
    try:
        result = analyze(columns, sample_data, use_cache=False)
        elapsed = time.time() - t0
    except Exception as e:
        print(f"  {red('[FAIL]')}  API 调用或解析失败: {e}")
        failed += 1
        return

    print(f"  API 耗时: {elapsed:.1f}s\n")

    # 1. 基本结构
    check(isinstance(result, dict), "返回 dict")
    check("field_mapping" in result, "包含 field_mapping")
    check("composite_col" in result, "包含 composite_col")
    check("target_col" in result, "包含 target_col")
    check("irrelevant_cols" in result, "包含 irrelevant_cols")

    fm = result.get("field_mapping", {})

    # 2. field_mapping 应包含至少 2 个映射
    check(len(fm) >= 2, f"field_mapping 至少 2 个映射（实际 {len(fm)}）")

    # 3. 映射值必须是合法的 canonical 字段
    from data.canonical_schema import CANONICAL_FIELDS
    for tcol, cfield in fm.items():
        check(
            cfield in CANONICAL_FIELDS,
            f"模板列 '{tcol}' -> canonical '{cfield}' 合法",
        )

    # 4. composite_col 应在 columns 中（如果非 None）
    composite = result.get("composite_col")
    if composite is not None:
        check(composite in columns, f"composite_col '{composite}' 在列名中")
        check(
            composite not in fm,
            f"composite_col '{composite}' 不在 field_mapping 中",
        )

    # 5. target_col 应在 columns 中（如果非 None）
    target = result.get("target_col")
    if target is not None:
        check(target in columns, f"target_col '{target}' 在列名中")

    print(f"\n  LLM 返回的完整映射:\n    {result}")


# ── Token Classifier 冒烟 ────────────────────────────────────────

def test_token_classifier():
    """用真实 API 测试 Token Classifier 的完整链路：调用 -> 解析 -> 验证。"""
    global failed, passed

    from agent.token_classifier import classify_batch, classify_single, reset_cache

    section("Token Classifier 真实 API 冒烟")

    reset_cache()

    # ── 单个分类 ──
    print("  1/2 classify_single（单值分类）\n")

    t0 = time.time()
    try:
        result = classify_single("红茶, 燕麦奶, 七分糖, 少冰", use_cache=False)
        elapsed = time.time() - t0
    except Exception as e:
        print(f"  {red('[FAIL]')}  API 调用或解析失败: {e}")
        failed += 1
        return

    print(f"  API 耗时: {elapsed:.1f}s\n")

    # 基本结构
    check(isinstance(result, dict), "返回 dict")
    check("tokens" in result and "missing" in result, "包含 tokens 和 missing")

    tokens = result.get("tokens", [])
    missing = result.get("missing", [])

    # 应至少有 3 个 token（茶底/奶底/糖度/温度至少出现 3 个）
    check(len(tokens) >= 3, f"tokens 数量 >= 3（实际 {len(tokens)}）")

    # 每个 token 必须有 value 和 type
    valid_types = {"茶底", "奶底", "糖度", "温度", "UNKNOWN"}
    for tok in tokens:
        check("value" in tok and "type" in tok, f"token 含 value/type: {tok}")
        check(tok.get("type") in valid_types, f"type '{tok.get('type')}' 合法")

    # missing 中的维度不应在 tokens 中出现
    present_types = {t.get("type") for t in tokens}
    for m in missing:
        check(m not in present_types, f"missing '{m}' 不在 tokens 中重复")

    print(f"\n  classify_single 结果:\n    tokens:   {tokens}\n    missing:  {missing}")

    # ── 批量分类 ──
    print("\n  2/2 classify_batch（批量分类）\n")

    reset_cache()
    batch_input = [
        "红茶, 十二分糖, 温热",
        "燕麦奶, 正常冰, 七分糖",
        "",                               # 空值
        "乌龙茶, 椰乳, 三分糖, 去冰",
    ]

    t0 = time.time()
    try:
        batch_results = classify_batch(batch_input, use_cache=False)
        elapsed = time.time() - t0
    except Exception as e:
        print(f"  {red('[FAIL]')}  批量 API 调用或解析失败: {e}")
        failed += 1
        return

    print(f"  API 耗时: {elapsed:.1f}s（{len(batch_input)} 条）\n")

    check(len(batch_results) == 4, f"返回 4 条结果（实际 {len(batch_results)}）")

    for i, (inp, res) in enumerate(zip(batch_input, batch_results)):
        t = res.get("tokens", [])
        m = res.get("missing", [])
        is_empty_input = not (inp and inp.strip())

        if is_empty_input:
            check(t == [], f"[{i}] 空输入 -> tokens 为空")
            check(len(m) == 4, f"[{i}] 空输入 -> 4 维全缺失")
        else:
            check(len(t) >= 2, f"[{i}] '{inp[:20]}...' -> {len(t)} tokens")
            # 一致性检查
            p_types = {tok.get("type") for tok in t}
            for dim in m:
                check(dim not in p_types, f"[{i}] '{dim}' 不在 tokens+missing 重复")

        print(f"    [{i}] \"{inp if inp else '(空)'}\"")
        print(f"        tokens={t}, missing={m}")


# ── 主入口 ────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DeepSeek API 真实冒烟测试")
    parser.add_argument("--schema-only", action="store_true", help="仅测试 Schema Analyzer")
    parser.add_argument("--token-only", action="store_true", help="仅测试 Token Classifier")
    args = parser.parse_args()

    run_all = not args.schema_only and not args.token_only

    print(bold("DeepSeek API 真实冒烟测试"))
    print(f"API Base: {config.DEEPSEEK_BASE_URL}")
    print(f"Model:    {config.DEEPSEEK_MODEL}")
    print(f"Timeout:  {config.LLM_TIMEOUT_SECONDS}s")

    t_start = time.time()

    if run_all or args.schema_only:
        test_schema_analyzer()

    if run_all or args.token_only:
        test_token_classifier()

    # ── 汇总 ──
    elapsed = time.time() - t_start
    print(f"\n{bold('=== 结果汇总 ===')}")
    print(f"  总耗时: {elapsed:.1f}s")
    print(f"  通过:   {green(str(passed))}")
    print(f"  失败:   {red(str(failed)) if failed else '0'}")

    if failed > 0:
        print(f"\n{red('冒烟测试未通过，请检查 API 配置或 LLM 返回格式。')}")
        sys.exit(1)
    else:
        print(f"\n{green('冒烟测试全部通过，真实 API 链路正常。')}")
        sys.exit(0)
