"""campaign_guardrails.py 确定性护栏 单元测试。

使用 pydantic CampaignAdjustmentItem 构造测试数据，
不 mock LLM / MCP / DB。
"""
from __future__ import annotations

import pytest
from app.models.campaign import CampaignAdjustmentItem
from app.workflow.steps.campaign_guardrails import (
    LOW_BID_MIN, LOW_BID_MAX, LOW_BUDGET_MAX, BID_HARD_CAP, BUDGET_CAP,
    is_strictly_in_low_bid_pool, _is_in_elimination_pool,
    apply_all, GuardrailPass, _p5_protection_reversal,
)


def _make_item(**overrides) -> CampaignAdjustmentItem:
    """默认构造一个"数据充足、非核心、老活动"的 item，避免误触 P1 样本不足保护。"""
    defaults = {
        "campaign_name": "test_campaign",
        "campaign_key": "test_key",
        "action": "eliminate_to_low_bid_pool",
        "match_type": "EXACT",  # 低价捡漏组仅限精准（KB10 §1.6 / ONT-016）
        "current_budget": 10.0,
        "current_bid": 0.50,
        "proposed_budget": 1.0,
        "proposed_bid": 0.20,
        "perf_7d": {"cost": 50.0, "clicks": 50, "orders": 5},
        "days_online": 30,
        "days_since_reactivation": -1,
        "is_core": False,
        "placement_adjustments": [],
        "negative_keywords": [],
        "direction": {},
    }
    defaults.update(overrides)
    return CampaignAdjustmentItem(**defaults)


# ── 常量 ─────────────────────────────────────────────

def test_constants():
    assert LOW_BID_MIN == 0.10
    assert LOW_BID_MAX == 0.20
    assert LOW_BUDGET_MAX == 1.00
    assert BID_HARD_CAP == 3.00
    assert BUDGET_CAP == 200.0


# ── pool 谓词 ─────────────────────────────────────────

def test_strictly_in_pool_hit():
    assert is_strictly_in_low_bid_pool(0.20, 1.00) is True
    assert is_strictly_in_low_bid_pool(0.15, 0.50) is True
    assert is_strictly_in_low_bid_pool(0.20, 0.99) is True


def test_strictly_in_pool_miss():
    assert is_strictly_in_low_bid_pool(0.25, 1.00) is False   # bid over
    assert is_strictly_in_low_bid_pool(0.20, 1.50) is False   # budget over
    assert is_strictly_in_low_bid_pool(None, 0.50) is False   # None bid
    assert is_strictly_in_low_bid_pool(0.10, None) is False   # None budget


def test_elimination_pool_or():
    assert _is_in_elimination_pool(0.10, 10.0) is True   # bid hit
    assert _is_in_elimination_pool(0.50, 1.00) is True   # budget hit
    assert _is_in_elimination_pool(0.10, 1.00) is True   # both hit
    assert _is_in_elimination_pool(0.15, 2.00) is False  # neither


# ── P0: 核心词禁淘汰 ─────────────────────────────────

def test_p0_core_protect_blocks_eliminate():
    item = _make_item(is_core=True, action="eliminate_to_low_bid_pool")
    gp = apply_all([item])
    assert gp.corrections == 1
    assert gp.results[0].rule_id == "P0_CORE_PROTECT"
    assert item.action == "keep"
    assert item.proposed_budget == item.current_budget


def test_p0_core_protect_ignores_keep():
    item = _make_item(is_core=True, action="keep")
    gp = apply_all([item])
    assert gp.corrections == 0


def test_p0_ignores_non_core():
    item = _make_item(is_core=False, action="eliminate_to_low_bid_pool")
    gp = apply_all([item])
    if any(r.rule_id == "P0_CORE_PROTECT" for r in gp.results):
        pytest.fail("P0 不应触发（非核心词）")


# ── P1: 样本不足只禁淘汰，调整透传 ────────────────────

def test_p1_blocks_eliminate_when_sample_insufficient():
    """cost < $5 or clicks < 10 or days ≤ 3 → 禁止淘汰"""
    item = _make_item(perf_7d={"cost": 2.0, "clicks": 3, "orders": 0},
                       days_online=30, action="eliminate_to_low_bid_pool",
                       negative_keywords=[{"keyword": "irrelevant query"}])
    gp = apply_all([item])
    assert any(r.rule_id == "P1_SAMPLE_INSUFFICIENT" for r in gp.results)
    assert item.action == "keep"
    # 否词是独立的叠加建议；P1 仅撤销淘汰，不得丢弃否词。
    assert item.negative_keywords == [{"keyword": "irrelevant query"}]


