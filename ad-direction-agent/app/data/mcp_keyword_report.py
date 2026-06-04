"""ad_keyword_report: configurable match_type and multi-call merge."""

from __future__ import annotations

from typing import Any

from app.config.settings import settings
from app.data.mcp_mapping import McpContext
from app.data.mcp_normalizers import _as_rows, _pick


def parse_keyword_match_types() -> list[str]:
    """Empty list => omit match_type (gateway returns all types)."""
    raw = (settings.mcp_keyword_match_types or "").strip()
    if not raw or raw.lower() in ("all", "*", "any", "none"):
        return []
    return list(dict.fromkeys(p.strip().upper() for p in raw.split(",") if p.strip()))


def keyword_report_arg_specs(ctx: McpContext) -> list[dict[str, Any]]:
    base = {
        "parent_asin": ctx.parent_asin,
        "parent_seller_sku": ctx.parent_seller_sku,
        "shop_account": ctx.shop_account,
    }
    types = parse_keyword_match_types()
    if not types:
        return [base]
    return [{**base, "match_type": mt} for mt in types]


def expand_planned_tools(planned: list[str], ctx: McpContext) -> list[tuple[str, dict[str, Any] | None]]:
    """Expand ad_keyword_report into one or more calls with different match_type."""
    out: list[tuple[str, dict[str, Any] | None]] = []
    for name in planned:
        if name == "ad_keyword_report":
            for spec in keyword_report_arg_specs(ctx):
                out.append((name, spec))
        else:
            out.append((name, None))
    return out


def merge_keyword_report_payloads(payloads: list[Any]) -> Any:
    """Merge multiple MCP keyword report responses; dedupe by keyword text (keep more clicks)."""
    merged: dict[str, dict] = {}
    for payload in payloads:
        for row in _as_rows(payload):
            kw = str(_pick(row, "keyword_text", "keyword", "搜索词") or "").strip()
            if not kw:
                continue
            clicks = int(float(_pick(row, "clicks", "点击量") or 0))
            prev = merged.get(kw)
            if prev is None or clicks >= int(float(prev.get("clicks") or 0)):
                merged[kw] = dict(row)
                merged[kw]["keyword_text"] = kw
    if not merged:
        return []
    return list(merged.values())
