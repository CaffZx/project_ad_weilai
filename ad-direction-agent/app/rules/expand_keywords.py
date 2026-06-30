"""新增扩词规则 KE-1~4"""

from app.rules import validation_rule
from app.models.asin_data import ASINData
from app.models.validation import ValidationItem, Evidence


@validation_rule(
    rule_id="KE-1",
    direction="expand_keywords",
    priority=0,
    description="检查搜索词报告中是否有合格的新词可扩",
)
async def check_qualified_new_keywords(data: ASINData, thresholds: dict) -> ValidationItem | None:
    cfg = thresholds.get("expand_keywords", {}).get("ke_1", {})
    min_qualified = cfg.get("min_qualified_keywords", 2)
    max_acos = cfg.get("max_acos_for_new", 30)

    qualified = data.available_new_keywords
    if qualified is None:
        return ValidationItem(
            rule_id="KE-1", level="force_correct",
            message="缺少搜索词报告数据，无法判断是否有新词可扩",
            data_missing=True,
        )

    if qualified >= min_qualified:
        return ValidationItem(
            rule_id="KE-1",
            level="confirmed",
            message=f"搜索词报告中有 {qualified} 个高转化未收录词，有词可扩",
            evidence=Evidence(current_value=qualified, threshold=min_qualified),
        )
    return ValidationItem(
        rule_id="KE-1",
        level="suggest_optimize",
        message=f"只有 {qualified} 个合格新词 ＜ {min_qualified}，扩词价值有限",
        evidence=Evidence(current_value=qualified, threshold=min_qualified),
    )


@validation_rule(
    rule_id="KE-2",
    direction="expand_keywords",
    priority=0,
    description="检查当前关键词覆盖率是否偏低",
)
async def check_keyword_coverage(data: ASINData, thresholds: dict) -> ValidationItem | None:
    max_count = thresholds.get("expand_keywords", {}).get("ke_2", {}).get("max_keyword_count", 10)

    count = data.keyword_count if data.keyword_count is not None else len(data.keywords)

    if count == 0:
        return ValidationItem(
            rule_id="KE-2", level="force_correct",
            message="缺少关键词数据，无法评估覆盖率",
            data_missing=True,
        )
    if count < max_count:
        return ValidationItem(
            rule_id="KE-2",
            level="confirmed",
            message=f"当前手动广告覆盖 {count} 个词 < {max_count} 个，覆盖率偏低，有扩词空间",
            evidence=Evidence(current_value=count, threshold=max_count),
        )
    return ValidationItem(
        rule_id="KE-2",
        level="suggest_optimize",
        message=f"当前覆盖 {count} 个词 ≥ {max_count}，覆盖率充足，优先优化现有词效率",
        evidence=Evidence(current_value=count, threshold=max_count),
    )


@validation_rule(
    rule_id="KE-3",
    direction="expand_keywords",
    priority=1,
    description="检查现有词 ACOS 是否健康，能否承载新词",
)
async def check_existing_keywords_acos(data: ASINData, thresholds: dict) -> ValidationItem | None:
    cfg = thresholds.get("expand_keywords", {}).get("ke_3", {})
    factor = cfg.get("acos_threshold_factor", 1.2)

    existing = [kw for kw in data.keywords if kw.acos is not None]
    if not existing:
        return ValidationItem(
            rule_id="KE-3", level="force_correct",
            message="缺少现有词 ACOS 数据",
            data_missing=True,
        )

    avg_acos = sum(kw.acos for kw in existing) / len(existing)
    # 用数据里的 ACOS 目标，没有则用阈值配置
    acos_target = data.ad_data.acos if data.ad_data and data.ad_data.acos else 25
    threshold = acos_target * factor

    if avg_acos < threshold:
        return ValidationItem(
            rule_id="KE-3",
            level="confirmed",
            message=f"现有词平均 ACOS {avg_acos:.1f}% < {threshold:.0f}%，可承载新词",
            evidence=Evidence(current_value=round(avg_acos, 1), threshold=round(threshold, 1)),
        )
    return ValidationItem(
        rule_id="KE-3",
        level="suggest_optimize",
        message=f"现有词平均 ACOS {avg_acos:.1f}% ≥ {threshold:.0f}%，建议先优化再扩词",
        evidence=Evidence(current_value=round(avg_acos, 1), threshold=round(threshold, 1)),
    )


@validation_rule(
    rule_id="KE-4",
    direction="expand_keywords",
    priority=1,
    description="检查单次新增关键词数量是否合理",
)
async def check_target_keyword_count(data: ASINData, thresholds: dict, sub_options: dict | None = None) -> ValidationItem | None:
    if sub_options is None:
        return None

    cfg = thresholds.get("expand_keywords", {}).get("ke_4", {})
    recommended = cfg.get("recommended_new_count", 5)
    max_new = cfg.get("max_new_count", 15)

    target = sub_options.get("target_count", recommended)

    if target > max_new:
        return ValidationItem(
            rule_id="KE-4",
            level="force_correct",
            message=f"单次新增 {target} 个词 > 上限 {max_new} 个，请减少数量",
            evidence=Evidence(current_value=target, threshold=max_new),
            suggestion=max_new,
        )
    if target > recommended * 2:
        return ValidationItem(
            rule_id="KE-4",
            level="suggest_optimize",
            message=f"你选了 {target} 个新词，建议从 {recommended} 个开始，先验证词质量再放量",
            evidence=Evidence(current_value=target, threshold=recommended),
            suggestion=recommended,
        )
    return ValidationItem(
        rule_id="KE-4",
        level="confirmed",
        message=f"计划新增 {target} 个词，数量合理",
    )
