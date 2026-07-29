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
from app.api import decision as decision_api
from app.models.campaign import CampaignAdjustmentItem, CampaignPerf, CampaignUnit
from app.persistence.erp_writer.advert_exec_mapper import (
    parse_batch_update_terminal,
)
from app.workflow.steps import advert_execution as AE
from app.workflow.steps import campaign as campaign_step
from app.workflow.steps.campaign_portfolio import (
    PORTFOLIO_BROAD,
    PORTFOLIO_ELIMINATE,
    PORTFOLIO_MAIN,
    PORTFOLIO_TEST,
    classify,
    find_portfolio_matches,
    find_portfolio_group_matches,
    target_group_code_if_current_mismatch,
)
from app.workflow.steps.portfolio_execution import _match_portfolio, _pf_field


def test_find_portfolio_matches_keeps_mcp_order_for_special_selection_policy():
    portfolios = [
        {"portfolioId": "pf-second", "portfolioName": "低价捡漏组-第二"},
        {"portfolioId": "pf-first", "portfolioName": "低价捡漏组-第一"},
        {"portfolioId": "pf-other", "portfolioName": "精准主力组"},
    ]

    matches = find_portfolio_matches("低价捡漏组", portfolios)

    assert [item["portfolioId"] for item in matches] == ["pf-second", "pf-first"]


def test_find_portfolio_group_matches_keeps_existing_budget_group_priority():
    """组合预算归类沿用旧顺序：主力 → 测试 → 广泛 → 低价。"""
    assert find_portfolio_group_matches("US-精准测试组-自动广泛组") == [
        PORTFOLIO_TEST,
        PORTFOLIO_BROAD,
    ]
    assert find_portfolio_group_matches("US-未知组合") == []


def test_target_group_code_only_when_current_portfolio_is_confirmed_mismatch():
    assert target_group_code_if_current_mismatch(
        "US-自动广泛组", PORTFOLIO_BROAD,
    ) == ""
    assert target_group_code_if_current_mismatch(
        "US-精准测试组", PORTFOLIO_BROAD,
    ) == "auto_broad_group"
    assert target_group_code_if_current_mismatch("", PORTFOLIO_BROAD) == ""


def test_reconcile_portfolio_targets_only_moves_broad_campaigns_outside_broad_group():
    existing_broad = CampaignAdjustmentItem(
        campaign_name="broad-existing",
        campaign_key="broad-existing-key",
        campaign_id="broad-existing-id",
        match_type="BROAD",
        action="adjust_bid",
        current_bid=0.8,
        proposed_bid=0.7,
    )
    existing_exact = CampaignAdjustmentItem(
        campaign_name="exact-legacy",
        campaign_key="exact-legacy-key",
        campaign_id="exact-legacy-id",
        match_type="EXACT",
        action="keep",
        target_campaign_group_type="exact_testing_group",
    )
    units = [
        CampaignUnit(
            campaign_name="broad-existing", campaign_key="broad-existing-key",
            campaign_id="broad-existing-id", child_asin="B0CHILD",
            keyword_text="broad kw", match_type="BROAD",
            current_portfolio_name="US-精准测试组",
        ),
        CampaignUnit(
            campaign_name="broad-already", campaign_key="broad-already-key",
            campaign_id="broad-already-id", child_asin="B0CHILD",
            keyword_text="already kw", match_type="PHRASE",
            current_portfolio_name="US-自动广泛组",
        ),
        CampaignUnit(
            campaign_name="exact-legacy", campaign_key="exact-legacy-key",
            campaign_id="exact-legacy-id", child_asin="B0CHILD",
            keyword_text="exact kw", match_type="EXACT",
            current_portfolio_name="US-精准主力组",
        ),
    ]
    excluded = [{
        "campaign_name": "broad-multi",
        "campaign_id": "broad-multi-id",
        "child_asin": "B0CHILD",
        "keyword_text": "多关键词活动",
        "match_type": "AUTO",
        "current_portfolio_name": "US-精准主力组",
    }]

    items = [existing_broad, existing_exact]
    campaign_step._reconcile_portfolio_targets(items, units, excluded)

    assert existing_broad.target_campaign_group_type == "auto_broad_group"
    assert existing_broad.proposed_bid == 0.7
    assert existing_exact.target_campaign_group_type == ""
    assert existing_exact.ai_portfolio_class == PORTFOLIO_MAIN
    added = [item for item in items if item.campaign_id == "broad-multi-id"]
    assert len(added) == 1
    assert added[0].action == "keep"
    assert added[0].target_campaign_group_type == "auto_broad_group"
    assert not any(item.campaign_id == "broad-already-id" for item in items)


def test_reconcile_portfolio_targets_does_not_use_clearance_permission_as_move_rule():
    """控制清货是护栏权限，不能单独授权精准活动迁入低价组。"""
    exact_to_eliminate = CampaignAdjustmentItem(
        campaign_name="exact-clearance",
        campaign_key="exact-clearance-key",
        campaign_id="exact-clearance-id",
        match_type="EXACT",
        action="eliminate_to_low_bid_pool",
    )
    exact_already_low_bid = CampaignAdjustmentItem(
        campaign_name="exact-already-low-bid",
        campaign_key="exact-already-low-bid-key",
        campaign_id="exact-already-low-bid-id",
        match_type="EXACT",
        action="eliminate_to_low_bid_pool",
    )
    broad_to_eliminate = CampaignAdjustmentItem(
        campaign_name="broad-clearance",
        campaign_key="broad-clearance-key",
        campaign_id="broad-clearance-id",
        match_type="BROAD",
        action="eliminate_to_low_bid_pool",
    )
    units = [
        CampaignUnit(
            campaign_name="exact-clearance", campaign_key="exact-clearance-key",
            campaign_id="exact-clearance-id", child_asin="B0CHILD",
            keyword_text="exact kw", match_type="EXACT",
            current_portfolio_name="US-精准主力组",
        ),
        CampaignUnit(
            campaign_name="exact-already-low-bid", campaign_key="exact-already-low-bid-key",
            campaign_id="exact-already-low-bid-id", child_asin="B0CHILD",
            keyword_text="exact low", match_type="EXACT",
            current_portfolio_name="US-低价捡漏组",
        ),
        CampaignUnit(
            campaign_name="broad-clearance", campaign_key="broad-clearance-key",
            campaign_id="broad-clearance-id", child_asin="B0CHILD",
            keyword_text="broad kw", match_type="BROAD",
            current_portfolio_name="US-精准主力组",
        ),
    ]

    campaign_step._reconcile_portfolio_targets(
        [exact_to_eliminate, exact_already_low_bid, broad_to_eliminate],
        units,
        [],
    )

    assert exact_to_eliminate.target_campaign_group_type == ""
    assert exact_already_low_bid.target_campaign_group_type == ""
    # 广泛/词组/自动始终归自动广泛组，不能因控制清货进入低价组。
    assert broad_to_eliminate.target_campaign_group_type == "auto_broad_group"


