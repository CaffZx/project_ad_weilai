"""预算回算（KB23）纯逻辑单测 — aggregate / validate / to_budget_summary。

无 DB / 无 LLM。约束维度：current_group_budget 兜底 = 父目标×60/20/20，
回算在 budget_pool(=父目标+允许净增) 上分配，proposed 守恒到 pool。
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
    classify,
)


def _adj(key, kw, cur, prop, group, action="adjust_budget", match="EXACT"):
    return CampaignAdjustmentItem(
        campaign_name=key, campaign_key=key, keyword_text=kw, match_type=match,
        action=action, ai_portfolio_class=group,
        current_budget=cur, proposed_budget=prop,
    )


def _ctx(target=140.0):
    return CampaignStrategyContext(
        parent_asin="B0TEST", product_level="重点产品 (P1)", season_stage="旺季准备",
        ad_purposes=["排名型"], ad_directions=["推进自然位"],
        daily_budget=target, daily_budget_source="override",
    )


def _adjustments():
    # 主力需求 +20 / 测试 +3 / 广泛 +7；一条转入低价捡漏释放 20（21→1）
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


def test_aggregate_base_constraint_anchors_on_parent_target():
    # 默认 parent_allowed=0 → pool = 父目标 140；base_constraint = 140×60/20/20
    agg = aggregate(_adjustments(), [], _ctx(140.0))
    p = agg["parent"]
    assert p["budget_pool"] == 140.0
    assert p["constraint_basis"] == "fallback_share_60_20_20"
    by = {g["group"]: g for g in agg["groups"]}
    assert by[PORTFOLIO_MAIN]["base_constraint"] == 84.0     # 140×0.6
    assert by[PORTFOLIO_TEST]["base_constraint"] == 28.0     # 140×0.2
    assert by[PORTFOLIO_BROAD]["base_constraint"] == 28.0
    # delta 只作需求信号（不累加成绝对预算）
    assert by[PORTFOLIO_MAIN]["group_requested_delta"] == 20.0
    assert by[PORTFOLIO_TEST]["group_requested_delta"] == 3.0
    assert by[PORTFOLIO_BROAD]["group_requested_delta"] == 7.0
    assert agg["low_bid_group"]["moved_in_count"] == 1
    assert p["low_bid_retention_release"] == 20.0
    assert p["priority_context"]["has_ranking_push"] is True


def test_aggregate_pool_grows_with_parent_allowed(parent_allowed_10):
    agg = aggregate(_adjustments(), [], _ctx(140.0))
    assert agg["parent"]["budget_pool"] == 150.0          # 140 + 10 允许净增


def test_search_volume_and_acos_join():
    units = [CampaignUnit(
        campaign_name="camp_main", campaign_key="camp_main", child_asin="B0C1",
        keyword_text="Main KW", match_type="EXACT", perf_7d=CampaignPerf(acos=22.0),
    )]
    agg = aggregate(_adjustments(), units, _ctx(), search_volume_map={"main kw": 1000})
    camp = next(g for g in agg["groups"] if g["group"] == PORTFOLIO_MAIN)["campaigns"][0]
    assert camp["search_volume"] == 1000     # join by keyword_text.lower()
    assert camp["acos"] == 22.0              # 透传 perf_7d.acos
    agg2 = aggregate(_adjustments(), [], _ctx(), search_volume_map={})
    assert next(g for g in agg2["groups"] if g["group"] == PORTFOLIO_MAIN)["campaigns"][0]["search_volume"] is None


def _agent_out(main=90.0, test=25.0, broad=25.0):
    # 守恒到 pool=140（默认），向主力倾斜
    return {
        "allocation_method": "weighted_main",
        "parent": {"proposed_total_group_budget": main + test + broad, "explanation": "向主力倾斜"},
        "budget_groups": [
            {"group": PORTFOLIO_MAIN, "base_constraint": 84, "proposed_group_budget": main, "reason": "x"},
            {"group": PORTFOLIO_TEST, "base_constraint": 28, "proposed_group_budget": test, "reason": "x"},
            {"group": PORTFOLIO_BROAD, "base_constraint": 28, "proposed_group_budget": broad, "reason": "x"},
        ],
    }


def test_validate_pass_when_sum_within_pool():
    agg = aggregate(_adjustments(), [], _ctx(140.0))       # pool=140
    ok, why = validate(_agent_out(90, 25, 25), agg)        # sum=140
    assert ok, why


def test_validate_rejects_over_pool():
    agg = aggregate(_adjustments(), [], _ctx(140.0))       # pool=140
    ok, _ = validate(_agent_out(220, 11, 19), agg)         # sum=250 ≫ 140
    assert ok is False                                     # 守恒/父目标硬顶


def test_validate_rejects_low_bid_in_groups():
    agg = aggregate(_adjustments(), [], _ctx(140.0))
    bad = _agent_out(80, 25, 25)
    bad["budget_groups"].append(
        {"group": PORTFOLIO_ELIMINATE, "proposed_group_budget": 10})
    ok, _ = validate(bad, agg)
    assert ok is False                                     # GROUP-004


def test_to_budget_summary_constraints_sum_within_target():
    agg = aggregate(_adjustments(), [], _ctx(140.0))
    bs = to_budget_summary(_agent_out(90, 25, 25), agg, source="agent")
    assert bs["source"] == "agent"
    assert bs["target_budget"] == 140.0
    pc = bs["portfolio_constraints"]
    assert pc[PORTFOLIO_MAIN] == 90.0 and pc[PORTFOLIO_TEST] == 25.0 and pc[PORTFOLIO_BROAD] == 25.0
    # 关键回归：3 组约束之和不再超父目标
    assert round(sum(pc.values()), 2) <= 140.0 + 0.5


# ── 终态组合分类：按 proposed 定组（KB23 §3.1B/§3.5/§3.7）─────────────────────

def _unit(cur_budget, match="EXACT", cur_bid=0.5):
    return CampaignUnit(
        campaign_name="c", campaign_key="c", child_asin="B0", keyword_text="kw",
        match_type=match, current_budget=cur_budget, current_bid=cur_bid,
    )


def test_classify_proposed_upgrade_testing_to_main():
    # current $3(测试级) + proposed $7 → 升主力组（§3.1B/§3.5）
    assert classify(_unit(3.0), llm_action="adjust_budget", effective_budget=7.0) == PORTFOLIO_MAIN


def test_classify_proposed_downgrade_main_to_testing():
    # current $7(主力级) + proposed $3 → 降测试组（§3.7）
    assert classify(_unit(7.0), llm_action="adjust_budget", effective_budget=3.0) == PORTFOLIO_TEST


def test_classify_default_uses_current_when_no_effective_budget():
    # 不传 effective_budget → 按 current（预分类阶段零改）
    assert classify(_unit(3.0)) == PORTFOLIO_TEST
    assert classify(_unit(7.0)) == PORTFOLIO_MAIN


def test_classify_eliminate_and_broad_unaffected_by_proposed():
    # 淘汰 action 优先，proposed 不影响
    assert classify(_unit(7.0), llm_action="eliminate_to_low_bid_pool", effective_budget=7.0) == PORTFOLIO_ELIMINATE
    # 广泛永不进主力，即便 proposed≥5
    assert classify(_unit(3.0, match="BROAD"), effective_budget=7.0) == PORTFOLIO_BROAD


def test_classify_elimination_pool_stays_by_current_no_revival_gate():
    # 本期不加复活闸：在淘汰池($1/$0.20)即归淘汰组，即使 proposed=$6
    assert classify(_unit(1.0, cur_bid=0.20), llm_action="adjust_budget", effective_budget=6.0) == PORTFOLIO_ELIMINATE
    assert bs["portfolio_budget_summary"]["budget_pool"] == 140.0
