# Campaign 广告活动分析引擎 — 交接文档

> **最后更新**: 2026-06-26（版本日志见文末，最新 v3.7）
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
- **AI 分析**：基于 KB 知识库规则（18/17/15/19/22/21，精准/广泛分流切片），对每个活动给出淘汰/调整/保持建议
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

> 行数为 2026-06-26 实测（`wc -l`）。行号易漂移，引用 `file:line` 前请先 Grep 实测。

| 文件 | 行数 | 角色 |
|------|------|------|
| `app/workflow/steps/campaign.py` | 2138 | ★编排引擎：分流→分批→投票→校验→合成；action 归一化；组合分类调度 |
| `app/llm/reasoner.py` | 2112 | LLM Prompt 构建 + `recommend_campaign_batch()` + `recommend_campaign_synthesis()`（含四层工作流全部 prompt） |
| `app/models/campaign.py` | 269 | 全部 Campaign 数据模型 (含 portfolio/ai_portfolio_class/budget_summary) |
| `app/data/campaign_fetcher.py` | 869 | 数据编排器：Doris上下文→预筛选→MCP→回落（含排名旁路 `_fetch_keyword_ranks` + 竞品词源 `discover_competitor_keywords`） |
| `app/data/campaign_prefilter.py` | 92 | 硬过滤纯函数（多词去重+补维度字段+`__prefiltered` 标记，供前端预过滤卡展示） |
| `app/api/campaign.py` | 566 | API 端点（6 个）：`/campaign/analyze`·`/viewmodel`·`/snapshot`·`/confirm`·`/execute`·`/execute-portfolio-budget`（详见 §3.3） |
| `app/llm/client.py` | 275 | DeepSeek API 客户端 + KeyPool 轮询(Rlock) |
| `app/config/settings.py` | 295 | Campaign 相关配置项 (含 portfolio shares/fallback_multiplier) |
| `demo/campaign_test.html` | 1464 | 调试前端 (含组合筛选气泡 + 预算约束卡) |
| `app/llm/kb_loader.py` | 232 | KB 加载器。**2026-06-24 起 `campaign_adjustment` 已拆为 `_exact`/`_broad` 两个 preset 并做节级切片**（精准=`18:1,3 17:1,2,3,4,5,7 15:1,2,3,4 19:1,2,3,4,5,6,9 22:0,2 21`；广泛=`...15:1,2,4 19:1,2,3,4,6,7,8 22:0,3...`）。另有 `campaign_overview`/`budget_reallocation` 两个 campaign 级 preset。**早期文档写的单一 `campaign_adjustment`(18/19/21/22 或 18/17/15/19/22/21) 均已过时** |
| `app/workflow/steps/campaign_portfolio.py` | 126 | ★组合分类器：4 类 deterministic (淘汰→广泛/自动→测试/新增→主推) |
| `app/workflow/steps/campaign_budget_summary.py` | 95 | ★预算汇总：3 组约束分配 (主推/测试/广泛)，淘汰不参与约束 |
| `app/workflow/steps/campaign_new.py` | 624 | ★新增活动分析线 (KB 16/28)：候选词发现(flow/own/竞品)→硬过滤→相关性→长尾优先排序→双轮取交集→组装；`pick_target_child_asin` 选投放子ASIN (详见 §9/§22/§24) |
| `app/workflow/steps/campaign_budget_reallocation.py` | 283 | ★组合预算回算（KB23）：闸控读 `parent_allowed_net_increase`（值仍占位，§17 ③） |
| `app/workflow/steps/portfolio_execution.py` | 206 | ★组合预算调整真实执行（`/campaign/execute-portfolio-budget` 后端，06-17 新增） |
| `app/workflow/steps/advert_execution.py` | 324 | ★广告调整 MCP 真实执行（Part 6，6 工具→落 4 record 表） |
| `demo/campaign-panel/` | ~2165 | ★前端合并模块 (ES module + CSS `.camp-` 前缀 + 事件委托)，独立维护于 `campaign-panel/` 目录 (详见 §10.3) |

### 3.2 关键配置项（settings.py）

```python
# Campaign LLM
campaign_llm_concurrency: int = 50  # 单 ASIN 批次并发数（per-stream sem；2026-06-18 实测值）
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
deepseek_model: str = "deepseek-v4-flash"   # 快档(默认/非思考)；强档(总览/汇总)= flash + 思考模式（见主交接文档 2026-06-09 LLM 分级）
llm_global_concurrency: int = 420   # 服务级 LLM 总并发（client 层信号量；多 worker 时 ÷num_workers）
num_workers: int = 1                 # 读 NUM_WORKERS,把全局闸切给各 worker（须 = 启动 --workers）
mcp_max_concurrency: int = 115       # MCP 工具并发（mcp_max_connections=120 须 ≥ 此值）
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
| POST | `/campaign/viewmodel` | 实时操作台：分析→强制落库→回读快照（方向 A 收敛） | ✅ 生产可用 |
| GET | `/campaign/snapshot` | 读某决策批次执行层快照（mode=readonly） | ✅ 生产可用 |
| POST | `/campaign/confirm` | 双路审核（写 card + 3×pending `confirm_status`） | ✅ 已落地（非 stub，§11.3） |
| POST | `/campaign/execute` | 执行已确认调整 → 调广告调整 MCP（dry-run 默认空跑） | ✅ 链路通（§15.4） |
| POST | `/campaign/execute-portfolio-budget` | 组合预算调整执行 → 实时查 portfolioId → MCP | ✅ 链路通（06-17 新增） |

> 决策批次相关端点在 `api/decision.py`：`GET /decision/context`、`POST /decision/new-event`、`POST /decision/cancel-event`、`GET /decision/{id}/preset`（详见 §11）。

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
| 06-24 | **ACOS 阶段优先** | 上限由 `min(阶段,层级)` 改为阶段优先+层级兜底；清货期上限 60、测试期 50 生效（运营确认） |
| 06-24 | **KB 切片化** | kb_loader 按 `## N.` 节号切片注入，精准/广泛分流 preset（campaign_adjustment_exact/_broad），消除注意力稀释 |
| 06-24 | **长尾优先排序** | 候选词排序改为词数多者优先（非搜索量降序），修正"大词泛词霸榜"代码层根因 |
| 06-24 | **MCP 退避抖动** | mcp_adapter 重试加 50–200ms 随机抖动，防 thundering herd |
| 06-25 | **data_unavailable 区分** | fetch_campaigns 失败/超时 → `data_unavailable=True`，ERP 门禁返回独立原因，批量统计单列+退出码（>50%→rc=2）；不再伪装成 "no adjustments" |
| 06-25 | **StarRocks BE 重试** | `starrocks_retry.py` 单一真源：4 条数仓 SQL 地基统一 index-retry+抖动（errno 1064 且 starlet/BE: 签名）；SQL 语法错不重试 |
| 06-25 | **MCP 多站点日期窗口** | `make_date_window(days, site_code)`：6 站点硬编码时区（US/UK/DE/IT/ES/FR），`end=当地今天-1`、`start=当地今天-days`；消除"服务器本地时间≠数仓当地时间"的窗口错位 |
| 06-25 | **ctx UnboundLocalError** | `_prefetch_placement/search_terms` 的 `ctx` 只在 `if not shop_account:` 内赋值 → 缓存命中时崩；改走 `_last_site_code` 缓存 |
| 06-25 | **suggest_budget 空值 NULL** | `_upsert_ai_suggest` 写 `decimal(12,2)` 列时空→`""` → MySQL 1366；改为空→NULL（与同文件 `_upsert_decision` 一致） |
| 06-25 | **守夜监控常驻** | `batch_night_monitor.sh`：每日 01:00–05:00 每30min 探测批跑成功率；≥80%正常、5–80%定向重试失败/跳过 asin、<5%全量重试（含运行中先 pkill）；常驻 cron `0 1 * * *` |
| 06-27 | **新 MCP 工具接入** | `parent_listing_detail` 替代 SQL #1（父ASIN→site/sku/shop）、`ad_campaign_product_keyword_list` 替代 SQL #2+3（父ASIN→子ASIN→活跃活动+关键词）；两开关 `mcp_resolve_context`/`mcp_discover_campaigns` 默认开，失败自动回落 DB |
| 06-27 | **MCP 接入架构文档** | [docs/MCP工具接入架构范式.md](docs/MCP工具接入架构范式.md)：注册→构造→调用→返回字段透传→回落模式 全链路代码审计 |
| 06-27 | **MCP 字段安全修复** | match_type 大写归一（P1）、MCP 响应信封解包（P2）、resolve 必填字段校验 sku+shop（P3） |
| 06-27 | **写库隐患修复** | campaign_id 为空跳过（防 UNIQUE KEY 碰撞）、`load_pending_by_card_ids` 补 perf_json+trigger_rule、`_ACTION_TO_CATEGORY` 显式加 reactivate_* 映射 |
| 06-27 | **前端秒开配置栏** | `onNewEventClick` 改为 `await loadConfigOnly()` + 后台 `loadAll(true)`（不 await）；新增 `loadConfigOnly()` 只调 `loadStrategy()`（state DB + 静态配置），不调 `loadTactics()`（避免触发 `ensure_data_for_llm` 拉 MCP/StarRocks）。[demo/ad-asisitant-agent.html](AD_assistant_agent-v3.2/ad-direction-agent/demo/ad-asisitant-agent.html) |

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
| ~~L420 投票 key 同源化~~ | ✅ 已解决（2026-06-26） | 改 cid 句柄方案：LLM 回吐批内 `C1..Cn`，代码 `cid_map` 权威回填 campaign_key（不再依赖 LLM 复现长串）；并补缺轮兜底（单轮/双轮缺失送 R3，详见 §27 / 主交接 06-26 条） |
| **`is_core` 核心词真实数据源** | P1 | 模型字段 `CampaignUnit.is_core: bool` 已定义（model L122），reasoner 已透传至 LLM prompt（reasoner.py `_campaign_to_prompt_dict`，~L1527）+ synthesis 输出。但**写入端硬编码 `False`**（campaign.py:1111：`is_core=False`，行号易漂移先 Grep `is_core=False`）。需 (a) 确定数据源（Doris keyword_library 字段 / LLM 从 purpose-agent keyword_class 判定 / 运营手动标注）；(b) 填充 `_campaign_to_prompt_dict` 调用处的真实值 |
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
| preset | `new_campaign = ["16:1,6","06","02:1-6","08","28:0,2,3"]` (LLM 不算数值故无 15/19/23；28=相关性R1-R4/场景/配额) | `kb_loader.py` |
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
| `app/llm/kb_loader.py` | preset `new_campaign=["16:1,6","06","02:1-6","08","28:0,2,3"]`（28=新增词相关性/场景/配额） |
| `app/models/campaign.py` | `NewCampaignCandidate`(含 `week_rank`/`week_search_volume`)/`NewCampaignItem`(含 child_asin/`relevance_tier`) + `CampaignAnalysisResult.new_campaigns` |
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