def test_parse_batch_update_terminal_maps_campaign_results():
    result = parse_batch_update_terminal([
        {
            "result": {
                "detailVoList": [{
                    "campaignResList": [
                        {
                            "campaignId": "camp-success",
                            "updateCampaignState": "success",
                        },
                        {
                            "campaignId": "camp-fail",
                            "updateCampaignState": "fail",
                            "errorMsg": "活动状态修改失败",
                        },
                        {
                            "campaignId": "camp-wait",
                            "updateCampaignState": "submitted",
                        },
                    ],
                }],
            },
        },
    ])

    assert result == {
        "camp-success": "SUCCESS",
        "camp-fail": "FAIL",
        "camp-wait": "IN_PROGRESS",
    }


@pytest.mark.parametrize(
    "raw",
    [
        [],
        {"errorMsg": "未找到相关记录"},
        [{"result": {}}],
        [{"result": {"detailVoList": []}}],
        [{
            "result": {
                "detailVoList": [{
                    "campaignResList": [{
                        "campaignId": "camp-1",
                        "updateCampaignState": "unknown",
                    }],
                }],
            },
        }],
    ],
)
def test_parse_batch_update_terminal_does_not_guess_missing_or_unknown(raw):
    result = parse_batch_update_terminal(raw)

    if raw and isinstance(raw, list) and "campaignResList" in str(raw):
        assert result == {"camp-1": "IN_PROGRESS"}
    else:
        assert result == {}


def test_parse_batch_update_terminal_error_message_is_explicit_failure():
    result = parse_batch_update_terminal({
        "result": {
            "detailVoList": [{
                "campaignResList": [{
                    "campaignId": "camp-1",
                    "updateCampaignMsg": "预算修改失败",
                }],
            }],
        },
    })

    assert result == {"camp-1": "FAIL"}


def test_parse_batch_update_terminal_error_message_overrides_success_state():
    result = parse_batch_update_terminal({
        "result": {
            "detailVoList": [{
                "campaignResList": [{
                    "campaignId": "camp-1",
                    "updateCampaignState": "success",
                    "errorMsg": "部分字段修改失败",
                }],
            }],
        },
    })

    assert result == {"camp-1": "FAIL"}


def test_parse_batch_update_terminal_success_message_is_not_failure():
    result = parse_batch_update_terminal({
        "result": {
            "detailVoList": [{
                "campaignResList": [{
                    "campaignId": "camp-1",
                    "updateCampaignState": "success",
                    "updateCampaignMsg": "修改成功",
                }],
            }],
        },
    })

    assert result == {"camp-1": "SUCCESS"}


@pytest.mark.parametrize("message_key", ["updateCampaignMsg", "errorMsg"])
def test_parse_batch_update_terminal_not_found_message_stays_in_progress(
    message_key,
):
    result = parse_batch_update_terminal({
        "result": {
            "detailVoList": [{
                "campaignResList": [{
                    "campaignId": "camp-1",
                    "updateCampaignState": "unknown",
                    message_key: "未找到相关记录",
                }],
            }],
        },
    })

    assert result == {"camp-1": "IN_PROGRESS"}


@pytest.mark.parametrize("message", ["no error", "0 failed", "无错误"])
def test_parse_batch_update_terminal_negated_error_stays_success(message):
    result = parse_batch_update_terminal({
        "result": {
            "detailVoList": [{
                "campaignResList": [{
                    "campaignId": "camp-1",
                    "updateCampaignState": "success",
                    "errorMsg": message,
                }],
            }],
        },
    })

    assert result == {"camp-1": "SUCCESS"}


def _terminal_snapshot() -> dict:
    return {
        "decision": {
            "id": "dec-1",
            "shop_id": 1622,
            "parent_asin": "B0PARENT",
            "parent_seller_sku": "SKU-1",
            "operating_mode": "IMMEDIATE_EXIT",
        },
        "cards": [
            {
                "id": "card-success",
                "campaign_id": "camp-success",
                "suggest_category": "ELIMINATE",
                "campaign_name": "exact-success",
                "campaign_key": "exact-success-key",
                "asin": "B0CHILD",
            },
            {
                "id": "card-fail",
                "campaign_id": "camp-fail",
                "suggest_category": "ELIMINATE",
                "campaign_name": "exact-fail",
            },
            {
                "id": "card-wait",
                "campaign_id": "camp-wait",
                "suggest_category": "ADJUST",
                "campaign_name": "broad-wait",
            },
        ],
        "campaign_pending": [
            {
                "suggest_card_id": "card-success",
                "campaign_id": "camp-success",
                "execute_status": "IN_PROGRESS",
            },
            {
                "suggest_card_id": "card-fail",
                "campaign_id": "camp-fail",
                "execute_status": "IN_PROGRESS",
            },
            {
                "suggest_card_id": "card-wait",
                "campaign_id": "camp-wait",
                "execute_status": "IN_PROGRESS",
            },
        ],
        "keyword_pending": [],
        "placement_pending": [],
    }


