"""Detect MCP tools that succeeded but returned no usable rows."""

from __future__ import annotations

from app.data.mcp_mapping import META_TO_MCP_TOOLS
from app.data.mcp_normalizers import (
    _as_rows,
    normalize_ad_summary,
    normalize_competitors,
    normalize_flow_keywords,
    normalize_keywords,
    normalize_listing_basic_info,
    normalize_product_sales,
    normalize_trend,
)
from app.models.asin_data import ASINData

# meta_id -> (tool_name, checker on normalized fragment or ASINData field)
_META_EMPTY_CHECKS: list[tuple[str, str, str]] = [
    ("META_AD_PRODUCT", "ad_product_report", "ad"),
    ("META_KW_AD", "ad_keyword_report", "keywords"),
    ("META_TREND", "product_sales", "trend"),
    ("META_COMPETITOR", "direct_competitors", "competitors"),
    ("META_FLOW_KEYWORD", "flow_keywords", "flow"),
    ("META_AD_PLACEMENT", "ad_placement_report", "placement"),
]


def _has_ad_metrics(data: ASINData) -> bool:
    ad = data.ad_data
    if not ad:
        return False
    return any(
        getattr(ad, f, None) not in (None, 0, 0.0)
        for f in ("spend", "clicks", "impressions", "sales", "orders", "acos")
    )


def _tool_payload_empty(tool: str, payload) -> bool:
    if payload is None:
        return True
    if tool == "listing_basic_info_v2":
        return not normalize_listing_basic_info(payload)
    if tool == "ad_product_report":
        return not normalize_ad_summary(payload)
    if tool == "ad_keyword_report":
        return not normalize_keywords(payload)
    if tool == "product_sales":
        rows = normalize_trend(payload)
        ps = normalize_product_sales(payload)
        return not rows and not (ps.get("total_sales") or ps.get("total_orders"))
    if tool == "direct_competitors":
        return not normalize_competitors(payload)
    if tool == "flow_keywords":
        return not normalize_flow_keywords(payload)
    if tool == "ad_placement_report":
        return not _as_rows(payload)
    if tool == "parent_listing_stock_summary":
        return not _as_rows(payload)
    return not _as_rows(payload)


def _data_field_empty(check_key: str, data: ASINData) -> bool:
    if check_key == "ad":
        return not _has_ad_metrics(data)
    if check_key == "keywords":
        return data.keyword_count <= 0 and not data.keywords
    if check_key == "trend":
        return not data.trend
    if check_key == "competitors":
        return not data.competitors
    if check_key == "flow":
        return not data.expand_keyword_candidates and (data.available_new_keywords or 0) <= 0
    if check_key == "placement":
        ad = data.ad_data
        if not ad:
            return True
        return not any(
            getattr(ad, f, None) is not None
            for f in (
                "placement_tos_acos",
                "placement_tos_cpc",
                "placement_ros_acos",
                "placement_ros_cpc",
            )
        )
    return False


def empty_report_tools(
    data: ASINData,
    payload_map: dict,
    missing_fields: list[str],
    meta_ids: list[str] | None,
) -> list[str]:
    """MCP 调用成功但无可用行、且组装后仍空的工具名（用于 Doris 回落）。"""
    missing_set = set(missing_fields or [])
    meta_set = set(meta_ids or [])
    tools: list[str] = []

    for meta_id, tool, check_key in _META_EMPTY_CHECKS:
        if meta_id not in meta_set:
            continue
        if tool in missing_set:
            continue
        if tool not in payload_map:
            continue
        if not _tool_payload_empty(tool, payload_map.get(tool)):
            continue
        if not _data_field_empty(check_key, data):
            continue
        tools.append(tool)

    if (
        "listing_basic_info_v2" in payload_map
        and "listing_basic_info_v2" not in missing_set
        and normalize_listing_basic_info(payload_map.get("listing_basic_info_v2"))
        and "parent_listing_stock_summary" in payload_map
        and "parent_listing_stock_summary" not in missing_set
        and _tool_payload_empty("parent_listing_stock_summary", payload_map.get("parent_listing_stock_summary"))
    ):
        tools.append("parent_listing_stock_summary")

    return list(dict.fromkeys(tools))


def append_empty_report_failures(
    data: ASINData,
    payload_map: dict,
    missing_fields: list[str],
    meta_ids: list[str] | None,
) -> list[str]:
    """Return mcp:{tool}:empty entries for successful tools with no rows."""
    return [f"mcp:{tool}:empty" for tool in empty_report_tools(
        data, payload_map, missing_fields, meta_ids,
    )]


def tools_for_meta(meta_ids: list[str] | None) -> set[str]:
    ids = meta_ids or list(META_TO_MCP_TOOLS.keys())
    out: set[str] = set()
    for meta in ids:
        out.update(META_TO_MCP_TOOLS.get(meta, []))
    return out
