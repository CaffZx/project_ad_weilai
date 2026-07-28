import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException

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


def test_strategy_demo_replaces_product_stage_with_persisted_operating_mode():
    demo = Path(__file__).parents[2] / "demo" / "ad-asisitant-agent.html"
    source = demo.read_text(encoding="utf-8")

    assert "product_stage" not in source
    assert "_snapshotRow('经营模式'" in source
    assert "operating_mode:om" in source
    assert "'/long-term-config/${encodeURIComponent(asin)}'" not in source
    assert "`/long-term-config/${encodeURIComponent(asin)}`" in source


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
