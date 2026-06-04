"""Unit tests for DB SQL helpers (no live Doris)."""

import pytest

from app.data.db_adapter import DbAdapter
from app.data.db_sql_helpers import ListingContext, build_in_clause, meta_fallback_timeout


def test_build_in_clause_empty():
    sql, params = build_in_clause("t.asin", [])
    assert sql == "1=0"
    assert params == ()


def test_build_in_clause_values():
    sql, params = build_in_clause("daap.asin", ["B001", "B002"])
    assert "IN (%s,%s)" in sql
    assert params == ("B001", "B002")


def test_listing_context_caps_child_asins(monkeypatch):
    monkeypatch.setattr(
        "app.data.db_sql_helpers.settings.db_child_asin_cap",
        2,
        raising=False,
    )
    listing = {
        "parent_asin": "B0TEST",
        "parent_seller_sku": "SKU1",
        "shop_id": 1,
        "child_asins": [("A1", "S1"), ("A2", "S2"), ("A3", "S3")],
        "child_asins_follow_up": ["A1", "A2", "A3"],
    }
    ctx = ListingContext.from_listing_row(listing)
    assert len(ctx.child_asins) == 2
    assert ctx.child_asins == ["A1", "A2"]


def test_meta_fallback_timeout_uses_failover_budget():
    t = meta_fallback_timeout(["META_FLOW_KEYWORD", "META_KW_AD"])
    assert t >= 180.0


@pytest.mark.asyncio
async def test_ad_keywords_sql_uses_in_not_listing_exists():
    captured: list[str] = []

    async def fake_query(sql, params=(), timeout=None, label=""):
        captured.append(sql)
        return []

    adapter = DbAdapter()
    adapter._query = fake_query  # type: ignore[method-assign]
    ctx = ListingContext(
        parent_asin="B0TEST",
        parent_seller_sku="SKU1",
        shop_id=1,
        child_asins=["B001", "B002"],
    )
    await adapter._fetch_ad_keywords(ctx, days=7)
    assert captured
    sql = captured[0]
    assert "daap.asin IN (%s,%s)" in sql
    assert "EXISTS (SELECT 1 FROM dwd_whp_amazon_listing" not in sql
