from app.agents.state import AgentState
from app.workflow.context import WorkflowContext
from app.workflow.steps import diagnosis as diagnosis_steps


async def diagnosis_node(state: AgentState, *, ctx: WorkflowContext) -> dict:
    result = await diagnosis_steps.run_get_diagnosis(
        ctx,
        state["asin"],
        state.get("refresh", False),
        state.get("days", 7),
    )
    return {"result": result}
