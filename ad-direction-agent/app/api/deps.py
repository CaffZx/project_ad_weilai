"""依赖注入 — 提供全局单例"""

from app.core.data_aggregator import DataAggregator
from app.core.validation_engine import ValidationEngine
from app.core.recommender import Recommender
from app.core.decision_package import DecisionPackageGenerator
from app.llm.reasoner import LLMReasoner


async def get_data_aggregator() -> DataAggregator:
    from app.core.data_aggregator import data_aggregator
    return data_aggregator


async def get_validation_engine() -> ValidationEngine:
    from app.core.validation_engine import validation_engine
    return validation_engine


async def get_recommender() -> Recommender:
    from app.core.recommender import recommender
    return recommender


async def get_decision_generator() -> DecisionPackageGenerator:
    from app.core.decision_package import decision_generator
    return decision_generator


async def get_llm_reasoner() -> LLMReasoner:
    from app.llm.reasoner import reasoner
    return reasoner


async def get_workflow_orchestrator():
    from app.core.workflow_orchestrator import workflow_orchestrator
    return workflow_orchestrator


async def get_state_manager():
    from app.persistence.state_manager import state_manager
    return state_manager
