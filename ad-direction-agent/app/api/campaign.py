"""Campaign 活动分析 API — 调试用薄路由，委托给 steps/campaign.py。

设计权衡（本期）：
- 不走 ctx 路径（因 run_campaign_analysis 内 ensure_data 解包问题 A1 本期未修）
- 在 handler 内手动组装 strat_ctx + 三级 target_acos 回落
- 用 get_state_manager() 单例，与左侧卡片读写后端一致
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
from app.persistence.state_factory import get_state_manager
from app.workflow.steps.campaign import (
    analyze_campaigns,
    build_campaign_strategy_context,
)

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/campaign/analyze")
async def campaign_analyze(req: dict):
    """运行 Campaign LLM 分析（分批 + R1+R2 投票 + 可选 R3 + sanity_check）。

    Request body:
        asin        (str, 必填)
        days        (int, 默认 7)
        temperature (float, 可选；默认读 settings.campaign_llm_temperature)

    Response: CampaignAnalysisResult.model_dump()
    """
    asin = str(req.get("asin", "")).strip()
    days = int(req.get("days", 7))
    temp = req.get("temperature", None)

    if not asin:
        return CampaignAnalysisResult(
            parent_asin="", days=days,
            warnings=["asin 必填"],
            sanity_check_passed=False,
            llm_rounds_completed=0,
        ).model_dump()

    # ── 顶层 try/except：任何未捕获异常都转成降级 CampaignAnalysisResult，避免 500 + 堆栈 ──
    try:
        # 状态 & 长期配置
        state = get_state_manager()
        long_term = state.get_long_term_config(asin) or {}
        wf = state.get_workflow_state(asin) or {}
        keyword_analysis = wf.get("keyword_analysis", {})

        # ASIN 数据（120s 超时；超时由外层 except 兜住）
        aggregator = DataAggregator()
        asin_data = await asyncio.wait_for(
            aggregator.fetch(asin, days=days),
            timeout=120,
        )

        # 策略上下文组装
        strat_ctx = build_campaign_strategy_context(
            asin, asin_data, long_term, keyword_analysis,
        )

        # target_acos 三级回落：manual override > P3缓存 > 算法
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

        # LLM 分析（内部已有 fetch_campaigns timeout / sanity+synthesis 禁用 flag 等多层防护）
        fetcher = CampaignFetcher()
        result = await analyze_campaigns(
            fetcher=fetcher,
            reasoner=reasoner,
            parent_asin=asin,
            asin_data=asin_data,
            strategy_context=strat_ctx,
            days=days,
            temperature=effective_temp,
            keyword_analysis=keyword_analysis,
        )
        return result.model_dump()

    except asyncio.TimeoutError as e:
        logger.warning("Campaign analyze 超时 [%s]: %s", asin, e)
        return CampaignAnalysisResult(
            parent_asin=asin, days=days,
            warnings=[f"分析超时（>120s 数据拉取阶段），请稍后重试"],
            sanity_check_passed=False,
            llm_rounds_completed=0,
        ).model_dump()
    except Exception as e:
        logger.exception("Campaign analyze 异常 [%s]: %s", asin, e)
        return CampaignAnalysisResult(
            parent_asin=asin, days=days,
            warnings=[f"分析失败: {type(e).__name__}: {e}"],
            sanity_check_passed=False,
            llm_rounds_completed=0,
        ).model_dump()


# ── 审核占位端点（本期 stub）─────────────────────────────────────────────────


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
