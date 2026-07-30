import asyncio
import copy
import logging
import threading
import time
import tomllib
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.api import campaign as campaign_api
from app.api import decision as decision_api
from app.api.product_identity import (
    require_product_identity,
    require_product_identity_dict,
)
from app.config.settings import settings
from app.models.layers import (
    OperatingMode,
    ProductLevel,
    ProductStage,
    SeasonStage,
    StrategyConfirmRequest,
)
from app.models.asin_data import ASINData
from app.workflow.steps.strategy import run_confirm_strategy, run_get_strategy_options
from app.workflow.steps.wizard import run_get_wizard_state


class _StrategyState:
    def __init__(self, config: dict | None = None):
        self.saved_config: dict | None = None
        self.config = config or {}

    def set_long_term_config(self, asin: str, config: dict) -> bool:
        self.saved_config = config
        return True

    def advance_layer(self, *args, **kwargs) -> bool:
        return True

    def get_long_term_config(self, asin: str) -> dict:
        return self.config

    def get_workflow_state(self, asin: str) -> dict:
        return {}

    def config_exists(self, asin: str) -> bool:
        return bool(self.config)

    def get_target_acos_override(self, asin: str) -> None:
        return None


class _StrategyContext:
    def __init__(self, config: dict | None = None):
        self.state = _StrategyState(config)

    async def preload_data(self, *args, **kwargs) -> None:
        return None

    async def ensure_data(self, asin: str, *, days: int = 7) -> ASINData:
        return ASINData(asin=asin)


class _CampaignState:
    def __init__(self, long_term: dict):
        self.long_term = long_term

    def get_analysis_session(self, asin: str) -> None:
        return None

    def get_long_term_config(self, asin: str) -> dict:
        return self.long_term

    def get_workflow_state(self, asin: str) -> dict:
        return {}


def test_require_product_identity_accepts_main_page_injected_fields():
    req = StrategyConfirmRequest.model_validate(
        {
            "asin": "B0TEST",
            "_shopId": 1622,
            "_parentSellerSku": "SKU-001",
            "product_level": ProductLevel.P2,
            "product_stage": ProductStage.TEST,
            "season_stage": SeasonStage.OFF_SEASON,
        }
    )

    require_product_identity(req)


def test_strategy_confirm_allows_missing_product_stage_without_clearing_it(monkeypatch):
    """产品阶段改为前端非必填后，确认请求仍可保存且不提交空值。"""
    monkeypatch.setattr(settings, "data_source", "test")
    ctx = _StrategyContext()
    req = StrategyConfirmRequest(
        asin="B0TEST",
        shop_id=1622,
        parent_seller_sku="SKU-001",
        product_level=ProductLevel.P2,
        season_stage=SeasonStage.OFF_SEASON,
    )

    result = asyncio.run(run_confirm_strategy(ctx, req))

    assert result.accepted is True
    assert ctx.state.saved_config is not None
    assert "product_stage" not in ctx.state.saved_config


def test_strategy_options_exposes_and_prefills_operating_mode():
    ctx = _StrategyContext({"operating_mode": "控制清货"})

    result = asyncio.run(run_get_strategy_options(ctx, "B0TEST"))

    assert [dimension.id for dimension in result.dimensions] == [
        "product_level",
        "operating_mode",
        "season_stage",
    ]
    assert result.current_selection == {
        "product_level": None,
        "operating_mode": "控制清货",
        "season_stage": None,
    }


def test_strategy_confirm_persists_operating_mode(monkeypatch):
    monkeypatch.setattr(settings, "data_source", "test")
    ctx = _StrategyContext()
    req = StrategyConfirmRequest(
        asin="B0TEST",
        shop_id=1622,
        parent_seller_sku="SKU-001",
        product_level=ProductLevel.P2,
        season_stage=SeasonStage.OFF_SEASON,
        operating_mode=OperatingMode.CONTROLLED_CLEARANCE,
    )

    result = asyncio.run(run_confirm_strategy(ctx, req))

    assert result.accepted is True
    assert ctx.state.saved_config is not None
    assert ctx.state.saved_config["operating_mode"] == "控制清货"


def test_wizard_strategy_context_keeps_saved_operating_mode():
    ctx = _StrategyContext(
        {
            "product_level": "常规产品 (P2)",
            "season_stage": "淡季",
            "operating_mode": "控制清货",
        }
    )

    result = run_get_wizard_state(ctx, "B0TEST")

    assert result.strategy is not None
    assert result.strategy.operating_mode == "控制清货"


