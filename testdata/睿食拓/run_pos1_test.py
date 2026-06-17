# -*- coding: utf-8 -*-
"""
POS1 配方导出模板测试脚本
读取新测试数据，运行映射管线，对比答案文件，输出准确率报告。
"""
import sys
import os
import time
import tempfile
import shutil
import json

import pandas as pd
import numpy as np

# 切换到 testdata 目录
os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "..")

if __name__ == "__main__":
        # ── 0. 预填充未知 Token ──
    from data.memory import add_token as mem_add, reset_memory, reload as mem_reload

    # ── 备份真实 memory.json ──
    _mem_path = os.path.expanduser("~/.pos_agent/memory.json")
    _mem_backup = None
    if os.path.exists(_mem_path):
        _mem_backup_path = _mem_path + ".run_test_backup"
        shutil.copy(_mem_path, _mem_backup_path)
        _mem_backup = _mem_backup_path

    reset_memory()

    UNKNOWN_TOKENS = {
        "锡兰红茶": "茶底", "白桃乌龙": "茶底", "白兰碧螺春": "茶底",
        "大红袍": "茶底", "额外加一份糖": "糖度",
    }
    for word, wtype in UNKNOWN_TOKENS.items():
        mem_add(word, wtype)
    print("Pre-filled %d unknown tokens to memory" % len(UNKNOWN_TOKENS))

    from agent.token_classifier import set_prompt_hook, reset_cache, reset_session_asked
    def auto_unknown_hook(word, context):
        print("  [AUTO] unknown word '%s' -> UNKNOWN" % word)
        return {"action": "unknown"}
    set_prompt_hook(auto_unknown_hook)
    reset_cache()
    reset_session_asked()

    # ── 1. 读取数据 ──
    print("=" * 60)
    print("  POS1 Recipe Export Template - Accuracy Test")
    print("=" * 60)

    master_raw = pd.read_excel("SOP代码主数据.xlsx")
    print("\nMaster raw columns: %s" % list(master_raw.columns))
    print("Master rows: %d" % len(master_raw))

    master_col_map = {
        "Unnamed: 0": "品名", "杯型": "杯型", "Unnamed: 2": "奶底",
        "做法": "做法", "糖": "糖", "代码": "SOP", "产品名称规格明细": "全信息",
    }
    master_df = master_raw.rename(columns=master_col_map)
    keep_cols = ["品名", "杯型", "奶底", "做法", "糖", "SOP"]
    if "全信息" in master_df.columns:
        keep_cols.append("全信息")
    master_df = master_df[[c for c in keep_cols if c in master_df.columns]]
    master_df["做法"] = master_df["做法"].apply(lambda x: str(x).strip() if pd.notna(x) else x)

    print("Processed master columns: %s" % list(master_df.columns))
    print("Unique products: %d, Sizes: %s" % (master_df['品名'].nunique(), list(master_df['杯型'].unique())))
    print("Temps: %s" % list(master_df['做法'].unique()))
    print("Sugars: %s" % list(master_df['糖'].unique()))
    print("Milk non-null: %d" % master_df['奶底'].notna().sum())

    template_df = pd.read_excel("pos1配方导出模板.xlsx", sheet_name="菜品配方")
    print("\nTemplate columns: %s" % list(template_df.columns))
    print("Template rows: %d" % len(template_df))

    answer_df = pd.read_excel("pos1配方导出模板答案.xlsx", sheet_name="菜品配方")
    print("Answer rows: %d, filled: %d, empty: %d" % (
        len(answer_df), answer_df['配料'].notna().sum(), answer_df['配料'].isna().sum()
    ))

    # ── 2. 准备临时文件 ──
    tmpdir = tempfile.mkdtemp()
    print("\nTemp dir: %s" % tmpdir)

    master_tmp = os.path.join(tmpdir, "master_renamed.xlsx")
    template_tmp = os.path.join(tmpdir, "template_single.xlsx")
    output_path = os.path.join(tmpdir, "output.xlsx")
    report_path = os.path.join(tmpdir, "report.txt")

    master_df.to_excel(master_tmp, index=False)
    template_df.to_excel(template_tmp, index=False)

    # ── 3. 运行管线 ──
    print("\n" + "-" * 60)
    print("  Running pipeline...")
    print("-" * 60)

    from agent.workflow import run_pipeline

    t0 = time.time()
    state = run_pipeline(
        master_path=master_tmp,
        template_path=template_tmp,
        output_path=output_path,
        report_path=report_path,
        target_col="配料",
        use_langgraph=False,
    )
    elapsed = time.time() - t0

    if state.has_error:
        print("\n[FAIL] Pipeline failed: %s - %s" % (state.error_step, state.error))
        sys.exit(1)

    api_calls = state.api_call_count

    # ── 4. 结果概览 ──
    print("\n[OK] Pipeline done (%.1fs, API: %d)" % (elapsed, api_calls))

    total = len(state.match_results)
    high = sum(1 for r in state.match_results if r.get("confidence") == "HIGH")
    low = total - high

    print("  Total: %d" % total)
    print("  HIGH:  %d (%.1f%%)" % (high, 100.0*high/max(total,1)))
    print("  LOW:   %d (%.1f%%)" % (low, 100.0*low/max(total,1)))

    # ── 5. 对比答案 ──
    print("\n" + "=" * 60)
    print("  Accuracy Analysis")
    print("=" * 60)

    match_results = state.match_results
    answer_sops = answer_df["配料"].tolist()
    answer_products = answer_df["菜品名称"].tolist()
    answer_sizes = answer_df["规格"].tolist()
    answer_combos = answer_df["口味做法组合"].tolist()

    correct_high = 0
    correct_low = 0
    wrong_high = 0
    wrong_low = 0
    no_answer = 0
    correct_no_answer = 0
    wrong_no_answer = 0
    errors = []

    def norm_sop(s):
        """Normalize SOP string for comparison: unify separators."""
        if not s:
            return ""
        s = str(s).strip()
        # Normalize Chinese punctuation to ASCII equivalents
        s = s.replace("、", ",")   # 、-> ,
        s = s.replace("，", ",")   # ，-> ,
        s = s.replace("；", ";")   # ；-> ;
        s = s.replace("　", " ")   # 全角空格 -> 半角空格
        # Remove whitespace around commas
        s = ",".join(p.strip() for p in s.split(","))
        return s

    for i in range(total):
        predicted_sop_raw = str(match_results[i].get("sop", "") or "").strip()
        predicted_sop = norm_sop(predicted_sop_raw)
        confidence = match_results[i].get("confidence", "")
        product_score = match_results[i].get("product_score", 0)
        match_type = match_results[i].get("match_type", "?")
        matched_attrs = match_results[i].get("matched_attributes", [])
        unmatched_attrs = match_results[i].get("unmatched_attributes", [])

        actual_sop = answer_sops[i] if i < len(answer_sops) else None

        if actual_sop is None or (isinstance(actual_sop, float) and np.isnan(actual_sop)):
            no_answer += 1
            if confidence == "HIGH":
                wrong_no_answer += 1
                errors.append({
                    "row": i + 1,
                    "product": answer_products[i] if i < len(answer_products) else "?",
                    "size": answer_sizes[i] if i < len(answer_sizes) else "?",
                    "combo": answer_combos[i] if i < len(answer_combos) else "?",
                    "predicted": predicted_sop,
                    "actual": "(no answer)",
                    "confidence": confidence,
                    "type": "no_answer_HIGH",
                    "score": product_score,
                })
            else:
                correct_no_answer += 1
        else:
            actual_sop_clean = norm_sop(actual_sop)
            if predicted_sop == actual_sop_clean:
                if confidence == "HIGH":
                    correct_high += 1
                else:
                    correct_low += 1
            else:
                if confidence == "HIGH":
                    wrong_high += 1
                else:
                    wrong_low += 1
                errors.append({
                    "row": i + 1,
                    "product": answer_products[i] if i < len(answer_products) else "?",
                    "size": answer_sizes[i] if i < len(answer_sizes) else "?",
                    "combo": answer_combos[i] if i < len(answer_combos) else "?",
                    "predicted": predicted_sop,
                    "actual": actual_sop_clean,
                    "confidence": confidence,
                    "type": "SOP_mismatch",
                    "score": product_score,
                    "matched": matched_attrs,
                    "unmatched": unmatched_attrs,
                })

    # ── 6. 输出报告 ──
    has_answer_total = total - no_answer
    correct_total = correct_high + correct_low
    wrong_total = wrong_high + wrong_low

    print("\n  Rows with answer: %d" % has_answer_total)
    print("  Rows without answer: %d" % no_answer)

    print("\n  --- Answered Rows ---")
    print("  HIGH + correct:  %4d  (%5.1f%%)" % (correct_high, 100.0*correct_high/max(has_answer_total,1)))
    print("  LOW  + correct:  %4d  (%5.1f%%)" % (correct_low, 100.0*correct_low/max(has_answer_total,1)))
    print("  HIGH + WRONG:    %4d  (%5.1f%%) ** DANGER" % (wrong_high, 100.0*wrong_high/max(has_answer_total,1)))
    print("  LOW  + WRONG:    %4d  (%5.1f%%)" % (wrong_low, 100.0*wrong_low/max(has_answer_total,1)))
    print("  ---")
    print("  Effective Accuracy: %4d  (%5.1f%%)" % (correct_total, 100.0*correct_total/max(has_answer_total,1)))
    print("  HIGH Accuracy:      %4d  (%5.1f%%)" % (correct_high, 100.0*correct_high/max(has_answer_total,1)))

    print("\n  --- Unanswered Rows ---")
    print("  LOW  (reasonable): %4d" % correct_no_answer)
    print("  HIGH (overconfident): %4d **" % wrong_no_answer)

    total_usable = correct_high + correct_low + correct_no_answer
    print("\n  Overall usable: %d/%d (%.1f%%)" % (total_usable, total, 100.0*total_usable/total))

    # ── 7. 错误详情 ──
    if errors:
        print("\n" + "=" * 60)
        print("  Error Details (%d errors)" % len(errors))
        print("=" * 60)

        from collections import Counter
        error_types = Counter(e["type"] for e in errors)
        print("\n  Error type distribution:")
        for et, count in error_types.most_common():
            print("    %s: %d" % (et, count))

        print("\n  First 30 errors:")
        for e in errors[:30]:
            print("    Row %4d | %-12s | %-6s | %s" % (e["row"], e["product"], e["size"], e["combo"][:30]))
            print("           predicted: %s" % e["predicted"][:60])
            print("           actual:    %s" % e["actual"][:60])
            print("           confidence: %s  score: %.0f" % (e["confidence"], e.get("score", 0)))
            if e.get("unmatched"):
                print("           unmatched: %s" % e["unmatched"])
            print()

        if len(errors) > 30:
            print("  ... %d more errors" % (len(errors) - 30))

    # ── 8. 低置信度原因分析 ──
    print("=" * 60)
    print("  LOW Confidence Root Cause Analysis")
    print("=" * 60)

    low_reasons = Counter()
    for i, r in enumerate(state.match_results):
        if r.get("confidence") == "LOW_CONFIDENCE":
            unmatched = tuple(r.get("unmatched_attributes", []))
            mtype = r.get("match_type", "?")
            score = r.get("product_score", 0)
            if mtype == "best_guess":
                low_reasons["best_guess (score=%d, unmatched=%s)" % (score, unmatched)] += 1
            elif mtype == "product_only":
                low_reasons["product_only (score=%d, unmatched=%s)" % (score, unmatched)] += 1
            else:
                low_reasons["%s (unmatched=%s)" % (mtype, unmatched)] += 1

    for reason, count in low_reasons.most_common(15):
        print("  %4d: %s" % (count, reason))

    # ── 9. 商品名匹配分析 ──
    print("\n" + "=" * 60)
    print("  Product Name Score < 90 Analysis")
    print("=" * 60)

    low_score_products = Counter()
    for i, r in enumerate(state.match_results):
        score = r.get("product_score", 0)
        if score < 90:
            tp = answer_products[i] if i < len(answer_products) else "?"
            low_score_products[(tp, score)] += 1

    for (prod, score), count in low_score_products.most_common(20):
        print("  %3d: '%s' (score=%d)" % (count, prod, score))

    # ── 10. 保存详细结果到 JSON ──
    detail_path = "firstresult/pos1_test_details.json"
    os.makedirs("firstresult", exist_ok=True)

    details = {
        "summary": {
            "total": total,
            "high": high,
            "low": low,
            "answered_total": has_answer_total,
            "no_answer": no_answer,
            "correct_high": correct_high,
            "correct_low": correct_low,
            "wrong_high": wrong_high,
            "wrong_low": wrong_low,
            "effective_accuracy_pct": round(100.0 * correct_total / max(has_answer_total, 1), 1),
            "high_accuracy_pct": round(100.0 * correct_high / max(has_answer_total, 1), 1),
            "elapsed_seconds": round(elapsed, 1),
            "api_calls": api_calls,
        },
        "error_types": dict(error_types) if errors else {},
        "low_reasons": dict(low_reasons.most_common(20)),
        "errors": errors[:50],
    }
    with open(detail_path, "w", encoding="utf-8") as f:
        json.dump(details, f, ensure_ascii=False, indent=2, default=str)
    print("\nDetailed results saved to: %s" % detail_path)

    # ── 清理临时文件 ──
    shutil.rmtree(tmpdir, ignore_errors=True)
    print("Temp files cleaned up")

    # ── 还原真实 memory.json ──
    if _mem_backup:
        shutil.move(_mem_backup, _mem_path)
        mem_reload()

    print("\n" + "=" * 60)
    print("  Test Complete")
    print("=" * 60)
