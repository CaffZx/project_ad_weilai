"""Campaign card target/current and pending move semantics."""

from app.persistence.erp_writer.mappers import (
    _portfolio_groups_from_payload,
    canonicalize_payload,
)


def _payload(adj):
    return {
        "parent_asin": "B0TEST",
        "experiment_id": "exp-group-semantics",
        "run_number": 1,
        "timestamp": "2026-06-03T00:00:00+00:00",
        "summary": {},
        "adjustments": [adj],
    }


def test_card_keeps_normalized_current_and_target_code():
    run = canonicalize_payload(_payload({
        "campaign_name": "camp-main",
        "campaign_id": "111",
        "keyword_id": "222",
        "keyword_text": "kw",
        "match_type": "EXACT",
        "action": "keep",
        "current_portfolio": "US-精准主力组",
        "target_campaign_group_type": "exact_testing_group",
    }))

    card = run.cards[0]
    assert card.current_portfolio == "exact_core_group"
    assert card.campaign_group_type == "exact_testing_group"
    assert card.campaign_pending
    assert card.campaign_pending[0].target_campaign_group_type == "exact_testing_group"


def test_unknown_current_is_preserved_and_move_only_pending_is_created():
    run = canonicalize_payload(_payload({
        "campaign_name": "camp-raw",
        "campaign_id": "111",
        "keyword_id": "222",
        "keyword_text": "kw",
        "match_type": "BROAD",
        "action": "keep",
        "current_portfolio": "custom-portfolio-name",
        "target_campaign_group_type": "auto_broad_group",
    }))

    card = run.cards[0]
    assert card.current_portfolio == "custom-portfolio-name"
    assert card.campaign_group_type == "auto_broad_group"
    assert len(card.campaign_pending) == 1
    assert card.campaign_pending[0].target_campaign_group_type == "auto_broad_group"


def test_equal_current_and_target_does_not_create_move_pending():
    run = canonicalize_payload(_payload({
        "campaign_name": "camp-broad",
        "campaign_id": "111",
        "keyword_id": "222",
        "keyword_text": "kw",
        "match_type": "BROAD",
        "action": "keep",
        "current_portfolio": "auto_broad_group",
        "target_campaign_group_type": "auto_broad_group",
    }))

    card = run.cards[0]
    assert card.current_portfolio == "auto_broad_group"
    assert card.campaign_group_type == "auto_broad_group"
    assert card.campaign_pending == []


def test_summary_uses_target_for_unknown_current():
    summary = _portfolio_groups_from_payload([{
        "current_portfolio": "custom-portfolio-name",
        "target_campaign_group_type": "auto_broad_group",
        "proposed_budget": 7,
    }], None)

    assert summary["broad_auto_count"] == 1
    assert summary["broad_auto_budget"] == 7.0
