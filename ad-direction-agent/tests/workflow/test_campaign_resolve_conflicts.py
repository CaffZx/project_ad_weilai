"""_resolve_budget_conflicts 护栏优先级单测。

当前优先级：
  P3 硬淘汰（无单且 bid≤$0.10 或 budget≤$1）高于 P1 样本不足。
  P1 只拦 LLM 对样本不足活动的非硬淘汰误判。
  P2 复评保护仍高于 P3。

样本不足条件：
  1. 上线 ≤3 天（新活动样本不足）
  2. 测试期 + 上线 <14 天（测试期样本保护）
"""

import os
os.environ["LLM_GLOBAL_CONCURRENCY"] = "420"

import pytest
from app.models.campaign import CampaignAdjustmentItem
from app.workflow.steps.campaign import _normalize_action, _resolve_budget_conflicts


def _item(name="c1", action="keep", current_budget=1.0, current_bid=0.20,
          proposed_budget=None, proposed_bid=None, days_online=-1,
          days_since_reactivation=-1):
    """工厂：构造 CampaignAdjustmentItem，默认模拟已淘汰池活动。"""
    return CampaignAdjustmentItem(
        campaign_name=name,
        campaign_key=f"{name} x A1",
        action=action,
        current_budget=current_budget,
        current_bid=current_bid,
        proposed_budget=proposed_budget,
        proposed_bid=proposed_bid,
        days_online=days_online,
        days_since_reactivation=days_since_reactivation,
    )


# ── 新活动样本不足：P3 硬淘汰优先 ────────────────────────────────

def test_new_campaign_low_budget_force_eliminated_by_p3():
    """新活动上线 2 天、预算=$1，LLM 判 keep → P3 硬淘汰优先。"""
    item = _item(days_online=2, current_budget=1.0, current_bid=0.50, action="keep")
    warnings = _resolve_budget_conflicts([item])
    assert item.action == "eliminate_to_low_bid_pool"
    assert any("强制淘汰" in w for w in warnings)


def test_new_campaign_low_bid_force_eliminated_by_p3():
    """新活动上线 2 天、bid=$0.05，LLM 判 adjust_bid → P3 硬淘汰优先。"""
    item = _item(days_online=2, current_budget=5.0, current_bid=0.05,
                 proposed_bid=0.10, action="adjust_bid")
    warnings = _resolve_budget_conflicts([item])
    assert item.action == "eliminate_to_low_bid_pool"
    assert any("强制淘汰" in w for w in warnings)


def test_new_campaign_boundary_3_days_force_eliminated_when_budget_touch_floor():
    """上线恰好 3 天但预算触底 → P3 硬淘汰优先。"""
    item = _item(days_online=3, current_budget=1.0, current_bid=0.50, action="keep")
    _resolve_budget_conflicts([item])
    assert item.action == "eliminate_to_low_bid_pool"


def test_new_campaign_boundary_4_days_not_protected():
    """上线 4 天 → 不再受保护，触发强制淘汰。"""
    item = _item(days_online=4, current_budget=1.0, current_bid=0.50, action="keep")
    _resolve_budget_conflicts([item])
    assert item.action == "eliminate_to_low_bid_pool"
    assert item.proposed_budget == 1.0
    assert item.proposed_bid == 0.20


# ── 新活动保护：LLM 误判淘汰 → 强制修正为 keep ─────────────────

def test_new_campaign_llm_misjudge_eliminate_corrected_to_keep():
    """新活动上线 2 天，LLM 误判 eliminate → 强制修正为 keep。"""
    item = _item(days_online=2, current_budget=5.0, current_bid=0.50,
                 proposed_budget=1.0, proposed_bid=0.20,
                 action="eliminate_to_low_bid_pool")
    item.direction = {"bid": "down", "budget": "down"}
    item.placement_adjustments = [{"placement": "头部", "action": "大降"}]
    item.negative_keywords = [{"keyword": "bad"}]
    warnings = _resolve_budget_conflicts([item])

    assert item.action == "keep"
    assert item.proposed_budget == 5.0   # 回退 current
    assert item.proposed_bid == 0.50     # 回退 current
    assert item.direction == {}
    assert item.placement_adjustments == []
    # 样本保护只撤销淘汰，不能顺带抹掉独立否词建议。
    assert item.negative_keywords == [{"keyword": "bad"}]
    assert len(warnings) == 1
    assert "上线仅 2 天" in warnings[0]
    assert "样本不足" in warnings[0]
    assert "强制修正为 keep" in warnings[0]


def test_normalize_keep_preserves_negative_keywords():
    """数值动作归一为 keep 时，否词仍是可执行的独立建议。"""
    item = _item(action="adjust_bid", current_budget=5.0, current_bid=0.50)
    item.negative_keywords = [{"keyword": "irrelevant query"}]

    changed = _normalize_action(item)

    assert changed is True
    assert item.action == "keep"
    assert item.negative_keywords == [{"keyword": "irrelevant query"}]


