# MCP 调用链路修复方案

> 配套文档：[MCP调用链路现状分析](./MCP调用链路现状分析.md)

## 原则

- **以 mcp_adapter 为主**——Tab5 继续走 `campaign_fetcher` → `mcp_adapter` 直调，不做 playbook 编排
- **前置工作流只拉产品级数据**——Tab1 只需 `parent_listing_detail`，Tab2-4 按各自 meta_filter 按需拉
- **统一缓存**——Tab1-4 和 Tab5 共享同一个缓存空间，避免重复调用

---

## 修改清单

### ① 统一缓存层

**文件**: `app/core/workflow_orchestrator.py` + `app/workflow/steps/campaign.py`

让 Tab5 的 `campaign_fetcher` 拉取前先查 `_ensure_data()` 的缓存，命中则跳过 MCP。

**或者反过来**：让 `_ensure_data()` 也查 `campaign:data:*`。选一种方向统一。

### ② 修正 preload 范围

**文件**: `app/core/workflow_orchestrator.py` — `_preload_data()`

```python
# 改前: meta_filter=None → 全量 MCP
data = await self.aggregator.fetch(asin, days=days)

# 改后: 只拉 context，其余各 Tab 按需
data = await self.aggregator.fetch(asin, days=days, meta_filter=["META_CONTEXT"])
```

`strategy/options` 实际只用到 `parent_listing_detail` 做 ASIN 验证。连 context 的 preload 都可以去掉，改为 `confirm_strategy` 时才调 `resolve_mcp_context_from_mcp`（已有此逻辑）。

### ③ 去重 playbook 重复工具

**文件**: `app/config/skills/mcp-query/playbook.yaml`

```yaml
# 改前: Phase 2 包含 ad_campaign_product_keyword_list
- id: mcp_bootstrap
  tools:
    - listing_basic_info_v2
    - parent_listing_stock_summary
    - ad_campaign_product_keyword_list    # ← 删掉

# 改后: 只保留真正的基础工具
- id: mcp_bootstrap
  tools:
    - listing_basic_info_v2
    - parent_listing_stock_summary
```

`ad_campaign_product_keyword_list` 由 Phase 3 的 `META_KW_AD` → `tools_from_meta` 统一拉取。

### ④ 可选：Tab5 按需懒加载

**文件**: `app/data/campaign_fetcher.py`

当前 `fetch_campaigns()` 一次性拉全部工具。可按分析阶段分批：

1. 先拉 `parent_listing_detail` + `listing_basic_info_v2` → 立即返回基础上下文
2. 再拉 `ad_campaign_product_keyword_list` + `ad_campaign_list` → 构建活动列表
3. 最后拉 `ad_product_report` + `ad_search_term_report` + `ad_placement_report` → 报表数据

用户可先看到活动列表，报表数据后台补全。

## 预期效果

| 指标 | 改前 | 改后 |
|------|------|------|
| 新建事件期 MCP 调用数 | 11 工具 | 0~1 工具 |
| `ad_campaign_product_keyword_list` 调用次数 | 2次/ASIN | 1次/ASIN |
| Tab5 命中上游已拉数据 | 从不 | 命中则 0 次 MCP |
| 最慢 ASIN 端到端 | 4轮重试 + LLM | ≤2轮 + LLM |
