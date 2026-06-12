"""Normalize MCP tool outputs to ASINData-compatible structures."""

from __future__ import annotations

from typing import Any


def _as_rows(payload: Any) -> list[dict]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        # Common wrappers: {"rows":[...]} / {"data":[...]} / single row dict.
        for key in ("rows", "data", "items", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
        return [payload]
    return []


def _float(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v) -> int | None:
    if v is None:
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _row_key_index(row: dict) -> dict[str, Any]:
    """Case-insensitive, whitespace-stripped key index."""
    out: dict[str, Any] = {}
    for k, v in row.items():
        norm = str(k).strip().lower()
        if norm not in out:
            out[norm] = v
    return out


def _pick(row: dict, *aliases: str) -> Any:
    """Return first non-None value for aliases; fallback to case-insensitive key match."""
    for alias in aliases:
        if alias in row:
            v = row[alias]
            if v is not None:
                return v
    lowered = _row_key_index(row)
    for alias in aliases:
        v = lowered.get(str(alias).strip().lower())
        if v is not None:
            return v
    return None


def normalize_listing_basic_info(payload: Any) -> dict:
    rows = _as_rows(payload)
    if not rows:
        return {}
    row = rows[0]
    return {
        "seller_sku": row.get("parent_seller_sku") or row.get("seller_sku"),
        "product_price": _float(_pick(row, "price", "product_price", "售价")),
        "star_level": _float(_pick(row, "star_level", "rating", "星级")),
        "comment_num": _int(_pick(row, "comment_num", "review_count", "评论数")),
        "brand": _pick(row, "brand", "品牌"),
        "category_name": _pick(row, "category_name", "品类", "类目"),
        "refund_rate": _float(_pick(row, "refund_rate", "16周退款率")),
    }


def normalize_ad_summary(payload: Any) -> dict:
    rows = _as_rows(payload)
    if not rows:
        return {}
    row = rows[0]
    return {
        "cost": _float(_pick(row, "cost", "spend", "花费", "广告花费")),
        "sale": _float(_pick(row, "sale", "sales", "销售额")),
        "clicks": _int(_pick(row, "clicks", "点击量")),
        "impressions": _int(_pick(row, "impressions", "曝光量")),
        "units_order": _int(_pick(row, "units_order", "orders", "广告订单量", "销售数量")),
        "acos": _float(_pick(row, "acos", "ACOS")),
        "cpc": _float(_pick(row, "cpc", "CPC")),
        "ctr": _float(_pick(row, "ctr", "CTR")),
        "cvr": _float(_pick(row, "cvr", "CVR")),
        "campaign_budget": _float(_pick(row, "campaign_budget")),
    }


def normalize_ad_placement(payload: Any) -> dict:
    rows = _as_rows(payload)
    if not rows:
        return {}
    result = {}
    for row in rows:
        placement = str(row.get("placement", "")).lower()
        cost = _float(row.get("cost")) or 0
        sale = _float(row.get("sale")) or 0
        clicks = _float(row.get("clicks")) or 0
        if "top" in placement or "tos" in placement:
            result["placement_tos_acos"] = round(cost / sale * 100, 1) if sale else None
            result["placement_tos_cpc"] = round(cost / clicks, 2) if clicks else None
        else:
            result["placement_ros_acos"] = round(cost / sale * 100, 1) if sale else None
            result["placement_ros_cpc"] = round(cost / clicks, 2) if clicks else None
    return result


def normalize_keywords(payload: Any) -> list[dict]:
    rows = _as_rows(payload)
    out = []
    for row in rows:
        out.append(
            {
                "keyword_text": _pick(row, "keyword_text", "keyword", "搜索词"),
                "match_type": _pick(row, "match_type") or "",
                "clicks": _int(_pick(row, "clicks", "点击量")),
                "cost": _float(_pick(row, "cost", "spend", "花费")),
                "impressions": _int(_pick(row, "impressions", "曝光量")),
                "sale": _float(_pick(row, "sale", "sales", "销售额")),
                "units_order": _int(_pick(row, "units_order", "orders", "广告订单量")),
                "keyword_bid": _float(_pick(row, "keyword_bid", "bid")),
                "acos": _float(_pick(row, "acos", "ACOS")),
                "cvr": _float(_pick(row, "cvr", "CVR")),
            }
        )
    return out


def normalize_flow_keywords(payload: Any) -> list[dict]:
    rows = _as_rows(payload)
    out = []
    for row in rows:
        out.append(
            {
                "keyword": row.get("keyword"),
                "searches": _int(row.get("searches")),
                "searches_rank": _int(row.get("searches_rank")),
                "top_click_ratio": _float(row.get("top_click_ratio")),
                "top_convert_ratio": _float(row.get("top_convert_ratio")),
            }
        )
    return out


def normalize_competitors(payload: Any) -> list[dict]:
    rows = _as_rows(payload)
    out = []
    for row in rows:
        out.append(
            {
                "asin": row.get("asin"),
                "price": _float(row.get("price")),
                "asin_star": _float(row.get("asin_star") or row.get("rating")),
                "reviews_num": _int(row.get("reviews_num") or row.get("review_count")),
                "top_category_rank": _int(row.get("top_category_rank")),
            }
        )
    return out


def normalize_keyword_rankings(competitors_payload: Any, child_asins_payload: Any) -> list[dict]:
    rows = []
    for payload in (_as_rows(competitors_payload), _as_rows(child_asins_payload)):
        for row in payload:
            rows.append(
                {
                    "keyword": row.get("keyword"),
                    "craw_nature_rank": _int(row.get("natural_rank") or row.get("craw_nature_rank")),
                    "near_craw_nature_rank": _int(row.get("near_natural_rank") or row.get("near_craw_nature_rank")),
                    "craw_sp_rank": _int(row.get("sp_rank") or row.get("craw_sp_rank")),
                }
            )
    return rows


def normalize_product_sales(payload: Any) -> dict:
    rows = _as_rows(payload)
    if not rows:
        return {}
    # Aggregate for parent-level usage.
    total_sales = 0.0
    total_orders = 0.0
    ad_orders = 0.0
    total_ad_cost = 0.0
    margin = None
    for row in rows:
        total_sales += _float(_pick(row, "sales", "sale", "sales_rmb", "全部销售额")) or 0
        total_orders += _float(_pick(row, "orders", "order_num", "全部单量", "总单量", "全部订单")) or 0
        ad_orders += _float(_pick(row, "ad_orders", "ad_sale_num", "广告单量", "广告订单量")) or 0
        total_ad_cost += _float(_pick(row, "ad_cost", "cost", "spend", "广告花费")) or 0
        m = _float(_pick(row, "margin", "gross_margin"))
        if m is not None:
            margin = m if margin is None else (margin + m) / 2
    return {
        "total_sales": total_sales,
        "total_orders": total_orders,
        "ad_orders": ad_orders,
        "total_ad_cost": total_ad_cost,
        "margin": margin,
    }


def normalize_trend(payload: Any) -> list[dict]:
    rows = _as_rows(payload)
    out = []
    for row in rows:
        out.append(
            {
                "date": _pick(row, "date", "日期"),
                "orders": _int(_pick(row, "orders", "order_num", "全部单量", "总单量", "全部订单")),
                "ad_orders": _int(_pick(row, "ad_orders", "ad_sale_num", "广告单量", "广告订单量")),
                "clicks": _int(_pick(row, "clicks", "点击量")),
                "impressions": _int(_pick(row, "impressions", "曝光量")),
                "spend": _float(_pick(row, "spend", "cost", "ad_cost", "广告花费")),
                "ad_sales": _float(_pick(row, "ad_sales", "sale", "sales", "销售额")),
            }
        )
    return out


def compute_natural_order_ratio(
    total_orders: float | None,
    ad_orders: float | None,
    trend_rows: list[dict] | None = None,
) -> float | None:
    """(总订单 - 广告订单) / 总订单 * 100；聚合为 0 时从按日趋势行回退求和。"""
    if total_orders and ad_orders is not None and total_orders > 0:
        return max(0.0, (float(total_orders) - float(ad_orders)) / float(total_orders) * 100)
    if not trend_rows:
        return None
    sum_orders = 0.0
    sum_ad = 0.0
    saw_ad = False
    for row in trend_rows:
        o = _float(row.get("orders"))
        a = row.get("ad_orders")
        if o is not None:
            sum_orders += o
        if a is not None:
            sum_ad += _float(a) or 0
            saw_ad = True
    if sum_orders > 0 and saw_ad:
        return max(0.0, (sum_orders - sum_ad) / sum_orders * 100)
    return None

