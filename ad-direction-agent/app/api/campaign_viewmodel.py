"""Campaign ViewModel adapter — 后端主归一，将 CampaignAnalysisResult 转为前端统一 CampaignViewModel。

双轨入口：
  to_viewmodel(result, mode="interactive")   →  实时操作台
  to_viewmodel(result, mode="readonly")      →  快照只读

职责边界（唯一真相源）：
  - 字段名映射（NewCampaignItem.proposed_daily_budget → proposed_budget 等）
  - 派生字段计算（action_klass / conf_klass）
  - 来源差异消化（primary_placement → placement_adjustments + is_declaration）
  - mode 注入
  - 所有语义转换、类型转换

枚举翻译（库英文码 → 前端值）的权威表见 ERP 改造方案 §4。
快照轨 mapper 在此文件基础上增加 DB 字段 → 模型字段的转换逻辑。
"""

from __future__ import annotations

from app.models.campaign import CampaignAnalysisResult


def _action_klass(action: str) -> str:
    """action 字段 → CSS klass."""
    if action == "eliminate_to_low_bid_pool":
        return "eliminate"
    if action.startswith("adjust"):
        return "adjust"
    if action == "keep":
        return "keep"
    if action in ("create_campaign", "create"):
        return "create"
    if action == "prefiltered":
        return "skipped"
    return "skipped"


def _conf_klass(confidence: str) -> str:
    """confidence → CSS klass."""
    if confidence in ("high", "medium", "low"):
        return confidence
    return ""


def _item_from_adjustment(adj) -> dict:
    """CampaignAdjustmentItem → CampaignViewItem（existing 类型）."""
    return {
        "item_type": "existing",
        "item_id": adj.campaign_key or "",
        "campaign_key": adj.campaign_key or "",
        "campaign_name": adj.campaign_name or "",
        "child_asin": adj.child_asin or "",
        "keyword_text": adj.keyword_text or "",
        "match_type": adj.match_type or "",
        "keyword_class": getattr(adj, "keyword_class", "") or "",
        "action": adj.action or "",
        "reason": adj.reason or "",
        "evidence": adj.evidence or [],
        "confidence": adj.confidence or "",
        "current_budget": adj.current_budget,
        "proposed_budget": adj.proposed_budget,
        "current_bid": adj.current_bid,
        "proposed_bid": adj.proposed_bid,
        "placement_adjustments": adj.placement_adjustments or [],
        "negative_keywords": adj.negative_keywords or [],
        "ai_portfolio_class": getattr(adj, "ai_portfolio_class", "") or "",
        "triggered_rule": adj.triggered_rule or "",
        "review_level": adj.review_level or "",
        "is_core": getattr(adj, "is_core", False),
        "action_klass": _action_klass(adj.action or ""),
        "conf_klass": _conf_klass(adj.confidence or ""),
    }


def _item_from_new(n) -> dict:
    """NewCampaignItem → CampaignViewItem（new 类型）。

    字段差异在后端消化，不泄漏到前端：
      proposed_daily_budget → proposed_budget
      proposed_base_bid    → proposed_bid
      trigger_scene        → triggered_rule
      primary_placement    → placement_adjustments (is_declaration=True)
      negative_strategy    → negative_keywords (is_declaration=True)
      campaign_name        → item_id + campaign_key
    """
    placement_adjustments = []
    primary = getattr(n, "primary_placement", "") or ""
    if primary and primary != "N/A":
        placement_adjustments.append({
            "placement": primary,
            "is_declaration": True,
            "note": "首轮仅声明主投位，不加价",
        })

    negative_keywords = []
    neg_strat = getattr(n, "negative_strategy", "") or ""
    if neg_strat:
        negative_keywords.append({
            "keyword": neg_strat,
            "is_declaration": True,
        })

    return {
        "item_type": "new",
        "item_id": n.campaign_name or n.keyword_text or "",
        "campaign_key": n.campaign_name or n.keyword_text or "",
        "campaign_name": n.campaign_name or "",
        "child_asin": getattr(n, "child_asin", "") or "",
        "keyword_text": n.keyword_text or "",
        "match_type": n.match_type or "",
        "keyword_class": n.keyword_class or "",
        "action": "create_campaign",
        "reason": n.reason or "",
        "evidence": n.evidence or [],
        "confidence": n.confidence or "medium",
        "current_budget": 0.0,
        "proposed_budget": n.proposed_daily_budget,
        "current_bid": 0.0,
        "proposed_bid": n.proposed_base_bid,
        "placement_adjustments": placement_adjustments,
        "negative_keywords": negative_keywords,
        "ai_portfolio_class": n.ai_portfolio_class or "",
        "triggered_rule": n.trigger_scene or "",
        "review_level": n.review_level or "MANUAL_REVIEW",
        "is_core": False,
        "action_klass": "create",
        "conf_klass": _conf_klass(n.confidence or ""),
    }


