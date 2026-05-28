from app.agents.state import AgentState
from app.workflow.context import WorkflowContext
from app.workflow.steps import strategy as strategy_steps


async def strategy_options_node(state: AgentState, *, ctx: WorkflowContext) -> dict:
    result = await strategy_steps.run_get_strategy_options(
        ctx, state["asin"], state.get("days", 7)
    )
    return {"result": result}


async def strategy_confirm_node(state: AgentState, *, ctx: WorkflowContext) -> dict:
    result = await strategy_steps.run_confirm_strategy(
        ctx, state["req"], state.get("days", 7)
    )
    return {"result": result}
