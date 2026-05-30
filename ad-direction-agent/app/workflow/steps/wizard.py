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

def run_get_wizard_state(ctx: WorkflowContext, asin: str) -> WizardStateResponse:
    """获取完整向导状态"""
    wf = ctx.state.get_workflow_state(asin)
    lt = ctx.state.get_long_term_config(asin)

    strategy = None
    if lt.get("product_level") or lt.get("product_stage"):
        strategy = StrategyConfirmRequest(
            asin=asin,
            product_level=lt.get("product_level", "腰部"),
            product_stage=lt.get("product_stage", "收割利润期"),
            season_stage=lt.get("season_stage", "淡季"),
        )

    tactics = None
    if lt.get("ad_purposes"):
        tactics = TacticsConfirmRequest(
            asin=asin,
            ad_purposes=lt.get("ad_purposes", []),
            target_keyword_strategy=lt.get("target_keyword_strategy", []),
        )

    execution = None
    exec_data = wf.get("execution")
    if exec_data:
        execution = ExecutionSelectRequest(
            asin=asin,
            selected_directions=exec_data.get("selected_directions", []),
            sub_options=exec_data.get("sub_options", {}),
        )

    return WizardStateResponse(
        asin=asin,
        current_layer=wf.get("current_layer", "strategy"),
        layers_completed=wf.get("layers_completed", []),
        strategy=strategy,
        tactics=tactics,
        execution=execution,
        long_term_config_exists=ctx.state.config_exists(asin),
        last_updated=wf.get("last_updated", ""),
    )

def run_reset_asin(ctx: WorkflowContext, asin: str) -> bool:
    return ctx.state.reset_asin(asin)

# ── 辅助方法 ─────────────────────────────────────────

