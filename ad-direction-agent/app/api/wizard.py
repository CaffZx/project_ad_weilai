"""向导状态 API — 查询/重置工作流状态"""

from fastapi import APIRouter, Depends

from app.api.deps import get_workflow_orchestrator
from app.core.workflow_orchestrator import WorkflowOrchestrator
from app.models.layers import WizardStateResponse

router = APIRouter()


@router.get("/wizard/state/{asin}", response_model=WizardStateResponse)
async def wizard_state(
    asin: str,
    orchestrator: WorkflowOrchestrator = Depends(get_workflow_orchestrator),
):
    """获取ASIN的完整向导状态（用于前端恢复进度）"""
    return orchestrator.get_wizard_state(asin)


@router.post("/wizard/reset/{asin}")
async def wizard_reset(
    asin: str,
    orchestrator: WorkflowOrchestrator = Depends(get_workflow_orchestrator),
):
    """重置ASIN工作流状态（保留长期配置）"""
    ok = orchestrator.reset_asin(asin)
    return {"asin": asin, "reset": ok}


@router.post("/wizard/report")
async def wizard_report(
    req: dict,
    orchestrator: WorkflowOrchestrator = Depends(get_workflow_orchestrator),
):
    """Layer 1.5 — 运行校验+确认+LLM报告"""
    return await orchestrator.run_validation_and_report(req.get("asin", ""), days=req.get("days", 7))