@patch.object(AE, "_get_repository")
@patch.object(AE, "AdvertMcpClient")
def test_poll_execution_result_without_erp_task_id_stays_in_progress(
    mock_client_cls,
    mock_repo,
):
    repo = MagicMock()
    repo.get_decision_basic.return_value = {
        "id": "dec-1",
        "is_latest": 1,
        "operating_mode": "IMMEDIATE_EXIT",
    }
    repo.list_execution_task_ids.return_value = []
    mock_repo.return_value = repo

    result = asyncio.run(AE.poll_execution_result(
        "dec-1",
        operator="42",
    ))

    assert result == {
        "ok": True,
        "decision_id": "dec-1",
        "execute_status": "IN_PROGRESS",
        "task_ids": [],
        "campaign_statuses": {},
    }
    repo.read_snapshot.assert_not_called()
    repo.update_campaign_terminal_status.assert_not_called()
    mock_client_cls.assert_not_called()


@patch.object(AE, "_get_repository")
@patch.object(AE, "AdvertMcpClient")
def test_poll_execution_result_updates_each_campaign_and_only_pools_success(
    mock_client_cls,
    mock_repo,
):
    repo = MagicMock()
    repo.get_decision_basic.return_value = {
        "id": "dec-1",
        "is_latest": 1,
        "operating_mode": "IMMEDIATE_EXIT",
    }
    repo.list_execution_task_ids.return_value = ["task-1"]
    repo.read_snapshot.return_value = _terminal_snapshot()
    mock_repo.return_value = repo
    client = MagicMock()
    client.batch_update_result = AsyncMock(return_value=[{
        "result": {
            "detailVoList": [{
                "campaignResList": [
                    {
                        "campaignId": "camp-success",
                        "updateCampaignState": "success",
                    },
                    {
                        "campaignId": "camp-fail",
                        "updateCampaignState": "fail",
                        "errorMsg": "修改失败",
                    },
                ],
            }],
        },
    }])
    client.aclose = AsyncMock()
    mock_client_cls.return_value = client

    result = asyncio.run(AE.poll_execution_result(
        "dec-1",
        operator="42",
    ))

    assert result == {
        "ok": True,
        "decision_id": "dec-1",
        "execute_status": "FAIL",
        "task_ids": ["task-1"],
        "campaign_statuses": {
            "camp-success": "SUCCESS",
            "camp-fail": "FAIL",
            "camp-wait": "IN_PROGRESS",
        },
    }
    repo.update_campaign_terminal_status.assert_called_once_with(
        "dec-1",
        {
            "camp-success": "SUCCESS",
            "camp-fail": "FAIL",
            "camp-wait": "IN_PROGRESS",
        },
        operator="42",
        message_by_campaign={"camp-fail": "修改失败"},
    )
    repo.upsert_pool_entry.assert_called_once()
    assert repo.upsert_pool_entry.call_args.kwargs["campaign_id"] == "camp-success"
    client.batch_update_result.assert_awaited_once_with(["task-1"])
    client.aclose.assert_awaited_once()


@patch.object(AE, "_get_repository")
@patch.object(AE, "AdvertMcpClient")
def test_poll_execution_result_not_found_retries_persisted_success_pool(
    mock_client_cls,
    mock_repo,
):
    snapshot = _terminal_snapshot()
    snapshot["campaign_pending"][0]["execute_status"] = "SUCCESS"
    repo = MagicMock()
    repo.get_decision_basic.return_value = {
        "id": "dec-1",
        "is_latest": 1,
        "operating_mode": "IMMEDIATE_EXIT",
    }
    repo.list_execution_task_ids.return_value = ["task-1"]
    repo.read_snapshot.return_value = snapshot
    mock_repo.return_value = repo
    client = MagicMock()
    client.batch_update_result = AsyncMock(
        return_value={"errorMsg": "未找到相关记录"}
    )
    client.aclose = AsyncMock()
    mock_client_cls.return_value = client

    result = asyncio.run(AE.poll_execution_result(
        "dec-1",
        operator="42",
    ))

    assert result["execute_status"] == "IN_PROGRESS"
    assert result["campaign_statuses"] == {
        "camp-success": "SUCCESS",
        "camp-fail": "IN_PROGRESS",
        "camp-wait": "IN_PROGRESS",
    }
    repo.upsert_pool_entry.assert_called_once()
    assert repo.upsert_pool_entry.call_args.kwargs["campaign_id"] == "camp-success"


@patch.object(AE, "_get_repository")
@patch.object(AE, "AdvertMcpClient")
def test_poll_execution_result_rejects_non_immediate_exit_decision(
    mock_client_cls,
    mock_repo,
):
    snapshot = _terminal_snapshot()
    snapshot["decision"]["operating_mode"] = "STABLE_OPERATION"
    repo = MagicMock()
    repo.get_decision_basic.return_value = {
        "id": "dec-1",
        "is_latest": 1,
        "operating_mode": "STABLE_OPERATION",
    }
    repo.list_execution_task_ids.return_value = ["task-1"]
    repo.read_snapshot.return_value = snapshot
    mock_repo.return_value = repo

    result = asyncio.run(AE.poll_execution_result(
        "dec-1",
        operator="42",
    ))

    assert result == {
        "ok": False,
        "decision_id": "dec-1",
        "execute_status": "IN_PROGRESS",
        "task_ids": [],
        "campaign_statuses": {},
        "error": "该批次不是立即退出决策，拒绝查询并回写",
    }
    mock_client_cls.assert_not_called()
    repo.list_execution_task_ids.assert_not_called()
    repo.read_snapshot.assert_not_called()
    repo.update_campaign_terminal_status.assert_not_called()


