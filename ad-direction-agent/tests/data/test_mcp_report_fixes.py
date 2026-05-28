"""budget_params、关键词 match_type 合并、MCP 空表 partial_failures。"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.data.db_adapter import build_in_clause
from app.data.mcp_empty_reports import append_empty_report_failures, empty_report_tools
from app.data.doris_fallback import apply_doris_fallback
from app.data.mcp_keyword_report import (
    expand_planned_tools,
    merge_keyword_report_payloads,
    parse_keyword_match_types,
)
from app.data.mcp_mapping import McpContext
from app.models.asin_data import ASINData, AdData, KeywordData


def test_budget_params_match_budget_sql_placeholders():
    """budget_sql: shop_id, days, then N asin placeholders — no duplicate shop_id."""
    child_asins = ["A1", "A2", "A3"]
    shop_id, days = 42, 7
    _, in_params = build_in_clause("t.asin", child_asins)
    budget_params = (shop_id, days, *in_params)
    assert len(budget_params) == 2 + len(child_asins)
    assert budget_params[:2] == (shop_id, days)
    assert budget_params[2:] == tuple(child_asins)


def test_merge_keyword_report_payloads_dedupes_by_keyword():
    payloads = [
        [{"keyword_text": "dress", "clicks": 5}],
        [{"keyword": "dress", "clicks": 20, "cost": 1}],
        [{"keyword_text": "shoes", "clicks": 3}],
    ]
    merged = merge_keyword_report_payloads(payloads)
    by_kw = {r["keyword_text"]: r for r in merged}
    assert len(by_kw) == 2
    assert by_kw["dress"]["clicks"] == 20


def test_empty_report_tools_lists_tool_name():
    data = ASINData(asin="B0X", keyword_count=0, keywords=[])
    tools = empty_report_tools(
        data, {"ad_keyword_report": []}, missing_fields=[], meta_ids=["META_KW_AD"],
    )
    assert tools == ["ad_keyword_report"]


def test_append_empty_report_failures_when_payload_and_data_empty():
    data = ASINData(asin="B0X", keyword_count=0, keywords=[])
    payload_map = {"ad_keyword_report": []}
    flags = append_empty_report_failures(
        data,
        payload_map,
        missing_fields=[],
        meta_ids=["META_KW_AD"],
    )
    assert "mcp:ad_keyword_report:empty" in flags


def test_append_empty_report_skips_when_data_present():
    data = ASINData(
        asin="B0X",
        keyword_count=1,
        keywords=[{"keyword": "dress", "clicks": 10}],
    )
    payload_map = {"ad_keyword_report": []}
    flags = append_empty_report_failures(
        data,
        payload_map,
        missing_fields=[],
        meta_ids=["META_KW_AD"],
    )
    assert "mcp:ad_keyword_report:empty" not in flags


@pytest.mark.asyncio
async def test_apply_doris_fallback_on_empty_mcp_keyword_report():
    from unittest.mock import AsyncMock, patch

    mcp_data = ASINData(asin="B0X", keyword_count=0, keywords=[])
    db_data = ASINData(
        asin="B0X",
        keyword_count=1,
        keywords=[KeywordData(keyword="dress", clicks=5)],
    )
    with patch("app.data.db_adapter.DbAdapter") as mock_db:
        mock_db.return_value.fetch_asin_data = AsyncMock(return_value=db_data)
        out, pf = await apply_doris_fallback(
            asin="B0X",
            days=7,
            data=mcp_data,
            payload_map={"ad_keyword_report": []},
            missing_fields=[],
            meta_ids=["META_KW_AD"],
            partial_failures=["mcp:ad_keyword_report:empty"],
            failed_tools=[],
            full_scene_meta=False,
            timeout_seconds=60,
        )
    assert out.keyword_count == 1
    assert "mcp:ad_keyword_report:empty" not in pf


def test_expand_planned_tools_keyword_match_types():
    ctx = McpContext(
        parent_asin="B0",
        parent_seller_sku="SKU",
        shop_account="shop",
        site_code="US",
        start_date="2026-01-01",
        end_date="2026-01-07",
    )
    with patch("app.data.mcp_keyword_report.settings") as mock_settings:
        mock_settings.mcp_keyword_match_types = "EXACT, BROAD"
        types = parse_keyword_match_types()
        assert types == ["EXACT", "BROAD"]
        expanded = expand_planned_tools(["ad_keyword_report", "ad_product_report"], ctx)
        kw_calls = [e for e in expanded if e[0] == "ad_keyword_report"]
        assert len(kw_calls) == 2
        assert {c[1]["match_type"] for c in kw_calls} == {"EXACT", "BROAD"}
        assert sum(1 for e in expanded if e[0] == "ad_product_report") == 1