## 14. 2026-06-13：执行层 ERP 全链路落库 + 缓存分 key + pipeline 并行 + 前端/数据修复

> 当日 6 commit + 4 未提交。**完整细节见 [docs/2026-06-13-交接文档.md](2026-06-13-交接文档.md)**。

### 14.1 ERP 执行层全链路落库接线（`53fa5b3` + 未提交 mappers）
表结构经核对 **0 加列**（`ERP数据库改造方案.md` DDL 已落地，含 synthesis 三表/card 新列/summary 新列/collation 已转 general_ci）。
- `auto_push`：payload 带 overview/synthesis/new_campaigns/skipped。
- `models`：`SuggestCardCanonical` +campaign_key/keyword_class/review_level/is_core/perf_json/is_prefiltered/prefilter_reason；`CanonicalRun` +overview_text/synthesis/create_count。
- `mappers`：cards 扩展（adjustments + **CREATE 卡 + 预过滤 prefiltered 卡**，新增/预过滤卡 `campaign_id=NULL` 避免撞 `uk(decision_id,campaign_id)`）；提取 overview/synthesis。
- `repository`：summary 写 `analysis_overview`/`create_count`；card 写 6 新列；新增 `_upsert_synthesis`（reason_group/member/special，member.suggest_card_id=card_id）；read_snapshot 读 synthesis 三表；pending 行主键种子改 `card_id`。
- `from_db_snapshot`：读 synthesis 三表；`target_budget` 取 `decision.daily_budget_suggest`（不在 summary 冗余存）。
- 真库验证：analysis_overview / review_level / is_prefiltered(39 灰卡) / create_count ✅。

### 14.2 batch_no + confidence_level 修复（未提交 mappers）
- **batch_no**：从 run_id 派生本地 `YYYYMMDD-HHMM-序号`（复刻旧格式、与历史 7 天一致、同 run_id 重试稳定），与 decision_id 解耦；不参与唯一键。
- **confidence_level**：原 `to_int("high")=None → _confidence_level → "low"` 把所有卡写成 low；改为直接用字符串 high/medium/low。**历史脏数据无法 SQL 回填，需重跑覆盖**。

### 14.3 pipeline 并行提速（`5d232dd`）
basic/perf 每活动配对并行；overview 改 gate task 与三流 prefetch 重叠；新增活动批次 `for`→`gather`（并修 `sem or Semaphore(1)` 失效限流）；sanity/synthesis 合成并行。

### 14.4 缓存按 filter 分 key（`ecc207e`）
`_ensure_data` 的 meta_filter 分支原**不读写缓存** → 同 ASIN 当天每次「加载分析」重拉。改：redis key 加 `:f<hash>` filter 维度 + 三级查找（本 filter→全量超集→fetch），refresh 删本 filter+全量。运维：Redis 未起只剩 30s LRU。

### 14.5 决策批次面板修复（`889a0db`）
B1 汇总 tab ReferenceError(state→vm) / B2 ViewModel 字段透传 / B3 localStorage GC 误删 / B5 跳转门禁 / B6 onComplete 三态 / B7 预算合计全集 / 缺口1 C 态放弃分析 / 实时切 tab 重复触发（`_campaignRealtimeRunning`）。

### 14.6 前端展示 + synthesis 截断（未提交 render/panel/reasoner）
- reason 完整显示（pre-wrap，不截断、不进展开详情）。
- 批量栏去 inline `display:none`（`_show()` 清不掉 inline → 整条栏含「全选可见」永不显示）。
- synthesis 截断（Unterminated string）：精简输入字段 + `max_tokens` 8192→16384 + timeout 240。

### 14.7 待办（详见 6.13 文档）
P1：方向 A 收敛（实时轨落库→读快照+删 to_viewmodel，落库失败报错不兜底）；ERP 脚本迁移（scripts/erp_db→根 scripts/erp，去硬编码可插拔）；历史 confidence 脏数据重跑；is_core 数据源。P2：定时调度器（decision_config 已备）；special 跳转；synthesis 兜底。

---

## 15. 2026-06-14/15：双轨读回 + tab5 多处口径修复 + Part 6 真实执行接入

> 跨多 commit / 多窗口并行：`99ce0b6`(方向A收敛+KB23回算) → `c436415` → `63ba75d`(action粗类化+placement判据+campaign_new shop_account回退+flow_sv) → `1a79370`(post-merge rounding/card_id/TZ) → `8c6efd0`(Part 6 合并)。

### 15.1 前置双轨读回（Tab3/Tab4 快照富化）
- **Tab3**：`decision.py:_translate_p3_recommend` → `/decision/{id}/preset` 出 `p3_recommend`；前端 `renderReadonlyPreset` 复用 `renderP3` 还原 ACOS/预算推理全文（ai_suggest 真库有值，前端接线即出）。
- **Tab4**：`repository.read_decision_preset_rich` 加读 `direction_recommend_detail`；`decision.py:_translate_p4_directions` → `directions_rich`；前端抽 `renderDirCard(d)`（实时 `loadExecution` 与快照共用），渲评分+理由全文卡。**前提**：写入侧须先把完整 directions 落 `wf["directions"]`（`run_get_execution_options` 持久化）——历史批次无则降级方向名 badge。

### 15.2 tab5 执行层口径修复（均在 `from_db_snapshot` / 分析侧，单源生效）
- **action 粗类化**：`_snapshot_action` 退化为纯查 `suggest_category`（ELIMINATE/CREATE/KEEP/低价 group → 对应；ADJUST/空 → 粗类 `adjust`=「调整」），**不再按 pending 行有无猜 adjust_bid**（曾把全维持活动误标「调出价」）。前端 `_actionLabel` 补 `adjust:'调整'`。
- **placement 判据**：`_normalize_action` 的 `placement_changed` 从「列表非空」改为「任一广告位 proposed_pct≠current_pct」——全维持的 EXACT 活动正确 derive 成 `keep`。
- **预算约束 backend 化**：`from_db_snapshot` 的 `target_budget`（顶部预算约束）= **3 活跃组约束加总 + 低价捡漏固定 $1**（KB21§6），不再取 `decision.daily_budget_suggest`（那是回算前父级预算，与组加总不同口径，会对不上）。低价 $1 前端硬编码（KB 常量）。

### 15.3 低价捡漏组判定重做（阈值 0.21/1.01）
统一常量 `LOW_BID_MAX=0.21` / `LOW_BUDGET_MAX=1.01`（`campaign_portfolio.py`，campaign.py 复用）：
- **预过滤（AND）**：`bid≤0.21 且 budget≤1.01`（两者都到底=已入池）→ 灰卡剔除不分析。
- **分类（OR）**：`_is_in_elimination_pool` = `bid≤0.21 或 budget≤1.01` → 低价捡漏组。
- **LLM 硬规则**：EXACT/BROAD prompt 加「bid≤0.21 或 budget≤1.01 → 必须 eliminate」。
- **强制兜底（不靠 LLM）**：`_resolve_budget_conflicts` 循环顶部命中 OR 即翻 `action=eliminate`，**紧接现有淘汰硬校验无条件把 proposed 修正到 $1/$0.20**（治 LLM 不听话把 proposed 调高）。

### 15.4 Part 6：广告调整 MCP 真实执行接入（`8c6efd0`，详见 [docs/2026-06-15-交接文档.md]）
confirm(CONFIRMED) → 「执行已确认调整」→ 调 `whp-advert-agent` MCP（6 工具）→ 落 4 张 `_record` 表 + 回写 pending `execute_status` → 前端「调整记录」表。新增 `advert_mcp_client`/`advert_exec_mapper`(唯一映射点)/`advert_execution`；repo 6 执行方法 + `_audit_int` + `use_tls`；端点现仅存 `/campaign/execute`（原 `execute/status`、`execution-records` 已随 §16.3-④ 前端调用点一并下线）。**默认安全**：`advert_mcp_enabled=false` + `advert_exec_dry_run=true`。真库 dry-run 全链路通；真实写 Amazon「新建活动」链路通仅被 IP 白名单 403 拦；「改已有活动」卡在数仓 campaign_id 调整 MCP 不识别（外部契约待 MCP 团队）。

