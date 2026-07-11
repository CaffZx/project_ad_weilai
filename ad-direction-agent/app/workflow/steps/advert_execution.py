"""广告调整真实执行编排（Part 6）。

submit_execution: load CONFIRMED pending → mapper → 插主记录 → 调 MCP(或 dry-run)
                  → 插子记录 + 回写 pending execute_status。

安全：advert_mcp_enabled 总开关；advert_exec_dry_run 默认空跑（不动真实广告）。
幂等：load_confirmed_pending 只取 execute_status=PENDING，已执行行不再取。
"""

from __future__ import annotations

import logging

from app.config.settings import settings
from app.data.advert_mcp_client import AdvertMcpClient
from app.persistence.erp_writer.advert_exec_mapper import (
    build_exec_plan,
    extract_task_ids,
    parse_result_envelope,
)
from app.persistence.erp_writer.repository import _get_repository
from app.persistence.erp_writer.text_utils import json_dumps, unmap_campaign_group_type
from app.workflow.steps.portfolio_execution import (
    _match_portfolio,
    _normalize_portfolio_list,
    _pf_field,
)

logger = logging.getLogger(__name__)


def _sync_pool_entries_from_exec(
    pending: dict, plan, *, mcp_async_ok: bool, repo,
) -> None:
    """执行钩子：根据本次 MCP 真跑结果写/删 t_advert_agent_pool_entry。

    触发条件：
      - 淘汰卡（suggest_category='ELIMINATE'）：async_batch_update 成功 → upsert_pool_entry
      - 复评卡（trigger_rule 以 'REACTIVATE_' 开头）：async_batch_update 成功 → mark_pool_exit

    只看 async_batch_update 的整批结果（改已有活动统一走过它）：
      新建/否词路径不涉及淘汰/复评，无需单独判。MCP 部分失败时 ops 已逐条带 execute_status，
      但池表语义是「活动已真被改成 $1/$0.20」或「真被改成 $3」——只要 async 这一批成功就写。

    fail-open：DB 异常仅记日志，不阻断 MCP 已生效的事实。
    """
    dec = pending.get("decision") or {}
    parent_asin = str(dec.get("parent_asin") or "")
    if not parent_asin:
        return
    if not mcp_async_ok:
        return  # MCP 没真跑成功，不写池表

    shop_id = int(dec.get("shop_id") or 0) or None
    shop_account = ""  # decision 行未含，由 sync_pool_entries 路径在分析时补；执行钩子无新源
    parent_sku = str(dec.get("parent_seller_sku") or "") or None
    decision_id = str(dec.get("id") or "") or None

    cards = {str(c.get("id")): c for c in (pending.get("cards") or [])}
    camp_pendings = pending.get("campaign_pending") or []

    # 卡 → campaign_id 映射（改已有活动时 campaign_pending 上的 campaign_id 才是真值）
    card_to_campaign: dict[str, str] = {}
    for r in camp_pendings:
        cid = str(r.get("suggest_card_id") or "")
        camp_id = str(r.get("campaign_id") or "")
        if cid and camp_id:
            card_to_campaign[cid] = camp_id

    for card_id, card in cards.items():
        category = str(card.get("suggest_category") or "").upper()
        trigger = str(card.get("trigger_rule") or "")
        campaign_id = card_to_campaign.get(card_id) or str(card.get("campaign_id") or "")
        if not campaign_id:
            continue

        if category == "ELIMINATE":
            try:
                repo.upsert_pool_entry(
                    parent_asin,
                    campaign_id=campaign_id,
                    child_asin=str(card.get("asin") or "") or None,
                    campaign_key=str(card.get("campaign_key") or "") or None,
                    campaign_name=str(card.get("campaign_name") or "") or "",
                    keyword_text=str(card.get("keyword") or "") or None,
                    decision_id=decision_id,
                    eliminate_spend_7d=_perf_json_cost(card.get("perf_json")),
                    shop_id=shop_id,
                    shop_account=shop_account or None,
                    parent_sku=parent_sku,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("upsert_pool_entry 失败 [%s/%s]: %s", parent_asin, campaign_id, e)

        if trigger.startswith("REACTIVATE_"):
            try:
                repo.mark_pool_exit(parent_asin, campaign_id)
            except Exception as e:  # noqa: BLE001
                logger.warning("mark_pool_exit 失败 [%s/%s]: %s", parent_asin, campaign_id, e)

    # 回写 pending 表 execute_status='SUCCESS' + execute_time=NOW()
    # （填补 submit_execution_direct "不写库"的历史缺口：MCP 真跑了但 DB 无记录）
    try:
        repo.update_pending_execute_status(plan.ops, "SUCCESS", operator="system", msg="direct execution via MCP")
    except Exception as e:  # noqa: BLE001
        logger.warning("update_pending_execute_status 失败 [%s]: %s", parent_asin, e)


def _perf_json_cost(perf_json) -> float | None:
    """复用同名私有函数的轻量副本，避免跨模块循环导入。"""
    if not perf_json:
        return None
    try:
        d = perf_json if isinstance(perf_json, dict) else __import__("json").loads(perf_json)
        v = d.get("cost")
        return float(v) if v is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


async def _resolve_create_portfolios(
    client, plan, *, shop_id: int, parent_asin: str, parent_sku: str, operator: str,
) -> set[str]:
    """为每张 CREATE 卡解析归属组合（模糊匹配现有 portfolio）。

    - 命中  → 注入 call["portfolioId"]，正常建活动到该组合下。
    - 未命中 / 组合列表查询失败（严格模式）→ 跳过该卡，不建活动、不新建组合。
    返回需跳过的 card_id 集合。
    """
    skip: set[str] = set()
    if not plan.create_calls:
        return skip
    try:
        res = await client.query_portfolio_list(
            shop_id, parent_asin, parent_sku, current_user_id=operator)
        portfolios = _normalize_portfolio_list(res)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "query_portfolio_list 失败 [%s/%s]，严格模式：新建活动全部跳过: %s",
            parent_asin, parent_sku, e)
        plan.warnings.append(
            f"组合列表查询失败，新建活动全部跳过：{type(e).__name__}: {e}")
        return {str(c.get("_card_id") or "") for c in plan.create_calls}
    for call in plan.create_calls:
        cid = str(call.get("_card_id") or "")
        zh = unmap_campaign_group_type(call.get("_group_type"))
        pf, _ = _match_portfolio(zh, portfolios) if zh else (None, 0)
        pid = _pf_field(pf, "portfolioId", "id", "portfolio_id") if pf else None
        if pid:
            call["portfolioId"] = str(pid)
        else:
            skip.add(cid)
            cname = (call.get("createCampaignVo") or {}).get("campaignName")
            plan.warnings.append(
                f"新建活动「{cname}」未匹配到「{zh or call.get('_group_type')}」组合，已跳过")
    return skip


