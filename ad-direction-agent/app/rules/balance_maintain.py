"""平衡维持规则 BM-1~5"""

from app.rules import validation_rule
from app.models.asin_data import ASINData
from app.models.validation import ValidationItem, Evidence


@validation_rule(
    rule_id="BM-1",
    direction="balance_maintain",
    priority=0,
    description="检查各项指标波动是否在容忍范围内",
)
async def check_fluctuation(data: ASINData, thresholds: dict) -> ValidationItem | None:
    cfg = thresholds.get("balance_maintain", {}).get("bm_1", {})
    acos_tol = cfg.get("acos_fluctuation_tolerance", 15)
    cvr_tol = cfg.get("cvr_fluctuation_tolerance", 15)
    sales_tol = cfg.get("sales_fluctuation_tolerance", 15)

    issues = []

    # 检查 ACOS 是否偏高（acos_7d 无数据源，跳过波动对比）
    ad = data.ad_data
    if ad and ad.acos is not None:
        if ad.acos > 40:
            issues.append(f"ACOS {ad.acos:.0f}% 偏高")

    if data.avg_daily_sales_7d and data.avg_daily_sales_30d and data.avg_daily_sales_30d > 0:
        sales_change = abs(data.avg_daily_sales_7d - data.avg_daily_sales_30d) / data.avg_daily_sales_30d * 100
        if sales_change > sales_tol:
            issues.append(f"销量波动 {sales_change:.0f}% > {sales_tol}%")

    if issues:
        return ValidationItem(
            rule_id="BM-1",
            level="suggest_optimize",
            message="；".join(issues) + "，不建议维持现状",
        )
    return ValidationItem(
        rule_id="BM-1",
        level="confirmed",
        message="各项指标波动在容忍范围内，适合维持",
    )


@validation_rule(
    rule_id="BM-2",
    direction="balance_maintain",
    priority=0,
    description="检查是否有异常信号（库存预警、竞品威胁等）",
)
async def check_abnormal_signals(data: ASINData, thresholds: dict) -> ValidationItem | None:
    issues = []

    # 库存预警：inventory_qty == 0 或非常低
    if data.signals and data.signals.inventory_qty is not None:
        if data.signals.inventory_qty == 0:
            issues.append("库存为 0，存在断货风险")
        elif data.signals.inventory_qty < 50:
            issues.append(f"库存仅 {data.signals.inventory_qty} 件，偏低")

    # 竞品压力：竞品价格低于我方
    if data.price and data.competitors:
        cheaper = [c for c in data.competitors if c.price and c.price < data.price * 0.8]
        if len(cheaper) >= 3:
            issues.append(f"有 {len(cheaper)} 个竞品价格低于我方 20% 以上，存在价格战风险")

    if issues:
        return ValidationItem(
            rule_id="BM-2",
            level="suggest_optimize",
            message="；".join(issues) + "，需关注",
        )
    return ValidationItem(
        rule_id="BM-2",
        level="confirmed",
        message="无异常信号，可以维持",
    )


@validation_rule(
    rule_id="BM-3",
    direction="balance_maintain",
    priority=1,
    description="检查当前参数是否在合理区间",
)
async def check_parameters_reasonable(data: ASINData, thresholds: dict) -> ValidationItem | None:
    cfg = thresholds.get("balance_maintain", {}).get("bm_3", {})
    upper_factor = cfg.get("acos_upper_bound_factor", 1.2)
    lower_factor = cfg.get("acos_lower_bound_factor", 0.5)

    current_acos = data.ad_data.acos if data.ad_data else None
    if current_acos is None:
        return None

    acos_target = 25  # 默认目标
    upper = acos_target * upper_factor
    lower = acos_target * lower_factor

    if current_acos > upper:
        return ValidationItem(
            rule_id="BM-3",
            level="suggest_optimize",
            message=f"ACOS {current_acos}% > 目标上限 {upper:.0f}%，应优先优化而非维持",
            evidence=Evidence(current_value=current_acos, threshold=upper),
        )
    if current_acos < lower:
        return ValidationItem(
            rule_id="BM-3",
            level="suggest_optimize",
            message=f"ACOS {current_acos}% < {lower:.0f}%，远低于目标，可考虑扩词放量",
            evidence=Evidence(current_value=current_acos, threshold=lower),
        )
    return ValidationItem(
        rule_id="BM-3",
        level="confirmed",
        message=f"ACOS {current_acos}% 在合理区间 [{lower:.0f}%, {upper:.0f}%]",
        evidence=Evidence(current_value=current_acos, threshold=round(upper, 1)),
    )


