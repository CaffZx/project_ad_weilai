import pytest
from fastapi import HTTPException

from app.api.product_identity import (
    require_product_identity,
    require_product_identity_dict,
)
from app.models.layers import ProductLevel, ProductStage, SeasonStage, StrategyConfirmRequest


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
