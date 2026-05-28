"""reasoner 运营文案后处理 — 决策依据趋势化"""

from app.llm.reasoner import LLMReasoner

_SAMPLE = """【决策依据】
产品处于收割利润期、旺季末期，核心词自然位稳固（约第3名），自然单占比约75%。
近4日ACOS在27-32%间波动，CVR约23-27%，整体ACOS约28%，有3个在投词ACOS超40%，同时有76个高转化词未收录。
平衡维持评分82分最高，优化ACOS 51分，新增扩词55分，推进自然位0分（核心词已前列，边际价值低）。

【建议】
以平衡维持为主方向。

【后续关注】
若ACOS持续高于35%。"""

_TREND_HINT = """  2025-05-13: ACOS=31.8%, CVR=23.5%
  2025-05-17: ACOS=26.9%, CVR=27.4%
  → 趋势摘要: 近4日ACOS从2025-05-13的31.8%变化至2025-05-17的26.9%；近4日CVR从2025-05-13的23.5%变化至2025-05-17的27.4%"""


def test_polish_splits_trend_judgments():
    out = LLMReasoner._polish_exec_reasoning(_SAMPLE)
    assert "【建议】" in out and "【后续关注】" in out
    assert "82分" not in out
    basis = out.split("【建议】")[0]
    assert basis.count("根据") >= 3
    assert basis.count("判断") >= 3
    assert "平衡维持评分" not in basis


def test_polish_rewrites_acos_range_with_trend_hint():
    text = """【决策依据】
近4日ACOS在26.9%-31.8%之间波动，CVR在23.5%-27.4%之间。

【建议】
维持。

【后续关注】
无。"""
    out = LLMReasoner._polish_sectioned_text(text, _TREND_HINT)
    basis = out.split("【建议】")[0]
    assert "变化至" in basis or "升至" in basis or "降至" in basis
    assert "26.9%-31.8%" not in basis or "从2025-05-13" in basis


def test_extract_trend_highlights():
    h = LLMReasoner._extract_trend_highlights(_TREND_HINT)
    assert "acos_narrative" in h
    assert "变化至" in h["acos_narrative"]


def test_sanitize_preserves_newlines():
    san = LLMReasoner._sanitize_ops_text(_SAMPLE)
    assert "\n【建议】" in san or san.strip().endswith("未收录。")


def test_sort_direction_analyses_by_score():
    analyses = [
        {"direction": "新增扩词", "analysis": "x"},
        {"direction": "平衡维持", "analysis": "y"},
        {"direction": "优化ACOS", "analysis": "z"},
    ]
    scores = [
        {"id": "balance_maintain", "label": "平衡维持", "suitability_score": 82},
        {"id": "expand_keywords", "label": "新增扩词", "suitability_score": 55},
        {"id": "optimize_acos", "label": "优化ACOS", "suitability_score": 51},
    ]
    sorted_a = LLMReasoner._sort_direction_analyses(analyses, scores)
    assert [a["direction"] for a in sorted_a] == ["平衡维持", "新增扩词", "优化ACOS"]
