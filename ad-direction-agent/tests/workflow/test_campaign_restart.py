"""淘汰活动复评（KB 21 §7）确定性核心 analyze_eliminated_restart 单测（纯函数，无 I/O）。"""

from datetime import date, timedelta

from app.models.campaign import CampaignPerf, CampaignUnit
from app.workflow.steps.campaign_restart import analyze_eliminated_restart

TODAY = date(2026, 6, 16)


def _unit(cid="c1", mt="EXACT", budget=1.0, bid=0.20, cost=0.0):
    return CampaignUnit(
        campaign_name=f"camp-{cid}", campaign_key=f"camp-{cid} x A1", campaign_id=cid,
        child_asin="A1", keyword_text="kw", match_type=mt,
        current_budget=budget, current_bid=bid,
        perf_7d=CampaignPerf(cost=cost),
    )


def _entry(days_ago=20, spend=None):
    return {"entry_date": TODAY - timedelta(days=days_ago), "eliminate_spend_7d": spend}


def test_case_one_budget_only():
    """情况一：在池出单 ≥1 → $1→$3，Bid 维持 $0.20，AUTO_BATCHABLE，精准测试组。"""
    items, keys = analyze_eliminated_restart(
        [_unit("c1", mt="EXACT")], {"c1": _entry()}, {"c1": 2}, None, today=TODAY,
    )
    assert len(items) == 1
    it = items[0]
    assert it.action == "reactivate_budget_only"
    assert it.triggered_rule == "REACTIVATE_BUDGET_ONLY"
    assert it.proposed_budget == 3.0
    assert it.proposed_bid == 0.20
    assert it.review_level == "AUTO_BATCHABLE"
    assert it.current_portfolio == "精准测试组"
    assert it.current_budget == 1.0 and it.current_bid == 0.20
    assert keys == {"camp-c1 x A1"}


def test_case_two_calibrated_bid():
    """情况二：0 单 + 淘汰前花费>$15 + 有均CPC → $3，Bid=min(0.5,CPC)，MANUAL，广泛归自动广泛组。"""
    items, _ = analyze_eliminated_restart(
        [_unit("c2", mt="BROAD")], {"c2": _entry(spend=20.0)}, {"c2": 0}, 0.35, today=TODAY,
    )
    assert len(items) == 1
    it = items[0]
    assert it.action == "reactivate_with_calibrated_bid"
    assert it.proposed_budget == 3.0
    assert it.proposed_bid == 0.35
    assert it.review_level == "MANUAL_REVIEW"
    assert it.current_portfolio == "自动广泛组"


def test_case_two_bid_floor():
    """情况二 CPC 极低 → proposed_bid 受 $0.20 下限保护。"""
    items, _ = analyze_eliminated_restart(
        [_unit("c3")], {"c3": _entry(spend=20.0)}, {"c3": 0}, 0.05, today=TODAY,
    )
    assert items[0].proposed_bid == 0.20


def test_case_two_no_cpc_data():
    """情况二无均CPC数据 → Bid 维持 $0.20，仍 MANUAL_REVIEW。"""
    items, _ = analyze_eliminated_restart(
        [_unit("c4")], {"c4": _entry(spend=20.0)}, {"c4": 0}, None, today=TODAY,
    )
    assert len(items) == 1
    assert items[0].proposed_bid == 0.20
    assert items[0].review_level == "MANUAL_REVIEW"


def test_no_output_low_spend():
    """0 单且淘汰前花费 ≤$15 → 不产出（维持淘汰）。"""
    items, keys = analyze_eliminated_restart(
        [_unit("c5")], {"c5": _entry(spend=10.0)}, {"c5": 0}, 0.4, today=TODAY,
    )
    assert items == [] and keys == set()


def test_no_output_spend_unknown():
    """0 单且淘汰前花费未知（历史卡无 perf_json）→ 保守不提情况二。"""
    items, _ = analyze_eliminated_restart(
        [_unit("c6")], {"c6": _entry(spend=None)}, {"c6": 0}, 0.4, today=TODAY,
    )
    assert items == []


def test_under_review_window():
    """入池不足 14 天 → 不复评。"""
    items, _ = analyze_eliminated_restart(
        [_unit("c7")], {"c7": _entry(days_ago=5)}, {"c7": 3}, None, today=TODAY,
    )
    assert items == []


def test_no_elimination_record():
    """无 CONFIRMED 淘汰记录（无入池日期）→ 不复评，不臆造。"""
    items, _ = analyze_eliminated_restart(
        [_unit("c8")], {}, {"c8": 3}, None, today=TODAY,
    )
    assert items == []
