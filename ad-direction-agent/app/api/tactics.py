"""Layer 1.2 策略层 API — 广告目的/关键词类型"""

from fastapi import APIRouter, Depends

from app.api.config_mirror import mirror_agent_config
from app.api.deps import get_workflow_orchestrator
from app.api.product_identity import require_product_identity
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
    return await orchestrator.get_tactics_options(req.get("asin", ""), days=req.get("days", 7))


@router.post("/tactics/recommend")
async def tactics_recommend(
    req: dict,
    orchestrator: WorkflowOrchestrator = Depends(get_workflow_orchestrator),
):
    """强制 AI 重新推荐策略选项（不保存，仅返回推荐结果）"""
    return await orchestrator.get_tactics_recommendations(req.get("asin", ""), days=req.get("days", 7))


@router.post("/tactics/confirm", response_model=TacticsConfirmResponse)
async def tactics_confirm(
    req: TacticsConfirmRequest,
    orchestrator: WorkflowOrchestrator = Depends(get_workflow_orchestrator),
):
    """确认策略层选择，持久化为长期默认配置"""
    require_product_identity(req)
    result = await orchestrator.confirm_tactics(req)
    if result.config_saved:
        await mirror_agent_config(
            req=req,
            patch={
                "advert_purposes": [value.value for value in req.ad_purposes],
                "target_keyword_types": [
                    value.value for value in req.target_keyword_strategy
                ],
            },
            operation="tactics_confirm",
        )
    return result