def test_p1_retry_instruction_blocks_eliminate_without_keep_only_bias():
    item = _make_item(perf_7d={"cost": 2.0, "clicks": 3, "orders": 0},
                       days_online=30, action="eliminate_to_low_bid_pool")
    gp = apply_all([item])
    instruction = next(r.retry_instruction for r in gp.results if r.rule_id == "P1_SAMPLE_INSUFFICIENT")
    assert "不得直接淘汰" in instruction
    assert "硬淘汰" in instruction
    assert "小幅 bid、预算、广告位调整或维持" in instruction
    assert "一律" not in instruction


def test_p1_allows_adjustment_when_sample_insufficient():
    """样本不足时 adjust_bid 放行，不变 keep"""
    item = _make_item(perf_7d={"cost": 2.0, "clicks": 3, "orders": 0},
                       days_online=30, action="adjust_bid",
                       proposed_bid=1.0, proposed_budget=20.0)
    gp = apply_all([item])
    assert not any(r.rule_id == "P1_SAMPLE_INSUFFICIENT" for r in gp.results)
    assert item.action == "adjust_bid"


def test_p1_does_not_block_eliminate_when_sufficient():
    item = _make_item(perf_7d={"cost": 20.0, "clicks": 20, "orders": 5},
                       days_online=10, action="eliminate_to_low_bid_pool")
    gp = apply_all([item])
    # P0/P2 不触发；P1 不触发；P3 可能有 orders>0 不触发
    assert not any(r.rule_id == "P1_SAMPLE_INSUFFICIENT" for r in gp.results)


def test_p1_days_online_boundary():
    """days_online=3 → 保护；days_online=4 → 不保护"""
    item1 = _make_item(days_online=3, action="eliminate_to_low_bid_pool",
                        perf_7d={"cost": 100.0, "clicks": 100, "orders": 10})
    gp1 = apply_all([item1])
    assert any(r.rule_id == "P1_SAMPLE_INSUFFICIENT" for r in gp1.results)

    item2 = _make_item(days_online=4, action="eliminate_to_low_bid_pool",
                        perf_7d={"cost": 100.0, "clicks": 100, "orders": 10})
    gp2 = apply_all([item2])
    assert not any(r.rule_id == "P1_SAMPLE_INSUFFICIENT" for r in gp2.results)


# ── P2: 复评抖动保护 ─────────────────────────────────

def test_p2_reactivation_protect():
    item = _make_item(days_since_reactivation=2, action="eliminate_to_low_bid_pool",
                       # 默认 perf_7d cost=50 足够，不触发 P1
                       days_online=30)
    gp = apply_all([item])
    assert any(r.rule_id == "P2_REACTIVATION_PROTECT" for r in gp.results)
    assert item.action == "keep"


def test_p2_reactivation_not_protected():
    item = _make_item(days_since_reactivation=5, action="eliminate_to_low_bid_pool")
    gp = apply_all([item])
    assert not any(r.rule_id == "P2_REACTIVATION_PROTECT" for r in gp.results)


# ── P3: 强制淘汰 (OR, 无出单) ─────────────────────────

def test_p3_force_eliminate_no_orders():
    item = _make_item(perf_7d={"cost": 20, "clicks": 20, "orders": 0},
                       current_bid=0.08, current_budget=5.0,
                       action="adjust_bid")
    gp = apply_all([item])
    assert any(r.rule_id == "P3_FORCE_ELIMINATE" for r in gp.results)
    assert item.action == "eliminate_to_low_bid_pool"


def test_p3_blocked_by_orders():
    item = _make_item(perf_7d={"cost": 10.0, "clicks": 5, "orders": 2},
                       current_bid=0.08, current_budget=5.0,
                       action="adjust_bid")
    gp = apply_all([item])
    assert not any(r.rule_id == "P3_FORCE_ELIMINATE" for r in gp.results)


def test_p3_bid_between_thresholds():
    """bid 0.15 (0.10~0.20) + budget > 1.00 → 不触发 P3"""
    item = _make_item(perf_7d={"cost": 0, "clicks": 0, "orders": 0},
                       current_bid=0.15, current_budget=5.0,
                       action="adjust_bid")
    gp = apply_all([item])
    assert not any(r.rule_id == "P3_FORCE_ELIMINATE" for r in gp.results)


