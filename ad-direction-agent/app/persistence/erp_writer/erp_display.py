"""WHP 前端展示字段构建，对应 t_advert_agent_direction_recommend 表的三列。

decision_basis_json  → 决策依据
conclusion_json      → 建议
data_focus_json      → 后续关注
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .text_utils import build_conclusion_json_for_erp, split_prose_to_display_lines


@dataclass
class WHPDisplayFields:
    decision_basis: list[str]
    suggestion: list[str]
    future_attention: list[str]


def _lines_from_conclusion(out: dict[str, Any] | list[Any] | None, key: str) -> list[str]:
    if not isinstance(out, dict):
        return []
    val = out.get(key)
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    return []


def build_whip_display_fields(
    analysis: dict[str, Any] | None,
    wizard: dict[str, Any] | None = None,
) -> WHPDisplayFields:
    """Build WHP decision_basis / suggestion / future_attention in one pass."""
    analysis_dict = analysis if isinstance(analysis, dict) else {}
    conclusion = build_conclusion_json_for_erp(analysis_dict, wizard)
    return WHPDisplayFields(
        decision_basis=split_prose_to_display_lines(analysis_dict.get("overall_analysis")),
        suggestion=_lines_from_conclusion(conclusion, "suggest_lines"),
        future_attention=_lines_from_conclusion(conclusion, "future_attention_lines"),
    )