def test_immediate_exit_option_is_last_and_marked_as_dangerous():
    options_file = Path(__file__).parents[2] / "app" / "config" / "layer_options.toml"
    options = tomllib.loads(options_file.read_text(encoding="utf-8"))
    operating_modes = options["strategy"]["operating_mode"]["options"]

    assert operating_modes[-1]["id"] == "immediate_exit"
    assert operating_modes[-1]["risk_level"] == "danger"


def test_strategy_demo_marks_immediate_exit_and_confirms_before_save():
    demo = Path(__file__).parents[2] / "demo" / "ad-asisitant-agent.html"
    source = demo.read_text(encoding="utf-8")

    assert 'data-risk="${riskLevel}"' in source
    assert 'data-label="${opt.label}"' in source
    assert "高危操作" in source
    assert "dd-text-danger" in source
    assert "showImmediateExitConfirm" in source
    assert source.index("if (om === '立即退出')") < source.index(
        "callAPI('/strategy/confirm'"
    )


def test_immediate_exit_save_starts_deterministic_flow_before_loading_tactics():
    demo = Path(__file__).parents[2] / "demo" / "ad-asisitant-agent.html"
    source = demo.read_text(encoding="utf-8")
    handler_start = source.index("async function confirmStrategy()")
    handler_end = source.index("\n\nfunction applyPersistedStrategySelection", handler_start)
    handler = source[handler_start:handler_end]

    exit_start = handler.rindex("if (om === '立即退出')")
    exit_branch = handler[exit_start:handler.index("return;", exit_start)]

    assert handler.index("callAPI('/strategy/confirm'") < exit_start
    assert "const runId = ((_decisionContext || {}).in_progress || '').trim();" in exit_branch
    assert "callAPI('/decision/immediate-exit'" in exit_branch
    assert "await refreshDecisionContext();" in exit_branch
    assert "await loadAll(true);" in exit_branch
    assert "startImmediateExitStatusPolling" in exit_branch
    assert "await onCancelEventClick({ skipConfirm: true });" not in exit_branch
    assert "await loadTactics()" not in exit_branch


def test_immediate_exit_status_polling_is_bounded_and_snapshot_only():
    demo = Path(__file__).parents[2] / "demo" / "ad-asisitant-agent.html"
    source = demo.read_text(encoding="utf-8")
    start = source.index("async function startImmediateExitStatusPolling(decisionId)")
    polling = source[start:source.index("\n\nfunction showImmediateExitConfirm", start)]

    assert "attempt < 12" in polling
    assert "/decision/immediate-exit/status" in polling
    assert "executable: false" in polling


def test_campaign_guard_blocks_unsaved_strategy_fields():
    demo = Path(__file__).parents[2] / "demo" / "ad-asisitant-agent.html"
    source = demo.read_text(encoding="utf-8")
    guard_start = source.index("function guardUnsavedBeforeRun()")
    guard_end = source.index("\nfunction resetSaveTags()", guard_start)
    guard = source[guard_start:guard_end]

    assert "_strategyPendingFields()" in guard
    assert "产品定位" in source
    assert "经营模式" in source
    assert "季节阶段" in source


def test_realtime_immediate_exit_skips_data_fetch_and_llm_until_executor_is_available(monkeypatch, caplog):
    monkeypatch.setattr(
        campaign_api, "get_state_manager", lambda: _CampaignState({"operating_mode": "立即退出"})
    )
    async def unexpected_data_fetch(*args, **kwargs):
        raise AssertionError("立即退出的确定性执行接口未接入时不应预拉取数据")

    async def unexpected_campaign_llm(*args, **kwargs):
        raise AssertionError("立即退出不应进入 Campaign LLM")

    monkeypatch.setattr(campaign_api.DataAggregator, "fetch", unexpected_data_fetch)
    monkeypatch.setattr(campaign_api, "analyze_campaigns", unexpected_campaign_llm)
    caplog.set_level(logging.INFO, logger=campaign_api.logger.name)

    result, extra = asyncio.run(campaign_api._do_analyze({"asin": "B0EXIT"}))

    assert extra is None
    assert result.warnings == ["经营模式为“立即退出”，拒绝发起 Campaign 分析"]
    assert "确定性执行接口未接入，跳过数据拉取与 Campaign LLM" in caplog.text


