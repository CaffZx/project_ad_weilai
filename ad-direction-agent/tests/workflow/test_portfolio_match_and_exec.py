"""portfolio 子串匹配 + classify perf_7d_orders 守卫 + dry_run move_errors 单测。

覆盖：
  - _match_portfolio: 0/1/2+ 命中 + 子串包含语义
  - classify: perf_7d_orders 阻止有出单活动被 OR 误归淘汰
  - _resolve_modify_portfolios: move_errors 产出
  - submit_execution dry_run: 调 portfolio 解析 + 返回 move_errors + 不调写 MCP
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.models.campaign import CampaignPerf, CampaignUnit
from app.workflow.steps import advert_execution as AE
from app.workflow.steps.campaign_portfolio import (
    PORTFOLIO_BROAD,
    PORTFOLIO_ELIMINATE,
    PORTFOLIO_MAIN,
    PORTFOLIO_TEST,
    classify,
)
from app.workflow.steps.portfolio_execution import _match_portfolio, _pf_field


# ═══════════════════════════════════════════════════════════════
# _match_portfolio
# ═══════════════════════════════════════════════════════════════

def _pf(name: str) -> dict:
    return {"name": name, "portfolioId": f"id-{name}"}


def test_match_unique():
    portfolios = [_pf("US-产品-精准主力组"), _pf("US-产品-自动广泛组")]
    pf, count = _match_portfolio("精准主力组", portfolios)
    assert count == 1
    assert pf is not None
    assert pf["name"] == "US-产品-精准主力组"


def test_match_zero():
    portfolios = [_pf("US-产品-自动广泛组"), _pf("US-产品-低价捡漏组")]
    pf, count = _match_portfolio("精准测试组", portfolios)
    assert count == 0
    assert pf is None


def test_match_ambiguous():
    portfolios = [
        _pf("US-产品-低价捡漏组"),
        _pf("US-产品2pc-低价捡漏组"),
    ]
    pf, count = _match_portfolio("低价捡漏组", portfolios)
    assert count == 2
    assert pf is None


def test_match_substring_contiguous_only():
    """子串必须连续——中间多了字不算命中。"""
    portfolios = [_pf("US-产品-自动广泛扩词组")]
    pf, count = _match_portfolio("自动广泛组", portfolios)
    assert count == 0  # "扩词"打断了"自动广泛组"


def test_match_empty_group_name():
    portfolios = [_pf("任意")]
    pf, count = _match_portfolio("", portfolios)
    assert count == 0
    assert pf is None


def test_match_empty_portfolio_name():
    portfolios = [_pf("")]
    pf, count = _match_portfolio("精准主力组", portfolios)
    assert count == 0


def test_match_prefix_suffix_ok():
    """前后缀不影响匹配。"""
    portfolios = [_pf("US-女士落肩露脐短袖2pc-精准测试组")]
    pf, count = _match_portfolio("精准测试组", portfolios)
    assert count == 1
    assert pf is not None


def test_match_multiple_fields_fallback():
    """_pf_field 回退: name → portfolioName 等。"""
    pf = {"portfolioName": "test-精准主力组"}
    nm = _pf_field(pf, "name", "portfolioName")
    assert nm == "test-精准主力组"


# ═══════════════════════════════════════════════════════════════
# classify + perf_7d_orders 守卫
# ═══════════════════════════════════════════════════════════════

def _cu(match_type: str = "BROAD", bid: float = 0.50, budget: float = 10.0,
        orders: int = 5) -> CampaignUnit:
    return CampaignUnit(
        campaign_name="test",
        campaign_key="test_key",
        child_asin="B0TEST",
        keyword_text="test kw",
        match_type=match_type,
        current_bid=bid,
        current_budget=budget,
        perf_7d=CampaignPerf(orders=orders),
    )


def test_classify_broad_low_budget_has_orders_not_eliminate():
    """BROAD + budget=$1 + 有出单 → 不归淘汰，归自动广泛组。"""
    cu = _cu("BROAD", bid=0.50, budget=1.00, orders=3)
    assert classify(cu, perf_7d_orders=cu.perf_7d.orders) == PORTFOLIO_BROAD


def test_classify_broad_low_bid_has_orders_not_eliminate():
    """BROAD + bid=$0.10 + 有出单 → 不归淘汰，归自动广泛组。"""
    cu = _cu("BROAD", bid=0.10, budget=10.00, orders=1)
    assert classify(cu, perf_7d_orders=cu.perf_7d.orders) == PORTFOLIO_BROAD


def test_classify_no_orders_and_low_budget_eliminate():
    """无出单 + budget=$1 → 归淘汰。"""
    cu = _cu("BROAD", bid=0.50, budget=1.00, orders=0)
    assert classify(cu, perf_7d_orders=cu.perf_7d.orders) == PORTFOLIO_ELIMINATE


def test_classify_core_broad_low_budget_no_orders_stays_broad():
    """核心词受保护：即使预算触底且零订单，也不得归低价捡漏组。"""
    cu = _cu("BROAD", bid=0.25, budget=1.00, orders=0)
    assert classify(
        cu,
        llm_action="adjust_bid",
        perf_7d_orders=cu.perf_7d.orders,
        is_core=True,
    ) == PORTFOLIO_BROAD


def test_classify_no_orders_and_low_bid_eliminate():
    """无出单 + bid=$0.10 → 归淘汰。"""
    cu = _cu("BROAD", bid=0.10, budget=10.00, orders=0)
    assert classify(cu, perf_7d_orders=cu.perf_7d.orders) == PORTFOLIO_ELIMINATE


def test_classify_llm_eliminate_overrides_orders():
    """LLM 判淘汰 → 无论出单多少都归淘汰。"""
    cu = _cu("BROAD", bid=0.50, budget=10.00, orders=100)
    assert classify(cu, llm_action="eliminate_to_low_bid_pool",
                    perf_7d_orders=cu.perf_7d.orders) == PORTFOLIO_ELIMINATE


def test_classify_exact_high_budget_main():
    cu = _cu("EXACT", bid=1.00, budget=10.00, orders=10)
    assert classify(cu, perf_7d_orders=cu.perf_7d.orders) == PORTFOLIO_MAIN


def test_classify_exact_low_budget_test():
    cu = _cu("EXACT", bid=0.50, budget=3.00, orders=10)
    assert classify(cu, perf_7d_orders=cu.perf_7d.orders) == PORTFOLIO_TEST


def test_classify_default_perf_7d_orders():
    """不传 perf_7d_orders 时默认 0，行为与改前兼容。"""
    cu = _cu("BROAD", bid=0.50, budget=1.00, orders=5)
    # 不传 → orders=0 → 归淘汰（与旧行为一致）
    assert classify(cu) == PORTFOLIO_ELIMINATE


# ═══════════════════════════════════════════════════════════════
# _resolve_modify_portfolios → move_errors
# ═══════════════════════════════════════════════════════════════

@dataclass
class _FakePlan:
    params_vo_list: list = field(default_factory=list)
    ops: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    move_errors: list = field(default_factory=list)
    create_calls: list = field(default_factory=list)
    negative_calls: list = field(default_factory=list)


def _fake_client(portfolios: list[dict]):
    c = MagicMock()
    c.query_portfolio_list = AsyncMock(return_value=portfolios)
    return c


def test_resolve_modify_portfolios_not_found():
    """portfolio 不存在 → move_errors 带 reason='不存在'。"""
    plan = _FakePlan(params_vo_list=[{
        "campaignVoList": [{"campaignId": "c1", "campaignGroupType": "exact_testing_group"}],
    }])
    plan.ops = [{"campaign_id": "c1", "campaign_name": "活动A"}]

    asyncio.run(AE._resolve_modify_portfolios(
        _fake_client([{"name": "其他组", "portfolioId": "99"}]),
        plan, shop_id=1, parent_asin="B0X", parent_sku="SKU", operator="op",
    ))
    assert len(plan.move_errors) == 1
    assert plan.move_errors[0]["reason"] == "不存在"
    assert plan.move_errors[0]["group"] == "精准测试组"
    assert plan.move_errors[0]["campaign_name"] == "活动A"


def test_resolve_modify_portfolios_ambiguous():
    """两个同名 portfolio → move_errors 带 reason='存在多个'。"""
    plan = _FakePlan(params_vo_list=[{
        "campaignVoList": [{"campaignId": "c1", "campaignGroupType": "low_bid_retention_group"}],
    }])
    plan.ops = [{"campaign_id": "c1", "campaign_name": "活动B"}]

    asyncio.run(AE._resolve_modify_portfolios(
        _fake_client([
            {"name": "US-产品-低价捡漏组", "portfolioId": "1"},
            {"name": "US-产品2pc-低价捡漏组", "portfolioId": "2"},
        ]),
        plan, shop_id=1, parent_asin="B0X", parent_sku="SKU", operator="op",
    ))
    assert len(plan.move_errors) == 1
    assert plan.move_errors[0]["reason"] == "存在多个"
    assert plan.move_errors[0]["group"] == "低价捡漏组"


def test_resolve_modify_portfolios_success_no_move_errors():
    """唯一命中 → 无 move_errors，portfolioId 注入。"""
    plan = _FakePlan(params_vo_list=[{
        "campaignVoList": [{"campaignId": "c1", "campaignGroupType": "exact_core_group"}],
    }])
    plan.ops = [{"campaign_id": "c1", "campaign_name": "活动C"}]

    asyncio.run(AE._resolve_modify_portfolios(
        _fake_client([{"name": "US-产品-精准主力组", "portfolioId": "888"}]),
        plan, shop_id=1, parent_asin="B0X", parent_sku="SKU", operator="op",
    ))
    assert len(plan.move_errors) == 0
    # portfolioId 注入到 params_vo_list
    assert plan.params_vo_list[0].get("portfolioId") == "888"


def test_resolve_modify_portfolios_no_group_type_skips():
    """无 campaignGroupType → 不参与匹配，直接归入 no_pid。"""
    plan = _FakePlan(params_vo_list=[{
        "campaignVoList": [{"campaignId": "c1"}],  # 无 campaignGroupType
    }])
    plan.ops = []
    asyncio.run(AE._resolve_modify_portfolios(
        _fake_client([]), plan, shop_id=1, parent_asin="B0X", parent_sku="SKU", operator="op",
    ))
    assert len(plan.move_errors) == 0


# ═══════════════════════════════════════════════════════════════
# submit_execution dry_run → 调 portfolio 解析 + 返回 move_errors
# ═══════════════════════════════════════════════════════════════

def _pending_with_group(group_code: str) -> dict:
    return {
        "decision": {
            "id": "dec-1", "shop_id": 1, "parent_asin": "B0X",
            "parent_seller_sku": "SKU",
        },
        "cards": [{
            "id": "c1", "campaign_group_type": group_code,
            "campaign_id": "cid-1", "campaign_name": "精准-测试活动",
            "suggest_category": "ADJUST",
        }],
        "campaign_pending": [{
            "id": "cp-1", "suggest_card_id": "c1",
            "campaign_id": "cid-1", "campaign_name": "精准-测试活动",
            "new_budget": 5.0,
        }],
        "keyword_pending": [],
        "placement_pending": [],
    }


@patch.object(AE, "_get_repository")
@patch.object(AE, "AdvertMcpClient")
@patch("app.workflow.steps.advert_execution.settings")
def test_dry_run_returns_move_errors(mock_settings, mock_client_cls, mock_repo):
    """dry_run 时 portfolio 不存在 → 返回 move_errors。"""
    mock_settings.advert_mcp_enabled = True
    mock_settings.advert_exec_dry_run = True

    # repo
    repo = MagicMock()
    repo.load_confirmed_pending.return_value = _pending_with_group("exact_testing_group")
    repo.insert_advert_record.return_value = "rec-1"
    mock_repo.return_value = repo

    # MCP client → portfolio 列表里没有精准测试组
    mock_client = MagicMock()
    mock_client.query_portfolio_list = AsyncMock(return_value=[
        {"name": "US-产品-精准主力组", "portfolioId": "111"},
    ])
    mock_client.aclose = AsyncMock()
    mock_client_cls.return_value = mock_client

    result = asyncio.run(AE.submit_execution("dec-1", operator="op"))

    assert result["ok"] is True
    assert result["dry_run"] is True
    assert len(result["move_errors"]) == 1
    assert result["move_errors"][0]["group"] == "精准测试组"
    assert result["move_errors"][0]["reason"] == "不存在"
    # 确认调了 query_portfolio_list（读）但没调写工具
    mock_client.query_portfolio_list.assert_awaited_once()
    mock_client.async_batch_update.assert_not_called()
    mock_client.create_portfolio_campaign.assert_not_called()
    mock_client.create_negative_keywords.assert_not_called()
    mock_client.aclose.assert_awaited_once()


@patch.object(AE, "_get_repository")
@patch.object(AE, "AdvertMcpClient")
@patch("app.workflow.steps.advert_execution.settings")
def test_dry_run_matching_success_no_move_errors(mock_settings, mock_client_cls, mock_repo):
    """dry_run 时 portfolio 唯一命中 → 无 move_errors。"""
    mock_settings.advert_mcp_enabled = True
    mock_settings.advert_exec_dry_run = True

    repo = MagicMock()
    repo.load_confirmed_pending.return_value = _pending_with_group("exact_core_group")
    repo.insert_advert_record.return_value = "rec-2"
    mock_repo.return_value = repo

    mock_client = MagicMock()
    mock_client.query_portfolio_list = AsyncMock(return_value=[
        {"name": "US-产品-精准主力组", "portfolioId": "111"},
    ])
    mock_client.aclose = AsyncMock()
    mock_client_cls.return_value = mock_client

    result = asyncio.run(AE.submit_execution("dec-1", operator="op"))

    assert result["ok"] is True
    assert result["dry_run"] is True
    assert len(result["move_errors"]) == 0


@patch.object(AE, "_get_repository")
@patch.object(AE, "AdvertMcpClient")
@patch("app.workflow.steps.advert_execution.settings")
def test_dry_run_disabled_no_portfolio_query(mock_settings, mock_client_cls, mock_repo):
    """advert_mcp_enabled=False → 直接拒绝，不创建 client。"""
    mock_settings.advert_mcp_enabled = False
    mock_settings.advert_exec_dry_run = True

    result = asyncio.run(AE.submit_execution("dec-1", operator="op"))

    assert result["ok"] is False
    assert "未启用" in result["error"]
    mock_client_cls.assert_not_called()


@patch.object(AE, "_get_repository")
@patch.object(AE, "AdvertMcpClient")
@patch("app.workflow.steps.advert_execution.settings")
def test_dry_run_portfolio_query_fails_move_errors_still_returned(
    mock_settings, mock_client_cls, mock_repo,
):
    """portfolio 查询抛异常 → fail-open，dry_run 仍正常返回（warnings 记错）。"""
    mock_settings.advert_mcp_enabled = True
    mock_settings.advert_exec_dry_run = True

    repo = MagicMock()
    repo.load_confirmed_pending.return_value = _pending_with_group("exact_core_group")
    repo.insert_advert_record.return_value = "rec-3"
    mock_repo.return_value = repo

    mock_client = MagicMock()
    mock_client.query_portfolio_list = AsyncMock(side_effect=RuntimeError("MCP boom"))
    mock_client.aclose = AsyncMock()
    mock_client_cls.return_value = mock_client

    result = asyncio.run(AE.submit_execution("dec-1", operator="op"))

    assert result["ok"] is True
    assert result["dry_run"] is True
    assert any("MCP boom" in w or "组合解析失败" in w for w in result["warnings"])
    mock_client.aclose.assert_awaited_once()
