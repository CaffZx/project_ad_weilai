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
        return {
            "asin": "", "parent_asin": "", "days": days,
            "total_campaigns": 0,
            "adjustments": [], "summary": {},
            "warnings": ["asin 必填"],
            "sanity_check_passed": False,
            "llm_rounds_completed": 0,
            "rounds_detail": {},
        }

    # ── 状态 & 长期配置 ────────────────────────────────────────────────────
    # 用工厂单例，保证与左侧卡片 (strategy/tactics/P3 override) 读写同一后端
    state = get_state_manager()
    long_term = state.get_long_term_config(asin) or {}
    wf = state.get_workflow_state(asin) or {}
    keyword_analysis = wf.get("keyword_analysis", {})

    # ── ASIN 数据 ──────────────────────────────────────────────────────────
    aggregator = DataAggregator()
    asin_data = await asyncio.wait_for(
        aggregator.fetch(asin, days=days),
        timeout=120,
    )

    # ── 策略上下文组装 ────────────────────────────────────────────────────
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

    # ── LLM 分析 ───────────────────────────────────────────────────────────
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
