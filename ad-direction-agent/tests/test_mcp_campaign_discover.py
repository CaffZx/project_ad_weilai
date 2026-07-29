"""ad_campaign_product_keyword_list normalizer + MCP discover 单元测试。

normalizer 直接测中文 key → 英文 key 映射（不依赖外部服务）。
_discover_context_from_mcp 通过 mock _mcp() 验证正常/空/失败三条路径。
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.api import decision as decision_api
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
    "match_type": "EXACT",
    "campaign_status": "ENABLED",
}


def test_normalize_single_row():
    out = _normalize_mcp_campaign_keywords([_SAMPLE_MCP_ROW])
    assert len(out) == 1
    assert out[0] == _EXPECTED_NORMALIZED


def test_normalize_uppercases_match_type():
    """P1 修复：MCP 返回小写 \"exact\"/\"broad\" → normalizer 必须转换大写。"""
    r = dict(_SAMPLE_MCP_ROW)
    r["关键词匹配类型"] = "exact"
    out = _normalize_mcp_campaign_keywords([r])
    assert out[0]["match_type"] == "EXACT"
    r["关键词匹配类型"] = "Broad"
    out = _normalize_mcp_campaign_keywords([r])
    assert out[0]["match_type"] == "BROAD"
    r["关键词匹配类型"] = ""
    out = _normalize_mcp_campaign_keywords([r])
    assert out[0]["match_type"] == ""  # 空串保持空串


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


def test_discover_from_mcp_strict_raises_on_mcp_failure():
    """立即退出链路不得把关键词工具失败静默解释为空活动。"""
    f = CampaignFetcher.__new__(CampaignFetcher)
    mock_mcp = MagicMock()
    mock_mcp.call_tool_timed_with_args = AsyncMock(return_value=SimpleNamespace(
        ok=False, error="timeout",
    ))
    f._mcp_adapter = mock_mcp

    with patch.object(f, "_mcp", return_value=mock_mcp):
        with pytest.raises(RuntimeError, match="timeout"):
            asyncio.run(
                f._discover_context_from_mcp(
                    "B0TEST", "am_test", strict=True,
                )
            )


def test_fetch_campaign_list_strict_raises_on_mcp_failure():
    """立即退出链路不得在活动列表查询失败时继续生成空决策。"""
    f = CampaignFetcher.__new__(CampaignFetcher)
    mock_mcp = MagicMock()
    mock_mcp.campaign_call_tool = AsyncMock(return_value=SimpleNamespace(
        ok=False, error="campaign list failed",
    ))
    f._mcp_adapter = mock_mcp

    with patch.object(f, "_mcp", return_value=mock_mcp):
        with pytest.raises(RuntimeError, match="campaign list failed"):
            asyncio.run(
                f._fetch_campaign_list(
                    "B0TEST", "SKU-1", "am_test", strict=True,
                )
            )


def test_immediate_exit_fetches_two_discovery_tools_in_parallel_then_basic():
    """只并行拉活动清单与关键词清单，之后按活动 ID 补 basic_info_v2。"""
    fetcher = MagicMock()
    both_started = asyncio.Event()
    started: set[str] = set()
    call_order: list[str] = []

    async def _campaign_list(*args, **kwargs):
        call_order.append("campaign_list_start")
        started.add("campaign")
        if len(started) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=1)
        return {"exact-one": "c-1", "auto-one": "c-2"}

    async def _keyword_list(*args, **kwargs):
        call_order.append("keyword_list_start")
        started.add("keyword")
        if len(started) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=1)
        return [{
            "campaign_id": "c-1",
            "campaign_name": "exact-one",
            "keyword_id": "k-1",
            "keyword_text": "red dress",
            "match_type": "EXACT",
        }]

    async def _basic(id_list, shop_account):
        call_order.append("basic")
        assert id_list == [("auto-one", "c-2"), ("exact-one", "c-1")]
        assert shop_account == "am_test"
        return {
            "exact-one": {
                "campaign_budget": 10,
                "keyword_bid": 0.5,
                "campaign_status": "enabled",
            },
            "auto-one": {
                "campaign_budget": 8,
                "keyword_bid": 0,
                "campaign_status": "enabled",
            },
        }

    fetcher._fetch_campaign_list = AsyncMock(side_effect=_campaign_list)
    fetcher._discover_context_from_mcp = AsyncMock(side_effect=_keyword_list)
    fetcher._fetch_basic_batch_v2 = AsyncMock(side_effect=_basic)

    with patch.object(decision_api, "CampaignFetcher", return_value=fetcher):
        result = asyncio.run(decision_api._fetch_immediate_exit_inputs(
            asin="B0TEST",
            identity={
                "parent_seller_sku": "SKU-1",
                "shop_account": "am_test",
            },
        ))

    assert call_order[:2] == ["campaign_list_start", "keyword_list_start"]
    assert call_order[-1] == "basic"
    assert result["campaign_name_to_id"]["exact-one"] == "c-1"
    assert len(result["keyword_rows"]) == 1