### 15.5 待办状态核对（对照 §4.2/§9.1/§12.5，已按代码现状核实）
| 项 | 状态 |
|---|---|
| 方向 A 收敛（实时轨落库→读快照+删 to_viewmodel） | ✅ 已完成（`99ce0b6`） |
| `/campaign/confirm` 落地（confirm_decisions 非 stub） | ✅ 已完成 |
| A1（`_ensure_data` meta_filter 按 filter 分 key 缓存） | ✅ 已完成（`ecc207e`） |
| `analysis_overview` 写入端 / 执行层 DB 全链路落库 | ✅ 已完成（`53fa5b3`） |
| 建议竞价（suggestedBid）MCP 接入 | ✅ 已完成（§12.1） |
| **`is_core` 真实数据源** | ❌ **仍硬编码 `False`**（campaign.py:1111，行号持续漂移），P1 未动 |
| **P4 定时调度器**（decision_config 已备） | 🟡 **换方式做了**（无 APScheduler，但 **cron 已上线**：`crontab_schedule.sh` 每天 0 点 → `batch_via_api.py`，定时目标达成。详见 §17） |
| **新增活动预算接入组合回算**（KB23 §5.1） | ❌ 未做（`campaign_budget_reallocation` 不含 new_campaigns） |
| 触发场景 KEYWORD_PROMOTED_FROM_BROAD 等 5 类 | ❌ 未做（需搜索词聚合/KB08/词池表，仅展示标签） |
| **组合按 proposed（执行后预算）分类** | ✅ **已实现（A 派：显示+回算统一按 proposed）**。`campaign_portfolio.classify` 加 `effective_budget`：预分类(campaign.py:322)按 `current` 喂 LLM 现状上下文，**终分类(campaign.py:533)传 `eff=proposed`** 做主力↔测试升降组（KB23 §3.1B/§3.5/§3.7）；淘汰判定仍读 current。回算 `bra._group_of` 读同一 `ai_portfolio_class`（即 proposed 分组）+ 兜底也按 proposed。 |
| Part 6 真实写 Amazon（改已有活动 / 新建活动） | ⏳ 链路通，被 IP 白名单 + 数仓 campaign_id 外部契约阻塞 |

> **新增活动 LLM 触发条件（澄清）**：LLM **无触发门禁**。`trigger_scene` 仅展示标签；门是「硬过滤(KB16§6)→排序→Top-N(≤20)→双轮 LLM 取交集」，LLM 只对每候选词判 create/skip。

---

## 16. 2026-06-16：双轨读回补全（Tab1关键词/Tab3接线/Tab4） + 前端重构 + 总览·方向·预算·库存 根因修复

> 承 §15。`app/**.py` 改动需**重启 uvicorn**；`demo/**` 刷新浏览器。

### 16.1 前置双轨读回补全（接 §15.1）
- **Tab1 核心关键词监控（新增）**：`repository.read_decision_preset_rich` 加读 `core_keyword_tracking`；`decision.py:_translate_core_keywords`（keyword_type ERP码→前端 Broad/Long-tail 键、nature_rank varchar→int，near_rank/周变化未落库→缺省）→ endpoint `core_keywords`；`renderReadonlyPreset` 复用 `renderKeywordTable`。**排名列大概率仍空**（源头改造未做，06-14 §5.3；真库 nature_rank 99% 空）。
- **Tab3 前端接线（补 §15.1 落地）**：`renderReadonlyPreset` 改为 `renderP3(data.p3_recommend)`（原只渲 badge）；`renderP3` 预算 delta 加 `if(bb.current!=null)` 守卫（快照零 delta 不渲丑串，实时零回归）。
- **Tab3 人工覆盖提示（新增）**：快照有数值但三段推理全空（人工覆盖/未经 LLM）→ 复用 `p3Warnings` 显示「⚠ 人工覆盖值，未经 LLM 推理建议」（无 manual_override 列，以「有值无推理」作代理）。
- **Tab4 写入轨缺陷（诊断锐化，承 §15.1 前提）**：根因锁死——完整方向对象**从未落 state**（`run_get_execution_options` 只返 response、`ExecutionSelectRequest` 只有 selected_directions、`save_execution` 只存 id、全工程无 `wf["directions"]=`）→ `_upsert_wizard_direction` 整段不写。真库有 detail 的老决策是早期 `/wizard/report` 遗留。**修法待办**：`run_get_execution_options` 落 `wf["directions"]` 或 `_do_analyze` resolved_directions 透传（零映射，与 ACOS resolved_* 对称）；修后新批次 Tab4 读回零改点亮。

### 16.2 Tab5 显隐门禁改 vm.is_latest 自决
执行控件（勾选框/批量栏/执行按钮）原由页面层 `/decision/context`（latest_completed_id + in_progress）决定，与 ERP 可达性强耦合（断则控件静默全消失）。改：`state.js:setData` 里 `state.executable = vm.is_latest===true`（展示批次自身），`panel.js` 不再用调用方传入值；`ad-asisitant-agent.html:switchBatch` 每次切批次先 `refreshDecisionContext` + 重渲批次栏。门禁真相源 = 展示批次 `decision.is_latest`（与下拉「· 最新」同源）。

### 16.3 campaign-panel 前端重构（纯前端）
1. 组合气泡标签「约束」→「广告组合预算」。
2. **回算修改弹窗**：改/执行移出气泡到行末统一操作区 `[回算修改][执行]`（竖排等宽，+override 时[恢复]）；弹窗内 4 列两行表（上行 AI 回算推荐、下行 3 输入框+灰禁 `1`），3 组同改；输入框数字居中防误点；挂载点 `#camp-modal-mount`（camp-root 内保事件委托）。
3. **三执行按钮加确认弹窗**（同意/不同意/回算执行）：点击先弹确认，执行流绑「确认」`runConfirm`、「取消」不执行；挂载点 `#camp-confirm-mount`。
4. 去「执行已确认调整」按钮 + 前端调用点（`executeConfirmed`/`pollExecStatus`/`_operator` 删；后端 `/campaign/execute` 保留）。
5. 批量栏重排：同意/不同意挨着调整记录。
6. **处理状态分段筛选**「全部/待处理/已处理」（`camp-set-process`）；已处理=本地 `_reviewState` 或快照 `confirm_status∈{CONFIRMED,REJECTED}`；预过滤/丢失仅「全部」可见。
7. **sticky 控制区**：明细/汇总+筛选+组合气泡+批量栏钉顶、卡片列表内部独立滚动。配套修高度链：`.camp-root` 加 flex 列 + min-height:0、`camp-app-layout/camp-main-area` 调整；**主看板**补 `#panel-tab5.tab-panel.active{flex:1;min-height:0}` + `#mainContent`/`#campPanelRoot` min-height:0（原 `.tab-panel.active` 缺 flex:1 致高度链断、整体一起滚）。⚠ iframe 嵌入的 live 生效未验。

### 16.4 总览/广告方向/预算/库存 根因
- **总览缺数据过度阻断（已修 prompt）**：库存 N/A → 总览硬断「禁一切增长」冻结健康活动。`reasoner.py:507` 改「数据缺失既不当正常也不当风险、不得据此阻断；只有数值确认越线（库存<7天）才阻断」（只改 507）。
- **广告方向英文 id 未翻译（已修，一处堵三处）**：`saveDirectionsLeft` 存英文 id（`expand_keywords`），`build_campaign_strategy_context` 原样透传；而总览/逐活动 prompt/KB23（查 `"推进自然位" in ad_directions`）全按中文匹配 → 认不出选中方向（"选了新增扩词、总览却禁新增扩词"）。`campaign.py` 加 `_zh_ad_direction`（id→中文，幂等），翻译后进 `strat_ctx.ad_directions`，一处修好总览+逐活动+KB23。
- **KB23 父目标未接进回算（诊断，未修）**：`campaign_parent_allowed_net_increase=0.0`（占位无源）→ `available=只有释放预算`，父目标只作 LLM 文字 context、不入 `aggregate()`；`validate()` 无「proposed 合计 vs 父目标」绝对护栏；GROUP-009 超目标压缩/「建议提父预算」未实现。澄清 §4.2：caps 允许超父目标（caps≠花费），proposed_group_budget 是自底向上回算值。**修法待办**：算 `expected_total_spend=Σ(perf_7d.cost/7)` 与 headroom，<0 触发 GROUP-009 或建议提预算（perf_7d.cost 现成）。
- **库存 MCP 透传 bug（已修解包）**：`mcp_adapter` 库存解析原用裸 `isinstance(list)` 判**原始 payload**，MCP 返回是 envelope（`{"rows"}`/单行 dict）→ 恒 False → 库存丢 None（数据查到却没透传）。改经 `_as_rows` 解包 + `_int`，保持 `can_sale_num`（FBA可售=可售天数指标，**不混** `in_stock_num`/`stock`，口径不同）。**~~待办~~**（06-18 已闭环）：Doris 路径口径已统一成 `can_sale_num`（`db_adapter.py:426` `inventory_qty=_int(listing.get("can_sale_num"))`），两路径不再口径不一致。（真库 live 调 `listing_inventory` 确认 envelope/列名仍建议补一次端到端验证。）

### 16.5 `summary total_count mismatch` 告警（仅解释）
`_upsert_modern_summary`（repository.py:786）落库前校验：`declared=total_campaigns` vs `categorized=淘汰+调整+保持` 不等则告警，按 categorized 写 `total_count`。差值正常 = 新增+预过滤+丢失（三桶不含）；若差值 > 这三类合计 → 有活动真丢（多为 LLM 分批解析异常未记 lost），查 `warnings`。

