# 超时根因定位（2026-05-26）

## 结论摘要

| 层级 | 是否确认为本次根因 | 说明 |
|------|-------------------|------|
| **前端 Demo 90s** | 是（用户可见「请求超时」） | `callAPI` 默认 90s，战术/战略接口未加大 |
| **MCP 单工具超时** | 是（部分 ASIN/工具） | 如 `ad_keyword_report` 对 B0DW495X3M 打满 120s |
| **Doris 整场景回落 300s** | 是（拖长总耗时） | `MCP_FAILOVER_FULL_DB=true`，任一 MCP 失败即拉全 meta |
| **Doris ad_summary 1064** | 是（数据不全） | StarRocks BE HDFS，非应用超时 |
| **ad_summary budget SQL** | 是（代码 bug） | `budget_params` 多传 `shop_id` |
| MCP 网关整体不可用 | 否 | 同环境 `listing_inventory` 可 2–13s 返回 |

## 实测（`scripts/diagnose_timeout_layers.py`）

### ASIN B0GCZHVBWN（探测时网关正常）

- context: 0.13s
- listing_inventory: **2.13s** OK
- ad_keyword_report: **1.72s** OK

### ASIN B0DW495X3M（与用户截图一致）

- context: 0.12s
- listing_inventory: **13.54s** OK
- ad_keyword_report: **120.00s FAIL** `timeout after 120.0s`

→ **超时原因 1：MCP 网关对 `ad_keyword_report` 在该 ASIN 上超过 120s 无响应。**

## 终端日志（进程 9832）

- 无 `MCP tool fail` 行（uvicorn 默认未打出 app logger INFO）
- 有 Doris 慢查询：竞品 13s、排名 30s、flow step_b 28s
- 重复：`ad_summary metrics` **1064**；`ad_summary budget` **参数格式化错误**

→ **超时原因 2：MCP 失败或跳过后，Doris 回落并行查数仓，累加 1–3 分钟。**

→ **超时原因 3：非超时但导致「无广告数据」— 1064 + budget bug。**

## 墙钟估算（战术层 tactics）

- MCP 最坏（8 工具、并发 4）：约 **360s**
- MCP 失败后 Doris 回落：+**300s**
- 合计最坏 **660s** >> 前端 **90s**

## 战略层误导

`strategy/options` 立即 200，但 `_preload_data` 后台 **全量** `fetch`（无 meta_filter），会提前触发 MCP + Doris，日志里的慢 SQL 常来自预加载而非当前点击的接口。

## 建议修复顺序

1. 前端：`tactics/options`、`diagnosis` 的 `callAPI` 调至 240–300s
2. MCP：排查网关 `ad_keyword_report` 慢 SQL；或单工具限时外增加异步
3. 回落：`MCP_FAILOVER_FULL_DB=false`，只补失败维度 meta
4. 代码：修复 `db_adapter._fetch_ad_summary` 的 `budget_params`
5. 运维：StarRocks 1064 HDFS cache

复测：`python scripts/diagnose_timeout_layers.py <ASIN>`
