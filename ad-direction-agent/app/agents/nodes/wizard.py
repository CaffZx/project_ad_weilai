from app.agents.state import AgentState
from app.workflow.context import WorkflowContext
from app.workflow.steps import wizard as wizard_steps


def wizard_state_node(state: AgentState, *, ctx: WorkflowContext) -> dict:
    result = wizard_steps.run_get_wizard_state(ctx, state["asin"])
    return {"result": result}


def wizard_reset_node(state: AgentState, *, ctx: WorkflowContext) -> dict:
    result = wizard_steps.run_reset_asin(ctx, state["asin"])
    return {"result": result}
