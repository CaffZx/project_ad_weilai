from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from app.config.settings import settings
from app.models.asin_data import ASINData
from app.models.campaign import CampaignData, CampaignStrategyContext, CampaignUnit
from app.workflow.steps import campaign as campaign_steps


def _campaign_data() -> CampaignData:
    return CampaignData(
        parent_asin="B0CACHE",
        shop_id=42453,
        shop_account="cached-shop",
        parent_seller_sku="PARENT-SKU",
        site_code="Amazon_US",
        total_campaigns=1,
        campaigns=[
            CampaignUnit(
                campaign_name="camp-1",
                campaign_key="camp-1",
                child_asin="B0CHILD",
                keyword_text="cache keyword",
                match_type="EXACT",
            )
        ],
    )


def _run_impl(monkeypatch, fetcher, campaign_data=None):
    monkeypatch.setattr(campaign_steps, "filter_eliminated_pool", lambda campaigns: (campaigns, [], []))
    monkeypatch.setattr(
        campaign_steps,
        "_analyze_one_stream",
        AsyncMock(return_value=([], {}, [], [], [])),
    )
    monkeypatch.setattr(campaign_steps, "_SANITY_CHECK_ENABLED", False)
    monkeypatch.setattr(campaign_steps, "_SYNTHESIS_ENABLED", False)
    monkeypatch.setattr(settings, "campaign_new_enabled", False)
    monkeypatch.setattr(settings, "campaign_restart_enabled", False)
    monkeypatch.setattr(settings, "campaign_overview_enabled", False)
    monkeypatch.setattr(settings, "campaign_budget_agent_enabled", False)
    monkeypatch.setattr(settings, "campaign_portfolio_enabled", True)

    return asyncio.run(
        campaign_steps._analyze_campaigns_impl(
            fetcher=fetcher,
            reasoner=MagicMock(),
            parent_asin="B0CACHE",
            asin_data=MagicMock(title=""),
            strategy_context=CampaignStrategyContext(
                parent_asin="B0CACHE",
                daily_budget=100.0,
                daily_budget_source="test",
            ),
            days=7,
            bs=6,
            cc=1,
            temperature=0.3,
            refresh=False,
            campaign_data=campaign_data,
            keyword_analysis={},
            run_id="test-run",
            _t=lambda _label: None,
        )
    )


def test_redis_hit_reuses_cached_shop_account_for_portfolio(monkeypatch):
    cached = _campaign_data()
    fetcher = MagicMock()
    fetcher._last_shop_account = ""
    fetcher.fetch_campaigns = AsyncMock()
    fetcher.fetch_portfolio_list = AsyncMock(return_value={})

    monkeypatch.setattr(campaign_steps, "_load_cached_campaigns", AsyncMock(return_value=cached))

    _run_impl(monkeypatch, fetcher)

    fetcher.fetch_campaigns.assert_not_awaited()
    fetcher.fetch_portfolio_list.assert_awaited_once_with(
        "B0CACHE",
        parent_seller_sku="PARENT-SKU",
        shop_account="cached-shop",
        days=7,
        site_code="Amazon_US",
    )


def test_prefetched_campaign_data_still_queries_portfolio(monkeypatch):
    prefetched = _campaign_data()
    fetcher = MagicMock()
    fetcher._last_shop_account = ""
    fetcher.fetch_portfolio_list = AsyncMock(return_value={})

    _run_impl(monkeypatch, fetcher, campaign_data=prefetched)

    fetcher.fetch_portfolio_list.assert_awaited_once_with(
        "B0CACHE",
        parent_seller_sku="PARENT-SKU",
        shop_account="cached-shop",
        days=7,
        site_code="Amazon_US",
    )


def test_strategy_context_keeps_operating_mode_without_runtime_permission():
    ctx = campaign_steps.build_campaign_strategy_context(
        "B0CLEARANCE",
        ASINData(asin="B0CLEARANCE"),
        {"operating_mode": "控制清货"},
    )

    assert ctx.operating_mode == "控制清货"
    assert "ad_permission" not in ctx.model_dump()


def test_strategy_context_allows_null_product_stage():
    """历史配置的空产品阶段不得阻断 Campaign 分析入口。"""
    ctx = campaign_steps.build_campaign_strategy_context(
        "B0NULLSTAGE",
        ASINData(asin="B0NULLSTAGE"),
        {"product_stage": None},
    )

    assert ctx.product_stage == ""


def test_clearance_permission_skips_new_and_restart_analysis(monkeypatch):
    unit = CampaignUnit(
        campaign_name="pooled-campaign",
        campaign_key="pooled-campaign",
        child_asin="B0CHILD",
        keyword_text="pooled keyword",
        match_type="EXACT",
    )
    campaign_data = CampaignData(
        parent_asin="B0CLEARANCE",
        total_campaigns=1,
        campaigns=[unit],
    )
    new_analysis = AsyncMock()
    restart_analysis = AsyncMock()
    fetcher = MagicMock()
    fetcher._last_shop_account = ""

    monkeypatch.setattr(
        campaign_steps,
        "filter_eliminated_pool",
        lambda _campaigns: ([], [], [unit]),
    )
    monkeypatch.setattr(campaign_steps, "analyze_new_campaigns", new_analysis)
    monkeypatch.setattr(campaign_steps, "_run_restart_review", restart_analysis)
    monkeypatch.setattr(settings, "campaign_new_enabled", True)
    monkeypatch.setattr(settings, "campaign_restart_enabled", True)

    result = asyncio.run(
        campaign_steps._analyze_campaigns_impl(
            fetcher=fetcher,
            reasoner=MagicMock(),
            parent_asin="B0CLEARANCE",
            asin_data=MagicMock(title=""),
            strategy_context=CampaignStrategyContext(
                parent_asin="B0CLEARANCE",
                operating_mode="控制清货",
            ),
            days=7,
            bs=6,
            cc=1,
            temperature=0.3,
            refresh=False,
            campaign_data=campaign_data,
            keyword_analysis={},
            run_id="test-clearance",
            _t=lambda _label: None,
        )
    )

    assert result.new_campaigns == []
    new_analysis.assert_not_awaited()
    restart_analysis.assert_not_awaited()
