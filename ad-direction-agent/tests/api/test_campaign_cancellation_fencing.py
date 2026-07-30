"""Campaign 取消事件的 run_id 围栏回归。"""
from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.api import campaign as campaign_api
from app.api import decision as decision_api
from app.models.campaign import CampaignAnalysisResult
from app.workflow.analysis_run_guard import AnalysisRunCancelled
from app.workflow.steps.campaign_new import analyze_new_campaigns


class _RunState:
    def __init__(self):
        self.execution_clears: list[tuple[str, str]] = []

    def get_analysis_session(self, _asin):
        return {"run_id": "RUN-A"}

    def mark_analysis_execution_started(self, *_args, **_kwargs):
        return True

    def clear_analysis_execution_started_if_run(self, asin, run_id):
        self.execution_clears.append((asin, run_id))
        return True


@pytest.mark.asyncio
async def test_campaign_viewmodel_reads_decision_id_after_successful_erp(monkeypatch):
    """ERP 成功后必须读取 decision_id 并回读快照，而不是引用未赋值局部变量。"""
    state = _RunState()
    result = CampaignAnalysisResult(parent_asin="B0TEST", days=7)
    extra = {
        "state": state,
        "asin": "B0TEST",
        "days": 7,
        "temperature": 0.3,
        "analysis_mode": "REALTIME",
        "run_id": "RUN-A",
    }

    async def _do_analyze(_req):
        return result, extra

    async def _push(*_args, **_kwargs):
        return {"ok": True, "decision_id": "DEC-1"}

    monkeypatch.setattr(campaign_api, "get_state_manager", lambda: state)
    monkeypatch.setattr(campaign_api, "_do_analyze", _do_analyze)
    monkeypatch.setattr(campaign_api, "_maybe_push_erp", _push)
    monkeypatch.setattr(
        campaign_api,
        "_get_repository",
        lambda: SimpleNamespace(read_snapshot=lambda _decision_id: {"id": "DEC-1"}),
    )
    monkeypatch.setattr(
        campaign_api,
        "from_db_snapshot",
        lambda snapshot, *, mode: {"decision_id": snapshot["id"], "mode": mode},
    )

    vm = await campaign_api.campaign_viewmodel({"asin": "B0TEST", "run_id": "RUN-A"})

    assert vm["decision_id"] == "DEC-1"
    assert vm["mode"] == "interactive"


def test_batch_without_analysis_event_does_not_create_cancel_checker():
    """cfg_source=config 批跑没有 analysis_session，不能被取消机制拦截。"""
    assert campaign_api._build_analysis_cancel_check(None, "B0TEST", None) is None


@pytest.mark.asyncio
async def test_failure_cleanup_only_clears_its_own_run_execution_marker():
    state = _RunState()

    await campaign_api._clear_execution_started(state, "B0TEST", "RUN-A")

    assert state.execution_clears == [("B0TEST", "RUN-A")]


def test_immediate_exit_clears_identity_run_not_current_session():
    """立即退出 A 完成时不得读取并清除后来 B 的当前 session。"""
    events: list[tuple[str, str]] = []

    class _State:
        def get_analysis_session(self, _asin):
            raise AssertionError("immediate exit must not clear the current session")

        def clear_analysis_session_if_run(self, asin, run_id):
            events.append((asin, run_id))
            return True

    @contextmanager
    def _lock(_decision_id):
        yield

    repo = SimpleNamespace(
        immediate_exit_lock=_lock,
        get_decision_basic=lambda _decision_id: None,
        write_immediate_exit=lambda _run, *, operator: SimpleNamespace(as_dict=lambda: {}),
        finalize_batch=lambda *_args, **_kwargs: None,
        confirm_decisions=lambda *_args, **_kwargs: {"ok": True, "applied": 1},
    )
    run = SimpleNamespace(
        decision_id="DEC-1",
        parent_asin="B0TEST",
        cards=[SimpleNamespace(card_id="CARD-1")],
    )

    decision_api._persist_and_confirm_immediate_exit_locked(
        run=run,
        identity={"operator": "42", "run_id": "RUN-A"},
        repo=repo,
        state=_State(),
    )

    assert events == [("B0TEST", "RUN-A")]


@pytest.mark.asyncio
async def test_new_campaign_flow_propagates_cancellation_before_any_fetch():
    async def _cancel():
        raise AnalysisRunCancelled("B0TEST", "RUN-A")

    with pytest.raises(AnalysisRunCancelled):
        await analyze_new_campaigns(
            fetcher=SimpleNamespace(),
            reasoner=SimpleNamespace(),
            parent_asin="B0TEST",
            shop_id=1622,
            parent_seller_sku="SKU-1",
            site_code="Amazon_US",
            shop_account="shop",
            existing_keywords=set(),
            pre_eliminated_count=0,
            strategy_context=SimpleNamespace(),
            ctx_dict={},
            temperature=0.3,
            cancel_check=_cancel,
        )
