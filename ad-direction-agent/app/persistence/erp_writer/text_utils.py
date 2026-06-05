"""Text helpers for ERP field mapping — WHP enum codes only in DB."""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

# target_keyword_types (keyword_class / target_keyword_strategy → ERP code)
_TARGET_KEYWORD_TYPE_MAP = {
    "大词": "GENERIC",
    "长尾词": "LONG_TAIL",
    "竞品词": "COMPETITOR",
    "品牌词": "BRAND",
    "自定义": "CUSTOM",
    "自定义词": "CUSTOM",
    "generic": "GENERIC",
    "GENERIC": "GENERIC",
    "broad": "GENERIC",
    "Broad": "GENERIC",
    "BROAD": "GENERIC",
    "long_tail": "LONG_TAIL",
    "long-tail": "LONG_TAIL",
    "Long-tail": "LONG_TAIL",
    "LONG_TAIL": "LONG_TAIL",
    "competitor": "COMPETITOR",
    "Competitor": "COMPETITOR",
    "COMPETITOR": "COMPETITOR",
    "brand": "BRAND",
    "Brand": "BRAND",
    "BRAND": "BRAND",
    "custom": "CUSTOM",
    "Custom": "CUSTOM",
    "CUSTOM": "CUSTOM",
}

# advert_purpose / purpose_score.advert_purpose
_AD_PURPOSE_MAP = {
    "Traffic": "TRAFFIC",
    "Conversion": "CONVERSION",
    "Ranking": "RANKING",
    "Profit": "PROFIT",
    "引流型": "TRAFFIC",
    "转化型": "CONVERSION",
    "排名型": "RANKING",
    "盈利型": "PROFIT",
    "TRAFFIC": "TRAFFIC",
    "CONVERSION": "CONVERSION",
    "RANKING": "RANKING",
    "PROFIT": "PROFIT",
}

_PRODUCT_POSITION_MAP = {
    "头部": "TOP",
    "腰部": "WAIST",
    "长尾": "LONG_TAIL",
    "同步": "WAIST",
    "TOP": "TOP",
    "WAIST": "WAIST",
    "LONG_TAIL": "LONG_TAIL",
}

_PRODUCT_STAGE_MAP = {
    "收割利润期": "HARVEST_PROFIT",
    "测试期": "TESTING",
    "推进期": "PROMOTING",
    "维持期": "MAINTAINING",
    "HARVEST_PROFIT": "HARVEST_PROFIT",
    "TESTING": "TESTING",
    "PROMOTING": "PROMOTING",
    "MAINTAINING": "MAINTAINING",
}

_SEASON_TYPE_MAP = {
    "淡季": "OFF_SEASON",
    "旺季准备": "PEAK_SEASON_PREPARE",
    "大旺季": "BIG_PEAK_SEASON",
    "旺季末期": "LATE_PEAK_SEASON",
    "OFF_SEASON": "OFF_SEASON",
    "PEAK_SEASON_PREPARE": "PEAK_SEASON_PREPARE",
    "BIG_PEAK_SEASON": "BIG_PEAK_SEASON",
    "LATE_PEAK_SEASON": "LATE_PEAK_SEASON",
}

# advert_direction_types / direction_recommend_detail.direction_type (WHP ad_directions)
_DIRECTION_ID_TO_ERP = {
    "push_natural": "PUSH_NATURAL",
    "expand_keywords": "EXPAND_KEYWORDS",
    "optimize_acos": "OPTIMIZE_ACOS",
    "balance_maintain": "BALANCE_MAINTAIN",
    "推进自然位": "PUSH_NATURAL",
    "新增扩词": "EXPAND_KEYWORDS",
    "优化ACOS": "OPTIMIZE_ACOS",
    "平衡维持": "BALANCE_MAINTAIN",
    "PUSH_NATURAL": "PUSH_NATURAL",
    "EXPAND_KEYWORDS": "EXPAND_KEYWORDS",
    "OPTIMIZE_ACOS": "OPTIMIZE_ACOS",
    "BALANCE_MAINTAIN": "BALANCE_MAINTAIN",
    # legacy Agent codes → WHP enum
    "PROMOTE_NATURAL_RANK": "PUSH_NATURAL",
    "ADD_KEYWORD_EXPANSION": "EXPAND_KEYWORDS",
    "BALANCE_MAINTENANCE": "BALANCE_MAINTAIN",
}

