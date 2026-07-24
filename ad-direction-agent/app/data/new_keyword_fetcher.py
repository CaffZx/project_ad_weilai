"""NewKeywordFetcher — 新增扩词数据编排层。

flow_keywords (SR) + own_keyword_flow (SR) → 硬过滤 + 预选
→ erp_listing_asin_keyword_rank_history (AZ, 逐词 enrichment)
→ whp_amazon_advert_keyword_suggest_bid (SR, 批量)
→ NewKeywordData

所有 MCP 调用经 McpAdapter → registry 路由。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from app.config.settings import settings
from app.models.campaign import NewKeywordData, NewKeywordRecord

if TYPE_CHECKING:
    from app.models.campaign import CampaignStrategyContext

logger = logging.getLogger(__name__)

_MIN_SEARCH_VOLUME = 100
_MAX_KEYWORD_TOKENS = 10
_LONGTAIL_TOKEN_CAP = 5
_TOP_N = 60  # 预选上限，给排序留余量


def _is_noise_keyword(kw: str) -> bool:
    """噪声过滤：空、<3字符、纯数字符号、单字母、>10词。"""
    import re
    s = (kw or "").strip()
    _NOISE_RE = re.compile(r"^[\d\W_]+$|^[a-zA-Z]$")
    return not kw or len(kw) < 3 or bool(_NOISE_RE.match(s)) or len(s.split()) > _MAX_KEYWORD_TOKENS


def _to_int_or_none(v):
    try:
        return int(float(v)) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


class NewKeywordFetcher:
    def __init__(self):
        self._mcp_adapter = None

    def _mcp(self):
        if self._mcp_adapter is None:
            from app.data.mcp_adapter import McpAdapter
            self._mcp_adapter = McpAdapter()
        return self._mcp_adapter

    async def fetch(
        self,
        parent_asin: str,
        shop_account: str,
        parent_seller_sku: str = "",
        site_code: str = "Amazon_US",
        *,
        existing_keywords: set[str] | None = None,
        target_child_asin: str = "",
        pre_eliminated_count: int = 0,
        strategy_context: "CampaignStrategyContext | None" = None,
        days: int = 7,
    ) -> NewKeywordData:
        existing = existing_keywords or set()
        errors: list[str] = []

        # ── Step 1: flow + own 并行 ──
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
        t = getattr(settings, "campaign_mcp_tool_timeout", 300.0)

        flow_res, own_res = await asyncio.gather(
            self._mcp().call_tool_timed_with_args("flow_keywords", flow_args, t),
            self._mcp().call_tool_timed_with_args("own_keyword_flow", own_args, t),
            return_exceptions=True,
        )

        def _rows(res, label: str) -> list[dict]:
            from app.data.mcp_normalizers import _as_rows
            if isinstance(res, BaseException):
                logger.warning("NewKeywordFetcher [%s]: %s MCP 异常: %s", parent_asin, label, res)
                return []
            if not getattr(res, "ok", False):
                logger.warning("NewKeywordFetcher [%s]: %s MCP 返回 not-ok", parent_asin, label)
                return []
            val = res.value
            if isinstance(val, dict) and (val.get("success") is False or val.get("error")):
                logger.warning("NewKeywordFetcher [%s]: %s 工具返回错误: %s",
                               parent_asin, label, str(val.get("error"))[:300])
                return []
            return _as_rows(val)

        flow_rows = _rows(flow_res, "flow_keywords")
        own_rows = _rows(own_res, "own_keyword_flow")
        logger.info("NewKeywordFetcher [%s]: flow=%d own=%d", parent_asin, len(flow_rows), len(own_rows))

        # ── Step 2: 解析 own — 预过滤 + 周维数据 ──
        own_rank_map: dict[str, int] = {}
        own_tier_map: dict[str, str] = {}
        own_week_map: dict[str, dict] = {}
        for r in own_rows:
            kw = str(r.get("关键词") or r.get("keyword") or "").strip().lower()
            if not kw:
                continue
            rank = _to_int_or_none(
                r.get("自然排位排名") or r.get("自然位排位")
                or r.get("自然排名") or r.get("natural_rank")
            )
            if rank is not None:
                own_rank_map[kw] = rank
            tier = str(r.get("自然位排位") or r.get("natural_rank_position") or "")
            if tier:
                own_tier_map[kw] = tier
            wk = _to_int_or_none(r.get("词的周排名") or r.get("week_rank"))
            wsv = _to_int_or_none(r.get("周搜索量") or r.get("week_search_volume"))
            if wk is not None or wsv is not None:
                own_week_map[kw] = {"week_rank": wk, "week_search_volume": wsv}

        # ── Step 3: flow 硬过滤 ──
        kw_records: dict[str, NewKeywordRecord] = {}
        search_volume_map: dict[str, int] = {}

        for r in flow_rows:
            kw = str(r.get("关键词") or r.get("keyword") or "").strip().lower()
            if not kw:
                continue
            try:
                sv = int(float(r.get("搜索量") or r.get("search_volume") or 0))
            except (TypeError, ValueError):
                sv = 0
            # 全量搜索量 map（含已投词，预算回算用）
            search_volume_map[kw] = sv
            # 硬过滤
            if kw in existing or _is_noise_keyword(kw) or sv < _MIN_SEARCH_VOLUME:
                continue
            sr = _to_int_or_none(r.get("搜索排名") or r.get("search_rank") or r.get("searchesRank"))
            wk = own_week_map.get(kw, {})
            record = NewKeywordRecord(
                keyword_text=kw,
                search_volume=sv,
                search_rank=sr,
                week_rank=wk.get("week_rank"),
                week_search_volume=wk.get("week_search_volume"),
                own_natural_rank=own_rank_map.get(kw),
                own_rank_tier=own_tier_map.get(kw),
            )
            kw_records[kw] = record

        # ── Step 4: own-only 补漏 (自然位 28-48, flow 没有的词) ──
        for kw, rank in own_rank_map.items():
            if kw in kw_records or kw in existing or _is_noise_keyword(kw):
                continue
            if not (28 <= rank <= 48):
                continue
            wk = own_week_map.get(kw, {})
            kw_records[kw] = NewKeywordRecord(
                keyword_text=kw,
                search_volume=0,
                week_rank=wk.get("week_rank"),
                week_search_volume=wk.get("week_search_volume"),
                own_natural_rank=rank,
                own_rank_tier=own_tier_map.get(kw),
                is_own_only=True,
            )

        records = list(kw_records.values())
        total_discovered = len(flow_rows)
        total_own = len(own_rows)
        if not records:
            return NewKeywordData(
                parent_asin=parent_asin, parent_seller_sku=parent_seller_sku,
                site_code=site_code, shop_account=shop_account,
                search_volume_map=search_volume_map,
                total_discovered=total_discovered, total_own=total_own,
                errors=errors,
            )

        # ── Step 5: 排序 → Top-N ──
        # 28-48 自然位且搜索量 ≥500 的词优先占用 Top-N 配额；
        # 超额时按搜索量降序取满，避免突破 history 查询上限。
        _MIN_OPPORTUNITY_SV = 500
        guaranteed = [r for r in records
                      if r.own_natural_rank is not None and 28 <= r.own_natural_rank <= 48
                      and (r.search_volume or 0) >= _MIN_OPPORTUNITY_SV]
        guaranteed.sort(key=lambda r: (-(r.search_volume or 0), r.keyword_text))
        guaranteed = guaranteed[:_TOP_N]
        rest = [r for r in records if r not in guaranteed]

        def _sort_key(r: NewKeywordRecord):
            tokens = min(len((r.keyword_text or "").split()), _LONGTAIL_TOKEN_CAP)
            return (r.own_natural_rank is None, -tokens, -(r.search_volume or 0))

        rest.sort(key=_sort_key)
        quota = _TOP_N - len(guaranteed)
        records = guaranteed + rest[:max(0, quota)]
        logger.info("NewKeywordFetcher [%s]: %d guaranteed + %d ranked = %d (top-%d)",
                     parent_asin, len(guaranteed), len(records) - len(guaranteed), len(records), _TOP_N)

        # ── Step 6: history enrichment (AZ, 逐词, 信号量限流) ──
        az_sem = asyncio.Semaphore(getattr(settings, "azlisting_mcp_max_in_flight", 5))
        az_timeout = getattr(settings, "azlisting_mcp_timeout", 60.0)
        enrichment_timeout = getattr(settings, "campaign_new_enrichment_timeout", 600.0)

        # 7 天窗口日期（按站点时区，复用统一口径）
        hist_start, hist_end = make_date_window(7, site_code)

        async def _enrich_one(record: NewKeywordRecord) -> None:
            child_asin = target_child_asin or ""
            if not child_asin:
                for r in own_rows:
                    if str(r.get("关键词") or r.get("keyword") or "").strip().lower() == record.keyword_text:
                        child_asin = str(r.get("ASIN") or r.get("asin") or "")
                        break
            if not child_asin:
                child_asin = parent_asin
            async with az_sem:
                try:
                    res = await asyncio.wait_for(
                        self._mcp().call_tool_timed_with_args(
                            "erp_listing_asin_keyword_rank_history",
                            {
                                "asin": child_asin,
                                "keyword": record.keyword_text,
                                "siteCode": site_code.replace("Amazon_", ""),
                                "startDate": hist_start,
                                "endDate": hist_end,
                            },
                            az_timeout,
                        ),
                        timeout=az_timeout + 5,
                    )
                except asyncio.TimeoutError:
                    record.history_state = "query_failed"
                    return
                except Exception as e:
                    logger.debug("history enrichment [%s][%s]: %s", parent_asin, record.keyword_text, e)
                    record.history_state = "query_failed"
                    return
            if not (isinstance(res, object) and getattr(res, "ok", False)):
                record.history_state = "query_failed"
                return
            data = res.value
            if isinstance(data, dict) and "data" in data:
                rows = data["data"]
            elif isinstance(data, dict) and "rows" in data:
                rows = data["rows"]
            else:
                rows = _as_rows_safe(data)
            if not rows or not isinstance(rows, list):
                record.history_state = "empty"
                return
            # 最新日
            latest = rows[-1] if isinstance(rows, list) else None
            if not isinstance(latest, dict):
                record.history_state = "empty"
                return
            record.natural_rank = _to_int_or_none(latest.get("crawNatureRank"))
            record.rank_tier = str(latest.get("crawNatureRankPosition") or "")
            record.sponsored_rank = _to_int_or_none(latest.get("crawSpRank"))
            # 7 天趋势: "5→6→9→3→3→6→6"
            trend_parts: list[str] = []
            for day in rows:
                if not isinstance(day, dict):
                    continue
                nr = _to_int_or_none(day.get("crawNatureRank"))
                trend_parts.append(str(nr) if nr is not None else "-")
            if trend_parts:
                record.rank_trend = "→".join(trend_parts)
            record.history_state = "ok"

        async def _enrich_all():
            eligible = [r for r in records if r.own_natural_rank is not None]
            for r in records:
                if r.own_natural_rank is None:
                    r.history_state = "not_eligible"
            if not eligible:
                return
            tasks = [asyncio.create_task(_enrich_one(r)) for r in eligible]
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=enrichment_timeout,
                )
            except asyncio.TimeoutError:
                logger.warning("NewKeywordFetcher [%s]: history enrichment 总超时 %.0fs",
                               parent_asin, enrichment_timeout)
            # 统计
            ok = sum(1 for r in records if r.history_state == "ok")
            logger.info("NewKeywordFetcher [%s]: history enriched %d/%d", parent_asin, ok, len(records))

        await _enrich_all()

        # ── Step 7: source 标注 ──
        for r in records:
            if r.is_own_only:
                r.source = "ranking_opportunity"
                r.source_reason = f"自然位{r.own_natural_rank}机会词(own only)"
            elif r.own_natural_rank is not None and 28 <= r.own_natural_rank <= 48:
                r.source = "ranking_opportunity"
                r.source_reason = f"自然位{r.own_natural_rank}机会词(搜索量{r.search_volume})"
            else:
                r.source = "flow"
                r.source_reason = f"流量词库(搜索量{r.search_volume})"

        return NewKeywordData(
            parent_asin=parent_asin,
            parent_seller_sku=parent_seller_sku,
            site_code=site_code,
            shop_account=shop_account,
            records=records,
            search_volume_map=search_volume_map,
            total_discovered=total_discovered,
            total_own=total_own,
            history_queried=sum(1 for r in records if r.history_state != "not_queried"),
            history_success=sum(1 for r in records if r.history_state == "ok"),
            errors=errors,
        )


def _as_rows_safe(val) -> list[dict]:
    try:
        from app.data.mcp_normalizers import _as_rows
        return _as_rows(val)
    except Exception:
        return []
