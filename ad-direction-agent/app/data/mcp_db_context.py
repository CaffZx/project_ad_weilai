"""Resolve MCP tool context (SKU, shop, parent ASIN) from Doris when DATA_SOURCE=mcp."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import pymysql
from pymysql.cursors import DictCursor

from app.config.settings import settings

logger = logging.getLogger(__name__)

_LOOKUP_SQL = """
    SELECT a.parent_asin,
           a.parent_seller_sku,
           a.shop_id,
           s.account AS shop_account,
           a.site_code
    FROM dwd_whp_amazon_listing_general a
    JOIN dwd_shop s ON a.shop_id = s.id
    WHERE (a.parent_asin = %s OR a.asin = %s)
      AND a.parent_seller_sku IS NOT NULL
      AND a.parent_seller_sku != ''
      {shop_clause}
    ORDER BY
      CASE WHEN a.parent_asin = %s THEN 0 ELSE 1 END,
      a.product_price IS NOT NULL DESC,
      a.product_price ASC
    LIMIT 1
"""


@dataclass(frozen=True)
class McpDbContext:
    parent_asin: str
    parent_seller_sku: str
    shop_account: str
    shop_id: int | None = None
    site_code: str = "Amazon_US"


class McpDbContextError(Exception):
    """Raised when listing context cannot be resolved."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _db_connect_kwargs() -> dict:
    host = (settings.mcp_db_host or settings.db_host or "").strip()
    return {
        "host": host,
        "port": settings.db_port,
        "user": settings.db_user,
        "password": settings.db_pass,
        "database": settings.db_database,
        "charset": "utf8mb4",
        "cursorclass": DictCursor,
        "connect_timeout": 10,
        "read_timeout": max(90, int(settings.mcp_context_timeout or 30)),
    }



def _lookup_sync(asin: str, shop_account: str | None) -> dict | None:
    kwargs = _db_connect_kwargs()
    if not kwargs.get("host"):
        return None
    shop_clause = ""
    params: list = [asin, asin, asin]
    if shop_account:
        shop_clause = "AND s.account = %s"
        params.append(shop_account)
    sql = _LOOKUP_SQL.format(shop_clause=shop_clause)
    conn = pymysql.connect(**kwargs)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            return cur.fetchone()
    finally:
        conn.close()


def _coerce_int(v) -> int | None:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


async def resolve_mcp_context_from_db(
    asin: str, override: dict | None = None
) -> McpDbContext | None:
    """解析 MCP 入参。

    override（来自 ERP URL：shop_account/parent_seller_sku/site_code/shop_id）若带
    shop_account（MCP 工具必填的账号字符串）则直接构造，**不查库**——保证用的是运营
    当前在看的店铺，且省一次 dwd_shop JOIN。无 override（如定时跑批无 URL 参数）则保留
    原 Doris 解析路径。
    """
    if override and str(override.get("shop_account") or "").strip():
        ctx = McpDbContext(
            parent_asin=asin,
            parent_seller_sku=str(override.get("parent_seller_sku") or "").strip(),
            shop_account=str(override["shop_account"]).strip(),
            shop_id=_coerce_int(override.get("shop_id")),
            site_code=str(override.get("site_code") or settings.mcp_default_site_code or "Amazon_US"),
        )
        logger.info(
            "MCP 上下文(URL注入) [%s] sku=%s shop=%s site=%s",
            asin, ctx.parent_seller_sku, ctx.shop_account, ctx.site_code,
        )
        return ctx

    if not settings.mcp_resolve_sku_via_db:
        return _context_from_env_only(asin)

    shop_hint = (settings.mcp_default_shop_account or "").strip()
    env_sku = (settings.mcp_default_parent_seller_sku or "").strip()

    if env_sku and shop_hint:
        return McpDbContext(
            parent_asin=asin,
            parent_seller_sku=env_sku,
            shop_account=shop_hint,
            site_code=settings.mcp_default_site_code,
        )

    host = (settings.mcp_db_host or settings.db_host or "").strip()
    if not host:
        logger.warning("MCP 上下文：未配置 db_host / mcp_db_host")
        return None

    try:
        row = await asyncio.to_thread(_lookup_sync, asin, shop_hint or None)
        if not row and shop_hint:
            # 店铺过滤无结果时，再试一次不限店铺（ASIN 可能在其他店）
            row = await asyncio.to_thread(_lookup_sync, asin, None)
    except pymysql.err.OperationalError as e:
        logger.error(
            "MCP 上下文：数据库不可达 host=%s:%s asin=%s err=%s",
            host,
            settings.db_port,
            asin,
            e,
        )
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning("MCP 上下文 DB 查询失败 [%s]: %s", asin, e)
        return None

    if not row:
        logger.warning("MCP 上下文：listing 无记录 asin=%s shop=%s", asin, shop_hint or "*")
        return None

    site_code = str(row.get("site_code") or settings.mcp_default_site_code or "")
    ctx = McpDbContext(
        parent_asin=str(row.get("parent_asin") or asin),
        parent_seller_sku=str(row.get("parent_seller_sku") or ""),
        shop_account=str(row.get("shop_account") or shop_hint),
        shop_id=row.get("shop_id"),
        site_code=site_code,
    )
    logger.info(
        "MCP 上下文(DB) [%s] parent_asin=%s sku=%s shop=%s",
        asin,
        ctx.parent_asin,
        ctx.parent_seller_sku,
        ctx.shop_account,
    )
    return ctx


def _context_from_env_only(asin: str) -> McpDbContext | None:
    sku = (settings.mcp_default_parent_seller_sku or "").strip()
    shop = (settings.mcp_default_shop_account or "").strip()
    if sku and shop:
        return McpDbContext(
            parent_asin=asin,
            parent_seller_sku=sku,
            shop_account=shop,
            site_code=settings.mcp_default_site_code,
        )
    return None


async def resolve_parent_seller_sku(parent_asin: str, shop_account: str) -> str:
    ctx = await resolve_mcp_context_from_db(parent_asin)
    if ctx:
        return ctx.parent_seller_sku
    if settings.mcp_default_parent_seller_sku:
        return settings.mcp_default_parent_seller_sku
    return ""
