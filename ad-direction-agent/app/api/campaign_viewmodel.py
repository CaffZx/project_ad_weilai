"""Campaign ViewModel adapter — 单一 mapper `from_db_snapshot`，21 表快照 → 前端统一 CampaignViewModel。

唯一真相源（方向 A 收敛后）：实时轨与快照轨**共用** `from_db_snapshot`——
  实时轨：分析 → 落库 → read_snapshot → from_db_snapshot(mode="interactive")  (api/campaign.py)
  快照轨：read_snapshot → from_db_snapshot(mode="readonly")                    (/campaign/snapshot)
原内存 mapper `to_viewmodel` + `_item_from_*` 已删除（消两 mapper 漂移债）。

职责边界：
  - DB 列 → 模型字段映射（suggest_category/group_type → action 等）
  - 派生字段计算（action_klass / conf_klass）
  - 枚举翻译（库英文码 → 前端值，权威表见 ERP 改造方案 §4）
  - Decimal/None → JSON 安全
  - mode 注入

注：sanity 失败的批次照常落库，summary.validation_passed → sanity_check_passed
透传给前端警示（不阻断落库，详见 erp_writer/auto_push.should_push_to_erp）。
"""

from __future__ import annotations


def _action_klass(action: str) -> str:
    """action 字段 → CSS klass."""
    if action == "eliminate_to_low_bid_pool":
        return "eliminate"
    if action.startswith("reactivate"):
        return "reactivate"
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


# ── DB 快照 → ViewModel（21 表反向 mapper，实时轨 + 快照轨共用） ──────────────────

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


def _snapshot_action(card: dict) -> str:
    """card.suggest_category(+group_type 兜底) → 前端 action 串（粗枚举原样渲染）。

    判定在写入侧 _normalize_action 做完、结果落 suggest_category；读回层只查表，
    不再按 pending 行有无猜具体子类型（旧实现会把"全维持但有 pending 行"误判成 adjust_bid）。
    ADJUST/空 → 粗类 "adjust"（前端显示"调整"）；具体改了什么看卡片的 old→new 行。
    """
    cat = (card.get("suggest_category") or "").upper()
    grp = card.get("campaign_group_type") or ""
    if cat == "ELIMINATE":
        return "eliminate_to_low_bid_pool"
    if cat == "CREATE":
        return "create_campaign"
    if cat == "KEEP":
        return "keep"
    # 旧数据：淘汰藏在 group_type=low_bid_retention_group
    if grp == "low_bid_retention_group":
        return "eliminate_to_low_bid_pool"
    return "adjust"


def _index_pending(rows: list, key: str = "suggest_card_id") -> dict:
    out: dict = {}
    for r in rows or []:
        out.setdefault(r.get(key), []).append(r)
    return out


def from_db_snapshot(snapshot: dict, *, mode: str = "readonly") -> dict:
    """21 表快照 raw dict → CampaignViewModel（实时轨 mode=interactive / 快照轨 mode=readonly 共用）。

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
        action = _snapshot_action(card)
        if cat == "CREATE":
            item_type = "new"
            create_count += 1
        elif is_pref:
            item_type = "prefiltered"
            prefiltered_count += 1
            action = "prefiltered"
        else:
            item_type = "existing"

        # 复评卡识别（KB21§7）：trigger_rule=REACTIVATE_* → action/klass 走 reactivate（前端深蓝「复评」）
        _trig = card.get("trigger_rule") or ""
        if not is_pref and _trig.upper().startswith("REACTIVATE"):
            action = _trig.lower()

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
    # 顶部总预算约束 = 各活跃组约束加总 + 低价捡漏组固定 $1（KB 21 §6；前端低价硬编码 $1，口径一致）。
    # 不再取 decision.daily_budget_suggest（那是回算【前】父级预算推荐，与组加总不同口径，会对不上）。
    target_budget = round(sum(pc.values()) + 1.0, 2) if pc else None
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
    _CAT_TO_ACTION = {
        "ELIMINATE": "eliminate_to_low_bid_pool",
        "REACTIVATE": "reactivate_budget_only",
        "KEEP": "keep", "ADJUST": "adjust_bid",
    }
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