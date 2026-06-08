"""CSV列名 → ASINData字段路径映射

将CSV/Excel列名映射到ASINData模型的嵌套字段路径。
支持:
- 顶层字段: asin → ("asin",)
- 嵌套字段: acos_14d → ("ad_data", "acos")
- 列表前缀: keyword_1_keyword → 识别为keywords列表元素
- 中文列名映射: 父asin → ("asin",)
- 值转换: VALUE_MAP 做中文标签到枚举值的转换
"""

import re
from typing import Any

# 默认映射: CSV列名 → (模型字段路径, ...)
DEFAULT_FIELD_MAP: dict[str, tuple[str, ...]] = {
    # === 英文列名（通用） ===
    "asin": ("asin",),
    "sku": ("sku",),
    "parent_asin": ("parent_asin",),
    "days_since_launch": ("days_since_launch",),
    "title": ("title",),
    "price": ("price",),
    "cost": ("cost",),
    "fulfillment_fee": ("fulfillment_fee",),
    "margin": ("margin",),
    # 销量
    "avg_daily_sales_7d": ("avg_daily_sales_7d",),
    "avg_daily_sales_30d": ("avg_daily_sales_30d",),
    "sales_trend_30d": ("sales_trend_30d",),
    "natural_order_ratio": ("natural_order_ratio",),
    # 评论
    "rating": ("rating",),
    "review_count": ("review_count",),
    # 广告数据
    "acos_14d": ("ad_data", "acos"),
    "acos_7d": ("ad_data", "acos_7d"),
    "ctr_14d": ("ad_data", "ctr"),
    "cvr_14d": ("ad_data", "cvr"),
    "net_cvr": ("ad_data", "net_cvr"),
    "tacos_14d": ("ad_data", "tacos"),
    "tos_ratio": ("ad_data", "tos_ratio"),
    "pp_ratio": ("ad_data", "pp_ratio"),
    "ros_ratio": ("ad_data", "ros_ratio"),
    "impressions_14d": ("ad_data", "impressions"),
    "clicks_14d": ("ad_data", "clicks"),
    "spend_14d": ("ad_data", "spend"),
    "orders_14d": ("ad_data", "orders"),
    "daily_budget": ("ad_data", "daily_budget"),
    "average_cpc": ("ad_data", "cpc"),
    # 精准/非精准拆分
    "precision_acos": ("ad_data", "precision_acos"),
    "precision_spend": ("ad_data", "precision_spend"),
    "precision_cpc": ("ad_data", "precision_cpc"),
    "precision_spend_ratio": ("ad_data", "precision_spend_ratio"),
    "precision_order_ratio": ("ad_data", "precision_order_ratio"),
    "broad_acos": ("ad_data", "broad_acos"),
    "broad_spend": ("ad_data", "broad_spend"),
    "broad_cpc": ("ad_data", "broad_cpc"),
    "broad_spend_ratio": ("ad_data", "broad_spend_ratio"),
    "broad_order_ratio": ("ad_data", "broad_order_ratio"),
    # 关键词数量
    "keyword_count": ("keyword_count",),
    "available_new_keywords": ("available_new_keywords",),
    # 竞品价格分位
    "competitor_price_p25": ("competitor_price_p25",),
    "competitor_price_p50": ("competitor_price_p50",),
    "competitor_price_p75": ("competitor_price_p75",),
    # 特殊信号
    "inventory_days": ("signals", "inventory_days"),
    "in_transit_inventory": ("signals", "in_transit_inventory"),
    "inventory_qty": ("signals", "inventory_qty"),
    "recent_rating_change": ("signals", "recent_rating_change"),
    "recent_negative_reviews": ("signals", "recent_negative_reviews"),
    "has_coupon": ("signals", "has_coupon"),
    "has_lightning_deal": ("signals", "has_lightning_deal"),
    "listing_modified_recently": ("signals", "listing_modified_recently"),
    "threat_score": ("signals", "threat_score"),
    # 年度/供应链
    "annual_sales_target": ("annual_sales_target",),
    "annual_sales_achieved": ("annual_sales_achieved",),
    "supply_chain_priority": ("supply_chain_priority",),
    # 长周期标签
    "product_level": ("product_level",),
    "product_stage": ("product_stage",),
    "season_stage": ("season_stage",),
    "ad_purpose": ("ad_purpose",),
    "target_keyword_strategy": ("target_keyword_strategy",),

    # === 中文列名（asin_test_data.xlsx 等） ===
    "父asin": ("asin",),
    "产品定位": ("product_level",),
    "当前进度与备货准备": ("product_stage",),
    "淡旺季": ("season_stage",),
    "售价": ("price",),
    "是否有coupon": ("signals", "has_coupon"),
    "统计期销量和": ("avg_daily_sales_30d",),
    "毛利 cny": ("margin",),
    "自然订单占比%": ("natural_order_ratio",),
    "总广告花费": ("ad_data", "spend"),
    "单日广告花费": ("ad_data", "daily_budget"),
    "广告花费占比%": ("ad_data", "tos_ratio"),
    "总ACOS%": ("ad_data", "acos"),
    "精准ACOS %": ("ad_data", "precision_acos"),
    "精准广告花费": ("ad_data", "precision_spend"),
    "精准CPC": ("ad_data", "precision_cpc"),
    "精准花费占比 %": ("ad_data", "precision_spend_ratio"),
    "精准订单量占比 %": ("ad_data", "precision_order_ratio"),
    "非精准ACOS %": ("ad_data", "broad_acos"),
    "非精准广告花费": ("ad_data", "broad_spend"),
    "非精准CPC": ("ad_data", "broad_cpc"),
    "非精准花费占比 %": ("ad_data", "broad_spend_ratio"),
    "非精准订单量占比 %": ("ad_data", "broad_order_ratio"),
    "库存": ("signals", "inventory_qty"),
    "整体转化率%": ("ad_data", "cvr"),
}

