"""决策批次管理 API — context / new-event / cancel-event。

批次 = 一行 t_advert_agent_decision，冻结一份前置 1-4 快照 + 一份执行层结果。
进行中事件落 state 库 analysis_session；ERP decision 行只表示已完成快照。
执行权 = is_latest AND NOT exists(该 ASIN 的 analysis_session)。
"""

from __future__ import annotations

import asyncio
import json
import logging
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

        # 2. 进行中事件标记（state 库，run_id 句柄；不污染 ERP 库）
        sess = state.get_analysis_session(asin)
        in_progress = sess.get("run_id") if sess else None

        # 3. ERP DB 已完成批次列表（容错: DB 不通不崩）
        batches = []
        latest_updated = None
        try:
            repo = _repo()
            batches = repo.list_batches(asin) if repo else []
            if batches:
                latest = next((b for b in batches if b.get("is_latest")), None)
                if latest:
                    latest_updated = latest.get("completed_at")
        except Exception as e:
            logger.warning("ERP DB 连不上，批次查询降级: %s", e)
            return {
                "has_config": has_config,
                "in_progress": in_progress,
                "latest_completed_id": None,
                "latest_completed_updated_at": None,
                "batches": [],
                "degraded": True,
            }

        # 4. 计算 executable = 最新批次 且 无进行中事件
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
    """新建分析事件 → 在 state 库标记进行中（run_id 作批次句柄）。

    不在 ERP 库预建 decision 行——decision_id 由执行层 write_full 落库时生成。
    清空 3-4（target_acos_override + p3 缓存 + execution），保留 1-2 继承。
    返回: { run_id, analysis_mode }
    """
    asin = str(req.get("asin", "")).strip()
    if not asin:
        return {"ok": False, "error": "asin 必填"}

    state = get_state_manager()
    analysis_mode = str(req.get("analysis_mode", "REALTIME")).upper() or "REALTIME"

    # 幂等:已有进行中事件则复用
    existing = state.get_analysis_session(asin)
    if existing and existing.get("run_id"):
        logger.info("复用现存进行中事件 [%s] run_id=%s", asin, existing["run_id"])
        return {"ok": True, "run_id": existing["run_id"],
                "analysis_mode": analysis_mode, "reused": True}

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if not state.set_analysis_session(asin, run_id):
        return {"ok": False, "error": "标记进行中事件失败"}

    # 清 3-4，保留 1-2 继承
    try:
        state.clear_target_acos_override(asin)
        if hasattr(state, "clear_p3_recommendation"):
            state.clear_p3_recommendation(asin)
        wf = state.get_workflow_state(asin) or {}
        execution = dict(wf.get("execution") or {})
        execution["selected_directions"] = None
        wf["execution"] = execution
        state.set_workflow_state(asin, wf)
    except Exception as e:
        logger.warning("清 3-4 失败 [%s]: %s (非阻塞)", asin, e)

    logger.info("新建事件 [%s] run_id=%s mode=%s", asin, run_id, analysis_mode)
    return {"ok": True, "run_id": run_id, "analysis_mode": analysis_mode}


# ── POST /decision/cancel-event ──────────────────────────────────────────


@router.post("/decision/cancel-event")
async def cancel_decision_event(req: dict):
    """放弃进行中分析事件 → 清 state 库标记，旧批次执行权自动恢复。"""
    asin = str(req.get("asin", "")).strip()
    if not asin:
        return {"ok": False, "error": "asin 必填"}
    try:
        get_state_manager().clear_analysis_session(asin)
    except Exception as e:
        logger.exception("清进行中事件失败 [%s]: %s", asin, e)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    logger.info("取消事件 [%s]", asin)
    return {"ok": True}


# ── GET /decision/{id}/preset ────────────────────────────────────────────
# 前置 1-4 配置快照（ERP 码 → 中文 label，供左侧只读渲染）

