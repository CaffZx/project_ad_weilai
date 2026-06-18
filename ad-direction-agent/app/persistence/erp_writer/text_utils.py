"""Text helpers for ERP field mapping — WHP enum codes only in DB."""
from __future__ import annotations

import json
import re
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
    # 带后缀枚举值（权威）
    "战略级产品 (P0)": "P0_PRODUCT",
    "重点产品 (P1)": "P1_PRODUCT",
    "常规产品 (P2)": "P2_PRODUCT",
    "长尾产品 (P3)": "P3_PRODUCT",
    # 裸中文兼容（中间态）
    "战略级产品": "P0_PRODUCT",
    "重点产品": "P1_PRODUCT",
    "常规产品": "P2_PRODUCT",
    "长尾产品": "P3_PRODUCT",
    # 旧值兼容别名（2026-06 迁移过渡期，腰部统一 P2）
    "头部": "P0_PRODUCT",
    "腰部": "P2_PRODUCT",
    "长尾": "P3_PRODUCT",
    "同步": "P2_PRODUCT",
    # self-map（code 透传）
    "P0_PRODUCT": "P0_PRODUCT",
    "P1_PRODUCT": "P1_PRODUCT",
    "P2_PRODUCT": "P2_PRODUCT",
    "P3_PRODUCT": "P3_PRODUCT",
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


# ── 反向 mapper：ERP enum code → 内部(prompt)格式。从 decision_config 读回 1-4 用。──
# 目标格式与 state 库/LLM prompt 一致（中文值）。与 decision.py:_translate_preset 同口径。
_PRODUCT_POSITION_REVERSE = {
    "P0_PRODUCT": "战略级产品 (P0)", "P1_PRODUCT": "重点产品 (P1)",
    "P2_PRODUCT": "常规产品 (P2)", "P3_PRODUCT": "长尾产品 (P3)",
}
_PRODUCT_STAGE_REVERSE = {
    "HARVEST_PROFIT": "收割利润期", "TESTING": "测试期",
    "PROMOTING": "推进期", "MAINTAINING": "维持期",
}
_SEASON_TYPE_REVERSE = {
    "OFF_SEASON": "淡季", "PEAK_SEASON_PREPARE": "旺季准备",
    "BIG_PEAK_SEASON": "大旺季", "LATE_PEAK_SEASON": "旺季末期",
}
_AD_PURPOSE_REVERSE = {
    "TRAFFIC": "引流型", "CONVERSION": "转化型", "RANKING": "排名型", "PROFIT": "盈利型",
}
_TARGET_KEYWORD_TYPE_REVERSE = {
    "GENERIC": "大词", "LONG_TAIL": "长尾词", "COMPETITOR": "竞品词",
    "BRAND": "品牌词", "CUSTOM": "自定义",
}


def _unmap_single(value: str | None, table: dict) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    return table.get(raw, raw)


def unmap_product_position(value: str | None) -> str | None:
    return _unmap_single(value, _PRODUCT_POSITION_REVERSE)


def unmap_product_stage(value: str | None) -> str | None:
    return _unmap_single(value, _PRODUCT_STAGE_REVERSE)


def unmap_season_type(value: str | None) -> str | None:
    return _unmap_single(value, _SEASON_TYPE_REVERSE)


def unmap_ad_purpose(value: str | None) -> str | None:
    return _unmap_single(value, _AD_PURPOSE_REVERSE)


def unmap_target_keyword_type(value: str | None) -> str | None:
    return _unmap_single(value, _TARGET_KEYWORD_TYPE_REVERSE)


def from_enum_list(raw: Any, mapper: Callable[[str | None], str | None] | None = None) -> list[str]:
    """JSON 数组字符串(枚举码) → 内部值列表。decision_config 多值字段读回用。"""
    if not raw:
        return []
    if isinstance(raw, list):
        items = raw
    else:
        s = str(raw).strip()
        if not s:
            return []
        if s.startswith("["):
            try:
                parsed = json.loads(s)
                items = parsed if isinstance(parsed, list) else [s]
            except json.JSONDecodeError:
                items = [p.strip() for p in s.replace("，", ",").split(",") if p.strip()]
        else:
            items = [p.strip() for p in s.replace("，", ",").split(",") if p.strip()]
    out: list[str] = []
    for v in items:
        if v is None:
            continue
        m = mapper(str(v).strip()) if mapper else str(v).strip()
        if m and m not in out:
            out.append(m)
    return out


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


def split_text_to_semicolon_lines(text: str | None) -> list[str]:
    """Split prose into JSON string array by ``;`` / ``；``."""
    if text is None:
        return []
    raw = str(text).strip()
    if not raw:
        return []
    parts = re.split(r"[;；]", raw)
    return [p.strip() for p in parts if p.strip()]


_SECTION_HDR_RE = re.compile(r"^【[^】]+】$")


