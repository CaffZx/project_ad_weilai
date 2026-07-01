# MCP 工具调用链路：定时分析 → ERP 落库

## 概述

定时批跑（cron → `batch_via_api.py` → `/campaign/viewmodel`）到 ERP 落库全链路涉及 **16 个 MCP 工具**，分 6 个阶段，含大量并行调用。

工具调用方分为三类：
- **Skill 执行器** (`mcp_query.py`)：主数据拉取，通过 `run_planned_mcp_tools` 并行
- **Campaign 编排器** (`campaign_fetcher.py`)：活动发现 + 效果拉取 + 新词发现
- **复评/CPC** (`campaign.py`)：淘汰活动复评所需的窗口订单和 CPC 数据

---

## 阶段 0：ASIN 主数据拉取

**入口**：`mcp_adapter.fetch_asin_data()`  
**并发行**：`run_planned_mcp_tools` 通过 `asyncio.gather` + `Semaphore(115)` 控制

### 0a. `parent_listing_detail`（串行，最先）

| 项目 | 内容 |
|---|---|
| **调用位置** | `mcp_db_context.py:54` `resolve_mcp_context_from_mcp` |
| **调用目的** | 解析父 ASIN→店铺/sku/站点/产品名，**是所有下游 MCP 工具的入参地基** |
| **入参** | `{"parent_asin": "B0XXX"}` — 来自 API 请求的 `asin` 字段 |
| **arg builder** | `mcp_mapping.py:100-102` |
| **返回结构** | `{父ASIN, 父卖家SKU, 店铺ID, 店铺账号, 站点, 产品中文名, 产品名称}` → 映射为 `McpDbContext` |
| **下游消费** | 构造 `McpContext`（含 start_date/end_date），注入 0b–0d 所有工具的入参；site_code 决定日期窗口时区 |

### 0b. `listing_basic_info`（串行，紧跟 0a）

| 项目 | 内容 |
|---|---|
| **调用位置** | `mcp_adapter.py:184` |
| **调用目的** | 获取星级、价格等基础信息 |
| **入参** | `{"shop_account", "parent_asin", "parent_seller_sku"}` — 来自 0a 返回 |
| **arg builder** | `mcp_mapping.py:103-107` |
| **返回结构** | `{seller_sku, product_price, star_level, comment_num, brand, category_name, refund_rate}`（经 `normalize_listing_basic_info`） |
| **下游消费** | 总览分析消费以上内容 |

### 0c. Phase 1 — Bootstrap + 报告工具（全部并行）

**调用位置**：`mcp_fetch_run.py:16` `run_planned_mcp_tools`  
**超时**：bootstrap 180s / 报告 1200s

| 工具 | 来源 | 入参（均来自 0a+0b 构造的 McpContext） | 返回（经 normalizer） | 下游消费 |
|---|---|---|---|---|
| 0c-1 `listing_basic_info` | BOOTSTRAP_TOOLS | `{shop_account, parent_asin, parent_seller_sku}` | 同 0b | `assemble_from_payloads` → `data.sku` |
| 0c-2 `listing_inventory` | BOOTSTRAP_TOOLS | `{parent_asin, parent_seller_sku, shop_account}` | `{FBA可售}`（库存量） | `data.signals.inventory_qty`（跨子 ASIN 汇总） |
| 0c-3 `ad_campaign_product_keyword_list` | BOOTSTRAP_TOOLS | `{parent_asin, parent_seller_sku, shop_account}` | `[{广告活动名称, 关键词, 关键词匹配类型, 子ASIN, ...}]` | ① `_extract_campaign_exact_keywords` 提取 EXACT 关键词 → 供 Phase 2 逐词查排名 ② 当 `ad_keyword_report` 空时回退为关键词列表 |
| 0c-4 `ad_product_report` | META_AD_PRODUCT | `{parent_asin, parent_seller_sku, shop_account, start_date, end_date}` | `{cost, sale, clicks, impressions, orders, acos, cpc, ctr, cvr, campaign_budget}` | `data.ad_data`（AdData 模型） |
| 0c-5 `product_sales` | META_TREND | 同上 `_ad_common` | ① `{total_sales, total_orders, margin}` ② `[{date, orders, ad_sales, spend, ...}]`（日趋势） | `data.avg_daily_sales_30d`, `data.margin`, `data.trend`（ECharts 折线图） |

