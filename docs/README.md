# 文档索引

> 仓库：`AD_assistant_agent-v3.0.2` · 主代码在 `ad-direction-agent/`

## ERP 与 WHP 对接

| 文档 | 说明 |
|------|------|
| **[WHP-前端对接完整说明.md](WHP-前端对接完整说明.md)** | **发给 WHP 前端的一份总文档**（Tab4 绑定 + 全部枚举） |
| [ERP写入说明.md](ERP写入说明.md) | Agent → ERP 写入、`write_full`、在线 `campaign/analyze` 自动写库 |
| [ERP数据库设计问题清单.md](ERP数据库设计问题清单.md) | ERP 表结构与设计遗留问题 |
| [ERP测试库现状说明.md](ERP测试库现状说明.md) | 测试库数据现状 |
| [ERP方向枚举对照.md](ERP方向枚举对照.md) | （已并入完整说明）枚举 |
| [WHP-Tab4-方向推荐字段契约.md](WHP-Tab4-方向推荐字段契约.md) | （已并入完整说明）字段契约 |
| [WHP-Tab4-对接说明（发给WHP）.md](WHP-Tab4-对接说明（发给WHP）.md) | （已并入完整说明）Tab4 对接 |
| [../ERP数据库接入文档1.md](../ERP数据库接入文档1.md) | pending 基础表 |
| [../ERP数据库接入文档2_campaign.md](../ERP数据库接入文档2_campaign.md) | 分析建议 Tab |
| [../交接文档.md](../交接文档.md) | 数仓、规则、部署交接 |

## 运维与排障

| 文档 | 说明 |
|------|------|
| [diagnose-timeout-root-cause.md](diagnose-timeout-root-cause.md) | MCP/Doris 超时根因与调参 |
| [sql/warehouse_index_recommendations.md](sql/warehouse_index_recommendations.md) | 数仓索引建议 |

## LLM 知识库（规则正文）

目录：[knowledge_base/](knowledge_base/) · 索引：[knowledge_base/知识库目录索引.md](knowledge_base/知识库目录索引.md)

执行规则（Campaign 等）：`knowledge_base/执行规则/`（含 15–22 等）

## 版本公告

[announcements/](announcements/)：`v2.2` … `v2.6`

## 脚本与代码索引

| 位置 | 说明 |
|------|------|
| `ad-direction-agent/scripts/erp_db/README.md` | ERP 写入/校验脚本一览 |
| `ad-direction-agent/app/persistence/erp_writer/` | 写入实现（含 `auto_push.py`） |
