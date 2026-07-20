"""Campaign 预算回算 — LLM agent 中间件的代码侧（聚合 / 校验 / 映射）。

═══ 约束值 vs 统计值（KB23 §4.2 / §4.3，核心区分）═══
  - 约束值 = 组合层预算上限(current/proposed_group_budget)，低价捡漏组恒 $1。回算走这条线。
  - 统计值 = 组内活动日预算之和，可达约束值 1.5~2x(§4.3)，≠ 约束值。

═══ current_group_budget 的来源（数据现实 + 拍板）═══
  KB §5~§9 把 current_group_budget 当【已知输入】(真实组合/Portfolio 预算)，但本项目
  没有真实组合预算源（classify 仅逻辑分类、portfolio_or_group 留空）。
  拍板：查不到时**兜底 = 父目标 × 基础占比(60/20/20, settings)**。
  回算在「budget_pool = 父目标 + 允许净增」这个池上分配，proposed_total 守恒到池
  （KB §4.2 允许超父目标，但只受 parent_allowed_net_increase 闸控；=0 时 ≈ 父目标）。

═══ 职责切分 ═══
  - 算术归代码：aggregate() 算占比基线/需求 delta/释放/池（确定性）。
  - 判断归 LLM：recommend_budget_reallocation 在池内按 §7 调占比 + §3.1A 组内优先级。
  - 校验+兜底：validate() 守恒(Σproposed ≤ 池) + GROUP-004；不过由调用方回落 build_summary。

字段全部透传，零新增 MCP 查询（acos←perf_7d；search_volume←flow_keywords 透传）。
"""

from __future__ import annotations

import logging

from app.config.settings import settings
from app.models.campaign import (
    CampaignAdjustmentItem,
    CampaignStrategyContext,
    CampaignUnit,
    NewCampaignItem,
)
from app.workflow.steps.campaign_portfolio import (
    PORTFOLIO_BROAD,
    PORTFOLIO_ELIMINATE,
    PORTFOLIO_MAIN,
    PORTFOLIO_TEST,
)

logger = logging.getLogger(__name__)

# 参与瓜分父目标的 3 个活跃组合（低价捡漏组固定 $1，单列，不瓜分，KB §8.4）
_ACTIVE_GROUPS = (PORTFOLIO_MAIN, PORTFOLIO_TEST, PORTFOLIO_BROAD)
_LOW_BID_FIXED = 1.0
_ELIMINATE_ACTION = "eliminate_to_low_bid_pool"
_TOL = 0.5  # 美元舍入容差

# 加权触发：产品定位 P0/P1（KB §7.2），product_level 为中文 label
_PRIORITY_LEVELS = ("P0", "P1")
_RANKING_PURPOSE = "排名型"
_PUSH_DIRECTION = "推进自然位"


