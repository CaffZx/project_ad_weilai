from __future__ import annotations

import pytest
from unittest.mock import patch

from app.data.mcp_adapter import McpAdapter
from app.data.mcp_db_context import McpDbContext
from app.data.mcp_mapping import bootstrap_tools_for_meta


class _FakeInvoker:
    def __init__(self, payloads: dict[str, object], fail_tools: set[str] | None = None):
        self.payloads = payloads
        self.fail_tools = fail_tools or set()
        self.calls: list[str] = []

    async def call_tool(self, tool_name: str, arguments: dict):
        self.calls.append(tool_name)
        if tool_name in self.fail_tools:
            raise RuntimeError(f"tool failed: {tool_name}")
        return self.payloads.get(tool_name, [])


def test_bootstrap_tools_for_product_filter_excludes_campaign_keywords():
    tools = bootstrap_tools_for_meta(["META_AD_PRODUCT", "META_TREND"])

    assert "listing_basic_info_v2" in tools
    assert "parent_listing_stock_summary" in tools
    assert "ad_campaign_product_keyword_list" not in tools


def test_bootstrap_tools_for_keyword_filter_includes_campaign_keywords():
    tools = bootstrap_tools_for_meta(["META_AD_PRODUCT", "META_KW_COMPETITOR_RANK"])

    assert "ad_campaign_product_keyword_list" in tools


@pytest.mark.asyncio
async def test_mcp_adapter_builds_core_fields():
    payloads = {
        "listing_basic_info_v2": [{"parent_seller_sku": "PARENT-SKU", "price": 29.9, "rating": 4.5, "review_count": 123}],
        "ad_product_report": [{"cost": 100, "sale": 400, "clicks": 200, "impressions": 10000, "units_order": 20, "acos": 0.25, "cpc": 0.5, "ctr": 0.02, "cvr": 0.10}],
        "ad_campaign_product_keyword_list": [{"关键词": "dress", "关键词匹配类型": "EXACT", "广告活动名称": "test"}],
        "flow_keywords": [{"keyword": "summer dress", "searches": 900, "top_convert_ratio": 0.2}],
        "direct_competitors": [{"asin": "B0C1", "price": 31, "asin_star": 4.3, "reviews_num": 90, "top_category_rank": 20}],
        "product_sales": [{"sales": 1000, "orders": 50, "ad_orders": 15, "ad_cost": 200, "margin": 0.35, "date": "05-20"}],
    }
    adapter = McpAdapter(invoker=_FakeInvoker(payloads))
    _ctx = McpDbContext(
        parent_asin="B0TEST",
        parent_seller_sku="PARENT-SKU",
        shop_account="shop_us",
    )
    with patch("app.data.mcp_adapter.resolve_mcp_context_from_mcp", return_value=_ctx):
        data = await adapter.fetch_asin_data("B0TEST")
    assert data.asin == "B0TEST"
    assert data.sku == "PARENT-SKU"
    assert data.ad_data.acos == 25
    assert data.keyword_count == 1
    assert len(data.competitors) == 1
    assert data.natural_order_ratio is not None
    assert data.data_missing is False


@pytest.mark.asyncio
async def test_mcp_adapter_partial_failure_degrades_not_crash():
    payloads = {
        "listing_basic_info_v2": [{"parent_seller_sku": "PARENT-SKU"}],
        "ad_product_report": [{"cost": 20, "sale": 40, "clicks": 10, "impressions": 100, "units_order": 2}],
    }
    adapter = McpAdapter(invoker=_FakeInvoker(payloads, fail_tools={"flow_keywords"}))
    _ctx = McpDbContext(
        parent_asin="B0TEST",
        parent_seller_sku="PARENT-SKU",
        shop_account="shop_us",
    )
    with patch("app.data.mcp_adapter.resolve_mcp_context_from_mcp", return_value=_ctx):
        data = await adapter.fetch_asin_data("B0TEST")
    assert data.asin == "B0TEST"
    assert "flow_keywords" in data.missing_fields
    assert data.ad_data.spend == 20