def test_scheduled_immediate_exit_skips_data_fetch_and_llm_until_executor_is_available(monkeypatch):
    monkeypatch.setattr(campaign_api, "get_state_manager", lambda: _CampaignState({}))
    from app.data import decision_config_reader

    monkeypatch.setattr(
        decision_config_reader,
        "load_layer14",
        lambda *args, **kwargs: {"long_term": {"operating_mode": "立即退出"}},
    )

    async def unexpected_data_fetch(*args, **kwargs):
        raise AssertionError("立即退出的确定性执行接口未接入时不应预拉取数据")

    async def unexpected_campaign_llm(*args, **kwargs):
        raise AssertionError("立即退出不应进入 Campaign LLM")

    monkeypatch.setattr(campaign_api.DataAggregator, "fetch", unexpected_data_fetch)
    monkeypatch.setattr(campaign_api, "analyze_campaigns", unexpected_campaign_llm)

    result, extra = asyncio.run(
        campaign_api._do_analyze({"asin": "B0EXIT", "cfg_source": "config"})
    )

    assert extra is None
    assert result.warnings == ["经营模式为“立即退出”，拒绝发起 Campaign 分析"]


def test_strategy_demo_replaces_product_stage_with_persisted_operating_mode():
    demo = Path(__file__).parents[2] / "demo" / "ad-asisitant-agent.html"
    source = demo.read_text(encoding="utf-8")

    assert "product_stage" not in source
    assert "_snapshotRow('经营模式'" in source
    assert "operating_mode:om" in source
    assert "'/long-term-config/${encodeURIComponent(asin)}'" not in source
    assert "`/long-term-config/${encodeURIComponent(asin)}`" in source


def test_immediate_exit_decision_id_is_stable_for_same_event():
    assert decision_api._immediate_exit_decision_id(
        "B0TEST", "20260728T120000Z",
    ) == decision_api._immediate_exit_decision_id(
        "B0TEST", "20260728T120000Z",
    )


def test_existing_immediate_exit_decision_rejects_identity_mismatch():
    with pytest.raises(HTTPException) as exc:
        decision_api._validate_existing_immediate_exit_decision(
            {
                "id": "dec-1",
                "parent_asin": "B0OTHER",
                "shop_id": 999,
                "parent_seller_sku": "OTHER",
            },
            asin="B0TEST",
            identity={
                "shop_id": 1622,
                "parent_seller_sku": "SKU-1",
            },
        )

    assert exc.value.status_code == 409


def test_existing_immediate_exit_decision_rejects_normal_campaign_batch():
    """相同 run_id 的普通 Campaign 批次不得被立即退出恢复逻辑自动确认。"""
    with pytest.raises(HTTPException) as exc:
        decision_api._validate_existing_immediate_exit_decision(
            {
                "id": "dec-1",
                "parent_asin": "B0TEST",
                "shop_id": 1622,
                "parent_seller_sku": "SKU-1",
                "operating_mode": "STABLE_OPERATION",
            },
            asin="B0TEST",
            identity={
                "shop_id": 1622,
                "parent_seller_sku": "SKU-1",
            },
        )

    assert exc.value.status_code == 409
    assert "立即退出批次" in str(exc.value.detail)


def test_persist_immediate_exit_orders_write_finalize_clear_confirm():
    events: list[str] = []
    repo = MagicMock()
    repo.write_immediate_exit.side_effect = lambda *args, **kwargs: (
        events.append("write")
        or SimpleNamespace(as_dict=lambda: {"decision_id": "dec-1"})
    )
    repo.finalize_batch.side_effect = lambda *args, **kwargs: events.append(
        "finalize"
    )
    repo.confirm_decisions.side_effect = lambda *args, **kwargs: (
        events.append("confirm")
        or {"ok": True, "applied": 2, "skipped": 0}
    )
    state = MagicMock()
    state.clear_analysis_session_if_run.side_effect = lambda asin, run_id: (
        events.append("clear") or True
    )
    run = SimpleNamespace(
        decision_id="dec-1",
        parent_asin="B0TEST",
        cards=[
            SimpleNamespace(card_id="card-1"),
            SimpleNamespace(card_id="card-2"),
        ],
    )

    result = asyncio.run(
        decision_api._persist_and_confirm_immediate_exit(
            run=run,
            identity={"operator": "42", "run_id": "20260728T120000Z"},
            repo=repo,
            state=state,
        )
    )

    assert events == ["write", "finalize", "clear", "confirm"]
    repo.finalize_batch.assert_called_once_with(
        "dec-1", "B0TEST", analysis_mode="REALTIME",
    )
    repo.confirm_decisions.assert_called_once_with(
        "dec-1",
        [
            {"campaign_key": "card-1", "decision": "approve"},
            {"campaign_key": "card-2", "decision": "approve"},
        ],
        operator="42",
        in_progress=False,
    )
    assert result["decision_id"] == "dec-1"
    assert result["confirm"]["applied"] == 2


