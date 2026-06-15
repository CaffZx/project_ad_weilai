"""淘汰活动复评（重启）— KB 21 §7，纯确定性规则引擎（无 LLM、无 I/O）。

输入全部预取（入池日期 / 在池窗口订单 / 父ASIN精准30d均CPC 由 campaign.py 门控抓取后注入），
输出 reactivate 的 CampaignAdjustmentItem[]，复用既有 adjustment → pending → 落库 → 渲染链。

KB 21 §7：
  情况一 REACTIVATE_BUDGET_ONLY        — 在池窗口出单 ≥1 → 预算 $1→$3，Bid $0.20 不变，AUTO_BATCHABLE
  情况二 REACTIVATE_WITH_CALIBRATED_BID — 在池窗口 0 单 且 淘汰前7d花费 >$15 → 预算 $1→$3，
                                          Bid=min(0.5, 父ASIN精准30d均CPC)，MANUAL_REVIEW
  否则（0 单且花费≤$15或未知，或无淘汰记录，或不足 N 天）→ 不产出，留灰卡

★订单窗口（评审 #1）：只数「在低价捡漏组中」的订单 = 近 min(入池天数, 上限) 天，绝不平铺 30d。
"""

from __future__ import annotations

import logging
from datetime import date, datetime

from app.config.settings import settings
from app.models.campaign import CampaignAdjustmentItem, CampaignUnit
from app.workflow.steps.campaign_portfolio import PORTFOLIO_BROAD, PORTFOLIO_TEST

logger = logging.getLogger(__name__)

REACTIVATE_BUDGET_ONLY = "reactivate_budget_only"
REACTIVATE_WITH_CALIBRATED_BID = "reactivate_with_calibrated_bid"
_BROAD_MATCH_TYPES = {"BROAD", "PHRASE", "AUTO"}


def _as_date(v) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return None


def _restart_group(match_type: str | None) -> str:
    """重启后归组（KB §7 落精准测试/自动广泛组）。

    不用 campaign_portfolio.classify()——它按当前 $1/$0.20 状态会把活动判回低价捡漏组；
    复评后该活动离开淘汰池，按 match_type 直接定组。
    """
    return PORTFOLIO_BROAD if (match_type or "").upper() in _BROAD_MATCH_TYPES else PORTFOLIO_TEST


def restart_orders_window_days(days_in_elimination: int) -> int:
    """在池订单窗口 = min(入池天数, 上限)（评审 #1：裁到在池区间 ∩ 近30天）。"""
    return max(1, min(days_in_elimination, settings.campaign_restart_orders_window_max_days))