def test_new_campaign_llm_misjudge_eliminate_with_low_budget_force_eliminated():
    """新活动上线 1 天、预算=$1，LLM 判 eliminate → P3 硬淘汰优先。"""
    item = _item(days_online=1, current_budget=1.0, current_bid=0.50,
                 proposed_budget=1.0, proposed_bid=0.20,
                 action="eliminate_to_low_bid_pool")
    _resolve_budget_conflicts([item])
    assert item.action == "eliminate_to_low_bid_pool"
    assert item.proposed_budget == 1.0


# ── 旧活动 / 未知天数：强制淘汰照常 ──────────────────────────────

def test_old_campaign_low_budget_force_eliminated():
    """老活动 30 天、预算=$1，LLM 判 keep → 强制淘汰。"""
    item = _item(days_online=30, current_budget=1.0, current_bid=0.50, action="keep")
    warnings = _resolve_budget_conflicts([item])
    assert item.action == "eliminate_to_low_bid_pool"
    assert item.proposed_budget == 1.0
    assert item.proposed_bid == 0.20


def test_unknown_days_online_force_eliminated():
    """days_online=-1（未知）→ 不豁免，照常强制淘汰（安全侧）。"""
    item = _item(days_online=-1, current_budget=1.0, current_bid=0.50, action="keep")
    _resolve_budget_conflicts([item])
    assert item.action == "eliminate_to_low_bid_pool"


def test_old_campaign_normal_eliminate_unchanged():
    """老活动，LLM 正确判 eliminate → 正常硬填 $1/$0.20，不改 action。"""
    item = _item(days_online=60, current_budget=8.0, current_bid=0.80,
                 proposed_budget=None, proposed_bid=None,
                 action="eliminate_to_low_bid_pool")
    _resolve_budget_conflicts([item])
    assert item.action == "eliminate_to_low_bid_pool"
    assert item.proposed_budget == 1.0
    assert item.proposed_bid == 0.20


# ── 新活动正常调整不受影响 ──────────────────────────────────────

def test_new_campaign_normal_adjustment_unchanged():
    """新活动 2 天、预算=$10，LLM 判 adjust_budget → 正常通过，不改 action。"""
    item = _item(days_online=2, current_budget=10.0, current_bid=0.50,
                 proposed_budget=12.0, proposed_bid=0.50, action="adjust_budget")
    _resolve_budget_conflicts([item])
    assert item.action == "adjust_budget"
    assert item.proposed_budget == 12.0


def test_new_campaign_keep_unchanged():
    """新活动 2 天、预算=$8，LLM 判 keep → 正常通过。"""
    item = _item(days_online=2, current_budget=8.0, current_bid=0.50, action="keep")
    _resolve_budget_conflicts([item])
    assert item.action == "keep"


# ── 批量混合场景 ──────────────────────────────────────────────────

def test_mixed_batch_hard_eliminate_and_sample_protect():
    """批量：触底活动按 P3 淘汰，非触底样本不足误淘汰由 P1 修正。"""
    new1 = _item("new1", days_online=2, current_budget=1.0, current_bid=0.50, action="keep")
    old1 = _item("old1", days_online=30, current_budget=1.0, current_bid=0.50, action="keep")
    new2 = _item("new2", days_online=1, current_budget=5.0, current_bid=0.50,
                 proposed_budget=1.0, proposed_bid=0.20,
                 action="eliminate_to_low_bid_pool")

    warnings = _resolve_budget_conflicts([new1, old1, new2])

    assert new1.action == "eliminate_to_low_bid_pool"  # P3 硬淘汰
    assert old1.action == "eliminate_to_low_bid_pool"  # P3 硬淘汰
    assert new2.action == "keep"                      # 非触底，P1 修正
    assert len(warnings) == 3  # new1/old1 P3 强制淘汰 + new2 样本保护均需生成护栏告警
    assert any("new2" in w for w in warnings)
    assert any("new1" in w for w in warnings)
    assert any("old1" in w for w in warnings)


# ── 测试期保护（product_stage=测试期 + days_online < 14）──────────

def test_testing_stage_under_14_days_force_eliminated_when_budget_touch_floor():
    """测试期活动上线 10 天、预算=$1 → P3 硬淘汰优先。"""
    item = _item(days_online=10, current_budget=1.0, current_bid=0.50, action="keep")
    _resolve_budget_conflicts([item], product_stage="测试期")
    assert item.action == "eliminate_to_low_bid_pool"


