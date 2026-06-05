"""Push Campaign analysis + workflow state to ERP (write_full)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Protocol

from app.config.settings import settings
from app.models.campaign import CampaignAnalysisResult

from .listing_context import resolve_listing_context
from .mappers import canonicalize_payload
from .repository import ErpDualWriterRepository, WriteReport
from .text_utils import map_direction_type

logger = logging.getLogger(__name__)


class _StateReader(Protocol):
    def get_long_term_config(self, asin: str) -> dict: ...
    def get_workflow_state(self, asin: str) -> dict: ...
    def get_p3_recommendation(self, asin: str) -> dict | None: ...


def _unwrap_by_days(value: Any, days: int) -> list | dict:
    """workflow_state 字段可能为 list 或 {str(days): ...}."""
    if isinstance(value, dict) and value and all(str(k).isdigit() for k in value.keys()):
        keyed = value.get(str(days))
        if keyed is not None:
            return keyed
        for k in sorted(value.keys(), reverse=True):
            if value[k]:
                return value[k]
        return []
    return value if value is not None else []


def analysis_to_kb_payload(
    result: CampaignAnalysisResult | dict[str, Any],
    *,
    temperature: float | None = None,
) -> dict[str, Any]:
    """将 Campaign 分析结果转为 write_erp / canonicalize 所需的 KB dict。"""
    if isinstance(result, CampaignAnalysisResult):
        data = result.model_dump()
    else:
        data = dict(result)

    run_id = (data.get("run_id") or "").strip()
    experiment_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    adjustments = data.get("adjustments") or []
    if adjustments and hasattr(adjustments[0], "model_dump"):
        adjustments = [a.model_dump() for a in adjustments]

    payload: dict[str, Any] = {
        "experiment_id": experiment_id,
        "run_number": 1,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "parent_asin": data.get("parent_asin") or "",
        "temperature": temperature if temperature is not None else settings.campaign_llm_temperature,
        "total_campaigns": data.get("total_campaigns") or 0,
        "adjustments": adjustments,
        "summary": data.get("summary") or {},
        "warnings": data.get("warnings") or [],
        "sanity_check_passed": data.get("sanity_check_passed", True),
        "llm_rounds_completed": data.get("llm_rounds_completed") or 0,
        "rounds_detail": data.get("rounds_detail") or {},
    }
    if data.get("budget_summary") is not None:
        payload["budget_summary"] = data["budget_summary"]
    return payload


def wizard_payload_from_state(
    asin: str,
    days: int,
    state: _StateReader,
) -> tuple[dict[str, Any], bool]:
    """从工作流状态组装 wizard JSON（不重跑向导 LLM）。返回 (payload, wizard_partial)."""
    long_term = state.get_long_term_config(asin) or {}
    wf = state.get_workflow_state(asin) or {}
    p3 = state.get_p3_recommendation(asin) or {}

    target_scores = _unwrap_by_days(wf.get("target_scores"), days)
    if not isinstance(target_scores, list):
        target_scores = list(target_scores) if target_scores else []

    keyword_analysis = _unwrap_by_days(wf.get("keyword_analysis"), days)
    if not isinstance(keyword_analysis, list):
        keyword_analysis = list(keyword_analysis) if keyword_analysis else []

    execution = wf.get("execution") or {}
    selected = execution.get("selected_directions") or []
    erp_directions = [map_direction_type(d) for d in selected if d]

    ad_purposes = long_term.get("ad_purposes") or []
    target_kw = long_term.get("target_keyword_strategy") or []

    decision_meta = {
        "product_position": long_term.get("product_level"),
        "product_stage": long_term.get("product_stage"),
        "season_type": long_term.get("season_stage"),
        "ad_purposes": ad_purposes,
        "target_keyword_types": target_kw,
        "advert_direction_types": erp_directions,
        "day_range": f"DAY_{days}",
        "p3": p3,
    }

    wizard_partial = not (
        target_scores
        and keyword_analysis
        and p3
        and selected
    )

    return {
        "parent_asin": asin,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "days": days,
        "long_term_config": long_term,
        "target_scores": target_scores,
        "keyword_analysis": keyword_analysis,
        "p3": p3,
        "directions": wf.get("directions") or [],
        "selected_directions": selected,
        "advert_direction_types": erp_directions,
        "decision_meta": decision_meta,
        "analysis": wf.get("analysis") or {},
        "validation_report": wf.get("validation_report") or {},
    }, wizard_partial


def erp_connection_kwargs() -> dict[str, Any]:
    return {
        "host": settings.erp_host,
        "port": settings.erp_port,
        "user": settings.erp_user,
        "password": settings.erp_password,
        "database": settings.erp_database,
    }


def should_push_to_erp(result: CampaignAnalysisResult | dict[str, Any]) -> tuple[bool, str]:
    """门禁：是否满足 ERP 写入条件。返回 (ok, reason)."""
    if isinstance(result, CampaignAnalysisResult):
        data = result.model_dump()
    else:
        data = dict(result)

    if not data.get("sanity_check_passed", True):
        return False, "sanity_check_passed=false"
    adjustments = data.get("adjustments") or []
    if not adjustments:
        return False, "no adjustments"
    has_cid = False
    for adj in adjustments:
        if isinstance(adj, dict):
            cid = str(adj.get("campaign_id") or "").strip()
        else:
            cid = str(getattr(adj, "campaign_id", "") or "").strip()
        if cid:
            has_cid = True
            break
    if not has_cid:
        return False, "no campaign_id on adjustments"
    return True, ""


def _merge_decision_meta(kb_payload: dict[str, Any], wizard_payload: dict[str, Any]) -> dict[str, Any]:
    from .text_utils import normalize_advert_direction_types_list

    decision_meta = dict(wizard_payload.get("decision_meta") or {})
    if wizard_payload.get("long_term_config"):
        lt = wizard_payload["long_term_config"]
        decision_meta.setdefault("product_position", lt.get("product_level"))
        decision_meta.setdefault("product_stage", lt.get("product_stage"))
        decision_meta.setdefault("season_type", lt.get("season_stage"))
        decision_meta.setdefault("ad_purposes", lt.get("ad_purposes"))
        decision_meta.setdefault("target_keyword_types", lt.get("target_keyword_strategy"))
    raw_dirs = (
        decision_meta.get("advert_direction_types")
        or wizard_payload.get("advert_direction_types")
        or wizard_payload.get("selected_directions")
    )
    if raw_dirs:
        decision_meta["advert_direction_types"] = normalize_advert_direction_types_list(
            raw_dirs
        )
    return decision_meta


def push_full_to_erp(
    kb_payload: dict[str, Any],
    wizard_payload: dict[str, Any] | None = None,
    *,
    conn_kwargs: dict[str, Any] | None = None,
) -> WriteReport:
    """resolve listing → canonicalize → write_full（同步，供 asyncio.to_thread 调用）。"""
    asin = (kb_payload.get("parent_asin") or "").strip()
    listing = resolve_listing_context(asin)
    kb_payload = dict(kb_payload)
    kb_payload["shop_id"] = listing.shop_id

    wizard_payload = dict(wizard_payload or {})
    decision_meta = _merge_decision_meta(kb_payload, wizard_payload)
    kb_payload["decision_meta"] = decision_meta
    decision_meta["site_code"] = listing.site_code

    run = canonicalize_payload(
        kb_payload,
        shop_id=listing.shop_id,
        parent_seller_sku=listing.parent_seller_sku,
        site_code=listing.site_code,
    )

    kw = conn_kwargs or erp_connection_kwargs()
    repo = ErpDualWriterRepository(
        host=kw["host"],
        port=int(kw["port"]),
        user=kw["user"],
        password=kw["password"],
        database=kw["database"],
    )
    return repo.write_full(
        run,
        wizard_payload=wizard_payload or None,
        decision_meta=decision_meta,
    )