def analyze_eliminated_restart(
    pool_units: list[CampaignUnit],
    entry_records: dict[str, dict],
    orders_inpool: dict[str, int],
    exact_cpc_30d: float | None,
    *,
    today: date | None = None,
) -> tuple[list[CampaignAdjustmentItem], set[str]]:
    """对严格在池活动做 KB 21 §7 复评，返回 (reactivate_items, reactivated_campaign_keys)。

    Args:
        pool_units: 严格在池活动（is_strictly_in_low_bid_pool 命中）。
        entry_records: {campaign_id: {"entry_date": date|datetime, "eliminate_spend_7d": float|None}}。
        orders_inpool: {campaign_id: 在池窗口订单数}（窗口 = restart_orders_window_days）。
        exact_cpc_30d: 父ASIN精准30d均CPC（情况二定价用）；无则 None。
        today: 复评基准日（默认今天）；测试可注入。
    """
    today = today or date.today()
    items: list[CampaignAdjustmentItem] = []
    reactivated: set[str] = set()
    if not settings.campaign_restart_enabled:
        return items, reactivated

    budget = settings.campaign_restart_budget
    bid_floor = settings.campaign_restart_bid_floor
    bid_cap = settings.campaign_restart_bid_cap
    high_spend = settings.campaign_restart_high_spend_7d
    review_days = settings.campaign_restart_review_days

    for cu in pool_units:
        cid = (cu.campaign_id or "").strip()
        rec = entry_records.get(cid) if cid else None
        entry_date = _as_date(rec.get("entry_date")) if rec else None
        if entry_date is None:
            continue  # 无 CONFIRMED 淘汰记录 / 入池日期不可得 → 留灰卡（不臆造）
        days_in_elim = (today - entry_date).days
        if days_in_elim < review_days:
            continue  # 不足复评窗口

        window = restart_orders_window_days(days_in_elim)
        orders = int(orders_inpool.get(cid, 0) or 0)
        spend7 = rec.get("eliminate_spend_7d")

        common_evidence = [
            f"淘汰入池日期：{entry_date.isoformat()}",
            f"已在低价捡漏组：{days_in_elim} 天（复评阈值 {review_days} 天）",
            f"在池窗口订单（近 {window} 天，仅数在池期间）：{orders}",
        ]

        if orders >= 1:
            # ── 情况一：偶然出单 → 仅恢复预算 ──
            item = _build_item(
                cu, action=REACTIVATE_BUDGET_ONLY, scene="REACTIVATE_BUDGET_ONLY",
                proposed_budget=budget, proposed_bid=bid_floor,
                review_level="AUTO_BATCHABLE", confidence="high",
                reason=(f"复评·情况一：入池 {days_in_elim} 天，近 {window} 天在低价捡漏组"
                        f"（$0.20/$1）仍出单 {orders} 单，词有基础信号 → 恢复日预算至 ${budget:.0f}，"
                        f"Bid 维持 $0.20（数据不足，不提价）。"),
                evidence=common_evidence + ["依据：KB 21 §7 情况一 REACTIVATE_BUDGET_ONLY"],
            )
            items.append(item)
            reactivated.add(cu.campaign_key)
            continue

        # orders == 0
        if spend7 is None or float(spend7) <= high_spend:
            continue  # 0 单且淘汰前花费不高/未知 → 维持淘汰，不产出

        # ── 情况二：历史高花费但转化差 → 恢复预算 + 重新定价 ──
        if exact_cpc_30d is not None and exact_cpc_30d > 0:
            proposed_bid = max(bid_floor, min(bid_cap, float(exact_cpc_30d)))
            bid_note = (f"父ASIN精准30天均CPC ${exact_cpc_30d:.2f} → "
                        f"proposed_bid=max(${bid_floor:.2f}, min(${bid_cap:.1f}, ${exact_cpc_30d:.2f}))"
                        f"=${proposed_bid:.2f}")
        else:
            proposed_bid = bid_floor
            bid_note = "父ASIN无精准CPC数据 → Bid 暂维持 $0.20，转人工复核定价"

        item = _build_item(
            cu, action=REACTIVATE_WITH_CALIBRATED_BID, scene="REACTIVATE_WITH_CALIBRATED_BID",
            proposed_budget=budget, proposed_bid=proposed_bid,
            review_level="MANUAL_REVIEW", confidence="medium",
            reason=(f"复评·情况二：入池 {days_in_elim} 天，近 {window} 天在池 0 单 且 "
                    f"淘汰前 7 天花费 ${float(spend7):.2f}>${high_spend:.0f}（曾高花费低转化）→ "
                    f"恢复日预算至 ${budget:.0f}，按精准均CPC重定价 Bid ${proposed_bid:.2f}。"),
            evidence=common_evidence + [
                f"淘汰前 7 天花费：${float(spend7):.2f}（> ${high_spend:.0f} 触发重定价）",
                bid_note,
                "依据：KB 21 §7 情况二 REACTIVATE_WITH_CALIBRATED_BID",
            ],
        )
        items.append(item)
        reactivated.add(cu.campaign_key)

    if items:
        logger.info("Campaign 复评：产出 %d 张复评卡（候选池 %d）", len(items), len(pool_units))
    return items, reactivated


def _build_item(
    cu: CampaignUnit, *, action: str, scene: str,
    proposed_budget: float, proposed_bid: float,
    review_level: str, confidence: str, reason: str, evidence: list[str],
) -> CampaignAdjustmentItem:
    """组装复评 CampaignAdjustmentItem（current 透传真实池值；占位字段给空）。"""
    return CampaignAdjustmentItem(
        campaign_name=cu.campaign_name,
        campaign_key=cu.campaign_key,
        campaign_id=cu.campaign_id,
        child_asin=cu.child_asin,
        seller_sku=cu.seller_sku,
        keyword_text=cu.keyword_text,
        keyword_id=cu.keyword_id,
        match_type=cu.match_type,
        action=action,
        triggered_rule=scene,
        reason=reason,
        evidence=evidence,
        confidence=confidence,
        current_budget=cu.current_budget,   # 透传真实池值（≈$1）
        current_bid=cu.current_bid,         # 透传真实池值（≈$0.20）
        proposed_budget=proposed_budget,
        proposed_bid=proposed_bid,
        placement_adjustments=[],           # 占位（复评不调广告位）
        negative_keywords=[],               # 占位
        review_level=review_level,
        ai_portfolio_class=_restart_group(cu.match_type),
        perf_7d=cu.perf_7d.model_dump() if cu.perf_7d else {},
    )
