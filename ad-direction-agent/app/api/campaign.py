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
    refresh = bool(req.get("refresh", False))   # True 时跳过 Redis 缓存，强制重新拉数据

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
        # 广告方向：运营 tab4「生成评估报告」已选并持久化到 workflow_state.execution
        ad_directions = (wf.get("execution") or {}).get("selected_directions") or []

        # ASIN 数据（120s 超时；超时由外层 except 兜住）
        # 只拉 META_AD_PRODUCT(供 daily_budget 兜底 spend/days × 1.15)
        # + listing/gross_profit (必跑,提供 margin/inventory_qty/rating/refund_rate/avg_daily_sales)。
        # Campaign 模块完全用不到的 6 个 META 一律跳过:
        #   META_KW_AD / META_AD_PLACEMENT(ASIN级,campaign 有自己的懒加载) /
        #   META_KW_COMPETITOR_RANK + META_KW_SUB_ASIN_RANK (自然排名 ~62s 慢查询) /
        #   META_FLOW_KEYWORD (搜索量库 ~87s 慢查询) /
        #   META_COMPETITOR / META_TREND
        # MCP 路径下原本并发拉快几百毫秒看不出,doris_fallback 串行就累计 ~150s 触发 120s 超时。
        #
        # TODO(A1): 修 run_campaign_analysis 内 ensure_data 解包 bug 后,
        #   本端点应改走 ctx._ensure_data(meta_filter=...) → asin_data_cache(Redis TTL 4h),
        #   主应用诊断查过后 Campaign 直接命中缓存,完全不查数仓;
        #   届时本黑名单 meta_filter 可保留(ctx 内 _ensure_data 也支持透传)。
        aggregator = DataAggregator()
        asin_data = await asyncio.wait_for(
            aggregator.fetch(asin, days=days, meta_filter=["META_AD_PRODUCT"]),
            timeout=120,
        )

        # 策略上下文组装
        strat_ctx = build_campaign_strategy_context(
            asin, asin_data, long_term, keyword_analysis, ad_directions, days=days,
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

        # LLM 分析（纵深防御: 任务级总超时,防 Semaphore 饥饿永久挂死）
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
        return result.model_dump()

    except asyncio.TimeoutError as e:
        logger.warning("Campaign analyze 超时 [%s]: %s", asin, e)
        return CampaignAnalysisResult(
            parent_asin=asin, days=days,
            warnings=[f"分析总超时（>{settings.campaign_total_timeout}s），请稍后重试或联系管理员"],
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
