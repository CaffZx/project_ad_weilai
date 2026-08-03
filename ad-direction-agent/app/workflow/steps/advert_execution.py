"""广告调整真实执行编排（Part 6）— 统一提交/轮询服务。

spec 2026-08-03-pending-taskid-polling-design：
- 所有权限模式统一 pending 执行语义：PENDING → IN_PROGRESS → SUCCESS/FAIL。
- 异步 MCP 返回 taskId 后立即写入精确 pending 行（保持 IN_PROGRESS），后台调度器按
  3/6/12/24 分钟最多四次轮询结果 MCP；终态按 (record_kind, pending_id) 精确回写。
- dry-run 只构造计划，不抢占、不调 MCP、不入轮询队列。
- 任何未拿到有效 taskId 的提交结果都是提交阶段终态失败，不得伪称平台明确失败。

安全：advert_mcp_enabled 总开关；advert_exec_dry_run 默认空跑（不动真实广告）。
幂等：原子抢占 PENDING→IN_PROGRESS 成功才允许提交；已执行行不再取。
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
from app.workflow.steps.campaign_portfolio import match_unique_portfolio
from app.workflow.steps.portfolio_execution import (
    _normalize_portfolio_list,
    _pf_field,
)
from app.workflow.steps.task_poll_scheduler import PollTask, get_scheduler

logger = logging.getLogger(__name__)

# 提交阶段"结果未知"文案（与轮询耗尽、MCP 明确失败区分）
_SUBMIT_UNKNOWN_MSG = (
    "提交结果未知，未获得 taskId，禁止自动重提，需人工核对（{reason}）"
)


def _upsert_eliminate_pool_entry(
    *,
    repo,
    decision: dict,
    card: dict,
    campaign_id: str,
    require_latest_decision: bool = False,
) -> None:
    parent_asin = str(decision.get("parent_asin") or "")
    repo.upsert_pool_entry(
        parent_asin,
        campaign_id=campaign_id,
        child_asin=str(card.get("asin") or "") or None,
        campaign_key=str(card.get("campaign_key") or "") or None,
        campaign_name=str(card.get("campaign_name") or "") or "",
        keyword_text=str(card.get("keyword") or "") or None,
        decision_id=str(decision.get("id") or "") or None,
        eliminate_spend_7d=_perf_json_cost(card.get("perf_json")),
        shop_id=int(decision.get("shop_id") or 0) or None,
        shop_account=None,
        parent_sku=str(
            decision.get("parent_seller_sku") or ""
        ) or None,
        require_latest_decision=require_latest_decision,
    )


def _sync_pool_entries_from_exec(
    pending: dict, plan, *, mcp_async_ok: bool, repo,
) -> None:
    """执行钩子：根据本次 MCP 提交结果写/删 t_advert_agent_pool_entry。

    触发条件：
      - 淘汰卡（suggest_category='ELIMINATE'）：async_batch_update 成功 → upsert_pool_entry
      - 复评卡（trigger_rule 以 'REACTIVATE_' 开头）：async_batch_update 成功 → mark_pool_exit

    只负责池表，不写 pending 的 execute_status（终态由轮询精确回写，spec §6.1 第2条）。
    fail-open：DB 异常仅记日志，不阻断 MCP 已生效的事实。
    """
    dec = pending.get("decision") or {}
    parent_asin = str(dec.get("parent_asin") or "")
    if not parent_asin:
        return
    if not mcp_async_ok:
        return  # MCP 没真跑成功，不写池表

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
                _upsert_eliminate_pool_entry(
                    repo=repo,
                    decision=dec,
                    card=card,
                    campaign_id=campaign_id,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("upsert_pool_entry 失败 [%s/%s]: %s", parent_asin, campaign_id, e)

        if trigger.startswith("REACTIVATE_"):
            try:
                repo.mark_pool_exit(parent_asin, campaign_id)
            except Exception as e:  # noqa: BLE001
                logger.warning("mark_pool_exit 失败 [%s/%s]: %s", parent_asin, campaign_id, e)


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


def _campaign_statuses_from_snapshot(snapshot: dict) -> dict[str, str]:
    """从三类 pending 读取每个活动当前已持久化的聚合状态。"""
    by_campaign: dict[str, list[str]] = {}
    for key in (
        "campaign_pending", "keyword_pending", "placement_pending",
    ):
        for row in snapshot.get(key) or []:
            campaign_id = str(row.get("campaign_id") or "").strip()
            if campaign_id:
                by_campaign.setdefault(campaign_id, []).append(
                    str(
                        row.get("execute_status") or "PENDING"
                    ).strip().upper()
                )

    result: dict[str, str] = {}
    for campaign_id, statuses in by_campaign.items():
        if "FAIL" in statuses:
            result[campaign_id] = "FAIL"
        elif statuses and set(statuses) == {"SUCCESS"}:
            result[campaign_id] = "SUCCESS"
        else:
            result[campaign_id] = "IN_PROGRESS"
    return result


def _sync_terminal_eliminate_pool(
    snapshot: dict,
    campaign_statuses: dict[str, str],
    *,
    repo,
) -> None:
    """仅对最终有效状态为 SUCCESS 的 ELIMINATE 活动写入低价池（轮询终态回写后调用）。"""
    successful = {
        campaign_id
        for campaign_id, status in campaign_statuses.items()
        if status == "SUCCESS"
    }
    if not successful:
        return

    decision = snapshot.get("decision") or {}
    if not str(decision.get("parent_asin") or "").strip():
        return
    card_to_campaign = {
        str(row.get("suggest_card_id") or ""): str(
            row.get("campaign_id") or ""
        ).strip()
        for row in snapshot.get("campaign_pending") or []
        if row.get("suggest_card_id") and row.get("campaign_id")
    }
    for card in snapshot.get("cards") or []:
        if str(card.get("suggest_category") or "").upper() != "ELIMINATE":
            continue
        card_id = str(card.get("id") or "")
        campaign_id = (
            card_to_campaign.get(card_id)
            or str(card.get("campaign_id") or "").strip()
        )
        if campaign_id not in successful:
            continue
        try:
            _upsert_eliminate_pool_entry(
                repo=repo,
                decision=decision,
                card=card,
                campaign_id=campaign_id,
                require_latest_decision=True,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "立即退出终态入池失败 [%s/%s]: %s",
                decision.get("parent_asin"), campaign_id, e,
            )


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
        pf, _ = match_unique_portfolio(zh, portfolios) if zh else (None, 0)
        pid = _pf_field(pf, "portfolioId") if pf else None
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
    resolved_portfolio_ids: dict[str, str] | None = None,
) -> None:
    """为 MODIFY 路径注入 portfolioId（挪组）。"""
    if not plan.params_vo_list:
        return

    all_vos: list[dict] = []
    for pv in plan.params_vo_list:
        all_vos.extend(pv.get("campaignVoList") or [])

    if not all_vos:
        return

    if not any(vo.get("campaignGroupType") for vo in all_vos):
        return

    # 从 ops 建 campaign_id → campaign_name 映射（不污染 campaignVo）
    campaign_names: dict[str, str] = {}
    for op in plan.ops:
        cid = str(op.get("campaign_id") or "")
        cname = op.get("campaign_name")
        if cid and cname:
            campaign_names[cid] = str(cname)

    portfolios: list[dict] | None = None
    if resolved_portfolio_ids is None:
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
        if resolved_portfolio_ids is None:
            pf, match_count = match_unique_portfolio(zh, portfolios or [])
            pid = _pf_field(pf, "portfolioId") if pf else None
        else:
            pid = resolved_portfolio_ids.get(group_code)
            match_count = 1 if pid else 0
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


def _async_ops(plan) -> list[dict]:
    """ExecPlan.ops 中走异步批量调整的行（campaign/keyword/placement，非 create/negative）。"""
    return [
        op for op in plan.ops
        if not op.get("is_create") and not op.get("is_negative")
        and op.get("record_kind") in {"campaign", "keyword", "placement"}
    ]


def _unique_pending_target_count(ops: list[dict]) -> int:
    """Use the same one-row identity at every async execution stage.

    Invalid identities deliberately remain part of the count: the repository
    cannot claim them, so submission is stopped before an MCP call.
    """
    return len({
        (
            str(op.get("record_kind") or ""),
            str(op.get("pending_id") or ""),
        )
        for op in ops
    })


async def submit_and_poll(
    decision_id: str,
    pending: dict,
    plan,
    *,
    operator: str,
    resolved_portfolio_ids: dict[str, str] | None = None,
) -> dict:
    """统一提交+轮询服务（spec §5）：抢占 → 异步提交 → taskId 落库 → 入队轮询。

    调用方必须已确认（confirm_status='CONFIRMED'）或先完成确认。
    返回 dict：dry_run / submitted(IN_PROGRESS) / failed(FAIL) / capacity_full。
    """
    dec = pending.get("decision") or {}
    repo = _get_repository()
    async_ops = _async_ops(plan)
    async_target_count = _unique_pending_target_count(async_ops)

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

    # ── dry-run 分流：只落记录，不抢占、不调 MCP、不入轮询队列（spec §5.1 第1条）──
    if settings.advert_exec_dry_run:
        try:
            for op in plan.ops:
                op["modify_result"] = "DRY_RUN"
                op["execute_status"] = "DRY_RUN"
            repo.insert_exec_sub_records(record_id, plan.ops, operator)
            repo.update_pending_execute_status(
                plan.ops, "DRY_RUN", operator=operator, msg="dry_run",
            )
            repo.update_advert_record_result(
                record_id,
                response_params_json=json_dumps({"dry_run": True}),
            )
            logger.info(
                "Advert exec DRY-RUN [%s] record=%s ops=%d",
                decision_id, record_id, len(plan.ops),
            )
        finally:
            pass  # MCP client 由调用方统一关闭
        return {"ok": True, "dry_run": True, "record_id": record_id,
                "ops": len(plan.ops), "warnings": plan.warnings,
                "move_errors": plan.move_errors}

    # ── 容量预检：有 async 操作时必须先预留轮询名额（spec §5.1 第2条）──
    scheduler = None
    reserved = False
    if async_ops:
        scheduler = get_scheduler()
        if not scheduler.reserve():
            logger.warning(
                "轮询容量不足 [%s] async_ops=%d，拒绝提交，pending 保持 PENDING",
                decision_id, len(async_ops),
            )
            return {"ok": False, "error": "轮询容量不足，请稍后重试", "ops": len(plan.ops),
                    "record_id": record_id, "warnings": plan.warnings,
                    "move_errors": plan.move_errors}
        reserved = True

    # ── 原子抢占（spec §5.1 第4条）：全部目标行成功才允许继续 ──
    if async_ops:
        claimed = repo.claim_pending_for_execution(async_ops, operator=operator)
        if claimed != async_target_count:
            if reserved and scheduler is not None:
                scheduler.release()
            return {
                "ok": True,
                "already_claimed": True,
                "execute_status": "IN_PROGRESS",
                "ops": len(plan.ops),
                "record_id": record_id,
                "warnings": plan.warnings,
                "move_errors": plan.move_errors,
            }

    client = AdvertMcpClient()
    try:
        await _resolve_modify_portfolios(
            client, plan,
            shop_id=int(dec.get("shop_id") or 0),
            parent_asin=str(dec.get("parent_asin") or ""),
            parent_sku=str(dec.get("parent_seller_sku") or ""),
            operator=operator,
            resolved_portfolio_ids=resolved_portfolio_ids,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("portfolio 解析失败 [%s]: %s", decision_id, e)
        plan.warnings.append(f"组合解析失败：{type(e).__name__}: {e}")

    results: dict = {"async": None, "create": [], "negative": []}
    task_ids: list[str] = []
    errors: list[str] = []
    async_submit_ok = False
    try:
        # ── 异步批量调整 ──
        if plan.params_vo_list:
            try:
                res = await client.async_batch_update(plan.params_vo_list)
                results["async"] = res
                task_ids = extract_task_ids(res)
                ok, msg = parse_result_envelope(res)
                if ok and task_ids:
                    async_submit_ok = True
                else:
                    # 提交阶段未拿到有效 taskId = 终态失败（spec §5.1 第6点）
                    reason = msg or "异步提交未返回 taskId"
                    errors.append(f"async:{reason}")
                    for op in async_ops:
                        op["modify_result"] = "FAIL"
                        op["execute_status"] = "FAIL"
                        op["error_msg"] = _SUBMIT_UNKNOWN_MSG.format(reason=reason)
            except Exception as e:  # noqa: BLE001
                logger.exception("async_batch_update 失败 [%s]: %s", decision_id, e)
                reason = f"{type(e).__name__}: {e}"
                errors.append(f"async:{reason}")
                for op in async_ops:
                    op["modify_result"] = "FAIL"
                    op["execute_status"] = "FAIL"
                    op["error_msg"] = _SUBMIT_UNKNOWN_MSG.format(reason=reason)

        # ── 同步新建活动 ──
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

        # ── 同步否词 ──
        if settings.campaign_negative_keyword_exec_enabled:
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
        try:
            await client.aclose()
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Advert MCP client 关闭失败 [%s]: %s",
                decision_id, e,
            )

    # ── 同步路径（create/negative）精确回写 ──
    sync_ops = [op for op in plan.ops if op.get("is_create") or op.get("is_negative")]
    if sync_ops:
        try:
            repo.update_pending_execute_status(sync_ops, "", operator=operator)
        except Exception as e:  # noqa: BLE001
            logger.warning("同步路径 pending 回写失败 [%s]: %s", decision_id, e)
    # 插子记录（历史审计）
    repo.insert_exec_sub_records(record_id, plan.ops, operator)
    repo.update_advert_record_result(
        record_id, task_id=(task_ids[0] if task_ids else ""),
        response_params_json=json_dumps(results),
    )

    # CREATE / negative-keyword tools are synchronous.  When this plan has
    # no async batch operation, their per-row result is the terminal result;
    # there is intentionally no taskId, scheduler, or result-MCP polling.
    if not async_ops:
        sync_failed = any(
            str(op.get("execute_status") or "").upper() == "FAIL"
            for op in sync_ops
        )
        return {
            "ok": not sync_failed,
            "record_id": record_id,
            "task_ids": [],
            "execute_status": "FAIL" if sync_failed else "SUCCESS",
            "errors": errors,
            "ops": len(plan.ops),
            "warnings": plan.warnings,
            "move_errors": plan.move_errors,
        }

    # ── 提交阶段失败（未拿到 taskId）：显式写 FAIL（spec §5.1 第6点，P0 修复）──
    if not async_submit_ok:
        if reserved and scheduler is not None:
            scheduler.release()
        if async_ops:
            reason = "; ".join(errors) or "异步提交未返回 taskId"
            msg = _SUBMIT_UNKNOWN_MSG.format(reason=reason)
            try:
                repo.write_pending_submit_failed(
                    async_ops, msg, task_id=(task_ids[0] if task_ids else ""),
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("提交阶段 FAIL 回写失败 [%s]: %s", decision_id, e)
        return {
            "ok": False, "record_id": record_id, "task_ids": task_ids,
            "execute_status": "FAIL", "errors": errors,
            "ops": len(plan.ops), "warnings": plan.warnings,
            "move_errors": plan.move_errors,
        }

    # ── 提交成功：taskId 落库 → 入队轮询 ──
    task_id = task_ids[0]
    expected_task_id_rows = async_target_count
    task_id_persisted = False
    try:
        written = repo.write_pending_task_id(async_ops, task_id)
        if written != expected_task_id_rows:
            logger.error(
                "task_id 落库不完整 [%s] taskId=%s written=%d/%d",
                decision_id, task_id, written, expected_task_id_rows,
            )
            raise RuntimeError("task_id bind count mismatch")
        else:
            task_id_persisted = True
    except Exception as e:  # noqa: BLE001
        logger.exception("task_id 落库失败 [%s] taskId=%s: %s", decision_id, task_id, e)
        # spec §5.1 第8点：落库失败不得静默当作成功；同步有限重试一次
        try:
            written = repo.write_pending_task_id(async_ops, task_id)
            task_id_persisted = written == expected_task_id_rows
        except Exception:  # noqa: BLE001
            task_id_persisted = False
        if not task_id_persisted:
            if reserved and scheduler is not None:
                scheduler.release()
            logger.error("task_id 落库重试仍失败 [%s] taskId=%s，需人工介入", decision_id, task_id)
            return {
                "ok": False, "record_id": record_id, "task_ids": [task_id],
                "execute_status": "IN_PROGRESS",
                "error": f"taskId 已从 MCP 返回但落库失败，需人工核对 taskId={task_id}",
                "ops": len(plan.ops), "warnings": plan.warnings,
                "move_errors": plan.move_errors,
            }

    # 池表：提交成功即写（保持既有语义；终态 SUCCESS 由轮询回写再核对）
    # A non-exception short write is still a failed task-id bind.  It must
    # never reach the scheduler because the database would no longer be the
    # authoritative audit/recovery record for this task.
    if not task_id_persisted:
        if reserved and scheduler is not None:
            scheduler.release()
        logger.error("task_id bind incomplete [%s] taskId=%s", decision_id, task_id)
        return {
            "ok": False, "record_id": record_id, "task_ids": [task_id],
            "execute_status": "IN_PROGRESS",
            "error": f"taskId returned but persistence failed; manual reconciliation required: {task_id}",
            "ops": len(plan.ops), "warnings": plan.warnings,
            "move_errors": plan.move_errors,
        }

    async_ok = bool(results.get("async")) and not any(
        str(e).startswith("async:") for e in errors
    )
    if async_ok:
        try:
            _sync_pool_entries_from_exec(
                pending, plan, mcp_async_ok=True, repo=repo,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("池表同步失败 [%s]: %s", decision_id, e)

    # 终态回写闭包：精确回写 + SUCCESS 活动写池表
    async def _terminal_write_fn(
        ops, tid, status_by_campaign, message_by_campaign, *, exhausted,
    ) -> None:
        try:
            repo.write_pending_terminal(
                ops, tid, status_by_campaign, message_by_campaign,
                exhausted=exhausted, operator=operator,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("轮询终态回写失败 [%s/%s]: %s", decision_id, tid, e)
        if status_by_campaign:
            try:
                _sync_terminal_eliminate_pool(pending, status_by_campaign, repo=repo)
            except Exception as e:  # noqa: BLE001
                logger.warning("终态池表同步失败 [%s/%s]: %s", decision_id, tid, e)

    enqueued = scheduler.enqueue(PollTask(
        task_id=task_id,
        ops=async_ops,
        poll_fn=lambda tid: client_batch_result_once(tid),
        write_fn=_terminal_write_fn,
    ))
    if not enqueued:
        # taskId 已落库但入队失败：任务不会被轮询，必须告警并转人工（不得静默）
        logger.error(
            "轮询入队失败 [%s] taskId=%s 已落库但无轮询器，需人工核对",
            decision_id, task_id,
        )
        return {
            "ok": False, "record_id": record_id, "task_ids": [task_id],
            "execute_status": "IN_PROGRESS",
            "error": f"taskId 已落库但轮询入队失败，需人工核对 taskId={task_id}",
            "ops": len(plan.ops), "warnings": plan.warnings,
            "move_errors": plan.move_errors,
        }
    logger.info(
        "Advert exec submitted [%s] record=%s taskId=%s ops=%d（轮询已入队）",
        decision_id, record_id, task_id, len(plan.ops),
    )

    return {
        "ok": True, "record_id": record_id, "task_ids": [task_id],
        "execute_status": "IN_PROGRESS",
        "ops": len(plan.ops), "warnings": plan.warnings,
        "move_errors": plan.move_errors,
    }


async def client_batch_result_once(task_id: str) -> dict:
    """单次结果查询：调 agent_batch_update_advert_result，返回逐活动状态。"""
    from app.persistence.erp_writer.advert_exec_mapper import parse_batch_update_terminal
    client = AdvertMcpClient()
    try:
        raw = await client.batch_update_result([task_id])
        message_by_campaign: dict[str, str] = {}
        status_by_campaign = parse_batch_update_terminal(
            raw, message_by_campaign=message_by_campaign,
        )
        return {
            "status_by_campaign": status_by_campaign,
            "message_by_campaign": message_by_campaign,
        }
    finally:
        try:
            await client.aclose()
        except Exception as e:  # noqa: BLE001
            logger.warning("结果 MCP client 关闭失败: %s", e)


async def submit_execution(
    decision_id: str,
    *,
    operator: str,
    resolved_portfolio_ids: dict[str, str] | None = None,
    wait_for_terminal: bool = False,
) -> dict:
    """执行一个批次已确认(CONFIRMED)的调整（统一服务薄包装）。

    wait_for_terminal 参数保留兼容签名，内部不再区分——统一提交后由后台轮询器处理终态。
    """
    if not settings.advert_mcp_enabled:
        return {"ok": False, "error": "广告调整执行通道未启用（advert_mcp_enabled=false）"}

    repo = _get_repository()
    pending = repo.load_confirmed_pending(decision_id)
    if not pending:
        return {"ok": False, "error": f"批次 {decision_id} 不存在"}

    plan = build_exec_plan(pending, operator=operator)
    if plan.is_empty():
        return {"ok": True, "applied": 0, "skipped": 0, "msg": "无待执行项（可能已执行或无确认）"}

    return await submit_and_poll(
        decision_id, pending, plan, operator=operator,
        resolved_portfolio_ids=resolved_portfolio_ids,
    )


async def submit_execution_direct(
    decision_id: str, card_ids: list[str], *, operator: str,
) -> dict:
    """合并"同意+执行"：按 card_ids 先确认再调统一提交/轮询服务（spec §6.2 保留的薄包装）。

    前端"同意所选"弹窗仍为一次交互；确认失败不得下发（spec §6 最后一行）。
    """
    if not settings.advert_mcp_enabled:
        return {"ok": False, "error": "广告调整执行通道未启用（advert_mcp_enabled=false）"}
    if not card_ids:
        return {"ok": False, "error": "card_ids 必填"}

    repo = _get_repository()

    # dry-run：不写 confirm，直接按当前选中范围构造计划（spec §5.1 第1条）
    if settings.advert_exec_dry_run:
        pending = repo.load_pending_by_card_ids(decision_id, card_ids)
        if not pending:
            return {"ok": False, "error": f"批次 {decision_id} 不存在或 card_ids 无匹配"}
        plan = build_exec_plan(pending, operator=operator)
        if plan.is_empty():
            return {"ok": True, "applied": 0, "ops": 0, "msg": "选中项无可执行操作"}
        logger.info("Advert exec DRY-RUN(direct) [%s] ops=%d cards=%d",
                    decision_id, len(plan.ops), len(card_ids))
        return {"ok": True, "dry_run": True, "ops": len(plan.ops),
                "warnings": plan.warnings}

    # 标 CONFIRMED：确认失败不得下发
    confirm_items = [{"card_id": cid, "decision": "approve"} for cid in card_ids]
    confirm_result = await asyncio_to_thread_confirm(repo, decision_id, confirm_items, operator)
    if not confirm_result.get("ok"):
        logger.warning(
            "Campaign confirm 失败，拒绝下发 [%s] cards=%d: %s",
            decision_id, len(card_ids), confirm_result.get("error"),
        )
        return {"ok": False, "ops": 0, "error": f"确认失败，未下发：{confirm_result.get('error')}"}

    # Confirmation can legitimately skip already processed cards.  Reloading
    # only CONFIRMED/PENDING rows gives the shared submit service the exact
    # eligible subset rather than the pre-confirmation selection.
    pending = repo.load_pending_by_card_ids(
        decision_id, card_ids, confirmed_only=True,
    )
    if not pending:
        return {"ok": False, "ops": 0, "error": f"批次 {decision_id} 确认后无匹配项"}
    plan = build_exec_plan(pending, operator=operator)
    if plan.is_empty():
        return {"ok": True, "applied": 0, "ops": 0, "msg": "选中项无已确认待执行操作"}

    return await submit_and_poll(decision_id, pending, plan, operator=operator)


async def asyncio_to_thread_confirm(repo, decision_id, confirm_items, operator):
    import asyncio
    try:
        return await asyncio.to_thread(
            repo.confirm_decisions, decision_id, confirm_items, operator or None,
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
