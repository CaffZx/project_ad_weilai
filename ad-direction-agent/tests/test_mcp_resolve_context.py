"""parent_listing_detail normalizer + MCP resolve 单元测试。

normalizer 直接测中文 key→McpDbContext 映射；resolve_mcp_context_from_mcp
通过 mock adapter 验证 MCP ok/fail/空三条路径。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.data.mcp_db_context import (
    McpDbContext,
    _MCP_PARENT_KEY_MAP,
    _coerce_int,
    resolve_mcp_context_from_mcp,
)

_SAMPLE_MCP_JSON = (
    '{"success":true,"found":true,"count":1,'
    '"rows":[{"店铺ID":1622,"父ASIN":"B0TEST","店铺账号":"am_test",'
    '"产品名称":"Test Product","产品中文名":"测试产品","父卖家SKU":"SKU-001","站点":"Amazon_US"}]}'
)


def _make_mcp(value) -> AsyncMock:
    """构造一个带 call_tool_timed_with_args 的 mock adapter。"""
    m = AsyncMock()
    m.call_tool_timed_with_args = AsyncMock(return_value=value)
    return m


def test_key_map_covers_all_required():
    required = {"父ASIN", "父卖家SKU", "店铺ID", "店铺账号", "站点", "产品中文名", "产品名称"}
    assert set(_MCP_PARENT_KEY_MAP) == required


def test_resolve_from_mcp_ok():
    m = _make_mcp(SimpleNamespace(ok=True, value={"content": [
        {"type": "text", "text": _SAMPLE_MCP_JSON}
    ]}))
    ctx = asyncio.run(resolve_mcp_context_from_mcp("B0TEST", m))
    assert ctx is not None
    assert ctx.parent_asin == "B0TEST"
    assert ctx.parent_seller_sku == "SKU-001"
    assert ctx.shop_account == "am_test"
    assert ctx.shop_id == 1622
    assert ctx.site_code == "Amazon_US"
    assert ctx.product_name == "测试产品"  # 优先中文名


def test_resolve_from_mcp_fails_silently():
    m = _make_mcp(SimpleNamespace(ok=False, error="timeout"))
    ctx = asyncio.run(resolve_mcp_context_from_mcp("B0TEST", m))
    assert ctx is None


def test_resolve_from_mcp_empty_rows():
    m = _make_mcp(SimpleNamespace(ok=True, value={"content": [
        {"type": "text", "text": '{"success":true,"found":false,"count":0,"rows":[]}'}
    ]}))
    ctx = asyncio.run(resolve_mcp_context_from_mcp("B0TEST", m))
    assert ctx is None


def test_resolve_from_mcp_raw_flat():
    """部分 MCP 工具的返回可能不是 {content:[...]} 双层包装。"""
    m = _make_mcp(SimpleNamespace(ok=True, value={
        "success": True, "rows": [{
            "父ASIN": "B0TEST", "父卖家SKU": "SKU001", "店铺ID": 1,
            "店铺账号": "shop", "站点": "Amazon_US",
            "产品名称": "p", "产品中文名": "p中",
        }]
    }))
    ctx = asyncio.run(resolve_mcp_context_from_mcp("B0TEST", m))
    assert ctx is not None
    assert ctx.parent_seller_sku == "SKU001"


def test_resolve_from_mcp_missing_required_returns_none():
    """P3 修复：MCP 返回缺少 parent_seller_sku 或 shop_account → 返回 None（回落 DB）。"""
    m = _make_mcp(SimpleNamespace(ok=True, value={"content": [
        {"type": "text", "text": '{"success":true,"rows":[{"父ASIN":"X","店铺ID":1,"站点":"Amazon_US"}]}'}
    ]}))
    ctx = asyncio.run(resolve_mcp_context_from_mcp("B0TEST", m))
    assert ctx is None  # 缺 sku + shop → 不应返回 McpDbContext


def test_coerce_int():
    assert _coerce_int("1622") == 1622
    assert _coerce_int(1622) == 1622
    assert _coerce_int("") is None
    assert _coerce_int(None) is None
    assert _coerce_int("abc") is None
