from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

from app.workflow.steps.campaign_portfolio import (
    ALL_PORTFOLIOS,
    PORTFOLIO_BROAD,
    PORTFOLIO_ELIMINATE,
    PORTFOLIO_MAIN,
    PORTFOLIO_TEST,
)

from .text_utils import map_campaign_group_type

from .models import (
    CampaignPendingCanonical,
    CanonicalRun,
    KeywordPendingCanonical,
    LegacyDetailCanonical,
    PlacementCanonical,
    SuggestCardCanonical,
    stable_id,
    to_decimal,
    to_float,
    to_int,
)

_MATCH_MAP = {
    "EXACT": "EXACT",
    "PHRASE": "PHRASE",
    "BROAD": "BROAD",
}

_PLACEMENT_MAP = {
    "top": "TOP_OF_SEARCH",
    "rest": "REST_OF_SEARCH",
    "product": "PRODUCT_PAGE",
    "top_of_search": "TOP_OF_SEARCH",
    "rest_of_search": "REST_OF_SEARCH",
    "product_page": "PRODUCT_PAGE",
    "头部": "TOP_OF_SEARCH",
    "顶部": "TOP_OF_SEARCH",
    "其他": "REST_OF_SEARCH",
    "商品": "PRODUCT_PAGE",
}

_ACTION_TO_CATEGORY = {
    "eliminate": "ELIMINATE",
    "adjust": "ADJUST",
    "keep": "KEEP",
}

_CATEGORY_PRIORITY = {"ELIMINATE": 0, "ADJUST": 1, "KEEP": 2}

_PORTFOLIO_LABELS = ALL_PORTFOLIOS


def _warehouse_id(value: Any) -> str:
    """Doris / keyword_report 原始 ID（字符串），空则视为缺失。"""
    return str(value or "").strip()


def _build_keyword_id_lookup(payload: dict[str, Any]) -> dict[tuple[str, str, str], str]:
    """(campaign_id, keyword_lower, match_type) -> keyword_id；仅来自 keyword_report 上下文。"""
    lookup: dict[tuple[str, str, str], str] = {}
    for row in payload.get("keyword_id_lookup") or []:
        cid = _warehouse_id(row.get("campaign_id"))
        kid = _warehouse_id(row.get("keyword_id"))
        kw = (row.get("keyword_text") or row.get("keyword") or "").strip().lower()
        mt = _normalize_match_type(row.get("match_type") or "")
        if cid and kid and kw and mt:
            lookup[(cid, kw, mt)] = kid
    for adj in payload.get("adjustments") or []:
        cid = _warehouse_id(adj.get("campaign_id"))
        kid = _warehouse_id(adj.get("keyword_id"))
        kw = (adj.get("keyword_text") or "").strip().lower()
        mt = _normalize_match_type(adj.get("match_type") or "")
        if cid and kid and kw and mt:
            lookup[(cid, kw, mt)] = kid
    return lookup


def _resolve_keyword_id(
    lookup: dict[tuple[str, str, str], str],
    campaign_id: str,
    keyword_text: str | None,
    match_type: str | None,
) -> str:
    cid = _warehouse_id(campaign_id)
    kw = (keyword_text or "").strip().lower()
    mt = _normalize_match_type(match_type or "")
    if not cid or not kw or not mt:
        return ""
    return lookup.get((cid, kw, mt), "")


