"""POST /confirm — 确认最终方向，生成决策包"""

from fastapi import APIRouter, Depends
from app.models.tag import ConfirmRequest
from app.models.decision import ConfirmResponse
from app.api.deps import get_data_aggregator, get_decision_generator
from app.core.data_aggregator import DataAggregator
from app.core.decision_package import DecisionPackageGenerator

router = APIRouter()


@router.post("/confirm", response_model=ConfirmResponse)
async def confirm(
    req: ConfirmRequest,
    aggregator: DataAggregator = Depends(get_data_aggregator),
    generator: DecisionPackageGenerator = Depends(get_decision_generator),
):
    """确认最终广告方向，生成决策包（含操作任务）"""
    data = await aggregator.fetch(req.asin)

    package = generator.generate(
        data=data,
        direction=req.direction,
        sub_options=req.sub_options,
    )

    return ConfirmResponse(
        asin=req.asin,
        direction=req.direction,
        sub_options=req.sub_options,
        decision_package=package,
    )
