"""Fetch data + completeness gate with one automatic refresh retry."""

from __future__ import annotations

import logging

from app.models.asin_data import ASINData
from app.workflow.context import WorkflowContext
from app.workflow.data_contract import (
    CompletenessVerdict,
    ModuleId,
    blocked_message,
    blocked_payload,
    evaluate_completeness,
)

logger = logging.getLogger(__name__)


async def ensure_data_for_llm(
    ctx: WorkflowContext,
    module: ModuleId,
    asin: str,
    *,
    days: int = 7,
    meta_filter: list[str] | None = None,
    refresh: bool = False,
) -> tuple[ASINData, CompletenessVerdict]:
    """Load ASIN data and evaluate contract; retry once with refresh if blocked+retryable."""
    data = await ctx.ensure_data(
        asin,
        refresh=refresh,
        meta_filter=meta_filter,
        days=days,
    )
    verdict = evaluate_completeness(module, data)

    if verdict.status == "blocked" and verdict.retryable and not refresh:
        logger.info(
            "LLM gate [%s] blocked retryable, refreshing data asin=%s missing=%s",
            module,
            asin,
            verdict.missing_required,
        )
        data = await ctx.ensure_data(
            asin,
            refresh=True,
            meta_filter=meta_filter,
            days=days,
        )
        verdict = evaluate_completeness(module, data)

    return data, verdict


def is_llm_blocked(verdict: CompletenessVerdict) -> bool:
    return verdict.status == "blocked"


def llm_blocked_dict(asin: str, verdict: CompletenessVerdict) -> dict:
    return blocked_payload(asin, verdict)
