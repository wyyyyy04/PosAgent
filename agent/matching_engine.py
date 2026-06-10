"""
Matching Engine — 商品名精确匹配 + 属性组合匹配 + 低置信度兜底。
纯规则引擎，不调用 LLM。位于 Rule Engine 之后，是整个工作流的最后一步。

匹配策略（按优先级）：
1. RapidFuzz token_sort_ratio 商品名匹配（阈值 ≥ 90）
2. 属性组合精确匹配（规格/温度/糖度必须匹配，奶底/茶底支持通配）
3. Embedding 候选召回（可选，默认关闭）
4. 兜底：填入最佳猜测，标注 LOW_CONFIDENCE
"""

from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from rapidfuzz import fuzz

import config
from data.canonical_schema import CANONICAL_FIELDS, REQUIRED_DIMENSIONS, WILDCARD_DIMENSIONS

# ── 常量 ────────────────────────────────────────────────────────

HIGH = "HIGH"
LOW_CONFIDENCE = "LOW_CONFIDENCE"

MATCH_EXACT = "exact"
MATCH_ATTRIBUTE = "attribute_match"
MATCH_PRODUCT_ONLY = "product_only"
MATCH_BEST_GUESS = "best_guess"


def _empty(val) -> bool:
    """判断值是否为空。"""
    if val is None:
        return True
    if isinstance(val, float) and val != val:  # NaN check
        return True
    if isinstance(val, str) and val.strip() == "":
        return True
    return False


def _infer_failure_reason(
    product_score: float,
    unmatched_attrs: List[str],
    template_row: Dict[str, Any],
) -> str:
    """从匹配结果推断 LOW_CONFIDENCE 的失败原因。

    优先级：
      1. 商品名分数低于阈值 → PRODUCT_NOT_FOUND
      2. 属性不匹配 → 按 milk_base > size > temperature > sugar > tea_base 顺序，
         取第一个不匹配属性，附带模板中的实际值（用于报告展示）。

    Args:
        product_score: 商品名匹配分数。
        unmatched_attrs: 不匹配的属性列表。
        template_row: 模板 canonical 行（用于提取不匹配属性的实际值）。

    Returns:
        失败原因字符串，格式为 "REASON_CODE" 或 "REASON_CODE:extra_value"。
        HIGH 置信度应传空字符串，调用方自行处理。
    """
    threshold = config.MATCHING_CONFIG.get("product_name_threshold", 90)

    # 商品名分数不足 → 未找到
    if product_score < threshold:
        return "PRODUCT_NOT_FOUND"

    # 属性不匹配 → 按优先级取第一个
    priority_fields = ["milk_base", "size", "temperature", "sugar", "tea_base"]
    for field in priority_fields:
        if field in unmatched_attrs:
            extra = str(template_row.get(field, "") or "").strip()
            if extra:
                return f"{field.upper()}_NOT_FOUND:{extra}"
            return f"{field.upper()}_NOT_FOUND"

    # 兜底
    return "UNKNOWN"


# ── 商品名匹配 ────────────────────────────────────────────────

def _compute_product_scores(
    template_name: str,
    master_names: List[str],
) -> List[float]:
    """计算模板商品名与所有主数据商品名的 RapidFuzz 相似度。

    Args:
        template_name: 模板行商品名。
        master_names: 所有主数据商品名列表。

    Returns:
        与 master_names 一一对应的 token_sort_ratio 分数列表。
    """
    scores = []
    for m_name in master_names:
        score = fuzz.token_sort_ratio(
            str(template_name or "").strip(),
            str(m_name or "").strip(),
        )
        scores.append(score)
    return scores


# ── 属性匹配 ──────────────────────────────────────────────────

