"""Workflow 运行时上下文 — 步骤函数通过 ctx 访问服务与数据缓存。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from app.core.data_aggregator import DataAggregator
from app.core.decision_package import DecisionPackageGenerator
from app.core.recommender import Recommender
from app.core.validation_engine import ValidationEngine
from app.llm.reasoner import LLMReasoner
from app.models.asin_data import ASINData
from app.persistence.state_manager import StateManager

if TYPE_CHECKING:
    from app.core.workflow_orchestrator import WorkflowOrchestrator


@dataclass
class WorkflowContext:
    aggregator: DataAggregator
    recommender: Recommender
    reasoner: LLMReasoner
    state: StateManager
    validator: ValidationEngine
    decision_gen: DecisionPackageGenerator
    ensure_data: Callable[..., Awaitable[ASINData]]
    preload_data: Callable[..., Awaitable[None]]

    @classmethod
    def from_orchestrator(cls, orch: WorkflowOrchestrator) -> WorkflowContext:
        return cls(
            aggregator=orch.aggregator,
            recommender=orch.recommender,
            reasoner=orch.reasoner,
            state=orch.state,
            validator=orch.validator,
            decision_gen=orch.decision_gen,
            ensure_data=orch._ensure_data,
            preload_data=orch._preload_data,
        )
