# ERP 数据库脚本（统一目录）

所有连接 **ERP MySQL**（`erp_agentadvert` @ `192.168.2.51`）的运维脚本集中在此。  
写入实现代码在 `app/persistence/erp_writer/`。

默认连接见 `_erp_conn.py`，可用各脚本的 `--host` 等参数覆盖（若支持）。

在 **`ad-direction-agent`** 目录下执行：`python scripts/erp_db/<脚本名>.py ...`

---

## 写入

| 脚本 | 说明 |
|------|------|
| `write_erp_from_jsonl.py` | KB JSONL → `write_dual()`（分析建议 + metrics + legacy 方向） |
| `write_erp_all.py` | KB + Wizard JSONL → `write_full()`（含 decision / 向导表） |
| `run_erp_batch_sequential.py` | 多 ASIN：Campaign 实验 → Wizard → `write_erp_all` |
| `truncate_erp_agent_tables.py` | 清空 18 张 `t_advert_agent_*` 表（**需 `--yes`**） |

## 校验 / 审计

| 脚本 | 说明 |
|------|------|
| `verify_erp_write.py` | 对照 JSONL 预期行数 vs 库内实际（dual 写入后） |
| `verify_erp_full_write.py` | 按 `decision_id` 检查全量表是否齐全 |
| `verify_keyword_columns.py` | 检查 card 表 `keyword` / `keyword_match_type` 语义 |
| `audit_latest_writes.py` | 指定 decision 枚举与数据质量审计 |
| `audit_all_erp_tables.py` | 18 表总行数 + 分 decision 统计 |
| `audit_data_metrics.py` | `data_metrics` 空值与 kb 源数据对照 |
| `db_health_check.py` | 结构健康：孤儿行、枚举、summary 一致性 |

##  schema / 文档

| 脚本 | 说明 |
|------|------|
| `dump_erp_schema.py` | 导出 `t_advert_agent*` 表结构到 stdout |
| `dump_erp_schema_live_full.py` | 结构 + 行数 + 样例 → `cursor临时文件/erp_schema_live_full.json` |
| `gen_erp_db_summary_md.py` | 由上述 JSON 生成 `cursor临时文件/ERP测试库现状说明-最新.md` |

## 辅助

| 脚本 | 说明 |
|------|------|
| `resolve_listing_context.py` | 解析 ASIN 的 `shop_id` / SKU / `site_code`（写库前） |
| `query_erp_empty_asins.py` | 快速查若干 ASIN 的 summary/card 行数 |

## 公共模块

| 文件 | 说明 |
|------|------|
| `_bootstrap.py` | `AD_DIRECTION_AGENT`、`REPO_ROOT`、`bootstrap_sys_path()` |
| `_erp_conn.py` | `ERP_DEFAULT`、`erp_pymysql_conn()` |

## 示例

```powershell
cd ad-direction-agent

python scripts/erp_db/write_erp_all.py --asin B0CGH9QRKK --kb ..\cursor临时文件\B0CGH9QRKK_kb.jsonl --wizard ..\cursor临时文件\B0CGH9QRKK_wizard.jsonl --report ..\cursor临时文件\report.json

python scripts/erp_db/truncate_erp_agent_tables.py --yes

python scripts/erp_db/verify_erp_full_write.py --decision-id dec5da0a62fe024fa952815cd44be083 --expected-cards 56

python scripts/erp_db/db_health_check.py
```

说明文档：[docs/ERP写入说明.md](../../../docs/ERP写入说明.md)
