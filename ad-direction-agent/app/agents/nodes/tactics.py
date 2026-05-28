from app.agents.state import AgentState
from app.workflow.context import WorkflowContext
from app.workflow.steps import tactics as tactics_steps


async def tactics_options_node(state: AgentState, *, ctx: WorkflowContext) -> dict:
    result = await tactics_steps.run_get_tactics_options(
        ctx, state["asin"], state.get("days", 7)
    )
    return {"result": result}


async def tactics_recommendations_node(state: AgentState, *, ctx: WorkflowContext) -> dict:
    result = await tactics_steps.run_get_tactics_recommendations(
        ctx, state["asin"], state.get("days", 7)
    )
    return {"result": result}


async def tactics_confirm_node(state: AgentState, *, ctx: WorkflowContext) -> dict:
    result = await tactics_steps.run_confirm_tactics(ctx, state["req"])
    return {"result": result}
