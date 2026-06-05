"""Campaign 预算汇总 — 极简版 (2026-06-02)。

⚠️ 本文件产出以下字段:
    - target_budget: 总预算约束 (= 策略上下文 daily_budget,已三级兜底)
    - target_budget_source: 兜底来源 (前端用于显示"按花费兜底"等提示)
    - portfolio_constraints: 3 组分配约束 {主推, 测试/新增, 广泛/自动} (淘汰组不在内)
        总约束 = 主推 + 测试 + 广泛 (3 组之和 == target_budget,守恒)
        占比来自 settings (60 / 20 / 20)
        淘汰组完全不参与约束概念 (KB 21 §6 每活动 $1,与运营策略预算无关)

其他全部由前端动态算:
  - "当前勾选活动预算汇总" = 运营勾选活动 proposed_budget 之和 (随勾选变化)
  - 4 组合表格 / 进度条 / 告警: 视前端需求实时算

依据:
  - daily_budget 来自 strategy_context.daily_budget
    (已三级兜底,见 build_campaign_strategy_context: override→asin_data→spend/days×1.15)
  - 占比 60/20/20: settings.campaign_portfolio_share_* (运营给 (阶段×目的) 表后迁 TOML)
"""

from __future__ import annotations

import logging

from app.config.settings import settings
from app.models.campaign import CampaignAdjustmentItem, CampaignStrategyContext, CampaignUnit
from app.workflow.steps.campaign_portfolio import (
    PORTFOLIO_BROAD,
    PORTFOLIO_MAIN,
    PORTFOLIO_TEST,
)

logger = logging.getLogger(__name__)


def build_summary(
    adjustments: list[CampaignAdjustmentItem],
    all_units: list[CampaignUnit],
    ctx: CampaignStrategyContext,
) -> dict:
    """返回总预算约束 + 3 组分配约束。

    Returns:
      {
        "target_budget": float | None,           # 总约束 (策略上下文 daily_budget)
        "target_budget_source": str,             # "override" | "asin_data" | "fallback_spend_x1.15" | ""
        "portfolio_constraints": {               # 3 组分配 (淘汰不在内);target=None 时整体为 None
            "精准主力组": float,
            "精准测试组": float,
            "自动广泛组": float,
        } | None,
      }
    """
    target_budget: float | None = ctx.daily_budget
    source: str = ctx.daily_budget_source or ""

    if target_budget is None or target_budget <= 0:
        logger.info(
            "Campaign budget_summary [%s]: 目标预算不可用 (策略上下文 daily_budget=None)",
            ctx.parent_asin,
        )
        return {
            "target_budget": None,
            "target_budget_source": source,
            "portfolio_constraints": None,
        }

    # 3 组占比 (settings,默认 60/20/20)
    share_main = settings.campaign_portfolio_share_main
    share_test = settings.campaign_portfolio_share_test
    share_broad = settings.campaign_portfolio_share_broad
    share_sum = share_main + share_test + share_broad

    if share_sum != 100:
        # 配置错误兜底: 按比例归一化,避免悄默不上百 → 3 组合计 ≠ target
        logger.error(
            "Portfolio share 配置异常: main+test+broad=%d ≠ 100,已按比例归一化",
            share_sum,
        )

    # 用 share/share_sum 而非 share/100,即使 share_sum ≠ 100 也守恒
    main_amount = round(target_budget * share_main / share_sum, 2)
    test_amount = round(target_budget * share_test / share_sum, 2)
    # 广泛 = target - main - test, 保证 3 组之和 == target (吃掉舍入误差)
    broad_amount = round(target_budget - main_amount - test_amount, 2)

    return {
        "target_budget": round(target_budget, 2),
        "target_budget_source": source,
        "portfolio_constraints": {
            PORTFOLIO_MAIN: main_amount,
            PORTFOLIO_TEST: test_amount,
            PORTFOLIO_BROAD: broad_amount,
        },
    }
