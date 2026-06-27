"""Resolve MCP tool context (SKU, shop, parent ASIN) from Doris when DATA_SOURCE=mcp."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import pymysql
from pymysql.cursors import DictCursor

from app.config.settings import settings
from app.data.starrocks_retry import run_sync_with_be_retry

logger = logging.getLogger(__name__)

_LOOKUP_SQL = """
    SELECT a.parent_asin,
           a.parent_seller_sku,
           a.shop_id,
           s.account AS shop_account,
           a.site_code,
           a.product_cn_name,
           a.product_name
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
    # 产品名（优先中文名 product_cn_name，否则英文 product_name；写 ERP decision.product_name）
    product_name: str = ""


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

    def _once() -> dict | None:
        # 每次重试用全新连接（勿复用可能已坏的连接）
        conn = pymysql.connect(**kwargs)
        try:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                return cur.fetchone()
        finally:
            conn.close()

    # 套 StarRocks BE 存储错重试（退避+抖动）——这条裸查询不经过 db_adapter._query，
    # 是 MCP 入参解析(#1)与 ERP 写入 listing 上下文(#3)的 SQL 地基，必须自带兜底。
    return run_sync_with_be_retry(_once, label=f"mcp_ctx {asin}")


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
    # product_name 优先用中文名（更短、更人性化），否则回落到亚马逊原 listing 标题。
    # decision.product_name 是 varchar(200)，超长会被 MySQL 截断；这里预先截到 200。
    raw_pn = (row.get("product_cn_name") or row.get("product_name") or "")
    product_name = str(raw_pn).strip()[:200]
    ctx = McpDbContext(
        parent_asin=str(row.get("parent_asin") or asin),
        parent_seller_sku=str(row.get("parent_seller_sku") or ""),
        shop_account=str(row.get("shop_account") or shop_hint),
        shop_id=row.get("shop_id"),
        site_code=site_code,
        product_name=product_name,
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


# ── MCP 上下文解析（新工具 parent_listing_detail，替代 _LOOKUP_SQL）─────────

_MCP_PARENT_KEY_MAP: dict[str, str] = {
    "父ASIN": "parent_asin",
    "父卖家SKU": "parent_seller_sku",
    "店铺ID": "shop_id",
    "店铺账号": "shop_account",
    "站点": "site_code",
    "产品中文名": "product_cn_name",
    "产品名称": "product_name",
}


async def resolve_mcp_context_from_mcp(asin: str, adapter) -> McpDbContext | None:
    """通过 MCP parent_listing_detail 解析上下文（替代 _lookup_sync 的 SQL）。

    adapter 需要 call_tool_timed_with_args；由调用方（McpAdapter / campaign_fetcher）传入。
    失败/超时返 None，调用方走 _lookup_sync DB 回落。
    """
    import json

    try:
        res = await adapter.call_tool_timed_with_args(
            "parent_listing_detail",
            {"parent_asin": asin},
            timeout=getattr(settings, "mcp_context_timeout", 30.0),
        )
        if not res.ok:
            logger.warning("resolve_mcp_context_from_mcp [%s] MCP 失败: %s", asin, res.error)
            return None
        # MCP 响应可能被多包：{content:[{type:"text", text:"{\"success\":true,...}"}]}
        raw = res.value
        if isinstance(raw, dict) and "content" in raw:
            for item in raw["content"]:
                txt = item.get("text", "")
                if isinstance(txt, str):
                    raw = json.loads(txt)
                    break
        if isinstance(raw, dict) and "success" in raw:
            rows = raw.get("rows") or []
            raw = rows[0] if rows else {}
        if not raw or not isinstance(raw, dict):
            return None
        # 中文 key → 英文 key 映射
        mapped = {_MCP_PARENT_KEY_MAP.get(k, k): v for k, v in raw.items()}
        # 产品名：优先中文名
        product_name = str(mapped.pop("product_cn_name", "") or mapped.get("product_name", ""))
        site_code = str(mapped.get("site_code") or settings.mcp_default_site_code or "Amazon_US")
        return McpDbContext(
            parent_asin=str(mapped.get("parent_asin") or asin),
            parent_seller_sku=str(mapped.get("parent_seller_sku") or ""),
            shop_account=str(mapped.get("shop_account") or ""),
            shop_id=_coerce_int(mapped.get("shop_id")),
            site_code=site_code,
            product_name=product_name,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("resolve_mcp_context_from_mcp [%s] 异常: %s", asin, e)
        return None


def lookup_top_child_attrs(parent_asin: str, *, days: int = 30) -> dict | None:
    """找 parent_asin 下"近 N 天广告花费最多"的子 ASIN 的 product_size / product_color。

    返回 {"asin": <child>, "product_size": <str>, "product_color": <str>} 或 None。

    Fallback 链：
      1. 子 ASIN 列表（listing_general 主表，按 parent_asin 找）
      2. ad_product_report 30d cost>0 → ORDER BY SUM(cost) DESC LIMIT 1
      3. 30d 全空 → 试 90d
      4. 还空 → listing_general 取第一条 size+color 都非空的子 ASIN
      5. 全空 → return None
    """
    if not parent_asin:
        return None
    kwargs = _db_connect_kwargs()
    if not kwargs.get("host"):
        return None
    def _once() -> dict | None:
        # 每次重试用全新连接（勿复用可能已坏的连接）
        conn = pymysql.connect(**kwargs)
        try:
            with conn.cursor() as cur:
                # step 1: 子 ASIN 列表
                cur.execute(
                    "SELECT DISTINCT asin FROM dwd_whp_amazon_listing_general "
                    "WHERE parent_asin=%s", (parent_asin,))
                children = [r["asin"] for r in cur.fetchall() if r.get("asin")]
                if not children:
                    return None
                ph = ",".join(["%s"] * len(children))

                # step 2/3: 按 30d/90d 花费排序找 top 1
                top_asin: str | None = None
                for window_days in (days, 90):
                    cur.execute(
                        f"SELECT asin, SUM(cost) AS s FROM dwd_amazon_ad_product_report "
                        f"WHERE asin IN ({ph}) "
                        f"  AND local_report_time >= CURDATE() - INTERVAL %s DAY "
                        f"  AND cost > 0 "
                        f"GROUP BY asin ORDER BY s DESC LIMIT 1",
                        (*children, window_days),
                    )
                    r = cur.fetchone()
                    if r and r.get("asin"):
                        top_asin = r["asin"]
                        break

                # step 4: 仍无 → 第一条 size+color 都非空
                if not top_asin:
                    cur.execute(
                        "SELECT asin, product_size, product_color "
                        "FROM dwd_whp_amazon_listing_general "
                        "WHERE parent_asin=%s "
                        "  AND product_size IS NOT NULL AND product_size != '' "
                        "  AND product_color IS NOT NULL AND product_color != '' "
                        "LIMIT 1",
                        (parent_asin,),
                    )
                    r = cur.fetchone()
                    if r:
                        return {"asin": r["asin"], "product_size": r["product_size"],
                                "product_color": r["product_color"]}
                    return None

                # 拿 top_asin 的 size/color
                cur.execute(
                    "SELECT product_size, product_color FROM dwd_whp_amazon_listing_general "
                    "WHERE asin=%s LIMIT 1", (top_asin,))
                r = cur.fetchone()
                size = (r or {}).get("product_size") or None
                color = (r or {}).get("product_color") or None
                return {"asin": top_asin, "product_size": size, "product_color": color}
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # 套 StarRocks BE 存储错重试（退避+抖动）——与 _lookup_sync 同模块同库统一兜底；
    # 重试用尽 / 非 BE 错（连接失败、SQL 错等）仍 fail-soft 返 None，行为对下游不变。
    try:
        return run_sync_with_be_retry(_once, label=f"top_child {parent_asin}")
    except Exception as e:  # noqa: BLE001
        logger.warning("lookup_top_child_attrs(%s) 失败: %s", parent_asin, e)
        return None

