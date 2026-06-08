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
from app.workflow.data_contract import blocked_message
from app.workflow.data_gate import ensure_data_for_llm, is_llm_blocked
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
TACTICS_META_FILTER = get_meta_filter("tactics")
PURPOSE_TIMEOUT = 120


def _read_cached_scores(wf: dict, days: int) -> list:
    ts = wf.get("target_scores")
    if isinstance(ts, dict):
        return ts.get(str(days), []) or []
    if isinstance(ts, list):
        return ts
    return []


def _scores_stale_vs_metrics(cached_scores: list, metrics: dict) -> bool:
    """缓存的 AI 评分是否基于「空数据」生成，而当前 metrics 已有订单/花费。"""
    if not cached_scores:
        return True
    orders = int(metrics.get("total_orders") or 0)
    spend = float(metrics.get("total_spend") or 0)
    if orders <= 0 and spend <= 0:
        return False
    blob = " ".join((s.get("reason") or "") for s in cached_scores)
    stale_markers = ("订单为0", "订单=0", "无任何销售", "花费为0", "花费为 0")
    return any(m in blob for m in stale_markers)


def _derive_ad_purposes_from_scores(wf: dict, days: int) -> list:
    """从缓存的 target_scores 中提取 level="推荐" 的广告目的作为侧边栏 AI 推荐标签。"""
    ts = _read_cached_scores(wf, days)
    if not ts:
        return []
    target_map = {"Traffic": "引流型", "Conversion": "转化型", "Ranking": "排名型", "Profit": "盈利型"}
    result = []
    for s in ts:
        if not isinstance(s, dict):
            continue
        if s.get("level") == "推荐":
            cn = target_map.get(s.get("target", ""), "")
            if cn:
                result.append(cn)
    return result[:2]


def _build_fallback_target_scores() -> list:
    """LLM 失败时生成占位评分，全部不推荐，引导用户点击「AI 重新推荐」。"""
    msg = "AI 评分服务暂不可用，请点击右下方「AI 重新推荐」重试"
    return [
        {"target": "Traffic", "level": "不推荐", "reason": msg},
        {"target": "Conversion", "level": "不推荐", "reason": msg},
        {"target": "Ranking", "level": "不推荐", "reason": msg},
        {"target": "Profit", "level": "不推荐", "reason": msg},
    ]


async def _run_purpose_and_cache(
    ctx: WorkflowContext,
    asin: str,
    wf: dict,
    data: ASINData,
    strategy_context: StrategyConfirmRequest,
    days: int,
    tactics_saved: bool,
) -> tuple[dict, str, list]:
    """调用 purpose-agent 并写入 keyword_analysis / target_scores 缓存。"""
    from app.llm.purpose_adapter import recommend_tactics_from_purpose

    try:
        rec = await asyncio.wait_for(
            recommend_tactics_from_purpose(
                data=data,
                position=strategy_context.product_level,
                stage=strategy_context.product_stage,
                season=strategy_context.season_stage,
                days=days,
            ),
            timeout=PURPOSE_TIMEOUT,
        )
    except asyncio.TimeoutError as e:
        raise ValueError(f"purpose-agent timeout after {PURPOSE_TIMEOUT}s") from e
    if "error" in rec:
        raise ValueError(rec["error"])
    recommendations = {
        "ad_purposes": rec.get("ad_purposes", []),
        "target_keyword_strategy": rec.get("target_keyword_strategy", []),
    } if not tactics_saved else {"ad_purposes": [], "target_keyword_strategy": []}
    reasoning = rec.get("reason", "") if not tactics_saved else ""
    ai_kw_map = {a.get("word", ""): a for a in rec.get("keyword_analysis", [])}
    merged_kws = []
    for kw in data.keywords[:20]:
        ai = ai_kw_map.get(kw.keyword, {})
        merged_kws.append({
            "word": kw.keyword,
            "rank": kw.natural_rank,
            "near_rank": kw.near_natural_rank,
            "rank_change": kw.rank_change_14d or 0,
            "rank_change_14d": kw.rank_change_14d or 0,
            "rank_change_7d": kw.rank_change_7d,
            "keyword_class": ai.get("keyword_class", ""),
            "action": ai.get("action", ""),
        })
    if isinstance(wf.get("keyword_analysis"), dict):
        wf["keyword_analysis"][str(days)] = merged_kws
    else:
        wf["keyword_analysis"] = {str(days): merged_kws}
    new_scores = rec.get("target_scores") or []
    ts = wf.get("target_scores") or {}
    if not isinstance(ts, dict):
        ts = {}
    ts[str(days)] = new_scores
    wf["target_scores"] = ts
    ctx.state.set_workflow_state(asin, wf)
    return recommendations, reasoning, merged_kws


