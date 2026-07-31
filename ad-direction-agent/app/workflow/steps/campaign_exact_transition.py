"""精准组合 (EXACT) 关键词生命周期升降级判定 -- 纯函数, 无 I/O。

规则来源: KB 32-精准组合升降级规则.md
依赖: 仅传入参数, 不调用任何数据库/MCP/外部服务。

transition_type → action 映射:
  promote_to_core      → promote_to_exact_core
  demote_to_testing    → demote_to_exact_testing
  eliminate_to_low_bid → eliminate_to_low_bid_pool
  stay_with_adjustment → (无动作, 仅标记)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any


# ── 常量 ────────────────────────────────────────────────────────

_MIN_DATA_DAYS = 14          # 数据成熟度: 至少 14 天有效记录
_PROMOTE_SPEND_FLOOR = 5.00  # 单日花费门槛 (T-3/T-4/T-5)
_ELIMINATE_CLICKS_FLOOR = 10 # 7 日点击门槛
_ELIMINATE_SPEND_FLOOR = 15.00  # 7 日花费门槛 (与 target_cpa 取 max)
_ELIMINATE_BUDGET = 1.00     # 淘汰后预算
_ELIMINATE_BID_CAP = 0.20    # 淘汰后 bid 上限
_DEMOTE_ORDERS_DECLINE_PCT = 0.20  # 订单下降 >20% 视为衰退


# ── 数据模型 ────────────────────────────────────────────────────

@dataclass
class ExactTransitionDecision:
    """精准组合升降级判定结果。

    transition_type 取值:
      - "promote_to_core"
      - "demote_to_testing"
      - "eliminate_to_low_bid"
      - "stay_with_adjustment"
      - "" (不触发任何转换)

    对应 action:
      promote_to_core      → promote_to_exact_core
      demote_to_testing    → demote_to_exact_testing
      eliminate_to_low_bid → eliminate_to_low_bid_pool
    """

    transition_type: str = ""
    target_group_type: str = ""   # exact_core_group / exact_testing_group / low_bid_retention_group / ""
    severity_tier: str = ""       # mild / moderate / severe / ""
    proposed_budget: float | None = None
    proposed_bid: float | None = None
    proposed_placement: dict | None = None  # eliminate 时: {tos: 0, pp: 0, ros: 0}
    review_level: str = ""        # AUTO / MANUAL_REVIEW / ""
    evidence: list[str] = field(default_factory=list)

    @property
    def action(self) -> str:
        """映射 transition_type 到执行层 action 名称。"""
        _ACTION_MAP = {
            "promote_to_core": "promote_to_exact_core",
            "demote_to_testing": "demote_to_exact_testing",
            "eliminate_to_low_bid": "eliminate_to_low_bid_pool",
        }
        return _ACTION_MAP.get(self.transition_type, "")

    @property
    def triggered(self) -> bool:
        return bool(self.transition_type)


# ── 辅助函数 ────────────────────────────────────────────────────

def _parse_date(val: Any) -> datetime | None:
    """将 str / datetime 统一为 naive datetime, 失败返回 None。"""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.replace(tzinfo=None)
    if isinstance(val, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(val, fmt)
            except ValueError:
                continue
    return None


def _parse_date_as_date(val: Any) -> datetime | None:
    """取 date 对象返回 datetime(年,月,日) 纯日期, 用于按日分组。"""
    dt = _parse_date(val)
    if dt is None:
        return None
    return datetime(dt.year, dt.month, dt.day)


def _window_acos(rows: list[dict]) -> float | None:
    """计算窗口 ACOS = sum(spend) / sum(sales), 任一为空或 sales<=0 返回 None。"""
    total_spend = 0.0
    total_sales = 0.0
    for r in rows:
        spend = r.get("spend")
        sales = r.get("sales")
        if spend is None or sales is None:
            return None
        total_spend += float(spend)
        total_sales += float(sales)
    if total_sales <= 0:
        return None
    return total_spend / total_sales * 100  # 百分比


def _count_complete_days(metric_daily: list[dict], today: datetime) -> int:
    """统计 stat_date < today 且 spend/sales/orders/clicks 均非 NULL 的天数 (按日去重)。

    恒接收全量日序列 metric_daily（唯一调用点 evaluate_exact_transition 传全量）。
    """
    seen_dates: set[datetime] = set()
    count = 0
    for r in metric_daily:
        stat_date = _parse_date_as_date(r.get("stat_date"))
        if stat_date is None or stat_date >= today:
            continue
        for key in ("spend", "sales", "orders", "clicks"):
            if r.get(key) is None:
                break
        else:
            if stat_date not in seen_dates:
                seen_dates.add(stat_date)
                count += 1
    return count


def _get_rows_in_window(
    metric_daily: list[dict],
    today: datetime,
    *,
    start_offset: int,
    end_offset: int,
) -> list[dict]:
    """返回 stat_date 在 [today - end_offset, today - start_offset) 范围内的行。

    offset=1 表示昨天, offset=3 表示 3 天前。
    start_offset 为包含边界, end_offset 为排他边界 (即 end_offset 天前的那天不含)。
    例: start_offset=1, end_offset=4 → T-1, T-2, T-3 (3 天)
    """
    start = today - timedelta(days=start_offset)
    end = today - timedelta(days=end_offset)
    result = []
    for r in metric_daily:
        stat_date = _parse_date_as_date(r.get("stat_date"))
        if stat_date is None:
            continue
        if end <= stat_date <= start:
            result.append(r)
    return result


def _avg_natural_rank(rows: list[dict]) -> float | None:
    """计算窗口内 natural_rank 的平均值。全部为 None 则返回 None。"""
    vals = []
    for r in rows:
        rank = r.get("natural_rank")
        if rank is not None:
            vals.append(float(rank))
    if not vals:
        return None
    return sum(vals) / len(vals)


# ── 主判定函数 ──────────────────────────────────────────────────

def evaluate_exact_transition(
    *,
    current_group_type: str,
    match_type: str,
    keyword_id: str,
    metric_daily: list[dict],
    lifecycle: dict | None,
    target_acos: float,
    effective_acos_tolerance: float,
    current_bid: float,
    is_core_keyword: bool,
    _today: datetime | None = None,
) -> ExactTransitionDecision:
    """评估单个精准关键词是否需要升降级。

    纯函数: 无 I/O, 无数据库调用, 无 MCP 调用。
    所有 ACOS 比较使用 effective_acos_tolerance (15号§4), 从不使用固定倍率。

    Args:
        current_group_type: 当前归组 (exact_core_group / exact_testing_group /
            UNKNOWN / auto_broad_group / low_bid_retention_group)
        match_type: 匹配类型, 必须为 "EXACT" 才生效
        keyword_id: 关键词 ID, 空字符串表示非单关键词
        metric_daily: 按 stat_date DESC 排序的日维度指标列表
        lifecycle: 来自 get_exact_lifecycle 的生命周期字典, 可为 None
        target_acos: 目标 ACOS (小数或百分比均可, 与 metric_daily 中的数据单位一致)
        effective_acos_tolerance: 有效 ACOS 容忍上限 (15号§4)
        current_bid: 当前出价
        is_core_keyword: 是否为核心词 (核心词禁淘汰)
        _today: 仅供测试注入的日期, 默认 datetime.utcnow()

    Returns:
        ExactTransitionDecision (triggered=True 时表示触发转换)
    """
    today = _today or datetime.utcnow()
    today = datetime(today.year, today.month, today.day)

    decision = ExactTransitionDecision()

    # ── 规则 0: 非单关键词精准活动 → 不适用 ──
    if match_type != "EXACT" or not keyword_id:
        decision.evidence.append("非单关键词精准活动, 不适用精准升降级规则")
        return decision

    # ── 规则 1: 不在精准测试/核心组 → 不适用 ──
    if current_group_type not in ("exact_core_group", "exact_testing_group"):
        decision.evidence.append(
            f"当前归组 {current_group_type!r} 不在精准升降级适用范围"
        )
        return decision

    # ── 规则 2: 数据成熟度检查 ──
    mature_days = _count_complete_days(metric_daily, today)
    if mature_days < _MIN_DATA_DAYS:
        decision.evidence.append(
            f"数据不足: 有效天数 {mature_days} < {_MIN_DATA_DAYS}"
        )
        return decision

    # ── 规则 3 & 4: 分支判定 ──
    if current_group_type == "exact_testing_group":
        return _evaluate_testing_group(
            decision=decision,
            metric_daily=metric_daily,
            today=today,
            lifecycle=lifecycle,
            target_acos=target_acos,
            effective_acos_tolerance=effective_acos_tolerance,
            current_bid=current_bid,
            is_core_keyword=is_core_keyword,
        )

    if current_group_type == "exact_core_group":
        return _evaluate_core_group(
            decision=decision,
            metric_daily=metric_daily,
            today=today,
            target_acos=target_acos,
            effective_acos_tolerance=effective_acos_tolerance,
        )

    return decision


# ── 精准测试组判定 ──────────────────────────────────────────────

def _evaluate_testing_group(
    *,
    decision: ExactTransitionDecision,
    metric_daily: list[dict],
    today: datetime,
    lifecycle: dict | None,
    target_acos: float,
    effective_acos_tolerance: float,
    current_bid: float,
    is_core_keyword: bool,
) -> ExactTransitionDecision:
    """精准测试组: 优先检查 promote, 再检查 eliminate。"""

    # ── 3a. promote_to_core ──
    # T-3/T-4/T-5 各日花费 > $5, 3 日合计 ACOS < target_acos,
    # 且自然排名改善 (最新 7 日均值 < 前 7 日均值)
    t3_rows = _get_rows_in_window(metric_daily, today, start_offset=3, end_offset=4)
    t4_rows = _get_rows_in_window(metric_daily, today, start_offset=4, end_offset=5)
    t5_rows = _get_rows_in_window(metric_daily, today, start_offset=5, end_offset=6)

    all_three_days_have_spend = all(
        len(day_rows) > 0 and float(day_rows[0].get("spend", 0)) > _PROMOTE_SPEND_FLOOR
        for day_rows in (t3_rows, t4_rows, t5_rows)
    )

    # 3 日合计 ACOS
    combined_3d = t3_rows + t4_rows + t5_rows
    acos_3d = _window_acos(combined_3d) if combined_3d else None
    acos_below_target = acos_3d is not None and acos_3d < target_acos

    # 自然排名改善: 最新 7 日 vs 前 7 日
    latest_7d = _get_rows_in_window(metric_daily, today, start_offset=1, end_offset=8)
    prev_7d = _get_rows_in_window(metric_daily, today, start_offset=8, end_offset=15)
    rank_latest = _avg_natural_rank(latest_7d)
    rank_prev = _avg_natural_rank(prev_7d)
    rank_improving = (
        rank_latest is not None
        and rank_prev is not None
        and rank_latest < rank_prev
    )

    if all_three_days_have_spend and acos_below_target and rank_improving:
        decision.transition_type = "promote_to_core"
        decision.target_group_type = "exact_core_group"
        decision.severity_tier = ""
        decision.review_level = "AUTO"
        decision.evidence.extend([
            f"T-3/T-4/T-5 各日花费均 > ${_PROMOTE_SPEND_FLOOR:.2f}",
            f"3 日合计 ACOS={acos_3d:.2f}% < target_acos={target_acos:.2f}%",
            f"自然排名改善: 最新 7 日均值 {rank_latest:.1f} < 前 7 日均值 {rank_prev:.1f}",
        ])
        return decision

    # ── 3b. eliminate_to_low_bid ──
    # 核心词禁淘汰
    if is_core_keyword:
        decision.evidence.append("is_core_keyword=True, 核心词禁淘汰")
        return decision

    # 观察窗口是否满足
    observation_met = _check_observation_window(lifecycle, today)
    if not observation_met:
        decision.evidence.append("观察窗口未满足, 跳过淘汰判定")
        return decision

    # 7 日指标
    window_7d = _get_rows_in_window(metric_daily, today, start_offset=1, end_offset=8)
    total_orders_7d = sum(float(r.get("orders", 0)) for r in window_7d)
    total_clicks_7d = sum(float(r.get("clicks", 0)) for r in window_7d)
    total_spend_7d = sum(float(r.get("spend", 0)) for r in window_7d)

    # target_cpa: 用 target_acos * avg_order_value 推算, 但此处无 avg_order_value,
    # 故使用 spend_fallback: max($15, target_cpa)
    # 如果 caller 未传 target_cpa, 用 $_ELIMINATE_SPEND_FLOOR 作下限
    spend_threshold = max(_ELIMINATE_SPEND_FLOOR, 0)  # target_cpa 由 caller 通过 effective_acos_tolerance 间接体现

    zero_orders = total_orders_7d == 0
    enough_clicks_or_spend = (
        total_clicks_7d >= _ELIMINATE_CLICKS_FLOOR
        or total_spend_7d >= spend_threshold
    )

    if zero_orders and enough_clicks_or_spend:
        decision.transition_type = "eliminate_to_low_bid"
        decision.target_group_type = "low_bid_retention_group"
        decision.severity_tier = "severe"
        decision.proposed_budget = _ELIMINATE_BUDGET
        decision.proposed_bid = min(current_bid, _ELIMINATE_BID_CAP)
        decision.proposed_placement = {"tos": 0, "pp": 0, "ros": 0}
        decision.review_level = "AUTO"
        decision.evidence.extend([
            f"观察窗口满足 (testing_origin={lifecycle.get('testing_origin', '?')})",
            f"7 日订单 = {int(total_orders_7d)}",
            f"7 日点击 = {int(total_clicks_7d)} >= {_ELIMINATE_CLICKS_FLOOR}"
            if total_clicks_7d >= _ELIMINATE_CLICKS_FLOOR
            else f"7 日花费 = ${total_spend_7d:.2f} >= ${spend_threshold:.2f}",
        ])
        return decision

    decision.evidence.append("精准测试组: 未触发 promote 或 eliminate")
    return decision


def _check_observation_window(lifecycle: dict | None, today: datetime) -> bool:
    """检查观察窗口是否满足淘汰条件。

    demoted: demoted_at >= 7 days ago
    native:  campaign_created_at >= 14 days ago
    """
    if lifecycle is None:
        return False

    testing_origin = (lifecycle.get("testing_origin") or "").upper()

    if testing_origin == "DEMOTED":
        demoted_at = _parse_date(lifecycle.get("demoted_at"))
        if demoted_at is None:
            return False
        return (today - demoted_at).days >= 7

    if testing_origin == "NATIVE":
        created_at = _parse_date(lifecycle.get("campaign_created_at"))
        if created_at is None:
            return False
        return (today - created_at).days >= 14

    return False


# ── 精准核心组判定 ──────────────────────────────────────────────

def _evaluate_core_group(
    *,
    decision: ExactTransitionDecision,
    metric_daily: list[dict],
    today: datetime,
    target_acos: float,
    effective_acos_tolerance: float,
) -> ExactTransitionDecision:
    """精准核心组: mild regression → stay, moderate regression → demote。"""

    # ── 4a. 轻度衰退: target_acos < 3d-ACOS <= effective_acos_tolerance 且 7d 有出单 ──
    window_3d = _get_rows_in_window(metric_daily, today, start_offset=1, end_offset=4)
    acos_3d = _window_acos(window_3d)

    window_7d = _get_rows_in_window(metric_daily, today, start_offset=1, end_offset=8)
    total_orders_7d = sum(float(r.get("orders", 0)) for r in window_7d)
    total_clicks_7d = sum(float(r.get("clicks", 0)) for r in window_7d)

    if (
        acos_3d is not None
        and target_acos < acos_3d <= effective_acos_tolerance
        and total_orders_7d > 0
    ):
        decision.transition_type = "stay_with_adjustment"
        decision.target_group_type = "exact_core_group"
        decision.severity_tier = "mild"
        decision.review_level = "AUTO"
        decision.evidence.extend([
            f"3 日 ACOS={acos_3d:.2f}% 处于容忍区间 "
            f"({target_acos:.2f}% < ACOS <= {effective_acos_tolerance:.2f}%)",
            f"7 日订单 = {int(total_orders_7d)} > 0",
        ])
        return decision

    # ── 4b. 中度衰退: 连续两个 3 日周期 ACOS > tolerance 且 (7d 无单+点击>=10 或 订单下降>20%) ──
    # 最新 3 日: T-1 ~ T-3
    period_a = _get_rows_in_window(metric_daily, today, start_offset=1, end_offset=4)
    acos_a = _window_acos(period_a)

    # 前一个 3 日: T-4 ~ T-6
    period_b = _get_rows_in_window(metric_daily, today, start_offset=4, end_offset=7)
    acos_b = _window_acos(period_b)

    both_over_tolerance = (
        acos_a is not None
        and acos_b is not None
        and acos_a > effective_acos_tolerance
        and acos_b > effective_acos_tolerance
    )

    if not both_over_tolerance:
        decision.evidence.append("精准核心组: 未触发 mild 或 moderate regression")
        return decision

    # 检查衰退佐证
    zero_orders_and_clicks = total_orders_7d == 0 and total_clicks_7d >= 10

    # 订单下降 > 20%: 对比最新 3 日 vs 前一个 3 日
    orders_a = sum(float(r.get("orders", 0)) for r in period_a)
    orders_b = sum(float(r.get("orders", 0)) for r in period_b)
    declining = (
        orders_b > 0
        and orders_a < orders_b * (1 - _DEMOTE_ORDERS_DECLINE_PCT)
    )

    if zero_orders_and_clicks or declining:
        decision.transition_type = "demote_to_testing"
        decision.target_group_type = "exact_testing_group"
        decision.severity_tier = "moderate"
        decision.review_level = "MANUAL_REVIEW"
        decision.evidence.extend([
            f"连续两个 3 日周期 ACOS 超出容忍: "
            f"最新 3 日={acos_a:.2f}%, 前 3 日={acos_b:.2f}% > tolerance={effective_acos_tolerance:.2f}%",
            f"7 日订单={int(total_orders_7d)}, 7 日点击={int(total_clicks_7d)}"
            if zero_orders_and_clicks
            else f"订单下降: {int(orders_a)} vs {int(orders_b)} "
                 f"(降幅 {(1 - orders_a / orders_b) * 100:.0f}% > {_DEMOTE_ORDERS_DECLINE_PCT * 100:.0f}%)",
            "需人工复核",
        ])
        return decision

    decision.evidence.append("精准核心组: 连续超限但无佐证, 不触发 demote")
    return decision