_LEVEL_TO_SCORE = {"推荐": 80, "可选": 50, "不推荐": 20, "high": 80, "medium": 50, "low": 20}

# t_advert_agent_modify_suggest_card.campaign_group_type (Java: campaignGroupType)
_CAMPAIGN_GROUP_TYPE_MAP = {
    # 现行中文标签 → ERP 码
    "精准主力组": "exact_core_group",
    "精准测试组": "exact_testing_group",
    "自动广泛组": "auto_broad_group",
    "低价捡漏组": "low_bid_retention_group",
    # 现行 ERP 码（透传）
    "exact_core_group": "exact_core_group",
    "exact_testing_group": "exact_testing_group",
    "auto_broad_group": "auto_broad_group",
    "low_bid_retention_group": "low_bid_retention_group",
    # 遗留中文标签（历史 JSON / 旧分析结果）
    "主推": "exact_core_group",
    "广泛/自动": "auto_broad_group",
    "测试/新增": "exact_testing_group",
    "淘汰": "low_bid_retention_group",
    # 遗留 ERP 码
    "core": "exact_core_group",
    "auto_broad": "auto_broad_group",
    "test": "exact_testing_group",
    "eliminate": "low_bid_retention_group",
    "MAIN_PUSH": "exact_core_group",
    "BROAD_AUTO": "auto_broad_group",
    "TEST_NEW": "exact_testing_group",
    "ELIMINATE_BUBBLE": "low_bid_retention_group",
}

# Short marketplace codes → WHP site_code enum
_SITE_SHORT_TO_AMAZON = {
    "US": "Amazon_US",
    "UK": "Amazon_UK",
    "DE": "Amazon_DE",
    "GB": "Amazon_UK",
}


def _map_single(value: str | None, table: dict[str, str]) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    return table.get(raw, raw)


def map_ad_purpose(value: str | None) -> str | None:
    return _map_single(value, _AD_PURPOSE_MAP)


def map_target_keyword_type(value: str | None) -> str | None:
    """target_keyword_types / core_keyword_tracking.keyword_type — ERP enum code."""
    return _map_single(value, _TARGET_KEYWORD_TYPE_MAP)


def map_product_position(value: str | None) -> str | None:
    return _map_single(value, _PRODUCT_POSITION_MAP)


def map_product_stage(value: str | None) -> str | None:
    return _map_single(value, _PRODUCT_STAGE_MAP)


def map_season_type(value: str | None) -> str | None:
    return _map_single(value, _SEASON_TYPE_MAP)


def map_purpose_target(value: str | None) -> str | None:
    """purpose_score.advert_purpose — ERP enum code."""
    return map_ad_purpose(value)


def map_campaign_group_type(value: str | None) -> str | None:
    """Campaign portfolio → ERP campaign_group_type (campaignGroupType)."""
    return _map_single(value, _CAMPAIGN_GROUP_TYPE_MAP)


def map_direction_type(direction_id: str | None) -> str:
    if not direction_id:
        return ""
    key = str(direction_id).strip()
    if key in _DIRECTION_ID_TO_ERP:
        return _DIRECTION_ID_TO_ERP[key]
    upper = key.upper()
    return _DIRECTION_ID_TO_ERP.get(upper, upper)


def normalize_site_code(site: str | None) -> str:
    """WHP site_code: Amazon_US / Amazon_DE / Amazon_UK (keep Amazon_ prefix)."""
    raw = (site or "").strip()
    if not raw:
        return "Amazon_US"
    if raw.startswith("Amazon_"):
        return raw
    short = raw.upper()
    if short in _SITE_SHORT_TO_AMAZON:
        return _SITE_SHORT_TO_AMAZON[short]
    return f"Amazon_{short}" if len(short) <= 3 else raw


