from fastapi import HTTPException


_SHOP_ID_KEYS = ("shop_id", "_shopId", "shopId")
_PARENT_SKU_KEYS = ("parent_seller_sku", "_parentSellerSku", "parentSellerSku")


def _valid_shop_id(value) -> bool:
    try:
        return int(value) > 0
    except (TypeError, ValueError):
        return False


def _valid_parent_seller_sku(value) -> bool:
    return bool(str(value or "").strip())


def _identity_error():
    return HTTPException(
        status_code=400,
        detail="missing product identity: shop_id and parent_seller_sku are required",
    )


def require_product_identity(req) -> None:
    if not _valid_shop_id(getattr(req, "shop_id", None)):
        raise _identity_error()
    if not _valid_parent_seller_sku(getattr(req, "parent_seller_sku", None)):
        raise _identity_error()


def require_product_identity_dict(req: dict) -> None:
    shop_id = next((req.get(k) for k in _SHOP_ID_KEYS if req.get(k) is not None), None)
    parent_seller_sku = next(
        (req.get(k) for k in _PARENT_SKU_KEYS if req.get(k) is not None),
        None,
    )
    if not _valid_shop_id(shop_id) or not _valid_parent_seller_sku(parent_seller_sku):
        raise _identity_error()
