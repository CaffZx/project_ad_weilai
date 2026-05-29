from __future__ import annotations

import pytest
from unittest.mock import patch

from app.data.mcp_adapter import McpAdapter
from app.data.mcp_db_context import McpDbContext


class _FakeInvoker:
    def __init__(self, payloads: dict[str, object], fail_tools: set[str] | None = None):
        self.payloads = payloads
        self.fail_tools = fail_tools or set()

    async def call_tool(self, tool_name: str, arguments: dict):
        if tool_name in self.fail_tools:
            raise RuntimeError(f"tool failed: {tool_name}")
        return self.payloads.get(tool_name, [])


@pytest.mark.asyncio
async def test_mcp_adapter_builds_core_fields():
    payloads = {
        "listing_basic_info": [{"parent_seller_sku": "PARENT-SKU", "price": 29.9, "rating": 4.5, "review_count": 123}],
        "ad_product_report": [{"cost": 100, "sale": 400, "clicks": 200, "impressions": 10000, "units_order": 20, "acos": 25, "cpc": 0.5, "ctr": 2, "cvr": 10}],
        "ad_keyword_report": [{"keyword": "dress", "match_type": "EXACT", "clicks": 20, "cost": 10, "impressions": 500, "sale": 50, "units_order": 5, "acos": 20, "cvr": 25}],
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
    with patch("app.data.mcp_adapter.resolve_mcp_context_from_db", return_value=_ctx):
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
        "listing_basic_info": [{"parent_seller_sku": "PARENT-SKU"}],
        "ad_product_report": [{"cost": 20, "sale": 40, "clicks": 10, "impressions": 100, "units_order": 2}],
    }
    adapter = McpAdapter(invoker=_FakeInvoker(payloads, fail_tools={"flow_keywords", "ad_keyword_report"}))
    _ctx = McpDbContext(
        parent_asin="B0TEST",
        parent_seller_sku="PARENT-SKU",
        shop_account="shop_us",
    )
    with patch("app.data.mcp_adapter.resolve_mcp_context_from_db", return_value=_ctx):
        data = await adapter.fetch_asin_data("B0TEST")
    assert data.asin == "B0TEST"
    assert "flow_keywords" in data.missing_fields
    assert "ad_keyword_report" in data.missing_fields
    assert data.ad_data.spend == 20


@pytest.mark.asyncio
async def test_mcp_adapter_chinese_field_keys():
    """MCP gateway returns Chinese column names; normalizers must map them."""
    payloads = {
        "listing_basic_info": [{"parent_seller_sku": "PARENT-SKU", "price": 29.9}],
        "ad_product_report": [
            {
                "花费": 100,
                "销售额": 400,
                "点击量": 200,
                "曝光量": 10000,
                "广告订单量": 20,
                "ACOS": 25,
                "CPC": 0.5,
                "CTR": 2,
                "CVR": 10,
            }
        ],
        "ad_keyword_report": [
            {
                "搜索词": "dress",
                "match_type": "EXACT",
                "点击量": 20,
                "花费": 10,
                "曝光量": 500,
                "销售额": 50,
                "广告订单量": 5,
                "ACOS": 20,
                "CVR": 25,
            }
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
    with patch("app.data.mcp_adapter.resolve_mcp_context_from_db", return_value=_ctx):
        data = await adapter.fetch_asin_data("B0TEST")
    assert data.ad_data.spend == 100
    assert data.ad_data.acos == 25
    assert data.natural_order_ratio is not None
    assert abs(data.natural_order_ratio - 70.0) < 0.1
    assert data.ad_data.tacos is not None
    assert abs(data.ad_data.tacos - 20.0) < 0.1
    assert data.keyword_count == 1
    assert data.keywords[0].keyword == "dress"


@pytest.mark.asyncio
async def test_mcp_adapter_natural_order_ratio_from_daily_trend_rows():
    """When aggregate keys are absent, sum daily 全部单量/广告单量 from product_sales rows."""
    payloads = {
        "listing_basic_info": [{"parent_seller_sku": "PARENT-SKU"}],
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
    with patch("app.data.mcp_adapter.resolve_mcp_context_from_db", return_value=_ctx):
        data = await adapter.fetch_asin_data("B0TEST", meta_filter=["META_TREND", "META_AD_PRODUCT"])
    assert data.natural_order_ratio is not None
    assert abs(data.natural_order_ratio - 75.0) < 0.1
