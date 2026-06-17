"""广告调整真实执行编排（Part 6）。

submit_execution: load CONFIRMED pending → mapper → 插主记录 → 调 MCP(或 dry-run)
                  → 插子记录 + 回写 pending execute_status。
poll_execution:   对仍 IN_PROGRESS 的异步任务查 batch_update_result → 终态回写。

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
from app.persistence.erp_writer.text_utils import json_dumps

logger = logging.getLogger(__name__)


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
        "create_calls": [{k: v for k, v in c.items() if k != "_card_id"} for c in plan.create_calls],
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

        for call in plan.create_calls:
            card_id = call.pop("_card_id", "")
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


async def poll_execution(decision_id: str = "", record_id: str = "") -> dict:
    """查询异步任务最终结果并回写终态。返回 {ok, finalized, records}。"""
    if not settings.advert_mcp_enabled:
        return {"ok": False, "error": "执行通道未启用"}
    repo = _get_repository()
    records = repo.list_execution_records(decision_id=decision_id)
    task_ids = [r["task_id"] for r in records if r.get("task_id")]
    if not task_ids:
        return {"ok": True, "finalized": 0, "msg": "无进行中异步任务"}
    client = AdvertMcpClient()
    try:
        res = await client.batch_update_result(task_ids)
    except Exception as e:  # noqa: BLE001
        logger.exception("batch_update_result 失败 [%s]: %s", decision_id, e)
        await client.aclose()
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    await client.aclose()
    ok, msg = parse_result_envelope(res)
    # 终态结构待真跑细化：此处按信封 ok 粗粒度回写（全量结果存 advert_record）。
    for r in records:
        if r.get("task_id"):
            repo.update_advert_record_result(
                r["id"], response_params_json=json_dumps(res))
    return {"ok": True, "finalized": len(task_ids), "state_ok": ok, "msg": msg}


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

        for call in plan.create_calls:
            call.pop("_card_id", "")
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
    return {"ok": not errors, "ops": len(plan.ops), "task_ids": task_ids,
            "errors": errors, "warnings": plan.warnings}

