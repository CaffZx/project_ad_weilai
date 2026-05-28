"""Manual MCP smoke test. Run from ad-direction-agent: python scripts/test_mcp_live.py"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config.settings import settings
from app.data.mcp_client import StreamableHttpMcpInvoker
from app.data.mcp_db_context import resolve_mcp_context_from_db


async def main() -> None:
    asin = sys.argv[1] if len(sys.argv) > 1 else "B0GCZHVBWN"
    ctx = await resolve_mcp_context_from_db(asin)
    print("ctx", ctx)
    if not ctx:
        print("无法从 DB 解析上下文，请检查数据库与 MCP_DEFAULT_SHOP_ACCOUNT")
        return
    inv = StreamableHttpMcpInvoker()
    try:
        rows = await inv.call_tool(
            "listing_basic_info",
            {
                "shop_account": ctx.shop_account,
                "parent_asin": ctx.parent_asin,
                "parent_seller_sku": ctx.parent_seller_sku,
            },
        )
        print("listing rows:", str(rows)[:500])
    finally:
        await inv.aclose()


if __name__ == "__main__":
    asyncio.run(main())