### 16.6 待办（本期新增/锐化）
> ⚠ 部分条目已被 06-17/18 后续工作覆盖，最新核实状态见 §17。
- [ ] **Tab4 写入轨修复**（16.1）—— 阻塞 Tab4 富卡见数。**（06-18 核实仍未实现）**
- [x] ~~**KB23 父目标接进回算**~~（16.4）—— 06-18 核实：闸控已接线（`campaign_budget_reallocation.py:108` 读 `parent_allowed_net_increase`），但值仍 `=0.0` 占位无源（settings.py:160）；**GROUP-009 超目标压缩护栏仍缺**。
- [x] ~~**库存口径统一**~~（16.4）—— 06-18 核实：已统一成 `can_sale_num`（`db_adapter.py:426`），本条已完成。
- [ ] 重启 uvicorn + 真库回归（Tab1关键词/Tab3/Tab4 还原；总览不误禁增长+认得方向；库存 N/A 消失）。
- [ ] Tab2 诊断/趋势双轨（ECharts 隐藏容器懒渲染 + 列名核 + nor 缺线）。
- [ ] sticky 控制区 iframe live 验证（不生效则改 JS 实测高度兜底）。

---

---

## 17. 2026-06-18：待办逐条对代码核实（状态校准，无代码改动）

> 对照 §4.2 / §9.1 / §12.5 / §15.5 / §16.6 的历史待办，逐条对**当前服务器代码**（含 06-17/18 改动）核实。结论：大部分待办仍未实现，但 **3 条状态已变**（⑦已做、④换方式做了、③⑧是半成品），文档相应条目已就地更新，本节存证。

| # | 待办 | 实际状态 | 证据 |
|---|------|----------|------|
| ① | `is_core` 硬编码 False | ❌ **仍未实现** | `workflow/steps/campaign.py:1111` `is_core=False`（行号持续漂移：956→1106→1111，引用前先 Grep） |
| ② | Tab4 方向对象未落 state | ❌ **仍未实现** | `run_get_execution_options`（GET，生成富对象）**不存 state**；只有 `run_confirm_execution:181 save_execution` 存，且存的是选择 req（选中 ID）非富对象 → Tab4 读回只有 ID，富卡不亮 |
| ③ | KB23 父目标接回算 + GROUP-009 压缩 | 🟡 **半成品** | `parent_allowed_net_increase` **已接进回算闸控**（`campaign_budget_reallocation.py:108`），但 `settings.py:160` 值 `=0.0` 占位无源；**GROUP-009 超目标压缩护栏未找到 = 未做** |
| ④ | P4 定时调度（APScheduler） | 🟡 **换方式做了** | 无 APScheduler；但 **cron 已上线**（`crontab_schedule.sh` 每天 0 点 → `batch_via_api.py`），定时目标达成（另一窗口做的） |
| ⑤ | 新增活动预算接入组合回算 | ❌ **仍未实现** | 回算代码里没找到新增活动预算的接线 |
| ⑥ | 5 类触发场景仅展示标签 | ❌ **确认就是标签** | `campaign_new.py:6` 明确「trigger_scene 仅作展示标签，不作筛选门禁」——与待办描述一致 |
| ⑦ | 库存口径 Doris 用 in_stock_num | ✅ **已统一** | `db_adapter.py:426` `inventory_qty=_int(listing.get("can_sale_num"))`，两路径口径统一，文档原条已过时 |
| ⑧ | 核心关键词 nature_rank 源头改造 | 🟡 **代码在，数据源没改** | `db_adapter` 有 natural_rankings 拉取（:194/:314 读 `craw_nature_rank`），但真库 99% 空 → 「源头改造」（让数据有值）是**数据/ETL 侧**的事，代码没动数据源 |

**结论**：
- **确实未实现（4 条属实）**：① is_core、② Tab4 富卡落 state、⑤ 新增活动预算回算、⑥ 触发场景门禁。
- **状态已变（文档已更新）**：⑦ 库存口径已统一（06-18 改）；④ 定时已用 cron 实现（非 APScheduler）；③ 父目标闸控已接线但值=0占位、GROUP-009 压缩仍缺；⑧ 拉取代码在、缺的是数据源（非代码待办）。

---

## 18. 2026-06-18：前端三项优化（已实现，主看板 `ad-asisitant-agent.html`）

> 三项前端渲染/批次状态优化，均已落地（后端零改动）。下列为改动记录 + 关键代码锚点。

| # | 待办 | 现状（代码锚点） | 目标 |
|---|------|------------------|------|
| F1 | **分析完成后左栏切快照** | `onComplete`（:3249）原仅刷新批次栏 + 挂 tab5 快照，未切左侧前置配置栏 | ✅ **已实现（2026-06-18）**：`onComplete` 成功分支插入 `renderReadonlyDecisionPreset(write.decision_id||ctx.latest_completed_id, {switchDefault:false})`，复用 loadAll B 态同一函数完整切快照、停留 tab5。**未加失败兜底/loading 态**（评估为低 UX 价值，见会话决策） |
| F2 | **进行中隐藏历史批次下拉** | `renderBatchBar` C 态原只隐藏「新建分析事件」按钮，`batchSelect` 仍可见 | ✅ **已实现（2026-06-18）**：标签加 `id="batchSelectLabel"`；`renderBatchBar` 中 C 态隐藏标签 + `batchSelect`（仅留徽标 + 放弃按钮），A/B 态显示。完成后转 B 态经 `applyBatchBarFromCtx` + `refreshDecisionContext` 自动重渲并刷新批次 |
| F3 | **去掉战略层分析天数下拉** | `daysControl`/`daysSelect`（:580-588）+ `onDaysChange`（:1455）；`days` 传入 11 处 API（strategy/tactics/recommend/diagnosis/execution/wizard/campaign）。**⚠ 后端确实在用 `days`**——`campaign.py:_do_analyze` 实打实 `days=int(req.get("days",7))` 并下传 `aggregator.fetch(days=)`/`analyze_campaigns(days=)`；零改动能成立是因为**前端永远传 7 → 后端就跑 7 天**，不是因为后端忽略 days | ✅ **已实现（2026-06-18）**：删前端天数下拉 UI + `onDaysChange` + 快照天数行 + 两处 `show/hide('daysControl')`；**保留 `let days=7` 常量**（11 处调用全锁 7）；后端零改动 |

> **F3 ⚠ 切勿据"后端忽略 days"去清理后端 `days` 参数**——会搞坏定时跑批（SCHEDULED 链可能传非 7 的 days）。后端 `days` 是活参数，本次只是前端锁 7。
>
> F3 注：主交接文档「days 时间窗口参数化」节称"前端无下拉框"已**过时**——本次移除后该节描述方与代码一致。

---

## 19. 2026-06-18：已知逻辑缺陷（待修，暂无时间）

> ⚠ **定时分析不采信 config 的 acos/预算配置 —— 这是错的逻辑，已确认，暂无时间改。** 记此备忘，未来修复。（核心缺陷仍成立：`load_layer14` SELECT 仍只 7 列、config 两列无人读；§21 后定时分析会采信 state 库 override，仅 config 配置列仍不读。）

**现象**：config 表 `target_acos_suggest`/`daily_budget_suggest` 满屏 NULL（520 ASIN 中 482 个最新行 acos 为 NULL）；定时跑批的目标 ACOS 全靠 AI 现算，数据差的产品被钳到区间下限 `min_acos`=25%，与"运营在 config 配的目标"完全脱节。

**根因（代码已核实）**：
- 定时分析（`cfg_source=config`）只从 config 读 1-4 类目——`decision_config_reader.load_layer14` 的 SELECT **只取 7 列**（product_position/stage/season_type/advert_purposes/target_keyword_types/advert_direction_types/day_range），**不含 acos/预算**。
- `api/campaign.py:_do_analyze` 的 acos 解析链 = `get_target_acos_override`(state) → p3 缓存(state) → `TargetAcosRecommender` AI 现算（区间下限 = `min_acos`=25，`compute_target_acos_band` @ recommender.py:585），**全程不读 config 的 acos**。
- `repository._upsert_decision_config` 又**故意不写**这两列（注释"AI 不进 config"）。
- 且 config 的这两列**无任何代码读取**（load_layer14 不读；前端 Tab3 快照读的是 **decision 表** `get_decision_preset`，非 config）→ 这两列当前是"只写历史值、现已不写、谁也不读"的死列。

**正确逻辑应为**：定时分析的 acos/预算应**优先采信 config 中运营确认的 `target_acos_suggest`/`daily_budget_suggest`**，无配置才回落 AI 现算——否则"运营在 config 配的目标"永远不生效。

**未来修复点（三处）**：
1. `load_layer14`：SELECT 增加 `target_acos_suggest`/`daily_budget_suggest`，返回给 `_cfg14`。
2. `_do_analyze`：acos/预算解析链在 override 之后、AI 现算之前，插入"读 config 配置值"一级（`cfg_source=config` 时）。
3. `_upsert_decision_config`：确认回写策略（运营手动值 vs AI 值是否回 config），与上面读取口径一致。

**附带（本次未做）**：曾计划把 config 历史 NULL 的 acos/预算回填（A 队列 acos=35%、B 队列采信最近非空），dry-run 完成（483 行待写）但**未执行**——因为发现"这两列没人读"，回填当前无收益，且根因是上面的逻辑缺陷，应先修逻辑而非补数据。**prod 未做任何写入。**

---

