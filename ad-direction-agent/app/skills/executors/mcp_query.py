"""mcp-query skill executor — playbook-driven phased fetch."""

from __future__ import annotations

import asyncio
import logging
import time

from app.config.settings import settings
from app.data.doris_fallback import apply_doris_fallback
from app.data.mcp_adapter import McpAdapter, finalize_mcp_asin_data
from app.data.mcp_fetch_run import run_planned_mcp_tools
from app.data.mcp_mapping import META_TO_MCP_TOOLS, McpContext, make_date_window
from app.data.mcp_db_context import resolve_mcp_context_from_db
from app.data.mcp_tool_fallback import BOOTSTRAP_TOOLS
from app.models.asin_data import ASINData
from app.skills.models import SkillPlaybook

logger = logging.getLogger(__name__)
LOG_PREFIX = "[skill:mcp-query]"


class McpQuerySkillExecutor:
    """Execute mcp-query playbook: Doris context → MCP tools → Doris fallback."""

    def __init__(self, playbook: SkillPlaybook):
        self.playbook = playbook

    def _should_prefer_db(self, prefer_db: bool) -> bool:
        if prefer_db and "prefer_db_arg" in self.playbook.prefer_db_when:
            return True
        if settings.data_source == "db" and "data_source_eq_db" in self.playbook.prefer_db_when:
            return True
        return prefer_db or settings.data_source == "db"

    async def run(
        self,
        asin: str,
        meta_filter: list[str] | None = None,
        days: int = 7,
        *,
        prefer_db: bool = False,
    ) -> ASINData:
        if self._should_prefer_db(prefer_db):
            logger.info("%s prefer_db asin=%s", LOG_PREFIX, asin)
            from app.data.db_adapter import DbAdapter

            data = await DbAdapter().fetch_asin_data(asin, meta_filter=meta_filter, days=days)
            data.data_freshness = "fresh"
            data.partial_failures = []
            return data

        ctx_phase = self.playbook.phase("context")
        ctx_timeout = ctx_phase.timeout_seconds if ctx_phase else settings.mcp_context_timeout

        try:
            logger.info("%s phase=context asin=%s timeout=%.0fs", LOG_PREFIX, asin, ctx_timeout)
            db_ctx = await asyncio.wait_for(
                resolve_mcp_context_from_db(asin),
                timeout=ctx_timeout,
            )
        except asyncio.TimeoutError:
            logger.error("%s phase=context timeout asin=%s", LOG_PREFIX, asin)
            return ASINData(asin=asin, data_missing=True, missing_fields=["context"])
        except Exception as e:  # noqa: BLE001
            logger.error("%s phase=context fail asin=%s err=%s", LOG_PREFIX, asin, e)
            return ASINData(asin=asin, data_missing=True, missing_fields=["context"])

        if not db_ctx:
            return ASINData(asin=asin, data_missing=True, missing_fields=["context"])

        start_date, end_date = make_date_window(days, db_ctx.site_code)
        ctx = McpContext(
            parent_asin=db_ctx.parent_asin,
            parent_seller_sku=db_ctx.parent_seller_sku,
            shop_account=db_ctx.shop_account,
            site_code=db_ctx.site_code,
            start_date=start_date,
            end_date=end_date,
        )

        meta_ids = meta_filter or list(META_TO_MCP_TOOLS.keys())
        bootstrap_phase = self.playbook.phase("mcp_bootstrap")
        reports_phase = self.playbook.phase("mcp_reports")

        planned: list[str] = list(bootstrap_phase.tools if bootstrap_phase else BOOTSTRAP_TOOLS)
        if reports_phase and reports_phase.tools_from_meta:
            for meta in meta_ids:
                planned.extend(META_TO_MCP_TOOLS.get(meta, []))
        planned = list(dict.fromkeys(planned))

        logger.info(
            "%s phase=mcp_planned asin=%s meta=%s tools=%s",
            LOG_PREFIX, asin, meta_ids, len(planned),
        )

        adapter = McpAdapter()
        bootstrap_set = set(self.playbook.bootstrap_tools or BOOTSTRAP_TOOLS)
        bootstrap_timeout = (
            bootstrap_phase.timeout_seconds if bootstrap_phase else settings.mcp_bootstrap_timeout
        )
        reports_timeout = (
            reports_phase.timeout_seconds if reports_phase else settings.mcp_tool_timeout
        )
        max_concurrency = max(
            1, bootstrap_phase.concurrency if bootstrap_phase else settings.mcp_max_concurrency,
        )

        payload_map, missing_fields, partial_failures = await run_planned_mcp_tools(
            adapter,
            ctx,
            planned,
            bootstrap_tools=bootstrap_set,
            bootstrap_timeout=bootstrap_timeout,
            reports_timeout=reports_timeout,
            max_concurrency=max_concurrency,
        )
        failed_tools = list(missing_fields)

        data = adapter.assemble_from_payloads(
            asin=asin,
            ctx=ctx,
            payload_map=payload_map,
            missing_fields=missing_fields,
            days=days,
        )

        fb = self.playbook.fallback
        if fb.enabled and fb.on_mcp_failure == "doris":
            data, partial_failures = await apply_doris_fallback(
                asin=asin,
                days=days,
                data=data,
                payload_map=payload_map,
                missing_fields=missing_fields,
                meta_ids=meta_ids,
                partial_failures=partial_failures,
                failed_tools=failed_tools,
                log_prefix=f"{LOG_PREFIX} phase=fallback",
                full_scene_meta=fb.full_scene_meta,
                timeout_seconds=fb.timeout_seconds,
            )

        data = finalize_mcp_asin_data(
            data, payload_map, missing_fields, meta_ids, partial_failures,
        )
        data.partial_failures = list(dict.fromkeys(data.partial_failures))
        if data.partial_failures:
            data.data_freshness = "partial"
        return data
