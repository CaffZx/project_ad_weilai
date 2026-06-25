#!/usr/bin/env python3.11
"""每日批量任务：拉 ASIN 列表 → 调 8010 /campaign/viewmodel → 自动写 prod ERP.

设计要点:
- 复用 8010 服务 /campaign/viewmodel 路径（已端到端验证写 prod ERP 正常）
- asyncio + httpx 并发 5 个 ASIN（不占满 8010 的 6 worker）
- 每 ASIN 重试 1 次（LLM 抖动恢复，30s 延迟）
- 超时 240s/ASIN（前端实测 60-180s 内完成）
- 实时落状态文件，便于中途观察
- ASIN 列表来源：prod ERP 192.168.0.29.t_advert_agent_decision_config（同老脚本）

用法:
    python3.11 batch_via_api.py                          # 跑所有 ASIN
    python3.11 batch_via_api.py --asins B0CJVMJJQ8,B0... # 指定 ASIN
    python3.11 batch_via_api.py --limit 5                # 只跑前 5 个（测试）
    python3.11 batch_via_api.py --concurrency 3          # 改并发
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pymysql

API = "http://127.0.0.1:8010/api/v1/agent/ad-direction/campaign/viewmodel"

ERP_CONN = dict(
    host="192.168.0.29", port=3306,
    user="erp_agentadvert", password="WAzx)erp2312whIT666&",
    database="erp_agentadvert", charset="utf8mb4",
    connect_timeout=10,
)

DEFAULT_CONCURRENCY = 5
DEFAULT_RETRIES = 1
HTTP_TIMEOUT_PER_ASIN = 240.0
RETRY_DELAY = 30.0

log = logging.getLogger("batch")


def fetch_asin_list_from_prod() -> list[str]:
    """从 prod ERP decision_config 表拉 ASIN 列表（同老脚本逻辑）。"""
    conn = pymysql.connect(**ERP_CONN)
    try:
        cur = conn.cursor()
        cur.execute("SET SESSION group_concat_max_len = 1048576")
        cur.execute(
            "SELECT GROUP_CONCAT(DISTINCT parent_asin) "
            "FROM t_advert_agent_decision_config "
            "WHERE parent_seller_sku IS NOT NULL AND parent_seller_sku != ''"
        )
        r = cur.fetchone()
    finally:
        conn.close()
    raw = (r[0] or "") if r else ""
    return [a.strip() for a in raw.split(",") if a.strip()]


class StatusWriter:
    def __init__(self, path: Path):
        self.path = path
        self.results: list[dict] = []
        self.start_ts = datetime.now(timezone.utc).isoformat()

    def append(self, entry: dict) -> None:
        self.results.append(entry)
        ok = sum(1 for x in self.results if x.get("ok") and not x.get("skipped"))
        skipped = sum(1 for x in self.results if x.get("skipped"))
        failed = sum(1 for x in self.results if not x.get("ok"))
        body = {
            "summary": {
                "total": len(self.results),
                "ok": ok,
                "skipped": skipped,
                "failed": failed,
            },
            "started_at": self.start_ts,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "results": self.results,
        }
        self.path.write_text(
            json.dumps(body, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


async def analyze_one(
    client: httpx.AsyncClient,
    asin: str,
    sem: asyncio.Semaphore,
    retries: int,
    status: StatusWriter,
    lock: asyncio.Lock,
    i: int,
    total: int,
) -> None:
    async with sem:
        entry: dict = {
            "asin": asin,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "ok": False,
        }

        for attempt in range(1, retries + 2):
            t0 = time.time()
            entry["attempt"] = attempt
            log.info("[%d/%d] %s START (attempt %d/%d)", i, total, asin, attempt, retries + 1)
            try:
                body = {
                    "asin": asin,
                    "days": 7,
                    "temperature": 0.3,
                    "analysis_mode": "SCHEDULED",
                    "write_erp": True,
                    "_userId": "batch-cron",
                    "cfg_source": "config",  # 批量定时统一走 decision_config 读 1-4
                }
                resp = await client.post(API, json=body, timeout=HTTP_TIMEOUT_PER_ASIN)
                elapsed = time.time() - t0

                if resp.status_code != 200:
                    entry["error"] = f"HTTP {resp.status_code}: {resp.text[:200]}"
                    log.warning("[%s] HTTP %s (%.0fs)", asin, resp.status_code, elapsed)
                else:
                    data = resp.json()
                    ew = data.get("erp_write", {}) or {}
                    if ew.get("ok"):
                        entry["ok"] = True
                        entry["decision_id"] = ew.get("decision_id")
                        entry["items"] = len(data.get("items", []) or [])
                        entry["elapsed_s"] = round(elapsed, 1)
                        log.info(
                            "[%s] OK %.1fs did=%s items=%d",
                            asin, elapsed, (ew.get("decision_id") or "")[:16], entry["items"],
                        )
                        break
                    # 上游数据拉取失败（数仓/MCP 不可用）：本次未真正分析，不是良性跳过。
                    # 不算成功、不重试（重试结果一样），单独标记 → 汇总分类计数 + 退出码体现，
                    # 避免上游故障被伪装成正常 "no adjustments" 跳过。
                    if ew.get("data_unavailable"):
                        entry["data_unavailable"] = True
                        entry["error"] = str(ew.get("skipped") or "data_unavailable")[:200]
                        entry["elapsed_s"] = round(elapsed, 1)
                        log.warning("[%s] DATA_UNAVAILABLE %.1fs: %s", asin, elapsed, entry["error"])
                        break
                    # 业务正常跳过：attempted=false + skipped 字段说明原因（如 "no adjustments"）
                    # 不算失败，不重试 — 重试结果一样
                    if ew.get("attempted") is False and ew.get("skipped"):
                        entry["ok"] = True
                        entry["skipped"] = str(ew.get("skipped"))[:200]
                        entry["items"] = 0
                        entry["elapsed_s"] = round(elapsed, 1)
                        log.info("[%s] SKIP %.1fs: %s", asin, elapsed, entry["skipped"])
                        break
                    # 真失败：attempted=true + 落库失败 / 或其他
                    err = ew.get("error") or "erp_write not ok"
                    entry["error"] = str(err)[:300]
                    log.warning("[%s] FAIL attempt %d (%.0fs): %s", asin, attempt, elapsed, entry["error"])

            except httpx.TimeoutException:
                entry["error"] = f"timeout after {HTTP_TIMEOUT_PER_ASIN}s"
                log.warning("[%s] TIMEOUT attempt %d", asin, attempt)
            except Exception as e:
                entry["error"] = f"{type(e).__name__}: {e}"
                log.exception("[%s] EXC attempt %d", asin, attempt)

            if attempt <= retries:
                log.info("[%s] sleep %.0fs and retry", asin, RETRY_DELAY)
                await asyncio.sleep(RETRY_DELAY)
                continue
            entry["elapsed_s"] = round(time.time() - t0, 1)
            break

        entry["finished_at"] = datetime.now(timezone.utc).isoformat()
        async with lock:
            status.append(entry)
            ok = sum(1 for r in status.results if r.get("ok"))
            log.info("[%d/%d done] cumulative ok=%d fail=%d", len(status.results), total, ok, len(status.results) - ok)


async def amain() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--asins", help="Comma-separated ASINs (overrides prod ERP fetch)")
    p.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                   help=f"Parallel ASINs (default {DEFAULT_CONCURRENCY})")
    p.add_argument("--retries", type=int, default=DEFAULT_RETRIES,
                   help=f"Retry per ASIN (default {DEFAULT_RETRIES})")
    p.add_argument("--limit", type=int, help="Run only first N ASINs (for testing)")
    p.add_argument("--output-dir", default="/opt/ad_agent_github_chenv31/logs",
                   help="Status file output dir (date subdir auto)")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    if args.asins:
        asins = [a.strip() for a in args.asins.split(",") if a.strip()]
        log.info("ASIN 列表来源: --asins 参数 (%d 个)", len(asins))
    else:
        log.info("从 prod ERP decision_config 表拉 ASIN ...")
        asins = fetch_asin_list_from_prod()
        log.info("拉到 %d 个 ASIN", len(asins))
    if args.limit:
        asins = asins[: args.limit]
        log.info("--limit 截断到前 %d 个", len(asins))

    if not asins:
        log.error("ASIN 列表为空，退出")
        return 0

    date_dir = Path(args.output_dir) / datetime.now().strftime("%Y%m%d")
    date_dir.mkdir(parents=True, exist_ok=True)
    status_path = date_dir / "batch_status.json"
    status = StatusWriter(status_path)

    log.info("== 批次启动 ==")
    log.info("  总 ASIN: %d", len(asins))
    log.info("  并发:    %d", args.concurrency)
    log.info("  重试:    %d", args.retries)
    log.info("  状态:    %s", status_path)

    t0 = time.time()
    sem = asyncio.Semaphore(args.concurrency)
    lock = asyncio.Lock()
    timeout = httpx.Timeout(HTTP_TIMEOUT_PER_ASIN + 30, connect=10.0)
    limits = httpx.Limits(max_connections=args.concurrency + 2, max_keepalive_connections=args.concurrency + 2)

    async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
        tasks = [
            analyze_one(client, asin, sem, args.retries, status, lock, i, len(asins))
            for i, asin in enumerate(asins, start=1)
        ]
        await asyncio.gather(*tasks)

    elapsed = time.time() - t0
    total_n = len(status.results)
    data_unavail = sum(1 for r in status.results if r.get("data_unavailable"))
    ok = sum(1 for r in status.results if r.get("ok") and not r.get("skipped"))
    skipped = sum(1 for r in status.results if r.get("skipped") and not r.get("data_unavailable"))
    # 真失败 = 非成功、非业务跳过、非数据不可用（ERP 落库失败 / HTTP / 超时等）
    failed = sum(
        1 for r in status.results
        if not r.get("ok") and not r.get("skipped") and not r.get("data_unavailable")
    )
    log.info("=================================")
    log.info("== 批次完成 ==")
    log.info("  总耗时: %.1f s (%.1f min)", elapsed, elapsed / 60)
    log.info("  真成功: %d", ok)
    log.info("  业务跳过 (no adjustments): %d", skipped)
    log.info("  数据不可用 (上游数仓/MCP 失败): %d", data_unavail)
    log.info("  失败:   %d", failed)
    log.info("  总计:   %d", total_n)
    log.info("  状态文件: %s", status_path)
    if data_unavail:
        ratio = (data_unavail / total_n) if total_n else 0.0
        log.warning(
            "⚠ 数据不可用 %d/%d (%.0f%%)，疑似上游数仓/MCP 故障，本批结果不可信，请先排查上游再重跑",
            data_unavail, total_n, ratio * 100,
        )
    log.info("=================================")

    # 退出码：真失败 → 1；数据不可用占比过高(>50%) → 2（区别于普通失败，便于 cron/告警识别"上游故障"）。
    DATA_UNAVAIL_ALERT_RATIO = 0.5
    if failed:
        return 1
    if total_n and (data_unavail / total_n) > DATA_UNAVAIL_ALERT_RATIO:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
