"""Workflow 共享辅助函数（关键词趋势、ASIN 日趋势等）。"""

from app.models.asin_data import ASINData


def is_valid_data(data: ASINData) -> bool:
    """防御：查询结果空值超过 3 个 → 不合法，不得缓存"""
    nulls = 0
    if not data.ad_data or data.ad_data.acos is None or data.ad_data.acos == 0:
        nulls += 1
    if not data.ad_data or data.ad_data.cvr is None or data.ad_data.cvr == 0:
        nulls += 1
    if data.margin is None:
        nulls += 1
    if not data.keywords:
        nulls += 1
    if not data.trend:
        nulls += 1
    if not data.signals or data.signals.inventory_qty is None:
        nulls += 1
    return nulls <= 3


def rank_trend_label(change_14d: int | None) -> str:
    if change_14d is None:
        return "排名趋势未知"
    if change_14d >= 5:
        return "排名快速上升"
    if change_14d >= 2:
        return "排名缓慢上升"
    if change_14d <= -5:
        return "排名快速下滑"
    if change_14d <= -2:
        return "排名轻微下滑"
    return "排名持平"


def acos_trend_label(acos_recent: float | None, acos_prior: float | None) -> str:
    if acos_recent is None or acos_prior is None:
        return "ACOS趋势未知"
    delta = acos_recent - acos_prior
    if delta >= 8:
        return "ACOS明显恶化"
    if delta >= 3:
        return "ACOS轻微上升"
    if delta <= -8:
        return "ACOS明显改善"
    if delta <= -3:
        return "ACOS轻微改善"
    return "ACOS基本持平"


def format_asin_daily_trend(data: ASINData, days: int) -> tuple[list[dict], str]:
    """ASIN 级日趋势：返回结构化列表 + 供 LLM 阅读的摘要（忽略可能不完整的最近一天）"""
    if not data.trend:
        return [], "(无日趋势数据)"
    points = data.trend[:-1] if len(data.trend) > 2 else list(data.trend)
    daily = []
    for tp in points:
        daily.append({
            "date": tp.date,
            "acos": tp.acos,
            "cvr": tp.cvr,
            "cpc": tp.cpc,
            "orders": tp.orders,
            "spend": tp.spend,
        })
    lines = [
        f"  {p['date']}: ACOS={p['acos']}%, CVR={p['cvr']}%, CPC=${p['cpc']}, 订单={p['orders']}, 花费=${p['spend']}"
        for p in daily
    ]
    highlights = []
    acos_vals = [(p["date"], p["acos"]) for p in daily if p["acos"] is not None]
    cvr_vals = [(p["date"], p["cvr"]) for p in daily if p["cvr"] is not None]
    if len(acos_vals) >= 2:
        highlights.append(
            f"近{len(acos_vals)}日ACOS从{acos_vals[0][0]}的{acos_vals[0][1]}%变化至{acos_vals[-1][0]}的{acos_vals[-1][1]}%"
        )
    if len(cvr_vals) >= 2:
        highlights.append(
            f"近{len(cvr_vals)}日CVR从{cvr_vals[0][0]}的{cvr_vals[0][1]}%变化至{cvr_vals[-1][0]}的{cvr_vals[-1][1]}%"
        )
    narrative = "；".join(highlights) if highlights else "日趋势波动较小"
    return daily, "\n".join(lines) + f"\n  → 趋势摘要: {narrative}"


def kw_to_ai_summary(kw, days: int) -> dict:
    """关键词摘要（含时间维度）"""
    half = max(1, days // 2)
    item = {
        "keyword": kw.keyword,
        "match_type": kw.match_type,
        "acos": round(kw.acos, 1) if kw.acos is not None else None,
        f"spend_{days}d": round(kw.spend, 1) if kw.spend else None,
        "orders": kw.orders,
        "natural_rank": kw.natural_rank,
        "rank_change_14d": kw.rank_change_14d,
        "rank_trend": rank_trend_label(kw.rank_change_14d),
    }
    if kw.acos_recent is not None and kw.acos_prior is not None:
        item[f"acos近{half}日"] = kw.acos_recent
        item[f"acos前{days - half}日"] = kw.acos_prior
        item["acos_delta"] = round(kw.acos_recent - kw.acos_prior, 1)
        item["acos_trend"] = acos_trend_label(kw.acos_recent, kw.acos_prior)
    if kw.spend_recent is not None and kw.spend_prior is not None:
        item[f"spend近{half}日"] = kw.spend_recent
        item[f"spend前{days - half}日"] = kw.spend_prior
    if kw.orders_recent is not None:
        item[f"orders近{half}日"] = kw.orders_recent
        item[f"orders前{days - half}日"] = kw.orders_prior
    return item


def kw_trend_priority(kw) -> float:
    score = 0.0
    if kw.rank_change_14d is not None:
        score += abs(kw.rank_change_14d) * 3
    if kw.acos_recent is not None and kw.acos_prior is not None:
        score += abs(kw.acos_recent - kw.acos_prior) * 2
    if kw.spend_recent is not None and kw.spend_prior is not None:
        score += abs(kw.spend_recent - kw.spend_prior) * 0.05
    return score


# 步骤模块仍使用下划线别名，便于与历史提取脚本一致
_is_valid_data = is_valid_data
_rank_trend_label = rank_trend_label
_acos_trend_label = acos_trend_label
_format_asin_daily_trend = format_asin_daily_trend
_kw_to_ai_summary = kw_to_ai_summary
_kw_trend_priority = kw_trend_priority
