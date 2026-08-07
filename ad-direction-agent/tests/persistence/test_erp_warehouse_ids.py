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
                "current_portfolio": "精准主力组",
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


def test_target_group_creates_campaign_pending_without_budget_change():
    """纯挪组是活动级操作，必须落在 campaign_pending 而非只留在卡片。"""
    payload = {
        "parent_asin": "B0TEST",
        "experiment_id": "exp-target-group",
        "run_number": 1,
        "timestamp": "2026-06-03T00:00:00+00:00",
        "summary": {},
        "adjustments": [{
            "campaign_name": "exact-testing-campaign",
            "campaign_id": "111222333",
            "keyword_id": "999888777",
            "keyword_text": "fishnet tights",
            "match_type": "EXACT",
            "action": "keep",
            # 旧 LLM 输出不能决定下游挪组；代码写入的 target 才是权威。
            "current_portfolio": "低价捡漏组",
            "target_campaign_group_type": "exact_testing_group",
        }],
    }

    run = canonicalize_payload(payload, shop_id=1622)

    assert len(run.cards) == 1
    card = run.cards[0]
    assert card.campaign_group_type == "exact_testing_group"
    assert len(card.campaign_pending) == 1
    assert card.campaign_pending[0].old_budget is None
    assert card.campaign_pending[0].new_budget is None
    assert card.campaign_pending[0].target_campaign_group_type == "exact_testing_group"


def test_legacy_current_portfolio_does_not_create_move_pending():
    payload = {
        "parent_asin": "B0TEST",
        "experiment_id": "exp-no-target-group",
        "run_number": 1,
        "timestamp": "2026-06-03T00:00:00+00:00",
        "summary": {},
        "adjustments": [{
            "campaign_name": "exact-campaign",
            "campaign_id": "111222333",
            "keyword_id": "999888777",
            "keyword_text": "fishnet tights",
            "match_type": "EXACT",
            "action": "keep",
            "current_portfolio": "低价捡漏组",
        }],
    }

    run = canonicalize_payload(payload, shop_id=1622)

    assert run.cards[0].campaign_pending == []


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