### 0d. Phase 2 — 逐词自然排名（0c-3 完成后，并行）

| 工具 | 调用方式 | 入参 | 返回 | 下游消费 |
|---|---|---|---|---|
| `keyword_child_asins` × N（N≤50） | `asyncio.gather` 全并行，每词超时 45s | `{"keyword": "具体词", "site_code", "parent_asin", ...}` — 关键词来自 0c-3 的 EXACT 词列表 | `[{keyword, craw_nature_rank, near_craw_nature_rank}]` | 合并入 `data.keywords`，设置 `natural_rank`, `near_natural_rank` |

---

## 阶段 1：Campaign 活动发现 + 效果拉取

**入口**：`campaign_fetcher.fetch_campaigns()`  
**触发**：`_analyze_campaigns_impl` step 1 (`campaign.py:331`)

### 1a. `parent_listing_detail`（条件串行）

- 仅当 **无 URL override**（定时批跑无 override，必然走此）时调用
- 同 0a，解析站点/sku/店铺 → 缓存到 `self._last_shop_account`, `_last_site_code` 供懒加载复用

### 1b. `ad_campaign_product_keyword_list`（串行，紧跟 1a）

| 项目 | 内容 |
|---|---|
| **调用位置** | `campaign_fetcher.py:223` `_discover_context_from_mcp` |
| **调用目的** | **替代原两条 SQL（已删）**（`_resolve_and_fetch_listing` + `_fetch_campaign_context`），发现父 ASIN 下所有子 ASIN 的活跃广告活动+关键词 |
| **入参** | `{"parent_asin", "parent_seller_sku", "shop_account"}` — 来自 1a 或缓存 |
| **返回结构** | `[{campaign_id, campaign_name, keyword_id, child_asin, seller_sku, keyword_text, match_type}, ...]`（经 `_normalize_mcp_campaign_keywords` 中→英 key 映射） |
| **下游消费** | ① `filter_campaigns` 硬过滤 → surviving + excluded ② surviving 的 `names` 列表作为 1c 的遍历集 |

### 1c. 每活动配对拉取（全部并行）

**模式**：对每个活动名，`asyncio.gather(basic_info, product_report)` 成对并行；所有活动对再外层 `gather` 全并行。

#### 1c-a. `ad_campaign_basic_info` × N

| 项目 | 内容 |
|---|---|
| **调用位置** | `campaign_fetcher.py:264` `_fetch_basic_one` |
| **入参** | `{"shop_account", "campaign_name"}` — campaign_name 来自 1b 的 names 列表 |
| **arg builder** | `mcp_mapping.py:182-208` `build_campaign_tool_args` |
| **返回结构** | 取首行 `{campaign_budget, keyword_bid, campaign_status, days_online, tos_bid_pct, pp_bid_pct, ros_bid_pct}` |
| **下游消费** | `_assemble` → `CampaignUnit` 的 `current_bid`, `current_budget`, `days_online`, 广告位系数 |

#### 1c-b. `ad_campaign_product_report` × N

| 项目 | 内容 |
|---|---|
| **调用位置** | `campaign_fetcher.py:293` `_fetch_perf_one` |
| **入参** | `{"shop_account", "campaign_name", "start_date", "end_date"}` — 日期窗口来自 `make_date_window(days, site_code)` |
| **返回结构** | `{cost, sale, clicks, impressions, orders, acos, cpc, ctr, cvr}`（7 天效果数据） |
| **下游消费** | `_assemble` → `CampaignUnit.perf_7d`（CampaignPerf 模型），用于 LLM 分析判断 |

