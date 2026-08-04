"""长期配置 API — 查看/手动修改持久化的战略+策略选择"""

import asyncio
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException

from app.api.product_identity import require_product_identity_dict
from app.persistence.erp_writer.repository import _get_repository
from app.persistence.erp_writer.text_utils import (
    from_enum_list,
    unmap_ad_purpose,
    unmap_direction_type,
    unmap_operating_mode,
    unmap_product_position,
    unmap_season_type,
    unmap_target_keyword_type,
)
from app.persistence.state_manager import StateManager
from app.models.layers import (
    ConfigSnapshotResponse,
    LongTermConfigResponse,
    LongTermConfigUpdateRequest,
    SaveAllConfigRequest,
)

router = APIRouter()


def get_state_manager() -> StateManager:
    from app.persistence.state_manager import state_manager
    return state_manager


def _empty_snapshot(asin: str) -> ConfigSnapshotResponse:
    return ConfigSnapshotResponse(asin=asin)


def _last_modified(value) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value or "")


def _p3_snapshot(
    *,
    asin: str,
    target_acos: int | None,
    daily_budget: float | None,
    target_acos_manual: bool,
    daily_budget_manual: bool,
) -> dict | None:
    if target_acos is None and daily_budget is None:
        return None
    return {
        "asin": asin,
        "target_acos": {
            "recommended_target": target_acos or 0,
            "reasoning": "",
            "confidence": "medium",
            "manual_override": target_acos_manual,
        },
        "budget_bid": {
            "current": 0.0,
            "suggested": daily_budget or 0.0,
            "direction": "maintain",
            "magnitude_pct": 0.0,
            "reason": "",
            "manual_override": daily_budget_manual,
        },
        "overall_reasoning": "",
        "risk_warnings": [],
        "from_cache": False,
    }


async def _build_config_snapshot(
    asin: str,
    identity: dict,
    sm: StateManager,
) -> ConfigSnapshotResponse:
    row = await asyncio.to_thread(_get_repository().get_agent_config_row, identity)
    if not row:
        return _empty_snapshot(asin)

    target_acos_raw = row.get("target_acos_suggest")
    daily_budget_raw = row.get("daily_budget_suggest")
    target_acos = int(target_acos_raw) if target_acos_raw is not None else None
    daily_budget = float(daily_budget_raw) if daily_budget_raw is not None else None
    state_long_term = sm.get_long_term_config(asin) or {}
    state_target_acos = sm.get_target_acos_override(asin)
    directions = from_enum_list(
        row.get("advert_direction_types"), unmap_direction_type
    )
    return ConfigSnapshotResponse(
        asin=asin,
        product_level=unmap_product_position(row.get("product_position")),
        operating_mode=unmap_operating_mode(row.get("operating_mode")),
        season_stage=unmap_season_type(row.get("season_type")),
        ad_purposes=from_enum_list(row.get("advert_purposes"), unmap_ad_purpose),
        target_keyword_strategy=from_enum_list(
            row.get("target_keyword_types"), unmap_target_keyword_type
        ),
        target_acos=target_acos,
        daily_budget=daily_budget,
        directions=directions,
        p3=_p3_snapshot(
            asin=asin,
            target_acos=target_acos,
            daily_budget=daily_budget,
            target_acos_manual=state_target_acos is not None,
            daily_budget_manual=state_long_term.get("daily_budget_override") is not None,
        ),
        last_modified=_last_modified(row.get("update_time")),
    )


def _require_state_write(ok: bool, operation: str) -> None:
    if not ok:
        raise HTTPException(status_code=500, detail=f"state mirror failed: {operation}")


