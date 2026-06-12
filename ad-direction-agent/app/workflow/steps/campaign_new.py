"""新增广告活动分析 — KB 16《新增活动规则》实现（独立并行分析线）。

设计原则:
- LLM 只判「该不该建 + keyword_class」+ 写 reason/evidence/negative_strategy（语义判断）
- bid / budget / campaign_name / match_type / placement 全部代码层确定性产出（KB 16 §2/§3/§4/§5）
- 触发场景 trigger_scene 仅作展示标签(KB 16 §5)，不作筛选门禁；量控靠 硬过滤→排序→Top-N 截断
- 双轮取交集降幻觉：两轮都判"建"的词才保留；keyword_class 两轮一致=high，不一致=MANUAL_REVIEW
- 「建议竞价」字段当前无源 → bid 占位 $0.30；数据源到位后只改 _calc_initial_bid 一处
- fail-open：任何阶段失败仅记 warning，返回空列表，不影响主分析
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date
from typing import TYPE_CHECKING

from app.config.settings import settings
from app.models.campaign import (
    CampaignStrategyContext,
    NewCampaignCandidate,
    NewCampaignItem,
)
from app.workflow.steps.campaign_portfolio import PORTFOLIO_BROAD, PORTFOLIO_TEST

if TYPE_CHECKING:
    from app.data.campaign_fetcher import CampaignFetcher
    from app.llm.reasoner import LLMReasoner

logger = logging.getLogger(__name__)

# KB 16 §2 / §3 常量
DEFAULT_NEW_BUDGET = 3.00       # KB 16 §2 首次创建默认日预算
BID_HARD_LOWER = 0.20           # KB 16 §3 出价下限
BID_HARD_UPPER = 0.50           # KB 16 §3 首次创建出价硬上限
BID_PLACEHOLDER = 0.30          # 建议竞价字段到位前的占位（区间中位安全值）
MIN_SEARCH_VOLUME = 50          # 低搜索量噪声词过滤


# ── 1. 硬过滤 (KB 16 §6) ─────────────────────────────────────────────────

def _is_blocked_by_asin(ctx: CampaignStrategyContext) -> str | None:
    """KB 16 §6 阻断条件；返回阻断原因码或 None。"""
    if ctx.inventory_days is not None and ctx.inventory_days < 7:
        return "INVENTORY_LOW_BLOCK"
    if ctx.refund_rate is not None and ctx.refund_rate >= 30.0:   # refund_rate 为百分数口径
        return "HIGH_RETURN_RATE_BLOCK"
    if ctx.rating is not None and ctx.rating < 3.8:
        return "LOW_RATING_BLOCK"
    if ctx.product_stage == "清货期":
        # 清货期默认阻断非核心词新增 (KB 16 §6 LIQUIDATING_ONLY_CORE)
        return "LIQUIDATING_ONLY_CORE"
    return None


_NOISE_RE = re.compile(r"^[\d\W_]+$|^[a-zA-Z]$")


def _is_noise_keyword(kw: str) -> bool:
    """纯数字 / 单字母 / 过短 / 乱码过滤。"""
    return not kw or len(kw) < 3 or bool(_NOISE_RE.match(kw.strip()))


# ── 2. 触发场景标注 (KB 16 §1, 仅展示标签, 非门禁) ──────────────────────────

def _label_trigger(
    cand: NewCampaignCandidate,
    ctx: CampaignStrategyContext,
    pre_eliminated_count: int,
) -> str:
    """按 KB 16 §1 优先级标注触发场景（展示用）；总返回非空字符串。

    ⚠️ trigger_scene 不具过滤力（旺季/有淘汰时会给所有词打同一标签），
       故只标注、不 gate；量控完全靠硬过滤+排序+截断。
    """
    if cand.natural_rank is not None and 28 <= cand.natural_rank <= 48:
        return "RANKING_OPPORTUNITY_NO_EXACT"
    if ctx.season_stage in ("旺季准备", "旺季前期", "大旺季", "旺季"):
        return "SEASONAL_ADVANCE_BUILD"
    if pre_eliminated_count > 0:
        return "FILL_AFTER_ELIMINATION"
    # 兜底标签：经硬过滤的高搜索量词，本质是扩词引流
    return "KEYWORD_POOL_EXPANSION"
    # TODO(triggers): KEYWORD_PROMOTED_FROM_BROAD 需搜索词聚合 / COMPETITOR_INTERCEPT_WINDOW 需 KB 08
    #   / CUSTOM_KEYWORD_POOL 需运营词池表，待数据源接入后细化标签，不影响本期选词。


# ── 3. Bid / match_type / 命名 计算 (KB 16 §2 §3 §5 + KB 06) ──────────────

def _calc_initial_bid(cand: NewCampaignCandidate) -> tuple[float, str]:
    """KB 16 §3: 初始 bid = min(0.5, 建议竞价×0.5)，下限 $0.20。返回 (bid, source)。"""
    if cand.suggested_bid is not None and cand.suggested_bid > 0:
        bid = max(BID_HARD_LOWER, min(BID_HARD_UPPER, round(cand.suggested_bid * 0.5, 2)))
        return bid, "amazon_api"
    # MCP 未命中时降级占位（关键词不在亚马逊建议竞价覆盖范围内）
    return BID_PLACEHOLDER, "placeholder"


# keyword_class → match_type 推导 (KB 06 §1-5 + KB 16 §4)
_CLASS_TO_MATCH_TYPE = {
    "generic": "BROAD",      # 大词测词，广泛起步
    "long_tail": "EXACT",    # 长尾相关性高，精准承接
    "competitor": "EXACT",   # 竞品截流，精准
    "brand": "EXACT",        # 品牌平替，KB 06 §4「精确匹配」
    "custom": "EXACT",       # 自定义词池，KB 06 §5「精确匹配」
}


def _derive_match_type(keyword_class: str, cand: NewCampaignCandidate) -> str:
    """从 LLM 判定的 keyword_class 推导 match_type；缺失时回退（有自然位→EXACT，否则 BROAD）。"""
    kc = (keyword_class or "").strip().lower()
    if kc in _CLASS_TO_MATCH_TYPE:
        return _CLASS_TO_MATCH_TYPE[kc]
    return "EXACT" if cand.natural_rank is not None else "BROAD"


def pick_target_child_asin(campaigns: list) -> str:
    """新增活动的投放目标子 ASIN。

    新建活动须挂到一个具体子 ASIN 投放，不能用父 ASIN 占位。
    选「历史活动数最多」的子 ASIN（主力投放变体）；活动数平手时按 7 天总花费最高。
    campaigns 为空或均无 child_asin 时返回 ""（前端再回退父 ASIN 占位）。
    """
    from collections import defaultdict

    stat: dict[str, list] = defaultdict(lambda: [0, 0.0])  # child_asin -> [活动数, 总花费]
    for cu in campaigns:
        ca = (getattr(cu, "child_asin", "") or "").strip()
        if not ca:
            continue
        stat[ca][0] += 1
        perf = getattr(cu, "perf_7d", None)
        stat[ca][1] += (getattr(perf, "cost", 0.0) or 0.0) if perf else 0.0
    if not stat:
        return ""
    best = max(stat.items(), key=lambda kv: (kv[1][0], kv[1][1]))  # 活动数 desc, 花费 desc
    return best[0]


_CAMPAIGN_NAME_INVALID_RE = re.compile(r"[\\/:*?\"<>|]")


def _generate_campaign_name(keyword: str, match_type: str, today: str = "") -> str:
    """命名规则（运营拍板 2026-06-10）: 匹配类型(中文)-关键词-日期(YYYY-MM-DD)，不含 ASIN。

    例: "精准-fishnet body suits women-2026-06-10"
    """
    type_cn = "精准" if match_type == "EXACT" else "广泛"
    kw_clean = _CAMPAIGN_NAME_INVALID_RE.sub(" ", keyword).strip()[:80]
    d = today or date.today().strftime("%Y-%m-%d")
    return f"{type_cn}-{kw_clean}-{d}"


# ── 4. 主入口 ─────────────────────────────────────────────────────────────

async def analyze_new_campaigns(
    fetcher: "CampaignFetcher",
    reasoner: "LLMReasoner",
    parent_asin: str,
    shop_id: int,
    parent_seller_sku: str,
    site_code: str,
    shop_account: str,
    existing_keywords: set[str],
    pre_eliminated_count: int,
    strategy_context: CampaignStrategyContext,
    ctx_dict: dict,
    temperature: float,
    *,
    target_child_asin: str = "",
    days: int = 7,
    sem: asyncio.Semaphore | None = None,
    overview_gate: "asyncio.Task | None" = None,
) -> tuple[list[NewCampaignItem], list[str]]:
    """完整新增活动分析（独立并行管道）。返回 (new_campaigns, warnings)。

    ctx_dict 含 _strategic_overview_text(posture_brief)，注入 LLM 作今日总纲。
    fail-open：任何阶段失败仅记 warning，返回空列表。
    """
    warnings: list[str] = []

    # 0. ASIN 级阻断 (KB 16 §6)
    block = _is_blocked_by_asin(strategy_context)
    if block:
        logger.info("Campaign new [%s]: 阻断 %s", parent_asin, block)
        warnings.append(f"新增活动分析被阻断: {block}")
        return [], warnings

    # 1. 候选词发现 (MCP flow_keywords + own_keyword_flow)
    try:
        flow_rows, own_rows = await fetcher.discover_new_keywords(
            parent_asin=parent_asin,
            shop_account=shop_account,
            parent_seller_sku=parent_seller_sku,
            site_code=site_code,
            days=days,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("new_campaigns 候选词发现失败 [%s]: %s", parent_asin, e)
        warnings.append(f"候选词发现失败: {type(e).__name__}: {e}")
        return [], warnings

    if not flow_rows and not own_rows:
        warnings.append("候选词发现返回空 (MCP 数据不可用)")
        return [], warnings

    # 2. 归一化 + 硬过滤
    own_rank_map: dict[str, int] = {}
    for r in own_rows:
        kw = str(r.get("关键词") or r.get("keyword") or "").strip().lower()
        rank = r.get("自然排名") or r.get("natural_rank")
        if kw and rank is not None:
            try:
                own_rank_map[kw] = int(float(rank))
            except (TypeError, ValueError):
                continue

    candidates: list[NewCampaignCandidate] = []
    seen: set[str] = set()
    for r in flow_rows:
        kw = str(r.get("关键词") or r.get("keyword") or "").strip().lower()
        if not kw or kw in seen or kw in existing_keywords or _is_noise_keyword(kw):
            continue
        try:
            sv = int(float(r.get("搜索量") or r.get("search_volume") or 0))
        except (TypeError, ValueError):
            sv = 0
        if sv < MIN_SEARCH_VOLUME:
            continue
        seen.add(kw)
        cand = NewCampaignCandidate(
            keyword_text=kw,
            search_volume=sv,
            natural_rank=own_rank_map.get(kw),
            suggested_bid=None,  # 待补，见 _calc_initial_bid
        )
        cand.trigger_scene = _label_trigger(cand, strategy_context, pre_eliminated_count)
        candidates.append(cand)

    # own_rows 中有自然位机会但 flow 缺失的词也纳入
    for kw, rank in own_rank_map.items():
        if kw in seen or kw in existing_keywords or _is_noise_keyword(kw):
            continue
        if 28 <= rank <= 48:
            candidates.append(NewCampaignCandidate(
                keyword_text=kw, search_volume=0, natural_rank=rank,
                trigger_scene="RANKING_OPPORTUNITY_NO_EXACT",
            ))
            seen.add(kw)

    if not candidates:
        logger.info("Campaign new [%s]: 硬过滤后无候选词", parent_asin)
        return [], warnings

    # ★量控: 按"有自然位优先 + 搜索量降序"排序，再截断 Top-N
    candidates.sort(key=lambda c: (c.natural_rank is None, -c.search_volume))
    max_n = getattr(settings, "campaign_new_max_count", 20)
    candidates = candidates[:max_n]

    # 2b. ★建议竞价（KB 16 §3）：批量查 MCP，填入 cand.suggested_bid
    #     _calc_initial_bid 已有 if cand.suggested_bid is not None 分支，只填值即可
    if settings.campaign_new_enabled and shop_account:
        try:
            kw_texts = [c.keyword_text for c in candidates]
            bids = await fetcher.fetch_suggested_bids(
                kw_texts, shop_account, parent_asin, parent_seller_sku,
            )
            for c in candidates:
                sb = bids.get(c.keyword_text)
                if sb is not None:
                    c.suggested_bid = sb
            logger.info(
                "Campaign new [%s]: MCP 建议竞价命中 %d/%d",
                parent_asin, len([c for c in candidates if c.suggested_bid is not None]), len(candidates),
            )
        except Exception as e:
            logger.warning("Campaign new [%s]: 建议竞价查询失败 (非阻塞): %s", parent_asin, e)

    logger.info("Campaign new [%s]: %d 个候选词进入双轮 LLM 选词", parent_asin, len(candidates))

    # 3. 双轮 LLM 选词取交集（KB 06 判 keyword_class + create/skip）
    batch_size = getattr(settings, "campaign_new_batch_size", 10)
    cand_dump = [c.model_dump() for c in candidates]

    # 信号量提到循环外：原 `sem or asyncio.Semaphore(1)` 每次迭代新建 = 限流形同虚设
    # （改 gather 后必须共享同一把，否则真的不限流）。sem 实际由调用方传入(new_sem)。
    round_sem = sem or asyncio.Semaphore(1)

    async def _run_round(seed: int) -> dict[str, dict]:
        import random as _rnd
        ordered = cand_dump[:]
        _rnd.Random(seed).shuffle(ordered)
        batches = [ordered[i:i + batch_size] for i in range(0, len(ordered), batch_size)]

        async def _call_one(batch):
            async with round_sem:
                return await reasoner.recommend_new_campaigns(
                    asin=parent_asin, candidates=batch,
                    strategy_context=ctx_dict,           # 含 posture_brief
                    temperature=temperature, timeout_override=55,
                )

        # 轮内批次并行（对齐主流 _run_round 的 gather 模式）
        results = await asyncio.gather(
            *[_call_one(b) for b in batches], return_exceptions=True,
        )
        out: dict[str, dict] = {}
        for res in results:
            if isinstance(res, BaseException):
                warnings.append(f"new_campaigns 批次异常: {type(res).__name__}: {res}")
                continue
            if res.get("success") and isinstance(res.get("parsed"), dict):
                for it in res["parsed"].get("new_campaigns", []) or []:
                    if not isinstance(it, dict):
                        continue
                    if str(it.get("action", "create")).lower() == "skip":
                        continue
                    kw = str(it.get("keyword_text", "")).strip().lower()
                    if kw:
                        out[kw] = it
            else:
                warnings.append(f"new_campaigns 批次失败: {res.get('error', 'unknown')}")
        return out

    # LLM 轮前等策略总览 gate：保证 ctx_dict 含 posture_brief（候选词发现/建议竞价已跑完，不串行）
    if overview_gate is not None:
        await overview_gate

    try:
        r1, r2 = await asyncio.gather(_run_round(1), _run_round(2))
    except Exception as e:  # noqa: BLE001
        logger.warning("new_campaigns 双轮 LLM 异常 [%s]: %s", parent_asin, e)
        warnings.append(f"new_campaigns LLM 异常: {type(e).__name__}: {e}")
        return [], warnings

    # 4. 取交集 + 组装（代码补齐 match_type/bid/budget/name/归组）
    cand_by_kw = {c.keyword_text: c for c in candidates}
    items: list[NewCampaignItem] = []
    for kw in r1.keys() & r2.keys():            # 两轮都判"建"的词才保留（交集降幻觉）
        cand = cand_by_kw.get(kw)
        if cand is None:
            continue
        o1, o2 = r1[kw], r2[kw]
        kc1 = str(o1.get("keyword_class", "")).strip().lower()
        kc2 = str(o2.get("keyword_class", "")).strip().lower()
        if kc1 == kc2 and kc1:
            keyword_class, conf, review = kc1, "high", "AUTO_BATCHABLE"
        else:
            keyword_class, conf, review = (kc1 or kc2), "low", "MANUAL_REVIEW"

        mt = _derive_match_type(keyword_class, cand)
        is_exact = mt == "EXACT"
        bid, src = _calc_initial_bid(cand)
        items.append(NewCampaignItem(
            keyword_text=cand.keyword_text,
            child_asin=target_child_asin,
            campaign_name=_generate_campaign_name(cand.keyword_text, mt),
            campaign_type="精准广告" if is_exact else "广泛广告",
            match_type=mt,
            keyword_class=keyword_class,
            keywords_or_targets=[cand.keyword_text],
            proposed_daily_budget=DEFAULT_NEW_BUDGET,
            proposed_base_bid=bid,
            primary_placement="头部" if is_exact else "N/A",   # 代码默认，不交 LLM
            placement_adjustment="N/A",
            negative_strategy=str(o1.get("negative_strategy", "")) if not is_exact else "",
            trigger_scene=cand.trigger_scene,
            reason=str(o1.get("reason", "")),
            evidence=list(o1.get("evidence", []) or []),
            ai_portfolio_class=PORTFOLIO_TEST if is_exact else PORTFOLIO_BROAD,
            confidence=conf,
            review_level=review,
            suggested_bid_source=src,
        ))

    items.sort(key=lambda x: x.confidence != "high")  # 高置信优先
    logger.info(
        "Campaign new [%s]: R1=%d R2=%d 交集=%d 个新增建议",
        parent_asin, len(r1), len(r2), len(items),
    )
    return items, warnings
