# MCP 工具与网关索引

> 生成日期：2026-06-23 ｜ 依据：对 `ad-direction-agent/app/` 全量扫描（工具字面量调用点 + 适配器/客户端 + 配置）
> 用途：跨部门排障时，快速定位「哪个代码文件调了哪个 MCP 工具」「网关/连接池在哪、怎么配」。

---

## 0. 两套独立 MCP 网关（先分清）

| 网关 | 配置项 | 默认地址 | 用途 | 方向 |
|---|---|---|---|---|
| **数据网关**（StarRocks data server / whp advert api） | `mcp_gateway_url` + `mcp_gateway_token` | `.env` 注入 | 查 ASIN/广告活动**数据**（read） | 只读 |
| **广告执行网关**（whp-advert-agent） | `advert_mcp_url` + `advert_mcp_token` | `http://192.168.2.31:5678/mcp` | **真实下发广告调整**（write） | 写 Amazon |

> 两者**复用同一套传输层** `mcp_client.StreamableHttpMcpInvoker`（HTTP JSON-RPC over `POST /mcp` + SSE 解析 + 会话复用），但**连不同地址/token**。
> 数据网关默认走 `DATA_SOURCE=mcp`；广告执行网关默认**关**（`advert_mcp_enabled=False`）。

---

## 1. 调用 MCP 工具的代码文件 → 对应工具

### 1.1 数据网关工具（read）

| 代码文件 | 角色 | 调用的 MCP 工具 |
|---|---|---|
| `app/data/mcp_mapping.py` | **工具注册中心 + 入参构造器**（`META_TO_MCP_TOOLS` / `CAMPAIGN_TOOLS` / `build_tool_args` / `make_date_window`） | ad_product_report, ad_placement_report, ad_keyword_report, ad_search_term_report, flow_keywords, own_keyword_flow, product_sales, listing_basic_info, listing_inventory, direct_competitors, keyword_competitors, keyword_child_asins, **ad_campaign_basic_info / ad_campaign_product_report / ad_campaign_placement_report / ad_campaign_search_term_report** |
| `app/data/mcp_adapter.py` | **ASIN 级适配器**（`McpAdapter`：信号量 + 调用 + 归一化）。方法 `call_tool_timed` / `call_tool_timed_with_args` / `campaign_call_tool` | ad_product_report, ad_placement_report, ad_keyword_report, flow_keywords, own_keyword_flow, product_sales, listing_basic_info, listing_inventory, direct_competitors, keyword_competitors, keyword_child_asins |
| `app/data/campaign_fetcher.py` | **广告活动 + 新增词 编排器** | **ad_campaign_basic_info**（活动预算/状态/上线天数）, **ad_campaign_product_report**（活动花费/点击/订单/曝光）, **ad_campaign_placement_report**（广告位）, **ad_campaign_search_term_report**（搜索词）, flow_keywords, own_keyword_flow, direct_competitors, **seller_sprite_keyword_reverse**（竞品反查，默认关）, keyword_child_asins, **whp_amazon_advert_keyword_suggest_bid**（建议竞价） |
| `app/skills/executors/mcp_query.py` | skill 入口（不直接传工具名，调 `data_aggregator`） | — （间接） |
| `app/core/data_aggregator.py` | 数据源选择（MCP 优先 / Doris 回落），调 McpAdapter | — （经适配器） |
| `app/data/mcp_fetch_run.py` | **MCP 工具并行执行器**（自带 `Semaphore`） | — （执行调度，工具名由调用方传入） |

### 1.2 广告执行网关工具（write，whp-advert-agent）

| 代码文件 | 角色 | 调用的 MCP 工具 |
|---|---|---|
| `app/data/advert_mcp_client.py` | **广告执行客户端**（`AdvertMcpClient`，封装各执行工具） | agent_async_batch_update_advert（异步改已有广告）, agent_batch_update_advert_result（查异步结果）, agent_create_portfolio_campaign（新建组合+活动）, agent_create_negative_keywords（否定词）, agent_query_portfolio_list（查组合）, agent_query_keyword_suggest_bid（建议竞价） |
| `app/workflow/steps/advert_execution.py` | 执行层落地（confirm→真实下发），**调 `AdvertMcpClient` 方法**（不含工具名字面量） | （经 AdvertMcpClient）async_batch_update / create_portfolio_campaign / create_negative_keywords |
| `app/workflow/steps/portfolio_execution.py` | 组合预算调整执行，**调 `AdvertMcpClient`** | （经 AdvertMcpClient）query_portfolio_list / async_batch_update |

---

