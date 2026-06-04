"""从 ASINData 提取 API 层数据新鲜度字段。"""

from __future__ import annotations

from app.models.asin_data import ASINData


def merge_partial_failures(data: ASINData, extra: list[str] | None = None) -> list[str]:
    combined = list(data.partial_failures or [])
    for item in extra or []:
        if item and item not in combined:
            combined.append(item)
    return combined


def data_status_fields(data: ASINData, extra_partial_failures: list[str] | None = None) -> dict:
    pf = merge_partial_failures(data, extra_partial_failures)
    freshness = data.data_freshness or "fresh"
    if pf and freshness == "fresh":
        freshness = "partial"
    return {
        "partial_failures": pf,
        "data_freshness": freshness,
    }
