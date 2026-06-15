"""Campaign 组合(Portfolio)分类器 — AI 自造 4 类逻辑分类。

⚠️ 与亚马逊后台 Portfolio 实体无关 (那是 KB 18/21 portfolio_or_group 字段的原意,
   当前数据层未拉取,该字段留空)。本模块产出的是逻辑分类标签,只用于前端汇总展示
   与运营筛选,不写回 Amazon API。

⚠️ "测试/新增"组是【活动层面】的测试 — 即"新建活动还在跑数据的阶段",
   与 ASIN 级产品阶段 (KB 02 ProductStage 的"测试期") 完全无关,不读 product_stage。

判定优先级 (命中即止) - 2026-06-04 对齐 KB23 §3.1:
  1. 淘汰      — LLM action=eliminate_to_low_bid_pool OR 已在淘汰池 ($1/$0.20)
  2. 广泛/自动 — match_type ∈ {BROAD, PHRASE, AUTO}
  3. 测试/新增 — EXACT AND current_budget < $5 (KB23 精准测试组)
  4. 主推      — EXACT AND current_budget ≥ $5 (KB23 精准主力组)

为什么把广泛/自动提前到测试/新增之前?
  - 广泛/自动 是按 match_type 的硬归类 (业务上"测词广告"),
    一个 BROAD 活动即使小预算,本质仍是测词,不归精准测试组
  - 测试/新增 这层口径限定为 EXACT 流的小预算(<$5)活动;
    精准且预算达 $5 视为已进入主推承接 (KB23 §3.1)

数据来源:
  - current_budget : MCP basic_info → Doris 回落 (主推/测试分界 $5)
  - current_bid    : Doris 上下文 (MCP 不返回, 淘汰池判定用)
  - match_type     : Doris 上下文
  - llm_action     : 本批 LLM 输出 (合并后才有)
  注: days_online 不再参与分类 (改前用 14 天判"新建", 现按 KB23 §3.1 纯预算阈值 $5)

依据:
  - KB 21 §6   : 淘汰池固定 $1.00 / $0.20
  - KB 23 §3.1 : 精准主力组 = EXACT 且预算≥$5; 精准测试组 = EXACT 且预算<$5
"""

from __future__ import annotations

from app.models.campaign import CampaignUnit

# 组合标签常量 (中文 — 前端直接显示)；ERP 英文码映射见 erp_writer/text_utils._CAMPAIGN_GROUP_TYPE_MAP
# 2026-06-04 改名：主推→精准主力组 / 广泛自动→自动广泛组 / 测试新增→精准测试组 / 淘汰→低价捡漏组
PORTFOLIO_MAIN = "精准主力组"
PORTFOLIO_BROAD = "自动广泛组"
PORTFOLIO_TEST = "精准测试组"
PORTFOLIO_ELIMINATE = "低价捡漏组"

ALL_PORTFOLIOS = (PORTFOLIO_MAIN, PORTFOLIO_BROAD, PORTFOLIO_TEST, PORTFOLIO_ELIMINATE)

# 淘汰池识别容差
_ELIMINATION_BUDGET = 1.00
_ELIMINATION_BID = 0.20
_FLOAT_EPS = 0.01

# 精准主力 / 精准测试分界:活动预算 ≥ $5 入主推, < $5 入测试 (KB23 §3.1)
_MAIN_BUDGET_MIN = 5.0

# 广泛流匹配类型 (与 campaign.py 分流口径一致)
_BROAD_MATCH_TYPES = {"BROAD", "PHRASE", "AUTO"}


def _is_in_elimination_pool(unit: CampaignUnit) -> bool:
    """活动是否已经处于淘汰池(预算 $1 + Bid $0.20)。"""
    return (
        abs(unit.current_budget - _ELIMINATION_BUDGET) < _FLOAT_EPS
        and abs(unit.current_bid - _ELIMINATION_BID) < _FLOAT_EPS
    )


def _is_exact_testing(unit: CampaignUnit, effective_budget: float | None = None) -> bool:
    """精准测试组判定:活动预算 < $5 (KB23 §3.1)。

    仅对 EXACT 流调用 (调用方已过滤 match_type)。
    effective_budget 给定时按它判 (终态分类传 proposed,KB23 §3.1B/§3.5/§3.7 升降组按本轮建议预算);
    缺省 None → current (预分类阶段)。预算未知按 0 处理 → 归测试组 (尚未达 $5 主推门槛)。
    """
    budget = effective_budget if effective_budget is not None else unit.current_budget
    return (budget or 0) < _MAIN_BUDGET_MIN


def classify(
    unit: CampaignUnit,
    llm_action: str | None = None,
    effective_budget: float | None = None,
) -> str:
    """按优先级判定 4 组合归属,返回常量字符串。

    顺序 (命中即止): 淘汰 → 广泛/自动 → 测试/新增 → 主推

    Args:
        unit: 活动单元。current_budget/current_bid/match_type 是关键输入。
        llm_action: 本批 LLM 输出的 action (合并后才有);分析前阶段传 None,
                    淘汰组只能通过"已在淘汰池"判定。
        effective_budget: 主力↔测试 $5 分界的判定预算。终态分类传 proposed
                    (KB23 §3.1B/§3.5/§3.7 按本轮建议预算升降组);缺省 None → current。
                    仅作用于 EXACT 主力↔测试,淘汰/广泛分支不受影响。
    """
    # 1. 淘汰 (LLM 标记 OR 已在淘汰池 $1/$0.20) —— 读 current,不受 effective_budget 影响
    if llm_action == "eliminate_to_low_bid_pool" or _is_in_elimination_pool(unit):
        return PORTFOLIO_ELIMINATE
    mt = (unit.match_type or "").upper()
    # 2. 广泛 / 自动 (BROAD/PHRASE/AUTO) —— 业务上"测词广告",硬归类
    #    一个 BROAD 即使刚上线/小预算,本质仍是测词,不算"测试新活动"
    if mt in _BROAD_MATCH_TYPES:
        return PORTFOLIO_BROAD
    # 3. 精准流: 预算 ≥ $5 入主推, < $5 入测试 (KB23 §3.1;终态按 proposed)
    if mt == "EXACT":
        return PORTFOLIO_TEST if _is_exact_testing(unit, effective_budget) else PORTFOLIO_MAIN
    # 兜底: 未知 match_type → 归广泛 (与 campaign.py 既有口径一致)
    return PORTFOLIO_BROAD
