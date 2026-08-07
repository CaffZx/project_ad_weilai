"""否词从 canonical payload 到 ERP 写入参数的离线回归测试。"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from app.persistence.erp_writer.mappers import canonicalize_payload
from app.persistence.erp_writer.repository import ErpDualWriterRepository


class _Cursor:
    def __init__(self):
        self.calls: list[tuple[str, tuple | None]] = []

    def execute(self, sql, params=None):
        self.calls.append((" ".join(str(sql).split()), params))

    def call_for(self, table_name: str) -> tuple[str, tuple]:
        return next((sql, params) for sql, params in self.calls if table_name in sql)

    def calls_for(self, table_name: str) -> list[tuple[str, tuple]]:
        return [(sql, params) for sql, params in self.calls if table_name in sql]


def _payload(adjustments: list[dict]) -> dict:
    return {
        "parent_asin": "B0PARENT",
        "parent_seller_sku": "PARENT-SKU",
        "experiment_id": "20260722T000000Z",
        "run_number": 1,
        "timestamp": "2026-07-22T00:00:00+00:00",
        "summary": {},
        "adjustments": adjustments,
    }


def _adjustment(*, keyword_id: str, keyword_text: str, match_type: str, negatives: list[dict]) -> dict:
    return {
        "campaign_name": "campaign-a",
        "campaign_id": "10001",
        "keyword_id": keyword_id,
        "keyword_text": keyword_text,
        "match_type": match_type,
        "child_asin": "B0CHILD",
        "action": "adjust",
        "current_portfolio": "精准主力组",
        "current_bid": 1.0,
        "proposed_bid": 0.8,
        "negative_keywords": negatives,
    }


def test_canonicalize_negative_exact_writes_card_snapshot_and_pending_fields():
    run = canonicalize_payload(
        _payload([_adjustment(
            keyword_id="kw-exact",
            keyword_text="dress",
            match_type="EXACT",
            negatives=[{
                "keyword": "wedding dress",
                "match_type": "NEGATIVE_EXACT",
                "reason": "7天点击12次、零订单、无转化",
            }],
        )]),
        shop_id=1622,
    )

    card = run.cards[0]
    negatives = [kw for kw in card.keyword_pending if kw.new_state == "NEGATIVE"]

    assert card.proposed_negetive_exact_keyword == ["wedding dress"]
    assert card.proposed_negetive_phrase_keyword == []
    assert len(negatives) == 1
    assert negatives[0].keyword_id == ""
    assert negatives[0].keyword_text == "wedding dress"
    assert negatives[0].match_type == "NEGATIVE_EXACT"
    assert negatives[0].new_state == "NEGATIVE"
    assert negatives[0].neg_evidence == "7天点击12次、零订单、无转化"


def test_canonicalize_keeps_existing_keywords_by_id_and_deduplicates_same_negative():
    negative = {
        "keyword": "wedding dress",
        "match_type": "NEGATIVE_EXACT",
        "reason": "无转化",
    }
    run = canonicalize_payload(
        _payload([
            _adjustment(
                keyword_id="kw-exact", keyword_text="wedding dress", match_type="EXACT",
                negatives=[negative],
            ),
            _adjustment(
                keyword_id="kw-broad", keyword_text="wedding dress", match_type="BROAD",
                negatives=[negative],
            ),
        ]),
        shop_id=1622,
    )

    pending = run.cards[0].keyword_pending
    existing = [kw for kw in pending if kw.new_state != "NEGATIVE"]
    negatives = [kw for kw in pending if kw.new_state == "NEGATIVE"]

    assert {(kw.keyword_id, kw.match_type) for kw in existing} == {
        ("kw-exact", "EXACT"),
        ("kw-broad", "BROAD"),
    }
    assert [(kw.keyword_text, kw.match_type) for kw in negatives] == [
        ("wedding dress", "NEGATIVE_EXACT"),
    ]
    assert run.cards[0].proposed_negetive_exact_keyword == ["wedding dress"]


def test_repository_persists_negative_fields_and_null_keyword_id():
    run = canonicalize_payload(
        _payload([_adjustment(
            keyword_id="kw-exact",
            keyword_text="dress",
            match_type="EXACT",
            negatives=[{
                "keyword": "wedding dress",
                "match_type": "NEGATIVE_EXACT",
                "reason": "7天点击12次、零订单、无转化",
            }],
        )]),
        shop_id=1622,
    )
    cursor = _Cursor()
    repo = ErpDualWriterRepository.__new__(ErpDualWriterRepository)
    repo._operator = None

    repo._upsert_modern_cards_and_pending(cursor, run, datetime.now(timezone.utc))

    card_sql, card_params = cursor.call_for("t_advert_agent_modify_suggest_card")
    keyword_sql, keyword_params = next(
        (sql, params)
        for sql, params in cursor.calls_for("t_advert_agent_modify_keyword_pending")
        if params[15] == "NEGATIVE"
    )

    assert "proposed_negetive_exact_keyword" in card_sql
    assert "proposed_negetive_phrase_keyword" in card_sql
    assert json.loads(card_params[24]) == ["wedding dress"]
    assert json.loads(card_params[25]) == []
    assert "neg_evidence" in keyword_sql
    assert keyword_params[10] is None
    assert keyword_params[11:16] == (
        "wedding dress",
        "7天点击12次、零订单、无转化",
        "NEGATIVE_EXACT",
        None,
        "NEGATIVE",
    )
