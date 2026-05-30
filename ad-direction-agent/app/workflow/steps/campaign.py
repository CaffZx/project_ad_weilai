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

TYPE_MAP: dict[str, str] = {
    "broad": "大词", "long_tail": "长尾词", "long-tail": "长尾词",
    "competitor": "竞品词", "brand": "品牌词", "custom": "自定义",
}


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
    campaign_data: CampaignData | None = None,
    keyword_analysis: dict | None = None,
) -> CampaignAnalysisResult:
    """完整 LLM 分析：拉数据 → 分批 → R1+R2 → 投票 → (R3) → sanity_check。

    实验脚本直接调用此函数，无需 WorkflowContext。
    campaign_data 可预取后复用；keyword_analysis 用于逐词 keyword_class 富化。
    """

    bs = batch_size or settings.campaign_batch_size
    cc = concurrency or settings.campaign_llm_concurrency

    # 1. 获取活动数据
    if campaign_data is None:
        campaign_data = await fetcher.fetch_campaigns(parent_asin, days=days)

    if campaign_data.total_campaigns == 0:
        return CampaignAnalysisResult(
            parent_asin=parent_asin, days=days,
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
    skipped_eliminated: list[str] = []
    for cu in campaign_data.campaigns:
        if (cu.current_budget is not None and cu.current_bid is not None
                and 0.99 <= cu.current_budget <= 1.01
                and 0.19 <= cu.current_bid <= 0.21):
            skipped_eliminated.append(cu.campaign_name)
            continue
        llm_campaigns.append(cu)

    if skipped_eliminated:
        logger.info("Campaign 预过滤 [%s]: 跳过 %d 个疑似已淘汰活动 (budget≈$1, bid≈$0.2)",
                     parent_asin, len(skipped_eliminated))

    total = len(llm_campaigns)
    if total == 0:
        return CampaignAnalysisResult(
            parent_asin=parent_asin, days=days,
            total_campaigns=0,
            warnings=["所有活动均在预过滤阶段被排除（疑似全部已淘汰）"],
            rounds_detail={},
        )

    # 4. 构建 per-campaign summary
    summaries: list[dict] = []
    unit_lookup = _build_unit_lookup(llm_campaigns)
    for cu in llm_campaigns:
        keyword_cls = keyword_class_map.get(cu.keyword_text, "")
        is_core = False
        summaries.append(reasoner._campaign_to_prompt_dict(
            cu, target_acos=strategy_context.target_acos,
            keyword_class=keyword_cls, is_core=is_core,
        ))

    ctx_dict = strategy_context.model_dump()
    rounds_detail: dict[str, dict] = {}
    warnings_list: list[str] = []

    # 5. 预取 placement + search_term (批量，一次调用)
    enriched_summaries = await _prefetch_enrichment(
        fetcher, parent_asin, days, summaries, unit_lookup,
    )

    # 6. Round 1 + Round 2 并行 (均含富化数据)
    llm_sem = asyncio.Semaphore(cc)
    r1_batches = _build_batches(enriched_summaries, bs, seed=1)
    r2_batches = _build_batches(enriched_summaries, bs, seed=2)
    r1_results, r2_results = await asyncio.gather(
        _run_round(reasoner, parent_asin, r1_batches, ctx_dict, temperature, llm_sem, 1),
        _run_round(reasoner, parent_asin, r2_batches, ctx_dict, temperature, llm_sem, 2),
    )
    rounds_detail["round1"] = _round_stats(r1_results)
    rounds_detail["round2"] = _round_stats(r2_results)
    rounds_detail["round3"] = {"ran": False}

    # 7. 投票 (比较 action + direction)
    votes, needs_tiebreaker = _vote(r1_results, r2_results)
    r3_results: list[CampaignBatchResult] = []

    # 8. Tiebreaker (按需)
    if needs_tiebreaker:
        tiebreaker_summaries = [s for s in enriched_summaries
                                if s.get("campaign_key") in needs_tiebreaker]
        if tiebreaker_summaries:
            r3_results = await _run_round(
                reasoner, parent_asin,
                _build_batches(tiebreaker_summaries, bs, seed=3),
                ctx_dict, temperature, llm_sem, round_number=3,
            )
            _resolve_tiebreaker(votes, r3_results)
            rounds_detail["round3"] = {
                "ran": True,
                "disputed_count": len(needs_tiebreaker),
                **_round_stats(r3_results),
            }

    # 9. 合并 + 预算冲突裁决
    adjustments = _merge_to_adjustments(votes, r1_results, r2_results, r3_results)
    budget_warnings = _resolve_budget_conflicts(adjustments)
    warnings_list.extend(budget_warnings)

    # 10. Sanity check
    sanity_ok = True
    try:
        sc_warnings = await _sanity_check(
            reasoner, parent_asin, adjustments, enriched_summaries,
            ctx_dict, temperature,
        )
        warnings_list.extend(sc_warnings)
    except Exception as e:
        logger.warning("Campaign sanity check 失败 [%s]: %s", parent_asin, e)
        warnings_list.append(f"sanity_check 执行失败: {e}")
        sanity_ok = False

    # 11. 汇总统计
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

    rounds_completed = 3 if r3_results else 2

    return CampaignAnalysisResult(
        parent_asin=parent_asin, days=days,
        total_campaigns=total,
        adjustments=adjustments,
        summary=summary_stats,
        warnings=warnings_list,
        sanity_check_passed=sanity_ok,
        llm_rounds_completed=rounds_completed,
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
                    ),
                    timeout=LLM_TIMEOUT,
                )
            except asyncio.TimeoutError:
                return CampaignBatchResult(
                    batch_id=batch_idx, round_number=round_number,
                    llm_success=False, llm_error="timeout",
                    temperature=temperature,
                )
            except Exception as e:
                return CampaignBatchResult(
                    batch_id=batch_idx, round_number=round_number,
                    llm_success=False, llm_error=str(e),
                    temperature=temperature,
                )

        parsed = result.get("parsed", {})
        raw_adj = parsed.get("campaign_adjustments", [])
        items: list[CampaignAdjustmentItem] = []
        for adj in raw_adj:
            try:
                items.append(CampaignAdjustmentItem(**adj))
            except Exception as e:
                logger.warning("Campaign adjustment item 解析失败 batch=%d: %s", batch_idx, e)

        return CampaignBatchResult(
            batch_id=batch_idx, round_number=round_number,
            items=items,
            raw_llm_output=result.get("raw_output", ""),
            temperature=temperature,
            llm_success=result.get("success", False),
            llm_error=result.get("error", ""),
        )

    tasks = [_call_one(i, batch) for i, batch in enumerate(batches)]
    results = await asyncio.gather(*tasks)
    return list(results)


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


