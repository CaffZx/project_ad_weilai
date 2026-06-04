from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.core.data_aggregator import DataAggregator
from app.models.asin_data import ASINData, AdData


def _db_like_data() -> ASINData:
    return ASINData(
        asin="B0TEST",
        sku="PARENT-SKU",
        ad_data=AdData(spend=100.0, sales=500.0, orders=20, acos=20.0, cpc=0.5, ctr=2.0, cvr=10.0),
    )


@pytest.mark.asyncio
async def test_shadow_compare_runs_without_breaking_primary_path():
    primary = _db_like_data()
    agg = DataAggregator(adapter=AsyncMock())
    agg.adapter.fetch_asin_data = AsyncMock(return_value=primary)

    def _consume_task(coro):
        coro.close()
        return None

    with patch("app.core.data_aggregator.settings.data_source", "db"), patch(
        "app.core.data_aggregator.settings.mcp_shadow_enabled", True
    ), patch.object(agg, "_shadow_compare", new=AsyncMock()), patch(
        "app.core.data_aggregator.asyncio.create_task", side_effect=_consume_task
    ) as create_task:
        result = await agg.fetch("B0TEST", days=7)
        assert result.asin == "B0TEST"
        create_task.assert_called_once()


@pytest.mark.asyncio
async def test_mcp_source_selected_by_settings():
    with patch("app.core.data_aggregator.settings.data_source", "mcp"):
        agg = DataAggregator()
        assert agg.adapter.__class__.__name__ == "McpAdapter"