## 20. 2026-06-18：修复4 — 目标ACOS 区间化（AI 在 KB 锚定区间内出单值）

> 仅本地（`AD_assistant_agent-v3.2`），**未上服务器 chenv31**。背景见 §19 + 主交接文档「目标ACOS/预算 prompt↔KB 出入核查」。

### 20.1 设计
目标ACOS = **单值**（运营衡量偏离度的基准）；区间是**给 AI/算法看的工作边界**，在其中选一个值。
- **上限 acos_ceiling = 阶段上限**（KB03 §2，优先生效）：测试50/推进40/收割40/维持40/清货60；阶段未定义/未归一化时回落层级**默认**上限（KB03 §1：长尾P3=30、其余40）。⚠ 2026-06-24 修正：原为 `min(阶段, 层级)`，因非长尾层级恒40 把清货期60/测试期50 永久封顶（详见 20.4）；运营确认阶段优先、层级仅作 fallback（长尾 P3 清货期亦随阶段到 60%）。
- **下限 acos_floor = max(全局 min_acos=25, 各广告目的下限的最大值)**，且不超过上限（上限优先）。目的下限（needs_review 初值）：盈利25/转化25/排名35/引流40；多目的取最大。
- 精度 **5% 取整**。
- 同一区间函数同时进**算法**（定时跑批主力 + 实时降级）与 **prompt**（实时 LLM 主路）→ 四处口径一致。

### 20.2 改动（4 文件）
| 文件 | 改动 |
|---|---|
| `app/config/thresholds.toml` | `[target_acos]` 保留 `min_acos=25`、`increment=5`；**删** `stage_scopes`（min=5/10 与 25 矛盾）；**新增** `stage_ceilings`/`level_ceilings`(KB03同步)/`purpose_floors`(目的下限) |
| `app/core/recommender.py` | 新增模块函数 `compute_target_acos_band(cfg, stage, level, ad_purposes)`；`TargetAcosRecommender` Step1 基准取区间中点、Step7 钳到 `[floor,ceiling]`+5%取整（先取整再钳）|
| `app/llm/reasoner.py` | `_P3_TASK_PROMPT` 加「概念澄清」段（目标ACOS↔实测ACOS↔KB上限/容忍上限 三者区分，治幻觉）；删硬编码矛盾（5%下限/±40%/预算30/Bid25）；精度1%→5%；`recommend_p3` 用户消息注入「目标ACOS取值区间」；**删 P3 的关键词级 Bid 调整表述**（P3 仅产出目标ACOS+每日预算，schema 本无该字段）|
| `app/llm/kb_loader.py` | `p3_recommend` 移除 `"19"`（活动级数值矩阵，对 ASIN 级误导/易引发 ACOS-目标 幻觉）|

### 20.3 自检结论
- ✅ 编译/TOML/运行时 `recommend()` 端到端跑通；区间边界用例全对（含 floor>ceiling 上限优先、多目的取大、兜底 25–40）。
- ✅ 删 `stage_scopes` 全仓无残留读取者；`test_recommender_scoring.py` 用的是 `Recommender`（方向评分器）非 `TargetAcosRecommender`，无测试断言 target_acos 具体值 → **零回归**；签名未变；无循环依赖。
- ⚠️ **行为注意**：① 当前 ACOS 离区间中点远时 Step3/6 易触发 violation → 置信度偏 medium/low（语义合理）；② prompt 示例值（25%）未跟区间联动，靠"示例不照搬"免责声明 + 区间硬约束兜底；③ 阶段值须已归一化（旧值→默认上限40）。

### 20.4 遗留（未引入新问题）
- **预算线未对齐**：prompt 已改"预算幅度遵循 KB"（KB03=50%），但 `BudgetBidRecommender`/`[budget_bid] max_budget_adjustment_pct=30` 仍 30 —— **改前即存在的不一致**，本轮只做 ACOS，预算另起一轮。
- **`purpose_floors` 是 needs_review 初值**（运营给的 25/25/35/40），隔离在 toml 一张表便于改。
- ~~**服务器未同步**~~ → **已于 2026-06-22 上线**（见 §21）。
- ✅ **阶段上限 >40 永不生效 —— 已修复（2026-06-24，补测试时发现，运营确认）**：原 `compute_target_acos_band` 取 `ceiling = min(阶段上限, 层级上限)`，而层级上限对非长尾产品恒为 40 → `stage_ceilings` 里测试期=50、清货期=60 对任何非长尾产品被层级 40 永久封顶、永不生效（清货期 ACOS 给不到 40% 以上）。**根因**：KB03 §1 层级栏措辞为「**默认**ACOS上限」=仅作 fallback，应被 §2 阶段具体值覆盖；旧实现误当硬约束 min。**修法**：改为阶段上限优先，层级仅在阶段未定义/未归一化时兜底（含长尾 P3 清货期亦随阶段到 60%——运营拍板长尾清货也要能清掉库存）。改 `recommender.py:compute_target_acos_band` + `thresholds.toml` 注释；由 `tests/test_target_acos_band.py` 18 例钉死（含 `test_band_stage_ceiling_takes_effect`/`test_band_longtail_follows_stage_when_defined`）。⚠ **仅本地，未上服务器 chenv31**。

---

## 21. 2026-06-22：override 持久化上线 + 前端广告方向统一 + 卡C态/MCP慢 诊断 + state库回填

> 本会话改动**已全部同步到服务器 chenv31** 并重启生效（systemd `ad-direction-agent`）。每次同步走：本地改 → diff(无漂移) → 备份(`_bak_*`) → 转LF上传 → py_compile/import 预检 → 重启 → /health 200。

### 21.1 override 持久化（acos/预算 override 跨日/跨事件持久）— 已上线
运营设的目标 ACOS / 预算 override 此前**每日 5:00 过期** + new-event 清除，导致定时分析读不到。4 处最小改法：
| # | 文件 | 改动 |
|---|---|---|
| ①a | `mysql_state_manager.py` `get_target_acos_override` | 删过期 DELETE 分支 → 持久 `return int(row["value"])` |
| ①b | 同文件 `get_long_term_config`（budget） | 删过期门控 → 无条件 `data["daily_budget_override"]=bud["value"]` |
| ② | `api/campaign.py:316` | cfg14 替换 long_term 前**先存 `daily_budget_override`、替换后灌回**（定时轨预算 override 才读得到）|
| ③ | `api/decision.py` new-event | 去掉 `clear_target_acos_override` → acos override 跨新建事件保留（仅"取消覆盖"删，与预算对齐）|
- **效果**：定时分析现在**采信 state库 override**（acos 走 `get_target_acos_override`；预算走 cfg14 灌回 + `build_campaign_strategy_context`）。**前提：运营设过**——没设则 acos 落 AI 推荐器、预算落数仓兜底（§21.4 实测 542 个配置 ASIN 里仅 34 有 acos override、508 空）。
- ⚠ `①a` 注释残留"仅由 new-event 失效"与 `③` 矛盾（实际仅"取消覆盖"删），待清理。

### 21.2 前端：广告方向开关统一 + renderP3 守卫 — 已上线
- **广告方向配置栏与 ACOS/预算同框同切**（根治"偶现只读'—'无法调"漂移）：`leftDirectionsBlock` 去自身 `hidden`(绑父 cardP3Override 可见性) + 开关绑到 `loadAll` 同一处 + `_dirFallbackOptsHtml()` 4方向兜底 + `loadExecution` 降级为只灌选项。快照态 `renderReadonlyPreset` 仍原子只读。
- **`renderP3` 加 `if(!data)return`**：修保存目标ACOS时 `cannot read properties of null (reading 'target_acos')` 崩溃（`_p3data` 未就绪时保存/取消传 null）。

### 21.3 已知问题/待办（本会话诊断，未修）
| 问题 | 根因 | 修法方向 |
|---|---|---|
| **实时分析结束后卡 C 态**（下拉不渲快照 + 显"放弃本次分析"）| `analysis_session` 表行没清 → `/decision/context` 恒判 in_progress。`clear_analysis_session` **只在 write_full OK 时清**；失败/超时/中途放弃不清；12h TTL 只在该 ASIN 被查时才触发。实测残留 14 行(含 06-17/18 死行) | ① 失败也清 session ② 全局 TTL 清理(`DELETE WHERE started_at<now-12h`) ③ 前端超时对齐后端 |
| **每次新建分析"先快照后配置"闪烁** | `onNewEventClick` 里**阻塞 `alert`** 把旧 B 态快照晾在背后，点掉才 `loadAll` 渲配置。每次必现 | 确认后立即切加载态 + 把 alert 移到 loadAll 之后 |
| **新建分析进来慢（60-95s/MCP工具）** | MCP 网关慢（avg 11s/max 258s/工具），一次分析 90-465s。叠加多 ASIN 并发 + 个别店铺(am_jinglilai_US)数仓慢查 + ad_keyword_report 工具名失效回落 | 治 MCP 网关/数仓，非 app |
| **超长 SKU 名写不进 state库** | `asin` 列宽不够（`US-运动文胸短细常规V领+宽肩带低星清货` 撑爆）| 跳过 或 加宽列 |

### 21.4 一次性数据操作：state库 override 回填（315 个）
- 从 `t_advert_agent_decision`（06-17/18，acos>5%）逐 ASIN 选值（优先手动REALTIME、取预算最高），剔除已有 override 的，回填 `acos_override`+`budget_override`。
- **315/316 成功**（1 个超长 SKU 名失败，§21.3）。写前已备份 `acos_override_bak_20260622`(34)/`budget_override_bak_20260622`(132)，回滚就绪。

