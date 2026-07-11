"""组合(portfolio)预算调整真实执行（任务C）。

execute_portfolio_budget:
  实时查 portfolio 列表(组名→portfolioId) → 构造 paramsVo(portfolioId+portfolioBudget)
  → 插主记录(advert_record) → 调 MCP agent_async_batch_update_advert(或 dry-run)
  → 插 portfolio 子记录(t_advert_agent_modify_portfolio_record)。

安全：advert_mcp_enabled 总开关；advert_exec_dry_run 默认空跑（不动真实广告）。
两步模型：前端「组合预算调整」仅暂存 override；「执行」才走本流程真实下发。
"""

from __future__ import annotations

import logging
from typing import Any

from app.config.settings import settings
from app.data.advert_mcp_client import AdvertMcpClient
from app.persistence.erp_writer.advert_exec_mapper import (
    extract_task_ids,
    parse_result_envelope,
)
from app.persistence.erp_writer.repository import _get_repository
from app.persistence.erp_writer.text_utils import json_dumps

logger = logging.getLogger(__name__)

_LOW_BID = "低价捡漏组"  # 固定 $1，不参与组合预算调整


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pf_field(pf: dict | None, *names: str) -> Any:
    """从 portfolio dict 容错取字段（MCP 字段名大小写/命名可能多变）。"""
    if not isinstance(pf, dict):
        return None
    for n in names:
        if n in pf and pf[n] not in (None, ""):
            return pf[n]
    return None


def _normalize_portfolio_list(res: Any) -> list[dict]:
    """query_portfolio_list 返回必须为 list[dict]。非预期格式直接报错，不猜测信封。"""
    if isinstance(res, list):
        return [x for x in res if isinstance(x, dict)]
    raise ValueError(
        f"query_portfolio_list 返回格式异常，期望 list，实际 {type(res).__name__}"
    )


def _match_portfolio(group_name: str, portfolios: list[dict]) -> tuple[dict | None, int]:
    """组合分组名(精准主力组…) → (MCP portfolio, 匹配数)。仅子串包含匹配，必须唯一。

    - 恰好 1 个 → (portfolio, 1)
    - 0 个     → (None, 0)
    - ≥2 个   → (None, match_count)  — ambiguous
    """
    g = (group_name or "").strip()
    matches: list[dict] = []
    for pf in portfolios:
        nm = str(_pf_field(pf, "portfolioName", "name", "portfolio_name") or "").strip()
        if g and nm and g in nm:
            matches.append(pf)
    if len(matches) == 1:
        return matches[0], 1
    if len(matches) >= 2:
        logger.warning(
            "portfolio 子串匹配不唯一 [%s]: 命中 %d 个 (%s)，跳过",
            g, len(matches),
            ", ".join(str(_pf_field(m, "portfolioName", "name")) for m in matches[:5]),
        )
    return None, len(matches)