async def _resolve_modify_portfolios(
    client, plan, *, shop_id: int, parent_asin: str, parent_sku: str, operator: str,
) -> None:
    """为 MODIFY 路径注入 portfolioId（挪组）。

    - 查 query_portfolio_list → 按组名匹配 → 按 portfolioId 拆分 paramsVoList。
    - portfolioId 放在 paramsVo 顶层（schema 规定位置）。
    - 匹配失败的活动移除 campaignGroupType，其他调整字段原样保留继续下发，写入 move_errors。
    - 查询失败时整体跳过挪组，其他调整继续。
    """
    if not plan.params_vo_list:
        return

    all_vos: list[dict] = []
    for pv in plan.params_vo_list:
        all_vos.extend(pv.get("campaignVoList") or [])

    if not all_vos:
        return

    # 从 ops 建 campaign_id → campaign_name 映射（不污染 campaignVo）
    campaign_names: dict[str, str] = {}
    for op in plan.ops:
        cid = str(op.get("campaign_id") or "")
        cname = op.get("campaign_name")
        if cid and cname:
            campaign_names[cid] = str(cname)

    try:
        res = await client.query_portfolio_list(
            shop_id, parent_asin, parent_sku, current_user_id=operator)
        portfolios = _normalize_portfolio_list(res)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "query_portfolio_list 失败 [%s/%s]，已跳过挪组，其他调整继续: %s",
            parent_asin, parent_sku, e)
        plan.warnings.append(
            f"组合列表查询失败，已跳过挪组，其他调整继续下发：{type(e).__name__}: {e}")
        for vo in all_vos:
            vo.pop("campaignGroupType", None)
        return

    by_pid: dict[str, list[dict]] = {}
    no_pid: list[dict] = []

    for vo in all_vos:
        group_code = (vo.get("campaignGroupType") or "").strip()
        if not group_code:
            no_pid.append(vo)
            continue
        zh = unmap_campaign_group_type(group_code)
        if not zh:
            vo.pop("campaignGroupType", None)
            no_pid.append(vo)
            continue
        pf, match_count = _match_portfolio(zh, portfolios)
        pid = _pf_field(pf, "portfolioId", "id", "portfolio_id") if pf else None
        if pid:
            vo.pop("campaignGroupType", None)
            by_pid.setdefault(str(pid), []).append(vo)
        else:
            vo.pop("campaignGroupType", None)
            no_pid.append(vo)
            cid = str(vo.get("campaignId") or "")
            if match_count == 0:
                reason = "不存在"
            elif match_count > 1:
                reason = "存在多个"
            else:
                reason = "ID缺失"
            plan.move_errors.append({
                "campaign_id": cid,
                "campaign_name": campaign_names.get(cid, ""),
                "group": zh,
                "reason": reason,
            })

    base = plan.params_vo_list[0]
    base_meta = {k: v for k, v in base.items() if k != "campaignVoList"}
    new_list: list[dict] = []

    for pid, vos in by_pid.items():
        new_list.append({**base_meta, "portfolioId": pid, "campaignVoList": vos})

    if no_pid:
        new_list.append({**base_meta, "campaignVoList": no_pid})

    plan.params_vo_list = new_list


