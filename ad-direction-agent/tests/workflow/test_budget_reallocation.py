"""预算回算（KB23）纯逻辑单测 — aggregate / validate / to_budget_summary。

无 DB / 无 LLM。约束维度：current_group_budget 兜底 = 父目标×60/20/20，
回算在 budget_pool(=父目标+允许净增) 上分配，proposed 守恒到 pool。
"""

import pytest

from app.config.settings import settings
from app.models.campaign import (
    CampaignAdjustmentItem,
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


def test_aggregate_fallback_constraint_anchors_on_parent_target():
    # 无 portfolio_data → 回退 parent_target×60/20/20 兜底
    agg = aggregate(_adjustments(), [], _ctx(140.0))
    p = agg["parent"]
    assert p["budget_pool"] == 140.0
    assert p["constraint_basis"] == "fallback_share_60_20_20"
    assert p["available_for_increase"] == 20.0  # low_bid_release=20 + allowed=0
    by = {g["group"]: g for g in agg["groups"]}
    assert by[PORTFOLIO_MAIN]["current_group_budget"] == 84.0     # 140×0.6
    assert by[PORTFOLIO_TEST]["current_group_budget"] == 28.0     # 140×0.2
    assert by[PORTFOLIO_BROAD]["current_group_budget"] == 28.0
    assert by[PORTFOLIO_MAIN]["group_requested_delta"] == 20.0
    assert by[PORTFOLIO_TEST]["group_requested_delta"] == 3.0
    assert by[PORTFOLIO_BROAD]["group_requested_delta"] == 7.0
    assert agg["low_bid_group"]["moved_in_count"] == 1
    assert p["low_bid_retention_release"] == 20.0
    assert p["priority_context"]["has_ranking_push"] is True


def test_aggregate_pool_grows_with_parent_allowed(parent_allowed_10):
    agg = aggregate(_adjustments(), [], _ctx(140.0))
    assert agg["parent"]["budget_pool"] == 150.0          # 140 + 10 允许净增


def test_aggregate_portfolio_level_fields():
    # 组合级字段来自 portfolio_data：acos_7d / spend_utilization / daily_spend
    pf = {
        PORTFOLIO_MAIN: {"budget": 40.0, "daily_spend": 36.5, "acos_7d": 0.24},
        PORTFOLIO_TEST: {"budget": 12.0, "daily_spend": 7.8, "acos_7d": 0.30},
        PORTFOLIO_BROAD: {"budget": 18.0, "daily_spend": None, "acos_7d": None},
    }
    agg = aggregate(_adjustments(), [], _ctx(), portfolio_data=pf)
    assert agg["parent"]["constraint_basis"] == "portfolio"
    by = {g["group"]: g for g in agg["groups"]}
    main = by[PORTFOLIO_MAIN]
    assert main["current_group_budget"] == 40.0
    assert main["daily_spend"] == 36.5
    assert main["spend_utilization"] == round(36.5 / 40.0, 2)   # 0.91
    assert main["acos_7d"] == 0.24
    assert "campaigns" not in main                              # 活动明细已移除
    broad = by[PORTFOLIO_BROAD]
    assert broad["spend_utilization"] is None                   # daily_spend=None 不计算
    assert broad["acos_7d"] is None


def test_aggregate_keeps_net_demand_signals():
    # group_requested_delta / new_requested_delta 保留（组合级净需求，非活动明细）
    agg = aggregate(_adjustments(), [], _ctx(140.0))
    by = {g["group"]: g for g in agg["groups"]}
    assert by[PORTFOLIO_MAIN]["group_requested_delta"] == 20.0
    assert by[PORTFOLIO_TEST]["group_requested_delta"] == 3.0
    assert by[PORTFOLIO_BROAD]["group_requested_delta"] == 7.0
    assert "campaigns" not in by[PORTFOLIO_MAIN]


def test_aggregate_priority_context_acos_and_budget_sum_after():
    # priority_context 透传 target_acos / effective_acos_tolerance；
    # campaign_budget_sum_after = 挪组后+新增后组内活动预算之和（统计值，恢复旧 campaigns[].proposed 求和）
    ctx = CampaignStrategyContext(
        parent_asin="B0TEST", product_level="重点产品 (P1)", season_stage="旺季准备",
        ad_purposes=["排名型"], ad_directions=["推进自然位"],
        daily_budget=140.0, daily_budget_source="override",
        target_acos=25, effective_acos_tolerance=40.0,
    )
    agg = aggregate(_adjustments(), [], ctx)
    pc = agg["parent"]["priority_context"]
    assert pc["target_acos"] == 25
    assert pc["effective_acos_tolerance"] == 40.0
    by = {g["group"]: g for g in agg["groups"]}
    assert by[PORTFOLIO_MAIN]["campaign_budget_sum_after"] == 70.0    # camp_main proposed
    assert by[PORTFOLIO_TEST]["campaign_budget_sum_after"] == 13.0    # camp_test proposed
    assert by[PORTFOLIO_BROAD]["campaign_budget_sum_after"] == 27.0   # camp_broad proposed
    # 淘汰活动不计入任何活跃组
    assert by[PORTFOLIO_MAIN]["campaign_budget_sum_after"] == 70.0


def test_aggregate_group_by_target_priority():
    # EXACT_TRANSITION 挪组活动按 target_campaign_group_type 归位（主力→测试降级）
    items = [
        _adj("camp_main", "Main KW", 50, 55, PORTFOLIO_MAIN),
        _adj("camp_demote", "Demote KW", 30, 20, PORTFOLIO_MAIN, match="EXACT"),
        _adj("camp_broad", "Broad KW", 20, 27, PORTFOLIO_BROAD, match="BROAD"),
    ]
    items[1].target_campaign_group_type = "exact_testing_group"   # 挪组目标：主力→测试
    agg = aggregate(items, [], _ctx(140.0))
    by = {g["group"]: g for g in agg["groups"]}
    assert by[PORTFOLIO_TEST]["campaign_budget_sum_after"] == 20.0   # camp_demote 按挪组目标归测试组
    assert by[PORTFOLIO_TEST]["group_requested_delta"] == 20.0      # 跨组进入 +20（proposed）
    assert by[PORTFOLIO_MAIN]["campaign_budget_sum_after"] == 55.0   # 主力组只剩 camp_main
    assert by[PORTFOLIO_MAIN]["group_requested_delta"] == -25.0      # camp_main +5，camp_demote 离开释放 -30


def test_aggregate_cross_group_move_accounting():
    # 跨组移动（主力 $30→$20 降入测试组）：来源组释放 -current，目标组新增 +proposed
    items = [_adj("camp_demote", "Demote KW", 30, 20, PORTFOLIO_MAIN, match="EXACT")]
    items[0].target_campaign_group_type = "exact_testing_group"
    agg = aggregate(items, [], _ctx())
    by = {g["group"]: g for g in agg["groups"]}
    assert by[PORTFOLIO_MAIN]["group_requested_delta"] == -30.0   # 活动离开释放 -30
    assert by[PORTFOLIO_TEST]["group_requested_delta"] == 20.0    # 活动进入新增 +20
    assert by[PORTFOLIO_MAIN]["campaign_budget_sum_after"] == 0.0
    assert by[PORTFOLIO_TEST]["campaign_budget_sum_after"] == 20.0





def _agent_out(main=90.0, test=25.0, broad=25.0, *, cur_main=84.0, cur_test=28.0, cur_broad=28.0):
    return {
        "allocation_method": "weighted_main",
        "parent": {"proposed_total_group_budget": main + test + broad, "explanation": "向主力倾斜"},
        "budget_groups": [
            {"group": PORTFOLIO_MAIN, "current_group_budget": cur_main, "proposed_group_budget": main, "reason": "x"},
            {"group": PORTFOLIO_TEST, "current_group_budget": cur_test, "proposed_group_budget": test, "reason": "x"},
            {"group": PORTFOLIO_BROAD, "current_group_budget": cur_broad, "proposed_group_budget": broad, "reason": "x"},
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


def test_validate_allows_filling_pool_below_parent():
    # 三组当前之和(70) < pool(100)，填满 pool 正增长 30 超 available_for_increase(0)
    # 但 ≤ budget_pool → 应放行（只受父目标硬顶约束）
    pf = {
        PORTFOLIO_MAIN: {"budget": 30.0},
        PORTFOLIO_TEST: {"budget": 20.0},
        PORTFOLIO_BROAD: {"budget": 20.0},
    }
    items = [_adj("camp_main", "Main KW", 30, 32, PORTFOLIO_MAIN)]
    agg = aggregate(items, [], _ctx(100.0), portfolio_data=pf)
    assert agg["parent"]["budget_pool"] == 100.0
    assert agg["parent"]["available_for_increase"] == 0.0
    out = _agent_out(45, 25, 30, cur_main=30.0, cur_test=20.0, cur_broad=20.0)  # sum=100=pool
    ok, why = validate(out, agg)
    assert ok, why


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


# ── P0-D 暂停(paused)预算释放 ──────────────────────────────

def test_aggregate_pause_releases_full_budget():
    """暂停释放整预算（可再分配）；不迁低价池（low_bid_release 不含暂停）。"""
    adj = [
        _adj("camp_pause1", "Pause1", 20.0, 20.0, PORTFOLIO_BROAD, action="paused", match="BROAD"),
        _adj("camp_pause2", "Pause2", 10.0, 10.0, PORTFOLIO_BROAD, action="paused", match="BROAD"),
    ]
    agg = aggregate(adj, [], _ctx(140.0))
    p = agg["parent"]
    assert p["other_campaign_release"] == 30.0, "暂停释放应等于活动当前预算之和"
    assert p["low_bid_retention_release"] == 0.0, "暂停不迁低价池，不得计入 low_bid_release"
    assert p["available_for_increase"] == 30.0  # 暂停释放可再分配（allowed=0）
    # 会计平衡：有加就有减——暂停活动从 auto_broad_group 释放，组需求 -30
    by = {g["group"]: g for g in agg["groups"]}
    assert by[PORTFOLIO_BROAD]["group_requested_delta"] == -30.0, \
        "暂停活动应从所属组扣减预算（组需求 -cur）"


def test_aggregate_eliminate_and_pause_releases_separate():
    """淘汰（留$1）与暂停（整释放）并行，各自独立记账。"""
    adj = [
        _adj("camp_elim", "Elim", 21.0, 1.0, PORTFOLIO_ELIMINATE,
             action="eliminate_to_low_bid_pool"),
        _adj("camp_pause", "Pause", 15.0, 15.0, PORTFOLIO_BROAD, action="paused", match="BROAD"),
    ]
    agg = aggregate(adj, [], _ctx(140.0))
    p = agg["parent"]
    assert p["low_bid_retention_release"] == 20.0   # eliminate: 21-1（留 $1 在低价池）
    assert p["other_campaign_release"] == 15.0      # pause: 整 15
    assert p["available_for_increase"] == 35.0      # 20+15+allowed 0
