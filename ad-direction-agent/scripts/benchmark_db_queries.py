"""Benchmark individual DbAdapter queries (live Doris).

Usage (from ad-direction-agent):
  python scripts/benchmark_db_queries.py [ASIN]
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.data.db_adapter import DbAdapter
from app.data.db_sql_helpers import ListingContext


async def main() -> None:
    asin = sys.argv[1] if len(sys.argv) > 1 else "B0GCZHVBWN"
    days = 7
    adapter = DbAdapter()

    listing = await adapter._resolve_and_fetch_listing(asin)
    if not listing:
        print(f"No listing for {asin}")
        return
    ctx = ListingContext.from_listing_row(listing)
    print(f"ASIN={asin} child_asins={len(ctx.child_asins)} shop_id={ctx.shop_id}")

    cases = [
        ("flow_keywords", lambda: adapter._fetch_flow_keywords(ctx)),
        ("ad_keywords", lambda: adapter._fetch_ad_keywords(ctx, days=days)),
        ("natural_rankings", lambda: adapter._fetch_natural_rankings(ctx, days=days)),
        ("ad_summary", lambda: adapter._fetch_ad_summary(ctx, days=days)),
        ("trend_data", lambda: adapter._fetch_trend_data(ctx.parent_asin, ctx.parent_seller_sku, days=days)),
    ]

    print(f"\n{'Query':<20} {'Status':<8} {'Seconds':>8}  Rows")
    print("-" * 50)
    for name, fn in cases:
        t0 = time.perf_counter()
        try:
            result = await fn()
            sec = time.perf_counter() - t0
            rows = len(result) if isinstance(result, list) else (1 if result else 0)
            print(f"{name:<20} {'OK':<8} {sec:>8.2f}  {rows}")
        except Exception as e:  # noqa: BLE001
            sec = time.perf_counter() - t0
            print(f"{name:<20} {'FAIL':<8} {sec:>8.2f}  {type(e).__name__}: {e}")


if __name__ == "__main__":
    asyncio.run(main())
