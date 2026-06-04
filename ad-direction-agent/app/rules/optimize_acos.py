"""优化 ACOS 规则 OA-1~4"""

from app.rules import validation_rule
from app.models.asin_data import ASINData
from app.models.validation import ValidationItem, Evidence


@validation_rule(
    rule_id="OA-1",
    direction="optimize_acos",
    priority=0,
    description="检查是否有 ACOS 超标的词需要处理",
)
async def check_acos_overage(data: ASINData, thresholds: dict) -> ValidationItem | None:
    cfg = thresholds.get("optimize_acos", {}).get("oa_1", {})
    warning_threshold = cfg.get("acos_warning_threshold", 30)
    force_threshold = cfg.get("acos_force_threshold", 50)

    keywords_with_acos = [kw for kw in data.keywords if kw.acos is not None]
    if not keywords_with_acos:
        return ValidationItem(
            rule_id="OA-1", level="force_correct",
            message="缺少关键词 ACOS 数据",
            data_missing=True,
        )

    force = [kw for kw in keywords_with_acos if kw.acos >= force_threshold]
    warning = [kw for kw in keywords_with_acos if warning_threshold <= kw.acos < force_threshold]

    if force:
        return ValidationItem(
            rule_id="OA-1",
            level="force_correct",
            message=f"{len(force)} 个词 ACOS ≥ {force_threshold}%（严重超标），必须优化",
            evidence=Evidence(threshold=force_threshold),
        )
    if warning:
        return ValidationItem(
            rule_id="OA-1",
            level="suggest_optimize",
            message=f"{len(warning)} 个词 ACOS 在 {warning_threshold}%~{force_threshold}% 之间，建议优化",
            evidence=Evidence(threshold=warning_threshold),
        )
    return ValidationItem(
        rule_id="OA-1",
        level="confirmed",
        message=f"所有词 ACOS 均 < {warning_threshold}%，无超标词",
        evidence=Evidence(threshold=warning_threshold),
    )


@validation_rule(
    rule_id="OA-2",
    direction="optimize_acos",
    priority=0,
    description="检查有无高花费零转化的词",
)
async def check_high_spend_zero_conversion(data: ASINData, thresholds: dict) -> ValidationItem | None:
    threshold = (
        thresholds.get("optimize_acos", {})
        .get("oa_2", {})
        .get("high_spend_zero_convert_threshold", 15)
    )

    wasteful = [
        kw for kw in data.keywords
        if kw.orders == 0 and kw.spend >= threshold
    ]

    if wasteful:
        return ValidationItem(
            rule_id="OA-2",
            level="force_correct",
            message=f"{len(wasteful)} 个词花费 ≥${threshold:.0f} 但 0 转化，建议暂停或否定",
            evidence=Evidence(threshold=threshold),
        )
    return ValidationItem(
        rule_id="OA-2",
        level="confirmed",
        message="无高花费零转化词",
    )


@validation_rule(
    rule_id="OA-3",
    direction="optimize_acos",
    priority=1,
    description="检查是否有 Bid 下调空间（Bid 显著高于实际 CPC）",
)
async def check_bid_headroom(data: ASINData, thresholds: dict) -> ValidationItem | None:
    factor = (
        thresholds.get("optimize_acos", {})
        .get("oa_3", {})
        .get("bid_reduction_headroom", 1.5)
    )

    adjustable = []
    for kw in data.keywords:
        if kw.bid is not None and kw.clicks > 0:
            effective_cpc = kw.spend / kw.clicks
            if kw.bid > effective_cpc * factor:
                adjustable.append(kw)

    if adjustable:
        return ValidationItem(
            rule_id="OA-3",
            level="confirmed",
            message=f"{len(adjustable)} 个词 Bid 显著高于实际 CPC，有下调空间",
        )
    return ValidationItem(
        rule_id="OA-3",
        level="suggest_optimize",
        message="各词 Bid 与实际 CPC 差距不大，无显著下调空间",
    )


@validation_rule(
    rule_id="OA-4",
    direction="optimize_acos",
    priority=1,
    description="检查否定词机会",
)
async def check_negative_keyword_opportunities(data: ASINData, thresholds: dict) -> ValidationItem | None:
    min_impressions = (
        thresholds.get("optimize_acos", {})
        .get("oa_4", {})
        .get("min_impressions_for_negative", 500)
    )

    candidates = [
        kw for kw in data.keywords
        if kw.impressions >= min_impressions
        and kw.clicks > 0
        and kw.orders == 0
    ]

    if candidates:
        return ValidationItem(
            rule_id="OA-4",
            level="confirmed",
            message=f"发现 {len(candidates)} 个展示量 ≥{min_impressions} 且 0 转化的词，可考虑添加否定",
            evidence=Evidence(threshold=min_impressions),
        )
    return ValidationItem(
        rule_id="OA-4",
        level="suggest_optimize",
        message="当前没有明显的否定词候选",
    )