### 1d. 自然排名旁路（与 1c 并行的 Task）

**条件**：仅当存在 EXACT 匹配类型活动  
**方式**：`asyncio.create_task` 与 1c 并行启动

| 工具 | 入参 | 超时 | 返回 | 下游消费 |
|---|---|---|---|---|
| `keyword_child_asins` | `{"keyword":"", "site_code", "parent_asin", ...}` — 全量查，非逐词 | 300s/工具, 45s overall | 每词 `{craw_nature_rank, near_craw_nature_rank}` | 主源：按 keyword 匹配到 CampaignUnit |
| `own_keyword_flow` | `{"parent_asin", "parent_seller_sku", "shop_account"}` — 与 1d-a 并行 | 同上 | 每词 `{自然位排位}`（当前排名） | 补缺：1d-a 没覆盖的关键词 |

---

## 阶段 2：LLM 前置数据预取

**入口**：`_analyze_one_stream` (`campaign.py:1101`)  
**模式**：exact / broad / new 三条流 `asyncio.gather` 完全并行

### 2a. EXACT 流 — 广告位明细

| 工具 | 调用 | 入参 | 超时 | 返回 | 下游 |
|---|---|---|---|---|---|
| `ad_campaign_placement_report` × M | `asyncio.gather` 全并行 | `{shop_account, campaign_name, start_date, end_date}` | 60s overall | `{placement_name: {cost, sale, clicks, acos, cpc}}` | 注入 LLM prompt 的 `_placement_data`，展示各广告位(TOS/ROS/PP)表现 |

### 2b. BROAD 流 — 搜索词明细

| 工具 | 调用 | 入参 | 超时 | 返回 | 下游 |
|---|---|---|---|---|---|
| `ad_campaign_search_term_report` × M | `asyncio.gather` 全并行 | `{shop_account, campaign_name, start_date, end_date}` | 60s overall | `[{keyword, clicks, cost, sales, orders, acos, cvr}]` | 注入 LLM prompt 的 `_search_term_data`，用于否词决策 |

---

## 阶段 3：新活动候选词发现

**入口**：`analyze_new_campaigns` (`campaign_new.py:263`)  
**模式**：作为第三条流与 exact/broad 并行

### 3a. 流量 + 自有词（并行）

| 工具 | 入参 | 返回 | 下游消费 |
|---|---|---|---|
| `flow_keywords` | `{site_code, parent_asin, parent_seller_sku, shop_account}` | `[{关键词, 搜索量, 搜索人数}]` | ① 构造 `NewCampaignCandidate`（source="flow"/"ranking_opportunity"）② `search_volume_map` 透传给 budget_reallocation 的 LLM |
| `own_keyword_flow` | `{parent_asin, parent_seller_sku, shop_account}` | `{关键词, 周搜索量, 自然排位排名, 自然位排位, 词的周排名}` | ① `own_rank_map[词]` 用于候选打标 ② `own_week_map[词]` 提取 week_rank/week_search_volume ③ 排名 28–48 的"排名机会词"单独建候选 |

### 3b. 竞品词发现（条件并行 Task）

**条件**：`campaign_new_competitor_enabled`

| 步骤 | 工具 | 入参 | 返回 | 下游 |
|---|---|---|---|---|
| 1 串行 | `direct_competitors` | `{parent_asin, parent_seller_sku, shop_account}` | `[{asin, price, star, reviews_num}]` (≤3) | 提取竞品 ASIN 列表 |
| 2 并行 | `seller_sprite_keyword_reverse` × ≤3 | `{asin: "竞品ASIN", market: "site_code"}` | `data.data.list[{keyword, searches, bid}]` | 构造竞品源候选 `source="competitor"` |

### 3c. 建议竞价（并行 Task）

| 工具 | 入参 | 返回 | 下游 |
|---|---|---|---|
| `whp_amazon_advert_keyword_suggest_bid` | `{shopAccount, parentAsin, parentSellerSku, keywordVoList: "[{\"keyword\":\"...\"},...]"}` | `{data: {suggestBidList: [{keyword, suggestBid}]}}` | 填充 `cand.suggested_bid` → `_calc_initial_bid` 计算初始出价 |