def test_testing_stage_llm_misjudge_corrected():
    """测试期活动上线 10 天，LLM 误判 eliminate → 强制修正为 keep。"""
    item = _item(days_online=10, current_budget=5.0, current_bid=0.50,
                 proposed_budget=1.0, proposed_bid=0.20,
                 action="eliminate_to_low_bid_pool")
    item.direction = {"bid": "down"}
    warnings = _resolve_budget_conflicts([item], product_stage="测试期")

    assert item.action == "keep"
    assert item.proposed_budget == 5.0
    assert item.proposed_bid == 0.50
    assert item.direction == {}
    assert len(warnings) == 1
    assert "测试期且上线仅 10 天" in warnings[0]
    assert "受样本保护" in warnings[0]


def test_testing_stage_boundary_14_days_not_protected():
    """测试期活动上线恰好 14 天 → 不再受保护（≥14），触发强制淘汰。"""
    item = _item(days_online=14, current_budget=1.0, current_bid=0.50, action="keep")
    _resolve_budget_conflicts([item], product_stage="测试期")
    assert item.action == "eliminate_to_low_bid_pool"


def test_non_testing_stage_not_protected_by_stage_rule():
    """产品阶段不是测试期时仅靠 days_online=10 不触发测试期保护（走新活动 ≤3 那条也够不着）。"""
    item = _item(days_online=10, current_budget=1.0, current_bid=0.50, action="keep")
    _resolve_budget_conflicts([item], product_stage="推进期")
    assert item.action == "eliminate_to_low_bid_pool"


def test_testing_stage_unknown_days_not_protected():
    """测试期但 days_online=-1 → 未知天数不给保护（安全侧）。"""
    item = _item(days_online=-1, current_budget=1.0, current_bid=0.50, action="keep")
    _resolve_budget_conflicts([item], product_stage="测试期")
    assert item.action == "eliminate_to_low_bid_pool"


# ── 复评防抖（days_since_reactivation ≤ 3）────────────────────────

def test_reactivation_within_3_days_protected():
    """复评后 2 天，预算=$1 → 不强制淘汰（防淘汰↔复评抖动）。"""
    item = _item(days_online=30, current_budget=1.0, current_bid=0.50,
                 days_since_reactivation=2, action="keep")
    _resolve_budget_conflicts([item])
    assert item.action == "keep"


def test_reactivation_llm_misjudge_corrected():
    """复评后 1 天，LLM 误判 eliminate → 强制修正为 keep。"""
    item = _item(days_online=30, current_budget=8.0, current_bid=0.80,
                 proposed_budget=1.0, proposed_bid=0.20,
                 days_since_reactivation=1, action="eliminate_to_low_bid_pool")
    item.direction = {"bid": "down", "budget": "down"}
    warnings = _resolve_budget_conflicts([item])

    assert item.action == "keep"
    assert item.proposed_budget == 8.0
    assert item.proposed_bid == 0.80
    assert item.direction == {}
    assert len(warnings) == 1
    assert "复评后仅 1 天" in warnings[0]


def test_reactivation_boundary_3_days_protected():
    """复评后恰好 3 天 → 受保护（≤3）。"""
    item = _item(days_online=30, current_budget=1.0, current_bid=0.50,
                 days_since_reactivation=3, action="keep")
    _resolve_budget_conflicts([item])
    assert item.action == "keep"


def test_reactivation_boundary_4_days_not_protected():
    """复评后 4 天 → 不再受保护，触发强制淘汰。"""
    item = _item(days_online=30, current_budget=1.0, current_bid=0.50,
                 days_since_reactivation=4, action="keep")
    _resolve_budget_conflicts([item])
    assert item.action == "eliminate_to_low_bid_pool"


def test_never_reactivated_not_protected():
    """days_since_reactivation=-1（从未复评）→ 不触发复评防抖，但不影响新活动保护（days_online=2 也够）。"""
    item = _item(days_online=30, current_budget=1.0, current_bid=0.50,
                 days_since_reactivation=-1, action="keep")
    _resolve_budget_conflicts([item])
    assert item.action == "eliminate_to_low_bid_pool"


# ── 三条件同时覆盖的混合场景 ──────────────────────────────────────

@pytest.mark.parametrize("days_online,days_since_react,stage,expected_action", [
    (2, -1, "", "eliminate_to_low_bid_pool"),           # 新活动但预算触底 → P3
    (10, -1, "测试期", "eliminate_to_low_bid_pool"),     # 测试期但预算触底 → P3
    (30, 2, "", "keep"),           # 复评后 ≤3 天
    (4, -1, "", "eliminate_to_low_bid_pool"),  # 都不满足 → 强制淘汰
    (30, 4, "推进期", "eliminate_to_low_bid_pool"),  # 都不满足
])
def test_all_protection_conditions_orthogonal(
    days_online, days_since_react, stage, expected_action,
):
    """三条件正交：任一中=保护，都不中=淘汰。"""
    item = _item(days_online=days_online, current_budget=1.0, current_bid=0.50,
                 days_since_reactivation=days_since_react, action="keep")
    _resolve_budget_conflicts([item], product_stage=stage)
    assert item.action == expected_action