# 值转换映射: 字段路径 → {原始值 → 转换值}
# 用于将CSV中的数据标签转换为系统枚举值（两者已对齐，此为正向映射）
VALUE_MAP: dict[tuple[str, ...], dict[str, str]] = {
    ("product_level",): {"头部": "战略级产品 (P0)", "腰部": "常规产品 (P2)", "长尾": "长尾产品 (P3)",
                        "战略级产品": "战略级产品 (P0)", "重点产品": "重点产品 (P1)",
                        "常规产品": "常规产品 (P2)", "长尾产品": "长尾产品 (P3)",
                        "战略级产品 (P0)": "战略级产品 (P0)", "重点产品 (P1)": "重点产品 (P1)",
                        "常规产品 (P2)": "常规产品 (P2)", "长尾产品 (P3)": "长尾产品 (P3)"},
    ("product_stage",): {"测试期":"测试期","推进期":"推进期","收割利润期":"收割利润期","维持期":"维持期","测试":"测试期","推进":"推进期","收割":"收割利润期","维持":"维持期"},
    ("season_stage",): {"淡季": "淡季", "旺季准备": "旺季准备", "大旺季": "大旺季", "旺季末期": "旺季末期"},
}

# 关键词列表前缀模式: keyword_1_keyword, keyword_1_impressions, keyword_2_keyword, ...
KEYWORD_PREFIX_RE = re.compile(r"^keyword_(\d+)_(.+)$")

# 关键词字段映射: CSV后缀 → KeywordData字段
KEYWORD_FIELD_MAP: dict[str, str] = {
    "keyword": "keyword",
    "impressions": "impressions",
    "clicks": "clicks",
    "spend": "spend",
    "orders": "orders",
    "acos": "acos",
    "cvr": "cvr",
    "bid": "bid",
    "suggested_bid": "suggested_bid",
    "search_rank": "search_rank",
    "natural_rank": "natural_rank",
    "rank_change_14d": "rank_change_14d",
    "is_manual": "is_manual",
}

# 竞品列表前缀: competitor_1_asin, competitor_1_price, ...
COMPETITOR_PREFIX_RE = re.compile(r"^competitor_(\d+)_(.+)$")

