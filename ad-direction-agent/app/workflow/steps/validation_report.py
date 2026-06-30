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

logger = logging.getLogger(__name__)

FETCH_TIMEOUT = 35
LLM_TIMEOUT = 60

async def run_validation_and_report(ctx: WorkflowContext, asin: str, days: int = 7) -> dict:
    """运行校验+确认+LLM报告，返回完整结果（从缓存取）"""
    data, verdict = await ensure_data_for_llm(ctx, "report", asin, days=days)
    long_term = ctx.state.get_long_term_config(asin)
    wf_state = ctx.state.get_workflow_state(asin)

    # 注入上下文
    if long_term:
        data.product_level = long_term.get("product_level")
        data.product_stage = long_term.get("product_stage")
        data.season_stage = long_term.get("season_stage")
        purposes = long_term.get("ad_purposes", [])
        data.ad_purpose = purposes[0] if purposes else None
        data.target_keyword_strategy = ",".join(long_term.get("target_keyword_strategy", []))

    selected_dirs = (wf_state.get("execution", {}) or {}).get("selected_directions", [])
    sub_options = (wf_state.get("execution", {}) or {}).get("sub_options", {})

    if not selected_dirs:
        # 默认取推荐方向
        rec = ctx.recommender.recommend(data)
        selected_dirs = [rec.recommended_direction]

    # 并行校验+确认
    # 2026-06-30: 关键词数据暂缺时跳过决策包生成（空 keywords 会产出
    # "0词/0排名"的无意义数据，且调用链本身已标记为死代码）。
    validations = {}
    decisions = {}
    _has_kw = bool(data.keywords)
    for direction in selected_dirs:
        validations[direction] = (
            await ctx.validator.validate(data, direction, sub_options.get(direction, {}))
        ).model_dump()
        if _has_kw:
            decisions[direction] = {
                "decision_package": (
                    ctx.decision_gen.generate(data, direction, sub_options.get(direction, {}))
                ).model_dump()
            }

    # 评分
    rec_response = ctx.recommender.recommend(data)

    # LLM报告
    strategy = {
        "product_level": long_term.get("product_level"),
        "product_stage": long_term.get("product_stage"),
        "season_stage": long_term.get("season_stage"),
    } if long_term else None
    tactics = {
        "ad_purposes": long_term.get("ad_purposes", []),
        "target_keyword_strategy": long_term.get("target_keyword_strategy", []),
    } if long_term else None

    rpt_summary = build_data_summary(data, days=days, verdict=verdict)
    manual_acos = ctx.state.get_target_acos_override(asin)
    if manual_acos is not None:
        rpt_summary["acos_target"] = manual_acos
    eligible_ids = ctx.recommender.get_eligible_ids(rec_response.directions)

    if is_llm_blocked(verdict):
        msg = blocked_message(verdict)
        analysis = sanitize_analysis_for_display({
            "overall_analysis": msg,
            "direction_analyses": [],
            "action_priorities": [],
            "risk_warnings": ["核心数据不足，未生成 AI 综合分析"],
            "skip_directions_note": "",
            "llm_status": "blocked",
        })
    else:
        analysis = sanitize_analysis_for_display(
            await ctx.reasoner.analyze(
                asin=asin,
                data_summary=rpt_summary,
                scores=[s.model_dump() for s in rec_response.directions],
                validations=validations,
                decisions=decisions,
                strategy=strategy,
                tactics=tactics,
                days=days,
                selected_directions=selected_dirs,
                eligible_directions=eligible_ids,
                missing_notice=rpt_summary.get("missing_notice"),
            )
        )

    # 查询路由
    scenario = detect_scenario(
        data,
        strategy.get("product_stage") if strategy else None,
        tactics.get("ad_purposes", []) if tactics else [],
    )
    meta_ids = QueryRouter.resolve(scenario.get("id", "default"), selected_dirs)
    query_plan = QueryRouter.describe(meta_ids)

    return {
        "asin": asin,
        "validations": validations,
        "decisions": decisions,
        "analysis": analysis,
        "directions": [s.model_dump() for s in rec_response.directions],
        "data_summary": rpt_summary,
        "data_completeness": verdict.to_completeness_dict(),
        "llm_status": verdict.status,
        "query_plan": query_plan,
    }

# ── 向导状态 ─────────────────────────────────────────

