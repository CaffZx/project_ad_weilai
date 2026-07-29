"""回归：新建活动下发的子 ASIN 只复用库里 card.asin，不在执行期重查数仓。"""
from __future__ import annotations

from decimal import Decimal

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


def test_execution_layer_has_no_child_asin_warehouse_fallback():
    assert not hasattr(AE, "lookup_top_child_attrs")
    assert not hasattr(AE, "_fill_create_asin_fallback")


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


def test_immediate_exit_maps_loaded_confirmed_pending_shape():
    """Repository 已过滤后的立即退出 pending 统一复用现有 mapper。"""
    cards = [
        {
            "id": "card-broad",
            "suggest_category": "ADJUST",
            "campaign_id": "c-broad",
            "campaign_group_type": None,
        },
        {
            "id": "card-phrase",
            "suggest_category": "ADJUST",
            "campaign_id": "c-phrase",
            "campaign_group_type": None,
        },
        {
            "id": "card-auto",
            "suggest_category": "ADJUST",
            "campaign_id": "c-auto",
            "campaign_group_type": None,
        },
        {
            "id": "card-exact-single",
            "suggest_category": "ELIMINATE",
            "campaign_id": "c-exact-single",
            "campaign_group_type": "low_bid_retention_group",
        },
        {
            "id": "card-exact-multi",
            "suggest_category": "ADJUST",
            "campaign_id": "c-exact-multi",
            "campaign_group_type": "low_bid_retention_group",
        },
        {
            "id": "card-product",
            "suggest_category": "ADJUST",
            "campaign_id": "c-product",
            "campaign_group_type": "low_bid_retention_group",
        },
    ]
    campaign_pending = [
        {
            "id": f"cp-{kind}",
            "suggest_card_id": f"card-{kind}",
            "campaign_id": f"c-{kind}",
            "campaign_name": kind,
            "new_state": "paused",
            "new_budget": None,
            "confirm_status": "CONFIRMED",
            "execute_status": "PENDING",
        }
        for kind in ("broad", "phrase", "auto")
    ] + [
        {
            "id": f"cp-{kind}",
            "suggest_card_id": f"card-{kind}",
            "campaign_id": f"c-{kind}",
            "campaign_name": kind,
            "new_state": None,
            "new_budget": Decimal("1.00"),
            "confirm_status": "CONFIRMED",
            "execute_status": "PENDING",
        }
        for kind in ("exact-single", "exact-multi", "product")
    ]
    pending = {
        "decision": {
            "id": "dec-immediate",
            "shop_id": 1622,
            "parent_asin": "B0PARENT",
            "parent_seller_sku": "SKU-1",
        },
        "cards": cards,
        "campaign_pending": campaign_pending,
        "keyword_pending": [{
            "id": "kp-exact-single",
            "suggest_card_id": "card-exact-single",
            "campaign_id": "c-exact-single",
            "campaign_name": "exact-single",
            "keyword_id": "kw-1",
            "keyword_text": "red dress",
            "match_type": "EXACT",
            "new_bid": Decimal("0.20"),
            "confirm_status": "CONFIRMED",
            "execute_status": "PENDING",
        }],
        "placement_pending": [],
    }

    plan = build_exec_plan(pending, operator="operator-1")

    assert len(plan.params_vo_list) == 1
    params_vo = plan.params_vo_list[0]
    assert params_vo["shopId"] == 1622
    assert params_vo["parentAsin"] == "B0PARENT"
    assert params_vo["parentSellerSku"] == "SKU-1"
    assert params_vo["currentUserId"] == "operator-1"
    assert params_vo["decisionId"] == "dec-immediate"
    assert params_vo["agentVersion"] == "V2"

    campaign_vos = params_vo["campaignVoList"]
    assert len(campaign_vos) == 6
    assert len({row["campaignId"] for row in campaign_vos}) == 6
    by_campaign = {row["campaignId"]: row for row in campaign_vos}
    assert len(by_campaign) == 6
    for campaign_id in ("c-broad", "c-phrase", "c-auto"):
        assert by_campaign[campaign_id]["campaignState"] == "paused"
        assert set(by_campaign[campaign_id]) == {
            "campaignId", "campaignState",
        }
    for campaign_id in ("c-exact-single", "c-exact-multi", "c-product"):
        assert by_campaign[campaign_id]["campaignBudget"] == 1.0
        assert by_campaign[campaign_id]["campaignGroupType"] == (
            "low_bid_retention_group"
        )

    single = by_campaign["c-exact-single"]
    assert set(single) == {
        "campaignId",
        "campaignBudget",
        "campaignGroupType",
        "keywordShowVoList",
    }
    assert single["keywordShowVoList"] == [{
        "keyword": "red dress",
        "keywordId": "kw-1",
        "keywordBid": 0.2,
    }]
    assert set(by_campaign["c-exact-multi"]) == {
        "campaignId", "campaignBudget", "campaignGroupType",
    }
    assert set(by_campaign["c-product"]) == {
        "campaignId", "campaignBudget", "campaignGroupType",
    }
    assert all("targetShowVoList" not in row for row in by_campaign.values())
    assert plan.create_calls == []
    assert plan.negative_calls == []
    assert plan.warnings == []
    assert len(plan.ops) == 7
    assert {op["pending_id"] for op in plan.ops} == {
        *(f"cp-{kind}" for kind in (
            "broad", "phrase", "auto",
            "exact-single", "exact-multi", "product",
        )),
        "kp-exact-single",
    }