def test_persist_immediate_exit_does_not_clear_or_confirm_after_write_failure():
    repo = MagicMock()
    repo.write_immediate_exit.side_effect = RuntimeError("db write failed")
    state = MagicMock()
    run = SimpleNamespace(
        decision_id="dec-1",
        parent_asin="B0TEST",
        cards=[SimpleNamespace(card_id="card-1")],
    )

    with pytest.raises(RuntimeError, match="db write failed"):
        asyncio.run(
                decision_api._persist_and_confirm_immediate_exit(
                    run=run,
                    identity={"operator": "42", "run_id": "20260728T120000Z"},
                    repo=repo,
                    state=state,
                )
        )

    repo.finalize_batch.assert_not_called()
    state.clear_analysis_session_if_run.assert_not_called()
    repo.confirm_decisions.assert_not_called()


def test_persist_immediate_exit_same_decision_resumes_without_rewrite():
    """相同 run_id 重试只读既有 pending 状态，绝不重建已确认记录。"""
    events: list[str] = []
    repo = MagicMock()
    repo.get_decision_basic.return_value = {
        "id": "dec-1",
        "parent_asin": "B0TEST",
        "shop_id": 1622,
        "parent_seller_sku": "SKU-1",
        "operating_mode": "IMMEDIATE_EXIT",
        "is_latest": 1,
    }
    repo.read_snapshot.return_value = {
        "cards": [{
            "id": "card-1",
            "confirm_status": "CONFIRMED",
            "execute_status": "PENDING",
        }],
        "campaign_pending": [{
            "id": "cp-1",
            "confirm_status": "CONFIRMED",
            "execute_status": "PENDING",
        }],
        "keyword_pending": [],
        "placement_pending": [],
    }
    state = MagicMock()
    state.clear_analysis_session_if_run.side_effect = lambda asin, run_id: (
        events.append("clear") or True
    )
    run = SimpleNamespace(
        decision_id="dec-1",
        parent_asin="B0TEST",
        cards=[SimpleNamespace(card_id="card-1")],
    )

    with (
        patch.object(decision_api, "CampaignFetcher") as fetcher_cls,
        patch.object(decision_api, "AdvertMcpClient") as advert_client_cls,
    ):
        result = asyncio.run(
            decision_api._persist_and_confirm_immediate_exit(
                run=run,
                identity={
                    "shop_id": 1622,
                    "parent_seller_sku": "SKU-1",
                    "operator": "42",
                    "run_id": "20260728T120000Z",
                },
                repo=repo,
                state=state,
            )
        )

    repo.write_immediate_exit.assert_not_called()
    repo.finalize_batch.assert_not_called()
    repo.confirm_decisions.assert_not_called()
    fetcher_cls.assert_not_called()
    advert_client_cls.assert_not_called()
    assert events == ["clear"]
    assert result["resumed"] is True
    assert result["resume_execution"] is True
    assert result["execute_statuses"] == ["PENDING"]


