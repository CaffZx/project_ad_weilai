"""Workflow step implementations."""

import asyncio
import logging
import time
from datetime import datetime, timezone

from app.config.settings import settings
from app.core.metrics_ops_language import humanize_ops_text, sanitize_analysis_for_display
from app.core.scenario_analyzer import detect_scenario, build_metric_board
from app.core.query_router import QueryRouter
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
from app.workflow.data_contract import blocked_message
from app.workflow.data_gate import ensure_data_for_llm, is_llm_blocked
from app.workflow.data_summary import build_data_summary
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
EXECUTION_META_FILTER = get_meta_filter("execution")

async def run_get_execution_options(ctx: WorkflowContext, asin: str, days: int = 7) -> ExecutionOptionsResponse:
    """返回方向选项 + 评分 + LLM推荐（从缓存取）"""
    data, verdict = await ensure_data_for_llm(
        ctx, "execution", asin, days=days, meta_filter=EXECUTION_META_FILTER,
    )
    long_term = ctx.state.get_long_term_config(asin)

    # 运营已保存的方向选择（不从推荐器取值，纯粹读回 state，供前端恢复勾选）
    wf = ctx.state.get_workflow_state(asin)
    saved_selections: list[str] = (
        ((wf or {}).get("execution") or {}).get("selected_directions") or []
    )

    # 将战略/策略字段注入ASINData，供Recommender使用
    if long_term:
        data.product_level = long_term.get("product_level")
        data.product_stage = long_term.get("product_stage")
        data.season_stage = long_term.get("season_stage")
        purposes = long_term.get("ad_purposes", [])
        data.ad_purpose = purposes[0] if purposes else None
        data.target_keyword_strategy = ",".join(long_term.get("target_keyword_strategy", []))

    # 运行推荐器评分
    rec_response = ctx.recommender.recommend(data)

    # LLM 推荐
    strategy = {
        "product_level": long_term.get("product_level", "常规产品 (P2)"),
        "product_stage": long_term.get("product_stage", ""),
        "season_stage": long_term.get("season_stage", ""),
    } if long_term else {}
    tactics = {
        "ad_purposes": long_term.get("ad_purposes", []),
        "target_keyword_strategy": long_term.get("target_keyword_strategy", []),
    } if long_term else {}

    data_summary = build_data_summary(data, days=days, verdict=verdict)
    manual_acos = ctx.state.get_target_acos_override(asin)
    if manual_acos is not None:
        data_summary["acos_target"] = manual_acos
    scores_list = [s.model_dump() for s in rec_response.directions]
    eligible_ids = ctx.recommender.get_eligible_ids(rec_response.directions)
    ineligible = [
        {
            "id": s.id,
            "label": s.label,
            "score": s.suitability_score,
            "reason": s.reason,
        }
        for s in rec_response.directions
        if s.id not in eligible_ids
    ]
    if is_llm_blocked(verdict):
        top = sorted(scores_list, key=lambda s: s.get("suitability_score", 0), reverse=True)
        fallback_dirs = [s["id"] for s in top if s["id"] in eligible_ids][:2]
        exec_rec = {
            "recommended_directions": fallback_dirs or (eligible_ids[:1] if eligible_ids else []),
            "reasoning": blocked_message(verdict),
            "priority_order": [s["id"] for s in top],
            "conflict_notes": "",
        }
    else:
        exec_rec = await ctx.reasoner.recommend_execution(
            asin=asin,
            data_summary=data_summary,
            strategy=strategy,
            tactics=tactics,
            scores=scores_list,
            eligible_directions=eligible_ids,
            ineligible_directions=ineligible,
            missing_notice=data_summary.get("missing_notice"),
        )
    exec_reasoning = humanize_ops_text(exec_rec.get("reasoning", "") or "")

    # 组装方向列表（过滤 LLM 越权推荐）
    recommended_ids = ctx.recommender.filter_recommended(
        exec_rec.get("recommended_directions", []),
        eligible_ids,
        rec_response.directions,
    )
    directions = []
    for s in rec_response.directions:
        directions.append(ExecutionDirection(
            id=s.id,
            label=s.label,
            suitability_score=s.suitability_score,
            suitability=s.suitability,
            reason=s.reason,
            recommended=s.id in recommended_ids,
            recommendation_reason=exec_reasoning,
            default_sub_options=s.default_sub_options,
        ))

    strategy_ctx = StrategyConfirmRequest(
        asin=asin,
        product_level=long_term.get("product_level", "常规产品 (P2)"),
        product_stage=long_term.get("product_stage", "推进期"),
        season_stage=long_term.get("season_stage", "淡季"),
        operating_mode=long_term.get("operating_mode"),
    ) if long_term else None
    tactics_ctx = TacticsConfirmRequest(
        asin=asin,
        ad_purposes=long_term.get("ad_purposes", []),
        target_keyword_strategy=long_term.get("target_keyword_strategy", []),
    ) if long_term else None

    # 查询路由：场景 → 元脚本清单
    scenario = detect_scenario(
        data,
        strategy.get("product_stage"),
        tactics.get("ad_purposes"),
    )
    meta_ids = QueryRouter.resolve(scenario.get("id", "default"), recommended_ids)
    query_plan = QueryRouter.describe(meta_ids)

    from app.workflow.data_status import data_status_fields

    status = data_status_fields(data)
    resp = ExecutionOptionsResponse(
        asin=asin,
        directions=directions,
        recommended_directions=recommended_ids,
        selected_directions=saved_selections,
        recommendation_summary=exec_reasoning,
        strategy_context=strategy_ctx,
        tactics_context=tactics_ctx,
        query_plan=query_plan,
        llm_status=verdict.status,
        data_completeness=verdict.to_completeness_dict(),
        **status,
    )
    return resp

async def run_confirm_execution(ctx: WorkflowContext, req: ExecutionSelectRequest) -> ExecutionSelectResponse:
    """保存执行层选择到工作流状态（不持久化）"""
    ctx.state.save_execution(
        req.asin,
        req.model_dump(),
        shop_id=req.shop_id,
        parent_seller_sku=req.parent_seller_sku,
    )
    ctx.state.advance_layer(
        req.asin,
        "validation",
        shop_id=req.shop_id,
        parent_seller_sku=req.parent_seller_sku,
    )

    return ExecutionSelectResponse(
        asin=req.asin,
        accepted=True,
    )

# ── P3 上游推荐方法 ──────────────────────────────────

