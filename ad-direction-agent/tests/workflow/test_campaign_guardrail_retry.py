"""Campaign guardrail retry orchestration helpers."""
from __future__ import annotations

from app.models.campaign import CampaignAdjustmentItem, CampaignUnit
from app.workflow.steps.campaign import (
    _GUARDRAIL_RETRY_INSTRUCTION,
    _backfill_placement_pcts,
    _build_guardrail_alerts,
    _guardrail_replacement_summary,
    _guardrail_rule_counts,
    _guardrail_snapshot,
)
from app.workflow.steps.campaign_guardrails import GuardrailPass, GuardrailResult


def test_build_guardrail_alerts_groups_messages_by_campaign_key():
    gp = GuardrailPass()
    gp.add(GuardrailResult(
        rule_id="P6_BUDGET_CAP",
        campaign_key="campaign-a x asin",
        corrected=True,
        message="[same name] budget capped",
    ))
    gp.add(GuardrailResult(
        rule_id="P9_BID_CAP",
        campaign_key="campaign-a x asin",
        corrected=True,
        message="[same name] bid capped",
    ))
    gp.add(GuardrailResult(
        rule_id="P10_PLACEMENT_BLOCK",
        campaign_key="campaign-b x asin",
        corrected=True,
        message="[same name] placement blocked",
    ))

    alerts = _build_guardrail_alerts(gp)

    assert set(alerts) == {"campaign-a x asin", "campaign-b x asin"}
    assert "[same name] budget capped" in alerts["campaign-a x asin"]
    assert "[same name] bid capped" in alerts["campaign-a x asin"]
    assert "P6_BUDGET_CAP" not in alerts["campaign-a x asin"]
    assert "P9_BID_CAP" not in alerts["campaign-a x asin"]
    assert "placement blocked" not in alerts["campaign-a x asin"]
    assert alerts["campaign-b x asin"] == "[same name] placement blocked"


def test_build_guardrail_alerts_prefers_retry_instruction():
    gp = GuardrailPass()
    gp.add(GuardrailResult(
        rule_id="P1_SAMPLE_INSUFFICIENT",
        campaign_key="campaign-a x asin",
        corrected=True,
        message="[campaign-a] 样本不足，已强制修正为 keep",
        retry_instruction="样本不足时不得直接淘汰；可结合事实评估小幅调整或维持。",
    ))

    alerts = _build_guardrail_alerts(gp)

    assert alerts["campaign-a x asin"] == "样本不足时不得直接淘汰；可结合事实评估小幅调整或维持。"
    assert "P1_SAMPLE_INSUFFICIENT" not in alerts["campaign-a x asin"]
    assert "已强制修正为 keep" not in alerts["campaign-a x asin"]


def test_guardrail_retry_instruction_is_internal_and_not_keep_only():
    assert "内部护栏反馈" in _GUARDRAIL_RETRY_INSTRUCTION
    assert "不要写入 reason/evidence" in _GUARDRAIL_RETRY_INSTRUCTION
    assert "不是要求一律 keep" in _GUARDRAIL_RETRY_INSTRUCTION
    assert "该淘汰就淘汰" in _GUARDRAIL_RETRY_INSTRUCTION
    assert "个活动触发护栏" not in _GUARDRAIL_RETRY_INSTRUCTION
    assert "failed_count" not in _GUARDRAIL_RETRY_INSTRUCTION
    assert "total_count" not in _GUARDRAIL_RETRY_INSTRUCTION
    assert "解释你如何遵守护栏" not in _GUARDRAIL_RETRY_INSTRUCTION


