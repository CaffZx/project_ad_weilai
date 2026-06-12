# Campaign 广告活动分析引擎 — 交接文档

> **最后更新**: 2026-06-12
> **版本**: v2.0
> **分支**: chenv3.1

---

## 1. 项目背景与目标

### 1.1 背景

**项目**：广告方向决策子智能体（Ad Direction Agent），面向亚马逊广告运营的 AI 辅助决策系统。

**数据源**：Doris 数仓 + MCP StarRocks 网关（`DATA_SOURCE=mcp` 优先，失败降级 Doris）。

**技术栈**：Python 3.13 / FastAPI / PyMySQL / DeepSeek API / Pydantic

### 1.2 Campaign 模块目标

自动化分析和调整所有广告活动（Campaign），核心能力：

- **自动发现**：从 Doris 获取 ASIN 下所有广告活动的维度信息
- **AI 分析**：基于 KB 知识库规则（18/19/21/22），对每个活动给出淘汰/调整/保持建议
- **分流策略**：精准广告（EXACT）和广泛广告（BROAD/PHRASE/AUTO）使用不同的调整维度
- **多轮投票**：R1+R2 并行 → 投票比对 → R3 tiebreaker（分歧时触发），保证决策可靠性
- **AI 汇总**：将 N 条单活动建议合成为按共同原因分组的运营叙事

### 1.3 姐妹项目

| 项目 | 路径 | 用途 | 备注 |
|------|------|------|------|
| **ad-direction-agent** | `ad-direction-agent/` | 主服务：四层工作流 + Campaign 分析 | FastAPI，端口 8000 |
| **ad-purpose-agent** | `ad-purpose-agent/` | 广告目的/词类 AI 推荐 | PYTHONPATH 引用，勿移目录 |

purpose-agent 负责策略层 LLM 调用（`determine_ad_targets_from_metrics()`），输出广告目的（引流型/转化型/排名型/盈利型）+ 关键词分类（Broad/Long-tail/Competitor/Brand/Custom）。方向的 Campaign 模块不调用 purpose-agent。

---

## 2. Campaign 功能概况

### 2.1 数据流

```
parent_asin
  ├─ Doris 上下文查询 → 仅维度字段 (campaign_name/campaign_id/child_asin/keyword_text/match_type/keyword_bid)
  ├─ 代码硬过滤 → 排除 non-ENABLED / 无数据 / 多关键词活动
  ├─ Campaign 预过滤 → 排除 budget≈$1 & bid≈$0.2 的疑似已淘汰活动
  ├─ MCP basic_info + product_report 并行 → 回落 Doris
  ├─ 组装 CampaignUnit[] (campaign_key = "活动名 × 子ASIN")
  ├─ ★组合预分类 → 4 类判定: 主推/广泛自动/测试新增/淘汰 (campaign_portfolio.py)
  ├─ ★三股并行 (strategic_overview 后, 共享 ctx_dict/posture_brief, return_exceptions 隔离):
  │   ├─ 精准流: EXACT, 预取 placement → _EXACT_PROMPT → 分批投票
  │   ├─ 广泛流: BROAD+PHRASE+AUTO, 预取 search_term → _BROAD_PROMPT → 分批投票
  │   └─ ★新增活动线: 候选词发现→硬过滤→双轮取交集 (KB16+06, campaign_new.py, 详见 §9)
  ├─ 合并两流结果 + ★组合终分类 (LLM action 补淘汰判定); new_campaigns 独立挂载
  ├─ ★action 归一化 (_normalize_action: proposed vs current 差值 derive 权威 action)
  ├─ Budget 冲突裁决 + ★终态 action 二次归一
  ├─ ★预过滤可见化: 淘汰池/多词活动打 __prefiltered, 合并 excluded → 前端灰卡 (§9.3)
  ├─ Sanity check (仅低置信项，≤10/批，并行)
  ├─ ★组合预算汇总 (campaign_budget_summary.py: 3 组约束分配)
  └─ AI 汇总合成 (按共同原因分组叙事) — 2026-06-08 已恢复 (§7.1)
```

### 2.2 核心设计决策

| 决策 | 说明 |
|------|------|
| campaign_key = "活动名 × 子ASIN" | 运营确认的唯一标识，取代 "child_asin\|match_type\|keyword" |
| 精准/广泛分流 | 不同匹配类型使用不同 prompt 和调整维度（Placement vs SearchTerm） |
| 分批大小 = 6 | 每批 6 个活动送入 LLM，平衡覆盖率和输出质量 |
| R1+R2 并行投票 | 两轮不同随机种子排序 → 比对 action + direction → 一致=high，分歧=low |
| R3 tiebreaker | 仅在 R1+R2 分歧时触发，从未真触发（B0B7S3PWWB 全部一致） |
| Sanity 仅校验低置信 | 高/中置信跳过，节省 LLM 调用 |
| 策略上下文注入 | 每批 LLM 看到产品阶段/广告目的/目标ACOS/毛利率等 12 个字段 |

### 2.3 置信度划分

| 等级 | 条件 |
|------|------|
| **high** | R1+R2 的 action + direction 完全一致，取保守幅度 |
| **medium** | 仅单轮（<2批）不投票；或 R3 tiebreaker 打破僵局 |
| **low** | 两轮分歧且 R3 未解决；或仅一轮有结果 |

### 2.4 调整类型

| action | 说明 | 精准流额外字段 | 广泛流额外字段 |
|--------|------|--------------|--------------|
| `eliminate_to_low_bid_pool` | 淘汰至低竞价池 (Bid=$0.20, Budget=$1) | placement_adjustments | negative_keywords |
| `adjust_bid` | 调整出价 | placement_adjustments | negative_keywords |
| `adjust_budget` | 调整预算 | placement_adjustments | negative_keywords |
| `adjust_placement` | 调整广告位分配（仅精准） | placement_adjustments | — |
| `keep` | 保持现状 | — | — |

---

## 3. 代码文件与改动

### 3.1 核心文件总览

| 文件 | 行数 | 角色 |
|------|------|------|
| `app/workflow/steps/campaign.py` | ~1450 | ★编排引擎：分流→分批→投票→校验→合成；action 归一化；组合分类调度 |
| `app/llm/reasoner.py` | ~1640 | LLM Prompt 构建 + `recommend_campaign_batch()` + `recommend_campaign_synthesis()` |
| `app/models/campaign.py` | ~183 | 全部 Campaign 数据模型 (含 portfolio/ai_portfolio_class/budget_summary) |
| `app/data/campaign_fetcher.py` | ~507 | 数据编排器：Doris上下文→预筛选→MCP→回落 |
| `app/data/campaign_prefilter.py` | ~80 | 硬过滤纯函数 |
| `app/api/campaign.py` | ~148 | API 端点：`POST /campaign/analyze`(meta_filter="META_AD_PRODUCT") + `/confirm`(stub) |
| `app/llm/client.py` | ~200 | DeepSeek API 客户端 + KeyPool 轮询(Rlock) |
| `app/config/settings.py` | ~210 | Campaign 相关配置项 (含 portfolio shares/fallback_multiplier) |
| `demo/campaign_test.html` | ~950 | 调试前端 (含组合筛选气泡 + 预算约束卡) |
| `app/llm/kb_loader.py` | ~155 | KB 加载器，`campaign_adjustment` preset (KB 18/19/21/22) |
| `app/workflow/steps/campaign_portfolio.py` | ~105 | ★组合分类器：4 类 deterministic (淘汰→广泛/自动→测试/新增→主推) |
| `app/workflow/steps/campaign_budget_summary.py` | ~90 | ★预算汇总：3 组约束分配 (主推/测试/广泛)，淘汰不参与约束 |
| `app/workflow/steps/campaign_new.py` | ~340 | ★新增活动分析线 (KB 16)：候选词发现→硬过滤→trigger标注→双轮取交集→组装；`pick_target_child_asin` 选投放子ASIN (详见 §9) |
| `app/data/campaign_prefilter.py` | ~85 | 硬过滤纯函数 (v1.7 加多词去重+补维度字段+`__prefiltered` 标记，供前端预过滤卡展示) |
| `demo/campaign-panel/` | ~1700 | ★前端合并模块 (ES module + CSS `.camp-` 前缀 + 事件委托)，独立维护于 `campaign-panel/` 目录 (详见 §10.3) |