async def execute_portfolio_budget(
    decision_id: str, *, asin: str, portfolio_overrides: dict, operator: str,
) -> dict:
    """执行一个批次的组合预算调整。返回汇总 dict。"""
    if not settings.advert_mcp_enabled:
        return {"ok": False, "error": "广告调整执行通道未启用（advert_mcp_enabled=false）"}

    # 过滤：低价捡漏组固定不改；只保留合法非负数值
    overrides: dict[str, float] = {}
    for k, v in (portfolio_overrides or {}).items():
        name = str(k).strip()
        if name == _LOW_BID:
            continue
        nv = _num(v)
        if nv is not None and nv >= 0:
            overrides[name] = nv
    if not overrides:
        return {"ok": True, "applied": 0, "skipped": 0, "msg": "无有效的组合预算调整项"}

    repo = _get_repository()
    dec = repo.get_decision_basic(decision_id)
    if not dec:
        return {"ok": False, "error": f"批次 {decision_id} 不存在"}
    shop_id = int(dec.get("shop_id") or 0)
    parent_asin = str(dec.get("parent_asin") or asin or "")
    parent_sku = str(dec.get("parent_seller_sku") or "")

    client = AdvertMcpClient()
    try:
        # ── 实时查 portfolio 列表（组名 → portfolioId）──
        portfolios: list[dict] = []
        query_err: str | None = None
        try:
            res = await client.query_portfolio_list(
                shop_id, parent_asin, parent_sku, current_user_id=operator)
            portfolios = _normalize_portfolio_list(res)
        except Exception as e:  # noqa: BLE001
            query_err = f"{type(e).__name__}: {e}"
            logger.exception("query_portfolio_list 失败 [%s]: %s", decision_id, e)

        # ── 构造 paramsVo + 逐项 ops ──
        params_vo_list: list[dict] = []
        ops: list[dict] = []
        warnings: list[str] = []
        if query_err:
            warnings.append(f"组合列表查询失败：{query_err}")
        for gname, new_budget in overrides.items():
            pf, _ = _match_portfolio(gname, portfolios)
            pid = _pf_field(pf, "portfolioId")
            old_budget = _num(_pf_field(pf, "portfolioBudget", "budget", "dailyBudget"))
            op: dict[str, Any] = {
                "portfolio_name": gname,
                "portfolio_id": (str(pid) if pid else None),
                "old_budget": old_budget,
                "new_budget": new_budget,
            }
            if not pid:
                op["risk_check_pass"] = False
                op["risk_reject_reason"] = "未匹配到对应 portfolio"
                op["modify_result"] = "SKIP"
                warnings.append(f"组「{gname}」未匹配到 portfolio，已跳过")
            else:
                params_vo_list.append({
                    "shopId": shop_id,
                    "parentAsin": parent_asin,
                    "parentSellerSku": parent_sku,
                    "currentUserId": operator,
                    "adjustReason": f"组合预算调整：{gname} → {new_budget}",
                    "portfolioId": str(pid),
                    "portfolioBudget": new_budget,
                })
            ops.append(op)

        request_json = json_dumps({
            "params_vo_list": params_vo_list,
            "overrides": overrides,
            "warnings": warnings,
        })
        record_id = repo.insert_advert_record(
            decision_id=decision_id, shop_id=shop_id, parent_asin=parent_asin,
            parent_seller_sku=parent_sku, current_user_id=operator,
            request_params_json=request_json,
        )

        # ── DRY-RUN：只落记录，不调 MCP ──
        if settings.advert_exec_dry_run:
            for op in ops:
                if op.get("modify_result") != "SKIP":
                    op["modify_result"] = "DRY_RUN"
            # 跳过 t_advert_agent_modify_portfolio_record 写入（由 ERP 系统自身记录）
            repo.update_advert_record_result(
                record_id, response_params_json=json_dumps({"dry_run": True}))
            logger.info("Portfolio budget DRY-RUN [%s] record=%s ops=%d applied=%d",
                        decision_id, record_id, len(ops), len(params_vo_list))
            return {"ok": True, "dry_run": True, "record_id": record_id,
                    "applied": len(params_vo_list), "ops": len(ops), "warnings": warnings}

        # ── 真实执行 ──
        task_ids: list[str] = []
        result_env: Any = None
        if params_vo_list:
            try:
                res = await client.async_batch_update(params_vo_list)
                result_env = res
                task_ids = extract_task_ids(res)
                ok, msg = parse_result_envelope(res)
                for op in ops:
                    if op.get("modify_result") == "SKIP":
                        continue
                    op["modify_result"] = "PENDING" if ok else "FAIL"
                    if not ok:
                        op["error_msg"] = msg
            except Exception as e:  # noqa: BLE001
                logger.exception("portfolio async_batch_update 失败 [%s]: %s", decision_id, e)
                for op in ops:
                    if op.get("modify_result") != "SKIP":
                        op["modify_result"] = "FAIL"
                        op["error_msg"] = f"{type(e).__name__}: {e}"

        # 跳过 t_advert_agent_modify_portfolio_record 写入（由 ERP 系统自身记录）
        repo.update_advert_record_result(
            record_id, task_id=(task_ids[0] if task_ids else ""),
            response_params_json=json_dumps({"result": result_env, "task_ids": task_ids}))
        logger.info("Portfolio budget submitted [%s] record=%s tasks=%s ops=%d applied=%d",
                    decision_id, record_id, task_ids, len(ops), len(params_vo_list))
        return {"ok": True, "record_id": record_id, "task_ids": task_ids,
                "applied": len(params_vo_list), "ops": len(ops), "warnings": warnings}
    finally:
        await client.aclose()
