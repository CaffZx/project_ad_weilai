"""按 HTTP 端点编译单节点 LangGraph（分步 Demo 对齐）。"""

import asyncio
from typing import Callable

from langgraph.graph import END, StateGraph

from app.agents.state import AgentState
from app.workflow.context import WorkflowContext


def compile_single_node_graph(
    node_fn: Callable,
    ctx: WorkflowContext,
):
    """单端点 → 单节点子图，无 checkpoint（P1 使用 workflow_state.json）。"""
    graph = StateGraph(AgentState)

    if asyncio.iscoroutinefunction(node_fn):

        async def runner(state: AgentState) -> dict:
            return await node_fn(state, ctx=ctx)

    else:

        def runner(state: AgentState) -> dict:
            return node_fn(state, ctx=ctx)

    graph.add_node("run", runner)
    graph.set_entry_point("run")
    graph.add_edge("run", END)
    return graph.compile()
