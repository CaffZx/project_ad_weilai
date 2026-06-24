"""Campaign 活动分析 API — 调试用薄路由，委托给 steps/campaign.py。

设计权衡（本期）：
- 不走 ctx 路径（因 run_campaign_analysis 内 ensure_data 解包问题 A1 本期未修）
- 在 handler 内手动组装 strat_ctx + 三级 target_acos 回落
- 用 get_state_manager() 单例，与左侧卡片读写后端一致
- fetch 时 meta_filter=["META_AD_PRODUCT","META_TREND"]：广告指标 + product_sales
  (后者供 avg_daily_sales_30d → 库存天数)，砍掉其余 Campaign 用不到的 META
"""

import asyncio
import logging

from fastapi import APIRouter

from app.config.settings import settings
from app.core.data_aggregator import DataAggregator
from app.core.recommender import TargetAcosRecommender
from app.data.campaign_fetcher import CampaignFetcher
from app.llm.reasoner import reasoner
from app.models.campaign import CampaignAnalysisResult, CampaignConfirmRequest
from app.persistence.erp_writer.auto_push import (
    analysis_to_kb_payload,
    push_full_to_erp,
    should_push_to_erp,
    wizard_payload_from_state,
)
from app.persistence.erp_writer.repository import _get_repository
from app.persistence.state_factory import get_state_manager
from app.workflow.steps.campaign import (
    analyze_campaigns,
    build_campaign_strategy_context,
)
from app.api.campaign_viewmodel import from_db_snapshot

router = APIRouter()
logger = logging.getLogger(__name__)


async def _maybe_push_erp(
    result: CampaignAnalysisResult,
    *,
    asin: str,
    days: int,
    temperature: float,
    write_erp: bool,
    state,
    analysis_mode: str = "REALTIME",
    resolved_target_acos: int | None = None,
    resolved_daily_budget: float | None = None,
    operator: str = "tab5",
    cfg_override: dict | None = None,
) -> dict:
    """分析成功后可选写入 ERP；失败不抛异常。"""
    enabled = write_erp or settings.erp_auto_write
    if not enabled:
        return {"attempted": False, "ok": False, "skipped": "erp_auto_write disabled"}

    ok, reason = should_push_to_erp(result)
    if not ok:
        out = {"attempted": False, "ok": False, "skipped": reason}
        if reason == "data_unavailable":
            # 上游数据拉取失败：显式标记，供批量/前端区分于良性"无调整"跳过
            out["data_unavailable"] = True
        return out

    wizard_payload, wizard_partial = wizard_payload_from_state(
        asin, days, state,
        resolved_target_acos=resolved_target_acos,
        resolved_daily_budget=resolved_daily_budget,
        cfg_override=cfg_override,
    )
    kb_payload = analysis_to_kb_payload(result, temperature=temperature)
    try:
        report = await asyncio.to_thread(
            push_full_to_erp,
            kb_payload,
            wizard_payload,
            operator=operator,
        )
        out = {
            "attempted": True,
            "ok": True,
            "decision_id": report.decision_id,
            "wizard_partial": wizard_partial,
            **report.as_dict(),
        }
        logger.info(
            "ERP write_full OK [%s] decision_id=%s cards=%d",
            asin, report.decision_id, report.modern_card,
        )
        # 批次收尾：本批次置最新（is_latest 我方维护）+ 清进行中事件标记（state 库）
        try:
            from app.persistence.erp_writer.repository import _get_repository
            await asyncio.to_thread(
                _get_repository().finalize_batch, report.decision_id, asin, analysis_mode,
            )
            await asyncio.to_thread(state.clear_analysis_session, asin)
        except Exception as fe:
            logger.warning("finalize_batch/清进行中 失败 [%s] %s: %s (非阻塞)", asin, report.decision_id, fe)
        return out
    except Exception as e:
        logger.exception("ERP write_full 失败 [%s]: %s", asin, e)
        return {
            "attempted": True,
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "wizard_partial": wizard_partial,
        }


@router.post("/campaign/analyze")
async def campaign_analyze(req: dict):
    """运行 Campaign LLM 分析（分批 + R1+R2 投票 + 可选 R3 + sanity_check）。

    保持老格式 CampaignAnalysisResult，供 campaign_test.html 独立调试用。
    """
    result, extra = await _do_analyze(req)

    body = result.model_dump()
    if extra is not None:
        body["erp_write"] = await _maybe_push_erp(
            result,
            asin=extra["asin"],
            days=extra["days"],
            temperature=extra["temperature"],
            write_erp=extra["write_erp"],
            state=extra["state"],
            analysis_mode=extra["analysis_mode"],
            resolved_target_acos=extra.get("resolved_target_acos"),
            resolved_daily_budget=extra.get("resolved_daily_budget"),
            operator=extra.get("operator", "tab5"),
            cfg_override=extra.get("cfg_override"),
        )
    return body


