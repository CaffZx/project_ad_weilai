"""回归：_prefetch_placement / _prefetch_search_terms 的 `ctx` UnboundLocalError。

bug：`ctx` 仅在 `if not shop_account:` 块内赋值，但日期窗口构造在块外引用 `ctx.site_code`
→ shop_account 已缓存（跳过该块，常见路径）时 `ctx` 未绑定 → UnboundLocalError，
精准/广泛两条流崩溃、大批活动「未被分析」。

修复：site_code 从 fetcher 缓存（主 fetch 已解析）取，不依赖块内 ctx；
本测试钉死：①缓存命中路径不再抛异常 ②日期窗口按缓存站点（非默认 US）构造。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from app.data.mcp_mapping import make_date_window
from app.models.campaign import CampaignUnit
from app.workflow.steps import campaign as C


def _fetcher(site: str = "Amazon_DE") -> MagicMock:
    f = MagicMock()
    f._last_shop_account = "shop"      # 缓存命中 → 跳过 ctx resolve 块（= 原 bug 触发路径）
    f._last_shop_id = 1
    f._last_site_code = site
    return f


def _summaries_lookup():
    cu = CampaignUnit(
        campaign_name="c1", child_asin="B0X", keyword_text="kw",
        campaign_id="cid1", campaign_key="k1",
    )
    return [{"campaign_key": "k1"}], {"k1": cu}


def test_prefetch_placement_no_unbound_ctx_site_aware():
    f = _fetcher("Amazon_DE")
    f.fetch_placement_for = AsyncMock(return_value={})
    summaries, lookup = _summaries_lookup()
    # 缓存命中路径：不得抛 UnboundLocalError('ctx')
    asyncio.run(C._prefetch_placement(f, "B0PARENT", 7, summaries, lookup))
    f.fetch_placement_for.assert_awaited_once()
    kw = f.fetch_placement_for.await_args.kwargs
    # 日期窗口按缓存站点 DE（若错回落 US，多数日期会不等 → 钉住站点感知）
    assert (kw["start_date"], kw["end_date"]) == make_date_window(7, "Amazon_DE")


def test_prefetch_search_terms_no_unbound_ctx_site_aware():
    f = _fetcher("Amazon_DE")
    f.fetch_search_terms_for = AsyncMock(return_value={})
    summaries, lookup = _summaries_lookup()
    asyncio.run(C._prefetch_search_terms(f, "B0PARENT", 7, summaries, lookup))
    f.fetch_search_terms_for.assert_awaited_once()
    kw = f.fetch_search_terms_for.await_args.kwargs
    assert (kw["start_date"], kw["end_date"]) == make_date_window(7, "Amazon_DE")
