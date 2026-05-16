"""Layer 1.3 诊断层 API — 产品数据只读"""

from fastapi import APIRouter, Depends

from app.api.deps import get_workflow_orchestrator
from app.core.workflow_orchestrator import WorkflowOrchestrator
from app.models.layers import DiagnosisResponse

router = APIRouter()


@router.post("/diagnosis", response_model=DiagnosisResponse)
async def diagnosis(
    req: dict,
    orchestrator: WorkflowOrchestrator = Depends(get_workflow_orchestrator),
):
    """获取诊断数据（只读）

    refresh=True 时跳过缓存强制刷新数据
    """
    return await orchestrator.get_diagnosis(
        asin=req.get("asin", ""),
        refresh=req.get("refresh", False),
    )
