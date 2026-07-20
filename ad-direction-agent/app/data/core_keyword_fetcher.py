"""CoreKeywordFetcher — 核心词判定专用窄 fetcher。

私有双持：
- StarRocks McpAdapter：5 个数据工具
- Azlisting StreamableHttpMcpInvoker：1 个商品信息工具

不提 McpAdapter 全局路由，不碰 CampaignFetcher。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.config.settings import settings
from app.core.core_keyword_policy import normalize_core_keyword
from app.data.mcp_adapter import McpAdapter
from app.data.mcp_client import StreamableHttpMcpInvoker
from app.data.mcp_db_context import _MCP_PARENT_KEY_MAP
from app.data.mcp_mapping import make_date_window
from app.data.mcp_normalizers import (
    _as_rows,
    _int,
    normalize_flow_keywords,
    normalize_keyword_rankings,
)

logger = logging.getLogger(__name__)


@dataclass
class FetchedContext:
    shop_account: str = ""
    parent_seller_sku: str = ""
    site_code: str = ""
    shop_id: int = 0


@dataclass
class ListingProductInfo:
    title: str = ""
    bullets: list[str] = field(default_factory=list)
    category: str = ""
    variation_theme: str = ""
    colors: list[str] = field(default_factory=list)
    sizes: list[str] = field(default_factory=list)


@dataclass
class CampaignKeywordItem:
    campaign_name: str
    keyword_text: str
    match_type: str
    child_asin: str
    cost_14d: float = 0.0
    orders_14d: int = 0
    acos_14d: float = 0.0
    natural_rank: int | None = None
    near_natural_rank: int | None = None


@dataclass
class FetchResult:
    context: FetchedContext
    listing: ListingProductInfo
    campaigns: list[CampaignKeywordItem]
    flow_keywords: list[dict[str, Any]]
    error: str = ""


class CoreKeywordFetcher:

    def __init__(self) -> None:
        self._starrocks = McpAdapter()
        self._azlisting: StreamableHttpMcpInvoker | None = None
        self._mcp_sem = asyncio.Semaphore(5)  # per-worker MCP 总并发

    def _ensure_azlisting(self) -> StreamableHttpMcpInvoker:
        if self._azlisting is None:
            if not settings.azlisting_mcp_url:
                raise RuntimeError("azlisting_mcp_url 未配置")
            self._azlisting = StreamableHttpMcpInvoker(
                endpoint=settings.azlisting_mcp_url,
                token=settings.azlisting_mcp_token,
                header_name=settings.azlisting_mcp_header_name,
                timeout=settings.azlisting_mcp_timeout,
            )
        return self._azlisting

    # ── Step1 ──────────────────────────────────────────────

    async def _step1_context(self, parent_asin: str) -> FetchedContext:
        async with self._mcp_sem:
            res = await self._starrocks.call_tool_timed_with_args(
                "parent_listing_detail",
                {"parent_asin": parent_asin},
                timeout=settings.mcp_context_timeout,
            )
        if not res.ok:
            raise RuntimeError(f"parent_listing_detail 失败: {res.error}")
        rows = _as_rows(res.value)
        if not rows:
            raise RuntimeError(f"parent_listing_detail 无返回: {parent_asin}")
        r = rows[0]
        mapped = {_MCP_PARENT_KEY_MAP.get(k, k): v for k, v in r.items()}
        return FetchedContext(
            shop_account=str(mapped.get("shop_account") or ""),
            parent_seller_sku=str(mapped.get("parent_seller_sku") or ""),
            site_code=str(mapped.get("site_code") or ""),
            shop_id=_int(mapped.get("shop_id")),
        )

    # ── Step2: Azlisting ───────────────────────────────────

    async def _step2_listing_info(
        self, shop_account: str, parent_asin: str, parent_seller_sku: str,
    ) -> ListingProductInfo:
        invoker = self._ensure_azlisting()
        async with self._mcp_sem:
            raw = await invoker.call_tool(
                "erp_listing_product_info",
                {"paramsJson": json.dumps({
                    "shopAccount": shop_account,
                    "parentAsin": parent_asin,
                    "parentSellerSku": parent_seller_sku,
                })},
            )
        rows = _as_rows(raw)
        if not rows:
            return ListingProductInfo()

        colors: list[str] = []
        sizes: list[str] = []
        bullets: list[str] = []
        title = ""
        category = ""
        variation_theme = ""

        for r in rows:
            c = str(r.get("productColor") or "").strip()
            s = str(r.get("productSize") or "").strip()
            if c:
                colors.append(c)
            if s:
                sizes.append(s)
            if not bullets:
                for i in range(1, 6):
                    b = str(r.get(f"fiveBulletPoint{i}") or "").strip()
                    if b:
                        bullets.append(b)
            if not title:
                title = str(r.get("productName") or "").strip()
            if not category:
                cat_raw = str(r.get("lastCategory") or "")
                try:
                    cat_list = json.loads(cat_raw)
                    if cat_list and isinstance(cat_list, list):
                        category = str(cat_list[0].get("title") or "")
                except (json.JSONDecodeError, IndexError, KeyError):
                    pass
            if not variation_theme:
                variation_theme = str(r.get("variationThemeName") or "").strip()

        return ListingProductInfo(
            title=title, bullets=bullets, category=category,
            variation_theme=variation_theme,
            colors=list(dict.fromkeys(colors)),
            sizes=list(dict.fromkeys(sizes)),
        )

    # ── Step3 ──────────────────────────────────────────────

    async def _step3_campaign_keywords(
        self, parent_asin: str, parent_seller_sku: str, shop_account: str,
    ) -> list[CampaignKeywordItem]:
        async with self._mcp_sem:
            res = await self._starrocks.call_tool_timed_with_args(
                "ad_campaign_product_keyword_list",
                {"parent_asin": parent_asin, "parent_seller_sku": parent_seller_sku,
                 "shop_account": shop_account},
                timeout=settings.campaign_mcp_tool_timeout,
            )
        if not res.ok:
            logger.warning("ad_campaign_product_keyword_list 失败: %s", res.error)
            return []
        rows = _as_rows(res.value)

        # 与 campaign_prefilter.py 对齐：按 campaign_id 分组，set 去重关键词，排除否定词。
        def _is_negative_match(mt: str) -> bool:
            return "negative" in (mt or "").lower()

        cid_kw_set: dict[str, set[str]] = {}
        cid_name: dict[str, str] = {}
        cid_mt: dict[str, str] = {}
        cid_asin: dict[str, str] = {}
        for r in rows:
            cid = str(r.get("campaign_id") or r.get("广告活动D") or r.get("广告活动ID") or "").strip()
            kw = str(r.get("关键词") or r.get("keyword_text") or r.get("keyword") or "").strip()
            mt = str(r.get("关键词匹配类型") or r.get("match_type") or "").strip()
            cn = str(r.get("广告活动名称") or r.get("campaign_name") or "").strip()
            ca = str(r.get("child_asin") or r.get("子SIN") or r.get("子ASIN") or "").strip()
            if not cid or not kw or not cn:
                continue
            if _is_negative_match(mt):
                continue
            cid_kw_set.setdefault(cid, set()).add(kw)
            if cid not in cid_name:
                cid_name[cid] = cn
                cid_mt[cid] = mt.upper()
                cid_asin[cid] = ca

        multi_count = sum(1 for kws in cid_kw_set.values() if len(kws) > 1)
        if multi_count:
            logger.info("Step3 过滤多词活动: %d 个活动，剔除", multi_count)

        items: list[CampaignKeywordItem] = []
        for cid, kws in cid_kw_set.items():
            if len(kws) != 1:
                continue
            kw = next(iter(kws))
            items.append(CampaignKeywordItem(
                campaign_name=cid_name[cid], keyword_text=kw,
                match_type=cid_mt.get(cid, ""), child_asin=cid_asin.get(cid, ""),
            ))
        return items

    # ── Step4: 14 天活动效果 ────────────────────────────────

    async def _step4_perf_reports(
        self, items: list[CampaignKeywordItem], shop_account: str, site_code: str,
    ) -> None:
        """拉 14 天报告，按 campaign_name 去重（Step3 已保证每个 campaign 只有 1 个关键词）。"""
        start, end = make_date_window(14, site_code)

        # 按 campaign 去重（防同一 campaign 被多次拉取；Step3 过滤后不应出现，纯防御）
        by_campaign: dict[str, CampaignKeywordItem] = {}
        for item in items:
            by_campaign.setdefault(item.campaign_name, item)

        async def _fetch_one(campaign_name: str, item: CampaignKeywordItem) -> None:
            async with self._mcp_sem:
                try:
                    res = await self._starrocks.call_tool_timed_with_args(
                        "ad_campaign_product_report",
                        {"shop_account": shop_account,
                         "campaign_name": campaign_name,
                         "start_date": start, "end_date": end},
                        timeout=60.0,
                    )
                    if not res.ok:
                        return
                    rows = _as_rows(res.value)
                    if not rows:
                        return
                    r = rows[0]
                    item.cost_14d = float(r.get("花费") or r.get("cost") or 0)
                    item.orders_14d = _int(r.get("广告订单量") or r.get("orders"))
                    acos_raw = r.get("ACOS") or r.get("acos")
                    item.acos_14d = _pct_raw(acos_raw) if acos_raw is not None else 0.0
                except Exception:
                    logger.warning("step4 perf fail: %s", campaign_name, exc_info=True)

        await asyncio.gather(
            *(_fetch_one(cn, item) for cn, item in by_campaign.items()),
        )

    # ── Step5: 自然排名 ────────────────────────────────────

    def _dedupe_keyword_items(
        self, items: list[CampaignKeywordItem],
    ) -> list[CampaignKeywordItem]:
        merged: dict[str, CampaignKeywordItem] = {}
        for item in items:
            key = item.keyword_text.strip().lower()
            if not key:
                continue
            existing = merged.get(key)
            if existing is None:
                merged[key] = item
                continue

            total_cost = existing.cost_14d + item.cost_14d
            total_orders = (existing.orders_14d or 0) + (item.orders_14d or 0)
            if total_cost > 0:
                existing.acos_14d = (
                    (existing.acos_14d or 0.0) * existing.cost_14d
                    + (item.acos_14d or 0.0) * item.cost_14d
                ) / total_cost
            existing.cost_14d = total_cost
            existing.orders_14d = total_orders
            if not existing.child_asin and item.child_asin:
                existing.child_asin = item.child_asin
            if existing.match_type != item.match_type:
                existing.match_type = existing.match_type or item.match_type
        return list(merged.values())

    @staticmethod
    def _filter_excluded_keyword_items(
        items: list[CampaignKeywordItem], excluded_keyword_norms: set[str],
    ) -> list[CampaignKeywordItem]:
        if not excluded_keyword_norms:
            return items
        excluded = {normalize_core_keyword(keyword) for keyword in excluded_keyword_norms}
        return [
            item for item in items
            if normalize_core_keyword(item.keyword_text) not in excluded
        ]


    async def _step5_rankings(
        self, items: list[CampaignKeywordItem], shop_account: str,
        parent_asin: str, parent_seller_sku: str, site_code: str,
    ) -> None:

        async def _one(item: CampaignKeywordItem) -> None:
            async with self._mcp_sem:
                try:
                    res = await self._starrocks.call_tool_timed_with_args(
                        "keyword_child_asins",
                        {"keyword": item.keyword_text, "site_code": site_code,
                         "parent_asin": parent_asin, "parent_seller_sku": parent_seller_sku,
                         "shop_account": shop_account},
                        timeout=45.0,
                    )
                    if not res.ok:
                        return
                    norm = normalize_keyword_rankings(None, res.value)
                    if norm:
                        n = norm[0]
                        item.natural_rank = n.get("craw_nature_rank")
                        item.near_natural_rank = n.get("near_craw_nature_rank")
                except Exception:
                    logger.warning("step5 rank fail: %s", item.keyword_text, exc_info=True)

        await asyncio.gather(*(_one(item) for item in items))

    # ── Step6: 流量关键词 ──────────────────────────────────

    async def _step6_flow(
        self, shop_account: str, parent_asin: str,
        parent_seller_sku: str, site_code: str,
    ) -> list[dict[str, Any]]:
        async with self._mcp_sem:
            res = await self._starrocks.call_tool_timed_with_args(
                "flow_keywords",
                {"site_code": site_code, "parent_asin": parent_asin,
                 "parent_seller_sku": parent_seller_sku, "shop_account": shop_account},
                timeout=settings.campaign_mcp_tool_timeout,
            )
        if not res.ok:
            logger.warning("flow_keywords 失败: %s", res.error)
            return []
        return normalize_flow_keywords(res.value)

    # ── 主入口 ─────────────────────────────────────────────

    async def fetch(
        self, parent_asin: str, parent_seller_sku: str, shop_id: int,
        *, excluded_keyword_norms: set[str] | None = None,
    ) -> FetchResult:
        # Step1
        ctx = await self._step1_context(parent_asin)

        # 身份校验：请求入参的三元组为权威
        if ctx.parent_seller_sku and ctx.parent_seller_sku != parent_seller_sku:
            return FetchResult(
                context=ctx, listing=ListingProductInfo(),
                campaigns=[], flow_keywords=[],
                error=f"parent_seller_sku 不一致: 请求={parent_seller_sku} 返回={ctx.parent_seller_sku}",
            )
        if ctx.shop_id and ctx.shop_id != shop_id:
            return FetchResult(
                context=ctx, listing=ListingProductInfo(),
                campaigns=[], flow_keywords=[],
                error=f"shop_id 不一致: 请求={shop_id} 返回={ctx.shop_id}",
            )

        shop_account = ctx.shop_account
        site_code = ctx.site_code

        # 剩余 MCP 步骤加总超时
        async def _mcp_work() -> FetchResult:
            # Step2-3 并行
            listing, campaigns = await asyncio.gather(
                self._step2_listing_info(shop_account, parent_asin, parent_seller_sku),
                self._step3_campaign_keywords(parent_asin, parent_seller_sku, shop_account),
            )
            campaigns = self._filter_excluded_keyword_items(
                campaigns, excluded_keyword_norms or set(),
            )
            if not campaigns:
                return FetchResult(
                    context=ctx, listing=listing, campaigns=[], flow_keywords=[],
                    error="核心词候选为空：广告关键词均来自多词活动或被人工锁定/否决",
                )

            # Step4 → 合并跨 campaign 同词 → Step5 → Step6（串行：合并依赖 Step4 的成本数据）
            flow_kws: list[dict[str, Any]] = []
            await self._step4_perf_reports(campaigns, shop_account, site_code)
            campaigns = self._dedupe_keyword_items(campaigns)
            await self._step5_rankings(
                campaigns, shop_account, parent_asin, parent_seller_sku, site_code,
            )
            flow_kws = await self._step6_flow(shop_account, parent_asin, parent_seller_sku, site_code)
            return FetchResult(
                context=ctx, listing=listing,
                campaigns=campaigns, flow_keywords=flow_kws,
            )

        try:
            return await asyncio.wait_for(
                _mcp_work(), timeout=settings.core_keyword_mcp_timeout,
            )
        except asyncio.TimeoutError:
            return FetchResult(
                context=ctx, listing=ListingProductInfo(),
                campaigns=[], flow_keywords=[],
                error=f"MCP 总超时 ({settings.core_keyword_mcp_timeout}s)",
            )


def _pct_raw(v: Any) -> float:
    """MCP 返回的百分比值转 float（如 25.4 → 25.4，保持百分比单位不变）。"""
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0
