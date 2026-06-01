# Campaign 广告活动分析引擎 — 交接文档

> **最后更新**: 2026-06-01
> **版本**: v1.0
> **分支**: chenv3.0

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
  ├─ Doris 上下文查询 → 仅维度字段 (campaign_name/child_asin/keyword_text/match_type/keyword_bid)
  ├─ 代码硬过滤 → 排除 non-ENABLED / 无数据 / 多关键词活动
  ├─ Campaign 预过滤 → 排除 budget≈$1 & bid≈$0.2 的疑似已淘汰活动
  ├─ MCP basic_info + product_report 并行 → 回落 Doris
  ├─ 组装 CampaignUnit[] (campaign_key = "活动名 × 子ASIN")
  ├─ 分流: EXACT → 精准流 / BROAD+PHRASE+AUTO → 广泛流
  │   ├─ 精准流: 预取 placement → _EXACT_PROMPT → 分批投票
  │   └─ 广泛流: 预取 search_term → _BROAD_PROMPT → 分批投票
  ├─ 合并两流结果 + Budget 冲突裁决
  ├─ Sanity check (仅低置信项，≤10/批，并行) — 当前临时禁用
  └─ AI 汇总合成 (按共同原因分组叙事) — 当前临时禁用
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
| `app/workflow/steps/campaign.py` | ~1150 | ★编排引擎：分流→分批→投票→校验→合成 |
| `app/llm/reasoner.py` | ~1640 | LLM Prompt 构建 + `recommend_campaign_batch()` + `recommend_campaign_synthesis()` |
| `app/models/campaign.py` | ~160 | 全部 Campaign 数据模型 |
| `app/data/campaign_fetcher.py` | ~507 | 数据编排器：Doris上下文→预筛选→MCP→回落 |
| `app/data/campaign_prefilter.py` | ~80 | 硬过滤纯函数 |
| `app/api/campaign.py` | ~133 | API 端点：`POST /campaign/analyze` + `POST /campaign/confirm`(stub) |
| `app/llm/client.py` | ~200 | DeepSeek API 客户端 + KeyPool 轮询 |
| `app/config/settings.py` | ~203 | Campaign 相关配置项 |
| `demo/campaign_test.html` | ~900 | 调试前端 |
| `app/llm/kb_loader.py` | ~155 | KB 加载器，`campaign_adjustment` preset (KB 18/19/21/22) |

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

