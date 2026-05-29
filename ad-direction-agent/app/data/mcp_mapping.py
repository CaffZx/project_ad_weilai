"""MCP tool mapping and parameter builders.

Maps QueryRouter META_* scripts to user-starrocks-data-server tools.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable


@dataclass(frozen=True)
class McpContext:
    parent_asin: str
    parent_seller_sku: str
    shop_account: str
    site_code: str
    start_date: str
    end_date: str


ArgBuilder = Callable[[McpContext], dict]


def make_date_window(days: int) -> tuple[str, str]:
    """Convert N-day window into MCP start/end date string."""
    today = date.today()
    start = today - timedelta(days=max(days, 1))
    return start.isoformat(), today.isoformat()


def _ad_common(ctx: McpContext) -> dict:
    return {
        "parent_asin": ctx.parent_asin,
        "parent_seller_sku": ctx.parent_seller_sku,
        "shop_account": ctx.shop_account,
        "start_date": ctx.start_date,
        "end_date": ctx.end_date,
    }


META_TO_MCP_TOOLS: dict[str, list[str]] = {
    "META_KW_AD": ["ad_keyword_report"],
    "META_COMPETITOR": ["direct_competitors"],
    "META_KW_COMPETITOR_RANK": ["keyword_competitors", "keyword_child_asins"],
    "META_KW_SUB_ASIN_RANK": ["keyword_child_asins"],
    "META_FLOW_KEYWORD": ["flow_keywords"],
    "META_AD_PRODUCT": ["ad_product_report"],
    "META_AD_PLACEMENT": ["ad_placement_report"],
    "META_AD_SEARCH_TERM": ["ad_search_term_report"],
    "META_TREND": ["product_sales"],
}


TOOL_ARG_BUILDERS: dict[str, ArgBuilder] = {
    "listing_basic_info": lambda ctx: {
        "shop_account": ctx.shop_account,
        "parent_asin": ctx.parent_asin,
        "parent_seller_sku": ctx.parent_seller_sku,
    },
    "listing_inventory": lambda ctx: {
        "parent_asin": ctx.parent_asin,
        "parent_seller_sku": ctx.parent_seller_sku,
        "shop_account": ctx.shop_account,
    },
    "product_sales": _ad_common,
    "ad_product_report": _ad_common,
    "ad_placement_report": _ad_common,
    "ad_search_term_report": _ad_common,
    "ad_keyword_report": lambda ctx: _keyword_report_args(ctx),
    "flow_keywords": lambda ctx: {
        "site_code": ctx.site_code,
        "parent_asin": ctx.parent_asin,
        "parent_seller_sku": ctx.parent_seller_sku,
        "shop_account": ctx.shop_account,
    },
    "own_keyword_flow": lambda ctx: {
        "parent_asin": ctx.parent_asin,
        "parent_seller_sku": ctx.parent_seller_sku,
        "shop_account": ctx.shop_account,
    },
    "direct_competitors": lambda ctx: {
        "parent_asin": ctx.parent_asin,
        "parent_seller_sku": ctx.parent_seller_sku,
        "shop_account": ctx.shop_account,
    },
    "keyword_competitors": lambda ctx: {
        "keyword": "",
        "site_code": ctx.site_code,
        "parent_asin": ctx.parent_asin,
        "parent_seller_sku": ctx.parent_seller_sku,
        "shop_account": ctx.shop_account,
    },
    "keyword_child_asins": lambda ctx: {
        "keyword": "",
        "site_code": ctx.site_code,
        "parent_asin": ctx.parent_asin,
        "parent_seller_sku": ctx.parent_seller_sku,
        "shop_account": ctx.shop_account,
    },
}


def _keyword_report_args(ctx: McpContext) -> dict:
    from app.data.mcp_keyword_report import keyword_report_arg_specs

    specs = keyword_report_arg_specs(ctx)
    return specs[0] if specs else {}


def build_tool_args(tool_name: str, ctx: McpContext) -> dict:
    builder = TOOL_ARG_BUILDERS.get(tool_name)
    if not builder:
        return {}
    return {k: v for k, v in builder(ctx).items() if v not in (None, "")}


# ── Campaign 级 MCP 工具 ─────────────────────────────

CAMPAIGN_TOOLS = [
    "ad_campaign_basic_info",           # shop_account, campaign_name
    "ad_campaign_product_report",       # + start_date, end_date
    "ad_campaign_placement_report",     # + start_date, end_date (懒加载)
    "ad_campaign_search_term_report",   # + start_date, end_date (懒加载)
]


def build_campaign_tool_args(
    tool_name: str,
    campaign_name: str,
    shop_account: str,
    start_date: str = "",
    end_date: str = "",
) -> dict:
    """构建 campaign 级 MCP 入参。

    与 TOOL_ARG_BUILDERS（ASIN 级，使用 parent_asin）不同，
    campaign 级工具以 campaign_name 为必填参数。
    """
    base = {
        "shop_account": shop_account,
        "campaign_name": campaign_name,
    }
    # 需要日期范围的报告类工具
    if tool_name in (
        "ad_campaign_product_report",
        "ad_campaign_placement_report",
        "ad_campaign_search_term_report",
    ):
        if start_date:
            base["start_date"] = start_date
        if end_date:
            base["end_date"] = end_date
    return {k: v for k, v in base.items() if v != ""}