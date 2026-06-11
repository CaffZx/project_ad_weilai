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
        )
    return vm


@router.get("/campaign/snapshot")
async def campaign_snapshot(asin: str = "", decision_id: str = "", days: int = 7):
    """读取最近一次定时分析的快照（mode=readonly）。

    不传 decision_id 时默认取 is_latest=1 的最新决策。
    本期为 stub 实现，返回 mock 数据；后端 mapper 另排期接入业务库。
    """
    from datetime import datetime, timezone

    if not asin:
        return {"mode": "readonly", "parent_asin": "", "days": days, "run_id": "",
                "snapshot_time": None, "summary": {}, "overview": None,
                "budget_summary": None, "synthesis": None, "items": [], "warnings": ["asin 必填"]}

    logger.info("Snapshot stub [%s] decision_id=%s", asin, decision_id or "(latest)")

    return {
        "mode": "readonly",
        "parent_asin": asin,
        "days": 7,
        "run_id": decision_id or "latest",
        "snapshot_time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "summary": {
            "total": 0, "eliminate": 0, "adjust": 0, "keep": 0, "create": 0,
            "prefiltered": 0, "lost": 0,
            "confidence_high": 0, "confidence_medium": 0, "confidence_low": 0,
            "budget_impact": None, "sanity_check_passed": None,
        },
        "overview": {
            "facts": {},
            "assessment_text": "（快照数据未接入，此处为占位）",
            "direction_text": "",
            "generated_by": "snapshot",
        },
        "budget_summary": None,
        "synthesis": None,
        "items": [],
        "warnings": ["快照接口为 stub，后端 mapper 另排期接入业务库"],
    }


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
            ),
            timeout=settings.campaign_total_timeout,
        )

        extra = {
            "state": state,
            "asin": asin,
            "days": days,
            "temperature": effective_temp,
            "write_erp": write_erp,
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
    """运营批量审核占位端点 —— 本期仅记录日志，后续生产化会：
    1. 写 MySQL 表 campaign_confirm_log（含 run_id 幂等键）
    2. 异步推送到 ERP 系统
    3. 与 adjustment_history 关联
    """
    # TODO 生产化：写表 + 推 ERP
    approve_n = sum(1 for d in req.decisions if d.decision == "approve")
    reject_n = sum(1 for d in req.decisions if d.decision == "reject")
    logger.info(
        "Campaign confirm [%s run_id=%s] %d decisions (approve=%d, reject=%d) from %s",
        req.asin, req.run_id or "?", len(req.decisions), approve_n, reject_n,
        req.operator or "anonymous",
    )
    return {
        "received": len(req.decisions),
        "asin": req.asin,
        "run_id": req.run_id,
        "approve": approve_n,
        "reject": reject_n,
        "note": "本期仅日志占位，后续会写库+推 ERP",
    }