## 2. MCP 网关 / 连接池 / 基础设施 文件

| 文件 | 说明 |
|---|---|
| **`app/data/mcp_client.py`** | **传输层核心**。`StreamableHttpMcpInvoker`：HTTP JSON-RPC（`POST /mcp`）+ SSE 解析 + **会话复用**（`mcp-session-id`）。**httpx 连接池**在此：`max_connections=mcp_max_connections(120)`、`max_keepalive_connections=40`、`timeout=mcp_timeout(1200s)`。`endpoint=mcp_gateway_url`。含 `unwrap_tool_payload`（递归拆 content→text→JSON 嵌套）、`parse_jsonrpc_body`。**两套网关共用此类**。 |
| **`app/data/mcp_adapter.py`** | **ASIN 级适配层**。`McpAdapter`：**并发信号量** `self._sem = Semaphore(mcp_max_concurrency=115)`（`:91`，`async with self._sem` 包裹每次调用 `:108`）。提供 `call_tool_timed` / `call_tool_timed_with_args` / `campaign_call_tool`，串起「构参→限流→调 client→归一化」。 |
| **`app/data/mcp_mapping.py`** | **工具注册表 + 入参构造**。`META_TO_MCP_TOOLS`（9 META→工具）、`CAMPAIGN_TOOLS`（4 活动级工具）、各 `build_tool_args` arg builder、`make_date_window(days)`（`today-Δdays ~ today` 日期窗口）。 |
| **`app/data/advert_mcp_client.py`** | **广告执行网关客户端**。复用 `StreamableHttpMcpInvoker`，但连 `advert_mcp_url`（`192.168.2.31:5678/mcp`）+ `advert_mcp_token`，`timeout=advert_mcp_timeout(120s)`。 |
| `app/data/mcp_fetch_run.py` | **并行执行器**：`Semaphore(max_concurrency)` 批量并发跑多个 MCP 工具。 |
| `app/data/mcp_db_context.py` | **上下文解析（直连 DB，非 MCP 工具）**：`pymysql` 连 `mcp_db_host`（回落 `db_host`），查 `dwd_whp_amazon_listing_general` 把 `parent_asin → SKU/店铺/站点`。日志里 `MCP 上下文：数据库不可达 host=172.17.0.16:9030` 即此处。 |
| `app/data/mcp_normalizers.py` | MCP 响应归一化（中英文字段别名 `_pick` / `_row_key_index`）。 |
| `app/data/mcp_keyword_report.py` | 关键词报告多 match_type 合并。 |
| `app/data/mcp_empty_reports.py` | MCP 空报告检测 → 触发回落。 |
| `app/data/mcp_tool_fallback.py` | MCP 工具 → META 反向映射（回落用）。 |
| `app/data/doris_fallback.py` / `fallback_merge.py` | MCP 失败/空表 → Doris 回落编排 + 合并。 |

---

## 3. 关键配置项（`app/config/settings.py`）

### 数据网关
| 配置 | 默认 | 含义 |
|---|---|---|
| `mcp_gateway_url` / `mcp_gateway_token` | `.env` 注入 | 数据网关地址 / token |
| `mcp_server_name` | `user-starrocks-data-server` | 服务器名 |
| `mcp_timeout` | 1200.0 | httpx 传输超时 |
| `mcp_tool_timeout` | 1200.0 | 单工具超时 |
| `mcp_context_timeout` | 30.0 | 上下文解析超时 |
| `mcp_max_concurrency` | **115** | 工具并发信号量（adapter `_sem`） |
| `mcp_max_connections` | **120** | httpx 连接池上限（须 ≥ 并发数） |
| `mcp_db_host` | `.env` | 上下文解析直连 DB（回落 `db_host`） |
| `mcp_fallback_to_db` | True | MCP 失败回落 Doris |
| `campaign_mcp_tool_timeout` | 300.0 | 活动级工具超时 |

### 广告执行网关
| 配置 | 默认 | 含义 |
|---|---|---|
| `advert_mcp_url` | `http://192.168.2.31:5678/mcp` | 执行网关地址 |
| `advert_mcp_token` | `.env` 注入 | Bearer token |
| `advert_mcp_timeout` | 120.0 | 单次执行超时 |
| `advert_mcp_enabled` | **False** | 总开关（关则 `/campaign/execute` 直接拒） |
| `advert_exec_dry_run` | True | 空跑（只落记录不真调） |

---

## 4. 工具 → 入参 / 数仓回落表 速查

