"""paused_campaign（暂停活动）后端支持单测。

覆盖接线方案 §5：
  T1 归一化：paused_campaign → paused，二次归一化不被推导改回 keep/adjust
  T2 落库：paused 只生成活动级 new_state=paused pending，不带预算/Bid/广告位/否词
  T3 分类映射：_ACTION_TO_CATEGORY["paused"]="PAUSED"、_CATEGORY_PRIORITY 与淘汰同级
  T4 快照读回：PAUSED → _snapshot_action="paused"、_action_klass="eliminate_or_paused"
"""

from app.api.campaign_viewmodel import _action_klass, _snapshot_action
from app.models.campaign import CampaignAdjustmentItem
from app.persistence.erp_writer.mappers import (
    _ACTION_TO_CATEGORY,
    _CATEGORY_PRIORITY,
    _pending_lists_from_adjustment,
)
from app.workflow.steps.campaign import _normalize_action


def _adj(**kw) -> CampaignAdjustmentItem:
    base = dict(
        campaign_name="C1",
        campaign_key="k1",
        campaign_id="c1",
        child_asin="B0CHILD",
        keyword_text="fishnet",
        match_type="BROAD",
    )
    base.update(kw)
    return CampaignAdjustmentItem(**base)


# ── T1 归一化：翻译 + 二次归一化保留 ───────────────────────────────────

def test_normalize_translates_paused_campaign_to_paused():
    item = _adj(action="paused_campaign", proposed_budget=20.0, current_budget=20.0)
    changed = _normalize_action(item)
    assert item.action == "paused"
    # 翻译由归一化完成，不算"按差异推导的改写"
    assert changed is False


def test_normalize_second_pass_keeps_paused():
    """二次归一化（护栏结束后）不能把 paused 推导成 keep/adjust。"""
    item = _adj(action="paused", proposed_budget=20.0, current_budget=20.0)
    changed = _normalize_action(item)  # 模拟第二次调用
    assert item.action == "paused"
    assert changed is False


def test_normalize_full_chain_paused_survives():
    """首次归一化（paused_campaign→paused）+ 终态归一化 都保持 paused。"""
    item = _adj(action="paused_campaign", proposed_bid=0.5, current_bid=0.5,
                proposed_budget=20.0, current_budget=20.0)
    _normalize_action(item)          # 首次
    assert item.action == "paused"
    _normalize_action(item)          # 终态（护栏后）
    assert item.action == "paused", "二次归一化把 paused 推导成了非 paused"


# ── T2 落库：状态专用通道 ───────────────────────────────────────────────

def test_pending_from_paused_only_state():
    kw_pending, camp_pending, plc, neg_exact, neg_phrase = _pending_lists_from_adjustment(
        _adj(action="paused", current_budget=20.0, proposed_budget=20.0,
             current_bid=0.5, proposed_bid=0.5).model_dump(),
        parent_asin="B0PARENT",
        campaign_id="c1",
        keyword_text="fishnet",
        match_type="BROAD",
        kw_lookup={},
    )
    assert kw_pending == [], "暂停不得生成 keyword pending"
    assert plc == [] and neg_exact == [] and neg_phrase == []
    assert len(camp_pending) == 1
    assert camp_pending[0].new_state == "paused"
    assert camp_pending[0].new_budget is None, "暂停不得携带预算变更"
    assert camp_pending[0].target_campaign_group_type is None, "暂停不得触发组合迁移"


def test_pending_from_paused_with_llm_filled_numerics():
    """广泛流 prompt 要求 proposed 必填数值；即便 LLM 填了，暂停也只写状态。"""
    kw_pending, camp_pending, plc, neg_exact, neg_phrase = _pending_lists_from_adjustment(
        _adj(action="paused", current_budget=20.0, proposed_budget=25.0,
             current_bid=0.5, proposed_bid=0.6).model_dump(),
        parent_asin="B0PARENT",
        campaign_id="c1",
        keyword_text="fishnet",
        match_type="BROAD",
        kw_lookup={},
    )
    assert kw_pending == []
    assert len(camp_pending) == 1
    assert camp_pending[0].new_state == "paused"
    assert camp_pending[0].new_budget is None and camp_pending[0].old_budget is None


# ── T3 分类映射 ──────────────────────────────────────────────────────────

def test_action_category_has_paused():
    assert _ACTION_TO_CATEGORY["paused"] == "PAUSED"
    assert _CATEGORY_PRIORITY["PAUSED"] == _CATEGORY_PRIORITY["ELIMINATE"], "暂停与淘汰应同级优先"


# ── T4 快照读回 ──────────────────────────────────────────────────────────

def test_snapshot_action_paused():
    assert _snapshot_action({"suggest_category": "PAUSED"}) == "paused"
    assert _snapshot_action({"suggest_category": "ELIMINATE"}) == "eliminate_to_low_bid_pool"


def test_action_klass_paused():
    assert _action_klass("paused") == "eliminate_or_paused"
    assert _action_klass("eliminate_to_low_bid_pool") == "eliminate_or_paused"
