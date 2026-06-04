"""Generate ERP live DB summary markdown from erp_schema_live_full.json."""
from __future__ import annotations

import json
from pathlib import Path

from _bootstrap import REPO_ROOT

JSON_PATH = REPO_ROOT / "cursor临时文件" / "erp_schema_live_full.json"
OUT_PATH = REPO_ROOT / "cursor临时文件" / "ERP测试库现状说明-最新.md"

CATEGORIES = [
    (
        "一、决策会话根（一次分析 = 一个 decision_id）",
        [
            "t_advert_agent_decision",
            "t_advert_agent_decision_config",
        ],
    ),
    (
        "二、Campaign 分析建议（现代模型：概览 + 卡片 + 待执行明细）",
        [
            "t_advert_agent_modify_suggest_summary",
            "t_advert_agent_modify_suggest_card",
            "t_advert_agent_modify_keyword_pending",
            "t_advert_agent_modify_campaign_pending",
            "t_advert_agent_modify_placement_pending",
        ],
    ),
    (
        "三、数据监控 KPI（折线图 / 周期汇总）",
        ["t_advert_agent_data_metrics"],
    ),
    (
        "四、Wizard 方向与广告目的（策略向导产出）",
        [
            "t_advert_agent_direction_recommend",
            "t_advert_agent_direction_recommend_detail",
            "t_advert_agent_purpose_score",
            "t_advert_agent_core_keyword_tracking",
            "t_advert_agent_ai_suggest",
        ],
    ),
    (
        "五、WHP 执行回写（用户同意后 Amazon API 结果，当前无数据）",
        [
            "t_advert_agent_modify_advert_record",
            "t_advert_agent_modify_keyword_record",
            "t_advert_agent_modify_campaign_record",
            "t_advert_agent_modify_placement_record",
            "t_advert_agent_modify_portfolio_record",
        ],
    ),
]

LATEST = {
    "B0B7S3PWWB": "dec555e98e7cf27ac40ba2f2cd3209b9",
    "B0CGH9QRKK": "dec5da0a62fe024fa952815cd44be083",
}


def fmt_type(col: dict) -> str:
    parts = [col["type"]]
    if col["null"] == "NO":
        parts.append("NOT NULL")
    if col["key"] == "PRI":
        parts.append("PK")
    elif col["key"] == "MUL":
        parts.append("IDX")
    return " ".join(parts)


def fmt_storage(field: str, distincts: list, sample: dict | None) -> str:
    hints = []
    if distincts:
        vals = [str(d.get("v", d.get("v"))) for d in distincts[:8]]
        hints.append(f"库内取值示例: {', '.join(vals)}")
    if sample and field in sample and sample[field] is not None:
        v = sample[field]
        if isinstance(v, str) and len(v) > 80:
            v = v[:77] + "..."
        hints.append(f"B0B7样本: `{v}`")
    return "；".join(hints) if hints else "—"


