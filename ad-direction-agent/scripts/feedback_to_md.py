#!/usr/bin/env python3
"""将反馈 JSON 导出文件转换为 AI 可读的 Markdown 格式

用法:
    python scripts/feedback_to_md.py feedback_export_all.json > feedback_report.md
    python scripts/feedback_to_md.py feedback_export_all.json -o feedback_report.md
"""

import argparse
import json
import sys
from pathlib import Path


ADOPTION_EMOJI = {
    "采纳": "✅",
    "可以优化": "⚠️",
    "完全不采纳": "❌",
    "": "⬜",
}


def _format_value(v) -> str:
    """格式化 AI/人工判断值"""
    if v is None:
        return "—"
    if isinstance(v, list):
        return "、".join(str(x) for x in v)
    return str(v)


def convert(input_path: str, output_path: str | None = None) -> str:
    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        data = [data]

    # 按 ASIN 分组
    by_asin: dict[str, list[dict]] = {}
    for item in data:
        asin = item.get("parent_asin", "UNKNOWN")
        by_asin.setdefault(asin, []).append(item)

    lines: list[str] = []
    lines.append(f"# 广告辅助决策Agent — 反馈报告")
    lines.append(f"\n> 导出时间: {_now()}")
    lines.append(f"> 总记录数: {len(data)}")
    lines.append(f"> 涉及 ASIN: {len(by_asin)} 个\n")
    lines.append("---\n")

    for asin, items in by_asin.items():
        lines.append(f"## ASIN: {asin}  ({len(items)} 条反馈)\n")

        for item in items:
            ts = item.get("submitted_at", "")[:16].replace("T", " ")
            lines.append(f"### {ts}\n")

            # 元信息
            err = item.get("error_info")
            if err:
                lines.append(f"⚠️ **报错**: {err}\n")

            # 反馈表格
            lines.append("| 模块 | AI判断 | 人工判断 | 采纳 | 纠错理由 | 备注 |")
            lines.append("|------|--------|----------|------|----------|------|")

            for mod in item.get("modules", []):
                adoption = mod.get("adoption", "")
                emoji = ADOPTION_EMOJI.get(adoption, "")
                lines.append(
                    f"| {mod.get('module', '')} "
                    f"| {_format_value(mod.get('ai_judgment'))} "
                    f"| {_format_value(mod.get('human_judgment'))} "
                    f"| {emoji} {adoption} "
                    f"| {mod.get('correction_reason', '')} "
                    f"| {mod.get('notes', '')} |"
                )

            lines.append("")

        lines.append("---\n")

    result = "\n".join(lines)

    if output_path:
        Path(output_path).write_text(result, encoding="utf-8")
        print(f"已写入: {output_path}", file=sys.stderr)

    return result


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="反馈 JSON → Markdown 转换")
    parser.add_argument("input", help="反馈导出的 JSON 文件路径")
    parser.add_argument("-o", "--output", help="输出 Markdown 文件路径（可选，不指定则打印到 stdout）")
    args = parser.parse_args()

    md = convert(args.input, args.output)
    if not args.output:
        print(md)
