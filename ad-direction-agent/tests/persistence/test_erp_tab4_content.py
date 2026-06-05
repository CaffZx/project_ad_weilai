"""Tab4 content_json and decision_basis_json — semicolon-split string arrays."""
from __future__ import annotations

import json

from app.persistence.erp_writer.erp_display import build_whip_display_fields
from app.persistence.erp_writer.text_utils import (
    build_conclusion_json_for_erp,
    build_wizard_direction_content_json,
    json_dumps,
    split_direction_analysis_to_lines,
    split_prose_to_display_lines,
    split_text_to_semicolon_lines,
)


def test_split_semicolon_chinese_and_ascii():
    text = "第一句；第二句;第三句"
    assert split_text_to_semicolon_lines(text) == ["第一句", "第二句", "第三句"]


def test_content_json_array_no_reason_key():
    d = {
        "reason": "根据近5日ACOS由30.1%升至71.6%；判断宜先控 ACOS；清理无效花费",
    }
    lines = build_wizard_direction_content_json(d, {}, None)
    assert lines == [
        "根据近5日ACOS由30.1%升至71.6%",
        "判断宜先控 ACOS",
        "清理无效花费",
    ]
    raw = json_dumps(lines)
    parsed = json.loads(raw)
    assert isinstance(parsed, list)
    assert "reason" not in raw


def test_decision_basis_from_analysis_only():
    analysis = {
        "overall_analysis": "当前处于推进期；ACOS恶化；应以优化ACOS为主",
        "direction_analyses": [{"direction": "优化ACOS", "analysis": "不应进 basis"}],
    }
    fields = build_whip_display_fields(analysis)
    assert fields.decision_basis == ["当前处于推进期", "ACOS恶化", "应以优化ACOS为主"]


def test_decision_basis_splits_on_period():
    text = "第一句。第二句。第三句"
    assert split_prose_to_display_lines(text) == ["第一句", "第二句", "第三句"]
    analysis = {"overall_analysis": text}
    assert build_whip_display_fields(analysis).decision_basis == ["第一句", "第二句", "第三句"]


def test_single_clause_no_semicolon():
    assert build_wizard_direction_content_json({"reason": "仅一句"}, {}, None) == ["仅一句"]


def test_split_direction_analysis_bullets_and_semicolon():
    text = (
        "【分析与建议】\n"
        "根据ACOS恶化，判断需优化；根据词A花费高，判断应降价\n"
        "• 否定词B精确匹配\n"
        "• 降低词C出价15%\n\n"
        "【后续关注】\n若7天ACOS未降至40%以下，需进一步降价"
    )
    lines = split_direction_analysis_to_lines(text)
    assert lines == [
        "根据ACOS恶化，判断需优化",
        "根据词A花费高，判断应降价",
        "否定词B精确匹配",
        "降低词C出价15%",
        "若7天ACOS未降至40%以下，需进一步降价",
    ]


def test_conclusion_json_direction_analyses_as_arrays():
    raw = {
        "overall_analysis": "概览",
        "direction_analyses": [
            {
                "direction": "优化ACOS",
                "analysis": "句一；句二\n• 动作A",
            }
        ],
    }
    out = build_conclusion_json_for_erp(raw)
    assert out["overall_analysis"] == ["概览"]
    assert out["direction_analyses"][0]["analysis"] == ["句一", "句二", "动作A"]


def test_conclusion_json_suggest_lines_from_p3():
    analysis = {"overall_analysis": "概览一句。"}
    wizard = {
        "p3": {
            "overall_reasoning": "【决策依据】依据A。【建议】建议一；建议二。【后续关注】关注一。",
        }
    }
    out = build_conclusion_json_for_erp(analysis, wizard)
    assert out["overall_analysis"] == ["概览一句"]
    assert out["suggest_lines"] == ["建议一", "建议二"]
    assert out["future_attention_lines"] == ["关注一"]


def test_conclusion_json_suggest_lines_from_new_p3_headers():
    analysis = {
        "overall_analysis": "概览一句。",
        "action_priorities": [{"action": "动作A"}, {"action": "动作B"}],
        "risk_warnings": ["风险X"],
    }
    wizard = {
        "p3": {
            "overall_reasoning": "【综合判断】依据A。【执行节奏】建议甲；建议乙。【风险提示】关注甲。",
        }
    }
    out = build_conclusion_json_for_erp(analysis, wizard)
    assert out["suggest_lines"] == ["建议甲", "建议乙"]
    assert out["future_attention_lines"] == ["关注甲"]


def test_conclusion_json_suggest_lines_fallback_action_priorities():
    analysis = {
        "overall_analysis": "概览一句。",
        "action_priorities": [{"action": "动作A"}, {"action": "动作B"}],
        "risk_warnings": ["风险X", "风险Y"],
    }
    wizard = {"p3": {"overall_reasoning": "【综合判断】仅依据，无建议段。"}}
    out = build_conclusion_json_for_erp(analysis, wizard)
    assert out["suggest_lines"] == ["动作A", "动作B"]
    assert out["future_attention_lines"] == ["风险X", "风险Y"]


def test_conclusion_lines_only_suggest():
    analysis = {
        "overall_analysis": "概览勿入。",
        "action_priorities": [{"action": "动作X"}],
    }
    wizard = {
        "p3": {
            "overall_reasoning": "【执行节奏】建议甲；建议乙。【风险提示】关注丙。",
        }
    }
    fields = build_whip_display_fields(analysis, wizard)
    assert fields.suggestion == ["建议甲", "建议乙"]
    assert "概览勿入" not in str(fields.suggestion)
    assert "关注丙" not in str(fields.suggestion)


def test_future_attention_array():
    analysis = {"overall_analysis": "概览。", "risk_warnings": ["风险X"]}
    wizard = {
        "p3": {
            "overall_reasoning": "【执行节奏】建议甲。【风险提示】关注丙。",
        }
    }
    fields = build_whip_display_fields(analysis, wizard)
    assert fields.future_attention == ["关注丙"]
    assert "建议甲" not in str(fields.future_attention)
    assert "概览" not in str(fields.future_attention)
