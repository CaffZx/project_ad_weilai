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
        "natural_rank": getattr(adj, "natural_rank", None),
        "near_natural_rank": getattr(adj, "near_natural_rank", None),
        "rank_change": getattr(adj, "rank_change", None),
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
        # 多词预过滤卡词数（数据源 campaign_prefilter.py 已产出，前端灰卡展示用）
        "keyword_count": s.get("keyword_count"),
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


# ── P2: DB 快照 → ViewModel（21 表反向 mapper，快照只读轨） ──────────────────

_GROUP_CODE_TO_LABEL = {
    "exact_core_group": "精准主力组",
    "exact_testing_group": "精准测试组",
    "auto_broad_group": "自动广泛组",
    "low_bid_retention_group": "低价捡漏组",
}
_PLACEMENT_CODE_TO_ZH = {
    "TOP_OF_SEARCH": "头部",
    "REST_OF_SEARCH": "其他",
    "PRODUCT_PAGE": "商品",
}


def _f(v):
    """Decimal/None → float/None（JSON 安全）。"""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _evidence_list(text) -> list:
    if not text:
        return []
    if isinstance(text, list):
        return [str(x) for x in text if x]
    return [ln.strip() for ln in str(text).split("\n") if ln.strip()]


def _snapshot_action(card: dict, has_bid: bool, has_budget: bool, has_placement: bool) -> str:
    """card.suggest_category(+group_type 兜底) → 前端 action 串。"""
    cat = (card.get("suggest_category") or "").upper()
    grp = card.get("campaign_group_type") or ""
    if cat == "ELIMINATE":
        return "eliminate_to_low_bid_pool"
    if cat == "CREATE":
        return "create_campaign"
    if cat == "KEEP":
        return "keep"
    # ADJUST 或空：旧数据淘汰藏在 group_type=low_bid_retention_group
    if grp == "low_bid_retention_group":
        return "eliminate_to_low_bid_pool"
    if has_placement and not has_bid and not has_budget:
        return "adjust_placement"
    if has_budget and not has_bid:
        return "adjust_budget"
    return "adjust_bid"


def _index_pending(rows: list, key: str = "suggest_card_id") -> dict:
    out: dict = {}
    for r in rows or []:
        out.setdefault(r.get(key), []).append(r)
    return out


