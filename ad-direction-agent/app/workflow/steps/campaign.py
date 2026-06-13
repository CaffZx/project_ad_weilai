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
from app.persistence.redis_client import acquire_lock, get_redis, release_lock
from app.workflow.steps.campaign_portfolio import (
    PORTFOLIO_ELIMINATE,
    classify as _classify_portfolio,
)
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
_SANITY_CHECK_ENABLED = True
_SYNTHESIS_ENABLED = True   # 恢复(2026-06-08)：SelectorEventLoopPolicy 根治 asyncio 取消；synthesis 用 timeout_override=55(httpx 自断) + try/except fail-open

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
    # 注意：本部署内 parent_asin → 单店铺（resolve_mcp_context_from_db 解析）。
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
) -> CampaignAnalysisResult:
    """完整 LLM 分析：拉数据 → 分批 → R1+R2 → 投票 → (R3) → sanity_check。

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
                    fetcher.fetch_campaigns(parent_asin, days=days, override=erp_override),
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
                "reason": "已入淘汰池（预算≈$1，出价≈$0.2），请到ERP手动修改",
                "__prefiltered": True,   # 前端按此渲染为灰色不可操作的预过滤卡（无悬停警告）
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
            shop_id=campaign_data.shop_id,
            parent_seller_sku=campaign_data.parent_seller_sku,
            site_code=campaign_data.site_code,
            total_campaigns=0,
            skipped_campaigns=skipped_eliminated + (campaign_data.excluded or []),
            warnings=["所有活动均在预过滤阶段被排除（疑似全部已淘汰）"],
            rounds_detail={},
        )

    # 4. 按 match_type 分流
    exact_list = [cu for cu in llm_campaigns if cu.match_type == "EXACT"]
    broad_list = [cu for cu in llm_campaigns if cu.match_type != "EXACT"]
    _t(f"split: exact={len(exact_list)} broad={len(broad_list)}")

    # 4.1 组合预分类 (主推/广泛自动/测试新增,无 llm_action 时淘汰组用"已在池中"判定)
    #     分析前先填,分析后再用 llm_action 在 adjustments 上补一遍。
    if settings.campaign_portfolio_enabled:
        for cu in llm_campaigns:
            cu.portfolio = _classify_portfolio(cu)
        # skipped_eliminated 也归入淘汰组(用于汇总展示一致)
        for s in skipped_eliminated:
            s["portfolio"] = PORTFOLIO_ELIMINATE

    ctx_dict = strategy_context.model_dump()
    exact_sem = asyncio.Semaphore(cc)   # 每流并发上限 = campaign_llm_concurrency
    broad_sem = asyncio.Semaphore(cc)
    new_sem = asyncio.Semaphore(cc)     # 新增活动线独立限流（与 exact/broad 对等）
    rounds_detail: dict[str, dict] = {}
    warnings_list: list[str] = []

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
    # 新增线输入（并行启动前一次性算好）：
    existing_kws = {
        (cu.keyword_text or "").strip().lower()
        for cu in campaign_data.campaigns if cu.keyword_text
    }
    # 用预过滤阶段疑似已淘汰活动数作 FILL_AFTER_ELIMINATION 触发输入
    # （并行架构下精准/广泛 adjustments 尚未产出；语义=已存在淘汰活动→词池已变窄）
    pre_eliminated_count = len(skipped_eliminated)
    shop_account = getattr(fetcher, "_last_shop_account", "") or ""
    if not shop_account:
        # 与 placement/search_term 懒加载（本文件 ~1428/~1477 行）一致的回落：
        # _last_shop_account 为空时从 DB 解析。candidate 发现的 flow_keywords/
        # own_keyword_flow 把 shop_account 列为必填，缺则 build_tool_args 丢弃该参数
        # → MCP 查不到 → new_campaigns 恒空。此处补齐，杜绝"店铺未缓存即无新增活动"。
        from app.data.mcp_db_context import resolve_mcp_context_from_db
        try:
            _shop_ctx = await asyncio.wait_for(resolve_mcp_context_from_db(parent_asin), timeout=15)
            shop_account = (_shop_ctx.shop_account if _shop_ctx else "") or ""
        except Exception:
            shop_account = ""
    # 新增活动投放目标子 ASIN：历史活动数最多/花费最高的子 ASIN（非父 ASIN 占位）
    target_child_asin = _pick_target_child_asin(campaign_data.campaigns)

    async def _no_op_new_campaigns():
        return [], []

    stream_results = await asyncio.gather(
        _analyze_one_stream(
            exact_list, "exact", reasoner, fetcher, parent_asin, days,
            strategy_context, keyword_class_map, bs, exact_sem, temperature, ctx_dict,
            overview_gate=overview_gate,
        ),
        _analyze_one_stream(
            broad_list, "broad", reasoner, fetcher, parent_asin, days,
            strategy_context, keyword_class_map, bs, broad_sem, temperature, ctx_dict,
            overview_gate=overview_gate,
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
        ) if settings.campaign_new_enabled else _no_op_new_campaigns()),
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
        if isinstance(r, BaseException):
            logger.exception("Stream %s 异常 [%s]: %s", label, parent_asin, r)
            warnings_list.append(f"{label} 流分析异常: {type(r).__name__}: {r}")
            return [], {}, [], []
        return r

    (exact_adjustments, exact_rd, exact_summaries, exact_skipped) = _unpack_stream(stream_results[0], "exact")
    (broad_adjustments, broad_rd, broad_summaries, broad_skipped) = _unpack_stream(stream_results[1], "broad")

    # 新增活动线解包（返回 tuple[list[NewCampaignItem], list[str]]）
    new_campaigns: list = []
    new_campaigns_warnings: list[str] = []
    nc_result = stream_results[2]
    if isinstance(nc_result, BaseException):
        logger.exception("new_campaigns 流异常 [%s]: %s", parent_asin, nc_result)
        warnings_list.append(f"新增活动分析异常: {type(nc_result).__name__}: {nc_result}")
    else:
        new_campaigns, new_campaigns_warnings = nc_result
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

    for item in adjustments:
        cu = unit_by_key.get(item.campaign_key)
        if cu is None:
            continue
        item.campaign_id = cu.campaign_id
        item.keyword_id = cu.keyword_id
        item.seller_sku = cu.seller_sku
        # current_* 以代码可信源（CampaignUnit）为准：current_budget 来自 MCP
        # ad_campaign_basic_info、current_bid 来自 Doris 上下文，均为后台真值。
        # LLM 在 JSON 回填的 current_* 可能抄错，不予采信（proposed_* 仍用 LLM 产出）。
        # 修正后，_resolve_budget_conflicts 末尾的终态 _normalize_action 会据真值重派生 action。
        item.current_budget = cu.current_budget
        item.current_bid = cu.current_bid
        # 自然排名回填 + 证据行（仅精准；evidence 经 card.evidence 落库，快照轨零改可见）
        item.keyword_class = keyword_class_map.get(cu.keyword_text, "")
        item.natural_rank = cu.natural_rank
        item.near_natural_rank = cu.near_natural_rank
        item.rank_change = cu.rank_change
        if cu.match_type == "EXACT":
            _ln = _rank_evidence_line(cu)
            if _ln and _ln not in item.evidence:
                item.evidence.append(_ln)
        if settings.campaign_portfolio_enabled:
            item.ai_portfolio_class = _classify_portfolio(cu, llm_action=item.action)
            cu.portfolio = item.ai_portfolio_class
        # portfolio_or_group 维持空(KB 18/21 原字段,数据层未拉,留空待后续)

    # 7. 预算冲突裁决
    budget_warnings = _resolve_budget_conflicts(adjustments)
    warnings_list.extend(budget_warnings)

    # 8 + 8b：Sanity check 与 AI 汇总合成【并行】。
    # 两者都只读已定稿的 adjustments，产出独立（warnings vs 分组叙事），无数据依赖 →
    # gather 省墙钟（约 20-55s）。各自吞异常 + 返回自身 warnings，避免并发改 warnings_list。
    async def _run_sanity() -> tuple[list[str], bool]:
        # sanity_ok 默认 False：未运行/有批次失败都按"未通过"展示，仅全批次成功才 True
        if not _SANITY_CHECK_ENABLED:
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
        if not (_SYNTHESIS_ENABLED and adjustments):
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

    sanity_res, synth_res = await asyncio.gather(
        _run_sanity(), _run_synth(), return_exceptions=True,
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
        "confidence_high": sum(1 for a in adjustments if a.confidence == "high"),
        "confidence_medium": sum(1 for a in adjustments if a.confidence == "medium"),
        "confidence_low": sum(1 for a in adjustments if a.confidence == "low"),
        "estimated_budget_impact": round(sum(
            (a.proposed_budget or 0) - (a.current_budget or 0)
            for a in adjustments
        ), 2),
    }

    # 10. 预算汇总 (极简版: 只返 target_budget + source,前端按勾选动态算"已勾选总和")
    budget_summary: dict | None = None
    if settings.campaign_portfolio_enabled:
        try:
            from app.workflow.steps.campaign_budget_summary import build_summary
            budget_summary = build_summary(
                adjustments=adjustments,
                all_units=llm_campaigns,
                ctx=strategy_context,
            )
            _t("DONE budget_summary")
        except Exception as e:
            logger.exception("budget_summary 构建失败 [%s]: %s", parent_asin, e)
            warnings_list.append(f"预算汇总构建失败: {type(e).__name__}: {e}")
            budget_summary = None

    _t("DONE total")
    return CampaignAnalysisResult(
        parent_asin=parent_asin, days=days, run_id=run_id,
        shop_id=campaign_data.shop_id,
        parent_seller_sku=campaign_data.parent_seller_sku,
        site_code=campaign_data.site_code,
        total_campaigns=len(llm_campaigns),
        adjustments=adjustments,
        new_campaigns=new_campaigns,
        new_campaigns_warnings=new_campaigns_warnings,
        skipped_campaigns=skipped_campaigns,
        strategic_overview=strategic_overview,
        synthesis=synthesis,
        budget_summary=budget_summary,
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
        ad_directions=ad_directions or [],
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
) -> tuple[list[CampaignAdjustmentItem], dict, list[dict], list[dict]]:
    """单流全流程: summaries → unit_lookup → 预取 → (await overview_gate) → 分批 → R1+R2 → 投票 → (R3) → 合并。

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

    # LLM 轮前等策略总览 gate：保证 ctx_dict 已含 posture_brief（与上面的 prefetch 重叠跑，不串行）
    if overview_gate is not None:
        await overview_gate

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
        if task_type == "exact":
            _backfill_placement_pcts(adjustments, unit_lookup)
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
    if task_type == "exact":
        _backfill_placement_pcts(adjustments, unit_lookup)
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


