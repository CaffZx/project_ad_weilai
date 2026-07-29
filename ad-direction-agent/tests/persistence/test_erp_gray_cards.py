from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.api import decision as decision_api
from app.persistence.erp_writer.mappers import canonicalize_payload
from app.persistence.erp_writer.repository import (
    ErpDualWriterRepository,
    WriteReport,
)


class _Cursor:
    def __init__(self):
        self.sql: list[str] = []

    def execute(self, sql, params=None):
        self.sql.append(" ".join(str(sql).split()))

    def count(self, needle: str) -> int:
        return sum(1 for sql in self.sql if needle in sql)


class _Connection:
    def __init__(self):
        self.cursor_value = MagicMock()
        self.cursor_value.__enter__.return_value = self.cursor_value
        self.cursor_value.__exit__.return_value = False
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self.cursor_value

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


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


def test_write_immediate_exit_only_writes_decision_summary_cards_and_pending():
    """确定性链路不写 wizard、legacy metrics、direction 或 synthesis 表。"""
    run = MagicMock()
    run.decision_id = "dec-1"
    run.decision_meta = {"operating_mode": "立即退出"}
    conn = _Connection()
    repo = ErpDualWriterRepository.__new__(ErpDualWriterRepository)
    repo._operator = "other-request"
    repo._audit_int_val = 999
    repo._connect = MagicMock(return_value=conn)
    repo._upsert_decision = MagicMock()
    repo._upsert_decision_config = MagicMock()
    repo._upsert_modern_summary = MagicMock()
    repo._delete_modern_campaign_children = MagicMock()
    repo._upsert_modern_cards_and_pending = MagicMock(
        return_value=(4, 1, 4, 0)
    )
    repo._upsert_legacy_metrics = MagicMock()
    repo._upsert_legacy_recommend = MagicMock()
    repo._upsert_legacy_details = MagicMock()
    repo._upsert_synthesis = MagicMock()

    report = repo.write_immediate_exit(run, operator="42")

    assert isinstance(report, WriteReport)
    assert report.as_dict() == {
        "decision_id": "dec-1",
        "decision": 1,
        "decision_config": 1,
        "modern_summary": 1,
        "modern_card": 4,
        "modern_keyword_pending": 1,
        "modern_campaign_pending": 4,
        "modern_placement_pending": 0,
        "legacy_recommend": 0,
        "legacy_detail": 0,
        "legacy_metrics": 0,
        "purpose_score": 0,
        "core_keyword_tracking": 0,
        "ai_suggest": 0,
        "direction_recommend": 0,
        "direction_detail": 0,
    }
    repo._upsert_decision.assert_called_once()
    repo._upsert_decision_config.assert_called_once()
    repo._upsert_modern_summary.assert_called_once()
    repo._delete_modern_campaign_children.assert_called_once_with(
        conn.cursor_value, "dec-1",
    )
    repo._upsert_legacy_metrics.assert_not_called()
    repo._upsert_legacy_recommend.assert_not_called()
    repo._upsert_legacy_details.assert_not_called()
    repo._upsert_synthesis.assert_not_called()
    assert repo._upsert_decision.call_args.kwargs["operator"] == "42"
    assert repo._upsert_modern_summary.call_args.kwargs["operator"] == "42"
    assert (
        repo._upsert_modern_cards_and_pending.call_args.kwargs["operator"]
        == "42"
    )
    assert repo._operator == "other-request"
    assert repo._audit_int_val == 999
    assert conn.committed is True
    assert conn.rolled_back is False
    assert conn.closed is True


def test_immediate_exit_lock_uses_mysql_advisory_lock_and_releases():
    """锁必须绑定同一 MySQL 连接，并在退出时显式 RELEASE_LOCK。"""
    conn = _Connection()
    conn.cursor_value.fetchone.side_effect = [
        {"acquired": 1},
        {"released": 1},
    ]
    repo = ErpDualWriterRepository.__new__(ErpDualWriterRepository)
    repo._connect = MagicMock(return_value=conn)

    with repo.immediate_exit_lock("dec-1", timeout_seconds=7):
        assert conn.closed is False

    executed = conn.cursor_value.execute.call_args_list
    assert "GET_LOCK" in executed[0].args[0]
    assert executed[0].args[1] == ("ad-agent:immediate-exit:dec-1", 7)
    assert "RELEASE_LOCK" in executed[1].args[0]
    assert executed[1].args[1] == ("ad-agent:immediate-exit:dec-1",)
    assert conn.closed is True


