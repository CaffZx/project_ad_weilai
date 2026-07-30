"""将既有保存动作非阻塞镜像到未来批跑配置表。"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.persistence.erp_writer.repository import _get_repository


logger = logging.getLogger(__name__)


def _has_complete_identity(identity: dict[str, Any]) -> bool:
    if not str(identity.get("parent_asin") or "").strip():
        return False
    if not str(identity.get("parent_seller_sku") or "").strip():
        return False
    try:
        return int(identity.get("shop_id")) > 0
    except (TypeError, ValueError):
        return False


async def mirror_agent_config(*, req: Any, patch: dict[str, Any], operation: str) -> None:
    """镜像保存结果；失败只记录日志，绝不改变既有 API 的响应语义。"""
    identity = {
        "parent_asin": getattr(req, "asin", None),
        "parent_seller_sku": getattr(req, "parent_seller_sku", None),
        "shop_id": getattr(req, "shop_id", None),
        "shop_account": getattr(req, "shop_account", None),
        "site_code": getattr(req, "site_code", None),
        "user_id": getattr(req, "user_id", None),
        "day_range": "DAY_7",
    }
    if not _has_complete_identity(identity):
        logger.warning(
            "agent config mirror skipped: operation=%s asin=%r shop_id=%r",
            operation,
            identity["parent_asin"],
            identity["shop_id"],
        )
        return

    try:
        await asyncio.to_thread(
            _get_repository().upsert_agent_config,
            identity=identity,
            patch=patch,
        )
        logger.info(
            "agent config mirror saved: operation=%s asin=%s shop_id=%s columns=%s",
            operation,
            identity["parent_asin"],
            identity["shop_id"],
            sorted(patch),
        )
    except Exception:
        logger.exception(
            "agent config mirror failed: operation=%s asin=%s shop_id=%s columns=%s",
            operation,
            identity["parent_asin"],
            identity["shop_id"],
            sorted(patch),
        )