def _f(v, default: float = 0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _group_of(item: CampaignAdjustmentItem) -> str:
    """活动归组：已分类用 ai_portfolio_class；空则按 KB23 §3.1 用 item 字段兜底。"""
    g = (item.ai_portfolio_class or "").strip()
    if g in (PORTFOLIO_MAIN, PORTFOLIO_TEST, PORTFOLIO_BROAD, PORTFOLIO_ELIMINATE):
        return g
    if (item.action or "") == _ELIMINATE_ACTION:
        return PORTFOLIO_ELIMINATE
    mt = (item.match_type or "").upper()
    if mt in ("BROAD", "PHRASE", "AUTO"):
        return PORTFOLIO_BROAD
    if mt == "EXACT":
        # 主路径恒由终态分类(按 proposed)设好 ai_portfolio_class，此兜底罕见命中；
        # 为一致性也按 proposed（缺省回落 current）判 $5 分界。
        eff = item.proposed_budget if item.proposed_budget is not None else item.current_budget
        return PORTFOLIO_TEST if _f(eff) < 5.0 else PORTFOLIO_MAIN
    return PORTFOLIO_BROAD


def _group_of_new(nc: NewCampaignItem) -> str:
    """新增活动归组（永不淘汰）：优先 ai_portfolio_class（campaign_new 已设测试/广泛），
    缺失时按 match_type 兜底——新 EXACT 一律进精准测试组（KB23 §3.4：<$5、未验证，不套 $5 分界）。"""
    g = (nc.ai_portfolio_class or "").strip()
    if g in (PORTFOLIO_MAIN, PORTFOLIO_TEST, PORTFOLIO_BROAD):
        return g
    mt = (nc.match_type or "").upper()
    if mt in ("BROAD", "PHRASE", "AUTO"):
        return PORTFOLIO_BROAD
    return PORTFOLIO_TEST  # 新 EXACT（及未知）→ 测试组（兜底分支，ai_portfolio_class 通常已设）


def aggregate(
    adjustments: list[CampaignAdjustmentItem],
    all_units: list[CampaignUnit],
    ctx: CampaignStrategyContext,
    *,
    search_volume_map: dict[str, int] | None = None,
    new_campaigns: list[NewCampaignItem] | None = None,
    portfolio_data: dict[str, dict] | None = None,
) -> dict:
    """预聚合 LLM 输入包。

    组合预算/花费来源优先级：ad_portfolio_list MCP 真实值 > 父目标×60/20/20 兜底。
    daily_spend 为日均花费（MCP 已换算），与 current_group_budget（日预算）同口径，
    供 LLM 直接对比预算消耗率做回算决策。
    新增活动（KB23 §5.1）：current=0 的纯净增需求。
    """
    sv_map = search_volume_map or {}
    unit_by_key = {cu.campaign_key: cu for cu in all_units}
    parent_target = _f(ctx.daily_budget) if ctx.daily_budget is not None else None
    parent_allowed = _f(settings.campaign_parent_allowed_net_increase)

    # ── 组合预算：优先 MCP 真实 portfolio；MCP 全失败(fetch 返回 {})才走 60/20/20 兜底 ──
    pf = portfolio_data or {}
    pf_ok = len(pf) > 0   # fetch 成功（即使各组 budget=0），区别于整体 MCP 失败
    shares = {
        PORTFOLIO_MAIN: settings.campaign_portfolio_share_main,
        PORTFOLIO_TEST: settings.campaign_portfolio_share_test,
        PORTFOLIO_BROAD: settings.campaign_portfolio_share_broad,
    }
    share_sum = sum(shares.values()) or 100
    constraint_basis = "fallback_share_60_20_20"

    groups: dict[str, dict] = {}
    for g in _ACTIVE_GROUPS:
        if pf_ok:
            # MCP 成功：按真实值，缺组/0 就是 0（不虚构）；daily_spend 缺失保留 None
            current_group_budget = _f((pf.get(g) or {}).get("budget")) or 0.0
            daily_spend = (pf.get(g) or {}).get("daily_spend")
            if daily_spend is not None:
                daily_spend = _f(daily_spend)
            constraint_basis = "portfolio"
        else:
            # MCP 整体失败：父目标 × 占比兜底
            current_group_budget = (round(parent_target * shares[g] / share_sum, 2)
                                    if parent_target else 0.0)
            daily_spend = None
        groups[g] = {
            "group": g,
            "current_group_budget": round(current_group_budget, 2),
            "daily_spend": round(daily_spend, 2) if daily_spend is not None else None,
            "constraint_source": constraint_basis if current_group_budget > 0 else "none",
            "group_requested_delta": 0.0,
            "new_requested_delta": 0.0,
            "campaigns": [],
        }

    low_bid_release = 0.0
    low_bid_moved = 0

    for item in adjustments:
        cur = _f(item.current_budget)
        action = item.action or ""
        grp = _group_of(item)
        if action == _ELIMINATE_ACTION or grp == PORTFOLIO_ELIMINATE:
            if action == _ELIMINATE_ACTION:
                low_bid_release += max(0.0, cur - _LOW_BID_FIXED)
                low_bid_moved += 1
            continue
        proposed = _f(item.proposed_budget, cur)
        gd = groups[grp]
        gd["group_requested_delta"] += proposed - cur
        unit = unit_by_key.get(item.campaign_key)
        gd["campaigns"].append({
            "campaign_key": item.campaign_key,
            "keyword_text": item.keyword_text,
            "current_budget": round(cur, 2),
            "proposed_budget": round(proposed, 2),
            "natural_rank": item.natural_rank,
            "rank_change": item.rank_change,
            "acos": unit.perf_7d.acos if unit else None,
            "search_volume": sv_map.get((item.keyword_text or "").strip().lower()),
        })

    for nc in (new_campaigns or []):
        grp = _group_of_new(nc)
        if grp not in _ACTIVE_GROUPS:
            continue
        proposed = _f(nc.proposed_daily_budget)
        gd = groups[grp]
        gd["group_requested_delta"] += proposed
        gd["new_requested_delta"] += proposed
        gd["campaigns"].append({
            "campaign_key": nc.campaign_name or nc.keyword_text,
            "keyword_text": nc.keyword_text,
            "current_budget": 0.0,
            "proposed_budget": round(proposed, 2),
            "natural_rank": None,
            "rank_change": None,
            "acos": None,
            "search_volume": sv_map.get((nc.keyword_text or "").strip().lower()),
            "is_new": True,
        })

    for g in _ACTIVE_GROUPS:
        groups[g]["group_requested_delta"] = round(groups[g]["group_requested_delta"], 2)
        groups[g]["new_requested_delta"] = round(groups[g]["new_requested_delta"], 2)

    # ★ 3 活跃组全为零（不区分是否含低价捡漏）→ 跳过回算 LLM
    all_active_zero = all(groups[g]["current_group_budget"] <= 0 for g in _ACTIVE_GROUPS)

    pool = round((parent_target or 0.0) + parent_allowed, 2)
    # 本轮可用于覆盖正增长的最大额度（含淘汰释放）
    available_for_increase = round(low_bid_release + parent_allowed, 2)
    purposes = ctx.ad_purposes or []
    directions = ctx.ad_directions or []
    return {
        "parent": {
            "parent_asin": ctx.parent_asin,
            "parent_target_daily_budget": parent_target,
            "target_budget_source": ctx.daily_budget_source or "",
            "parent_allowed_net_increase": parent_allowed,
            "budget_pool": pool,
            "available_for_increase": available_for_increase,
            "low_bid_retention_release": round(low_bid_release, 2),
            "constraint_basis": constraint_basis,
            "all_active_zero": all_active_zero,
            "priority_context": {
                "product_level": ctx.product_level,
                "season_stage": ctx.season_stage,
                "ad_purposes": purposes,
                "ad_directions": directions,
                "is_p0_p1": any(lv in (ctx.product_level or "") for lv in _PRIORITY_LEVELS),
                "has_ranking_push": (_RANKING_PURPOSE in purposes) or (_PUSH_DIRECTION in directions),
            },
        },
        "groups": [groups[g] for g in _ACTIVE_GROUPS],
        "low_bid_group": {
            "group": PORTFOLIO_ELIMINATE,
            "fixed_group_budget": _LOW_BID_FIXED,
            "released_at_campaign_level": round(low_bid_release, 2),
            "moved_in_count": low_bid_moved,
        },
    }


def validate(agent_out: dict, agg: dict) -> tuple[bool, str]:
    """守恒(Σproposed ≤ budget_pool) + GROUP-004 + 增量约束。"""
    if not isinstance(agent_out, dict):
        return False, "agent 输出非 dict"
    bg = agent_out.get("budget_groups")
    if not isinstance(bg, list) or not bg:
        return False, "缺 budget_groups"

    parent = agg.get("parent", {})
    pool = _f(parent.get("budget_pool"))
    available = _f(parent.get("available_for_increase"))

    by_name: dict = {}
    for g in bg:
        if not isinstance(g, dict):
            return False, "budget_groups 含非 dict"
        if g.get("group") == PORTFOLIO_ELIMINATE:
            return False, "低价捡漏组不得出现在 agent 分配中 (GROUP-004)"
        by_name[g.get("group")] = g

    total = 0.0
    increase_used = 0.0
    for name in _ACTIVE_GROUPS:
        g = by_name.get(name)
        if g is None:
            return False, f"缺组合 {name}"
        pgb = g.get("proposed_group_budget")
        if pgb is None:
            return False, f"{name} 缺 proposed_group_budget"
        v = _f(pgb)
        if v < -_TOL:
            return False, f"{name} proposed_group_budget 为负"
        total += max(0.0, v)
        # 增量约束：正增长合计不得超过可用额度
        cur = _f(g.get("current_group_budget"))
        increase_used += max(0.0, v - cur)

    if pool > 0 and total > pool + _TOL:
        return False, f"3 组约束合计 {round(total, 2)} 超 budget_pool {pool}（父目标硬顶）"
    if available >= 0 and increase_used > available + _TOL:
        return False, f"正增长合计 {round(increase_used, 2)} 超 available_for_increase {available}"
    return True, ""


def to_budget_summary(
    agent_out: dict, agg: dict, *,
    portfolio_data: dict[str, dict] | None = None,
    source: str = "agent",
) -> dict:
    """映射成与 build_summary 同形契约。前端 portfolio_constraints 零改。"""
    parent = agg.get("parent", {})
    by_name = {g.get("group"): g for g in agent_out.get("budget_groups", []) if isinstance(g, dict)}

    def _proposed(name: str):
        g = by_name.get(name)
        return round(_f(g.get("proposed_group_budget")), 2) if g else None

    proposed_total = round(sum(_f(_proposed(g)) for g in _ACTIVE_GROUPS if _proposed(g) is not None), 2)
    # portfolio_current_budget：仅 MCP 成功时取真实值，绝不填入 60/20/20 推算值
    basis = parent.get("constraint_basis", "")
    agg_groups = {g.get("group"): g for g in agg.get("groups", []) if isinstance(g, dict)}
    current_budget = {}
    if basis == "portfolio":
        for g in _ACTIVE_GROUPS:
            gd = agg_groups.get(g) or {}
            current_budget[g] = gd.get("current_group_budget")
    else:
        for g in _ACTIVE_GROUPS:
            current_budget[g] = None
    pf = portfolio_data or {}
    pf_ok = len(pf) > 0
    def _safe_spend(v) -> float | None:
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    _ALL_PORTFOLIO_GROUPS = _ACTIVE_GROUPS + (PORTFOLIO_ELIMINATE,)
    def _spend_map(field: str) -> dict[str, float | None]:
        out: dict[str, float | None] = {}
        for g in _ALL_PORTFOLIO_GROUPS:
            if pf_ok:
                p = pf.get(g) or {}
                fv = _safe_spend(p.get(field))
                out[g] = round(fv, 2) if fv is not None else None
            else:
                out[g] = None
        return out
    return {
        "target_budget": parent.get("parent_target_daily_budget"),
        "target_budget_source": parent.get("target_budget_source", ""),
        "portfolio_constraints": {
            PORTFOLIO_MAIN: _proposed(PORTFOLIO_MAIN),
            PORTFOLIO_TEST: _proposed(PORTFOLIO_TEST),
            PORTFOLIO_BROAD: _proposed(PORTFOLIO_BROAD),
        },
        "portfolio_current_budget": current_budget,
        "portfolio_spend_1d": _spend_map("spend_1d"),
        "portfolio_spend_3d": _spend_map("spend_3d"),
        "portfolio_spend_7d": _spend_map("spend_7d"),
        "portfolio_acos_1d": _spend_map("acos_1d"),
        "portfolio_acos_3d": _spend_map("acos_3d"),
        "portfolio_acos_7d": _spend_map("acos_7d"),
        "portfolio_budget_summary": {
            "parent_target_daily_budget": parent.get("parent_target_daily_budget"),
            "parent_allowed_net_increase": parent.get("parent_allowed_net_increase"),
            "budget_pool": parent.get("budget_pool"),
            "available_for_increase": parent.get("available_for_increase"),
            "low_bid_retention_release": parent.get("low_bid_retention_release"),
            "constraint_basis": parent.get("constraint_basis"),
            "proposed_total_group_budget": proposed_total,
            "allocation_method": agent_out.get("allocation_method"),
        },
        "budget_groups": agent_out.get("budget_groups", []),
        "agent_explanation": (agent_out.get("parent", {}) or {}).get("explanation", ""),
        "source": source,
    }