def test_persist_immediate_exit_concurrent_same_run_writes_once():
    """两个并发同 runId 请求只能有一个创建批次，另一个在锁内恢复。"""

    class _ConcurrentRepo:
        def __init__(self):
            self.lock = threading.Lock()
            self.outside_precheck = threading.Barrier(2)
            self.existing = None
            self.snapshot = None
            self.write_count = 0

        @contextmanager
        def immediate_exit_lock(self, decision_id, timeout_seconds=30):
            acquired = self.lock.acquire(timeout=timeout_seconds)
            if not acquired:
                raise TimeoutError(decision_id)
            try:
                yield
            finally:
                self.lock.release()

        def get_decision_basic(self, decision_id):
            if not self.lock.locked():
                self.outside_precheck.wait(timeout=2)
            return copy.deepcopy(self.existing)

        def write_immediate_exit(self, run, *, operator):
            time.sleep(0.03)
            self.write_count += 1
            self.existing = {
                "id": run.decision_id,
                "parent_asin": run.parent_asin,
                "shop_id": 1622,
                "parent_seller_sku": "SKU-1",
                "operating_mode": "IMMEDIATE_EXIT",
                "is_latest": 0,
            }
            self.snapshot = {
                "cards": [{
                    "id": "card-1",
                    "confirm_status": "PENDING",
                    "execute_status": "PENDING",
                }],
                "campaign_pending": [{
                    "id": "cp-1",
                    "confirm_status": "PENDING",
                    "execute_status": "PENDING",
                }],
                "keyword_pending": [],
                "placement_pending": [],
            }
            return SimpleNamespace(as_dict=lambda: {"decision_id": run.decision_id})

        def finalize_batch(self, decision_id, asin, analysis_mode="REALTIME"):
            self.existing["is_latest"] = 1

        def read_snapshot(self, decision_id):
            return copy.deepcopy(self.snapshot)

        def confirm_decisions(
            self, decision_id, decisions, operator=None, *, in_progress=False,
        ):
            applied = 0
            for card in self.snapshot["cards"]:
                if card["confirm_status"] == "PENDING":
                    card["confirm_status"] = "CONFIRMED"
                    applied += 1
            for row in self.snapshot["campaign_pending"]:
                row["confirm_status"] = "CONFIRMED"
            return {"ok": True, "applied": applied, "skipped": 0}

    repo = _ConcurrentRepo()
    state = MagicMock()
    state.clear_analysis_session_if_run.return_value = True
    run = SimpleNamespace(
        decision_id="dec-1",
        parent_asin="B0TEST",
        cards=[SimpleNamespace(card_id="card-1")],
    )
    identity = {
        "shop_id": 1622,
        "parent_seller_sku": "SKU-1",
        "operator": "42",
        "run_id": "20260728T120000Z",
    }

    async def _run_twice():
        return await asyncio.gather(
            decision_api._persist_and_confirm_immediate_exit(
                run=run, identity=identity, repo=repo, state=state,
            ),
            decision_api._persist_and_confirm_immediate_exit(
                run=run, identity=identity, repo=repo, state=state,
            ),
        )

    results = asyncio.run(_run_twice())

    assert repo.write_count == 1
    assert sorted(bool(result.get("resumed")) for result in results) == [
        False,
        True,
    ]
    assert repo.snapshot["cards"][0]["confirm_status"] == "CONFIRMED"


def test_require_product_identity_rejects_missing_shop_and_sku():
    req = StrategyConfirmRequest.model_validate(
        {
            "asin": "B0TEST",
            "product_level": ProductLevel.P2,
            "product_stage": ProductStage.TEST,
            "season_stage": SeasonStage.OFF_SEASON,
        }
    )

    with pytest.raises(HTTPException) as exc:
        require_product_identity(req)

    assert exc.value.status_code == 400
    assert "shop_id" in str(exc.value.detail)
    assert "parent_seller_sku" in str(exc.value.detail)


def test_require_product_identity_rejects_missing_asin():
    req = StrategyConfirmRequest.model_validate(
        {
            "asin": "",
            "_shopId": 1622,
            "_parentSellerSku": "SKU-001",
            "product_level": ProductLevel.P2,
            "product_stage": ProductStage.TEST,
            "season_stage": SeasonStage.OFF_SEASON,
        }
    )

    with pytest.raises(HTTPException) as exc:
        require_product_identity(req)

    assert exc.value.status_code == 400
    assert "asin" in str(exc.value.detail)


def test_require_product_identity_dict_accepts_main_page_injected_fields():
    require_product_identity_dict(
        {"asin": "B0TEST", "_shopId": 1622, "_parentSellerSku": "SKU-001"}
    )


def test_require_product_identity_dict_rejects_missing_identity():
    with pytest.raises(HTTPException) as exc:
        require_product_identity_dict({"asin": "B0TEST"})

    assert exc.value.status_code == 400


def test_require_product_identity_dict_accepts_new_event_existing_fields():
    require_product_identity_dict(
        {"asin": "B0TEST", "shopId": 1622, "parent_seller_sku": "SKU-001"}
    )


def test_require_product_identity_dict_rejects_guessed_backend_aliases():
    with pytest.raises(HTTPException):
        require_product_identity_dict(
            {"parent_asin": "B0TEST", "shop_id": 1622, "parentSellerSku": "SKU-001"}
        )


def test_require_product_identity_dict_uses_explicit_asin_for_path_param_routes():
    require_product_identity_dict(
        {"_shopId": 1622, "_parentSellerSku": "SKU-001"},
        asin="B0TEST",
    )


def test_require_product_identity_dict_rejects_missing_explicit_asin():
    with pytest.raises(HTTPException) as exc:
        require_product_identity_dict(
            {"_shopId": 1622, "_parentSellerSku": "SKU-001"},
            asin="",
        )

    assert exc.value.status_code == 400
    assert "asin" in str(exc.value.detail)


