"""Doris fallback merge helpers (shared by phased_fetcher and skill executor)."""

from __future__ import annotations

from app.config.settings import settings
from app.data.mcp_tool_fallback import BOOTSTRAP_TOOLS, meta_ids_for_failed_tools
from app.models.asin_data import ASINData


def merge_db_fallback(
    base: ASINData,
    db_data: ASINData,
    failed_tools: list[str],
    *,
    meta_ids: list[str] | None = None,
) -> ASINData:
    """将 Doris 回落数据填入 MCP 失败维度（db 优先）。"""
    failed_meta = set(meta_ids or meta_ids_for_failed_tools(failed_tools))
    scope = set(meta_ids) if meta_ids else failed_meta

    if scope & {"META_AD_PRODUCT", "META_AD_PLACEMENT"}:
        if db_data.ad_data and (
            db_data.ad_data.spend
            or db_data.ad_data.orders
            or db_data.ad_data.clicks
            or db_data.ad_data.impressions
        ):
            base.ad_data = db_data.ad_data

    if "META_KW_AD" in scope and db_data.keywords:
        base.keywords = db_data.keywords
        base.keyword_count = len(db_data.keywords)
    elif "META_KW_COMPETITOR_RANK" in scope and db_data.keywords:
        kw_map = {k.keyword: k for k in db_data.keywords}
        for kw in base.keywords:
            if kw.keyword in kw_map:
                dk = kw_map[kw.keyword]
                kw.natural_rank = dk.natural_rank or kw.natural_rank
                kw.near_natural_rank = dk.near_natural_rank or kw.near_natural_rank
                kw.sp_rank = dk.sp_rank or kw.sp_rank
                kw.rank_change_14d = dk.rank_change_14d or kw.rank_change_14d
        if not base.keywords and db_data.keywords:
            base.keywords = db_data.keywords
            base.keyword_count = len(db_data.keywords)

    if "META_FLOW_KEYWORD" in scope:
        base.available_new_keywords = db_data.available_new_keywords
        base.expand_keyword_candidates = db_data.expand_keyword_candidates

    if "META_TREND" in scope and db_data.trend:
        base.trend = db_data.trend
        if db_data.margin is not None:
            base.margin = db_data.margin

    if db_data.natural_order_ratio is not None and base.natural_order_ratio is None:
        base.natural_order_ratio = db_data.natural_order_ratio

    if "META_COMPETITOR" in scope and db_data.competitors:
        base.competitors = db_data.competitors

    if db_data.margin is not None and base.margin is None:
        base.margin = db_data.margin

    if db_data.signals.inventory_qty is not None:
        base.signals = db_data.signals

    if db_data.price is not None:
        base.price = db_data.price
    if db_data.rating is not None:
        base.rating = db_data.rating
    if db_data.review_count is not None:
        base.review_count = db_data.review_count

    return base


def prune_recovered_partial_failures(
    partial_failures: list[str],
    failed_tools: list[str],
    db_data: ASINData,
) -> list[str]:
    """MCP 失败但 Doris 已补全的项，不再向用户展示黄条。"""
    keep: list[str] = []
    for entry in partial_failures:
        if not entry.startswith("mcp:"):
            keep.append(entry)
            continue
        parts = entry.split(":", 2)
        if len(parts) < 2:
            keep.append(entry)
            continue
        tool = parts[1]
        if tool == "listing_inventory":
            if db_data.signals.inventory_qty is not None:
                continue
        elif tool == "ad_keyword_report":
            if db_data.keywords:
                continue
        elif tool in ("keyword_competitors", "keyword_child_asins"):
            if any(k.natural_rank is not None for k in db_data.keywords):
                continue
        elif tool in ("ad_product_report", "ad_placement_report"):
            ad = db_data.ad_data
            if ad and (ad.spend or ad.clicks or ad.impressions):
                continue
            if tool == "ad_placement_report" and ad and any(
                getattr(ad, f, None) is not None
                for f in (
                    "placement_tos_acos",
                    "placement_tos_cpc",
                    "placement_ros_acos",
                    "placement_ros_cpc",
                )
            ):
                continue
        elif tool == "flow_keywords":
            if db_data.expand_keyword_candidates or db_data.available_new_keywords:
                continue
        elif tool == "product_sales":
            if db_data.trend or db_data.natural_order_ratio is not None:
                continue
        elif tool == "direct_competitors":
            if db_data.competitors:
                continue
        keep.append(entry)
    return list(dict.fromkeys(keep))


def resolve_db_fallback_metas(
    failed_tools: list[str],
    meta_ids: list[str],
    *,
    full_scene_meta: bool | None = None,
) -> list[str]:
    """决定 Doris 回落要拉取的 meta 维度。"""
    use_full = full_scene_meta if full_scene_meta is not None else getattr(
        settings, "mcp_failover_full_db", True,
    )
    if use_full:
        return list(meta_ids)
    meta_fb = meta_ids_for_failed_tools(failed_tools)
    if not meta_fb and any(t in BOOTSTRAP_TOOLS for t in failed_tools):
        return list(meta_ids)
    return meta_fb
