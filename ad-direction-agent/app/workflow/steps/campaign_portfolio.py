"""Campaign 组合(Portfolio)分类器 — AI 自造 4 类逻辑分类。

⚠️ 与亚马逊后台 Portfolio 实体无关 (那是 KB 18/21 portfolio_or_group 字段的原意,
   当前数据层未拉取,该字段留空)。本模块产出的是逻辑分类标签,只用于前端汇总展示
   与运营筛选,不写回 Amazon API。

⚠️ "测试/新增"组是【活动层面】的测试 — 即"新建活动还在跑数据的阶段",
   与 ASIN 级产品阶段 (KB 02 ProductStage 的"测试期") 完全无关,不读 product_stage。

判定优先级 (命中即止) - 2026-06-02 更新顺序:
  1. 淘汰      — LLM action=eliminate_to_low_bid_pool OR 已在淘汰池 ($1/$0.20)
  2. 广泛/自动 — match_type ∈ {BROAD, PHRASE, AUTO}
  3. 测试/新增 — 0 ≤ days_online < 14 AND current_budget < $20 (仅对 EXACT 生效)
  4. 主推      — 其它 (默认 EXACT 入主推)

为什么把广泛/自动提前到测试/新增之前?
  - 广泛/自动 是按 match_type 的硬归类 (业务上"测词广告"),
    一个 BROAD 活动即使刚上线/小预算,本质仍是测词,不是"测试新活动"
  - 测试/新增 这层口径限定为 EXACT 流的新建活动 (因为运营测词都走广泛/自动 portfolio,
    新建的精准活动才是"测试/新增")

数据来源:
  - days_online    : MCP basic_info 拿"活动上线天数" (拿不到时 = -1,不算"新")
                     → MCP 能拿到,但不保证每次都有;Doris 回落不提供
  - current_budget : MCP basic_info → Doris 回落
  - current_bid    : Doris 上下文 (MCP 不返回)
  - match_type     : Doris 上下文
  - llm_action     : 本批 LLM 输出 (合并后才有)

依据:
  - KB 21 §6 / §line 99-100 : 淘汰池固定 $1.00 / $0.20
  - KB 21 §7                : ≥14 天淘汰组复评窗口 (借此作"测试新"上限)
  - 会议 2026-06-02         : 4 类逻辑分类,占比 50/30/15/5
"""

from __future__ import annotations

from app.models.campaign import CampaignUnit

# 组合标签常量 (中文 — 前端直接显示)
PORTFOLIO_MAIN = "主推"
PORTFOLIO_BROAD = "广泛/自动"
PORTFOLIO_TEST = "测试/新增"
PORTFOLIO_ELIMINATE = "淘汰"

ALL_PORTFOLIOS = (PORTFOLIO_MAIN, PORTFOLIO_BROAD, PORTFOLIO_TEST, PORTFOLIO_ELIMINATE)

# 淘汰池识别容差
_ELIMINATION_BUDGET = 1.00
_ELIMINATION_BID = 0.20
_FLOAT_EPS = 0.01

# 测试组阈值 (硬阈值,与 P 级 / 产品阶段无关)
_TEST_DAYS_MAX = 14
_TEST_BUDGET_MAX = 20.0

# 广泛流匹配类型 (与 campaign.py 分流口径一致)
_BROAD_MATCH_TYPES = {"BROAD", "PHRASE", "AUTO"}


def _is_in_elimination_pool(unit: CampaignUnit) -> bool:
    """活动是否已经处于淘汰池(预算 $1 + Bid $0.20)。"""
    return (
        abs(unit.current_budget - _ELIMINATION_BUDGET) < _FLOAT_EPS
        and abs(unit.current_bid - _ELIMINATION_BID) < _FLOAT_EPS
    )


def _is_new_test_campaign(unit: CampaignUnit) -> bool:
    """活动是否处于"新建测试"阶段 (活动层概念,与产品阶段无关)。

    days_online == -1 表示 MCP 拿不到上线天数 → 保守判定为非新。
    """
    if unit.days_online < 0:
        return False
    if unit.days_online >= _TEST_DAYS_MAX:
        return False
    return unit.current_budget < _TEST_BUDGET_MAX


def classify(unit: CampaignUnit, llm_action: str | None = None) -> str:
    """按优先级判定 4 组合归属,返回常量字符串。

    顺序 (命中即止): 淘汰 → 广泛/自动 → 测试/新增 → 主推

    Args:
        unit: 活动单元。current_budget/current_bid/days_online/match_type 是关键输入。
        llm_action: 本批 LLM 输出的 action (合并后才有);分析前阶段传 None,
                    淘汰组只能通过"已在淘汰池"判定。
    """
    # 1. 淘汰 (LLM 标记 OR 已在淘汰池 $1/$0.20)
    if llm_action == "eliminate_to_low_bid_pool" or _is_in_elimination_pool(unit):
        return PORTFOLIO_ELIMINATE
    mt = (unit.match_type or "").upper()
    # 2. 广泛 / 自动 (BROAD/PHRASE/AUTO) —— 业务上"测词广告",硬归类
    #    一个 BROAD 即使刚上线/小预算,本质仍是测词,不算"测试新活动"
    if mt in _BROAD_MATCH_TYPES:
        return PORTFOLIO_BROAD
    # 3. 测试 / 新增 (仅 EXACT 流,新建的精准活动才算 "测试新")
    if _is_new_test_campaign(unit):
        return PORTFOLIO_TEST
    # 4. 主推 (EXACT 且非新建)
    if mt == "EXACT":
        return PORTFOLIO_MAIN
    # 兜底: 未知 match_type → 归广泛 (与 campaign.py 既有口径一致)
    return PORTFOLIO_BROAD
