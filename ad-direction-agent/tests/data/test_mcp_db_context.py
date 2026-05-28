from __future__ import annotations

import pytest
from unittest.mock import patch

from app.data.mcp_db_context import resolve_mcp_context_from_db


@pytest.mark.asyncio
async def test_resolve_mcp_context_from_db_row():
    with patch("app.data.mcp_db_context.settings.mcp_resolve_sku_via_db", True):
        with patch("app.data.mcp_db_context.settings.mcp_default_parent_seller_sku", ""):
            with patch("app.data.mcp_db_context.settings.mcp_default_shop_account", "shop_us"):
                with patch("app.data.mcp_db_context.settings.db_host", "127.0.0.1"):
                    with patch("app.data.mcp_db_context._lookup_sync") as lookup:
                        lookup.side_effect = [
                            {
                                "parent_asin": "B0PARENT",
                                "parent_seller_sku": "SKU-001",
                                "shop_account": "shop_us",
                                "shop_id": 12,
                            },
                            None,
                        ]
                        ctx = await resolve_mcp_context_from_db("B0CHILD")
    assert ctx is not None
    assert ctx.parent_asin == "B0PARENT"
    assert ctx.parent_seller_sku == "SKU-001"


@pytest.mark.asyncio
async def test_resolve_mcp_context_env_fallback():
    with patch("app.data.mcp_db_context.settings.mcp_resolve_sku_via_db", True):
        with patch("app.data.mcp_db_context.settings.mcp_default_parent_seller_sku", "SKU-ENV"):
            with patch("app.data.mcp_db_context.settings.mcp_default_shop_account", "shop_us"):
                ctx = await resolve_mcp_context_from_db("B0TEST")
    assert ctx is not None
    assert ctx.parent_seller_sku == "SKU-ENV"
