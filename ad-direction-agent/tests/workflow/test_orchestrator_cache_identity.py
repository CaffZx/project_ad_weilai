import pytest

from app.core.workflow_orchestrator import WorkflowOrchestrator
from app.models.asin_data import ASINData


@pytest.mark.asyncio
async def test_ensure_data_discards_cached_data_without_product_identity(monkeypatch):
    stale = ASINData(asin="B0TEST")
    fresh = ASINData(asin="B0TEST", shop_id=1622, parent_seller_sku="SKU-001")
    calls = {"get": 0, "delete": [], "fetch": 0}

    async def fake_get(asin, days=7, meta_filter=None):
        calls["get"] += 1
        return stale if calls["get"] == 1 else None

    async def fake_delete(asin, days=7, meta_filter=None):
        calls["delete"].append((asin, days, tuple(meta_filter or [])))

    async def fake_set(asin, data, days=7, meta_filter=None):
        return None

    class FakeAggregator:
        async def fetch(self, asin, days=7, meta_filter=None):
            calls["fetch"] += 1
            return fresh

    monkeypatch.setattr("app.core.workflow_orchestrator.asin_data_cache.get", fake_get)
    monkeypatch.setattr("app.core.workflow_orchestrator.asin_data_cache.delete", fake_delete)
    monkeypatch.setattr("app.core.workflow_orchestrator.asin_data_cache.set", fake_set)

    got = await WorkflowOrchestrator(aggregator=FakeAggregator())._ensure_data("B0TEST")

    assert got is fresh
    assert calls["fetch"] == 1
    assert calls["delete"] == [("B0TEST", 7, ())]


@pytest.mark.asyncio
async def test_ensure_data_reuses_cached_data_with_product_identity(monkeypatch):
    cached = ASINData(asin="B0TEST", shop_id=1622, parent_seller_sku="SKU-001")
    calls = {"fetch": 0}

    async def fake_get(asin, days=7, meta_filter=None):
        return cached

    async def fake_delete(asin, days=7, meta_filter=None):
        raise AssertionError("complete cache should not be deleted")

    class FakeAggregator:
        async def fetch(self, asin, days=7, meta_filter=None):
            calls["fetch"] += 1
            return ASINData(asin=asin)

    monkeypatch.setattr("app.core.workflow_orchestrator.asin_data_cache.get", fake_get)
    monkeypatch.setattr("app.core.workflow_orchestrator.asin_data_cache.delete", fake_delete)

    got = await WorkflowOrchestrator(aggregator=FakeAggregator())._ensure_data("B0TEST")

    assert got is cached
    assert calls["fetch"] == 0
