"""Build LLM/report data summary from ASINData."""

from __future__ import annotations

from app.models.asin_data import ASINData
from app.workflow.data_contract import CompletenessVerdict, merge_completeness_into_summary
from app.workflow.helpers import (
    format_asin_daily_trend,
    kw_to_ai_summary,
    kw_trend_priority,
)


def build_data_summary(
    data: ASINData,
    days: int = 7,
    verdict: CompletenessVerdict | None = None,
) -> dict:
    """从ASINData提取数据摘要

    包含5层:
    1. 基础聚合指标
    2. ASIN 日趋势（时间维度）
    3. 关键词级洞察（按趋势+表现选词，含前后半段 ACOS 对比）
    4. 竞品摘要
    5. 广告位对比
    """
    summary = {"analysis_days": days}

    # ── 1. 基础聚合指标 ──
    summary["keyword_count"] = data.keyword_count or len(data.keywords)
    summary["acos"] = round(data.ad_data.acos, 1) if data.ad_data and data.ad_data.acos is not None else None
    summary["tacos"] = round(data.ad_data.tacos, 1) if data.ad_data and data.ad_data.tacos is not None else None
    summary["cvr"] = round(data.ad_data.cvr, 1) if data.ad_data and data.ad_data.cvr is not None else None
    summary["cpc"] = round(data.ad_data.cpc, 2) if data.ad_data and data.ad_data.cpc is not None else None
    summary["spend"] = round(data.ad_data.spend, 2) if data.ad_data and data.ad_data.spend is not None else None
    # acos_target 仅在运营手动设定时才存在，不设置默认值（错误的值比未知危害更大）
    summary["top_keyword_rank"] = min(
        (kw.natural_rank for kw in data.keywords if kw.natural_rank), default=None
    )
    summary["natural_order_ratio"] = round(data.natural_order_ratio, 1) if data.natural_order_ratio is not None else None
    summary["available_new_keywords"] = data.available_new_keywords
    summary["inventory_days"] = round(data.signals.inventory_days, 1) if data.signals and data.signals.inventory_days is not None else None
    summary["days_since_launch"] = data.days_since_launch
    summary["daily_ad_spend_ratio"] = round(data.ad_data.daily_ad_spend_ratio, 1) if data.ad_data and data.ad_data.daily_ad_spend_ratio is not None else None
    summary["avg_daily_sales_30d"] = round(data.avg_daily_sales_30d, 1) if data.avg_daily_sales_30d is not None else None
    summary["margin"] = round(data.margin * 100, 1) if data.margin is not None else None

    # ── 2. ASIN 日趋势 ──
    daily_trend, trend_text = format_asin_daily_trend(data, days)
    summary["daily_trend"] = daily_trend
    summary["daily_trend_text"] = trend_text

    # ── 3. 关键词级洞察（时间维度选词）──
    valid_kws = [kw for kw in data.keywords if kw.keyword]

    # 高ACOS词 TOP5（近N天整体 ACOS 高，且优先含 ACOS 恶化趋势）
    acos_candidates = [kw for kw in valid_kws if kw.acos is not None and kw.acos > 0]
    acos_sorted = sorted(
        acos_candidates,
        key=lambda x: (
            (x.acos or 0),
            kw_trend_priority(x),
            abs((x.acos_recent or 0) - (x.acos_prior or 0)),
        ),
        reverse=True,
    )[:5]
    summary["high_acos_keywords"] = [kw_to_ai_summary(kw, days) for kw in acos_sorted]

    # 上升词 TOP5（按排名变化幅度）
    rising_sorted = sorted(
        [kw for kw in valid_kws if kw.rank_change_14d is not None and kw.rank_change_14d > 0],
        key=lambda x: x.rank_change_14d or 0,
        reverse=True,
    )[:5]
    summary["rising_keywords"] = [kw_to_ai_summary(kw, days) for kw in rising_sorted]

    # 高花费零转化词
    wasteful = sorted(
        [kw for kw in valid_kws if kw.spend >= 15 and kw.orders == 0],
        key=lambda x: x.spend or 0,
        reverse=True,
    )
    summary["wasteful_keywords"] = [kw_to_ai_summary(kw, days) for kw in wasteful[:5]]

    # 高转化词 TOP5（至少3单；优先近期订单上升）
    cvr_candidates = [
        kw for kw in valid_kws if kw.cvr is not None and kw.cvr > 0 and kw.orders >= 3
    ]
    cvr_sorted = sorted(
        cvr_candidates,
        key=lambda x: (
            x.cvr or 0,
            (x.orders_recent or 0) - (x.orders_prior or 0),
        ),
        reverse=True,
    )[:10]
    summary["top_cvr_keywords"] = [kw_to_ai_summary(kw, days) for kw in cvr_sorted]

    summary["expand_keyword_candidates"] = getattr(data, "expand_keyword_candidates", None) or []

    # 趋势异动词 TOP5（ACOS/花费/排名变化最显著，供 AI 重点判断）
    seen = {kw.keyword for kw in acos_sorted + rising_sorted + wasteful[:5] + cvr_sorted}
    trend_pool = [kw for kw in valid_kws if kw_trend_priority(kw) > 0 and kw.keyword not in seen]
    trend_sorted = sorted(trend_pool, key=kw_trend_priority, reverse=True)[:5]
    summary["keyword_trend_watch"] = [kw_to_ai_summary(kw, days) for kw in trend_sorted]

    # ── 3. 竞品摘要 ──
    comps = data.competitors
    if comps:
        prices = [c.price for c in comps if c.price]
        ratings = [c.rating for c in comps if c.rating]
        our_price = data.price
        price_comp = ""
        if prices and our_price:
            avg_comp = sum(prices) / len(prices)
            if our_price < avg_comp * 0.85:
                price_comp = "低于竞品均价"
            elif our_price > avg_comp * 1.15:
                price_comp = "高于竞品均价"
            else:
                price_comp = "与竞品均价持平"

        summary["competitor_summary"] = {
            "competitor_count": len(comps),
            "price_range": f"${min(prices):.2f} - ${max(prices):.2f}" if prices else None,
            "avg_rating": round(sum(ratings) / len(ratings), 1) if ratings else None,
            "our_price": our_price,
            "price_position": price_comp,
        }
    else:
        summary["competitor_summary"] = None

    # ── 4. 广告位对比（TOS vs ROS，来自 placement_report）──
    ad = data.ad_data
    if ad and (ad.placement_tos_acos is not None or ad.placement_ros_acos is not None):
        summary["placement_comparison"] = {
            "tos_acos": round(ad.placement_tos_acos, 1) if ad.placement_tos_acos else None,
            "ros_acos": round(ad.placement_ros_acos, 1) if ad.placement_ros_acos else None,
            "tos_cpc": round(ad.placement_tos_cpc, 2) if ad.placement_tos_cpc else None,
            "ros_cpc": round(ad.placement_ros_cpc, 2) if ad.placement_ros_cpc else None,
            "tos_spend_ratio": round(ad.placement_tos_spend_ratio, 1) if ad.placement_tos_spend_ratio else None,
            "ros_spend_ratio": round(ad.placement_ros_spend_ratio, 1) if ad.placement_ros_spend_ratio else None,
        }
    else:
        summary["placement_comparison"] = None

    # 数据质量（无 verdict 时保持兼容）
    if verdict is None:
        summary["data_completeness"] = "complete" if not data.data_missing else "missing"
        return summary

    return merge_completeness_into_summary(summary, verdict)
