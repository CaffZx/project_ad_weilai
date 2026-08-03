"""Campaign LLM 分析引擎 — 分批 + R1 单轮 + 护栏 R2/R3/R4 重试 + sanity_check。

复用资产:
- CampaignFetcher.fetch_campaigns() → CampaignData
- LLMReasoner.recommend_campaign_batch() → 单批 LLM 分析
- kb.build("campaign_adjustment_exact"/"_broad") → KB 18:1,3 / 17:1,2,3,4,5,7 / 15 / 19 / 22:0,2(或0,3) / 21 切片(精准/广泛分流)
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Awaitable, Callable

from app.config.settings import settings
from app.core.acos_constraints import compute_acos_tolerance
from app.core.core_keyword_policy import normalize_core_keyword
from app.data.campaign_fetcher import CampaignFetcher
from app.data.campaign_prefilter import filter_eliminated_pool
from app.models.asin_data import ASINData
from app.persistence.redis_client import acquire_lock, get_redis, release_lock
from app.workflow.analysis_run_guard import AnalysisRunCancelled
from app.workflow.steps.campaign_portfolio import (
    LOW_BID_MAX,
    LOW_BUDGET_MAX,
    PORTFOLIO_BROAD,
    PORTFOLIO_ELIMINATE,
    find_portfolio_group_matches,
    is_strictly_in_low_bid_pool,
    target_group_code_if_current_mismatch,
)
from app.workflow.steps.campaign_restart import analyze_eliminated_restart
from app.workflow.steps.campaign_new import (
    analyze_new_campaigns,
    pick_target_child_asin as _pick_target_child_asin,
)
from app.models.campaign import (
    CampaignAdjustmentItem,
    CampaignAnalysisResult,
    CampaignBatchResult,
    CampaignData,
    CampaignStrategicOverview,
    CampaignStrategyContext,
    CampaignUnit,
)
from app.models.layers import AdPermission, OperatingMode, operating_mode_to_permission
if TYPE_CHECKING:
    from app.llm.reasoner import LLMReasoner
    from app.workflow.context import WorkflowContext

logger = logging.getLogger(__name__)

LLM_TIMEOUT = 60  # 单批 LLM 超时 (秒)

# 进程级 LLM 全局并发已统一收口到 client 层（client._global_llm_sem，每个 chat() 过闸）。
# campaign 仅保留 per-stream Semaphore(cc) 作单轮内公平限流（见 _analyze_one_stream）。


# Sanity / Synthesis 开关（2026-06-01 恢复）
# 修复方式：去掉外层 asyncio.wait_for（Windows 取消不生效），改用 chat(timeout_override=)
# 由 httpx socket 层超时接管，不依赖 asyncio 取消
# keyword_class 上游有两种拼写,查表前统一 .lower() 归一:
#  - purpose-agent LLM 输出首字母大写 (Broad/Long-tail/Competitor/Brand/Custom)
#  - KB 权威拼写为 generic (大词);兜底都收
TYPE_MAP: dict[str, str] = {
    "broad": "大词", "generic": "大词",
    "long_tail": "长尾词", "long-tail": "长尾词",
    "competitor": "竞品词", "brand": "品牌词", "custom": "自定义",
}


# ── 运行互斥 & 数据缓存 ──────────────────────────────────────────────────

# 跨 worker 幂等锁与共享 Redis 客户端已抽到 app.persistence.redis_client（通用基础设施）。
# 本模块只定义 campaign 专属的锁 key 命名与 TTL。


def _running_ttl() -> int:
    """幂等锁 TTL（秒）。必须 ≥ 单次分析最大时长(campaign_total_timeout)，
    否则长任务跑到一半锁过期 → 另一 worker 重开一轮 → 重复 LLM。
    +60s 余量覆盖 API 层 wait_for 边界与释放延迟。"""
    return settings.campaign_total_timeout + 60


def _running_key(asin: str) -> str:
    return f"campaign:running:{asin}"


def _campaign_cache_key(asin: str, days: int) -> str:
    # 注意：本部署内 parent_asin → 单店铺。
    # 若未来同一 parent_asin 跨店铺复用，需在 key 中加入 shop_account 前缀防串店。
    return f"campaign:data:{asin}:{days}"


async def _load_cached_campaigns(asin: str, days: int) -> CampaignData | None:
    """从 Redis 读取缓存的 CampaignData。"""
    import json as _json
    r = await get_redis()
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
    """将 CampaignData 写入 Redis 缓存，TTL 30 分钟。

    空结果防毒化:total_campaigns == 0 或 errors 非空时不入缓存。
    原因:上游(MCP/Doris)临时失败/超时时 fetcher 会兜底返回空 CampaignData,
    若缓存空对象,后续 30 分钟内同 ASIN 永远命中空 → 运营看到"无可用广告活动"
    即使刷新也无效(直到 TTL 过期)。空结果不缓存可以让下次请求重新拉取。
    """
    import json as _json
    if data is None or data.total_campaigns == 0:
        logger.info(
            "Campaign 空结果不入缓存 [%s] (total_campaigns=0,防毒化)",
            asin,
        )
        return
    if data.errors:
        logger.info(
            "Campaign 有 fetcher 错误不入缓存 [%s]: %s",
            asin, data.errors[:3],
        )
        return
    r = await get_redis()
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


# ── 淘汰复评 I/O 编排（KB 21 §7；纯判定在 campaign_restart.analyze_eliminated_restart） ──


async def _sem_gather(coros: list, limit: int):
    """信号量限并发的 gather（复评拉数防大池一次打爆 MCP）。"""
    sem = asyncio.Semaphore(max(1, limit))

    async def _run(c):
        async with sem:
            return await c

    return await asyncio.gather(*[_run(c) for c in coros], return_exceptions=True)


async def _run_restart_review(
    pool_units: list[CampaignUnit],
    entry_dates: dict,
    fetcher: CampaignFetcher,
    campaign_data: CampaignData | None,
    parent_asin: str,
) -> tuple[list[CampaignAdjustmentItem], set[str]]:
    """门控抓取后调纯函数。仅 ≥review_days 候选拉在池窗口订单；有情况二候选才拉精准30d均CPC。"""
    from datetime import date
    from app.data.mcp_mapping import make_date_window
    from app.workflow.steps.campaign_restart import restart_orders_window_days

    today = date.today()
    shop_account = getattr(fetcher, "_last_shop_account", "") or ""
    review_days = settings.campaign_restart_review_days
    fetch_conc = settings.campaign_restart_fetch_concurrency

    def _days(rec: dict) -> int:
        ed = rec.get("entry_date")
        ed = ed.date() if hasattr(ed, "date") else ed
        return (today - ed).days if ed else -1

    candidates = []
    for cu in pool_units:
        cid = (cu.campaign_id or "").strip()
        rec = entry_dates.get(cid) if cid else None
        if rec and _days(rec) >= review_days:
            candidates.append((cu, _days(rec), rec))
    if not candidates:
        return [], set()

    # 候选封顶（按入池天数降序，久的优先）——防大池(如 100+)一次拉爆 MCP
    max_n = settings.campaign_restart_max_candidates
    if len(candidates) > max_n:
        logger.warning("Campaign 复评 [%s]: 候选 %d 超上限 %d，按入池天数降序截断",
                       parent_asin, len(candidates), max_n)
        candidates.sort(key=lambda c: c[1], reverse=True)
        candidates = candidates[:max_n]

    # 在池窗口订单（限并发；窗口 = min(入池天数, 上限)，评审 #1）
    orders_inpool: dict[str, int] = {}
    coros = [
        fetcher._fetch_perf_one(cu.campaign_name, shop_account, *make_date_window(restart_orders_window_days(d), campaign_data.site_code if campaign_data else ""))
        for cu, d, _ in candidates
    ]
    for (cu, _, _), r in zip(candidates, await _sem_gather(coros, fetch_conc)):
        if isinstance(r, tuple) and len(r) == 2 and getattr(r[1], "ok", False) and isinstance(r[1].value, dict):
            orders_inpool[(cu.campaign_id or "").strip()] = int(r[1].value.get("orders") or 0)

    # 情况二门控：有 0 单且淘汰前花费>$15 的候选，才拉精准 30d 均CPC
    high_spend = settings.campaign_restart_high_spend_7d
    need_cpc = any(
        orders_inpool.get((cu.campaign_id or "").strip(), 0) == 0
        and rec.get("eliminate_spend_7d") is not None
        and float(rec["eliminate_spend_7d"]) > high_spend
        for cu, _, rec in candidates
    )
    exact_cpc_30d = await _fetch_exact_cpc_30d(fetcher, campaign_data, shop_account) if need_cpc else None

    return analyze_eliminated_restart(
        [c[0] for c in candidates], entry_dates, orders_inpool, exact_cpc_30d, today=today,
    )


async def _fetch_exact_cpc_30d(
    fetcher: CampaignFetcher, campaign_data: CampaignData | None, shop_account: str,
) -> float | None:
    """父ASIN活跃精准广告近30天均CPC（KB §7 情况二定价）：有出单词优先，否则全部精准词。"""
    from app.data.mcp_mapping import make_date_window

    exacts = [
        cu for cu in (campaign_data.campaigns if campaign_data else [])
        if (cu.match_type or "").upper() == "EXACT"
        and not is_strictly_in_low_bid_pool(cu.current_bid, cu.current_budget)
    ]
    if not exacts:
        return None
    sd, ed = make_date_window(30, campaign_data.site_code if campaign_data else "")
    coros = [fetcher._fetch_perf_one(cu.campaign_name, shop_account, sd, ed) for cu in exacts]
    cpc_ordered: list[float] = []
    cpc_all: list[float] = []
    for r in await _sem_gather(coros, settings.campaign_restart_fetch_concurrency):
        if not (isinstance(r, tuple) and len(r) == 2 and getattr(r[1], "ok", False) and isinstance(r[1].value, dict)):
            continue
        cpc = r[1].value.get("cpc")
        if cpc is not None and float(cpc) > 0:
            cpc_all.append(float(cpc))
            if int(r[1].value.get("orders") or 0) > 0:
                cpc_ordered.append(float(cpc))
    pool = cpc_ordered or cpc_all
    return round(sum(pool) / len(pool), 2) if pool else None


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
    run_id: str | None = None,
    erp_override: dict | None = None,
    elimination_entry_dates: dict | None = None,  # deprecated: 现由内部 sync 后从 state 库读取，外部传 None 即可
    cancel_check: Callable[[], Awaitable[None]] | None = None,
) -> CampaignAnalysisResult:
    """完整 LLM 分析：拉数据 → 分批 → R1 单轮 → 护栏 R2/R3/R4 重试 → sanity_check。

    实验脚本直接调用此函数，无需 WorkflowContext。
    campaign_data 可预取后复用；keyword_analysis 用于逐词 keyword_class 富化。
    erp_override：ERP URL 注入的 shop_account/sku/site_code/shop_id，透传给数据拉取以跳过 dwd_shop 反查。
    """

    bs = batch_size or settings.campaign_batch_size
    cc = concurrency or settings.campaign_llm_concurrency
    run_id = (run_id or "").strip() or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    t_start = time.monotonic()
    _t = lambda label: logger.info("Campaign timing [%s] +%.1fs: %s", parent_asin, time.monotonic() - t_start, label)

    # 0. 幂等检查：跨 worker 锁（Redis SET NX EX，Redis 不可用降级进程内）。
    #    TTL ≥ campaign_total_timeout，长任务不会中途失锁；释放走 compare-and-delete 防误删。
    acquired, holder = await acquire_lock(_running_key(parent_asin), run_id, _running_ttl())
    if not acquired:
        logger.warning("Campaign 幂等拦截 [%s]: 已有 run_id=%s 运行中", parent_asin, holder)
        return CampaignAnalysisResult(
            parent_asin=parent_asin, days=days, run_id=run_id,
            total_campaigns=0,
            warnings=[f"该 ASIN 已有分析正在运行 (run_id={holder})，请等待完成后重试"],
            sanity_check_passed=False,
        )

    try:
        result = await _analyze_campaigns_impl(
            fetcher=fetcher, reasoner=reasoner, parent_asin=parent_asin,
            asin_data=asin_data, strategy_context=strategy_context,
            days=days, bs=bs, cc=cc, temperature=temperature, refresh=refresh,
            campaign_data=campaign_data, keyword_analysis=keyword_analysis,
            run_id=run_id, _t=_t, erp_override=erp_override,
            elimination_entry_dates=elimination_entry_dates,
            cancel_check=cancel_check,
        )
        return result
    finally:
        await release_lock(_running_key(parent_asin), run_id)


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
    erp_override: dict | None = None,
    elimination_entry_dates: dict | None = None,  # deprecated/unused: 内部 sync 后从 state 库读取
    cancel_check: Callable[[], Awaitable[None]] | None = None,
) -> CampaignAnalysisResult:

    async def _cancel():
        if cancel_check:
            await cancel_check()

    # ① 获取活动数据前
    await _cancel()

    # Portfolio 每次分析都重新拉取；这里只缓存 CampaignData。
    portfolio_data: dict | None = None

    # 1. 获取活动数据 — 优先 Redis 缓存（refresh=True 时跳过），miss 时拉 MCP/Doris
    if campaign_data is None:
        if not refresh:
            campaign_data = await _load_cached_campaigns(parent_asin, days)
        if campaign_data:
            _t("DONE fetch_campaigns (redis hit)")
        else:
            try:
                campaign_data = await asyncio.wait_for(
                    fetcher.fetch_campaigns(parent_asin, days=days, override=erp_override),
                    timeout=300,
                )
                await _save_cached_campaigns(parent_asin, days, campaign_data)
                _t("DONE fetch_campaigns (fetched)")
                # ② 拉完活动数据后
                await _cancel()
            except asyncio.TimeoutError:
                logger.warning("fetch_campaigns 超时 [%s] >300s", parent_asin)
                return CampaignAnalysisResult(
                    parent_asin=parent_asin, days=days, run_id=run_id,
                    total_campaigns=0,
                    warnings=[f"获取活动数据超时 (>300s)，请重试"],
                    sanity_check_passed=False,
                    data_unavailable=True,
                )
            except Exception as e:
                logger.exception("fetch_campaigns 异常 [%s]: %s", parent_asin, e)
                return CampaignAnalysisResult(
                    parent_asin=parent_asin, days=days, run_id=run_id,
                    total_campaigns=0,
                    warnings=[f"获取活动数据失败: {type(e).__name__}: {e}"],
                    sanity_check_passed=False,
                    data_unavailable=True,
                )

    if campaign_data.total_campaigns == 0:
        return CampaignAnalysisResult(
            parent_asin=parent_asin, days=days, run_id=run_id,
            shop_id=campaign_data.shop_id,
            parent_seller_sku=campaign_data.parent_seller_sku,
            site_code=campaign_data.site_code,
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
                            # .lower() 归一:兼容 purpose-agent 大写(Broad)与 KB 拼写(generic)
                            keyword_class_map[kw] = TYPE_MAP.get(st.lower(), st)

    # 2a. 读取离线核心词标签（fail-soft：失败/未启用 → 空 set，不影响主流程）
    core_keyword_set: set[str] = set()
    try:
        from app.persistence.erp_writer.repository import ErpDualWriterRepository
        core_keyword_set = ErpDualWriterRepository.fetch_core_keyword_set(
            parent_asin,
            campaign_data.parent_seller_sku or "",
            campaign_data.shop_id or 0,
        )
    except Exception:
        logger.debug("core_keyword_set 不可用 [%s]，按空集继续", parent_asin, exc_info=True)

    # 3. LLM 分析前预过滤
    # 3a. 批量词活动：一个活动名下多个关键词，本期暂不处理
    #     → 已在 CampaignFetcher 硬过滤阶段排除 (campaign_prefilter.filter_campaigns)
    # 3b. 疑似已淘汰活动：Bid ≤ $0.20 且 预算 ≤ $1.00 → 不进 LLM (campaign_prefilter.filter_eliminated_pool)
    llm_campaigns, skipped_eliminated, pool_units = filter_eliminated_pool(campaign_data.campaigns)

    # 广告权限是本轮 Campaign 分析的运行时派生值，不进入战略上下文或持久化模型。
    # 新增/复活两个增长流只消费该权限，不直接以经营模式字符串做分支。
    try:
        mode = OperatingMode(str(strategy_context.operating_mode).strip())
    except ValueError:
        mode = None
    ad_permission = operating_mode_to_permission(mode)
    growth_analysis_enabled = (
        ad_permission != AdPermission.CLEARANCE_ONLY
    )

    if skipped_eliminated:
        logger.info("Campaign 预过滤 [%s]: 跳过 %d 个疑似已淘汰活动 (budget≈$1, bid≈$0.2)",
                     parent_asin, len(skipped_eliminated))

    # ── 淘汰池表同步 helper（discovery 入池 + 手动复评离池），fail-open ──
    # 两处调用：①全预过滤 early return 前  ②正常路径复评前
    # 单一定义防止逻辑漂移。
    async def _sync_pool_entries_if_needed() -> None:
        if not (settings.campaign_restart_enabled and pool_units):
            return
        _shop = getattr(fetcher, "_last_shop_account", "") or ""
        _live = list((campaign_data.campaigns if campaign_data else []) or [])
        try:
            from app.persistence.erp_writer.repository import _get_repository
            await asyncio.to_thread(
                _get_repository().sync_pool_entries,
                parent_asin, _live, _shop,
                (campaign_data.parent_seller_sku if campaign_data else None),
                (campaign_data.shop_id if campaign_data else None),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("sync_pool_entries 失败 [%s]: %s (fail-open)", parent_asin, e)

    total = len(llm_campaigns)

    # ── 预计算值（全预过滤 + 正常路径共用）────────────────────────────
    existing_kws = {
        (cu.keyword_text or "").strip().lower()
        for cu in campaign_data.campaigns if cu.keyword_text
    }
    pre_eliminated_count = len(skipped_eliminated)
    shop_account = getattr(fetcher, "_last_shop_account", "") or ""
    if not shop_account:
        from app.config.settings import settings as _ss
        from app.data.mcp_db_context import resolve_mcp_context_from_mcp
        if getattr(_ss, "mcp_resolve_context", False):
            try:
                from app.data.mcp_adapter import McpAdapter
                _shop_ctx = await asyncio.wait_for(
                    resolve_mcp_context_from_mcp(parent_asin, McpAdapter()),
                    timeout=getattr(_ss, "mcp_context_timeout", 30.0),
                )
                shop_account = (_shop_ctx.shop_account if _shop_ctx else "") or ""
            except Exception:
                shop_account = ""
        else:
            shop_account = ""
    target_child_asin = _pick_target_child_asin(campaign_data.campaigns)

    # ── 淘汰复评辅助函数（全预过滤 + 正常路径共用）─────────────────────
    async def _maybe_restart_review() -> tuple[list[CampaignAdjustmentItem], set[str]]:
        """KB21§7 淘汰复评。返回 (reactivate_items, reactivated_keys)。"""
        if not (growth_analysis_enabled and settings.campaign_restart_enabled and pool_units):
            return [], set()
        from app.persistence.erp_writer.repository import _get_repository
        repo = _get_repository()
        await _sync_pool_entries_if_needed()
        entry_dates: dict = {}
        try:
            entry_dates = await asyncio.to_thread(
                repo.get_active_entries, parent_asin,
            )
        except Exception as e:
            logger.warning("get_active_entries 失败 [%s]: %s (复评跳过)", parent_asin, e)
        if not entry_dates:
            return [], set()
        try:
            reactivate_items, reactivated_keys = await _run_restart_review(
                pool_units, entry_dates, fetcher, campaign_data, parent_asin,
            )
            if reactivate_items:
                _t(f"DONE restart_review ({len(reactivate_items)} 复评卡)")
            return reactivate_items, reactivated_keys
        except Exception as e:
            logger.warning("Campaign 复评异常 [%s]: %s (fail-open)", parent_asin, e)
            warnings_list.append(f"淘汰复评失败: {type(e).__name__}: {e}")
            return [], set()

    warnings_list: list[str] = []

    if total == 0:
        # ── 全预过滤：无活动可分析，但仍执行复评 + 新增活动分析 ──
        adjustments: list[CampaignAdjustmentItem] = []
        skipped = skipped_eliminated + (campaign_data.excluded or [])
        warnings_list.append("所有活动均在预过滤阶段被排除（疑似全部已淘汰）")

        ri, rk = await _maybe_restart_review()
        if ri:
            adjustments.extend(ri)
            skipped = [s for s in skipped if s.get("campaign_key") not in rk]

        new_campaigns: list = []
        nc_warnings: list[str] = []
        if settings.campaign_new_enabled and growth_analysis_enabled:
            try:
                new_campaigns, nc_warnings, _ = await analyze_new_campaigns(
                    fetcher=fetcher, reasoner=reasoner, parent_asin=parent_asin,
                    shop_id=campaign_data.shop_id,
                    parent_seller_sku=campaign_data.parent_seller_sku,
                    site_code=campaign_data.site_code,
                    shop_account=shop_account,
                    existing_keywords=existing_kws,
                    pre_eliminated_count=pre_eliminated_count,
                    strategy_context=strategy_context,
                    ctx_dict={},
                    temperature=temperature,
                    target_child_asin=target_child_asin,
                    days=days,
                    sem=asyncio.Semaphore(cc),
                    product_title=(asin_data.title or "") if asin_data else "",
                )
            except Exception as e:
                logger.warning("新增活动分析异常 [%s] (全预过滤): %s", parent_asin, e)
                nc_warnings.append(f"新增活动分析失败: {type(e).__name__}: {e}")
        warnings_list.extend(nc_warnings)

        return CampaignAnalysisResult(
            parent_asin=parent_asin, days=days, run_id=run_id,
            shop_id=campaign_data.shop_id,
            parent_seller_sku=campaign_data.parent_seller_sku,
            site_code=campaign_data.site_code,
            total_campaigns=0,
            adjustments=adjustments,
            new_campaigns=new_campaigns,
            new_campaigns_warnings=nc_warnings,
            skipped_campaigns=skipped,
            warnings=warnings_list,
            rounds_detail={},
            sanity_check_passed=True,
            llm_rounds_completed=0,
        )

    # ★ Portfolio 预算+花费不随 CampaignData 缓存，每次分析都重新拉取。
    # 放在两个 early return 之后：total_campaigns==0 和全预过滤 total==0 都不需要 portfolio。
    # shop_account 优先取缓存/预取 CampaignData，兼容旧对象时回退到 fetcher 上下文。
    try:
        portfolio_data = await fetcher.fetch_portfolio_list(
            parent_asin,
            parent_seller_sku=(campaign_data.parent_seller_sku
                               if campaign_data and campaign_data.parent_seller_sku else ""),
            shop_account=(campaign_data.shop_account
                          if campaign_data and campaign_data.shop_account
                          else getattr(fetcher, "_last_shop_account", "") or shop_account or ""),
            site_code=(campaign_data.site_code
                       if campaign_data and campaign_data.site_code else "Amazon_US"),
        )
        if portfolio_data:
            _t("DONE fetch_portfolio")
    except Exception as e:  # noqa: BLE001
        logger.warning("fetch_portfolio_list 异常 [%s]: %s (回退 60/20/20)", parent_asin, e)

    # ── 正常路径：有可分析活动 ──
    # 4. 按 match_type 分流
    exact_list = [cu for cu in llm_campaigns if cu.match_type == "EXACT"]
    broad_list = [cu for cu in llm_campaigns if cu.match_type != "EXACT"]
    _t(f"split: exact={len(exact_list)} broad={len(broad_list)}")

    # 4.1 组合预分类只反映当前真实归属；不再用旧 $5 规则推导精准活动的迁移目标。
    if settings.campaign_portfolio_enabled:
        for cu in llm_campaigns:
            current_groups = find_portfolio_group_matches(cu.current_portfolio_name)
            if (cu.match_type or "").upper() in {"BROAD", "PHRASE", "AUTO"}:
                cu.portfolio = PORTFOLIO_BROAD
            else:
                cu.portfolio = current_groups[0] if current_groups else ""
        # skipped_eliminated 也归入淘汰组(用于汇总展示一致)
        for s in skipped_eliminated:
            s["portfolio"] = PORTFOLIO_ELIMINATE

    ctx_dict = strategy_context.model_dump()
    exact_sem = asyncio.Semaphore(cc)   # 每流并发上限 = campaign_llm_concurrency
    broad_sem = asyncio.Semaphore(cc)
    new_sem = asyncio.Semaphore(cc)     # 新增活动线独立限流（与 exact/broad 对等）
    rounds_detail: dict[str, dict] = {}
    llm_rounds_completed = 1  # R1 必定执行；护栏重试轮次在循环内 max() 更新

    # 4.5 策略总览(执行总纲)：改为 gate task，与三流的 prefetch【重叠】跑（prefetch 不读 ctx_dict）。
    #     各流在 LLM 轮(_run_round)前 await gate → posture_brief 已注入，保证今日总纲一致。
    #     fail-open：gate 异常仅 warning，不阻塞三流。结果在三流 gather 后从 _ov_holder 取。
    strategic_overview: dict | None = None
    _ov_holder: dict = {}
    overview_gate: asyncio.Task | None = None
    if settings.campaign_overview_enabled:
        _ov_facts = _build_overview_facts(campaign_data, llm_campaigns, strategy_context)

        async def _run_overview_gate():
            try:
                obj = await _run_overview(reasoner, parent_asin, _ov_facts, ctx_dict, temperature)
                _ov_holder["overview"] = obj.model_dump()
                if obj.posture_brief:
                    ctx_dict["_strategic_overview_text"] = obj.posture_brief  # 原地注入，三流共享引用
                if obj.generated_by == "fallback":
                    warnings_list.append("策略总览 AI 生成失败/超时，仅展示现状数字（不影响明细）")
                _t("DONE strategic_overview")
            except Exception:
                logger.exception("策略总览 gate 异常 [%s]（fail-open，不阻塞三流）", parent_asin)
                warnings_list.append("策略总览生成异常，仅展示现状数字（不影响明细）")

        overview_gate = asyncio.create_task(_run_overview_gate())

    # 5. ★三股并行：精准流 / 广泛流 / 新增活动分析线（各自独立限流，互不阻塞）
    # return_exceptions=True：任一流抛未捕获异常 → 不连累其余流，转为 warning
    # 三股共享 ctx_dict（含 _strategic_overview_text = posture_brief），保证今日总纲一致
    async def _no_op_new_campaigns():
        return [], [], {}

    # ③ LLM 分批前
    await _cancel()
    stream_results = await asyncio.gather(
        _analyze_one_stream(
            exact_list, "exact", reasoner, fetcher, parent_asin, days,
            strategy_context, keyword_class_map, bs, exact_sem, temperature, ctx_dict,
            overview_gate=overview_gate, core_keyword_set=core_keyword_set,
            cancel_check=cancel_check,
        ),
        _analyze_one_stream(
            broad_list, "broad", reasoner, fetcher, parent_asin, days,
            strategy_context, keyword_class_map, bs, broad_sem, temperature, ctx_dict,
            overview_gate=overview_gate, core_keyword_set=core_keyword_set,
            cancel_check=cancel_check,
        ),
        (analyze_new_campaigns(
            fetcher=fetcher, reasoner=reasoner, parent_asin=parent_asin,
            shop_id=campaign_data.shop_id,
            parent_seller_sku=campaign_data.parent_seller_sku,
            site_code=campaign_data.site_code,
            shop_account=shop_account,
            existing_keywords=existing_kws,
            pre_eliminated_count=pre_eliminated_count,
            strategy_context=strategy_context,
            ctx_dict=ctx_dict,
            temperature=temperature,
            target_child_asin=target_child_asin,
            days=days,
            sem=new_sem,
            overview_gate=overview_gate,
            # 相关性锚点仅传标题（brand/category 已去除：品类太粗、会把 LLM 引向品类级误匹配，
            # 判别"短裙≠中长裙"靠标题具体属性）
            product_title=(asin_data.title or "") if asin_data else "",
            cancel_check=cancel_check,
        ) if settings.campaign_new_enabled and growth_analysis_enabled else _no_op_new_campaigns()),
        return_exceptions=True,
    )

    # 策略总览 gate 兜底等待（空流不会在内部 await）→ 取结果（fail-open，已在 gate 内吞异常）
    if overview_gate is not None:
        try:
            await overview_gate
        except Exception:
            pass
        strategic_overview = _ov_holder.get("overview")

    def _unpack_stream(r, label):
        if isinstance(r, AnalysisRunCancelled):
            raise r
        if isinstance(r, BaseException):
            logger.exception("Stream %s 异常 [%s]: %s", label, parent_asin, r)
            warnings_list.append(f"{label} 流分析异常: {type(r).__name__}: {r}")
            return [], {}, [], []
        return r

    (exact_adjustments, exact_rd, exact_summaries, exact_skipped) = _unpack_stream(stream_results[0], "exact")
    (broad_adjustments, broad_rd, broad_summaries, broad_skipped) = _unpack_stream(stream_results[1], "broad")

    # 新增活动线解包（返回 tuple[list[NewCampaignItem], list[str], dict[str,int]]）
    # 第 3 元 flow_sv_map = flow_keywords 全量搜索量映射，透传给预算回算 agent（零新增 MCP）。
    new_campaigns: list = []
    new_campaigns_warnings: list[str] = []
    flow_sv_map: dict = {}
    nc_result = stream_results[2]
    if isinstance(nc_result, BaseException):
        logger.exception("new_campaigns 流异常 [%s]: %s", parent_asin, nc_result)
        warnings_list.append(f"新增活动分析异常: {type(nc_result).__name__}: {nc_result}")
    else:
        new_campaigns, new_campaigns_warnings, flow_sv_map = nc_result
        warnings_list.extend(new_campaigns_warnings)

    rounds_detail["exact"] = exact_rd
    rounds_detail["broad"] = broad_rd
    _t(f"DONE exact+broad+new streams (new={len(new_campaigns)})")

    # 短期兜底：广泛流完全没拿到搜索词报告时显式 warning。
    # 搜索词无 Doris 回落，MCP 空/失败会静默 → 否词不可用，需让运营可见而非以为"无否词"。
    if broad_list:
        with_terms = sum(1 for s in broad_summaries if s.get("_search_term_data"))
        if with_terms == 0:
            warnings_list.append(
                f"广泛流 {len(broad_list)} 个活动均未取到搜索词报告（MCP 无数据/失败，暂无 Doris 回落）→ 否词建议不可用"
            )
            logger.warning("Campaign [%s] 广泛流搜索词全空，否词不可用", parent_asin)

    # 6. 合并两流结果（含预过滤活动，供前端可见）
    #    skipped_eliminated(淘汰池) + campaign_data.excluded(多词等) 均带 __prefiltered，
    #    exact/broad_skipped 是 LLM 丢失项(不带标记)，前端按标记区分"预过滤"vs"已丢失"
    adjustments = exact_adjustments + broad_adjustments
    skipped_campaigns = (
        exact_skipped + broad_skipped + skipped_eliminated + (campaign_data.excluded or [])
    )
    if skipped_campaigns:
        logger.warning(
            "Campaign analyze [%s]: %d 个活动未被分析（LLM 批次失败或未返回）",
            parent_asin, len(skipped_campaigns),
        )
    action_order = {"eliminate_to_low_bid_pool": 0, "adjust_bid": 1, "adjust_budget": 1, "adjust_placement": 1, "keep": 2}
    adjustments.sort(key=lambda x: action_order.get(x.action, 9))

    # 6b. 组合终分类: 用 LLM action 把"建议淘汰"的活动从主推/广泛重分到淘汰组
    #     必须在 _resolve_budget_conflicts 之前——后者会把淘汰活动 budget 改成 $1,
    #     之后再走 _is_in_elimination_pool 会误判一批"刚被强制淘汰"的活动。
    # 用 unit_lookup 把 Doris 上下文独有字段补到 adjustment 上 (LLM 不产出这些)
    unit_by_key = {cu.campaign_key: cu for cu in llm_campaigns}

    def _rank_evidence_line(cu) -> str:
        """自然排名证据行（代码事实，非 LLM 产出）。"""
        if cu.natural_rank is not None:
            if cu.rank_change is not None and cu.rank_change != 0:
                sym = "↑" if cu.rank_change > 0 else "↓"
                return f"自然排名第{cu.natural_rank}位（较上次{sym}{abs(cu.rank_change)}）"
            return f"自然排名第{cu.natural_rank}位"
        if cu.near_natural_rank is not None:
            return f"已掉榜（上次自然排名第{cu.near_natural_rank}位）"
        return ""

    _backfill_campaign_adjustment_context(adjustments, unit_by_key, keyword_class_map, _rank_evidence_line,
                                          core_keyword_set=core_keyword_set)

    # 6c. 复评保护回填：取池表最近 N 天内离池（= 被复评捞回）的 campaign_id 集合，
    #     算 days_since_reactivation，供 _resolve_budget_conflicts 防淘汰↔复评抖动。
    #     在 _resolve_budget_conflicts 之前、回填循环之后执行。
    recent_reactivated: dict[str, int] = {}
    try:
        from app.persistence.erp_writer.repository import _get_repository
        recent_reactivated = _get_repository().get_recently_reactivated(parent_asin, max_days=3)
    except Exception:
        pass  # fail-open: DB 不通则无保护，不影响主分析
    for item in adjustments:
        cid = (item.campaign_id or "").strip()
        item.days_since_reactivation = recent_reactivated.get(cid, -1)

    # 7. 护栏 + R2/R3/R4 重试编排
    #    护栏失败项与 R1 缺失补答项统一进重试循环；告警跨轮累积（retry_instruction 逐条去重）。
    #    送进去没吐出来的 key 一律带入下一轮，R4 后由最终护栏兜底。
    #    精准确定性规则（32号）不在此处执行——必须等 LLM 全部轮次结束后再补建/覆写，
    #    否则补建结果会被后续 LLM 重试 replacement 覆盖（spec §0.5）。
    retry_rounds = ((2, "R2"), (3, "R3"), (4, "R4"))
    guardrail_alert_history: dict[str, list[str]] = {}
    guardrail_rounds: dict[str, dict] = {}
    unresolved_keys: set[str] = {
        s.get("campaign_key", "")
        for s in (exact_skipped + broad_skipped)
        if s.get("campaign_key")
    }
    for round_number, round_label in retry_rounds:
        guardrail_pass, budget_warnings = _apply_campaign_guardrails(
            adjustments,
            product_stage=strategy_context.product_stage,
            inventory_days=strategy_context.inventory_days,
            refund_rate=strategy_context.refund_rate,
            rating=strategy_context.rating,
        )
        warnings_list.extend(budget_warnings)

        # 告警累积（逐条 retry_instruction 去重，跨轮保留）
        for r in guardrail_pass.results:
            if not r.corrected:
                continue
            key = getattr(r, "campaign_key", "") or ""
            instruction = (getattr(r, "retry_instruction", "") or "").strip()
            if not key or not instruction:
                continue
            entries = guardrail_alert_history.setdefault(key, [])
            if instruction not in entries:
                entries.append(instruction)
        cumulative_alerts = {k: "\n".join(v) for k, v in guardrail_alert_history.items()}

        # 护栏失败项（仅 retry_instruction 非空，与既有行为一致）
        alerts = _build_guardrail_alerts(guardrail_pass)
        guardrail_failed = set(alerts)

        # 合并入口：护栏失败 ∪ 待补答
        retry_keys = guardrail_failed | unresolved_keys
        guardrail_rounds[round_label] = {
            "ran": True,
            "corrections": guardrail_pass.corrections,
            "failed": len(guardrail_failed),
            "unresolved": len(unresolved_keys),
            "retried": len(retry_keys),
            "items_returned": 0,
            "recovered": 0,
        }
        if not retry_keys:
            logger.info(
                "Guardrail %s pass [%s]: corrections=%d rules=%s",
                round_label, parent_asin, guardrail_pass.corrections,
                _guardrail_rule_counts(guardrail_pass),
            )
            break
        logger.info(
            "Guardrail %s blocked [%s]: corrections=%d failed=%d unresolved=%d retry=%d rules=%s",
            round_label, parent_asin, guardrail_pass.corrections,
            len(guardrail_failed), len(unresolved_keys), len(retry_keys),
            _guardrail_rule_counts(guardrail_pass),
        )

        # 构建重试源（护栏失败项 + 缺失项）
        retry_source = []
        for s in (exact_summaries + broad_summaries):
            if s.get("campaign_key") in retry_keys:
                retry_source.append(dict(s))
        if not retry_source:
            logger.warning(
                "Guardrail %s retry skipped [%s]: no source summaries for retry_keys=%s",
                round_label, parent_asin, sorted(retry_keys),
            )
            if unresolved_keys:
                continue  # 缺失项仍需补答，带到下一轮
            break

        exact_retry = [s for s in retry_source if (s.get("match_type") or "").upper() == "EXACT"]
        broad_retry = [s for s in retry_source if (s.get("match_type") or "").upper() != "EXACT"]
        logger.info(
            "Guardrail %s retry source [%s]: exact=%d broad=%d missing_source=%s",
            round_label, parent_asin, len(exact_retry), len(broad_retry),
            sorted(retry_keys - {s.get("campaign_key") for s in retry_source}),
        )
        alert_text = _GUARDRAIL_RETRY_INSTRUCTION
        retry_items: list[CampaignAdjustmentItem] = []

        for retry_summaries, task_type_name, retry_sem in (
            (exact_retry, "exact", exact_sem),
            (broad_retry, "broad", broad_sem),
        ):
            if not retry_summaries:
                continue
            # 循环内注入累计告警（护栏失败项有告警，纯缺失项无告警）
            _inject_alerts_to_summaries(retry_summaries, cumulative_alerts)
            ctx_dict["_guardrail_instruction"] = alert_text
            try:
                results = await _run_round(
                    reasoner, parent_asin,
                    _build_batches(retry_summaries, bs, seed=round_number),
                    ctx_dict, temperature, retry_sem, round_number,
                    task_type=task_type_name,
                    cancel_check=cancel_check,
                )
                for br in results:
                    retry_items.extend(br.items)
                llm_rounds_completed = max(llm_rounds_completed, round_number)
                logger.info(
                    "Guardrail %s retry result [%s|%s]: batches=%s items=%d",
                    round_label, parent_asin, task_type_name,
                    _round_stats(results), len(retry_items),
                )
            except AnalysisRunCancelled:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("Guardrail %s retry failed [%s|%s]: %s", round_label, parent_asin, task_type_name, e)
            finally:
                ctx_dict.pop("_guardrail_instruction", None)

        returned_keys = {item.campaign_key for item in retry_items}
        guardrail_rounds[round_label]["items_returned"] = len(retry_items)

        if not retry_items:
            logger.warning(
                "Guardrail %s empty result [%s]: %d retry_keys carried to next round",
                round_label, parent_asin, len(retry_keys),
            )
            continue

        # 统一 upsert：已在 adjustments → 替换；不在 → 追加。不区分护栏失败/缺失补答。
        # 替换前快照旧值，供 before→after 诊断日志（旧值瞬态，覆盖即丢）
        old_by_key = {item.campaign_key: item for item in adjustments}
        replacement_by_key: dict[str, CampaignAdjustmentItem] = {}
        newly_appended: list[CampaignAdjustmentItem] = []
        for ri in retry_items:
            key = ri.campaign_key
            ri.confidence = "medium"
            replaced = False
            for idx, item in enumerate(adjustments):
                if item.campaign_key == key:
                    replacement_by_key[key] = ri
                    adjustments[idx] = ri
                    replaced = True
                    break
            if not replaced:
                adjustments.append(ri)
                newly_appended.append(ri)

        if replacement_by_key:
            old_snapshot = [old_by_key[k] for k in replacement_by_key]
            logger.info(
                "Guardrail %s replacement [%s]: %d changed %s",
                round_label, parent_asin, len(replacement_by_key),
                _guardrail_replacement_summary(old_snapshot, replacement_by_key),
            )

        # 回填 + 从 skipped 移除
        recovered_keys = {item.campaign_key for item in newly_appended}
        guardrail_rounds[round_label]["recovered"] = len(recovered_keys)
        if recovered_keys:
            skipped_campaigns[:] = [
                s for s in skipped_campaigns
                if s.get("campaign_key") not in recovered_keys
            ]
            _backfill_campaign_adjustment_context(adjustments, unit_by_key, keyword_class_map, _rank_evidence_line,
                                                  core_keyword_set=core_keyword_set)
            _backfill_placement_pcts(adjustments, unit_by_key)
            for item in adjustments:
                cid = (item.campaign_id or "").strip()
                item.days_since_reactivation = recent_reactivated.get(cid, -1)
            logger.info(
                "Guardrail %s recovered [%s]: %d campaigns, skipped now=%d",
                round_label, parent_asin, len(recovered_keys), len(skipped_campaigns),
            )

        # 下轮待补答：本轮送进去但没吐出来的，一律带入下一轮
        unresolved_keys = retry_keys - returned_keys
        if unresolved_keys:
            logger.warning(
                "Guardrail %s still unresolved [%s]: %s",
                round_label, parent_asin, sorted(unresolved_keys),
            )
    else:
        guardrail_pass, budget_warnings = _apply_campaign_guardrails(
            adjustments,
            product_stage=strategy_context.product_stage,
            inventory_days=strategy_context.inventory_days,
            refund_rate=strategy_context.refund_rate,
            rating=strategy_context.rating,
        )
        warnings_list.extend(budget_warnings)
        logger.warning(
            "Guardrail final fallback after R4 [%s]: corrections=%d rules=%s snapshot=%s",
            parent_asin, guardrail_pass.corrections, _guardrail_rule_counts(guardrail_pass),
            _guardrail_snapshot(adjustments, set(_build_guardrail_alerts(guardrail_pass))),
        )
        rounds_detail["guardrail_final"] = {
            "corrections": guardrail_pass.corrections,
            "rules": _guardrail_rule_counts(guardrail_pass),
        }

    rounds_detail["guardrail"] = guardrail_rounds

    # 6d. 精准确定性规则（32号）—— LLM 全部轮次（R1-R4）结束后执行。
    #     只生成独立迁组补丁，不覆写 LLM 调整字段；补丁不会被 LLM 重试丢失。
    #     §0.1 冻结门禁：未验收前不生成 target_group_type；日快照仍正常积累。
    exact_created_keys: set[str] = set()
    exact_group_targets: dict[str, str] = {}
    if getattr(settings, "exact_transition_enabled", False):
        exact_created_keys, exact_group_targets = _apply_exact_transition_rules(
            adjustments, unit_by_key, strategy_context,
            campaign_data, core_keyword_set,
        )
        # 补建的活动已有确定性 item，从 skipped 中移除，避免前端同时展示"已丢失"与调整项
        if exact_created_keys:
            skipped_campaigns = [
                s for s in skipped_campaigns
                if (
                    s.get("campaign_key", "") if isinstance(s, dict)
                    else getattr(s, "campaign_key", "")
                ) not in exact_created_keys
            ]
            # 补建 item 补齐上下文字段（keyword_class/is_core/自然位 evidence 等），与重试 recovered 后一致
            _backfill_campaign_adjustment_context(
                adjustments, unit_by_key, keyword_class_map,
                _rank_evidence_line, core_keyword_set=core_keyword_set,
            )
            _backfill_placement_pcts(adjustments, unit_by_key)
            for item in adjustments:
                cid = (item.campaign_id or "").strip()
                item.days_since_reactivation = recent_reactivated.get(cid, -1)

    # 7a. 补答/替换可能打乱 action 顺序，重排
    adjustments.sort(key=lambda x: action_order.get(x.action, 9))

    # 7b. 组合目标收拢：广泛/词组/自动固定进入自动广泛组；精准活动暂不走旧 $5 迁移。
    # 必须在护栏后、落库前：已有调整项保留其预算/Bid，仅补活动级 target；
    # 未被 LLM 返回的广泛活动则补一条纯挪组项。执行侧只消费 pending 的 target 字段。
    if settings.campaign_portfolio_enabled:
        _reconcile_portfolio_targets(
            adjustments,
            campaign_data.campaigns,
            campaign_data.excluded or [],
        )

    # 7c. 淘汰活动复评（KB 21 §7，确定性规则引擎，无 LLM）。
    #   位置关键：必须在 _resolve_budget_conflicts(§7) 之后——否则"低价捡漏强制淘汰兜底"会因
    #   复评项 current=$1/$0.20 把它打回 eliminate。复评项已带完整字段，无需 backfill/终态分类。
    #   实现委托 _maybe_restart_review()（全预过滤路径共用）。
    ri, rk = await _maybe_restart_review()
    if ri:
        adjustments.extend(ri)
        skipped_campaigns = [
            s for s in skipped_campaigns
            if s.get("campaign_key") not in rk
        ]

    # 8 + 8b：Sanity check 与 AI 汇总合成【并行】。
    # 两者都只读已定稿的 adjustments，产出独立（warnings vs 分组叙事），无数据依赖 →
    # gather 省墙钟（约 20-55s）。各自吞异常 + 返回自身 warnings，避免并发改 warnings_list。
    async def _run_sanity() -> tuple[list[str], bool]:
        # sanity_ok 默认 False：未运行/有批次失败都按"未通过"展示，仅全批次成功才 True
        if not settings.campaign_sanity_enabled:
            logger.info("Campaign sanity check 已禁用 [%s]", parent_asin)
            return [], False
        try:
            sc_warnings, ok = await _sanity_check_batched(
                reasoner, parent_asin, adjustments,
                exact_summaries + broad_summaries,
                ctx_dict, temperature,
            )
            _t("DONE sanity_check")
            return sc_warnings, ok
        except Exception as e:
            logger.warning("Campaign sanity check 失败 [%s]: %s", parent_asin, e)
            return [f"sanity_check 执行失败: {e}"], False

    async def _run_synth() -> tuple[dict | None, list[str]]:
        # timeout_override=55：httpx socket 层自断，不依赖 asyncio 取消
        if not (settings.campaign_synthesis_enabled and adjustments):
            if adjustments:
                logger.info("Campaign synthesis 已禁用 [%s]", parent_asin)
            return None, []
        try:
            syn = await reasoner.recommend_campaign_synthesis(
                asin=parent_asin,
                adjustments=adjustments,
                strategy_context=ctx_dict,
                temperature=temperature,
                timeout_override=55,
            )
            _t("DONE synthesis")
            if syn and syn.get("error"):
                return syn, [f"AI 汇总合成失败: {syn['error']}"]
            return syn, []
        except Exception as e:
            logger.warning("Campaign synthesis 异常 [%s]: %s", parent_asin, e)
            return None, [f"AI 汇总合成异常: {e}"]

    async def _run_realloc() -> dict | None:
        # 预算回算：agent 优先（KB23），失败/超时/校验不过 → 规则引擎兜底（build_summary）。
        # 与 sanity/synth 同为 merge 后独立 LLM 调用 → 并入 gather 并行，无额外串行延迟。
        if not settings.campaign_portfolio_enabled:
            return None
        from app.workflow.steps.campaign_budget_summary import build_summary
        from app.workflow.steps import campaign_budget_reallocation as bra

        def _fallback(reason: str = "", source: str = "rule_fallback") -> dict | None:
            try:
                bs = build_summary(adjustments=adjustments, all_units=llm_campaigns,
                                   ctx=strategy_context, portfolio_data=portfolio_data)
                if bs is not None:
                    bs["source"] = source
                if reason:
                    warnings_list.append(f"预算回算 agent 回落规则引擎: {reason}")
                return bs
            except Exception as e:  # noqa: BLE001
                logger.exception("budget_summary 规则兜底失败 [%s]: %s", parent_asin, e)
                warnings_list.append(f"预算汇总构建失败: {type(e).__name__}: {e}")
                return None

        if not (settings.campaign_budget_agent_enabled and adjustments):
            return _fallback(source="rule")          # agent 关 / 无调整 → 规则引擎（正常路径，无 warning）
        try:
            agg = bra.aggregate(adjustments, llm_campaigns, strategy_context,
                                search_volume_map=flow_sv_map, new_campaigns=new_campaigns,
                                portfolio_data=portfolio_data)
            # ★ 3 活跃组全为零 → 跳过回算 LLM，直接以 portfolio 真实值（或 $0）兜底
            if agg.get("parent", {}).get("all_active_zero"):
                warnings_list.append(
                    "未查询到组合信息，请检查本产品是否完成组合创建初始化！"
                )
                return _fallback(source="portfolio_all_zero")

            agent_out = await reasoner.recommend_budget_reallocation(
                parent_asin, agg, temperature=temperature,
            )
            if not isinstance(agent_out, dict) or agent_out.get("error"):
                return _fallback((agent_out or {}).get("error", "agent 返回非法"))
            ok, why = bra.validate(agent_out, agg)
            if not ok:
                return _fallback(why)
            _t("DONE budget_realloc(agent)")
            return bra.to_budget_summary(agent_out, agg, portfolio_data=portfolio_data, source="agent")
        except Exception as e:  # noqa: BLE001
            logger.exception("预算回算 agent 异常 [%s]: %s", parent_asin, e)
            return _fallback(f"{type(e).__name__}: {e}")

    # ④ 汇总 LLM 前
    await _cancel()
    sanity_res, synth_res, realloc_res = await asyncio.gather(
        _run_sanity(), _run_synth(), _run_realloc(), return_exceptions=True,
    )

    if isinstance(sanity_res, BaseException):
        logger.warning("Campaign sanity gather 异常 [%s]: %s", parent_asin, sanity_res)
        sanity_ok = False
    else:
        sc_warnings, sanity_ok = sanity_res
        warnings_list.extend(sc_warnings)

    synthesis: dict | None = None
    if isinstance(synth_res, BaseException):
        logger.warning("Campaign synthesis gather 异常 [%s]: %s", parent_asin, synth_res)
        warnings_list.append(f"AI 汇总合成 gather 异常: {synth_res}")
    else:
        synthesis, synth_warns = synth_res
        warnings_list.extend(synth_warns)

    # 9. 汇总统计
    summary_stats = {
        "to_eliminate": sum(1 for a in adjustments if a.action == "eliminate_to_low_bid_pool"),
        "to_adjust": sum(1 for a in adjustments if a.action.startswith("adjust")),
        "to_keep": sum(1 for a in adjustments if a.action == "keep"),
        "to_reactivate": sum(1 for a in adjustments if (a.action or "").startswith("reactivate")),
        "confidence_high": sum(1 for a in adjustments if a.confidence == "high"),
        "confidence_medium": sum(1 for a in adjustments if a.confidence == "medium"),
        "confidence_low": sum(1 for a in adjustments if a.confidence == "low"),
        "estimated_budget_impact": round(sum(
            (a.proposed_budget or 0) - (a.current_budget or 0)
            for a in adjustments
        ), 2),
    }

    # 10. 预算汇总：_run_realloc 已在上方 gather 并行算好（agent 优先 + 规则兜底，恒返有效或 None）
    budget_summary: dict | None = None
    if isinstance(realloc_res, BaseException):
        logger.warning("Campaign budget realloc gather 异常 [%s]: %s", parent_asin, realloc_res)
        warnings_list.append(f"预算回算 gather 异常: {realloc_res}")
    else:
        budget_summary = realloc_res
    _t("DONE budget_summary")

    _t("DONE total")
    return CampaignAnalysisResult(
        parent_asin=parent_asin, days=days, run_id=run_id,
        shop_id=campaign_data.shop_id,
        shop_account=shop_account,
        parent_seller_sku=campaign_data.parent_seller_sku,
        site_code=campaign_data.site_code,
        total_campaigns=len(llm_campaigns),
        adjustments=adjustments,
        campaign_group_targets=exact_group_targets,
        new_campaigns=new_campaigns,
        new_campaigns_warnings=new_campaigns_warnings,
        skipped_campaigns=skipped_campaigns,
        strategic_overview=strategic_overview,
        synthesis=synthesis,
        budget_summary=budget_summary,
        summary=summary_stats,
        warnings=warnings_list,
        sanity_check_passed=sanity_ok,
        llm_rounds_completed=llm_rounds_completed,
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
    # 广告方向：运营 tab4「生成评估报告」已选并持久化到 workflow_state.execution
    ad_directions = (wf_state.get("execution") or {}).get("selected_directions") or []

    # ASIN 数据
    asin_data = await ctx.ensure_data(asin, days=days, refresh=refresh)

    # 组装策略上下文
    strat_ctx = build_campaign_strategy_context(
        asin, asin_data, long_term, keyword_analysis, ad_directions, days=days,
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

    # ── 15号§4 有效容忍上限 + 03号§7 目标 CPA 预计算 ──
    fill_acos_constraints(strat_ctx, asin_data)

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


# 广告方向 英文 id（前端 selected_directions 存的形态）→ 中文方向名。
# 所有下游消费方（总览 prompt / 逐活动 prompt / KB23 回算 has_ranking_push）均按中文匹配，
# 故在此唯一汇聚点统一翻译；已是中文的原样返回（fallback），幂等安全。
_AD_DIRECTION_ID_ZH = {
    "push_natural": "推进自然位",
    "push_natural_rank": "推进自然位",
    "expand_keywords": "新增扩词",
    "optimize_acos": "优化ACOS",
    "balance_maintain": "平衡维持",
}


def _zh_ad_direction(d) -> str:
    return _AD_DIRECTION_ID_ZH.get(str(d).strip().lower(), str(d))


def _apply_exact_transition_rules(
    adjustments: list[CampaignAdjustmentItem],
    unit_by_key: dict[str, CampaignUnit],
    strat_ctx: CampaignStrategyContext,
    campaign_data: CampaignData,
    core_keyword_set: set[str],
) -> tuple[set[str], dict[str, str]]:
    """精准确定性规则（32号）：生成独立的活动迁组补丁。

    基于全部 CampaignUnit（非仅 LLM adjustments）——LLM 未返回的活动同样进入规则，
    命中迁组时补建纯挪组 item（spec §0.5）。在 LLM 全部轮次和护栏结束后执行。

    返回 (补建 campaign_key 集合, {campaign_id: target_group_type})。
    迁组补丁不覆盖已有 LLM adjustment；持久化侧按活动合并到 campaign_pending。
    """
    from app.workflow.steps.campaign_exact_transition import evaluate_exact_transition

    created_keys: set[str] = set()
    group_targets: dict[str, str] = {}

    if not strat_ctx.effective_acos_tolerance:
        return created_keys, group_targets

    shop_id = campaign_data.shop_id or 0
    site_code = campaign_data.site_code or ""
    parent_asin = campaign_data.parent_asin or ""
    if not shop_id or not site_code or not parent_asin:
        return created_keys, group_targets

    repo = None
    try:
        from app.persistence.erp_writer.repository import _get_repository
        repo = _get_repository()
    except Exception:
        return created_keys, group_targets

    # 已存在的 adjustment 按 campaign_key 索引；仅判断是否需要补建，不覆写已有项。
    item_by_key = {item.campaign_key: item for item in adjustments}

    for cu in unit_by_key.values():
        # 只处理单关键词 EXACT
        if cu.match_type != "EXACT" or not cu.keyword_id:
            continue
        campaign_id = cu.campaign_id or ""
        if not campaign_id:
            continue

        try:
            metric_daily = repo.list_campaign_metric_daily(
                shop_id=shop_id, site_code=site_code,
                parent_asin=parent_asin, campaign_id=campaign_id,
                limit=21,
            )
            lifecycle = repo.get_exact_lifecycle(
                shop_id=shop_id, site_code=site_code, campaign_id=campaign_id,
            )
            is_core = (cu.keyword_text or "").strip().lower() in {
                kw.strip().lower() for kw in core_keyword_set
            }

            decision = evaluate_exact_transition(
                current_group_type=cu.current_group_type,
                match_type=cu.match_type,
                keyword_id=cu.keyword_id,
                metric_daily=metric_daily,
                lifecycle=lifecycle,
                target_acos=float(strat_ctx.target_acos or 0),
                effective_acos_tolerance=strat_ctx.effective_acos_tolerance,
                current_bid=cu.current_bid,
                is_core_keyword=is_core,
            )
            if not decision.triggered:
                continue
            # stay_with_adjustment 仅表示继续观察：没有可执行 action，
            # 既不能补建活动调整卡，也不能覆盖已有的 LLM 调整。
            if not decision.action:
                continue

            group_targets[campaign_id] = decision.target_group_type
            created = cu.campaign_key not in item_by_key
            if created:
                # LLM 未返回该活动：补建纯迁组卡；不携带规则引擎的预算/Bid/Placement 值。
                item = CampaignAdjustmentItem(
                    campaign_name=cu.campaign_name,
                    campaign_key=cu.campaign_key,
                    campaign_id=campaign_id,
                    child_asin=cu.child_asin,
                    keyword_text=cu.keyword_text,
                    match_type=cu.match_type,
                    action="keep",
                    current_budget=cu.current_budget,
                    current_bid=cu.current_bid,
                    perf_7d=cu.perf_7d.model_dump() if cu.perf_7d else {},
                    triggered_rule=f"EXACT_TRANSITION:{decision.transition_type}",
                    reason="; ".join(decision.evidence),
                    evidence=decision.evidence,
                )
                adjustments.append(item)
                item_by_key[cu.campaign_key] = item
                created_keys.add(cu.campaign_key)

            logger.info(
                "精准规则 [%s] %s: %s → %s (%s)%s",
                parent_asin, cu.keyword_text, cu.current_group_type,
                decision.target_group_type, decision.transition_type,
                " [补建]" if created else "",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "精准规则异常 [%s/%s]: %s", campaign_id, cu.keyword_text, e,
                exc_info=True,
            )

    return created_keys, group_targets


def fill_acos_constraints(
    strat_ctx: CampaignStrategyContext, asin_data: ASINData,
) -> None:
    """target_acos 确定后立即调用：填充 effective_acos_tolerance / target_cpa / components。

    两项修正在此处完成，不委托给 compute_acos_tolerance：
    1. 测试期超 60 天 → 阶段加值归零（15号§4.2 C-17）
    2. PRC-005：毛利率<25% 且退货率≥25% → 女装修正 −5pp
    """
    if strat_ctx.target_acos is None:
        return

    # ── avg_order_value ──
    ad = getattr(asin_data, "ad_data", None)
    avg_order_value: float | None = None
    if ad:
        sales = getattr(ad, "sales", None)
        orders = getattr(ad, "orders", None)
        if sales is not None and orders is not None and orders > 0:
            avg_order_value = round(sales / orders, 2)

    # ── 15号§4.2 C-17：测试期超 60 天 → 阶段加值归零 ──
    product_stage = strat_ctx.product_stage
    days_since_launch = getattr(asin_data, "days_since_launch", None)
    if product_stage == "测试期" and days_since_launch is not None and days_since_launch > 60:
        product_stage = "维持期"

    # ── 24号 PRC-005：小数比例，margin<0.25 且 refund_rate>=0.25 ──
    margin = getattr(asin_data, "margin", None)
    refund_rate = getattr(asin_data, "refund_rate", None)
    is_women_clothing_refund = (
        margin is not None
        and refund_rate is not None
        and margin < 0.25
        and refund_rate >= 0.25
    )

    result = compute_acos_tolerance(
        target_acos=float(strat_ctx.target_acos),
        product_stage=product_stage,
        operating_mode=strat_ctx.operating_mode,
        ad_purposes=strat_ctx.ad_purposes,
        season_stage=strat_ctx.season_stage,
        avg_order_value=avg_order_value,
        is_promotion=False,  # TODO(promotion-context): 促销信号暂不可用
        is_women_clothing_refund=is_women_clothing_refund,
    )
    strat_ctx.effective_acos_tolerance = result.effective_tolerance
    strat_ctx.tolerance_components = result.components
    strat_ctx.target_cpa = result.target_cpa
    strat_ctx.avg_order_value = result.avg_order_value


def build_campaign_strategy_context(
    asin: str,
    asin_data: ASINData,
    long_term: dict,
    keyword_analysis: dict | None = None,
    ad_directions: list[str] | None = None,
    *,
    days: int = 7,
) -> CampaignStrategyContext:
    """从 ASINData + long_term_config + keyword_analysis 组装 ASIN 级上下文。

    字段映射（已核实）：
    - rating → asin_data.rating (非 signals.recent_rating_change)
    - refund_rate → asin_data.refund_rate
    - inventory_days → 计算值 (inventory_qty / avg_daily_sales_30d)
    - target_keyword_strategy → long_term.get("target_keyword_strategy", [])
    - ad_directions → 调用方从 workflow_state.execution.selected_directions 取（运营 tab4 已选）
    - daily_budget → long_term.daily_budget_override → asin_data.daily_budget 回落
    - target_acos → 仍由调用方三级回落填充（override→P3→算法）
    - days → 用于 daily_budget 兜底:asin_data.ad_data.spend 是 days 窗口总花费,
            兜底 daily_avg = spend / max(days, 1)。默认 7 与 ad_data 拉取窗口一致。
    """
    flags: list[str] = []

    # 每日预算三级兜底:
    #   1. long_term.daily_budget_override (运营手动设定)
    #   2. asin_data.daily_budget (数仓拉取)
    #   3. 日均广告花费 × campaign_budget_fallback_multiplier (兜底,通常 1.15)
    # daily_budget_source 标记来源,前端用于显示"按花费兜底"提示。
    daily_budget: float | None = None
    daily_budget_source: str = ""

    override_val = long_term.get("daily_budget_override")
    if override_val is not None:
        daily_budget = float(override_val)
        daily_budget_source = "override"
    else:
        asin_val = getattr(asin_data, "daily_budget", None)
        if asin_val is not None:
            daily_budget = float(asin_val)
            daily_budget_source = "asin_data"
        else:
            # 兜底: 日均广告花费 × 1.15
            # ad_data.spend 是 days 窗口总花费,除以 days 得日均
            ad = getattr(asin_data, "ad_data", None)
            spend = getattr(ad, "spend", None) if ad else None
            if spend is not None and spend > 0:
                window_days = max(int(days), 1)  # 防 0/负数
                daily_avg_spend = spend / window_days
                daily_budget = round(
                    daily_avg_spend * settings.campaign_budget_fallback_multiplier, 2
                )
                daily_budget_source = "fallback_spend_x1.15"
                flags.append(
                    f"目标预算未配置,按近 {window_days} 天日均花费 ${daily_avg_spend:.2f} × "
                    f"{settings.campaign_budget_fallback_multiplier} 兜底"
                )

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

    if asin_data.refund_rate is not None and asin_data.refund_rate >= 30:
        flags.append(f"退货率 {asin_data.refund_rate:.1f}% (≥30%，阻断 TOS 广告位上调)")

    discount = long_term.get("discount_rate")
    if discount is not None:
        flags.append(f"折扣率 {discount}% (Listing优化中，禁止大动作)")

    operating_mode = long_term.get("operating_mode") or ""

    return CampaignStrategyContext(
        parent_asin=asin,
        # MySQL/ERP 历史配置允许 product_stage 为 NULL；Campaign 上下文
        # 仍以字符串承载，空值统一为 ""，避免在 LLM/护栏之前中断分析。
        product_stage=long_term.get("product_stage") or "",
        product_level=long_term.get("product_level", ""),
        season_stage=long_term.get("season_stage", ""),
        operating_mode=operating_mode,
        ad_purposes=long_term.get("ad_purposes", []),
        ad_directions=[_zh_ad_direction(d) for d in (ad_directions or [])],
        target_keyword_strategy=long_term.get("target_keyword_strategy", []),
        margin=asin_data.margin,
        natural_order_ratio=asin_data.natural_order_ratio,
        rating=asin_data.rating,
        refund_rate=asin_data.refund_rate,
        inventory_qty=qty,
        inventory_days=inventory_days,
        avg_daily_sales_30d=asin_data.avg_daily_sales_30d,
        target_acos=None,  # 由调用方填充 (resolve_target_acos)
        daily_budget=daily_budget,
        daily_budget_source=daily_budget_source,
        warning_flags=flags,
    )


# ── 策略总览(执行总纲) ────────────────────────────────────────────────────────


def _build_overview_facts(
    campaign_data: CampaignData,
    llm_campaigns: list[CampaignUnit],
    strat_ctx: CampaignStrategyContext,
) -> dict:
    """确定性算总览「现状」数字 —— 纯当前状态，不调 LLM、不引用任何分析结果。

    ACOS 分桶以**运营目标 ACOS**为界（权威值），不碰争议的"有效容忍上限"。
    """
    total = len(llm_campaigns)
    exact = sum(1 for cu in llm_campaigns if cu.match_type == "EXACT")
    total_budget = round(sum((cu.current_budget or 0) for cu in llm_campaigns), 2)

    target = strat_ctx.target_acos
    acos_pass = acos_over = acos_zero = 0
    for cu in llm_campaigns:
        p = cu.perf_7d
        if (p.cost or 0) <= 0:
            acos_zero += 1          # 零花费
        elif p.acos is None or target is None:
            continue                # 无法判定达标/超标，不计入
        elif p.acos <= target:
            acos_pass += 1          # ≤目标
        else:
            acos_over += 1          # >目标

    return {
        "product_stage": strat_ctx.product_stage,
        "product_level": strat_ctx.product_level,
        "season_stage": strat_ctx.season_stage,
        "operating_mode": strat_ctx.operating_mode,
        "ad_purposes": strat_ctx.ad_purposes,
        "target_keyword_strategy": strat_ctx.target_keyword_strategy,
        "ad_directions": strat_ctx.ad_directions,
        "target_acos": target,
        "daily_budget": strat_ctx.daily_budget,
        "margin": strat_ctx.margin,
        "rating": strat_ctx.rating,
        "refund_rate": strat_ctx.refund_rate,
        "inventory_days": strat_ctx.inventory_days,
        "natural_order_ratio": strat_ctx.natural_order_ratio,
        "total_campaigns": total,
        "exact_campaigns": exact,
        "broad_campaigns": total - exact,
        "total_current_budget": total_budget,
        "acos_pass": acos_pass,
        "acos_over": acos_over,
        "acos_zero_spend": acos_zero,
        "warning_flags": strat_ctx.warning_flags,
    }


async def _run_overview(
    reasoner: "LLMReasoner",
    parent_asin: str,
    facts: dict,
    ctx_dict: dict,
    temperature: float,
) -> CampaignStrategicOverview:
    """调 LLM 生成执行总纲三段 + posture_brief；fail-open：失败返回 facts-only。"""
    try:
        res = await reasoner.recommend_campaign_overview(
            asin=parent_asin, facts=facts, strategy_context=ctx_dict,
            temperature=temperature, timeout_override=55,
        )
        if not isinstance(res, dict) or res.get("error"):
            return CampaignStrategicOverview(facts=facts, generated_by="fallback")
        return CampaignStrategicOverview(
            facts=facts,
            assessment_text=res.get("assessment_text", ""),
            direction_text=res.get("direction_text", ""),
            posture_brief=res.get("posture_brief", ""),
            generated_by="ai",
        )
    except Exception as e:
        logger.warning("Campaign overview 异常 [%s]: %s", parent_asin, e)
        return CampaignStrategicOverview(facts=facts, generated_by="fallback")


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
    overview_gate: "asyncio.Task | None" = None,
    *,
    core_keyword_set: set[str] | None = None,
    cancel_check: Callable[[], Awaitable[None]] | None = None,
) -> tuple[list[CampaignAdjustmentItem], dict, list[dict], list[dict]]:
    _core_set: set[str] = core_keyword_set or set()
    """单流全流程: summaries → unit_lookup → 预取 → (await overview_gate) → 分批 → R1 单轮 → 合并。

    返回 (adjustments, rounds_detail, enriched_summaries, skipped_campaigns)。
    skipped = 整批 LLM 失败或未返回 item 的活动（运营需人工补救；外层护栏循环会补答）。
    """
    if not campaigns:
        return [], {}, [], []

    async def _cancel() -> None:
        if cancel_check:
            await cancel_check()
    t0 = time.monotonic()
    _st = lambda label: logger.info("Stream timing [%s|%s] +%.1fs: %s", parent_asin, task_type, time.monotonic() - t0, label)

    # 1. 构建 summaries + unit_lookup
    summaries = [
        reasoner._campaign_to_prompt_dict(
            cu,
            target_acos=strategy_context.target_acos,
            keyword_class=keyword_class_map.get(cu.keyword_text, ""),
            is_core=normalize_core_keyword(cu.keyword_text) in _core_set,
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
    await _cancel()

    # LLM 轮前等策略总览 gate：保证 ctx_dict 已含 posture_brief（与上面的 prefetch 重叠跑，不串行）
    if overview_gate is not None:
        await overview_gate

    # 3. 分批 + R1 单轮分析（不再双轮投票）
    batches = _build_batches(summaries, batch_size, seed=1)
    r1_results = await _run_round(
        reasoner, parent_asin, batches, ctx_dict, temperature, sem, 1,
        task_type=task_type, cancel_check=cancel_check,
    )
    _st(f"DONE R1 ({len(batches)} batches)")
    await _cancel()

    adjustments: list[CampaignAdjustmentItem] = []
    for br in r1_results:
        for item in br.items:
            item.confidence = "medium"
            adjustments.append(item)
    skipped = _collect_skipped(campaigns, adjustments)
    if task_type == "exact":
        _backfill_placement_pcts(adjustments, unit_lookup)
    _st(f"DONE merge ({len(adjustments)} items, {len(skipped)} skipped)")
    return adjustments, {
        "round1": _round_stats(r1_results), "round2": None, "round3": None,
    }, summaries, skipped


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
    cancel_check: Callable[[], Awaitable[None]] | None = None,
) -> list[CampaignBatchResult]:
    """执行一轮 LLM 调用 (所有 batch 并发，Semaphore 由调用方注入)。"""

    async def _call_one(batch_idx: int, batch: list[dict]) -> CampaignBatchResult:
        if cancel_check:
            await cancel_check()
        # 纵深防御 1: per-stream 信号量获取超时 (防 TCP 半开 batch 占槽后其他 batch 在 sem 门前饿死)。
        # 全局 LLM 并发已由 client 层信号量接管，此处仅控单轮内公平限流。
        sem_timeout = settings.campaign_sem_acquire_timeout
        got_sem = False
        try:
            try:
                await asyncio.wait_for(sem.acquire(), timeout=sem_timeout)
                got_sem = True
            except asyncio.TimeoutError:
                return CampaignBatchResult(
                    batch_id=batch_idx, round_number=round_number,
                    llm_success=False, llm_error="sem_acquire_timeout(stream)",
                    temperature=temperature,
                )
            # 纵深防御 2: 单批 HTTP socket 超时 (httpx 层自断,绕过 asyncio 取消缺陷)
            try:
                result = await asyncio.wait_for(
                    reasoner.recommend_campaign_batch(
                        asin=asin,
                        campaign_summaries=batch,
                        strategy_context=strategy_context,
                        temperature=temperature,
                        task_type=task_type,
                        timeout_override=LLM_TIMEOUT,
                    ),
                    timeout=LLM_TIMEOUT,
                )
                if cancel_check:
                    await cancel_check()

                if not isinstance(result, dict):
                    raise ValueError(f"recommend_campaign_batch 返回非 dict: {type(result).__name__}")
                parsed = result.get("parsed") or {}
                raw_adj = parsed.get("campaign_adjustments", []) if isinstance(parsed, dict) else []
                items: list[CampaignAdjustmentItem] = []
                normalized_cnt = 0
                for adj in raw_adj:
                    try:
                        item = CampaignAdjustmentItem(**adj)
                        if _normalize_action(item):
                            normalized_cnt += 1
                        items.append(item)
                    except Exception as e:
                        logger.warning("Campaign adjustment item 解析失败 batch=%d: %s", batch_idx, e)
                if normalized_cnt:
                    logger.info(
                        "Batch %d [%s] action 归一: 改写 %d/%d 条",
                        batch_idx, asin, normalized_cnt, len(items),
                    )

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
        finally:
            if got_sem:
                sem.release()

    tasks = [asyncio.ensure_future(_call_one(i, batch)) for i, batch in enumerate(batches)]
    # 纵深防御 3: 单轮 gather 兜底超时
    round_timeout = LLM_TIMEOUT * 2 + 30
    try:
        raw_results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=round_timeout,
        )
    except asyncio.TimeoutError:
        not_done = sum(1 for t in tasks if not t.done())
        logger.warning(
            "Round %d gather 超时 [%s] >%ds: %d/%d 批未完成",
            round_number, asin, round_timeout, not_done, len(tasks),
        )
        raw_results = [
            t.result() if t.done() and not t.cancelled()
            else TimeoutError(f"round_timeout:{round_timeout}s")
            for t in tasks
        ]
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


# ── 统计与回填辅助 ───────────────────────────────────────────────────────────


def _round_stats(results: list[CampaignBatchResult]) -> dict:
    successful = sum(1 for r in results if r.llm_success)
    total_items = sum(len(r.items) for r in results)
    return {
        "batches": len(results),
        "successful_batches": successful,
        "failed_batches": len(results) - successful,
        "total_items": total_items,
    }


# KB 07 加价比例边界
# KB 07「最大加幅」= 单次广告位调整的【加幅绝对值上限】(百分点)，叠加在当前加价比例上；
# 不是加价比例的"值上限"。头部(TOS)≤30 / 其他(RoS)≤15 / 商品(PP)≤10；加价比例本身无硬上限。
_PLACEMENT_DELTA_CAP = {"头部": 30, "商品": 10, "其他": 15}

# action → 加幅(百分点)。小涨/小降 固定 ±10；大涨/大降 = ±当前广告位最大加幅(_PLACEMENT_DELTA_CAP)。
def _placement_step(action: str, cap: float) -> float:
    if action == "小涨":
        return min(10.0, cap)
    if action == "大涨":
        return cap
    if action == "小降":
        return -min(10.0, cap)
    if action == "大降":
        return -cap
    return 0.0  # 维持 / 未知

_PLACEMENT_NAME_MAP: dict[str, str] = {
    "头部": "头部", "Top of Search on-Amazon": "头部", "top_of_search": "头部",
    "商品": "商品", "Detail Page on-Amazon": "商品", "product_page": "商品",
    "其他": "其他", "Other on-Amazon": "其他", "rest_of_search": "其他",
}


def _backfill_placement_pcts(
    adjustments: list,
    unit_lookup: dict[str, "CampaignUnit"],
) -> None:
    """代码回填 placement 的 current_pct + proposed_pct，LLM 只负责 action/evidence。

    current_pct 从 CampaignUnit 取真实值；
    proposed_pct 按 action 步长 + KB 07 边界算。
    """
    for item in adjustments:
        cu = unit_lookup.get(item.campaign_key)
        if cu is None:
            continue
        pct_by_placement = {"头部": cu.tos_bid_pct, "商品": cu.pp_bid_pct, "其他": cu.ros_bid_pct}
        for p in item.placement_adjustments or []:
            pname_raw = str(p.get("placement", "") or "")
            pname = _PLACEMENT_NAME_MAP.get(pname_raw, pname_raw)
            current = pct_by_placement.get(pname, 0.0)
            p["current_pct"] = current

            action = str(p.get("action", "维持") or "维持")
            # KB 07 加幅: 小涨/小降=±10；大涨/大降=±最大加幅(头部30/其他15/商品10)。
            # 叠加在当前加价比例上，加价比例本身无值上限（仅 ≥0）。
            # 例: 头部 183% 大涨(+30)→213%；小涨(+10)→193%。
            cap = _PLACEMENT_DELTA_CAP.get(pname, 100)
            step = _placement_step(action, cap)
            proposed = max(0.0, current + step)
            p["proposed_pct"] = proposed


def _backfill_campaign_adjustment_context(
    adjustments: list[CampaignAdjustmentItem],
    unit_by_key: dict[str, CampaignUnit],
    keyword_class_map: dict[str, str],
    rank_evidence_line,
    *,
    core_keyword_set: set[str] | None = None,
) -> None:
    _core_set: set[str] = core_keyword_set or set()
    for item in adjustments:
        cu = unit_by_key.get(item.campaign_key)
        if cu is None:
            continue
        item.campaign_id = cu.campaign_id
        item.keyword_id = cu.keyword_id
        item.seller_sku = cu.seller_sku
        item.current_budget = cu.current_budget
        item.current_bid = cu.current_bid
        item.days_online = cu.days_online
        item.perf_7d = cu.perf_7d.model_dump() if cu.perf_7d else {}
        item.keyword_class = keyword_class_map.get(cu.keyword_text, "")
        item.is_core = normalize_core_keyword(cu.keyword_text) in _core_set
        item.natural_rank = cu.natural_rank
        item.near_natural_rank = cu.near_natural_rank
        item.rank_change = cu.rank_change
        item.portfolio_or_group = cu.current_portfolio_name
        if cu.match_type == "EXACT":
            line = rank_evidence_line(cu)
            if line and line not in item.evidence:
                item.evidence.append(line)


_BROAD_PORTFOLIO_MATCH_TYPES = frozenset({"BROAD", "PHRASE", "AUTO"})


def _reconcile_portfolio_targets(
    adjustments: list[CampaignAdjustmentItem],
    units: list[CampaignUnit],
    excluded: list[dict],
) -> None:
    """在落库前生成唯一可信的活动级挪组 target。

    广泛/词组/自动不依赖 LLM 输出：当前组合确认不属于自动广泛组才写 target。
    精准活动仅消费 Rule 32 等明确分组规则的结果；均须确认当前组合与目标组
    不一致才写 target。经营模式只能影响前序分析与护栏，不参与此处迁组授权。
    """
    units_by_id = {
        str(unit.campaign_id or "").strip(): unit
        for unit in units
        if str(unit.campaign_id or "").strip()
    }
    units_by_key = {unit.campaign_key: unit for unit in units if unit.campaign_key}

    adjusted_campaign_ids: set[str] = set()
    for item in adjustments:
        campaign_id = str(item.campaign_id or "").strip()
        unit = units_by_id.get(campaign_id) or units_by_key.get(item.campaign_key)
        if unit is None:
            # LLM / 旧分类都不能直接授权挪组；缺少当前真实组合时也不写 target。
            item.target_campaign_group_type = ""
            continue

        current_groups = find_portfolio_group_matches(unit.current_portfolio_name)
        match_type = (unit.match_type or item.match_type or "").upper()
        if match_type in _BROAD_PORTFOLIO_MATCH_TYPES:
            item.ai_portfolio_class = PORTFOLIO_BROAD
            item.target_campaign_group_type = target_group_code_if_current_mismatch(
                unit.current_portfolio_name,
                PORTFOLIO_BROAD,
            )
            unit.portfolio = PORTFOLIO_BROAD
        else:
            # 精准旧 $5 分类及 LLM 遗留字段都不能直接授权挪组；
            # 仅明确分组规则的结果才可写 pending target。
            is_exact_transition = str(item.triggered_rule or "").startswith(
                "EXACT_TRANSITION:"
            )
            if match_type == "EXACT" and is_exact_transition:
                item.target_campaign_group_type = target_group_code_if_current_mismatch(
                    unit.current_portfolio_name,
                    item.target_campaign_group_type,
                )
            else:
                item.target_campaign_group_type = ""
            # 当前真实归属仍可供展示/预算回算读取。
            item.ai_portfolio_class = current_groups[0] if current_groups else ""
            unit.portfolio = item.ai_portfolio_class

        if campaign_id:
            adjusted_campaign_ids.add(campaign_id)

    # 广泛活动可能没有 LLM 返回（包括多关键词预过滤活动）；每个 campaign_id 最多补一条纯挪组项。
    candidates: list[tuple[str, str, str, str, str, str, str, float | None, float | None, dict]] = []
    seen_candidates: set[str] = set()
    for unit in units:
        campaign_id = str(unit.campaign_id or "").strip()
        if not campaign_id or campaign_id in seen_candidates:
            continue
        seen_candidates.add(campaign_id)
        if (unit.match_type or "").upper() not in _BROAD_PORTFOLIO_MATCH_TYPES:
            continue
        candidates.append((
            campaign_id, unit.campaign_name, unit.campaign_key, unit.child_asin,
            unit.keyword_text, unit.match_type, unit.current_portfolio_name,
            unit.current_budget, unit.current_bid,
            unit.perf_7d.model_dump() if unit.perf_7d else {},
        ))
    for row in excluded:
        campaign_id = str(row.get("campaign_id") or "").strip()
        if not campaign_id or campaign_id in seen_candidates:
            continue
        seen_candidates.add(campaign_id)
        match_type = str(row.get("match_type") or "").upper()
        if match_type not in _BROAD_PORTFOLIO_MATCH_TYPES:
            continue
        candidates.append((
            campaign_id,
            str(row.get("campaign_name") or campaign_id),
            f"portfolio-reconcile:{campaign_id}",
            str(row.get("child_asin") or ""),
            str(row.get("keyword_text") or ""),
            match_type,
            str(row.get("current_portfolio_name") or ""),
            None,
            None,
            {},
        ))

    added = 0
    for (
        campaign_id, campaign_name, campaign_key, child_asin, keyword_text,
        match_type, current_portfolio_name, current_budget, current_bid, perf_7d,
    ) in candidates:
        if campaign_id in adjusted_campaign_ids:
            continue
        target = target_group_code_if_current_mismatch(
            current_portfolio_name,
            PORTFOLIO_BROAD,
        )
        if not target:
            continue
        adjustments.append(CampaignAdjustmentItem(
            campaign_name=campaign_name,
            campaign_key=campaign_key,
            campaign_id=campaign_id,
            child_asin=child_asin,
            keyword_text=keyword_text,
            match_type=match_type,
            action="keep",
            triggered_rule="BROAD_PORTFOLIO_RECONCILIATION",
            reason="当前广告组合不属于自动广泛组，按分组规则迁入自动广泛组",
            evidence=[f"当前广告组合：{current_portfolio_name}"],
            current_budget=current_budget,
            current_bid=current_bid,
            ai_portfolio_class=PORTFOLIO_BROAD,
            target_campaign_group_type=target,
            portfolio_or_group=current_portfolio_name,
            perf_7d=perf_7d,
        ))
        added += 1

    if added:
        logger.info("Campaign portfolio reconciliation: added %d broad move-only items", added)


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

    # 优先用主 fetch 已解析并缓存的店铺(含 URL override)，避免二次 dwd_shop 反查
    shop_account = getattr(fetcher, "_last_shop_account", "") or ""
    shop_id = getattr(fetcher, "_last_shop_id", 0) or 0
    site_code = getattr(fetcher, "_last_site_code", "") or ""  # 主 fetch 已缓存该 ASIN 站点
    if not shop_account:
        from app.config.settings import settings as _ss
        from app.data.mcp_db_context import resolve_mcp_context_from_mcp
        if getattr(_ss, "mcp_resolve_context", False):
            try:
                from app.data.mcp_adapter import McpAdapter
                ctx = await asyncio.wait_for(
                    resolve_mcp_context_from_mcp(parent_asin, McpAdapter()),
                    timeout=getattr(_ss, "mcp_context_timeout", 30.0),
                )
                shop_account = (ctx.shop_account if ctx else "") or ""
                shop_id = (ctx.shop_id if ctx else 0) or 0
                site_code = (ctx.site_code if ctx else "") or site_code
            except Exception:
                pass

    if not shop_account:
        logger.warning("placement 预取跳过 [%s]: shop_account 为空", parent_asin)
        return enriched

    sd, ed = _make_date_window(days, site_code)
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

    # 优先用主 fetch 已解析并缓存的店铺(含 URL override)，避免二次 dwd_shop 反查
    shop_account = getattr(fetcher, "_last_shop_account", "") or ""
    site_code = getattr(fetcher, "_last_site_code", "") or ""  # 主 fetch 已缓存该 ASIN 站点
    if not shop_account:
        from app.config.settings import settings as _ss
        from app.data.mcp_db_context import resolve_mcp_context_from_mcp
        if getattr(_ss, "mcp_resolve_context", False):
            try:
                from app.data.mcp_adapter import McpAdapter
                ctx = await asyncio.wait_for(
                    resolve_mcp_context_from_mcp(parent_asin, McpAdapter()),
                    timeout=getattr(_ss, "mcp_context_timeout", 30.0),
                )
                shop_account = (ctx.shop_account if ctx else "") or ""
                site_code = (ctx.site_code if ctx else "") or site_code
            except Exception:
                pass

    if not shop_account:
        logger.warning("search_terms 预取跳过 [%s]: shop_account 为空", parent_asin)
        return enriched

    sd, ed = _make_date_window(days, site_code)
    result: dict = {}                          # 显式初始化：异常路径下 logger 也要能安全取长度
    try:
        result = await asyncio.wait_for(
            fetcher.fetch_search_terms_for(list(search_term_names), shop_account,
                                           start_date=sd, end_date=ed),
            timeout=getattr(settings, "campaign_st_fetch_timeout", 180),
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


def _make_date_window(days: int, site_code: str = "") -> tuple[str, str]:
    # 统一走 mcp_mapping（按站点当地时间、end=当地今天-1）；勿再用 date.today() 本地时区老口径
    from app.data.mcp_mapping import make_date_window
    return make_date_window(days, site_code)


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
    - 过滤：只取 confidence=='low' 的 adjustments（原投票分歧最需复核）。
      ⚠ 2026-07-31 切除双轮投票后所有项统一 medium，low_conf 恒空 → 当前为 no-op，
      待后续单独重构筛选对象（如改为被护栏拦截过的项）。
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


def _normalize_action(item: CampaignAdjustmentItem) -> bool:
    """根据 proposed vs current 的实际差异 derive 出权威 action。

    设计原则:
      - action 不应该由 LLM 决定 (LLM 经常 action=keep 但又填了不同 proposed,
        或者 action=adjust_bid 但 proposed_bid==current_bid)
      - eliminate_to_low_bid_pool 是业务语义特殊保留 LLM 决定 (淘汰 ≠ 简单调到 $1)
      - 其他全部代码 derive: budget 变 → adjust_budget; bid 变 → adjust_bid;
        placement 非空 → adjust_placement; 全没变 → keep
      - 多维同时变: 按业务优先级 budget > bid > placement 选 1 个 (单 action 字段限制)
      - keep 时强制 proposed=current,清 direction/placement；否词是独立叠加建议，保留

    Returns:
        bool: True 表示 action 被改写过 (用于日志统计)
    """
    # 淘汰由 LLM 决定,代码不干预 (淘汰组业务语义)
    if item.action == "eliminate_to_low_bid_pool":
        return False

    _EPS = 0.001
    bid_changed = (
        item.proposed_bid is not None and item.current_bid is not None
        and abs(item.proposed_bid - item.current_bid) > _EPS
    )
    budget_changed = (
        item.proposed_budget is not None and item.current_budget is not None
        and abs(item.proposed_budget - item.current_budget) > _EPS
    )
    # 全维持的广告位也会被 _backfill_placement_pcts 填成非空条目(proposed_pct==current_pct)，
    # 故不能用列表非空判变，须看任一广告位 proposed_pct≠current_pct 才算变(否则健康活动恒被判 adjust_placement)。
    placement_changed = any(
        p.get("proposed_pct") is not None and p.get("current_pct") is not None
        and abs(float(p["proposed_pct"]) - float(p["current_pct"])) > _EPS
        for p in (item.placement_adjustments or [])
    )
    # negative_keywords 是叠加建议,不算 action 变更 (KB 22 §3.3)

    if budget_changed:
        derived = "adjust_budget"
    elif bid_changed:
        derived = "adjust_bid"
    elif placement_changed:
        derived = "adjust_placement"
    else:
        derived = "keep"

    changed = (item.action != derived)
    item.action = derived

    # keep 语义清理: proposed 对齐 current, 清空 direction/placement。
    # 否词不参与 action 推导，是独立叠加建议，不能因数值维度维持而丢失。
    if derived == "keep":
        item.proposed_bid = item.current_bid
        item.proposed_budget = item.current_budget
        item.direction = {}
        if item.placement_adjustments:
            item.placement_adjustments = []

    return changed


# ── 护栏重试编排 ──────────────────────────────────────────────────────────


_GUARDRAIL_RETRY_INSTRUCTION = """## 内部护栏反馈
以下「⚠️ 护栏告警」仅用于本轮复判，不要写入 reason/evidence，不要向运营解释护栏或规则编号。

