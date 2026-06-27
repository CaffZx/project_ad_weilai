"""回归：新建活动下发的子 ASIN 应复用库里(card.asin)，不重查数仓。

bug：执行期用 lookup_top_child_attrs 重查数仓拿 child_asin 注入；数仓慢→超时→None
→ agent_create_portfolio_campaign「子ASIN不能为空」新建全败。
修复：build_exec_plan 优先用 card.asin；仅卡里没存才由 _fill_create_asin_fallback 兜底查。
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

from app.persistence.erp_writer.advert_exec_mapper import build_exec_plan
from app.workflow.steps import advert_execution as AE


def _pending(card_asin: str) -> dict:
    return {
        "decision": {"shop_id": 1, "parent_asin": "B0PARENT", "parent_seller_sku": "SKU"},
        "cards": [{
            "id": "c1", "suggest_category": "CREATE", "asin": card_asin,
            "campaign_name": "精准-x", "keyword_match_type": "EXACT",
        }],
        "campaign_pending": [], "keyword_pending": [], "placement_pending": [],
    }


def test_create_uses_card_asin_without_inject():
    # 卡里有 asin → 不传 child_asin 也能用上（不需重查数仓）
    plan = build_exec_plan(_pending("B0CHILD"), operator="op")
    assert len(plan.create_calls) == 1
    assert plan.create_calls[0]["asin"] == "B0CHILD"


def test_create_falls_back_to_injected_when_card_empty():
    plan = build_exec_plan(_pending(""), operator="op", child_asin="B0FALLBACK")
    assert plan.create_calls[0]["asin"] == "B0FALLBACK"


def test_create_no_asin_when_both_empty():
    # 卡空 + 不传 → 不设 asin，留给 _fill_create_asin_fallback 兜底
    plan = build_exec_plan(_pending(""), operator="op")
    assert "asin" not in plan.create_calls[0]


class _Plan:
    def __init__(self, calls): self.create_calls = calls


def test_fallback_fills_only_missing():
    plan = _Plan([{"asin": ""}, {"asin": "B0HAS"}])
    with patch.object(AE, "lookup_top_child_attrs", return_value={"asin": "B0FB"}):
        asyncio.run(AE._fill_create_asin_fallback(plan, {"decision": {"parent_asin": "B0P"}}))
    assert plan.create_calls[0]["asin"] == "B0FB"   # 缺的被兜底填
    assert plan.create_calls[1]["asin"] == "B0HAS"  # 有的不动


def test_fallback_skips_query_when_all_present():
    plan = _Plan([{"asin": "B0HAS1"}, {"asin": "B0HAS2"}])
    with patch.object(AE, "lookup_top_child_attrs") as m:
        asyncio.run(AE._fill_create_asin_fallback(plan, {"decision": {"parent_asin": "B0P"}}))
    m.assert_not_called()   # 卡都齐 → 完全不查数仓（核心：不重查）