@router.post(
    "/long-term-config/{asin}/save-all",
    response_model=ConfigSnapshotResponse,
)
async def save_all_config(
    asin: str,
    req: SaveAllConfigRequest,
    sm: StateManager = Depends(get_state_manager),
):
    """以 Agent 配置表为真源，一次写入当前可编辑配置并同步 state。"""
    require_product_identity_dict(req.model_dump(), asin=asin)
    identity = {
        "parent_asin": asin,
        "parent_seller_sku": req.parent_seller_sku,
        "shop_id": req.shop_id,
        "shop_account": req.shop_account,
        "site_code": req.site_code,
        "user_id": req.user_id,
    }
    base = sm.get_long_term_config(asin) or {}
    strategy = {
        "product_position": base.get("product_level"),
        "operating_mode": base.get("operating_mode"),
        "season_type": base.get("season_stage"),
    }
    if any(value is None for value in strategy.values()):
        raise HTTPException(status_code=409, detail="strategy config is incomplete")

    patch = {
        **strategy,
        "advert_purposes": req.ad_purposes,
        "target_keyword_types": req.target_keyword_strategy,
        "advert_direction_types": req.directions,
    }
    if req.target_acos is not None:
        patch["target_acos_suggest"] = req.target_acos
    if req.daily_budget is not None:
        patch["daily_budget_suggest"] = req.daily_budget

    # ERP 单行是配置真源；后续 state 写均为可重试的幂等镜像。
    await asyncio.to_thread(
        _get_repository().upsert_agent_config,
        identity=identity,
        patch=patch,
    )

    long_updates = {
        "shop_id": req.shop_id,
        "parent_seller_sku": req.parent_seller_sku,
        "ad_purposes": req.ad_purposes,
        "target_keyword_strategy": req.target_keyword_strategy,
    }
    if req.daily_budget is not None:
        long_updates["daily_budget_override"] = req.daily_budget
    _require_state_write(
        sm.set_long_term_config(asin, long_updates),
        "long_term_config",
    )
    if req.target_acos is not None:
        _require_state_write(
            sm.set_target_acos_override(
                asin,
                req.target_acos,
                shop_id=req.shop_id,
                parent_seller_sku=req.parent_seller_sku,
            ),
            "target_acos",
        )
    _require_state_write(
        sm.save_execution(
            asin,
            {"selected_directions": req.directions, "sub_options": {}},
            shop_id=req.shop_id,
            parent_seller_sku=req.parent_seller_sku,
        ),
        "execution",
    )
    workflow_state = sm.get_workflow_state(asin) or {}
    if workflow_state.get("current_layer") != "validation":
        _require_state_write(
            sm.advance_layer(
                asin,
                "validation",
                shop_id=req.shop_id,
                parent_seller_sku=req.parent_seller_sku,
            ),
            "advance:validation",
        )
    return await _build_config_snapshot(asin, identity, sm)


@router.get(
    "/long-term-config/{asin}/latest",
    response_model=ConfigSnapshotResponse,
)
async def get_latest_config(
    asin: str,
    shop_id: int | None = None,
    parent_seller_sku: str | None = None,
    sm: StateManager = Depends(get_state_manager),
):
    """读取 C 态编辑控件使用的 Agent 配置快照。"""
    if not asin.strip() or not parent_seller_sku or not shop_id or shop_id <= 0:
        return _empty_snapshot(asin)
    return await _build_config_snapshot(
        asin,
        {
            "parent_asin": asin,
            "parent_seller_sku": parent_seller_sku,
            "shop_id": shop_id,
        },
        sm,
    )


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
        operating_mode=config.get("operating_mode"),
        ad_purposes=config.get("ad_purposes", []),
        target_keyword_strategy=config.get("target_keyword_strategy", []),
        last_modified=config.get("last_modified", ""),
    )


@router.put("/long-term-config/{asin}", response_model=LongTermConfigResponse)
async def update_config(
    asin: str,
    req: LongTermConfigUpdateRequest,
    sm: StateManager = Depends(get_state_manager),
):
    """手动更新ASIN的长期配置"""
    require_product_identity_dict(req.model_dump(), asin=asin)
    updates = {
        "shop_id": req.shop_id,
        "parent_seller_sku": req.parent_seller_sku,
    }
    if req.product_level is not None:
        updates["product_level"] = req.product_level
    # [产品阶段] 前端已用经营模式替代，不再接受 API 更新。
    # if req.product_stage is not None:
    #     updates["product_stage"] = req.product_stage
    if req.season_stage is not None:
        updates["season_stage"] = req.season_stage
    if req.operating_mode is not None:
        updates["operating_mode"] = req.operating_mode.value
    if req.ad_purposes is not None:
        updates["ad_purposes"] = req.ad_purposes
    if req.target_keyword_strategy is not None:
        updates["target_keyword_strategy"] = req.target_keyword_strategy

    sm.set_long_term_config(asin, updates)
    config = sm.get_long_term_config(asin)

    return LongTermConfigResponse(
        asin=asin,
        product_level=config.get("product_level"),
        product_stage=config.get("product_stage"),
        season_stage=config.get("season_stage"),
        operating_mode=config.get("operating_mode"),
        ad_purposes=config.get("ad_purposes", []),
        target_keyword_strategy=config.get("target_keyword_strategy", []),
        last_modified=config.get("last_modified", ""),
    )
