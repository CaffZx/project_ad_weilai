"""核心词判定 API — 离线 endpoint，不串入 /campaign/viewmodel 主链路。

POST /core-keyword/analyze
  入参: {parent_asin, parent_seller_sku, shop_id, target_acos?}
  返回: {ok, task_id, core_count, total_count, error?}
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.config.settings import settings
from app.workflow.steps.core_keyword import run_core_keyword_analysis

logger = logging.getLogger(__name__)
router = APIRouter()


def _erp_repository():
    from app.persistence.erp_writer.repository import ErpDualWriterRepository
    return ErpDualWriterRepository(
        host=settings.erp_host, port=settings.erp_port,
        user=settings.erp_user, password=settings.erp_password,
        database=settings.erp_database, use_tls=settings.erp_use_tls,
    )


@router.get("/core-keyword/management")
async def get_core_keyword_management(
    parent_asin: str,
    parent_seller_sku: str,
    shop_id: int,
) -> dict[str, Any]:
    if not parent_asin.strip() or not parent_seller_sku.strip() or not shop_id:
        return {"ok": False, "error": "缺少必填入参: parent_asin, parent_seller_sku, shop_id"}
    try:
        payload = _erp_repository().list_core_keyword_management(
            parent_asin.strip(), parent_seller_sku.strip(), shop_id,
        )
        return {"ok": True, **payload}
    except Exception as exc:
        logger.warning("读取核心词管理失败 [%s/%s/%s]", parent_asin, parent_seller_sku, shop_id, exc_info=True)
        return {"ok": False, "error": f"读取核心词管理失败: {exc}"}


@router.post("/core-keyword/policy")
async def set_core_keyword_policy(req: dict[str, Any]) -> dict[str, Any]:
    parent_asin = str(req.get("parent_asin") or "").strip()
    parent_seller_sku = str(req.get("parent_seller_sku") or "").strip()
    shop_id = int(req.get("shop_id") or 0)
    if not parent_asin or not parent_seller_sku or not shop_id:
        return {"ok": False, "error": "缺少必填入参: parent_asin, parent_seller_sku, shop_id"}
    try:
        payload = _erp_repository().upsert_core_keyword_policy(
            parent_asin, parent_seller_sku, shop_id,
            str(req.get("keyword_text") or ""), str(req.get("state") or ""),
            str(req.get("expected_task_id") or ""), req.get("expected_task_finished_at"),
            str(req.get("operator") or "") or None,
        )
        return {"ok": True, **payload}
    except RuntimeError as exc:
        if str(exc) == "STALE_TASK":
            return JSONResponse(
                status_code=409,
                content={"ok": False, "error": "核心词任务已更新，请刷新后重试"},
            )
        raise
    except (ValueError, TypeError) as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        logger.warning("写入核心词策略失败", exc_info=True)
        return {"ok": False, "error": f"写入核心词策略失败: {exc}"}


@router.post("/core-keyword/analyze")
async def analyze_core_keywords(req: dict[str, Any]) -> dict[str, Any]:
    parent_asin = str(req.get("parent_asin") or "").strip()
    parent_seller_sku = str(req.get("parent_seller_sku") or "").strip()
    shop_id = int(req.get("shop_id") or 0)
    target_acos = req.get("target_acos")

    if not parent_asin or not parent_seller_sku or not shop_id:
        return {"ok": False, "error": "缺少必填入参: parent_asin, parent_seller_sku, shop_id"}

    analysis = await run_core_keyword_analysis(
        parent_asin=parent_asin,
        parent_seller_sku=parent_seller_sku,
        shop_id=shop_id,
        target_acos=float(target_acos) if target_acos is not None else None,
        triggered_by="manual",
    )

    if not analysis.get("ok"):
        return {"ok": False, "error": analysis.get("error", "unknown")}

    # 落 ERP
    try:
        _erp_repository().write_core_keyword_task(analysis)
    except Exception as e:
        logger.warning("核心词落 ERP 失败 [%s/%s/%s]: %s",
                       parent_asin, parent_seller_sku, shop_id, e, exc_info=True)
        return {"ok": False, "error": f"ERP 写入失败: {e}"}

    return {
        "ok": True,
        "task_id": analysis["task_id"],
        "total_keyword_count": analysis["total_keyword_count"],
        "core_keyword_count": analysis["core_keyword_count"],
    }