def test_p3_does_not_force_eliminate_non_exact():
    """KB10 §1.6 / ONT-016: 低价捡漏组仅限精准，BROAD 触底也不强制淘汰"""
    item = _make_item(perf_7d={"cost": 20, "clicks": 20, "orders": 0},
                       current_bid=0.08, current_budget=5.0,
                       action="adjust_bid", match_type="BROAD")
    gp = apply_all([item])
    assert not any(r.rule_id == "P3_FORCE_ELIMINATE" for r in gp.results)
    assert item.action == "adjust_bid"


def test_p3_force_eliminate_only_exact():
    """仅 EXACT 活动可被 P3 强制淘汰入低价捡漏组"""
    item = _make_item(perf_7d={"cost": 20, "clicks": 20, "orders": 0},
                       current_bid=0.08, current_budget=5.0,
                       action="adjust_bid", match_type="EXACT")
    gp = apply_all([item])
    assert any(r.rule_id == "P3_FORCE_ELIMINATE" for r in gp.results)
    assert item.action == "eliminate_to_low_bid_pool"


# ── P4: 淘汰值填充 ───────────────────────────────────

def test_p4_bid_in_range_kept():
    """bid 已在 [0.10, 0.20] 区间内 → 维持"""
    item = _make_item(action="eliminate_to_low_bid_pool",
                       proposed_bid=0.15, proposed_budget=1.0)
    gp = apply_all([item])
    assert item.proposed_bid == 0.15
    assert item.proposed_budget == 1.0


def test_p4_fixes_bid_above_max():
    item = _make_item(action="eliminate_to_low_bid_pool",
                       proposed_bid=0.50, proposed_budget=1.0)
    gp = apply_all([item])
    assert item.proposed_bid == LOW_BID_MAX


def test_p4_fixes_bid_below_min():
    item = _make_item(action="eliminate_to_low_bid_pool",
                       proposed_bid=0.05, proposed_budget=1.0)
    gp = apply_all([item])
    assert item.proposed_bid == LOW_BID_MIN


# ── P6: 日预算上限 $200 ──────────────────────────────

def test_p6_budget_cap():
    """P6 在 P7 之后执行，确保不被 P7 先截断"""
    item = _make_item(action="adjust_budget", proposed_budget=300.0,
                       current_budget=100.0,
                       perf_7d={"cost": 500.0, "clicks": 50, "orders": 10})
    gp = apply_all([item])
    assert any(r.rule_id == "P6_BUDGET_CAP" for r in gp.results)
    assert item.proposed_budget == BUDGET_CAP


def test_p6_budget_under_cap():
    item = _make_item(action="adjust_budget", proposed_budget=150.0)
    gp = apply_all([item])
    assert not any(r.rule_id == "P6_BUDGET_CAP" for r in gp.results)


# ── P7: 预算花不完禁加 ───────────────────────────────

def test_p7_blocks_budget_increase_when_low_spend():
    item = _make_item(action="adjust_budget",
                       current_budget=10.0, proposed_budget=30.0,
                       perf_7d={"cost": 3.0, "clicks": 5, "orders": 1})
    gp = apply_all([item])
    assert any(r.rule_id == "P7_BUDGET_LOW_SPEND" for r in gp.results)
    assert item.proposed_budget == item.current_budget


def test_p7_allows_budget_increase_when_enough_spend():
    item = _make_item(action="adjust_budget",
                       current_budget=10.0, proposed_budget=30.0,
                       perf_7d={"cost": 40.0, "clicks": 20, "orders": 5})
    gp = apply_all([item])
    assert not any(r.rule_id == "P7_BUDGET_LOW_SPEND" for r in gp.results)


def test_p7_blocks_budget_increase_when_7d_total_spend_but_daily_utilization_low():
    item = _make_item(action="adjust_budget",
                       current_budget=58.60, proposed_budget=80.0,
                       perf_7d={"cost": 151.80, "clicks": 20, "orders": 5})
    gp = apply_all([item])
    assert any(r.rule_id == "P7_BUDGET_LOW_SPEND" for r in gp.results)
    assert item.proposed_budget == item.current_budget


