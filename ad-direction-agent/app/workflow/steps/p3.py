"""Workflow step implementations."""

import asyncio
import logging
import time
from datetime import datetime, timezone

from app.config.settings import settings
from app.core.metrics_ops_language import humanize_ops_text, sanitize_analysis_for_display
from app.core.scenario_analyzer import detect_scenario, build_metric_board
from app.core.recommender import TargetAcosRecommender, BudgetBidRecommender
from app.models.asin_data import ASINData
from app.models.layers import (
    StrategyDimension,
    StrategyOptionsResponse,
    StrategyConfirmRequest,
    StrategyConfirmResponse,
    TacticsDimension,
    TacticsOptionsResponse,
    TacticsConfirmRequest,
    TacticsConfirmResponse,
    DiagnosisResponse,
    ExecutionDirection,
    ExecutionOptionsResponse,
    ExecutionSelectRequest,
    ExecutionSelectResponse,
    WizardStateResponse,
    UnifiedRecommendRequest,
    UnifiedRecommendResponse,
    TargetAcosResult,
    BudgetBidResult,
)
from app.workflow.context import WorkflowContext
from app.workflow.data_contract import blocked_message, field_labels
from app.workflow.data_gate import ensure_data_for_llm, is_llm_blocked
from app.workflow.data_summary import build_data_summary
from app.workflow.data_status import data_status_fields
from app.workflow.steps.tactics import _attach_data_identity
from app.workflow.helpers import (
    _is_valid_data,
    _rank_trend_label,
    _acos_trend_label,
    _format_asin_daily_trend,
    _kw_to_ai_summary,
    _kw_trend_priority,
)
from app.workflow.meta_filters import get_meta_filter

logger = logging.getLogger(__name__)

FETCH_TIMEOUT = 35
LLM_TIMEOUT = 60
P3_META_FILTER = get_meta_filter("p3")

async def run_get_target_acos_recommendation(ctx: WorkflowContext, asin: str, days: int = 7):
    """P3 Feature 1: 目标 ACOS 推荐 — 优先返回手动设定值（每日5:00过期），否则走算法"""
    manual = ctx.state.get_target_acos_override(asin)

    if manual is not None:
        recommender = TargetAcosRecommender()
        return recommender.recommend_manual(asin, manual)

    long_term = ctx.state.get_long_term_config(asin)
    data = await ctx.ensure_data(asin, days=days, meta_filter=P3_META_FILTER)
    if long_term:
        data.product_stage = long_term.get("product_stage")
        data.product_level = long_term.get("product_level")
        purposes = long_term.get("ad_purposes", [])
        data.ad_purpose = purposes[0] if purposes else None

    recommender = TargetAcosRecommender()
    ad_purposes = long_term.get("ad_purposes", []) if long_term else []
    return recommender.recommend(data, ad_purposes)

def run_save_target_acos_override(
    ctx: WorkflowContext,
    asin: str,
    value: int,
    shop_id: int | None = None,
    parent_seller_sku: str | None = None,
) -> bool:
    """运营手动设定目标 ACOS（次日 5:00 过期），写入调整历史"""
    ctx.state.record_adjustment(
        asin,
        target_acos=value,
        shop_id=shop_id,
        parent_seller_sku=parent_seller_sku,
    )
    return ctx.state.set_target_acos_override(
        asin,
        value,
        shop_id=shop_id,
        parent_seller_sku=parent_seller_sku,
    )

def run_clear_target_acos_override(ctx: WorkflowContext, asin: str) -> bool:
    """清除手动设定的目标 ACOS"""
    return ctx.state.clear_target_acos_override(asin)

async def run_get_budget_bid_recommendation(ctx: WorkflowContext, asin: str, days: int = 7):
    """P3 Feature 2: 预算和 Bid 推荐 — 优先返回手动设定值，否则走算法"""
    long_term = ctx.state.get_long_term_config(asin)
    manual = long_term.get("daily_budget_override") if long_term else None

    if manual is not None:
        recommender = BudgetBidRecommender()
        return recommender.recommend_manual(asin, manual)

    data = await ctx.ensure_data(asin, days=days, meta_filter=P3_META_FILTER)
    if long_term:
        data.product_stage = long_term.get("product_stage")
        data.season_stage = long_term.get("season_stage")
        purposes = long_term.get("ad_purposes", [])
        data.ad_purpose = purposes[0] if purposes else None

    last_adjustment = ctx.state.get_adjustment_history(asin)

    recommender = BudgetBidRecommender()
    ad_purposes = long_term.get("ad_purposes", []) if long_term else []
    season_stage = long_term.get("season_stage", "淡季") if long_term else "淡季"
    return recommender.recommend(data, ad_purposes, season_stage, last_adjustment)