def from_db_snapshot(snapshot: dict, *, mode: str = "readonly") -> dict:
    """21 表快照 raw dict → CampaignViewModel（与 to_viewmodel 同形）。

    未持久化字段（overview/synthesis/perf）按 null 降级——前端按字段有无渲染。
    item_id = card_id，供 /campaign/confirm 直接按 card 定位。
    """
    decision = snapshot.get("decision") or {}
    srow = snapshot.get("summary") or {}
    cards = snapshot.get("cards") or []

    camp_by_card = _index_pending(snapshot.get("campaign_pending"))
    kw_by_card = _index_pending(snapshot.get("keyword_pending"))
    plc_by_card = _index_pending(snapshot.get("placement_pending"))

    items = []
    create_count = 0
    prefiltered_count = 0
    for card in cards:
        card_id = card.get("id") or ""
        camp_rows = camp_by_card.get(card_id, [])
        kw_rows = kw_by_card.get(card_id, [])
        plc_rows = plc_by_card.get(card_id, [])

        bid_rows = [
            r for r in kw_rows
            if (r.get("match_type") or "").upper() != "NEGATIVE"
            and (r.get("old_bid") is not None or r.get("new_bid") is not None)
        ]
        neg_rows = [
            r for r in kw_rows
            if (r.get("match_type") or "").upper() == "NEGATIVE"
            or (r.get("new_state") or "").upper() in ("NEGATIVE", "PAUSED")
        ]

        current_budget = _f(camp_rows[0].get("old_budget")) if camp_rows else None
        proposed_budget = _f(camp_rows[0].get("new_budget")) if camp_rows else None
        current_bid = _f(bid_rows[0].get("old_bid")) if bid_rows else None
        proposed_bid = _f(bid_rows[0].get("new_bid")) if bid_rows else None

        placement_adjustments = [{
            "placement": _PLACEMENT_CODE_TO_ZH.get(r.get("placement_type"), r.get("placement_type")),
            "current_pct": _f(r.get("old_percent")),
            "proposed_pct": _f(r.get("new_percent")),
            "action": r.get("adjust_action") or "",
            "evidence": r.get("remark") or "",
        } for r in plc_rows]
        negative_keywords = [{"keyword": r.get("keyword_text") or ""} for r in neg_rows]

        cat = (card.get("suggest_category") or "").upper()
        is_pref = bool(card.get("is_prefiltered"))
        action = _snapshot_action(card, bool(bid_rows), bool(camp_rows), bool(plc_rows))
        if cat == "CREATE":
            item_type = "new"
            create_count += 1
        elif is_pref:
            item_type = "prefiltered"
            prefiltered_count += 1
            action = "prefiltered"
        else:
            item_type = "existing"

        grp_code = card.get("campaign_group_type") or ""
        items.append({
            "item_type": item_type,
            "item_id": card_id,
            "campaign_key": card_id,
            "campaign_name": card.get("campaign_name") or "",
            "child_asin": card.get("asin") or "",
            "keyword_text": card.get("keyword") or "",
            "match_type": card.get("keyword_match_type") or "",
            "keyword_class": card.get("keyword_class") or "",
            "action": action,
            "reason": card.get("description") or card.get("prefilter_reason") or "",
            "evidence": _evidence_list(card.get("evidence")),
            "confidence": card.get("confidence_level") or "",
            "current_budget": current_budget,
            "proposed_budget": proposed_budget,
            "current_bid": current_bid,
            "proposed_bid": proposed_bid,
            "placement_adjustments": placement_adjustments,
            "negative_keywords": negative_keywords,
            "ai_portfolio_class": _GROUP_CODE_TO_LABEL.get(grp_code, grp_code),
            "triggered_rule": card.get("trigger_rule") or "",
            "review_level": card.get("review_level") or "",
            "is_core": bool(card.get("is_core")),
            "action_klass": "skipped" if is_pref else _action_klass(action),
            "conf_klass": _conf_klass(card.get("confidence_level") or ""),
            "confirm_status": card.get("confirm_status") or "PENDING",
            "execute_status": card.get("execute_status") or "PENDING",
        })

    summary = {
        "total": srow.get("total_count") or len(cards),
        "eliminate": srow.get("eliminate_count") or 0,
        "adjust": srow.get("adjust_count") or 0,
        "keep": srow.get("keep_count") or 0,
        "create": create_count,
        "prefiltered": prefiltered_count,
        "lost": 0,
        "confidence_high": srow.get("confidence_high_count") or 0,
        "confidence_medium": srow.get("confidence_medium_count") or 0,
        "confidence_low": srow.get("confidence_low_count") or 0,
        "budget_impact": _f(srow.get("budget_impact")),
        "sanity_check_passed": bool(srow.get("validation_passed")) if srow else None,
    }

    budget_summary = None
    pc = {}
    if srow:
        if srow.get("main_push_budget") is not None:
            pc["精准主力组"] = _f(srow.get("main_push_budget"))
        if srow.get("broad_auto_budget") is not None:
            pc["自动广泛组"] = _f(srow.get("broad_auto_budget"))
        if srow.get("test_new_budget") is not None:
            pc["精准测试组"] = _f(srow.get("test_new_budget"))
    # 总预算约束 = 前置每日预算推荐（decision.daily_budget_suggest），不在 summary 冗余存
    target_budget = _f(decision.get("daily_budget_suggest"))
    if pc or target_budget is not None:
        budget_summary = {}
        if pc:
            budget_summary["portfolio_constraints"] = pc
        if target_budget is not None:
            budget_summary["target_budget"] = target_budget

    overview = None
    ov_text = srow.get("analysis_overview") if srow else None
    if ov_text:
        overview = {"facts": {}, "assessment_text": ov_text,
                    "direction_text": "", "generated_by": "snapshot"}

    # ── synthesis 组装（reason_group + member + special 三表）──
    #   member.suggest_card_id = card.id = 前端 item_id → campaign_keys 直接用 card_id，
    #   保证前端"全选本组/跳转成员"按 data-key(item_id) 命中。
    _CAT_TO_ACTION = {"ELIMINATE": "eliminate_to_low_bid_pool", "KEEP": "keep", "ADJUST": "adjust_bid"}
    _mem_by_group: dict = {}
    for m in snapshot.get("reason_members") or []:
        _mem_by_group.setdefault(m.get("group_id"), []).append(m)
    syn_groups = []
    for g in snapshot.get("reason_groups") or []:
        mems = _mem_by_group.get(g.get("id"), [])
        keys = [m.get("suggest_card_id") for m in mems if m.get("suggest_card_id")]
        syn_groups.append({
            "title": g.get("group_title") or "",
            "narrative": g.get("description") or "",
            "action": _CAT_TO_ACTION.get((g.get("suggest_category") or "").upper(), "adjust_bid"),
            "count": g.get("member_count") or len(keys),
            "campaign_keys": keys,
        })
    syn_specials = [{
        "campaign_key": s.get("display_text") or "",
        "why_special": s.get("recommendation") or "",
        "metrics": s.get("metrics_text") or "",
    } for s in snapshot.get("specials") or []]
    synthesis = {"groups": syn_groups, "special_cases": syn_specials} if (syn_groups or syn_specials) else None

    warnings = []
    if srow and srow.get("alert_msg"):
        warnings = [w for w in str(srow["alert_msg"]).split("; ") if w]

    dr = str(decision.get("day_range") or "DAY_7").split("_")[-1]
    days = int(dr) if dr.isdigit() else 7
    snap_time = decision.get("update_time") or decision.get("create_time")
    return {
        "mode": mode,
        "parent_asin": decision.get("parent_asin") or "",
        "days": days,
        "run_id": decision.get("id") or "",
        "snapshot_time": snap_time.isoformat() if hasattr(snap_time, "isoformat") else snap_time,
        "summary": summary,
        "overview": overview,
        "budget_summary": budget_summary,
        "synthesis": synthesis,
        "items": items,
        "warnings": warnings,
        "is_latest": bool(decision.get("is_latest")),
    }