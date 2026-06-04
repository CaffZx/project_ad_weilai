"""依赖注入 — 提供全局单例"""

from app.llm.reasoner import LLMReasoner


async def get_llm_reasoner() -> LLMReasoner:
    from app.llm.reasoner import reasoner
    return reasoner


async def get_workflow_orchestrator():
    from app.core.workflow_orchestrator import workflow_orchestrator
    return workflow_orchestrator
