"""运营可读：时间窗表述与 BM/半窗术语清洗"""

from app.core.metrics_ops_language import (
    format_keyword_acos_change,
    humanize_ops_text,
    period_labels,
    sanitize_analysis_for_display,
    strip_rule_jargon,
)
from app.llm.reasoner import LLMReasoner


def test_period_labels_seven_days():
    """与 db_adapter 一致：7 日窗 = 前 4 天 + 后 3 天（half = days//2 为后半段天数）"""
    recent, prior, window, recent_lbl, prior_lbl = period_labels(7)
    assert recent == 3
    assert prior == 4
    assert window == "近7天"
    assert recent_lbl == "后3天"
    assert prior_lbl == "前4天"


def test_format_keyword_acos_change_ops_language():
    line = format_keyword_acos_change(7, 92.5, 92.5, 15.7, "明显改善")
    assert line is not None
    assert "近7天" in line
    assert "后3天" in line
    assert "92.5" in line
    assert "15.7" in line
    assert "半窗" not in line
    assert "全窗" not in line


def test_humanize_ops_text_half_window_jargon():
    out = humanize_ops_text("半窗ACOS改善但全窗仍高")
    assert "半窗" not in out
    assert "全窗" not in out
    assert "近期下半段" in out or "统计周期内" in out


def test_humanize_strips_bm4_hint():
    out = humanize_ops_text("BM-4提示排名不稳定，需关注核心词")
    assert "BM-4" not in out
    assert "BM4" not in out.upper().replace("-", "")


def test_strip_rule_jargon_bm_confirmed_parentheses():
    raw = (
        "整体ACOS 19.7%在目标区间[12%,30%]内（BM-3 confirmed），近4日ACOS从24.9%降至14.0%，"
        "波动在容忍范围内（BM-1 confirmed）。"
        "但4个词排名下滑（BM-4 suggest_optimize），3个竞品价格低于我方（BM-2 suggest_optimize），需关注。"
    )
    out = strip_rule_jargon(raw)
    for token in ("BM-1", "BM-2", "BM-3", "BM-4", "confirmed", "suggest_optimize"):
        assert token not in out, f"found {token} in {out!r}"


def test_sanitize_analysis_for_display():
    payload = sanitize_analysis_for_display({
        "overall_analysis": "（BM-3 confirmed）整体良好",
        "direction_analyses": [{"direction": "平衡维持", "analysis": "（BM-4 suggest_optimize）需关注"}],
    })
    assert "BM-" not in payload["overall_analysis"]
    assert "BM-" not in payload["direction_analyses"][0]["analysis"]


def test_sanitize_ops_text_bm4_via_reasoner():
    raw = "根据BM-4提示排名波动，判断需关注自然位。"
    cleaned = LLMReasoner._sanitize_ops_text(raw)
    assert "BM-4" not in cleaned
    assert "BM4" not in cleaned.replace("-", "")
