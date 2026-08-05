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
from typing import TYPE_CHECKING, Awaitable, Callable

from app.config.settings import settings
from app.models.campaign import (
    CampaignStrategyContext,
    NewCampaignCandidate,
    NewCampaignDecision,
    NewCampaignItem,
)
from app.workflow.steps.campaign_portfolio import PORTFOLIO_BROAD, PORTFOLIO_TEST
from app.workflow.analysis_run_guard import AnalysisRunCancelled

if TYPE_CHECKING:
    from app.data.campaign_fetcher import CampaignFetcher
    from app.llm.reasoner import LLMReasoner

logger = logging.getLogger(__name__)

# KB 16 §2 / §3 常量
DEFAULT_NEW_BUDGET = 3.00       # KB 16 §2 首次创建默认日预算
BID_HARD_LOWER = 0.20           # KB 16 §3 出价下限
BID_HARD_UPPER = 0.50           # KB 16 §3 首次创建出价硬上限
BID_PLACEHOLDER = 0.30          # 建议竞价字段到位前的占位（区间中位安全值）
MIN_SEARCH_VOLUME = 100         # 低搜索量噪声词过滤（保留 sv ≥ 100；2026-06-24 由 50 上调）


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
MAX_KEYWORD_TOKENS = 10  # 词数硬上限：>10 词的超长拼凑长尾直接丢弃（运营反馈，2026-06-24）


def _is_noise_keyword(kw: str) -> bool:
    """噪声过滤：纯数字 / 单字母 / 过短 / 乱码 / **超长词数(>10 词)**。"""
    s = (kw or "").strip()
    return (not kw or len(kw) < 3 or bool(_NOISE_RE.match(s))
            or len(s.split()) > MAX_KEYWORD_TOKENS)


_LONGTAIL_TOKEN_CAP = 5  # 词数计分上限：≥5 词并列，防 10+ 词超长拼凑串霸榜（运营反馈）


def _longtail_sort_key(c: "NewCampaignCandidate"):
    """候选排序键 —— **长尾优先**（运营偏好精准长尾，非大词/泛词）：
    ① 有自然位优先（产品已在排 = 事实相关）；
    ② **词数多者优先，但 cap 在 5**（`min(词数,5)`）：3~5 词的真长尾优先；≥5 词并列、
       不让 10+ 词的超长拼凑串排到最前（运营反馈"一口气推多个 >10 词的词"）；
    ③ 搜索量作同级 tiebreak（不当主排序——否则高流量大词霸榜、长尾在进 LLM 前就被 Top-N
       截掉，是"总扩大词/泛词"的代码层根因。KB06：long_tail 精准 优先于 generic 大词测词）。
    MIN_SEARCH_VOLUME 已兜底防零流量垃圾长串。
    """
    tokens = min(len((c.keyword_text or "").split()), _LONGTAIL_TOKEN_CAP)
    return (c.natural_rank is None, -tokens, -(c.search_volume or 0))


def _select_by_quota(
    candidates: list["NewCampaignCandidate"],
    max_n: int,
    quotas: dict[str, int],
    priority: list[str],
) -> list["NewCampaignCandidate"]:
    """多源配额分桶选取（H2：禁止单一全局排序，否则竞品 natural_rank=None+低搜索量被挤光）。

    ① 按 source 分桶，桶内"有自然位优先 + 搜索量降序"；
    ② 各源先取至配额；③ 剩余名额（含某源欠额释放的，如 competitor 关闭）按 priority
       从各源超额部分回补，直到 max_n。
    ⚠ 配额值 20/15/5 暂定，待 KB 最终核（settings.campaign_new_quota_*）。
    """
    buckets: dict[str, list] = {}
    for c in candidates:
        buckets.setdefault(c.source, []).append(c)
    for s in buckets:
        buckets[s].sort(key=_longtail_sort_key)   # 桶内长尾优先（词数多优先，非搜索量降序）
    selected: list = []
    taken: dict[str, int] = {}
    for s in priority:                       # ② 各源取至配额
        chunk = buckets.get(s, [])[:max(0, int(quotas.get(s, 0)))]
        selected.extend(chunk)
        taken[s] = len(chunk)
    if len(selected) < max_n:                # ③ 回补：剩余名额按优先级从各源超额部分取
        for s in priority:
            if len(selected) >= max_n:
                break
            for c in buckets.get(s, [])[taken.get(s, 0):]:
                if len(selected) >= max_n:
                    break
                selected.append(c)
    return selected[:max_n]


