"""Campaign 4 组合预算汇总聚合器 (纯代码,无 LLM)。

输入: adjustments(LLM 输出) + all_units(分析前活动列表) + strategy_context
输出: dict 结构 (见 build_summary 返回值)。

⚠️ 不聚合 AI 推荐预算 (Σ proposed_budget):
    LLM 是按活动独立给建议,把它们加总成 ASIN 级数字无宏观意义。
    本聚合器只产出:
      - current_budget_sum  : 各组合当前预算合计 (事实)
      - target_amount       : 各组合预算约束 = daily_budget × target_share_pct (常数)
      - campaign_keys       : 各组合活动列表 (前端按组合筛选 + 动态算"已同意"用)
    "已同意预算合计" 由前端动态算 (后端不感知运营点击同意的状态)。
    "ASIN 总变化 ±50% 硬约束 (KB 03 §7)" 也由前端按 "已同意 vs 当前" 动态触发。

数据守恒约束 (assert):
  Σ portfolios[*].current_budget_sum ≈ Σ adjustments 对应 unit 的 current_budget
  4 组合互斥: Σ counts == len(adjustments)

依据:
  - 占比来自 settings (50/30/15/5),v1 单一默认值,后续运营给表后迁 TOML
  - daily_budget 来自 strategy_context.daily_budget (已三级兜底,见 build_campaign_strategy_context)
  - KB 21 §6 淘汰组固定 $1.00 (此处不参与 AI sum,但仍记 campaign_keys 给前端)
"""

from __future__ import annotations

import logging

from app.config.settings import settings
from app.models.campaign import CampaignAdjustmentItem, CampaignStrategyContext, CampaignUnit
from app.workflow.steps.campaign_portfolio import ALL_PORTFOLIOS

logger = logging.getLogger(__name__)


def _default_share_pcts() -> dict[str, int]:
    """4 组合默认占比 (settings)。"""
    return {
        "主推": settings.campaign_portfolio_share_main,
        "广泛/自动": settings.campaign_portfolio_share_broad,
        "测试/新增": settings.campaign_portfolio_share_test,
        "淘汰": settings.campaign_portfolio_share_eliminate,
    }


def build_summary(
    adjustments: list[CampaignAdjustmentItem],
    all_units: list[CampaignUnit],
    ctx: CampaignStrategyContext,
) -> dict:
    """构建 4 组合预算汇总。

    Returns:
      {
        "total_current_budget": float,           # ASIN 当前预算合计
        "total_target_budget":  float | None,    # 目标预算 (来自 strategy_context, 已三级兜底)
        "target_budget_source": str,             # "override" | "asin_data" | "fallback_spend_x1.15" | ""
        "portfolios": [
          {
            "name": str,
            "campaign_count": int,
            "current_budget_sum": float,
            "target_share_pct": int,             # 占比 (常数)
            "target_amount": float | None,       # 预算约束 = total_target × share_pct (常数)
            "campaign_keys": [str, ...],
          }, ...
        ],
        "alerts": [str, ...],                    # 后端层告警 (目前仅"无目标预算"提示)
      }
    """
    unit_by_key = {cu.campaign_key: cu for cu in all_units}

    # 1. 初始化 4 组合容器
    target_share = _default_share_pcts()
    portfolios: dict[str, dict] = {
        name: {
            "name": name,
            "campaign_count": 0,
            "current_budget_sum": 0.0,
            "target_share_pct": target_share[name],
            "campaign_keys": [],
        }
        for name in ALL_PORTFOLIOS
    }

    # 2. 按 adjustments 累加 current_budget + 收集 campaign_keys
    for item in adjustments:
        portfolio = item.ai_portfolio_class or ""
        if portfolio not in portfolios:
            # 兜底: 未分类的归广泛/自动 (与 classifier 兜底一致),不丢数据
            portfolio = "广泛/自动"

        cu = unit_by_key.get(item.campaign_key)
        current = float(cu.current_budget) if cu else 0.0

        p = portfolios[portfolio]
        p["campaign_count"] += 1
        p["current_budget_sum"] += current
        p["campaign_keys"].append(item.campaign_key)

    # 3. 总数
    total_current = sum(p["current_budget_sum"] for p in portfolios.values())

    # 4. 目标预算: 来自 strategy_context (已三级兜底)
    total_target: float | None = ctx.daily_budget
    target_source: str = ctx.daily_budget_source or ""

    alerts: list[str] = []

    # 5. 逐组合: 计算约束分配 target_amount + 四舍五入
    for p in portfolios.values():
        if total_target is not None and total_target > 0:
            p["target_amount"] = round(total_target * p["target_share_pct"] / 100.0, 2)
        else:
            p["target_amount"] = None
        p["current_budget_sum"] = round(p["current_budget_sum"], 2)

    if total_target is None or total_target <= 0:
        alerts.append("无法读取目标预算,已禁用占比/约束相关告警 (前端动态判断同步禁用)")

    # 6. 守恒检查 (容差 $0.02)
    # 用显式 if 而非 assert: python -O 模式会编译期删 assert,守恒防护就丢了。
    expected_current = round(
        sum(
            float(unit_by_key[item.campaign_key].current_budget)
            for item in adjustments
            if item.campaign_key in unit_by_key
        ),
        2,
    )
    if abs(total_current - expected_current) >= 0.02:
        logger.error(
            "预算汇总守恒失败: total_current=%s expected=%s (差 %.4f)",
            total_current, expected_current, total_current - expected_current,
        )
        alerts.append(
            f"⚠ 预算守恒检查失败 (total={total_current:.2f} expected={expected_current:.2f}),"
            f" 数据可能不一致,请检查日志"
        )

    return {
        "total_current_budget": round(total_current, 2),
        "total_target_budget": round(total_target, 2) if total_target is not None else None,
        "target_budget_source": target_source,
        "portfolios": [portfolios[name] for name in ALL_PORTFOLIOS],
        "alerts": alerts,
    }