def _vote(
    r1_results: list[CampaignBatchResult],
    r2_results: list[CampaignBatchResult],
) -> tuple[dict[str, dict], set[str]]:
    """比较 R1 与 R2 的 action + direction：全一致 → high (保守幅度)；否则 → tiebreaker。"""
    def _make_key(item: CampaignAdjustmentItem) -> str:
        return item.campaign_key or item.campaign_name or f"unknown_{id(item)}"

    def _same_direction(a: CampaignAdjustmentItem, b: CampaignAdjustmentItem) -> bool:
        if a.action != b.action:
            return False
        da = a.direction or {}
        db = b.direction or {}
        return da.get("bid") == db.get("bid") and da.get("budget") == db.get("budget")

    r1_map: dict[str, CampaignAdjustmentItem] = {}
    for br in r1_results:
        if br.llm_success:
            for item in br.items:
                r1_map[_make_key(item)] = item

    r2_map: dict[str, CampaignAdjustmentItem] = {}
    for br in r2_results:
        if br.llm_success:
            for item in br.items:
                r2_map[_make_key(item)] = item

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
) -> None:
    """R3 直接采信，覆盖 votes 中暂存的 R1 值。"""
    r3_map: dict[str, CampaignAdjustmentItem] = {}
    for br in r3_results:
        if br.llm_success:
            for item in br.items:
                r3_map[item.campaign_key or item.campaign_name] = item

    for key in votes:
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