# LLM
llm_timeout: int = 75               # httpx 层超时（秒）
deepseek_model: str = "deepseek-v4-pro"
```

#### Timeout 配置全景（多层级，外层优先级最高）

| 层级 | 配置 | 值 | 触发位置 |
|---|---|:--:|---|
| API handler 顶层 | (硬编码) wait_for | 120s | api/campaign.py `aggregator.fetch` |
| Campaign 数据拉取 | (硬编码) wait_for | 300s | steps/campaign.py `fetch_campaigns` |
| 流级单批 LLM | `LLM_TIMEOUT` | 60s | steps/campaign.py `_run_round` |
| Sanity 单批 LLM | (硬编码) wait_for | 60s | steps/campaign.py `_sanity_check` |
| Synthesis 单次 LLM | (硬编码) wait_for | 60s | steps/campaign.py `analyze_campaigns` |
| LLM HTTP 单次 | `llm_timeout` | 75s | llm/client.py `httpx.AsyncClient` |
| LLM 重试 | `MAX_RETRIES` | 2 | llm/client.py |
| LLM 单次最坏耗时 | = 75 × 2 重试 | ~150s | 累计 |
| MCP 工具 | `mcp_tool_timeout` | 1200s | settings.py |
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
| 06-01 | playbook.yaml 迁移 | `.claude/skills` → `app/config/skills` |
| 06-01 | `fetch_campaigns` 加 wait_for(300s) | MCP/Doris 子调用挂死防护，超时返回降级 result |
| 06-01 | `api/campaign.py` 顶层 try/except | TimeoutError + Exception 全捕获，500 → 降级 CampaignAnalysisResult |
| 06-01 | `_run_round` 解析挪进 try | result 非 dict / parsed 非 dict 等异常落到 except，不外抛 |
| 06-01 | `_run_round` gather `return_exceptions=True` | 二次防御 `_call_one` 未来回归 |
| 06-01 | `_unpack_stream` helper | 流级 gather 异常→空 4 元组，warning 入栈 |

---

## 4. 已知问题与待办

### 4.1 Windows asyncio 缺陷

| 问题 | 影响 | 临时方案 |
|------|------|---------|
| ProactorEventLoop 下 asyncio 取消 httpx recv 不生效 | sanity/synthesis LLM 调用 >60s 时挂死 | 已禁用，待切换线程池方案 |
| `timeout_override` 同样依赖 asyncio | httpx 超时也可能不生效 | 设置不超过 90s，但不能根本解决 |

**恢复方案**：用 `loop.run_in_executor(ThreadPoolExecutor, fn)` 在独立线程跑 httpx，`future.result(timeout=N)` 可强制中断。

### 4.2 功能待办

| 任务 | 优先级 | 说明 |
|------|--------|------|
| Sanity check 恢复 | P0 | 线程池方案 |
| Synthesis 恢复 | P0 | 同上 + 加 `timeout_override` |
| LLM 调用入口日志 | P0 | `recommend_campaign_batch/synthesis/_sanity_check` 入口打 logger.info（含 ASIN / batch_id / item count），下次挂死能立刻定位卡在哪一步 |
| R3 tiebreaker 端到端验证 | P1 | 从未真触发，需构造分歧用例 |
| 策略上下文→决策联动 | P1 | KB 19/21/22 缺策略联动规则 |
| DB 落库 | P1 | `t_advert_agent_campaign_analysis` + `_adjustment` 表 |
| L420 投票 key 同源化 | P1 | `tiebreaker_summaries = [s for s in summaries if s.get("campaign_key") in needs_tiebreaker]` 依赖 LLM 原样回显 campaign_key；需用代码侧反向索引消除脆弱性 |
| `POST /campaign/confirm` 落地 | P2 | MySQL pending 表 + ERP 推送 |
| `is_core` 真实数据源 | P2 | 替换硬编码 False |
| KB 遵循度评分器 | P2 | 消费实验 JSONL |

### 4.3 已修复的 Bug

| 问题 | 修复 |
|------|------|
| Doris 上下文重复行 | `campaign_budget` 移除 GROUP BY |
| MCP campaign 工具 budget=0 | `unwrap_tool_payload` 递归拆解双层嵌套 |
| `_prefetch` UnboundLocalError | `result: dict = {}` 显式初始化 |
| Sanity check 截断 | max_tokens: 2048→4096 |
| `_resolve_tiebreaker` key 不一致 | 统一使用 `_vote_key()` |
| API 错误返回缺字段 | 使用 `CampaignAnalysisResult().model_dump()` |
| `fetch_campaigns` 无外层 timeout | 包 `wait_for(timeout=300)` + try/except，子调用挂死不连累整链 |
| `asyncio.gather` 默认不 `return_exceptions` | 流级 + batch 级两处加 `return_exceptions=True`，异常隔离 |
| `_run_round` 解析在 try 之外 | result 形状异常会 AttributeError 外抛；改为全部挪进 try，加 `isinstance(result, dict)` 守卫 |
| API handler 无顶层 try/except | 任何未捕获异常 → 500 + 堆栈；改为统一返回降级 `CampaignAnalysisResult` |

### 4.4 失败路径覆盖矩阵（多层防护后）

| 失败点 | 旧行为 | 新行为 |
|---|---|---|
| MCP 上下文挂起 | analyze 永远挂 | 300s timeout → 返回空 CampaignData + warning |
| Doris MySQL 断连 | 500 + 堆栈 | warning「分析失败: OperationalError」+ 空 result |
| `aggregator.fetch` 120s 超时 | 500 | warning「数据拉取超时」+ 空 result |
| LLM 单批挂 60s | 该 batch 失败但其他 OK | 同（既有） |
| LLM 返回非 dict | AttributeError → 整流崩 | 转单批失败 warning |
| `TargetAcosRecommender` 内部 AttributeError | 500 | warning「分析失败: AttributeError」+ 空 result |
| sanity / synthesis | 挂 10 分钟无返回 | flag 禁用 → 跳过 + warning |
| exact 流抛异常 | broad 流被 cancel | broad 流照常完成，exact warning 入栈 |
| state 查询 MySQL 抖动 | 500 | warning + 空 result |

---

## 5. 调试与测试

### 5.1 测试 ASIN

| ASIN | 活动数 | 特点 |
|------|--------|------|
| B0B7S3PWWB | 102 | Fishnet Stockings，数据最全，已验证 |

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
Campaign timing [ASIN] +Xs: DONE synthesis           # 合成耗时
Campaign timing [ASIN] +Xs: DONE total               # 总耗时
Campaign batch LLM 成功 [ASIN], N items              # 单批 LLM 完成
Sanity check [ASIN]: 低置信 N/M 条 → K 批            # Sanity 触发
```

---

## 6. ERP 对接（计划）

Campaign 分析结果→待确认表的映射关系：

| Campaign 输出 | 目标表 | 字段 |
|--------------|--------|------|
| `eliminate_to_low_bid_pool` | `t_advert_agent_modify_campaign_pending` | STATE: ENABLED→PAUSED |
| `adjust_bid` | `t_advert_agent_modify_keyword_pending` | BID: old→new |
| `adjust_budget` | `t_advert_agent_modify_campaign_pending` | BUDGET: old→new |
| `adjust_placement` | `t_advert_agent_modify_placement_pending` | percent: old→new |
| `negative_keywords` | `t_advert_agent_modify_keyword_pending` | STATE: NEGATIVE |

所有记录通过 `decision_id` + `batch_no` 与 `t_advert_agent_decision` 关联，走 `PENDING→CONFIRMED` 审批流。

---

*最后更新：2026-06-01（追加全链路防护改动 + 失败路径覆盖矩阵 + Timeout 配置全景）*
