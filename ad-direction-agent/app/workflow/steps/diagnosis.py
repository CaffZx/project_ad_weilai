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
from app.workflow.data_contract import evaluate_completeness
from app.workflow.data_gate import is_llm_blocked
from app.workflow.data_summary import build_data_summary
from app.workflow.data_status import data_status_fields
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
DIAGNOSIS_META_FILTER = get_meta_filter("diagnosis")

async def run_get_diagnosis(ctx: WorkflowContext, asin: str, refresh: bool = False,
                          days: int = 7) -> DiagnosisResponse:
    """返回只读诊断数据

    无缓存 → 全量查，不触发二次按需查
    有缓存 → 直接读缓存
    刷新   → 缓存快照做场景检测 → 一次按需查询
    """
    long_term = ctx.state.get_long_term_config(asin)
    data = await ctx.ensure_data(
        asin,
        refresh=refresh,
        days=days,
        meta_filter=DIAGNOSIS_META_FILTER,
    )

    strategy_ctx = StrategyConfirmRequest(
        asin=asin,
        product_level=long_term.get("product_level", "常规产品"),
        product_stage=long_term.get("product_stage", "推进期"),
        season_stage=long_term.get("season_stage", "淡季"),
    ) if long_term else None

    tactics_ctx = TacticsConfirmRequest(
        asin=asin,
        ad_purposes=long_term.get("ad_purposes", []),
        target_keyword_strategy=long_term.get("target_keyword_strategy", []),
    ) if long_term else None

    metric_board = build_metric_board(
        data,
        strategy_ctx.product_stage if strategy_ctx else None,
        long_term.get("ad_purposes", []) if long_term else [],
    )

    scenario_id = metric_board.get("scenario_id", "default")
    meta_ids = QueryRouter.resolve(scenario_id)
    query_plan = QueryRouter.describe(meta_ids)

    # 全量 refresh 已拉齐全部维度；原逻辑会再按场景 meta 二次查库，易叠加 50s+ 导致前端超时
    if refresh and _is_valid_data(data):
        logger.debug("诊断刷新跳过场景 meta 二次查询 [%s] scenario=%s", asin, scenario_id)

    # 告警信号
    alerts = []
    sig = data.signals
    if sig:
        if sig.inventory_days and sig.inventory_days < 14:
            alerts.append(f"库存仅剩 {sig.inventory_days:.0f} 天")
        if sig.threat_score and sig.threat_score > 50:
            alerts.append(f"竞争威胁分 {sig.threat_score:.0f}，需关注竞品动态")
    if data.ad_data:
        if data.ad_data.acos and data.ad_data.acos > 40:
            alerts.append(f"ACOS {data.ad_data.acos:.0f}% 严重偏高")
    if not alerts:
        alerts.append("当前无异常告警信号")

    # 关键词监控表 — 合并 AI 分类结果
    wf = ctx.state.get_workflow_state(asin)
    strategy_saved = all(k in long_term for k in ("product_level", "product_stage", "season_stage"))
    ka_raw = wf.get("keyword_analysis")
    ka_current = ka_raw.get(str(days)) if isinstance(ka_raw, dict) else ka_raw
    if not ka_current and strategy_saved and not is_llm_blocked(
        evaluate_completeness("tactics", data)
    ):
        # 回访已有策略的 ASIN 时，独立获取关键词 AI 分类
        try:
            from app.llm.purpose_adapter import recommend_tactics_from_purpose
            rec = await recommend_tactics_from_purpose(
                data=data,
                position=long_term.get("product_level", "常规产品"),
                stage=long_term.get("product_stage", "推进期"),
                season=long_term.get("season_stage", "淡季"),
                days=days,
            )
            if "error" not in rec:
                if isinstance(ka_raw, dict):
                    ka_raw[str(days)] = rec.get("keyword_analysis", [])
                else:
                    ka_raw = {str(days): rec.get("keyword_analysis", [])}
                wf["keyword_analysis"] = ka_raw
                ctx.state.set_workflow_state(asin, wf)
        except Exception as e:
            logger.warning("诊断层关键词 AI 分类失败 [%s]: %s", asin, e)
    ai_kw_map = {}
    ka_final = ka_raw.get(str(days)) if isinstance(ka_raw, dict) else ka_raw
    for ak in (ka_final or []):
        ai_kw_map[ak.get("word", "")] = ak
    keyword_list = []
    for kw in data.keywords[:20]:
        ai = ai_kw_map.get(kw.keyword, {})
        keyword_list.append({
            "keyword": kw.keyword,
            "natural_rank": kw.natural_rank,
            "near_natural_rank": kw.near_natural_rank,
            "sp_rank": kw.sp_rank,
            "rank_change_14d": kw.rank_change_14d,
            "rank_change_7d": kw.rank_change_7d,
            "spend": kw.spend,
            "acos": kw.acos,
            "cvr": kw.cvr,
            "bid": kw.bid,
            "match_type": kw.match_type,
            "keyword_class": ai.get("keyword_class", ""),
            "action": ai.get("action", ""),
        })
    # 趋势数据
    trend_list = [
        {"date": tp.date, "acos": tp.acos, "cvr": tp.cvr,
         "ctr": tp.ctr, "cpc": tp.cpc, "orders": tp.orders,
         "ad_orders": tp.ad_orders, "spend": tp.spend}
        for tp in data.trend
    ] if data.trend else []

    diag_summary = build_data_summary(data, days=days)
    manual_acos = ctx.state.get_target_acos_override(asin)
    if manual_acos is not None:
        diag_summary["acos_target"] = manual_acos
    return DiagnosisResponse(
        asin=asin,
        data_summary=diag_summary,
        data_completeness={
            "status": "missing" if data.data_missing else "complete",
            "missing_fields": data.missing_fields,
        },
        strategy_context=strategy_ctx,
        tactics_context=tactics_ctx,
        alert_signals=alerts,
        metric_board=metric_board,
        query_plan=query_plan,
        keywords=keyword_list,
        trend_data=trend_list,
        **data_status_fields(data),
    )

# ── Layer 1.4 执行层 ─────────────────────────────────

