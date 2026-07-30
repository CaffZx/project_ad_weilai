"""Layer 1.1 战略层 API — 产品定位/产品阶段/淡旺季"""

from fastapi import APIRouter, Depends

from app.api.config_mirror import mirror_agent_config
from app.api.deps import get_workflow_orchestrator
from app.api.product_identity import require_product_identity
from app.core.workflow_orchestrator import WorkflowOrchestrator
from app.models.layers import (
    StrategyOptionsResponse,
    StrategyConfirmRequest,
    StrategyConfirmResponse,
)

router = APIRouter()


@router.post("/strategy/options", response_model=StrategyOptionsResponse)
async def strategy_options(
    req: dict,
    orchestrator: WorkflowOrchestrator = Depends(get_workflow_orchestrator),
):
    """获取战略层三个维度选项（产品定位/产品阶段/淡旺季）

    Agent 只提供选项，不推荐。单一维度各选项互斥。
    若已有长期配置则自动预填。
    """
    return await orchestrator.get_strategy_options(req.get("asin", ""), days=req.get("days", 7))


@router.post("/strategy/confirm", response_model=StrategyConfirmResponse)
async def strategy_confirm(
    req: StrategyConfirmRequest,
    orchestrator: WorkflowOrchestrator = Depends(get_workflow_orchestrator),
):
    """确认战略层选择，持久化为长期默认配置"""
    require_product_identity(req)
    result = await orchestrator.confirm_strategy(req)
    if result.config_saved:
        await mirror_agent_config(
            req=req,
            patch={
                "product_position": req.product_level.value,
                "operating_mode": req.operating_mode.value if req.operating_mode else None,
                "season_type": req.season_stage.value,
            },
            operation="strategy_confirm",
        )
    return result