@router.post("/campaign/viewmodel")
async def campaign_viewmodel(req: dict):
    """实时操作台：分析 → 强制落库 → 回读快照渲染（方向 A 收敛）。

    单一 mapper `from_db_snapshot` 为唯一真相源（与 /campaign/snapshot 同源），
    消两 mapper 漂移债。红线：执行层无落库记录禁止展示可执行——落库/回读失败
    一律明确报错（_failure_vm），不内存兜底。
    """
    result, extra = await _do_analyze(req)
    if extra is None:                       # 分析本身失败（超时/异常/缺 asin）
        return _failure_vm(result)

    # 实时轨强制落库（本路径落库是硬约束，不看请求的 write_erp 位）。
    erp = await _maybe_push_erp(
        result,
        asin=extra["asin"],
        days=extra["days"],
        temperature=extra["temperature"],
        write_erp=True,
        state=extra["state"],
        analysis_mode=extra["analysis_mode"],
        resolved_target_acos=extra.get("resolved_target_acos"),
        resolved_daily_budget=extra.get("resolved_daily_budget"),
        operator=extra.get("operator", "tab5"),
    )
    decision_id = erp.get("decision_id")
    if not erp.get("ok") or not decision_id:
        reason = erp.get("error") or erp.get("skipped") or "未知原因"
        vm = _failure_vm(result, extra_warnings=[f"执行层落库失败，无法操作：{reason}"])
        vm["erp_write"] = erp
        return vm

    # 落库成功 → 回读快照，单一 mapper 渲染（mode=interactive 供操作台勾选）。
    try:
        snap = await asyncio.to_thread(_get_repository().read_snapshot, decision_id)
    except Exception as e:  # noqa: BLE001
        logger.exception("read_snapshot 回读失败 [%s] %s", decision_id, e)
        snap = None
    if not snap:
        vm = _failure_vm(
            result,
            extra_warnings=[f"落库成功但快照回读失败（decision_id={decision_id}）"],
        )
        vm["erp_write"] = erp
        return vm

    vm = from_db_snapshot(snap, mode="interactive")
    vm["erp_write"] = erp
    return vm


def _failure_vm(
    result: CampaignAnalysisResult,
    *,
    mode: str = "interactive",
    extra_warnings: list[str] | None = None,
) -> dict:
    """方向 A：分析失败 / 落库失败 → 空 vm（无 items、不可执行），不内存兜底。

    与 to_viewmodel/from_db_snapshot 同形，但 items=[]、is_latest=False，
    前端据此明确报错且不展示可执行 UI。
    """
    warnings = list(result.warnings or [])
    if extra_warnings:
        warnings += extra_warnings
    return {
        "mode": mode,
        "parent_asin": result.parent_asin or "",
        "days": result.days or 7,
        "run_id": result.run_id or "",
        "snapshot_time": None,
        "summary": {
            "total": 0, "eliminate": 0, "adjust": 0, "keep": 0, "create": 0,
            "prefiltered": 0, "lost": 0, "confidence_high": 0,
            "confidence_medium": 0, "confidence_low": 0,
            "budget_impact": None, "sanity_check_passed": result.sanity_check_passed,
        },
        "overview": None, "budget_summary": None, "synthesis": None,
        "items": [], "warnings": warnings, "is_latest": False,
    }


def _empty_snapshot(asin: str, days: int, msg: str) -> dict:
    from datetime import datetime, timezone
    return {
        "mode": "readonly", "parent_asin": asin, "days": days, "run_id": "",
        "snapshot_time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "summary": {"total": 0, "eliminate": 0, "adjust": 0, "keep": 0, "create": 0,
                    "prefiltered": 0, "lost": 0, "confidence_high": 0,
                    "confidence_medium": 0, "confidence_low": 0,
                    "budget_impact": None, "sanity_check_passed": None},
        "overview": None, "budget_summary": None, "synthesis": None,
        "items": [], "warnings": [msg],
    }