def _attributes_match(
    master: Dict[str, Any],
    template: Dict[str, Any],
) -> Tuple[bool, List[str], List[str]]:
    """检查模板行与主数据行在属性维度上是否匹配。

    匹配规则：
    - 必要维度（规格/温度/糖度）：master 和 template 都必须有值且精确相等。
    - 通配维度（奶底/茶底）：master 有值时必须精确匹配；master 为空则通配（跳过）。

    Args:
        master: 主数据 canonical 行。
        template: 模板 canonical 行。

    Returns:
        (is_match, matched_fields, unmatched_fields)
    """
    matched = []
    unmatched = []

    for field in REQUIRED_DIMENSIONS:
        m_val = master.get(field)
        t_val = template.get(field)
        if _empty(m_val) or _empty(t_val):
            unmatched.append(field)
        elif str(m_val).strip() == str(t_val).strip():
            matched.append(field)
        else:
            unmatched.append(field)

    for field in WILDCARD_DIMENSIONS:
        m_val = master.get(field)
        t_val = template.get(field)
        if _empty(m_val):
            # master 通配：无论 template 有无值都匹配
            matched.append(f"{field}(通配)")
        elif _empty(t_val):
            # master 有值但 template 缺失 → 不匹配
            unmatched.append(field)
        elif str(m_val).strip() == str(t_val).strip():
            matched.append(field)
        else:
            unmatched.append(field)

    return len(unmatched) == 0, matched, unmatched


# ── 匹配主流程 ────────────────────────────────────────────────

def match_single(
    template_row: Dict[str, Any],
    master_rows: List[Dict[str, Any]],
    threshold: Optional[int] = None,
    low_threshold: Optional[int] = None,
) -> Dict[str, Any]:
    """对单条模板行执行匹配。

    Args:
        template_row: 模板 canonical 行。
        master_rows: 所有主数据 canonical 行。
        threshold: 商品名高置信度阈值，默认从 config 读取。
        low_threshold: 低置信度阈值，默认从 config 读取。

    Returns:
        {
            "sop": "T240、B30/80、S4",
            "confidence": "HIGH" | "LOW_CONFIDENCE",
            "product_score": 95.0,
            "match_type": "exact" | "attribute_match" | "product_only" | "best_guess",
            "master_index": 0,
            "matched_attributes": [...],
            "unmatched_attributes": [...],
        }
    """
    if threshold is None:
        threshold = config.MATCHING_CONFIG["product_name_threshold"]
    if low_threshold is None:
        low_threshold = config.MATCHING_CONFIG["low_confidence_threshold"]

    template_name = str(template_row.get("product_name", "") or "").strip()

    # 快速失败：模板商品名为空
    if not template_name:
        return {
            "sop": "",
            "confidence": LOW_CONFIDENCE,
            "product_score": 0,
            "match_type": MATCH_BEST_GUESS,
            "master_index": -1,
            "matched_attributes": [],
            "unmatched_attributes": REQUIRED_DIMENSIONS[:],
            "failure_reason": "PRODUCT_NOT_FOUND",
        }

    master_names = [str(m.get("product_name", "") or "").strip() for m in master_rows]
    scores = _compute_product_scores(template_name, master_names)

    # 分离高置信度候选（≥ threshold）和低置信度候选（≥ low_threshold）
    high_candidates = [(i, scores[i]) for i in range(len(scores)) if scores[i] >= threshold]
    low_candidates = [
        (i, scores[i])
        for i in range(len(scores))
        if low_threshold <= scores[i] < threshold
    ]

    def _make_result(master_idx, score, mtype, confidence, matched, unmatched,
                     failure_reason=None):
        sop = ""
        if 0 <= master_idx < len(master_rows):
            sop = str(master_rows[master_idx].get("sop", "") or "")
        # 自动推断 failure_reason（仅 LOW_CONFIDENCE）
        if failure_reason is None and confidence == LOW_CONFIDENCE:
            failure_reason = _infer_failure_reason(score, unmatched, template_row)
        return {
            "sop": sop,
            "confidence": confidence,
            "product_score": score,
            "match_type": mtype,
            "master_index": master_idx,
            "matched_attributes": matched,
            "unmatched_attributes": unmatched,
            "failure_reason": failure_reason or "",
        }

    # ── Step 1: 高置信度候选 + 属性过滤 ──
    if high_candidates:
        # 按商品名分数降序排列
        high_candidates.sort(key=lambda x: x[1], reverse=True)

        attribute_matches = []
        for idx, score in high_candidates:
            is_match, matched_attrs, unmatched_attrs = _attributes_match(
                master_rows[idx], template_row
            )
            if is_match:
                attribute_matches.append((idx, score, matched_attrs, unmatched_attrs))

        if len(attribute_matches) == 1:
            idx, score, matched_attrs, unmatched_attrs = attribute_matches[0]
            return _make_result(
                idx, score, MATCH_EXACT, HIGH, matched_attrs, unmatched_attrs
            )

        if len(attribute_matches) > 1:
            # 多个候选都匹配属性 → 取商品名分数最高的
            attribute_matches.sort(key=lambda x: x[1], reverse=True)
            idx, score, matched_attrs, unmatched_attrs = attribute_matches[0]
            return _make_result(
                idx, score, MATCH_EXACT, HIGH, matched_attrs, unmatched_attrs
            )

        # 无属性完全匹配 → 用商品名最接近的候选，标 LOW_CONFIDENCE
        best_idx, best_score = high_candidates[0]
        _, matched_attrs, unmatched_attrs = _attributes_match(
            master_rows[best_idx], template_row
        )
        return _make_result(
            best_idx,
            best_score,
            MATCH_PRODUCT_ONLY,
            LOW_CONFIDENCE,
            matched_attrs,
            unmatched_attrs,
        )

    # ── Step 2: 低置信度候选（商品名阈值以下但仍可猜测）──
    if low_candidates:
        low_candidates.sort(key=lambda x: x[1], reverse=True)
        # 属性过滤
        for idx, score in low_candidates:
            is_match, matched_attrs, unmatched_attrs = _attributes_match(
                master_rows[idx], template_row
            )
            if is_match:
                return _make_result(
                    idx,
                    score,
                    MATCH_PRODUCT_ONLY,
                    LOW_CONFIDENCE,
                    matched_attrs,
                    unmatched_attrs,
                )

        # 都不匹配 → 用分数最高的
        best_idx, best_score = low_candidates[0]
        _, matched_attrs, unmatched_attrs = _attributes_match(
            master_rows[best_idx], template_row
        )
        return _make_result(
            best_idx,
            best_score,
            MATCH_BEST_GUESS,
            LOW_CONFIDENCE,
            matched_attrs,
            unmatched_attrs,
        )

    # ── Step 3: 完全无法匹配 → 全局最接近猜测 ──
    if scores:
        best_idx = max(range(len(scores)), key=lambda i: scores[i])
        best_score = scores[best_idx]
    else:
        best_idx, best_score = -1, 0

    _, matched_attrs, unmatched_attrs = (
        _attributes_match(master_rows[best_idx], template_row)
        if best_idx >= 0
        else (False, [], REQUIRED_DIMENSIONS[:])
    )
    return _make_result(
        best_idx,
        best_score,
        MATCH_BEST_GUESS,
        LOW_CONFIDENCE,
        matched_attrs,
        unmatched_attrs,
    )


