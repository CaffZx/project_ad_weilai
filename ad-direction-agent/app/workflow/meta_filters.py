"""Scenario-specific META filters for phased data fetching."""

from __future__ import annotations

from app.config.settings import settings


DEFAULT_META_FILTERS: dict[str, list[str]] = {
    # purpose-agent and tactics keyword panel
    "tactics": [
        "META_AD_PRODUCT",
        "META_KW_AD",
        "META_KW_COMPETITOR_RANK",
        "META_TREND",
        "META_COMPETITOR",
    ],
    # diagnosis card + keyword trend + competitor + placement
    "diagnosis": [
        "META_AD_PRODUCT",
        "META_AD_PLACEMENT",
        "META_KW_AD",
        "META_KW_COMPETITOR_RANK",
        "META_FLOW_KEYWORD",
        "META_TREND",
        "META_COMPETITOR",
    ],
    # recommender mainly needs ad/keyword/trend plus new keyword opportunities
    "execution": [
        "META_AD_PRODUCT",
        "META_AD_PLACEMENT",
        "META_KW_AD",
        "META_KW_COMPETITOR_RANK",
        "META_FLOW_KEYWORD",
        "META_TREND",
    ],
    # p3 pricing/budget relies on ad + keyword + trend and context signals
    "p3": [
        "META_AD_PRODUCT",
        "META_KW_AD",
        "META_FLOW_KEYWORD",
        "META_TREND",
        "META_COMPETITOR",
    ],
}


def get_meta_filter(scene: str) -> list[str]:
    """Resolve scene meta filter from settings with sane defaults."""
    value = getattr(settings, f"meta_filter_{scene}", None)
    if isinstance(value, list) and value:
        return value
    return DEFAULT_META_FILTERS.get(scene, [])
