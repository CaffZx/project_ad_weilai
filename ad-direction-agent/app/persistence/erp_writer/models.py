from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from hashlib import md5
from typing import Any


def stable_id(prefix: str, *parts: Any) -> str:
    raw = "|".join("" if p is None else str(p) for p in parts)
    digest = md5(raw.encode("utf-8")).hexdigest()  # noqa: S324
    return f"{prefix}{digest[:29]}"


def warehouse_pending_id(
    prefix: str,
    decision_id: str,
    campaign_id: str,
    *extra: Any,
) -> str:
    """pending 表主键：仅由 decision_id + 数仓 campaign_id/keyword_id 等确定性派生。"""
    return stable_id(prefix, decision_id, str(campaign_id or "").strip(), *extra)


def to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except Exception:
        return None


def to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except Exception:
        return None


@dataclass(slots=True)
class PlacementCanonical:
    placement_type: str
    old_percent: Decimal | None
    new_percent: Decimal | None
    adjust_action: str | None
    remark: str | None


@dataclass(slots=True)
class KeywordPendingCanonical:
    keyword_id: str
    keyword_text: str | None
    match_type: str | None
    old_state: str | None
    new_state: str | None
    old_bid: Decimal | None
    new_bid: Decimal | None


@dataclass(slots=True)
class CampaignPendingCanonical:
    old_state: str | None
    new_state: str | None
    old_budget: Decimal | None
    new_budget: Decimal | None


@dataclass(slots=True)
class SuggestCardCanonical:
    card_id: str
    decision_id: str
    campaign_id: str
    campaign_name: str
    asin: str | None
    keyword: str | None
    keyword_match_type: str | None
    trigger_rule: str | None
    suggest_category: str
    confidence_level: str
    campaign_group_type: str | None
    description: str | None
    evidence: str | None
    sort_order: int
    current_budget: Decimal | None
    proposed_budget: Decimal | None
    current_bid: Decimal | None
    proposed_bid: Decimal | None
    placements: list[PlacementCanonical] = field(default_factory=list)
    keyword_pending: list[KeywordPendingCanonical] = field(default_factory=list)
    campaign_pending: list[CampaignPendingCanonical] = field(default_factory=list)


@dataclass(slots=True)
class LegacyDetailCanonical:
    detail_id: str
    direction_recommend_id: str
    decision_id: str
    direction_type: str
    recommend_tag: str
    suggest_score: int | None
    sort_order: int
    content_json: str


@dataclass(slots=True)
class CanonicalRun:
    decision_id: str
    batch_no: str
    parent_asin: str
    experiment_id: str
    run_number: int
    timestamp: datetime
    total_campaigns: int
    sanity_check_passed: bool
    summary: dict[str, Any]
    cards: list[SuggestCardCanonical]
    legacy_details: list[LegacyDetailCanonical]
    metrics_rows: list[dict[str, Any]]
    raw_payload: dict[str, Any]
    shop_id: int | None = None
    parent_seller_sku: str | None = None
    site_code: str | None = None
    budget_groups: dict[str, Any] = field(default_factory=dict)
    decision_meta: dict[str, Any] = field(default_factory=dict)

