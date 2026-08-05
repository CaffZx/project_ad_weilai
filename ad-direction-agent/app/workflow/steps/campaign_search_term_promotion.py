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
    orders, cost, sales, acos = _metrics(items)
    source_names = ", ".join(sorted({item.campaign_name for item in items if item.campaign_name}))
    metrics = f"7天订单{orders}，花费${cost:.2f}，销售额${sales:.2f}"
    if acos is not None:
        metrics += f"，ACOS {acos:.2f}%"
    evidence = [f"搜索词提精{channel}：{metrics}" + (f"；来源活动：{source_names}" if source_names else "")]
    for item in items:
        for line in item.evidence:
            if line and line not in evidence:
                evidence.append(line)
    return evidence


def _decision(
    keyword: str,
    items: list[SearchTermPromotionCandidate],
    *,
    channel: str,
) -> NewCampaignDecision:
    source = "SRC_CONVERTED" if any(item.orders > 0 for item in items) else "SRC_BROAD_DERIVED"
    first = items[0]
    # 聚合全来源的 distinct reason（与 evidence 同理：多个活动的同词候选不应只保留首条）
    reasons: list[str] = []
    for item in items:
        if item.reason and item.reason not in reasons:
            reasons.append(item.reason)
    return NewCampaignDecision(
        keyword_text=keyword,
        keyword_class=first.keyword_class,
        relevance_tier="R1",
        source=source,
        trigger_scene="KEYWORD_PROMOTED_FROM_BROAD",
        prescribed_match_type="EXACT",
        reason="；".join(reasons) if reasons else "",
        evidence=_evidence(channel, items),
        confidence="high",
        review_level="MANUAL_REVIEW",
    )


def build_search_term_promotion_decisions(
    candidates: list[SearchTermPromotionCandidate],
    *,
    existing_exact_keywords: set[str],
    target_acos: float | None,
) -> tuple[list[NewCampaignDecision], list[str]]:
    """按 KB23 §3.4 的当前可验证通道生成 EXACT 决策。

    订单通道需要订单和目标 ACOS；词根通道可由聚合订单独立触发。
    CVR 通道缺少品类基准，刻意不在此处伪造。
    """
    existing = {_normalize(keyword) for keyword in existing_exact_keywords}
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
        orders, _cost, sales, acos = _metrics(items)
        acos_ok = target_acos is not None and sales > 0 and acos is not None and acos <= float(target_acos)
        if orders >= 3 or acos_ok:
            decisions[root] = _decision(root, items, channel="词根通道")

    return list(decisions.values()), []