@patch.object(AE, "_get_repository")
@patch.object(AE, "AdvertMcpClient")
def test_poll_execution_result_rejects_non_immediate_before_task_id_lookup(
    mock_client_cls,
    mock_repo,
):
    repo = MagicMock()
    repo.get_decision_basic.return_value = {
        "id": "dec-1",
        "is_latest": 1,
        "operating_mode": "STABLE_OPERATION",
    }
    mock_repo.return_value = repo

    result = asyncio.run(AE.poll_execution_result(
        "dec-1",
        operator="42",
    ))

    assert result["ok"] is False
    assert "不是立即退出决策" in result["error"]
    repo.list_execution_task_ids.assert_not_called()
    repo.read_snapshot.assert_not_called()
    mock_client_cls.assert_not_called()


@patch.object(AE, "_get_repository")
@patch.object(AE, "AdvertMcpClient")
def test_poll_execution_result_rejects_non_latest_immediate_exit(
    mock_client_cls,
    mock_repo,
):
    repo = MagicMock()
    repo.get_decision_basic.return_value = {
        "id": "dec-old",
        "is_latest": 0,
        "operating_mode": "IMMEDIATE_EXIT",
    }
    mock_repo.return_value = repo

    result = asyncio.run(AE.poll_execution_result(
        "dec-old",
        operator="42",
    ))

    assert result == {
        "ok": False,
        "decision_id": "dec-old",
        "execute_status": "IN_PROGRESS",
        "task_ids": [],
        "campaign_statuses": {},
        "error": "该立即退出批次已不是最新批次，拒绝查询并回写",
    }
    repo.list_execution_task_ids.assert_not_called()
    repo.read_snapshot.assert_not_called()
    mock_client_cls.assert_not_called()


@patch.object(AE, "_get_repository")
@patch.object(AE, "AdvertMcpClient")
def test_poll_execution_result_existing_fail_blocks_later_success_pool(
    mock_client_cls,
    mock_repo,
):
    snapshot = _terminal_snapshot()
    snapshot["campaign_pending"][0]["execute_status"] = "FAIL"
    repo = MagicMock()
    repo.get_decision_basic.return_value = {
        "id": "dec-1",
        "is_latest": 1,
        "operating_mode": "IMMEDIATE_EXIT",
    }
    repo.list_execution_task_ids.return_value = ["task-1"]
    repo.read_snapshot.return_value = snapshot
    mock_repo.return_value = repo
    client = MagicMock()
    client.batch_update_result = AsyncMock(return_value=[{
        "result": {
            "detailVoList": [{
                "campaignResList": [{
                    "campaignId": "camp-success",
                    "updateCampaignState": "success",
                }],
            }],
        },
    }])
    client.aclose = AsyncMock()
    mock_client_cls.return_value = client

    result = asyncio.run(AE.poll_execution_result(
        "dec-1",
        operator="42",
    ))

    assert result["execute_status"] == "FAIL"
    assert result["campaign_statuses"]["camp-success"] == "FAIL"
    repo.upsert_pool_entry.assert_not_called()


@patch.object(AE, "_get_repository")
@patch.object(AE, "AdvertMcpClient")
def test_poll_execution_result_all_adjust_success_without_pool_write(
    mock_client_cls,
    mock_repo,
):
    snapshot = _terminal_snapshot()
    snapshot["cards"] = [snapshot["cards"][2]]
    snapshot["campaign_pending"] = [snapshot["campaign_pending"][2]]
    repo = MagicMock()
    repo.get_decision_basic.return_value = {
        "id": "dec-1",
        "is_latest": 1,
        "operating_mode": "IMMEDIATE_EXIT",
    }
    repo.list_execution_task_ids.return_value = ["task-1"]
    repo.read_snapshot.return_value = snapshot
    mock_repo.return_value = repo
    client = MagicMock()
    client.batch_update_result = AsyncMock(return_value=[{
        "result": {
            "detailVoList": [{
                "campaignResList": [{
                    "campaignId": "camp-wait",
                    "updateCampaignState": "success",
                }],
            }],
        },
    }])
    client.aclose = AsyncMock()
    mock_client_cls.return_value = client

    result = asyncio.run(AE.poll_execution_result(
        "dec-1",
        operator="42",
    ))

    assert result["execute_status"] == "SUCCESS"
    assert result["campaign_statuses"] == {"camp-wait": "SUCCESS"}
    repo.upsert_pool_entry.assert_not_called()


@patch.object(AE, "_get_repository")
@patch.object(AE, "AdvertMcpClient")
def test_poll_execution_result_stops_when_latest_changes_during_mcp(
    mock_client_cls,
    mock_repo,
):
    snapshot = _terminal_snapshot()
    repo = MagicMock()
    repo.get_decision_basic.return_value = {
        "id": "dec-1",
        "is_latest": 1,
        "operating_mode": "IMMEDIATE_EXIT",
    }
    repo.list_execution_task_ids.return_value = ["task-1"]
    repo.read_snapshot.return_value = snapshot
    repo.update_campaign_terminal_status.return_value = False
    mock_repo.return_value = repo
    client = MagicMock()
    client.batch_update_result = AsyncMock(return_value=[{
        "result": {
            "detailVoList": [{
                "campaignResList": [{
                    "campaignId": "camp-success",
                    "updateCampaignState": "success",
                }],
            }],
        },
    }])
    client.aclose = AsyncMock()
    mock_client_cls.return_value = client

    result = asyncio.run(AE.poll_execution_result(
        "dec-1",
        operator="42",
    ))

    assert result["ok"] is False
    assert result["execute_status"] == "IN_PROGRESS"
    assert "不再是最新批次" in result["error"]
    repo.upsert_pool_entry.assert_not_called()


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


