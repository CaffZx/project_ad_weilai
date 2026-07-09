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


def _f(v, default: float = 0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def build_summary(
    adjustments: list[CampaignAdjustmentItem],
    all_units: list[CampaignUnit],
    ctx: CampaignStrategyContext,
    *,
    portfolio_data: dict[str, dict] | None = None,
) -> dict:
    """返回总预算约束 + 3 组分配约束。

    组合预算来源优先级：MCP portfolio 真实值 > 父目标×60/20/20 兜底。
    """
    target_budget: float | None = ctx.daily_budget
    source: str = ctx.daily_budget_source or ""

    if target_budget is None or target_budget <= 0:
        logger.info(
            "Campaign budget_summary [%s]: 目标预算不可用 (daily_budget=None)",
            ctx.parent_asin,
        )
        return {
            "target_budget": None,
            "target_budget_source": source,
            "portfolio_constraints": None,
        }

    pf = portfolio_data or {}
    pf_ok = len(pf) > 0   # MCP 成功（即使各组 budget=0），区别于整体失败

    def _budget_for(group: str) -> float:
        """MCP 成功 → 真实值（含 0）；MCP 失败 → 0 供下游回退判定。"""
        p = pf.get(group)
        if p is not None and pf_ok:
            return round(_f(p.get("budget", 0)), 2)
        return 0.0

    main_amount = _budget_for(PORTFOLIO_MAIN)
    test_amount = _budget_for(PORTFOLIO_TEST)
    broad_amount = _budget_for(PORTFOLIO_BROAD)

    # MCP 成功 → 用真实值（含 0）；仅在 MCP 整体失败 + 全为 0 时才回退 60/20/20
    if not pf_ok and main_amount <= 0 and test_amount <= 0 and broad_amount <= 0:
        share_main = settings.campaign_portfolio_share_main
        share_test = settings.campaign_portfolio_share_test
        share_broad = settings.campaign_portfolio_share_broad
        share_sum = share_main + share_test + share_broad or 100
        main_amount = round(target_budget * share_main / share_sum, 2)
        test_amount = round(target_budget * share_test / share_sum, 2)
        broad_amount = round(target_budget - main_amount - test_amount, 2)
        source = f"{source}+fallback_60_20_20" if source else "fallback_60_20_20"
    else:
        source = f"{source}+portfolio" if source else "portfolio"

    return {
        "target_budget": round(target_budget, 2),
        "target_budget_source": source,
        "portfolio_constraints": {
            PORTFOLIO_MAIN: main_amount,
            PORTFOLIO_TEST: test_amount,
            PORTFOLIO_BROAD: broad_amount,
        },
    }
