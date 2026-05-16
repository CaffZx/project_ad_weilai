"""工作流编排器 — 协调四层递进流程

流程: ASIN → 战略(人工) → 策略(AI推荐+人工) → 诊断(只读) → 执行(AI推荐+人工) → 校验+报告

职责:
1. 协调 DataAggregator / Recommender / LLMReasoner / StateManager / ValidationEngine
2. 管理各层之间的数据流转和状态推进
3. 处理 LLM 调用失败的降级逻辑
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from app.config.settings import settings
from app.core.data_aggregator import DataAggregator
from app.core.query_router import QueryRouter
from app.core.recommender import Recommender, TargetAcosRecommender, BudgetBidRecommender
from app.core.validation_engine import ValidationEngine
from app.core.decision_package import DecisionPackageGenerator
from app.core.scenario_analyzer import detect_scenario, build_metric_board
from app.llm.reasoner import LLMReasoner
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
from app.persistence.state_manager import StateManager

logger = logging.getLogger(__name__)

# ── 缓存条目 ──────────────────────────────────────────
CACHE_TTL = 14400  # 缓存 TTL：4 小时
FETCH_TIMEOUT = 35  # DB 查询超时（略大于 read_timeout=30）
LLM_TIMEOUT = 60    # LLM 调用超时


@dataclass
class CacheEntry:
    """缓存条目（数据 + 时间戳）"""
    data: "ASINData"
    timestamp: float


def _is_valid_data(data: "ASINData") -> bool:
    """防御：查询结果空值超过 3 个 → 不合法，不得缓存"""
    nulls = 0
    if not data.ad_data or data.ad_data.acos is None or data.ad_data.acos == 0:
        nulls += 1
    if not data.ad_data or data.ad_data.cvr is None or data.ad_data.cvr == 0:
        nulls += 1
    if data.margin is None:
        nulls += 1
    if not data.keywords:
        nulls += 1
    if not data.trend:
        nulls += 1
    if not data.signals or data.signals.inventory_qty is None:
        nulls += 1
    return nulls <= 3


class WorkflowOrchestrator:
    """四层工作流编排器"""

    def __init__(
        self,
        aggregator: DataAggregator | None = None,
        recommender: Recommender | None = None,
        reasoner: LLMReasoner | None = None,
        state_manager: StateManager | None = None,
        validation_engine: ValidationEngine | None = None,
        decision_generator: DecisionPackageGenerator | None = None,
    ):
        self.aggregator = aggregator or DataAggregator()
        self.recommender = Recommender()
        self.reasoner = reasoner or LLMReasoner()
        self.state = state_manager or StateManager()
        self.validator = validation_engine or ValidationEngine()
        self.decision_gen = decision_generator or DecisionPackageGenerator()
        # 数据缓存（TTL 4小时，支持强制刷新）
        self._cache_lock = asyncio.Lock()
        self._data_cache: dict[str, CacheEntry] = {}
        self._data_tasks: dict[str, asyncio.Task] = {}

    # ── 数据缓存机制 ───────────────────────────────────────

    async def _preload_data(self, asin: str):
        """后台预加载 ASIN 数据，不阻塞当前请求"""
        async with self._cache_lock:
            if asin in self._data_cache:
                entry = self._data_cache[asin]
                if time.time() - entry.timestamp < CACHE_TTL:
                    return
            if asin in self._data_tasks:
                return
        task = asyncio.create_task(self._do_preload(asin))
        self._data_tasks[asin] = task

    async def _do_preload(self, asin: str):
        try:
            data = await self.aggregator.fetch(asin)
            self._data_cache[asin] = CacheEntry(data=data, timestamp=time.time())
        except Exception as e:
            logger.error("后台数据预加载失败 [%s]: %s", asin, e)
        finally:
            self._data_tasks.pop(asin, None)

    async def _ensure_data(self, asin: str, refresh: bool = False,
                            meta_filter: list[str] | None = None) -> ASINData:
        """获取 ASIN 数据

        meta_filter 非空时跳过缓存直接按需查询。
        refresh=True 时跳过缓存，强制从数据库重新查询。
        正常模式下检查 TTL，缓存未过期则直接返回。
        """
        # 0) 选择性查询：跳过缓存，只查指定维度
        if meta_filter:
            return await self.aggregator.fetch(asin, meta_filter=meta_filter)

        # 1) 强制刷新：清除旧缓存 → 重新查 DB → 写入新缓存
        if refresh:
            async with self._cache_lock:
                if asin in self._data_tasks:
                    self._data_tasks[asin].cancel()
                    del self._data_tasks[asin]
                self._data_cache.pop(asin, None)
            logger.info("诊断刷新: 缓存已清除, 重新查询 %s", asin)
            data = await self.aggregator.fetch(asin)
            async with self._cache_lock:
                self._data_cache[asin] = CacheEntry(data=data, timestamp=time.time())
            return data

        # 2) TTL 检查
        async with self._cache_lock:
            if asin in self._data_cache:
                entry = self._data_cache[asin]
                if time.time() - entry.timestamp < CACHE_TTL:
                    return entry.data
                logger.info("Cache expired for %s (%.0fs old)", asin, time.time() - entry.timestamp)

        # 3) 等待进行中的预加载
        task = None
        async with self._cache_lock:
            if asin in self._data_tasks:
                task = self._data_tasks[asin]
        if task:
            try:
                await task
            except Exception:
                pass
            async with self._cache_lock:
                if asin in self._data_cache:
                    return self._data_cache[asin].data

        # 4) 缓存未命中或过期：直接查 → 写入
        data = await self.aggregator.fetch(asin)
        async with self._cache_lock:
            self._data_cache[asin] = CacheEntry(data=data, timestamp=time.time())
        return data

    # ── Layer 1.1 战略层 ─────────────────────────────────

    async def get_strategy_options(self, asin: str) -> StrategyOptionsResponse:
        """返回三维度选项（静态配置，无 DB 查询）

        数据预加载在后台触发，不阻塞用户操作。
        """
        # 后台预加载数据（用户选战略时数据已在加载）
        await self._preload_data(asin)

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

        current = self.state.get_long_term_config(asin)

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
        )

    async def confirm_strategy(self, req: StrategyConfirmRequest) -> StrategyConfirmResponse:
        """保存战略层选择并持久化"""
        # 校验 ASIN 是否存在，防止无效 ASIN 创建垃圾 config 目录
        data = await self._ensure_data(req.asin)
        if data.data_missing:
            return StrategyConfirmResponse(
                asin=req.asin,
                accepted=False,
                config_saved=False,
            )

        config = {
            "product_level": req.product_level,
            "product_stage": req.product_stage,
            "season_stage": req.season_stage,
        }
        self.state.set_long_term_config(req.asin, config)
        self.state.advance_layer(req.asin, "tactics")

        return StrategyConfirmResponse(
            asin=req.asin,
            accepted=True,
            config_saved=True,
        )

    # ── Layer 1.2 策略层 ─────────────────────────────────

    async def get_tactics_options(self, asin: str) -> TacticsOptionsResponse:
        """返回策略选项

        首次访问（无 keyword_analysis 缓存）：调用 purpose-agent LLM，产出推荐 + 关键词分类
        回访（keyword_analysis 已缓存）：跳过 LLM，从缓存读，仅刷新 DB 排名字段
        """
        long_term = self.state.get_long_term_config(asin)
        strategy_saved = all(k in long_term for k in ("product_level", "product_stage", "season_stage"))
        tactics_saved = all(k in long_term for k in ("ad_purposes", "keyword_types"))

        strategy_context = StrategyConfirmRequest(
            asin=asin,
            product_level=long_term.get("product_level", "腰部"),
            product_stage=long_term.get("product_stage", "推进期"),
            season_stage=long_term.get("season_stage", "淡季"),
        ) if strategy_saved else None

        layer_config = settings.layer_options_config or {}
        tactics_cfg = layer_config.get("tactics", {})

        recommendations = {"ad_purposes": [], "keyword_types": []}
        reasoning = ""

        if strategy_saved:
            wf = self.state.get_workflow_state(asin)
            has_kw_cache = wf.get("keyword_analysis") is not None

            if has_kw_cache:
                # 回访：跳过 LLM，从 state 读 AI 分类，仅刷新 DB 排名字段
                data = await self._ensure_data(asin)
                ai_kw_map = {a.get("word", ""): a for a in wf["keyword_analysis"]}
                merged_kws = []
                for kw in data.keywords[:20]:
                    ai = ai_kw_map.get(kw.keyword, {})
                    merged_kws.append({
                        "word": kw.keyword,
                        "rank": kw.natural_rank,
                        "near_rank": kw.near_natural_rank,
                        "rank_change": kw.rank_change_14d or 0,
                        "rank_change_14d": kw.rank_change_14d or 0,
                        "strategy_type": ai.get("strategy_type", ""),
                        "action": ai.get("action", ""),
                    })
                wf["keyword_analysis"] = merged_kws
                self.state.set_workflow_state(asin, wf)
                recommendations = {
                    "ad_purposes": long_term.get("ad_purposes", []),
                    "keyword_types": long_term.get("keyword_types", []),
                }
            else:
                # 首次访问：调用 purpose-agent LLM
                try:
                    from app.llm.purpose_adapter import recommend_tactics_from_purpose
                    data = await self._ensure_data(asin)
                    rec = await recommend_tactics_from_purpose(
                        data=data,
                        position=strategy_context.product_level,
                        stage=strategy_context.product_stage,
                        season=strategy_context.season_stage,
                    )
                    if "error" in rec:
                        raise ValueError(rec["error"])
                    if not tactics_saved:
                        recommendations = {
                            "ad_purposes": rec.get("ad_purposes", []),
                            "keyword_types": rec.get("keyword_types", []),
                        }
                        reasoning = rec.get("reason", "")
                    # 合并 DB 关键词数据与 AI 分类
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
                            "strategy_type": ai.get("strategy_type", ""),
                            "action": ai.get("action", ""),
                        })
                    wf["keyword_analysis"] = merged_kws
                    wf["target_scores"] = rec.get("target_scores", [])
                    self.state.set_workflow_state(asin, wf)
                except Exception as e:
                    logger.warning("策略层 purpose-agent 推荐失败 [%s]: %s", asin, e)
                    # LLM 失败时，用 DB 关键词填充 keyword_analysis（无 AI 分类）
                    if 'data' in dir() and data and not data.data_missing:
                        fallback_kws = []
                        for kw in data.keywords[:20]:
                            fallback_kws.append({
                                "word": kw.keyword, "rank": kw.natural_rank,
                                "near_rank": kw.near_natural_rank,
                                "rank_change": kw.rank_change_14d or 0,
                                "rank_change_14d": kw.rank_change_14d or 0,
                                "strategy_type": "", "action": "",
                            })
                        wf["keyword_analysis"] = fallback_kws
                        wf["target_scores"] = []
                        self.state.set_workflow_state(asin, wf)

        dimensions = []
        for dim_key in ("ad_purposes", "keyword_types"):
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

        current = self.state.get_long_term_config(asin)

        # 读取保存的 AI 诊断附加数据
        wf = self.state.get_workflow_state(asin)
        return TacticsOptionsResponse(
            asin=asin,
            dimensions=dimensions,
            strategy_context=strategy_context,
            current_selection={
                "ad_purposes": current.get("ad_purposes"),
                "keyword_types": current.get("keyword_types"),
            } if current else None,
            target_scores=wf.get("target_scores", []),
            keyword_analysis=wf.get("keyword_analysis", []),
        )

    async def get_tactics_recommendations(self, asin: str) -> dict:
        """强制 AI 重新推荐策略选项（不保存，仅返回推荐结果）

        由前端「AI 重新推荐」按钮触发，调用 purpose-agent。
        """
        long_term = self.state.get_long_term_config(asin)
        data = await self._ensure_data(asin)

        from app.llm.purpose_adapter import recommend_tactics_from_purpose
        rec = await recommend_tactics_from_purpose(
            data=data,
            position=long_term.get("product_level", "腰部"),
            stage=long_term.get("product_stage", "推进期"),
            season=long_term.get("season_stage", "淡季"),
        )

        # purpose-agent 失败时不清空已有缓存
        if "error" in rec:
            wf = self.state.get_workflow_state(asin)
            return {
                "asin": asin,
                "dimensions": [],
                "reasoning": "",
                "keyword_analysis": wf.get("keyword_analysis", []),
                "target_scores": wf.get("target_scores", []),
                "error": rec["error"],
            }

        layer_config = settings.layer_options_config or {}
        tactics_cfg = layer_config.get("tactics", {})
        reasoning = rec.get("reason", "")

        dimensions = []
        for dim_key in ("ad_purposes", "keyword_types"):
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
                "strategy_type": ai.get("strategy_type", ""),
                "action": ai.get("action", ""),
            })
        wf = self.state.get_workflow_state(asin)
        wf["keyword_analysis"] = merged_kws
        wf["target_scores"] = rec.get("target_scores", [])
        self.state.set_workflow_state(asin, wf)

        return {
            "asin": asin,
            "dimensions": dimensions,
            "reasoning": reasoning,
            "keyword_analysis": merged_kws,
            "target_scores": rec.get("target_scores", []),
        }

    async def confirm_tactics(self, req: TacticsConfirmRequest) -> TacticsConfirmResponse:
        config = {
            "ad_purposes": [p.value for p in req.ad_purposes],
            "keyword_types": [k.value for k in req.keyword_types],
        }
        self.state.set_long_term_config(req.asin, config)
        self.state.advance_layer(req.asin, "diagnosis")

        return TacticsConfirmResponse(
            asin=req.asin,
            accepted=True,
            config_saved=True,
        )

    # ── Layer 1.3 诊断层 ─────────────────────────────────

    async def get_diagnosis(self, asin: str, refresh: bool = False) -> DiagnosisResponse:
        """返回只读诊断数据

        无缓存 → 全量查，不触发二次按需查
        有缓存 → 直接读缓存
        刷新   → 缓存快照做场景检测 → 一次按需查询
        """
        long_term = self.state.get_long_term_config(asin)
        async with self._cache_lock:
            cached_entry = self._data_cache.get(asin)

        if not cached_entry:
            data = await self._ensure_data(asin)
            if _is_valid_data(data):
                async with self._cache_lock:
                    self._data_cache[asin] = CacheEntry(data=data, timestamp=time.time())
        elif not refresh:
            data = cached_entry.data
            if not _is_valid_data(data):
                logger.warning("诊断缓存无效，降级全量查询 [%s]", asin)
                data = await self._ensure_data(asin)
                if _is_valid_data(data):
                    async with self._cache_lock:
                        self._data_cache[asin] = CacheEntry(data=data, timestamp=time.time())
        else:
            snapshot = cached_entry.data

        strategy_ctx = StrategyConfirmRequest(
            asin=asin,
            product_level=long_term.get("product_level", "腰部"),
            product_stage=long_term.get("product_stage", "推进期"),
            season_stage=long_term.get("season_stage", "淡季"),
        ) if long_term else None

        tactics_ctx = TacticsConfirmRequest(
            asin=asin,
            ad_purposes=long_term.get("ad_purposes", []),
            keyword_types=long_term.get("keyword_types", []),
        ) if long_term else None

        # 北极星指标看板（含场景检测）
        _detection_data = snapshot if refresh and cached_entry else data
        metric_board = build_metric_board(
            _detection_data,
            strategy_ctx.product_stage if strategy_ctx else None,
            long_term.get("ad_purposes", []) if long_term else [],
        )

        scenario_id = metric_board.get("scenario_id", "default")
        meta_ids = QueryRouter.resolve(scenario_id)
        query_plan = QueryRouter.describe(meta_ids)

        if refresh and cached_entry:
            async with self._cache_lock:
                self._data_cache.pop(asin, None)
            data = await self.aggregator.fetch(asin, meta_filter=meta_ids)
            if _is_valid_data(data):
                async with self._cache_lock:
                    self._data_cache[asin] = CacheEntry(data=data, timestamp=time.time())

        # 告警信号
        alerts = []
        sig = data.signals
        if sig:
            if sig.inventory_days and sig.inventory_days < 14:
                alerts.append(f"库存仅剩 {sig.inventory_days:.0f} 天")
            if sig.threat_score and sig.threat_score > 50:
                alerts.append(f"竞争威胁分 {sig.threat_score:.0f}，需关注竞品动态")
            if sig.clearance_urgent:
                alerts.append("清仓急迫，建议加速清库存")
        if data.ad_data:
            if data.ad_data.acos and data.ad_data.acos > 40:
                alerts.append(f"ACOS {data.ad_data.acos:.0f}% 严重偏高")
        if not alerts:
            alerts.append("当前无异常告警信号")

        # 关键词监控表 — 合并 AI 分类结果
        wf = self.state.get_workflow_state(asin)
        strategy_saved = all(k in long_term for k in ("product_level", "product_stage", "season_stage"))
        if not wf.get("keyword_analysis") and strategy_saved:
            # 回访已有策略的 ASIN 时，独立获取关键词 AI 分类
            try:
                from app.llm.purpose_adapter import recommend_tactics_from_purpose
                rec = await recommend_tactics_from_purpose(
                    data=data,
                    position=long_term.get("product_level", "腰部"),
                    stage=long_term.get("product_stage", "推进期"),
                    season=long_term.get("season_stage", "淡季"),
                )
                if "error" not in rec:
                    wf["keyword_analysis"] = rec.get("keyword_analysis", [])
                    self.state.set_workflow_state(asin, wf)
            except Exception as e:
                logger.warning("诊断层关键词 AI 分类失败 [%s]: %s", asin, e)
        ai_kw_map = {}
        for ak in wf.get("keyword_analysis", []):
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
                "spend": kw.spend,
                "acos": kw.acos,
                "cvr": kw.cvr,
                "bid": kw.bid,
                "match_type": kw.match_type,
                "strategy_type": ai.get("strategy_type", ""),
                "action": ai.get("action", ""),
            })
        # 趋势数据
        trend_list = [
            {"date": tp.date, "acos": tp.acos, "cvr": tp.cvr,
             "ctr": tp.ctr, "cpc": tp.cpc, "orders": tp.orders, "spend": tp.spend}
            for tp in data.trend
        ] if data.trend else []

        diag_summary = self._build_data_summary(data)
        manual_acos = self.state.get_target_acos_override(asin)
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
        )

    # ── Layer 1.4 执行层 ─────────────────────────────────

    async def get_execution_options(self, asin: str) -> ExecutionOptionsResponse:
        """返回方向选项 + 评分 + LLM推荐（从缓存取）"""
        data = await self._ensure_data(asin)
        long_term = self.state.get_long_term_config(asin)

        # 将战略/策略字段注入ASINData，供Recommender使用
        if long_term:
            data.product_level = long_term.get("product_level")
            data.product_stage = long_term.get("product_stage")
            data.season_stage = long_term.get("season_stage")
            purposes = long_term.get("ad_purposes", [])
            data.ad_purpose = purposes[0] if purposes else None
            data.keyword_type = ",".join(long_term.get("keyword_types", []))

        # 运行推荐器评分
        rec_response = self.recommender.recommend(data)

        # LLM 推荐
        strategy = {
            "product_level": long_term.get("product_level", "腰部"),
            "product_stage": long_term.get("product_stage", ""),
            "season_stage": long_term.get("season_stage", ""),
        } if long_term else {}
        tactics = {
            "ad_purposes": long_term.get("ad_purposes", []),
            "keyword_types": long_term.get("keyword_types", []),
        } if long_term else {}

        data_summary = self._build_data_summary(data)
        # 注入运营手动设定的目标 ACOS（覆盖硬编码默认值 25）
        manual_acos = self.state.get_target_acos_override(asin)
        if manual_acos is not None:
            data_summary["acos_target"] = manual_acos
        exec_rec = await self.reasoner.recommend_execution(
            asin=asin,
            data_summary=data_summary,
            strategy=strategy,
            tactics=tactics,
            scores=[s.model_dump() for s in rec_response.directions],
        )

        # 组装方向列表
        recommended_ids = exec_rec.get("recommended_directions", [])
        directions = []
        for s in rec_response.directions:
            directions.append(ExecutionDirection(
                id=s.id,
                label=s.label,
                suitability_score=s.suitability_score,
                suitability=s.suitability,
                reason=s.reason,
                recommended=s.id in recommended_ids,
                recommendation_reason=exec_rec.get("reasoning", ""),
                default_sub_options=s.default_sub_options,
            ))

        strategy_ctx = StrategyConfirmRequest(
            asin=asin,
            product_level=long_term.get("product_level", "腰部"),
            product_stage=long_term.get("product_stage", "推进期"),
            season_stage=long_term.get("season_stage", "淡季"),
        ) if long_term else None
        tactics_ctx = TacticsConfirmRequest(
            asin=asin,
            ad_purposes=long_term.get("ad_purposes", []),
            keyword_types=long_term.get("keyword_types", []),
        ) if long_term else None

        # 查询路由：场景 → 元脚本清单
        scenario = detect_scenario(
            data,
            strategy.get("product_stage"),
            tactics.get("ad_purposes"),
        )
        meta_ids = QueryRouter.resolve(scenario.get("id", "default"), recommended_ids)
        query_plan = QueryRouter.describe(meta_ids)

        return ExecutionOptionsResponse(
            asin=asin,
            directions=directions,
            recommended_directions=recommended_ids,
            recommendation_summary=exec_rec.get("reasoning", ""),
            strategy_context=strategy_ctx,
            tactics_context=tactics_ctx,
            query_plan=query_plan,
        )

    async def confirm_execution(self, req: ExecutionSelectRequest) -> ExecutionSelectResponse:
        """保存执行层选择到工作流状态（不持久化）"""
        self.state.save_execution(req.asin, req.model_dump())
        self.state.advance_layer(req.asin, "validation")

        return ExecutionSelectResponse(
            asin=req.asin,
            accepted=True,
        )

    # ── P3 上游推荐方法 ──────────────────────────────────

    async def get_target_acos_recommendation(self, asin: str):
        """P3 Feature 1: 目标 ACOS 推荐 — 优先返回手动设定值（每日5:00过期），否则走算法"""
        manual = self.state.get_target_acos_override(asin)

        if manual is not None:
            recommender = TargetAcosRecommender()
            return recommender.recommend_manual(asin, manual)

        long_term = self.state.get_long_term_config(asin)
        data = await self._ensure_data(asin)
        if long_term:
            data.product_stage = long_term.get("product_stage")
            data.product_level = long_term.get("product_level")
            purposes = long_term.get("ad_purposes", [])
            data.ad_purpose = purposes[0] if purposes else None

        recommender = TargetAcosRecommender()
        ad_purposes = long_term.get("ad_purposes", []) if long_term else []
        return recommender.recommend(data, ad_purposes)

    def save_target_acos_override(self, asin: str, value: int) -> bool:
        """运营手动设定目标 ACOS（次日 5:00 过期），写入调整历史"""
        self.state.record_adjustment(asin, target_acos=value)
        return self.state.set_target_acos_override(asin, value)

    def clear_target_acos_override(self, asin: str) -> bool:
        """清除手动设定的目标 ACOS"""
        return self.state.clear_target_acos_override(asin)

    async def get_budget_bid_recommendation(self, asin: str):
        """P3 Feature 2: 预算和 Bid 推荐 — 优先返回手动设定值，否则走算法"""
        long_term = self.state.get_long_term_config(asin)
        manual = long_term.get("daily_budget_override") if long_term else None

        if manual is not None:
            recommender = BudgetBidRecommender()
            return recommender.recommend_manual(asin, manual)

        data = await self._ensure_data(asin)
        if long_term:
            data.product_stage = long_term.get("product_stage")
            data.season_stage = long_term.get("season_stage")
            purposes = long_term.get("ad_purposes", [])
            data.ad_purpose = purposes[0] if purposes else None

        last_adjustment = self.state.get_adjustment_history(asin)

        recommender = BudgetBidRecommender()
        ad_purposes = long_term.get("ad_purposes", []) if long_term else []
        season_stage = long_term.get("season_stage", "淡季") if long_term else "淡季"
        return recommender.recommend(data, ad_purposes, season_stage, last_adjustment)

    def save_budget_override(self, asin: str, value: float) -> bool:
        """运营手动设定日预算，写入 long_term_config + 调整历史"""
        self.state.record_adjustment(asin, daily_budget=value)
        return self.state.set_long_term_config(asin, {"daily_budget_override": value})

    def clear_budget_override(self, asin: str) -> bool:
        """清除手动设定的日预算"""
        return self.state.set_long_term_config(asin, {"daily_budget_override": None})

    # ── P3 统一推荐（LLM 驱动）─────────────────────────────

    async def get_unified_recommendation(self, asin: str, refresh: bool = False):
        """P3 统一推荐：手动覆盖 > LLM > 缓存 > 算法降级"""
        long_term = self.state.get_long_term_config(asin)

        # 0. 手动覆盖优先（refresh 时跳过，强制走 LLM）
        manual_acos = self.state.get_target_acos_override(asin)
        manual_budget = long_term.get("daily_budget_override") if long_term else None
        if not refresh and (manual_acos is not None or manual_budget is not None):
            from app.core.recommender import TargetAcosRecommender, BudgetBidRecommender
            tacos_rec = TargetAcosRecommender.recommend_manual(asin, manual_acos) if manual_acos is not None else None
            bb_rec = BudgetBidRecommender.recommend_manual(asin, manual_budget) if manual_budget is not None else None
            return {
                "asin": asin,
                "target_acos": {
                    "recommended_target": tacos_rec.recommended_target if tacos_rec else 0,
                    "reasoning": tacos_rec.reasoning_chain[0].result if tacos_rec else "",
                    "confidence": "high" if tacos_rec else "medium",
                    "manual_override": True if tacos_rec else False,
                },
                "budget_bid": {
                    "current": bb_rec.budget_recommendation.current if bb_rec else 0.0,
                    "suggested": bb_rec.budget_recommendation.suggested if bb_rec else 0.0,
                    "direction": "maintain",
                    "magnitude_pct": 0.0,
                    "reason": bb_rec.budget_recommendation.reason if bb_rec else "",
                    "bid_adjustments": [],
                    "manual_override": True if bb_rec else False,
                },
                "overall_reasoning": "当前使用运营手动设定值，点击「AI 重新推荐」可覆盖",
                "risk_warnings": [],
                "from_cache": False,
            }

        # 1. 检查缓存
        if not refresh:
            cached = self.state.get_p3_recommendation(asin)
            if cached:
                return cached

        # 2. 加载数据（refresh 时一并刷新数据缓存，避免 DB 瞬时故障的脏缓存）
        data = await self._ensure_data(asin, refresh=refresh)
        long_term = self.state.get_long_term_config(asin)

        strategy = {
            "product_level": long_term.get("product_level", "腰部"),
            "product_stage": long_term.get("product_stage", "推进期"),
            "season_stage": long_term.get("season_stage", "淡季"),
        } if long_term else {}
        tactics = {
            "ad_purposes": long_term.get("ad_purposes", []),
            "keyword_types": long_term.get("keyword_types", []),
        } if long_term else {}

        # 构建数据摘要
        data_summary = {}
        if data.ad_data:
            data_summary["ACOS"] = f"{data.ad_data.acos:.1f}%" if data.ad_data.acos else "N/A"
            data_summary["精准ACOS"] = f"{data.ad_data.precision_acos:.1f}%" if data.ad_data.precision_acos else "N/A"
            data_summary["非精准ACOS"] = f"{data.ad_data.broad_acos:.1f}%" if data.ad_data.broad_acos else "N/A"
            data_summary["TACOS"] = f"{data.ad_data.tacos:.1f}%" if data.ad_data.tacos else "N/A"
            data_summary["CPC"] = f"${data.ad_data.cpc:.2f}" if data.ad_data.cpc else "N/A"
            data_summary["CTR"] = f"{data.ad_data.ctr:.1f}%" if data.ad_data.ctr else "N/A"
            data_summary["CVR"] = f"{data.ad_data.cvr:.1f}%" if data.ad_data.cvr else "N/A"
            data_summary["日均花费"] = f"${data.ad_data.spend/7:.2f}" if data.ad_data.spend else "N/A"
            data_summary["日预算(活动合计)"] = f"${data.ad_data.daily_budget:.2f}" if data.ad_data.daily_budget else "N/A"
            spend = data.ad_data.spend or 0
            budget = data.ad_data.daily_budget or 1
            data_summary["花费率"] = f"{spend/7/budget*100:.0f}%" if budget > 0 else "N/A"
        if data.margin:
            data_summary["毛利率"] = f"{data.margin*100:.1f}%"
        if data.natural_order_ratio:
            data_summary["自然单占比"] = f"{data.natural_order_ratio:.1f}%"
        if data.avg_daily_sales_30d:
            data_summary["日均销量"] = f"{data.avg_daily_sales_30d:.1f}单"
        if data.signals and data.signals.inventory_qty:
            data_summary["可售库存"] = f"{data.signals.inventory_qty}件"

        # 关键词详情
        kw_details = []
        for kw in data.keywords[:10]:
            kw_details.append({
                "keyword": kw.keyword,
                "bid": kw.bid or 0,
                "cpc": kw.spend / kw.clicks if kw.clicks > 0 else 0,
                "acos": kw.acos or 0,
                "cvr": kw.cvr or 0,
                "spend": kw.spend or 0,
            })

        # 历史记录
        history = self.state.get_adjustment_history(asin)

        # 3. 调用 LLM
        try:
            llm_result = await asyncio.wait_for(self.reasoner.recommend_p3(
                asin=asin,
                strategy=strategy,
                tactics=tactics,
                data_summary=data_summary,
                keyword_details=kw_details,
                history=history,
                current_acos_target=manual_acos,
                current_daily_budget=manual_budget,
            ), timeout=LLM_TIMEOUT)
            # 组装为标准响应
            ta_raw = llm_result.get("target_acos", {})
            bb_raw = llm_result.get("budget_bid", {})
            result = {
                "asin": asin,
                "target_acos": {
                    "recommended_target": ta_raw.get("recommended_target", 20),
                    "reasoning": ta_raw.get("reasoning", ""),
                    "confidence": ta_raw.get("confidence", "medium"),
                },
                "budget_bid": {
                    "current": bb_raw.get("current", 0),
                    "suggested": bb_raw.get("suggested", 0),
                    "direction": bb_raw.get("direction", "maintain"),
                    "magnitude_pct": bb_raw.get("magnitude_pct", 0),
                    "reason": bb_raw.get("reason", ""),
                    "bid_adjustments": bb_raw.get("bid_adjustments", []),
                },
                "overall_reasoning": llm_result.get("overall_reasoning", ""),
                "risk_warnings": llm_result.get("risk_warnings", []),
                "from_cache": False,
            }
            self.state.set_p3_recommendation(asin, result)
            return result
        except Exception as e:
            logger.warning("P3 LLM 推荐失败 [%s]，降级为算法推荐: %s", asin, e)
            # 降级：算法推荐器
            from app.core.recommender import TargetAcosRecommender, BudgetBidRecommender
            ad_purposes = long_term.get("ad_purposes", []) if long_term else []
            tacos_rec = TargetAcosRecommender().recommend(data, ad_purposes)
            bb_rec = BudgetBidRecommender().recommend(
                data, ad_purposes,
                long_term.get("season_stage", "淡季") if long_term else "淡季",
            )
            return {
                "asin": asin,
                "target_acos": {
                    "recommended_target": tacos_rec.recommended_target,
                    "reasoning": "(算法降级) " + "；".join(
                        s.result for s in tacos_rec.reasoning_chain
                    ),
                    "confidence": "low",
                },
                "budget_bid": {
                    "current": bb_rec.budget_recommendation.current,
                    "suggested": bb_rec.budget_recommendation.suggested,
                    "direction": bb_rec.budget_recommendation.direction,
                    "magnitude_pct": bb_rec.budget_recommendation.magnitude_pct,
                    "reason": "(算法降级) " + bb_rec.budget_recommendation.reason,
                    "bid_adjustments": [
                        {"keyword": a.keyword, "current_bid": a.current_bid,
                         "suggested_bid": a.suggested_bid, "direction": a.direction,
                         "magnitude_pct": a.magnitude_pct, "reason": a.reason}
                        for a in bb_rec.bid_adjustments
                    ],
                },
                "overall_reasoning": "LLM 调用失败，使用算法降级推荐。建议稍后点击「AI 重新推荐」。",
                "risk_warnings": ["LLM 调用失败，当前为算法降级结果"],
                "from_cache": False,
            }

    # ── Layer 1.5 校验 + 报告 ────────────────────────────

    async def run_validation_and_report(self, asin: str) -> dict:
        """运行校验+确认+LLM报告，返回完整结果（从缓存取）"""
        data = await self._ensure_data(asin)
        long_term = self.state.get_long_term_config(asin)
        wf_state = self.state.get_workflow_state(asin)

        # 注入上下文
        if long_term:
            data.product_level = long_term.get("product_level")
            data.product_stage = long_term.get("product_stage")
            data.season_stage = long_term.get("season_stage")
            purposes = long_term.get("ad_purposes", [])
            data.ad_purpose = purposes[0] if purposes else None
            data.keyword_type = ",".join(long_term.get("keyword_types", []))

        selected_dirs = (wf_state.get("execution", {}) or {}).get("selected_directions", [])
        sub_options = (wf_state.get("execution", {}) or {}).get("sub_options", {})

        if not selected_dirs:
            # 默认取推荐方向
            rec = self.recommender.recommend(data)
            selected_dirs = [rec.recommended_direction]

        # 并行校验+确认
        validations = {}
        decisions = {}
        for direction in selected_dirs:
            validations[direction] = (
                await self.validator.validate(data, direction, sub_options.get(direction, {}))
            ).model_dump()
            decisions[direction] = {
                "decision_package": (
                    self.decision_gen.generate(data, direction, sub_options.get(direction, {}))
                ).model_dump()
            }

        # 评分
        rec_response = self.recommender.recommend(data)

        # LLM报告
        strategy = {
            "product_level": long_term.get("product_level"),
            "product_stage": long_term.get("product_stage"),
            "season_stage": long_term.get("season_stage"),
        } if long_term else None
        tactics = {
            "ad_purposes": long_term.get("ad_purposes", []),
            "keyword_types": long_term.get("keyword_types", []),
        } if long_term else None

        rpt_summary = self._build_data_summary(data)
        manual_acos = self.state.get_target_acos_override(asin)
        if manual_acos is not None:
            rpt_summary["acos_target"] = manual_acos
        analysis = await self.reasoner.analyze(
            asin=asin,
            data_summary=rpt_summary,
            scores=[s.model_dump() for s in rec_response.directions],
            validations=validations,
            decisions=decisions,
            strategy=strategy,
            tactics=tactics,
        )

        # 查询路由
        scenario = detect_scenario(
            data,
            strategy.get("product_stage") if strategy else None,
            tactics.get("ad_purposes", []) if tactics else [],
        )
        meta_ids = QueryRouter.resolve(scenario.get("id", "default"), selected_dirs)
        query_plan = QueryRouter.describe(meta_ids)

        return {
            "asin": asin,
            "validations": validations,
            "decisions": decisions,
            "analysis": analysis,
            "directions": [s.model_dump() for s in rec_response.directions],
            "data_summary": rpt_summary,
            "data_completeness": {
                "status": "missing" if data.data_missing else "complete",
                "missing_fields": data.missing_fields,
            },
            "query_plan": query_plan,
        }

    # ── 向导状态 ─────────────────────────────────────────

    def get_wizard_state(self, asin: str) -> WizardStateResponse:
        """获取完整向导状态"""
        wf = self.state.get_workflow_state(asin)
        lt = self.state.get_long_term_config(asin)

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
                keyword_types=lt.get("keyword_types", []),
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
            long_term_config_exists=self.state.config_exists(asin),
            last_updated=wf.get("last_updated", ""),
        )

    def reset_asin(self, asin: str) -> bool:
        return self.state.reset_asin(asin)

    # ── 辅助方法 ─────────────────────────────────────────

    def _build_data_summary(self, data: ASINData) -> dict:
        """从ASINData提取数据摘要

        包含4层:
        1. 基础聚合指标（原有）
        2. 关键词级洞察（高ACOS/上升词/零转化/高转化）
        3. 竞品摘要（价格/评分对比）
        4. 广告位对比（精准 vs 非精准）
        """
        summary = {}

        # ── 1. 基础聚合指标 ──
        summary["keyword_count"] = data.keyword_count or len(data.keywords)
        summary["acos"] = round(data.ad_data.acos, 1) if data.ad_data and data.ad_data.acos is not None else None
        summary["tacos"] = round(data.ad_data.tacos, 1) if data.ad_data and data.ad_data.tacos is not None else None
        summary["cvr"] = round(data.ad_data.cvr, 1) if data.ad_data and data.ad_data.cvr is not None else None
        summary["cpc"] = round(data.ad_data.cpc, 2) if data.ad_data and data.ad_data.cpc is not None else None
        summary["spend"] = round(data.ad_data.spend, 2) if data.ad_data and data.ad_data.spend is not None else None
        # acos_target 仅在运营手动设定时才存在，不设置默认值（错误的值比未知危害更大）
        summary["top_keyword_rank"] = min(
            (kw.natural_rank for kw in data.keywords if kw.natural_rank), default=None
        )
        summary["natural_order_ratio"] = round(data.natural_order_ratio, 1) if data.natural_order_ratio is not None else None
        summary["available_new_keywords"] = data.available_new_keywords
        summary["inventory_days"] = round(data.signals.inventory_days, 1) if data.signals and data.signals.inventory_days is not None else None
        summary["days_since_launch"] = data.days_since_launch
        summary["daily_ad_spend_ratio"] = round(data.ad_data.daily_ad_spend_ratio, 1) if data.ad_data and data.ad_data.daily_ad_spend_ratio is not None else None
        summary["avg_daily_sales_30d"] = round(data.avg_daily_sales_30d, 1) if data.avg_daily_sales_30d is not None else None
        summary["margin"] = round(data.margin * 100, 1) if data.margin is not None else None

        # ── 2. 关键词级洞察 ──
        valid_kws = [kw for kw in data.keywords if kw.keyword]

        # 高ACOS词 TOP5
        acos_sorted = sorted(
            [kw for kw in valid_kws if kw.acos is not None and kw.acos > 0],
            key=lambda x: x.acos or 0, reverse=True
        )[:5]
        summary["high_acos_keywords"] = [
            {"keyword": kw.keyword, "acos": round(kw.acos, 1),
             "spend": round(kw.spend, 1), "orders": kw.orders,
             "match_type": kw.match_type}
            for kw in acos_sorted
        ]

        # 上升词 TOP5
        rising_sorted = sorted(
            [kw for kw in valid_kws if kw.rank_change_14d is not None and kw.rank_change_14d > 0],
            key=lambda x: x.rank_change_14d or 0, reverse=True
        )[:5]
        summary["rising_keywords"] = [
            {"keyword": kw.keyword, "rank_change": kw.rank_change_14d,
             "natural_rank": kw.natural_rank, "acos": round(kw.acos, 1) if kw.acos else None}
            for kw in rising_sorted
        ]

        # 高花费零转化词
        wasteful = [kw for kw in valid_kws if kw.spend >= 15 and kw.orders == 0]
        summary["wasteful_keywords"] = [
            {"keyword": kw.keyword, "spend": round(kw.spend, 1),
             "impressions": kw.impressions, "match_type": kw.match_type}
            for kw in wasteful[:5]
        ]

        # 高转化词 TOP5（至少3单）
        cvr_sorted = sorted(
            [kw for kw in valid_kws if kw.cvr is not None and kw.cvr > 0 and kw.orders >= 3],
            key=lambda x: x.cvr or 0, reverse=True
        )[:5]
        summary["top_cvr_keywords"] = [
            {"keyword": kw.keyword, "cvr": round(kw.cvr, 1),
             "acos": round(kw.acos, 1) if kw.acos else None,
             "orders": kw.orders}
            for kw in cvr_sorted
        ]

        # ── 3. 竞品摘要 ──
        comps = data.competitors
        if comps:
            prices = [c.price for c in comps if c.price]
            ratings = [c.rating for c in comps if c.rating]
            our_price = data.price
            price_comp = ""
            if prices and our_price:
                avg_comp = sum(prices) / len(prices)
                if our_price < avg_comp * 0.85:
                    price_comp = "低于竞品均价"
                elif our_price > avg_comp * 1.15:
                    price_comp = "高于竞品均价"
                else:
                    price_comp = "与竞品均价持平"

            summary["competitor_summary"] = {
                "competitor_count": len(comps),
                "price_range": f"${min(prices):.2f} - ${max(prices):.2f}" if prices else None,
                "avg_rating": round(sum(ratings) / len(ratings), 1) if ratings else None,
                "our_price": our_price,
                "price_position": price_comp,
            }
        else:
            summary["competitor_summary"] = None

        # ── 4. 广告位对比（TOS vs ROS，来自 placement_report）──
        ad = data.ad_data
        if ad and (ad.placement_tos_acos is not None or ad.placement_ros_acos is not None):
            summary["placement_comparison"] = {
                "tos_acos": round(ad.placement_tos_acos, 1) if ad.placement_tos_acos else None,
                "ros_acos": round(ad.placement_ros_acos, 1) if ad.placement_ros_acos else None,
                "tos_cpc": round(ad.placement_tos_cpc, 2) if ad.placement_tos_cpc else None,
                "ros_cpc": round(ad.placement_ros_cpc, 2) if ad.placement_ros_cpc else None,
                "tos_spend_ratio": round(ad.placement_tos_spend_ratio, 1) if ad.placement_tos_spend_ratio else None,
                "ros_spend_ratio": round(ad.placement_ros_spend_ratio, 1) if ad.placement_ros_spend_ratio else None,
            }
        else:
            summary["placement_comparison"] = None

        # 数据质量
        summary["data_completeness"] = "complete" if not data.data_missing else "missing"

        return summary


# 全局单例
workflow_orchestrator = WorkflowOrchestrator()
