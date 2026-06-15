"""预算回算（KB23）纯逻辑单测 — aggregate / validate / to_budget_summary。

无 DB / 无 LLM。数据对齐 KB23 §11 示例。
"""

import pytest

from app.config.settings import settings
from app.models.campaign import (
    CampaignAdjustmentItem,
    CampaignPerf,
    CampaignStrategyContext,
    CampaignUnit,
)
from app.workflow.steps.campaign_budget_reallocation import (
    aggregate,
    to_budget_summary,
    validate,
)
from app.workflow.steps.campaign_portfolio import (
    PORTFOLIO_BROAD,
    PORTFOLIO_ELIMINATE,
    PORTFOLIO_MAIN,
    PORTFOLIO_TEST,
)


def _adj(key, kw, cur, prop, group, action="adjust_budget", match="EXACT"):
    return CampaignAdjustmentItem(
        campaign_name=key, campaign_key=key, keyword_text=kw, match_type=match,
        action=action, ai_portfolio_class=group,
        current_budget=cur, proposed_budget=prop,
    )


def _ctx():
    return CampaignStrategyContext(
        parent_asin="B0TEST", product_level="重点产品 (P1)", season_stage="旺季准备",
        ad_purposes=["排名型"], ad_directions=["推进自然位"],
        daily_budget=100.0, daily_budget_source="override",
    )


def _kb_example_adjustments():
    # KB23 §11：主力 +20 / 测试 +3 / 广泛 +7；一条转入低价捡漏释放 20（21→1）
    return [
        _adj("camp_main", "Main KW", 50, 70, PORTFOLIO_MAIN),
        _adj("camp_test", "Test KW", 10, 13, PORTFOLIO_TEST),
        _adj("camp_broad", "Broad KW", 20, 27, PORTFOLIO_BROAD, match="BROAD"),
        _adj("camp_elim", "Elim KW", 21, 1, PORTFOLIO_ELIMINATE,
             action="eliminate_to_low_bid_pool"),
    ]


@pytest.fixture
def parent_allowed_10():
    old = settings.campaign_parent_allowed_net_increase
    settings.campaign_parent_allowed_net_increase = 10.0
    yield
    settings.campaign_parent_allowed_net_increase = old


def test_aggregate_kb_example(parent_allowed_10):
    agg = aggregate(_kb_example_adjustments(), [], _ctx())
    p = agg["parent"]
    assert p["requested_increase_budget"] == 30.0
    assert p["low_bid_retention_release"] == 20.0
    assert p["released_budget"] == 20.0
    assert p["net_required_increase"] == 10.0
    assert p["available_reallocation_budget"] == 30.0     # released 20 + parent_allowed 10
    by = {g["group"]: g for g in agg["groups"]}
    assert by[PORTFOLIO_MAIN]["group_requested_delta"] == 20.0
    assert by[PORTFOLIO_TEST]["group_requested_delta"] == 3.0
    assert by[PORTFOLIO_BROAD]["group_requested_delta"] == 7.0
    assert agg["low_bid_group"]["moved_in_count"] == 1
    assert p["priority_context"]["has_ranking_push"] is True


def test_aggregate_default_parent_allowed_zero():
    # 默认 settings.campaign_parent_allowed_net_increase = 0 → available = released = 20
    agg = aggregate(_kb_example_adjustments(), [], _ctx())
    assert agg["parent"]["available_reallocation_budget"] == 20.0


def test_search_volume_and_acos_join():
    units = [CampaignUnit(
        campaign_name="camp_main", campaign_key="camp_main", child_asin="B0C1",
        keyword_text="Main KW", match_type="EXACT",
        perf_7d=CampaignPerf(acos=22.0),
    )]
    agg = aggregate(_kb_example_adjustments(), units, _ctx(),
                    search_volume_map={"main kw": 1000})
    main = next(g for g in agg["groups"] if g["group"] == PORTFOLIO_MAIN)
    camp = main["campaigns"][0]
    assert camp["search_volume"] == 1000        # join by keyword_text.lower()
    assert camp["acos"] == 22.0                 # 透传 perf_7d.acos
    # 空 map / 无 unit → 缺省不报错（best-effort 降级）
    agg2 = aggregate(_kb_example_adjustments(), [], _ctx(), search_volume_map={})
    assert next(g for g in agg2["groups"] if g["group"] == PORTFOLIO_MAIN)["campaigns"][0]["search_volume"] is None


def _valid_agent_out():
    return {
        "allocation_method": "full",
        "parent": {"proposed_total_group_budget": 111.0, "net_required_increase": 10.0, "explanation": "ok"},
        "budget_groups": [
            {"group": PORTFOLIO_MAIN, "current_group_budget": 50, "requested_delta": 20, "actual_delta": 20, "proposed_group_budget": 70, "reason": "x"},
            {"group": PORTFOLIO_TEST, "current_group_budget": 10, "requested_delta": 3, "actual_delta": 3, "proposed_group_budget": 13, "reason": "x"},
            {"group": PORTFOLIO_BROAD, "current_group_budget": 20, "requested_delta": 7, "actual_delta": 7, "proposed_group_budget": 27, "reason": "x"},
        ],
    }


def test_validate_pass(parent_allowed_10):
    agg = aggregate(_kb_example_adjustments(), [], _ctx())
    ok, why = validate(_valid_agent_out(), agg)
    assert ok, why


def test_validate_rejects_over_requested(parent_allowed_10):
    agg = aggregate(_kb_example_adjustments(), [], _ctx())
    bad = _valid_agent_out()
    bad["budget_groups"][0]["actual_delta"] = 25      # > requested 20
    ok, _ = validate(bad, agg)
    assert ok is False


def test_validate_rejects_low_bid_in_groups(parent_allowed_10):
    agg = aggregate(_kb_example_adjustments(), [], _ctx())
    bad = _valid_agent_out()
    bad["budget_groups"].append(
        {"group": PORTFOLIO_ELIMINATE, "actual_delta": 5, "proposed_group_budget": 6})
    ok, _ = validate(bad, agg)
    assert ok is False                                # GROUP-004


def test_validate_rejects_over_available():
    # 默认 parent_allowed=0 → available=20；构造 Σactual=30 超额
    agg = aggregate(_kb_example_adjustments(), [], _ctx())
    ok, _ = validate(_valid_agent_out(), agg)          # Σactual=30 > available 20
    assert ok is False


def test_to_budget_summary_agent():
    agg = aggregate(_kb_example_adjustments(), [], _ctx())
    bs = to_budget_summary(_valid_agent_out(), agg, source="agent")
    assert bs["source"] == "agent"
    assert bs["target_budget"] == 100.0
    assert bs["portfolio_constraints"][PORTFOLIO_MAIN] == 70.0
    assert bs["portfolio_constraints"][PORTFOLIO_TEST] == 13.0
    assert bs["portfolio_constraints"][PORTFOLIO_BROAD] == 27.0
    assert bs["portfolio_budget_summary"]["allocation_method"] == "full"
    assert "budget_groups" in bs
