"""工作流编排器 — 数据缓存 + 委托 workflow.steps（可选 LangGraph bridge）。"""

import asyncio
import logging

from app.config.settings import settings
from app.core.data_aggregator import DataAggregator
from app.core.recommender import Recommender
from app.core.validation_engine import ValidationEngine
from app.core.decision_package import DecisionPackageGenerator
from app.llm.reasoner import LLMReasoner
from app.models.asin_data import ASINData
from app.models.layers import (
    StrategyOptionsResponse,
    StrategyConfirmRequest,
    StrategyConfirmResponse,
    TacticsOptionsResponse,
    TacticsConfirmRequest,
    TacticsConfirmResponse,
    DiagnosisResponse,
    ExecutionOptionsResponse,
    ExecutionSelectRequest,
    ExecutionSelectResponse,
    WizardStateResponse,
)
from app.persistence.redis_cache import asin_data_cache
from app.persistence.state_factory import get_state_manager
from app.persistence.state_manager import StateManager
from app.workflow.context import WorkflowContext
from app.workflow.steps import (
    diagnosis,
    execution,
    p3,
    strategy,
    tactics,
    validation_report,
    wizard,
)

logger = logging.getLogger(__name__)


class WorkflowOrchestrator:
    """四层工作流编排器 — 薄层：缓存 + 步骤委托"""

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
        self.recommender = recommender or Recommender()
        self.reasoner = reasoner or LLMReasoner()
        self.state = state_manager or get_state_manager()
        self.validator = validation_engine or ValidationEngine()
        self.decision_gen = decision_generator or DecisionPackageGenerator()
        self._cache_lock = asyncio.Lock()
        self._data_tasks: dict[tuple, asyncio.Task] = {}
        self._bridge = None

    def _ctx(self) -> WorkflowContext:
        return WorkflowContext.from_orchestrator(self)

    def _get_bridge(self):
        if self._bridge is None:
            from app.agents.bridge import GraphBridge
            self._bridge = GraphBridge(self)
        return self._bridge

    @staticmethod
    def _data_task_key(asin: str, days: int, meta_filter: list[str] | None = None) -> tuple:
        return (asin, days, tuple(sorted(set(meta_filter or []))))

    @staticmethod
    def _has_product_identity(data: ASINData | None) -> bool:
        return bool(
            data
            and getattr(data, "shop_id", None)
            and getattr(data, "parent_seller_sku", None)
        )

    async def _discard_cache_without_identity(
        self,
        asin: str,
        data: ASINData | None,
        *,
        days: int,
        meta_filter: list[str] | None = None,
    ) -> ASINData | None:
        if not data:
            return None
        if self._has_product_identity(data):
            return data
        await asin_data_cache.delete(asin, days, meta_filter=meta_filter)
        logger.info(
            "ASIN data cache missing product identity, discarded [%s] days=%s meta_filter=%s",
            asin,
            days,
            meta_filter,
        )
        return None

    # ── 数据缓存机制 ───────────────────────────────────────

    async def _preload_data(
        self,
        asin: str,
        days: int = 7,
        meta_filter: list[str] | None = None,
    ):
        """后台预加载 ASIN 数据，不阻塞当前请求"""
        key = self._data_task_key(asin, days, meta_filter)
        async with self._cache_lock:
            cached = await asin_data_cache.get(asin, days, meta_filter=meta_filter)
            cached = await self._discard_cache_without_identity(
                asin,
                cached,
                days=days,
                meta_filter=meta_filter,
            )
            if cached:
                return
            if key in self._data_tasks:
                return
        task = asyncio.create_task(self._do_preload(asin, days=days, meta_filter=meta_filter))
        self._data_tasks[key] = task

    async def _do_preload(
        self,
        asin: str,
        days: int = 7,
        meta_filter: list[str] | None = None,
    ):
        key = self._data_task_key(asin, days, meta_filter)
        try:
            data = await self.aggregator.fetch(asin, days=days, meta_filter=meta_filter)
            await asin_data_cache.set(asin, data, days=days, meta_filter=meta_filter)
        except Exception as e:
            logger.error("后台数据预加载失败 [%s]: %s", asin, e)
        finally:
            self._data_tasks.pop(key, None)

    async def _ensure_data(
        self,
        asin: str,
        refresh: bool = False,
        meta_filter: list[str] | None = None,
        days: int = 7,
    ) -> ASINData:
        """获取 ASIN 数据（Redis 短期缓存 / 强制刷新 / 按需 meta）。

        缓存按 (asin, days, meta_filter 集合) 分 key：各层场景裁剪的 META 列表不同 →
        各存各的部分快照，互不污染。meta_filter 分支同样走 读缓存→miss→fetch→写缓存，
        不再每次直拉（这是"同一 ASIN 当天重复点仍慢"的根因）。
        """
        key = self._data_task_key(asin, days)

        if refresh:
            async with self._cache_lock:
                if key in self._data_tasks:
                    self._data_tasks[key].cancel()
                    del self._data_tasks[key]
            # 删本 filter 的缓存；带 filter 刷新时连全量 key 一起失效，
            # 否则下次"全量超集命中"会拿旧全量盖过本次刷新。
            await asin_data_cache.delete(asin, days, meta_filter=meta_filter)
            if meta_filter:
                await asin_data_cache.delete(asin, days)
            logger.info("诊断刷新: 缓存已清除, 重新查询 %s", asin)
            if meta_filter:
                data = await self.aggregator.fetch(
                    asin,
                    meta_filter=meta_filter,
                    days=days,
                )
                await asin_data_cache.set(asin, data, days=days, meta_filter=meta_filter)
            else:
                data = await self.aggregator.fetch(asin, days=days)
                await asin_data_cache.set(asin, data, days=days)
            return data

        if meta_filter:
            # 1) 本 filter 部分缓存命中
            cached = await asin_data_cache.get(asin, days, meta_filter=meta_filter)
            cached = await self._discard_cache_without_identity(
                asin,
                cached,
                days=days,
                meta_filter=meta_filter,
            )
            if cached:
                return cached
            # 2) 全量缓存是任意 filter 的超集（strategy/options 后台 preload 写入）→ 直接复用。
            #    仅接受完整全量；partial 全量可能恰好缺本层维度，不冒险复用。
            full = await asin_data_cache.get(asin, days)
            full = await self._discard_cache_without_identity(asin, full, days=days)
            if full and getattr(full, "data_freshness", None) != "partial":
                return full
            # 3) miss → 拉本 filter 子集并缓存
            data = await self.aggregator.fetch(asin, meta_filter=meta_filter, days=days)
            await asin_data_cache.set(asin, data, days=days, meta_filter=meta_filter)
            return data

        cached = await asin_data_cache.get(asin, days)
        cached = await self._discard_cache_without_identity(asin, cached, days=days)
        if cached:
            return cached

        task = None
        async with self._cache_lock:
            if key in self._data_tasks:
                task = self._data_tasks[key]
        if task:
            try:
                await task
            except Exception:
                pass
            cached = await asin_data_cache.get(asin, days)
            cached = await self._discard_cache_without_identity(asin, cached, days=days)
            if cached:
                return cached

        data = await self.aggregator.fetch(asin, days=days)
        await asin_data_cache.set(asin, data, days=days)
        return data

    # ── Layer 1.1 战略层 ─────────────────────────────────

    async def get_strategy_options(self, asin: str, days: int = 7) -> StrategyOptionsResponse:
        if settings.use_langgraph:
            return await self._get_bridge().run_endpoint("strategy.options", asin=asin, days=days)
        return await strategy.run_get_strategy_options(self._ctx(), asin, days)

    async def confirm_strategy(
        self, req: StrategyConfirmRequest, days: int = 7
    ) -> StrategyConfirmResponse:
        if settings.use_langgraph:
            return await self._get_bridge().run_endpoint(
                "strategy.confirm", req=req, days=days
            )
        return await strategy.run_confirm_strategy(self._ctx(), req, days)

    # ── Layer 1.2 策略层 ─────────────────────────────────

    async def get_tactics_options(self, asin: str, days: int = 7) -> TacticsOptionsResponse:
        if settings.use_langgraph:
            return await self._get_bridge().run_endpoint("tactics.options", asin=asin, days=days)
        return await tactics.run_get_tactics_options(self._ctx(), asin, days)

    async def get_tactics_recommendations(self, asin: str, days: int = 7) -> dict:
        if settings.use_langgraph:
            return await self._get_bridge().run_endpoint(
                "tactics.recommendations", asin=asin, days=days
            )
        return await tactics.run_get_tactics_recommendations(self._ctx(), asin, days)

    async def confirm_tactics(self, req: TacticsConfirmRequest) -> TacticsConfirmResponse:
        if settings.use_langgraph:
            return await self._get_bridge().run_endpoint("tactics.confirm", req=req)
        return await tactics.run_confirm_tactics(self._ctx(), req)

    # ── Layer 1.3 诊断层 ─────────────────────────────────

    async def get_diagnosis(
        self, asin: str, refresh: bool = False, days: int = 7
    ) -> DiagnosisResponse:
        if settings.use_langgraph:
            return await self._get_bridge().run_endpoint(
                "diagnosis", asin=asin, refresh=refresh, days=days
            )
        return await diagnosis.run_get_diagnosis(self._ctx(), asin, refresh, days)

    # ── Layer 1.4 执行层 ─────────────────────────────────

    async def get_execution_options(self, asin: str, days: int = 7) -> ExecutionOptionsResponse:
        if settings.use_langgraph:
            return await self._get_bridge().run_endpoint(
                "execution.options", asin=asin, days=days
            )
        return await execution.run_get_execution_options(self._ctx(), asin, days)

    async def confirm_execution(self, req: ExecutionSelectRequest) -> ExecutionSelectResponse:
        if settings.use_langgraph:
            return await self._get_bridge().run_endpoint("execution.confirm", req=req)
        return await execution.run_confirm_execution(self._ctx(), req)

    # ── P3 推荐 ──────────────────────────────────────────

    async def get_target_acos_recommendation(self, asin: str, days: int = 7):
        if settings.use_langgraph:
            return await self._get_bridge().run_endpoint(
                "p3.target_acos", asin=asin, days=days
            )
        return await p3.run_get_target_acos_recommendation(self._ctx(), asin, days)

    def save_target_acos_override(
        self,
        asin: str,
        value: int,
        shop_id: int | None = None,
        parent_seller_sku: str | None = None,
    ) -> bool:
        return p3.run_save_target_acos_override(
            self._ctx(),
            asin,
            value,
            shop_id=shop_id,
            parent_seller_sku=parent_seller_sku,
        )

    def clear_target_acos_override(self, asin: str) -> bool:
        return p3.run_clear_target_acos_override(self._ctx(), asin)

    async def get_budget_bid_recommendation(self, asin: str, days: int = 7):
        if settings.use_langgraph:
            return await self._get_bridge().run_endpoint("p3.budget_bid", asin=asin, days=days)
        return await p3.run_get_budget_bid_recommendation(self._ctx(), asin, days)

    def save_budget_override(
        self,
        asin: str,
        value: float,
        shop_id: int | None = None,
        parent_seller_sku: str | None = None,
    ) -> bool:
        return p3.run_save_budget_override(
            self._ctx(),
            asin,
            value,
            shop_id=shop_id,
            parent_seller_sku=parent_seller_sku,
        )

    def clear_budget_override(self, asin: str) -> bool:
        return p3.run_clear_budget_override(self._ctx(), asin)

    async def get_unified_recommendation(
        self, asin: str, refresh: bool = False, days: int = 7
    ):
        if settings.use_langgraph:
            return await self._get_bridge().run_endpoint(
                "p3.unified", asin=asin, refresh=refresh, days=days
            )
        return await p3.run_get_unified_recommendation(self._ctx(), asin, refresh, days)

    # ── 校验与报告 ───────────────────────────────────────

    async def run_validation_and_report(self, asin: str, days: int = 7) -> dict:
        if settings.use_langgraph:
            return await self._get_bridge().run_endpoint(
                "wizard.report", asin=asin, days=days
            )
        return await validation_report.run_validation_and_report(self._ctx(), asin, days)

    def get_wizard_state(self, asin: str) -> WizardStateResponse:
        return wizard.run_get_wizard_state(self._ctx(), asin)

    def reset_asin(self, asin: str) -> bool:
        return wizard.run_reset_asin(self._ctx(), asin)


workflow_orchestrator = WorkflowOrchestrator()
