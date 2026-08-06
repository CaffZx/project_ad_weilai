# MCP工具原始Schema说明

## 目的

本篇记录 AD-Agent 当前使用的 MCP 工具、项目侧入参构造、返回消费点和功能边界。

注意：本地代码并不保存 MCP 网关完整原始 schema。本篇的“schema”指项目侧可验证的参数结构和返回字段消费形态；若后续能从 MCP 网关导出原始 schema，应追加为附录并标明导出命令。

## 当前代码真源

- 数据 MCP 映射：`ad-direction-agent/app/data/mcp_mapping.py`
- 数据 MCP 调用：`ad-direction-agent/app/data/mcp_adapter.py`
- 分阶段调度：`ad-direction-agent/app/data/mcp_fetch_run.py`
- 关键词报表扩展：`ad-direction-agent/app/data/mcp_keyword_report.py`
- 返回标准化：`ad-direction-agent/app/data/mcp_normalizers.py`
- Campaign fetcher：`ad-direction-agent/app/data/campaign_fetcher.py`
- 执行 MCP 客户端：`ad-direction-agent/app/data/advert_mcp_client.py`
- 执行参数映射：`ad-direction-agent/app/persistence/erp_writer/advert_exec_mapper.py`

## 代码锚点地图

| 职责 | 代码位置 | 说明 |
| --- | --- | --- |
| MCP 上下文对象 | `app/data/mcp_mapping.py:18` `McpContext` | parent_asin、site_code、shop_account、parent_seller_sku 等 |
| 日期窗口 | `mcp_mapping.py:52` `make_date_window()` | 按站点时区生成 start/end，排除未完整当天 |
| meta 到工具映射 | `mcp_mapping.py:94` `META_TO_MCP_TOOLS` | 前置 ASINData 按 meta_filter 选工具 |
| bootstrap 工具选择 | `mcp_mapping.py:110` `bootstrap_tools_for_meta()` | campaign/keyword 场景才加入 `ad_campaign_product_keyword_list` |
| 普通工具入参 | `mcp_mapping.py:192` `build_tool_args()` | 数据 MCP 工具参数构造 |
| Campaign 工具入参 | `mcp_mapping.py:212` `build_campaign_tool_args()` | campaign_id/name、shop_account、日期窗口等 |
| MCP invoker | `app/data/mcp_client.py:90` `StreamableHttpMcpInvoker` | Streamable HTTP JSON-RPC client |
| MCP 适配器 | `app/data/mcp_adapter.py:120` `McpAdapter` | 信号量、重试、标准化、ASINData 装配 |
| 分阶段并发 | `app/data/mcp_fetch_run.py:16` `run_planned_mcp_tools()` | bootstrap/report timeout 分层 |
| Campaign fetcher | `app/data/campaign_fetcher.py:45` `fetch_campaigns()` | CampaignData 构造 |
| Advert 执行 mapper | `erp_writer/advert_exec_mapper.py:82` `build_exec_plan()` | pending 到 Advert MCP payload |

## 项目侧调用形态

MCP 工具在项目里有三种调用形态：

| 调用形态 | 入口 | 参数来源 | 返回消费 |
| --- | --- | --- | --- |
| ASINData 普通工具 | `McpAdapter.fetch_asin_data()` | `build_tool_args(tool, McpContext)` | `mcp_normalizers.py` + `mcp_adapter.py` 组装 ASINData |
| Campaign 级工具 | `CampaignFetcher` / `McpAdapter.campaign_call_tool()` | `build_campaign_tool_args()` 或 fetcher 手工 args | `CampaignUnit`、placement/search term、portfolio |
| Advert 执行工具 | `advert_exec_mapper.build_exec_plan()` + `AdvertMcpClient` | ERP pending + card + decision 上下文 | MCP 下发结果、task id、pending execute_status |

返回结构边界：

- 数据 MCP 的返回可能在 `structuredContent`、`content[].text` 或工具包装 envelope 里，先由 `mcp_client.unwrap_tool_payload()` 和 adapter 解包。
- 项目文档应记录“代码实际消费的字段”，不要假设网关所有字段稳定可用。
- 执行 MCP 返回只作为执行结果判断和 task id 提取来源，不应反向改写分析建议本身。

## 数据 MCP 工具

### `parent_listing_detail`

功能：父 ASIN 解析站点、店铺、父 SKU、产品名等上下文。

项目侧入参：

```json
{
  "parent_asin": "B0..."
}
```

返回消费：`mcp_db_context.py`、`mcp_adapter.py` 用于构建 `McpContext`。

