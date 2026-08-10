"""paused_campaign（暂停活动）后端支持单测。

覆盖接线方案 §5：
  T1 归一化：paused_campaign → paused，二次归一化不被推导改回 keep/adjust
  T2 落库：paused 只生成活动级 new_state=paused pending，不带预算/Bid/广告位/否词
  T3 分类映射：_ACTION_TO_CATEGORY["paused"]="PAUSED"、_CATEGORY_PRIORITY 与淘汰同级
  T4 快照读回：PAUSED → _snapshot_action="paused"、_action_klass="eliminate_or_paused"
"""

from app.api.campaign_viewmodel import _action_klass, _snapshot_action
from app.models.campaign import CampaignAdjustmentItem, CampaignUnit
from app.persistence.erp_writer.mappers import (
    _ACTION_TO_CATEGORY,
    _CATEGORY_PRIORITY,
    _pending_lists_from_adjustment,
    canonicalize_payload,
)
from app.workflow.steps.campaign import _normalize_action
from app.workflow.steps import campaign as campaign_step


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


def test_low_bid_non_exact_builder_creates_fixed_paused_adjustment():
    unit = CampaignUnit(
        campaign_name="广泛活动",
        campaign_key="broad-low-bid",
        campaign_id="campaign-1",
        keyword_id="keyword-1",
        child_asin="B0CHILD",
        seller_sku="SKU-1",
        keyword_text="broad kw",
        match_type="BROAD",
        current_bid=0.2,
        current_budget=1.0,
        current_portfolio_name="US-低价捡漏组",
    )

    items = campaign_step._build_low_bid_pool_pause_adjustments([unit])

    assert len(items) == 1
    item = items[0]
    expected = "检测到该广泛/词组/自动广告活动已入淘汰池但未被暂停，决定将本活动暂停。同意则直接执行，不同意请人工到后台修改！"
    assert item.action == "paused"
    assert item.reason == expected
    assert item.evidence == [expected]
    assert item.campaign_id == "campaign-1"
    assert item.keyword_id == "keyword-1"
    assert item.review_level == "HIGH_RISK_REVIEW"
    assert item.current_portfolio == "auto_broad_group"
    assert item.target_campaign_group_type == "auto_broad_group"


def test_low_bid_pause_builder_reaches_campaign_card_and_pending():
    unit = CampaignUnit(
        campaign_name="广泛活动",
        campaign_key="broad-low-bid",
        campaign_id="campaign-1",
        keyword_id="keyword-1",
        child_asin="B0CHILD",
        seller_sku="SKU-1",
        keyword_text="broad kw",
        match_type="BROAD",
        current_bid=0.2,
        current_budget=1.0,
        current_portfolio_name="US-低价捡漏组",
    )
    item = campaign_step._build_low_bid_pool_pause_adjustments([unit])[0]

    run = canonicalize_payload({
        "experiment_id": "run-1",
        "parent_asin": "B0PARENT",
        "shop_id": 1,
        "site_code": "Amazon_US",
        "adjustments": [item.model_dump()],
    })

    assert len(run.cards) == 1
    assert run.cards[0].campaign_pending[0].new_state == "paused"
    assert run.cards[0].campaign_pending[0].new_budget is None
    assert run.cards[0].keyword_pending == []
