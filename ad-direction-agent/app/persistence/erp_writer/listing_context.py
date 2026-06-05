"""Resolve listing identifiers (shop_id, SKU, site) for ERP writes."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.data.known_listings import KNOWN_LISTINGS
from app.data.mcp_db_context import McpDbContext, resolve_mcp_context_from_db
from .text_utils import normalize_site_code


@dataclass(slots=True)
class ListingContext:
    parent_asin: str
    parent_seller_sku: str
    shop_id: int | None
    shop_account: str
    site_code: str


def _normalize_site(site: str | None) -> str:
    return normalize_site_code(site)


def _lookup_from_erp_summary(parent_asin: str) -> ListingContext | None:
    """Fallback when Doris is unreachable — reuse latest ERP summary row."""
    try:
        import pymysql
        from pymysql.cursors import DictCursor

        conn = pymysql.connect(
            host="192.168.2.51",
            port=3306,
            user="erp_agentadvert",
            password="erp_agentadvert#weilai123",
            database="erp_agentadvert",
            charset="utf8mb4",
            cursorclass=DictCursor,
            connect_timeout=8,
            read_timeout=8,
        )
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT parent_asin, parent_seller_sku, shop_id, site_code
                FROM t_advert_agent_modify_suggest_summary
                WHERE parent_asin=%s AND parent_seller_sku IS NOT NULL
                  AND parent_seller_sku != '' AND parent_seller_sku != 'TEST-PARENT-SKU'
                ORDER BY update_time DESC LIMIT 1
                """,
                (parent_asin,),
            )
            row = cur.fetchone()
        conn.close()
        if not row or not row.get("parent_seller_sku"):
            return None
        return ListingContext(
            parent_asin=row["parent_asin"] or parent_asin,
            parent_seller_sku=row["parent_seller_sku"],
            shop_id=int(row["shop_id"]) if row.get("shop_id") else None,
            shop_account="",
            site_code=_normalize_site(row.get("site_code")),
        )
    except Exception:
        return None


async def resolve_listing_context_async(parent_asin: str) -> ListingContext:
    ctx: McpDbContext | None = await resolve_mcp_context_from_db(parent_asin)
    if ctx and ctx.parent_seller_sku:
        return ListingContext(
            parent_asin=ctx.parent_asin or parent_asin,
            parent_seller_sku=ctx.parent_seller_sku,
            shop_id=int(ctx.shop_id) if ctx.shop_id is not None else None,
            shop_account=ctx.shop_account or "",
            site_code=_normalize_site(ctx.site_code),
        )
    erp_ctx = _lookup_from_erp_summary(parent_asin)
    if erp_ctx:
        return erp_ctx
    known = KNOWN_LISTINGS.get(parent_asin)
    if known:
        sku, shop_id, site, shop_account = known
        return ListingContext(
            parent_asin=parent_asin,
            parent_seller_sku=sku,
            shop_id=shop_id,
            shop_account=shop_account,
            site_code=site,
        )
    if not ctx or not ctx.parent_seller_sku:
        raise RuntimeError(f"无法从 Doris 解析 listing 上下文: asin={parent_asin}")
    return ListingContext(
        parent_asin=ctx.parent_asin or parent_asin,
        parent_seller_sku=ctx.parent_seller_sku,
        shop_id=int(ctx.shop_id) if ctx.shop_id is not None else None,
        shop_account=ctx.shop_account or "",
        site_code=_normalize_site(ctx.site_code),
    )


def resolve_listing_context(parent_asin: str) -> ListingContext:
    return asyncio.run(resolve_listing_context_async(parent_asin))