### `listing_basic_info_v2`

功能：父 ASIN listing 基础信息。

项目侧入参：

```json
{
  "parent_asin": "B0...",
  "shop_account": "...",
  "site_code": "Amazon_US"
}
```

返回消费：`mcp_normalizers.py` 标准化到产品基础信息字段。

### `parent_listing_stock_summary`

功能：父 ASIN 库存汇总。

项目侧入参由 `McpContext` 构造，通常包括父 ASIN、店铺、站点或 SKU 信息。

返回消费：ASINData 库存和可售性相关字段。

### `product_sales`

功能：父 ASIN 销售数据。

项目侧入参包括父 ASIN、站点、店铺、日期窗口。

返回消费：ASINData 销售表现与趋势。

### `ad_product_report`

功能：父 ASIN 广告商品报表。

项目侧入参包括父 ASIN、店铺、站点、`start_date`、`end_date`。

返回消费：ASINData 广告指标。

### `ad_placement_report`

功能：placement 粒度广告报表。

项目侧入参包括父 ASIN、店铺、站点、日期窗口。

返回消费：ASINData placement 相关指标；Campaign 链路另有 Campaign 级 placement 工具。

### `ad_search_term_report`

功能：搜索词广告报表。

项目侧入参包括父 ASIN、店铺、站点、日期窗口。

返回消费：搜索词诊断、新建活动候选、Campaign 精细分析。

### `flow_keywords`

功能：流量词库。

项目侧入参包括父 ASIN、站点和分页/数量控制参数。

返回消费：关键词机会、前置策略和新建 Campaign 候选。

### `own_keyword_flow`

功能：自有关键词自然流量/排名信息。

项目侧入参包括关键词、父 ASIN、站点等上下文。

返回消费：自然排名旁路、Campaign 新增候选排序。

### `direct_competitors`

功能：直接竞品列表。

项目侧入参包括父 ASIN、站点、店铺上下文。

返回消费：竞品诊断和可选竞品词源。

### `keyword_competitors`

功能：关键词维度竞品。

项目侧有 builder，但是否在当前 meta 下主动调用取决于 `META_TO_MCP_TOOLS`。

### `keyword_child_asins`

功能：关键词下子 ASIN 排名/曝光信息。

调用方式：不走普通 meta 工具空跑，而是由 `mcp_adapter.py` Phase 2 按词并行调用。

### `ad_campaign_product_keyword_list`

功能：按父 ASIN/SKU 发现 Campaign、商品和关键词关系。

项目侧入参：

```json
{
  "parent_asin": "B0...",
  "parent_seller_sku": "...",
  "shop_account": "...",
  "site_code": "Amazon_US"
}
```

返回消费：Campaign discovery、精准关键词提取、Campaign 前置上下文。

## Campaign MCP 工具

### `ad_campaign_list`

功能：父 ASIN 下在线广告活动发现。

项目侧入参：父 ASIN、父 SKU、店铺账号。

### `ad_campaign_basic_info_v2`

功能：按 `campaign_id_list` 批量查询活动基础信息。

项目侧入参：

```json
{
  "shop_account": "...",
  "campaign_id_list": "123,456"
}
```

当前优先使用 v2。v1 `ad_campaign_basic_info` 保留兼容，使用 `campaign_name_list`。

### `ad_campaign_product_report`

功能：Campaign 商品效果报表。

项目侧入参：店铺、活动名或活动 ID 上下文、日期窗口。

### `ad_campaign_placement_report`

功能：Campaign placement 报表，通常在 LLM 精细分析前懒加载。

### `ad_campaign_search_term_report`

功能：Campaign 搜索词报表，通常在 LLM 精细分析前按活动懒加载。MCP 返回原始搜索词行；当前 Campaign 调用方在活动级样本门禁通过后并行请求 7d/14d，不请求 30d。

当前消费约束（由 `campaign_fetcher.py` 实现，不是 MCP schema 本身的字段约束）：以 7d 结果生成候选集合，删除完全无信号词，按 7d 订单数、花费、点击数、曝光数和归一化词排序，单活动最多透传 20 条；仅 7d 词级样本不足的候选按归一化词键补入 14d 指标。渲染给 LLM 时必须标注窗口，7d 是动作基线，14d 是辅助观察。

### `ad_portfolio_list`

功能：组合列表，用于预算重分配、组合展示和执行前 portfolio 匹配。

## Advert 执行 MCP 工具

### `agent_async_batch_update_advert`

功能：批量修改已有广告活动、关键词、placement。

项目侧入参核心结构：