### 21.5 容量结论（定时批量 500+ ASIN）
- **state库扛得住、非瓶颈**：`max_connections=151`，25天历史峰值 `Max_used_connections=5`，容器闲（CPU<1%）。
- 批次并发 = **5**（`batch_via_api.py:41 DEFAULT_CONCURRENCY`，cron 不传参→用默认；asyncio.Semaphore 节流）。可调 `--concurrency` 但**真瓶颈是 MCP**，调高并发可能不提速反增超时/卡C态。
- ⚠ `mysql_state_manager._execute` 每次新建连接、无连接池——并发5无碍，并发调到几十才需加池。
- ⚠ `batch_via_api.py:34` 硬编码 prod ERP 明文账密，建议挪 `.env`。

---

## 22. 2026-06-24：新增活动选词改造（相关性把关 + 多源词池 + 竞品源接入 + 串行优化）

> 仅本地（`AD_assistant_agent-v3.2`），**竞品源默认关、未上服务器 chenv31**。改动落在 `campaign_new.py` / `campaign_fetcher.py` / `reasoner.py` / `models/campaign.py` / `settings.py` / `kb_loader.py`。方案+隐患核实见 `~/.claude/plans/40-mcp-keyword-competitor-flow-...md`。

### 22.1 背景
新增活动候选词原仅 2 源（`flow_keywords` + `own_keyword_flow`），三个问题：① 无相关性把关 → 出现与产品**毫无相关**的词（代码只滤去重/噪声/搜索量<50，LLM 又拿不到产品标识）；② 候选硬截断 20、单源全局排序 → 一源占满轮不到其他源；③ KB16 §1 多路词源（尤其**竞品** `COMPETITOR_INTERCEPT_WINDOW`）未接。

### 22.2 已实现改动（分模块）

**A. LLM 相关性把关（影响每次运行）** — `campaign_new.py` + `reasoner._NEW_CAMPAIGN_PROMPT`
- 注入锚点：`existing_keywords`（已投词，原仅去重用）+ 产品 `title/brand/category_name`（asin_data，campaign.py 透传）→ 进 prompt 作"本产品相关性参照"。
- prompt 硬规则：相关性为 create **首要 skip 判据**；create 须写相关性依据，说不出 → skip；`natural_rank` 有值 = **事实相关**可信；**锚点稀薄保护**（新品/伪 ASIN 参照少时以标题为主，勿过度 skip 误杀）。

**B. 放宽 + 输出截断（影响每次运行）** — `settings.py` + `campaign_new.py`
- `campaign_new_max_count` 20→**40**；新增输出硬顶 `campaign_new_max_creates=20`（双轮交集后 EXACT 优先截 20）。⚠ 20 与 **KB03 §7「每日最大新词数 15」冲突**，代码注释标"暂用 20，待 KB/运营定夺"。

**C. 竞品词源接入（默认关）** — `campaign_fetcher.py` + `kb_loader.py`
- `discover_competitor_keywords`：`direct_competitors`→KB06 §3 弱势排序取 Top-K 竞品→并行 `seller_sprite_keyword_reverse` 反查流量词。mirror `discover_new_keywords`，fail-open。
- `new_campaign` preset 加 `"08"`（竞品姿态）。KB08 姿态门禁本期**简化**（标注待全量字段）。
- 配置：`campaign_new_competitor_enabled=false`(默认关) / `_competitor_max` / `_competitor_kw_per` / `_competitor_timeout`。

**D. 多源词池 + 配额** — `campaign_new.py` + `models/campaign.py`
- `NewCampaignCandidate` 加 `source` / `source_reason` 字段。
- `_select_by_quota`：**竞品20 / 自然位15 / 流量5**（暂定，注释待 KB），欠额按优先级回补（竞品>自然位>流量）。competitor 关时退回单一全局排序（现状）。
- H2 防护：配额在**打 source 标签后分桶选取**，不复用全局 sort+截断（否则竞品 natural_rank=None 被挤掉）。

**E. Live 解析修复（关键，避免踩坑）** — `campaign_fetcher.py`
- `seller_sprite_keyword_reverse` 真实结构 **`value.data.data.list`**（嵌套 dict→dict→list，`_as_rows` 只解一层够不着）；真实字段 **`keyword` / `searches`（不是 `搜索量`）/ `bid`,`bid_max`,`bid_min`**。新增 `_reverse_keyword_rows` 容错下钻。原代码层级错+字段名错 → 即便开源也必得 0 词。
- **SellerSprite 收录依赖**：niche/跟卖伪 ASIN 无反查数据（`total_keywords:0`）；主流竞品有（实测 `B0DQLB8WWC`→2095 词）；`direct_competitors` 对部分 niche `found:false`。→ 竞品源对**本账号伪 ASIN（跟卖/中文名）系统性偏弱**。
- **bonus**：reverse 自带 `bid` → 竞品词直接用作 `suggested_bid`。

**F. 去重合并来源（Q1）** — `campaign_new.py`
- `_merge_candidate` + `_SOURCE_PRIORITY`：同词多源 → 留一条、`source_reason` 合并、bucket 归**最高优先级源**、补 natural_rank/suggested_bid/取大 search_volume。替换原 `seen`-skip（原只留首个源、丢弃后源）。

**G. 三处串行优化（核实 LLM 不依赖 bid 后）** — `campaign_new.py`
- **①** 竞品发现 ∥ flow/own（competitor 开时 `create_task` 并行，竞品块 await 同一 task）；顺带修"flow/own 空→直接 return 跳过竞品"→改"competitor 关才 return"。
- **②** 建议竞价 ∥ 双轮 LLM（LLM prompt 禁用 bid，reasoner.py:397/402）：bid 作 task 与 LLM `gather` 并行，组装前 await。**每次净省 ~3s**。
- **③** `fetch_suggested_bids` 只查 `suggested_bid is None` 的词（竞品 reverse 自带 bid 不再重查）。

### 22.3 验证
- 离线单测 `tests/workflow/test_campaign_new_quota.py` **7 个全过**（配额分配/欠额回补、**H2 竞品 natural_rank=None 不被挤掉**、reverse 真实层级解析、空/envelope、来源合并）。workflow 全套 **39 passed**。
- ⚠ 预存坏测试 `test_budget_reallocation.py::test_classify_elimination_pool_stays_by_current_no_revival_gate`（`NameError: bs`，与本改动无关）。

### 22.4 上线前必做 / 待办
- **competitor 源 live 验证后再开** `campaign_new_competitor_enabled`：验 reverse 对主流品出词、niche 品 fail-open、`direct_competitors` 拿到竞品 ASIN、并行后双轮成功率不降。
- 输出 20 vs KB15、配额 20/15/5：均**暂定待 KB/运营定夺**（代码已注释）。
- 首版最小闭环：竞品词源 + LLM 相关性。**延后（标 TODO）**：`keyword_competitor_flow` 逐词打分、KB06 弱势精排、KB08 完整姿态。
- 锚点对伪 ASIN 系统性偏弱（H12）：live 打印 `brand/category` 须**专挑伪 ASIN** 验，别拿真 B0 ASIN 验完了事。
- KB16 其余未接源（CUSTOM_KEYWORD_POOL 运营词池 / KEYWORD_PROMOTED_FROM_BROAD 搜索词报告）仍 TODO。

---

## 23. 2026-06-24：淘汰判定是【多环节】——各环节阈值/口径有意不同（防误判备忘）

> 本节**不改逻辑**，只记录设计意图 + 一次真实误判，防后人（或 AI）把"看着不一致"当 bug 去推平。配套已就地补/改注释（仅注释）。

**起因**：有人（含本轮一个 AI）看到 `_is_in_elimination_pool` 用 `bid ≤ 0.10`，而常量 `LOW_BID_MAX = 0.21`、旧注释/docstring 又写"0.21"，**误判为 bug**，去"对齐"成 0.21。**错**。运营确认：**bid∈(0.10, 0.21] 且预算正常的活动，不应单凭 bid 强制归低价捡漏组**。

**真相：淘汰命中分多个环节，各环节问的问题不同，判据本就不同，勿统一。**

| 环节 | 在哪 | 判据 | 干什么 |
|---|---|---|---|
| **预过滤** | `campaign.py` / `is_strictly_in_low_bid_pool` | **AND**：bid ≤ `LOW_BID_MAX`(0.21) **且** 预算 ≤ 1.01 | 判"确实已淘汰执行"(两维都触底) → 剔除不分析(灰卡) |
| **预分类** | `campaign_portfolio.classify`(读 current) | **OR**：`_is_in_elimination_pool` = bid ≤ **0.10** 或 预算 ≤ 1.01 | 喂 LLM 前按现状归组 |
| **终分类** | `classify`(effective_budget=proposed) | 淘汰判据仍读 current；`effective_budget` 只影响主力↔测试 | 合并后按本轮建议归组 |
| **强制修正** | `campaign.py` `_resolve_budget_conflicts` | **OR**：bid ≤ **0.10** 或 预算 ≤ 1.01 | LLM 不听话时翻成 eliminate + 硬填 $1/$0.20 |

**关键点（勿踩）**：
- **bid 阈值两套，有意不同**：预过滤(AND) 用 **0.21**（`LOW_BID_MAX`）；归组/强制修正(OR) 用 **0.10**（裸字面量）。AND 路径判"已执行淘汰"，OR 路径判"该不该归组/翻正"，门槛本就不同。
- 常量 `LOW_BID_MAX=0.21` **只服务预过滤(AND)**；OR 路径的 0.10 是**独立口径**，**勿改成 LOW_BID_MAX、勿抽成"统一常量"**（那等于抹掉环节差别）。
- 预算阈值 1.01 各环节共用。
- 已补/改的注释（**逻辑零改**）：`campaign_portfolio.py`（阈值常量块 + `_is_in_elimination_pool` docstring）、`campaign.py`（强制修正 :1905 注释）——原写"0.21"的误导处已改准。

