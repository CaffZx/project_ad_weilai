"""保存配置到 t_advet_agent_config 的 API 镜像契约。"""
from __future__ import annotations

import logging
from decimal import Decimal

import pytest

from app.api import config_mirror
from app.api import execution as execution_api
from app.api import strategy as strategy_api
from app.api import tactics as tactics_api
from app.models.layers import (
    BudgetBidRequest,
    BudgetOverrideRequest,
    ExecutionSelectRequest,
    ExecutionSelectResponse,
    StrategyConfirmRequest,
    StrategyConfirmResponse,
    TacticsConfirmRequest,
    TacticsConfirmResponse,
    TargetAcosOverrideRequest,
    TargetAcosRequest,
)


def test_strategy_request_accepts_existing_call_api_identity_extras():
    req = StrategyConfirmRequest.model_validate(
        {
            "asin": "B0TEST",
            "product_level": "重点产品 (P1)",
            "season_stage": "淡季",
            "_shopId": 1622,
            "_parentSellerSku": "SKU-1",
            "_shopAccount": "demo-shop",
            "_siteCode": "US",
            "_userId": "42",
        }
    )

    assert req.shop_account == "demo-shop"
    assert req.site_code == "US"
    assert req.user_id == "42"


def _request(**overrides) -> StrategyConfirmRequest:
    payload = {
        "asin": "B0TEST",
        "product_level": "重点产品 (P1)",
        "season_stage": "淡季",
        "_shopId": 1622,
        "_parentSellerSku": "SKU-1",
        "_shopAccount": "demo-shop",
        "_siteCode": "US",
        "_userId": "42",
    }
    payload.update(overrides)
    return StrategyConfirmRequest.model_validate(payload)


class _Repo:
    def __init__(self):
        self.calls: list[tuple[dict, dict]] = []

    def upsert_agent_config(self, *, identity, patch):
        self.calls.append((identity, patch))


class _FailingRepo:
    def upsert_agent_config(self, *, identity, patch):
        raise RuntimeError("ERP unavailable")


@pytest.mark.asyncio
async def test_mirror_passes_identity_and_patch_to_repository(monkeypatch, caplog):
    repo = _Repo()
    monkeypatch.setattr(config_mirror, "_get_repository", lambda: repo)

    with caplog.at_level(logging.INFO):
        await config_mirror.mirror_agent_config(
            req=_request(),
            patch={"target_acos_suggest": 25},
            operation="target_acos_override_save",
        )

    assert repo.calls == [
        (
            {
                "parent_asin": "B0TEST",
                "parent_seller_sku": "SKU-1",
                "shop_id": 1622,
                "shop_account": "demo-shop",
                "site_code": "US",
                "user_id": "42",
                "day_range": "DAY_7",
            },
            {"target_acos_suggest": 25},
        )
    ]
    assert "agent config mirror saved" in caplog.text


@pytest.mark.asyncio
async def test_mirror_logs_failure_without_raising(monkeypatch, caplog):
    monkeypatch.setattr(config_mirror, "_get_repository", lambda: _FailingRepo())

    with caplog.at_level(logging.ERROR):
        await config_mirror.mirror_agent_config(
            req=_request(),
            patch={"target_acos_suggest": 25},
            operation="target_acos_override_save",
        )

    assert "agent config mirror failed" in caplog.text


@pytest.mark.asyncio
async def test_mirror_skips_when_identity_is_incomplete(monkeypatch, caplog):
    repo = _Repo()
    monkeypatch.setattr(config_mirror, "_get_repository", lambda: repo)

    with caplog.at_level(logging.WARNING):
        await config_mirror.mirror_agent_config(
            req=_request(_parentSellerSku=""),
            patch={"target_acos_suggest": None},
            operation="target_acos_override_clear",
        )

    assert repo.calls == []
    assert "agent config mirror skipped" in caplog.text