_POSITION_ZH = {
    "P0_PRODUCT": "战略级产品 (P0)", "P1_PRODUCT": "重点产品 (P1)",
    "P2_PRODUCT": "常规产品 (P2)", "P3_PRODUCT": "长尾产品 (P3)",
    # 旧 3 级英文码兼容（decision.product_position 历史值 TOP/WAIST/LONG_TAIL）
    "TOP": "战略级产品 (P0)", "WAIST": "常规产品 (P2)", "LONG_TAIL": "长尾产品 (P3)",
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


# ── 富快照反译（Tab1 评分卡 / Tab3 ACOS·预算推理），与实时渲染器同形 ──────────
# purpose_score.advert_purpose(ERP 码) → renderTargetScores 期望的 target；
# renderTargetScores 内部 targetMap[s.target]||s.target，给中文也能正确回退显示。
_PURPOSE_TARGET = {"TRAFFIC": "Traffic", "CONVERSION": "Conversion",
                   "RANKING": "Ranking", "PROFIT": "Profit"}
# recommend_tag(direction_detail/ai_suggest) → renderP3 的 confidence 着色（纯视觉）
_TAG_CONF = {"RECOMMENDED": "high", "OPTIONAL": "medium", "NOT_RECOMMENDED": "low"}


def _join_reason(basis, suggest, future) -> str:
    """split_reason_sections 的逆操作：三段带标记拼回，供前端 formatAiSections 展示。"""
    parts = []
    if basis:
        parts.append("【决策依据】" + str(basis))
    if suggest:
        parts.append("【建议】" + str(suggest))
    if future:
        parts.append("【后续关注】" + str(future))
    return "\n".join(parts)


def _translate_tactics_scores(rows: list) -> list:
    """purpose_score 行 → renderTargetScores(scores) 入参（Tab1 评分卡）。"""
    out = []
    for r in rows or []:
        code = (r.get("advert_purpose") or "").upper()
        out.append({
            "target": _PURPOSE_TARGET.get(code, _PURPOSE_ZH.get(code, code)),
            "level": r.get("recommend_level") or "",
            "score": r.get("score"),
            "reason": _join_reason(r.get("decision_basis"), r.get("suggest"),
                                   r.get("future_attention")),
        })
    return out


def _translate_p3_recommend(ai: dict | None) -> dict | None:
    """ai_suggest 行 → renderP3(data) 入参（Tab3）。manual_override=False 走净分支，
    不设 llm_status/data_completeness → isLlmBlocked=False、不弹 dataStatus banner。
    budget 的 current/direction/magnitude 是实时分析态、快照未存 → 降级（前端显示 —）。"""
    if not ai:
        return None
    acos = ai.get("suggest_acos")
    budget = ai.get("suggest_budget")
    risk = ai.get("risk_warning")
    return {
        "target_acos": {
            "recommended_target": float(acos) if acos is not None else None,
            "confidence": _TAG_CONF.get((ai.get("suggest_acos_tag") or "").upper(), "medium"),
            "reasoning": _join_reason(ai.get("suggest_acos_decision_basis"),
                                      ai.get("suggest_acos_suggest"),
                                      ai.get("suggest_acos_future_attention")),
            "manual_override": False,
        },
        "budget_bid": {
            "suggested": float(budget) if budget is not None else None,
            "current": None, "direction": "", "magnitude_pct": "",
            "reason": _join_reason(ai.get("suggest_budget_decision_basis"),
                                   ai.get("suggest_budget_suggest"),
                                   ai.get("suggest_budget_future_attention")),
            "bid_adjustments": [],
            "manual_override": False,
        },
        "overall_reasoning": ai.get("comprehensive_judgment") or "",
        "risk_warnings": [risk] if risk else [],
        "from_cache": False,
    }


# ── Tab4 方向卡反译（direction_recommend_detail → renderDirCard 入参，同形零映射）──


def _content_lines_to_reason(v) -> str:
    """content_json（JSON 字符串数组）→ 用「；」拼回 reason，供前端 formatDirReason 按「；」拆。"""
    if not v:
        return ""
    items = v
    if isinstance(v, str):
        try:
            items = json.loads(v)
        except json.JSONDecodeError:
            items = [v]
    if not isinstance(items, list):
        items = [str(items)]
    return "；".join(str(x).strip() for x in items if str(x).strip())


def _translate_p4_directions(rows: list) -> list:
    """direction_recommend_detail 行 → renderDirCard(d) 入参（Tab4 方向卡）。

    direction_type(ERP码) → id(小写)+label(中文)；recommend_tag(库存小写) → suitability 直用；
    suggest_score → suitability_score；content_json(数组) → reason(；拼)；recommended 由 suitability 派生。
    """
    out = []
    for r in rows or []:
        code = (r.get("direction_type") or "").strip()
        suit = (r.get("recommend_tag") or "available").strip().lower()
        out.append({
            "id": code.lower(),
            "label": _DIRECTION_ZH.get(code, code),
            "suitability": suit,
            "suitability_score": r.get("suggest_score"),
            "reason": _content_lines_to_reason(r.get("content_json")),
            "recommended": suit == "recommended",
        })
    return out


def _translate_p4_summary(main_row: dict | None) -> str:
    """direction_recommend.conclusion_json → recommendation_summary 文本（总述，可空）。"""
    if not main_row or not main_row.get("conclusion_json"):
        return ""
    raw = main_row["conclusion_json"]
    try:
        obj = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return ""
    if isinstance(obj, dict):
        oa = obj.get("overall_analysis")
        if isinstance(oa, list):
            return "\n".join(str(x).strip() for x in oa if str(x).strip())
        if isinstance(oa, str):
            return oa
    return ""


@router.get("/decision/{decision_id}/preset")
async def decision_preset(decision_id: str):
    """读取某批次冻结的前置 1-4 快照（只读展示用）。

    config 标签（strategy/tactics/p3/directions）+ 富段（tactics_scores=Tab1 评分卡、
    p3_recommend=Tab3 ACOS/预算推理、directions_rich=Tab4 方向卡，与实时渲染器同形）。
    富段缺失时为空/None，前端降级。
    """
    if not decision_id:
        return {"ok": False, "error": "decision_id 必填"}
    try:
        repo = _repo()
        snap = await asyncio.to_thread(repo.read_decision_preset_rich, decision_id) if repo else None
        if not snap or not snap.get("decision"):
            return {"ok": False, "error": "批次不存在"}
        out = {"ok": True, **_translate_preset(snap["decision"])}
        out["tactics_scores"] = _translate_tactics_scores(snap.get("purpose_scores") or [])
        out["p3_recommend"] = _translate_p3_recommend(snap.get("ai_suggest"))
        out["directions_rich"] = _translate_p4_directions(snap.get("direction_detail") or [])
        out["directions_summary"] = _translate_p4_summary(snap.get("direction_main"))
        return out
    except Exception as e:
        logger.exception("decision/preset 异常 [%s]: %s", decision_id, e)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
