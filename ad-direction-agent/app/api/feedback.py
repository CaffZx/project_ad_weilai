"""反馈收集 + 导出 API"""

import json
import logging
from pathlib import Path

from fastapi import APIRouter, Response

from app.models.feedback import FeedbackSubmission, FeedbackListResponse

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/feedback", response_model=dict)
async def submit_feedback(submission: FeedbackSubmission):
    """提交反馈日志"""
    from datetime import datetime, timezone

    # 生成 ID
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    submission.submitted_at = datetime.now(timezone.utc).isoformat()
    submission.id = f"{ts}_{submission.parent_asin}"

    # 落盘
    from app.persistence.state_manager import state_manager
    saved = state_manager.save_feedback(submission)

    if saved:
        logger.info("反馈已保存 [%s]: %d 个模块", submission.id, len(submission.modules))
        return {"ok": True, "id": submission.id, "message": "感谢反馈，持续优化中，祝工作顺利！"}
    else:
        return {"ok": False, "message": "反馈保存失败，请稍后重试"}


@router.get("/feedback/list", response_model=FeedbackListResponse)
async def list_feedback(asin: str):
    """列出某 ASIN 的所有反馈记录"""
    from app.persistence.state_manager import state_manager
    items = state_manager.list_feedback(asin)
    return FeedbackListResponse(asin=asin, total=len(items), items=items)


@router.get("/feedback/export")
async def export_feedback(asin: str | None = None, format: str = "json"):
    """导出反馈日志

    - format=json: 返回合并 JSON 数组
    - asin: 可选筛选单个 ASIN，不传则导出全部
    """
    from app.persistence.state_manager import state_manager
    items = state_manager.export_feedback(asin)

    content = json.dumps(items, ensure_ascii=False, indent=2)

    filename = f"feedback_export_{'all' if asin is None else asin}.json"
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Type": "application/json; charset=utf-8",
    }
    return Response(content=content, media_type="application/json", headers=headers)