async def run_get_tactics_options(ctx: WorkflowContext, asin: str, days: int = 7) -> TacticsOptionsResponse:
    """返回策略选项

    首次访问（无 keyword_analysis 缓存）：调用 purpose-agent LLM，产出推荐 + 关键词分类
    回访（keyword_analysis 已缓存）：跳过 LLM，从缓存读，仅刷新 DB 排名字段
    """
    long_term = ctx.state.get_long_term_config(asin)
    strategy_saved = all(k in long_term for k in ("product_level", "product_stage", "season_stage"))
    tactics_saved = all(k in long_term for k in ("ad_purposes", "target_keyword_strategy"))

    strategy_context = StrategyConfirmRequest(
        asin=asin,
        product_level=long_term.get("product_level", "常规产品 (P2)"),
        product_stage=long_term.get("product_stage", "推进期"),
        season_stage=long_term.get("season_stage", "淡季"),
    ) if strategy_saved else None

    layer_config = settings.layer_options_config or {}
    tactics_cfg = layer_config.get("tactics", {})

    recommendations = {"ad_purposes": [], "target_keyword_strategy": []}
    reasoning = ""
    data = None
    scoring_error = ""  # purpose-agent 失败时记录，透传前端用于提示重试
    llm_status = "ok"
    completeness_dict: dict = {}

    if strategy_saved:
        wf = ctx.state.get_workflow_state(asin)
        # 按 days 维度读取 keyword_analysis（兼容旧格式）
        kw_raw = wf.get("keyword_analysis")
        if isinstance(kw_raw, list):
            kw_cache = kw_raw  # 旧格式：直接 list
        elif isinstance(kw_raw, dict):
            kw_cache = kw_raw.get(str(days))
        else:
            kw_cache = None
        has_kw_cache = kw_cache is not None

        data, verdict = await ensure_data_for_llm(
            ctx, "tactics", asin, days=days, meta_filter=TACTICS_META_FILTER,
        )
        llm_status = verdict.status
        completeness_dict = verdict.to_completeness_dict()
        if is_llm_blocked(verdict):
            scoring_error = blocked_message(verdict)
        elif has_kw_cache:
            from app.data.field_mapping import asin_data_to_metrics

            metrics = asin_data_to_metrics(data, days)
            cached_scores = _read_cached_scores(wf, days)
            if _scores_stale_vs_metrics(cached_scores, metrics):
                logger.info(
                    "AI 评分缓存与当前数据不一致（曾有「订单为0」），重新生成 [%s]",
                    asin,
                )
                try:
                    recommendations, reasoning, _ = await _run_purpose_and_cache(
                        ctx, asin, wf, data, strategy_context, days, tactics_saved,
                    )
                    # 评分重新生成成功，从新评分推导侧边栏 AI 推荐（避免 tactics_saved 时返回空）
                    rec_from_new = _derive_ad_purposes_from_scores(wf, days)
                    if rec_from_new:
                        recommendations["ad_purposes"] = rec_from_new
                except Exception as e:
                    logger.warning("purpose-agent 重新评分失败 [%s]: %s", asin, e)
                    scoring_error = f"AI 评分服务暂不可用：{e}"
                    # 写兜底评分，防止前端评分卡片静默空白
                    if not _read_cached_scores(wf, days):
                        ts_dict = wf.get("target_scores") or {}
                        if not isinstance(ts_dict, dict):
                            ts_dict = {}
                        ts_dict[str(days)] = _build_fallback_target_scores()
                        wf["target_scores"] = ts_dict
                        ctx.state.set_workflow_state(asin, wf)
                    recommendations = {
                        "ad_purposes": long_term.get("ad_purposes", []),
                        "target_keyword_strategy": long_term.get("target_keyword_strategy", []),
                    }
            else:
                ai_kw_map = {a.get("word", ""): a for a in kw_cache}
                merged_kws = []
                for kw in data.keywords[:20]:
                    ai = ai_kw_map.get(kw.keyword, {})
                    merged_kws.append({
                        "word": kw.keyword,
                        "rank": kw.natural_rank,
                        "near_rank": kw.near_natural_rank,
                        "rank_change": kw.rank_change_14d or 0,
                        "rank_change_14d": kw.rank_change_14d or 0,
                        "rank_change_7d": kw.rank_change_7d,
                        "keyword_class": ai.get("keyword_class", ""),
                        "action": ai.get("action", ""),
                    })
                if isinstance(wf.get("keyword_analysis"), dict):
                    wf["keyword_analysis"][str(days)] = merged_kws
                else:
                    wf["keyword_analysis"] = {str(days): merged_kws}
                ctx.state.set_workflow_state(asin, wf)
                # 侧边栏 AI 推荐标签从评分推导，与评分卡片保持一致
                rec_from_scores = _derive_ad_purposes_from_scores(wf, days)
                recommendations = {
                    "ad_purposes": rec_from_scores if rec_from_scores else long_term.get("ad_purposes", []),
                    "target_keyword_strategy": long_term.get("target_keyword_strategy", []),
                }
        else:
            try:
                recommendations, reasoning, _ = await _run_purpose_and_cache(
                    ctx, asin, wf, data, strategy_context, days, tactics_saved,
                )
            except Exception as e:
                logger.warning("策略层 purpose-agent 推荐失败 [%s]: %s", asin, e)
                scoring_error = f"AI 评分服务暂不可用：{e}"
                # LLM 失败时，用 DB 关键词填充 keyword_analysis（无 AI 分类）
                if 'data' in dir() and data and not data.data_missing:
                    fallback_kws = []
                    for kw in data.keywords[:20]:
                        fallback_kws.append({
                            "word": kw.keyword, "rank": kw.natural_rank,
                            "near_rank": kw.near_natural_rank,
                            "rank_change": kw.rank_change_14d or 0,
                            "rank_change_14d": kw.rank_change_14d or 0,
                            "rank_change_7d": kw.rank_change_7d,
                            "keyword_class": "", "action": "",
                        })
                    wf["keyword_analysis"] = {str(days): fallback_kws}
                    # 写兜底评分，防止前端评分卡片静默空白
                    ts_dict = wf.get("target_scores") or {}
                    if not isinstance(ts_dict, dict):
                        ts_dict = {}
                    ts_dict[str(days)] = _build_fallback_target_scores()
                    wf["target_scores"] = ts_dict
                    ctx.state.set_workflow_state(asin, wf)

    dimensions = []
    for dim_key in ("ad_purposes", "target_keyword_strategy"):
        dim_cfg = tactics_cfg.get(dim_key, {})
        rec_ids = recommendations.get(dim_key, [])
        dimensions.append(TacticsDimension(
            id=dim_key,
            label=dim_cfg.get("label", dim_key),
            description=dim_cfg.get("description", ""),
            options=dim_cfg.get("options", []),
            selection_type=dim_cfg.get("selection_type", "multi_select"),
            recommendations=rec_ids,
            recommendation_reason=reasoning if dim_key == "ad_purposes" else "",
        ))

    current = ctx.state.get_long_term_config(asin)

    # 读取保存的 AI 诊断附加数据（按 days 维度）
    wf = ctx.state.get_workflow_state(asin)
    ts = wf.get("target_scores") or []
    if isinstance(ts, dict):
        ts = ts.get(str(days), [])
    elif not isinstance(ts, list):
        ts = []
    ka = wf.get("keyword_analysis") or []
    if isinstance(ka, dict):
        ka = ka.get(str(days), [])
    elif not isinstance(ka, list):
        ka = []
    status = data_status_fields(data) if data is not None else {
        "partial_failures": [],
        "data_freshness": "fresh",
    }
    return TacticsOptionsResponse(
        asin=asin,
        dimensions=dimensions,
        strategy_context=strategy_context,
        current_selection={
            "ad_purposes": current.get("ad_purposes"),
            "target_keyword_strategy": current.get("target_keyword_strategy"),
        } if current else None,
        target_scores=ts,
        keyword_analysis=ka,
        scoring_error=scoring_error,
        llm_status=llm_status,
        data_completeness=completeness_dict,
        **status,
    )

