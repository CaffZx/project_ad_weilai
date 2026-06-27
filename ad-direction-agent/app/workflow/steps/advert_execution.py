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
from app.data.mcp_db_context import lookup_top_child_attrs
from app.persistence.erp_writer.text_utils import json_dumps, unmap_campaign_group_type
from app.workflow.steps.portfolio_execution import (
    _match_portfolio,
    _normalize_portfolio_list,
    _pf_field,
)

logger = logging.getLogger(__name__)


async def _fill_create_asin_fallback(plan, pending) -> None:
    """create 卡的子 ASIN 已由 build_exec_plan 从库里(card.asin)填好；
    仅当某卡库里没存时，才回退查数仓 top-child 兜底。

    正常情况（前端能渲染出卡=库里已有子 ASIN）下完全不查数仓——避免数仓慢时
    重查超时导致 child_asin=None →「子ASIN不能为空」新建活动全败。
    """
    missing = [c for c in plan.create_calls if not str(c.get("asin") or "").strip()]
    if not missing:
        return
    parent = str((pending.get("decision") or {}).get("parent_asin") or "")
    if not parent:
        return
    import asyncio as _aio
    attrs = await _aio.to_thread(lookup_top_child_attrs, parent)
    fb = str((attrs or {}).get("asin") or "").strip()
    if fb:
        for c in missing:
            c["asin"] = fb
        logger.info("Advert exec: %d 个 create 卡库里无子ASIN → 回退 top-child=%s", len(missing), fb)
    else:
        logger.warning("Advert exec: %d 个 create 卡无子ASIN 且 top-child 查不到", len(missing))


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
        pf = _match_portfolio(zh, portfolios) if zh else None
        pid = _pf_field(pf, "portfolioId", "id", "portfolio_id") if pf else None
        if pid:
            call["portfolioId"] = str(pid)
        else:
            skip.add(cid)
            cname = (call.get("createCampaignVo") or {}).get("campaignName")
            plan.warnings.append(
                f"新建活动「{cname}」未匹配到「{zh or call.get('_group_type')}」组合，已跳过")
    return skip


async def submit_execution(decision_id: str, *, operator: str) -> dict:
    """执行一个批次已确认(CONFIRMED)的调整。返回汇总 dict。"""
    if not settings.advert_mcp_enabled:
        return {"ok": False, "error": "广告调整执行通道未启用（advert_mcp_enabled=false）"}

    repo = _get_repository()
    pending = repo.load_confirmed_pending(decision_id)
    if not pending:
        return {"ok": False, "error": f"批次 {decision_id} 不存在"}

    # 子ASIN 优先复用库里(card.asin，分析阶段已选定)；仅卡里缺失才回退查数仓
    plan = build_exec_plan(pending, operator=operator)
    await _fill_create_asin_fallback(plan, pending)
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
    try:
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
    return {"ok": True, "record_id": record_id, "task_ids": task_ids,
            "ops": len(plan.ops), "warnings": plan.warnings}


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

    # 子ASIN 优先复用库里(card.asin，分析阶段已选定)；仅卡里缺失才回退查数仓
    plan = build_exec_plan(pending, operator=operator)
    await _fill_create_asin_fallback(plan, pending)
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
    return {"ok": not errors, "ops": len(plan.ops), "task_ids": task_ids,
            "errors": errors, "warnings": plan.warnings}

