"""ERP mapper 使用数仓 campaign_id / keyword_id，不用 stable_id 伪造 Amazon ID。"""
from app.persistence.erp_writer.mappers import canonicalize_payload


def test_pending_uses_doris_ids_not_stable_hash():
    payload = {
        "parent_asin": "B0TEST",
        "experiment_id": "exp1",
        "run_number": 1,
        "timestamp": "2026-06-03T00:00:00+00:00",
        "summary": {},
        "adjustments": [
            {
                "campaign_name": "camp-a",
                "campaign_key": "camp-a × B0CHILD",
                "campaign_id": "111222333",
                "keyword_id": "999888777",
                "keyword_text": "fishnet tights",
                "match_type": "EXACT",
                "child_asin": "B0CHILD",
                "action": "adjust_bid",
                "ai_portfolio_class": "精准主力组",
                "current_bid": 1.0,
                "proposed_bid": 0.8,
                "confidence": 80,
            },
        ],
    }
    run = canonicalize_payload(payload, shop_id=1622)
    card = run.cards[0]
    assert card.campaign_id == "111222333"
    assert card.campaign_group_type == "exact_core_group"
    assert card.campaign_group_type == "exact_core_group"
    assert len(card.keyword_pending) == 1
    assert card.keyword_pending[0].keyword_id == "999888777"
    assert not card.keyword_pending[0].keyword_id.startswith("kwd")


def test_skip_card_when_campaign_id_missing():
    payload = {
        "parent_asin": "B0TEST",
        "experiment_id": "exp1",
        "run_number": 1,
        "timestamp": "2026-06-03T00:00:00+00:00",
        "summary": {},
        "adjustments": [
            {
                "campaign_name": "camp-a",
                "campaign_id": "",
                "keyword_id": "999",
                "keyword_text": "kw",
                "match_type": "EXACT",
                "action": "adjust_bid",
                "current_bid": 1.0,
                "proposed_bid": 0.8,
            },
        ],
    }
    run = canonicalize_payload(payload, shop_id=1622)
    assert len(run.cards) == 0


def test_shop_id_from_payload():
    payload = {
        "parent_asin": "B0TEST",
        "experiment_id": "exp1",
        "run_number": 1,
        "shop_id": 1622,
        "timestamp": "2026-06-03T00:00:00+00:00",
        "summary": {},
        "adjustments": [
            {
                "campaign_name": "c",
                "campaign_id": "100",
                "keyword_id": "200",
                "keyword_text": "kw",
                "match_type": "EXACT",
                "action": "keep",
            },
        ],
    }
    run = canonicalize_payload(payload)
    assert run.shop_id == 1622
