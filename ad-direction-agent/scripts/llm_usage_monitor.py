"""LLM Token 消耗 & 缓存命中率监控 — 读取 llm_usage.log 按 label 聚合统计。

用法: python scripts/llm_usage_monitor.py logs/llm_usage.log [--today]
      --today 仅统计当天（默认全量）。
日志格式: <date> <time> <label> prompt=<n> completion=<n> cached=<n> total=<n> model=<name> <latency>s
"""
from __future__ import annotations

import argparse
import collections
import re
from datetime import datetime
from pathlib import Path

_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) \S+ (\S+) "
    r"prompt=(\d+) completion=(\d+) cached=(\d+) total=(\d+) "
    r"model=(\S+) (\d+\.?\d*)s$"
)


def parse(path: Path, today: bool) -> dict[str, list[dict]]:
    label_rows: dict[str, list[dict]] = collections.defaultdict(list)
    today_str = datetime.now().strftime("%Y-%m-%d")
    with path.open(encoding="utf-8") as f:
        for line in f:
            m = _LINE_RE.match(line.rstrip("\n"))
            if not m:
                continue
            date_str = m.group(1)
            if today and date_str != today_str:
                continue
            label_rows[m.group(2)].append({
                "date": date_str,
                "label": m.group(2),
                "prompt_tokens": int(m.group(3)),
                "completion_tokens": int(m.group(4)),
                "cached_tokens": int(m.group(5)),
                "total_tokens": int(m.group(6)),
                "model": m.group(7),
                "latency": float(m.group(8)),
            })
    return label_rows


def print_table(label_rows: dict[str, list[dict]], title: str) -> None:
    print(f"\n=== {title} ===")
    header = f"  {'label':<22} {'calls':>6}  {'prompt_tok':>12}  {'compl_tok':>10}  {'cached_tok':>10}  {'cache_hit%':>9}  {'总tok':>10}  {'avg_lat':>7}"
    print(header)
    print("-" * len(header))

    total_calls = 0
    total_prompt = 0
    total_completion = 0
    total_cached = 0

    for label in sorted(label_rows):
        rows = label_rows[label]
        calls = len(rows)
        prompt = sum(r["prompt_tokens"] for r in rows)
        completion = sum(r["completion_tokens"] for r in rows)
        cached = sum(r["cached_tokens"] for r in rows)
        hit = (cached / prompt * 100) if prompt else 0.0
        avg_lat = sum(r["latency"] for r in rows) / calls if calls else 0

        def _fmt(n: int) -> str:
            if n >= 1_000_000:
                return f"{n/1_000_000:.1f}M"
            if n >= 1_000:
                return f"{n/1_000:.1f}K"
            return str(n)

        print(f"  {label:<22} {calls:>6}  {_fmt(prompt):>12}  {_fmt(completion):>10}  {_fmt(cached):>10}  {hit:>8.1f}%  {_fmt(prompt+completion):>10}  {avg_lat:>6.1f}s")

        total_calls += calls
        total_prompt += prompt
        total_completion += completion
        total_cached += cached

    if total_calls:
        hit = (total_cached / total_prompt * 100) if total_prompt else 0.0
        print("-" * len(header))
        print(f"  {'TOTAL':<22} {total_calls:>6}  {_fmt(total_prompt):>12}  {_fmt(total_completion):>10}  {_fmt(total_cached):>10}  {hit:>8.1f}%  {_fmt(total_prompt+total_completion):>10}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("logfile", help="Path to llm_usage.log")
    p.add_argument("--today", action="store_true", help="仅统计当天")
    args = p.parse_args()
    path = Path(args.logfile)
    if not path.exists():
        print(f"文件不存在: {path}")
        return
    label_rows = parse(path, today=args.today)
    if not label_rows:
        print("暂无数据")
        return
    if args.today:
        print_table(label_rows, "今日统计")
        return
    # 全量：按日分组
    by_date: dict[str, dict[str, list[dict]]] = collections.defaultdict(lambda: collections.defaultdict(list))
    for rows in label_rows.values():
        for r in rows:
            by_date[r["date"]][r["label"]].append(r)
    for date in sorted(by_date):
        print_table(by_date[date], date)


if __name__ == "__main__":
    main()