def test_guardrail_rule_counts_and_snapshot_are_diagnostic():
    gp = GuardrailPass()
    gp.add(GuardrailResult(
        rule_id="P6_BUDGET_CAP",
        campaign_key="campaign-a x asin",
        corrected=True,
        message="budget capped",
    ))
    gp.add(GuardrailResult(
        rule_id="P6_BUDGET_CAP",
        campaign_key="campaign-b x asin",
        corrected=True,
        message="budget capped",
    ))
    gp.add(GuardrailResult(
        rule_id="P9_BID_CAP",
        campaign_key="campaign-a x asin",
        corrected=True,
        message="bid capped",
    ))
    items = [
        CampaignAdjustmentItem(
            campaign_name="campaign-a",
            campaign_key="campaign-a x asin",
            action="adjust_budget",
            current_budget=10,
            proposed_budget=200,
            current_bid=1,
            proposed_bid=1,
        ),
        CampaignAdjustmentItem(
            campaign_name="campaign-c",
            campaign_key="campaign-c x asin",
            action="keep",
            current_budget=10,
            proposed_budget=10,
            current_bid=1,
            proposed_bid=1,
        ),
    ]

    assert _guardrail_rule_counts(gp) == {"P6_BUDGET_CAP": 2, "P9_BID_CAP": 1}
    assert _guardrail_snapshot(items, {"campaign-a x asin", "missing-key"}) == [
        {
            "key": "campaign-a x asin",
            "name": "campaign-a",
            "action": "adjust_budget",
            "bid": "1->1",
            "budget": "10->200",
        }
    ]


def test_guardrail_replacement_summary_shows_before_after():
    old_items = [
        CampaignAdjustmentItem(
            campaign_name="campaign-a",
            campaign_key="campaign-a x asin",
            action="adjust_budget",
            current_budget=10,
            proposed_budget=200,
            current_bid=1,
            proposed_bid=1,
        )
    ]
    replacements = {
        "campaign-a x asin": CampaignAdjustmentItem(
            campaign_name="campaign-a",
            campaign_key="campaign-a x asin",
            action="adjust_bid",
            current_budget=10,
            proposed_budget=10,
            current_bid=1,
            proposed_bid=0.8,
        )
    }

    assert _guardrail_replacement_summary(old_items, replacements) == [
        {
            "key": "campaign-a x asin",
            "name": "campaign-a",
            "action": "adjust_budget->adjust_bid",
            "bid": "1->1 to 1->0.8",
            "budget": "10->200 to 10->10",
        }
    ]


def test_backfill_placement_pcts_does_not_create_empty_placements():
    item = CampaignAdjustmentItem(
        campaign_name="campaign-a",
        campaign_key="campaign-a x asin",
        action="keep",
        placement_adjustments=[],
    )
    unit = CampaignUnit(
        campaign_name="campaign-a",
        campaign_key="campaign-a x asin",
        child_asin="asin",
        keyword_text="keyword",
        match_type="EXACT",
        tos_bid_pct=10,
        pp_bid_pct=20,
        ros_bid_pct=30,
    )

    _backfill_placement_pcts([item], {item.campaign_key: unit})

    assert item.placement_adjustments == []


def test_backfill_placement_pcts_fills_existing_placements():
    item = CampaignAdjustmentItem(
        campaign_name="campaign-a",
        campaign_key="campaign-a x asin",
        action="adjust_placement",
        placement_adjustments=[
            {"placement": "头部", "action": "小涨", "evidence": "r3"},
            {"placement": "商品", "action": "维持", "evidence": "r3"},
        ],
    )
    unit = CampaignUnit(
        campaign_name="campaign-a",
        campaign_key="campaign-a x asin",
        child_asin="asin",
        keyword_text="keyword",
        match_type="EXACT",
        tos_bid_pct=15,
        pp_bid_pct=20,
        ros_bid_pct=30,
    )

    _backfill_placement_pcts([item], {item.campaign_key: unit})

    assert item.placement_adjustments[0]["current_pct"] == 15
    assert item.placement_adjustments[0]["proposed_pct"] == 25
    assert item.placement_adjustments[1]["current_pct"] == 20
    assert item.placement_adjustments[1]["proposed_pct"] == 20
