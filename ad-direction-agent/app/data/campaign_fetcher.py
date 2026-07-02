"""Campaign 数据编排器 — MCP 主力拉取上下文 + 效果数据，不再回退 Doris。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from app.config.settings import settings
from app.data.campaign_prefilter import filter_campaigns
from app.data.mcp_adapter import McpAdapter
from app.data.mcp_db_context import McpDbContext, _coerce_int, resolve_mcp_context_from_mcp
from app.data.mcp_mapping import make_date_window
from app.models.campaign import CampaignData, CampaignPerf, CampaignUnit

logger = logging.getLogger(__name__)


@dataclass
class _CallResult:
    ok: bool
    value: Any = None
    error: str | None = None


class CampaignFetcher:
    """MCP 主力拉取上下文 + 效果数据，不再回退 Doris。"""

    def __init__(self):
        self._mcp_adapter: McpAdapter | None = None
        self._mcp_sem = asyncio.Semaphore(settings.mcp_max_concurrency)
        self._last_shop_id: int = 0  # fetch_campaigns 解析后缓存，供懒加载回落复用
        self._last_shop_account: str = ""  # 同上，供新增活动线 discover_new_keywords 复用
        self._last_site_code: str = "Amazon_US"  # 同上，供懒加载 placement/search_term 按站点构日期窗口

    # ── 主流程 ──

    async def fetch_campaigns(
        self,
        parent_asin: str,
        days: int = 7,
        *,
        override: dict | None = None,
    ) -> CampaignData:
        """完整 pipeline 入口。

        override（ERP URL 注入 shop_account/parent_seller_sku/site_code/shop_id）传入则
        跳过 MCP 反查，直接用 URL 上下文。
        """
        errors: list[str] = []

        # ① 解析上下文 → child_asins + shop_account（URL 注入优先 → MCP 兜底）
        db_ctx = None
        # URL 注入：override 自带 shop_account/sku/site → 直接构造，不查 MCP/DB
        if override and str(override.get("shop_account") or "").strip():
            db_ctx = McpDbContext(
                parent_asin=parent_asin,
                parent_seller_sku=str(override.get("parent_seller_sku") or "").strip(),
                shop_account=str(override["shop_account"]).strip(),
                shop_id=_coerce_int(override.get("shop_id")),
                site_code=str(override.get("site_code") or settings.mcp_default_site_code or "Amazon_US"),
            )
        # 无 URL 注入 → MCP 解析
        if not db_ctx and settings.mcp_resolve_context:
            db_ctx = await resolve_mcp_context_from_mcp(parent_asin, self._mcp())
        if not db_ctx:
            return CampaignData(
                parent_asin=parent_asin,
                errors=["无法解析 MCP 上下文"],
                fetch_source="mcp",
            )
        shop_id = db_ctx.shop_id or 0
        parent_seller_sku = db_ctx.parent_seller_sku or ""
        site_code = db_ctx.site_code or "Amazon_US"
        # 缓存供新增活动线 discover_new_keywords 复用 (shop_account 不在 CampaignData 上)
        self._last_shop_id = shop_id
        self._last_shop_account = db_ctx.shop_account or ""
        self._last_site_code = site_code  # 供懒加载 placement/search_term 按站点构日期窗口

        # ② 活动+关键词发现：MCP
        shop_account = db_ctx.shop_account or ""
        raw_campaigns: list[dict] = []
        campaign_name_to_id: dict[str, str] = {}
        if settings.mcp_discover_campaigns:
            # 并行：ad_campaign_list (活动清单) + ad_campaign_product_keyword_list (关键词数据)
            list_task = asyncio.create_task(
                self._fetch_campaign_list(parent_asin, parent_seller_sku, shop_account),
            )
            kw_task = asyncio.create_task(
                self._discover_context_from_mcp(parent_asin, shop_account, parent_seller_sku),
            )
            raw_campaigns = await kw_task
            campaign_name_to_id = await list_task
            if raw_campaigns:
                logger.info(
                    "_discover_context [%s]: %d rows (MCP)", parent_asin, len(raw_campaigns),
                )
            else:
                logger.warning(
                    "_discover_context [%s]: MCP 空/失败，跳过 Doris", parent_asin,
                )
                raw_campaigns = []
        else:
            raw_campaigns = []
        if not raw_campaigns:
            return CampaignData(
                parent_asin=parent_asin,
                shop_id=shop_id,
                parent_seller_sku=parent_seller_sku,
                site_code=site_code,
                total_campaigns=0,
                fetch_source="mcp",
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
                fetch_source="mcp",
            )

        # ④ MCP 必拉: basic_info (days_online, 交叉验证 budget/status)
        #    + product_report (7d 效果指标)
        start_date, end_date = _make_date_window(days, db_ctx.site_code)
        shop_account = db_ctx.shop_account

        # listing/shop_id — 从 db_ctx 取（MCP 已解析）
        shop_id = db_ctx.shop_id or 0
        self._last_shop_id = shop_id

        # 唯一活动名（去重）+ campaign_id 映射
        names = sorted({str(c.get("campaign_name") or "") for c in surviving})
        # 构建 id_list：优先用 ad_campaign_list 返回的 id，否则从 surviving 取
        id_list: list[tuple[str, str]] = []
        for name in names:
            cid = campaign_name_to_id.get(name) or str(
                next((c.get("campaign_id") for c in surviving if c.get("campaign_name") == name), "")
            )
            if cid:
                id_list.append((name, cid))

        basic_results: dict[str, dict] = {}
        perf_results: dict[str, dict] = {}
        rank_map: dict[str, dict] = {}
        mcp_ok = 0
        mcp_fail = 0

        # 旁路并行拉自然排名
        has_exact = any(str(c.get("match_type") or "") == "EXACT" for c in surviving)
        rank_task = (
            asyncio.create_task(self._fetch_keyword_ranks(
                parent_asin, parent_seller_sku, shop_account, site_code, days))
            if has_exact else None
        )
        # basic_info 批量（ad_campaign_basic_info_v2: campaign_id_list 逗号分隔，≤20）
        # 开关 campaign_basic_info_v2 控制；V2 稳定后可删除 V1 分支及旧 _fetch_basic_batch
        if settings.campaign_basic_info_v2 and id_list:
            basic_batch_task = asyncio.create_task(
                self._fetch_basic_batch_v2(id_list, shop_account),
            )
        else:
            if settings.campaign_basic_info_v2:
                logger.warning("basic_info [%s]: 无 campaign_id，回退 V1 name 批量", parent_asin)
            basic_batch_task = asyncio.create_task(
                self._fetch_basic_batch(names, shop_account),
            )
        perf_list_raw = await asyncio.gather(*[
            self._fetch_perf_one(name, shop_account, start_date, end_date)
            for name in names
        ], return_exceptions=True)
        basic_dict = await basic_batch_task

        for name in names:
            b = basic_dict.get(name)
            if b is not None:
                basic_results[name] = {**b, "source": "mcp"}
                mcp_ok += 1
            else:
                basic_results[name] = {"campaign_budget": 0.0, "campaign_status": "", "days_online": -1, "tos_bid_pct": 0.0, "pp_bid_pct": 0.0, "ros_bid_pct": 0.0, "source": "mcp_fail"}
                mcp_fail += 1

        for name, res in self._zip_results(names, perf_list_raw):
            if res.ok and isinstance(res.value, dict):
                perf_results[name] = {**res.value, "source": "mcp"}
                mcp_ok += 1
            else:
                perf_results[name] = {"cost": 0.0, "sale": 0.0, "clicks": 0, "impressions": 0, "orders": 0, "source": "mcp_fail"}
                mcp_fail += 1

        if rank_task is not None:
            try:
                rank_map = await rank_task
            except Exception as e:
                logger.warning("自然排名等待失败 [%s]: %s (非阻塞)", parent_asin, e)
                rank_map = {}

        fetch_source = "mcp" if mcp_fail == 0 else "partial"
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
            unit = self._assemble(camp, basic, perf, rank_map)
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

    async def _discover_context_from_mcp(self, parent_asin: str, shop_account: str, parent_seller_sku: str = "") -> list[dict]:
        """① MCP 工具 ad_campaign_product_keyword_list → 替代 Doris 两条 SQL
        (_resolve_and_fetch_listing + _fetch_campaign_context)。
        失败/返回空时返 []，调用方走 Doris 回落。
        """
        try:
            res = await self._mcp().call_tool_timed_with_args(
                "ad_campaign_product_keyword_list",
                {
                    "parent_asin": parent_asin,
                    "parent_seller_sku": parent_seller_sku or "",
                    "shop_account": shop_account or "",
                },
                timeout=getattr(settings, "campaign_mcp_tool_timeout", 300.0),
            )
            if not res.ok:
                logger.warning(
                    "_discover_context_from_mcp [%s] MCP 失败: %s", parent_asin, res.error,
                )
                return []
            # MCP 响应可能多包：{content:[{type:"text", text:"{\"success\":true,...}"}]}
            val = res.value
            if isinstance(val, dict) and "content" in val:
                import json
                for item in val["content"]:
                    txt = item.get("text", "")
                    if isinstance(txt, str):
                        val = json.loads(txt)
                        break
            if isinstance(val, dict) and "success" in val:
                val = val.get("rows", [])  # rows 不存在时回退 []（而非 dict），防下游 _as_rows 误判
            raw = _as_rows(val)
            if not raw:
                return []
            normalized = _normalize_mcp_campaign_keywords(raw)
            logger.info(
                "_discover_context_from_mcp [%s]: %d rows (MCP)",
                parent_asin, len(normalized),
            )
            return normalized
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "_discover_context_from_mcp [%s] 异常: %s", parent_asin, e,
            )
            return []

    async def _fetch_campaign_list(
        self, parent_asin: str, parent_seller_sku: str, shop_account: str,
    ) -> dict[str, str]:
        """调用 ad_campaign_list → 返回 {campaign_name: campaign_id}。

        与 _discover_context_from_mcp 并行调用；返回只有 2 字段，秒回。
        """
        try:
            res = await self._mcp().call_tool_timed_with_args(
                "ad_campaign_list",
                {
                    "parent_asin": parent_asin,
                    "parent_seller_sku": parent_seller_sku or "",
                    "shop_account": shop_account or "",
                },
                timeout=60.0,
            )
            if not res.ok:
                logger.warning(
                    "_fetch_campaign_list [%s] MCP 失败: %s", parent_asin, res.error,
                )
                return {}
            name_to_id: dict[str, str] = {}
            for row in _as_rows(res.value):
                n = str(row.get("广告活动名称") or "").strip()
                cid = str(row.get("广告活动id") or "").strip()
                if n and cid:
                    name_to_id[n] = cid
            logger.info(
                "_fetch_campaign_list [%s]: %d 活动 (MCP)", parent_asin, len(name_to_id),
            )
            return name_to_id
        except Exception as e:  # noqa: BLE001
            logger.warning("_fetch_campaign_list [%s] 异常: %s", parent_asin, e)
            return {}

    _BASIC_BATCH_SIZE = 20  # campaign_id_list 单次上限

    async def _fetch_basic_batch_v2(
        self, id_list: list[tuple[str, str]], shop_account: str,
    ) -> dict[str, dict]:
        """批量调用 ad_campaign_basic_info_v2（campaign_id_list 逗号分隔，每批 ≤20）。

        id_list = [(campaign_name, campaign_id), ...]
        返回 {campaign_name: {campaign_budget, keyword_bid, campaign_status,
               days_online, tos_bid_pct, pp_bid_pct, ros_bid_pct}}。
        不在返回中的活动 → 调用方走 mcp_fail 默认值。
        key 始终用入参的 campaign_name（与 surviving 对齐）。
        """
        results: dict[str, dict] = {}
        id_to_name = {cid: name for name, cid in id_list}

        async def _one(chunk: list[tuple[str, str]]) -> None:
            ids = [cid for _, cid in chunk]
            try:
                res = await self._mcp().campaign_call_tool(
                    "ad_campaign_basic_info_v2", "", shop_account,
                    campaign_id_list=",".join(ids),
                    timeout=420.0,   # 批量 ≤20 活动，比单活动 300s 宽
                )
                if res.ok:
                    # 按入参 id 匹配 MCP 返回行，key 用入参 name
                    for row in _as_rows(res.value):
                        cid = str(row.get("广告活动id") or row.get("campaign_id") or "").strip()
                        name = id_to_name.get(cid)
                        if name:
                            results[name] = {
                                "campaign_budget": _to_float(row.get("广告活动预算")) or 0.0,
                                "keyword_bid": _to_float(row.get("关键词BID")) or 0.0,
                                "campaign_status": str(row.get("状态") or ""),
                                "days_online": _to_days_online(row.get("活动上线天数")),
                                "tos_bid_pct": _to_float(row.get("头部位置加价比例")) or 0.0,
                                "pp_bid_pct": _to_float(row.get("商品位置加价比例")) or 0.0,
                                "ros_bid_pct": _to_float(row.get("其他位置加价比例")) or 0.0,
                            }
                else:
                    logger.warning("basic_info_v2 批量失败 [%d 活动]: %s", len(chunk), res.error)
                    logger.debug("basic_info_v2 批量失败 ids: %s", ids)
            except Exception as e:  # noqa: BLE001
                logger.warning("basic_info_v2 批量异常 [%d 活动]: %s", len(chunk), e)
                logger.debug("basic_info_v2 批量异常 ids: %s", ids)

        batches = [id_list[i:i + self._BASIC_BATCH_SIZE] for i in range(0, len(id_list), self._BASIC_BATCH_SIZE)]
        await asyncio.gather(*[_one(c) for c in batches], return_exceptions=True)
        return results

    _BASIC_BATCH_SIZE = 20  # campaign_id_list 单次上限

    # [V1 保留兼容] 原 _fetch_basic_batch，V2 稳定后可删除
    async def _fetch_basic_batch(
        self, names: list[str], shop_account: str,
    ) -> dict[str, dict]:
        """批量调用 ad_campaign_basic_info（campaign_name_list 逗号分隔，每批 ≤20）。

        返回 {campaign_name: {campaign_budget, keyword_bid, campaign_status,
               days_online, tos_bid_pct, pp_bid_pct, ros_bid_pct}}。
        不在返回中的活动名 → 调用方走 mcp_fail 默认值。
        key 始终用入参 chunk 中的名称（非 MCP 返回名）：两个 MCP 工具的活动名
        可能存在空格/编码差异，跨工具字符串匹配不可靠。
        """
        results: dict[str, dict] = {}
        async def _one(chunk: list[str]) -> None:
            try:
                res = await self._mcp().campaign_call_tool(
                    "ad_campaign_basic_info", "", shop_account,
                    campaign_name_list=",".join(chunk),
                    timeout=420.0,   # 批量 ≤20 活动，比单活动 300s 宽
                )
                if res.ok:
                    # 按入参名匹配 MCP 返回行（归一化去空格），key 用入参名
                    row_by_name: dict[str, dict] = {}
                    for row in _as_rows(res.value):
                        raw = str(row.get("广告活动名称") or "").strip()
                        if raw:
                            row_by_name[raw] = row
                    for name in chunk:
                        row = row_by_name.get(name.strip())
                        if row is not None:
                            results[name] = {
                                "campaign_budget": _to_float(row.get("广告活动预算")) or 0.0,
                                "keyword_bid": _to_float(row.get("关键词BID")) or 0.0,
                                "campaign_status": str(row.get("状态") or ""),
                                "days_online": _to_days_online(row.get("活动上线天数")),
                                "tos_bid_pct": _to_float(row.get("头部位置加价比例")) or 0.0,
                                "pp_bid_pct": _to_float(row.get("商品位置加价比例")) or 0.0,
                                "ros_bid_pct": _to_float(row.get("其他位置加价比例")) or 0.0,
                            }
                else:
                    logger.warning("basic_info 批量失败 [%d 活动]: %s", len(chunk), res.error)
                    logger.debug("basic_info 批量失败 chunk: %s", chunk)
            except Exception as e:  # noqa: BLE001
                logger.warning("basic_info 批量异常 [%d 活动]: %s", len(chunk), e)
                logger.debug("basic_info 批量异常 chunk: %s", chunk)

        batches = [names[i:i + self._BASIC_BATCH_SIZE] for i in range(0, len(names), self._BASIC_BATCH_SIZE)]
        await asyncio.gather(*[_one(c) for c in batches], return_exceptions=True)
        return results

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
            # 回落 Doris 已禁用，返回空
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
        own_rows 字段（live 核实）: 关键词 / 周搜索量 / 自然排位排名 / 自然位排位 / 词的周排名
        """
        from app.data.mcp_mapping import McpContext, build_tool_args, make_date_window

        start_date, end_date = make_date_window(days, site_code)
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

        def _rows(res, label: str) -> list[dict]:
            # 不静默吞错：MCP 异常/返回 not-ok 时打 warning（否则只剩 count=0，查不出原因）
            if isinstance(res, BaseException):
                logger.warning("discover_new_keywords [%s]: %s MCP 异常: %s", parent_asin, label, res)
                return []
            if not getattr(res, "ok", False):
                logger.warning("discover_new_keywords [%s]: %s MCP 返回 not-ok（无数据）", parent_asin, label)
                return []
            # ★payload 级错误：HTTP 200 但工具返回 {success:False, error:...}（如数仓 SQL 报错）。
            #   .ok 只反映 HTTP 层 → 此处必须显式拦，否则错误 dict 被当成一行数据静默吞掉。
            val = res.value
            if isinstance(val, dict) and (val.get("success") is False or val.get("error")):
                logger.warning("discover_new_keywords [%s]: %s 工具返回错误（非数据）: %s",
                               parent_asin, label, str(val.get("error"))[:300])
                return []
            return _as_rows(val)

        flow_rows = _rows(flow_res, "flow_keywords")
        own_rows = _rows(own_res, "own_keyword_flow")
        logger.info(
            "discover_new_keywords [%s]: flow=%d own=%d",
            parent_asin, len(flow_rows), len(own_rows),
        )
        return flow_rows, own_rows

    async def discover_competitor_keywords(
        self,
        parent_asin: str,
        shop_account: str,
        parent_seller_sku: str = "",
        site_code: str = "Amazon_US",
        timeout: float | None = None,
    ) -> list[dict]:
        """竞品词源（KB 16 COMPETITOR_INTERCEPT_WINDOW，reverse-only）。

        direct_competitors → 取前 N 个竞品 ASIN → 并行 seller_sprite_keyword_reverse 反查流量词。
        返回 [{"keyword": str, "search_volume": int|None, "suggested_bid": float|None,
              "competitor_asin": str}, ...]（suggested_bid 来自反查自带 bid，可省 fetch_suggested_bids）。
        硬扇出上限：竞品数 ≤ campaign_new_competitor_max（默认 3），每竞品 1 页 → 总 MCP ≤ ~N+1 次。
        ⚠ 本版**不做** keyword_competitor_flow 逐词打分（那才是数十次扇出炸弹）。
        fail-open：缺 parent_seller_sku / 任一步失败 / 无数据 → 返回空 list（上游记 warning 继续）。
        """
        from app.data.mcp_normalizers import normalize_competitors

        if not (parent_seller_sku or "").strip():
            logger.warning("discover_competitor_keywords [%s]: parent_seller_sku 为空，跳过竞品源", parent_asin)
            return []

        t = timeout if timeout is not None else getattr(settings, "campaign_new_competitor_timeout", 60.0)
        max_comp = getattr(settings, "campaign_new_competitor_max", 3)
        kw_per = getattr(settings, "campaign_new_competitor_kw_per", 100)

        # 1. 取竞品 ASIN（手工构参，对齐 direct_competitors arg builder 的 snake_case 键）
        try:
            comp_res = await self._mcp().call_tool_timed_with_args(
                "direct_competitors",
                {"parent_asin": parent_asin, "parent_seller_sku": parent_seller_sku,
                 "shop_account": shop_account},
                t,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("discover_competitor_keywords [%s]: direct_competitors 失败 %s", parent_asin, e)
            return []
        if isinstance(comp_res, BaseException) or not getattr(comp_res, "ok", False):
            logger.warning("discover_competitor_keywords [%s]: direct_competitors 返回 not-ok（无竞品数据）", parent_asin)
            return []
        comp_asins: list[str] = []
        for r in (normalize_competitors(comp_res.value) or []):
            a = str((r or {}).get("asin") or "").strip()
            if a and a not in comp_asins:
                comp_asins.append(a)
            if len(comp_asins) >= max_comp:
                break
        if not comp_asins:
            logger.info("discover_competitor_keywords [%s]: 无竞品 ASIN", parent_asin)
            return []

        # 2. 并行 reverse 反查（每竞品 1 页；site_code 已是 Amazon_US 全称，两工具直接接受）
        async def _reverse_one(comp_asin: str):
            args = {"asin": comp_asin, "market": site_code, "page": 1, "page_size": kw_per}
            return comp_asin, await self._mcp().call_tool_timed_with_args(
                "seller_sprite_keyword_reverse", args, t,
            )

        results = await asyncio.gather(
            *[_reverse_one(a) for a in comp_asins], return_exceptions=True,
        )
        out: list[dict] = []
        for res in results:
            if isinstance(res, BaseException):
                logger.warning("discover_competitor_keywords [%s]: reverse 任务异常: %s", parent_asin, res)
                continue
            comp_asin, rev = res
            if isinstance(rev, BaseException) or not getattr(rev, "ok", False):
                logger.warning("discover_competitor_keywords [%s]: 竞品 %s reverse 失败/无收录数据", parent_asin, comp_asin)
                continue
            # 真实层级 data.data.list + 真实字段 keyword/searches/bid（live 核实 2026-06-18）
            for row in _reverse_keyword_rows(rev.value):
                kw = str(row.get("keyword") or "").strip()
                if not kw:
                    continue
                sv_raw = row.get("searches")          # 搜索量真实字段名 = searches
                try:
                    sv = int(float(sv_raw)) if sv_raw is not None else None
                except (TypeError, ValueError):
                    sv = None
                bid_raw = row.get("bid")              # bonus：反查自带建议竞价，省 fetch_suggested_bids
                try:
                    bid = float(bid_raw) if bid_raw is not None else None
                except (TypeError, ValueError):
                    bid = None
                out.append({"keyword": kw, "search_volume": sv,
                            "suggested_bid": bid, "competitor_asin": comp_asin})
        logger.info(
            "discover_competitor_keywords [%s]: %d 竞品 → %d 词",
            parent_asin, len(comp_asins), len(out),
        )
        return out

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

    async def _fetch_keyword_ranks(
        self,
        parent_asin: str,
        parent_seller_sku: str,
        shop_account: str,
        site_code: str = "Amazon_US",
        days: int = 7,
    ) -> dict[str, dict]:
        """旁路拉自然排名（周排名），全 MCP 不碰 Doris。

        主源 keyword_child_asins（含近次→周变化），own_keyword_flow 补缺（仅当前排名）。
        返回 {keyword_lower: {natural_rank, near_natural_rank, rank_change}}；失败 fail-open 返回 {}。
        """
        from app.data.mcp_mapping import McpContext, build_tool_args, make_date_window
        from app.data.mcp_normalizers import normalize_keyword_rankings

        start_date, end_date = make_date_window(days, site_code)
        ctx = McpContext(
            parent_asin=parent_asin,
            parent_seller_sku=parent_seller_sku,
            shop_account=shop_account,
            site_code=site_code,
            start_date=start_date,
            end_date=end_date,
        )
        child_args = build_tool_args("keyword_child_asins", ctx)
        own_args = build_tool_args("own_keyword_flow", ctx)
        t = getattr(settings, "campaign_mcp_tool_timeout", 300.0)
        # 墙钟超时在此处（从本协程启动算），与 await 时机无关；超时/异常 → fail-open 返回 {}
        rank_timeout = getattr(settings, "campaign_rank_timeout", 45.0)
        try:
            child_res, own_res = await asyncio.wait_for(
                asyncio.gather(
                    self._mcp().call_tool_timed_with_args("keyword_child_asins", child_args, t),
                    self._mcp().call_tool_timed_with_args("own_keyword_flow", own_args, t),
                    return_exceptions=True,
                ),
                timeout=rank_timeout,
            )
        except Exception as e:  # noqa: BLE001  (含 asyncio.TimeoutError)
            logger.warning("自然排名拉取异常/超时 [%s]: %s (非阻塞)", parent_asin, e)
            return {}

        rank_map: dict[str, dict] = {}
        # 主源：keyword_child_asins（含近次排名 → 周变化）
        if not isinstance(child_res, BaseException) and getattr(child_res, "ok", False):
            for r in normalize_keyword_rankings(None, child_res.value):
                kw = str(r.get("keyword") or "").strip().lower()
                cur = r.get("craw_nature_rank")
                near = r.get("near_craw_nature_rank")
                if not kw or (cur is None and near is None):
                    continue
                change = (near - cur) if (cur is not None and near is not None) else None
                rank_map[kw] = {"natural_rank": cur, "near_natural_rank": near, "rank_change": change}
        # 补缺：own_keyword_flow（仅当前排名，无周变化）
        if not isinstance(own_res, BaseException) and getattr(own_res, "ok", False):
            for row in _as_rows(own_res.value):
                kw = str(row.get("关键词") or row.get("keyword") or "").strip().lower()
                if not kw or kw in rank_map:
                    continue
                # own_keyword_flow 真实字段=自然排位排名/自然位排位（live 核实 2026-06-24）
                cur = _to_float(row.get("自然排位排名") or row.get("自然位排位")
                                or row.get("自然排名") or row.get("natural_rank"))
                if cur is None or cur <= 0:
                    continue
                rank_map[kw] = {"natural_rank": int(cur), "near_natural_rank": None, "rank_change": None}
        logger.info("自然排名 [%s]: %d 词命中 (child+own)", parent_asin, len(rank_map))
        return rank_map

    # ── 组装 ──

    def _assemble(
        self,
        ctx: dict,
        basic: dict,
        perf: dict,
        rank_map: dict | None = None,
    ) -> CampaignUnit:
        """合并 Doris 上下文 + MCP 数据 → CampaignUnit。"""
        child_asin = str(ctx.get("child_asin") or "")
        match_type = str(ctx.get("match_type") or "")
        keyword_text = str(ctx.get("keyword_text") or "")
        # 自然排名仅精准 join（按 keyword 小写归一）
        rk: dict = {}
        if match_type == "EXACT" and rank_map and keyword_text:
            rk = rank_map.get(keyword_text.strip().lower()) or {}

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
        source = "mcp" if basic_source == "mcp" and perf_source == "mcp" else "mixed"

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
            # current_bid 只取 MCP basic_info 的实时出价；MCP 缺失 → 0（不回落 Doris）
            current_bid=float(basic.get("keyword_bid") or 0),
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
            natural_rank=rk.get("natural_rank"),
            near_natural_rank=rk.get("near_natural_rank"),
            rank_change=rk.get("rank_change"),
            source=source,
            flags=[],
        )

    # ── 内部辅助 ──

    def _mcp(self) -> McpAdapter:
        if self._mcp_adapter is None:
            self._mcp_adapter = McpAdapter()
        return self._mcp_adapter