COMPETITOR_FIELD_MAP: dict[str, str] = {
    "asin": "asin",
    "price": "price",
    "rating": "rating",
    "review_count": "review_count",
    "bsr": "bsr",
    "estimated_sales_7d": "estimated_sales_7d",
    "price_change_7d": "price_change_7d",
}

# 表头检测模式 — 如果某行所有值都出现在headers或此集合中，判定为重复表头
HEADER_DETECT_WORDS: set[str] = {"产品线", "产品定位", "类目", "售价", "父asin", "库存"}


def _resolve_field_type(obj: Any, field: str) -> type | None:
    """通过类型注解确定字段的期望类型，而非当前值类型"""
    from pydantic import BaseModel
    if isinstance(obj, BaseModel):
        annotation = obj.model_fields.get(field)
        if annotation and hasattr(annotation, "annotation"):
            origin = getattr(annotation.annotation, "__origin__", None)
            if origin is not None:
                args = getattr(annotation.annotation, "__args__", ())
                # Optional[X] = Union[X, None] → 取第一个非None类型
                for arg in args:
                    if arg is not type(None):
                        return arg
            return annotation.annotation
    # Fallback: 检查当前值类型
    current = getattr(obj, field, None)
    if current is not None:
        return type(current)
    return None


def set_nested_value(obj: Any, path: tuple[str, ...], value: Any) -> None:
    """按路径设置嵌套对象的值，跳过 None/空值，支持 VALUE_MAP 转换"""
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return

    # 应用值转换
    if path in VALUE_MAP and isinstance(value, str):
        value = VALUE_MAP[path].get(value.strip(), value.strip())

    for part in path[:-1]:
        obj = getattr(obj, part)
    field = path[-1]
    field_type = _resolve_field_type(obj, field)

    if field_type is bool and isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes", "是"):
            value = True
        elif v in ("false", "0", "no", "否", ""):
            value = False
        else:
            try:
                value = float(v.replace("%", ""))
                value = value > 0
            except (ValueError, AttributeError):
                value = False
    elif field_type is bool and isinstance(value, (int, float)):
        value = value > 0
    elif field_type is float and isinstance(value, str):
        try:
            value = float(value.replace("%", "").replace(",", ""))
        except (ValueError, AttributeError):
            return
    elif field_type is int and isinstance(value, str):
        try:
            value = int(float(value.replace(",", "")))
        except (ValueError, AttributeError):
            return
    elif field_type is str and not isinstance(value, str):
        value = str(value)
    try:
        setattr(obj, field, value)
    except (TypeError, ValueError):
        pass


# ══════════════════════════════════════════════════════════════
#  Project1 ↔ Project2 字段映射（Phase 1 统一数据源新增）
# ══════════════════════════════════════════════════════════════

from app.models.asin_data import ASINData  # noqa: E402


# ── 统一字段名常量（Project1 名 → Project2 统一名）────────

P1_TO_P2_FIELD_NAMES = {
    "avg_star": "rating",
    "total_spend": "spend",
    "average_cpc": "cpc",
    "net_cvr": "cvr",
    "avg_acos": "acos",
    "comment_num": "review_count",
    "total_inventory": "inventory_qty",
}

# 反向映射（统一名 → Project1 名）
P2_TO_P1_FIELD_NAMES = {v: k for k, v in P1_TO_P2_FIELD_NAMES.items()}


# ── 原始数据行 → 统一指标（显式转换，不用翻译器）──────

def ad_summary_to_metrics(row: dict) -> dict:
    """dwd_amazon_ad_product_report_update 汇总行 → 统一指标"""
    cost = _float(row.get("cost", 0)) or 0
    sale = _float(row.get("sale", 0)) or 0
    clicks = _float(row.get("clicks", 0)) or 0
    impressions = _float(row.get("impressions", 0)) or 0
    orders = _float(row.get("units_order", 0)) or 0
    return {
        "spend": cost,
        "sales": sale,
        "clicks": clicks,
        "impressions": impressions,
        "orders": orders,
        "acos": round(cost / sale * 100, 1) if sale else None,
        "cpc": round(cost / clicks, 2) if clicks else None,
        "ctr": round(clicks / impressions * 100, 1) if impressions else None,
        "cvr": round(orders / clicks * 100, 1) if clicks else None,
    }


