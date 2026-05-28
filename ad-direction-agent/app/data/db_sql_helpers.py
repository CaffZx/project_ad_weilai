"""SQL helpers for DbAdapter — listing context and IN-clause builders."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.config.settings import settings

logger = logging.getLogger(__name__)

# Per-META Doris fallback timeouts (seconds) when MCP tool fails
META_FALLBACK_TIMEOUTS: dict[str, float] = {
    "META_FLOW_KEYWORD": 25.0,
    "META_KW_AD": 40.0,
    "META_KW_COMPETITOR_RANK": 35.0,
    "META_KW_SUB_ASIN_RANK": 35.0,
    "META_AD_PRODUCT": 45.0,
    "META_AD_PLACEMENT": 45.0,
    "META_TREND": 45.0,
    "META_COMPETITOR": 30.0,
}


def meta_fallback_timeout(meta_ids: list[str]) -> float:
    """Max timeout when falling back multiple META dimensions."""
    full = float(getattr(settings, "db_failover_timeout", 0) or 0)
    if full > 0:
        return full
    if not meta_ids:
        return float(getattr(settings, "db_fetch_timeout", 180.0))
    return max(
        META_FALLBACK_TIMEOUTS.get(m, float(getattr(settings, "db_fetch_timeout", 180.0)))
        for m in meta_ids
    )


@dataclass(frozen=True)
class ListingContext:
    parent_asin: str
    parent_seller_sku: str
    shop_id: int
    child_asins: list[str]

    @classmethod
    def from_listing_row(cls, listing: dict) -> ListingContext:
        """Build from _resolve_and_fetch_listing result."""
        cap = int(getattr(settings, "db_child_asin_cap", 80))
        raw_pairs = listing.get("child_asins") or []
        follow_up = listing.get("child_asins_follow_up")
        if follow_up is not None:
            asins = list(follow_up)
        else:
            asins = [p[0] for p in raw_pairs if p and p[0]]

        asins = list(dict.fromkeys(asins))
        if len(asins) > cap:
            logger.warning(
                "child_asins capped %d -> %d for parent %s",
                len(asins),
                cap,
                listing.get("parent_asin"),
            )
            asins = asins[:cap]

        return cls(
            parent_asin=str(listing.get("parent_asin") or ""),
            parent_seller_sku=str(listing.get("parent_seller_sku") or ""),
            shop_id=int(listing.get("shop_id") or 0),
            child_asins=asins,
        )


def build_in_clause(column: str, values: list[str]) -> tuple[str, tuple]:
    """Return SQL fragment ``col IN (%s,...)`` and value tuple."""
    if not values:
        return "1=0", ()
    placeholders = ",".join(["%s"] * len(values))
    return f"{column} IN ({placeholders})", tuple(values)
