"""Print timeout budget and optionally probe MCP tools (no HTTP).

Usage:
  python scripts/diagnose_timeout_layers.py [ASIN]
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config.settings import settings
from app.data.mcp_mapping import BOOTSTRAP_TOOLS as BT, META_TO_MCP_TOOLS
from app.skills.loader import load_playbook
from app.workflow.meta_filters import get_meta_filter


def _section(title: str) -> None:
    print(f"\n=== {title} ===")


def print_config() -> None:
    _section("应用超时配置 (.env / settings)")
    rows = [
        ("DATA_SOURCE", settings.data_source),
        ("SKILLS_ENABLED", getattr(settings, "skills_enabled", False)),
        ("MCP_CONTEXT_TIMEOUT", settings.mcp_context_timeout),
        ("MCP_BOOTSTRAP_TIMEOUT", settings.mcp_bootstrap_timeout),
        ("MCP_TOOL_TIMEOUT", settings.mcp_tool_timeout),
        ("MCP_TIMEOUT (httpx inner)", settings.mcp_timeout),
        ("MCP_MAX_CONCURRENCY", settings.mcp_max_concurrency),
        ("MCP_FAILOVER_FULL_DB", getattr(settings, "mcp_failover_full_db", None)),
        ("DB_FAILOVER_TIMEOUT", getattr(settings, "db_failover_timeout", None)),
        ("DB_FETCH_TIMEOUT", settings.db_fetch_timeout),
        ("Demo callAPI default", "90s (tactics/strategy)"),
        ("Demo diagnosis refresh", "180s"),
    ]
    for k, v in rows:
        print(f"  {k}: {v}")

    try:
        pb = load_playbook("mcp-query")
        _section("playbook.yaml 解析结果")
        for p in pb.phases:
            print(f"  phase={p.id} timeout={p.timeout_seconds}s concurrency={p.concurrency} tools={p.tools or ('meta_expand' if p.tools_from_meta else '')}")
        print(f"  fallback enabled={pb.fallback.enabled} full_scene={pb.fallback.full_scene_meta} timeout={pb.fallback.timeout_seconds}s")
    except Exception as e:  # noqa: BLE001
        print(f"  playbook load failed: {e}")


def print_wall_clock_estimate() -> None:
    _section("战术层 (tactics) 最坏墙钟估算")
    meta = get_meta_filter("tactics")
    tools = list(BT)
    for m in meta:
        tools.extend(META_TO_MCP_TOOLS.get(m, []))
    tools = list(dict.fromkeys(tools))
    print(f"  meta_filter: {meta}")
    print(f"  MCP tools ({len(tools)}): {tools}")
    # Parallel batches with concurrency 4
    conc = max(1, settings.mcp_max_concurrency)
    bootstrap = [t for t in tools if t in BT]
    reports = [t for t in tools if t not in BT]
    boot_sec = settings.mcp_bootstrap_timeout
    rep_sec = settings.mcp_tool_timeout
    import math
    boot_batches = math.ceil(len(bootstrap) / conc) if bootstrap else 0
    rep_batches = math.ceil(len(reports) / conc) if reports else 0
    mcp_worst = settings.mcp_context_timeout + boot_batches * boot_sec + rep_batches * rep_sec
    db_worst = float(getattr(settings, "db_failover_timeout", 300))
    print(f"  MCP 阶段(全失败仍等满): context + bootstrap_batches({boot_batches})*{boot_sec}s + report_batches({rep_batches})*{rep_sec}s ≈ {mcp_worst:.0f}s")
    print(f"  Doris 整场景回落(若 MCP 有失败): +{db_worst:.0f}s")
    print(f"  合计最坏 ≈ {mcp_worst + db_worst:.0f}s  >> Demo 90s → 页面会先超时")
    _section("战略页 strategy/options")
    print("  接口立即 200，但后台 preload 会拉全量 meta（无 meta_filter）→ 日志里 Doris 慢查询多来自此处")


async def probe_mcp(asin: str) -> None:
    _section(f"MCP 单工具探测 ASIN={asin}")
    from app.data.mcp_db_context import resolve_mcp_context_from_db
    from app.data.mcp_adapter import McpAdapter
    from app.data.mcp_mapping import McpContext, make_date_window

    t0 = time.perf_counter()
    try:
        ctx_row = await asyncio.wait_for(
            resolve_mcp_context_from_db(asin),
            timeout=settings.mcp_context_timeout,
        )
    except asyncio.TimeoutError:
        print(f"  context: TIMEOUT after {settings.mcp_context_timeout}s")
        return
    except Exception as e:  # noqa: BLE001
        print(f"  context: FAIL {e}")
        return
    print(f"  context: OK {time.perf_counter() - t0:.2f}s shop={getattr(ctx_row, 'shop_account', '')}")

    if not ctx_row:
        print("  context: empty")
        return

    start, end = make_date_window(7)
    ctx = McpContext(
        parent_asin=ctx_row.parent_asin,
        parent_seller_sku=ctx_row.parent_seller_sku,
        shop_account=ctx_row.shop_account,
        site_code=ctx_row.site_code,
        start_date=start,
        end_date=end,
    )
    adapter = McpAdapter()
    for tool in ("parent_listing_stock_summary",):
        limit = (
            settings.mcp_bootstrap_timeout if tool in BT else settings.mcp_tool_timeout
        )
        t1 = time.perf_counter()
        res = await adapter.call_tool_timed(tool, ctx, limit)
        sec = time.perf_counter() - t1
        status = "OK" if res.ok else f"FAIL {res.error}"
        print(f"  {tool}: {status} elapsed={sec:.2f}s (limit={limit}s)")


def main() -> None:
    asin = sys.argv[1] if len(sys.argv) > 1 else "B0GCZHVBWN"
    print_config()
    print_wall_clock_estimate()
    if os.environ.get("PROBE_MCP", "1") == "1":
        asyncio.run(probe_mcp(asin))


if __name__ == "__main__":
    main()