def test_immediate_exit_lock_rejects_timeout():
    conn = _Connection()
    conn.cursor_value.fetchone.return_value = {"acquired": 0}
    repo = ErpDualWriterRepository.__new__(ErpDualWriterRepository)
    repo._connect = MagicMock(return_value=conn)

    with pytest.raises(TimeoutError, match="dec-1"):
        with repo.immediate_exit_lock("dec-1", timeout_seconds=1):
            raise AssertionError("lock body must not run")

    assert conn.closed is True


def test_claim_pending_for_execution_only_claims_confirmed_pending_rows():
    conn = _Connection()
    conn.cursor_value.rowcount = 1
    repo = ErpDualWriterRepository.__new__(ErpDualWriterRepository)
    repo._connect = MagicMock(return_value=conn)
    ops = [
        {"record_kind": "campaign", "pending_id": "cp-1"},
        {"record_kind": "keyword", "pending_id": "kp-1"},
    ]

    claimed = repo.claim_pending_for_execution(ops, operator="42")

    assert claimed == 2
    assert conn.committed is True
    executed = conn.cursor_value.execute.call_args_list
    assert len(executed) == 2
    assert all("confirm_status='CONFIRMED'" in call.args[0] for call in executed)
    assert all("execute_status='PENDING'" in call.args[0] for call in executed)
    assert all("SET execute_status='IN_PROGRESS'" in call.args[0] for call in executed)


def test_claim_pending_for_execution_rolls_back_partial_claim():
    conn = _Connection()
    rowcounts = iter((1, 0))

    def _execute(*_args, **_kwargs):
        conn.cursor_value.rowcount = next(rowcounts)

    conn.cursor_value.execute.side_effect = _execute
    repo = ErpDualWriterRepository.__new__(ErpDualWriterRepository)
    repo._connect = MagicMock(return_value=conn)

    claimed = repo.claim_pending_for_execution(
        [
            {"record_kind": "campaign", "pending_id": "cp-1"},
            {"record_kind": "keyword", "pending_id": "kp-1"},
        ],
        operator="42",
    )

    assert claimed == 0
    assert conn.rolled_back is True
    assert conn.committed is False
    assert conn.closed is True


def test_list_execution_task_ids_is_read_only_and_keeps_first_seen_order():
    conn = _Connection()
    conn.cursor_value.fetchall.return_value = [
        {"task_id": "task-2"},
        {"task_id": "task-1"},
        {"task_id": "task-2"},
        {"task_id": ""},
        {"task_id": None},
    ]
    repo = ErpDualWriterRepository.__new__(ErpDualWriterRepository)
    repo._connect = MagicMock(return_value=conn)

    result = repo.list_execution_task_ids("dec-1")

    assert result == ["task-2", "task-1"]
    executed = conn.cursor_value.execute.call_args_list
    assert len(executed) == 1
    sql = " ".join(executed[0].args[0].split())
    assert sql.startswith("SELECT task_id")
    assert "FROM t_advert_agent_modify_advert_record" in sql
    assert "WHERE decision_id=%s" in sql
    assert executed[0].args[1] == ("dec-1",)
    assert "INSERT" not in sql.upper()
    assert "UPDATE" not in sql.upper()
    assert conn.committed is False
    assert conn.closed is True