async def run_get_tactics_recommendations(ctx: WorkflowContext, asin: str, days: int = 7) -> dict:
    """强制 AI 重新推荐策略选项（不保存，仅返回推荐结果）

    由前端「AI 重新推荐」按钮触发，调用 purpose-agent。
    """
    long_term = ctx.state.get_long_term_config(asin)
    data, verdict = await ensure_data_for_llm(
        ctx, "tactics", asin, days=days, meta_filter=TACTICS_META_FILTER,
    )

    if is_llm_blocked(verdict):
        wf = ctx.state.get_workflow_state(asin)
        ka = wf.get("keyword_analysis") or []
        if isinstance(ka, dict):
            ka = ka.get(str(days), [])
        elif not isinstance(ka, list):
            ka = []
        ts = wf.get("target_scores") or []
        if isinstance(ts, dict):
            ts = ts.get(str(days), [])
        elif not isinstance(ts, list):
            ts = []
        msg = blocked_message(verdict)
        return {
            "asin": asin,
            "status": "blocked",
            "llm_status": "blocked",
            "message": msg,
            "data_completeness": verdict.to_completeness_dict(),
            "dimensions": [],
            "reasoning": "",
            "keyword_analysis": ka,
            "target_scores": ts,
            "error": msg,
        }

    from app.llm.purpose_adapter import recommend_tactics_from_purpose
    try:
        rec = await asyncio.wait_for(
            recommend_tactics_from_purpose(
                data=data,
                position=long_term.get("product_level", "常规产品 (P2)"),
                stage=long_term.get("product_stage", "推进期"),
                season=long_term.get("season_stage", "淡季"),
                days=days,
            ),
            timeout=PURPOSE_TIMEOUT,
        )
    except asyncio.TimeoutError:
        rec = {"error": f"purpose-agent timeout after {PURPOSE_TIMEOUT}s"}

    # purpose-agent 失败时不清空已有缓存
    if "error" in rec:
        wf = ctx.state.get_workflow_state(asin)
        ka = wf.get("keyword_analysis") or []
        if isinstance(ka, dict):
            ka = ka.get(str(days), [])
        elif not isinstance(ka, list):
            ka = []
        ts = wf.get("target_scores") or []
        if isinstance(ts, dict):
            ts = ts.get(str(days), [])
        elif not isinstance(ts, list):
            ts = []
        return {
            "asin": asin,
            "dimensions": [],
            "reasoning": "",
            "keyword_analysis": ka,
            "target_scores": ts,
            "error": rec["error"],
        }

    layer_config = settings.layer_options_config or {}
    tactics_cfg = layer_config.get("tactics", {})
    reasoning = rec.get("reason", "")

    dimensions = []
    for dim_key in ("ad_purposes", "target_keyword_strategy"):
        dim_cfg = tactics_cfg.get(dim_key, {})
        rec_ids = rec.get(dim_key, [])
        dimensions.append({
            "id": dim_key,
            "label": dim_cfg.get("label", dim_key),
            "recommendations": rec_ids,
            "recommendation_reason": reasoning if dim_key == "ad_purposes" else "",
        })

    # 同时刷新 keyword_analysis 和 target_scores 缓存
    ai_kw_map = {a.get("word", ""): a for a in rec.get("keyword_analysis", [])}
    merged_kws = []
    for kw in data.keywords[:20]:
        ai = ai_kw_map.get(kw.keyword, {})
        merged_kws.append({
            "word": kw.keyword,
            "rank": kw.natural_rank,
            "near_rank": kw.near_natural_rank,
            "rank_change": kw.rank_change_14d or 0,
            "rank_change_14d": kw.rank_change_14d or 0,
            "rank_change_7d": kw.rank_change_7d,
            "keyword_class": ai.get("keyword_class", ""),
            "action": ai.get("action", ""),
        })
    wf = ctx.state.get_workflow_state(asin)
    wf["keyword_analysis"] = {str(days): merged_kws}
    new_scores = rec.get("target_scores") or []
    ts = wf.get("target_scores") or {}
    if not isinstance(ts, dict):
        ts = {}
    ts[str(days)] = new_scores
    wf["target_scores"] = ts
    ctx.state.set_workflow_state(asin, wf)

    return {
        "asin": asin,
        "llm_status": verdict.status,
        "data_completeness": verdict.to_completeness_dict(),
        "dimensions": dimensions,
        "reasoning": reasoning,
        "keyword_analysis": merged_kws,
        "target_scores": rec.get("target_scores", []),
    }

async def run_confirm_tactics(ctx: WorkflowContext, req: TacticsConfirmRequest) -> TacticsConfirmResponse:
    config = {
        "ad_purposes": [p.value for p in req.ad_purposes],
        "target_keyword_strategy": [k.value for k in req.target_keyword_strategy],
    }
    ctx.state.set_long_term_config(req.asin, config)
    ctx.state.advance_layer(req.asin, "diagnosis")

    return TacticsConfirmResponse(
        asin=req.asin,
        accepted=True,
        config_saved=True,
    )

# ── Layer 1.3 诊断层 ─────────────────────────────────

