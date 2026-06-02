"""Campaign LLM 分析引擎 — 分批 + 两轮投票 + 可选 Round3 + sanity_check。

复用资产:
- CampaignFetcher.fetch_campaigns() → CampaignData
- LLMReasoner.recommend_campaign_batch() → 单批 LLM 分析
- kb.build("campaign_adjustment") → KB 18/19/21/22
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from app.config.settings import settings
from app.data.campaign_fetcher import CampaignFetcher
from app.models.asin_data import ASINData
from app.models.campaign import (
    CampaignAdjustmentItem,
    CampaignAnalysisResult,
    CampaignBatchResult,
    CampaignData,
    CampaignStrategyContext,
    CampaignUnit,
)
if TYPE_CHECKING:
    from app.llm.reasoner import LLMReasoner
    from app.workflow.context import WorkflowContext

logger = logging.getLogger(__name__)

LLM_TIMEOUT = 60  # 单批 LLM 超时 (秒)

# Sanity / Synthesis 开关（2026-06-01 恢复）
# 修复方式：去掉外层 asyncio.wait_for（Windows 取消不生效），改用 chat(timeout_override=)
# 由 httpx socket 层超时接管，不依赖 asyncio 取消
_SANITY_CHECK_ENABLED = True
_SYNTHESIS_ENABLED = True

TYPE_MAP: dict[str, str] = {
    "broad": "大词", "long_tail": "长尾词", "long-tail": "长尾词",
    "competitor": "竞品词", "brand": "品牌词", "custom": "自定义",
}


# ── 运行互斥 & 数据缓存 ──────────────────────────────────────────────────

# 进程内幂等表：asin → (run_id, start_monotonic)
# 注意：仅单 worker 有效；多 worker 部署需换 Redis SET NX EX 才能跨进程互斥。
_running: dict[str, tuple[str, float]] = {}
_RUNNING_TTL = 600.0  # 僵尸条目兜底：超 10 分钟视为已死，放行新请求


async def _get_redis():
    """懒加载 Redis 客户端，不可用时返回 None。"""
    import redis.asyncio as aioredis
    from app.config.settings import settings

    try:
        r = aioredis.from_url(settings.redis_url, encoding="utf-8", decode_responses=True)
        await r.ping()
        return r
    except Exception:
        return None


def _campaign_cache_key(asin: str, days: int) -> str:
    # 注意：本部署内 parent_asin → 单店铺（resolve_mcp_context_from_db 解析）。
    # 若未来同一 parent_asin 跨店铺复用，需在 key 中加入 shop_account 前缀防串店。
    return f"campaign:data:{asin}:{days}"


async def _load_cached_campaigns(asin: str, days: int) -> CampaignData | None:
    """从 Redis 读取缓存的 CampaignData。"""
    import json as _json
    r = await _get_redis()
    if not r:
        return None
    try:
        raw = await r.get(_campaign_cache_key(asin, days))
        if raw:
            return CampaignData.model_validate(_json.loads(raw))
    except Exception:
        pass
    return None


async def _save_cached_campaigns(asin: str, days: int, data: CampaignData) -> None:
    """将 CampaignData 写入 Redis 缓存，TTL 30 分钟。"""
    import json as _json
    r = await _get_redis()
    if not r:
        return
    try:
        await r.setex(
            _campaign_cache_key(asin, days),
            1800,
            _json.dumps(data.model_dump()),
        )
    except Exception:
        pass


# ── 公开入口 ────────────────────────────────────────────────────────────────


async def analyze_campaigns(
    fetcher: CampaignFetcher,
    reasoner: "LLMReasoner",
    parent_asin: str,
    asin_data: ASINData,
    strategy_context: CampaignStrategyContext,
    *,
    days: int = 7,
    batch_size: int | None = None,
    concurrency: int | None = None,
    temperature: float = 0.3,
    refresh: bool = False,
    campaign_data: CampaignData | None = None,
    keyword_analysis: dict | None = None,
) -> CampaignAnalysisResult:
    """完整 LLM 分析：拉数据 → 分批 → R1+R2 → 投票 → (R3) → sanity_check。

    实验脚本直接调用此函数，无需 WorkflowContext。
    campaign_data 可预取后复用；keyword_analysis 用于逐词 keyword_class 富化。
    """

    bs = batch_size or settings.campaign_batch_size
    cc = concurrency or settings.campaign_llm_concurrency
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    t_start = time.monotonic()
    _t = lambda label: logger.info("Campaign timing [%s] +%.1fs: %s", parent_asin, time.monotonic() - t_start, label)

    # 0. 幂等检查：同 ASIN 已跑且未超 TTL 则拒绝（超 TTL 视为僵尸条目，放行）
    now = time.monotonic()
    existing = _running.get(parent_asin)
    if existing and (now - existing[1]) < _RUNNING_TTL:
        logger.warning("Campaign 幂等拦截 [%s]: 已有 run_id=%s 运行中 (%.0fs)",
                       parent_asin, existing[0], now - existing[1])
        return CampaignAnalysisResult(
            parent_asin=parent_asin, days=days, run_id=run_id,
            total_campaigns=0,
            warnings=[f"该 ASIN 已有分析正在运行 (run_id={existing[0]})，请等待完成后重试"],
            sanity_check_passed=False,
        )
    _running[parent_asin] = (run_id, now)

    try:
        result = await _analyze_campaigns_impl(
            fetcher=fetcher, reasoner=reasoner, parent_asin=parent_asin,
            asin_data=asin_data, strategy_context=strategy_context,
            days=days, bs=bs, cc=cc, temperature=temperature, refresh=refresh,
            campaign_data=campaign_data, keyword_analysis=keyword_analysis,
            run_id=run_id, _t=_t,
        )
        return result
    finally:
        _running.pop(parent_asin, None)


async def _analyze_campaigns_impl(
    fetcher: CampaignFetcher,
    reasoner: "LLMReasoner",
    parent_asin: str,
    asin_data: ASINData,
    strategy_context: CampaignStrategyContext,
    *,
    days: int,
    bs: int,
    cc: int,
    temperature: float,
    refresh: bool,
    campaign_data: CampaignData | None,
    keyword_analysis: dict | None,
    run_id: str,
    _t,
) -> CampaignAnalysisResult:

    # 1. 获取活动数据 — 优先 Redis 缓存（refresh=True 时跳过），miss 时拉 MCP/Doris
    if campaign_data is None:
        if not refresh:
            campaign_data = await _load_cached_campaigns(parent_asin, days)
        if campaign_data:
            _t("DONE fetch_campaigns (redis hit)")
        else:
            try:
                campaign_data = await asyncio.wait_for(
                    fetcher.fetch_campaigns(parent_asin, days=days),
                    timeout=300,
                )
                await _save_cached_campaigns(parent_asin, days, campaign_data)
                _t("DONE fetch_campaigns (fetched)")
            except asyncio.TimeoutError:
                logger.warning("fetch_campaigns 超时 [%s] >300s", parent_asin)
                return CampaignAnalysisResult(
                    parent_asin=parent_asin, days=days, run_id=run_id,
                    total_campaigns=0,
                    warnings=[f"获取活动数据超时 (>300s)，请重试"],
                    sanity_check_passed=False,
                )
            except Exception as e:
                logger.exception("fetch_campaigns 异常 [%s]: %s", parent_asin, e)
                return CampaignAnalysisResult(
                    parent_asin=parent_asin, days=days, run_id=run_id,
                    total_campaigns=0,
                    warnings=[f"获取活动数据失败: {type(e).__name__}: {e}"],
                    sanity_check_passed=False,
                )

    if campaign_data.total_campaigns == 0:
        return CampaignAnalysisResult(
            parent_asin=parent_asin, days=days, run_id=run_id,
            total_campaigns=0,
            warnings=[f"ASIN {parent_asin} 无可用广告活动"],
            rounds_detail={},
        )

    # 2. 构建 per-keyword 富化映射
    keyword_class_map: dict[str, str] = {}
    if keyword_analysis:
        for window_key, entries in keyword_analysis.items():
            if isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, dict):
                        kw = entry.get("keyword", "")
                        st = entry.get("keyword_class", "")
                        if kw and st:
                            keyword_class_map[kw] = TYPE_MAP.get(st, st)

    # 3. LLM 分析前预过滤
    # 3a. 批量词活动：一个活动名下多个关键词，本期暂不处理
    #     → 已在 CampaignFetcher 硬过滤阶段排除 (campaign_prefilter.py:61-68)
    # 3b. 疑似已淘汰活动：预算 ≈ $1.00 且 Bid ≈ $0.20
    #     → 符合 KB 21 §4 淘汰池执行值特征，本期暂时过滤不做重复分析
    llm_campaigns: list[CampaignUnit] = []
    skipped_eliminated: list[dict] = []
    for cu in campaign_data.campaigns:
        if (cu.current_budget is not None and cu.current_bid is not None
                and 0.99 <= cu.current_budget <= 1.01
                and 0.19 <= cu.current_bid <= 0.21):
            skipped_eliminated.append({
                "campaign_key": cu.campaign_key,
                "campaign_name": cu.campaign_name,
                "child_asin": cu.child_asin,
                "match_type": cu.match_type,
                "keyword_text": cu.keyword_text,
                "reason": "suspected_eliminated",
            })
            continue
        llm_campaigns.append(cu)

    if skipped_eliminated:
        logger.info("Campaign 预过滤 [%s]: 跳过 %d 个疑似已淘汰活动 (budget≈$1, bid≈$0.2)",
                     parent_asin, len(skipped_eliminated))

    total = len(llm_campaigns)
    if total == 0:
        return CampaignAnalysisResult(
            parent_asin=parent_asin, days=days, run_id=run_id,
            total_campaigns=0,
            skipped_campaigns=skipped_eliminated,
            warnings=["所有活动均在预过滤阶段被排除（疑似全部已淘汰）"],
            rounds_detail={},
        )

    # 4. 按 match_type 分流
    exact_list = [cu for cu in llm_campaigns if cu.match_type == "EXACT"]
    broad_list = [cu for cu in llm_campaigns if cu.match_type != "EXACT"]
    _t(f"split: exact={len(exact_list)} broad={len(broad_list)}")

    ctx_dict = strategy_context.model_dump()
    exact_sem = asyncio.Semaphore(10)
    broad_sem = asyncio.Semaphore(10)
    rounds_detail: dict[str, dict] = {}
    warnings_list: list[str] = []

    # 5. 精准流 + 广泛流并行分析（各自独立限流 10，互不阻塞）
    # return_exceptions=True：一流抛未捕获异常 → 不连累另一流，转为 warning
    stream_results = await asyncio.gather(
        _analyze_one_stream(
            exact_list, "exact", reasoner, fetcher, parent_asin, days,
            strategy_context, keyword_class_map, bs, exact_sem, temperature, ctx_dict,
        ),
        _analyze_one_stream(
            broad_list, "broad", reasoner, fetcher, parent_asin, days,
            strategy_context, keyword_class_map, bs, broad_sem, temperature, ctx_dict,
        ),
        return_exceptions=True,
    )

    def _unpack_stream(r, label):
        if isinstance(r, BaseException):
            logger.exception("Stream %s 异常 [%s]: %s", label, parent_asin, r)
            warnings_list.append(f"{label} 流分析异常: {type(r).__name__}: {r}")
            return [], {}, [], []
        return r

    (exact_adjustments, exact_rd, exact_summaries, exact_skipped) = _unpack_stream(stream_results[0], "exact")
    (broad_adjustments, broad_rd, broad_summaries, broad_skipped) = _unpack_stream(stream_results[1], "broad")

    rounds_detail["exact"] = exact_rd
    rounds_detail["broad"] = broad_rd
    _t("DONE exact+broad streams")

    # 6. 合并两流结果（含预过滤阶段疑似已淘汰的活动，供前端可见）
    adjustments = exact_adjustments + broad_adjustments
    skipped_campaigns = exact_skipped + broad_skipped + skipped_eliminated
    if skipped_campaigns:
        logger.warning(
            "Campaign analyze [%s]: %d 个活动未被分析（LLM 批次失败或未返回）",
            parent_asin, len(skipped_campaigns),
        )
    action_order = {"eliminate_to_low_bid_pool": 0, "adjust_bid": 1, "adjust_budget": 1, "adjust_placement": 1, "keep": 2}
    adjustments.sort(key=lambda x: action_order.get(x.action, 9))

    # 7. 预算冲突裁决
    budget_warnings = _resolve_budget_conflicts(adjustments)
    warnings_list.extend(budget_warnings)

    # 8. Sanity check（仅校验低置信项，分批并行避免 LLM 输出超 max_tokens）
    # sanity_ok 默认 False：未运行/有批次失败都按"未通过"展示，仅全批次成功才置 True
    sanity_ok = False
    if _SANITY_CHECK_ENABLED:
        try:
            sc_warnings, sanity_ok = await _sanity_check_batched(
                reasoner, parent_asin, adjustments,
                exact_summaries + broad_summaries,
                ctx_dict, temperature,
            )
            warnings_list.extend(sc_warnings)
            _t("DONE sanity_check")
        except Exception as e:
            logger.warning("Campaign sanity check 失败 [%s]: %s", parent_asin, e)
            warnings_list.append(f"sanity_check 执行失败: {e}")
            sanity_ok = False
    else:
        logger.info("Campaign sanity check 已禁用 [%s]", parent_asin)

    # 8b. AI 汇总合成 (按共同原因分组的运营叙事)
    # timeout_override=55：httpx socket 层自断，不依赖 asyncio 取消
    synthesis: dict | None = None
    if _SYNTHESIS_ENABLED and adjustments:
        try:
            synthesis = await reasoner.recommend_campaign_synthesis(
                asin=parent_asin,
                adjustments=adjustments,
                strategy_context=ctx_dict,
                temperature=temperature,
                timeout_override=55,
            )
            _t("DONE synthesis")
            if synthesis and synthesis.get("error"):
                warnings_list.append(f"AI 汇总合成失败: {synthesis['error']}")
        except Exception as e:
            logger.warning("Campaign synthesis 异常 [%s]: %s", parent_asin, e)
            warnings_list.append(f"AI 汇总合成异常: {e}")
            synthesis = None
    elif adjustments:
        logger.info("Campaign synthesis 已禁用 [%s]", parent_asin)

    # 9. 汇总统计
    summary_stats = {
        "to_eliminate": sum(1 for a in adjustments if a.action == "eliminate_to_low_bid_pool"),
        "to_adjust": sum(1 for a in adjustments if a.action.startswith("adjust")),
        "to_keep": sum(1 for a in adjustments if a.action == "keep"),
        "confidence_high": sum(1 for a in adjustments if a.confidence == "high"),
        "confidence_medium": sum(1 for a in adjustments if a.confidence == "medium"),
        "confidence_low": sum(1 for a in adjustments if a.confidence == "low"),
        "estimated_budget_impact": round(sum(
            (a.proposed_budget or 0) - (a.current_budget or 0)
            for a in adjustments
        ), 2),
    }

    _t("DONE total")
    return CampaignAnalysisResult(
        parent_asin=parent_asin, days=days, run_id=run_id,
        total_campaigns=len(llm_campaigns),
        adjustments=adjustments,
        skipped_campaigns=skipped_campaigns,
        synthesis=synthesis,
        summary=summary_stats,
        warnings=warnings_list,
        sanity_check_passed=sanity_ok,
        llm_rounds_completed=2,
        rounds_detail=rounds_detail,
    )


async def run_campaign_analysis(
    ctx: "WorkflowContext",
    asin: str,
    *,
    days: int = 7,
    refresh: bool = False,
    temperature: float | None = None,
) -> CampaignAnalysisResult:
    """WorkflowContext 包装器 (未来 API 集成用)。

    模式对齐 p3.py:run_get_unified_recommendation。
    """
    from app.core.recommender import TargetAcosRecommender

    long_term = ctx.state.get_long_term_config(asin) or {}
    wf_state = ctx.state.get_workflow_state(asin) or {}
    keyword_analysis = wf_state.get("keyword_analysis", {})

    # ASIN 数据
    asin_data = await ctx.ensure_data(asin, days=days, refresh=refresh)

    # 组装策略上下文
    strat_ctx = build_campaign_strategy_context(
        asin, asin_data, long_term, keyword_analysis,
    )

    # target_acos: manual > P3 > algorithm
    manual_acos = ctx.state.get_target_acos_override(asin)
    if manual_acos is not None:
        strat_ctx.target_acos = int(manual_acos)
    else:
        p3 = ctx.state.get_p3_recommendation(asin)
        if p3 and p3.get("target_acos", {}).get("recommended_target"):
            strat_ctx.target_acos = int(p3["target_acos"]["recommended_target"])
        else:
            ad_purposes = long_term.get("ad_purposes", [])
            rec = TargetAcosRecommender().recommend(asin_data, ad_purposes)
            strat_ctx.target_acos = int(rec.recommended_target)

    fetcher = CampaignFetcher()
    temp = temperature if temperature is not None else settings.campaign_llm_temperature

    return await analyze_campaigns(
        fetcher=fetcher,
        reasoner=ctx.reasoner,
        parent_asin=asin,
        asin_data=asin_data,
        strategy_context=strat_ctx,
        days=days,
        temperature=temp,
        refresh=refresh,
        keyword_analysis=keyword_analysis,
    )


# ── 策略上下文组装 ──────────────────────────────────────────────────────────


def build_campaign_strategy_context(
    asin: str,
    asin_data: ASINData,
    long_term: dict,
    keyword_analysis: dict | None = None,
) -> CampaignStrategyContext:
    """从 ASINData + long_term_config + keyword_analysis 组装 ASIN 级上下文。

    字段映射（已核实）：
    - rating → asin_data.rating (非 signals.recent_rating_change)
    - refund_rate → asin_data.refund_rate
    - inventory_days → 计算值 (inventory_qty / avg_daily_sales_30d)
    - target_keyword_strategy → long_term.get("target_keyword_strategy", [])
    """
    flags: list[str] = []

    # 库存天数计算
    inventory_days: float | None = None
    qty = asin_data.signals.inventory_qty if asin_data.signals else None
    sales = asin_data.avg_daily_sales_30d
    if qty is not None and sales is not None and sales > 0:
        inventory_days = round(qty / sales, 1)

    if inventory_days is not None and inventory_days < 7:
        flags.append(f"库存仅 {inventory_days:.0f} 天 (<7天，阻断 TOS/PP 广告位上调)")

    if asin_data.rating is not None and asin_data.rating < 3.8:
        flags.append(f"评分 {asin_data.rating} (<3.8，阻断 TOS 广告位上调)")

    if asin_data.refund_rate is not None and asin_data.refund_rate >= 25:
        flags.append(f"退货率 {asin_data.refund_rate:.1f}% (≥25%，阻断 TOS 广告位上调)")

    discount = long_term.get("discount_rate")
    if discount is not None:
        flags.append(f"折扣率 {discount}% (Listing优化中，禁止大动作)")

    return CampaignStrategyContext(
        parent_asin=asin,
        product_stage=long_term.get("product_stage", ""),
        product_level=long_term.get("product_level", ""),
        season_stage=long_term.get("season_stage", ""),
        ad_purposes=long_term.get("ad_purposes", []),
        target_keyword_strategy=long_term.get("target_keyword_strategy", []),
        margin=asin_data.margin,
        natural_order_ratio=asin_data.natural_order_ratio,
        rating=asin_data.rating,
        refund_rate=asin_data.refund_rate,
        inventory_qty=qty,
        inventory_days=inventory_days,
        avg_daily_sales_30d=asin_data.avg_daily_sales_30d,
        target_acos=None,  # 由调用方填充 (resolve_target_acos)
        warning_flags=flags,
    )


# ── 单流分析 ────────────────────────────────────────────────────────────────


async def _analyze_one_stream(
    campaigns: list[CampaignUnit],
    task_type: str,
    reasoner: "LLMReasoner",
    fetcher: CampaignFetcher,
    parent_asin: str,
    days: int,
    strategy_context: CampaignStrategyContext,
    keyword_class_map: dict[str, str],
    batch_size: int,
    sem: asyncio.Semaphore,
    temperature: float,
    ctx_dict: dict,
) -> tuple[list[CampaignAdjustmentItem], dict, list[dict], list[dict]]:
    """单流全流程: summaries → unit_lookup → 预取 → 分批 → R1+R2 → 投票 → (R3) → 合并。

    返回 (adjustments, rounds_detail, enriched_summaries, skipped_campaigns)。
    skipped = 整批 LLM 失败或未返回 item 的活动（运营需人工补救）。
    """
    if not campaigns:
        return [], {}, [], []
    t0 = time.monotonic()
    _st = lambda label: logger.info("Stream timing [%s|%s] +%.1fs: %s", parent_asin, task_type, time.monotonic() - t0, label)

    # 1. 构建 summaries + unit_lookup
    summaries = [
        reasoner._campaign_to_prompt_dict(
            cu,
            target_acos=strategy_context.target_acos,
            keyword_class=keyword_class_map.get(cu.keyword_text, ""),
            is_core=False,
        )
        for cu in campaigns
    ]
    unit_lookup = _build_unit_lookup(campaigns)

    # 2. 预取（精准流只拉 placement，广泛流只拉 search_term）
    if task_type == "exact":
        summaries = await _prefetch_placement(
            fetcher, parent_asin, days, summaries, unit_lookup,
        )
    else:
        summaries = await _prefetch_search_terms(
            fetcher, parent_asin, days, summaries, unit_lookup,
        )
    _st(f"DONE prefetch ({len(campaigns)} campaigns)")

    # 3. 分批 + R1+R2（少于 2 批时跳过投票，单轮直出）
    if len(campaigns) < batch_size * 2:
        batches = _build_batches(summaries, batch_size, seed=1)
        r1 = await _run_round(
            reasoner, parent_asin, batches, ctx_dict, temperature, sem, 1,
            task_type=task_type,
        )
        adjustments: list[CampaignAdjustmentItem] = []
        for br in r1:
            for item in br.items:
                item.confidence = "medium"
                adjustments.append(item)
        skipped = _collect_skipped(campaigns, adjustments)
        return adjustments, {
            "round1": _round_stats(r1), "round2": None, "round3": None,
        }, summaries, skipped

    r1_batches = _build_batches(summaries, batch_size, seed=1)
    r2_batches = _build_batches(summaries, batch_size, seed=2)
    r1_results, r2_results = await asyncio.gather(
        _run_round(reasoner, parent_asin, r1_batches, ctx_dict, temperature, sem, 1, task_type=task_type),
        _run_round(reasoner, parent_asin, r2_batches, ctx_dict, temperature, sem, 2, task_type=task_type),
    )
    _st(f"DONE R1+R2 ({len(r1_batches)}+{len(r2_batches)} batches)")
    rd: dict = {
        "round1": _round_stats(r1_results),
        "round2": _round_stats(r2_results),
        "round3": {"ran": False},
    }

    # 4. 投票 + tiebreaker
    votes, needs_tiebreaker = _vote(r1_results, r2_results)
    if needs_tiebreaker:
        tiebreaker_summaries = [s for s in summaries if s.get("campaign_key") in needs_tiebreaker]
        if tiebreaker_summaries:
            r3_results = await _run_round(
                reasoner, parent_asin,
                _build_batches(tiebreaker_summaries, batch_size, seed=3),
                ctx_dict, temperature, sem, 3, task_type=task_type,
            )
            _resolve_tiebreaker(votes, r3_results, needs_tiebreaker)
            rd["round3"] = {
                "ran": True,
                "disputed_count": len(needs_tiebreaker),
                **_round_stats(r3_results),
            }

    adjustments = _merge_to_adjustments(votes, r1_results, r2_results)
    skipped = _collect_skipped(campaigns, adjustments)
    _st(f"DONE merge ({len(adjustments)} items, {len(skipped)} skipped)")
    return adjustments, rd, summaries, skipped


def _collect_skipped(
    campaigns: list[CampaignUnit],
    adjustments: list[CampaignAdjustmentItem],
) -> list[dict]:
    """对比期望 vs 实际产出，找出未被分析的活动（整批 LLM 失败时）。"""
    returned_keys = {item.campaign_key for item in adjustments if item.campaign_key}
    return [
        {
            "campaign_key": cu.campaign_key,
            "campaign_name": cu.campaign_name,
            "child_asin": cu.child_asin,
            "match_type": cu.match_type,
            "keyword_text": cu.keyword_text,
            "reason": "LLM 批次失败或未返回此活动",
        }
        for cu in campaigns
        if cu.campaign_key and cu.campaign_key not in returned_keys
    ]


# ── 分批与并发 ──────────────────────────────────────────────────────────────


def _build_batches(
    items: list[dict],
    batch_size: int,
    seed: int | None = None,
) -> list[list[dict]]:
    """随机打乱后按 batch_size 分块。不同 seed 产生不同分组。"""
    shuffled = list(items)
    if seed is not None:
        random.Random(seed).shuffle(shuffled)
    else:
        random.shuffle(shuffled)
    return [shuffled[i:i + batch_size] for i in range(0, len(shuffled), batch_size)]


async def _run_round(
    reasoner: "LLMReasoner",
    asin: str,
    batches: list[list[dict]],
    strategy_context: dict,
    temperature: float,
    sem: asyncio.Semaphore,
    round_number: int,
    task_type: str = "exact",
) -> list[CampaignBatchResult]:
    """执行一轮 LLM 调用 (所有 batch 并发，Semaphore 由调用方注入)。"""

    async def _call_one(batch_idx: int, batch: list[dict]) -> CampaignBatchResult:
        async with sem:
            try:
                result = await asyncio.wait_for(
                    reasoner.recommend_campaign_batch(
                        asin=asin,
                        campaign_summaries=batch,
                        strategy_context=strategy_context,
                        temperature=temperature,
                        task_type=task_type,
                    ),
                    timeout=LLM_TIMEOUT,
                )

                # 解析挪进 try：result 形状异常也会落到下面的 except，不外抛
                if not isinstance(result, dict):
                    raise ValueError(f"recommend_campaign_batch 返回非 dict: {type(result).__name__}")
                parsed = result.get("parsed") or {}
                raw_adj = parsed.get("campaign_adjustments", []) if isinstance(parsed, dict) else []
                items: list[CampaignAdjustmentItem] = []
                for adj in raw_adj:
                    try:
                        items.append(CampaignAdjustmentItem(**adj))
                    except Exception as e:
                        logger.warning("Campaign adjustment item 解析失败 batch=%d: %s", batch_idx, e)

                return CampaignBatchResult(
                    batch_id=batch_idx, round_number=round_number,
                    items=items,
                    raw_llm_output=result.get("raw_output", "") or "",
                    temperature=temperature,
                    llm_success=bool(result.get("success", False)),
                    llm_error=result.get("error", "") or "",
                )
            except asyncio.TimeoutError:
                return CampaignBatchResult(
                    batch_id=batch_idx, round_number=round_number,
                    llm_success=False, llm_error="timeout",
                    temperature=temperature,
                )
            except Exception as e:
                logger.warning("Batch %d 处理异常 [%s]: %s", batch_idx, asin, e)
                return CampaignBatchResult(
                    batch_id=batch_idx, round_number=round_number,
                    llm_success=False, llm_error=str(e),
                    temperature=temperature,
                )

    tasks = [_call_one(i, batch) for i, batch in enumerate(batches)]
    # return_exceptions=True 防御性兜底：_call_one 已自包裹异常，这里再防万一
    raw_results = await asyncio.gather(*tasks, return_exceptions=True)
    final: list[CampaignBatchResult] = []
    for i, r in enumerate(raw_results):
        if isinstance(r, BaseException):
            logger.warning("Batch %d gather 异常 [%s]: %s", i, asin, r)
            final.append(CampaignBatchResult(
                batch_id=i, round_number=round_number,
                llm_success=False, llm_error=f"gather: {r}",
                temperature=temperature,
            ))
        else:
            final.append(r)
    return final


# ── 投票与合并 ──────────────────────────────────────────────────────────────


def _round_stats(results: list[CampaignBatchResult]) -> dict:
    successful = sum(1 for r in results if r.llm_success)
    total_items = sum(len(r.items) for r in results)
    return {
        "batches": len(results),
        "successful_batches": successful,
        "failed_batches": len(results) - successful,
        "total_items": total_items,
    }


def _vote_key(item: CampaignAdjustmentItem) -> str:
    """投票/合并阶段对 item 的统一 key 生成规则。"""
    return item.campaign_key or item.campaign_name or f"unknown_{id(item)}"


def _placement_sig(adjustments: list[dict]) -> frozenset:
    """广告位调整签名：{(广告位, 动作)} 集合，用于跨轮一致性比对（精准流）。"""
    sig: set[tuple[str, str]] = set()
    for p in adjustments or []:
        if isinstance(p, dict):
            sig.add((str(p.get("placement", "")), str(p.get("action", ""))))
    return frozenset(sig)


def _negative_kw_sig(neg: list[dict]) -> frozenset:
    """否定关键词签名：去重小写词集合，用于跨轮一致性比对（广泛流）。"""
    sig: set[str] = set()
    for n in neg or []:
        if isinstance(n, dict):
            kw = str(n.get("keyword", "")).strip().lower()
            if kw:
                sig.add(kw)
    return frozenset(sig)


def _vote(
    r1_results: list[CampaignBatchResult],
    r2_results: list[CampaignBatchResult],
) -> tuple[dict[str, dict], set[str]]:
    """比较 R1 与 R2 的 action + direction：全一致 → high (保守幅度)；否则 → tiebreaker。"""

    def _same_direction(a: CampaignAdjustmentItem, b: CampaignAdjustmentItem) -> bool:
        if a.action != b.action:
            return False
        da = a.direction or {}
        db = b.direction or {}
        if da.get("bid") != db.get("bid") or da.get("budget") != db.get("budget"):
            return False
        # 精准流：广告位调整方向也须一致，否则视为分歧（防 placement 分歧被误判 high）
        if a.match_type == "EXACT" or b.match_type == "EXACT":
            return _placement_sig(a.placement_adjustments) == _placement_sig(b.placement_adjustments)
        # 广泛流：否定关键词集合须一致
        return _negative_kw_sig(a.negative_keywords) == _negative_kw_sig(b.negative_keywords)

    r1_map: dict[str, CampaignAdjustmentItem] = {}
    for br in r1_results:
        if br.llm_success:
            for item in br.items:
                r1_map[_vote_key(item)] = item

    r2_map: dict[str, CampaignAdjustmentItem] = {}
    for br in r2_results:
        if br.llm_success:
            for item in br.items:
                r2_map[_vote_key(item)] = item

    all_keys = set(r1_map.keys()) | set(r2_map.keys())
    votes: dict[str, dict] = {}
    needs_tiebreaker: set[str] = set()

    for key in all_keys:
        r1 = r1_map.get(key)
        r2 = r2_map.get(key)

        if r1 and r2:
            if _same_direction(r1, r2):
                # action + direction 一致 → 取保守幅度
                conservative = _conservative(r1, r2)
                conservative["round_votes"] = {
                    "round1": f"{r1.action}|{r1.direction}",
                    "round2": f"{r2.action}|{r2.direction}",
                }
                conservative["confidence"] = "high"
                votes[key] = conservative
            else:
                # 不一致 → tiebreaker
                needs_tiebreaker.add(key)
                votes[key] = {
                    "campaign_key": key,
                    "campaign_name": r1.campaign_name,
                    "child_asin": r1.child_asin,
                    "keyword_text": r1.keyword_text,
                    "match_type": r1.match_type,
                    "action": r1.action,
                    "direction": r1.direction,
                    "confidence": "low",
                    "round_votes": {
                        "round1": f"{r1.action}|{r1.direction}",
                        "round2": f"{r2.action}|{r2.direction}",
                    },
                    "_r1": r1, "_r2": r2,
                }
        elif r1:
            votes[key] = _item_to_vote(r1, "low")
            votes[key]["round_votes"] = {"round1": r1.action, "round2": "missing"}
        else:
            votes[key] = _item_to_vote(r2, "low")
            votes[key]["round_votes"] = {"round1": "missing", "round2": r2.action}

    return votes, needs_tiebreaker


def _conservative(
    a: CampaignAdjustmentItem,
    b: CampaignAdjustmentItem,
) -> dict:
    """两个同 action 的建议中取保守幅度。"""
    chosen = a.model_dump()
    # Bid: 取绝对值较小者 (更安全)
    if (a.proposed_bid is not None and b.proposed_bid is not None
            and a.current_bid is not None and b.current_bid is not None):
        delta_a = abs(a.proposed_bid - a.current_bid)
        delta_b = abs(b.proposed_bid - b.current_bid)
        if delta_b < delta_a:
            chosen["proposed_bid"] = b.proposed_bid
            chosen["reason"] = b.reason
    # Budget: 取绝对值较小者
    if (a.proposed_budget is not None and b.proposed_budget is not None
            and a.current_budget is not None and b.current_budget is not None):
        delta_a = abs(a.proposed_budget - a.current_budget)
        delta_b = abs(b.proposed_budget - b.current_budget)
        if delta_b < delta_a:
            chosen["proposed_budget"] = b.proposed_budget
            chosen["reason"] = b.reason
    return chosen


def _item_to_vote(item: CampaignAdjustmentItem, confidence: str) -> dict:
    d = item.model_dump()
    d["confidence"] = confidence
    return d


def _resolve_tiebreaker(
    votes: dict[str, dict],
    r3_results: list[CampaignBatchResult],
    disputed_keys: set[str],
) -> None:
    """R3 直接采信，覆盖 votes 中暂存的 R1 值。

    仅处理 disputed_keys（R1/R2 分歧项）；高置信项已定，不得在此被误降级为 low。
    """
    r3_map: dict[str, CampaignAdjustmentItem] = {}
    for br in r3_results:
        if br.llm_success:
            for item in br.items:
                r3_map[_vote_key(item)] = item

    for key in disputed_keys:
        if key not in votes:
            continue
        r3 = r3_map.get(key)
        if r3:
            votes[key].update(r3.model_dump())
            votes[key]["confidence"] = "medium"
            current_round = dict(votes[key].get("round_votes", {}))
            current_round["round3"] = r3.action
            votes[key]["round_votes"] = current_round
            # 清理内部引用
            votes[key].pop("_r1", None)
            votes[key].pop("_r2", None)
        else:
            # R3 也缺失 → 降级保持 R1
            votes[key]["confidence"] = "low"
            votes[key].pop("_r1", None)
            votes[key].pop("_r2", None)


def _merge_to_adjustments(
    votes: dict[str, dict],
    *rounds: list[CampaignBatchResult],
) -> list[CampaignAdjustmentItem]:
    """将最终 votes 转为 CampaignAdjustmentItem 列表，排序：淘汰 > 调整 > 保持。"""
    items: list[CampaignAdjustmentItem] = []
    for v in votes.values():
        v.pop("_r1", None)
        v.pop("_r2", None)
        try:
            items.append(CampaignAdjustmentItem(**v))
        except Exception as e:
            logger.warning("_merge_to_adjustments 解析失败: %s", e)

    action_order = {
        "eliminate_to_low_bid_pool": 0,
        "adjust_bid": 1, "adjust_budget": 1, "adjust_placement": 1,
        "keep": 2,
    }
    items.sort(key=lambda x: action_order.get(x.action, 9))
    return items


# ── 懒加载 enrichment ────────────────────────────────────────────────────────


async def _prefetch_placement(
    fetcher: CampaignFetcher,
    parent_asin: str,
    days: int,
    summaries: list[dict],
    unit_lookup: dict[str, CampaignUnit],
) -> list[dict]:
    """预取 EXACT 活动的 placement 数据并注入 summaries。"""
    enriched = [dict(s) for s in summaries]
    placement_names: dict[str, str] = {}
    for s in summaries:
        cu = _find_campaign_unit(unit_lookup, s.get("campaign_key", ""))
        if cu and cu.campaign_id:
            placement_names[cu.campaign_name] = cu.campaign_id

    if not placement_names:
        return enriched

    from app.data.mcp_db_context import resolve_mcp_context_from_db
    try:
        ctx = await asyncio.wait_for(resolve_mcp_context_from_db(parent_asin), timeout=15)
        shop_account = ctx.shop_account if ctx else ""
        shop_id = ctx.shop_id if ctx else 0
    except Exception:
        shop_account = ""; shop_id = 0

    sd, ed = _make_date_window(days)
    result: dict = {}                          # 显式初始化：异常路径下 logger 也要能安全取长度
    try:
        result = await asyncio.wait_for(
            fetcher.fetch_placement_for(placement_names, shop_account, shop_id,
                                        start_date=sd, end_date=ed, days=days),
            timeout=60,
        ) or {}
        for name in enriched:
            s_name = name.get("campaign_name", "")
            if s_name in result:
                name["_placement_data"] = result[s_name]
    except Exception as e:
        logger.warning("placement 预取失败 [%s]: %s", parent_asin, e)

    logger.info("Campaign prefetch placement [%s]: %d/%d",
                 parent_asin, len(result), len(placement_names))
    return enriched


async def _prefetch_search_terms(
    fetcher: CampaignFetcher,
    parent_asin: str,
    days: int,
    summaries: list[dict],
    unit_lookup: dict[str, CampaignUnit],
) -> list[dict]:
    """预取 BROAD/PHRASE/AUTO 活动的 search_term 数据并注入 summaries。"""
    enriched = [dict(s) for s in summaries]
    search_term_names: set[str] = set()
    for s in summaries:
        cu = _find_campaign_unit(unit_lookup, s.get("campaign_key", ""))
        if cu:
            search_term_names.add(cu.campaign_name)

    if not search_term_names:
        return enriched

    from app.data.mcp_db_context import resolve_mcp_context_from_db
    try:
        ctx = await asyncio.wait_for(resolve_mcp_context_from_db(parent_asin), timeout=15)
        shop_account = ctx.shop_account if ctx else ""
    except Exception:
        shop_account = ""

    sd, ed = _make_date_window(days)
    result: dict = {}                          # 显式初始化：异常路径下 logger 也要能安全取长度
    try:
        result = await asyncio.wait_for(
            fetcher.fetch_search_terms_for(list(search_term_names), shop_account,
                                           start_date=sd, end_date=ed),
            timeout=60,
        ) or {}
        for name in enriched:
            s_name = name.get("campaign_name", "")
            if s_name in result:
                name["_search_term_data"] = result[s_name]
    except Exception as e:
        logger.warning("search_term 预取失败 [%s]: %s", parent_asin, e)

    logger.info("Campaign prefetch search_terms [%s]: %d/%d",
                 parent_asin, len(result), len(search_term_names))
    return enriched


def _make_date_window(days: int) -> tuple[str, str]:
    from datetime import date, timedelta
    end = date.today()
    start = end - timedelta(days=days)
    return start.isoformat(), end.isoformat()


def _build_unit_lookup(campaigns: list[CampaignUnit]) -> dict[str, CampaignUnit]:
    """构建 campaign_key + campaign_name → CampaignUnit 的查找表。"""
    lookup: dict[str, CampaignUnit] = {}
    for cu in campaigns:
        lookup[cu.campaign_key] = cu
        lookup[cu.campaign_name] = cu
    return lookup


def _find_campaign_unit(
    lookup: dict[str, CampaignUnit], key_or_name: str,
) -> CampaignUnit | None:
    """从 lookup 表反查 CampaignUnit（先 key 后 name）。"""
    cu = lookup.get(key_or_name)
    if cu:
        return cu
    for cu in lookup.values():
        if cu.campaign_name == key_or_name:
            return cu
    return None


# ── Sanity check 输入截断 ───────────────────────────────────────────────────


async def _sanity_check_batched(
    reasoner: "LLMReasoner",
    asin: str,
    adjustments: list[CampaignAdjustmentItem],
    campaign_summaries: list[dict],
    strategy_context: dict,
    temperature: float,
    *,
    batch_size: int = 10,
) -> tuple[list[str], bool]:
    """只校验低置信 (confidence=low) 项，分批并行避免 LLM 输出超 max_tokens。

    返回 (warnings, all_ok)：all_ok 仅当所有批次都成功执行时为 True；
    任一批次 LLM 失败 / 超时 / gather 异常 → all_ok=False，前端据此显示 ✗。

    设计：
    - 过滤：只取 confidence=='low' 的 adjustments（投票分歧最需要复核）。
      高置信项假定 LLM 双轮一致，不再 sanity check（节省调用且这类最稳）。
    - 分批：每批 ≤batch_size 条；不做优先级排序，按原顺序切分。
    - 并行：asyncio.gather 同时跑所有批次，外层 wait_for(120s) 防整体挂死。
    - 失败隔离：单批 LLM 失败仅该批 warning + all_ok=False，其他批不受影响。
    """
    low_conf = [a for a in adjustments if a.confidence == "low"]
    if not low_conf:
        return [], True

    chunks = [
        low_conf[i:i + batch_size]
        for i in range(0, len(low_conf), batch_size)
    ]

    logger.info(
        "Sanity check [%s]: 低置信 %d/%d 条 → %d 批 × ≤%d 条/批，并行执行",
        asin, len(low_conf), len(adjustments), len(chunks), batch_size,
    )

    async def _run_one(idx: int, batch: list[CampaignAdjustmentItem]) -> tuple[list[str], bool]:
        try:
            warns = await _sanity_check(
                reasoner, asin, batch, campaign_summaries,
                strategy_context, temperature,
            )
            return warns, True
        except Exception as e:
            logger.warning(
                "Sanity 批次 %d/%d 失败 [%s]: %s",
                idx + 1, len(chunks), asin, e,
            )
            return [f"sanity_check 批次 {idx + 1}/{len(chunks)} 失败: {e}"], False

    batch_results = await asyncio.wait_for(
        asyncio.gather(
            *[_run_one(i, b) for i, b in enumerate(chunks)],
            return_exceptions=True,
        ),
        timeout=120,
    )

    all_warnings: list[str] = []
    all_ok = True
    for item in batch_results:
        if isinstance(item, BaseException):
            logger.warning("Sanity 批次异常 [%s]: %s", asin, item)
            all_warnings.append(f"sanity_check 批次异常: {item}")
            all_ok = False
        elif isinstance(item, tuple):
            warns, ok = item
            all_warnings.extend(warns)
            all_ok = all_ok and ok
    return all_warnings, all_ok


# ── 预算冲突裁决 ──────────────────────────────────────────────────────────────


def _resolve_budget_conflicts(
    adjustments: list[CampaignAdjustmentItem],
    total_budget_limit: float | None = None,
) -> list[str]:
    """预算冲突后处理：按优先级依次执行，超总预算上限时截断不缩放。

    当前无总预算上限来源，仅做防御性检查：
    - 单个活动日预算 ≤ $200 (KB 19 §10 上限)
    - 淘汰活动预算固定 $1.00 / Bid 固定 $0.20 (KB 21 §4)
    """
    warnings: list[str] = []

    for adj in adjustments:
        # 淘汰执行值硬校验：无条件填充 $1.00/$0.20
        # （LLM 听话留 None 时也要补齐，避免前端淘汰活动出价/预算空白）
        if adj.action == "eliminate_to_low_bid_pool":
            expected_budget = 1.0
            expected_bid = 0.20
            if adj.proposed_budget != expected_budget:
                if adj.proposed_budget is not None:
                    warnings.append(
                        f"[{adj.campaign_name}] 淘汰活动 proposed_budget=${adj.proposed_budget} "
                        f"(应为 ${expected_budget})，已强制修正"
                    )
                adj.proposed_budget = expected_budget
            if adj.proposed_bid != expected_bid:
                if adj.proposed_bid is not None:
                    warnings.append(
                        f"[{adj.campaign_name}] 淘汰活动 proposed_bid=${adj.proposed_bid} "
                        f"(应为 ${expected_bid})，已强制修正"
                    )
                adj.proposed_bid = expected_bid
            # 淘汰活动不应携带广告位/否词调整（前端展示无意义），清空
            if adj.placement_adjustments:
                adj.placement_adjustments = []
            if adj.negative_keywords:
                adj.negative_keywords = []

        # 日预算上限 (KB 19 §10)
        if adj.proposed_budget is not None and adj.proposed_budget > 200:
            warnings.append(
                f"[{adj.campaign_name}] 日预算 ${adj.proposed_budget} 超过上限 $200，已截断"
            )
            adj.proposed_budget = 200.0

        # Bid 绝对上限 (KB 19 §10)
        if adj.proposed_bid is not None and adj.proposed_bid > 3.0:
            warnings.append(
                f"[{adj.campaign_name}] Bid ${adj.proposed_bid} 超过上限 $3.00，已截断"
            )
            adj.proposed_bid = 3.0

    # 总预算上限检查 (如果有外部输入)
    if total_budget_limit is not None:
        total_proposed = sum(
            (a.proposed_budget or 0) for a in adjustments
        )
        if total_proposed > total_budget_limit:
            # 按优先级截断：keep > adjust > eliminate
            overflow = total_proposed - total_budget_limit
            warnings.append(
                f"总预算 ${total_proposed:.2f} 超出上限 ${total_budget_limit:.2f}，"
                f"溢出 ${overflow:.2f}"
            )
            # 从低优先级开始截断
            for adj in reversed(adjustments):
                if overflow <= 0:
                    break
                if adj.proposed_budget and adj.proposed_budget > 1:
                    cut = min(overflow, adj.proposed_budget - 1)
                    adj.proposed_budget -= cut
                    overflow -= cut

    return warnings


# ── Sanity Check ────────────────────────────────────────────────────────────


_SANITY_PROMPT = """你是亚马逊广告数据一致性校验员，不是决策者。