def test_p7_ignores_eliminate():
    """P1 先保护（样本不足→keep）后，不再叠加预算调整护栏。"""
    item = _make_item(action="eliminate_to_low_bid_pool",
                       current_budget=10.0, proposed_budget=30.0,
                       perf_7d={"cost": 2.0, "clicks": 0, "orders": 0})
    gp = apply_all([item])
    assert any(r.rule_id == "P1_SAMPLE_INSUFFICIENT" for r in gp.results)  # P1 先触发
    assert not any(r.rule_id == "P7_BUDGET_LOW_SPEND" for r in gp.results)
    assert item.action == "keep"


# ── P8: Bid 振幅收敛 ─────────────────────────────────

def test_p8_converges_large_change_low_clicks():
    """变动 > 50% 且 clicks < 10 → 收敛到 30%"""
    item = _make_item(action="adjust_bid",
                       current_bid=1.00, proposed_bid=0.40,  # -60%
                       perf_7d={"cost": 5.0, "clicks": 3, "orders": 0})
    gp = apply_all([item])
    assert any(r.rule_id == "P8_BID_AMPLITUDE" for r in gp.results)
    # 1.00 × 0.70 = 0.70
    assert abs(item.proposed_bid - 0.70) < 0.01


def test_p8_allows_large_change_with_clicks():
    """clicks ≥ 10 → 放行"""
    item = _make_item(action="adjust_bid",
                       current_bid=1.00, proposed_bid=0.40,
                       perf_7d={"cost": 20.0, "clicks": 15, "orders": 3})
    gp = apply_all([item])
    assert not any(r.rule_id == "P8_BID_AMPLITUDE" for r in gp.results)


def test_p8_ignores_small_change():
    item = _make_item(action="adjust_bid",
                       current_bid=1.00, proposed_bid=0.80,  # -20%
                       perf_7d={"cost": 5.0, "clicks": 3, "orders": 0})
    gp = apply_all([item])
    assert not any(r.rule_id == "P8_BID_AMPLITUDE" for r in gp.results)


# ── P9: Bid 硬上限 $3.00 ──────────────────────────────

def test_p9_bid_cap():
    """P9 在 P7/P8 之后执行，current_bid 设得较低避免 P8 触发"""
    item = _make_item(action="adjust_bid", proposed_bid=5.0,
                       current_bid=4.50,  # 变动小，不触发 P8
                       perf_7d={"cost": 100.0, "clicks": 50, "orders": 10})
    gp = apply_all([item])
    assert any(r.rule_id == "P9_BID_CAP" for r in gp.results)
    assert item.proposed_bid == BID_HARD_CAP


def test_p9_bid_under_cap():
    item = _make_item(action="adjust_bid", proposed_bid=2.50)
    gp = apply_all([item])
    assert not any(r.rule_id == "P9_BID_CAP" for r in gp.results)


# ── P10: 广告位 TOS 阻断 ──────────────────────────────

def test_p10_blocks_tos_when_inventory_low():
    item = _make_item(action="adjust_placement",
                       placement_adjustments=[
                           {"placement": "头部", "action": "大涨",
                            "evidence": "test"}
                       ])
    gp = apply_all([item], inventory_days=10, refund_rate=10.0, rating=4.0)
    assert any(r.rule_id == "P10_PLACEMENT_BLOCK" for r in gp.results)
    assert item.placement_adjustments[0]["action"] == "维持"


def test_p10_syncs_pct_when_blocking_tos():
    item = _make_item(action="adjust_placement",
                       placement_adjustments=[
                           {"placement": "头部", "action": "大涨",
                            "current_pct": 20, "proposed_pct": 50,
                            "evidence": "test"}
                       ])
    gp = apply_all([item], inventory_days=10, refund_rate=10.0, rating=4.0)
    assert any(r.rule_id == "P10_PLACEMENT_BLOCK" for r in gp.results)
    assert item.placement_adjustments[0]["action"] == "维持"
    assert item.placement_adjustments[0]["proposed_pct"] == 20


def test_p10_retry_instruction_only_blocks_tos_increase():
    item = _make_item(action="adjust_placement",
                       placement_adjustments=[
                           {"placement": "头部", "action": "大涨",
                            "current_pct": 20, "proposed_pct": 50,
                            "evidence": "test"}
                       ])
    gp = apply_all([item], inventory_days=10, refund_rate=10.0, rating=4.0)
    instruction = next(r.retry_instruction for r in gp.results if r.rule_id == "P10_PLACEMENT_BLOCK")
    assert "不得上调头部 TOS 加价" in instruction
    assert "不代表商品位、其他位、bid 或预算必须维持" in instruction
    assert "全部维持" not in instruction


