"""Layer 1.2 策略层 API — 广告目的/关键词类型"""

from fastapi import APIRouter, Depends

from app.api.deps import get_workflow_orchestrator
from app.core.workflow_orchestrator import WorkflowOrchestrator
from app.models.layers import (
    TacticsOptionsResponse,
    TacticsConfirmRequest,
    TacticsConfirmResponse,
)

router = APIRouter()


@router.post("/tactics/options", response_model=TacticsOptionsResponse)
async def tactics_options(
    req: dict,
    orchestrator: WorkflowOrchestrator = Depends(get_workflow_orchestrator),
):
    """获取策略层选项（含AI推荐）

    Agent 基于战略层选择 + 诊断数据推荐广告目的和关键词类型。
    选项非互斥，可多选。已有长期配置则自动预填。
    """
    return await orchestrator.get_tactics_options(req.get("asin", ""))


@router.post("/tactics/recommend")
async def tactics_recommend(
    req: dict,
    orchestrator: WorkflowOrchestrator = Depends(get_workflow_orchestrator),
):
    """强制 AI 重新推荐策略选项（不保存，仅返回推荐结果）"""
    return await orchestrator.get_tactics_recommendations(req.get("asin", ""))


@router.post("/tactics/confirm", response_model=TacticsConfirmResponse)
async def tactics_confirm(
    req: TacticsConfirmRequest,
    orchestrator: WorkflowOrchestrator = Depends(get_workflow_orchestrator),
):
    """确认策略层选择，持久化为长期默认配置"""
    return await orchestrator.confirm_tactics(req)
