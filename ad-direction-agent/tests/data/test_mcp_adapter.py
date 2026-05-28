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