```json
{
  "paramsVoList": [
    {
      "parentAsin": "B0...",
      "parentSku": "...",
      "shopId": "...",
      "currentUserId": "...",
      "portfolioId": "...",
      "campaignVoList": [
        {
          "campaignId": "...",
          "campaignBudget": 10.0,
          "campaignState": "enabled",
          "placementTopPercent": 50,
          "keywordShowVoList": [
            {
              "keywordId": "...",
              "keyword": "...",
              "keywordBid": 0.5,
              "keywordState": "paused"
            }
          ]
        }
      ]
    }
  ]
}
```

返回消费：`parse_result_envelope()` 判断成功失败，`extract_task_ids()` 提取异步任务 ID。

### `agent_create_portfolio_campaign`

功能：新建组合下广告活动，或在已有组合下新建活动。

项目侧入参由每张 CREATE card 生成，核心结构包括 `createCampaignVo`、`portfolioId`、关键词列表、placement 设置、预算和 operator。

### `agent_create_negative_keywords`

功能：创建否定词。

项目侧入参按 campaign 聚合：

```json
{
  "keywordType": "negative",
  "campaignVoList": [
    {
      "campaignId": "...",
      "keywordVoList": [
        {
          "keyword": "...",
          "matchType": "NEGATIVE_EXACT"
        }
      ]
    }
  ]
}
```

### `agent_query_portfolio_list`

功能：执行前查询 portfolio 列表，用于新建活动或挪组时匹配组合。

项目侧入参包括父 ASIN、父 SKU、店铺、可选组合名模糊查询。

### `agent_query_keyword_suggest_bid`

功能：查询关键词建议竞价。

项目侧入参：

```json
{
  "parentAsin": "B0...",
  "parentSku": "...",
  "shopId": "...",
  "keywordVoList": [
    {"keyword": "example"}
  ]
}
```

## Azlisting MCP 工具

独立 MCP 服务（`azlisting_mcp_url`），与 StarRocks 数据 MCP 分开配置。由 `mcp_registry.py` 统一管理连接池和并发阀。

| 工具名 | 用途 | 调用点 |
|--------|------|--------|
| `erp_listing_product_info` | 父 ASIN 下所有子体变体的五点、颜色、尺码、类目、搜索词 | `mcp_registry.py` `erp_listing_product_info()` → `campaign.py` `_run_new_campaigns_if_enabled` |
| `erp_listing_asin_keyword_rank_history` | ASIN 关键词排名历史 | 核心词离线批跑 |

**限流**：azlisting 全局 5req/10s，`settings.azlisting_mcp_max_in_flight` 控制进程内并发。

**`erp_listing_product_info` 返回结构**：

```json
{
  "data": [
    {
      "asin": "B0CQ562G5C",
      "productColor": "Black5",
      "productSize": "One Size",
      "productPrice": 11.99,
      "variationThemeName": "SIZE/COLOR",
      "productName": "Buauty 3 pcs black fishnet stockings...",
      "fiveBulletPoint1": "High-Waisted Elegance--...",
      "fiveBulletPoint5": "Suitable for a variety of occasions--...",
      "lastCategory": "[{\"title\":\"Women's Exotic Hosiery\"}]",
      "genericKeyword": "fishnet stockings for women..."
    }
  ]
}
```

- 父级字段（`productName`/`fiveBulletPoint1-5`/`lastCategory`/`genericKeyword`）每行相同，取第一行即可
- 子体字段（`asin`/`productColor`/`productSize`/`productPrice`）每行不同，遍历收集
- `call_tool` 返回已被 `unwrap_tool_payload` 解包为 `{"data": [...]}`，用 `_as_rows()` 提取

## 常见误区

- `listing_inventory` 不是当前 `mcp_mapping.py` 中的库存工具名，当前使用 `parent_listing_stock_summary`。
- `ad_campaign_basic_info_v2` 按 campaign ID 批量查询，不应继续按活动名拼 v1 参数。
- `keyword_child_asins` 是 Phase 2 逐词调用，不要把它简单加入普通 meta 工具列表。
- Advert 执行 MCP 和数据 MCP 是两组不同工具，配置项、超时和安全开关不同。

## 更新检查清单

- 修改 `mcp_mapping.py` 后，同步更新本篇数据 MCP 和 Campaign MCP 表述。
- 修改 `advert_mcp_client.py` 或 `advert_exec_mapper.py` 后，同步更新 Advert MCP schema。
- 从 MCP 网关导出原始 schema 后，应追加“网关原始 schema 附录”。
- 工具废弃时不要只删除说明，应记录替代工具和代码调用点。
