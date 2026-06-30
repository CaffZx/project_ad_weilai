"""Resolve listing identifiers (shop_id, SKU, site) for ERP writes."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from app.config.settings import settings
from app.data.mcp_db_context import McpDbContext, resolve_mcp_context_from_mcp
from .text_utils import normalize_site_code

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ListingContext:
    parent_asin: str
    parent_seller_sku: str
    shop_id: int | None
    shop_account: str
    site_code: str
    # 产品名（来自 Doris listing.product_cn_name 或 product_name；写 ERP decision.product_name）
    product_name: str = ""


def _normalize_site(site: str | None) -> str:
    return normalize_site_code(site)


def _lookup_from_erp_summary(parent_asin: str) -> ListingContext | None:
    """Fallback when MCP is unreachable — reuse latest ERP summary row."""
    try:
        import pymysql
        from pymysql.cursors import DictCursor

        conn = pymysql.connect(
            host=settings.erp_host,
            port=settings.erp_port,
            user=settings.erp_user,
            password=settings.erp_password,
            database=settings.erp_database,
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
    # MCP 优先（parent_listing_detail） → ERP summary 兜底。
    ctx: McpDbContext | None = None
    if getattr(settings, "mcp_resolve_context", False):
        for attempt in range(2):
            try:
                from app.data.mcp_adapter import McpAdapter
                adapter = McpAdapter()
                ctx = await resolve_mcp_context_from_mcp(parent_asin, adapter)
                if ctx and ctx.parent_seller_sku:
                    break
            except Exception as e:
                logger.warning(
                    "resolve_listing_context MCP failed [%s] attempt=%d: %s, fallback to ERP",
                    parent_asin, attempt + 1, e,
                )
                ctx = None
    if ctx and ctx.parent_seller_sku:
        return ListingContext(
            parent_asin=ctx.parent_asin or parent_asin,
            parent_seller_sku=ctx.parent_seller_sku,
            shop_id=int(ctx.shop_id) if ctx.shop_id is not None else None,
            shop_account=ctx.shop_account or "",
            site_code=_normalize_site(ctx.site_code),
            product_name=getattr(ctx, "product_name", "") or "",
        )
    erp_ctx = _lookup_from_erp_summary(parent_asin)
    if erp_ctx:
        return erp_ctx
    if not ctx or not ctx.parent_seller_sku:
        raise RuntimeError(f"无法解析 listing 上下文: asin={parent_asin}")
    return ListingContext(
        parent_asin=ctx.parent_asin or parent_asin,
        parent_seller_sku=ctx.parent_seller_sku,
        shop_id=int(ctx.shop_id) if ctx.shop_id is not None else None,
        shop_account=ctx.shop_account or "",
        site_code=_normalize_site(ctx.site_code),
        product_name=getattr(ctx, "product_name", "") or "",
    )


def resolve_listing_context(parent_asin: str) -> ListingContext:
    return asyncio.run(resolve_listing_context_async(parent_asin))
