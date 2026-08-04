from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api import long_term_config as api
from app.models import layers


def test_save_all_request_requires_non_empty_multi_selects_and_accepts_page_identity_aliases():
    assert hasattr(layers, "SaveAllConfigRequest")
    request_type = layers.SaveAllConfigRequest

    request = request_type.model_validate(
        {
            "_shopId": 1622,
            "_parentSellerSku": "SKU-001",
            "_shopAccount": "shop-us",
            "_siteCode": "US",
            "ad_purposes": ["引流型"],
            "target_keyword_strategy": ["大词"],
            "directions": ["push_natural"],
        }
    )

    assert request.shop_id == 1622
    assert request.parent_seller_sku == "SKU-001"

    with pytest.raises(ValidationError):
        request_type.model_validate(
            {
                "_shopId": 1622,
                "_parentSellerSku": "SKU-001",
                "ad_purposes": [],
                "target_keyword_strategy": ["大词"],
                "directions": ["push_natural"],
            }
        )


class _State:
    def __init__(self, *, fail: str | None = None):
        self.fail = fail
        self.long_term = {
            "product_level": "常规产品 (P2)",
            "operating_mode": "稳定经营",
            "season_stage": "淡季",
        }
        self.execution = None
        self.advanced: list[str] = []
        self.acos = None
        self.current_layer = "tactics"

    def get_long_term_config(self, asin: str) -> dict:
        return dict(self.long_term)

    def set_long_term_config(self, asin: str, config: dict) -> bool:
        if self.fail == "long_term":
            return False
        self.long_term.update(config)
        return True

    def set_target_acos_override(
        self, asin: str, value: int, shop_id=None, parent_seller_sku=None
    ) -> bool:
        if self.fail == "acos":
            return False
        self.acos = value
        return True

    def get_target_acos_override(self, asin: str):
        return self.acos

    def save_execution(
        self, asin: str, selection: dict, shop_id=None, parent_seller_sku=None
    ) -> bool:
        if self.fail == "execution":
            return False
        self.execution = selection
        return True

    def get_workflow_state(self, asin: str) -> dict:
        return {"current_layer": self.current_layer}

    def advance_layer(
        self, asin: str, layer: str, shop_id=None, parent_seller_sku=None
    ) -> bool:
        if self.fail == f"advance:{layer}":
            return False
        self.advanced.append(layer)
        self.current_layer = layer
        return True


class _Repo:
    def __init__(self):
        self.upsert = None
        self.read_identity = None
        self.row = {
            "product_position": "P2_PRODUCT",
            "operating_mode": "STABLE_OPERATION",
            "season_type": "OFF_SEASON",
            "advert_purposes": '["TRAFFIC", "CONVERSION"]',
            "target_keyword_types": '["GENERIC", "LONG_TAIL"]',
            "target_acos_suggest": 28,
            "daily_budget_suggest": 120.5,
            "advert_direction_types": '["PUSH_NATURAL", "OPTIMIZE_ACOS"]',
            "update_time": "2026-08-04 12:00:00",
        }

    def upsert_agent_config(self, *, identity: dict, patch: dict) -> None:
        self.upsert = (identity, patch)

    def get_agent_config_row(self, identity: dict):
        self.read_identity = identity
        return dict(self.row)


def _request():
    return layers.SaveAllConfigRequest.model_validate(
        {
            "_shopId": 1622,
            "_parentSellerSku": "SKU-001",
            "_shopAccount": "shop-us",
            "_siteCode": "US",
            "_userId": 99,
            "ad_purposes": ["引流型", "转化型"],
            "target_keyword_strategy": ["大词", "长尾词"],
            "target_acos": 28,
            "daily_budget": 120.5,
            "directions": ["push_natural", "optimize_acos"],
        }
    )


def test_save_all_writes_one_full_erp_patch_and_exact_state_shapes(monkeypatch):
    assert hasattr(api, "save_all_config")
    state = _State()
    repo = _Repo()
    monkeypatch.setattr(api, "_get_repository", lambda: repo)

    snapshot = asyncio.run(api.save_all_config("B0TEST", _request(), state))

    identity, patch = repo.upsert
    assert identity == {
        "parent_asin": "B0TEST",
        "parent_seller_sku": "SKU-001",
        "shop_id": 1622,
        "shop_account": "shop-us",
        "site_code": "US",
        "user_id": 99,
    }
    assert patch == {
        "product_position": "常规产品 (P2)",
        "operating_mode": "稳定经营",
        "season_type": "淡季",
        "advert_purposes": ["引流型", "转化型"],
        "target_keyword_types": ["大词", "长尾词"],
        "target_acos_suggest": 28,
        "daily_budget_suggest": 120.5,
        "advert_direction_types": ["push_natural", "optimize_acos"],
    }
    assert state.execution == {
        "selected_directions": ["push_natural", "optimize_acos"],
        "sub_options": {},
    }
    assert state.long_term["shop_id"] == 1622
    assert state.long_term["parent_seller_sku"] == "SKU-001"
    assert state.advanced == ["validation"]
    assert snapshot.product_level == "常规产品 (P2)"
    assert snapshot.ad_purposes == ["引流型", "转化型"]
    assert snapshot.target_keyword_strategy == ["大词", "长尾词"]
    assert snapshot.directions == ["push_natural", "optimize_acos"]
    assert snapshot.p3["target_acos"]["manual_override"] is True
    assert snapshot.p3["budget_bid"]["manual_override"] is True

    asyncio.run(api.save_all_config("B0TEST", _request(), state))
    assert state.advanced == ["validation"]


def test_save_all_reports_state_mirror_failure_instead_of_false_success(monkeypatch):
    assert hasattr(api, "save_all_config")
    state = _State(fail="execution")
    repo = _Repo()
    monkeypatch.setattr(api, "_get_repository", lambda: repo)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(api.save_all_config("B0TEST", _request(), state))

    assert exc.value.status_code == 500
    assert "execution" in str(exc.value.detail)
    assert repo.upsert is not None


def test_latest_without_complete_identity_returns_empty_snapshot(monkeypatch):
    assert hasattr(api, "get_latest_config")
    repo = _Repo()
    monkeypatch.setattr(api, "_get_repository", lambda: repo)

    snapshot = asyncio.run(api.get_latest_config("B0TEST", None, None, _State()))

    assert snapshot == layers.ConfigSnapshotResponse(asin="B0TEST")
    assert repo.read_identity is None
