"""Campaign 预算回算 — LLM agent 中间件的代码侧（聚合 / 校验 / 映射）。

职责切分（KB23）：
  - 算术归代码：本模块 aggregate() 按 KB §5 把活动层 delta / 释放 / 可分配预算
    全部算好，喂给 LLM（防 LLM 做加减算错）。
  - 判断归 LLM：reasoner.recommend_budget_reallocation 只判分配方式(§7)、组内
    优先级排序(§3.1A) + 产出 3 组推荐值 + 父净增 + 解释。
  - 校验 + 兜底：validate() 按 KB §10 护栏 + 守恒判定；不过/异常由调用方回落
    campaign_budget_summary.build_summary（规则引擎兜底）。

输出契约 to_budget_summary() 与 build_summary 同形（target_budget + portfolio_constraints），
前端零改；附富字段（portfolio_budget_summary / budget_groups / source）。

字段全部透传，零新增 MCP 查询：
  - 活动 current/proposed/action/ai_portfolio_class/match_type/natural_rank/rank_change
    ← CampaignAdjustmentItem
  - acos ← all_units[campaign_key].perf_7d.acos（已查的 perf）
  - search_volume ← search_volume_map（新增活动线已查的 flow_keywords 透传，best-effort）
"""

from __future__ import annotations

import logging

from app.config.settings import settings
from app.models.campaign import (
    CampaignAdjustmentItem,
    CampaignStrategyContext,
    CampaignUnit,
)
from app.workflow.steps.campaign_portfolio import (
    PORTFOLIO_BROAD,
    PORTFOLIO_ELIMINATE,
    PORTFOLIO_MAIN,
    PORTFOLIO_TEST,
)

logger = logging.getLogger(__name__)

# 参与回算/二次分配的 3 个活动组合（低价捡漏组固定 $1，不参与，KB §5/§8.4）
_ACTIVE_GROUPS = (PORTFOLIO_MAIN, PORTFOLIO_TEST, PORTFOLIO_BROAD)
_LOW_BID_FIXED = 1.0
_ELIMINATE_ACTION = "eliminate_to_low_bid_pool"
_TOL = 0.5  # 美元舍入容差（后台预算粒度）

# 加权触发：产品定位 P0/P1（KB §7.2）。product_level 为中文 label。
_PRIORITY_LEVELS = ("P0", "P1")
_RANKING_PURPOSE = "排名型"
_PUSH_DIRECTION = "推进自然位"


