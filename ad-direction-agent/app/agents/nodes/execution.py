from app.agents.state import AgentState
from app.workflow.context import WorkflowContext
from app.workflow.steps import execution as execution_steps


async def execution_options_node(state: AgentState, *, ctx: WorkflowContext) -> dict:
    result = await execution_steps.run_get_execution_options(
        ctx, state["asin"], state.get("days", 7)
    )
    return {"result": result}


async def execution_confirm_node(state: AgentState, *, ctx: WorkflowContext) -> dict:
    result = await execution_steps.run_confirm_execution(ctx, state["req"])
    return {"result": result}
