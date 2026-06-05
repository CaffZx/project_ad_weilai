"""Run full direction-recommendation wizard for ASINs and export wizard JSONL."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

_PROJ = Path(__file__).resolve().parents[1]
_REPO = _PROJ.parent
_PURPOSE = _REPO / "ad-purpose-agent"
for _p in (_PROJ, _PURPOSE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

STRATEGY_PRESET = {
    "product_level": "腰部",
    "product_stage": "推进期",
    "season_stage": "旺季准备",
}

from app.core.data_aggregator import DataAggregator
from app.core.workflow_orchestrator import WorkflowOrchestrator
from app.llm.reasoner import LLMReasoner
from app.models.layers import (
    AdPurpose,
    ExecutionSelectRequest,
    ProductLevel,
    ProductStage,
    SeasonStage,
    StrategyConfirmRequest,
    TacticsConfirmRequest,
    TargetKeywordStrategy,
)
from app.persistence.state_factory import get_state_manager
from app.persistence.erp_writer.text_utils import map_direction_type

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("wizard_kb_experiment")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Wizard full pipeline exporter")
    p.add_argument("--asins", required=True, help="Comma-separated parent ASINs")
    p.add_argument("--output-dir", default="../cursor临时文件")
    p.add_argument("--days", type=int, default=7)
    return p.parse_args()


def _default_purposes(target_scores: list[dict]) -> list[AdPurpose]:
    mapping = {
        "Traffic": AdPurpose.TRAFFIC,
        "Conversion": AdPurpose.CONVERSION,
        "Ranking": AdPurpose.RANKING,
        "Profit": AdPurpose.PROFIT,
        "引流型": AdPurpose.TRAFFIC,
        "转化型": AdPurpose.CONVERSION,
        "排名型": AdPurpose.RANKING,
        "盈利型": AdPurpose.PROFIT,
    }
    purposes: list[AdPurpose] = []
    for item in target_scores:
        if item.get("level") != "推荐":
            continue
        target = str(item.get("target") or "").strip()
        ap = mapping.get(target)
        if ap and ap not in purposes:
            purposes.append(ap)
    if not purposes:
        purposes = [AdPurpose.CONVERSION]
    return purposes[:2]


async def run_one(orch: WorkflowOrchestrator, asin: str, days: int) -> dict:
    logger.info("=== Wizard [%s] ===", asin)
    orch.state.set_long_term_config(asin, STRATEGY_PRESET)
    wf0 = orch.state.get_workflow_state(asin) or {}
    for key in ("target_scores", "keyword_analysis"):
        val = wf0.get(key)
        if isinstance(val, dict):
            val.pop(str(days), None)
        else:
            wf0.pop(key, None)
    orch.state.set_workflow_state(asin, wf0)
    await orch.confirm_strategy(
        StrategyConfirmRequest(
            asin=asin,
            product_level=ProductLevel(STRATEGY_PRESET["product_level"]),
            product_stage=ProductStage(STRATEGY_PRESET["product_stage"]),
            season_stage=SeasonStage(STRATEGY_PRESET["season_stage"]),
        ),
        days=days,
    )

    tactics_opts = await orch.get_tactics_options(asin, days=days)
    purposes = _default_purposes(tactics_opts.target_scores or [])
    kw_raw = []
    if tactics_opts.current_selection:
        kw_raw = tactics_opts.current_selection.get("target_keyword_strategy") or []
    kw_strategy: list[TargetKeywordStrategy] = []
    for x in kw_raw or ["大词", "长尾词"]:
        try:
            kw_strategy.append(TargetKeywordStrategy(x))
        except ValueError:
            continue
    if not kw_strategy:
        kw_strategy = [TargetKeywordStrategy.BROAD, TargetKeywordStrategy.LONG_TAIL]
    await orch.confirm_tactics(
        TacticsConfirmRequest(
            asin=asin,
            ad_purposes=purposes,
            target_keyword_strategy=kw_strategy,
        )
    )

    await orch.get_diagnosis(asin, days=days)
    p3 = await orch.get_unified_recommendation(asin, refresh=True, days=days)
    exec_opts = await orch.get_execution_options(asin, days=days)
    selected = exec_opts.recommended_directions or [
        d.id for d in exec_opts.directions if d.recommended
    ]
    if not selected and exec_opts.directions:
        selected = [exec_opts.directions[0].id]
    await orch.confirm_execution(
        ExecutionSelectRequest(asin=asin, selected_directions=selected, sub_options={})
    )
    report = await orch.run_validation_and_report(asin, days=days)

    long_term = orch.state.get_long_term_config(asin) or {}
    wf = orch.state.get_workflow_state(asin) or {}
    target_scores = (wf.get("target_scores") or {}).get(str(days)) or tactics_opts.target_scores or []
    keyword_analysis = (wf.get("keyword_analysis") or {}).get(str(days)) or tactics_opts.keyword_analysis or []

    erp_directions = [map_direction_type(d) for d in selected]

    return {
        "parent_asin": asin,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "days": days,
        "long_term_config": long_term,
        "target_scores": target_scores,
        "keyword_analysis": keyword_analysis,
        "p3": p3,
        "directions": report.get("directions") or [],
        "validations": report.get("validations") or {},
        "decisions": report.get("decisions") or {},
        "analysis": report.get("analysis") or {},
        "data_summary": report.get("data_summary") or {},
        "validation_report": report,
        "selected_directions": selected,
        "advert_direction_types": erp_directions,
        "decision_meta": {
            "product_position": long_term.get("product_level"),
            "product_stage": long_term.get("product_stage"),
            "season_type": long_term.get("season_stage"),
            "ad_purposes": long_term.get("ad_purposes") or [p.value for p in purposes],
            "target_keyword_types": long_term.get("target_keyword_strategy")
            or [TargetKeywordStrategy.BROAD.value, TargetKeywordStrategy.LONG_TAIL.value],
            "advert_direction_types": erp_directions,
            "day_range": "DAY_7",
            "p3": p3,
        },
    }


async def main() -> None:
    args = _parse_args()
    asins = [a.strip() for a in args.asins.split(",") if a.strip()]
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    orch = WorkflowOrchestrator(
        aggregator=DataAggregator(),
        reasoner=LLMReasoner(),
        state_manager=get_state_manager(),
    )

    for asin in asins:
        try:
            payload = await run_one(orch, asin, days=args.days)
            out_path = out_dir / f"{asin}_wizard.jsonl"
            with out_path.open("w", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
            logger.info("Wizard 完成: %s", out_path)
        except Exception as e:
            logger.exception("Wizard 失败 [%s]: %s", asin, e)


if __name__ == "__main__":
    asyncio.run(main())
