from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.data.campaign_fetcher import build_search_term_bundle
from app.data.campaign_fetcher import CampaignFetcher, _CallResult


def _row(keyword: str, *, clicks: int = 0, cost: float = 0, sales: float = 0,
         orders: int = 0, impressions: int = 0) -> dict:
    return {
        "搜索词": keyword,
        "点击量": clicks,
        "花费": cost,
        "销售额": sales,
        "广告订单量": orders,
        "曝光量": impressions,
    }


def test_bundle_uses_7d_as_canonical_set_and_caps_by_orders_cost_clicks():
    rows_7d = [
        _row("noise", impressions=0),
        _row("no-order-expensive", clicks=8, cost=20, impressions=100),
        _row("order-low-cost", clicks=1, cost=1, orders=1, sales=10, impressions=10),
        _row("order-high-cost", clicks=2, cost=30, orders=2, sales=40, impressions=20),
        _row("click-heavy", clicks=20, cost=2, impressions=500),
    ]
    rows_14d = [
        _row("14d-only", clicks=99, cost=99, impressions=999),
        _row("no-order-expensive", clicks=15, cost=30, impressions=300),
        _row("order-high-cost", clicks=3, cost=40, orders=3, sales=80, impressions=30),
    ]

    bundle = build_search_term_bundle(rows_7d, rows_14d, max_terms_per_campaign=3)

    assert [term["keyword"] for term in bundle["terms"]] == [
        "order-high-cost", "order-low-cost", "no-order-expensive",
    ]
    assert bundle["summary"]["dropped_low_signal_count"] == 1
    assert bundle["summary"]["cap_truncated_count"] == 1
    assert "14d-only" not in {term["keyword"] for term in bundle["terms"]}


def test_bundle_only_attaches_14d_for_selected_7d_insufficient_term_by_key():
    rows_7d = [
        _row("long tail", clicks=4, cost=3, impressions=80),
        _row("missing 14d", clicks=4, cost=3, impressions=80),
    ]
    rows_14d = [_row("long tail", clicks=9, cost=8, impressions=210)]

    bundle = build_search_term_bundle(rows_7d, rows_14d, max_terms_per_campaign=20)
    by_keyword = {term["keyword"]: term for term in bundle["terms"]}

    assert by_keyword["long tail"]["term_sample_insufficient"] is True
    assert by_keyword["long tail"]["metrics_14d"]["available"] is True
    assert by_keyword["missing 14d"]["term_sample_insufficient"] is True
    assert by_keyword["missing 14d"]["metrics_14d"]["available"] is False
    assert bundle["summary"]["fourteen_day_match_count"] == 1


def test_bundle_transmits_7d_only_for_sample_sufficient_term():
    bundle = build_search_term_bundle(
        [_row("converted", clicks=10, cost=15, orders=1, sales=50, impressions=100)],
        [_row("converted", clicks=30, cost=40, orders=2, sales=100, impressions=300)],
        max_terms_per_campaign=20,
    )

    term = bundle["terms"][0]
    assert term["term_sample_insufficient"] is False
    assert "metrics_14d" not in term


def test_fetcher_fetches_7d_and_14d_for_same_campaign():
    class FakeMcp:
        def __init__(self):
            self.calls = []

        async def campaign_call_tool(self, tool, name, shop, **kwargs):
            self.calls.append((tool, name, shop, kwargs))
            if kwargs["start_date"] == "14d":
                rows = [_row("long tail", clicks=9, cost=8, impressions=210)]
            else:
                rows = [_row("long tail", clicks=4, cost=3, impressions=80)]
            return _CallResult(ok=True, value=rows)

    fetcher = CampaignFetcher()
    fake = FakeMcp()
    fetcher._mcp_adapter = fake
    fetcher._mcp = lambda: fake

    result = asyncio.run(fetcher.fetch_search_terms_for(
        ["c1"], "shop", start_date="7d", end_date="7end",
        start_date_14d="14d", end_date_14d="14end",
        campaign_online_days={"c1": 7}, target_cpa=20,
    ))

    assert len(fake.calls) == 2
    assert {call[3]["start_date"] for call in fake.calls} == {"7d", "14d"}
    assert result["c1"]["terms"][0]["metrics_14d"]["available"] is True