def _item_from_skipped(s) -> dict:
    """skipped_campaigns 条目 → CampaignViewItem（prefiltered 或 lost 类型）."""
    is_prefiltered = s.get("__prefiltered", False)
    return {
        "item_type": "prefiltered" if is_prefiltered else "lost",
        "item_id": s.get("campaign_key", "") or s.get("campaign_name", "") or "",
        "campaign_key": s.get("campaign_key", "") or s.get("campaign_name", "") or "",
        "campaign_name": s.get("campaign_name", "") or s.get("campaign_key", "") or "",
        "child_asin": s.get("child_asin", "") or "",
        "keyword_text": s.get("keyword_text", "") or "",
        "match_type": s.get("match_type", "") or "",
        "keyword_class": s.get("keyword_class", "") or "",
        "action": "prefiltered" if is_prefiltered else "skipped",
        "reason": s.get("reason", "") or "",
        "evidence": [],
        "confidence": "",
        "current_budget": None,
        "proposed_budget": None,
        "current_bid": None,
        "proposed_bid": None,
        "placement_adjustments": [],
        "negative_keywords": [],
        "ai_portfolio_class": s.get("portfolio", "") or "",
        "triggered_rule": "",
        "review_level": "",
        "is_core": False,
        "action_klass": "skipped" if not is_prefiltered else "skipped",
        "conf_klass": "",
    }


def to_viewmodel(result: CampaignAnalysisResult, *, mode: str) -> dict:
    """CampaignAnalysisResult → CampaignViewModel dict."""
    adjustments = result.adjustments or []
    new_campaigns = result.new_campaigns or []
    skipped_campaigns = result.skipped_campaigns or []

    # 预过滤 / 丢失分类
    all_skipped = list(skipped_campaigns)
    prefiltered = [s for s in all_skipped if s.get("__prefiltered")]
    lost = [s for s in all_skipped if not s.get("__prefiltered")]

    # 统一 items 数组
    items = []
    items.extend(_item_from_adjustment(a) for a in adjustments)
    items.extend(_item_from_new(n) for n in new_campaigns)
    items.extend(_item_from_skipped(s) for s in all_skipped)

    # summary 统计
    s = result.summary or {}
    summary = {
        "total": result.total_campaigns or 0,
        "eliminate": s.get("to_eliminate", 0) if isinstance(s, dict) else 0,
        "adjust": s.get("to_adjust", 0) if isinstance(s, dict) else 0,
        "keep": s.get("to_keep", 0) if isinstance(s, dict) else 0,
        "create": len(new_campaigns),
        "prefiltered": len(prefiltered),
        "lost": len(lost),
        "confidence_high": s.get("confidence_high", 0) if isinstance(s, dict) else 0,
        "confidence_medium": s.get("confidence_medium", 0) if isinstance(s, dict) else 0,
        "confidence_low": s.get("confidence_low", 0) if isinstance(s, dict) else 0,
        "budget_impact": s.get("estimated_budget_impact") if isinstance(s, dict) else None,
        "sanity_check_passed": result.sanity_check_passed,
    }

    # overview（移除 posture_brief，不展示给运营）
    ov = result.strategic_overview
    overview = None
    if ov:
        overview = {
            "facts": ov.get("facts", {}) if isinstance(ov, dict) else getattr(ov, "facts", {}),
            "assessment_text": ov.get("assessment_text", "") if isinstance(ov, dict) else getattr(ov, "assessment_text", ""),
            "direction_text": ov.get("direction_text", "") if isinstance(ov, dict) else getattr(ov, "direction_text", ""),
            "generated_by": ov.get("generated_by", "ai") if isinstance(ov, dict) else getattr(ov, "generated_by", "ai"),
        }

    # budget_summary 透传
    bs = result.budget_summary

    # synthesis 透传
    sy = result.synthesis

    return {
        "mode": mode,
        "parent_asin": result.parent_asin or "",
        "days": result.days or 7,
        "run_id": result.run_id or "",
        "snapshot_time": None,
        "summary": summary,
        "overview": overview,
        "budget_summary": bs,
        "synthesis": sy,
        "items": items,
        "warnings": (result.warnings or []) + (result.new_campaigns_warnings or []),
    }