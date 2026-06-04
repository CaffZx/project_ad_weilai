"""Campaign KB 遵循度实验运行器 — N × temperature × runs 矩阵 + JSONL 落盘。

用法:
  python scripts/campaign_kb_experiment.py --asins B0C58N3MWJ --temperatures 0.0,0.3,0.7,1.0 --runs 5
  python scripts/campaign_kb_experiment.py --asins-file asins.txt --temperatures 0.0,0.3 --runs 3 --output results.jsonl

产出 JSONL 每行: experiment_id / asin / temperature / run_number / adjustments / raw_llm_outputs / strategy_context
断点续跑: 跳过已完成 (asin, temp, run) 组合。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# 确保 ad-direction-agent 在 sys.path
_PROJ = Path(__file__).resolve().parents[1]
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))

from app.config.settings import settings
from app.core.data_aggregator import DataAggregator
from app.data.campaign_fetcher import CampaignFetcher
from app.llm.reasoner import LLMReasoner
from app.persistence.mysql_state_manager import MySQLStateManager
from app.workflow.steps.campaign import (
    analyze_campaigns,
    build_campaign_strategy_context,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("campaign_kb_experiment")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Campaign KB compliance experiment")
    p.add_argument("--asins", type=str, default="",
                   help="逗号分隔 parent ASIN")
    p.add_argument("--asins-file", type=str, default="",
                   help="每行一个 ASIN 的文件")
    p.add_argument("--temperatures", type=str, default="0.0,0.3,0.7,1.0",
                   help="逗号分隔温度值")
    p.add_argument("--runs", type=int, default=5,
                   help="每个 (asin, temp) 重复次数")
    p.add_argument("--output", type=str, default="",
                   help="输出 JSONL 路径 (默认自动生成)")
    p.add_argument("--batch-size", type=int, default=None,
                   help="每批最大活动数 (默认 settings.campaign_batch_size)")
    p.add_argument("--concurrency", type=int, default=None,
                   help="LLM 并发数 (默认 settings.campaign_llm_concurrency)")
    return p.parse_args()


def _resolve_asins(args: argparse.Namespace) -> list[str]:
    asins: list[str] = []
    if args.asins:
        asins.extend(a.strip() for a in args.asins.split(",") if a.strip())
    if args.asins_file:
        path = Path(args.asins_file)
        if path.exists():
            asins.extend(
                line.strip() for line in path.read_text().splitlines()
                if line.strip() and not line.startswith("#")
            )
    if not asins:
        asins = ["B0C58N3MWJ"]
        logger.warning("未指定 ASIN，使用默认: %s", asins)
    return asins


def _load_completed(output_path: Path) -> set[tuple[str, float, int]]:
    """扫描已有 JSONL，返回已完成的 (asin, temp, run) 集合。"""
    completed: set[tuple[str, float, int]] = set()
    if not output_path.exists():
        return completed
    try:
        for line in output_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                key = (obj.get("parent_asin", ""), obj.get("temperature", 0.0),
                       obj.get("run_number", 0))
                completed.add(key)
            except json.JSONDecodeError:
                continue
    except Exception:
        pass
    return completed


def _append_jsonl(path: Path, obj: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


async def run_experiment() -> None:
    args = _parse_args()

    asins = _resolve_asins(args)
    temperatures = [float(t) for t in args.temperatures.split(",")]
    runs = args.runs

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    output_path = Path(args.output) if args.output else Path(
        f"campaign_kb_experiment_{ts}.jsonl"
    )
    completed = _load_completed(output_path)

    # 初始化服务 (一次创建，跨运行复用)
    aggregator = DataAggregator()
    fetcher = CampaignFetcher()
    reasoner = LLMReasoner()
    state = MySQLStateManager()

    experiment_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    total = len(asins) * len(temperatures) * runs
    done = 0

    for asin in asins:
        logger.info("=== 加载 ASIN 级数据: %s ===", asin)
        try:
            asin_data = await asyncio.wait_for(
                aggregator.fetch(asin, days=7), timeout=120,
            )
        except Exception as e:
            logger.error("ASINData 加载失败 [%s]: %s — 跳过", asin, e)
            done += len(temperatures) * runs
            continue

        long_term = state.get_long_term_config(asin) or {}
        wf_state = state.get_workflow_state(asin) or {}
        keyword_analysis = wf_state.get("keyword_analysis", {})

        # target_acos 优先级: manual > P3 > algorithm
        target_acos = state.get_target_acos_override(asin)
        if target_acos is None:
            p3 = state.get_p3_recommendation(asin)
            if p3 and p3.get("target_acos", {}).get("recommended_target"):
                target_acos = int(p3["target_acos"]["recommended_target"])

        strategy_context = build_campaign_strategy_context(
            asin, asin_data, long_term, keyword_analysis, days=7,
        )
        strategy_context.target_acos = target_acos

        try:
            campaign_data = await fetcher.fetch_campaigns(asin, days=7)
        except Exception as e:
            logger.error("CampaignData 加载失败 [%s]: %s — 跳过", asin, e)
            done += len(temperatures) * runs
            continue

        logger.info("%s: %d 个活动 (数据源=%s)", asin,
                     campaign_data.total_campaigns, campaign_data.fetch_source)

        for temp in temperatures:
            for run in range(1, runs + 1):
                key = (asin, temp, run)
                if key in completed:
                    done += 1
                    logger.info("[%s] temp=%.1f run=%d/%d — 已完成, 跳过 (进度 %d/%d)",
                                asin, temp, run, runs, done, total)
                    continue

                logger.info("[%s] temp=%.1f run=%d/%d (进度 %d/%d)",
                            asin, temp, run, runs, done, total)

                try:
                    result = await analyze_campaigns(
                        fetcher=fetcher,
                        reasoner=reasoner,
                        parent_asin=asin,
                        asin_data=asin_data,
                        strategy_context=strategy_context,
                        days=7,
                        batch_size=args.batch_size,
                        concurrency=args.concurrency,
                        temperature=temp,
                        campaign_data=campaign_data,
                        keyword_analysis=keyword_analysis,
                    )
                except Exception as e:
                    logger.error("[%s] temp=%.1f run=%d 执行失败: %s", asin, temp, run, e)
                    result = None

                # JSONL 行
                line_obj: dict = {
                    "experiment_id": experiment_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "parent_asin": asin,
                    "temperature": temp,
                    "run_number": run,
                    "total_campaigns": campaign_data.total_campaigns,
                    "data_source": campaign_data.fetch_source,
                    "strategy_context": strategy_context.model_dump(),
                }

                if result:
                    line_obj.update({
                        "adjustments": [a.model_dump() for a in result.adjustments],
                        "summary": result.summary,
                        "warnings": result.warnings,
                        "sanity_check_passed": result.sanity_check_passed,
                        "llm_rounds_completed": result.llm_rounds_completed,
                        "rounds_detail": result.rounds_detail,
                    })
                else:
                    line_obj.update({
                        "adjustments": [],
                        "summary": {"error": "execution_failed"},
                        "warnings": ["执行失败"],
                        "sanity_check_passed": False,
                        "llm_rounds_completed": 0,
                        "rounds_detail": {},
                    })

                _append_jsonl(output_path, line_obj)
                done += 1

    logger.info("实验完成。结果: %s (%d 行)", output_path, done)


if __name__ == "__main__":
    asyncio.run(run_experiment())