@pytest.mark.asyncio
async def test_mcp_adapter_chinese_field_keys():
    """MCP gateway returns Chinese column names; normalizers must map them."""
    payloads = {
        "listing_basic_info_v2": [{"parent_seller_sku": "PARENT-SKU", "price": 29.9}],
        "ad_product_report": [
            {
                "花费": 100,
                "销售额": 400,
                "点击量": 200,
                "曝光量": 10000,
                "广告订单量": 20,
                "ACOS": 0.25,
                "CPC": 0.5,
                "CTR": 0.02,
                "CVR": 0.10,
            }
        ],
        "ad_campaign_product_keyword_list": [
            {"关键词": "dress", "关键词匹配类型": "EXACT", "广告活动名称": "test"}
        ],
        "product_sales": [
            {
                "全部销售额": 1000,
                "全部单量": 50,
                "广告单量": 15,
                "广告花费": 200,
                "日期": "05-20",
            }
        ],
    }
    adapter = McpAdapter(invoker=_FakeInvoker(payloads))
    _ctx = McpDbContext(
        parent_asin="B0TEST",
        parent_seller_sku="PARENT-SKU",
        shop_account="shop_us",
    )
    with patch("app.data.mcp_adapter.resolve_mcp_context_from_mcp", return_value=_ctx):
        data = await adapter.fetch_asin_data("B0TEST")
    assert data.ad_data.spend == 100
    assert data.ad_data.acos == 25
    assert data.natural_order_ratio is not None
    assert abs(data.natural_order_ratio - 70.0) < 0.1
    assert data.ad_data.tacos is not None
    assert abs(data.ad_data.tacos - 20.0) < 0.1
    assert data.keyword_count == 1


@pytest.mark.asyncio
async def test_mcp_adapter_natural_order_ratio_from_daily_trend_rows():
    """When aggregate keys are absent, sum daily 全部单量/广告单量 from product_sales rows."""
    payloads = {
        "listing_basic_info_v2": [{"parent_seller_sku": "PARENT-SKU"}],
        "ad_product_report": [{"花费": 10, "销售额": 40, "点击量": 5, "曝光量": 100, "广告订单量": 2}],
        "product_sales": [
            {"全部单量": 10, "广告单量": 3, "全部销售额": 200, "广告花费": 10, "日期": "05-18"},
            {"全部单量": 10, "广告单量": 2, "全部销售额": 200, "广告花费": 10, "日期": "05-19"},
        ],
    }
    adapter = McpAdapter(invoker=_FakeInvoker(payloads))
    _ctx = McpDbContext(
        parent_asin="B0TEST",
        parent_seller_sku="PARENT-SKU",
        shop_account="shop_us",
    )
    with patch("app.data.mcp_adapter.resolve_mcp_context_from_mcp", return_value=_ctx):
        data = await adapter.fetch_asin_data("B0TEST", meta_filter=["META_TREND", "META_AD_PRODUCT"])
    assert data.natural_order_ratio is not None
    assert abs(data.natural_order_ratio - 75.0) < 0.1


@pytest.mark.asyncio
async def test_mcp_adapter_light_product_filter_skips_campaign_keyword_bootstrap():
    payloads = {
        "listing_basic_info_v2": [{"parent_seller_sku": "PARENT-SKU"}],
        "parent_listing_stock_summary": [{"FBA可售库存": 20}],
        "ad_product_report": [{"花费": 10, "销售额": 40, "点击量": 5, "曝光量": 100, "广告订单量": 2}],
        "product_sales": [{"全部单量": 10, "广告单量": 3, "全部销售额": 200, "广告花费": 10, "日期": "05-18"}],
    }
    invoker = _FakeInvoker(payloads)
    adapter = McpAdapter(invoker=invoker)
    _ctx = McpDbContext(
        parent_asin="B0TEST",
        parent_seller_sku="PARENT-SKU",
        shop_account="shop_us",
    )

    with patch("app.data.mcp_adapter.resolve_mcp_context_from_mcp", return_value=_ctx):
        data = await adapter.fetch_asin_data("B0TEST", meta_filter=["META_AD_PRODUCT", "META_TREND"])

    assert data.asin == "B0TEST"
    assert "ad_campaign_product_keyword_list" not in invoker.calls
    assert "keyword_child_asins" not in invoker.calls
    assert "flow_keywords" not in invoker.calls
