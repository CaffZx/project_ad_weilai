from app.agents.state import AgentState
from app.workflow.context import WorkflowContext
from app.workflow.steps import validation_report as report_steps


async def validation_report_node(state: AgentState, *, ctx: WorkflowContext) -> dict:
    result = await report_steps.run_validation_and_report(
        ctx, state["asin"], state.get("days", 7)
    )
    return {"result": result}