@router.get("/campaign/snapshot")
async def campaign_snapshot(asin: str = "", decision_id: str = "", days: int = 7):
    """读取某决策批次的执行层快照（mode=readonly）。

    不传 decision_id 时取该 ASIN is_latest=1 的最新已完成批次。
    """
    if not asin and not decision_id:
        return _empty_snapshot(asin, days, "asin 或 decision_id 必填")

    try:
        repo = _get_repository()
        if not decision_id:
            latest = await asyncio.to_thread(repo.get_latest_completed, asin)
            if not latest:
                return _empty_snapshot(asin, days, "该 ASIN 暂无已完成批次，请先运行执行层分析")
            decision_id = latest.get("decision_id")
        snap = await asyncio.to_thread(repo.read_snapshot, decision_id)
        if not snap:
            return _empty_snapshot(asin, days, f"批次 {decision_id} 不存在")
        return from_db_snapshot(snap, mode="readonly")
    except Exception as e:
        logger.exception("Snapshot 读取失败 [%s] decision_id=%s: %s", asin, decision_id, e)
        return _empty_snapshot(asin, days, f"快照读取失败: {type(e).__name__}: {e}")


# ── 审核占位端点（本期 stub）─────────────────────────────────────────────────


# ── 共享：抽取核心分析逻辑，供 /campaign/analyze 和 /campaign/viewmodel 复用 ──


