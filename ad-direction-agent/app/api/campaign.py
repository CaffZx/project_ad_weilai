"""Campaign 活动分析 API — 调试用薄路由，委托给 steps/campaign.py。

设计权衡（本期）：
- 不走 ctx 路径（因 run_campaign_analysis 内 ensure_data 解包问题 A1 本期未修）
- 在 handler 内手动组装 strat_ctx + 三级 target_acos 回落
- 用 get_state_manager() 单例，与左侧卡片读写后端一致
- fetch 时硬限 meta_filter=["META_AD_PRODUCT"] 砍掉 Campaign 用不到的 6 个 META
  (详见 fetch 调用点的 TODO(A1) 注释)
"""

import asyncio
import logging

from fastapi import APIRouter

from app.config.settings import settings
from app.core.data_aggregator import DataAggregator
from app.core.recommender import TargetAcosRecommender
from app.data.campaign_fetcher import CampaignFetcher
from app.llm.reasoner import reasoner
from app.models.campaign import CampaignAnalysisResult, CampaignConfirmRequest
from app.persistence.erp_writer.auto_push import (
    analysis_to_kb_payload,
    push_full_to_erp,
    should_push_to_erp,
    wizard_payload_from_state,
)
from app.persistence.state_factory import get_state_manager
from app.workflow.steps.campaign import (
    analyze_campaigns,
    build_campaign_strategy_context,
)
from app.api.campaign_viewmodel import to_viewmodel

router = APIRouter()
logger = logging.getLogger(__name__)


async def _maybe_push_erp(
    result: CampaignAnalysisResult,
    *,
    asin: str,
    days: int,
    temperature: float,
    write_erp: bool,
    state,
    analysis_mode: str = "REALTIME",
) -> dict:
    """分析成功后可选写入 ERP；失败不抛异常。"""
    enabled = write_erp or settings.erp_auto_write
    if not enabled:
        return {"attempted": False, "ok": False, "skipped": "erp_auto_write disabled"}

    ok, reason = should_push_to_erp(result)
    if not ok:
        return {"attempted": False, "ok": False, "skipped": reason}

    wizard_payload, wizard_partial = wizard_payload_from_state(asin, days, state)
    kb_payload = analysis_to_kb_payload(result, temperature=temperature)
    try:
        report = await asyncio.to_thread(
            push_full_to_erp,
            kb_payload,
            wizard_payload,
        )
        out = {
            "attempted": True,
            "ok": True,
            "decision_id": report.decision_id,
            "wizard_partial": wizard_partial,
            **report.as_dict(),
        }
        logger.info(
            "ERP write_full OK [%s] decision_id=%s cards=%d",
            asin, report.decision_id, report.modern_card,
        )
        # 批次收尾：本批次置最新（is_latest 我方维护）+ 清进行中事件标记（state 库）
        try:
            from app.persistence.erp_writer.repository import _get_repository
            await asyncio.to_thread(
                _get_repository().finalize_batch, report.decision_id, asin, analysis_mode,
            )
            await asyncio.to_thread(state.clear_analysis_session, asin)
        except Exception as fe:
            logger.warning("finalize_batch/清进行中 失败 [%s] %s: %s (非阻塞)", asin, report.decision_id, fe)
        return out
    except Exception as e:
        logger.exception("ERP write_full 失败 [%s]: %s", asin, e)
        return {
            "attempted": True,
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "wizard_partial": wizard_partial,
        }


@router.post("/campaign/analyze")
async def campaign_analyze(req: dict):
    """运行 Campaign LLM 分析（分批 + R1+R2 投票 + 可选 R3 + sanity_check）。

    保持老格式 CampaignAnalysisResult，供 campaign_test.html 独立调试用。
    """
    result, extra = await _do_analyze(req)

    body = result.model_dump()
    if extra is not None:
        body["erp_write"] = await _maybe_push_erp(
            result,
            asin=extra["asin"],
            days=extra["days"],
            temperature=extra["temperature"],
            write_erp=extra["write_erp"],
            state=extra["state"],
            analysis_mode=extra["analysis_mode"],
        )
    return body


