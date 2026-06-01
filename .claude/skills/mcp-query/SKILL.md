---
name: mcp-query
description: Query Amazon listing and ad data via user-starrocks-data-server MCP tools with per-tool timeout and Doris fallback. Use when fetching ASIN metrics, debugging MCP 1064/timeout, or extending phased_fetcher / mcp_mapping.
---

# MCP 数据查询 Skill

> **运行时配置（真相源）**：[`playbook.yaml`](playbook.yaml)  
> 应用启动时由 `app/skills/loader.py` 加载，由 `app/skills/executors/mcp_query.py` 执行。  
> 超时、并发、回落规则请改 playbook 或对应 `.env` 变量（`${MCP_*}` / `${DB_*}` 占位符）。

## 何时用 MCP vs Doris

| 场景 | 路径 |
|------|------|
| 解析 `parent_seller_sku` / `shop_account` | **Doris** `mcp_db_context.resolve_mcp_context_from_db` |
| 业务报表（广告、关键词、趋势） | **MCP 工具** → 单工具超时 → **Doris 回落** |
| 用户点「刷新」/ `prefer_db=true` | **仅 Doris** `DbAdapter` |
| 短期重复访问同一 ASIN | **Redis** `asin_data:{asin}:{days}` TTL 2h |

## 分阶段抓数（playbook phases）

| Phase | 说明 | 超时 env |
|-------|------|----------|
| `context` | Doris 解析 `McpContext` | `MCP_CONTEXT_TIMEOUT` |
| `mcp_bootstrap` | `listing_basic_info`, `listing_inventory` | `MCP_BOOTSTRAP_TIMEOUT` |
| `mcp_reports` | 按场景 `meta_filter` 展开 MCP 工具 | `MCP_TOOL_TIMEOUT` |
| `fallback` | MCP 失败 → Doris（可选全场景 meta） | `DB_FAILOVER_TIMEOUT` |

日志前缀：`[skill:mcp-query] phase=... tool=...`

## 工具清单与 META 映射

| MCP 工具 | 用途 | 回落 META |
|----------|------|-----------|
| `listing_basic_info` | Listing 基础 | bootstrap |
| `listing_inventory` | 库存 | signals（合并） |
| `ad_product_report` | 广告汇总 | META_AD_PRODUCT |
| `ad_placement_report` | 广告位 | META_AD_PLACEMENT |
| `ad_keyword_report` | 关键词报告 | META_KW_AD |
| `keyword_competitors` | 竞品词排名 | META_KW_COMPETITOR_RANK |
| `keyword_child_asins` | 子 ASIN 排名 | META_KW_COMPETITOR_RANK |
| `flow_keywords` | 流量词 | META_FLOW_KEYWORD |
| `product_sales` | 销售/趋势 | META_TREND |
| `direct_competitors` | 直接竞品 | META_COMPETITOR |

完整参数见 `app/data/mcp_mapping.py` → `TOOL_ARG_BUILDERS`。

## McpContext 构造

```python
McpContext(
    parent_asin=...,
    parent_seller_sku=...,  # 来自 Doris listing
    shop_account=...,       # dwd_shop.account
    site_code="Amazon_US",
    start_date="YYYY-MM-DD",
    end_date="YYYY-MM-DD",
)
```

环境变量：`MCP_DEFAULT_SHOP_ACCOUNT` 仅作优先过滤；无匹配时会不限店铺再查。

## 调用 MCP（Streamable HTTP）

- 网关：`MCP_GATEWAY_URL` + `MCP_GATEWAY_TOKEN`（header `X-Api-Key`）
- 实现：`app/data/mcp_client.py` — `initialize` → `tools/call`
- **不要**使用 `POST /mcp/tools/{name}`（旧 REST，404）

## 超时与 partial_failures

- 单工具超时：见 playbook `mcp_bootstrap` / `mcp_reports` 的 `timeout_seconds`
- 失败示例：`partial_failures: ["mcp:flow_keywords:timeout after 120s"]`
- Doris 补全成功后，对应 MCP 项会从黄条中剔除
- API：`data_freshness` = `fresh` | `partial`

## 环境变量

| 变量 | 作用 |
|------|------|
| `SKILLS_ENABLED` | 是否走 Skill 运行时（默认 true） |
| `SKILLS_DIR` | playbook 根目录，默认 `../.claude/skills` |
| `DATA_SOURCE` | `mcp` 走本 Skill；`db` 跳过 MCP |
| `MCP_FAILOVER_FULL_DB` | MCP 失败时是否按整场景 meta 回落 |

## 排错

| 现象 | 处理 |
|------|------|
| `1064 hdfs cache directory` | StarRocks BE 运维；应用层对该维度走 Doris 回落 |
| `partial_failures` 有 MCP 超时 | 调大 `MCP_TOOL_TIMEOUT` 或查网关慢查询 |
| `context_error` | Doris 不可达或 ASIN 无 listing |
| Redis 不可用 | 自动降级 30s 进程内 LRU |

## 相关文件

- `playbook.yaml` — **运行时编排配置**
- `app/skills/registry.py` — Skill 注册与调度
- `app/skills/executors/mcp_query.py` — 执行器
- `app/data/phased_fetcher.py` — 薄封装，委托 `mcp-query`
- `app/data/fallback_merge.py` — Doris 回落合并
- `app/data/mcp_tool_fallback.py` — 工具→META
- `app/persistence/redis_cache.py` — 短期缓存
