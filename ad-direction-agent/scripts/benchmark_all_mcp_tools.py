"""Benchmark MCP gateway connectivity and per-tool latency.

Usage (from ad-direction-agent):
  python scripts/benchmark_all_mcp_tools.py [ASIN] [--days 7]

Tests every tool exposed by the gateway (tools/list) plus app-mapped tools.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config.settings import settings
from app.data.mcp_client import McpClientError, StreamableHttpMcpInvoker
from app.data.mcp_db_context import resolve_mcp_context_from_db
from app.data.mcp_mapping import McpContext, build_tool_args, make_date_window
from app.workflow.meta_filters import DEFAULT_META_FILTERS


# Tools the app actually calls (unique)
APP_TOOLS = tuple(
    dict.fromkeys(
        [
            "listing_basic_info",
            "parent_listing_stock_summary",
            "ad_product_report",
            "ad_placement_report",
            "ad_search_term_report",
            "keyword_competitors",
            "keyword_child_asins",
            "flow_keywords",
            "product_sales",
            "direct_competitors",
            "own_keyword_flow",
        ]
    )
)


def _row_count(payload: Any) -> str:
    if payload is None:
        return "0"
    if isinstance(payload, list):
        return str(len(payload))
    if isinstance(payload, dict):
        for key in ("rows", "data", "items", "keywords", "records"):
            if isinstance(payload.get(key), list):
                return str(len(payload[key]))
        return "dict"
    return type(payload).__name__


def _guess_args(tool_name: str, ctx: McpContext) -> dict:
    """Best-effort args for gateway tools not in TOOL_ARG_BUILDERS."""
    built = build_tool_args(tool_name, ctx)
    if built:
        return built
    common = {
        "parent_asin": ctx.parent_asin,
        "parent_seller_sku": ctx.parent_seller_sku,
        "shop_account": ctx.shop_account,
        "start_date": ctx.start_date,
        "end_date": ctx.end_date,
        "site_code": ctx.site_code,
    }
    if tool_name.startswith("product_competitors"):
        return {
            "parent_asin": ctx.parent_asin,
            "shop_account": ctx.shop_account,
            "site_code": ctx.site_code,
        }
    if tool_name == "sales_performance":
        return {
            "parent_asin": ctx.parent_asin,
            "parent_seller_sku": ctx.parent_seller_sku,
            "shop_account": ctx.shop_account,
            "start_date": ctx.start_date,
            "end_date": ctx.end_date,
        }
    if tool_name == "ad_optimization":
        return {
            "parent_asin": ctx.parent_asin,
            "parent_seller_sku": ctx.parent_seller_sku,
            "shop_account": ctx.shop_account,
        }
    if tool_name == "keyword_competitor_flow":
        return {
            "keyword": "test",
            "site_code": ctx.site_code,
            "parent_asin": ctx.parent_asin,
            "parent_seller_sku": ctx.parent_seller_sku,
            "shop_account": ctx.shop_account,
        }
    return {k: v for k, v in common.items() if v not in (None, "")}


async def list_gateway_tools(inv: StreamableHttpMcpInvoker) -> list[str]:
    await inv._ensure_initialized()
    req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {},
    }
    parsed = await inv._post(req)
    tools = (parsed.get("result") or {}).get("tools") or []
    return sorted(t.get("name") for t in tools if t.get("name"))


async def timed_call(
    inv: StreamableHttpMcpInvoker,
    tool_name: str,
    args: dict,
    timeout: float,
) -> tuple[str, float, str, str]:
    t0 = time.perf_counter()
    try:
        result = await asyncio.wait_for(
            inv.call_tool(tool_name, args),
            timeout=timeout,
        )
        elapsed = time.perf_counter() - t0
        return "OK", elapsed, _row_count(result), ""
    except asyncio.TimeoutError:
        elapsed = time.perf_counter() - t0
        return "TIMEOUT", elapsed, "-", f">{timeout}s"
    except McpClientError as e:
        elapsed = time.perf_counter() - t0
        return "MCP_ERR", elapsed, "-", str(e)[:120]
    except Exception as e:  # noqa: BLE001
        elapsed = time.perf_counter() - t0
        return "ERR", elapsed, "-", f"{type(e).__name__}: {e}"[:120]


def _print_table(rows: list[tuple[str, str, float, str, str]]) -> None:
    print()
    print(f"{'Tool':<36} {'Status':<10} {'Seconds':>8}  {'Rows':<8}  Note")
    print("-" * 100)
    for name, status, sec, rows_n, note in rows:
        print(f"{name:<36} {status:<10} {sec:>8.2f}  {rows_n:<8}  {note}")
    ok = sum(1 for r in rows if r[1] == "OK")
    print("-" * 100)
    print(f"Total: {len(rows)} tools | OK: {ok} | Failed/timeout: {len(rows) - ok}")


def _quantile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    k = max(0, min(len(values) - 1, int(round((len(values) - 1) * pct))))
    return sorted(values)[k]


def _print_stats(rows: list[tuple[str, str, float, str, str]]) -> None:
    ok_rows = [r for r in rows if r[1] == "OK"]
    if not ok_rows:
        return
    secs = [r[2] for r in ok_rows]
    print(
        "Latency stats (OK tools): "
        f"min={min(secs):.2f}s p50={_quantile(secs, 0.5):.2f}s "
        f"p95={_quantile(secs, 0.95):.2f}s max={max(secs):.2f}s "
        f"avg={statistics.mean(secs):.2f}s"
    )

    tool_sec = {r[0]: r[2] for r in ok_rows}
    bootstrap = max(tool_sec.get("listing_basic_info", 0.0), tool_sec.get("parent_listing_stock_summary", 0.0))
    for scene, meta_ids in DEFAULT_META_FILTERS.items():
        scene_tools = ["listing_basic_info", "parent_listing_stock_summary"]
        for meta in meta_ids:
            scene_tools.extend({
                "META_AD_PRODUCT": ["ad_product_report"],
                "META_AD_PLACEMENT": ["ad_placement_report"],
                "META_AD_SEARCH_TERM": ["ad_search_term_report"],
                "META_KW_COMPETITOR_RANK": ["keyword_competitors", "keyword_child_asins"],
                "META_KW_SUB_ASIN_RANK": ["keyword_child_asins"],
                "META_FLOW_KEYWORD": ["flow_keywords"],
                "META_TREND": ["product_sales"],
                "META_COMPETITOR": ["direct_competitors"],
            }.get(meta, []))
        unique_tools = sorted(dict.fromkeys(scene_tools))
        phase_b = [t for t in unique_tools if t not in ("listing_basic_info", "parent_listing_stock_summary")]
        estimated = bootstrap + max([tool_sec.get(t, 0.0) for t in phase_b] or [0.0])
        print(
            f"Estimated phased wall-time ({scene}): "
            f"bootstrap={bootstrap:.2f}s + phaseB(max)={estimated-bootstrap:.2f}s => {estimated:.2f}s "
            f"(tools={len(unique_tools)})"
        )


async def main() -> None:
    parser = argparse.ArgumentParser(description="MCP connectivity & latency benchmark")
    parser.add_argument("asin", nargs="?", default="B0GCZHVBWN", help="Test ASIN")
    parser.add_argument("--days", type=int, default=7, help="Date window days")
    parser.add_argument("--timeout", type=float, default=60.0, help="Per-tool timeout (s)")
    parser.add_argument("--app-only", action="store_true", help="Only test app-mapped tools")
    args = parser.parse_args()

    print("=== MCP Benchmark ===")
    print(f"Gateway: {settings.mcp_gateway_url}")
    print(f"ASIN: {args.asin} | days: {args.days} | per-tool timeout: {args.timeout}s")
    print()

    # 1) DB context
    t0 = time.perf_counter()
    db_ctx = await resolve_mcp_context_from_db(args.asin)
    ctx_ms = (time.perf_counter() - t0) * 1000
    if not db_ctx:
        print(f"[FAIL] Doris context resolve ({ctx_ms:.0f}ms): no listing for {args.asin}")
        sys.exit(1)
    start_date, end_date = make_date_window(args.days)
    ctx = McpContext(
        parent_asin=db_ctx.parent_asin,
        parent_seller_sku=db_ctx.parent_seller_sku,
        shop_account=db_ctx.shop_account,
        site_code=db_ctx.site_code,
        start_date=start_date,
        end_date=end_date,
    )
    print(
        f"[OK] Doris context ({ctx_ms:.0f}ms): sku={ctx.parent_seller_sku!r} "
        f"shop={ctx.shop_account} site={ctx.site_code}"
    )
    print(f"     Date window: {start_date} .. {end_date}")

    inv = StreamableHttpMcpInvoker()
    results: list[tuple[str, str, float, str, str]] = []

    try:
        # 2) Initialize / connectivity
        t0 = time.perf_counter()
        await inv._ensure_initialized()
        init_sec = time.perf_counter() - t0
        print(f"[OK] MCP initialize + session ({init_sec:.2f}s) session={inv._session_id[:16]}...")

        # 3) tools/list
        t0 = time.perf_counter()
        try:
            gateway_tools = await list_gateway_tools(inv)
            list_sec = time.perf_counter() - t0
            print(f"[OK] tools/list ({list_sec:.2f}s): {len(gateway_tools)} tools on gateway")
            print("     ", ", ".join(gateway_tools))
        except Exception as e:  # noqa: BLE001
            list_sec = time.perf_counter() - t0
            gateway_tools = list(APP_TOOLS)
            print(f"[WARN] tools/list failed ({list_sec:.2f}s): {e}")
            print(f"       Falling back to app tool list ({len(gateway_tools)} tools)")

        tool_names = list(APP_TOOLS) if args.app_only else gateway_tools
        if not args.app_only:
            # ensure app tools included even if list missed any
            for t in APP_TOOLS:
                if t not in tool_names:
                    tool_names.append(t)

        print()
        print(f"--- Per-tool calls (sequential, timeout={args.timeout}s) ---")

        for tool_name in tool_names:
            tool_args = _guess_args(tool_name, ctx)
            status, sec, rows_n, note = await timed_call(
                inv, tool_name, tool_args, args.timeout
            )
            arg_preview = json.dumps(tool_args, ensure_ascii=False)
            if len(arg_preview) > 80:
                arg_preview = arg_preview[:77] + "..."
            if status != "OK" and not note:
                note = arg_preview
            elif status == "OK":
                note = arg_preview
            results.append((tool_name, status, sec, rows_n, note))
            print(f"  ... {tool_name}: {status} {sec:.2f}s", flush=True)

        _print_table(results)
        _print_stats(results)

    finally:
        await inv.aclose()


if __name__ == "__main__":
    asyncio.run(main())