### 3.2 关键配置项（settings.py）

```python
# Campaign LLM
campaign_llm_concurrency: int = 8   # 单 ASIN 批次并发数（10 key 容量）
campaign_batch_size: int = 6        # 每批活动数
campaign_llm_temperature: float = 0.3

# Campaign 数据管道
campaign_discovery_timeout: float = 90.0
campaign_mcp_tool_timeout: float = 300.0
campaign_db_fallback_timeout: float = 60.0
campaign_prefilter_enabled: bool = True

# Campaign 组合分类与预算
campaign_portfolio_enabled: bool = True    # 组合分类开关(关闭退化到改前)
campaign_budget_fallback_multiplier: float = 1.15  # daily_budget 兜底乘数
campaign_portfolio_share_main: int = 60    # 主推约束占比
campaign_portfolio_share_test: int = 20    # 测试/新增约束占比
campaign_portfolio_share_broad: int = 20   # 广泛/自动约束占比
# 淘汰组不参与约束概念(KB 21 §6 每活动 $1)

# LLM
llm_timeout: int = 75               # httpx read 超时（秒）
deepseek_model: str = "deepseek-v4-pro"
llm_global_concurrency: int = 420   # 服务级 LLM 总并发（client 层信号量；46 key 场景,6 worker→每 worker 70）
num_workers: int = 1                 # 读 NUM_WORKERS,把全局闸切给各 worker（须 = 启动 --workers）
# client.py 连接池 max_connections=600
# campaign_global_llm_concurrency: 已 DEPRECATED，全局闸收口到 client 层
```

#### Timeout 配置全景（多层级，外层优先级最高）

| 层级 | 配置 | 值 | 触发位置 |
|---|---|:--:|---|
| API handler 顶层 | (硬编码) wait_for | 120s | api/campaign.py `aggregator.fetch` |
| Campaign 数据拉取 | (硬编码) wait_for | 300s | steps/campaign.py `fetch_campaigns` |
| 流级单批 LLM | `LLM_TIMEOUT` | 60s | steps/campaign.py `_run_round` |
| Sanity 单批 LLM | (硬编码) wait_for | 60s | steps/campaign.py `_sanity_check` |
| Synthesis 单次 LLM | (硬编码) wait_for | 60s | steps/campaign.py `analyze_campaigns` |
| LLM HTTP 单次 | `llm_timeout`(read) | 75s | llm/client.py `httpx.Timeout` |
| LLM 连接超时 | (硬编码) connect | 8s | llm/client.py `httpx.Timeout` |
| LLM 重试 | `MAX_RETRIES` | 3 | llm/client.py（首次+2 重试，重试前退避 0.5/1.0s+抖动） |
| LLM 单次最坏耗时 | = 75 × 3 + 退避 | ~226s | 累计（受外层 wait_for 截断） |
| LLM 服务级并发闸 | `llm_global_concurrency` | 420 | llm/client.py `chat()` 内信号量（多 worker 时 ÷num_workers） |
| MCP 工具 | `mcp_tool_timeout` | 1200s | settings.py |
| Doris 子 ASIN 上限 | `db_child_asin_cap` | 200 | settings.py → db_sql_helpers.py |
| MCP 上下文解析 | `mcp_context_timeout` | 30s | settings.py |
| Doris 主 fetch | `db_fetch_timeout` | 180s | settings.py |
| Doris flow_keywords | `db_flow_keyword_timeout` | 90s | settings.py |

**设计原则**：外层 wait_for 比内层 httpx/DB 自身 timeout 更短，让外层先触发 cancel；内层 timeout 作为兜底保险。MCP tool 1200s 是底层最大，被各级外层 wait_for 截断。

### 3.3 API 端点

| 方法 | 路径 | 用途 | 状态 |
|------|------|------|------|
| POST | `/campaign/analyze` | 运行 Campaign LLM 分析 | ✅ 生产可用 |
| POST | `/campaign/confirm` | 运营批量审核 | ⚠️ stub（仅日志） |

### 3.4 关键改动记录