def listing_to_metrics(row: dict) -> dict:
    """listing_general 聚合行 → 统一指标"""
    return {
        "price": _float(row.get("product_price")),
        "rating": _float(row.get("star_level")),
        "review_count": _int(row.get("comment_num")),
        "inventory_qty": _int(row.get("in_stock_num")),
        "refund_rate": _float(row.get("refund_rate")),
    }


# ── 关键词排名 TOP N ──────────────────────────────────

def _build_top_keywords(keywords, ranked_limit: int = 10, unranked_limit: int = 3) -> list[dict]:
    """构建 top_keywords 列表：有排名的取前 N + 无排名高花费取前 M"""
    ranked = [kw for kw in keywords if kw.natural_rank is not None]
    unranked = [kw for kw in keywords if kw.natural_rank is None]

    ranked.sort(key=lambda k: k.natural_rank or 999)
    unranked.sort(key=lambda k: k.spend, reverse=True)

    result = []
    for kw in ranked[:ranked_limit]:
        result.append({
            "word": kw.keyword, "rank": kw.natural_rank,
            "near_rank": kw.near_natural_rank, "spend": round(kw.spend, 1),
            "rank_change": kw.rank_change_14d,
            "rank_change_7d": kw.rank_change_7d,
        })
    for kw in unranked[:unranked_limit]:
        result.append({
            "word": kw.keyword, "rank": None,
            "near_rank": kw.near_natural_rank, "spend": round(kw.spend, 1),
            "rank_change": None,
        })
    return result


# ── ASINData → Project1 兼容扁平 dict ───────────────────

def _orders_from_trend(data: ASINData) -> tuple[int, int]:
    """从趋势序列汇总订单（广告汇总表失败时的兜底）。"""
    if not data.trend:
        return 0, 0
    total = int(sum(tp.orders or 0 for tp in data.trend))
    ad_total = int(sum(tp.ad_orders or 0 for tp in data.trend))
    return total, ad_total


def asin_data_to_metrics(data: ASINData, days: int = 7) -> dict:
    """将 ASINData 展平为 Project1 兼容的扁平 metrics dict

    Phase 2 时供 project1_adapter.determine_ad_targets() 调用。
    所有百分数 → 小数，字段名使用 Project1 命名。
    """
    ad = data.ad_data
    trend_orders, trend_ad_orders = _orders_from_trend(data)
    total_orders = (ad.orders if ad and ad.orders else None) or trend_orders or 0
    ad_orders = (int(ad.orders) if ad and ad.orders else None) or trend_ad_orders or 0
    total_spend = (ad.spend if ad else None) or (
        sum(tp.spend or 0 for tp in data.trend) if data.trend else 0
    )
    return {
        "total_orders": total_orders,
        "ad_orders": ad_orders,
        "total_spend": total_spend or 0,
        "total_ad_sales": (ad.sales if ad else None) or 0,
        "total_clicks": (ad.clicks if ad else None) or 0,
        "total_impressions": (ad.impressions if ad else None) or 0,
        "avg_acos": (ad.acos / 100) if ad.acos else 0,
        "cpc": ad.cpc or 0,
        "ctr": (ad.ctr / 100) if ad.ctr else 0,
        "net_cvr": (ad.cvr / 100) if ad.cvr else 0,
        "natural_order_ratio": (data.natural_order_ratio / 100) if data.natural_order_ratio else 0,
        "avg_price": data.price or 0,
        "avg_star": data.rating or 4.0,
        "refund_rate": (data.refund_rate / 100) if data.refund_rate else 0,
        "total_inventory": data.signals.inventory_qty or 0,
        "total_ratings": data.review_count or 0,
        "analysis_days": days,
        "unit_gross_profit": (data.margin or 0) * (data.price or 0),
        "total_profit": (data.margin or 0) * (data.price or 0) * total_orders,
        "avg_nature_rank": min(
            (kw.natural_rank for kw in data.keywords if kw.natural_rank), default=None
        ),
        "top_keywords": _build_top_keywords(data.keywords),
    }
