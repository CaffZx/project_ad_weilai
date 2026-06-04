"""Shared MCP tool batch runner (keyword report expansion + merge)."""

from __future__ import annotations

import asyncio
from typing import Any

from app.data.mcp_adapter import McpAdapter
from app.data.mcp_keyword_report import expand_planned_tools, merge_keyword_report_payloads
from app.data.mcp_mapping import McpContext, build_tool_args

# Re-export for tests
__all__ = ["run_planned_mcp_tools"]


async def run_planned_mcp_tools(
    adapter: McpAdapter,
    ctx: McpContext,
    planned: list[str],
    *,
    bootstrap_tools: set[str],
    bootstrap_timeout: float,
    reports_timeout: float,
    max_concurrency: int,
) -> tuple[dict[str, Any], list[str], list[str]]:
    """
    Run planned MCP tools.

    Returns:
        payload_map, missing_fields (hard failures), partial_failures (mcp:tool:error)
    """
    expanded = expand_planned_tools(planned, ctx)
    partial_failures: list[str] = []
    missing_fields: list[str] = []
    payload_map: dict[str, Any] = {}
    kw_payloads: list[Any] = []

    sem = asyncio.Semaphore(max(1, max_concurrency))

    async def _run_one(tool_name: str, extra_args: dict[str, Any] | None) -> tuple[str, dict[str, Any] | None, object]:
        timeout = bootstrap_timeout if tool_name in bootstrap_tools else reports_timeout
        args = build_tool_args(tool_name, ctx)
        if extra_args:
            args = {**args, **extra_args}
        async with sem:
            res = await adapter.call_tool_timed_with_args(tool_name, args, timeout)
        return tool_name, extra_args, res

    results = await asyncio.gather(*[_run_one(name, extra) for name, extra in expanded])
    kw_failed = 0

    for tool_name, extra, res in results:
        if res.ok:
            if tool_name == "ad_keyword_report":
                kw_payloads.append(res.value)
            else:
                payload_map[tool_name] = res.value
        else:
            mt = (extra or {}).get("match_type")
            suffix = f":{mt}" if mt else ""
            partial_failures.append(f"mcp:{tool_name}:{res.error or 'fail'}{suffix}")
            if tool_name == "ad_keyword_report":
                kw_failed += 1
            elif tool_name not in missing_fields:
                missing_fields.append(tool_name)

    if kw_payloads:
        payload_map["ad_keyword_report"] = merge_keyword_report_payloads(kw_payloads)
    elif any(name == "ad_keyword_report" for name, _ in expanded) and kw_failed:
        if "ad_keyword_report" not in missing_fields:
            missing_fields.append("ad_keyword_report")

    return payload_map, missing_fields, partial_failures