async def submit_execution(decision_id: str, *, operator: str) -> dict:
    """执行一个批次已确认(CONFIRMED)的调整。返回汇总 dict。"""
    if not settings.advert_mcp_enabled:
        return {"ok": False, "error": "广告调整执行通道未启用（advert_mcp_enabled=false）"}

    repo = _get_repository()
    pending = repo.load_confirmed_pending(decision_id)
    if not pending:
        return {"ok": False, "error": f"批次 {decision_id} 不存在"}

    plan = build_exec_plan(pending, operator=operator)
    if plan.is_empty():
        return {"ok": True, "applied": 0, "skipped": 0, "msg": "无待执行项（可能已执行或无确认）"}

    dec = pending["decision"]
    request_json = json_dumps({
        "params_vo_list": plan.params_vo_list,
        "create_calls": [{k: v for k, v in c.items() if k not in ("_card_id", "_group_type")} for c in plan.create_calls],
        "negative_calls": plan.negative_calls,
    })
    record_id = repo.insert_advert_record(
        decision_id=decision_id,
        shop_id=int(dec.get("shop_id") or 0),
        parent_asin=str(dec.get("parent_asin") or ""),
        parent_seller_sku=str(dec.get("parent_seller_sku") or ""),
        current_user_id=operator,
        request_params_json=request_json,
    )

    # ── DRY-RUN：只落记录，不调 MCP ──
    if settings.advert_exec_dry_run:
        for op in plan.ops:
            op["modify_result"] = "DRY_RUN"
            op["execute_status"] = "DRY_RUN"
        repo.insert_exec_sub_records(record_id, plan.ops, operator)
        repo.update_pending_execute_status(plan.ops, "DRY_RUN", operator=operator, msg="dry_run")
        repo.update_advert_record_result(record_id, response_params_json=json_dumps({"dry_run": True}))
        logger.info("Advert exec DRY-RUN [%s] record=%s ops=%d", decision_id, record_id, len(plan.ops))
        return {"ok": True, "dry_run": True, "record_id": record_id,
                "ops": len(plan.ops), "warnings": plan.warnings}

    # ── 真实执行 ──
    client = AdvertMcpClient()
    results: dict = {"async": None, "create": [], "negative": []}
    task_ids: list[str] = []
    errors: list[str] = []
    try:
        # 挪组：MODIFY 路径注入 portfolioId（先于 MCP 调用）
        try:
            await _resolve_modify_portfolios(
                client, plan,
                shop_id=int(dec.get("shop_id") or 0),
                parent_asin=str(dec.get("parent_asin") or ""),
                parent_sku=str(dec.get("parent_seller_sku") or ""),
                operator=operator,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("portfolio 解析失败 [%s]: %s", decision_id, e)
            plan.warnings.append(f"组合解析失败：{type(e).__name__}: {e}")

        if plan.params_vo_list:
            try:
                res = await client.async_batch_update(plan.params_vo_list)
                results["async"] = res
                task_ids = extract_task_ids(res)
                ok, msg = parse_result_envelope(res)
                # 异步：提交成功 → IN_PROGRESS（待 poll 终态）；提交失败 → FAIL
                st = "IN_PROGRESS" if (ok and task_ids) else ("IN_PROGRESS" if ok else "FAIL")
                for op in plan.ops:
                    if not op.get("is_create") and not op.get("is_negative"):
                        op["modify_result"] = "PENDING" if st == "IN_PROGRESS" else "FAIL"
                        op["execute_status"] = st
                        if st == "FAIL":
                            op["error_msg"] = msg
            except Exception as e:  # noqa: BLE001
                logger.exception("async_batch_update 失败 [%s]: %s", decision_id, e)
                for op in plan.ops:
                    if not op.get("is_create") and not op.get("is_negative"):
                        op["modify_result"] = "FAIL"; op["execute_status"] = "FAIL"
                        op["error_msg"] = f"{type(e).__name__}: {e}"

        skip_create = await _resolve_create_portfolios(
            client, plan,
            shop_id=int(dec.get("shop_id") or 0),
            parent_asin=str(dec.get("parent_asin") or ""),
            parent_sku=str(dec.get("parent_seller_sku") or ""),
            operator=operator,
        )

        for call in plan.create_calls:
            card_id = call.pop("_card_id", "")
            call.pop("_group_type", None)
            if card_id in skip_create:
                for op in plan.ops:
                    if op.get("is_create") and str(op.get("suggest_card_id")) == card_id:
                        op["modify_result"] = "SKIP"
                        op["execute_status"] = "SKIP"
                        op["error_msg"] = "未匹配到对应组合，已跳过"
                continue
            try:
                res = await client.create_portfolio_campaign(call)
                results["create"].append(res)
                ok, msg = parse_result_envelope(res)
                for op in plan.ops:
                    if op.get("is_create") and str(op.get("suggest_card_id")) == card_id:
                        op["modify_result"] = "SUCCESS" if ok else "FAIL"
                        op["execute_status"] = "SUCCESS" if ok else "FAIL"
                        if not ok:
                            op["error_msg"] = msg
            except Exception as e:  # noqa: BLE001
                logger.exception("create_portfolio_campaign 失败 [%s]: %s", decision_id, e)
                for op in plan.ops:
                    if op.get("is_create") and str(op.get("suggest_card_id")) == card_id:
                        op["modify_result"] = "FAIL"; op["execute_status"] = "FAIL"
                        op["error_msg"] = f"{type(e).__name__}: {e}"

        for call in plan.negative_calls:
            try:
                res = await client.create_negative_keywords(call)
                results["negative"].append(res)
                ok, msg = parse_result_envelope(res)
                for op in plan.ops:
                    if op.get("is_negative"):
                        op["modify_result"] = "SUCCESS" if ok else "FAIL"
                        op["execute_status"] = "SUCCESS" if ok else "FAIL"
                        if not ok:
                            op["error_msg"] = msg
            except Exception as e:  # noqa: BLE001
                logger.exception("create_negative_keywords 失败 [%s]: %s", decision_id, e)
                for op in plan.ops:
                    if op.get("is_negative"):
                        op["modify_result"] = "FAIL"; op["execute_status"] = "FAIL"
                        op["error_msg"] = f"{type(e).__name__}: {e}"
    finally:
        await client.aclose()

    repo.insert_exec_sub_records(record_id, plan.ops, operator)
    repo.update_pending_execute_status(plan.ops, "IN_PROGRESS", operator=operator)
    repo.update_advert_record_result(
        record_id, task_id=(task_ids[0] if task_ids else ""),
        response_params_json=json_dumps(results),
    )
    logger.info("Advert exec submitted [%s] record=%s tasks=%s ops=%d",
                decision_id, record_id, task_ids, len(plan.ops))

    # ★ 执行钩子：async_batch_update 成功 → 写/删 池表
    async_ok = bool(results.get("async")) and not any(
        str(e).startswith("async:") for e in errors
    )
    if async_ok:
        _sync_pool_entries_from_exec(
            pending, plan, mcp_async_ok=True, repo=repo,
        )

    return {"ok": True, "record_id": record_id, "task_ids": task_ids,
            "ops": len(plan.ops), "warnings": plan.warnings,
            "move_errors": plan.move_errors}


async def submit_execution_direct(
    decision_id: str, card_ids: list[str], *, operator: str
) -> dict:
    """合并"同意+执行"：按 card_ids 直接拉 pending → 调 MCP 真跑 → 不写任何 confirm_status / record / pending.execute_status。

    用于前端"同意所选"弹窗确认后的一步式真实下发。
    """
    if not settings.advert_mcp_enabled:
        return {"ok": False, "error": "广告调整执行通道未启用（advert_mcp_enabled=false）"}
    if not card_ids:
        return {"ok": False, "error": "card_ids 必填"}

    repo = _get_repository()
    pending = repo.load_pending_by_card_ids(decision_id, card_ids)
    if not pending:
        return {"ok": False, "error": f"批次 {decision_id} 不存在或 card_ids 无匹配"}

    plan = build_exec_plan(pending, operator=operator)
    if plan.is_empty():
        return {"ok": True, "applied": 0, "ops": 0, "msg": "选中项无可执行操作"}

    # DRY-RUN
    if settings.advert_exec_dry_run:
        logger.info("Advert exec DRY-RUN(direct) [%s] ops=%d cards=%d",
                    decision_id, len(plan.ops), len(card_ids))
        return {"ok": True, "dry_run": True, "ops": len(plan.ops),
                "warnings": plan.warnings}

    # 真跑：调 MCP，不写库
    client = AdvertMcpClient()
    try:
        # 挪组：MODIFY 路径注入 portfolioId
        await _resolve_modify_portfolios(
            client, plan,
            shop_id=int((pending.get("decision") or {}).get("shop_id") or 0),
            parent_asin=str((pending.get("decision") or {}).get("parent_asin") or ""),
            parent_sku=str((pending.get("decision") or {}).get("parent_seller_sku") or ""),
            operator=operator,
        )
    except Exception as e:
        logger.exception("portfolio 解析失败(direct) [%s]: %s", decision_id, e)
        plan.warnings.append(f"组合解析失败：{type(e).__name__}: {e}")

    results: dict = {"async": None, "create": [], "negative": []}
    task_ids: list[str] = []
    errors: list[str] = []
    try:
        if plan.params_vo_list:
            try:
                res = await client.async_batch_update(plan.params_vo_list)
                results["async"] = res
                task_ids = extract_task_ids(res)
                ok, msg = parse_result_envelope(res)
                if not ok:
                    errors.append(f"async: {msg}")
            except Exception as e:  # noqa: BLE001
                logger.exception("async_batch_update 失败(direct) [%s]: %s", decision_id, e)
                errors.append(f"async: {type(e).__name__}: {e}")

        _dec_d = pending.get("decision") or {}
        skip_create = await _resolve_create_portfolios(
            client, plan,
            shop_id=int(_dec_d.get("shop_id") or 0),
            parent_asin=str(_dec_d.get("parent_asin") or ""),
            parent_sku=str(_dec_d.get("parent_seller_sku") or ""),
            operator=operator,
        )

        for call in plan.create_calls:
            cid = call.pop("_card_id", "")
            call.pop("_group_type", None)
            if cid in skip_create:
                continue
            try:
                res = await client.create_portfolio_campaign(call)
                results["create"].append(res)
                ok, msg = parse_result_envelope(res)
                if not ok:
                    errors.append(f"create: {msg}")
            except Exception as e:  # noqa: BLE001
                logger.exception("create_portfolio_campaign 失败(direct) [%s]: %s", decision_id, e)
                errors.append(f"create: {type(e).__name__}: {e}")

        for call in plan.negative_calls:
            try:
                res = await client.create_negative_keywords(call)
                results["negative"].append(res)
                ok, msg = parse_result_envelope(res)
                if not ok:
                    errors.append(f"negative: {msg}")
            except Exception as e:  # noqa: BLE001
                logger.exception("create_negative_keywords 失败(direct) [%s]: %s", decision_id, e)
                errors.append(f"negative: {type(e).__name__}: {e}")
    finally:
        await client.aclose()

    logger.info("Advert exec direct [%s] ops=%d task_ids=%s errs=%d",
                decision_id, len(plan.ops), task_ids, len(errors))
    if errors:
        for i, e in enumerate(errors, 1):
            logger.warning("Advert exec direct [%s] error %d/%d: %s",
                           decision_id, i, len(errors), str(e)[:500])

    # ★ 执行钩子：async_batch_update 成功 → 写/删 池表。新建/否词路径不影响池表。
    async_ok = bool(results.get("async")) and not any(
        str(e).startswith("async:") for e in errors
    )
    if async_ok:
        _sync_pool_entries_from_exec(
            pending, plan, mcp_async_ok=True, repo=_get_repository(),
        )

    return {"ok": not errors, "ops": len(plan.ops), "task_ids": task_ids,
            "errors": errors, "warnings": plan.warnings,
            "move_errors": plan.move_errors}

