"""Doris 数仓适配器 — 从数据仓库拉取 ASIN 全量数据"""

import asyncio
import logging
import random
import time
from datetime import datetime, timezone

import pymysql
from dbutils.pooled_db import PooledDB
from pymysql.cursors import DictCursor

from app.config.settings import settings
from app.data.base import DataSourceAdapter
from app.data.db_sql_helpers import ListingContext, build_in_clause
from app.data.template_registry import list_all
from app.models.asin_data import ASINData, AdData, KeywordData, CompetitorData, SpecialSignals, TrendPoint

logger = logging.getLogger(__name__)

def _fetch_timeout() -> float:
    return max(30.0, float(getattr(settings, "db_fetch_timeout", 75.0)))
SLOW_QUERY_THRESHOLD = 5  # 慢查询告警阈值（秒）

# StarRocks 共享存储 BE 故障的重试参数（仅针对 starlet/BE 存储错误，不含 SQL 语法错）
STARROCKS_BE_RETRIES = 2            # 额外重试 2 次（共 3 次尝试）
STARROCKS_BE_BACKOFF = (0.5, 1.0)  # 指数退避基值(秒)：第1次重试0.5s、第2次1.0s，另叠加 50–200ms 抖动


def _is_starrocks_be_storage_error(e: BaseException) -> bool:
    """是否为 StarRocks 共享存储 BE 故障（只读文件系统 / cache 目录分配失败等）。

    这类错误回来是 errno 1064 的 ProgrammingError，但带 starlet/BE: 签名；
    与真正的 SQL 语法错误（同为 1064）区分——后者不应重试。
    """
    if not isinstance(e, pymysql.err.ProgrammingError):
        return False
    args = getattr(e, "args", ())
    if not args or args[0] != 1064:
        return False
    msg = str(e)
    return "starlet err" in msg or "BE:1006" in msg


# ── 元脚本 → DbAdapter 方法映射 ──────────────────────
META_TO_METHOD: dict[str, str] = {
    "META_KW_AD": "ad_keywords",
    "META_COMPETITOR": "competitors",
    "META_KW_COMPETITOR_RANK": "natural_rankings",
    "META_KW_SUB_ASIN_RANK": "natural_rankings",
    "META_FLOW_KEYWORD": "flow_keywords",
    "META_AD_PRODUCT": "ad_summary",
    "META_AD_PLACEMENT": "placement_summary",
    "META_TREND": "trend_data",
}


