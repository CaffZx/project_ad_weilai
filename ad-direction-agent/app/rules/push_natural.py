"""推进自然位规则 PN-1~5"""

from app.rules import validation_rule
from app.models.asin_data import ASINData
from app.models.validation import ValidationItem, Evidence


@validation_rule(
    rule_id="PN-1",
    direction="push_natural",
    priority=0,
    description="检查是否有足够的上升词或优质位词来支撑推自然位策略",
)
async def check_rising_keywords(data: ASINData, thresholds: dict) -> ValidationItem | None:
    cfg = thresholds.get("push_natural", {}).get("pn_1", {})
    min_candidates = cfg.get("min_candidates", 2)
    rising_threshold = cfg.get("rising_rank_threshold", 3)
    well_positioned_max = cfg.get("well_positioned_max_rank", 20)

    if not data.keywords:
        return ValidationItem(
            rule_id="PN-1",
            level="force_correct",
            message="缺少关键词数据，无法判断是否有上升词",
            data_missing=True,
        )

    rising = [kw for kw in data.keywords if (kw.rank_change_14d or 0) >= rising_threshold]
    well_pos = [kw for kw in data.keywords if kw.natural_rank is not None and kw.natural_rank <= well_positioned_max]
    candidates = set(kw.keyword for kw in rising + well_pos)

    if len(candidates) >= min_candidates:
        detail = []
        if rising:
            detail.append(f"{len(rising)} 个上升词")
        if well_pos:
            detail.append(f"{len(well_pos)} 个自然位≤{well_positioned_max}的词")
        return ValidationItem(
            rule_id="PN-1",
            level="confirmed",
            message=f"有 {'、'.join(detail)}，适合推自然位",
            evidence=Evidence(current_value=len(candidates), threshold=min_candidates),
        )
    else:
        return ValidationItem(
            rule_id="PN-1",
            level="suggest_optimize",
            message=f"只有 {len(candidates)} 个候选词＜{min_candidates}，推自然位效益有限",
            evidence=Evidence(current_value=len(candidates), threshold=min_candidates),
            suggestion="建议考虑新增扩词或优化 ACOS",
        )


@validation_rule(
    rule_id="PN-2",
    direction="push_natural",
    priority=1,
    description="检查子选项中预算倾斜比例是否合理",
)
async def check_budget_ratio(data: ASINData, thresholds: dict, sub_options: dict | None = None) -> ValidationItem | None:
    if sub_options is None:
        return None
    ratio = sub_options.get("budget_ratio", 0)
    max_ratio = thresholds.get("push_natural", {}).get("pn_2", {}).get("max_budget_ratio", 50)

    if ratio > max_ratio:
        return ValidationItem(
            rule_id="PN-2",
            level="suggest_optimize",
            message=f"预算倾斜比例 {ratio}% > {max_ratio}%，可能导致其他词预算不足",
            evidence=Evidence(current_value=ratio, threshold=max_ratio),
            suggestion=max_ratio,
        )
    return ValidationItem(
        rule_id="PN-2",
        level="confirmed",
        message=f"预算倾斜比例 {ratio}% 在合理范围内",
        evidence=Evidence(current_value=ratio, threshold=max_ratio),
    )


@validation_rule(
    rule_id="PN-3",
    direction="push_natural",
    priority=0,
    description="检查是否有广告依赖型关键词（ACOS好但自然位差），可转向自然位突破",
)
async def check_ad_driven_keywords(data: ASINData, thresholds: dict) -> ValidationItem | None:
    cfg = thresholds.get("push_natural", {}).get("pn_3", {})
    max_acos = cfg.get("max_acos_for_ad_driven", 30)
    min_spend = cfg.get("min_spend_for_ad_driven", 50)
    rank_threshold = cfg.get("natural_rank_threshold", 20)

    ranked = [kw for kw in data.keywords if kw.natural_rank is not None and kw.acos is not None]
    if not ranked:
        return ValidationItem(
            rule_id="PN-3", level="force_correct",
            message="缺少关键词排名或ACOS数据，无法判断自然位提升空间",
            data_missing=True,
        )

    ad_driven = [
        kw for kw in ranked
        if kw.acos <= max_acos
        and kw.spend >= min_spend
        and kw.natural_rank > rank_threshold
    ]

    if ad_driven:
        return ValidationItem(
            rule_id="PN-3",
            level="confirmed",
            message=f"发现 {len(ad_driven)} 个广告依赖词（ACOS≤{max_acos}%但自然位>{rank_threshold}），推自然位可降低广告成本",
        )
    return ValidationItem(
        rule_id="PN-3",
        level="suggest_optimize",
        message="关键词自然位整体良好，推自然位空间有限",
    )


@validation_rule(
    rule_id="PN-4",
    direction="push_natural",
    priority=1,
    description="检查上升词的 ACOS 是否在可接受范围",
)
async def check_rising_keywords_acos(data: ASINData, thresholds: dict) -> ValidationItem | None:
    cfg = thresholds.get("push_natural", {}).get("pn_4", {})
    max_acos = cfg.get("max_acos_for_rising", 35)
    rank_threshold = thresholds.get("push_natural", {}).get("pn_1", {}).get("rising_rank_threshold", 5)

    rising = [kw for kw in data.keywords if (kw.rank_change_14d or 0) >= rank_threshold and kw.acos is not None]
    if not rising:
        return None

    high_acos = [kw for kw in rising if kw.acos > max_acos]
    if high_acos:
        return ValidationItem(
            rule_id="PN-4",
            level="suggest_optimize",
            message=f"{len(high_acos)} 个上升词 ACOS > {max_acos}%，加预算前需先优化",
            evidence=Evidence(threshold=max_acos),
        )
    return ValidationItem(
        rule_id="PN-4",
        level="confirmed",
        message=f"上升词平均 ACOS 在 {max_acos}% 容忍范围内",
    )


@validation_rule(
    rule_id="PN-5",
    direction="push_natural",
    priority=0,
    description="检查核心词自然位是否还有上升空间",
)
async def check_rank_headroom(data: ASINData, thresholds: dict) -> ValidationItem | None:
    min_rank = thresholds.get("push_natural", {}).get("pn_5", {}).get("min_current_rank", 5)

    ranked = [kw for kw in data.keywords if kw.natural_rank is not None]
    if not ranked:
        return ValidationItem(
            rule_id="PN-5", level="force_correct",
            message="缺少核心词自然排名数据",
            data_missing=True,
        )

    # 取最低（最优）的自然排名
    best_rank = min(kw.natural_rank for kw in ranked)

    if best_rank >= min_rank:
        return ValidationItem(
            rule_id="PN-5",
            level="confirmed",
            message=f"核心词最佳自然位 {best_rank}，仍有上升空间",
            evidence=Evidence(current_value=best_rank, threshold=min_rank),
        )
    return ValidationItem(
        rule_id="PN-5",
        level="suggest_optimize",
        message=f"核心词已进入 TOP {best_rank}，推自然位边际效益递减，建议考虑其他方向",
        evidence=Evidence(current_value=best_rank, threshold=min_rank),
    )
