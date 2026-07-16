"""回归：新建活动下发的子 ASIN 应复用库里(card.asin)，不重查数仓。

bug：执行期用 lookup_top_child_attrs 重查数仓拿 child_asin 注入；数仓慢→超时→None
→ agent_create_portfolio_campaign「子ASIN不能为空」新建全败。
修复：build_exec_plan 优先用 card.asin；仅卡里没存才由 _fill_create_asin_fallback 兜底查。
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import patch

from app.persistence.erp_writer.advert_exec_mapper import _num, build_exec_plan
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


def _adjust_pending(new_bid: float) -> dict:
    """模拟非 CREATE 的 adjust_bid 活动，含 keyword_pending。"""
    return {
        "decision": {"shop_id": 1, "parent_asin": "B0PARENT", "parent_seller_sku": "SKU"},
        "cards": [{
            "id": "c1", "suggest_category": "ADJUST",
            "campaign_id": "cid-1", "campaign_name": "广泛1-tank top sets",
            "keyword_match_type": "BROAD",
        }],
        "campaign_pending": [],
        "keyword_pending": [{
            "id": "kp-1", "suggest_card_id": "c1",
            "campaign_id": "cid-1", "campaign_name": "广泛1-tank top sets",
            "keyword_id": "kw-1", "keyword_text": "tank top sets",
            "match_type": "BROAD", "new_bid": new_bid, "old_bid": 0.45,
        }],
        "placement_pending": [],
    }


def test_create_uses_card_asin_without_inject():
    plan = build_exec_plan(_pending("B0CHILD"), operator="op")
    assert len(plan.create_calls) == 1
    assert plan.create_calls[0]["asin"] == "B0CHILD"


def test_create_falls_back_to_injected_when_card_empty():
    plan = build_exec_plan(_pending(""), operator="op", child_asin="B0FALLBACK")
    assert plan.create_calls[0]["asin"] == "B0FALLBACK"


def test_create_no_asin_when_both_empty():
    plan = build_exec_plan(_pending(""), operator="op")
    assert "asin" not in plan.create_calls[0]


class _Plan:
    def __init__(self, calls): self.create_calls = calls


def test_fallback_fills_only_missing():
    plan = _Plan([{"asin": ""}, {"asin": "B0HAS"}])
    with patch.object(AE, "lookup_top_child_attrs", return_value={"asin": "B0FB"}):
        asyncio.run(AE._fill_create_asin_fallback(plan, {"decision": {"parent_asin": "B0P"}}))
    assert plan.create_calls[0]["asin"] == "B0FB"
    assert plan.create_calls[1]["asin"] == "B0HAS"


def test_fallback_skips_query_when_all_present():
    plan = _Plan([{"asin": "B0HAS1"}, {"asin": "B0HAS2"}])
    with patch.object(AE, "lookup_top_child_attrs") as m:
        asyncio.run(AE._fill_create_asin_fallback(plan, {"decision": {"parent_asin": "B0P"}}))
    m.assert_not_called()


# ── bid 精度：DB Decimal → MCP keywordBid ──

def test_num_decimal_4dp_to_float_2dp():
    """MySQL DECIMAL(?,4) 存 0.3300 → _num() → float 0.33（不是 0.33000000000000003）。"""
    assert _num(Decimal("0.3300")) == 0.33


def test_num_decimal_3dp_to_float():
    """LLM 输出 0.335 → to_decimal → DB 存 0.3350 → _num() 仍是 0.335。"""
    assert _num(Decimal("0.3350")) == 0.335


def test_build_exec_plan_bid_float_preserved():
    """float 0.33 入 pending → build_exec_plan → MCP keywordBid = 0.33（不漂移）。"""
    plan = build_exec_plan(_adjust_pending(0.33), operator="op")
    assert len(plan.params_vo_list) == 1
    vos = plan.params_vo_list[0]["campaignVoList"]
    assert len(vos) == 1
    kw_vo = vos[0]["keywordShowVoList"][0]
    assert kw_vo["keywordBid"] == 0.33
    assert isinstance(kw_vo["keywordBid"], float)


def test_build_exec_plan_bid_decimal_preserved():
    """Decimal('0.3300')（MySQL 真实返回）→ keywordBid = 0.33。"""
    plan = build_exec_plan(_adjust_pending(Decimal("0.3300")), operator="op")
    kw_vo = plan.params_vo_list[0]["campaignVoList"][0]["keywordShowVoList"][0]
    assert kw_vo["keywordBid"] == 0.33


def test_build_exec_plan_bid_3dp_preserved():
    """LLM 输出 0.335 → pending → MCP keywordBid = 0.335（未被四舍五入到 2 位）。"""
    plan = build_exec_plan(_adjust_pending(0.335), operator="op")
    kw_vo = plan.params_vo_list[0]["campaignVoList"][0]["keywordShowVoList"][0]
    assert kw_vo["keywordBid"] == 0.335