async def _prefetch_enrichment(
    fetcher: CampaignFetcher,
    parent_asin: str,
    days: int,
    summaries: list[dict],
    unit_lookup: dict[str, CampaignUnit],
) -> list[dict]:
    """预取所有 placement (EXACT) + search_term (BROAD/PHRASE/AUTO) 数据，注入 summaries。

    R1+R2 并行前调用，确保两轮都有完整数据。
    """
    enriched = [dict(s) for s in summaries]

    placement_names: dict[str, str] = {}  # campaign_name → campaign_id
    search_term_names: set[str] = set()

    for s in summaries:
        mt = s.get("match_type", "")
        name = s.get("campaign_name", "")
        if not name:
            continue
        cu = _find_campaign_unit(unit_lookup, s.get("campaign_key", ""))
        if not cu:
            continue
        if mt == "EXACT" and cu.campaign_id:
            placement_names[cu.campaign_name] = cu.campaign_id
        elif mt in ("BROAD", "PHRASE", "AUTO"):
            search_term_names.add(cu.campaign_name)

    if not placement_names and not search_term_names:
        return enriched

    from app.data.mcp_db_context import resolve_mcp_context_from_db
    try:
        ctx = await asyncio.wait_for(
            resolve_mcp_context_from_db(parent_asin), timeout=15,
        )
        shop_account = ctx.shop_account if ctx else ""
        shop_id = ctx.shop_id if ctx else 0
    except Exception:
        shop_account = ""
        shop_id = 0

    sd, ed = _make_date_window(days)
    placement_raw: dict[str, dict] = {}
    search_term_raw: dict[str, list] = {}

    async def _fetch_p():
        if placement_names and shop_account:
            try:
                result = await asyncio.wait_for(
                    fetcher.fetch_placement_for(
                        placement_names, shop_account, shop_id,
                        start_date=sd, end_date=ed, days=days,
                    ),
                    timeout=60,
                )
                for k, v in (result or {}).items():
                    placement_raw[k] = v
            except Exception as e:
                logger.warning("placement 预取失败: %s", e)

    async def _fetch_st():
        if search_term_names and shop_account:
            try:
                result = await asyncio.wait_for(
                    fetcher.fetch_search_terms_for(
                        list(search_term_names), shop_account,
                        start_date=sd, end_date=ed,
                    ),
                    timeout=60,
                )
                for k, v in (result or {}).items():
                    search_term_raw[k] = v
            except Exception as e:
                logger.warning("search_term 预取失败: %s", e)

    await asyncio.gather(_fetch_p(), _fetch_st())

    for s in enriched:
        name = s.get("campaign_name", "")
        if name in placement_raw:
            s["_placement_data"] = placement_raw[name]
        if name in search_term_raw:
            s["_search_term_data"] = search_term_raw[name]

    logger.info("Campaign prefetch [%s]: placement=%d/%d search_term=%d/%d",
                 parent_asin,
                 len(placement_raw), len(placement_names),
                 len(search_term_raw), len(search_term_names))
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
        # 淘汰执行值硬校验
        if adj.action == "eliminate_to_low_bid_pool":
            expected_budget = 1.0
            expected_bid = 0.20
            if adj.proposed_budget is not None and adj.proposed_budget != expected_budget:
                warnings.append(
                    f"[{adj.campaign_name}] 淘汰活动 proposed_budget=${adj.proposed_budget} "
                    f"(应为 ${expected_budget})，已强制修正"
                )
                adj.proposed_budget = expected_budget
            if adj.proposed_bid is not None and adj.proposed_bid != expected_bid:
                warnings.append(
                    f"[{adj.campaign_name}] 淘汰活动 proposed_bid=${adj.proposed_bid} "
                    f"(应为 ${expected_bid})，已强制修正"
                )
                adj.proposed_bid = expected_bid

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
      "contradiction": "事实 CVR=3.2%、订单=2，不满足 NO_CVR_HIGH_SPEND 条件 (需要 CVR=0 + spend>$15 + 无自然位支撑)",
      "severity": "warning"
    }
  ],
  "overall_notes": "..."
}

## 规则
- 只报真正的矛盾（事实 vs 规则触发条件），不报判断偏好差异
- 不修改建议，不输出新建议
- 无矛盾 → contradictions 空数组
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
    """纯事实陈述 + LLM 交叉校验 → 矛盾 → warnings。"""
    if not adjustments:
        return []

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
        raw = await asyncio.wait_for(
            reasoner.client.chat(
                messages=messages,
                temperature=max(temperature, 0.1),
                response_format={"type": "json_object"},
                max_tokens=2048,
            ),
            timeout=30,
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
        return [f"sanity_check LLM 调用失败: {e}"]
