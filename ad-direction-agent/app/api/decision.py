"""决策批次管理 API — context / new-event / cancel-event。

批次 = 一行 t_advert_agent_decision，冻结一份前置 1-4 快照 + 一份执行层结果。
状态机: DRAFT(进行中) → COMPLETED(已完成) / ARCHIVED(失效)。
执行权 = is_latest AND NOT exists(该 ASIN 的 DRAFT)。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter

from app.config.settings import settings
from app.persistence.state_factory import get_state_manager

router = APIRouter()
logger = logging.getLogger(__name__)


def _repo():
    """延迟导入，避免启动时 ERP 不通炸模块加载。"""
    from app.persistence.erp_writer.repository import _get_repository
    return _get_repository()


# ── GET /decision/context ────────────────────────────────────────────────


@router.get("/decision/context")
async def decision_context(asin: str = "", shopId: str = ""):
    """返回该 ASIN 的批次列表 + 页面三态判定。

    Response:
      { has_config, in_progress, latest_completed_id, latest_completed_updated_at,
        batches: [{decision_id, label, analysis_mode, completed_at, is_latest, executable}],
        degraded?: true }
    DB 不通时返回 degraded=true，不抛 500。
    """
    if not asin:
        return {"has_config": False, "in_progress": None, "latest_completed_id": None,
                "latest_completed_updated_at": None, "batches": [], "degraded": False}

    try:
        # 1. 检查长期配置是否已存在（state DB）
        state = get_state_manager()
        long_term = state.get_long_term_config(asin) or {}
        has_config = bool(long_term.get("product_level") or long_term.get("product_stage"))

        # 2. ERP DB 批次列表 + 草稿（容错: DB 不通不崩）
        repo = None
        batches = []
        in_progress = None
        latest_updated = None
        try:
            repo = _repo()
            batches = repo.list_batches(asin) if repo else []
            in_progress = repo.get_draft(asin) if repo else None
            if in_progress:
                in_progress = in_progress.get("decision_id") if isinstance(in_progress, dict) else in_progress
            if batches:
                latest = next((b for b in batches if b.get("is_latest")), None)
                if latest:
                    latest_updated = latest.get("completed_at")
        except Exception as e:
            logger.warning("ERP DB 连不上，批次查询降级: %s", e)
            return {
                "has_config": has_config,
                "in_progress": None,
                "latest_completed_id": None,
                "latest_completed_updated_at": None,
                "batches": [],
                "degraded": True,
            }

        # 3. 计算 executable
        for b in batches:
            b["executable"] = bool(b.get("is_latest")) and not in_progress

        latest_id = None
        if batches:
            latest = next((b for b in batches if b.get("is_latest")), None)
            if latest:
                latest_id = latest.get("decision_id")

        return {
            "has_config": has_config,
            "in_progress": in_progress,
            "latest_completed_id": latest_id,
            "latest_completed_updated_at": latest_updated,
            "batches": batches,
            "degraded": False,
        }
    except Exception as e:
        logger.exception("decision/context 异常 [%s]: %s", asin, e)
        return {"has_config": False, "in_progress": None, "latest_completed_id": None,
                "latest_completed_updated_at": None, "batches": [], "degraded": True}


# ── POST /decision/new-event ─────────────────────────────────────────────


@router.post("/decision/new-event")
async def new_decision_event(req: dict):
    """新建分析事件 → INSERT DRAFT(REALTIME)。

    清空 3-4（target_acos_override + p3 缓存 + execution），保留 1-2 继承。
    返回: { decision_id, analysis_mode: "REALTIME" }
    """
    asin = str(req.get("asin", "")).strip()
    if not asin:
        return {"ok": False, "error": "asin 必填"}

    # 幂等:已有 DRAFT 则复用，不重复建
    try:
        repo = _repo()
        if repo:
            existing = repo.get_draft(asin)
            if existing:
                did = existing.get("decision_id") if isinstance(existing, dict) else existing
                logger.info("复用现存 DRAFT [%s] decision_id=%s", asin, did)
                return {"ok": True, "decision_id": did, "analysis_mode": existing.get("analysis_mode", "REALTIME"),
                        "reused": True}
    except Exception:
        pass

    decision_id = uuid.uuid4().hex[:32]  # 32-char hex, 与 write_full 的 UUID 口径一致
    batch_no = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M-%S")
    analysis_mode = str(req.get("analysis_mode", "REALTIME")).upper() or "REALTIME"

    try:
        repo = _repo()
        if repo:
            repo.create_draft(asin, decision_id, batch_no, analysis_mode)
    except Exception as e:
        logger.exception("create_draft 失败 [%s]: %s", asin, e)
        return {"ok": False, "error": f"create_draft 失败: {e}"}

    # 清 3-4，保留 1-2 继承
    try:
        state = get_state_manager()
        state.clear_target_acos_override(asin)       # 清 ACOS 手动覆盖
        if hasattr(state, "clear_p3_recommendation"):
            state.clear_p3_recommendation(asin)        # 清 P3 缓存
        wf = state.get_workflow_state(asin) or {}
        execution = dict(wf.get("execution") or {})
        execution["selected_directions"] = None
        wf["execution"] = execution
        state.set_workflow_state(asin, wf)             # ← 正确方法名
    except Exception as e:
        logger.warning("清 3-4 失败 [%s]: %s (非阻塞)", asin, e)

    logger.info("新建事件 [%s] decision_id=%s mode=%s", asin, decision_id, analysis_mode)
    return {"ok": True, "decision_id": decision_id, "analysis_mode": analysis_mode,
            "batch_no": batch_no}


# ── POST /decision/cancel-event ──────────────────────────────────────────


@router.post("/decision/cancel-event")
async def cancel_decision_event(req: dict):
    """放弃实时分析事件 → 删除 DRAFT，旧批次执行权自动恢复。

    仅允许删除 DRAFT 状态，不允许删除 COMPLETED/ARCHIVED。
    """
    asin = str(req.get("asin", "")).strip()
    decision_id = str(req.get("decision_id", "")).strip()
    if not asin or not decision_id:
        return {"ok": False, "error": "asin 和 decision_id 必填"}

    try:
        repo = _repo()
        if repo:
            repo.delete_draft(decision_id)
    except Exception as e:
        logger.exception("delete_draft 失败 [%s] %s: %s", asin, decision_id, e)
        return {"ok": False, "error": f"delete_draft 失败: {e}"}

    logger.info("取消事件 [%s] decision_id=%s", asin, decision_id)
    return {"ok": True}


# ── GET /decision/{id}/preset ────────────────────────────────────────────
# 前置 1-4 配置快照（ERP 码 → 中文 label，供左侧只读渲染）

_POSITION_ZH = {
    "P0_PRODUCT": "战略级产品 (P0)", "P1_PRODUCT": "重点产品 (P1)",
    "P2_PRODUCT": "常规产品 (P2)", "P3_PRODUCT": "长尾产品 (P3)",
}
_STAGE_ZH = {
    "TESTING": "测试期", "PROMOTING": "推进期", "HARVEST_PROFIT": "收割利润期",
    "MAINTAINING": "维持期", "LIQUIDATING": "清货期",
}
_SEASON_ZH = {
    "OFF_SEASON": "淡季", "PEAK_SEASON_PREPARE": "旺季准备",
    "BIG_PEAK_SEASON": "大旺季", "LATE_PEAK_SEASON": "旺季末期",
}
_PURPOSE_ZH = {"TRAFFIC": "引流型", "CONVERSION": "转化型", "RANKING": "排名型", "PROFIT": "盈利型"}
_KWTYPE_ZH = {"GENERIC": "大词", "LONG_TAIL": "长尾词", "COMPETITOR": "竞品词",
              "BRAND": "品牌词", "CUSTOM": "自定义"}
_DIRECTION_ZH = {"PUSH_NATURAL": "推进自然位", "EXPAND_KEYWORDS": "新增扩词",
                 "OPTIMIZE_ACOS": "优化ACOS", "BALANCE_MAINTAIN": "平衡维持"}
_DAYRANGE = {"DAY_7": 7, "DAY_14": 14, "DAY_30": 30}


def _json_list(v) -> list:
    if not v:
        return []
    if isinstance(v, list):
        return v
    s = str(v).strip()
    if s.startswith("["):
        try:
            parsed = json.loads(s)
            return parsed if isinstance(parsed, list) else [s]
        except json.JSONDecodeError:
            pass
    return [x.strip() for x in s.replace("，", ",").split(",") if x.strip()]


def _translate_preset(row: dict) -> dict:
    return {
        "decision_id": row.get("id"),
        "parent_asin": row.get("parent_asin"),
        "analysis_mode": row.get("analysis_mode"),
        "decision_status": row.get("decision_status"),
        "is_latest": bool(row.get("is_latest")),
        "days": _DAYRANGE.get(row.get("day_range"), 7),
        "strategy": {
            "product_level": _POSITION_ZH.get(row.get("product_position"), row.get("product_position")),
            "product_stage": _STAGE_ZH.get(row.get("product_stage"), row.get("product_stage")),
            "season_stage": _SEASON_ZH.get(row.get("season_type"), row.get("season_type")),
        },
        "tactics": {
            "ad_purposes": [_PURPOSE_ZH.get(x, x) for x in _json_list(row.get("advert_purposes"))],
            "target_keyword_strategy": [_KWTYPE_ZH.get(x, x) for x in _json_list(row.get("target_keyword_types"))],
        },
        "p3": {
            "target_acos": row.get("target_acos_suggest"),
            "daily_budget": float(row["daily_budget_suggest"])
            if row.get("daily_budget_suggest") is not None else None,
        },
        "directions": [_DIRECTION_ZH.get(x, x) for x in _json_list(row.get("advert_direction_types"))],
    }


@router.get("/decision/{decision_id}/preset")
async def decision_preset(decision_id: str):
    """读取某批次冻结的前置 1-4 配置快照（只读展示用，ERP 码→中文 label）。"""
    if not decision_id:
        return {"ok": False, "error": "decision_id 必填"}
    try:
        repo = _repo()
        row = await asyncio.to_thread(repo.get_decision_preset, decision_id) if repo else None
        if not row:
            return {"ok": False, "error": "批次不存在"}
        return {"ok": True, **_translate_preset(row)}
    except Exception as e:
        logger.exception("decision/preset 异常 [%s]: %s", decision_id, e)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