# 来源优先级（数字小=优先级高，用于去重合并时定 bucket 归属）：竞品 > 自然位机会 > 流量
_SOURCE_PRIORITY = {"competitor": 0, "ranking_opportunity": 1, "flow": 2}


def _merge_candidate(by_kw: dict, cand: "NewCampaignCandidate") -> None:
    """去重合并来源（Q1：同词多源出现 → 留一条，但来源说明合并、bucket 归最高优先级源）。

    先到先得仅适用于"已存在则合并"而非"丢弃"：
    - source_reason 合并（去重保序，让 LLM 看到该词来自哪些源）；
    - source（决定配额分桶）归优先级最高的源（competitor > ranking_opportunity > flow）；
    - natural_rank / suggested_bid 取有值者；search_volume 取较大者。
    """
    ex = by_kw.get(cand.keyword_text)
    if ex is None:
        by_kw[cand.keyword_text] = cand
        return
    reasons = [r for r in [ex.source_reason, cand.source_reason] if r]
    ex.source_reason = " | ".join(dict.fromkeys(reasons))
    if _SOURCE_PRIORITY.get(cand.source, 9) < _SOURCE_PRIORITY.get(ex.source, 9):
        ex.source = cand.source
        if cand.trigger_scene:
            ex.trigger_scene = cand.trigger_scene
    if ex.natural_rank is None and cand.natural_rank is not None:
        ex.natural_rank = cand.natural_rank
    if ex.suggested_bid is None and cand.suggested_bid is not None:
        ex.suggested_bid = cand.suggested_bid
    if (cand.search_volume or 0) > (ex.search_volume or 0):
        ex.search_volume = cand.search_volume


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


def _derive_match_type(_keyword_class: str, cand: NewCampaignCandidate) -> str:
    """来源驱动：有验证信号→EXACT，无验证信号→BROAD 探词拿样本。

    keyword_class 只描述词形/相关性，不参与 match_type 推导（KB16 §4：
    BROAD=低成本拿搜索词样本，Exact=词已验证有效）。竞品词虽常见于 EXACT，
    但此处仅定 match_type；门槛（HIGH_RISK_REVIEW/单轮上限等）由调用方按
    KB28 §4.1 执行。
    """
    if cand.source in ("flow", "ranking_opportunity"):
        return "BROAD"
    return "EXACT"


# ── KB28 §2 相关性档位 (R1精确 > R2扩展 > R3试探 > R4风险) ────────────────────
_TIER_ORDER = {"R1": 1, "R2": 2, "R3": 3, "R4": 4}


def _norm_tier(v) -> str:
    t = str(v or "").strip().upper()
    return t if t in _TIER_ORDER else ""


def _conservative_tier(a, b) -> str:
    """两轮相关性档取更保守(数字更大=更不相关)的一档；任一缺失则取另一个。"""
    ta, tb = _norm_tier(a), _norm_tier(b)
    if not ta:
        return tb
    if not tb:
        return ta
    return ta if _TIER_ORDER[ta] >= _TIER_ORDER[tb] else tb