**教训**：看到"看着不一致"的阈值，先确认是不是**多环节的有意差别**，**报告、别擅自推平**（更别在被纠正后继续改）。

---

## 24. 2026-06-24：新增词总扩大词/泛词 → 长尾优先选词 + 属性级相关性（已修，本地）

> 已上线 chenv31 2026-06-24。**影响每次新增分析**（非默认关的竞品路径）。改 `campaign_new.py` + `reasoner.py` + `campaign.py`（调用点）。承 §22 的相关性锚点改造，进一步治"总扩大词"。

**现象**：新增词 agent 总选大词/泛词（产品是短裙却扩中长裙/连衣裙），相关性只到品类级。

**根因（已核实，两层）**：
1. **代码层放大（主因）**：选词两处（`_select_by_quota` 桶内 + 无竞品全局路径）都按 **`-search_volume` 降序**排再截 Top-40 → 高流量大词霸榜，**长尾在进 LLM 前就被截掉**（garbage-in，LLM 只能在大词里挑）。不只是"MCP 源偏大词"那么简单。
2. **品类锚点误导**：§22 注入的 `category_name` 把 LLM 引向**品类级**匹配（"同属裙类→相关"），恰是"短裙误扩中长裙"的来源——这些 MCP 源本就大致同品类，品类无区分力、还常空（伪 ASIN）。

**修复**：
| # | 改动 | 文件 |
|---|---|---|
| ① 长尾优先选词 | 新增 `_longtail_sort_key`：排序键 `(有自然位 → 词数多 → 搜索量 tiebreak)`，**词数无上限**（词越多越精准越靠前），搜索量不再主排。两处选词统一用 | `campaign_new.py` |
| ② 属性级相关性 | prompt 相关性判据从"同品类"→**匹配标题具体属性**（短裙≠中长裙≠连衣裙→skip，reason 须点明属性吻合）| `reasoner._NEW_CAMPAIGN_PROMPT` |
| ③ 词型偏好 | prompt 新增：优先精准长尾；审慎大词/泛词，除非测试期/引流型否则倾向 skip（按阶段/目的）| `reasoner._NEW_CAMPAIGN_PROMPT` |
| ④ 去品类/品牌锚点 | 相关性锚点只留**标题 + 已投词**；`product_brand`/`product_category` 从 reasoner 签名+prompt、campaign_new 签名+pass-through、campaign.py 调用点**全部移除**（非死代码）。⚠ **推翻 §22.2-A 与 §22.4-H12 的 brand/category 注入** | 3 文件 |

**KB 依据**：KB06 long_tail 精准优先于 generic 大词测词；阶段感知（KB02/03：收割/盈利/维持期不宜建大词活动）。

**验证**：3 文件 compile OK；brand/category 残留=0；workflow 全套 passed（含 H2 竞品 natural_rank=None 不被挤掉——排序改长尾优先后配额分桶不受影响）。

**待办/上线前**：真机拿"短裙类"产品验——确认不再扩中长裙/大词、长尾能进候选并被 LLM 选中。`MIN_SEARCH_VOLUME=50` 保留兜底。按纪律 diff→备份→上传→预检→重启。

---

## 25. 2026-06-24（续）：前端三项优化（A态空壳引导 / 执行结果toast常驻 / 告警tab化+筛选瘦身）

> 均为**纯前端**（主看板 `demo/ad-asisitant-agent.html` + `demo/campaign-panel/**`），刷新浏览器即生效，后端零改动，**已上线 chenv31 2026-06-24**。

### 25.1 A 态空壳引导（新 ASIN 未配置·无批次）
**背景**：全新 ASIN（state 库无配置）透传进来时，旧设计左侧配置栏可编辑 + 顶部「新建分析事件」按钮并列，用户不知点哪个；且 A 态 tab5 实际跑不了执行层（`_mountCampaignRealtime` 仅 C 态触发），左侧配了也没用 → 反常识。另注：叠加触发过「标记进行中事件失败」，根因是超长 SKU 名撑爆 state 库 `asin VARCHAR(20)`（§21.3，列宽已另行修复）。
**改**（`ad-asisitant-agent.html`）：`bootstrapFromContext` 的 A 分支由 `renderBatchBar('A')+loadAll(true)` 改为新函数 **`renderEmptyStateA()`**——左侧仅基础信息卡（隐藏 `#cardStrategy`/`#cardTactics`），右侧仅批次栏 +「新建分析事件」+ 中心灰色引导 `#emptyGuide`（隐藏 `#tabBar` + 所有 `.tab-panel`）；`loadAll` 开头加还原（进 B/C 态恢复 `cardStrategy`/`cardTactics`/`tabBar`、隐藏 `emptyGuide`）。使「新建分析事件」成为「配置→执行层」完整流程的**唯一入口**。点新建 → `onNewEventClick` → `loadAll(true)` 还原配置栏 → 进 C 态展开。

### 25.2 执行结果 toast 常驻可关闭 + 即时提示
**背景**：点「同意所选」确认后，结果 toast 必须等 `/campaign/confirm` 同步等 MCP 真实下发完成（§21.3，MCP avg 11s/max 258s/工具）才弹，1800ms 一闪即逝看不清；且点确认到结果之间**零反馈**，用户以为"点了没反应"。
**改**（`campaign-panel/state.js` + `panel.css`）：
- `_toast(msg, opts)` 加 `opts.sticky` 模式：**常驻不消失 + 右上角「×」可关闭**；非 sticky 自动消失 **1800→2400ms**。沿用原深色样式，无新增配色。
- 6 个执行结果调用点（同意下发成功/部分成功/reject 写回/审核失败/组合预算成功/失败）改 `{sticky:true}`。
- `batchConfirm` 发请求**前**加轻量即时提示「正在下发 N 个调整到 MCP，请稍候…」/「正在提交 N 项审核…」（2400ms，随后结果常驻 toast 覆盖同一 `#camp-toast`）。

### 25.3 告警 tab 化 + 筛选器瘦身（给卡片列表腾纵向空间）
**背景**：右下「调整活动」区太小，扩大空间来源。
**改**（`campaign-panel/render.js` + `panel.js`）：
- 告警从顶部红气泡 `#camp-warnings`（点击 `alert` 弹全部）→ 改为与「明细/汇总」**同级的常驻 tab**「告警 (N)」：新增 `#camp-warnings-panel` 容器；`_renderWarnings`(气泡)→`_renderWarningsPanel`(列表，空态「暂无告警」)；`_fullRender` 加 `warnings` 切换分支（与 detail/summary 同机制，走统一 `applyFilters` 重渲）。**数据契约不变**——仍读 `vm.warnings`（viewmodel.js 三处兜底为字符串数组），`_esc` 容错更稳；grep 确认无悬空引用。
- `#camp-filters` 上下 padding **8→6px**。
- **（未做，高风险待评）batchBar 进 topbar**：涉及 `.app-layout { height: calc(100vh - 46px) }` 硬编码高度链 + topbar 横向拥挤 + iframe 内滚动，需 ERP 真嵌入下验三态+滚动，留待单独做。

---

## 26. 2026-06-24/25：可靠性与口径修复（数据不可用区分 / 数仓 BE 重试 / MCP 日期窗口按站点）

> 背景：一周的 StarRocks 数仓 BE 故障（只读 FS → 无存活 BE）暴露三类问题；本节修复**均已上线 chenv31（2026-06-24/25）**。

### 26.1 数据拉取失败 vs 业务无调整（`data_unavailable`）
**问题**：数仓宕机时 `fetch_campaigns` 拉空/报错被压成「no adjustments」良性跳过——一次 529 个 ASIN「真成功 0 / 业务跳过 522」却 0 失败，宕机被伪装成正常。
**改**：
- `CampaignAnalysisResult` 加 `data_unavailable: bool`；`campaign.py` 的 fetch 超时/异常两分支置 True。
- `auto_push.should_push_to_erp` 优先判 `data_unavailable` → 返回独立原因（非「no adjustments」）；`api/campaign._maybe_push_erp` 在 `erp_write` 带 `data_unavailable=True`。
- `batch_via_api.py`：单列「数据不可用」计数 + 退出码（真失败→1；数据不可用占比 >50%→2）+ 告警。
- ⚠ 仍未覆盖：真正「无可用广告活动」(`total_campaigns==0` 但拉取成功) 与「数仓返回空行」无法区分，后者仍记业务跳过——留待**批量级空结果护栏**（待办：单批 `total_campaigns==0` 占比过高时告警/非零退出）。
- 测试 `tests/persistence/test_erp_auto_push.py`(+2)。