def _split_prose_chunk(chunk: str) -> list[str]:
    lines: list[str] = []
    for block in re.split(r"\n+", chunk):
        block = block.strip().lstrip("- ").strip()
        if not block or _SECTION_HDR_RE.match(block):
            continue
        if block.startswith("•") or block.startswith("·"):
            lines.append(block.lstrip("•·").strip())
            continue
        for part in re.split(r"(?<=[。；;])|[;；]", block):
            part = part.strip().rstrip("。；;")
            if part:
                lines.append(part)
    return lines


def split_prose_to_display_lines(text: str | None) -> list[str]:
    """Split prose into display lines by ``。`` / ``；`` / bullets (WHP bottom summary)."""
    if text is None:
        return []
    raw = str(text).strip()
    if not raw:
        return []
    if "【" in raw:
        lines: list[str] = []
        basis, suggest, future = split_reason_sections(raw)
        for part in (basis, suggest, future):
            if part:
                lines.extend(_split_prose_chunk(part))
        return lines
    return _split_prose_chunk(raw)


def build_wizard_direction_content_json(
    direction: dict[str, Any],
    validation: dict[str, Any] | None,
    decision_package: dict[str, Any] | None,
) -> list[str]:
    """ERP content_json for Tab4 — JSON array of reason clauses split by ``;`` / ``；``.

    validation and decision_package are accepted for call-site compatibility but not written.
    """
    d = direction or {}
    return split_text_to_semicolon_lines(d.get("reason"))


def split_direction_analysis_to_lines(text: str | None) -> list[str]:
    """Split LLM direction ``analysis`` prose into string lines for ERP conclusion_json."""
    if text is None:
        return []
    raw = str(text).strip()
    if not raw:
        return []
    lines: list[str] = []
    for block in re.split(r"\n+", raw):
        block = block.strip()
        if not block or _SECTION_HDR_RE.match(block):
            continue
        if block.startswith("•") or block.startswith("·"):
            lines.append(block.lstrip("•·").strip())
            continue
        lines.extend(split_text_to_semicolon_lines(block))
    return lines


def build_conclusion_json_for_erp(
    analysis: dict[str, Any] | None,
    wizard: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """ERP conclusion_json — prose fields as string arrays for WHP bottom summary."""
    if not isinstance(analysis, dict):
        return {}
    out = dict(analysis)
    oa = out.get("overall_analysis")
    if isinstance(oa, str):
        out["overall_analysis"] = split_prose_to_display_lines(oa)
    elif isinstance(oa, list):
        out["overall_analysis"] = [str(x).strip() for x in oa if str(x).strip()]
    new_das: list[dict[str, Any]] = []
    for da in out.get("direction_analyses") or []:
        if not isinstance(da, dict):
            continue
        item = dict(da)
        val = item.get("analysis")
        if isinstance(val, str):
            item["analysis"] = split_direction_analysis_to_lines(val)
        elif isinstance(val, list):
            item["analysis"] = [str(x).strip() for x in val if str(x).strip()]
        new_das.append(item)
    out["direction_analyses"] = new_das
    if isinstance(wizard, dict):
        p3 = wizard.get("p3") or {}
        overall_reasoning = p3.get("overall_reasoning") or ""
        _, suggest, future = split_reason_sections(overall_reasoning)
        if suggest:
            out["suggest_lines"] = split_prose_to_display_lines(suggest)
        elif isinstance(out.get("action_priorities"), list):
            # Fallback for newer P3 schema where actions are structured.
            out["suggest_lines"] = [
                str(x.get("action") or "").strip()
                for x in out.get("action_priorities") or []
                if isinstance(x, dict) and str(x.get("action") or "").strip()
            ]
        if future:
            out["future_attention_lines"] = split_prose_to_display_lines(future)
        elif isinstance(out.get("risk_warnings"), list):
            out["future_attention_lines"] = [
                str(x).strip() for x in out.get("risk_warnings") or [] if str(x).strip()
            ]
    return out


def level_to_score(level: str | None) -> int | None:
    if not level:
        return None
    return _LEVEL_TO_SCORE.get(str(level).strip())


def split_reason_sections(text: str | None) -> tuple[str | None, str | None, str | None]:
    """Split purpose-agent / P3 text into basis, suggest, future_attention."""
    if not text:
        return None, None, None
    raw = str(text).strip()
    marker_groups = [
        (("【决策依据】", "【综合判断】"), "basis"),
        (("【建议】", "【执行节奏】"), "suggest"),
        (("【后续关注】", "【风险提示】", "【风险预警】"), "future"),
    ]
    marker_to_bucket: dict[str, str] = {}
    all_markers: list[str] = []
    for markers, bucket in marker_groups:
        for marker in markers:
            marker_to_bucket[marker] = bucket
            all_markers.append(marker)

    positions: list[tuple[int, str]] = []
    for marker in all_markers:
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
        bucket = marker_to_bucket.get(marker)
        if bucket:
            sections[bucket] = body
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
