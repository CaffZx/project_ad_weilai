"""Campaign 预筛选 — 纯函数，无 I/O。

硬过滤规则（排除不进 LLM）:
  - 否定词 (match_type 含 negative) — 静默丢弃
  - 多关键词活动 (本期暂不处理，后续迭代启用 suggest_split)
  - 已入淘汰池 (Bid ≤ $0.20 且 Budget ≤ $1.00) 的精准活动 — 不进 LLM
    广泛/词组/自动活动仍进入广泛流，由代码收拢至自动广泛组。
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

from app.workflow.steps.campaign_guardrails import is_strictly_in_low_bid_pool
from app.workflow.steps.campaign_portfolio import (
    GROUP_LABEL_TO_CODE,
    PORTFOLIO_ELIMINATE,
    normalize_current_portfolio,
)

if TYPE_CHECKING:
    from app.models.campaign import CampaignUnit


def filter_campaigns(raw: list[dict]) -> tuple[list[dict], list[dict]]:
    """返回 (surviving, excluded)。

    硬过滤:
    1. match_type 含 "negative" → 静默丢弃（否词无 bid/budget 可调）
    2. campaign_name 下 COUNT(DISTINCT keyword_text) > 1
       → reason="multi_keyword_deferred"
       # 本期暂不处理批量关键词活动，后续迭代启用 suggest_split

    注意: budget=$1 的活动不在此排除 -- 留给 LLM 按 KB 规则判断淘汰保护条件。
    """
    # Step 1: 按 campaign_id 统计 ① 去重投放关键词数（排除否定词）② 各子ASIN关联行数
    #         改用 campaign_id 而非 campaign_name：id 是数字主键，跨工具稳定。
    #         否定词（negativeExact/negativePhrase）不计入多词判定——广泛活动常有同词根否词，
    #         若把否词算进去会导致误判为"多关键词活动"。
    def _is_negative_match(mt: str) -> bool:
        return "negative" in (mt or "").lower()

    cid_kw_count: dict[str, set] = {}
    cid_asin_rows: dict[str, Counter] = {}
    cid_name: dict[str, str] = {}  # cid → name（取首次出现的名称）
    for r in raw:
        cid = str(r.get("campaign_id") or "")
        kw = str(r.get("keyword_text") or "")
        mt = str(r.get("match_type") or r.get("关键词匹配类型") or r.get("keyword_match_type") or "")
        ca = str(r.get("child_asin") or "")
        name = str(r.get("campaign_name") or "")
        if cid and kw and not _is_negative_match(mt):
            cid_kw_count.setdefault(cid, set()).add(kw)
        if cid and ca:
            cid_asin_rows.setdefault(cid, Counter())[ca] += 1
        if cid and cid not in cid_name:
            cid_name[cid] = name

    multi_kw_cids = {cid for cid, kws in cid_kw_count.items() if len(kws) > 1}

    surviving: list[dict] = []
    excluded: list[dict] = []
    emitted_multi: set[str] = set()   # 多词活动只产 1 条 excluded（防同活动 N 行重复）

    for r in raw:
        name = str(r.get("campaign_name") or "")
        cid = str(r.get("campaign_id") or "")

        # 规则 1: 否定词不进入 LLM 分析（否词无 bid/budget 可调，误入会浪费 token 且产生无效分析）
        if _is_negative_match(str(r.get("match_type") or r.get("关键词匹配类型") or r.get("keyword_match_type") or "")):
            continue

        # 规则 2: 多关键词活动（本期不进 LLM，前端展示为预过滤卡）
        #   - 折叠：每活动只产 1 条 excluded（raw 按词/子ASIN 多行，否则会 N 条重复）
        #   - 子ASIN：取该活动下关联词条数最多的子ASIN（prefilter 无 perf，"花费最高"不可得）
        #   - keyword_text 硬编码"多关键词活动"
        if cid in multi_kw_cids:
            if cid not in emitted_multi:
                emitted_multi.add(cid)
                top_asin = ""
                if cid_asin_rows.get(cid):
                    top_asin = cid_asin_rows[cid].most_common(1)[0][0]
                excluded.append({
                    "campaign_name": name,
                    "campaign_id": cid,
                    "child_asin": top_asin,
                    "match_type": str(r.get("match_type") or ""),
                    "keyword_text": "多关键词活动",
                    "keyword_count": len(cid_kw_count[cid]),
                    "reason": "多关键词活动，请到ERP手动修改",
                    "__prefiltered": True,
                })
            continue

        surviving.append(r)

    return surviving, excluded


def filter_eliminated_pool(
    units: list[CampaignUnit],
) -> tuple[list[CampaignUnit], list[dict], list[CampaignUnit]]:
    """筛选已入淘汰池的活动（Bid ≤ $0.20 且 Budget ≤ $1.00，KB 21 §6）。

    在 CampaignFetcher 组装 CampaignUnit 之后调用（依赖 MCP basic_info 拿到的 current_bid/current_budget）。

    Returns:
        surviving:  未入池，送入 LLM 分析
        skipped:    已入池，dict 含 __prefiltered=True 供前端灰卡渲染
        pool_units: 已入池，保留 CampaignUnit 供 KB21§7 淘汰复评
    """
    surviving: list[CampaignUnit] = []
    skipped: list[dict] = []
    pool_units: list[CampaignUnit] = []

    for cu in units:
        if (cu.match_type or "").upper() in {"BROAD", "PHRASE", "AUTO"}:
            surviving.append(cu)
            continue
        if is_strictly_in_low_bid_pool(cu.current_bid, cu.current_budget):
            pool_units.append(cu)
            skipped.append({
                "campaign_key": cu.campaign_key,
                "campaign_name": cu.campaign_name,
                "child_asin": cu.child_asin,
                "match_type": cu.match_type,
                "keyword_text": cu.keyword_text,
                "reason": "已入淘汰池（出价≤$0.20 且 预算≤$1），请到ERP手动修改",
                "__prefiltered": True,
            })
        else:
            surviving.append(cu)

    return surviving, skipped, pool_units


def split_low_bid_non_exact_for_pause(
    units: list[CampaignUnit],
) -> tuple[list[CampaignUnit], list[CampaignUnit]]:
    """Split standard low-bid non-EXACT campaigns before any LLM call."""
    low_bid_code = GROUP_LABEL_TO_CODE[PORTFOLIO_ELIMINATE]
    surviving: list[CampaignUnit] = []
    pause_units: list[CampaignUnit] = []
    for unit in units:
        match_type = str(unit.match_type or "").strip().upper()
        current = normalize_current_portfolio(unit.current_portfolio_name)
        if match_type in {"BROAD", "PHRASE", "AUTO"} and current == low_bid_code:
            pause_units.append(unit)
        else:
            surviving.append(unit)
    return surviving, pause_units