| 日期 | 改动 | 说明 |
|------|------|------|
| 06-01 | `_vote_key` 统一 | 提取模块级函数，修复 `_resolve_tiebreaker` L664 key 规则不一致 |
| 06-01 | 并发优化 | `campaign_llm_concurrency: 3→8`，exact/broad 各自 Semaphore(10) |
| 06-01 | 超时修复 | `llm_timeout: 180→75`，`MAX_RETRIES: 3→2` |
| 06-01 | Sanity 临时禁用 | Windows asyncio 取消缺陷导致挂死 |
| 06-01 | Synthesis 临时禁用 | 同上，`asyncio.wait_for(60s)` 取消不生效 |
| 06-01 | `timeout_override` | `chat()` 新增参数，httpx 层自断绕过 asyncio 取消 |
| 06-01 | `max_tokens: 4096→8192` | 防止 JSON 输出截断 |
| 06-01 | `return_exceptions=True` | 精准+广泛 stream gather 异常隔离 |
| 06-01 | 清货期补齐 | ProductStage 枚举 + TOML + scenario_analyzer |
| 06-01 | Sanity 恢复 | 恢复并加固：`timeout_override=90` + gather `wait_for(120s)` + 语义化 `(warnings, all_ok)` |
| 06-01 | Synthesis 修复 | `timeout_override=55`，去 `asyncio.wait_for`（需 Linux 验证后开启） |
| 06-01 | 幂等防护 | 同 ASIN 重复运行拒绝 + 600s 僵尸清理 |
| 06-01 | Redis 缓存 | `fetch_campaigns` 结果进 Redis（TTL 30min），`refresh=True` 跳过 |
| 06-01 | 淘汰活动字段清洗 | 无条件填 $1/$0.20 + 清空 placement/neg_kw |
| 06-01 | `_same_direction` 扩展 | 精准流补 placement 比对，广泛流补 neg_kw 比对 |
| 06-01 | `_resolve_tiebreaker` bug | 只处理 disputed_keys，防止高置信项被误降级 |
| 06-01 | 计时日志 | `Campaign timing` + `Stream timing` 全链路 |
| 06-02 | **策略总览（执行总纲）** | AI 定性指挥：现状→目的→方向（三段+posture_brief），注入批量分析 preamble |
| 06-02 | 广告方向+每日预算入上下文 | `ad_directions` ← `workflow_state.execution`，`daily_budget` ← override→asin_data |
| 06-02 | **Tier 1 Event Loop 修复** | `main.py` 切 `SelectorEventLoopPolicy`（Windows），根治 asyncio 取消挂死 |
| 06-02 | **Tier 2 全局并发上限** | `_global_llm_sem()` 进程级 Semaphore(30)，跨 ASIN/流共享 |
| 06-02 | 每流并发配置化 | `campaign_llm_concurrency: 10` 接到 exact/broad Semaphore |
| 06-02 | `db_child_asin_cap`: 80→200 | 子 ASIN 截断放宽 |
| 06-01 | playbook.yaml 迁移 | `.claude/skills` → `app/config/skills` |
| 06-01 | `fetch_campaigns` 加 wait_for(300s) | MCP/Doris 子调用挂死防护，超时返回降级 result |
| 06-01 | `api/campaign.py` 顶层 try/except | TimeoutError + Exception 全捕获，500 → 降级 CampaignAnalysisResult |
| 06-01 | `_run_round` 解析挪进 try | result 非 dict / parsed 非 dict 等异常落到 except，不外抛 |
| 06-01 | `_run_round` gather `return_exceptions=True` | 二次防御 `_call_one` 未来回归 |
| 06-01 | `_unpack_stream` helper | 流级 gather 异常→空 4 元组，warning 入栈 |
| 06-01 | Sanity/Synthesis 恢复 | `timeout_override` 绕过 Windows asyncio 缺陷；sanity gather 加 120s timeout |
| 06-01 | `_resolve_tiebreaker` bug | 修复全量降级：仅遍历 disputed_keys，高置信项不受影响 |
| 06-01 | `_same_direction` 扩展 | 精准流比 `_placement_sig`，广泛流比 `_negative_kw_sig`，杜绝 placement/否词分歧误判 |
| 06-01 | sanity 语义修正 | 返回 `tuple[warnings, all_ok]`，细化区分"校验通过"与"校验失败" |
| 06-01 | 淘汰活动清洗 | `_resolve_budget_conflicts` 无条件填 $1/$0.20 + 清空 placement/neg_kw |
| 06-01 | 幂等 TTL 兜底 | `_running` 存 `(run_id, monotonic)` + 600s 僵尸兜底 |
| 06-01 | Redis refresh | `refresh=True` 跳过缓存；`save_cached` 去掉 `default=str` hack |
| 06-01 | `db_child_asin_cap` | 80→200 |
| 06-01 | synthesis `max_tokens` | 3000→4096 |
| 06-01 | 入口日志 | batch/synthesis/sanity 三处 LLM 入口打 `logger.info` (ASIN/items/temp) |
| 06-02 | **组合分类器** | `campaign_portfolio.py`: 4 类 deterministic (淘汰→广泛/自动→测试/新增→主推) |
| 06-02 | **预算汇总** | `campaign_budget_summary.py`: 3 组约束分配 (主推 60%/测试 20%/广泛 20%),淘汰不参与 |
| 06-02 | **daily_budget 三级兜底** | `build_campaign_strategy_context`: override → asin_data → `spend/days × 1.15` |
| 06-02 | 前端组合气泡 + 预算卡 | 筛选气泡按钮(互斥单选)+ 预算约束 vs 勾选汇总卡片 |
| 06-02 | 组合分类顺序修正 | 广泛/自动提前到测试/新增之前 (BROAD 活动本质是测词,不算"新建测试") |
| 06-02 | 占比 3 组化 | 淘汰组从约束概念移除;share 60/20/20 (原 50/30/15/5) |
| 06-02 | 前端预算卡极简化 | 砍掉 4 行表格/占比条;改为 2 个数字:预算约束 + 当前勾选汇总 |
| 06-03 | **KeyPool RLock 死锁修复** | `threading.Lock`→`RLock`: `next_key()`→`_maybe_log_stats()`→`stats()` 同一锁重入死锁 |
| 06-03 | **meta_filter 优化** | `api/campaign.py`: fetch 时硬限 `meta_filter=["META_AD_PRODUCT"]`,砍 6 个无用 META |
| 06-03 | **action 归一化** | `_normalize_action()`: 代码从 proposed vs current derive 权威 action,LLM 只给淘汰判据 |
| 06-03 | 终态 action 二次归一 | `_resolve_budget_conflicts` 末尾: conservative merge 后补一次归一(keep 语义清理) |
| 06-03 | Redis 空结果防毒化 | `_save_cached_campaigns`: total=0 或 errors 不空时不入缓存,防 MCP 临时失败毒化缓存 |
| 06-03 | A1 问题文档化 | api handler 加 TODO(A1): `_ensure_data` meta_filter 不支持缓存,待修复后改走 ctx 路径 |
| 06-03 | 向导页显示 override | `wizard.py` + `layers.py`: 透传 target_acos_override / daily_budget_override |
| 06-04 | **LLM client 单例收口** | `reasoner.py:462` 默认 client → 共享 `deepseek_client`；3 个 httpx 池坍缩为 1，shutdown 泄漏消除（详见主交接文档 §7.5） |
| 06-04 | **服务级并发闸** | `client.py:_global_llm_sem` Semaphore(`llm_global_concurrency=40`)，在 `chat()` 内过闸；campaign `_GLOBAL_LLM_SEM` 删除，`_call_one` 移除 gsem，保留 per-stream sem |
| 06-04 | **连接池 + 超时分离** | `client.py:_ensure_client`: `max_connections=200`；`Timeout(connect=8/read=75/write=10/pool=5)` |
| 06-04 | **重试退避** | `MAX_RETRIES: 2→3`；重试前指数退避 0.5/1.0s + 抖动 50–200ms |
| 06-04 | **KeyPool 热路径硬化** | `mark_success` 不再写盘（阻塞事件循环）；`mark_failed` debounce（>1s 才写） |
| 06-04 | **幂等锁迁出** | `_acquire_running`/`_release_running`/`_get_redis` 抽到 `app/persistence/redis_client.py`（通用 `acquire_lock`/`release_lock`，SET NX EX + compare-and-delete Lua + TTL≥`campaign_total_timeout`）；campaign 改 import 调用 |
| 06-05 | **组合枚举改名** | 主推/广泛自动/测试新增/淘汰 → **精准主力组/精准测试组/自动广泛组/低价捡漏组**；ERP 码 core/auto_broad/test/eliminate → **exact_core_group/exact_testing_group/auto_broad_group/low_bid_retention_group**。真源 `campaign_portfolio.py` 常量 + `erp_writer/text_utils._CAMPAIGN_GROUP_TYPE_MAP`（含旧值兼容别名）。**分类逻辑保留我方 $5 分界（KB23 §3.1），未采用 v3.0.2 的 14天+$20** |
| 06-05 | **合并 v3.0.2 ERP 自动写入** | `api/campaign.py:_maybe_push_erp`（默认关，`ERP_AUTO_WRITE=true` 或请求 `write_erp:true` 开）+ settings 7 个 `erp_*` 字段 + `mcp_db_context` known_listings 回落(read_timeout≥90s) + `erp_writer` `build_wizard_direction_content_json`/auto_push 导出 + `listing_context` KNOWN_LISTINGS 集中化 |
| 06-05 | **KB Clearance 移除** | `docs/knowledge_base/12-输出规范.md` 核心策略标签枚举删 Clearance（是场景/产品阶段，非广告目的；广告目的由 ad-purpose-agent 权威产出仅 4 个）；`kb_loader.py` 注释同步；**Maintain 保留** |
| 06-05 | **ad_keyword_report 注释** | `mcp_mapping.py` 加注：MCP 拆分→直调返 Unknown tool→按 META_KW_AD 稳定回落 Doris，**非 bug**，暂不动 |
| 06-05 | **MCP/LLM 并发上调** | `mcp_max_concurrency: 8→80`、`llm_global_concurrency: →420`、连接池 `max_connections: →600`（面向批量并行) |

---

## 4. 已知问题与待办

### 4.1 Windows asyncio 缺陷（已修复）

| 问题 | 修复 | 说明 |
|------|------|------|
| ProactorEventLoop 下 asyncio 取消 httpx recv 不生效 | `main.py` 切 `SelectorEventLoopPolicy` | 在 `asyncio` 创建前设置，Windows only |
| 多 ASIN 并行时批量 LLM 打满信号量级联死锁 | ~~Tier 2 campaign 全局闸 `Semaphore(30)`~~ → **2026-06-04 收口到 client 层** `Semaphore(llm_global_concurrency)`，所有 `chat()` 过闸（campaign `_GLOBAL_LLM_SEM` 已删，详见主交接文档 §7.5） | 全服务统一，非仅 campaign |