def test_p10_blocks_tos_when_refund_high():
    item = _make_item(action="adjust_placement",
                       placement_adjustments=[
                           {"placement": "TOS", "action": "小涨", "evidence": "x"}
                       ])
    gp = apply_all([item], inventory_days=20, refund_rate=35.0, rating=4.0)
    assert any(r.rule_id == "P10_PLACEMENT_BLOCK" for r in gp.results)


def test_p10_blocks_tos_when_rating_low():
    item = _make_item(action="adjust_placement",
                       placement_adjustments=[
                           {"placement": "TOP_OF_SEARCH", "action": "大涨", "evidence": "x"}
                       ])
    gp = apply_all([item], inventory_days=20, refund_rate=10.0, rating=3.5)
    assert any(r.rule_id == "P10_PLACEMENT_BLOCK" for r in gp.results)


def test_p10_allows_tos_when_all_ok():
    item = _make_item(action="adjust_placement",
                       placement_adjustments=[
                           {"placement": "头部", "action": "小涨", "evidence": "x"}
                       ])
    gp = apply_all([item], inventory_days=20, refund_rate=10.0, rating=4.0)
    assert not any(r.rule_id == "P10_PLACEMENT_BLOCK" for r in gp.results)


def test_p10_ignores_non_tos():
    item = _make_item(action="adjust_placement",
                       placement_adjustments=[
                           {"placement": "商品", "action": "大涨", "evidence": "x"}
                       ])
    gp = apply_all([item], inventory_days=10, refund_rate=10.0, rating=4.0)
    assert not any(r.rule_id == "P10_PLACEMENT_BLOCK" for r in gp.results)


# ── P11: 新活动禁大降 Bid ─────────────────────────────

def test_p11_caps_large_drop():
    """bid 从 2.00 降到 1.50 → drop=$0.50 > max($0.05, 10%×2.00=$0.20) = $0.20 → 收敛"""
    item = _make_item(action="adjust_bid", days_online=2,
                       current_bid=2.00, proposed_bid=1.50)
    gp = apply_all([item])
    assert any(r.rule_id == "P11_NEW_CAMPAIGN_BID" for r in gp.results)
    # max_drop = min(0.10*2.00, 0.05) = min(0.20, 0.05) = 0.05
    # 结果: current_bid - 0.05 = 1.95
    assert item.proposed_bid == pytest.approx(1.95, rel=0.01)


def test_p11_retry_instruction_allows_small_drop_or_other_light_adjustment():
    item = _make_item(action="adjust_bid", days_online=2,
                       current_bid=2.00, proposed_bid=1.50)
    gp = apply_all([item])
    instruction = next(r.retry_instruction for r in gp.results if r.rule_id == "P11_NEW_CAMPAIGN_BID")
    assert "不应大幅下调" in instruction
    assert "可收敛到不超过 $0.05 的降幅" in instruction
    assert "维持或其他轻量调整" in instruction
    assert "不得降 bid" not in instruction


def test_p11_allows_small_drop():
    """drop=$0.03 < $0.05 (and < 10% of $1.00) → 放行"""
    item = _make_item(action="adjust_bid", days_online=2,
                       current_bid=1.00, proposed_bid=0.97)
    gp = apply_all([item])
    assert not any(r.rule_id == "P11_NEW_CAMPAIGN_BID" for r in gp.results)


def test_p11_ignores_old_campaigns():
    item = _make_item(action="adjust_bid", days_online=5,
                       current_bid=2.00, proposed_bid=1.00)
    gp = apply_all([item])
    assert not any(r.rule_id == "P11_NEW_CAMPAIGN_BID" for r in gp.results)


def test_p11_ignores_eliminate():
    """P1 先保护（新活动→keep）后，不再叠加新活动降 bid 护栏。"""
    item = _make_item(action="eliminate_to_low_bid_pool", days_online=2,
                       current_bid=2.00, proposed_bid=0.10)
    gp = apply_all([item])
    assert any(r.rule_id == "P1_SAMPLE_INSUFFICIENT" for r in gp.results)   # P1 先触发
    assert not any(r.rule_id == "P11_NEW_CAMPAIGN_BID" for r in gp.results)
    assert item.action == "keep"


# ── 集成: 多规则顺序 ──────────────────────────────────