def _f(v, default: float = 0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _group_of(item: CampaignAdjustmentItem) -> str:
    """活动归组：已分类用 ai_portfolio_class；空则按 KB23 §3.1 用 item 字段兜底分类。"""
    g = (item.ai_portfolio_class or "").strip()
    if g in (PORTFOLIO_MAIN, PORTFOLIO_TEST, PORTFOLIO_BROAD, PORTFOLIO_ELIMINATE):
        return g
    if (item.action or "") == _ELIMINATE_ACTION:
        return PORTFOLIO_ELIMINATE
    mt = (item.match_type or "").upper()
    if mt in ("BROAD", "PHRASE", "AUTO"):
        return PORTFOLIO_BROAD
    if mt == "EXACT":
        return PORTFOLIO_TEST if _f(item.current_budget) < 5.0 else PORTFOLIO_MAIN
    return PORTFOLIO_BROAD


def aggregate(
    adjustments: list[CampaignAdjustmentItem],
    all_units: list[CampaignUnit],
    ctx: CampaignStrategyContext,
    *,
    search_volume_map: dict[str, int] | None = None,
) -> dict:
    """按 KB23 §5 预聚合出 LLM 输入包（确定性算术）。"""
    sv_map = search_volume_map or {}
    unit_by_key = {cu.campaign_key: cu for cu in all_units}

    # 按组归集（仅 3 个活动组合）+ 单独算低价捡漏释放
    groups: dict[str, dict] = {
        g: {"group": g, "current_group_budget": 0.0, "group_requested_delta": 0.0, "campaigns": []}
        for g in _ACTIVE_GROUPS
    }
    low_bid_release = 0.0
    low_bid_moved = 0

    for item in adjustments:
        cur = _f(item.current_budget)
        action = item.action or ""
        grp = _group_of(item)

        # 本轮转入低价捡漏：活动层释放 (current - 1)，不计组层 delta（KB §5）
        if action == _ELIMINATE_ACTION or grp == PORTFOLIO_ELIMINATE:
            if action == _ELIMINATE_ACTION:
                low_bid_release += max(0.0, cur - _LOW_BID_FIXED)
                low_bid_moved += 1
            continue  # 已在池中的 $1 活动 delta≈0，跳过

        proposed = _f(item.proposed_budget, cur)  # 无 proposed 视为不变
        delta = proposed - cur
        gd = groups[grp]
        gd["current_group_budget"] += cur
        gd["group_requested_delta"] += delta

        unit = unit_by_key.get(item.campaign_key)
        gd["campaigns"].append({
            "campaign_key": item.campaign_key,
            "keyword_text": item.keyword_text,
            "current_budget": round(cur, 2),
            "proposed_budget": round(proposed, 2),
            "delta": round(delta, 2),
            "natural_rank": item.natural_rank,
            "rank_change": item.rank_change,
            "acos": unit.perf_7d.acos if unit else None,
            "search_volume": sv_map.get((item.keyword_text or "").strip().lower()),
        })

    for g in groups.values():
        g["current_group_budget"] = round(g["current_group_budget"], 2)
        g["group_requested_delta"] = round(g["group_requested_delta"], 2)

    # 父级回算（KB §5 / §6.2）
    requested_increase = round(sum(max(0.0, g["group_requested_delta"]) for g in groups.values()), 2)
    other_decrease = round(sum(abs(min(0.0, g["group_requested_delta"])) for g in groups.values()), 2)
    released = round(low_bid_release + other_decrease, 2)
    parent_allowed = _f(settings.campaign_parent_allowed_net_increase)
    net_required = round(requested_increase - released, 2)
    available = round(released + parent_allowed, 2)
    current_total = round(sum(g["current_group_budget"] for g in groups.values()) + _LOW_BID_FIXED, 2)

    purposes = ctx.ad_purposes or []
    directions = ctx.ad_directions or []
    has_ranking_push = (_RANKING_PURPOSE in purposes) or (_PUSH_DIRECTION in directions)

    return {
        "parent": {
            "parent_asin": ctx.parent_asin,
            "parent_target_daily_budget": _f(ctx.daily_budget) if ctx.daily_budget is not None else None,
            "target_budget_source": ctx.daily_budget_source or "",
            "parent_allowed_net_increase": parent_allowed,
            "current_total_group_budget": current_total,
            "low_bid_retention_release": round(low_bid_release, 2),
            "other_campaign_budget_decrease": other_decrease,
            "released_budget": released,
            "requested_increase_budget": requested_increase,
            "net_required_increase": net_required,
            "available_reallocation_budget": available,
            "priority_context": {
                "product_level": ctx.product_level,
                "season_stage": ctx.season_stage,
                "ad_purposes": purposes,
                "ad_directions": directions,
                "is_p0_p1": any(lv in (ctx.product_level or "") for lv in _PRIORITY_LEVELS),
                "has_ranking_push": has_ranking_push,
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
    """KB §10 护栏 + 守恒。返回 (ok, reason)。不过则调用方回落规则引擎。"""
    if not isinstance(agent_out, dict):
        return False, "agent 输出非 dict"
    bg = agent_out.get("budget_groups")
    if not isinstance(bg, list) or not bg:
        return False, "缺 budget_groups"

    by_name = {}
    for g in bg:
        if not isinstance(g, dict):
            return False, "budget_groups 含非 dict"
        name = g.get("group")
        # GROUP-004/004A：低价捡漏组不得参与 LLM 分配
        if name == PORTFOLIO_ELIMINATE:
            return False, "低价捡漏组不得出现在 agent 分配中 (GROUP-004)"
        by_name[name] = g

    # 三个活动组合都要在
    req_by_name = {g["group"]: g for g in agg.get("groups", [])}
    available = _f(agg.get("parent", {}).get("available_reallocation_budget"))
    total_actual = 0.0
    for name in _ACTIVE_GROUPS:
        g = by_name.get(name)
        if g is None:
            return False, f"缺组合 {name}"
        actual = g.get("actual_delta")
        proposed = g.get("proposed_group_budget")
        if actual is None or proposed is None:
            return False, f"{name} 缺 actual_delta/proposed_group_budget"
        actual_f = _f(actual)
        requested_f = _f(req_by_name.get(name, {}).get("group_requested_delta"))
        # 不超各组请求增量（KB §7：分配不得超 requested_delta）
        if actual_f > max(requested_f, 0.0) + _TOL:
            return False, f"{name} actual_delta {actual_f} 超 requested {requested_f} (GROUP-006/007)"
        if actual_f > 0:
            total_actual += actual_f

    # 守恒：正向分配之和不得超可分配预算（KB §7 / GROUP-007）
    if total_actual > available + _TOL:
        return False, f"正向分配合计 {round(total_actual,2)} 超可分配 {available} (GROUP-007)"
    return True, ""


def to_budget_summary(agent_out: dict, agg: dict, *, source: str = "agent") -> dict:
    """映射成与 build_summary 同形的契约 + 富字段。前端 portfolio_constraints 零改。"""
    parent = agg.get("parent", {})
    by_name = {g.get("group"): g for g in agent_out.get("budget_groups", []) if isinstance(g, dict)}

    def _proposed(name: str):
        g = by_name.get(name)
        return round(_f(g.get("proposed_group_budget")), 2) if g else None

    return {
        "target_budget": parent.get("parent_target_daily_budget"),
        "target_budget_source": parent.get("target_budget_source", ""),
        "portfolio_constraints": {
            PORTFOLIO_MAIN: _proposed(PORTFOLIO_MAIN),
            PORTFOLIO_TEST: _proposed(PORTFOLIO_TEST),
            PORTFOLIO_BROAD: _proposed(PORTFOLIO_BROAD),
        },
        # 富字段（向后兼容，前端可选用）
        "portfolio_budget_summary": {
            **{k: parent.get(k) for k in (
                "parent_target_daily_budget", "current_total_group_budget",
                "released_budget", "requested_increase_budget",
                "net_required_increase", "available_reallocation_budget",
            )},
            "proposed_total_group_budget": agent_out.get("parent", {}).get("proposed_total_group_budget"),
            "allocation_method": agent_out.get("allocation_method"),
        },
        "budget_groups": agent_out.get("budget_groups", []),
        "agent_explanation": (agent_out.get("parent", {}) or {}).get("explanation", ""),
        "source": source,
    }