def test_update_campaign_terminal_status_updates_pending_and_aggregates_cards():
    conn = _Connection()
    conn.cursor_value.fetchone.return_value = {
        "is_latest": 1,
        "operating_mode": "IMMEDIATE_EXIT",
    }
    conn.cursor_value.fetchall.side_effect = [
        [
            {"suggest_card_id": "card-success", "execute_status": "SUCCESS"},
            {"suggest_card_id": "card-fail", "execute_status": "FAIL"},
            {"suggest_card_id": "card-wait", "execute_status": "SUCCESS"},
            {"suggest_card_id": "card-only-campaign", "execute_status": "SUCCESS"},
        ],
        [
            {"suggest_card_id": "card-success", "execute_status": "SUCCESS"},
            {"suggest_card_id": "card-wait", "execute_status": "IN_PROGRESS"},
        ],
        [],
    ]
    repo = ErpDualWriterRepository.__new__(ErpDualWriterRepository)
    repo._connect = MagicMock(return_value=conn)

    updated = repo.update_campaign_terminal_status(
        "dec-1",
        {
            "camp-success": "SUCCESS",
            "camp-fail": "FAIL",
            "camp-wait": "IN_PROGRESS",
            "camp-invalid": "UNKNOWN",
        },
        operator="42",
        message_by_campaign={"camp-fail": "Amazon 修改失败"},
    )

    assert updated is True
    calls = conn.cursor_value.execute.call_args_list
    assert "FOR UPDATE" in calls[0].args[0]
    pending_updates = [
        call for call in calls
        if call.args[0].lstrip().upper().startswith("UPDATE T_ADVERT_AGENT_MODIFY_")
        and "_PENDING" in call.args[0].upper()
    ]
    assert len(pending_updates) == 9
    assert all("decision_id=%s AND campaign_id=%s" in call.args[0]
               for call in pending_updates)
    assert not any("camp-invalid" in call.args[1] for call in pending_updates)
    assert any(
        call.args[1][0] == "FAIL"
        and call.args[1][1] == "Amazon 修改失败"
        and call.args[1][-2:] == ("dec-1", "camp-fail")
        for call in pending_updates
    )

    card_updates = [
        call for call in calls
        if "UPDATE t_advert_agent_modify_suggest_card" in call.args[0]
    ]
    card_statuses = {
        call.args[1][-2]: call.args[1][0]
        for call in card_updates
    }
    assert card_statuses == {
        "card-success": "SUCCESS",
        "card-fail": "FAIL",
        "card-wait": "IN_PROGRESS",
        "card-only-campaign": "SUCCESS",
    }
    assert conn.committed is True
    assert conn.rolled_back is False
    assert conn.closed is True


def test_update_campaign_terminal_status_rejects_stale_decision_in_transaction():
    conn = _Connection()
    conn.cursor_value.fetchone.return_value = {
        "is_latest": 0,
        "operating_mode": "IMMEDIATE_EXIT",
    }
    repo = ErpDualWriterRepository.__new__(ErpDualWriterRepository)
    repo._connect = MagicMock(return_value=conn)

    updated = repo.update_campaign_terminal_status(
        "dec-old",
        {"camp-1": "SUCCESS"},
        operator="42",
    )

    assert updated is False
    calls = conn.cursor_value.execute.call_args_list
    assert len(calls) == 1
    assert "FOR UPDATE" in calls[0].args[0]
    assert conn.rolled_back is True
    assert conn.committed is False


def test_upsert_pool_entry_keeps_entry_time_for_same_active_decision():
    conn = _Connection()
    repo = ErpDualWriterRepository.__new__(ErpDualWriterRepository)
    repo._connect = MagicMock(return_value=conn)

    conn.cursor_value.fetchone.return_value = {
        "is_latest": 1,
        "operating_mode": "IMMEDIATE_EXIT",
    }
    inserted = repo.upsert_pool_entry(
        "B0PARENT",
        campaign_id="camp-1",
        decision_id="dec-1",
        require_latest_decision=True,
    )

    assert inserted is True
    calls = conn.cursor_value.execute.call_args_list
    assert "FOR UPDATE" in calls[0].args[0]
    sql = calls[1].args[0]
    normalized = " ".join(sql.split())
    assert (
        "entry_date = IF( decision_id <=> VALUES(decision_id), "
        "entry_date, VALUES(entry_date) )"
    ) in normalized
    assert (
        "update_time = IF( decision_id <=> VALUES(decision_id), "
        "update_time, VALUES(update_time) )"
    ) in normalized
    assert (
        "exit_date = IF( decision_id <=> VALUES(decision_id), "
        "exit_date, NULL )"
    ) in normalized
    assert conn.committed is True


def test_upsert_pool_entry_rejects_stale_decision_under_lock():
    conn = _Connection()
    conn.cursor_value.fetchone.return_value = {
        "is_latest": 0,
        "operating_mode": "IMMEDIATE_EXIT",
    }
    repo = ErpDualWriterRepository.__new__(ErpDualWriterRepository)
    repo._connect = MagicMock(return_value=conn)

    inserted = repo.upsert_pool_entry(
        "B0PARENT",
        campaign_id="camp-1",
        decision_id="dec-old",
        require_latest_decision=True,
    )

    assert inserted is False
    assert len(conn.cursor_value.execute.call_args_list) == 1
    assert "FOR UPDATE" in conn.cursor_value.execute.call_args.args[0]
    assert conn.rolled_back is True
    assert conn.committed is False