def _immediate_exit_request() -> dict:
    return {
        "asin": "B0TEST",
        "_shopId": 1622,
        "_parentSellerSku": "SKU-001",
        "_shopAccount": "US-account",
        "_siteCode": "Amazon_US",
        "_userId": "operator",
        "run_id": "20260728T010203Z",
    }


def test_validate_immediate_exit_accepts_saved_mode_and_normalizes_context():
    state = _StrategyState({"operating_mode": "立即退出", "product_level": "常规产品"})

    asin, identity, long_term = decision_api._validate_immediate_exit_request(
        _immediate_exit_request(),
        state=state,
    )

    assert asin == "B0TEST"
    assert identity == {
        "shop_id": 1622,
        "parent_seller_sku": "SKU-001",
        "shop_account": "US-account",
        "site_code": "Amazon_US",
        "operator": "operator",
        "run_id": "20260728T010203Z",
    }
    assert long_term == {
        "operating_mode": "立即退出",
        "product_level": "常规产品",
    }


def test_validate_immediate_exit_uses_saved_mode_not_request_mode():
    request = _immediate_exit_request()
    request["operating_mode"] = "立即退出"
    state = _StrategyState({"operating_mode": "控制清货"})

    with pytest.raises(HTTPException) as exc:
        decision_api._validate_immediate_exit_request(request, state=state)

    assert exc.value.status_code == 409
    assert "不是“立即退出”" in str(exc.value.detail)


def test_validate_immediate_exit_rejects_unknown_saved_mode():
    state = _StrategyState({"operating_mode": "未知模式"})

    with pytest.raises(HTTPException) as exc:
        decision_api._validate_immediate_exit_request(
            _immediate_exit_request(),
            state=state,
        )

    assert exc.value.status_code == 409
    assert "经营模式无效" in str(exc.value.detail)


@pytest.mark.parametrize(
    "field",
    [
        "asin",
        "_shopId",
        "_parentSellerSku",
        "_shopAccount",
        "_siteCode",
        "_userId",
        "run_id",
    ],
)
def test_validate_immediate_exit_rejects_missing_required_context(field):
    request = _immediate_exit_request()
    request[field] = ""
    state = _StrategyState({"operating_mode": "立即退出"})

    with pytest.raises(HTTPException) as exc:
        decision_api._validate_immediate_exit_request(request, state=state)

    assert exc.value.status_code == 400


def test_validate_immediate_exit_rejects_non_stop_permission(monkeypatch):
    state = _StrategyState({"operating_mode": "立即退出"})
    monkeypatch.setattr(
        decision_api,
        "operating_mode_to_permission",
        lambda mode: decision_api.AdPermission.NORMAL,
        raising=False,
    )

    with pytest.raises(HTTPException) as exc:
        decision_api._validate_immediate_exit_request(
            _immediate_exit_request(),
            state=state,
        )

    assert exc.value.status_code == 409
    assert "停止权限" in str(exc.value.detail)


def test_immediate_exit_route_validates_saved_mode_before_run():
    state = MagicMock()
    expected = {
        "ok": True,
        "decision_id": "dec-1",
        "task_ids": ["task-1"],
        "execute_status": "IN_PROGRESS",
    }
    with (
        patch.object(decision_api, "get_state_manager", return_value=state),
        patch.object(
            decision_api,
            "_validate_immediate_exit_request",
            return_value=(
                "B0TEST",
                {"operator": "operator", "run_id": "run-1"},
                {"operating_mode": "立即退出"},
            ),
        ) as validate,
        patch.object(
            decision_api,
            "_run_immediate_exit",
            new=AsyncMock(return_value=expected),
            create=True,
        ) as run,
    ):
        result = asyncio.run(decision_api.immediate_exit(
            _immediate_exit_request()
        ))

    assert result == expected
    validate.assert_called_once_with(
        _immediate_exit_request(),
        state=state,
    )
    run.assert_awaited_once_with(
        asin="B0TEST",
        identity={"operator": "operator", "run_id": "run-1"},
        long_term={"operating_mode": "立即退出"},
        state=state,
    )


def test_immediate_exit_route_is_registered():
    assert any(
        getattr(route, "path", "") == "/decision/immediate-exit"
        and "POST" in (getattr(route, "methods", set()) or set())
        for route in decision_api.router.routes
    )


