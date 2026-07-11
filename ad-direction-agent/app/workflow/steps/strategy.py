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
from app.workflow.meta_filters import get_meta_filter

logger = logging.getLogger(__name__)

FETCH_TIMEOUT = 35
LLM_TIMEOUT = 60
DASHBOARD_LIGHT_META_FILTER = get_meta_filter("dashboard_light")

async def run_get_strategy_options(ctx: WorkflowContext, asin: str, days: int = 7) -> StrategyOptionsResponse:
    """返回三维度选项（纯静态配置，无 MCP/DB 查询）。

    不再预加载全量 MCP 数据——各 Tab 打开时按需拉取、走 Redis 缓存。
    """
    await ctx.preload_data(asin, days=days, meta_filter=DASHBOARD_LIGHT_META_FILTER)

    layer_config = settings.layer_options_config or {}
    strategy_cfg = layer_config.get("strategy", {})

    dimensions = []
    for dim_key in ("product_level", "product_stage", "season_stage"):
        dim_cfg = strategy_cfg.get(dim_key, {})
        options = dim_cfg.get("options", [])
        dimensions.append(StrategyDimension(
            id=dim_key,
            label=dim_cfg.get("label", dim_key),
            description=dim_cfg.get("description", ""),
            options=options,
            selection_type=dim_cfg.get("selection_type", "radio"),
        ))

    current = ctx.state.get_long_term_config(asin)

    return StrategyOptionsResponse(
        asin=asin,
        dimensions=dimensions,
        current_selection={
            "product_level": current.get("product_level"),
            "product_stage": current.get("product_stage"),
            "season_stage": current.get("season_stage"),
        } if current else None,
        data_ok=True,
        missing_fields=[],
        days=days,
    )

async def run_confirm_strategy(ctx: WorkflowContext, req: StrategyConfirmRequest,
                             days: int = 7) -> StrategyConfirmResponse:
    """保存战略层选择并持久化"""
    # 校验 ASIN 是否存在，防止无效 ASIN 创建垃圾 config 目录
    from app.config.settings import settings

    if settings.data_source == "mcp":
        from app.data.mcp_db_context import resolve_mcp_context_from_mcp
        from app.data.mcp_adapter import McpAdapter

        db_ctx = await resolve_mcp_context_from_mcp(req.asin, McpAdapter())
        if not db_ctx:
            return StrategyConfirmResponse(
                asin=req.asin,
                accepted=False,
                config_saved=False,
                reject_reason="context_error",
            )
        data = None
    else:
        data = await ctx.ensure_data(req.asin, days=days)

    if data is not None and data.data_missing:
        if "asin_not_found" in data.missing_fields:
            reason = "asin_not_found"
        elif "context" in data.missing_fields:
            reason = "context_error"  # DB 不可达或缺少 parent_seller_sku
        else:
            reason = "data_unavailable"
        return StrategyConfirmResponse(
            asin=req.asin,
            accepted=False,
            config_saved=False,
            reject_reason=reason,
        )

    config = {
        "product_level": req.product_level,
        "product_stage": req.product_stage,
        "season_stage": req.season_stage,
    }
    ctx.state.set_long_term_config(req.asin, config)
    ctx.state.advance_layer(req.asin, "tactics")

    return StrategyConfirmResponse(
        asin=req.asin,
        accepted=True,
        config_saved=True,
    )