def test_immediate_exit_fetch_rejects_missing_basic_info():
    """任一活动缺 basic_info_v2 时停止，不用默认 0 预算/Bid 继续。"""
    fetcher = MagicMock()
    fetcher._fetch_campaign_list = AsyncMock(return_value={
        "exact-one": "c-1",
        "auto-one": "c-2",
    })
    fetcher._discover_context_from_mcp = AsyncMock(return_value=[])
    fetcher._fetch_basic_batch_v2 = AsyncMock(return_value={
        "exact-one": {
            "campaign_budget": 10,
            "keyword_bid": 0.5,
            "campaign_status": "enabled",
        },
    })

    with patch.object(decision_api, "CampaignFetcher", return_value=fetcher):
        with pytest.raises(RuntimeError, match="auto-one"):
            asyncio.run(decision_api._fetch_immediate_exit_inputs(
                asin="B0TEST",
                identity={
                    "parent_seller_sku": "SKU-1",
                    "shop_account": "am_test",
                },
            ))


def test_basic_info_v2_batches_campaign_ids_at_twenty():
    """basic_info_v2 延用既有每批最多 20 个 campaign_id 的限制。"""
    f = CampaignFetcher.__new__(CampaignFetcher)
    mock_mcp = MagicMock()

    def _respond(tool_name, campaign_name, shop_account, **kwargs):
        ids = kwargs["campaign_id_list"].split(",")
        assert len(ids) <= 20
        return SimpleNamespace(
            ok=True,
            value={"rows": [
                {
                    "广告活动名称": f"campaign-{int(cid[2:]):02d}",
                    "广告活动预算": 10,
                    "关键词BID": 0.5,
                    "状态": "enabled",
                }
                for cid in ids
            ]},
        )

    mock_mcp.campaign_call_tool = AsyncMock(side_effect=_respond)
    id_list = [
        (f"campaign-{index:02d}", f"id{index}")
        for index in range(45)
    ]

    with patch.object(f, "_mcp", return_value=mock_mcp):
        results = asyncio.run(f._fetch_basic_batch_v2(id_list, "am_test"))

    assert len(results) == 45
    assert mock_mcp.campaign_call_tool.await_count == 3


