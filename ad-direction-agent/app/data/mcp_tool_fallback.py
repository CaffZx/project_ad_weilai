"""MCP 工具名 → 数仓 META_* 维度映射（单工具超时回落）。"""

from __future__ import annotations

MCP_TOOL_TO_META: dict[str, str] = {
    "ad_product_report": "META_AD_PRODUCT",
    "ad_placement_report": "META_AD_PLACEMENT",
    "ad_keyword_report": "META_KW_AD",
    "ad_search_term_report": "META_AD_SEARCH_TERM",
    "keyword_competitors": "META_KW_COMPETITOR_RANK",
    "keyword_child_asins": "META_KW_COMPETITOR_RANK",
    "flow_keywords": "META_FLOW_KEYWORD",
    "product_sales": "META_TREND",
    "direct_competitors": "META_COMPETITOR",
    "own_keyword_flow": "META_FLOW_KEYWORD",
}

BOOTSTRAP_TOOLS = ("listing_basic_info", "listing_inventory")

PHASE_B_TOOLS = (
    "ad_product_report",
    "ad_placement_report",
    "ad_keyword_report",
    "keyword_competitors",
    "keyword_child_asins",
    "flow_keywords",
    "product_sales",
    "direct_competitors",
)


def meta_ids_for_failed_tools(failed_tools: list[str]) -> list[str]:
    """将失败 MCP 工具列表转为 DbAdapter meta_filter。"""
    meta: list[str] = []
    for tool in failed_tools:
        m = MCP_TOOL_TO_META.get(tool)
        if m and m not in meta:
            meta.append(m)
    return meta
