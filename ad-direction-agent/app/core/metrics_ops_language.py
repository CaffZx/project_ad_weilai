"""面向运营的时间窗与指标表述（禁止半窗/全窗等内部术语）"""

from __future__ import annotations

import re

_RULE_PREFIX = r"(?:PN|KE|OA|BM|PX|CROSS)"
_RULE_LEVEL = r"(?:confirmed|suggest_optimize|force_correct)"
# BM-3 confirmed / BM-4 suggest_optimize / BM-4提示（勿用 \\b，中文紧邻时词界失效）
_RULE_TAG_PATTERN = re.compile(
    rf"{_RULE_PREFIX}-?\d+(?:\s*{_RULE_LEVEL})?(?:提示|说明|检查)?",
    re.IGNORECASE,
)
_RULE_PAREN_PATTERN = re.compile(
    rf"[\(（]\s*{_RULE_PREFIX}-?\d+(?:\s+{_RULE_LEVEL})?\s*[\)）]",
    re.IGNORECASE,
)
_LEVEL_ONLY_PATTERN = re.compile(rf"\b{_RULE_LEVEL}\b", re.IGNORECASE)


def period_labels(days: int) -> tuple[int, int, str, str, str]:
    """返回 (recent_days, prior_days, window_label, recent_label, prior_label)"""
    days = max(1, int(days))
    recent_days = max(1, days // 2)
    prior_days = max(1, days - recent_days)
    window_label = f"近{days}天"
    recent_label = f"后{recent_days}天"
    prior_label = f"前{prior_days}天"
    return recent_days, prior_days, window_label, recent_label, prior_label


def format_keyword_acos_change(
    days: int,
    acos_full: float | None,
    acos_prior: float | None,
    acos_recent: float | None,
    trend_label: str | None = None,
) -> str | None:
    """关键词 ACOS：统计周期整体 + 前后半段变化（运营可读）"""
    _, _, window_label, recent_label, prior_label = period_labels(days)
    parts: list[str] = []

    if acos_full is not None:
        parts.append(f"{window_label}整体ACOS {acos_full:.1f}%")

    if acos_prior is not None and acos_recent is not None:
        change = (
            f"{recent_label}由 {acos_prior:.1f}% 降至 {acos_recent:.1f}%"
            if acos_recent < acos_prior
            else f"{recent_label}由 {acos_prior:.1f}% 升至 {acos_recent:.1f}%"
            if acos_recent > acos_prior
            else f"{recent_label}ACOS {acos_recent:.1f}%（与{prior_label}{acos_prior:.1f}%基本持平）"
        )
        if trend_label:
            change += f"（{trend_label}）"
        parts.append(change)
        if acos_full is not None and acos_full > 35 and acos_recent < acos_full - 3:
            parts.append(f"但{window_label}整体仍偏高")

    return "；".join(parts) if parts else None


def strip_rule_jargon(text: str) -> str:
    """删除规则编号及英文等级（含括号内 BM-3 confirmed 等）"""
    if not text:
        return text
    t = _RULE_PAREN_PATTERN.sub("", text)
    t = _RULE_TAG_PATTERN.sub("", t)
    t = _LEVEL_ONLY_PATTERN.sub("", t)
    t = re.sub(r"^[提示说明检查][：:，,]?\s*", "", t, flags=re.M)
    t = re.sub(r"[\(（]\s*[\)）]", "", t)
    t = re.sub(r"\(\s*\)", "", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"，\s*，", "，", t)
    t = re.sub(r"；\s*；", "；", t)
    return t.strip()


def humanize_ops_text(text: str) -> str:
    """将残留的半窗/全窗/规则编号_echo 改为运营语言"""
    if not text:
        return text

    t = strip_rule_jargon(text)
    t = re.sub(r"半窗ACOS", "近期下半段ACOS", t)
    t = re.sub(r"半窗", "近期下半段", t)
    t = re.sub(r"全窗", "统计周期内", t)
    t = re.sub(r"全(\d+)日", r"近\1天整体", t)
    t = re.sub(r"近半窗", "最近几天", t)
    t = re.sub(r"前半窗", "统计周期前半段", t)
    t = re.sub(r"近\d+日(?=ACOS|CVR|花费)", lambda m: m.group(0).replace("日", "天"), t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"；\s*；", "；", t)
    return t.strip()


def sanitize_analysis_for_display(analysis: dict | None) -> dict:
    """报告 API 返回前最后一道清洗（overall + 各方向 analysis）"""
    if not analysis:
        return analysis or {}
    out = dict(analysis)
    if out.get("overall_analysis"):
        out["overall_analysis"] = humanize_ops_text(out["overall_analysis"])
    if out.get("skip_directions_note"):
        out["skip_directions_note"] = humanize_ops_text(out["skip_directions_note"])
    dirs = []
    for da in out.get("direction_analyses") or []:
        item = dict(da)
        for key in ("analysis", "reasoning"):
            if item.get(key):
                item[key] = humanize_ops_text(item[key])
        dirs.append(item)
    out["direction_analyses"] = dirs
    return out