class _SaveOrchestrator:
    def __init__(self):
        self.events: list[str] = []

    async def confirm_strategy(self, req):
        self.events.append("old_strategy")
        return StrategyConfirmResponse(asin=req.asin, accepted=True, config_saved=True)

    async def confirm_tactics(self, req):
        self.events.append("old_tactics")
        return TacticsConfirmResponse(asin=req.asin, accepted=True, config_saved=True)

    async def confirm_execution(self, req):
        self.events.append("old_execution")
        return ExecutionSelectResponse(asin=req.asin, accepted=True)

    def save_target_acos_override(self, *args, **kwargs):
        self.events.append("old_target_acos")
        return True

    def clear_target_acos_override(self, *args, **kwargs):
        self.events.append("old_target_acos_clear")
        return True

    def save_budget_override(self, *args, **kwargs):
        self.events.append("old_budget")
        return True

    def clear_budget_override(self, *args, **kwargs):
        self.events.append("old_budget_clear")
        return True


@pytest.mark.asyncio
async def test_strategy_confirm_mirrors_after_old_save(monkeypatch):
    orchestrator = _SaveOrchestrator()
    calls = []

    async def _mirror(**kwargs):
        calls.append(kwargs)
        orchestrator.events.append("mirror")

    monkeypatch.setattr(strategy_api, "mirror_agent_config", _mirror, raising=False)
    result = await strategy_api.strategy_confirm(
        _request(operating_mode="控制清货"), orchestrator
    )

    assert result.config_saved is True
    assert orchestrator.events == ["old_strategy", "mirror"]
    assert calls[0]["patch"] == {
        "product_position": "重点产品 (P1)",
        "operating_mode": "控制清货",
        "season_type": "淡季",
    }


@pytest.mark.asyncio
async def test_tactics_and_execution_confirm_mirror_their_own_fields(monkeypatch):
    orchestrator = _SaveOrchestrator()
    tactics_calls = []
    execution_calls = []

    async def _tactics_mirror(**kwargs):
        tactics_calls.append(kwargs)

    async def _execution_mirror(**kwargs):
        execution_calls.append(kwargs)

    monkeypatch.setattr(tactics_api, "mirror_agent_config", _tactics_mirror, raising=False)
    monkeypatch.setattr(execution_api, "mirror_agent_config", _execution_mirror, raising=False)
    tactics_req = TacticsConfirmRequest.model_validate(
        {
            "asin": "B0TEST",
            "ad_purposes": ["转化型"],
            "target_keyword_strategy": ["长尾词"],
            "_shopId": 1622,
            "_parentSellerSku": "SKU-1",
        }
    )
    execution_req = ExecutionSelectRequest.model_validate(
        {
            "asin": "B0TEST",
            "selected_directions": ["push_natural", "optimize_acos"],
            "_shopId": 1622,
            "_parentSellerSku": "SKU-1",
        }
    )

    await tactics_api.tactics_confirm(tactics_req, orchestrator)
    await execution_api.execution_select(execution_req, orchestrator)

    assert tactics_calls[0]["patch"] == {
        "advert_purposes": ["转化型"],
        "target_keyword_types": ["长尾词"],
    }
    assert execution_calls[0]["patch"] == {
        "advert_direction_types": ["push_natural", "optimize_acos"]
    }


@pytest.mark.asyncio
async def test_p3_save_and_clear_mirror_only_after_old_operation_succeeds(monkeypatch):
    orchestrator = _SaveOrchestrator()
    calls = []

    async def _mirror(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(execution_api, "mirror_agent_config", _mirror, raising=False)
    target_req = TargetAcosOverrideRequest.model_validate(
        {**_request().model_dump(), "value": 25}
    )
    budget_req = BudgetOverrideRequest.model_validate(
        {**_request().model_dump(), "value": 12.5}
    )
    clear_target_req = TargetAcosRequest.model_validate(_request().model_dump())
    clear_budget_req = BudgetBidRequest.model_validate(_request().model_dump())

    await execution_api.save_target_acos_override(target_req, orchestrator)
    await execution_api.save_budget_override(budget_req, orchestrator)
    await execution_api.clear_target_acos_override(clear_target_req, orchestrator)
    await execution_api.clear_budget_override(clear_budget_req, orchestrator)

    assert [item["patch"] for item in calls] == [
        {"target_acos_suggest": 25},
        {"daily_budget_suggest": Decimal("12.5")},
        {"target_acos_suggest": None},
        {"daily_budget_suggest": None},
    ]
