"""Campaign 数据编排器 — Doris 上下文 → 硬过滤 → MCP 主力 → Doris 回落。

原则: Doris 只做轻量上下文（维度字段），MCP 拉效果数据，MCP 失败时回落 Doris。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from app.config.settings import settings
from app.data.campaign_prefilter import filter_campaigns
from app.data.db_adapter import DbAdapter
from app.data.db_sql_helpers import ListingContext
from app.data.mcp_adapter import McpAdapter
from app.data.mcp_db_context import resolve_mcp_context_from_db
from app.data.mcp_mapping import make_date_window
from app.models.campaign import CampaignData, CampaignPerf, CampaignUnit

logger = logging.getLogger(__name__)


@dataclass
class _CallResult:
    ok: bool
    value: Any = None
    error: str | None = None


class CampaignFetcher:
    """Doris 发现上下文 → 硬过滤 → MCP 主力 → Doris 回落"""

    def __init__(self):
        self._mcp_adapter: McpAdapter | None = None
        self._db_adapter: DbAdapter | None = None
        self._mcp_sem = asyncio.Semaphore(settings.mcp_max_concurrency)
        self._last_shop_id: int = 0  # fetch_campaigns 解析后缓存，供懒加载回落复用
        self._last_shop_account: str = ""  # 同上，供新增活动线 discover_new_keywords 复用

    # ── 主流程 ──

    async def fetch_campaigns(
        self,
        parent_asin: str,
        days: int = 7,
        *,
        prefer_db: bool = False,
    ) -> CampaignData:
        """完整 pipeline 入口。

        prefer_db=True 时跳过 MCP，全走 Doris（用于刷新/调试）。
        """
        errors: list[str] = []

        # ① 解析上下文 → child_asins + shop_account
        db_ctx = await resolve_mcp_context_from_db(parent_asin)
        shop_id = 0
        parent_seller_sku = ""
        site_code = "Amazon_US"
        if db_ctx:
            shop_id = db_ctx.shop_id or 0
            parent_seller_sku = db_ctx.parent_seller_sku or ""
            site_code = db_ctx.site_code or "Amazon_US"
            # 缓存供新增活动线 discover_new_keywords 复用 (shop_account 不在 CampaignData 上)
            self._last_shop_id = shop_id
            self._last_shop_account = db_ctx.shop_account or ""

        if not db_ctx:
            return CampaignData(
                parent_asin=parent_asin,
                errors=["无法解析 MCP 上下文"],
                fetch_source="mcp",
            )

        # ② Doris 轻量上下文查询 → 仅维度字段
        raw_campaigns = await self._discover_context_from_doris(parent_asin)
        if not raw_campaigns:
            return CampaignData(
                parent_asin=parent_asin,
                shop_id=shop_id,
                parent_seller_sku=parent_seller_sku,
                site_code=site_code,
                total_campaigns=0,
                fetch_source="doris",
            )

        # ③ 代码硬过滤
        surviving, excluded = filter_campaigns(raw_campaigns)
        logger.info(
            "Campaign prefilter [%s]: %d surviving, %d excluded",
            parent_asin, len(surviving), len(excluded),
        )

        if not surviving:
            return CampaignData(
                parent_asin=parent_asin,
                shop_id=shop_id,
                parent_seller_sku=parent_seller_sku,
                site_code=site_code,
                total_campaigns=len(raw_campaigns),
                excluded=excluded,
                fetch_source="doris",
            )

        # ④ MCP 必拉: basic_info (days_online, 交叉验证 budget/status)
        #    + product_report (7d 效果指标)
        #    prefer_db=True 时全部走 Doris
        start_date, end_date = _make_date_window(days)
        shop_account = db_ctx.shop_account

        # listing/shop_id 解析一次（#5 防 MCP 全挂时循环内 N 次冗余查询）
        db = self._get_db()
        listing = await db._resolve_and_fetch_listing(parent_asin)
        lctx = ListingContext.from_listing_row(listing) if listing else None
        shop_id = lctx.shop_id if lctx else 0
        self._last_shop_id = shop_id  # 供懒加载回落复用

        # 唯一活动名（去重）
        names = sorted({str(c.get("campaign_name") or "") for c in surviving})

        basic_results: dict[str, dict] = {}
        perf_results: dict[str, dict] = {}
        mcp_ok = 0
        mcp_fail = 0

        if prefer_db:
            for camp in surviving:
                name = str(camp.get("campaign_name") or "")
                basic_results[name] = {
                    "campaign_budget": float(camp.get("campaign_budget") or 0),
                    "campaign_status": str(camp.get("campaign_status") or ""),
                    "days_online": -1,  # Doris 无活动上线天数，标未知（勿用 0 触发新活动保护）
                    "tos_bid_pct": 0.0,
                    "pp_bid_pct": 0.0,
                    "ros_bid_pct": 0.0,
                    "source": "doris",
                }
                perf = await db._fetch_campaign_perf_from_db(name, shop_id, days)
                perf_results[name] = {**(perf or {}), "source": "doris"}
            fetch_source = "doris"
        else:
            # 并行拉 basic_info（return_exceptions=True：单活动异常不拖垮全部）
            basic_list = await asyncio.gather(
                *[self._fetch_basic_one(name, shop_account) for name in names],
                return_exceptions=True,
            )
            for name, res in self._zip_results(names, basic_list):
                if res.ok and isinstance(res.value, dict):
                    basic_results[name] = {**res.value, "source": "mcp"}
                    mcp_ok += 1
                else:
                    basic_results[name] = self._doris_fallback_basic_info(name)
                    mcp_fail += 1

            # 并行拉 product_report；失败回落 Doris（shop_id 已在循环外解析）
            perf_list = await asyncio.gather(
                *[self._fetch_perf_one(name, shop_account, start_date, end_date)
                  for name in names],
                return_exceptions=True,
            )
            for name, res in self._zip_results(names, perf_list):
                if res.ok and isinstance(res.value, dict):
                    perf_results[name] = {**res.value, "source": "mcp"}
                    mcp_ok += 1
                else:
                    fb = await db._fetch_campaign_perf_from_db(name, shop_id, days)
                    perf_results[name] = {**(fb or {}), "source": "doris"}
                    mcp_fail += 1

            fetch_source = "mcp" if mcp_fail == 0 else ("mixed" if mcp_ok > 0 else "doris")
            logger.info(
                "Campaign MCP [%s]: %d ok, %d failed, source=%s",
                parent_asin, mcp_ok, mcp_fail, fetch_source,
            )

        # ⑤ 组装 CampaignUnit
        campaigns: list[CampaignUnit] = []
        for camp in surviving:
            name = str(camp.get("campaign_name") or "")
            basic = basic_results.get(name, {})
            perf = perf_results.get(name, {})
            unit = self._assemble(camp, basic, perf)
            campaigns.append(unit)

        if errors:
            logger.warning("Campaign errors [%s]: %s", parent_asin, errors)

        return CampaignData(
            parent_asin=parent_asin,
            shop_id=shop_id,
            parent_seller_sku=parent_seller_sku,
            site_code=site_code,
            total_campaigns=len(campaigns),
            campaigns=campaigns,
            excluded=excluded,
            errors=errors,
            fetch_source=fetch_source,
        )

    # ── 内部方法 ──

    async def _discover_context_from_doris(self, parent_asin: str) -> list[dict]:
        """① Doris 轻量上下文查询 — 仅维度字段。"""
        db = self._get_db()
        listing = await db._resolve_and_fetch_listing(parent_asin)
        if not listing:
            return []
        lctx = ListingContext.from_listing_row(listing)
        return await db._fetch_campaign_context(lctx)

    async def _fetch_basic_one(
        self, campaign_name: str, shop_account: str,
    ) -> tuple[str, _CallResult]:
        """调用 ad_campaign_basic_info。解析异常降级为失败，触发回落。"""
        try:
            res = await self._mcp().campaign_call_tool(
                "ad_campaign_basic_info", campaign_name, shop_account,
            )
            if res.ok:
                payload = _as_rows(res.value)
                if payload:
                    row = payload[0]
                    return campaign_name, _CallResult(ok=True, value={
                        "campaign_budget": _to_float(row.get("广告活动预算")) or 0.0,
                        "campaign_status": str(row.get("状态") or ""),
                        # 字段缺失 → -1（未知），勿伪装成 0 天触发"新活动保护"
                        "days_online": _to_days_online(row.get("活动上线天数")),
                        # 广告位加价比例 (KB 19 §5 决策矩阵依赖)
                        "tos_bid_pct": _to_float(row.get("头部位置加价比例")) or 0.0,
                        "pp_bid_pct": _to_float(row.get("商品位置加价比例")) or 0.0,
                        "ros_bid_pct": _to_float(row.get("其他位置加价比例")) or 0.0,
                    })
            return campaign_name, _CallResult(ok=False, error=res.error)
        except Exception as e:  # noqa: BLE001
            logger.warning("basic_info 解析失败 [%s]: %s", campaign_name, e)
            return campaign_name, _CallResult(ok=False, error=str(e))

    async def _fetch_perf_one(
        self, campaign_name: str, shop_account: str, start_date: str, end_date: str,
    ) -> tuple[str, _CallResult]:
        """调用 ad_campaign_product_report。解析异常降级为失败，触发回落。"""
        try:
            res = await self._mcp().campaign_call_tool(
                "ad_campaign_product_report", campaign_name, shop_account,
                start_date=start_date, end_date=end_date,
            )
            if res.ok:
                payload = _as_rows(res.value)
                if payload:
                    row = payload[0]
                    return campaign_name, _CallResult(ok=True, value={
                        "cost": _to_float(row.get("花费")) or 0.0,
                        "sale": _to_float(row.get("销售额")) or 0.0,
                        "clicks": int(_to_float(row.get("点击量")) or 0),
                        "impressions": int(_to_float(row.get("曝光量")) or 0),
                        "orders": int(_to_float(row.get("广告订单量")) or 0),
                        # MCP 百分比字段统一归一为百分数口径，与 Doris 回落对齐
                        "acos": _to_pct(row.get("ACOS")),
                        "cpc": _to_float(row.get("CPC")),
                        "ctr": _to_pct(row.get("CTR")),
                        "cvr": _to_pct(row.get("CVR")),
                    })
            return campaign_name, _CallResult(ok=False, error=res.error)
        except Exception as e:  # noqa: BLE001
            logger.warning("product_report 解析失败 [%s]: %s", campaign_name, e)
            return campaign_name, _CallResult(ok=False, error=str(e))

    def _doris_fallback_basic_info(self, campaign_name: str) -> dict:
        """basic_info 回落。Doris 无活动上线天数 → days_online=-1（未知）。

        budget/status 在组装时由上下文行兜底（见 _assemble），此处仅给占位。
        """
        return {
            "campaign_budget": 0.0,
            "campaign_status": "",
            "days_online": -1,
            "tos_bid_pct": 0.0,
            "pp_bid_pct": 0.0,
            "ros_bid_pct": 0.0,
            "source": "doris",
        }

    @staticmethod
    def _zip_results(
        names: list[str], results: list,
    ) -> list[tuple[str, _CallResult]]:
        """配对 gather 结果；异常项转为失败 _CallResult（return_exceptions=True 善后）。"""
        out: list[tuple[str, _CallResult]] = []
        for name, r in zip(names, results):
            if isinstance(r, BaseException):
                out.append((name, _CallResult(ok=False, error=str(r))))
            elif isinstance(r, tuple) and len(r) == 2:
                out.append((r[0], r[1]))
            else:
                out.append((name, _CallResult(ok=False, error="unexpected result")))
        return out

    # ── 懒加载接口（LLM 层调用） ──

    async def fetch_placement_for(
        self,
        campaign_ids: dict[str, str],  # {campaign_name: campaign_id}
        shop_account: str,
        shop_id: int,
        start_date: str = "",
        end_date: str = "",
        days: int = 7,
    ) -> dict[str, dict]:
        """按需拉取 placement 拆分（仅精准广告）。

        返回: {campaign_name: {placement_name: {cost, sale, clicks, ...}}}
        shop_id 供 MCP 失败时 Doris 回落（按 campaign_id 查询）使用。
        """
        results: dict[str, dict] = {}
        sem = self._mcp_sem

        async def _one(name: str):
            try:
                async with sem:
                    res = await self._mcp().campaign_call_tool(
                        "ad_campaign_placement_report", name, shop_account,
                        start_date=start_date, end_date=end_date,
                    )
                if res.ok:
                    payload = _as_rows(res.value)
                    placements: dict[str, dict] = {}
                    for row in payload:
                        placement = str(row.get("广告位") or "")
                        cost = _to_float(row.get("花费")) or 0.0
                        sale = _to_float(row.get("销售额")) or 0.0
                        clicks = int(_to_float(row.get("点击量")) or 0)
                        placements[placement] = {
                            "cost": cost,
                            "sale": sale,
                            "clicks": clicks,
                            "impressions": int(_to_float(row.get("曝光量")) or 0),
                            "orders": int(_to_float(row.get("广告订单量")) or 0),
                            "acos": round(cost / sale * 100, 1) if sale else None,
                            "cpc": round(cost / clicks, 2) if clicks else None,
                        }
                    return name, placements
            except Exception as e:  # noqa: BLE001
                logger.warning("placement 解析失败 [%s]: %s", name, e)
            # 回落 Doris（按 campaign_id + shop_id）
            cid = campaign_ids.get(name, "")
            if cid and shop_id:
                placements = await self._get_db()._fetch_campaign_placement_from_db(
                    cid, shop_id, days,
                )
                return name, placements
            return name, {}

        tasks = [_one(name) for name in campaign_ids]
        raw = await asyncio.gather(*tasks, return_exceptions=True)
        for item in raw:
            if isinstance(item, BaseException):
                continue
            name, placements = item
            results[name] = placements
        return results

    async def fetch_search_terms_for(
        self,
        campaign_names: list[str],
        shop_account: str,
        start_date: str = "",
        end_date: str = "",
    ) -> dict[str, list]:
        """按需拉取搜索词报告（仅广泛广告）。"""
        results: dict[str, list] = {}
        sem = self._mcp_sem

        async def _one(name: str):
            try:
                async with sem:
                    res = await self._mcp().campaign_call_tool(
                        "ad_campaign_search_term_report", name, shop_account,
                        start_date=start_date, end_date=end_date,
                    )
                if res.ok:
                    payload = _as_rows(res.value)
                    terms = []
                    for row in payload:
                        terms.append({
                            "keyword": str(row.get("搜索词") or ""),
                            "clicks": int(_to_float(row.get("点击量")) or 0),
                            "cost": _to_float(row.get("花费")) or 0.0,
                            "sales": _to_float(row.get("销售额")) or 0.0,
                            "orders": int(_to_float(row.get("广告订单量")) or 0),
                            "acos": _to_pct(row.get("ACOS")),
                            "cvr": _to_pct(row.get("CVR")),
                        })
                    return name, terms
            except Exception as e:  # noqa: BLE001
                logger.warning("search_term 解析失败 [%s]: %s", name, e)
            return name, []

        tasks = [_one(name) for name in campaign_names]
        raw = await asyncio.gather(*tasks, return_exceptions=True)
        for item in raw:
            if isinstance(item, BaseException):
                continue
            name, terms = item
            results[name] = terms
        return results

    async def discover_new_keywords(
        self,
        parent_asin: str,
        shop_account: str,
        parent_seller_sku: str = "",
        site_code: str = "Amazon_US",
        days: int = 7,
        timeout: float | None = None,
    ) -> tuple[list[dict], list[dict]]:
        """新增活动候选词发现（KB 16）。并行调 flow_keywords + own_keyword_flow。

        复用 mcp_mapping 已注册的 args 构造器 + mcp_adapter.call_tool_timed_with_args。

        返回 (flow_rows, own_rows)，任一失败/无数据返回空 list（上游记 warning 继续）。
        flow_rows 字段（实测中文）: 关键词 / 搜索量 / 搜索人数
        own_rows 字段: 关键词 / 自然排名 等
        """
        from app.data.mcp_mapping import McpContext, build_tool_args, make_date_window

        start_date, end_date = make_date_window(days)
        ctx = McpContext(
            parent_asin=parent_asin,
            parent_seller_sku=parent_seller_sku,
            shop_account=shop_account,
            site_code=site_code,
            start_date=start_date,
            end_date=end_date,
        )
        flow_args = build_tool_args("flow_keywords", ctx)
        own_args = build_tool_args("own_keyword_flow", ctx)
        t = timeout if timeout is not None else getattr(settings, "campaign_mcp_tool_timeout", 300.0)

        flow_res, own_res = await asyncio.gather(
            self._mcp().call_tool_timed_with_args("flow_keywords", flow_args, t),
            self._mcp().call_tool_timed_with_args("own_keyword_flow", own_args, t),
            return_exceptions=True,
        )

        def _rows(res) -> list[dict]:
            if isinstance(res, BaseException) or not getattr(res, "ok", False):
                return []
            return _as_rows(res.value)

        flow_rows = _rows(flow_res)
        own_rows = _rows(own_res)
        logger.info(
            "discover_new_keywords [%s]: flow=%d own=%d",
            parent_asin, len(flow_rows), len(own_rows),
        )
        return flow_rows, own_rows

    async def fetch_suggested_bids(
        self,
        keywords: list[str],
        shop_account: str,
        parent_asin: str,
        parent_seller_sku: str,
        timeout: float | None = None,
    ) -> dict[str, float]:
        """批量查询亚马逊关键词建议竞价（KB 16 §3 数据源）。

        调 whp_amazon_advert_keyword_suggest_bid，一次传全部关键词。
        返回 {keyword_text: suggested_bid} mapping，未命中不留 key。
        """
        import json as _json
        if not keywords:
            return {}
        kw_list = _json.dumps([{"keyword": kw} for kw in keywords])
        args = {
            "shopAccount": shop_account,
            "parentAsin": parent_asin,
            "parentSellerSku": parent_seller_sku,
            "keywordVoList": kw_list,
        }
        t = timeout if timeout is not None else getattr(settings, "campaign_mcp_tool_timeout", 300.0)
        try:
            res = await self._mcp().call_tool_timed_with_args(
                "whp_amazon_advert_keyword_suggest_bid", args, t,
            )
            if res.ok and isinstance(res.value, dict):
                data = res.value.get("data") or {}
                bid_list = data.get("suggestBidList") or []
                out: dict[str, float] = {}
                for row in bid_list:
                    if not isinstance(row, dict):
                        continue
                    kw = str(row.get("keyword") or "").strip()
                    bid = row.get("suggestBid")
                    if kw and bid is not None:
                        try:
                            out[kw] = float(bid)
                        except (TypeError, ValueError):
                            pass
                logger.info("fetch_suggested_bids [%s]: %d/%d hit", parent_asin, len(out), len(keywords))
                return out
        except Exception as e:
            logger.warning("fetch_suggested_bids 失败 [%s]: %s (非阻塞)", parent_asin, e)
        return {}

    # ── 组装 ──

    def _assemble(
        self,
        ctx: dict,
        basic: dict,
        perf: dict,
    ) -> CampaignUnit:
        """合并 Doris 上下文 + MCP 数据 → CampaignUnit。"""
        child_asin = str(ctx.get("child_asin") or "")
        match_type = str(ctx.get("match_type") or "")
        keyword_text = str(ctx.get("keyword_text") or "")

        perf_7d = CampaignPerf(
            clicks=int(_to_float(perf.get("clicks")) or 0),
            cost=_to_float(perf.get("cost")) or 0.0,
            sales=_to_float(perf.get("sale")) or 0.0,
            orders=int(_to_float(perf.get("orders")) or 0),
            acos=perf.get("acos"),
            cvr=perf.get("cvr"),
            cpc=perf.get("cpc"),
            ctr=perf.get("ctr"),
            impressions=int(_to_float(perf.get("impressions")) or 0),
        )

        basic_source = basic.get("source", "mcp")
        perf_source = perf.get("source", "mcp")
        source = "mcp" if basic_source == "mcp" and perf_source == "mcp" else (
            "doris" if basic_source == "doris" and perf_source == "doris" else "mixed"
        )

        # days_online: -1=未知（保持，勿当 0）；basic 缺键时也按 -1
        days_raw = basic.get("days_online", -1)
        days_online = int(days_raw) if days_raw is not None else -1
        campaign_name = str(ctx.get("campaign_name") or "")

        return CampaignUnit(
            campaign_name=campaign_name,
            campaign_key=f"{campaign_name} × {child_asin}",
            campaign_id=str(ctx.get("campaign_id") or ""),
            keyword_id=str(ctx.get("keyword_id") or ""),
            child_asin=child_asin,
            seller_sku=str(ctx.get("seller_sku") or ""),
            keyword_text=keyword_text,
            match_type=match_type,
            current_bid=float(ctx.get("keyword_bid") or 0),
            current_budget=float(basic.get("campaign_budget") or ctx.get("campaign_budget") or 0),
            # status 优先 Doris 英文值（ENABLED）；MCP basic_info 返回中文"启用"，机读不一致
            campaign_status=str(ctx.get("campaign_status") or basic.get("campaign_status") or ""),
            days_online=days_online,
            perf_7d=perf_7d,
            placements={},
            placement_data_available=False,
            tos_bid_pct=float(basic.get("tos_bid_pct") or 0),
            pp_bid_pct=float(basic.get("pp_bid_pct") or 0),
            ros_bid_pct=float(basic.get("ros_bid_pct") or 0),
            source=source,
            flags=[],
        )

    # ── 内部辅助 ──

    def _get_db(self) -> DbAdapter:
        if self._db_adapter is None:
            self._db_adapter = DbAdapter()
        return self._db_adapter

    def _mcp(self) -> McpAdapter:
        if self._mcp_adapter is None:
            self._mcp_adapter = McpAdapter()
        return self._mcp_adapter


# ── 模块级工具 ──

def _to_float(value: Any) -> float | None:
    """容错解析数值：处理 None / 空串 / 带 % / 千分位逗号。失败返回 None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", "").rstrip("%").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _to_pct(value: Any) -> float | None:
    """百分比字段解析，统一归一为百分数口径（25.0 表示 25%）。

    实测（2026-05，am_leigaous 真实活动）：MCP campaign 报告的 ACOS/CVR/CTR
    返回的是**小数口径**（ACOS=0.2997 表示 29.97%），与 Doris 回落
    `cost/sale*100`（百分数 29.97）口径相差 100 倍。故此处统一 ×100。

    - 带 '%' 的字符串视为已是百分数（如 "29.97%"），不再乘。
    - 裸数值（MCP 小数）×100 归一。
    """
    if value is None:
        return None
    is_pct_str = isinstance(value, str) and "%" in value
    f = _to_float(value)
    if f is None:
        return None
    return round(f, 4) if is_pct_str else round(f * 100, 2)


def _to_days_online(value: Any) -> int:
    """活动上线天数。拿不到 → -1（未知），不可返回 0（会误触发"新活动<3天保护"）。"""
    f = _to_float(value)
    if f is None:
        return -1
    return int(f)


def _make_date_window(days: int) -> tuple[str, str]:
    """生成 MCP 日期参数。"""
    return make_date_window(days)


def _as_rows(payload: Any) -> list[dict]:
    """解析 MCP 响应为行列表（与 mcp_normalizers._as_rows 一致）。"""
    if payload is None:
        return []
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("rows", "data", "items", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
        return [payload]
    return []