### 4.2 功能待办

| 任务 | 优先级 | 说明 |
|------|--------|------|
| ~~策略总览(执行总纲)恢复~~ | ✅ 已完成 | 2026-06-08 恢复 `campaign_overview_enabled=True`，并改造为"判断驱动"(去现状数字复述)。详见 §7.1 |
| ~~Synthesis 汇总合成恢复~~ | ✅ 已完成 | 2026-06-08 恢复 `_SYNTHESIS_ENABLED=True`，并改造(去 KB / 组数 5-7 / special≤5-15 / max_tokens 8192)。详见 §7.1 |
| R3 tiebreaker 端到端验证 | P1 | `_same_direction` 变严后会首次真触发，需构造分歧用例 |
| 策略上下文→决策联动 | P1 | KB 19/21/22 缺策略联动规则（KB 03 已在 campaign preset 外） |
| ~~**新增广告活动分析**~~ | ✅ 已完成 | 2026-06-10/11 实现（KB 16+06，三股并行独立分析线）。详见 §9。**剩余子项**：建议竞价(suggestedBid)字段当前无源→bid 占位 $0.30，数据源到位后改 `_calc_initial_bid` 一处；`KEYWORD_PROMOTED_FROM_BROAD` 等 5 个触发场景未实现（需搜索词聚合/KB08/Custom词池）；新增活动预算未接入组合回算(KB23 §5.1)；ERP 写入未接 |
| DB 落库 | P1 | `t_advert_agent_campaign_analysis` + `_adjustment` 表 |
| L420 投票 key 同源化 | P1 | `tiebreaker_summaries` 依赖 LLM 回显 campaign_key |
| **`is_core` 核心词真实数据源** | P1 | 模型字段 `CampaignUnit.is_core: bool` 已定义（model L122），reasoner 已透传至 LLM prompt（reasoner.py L1428-1429）+ synthesis 输出。但**写入端硬编码 `False`**（campaign.py L770：`is_core=False`）。需 (a) 确定数据源（Doris keyword_library 字段 / LLM 从 purpose-agent keyword_class 判定 / 运营手动标注）；(b) 填充 `_campaign_to_prompt_dict` 调用处的真实值 |
| `POST /campaign/confirm` 落地 | P2 | MySQL pending 表 + ERP 推送 |
| KB 遵循度评分器 | P2 | 消费实验 JSONL |
| **A1 修复** | P1 | `_ensure_data` 不支持 meta_filter+缓存共存,修复后 Campaign 可走 ctx 路径命中 Redis |

### 4.3 新增设计决策 (v1.3)

| 决策 | 说明 |
|------|------|
| action 归一化 | LLM 只给淘汰判据 (`eliminate_to_low_bid_pool`),其余 action 由代码从 proposed vs current 差值 derive |
| 组合分类顺序 | 低价捡漏组 → 自动广泛组 → 精准测试组 → 精准主力组(命中即止)。精准组按 **KB23 §3.1 纯预算 $5 分界**(≥$5 主力 / <$5 测试),**不再用 days_online 判新建**。枚举名 2026-06-05 已改(详见 §3.4) |
| 淘汰组不参与预算约束 | KB 21 §6 每活动固定 $1,与运营策略预算无关。3 组占比(60/20/20)之和 = 100% = 总约束 |
| meta_filter 优化 | ASIN data 只拉 META_AD_PRODUCT,砍掉 Campaign 用不到的 6 个 META(natural_rankings/flow_keywords 等 150s 慢查询) |
| 空 CampaignData 不入 Redis | 防 MCP/Doris 临时失败毒化缓存(30min 空窗口) |

### 4.4 失败路径覆盖矩阵（多层防护后）

| 失败点 | 旧行为 | 新行为 |
|---|---|---|
| MCP 上下文挂起 | analyze 永远挂 | 300s timeout → 返回空 CampaignData + warning |
| Doris MySQL 断连 | 500 + 堆栈 | warning「分析失败: OperationalError」+ 空 result |
| `aggregator.fetch` 120s 超时 | 500 | warning「数据拉取超时」+ 空 result |
| Dorisfallback 串行慢查询 | 120s 超时断 | meta_filter 砍掉 6 个 META→<30s,不再触及超时 |
| LLM 单批挂 60s | 该 batch 失败但其他 OK | 同（既有） |
| LLM 返回非 dict | AttributeError → 整流崩 | 转单批失败 warning |
| LLM action 不一致 | 投票阶段分歧增多 | `_normalize_action()` 代码 derive,降低无谓 R3 |
| `TargetAcosRecommender` 内部 AttributeError | 500 | warning「分析失败: AttributeError」+ 空 result |
| sanity / synthesis | 挂 10 分钟无返回 | flag 禁用 → 跳过 + warning |
| exact 流抛异常 | broad 流被 cancel | broad 流照常完成，exact warning 入栈 |
| state 查询 MySQL 抖动 | 500 | warning + 空 result |
| KeyPool stats 统计死锁 | 8 个 R3 task 全挂 | RLock 可重入 → 不死锁 |
| MCP 临时失败 → 空 CampaignData 进缓存 | 后续 30min 看空 | 空结果不入缓存,下次重拉 |

---

## 5. 调试与测试

### 5.1 测试 ASIN

| ASIN | 活动数 | 特点 |
|------|--------|------|
| B0B7S3PWWB | 102 | Fishnet Stockings，数据最全，已验证 (精准流为主) |
| B0CGH9QRKK | 58 | 广泛流为主 (29 exact + 29 broad)，已验证预算汇总 |

### 5.2 启动与调试

```bash
cd ad-direction-agent
uvicorn app.main:app --reload --port 8008

# 调试前端
http://localhost:8008/demo/campaign_test.html

# 注意：不要边跑分析边改代码，uvicorn --reload 检测变更会杀进程
```

### 5.3 服务日志

日志文件：`ad-direction-agent/logs/server.log`

关键日志模式：
```
Campaign timing [ASIN] +Xs: DONE fetch_campaigns     # 数据拉取耗时
Campaign timing [ASIN] +Xs: split: exact=N broad=M   # 分流统计
Campaign timing [ASIN] +Xs: DONE exact+broad streams # LLM 分析耗时
Campaign timing [ASIN] +Xs: DONE sanity_check        # Sanity 耗时
Campaign timing [ASIN] +Xs: DONE budget_summary      # 预算汇总耗时(新增)
Campaign timing [ASIN] +Xs: DONE total               # 总耗时
Campaign batch LLM 成功 [ASIN], N items              # 单批 LLM 完成
Sanity check [ASIN]: 低置信 N/M 条 → K 批            # Sanity 触发
Batch N [ASIN] action 归一: 改写 N/M 条                # action 归一化(新增)
```

---

## 6. ERP 对接（已接入 auto_push，2026-06-05）

Campaign 分析成功后可选 write_full 到 ERP 测试库（`api/campaign.py:_maybe_push_erp`，`ERP_AUTO_WRITE=true` 或请求 `write_erp:true` 触发；详见主交接文档 §7.6）。结果→待确认表映射：

| Campaign 输出 | 目标表 | 字段 |
|--------------|--------|------|
| `eliminate_to_low_bid_pool` | `t_advert_agent_modify_campaign_pending` | STATE: ENABLED→PAUSED |
| `adjust_bid` | `t_advert_agent_modify_keyword_pending` | BID: old→new |
| `adjust_budget` | `t_advert_agent_modify_campaign_pending` | BUDGET: old→new |
| `adjust_placement` | `t_advert_agent_modify_placement_pending` | percent: old→new |
| `negative_keywords` | `t_advert_agent_modify_keyword_pending` | STATE: NEGATIVE |

