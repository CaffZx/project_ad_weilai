"""核心词离线判定编排。

MCP 拉数 → data_core(代码) → semantic_core(LLM) → merge → 落 ERP。
与主 campaign 流程完全解耦，不在 /campaign/viewmodel 链路内运行。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from app.config.settings import settings
from app.data.core_keyword_fetcher import (
    CampaignKeywordItem,
    CoreKeywordFetcher,
    ListingProductInfo,
)
from app.llm.reasoner import LLMReasoner, reasoner

logger = logging.getLogger(__name__)


def _new_task_id() -> str:
    """每次离线运行唯一；task 表的 VARCHAR(32) 内保留 ckt 前缀。"""
    return "ckt" + uuid.uuid4().hex[:29]


# ── data_core: 纯代码 ────────────────────────────────────────

def _compute_data_core(
    campaigns: list[CampaignKeywordItem],
    flow_keywords: list[dict[str, Any]],
    category: str,
    target_acos: float | None = None,
) -> dict[str, tuple[bool, list[dict[str, Any]]]]:
    """返回 {keyword_text: (is_data_core, evidence_list)}。
    所有 campaign 已在 Step3 发现阶段过滤为 1:1，无需处理多词活动。
    """
    results: dict[str, tuple[bool, list[dict]]] = {}
    if not campaigns:
        return results

    for c in campaigns:
        results[c.keyword_text] = (False, [])

    _condition_cost_rank(campaigns, results)
    _condition_search_rank(flow_keywords, category, results)
    _condition_orders_acos(campaigns, results, target_acos)
    _condition_nature_rank(campaigns, results)

    return results


def _condition_cost_rank(
    campaigns: list[CampaignKeywordItem],
    results: dict[str, tuple[bool, list[dict]]],
) -> None:
    eligible = [c for c in campaigns if c.cost_14d > 0]
    m = len(eligible)
    if m < 3:
        return
    threshold_idx = max(1, int(m * 0.3))
    sorted_c = sorted(eligible, key=lambda x: x.cost_14d, reverse=True)
    top_kw = {c.keyword_text for c in sorted_c[:threshold_idx]}
    n = len(campaigns)
    for c in campaigns:
        if c.keyword_text in top_kw:
            is_core, ev = results[c.keyword_text]
            ev.append({
                "condition": "高广告花费",
                "value": f"cost=${c.cost_14d:.2f}",
                "threshold": f"关键词花费排名 {sorted_c.index(c)+1}/{m}（前 {sorted_c.index(c)/m:.0%}，阈值 30%）",
            })
            results[c.keyword_text] = (True, ev)


def _condition_search_rank(
    flow_kws: list[dict[str, Any]],
    category: str,
    results: dict[str, tuple[bool, list[dict]]],
) -> None:
    if not flow_kws or not category:
        return
    pool = [k for k in flow_kws if k.get("keyword")]
    n = len(pool)
    if n < 3:
        return
    sorted_k = sorted(pool, key=lambda x: x.get("searches", 0), reverse=True)
    threshold_idx = max(1, int(n * 0.3))
    top_kw = {k["keyword"].strip().lower() for k in sorted_k[:threshold_idx]}
    for kw, (is_core, ev) in results.items():
        if kw.strip().lower() in top_kw:
            ev.append({
                "condition": "高搜索量",
                "value": f"搜索量在 flow_keywords 词池中",
                "threshold": f"top {threshold_idx}/{n}（前 30%）",
            })
            results[kw] = (True, ev)


def _condition_orders_acos(
    campaigns: list[CampaignKeywordItem],
    results: dict[str, tuple[bool, list[dict]]],
    target_acos: float | None = None,
) -> None:
    tolerance = target_acos if target_acos is not None else 40.0
    for c in campaigns:
        is_core, ev = results[c.keyword_text]
        orders = c.orders_14d or 0
        acos = c.acos_14d
        if acos is None:
            continue
        if orders >= 3 and acos <= tolerance:
            ev.append({
                "condition": "广告效果已验证",
                "value": f"orders={orders}, acos={acos:.1f}%",
                "threshold": f"orders>=3, acos<={tolerance:.0f}%",
            })
            results[c.keyword_text] = (True, ev)


def _condition_nature_rank(
    campaigns: list[CampaignKeywordItem],
    results: dict[str, tuple[bool, list[dict]]],
) -> None:
    for c in campaigns:
        if c.natural_rank is None:
            continue
        if c.natural_rank > 50:
            continue
        # rank_change = near - current；>0 表示当前排名数字更小，较上次改善。
        if c.near_natural_rank is not None:
            rank_change = c.near_natural_rank - c.natural_rank
            if rank_change < 0:
                continue  # 排名恶化
        # rank <= 50 AND (rank_change >= 0 or unknown) → 通过
        is_core, ev = results[c.keyword_text]
        ev.append({
            "condition": "自然排名价值",
            "value": f"rank={c.natural_rank}" + (
                f", rank_change={c.near_natural_rank - c.natural_rank}"
                if c.near_natural_rank is not None else ""),
            "threshold": "rank<=50 且排名不恶化",
        })
        results[c.keyword_text] = (True, ev)


# ── merge ────────────────────────────────────────────────────

def _merge(
    keywords: list[str],
    llm_results: list[dict[str, Any]],
    data_results: dict[str, tuple[bool, list[dict]]],
) -> list[dict[str, Any]]:
    """合并 LLM + 代码判定结果，返回落库用的 label 行列表。"""
    llm_by_kw: dict[str, dict] = {}
    for item in llm_results:
        kw = item.get("keyword_text", "")
        llm_by_kw[kw] = item

    rows: list[dict] = []
    for kw in keywords:
        llm = llm_by_kw.get(kw, {})
        conflict = llm.get("semantic_conflict", "pass")
        is_semantic = llm.get("semantic_core", False)
        semantic_evidence = llm.get("semantic_evidence", []) or []

        is_data, data_evidence = data_results.get(kw, (False, []))

        if conflict == "fail":
            is_core = False
            basis = []
        else:
            is_core = is_semantic or is_data
            basis = []
            if is_semantic:
                basis.append("semantic_core")
            if is_data:
                basis.append("data_core")

        rows.append({
            "keyword_text": kw,
            "semantic_conflict": conflict,
            "conflict_reason": llm.get("conflict_reason", ""),
            "semantic_core": is_semantic,
            "semantic_evidence": json.dumps(semantic_evidence, ensure_ascii=False) if semantic_evidence else None,
            "data_core": is_data,
            "data_evidence": json.dumps(data_evidence, ensure_ascii=False) if data_evidence else None,
            "is_core": is_core,
            "core_basis": json.dumps(basis, ensure_ascii=False) if basis else None,
        })
    return rows


# ── 数量截断 ──────────────────────────────────────────────────

def _truncate_top_n(rows: list[dict], max_n: int = 30) -> list[dict]:
    core_rows = [r for r in rows if r["is_core"]]
    non_core_rows = [r for r in rows if not r["is_core"]]
    if len(core_rows) <= max_n:
        return rows

    # 优先级：semantic_core + data_core > data_core > semantic_core
    priority: dict[str, int] = {}
    for r in core_rows:
        b = json.loads(r.get("core_basis") or "[]")
        if "semantic_core" in b and "data_core" in b:
            priority[r["keyword_text"]] = 1
        elif "data_core" in b:
            priority[r["keyword_text"]] = 2
        else:
            priority[r["keyword_text"]] = 3

    core_rows.sort(key=lambda r: priority.get(r["keyword_text"], 3))
    overflow = core_rows[max_n:]
    for r in overflow:
        r["is_core"] = False
        r["core_basis"] = None
    return core_rows[:max_n] + overflow + non_core_rows


# ── 主编排 ────────────────────────────────────────────────────

async def run_core_keyword_analysis(
    parent_asin: str,
    parent_seller_sku: str,
    shop_id: int,
    *,
    target_acos: float | None = None,
    triggered_by: str = "manual",
) -> dict[str, Any]:
    """离线核心词判定主入口。返回 {ok, task_id, core_count, ...}。"""
    task_id = _new_task_id()

    if not settings.core_keyword_analyze_enabled:
        return {"ok": False, "error": "core_keyword_analyze_enabled is False"}

    # 1. MCP 拉数（先读人工策略，获取排除词 + 锁定词数）
    excluded_keyword_norms: set[str] = set()
    locked_count: int = 0
    try:
        from app.persistence.erp_writer.repository import ErpDualWriterRepository
        excluded_keyword_norms = ErpDualWriterRepository.fetch_core_keyword_exclusion_set(
            parent_asin, parent_seller_sku, shop_id,
        )
        locked_count = ErpDualWriterRepository.fetch_core_keyword_locked_count(
            parent_asin, parent_seller_sku, shop_id,
        )
    except Exception:
        logger.warning(
            "读取核心词离线人工策略失败 [%s/%s/%s]，按无策略继续",
            parent_asin, parent_seller_sku, shop_id, exc_info=True,
        )
    fetcher = CoreKeywordFetcher()
    result = await fetcher.fetch(
        parent_asin, parent_seller_sku, shop_id,
        excluded_keyword_norms=excluded_keyword_norms,
    )
    if result.error:
        return {"ok": False, "error": result.error}

    # 2. data_core
    data_results = _compute_data_core(
        result.campaigns, result.flow_keywords,
        result.listing.category, target_acos,
    )

    # 3. semantic_core LLM：分批 20 词/批，并发调用
    all_keywords = [c.keyword_text for c in result.campaigns]
    llm_listing = {
        "title": result.listing.title,
        "bullets_str": "\n".join(result.listing.bullets),
        "variants_str": (
            f"{result.listing.variation_theme or '无变体'}，"
            f"颜色={','.join(result.listing.colors) if result.listing.colors else '无'}，"
            f"尺寸={','.join(result.listing.sizes) if result.listing.sizes else '无'}"
        ),
        "category": result.listing.category,
    }

    BATCH_SIZE = 20
    all_llm_kw: list[dict[str, Any]] = []
    if all_keywords:
        batches = [all_keywords[i:i + BATCH_SIZE]
                   for i in range(0, len(all_keywords), BATCH_SIZE)]
        batch_tasks = [
            reasoner.recommend_semantic_core(
                parent_asin, llm_listing, batch,
                timeout_override=settings.core_keyword_llm_timeout,
            )
            for batch in batches
        ]
        batch_results = await asyncio.gather(*batch_tasks)
        llm_error_parts: list[str] = []
        for i, br in enumerate(batch_results):
            if br.get("error"):
                llm_error_parts.append(f"batch{i}:{br['error']}")
            all_llm_kw.extend(br.get("keywords") or [])
        llm_error = "; ".join(llm_error_parts) if llm_error_parts else ""
    else:
        llm_error = ""

    # 4. merge + 截断
    rows = _merge(all_keywords, all_llm_kw, data_results)
    ai_max = max(0, min(60 - locked_count, 30))
    rows = _truncate_top_n(rows, max_n=ai_max)
    core_count = sum(1 for r in rows if r["is_core"])

    source_refs = ["starsrock"]
    if result.listing.title:
        source_refs.insert(0, "azlisting")

    return {
        "ok": True,
        "task_id": task_id,
        "parent_asin": parent_asin,
        "parent_seller_sku": parent_seller_sku,
        "shop_id": shop_id,
        "site_code": result.context.site_code,
        "total_keyword_count": len(rows),
        "core_keyword_count": core_count,
        "llm_error": llm_error,
        "source_refs": json.dumps(source_refs, ensure_ascii=False),
        "labels": rows,
    }