@router.post("/campaign/viewmodel")
async def campaign_viewmodel(req: dict):
    """运行 Campaign LLM 分析，返回统一 CampaignViewModel（mode=interactive）。

    供 tab5 实时操作台使用。与 /campaign/analyze 走同一分析链路，
    仅输出格式不同——pipe 过 to_viewmodel()。
    """
    result, extra = await _do_analyze(req)

    vm = to_viewmodel(result, mode="interactive")

    if extra is not None:
        vm["erp_write"] = await _maybe_push_erp(
            result,
            asin=extra["asin"],
            days=extra["days"],
            temperature=extra["temperature"],
            write_erp=extra["write_erp"],
            state=extra["state"],
            analysis_mode=extra["analysis_mode"],
        )
    return vm


def _empty_snapshot(asin: str, days: int, msg: str) -> dict:
    from datetime import datetime, timezone
    return {
        "mode": "readonly", "parent_asin": asin, "days": days, "run_id": "",
        "snapshot_time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "summary": {"total": 0, "eliminate": 0, "adjust": 0, "keep": 0, "create": 0,
                    "prefiltered": 0, "lost": 0, "confidence_high": 0,
                    "confidence_medium": 0, "confidence_low": 0,
                    "budget_impact": None, "sanity_check_passed": None},
        "overview": None, "budget_summary": None, "synthesis": None,
        "items": [], "warnings": [msg],
    }


@router.get("/campaign/snapshot")
async def campaign_snapshot(asin: str = "", decision_id: str = "", days: int = 7):
    """读取某决策批次的执行层快照（mode=readonly）。

    不传 decision_id 时取该 ASIN is_latest=1 的最新已完成批次。
    """
    from app.persistence.erp_writer.repository import _get_repository
    from app.api.campaign_viewmodel import from_db_snapshot

    if not asin and not decision_id:
        return _empty_snapshot(asin, days, "asin 或 decision_id 必填")

    try:
        repo = _get_repository()
        if not decision_id:
            latest = await asyncio.to_thread(repo.get_latest_completed, asin)
            if not latest:
                return _empty_snapshot(asin, days, "该 ASIN 暂无已完成批次，请先运行执行层分析")
            decision_id = latest.get("decision_id")
        snap = await asyncio.to_thread(repo.read_snapshot, decision_id)
        if not snap:
            return _empty_snapshot(asin, days, f"批次 {decision_id} 不存在")
        return from_db_snapshot(snap, mode="readonly")
    except Exception as e:
        logger.exception("Snapshot 读取失败 [%s] decision_id=%s: %s", asin, decision_id, e)
        return _empty_snapshot(asin, days, f"快照读取失败: {type(e).__name__}: {e}")


# ── 审核占位端点（本期 stub）─────────────────────────────────────────────────


# ── 共享：抽取核心分析逻辑，供 /campaign/analyze 和 /campaign/viewmodel 复用 ──


