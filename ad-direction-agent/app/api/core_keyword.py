"""核心词判定 API — 离线 endpoint，不串入 /campaign/viewmodel 主链路。

POST /core-keyword/analyze
  入参: {parent_asin, parent_seller_sku, shop_id, target_acos?}
  返回: {ok, task_id, core_count, total_count, error?}
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter

from app.config.settings import settings
from app.workflow.steps.core_keyword import run_core_keyword_analysis

logger = logging.getLogger(__name__)
router = APIRouter()


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
        from app.persistence.erp_writer.repository import ErpDualWriterRepository
        erp = ErpDualWriterRepository(
            host=settings.erp_host, port=settings.erp_port,
            user=settings.erp_user, password=settings.erp_password,
            database=settings.erp_database, use_tls=settings.erp_use_tls,
        )
        erp.write_core_keyword_task(analysis)
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