def test_immediate_exit_status_route_validates_and_delegates():
    with patch.object(
        decision_api,
        "poll_execution_result",
        new=AsyncMock(return_value={
            "ok": True,
            "decision_id": "dec-1",
            "execute_status": "SUCCESS",
        }),
        create=True,
    ) as poll:
        result = asyncio.run(decision_api.immediate_exit_status({
            "decision_id": " dec-1 ",
            "_userId": " operator ",
        }))

    assert result["execute_status"] == "SUCCESS"
    poll.assert_awaited_once_with("dec-1", operator="operator")

    missing = asyncio.run(decision_api.immediate_exit_status({}))
    assert missing == {"ok": False, "error": "decision_id 必填"}


def test_immediate_exit_status_route_is_registered():
    assert any(
        getattr(route, "path", "")
        == "/decision/immediate-exit/status"
        and "POST" in (getattr(route, "methods", set()) or set())
        for route in decision_api.router.routes
    )


@pytest.mark.parametrize(
    ("execution", "expected_status", "expected_error"),
    [
        (
            {"ok": True, "dry_run": True, "task_ids": []},
            "DRY_RUN",
            "未真实提交",
        ),
        (
            {"ok": True, "task_ids": [], "execute_status": None},
            "FAIL",
            "未返回 taskId",
        ),
    ],
)
def test_immediate_exit_response_never_guesses_in_progress(
    execution,
    expected_status,
    expected_error,
):
    result = decision_api._immediate_exit_execution_response(
        "dec-1",
        execution,
    )

    assert result["ok"] is False
    assert result["execute_status"] == expected_status
    assert expected_error in result["error"]


def test_immediate_exit_response_accepts_atomic_claim_lost_as_resumed():
    result = decision_api._immediate_exit_execution_response(
        "dec-1",
        {
            "ok": True,
            "already_claimed": True,
            "task_ids": [],
            "execute_status": "IN_PROGRESS",
            "move_errors": [],
        },
    )

    assert result == {
        "ok": True,
        "decision_id": "dec-1",
        "resumed": True,
        "task_ids": [],
        "execute_status": "IN_PROGRESS",
        "move_errors": [],
    }


def test_run_immediate_exit_orders_new_batch_then_submits_from_database():
    events: list[str] = []
    repo = MagicMock()
    state = MagicMock()
    run = SimpleNamespace(decision_id="dec-1")
    identity = {
        "operator": "operator",
        "run_id": "run-1",
    }

    async def _precheck(**kwargs):
        events.append("precheck")
        return None

    async def _fetch(**kwargs):
        events.append("fetch")
        return {"inputs": True}

    def _classify(inputs):
        events.append("classify")
        return [{"action_kind": "LOW_BID_SINGLE_EXACT"}]

    async def _resolve(**kwargs):
        events.append("portfolio")
        return {"portfolio_id": "pf-1", "match_count": 1}

    def _build(**kwargs):
        events.append("build")
        return run

    async def _persist(**kwargs):
        events.append("persist")
        return {"decision_id": "dec-1"}

    async def _submit(*args, **kwargs):
        events.append("submit")
        return {
            "ok": True,
            "task_ids": ["task-1"],
            "execute_status": "IN_PROGRESS",
            "move_errors": [],
        }

    with (
        patch.object(decision_api, "_repo", return_value=repo),
        patch.object(
            decision_api,
            "_precheck_immediate_exit_decision",
            new=_precheck,
        ),
        patch.object(decision_api, "_fetch_immediate_exit_inputs", new=_fetch),
        patch.object(
            decision_api,
            "_classify_immediate_exit_actions",
            new=_classify,
        ),
        patch.object(
            decision_api,
            "_resolve_immediate_exit_low_bid_portfolio",
            new=_resolve,
        ),
        patch.object(decision_api, "_build_immediate_exit_run", new=_build),
        patch.object(
            decision_api,
            "_persist_and_confirm_immediate_exit",
            new=_persist,
        ),
        patch.object(
            decision_api,
            "submit_execution",
            new=_submit,
            create=True,
        ),
    ):
        result = asyncio.run(decision_api._run_immediate_exit(
            asin="B0TEST",
            identity=identity,
            long_term={"operating_mode": "立即退出"},
            state=state,
        ))

    assert events == [
        "precheck", "fetch", "classify", "portfolio",
        "build", "persist", "submit",
    ]
    assert result == {
        "ok": True,
        "decision_id": "dec-1",
        "task_ids": ["task-1"],
        "execute_status": "IN_PROGRESS",
        "move_errors": [],
    }


