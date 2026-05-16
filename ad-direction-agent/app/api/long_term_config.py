"""长期配置 API — 查看/手动修改持久化的战略+策略选择"""

from fastapi import APIRouter, Depends

from app.persistence.state_manager import StateManager
from app.models.layers import LongTermConfigResponse, LongTermConfigUpdateRequest

router = APIRouter()


def get_state_manager() -> StateManager:
    from app.persistence.state_manager import state_manager
    return state_manager


@router.get("/long-term-config/{asin}", response_model=LongTermConfigResponse)
async def get_config(
    asin: str,
    sm: StateManager = Depends(get_state_manager),
):
    """获取ASIN的长期配置（战略+策略层选择）"""
    config = sm.get_long_term_config(asin)
    return LongTermConfigResponse(
        asin=asin,
        product_level=config.get("product_level"),
        product_stage=config.get("product_stage"),
        season_stage=config.get("season_stage"),
        ad_purposes=config.get("ad_purposes", []),
        keyword_types=config.get("keyword_types", []),
        last_modified=config.get("last_modified", ""),
    )


@router.put("/long-term-config/{asin}", response_model=LongTermConfigResponse)
async def update_config(
    asin: str,
    req: LongTermConfigUpdateRequest,
    sm: StateManager = Depends(get_state_manager),
):
    """手动更新ASIN的长期配置"""
    updates = {}
    if req.product_level is not None:
        updates["product_level"] = req.product_level
    if req.product_stage is not None:
        updates["product_stage"] = req.product_stage
    if req.season_stage is not None:
        updates["season_stage"] = req.season_stage
    if req.ad_purposes is not None:
        updates["ad_purposes"] = req.ad_purposes
    if req.keyword_types is not None:
        updates["keyword_types"] = req.keyword_types

    sm.set_long_term_config(asin, updates)
    config = sm.get_long_term_config(asin)

    return LongTermConfigResponse(
        asin=asin,
        product_level=config.get("product_level"),
        product_stage=config.get("product_stage"),
        season_stage=config.get("season_stage"),
        ad_purposes=config.get("ad_purposes", []),
        keyword_types=config.get("keyword_types", []),
        last_modified=config.get("last_modified", ""),
    )
