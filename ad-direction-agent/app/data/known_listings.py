"""Cached listing context when Doris is slow or unreachable."""

from __future__ import annotations

# parent_asin -> (parent_seller_sku, shop_id, site_code, shop_account)
KNOWN_LISTINGS: dict[str, tuple[str, int, str, str]] = {
    "B0CGH9QRKK": ("WY03235-80D新款", 1622, "Amazon_US", "am_vivibeautyus"),
    "B0B7S3PWWB": ("FS02721-3pcs", 1622, "Amazon_US", "am_vivibeautyus"),
    "B0DDWX58G3": ("8pcs-fishnet", 1622, "Amazon_US", "am_vivibeautyus"),
    "B0GCZHVBWN": ("FS03929-新宽三角bikini", 72549, "Amazon_US", "am_yidong_US"),
    "B0DNZNWGX5": ("FS03024-longsleeve-FU", 55454, "Amazon_US", "am_slow_US"),
}


def known_mcp_context(parent_asin: str):
    """Return McpDbContext-like dict fields or None."""
    row = KNOWN_LISTINGS.get((parent_asin or "").strip().upper())
    if not row:
        return None
    sku, shop_id, site_code, shop_account = row
    return {
        "parent_asin": parent_asin,
        "parent_seller_sku": sku,
        "shop_id": shop_id,
        "site_code": site_code,
        "shop_account": shop_account,
    }
