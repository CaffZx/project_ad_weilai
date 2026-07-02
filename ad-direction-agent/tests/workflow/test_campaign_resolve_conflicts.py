"""_resolve_budget_conflicts 新活动保护（KB 21 §2）单测。

验证上线 ≤3 天的新活动不被强制淘汰，LLM 误判淘汰时强制修正为 keep。
"""

import os
os.environ["LLM_GLOBAL_CONCURRENCY"] = "420"

from app.models.campaign import CampaignAdjustmentItem
from app.workflow.steps.campaign import _resolve_budget_conflicts


def _item(name="c1", action="keep", current_budget=1.0, current_bid=0.20,
          proposed_budget=None, proposed_bid=None, days_online=-1):
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
    )


# ── 新活动保护：强制淘汰豁免 ──────────────────────────────────────

def test_new_campaign_low_budget_not_force_eliminated():
    """新活动上线 2 天、预算=$1，LLM 判 keep → 不强制淘汰（KB 21 §2 保护）。"""
    item = _item(days_online=2, current_budget=1.0, current_bid=0.50, action="keep")
    warnings = _resolve_budget_conflicts([item])
    assert item.action == "keep"  # 没被翻成 eliminate，核心断言


def test_new_campaign_low_bid_not_force_eliminated():
    """新活动上线 2 天、bid=$0.05，LLM 判 adjust_bid → 不强制淘汰。"""
    item = _item(days_online=2, current_budget=5.0, current_bid=0.05,
                 proposed_bid=0.10, action="adjust_bid")
    warnings = _resolve_budget_conflicts([item])
    assert item.action == "adjust_bid"
    assert item.proposed_bid == 0.10  # LLM 原建议保留


def test_new_campaign_boundary_3_days_protected():
    """上线恰好 3 天 → 仍受保护（≤3）。"""
    item = _item(days_online=3, current_budget=1.0, current_bid=0.50, action="keep")
    _resolve_budget_conflicts([item])
    assert item.action == "keep"


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
    assert item.negative_keywords == []
    assert len(warnings) == 1
    assert "新活动上线仅 2 天" in warnings[0]
    assert "强制修正为 keep" in warnings[0]


def test_new_campaign_llm_misjudge_eliminate_with_low_budget():
    """新活动上线 1 天、预算=$1，LLM 也误判 eliminate → 修正为 keep（不应淘汰）。"""
    item = _item(days_online=1, current_budget=1.0, current_bid=0.50,
                 proposed_budget=1.0, proposed_bid=0.20,
                 action="eliminate_to_low_bid_pool")
    _resolve_budget_conflicts([item])
    assert item.action == "keep"
    assert item.proposed_budget == 1.0   # 回退 current（就是 $1，但 action 已改 keep）


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

def test_mixed_batch_new_and_old():
    """批量：新活动被保护，旧活动照常淘汰。"""
    new1 = _item("new1", days_online=2, current_budget=1.0, current_bid=0.50, action="keep")
    old1 = _item("old1", days_online=30, current_budget=1.0, current_bid=0.50, action="keep")
    new2 = _item("new2", days_online=1, current_budget=5.0, current_bid=0.50,
                 proposed_budget=1.0, proposed_bid=0.20,
                 action="eliminate_to_low_bid_pool")

    warnings = _resolve_budget_conflicts([new1, old1, new2])

    assert new1.action == "keep"                    # 保护豁免
    assert old1.action == "eliminate_to_low_bid_pool"  # 照常淘汰
    assert new2.action == "keep"                    # LLM 误判修正
    assert len(warnings) == 1  # 仅 new2 产生 warning
    assert "new2" in warnings[0]