@validation_rule(
    rule_id="BM-4",
    direction="balance_maintain",
    priority=1,
    description="检查核心词排名稳定性（基于 rank_change 和自然位数据）",
)
async def check_rank_stability(data: ASINData, thresholds: dict) -> ValidationItem | None:
    threshold = (
        thresholds.get("balance_maintain", {})
        .get("bm_4", {})
        .get("rank_change_threshold", 5)
    )

    if not data.keywords:
        return ValidationItem(
            rule_id="BM-4",
            level="force_correct",
            message="数据缺失：无关键词排名数据，无法评估排名稳定性",
            data_missing=True,
        )

    # 优先使用 rank_change_14d（有变化趋势数据时更精确）
    with_change = [kw for kw in data.keywords if kw.rank_change_14d is not None]
    if len(with_change) >= 3:
        unstable = [kw for kw in with_change if abs(kw.rank_change_14d) >= threshold]
        if unstable:
            return ValidationItem(
                rule_id="BM-4",
                level="suggest_optimize",
                message=f"{len(unstable)} 个词排名变化 ≥{threshold} 位，排名不稳定，需关注",
                evidence=Evidence(threshold=threshold),
            )
        return ValidationItem(
            rule_id="BM-4",
            level="confirmed",
            message="核心词排名稳定，适合维持",
        )

    # 降级：用自然位绝对值评估（有排名的词数量过少时不做判断）
    ranked = [kw for kw in data.keywords if kw.natural_rank is not None]
    if len(ranked) < 3:
        return ValidationItem(
            rule_id="BM-4",
            level="force_correct",
            message="排名数据不足（仅有 {} 个词有排名），无法评估稳定性".format(len(ranked)),
            data_missing=True,
        )

    return ValidationItem(
        rule_id="BM-4",
        level="confirmed",
        message=f"有 {len(ranked)} 个词有自然位数据，暂未发现剧烈波动",
    )


@validation_rule(
    rule_id="BM-5",
    direction="balance_maintain",
    priority=1,
    description="检查子选项 ACOS 容忍阈值是否合理",
)
async def check_acos_tolerance(data: ASINData, thresholds: dict, sub_options: dict | None = None) -> ValidationItem | None:
    if sub_options is None:
        return None

    cfg = thresholds.get("balance_maintain", {}).get("bm_5", {})
    min_tol = cfg.get("min_acos_tolerance", 3)
    max_tol = cfg.get("max_acos_tolerance", 20)

    tolerance = sub_options.get("acos_tolerance", 5)

    if tolerance < min_tol:
        return ValidationItem(
            rule_id="BM-5",
            level="suggest_optimize",
            message=f"ACOS 容忍阈值 {tolerance}% < {min_tol}%，过于敏感，会导致频繁误触发",
            evidence=Evidence(current_value=tolerance, threshold=min_tol),
            suggestion=min_tol,
        )
    if tolerance > max_tol:
        return ValidationItem(
            rule_id="BM-5",
            level="suggest_optimize",
            message=f"ACOS 容忍阈值 {tolerance}% > {max_tol}%，容忍度过高，可能错过优化时机",
            evidence=Evidence(current_value=tolerance, threshold=max_tol),
            suggestion=max_tol,
        )
    return ValidationItem(
        rule_id="BM-5",
        level="confirmed",
        message=f"ACOS 波动 {tolerance}% 触发调整，阈值合理",
    )
