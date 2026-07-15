#!/usr/bin/env python3.11
"""核心词离线判定批量任务：拉三元组列表 → 调 8015 /core-keyword/analyze → 写 ERP。

设计要点:
- 独立于 campaign 批量任务，7 天一次 cron 调度
- asyncio + httpx 并发 3 个产品（核心词判定含 LLM+MCP，避免抢资源）
- 超时 420s/产品
- 实时落状态文件

用法:
    python3.11 batch_core_keyword.py                        # 跑所有产品
    python3.11 batch_core_keyword.py --limit 5              # 只跑前 5 个（测试）
    python3.11 batch_core_keyword.py --asins B0B7S3PWWB,B0CJVMJJQ8  # 指定 ASIN
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pymysql

API = os.getenv(
    "CORE_KEYWORD_API",
    "http://127.0.0.1:8015/api/v1/agent/ad-direction/core-keyword/analyze",
)

ERP_CONN = dict(
    host=os.getenv("BATCH_ERP_HOST", ""),
    port=int(os.getenv("BATCH_ERP_PORT", "3306")),
    user=os.getenv("BATCH_ERP_USER", ""),
    password=os.getenv("BATCH_ERP_PASSWORD", ""),
    database=os.getenv("BATCH_ERP_DATABASE", ""),
    charset="utf8mb4",
    connect_timeout=10,
)

DEFAULT_CONCURRENCY = 5
HTTP_TIMEOUT_PER_PRODUCT = 420.0

log = logging.getLogger("batch_core_keyword")


def fetch_triplets_from_prod() -> list[dict]:
    """从 prod ERP decision_config 表拉产品三元组列表。"""
    conn = pymysql.connect(**ERP_CONN)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT DISTINCT parent_asin, parent_seller_sku, shop_id, site_code "
            "FROM t_advert_agent_decision_config "
            "WHERE parent_seller_sku IS NOT NULL AND parent_seller_sku != '' "
            "  AND shop_id IS NOT NULL AND shop_id > 0"
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    return [
        {"parent_asin": r[0], "parent_seller_sku": r[1],
         "shop_id": int(r[2]), "site_code": r[3] or ""}
        for r in (rows or [])
    ]


class StatusWriter:
    def __init__(self, path: Path):
        self.path = path
        self.results: list[dict] = []
        self.start_ts = datetime.now(timezone.utc).isoformat()
        self._lock = asyncio.Lock()

    async def append(self, entry: dict) -> None:
        async with self._lock:
            self.results.append(entry)
            ok_count = sum(1 for x in self.results if x.get("ok"))
            failed_count = sum(1 for x in self.results if not x.get("ok"))
            body = {
                "summary": {"total": len(self.results), "ok": ok_count, "failed": failed_count},
                "started_at": self.start_ts,
                "results": self.results,
            }
            self.path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")

    def elapsed(self) -> str:
        try:
            start = datetime.fromisoformat(self.start_ts)
            return str(datetime.now(timezone.utc) - start)
        except Exception:
            return "?"


async def analyze_one(client: httpx.AsyncClient, triplet: dict, sem: asyncio.Semaphore,
                      status: StatusWriter) -> dict:
    async with sem:
        start = datetime.now(timezone.utc)
        entry = {
            "parent_asin": triplet["parent_asin"],
            "parent_seller_sku": triplet["parent_seller_sku"],
            "shop_id": triplet["shop_id"],
            "started_at": start.isoformat(),
        }
        try:
            resp = await client.post(
                API, json=triplet,
                timeout=HTTP_TIMEOUT_PER_PRODUCT,
            )
            entry["status_code"] = resp.status_code
            if resp.is_error:
                snippet = (resp.text or "")[:300]
                raise RuntimeError(f"HTTP {resp.status_code}: {snippet}")
            body = resp.json()
            entry["ok"] = bool(body.get("ok"))
            entry["task_id"] = body.get("task_id", "")
            entry["total_keywords"] = body.get("total_keyword_count", 0)
            entry["core_keywords"] = body.get("core_keyword_count", 0)
            entry["error"] = body.get("error") or ""
            if not entry["ok"]:
                log.warning("FAIL [%s/%s/%s]: %s", triplet["parent_asin"],
                            triplet["parent_seller_sku"], triplet["shop_id"],
                            entry["error"][:120])
            else:
                log.info("OK [%s/%s/%s]: %s -> %d keys, %d core",
                         triplet["parent_asin"], triplet["parent_seller_sku"],
                         triplet["shop_id"], entry["task_id"],
                         entry["total_keywords"], entry["core_keywords"])
        except Exception as e:
            entry["ok"] = False
            entry["error"] = str(e)[:300]
            log.warning("FAIL [%s/%s/%s]: %s", triplet["parent_asin"],
                        triplet["parent_seller_sku"], triplet["shop_id"],
                        entry["error"])
        entry["elapsed_s"] = (datetime.now(timezone.utc) - start).total_seconds()
        await status.append(entry)
        return entry


async def main(limit: int | None, asins: list[str] | None, concurrency: int) -> int:
    triplets = fetch_triplets_from_prod()
    if asins:
        asin_set = set(asins)
        triplets = [t for t in triplets if t["parent_asin"] in asin_set]
    if limit and limit > 0:
        triplets = triplets[:limit]

    if not triplets:
        log.warning("无可用产品")
        return 0

    log.info("核心词判定开始: %d 个产品, 并发 %d", len(triplets), concurrency)

    status = StatusWriter(Path(f"batch_core_keyword_status_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"))
    sem = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient() as client:
        tasks = [analyze_one(client, t, sem, status) for t in triplets]
        results = await asyncio.gather(*tasks)

    ok_count = sum(1 for r in results if r.get("ok"))
    log.info("核心词判定结束: %d OK / %d FAIL", ok_count, len(results) - ok_count)
    return 0 if ok_count == len(results) else 1


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="核心词离线判定批量任务")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 个产品")
    parser.add_argument("--asins", type=str, default=None, help="指定 ASIN，逗号分隔")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY, help="并发数")
    args = parser.parse_args()

    asin_list = [a.strip() for a in (args.asins or "").split(",") if a.strip()] or None
    sys.exit(asyncio.run(main(args.limit, asin_list, args.concurrency)))
