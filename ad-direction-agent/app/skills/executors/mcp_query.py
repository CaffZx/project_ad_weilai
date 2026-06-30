"""mcp-query skill executor — playbook-driven phased fetch (MCP only)."""

from __future__ import annotations

import asyncio
import logging

from app.config.settings import settings
from app.data.mcp_adapter import McpAdapter, finalize_mcp_asin_data
from app.data.mcp_fetch_run import run_planned_mcp_tools
from app.data.mcp_mapping import BOOTSTRAP_TOOLS, META_TO_MCP_TOOLS, McpContext, make_date_window
from app.data.mcp_normalizers import _as_rows
from app.data.mcp_db_context import resolve_mcp_context_from_mcp
from app.models.asin_data import ASINData
from app.skills.models import SkillPlaybook

logger = logging.getLogger(__name__)
LOG_PREFIX = "[skill:mcp-query]"


class McpQuerySkillExecutor:
    """Execute mcp-query playbook: MCP context → MCP tools."""

    def __init__(self, playbook: SkillPlaybook):
        self.playbook = playbook

    async def run(
        self,
        asin: str,
        meta_filter: list[str] | None = None,
        days: int = 7,
    ) -> ASINData:
        ctx_phase = self.playbook.phase("context")
        ctx_timeout = ctx_phase.timeout_seconds if ctx_phase else settings.mcp_context_timeout

        # MCP 上下文解析
        db_ctx = None
        if settings.mcp_resolve_context:
            try:
                adapter_temp = McpAdapter()
                db_ctx = await asyncio.wait_for(
                    resolve_mcp_context_from_mcp(asin, adapter_temp),
                    timeout=ctx_timeout,
                )
            except (asyncio.TimeoutError, Exception):
                logger.error("%s phase=context fail asin=%s", LOG_PREFIX, asin)
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

        # ── Phase 2: 逐词查自然排名 ──
        _campaign_rows = _as_rows(payload_map.get("ad_campaign_product_keyword_list"))
        _seen: set[str] = set()
        _exact_kws: list[str] = []
        for r in _campaign_rows:
            kw = str(r.get("关键词") or r.get("keyword_text") or r.get("keyword") or "").strip().lower()
            if not kw or kw in _seen:
                continue
            mt = str(r.get("关键词匹配类型") or r.get("match_type") or "").strip().upper()
            if mt != "EXACT":
                continue
            _seen.add(kw)
            _exact_kws.append(kw)
        _RANK_CAP = 50
        if len(_exact_kws) > _RANK_CAP:
            _exact_kws = _exact_kws[:_RANK_CAP]
        if _exact_kws:
            _child_tasks = [
                adapter.call_tool_timed_with_args(
                    "keyword_child_asins",
                    {"keyword": kw, "site_code": ctx.site_code, "parent_asin": ctx.parent_asin,
                     "parent_seller_sku": ctx.parent_seller_sku, "shop_account": ctx.shop_account},
                    getattr(settings, "mcp_rank_timeout", 45.0),
                )
                for kw in _exact_kws
            ]
            _child_results = await asyncio.gather(*_child_tasks, return_exceptions=True)
            _rank_rows: list[dict] = []
            for kw, res in zip(_exact_kws, _child_results):
                if isinstance(res, BaseException) or not getattr(res, "ok", False):
                    continue
                for row in _as_rows(res.value):
                    row["keyword"] = kw
                    _rank_rows.append(row)
            if _rank_rows:
                payload_map["keyword_child_asins"] = _rank_rows
                logger.info("%s Phase2 ranking [%s]: %d rows from %d keywords", LOG_PREFIX, asin, len(_rank_rows), len(_exact_kws))

        data = adapter.assemble_from_payloads(
            asin=asin,
            ctx=ctx,
            payload_map=payload_map,
            missing_fields=missing_fields,
            days=days,
        )

        data = finalize_mcp_asin_data(
            data, payload_map, missing_fields, meta_ids, partial_failures,
        )
        data.partial_failures = list(dict.fromkeys(data.partial_failures))
        if data.partial_failures:
            data.data_freshness = "partial"
        return data