护栏不是要求一律 keep。请只修正被指出的违规部分，仍需根据活动事实做该做的调整：
该淘汰就淘汰，该小调就小调，该限制幅度就限制幅度，该修正调整方向就修正。
未被告警指出的调整维度，不要因为有护栏反馈而自动改成维持。
"""


def _build_guardrail_alerts(
    guardrail_pass,
) -> dict[str, str]:
    """护栏拦截结果 → {campaign_key: 告警文本}，用于注入 R3/R4 prompt。"""
    grouped: dict[str, list[str]] = {}
    for r in guardrail_pass.results:
        if not r.corrected:
            continue
        key = getattr(r, "campaign_key", "") or ""
        if not key:
            continue
        instruction = (getattr(r, "retry_instruction", "") or "").strip()
        if not instruction:
            continue
        grouped.setdefault(key, []).append(instruction)
    return {key: "\n".join(messages) for key, messages in grouped.items()}


def _inject_alerts_to_summaries(
    campaign_summaries: list[dict], alerts: dict[str, str],
) -> list[dict]:
    """为被拦截的活动注入护栏告警行。"""
    for s in campaign_summaries:
        ckey = s.get("campaign_key", "")
        if ckey in alerts:
            s["_guardrail_alert"] = alerts[ckey]
    return campaign_summaries


def _guardrail_rule_counts(guardrail_pass) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in guardrail_pass.results:
        if not getattr(r, "corrected", False):
            continue
        rule_id = getattr(r, "rule_id", "") or "UNKNOWN"
        counts[rule_id] = counts.get(rule_id, 0) + 1
    return counts


def _transition_text(current, proposed) -> str:
    def _fmt(value) -> str:
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)
    return f"{_fmt(current)}->{_fmt(proposed)}"


def _guardrail_snapshot(
    adjustments: list[CampaignAdjustmentItem],
    keys: set[str],
) -> list[dict]:
    wanted = set(keys)
    out: list[dict] = []
    for item in adjustments:
        if item.campaign_key not in wanted:
            continue
        out.append({
            "key": item.campaign_key,
            "name": item.campaign_name,
            "action": item.action,
            "bid": _transition_text(item.current_bid, item.proposed_bid),
            "budget": _transition_text(item.current_budget, item.proposed_budget),
        })
    return out


def _guardrail_replacement_summary(
    old_items: list[CampaignAdjustmentItem],
    replacement_by_key: dict[str, CampaignAdjustmentItem],
) -> list[dict]:
    out: list[dict] = []
    for old in old_items:
        new = replacement_by_key.get(old.campaign_key)
        if new is None:
            continue
        out.append({
            "key": old.campaign_key,
            "name": old.campaign_name,
            "action": f"{old.action}->{new.action}",
            "bid": f"{_transition_text(old.current_bid, old.proposed_bid)} to "
                   f"{_transition_text(new.current_bid, new.proposed_bid)}",
            "budget": f"{_transition_text(old.current_budget, old.proposed_budget)} to "
                      f"{_transition_text(new.current_budget, new.proposed_budget)}",
        })
    return out


# ── 预算冲突裁决 ────────────────────────────────────────────────────────────


def _apply_campaign_guardrails(
    adjustments: list[CampaignAdjustmentItem],
    total_budget_limit: float | None = None,
    *,
    product_stage: str = "",
    inventory_days: float | None = None,
    refund_rate: float | None = None,
    rating: float | None = None,
):
    from app.workflow.steps.campaign_guardrails import GuardrailResult
    from app.workflow.steps.campaign_guardrails import apply_all as _apply_guardrails

    gp = _apply_guardrails(
        adjustments,
        product_stage=product_stage,
        inventory_days=inventory_days,
        refund_rate=refund_rate,
        rating=rating,
    )
    warnings: list[str] = [r.message for r in gp.results if r.corrected]
    if gp.corrections > 0:
        logger.info("Guardrails: 护栏修正 %d 条 (共 %d 条)", gp.corrections, len(adjustments))

    # Bid 绝对上限兜底。P9 已覆盖常规路径；这里保留旧接口的最终防线。
    for adj in adjustments:
        if adj.proposed_bid is not None and adj.proposed_bid > 3.0:
            original = adj.proposed_bid
            adj.proposed_bid = 3.0
            message = f"[{adj.campaign_name}] Bid ${original} 超过上限 $3.00，已截断"
            warnings.append(message)
            gp.add(GuardrailResult(
                rule_id="FINAL_BID_CAP",
                corrected=True,
                campaign_key=getattr(adj, "campaign_key", ""),
                message=message,
                retry_instruction=(
                    f"[{adj.campaign_name}] bid 不得超过 $3.00。可在上限内重新给值，"
                    "或按活动事实选择维持/其他调整。"
                ),
            ))

    # 终态 action 归一: proposed 可能被改回 current,
    # 这种情况 action 从 adjust_X 变 keep。淘汰组 action 不变。
    re_normalized = 0
    for adj in adjustments:
        if adj.action == "eliminate_to_low_bid_pool":
            continue
        if _normalize_action(adj):
            re_normalized += 1
    if re_normalized:
        logger.info("_resolve_budget_conflicts: 终态 action 二次归一改写 %d 条", re_normalized)

    # 总预算上限检查 (如果有外部输入)
    if total_budget_limit is not None:
        total_proposed = sum((a.proposed_budget or 0) for a in adjustments)
        if total_proposed > total_budget_limit:
            overflow = total_proposed - total_budget_limit
            message = (
                f"总预算 ${total_proposed:.2f} 超出上限 ${total_budget_limit:.2f}，"
                f"溢出 ${overflow:.2f}"
            )
            warnings.append(message)
            for adj in reversed(adjustments):
                if overflow <= 0:
                    break
                if adj.proposed_budget and adj.proposed_budget > 1:
                    cut = min(overflow, adj.proposed_budget - 1)
                    adj.proposed_budget -= cut
                    overflow -= cut
                    gp.add(GuardrailResult(
                        rule_id="FINAL_TOTAL_BUDGET_CAP",
                        corrected=True,
                        campaign_key=getattr(adj, "campaign_key", ""),
                        message=message,
                        retry_instruction=(
                            f"[{adj.campaign_name}] 总预算已超过上限，请在总预算约束内重新评估预算值。"
                            "可优先压缩边际较弱活动，也可按事实维持或下调。"
                        ),
                    ))

    return gp, warnings


def _resolve_budget_conflicts(
    adjustments: list[CampaignAdjustmentItem],
    total_budget_limit: float | None = None,
    *,
    product_stage: str = "",
    inventory_days: float | None = None,
    refund_rate: float | None = None,
    rating: float | None = None,
) -> list[str]:
    """护栏后处理：调用 campaign_guardrails.apply_all() 执行全部确定性规则，
    然后做终态 action 归一 + 总预算上限截断。
    """
    _, warnings = _apply_campaign_guardrails(
        adjustments,
        total_budget_limit,
        product_stage=product_stage,
        inventory_days=inventory_days,
        refund_rate=refund_rate,
        rating=rating,
    )
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
        # 注入运营文本(reason) + 实际执行数值(current→proposed) + 广告位动作，
        # 供 LLM 比对"文本陈述 vs 实际数值决策"是否一致（不只校 action 合理性）
        plc_txt = "; ".join(
            f"{x.get('placement', '')}:{x.get('action', '')}"
            for x in (adj.placement_adjustments or [])
        ) or "无"
        facts_parts.append(
            f"  - {adj.campaign_name}: action={adj.action}, "
            f"triggered_rule={adj.triggered_rule}, "
            f"7d花费=${p.get('cost', 0)}, 7d订单={p.get('orders', 0)}, "
            f"7d CVR={p.get('cvr', 'N/A')}%, "
            f"match_type={adj.match_type}, keyword_class={adj.keyword_class}, "
            f"is_core={adj.is_core}\n"
            f"    实际执行数值: Bid {adj.current_bid}→{adj.proposed_bid}, "
            f"Budget {adj.current_budget}→{adj.proposed_budget}, 广告位: {plc_txt}\n"
            f"    运营文本(reason): {adj.reason}"
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
