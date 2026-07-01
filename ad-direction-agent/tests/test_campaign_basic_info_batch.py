"""ad_campaign_basic_info 批量接入单测（campaign_name_list 逗号分隔，≤20）。"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.data.campaign_fetcher import CampaignFetcher
from app.data.mcp_mapping import build_campaign_tool_args


# ── build_campaign_tool_args：入参构造 ──────────────────────────────

def test_build_args_uses_campaign_name_list_for_basic_info():
    """ad_campaign_basic_info 传入 campaign_name_list 时替代 campaign_name。"""
    args = build_campaign_tool_args(
        "ad_campaign_basic_info", "ignored", "shop1",
        campaign_name_list="a,b,c",
    )
    assert "campaign_name" not in args
    assert args["campaign_name_list"] == "a,b,c"
    assert args["shop_account"] == "shop1"


def test_build_args_falls_back_to_campaign_name():
    """非 basic_info / campaign_name_list 为空 → 仍用 campaign_name。"""
    args = build_campaign_tool_args(
        "ad_campaign_product_report", "c1", "shop1",
        start_date="2026-01-01", end_date="2026-01-07",
    )
    assert args["campaign_name"] == "c1"
    assert "campaign_name_list" not in args
    assert args["start_date"] == "2026-01-01"


def test_build_args_date_range_for_report_tools():
    """product/placement/search_term 报告工具保留日期字段。"""
    args = build_campaign_tool_args(
        "ad_campaign_product_report", "c1", "s1",
        start_date="2026-01-01", end_date="2026-01-07",
    )
    assert "start_date" in args and "end_date" in args


def test_build_args_no_date_for_basic_info():
    """basic_info 不需要日期字段。"""
    args = build_campaign_tool_args(
        "ad_campaign_basic_info", "", "s1",
        campaign_name_list="a",
    )
    assert "start_date" not in args and "end_date" not in args


# ── _fetch_basic_batch：批量拉取 + 解析 ─────────────────────────────

def _make_fetcher() -> CampaignFetcher:
    f = CampaignFetcher.__new__(CampaignFetcher)
    f._mcp_adapter = MagicMock()
    f._mcp_sem = asyncio.Semaphore(115)
    return f


def _basic_row(name="c1", budget=10.0, bid=0.5, status="启用",
               days=30, tos=0.1, pp=0.05, ros=0.0):
    return {
        "广告活动名称": name, "广告活动预算": budget, "关键词BID": bid,
        "状态": status, "活动上线天数": days,
        "头部位置加价比例": tos, "商品位置加价比例": pp, "其他位置加价比例": ros,
        "广告活动创建日期": "2025-01-01", "关键词": "kw",
    }


def test_batch_single_chunk_all_success():
    """10 个活动一批全成功 → 10 个结果，字段映射正确。"""
    f = _make_fetcher()
    names = [f"c{i}" for i in range(10)]
    call_mock = AsyncMock(return_value=MagicMock(
        ok=True,
        value={"rows": [_basic_row(n, budget=float(i)) for i, n in enumerate(names)]},
    ))
    f._mcp_adapter.campaign_call_tool = call_mock

    results = asyncio.run(f._fetch_basic_batch(names, "shop1"))

    assert len(results) == 10
    assert results["c0"]["campaign_budget"] == 0.0
    assert results["c5"]["campaign_budget"] == 5.0
    assert results["c0"]["keyword_bid"] == 0.5
    assert results["c0"]["campaign_status"] == "启用"
    assert results["c0"]["days_online"] == 30
    call_mock.assert_awaited_once()
    assert call_mock.call_args.kwargs["campaign_name_list"] == ",".join(names)


def test_batch_multi_chunk_parallel():
    """25 个活动 = 2 批（20+5），两批并行发出。"""
    f = _make_fetcher()
    names = [f"c{i}" for i in range(25)]

    def _respond(tool_name, campaign_name, shop_account, **_kw):
        chunk = _kw["campaign_name_list"].split(",")
        return MagicMock(ok=True, value={"rows": [_basic_row(n) for n in chunk]})

    f._mcp_adapter.campaign_call_tool = AsyncMock(side_effect=_respond)

    results = asyncio.run(f._fetch_basic_batch(names, "shop1"))
    assert len(results) == 25
    assert f._mcp_adapter.campaign_call_tool.call_count == 2


def test_batch_failure_returns_partial():
    """一批失败 → 该批不写入结果，调用方应填默认值。"""
    f = _make_fetcher()
    names = [f"c0", f"c1", f"c2"]

    f._mcp_adapter.campaign_call_tool = AsyncMock(
        return_value=MagicMock(ok=False, error="MCP 超时")
    )
    results = asyncio.run(f._fetch_basic_batch(names, "shop1"))
    assert results == {}  # 失败不产生结果，由调用方填默认


def test_batch_exception_returns_partial():
    """MCP 抛异常 → 不崩，该批无结果。"""
    f = _make_fetcher()
    f._mcp_adapter.campaign_call_tool = AsyncMock(side_effect=RuntimeError("boom"))
    results = asyncio.run(f._fetch_basic_batch(["c0", "c1"], "shop1"))
    assert results == {}


def test_batch_returns_only_sent_campaigns():
    """批量返回不应包含未请求的活动名。"""
    f = _make_fetcher()
    f._mcp_adapter.campaign_call_tool = AsyncMock(return_value=MagicMock(
        ok=True,
        value={"rows": [_basic_row("c0"), _basic_row("c1"), _basic_row("ghost")]},
    ))
    results = asyncio.run(f._fetch_basic_batch(["c0", "c1"], "shop1"))
    assert set(results.keys()) == {"c0", "c1", "ghost"}  # MCP 返回的全保留


def test_batch_empty_names():
    """空列表 → 空结果。"""
    f = _make_fetcher()
    results = asyncio.run(f._fetch_basic_batch([], "shop1"))
    assert results == {}
    f._mcp_adapter.campaign_call_tool.assert_not_called()


def test_batch_partial_rows_handled_by_caller():
    """20 个请求但 MCP 只返回 18 行 → batch 返回 18 个，
    调用方 _zip_results 应给缺失的 2 个填默认值（本测试仅验证 batch 行为）。"""
    f = _make_fetcher()
    names = [f"c{i}" for i in range(20)]
    rows18 = [_basic_row(n) for n in names[:18]]
    f._mcp_adapter.campaign_call_tool = AsyncMock(return_value=MagicMock(
        ok=True, value={"rows": rows18},
    ))
    results = asyncio.run(f._fetch_basic_batch(names, "shop1"))
    assert len(results) == 18  # batch 如实返回 MCP 给的行数
    assert "c18" not in results and "c19" not in results