def _portfolio_groups_from_payload(
    adjustments: list[dict[str, Any]],
    budget_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    counts = {label: 0 for label in _PORTFOLIO_LABELS}
    budget_sums = {label: 0.0 for label in _PORTFOLIO_LABELS}
    for adj in adjustments:
        label = (adj.get("ai_portfolio_class") or adj.get("portfolio") or "").strip()
        if label not in counts:
            continue
        counts[label] += 1
        try:
            budget_sums[label] += float(adj.get("proposed_budget") or adj.get("current_budget") or 0)
        except (TypeError, ValueError):
            pass
    constraints = (budget_summary or {}).get("portfolio_constraints") or {}
    return {
        "main_push_count": counts[PORTFOLIO_MAIN],
        "main_push_budget": constraints.get(PORTFOLIO_MAIN)
        if constraints.get(PORTFOLIO_MAIN) is not None
        else round(budget_sums[PORTFOLIO_MAIN], 2),
        "broad_auto_count": counts[PORTFOLIO_BROAD],
        "broad_auto_budget": constraints.get(PORTFOLIO_BROAD)
        if constraints.get(PORTFOLIO_BROAD) is not None
        else round(budget_sums[PORTFOLIO_BROAD], 2),
        "test_new_count": counts[PORTFOLIO_TEST],
        "test_new_budget": constraints.get(PORTFOLIO_TEST)
        if constraints.get(PORTFOLIO_TEST) is not None
        else round(budget_sums[PORTFOLIO_TEST], 2),
        "eliminate_bubble_count": counts[PORTFOLIO_ELIMINATE],
        "eliminate_bubble_budget": round(budget_sums[PORTFOLIO_ELIMINATE], 2),
    }


def _clip(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    s = str(value)
    if len(s) <= limit:
        return s
    return s[:limit]


def _utc_from_iso(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc).replace(tzinfo=None)
    raw = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _batch_no(experiment_id: str, run_number: int) -> str:
    """对外批次号：run_id(UTC, 形如 20260613T051742Z) → 本地 YYYYMMDD-HHMM-序号。

    从 run_id 派生（非 now()）→ 同一 run_id 重试（决策批次进行中事件失败重跑）时
    batch_no 稳定不漂移；与历史 7 天数据 YYYYMMDD-HHMM-序号 格式一致。
    run_id 非该格式（旧/外部）→ 兜底本地当前时间。
    """
    try:
        dt = datetime.strptime(experiment_id, "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc
        ).astimezone()
        return f"{dt.strftime('%Y%m%d-%H%M')}-{run_number:02d}"
    except (ValueError, TypeError):
        return f"{datetime.now().strftime('%Y%m%d-%H%M')}-{run_number:02d}"


def _confidence_level(score: int | float | None) -> str:
    if score is None:
        return "low"
    if score >= 80:
        return "high"
    if score >= 50:
        return "medium"
    return "low"


def _category_from_action(action: str | None) -> str:
    if not action:
        return "ADJUST"
    return _ACTION_TO_CATEGORY.get(action.lower(), "ADJUST")


def _normalize_match_type(match_type: str | None) -> str | None:
    if not match_type:
        return None
    return _MATCH_MAP.get(match_type.upper(), match_type.upper())


def _normalize_placement(value: str | None) -> str | None:
    if not value:
        return None
    v = str(value).strip()
    if not v:
        return None
    key = v.lower()
    mapped = _PLACEMENT_MAP.get(key)
    if mapped:
        return mapped
    # fallback for already-standard enum values
    key2 = key.replace("-", "_")
    mapped = _PLACEMENT_MAP.get(key2)
    if mapped:
        return mapped
    return _PLACEMENT_MAP.get(v)


def _pick_primary_adjustment(items: list[tuple[int, dict[str, Any]]]) -> tuple[int, dict[str, Any]]:
    """Prefer ELIMINATE card for display; else lowest sort index."""
    items_sorted = sorted(
        items,
        key=lambda t: (
            _CATEGORY_PRIORITY.get(_category_from_action(t[1].get("action")), 1),
            t[0],
        ),
    )
    return items_sorted[0]


def _pending_lists_from_adjustment(
    adj: dict[str, Any],
    *,
    parent_asin: str,
    campaign_id: str,
    keyword_text: str | None,
    match_type: str | None,
    kw_lookup: dict[tuple[str, str, str], str],
) -> tuple[list[KeywordPendingCanonical], list[CampaignPendingCanonical], list[PlacementCanonical]]:
    keyword_pending: list[KeywordPendingCanonical] = []
    if campaign_id and any(v is not None for v in (adj.get("current_bid"), adj.get("proposed_bid"))):
        kw_id = _warehouse_id(adj.get("keyword_id")) or _resolve_keyword_id(
            kw_lookup, campaign_id, keyword_text, match_type
        )
        if kw_id:
            keyword_pending.append(
                KeywordPendingCanonical(
                    keyword_id=kw_id,
                    keyword_text=keyword_text,
                    match_type=match_type,
                    old_state=None,
                    new_state=None,
                    old_bid=to_decimal(adj.get("current_bid")),
                    new_bid=to_decimal(adj.get("proposed_bid")),
                )
            )
        else:
            logger.warning(
                "[%s] 无 keyword_id（campaign_id=%s keyword=%s match=%s），跳过 bid pending",
                parent_asin,
                campaign_id,
                keyword_text,
                match_type,
            )

    campaign_pending: list[CampaignPendingCanonical] = []
    if campaign_id and any(
        v is not None for v in (adj.get("current_budget"), adj.get("proposed_budget"))
    ):
        campaign_pending.append(
            CampaignPendingCanonical(
                old_state=None,
                new_state=None,
                old_budget=to_decimal(adj.get("current_budget")),
                new_budget=to_decimal(adj.get("proposed_budget")),
            )
        )

    placements: list[PlacementCanonical] = []
    for plc in adj.get("placement_adjustments") or []:
        if not campaign_id:
            continue
        placement_type = _normalize_placement(plc.get("placement"))
        if not placement_type:
            continue
        placements.append(
            PlacementCanonical(
                placement_type=placement_type,
                old_percent=to_decimal(plc.get("current_pct")),
                new_percent=to_decimal(plc.get("proposed_pct")),
                adjust_action=plc.get("action"),
                remark=plc.get("evidence"),
            )
        )

    for neg in adj.get("negative_keywords") or []:
        if not campaign_id:
            continue
        neg_text = neg.get("keyword")
        neg_mt = _normalize_match_type(neg.get("match_type") or "NEGATIVE")
        kw_id = _resolve_keyword_id(kw_lookup, campaign_id, neg_text, neg_mt)
        if not kw_id:
            logger.warning(
                "[%s] 否定词无 keyword_id（campaign_id=%s keyword=%s），跳过",
                parent_asin,
                campaign_id,
                neg_text,
            )
            continue
        keyword_pending.append(
            KeywordPendingCanonical(
                keyword_id=kw_id,
                keyword_text=neg_text,
                match_type="NEGATIVE",
                old_state=None,
                new_state="PAUSED",
                old_bid=None,
                new_bid=None,
            )
        )
    return keyword_pending, campaign_pending, placements


def canonicalize_payload(
    payload: dict[str, Any],
    *,
    shop_id: int | None = None,
    parent_seller_sku: str | None = None,
    site_code: str | None = None,
) -> CanonicalRun:
    parent_asin = payload.get("parent_asin") or ""
    experiment_id = payload.get("experiment_id") or "exp"
    run_number = to_int(payload.get("run_number")) or 1
    decision_id = stable_id("dec", parent_asin, experiment_id, run_number)
    # batch_no 对外展示：从 run_id 派生本地 YYYYMMDD-HHMM-序号（复刻旧版，重试稳定）；
    # 不参与任何唯一键，纯标记。decision_id 仍用 experiment_id(run_id) 保可追溯。
    batch_no = _batch_no(experiment_id, run_number)
    timestamp = _utc_from_iso(payload.get("timestamp"))
    summary = payload.get("summary") or {}

    cards: list[SuggestCardCanonical] = []
    legacy_details: list[LegacyDetailCanonical] = []
    metrics_rows: list[dict[str, Any]] = []

    effective_shop_id = shop_id if shop_id is not None else payload.get("shop_id")
    if effective_shop_id is not None:
        try:
            effective_shop_id = int(effective_shop_id)
        except (TypeError, ValueError):
            effective_shop_id = None

    adjustments = payload.get("adjustments") or []
    kw_lookup = _build_keyword_id_lookup(payload)

    by_campaign: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for idx, adj in enumerate(adjustments, start=1):
        campaign_key = adj.get("campaign_key") or adj.get("campaign_name") or str(idx)
        campaign_id = _clip(_warehouse_id(adj.get("campaign_id")), 64)
        if not campaign_id:
            logger.warning(
                "[%s] adjustment[%d] 缺少 Doris campaign_id（活动=%s），跳过该条",
                parent_asin,
                idx,
                adj.get("campaign_name") or campaign_key,
            )
            continue
        by_campaign.setdefault(campaign_id, []).append((idx, adj))

    for campaign_id, group in by_campaign.items():
        sort_order = min(i for i, _ in group)
        primary_idx, primary_adj = _pick_primary_adjustment(group)
        match_type = _normalize_match_type(primary_adj.get("match_type"))
        keyword_text = _clip(primary_adj.get("keyword_text"), 512)
        # confidence 是字符串 high/medium/low（非数字分数）；直接归一，勿走 to_int
        confidence = str(primary_adj.get("confidence") or "medium").strip().lower()
        if confidence not in ("high", "medium", "low"):
            confidence = "medium"
        evidence_list = primary_adj.get("evidence") or []
        evidence_text = "\n".join(str(x) for x in evidence_list if x)
        description = primary_adj.get("reason")
        portfolio_label = (primary_adj.get("ai_portfolio_class") or primary_adj.get("portfolio") or "").strip()
        campaign_group_type = map_campaign_group_type(portfolio_label)

        keyword_pending: list[KeywordPendingCanonical] = []
        campaign_pending: list[CampaignPendingCanonical] = []
        placements_by_type: dict[str, PlacementCanonical] = {}
        seen_kw_pending: set[tuple[str, str]] = set()

        for idx, adj in group:
            match_type_adj = _normalize_match_type(adj.get("match_type"))
            keyword_text_adj = _clip(adj.get("keyword_text"), 512)
            kw_list, camp_list, plc_list = _pending_lists_from_adjustment(
                adj,
                parent_asin=parent_asin,
                campaign_id=campaign_id,
                keyword_text=keyword_text_adj,
                match_type=match_type_adj,
                kw_lookup=kw_lookup,
            )
            for kw in kw_list:
                key = (kw.keyword_id, kw.match_type or "")
                if key in seen_kw_pending:
                    continue
                seen_kw_pending.add(key)
                keyword_pending.append(kw)
            if camp_list:
                campaign_pending = camp_list
            for plc in plc_list:
                placements_by_type[plc.placement_type] = plc

            legacy_content = {
                "campaign_name": adj.get("campaign_name"),
                "campaign_key": adj.get("campaign_key"),
                "child_asin": adj.get("child_asin"),
                "keyword_text": keyword_text_adj,
                "match_type": match_type_adj,
                "action": adj.get("action"),
                "reason": adj.get("reason"),
                "evidence": adj.get("evidence") or [],
                "confidence": to_int(adj.get("confidence")),
                "current_budget": to_float(adj.get("current_budget")),
                "proposed_budget": to_float(adj.get("proposed_budget")),
                "current_bid": to_float(adj.get("current_bid")),
                "proposed_bid": to_float(adj.get("proposed_bid")),
                "placement_adjustments": adj.get("placement_adjustments") or [],
                "negative_keywords": adj.get("negative_keywords") or [],
            }
            legacy_details.append(
                LegacyDetailCanonical(
                    detail_id=stable_id("det", decision_id, campaign_id, idx),
                    direction_recommend_id=stable_id("rec", decision_id),
                    decision_id=decision_id,
                    direction_type=str(adj.get("action") or "adjust").upper(),
                    recommend_tag=_category_from_action(adj.get("action")),
                    suggest_score=to_int(adj.get("confidence")),
                    sort_order=idx,
                    content_json=json.dumps(legacy_content, ensure_ascii=False),
                )
            )

        cards.append(
            SuggestCardCanonical(
                card_id=stable_id("car", decision_id, campaign_id),
                decision_id=decision_id,
                campaign_id=campaign_id,
                campaign_name=_clip(primary_adj.get("campaign_name") or campaign_id, 512)
                or campaign_id,
                asin=_clip(primary_adj.get("child_asin"), 64),
                keyword=keyword_text,
                keyword_match_type=match_type,
                trigger_rule=_clip(primary_adj.get("triggered_rule"), 128),
                suggest_category=_category_from_action(primary_adj.get("action")),
                confidence_level=confidence,
                campaign_group_type=campaign_group_type,
                description=description,
                evidence=evidence_text,
                sort_order=sort_order,
                current_budget=to_decimal(primary_adj.get("current_budget")),
                proposed_budget=to_decimal(primary_adj.get("proposed_budget")),
                current_bid=to_decimal(primary_adj.get("current_bid")),
                proposed_bid=to_decimal(primary_adj.get("proposed_bid")),
                campaign_key=primary_adj.get("campaign_key") or "",
                keyword_class=primary_adj.get("keyword_class") or None,
                review_level=primary_adj.get("review_level") or None,
                is_core=bool(primary_adj.get("is_core")),
                placements=list(placements_by_type.values()),
                keyword_pending=keyword_pending,
                campaign_pending=campaign_pending,
            )
        )

    # ── 新增活动 → CREATE 卡（campaign_id 空；pending old=NULL 表"从无到有"）──
    create_count = 0
    for nc in payload.get("new_campaigns") or []:
        if not isinstance(nc, dict):
            continue
        name = nc.get("campaign_name") or nc.get("keyword_text") or ""
        if not name:
            continue
        seed = f"new:{name}"
        kw_text = _clip(nc.get("keyword_text"), 512)
        mt = _normalize_match_type(nc.get("match_type"))
        bid = to_decimal(nc.get("proposed_base_bid"))
        budget = to_decimal(nc.get("proposed_daily_budget"))
        nc_kw: list[KeywordPendingCanonical] = []
        if bid is not None:
            nc_kw.append(KeywordPendingCanonical(
                keyword_id=stable_id("nkw", decision_id, seed),  # 新活动无真 keyword_id，合成确定性 id
                keyword_text=kw_text, match_type=mt,
                old_state=None, new_state="ENABLED", old_bid=None, new_bid=bid))
        nc_camp: list[CampaignPendingCanonical] = []
        if budget is not None:
            nc_camp.append(CampaignPendingCanonical(
                old_state=None, new_state="ENABLED", old_budget=None, new_budget=budget))
        nc_plc: list[PlacementCanonical] = []
        primary = _normalize_placement(nc.get("primary_placement"))
        if primary:
            nc_plc.append(PlacementCanonical(
                placement_type=primary, old_percent=None, new_percent=None,
                adjust_action="首轮主投", remark=_clip(nc.get("negative_strategy"), 512) or None))
        cards.append(SuggestCardCanonical(
            card_id=stable_id("car", decision_id, seed),
            decision_id=decision_id, campaign_id=None,
            campaign_name=_clip(name, 512) or name, asin=_clip(nc.get("child_asin"), 64),
            keyword=kw_text, keyword_match_type=mt,
            trigger_rule=_clip(nc.get("trigger_scene"), 128),
            suggest_category="CREATE",
            confidence_level=str(nc.get("confidence") or "medium"),
            campaign_group_type=map_campaign_group_type((nc.get("ai_portfolio_class") or "").strip()),
            description=nc.get("reason"),
            evidence="\n".join(str(x) for x in (nc.get("evidence") or []) if x),
            sort_order=900,
            current_budget=None, proposed_budget=budget, current_bid=None, proposed_bid=bid,
            campaign_key=name, keyword_class=nc.get("keyword_class") or None,
            review_level=nc.get("review_level") or None, is_core=False,
            placements=nc_plc, keyword_pending=nc_kw, campaign_pending=nc_camp))
        create_count += 1

    # ── 预过滤活动 → prefiltered 卡（灰卡，不可执行，无 pending；lost 不落库）──
    for sk in payload.get("skipped_campaigns") or []:
        if not isinstance(sk, dict) or not sk.get("__prefiltered"):
            continue
        ckey = sk.get("campaign_key") or sk.get("campaign_name") or ""
        if not ckey:
            continue
        reason = sk.get("reason") or ""
        kcount = sk.get("keyword_count")
        if kcount and "词" not in reason:
            reason = f"{reason}（{kcount}词）" if reason else f"多关键词活动（{kcount}词）"
        cards.append(SuggestCardCanonical(
            card_id=stable_id("car", decision_id, f"pref:{ckey}"),
            decision_id=decision_id, campaign_id=None,
            campaign_name=_clip(sk.get("campaign_name") or ckey, 512) or ckey,
            asin=_clip(sk.get("child_asin"), 64),
            keyword=_clip(sk.get("keyword_text"), 512),
            keyword_match_type=_normalize_match_type(sk.get("match_type")),
            trigger_rule=None, suggest_category=None, confidence_level="low",
            campaign_group_type=map_campaign_group_type((sk.get("portfolio") or "").strip()),
            description=None, evidence="", sort_order=950,
            current_budget=None, proposed_budget=None, current_bid=None, proposed_bid=None,
            campaign_key=ckey, is_prefiltered=True, prefilter_reason=_clip(reason, 255)))

    # ── 总览文本（落 summary.analysis_overview）──
    _ov = payload.get("strategic_overview") or {}
    overview_text = None
    if isinstance(_ov, dict):
        _parts = []
        if _ov.get("assessment_text"):
            _parts.append("【判断】" + str(_ov["assessment_text"]))
        if _ov.get("direction_text"):
            _parts.append("【方向】" + str(_ov["direction_text"]))
        overview_text = "\n\n".join(_parts) or None

    # ── 汇总（落 synthesis 三表）──
    synthesis = payload.get("synthesis")
    if not isinstance(synthesis, dict):
        synthesis = {}

    strategy = payload.get("strategy_context") or {}
    # v2 metrics model: one SUMMARY row always; DAILY rows only when source provides day-level metrics.
    metrics_rows.append(
        {
            "id": stable_id("met", decision_id, "SUMMARY"),
            "decision_id": decision_id,
            "metrics_type": "SUMMARY",
            "day_str": "SUMMARY",
            "avg_daily_sale_num": to_decimal(strategy.get("avg_daily_sales_30d")),
            "acos": None,
            "organic_order_rate": to_decimal(strategy.get("natural_order_ratio")),
            "tacos": None,
            "overall_cvr": None,
            "cvr": None,
            "ctr": None,
            "cpc": None,
            "daily_cost": None,
        }
    )

    trend_rows = payload.get("daily_metrics") or []
    for item in trend_rows:
        day_str = item.get("day_str")
        if not day_str:
            continue
        metrics_rows.append(
            {
                "id": stable_id("met", decision_id, "DAILY", day_str),
                "decision_id": decision_id,
                "metrics_type": "DAILY",
                "day_str": day_str,
                "avg_daily_sale_num": None,
                "acos": to_decimal(item.get("acos")),
                "organic_order_rate": None,
                "tacos": None,
                "overall_cvr": None,
                "cvr": to_decimal(item.get("cvr")),
                "ctr": to_decimal(item.get("ctr")),
                "cpc": to_decimal(item.get("cpc")),
                "daily_cost": to_decimal(item.get("daily_cost")),
            }
        )

    budget_summary = payload.get("budget_summary") or {}
    budget_groups = _portfolio_groups_from_payload(adjustments, budget_summary)
    if payload.get("portfolio_counts"):
        pc = payload["portfolio_counts"]
        for key in (
            "main_push_count",
            "broad_auto_count",
            "test_new_count",
            "eliminate_bubble_count",
        ):
            if key in pc:
                budget_groups[key] = pc[key]

    return CanonicalRun(
        decision_id=decision_id,
        batch_no=batch_no,
        parent_asin=parent_asin,
        experiment_id=experiment_id,
        run_number=run_number,
        timestamp=timestamp,
        total_campaigns=to_int(payload.get("total_campaigns")) or len(cards),
        sanity_check_passed=bool(payload.get("sanity_check_passed")),
        summary=summary,
        cards=cards,
        legacy_details=legacy_details,
        metrics_rows=metrics_rows,
        raw_payload=payload,
        shop_id=effective_shop_id,
        parent_seller_sku=parent_seller_sku,
        site_code=site_code,
        budget_groups=budget_groups,
        decision_meta=payload.get("decision_meta") or {},
        overview_text=overview_text,
        synthesis=synthesis,
        create_count=create_count,
    )