async def _do_analyze(req: dict) -> tuple[CampaignAnalysisResult, dict | None]:
    """执行 Campaign LLM 分析，返回 (result, extra)。

    extra 包含 erp_write 所需上下文：state / days / temperature。
    """
    asin = str(req.get("asin", "")).strip()
    days = int(req.get("days", 7))
    temp = req.get("temperature", None)
    refresh = bool(req.get("refresh", False))
    write_erp = bool(req.get("write_erp", False))
    requested_run_id = str(req.get("run_id") or "").strip()
    analysis_mode = str(req.get("analysis_mode") or "REALTIME").upper()

    # ERP URL 注入的上下文（前端以 _ 前缀传入）。带 shop_account 才视为有效，
    # 用于跳过 dwd_shop 反查（定时跑批无此参数则走 DB 解析）。
    _shop_account = str(req.get("_shopAccount") or "").strip()
    erp_override = {
        "shop_account": _shop_account,
        "parent_seller_sku": str(req.get("_parentSellerSku") or "").strip(),
        "site_code": str(req.get("_siteCode") or "").strip(),
        "shop_id": req.get("_shopId"),
    } if _shop_account else None

    if not asin:
        return (
            CampaignAnalysisResult(
                parent_asin="", days=days,
                warnings=["asin 必填"],
                sanity_check_passed=False,
                llm_rounds_completed=0,
            ),
            None,
        )

    try:
        state = get_state_manager()
        sess = state.get_analysis_session(asin)
        session_run_id = str((sess or {}).get("run_id") or "").strip()
        effective_run_id = requested_run_id or session_run_id or None
        long_term = state.get_long_term_config(asin) or {}
        wf = state.get_workflow_state(asin) or {}
        keyword_analysis = wf.get("keyword_analysis", {})
        ad_directions = (wf.get("execution") or {}).get("selected_directions") or []

        # ── 批量定时分析统一走 config（cfg_source=config）：1-4 改读 decision_config；
        #    实时分析不带该开关，仍读 state 缓存（未落库），完成后照常落 config。
        #    _cfg14 一并透传给落库，保证决策记录/回写 config 与分析一致、幂等不踩踏。──
        _cfg14 = None
        if str(req.get("cfg_source") or "").lower() == "config":
            try:
                from app.data.decision_config_reader import load_layer14
                _cfg14 = load_layer14(
                    asin, parent_seller_sku=(erp_override or {}).get("parent_seller_sku"))
            except Exception:  # noqa: BLE001
                _cfg14 = None
            if _cfg14 and _cfg14.get("long_term"):
                # 修复②(2026-06-18 最小改法)：cfg14 只承载 1-4，整体替换会丢掉 state 的
                # daily_budget_override → 定时轨预算 override 因此一直读不到。替换后灌回它，
                # 让运营设的预算在定时分析里生效（目标 ACOS 走 get_target_acos_override 不受此影响）。
                _state_budget_override = (long_term or {}).get("daily_budget_override")
                long_term = dict(_cfg14["long_term"])
                if _state_budget_override is not None:
                    long_term["daily_budget_override"] = _state_budget_override
                if _cfg14.get("ad_directions"):
                    ad_directions = _cfg14["ad_directions"]

        aggregator = DataAggregator()
        # META_TREND(product_sales) 用于 avg_daily_sales_30d → 库存天数；META_AD_PRODUCT 为广告指标。
        asin_data = await asyncio.wait_for(
            aggregator.fetch(asin, days=days, meta_filter=["META_AD_PRODUCT", "META_TREND"]),
            timeout=120,
        )

        strat_ctx = build_campaign_strategy_context(
            asin, asin_data, long_term, keyword_analysis, ad_directions, days=days,
        )

        manual = state.get_target_acos_override(asin)
        if manual is not None:
            strat_ctx.target_acos = int(manual)
        else:
            p3 = state.get_p3_recommendation(asin)
            if p3 and p3.get("target_acos", {}).get("recommended_target"):
                strat_ctx.target_acos = int(p3["target_acos"]["recommended_target"])
            else:
                rec = TargetAcosRecommender().recommend(
                    asin_data, long_term.get("ad_purposes", []),
                )
                strat_ctx.target_acos = int(rec.recommended_target)

        effective_temp = float(temp) if temp is not None else settings.campaign_llm_temperature

        logger.info(
            "Campaign analyze [%s] days=%d temp=%.2f target_acos=%s",
            asin, days, effective_temp, strat_ctx.target_acos,
        )

        # 淘汰复评（KB21§7）入池日期/淘汰前花费：只读 ERP 历史，fail-open（库不通 → {} → 不复评）
        try:
            entry_dates = await asyncio.to_thread(
                _get_repository().get_elimination_entry_dates, asin,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("get_elimination_entry_dates 失败 [%s]: %s (复评跳过)", asin, e)
            entry_dates = {}

        fetcher = CampaignFetcher()
        result = await asyncio.wait_for(
            analyze_campaigns(
                fetcher=fetcher,
                reasoner=reasoner,
                parent_asin=asin,
                asin_data=asin_data,
                strategy_context=strat_ctx,
                days=days,
                temperature=effective_temp,
                refresh=refresh,
                keyword_analysis=keyword_analysis,
                run_id=effective_run_id,
                erp_override=erp_override,
                elimination_entry_dates=entry_dates,
            ),
            timeout=settings.campaign_total_timeout,
        )

        # 操作人 ID：前端 _userId（ERP 用户）或 operator 字段；写库时填 audit 列。
        operator = str(req.get("operator") or req.get("_userId") or "").strip() or "tab5"

        extra = {
            "state": state,
            "asin": asin,
            "days": days,
            "temperature": effective_temp,
            "write_erp": write_erp,
            "analysis_mode": analysis_mode,
            # 分析已解析的最终值（override→p3缓存→recommender 三级兜底），透传给落库，
            # 避免 wizard_payload_from_state 只读被 new-event 清空的 p3_recommendation → 空 ACOS。
            "resolved_target_acos": strat_ctx.target_acos,
            "resolved_daily_budget": strat_ctx.daily_budget,
            "operator": operator,
            "cfg_override": _cfg14,
        }
        return (result, extra)

    except asyncio.TimeoutError as e:
        logger.warning("Campaign analyze 超时 [%s]: %s", asin, e)
        return (
            CampaignAnalysisResult(
                parent_asin=asin, days=days,
                warnings=[f"分析总超时（>{settings.campaign_total_timeout}s），请稍后重试或联系管理员"],
                sanity_check_passed=False,
                llm_rounds_completed=0,
            ),
            None,
        )
    except Exception as e:
        logger.exception("Campaign analyze 异常 [%s]: %s", asin, e)
        return (
            CampaignAnalysisResult(
                parent_asin=asin, days=days,
                warnings=[f"分析失败: {type(e).__name__}: {e}"],
                sanity_check_passed=False,
                llm_rounds_completed=0,
            ),
            None,
        )


@router.post("/campaign/confirm")
async def campaign_confirm(req: CampaignConfirmRequest):
    """双路审核：
    - approve → 直接调 MCP 真改广告，不写库（合并"同意+执行"）
    - reject  → 走原 confirm_decisions：UPDATE suggest_card / *_pending.confirm_status='REJECTED'

    一次请求里 approve / reject 可混合存在，分别处理后合并返回。
    """
    decision_id = (req.run_id or "").strip()
    if not decision_id:
        return {"ok": False, "error": "run_id(decision_id) 必填", "ops": 0}

    approve_ids: list[str] = []
    reject_items: list[dict] = []
    for d in req.decisions:
        cid = (d.campaign_key or "").strip()
        dec = (d.decision or "").lower()
        if not cid:
            continue
        if dec == "approve":
            approve_ids.append(cid)
        elif dec == "reject":
            reject_items.append({"campaign_key": cid, "decision": "reject"})

    if not approve_ids and not reject_items:
        return {"ok": True, "ops": 0, "msg": "无可处理的勾选项"}

    # in-progress 互斥（分析中不让执行）
    sess = get_state_manager().get_analysis_session(req.asin) if req.asin else None
    in_progress = bool(sess and sess.get("run_id"))
    if in_progress and approve_ids:
        return {"ok": False, "error": "存在进行中分析事件，执行权已冻结", "ops": 0}

    from app.persistence.erp_writer.repository import _get_repository
    repo = _get_repository()

    # ── reject 路径：原 confirm_decisions（写 REJECTED）──
    reject_result: dict = {}
    if reject_items:
        try:
            reject_result = await asyncio.to_thread(
                repo.confirm_decisions, decision_id, reject_items, req.operator or None,
                in_progress=in_progress,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("Campaign confirm(reject) 失败 [%s] did=%s: %s", req.asin, decision_id, e)
            reject_result = {"ok": False, "error": f"{type(e).__name__}: {e}", "applied": 0, "skipped": 0}

    # ── approve 路径：直接 MCP 真跑（不写库）──
    approve_result: dict = {}
    if approve_ids:
        try:
            from app.workflow.steps.advert_execution import submit_execution_direct
            approve_result = await submit_execution_direct(
                decision_id, approve_ids, operator=(req.operator or "tab5"),
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("Campaign confirm(approve) 失败 [%s] did=%s: %s", req.asin, decision_id, e)
            approve_result = {"ok": False, "error": f"{type(e).__name__}: {e}", "ops": 0}

    logger.info(
        "Campaign confirm [%s did=%s] approve=%d→ops=%s reject=%d→applied=%s from %s",
        req.asin, decision_id,
        len(approve_ids), approve_result.get("ops"),
        len(reject_items), reject_result.get("applied"),
        req.operator or "anonymous",
    )

    return {
        "ok": (approve_result.get("ok", True) and reject_result.get("ok", True)),
        "approve": approve_result if approve_ids else None,
        "reject": reject_result if reject_items else None,
        # 兼容老前端：扁平展示
        "ops": approve_result.get("ops", 0),
        "applied": reject_result.get("applied", 0),
        "skipped": reject_result.get("skipped", 0),
        "task_ids": approve_result.get("task_ids") or [],
        "errors": approve_result.get("errors") or [],
    }


# ── 广告调整真实执行（Part 6）─────────────────────────────────────────────


@router.post("/campaign/execute")
async def campaign_execute(req: dict):
    """执行已确认(CONFIRMED)的广告调整 → 调广告调整 MCP（dry-run 默认空跑）。

    门禁与 confirm 一致：批次必须无进行中分析事件（state 库）。幂等由
    load_confirmed_pending 只取 execute_status=PENDING 保证。
    """
    decision_id = str(req.get("decision_id") or req.get("run_id") or "").strip()
    asin = str(req.get("asin") or "").strip()
    operator = str(req.get("operator") or req.get("_userId") or "").strip() or "tab5"
    if not decision_id:
        return {"ok": False, "error": "decision_id 必填"}
    try:
        sess = get_state_manager().get_analysis_session(asin) if asin else None
        if sess and sess.get("run_id"):
            return {"ok": False, "error": "存在进行中分析事件，执行权已冻结"}
        from app.workflow.steps.advert_execution import submit_execution
        return await submit_execution(decision_id, operator=operator)
    except Exception as e:  # noqa: BLE001
        logger.exception("Campaign execute 失败 [%s] decision_id=%s: %s", asin, decision_id, e)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@router.post("/campaign/execute-portfolio-budget")
async def campaign_execute_portfolio_budget(req: dict):
    """组合(portfolio)预算调整执行 → 实时查 portfolioId → 调广告调整 MCP（dry-run 默认空跑）。

    入参: {decision_id|run_id, asin, portfolio_overrides:{组名:预算}, operator|_userId}
    门禁与 execute 一致：批次必须无进行中分析事件（state 库）。
    """
    decision_id = str(req.get("decision_id") or req.get("run_id") or "").strip()
    asin = str(req.get("asin") or "").strip()
    operator = str(req.get("operator") or req.get("_userId") or "").strip() or "tab5"
    overrides = req.get("portfolio_overrides") or {}
    if not decision_id:
        return {"ok": False, "error": "decision_id 必填"}
    if not isinstance(overrides, dict) or not overrides:
        return {"ok": False, "error": "portfolio_overrides 必填"}
    try:
        sess = get_state_manager().get_analysis_session(asin) if asin else None
        if sess and sess.get("run_id"):
            return {"ok": False, "error": "存在进行中分析事件，执行权已冻结"}
        from app.workflow.steps.portfolio_execution import execute_portfolio_budget
        return await execute_portfolio_budget(
            decision_id, asin=asin, portfolio_overrides=overrides, operator=operator,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("Portfolio budget execute 失败 [%s] decision_id=%s: %s",
                         asin, decision_id, e)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