def test_immediate_exit_low_bid_portfolio_not_queried_when_not_required():
    """没有精准/商品低价动作时，不占用组合查询 MCP。"""
    with patch.object(decision_api, "AdvertMcpClient") as client_cls:
        result = asyncio.run(
            decision_api._resolve_immediate_exit_low_bid_portfolio(
                identity={
                    "shop_id": 1,
                    "parent_seller_sku": "SKU",
                    "operator": "op",
                },
                asin="B0TEST",
                required=False,
            )
        )

    assert result == {}
    client_cls.assert_not_called()


@pytest.mark.parametrize(
    ("portfolios", "expected_id", "expected_count"),
    [
        ([], None, 0),
        (
            [{"name": "US-产品-低价捡漏组", "portfolioId": "p-1"}],
            "p-1",
            1,
        ),
        (
            [
                {"name": "US-产品A-低价捡漏组", "portfolioId": "p-first"},
                {"name": "US-产品B-低价捡漏组", "portfolioId": "p-second"},
            ],
            "p-first",
            2,
        ),
    ],
)
def test_immediate_exit_low_bid_portfolio_uses_first_raw_match(
    portfolios, expected_id, expected_count,
):
    """零命中返回空；多命中按已确认口径取 MCP 原始顺序第一条。"""
    client = MagicMock()
    client.query_portfolio_list = AsyncMock(return_value=portfolios)
    client.aclose = AsyncMock()

    with patch.object(decision_api, "AdvertMcpClient", return_value=client):
        result = asyncio.run(
            decision_api._resolve_immediate_exit_low_bid_portfolio(
                identity={
                    "shop_id": 1,
                    "parent_seller_sku": "SKU",
                    "operator": "op",
                },
                asin="B0TEST",
                required=True,
            )
        )

    client.query_portfolio_list.assert_awaited_once_with(
        1,
        "B0TEST",
        "SKU",
        portfolio_name_like="低价捡漏组",
        current_user_id="op",
    )
    client.aclose.assert_awaited_once()
    assert result.get("portfolio_id") == expected_id
    assert result["match_count"] == expected_count


def test_build_immediate_exit_run_uses_existing_card_and_pending_models():
    """一活动一卡；单词精准才写 keyword_pending，其余动作只写 campaign_pending。"""
    actions = [
        {
            "campaign_id": "c-1",
            "campaign_name": "exact-single",
            "action_kind": "LOW_BID_SINGLE_EXACT",
            "current_state": "enabled",
            "new_state": "enabled",
            "current_budget": decision_api.Decimal("10"),
            "new_budget": decision_api.Decimal("1.00"),
            "current_bid": decision_api.Decimal("0.5"),
            "new_bid": decision_api.Decimal("0.20"),
            "positive_keywords": [{
                "keyword_id": "k-1",
                "keyword_text": "red dress",
                "match_type": "EXACT",
            }],
        },
        {
            "campaign_id": "c-2",
            "campaign_name": "exact-multi",
            "action_kind": "LOW_BID_MULTI_EXACT",
            "current_state": "enabled",
            "new_state": "enabled",
            "current_budget": decision_api.Decimal("8"),
            "new_budget": decision_api.Decimal("1.00"),
            "current_bid": decision_api.Decimal("0.6"),
            "new_bid": None,
            "positive_keywords": [
                {"keyword_id": "k-2", "keyword_text": "blue", "match_type": "EXACT"},
                {"keyword_id": "k-3", "keyword_text": "green", "match_type": "EXACT"},
            ],
        },
        {
            "campaign_id": "c-3",
            "campaign_name": "product-target",
            "action_kind": "LOW_BID_PRODUCT_TARGET",
            "current_state": "enabled",
            "new_state": "enabled",
            "current_budget": decision_api.Decimal("7"),
            "new_budget": decision_api.Decimal("1.00"),
            "current_bid": decision_api.Decimal("0"),
            "new_bid": None,
            "positive_keywords": [],
        },
        {
            "campaign_id": "c-4",
            "campaign_name": "auto-one",
            "action_kind": "PAUSE",
            "current_state": "enabled",
            "new_state": "paused",
            "current_budget": decision_api.Decimal("6"),
            "new_budget": None,
            "current_bid": decision_api.Decimal("0"),
            "new_bid": None,
            "positive_keywords": [{
                "keyword_id": "",
                "keyword_text": "",
                "match_type": "AUTO",
            }],
        },
    ]

    run = decision_api._build_immediate_exit_run(
        asin="B0TEST",
        identity={
            "shop_id": 1622,
            "parent_seller_sku": "SKU-1",
            "site_code": "US",
            "run_id": "20260728T120000Z",
        },
        long_term={
            "product_level": "P1重点",
            "season_stage": "旺季",
            "operating_mode": "立即退出",
        },
        actions=actions,
        low_bid_portfolio={"portfolio_id": "p-1", "match_count": 1},
    )

    assert run.decision_id == decision_api.stable_id(
        "dec", "B0TEST", "20260728T120000Z", 1,
    )
    assert run.total_campaigns == 4
    assert len(run.cards) == 4
    assert sum(len(card.campaign_pending) for card in run.cards) == 4
    assert sum(len(card.keyword_pending) for card in run.cards) == 1
    by_campaign = {card.campaign_id: card for card in run.cards}
    assert by_campaign["c-1"].suggest_category == "ELIMINATE"
    assert by_campaign["c-1"].campaign_group_type == "low_bid_retention_group"
    assert by_campaign["c-1"].campaign_pending[0].target_campaign_group_type == (
        "low_bid_retention_group"
    )
    assert by_campaign["c-2"].campaign_pending[0].target_campaign_group_type == (
        "low_bid_retention_group"
    )
    assert by_campaign["c-3"].campaign_pending[0].target_campaign_group_type == (
        "low_bid_retention_group"
    )
    assert by_campaign["c-2"].suggest_category == "ADJUST"
    assert by_campaign["c-2"].keyword == "多关键词活动（2词）"
    assert by_campaign["c-3"].keyword_match_type == "PRODUCT_TARGETING"
    assert by_campaign["c-4"].campaign_pending[0].new_state == "paused"
    assert run.summary["to_eliminate"] == 1
    assert run.summary["to_adjust"] == 3
    assert run.decision_meta["operating_mode"] == "立即退出"


