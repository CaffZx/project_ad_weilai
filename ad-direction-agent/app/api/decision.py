"""决策批次管理 API — context / new-event / cancel-event。

批次 = 一行 t_advert_agent_decision，冻结一份前置 1-4 快照 + 一份执行层结果。
进行中事件落 state 库 analysis_session；ERP decision 行只表示已完成快照。
执行权 = is_latest AND NOT exists(该 ASIN 的 analysis_session)。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, HTTPException

from app.api.product_identity import require_product_identity_dict
from app.config.settings import settings
from app.data.advert_mcp_client import AdvertMcpClient
from app.data.campaign_fetcher import CampaignFetcher
from app.models.layers import (
    AdPermission,
    OperatingMode,
    operating_mode_to_permission,
)
from app.persistence.erp_writer.mappers import _batch_no
from app.persistence.erp_writer.models import (
    CampaignPendingCanonical,
    CanonicalRun,
    KeywordPendingCanonical,
    SuggestCardCanonical,
    stable_id,
)
from app.persistence.state_factory import get_state_manager
from app.workflow.steps.advert_execution import (
    poll_execution_result,
    submit_execution,
)
from app.workflow.steps.portfolio_execution import (
    _normalize_portfolio_list,
    _pf_field,
)
from app.workflow.steps.campaign_portfolio import find_portfolio_matches

router = APIRouter()
logger = logging.getLogger(__name__)


def _repo():
    """延迟导入，避免启动时 ERP 不通炸模块加载。"""
    from app.persistence.erp_writer.repository import _get_repository
    return _get_repository()


def _validate_immediate_exit_request(
    req: dict,
    *,
    state,
) -> tuple[str, dict, dict]:
    """校验立即退出所需 URL 上下文及已保存经营模式，不触发任何下游调用。"""
    asin = str(req.get("asin") or "").strip()
    require_product_identity_dict(req, asin=asin)

    identity = {
        "shop_id": int(req.get("_shopId") or req.get("shopId") or 0),
        "parent_seller_sku": str(
            req.get("_parentSellerSku")
            or req.get("parent_seller_sku")
            or ""
        ).strip(),
        "shop_account": str(
            req.get("_shopAccount") or req.get("shopAccount") or ""
        ).strip(),
        "site_code": str(
            req.get("_siteCode") or req.get("siteCode") or ""
        ).strip(),
        "operator": str(
            req.get("_userId") or req.get("userId") or ""
        ).strip(),
        "run_id": str(req.get("run_id") or "").strip(),
    }
    missing = [key for key, value in identity.items() if not value]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"立即退出缺少必要上下文: {','.join(missing)}",
        )

    long_term = state.get_long_term_config(asin) or {}
    try:
        mode = OperatingMode(str(long_term.get("operating_mode") or "").strip())
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail="已保存经营模式无效，拒绝执行立即退出",
        ) from exc
    if mode is not OperatingMode.IMMEDIATE_EXIT:
        raise HTTPException(
            status_code=409,
            detail="已保存经营模式不是“立即退出”，拒绝执行",
        )
    if operating_mode_to_permission(mode) is not AdPermission.STOP:
        raise HTTPException(
            status_code=409,
            detail="经营模式未映射到停止权限，拒绝执行",
        )
    return asin, identity, long_term


def _immediate_exit_decision_id(asin: str, run_id: str) -> str:
    """同一分析事件稳定生成同一立即退出 decision_id。"""
    return stable_id("dec", asin, run_id, 1)


def _validate_existing_immediate_exit_decision(
    existing: dict,
    *,
    asin: str,
    identity: dict,
) -> None:
    """同 decision_id 只允许恢复同一产品身份，防止跨商品串批次。"""
    expected = {
        "parent_asin": asin,
        "shop_id": int(identity["shop_id"]),
        "parent_seller_sku": str(identity["parent_seller_sku"]),
    }
    actual = {
        "parent_asin": str(existing.get("parent_asin") or ""),
        "shop_id": int(existing.get("shop_id") or 0),
        "parent_seller_sku": str(existing.get("parent_seller_sku") or ""),
    }
    mismatched = [
        key for key, value in expected.items()
        if actual[key] != value
    ]
    if mismatched:
        raise HTTPException(
            status_code=409,
            detail=(
                "立即退出 decision_id 已属于其他产品身份: "
                + ",".join(mismatched)
            ),
        )
    existing_mode = str(existing.get("operating_mode") or "").strip()
    if existing_mode not in {
        OperatingMode.IMMEDIATE_EXIT.value,
        "IMMEDIATE_EXIT",
    }:
        raise HTTPException(
            status_code=409,
            detail="同 run_id 已存在非立即退出批次，拒绝恢复或自动确认",
        )


async def _fetch_immediate_exit_inputs(
    *,
    asin: str,
    identity: dict,
) -> dict:
    """只拉立即退出构造动作所需的三组广告数据。"""
    fetcher = CampaignFetcher()
    campaign_task = asyncio.create_task(
        fetcher._fetch_campaign_list(
            asin,
            identity["parent_seller_sku"],
            identity["shop_account"],
            strict=True,
        )
    )
    keyword_task = asyncio.create_task(
        fetcher._discover_context_from_mcp(
            asin,
            identity["shop_account"],
            identity["parent_seller_sku"],
            strict=True,
        )
    )
    campaign_name_to_id, keyword_rows = await asyncio.gather(
        campaign_task,
        keyword_task,
    )
    if not campaign_name_to_id:
        raise RuntimeError("ad_campaign_list 未返回任何活动")

    id_list = sorted(campaign_name_to_id.items())
    basic_by_name = await fetcher._fetch_basic_batch_v2(
        id_list,
        identity["shop_account"],
    )
    missing = [
        f"{name}({campaign_id})"
        for name, campaign_id in id_list
        if name not in basic_by_name
    ]
    if missing:
        raise RuntimeError(
            "ad_campaign_basic_info_v2 缺少活动: " + ",".join(missing)
        )
    return {
        "campaign_name_to_id": campaign_name_to_id,
        "keyword_rows": keyword_rows,
        "basic_by_name": basic_by_name,
    }


def _classify_immediate_exit_actions(inputs: dict) -> list[dict]:
    """把全部活动确定性归类为暂停或低价处理，不调用 LLM。"""

    def _decimal(value) -> Decimal:
        try:
            return Decimal(str(value or 0))
        except (InvalidOperation, ValueError, TypeError):
            return Decimal("0")

    campaign_name_to_id = inputs["campaign_name_to_id"]
    campaign_id_to_name = {
        campaign_id: campaign_name
        for campaign_name, campaign_id in campaign_name_to_id.items()
    }
    positive_by_campaign: dict[str, list[dict]] = {
        campaign_id: [] for campaign_id in campaign_id_to_name
    }
    seen_by_campaign: dict[str, set[tuple[str, ...]]] = {
        campaign_id: set() for campaign_id in campaign_id_to_name
    }

    for row in inputs.get("keyword_rows") or []:
        match_type = str(row.get("match_type") or "").strip().upper()
        if "NEGATIVE" in match_type:
            continue
        campaign_id = str(row.get("campaign_id") or "").strip()
        if not campaign_id:
            campaign_id = str(
                campaign_name_to_id.get(
                    str(row.get("campaign_name") or "").strip(),
                    "",
                )
            )
        if campaign_id not in positive_by_campaign:
            continue
        keyword_id = str(row.get("keyword_id") or "").strip()
        if keyword_id:
            dedupe_key = ("id", keyword_id)
        else:
            dedupe_key = (
                "text",
                str(row.get("keyword_text") or "").strip().casefold(),
                match_type,
            )
        if dedupe_key in seen_by_campaign[campaign_id]:
            continue
        seen_by_campaign[campaign_id].add(dedupe_key)
        normalized = dict(row)
        normalized["campaign_id"] = campaign_id
        normalized["campaign_name"] = campaign_id_to_name[campaign_id]
        normalized["match_type"] = match_type
        positive_by_campaign[campaign_id].append(normalized)

    actions: list[dict] = []
    for campaign_name, campaign_id in campaign_name_to_id.items():
        basic = inputs["basic_by_name"][campaign_name]
        positive_keywords = positive_by_campaign[campaign_id]
        match_types = {
            str(row.get("match_type") or "").strip().upper()
            for row in positive_keywords
        }
        current_budget = _decimal(basic.get("campaign_budget"))
        current_bid = _decimal(basic.get("keyword_bid"))
        current_state = str(basic.get("campaign_status") or "").strip()

        if not positive_keywords:
            action_kind = "LOW_BID_PRODUCT_TARGET"
        elif match_types == {"EXACT"}:
            action_kind = (
                "LOW_BID_SINGLE_EXACT"
                if len(positive_keywords) == 1
                else "LOW_BID_MULTI_EXACT"
            )
        else:
            action_kind = "PAUSE"

        is_pause = action_kind == "PAUSE"
        new_bid = None
        if action_kind == "LOW_BID_SINGLE_EXACT":
            if not str(positive_keywords[0].get("keyword_id") or "").strip():
                raise RuntimeError(
                    f"单词精准活动缺少 keywordId: {campaign_name}({campaign_id})"
                )
            if current_bid > 0:
                new_bid = min(current_bid, Decimal("0.20"))

        actions.append({
            "campaign_id": campaign_id,
            "campaign_name": campaign_name,
            "action_kind": action_kind,
            "current_state": current_state,
            "new_state": "paused" if is_pause else current_state,
            "current_budget": current_budget,
            "new_budget": (
                None
                if is_pause
                else Decimal("1.00")
            ),
            "current_bid": current_bid,
            "new_bid": new_bid,
            "positive_keywords": positive_keywords,
        })
    return actions


async def _resolve_immediate_exit_low_bid_portfolio(
    *,
    identity: dict,
    asin: str,
    required: bool,
) -> dict:
    """按立即退出特例查询低价捡漏组；多命中取 MCP 原始顺序第一条。"""
    if not required:
        return {}

    client = AdvertMcpClient()
    try:
        try:
            raw = await client.query_portfolio_list(
                identity["shop_id"],
                asin,
                identity["parent_seller_sku"],
                portfolio_name_like="低价捡漏组",
                current_user_id=identity["operator"],
            )
            portfolios = _normalize_portfolio_list(raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "立即退出低价捡漏组查询失败 [%s]: %s", asin, exc,
            )
            return {
                "match_count": 0,
                "query_error": f"{type(exc).__name__}: {exc}",
            }

        matches = find_portfolio_matches("低价捡漏组", portfolios)
        if not matches:
            return {"match_count": 0}

        selected = matches[0]
        portfolio_id = _pf_field(selected, "portfolioId", "portfolio_id", "id")
        result = {
            "match_count": len(matches),
            "portfolio_name": str(
                _pf_field(
                    selected,
                    "portfolioName",
                    "name",
                    "portfolio_name",
                )
                or ""
            ),
        }
        if portfolio_id not in (None, ""):
            result["portfolio_id"] = str(portfolio_id)
        return result
    finally:
        await client.aclose()


def _build_immediate_exit_run(
    *,
    asin: str,
    identity: dict,
    long_term: dict,
    actions: list[dict],
    low_bid_portfolio: dict,
) -> CanonicalRun:
    """用既有 CanonicalRun/card/pending 模型构造立即退出确定性批次。"""
    run_number = 1
    run_id = identity["run_id"]
    decision_id = _immediate_exit_decision_id(asin, run_id)
    has_low_bid_portfolio = bool(low_bid_portfolio.get("portfolio_id"))
    cards: list[SuggestCardCanonical] = []

    for sort_order, action in enumerate(actions, start=1):
        kind = action["action_kind"]
        is_pause = kind == "PAUSE"
        is_low_bid = kind.startswith("LOW_BID_")
        positive_keywords = action.get("positive_keywords") or []
        campaign_pending = CampaignPendingCanonical(
            old_state=action.get("current_state"),
            new_state=action.get("new_state") if is_pause else None,
            old_budget=action.get("current_budget"),
            new_budget=None if is_pause else action.get("new_budget"),
            target_campaign_group_type=(
                "low_bid_retention_group"
                if is_low_bid and has_low_bid_portfolio
                else None
            ),
        )
        keyword_pending: list[KeywordPendingCanonical] = []
        keyword = None
        keyword_match_type = None
        if kind == "LOW_BID_SINGLE_EXACT":
            source_keyword = positive_keywords[0]
            keyword_id = str(source_keyword.get("keyword_id") or "").strip()
            if not keyword_id:
                raise RuntimeError(
                    "单词精准活动缺少 keywordId: "
                    f"{action['campaign_name']}({action['campaign_id']})"
                )
            keyword = source_keyword.get("keyword_text")
            keyword_match_type = "EXACT"
            if action.get("new_bid") is not None:
                keyword_pending.append(KeywordPendingCanonical(
                    keyword_id=keyword_id,
                    keyword_text=keyword,
                    match_type="EXACT",
                    old_state=None,
                    new_state=None,
                    old_bid=action.get("current_bid"),
                    new_bid=action.get("new_bid"),
                ))
            description = (
                "立即退出：单词精准活动预算调整为 $1，"
                "有效 Bid 降至不高于 $0.20，并迁入低价捡漏组"
            )
        elif kind == "LOW_BID_MULTI_EXACT":
            keyword = f"多关键词活动（{len(positive_keywords)}词）"
            keyword_match_type = "EXACT"
            description = (
                "立即退出：多关键词精准活动预算调整为 $1，"
                "并迁入低价捡漏组；本期不调整关键词 Bid"
            )
        elif kind == "LOW_BID_PRODUCT_TARGET":
            keyword_match_type = "PRODUCT_TARGETING"
            description = (
                "立即退出：商品投放活动预算调整为 $1，"
                "并迁入低价捡漏组；本期不调整 target Bid"
            )
        else:
            if positive_keywords:
                keyword = positive_keywords[0].get("keyword_text")
                keyword_match_type = positive_keywords[0].get("match_type")
            description = "立即退出：停止投放，活动状态调整为 paused"

        can_mark_eliminate = (
            kind == "LOW_BID_SINGLE_EXACT"
            and action.get("new_bid") is not None
            and has_low_bid_portfolio
        )
        if is_low_bid and not has_low_bid_portfolio:
            description += "；当前未匹配到可用低价捡漏组，挪组暂不成立"

        cards.append(SuggestCardCanonical(
            card_id=stable_id("car", decision_id, action["campaign_id"]),
            decision_id=decision_id,
            campaign_id=action["campaign_id"],
            campaign_name=action["campaign_name"],
            asin=None,
            keyword=keyword,
            keyword_match_type=keyword_match_type,
            trigger_rule="OPERATING_MODE_IMMEDIATE_EXIT",
            suggest_category="ELIMINATE" if can_mark_eliminate else "ADJUST",
            confidence_level="high",
            campaign_group_type=(
                "low_bid_retention_group" if is_low_bid else None
            ),
            description=description,
            evidence="已保存经营模式=立即退出；由确定性规则生成",
            sort_order=sort_order,
            current_budget=action.get("current_budget"),
            proposed_budget=action.get("new_budget"),
            current_bid=action.get("current_bid"),
            proposed_bid=action.get("new_bid"),
            campaign_key=action["campaign_name"],
            review_level="AUTO_APPROVED",
            campaign_pending=[campaign_pending],
            keyword_pending=keyword_pending,
        ))

    eliminate_count = sum(
        1 for card in cards if card.suggest_category == "ELIMINATE"
    )
    warnings: list[str] = []
    if any(action["action_kind"].startswith("LOW_BID_") for action in actions):
        if low_bid_portfolio.get("query_error"):
            warnings.append(
                "低价捡漏组查询失败，预算/Bid pending 保留，挪组暂不成立"
            )
        elif not has_low_bid_portfolio:
            warnings.append(
                "未匹配到可用低价捡漏组，预算/Bid pending 保留，挪组暂不成立"
            )
        elif int(low_bid_portfolio.get("match_count") or 0) > 1:
            warnings.append("低价捡漏组多命中，按 MCP 原始顺序采用第一条")

    decision_meta = {
        "product_position": long_term.get("product_level"),
        "product_stage": long_term.get("product_stage"),
        "season_type": long_term.get("season_stage"),
        "operating_mode": long_term.get("operating_mode"),
        "site_code": identity.get("site_code"),
    }
    return CanonicalRun(
        decision_id=decision_id,
        batch_no=_batch_no(run_id, run_number),
        parent_asin=asin,
        experiment_id=run_id,
        run_number=run_number,
        timestamp=datetime.now(timezone.utc).replace(tzinfo=None),
        total_campaigns=len(cards),
        sanity_check_passed=True,
        summary={
            "to_eliminate": eliminate_count,
            "to_adjust": len(cards) - eliminate_count,
            "to_keep": 0,
            "to_reactivate": 0,
            "confidence_high": len(cards),
            "confidence_medium": 0,
            "confidence_low": 0,
        },
        cards=cards,
        legacy_details=[],
        metrics_rows=[],
        raw_payload={
            "warnings": warnings,
            "immediate_exit": {
                "low_bid_portfolio": low_bid_portfolio,
            },
        },
        shop_id=identity["shop_id"],
        parent_seller_sku=identity["parent_seller_sku"],
        site_code=identity.get("site_code"),
        decision_meta=decision_meta,
    )


def _precheck_immediate_exit_decision_sync(
    *,
    asin: str,
    identity: dict,
    repo,
    state,
    decision_id: str,
) -> dict | None:
    """在已持有 decisionId 互斥锁时，从既有数据库状态恢复。"""
    existing = repo.get_decision_basic(decision_id)
    if not isinstance(existing, dict):
        return None

    _validate_existing_immediate_exit_decision(
        existing,
        asin=asin,
        identity=identity,
    )
    snapshot = repo.read_snapshot(decision_id)
    if not snapshot:
        raise RuntimeError("立即退出既有 decision 缺少快照，无法安全恢复")
    if not existing.get("is_latest"):
        repo.finalize_batch(
            decision_id,
            asin,
            analysis_mode="REALTIME",
        )
    run_id = str(identity.get("run_id") or "").strip()
    cleared = state.clear_analysis_session_if_run(asin, run_id) if run_id else False
    if not cleared:
        raise RuntimeError("立即退出既有批次恢复时分析事件清除失败")

    pending_cards = [
        str(card.get("id") or "")
        for card in snapshot.get("cards") or []
        if card.get("confirm_status") == "PENDING" and card.get("id")
    ]
    confirm = None
    if pending_cards:
        confirm = repo.confirm_decisions(
            decision_id,
            [
                {"campaign_key": card_id, "decision": "approve"}
                for card_id in pending_cards
            ],
            operator=identity["operator"],
            in_progress=False,
        )
        if (
            not confirm.get("ok")
            or int(confirm.get("applied") or 0) != len(pending_cards)
        ):
            raise RuntimeError(f"立即退出既有批次自动确认失败: {confirm}")
        snapshot = repo.read_snapshot(decision_id)
        if not snapshot:
            raise RuntimeError("立即退出既有 decision 确认后快照读取失败")

    pending_rows = [
        row
        for key in (
            "campaign_pending",
            "keyword_pending",
            "placement_pending",
        )
        for row in (snapshot.get(key) or [])
        if row.get("confirm_status") == "CONFIRMED"
    ]
    execute_statuses = sorted({
        str(row.get("execute_status") or "PENDING")
        for row in pending_rows
    })
    needs_low_bid_portfolio = any(
        str(card.get("campaign_group_type") or "")
        == "low_bid_retention_group"
        for card in snapshot.get("cards") or []
    )
    return {
        "decision_id": decision_id,
        "resumed": True,
        "resume_execution": "PENDING" in execute_statuses,
        "needs_low_bid_portfolio": needs_low_bid_portfolio,
        "execute_statuses": execute_statuses,
        "confirm": confirm,
    }


def _resume_existing_immediate_exit_locked(
    *,
    asin: str,
    identity: dict,
    repo,
    state,
    decision_id: str,
) -> dict | None:
    with repo.immediate_exit_lock(decision_id):
        return _precheck_immediate_exit_decision_sync(
            asin=asin,
            identity=identity,
            repo=repo,
            state=state,
            decision_id=decision_id,
        )


async def _precheck_immediate_exit_decision(
    *,
    asin: str,
    identity: dict,
    repo,
    state,
    decision_id: str | None = None,
) -> dict | None:
    """拉数前快速查重；命中后在数据库锁内完成恢复。"""
    decision_id = decision_id or _immediate_exit_decision_id(
        asin,
        identity["run_id"],
    )
    existing = await asyncio.to_thread(
        repo.get_decision_basic,
        decision_id,
    )
    if not isinstance(existing, dict):
        return None
    return await asyncio.to_thread(
        _resume_existing_immediate_exit_locked,
        asin=asin,
        identity=identity,
        repo=repo,
        state=state,
        decision_id=decision_id,
    )


def _persist_and_confirm_immediate_exit_locked(
    *,
    run: CanonicalRun,
    identity: dict,
    repo,
    state,
) -> dict:
    """数据库锁内二次查重，并完成首次持久化生命周期。"""
    with repo.immediate_exit_lock(run.decision_id):
        existing_result = _precheck_immediate_exit_decision_sync(
            asin=run.parent_asin,
            identity=identity,
            repo=repo,
            state=state,
            decision_id=run.decision_id,
        )
        if existing_result is not None:
            return existing_result

        report = repo.write_immediate_exit(
            run,
            operator=identity["operator"],
        )
        repo.finalize_batch(
            run.decision_id,
            run.parent_asin,
            analysis_mode="REALTIME",
        )
        run_id = str(identity.get("run_id") or "").strip()
        cleared = state.clear_analysis_session_if_run(run.parent_asin, run_id) if run_id else False
        if not cleared:
            raise RuntimeError("立即退出批次已落库，但分析事件清除失败")

        decisions = [
            {"campaign_key": card.card_id, "decision": "approve"}
            for card in run.cards
        ]
        confirm = repo.confirm_decisions(
            run.decision_id,
            decisions,
            operator=identity["operator"],
            in_progress=False,
        )
        if not confirm.get("ok"):
            raise RuntimeError(
                f"立即退出自动确认失败: {confirm.get('error') or 'unknown'}"
            )
        if int(confirm.get("applied") or 0) != len(decisions):
            raise RuntimeError(
                "立即退出自动确认数量不一致: "
                f"expected={len(decisions)}, applied={confirm.get('applied') or 0}"
            )
        return {
            "decision_id": run.decision_id,
            "write_report": report.as_dict(),
            "confirm": confirm,
        }


async def _persist_and_confirm_immediate_exit(
    *,
    run: CanonicalRun,
    identity: dict,
    repo,
    state,
) -> dict:
    """先做无锁快速查重，再在数据库锁内二次查重并持久化。"""
    existing_result = await _precheck_immediate_exit_decision(
        asin=run.parent_asin,
        identity=identity,
        repo=repo,
        state=state,
        decision_id=run.decision_id,
    )
    if existing_result is not None:
        return existing_result
    return await asyncio.to_thread(
        _persist_and_confirm_immediate_exit_locked,
        run=run,
        identity=identity,
        repo=repo,
        state=state,
    )


def _immediate_exit_portfolio_ids(low_bid_portfolio: dict) -> dict[str, str]:
    portfolio_id = str(low_bid_portfolio.get("portfolio_id") or "").strip()
    if not portfolio_id:
        return {}
    return {"low_bid_retention_group": portfolio_id}


def _immediate_exit_execution_response(
    decision_id: str,
    execution: dict,
) -> dict:
    if not execution.get("ok"):
        return {
            "ok": False,
            "decision_id": decision_id,
            "task_ids": execution.get("task_ids") or [],
            "execute_status": execution.get("execute_status") or "FAIL",
            "error": execution.get("error") or "广告执行提交失败",
            "move_errors": execution.get("move_errors") or [],
        }
    if execution.get("dry_run"):
        return {
            "ok": False,
            "decision_id": decision_id,
            "task_ids": [],
            "execute_status": "DRY_RUN",
            "error": "立即退出当前仅完成 dry-run，未真实提交广告执行",
            "move_errors": execution.get("move_errors") or [],
        }
    if execution.get("already_claimed"):
        return {
            "ok": True,
            "decision_id": decision_id,
            "resumed": True,
            "task_ids": [],
            "execute_status": "IN_PROGRESS",
            "move_errors": execution.get("move_errors") or [],
        }
    task_ids = execution.get("task_ids") or []
    execute_status = execution.get("execute_status")
    if not task_ids or execute_status != "IN_PROGRESS":
        return {
            "ok": False,
            "decision_id": decision_id,
            "task_ids": task_ids,
            "execute_status": execute_status or "FAIL",
            "error": execution.get("error") or "广告执行未返回 taskId",
            "move_errors": execution.get("move_errors") or [],
        }
    return {
        "ok": True,
        "decision_id": decision_id,
        "task_ids": task_ids,
        "execute_status": execute_status,
        "move_errors": execution.get("move_errors") or [],
    }


async def _run_immediate_exit(
    *,
    asin: str,
    identity: dict,
    long_term: dict,
    state,
) -> dict:
    """按稳定 decisionId 恢复或创建立即退出批次，再从数据库 pending 执行。"""
    repo = _repo()
    decision_id = _immediate_exit_decision_id(asin, identity["run_id"])
    existing = await _precheck_immediate_exit_decision(
        asin=asin,
        identity=identity,
        repo=repo,
        state=state,
        decision_id=decision_id,
    )
    if existing is not None and not existing.get("resume_execution"):
        statuses = existing.get("execute_statuses") or []
        execute_status = (
            "FAIL"
            if "FAIL" in statuses
            else ("IN_PROGRESS" if "IN_PROGRESS" in statuses else (
                "SUCCESS" if statuses and set(statuses) == {"SUCCESS"}
                else "PENDING"
            ))
        )
        return {
            "ok": execute_status != "FAIL",
            "decision_id": decision_id,
            "resumed": True,
            "task_ids": [],
            "execute_status": execute_status,
            "execute_statuses": statuses,
            **(
                {"error": "立即退出广告执行已失败"}
                if execute_status == "FAIL"
                else {}
            ),
        }

    if existing is not None:
        low_bid_portfolio = await _resolve_immediate_exit_low_bid_portfolio(
            identity=identity,
            asin=asin,
            required=bool(existing.get("needs_low_bid_portfolio")),
        )
    else:
        inputs = await _fetch_immediate_exit_inputs(
            asin=asin,
            identity=identity,
        )
        actions = _classify_immediate_exit_actions(inputs)
        low_bid_required = any(
            str(action.get("action_kind") or "").startswith("LOW_BID_")
            for action in actions
        )
        low_bid_portfolio = await _resolve_immediate_exit_low_bid_portfolio(
            identity=identity,
            asin=asin,
            required=low_bid_required,
        )
        run = _build_immediate_exit_run(
            asin=asin,
            identity=identity,
            long_term=long_term,
            actions=actions,
            low_bid_portfolio=low_bid_portfolio,
        )
        persisted = await _persist_and_confirm_immediate_exit(
            run=run,
            identity=identity,
            repo=repo,
            state=state,
        )
        decision_id = run.decision_id
        if persisted.get("resumed") and not persisted.get("resume_execution"):
            statuses = persisted.get("execute_statuses") or []
            return {
                "ok": "FAIL" not in statuses,
                "decision_id": decision_id,
                "resumed": True,
                "task_ids": [],
                "execute_status": (
                    "FAIL"
                    if "FAIL" in statuses
                    else (
                        "IN_PROGRESS"
                        if "IN_PROGRESS" in statuses
                        else "SUCCESS"
                    )
                ),
                "execute_statuses": statuses,
                **(
                    {"error": "立即退出广告执行已失败"}
                    if "FAIL" in statuses
                    else {}
                ),
            }

    execution = await submit_execution(
        decision_id,
        operator=identity["operator"],
        resolved_portfolio_ids=_immediate_exit_portfolio_ids(
            low_bid_portfolio
        ),
        wait_for_terminal=True,
    )
    return _immediate_exit_execution_response(decision_id, execution)


@router.post("/decision/immediate-exit")
async def immediate_exit(req: dict):
    """保存后的“立即退出”确定性入口：不调用 LLM，提交后等待终态查询。"""
    state = get_state_manager()
    asin, identity, long_term = _validate_immediate_exit_request(
        req,
        state=state,
    )
    return await _run_immediate_exit(
        asin=asin,
        identity=identity,
        long_term=long_term,
        state=state,
    )


@router.post("/decision/immediate-exit/status")
async def immediate_exit_status(req: dict):
    """查询立即退出广告执行终态，不发起任何分析或新执行。"""
    decision_id = str(req.get("decision_id") or "").strip()
    operator = str(
        req.get("_userId") or req.get("userId") or ""
    ).strip()
    if not decision_id:
        return {"ok": False, "error": "decision_id 必填"}
    return await poll_execution_result(
        decision_id,
        operator=operator or "system",
    )


# ── GET /decision/context ────────────────────────────────────────────────


@router.get("/decision/context")
async def decision_context(asin: str = "", shopId: str = ""):
    """返回该 ASIN 的批次列表 + 页面三态判定。

    Response:
      { has_config, in_progress, latest_completed_id, latest_completed_updated_at,
        batches: [{decision_id, label, analysis_mode, completed_at, is_latest, executable}],
        degraded?: true }
    DB 不通时返回 degraded=true，不抛 500。
    """
    if not asin:
        return {"has_config": False, "in_progress": None, "latest_completed_id": None,
                "latest_completed_updated_at": None, "batches": [], "degraded": False}

    try:
        # 1. 进行中事件标记（state 库，run_id 句柄；不污染 ERP 库）
        state = get_state_manager()
        sess = state.get_analysis_session(asin)
        in_progress = sess.get("run_id") if sess else None
        execution_started_at = sess.get("execution_started_at") if sess else None
        cancel_requested_at = sess.get("cancel_requested_at") if sess else None
        cancelling = bool(cancel_requested_at)

        # 2. ERP DB 已完成批次列表（容错: DB 不通不崩）
        #    has_config 以 ERP 历史记录为准：有已完成批次即视为已配置（与 state 库无关）。
        batches = []
        latest_updated = None
        try:
            repo = _repo()
            batches = repo.list_batches(asin) if repo else []
            if batches:
                latest = next((b for b in batches if b.get("is_latest")), None)
                if latest:
                    latest_updated = latest.get("completed_at")
        except Exception as e:
            logger.warning("ERP DB 连不上，批次查询降级: %s", e)
            return {
                "has_config": False,
                "in_progress": in_progress,
                "cancelling": cancelling,
                "execution_started_at": execution_started_at,
                "execution_running": bool(execution_started_at),
                "latest_completed_id": None,
                "latest_completed_updated_at": None,
                "batches": [],
                "degraded": True,
            }

        has_config = len(batches) > 0

        # 4. 计算 executable = 最新批次 且 无进行中事件
        for b in batches:
            b["executable"] = bool(b.get("is_latest")) and not in_progress

        latest_id = None
        if batches:
            latest = next((b for b in batches if b.get("is_latest")), None)
            if latest:
                latest_id = latest.get("decision_id")

        return {
            "has_config": has_config,
            "in_progress": in_progress,
            "cancelling": cancelling,
            "cancel_requested_at": cancel_requested_at,
            "execution_started_at": execution_started_at,
            "execution_running": bool(execution_started_at),
            "latest_completed_id": latest_id,
            "latest_completed_updated_at": latest_updated,
            "batches": batches,
            "degraded": False,
        }
    except Exception as e:
        logger.exception("decision/context 异常 [%s]: %s", asin, e)
        return {"has_config": False, "in_progress": None, "latest_completed_id": None,
                "latest_completed_updated_at": None, "batches": [], "degraded": True}


# ── POST /decision/new-event ─────────────────────────────────────────────


@router.post("/decision/new-event")
async def new_decision_event(req: dict):
    """新建分析事件 → 在 state 库标记进行中（run_id 作批次句柄）。

    不在 ERP 库预建 decision 行——decision_id 由执行层 write_full 落库时生成。
    清空 p3 缓存 + 广告方向(execution)；acos/预算 override 保留继承（仅"保存"覆盖、
    "取消覆盖"删除，与预算行为对齐，2026-06-18 ③），保留 1-2 继承。
    返回: { run_id, analysis_mode }
    """
    asin = str(req.get("asin", "")).strip()
    if not asin:
        return {"ok": False, "error": "asin 必填"}

    require_product_identity_dict(req)
    state = get_state_manager()
    analysis_mode = str(req.get("analysis_mode", "REALTIME")).upper() or "REALTIME"
    shop_id = req.get("_shopId") or req.get("shopId") or req.get("shop_id")
    parent_seller_sku = (
        req.get("_parentSellerSku")
        or req.get("parentSellerSku")
        or req.get("parent_seller_sku")
    )
    # 幂等：仅当前未取消 session 复用；已取消 run 已由取消端点释放，不阻塞新事件。
    existing = state.get_analysis_session(asin)
    if existing and existing.get("run_id"):
        if existing.get("cancel_requested_at"):
            return {"ok": False, "error": "旧分析正在取消，请等待退出后重试",
                    "run_id": existing["run_id"], "cancelling": True}
        logger.info("复用现存进行中事件 [%s] run_id=%s", asin, existing["run_id"])
        return {"ok": True, "run_id": existing["run_id"],
                "analysis_mode": analysis_mode, "reused": True}

    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    if not state.set_analysis_session(
        asin,
        run_id,
        shop_id=shop_id,
        parent_seller_sku=parent_seller_sku,
    ):
        return {"ok": False, "error": "标记进行中事件失败"}

    # 清 p3 缓存 + 广告方向；acos/预算 override 保留继承（保存覆盖/取消覆盖删除），保留 1-2
    try:
        if hasattr(state, "clear_p3_recommendation"):
            state.clear_p3_recommendation(asin)
        wf = state.get_workflow_state(asin) or {}
        execution = dict(wf.get("execution") or {})
        execution["selected_directions"] = None
        wf["execution"] = execution
        if shop_id:
            wf["shop_id"] = shop_id
        if parent_seller_sku:
            wf["parent_seller_sku"] = parent_seller_sku
        state.set_workflow_state(asin, wf)
    except Exception as e:
        logger.warning("清 3-4 失败 [%s]: %s (非阻塞)", asin, e)

    logger.info("新建事件 [%s] run_id=%s mode=%s", asin, run_id, analysis_mode)
    return {"ok": True, "run_id": run_id, "analysis_mode": analysis_mode}


# ── POST /decision/cancel-event ──────────────────────────────────────────


@router.post("/decision/cancel-event")
async def cancel_decision_event(req: dict):
    """放弃进行中分析事件：写取消墓碑并立即释放 session，允许立即新建事件。"""
    asin = str(req.get("asin", "")).strip()
    if not asin:
        return {"ok": False, "error": "asin 必填"}
    try:
        state = get_state_manager()
        session = state.get_analysis_session(asin)
        if not session:
            logger.info("取消事件 幂等 [%s] 无进行中 session", asin)
            return {"ok": True, "run_id": "", "status": "FINISHED"}
        run_id = session["run_id"]
        cancelled = state.cancel_and_release_analysis_session(asin, run_id)
        if not cancelled:
            logger.warning("取消事件 未匹配 [%s] run_id=%s（session 已切换？）", asin, run_id)
            return {"ok": False, "error": "分析事件已切换，请刷新后重试"}
        logger.info("分析事件已取消并释放 [%s] run_id=%s", asin, run_id)
        return {"ok": True, "run_id": run_id, "status": "FINISHED"}
    except Exception as e:
        logger.exception("取消事件失败 [%s]: %s", asin, e)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ── GET /decision/session-status ─────────────────────────────────────────


@router.get("/decision/session-status")
async def session_status(asin: str, run_id: str):
    """查询指定 run 的当前状态（RUNNING / CANCELLING / FINISHED）。"""
    state = get_state_manager()
    session = state.get_analysis_session(asin)
    if not session:
        return {"ok": True, "asin": asin, "run_id": run_id, "status": "FINISHED", "active": False}
    if session.get("run_id") != run_id:
        return {"ok": True, "asin": asin, "run_id": run_id, "status": "FINISHED", "active": False}
    if session.get("cancel_requested_at"):
        return {"ok": True, "asin": asin, "run_id": run_id, "status": "CANCELLING", "active": True}
    return {"ok": True, "asin": asin, "run_id": run_id, "status": "RUNNING", "active": True}


# ── GET /decision/{id}/preset ────────────────────────────────────────────
# 前置 1-4 配置快照（ERP 码 → 中文 label，供左侧只读渲染）

_POSITION_ZH = {
    "P0_PRODUCT": "战略级产品 (P0)", "P1_PRODUCT": "重点产品 (P1)",
    "P2_PRODUCT": "常规产品 (P2)", "P3_PRODUCT": "长尾产品 (P3)",
    # 旧 3 级英文码兼容（decision.product_position 历史值 TOP/WAIST/LONG_TAIL）
    "TOP": "战略级产品 (P0)", "WAIST": "常规产品 (P2)", "LONG_TAIL": "长尾产品 (P3)",
}
_STAGE_ZH = {
    "TESTING": "测试期", "PROMOTING": "推进期", "HARVEST_PROFIT": "收割利润期",
    "MAINTAINING": "维持期", "LIQUIDATING": "清货期",
}
_SEASON_ZH = {
    "OFF_SEASON": "淡季", "PEAK_SEASON_PREPARE": "旺季准备",
    "BIG_PEAK_SEASON": "大旺季", "LATE_PEAK_SEASON": "旺季末期",
}
_OPERATING_MODE_ZH = {
    "IMMEDIATE_EXIT": "立即退出",
    "CONTROLLED_CLEARANCE": "控制清货",
    "LIMITED_REPAIR": "限时修复",
    "STABLE_OPERATION": "稳定经营",
    "ACTIVE_PROMOTION": "积极推进",
    "PROFIT_HARVEST": "获取利润",
}
_PURPOSE_ZH = {"TRAFFIC": "引流型", "CONVERSION": "转化型", "RANKING": "排名型", "PROFIT": "盈利型"}
_KWTYPE_ZH = {"GENERIC": "大词", "LONG_TAIL": "长尾词", "COMPETITOR": "竞品词",
              "BRAND": "品牌词", "CUSTOM": "自定义"}
_DIRECTION_ZH = {"PUSH_NATURAL": "推进自然位", "EXPAND_KEYWORDS": "新增扩词",
                 "OPTIMIZE_ACOS": "优化ACOS", "BALANCE_MAINTAIN": "平衡维持"}
_DAYRANGE = {"DAY_7": 7, "DAY_14": 14, "DAY_30": 30}


def _json_list(v) -> list:
    if not v:
        return []
    if isinstance(v, list):
        return v
    s = str(v).strip()
    if s.startswith("["):
        try:
            parsed = json.loads(s)
            return parsed if isinstance(parsed, list) else [s]
        except json.JSONDecodeError:
            pass
    return [x.strip() for x in s.replace("，", ",").split(",") if x.strip()]


def _translate_preset(row: dict) -> dict:
    return {
        "decision_id": row.get("id"),
        "parent_asin": row.get("parent_asin"),
        "analysis_mode": row.get("analysis_mode"),
        "is_latest": bool(row.get("is_latest")),
        "days": _DAYRANGE.get(row.get("day_range"), 7),
        "strategy": {
            "product_level": _POSITION_ZH.get(row.get("product_position"), row.get("product_position")),
            "product_stage": _STAGE_ZH.get(row.get("product_stage"), row.get("product_stage")),
            "season_stage": _SEASON_ZH.get(row.get("season_type"), row.get("season_type")),
            "operating_mode": _OPERATING_MODE_ZH.get(row.get("operating_mode"), row.get("operating_mode")),
        },
        "tactics": {
            "ad_purposes": [_PURPOSE_ZH.get(x, x) for x in _json_list(row.get("advert_purposes"))],
            "target_keyword_strategy": [_KWTYPE_ZH.get(x, x) for x in _json_list(row.get("target_keyword_types"))],
        },
        "p3": {
            "target_acos": row.get("target_acos_suggest"),
            "daily_budget": float(row["daily_budget_suggest"])
            if row.get("daily_budget_suggest") is not None else None,
        },
        "directions": [_DIRECTION_ZH.get(x, x) for x in _json_list(row.get("advert_direction_types"))],
    }


# ── 富快照反译（Tab1 评分卡 / Tab3 ACOS·预算推理），与实时渲染器同形 ──────────
# purpose_score.advert_purpose(ERP 码) → renderTargetScores 期望的 target；
# renderTargetScores 内部 targetMap[s.target]||s.target，给中文也能正确回退显示。
_PURPOSE_TARGET = {"TRAFFIC": "Traffic", "CONVERSION": "Conversion",
                   "RANKING": "Ranking", "PROFIT": "Profit"}
# recommend_tag(direction_detail/ai_suggest) → renderP3 的 confidence 着色（纯视觉）
_TAG_CONF = {"RECOMMENDED": "high", "OPTIONAL": "medium", "NOT_RECOMMENDED": "low"}


def _join_reason(basis, suggest, future) -> str:
    """split_reason_sections 的逆操作：三段带标记拼回，供前端 formatAiSections 展示。"""
    parts = []
    if basis:
        parts.append("【决策依据】" + str(basis))
    if suggest:
        parts.append("【建议】" + str(suggest))
    if future:
        parts.append("【后续关注】" + str(future))
    return "\n".join(parts)


def _translate_tactics_scores(rows: list) -> list:
    """purpose_score 行 → renderTargetScores(scores) 入参（Tab1 评分卡）。"""
    out = []
    for r in rows or []:
        code = (r.get("advert_purpose") or "").upper()
        out.append({
            "target": _PURPOSE_TARGET.get(code, _PURPOSE_ZH.get(code, code)),
            "level": r.get("recommend_level") or "",
            "score": r.get("score"),
            "reason": _join_reason(r.get("decision_basis"), r.get("suggest"),
                                   r.get("future_attention")),
        })
    return out


def _translate_p3_recommend(ai: dict | None) -> dict | None:
    """ai_suggest 行 → renderP3(data) 入参（Tab3）。manual_override=False 走净分支，
    不设 llm_status/data_completeness → isLlmBlocked=False、不弹 dataStatus banner。
    budget 的 current/direction/magnitude 是实时分析态、快照未存 → 降级（前端显示 —）。"""
    if not ai:
        return None
    acos = ai.get("suggest_acos")
    budget = ai.get("suggest_budget")
    risk = ai.get("risk_warning")
    return {
        "target_acos": {
            "recommended_target": float(acos) if acos is not None else None,
            "confidence": _TAG_CONF.get((ai.get("suggest_acos_tag") or "").upper(), "medium"),
            "reasoning": _join_reason(ai.get("suggest_acos_decision_basis"),
                                      ai.get("suggest_acos_suggest"),
                                      ai.get("suggest_acos_future_attention")),
            "manual_override": False,
        },
        "budget_bid": {
            "suggested": float(budget) if budget is not None else None,
            "current": None, "direction": "", "magnitude_pct": "",
            "reason": _join_reason(ai.get("suggest_budget_decision_basis"),
                                   ai.get("suggest_budget_suggest"),
                                   ai.get("suggest_budget_future_attention")),
            "manual_override": False,
        },
        "overall_reasoning": ai.get("comprehensive_judgment") or "",
        "risk_warnings": [risk] if risk else [],
        "from_cache": False,
    }


# ── Tab4 方向卡反译（direction_recommend_detail → renderDirCard 入参，同形零映射）──


def _content_lines_to_reason(v) -> str:
    """content_json（JSON 字符串数组）→ 用「；」拼回 reason，供前端 formatDirReason 按「；」拆。"""
    if not v:
        return ""
    items = v
    if isinstance(v, str):
        try:
            items = json.loads(v)
        except json.JSONDecodeError:
            items = [v]
    if not isinstance(items, list):
        items = [str(items)]
    return "；".join(str(x).strip() for x in items if str(x).strip())


def _translate_p4_directions(rows: list) -> list:
    """direction_recommend_detail 行 → renderDirCard(d) 入参（Tab4 方向卡）。

    direction_type(ERP码) → id(小写)+label(中文)；recommend_tag(库存小写) → suitability 直用；
    suggest_score → suitability_score；content_json(数组) → reason(；拼)；recommended 由 suitability 派生。
    """
    out = []
    for r in rows or []:
        code = (r.get("direction_type") or "").strip()
        suit = (r.get("recommend_tag") or "available").strip().lower()
        out.append({
            "id": code.lower(),
            "label": _DIRECTION_ZH.get(code, code),
            "suitability": suit,
            "suitability_score": r.get("suggest_score"),
            "reason": _content_lines_to_reason(r.get("content_json")),
            "recommended": suit == "recommended",
        })
    return out


def _translate_p4_summary(main_row: dict | None) -> str:
    """direction_recommend.conclusion_json → recommendation_summary 文本（总述，可空）。"""
    if not main_row or not main_row.get("conclusion_json"):
        return ""
    raw = main_row["conclusion_json"]
    try:
        obj = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return ""
    if isinstance(obj, dict):
        oa = obj.get("overall_analysis")
        if isinstance(oa, list):
            return "\n".join(str(x).strip() for x in oa if str(x).strip())
        if isinstance(oa, str):
            return oa
    return ""


# ── Tab1 核心关键词监控反译（core_keyword_tracking → renderKeywordTable 入参）──
# keyword_type(ERP码) → 前端 Broad/Long-tail 等键（与 renderKeywordTable 的 stCnMap 键对齐）。
_KWTYPE_KEY = {"GENERIC": "Broad", "LONG_TAIL": "Long-tail", "COMPETITOR": "Competitor",
               "BRAND": "Brand", "CUSTOM": "Custom"}


def _rank_to_int(v):
    """nature_rank(库 varchar) → 正整数排名；非正/不可解析 → None（前端显示未入榜）。"""
    try:
        n = int(float(str(v).strip()))
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def _change_to_int(v):
    """nature_rank_change(库 varchar) → 带符号整数（含负/0）；不可解析 → None。"""
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return None


def _translate_core_keywords(rows: list) -> list:
    """core_keyword_tracking 行 → renderKeywordTable(kws) 入参（Tab1 关键词监控表）。

    near_rank/rank_change_7d(周变化) 未落库 → 缺省，前端降级显示「—」。
    """
    out = []
    for r in rows or []:
        word = r.get("keyword")
        if not word:
            continue
        code = (r.get("keyword_type") or "").upper()
        out.append({
            "word": word,
            "rank": _rank_to_int(r.get("nature_rank")),
            "rank_change": _change_to_int(r.get("nature_rank_change")),
            "keyword_class": _KWTYPE_KEY.get(code, ""),
            "action": r.get("suggest") or "",
        })
    return out


@router.get("/decision/{decision_id}/preset")
async def decision_preset(decision_id: str):
    """读取某批次冻结的前置 1-4 快照（只读展示用）。

    config 标签（strategy/tactics/p3/directions）+ 富段（tactics_scores=Tab1 评分卡、
    p3_recommend=Tab3 ACOS/预算推理、directions_rich=Tab4 方向卡，与实时渲染器同形）。
    富段缺失时为空/None，前端降级。
    """
    if not decision_id:
        return {"ok": False, "error": "decision_id 必填"}
    try:
        repo = _repo()
        snap = await asyncio.to_thread(repo.read_decision_preset_rich, decision_id) if repo else None
        if not snap or not snap.get("decision"):
            return {"ok": False, "error": "批次不存在"}
        out = {"ok": True, **_translate_preset(snap["decision"])}
        out["tactics_scores"] = _translate_tactics_scores(snap.get("purpose_scores") or [])
        out["p3_recommend"] = _translate_p3_recommend(snap.get("ai_suggest"))
        out["directions_rich"] = _translate_p4_directions(snap.get("direction_detail") or [])
        out["directions_summary"] = _translate_p4_summary(snap.get("direction_main"))
        out["core_keywords"] = _translate_core_keywords(snap.get("core_keywords") or [])
        return out
    except Exception as e:
        logger.exception("decision/preset 异常 [%s]: %s", decision_id, e)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
