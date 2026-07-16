"""核心词人工策略的无状态规则。"""

from __future__ import annotations


def normalize_core_keyword(value: str) -> str:
    """为策略表、离线过滤与 Campaign 核心判断生成稳定键。"""
    return " ".join(str(value or "").strip().split()).casefold()


def resolve_effective_core_keywords(
    ai_keywords: set[str] | list[str],
    policy_by_norm: dict[str, str],
) -> set[str]:
    """将独立人工策略叠加到最新离线 AI 核心词集合。"""
    effective = {normalize_core_keyword(keyword) for keyword in ai_keywords}
    for keyword, state in policy_by_norm.items():
        norm = normalize_core_keyword(keyword)
        if not norm:
            continue
        if state == "LOCKED":
            effective.add(norm)
        elif state in {"DISABLED", "VETOED"}:
            effective.discard(norm)
    return effective
