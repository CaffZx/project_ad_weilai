"""MCP 失败或空表时回落 Doris 数仓。"""

from __future__ import annotations

import asyncio
import logging
from app.config.settings import settings
from app.data.db_sql_helpers import meta_fallback_timeout
from app.data.fallback_merge import (
    merge_db_fallback,
    prune_recovered_partial_failures,
    resolve_db_fallback_metas,
)
from app.data.mcp_empty_reports import empty_report_tools
from app.models.asin_data import ASINData

logger = logging.getLogger(__name__)


async def apply_doris_fallback(
    *,
    asin: str,
    days: int,
    data: ASINData,
    payload_map: dict,
    missing_fields: list[str],
    meta_ids: list[str],
    partial_failures: list[str],
    failed_tools: list[str],
    log_prefix: str = "[doris-fallback]",
    full_scene_meta: bool | None = None,
    timeout_seconds: float | None = None,
) -> tuple[ASINData, list[str]]:
    """
    MCP 工具失败或空表时查 Doris 并合并；数仓补全后剔除对应 partial_failures。
    """
    if not settings.mcp_fallback_to_db:
        return data, list(partial_failures)

    empty_tools = empty_report_tools(data, payload_map, missing_fields, meta_ids)
    fallback_tools = list(dict.fromkeys([*failed_tools, *empty_tools]))
    if not fallback_tools:
        return data, list(partial_failures)

    meta_fb = resolve_db_fallback_metas(
        fallback_tools,
        meta_ids,
        full_scene_meta=full_scene_meta,
    )
    if not meta_fb:
        return data, list(partial_failures)

    fb_timeout = min(
        timeout_seconds if timeout_seconds is not None else settings.db_failover_timeout,
        meta_fallback_timeout(meta_fb),
    )
    reason = []
    if failed_tools:
        reason.append(f"fail={failed_tools}")
    if empty_tools:
        reason.append(f"empty={empty_tools}")
    logger.info(
        "%s asin=%s meta=%s timeout=%.0fs %s",
        log_prefix, asin, meta_fb, fb_timeout, " ".join(reason),
    )

    pf = list(partial_failures)
    try:
        from app.data.db_adapter import DbAdapter

        async def _db_fetch():
            return await DbAdapter().fetch_asin_data(
                asin, meta_filter=meta_fb, days=days,
            )

        db_data = await asyncio.wait_for(_db_fetch(), timeout=fb_timeout)
        data = merge_db_fallback(
            data, db_data, fallback_tools, meta_ids=meta_fb,
        )
        pf = prune_recovered_partial_failures(pf, fallback_tools, db_data)
    except asyncio.TimeoutError:
        logger.warning("%s timeout asin=%s meta=%s", log_prefix, asin, meta_fb)
        pf.append(f"db_fallback:timeout:{','.join(meta_fb)}")
    except Exception as e:  # noqa: BLE001
        logger.warning("%s fail asin=%s err=%s", log_prefix, asin, e)
        pf.append(f"db_fallback:{e}")

    return data, list(dict.fromkeys(pf))
