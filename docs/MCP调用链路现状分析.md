# MCP 调用链路现状分析

> 2026-07-11，基于服务器日志 + 代码逐行追踪

## 1. 两套独立的 MCP 调用路径

```
路径 A: mcp_query skill (playbook 编排)
  被谁调 → DataAggregator.fetch() → Tab1-4 的 _ensure_data()
  缓存   → asin_data_cache (Redis)
  工具   → 按 playbook.yaml 分 3 阶段: context → bootstrap → reports

路径 B: mcp_adapter 直调 (硬编码工具名)
  被谁调 → CampaignFetcher.fetch_campaigns() → Tab5 的 campaign/viewmodel
  缓存   → campaign:data:{asin}:{days} (独立的 Redis key)
  工具   → 15+ 处 self._mcp().campaign_call_tool("xxx", ...)
```

两条路径调同一批 MCP 工具，两套 Redis 缓存相互不可见。**Tab5 永远不会读 Tab1-4 的缓存，Tab1-4 也永远不会读 Tab5 的缓存。**

## 2. 前置工作流拉取过量

点击"新建分析事件"后，`strategy/options` 的 `preload_data()` 传了 `meta_filter=None`，触发全部 MCP 工具。但 Tab1 实际只需要 `parent_listing_detail` 验证 ASIN 是否存在，其余 10 个工具被白拉。

## 3. 同一工具被重复调用

`ad_campaign_product_keyword_list` 在 playbook Phase 2 `mcp_bootstrap` 硬编码列表中，又在 Phase 3 `mcp_reports` 通过 `META_KW_AD` → `tools_from_meta` 被再次映射。无去重逻辑，导致同一 ASIN 最慢的工具（134~168s）被调两次。

## 4. 各 Tab 实际 MCP 需求

| MCP 工具 | Tab1 | Tab2 | Tab3 | Tab4 | Tab5 |
|----------|:----:|:----:|:----:|:----:|:----:|
| `parent_listing_detail` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `listing_basic_info_v2` | | ✓ | ✓ | ✓ | ✓ |
| `parent_listing_stock_summary` | | ✓ | ✓ | ✓ | ✓ |
| `ad_campaign_product_keyword_list` | | ✓ | ✓ | ✓ | ✓ |
| `ad_product_report` | | ✓ | ✓ | ✓ | ✓ |
| `product_sales` | | ✓ | ✓ | ✓ | ✓ |
| `direct_competitors` | | ✓ | ✓ | | |
| `flow_keywords` | | | ✓ | ✓ | ✓ |
| `ad_placement_report` | | | | ✓ | ✓ |
| `ad_search_term_report` | | | | | ✓ |
| `keyword_child_asins` | | ✓ | ✓ | ✓ | |
| **合计** | **1** | **7** | **8** | **9** | **9** |

## 5. 服务器实测验证

以 "US-连体吊带泳衣" 为例，一次新建事件触发了 4 轮 MCP 重试，`ad_campaign_product_keyword_list` 耗时 134~158s，`ad_campaign_list` 超时 60s。同期另一个 ASIN `B0CQZT519T` 正常完成。说明慢不是偶发，是工具本身对数据量大的 ASIN 稳定慢。
