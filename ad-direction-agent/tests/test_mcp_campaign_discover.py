"""ad_campaign_product_keyword_list normalizer + MCP discover 单元测试。

normalizer 直接测中文 key → 英文 key 映射（不依赖外部服务）。
_discover_context_from_mcp 通过 mock _mcp() 验证正常/空/失败三条路径。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.data.campaign_fetcher import (
    CampaignFetcher,
    _as_rows,
    _normalize_mcp_campaign_keywords,
)

# ── 与 B0B7S3PWWB 实跑数据同形 ──

_SAMPLE_MCP_ROW = {
    "广告活动D": "284444079449619",
    "广告活动名称": "3pcs渔网-广告受众测试",
    "关键词D": "548168016220710",
    "子SIN": "B09SGC3YZB",
    "子卖家KU": "FS02721-07-US",
    "关键词": "fishnet stockings",
    "关键词匹配类型": "exact",
}

_EXPECTED_NORMALIZED = {
    "campaign_id": "284444079449619",
    "campaign_name": "3pcs渔网-广告受众测试",
    "keyword_id": "548168016220710",
    "child_asin": "B09SGC3YZB",
    "seller_sku": "FS02721-07-US",
    "keyword_text": "fishnet stockings",
    "match_type": "exact",
    "campaign_status": "ENABLED",
    "keyword_status": "ENABLED",
}


def test_normalize_single_row():
    out = _normalize_mcp_campaign_keywords([_SAMPLE_MCP_ROW])
    assert len(out) == 1
    assert out[0] == _EXPECTED_NORMALIZED


def test_normalize_empty_input():
    assert _normalize_mcp_campaign_keywords([]) == []
    assert _normalize_mcp_campaign_keywords(None) == []


def test_normalize_preserves_unknown_keys():
    """未在映射表中的 key 原样透传（不影响下游，只多不丢）。"""
    r = dict(_SAMPLE_MCP_ROW)
    r["额外字段"] = "extra"
    out = _normalize_mcp_campaign_keywords([r])
    assert out[0].get("额外字段") == "extra"
    assert out[0].get("campaign_id") == _SAMPLE_MCP_ROW["广告活动D"]


def test_as_rows_handles_mcp_envelope():
    """_as_rows 解析常见 MCP 封装格式。"""
    assert _as_rows(None) == []
    assert _as_rows([{"a": 1}]) == [{"a": 1}]
    assert _as_rows({"rows": [{"b": 2}]}) == [{"b": 2}]
    assert _as_rows({"data": [{"c": 3}]}) == [{"c": 3}]


def test_discover_from_mcp_ok():
    """MCP 正常返回 → normalizer 产出格式与 _discover_context_from_doris 一致。"""
    f = CampaignFetcher.__new__(CampaignFetcher)
    mock_mcp = MagicMock()
    mock_mcp.call_tool_timed_with_args = AsyncMock(return_value=SimpleNamespace(
        ok=True, value={"rows": [_SAMPLE_MCP_ROW]},
    ))
    f._mcp_adapter = mock_mcp

    with patch.object(f, "_mcp", return_value=mock_mcp):
        rows = asyncio.run(f._discover_context_from_mcp("B0TEST", "am_test"))

    assert len(rows) == 1
    assert rows[0]["campaign_name"] == "3pcs渔网-广告受众测试"
    assert rows[0]["campaign_status"] == "ENABLED"


def test_discover_from_mcp_fails_silently():
    """MCP 失败/超时 → 返回 []（不抛异常，由调用方回落 Doris）。"""
    f = CampaignFetcher.__new__(CampaignFetcher)
    mock_mcp = MagicMock()
    mock_mcp.call_tool_timed_with_args = AsyncMock(return_value=SimpleNamespace(
        ok=False, error="timeout",
    ))
    f._mcp_adapter = mock_mcp

    with patch.object(f, "_mcp", return_value=mock_mcp):
        rows = asyncio.run(f._discover_context_from_mcp("B0TEST", "am_test"))
    assert rows == []


def test_discover_from_mcp_empty_rows():
    """MCP ok 但 rows 为空 → 返回 []。"""
    f = CampaignFetcher.__new__(CampaignFetcher)
    mock_mcp = MagicMock()
    mock_mcp.call_tool_timed_with_args = AsyncMock(return_value=SimpleNamespace(
        ok=True, value={"rows": []},
    ))
    f._mcp_adapter = mock_mcp

    with patch.object(f, "_mcp", return_value=mock_mcp):
        rows = asyncio.run(f._discover_context_from_mcp("B0TEST", "am_test"))
    assert rows == []


def test_discover_from_mcp_envelope_format():
    """MCP 返回双层 JSON 信封 {content:[{type:text, text:'{\"success\":true,\"rows\":[...]}'}]}
    时能正确解包（2026-06-27 线上验证发现信封未解包导致全部回落 Doris）。
    """
    import json as _json
    f = CampaignFetcher.__new__(CampaignFetcher)
    envelope = {
        "content": [{
            "type": "text",
            "text": _json.dumps({"success": True, "rows": [_SAMPLE_MCP_ROW]}),
        }],
    }
    mock_mcp = MagicMock()
    mock_mcp.call_tool_timed_with_args = AsyncMock(return_value=SimpleNamespace(
        ok=True, value=envelope,
    ))
    f._mcp_adapter = mock_mcp

    with patch.object(f, "_mcp", return_value=mock_mcp):
        rows = asyncio.run(f._discover_context_from_mcp("B0TEST", "am_test"))
    assert len(rows) == 1
    assert rows[0]["campaign_id"] == "284444079449619"