class DbAdapter(DataSourceAdapter):
    """从 Doris 数仓读取 ASIN 数据的适配器

    使用 DBUtils PooledDB 连接池，每次查询从池获取独立连接。
    同一 to_thread 内完成「取连接→执行→归还」，PyMySQL 连接不跨线程。
    """

    def __init__(self):
        read_timeout = int(_fetch_timeout()) + 5
        self._pool = PooledDB(
            creator=pymysql,
            # mincached=0：避免启动时批量建连；DB 不可达时不会拖垮进程初始化
            mincached=0, maxcached=10, maxconnections=20,
            maxusage=1000,
            ping=1,
            host=settings.db_host,
            port=settings.db_port,
            user=settings.db_user,
            password=settings.db_pass,
            database=settings.db_database,
            cursorclass=DictCursor,
            connect_timeout=10,
            read_timeout=read_timeout,
        )
        self._query_sem = asyncio.Semaphore(8)
        self._warmup()

    def _warmup(self):
        """预热连接池，避免首个请求冷启动 ~15s 延迟。
        DB 不可达时静默跳过，运行时各查询自行重试/降级。"""
        try:
            conns = [self._pool.connection() for _ in range(8)]
            for c in conns:
                c.close()
        except Exception:
            logger.warning("连接池预热失败，首个请求将承受冷启动延迟")

    # ── 查询基础 ─────────────────────────────────────────

    async def _query(
        self,
        sql: str,
        params: tuple = (),
        *,
        timeout: float | None = None,
        label: str = "",
    ) -> list[dict]:
        """执行查询，返回字典列表。每次从池获取独立连接，整段在同一线程完成。"""
        def _do_query():
            conn = self._pool.connection()
            closed = False
            try:
                cur = conn.cursor()
                try:
                    cur.execute(sql, params)
                    return cur.fetchall()
                finally:
                    cur.close()
            except (pymysql.err.OperationalError, pymysql.err.InterfaceError):
                try:
                    conn.close()
                except Exception:
                    pass
                closed = True
                conn = self._pool.connection()
                try:
                    cur = conn.cursor()
                    try:
                        cur.execute(sql, params)
                        return cur.fetchall()
                    finally:
                        cur.close()
                finally:
                    conn.close()
            finally:
                if not closed:
                    conn.close()

        timeout_s = timeout if timeout is not None else _fetch_timeout()
        t0 = time.monotonic()
        tag = f"[{label}] " if label else ""
        async with self._query_sem:
            for attempt in range(STARROCKS_BE_RETRIES + 1):   # 0,1,2 → 共 3 次
                try:
                    result = await asyncio.wait_for(
                        asyncio.to_thread(_do_query),
                        timeout=timeout_s,
                    )
                    elapsed = time.monotonic() - t0
                    if elapsed > SLOW_QUERY_THRESHOLD:
                        logger.warning("%s慢查询 %.2fs rows=%s: %s", tag, elapsed, len(result), sql[:200])
                    return result
                except asyncio.TimeoutError:
                    logger.error("%sDB 查询超时 (%.0fs): %s...", tag, timeout_s, sql[:120])
                    raise                                      # 超时不在本次需求内，保持直接抛
                except Exception as e:
                    if attempt < STARROCKS_BE_RETRIES and _is_starrocks_be_storage_error(e):
                        base = STARROCKS_BE_BACKOFF[min(attempt, len(STARROCKS_BE_BACKOFF) - 1)]
                        delay = base + random.uniform(0.05, 0.20)
                        logger.warning(
                            "%sStarRocks BE 存储错误，%.0fms 后重试 (第 %d/%d 次): %s",
                            tag, delay * 1000, attempt + 1, STARROCKS_BE_RETRIES, str(e)[:160],
                        )
                        await asyncio.sleep(delay)
                        continue
                    raise                                      # 非 BE 存储错 / 重试用尽 → 抛

    async def _query_one(
        self,
        sql: str,
        params: tuple = (),
        *,
        timeout: float | None = None,
        label: str = "",
    ) -> dict | None:
        rows = await self._query(sql, params, timeout=timeout, label=label)
        return rows[0] if rows else None

    # ── 入口 ─────────────────────────────────────────────

    async def fetch_asin_data(self, asin: str, meta_filter: list[str] | None = None,
                              days: int = 7) -> ASINData:
        """获取 ASIN 全量数据

        meta_filter: 元脚本ID列表，指定需要查询的数据维度。
                     None 或空列表表示查询全部（向后兼容）。
        days: 数据时间窗口（7/14/30），默认 7 天。
        """
        try:
            data = await self._fetch_all(asin, meta_filter=meta_filter, days=days)
            return data
        except Exception as e:
            logger.error("DbAdapter 查询失败 [%s]: %s", asin, e)
            return ASINData(
                asin=asin,
                data_missing=True,
                missing_fields=["all"],
            )

    def _should_fetch(self, meta_id: str, meta_filter: list[str] | None) -> bool:
        """判断某个元脚本是否需要执行"""
        if not meta_filter:
            return True
        method_name = META_TO_METHOD.get(meta_id)
        return method_name is not None and any(
            META_TO_METHOD.get(m) == method_name for m in meta_filter
        )

    async def _fetch_all(self, parent_asin: str,
                         meta_filter: list[str] | None = None,
                         days: int = 7) -> ASINData:
        """并行拉取数据并组装（按 meta_filter 选择性拉取）"""
        # 一次查询获取 context + listing 子 ASIN 列表（合并 _resolve_asin_context + _fetch_listing）
        listing = await self._resolve_and_fetch_listing(parent_asin)
        if not listing:
            return ASINData(asin=parent_asin, data_missing=True, missing_fields=["asin_not_found"])

        listing_ctx = ListingContext.from_listing_row(listing)
        shop_id = listing_ctx.shop_id
        parent_seller_sku = listing_ctx.parent_seller_sku

        # 按 meta_filter 选择性执行各维度查询
        ad_task = (self._fetch_ad_summary(listing_ctx, days=days)
                   if self._should_fetch("META_AD_PRODUCT", meta_filter) else None)
        placement_task = (self._fetch_placement_summary(listing_ctx, days=days)
                          if self._should_fetch("META_AD_PLACEMENT", meta_filter) else None)
        kw_task = (self._fetch_ad_keywords(listing_ctx, days=days)
                   if self._should_fetch("META_KW_AD", meta_filter) else None)
        nat_task = (self._fetch_natural_rankings(listing_ctx, days=days)
                    if (self._should_fetch("META_KW_COMPETITOR_RANK", meta_filter)
                        or self._should_fetch("META_KW_SUB_ASIN_RANK", meta_filter)) else None)
        comp_task = (self._fetch_competitors(parent_asin, shop_id)
                     if self._should_fetch("META_COMPETITOR", meta_filter) else None)
        flow_task = (self._fetch_flow_keywords(listing_ctx)
                     if self._should_fetch("META_FLOW_KEYWORD", meta_filter) else None)
        profit_task = self._fetch_gross_profit(parent_asin, parent_seller_sku, days=days)
        trend_task = (self._fetch_trend_data(parent_asin, parent_seller_sku, days=days)
                      if self._should_fetch("META_TREND", meta_filter) else None)

        # 并行执行所有无依赖的查询
        coros = {
            "ad": ad_task, "placement": placement_task,
            "kw": kw_task, "nat": nat_task,
            "comp": comp_task, "flow": flow_task,
            "profit": profit_task, "trend": trend_task,
        }
        valid = {k: v for k, v in coros.items() if v is not None}
        results = await asyncio.gather(*valid.values(), return_exceptions=True)
        rmap = {}
        for key, val in zip(valid.keys(), results):
            if not isinstance(val, BaseException):
                rmap[key] = val
            else:
                logger.warning("并行查询 %s 失败: %s", key, val)

        ad_summary       = rmap.get("ad")
        placement_summary = rmap.get("placement")
        keywords         = rmap.get("kw", [])
        rankings         = rmap.get("nat", [])
        competitors_list = rmap.get("comp", [])
        flow_kws         = rmap.get("flow", [])
        gross_profit     = rmap.get("profit")
        trend_rows       = rmap.get("trend", [])

        # 组装 ASINData
        data = ASINData(asin=parent_asin)

        # 基础数据
        data.sku = listing.get("seller_sku", "")
        data.parent_asin = parent_asin
        data.price = _float(listing.get("product_price"))
        # 毛利率：从 dwd_az_asin_gross_profit 的 gross_profit_amount_proportion 加权平均
        margin_val = _float(gross_profit.get("margin")) if gross_profit else None
        if margin_val is not None:
            data.margin = margin_val
        # 无毛利数据时用 child 级 single_gross_profit（几乎全为 NULL，仅作兜底）
        else:
            data.margin = _float(listing.get("single_gross_profit"))
        data.avg_daily_sales_30d = _float(listing.get("avg_daily"))
        data.rating = _float(listing.get("star_level")) or _float(listing.get("craw_asin_star"))
        data.review_count = _int(listing.get("comment_num"))
        data.product_level = listing.get("product_grade")
        data.product_stage = listing.get("progress_preparation")
        # DB 值可能为旧枚举（7值），映射为新 5 值
        from app.models.layers import STAGE_OLD_TO_NEW
        data.product_stage = STAGE_OLD_TO_NEW.get(data.product_stage, data.product_stage)
        from app.models.layers import LEVEL_OLD_TO_NEW
        data.product_level = LEVEL_OLD_TO_NEW.get(data.product_level, data.product_level)
        data.season_stage = listing.get("seasonality")
        data.brand = listing.get("brand")
        data.product_line = listing.get("product_line")
        data.category_name = listing.get("category_name")

        # 产品上架天数
        launch_date = listing.get("product_site_launch_date")
        if launch_date and isinstance(launch_date, datetime):
            data.days_since_launch = (datetime.now(timezone.utc).date() - launch_date.date()).days

        # 扩展字段
        data.refund_rate = _float(listing.get("refund_rate"))

        # 广告数据（含placement拆分）
        if ad_summary:
            ad_kwargs = {
                "acos": _float(ad_summary.get("acos")),
                "cpc": _float(ad_summary.get("cpc")),
                "ctr": _float(ad_summary.get("ctr")),
                "cvr": _float(ad_summary.get("cvr")),
                "impressions": _int(ad_summary.get("impressions")),
                "clicks": _int(ad_summary.get("clicks")),
                "spend": _float(ad_summary.get("cost")),
                "sales": _float(ad_summary.get("sale")),
                "orders": _int(ad_summary.get("units_order")),
                "daily_budget": _float(ad_summary.get("campaign_budget")),
            }
            # TACOS = 广告花费 / 总销售额（窗口来自 dwd_az_asin_gross_profit，由 days 参数控制）
            if gross_profit:
                total_sales_window = _float(gross_profit.get("total_sales"))
                total_ad_cost_window = _float(gross_profit.get("total_ad_cost"))
                if total_sales_window is not None and total_ad_cost_window is not None and total_sales_window > 0:
                    ad_kwargs["tacos"] = round(total_ad_cost_window / total_sales_window * 100, 1)
                if total_sales_window is not None and total_sales_window > 0:
                    daily_ad_cost = (total_ad_cost_window or 0) / days
                    daily_total_sale = total_sales_window / days
                    ad_kwargs["daily_ad_spend_ratio"] = round(daily_ad_cost / daily_total_sale * 100, 1)
            # 精准/非精准拆分（按 match_type，来自 keyword_report）
            if placement_summary:
                ad_kwargs.update(placement_summary)
            data.ad_data = AdData(**ad_kwargs)

        # 关键词数据 — 合并排名信息
        ranking_map = {r["keyword"]: r for r in rankings}
        for kw in keywords:
            if kw.get("keyword_text"):
                kd = KeywordData(
                    keyword=str(kw["keyword_text"]),
                    impressions=_int(kw.get("impressions")),
                    clicks=_int(kw.get("clicks")),
                    spend=_float(kw.get("cost")),
                    orders=_int(kw.get("units_order")),
                    acos=_float(kw.get("acos")),
                    cvr=_float(kw.get("cvr")),
                    bid=_float(kw.get("keyword_bid")),
                    match_type=str(kw.get("match_type", "")),
                )
                # 填充自然排名
                rank_info = ranking_map.get(kd.keyword)
                if rank_info:
                    kd.natural_rank = _int(rank_info.get("craw_nature_rank"))
                    kd.near_natural_rank = _int(rank_info.get("near_craw_nature_rank"))
                    kd.sp_rank = _int(rank_info.get("craw_sp_rank"))
                    near = kd.near_natural_rank
                    cur = kd.natural_rank
                    if near is not None and cur is not None:
                        kd.rank_change_14d = near - cur  # 正数=排名上升

                data.keywords.append(kd)

        # 按 match_type 计算精准(EXACT)/非精准(BROAD+PHRASE) ACOS和CPC
        if data.keywords:
            exact_cost = sum(kw.spend for kw in data.keywords if kw.match_type.upper() == "EXACT")
            exact_sale = sum(kw.spend / kw.acos * 100 for kw in data.keywords
                             if kw.match_type.upper() == "EXACT" and kw.acos and kw.acos > 0)
            exact_clicks = sum(kw.clicks for kw in data.keywords if kw.match_type.upper() == "EXACT")
            broad_cost = sum(kw.spend for kw in data.keywords if kw.match_type.upper() in ("BROAD", "PHRASE"))
            broad_sale = sum(kw.spend / kw.acos * 100 for kw in data.keywords
                             if kw.match_type.upper() in ("BROAD", "PHRASE") and kw.acos and kw.acos > 0)
            broad_clicks = sum(kw.clicks for kw in data.keywords if kw.match_type.upper() in ("BROAD", "PHRASE"))
            total_cost = exact_cost + broad_cost

            if exact_sale and data.ad_data:
                data.ad_data.precision_acos = round(exact_cost / exact_sale * 100, 1)
            if exact_clicks and data.ad_data:
                data.ad_data.precision_cpc = round(exact_cost / exact_clicks, 2)
            if broad_sale and data.ad_data:
                data.ad_data.broad_acos = round(broad_cost / broad_sale * 100, 1)
            if broad_clicks and data.ad_data:
                data.ad_data.broad_cpc = round(broad_cost / broad_clicks, 2)
            if total_cost and data.ad_data:
                data.ad_data.precision_spend_ratio = round(exact_cost / total_cost * 100, 1) if exact_cost else 0
                data.ad_data.broad_spend_ratio = round(broad_cost / total_cost * 100, 1) if broad_cost else 0

        data.keyword_count = len(data.keywords)

        # 高花费词：补充近/前半窗 ACOS、花费（趋势选词）
        if data.keywords and kw_task is not None:
            top_by_spend = sorted(data.keywords, key=lambda k: k.spend or 0, reverse=True)[:40]
            kw_texts = [k.keyword for k in top_by_spend if k.keyword]
            try:
                splits = await self._fetch_keyword_period_splits(
                    listing_ctx, kw_texts, days=days,
                )
                for kd in top_by_spend:
                    sp = splits.get(kd.keyword) or {}
                    kd.acos_recent = sp.get("acos_recent")
                    kd.acos_prior = sp.get("acos_prior")
                    kd.spend_recent = sp.get("spend_recent")
                    kd.spend_prior = sp.get("spend_prior")
                    kd.orders_recent = sp.get("orders_recent")
                    kd.orders_prior = sp.get("orders_prior")
            except Exception as e:
                logger.warning("关键词时间窗拆分查询失败: %s", e)

        # 可用新词
        ad_keywords_set = {kw.get("keyword_text") for kw in keywords}
        new_kws = [fk for fk in flow_kws if fk.get("keyword") not in ad_keywords_set]
        data.available_new_keywords = len(new_kws)
        new_kws_ranked = sorted(
            new_kws,
            key=lambda fk: (_float(fk.get("top_convert_ratio")) or 0, _float(fk.get("searches")) or 0),
            reverse=True,
        )[:10]
        data.expand_keyword_candidates = [
            {
                "keyword": fk.get("keyword"),
                "searches": _int(fk.get("searches")),
                "convert_ratio": _float(fk.get("top_convert_ratio")),
            }
            for fk in new_kws_ranked
            if fk.get("keyword")
        ]

        # 竞品数据
        for comp in competitors_list:
            data.competitors.append(CompetitorData(
                asin=str(comp.get("asin", "")),
                price=_float(comp.get("price")),
                rating=_float(comp.get("asin_star")),
                review_count=_int(comp.get("reviews_num")),
                bsr=_int(comp.get("top_category_rank")),
            ))

        # 自然订单占比 = (总订单 - 广告订单) / 总订单（从 gross_profit 按 days 窗口聚合）
        gp_total_orders = _float(gross_profit.get("total_orders")) if gross_profit else None
        gp_ad_orders = _float(gross_profit.get("ad_orders")) if gross_profit else None
        if gp_total_orders and gp_ad_orders is not None and gp_total_orders > 0:
            data.natural_order_ratio = max(0, (gp_total_orders - gp_ad_orders) / gp_total_orders * 100)

        # 库存口径完全统一为 MCP listing_inventory 的「FBA可售」：仅用 listing 各子ASIN
        # can_sale_num(可售)之和（= mcp_adapter 的 sum(FBA可售)，同口径、同为当前快照、无窗口、无兜底）。
        # 不用「在库」in_stock_num（口径不同），也不回退 gross_profit.stock（同列但带 7 天窗口易过期，
        # 且会把"真实售罄=0"误回退成陈旧正值，制造幻影库存）。
        data.signals = SpecialSignals(
            inventory_qty=_int(listing.get("can_sale_num")),
            in_transit_inventory=_int(listing.get("in_stock_receiving_num")),
        )

        # 趋势数据（按日聚合）
        for row in trend_rows:
            row_orders = _float(row.get("orders", 0)) or 0
            row_ad_orders = _float(row.get("ad_orders", 0)) or 0
            row_clicks = _float(row.get("clicks", 0)) or 0
            row_impressions = _float(row.get("impressions", 0)) or 0
            row_spend = _float(row.get("spend", 0)) or 0
            row_ad_sales = _float(row.get("ad_sales", 0)) or 0
            data.trend.append(TrendPoint(
                date=str(row.get("date", "")),
                acos=round(row_spend / row_ad_sales * 100, 1) if row_ad_sales else None,
                cvr=round(row_ad_orders / row_clicks * 100, 1) if row_clicks else None,
                ctr=round(row_clicks / row_impressions * 100, 1) if row_impressions else None,
                cpc=round(row_spend / row_clicks, 2) if row_clicks else None,
                orders=int(row_orders),
                ad_orders=int(row_ad_orders),
                spend=round(row_spend, 2),
            ))

        return data

    # ── 查询方法 ──────────────────────────────────────────

    async def resolve_mcp_context(
        self,
        asin: str,
        shop_account: str | None = None,
    ) -> dict | None:
        """从 listing 表解析 MCP 工具所需上下文（parent_seller_sku / 店铺 / 父 ASIN）。

        支持传入父 ASIN 或子 ASIN；优先匹配 parent_asin，与 _resolve_and_fetch_listing 排序一致。
        """
        hint = (shop_account or settings.mcp_default_shop_account or "").strip()
        params: list = [asin, asin, asin]
        shop_clause = ""
        if hint:
            shop_clause = "AND s.account = %s"
            params.append(hint)
        rows = await self._query(
            f"""
            SELECT a.parent_asin,
                   a.parent_seller_sku,
                   a.shop_id,
                   s.account AS shop_account
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
            """,
            tuple(params),
        )
        if not rows:
            return None
        row = rows[0]
        return {
            "parent_asin": str(row.get("parent_asin") or asin),
            "parent_seller_sku": str(row.get("parent_seller_sku") or ""),
            "shop_account": str(row.get("shop_account") or hint),
            "shop_id": row.get("shop_id"),
        }

    async def _resolve_and_fetch_listing(self, parent_asin: str) -> dict | None:
        """一次查询获取 ASIN context（shop_id/account/sku）+ 所有子 ASIN listing 数据。

        合并了原 _resolve_asin_context 和 _fetch_listing，省一次网络往返。
        """
        children = await self._query("""
            SELECT a.asin, a.seller_sku, a.parent_seller_sku,
                   a.shop_id, s.account AS shop_account,
                   a.product_price, a.in_stock_num,
                   a.seven_sale_num, a.month_sale_num,
                   a.product_line, a.brand, a.category_name, a.parent_asin,
                   e.star_level, e.comment_num, e.single_gross_profit,
                   e.refund_rate, e.craw_asin_star,
                   e.progress_preparation, e.seasonality, e.product_grade,
                   e.follow_up, a.seven_ad_cost, e.stock_inventory,
                   a.buy_box_price, a.product_site_launch_date,
                   a.seven_ad_sale_money, a.seven_order_num,
                   a.month_order_num, a.can_sale_num,
                   a.in_stock_receiving_num, a.in_stock_shipped_num,
                   a.in_stock_working_num, a.three_ad_cost,
                   a.three_ad_sale_money, a.product_type,
                   a.near_day_ad_order_num
            FROM dwd_whp_amazon_listing_general a
            JOIN dwd_shop s ON a.shop_id = s.id
            LEFT JOIN dwd_whp_az_extend e ON a.shop_id = e.shop_id
                AND a.asin = e.asin AND a.seller_sku = e.seller_sku
                AND e.follow_up = 'YES_FOLLOW_UP'
            WHERE a.parent_asin = %s
            ORDER BY a.product_price IS NOT NULL DESC, a.product_price ASC
        """, (parent_asin,))

        if not children:
            return None

        # 去重：当 az_extend 有多个 follow_up='YES_FOLLOW_UP' 记录时，
        # LEFT JOIN 会产生重复行，按 (asin, seller_sku) 保留第一条
        seen = set()
        deduped = []
        for c in children:
            key = (c.get("asin"), c.get("seller_sku"))
            if key not in seen:
                seen.add(key)
                deduped.append(c)
        if len(deduped) < len(children):
            logger.debug(
                "_resolve_and_fetch_listing [%s]: deduped %d -> %d rows",
                parent_asin, len(children), len(deduped),
            )
        children = deduped

        # 以第一个子 ASIN 为基准，浅拷贝父级共享字段
        main = dict(children[0])
        main["child_asins"] = [(c["asin"], c["seller_sku"]) for c in children]
        main["child_asins_follow_up"] = [
            c["asin"] for c in children
            if c.get("asin") and c.get("follow_up") == "YES_FOLLOW_UP"
        ]
        if not main["child_asins_follow_up"]:
            main["child_asins_follow_up"] = [p[0] for p in main["child_asins"] if p[0]]
        # shop_id / parent_seller_sku 从首行提取（同一 parent_asin 下所有行一致）
        main["shop_id"] = children[0].get("shop_id")

        # ── 数值字段：对所有子 ASIN 做 SUM 聚合 ──
        # 库存
        main["in_stock_num"] = sum(_int(c.get("in_stock_num", 0)) or 0 for c in children)
        main["in_stock_receiving_num"] = sum(_int(c.get("in_stock_receiving_num", 0)) or 0 for c in children)
        main["in_stock_shipped_num"] = sum(_int(c.get("in_stock_shipped_num", 0)) or 0 for c in children)
        main["in_stock_working_num"] = sum(_int(c.get("in_stock_working_num", 0)) or 0 for c in children)
        main["can_sale_num"] = sum(_int(c.get("can_sale_num", 0)) or 0 for c in children)
        main["stock_inventory"] = sum(_int(c.get("stock_inventory", 0)) or 0 for c in children)

        # 销量/订单
        main["seven_sale_num"] = sum(_float(c.get("seven_sale_num", 0)) or 0 for c in children)
        main["month_sale_num"] = sum(_float(c.get("month_sale_num", 0)) or 0 for c in children)
        main["seven_order_num"] = sum(_float(c.get("seven_order_num", 0)) or 0 for c in children)
        main["month_order_num"] = sum(_float(c.get("month_order_num", 0)) or 0 for c in children)
        main["near_day_ad_order_num"] = sum(_float(c.get("near_day_ad_order_num", 0)) or 0 for c in children)

        # 广告花费
        main["seven_ad_cost"] = sum(_float(c.get("seven_ad_cost", 0)) or 0 for c in children)
        main["seven_ad_sale_money"] = sum(_float(c.get("seven_ad_sale_money", 0)) or 0 for c in children)
        main["three_ad_cost"] = sum(_float(c.get("three_ad_cost", 0)) or 0 for c in children)
        main["three_ad_sale_money"] = sum(_float(c.get("three_ad_sale_money", 0)) or 0 for c in children)

        # 评价数
        main["comment_num"] = sum(_int(c.get("comment_num", 0)) or 0 for c in children)

        # ── 特殊处理字段 ──
        # 价格：优先取 buy_box_price（黄金购物车价）
        main["buy_box_price"] = next(
            (c.get("buy_box_price") for c in children if c.get("buy_box_price") is not None), None
        )
        main["product_price"] = (main["buy_box_price"]
                                 or next((c.get("product_price") for c in children
                                          if c.get("product_price") is not None), None))

        # 退款率：按月销量加权平均
        total_sales_weight = sum(_float(c.get("month_sale_num", 0)) or 0 for c in children)
        if total_sales_weight > 0:
            main["refund_rate"] = sum(
                (_float(c.get("refund_rate", 0)) or 0) * (_float(c.get("month_sale_num", 0)) or 0)
                for c in children
            ) / total_sales_weight

        # 评分：按7天销量加权平均
        star_pairs = [(_float(c.get("star_level")), _float(c.get("seven_sale_num", 0)) or 0)
                      for c in children]
        total_star_weight = sum(p[1] for p in star_pairs)
        if total_star_weight > 0:
            valid_stars = [(s, w) for s, w in star_pairs if s is not None]
            if valid_stars:
                main["star_level"] = sum(s * w for s, w in valid_stars) / total_star_weight

        # 上架日期：取最早的
        launch_dates = [c.get("product_site_launch_date") for c in children
                        if c.get("product_site_launch_date") is not None]
        if launch_dates:
            main["product_site_launch_date"] = min(launch_dates)

        # 日均销量（用聚合后的 seven_sale_num）
        total_sales_7d = main["seven_sale_num"]
        if total_sales_7d:
            main["avg_daily"] = total_sales_7d / 7

        return main

    async def _fetch_ad_summary(self, listing_ctx: ListingContext, days: int = 7) -> dict | None:
        """汇总广告商品级报告 — metrics 和 campaign_budget 拆为两个并行查询"""
        if not listing_ctx.parent_asin or not listing_ctx.parent_seller_sku:
            return None
        if not listing_ctx.child_asins:
            return None

        in_sql, in_params = build_in_clause("t.asin", listing_ctx.child_asins)
        details_params = (listing_ctx.shop_id, days, *in_params)
        budget_params = (listing_ctx.shop_id, days, *in_params)

        metrics_sql = f"""
            SELECT SUM(COALESCE(t.cost,0)) AS cost,
                   SUM(COALESCE(t.sale,0)) AS sale,
                   SUM(COALESCE(t.clicks,0)) AS clicks,
                   SUM(COALESCE(t.impressions,0)) AS impressions,
                   SUM(COALESCE(t.units_order,0)) AS units_order
            FROM dwd_amazon_ad_product_report_update t
            WHERE t.shop_id = %s
              AND t.LOCAL_REPORT_TIME >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
              AND t.LOCAL_REPORT_TIME < CURDATE()
              AND {in_sql}
        """

        budget_sql = f"""
            SELECT SUM(cb) AS campaign_budget FROM (
                SELECT DISTINCT campaign_id, campaign_budget AS cb
                FROM dwd_amazon_ad_product_report_update
                WHERE shop_id = %s
                  AND LOCAL_REPORT_TIME >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
                  AND LOCAL_REPORT_TIME < CURDATE()
                  AND asin IN ({",".join(["%s"] * len(listing_ctx.child_asins))})
                  AND campaign_budget IS NOT NULL
            ) u
        """

        metrics_task = asyncio.to_thread(self._do_query_one, metrics_sql, details_params)
        budget_task = asyncio.to_thread(self._do_query_one, budget_sql, budget_params)

        metrics, budget = await asyncio.gather(metrics_task, budget_task, return_exceptions=True)

        if isinstance(metrics, BaseException):
            logger.warning("ad_summary metrics 查询失败: %s", metrics)
            metrics = None
        if isinstance(budget, BaseException):
            logger.warning("ad_summary budget 查询失败: %s", budget)
            budget = None

        row = metrics
        if not row or row.get("cost") is None:
            return None

        cost = _float(row["cost"])
        sale = _float(row["sale"])
        clicks = _float(row["clicks"])
        impressions = _float(row["impressions"])
        orders = _float(row["units_order"])

        return {
            "cost": cost,
            "sale": sale,
            "clicks": clicks,
            "impressions": impressions,
            "units_order": orders,
            "acos": (cost / sale * 100) if sale else None,
            "cpc": (cost / clicks) if clicks else None,
            "ctr": (clicks / impressions * 100) if impressions else None,
            "cvr": (orders / clicks * 100) if clicks else None,
            "campaign_budget": _float(budget.get("campaign_budget")) if budget else None,
        }

    async def _fetch_placement_summary(self, listing_ctx: ListingContext, days: int = 7) -> dict | None:
        """获取 placement 维度的广告位数据（TOS vs ROS）"""
        if not listing_ctx.child_asins:
            return None

        in_sql, in_params = build_in_clause("p.asin", listing_ctx.child_asins)
        rows = await self._query(
            f"""
            SELECT t.placement,
                   SUM(COALESCE(t.cost,0)) AS cost,
                   SUM(COALESCE(t.sale,0)) AS sale,
                   SUM(COALESCE(t.clicks,0)) AS clicks,
                   SUM(COALESCE(t.impressions,0)) AS impressions,
                   SUM(COALESCE(t.units_order,0)) AS units_order
            FROM dwd_amazon_ad_placement_report_update t
            INNER JOIN dwd_amazon_ad_product p
                ON t.CAMPAIGN_ID = p.CAMPAIGN_ID
               AND t.shop_id = p.shop_id
               AND p.state = 'enabled'
            WHERE t.shop_id = %s
              AND t.LOCAL_REPORT_TIME >= DATE_SUB(NOW(), INTERVAL %s DAY)
              AND {in_sql}
            GROUP BY t.placement
            """,
            (listing_ctx.shop_id, days, *in_params),
            label="placement_summary",
        )

        if not rows:
            return None

        # 按 placement 分组：TOS = Top of Search, ROS = Rest of Search
        total_cost = 0
        tos = {"cost": 0, "sale": 0, "clicks": 0}
        ros = {"cost": 0, "sale": 0, "clicks": 0}
        for r in rows:
            placement_type = str(r.get("placement", "")).upper()
            cost = _float(r.get("cost", 0)) or 0
            sale = _float(r.get("sale", 0)) or 0
            clicks = _float(r.get("clicks", 0)) or 0
            total_cost += cost
            if "TOS" in placement_type:
                tos["cost"] += cost
                tos["sale"] += sale
                tos["clicks"] += clicks
            else:
                ros["cost"] += cost
                ros["sale"] += sale
                ros["clicks"] += clicks

        result = {}
        # TOS ACOS/CPC
        if tos["sale"]:
            result["placement_tos_acos"] = tos["cost"] / tos["sale"] * 100
        if tos["clicks"]:
            result["placement_tos_cpc"] = tos["cost"] / tos["clicks"]
        # ROS ACOS/CPC
        if ros["sale"]:
            result["placement_ros_acos"] = ros["cost"] / ros["sale"] * 100
        if ros["clicks"]:
            result["placement_ros_cpc"] = ros["cost"] / ros["clicks"]
        # 花费占比
        if total_cost:
            result["placement_tos_spend_ratio"] = tos["cost"] / total_cost * 100
            result["placement_ros_spend_ratio"] = ros["cost"] / total_cost * 100

        return result

    async def _fetch_ad_keywords(self, listing_ctx: ListingContext, days: int = 7) -> list[dict]:
        """获取关键词级广告报告（campaign/keyword 状态过滤）"""
        if not listing_ctx.child_asins:
            return []

        in_sql, in_params = build_in_clause("daap.asin", listing_ctx.child_asins)
        rows = await self._query(
            f"""
            SELECT daak.keyword_text, daak.match_type,
                   SUM(COALESCE(daak.clicks,0)) AS clicks,
                   SUM(COALESCE(daak.cost,0)) AS cost,
                   SUM(COALESCE(daak.impressions,0)) AS impressions,
                   SUM(COALESCE(daak.sale,0)) AS sale,
                   SUM(COALESCE(daak.units_order,0)) AS units_order,
                   MAX_BY(daak.keyword_bid,
                          CASE WHEN daak.keyword_bid IS NOT NULL
                               THEN CONCAT(CAST(daak.local_report_time AS CHAR), '|',
                                           CAST(COALESCE(daak.create_time, daak.local_report_time) AS CHAR))
                          END) AS keyword_bid
            FROM dwd_amazon_ad_keyword_report daak
            INNER JOIN dwd_amazon_ad_product daap
                ON daap.campaign_id = daak.campaign_id
               AND daap.shop_id = daak.shop_id
            WHERE daak.keyword_text IS NOT NULL
              AND daak.campaign_status = 'ENABLED'
              AND daak.keyword_status = 'ENABLED'
              AND daap.shop_id = %s
              AND daak.LOCAL_REPORT_TIME >= DATE_SUB(NOW(), INTERVAL %s DAY)
              AND {in_sql}
            GROUP BY daak.keyword_text, daak.match_type
            ORDER BY SUM(COALESCE(daak.cost,0)) DESC
            LIMIT 200
            """,
            (listing_ctx.shop_id, days, *in_params),
            label="ad_keywords",
        )

        # 计算每个关键词的ACOS和CVR
        for r in rows:
            cost = _float(r.get("cost"))
            sale = _float(r.get("sale"))
            clicks = _float(r.get("clicks"))
            orders = _float(r.get("units_order"))
            r["acos"] = (cost / sale * 100) if sale else None
            r["cvr"] = (orders / clicks * 100) if clicks else None

        return rows

    # ── Campaign 分析数据源 ─────────────────────────────

    async def _fetch_campaign_context(self, listing_ctx: ListingContext) -> list[dict]:
        """① Doris 轻量上下文查询 — 仅维度字段，不取指标。

        返回: [{campaign_name, campaign_id, keyword_id, child_asin, seller_sku,
                keyword_text, match_type, campaign_status, keyword_status}, ...]
        注: keyword_bid 不再从此处取 —— current_bid 改由 MCP ad_campaign_basic_info
            的「关键词BID」提供（campaign_fetcher._assemble），Doris 不再作 bid 来源。
        """
        if not listing_ctx.child_asins:
            return []

        in_sql, in_params = build_in_clause("daap.asin", listing_ctx.child_asins)
        rows = await self._query(
            f"""
            SELECT daak.campaign_name, daak.campaign_id, daak.keyword_id,
                   daap.asin AS child_asin, daap.seller_sku,
                   daak.keyword_text, daak.match_type,
                   'ENABLED' AS campaign_status, 'ENABLED' AS keyword_status
            FROM dwd_amazon_ad_keyword_report daak
            INNER JOIN dwd_amazon_ad_product daap
                ON daap.campaign_id = daak.campaign_id
               AND daap.shop_id = daak.shop_id
            WHERE daap.shop_id = %s
              AND daak.campaign_status = 'ENABLED'
              AND daak.keyword_status = 'ENABLED'
              AND daak.local_report_time >= DATE_SUB(NOW(), INTERVAL 7 DAY)
              AND {in_sql}
            GROUP BY daak.campaign_name, daak.campaign_id, daak.keyword_id,
                     daap.asin, daap.seller_sku,
                     daak.keyword_text, daak.match_type
            ORDER BY daak.campaign_name, daak.keyword_text
            """,
            (listing_ctx.shop_id, *in_params),
            timeout=getattr(settings, "campaign_discovery_timeout", 90.0),
            label="campaign_context",
        )
        return rows

    async def _fetch_campaign_perf_from_db(
        self, campaign_name: str, shop_id: int, days: int = 7,
    ) -> dict | None:
        """MCP product_report 回落 — 按 campaign_name 聚合指标。

        返回: {cost, sale, clicks, impressions, orders, acos, cpc, ctr, cvr}
        """
        row = await self._query_one(
            """
            SELECT SUM(COALESCE(cost, 0)) AS cost,
                   SUM(COALESCE(sale, 0)) AS sale,
                   SUM(COALESCE(clicks, 0)) AS clicks,
                   SUM(COALESCE(impressions, 0)) AS impressions,
                   SUM(COALESCE(units_order, 0)) AS orders
            FROM dwd_amazon_ad_keyword_report
            WHERE campaign_name = %s
              AND shop_id = %s
              AND local_report_time >= DATE_SUB(NOW(), INTERVAL %s DAY)
            """,
            (campaign_name, shop_id, days),
            timeout=getattr(settings, "campaign_db_fallback_timeout", 60.0),
            label="campaign_perf_fallback",
        )
        if not row:
            return None
        cost = float(row.get("cost") or 0)
        sale = float(row.get("sale") or 0)
        clicks = float(row.get("clicks") or 0)
        impressions = float(row.get("impressions") or 0)
        orders = float(row.get("orders") or 0)
        return {
            "cost": cost,
            "sale": sale,
            "clicks": int(clicks),
            "impressions": int(impressions),
            "orders": int(orders),
            "acos": round(cost / sale * 100, 1) if sale else None,
            "cpc": round(cost / clicks, 2) if clicks else None,
            "ctr": round(clicks / impressions * 100, 1) if impressions else None,
            "cvr": round(orders / clicks * 100, 1) if clicks else None,
        }

    async def _fetch_campaign_placement_from_db(
        self, campaign_id: str, shop_id: int, days: int = 7,
    ) -> dict[str, dict]:
        """MCP placement_report 回落 — 按 campaign_id 查询，返回四桶聚合。

        dwd_amazon_ad_placement_report_update 按 campaign_id 过滤，
        GROUP BY placement 原值，不做 TOS/ROS 两桶合并。
        """
        rows = await self._query(
            """
            SELECT placement,
                   SUM(COALESCE(cost, 0)) AS cost,
                   SUM(COALESCE(sale, 0)) AS sale,
                   SUM(COALESCE(clicks, 0)) AS clicks,
                   SUM(COALESCE(impressions, 0)) AS impressions,
                   SUM(COALESCE(units_order, 0)) AS units_order
            FROM dwd_amazon_ad_placement_report_update
            WHERE campaign_id = %s
              AND shop_id = %s
              AND local_report_time >= DATE_SUB(NOW(), INTERVAL %s DAY)
            GROUP BY placement
            """,
            (campaign_id, shop_id, days),
            timeout=getattr(settings, "campaign_db_fallback_timeout", 60.0),
            label="campaign_placement_fallback",
        )
        result: dict[str, dict] = {}
        for r in rows:
            placement = str(r.get("placement") or "")
            cost = float(r.get("cost") or 0)
            sale = float(r.get("sale") or 0)
            clicks = float(r.get("clicks") or 0)
            result[placement] = {
                "cost": cost,
                "sale": sale,
                "clicks": int(clicks),
                "impressions": int(float(r.get("impressions") or 0)),
                "orders": int(float(r.get("units_order") or 0)),
                "acos": round(cost / sale * 100, 1) if sale else None,
                "cpc": round(cost / clicks, 2) if clicks else None,
            }
        return result

    async def _fetch_keyword_period_splits(
        self,
        listing_ctx: ListingContext,
        keyword_texts: list[str],
        days: int = 7,
    ) -> dict[str, dict]:
        """按时间窗拆分关键词广告指标：近半窗 vs 前半窗（供趋势选词）"""
        if not keyword_texts or not listing_ctx.child_asins:
            return {}
        half = max(1, days // 2)
        kw_ph = ",".join(["%s"] * len(keyword_texts))
        in_sql, in_params = build_in_clause("daap.asin", listing_ctx.child_asins)
        sql = f"""
            SELECT daak.keyword_text,
                   SUM(CASE WHEN daak.LOCAL_REPORT_TIME >= DATE_SUB(NOW(), INTERVAL %s DAY)
                            THEN COALESCE(daak.cost, 0) ELSE 0 END) AS cost_recent,
                   SUM(CASE WHEN daak.LOCAL_REPORT_TIME >= DATE_SUB(NOW(), INTERVAL %s DAY)
                            THEN COALESCE(daak.sale, 0) ELSE 0 END) AS sale_recent,
                   SUM(CASE WHEN daak.LOCAL_REPORT_TIME >= DATE_SUB(NOW(), INTERVAL %s DAY)
                            THEN COALESCE(daak.units_order, 0) ELSE 0 END) AS orders_recent,
                   SUM(CASE WHEN daak.LOCAL_REPORT_TIME >= DATE_SUB(NOW(), INTERVAL %s DAY)
                             AND daak.LOCAL_REPORT_TIME < DATE_SUB(NOW(), INTERVAL %s DAY)
                            THEN COALESCE(daak.cost, 0) ELSE 0 END) AS cost_prior,
                   SUM(CASE WHEN daak.LOCAL_REPORT_TIME >= DATE_SUB(NOW(), INTERVAL %s DAY)
                             AND daak.LOCAL_REPORT_TIME < DATE_SUB(NOW(), INTERVAL %s DAY)
                            THEN COALESCE(daak.sale, 0) ELSE 0 END) AS sale_prior,
                   SUM(CASE WHEN daak.LOCAL_REPORT_TIME >= DATE_SUB(NOW(), INTERVAL %s DAY)
                             AND daak.LOCAL_REPORT_TIME < DATE_SUB(NOW(), INTERVAL %s DAY)
                            THEN COALESCE(daak.units_order, 0) ELSE 0 END) AS orders_prior
            FROM dwd_amazon_ad_keyword_report daak
            INNER JOIN dwd_amazon_ad_product daap
                ON daap.campaign_id = daak.campaign_id AND daap.shop_id = daak.shop_id
            WHERE daak.keyword_text IN ({kw_ph})
              AND daak.campaign_status = 'ENABLED'
              AND daak.keyword_status = 'ENABLED'
              AND daap.shop_id = %s
              AND daak.LOCAL_REPORT_TIME >= DATE_SUB(NOW(), INTERVAL %s DAY)
              AND {in_sql}
            GROUP BY daak.keyword_text
        """
        params = (
            half, half, half,
            days, half, days, half, days, half,
            *keyword_texts,
            listing_ctx.shop_id, days, *in_params,
        )
        rows = await self._query(sql, params, label="keyword_period_splits")
        out: dict[str, dict] = {}
        for r in rows:
            kw = str(r.get("keyword_text", ""))
            cr, sr = _float(r.get("cost_recent")), _float(r.get("sale_recent"))
            cp, sp = _float(r.get("cost_prior")), _float(r.get("sale_prior"))
            out[kw] = {
                "acos_recent": round(cr / sr * 100, 1) if sr and sr > 0 and cr is not None else None,
                "acos_prior": round(cp / sp * 100, 1) if sp and sp > 0 and cp is not None else None,
                "spend_recent": round(cr, 2) if cr is not None else None,
                "spend_prior": round(cp, 2) if cp is not None else None,
                "orders_recent": _int(r.get("orders_recent")),
                "orders_prior": _int(r.get("orders_prior")),
            }
        return out

    async def _fetch_natural_rankings(self, listing_ctx: ListingContext, days: int = 7) -> list[dict]:
        """获取关键词自然排名（每个 keyword 取排名最好的子 ASIN）"""
        if not listing_ctx.child_asins:
            return []

        in_sql, in_params = build_in_clause("t1.asin", listing_ctx.child_asins)
        rows = await self._query(
            f"""
            SELECT t1.keyword, t1.craw_nature_rank, t1.craw_nature_rank_position,
                   t1.craw_sp_rank, t1.craw_sp_rank_position, t1.craw_time,
                   t1.near_craw_nature_rank, t1.near_craw_nature_rank_position,
                   t1.near_craw_sp_rank
            FROM dwd_amazon_asin_keyword_library t1
            WHERE t1.craw_nature_rank IS NOT NULL
              AND t1.craw_time >= NOW() - INTERVAL %s DAY
              AND {in_sql}
            ORDER BY t1.keyword ASC, t1.craw_nature_rank ASC
            """,
            (days, *in_params),
            label="natural_rankings",
        )

        # 每个 keyword 取排名最好的那条（ORDER BY rank ASC 的第一条）
        seen = set()
        result = []
        for r in rows:
            kw = r.get("keyword")
            if kw and kw not in seen:
                seen.add(kw)
                result.append(r)
        return result

    async def _fetch_competitors(self, parent_asin: str, shop_id: int) -> list[dict]:
        """获取直接竞品列表"""
        rows = await self._query(f"""
            SELECT p.asin, p.title, p.price, p.asin_star, p.reviews_num,
                   p.ratings_num, p.top_category_rank, p.brand,
                   p.az_color, p.az_size, p.fabric_type_value, p.variant_num
            FROM dwd_amazon_listing_competitor_asin_group g
            INNER JOIN dwd_amazon_listing_competitor_asin_group_relation r
                ON g.id = r.group_id
            INNER JOIN dwd_az_cw_asin_prod_info p
                ON p.asin = r.competitor_asin AND p.site_code = r.site_code
            WHERE g.parent_asin = %s
              AND g.shop_id = %s
              AND g.group_name = 'AI直接竞品组'
            LIMIT 20
        """, (parent_asin, shop_id))

        return rows

    async def _fetch_flow_keywords_legacy(self, listing_ctx: ListingContext) -> list[dict]:
        """Legacy: single JOIN query (slow on large keyword library)."""
        return await self._query(
            """
            SELECT a.keyword, c.searches, c.searches_rank,
                   c.top_click_ratio, c.top_convert_ratio
            FROM dwd_amazon_listing_flow_keyword_us a
            LEFT JOIN dwd_amazon_precise_keyword_library c
                ON a.site_code = c.site_code AND a.keyword = c.keyword
            WHERE a.shop_id = %s
              AND a.parent_asin = %s
              AND (a.del_status IS NULL OR a.del_status = 'VALID')
            ORDER BY c.searches DESC
            LIMIT 100
            """,
            (listing_ctx.shop_id, listing_ctx.parent_asin),
            label="flow_keywords.legacy",
        )

    async def _fetch_flow_keywords(self, listing_ctx: ListingContext) -> list[dict]:
        """获取 Listing 流量关键词（两步查：先 flow 表，再 library 批量 IN）。"""
        if getattr(settings, "db_use_legacy_flow_sql", False):
            return await self._fetch_flow_keywords_legacy(listing_ctx)

        timeout_s = float(getattr(settings, "db_flow_keyword_timeout", 25.0))
        prefetch = int(getattr(settings, "db_flow_keyword_prefetch_limit", 150))
        out_limit = int(getattr(settings, "db_flow_keyword_expand_limit", 100))

        step_a = await self._query(
            """
            SELECT a.keyword, a.site_code
            FROM dwd_amazon_listing_flow_keyword_us a
            WHERE a.shop_id = %s
              AND a.parent_asin = %s
              AND (a.del_status IS NULL OR a.del_status = 'VALID')
            LIMIT %s
            """,
            (listing_ctx.shop_id, listing_ctx.parent_asin, prefetch),
            timeout=timeout_s,
            label="flow_keywords.step_a",
        )
        if not step_a:
            return []

        site_code = step_a[0].get("site_code") or "Amazon_US"
        keywords = list(dict.fromkeys(str(r["keyword"]) for r in step_a if r.get("keyword")))
        lib_map: dict[str, dict] = {}

        batch_size = 50
        max_batches = 3
        for i in range(0, min(len(keywords), batch_size * max_batches), batch_size):
            batch = keywords[i : i + batch_size]
            if not batch:
                break
            ph = ",".join(["%s"] * len(batch))
            try:
                lib_rows = await self._query(
                    f"""
                    SELECT c.keyword, c.searches, c.searches_rank,
                           c.top_click_ratio, c.top_convert_ratio
                    FROM dwd_amazon_precise_keyword_library c
                    WHERE c.site_code = %s
                      AND c.keyword IN ({ph})
                    """,
                    (site_code, *batch),
                    timeout=timeout_s,
                    label="flow_keywords.step_b",
                )
                for row in lib_rows:
                    kw = str(row.get("keyword", ""))
                    if kw:
                        lib_map[kw] = row
            except asyncio.TimeoutError:
                logger.warning(
                    "flow_keywords step_b batch %d timeout, degrade to step_a only",
                    i // batch_size + 1,
                )
                break
            except Exception as e:  # noqa: BLE001
                logger.warning("flow_keywords step_b failed: %s", e)
                break

        merged: list[dict] = []
        for kw in keywords:
            lib = lib_map.get(kw, {})
            merged.append({
                "keyword": kw,
                "searches": lib.get("searches"),
                "searches_rank": lib.get("searches_rank"),
                "top_click_ratio": lib.get("top_click_ratio"),
                "top_convert_ratio": lib.get("top_convert_ratio"),
            })

        def _sort_key(row: dict) -> tuple:
            searches = _float(row.get("searches")) or 0
            convert = _float(row.get("top_convert_ratio")) or 0
            return (searches, convert)

        merged.sort(key=_sort_key, reverse=True)
        return merged[:out_limit]

    async def _fetch_gross_profit(self, parent_asin: str, parent_seller_sku: str, days: int = 7) -> dict | None:
        """从 dwd_az_asin_gross_profit 拉取销售额/广告/毛利/库存（仅跟进中 ASIN）。

        metrics 和 stock 拆为两个独立查询并行执行，避免三层嵌套子查询。
        """
        if not parent_asin or not parent_seller_sku:
            return None

        # 主查询：销售/广告/毛利聚合
        main_task = asyncio.to_thread(self._do_query_one, """
            SELECT SUM(COALESCE(daagp.order_sale_amount, 0)) AS total_sales,
                   SUM(COALESCE(daagp.ad_cost_amount, 0)) AS total_ad_cost,
                   SUM(COALESCE(daagp.order_num, 0)) AS total_orders,
                   SUM(COALESCE(daagp.ad_sale_num, 0)) AS ad_orders,
                   SUM(COALESCE(daagp.gross_profit_amount_proportion
                       * daagp.order_sale_amount, 0))
                       / NULLIF(SUM(COALESCE(daagp.order_sale_amount, 0)), 0) AS margin
            FROM dwd_az_asin_gross_profit daagp
            WHERE daagp.parent_asin = %s
              AND daagp.parent_seller_sku = %s
              AND daagp.statistics_data_time >= DATE_SUB(NOW(), INTERVAL %s DAY)
              AND EXISTS (
                  SELECT 1
                  FROM dwd_whp_amazon_listing_general a
                  INNER JOIN dwd_whp_az_extend e
                      ON a.shop_id = e.shop_id AND a.asin = e.asin
                      AND a.seller_sku = e.seller_sku
                      AND e.follow_up = 'YES_FOLLOW_UP'
                  WHERE a.parent_asin = daagp.parent_asin
                    AND a.parent_seller_sku = daagp.parent_seller_sku
                    AND a.asin = daagp.asin
                    AND a.seller_sku = daagp.seller_sku
                    AND a.shop_id = daagp.shop_id
              )
        """, (parent_asin, parent_seller_sku, days))

        # 库存查询：最新日期的可售库存快照
        stock_task = asyncio.to_thread(self._do_query_one, """
            SELECT SUM(COALESCE(can_sale_num, 0)) AS stock
            FROM dwd_az_asin_gross_profit
            WHERE parent_asin = %s
              AND parent_seller_sku = %s
              AND statistics_data_time = (
                  SELECT MAX(statistics_data_time)
                  FROM dwd_az_asin_gross_profit
                  WHERE parent_asin = %s
                    AND parent_seller_sku = %s
                    AND statistics_data_time >= DATE_SUB(NOW(), INTERVAL %s DAY)
              )
        """, (parent_asin, parent_seller_sku, parent_asin, parent_seller_sku, days))

        main, stock = await asyncio.gather(main_task, stock_task, return_exceptions=True)

        if isinstance(main, BaseException):
            logger.warning("gross_profit metrics 查询失败: %s", main)
            main = None
        if isinstance(stock, BaseException):
            logger.warning("gross_profit stock 查询失败: %s", stock)
            stock = None

        if main:
            main["stock"] = (stock or {}).get("stock", 0) if stock else 0
            return main
        return None

    async def _fetch_trend_data(self, parent_asin: str, parent_seller_sku: str,
                                days: int = 7) -> list[dict]:
        """按日聚合趋势数据（返回原始数值，衍生指标由调用方计算）

        WHERE 条件与 _fetch_gross_profit 一致：parent_asin + parent_seller_sku。
        """
        if not parent_asin or not parent_seller_sku:
            return []

        max_row = await self._query_one(
            """
            SELECT MAX(statistics_data_time) AS max_time
            FROM dwd_az_asin_gross_profit
            WHERE parent_asin = %s AND parent_seller_sku = %s
            """,
            (parent_asin, parent_seller_sku),
            label="trend_data.max_time",
        )
        max_time = max_row.get("max_time") if max_row else None
        if not max_time:
            return []

        rows = await self._query(
            """
            SELECT
                DATE_FORMAT(statistics_data_time, '%%m-%%d') AS date,
                SUM(COALESCE(order_num, 0)) AS orders,
                SUM(COALESCE(ad_sale_num, 0)) AS ad_orders,
                SUM(COALESCE(ad_cost_amount, 0)) AS spend,
                SUM(COALESCE(ad_sale_money, 0)) AS ad_sales,
                SUM(COALESCE(ad_click, 0)) AS clicks,
                SUM(COALESCE(ad_impressions, 0)) AS impressions
            FROM dwd_az_asin_gross_profit
            WHERE parent_asin = %s
              AND parent_seller_sku = %s
              AND statistics_data_time >= DATE_SUB(NOW(), INTERVAL %s DAY)
              AND statistics_data_time <= %s
            GROUP BY statistics_data_time
            ORDER BY statistics_data_time ASC
            """,
            (parent_asin, parent_seller_sku, days, max_time),
            label="trend_data",
        )
        return rows

    def _do_query_one(self, sql: str, params: tuple = ()) -> dict | None:
        """同步版 _query_one，供 asyncio.to_thread 直接调用（避免双层 to_thread 包装）"""
        rows = self._pool_do_query(sql, params)
        return rows[0] if rows else None

    def _pool_do_query(self, sql: str, params: tuple = ()) -> list[dict]:
        """同步执行一次查询（直接借还连接，不走 semaphore，由调用方控制并发）"""
        conn = self._pool.connection()
        closed = False
        try:
            cur = conn.cursor()
            try:
                cur.execute(sql, params)
                return cur.fetchall()
            finally:
                cur.close()
        except (pymysql.err.OperationalError, pymysql.err.InterfaceError):
            try:
                conn.close()
            except Exception:
                pass
            closed = True
            conn = self._pool.connection()
            try:
                cur = conn.cursor()
                try:
                    cur.execute(sql, params)
                    return cur.fetchall()
                finally:
                    cur.close()
            finally:
                conn.close()
        finally:
            if not closed:
                conn.close()


# ── 辅助函数 ─────────────────────────────────────────────

def _float(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v) -> int | None:
    if v is None:
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None