---

## 阶段 4：淘汰活动复评

**入口**：`_run_restart_review` (`campaign.py:157`)  
**条件**：仅当存在严格低价池活动

| 工具 | 调用 | 入参 | 返回 | 下游 |
|---|---|---|---|---|
| `ad_campaign_product_report` × K | 并行，Semaphore 限流 | `{shop_account, campaign_name, 入池窗口start, 入池窗口end}`（窗口=min(入池天数, 上限)） | 仅取 `orders` 字段 | 判断 case1(有单→复开)/case2(0单→看花费) |
| `ad_campaign_product_report` × M（条件） | 仅 case2 候选存在时，并行 | `{shop_account, campaign_name, 30d_start, 30d_end}` | 仅取 `cpc` 字段 | case2 建议出价 = max($0.20, min($0.50, 均CPC)) |

---

## 阶段 5：LLM 分析（DeepSeek API，非 MCP）

| 调用 | 数量 | 并发 |
|---|---|---|
| `recommend_campaign_overview`（策略总览） | 1 | 与 stream prefetch 并行 |
| `recommend_campaign_batch`（exact R1+R2） | 2 × N_batches | 每轮内批并行，闸控=50 |
| `recommend_campaign_batch`（broad R1+R2） | 2 × N_batches | 同上 |
| `recommend_new_campaigns`（R1+R2） | 2 × 4 batches | 同上 |
| `recommend_campaign_synthesis`（汇总） | 1 | 与 sanity + budget_realloc 并行 |
| `recommend_budget_reallocation`（预算回算） | 1 | 同上 |
| sanity_check（置信度复核） | N_batches | 同上 |

---

## 阶段 6：ERP 落库

**入口**：`auto_push.push_full_to_erp`  
**无 MCP 工具**——使用 MySQL 直连 `erp_agentadvert`，写入 21 张表。listing 上下文复用阶段 1 已解析的 site/shop/sku。

---

## 并行拓扑总览

```
0a → 0b (串行)
  ↓
0c (5 工具全并行: listing_basic_info, listing_inventory, ad_campaign_product_keyword_list, ad_product_report, product_sales)
  ↓
0d (keyword_child_asins × N ≤50, 全并行)
─────────────────────────────────
1a → 1b (串行)
  ↓
1c (N 对 [ad_campaign_basic_info ∥ ad_campaign_product_report], 全并行)
  ∥
1d (keyword_child_asins ∥ own_keyword_flow, Task 并行)
─────────────────────────────────
┌─ 2a (ad_campaign_placement_report × M, 并行)
├─ 2b (ad_campaign_search_term_report × M, 并行)
└─ 3a (flow_keywords ∥ own_keyword_flow) → 3c (bid, Task)
     ∥ 3b (direct_competitors → seller_sprite_keyword_reverse × 3, Task)
─────────────────────────────────
4a (ad_campaign_product_report × K, 信号量限流)
4b (ad_campaign_product_report × M, 条件)
─────────────────────────────────
LLM (7 类调用, 3 组并行)
─────────────────────────────────
ERP Write (MySQL, 21 表)
```

---

## 其他已注册但本链路未调用的工具（用于实时分析）

| 工具 | 原因 |
|---|---|
| `ad_keyword_report`（ASIN 级） | 2026-06-30 MCP 服务端下线，已从 META_KW_AD 移除 |
| `keyword_competitors` | arg builder 存在但代码路径已不再调用 |
| `ad_placement_report`（ASIN 级） | 被 campaign 级 `ad_campaign_placement_report` 替代 |
| `ad_search_term_report`（ASIN 级） | 被 campaign 级 `ad_campaign_search_term_report` 替代 |
| `ad_optimization` | 工具已注册，尚未接入 |
| `shein_*` / `product_competitors*` 系列 | 非广告方向，不适用 |
