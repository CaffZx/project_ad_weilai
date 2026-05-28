"""P3 策略层适配器 — 调用 ad-purpose-agent 的 AI 诊断引擎

职责：
1. 从 direction-agent 的已有数据（ASINData）组装 metrics dict
2. 调用 purpose-agent 的 determine_ad_targets_from_metrics()
3. 将结果映射为 direction 策略层所需的格式

不重复查库，所有数据由 direction-agent 的 WorkflowOrchestrator 提供。
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def build_metrics_from_asin_data(data, days: int = 7) -> dict:
    """从 ASINData 构建 purpose agent 需要的 metrics dict

    复用 direction-agent 已有的 field_mapping.asin_data_to_metrics，
    若不可用则手工组装。
    """
    try:
        # 优先使用 direction-agent 已有的转换函数
        from app.data.field_mapping import asin_data_to_metrics  # type: ignore
        return asin_data_to_metrics(data, days=days)
    except (ImportError, AttributeError):
        pass

    # 手工组装 metrics
    ad = data.ad_data
    metrics = {}
    if ad:
        metrics["cpc"] = ad.cpc or 0
        metrics["avg_acos"] = (ad.acos / 100) if ad.acos else 0
        metrics["ctr"] = (ad.ctr / 100) if ad.ctr else 0
        metrics["spend"] = ad.spend or 0
        metrics["sales"] = ad.sales or 0
    else:
        metrics["cpc"] = 0
        metrics["avg_acos"] = 0
        metrics["ctr"] = 0

    trend_orders = sum(tp.orders or 0 for tp in data.trend) if data.trend else 0
    trend_ad_orders = sum(tp.ad_orders or 0 for tp in data.trend) if data.trend else 0
    metrics["total_orders"] = (ad.orders if ad and ad.orders else None) or trend_orders or 0
    metrics["ad_orders"] = (ad.orders if ad and ad.orders else None) or trend_ad_orders or 0
    metrics["natural_order_ratio"] = (data.natural_order_ratio / 100) if data.natural_order_ratio else 0
    metrics["net_cvr"] = (ad.cvr / 100) if ad and ad.cvr else 0
    metrics["refund_rate"] = (data.refund_rate / 100) if data.refund_rate else 0
    metrics["avg_star"] = data.rating or 0
    metrics["avg_price"] = data.price or 0
    metrics["total_inventory"] = data.signals.inventory_qty if data.signals else 0
    metrics["avg_nature_rank"] = min(
        (k.natural_rank for k in data.keywords if k.natural_rank), default=100
    ) if data.keywords else 100
    metrics["unit_gross_profit"] = (data.margin * data.price) if data.margin and data.price else 0

    # 关键词列表
    metrics["top_keywords"] = [
        {
            "word": kw.keyword,
            "rank": kw.natural_rank,
            "near_rank": kw.near_natural_rank,
            "spend": kw.spend,
            "search_rank": kw.search_rank or 0,
            "sp_rank": kw.sp_rank or 0,
            "rank_change": kw.rank_change_14d or 0,
            "rank_change_14d": kw.rank_change_14d or 0,
            "rank_change_7d": kw.rank_change_7d,
        }
        for kw in data.keywords[:20]
    ]

    return metrics


async def recommend_tactics_from_purpose(
    data,
    position: str,
    stage: str,
    season: str,
    days: int = 7,
) -> dict:
    """调用 purpose-agent 的 AI 引擎获取策略推荐

    Returns:
        {
            "targets": [...],              # English target names
            "keyword_analysis": [...],      # per-keyword {word, strategy_type, action}
            "reason": "...",               # HTML diagnosis report
            "indicators": [...],           # KPI dashboard data
            "trend_data": [...],           # time-series trend
            "chart_metrics": [...],        # metrics to chart
            "summary": "...",              # key observations
            "error": "..."                 # if failed
        }
    """
    try:
        from agent_router import determine_ad_targets_from_metrics  # type: ignore
    except ImportError as e:
        logger.error("无法导入 purpose-agent: %s", e)
        return {"error": f"purpose-agent 不可用: {e}"}

    metrics = build_metrics_from_asin_data(data, days=days)

    # 关键词数据
    keywords = metrics.get("top_keywords", [])
    # 趋势数据
    trend_history = [
        {"date": tp.date, "acos": tp.acos, "cvr": tp.cvr,
         "ctr": tp.ctr, "cpc": tp.cpc, "orders": tp.orders, "spend": tp.spend}
        for tp in data.trend
    ] if data.trend else []

    # 竞品价格
    competitor_price = data.competitor_price_p50

    # 库存天数（库存为 0 但有订单时，不强行报 0 天断货，交给 LLM 看订单数据）
    orders_total = int(metrics.get("total_orders") or 0)
    stock_days = None
    if orders_total > 0:
        daily_orders = orders_total / float(days)
        inv = int(metrics.get("total_inventory", 0))
        if inv > 0 and daily_orders > 0:
            stock_days = max(1, int(inv / daily_orders))
        elif inv <= 0:
            stock_days = None

    try:
        result = await determine_ad_targets_from_metrics(
            metrics=metrics,
            position=position,
            stage=stage,
            season=season,
            days=days,
            keywords=keywords,
            trend_history=trend_history,
            competitor_price=competitor_price,
            stock_days=stock_days,
        )
    except Exception as e:
        logger.error("purpose-agent 调用失败: %s", e)
        return {"error": str(e)}

    # 映射 targets（English → Chinese）
    target_map = {
        "Traffic": "引流型", "Conversion": "转化型", "Ranking": "排名型",
        "Profit": "盈利型",
    }

    def _extract_target(t):
        if isinstance(t, str):
            return t
        if isinstance(t, dict):
            return t.get("target", "") or t.get("name", "") or ""
        return str(t)

    result["ad_purposes"] = [
        target_map.get(_extract_target(t), _extract_target(t))
        for t in result.get("targets", [])
    ]

    # 聚合 keyword_analysis 中的 strategy_type → keyword_types
    type_map = {
        "Broad": "大词", "Long-tail": "长尾词", "Competitor": "竞品词",
        "Brand": "品牌词", "Custom": "自定义",
    }
    kw_types = set()
    for kw in result.get("keyword_analysis", []):
        cn_type = type_map.get(kw.get("strategy_type", ""))
        if cn_type:
            kw_types.add(cn_type)
    result["keyword_types"] = list(kw_types) if kw_types else []
    result["target_scores"] = result.get("target_scores", [])

    return result
