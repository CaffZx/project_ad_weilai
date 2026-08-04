from types import SimpleNamespace

import pytest

from app.data.new_keyword_fetcher import NewKeywordFetcher
from app.models.campaign import (
    CampaignStrategyContext,
    NewKeywordData,
    NewKeywordRecord,
)
from app.workflow.steps.campaign_new import analyze_new_campaigns


class _FakeMcp:
    def __init__(self, flow_rows, own_rows):
        self.flow_rows = flow_rows
        self.own_rows = own_rows
        self.history_calls: list[str] = []

    async def call_tool_timed_with_args(self, tool_name, arguments, timeout):
        if tool_name == "flow_keywords":
            return SimpleNamespace(ok=True, value={"rows": self.flow_rows})
        if tool_name == "own_keyword_flow":
            return SimpleNamespace(ok=True, value={"rows": self.own_rows})
        if tool_name == "erp_listing_asin_keyword_rank_history":
            self.history_calls.append(arguments["keyword"])
            return SimpleNamespace(ok=True, value={
                "rows": [{"crawNatureRank": 30, "crawNatureRankPosition": "28-48"}],
            })
        raise AssertionError(f"unexpected MCP tool: {tool_name}")


@pytest.mark.asyncio
async def test_top_60_caps_priority_terms_by_search_volume_before_history():
    flow_rows = [
        {"keyword": f"priority keyword {i}", "search_volume": i}
        for i in range(561, 500, -1)
    ]
    own_rows = [
        {"keyword": f"priority keyword {i}", "natural_rank": 30, "asin": "B0CHILD"}
        for i in range(561, 500, -1)
    ]
    fake_mcp = _FakeMcp(flow_rows, own_rows)
    fetcher = NewKeywordFetcher()
    fetcher._mcp_adapter = fake_mcp

    data = await fetcher.fetch(
        parent_asin="B0PARENT", shop_account="shop", target_child_asin="B0CHILD",
    )

    assert len(data.records) == 60
    assert len(fake_mcp.history_calls) == 60
    assert {r.search_volume for r in data.records} == set(range(502, 562))


@pytest.mark.asyncio
async def test_top_40_caps_confirmed_priority_terms_by_search_volume(monkeypatch):
    import app.data.new_keyword_fetcher as new_keyword_module

    captured_batches: list[list[dict]] = []
    records = [
        NewKeywordRecord(
            keyword_text=f"confirmed keyword {i}",
            search_volume=i,
            own_natural_rank=30,
            natural_rank=30,
            history_state="ok",
            source="ranking_opportunity",
        )
        for i in range(541, 500, -1)
    ]

    class FakeNewKeywordFetcher:
        async def fetch(self, **kwargs):
            return NewKeywordData(parent_asin="B0PARENT", records=records)

    class FakeCampaignFetcher:
        async def fetch_suggested_bids(self, *args, **kwargs):
            return {}

    class FakeReasoner:
        async def recommend_new_campaigns(self, *, candidates, **kwargs):
            captured_batches.append(candidates)
            return {
                "success": True,
                "parsed": {"new_campaigns": [
                    {
                        "keyword_text": candidate["keyword_text"],
                        "action": "create",
                        "keyword_class": "long_tail",
                        "relevance_tier": "R1",
                    }
                    for candidate in candidates
                ]},
            }

    monkeypatch.setattr(new_keyword_module, "NewKeywordFetcher", FakeNewKeywordFetcher)
    old_batch_size = 10
    from app.config.settings import settings
    old_batch_size = settings.campaign_new_batch_size
    settings.campaign_new_batch_size = 100
    try:
        await analyze_new_campaigns(
            fetcher=FakeCampaignFetcher(), reasoner=FakeReasoner(),
            parent_asin="B0PARENT", shop_id=1, parent_seller_sku="SKU",
            site_code="Amazon_US", shop_account="shop", existing_keywords=set(),
            pre_eliminated_count=0, strategy_context=CampaignStrategyContext(),
            ctx_dict={}, temperature=0.0, target_child_asin="B0CHILD",
        )
    finally:
        settings.campaign_new_batch_size = old_batch_size

    assert [len(batch) for batch in captured_batches] == [40, 40]
    assert {c["search_volume"] for c in captured_batches[0]} == set(range(502, 542))


@pytest.mark.asyncio
async def test_unverified_flow_long_tail_creates_broad_campaign(monkeypatch):
    import app.data.new_keyword_fetcher as new_keyword_module

    class FakeNewKeywordFetcher:
        async def fetch(self, **kwargs):
            return NewKeywordData(parent_asin="B0PARENT", records=[
                NewKeywordRecord(
                    keyword_text="long tail flow keyword",
                    search_volume=500,
                    source="flow",
                    source_reason="流量词库",
                ),
            ])

    class FakeCampaignFetcher:
        async def fetch_suggested_bids(self, *args, **kwargs):
            return {}

    class FakeReasoner:
        async def recommend_new_campaigns(self, *, candidates, **kwargs):
            return {
                "success": True,
                "parsed": {"new_campaigns": [{
                    "keyword_text": candidates[0]["keyword_text"],
                    "action": "create",
                    "keyword_class": "long_tail",
                    "relevance_tier": "R1",
                }]},
            }

    monkeypatch.setattr(new_keyword_module, "NewKeywordFetcher", FakeNewKeywordFetcher)
    campaigns, _warnings, _sv = await analyze_new_campaigns(
        fetcher=FakeCampaignFetcher(), reasoner=FakeReasoner(),
        parent_asin="B0PARENT", shop_id=1, parent_seller_sku="SKU",
        site_code="Amazon_US", shop_account="shop", existing_keywords=set(),
        pre_eliminated_count=0, strategy_context=CampaignStrategyContext(),
        ctx_dict={}, temperature=0.0, target_child_asin="B0CHILD",
    )

    assert len(campaigns) == 1
    assert campaigns[0].match_type == "BROAD"
    assert campaigns[0].campaign_type == "广泛广告"
