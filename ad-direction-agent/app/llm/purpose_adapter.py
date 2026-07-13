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
    """从 ASINData 构建 purpose agent 需要的 metrics dict（复用 field_mapping.asin_data_to_metrics）。"""
    from app.data.field_mapping import asin_data_to_metrics

    return asin_data_to_metrics(data, days=days)


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
            "keyword_analysis": [...],      # per-keyword {word, keyword_class, action}
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

    ad_purposes_raw = []
    for s in result.get("target_scores", []):
        if isinstance(s, dict) and s.get("level") == "推荐":
            cn = target_map.get(_extract_target(s), "")
            if cn:
                ad_purposes_raw.append(cn)
    # 兜底：LLM 未遵循 level 格式时，用 targets 数组
    if not ad_purposes_raw:
        ad_purposes_raw = [
            target_map.get(_extract_target(t), _extract_target(t))
            for t in result.get("targets", [])
        ]
    result["ad_purposes"] = ad_purposes_raw[:2]  # 安全 cap 最多 2 个推荐

    # 聚合 keyword_analysis 中的 keyword_class → target_keyword_strategy
    type_map = {
        "Broad": "大词", "Long-tail": "长尾词", "Competitor": "竞品词",
        "Brand": "品牌词", "Custom": "自定义",
    }
    kw_types = set()
    for kw in result.get("keyword_analysis", []):
        cn_type = type_map.get(kw.get("keyword_class", ""))
        if cn_type:
            kw_types.add(cn_type)
    result["target_keyword_strategy"] = list(kw_types) if kw_types else []
    result["target_scores"] = result.get("target_scores", [])

    return result