async def _do_analyze(req: dict) -> tuple[CampaignAnalysisResult, dict | None]:
    """执行 Campaign LLM 分析，返回 (result, extra)。

    extra 包含 erp_write 所需上下文：state / days / temperature。
    """
    asin = str(req.get("asin", "")).strip()
    days = int(req.get("days", 7))
    temp = req.get("temperature", None)
    refresh = bool(req.get("refresh", False))
    write_erp = bool(req.get("write_erp", False))
    requested_run_id = str(req.get("run_id") or "").strip()
    analysis_mode = str(req.get("analysis_mode") or "REALTIME").upper()

    if not asin:
        return (
            CampaignAnalysisResult(
                parent_asin="", days=days,
                warnings=["asin 必填"],
                sanity_check_passed=False,
                llm_rounds_completed=0,
            ),
            None,
        )

    try:
        state = get_state_manager()
        sess = state.get_analysis_session(asin)
        session_run_id = str((sess or {}).get("run_id") or "").strip()
        effective_run_id = requested_run_id or session_run_id or None
        long_term = state.get_long_term_config(asin) or {}
        wf = state.get_workflow_state(asin) or {}
        keyword_analysis = wf.get("keyword_analysis", {})
        ad_directions = (wf.get("execution") or {}).get("selected_directions") or []

        aggregator = DataAggregator()
        asin_data = await asyncio.wait_for(
            aggregator.fetch(asin, days=days, meta_filter=["META_AD_PRODUCT"]),
            timeout=120,
        )

        strat_ctx = build_campaign_strategy_context(
            asin, asin_data, long_term, keyword_analysis, ad_directions, days=days,
        )

        manual = state.get_target_acos_override(asin)
        if manual is not None:
            strat_ctx.target_acos = int(manual)
        else:
            p3 = state.get_p3_recommendation(asin)
            if p3 and p3.get("target_acos", {}).get("recommended_target"):
                strat_ctx.target_acos = int(p3["target_acos"]["recommended_target"])
            else:
                rec = TargetAcosRecommender().recommend(
                    asin_data, long_term.get("ad_purposes", []),
                )
                strat_ctx.target_acos = int(rec.recommended_target)

        effective_temp = float(temp) if temp is not None else settings.campaign_llm_temperature

        logger.info(
            "Campaign analyze [%s] days=%d temp=%.2f target_acos=%s",
            asin, days, effective_temp, strat_ctx.target_acos,
        )

        fetcher = CampaignFetcher()
        result = await asyncio.wait_for(
            analyze_campaigns(
                fetcher=fetcher,
                reasoner=reasoner,
                parent_asin=asin,
                asin_data=asin_data,
                strategy_context=strat_ctx,
                days=days,
                temperature=effective_temp,
                refresh=refresh,
                keyword_analysis=keyword_analysis,
                run_id=effective_run_id,
            ),
            timeout=settings.campaign_total_timeout,
        )

        extra = {
            "state": state,
            "asin": asin,
            "days": days,
            "temperature": effective_temp,
            "write_erp": write_erp,
            "analysis_mode": analysis_mode,
        }
        return (result, extra)

    except asyncio.TimeoutError as e:
        logger.warning("Campaign analyze 超时 [%s]: %s", asin, e)
        return (
            CampaignAnalysisResult(
                parent_asin=asin, days=days,
                warnings=[f"分析总超时（>{settings.campaign_total_timeout}s），请稍后重试或联系管理员"],
                sanity_check_passed=False,
                llm_rounds_completed=0,
            ),
            None,
        )
    except Exception as e:
        logger.exception("Campaign analyze 异常 [%s]: %s", asin, e)
        return (
            CampaignAnalysisResult(
                parent_asin=asin, days=days,
                warnings=[f"分析失败: {type(e).__name__}: {e}"],
                sanity_check_passed=False,
                llm_rounds_completed=0,
            ),
            None,
        )


@router.post("/campaign/confirm")
async def campaign_confirm(req: CampaignConfirmRequest):
    """运营批量审核 → 写 card + pending 的 confirm_status（PENDING→CONFIRMED/REJECTED）。

    req.run_id 为快照的 decision_id（write_full 落库时生成）；decision.campaign_key 即 card_id。
    校验：批次 is_latest=1 且该 ASIN 无进行中事件（state 库）；每活动只处理一次（幂等，重复→skipped）。
    """
    decision_id = (req.run_id or "").strip()
    if not decision_id:
        return {"ok": False, "error": "run_id(decision_id) 必填", "applied": 0, "skipped": 0}

    decisions = [{"campaign_key": d.campaign_key, "decision": d.decision} for d in req.decisions]
    from app.persistence.erp_writer.repository import _get_repository
    try:
        sess = get_state_manager().get_analysis_session(req.asin) if req.asin else None
        in_progress = bool(sess and sess.get("run_id"))
        repo = _get_repository()
        result = await asyncio.to_thread(
            repo.confirm_decisions, decision_id, decisions, req.operator or None,
            in_progress=in_progress,
        )
    except Exception as e:
        logger.exception("Campaign confirm 失败 [%s] decision_id=%s: %s", req.asin, decision_id, e)
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "applied": 0, "skipped": 0}

    logger.info(
        "Campaign confirm [%s decision_id=%s] applied=%s skipped=%s from %s",
        req.asin, decision_id, result.get("applied"), result.get("skipped"),
        req.operator or "anonymous",
    )
    return result
