"""新增活动两来源在主编排中的时序契约。"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from app.config.settings import settings
from app.models.campaign import (
    CampaignData,
    CampaignStrategyContext,
    CampaignUnit,
    NewCampaignDecision,
    SearchTermPromotionCandidate,
)
from app.workflow.steps import campaign as campaign_steps


def test_normal_path_finalizes_source_a_and_b_once_after_broad_returns(monkeypatch):
    broad_candidate = SearchTermPromotionCandidate(
        campaign_key="broad-1",
        campaign_name="broad-1",
        campaign_match_type="BROAD",
        search_term="sticky bra",
        relevance_tier="R1",
        orders=3,
        cost=12,
        sales=60,
    )
    traffic_source = NewCampaignDecision(
        keyword_text="flow keyword",
        source="flow",
        trigger_scene="KEYWORD_POOL_EXPANSION",
        prescribed_match_type="BROAD",
    )
    finalizer = AsyncMock(return_value=[])

    async def fake_stream(campaigns, task_type, *_args, **_kwargs):
        if task_type == "broad":
            return [], {}, [], [], [broad_candidate]
        return [], {}, [], [], []

    monkeypatch.setattr(campaign_steps, "filter_eliminated_pool", lambda campaigns: (campaigns, [], []))
    monkeypatch.setattr(campaign_steps, "_analyze_one_stream", fake_stream)
    monkeypatch.setattr(
        campaign_steps,
        "analyze_new_campaign_decisions",
        AsyncMock(return_value=([traffic_source], [], {})),
    )
    monkeypatch.setattr(campaign_steps, "finalize_new_campaign_decisions", finalizer)
    monkeypatch.setattr(settings, "campaign_new_enabled", True)
    monkeypatch.setattr(settings, "campaign_overview_enabled", False)
    monkeypatch.setattr(settings, "campaign_restart_enabled", False)
    monkeypatch.setattr(settings, "campaign_portfolio_enabled", False)
    monkeypatch.setattr(settings, "campaign_sanity_enabled", False)
    monkeypatch.setattr(settings, "campaign_synthesis_enabled", False)
    monkeypatch.setattr(settings, "campaign_budget_agent_enabled", False)

    data = CampaignData(
        parent_asin="B0PARENT",
        shop_id=1,
        shop_account="shop",
        parent_seller_sku="SKU",
        total_campaigns=1,
        campaigns=[CampaignUnit(
            campaign_key="broad-1",
            campaign_name="broad-1",
            keyword_text="broad seed",
            match_type="BROAD",
            child_asin="B0CHILD",
        )],
    )
    fetcher = MagicMock()
    fetcher._last_shop_account = "shop"

    asyncio.run(campaign_steps._analyze_campaigns_impl(
        fetcher=fetcher,
        reasoner=MagicMock(),
        parent_asin="B0PARENT",
        asin_data=MagicMock(title="product"),
        strategy_context=CampaignStrategyContext(parent_asin="B0PARENT", target_acos=25),
        days=7,
        bs=6,
        cc=1,
        temperature=0.0,
        refresh=False,
        campaign_data=data,
        keyword_analysis={},
        run_id="test",
        _t=lambda _label: None,
    ))

    finalizer.assert_awaited_once()
    decisions = finalizer.await_args.args[0]
    by_keyword = {decision.keyword_text: decision for decision in decisions}
    assert by_keyword["flow keyword"].prescribed_match_type == "BROAD"
    assert by_keyword["sticky bra"].prescribed_match_type == "EXACT"
