"""ACOS 约束预计算 — 15号§4 有效容忍上限 + 03号§7 目标 CPA。

纯函数模块，无 I/O、无副作用。所有输入取自 CampaignStrategyContext，不从外部配置或 LLM 输出读取。
输入接受中文（MySQL 实际值）或英文（KB/LLM 值），内部归一化为中文后查表。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.persistence.erp_writer.text_utils import normalize_enum


def _normalize(value: str) -> str:
    """英文/中文 → 中文权威值。"""
    return normalize_enum(value)


def _normalize_purposes(purposes: list[str]) -> list[str]:
    return [_normalize(p) for p in purposes]


# ── 15号§4.2 产品阶段加值 ──────────────────────────────────────────────────

_STAGE_ADDITION: dict[str, float] = {
    "测试期": 15.0,
    "推进期": 10.0,
    "收割利润期": 5.0,
    "维持期": 0.0,
    "清货期": 20.0,
}

# ── 15号§4.1A 经营模式系数 ─────────────────────────────────────────────────

_MODE_COEFFICIENT: dict[str, float] = {
    "立即退出": 0.0,
    "控制清货": 0.0,
    "获取利润": 0.0,
    "限时修复": 0.0,
    "稳定经营": 1.0,
    "积极推进": 1.0,
}


@dataclass
class AcosToleranceResult:
    """有效容忍上限计算结果。"""
    effective_tolerance: float          # 百分点，如 35.0 表示 35%
    components: dict[str, float]        # 每项明细，用于 evidence / 日志
    target_cpa: float | None            # 目标 CPA（美元）；avg_order_value 不可用时为 None
    avg_order_value: float | None       # 平均订单金额（美元）
    warnings: list[str] = field(default_factory=list)


def compute_acos_tolerance(
    *,
    target_acos: float,
    product_stage: str,
    operating_mode: str,
    ad_purposes: list[str],
    season_stage: str,
    avg_order_value: float | None,
    is_promotion: bool = False,
    is_women_clothing_refund: bool = False,
) -> AcosToleranceResult:
    """15号§4.1 有效容忍上限 + 03号§7 目标 CPA。

    Args:
        target_acos: 运营目标 ACOS（百分点值，如 25.0 表示 25%）
        product_stage: 产品阶段。支持中文（推进期）或英文（pushing），归一化后查表。
                       调用方负责：测试期超 60 天时应在传入前将 product_stage 修正为对应阶段，
                       本函数不检测超期（15号§4.2 C-17）。
        operating_mode: 经营模式。支持中文（稳定经营）或英文（stable_operation）。
                        控制清货的阶段联动在函数内处理。
        ad_purposes: 广告目的列表。支持中文（排名型）或英文（ranking）。
        season_stage: 季节阶段。支持中文（大旺季）或英文（peak）。
        avg_order_value: 平均订单金额（美元）。None 时 target_cpa 不计算。
        is_promotion: 本期固定 False。15号§4.1 的 +15pp 促销加值不生效。
                       不得用 discount_rate 或其他字段臆断促销状态。
        is_women_clothing_refund: 24号 PRC-005 命中时 True，−5pp。
                                  本期未接入判定逻辑，固定 False；接入后由调用方传入。
    """
    # ── 归一化：英文/中文 → 中文（单一权威源 text_utils._ENUM_NORMALIZE）──
    product_stage = _normalize(product_stage) or product_stage
    operating_mode = _normalize(operating_mode) or operating_mode
    season_stage = _normalize(season_stage) or season_stage
    ad_purposes = _normalize_purposes(ad_purposes)

    components: dict[str, float] = {}

    # ── 阶段加值（15号§4.2）──
    stage_addition = _STAGE_ADDITION.get(product_stage, 0.0)

    # ── 经营模式系数（15号§4.1A）──
    mode_coeff = _MODE_COEFFICIENT.get(operating_mode, 1.0)
    # 控制清货特殊：清货期阶段取 1（与清货方向一致），其余阶段取 0（收缩模式消解阶段正加值）
    if operating_mode == "控制清货":
        mode_coeff = 1.0 if product_stage == "清货期" else 0.0

    effective_stage = stage_addition * mode_coeff
    components["阶段加值"] = effective_stage

    # ── 广告目的加值 ──
    purpose_addition = 10.0 if "排名型" in ad_purposes else 0.0
    components["广告目的加值(ranking)"] = purpose_addition

    # ── 淡旺季加值 ──
    season_addition = 10.0 if season_stage in ("旺季准备", "大旺季") else 0.0
    components["淡旺季加值"] = season_addition

    # ── 促销加值 ──
    # TODO(promotion-context): 15号§4.1 的 promotion +15pp 本期固定为 0。
    # 不得用 discount_rate 或其他字段推断；待上游提供明确字段后再透传接入。
    promotion_addition = 15.0 if is_promotion else 0.0
    components["促销加值"] = promotion_addition

    # ── 女装毛利退货修正（24号 PRC-005）──
    # 本期未接入判定逻辑，is_women_clothing_refund 固定 False；
    # 接入后由调用方根据退货率/毛利率阈值计算传入。
    refund_correction = -5.0 if is_women_clothing_refund else 0.0
    components["女装退货修正"] = refund_correction

    # ── 汇总 ──
    effective_tolerance = (
        target_acos
        + effective_stage
        + purpose_addition
        + season_addition
        + promotion_addition
        + refund_correction
    )

    # ── 目标 CPA（03号§7）──
    # avg_order_value 不可用时 target_cpa 为 None，调用方按各自无单门槛规则处理。
    target_cpa: float | None = None
    if avg_order_value is not None and avg_order_value > 0:
        target_cpa = round(avg_order_value * (target_acos / 100.0), 2)

    return AcosToleranceResult(
        effective_tolerance=round(effective_tolerance, 2),
        components=components,
        target_cpa=target_cpa,
        avg_order_value=avg_order_value,
    )
