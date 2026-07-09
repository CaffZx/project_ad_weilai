"""MCP tool mapping and parameter builders.

Maps QueryRouter META_* scripts to user-starrocks-data-server tools.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class McpContext:
    parent_asin: str
    parent_seller_sku: str
    shop_account: str
    site_code: str
    start_date: str
    end_date: str


ArgBuilder = Callable[[McpContext], dict]


# 各站点广告数据按「当地时间」存储——覆盖实跑出现过的 6 个站点（日志核实），硬编码其时区；
# ZoneInfo 自动处理夏令时。其余罕见站点回落默认站并告警（出现即可发现，再按需补行）。
_SITE_TZ: dict[str, ZoneInfo] = {
    "Amazon_US": ZoneInfo("America/Los_Angeles"),  # PST/PDT
    "Amazon_UK": ZoneInfo("Europe/London"),        # GMT/BST
    "Amazon_DE": ZoneInfo("Europe/Berlin"),        # CET/CEST
    "Amazon_IT": ZoneInfo("Europe/Rome"),          # CET/CEST
    "Amazon_ES": ZoneInfo("Europe/Madrid"),        # CET/CEST
    "Amazon_FR": ZoneInfo("Europe/Paris"),         # CET/CEST
}
_DEFAULT_SITE = "Amazon_US"


def _site_today(site_code: str) -> date:
    """该站点「当地时间」的今天（数仓按当地时间存）。未知站点回落默认站并告警。"""
    tz = _SITE_TZ.get(site_code)
    if tz is None:
        logger.warning("make_date_window: 未知 site_code=%r，回落 %s 时区", site_code, _DEFAULT_SITE)
        tz = _SITE_TZ[_DEFAULT_SITE]
    return datetime.now(tz).date()


def make_date_window(days: int, site_code: str = "") -> tuple[str, str]:
    """N 天窗口 → MCP start/end 日期串（按 site 当地时间口径）。

    数仓按各站点当地时间存、且当天数据不完整，故窗口取「当地最新日的前一天」往前数 N 天：
      end_date   = 当地今天 - 1     （最后一个完整日，排除未完整的当天）
      start_date = 当地今天 - days   （[today-days, today-1] 含两端共 days 天）
    例：days=7、当地今天=06-24 → start=06-17, end=06-23（7 天）。
    site_code 缺省时按默认站(Amazon_US)。
    """
    today = _site_today(site_code or _DEFAULT_SITE)
    n = max(days, 1)
    start = today - timedelta(days=n)
    end = today - timedelta(days=1)
    return start.isoformat(), end.isoformat()


def _ad_common(ctx: McpContext) -> dict:
    return {
        "parent_asin": ctx.parent_asin,
        "parent_seller_sku": ctx.parent_seller_sku,
        "shop_account": ctx.shop_account,
        "start_date": ctx.start_date,
        "end_date": ctx.end_date,
    }


# 每次 ASIN 数据拉取必调的基础工具（上下文解析前提）。
# 原定义在 mcp_tool_fallback.py（已随 Doris 切除删除），此处为唯一真源。
BOOTSTRAP_TOOLS: frozenset[str] = frozenset({"listing_basic_info_v2", "parent_listing_stock_summary", "ad_campaign_product_keyword_list"})

META_TO_MCP_TOOLS: dict[str, list[str]] = {
    # 2026-06-30: ad_keyword_report MCP 工具已下线（服务端拆分），Doris 回落已切除。
    # 关键词广告效果数据暂缺，后续用 ad_optimization + ad_campaign_product_keyword_list 恢复。
    # "META_KW_AD": ["ad_keyword_report"],  -- MCP tool unavailable
    "META_COMPETITOR": ["direct_competitors"],
    # keyword_child_asins 已由 mcp_adapter Phase 2 逐词并行（正确传参），不再经 META 空跑
    "META_KW_COMPETITOR_RANK": [],
    "META_KW_SUB_ASIN_RANK": [],
    "META_FLOW_KEYWORD": ["flow_keywords"],
    "META_AD_PRODUCT": ["ad_product_report"],
    "META_AD_PLACEMENT": ["ad_placement_report"],
    "META_AD_SEARCH_TERM": ["ad_search_term_report"],
    "META_TREND": ["product_sales"],
}


TOOL_ARG_BUILDERS: dict[str, ArgBuilder] = {
    # 2026-06-27 新工具：父ASIN→站点/sku/店铺/产品名（替代 mcp_db_context._LOOKUP_SQL）。
    "parent_listing_detail": lambda ctx: {
        "parent_asin": ctx.parent_asin,
    },
    "listing_basic_info_v2": lambda ctx: {
        "shop_account": ctx.shop_account,
        "parent_asin": ctx.parent_asin,
        "parent_seller_sku": ctx.parent_seller_sku,
    },
    "parent_listing_stock_summary": lambda ctx: {
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
    # 2026-06-27 新工具：父ASIN→所有子ASIN的活跃广告活动+关键词（替代
    # _resolve_and_fetch_listing + _fetch_campaign_context 两条 SQL）。
    "ad_campaign_product_keyword_list": lambda ctx: {
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
    "ad_campaign_list",                 # 父 ASIN → 全量在线活动 (name + id)
    "ad_campaign_basic_info_v2",        # campaign_id → 活动详情 (替换 V1, 支持批量 ≤20)
    "ad_campaign_basic_info",           # [已废弃] shop_account, campaign_name — 保留兼容
    "ad_campaign_product_report",       # + start_date, end_date
    "ad_campaign_placement_report",     # + start_date, end_date (懒加载)
    "ad_campaign_search_term_report",   # + start_date, end_date (懒加载)
    "ad_portfolio_list",                # parent_asin → 广告组合预算 (2026-07-09)
]


def build_campaign_tool_args(
    tool_name: str,
    campaign_name: str,
    shop_account: str,
    start_date: str = "",
    end_date: str = "",
    *,
    campaign_name_list: str = "",
    campaign_id_list: str = "",
    parent_asin: str = "",
    parent_seller_sku: str = "",
    qryFixedPortfolio: bool | None = None,   # ad_portfolio_list 专用
) -> dict:
    """构建 campaign 级 MCP 入参。

    与 TOOL_ARG_BUILDERS（ASIN 级，使用 parent_asin）不同，
    campaign 级工具以 campaign_name 为必填参数。
    ad_campaign_basic_info_v2 支持批量：campaign_id_list 不为空时替代 campaign_name
    （逗号分隔，上限 20）。
    ad_campaign_basic_info (V1) 保留兼容，使用 campaign_name_list。
    ad_campaign_list / ad_portfolio_list 使用 parent_asin / parent_seller_sku。
    """
    if tool_name == "ad_campaign_list":
        base = {"shop_account": shop_account, "parent_asin": parent_asin,
                "parent_seller_sku": parent_seller_sku}
    elif tool_name == "ad_portfolio_list":
        base = {"shop_account": shop_account, "parent_asin": parent_asin,
                "parent_seller_sku": parent_seller_sku}
        if qryFixedPortfolio is not None:
            base["qryFixedPortfolio"] = qryFixedPortfolio
    elif tool_name == "ad_campaign_basic_info_v2" and campaign_id_list:
        base = {"shop_account": shop_account, "campaign_id_list": campaign_id_list}
    elif tool_name == "ad_campaign_basic_info" and campaign_name_list:
        base = {"shop_account": shop_account, "campaign_name_list": campaign_name_list}
    else:
        base = {"shop_account": shop_account, "campaign_name": campaign_name}
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