所有记录通过 `decision_id` + `batch_no` 与 `t_advert_agent_decision` 关联，走 `PENDING→CONFIRMED` 审批流。

---

## 7. 2026-06-08/09 迭代：总览/汇总恢复 + 前端重构 + 免跑加载

### 7.1 overview + synthesis 恢复并改造

**恢复**：`campaign_overview_enabled=True`(settings.py)、`_SYNTHESIS_ENABLED=True`(campaign.py)。禁用根因(Windows asyncio 取消挂死)已由 `main.py` SelectorEventLoopPolicy 根治；两者均带 `timeout_override=55` + fail-open(失败仅 warning，不影响明细)。

**synthesis 改造**(`reasoner.py recommend_campaign_synthesis` / `_CAMPAIGN_SYNTHESIS_PROMPT`)：
- **删 KB**：synthesis 只做"读已有 action/reason → 语义聚类 → 写叙事"，不做广告判断，不需要 `campaign_adjustment` 那 26K KB；降 token / 延迟 / 超时风险。
- **reason 完整不截断**(原 `[:80]`)——理由是分组命脉。
- **max_tokens 4096→8192**——容纳 100 个 campaign_key 互斥铺到各组 + 叙事。
- **组数 "5-7 组"**：按实际共同原因数决定，不为凑满硬拆、不把不同原因硬并；严禁产出理由实质相同的冗余组。
- **special_cases 硬编码"控制在 5-15 条"**(运营反馈特殊调整 20-30 条处理不过来；曾试动态 ≤总数10%，最终改回硬编码)。

**overview 改造**(`reasoner.py` + `campaign.py _run_overview` + `models/campaign.py`)：
- 从"现状数字复述"改为 **"KB×现状 → 判断 → 定调"**。
- 输出字段 `status/purpose/direction` → **`assessment`(判断) + `direction`(方向) + `posture_brief`(给后续逐活动 LLM 的指令基准)**；模型 `CampaignStrategicOverview` 删 `status_text/purpose_text`、加 `assessment_text`。
- prompt 硬约束：重判断轻数字、**禁复述现状数字**、缺数据(N/A)不臆测。
- `_build_overview_facts` **补 `target_keyword_strategy`**——让 `facts` 成为前端"当前状态(只读)"面板的**统一数据源**(原缺该字段)。

### 7.2 前端重构(`demo/campaign_test.html`)

- **双 Tab**：策略总览(执行总纲) + 分析概览 **固定顶部**(不随 tab 切换)；其下「明细 / 汇总」tab(`switchCampaignTab`)。明细=多维筛选+组合气泡+批量审核+活动列表；汇总=AI 汇总叙事。
- **组合预算汇总卡移到固定区**(原在汇总 tab)——勾选时实时可见；配套修复"重跑清场漏 `hide('campaignBudgetSummary')` 导致旧数据残留"。
- 总览只展示「判断 + 方向」，`posture_brief` 不展示给运营(它是写给后续 LLM 的，注入各批 preamble)。
- 明细卡 Bid/Budget 由左右两列改 **单列紧凑**(原分太开，运营视线飘)。

### 7.3 汇总 ↔ 明细 联动

- **汇总组**：`1.2.3` 序号 + 「☑ 全选本组到明细」(`selectGroup`，纯勾选不跳转) + 「查看成员活动」每个 key 可点跳转(`jumpToGroupMember`)。
- **特殊调整**：`1.2.` 序号 + 活动名做成可点链接(`jumpToSpecial`)，**跳转即自动勾选**。
- **跳转高亮**(`jumpToDetail`)：切明细 tab + 滚动定位 + **`IntersectionObserver` 等卡片真正滚进视口(≥60%)才播放**浅黄闪烁(2 次、1.2s)——规避平滑滚动途中动效被掩盖；目标被筛选藏住则临时重置筛选保证跳得过去。

### 7.4 组合气泡增强

- 每气泡内加 **「调整前 $X | 调整后 $Y」**：调整后与"执行后预计总预算"**同逻辑**(勾选→proposed / 未勾→current)，随勾选动态重算；低价捡漏组不参与(占位"—")。
- **约束 = AI 推荐 + 运营改/执行(占位)**：仅**选中(active)**气泡才显示 [改]/[执行]；[改] 为 inline 编辑([保存]用 `onmousedown+preventDefault` 抢在 blur 前、失焦不保存)；[执行] 占位 stub(后端 override 表待接)；覆盖值前端临时态 `_portfolioOverride`。
- 4 气泡统一大小(flex 等分 + 圆角矩形)、**浅天蓝底黑字**。

### 7.5 免跑加载(调前端提效)

- 痛点：每次调前端都要等几十分钟真分析。
- `loadDebugResult`：优先 localStorage(上次真跑缓存) → fetch 同目录 `campaign_sample.json` → `renderResult`，**跳过 analyze**；analyze 成功自动写 localStorage。
- 样例 `demo/campaign_sample.json`：完整 result，含手动注入的 synthesis(真实 campaign_key)/overview(新字段)/facts(完整策略上下文)/budget_summary。

### 7.6 独立展示版 `demo/前端样例展示/`

