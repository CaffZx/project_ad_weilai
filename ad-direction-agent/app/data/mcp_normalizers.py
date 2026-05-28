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


def normalize_listing_basic_info(payload: Any) -> dict:
    rows = _as_rows(payload)
    if not rows:
        return {}
    row = rows[0]
    return {
        "seller_sku": row.get("parent_seller_sku") or row.get("seller_sku"),
        "product_price": _float(row.get("price") or row.get("product_price")),
        "star_level": _float(row.get("rating") or row.get("star_level")),
        "comment_num": _int(row.get("review_count") or row.get("comment_num")),
        "brand": row.get("brand"),
        "category_name": row.get("category_name"),
        "refund_rate": _float(row.get("refund_rate")),
    }


def normalize_ad_summary(payload: Any) -> dict:
    rows = _as_rows(payload)
    if not rows:
        return {}
    row = rows[0]
    return {
        "cost": _float(row.get("cost") or row.get("spend")),
        "sale": _float(row.get("sale") or row.get("sales")),
        "clicks": _int(row.get("clicks")),
        "impressions": _int(row.get("impressions")),
        "units_order": _int(row.get("units_order") or row.get("orders")),
        "acos": _float(row.get("acos")),
        "cpc": _float(row.get("cpc")),
        "ctr": _float(row.get("ctr")),
        "cvr": _float(row.get("cvr")),
        "campaign_budget": _float(row.get("campaign_budget")),
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
                "keyword_text": row.get("keyword_text") or row.get("keyword"),
                "match_type": row.get("match_type") or "",
                "clicks": _int(row.get("clicks")),
                "cost": _float(row.get("cost") or row.get("spend")),
                "impressions": _int(row.get("impressions")),
                "sale": _float(row.get("sale") or row.get("sales")),
                "units_order": _int(row.get("units_order") or row.get("orders")),
                "keyword_bid": _float(row.get("keyword_bid") or row.get("bid")),
                "acos": _float(row.get("acos")),
                "cvr": _float(row.get("cvr")),
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
        total_sales += _float(row.get("sales") or row.get("sale") or row.get("sales_rmb")) or 0
        total_orders += _float(row.get("orders") or row.get("order_num")) or 0
        ad_orders += _float(row.get("ad_orders") or row.get("ad_sale_num")) or 0
        total_ad_cost += _float(row.get("ad_cost") or row.get("cost") or row.get("spend")) or 0
        m = _float(row.get("margin") or row.get("gross_margin"))
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
                "date": row.get("date"),
                "orders": _int(row.get("orders") or row.get("order_num")),
                "ad_orders": _int(row.get("ad_orders") or row.get("ad_sale_num")),
                "clicks": _int(row.get("clicks")),
                "impressions": _int(row.get("impressions")),
                "spend": _float(row.get("spend") or row.get("cost")),
                "ad_sales": _float(row.get("ad_sales") or row.get("sale")),
            }
        )
    return out