def test_build_immediate_exit_run_keeps_bid_and_budget_without_portfolio():
    """低价组零命中只影响挪组标记，不丢预算和单词 Bid pending。"""
    action = {
        "campaign_id": "c-1",
        "campaign_name": "exact-single",
        "action_kind": "LOW_BID_SINGLE_EXACT",
        "current_state": "enabled",
        "new_state": "enabled",
        "current_budget": decision_api.Decimal("10"),
        "new_budget": decision_api.Decimal("1.00"),
        "current_bid": decision_api.Decimal("0.5"),
        "new_bid": decision_api.Decimal("0.20"),
        "positive_keywords": [{
            "keyword_id": "k-1",
            "keyword_text": "red dress",
            "match_type": "EXACT",
        }],
    }
    run = decision_api._build_immediate_exit_run(
        asin="B0TEST",
        identity={
            "shop_id": 1622,
            "parent_seller_sku": "SKU-1",
            "site_code": "US",
            "run_id": "20260728T120000Z",
        },
        long_term={"operating_mode": "立即退出"},
        actions=[action],
        low_bid_portfolio={},
    )

    card = run.cards[0]
    assert card.suggest_category == "ADJUST"
    assert card.campaign_pending[0].new_budget == decision_api.Decimal("1.00")
    assert card.keyword_pending[0].new_bid == decision_api.Decimal("0.20")


def test_build_immediate_exit_run_rejects_single_exact_without_keyword_id():
    """CanonicalRun 构建层再次拒绝无 keywordId 的单词精准 Bid pending。"""
    action = {
        "campaign_id": "c-1",
        "campaign_name": "exact-single",
        "action_kind": "LOW_BID_SINGLE_EXACT",
        "current_state": "enabled",
        "new_state": "enabled",
        "current_budget": decision_api.Decimal("10"),
        "new_budget": decision_api.Decimal("1.00"),
        "current_bid": decision_api.Decimal("0.5"),
        "new_bid": decision_api.Decimal("0.20"),
        "positive_keywords": [{
            "keyword_id": "",
            "keyword_text": "red dress",
            "match_type": "EXACT",
        }],
    }

    with pytest.raises(RuntimeError, match="keywordId"):
        decision_api._build_immediate_exit_run(
            asin="B0TEST",
            identity={
                "shop_id": 1622,
                "parent_seller_sku": "SKU-1",
                "site_code": "US",
                "run_id": "20260728T120000Z",
            },
            long_term={"operating_mode": "立即退出"},
            actions=[action],
            low_bid_portfolio={"portfolio_id": "p-1", "match_count": 1},
        )


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


def test_classify_broad_low_budget_no_orders_stays_broad():
    """广泛活动即使零订单且预算触底，也只能归自动广泛组。"""
    cu = _cu("BROAD", bid=0.50, budget=1.00, orders=0)
    assert classify(cu, perf_7d_orders=cu.perf_7d.orders) == PORTFOLIO_BROAD


def test_classify_core_broad_low_budget_no_orders_stays_broad():
    """核心词受保护：即使预算触底且零订单，也不得归低价捡漏组。"""
    cu = _cu("BROAD", bid=0.25, budget=1.00, orders=0)
    assert classify(
        cu,
        llm_action="adjust_bid",
        perf_7d_orders=cu.perf_7d.orders,
        is_core=True,
    ) == PORTFOLIO_BROAD


def test_classify_phrase_low_bid_no_orders_stays_broad():
    """词组活动即使零订单且 Bid 触底，也只能归自动广泛组。"""
    cu = _cu("PHRASE", bid=0.10, budget=10.00, orders=0)
    assert classify(cu, perf_7d_orders=cu.perf_7d.orders) == PORTFOLIO_BROAD


def test_classify_broad_ignores_llm_elimination_action():
    """旧 LLM 的淘汰动作不得再决定广泛活动的目标组。"""
    cu = _cu("BROAD", bid=0.50, budget=10.00, orders=100)
    assert classify(cu, llm_action="eliminate_to_low_bid_pool",
                    perf_7d_orders=cu.perf_7d.orders) == PORTFOLIO_BROAD


def test_classify_exact_ignores_llm_elimination_action():
    """精准活动的淘汰/升降组交给新规则引擎，旧 LLM 动作不得消费。"""
    cu = _cu("EXACT", bid=1.00, budget=10.00, orders=10)
    assert classify(cu, llm_action="eliminate_to_low_bid_pool",
                    perf_7d_orders=cu.perf_7d.orders) == PORTFOLIO_MAIN


def test_classify_exact_high_budget_main():
    cu = _cu("EXACT", bid=1.00, budget=10.00, orders=10)
    assert classify(cu, perf_7d_orders=cu.perf_7d.orders) == PORTFOLIO_MAIN


def test_classify_exact_low_budget_test():
    cu = _cu("EXACT", bid=0.50, budget=3.00, orders=10)
    assert classify(cu, perf_7d_orders=cu.perf_7d.orders) == PORTFOLIO_TEST


def test_classify_broad_does_not_depend_on_perf_7d_orders():
    """广泛活动归组不再依赖旧的低价池订单判定。"""
    cu = _cu("BROAD", bid=0.50, budget=1.00, orders=5)
    assert classify(cu) == PORTFOLIO_BROAD


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