def test_classify_immediate_exit_actions_covers_every_campaign():
    """精准、广泛+词组、自动及商品投放均生成且只生成一个活动动作。"""
    inputs = {
        "campaign_name_to_id": {
            "exact-single": "c-1",
            "exact-multi": "c-2",
            "broad-phrase": "c-3",
            "phrase-one": "c-4",
            "auto-one": "c-5",
            "product-target": "c-6",
        },
        "keyword_rows": [
            {
                "campaign_id": "c-1", "campaign_name": "exact-single",
                "keyword_id": "k-1", "keyword_text": "red dress",
                "match_type": "EXACT",
            },
            {
                "campaign_id": "c-1", "campaign_name": "exact-single",
                "keyword_id": "nk-1", "keyword_text": "cheap",
                "match_type": "NEGATIVE_EXACT",
            },
            {
                "campaign_id": "c-2", "campaign_name": "exact-multi",
                "keyword_id": "k-2", "keyword_text": "blue dress",
                "match_type": "EXACT",
            },
            {
                "campaign_id": "c-2", "campaign_name": "exact-multi",
                "keyword_id": "k-3", "keyword_text": "green dress",
                "match_type": "EXACT",
            },
            {
                "campaign_id": "c-3", "campaign_name": "broad-phrase",
                "keyword_id": "k-4", "keyword_text": "dress",
                "match_type": "BROAD",
            },
            {
                "campaign_id": "c-3", "campaign_name": "broad-phrase",
                "keyword_id": "k-5", "keyword_text": "summer dress",
                "match_type": "PHRASE",
            },
            {
                "campaign_id": "c-4", "campaign_name": "phrase-one",
                "keyword_id": "k-6", "keyword_text": "summer dress",
                "match_type": "PHRASE",
            },
            {
                "campaign_id": "c-5", "campaign_name": "auto-one",
                "keyword_id": "", "keyword_text": "",
                "match_type": "AUTO",
            },
        ],
        "basic_by_name": {
            "exact-single": {
                "campaign_budget": 10, "keyword_bid": 0.5,
                "campaign_status": "enabled",
            },
            "exact-multi": {
                "campaign_budget": 8, "keyword_bid": 0.6,
                "campaign_status": "enabled",
            },
            "broad-phrase": {
                "campaign_budget": 12, "keyword_bid": 0.7,
                "campaign_status": "enabled",
            },
            "phrase-one": {
                "campaign_budget": 6, "keyword_bid": 0.4,
                "campaign_status": "enabled",
            },
            "auto-one": {
                "campaign_budget": 7, "keyword_bid": 0,
                "campaign_status": "enabled",
            },
            "product-target": {
                "campaign_budget": 4, "keyword_bid": 0,
                "campaign_status": "enabled",
            },
        },
    }

    actions = decision_api._classify_immediate_exit_actions(inputs)
    by_id = {item["campaign_id"]: item for item in actions}

    assert set(by_id) == {"c-1", "c-2", "c-3", "c-4", "c-5", "c-6"}
    assert by_id["c-1"]["action_kind"] == "LOW_BID_SINGLE_EXACT"
    assert by_id["c-1"]["new_budget"] == Decimal("1.00")
    assert by_id["c-1"]["new_bid"] == Decimal("0.20")
    assert [row["keyword_id"] for row in by_id["c-1"]["positive_keywords"]] == ["k-1"]

    assert by_id["c-2"]["action_kind"] == "LOW_BID_MULTI_EXACT"
    assert by_id["c-2"]["new_budget"] == Decimal("1.00")
    assert by_id["c-2"]["new_bid"] is None

    assert by_id["c-3"]["action_kind"] == "PAUSE"
    assert {
        row["match_type"] for row in by_id["c-3"]["positive_keywords"]
    } == {"BROAD", "PHRASE"}
    assert by_id["c-4"]["action_kind"] == "PAUSE"
    assert by_id["c-5"]["action_kind"] == "PAUSE"
    assert by_id["c-3"]["new_state"] == "paused"
    assert by_id["c-3"]["new_budget"] is None

    assert by_id["c-6"]["action_kind"] == "LOW_BID_PRODUCT_TARGET"
    assert by_id["c-6"]["new_budget"] == Decimal("1.00")
    assert by_id["c-6"]["new_bid"] is None


def test_classify_immediate_exit_deduplicates_multi_keyword_rows():
    """同一关键词的重复返回不应把单词精准活动误判成多词活动。"""
    row = {
        "campaign_id": "c-1", "campaign_name": "exact-single",
        "keyword_id": "k-1", "keyword_text": "red dress",
        "match_type": "EXACT",
    }
    actions = decision_api._classify_immediate_exit_actions({
        "campaign_name_to_id": {"exact-single": "c-1"},
        "keyword_rows": [row, dict(row)],
        "basic_by_name": {
            "exact-single": {
                "campaign_budget": 10,
                "keyword_bid": 0,
                "campaign_status": "enabled",
            },
        },
    })

    assert actions[0]["action_kind"] == "LOW_BID_SINGLE_EXACT"
    assert len(actions[0]["positive_keywords"]) == 1
    assert actions[0]["new_bid"] is None


def test_classify_immediate_exit_rejects_single_exact_without_keyword_id():
    """单词精准必须以 keywordId 修改已有关键词，不能仅凭文本下发 Bid。"""
    with pytest.raises(RuntimeError, match="keywordId"):
        decision_api._classify_immediate_exit_actions({
            "campaign_name_to_id": {"exact-single": "c-1"},
            "keyword_rows": [{
                "campaign_id": "c-1",
                "campaign_name": "exact-single",
                "keyword_id": "",
                "keyword_text": "red dress",
                "match_type": "EXACT",
            }],
            "basic_by_name": {
                "exact-single": {
                    "campaign_budget": 10,
                    "keyword_bid": 0.5,
                    "campaign_status": "enabled",
                },
            },
        })


def test_classify_immediate_exit_low_bid_budget_is_fixed_one():
    """低价策略预算固定为 $1，不依赖 current_budget 做 min 或缺省换算。"""
    actions = decision_api._classify_immediate_exit_actions({
        "campaign_name_to_id": {"product-target": "c-1"},
        "keyword_rows": [],
        "basic_by_name": {
            "product-target": {
                "campaign_budget": None,
                "keyword_bid": 0,
                "campaign_status": "enabled",
            },
        },
    })

    assert actions[0]["action_kind"] == "LOW_BID_PRODUCT_TARGET"
    assert actions[0]["new_budget"] == Decimal("1.00")
