# MCP 工具接入架构范式

> 本文档基于真代码审计（2026-06-27），不含任何虚构内容。
> 所有引用均为 `ad-direction-agent/` 下的实际文件与行号，可直接跳转。

---

## 1. 工具注册—构造—调用（三层递进）

### 1.1 注册：工具名 → 参数构造器

**ASIN 级注册（覆盖主数据拉取的大多数工具）**

文件：[app/data/mcp_mapping.py](ad-direction-agent/app/data/mcp_mapping.py#L94-L140)

`TOOL_ARG_BUILDERS: dict[str, ArgBuilder]` — 每个工具名一个条目，值是一个 `lambda ctx: {...}`（或复用函数引用），从 `McpContext` 取所需字段：

```python
TOOL_ARG_BUILDERS: dict[str, ArgBuilder] = {
    "listing_basic_info": lambda ctx: {
        "shop_account": ctx.shop_account,
        "parent_asin": ctx.parent_asin,
        "parent_seller_sku": ctx.parent_seller_sku,
    },
    "ad_product_report": _ad_common,   # 复用：几个报告类工具都用同一组 key
    "flow_keywords": lambda ctx: {
        "site_code": ctx.site_code,
        ...
    },
    ...
}
```

其中 `_ad_common`(:68) 被 `product_sales / ad_product_report / ad_placement_report / ad_search_term_report` 四个工具复用，其 key 集合为 `{parent_asin, parent_seller_sku, shop_account, start_date, end_date}`(:69-74)。

入口函数 `build_tool_args`(:150) 做两件事：
1. 查 `TOOL_ARG_BUILDERS` 拿到 builder
2. 过滤掉值为 `None` 或 `""` 的 key（防止把空值当参数传给 MCP）→ `{k:v for k,v in builder(ctx).items() if v not in (None,"")}`

**多工具批量注册（维度 → 工具列表）**

[app/data/mcp_mapping.py](ad-direction-agent/app/data/mcp_mapping.py#L78-L91) — `META_TO_MCP_TOOLS` 把业务维度（META_*）映射到对应工具名列表：

```python
META_TO_MCP_TOOLS = {
    "META_KW_AD": ["ad_keyword_report"],
    "META_TREND": ["product_sales"],
    ...
}
```

因为一个维度可能需要多个工具并用（如 `META_KW_COMPETITOR_RANK: ["keyword_competitors","keyword_child_asins"]`），批量拉取时展开维度列表→工具列表去重后并发调。

**活动级注册（活动维度，不用 McpContext）**

[app/data/mcp_mapping.py](ad-direction-agent/app/data/mcp_mapping.py#L157-L195) — `build_campaign_tool_args(tool_name, campaign_name, shop_account, start_date, end_date)`：

```python
CAMPAIGN_TOOLS = [           # 活动级工具的权威列表
    "ad_campaign_basic_info",
    "ad_campaign_product_report",      # + start/end_date
    "ad_campaign_placement_report",    # + start/end_date (懒加载)
    "ad_campaign_search_term_report",  # + start/end_date (懒加载)
]
```

活动级 `build_campaign_tool_args` 不用 `McpContext`，**直接收形参**（`campaign_name` + `shop_account` + 日期），因为活动的发起方在 campaign_fetcher 里已解析好了这些值。

### 1.2 构造：McpContext 的装配（所有调用的最上游瓶颈）

`McpContext`（[mcp_mapping.py:18-24](ad-direction-agent/app/data/mcp_mapping.py#L18-L24)）是 ASIN 级 MCP 调用的**通用载体**，固定 6 个字段：

```python
@dataclass(frozen=True)
class McpContext:
    parent_asin: str
    parent_seller_sku: str
    shop_account: str
    site_code: str
    start_date: str
    end_date: str
```

全仓有 **两条** 构造路径，都依赖同一个上游 SQL（`resolve_mcp_context_from_db`）：

| 路径 | 位置 | 流程 |
|---|---|---|
| **主拉取路径** | [mcp_adapter.py:140-175](ad-direction-agent/app/data/mcp_adapter.py#L140-L175) `_resolve_context` | `resolve_mcp_context_from_db(asin)` → 拿到 site/sku/shop → `make_date_window(days, site_code)` → `McpContext(...)` → 可选再调 `listing_basic_info` 纠偏 sku |
| **Skill 执行器** | [mcp_query.py:56-82](ad-direction-agent/app/skills/executors/mcp_query.py#L56-L82) | 同上：`resolve_mcp_context_from_db(asin)` → `make_date_window(days, site_code)` → `McpContext(...)` |

也就是说，**当前所有 MCP 调用都经 `resolve_mcp_context_from_db` → `mcp_db_context._lookup_sync` → SQL 查 `dwd_whp_amazon_listing_general` 拿 site/sku/shop**（见本会议中"三处直查数仓"的第 1 处）。这是"新 MCP 工具替换"最上层的切入点。

### 1.3 调用：并发执行

**ASIN 级** → [mcp_fetch_run.py:16-72](ad-direction-agent/app/data/mcp_fetch_run.py#L16-L72) `run_planned_mcp_tools`
- 入参 `planned_tools`（展开维度的工具名列表）、`ctx: McpContext`
- 每个工具调一次 `build_tool_args(tool_name, ctx)` 构参 → `adapter.call_tool_timed_with_args(tool_name, args, timeout)` → 并发 gather
- `_call_tool`([mcp_adapter.py:103](ad-direction-agent/app/data/mcp_adapter.py#L103)) 走 JSON-RPC `self._invoker.call_tool(tool_name, arguments)`，带 `mcp_max_concurrency` 信号量限流，带 `mcp_retries` 重试。

**活动级** → [campaign_fetcher.py:51-57](ad-direction-agent/app/data/campaign_fetcher.py#L51-L57)
- 对每个活动名并发：`asyncio.gather(_fetch_basic_one(name, shop), _fetch_perf_one(name, shop, sd, ed))`
- `_fetch_basic_one`(:234) 调 `adapter.campaign_call_tool("ad_campaign_basic_info", ...)`
- `_fetch_perf_one`(:264) 调 `adapter.campaign_call_tool("ad_campaign_product_report", ...)`

---

## 2. 返回字段的全链路透传（以主数据流为例）

**第一段**：MCP 原始响应 → `_CallResult(ok, value)`（[mcp_adapter.py:117](ad-direction-agent/app/data/mcp_adapter.py#L117)），`value` 是 MCP 服务端返回的原始 JSON dict/list。

**第二段**：`run_planned_mcp_tools` 把 tool_name → value 收进 `payload_map`（[mcp_fetch_run.py:52-58](ad-direction-agent/app/data/mcp_fetch_run.py#L52-L58)）：`payload_map[tool_name] = res.value`。

**第三段**：`assemble_from_payloads` → 每个 tool 配一个 `normalize_*` 函数（[mcp_adapter.py:218-237](ad-direction-agent/app/data/mcp_adapter.py#L218-L237)）：

| MCP 工具 | normalizer | 落 ASINData 字段 |
|---|---|---|
| listing_basic_info | [mcp_normalizers.py](ad-direction-agent/app/data/mcp_normalizers.py) `normalize_listing_basic_info` | asin, sku, price, rating, brand, category_name 等 |
| ad_product_report | `normalize_ad_summary` | acos, cpc, ctr, cvr, spend, sales → `data.ad_data` |
| ad_placement_report | `normalize_ad_placement` | 合并进 `AdData`（:278 `ad_kwargs.update(placement)`）|
| ad_keyword_report | merge + `normalize_keywords` | `data.keywords`（list of KeywordData）|
| keyword_competitors / keyword_child_asins | `normalize_keyword_rankings` | 关键词的 natural_rank/near_natural_rank |
| flow_keywords / own_keyword_flow | `normalize_flow_keywords` | flow 关键词列表 |
| product_sales | `normalize_product_sales` + `normalize_trend` | margin, units, total_sales, trend |
| direct_competitors | `normalize_competitors` | competitors |

正常的 MCP 返回路径：`_CallResult.value` → `payload_map[tool_name]` → `normalize_*` → `ASINData` 属性。
失败的 MCP 返回（`res.ok=False`）：记录到 `missing_fields` 和 `partial_failures`，供上层 `doris_fallback` 查询补缺。

**第四段**：活动级返回不走 normalizer，直接内联：

- `_fetch_basic_one`([campaign_fetcher.py:234-262](ad-direction-agent/app/data/campaign_fetcher.py#L234-L262))：MCP 中文列名（`"广告活动预算"/"关键词BID"/"活动上线天数"/"头部位置加价比例"` 等）→ 英文字段 dict
- `_fetch_perf_one`([campaign_fetcher.py:264-291](ad-direction-agent/app/data/campaign_fetcher.py#L264-L291))：MCP 中文列名（`"花费"/"销售额"/"点击量"/"ACOS"/"CPC"` 等）→ 英文字段 dict

> 注意：活动级字段映射（`_to_float(row.get("花费"))` 这种）是**散装的、内联在 campaign_fetcher 里的**，和主数据流有独立的 normalizer 不同——新增活动级工具时需在同文件里新增对应 normalizer 段。

---

## 3. 新 MCP 工具与数仓直查的关系（兜底回落）

### 3.1 当前的"MCP 优先、Doris 回落"模式

| 层级 | MCP 路径 | 回落路径 | 切换机制 |
|---|---|---|---|
| **主数据拉取** | `run_planned_mcp_tools` → MCP 成功 | `mcp_tool_fallback.py:MCP_TOOL_TO_META` 把失败工具映射到 META* 号 → Doris 并行查询补缺 | 工具级：单个工具失败就回落、不影响其他工具；`missing_fields` 做差分 |
| **活动效果** | `campaign_fetcher._fetch_basic_one` / `_fetch_perf_one` → MCP 成功 | `_doris_fallback_basic_info`(:295) 返回默认值字典 + `source:"doris"`；perf 也有 Doris 回落 `_zip_results` 后用 DB 补齐 | 活动级：单个活动的 basic/perf 都失败时用 DB 兜底 |
| **全 SQL（无 MCP，当前本会议重点）** | 无 | `_resolve_and_fetch_listing` / `_fetch_campaign_context` 必走 DB | 三条 SQL 没有 MCP 路径，是本次新工具要替代的目标 |

### 3.2 回落设计应该遵循的规则

新工具上线后的优先级：**MCP 成功 → 用 MCP 结果；MCP 失败/超时 → 走 Doris 回落；开关默认关**

即新工具接入后，三处 SQL 代码**不删不改**，而是在其外包装一层"先试 MCP，失败或不生效走回 SQL"的判断，与 `MCP_TOOL_TO_META`/campaign 级 fallback 的设计保持一致。

---

## 4. 怎么保证字段全链路透传（最上游改动）

### 4.1 核心原则：McpContext 是上游唯一入口

`McpContext` 的 6 个字段是**全仓所有 MCP 调用的公共契约**。新增任何返回字段，要么塞进 `McpContext`（变成公用的），要么在各 normalizer 内独立收——前者影响面最大、改动最少。

本项目实际采用的方式：**McpContext 只传 MCP 入参所需的 key**（parent_asin/sku/shop/site/date）；**返回值是独立路径**（每个工具配一个 normalizer，从 MCP 原始 JSON 的相应 key 拿出来，放进 `ASINData` 具体字段）。

### 4.2 一次"新增 MCP 工具并替换 SQL"的完整改动清单

以 **"新增一个工具替换 `_resolve_and_fetch_listing` → `_fetch_campaign_context` 两条 SQL"** 为例，需改动的文件和顺序：

**0. 确认 McpContext 里的字段是否够用**——不够则加在 `McpContext` 定义 + `_resolve_context` 构造处 + mcp_query skill 构造处。

**1. 注册** → [mcp_mapping.py](ad-direction-agent/app/data/mcp_mapping.py)：`TOOL_ARG_BUILDERS` 加一行 `"new_tool_name": lambda ctx: {...}`；如果是活动级，加到 `CAMPAIGN_TOOLS` 列表 + `build_campaign_tool_args` 里加日期判断。

**2. 构造参数字段** → `build_tool_args` / `build_campaign_tool_args` 已过滤 None+空串，注册的 lambda 只放模板。

**3. 调用点** → 在 [campaign_fetcher.py:81](ad-direction-agent/app/data/campaign_fetcher.py#L81) `_discover_context_from_doris` 之上，加 `_discover_context_from_mcp` 同签名函数，内部调 MCP、解析、失败返回 `[]`（fallback 标记）。

**4. 优先级** → 在 [campaign_fetcher.py:50-90](ad-direction-agent/app/data/campaign_fetcher.py#L50-L90) `fetch_campaigns` 中，用 settings.`mcp_discover_campaigns` 开关做 MCP→DB 二选一，与已有的 `prefer_db` 对齐语义。

**5. 返回 normalizer** → 新增或复用 normalizer 函数，把 MCP 原始 JSON（中文列名→字典）转成现有下游结构（list of dict，包含 campaign_name, campaign_id, child_asin, keyword_text, match_type 等），确保回落后的下游代码零改动。

**6. 保留 DB 回落** → 原 SQL 路径的 `_resolve_and_fetch_listing` + `_fetch_campaign_context` 代码不删，作为 MCP 失败的超时/失败兜底。

**7. 测试** → mock `_call_tool` 返回成功/失败/超时各场景，验证正常走 MCP、失败走 DB、开关关=走 DB 三条路径。

### 4.3 保证全链路字段不丢的检验清单（每次新增工具必过）

- [ ] `TOOL_ARG_BUILDERS` 注册的 key 是否都在 `McpContext` 里（不会因为过滤 None 而缺参）？
- [ ] 新工具的返回 normalizer 是否覆盖了 DB 回落路径的所有必填字段（否则 DB 和新工具行为不一致→下游崩）？
- [ ] `McpContext` 构造点的 site/sku/shop 没被误覆盖（注意 `_resolve_context` 和 mcp_query skill 两处要同改）？
- [ ] `build_tool_args` 的过滤逻辑（`v not in (None,"")`）是否与新增字段兼容？
- [ ] 接入新的mcp工具后必须同步写单元测试代码，测试要基于正确的断言出发，而不是让单测适配代码。