"""广泛搜索词提精准活动的确定性准入。"""

from __future__ import annotations

from collections import defaultdict

from app.models.campaign import NewCampaignDecision, SearchTermPromotionCandidate


def _normalize(value: object) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _aggregate(candidates: list[SearchTermPromotionCandidate], key_of) -> dict[str, list[SearchTermPromotionCandidate]]:
    grouped: dict[str, list[SearchTermPromotionCandidate]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        key = _normalize(key_of(candidate))
        if not key or (candidate.relevance_tier or "").upper() != "R1":
            continue
        source_key = (candidate.campaign_key, _normalize(candidate.search_term))
        if source_key in seen:
            continue
        seen.add(source_key)
        grouped[key].append(candidate)
    return grouped


def _metrics(items: list[SearchTermPromotionCandidate]) -> tuple[int, float, float, float | None]:
    orders = sum(max(0, item.orders) for item in items)
    cost = sum(max(0.0, item.cost) for item in items)
    sales = sum(max(0.0, item.sales) for item in items)
    acos = round(cost / sales * 100, 2) if sales > 0 else None
    return orders, cost, sales, acos


def _evidence(channel: str, items: list[SearchTermPromotionCandidate]) -> list[str]:
    """纯代码派生证据，不追加 LLM 原文。"""
    orders, cost, sales, acos = _metrics(items)
    source_names = ", ".join(sorted({item.campaign_name for item in items if item.campaign_name}))
    terms = sorted({item.search_term for item in items})
    metrics = f"7天订单{orders}，花费${cost:.2f}，销售额${sales:.2f}"
    if acos is not None:
        metrics += f"，ACOS {acos:.2f}%"
    evidence = [f"搜索词提精{channel}：{metrics}" + (f"；来源活动：{source_names}" if source_names else "")]
    if channel == "词根通道" and len(terms) >= 2:
        evidence.append(f"聚合搜索词({len(terms)}个): {', '.join(terms)}")
    return evidence


def _decision(
    keyword: str,
    items: list[SearchTermPromotionCandidate],
    *,
    channel: str,
) -> NewCampaignDecision:
    """代码生成 reason + evidence，不追加 LLM 原文。"""
    source = "SRC_CONVERTED" if any(item.orders > 0 for item in items) else "SRC_BROAD_DERIVED"
    first = items[0]
    orders, cost, sales, acos = _metrics(items)
    reason = (
        f"搜索词提精准（{channel}）：7天订单{orders}，"
        f"花费${cost:.2f}，销售额${sales:.2f}"
        + (f"，ACOS {acos:.2f}%" if acos is not None else "")
        + ("，满足提精准条件" if orders >= 3 else "，词根聚合后满足条件")
    )
    return NewCampaignDecision(
        keyword_text=keyword,
        keyword_class=first.keyword_class,
        relevance_tier="R1",
        source=source,
        trigger_scene="KEYWORD_PROMOTED_FROM_BROAD",
        prescribed_match_type="EXACT",
        reason=reason,
        evidence=_evidence(channel, items),
        confidence="high",
        review_level="MANUAL_REVIEW",
    )


def _is_contiguous_root(root: str, term: str) -> bool:
    """root 必须是 term 的连续子序列。"""
    if not root:
        return False
    root_tokens = root.split()
    term_tokens = term.split()
    w = len(root_tokens)
    return any(term_tokens[i:i + w] == root_tokens for i in range(len(term_tokens) - w + 1))


def build_search_term_promotion_decisions(
    candidates: list[SearchTermPromotionCandidate],
    *,
    existing_exact_keywords: set[str],
    target_acos: float | None,
    root_map: dict[str, str] | None = None,
) -> tuple[list[NewCampaignDecision], list[str]]:
    """按 KB23 §3.4 的当前可验证通道生成 EXACT 决策。

    订单通道需要订单和目标 ACOS；词根通道可由聚合订单独立触发。
    CVR 通道缺少品类基准，刻意不在此处伪造。

    root_map: {search_term: keyword_root}，由 recommend_keyword_roots LLM 产出。
    未提供时词根通道不生效。
    """
    existing = {_normalize(keyword) for keyword in existing_exact_keywords}

    # 回填 root_map 到候选（用于词根聚合）
    if root_map:
        for c in candidates:
            term_key = _normalize(c.search_term)
            root = root_map.get(c.search_term) or root_map.get(term_key) or ""
            if root and _is_contiguous_root(root, c.search_term):
                c.keyword_root = root

    by_term = _aggregate(candidates, lambda item: item.search_term)
    decisions: dict[str, NewCampaignDecision] = {}

    for term, items in by_term.items():
        orders, _cost, sales, acos = _metrics(items)
        if term in existing or target_acos is None or sales <= 0 or acos is None:
            continue
        if orders >= 3 and acos <= float(target_acos):
            decisions[term] = _decision(term, items, channel="订单通道")

    by_root = _aggregate(candidates, lambda item: item.keyword_root)
    for root, items in by_root.items():
        if root in existing or root in decisions:
            continue
        if len(items) < 2:  # KB23 §3.4 信号二：「多个搜索词合计」
            continue
        orders, _cost, sales, acos = _metrics(items)
        acos_ok = target_acos is not None and sales > 0 and acos is not None and acos <= float(target_acos)
        if orders >= 3 or acos_ok:
            decisions[root] = _decision(root, items, channel="词根通道")

    return list(decisions.values()), []