- **自包含、双击 `file://` 即用**(不依赖端口/后端)。关键：浏览器在 `file://` 下禁止 `fetch` 本地 json(CORS)，故把数据包成 **`campaign_sample.js`**(`window.CAMPAIGN_SAMPLE = {…}`)，用 `<script src>` 引入(script 标签不受 file:// 限制)。
- 「加载状态 / 运行分析 / LLM 温度」**禁用**；左上「加载状态」位换成 **「⚡ 加载样例展示」**。
- **数据更新需同步** json→js：
  ```bash
  cd demo/前端样例展示
  python -c "raw=open('campaign_sample.json',encoding='utf-8').read(); open('campaign_sample.js','w',encoding='utf-8').write('window.CAMPAIGN_SAMPLE = '+raw+';\n')"
  ```

> **注**：7.1 的 overview/synthesis 改造与 7.4 的约束 override 后端落库，均需真跑(走真实 LLM)/接后端 override 表后再端到端验证。

---

## 8. 执行层落地 ERP — 架构共识与待办 (2026-06-09 团队定稿)

> 6.9 讨论 + 后续梳理的团队共识。**取代**「定时跑完全部工作流→全部落库→ERP 渲染」的原方案（已否决）。

### 8.1 核心决策：前置缓存 + 执行层走库（定时跑、T+1 看）

按工作流阶段**分层处理**（不是一刀切缓存或一刀切落库）：

| 阶段 | 内容 | 实时性 | 存储 |
|------|------|--------|------|
| **前置工作流 (1)~(4)** | (1)产品定位/阶段/淡旺季 →(2)广告目的/关键词 →(3)目标ACOS/父ASIN预算 →(4)广告方向 | **需实时交互**（(1)(2)长期配置，日常高频在 (3)(4)） | **走缓存，即得即用** |
| **执行层 (5) campaign** | 广告活动分析（单父ASIN **40-70 次 LLM**、耗时长、终态无后顾之忧） | 不需实时 | **走数据库，每晚定时跑，运营第二天看** |

**运营使用节奏（T+1 批次制）**：每天看**前一天**的执行层分析 → 点击确认执行；配好**当天**的前置配置 → **当晚自动跑**；不配则用默认值。

**并发说明**：执行层支持并行但有上限——i/o 密集型（绝大多数时间在等 MCP/LLM 响应），当前 6 workers，**加 worker 用处不大**。

### 8.2 后端待办

> 业务数据库已有整体骨架（~90%），多数为**补充**。

| 项 | 说明 |
|---|---|
| **配置表** | 驱动 campaign 定时分析（哪些 ASIN / 参数 / 调度） |
| **渲染表** | 存"上一次定时分析"的快照，供前端渲染；快照含**前置配置结果值**（与调试前端"加载状态"同源） |
| —（渲染/配置表）兼作 | **为后续记忆系统预留依据** + **为质检 agent 预留依据** |
| **写入数据库脚本** | 已有，补充即可 |
| **新增"广告活动 agent 分析"功能** | 数据管道 + 功能设计 |
| **淘汰活动重启功能** | 不频繁跑（见 §4.2 / KB 21 §7） |
| **缓存** | 前置工作流需要 |
| **全链路日志设计** | — |
| **测试 / 质检 agent** | 为另一个项目预留接口，方便被调用 |

### 8.3 前端待办（预计 2-3 天）

**主要功能（大头）**：
1. **合并 campaign 看板到主看板**（统一入口，作第 5 tab）；
2. **补全 campaign 看板占位功能**：当前很多处仅占位、只有展示效果、无实质功能，需补全。

**细节优化（小头）**：
1. **跳转自动传父 ASIN**：从接口点击跳转后自动带入父 ASIN，无需手动输入（复用 ERP 前端已有设计）；
2. **支持「近一次分析」与「实时分析」两种视图**（做成类似 ERP 前端的效果）；
3. **区分操作台 / 快照查看区**：同一块区域——「实时分析」时是**操作台**（可执行），「近一次执行层分析」仅作**快照查看**（只读）。

> 第 3 点正好解决"campaign 是 T+1 快照、与当天前置不同时间基准"的困惑：实时=操作台、近一次=只读快照，语义清晰。

### 8.4 现有资产处理

| 资产 | 处理 |
|---|---|
| 已建业务数据库 | **复用**（配置表/渲染表在其上补充） |
| ERP 前端 | **部分设计复用**（自动传父ASIN、近一次/实时视图、快照区等交互参考）；整套退役 |
| ERP 写入脚本 / pending 执行链 | **保留**（执行落地：写 pending → ERP 后台执行） |
| ERP 接口 | **IT 负责** |

### 8.5 待展开
- 记忆系统梳理（渲染/配置表已为其预留依据）
- 质检 agent 接口（后端已预留）

---

## 9. 2026-06-10/11 迭代：新增广告活动分析线（KB 16）+ 预过滤可见化

### 9.1 新增活动分析线（KB 16 + 06）— 三股并行独立管道

**目标**：在只分析"已有活动"之外，补上"该 ASIN 应**新建**哪些活动"的建议。

**编排位置**：在 `strategic_overview` 之后，与精准流/广泛流**三股并行**（共享 `asyncio.gather(return_exceptions=True)` + 同一 `ctx_dict`，posture_brief 一致注入）。fail-open：任何阶段失败仅 warning，不连累主分析。

**数据流**（`campaign_new.py:analyze_new_campaigns`）：
```
候选词发现 (discover_new_keywords: flow_keywords + own_keyword_flow 并行)
  → 硬过滤 (KB 16 §6 阻断: 库存<7/退货≥30%/评分<3.8/清货期; + 去重已投词/去噪/搜索量<50)
  → trigger_scene 标注 (KB 16 §1, 仅展示标签非门禁)
  → 排序(有自然位优先+搜索量降序) + Top-N 截断 (campaign_new_max_count=20) ← 真正的量控
  → ★双轮 LLM 取交集 (R1+R2 不同随机种子, 两轮都判"建"才保留, 降幻觉)
  → 代码补齐: match_type(由keyword_class推导) / bid / budget / campaign_name / placement / 归组
```

**职责切分**（关键设计）：
- **LLM 只判**：`action`(create/skip) + `keyword_class`(KB 06 五类) + reason/evidence/negative_strategy。
- **代码确定性产出**：bid/budget/campaign_name/match_type/primary_placement/归组——不进 LLM。

**KB / 数值规则**：
| 项 | 规则 | 实现 |
|---|---|---|
| preset | `new_campaign = ["16","06","02"]` (LLM 不算数值故无 15/19/23) | `kb_loader.py` |
| 默认预算 | $3.00 (KB 16 §2) | `DEFAULT_NEW_BUDGET` |
| 初始 bid | `min(0.5, 建议竞价×0.5)`，下限 $0.20 (KB 16 §3) | `_calc_initial_bid` |
| **建议竞价字段** | MCP/Doris **当前无源** | **占位 $0.30**；TODO(suggested-bid) 数据源到位改一处 |
| match_type | keyword_class→映射 (KB 06+16§4): brand/custom→EXACT, generic→BROAD 等 | `_derive_match_type` |
| primary_placement | 仅 EXACT，代码默认"头部" (KB 16 §4 首轮只声明主投位，不加价) | 不交 LLM |
| 命名 | `匹配类型-关键词-YYYY-MM-DD`（运营拍板，无 ASIN），如 `精准-fishnet stockings-2026-06-11` | `_generate_campaign_name` |
| 置信 | keyword_class 两轮一致=high/AUTO_BATCHABLE；不一致=low/MANUAL_REVIEW | 交集组装 |

**触发场景**：本期标注 3 类（RANKING_OPPORTUNITY_NO_EXACT / SEASONAL_ADVANCE_BUILD / FILL_AFTER_ELIMINATION）+ 兜底 KEYWORD_POOL_EXPANSION；其余 5 类（KEYWORD_PROMOTED_FROM_BROAD 需搜索词聚合 / COMPETITOR_INTERCEPT_WINDOW 需 KB08 / CUSTOM_KEYWORD_POOL 需运营词池表等）待数据源接入，有 TODO 标记。

### 9.2 投放目标子 ASIN（`pick_target_child_asin`）

新建活动须挂到具体子 ASIN（非父 ASIN 占位）。判据（运营定）：**历史活动数最多优先，平手按 7 天花费最高**（复合排序 `(活动数, perf_7d.cost)` 双降序）。数据取自 `campaign_data.campaigns`（已 MCP 拉过 perf）。

### 9.3 预过滤可见化（第一期 A+B）

**背景**：原 `skipped_eliminated`（淘汰池）与 `excluded`（多词等）未在前端展示。本期让被预过滤的活动**也在前端可见**（灰色不可操作，只标签+原因），与 LLM"已丢失"项区分。

| 类别 | 数据源 | 本期 | 说明 |
|---|---|---|---|
| **A 已入淘汰池** ($1/$0.20) | `skipped_eliminated` (campaign.py) | ✅ | 打 `__prefiltered`，reason="已入淘汰池…请到ERP手动修改" |
| **B 多词活动** | `excluded` (prefilter) | ✅ | **折叠去重**(每活动1条,防 raw 多行 N 重复)+补维度字段+`keyword_text="多关键词活动"`+词条数最多子ASIN(prefilter 无 perf 故"花费最高"不可得) |
| **C 非 ENABLED** | — | ❌ 未做 | `_fetch_campaign_context` SQL 已 `WHERE campaign_status='ENABLED'` 且输出列写死 ENABLED → raw 不含；要展示**须改 SQL**(去 WHERE+取真实 status)，prefilter 规则1 才转活 |
| **D 近7天无数据** | — | ❌ 未做 | report 表对零活动**无行**，且**无 campaign 主表**可 LEFT JOIN；放宽窗口语义也变。需先确认数据口径，prefilter 规则2 当前为死代码 |

> A/B 现有数据齐全、零额外查询；C/D 需数据层改造，运营已确认本期不做。

### 9.4 前端（`demo/campaign_test.html`）

- `new_campaigns` 映射为 adjustment 形状复用卡片渲染器：绿色 `create` 卡 + `sumNew` 统计 + 筛选「新增」+ `actionLabel`/klass/badge 全配齐。
- **主投位/否词策略专用渲染** `renderNewCampaignExtras`：不套调整型 placement/否词模板（避免假"0%→100%"违反 KB16§5、及字段名对不上的 `?` 占位垃圾）；渲染为「主投广告位: 头部（首轮不加价）」+「否词策略: …」。
- skipped 分两类：`prefiltered`(灰卡、无悬停) vs `lost`(红色⚠悬停)；概览行显示「预过滤 N | 丢失 M」。
- 样例 `demo/campaign_sample.json` 已注入展示数据（4 新增 + 23 预过滤 + 1 丢失）。**注意** `loadDebugResult` 优先读 localStorage `campaign_debug_result`，看样例需先 `localStorage.removeItem('campaign_debug_result')`。

### 9.5 涉及文件

| 文件 | 改动 |
|---|---|
| `app/workflow/steps/campaign_new.py` | 新建：全流程 + `pick_target_child_asin` |
| `app/llm/reasoner.py` | `_NEW_CAMPAIGN_PROMPT` + `recommend_new_campaigns()` |
| `app/data/campaign_fetcher.py` | `discover_new_keywords()` + `_last_shop_account` |
| `app/llm/kb_loader.py` | preset `new_campaign=["16","06","02"]` |
| `app/models/campaign.py` | `NewCampaignCandidate`/`NewCampaignItem`(含 child_asin) + `CampaignAnalysisResult.new_campaigns` |
| `app/config/settings.py` | `campaign_new_enabled`/`_batch_size`/`_max_count` |
| `app/workflow/steps/campaign.py` | 三股并行 gather + 淘汰池打 `__prefiltered` + 合并 `excluded` |
| `app/data/campaign_prefilter.py` | 多词去重+补字段+`__prefiltered` |
| `demo/campaign_test.html` | new_campaigns 映射/绿卡/sumNew/筛选 + 预过滤分流灰卡 + `renderNewCampaignExtras` + 修复 `skipped` 变量遗留 bug |

---

## 10. 2026-06-11 (续)：广告位加价比例数据接入 + 代码回填 + 前端合并

### 10.1 广告位加价比例字段接入

**背景**：广告位调整建议一直产出一律 `0% → 0% (维持)`。排查发现两个原因叠加：
1. `ad_campaign_basic_info` MCP 新增了 `头部位置加价比例` / `商品位置加价比例` / `其他位置加价比例` 字段，但代码未解析。
2. LLM 被要求输出 `current_pct` 数字，但 prompt 无法可靠约束 LLM 从输入复制结构化数值。

**修法**：数据接入 + 代码回填 `current_pct`，LLM 只负责方向判断。

**改动链**（4 文件，~85 行）：
| # | 文件 | 改动 |
|---|---|---|
| ① | `models/campaign.py` | `CampaignUnit` 加 `tos_bid_pct` / `pp_bid_pct` / `ros_bid_pct`（默认 0.0） |
| ② | `campaign_fetcher.py` | 三条 basic_info 生产路径各补 3 个字段：MCP 路径 `_to_float(row.get("头部位置加价比例"))`，Doris-only 和 fallback 路径填 0.0；`_assemble` 透传 |
| ③ | `reasoner.py` | `_campaign_to_prompt_dict` 注入 `_placement_pcts: {头部:N, 商品:N, 其他:N}`；prompt 加 `当前加价比例` 行；placement schema 改：LLM **只输出 `action`（维持/小涨/大涨/小降/大降）+ `evidence`**，禁止输出 `current_pct/proposed_pct` |
| ④ | `campaign.py` | 新增 `_backfill_placement_pcts()` 回填函数——`current_pct` 从 `CampaignUnit` 取真实值，`proposed_pct` = `current + action步长` 受 KB 07 边界裁断（头部≤30/商品≤10/其他≤15，≥0）；`_ACTION_STEP` 步长: 小涨+5pp/大涨+10pp/小降-5pp/大降-10pp；`_PLACEMENT_NAME_MAP` 中英文 placement 名归一；两个 return 路径各调一次 |

**步长（运营未定，本期临时值，替换点 `_ACTION_STEP`）**：
| action | 步长 |
|---|---|
| 小涨 | +5pp |
| 大涨 | +10pp |
| 小降 | -5pp |
| 大降 | -10pp |
| 维持 | 0 |

**效果**：精准活动 `cur>0` 从 0 升至 43（代码回填），LLM 产出 placement 比率约 41/69（prompt 待加强令覆盖率到 100%）。

### 10.2 关于 `_to_float` vs `_to_pct`

MCP 的加价比例字段返回**百分数口径**（`9.0` = 9%），不和其他 MCP 百分比字段一样走 `_to_pct`（`_to_pct(9.0)` → `900.0` ❌），**必须用 `_to_float`**。

### 10.3 前端合并 (campaign-panel/)

`campaign_test.html` 的渲染逻辑迁移为 **ES module** 独立模块 `demo/campaign-panel/`，通过主看板 `ad-asisitant-agent.html` L2672 的 `<script type="module">` lazy-mount 到第 5 tab。

**文件**（均在 `demo/campaign-panel/`）：
| 文件 | 说明 |
|---|---|
| `panel.js` | 唯一入口 `mountCampaignPanel(el, {asin, days, mode})`，创建骨架→状态→事件→请求→返回 API |
| `viewmodel.js` | `normalizeViewModel(raw)` 兜底归一（仅 null 填充 + 旧格式兼容），`PORTFOLIO_NAMES` 常量 |
| `state.js` | `createCampaignState()` 工厂：全部状态 + 突变方法 |
| `render.js` | 全部渲染函数 + `_backfill_placement_pcts` 对应的前端 `_renderNewCampaignExtras` |
| `events.js` | `mountEventDelegation(root, state)` — 1 个 click + 1 个 mousedown + change/input/keydown/blur 捕获，按 `[data-action]` 分发 |
| `panel.css` | 全部 `.camp-` 前缀 CSS，`.camp-root` 包裹 |

**关键设计**：
- **双轨渲染**：`mode: "interactive"` (操作台，显示复选框/执行按钮) / `"readonly"` (快照只读，隐藏操作件)。前端不感知数据来源。
- **后端适配层** `app/api/campaign_viewmodel.py`：字段映射 + 枚举翻译 + 来源差异消化（`primary_placement` → `placement_adjustments`+`is_declaration`），前端见统一 ViewModel。
- **快照接口** `GET /campaign/snapshot?asin=&decision_id=`：stub 实现，后端 mapper 另排期。**当前 tab5 默认 `interactive` 模式**，snapshot 接入后改 `readonly`。
- **原 `campaign_test.html` 保留**作独立调试页。
- **CSS 全部 `.camp-` 前缀**，无污染主看板风险（`review-approve/reject` 已加 `.camp-root` 作用域）。

---

## 11. 2026-06-12：决策批次驱动 + 快照回读 + confirm 写回

### 11.1 决策批次状态机（P1-P2，详见 `docs/决策批次改造方案.md`）

**模型**：`run_id` = 批次句柄（分析启动时即存在），`decision_id` = 写库时生成（ERP 表主键）。进行中状态落 state 库 `analysis_session`（asin → run_id），不污染 ERP decision 表。

**A/B/C 三态**：
| 场景 | 左侧 1-4 | tab5 |
|---|---|---|
| A 首访/未配置 | 可编辑 | 锁定 |
| B 已配置·无进行中 | 只读快照 | 可交互·可执行（选中 `is_latest` 批次） |
| C 有进行中 | 可编辑 | 全锁（旧失效·新未完成） |

**执行权**：`executable = is_latest AND NOT state.get_in_progress()`

**关键 API**：
- `GET /decision/context` — 批次列表 + 三态判定
- `POST /decision/new-event` — 新建分析（写 state session，清 3-4）
- `POST /decision/cancel-event` — 取消分析（清 state session）
- `GET /decision/{id}/preset` — 前置 1-4 配置快照（ERP 码→中文 label）

### 11.2 快照回读 mapper（`campaign_viewmodel.from_db_snapshot`）

21 表 → ViewModel 反向映射：
- card + 3×pending 子表 → 统一 `items`
- `suggest_category` + group_type 兜底 → `action` 推导
- summary 8 列 → `portfolio_constraints` 重组
- `_f()` 处理 Decimal→float JSON 安全
- overview 读 `analysis_overview` 列
- 快照是实时子集（`prefiltered`/`lost`/`perf_7d` 可能缺），前端按字段有无降级

### 11.3 `/campaign/confirm` 写回

校验批次可执行 + 每活动幂等（`confirm_status='PENDING'` 限），写 card + 3×pending `confirm_status`。

### 11.4 `finalize_batch` 闭环

`_maybe_push_erp` 成功后 → `finalize_batch(report.decision_id, asin, analysis_mode)` → 旧批次 `is_latest=0`、新批次 `is_latest=1` + 清 state session。

### 11.5 ERP 库改造摘要

已落地列：`is_latest`/`analysis_mode`、card `keyword_class`/`review_level`/`is_core`/`perf_json`/`is_prefiltered`、summary `create_count`/`analysis_overview`/portfolio 8 列、`decision_config` P4 字段、synthesis collation 统一。

### 11.6 前端合并（`campaign-panel/`）

tab5 用统一的 `data-action` 事件委托 + `executable` 门禁替代原来的 `mode` 门禁。in-panel 模式条已移除，改由页面级批次选择器驱动。

---

## 12. 2026-06-12 (续)：建议竞价 MCP 接入 + 前端闭环 + 稳定性修复

### 12.1 建议竞价 MCP 接入（KB 16 §3 数据源）

`whp_amazon_advert_keyword_suggest_bid` 工具已上线。候选词截断后（≤20）一次批量查询，命中词填入 `cand.suggested_bid` → `_calc_initial_bid` 自动按 `min(0.5, bid×0.5)` 计算真实出价。未命中词降级 `$0.30` 占位（关键词不在亚马逊建议竞价数据库覆盖范围内）。实测命中率 ~70%（14/20）。

### 12.2 前端闭环接线（`0119404` 提交）

`new-event` 创建的 `run_id` → C 态 tab5 "运行执行层分析"按钮 → `mountCampaignPanel({write_erp:true, run_id})` → analysis 完成 → `finalize_batch` 翻 `is_latest` + 清 session → `onComplete` 刷新 context → 自动切 B 态快照。

### 12.3 前置配置只读快照渲染（P3）

`renderReadonlyDecisionPreset(decisionId)` 调 `/decision/{id}/preset`，渲染战略/策略/P3/广告方向为只读 badge 标签，隐藏保存/AI 推荐按钮。

### 12.4 稳定性修复

| 修复 | 说明 |
|---|---|
| session TTL 12h | `get_analysis_session` 超期自动清，防运营执行权永久冻结（MySQL + JSON 双后端） |
| userId 接线 | `batchConfirm` 的 `operator` 从硬编码 `'tab5'` 改为 `window._erpParams.userId` |
| state session 方法 | `set_workflow_state`（非 `save_`）、`clear_p3_recommendation` 双后端补全 |
| SQL 迁移 | `is_latest` 回填用 `ROW_NUMBER() OVER (PARTITION BY parent_asin)` 窗口函数 |

### 12.5 待完成

- P3 前置只读渲染完成闭环（B 态初始加载时自动调 `renderReadonlyDecisionPreset`，当前仅切换批次时触发）
- P4 定时调度器（`decision_config.enabled` 列已备，APScheduler 扫表）
- `analysis_overview` 写入端补（列在但 `write_full` 不填）
- `ensure_schema` 健壮化（`--` 注释 chunk 被整段跳过）

---

## 13. 2026-06-12 (续)：自然排名（周排名）接入精准活动分析

### 13.1 缺口
两条互不相通的排名路径：ASIN 级（wizard tab2，有 natural_rank/near/change）vs campaign 级（tab5，**无**）。且 `campaign.py:_do_analyze` 的 `aggregator.fetch(..., meta_filter=["META_AD_PRODUCT"])` 把排名 META 砍了 → campaign 路径**连排名数据都没拉**。KB 07/19§5/22§2.2 的 Ranking 规则依赖排名，LLM 只能靠 perf 反推——真实信息缺口。

### 13.2 修复（全 MCP 不碰 Doris，纯精准）
旁路拉取 → 挂活动 → 进 prompt：
| 文件 | 改动 |
|---|---|
| `data/campaign_fetcher.py` | `_fetch_keyword_ranks`：主源 `keyword_child_asins`（含近次→周变化），`own_keyword_flow` 补缺（仅当前排名）；返回 `{kw_lower:{natural_rank,near_natural_rank,rank_change}}`。`fetch_campaigns` 旁路 task 与 basic/perf 两波 gather **重叠**；`_assemble` 仅 EXACT 按 keyword join |
| `models/campaign.py` | `CampaignUnit` + `CampaignAdjustmentItem` 各 +`natural_rank`/`near_natural_rank`/`rank_change` |
| `llm/reasoner.py` | `_campaign_to_prompt_dict` 注入 `_natural_rank`（仅 EXACT）；活动列表三态渲染行（有排名/已掉榜/无数据不渲染）；EXACT prompt 加 RANK 决策指令 |
| `workflow/steps/campaign.py` | 回填 adjustment 3 字段 + 追加排名证据行（走 `card.evidence` 落库，**快照轨零改可见**） |
| `api/campaign_viewmodel.py` | `_item_from_adjustment` 透传 3 字段（实时轨） |
| `demo/campaign-panel/render.js` | 精准卡 meta 行显示排名 + `_rankArrow` 周变化箭头（↑绿/↓红/持平灰） |

### 13.3 关键性质
- **周语义** = `near_natural_rank`(上次爬取) vs 当前的 delta，与 tab2 同口径（非严格自然周环比）；
- **纯精准**：`has_exact` 门控——纯广泛 ASIN 根本不拉排名，广泛流 prompt/前端零改；
- **零串行延迟**：旁路 task 与 basic/perf 重叠；**超时 `campaign_rank_timeout=45s` 在 `_fetch_keyword_ranks` 内部**（从协程启动算，与 await 时机无关），超时/异常 fail-open 返回 `{}`，不连累主分析；
- **子 ASIN 精度**：child_asins 返回该词最优子 ASIN 排名（ASIN 级口径），与活动绑定 child_asin 可能不同——v1 接受（tab2 同款折衷）。

### 13.4 待真实环境确认
`keyword_child_asins` 传空 `keyword` 是否返回**全部词**排名（沿用 ASIN 级同款 builder，理论一致；本机 Doris/MCP 不可达未 live 验证）。若返回空/单词 → 改逐词查或换 `keyword_competitors`。解析/合并逻辑已**造假 MCP 响应单测通过**（child 优先 + own 补缺 + 周变化 + 掉榜 + rank≤0 脏值过滤）。

---

*最后更新：2026-06-12（v2.1: 自然排名（周排名）接入精准活动分析 §13）*
*v2.0: 建议竞价 MCP 接入 + 前端闭环 + session TTL + userId 接线 §12*
*v1.9: 决策批次状态机+快照回读+confirm写回+DRAFT→state库 §11*
*v1.8: placement 加价比例数据接入+代码回填+前端合并模块 §10*
*v1.7: 新增广告活动分析线 §9*
*v1.6: 执行层落地 ERP 架构共识 §8*
*v1.5: 总览/汇总恢复改造 + 前端双 tab §7*
