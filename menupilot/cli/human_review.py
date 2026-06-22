"""
Human Review CLI — 低置信度行交互式审核。

用法:
    from menupilot.cli.human_review import run_review
    result = run_review(low_conf_rows, master_fingerprint)
"""

from typing import Any, Dict, List


def run_review(
    low_conf_rows: List[Dict[str, Any]],
    master_fingerprint: str = "",
) -> Dict[str, Any]:
    """交互式审核低置信度行。

    Args:
        low_conf_rows: step_match 输出的低置信度行列表。
        master_fingerprint: 主数据文件 MD5 前 8 位（保留字段）。

    Returns:
        {"decisions": [{"row_index": 5, "action": "accept", "sop": "..."}, ...]}
    """
    total = len(low_conf_rows)
    if total == 0:
        return {"decisions": []}

    decisions = []
    for idx_in_list, row in enumerate(low_conf_rows):
        # row 来自 match_results，row_index 是原始 match_results 索引
        row_index = row.get("_match_index", idx_in_list)
        product = row.get("template_product_name", "?")
        sop = row.get("sop", "")
        score = row.get("product_score", 0)
        reason = row.get("failure_reason", "?")
        unmatched = row.get("unmatched_attributes", [])

        print(f"\n{'='*56}")
        print(f"[LOW_CONFIDENCE 审核] {idx_in_list + 1}/{total} 条需要确认")
        print(f"{'='*56}")
        print(f"\n[{idx_in_list + 1}/{total}] 商品：{product}")
        if unmatched:
            print(f"       不匹配属性：{', '.join(unmatched)}")
        print(f"       候选 SOP：{sop}（置信度 {score:.0f}%）")
        print(f"       原因：{reason}")
        print()
        print("[1] 接受此结果")
        print("[2] 手动输入正确 SOP")
        print("[3] 本次跳过（下次运行仍会提示）")
        print("[4] 永久跳过（不再提示此行）")
        print("-" * 42)

        decision = _get_user_decision(row_index, sop)
        decisions.append(decision)

    # 打印摘要
    accepted = sum(1 for d in decisions if d["action"] in ("accept", "manual"))
    skipped = sum(1 for d in decisions if d["action"] == "skip")
    perm_skipped = sum(1 for d in decisions if d["action"] == "permanent_skip")
    print(f"\n审核完成：接受 {accepted} / 跳过 {skipped} / 永久跳过 {perm_skipped}")

    return {"decisions": decisions}


def _get_user_decision(row_index: int, default_sop: str) -> Dict[str, Any]:
    """获取单条审核决策。"""
    while True:
        try:
            choice = input("  请输入 1/2/3/4: ").strip()
        except (EOFError, KeyboardInterrupt):
            return {"row_index": row_index, "action": "skip"}

        if choice == "1":
            return {
                "row_index": row_index,
                "action": "accept",
                "sop": default_sop,
            }
        elif choice == "2":
            sop = input("  请输入正确的 SOP: ").strip()
            if sop:
                return {"row_index": row_index, "action": "manual", "sop": sop}
            print("  [错误] SOP 不能为空，请重新输入或选择其他选项")
        elif choice == "3":
            return {"row_index": row_index, "action": "skip"}
        elif choice == "4":
            return {"row_index": row_index, "action": "permanent_skip"}
        else:
            print("  [错误] 无效输入，请输入 1、2、3 或 4")


def run_review_silent(
    low_conf_rows: List[Dict[str, Any]],
    action: str = "skip",
) -> Dict[str, Any]:
    """非交互式审核（批量模式用）。

    对所有低置信度行统一执行相同操作。

    Args:
        low_conf_rows: 低置信度行列表。
        action: "accept" | "skip" | "permanent_skip"

    Returns:
        {"decisions": [...]}
    """
    decisions = []
    for idx, row in enumerate(low_conf_rows):
        sop = row.get("sop", "")
        if action == "accept":
            decisions.append({
                "row_index": idx,
                "action": "accept",
                "sop": sop,
            })
        else:
            decisions.append({
                "row_index": idx,
                "action": action,
            })
    return {"decisions": decisions}


# ── 自测 ────────────────────────────────────────────────────────

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

    print("=== Human Review 自测 ===\n")

    # ── 1. 空列表 ──
    print("1. 空列表返回空 decisions")
    r1 = run_review_silent([], "skip")
    check(r1["decisions"] == [], "空列表 → 空 decisions")
    print()

    # ── 2. accept 操作 ──
    print("2. accept 操作")
    rows = [{"sop": "T240"}]
    r2 = run_review_silent(rows, "accept")
    check(len(r2["decisions"]) == 1, "1 条 decision")
    check(r2["decisions"][0]["action"] == "accept", "action=accept")
    check(r2["decisions"][0]["sop"] == "T240", "sop=T240")
    print()

    # ── 3. manual 操作需要用户输入，用 silent 模拟 ──
    print("3. manual 操作（silent 模拟）")
    # manual 只在交互模式触发，silent 模式用 accept + sop 覆盖
    r3 = run_review_silent(
        [{"sop": "OLD"}], "accept"
    )
    check(r3["decisions"][0]["action"] == "accept", "silent accept")
    print()

    # ── 4. skip 操作 ──
    print("4. skip 操作")
    r4 = run_review_silent(rows, "skip")
    check(r4["decisions"][0]["action"] == "skip", "action=skip")
    print()

    # ── 5. permanent_skip 操作 ──
    print("5. permanent_skip 操作")
    r5 = run_review_silent(rows, "permanent_skip")
    check(r5["decisions"][0]["action"] == "permanent_skip", "action=permanent_skip")
    print()

    # ── 6. 多条审核 ──
    print("6. 多条审核")
    rows6 = [
        {"sop": "SOP-A", "template_product_name": "商品A"},
        {"sop": "SOP-B", "template_product_name": "商品B"},
        {"sop": "SOP-C", "template_product_name": "商品C"},
    ]
    r6 = run_review_silent(rows6, "accept")
    check(len(r6["decisions"]) == 3, "3 条 decisions")
    check(r6["decisions"][0]["sop"] == "SOP-A", "第 1 条 SOP 正确")
    check(r6["decisions"][2]["sop"] == "SOP-C", "第 3 条 SOP 正确")
    print()

    print(f"=== 结果: {passed} passed, {failed} failed ===")