# ── 模块级工具 ──

# ad_campaign_product_keyword_list 中文 key → 英文 key 映射（normalizer）。
# MCP 工具返回的字段与 _fetch_campaign_context SQL 字段一一对应，
# 仅名称不同；映射后下游 _assemble / filter_campaigns 零改动。
_MCP_CAMPAIGN_KEY_MAP: dict[str, str] = {
    "广告活动D": "campaign_id",
    "广告活动ID": "campaign_id",     # MCP 同义别名
    "广告活动名称": "campaign_name",
    "关键词D": "keyword_id",        # 旧字段名（历史兼容）
    "关键词ID": "keyword_id",       # MCP 已改名（2026-07）
    "子SIN": "child_asin",
    "子ASIN": "child_asin",         # MCP 同义别名
    "子卖家KU": "seller_sku",
    "子卖家SKU": "seller_sku",      # MCP 同义别名
    "关键词": "keyword_text",
    "关键词匹配类型": "match_type",
}
_MCP_CAMPAIGN_DEFAULTS = {
    "campaign_status": "ENABLED",
    "keyword_status": "ENABLED",
}


def _normalize_mcp_campaign_keywords(rows: list[dict]) -> list[dict]:
    """将 MCP 工具返回的中文 key 映射为 _assemble 所期望的英文 key。

    入参 shape: [{"广告活动D":..., "广告活动名称":..., ...}, ...]  (MCP 原始)
    出参 shape: [{"campaign_id":..., "campaign_name":..., ..., "campaign_status":"ENABLED", "keyword_status":"ENABLED"}, ...]
    """
    if not rows:
        return []
    out: list[dict] = []
    for r in rows:
        mapped = {_MCP_CAMPAIGN_KEY_MAP.get(k, k): v for k, v in r.items()}
        # match_type 归一为大写：MCP 返小写 "exact"/"broad"/"phrase"，
        # 下游 filter_campaigns / _assemble / campaign stream 全用 == "EXACT" 作精准判定。
        mt = str(mapped.get("match_type") or "")
        if mt:
            mapped["match_type"] = mt.upper()
        mapped.update(_MCP_CAMPAIGN_DEFAULTS)
        out.append(mapped)
    return out


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


def _make_date_window(days: int, site_code: str = "") -> tuple[str, str]:
    """生成 MCP 日期参数（按站点当地时间）。"""
    return make_date_window(days, site_code)


def _reverse_keyword_rows(payload: Any) -> list[dict]:
    """seller_sprite_keyword_reverse 反查词列表解析（live 核实 2026-06-18）。

    真实层级：value["data"]["data"]["list"]（嵌套 dict→dict→list，_as_rows 只解一层够不着）。
    容错：从 payload 起逐层下钻 "data"，命中含 "list" 的节点即返回；niche ASIN 无收录→list 空。
    词条真实字段：keyword / searches(搜索量) / bid,bid_max,bid_min(建议竞价)。
    """
    node = payload
    for _ in range(4):  # 容错：上游若已部分解包，少钻几层也能命中
        if isinstance(node, dict) and isinstance(node.get("list"), list):
            return [x for x in node["list"] if isinstance(x, dict)]
        if isinstance(node, dict) and node.get("data") is not None:
            node = node["data"]
        else:
            break
    return []


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
