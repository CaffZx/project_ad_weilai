"""POST /validate — 校验所选方向和子选项"""

from fastapi import APIRouter, Depends
from app.models.tag import ValidateRequest
from app.models.validation import ValidationResult
from app.api.deps import get_data_aggregator, get_validation_engine
from app.core.data_aggregator import DataAggregator
from app.core.validation_engine import ValidationEngine

router = APIRouter()


@router.post("/validate", response_model=ValidationResult)
async def validate(
    req: ValidateRequest,
    aggregator: DataAggregator = Depends(get_data_aggregator),
    engine: ValidationEngine = Depends(get_validation_engine),
):
    """校验所选广告方向和子选项的合理性"""
    data = await aggregator.fetch(req.asin)

    # 将长周期标签映射到 data 对象
    if req.long_term_tags:
        for key, value in req.long_term_tags.items():
            if hasattr(data, key):
                setattr(data, key, value)
    if req.special_scenario and req.special_scenario != "无":
        if data.signals:
            data.signals.clearance_urgent = (req.special_scenario == "清仓急迫")

    result = await engine.validate(
        data=data,
        direction=req.direction,
        sub_options=req.sub_options,
    )
    return result