### 4.1 广告活动级（入参：`shop_account` + `campaign_name` + `start_date` + `end_date`）
| 工具 | 查什么 | Doris 回落 |
|---|---|---|
| `ad_campaign_basic_info` | 预算/状态/上线天数/出价 | `_fetch_campaign_context`（`dwd_amazon_ad_keyword_report` 维度） |
| `ad_campaign_product_report` | **花费/点击/订单/曝光/CTR/CPC/CVR/ACOS/销售额** | `dwd_amazon_ad_product_report_update` |
| `ad_campaign_placement_report` | 广告位拆分 | `dwd_amazon_ad_placement_report_update` |
| `ad_campaign_search_term_report` | 搜索词 | （无稳定回落） |

> 日期窗口 = `make_date_window(days)` → `start = today - days`，`end = today`。例：今天 2026-06-23、days=7 → `2026-06-16 ~ 2026-06-23`。

### 4.2 ASIN 级（入参多为 `parent_asin` + `parent_seller_sku` + `shop_account`，广告报告另加 `start/end_date`）
| 工具 | 查什么 | Doris 回落表 |
|---|---|---|
| `ad_product_report` | ASIN 广告商品报告 | `dwd_amazon_ad_product_report_update` |
| `ad_placement_report` | 广告位 | `dwd_amazon_ad_placement_report_update` |
| `ad_keyword_report` | 关键词广告（⚠ 直调返 "Unknown tool"，**已知非bug**，按 META_KW_AD 回落 Doris） | `dwd_amazon_ad_keyword_report` |
| `ad_search_term_report` | 搜索词 | — |
| `flow_keywords` | 流量词（入参 `site_code`+...） | `dwd_amazon_listing_flow_keyword_us/uk/de` |
| `own_keyword_flow` | 自家词流量 | listing 流量词表 |
| `product_sales` | 销量/趋势（+ `start/end_date`） | `dwd_az_asin_gross_profit` |
| `listing_basic_info` | listing 主数据 | `dwd_whp_amazon_listing_general` + `dwd_whp_az_extend` |
| `listing_inventory` | 库存（`can_sale_num`） | `dwd_az_asin_gross_profit` |
| `direct_competitors` | 直接竞品 | `dwd_az_cw_asin_prod_info` |
| `keyword_competitors` / `keyword_child_asins` | 关键词竞品/子ASIN排名 | `dwd_amazon_asin_keyword_library` |

### 4.3 外部三方（无数仓回落）
| 工具 | 入参 | 来源 |
|---|---|---|
| `seller_sprite_keyword_reverse` | `asin`, `market`, `page`, `page_size` | SellerSprite 反查；返回**嵌套** `data.data.list[]`，关键词字段 `keyword`、搜索量字段 **`searches`**、建议竞价 `bid`（live 核实 2026-06-18） |
| `whp_amazon_advert_keyword_suggest_bid` | `shopAccount`, `parentAsin`, `parentSellerSku`, `keywordVoList` | 亚马逊广告建议竞价 |

### 4.4 广告执行（whp-advert-agent，write）
| 工具 | 用途 |
|---|---|
| `agent_async_batch_update_advert` | 异步批量改已有广告（bid/budget/placement/state）→ 返回 taskId |
| `agent_batch_update_advert_result` | 查异步执行结果 |
| `agent_create_portfolio_campaign` | 新建组合+活动（入参 `shopId/parentAsin/parentSellerSku/createCampaignVo/asin/...`） |
| `agent_create_negative_keywords` | 创建否定词 |
| `agent_query_portfolio_list` | 查组合列表（取 portfolioId） |
| `agent_query_keyword_suggest_bid` | 执行侧建议竞价 |

---

## 5. 调用层次（自上而下）

```
api/campaign.py · workflow/steps/campaign*.py · advert_execution/portfolio_execution
        │
        ├─ 读数据 → data_aggregator → mcp_adapter(McpAdapter, 信号量115)
        │                                   │   campaign_fetcher(活动/新增词)
        │                                   └─ mcp_client(StreamableHttpMcpInvoker, httpx池120)
        │                                          → 数据网关 mcp_gateway_url
        │        (失败/空 → doris_fallback → DbAdapter → Doris)
        │        (上下文 → mcp_db_context 直连 DB)
        │
        └─ 写执行 → advert_mcp_client(AdvertMcpClient)
                         → mcp_client(同传输层) → 执行网关 advert_mcp_url(192.168.2.31:5678)
```

---

*备注：本文档为静态扫描结果（工具字面量 + 适配器调用 + settings）。新增/改动 MCP 工具时，注册入口在 `mcp_mapping.py`（数据网关）或 `advert_mcp_client.py`（执行网关），更新本表。*