## 任务
逐条检查用户消息中的 LLM 决策结果是否与事实矛盾（对照 KB 21 淘汰规则 / KB 22 调整规则的条件）。

## 输出格式
{
  "contradictions": [
    {
      "campaign_name": "...",
      "action": "eliminate_to_low_bid_pool",
      "triggered_rule": "NO_CVR_HIGH_SPEND",
      "contradiction": "花费不足$15不满足条件",
      "severity": "warning"
    }
  ]
}

## 输出约束
- contradictions 最多 10 条，按 severity (critical > warning > info) 只取最重要
- 每条 contradiction 字段 ≤ 80 字，精炼描述事实与规则的不符
- 只报真正的矛盾（事实 vs 规则触发条件），不报判断偏好差异
- 不修改建议，不输出新建议
- 无矛盾时 contradictions 为空数组
- 输出纯 JSON，不含 markdown 代码块标记
"""


async def _sanity_check(
    reasoner: "LLMReasoner",
    asin: str,
    adjustments: list[CampaignAdjustmentItem],
    campaign_summaries: list[dict],
    strategy_context: dict,
    temperature: float,
) -> list[str]:
    """纯事实陈述 + LLM 交叉校验 → 矛盾 → warnings。

    LLM 调用失败时**抛出**异常（由 _sanity_check_batched._run_one 捕获并标记该批失败），
    不再吞掉返回 warning 字符串——否则上层无法区分"校验通过"与"校验失败"。
    """
    if not adjustments:
        return []

    logger.info("Campaign sanity LLM 入口 [%s] items=%d", asin, len(adjustments))

    # 构建事实快照
    facts_parts: list[str] = []
    for adj in adjustments:
        # 找到对应的事实 summary
        camp_facts = {}
        for s in campaign_summaries:
            if s.get("campaign_key") == adj.campaign_key:
                camp_facts = s
                break

        p = camp_facts.get("perf_7d", {})
        facts_parts.append(
            f"  - {adj.campaign_name}: action={adj.action}, "
            f"triggered_rule={adj.triggered_rule}, "
            f"7d花费=${p.get('cost', 0)}, 7d订单={p.get('orders', 0)}, "
            f"7d CVR={p.get('cvr', 'N/A')}%, "
            f"match_type={adj.match_type}, keyword_class={adj.keyword_class}, "
            f"is_core={adj.is_core}"
        )

    strategy_text = "\n".join(
        f"  - {k}: {v}" for k, v in strategy_context.items()
        if k not in ("warning_flags",)
    )

    user_msg = f"""## 决策结果 (需校验)

{chr(10).join(facts_parts)}

## 策略上下文

{strategy_text}
"""

    messages = [
        {"role": "system", "content": _SANITY_PROMPT},
        {"role": "user", "content": user_msg},
    ]

    try:
        raw = await reasoner.client.chat(
            messages=messages,
            temperature=max(temperature, 0.1),
            response_format={"type": "json_object"},
            max_tokens=4096,
            timeout_override=90,
        )
        parsed = reasoner._parse_json(raw)
        contradictions = parsed.get("contradictions", [])
        warnings_list: list[str] = []
        for c in contradictions:
            warnings_list.append(
                f"[{c.get('campaign_name', '?')}] {c.get('contradiction', '')}"
            )
        return warnings_list
    except Exception as e:
        logger.warning("Sanity check LLM 失败 [%s]: %s", asin, e)
        raise  # 交由调用方标记该批失败（all_ok=False）