def test_resolve_modify_portfolios_uses_pre_resolved_id_without_query():
    plan = _FakePlan(params_vo_list=[{
        "shopId": 1,
        "campaignVoList": [{
            "campaignId": "c1",
            "campaignBudget": 1.0,
            "campaignGroupType": "low_bid_retention_group",
        }],
    }])
    plan.ops = [{"campaign_id": "c1", "campaign_name": "精准活动"}]
    client = _fake_client([])

    asyncio.run(AE._resolve_modify_portfolios(
        client,
        plan,
        shop_id=1,
        parent_asin="B0X",
        parent_sku="SKU",
        operator="op",
        resolved_portfolio_ids={"low_bid_retention_group": "pf-1"},
    ))

    client.query_portfolio_list.assert_not_awaited()
    assert plan.params_vo_list == [{
        "shopId": 1,
        "portfolioId": "pf-1",
        "campaignVoList": [{
            "campaignId": "c1",
            "campaignBudget": 1.0,
        }],
    }]
    assert plan.move_errors == []


def test_resolve_modify_portfolios_empty_pre_resolved_keeps_other_actions():
    plan = _FakePlan(params_vo_list=[{
        "shopId": 1,
        "campaignVoList": [{
            "campaignId": "c1",
            "campaignBudget": 1.0,
            "keywordShowVoList": [{
                "keywordId": "kw-1",
                "keywordBid": 0.2,
            }],
            "campaignGroupType": "low_bid_retention_group",
        }],
    }])
    plan.ops = [{"campaign_id": "c1", "campaign_name": "精准活动"}]
    client = _fake_client([])

    asyncio.run(AE._resolve_modify_portfolios(
        client,
        plan,
        shop_id=1,
        parent_asin="B0X",
        parent_sku="SKU",
        operator="op",
        resolved_portfolio_ids={},
    ))

    client.query_portfolio_list.assert_not_awaited()
    campaign_vo = plan.params_vo_list[0]["campaignVoList"][0]
    assert campaign_vo == {
        "campaignId": "c1",
        "campaignBudget": 1.0,
        "keywordShowVoList": [{
            "keywordId": "kw-1",
            "keywordBid": 0.2,
        }],
    }
    assert plan.move_errors == [{
        "campaign_id": "c1",
        "campaign_name": "精准活动",
        "group": "低价捡漏组",
        "reason": "不存在",
    }]


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
            "new_budget": 5.0, "target_campaign_group_type": group_code,
        }],
        "keyword_pending": [],
        "placement_pending": [],
    }


def _immediate_exit_pending() -> dict:
    return {
        "decision": {
            "id": "dec-immediate",
            "shop_id": 1622,
            "parent_asin": "B0X",
            "parent_seller_sku": "SKU",
        },
        "cards": [{
            "id": "card-1",
            "campaign_group_type": "low_bid_retention_group",
            "campaign_id": "cid-1",
            "campaign_name": "精准活动",
            "suggest_category": "ELIMINATE",
        }],
        "campaign_pending": [{
            "id": "cp-1",
            "suggest_card_id": "card-1",
            "campaign_id": "cid-1",
            "campaign_name": "精准活动",
            "new_budget": 1.0,
            "target_campaign_group_type": "low_bid_retention_group",
        }],
        "keyword_pending": [{
            "id": "kp-1",
            "suggest_card_id": "card-1",
            "campaign_id": "cid-1",
            "campaign_name": "精准活动",
            "keyword_id": "kw-1",
            "keyword_text": "red dress",
            "match_type": "EXACT",
            "new_bid": 0.2,
        }],
        "placement_pending": [],
    }


@patch.object(AE, "_sync_pool_entries_from_exec")
@patch.object(AE, "_get_repository")
@patch.object(AE, "AdvertMcpClient")
@patch("app.workflow.steps.advert_execution.settings")
def test_immediate_exit_submit_marks_in_progress_only_with_task_id(
    mock_settings,
    mock_client_cls,
    mock_repo,
    mock_sync_pool,
):
    mock_settings.advert_mcp_enabled = True
    mock_settings.advert_exec_dry_run = False
    mock_settings.campaign_negative_keyword_exec_enabled = False
    repo = MagicMock()
    repo.load_confirmed_pending.return_value = _immediate_exit_pending()
    repo.insert_advert_record.return_value = "record-1"
    repo.claim_pending_for_execution.return_value = 2
    mock_repo.return_value = repo
    client = MagicMock()
    client.async_batch_update = AsyncMock(return_value={
        "state": "success",
        "taskId": "task-1",
    })
    client.query_portfolio_list = AsyncMock()
    client.aclose = AsyncMock()
    mock_client_cls.return_value = client

    result = asyncio.run(AE.submit_execution(
        "dec-immediate",
        operator="operator-1",
        resolved_portfolio_ids={"low_bid_retention_group": "pf-1"},
        wait_for_terminal=True,
    ))

    repo.load_confirmed_pending.assert_called_once_with("dec-immediate")
    client.query_portfolio_list.assert_not_awaited()
    payload = client.async_batch_update.await_args.args[0]
    assert payload[0]["portfolioId"] == "pf-1"
    assert "campaignGroupType" not in payload[0]["campaignVoList"][0]
    assert result["ok"] is True
    assert result["task_ids"] == ["task-1"]
    assert result["execute_status"] == "IN_PROGRESS"
    assert {
        op["execute_status"] for op in repo.update_pending_execute_status.call_args.args[0]
    } == {"IN_PROGRESS"}
    mock_sync_pool.assert_not_called()
    repo.upsert_pool_entry.assert_not_called()