def match(
    template_rows: List[Dict[str, Any]],
    master_rows: List[Dict[str, Any]],
    threshold: Optional[int] = None,
    low_threshold: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """批量匹配：每条模板行匹配一条最佳主数据行。

    Args:
        template_rows: 模板 canonical 行列表。
        master_rows: 主数据 canonical 行列表。
        threshold: 商品名高置信度阈值。
        low_threshold: 低置信度兜底阈值。

    Returns:
        匹配结果列表，与 template_rows 一一对应。每条结果包含
        sop / confidence / product_score / match_type / master_index / matched_attributes / unmatched_attributes。

    Raises:
        ValueError: master_rows 为空。
    """
    if not master_rows:
        raise ValueError("主数据行列表不能为空")
    if not template_rows:
        return []

    results = []
    for t_row in template_rows:
        result = match_single(t_row, master_rows, threshold, low_threshold)
        results.append(result)
    return results


# ── 报告生成 ──────────────────────────────────────────────────

# ── 失败原因中文映射 ──────────────────────────────────────────────

_REASON_CN_MAP = {
    "MILK_BASE_NOT_FOUND": "{extra}规格在主数据中缺失",
    "PRODUCT_NOT_FOUND": "商品名称在主数据中未找到",
    "SIZE_NOT_FOUND": "规格在主数据中缺失",
    "TEMPERATURE_NOT_FOUND": "温度/做法在主数据中缺失",
    "SUGAR_NOT_FOUND": "糖度在主数据中缺失",
    "TEA_BASE_NOT_FOUND": "茶底在主数据中缺失",
}

_REASON_SUGGESTION_MAP = {
    "MILK_BASE_NOT_FOUND": "补充 {extra} 相关 SOP 到主数据表",
    "PRODUCT_NOT_FOUND": "检查商品名称是否有错别字，或补充主数据表",
}


def _parse_failure_reason(reason: str) -> Tuple[str, str]:
    """解析 failure_reason，分离枚举码和附加值。

    Args:
        reason: 格式为 "CODE" 或 "CODE:extra" 的失败原因字符串。

    Returns:
        (code, extra) 元组。extra 为空字符串表示无附加值。
    """
    if ":" in reason:
        code, extra = reason.split(":", 1)
        return code, extra
    return reason, ""


def _format_top_reason(reason: str, count: int) -> Tuple[str, str]:
    """格式化主要原因的中文描述和建议。

    Args:
        reason: failure_reason 原始值（如 "MILK_BASE_NOT_FOUND:燕麦奶"）。
        count: 该原因出现的行数。

    Returns:
        (display, suggestion) 元组。display 为 "原因描述（N 行）"，
        suggestion 为建议文本，无建议时为空字符串。
    """
    code, extra = _parse_failure_reason(reason)

    template = _REASON_CN_MAP.get(code)
    if template is None:
        # 未知原因 → 直接输出原始值
        display = f"{reason}（{count} 行）"
        return display, ""

    display = template.format(extra=extra) if "{extra}" in template else template
    display = f"{display}（{count} 行）"

    suggestion_tpl = _REASON_SUGGESTION_MAP.get(code, "")
    suggestion = suggestion_tpl.format(extra=extra) if suggestion_tpl and "{extra}" in suggestion_tpl else suggestion_tpl

    return display, suggestion


def generate_report(
    match_results: List[Dict[str, Any]],
) -> str:
    """生成面向用户的匹配摘要报告。

    输出中文分级的摘要，展示高置信度/需要确认/完全失败的行数，
    并自动聚合最常见失败原因及建议。摘要下方附详细日志供调试。

    Args:
        match_results: match() 返回的结果列表。

    Returns:
        格式化的报告文本（用于终端打印和文件写入）。
    """
    total = len(match_results)
    high = sum(1 for r in match_results if r.get("confidence") == HIGH)
    low = sum(1 for r in match_results if r.get("confidence") == LOW_CONFIDENCE)
    failed = total - high - low

    lines = []
    # ── 摘要部分 ──
    lines.append("=" * 56)
    lines.append(f"本次映射完成，共 {total} 行")
    lines.append("")

    # ✅ 高置信匹配
    lines.append(f"✅ 高置信匹配：{high} 行")

    # ⚠️ 需要确认
    if low > 0:
        lines.append(f"⚠️  需要确认：{low} 行")

        # 聚合 failure_reason
        reason_counter: Counter = Counter()
        for r in match_results:
            if r.get("confidence") == LOW_CONFIDENCE:
                reason = r.get("failure_reason", "UNKNOWN")
                reason_counter[reason] += 1

        if reason_counter:
            top_reason, top_count = reason_counter.most_common(1)[0]
            display, suggestion = _format_top_reason(top_reason, top_count)
            lines.append(f"   └─ 主要原因：{display}")
            if suggestion:
                lines.append(f"      建议：{suggestion}")

    # ❌ 完全失败
    if failed > 0:
        lines.append(f"❌ 完全失败：{failed} 行")
    else:
        lines.append("❌ 完全失败：0 行")

    lines.append("=" * 56)

    # ── 详细日志部分 ──
    if low > 0:
        lines.append("")
        lines.append("--- 详细日志 ---")
        lines.append("")
        for i, r in enumerate(match_results):
            if r.get("confidence") == LOW_CONFIDENCE:
                score = r.get("product_score", 0)
                mtype = r.get("match_type", "?")
                reason = r.get("failure_reason", "?")
                unmatched = r.get("unmatched_attributes", [])
                lines.append(
                    f"  行 {i + 1}: "
                    f"商品名分数={score:.1f}, "
                    f"匹配类型={mtype}, "
                    f"原因={reason}"
                )
                if unmatched:
                    lines.append(f"    不匹配属性: {', '.join(unmatched)}")

    return "\n".join(lines)


# ── Embedding 兜底（可选）──────────────────────────────────────

def build_embedding_index(master_rows: List[Dict[str, Any]]) -> Optional[Any]:
    """构建主数据商品名的 FAISS 向量索引。

    仅在 config.MATCHING_CONFIG['embedding_enabled'] = True 时可用。
    sentence-transformers 和 faiss 为可选依赖，运行时按需导入。

    Args:
        master_rows: 主数据 canonical 行列表。

    Returns:
        (model, index, product_names) 元组；失败返回 None。
    """
    if not config.MATCHING_CONFIG["embedding_enabled"]:
        return None

    try:
        from sentence_transformers import SentenceTransformer
        import faiss
        import numpy as np
    except ImportError as e:
        print(f"[WARNING] Embedding 兜底依赖缺失: {e}")
        return None

    model_name = config.MATCHING_CONFIG["embedding_model"]
    model = SentenceTransformer(model_name)
    names = [str(m.get("product_name", "")) for m in master_rows]
    embeddings = model.encode(names, convert_to_numpy=True)

    dim = embeddings.shape[1]
    index = faiss.IndexFlatIP(dim)
    # FAISS IndexFlatIP 需要归一化向量 -> cosine similarity
    faiss.normalize_L2(embeddings)
    index.add(embeddings)

    return (model, index, names)


def embedding_recall(
    template_name: str,
    embedding_index: Any,
    top_k: Optional[int] = None,
    sim_threshold: Optional[float] = None,
) -> List[Tuple[int, float]]:
    """用 Embedding 召回候选商品名。

    Args:
        template_name: 模板商品名。
        embedding_index: build_embedding_index() 返回的 (model, index, names) 元组。
        top_k: 返回候选数。
        sim_threshold: 相似度阈值。

    Returns:
        [(master_index, similarity_score), ...] 按相似度降序排列。
    """
    if embedding_index is None:
        return []

    if top_k is None:
        top_k = config.MATCHING_CONFIG["embedding_top_k"]
    if sim_threshold is None:
        sim_threshold = config.MATCHING_CONFIG["embedding_similarity_threshold"]

    import numpy as np

    model, index, names = embedding_index
    query_vec = model.encode([template_name], convert_to_numpy=True)
    faiss.normalize_L2(query_vec)

    scores, indices = index.search(query_vec, min(top_k, len(names)))

    results = []
    for score, idx in zip(scores[0], indices[0]):
        if idx < 0 or idx >= len(names):
            continue
        if score >= sim_threshold:
            results.append((int(idx), float(score)))

    return results


# ── 自测 ──────────────────────────────────────────────────────

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

    print("=== Matching Engine 自测 ===\n")

    # ── 准备测试用主数据 ──
    master = [
        {
            "product_name": "浅浅清茶",
            "size": "中杯",
            "milk_base": "牛奶",
            "temperature": "少冰",
            "sugar": "七分糖",
            "tea_base": None,
            "sop": "T240、B30/80、S4、IC(S)、MS 3-5",
        },
        {
            "product_name": "浅浅清茶",
            "size": "中杯",
            "milk_base": "牛奶",
            "temperature": "去冰",
            "sugar": "标准糖",
            "tea_base": None,
            "sop": "T265、B30/105、S5、IC(S)、MS 3-5",
        },
        {
            "product_name": "浅浅清茶",
            "size": "大杯",
            "milk_base": "牛奶",
            "temperature": "正常冰",
            "sugar": "全糖",
            "tea_base": None,
            "sop": "T300、B40/120、S6、IC(S)、MS 3-5",
        },
        {
            "product_name": "黑糖波波牛乳",
            "size": "大杯",
            "milk_base": None,  # 通配：黑糖波波牛乳不挑奶底
            "temperature": "正常冰",
            "sugar": "标准糖",
            "tea_base": None,
            "sop": "T200、B50/100、S5、MS 3-5",
        },
        {
            "product_name": "珍珠奶茶",
            "size": "中杯",
            "milk_base": "椰乳",
            "temperature": "热",
            "sugar": "无糖",
            "tea_base": None,
            "sop": "T180、B40/80、S2、HOT、MS 2-3",
        },
    ]

    # ── 1. 精确匹配：商品名 + 所有属性匹配 ──
    print("1. 精确匹配（商品名 + 全属性匹配）")
    t1 = {
        "product_name": "浅浅清茶",
        "size": "中杯",
        "milk_base": "牛奶",
        "temperature": "少冰",
        "sugar": "七分糖",
        "tea_base": None,
    }
    r1 = match_single(t1, master)
    check(r1["confidence"] == HIGH, f"置信度 HIGH（实际 {r1['confidence']}）")
    check(r1["match_type"] == MATCH_EXACT, f"匹配类型 exact（实际 {r1['match_type']}）")
    check(r1["product_score"] >= 90, f"商品名分数 ≥ 90（实际 {r1['product_score']}）")
    check(r1["sop"] == "T240、B30/80、S4、IC(S)、MS 3-5", f"SOP 正确")
    check(
        set(r1["unmatched_attributes"]) == set(),
        f"无不匹配属性（实际 {r1['unmatched_attributes']}）",
    )
    print()

    # ── 2. 精确匹配：选择不同属性的行 ──
    print("2. 精确匹配（同名不同属性 → 选正确的）")
    t2 = {
        "product_name": "浅浅清茶",
        "size": "中杯",
        "milk_base": "牛奶",
        "temperature": "去冰",
        "sugar": "标准糖",
        "tea_base": None,
    }
    r2 = match_single(t2, master)
    check(r2["confidence"] == HIGH, "置信度 HIGH")
    check(r2["sop"] == "T265、B30/105、S5、IC(S)、MS 3-5", "SOP 正确（去冰/标准糖）")
    print()

    # ── 3. 通配奶底：master 奶底为 None → 任意模板奶底都匹配 ──
    print("3. 通配奶底匹配")
    t3 = {
        "product_name": "黑糖波波牛乳",
        "size": "大杯",
        "milk_base": "燕麦奶",  # master 奶底是 None（通配），任何奶底都接受
        "temperature": "正常冰",
        "sugar": "标准糖",
        "tea_base": None,
    }
    r3 = match_single(t3, master)
    check(r3["confidence"] == HIGH, "置信度 HIGH（通配生效）")
    check(r3["sop"] == "T200、B50/100、S5、MS 3-5", "SOP 正确")
    check("milk_base(通配)" in r3["matched_attributes"], "milk_base 标记为通配匹配")
    print()

    # ── 4. 商品名高相似度匹配（token_sort_ratio ≥ 90） ──
    print("4. 商品名高相似度匹配")
    # 插入一个高相似度主数据行用于测试 token_sort_ratio 行为
    master_fuzzy = master + [
        {
            "product_name": "黑糖波波牛乳茶",
            "size": "中杯",
            "milk_base": "燕麦奶",
            "temperature": "少冰",
            "sugar": "五分糖",
            "tea_base": None,
            "sop": "T999",
        }
    ]
    t4 = {
        "product_name": "黑糖波波牛乳",  # 缺少"茶"，与"黑糖波波牛乳茶"高度相似
        "size": "中杯",
        "milk_base": "燕麦奶",
        "temperature": "少冰",
        "sugar": "五分糖",
        "tea_base": None,
    }
    r4 = match_single(t4, master_fuzzy)
    check(r4["product_score"] >= 85, f"token_sort_ratio ≥ 85（实际 {r4['product_score']:.1f}）")
    # 注意：如果分数 < 90 则是 LOW_CONFIDENCE，否则 HIGH
    print()

    # ── 4b. 完全相同的商品名 → 100 分 ──
    print("4b. 完全相同商品名（100 分）")
    t4b = {
        "product_name": "浅浅清茶",
        "size": "中杯",
        "milk_base": "牛奶",
        "temperature": "少冰",
        "sugar": "七分糖",
        "tea_base": None,
    }
    r4b = match_single(t4b, master)
    check(r4b["product_score"] == 100.0, f"相同商品名 = 100（实际 {r4b['product_score']}）")
    check(r4b["confidence"] == HIGH, "置信度 HIGH")
    print()

    # ── 5. 商品名无匹配 → LOW_CONFIDENCE ──
    print("5. 商品名无匹配（best_guess 兜底）")
    t5 = {
        "product_name": "完全不存在的商品XYZ",
        "size": "中杯",
        "milk_base": "牛奶",
        "temperature": "少冰",
        "sugar": "七分糖",
        "tea_base": None,
    }
    r5 = match_single(t5, master)
    check(r5["confidence"] == LOW_CONFIDENCE, f"置信度 LOW_CONFIDENCE（实际 {r5['confidence']}）")
    check(r5["match_type"] == MATCH_BEST_GUESS, f"匹配类型 best_guess（实际 {r5['match_type']}）")
    check(r5["sop"] != "", "兜底仍返回了 SOP（最佳猜测）")
    print()

    # ── 6. 属性不匹配 → LOW_CONFIDENCE ──
    print("6. 商品名匹配但属性不匹配")
    t6 = {
        "product_name": "浅浅清茶",
        "size": "超大杯",  # 主数据没有超大杯
        "milk_base": "牛奶",
        "temperature": "少冰",
        "sugar": "七分糖",
        "tea_base": None,
    }
    r6 = match_single(t6, master)
    check(r6["confidence"] == LOW_CONFIDENCE, "置信度 LOW_CONFIDENCE")
    check("size" in r6["unmatched_attributes"], "size 在不匹配属性中")
    print()

    # ── 7. 批量匹配 ──
    print("7. 批量匹配 match()")
    batch_results = match(
        [t1, t2, t3, t4b, t5, t6],
        master,
    )
    check(len(batch_results) == 6, f"6 条结果（实际 {len(batch_results)}）")
    check(batch_results[0]["confidence"] == HIGH, "第 1 条 HIGH")
    check(batch_results[4]["confidence"] == LOW_CONFIDENCE, "第 5 条 LOW_CONFIDENCE")
    check(batch_results[5]["confidence"] == LOW_CONFIDENCE, "第 6 条 LOW_CONFIDENCE")
    print()

    # ── 8. 空模板行 ──
    print("8. 空模板行处理")
    t8 = {
        "product_name": "",
        "size": None,
        "milk_base": None,
        "temperature": None,
        "sugar": None,
        "tea_base": None,
    }
    r8 = match_single(t8, master)
    check(r8["confidence"] == LOW_CONFIDENCE, "空商品名 → LOW_CONFIDENCE")
    check(r8["product_score"] == 0, "分数为 0")
    print()

    # ── 9. 空 template_rows ──
    print("9. 空模板列表")
    r9 = match([], master)
    check(r9 == [], "空模板 → 空结果")
    print()

    # ── 10. 空主数据 ──
    print("10. 空主数据处理")
    try:
        match([t1], [])
        check(False, "match() 空 master 应抛异常")
    except ValueError as e:
        check("不能为空" in str(e), f"match() ValueError: {e}")

    # match_single 空 master → 优雅降级
    r10 = match_single(t1, [])
    check(r10["confidence"] == LOW_CONFIDENCE, "match_single 空 master → LOW_CONFIDENCE")
    check(r10["master_index"] == -1, "match_single 空 master → master_index=-1")
    print()

    # ── 11. 同名产品多个候选按属性精确选择 ──
    print("11. 多候选精确属性匹配")
    # 浅浅清茶有 3 行不同属性，应精确选中大杯/正常冰/全糖
    t11 = {
        "product_name": "浅浅清茶",
        "size": "大杯",
        "milk_base": "牛奶",
        "temperature": "正常冰",
        "sugar": "全糖",
        "tea_base": None,
    }
    r11 = match_single(t11, master)
    check(r11["confidence"] == HIGH, "置信度 HIGH")
    check(r11["sop"] == "T300、B40/120、S6、IC(S)、MS 3-5", "选中大杯 SOP")
    print()

    # ── 12. 报告生成 ──
    print("12. 报告生成")
    report = generate_report(batch_results)
    check("需要确认：2 行" in report, "报告显示 2 条需要确认")
    check("详细日志" in report, "报告包含详细日志段")
    check("完全失败：0 行" in report, "报告显示完全失败 0 行")
    # 全 HIGH 报告
    high_only = [batch_results[0], batch_results[1], batch_results[2], batch_results[3], r11]
    report_high = generate_report(high_only)
    check("需要确认" not in report_high, "全 HIGH 报告不含需要确认段")
    check("高置信匹配：5 行" in report_high, "全 HIGH 报告显示 5 行高置信匹配")
    print()

    # ── 汇总 ──
    print(f"=== 结果: {passed} passed, {failed} failed ===")