def test_run_immediate_exit_resume_requeries_portfolio_without_refetch():
    repo = MagicMock()
    state = MagicMock()
    identity = {"operator": "operator", "run_id": "run-1"}
    decision_id = decision_api._immediate_exit_decision_id(
        "B0TEST", "run-1",
    )
    existing = {
        "decision_id": decision_id,
        "resumed": True,
        "resume_execution": True,
        "needs_low_bid_portfolio": True,
        "execute_statuses": ["PENDING"],
    }

    with (
        patch.object(decision_api, "_repo", return_value=repo),
        patch.object(
            decision_api,
            "_precheck_immediate_exit_decision",
            new=AsyncMock(return_value=existing),
        ),
        patch.object(
            decision_api,
            "_fetch_immediate_exit_inputs",
            new=AsyncMock(),
        ) as fetch,
        patch.object(
            decision_api,
            "_resolve_immediate_exit_low_bid_portfolio",
            new=AsyncMock(return_value={
                "portfolio_id": "pf-1",
                "match_count": 1,
            }),
        ) as resolve,
        patch.object(
            decision_api,
            "submit_execution",
            new=AsyncMock(return_value={
                "ok": True,
                "task_ids": ["task-1"],
                "execute_status": "IN_PROGRESS",
                "move_errors": [],
            }),
            create=True,
        ) as submit,
    ):
        result = asyncio.run(decision_api._run_immediate_exit(
            asin="B0TEST",
            identity=identity,
            long_term={"operating_mode": "立即退出"},
            state=state,
        ))

    fetch.assert_not_awaited()
    resolve.assert_awaited_once_with(
        identity=identity,
        asin="B0TEST",
        required=True,
    )
    submit.assert_awaited_once_with(
        decision_id,
        operator="operator",
        resolved_portfolio_ids={"low_bid_retention_group": "pf-1"},
        wait_for_terminal=True,
    )
    assert result["execute_status"] == "IN_PROGRESS"


def test_run_immediate_exit_in_progress_retry_does_not_resubmit():
    repo = MagicMock()
    state = MagicMock()
    decision_id = decision_api._immediate_exit_decision_id(
        "B0TEST", "run-1",
    )
    existing = {
        "decision_id": decision_id,
        "resumed": True,
        "resume_execution": False,
        "needs_low_bid_portfolio": True,
        "execute_statuses": ["IN_PROGRESS"],
    }

    with (
        patch.object(decision_api, "_repo", return_value=repo),
        patch.object(
            decision_api,
            "_precheck_immediate_exit_decision",
            new=AsyncMock(return_value=existing),
        ),
        patch.object(
            decision_api,
            "_fetch_immediate_exit_inputs",
            new=AsyncMock(),
        ) as fetch,
        patch.object(
            decision_api,
            "_resolve_immediate_exit_low_bid_portfolio",
            new=AsyncMock(),
        ) as resolve,
        patch.object(
            decision_api,
            "submit_execution",
            new=AsyncMock(),
            create=True,
        ) as submit,
    ):
        result = asyncio.run(decision_api._run_immediate_exit(
            asin="B0TEST",
            identity={"operator": "operator", "run_id": "run-1"},
            long_term={"operating_mode": "立即退出"},
            state=state,
        ))

    fetch.assert_not_awaited()
    resolve.assert_not_awaited()
    submit.assert_not_awaited()
    assert result == {
        "ok": True,
        "decision_id": decision_id,
        "resumed": True,
        "task_ids": [],
        "execute_status": "IN_PROGRESS",
        "execute_statuses": ["IN_PROGRESS"],
    }


def test_run_immediate_exit_failed_retry_returns_not_ok():
    decision_id = decision_api._immediate_exit_decision_id(
        "B0TEST", "run-1",
    )
    existing = {
        "decision_id": decision_id,
        "resumed": True,
        "resume_execution": False,
        "needs_low_bid_portfolio": True,
        "execute_statuses": ["FAIL"],
    }

    with (
        patch.object(decision_api, "_repo", return_value=MagicMock()),
        patch.object(
            decision_api,
            "_precheck_immediate_exit_decision",
            new=AsyncMock(return_value=existing),
        ),
        patch.object(
            decision_api,
            "submit_execution",
            new=AsyncMock(),
        ) as submit,
    ):
        result = asyncio.run(decision_api._run_immediate_exit(
            asin="B0TEST",
            identity={"operator": "operator", "run_id": "run-1"},
            long_term={"operating_mode": "立即退出"},
            state=MagicMock(),
        ))

    submit.assert_not_awaited()
    assert result["ok"] is False
    assert result["execute_status"] == "FAIL"