def run_save_budget_override(
    ctx: WorkflowContext,
    asin: str,
    value: float,
    shop_id: int | None = None,
    parent_seller_sku: str | None = None,
) -> bool:
    """运营手动设定日预算，写入 long_term_config + 调整历史"""
    ctx.state.record_adjustment(
        asin,
        daily_budget=value,
        shop_id=shop_id,
        parent_seller_sku=parent_seller_sku,
    )
    return ctx.state.set_long_term_config(
        asin,
        {
            "shop_id": shop_id,
            "parent_seller_sku": parent_seller_sku,
            "daily_budget_override": value,
        },
    )

def run_clear_budget_override(ctx: WorkflowContext, asin: str) -> bool:
    """清除手动设定的日预算"""
    return ctx.state.set_long_term_config(asin, {"daily_budget_override": None})

# ── P3 统一推荐（LLM 驱动）─────────────────────────────

async def run_get_unified_recommendation(ctx: WorkflowContext, asin: str, refresh: bool = False,
                                       days: int = 7):
    """P3 统一推荐：手动覆盖 > LLM > 缓存 > 算法降级"""
    long_term = ctx.state.get_long_term_config(asin)

    # 0. 手动覆盖优先（refresh 时跳过，强制走 LLM）
    manual_acos = ctx.state.get_target_acos_override(asin)
    manual_budget = long_term.get("daily_budget_override") if long_term else None
    if not refresh and (manual_acos is not None or manual_budget is not None):
        from app.core.recommender import TargetAcosRecommender, BudgetBidRecommender
        tacos_rec = TargetAcosRecommender.recommend_manual(asin, manual_acos) if manual_acos is not None else None
        bb_rec = BudgetBidRecommender.recommend_manual(asin, manual_budget) if manual_budget is not None else None
        return {
            "asin": asin,
            "target_acos": {
                "recommended_target": tacos_rec.recommended_target if tacos_rec else 0,
                "reasoning": tacos_rec.reasoning_chain[0].result if tacos_rec else "",
                "confidence": "high" if tacos_rec else "medium",
                "manual_override": True if tacos_rec else False,
            },
            "budget_bid": {
                "current": bb_rec.budget_recommendation.current if bb_rec else 0.0,
                "suggested": bb_rec.budget_recommendation.suggested if bb_rec else 0.0,
                "direction": "maintain",
                "magnitude_pct": 0.0,
                "reason": bb_rec.budget_recommendation.reason if bb_rec else "",
                "manual_override": True if bb_rec else False,
            },
            "overall_reasoning": "当前使用运营手动设定值，点击「AI 重新推荐」可覆盖",
            "risk_warnings": [],
            "from_cache": False,
        }

    # 1. 检查缓存
    if not refresh:
        cached = ctx.state.get_p3_recommendation(asin)
        if cached:
            return cached

    # 2. 加载数据（refresh 时强制清缓存重拉）+ 完整性闸门
    data, verdict = await ensure_data_for_llm(
        ctx,
        "p3",
        asin,
        days=days,
        meta_filter=P3_META_FILTER,
        refresh=refresh,
    )
    long_term = ctx.state.get_long_term_config(asin)

    if is_llm_blocked(verdict):
        msg = blocked_message(verdict)
        return {
            "asin": asin,
            "status": "blocked",
            "llm_status": "blocked",
            "message": msg,
            "missing_required_labels": field_labels(verdict.missing_required),
            "data_completeness": verdict.to_completeness_dict(),
            "target_acos": {
                "recommended_target": 0,
                "reasoning": msg,
                "confidence": "low",
                "manual_override": False,
            },
            "budget_bid": {
                "current": 0.0,
                "suggested": 0.0,
                "direction": "maintain",
                "magnitude_pct": 0.0,
                "reason": msg,
                "manual_override": False,
            },
            "overall_reasoning": msg,
            "risk_warnings": ["核心数据不足，未调用 AI 推荐"],
            "from_cache": False,
            **data_status_fields(data),
        }

    strategy = {
        "product_level": long_term.get("product_level", "常规产品 (P2)"),
        "product_stage": long_term.get("product_stage"),
        "season_stage": long_term.get("season_stage", "淡季"),
    } if long_term else {}
    tactics = {
        "ad_purposes": long_term.get("ad_purposes", []),
        "target_keyword_strategy": long_term.get("target_keyword_strategy", []),
    } if long_term else {}

    # 构建数据摘要
    data_summary = {}
    if data.ad_data:
        data_summary["ACOS"] = f"{data.ad_data.acos:.1f}%" if data.ad_data.acos else "N/A"
        data_summary["精准ACOS"] = f"{data.ad_data.precision_acos:.1f}%" if data.ad_data.precision_acos else "N/A"
        data_summary["非精准ACOS"] = f"{data.ad_data.broad_acos:.1f}%" if data.ad_data.broad_acos else "N/A"
        data_summary["TACOS"] = f"{data.ad_data.tacos:.1f}%" if data.ad_data.tacos else "N/A"
        data_summary["CPC"] = f"${data.ad_data.cpc:.2f}" if data.ad_data.cpc else "N/A"
        data_summary["CTR"] = f"{data.ad_data.ctr:.1f}%" if data.ad_data.ctr else "N/A"
        data_summary["CVR"] = f"{data.ad_data.cvr:.1f}%" if data.ad_data.cvr else "N/A"
        data_summary["日均花费"] = f"${data.ad_data.spend/days:.2f}" if data.ad_data.spend else "N/A"
    if data.margin:
        data_summary["毛利率"] = f"{data.margin*100:.1f}%"
    if data.natural_order_ratio:
        data_summary["自然单占比"] = f"{data.natural_order_ratio:.1f}%"
    if data.avg_daily_sales_30d:
        data_summary["日均销量"] = f"{data.avg_daily_sales_30d:.1f}单"
    if data.signals and data.signals.inventory_qty:
        data_summary["可售库存"] = f"{data.signals.inventory_qty}件"

    # 关键词详情
    kw_details = []
    for kw in data.keywords[:10]:
        kw_details.append({
            "keyword": kw.keyword,
            "bid": kw.bid or 0,
            "cpc": kw.spend / kw.clicks if kw.clicks > 0 else 0,
            "acos": kw.acos or 0,
            "cvr": kw.cvr or 0,
            "spend": kw.spend or 0,
            "spend_注": f"{days}天总计花费",
        })

    # 历史记录
    history = ctx.state.get_adjustment_history(asin)

    # 趋势数据摘要（供 LLM 判断指标在改善还是恶化）
    trend_lines = []
    if data.trend:
        for tp in data.trend:
            trend_lines.append(
                f"  {tp.date}: ACOS={tp.acos}%, CVR={tp.cvr}%, "
                f"CPC=${tp.cpc}, orders={tp.orders}, spend=${tp.spend}"
            )
    trend_text = "\n".join(trend_lines) if trend_lines else "(无趋势数据)"

    summary_for_llm = build_data_summary(data, days=days, verdict=verdict)
    for k, v in data_summary.items():
        if k not in summary_for_llm:
            summary_for_llm[k] = v

    # 3. 调用 LLM
    try:
        llm_result = await asyncio.wait_for(ctx.reasoner.recommend_p3(
            asin=asin,
            strategy=strategy,
            tactics=tactics,
            data_summary=summary_for_llm,
            keyword_details=kw_details,
            history=history,
            current_acos_target=manual_acos,
            current_daily_budget=manual_budget,
            days=days,
            trend_text=trend_text,
            missing_notice=summary_for_llm.get("missing_notice"),
        ), timeout=LLM_TIMEOUT)
        # 组装为标准响应
        ta_raw = llm_result.get("target_acos", {})
        bb_raw = llm_result.get("budget_bid", {})
        result = {
            "asin": asin,
            "target_acos": {
                "recommended_target": ta_raw.get("recommended_target", 20),
                "reasoning": ta_raw.get("reasoning", ""),
                "confidence": ta_raw.get("confidence", "medium"),
            },
            "budget_bid": {
                "current": bb_raw.get("current", 0),
                "suggested": bb_raw.get("suggested", 0),
                "direction": bb_raw.get("direction", "maintain"),
                "magnitude_pct": bb_raw.get("magnitude_pct", 0),
                "reason": bb_raw.get("reason", ""),
            },
            "overall_reasoning": llm_result.get("overall_reasoning", ""),
            "risk_warnings": llm_result.get("risk_warnings", []),
            "from_cache": False,
            "llm_status": verdict.status,
            "status": "ok",
            "data_completeness": verdict.to_completeness_dict(),
            **data_status_fields(data),
        }
        _attach_data_identity(result, data)
        ctx.state.set_p3_recommendation(asin, result)
        return result
    except Exception as e:
        logger.warning("P3 LLM 推荐失败 [%s]，降级为算法推荐: %s", asin, e)
        # 降级：算法推荐器
        from app.core.recommender import TargetAcosRecommender, BudgetBidRecommender
        ad_purposes = long_term.get("ad_purposes", []) if long_term else []
        tacos_rec = TargetAcosRecommender().recommend(data, ad_purposes)
        bb_rec = BudgetBidRecommender().recommend(
            data, ad_purposes,
            long_term.get("season_stage", "淡季") if long_term else "淡季",
        )
        return {
            "asin": asin,
            "target_acos": {
                "recommended_target": tacos_rec.recommended_target,
                "reasoning": "(算法降级) " + "；".join(
                    s.result for s in tacos_rec.reasoning_chain
                ),
                "confidence": "low",
            },
            "budget_bid": {
                "current": bb_rec.budget_recommendation.current,
                "suggested": bb_rec.budget_recommendation.suggested,
                "direction": bb_rec.budget_recommendation.direction,
                "magnitude_pct": bb_rec.budget_recommendation.magnitude_pct,
                "reason": "(算法降级) " + bb_rec.budget_recommendation.reason,
            },
            "overall_reasoning": "LLM 调用失败，使用算法降级推荐。建议稍后点击「AI 重新推荐」。",
            "risk_warnings": ["LLM 调用失败，当前为算法降级结果"],
            "from_cache": False,
            **data_status_fields(data),
        }

# ── Layer 1.5 校验 + 报告 ────────────────────────────