def main() -> None:
    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    tables = data["tables"]
    lines = [
        "# ERP 测试库现状说明（`erp_agentadvert` 实测）",
        "",
        "> **环境**: `192.168.2.51:3306` / `erp_agentadvert`  ",
        "> **快照**: 库内 `t_advert_agent_*` 共 **18** 张表；下文字段来自 `SHOW FULL COLUMNS` + 行数统计 + 最新两条 decision 样本。  ",
        "> **WHP 绑定 decision**: B0B7 → `dec555e98e7cf27ac40ba2f2cd3209b9`；B0CGH9 → `dec5da0a62fe024fa952815cd44be083`",
        "",
        "## 存储约定（全库通用）",
        "",
        "| 约定 | 说明 |",
        "|------|------|",
        "| 主键 `id` | `char(32)`，Agent 侧多为 `stable_id` 派生的 32 位十六进制字符串 |",
        "| `decision_id` | 同一次分析会话主键，子表通过其关联 `t_advert_agent_decision.id` |",
        "| `shop_id` | `bigint`，店铺 ID（实测 1622） |",
        "| `site_code` | `varchar`，**枚举 code**：`Amazon_US` / `Amazon_DE` / `Amazon_UK`（最新写入为 `Amazon_US`） |",
        "| `parent_asin` / `parent_seller_sku` | 父 ASIN、父 SKU |",
        "| `batch_no` | 实验批次，格式 `{experiment_id}-{run_number:02d}` |",
        "| 枚举字段 | 存 **VARCHAR code**，不存中文展示文案（策略字段已对齐 WHP 枚举文档） |",
        "| 多值字段 | `advert_purposes`、`target_keyword_types`、`advert_direction_types` 存 **JSON 数组字符串**，如 `[\"CONVERSION\",\"RANKING\"]` |",
        "| `confirm_status` / `execute_status` | Agent 写入阶段固定 `PENDING`；执行后 WHP 回写 record 表 |",
        "| 时间 | `create_time` / `update_time`，`datetime` |",
        "",
        "## 全表一览（当前行数）",
        "",
        "| 表名 | 行数 | 用途摘要 |",
        "|------|------|----------|",
    ]
    purpose_short = {
        "t_advert_agent_decision": "决策根",
        "t_advert_agent_decision_config": "决策配置快照",
        "t_advert_agent_modify_suggest_summary": "建议概览汇总",
        "t_advert_agent_modify_suggest_card": "建议卡片",
        "t_advert_agent_modify_keyword_pending": "关键词待改",
        "t_advert_agent_modify_campaign_pending": "活动预算待改",
        "t_advert_agent_modify_placement_pending": "广告位待改",
        "t_advert_agent_data_metrics": "KPI 日趋势+汇总",
        "t_advert_agent_direction_recommend": "方向推荐主表",
        "t_advert_agent_direction_recommend_detail": "四方向明细",
        "t_advert_agent_purpose_score": "四广告目的评分",
        "t_advert_agent_core_keyword_tracking": "核心词跟踪",
        "t_advert_agent_ai_suggest": "P3 AI 建议",
    }
    for name in sorted(tables.keys()):
        t = tables[name]
        short = purpose_short.get(name, (t.get("comment") or "")[:30])
        lines.append(f"| `{name}` | {t['actual_row_count']} | {short} |")

    lines.extend(["", "---", ""])

    for cat_title, table_names in CATEGORIES:
        lines.append(f"## {cat_title}")
        lines.append("")
        for name in table_names:
            if name not in tables:
                continue
            t = tables[name]
            lines.append(f"### `{name}`")
            lines.append("")
            lines.append(f"- **表注释**: {t.get('comment') or '—'}")
            lines.append(f"- **当前总行数**: {t['actual_row_count']}")
            stats = t.get("latest_decision_stats") or {}
            if stats:
                parts = []
                for asin, st in stats.items():
                    if "decision_id" in st:
                        parts.append(f"{asin}: {st['rows']} 行 (decision `{st['decision_id'][:16]}…`)")
                    else:
                        parts.append(f"{asin}: {st.get('rows_for_asin', st.get('rows', '?'))} 行")
                lines.append(f"- **最新 decision 行数**: {'；'.join(parts)}")
            lines.append("")
            lines.append("| 字段 | 类型 | 说明/注释 | 存储与实测 |")
            lines.append("|------|------|-----------|------------|")
            sample = t.get("sample_row_b0b7")
            distincts_all = t.get("distinct_values") or {}
            for col in t["columns"]:
                f = col["field"]
                comment = col.get("comment") or ""
                storage = fmt_storage(f, distincts_all.get(f, []), sample)
                lines.append(f"| `{f}` | {fmt_type(col)} | {comment} | {storage} |")
            lines.append("")

    lines.extend(
        [
            "## 六、最新两条 decision 数据要点",
            "",
            "### B0B7S3PWWB (`dec555e98e7cf27ac40ba2f2cd3209b9`)",
            "",
            "| 维度 | 值 |",
            "|------|-----|",
            "| shop_id / site_code | 1622 / `Amazon_US` |",
            "| 策略 | `WAIST` / `PROMOTING` / `PEAK_SEASON_PREPARE` / `DAY_7` |",
            "| advert_purposes | `[\"CONVERSION\", \"RANKING\"]` |",
            "| target_keyword_types | `[\"大词\", \"长尾词\"]`（暂无 WHP code） |",
            "| card | 105；keyword_pending 105；campaign_pending 105；placement_pending 75 |",
            "| data_metrics | 1 SUMMARY + 5 DAILY |",
            "| purpose_score | 4 行（TRAFFIC/CONVERSION/RANKING/PROFIT） |",
            "",
            "### B0CGH9QRKK (`dec5da0a62fe024fa952815cd44be083`)",
            "",
            "| 维度 | 值 |",
            "|------|-----|",
            "| shop_id / site_code | 1622 / `Amazon_US` |",
            "| advert_purposes | `[\"CONVERSION\"]` |",
            "| card | 56（kb 63 条 adjustment，7 组主键碰撞少写） |",
            "| data_metrics | 1 SUMMARY + 5 DAILY |",
            "",
            "## 七、空表与历史数据说明",
            "",
            "- **5 张 `*_record` 表行数为 0**：仅 WHP 用户确认执行后写入，Agent 分析阶段正常为空。",
            "- **同 ASIN 可有多条历史 `decision`**：展示层应只绑 §零 最新 `decision_id`。",
            "- **`decision_config` 按 `parent_asin` 可累积多行**（当前库 4 行，覆盖 2 个 ASIN 多次实验）。",
            "",
            "---",
            "",
            "*由 `scripts/erp_db/dump_erp_schema_live_full.py` + `scripts/erp_db/gen_erp_db_summary_md.py` 自动生成。*",
        ]
    )

    OUT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print("written", OUT_PATH)


if __name__ == "__main__":
    main()