### 26.2 StarRocks 共享存储 BE 错重试（`starrocks_retry` 单一真源）
**问题**：BE 存储错以 `errno 1064` 的 `ProgrammingError` 回来但带 `starlet err`/`BE:1006x` 签名（只读 FS `BE:10064`、cache 目录分配失败 `BE:10062`），瞬时抖动直接失败、无重试。
**改**：新增 `app/data/starrocks_retry.py`（判定 `is_starrocks_be_storage_error` + `backoff_delay` + 同步 `run_sync_with_be_retry`，**单一真源**）：
- `db_adapter._query`（异步 asyncio.sleep）复用判定/退避；与内层 `OperationalError/InterfaceError` once-retry 互斥不叠加；超时不重试。
- `mcp_db_context._lookup_sync`（#1 MCP 入参解析 / #3 ERP listing 上下文）+ `lookup_top_child_attrs`（执行层取 top 子 ASIN）两处同步裸查接入。
- 退避：第1次重试 0.55–0.70s、第2次 1.05–1.20s（含抖动），共 3 次尝试 ≈ 1.6s；**只兜瞬时抖动**，整体宕机仍需数仓侧修。
- 只重试带签名的 1064；SQL 语法错（同 1064 无签名）不重试。错误签名是外部契约，**StarRocks 升级后需回归**这个判定。
- 测试 `tests/test_starrocks_retry.py`(6) + `tests/test_db_adapter_be_retry.py`(3)。

### 26.3 MCP 日期窗口按 ASIN 站点当地时间
**问题**：`make_date_window` 用服务器本地 `date.today()`、`end=today`（含未完整当天、span 多一天）；且数仓按各站点当地时间存，硬编一个时区对非美站点错。
**改**：`make_date_window(days, site_code)` **单一真源**——按站点当地时间：`end=当地今天-1`（排未完整当天）、`start=当地今天-days`（[today-days, today-1] 共 days 天）。
- `_SITE_TZ` 覆盖**实跑 6 站点**（日志核实分布）：US=America/Los_Angeles、UK=Europe/London、DE=Europe/Berlin、IT=Europe/Rome、ES=Europe/Madrid、FR=Europe/Paris；未知站点回落默认站(US) + 告警。
- **全链路 6 类构参点收口**：`mcp_adapter._resolve_context`、`mcp_query`、`campaign_fetcher`×3（perf/flow/rank）、`campaign.py`×4（restart/CPC/懒加载 placement+search_term），均按 `db_ctx.site_code` / `campaign_data.site_code` / `ctx.site_code`。
- 修掉 `campaign.py` 里**第二处** `date.today()` 老实现（本地 `_make_date_window` 改委托 mcp_mapping）。
- ⚠ 数仓 `dwd_whp_amazon_listing_general.site_code` 实存 15 个站点（含 CA/MX/JP/NL 等），但实跑只命中 6 个；新站点上量需在 `_SITE_TZ` 补行（不补则回落 US 并告警，可发现）。
- 测试 `tests/test_mcp_date_window.py`(7)。

### 26.4 运营 DB 变更
- `ad_agent_state`（docker `mysql-state-v25`）13 张表 `asin` 列 `VARCHAR(20)→VARCHAR(50)`（含 2 张 `*_bak_20260622` 表）：原 20 对中文 SKU 串（如「US-运动文胸…」）报 `1406 Data too long`。无外键、`ALGORITHM=INPLACE` 在线变更、已全库备份（`/root/db_backup_ad_agent_state_*.sql.gz`）。

---

*v3.7: 选词/投票质量 + 新增扩词治不准（2026-06-26 上线 chenv31，详见主交接 06-26 条）—— ①逐活动 **cid 句柄**根治 campaign_key 漂移（LLM 回吐 `C1..Cn`，代码 `cid_map` 权威回填结构/现状字段，越界/重复 cid 丢弃→进 R3）；②双轮投票**缺轮兜底**（单轮缺失=分歧送 R3=Level A；两轮都漏种占位送 R3、R3 仍缺删占位还原"未分析"不伪造 keep=Level B）；③删 LLM 自报 **confidence**（死字段，投票一致性已定档）；④新增扩词**接入 KB28**（`new_campaign` 预设 +`08`+`28:0,2,3`）：按 §2 综合权衡自然位+周排名+搜索量+标题属性判 R1-R4 `relevance_tier`，候选补 own_keyword_flow 周排名/周搜索量信号，目标词类型软引导；相关性/词类型判断**全交 LLM**，代码只记录不硬判；⑤推自然位删占比判据（recommender+thresholds，另一窗口）。待核：own_keyword_flow 三排名字段语义 live 终核；竞品源仍默认关（direct_competitors 无词字段，启用需配 KB28 §4.1）*
*最后更新：2026-06-25（v3.6: 可靠性与口径修复 §26（均已上线 chenv31）—— ①campaign 拉取失败/超时标 `data_unavailable` 与「无调整」业务态区分，batch_via_api 单列计数+退出码(真失败=1/数据不可用过半=2)，治「数仓宕机被伪装成全部正常无调整」；②StarRocks BE 存储错(starlet/BE:1006x)重试抽 `starrocks_retry` 单一真源，db_adapter._query(异步)+mcp_db_context 两处同步裸查统一兜底，只兜瞬时抖动；③MCP `make_date_window(days,site_code)` 改按站点当地时间(US/UK/DE/IT/ES/FR)，end=当地今天-1 排未完整当天，修第二处 date.today() 老实现，全链路6类构参点收口；④运营 DB：ad_agent_state.asin VARCHAR(20)→(50)）*
*v3.5: 前端三项优化 §25 —— ①A态空壳引导(新ASIN未配置时左侧仅基础信息+右侧批次栏+中心引导,renderEmptyStateA,「新建分析事件」成完整流程唯一入口)②执行结果toast常驻可关闭(sticky+右上角×)+发请求前即时提示+轻量toast 1800→2400ms③告警气泡→常驻tab(暂无告警空态)+筛选器padding 8→6px;均纯前端已上线 chenv31 2026-06-24，batchBar进topbar高风险未做）*
*v3.4: 新增词"总扩大词/泛词"根因修复 §24 —— ①选词改长尾优先(词数多优先,搜索量降为tiebreak,治"长尾进LLM前被截")②prompt 属性级相关性(短裙≠中长裙)+词型偏好(审慎大词,按阶段)③去 category/brand 注入(品类太粗引品类级误匹配,推翻§22的brand/category锚点)；已上线 chenv31 2026-06-24）*
*v3.3: 修复**阶段上限>40永不生效** bug §20.4 —— 补 §20 测试时发现 `min(阶段,层级)` 把清货期60/测试期50 被非长尾层级40永久封顶；运营确认阶段优先、层级仅 fallback（长尾P3清货期亦到60%）；改 `recommender.compute_target_acos_band`+toml注释，由 test_target_acos_band 18 例钉死。已上线 chenv31 2026-06-24）*
*v3.2: 补 §20/§21 回归测试 —— `tests/test_target_acos_band.py` 锁目标ACOS区间边界+真实toml同步+Step7钳制契约；`tests/persistence/test_override_persistence.py`(6) 锁 override"过期仍透出"语义，纯离线mock不连库。全套 151→174 passed 零回归；测试+文档+一处 bug 修复）*
*v3.1: 淘汰多环节阈值差别防误判备忘 §23 —— 预过滤(AND,0.21) vs 归组/强制修正(OR,0.10) 有意不同，LOW_BID_MAX 只供 AND 路径勿统一；仅补注释零逻辑改）*
*v3.0: 新增活动选词改造 §22 —— LLM相关性锚点(已投词+产品标识)/放宽40+输出20/竞品reverse源(默认关)/多源配额20·15·5/reverse解析修复(data.data.list+searches+bid)/来源合并去重/三处串行优化(竞品∥发现·bid∥LLM·bid去重查)；竞品源 live 验证后再开）*
*v2.9: override 持久化上线 + 前端广告方向统一/renderP3守卫 + 卡C态·MCP慢·新建闪烁 诊断待办 + state库回填315 §21）*
*v2.8: 修复4 目标ACOS区间化 §20 —— KB锚定区间[下限,上限]内出单值；算法+prompt 双口对齐；删关键词Bid表述/移KB19；自检零回归，遗留预算线对齐）*
*v2.7: 新增已知逻辑缺陷 §19 —— 定时分析不采信 config 的 acos/预算配置，acos 恒走 AI 现算/floor 5%，config 这两列死写死读；附回填计划已弃、prod 未写入）*
*v2.6: 新增前端待办 §18 —— F1 完成后左栏切快照 / F2 进行中隐藏批次下拉 / F3 去战略层天数下拉(后端硬编码7天)）*
*v2.5: 待办逐条对代码核实 §17 —— ⑦库存口径已统一 / ④定时改 cron 实现 / ③父目标闸控已接线（值仍占位）；① is_core / ② Tab4 落 state / ⑤ 新增活动预算回算 / ⑥ 触发场景门禁 确认仍未实现）*
*v2.4: 双轨读回补全 Tab1关键词/Tab3接线/Tab4 + campaign-panel 前端重构（广告组合预算/回算弹窗/确认弹窗/处理状态筛选/sticky）+ 总览缺数据阻断·广告方向中文化·库存MCP解包 修复 + KB23父目标/Tab4写入 诊断 §16*
*v2.3: 双轨读回 Tab3/4 + tab5 action粗类化/placement判据/预算约束backend化 + 低价捡漏0.21·1.01重做 + Part 6 真实执行 §15*
*v2.2: 执行层 ERP 全链路落库 + 缓存分 key + pipeline 并行 + 前端/数据修复 §14*
*v2.1: 自然排名（周排名）接入精准活动分析 §13*
*v2.0: 建议竞价 MCP 接入 + 前端闭环 + session TTL + userId 接线 §12*
*v1.9: 决策批次状态机+快照回读+confirm写回+DRAFT→state库 §11*
*v1.8: placement 加价比例数据接入+代码回填+前端合并模块 §10*
*v1.7: 新增广告活动分析线 §9*
*v1.6: 执行层落地 ERP 架构共识 §8*
*v1.5: 总览/汇总恢复改造 + 前端双 tab §7*
