from __future__ import annotations

from datetime import datetime, timezone

from app.persistence.erp_writer.mappers import canonicalize_payload
from app.persistence.erp_writer.repository import ErpDualWriterRepository


class _Cursor:
    def __init__(self):
        self.sql: list[str] = []

    def execute(self, sql, params=None):
        self.sql.append(" ".join(str(sql).split()))

    def count(self, needle: str) -> int:
        return sum(1 for sql in self.sql if needle in sql)


def test_write_full_keeps_create_and_gray_cards_without_campaign_id():
    payload = {
        "parent_asin": "B0PARENT",
        "experiment_id": "exp-gray",
        "run_number": 1,
        "timestamp": "2026-06-27T00:00:00+00:00",
        "summary": {},
        "adjustments": [],
        "new_campaigns": [{
            "campaign_name": "exact-test-kw",
            "keyword_text": "test kw",
            "match_type": "EXACT",
            "child_asin": "B0CHILD",
            "proposed_base_bid": 0.8,
            "proposed_daily_budget": 5,
            "trigger_scene": "RANKING_OPPORTUNITY_NO_EXACT",
            "confidence": "medium",
            "ai_portfolio_class": "精准测试组",
        }],
        "skipped_campaigns": [{
            "campaign_name": "multi-camp",
            "campaign_key": "multi-camp x B0CHILD",
            "child_asin": "B0CHILD",
            "keyword_text": "multi keyword",
            "match_type": "EXACT",
            "__prefiltered": True,
            "reason": "multi_keyword_deferred",
        }],
    }
    run = canonicalize_payload(payload, shop_id=1)
    assert [card.suggest_category for card in run.cards] == ["CREATE", None]
    assert all(not card.campaign_id for card in run.cards)

    cur = _Cursor()
    repo = ErpDualWriterRepository.__new__(ErpDualWriterRepository)
    repo._operator = None
    counts = repo._upsert_modern_cards_and_pending(
        cur, run, datetime.now(timezone.utc),
    )

    assert counts == (2, 1, 1, 0)
    assert cur.count("INSERT INTO t_advert_agent_modify_suggest_card") == 2
    assert cur.count("INSERT INTO t_advert_agent_modify_keyword_pending") == 1
    assert cur.count("INSERT INTO t_advert_agent_modify_campaign_pending") == 1
