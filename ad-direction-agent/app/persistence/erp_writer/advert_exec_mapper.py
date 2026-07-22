"""广告调整执行映射器 —— CONFIRMED pending 行 ↔ 广告调整 MCP 入参 / record 行。

唯一映射点（要求3 维护友好）：所有 "Agent 决策 → MCP 工具" 的字段/枚举/命名映射只在此处。
纯逻辑、无 I/O，可单测。

输入：repository.load_confirmed_pending(decision_id) 返回的 dict：
  { decision:{...}, cards:[...], campaign_pending:[...], keyword_pending:[...], placement_pending:[...] }
输出：ExecPlan（三组 MCP payload + 逐项 ops 供写 *_record / 回写 pending）。

placement 三套命名：
  pending.placement_type      TOP_OF_SEARCH / REST_OF_SEARCH / PRODUCT_PAGE
  MCP async 字段              placementTopPercent / placementRestPercent / placementProductPagePercent
  MCP create predicate        TOP / REST / PRODUCT_PAGE
  record.placement_type       TOP_OF_SEARCH / REST_OF_SEARCH / PRODUCT_PAGE（与 pending 一致，直存）
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


# ── 枚举/命名归一 ──────────────────────────────────────────────────────────
_PLACEMENT_TO_ASYNC_FIELD = {
    "TOP_OF_SEARCH": "placementTopPercent",
    "REST_OF_SEARCH": "placementRestPercent",
    "PRODUCT_PAGE": "placementProductPagePercent",
}
_PLACEMENT_TO_CREATE_PREDICATE = {
    "TOP_OF_SEARCH": "TOP",
    "REST_OF_SEARCH": "REST",
    "PRODUCT_PAGE": "PRODUCT_PAGE",
}


def _norm_state(s: Any) -> str | None:
    """pending new_state → MCP campaignState/keywordState（enabled/paused），其余→None。"""
    if not s:
        return None
    v = str(s).strip().lower()
    if v in ("enabled", "paused"):
        return v
    if v in ("active",):
        return "enabled"
    return None


def _is_negative(row: dict) -> bool:
    st = str(row.get("new_state") or "").strip().upper()
    mt = str(row.get("match_type") or "").strip().upper()
    return st == "NEGATIVE" or mt.startswith("NEGATIVE")


def _neg_match_type(match_type: Any) -> str:
    """标准化否词匹配类型 → MCP 期望的 lowerCamelCase。
    DB 存储 NEGATIVE_EXACT / NEGATIVE_PHRASE，MCP 要求 negativeExact / negativePhrase。"""
    mt = str(match_type or "").strip().upper()
    if mt == "NEGATIVE_EXACT":
        return "negativeExact"
    if mt == "NEGATIVE_PHRASE":
        return "negativePhrase"
    return ""


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


@dataclass
class ExecPlan:
    params_vo_list: list[dict] = field(default_factory=list)   # agent_async_batch_update_advert
    create_calls: list[dict] = field(default_factory=list)     # agent_create_portfolio_campaign（逐 card）
    negative_calls: list[dict] = field(default_factory=list)   # agent_create_negative_keywords
    ops: list[dict] = field(default_factory=list)              # 逐项操作 → 写 *_record + 回写 pending
    warnings: list[str] = field(default_factory=list)
    move_errors: list[dict] = field(default_factory=list)      # 挪组失败详情 → 前端弹窗

    def is_empty(self) -> bool:
        return not (self.params_vo_list or self.create_calls or self.negative_calls)


def build_exec_plan(pending: dict, *, operator: str, child_asin: str | None = None) -> ExecPlan:
    """把一个 decision 的 CONFIRMED pending 行编译成 MCP 调用计划 + record ops。"""
    plan = ExecPlan()
    dec = pending.get("decision") or {}
    shop_id = int(dec.get("shop_id") or 0)
    parent_asin = str(dec.get("parent_asin") or "")
    parent_sku = str(dec.get("parent_seller_sku") or "")
    user = str(operator or "").strip()

    cards = {str(c.get("id")): c for c in (pending.get("cards") or [])}
    camp_rows = pending.get("campaign_pending") or []
    kw_rows = pending.get("keyword_pending") or []
    plc_rows = pending.get("placement_pending") or []

    create_card_ids = {
        cid for cid, c in cards.items()
        if str(c.get("suggest_category") or "").upper() == "CREATE"
    }

    # ── 1. 新建活动（CREATE）：逐 card → agent_create_portfolio_campaign ──
    for cid in create_card_ids:
        card = cards[cid]
        _build_create_call(plan, card, cid, camp_rows, kw_rows, plc_rows,
                           shop_id, parent_asin, parent_sku, user,
                           child_asin=child_asin)

    # ── 2. 否定词：keyword_pending 中 NEGATIVE 行 → agent_create_negative_keywords ──
    neg_by_campaign: dict[str, list[dict]] = defaultdict(list)
    for r in kw_rows:
        if str(r.get("suggest_card_id")) in create_card_ids:
            continue
        if _is_negative(r):
            neg_by_campaign[str(r.get("campaign_id") or "")].append(r)
    if neg_by_campaign:
        _build_negative_call(plan, neg_by_campaign, shop_id, parent_asin, parent_sku, user)

    # ── 3. 改已有活动：bid/budget/placement/state → agent_async_batch_update_advert ──
    # 按 campaign_id 聚合成 campaignVo；同一 parent 下汇成一个 paramsVo。
    camp_vo: dict[str, dict] = {}

    def _ensure_vo(campaign_id: str, campaign_name: str, card: dict | None) -> dict:
        vo = camp_vo.get(campaign_id)
        if vo is None:
            vo = {"campaignId": campaign_id}
            if card and card.get("campaign_group_type"):
                vo["campaignGroupType"] = card["campaign_group_type"]
            camp_vo[campaign_id] = vo
        return vo

    for r in camp_rows:
        cid = str(r.get("suggest_card_id"))
        if cid in create_card_ids:
            continue
        campaign_id = str(r.get("campaign_id") or "")
        if not campaign_id:
            plan.warnings.append(f"campaign_pending 缺 campaign_id（card={cid}），跳过")
            continue
        vo = _ensure_vo(campaign_id, str(r.get("campaign_name") or ""), cards.get(cid))
        budget = _num(r.get("new_budget"))
        state = _norm_state(r.get("new_state"))
        if budget is not None:
            vo["campaignBudget"] = budget
        if state:
            vo["campaignState"] = state
        plan.ops.append({
            "record_kind": "campaign", "suggest_card_id": cid, "campaign_id": campaign_id,
            "campaign_name": r.get("campaign_name"), "pending_id": r.get("id"),
            "old_budget": _num(r.get("old_budget")), "new_budget": budget,
            "old_state": r.get("old_state"), "new_state": r.get("new_state"),
        })

    for r in kw_rows:
        cid = str(r.get("suggest_card_id"))
        if cid in create_card_ids or _is_negative(r):
            continue
        campaign_id = str(r.get("campaign_id") or "")
        if not campaign_id:
            plan.warnings.append(f"keyword_pending 缺 campaign_id（card={cid}），跳过")
            continue
        vo = _ensure_vo(campaign_id, str(r.get("campaign_name") or ""), cards.get(cid))
        bid = _num(r.get("new_bid"))
        kstate = _norm_state(r.get("new_state"))
        kw_vo: dict[str, Any] = {"keyword": r.get("keyword_text")}
        if r.get("keyword_id"):
            kw_vo["keywordId"] = str(r.get("keyword_id"))
        if bid is not None:
            kw_vo["keywordBid"] = bid
        if kstate:
            kw_vo["keywordState"] = kstate
        vo.setdefault("keywordShowVoList", []).append(kw_vo)
        plan.ops.append({
            "record_kind": "keyword", "suggest_card_id": cid, "campaign_id": campaign_id,
            "keyword_id": r.get("keyword_id"), "keyword_text": r.get("keyword_text"),
            "pending_id": r.get("id"),
            "old_bid": _num(r.get("old_bid")), "new_bid": bid,
            "old_state": r.get("old_state"), "new_state": r.get("new_state"),
        })

    for r in plc_rows:
        cid = str(r.get("suggest_card_id"))
        if cid in create_card_ids:
            continue
        campaign_id = str(r.get("campaign_id") or "")
        ptype = str(r.get("placement_type") or "").upper()
        field_name = _PLACEMENT_TO_ASYNC_FIELD.get(ptype)
        new_pct = _num(r.get("new_percent"))
        if not campaign_id or not field_name or new_pct is None:
            continue
        vo = _ensure_vo(campaign_id, str(r.get("campaign_name") or ""), cards.get(cid))
        vo[field_name] = new_pct
        plan.ops.append({
            "record_kind": "placement", "suggest_card_id": cid, "campaign_id": campaign_id,
            "placement_type": ptype, "pending_id": r.get("id"),
            "old_percent": _num(r.get("old_percent")), "new_percent": new_pct,
        })

    if camp_vo:
        plan.params_vo_list.append({
            "shopId": shop_id,
            "parentAsin": parent_asin,
            "parentSellerSku": parent_sku,
            "currentUserId": user,
            "adjustReason": f"AI 决策批次 {dec.get('id') or ''} 执行",
            "decisionId": str(dec.get("id") or ""),
            "campaignVoList": list(camp_vo.values()),
        })
    return plan


def _build_create_call(plan, card, cid, camp_rows, kw_rows, plc_rows,
                       shop_id, parent_asin, parent_sku, user,
                       *, child_asin: str | None = None):
    budget_row = next((r for r in camp_rows if str(r.get("suggest_card_id")) == cid), None)
    kws = [r for r in kw_rows if str(r.get("suggest_card_id")) == cid]
    plcs = [r for r in plc_rows if str(r.get("suggest_card_id")) == cid]
    match_type = str(card.get("keyword_match_type") or "").upper()
    targeting = "AUTO" if match_type == "AUTO" else "MANUAL"
    create_vo: dict[str, Any] = {
        "campaignName": card.get("campaign_name"),
        "targetingType": targeting,
        "campaignBudget": _num(budget_row.get("new_budget")) if budget_row else None,
        "keywordTargeting": "yes" if kws else "no",
    }
    if kws:
        create_vo["keywordVoList"] = [{
            "keywordText": r.get("keyword_text"),
            "keywordMatchType": str(r.get("match_type") or "EXACT").upper(),
            "keywordBid": _num(r.get("new_bid")),
        } for r in kws]
    if plcs:
        create_vo["adjustmentVoList"] = [{
            "predicate": _PLACEMENT_TO_CREATE_PREDICATE.get(
                str(r.get("placement_type") or "").upper(), "TOP"),
            "percentage": _num(r.get("new_percent")) or 0,
        } for r in plcs if _num(r.get("new_percent")) is not None]
    create_call_args: dict[str, Any] = {
        "_card_id": cid,                       # 内部追踪（调用前剔除）
        "_group_type": card.get("campaign_group_type"),  # 内部：组别码，执行层解析 portfolioId 后剔除

        "shopId": shop_id,
        "parentAsin": parent_asin,
        "parentSellerSku": parent_sku,
        "currentUserId": user,
        "adjustReason": f"AI 新建活动：{card.get('campaign_name')}",
        "createCampaignVo": {k: v for k, v in create_vo.items() if v is not None},
    }
    # 子 ASIN：分析阶段 pick_target_child_asin 选定并落库到 card.asin，执行期直接读回。
    asin = (str(card.get("asin") or "")).strip() or (child_asin or "")
    if asin:
        create_call_args["asin"] = asin
    plan.create_calls.append(create_call_args)
    plan.ops.append({
        "record_kind": "campaign", "suggest_card_id": cid, "campaign_id": None,
        "campaign_name": card.get("campaign_name"), "is_create": True,
        "new_budget": _num(budget_row.get("new_budget")) if budget_row else None,
    })


def _build_negative_call(plan, neg_by_campaign, shop_id, parent_asin, parent_sku, user):
    campaign_vo_list = []
    for campaign_id, rows in neg_by_campaign.items():
        if not campaign_id:
            continue
        kw_vo_list = []
        for r in rows:
            mt = _neg_match_type(r.get("match_type"))
            if not mt:
                plan.warnings.append(
                    f"否词 match_type 无效（keyword={r.get('keyword_text')} "
                    f"match_type={r.get('match_type')}），跳过执行"
                )
                continue
            kw_vo_list.append({
                "matchType": mt,
                "keyword": r.get("keyword_text"),
            })
            plan.ops.append({
                "record_kind": "keyword", "suggest_card_id": str(r.get("suggest_card_id")),
                "campaign_id": campaign_id, "keyword_id": r.get("keyword_id"),
                "keyword_text": r.get("keyword_text"), "pending_id": r.get("id"),
                "is_negative": True, "new_state": "NEGATIVE",
            })
        if kw_vo_list:
            campaign_vo_list.append({
                "campaignId": campaign_id,
                "keywordVoList": kw_vo_list,
            })
    if campaign_vo_list:
        plan.negative_calls.append({
            "shopId": shop_id,
            "parentAsin": parent_asin,
            "parentSellerSku": parent_sku,
            "keywordType": "negative",
            "currentUserId": user,
            "campaignVoList": campaign_vo_list,
        })


def parse_result_envelope(res: Any) -> tuple[bool, str]:
    """MCP 返回信封 {state:'success'|'fail', errorMsg}。返回 (ok, msg)。

    实测信封：成功 state='success'；失败 state='fail' + errorMsg。
    异步提交工具返回含 taskId；逐项结果由 batch_update_result 给出（结构待真跑细化）。
    """
    if not isinstance(res, dict):
        return (True, "")
    # 实测三种信封：
    #   async_batch  {state, errorMsg}
    #   not-found    {errorMsg}                （无 state）
    #   create       {createCampaignState, createCampaignMsg}
    # 统一：任意以 state 结尾的键取状态；任意以 msg 结尾的键取消息。
    state_val = ""
    for k, v in res.items():
        lk = str(k).lower()
        if lk == "state" or lk.endswith("state"):
            state_val = str(v or "").lower()
            if state_val:
                break
    msg = ""
    for k in ("errorMsg", "msg", "createCampaignMsg"):
        if res.get(k):
            msg = str(res[k]); break
    if not msg:
        for k, v in res.items():
            if str(k).lower().endswith("msg") and v:
                msg = str(v); break
    if state_val:
        return (state_val == "success", msg)
    if msg:
        return (False, msg)
    if res.get("taskId") or res.get("taskIds"):
        return (True, "submitted")
    return (True, "")


def extract_task_ids(res: Any) -> list[str]:
    """从 async_batch_update 返回里抽 taskId（容错多种字段名）。"""
    if not isinstance(res, dict):
        return []
    for key in ("taskId", "taskIds", "task_id", "task_ids"):
        v = res.get(key)
        if isinstance(v, str) and v:
            return [v]
        if isinstance(v, list) and v:
            return [str(x) for x in v if x]
    # 嵌在 data 里
    data = res.get("data")
    if isinstance(data, dict):
        return extract_task_ids(data)
    return []