@patch.object(AE, "_sync_pool_entries_from_exec")
@patch.object(AE, "_get_repository")
@patch.object(AE, "AdvertMcpClient")
@patch("app.workflow.steps.advert_execution.settings")
def test_immediate_exit_submit_accepts_task_id_only_envelope(
    mock_settings,
    mock_client_cls,
    mock_repo,
    mock_sync_pool,
):
    """真实异步工具可仅返回 taskId；taskId 本身就是成功提交凭证。"""
    mock_settings.advert_mcp_enabled = True
    mock_settings.advert_exec_dry_run = False
    mock_settings.campaign_negative_keyword_exec_enabled = False
    repo = MagicMock()
    repo.load_confirmed_pending.return_value = _immediate_exit_pending()
    repo.insert_advert_record.return_value = "record-1"
    repo.claim_pending_for_execution.return_value = 2
    mock_repo.return_value = repo
    client = MagicMock()
    client.async_batch_update = AsyncMock(return_value={"taskId": "task-1"})
    client.query_portfolio_list = AsyncMock()
    client.aclose = AsyncMock()
    mock_client_cls.return_value = client

    result = asyncio.run(AE.submit_execution(
        "dec-immediate",
        operator="operator-1",
        resolved_portfolio_ids={},
        wait_for_terminal=True,
    ))

    assert result["ok"] is True
    assert result["task_ids"] == ["task-1"]
    assert result["execute_status"] == "IN_PROGRESS"
    mock_sync_pool.assert_not_called()


@patch.object(AE, "_get_repository")
@patch.object(AE, "AdvertMcpClient")
@patch("app.workflow.steps.advert_execution.settings")
def test_immediate_exit_submit_claim_lost_does_not_call_mcp(
    mock_settings,
    mock_client_cls,
    mock_repo,
):
    mock_settings.advert_mcp_enabled = True
    mock_settings.advert_exec_dry_run = False
    repo = MagicMock()
    repo.load_confirmed_pending.return_value = _immediate_exit_pending()
    repo.insert_advert_record.return_value = "record-1"
    repo.claim_pending_for_execution.return_value = 0
    mock_repo.return_value = repo

    result = asyncio.run(AE.submit_execution(
        "dec-immediate",
        operator="operator-1",
        resolved_portfolio_ids={},
        wait_for_terminal=True,
    ))

    mock_client_cls.assert_not_called()
    assert result == {
        "ok": True,
        "already_claimed": True,
        "task_ids": [],
        "execute_status": "IN_PROGRESS",
        "ops": 2,
        "warnings": [],
        "move_errors": [],
    }


@patch.object(AE, "_sync_pool_entries_from_exec")
@patch.object(AE, "_get_repository")
@patch.object(AE, "AdvertMcpClient")
@patch("app.workflow.steps.advert_execution.settings")
def test_immediate_exit_submit_close_error_does_not_hide_task_id(
    mock_settings,
    mock_client_cls,
    mock_repo,
    mock_sync_pool,
):
    mock_settings.advert_mcp_enabled = True
    mock_settings.advert_exec_dry_run = False
    mock_settings.campaign_negative_keyword_exec_enabled = False
    repo = MagicMock()
    repo.load_confirmed_pending.return_value = _immediate_exit_pending()
    repo.insert_advert_record.return_value = "record-1"
    repo.claim_pending_for_execution.return_value = 2
    mock_repo.return_value = repo
    client = MagicMock()
    client.async_batch_update = AsyncMock(return_value={"taskId": "task-1"})
    client.query_portfolio_list = AsyncMock()
    client.aclose = AsyncMock(side_effect=RuntimeError("close failed"))
    mock_client_cls.return_value = client

    result = asyncio.run(AE.submit_execution(
        "dec-immediate",
        operator="operator-1",
        resolved_portfolio_ids={},
        wait_for_terminal=True,
    ))

    assert result["ok"] is True
    assert result["task_ids"] == ["task-1"]
    repo.update_pending_execute_status.assert_called_once()
    mock_sync_pool.assert_not_called()


@pytest.mark.parametrize(
    ("response", "side_effect", "expected_error"),
    [
        ({"state": "success"}, None, "未返回 taskId"),
        ({"state": "fail", "errorMsg": "ERP rejected"}, None, "ERP rejected"),
        (None, RuntimeError("MCP boom"), "MCP boom"),
    ],
)
@patch.object(AE, "_sync_pool_entries_from_exec")
@patch.object(AE, "_get_repository")
@patch.object(AE, "AdvertMcpClient")
@patch("app.workflow.steps.advert_execution.settings")
def test_immediate_exit_submit_failure_marks_fail(
    mock_settings,
    mock_client_cls,
    mock_repo,
    mock_sync_pool,
    response,
    side_effect,
    expected_error,
):
    mock_settings.advert_mcp_enabled = True
    mock_settings.advert_exec_dry_run = False
    mock_settings.campaign_negative_keyword_exec_enabled = False
    repo = MagicMock()
    repo.load_confirmed_pending.return_value = _immediate_exit_pending()
    repo.insert_advert_record.return_value = "record-1"
    repo.claim_pending_for_execution.return_value = 2
    mock_repo.return_value = repo
    client = MagicMock()
    client.async_batch_update = AsyncMock(
        return_value=response,
        side_effect=side_effect,
    )
    client.query_portfolio_list = AsyncMock()
    client.aclose = AsyncMock()
    mock_client_cls.return_value = client

    result = asyncio.run(AE.submit_execution(
        "dec-immediate",
        operator="operator-1",
        resolved_portfolio_ids={},
        wait_for_terminal=True,
    ))

    assert result["ok"] is False
    assert result["execute_status"] == "FAIL"
    assert expected_error in result["error"]
    ops = repo.update_pending_execute_status.call_args.args[0]
    assert {op["execute_status"] for op in ops} == {"FAIL"}
    assert all(expected_error in op["error_msg"] for op in ops)
    mock_sync_pool.assert_not_called()


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
