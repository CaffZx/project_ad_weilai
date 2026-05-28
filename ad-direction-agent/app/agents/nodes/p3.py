from app.agents.state import AgentState
from app.workflow.context import WorkflowContext
from app.workflow.steps import p3 as p3_steps


async def p3_target_acos_node(state: AgentState, *, ctx: WorkflowContext) -> dict:
    result = await p3_steps.run_get_target_acos_recommendation(
        ctx, state["asin"], state.get("days", 7)
    )
    return {"result": result}


async def p3_budget_bid_node(state: AgentState, *, ctx: WorkflowContext) -> dict:
    result = await p3_steps.run_get_budget_bid_recommendation(
        ctx, state["asin"], state.get("days", 7)
    )
    return {"result": result}


async def p3_unified_node(state: AgentState, *, ctx: WorkflowContext) -> dict:
    result = await p3_steps.run_get_unified_recommendation(
        ctx,
        state["asin"],
        state.get("refresh", False),
        state.get("days", 7),
    )
    return {"result": result}
