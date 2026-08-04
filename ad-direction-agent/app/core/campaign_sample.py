"""活动级样本不足判定。

该模块只提供 KB17 的事实判定，不负责阻止 MCP、清空候选或生成动作。
搜索词级样本不足仍由搜索词 bundle 按 KB31 单独计算。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class SampleAssessment:
    insufficient: bool
    reasons: tuple[str, ...]
    scope: Literal["campaign"] = "campaign"
    days_online: int | None = None
    clicks_7d: int | None = None
    cost_7d: float | None = None
    threshold: float | None = None
    data_missing: tuple[str, ...] = ()


def assess_campaign_sample(
    *,
    days_online: int | None,
    clicks_7d: int | None,
    cost_7d: float | None,
    target_cpa: float | None,
) -> SampleAssessment:
    """按 KB17 判定活动是否样本不足。

    规则：活动上线不足 3 天，或 7 天花费低于 ``max($5, target_cpa*0.5)``，
    或点击少于 10。未知值不当作 0；目标 CPA 缺失时使用知识库的 $5 下限，
    同时保留数据缺失标记。
    """
    reasons: list[str] = []
    missing: list[str] = []
    threshold = max(5.0, float(target_cpa) * 0.5) if target_cpa is not None else 5.0
    if target_cpa is None:
        missing.append("target_cpa")

    if days_online is None:
        missing.append("days_online")
    elif days_online >= 0 and days_online < 3:
        reasons.append("days_online_lt_3")

    if cost_7d is None:
        missing.append("cost_7d")
    elif float(cost_7d) < threshold:
        reasons.append("cost_7d_below_threshold")

    if clicks_7d is None:
        missing.append("clicks_7d")
    elif int(clicks_7d) < 10:
        reasons.append("clicks_7d_lt_10")

    return SampleAssessment(
        insufficient=bool(reasons),
        reasons=tuple(dict.fromkeys(reasons)),
        days_online=days_online,
        clicks_7d=clicks_7d,
        cost_7d=cost_7d,
        threshold=threshold,
        data_missing=tuple(dict.fromkeys(missing)),
    )
