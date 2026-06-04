"""GraphBridge — API 端点映射到 LangGraph 单节点子图。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from app.agents.graph import compile_single_node_graph
from app.agents.nodes import diagnosis as diagnosis_nodes
from app.agents.nodes import execution as execution_nodes
from app.agents.nodes import p3 as p3_nodes
from app.agents.nodes import strategy as strategy_nodes
from app.agents.nodes import tactics as tactics_nodes
from app.agents.nodes import validation_report as report_nodes
from app.agents.nodes import wizard as wizard_nodes
from app.agents.state import AgentState

if TYPE_CHECKING:
    from app.core.workflow_orchestrator import WorkflowOrchestrator

# endpoint_id -> (node_fn, is_async)
ENDPOINT_NODES: dict[str, tuple[Any, bool]] = {
    "strategy.options": (strategy_nodes.strategy_options_node, True),
    "strategy.confirm": (strategy_nodes.strategy_confirm_node, True),
    "tactics.options": (tactics_nodes.tactics_options_node, True),
    "tactics.recommendations": (tactics_nodes.tactics_recommendations_node, True),
    "tactics.confirm": (tactics_nodes.tactics_confirm_node, True),
    "diagnosis": (diagnosis_nodes.diagnosis_node, True),
    "execution.options": (execution_nodes.execution_options_node, True),
    "execution.confirm": (execution_nodes.execution_confirm_node, True),
    "p3.target_acos": (p3_nodes.p3_target_acos_node, True),
    "p3.budget_bid": (p3_nodes.p3_budget_bid_node, True),
    "p3.unified": (p3_nodes.p3_unified_node, True),
    "wizard.report": (report_nodes.validation_report_node, True),
    "wizard.state": (wizard_nodes.wizard_state_node, False),
    "wizard.reset": (wizard_nodes.wizard_reset_node, False),
}


class GraphBridge:
    """按 endpoint 运行单节点 LangGraph，与分步 HTTP API 一致。"""

    def __init__(self, orchestrator: WorkflowOrchestrator):
        self._orch = orchestrator
        self._compiled: dict[str, Any] = {}

    def _ctx(self):
        return self._orch._ctx()

    def _get_graph(self, endpoint: str):
        if endpoint not in self._compiled:
            node_fn, _ = ENDPOINT_NODES[endpoint]
            self._compiled[endpoint] = compile_single_node_graph(
                node_fn, self._ctx()
            )
        return self._compiled[endpoint]

    async def run_endpoint(self, endpoint: str, **kwargs: Any) -> Any:
        if endpoint not in ENDPOINT_NODES:
            raise ValueError(f"Unknown LangGraph endpoint: {endpoint}")
        state: AgentState = {"endpoint": endpoint, **kwargs}
        graph = self._get_graph(endpoint)
        out = await graph.ainvoke(state)
        return out.get("result")

    def run_endpoint_sync(self, endpoint: str, **kwargs: Any) -> Any:
        node_fn, is_async = ENDPOINT_NODES[endpoint]
        state: AgentState = {"endpoint": endpoint, **kwargs}
        if is_async:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop and loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    return pool.submit(
                        asyncio.run, self.run_endpoint(endpoint, **kwargs)
                    ).result()
            return asyncio.run(self.run_endpoint(endpoint, **kwargs))
        return node_fn(state, ctx=self._ctx()).get("result")