def test_p0_before_p3():
    """核心词 is_core + 无出单 + bid 触底 → P0 先保护，P3 不应再淘汰"""
    item = _make_item(is_core=True, action="eliminate_to_low_bid_pool",
                       perf_7d={"cost": 0, "clicks": 0, "orders": 0},
                       current_bid=0.05, current_budget=5.0)
    gp = apply_all([item])
    assert any(r.rule_id == "P0_CORE_PROTECT" for r in gp.results)
    assert not any(r.rule_id == "P3_FORCE_ELIMINATE" for r in gp.results)
    assert item.action == "keep"


def test_p1_before_p3():
    """样本不足但 P3 高于 P1：adjust_bid + 无订单 + bid 触底 → P3 强制淘汰"""
    item = _make_item(perf_7d={"cost": 2.0, "clicks": 3, "orders": 0},
                       days_online=30, action="adjust_bid",
                       current_bid=0.05, current_budget=5.0)
    gp = apply_all([item])
    # P1 不触发（action 不是 eliminate）；P3 不再被样本不足阻断
    assert not any(r.rule_id == "P1_SAMPLE_INSUFFICIENT" for r in gp.results)
    assert any(r.rule_id == "P3_FORCE_ELIMINATE" for r in gp.results)
    assert item.action == "eliminate_to_low_bid_pool"


def test_p4_after_p3():
    """P3 强制淘汰后 P4 填充值"""
    item = _make_item(action="adjust_bid",
                       perf_7d={"cost": 20, "clicks": 20, "orders": 0},
                       current_bid=0.05, current_budget=0.50,
                       proposed_bid=0.50, proposed_budget=5.0)
    gp = apply_all([item])
    assert any(r.rule_id == "P3_FORCE_ELIMINATE" for r in gp.results)
    assert item.action == "eliminate_to_low_bid_pool"
    # P4 修正 bid 到 [0.10, 0.20]，预算到 $1.00
    assert item.proposed_budget == 1.0
    assert LOW_BID_MIN <= item.proposed_bid <= LOW_BID_MAX


def test_p5_reverses_core_eliminate_as_fallback():
    item = _make_item(is_core=True, action="eliminate_to_low_bid_pool",
                       perf_7d={"cost": 20, "clicks": 20, "orders": 0},
                       current_bid=0.05, current_budget=5.0,
                       proposed_bid=0.50, proposed_budget=5.0)
    gp = GuardrailPass()
    _p5_protection_reversal(item, gp)
    assert any(r.rule_id == "P5_PROTECTION_REVERSAL" for r in gp.results)
    assert item.action == "keep"


def test_p5_does_not_reverse_sample_insufficient_eliminate():
    item = _make_item(action="eliminate_to_low_bid_pool",
                       perf_7d={"cost": 2.0, "clicks": 3, "orders": 0},
                       days_online=30,
                       current_bid=0.05, current_budget=5.0)
    gp = GuardrailPass()
    _p5_protection_reversal(item, gp)
    assert not any(r.rule_id == "P5_PROTECTION_REVERSAL" for r in gp.results)
    assert item.action == "eliminate_to_low_bid_pool"


def test_p5_reverses_non_exact_eliminate():
    """KB10 §1.6 / ONT-016: 非精准活动不得迁入低价捡漏组，LLM 误判淘汰则拉回 keep"""
    item = _make_item(action="eliminate_to_low_bid_pool",
                       perf_7d={"cost": 50.0, "clicks": 50, "orders": 0},
                       days_online=30,
                       current_bid=0.50, current_budget=10.0,
                       match_type="PHRASE")
    gp = GuardrailPass()
    _p5_protection_reversal(item, gp)
    assert any(r.rule_id == "P5_PROTECTION_REVERSAL" for r in gp.results)
    assert item.action == "keep"


def test_p5_keeps_exact_eliminate_when_not_protected():
    """精准活动、无核心词/复评保护 → 不触发 P5 反修正"""
    item = _make_item(action="eliminate_to_low_bid_pool",
                       perf_7d={"cost": 50.0, "clicks": 50, "orders": 0},
                       days_online=30,
                       current_bid=0.50, current_budget=10.0,
                       match_type="EXACT")
    gp = GuardrailPass()
    _p5_protection_reversal(item, gp)
    assert not any(r.rule_id == "P5_PROTECTION_REVERSAL" for r in gp.results)
    assert item.action == "eliminate_to_low_bid_pool"
