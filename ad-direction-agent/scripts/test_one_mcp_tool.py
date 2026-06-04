import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.data.mcp_client import StreamableHttpMcpInvoker
from app.data.mcp_db_context import resolve_mcp_context_from_db


async def main() -> None:
    ctx = await resolve_mcp_context_from_db("B0GCZHVBWN")
    if not ctx:
        print("no ctx")
        return
    sku = ctx.parent_seller_sku
    print("sku repr:", repr(sku))
    inv = StreamableHttpMcpInvoker()
    try:
        rows = await inv.call_tool(
            "listing_basic_info",
            {
                "shop_account": ctx.shop_account,
                "parent_asin": ctx.parent_asin,
                "parent_seller_sku": sku,
            },
        )
        print("ok", json.dumps(rows, ensure_ascii=False)[:500])
    except Exception as e:
        print("fail", type(e).__name__, e)
    finally:
        await inv.aclose()


if __name__ == "__main__":
    asyncio.run(main())