def to_enum_list(
    values: Any,
    mapper: Callable[[str | None], str | None] | None = None,
) -> str | None:
    """Multi-value ERP fields stored as JSON array string, e.g. [\"CONVERSION\",\"RANKING\"]."""
    if values is None:
        return None
    if isinstance(values, str):
        s = values.strip()
        if not s:
            return None
        if s.startswith("["):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    values = parsed
                else:
                    values = [s]
            except json.JSONDecodeError:
                values = [p.strip() for p in s.replace("，", ",").split(",") if p.strip()]
        else:
            values = [p.strip() for p in s.replace("，", ",").split(",") if p.strip()]
    elif not isinstance(values, list):
        values = [values]

    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        if v is None:
            continue
        mapped = mapper(str(v).strip()) if mapper else str(v).strip()
        if not mapped or mapped in seen:
            continue
        seen.add(mapped)
        out.append(mapped)
    if not out:
        return None
    return json.dumps(out, ensure_ascii=False)


def map_direction_types_json(values: Any) -> str | None:
    """advert_direction_types — JSON array of direction enum codes."""

    def _map_dir(v: str | None) -> str | None:
        code = map_direction_type(v)
        return code or None

    return to_enum_list(values, _map_dir)


_RECOMMEND_TAG_ZH = {
    "not_recommended": "暂不建议",
    "available": "可考虑",
    "recommended": "推荐",
}


def normalize_advert_direction_types_list(values: Any) -> list[str]:
    """Normalize wizard/decision direction list to canonical ERP codes."""
    raw = map_direction_types_json(values)
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [str(x) for x in parsed] if isinstance(parsed, list) else []


def build_wizard_direction_content_json(
    direction: dict[str, Any],
    validation: dict[str, Any] | None,
    decision_package: dict[str, Any] | None,
) -> dict[str, Any]:
    """ERP content_json for Tab4 — WHP should read display_* first (see docs/WHP-Tab4渲染与组合枚举说明.md)."""
    d = direction or {}
    val = validation or {}
    pkg = decision_package or {}
    tag = d.get("suitability") or (
        "recommended" if d.get("recommended") else "available"
    )
    display_tasks: list[dict[str, Any]] = []
    for t in pkg.get("tasks") or []:
        if not isinstance(t, dict):
            continue
        display_tasks.append(
            {
                "priority": t.get("priority"),
                "action": t.get("action"),
                "details": t.get("details"),
                "estimated_impact": t.get("estimated_impact"),
            }
        )
    score = d.get("suitability_score")
    return {
        "display_label": d.get("label"),
        "display_reason": d.get("reason"),
        "display_score": score,
        "display_tag": tag,
        "display_tag_zh": _RECOMMEND_TAG_ZH.get(str(tag), str(tag)),
        "display_tasks": display_tasks,
        "direction": d,
        "validation": val,
        "decision_package": pkg or None,
    }


def level_to_score(level: str | None) -> int | None:
    if not level:
        return None
    return _LEVEL_TO_SCORE.get(str(level).strip())


def split_reason_sections(text: str | None) -> tuple[str | None, str | None, str | None]:
    """Split purpose-agent / P3 text into basis, suggest, future_attention."""
    if not text:
        return None, None, None
    raw = str(text).strip()
    markers = [
        ("【决策依据】", "basis"),
        ("【建议】", "suggest"),
        ("【后续关注】", "future"),
    ]
    positions: list[tuple[int, str]] = []
    for marker, _ in markers:
        idx = raw.find(marker)
        if idx >= 0:
            positions.append((idx, marker))
    if not positions:
        return raw, None, None
    positions.sort(key=lambda x: x[0])
    sections: dict[str, str] = {}
    for i, (start, marker) in enumerate(positions):
        end = positions[i + 1][0] if i + 1 < len(positions) else len(raw)
        body = raw[start + len(marker) : end].strip()
        if marker == "【决策依据】":
            sections["basis"] = body
        elif marker == "【建议】":
            sections["suggest"] = body
        else:
            sections["future"] = body
    return sections.get("basis"), sections.get("suggest"), sections.get("future")


def join_csv(values: Any) -> str | None:
    """Deprecated for ERP enum multi-value fields — use to_enum_list."""
    if values is None:
        return None
    if isinstance(values, str):
        return values.strip() or None
    if isinstance(values, list):
        parts = [str(v).strip() for v in values if v is not None and str(v).strip()]
        return ",".join(parts) if parts else None
    return str(values)


def json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)
