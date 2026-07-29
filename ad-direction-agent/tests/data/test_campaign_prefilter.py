"""campaign_prefilter.filter_eliminated_pool 单元测试。
验证淘汰池预过滤的三输出（surviving / skipped / pool_units）判定正确。
"""

import pytest
from app.data.campaign_prefilter import filter_eliminated_pool
from app.models.campaign import CampaignUnit


def _unit(name: str = "", bid: float | None = 0.0, budget: float | None = 0.0,
          campaign_key: str = "", child_asin: str = "", match_type: str = "",
          keyword_text: str = "") -> CampaignUnit:
    return CampaignUnit(
        campaign_name=name,
        campaign_key=campaign_key or name,
        child_asin=child_asin,
        match_type=match_type,
        keyword_text=keyword_text,
        current_bid=bid,       # None 直传，不转为 0.0
        current_budget=budget,
    )


# ── 边界值判定 ──

def test_both_at_boundary_eliminated():
    """bid=$0.20 + budget=$1.00 → 淘汰池（边界值 AND 判定）"""
    units = [_unit("c1", bid=0.20, budget=1.00)]
    surviving, skipped, pool = filter_eliminated_pool(units)
    assert len(surviving) == 0
    assert len(skipped) == 1
    assert len(pool) == 1
    assert skipped[0]["reason"] == "已入淘汰池（出价≤$0.20 且 预算≤$1），请到ERP手动修改"
    assert skipped[0]["__prefiltered"] is True


def test_both_below_boundary_eliminated():
    """bid=$0.10 + budget=$0.50 → 淘汰池"""
    units = [_unit("c2", bid=0.10, budget=0.50)]
    surviving, skipped, pool = filter_eliminated_pool(units)
    assert len(surviving) == 0
    assert len(skipped) == 1
    assert len(pool) == 1


# ── 单条件不满足 → 不放行 ──

def test_bid_above_budget_at_boundary_not_eliminated():
    """bid=$0.21 > $0.20, budget=$1.00 → 不淘汰"""
    units = [_unit("c3", bid=0.21, budget=1.00)]
    surviving, skipped, pool = filter_eliminated_pool(units)
    assert len(surviving) == 1
    assert len(skipped) == 0
    assert len(pool) == 0


def test_bid_at_boundary_budget_above_not_eliminated():
    """bid=$0.20, budget=$1.01 > $1.00 → 不淘汰"""
    units = [_unit("c4", bid=0.20, budget=1.01)]
    surviving, skipped, pool = filter_eliminated_pool(units)
    assert len(surviving) == 1
    assert len(skipped) == 0
    assert len(pool) == 0


def test_both_above_not_eliminated():
    """bid=$0.30, budget=$5.00 → 不淘汰"""
    units = [_unit("c5", bid=0.30, budget=5.00)]
    surviving, skipped, pool = filter_eliminated_pool(units)
    assert len(surviving) == 1
    assert len(skipped) == 0
    assert len(pool) == 0


# ── 空列表 ──

def test_empty_list():
    surviving, skipped, pool = filter_eliminated_pool([])
    assert surviving == []
    assert skipped == []
    assert pool == []


# ── 混合列表 ──

def test_mixed_split_keeps_broad_low_bid_campaign_out_of_elimination_prefilter():
    units = [
        _unit("elim1", bid=0.20, budget=1.00, campaign_key="e1", child_asin="A1",
              match_type="EXACT", keyword_text="kw1"),
        _unit("keep1", bid=0.50, budget=10.00, campaign_key="k1"),
        _unit("elim2", bid=0.10, budget=0.80, campaign_key="e2", child_asin="A2",
              match_type="BROAD", keyword_text="kw2"),
        _unit("keep2", bid=0.30, budget=5.00, campaign_key="k2"),
    ]
    surviving, skipped, pool = filter_eliminated_pool(units)

    assert len(surviving) == 3
    assert len(skipped) == 1
    assert len(pool) == 1

    assert surviving[0].campaign_name == "keep1"
    assert surviving[1].campaign_name == "elim2"
    assert surviving[2].campaign_name == "keep2"

    assert skipped[0]["campaign_name"] == "elim1"
    assert skipped[0]["campaign_key"] == "e1"
    assert skipped[0]["child_asin"] == "A1"
    assert skipped[0]["match_type"] == "EXACT"
    assert skipped[0]["keyword_text"] == "kw1"
    assert pool[0].campaign_name == "elim1"


# ── skipped dict 字段完整性 ──

def test_phrase_low_bid_campaign_is_not_prefiltered_as_eliminated():
    units = [_unit("full", bid=0.20, budget=1.00, campaign_key="fk",
                    child_asin="B0XX", match_type="PHRASE", keyword_text="test kw")]
    surviving, skipped, pool = filter_eliminated_pool(units)
    assert surviving == units
    assert skipped == []
    assert pool == []