def _merge_negative_keywords(
    a: CampaignAdjustmentItem,
    b: CampaignAdjustmentItem,
) -> list[dict]:
    """合并两轮否词：两轮都命中=推荐(vote=recommended)，仅单轮命中=可选(vote=optional)。

    否词是叠加型建议，不作为投票分歧判据；两轮并集全部保留，只用 vote 标注可信度。
    """
    def _index(items: list[dict]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for n in items or []:
            if isinstance(n, dict):
                kw = str(n.get("keyword", "")).strip().lower()
                if kw:
                    out[kw] = n
        return out

    ma, mb = _index(a.negative_keywords), _index(b.negative_keywords)
    merged: list[dict] = []
    for kw in ma.keys() | mb.keys():
        base = dict(ma.get(kw) or mb.get(kw) or {})
        base["vote"] = "recommended" if (kw in ma and kw in mb) else "optional"
        merged.append(base)
    merged.sort(key=lambda n: 0 if n.get("vote") == "recommended" else 1)  # 推荐排前
    return merged


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
        # 广泛流：否词是叠加型建议，不作为分歧判据；两轮否词在 _conservative 里合并为 推荐/可选
        return True

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
                # action / bid·budget 方向分歧 → tiebreaker
                # 保留 R1 全部富字段（否词/广告位/理由/建议值），不再用骨架 dict 丢字段
                needs_tiebreaker.add(key)
                v = r1.model_dump()
                v["campaign_key"] = key
                v["confidence"] = "low"
                v["round_votes"] = {
                    "round1": f"{r1.action}|{r1.direction}",
                    "round2": f"{r2.action}|{r2.direction}",
                }
                merged_neg = _merge_negative_keywords(r1, r2)
                if merged_neg:
                    v["negative_keywords"] = merged_neg
                v["_r1"] = r1
                v["_r2"] = r2
                votes[key] = v
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
    # 广泛流：合并两轮否词为 推荐(两轮一致)/可选(单轮)，不丢任一轮建议
    merged_neg = _merge_negative_keywords(a, b)
    if merged_neg:
        chosen["negative_keywords"] = merged_neg
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

    # 优先用主 fetch 已解析并缓存的店铺(含 URL override)，避免二次 dwd_shop 反查
    shop_account = getattr(fetcher, "_last_shop_account", "") or ""
    shop_id = getattr(fetcher, "_last_shop_id", 0) or 0
    if not shop_account:
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

    # 优先用主 fetch 已解析并缓存的店铺(含 URL override)，避免二次 dwd_shop 反查
    shop_account = getattr(fetcher, "_last_shop_account", "") or ""
    if not shop_account:
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


def _normalize_action(item: CampaignAdjustmentItem) -> bool:
    """根据 proposed vs current 的实际差异 derive 出权威 action。

    设计原则:
      - action 不应该由 LLM 决定 (LLM 经常 action=keep 但又填了不同 proposed,
        或者 action=adjust_bid 但 proposed_bid==current_bid)
      - eliminate_to_low_bid_pool 是业务语义特殊保留 LLM 决定 (淘汰 ≠ 简单调到 $1)
      - 其他全部代码 derive: budget 变 → adjust_budget; bid 变 → adjust_bid;
        placement 非空 → adjust_placement; 全没变 → keep
      - 多维同时变: 按业务优先级 budget > bid > placement 选 1 个 (单 action 字段限制)
      - keep 时强制 proposed=current,清 direction/placement/neg_kw (keep 就是 keep)

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
    placement_changed = bool(item.placement_adjustments)
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

    # keep 语义清理: proposed 对齐 current, 清空 direction/placement/neg_kw
    if derived == "keep":
        item.proposed_bid = item.current_bid
        item.proposed_budget = item.current_budget
        item.direction = {}
        if item.placement_adjustments:
            item.placement_adjustments = []
        if item.negative_keywords:
            item.negative_keywords = []

    return changed


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

    # 终态 action 归一: _conservative 可能把 proposed 改回 current,
    # 这种情况 action 从 adjust_X 变 keep。淘汰组 action 不变 (上面 if 已 continue)。
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