def pick_target_child_asin(campaigns: list) -> str:
    """新增活动的投放目标子 ASIN。

    新建活动须挂到一个具体子 ASIN 投放，不能用父 ASIN 占位。
    选「历史活动数最多」的子 ASIN（主力投放变体）；活动数平手时按 7 天总花费最高。
    campaigns 为空或均无 child_asin 时返回 ""。

    结果落库到 t_advert_agent_modify_suggest_card.asin，执行阶段直接读回，不重复查询。
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


async def finalize_new_campaign_decisions(
    decisions: list[NewCampaignDecision],
    *,
    fetcher: "CampaignFetcher",
    parent_asin: str,
    parent_seller_sku: str,
    shop_account: str,
    target_child_asin: str,
) -> list[NewCampaignItem]:
    """把已定来源/匹配方式的决策统一补齐为可执行新增活动。"""
    if not decisions:
        return []

    by_keyword: dict[str, NewCampaignDecision] = {}
    for decision in decisions:
        key = " ".join((decision.keyword_text or "").strip().lower().split())
        if not key:
            continue
        # 搜索词提精已验证，覆盖同词的流量词探索决策。
        if key not in by_keyword or decision.trigger_scene == "KEYWORD_PROMOTED_FROM_BROAD":
            by_keyword[key] = decision
    selected = list(by_keyword.values())

    bid_values: dict[str, float] = {}
    missing = [d.keyword_text for d in selected if d.suggested_bid is None]
    if missing:
        try:
            bid_values = await fetcher.fetch_suggested_bids(
                missing, shop_account, parent_asin, parent_seller_sku,
            )
        except Exception as e:
            logger.warning("Campaign new [%s]: 建议竞价查询失败 (非阻塞): %s", parent_asin, e)

    items: list[NewCampaignItem] = []
    for decision in selected:
        mt = decision.prescribed_match_type
        is_exact = mt == "EXACT"
        suggested_bid = decision.suggested_bid
        if suggested_bid is None:
            suggested_bid = bid_values.get(decision.keyword_text)
        bid_cand = NewCampaignCandidate(
            keyword_text=decision.keyword_text,
            suggested_bid=suggested_bid,
        )
        bid, bid_source = _calc_initial_bid(bid_cand)
        items.append(NewCampaignItem(
            keyword_text=decision.keyword_text,
            child_asin=target_child_asin,
            campaign_name=_generate_campaign_name(decision.keyword_text, mt),
            campaign_type="精准广告" if is_exact else "广泛广告",
            match_type=mt,
            keyword_class=decision.keyword_class,
            relevance_tier=decision.relevance_tier,
            keywords_or_targets=[decision.keyword_text],
            proposed_daily_budget=DEFAULT_NEW_BUDGET,
            proposed_base_bid=bid,
            primary_placement="头部" if is_exact else "N/A",
            placement_adjustment="N/A",
            negative_strategy=decision.negative_strategy if not is_exact else "",
            trigger_scene=decision.trigger_scene,
            source=decision.source,
            reason=decision.reason,
            evidence=decision.evidence,
            ai_portfolio_class=PORTFOLIO_TEST if is_exact else PORTFOLIO_BROAD,
            confidence=decision.confidence,
            review_level=decision.review_level,
            suggested_bid_source=bid_source,
        ))

    # 已验证搜索词优先，其余保持旧的精准优先/高置信优先的稳定排序。
    items.sort(key=lambda item: (
        item.trigger_scene != "KEYWORD_PROMOTED_FROM_BROAD",
        item.match_type != "EXACT",
        item.confidence != "high",
        item.keyword_text,
    ))
    return items[:getattr(settings, "campaign_new_max_creates", 15)]


def merge_new_campaign_decisions(
    traffic_source_decisions: list[NewCampaignDecision],
    search_term_decisions: list[NewCampaignDecision],
) -> list[NewCampaignDecision]:
    """合流新增活动决策，最终参数只由 ``finalize_new_campaign_decisions`` 组装一次。

    搜索词提精准仅替换同词的普通 ``flow`` 探索项；排名机会词、竞品词仍按既有
    管线保留，避免本次接线改变那两条尚未收口的数据契约。
    """
    by_keyword: dict[str, NewCampaignDecision] = {}
    for item in traffic_source_decisions:
        key = " ".join((item.keyword_text or "").strip().lower().split())
        if key:
            by_keyword[key] = item

    for item in search_term_decisions:
        key = " ".join((item.keyword_text or "").strip().lower().split())
        if not key:
            continue
        existing = by_keyword.get(key)
        if existing is None or (existing.source or "").strip().lower() == "flow":
            by_keyword[key] = item

    items = list(by_keyword.values())
    items.sort(key=lambda item: (
        item.trigger_scene != "KEYWORD_PROMOTED_FROM_BROAD",
        item.prescribed_match_type != "EXACT",
        item.confidence != "high",
        item.keyword_text,
    ))
    return items


# ── 4. 主入口 ─────────────────────────────────────────────────────────────

async def analyze_new_campaign_decisions(
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
    product_title: str = "",          # 相关性锚点（来自 asin_data.title；brand/category 已去除：太粗易引品类级误匹配）
    cancel_check: Callable[[], Awaitable[None]] | None = None,
) -> tuple[list[NewCampaignDecision], list[str], dict[str, int]]:
    """发现并选择来源 A 的新增活动决策，暂不查询 bid 或组装最终卡片。

    search_volume_map = flow_keywords 全量 {keyword_lower: 搜索量}（含已有活动词），
    透传给预算回算 agent 用（KB23 §3.1A），零新增 MCP 调用；失败/未跑时为 {}。
    ctx_dict 含 _strategic_overview_text(posture_brief)，注入 LLM 作今日总纲。
    fail-open：任何阶段失败仅记 warning，返回空列表。
    """
    warnings: list[str] = []
    search_volume_map: dict[str, int] = {}

    async def _cancel() -> None:
        if cancel_check:
            await cancel_check()

    await _cancel()

    # 0. ASIN 级阻断 (KB 16 §6)
    block = _is_blocked_by_asin(strategy_context)
    if block:
        logger.info("Campaign new [%s]: 阻断 %s", parent_asin, block)
        warnings.append(f"新增活动分析被阻断: {block}")
        return [], warnings, search_volume_map

    # 1. 候选词发现 (NewKeywordFetcher: flow + own + history + suggest_bid)
    from app.data.new_keyword_fetcher import NewKeywordFetcher

    # ① 竞品发现（独立 task，与 flow/own 并行）
    comp_task = (
        asyncio.create_task(fetcher.discover_competitor_keywords(
            parent_asin=parent_asin, shop_account=shop_account,
            parent_seller_sku=parent_seller_sku, site_code=site_code,
        ))
        if settings.campaign_new_competitor_enabled else None
    )
    try:
        kw_fetcher = NewKeywordFetcher()
        kw_data = await kw_fetcher.fetch(
            parent_asin=parent_asin, shop_account=shop_account,
            parent_seller_sku=parent_seller_sku, site_code=site_code,
            existing_keywords=existing_keywords,
            target_child_asin=target_child_asin,
            pre_eliminated_count=pre_eliminated_count,
            strategy_context=strategy_context,
            days=days,
        )
        search_volume_map = kw_data.search_volume_map
        if kw_data.errors:
            warnings.extend(kw_data.errors)
        await _cancel()
    except Exception as e:  # noqa: BLE001
        if comp_task is not None:
            comp_task.cancel()
        logger.warning("new_campaigns 候选词发现失败 [%s]: %s", parent_asin, e)
        warnings.append(f"候选词发现失败: {type(e).__name__}: {e}")
        return [], warnings, search_volume_map

    # 2. ★ NewKeywordRecord → NewCampaignCandidate 投影 + 竞品合并
    by_kw: dict[str, NewCampaignCandidate] = {}
    for r in kw_data.records:
        his_ok = r.history_state == "ok"
        cand = NewCampaignCandidate(
            keyword_text=r.keyword_text,
            search_volume=r.search_volume,
            search_rank=r.search_rank,
            natural_rank=r.own_natural_rank,                     # own 做主源
            rank_trend=r.rank_trend if his_ok else None,          # history 补充趋势
            rank_tier=r.rank_tier if his_ok else None,            # history 补充分位
            sponsored_rank=r.sponsored_rank if his_ok else None,  # history 补充广告排位
            week_rank=r.week_rank,
            week_search_volume=r.week_search_volume,
            history_state=r.history_state,
            suggested_bid=None,   # 下方 bid_task 并行填
            source=r.source,
            source_reason=r.source_reason,
        )
        cand.trigger_scene = _label_trigger(cand, strategy_context, pre_eliminated_count)
        _merge_candidate(by_kw, cand)

    # ── 竞品词合并 ──
    if settings.campaign_new_competitor_enabled:
        try:
            comp_rows = await comp_task if comp_task is not None else []
        except Exception as e:  # noqa: BLE001
            logger.warning("new_campaigns 竞品源失败 [%s]: %s (非阻塞)", parent_asin, e)
            comp_rows = []
        for r in comp_rows:
            kw = str(r.get("keyword") or "").strip().lower()
            if not kw or kw in existing_keywords or _is_noise_keyword(kw):
                continue
            sv = r.get("search_volume")
            _merge_candidate(by_kw, NewCampaignCandidate(
                keyword_text=kw,
                search_volume=int(sv) if isinstance(sv, int) else 0,
                natural_rank=r.get("natural_rank"),
                suggested_bid=r.get("suggested_bid"),
                trigger_scene="COMPETITOR_INTERCEPT_WINDOW",
                source="competitor",
                source_reason=f"竞品{r.get('competitor_asin', '')}反查"
                              + (f"·搜索量{sv}" if sv is not None else ""),
            ))

    candidates = list(by_kw.values())
    if not candidates:
        logger.info("Campaign new [%s]: 硬过滤后无候选词", parent_asin)
        return [], warnings, search_volume_map

    # 28-48 自然位且搜索量 ≥500 的词优先占用 Top-N 配额；
    # 超额时按搜索量降序取满，避免突破 LLM 输入上限。
    _MIN_OPPORTUNITY_SV = 500
    guaranteed = [c for c in candidates
                  if c.trigger_scene == "RANKING_OPPORTUNITY_NO_EXACT"
                  and (c.search_volume or 0) >= _MIN_OPPORTUNITY_SV]
    guaranteed.sort(key=lambda c: (-(c.search_volume or 0), c.keyword_text))
    guaranteed = guaranteed[:getattr(settings, "campaign_new_max_count", 40)]
    rest = [c for c in candidates if c not in guaranteed]

    # ★量控：competitor 开启→按来源配额分桶选取；关闭→退回单一全局排序 → Top-N
    max_n = getattr(settings, "campaign_new_max_count", 40)
    if settings.campaign_new_competitor_enabled:
        quotas = {
            "competitor": getattr(settings, "campaign_new_quota_competitor", 20),
            "ranking_opportunity": getattr(settings, "campaign_new_quota_ranking", 15),
            "flow": getattr(settings, "campaign_new_quota_flow", 5),
        }
        rest = _select_by_quota(
            rest, max_n - len(guaranteed), quotas,
            priority=["competitor", "ranking_opportunity", "flow"],
        )
        candidates = guaranteed + rest
    else:
        rest.sort(key=_longtail_sort_key)
        quota = max_n - len(guaranteed)
        candidates = guaranteed + rest[:max(0, quota)]

    logger.info("Campaign new [%s]: %d 个候选词进入双轮 LLM 选词", parent_asin, len(candidates))

    # 3. 双轮 LLM 选词取交集（KB 06 判 keyword_class + create/skip）
    batch_size = getattr(settings, "campaign_new_batch_size", 10)
    cand_dump = [c.model_dump() for c in candidates]

    # 信号量提到循环外：原 `sem or asyncio.Semaphore(1)` 每次迭代新建 = 限流形同虚设
    # （改 gather 后必须共享同一把，否则真的不限流）。sem 实际由调用方传入(new_sem)。
    round_sem = sem or asyncio.Semaphore(1)

    async def _run_round(seed: int) -> tuple[dict[str, dict], bool]:
        import random as _rnd
        ordered = cand_dump[:]
        _rnd.Random(seed).shuffle(ordered)
        batches = [ordered[i:i + batch_size] for i in range(0, len(ordered), batch_size)]

        async def _call_one(batch):
            await _cancel()
            async with round_sem:
                result = await reasoner.recommend_new_campaigns(
                    asin=parent_asin, candidates=batch,
                    strategy_context=ctx_dict,           # 含 posture_brief
                    temperature=temperature, timeout_override=55,
                    product_title=product_title,
                    existing_keywords=sorted(existing_keywords),  # 相关性参照锚点
                )
            await _cancel()
            return result

        # 轮内批次并行（对齐主流 _run_round 的 gather 模式）
        results = await asyncio.gather(
            *[_call_one(b) for b in batches], return_exceptions=True,
        )
        out: dict[str, dict] = {}
        any_success = False  # 本轮至少一个批次成功执行 → 区分"失败"与"真判都不建"
        for res in results:
            if isinstance(res, AnalysisRunCancelled):
                raise res
            if isinstance(res, BaseException):
                warnings.append(f"new_campaigns 批次异常: {type(res).__name__}: {res}")
                continue
            if res.get("success") and isinstance(res.get("parsed"), dict):
                any_success = True
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
        return out, any_success

    # LLM 轮前等策略总览 gate：保证 ctx_dict 含 posture_brief（候选词发现已跑完，不串行）
    if overview_gate is not None:
        await overview_gate
    await _cancel()

    # ③ 双轮 LLM 选词；建议竞价统一延后到两来源合流后查询一次。
    try:
        (r1, r1_ok), (r2, r2_ok) = await asyncio.gather(_run_round(1), _run_round(2))
    except AnalysisRunCancelled:
        raise
    except Exception as e:  # noqa: BLE001
        logger.warning("new_campaigns 双轮 LLM 异常 [%s]: %s", parent_asin, e)
        warnings.append(f"new_campaigns LLM 异常: {type(e).__name__}: {e}")
        return [], warnings, search_volume_map

    # 4. 容错选词：
    #    两轮都成功执行 → 取交集（降幻觉，两轮 keyword_class 一致才 high）
    #    仅一轮成功（另一轮整轮失败）→ 退化为成功那轮，但全部标 MANUAL_REVIEW/low
    #      （没有双轮验证，不冒充 high；保住结果不因一轮失败全丢）
    #    两轮都失败 → 空
    if r1_ok and r2_ok:
        selected_keys = set(r1.keys() & r2.keys())
        degraded_round: dict | None = None
    elif r1_ok or r2_ok:
        degraded_round = r1 if r1_ok else r2
        selected_keys = set(degraded_round.keys())
        warnings.append("new_campaigns：双轮中一轮整轮失败，退化为单轮结果（全部标记需人工复核）")
        logger.warning(
            "Campaign new [%s]: 一轮失败(r1_ok=%s r2_ok=%s)，退化单轮 %d 词",
            parent_asin, r1_ok, r2_ok, len(selected_keys),
        )
    else:
        warnings.append("new_campaigns：双轮均失败，无新增建议")
        return [], warnings, search_volume_map

    # 5. 形成来源 A 决策；最终参数统一延后到两来源合流后组装。
    #   相关性/词类型/建不建都是主观语义判断，交 LLM（已注入 KB28 §2：R4不建/R3仅测试期/
    #   量小≠不相关）；代码不再二次硬判，只把 LLM 的 relevance_tier 记录到输出供前端/审计。
    cand_by_kw = {c.keyword_text: c for c in candidates}
    decisions: list[NewCampaignDecision] = []
    for kw in selected_keys:
        cand = cand_by_kw.get(kw)
        if cand is None:
            continue
        if degraded_round is None:
            # 双轮交集：两轮 keyword_class 一致 → high，否则 low
            o1, o2 = r1[kw], r2[kw]
            kc1 = str(o1.get("keyword_class", "")).strip().lower()
            kc2 = str(o2.get("keyword_class", "")).strip().lower()
            if kc1 == kc2 and kc1:
                keyword_class, conf, review = kc1, "high", "AUTO_APPROVED"
            else:
                keyword_class, conf, review = (kc1 or kc2), "low", "MANUAL_REVIEW"
            relevance_tier = _conservative_tier(o1.get("relevance_tier"), o2.get("relevance_tier"))
        else:
            # 退化单轮：无双轮验证，统一低置信 + 人工复核（o1 供下方组装复用）
            o1 = degraded_round[kw]
            keyword_class = str(o1.get("keyword_class", "")).strip().lower()
            conf, review = "low", "MANUAL_REVIEW"
            relevance_tier = _norm_tier(o1.get("relevance_tier"))

        mt = _derive_match_type(keyword_class, cand)
        decisions.append(NewCampaignDecision(
            keyword_text=cand.keyword_text,
            prescribed_match_type=mt,
            keyword_class=keyword_class,
            relevance_tier=relevance_tier,
            negative_strategy=str(o1.get("negative_strategy", "")) if mt != "EXACT" else "",
            trigger_scene=cand.trigger_scene,
            source=cand.source,
            reason=str(o1.get("reason", "")),
            evidence=list(o1.get("evidence", []) or []),
            confidence=conf,
            review_level=review,
            search_volume=cand.search_volume,
            natural_rank=cand.natural_rank,
            suggested_bid=cand.suggested_bid,
        ))

    logger.info(
        "Campaign new [%s]: R1=%d R2=%d 交集=%d → 来源 A 决策 %d",
        parent_asin, len(r1), len(r2), len(selected_keys), len(decisions),
    )
    return decisions, warnings, search_volume_map


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
    product_title: str = "",
    cancel_check: Callable[[], Awaitable[None]] | None = None,
) -> tuple[list[NewCampaignItem], list[str], dict[str, int]]:
    """来源 A 单独运行时的兼容包装；正常主流程在合流后只调用一次组装器。"""
    decisions, warnings, search_volume_map = await analyze_new_campaign_decisions(
        fetcher=fetcher,
        reasoner=reasoner,
        parent_asin=parent_asin,
        shop_id=shop_id,
        parent_seller_sku=parent_seller_sku,
        site_code=site_code,
        shop_account=shop_account,
        existing_keywords=existing_keywords,
        pre_eliminated_count=pre_eliminated_count,
        strategy_context=strategy_context,
        ctx_dict=ctx_dict,
        temperature=temperature,
        target_child_asin=target_child_asin,
        days=days,
        sem=sem,
        overview_gate=overview_gate,
        product_title=product_title,
        cancel_check=cancel_check,
    )
    items = await finalize_new_campaign_decisions(
        decisions,
        fetcher=fetcher,
        parent_asin=parent_asin,
        parent_seller_sku=parent_seller_sku,
        shop_account=shop_account,
        target_child_asin=target_child_asin,
    )
    return items, warnings, search_volume_map
