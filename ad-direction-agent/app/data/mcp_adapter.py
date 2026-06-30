"""MCP-based data adapter.

Fetches ASIN data from MCP tools and normalizes outputs into ASINData.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.config.settings import settings
from app.data.base import DataSourceAdapter
from app.data.mcp_client import StreamableHttpMcpInvoker
from app.data.mcp_mapping import BOOTSTRAP_TOOLS, META_TO_MCP_TOOLS, McpContext, build_tool_args, make_date_window
from app.data.mcp_db_context import resolve_mcp_context_from_mcp
from app.data.mcp_normalizers import (
    _as_rows,
    _int,
    compute_natural_order_ratio,
    normalize_ad_placement,
    normalize_ad_summary,
    normalize_competitors,
    normalize_flow_keywords,
    normalize_keyword_rankings,
    normalize_keywords,
    normalize_listing_basic_info,
    normalize_product_sales,
    normalize_trend,
)
from app.models.asin_data import ASINData, AdData, CompetitorData, KeywordData, SpecialSignals, TrendPoint

logger = logging.getLogger(__name__)


def _extract_campaign_exact_keywords(payload: Any) -> list[dict]:
    """从 ad_campaign_product_keyword_list 回包提取精准关键词去重列表。
    入参 shape: [{关键词:..., 关键词匹配类型:..., ...}, ...]
    出参: [{keyword_text:..., match_type:"EXACT"}, ...]，去重仅保留 EXACT。
    """
    rows = _as_rows(payload)
    seen: set[str] = set()
    out: list[dict] = []
    for r in rows:
        kw_raw = str(r.get("关键词") or r.get("keyword_text") or r.get("keyword") or "").strip()
        if not kw_raw:
            continue
        kw_lower = kw_raw.lower()
        if kw_lower in seen:
            continue
        mt = str(r.get("关键词匹配类型") or r.get("match_type") or "").strip().upper()
        if mt != "EXACT":
            continue
        seen.add(kw_lower)
        out.append({"keyword_text": kw_raw, "match_type": "EXACT"})
    if out:
        logger.info("_extract_campaign_exact_keywords: %d unique EXACT keywords from ad_campaign_product_keyword_list", len(out))
    return out


class McpInvoker(Protocol):
    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        ...


class LegacyRestMcpInvoker:
    """Legacy REST gateway: POST {mcp_gateway_url}/tools/{tool_name}."""


    def __init__(self):
        self._client = httpx.AsyncClient(
            timeout=settings.mcp_timeout,
            limits=httpx.Limits(
                max_connections=settings.mcp_max_connections,
                max_keepalive_connections=40,
            ),
        )

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        if not settings.mcp_gateway_url:
            raise RuntimeError("MCP 网关未配置: mcp_gateway_url")
        url = settings.mcp_gateway_url.rstrip("/") + f"/tools/{tool_name}"
        headers = {}
        if settings.mcp_gateway_token:
            if settings.mcp_gateway_header_name.lower() == "authorization":
                headers["Authorization"] = f"Bearer {settings.mcp_gateway_token}"
            else:
                headers[settings.mcp_gateway_header_name] = settings.mcp_gateway_token
        resp = await self._client.post(url, json=arguments, headers=headers)
        resp.raise_for_status()
        return resp.json()


class UnsupportedMcpInvoker:
    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        raise RuntimeError(
            "MCP transport 未启用。请设置 mcp_transport=http 并配置 mcp_gateway_url。"
        )


@dataclass
class _CallResult:
    ok: bool
    value: Any = None
    error: str | None = None


class McpAdapter(DataSourceAdapter):
    def __init__(self, invoker: McpInvoker | None = None):
        self._invoker = invoker or self._create_invoker()
        self._sem = asyncio.Semaphore(max(1, settings.mcp_max_concurrency))

    @staticmethod
    def _create_invoker() -> McpInvoker:
        transport = (settings.mcp_transport or "none").lower()
        if transport in ("http", "sse", "streamable-http"):
            return StreamableHttpMcpInvoker()
        if transport == "rest":
            return LegacyRestMcpInvoker()
        return UnsupportedMcpInvoker()

    async def _call_tool(self, tool_name: str, arguments: dict[str, Any]) -> _CallResult:
        retries = max(0, settings.mcp_retries)
        last_err = None
        for attempt in range(retries + 1):
            t0 = time.monotonic()
            try:
                async with self._sem:
                    value = await asyncio.wait_for(
                        self._invoker.call_tool(tool_name, arguments),
                        timeout=settings.mcp_timeout,
                    )
                elapsed = time.monotonic() - t0
                logger.info(
                    "MCP tool ok [%s] attempt=%s elapsed=%.2fs args=%s",
                    tool_name,
                    attempt + 1,
                    elapsed,
                    json.dumps(arguments, ensure_ascii=False)[:220],
                )
                return _CallResult(ok=True, value=value)
            except asyncio.TimeoutError:
                last_err = f"timeout after {settings.mcp_timeout}s"
            except Exception as e:  # noqa: BLE001 - adapter-level fault boundary
                last_err = str(e) or type(e).__name__
                elapsed = time.monotonic() - t0
                logger.warning(
                    "MCP tool fail [%s] attempt=%s elapsed=%.2fs error=%s args=%s",
                    tool_name,
                    attempt + 1,
                    elapsed,
                    last_err,
                    json.dumps(arguments, ensure_ascii=False)[:220],
                )
                if attempt < retries:
                    await asyncio.sleep(min(0.5 * (attempt + 1), 1.5) + random.uniform(0.05, 0.2))
        return _CallResult(ok=False, error=last_err)

    async def _resolve_context(self, asin: str, days: int) -> McpContext:
        db_ctx = None
        if settings.mcp_resolve_context:
            db_ctx = await resolve_mcp_context_from_mcp(asin, self)
        if not db_ctx:
            raise RuntimeError(
                f"无法解析 MCP 上下文（ASIN={asin}）。"
                "请确认 MCP 服务可达，或配置 MCP_DEFAULT_PARENT_SELLER_SKU + MCP_DEFAULT_SHOP_ACCOUNT。"
            )
        # 日期窗口按该 ASIN 站点的当地时间（db_ctx.site_code）构造
        start_date, end_date = make_date_window(days, db_ctx.site_code)
        base_ctx = McpContext(
            parent_asin=db_ctx.parent_asin,
            parent_seller_sku=db_ctx.parent_seller_sku,
            shop_account=db_ctx.shop_account,
            site_code=db_ctx.site_code,
            start_date=start_date,
            end_date=end_date,
        )
        listing_res = await self._call_tool("listing_basic_info", build_tool_args("listing_basic_info", base_ctx))
        if listing_res.ok:
            listing = normalize_listing_basic_info(listing_res.value)
            seller_sku = listing.get("seller_sku") or db_ctx.parent_seller_sku
            return McpContext(
                parent_asin=db_ctx.parent_asin,
                parent_seller_sku=str(seller_sku or ""),
                shop_account=db_ctx.shop_account,
                site_code=db_ctx.site_code,
                start_date=start_date,
                end_date=end_date,
            )
        return base_ctx

    async def fetch_asin_data(
        self,
        asin: str,
        meta_filter: list[str] | None = None,
        days: int = 7,
    ) -> ASINData:
        missing_fields: list[str] = []
        try:
            ctx = await self._resolve_context(asin, days)
        except Exception as e:  # noqa: BLE001
            logger.error("MCP 上下文解析失败 [%s]: %s", asin, e)
            return ASINData(asin=asin, data_missing=True, missing_fields=["context"])

        meta_ids = meta_filter or list(META_TO_MCP_TOOLS.keys())
        bootstrap_tools = set(BOOTSTRAP_TOOLS)
        planned_tools: list[str] = list(bootstrap_tools)
        for meta in meta_ids:
            planned_tools.extend(META_TO_MCP_TOOLS.get(meta, []))
        planned_tools = list(dict.fromkeys(planned_tools))

        from app.data.mcp_fetch_run import run_planned_mcp_tools

        payload_map, missing_fields, partial_failures = await run_planned_mcp_tools(
            self,
            ctx,
            planned_tools,
            bootstrap_tools=bootstrap_tools,
            bootstrap_timeout=settings.mcp_bootstrap_timeout,
            reports_timeout=settings.mcp_tool_timeout,
            max_concurrency=settings.mcp_max_concurrency,
        )

        # ── Phase 2: 逐词查自然排名 ──
        # keyword_child_asins 需传具体 keyword 参数，无法在 Phase 1 并行（此时才拿到词表）。
        # 对 ad_campaign_product_keyword_list 发现的 EXACT 词逐条查询，并行发出。
        _campaign_rows = _as_rows(payload_map.get("ad_campaign_product_keyword_list"))
        _seen: set[str] = set()
        _exact_kws: list[str] = []
        for r in _campaign_rows:
            kw = str(r.get("关键词") or r.get("keyword_text") or r.get("keyword") or "").strip().lower()
            if not kw or kw in _seen:
                continue
            mt = str(r.get("关键词匹配类型") or r.get("match_type") or "").strip().upper()
            if mt != "EXACT":
                continue
            _seen.add(kw)
            _exact_kws.append(kw)
        _RANK_CAP = 50
        if len(_exact_kws) > _RANK_CAP:
            logger.info("Phase2 ranking [%s]: %d EXACT keywords, capping at %d", asin, len(_exact_kws), _RANK_CAP)
            _exact_kws = _exact_kws[:_RANK_CAP]

        if _exact_kws:
            _child_tasks = [
                self.call_tool_timed_with_args(
                    "keyword_child_asins",
                    {
                        "keyword": kw,
                        "site_code": ctx.site_code,
                        "parent_asin": ctx.parent_asin,
                        "parent_seller_sku": ctx.parent_seller_sku,
                        "shop_account": ctx.shop_account,
                    },
                    getattr(settings, "mcp_rank_timeout", 45.0),
                )
                for kw in _exact_kws
            ]
            _child_results = await asyncio.gather(*_child_tasks, return_exceptions=True)
            _rank_rows: list[dict] = []
            for kw, res in zip(_exact_kws, _child_results):
                if isinstance(res, BaseException) or not getattr(res, "ok", False):
                    continue
                for row in _as_rows(res.value):
                    row["keyword"] = kw
                    _rank_rows.append(row)
            if _rank_rows:
                payload_map["keyword_child_asins"] = _rank_rows
                logger.info("Phase2 ranking [%s]: %d rows from %d keywords", asin, len(_rank_rows), len(_exact_kws))

        data = self.assemble_from_payloads(
            asin=asin,
            ctx=ctx,
            payload_map=payload_map,
            missing_fields=missing_fields,
            days=days,
        )
        return finalize_mcp_asin_data(data, payload_map, missing_fields, meta_ids, partial_failures)

    def assemble_from_payloads(
        self,
        asin: str,
        ctx: McpContext,
        payload_map: dict[str, Any],
        missing_fields: list[str],
        days: int = 7,
    ) -> ASINData:
        listing = normalize_listing_basic_info(payload_map.get("listing_basic_info"))
        if "listing_basic_info" not in missing_fields and not listing:
            missing_fields.append("asin_not_found")
        inventory_rows = payload_map.get("listing_inventory")
        ad_summary = normalize_ad_summary(payload_map.get("ad_product_report"))
        placement = normalize_ad_placement(payload_map.get("ad_placement_report"))
        keywords = normalize_keywords(payload_map.get("ad_keyword_report"))
        # 2026-06-30: ad_keyword_report MCP 工具已下线，Doris 回落已切除。
        # 回退至 ad_campaign_product_keyword_list 发现精准关键词（campaign→词），
        # 仅取 EXACT 匹配，去重后作为关键词列表（无广告效果指标，后续接 ad_optimization 补）。
        if not keywords:
            keywords = _extract_campaign_exact_keywords(payload_map.get("ad_campaign_product_keyword_list"))
        rankings = normalize_keyword_rankings(
            payload_map.get("keyword_competitors"),
            payload_map.get("keyword_child_asins"),
        )
        flow_kws = normalize_flow_keywords(payload_map.get("flow_keywords"))
        competitors = normalize_competitors(payload_map.get("direct_competitors"))
        product_sales = normalize_product_sales(payload_map.get("product_sales"))
        trend_rows = normalize_trend(payload_map.get("product_sales"))

        data = ASINData(asin=asin)
        data.sku = listing.get("seller_sku") or ctx.parent_seller_sku
        data.parent_asin = asin
        data.price = listing.get("product_price")
        data.rating = listing.get("star_level")
        data.review_count = listing.get("comment_num")
        data.refund_rate = listing.get("refund_rate")
        data.brand = listing.get("brand")
        data.category_name = listing.get("category_name")

        margin_val = product_sales.get("margin")
        if margin_val is not None:
            data.margin = margin_val

        # 日均销量(MCP 口径)：全部销量(件) ÷ 窗口天数；无件数回落全部单量。
        # 供库存天数 = inventory_qty ÷ avg_daily_sales_30d（与 Doris 路径同义，不混用）。
        _units = product_sales.get("total_units") or product_sales.get("total_orders")
        if _units and days and days > 0:
            data.avg_daily_sales_30d = round(_units / days, 2)

        if ad_summary:
            ad_kwargs = {
                "acos": ad_summary.get("acos"),
                "cpc": ad_summary.get("cpc"),
                "ctr": ad_summary.get("ctr"),
                "cvr": ad_summary.get("cvr"),
                "impressions": ad_summary.get("impressions"),
                "clicks": ad_summary.get("clicks"),
                "spend": ad_summary.get("cost"),
                "sales": ad_summary.get("sale"),
                "orders": ad_summary.get("units_order"),
                "daily_budget": ad_summary.get("campaign_budget"),
            }
            total_sales = product_sales.get("total_sales")
            total_ad_cost = product_sales.get("total_ad_cost")
            if total_sales and total_ad_cost is not None and total_sales > 0:
                ad_kwargs["tacos"] = round(total_ad_cost / total_sales * 100, 1)
                ad_kwargs["daily_ad_spend_ratio"] = round(
                    (total_ad_cost / max(days, 1)) / (total_sales / max(days, 1)) * 100, 1
                )
            ad_kwargs.update(placement)
            data.ad_data = AdData(**ad_kwargs)

        ranking_map = {r.get("keyword"): r for r in rankings if r.get("keyword")}
        for kw in keywords:
            keyword = str(kw.get("keyword_text") or "")
            if not keyword:
                continue
            kd = KeywordData(
                keyword=keyword,
                impressions=int(kw.get("impressions") or 0),
                clicks=int(kw.get("clicks") or 0),
                spend=float(kw.get("cost") or 0),
                orders=int(kw.get("units_order") or 0),
                acos=kw.get("acos"),
                cvr=kw.get("cvr"),
                bid=kw.get("keyword_bid"),
                match_type=str(kw.get("match_type") or ""),
            )
            rank = ranking_map.get(keyword) or {}
            kd.natural_rank = rank.get("craw_nature_rank")
            kd.near_natural_rank = rank.get("near_craw_nature_rank")
            kd.sp_rank = rank.get("craw_sp_rank")
            if kd.near_natural_rank is not None and kd.natural_rank is not None:
                kd.rank_change_14d = kd.near_natural_rank - kd.natural_rank
            data.keywords.append(kd)

        data.keyword_count = len(data.keywords)
        ad_keyword_set = {k.keyword for k in data.keywords}
        new_kws = [fk for fk in flow_kws if fk.get("keyword") and fk.get("keyword") not in ad_keyword_set]
        data.available_new_keywords = len(new_kws)
        data.expand_keyword_candidates = [
            {
                "keyword": fk.get("keyword"),
                "searches": fk.get("searches"),
                "convert_ratio": fk.get("top_convert_ratio"),
            }
            for fk in sorted(
                new_kws,
                key=lambda fk: ((fk.get("top_convert_ratio") or 0), (fk.get("searches") or 0)),
                reverse=True,
            )[:10]
        ]

        for comp in competitors:
            data.competitors.append(
                CompetitorData(
                    asin=str(comp.get("asin") or ""),
                    price=comp.get("price"),
                    rating=comp.get("asin_star"),
                    review_count=comp.get("reviews_num"),
                    bsr=comp.get("top_category_rank"),
                )
            )

        nor = compute_natural_order_ratio(
            product_sales.get("total_orders"),
            product_sales.get("ad_orders"),
            trend_rows,
        )
        if nor is not None:
            data.natural_order_ratio = nor

        # listing_inventory 原始返回可能是 envelope（{"rows":[...]}/{"data":[...]}/单行 dict），
        # 必须经 _as_rows 解包（与空检查、其它工具口径一致）；原先裸 isinstance(list) 在 envelope 下
        # 恒为 False → 库存被丢成 None（数据查到了但没透传）。
        inv_rows = _as_rows(inventory_rows)
        # FBA可售：实测中文 key，父 ASIN 下各子 ASIN 加总
        data.signals = SpecialSignals(
            inventory_qty=sum(_int(r.get("FBA可售")) or 0 for r in inv_rows)
        )

        for row in trend_rows:
            row_orders = float(row.get("orders") or 0)
            row_ad_orders = float(row.get("ad_orders") or 0)
            row_clicks = float(row.get("clicks") or 0)
            row_impressions = float(row.get("impressions") or 0)
            row_spend = float(row.get("spend") or 0)
            row_ad_sales = float(row.get("ad_sales") or 0)
            data.trend.append(
                TrendPoint(
                    date=str(row.get("date") or ""),
                    acos=round(row_spend / row_ad_sales * 100, 1) if row_ad_sales else None,
                    cvr=round(row_ad_orders / row_clicks * 100, 1) if row_clicks else None,
                    ctr=round(row_clicks / row_impressions * 100, 1) if row_impressions else None,
                    cpc=round(row_spend / row_clicks, 2) if row_clicks else None,
                    orders=int(row_orders) if row_orders else None,
                    ad_orders=int(row_ad_orders) if row_ad_orders else None,
                    spend=round(row_spend, 2) if row_spend else None,
                )
            )

        data.data_missing = bool(missing_fields and len(missing_fields) >= 3)
        data.missing_fields = missing_fields
        return data

    async def campaign_call_tool(
        self,
        tool_name: str,
        campaign_name: str,
        shop_account: str,
        start_date: str = "",
        end_date: str = "",
        timeout: float | None = None,
    ) -> _CallResult:
        """Campaign 级 MCP 工具调用封装。

        与 call_tool_timed（ASIN 级）不同，使用 campaign_name 而非 parent_asin。
        """
        from app.data.mcp_mapping import build_campaign_tool_args

        args = build_campaign_tool_args(
            tool_name, campaign_name, shop_account, start_date, end_date,
        )
        t = timeout if timeout is not None else getattr(settings, "campaign_mcp_tool_timeout", 300.0)
        return await self.call_tool_timed_with_args(tool_name, args, t)

    async def call_tool_timed(
        self,
        tool_name: str,
        ctx: McpContext,
        timeout: float,
    ) -> _CallResult:
        """单工具带超时调用。"""
        args = build_tool_args(tool_name, ctx)
        return await self.call_tool_timed_with_args(tool_name, args, timeout)

    async def call_tool_timed_with_args(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        timeout: float,
    ) -> _CallResult:
        """单工具带超时调用（自定义入参，用于多 match_type 关键词报表）。"""
        try:
            return await asyncio.wait_for(self._call_tool(tool_name, arguments), timeout=timeout)
        except asyncio.TimeoutError:
            return _CallResult(ok=False, error=f"timeout after {timeout}s")


def finalize_mcp_asin_data(
    data: ASINData,
    payload_map: dict[str, Any],
    missing_fields: list[str],
    meta_ids: list[str] | None,
    partial_failures: list[str],
) -> ASINData:
    from app.data.mcp_empty_reports import append_empty_report_failures

    empty_flags = append_empty_report_failures(data, payload_map, missing_fields, meta_ids)
    merged = list(dict.fromkeys([*(partial_failures or []), *empty_flags]))
    data.partial_failures = merged
    if merged:
        data.data_freshness = "partial"
    elif not data.data_freshness:
        data.data_freshness = "fresh"
    return data

