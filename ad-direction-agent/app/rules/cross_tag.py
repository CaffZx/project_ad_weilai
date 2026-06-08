"""标签联动约束规则 — 跨方向/跨标签约束校验"""

from app.rules import validation_rule
from app.models.asin_data import ASINData
from app.models.validation import ValidationItem, Evidence


@validation_rule(
    rule_id="CROSS-1",
    direction="cross_tag",
    priority=0,
    description="测试阶段广告目的只能是 引流 或 卡位，不能是 盈利",
)
async def test_stage_ad_purpose(data: ASINData, thresholds: dict) -> ValidationItem | None:
    if data.product_stage != "测试期":
        return None

    disallowed = thresholds.get("cross_tag", {}).get("test_stage_forced_ad_purpose", {}).get("disallowed_purposes", ["盈利型"])
    if data.ad_purpose in disallowed:
        return ValidationItem(
            rule_id="CROSS-1",
            level="force_correct",
            message=f"{data.product_stage}产品广告目的不能是「{data.ad_purpose}」，{data.product_stage}应以引流或推排名为主",
            evidence=Evidence(current_value=data.ad_purpose or "", threshold=", ".join(disallowed)),
            suggestion="引流 或 卡位",
        )
    return None


@validation_rule(
    rule_id="CROSS-2",
    direction="cross_tag",
    priority=0,
    description="收割/维持阶段广告目的推荐 盈利 或 转化",
)
async def harvest_stage_ad_purpose(data: ASINData, thresholds: dict) -> ValidationItem | None:
    if data.product_stage not in ("收割利润期", "维持期"):
        return None

    allowed = thresholds.get("cross_tag", {}).get("harvest_stage_forced_ad_purpose", {}).get("allowed_purposes", ["盈利型", "转化型"])
    if data.ad_purpose not in allowed and data.ad_purpose is not None:
        return ValidationItem(
            rule_id="CROSS-2",
            level="suggest_optimize",
            message=f"{data.product_stage}建议广告目的为 {'/'.join(allowed)}，当前「{data.ad_purpose}」非最优",
            evidence=Evidence(current_value=data.ad_purpose or ""),
        )
    return None


@validation_rule(
    rule_id="CROSS-4",
    direction="cross_tag",
    priority=0,
    description="Broad 大词需要产品阶段不是测试阶段",
)
async def broad_keyword_stage_check(data: ASINData, thresholds: dict) -> ValidationItem | None:
    if data.target_keyword_strategy != "Broad":
        return None

    disallowed = thresholds.get("cross_tag", {}).get("broad_keyword_stage", {}).get("disallowed_stages", ["测试期", "起步期"])
    if data.product_stage in disallowed:
        return ValidationItem(
            rule_id="CROSS-4",
            level="force_correct",
            message=f"{data.product_stage}产品不推荐打 Broad 大词（评论/权重不足），建议选 Long-tail",
            suggestion="Long-tail",
        )
    return None


@validation_rule(
    rule_id="CROSS-5",
    direction="cross_tag",
    priority=0,
    description="检查广告方向与广告目的联动约束",
)
async def direction_ad_purpose_check(data: ASINData, thresholds: dict) -> ValidationItem | None:
    """expand_keywords 方向不能配 Profit 目的"""
    # direction 由 validation_engine 调用方传入
    # 此处只做基于 data 字段的通用检查
    return None


@validation_rule(
    rule_id="CROSS-6",
    direction="cross_tag",
    priority=0,
    description="特殊场景联动约束检查",
)
async def special_scenario_constraints(data: ASINData, thresholds: dict) -> ValidationItem | None:
    if data.signals is None:
        return None

    inv_days = data.signals.inventory_days
    if inv_days is not None and inv_days < 14:
        return ValidationItem(
            rule_id="CROSS-6",
            level="suggest_optimize",
            message=f"库存仅 {inv_days:.0f} 天 < 14，广告方向应选「平衡维持」或「优化 ACOS」",
            evidence=Evidence(current_value=inv_days, threshold=14),
        )

    return None


@validation_rule(
    rule_id="CROSS-7",
    direction="cross_tag",
    priority=1,
    description="旺季/旺季准备应防守品牌词，防止竞品截流",
)
async def peak_season_brand_defense(data: ASINData, thresholds: dict) -> ValidationItem | None:
    if data.season_stage not in ("大旺季", "旺季准备"):
        return None

    target_keyword_strategy = data.target_keyword_strategy or ""
    if "品牌词" not in target_keyword_strategy:
        return ValidationItem(
            rule_id="CROSS-7",
            level="suggest_optimize",
            message=f"当前处于{data.season_stage}，建议增加「品牌词」防守，防止竞品截流自有流量",
            evidence=Evidence(current_value=target_keyword_strategy, threshold="品牌词"),
            suggestion="增加品牌词防守",
        )
    return None


@validation_rule(
    rule_id="CROSS-8",
    direction="cross_tag",
    priority=1,
    description="收割/维持阶段 + 战略级/重点产品重点关注利润",
)
async def harvest_core_product_profit(data: ASINData, thresholds: dict) -> ValidationItem | None:
    if data.product_stage not in ("收割利润期", "维持期"):
        return None
    if data.product_level not in ("战略级产品", "重点产品"):
        return None
    if data.ad_purpose and "盈利型" not in (data.ad_purpose or ""):
        return ValidationItem(
            rule_id="CROSS-8",
            level="suggest_optimize",
            message=f"{data.product_stage}{data.product_level}产品建议包含「盈利型」广告目的，优先利润而非继续投入",
            evidence=Evidence(current_value=data.ad_purpose or "", threshold="盈利型"),
            suggestion="添加 盈利 广告目的",
        )
    return None


@validation_rule(
    rule_id="CROSS-10",
    direction="cross_tag",
    priority=1,
    description="旺季末期不建议大幅投入推进自然位",
)
async def peak_end_ranking_check(data: ASINData, thresholds: dict) -> ValidationItem | None:
    if data.season_stage != "旺季末期":
        return None
    # 此规则在校验 push_natural 方向时生效
    return ValidationItem(
        rule_id="CROSS-10",
        level="suggest_optimize",
        message="旺季末期流量即将下降，此时投入推进自然位 ROI 较低，建议转为优化 ACOS 收官",
        suggestion="考虑优化 ACOS 或平衡维持",
    )
