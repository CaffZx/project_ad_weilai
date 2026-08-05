# Campaign 广告活动分析引擎 — 交接文档

> **最后更新**: 2026-08-05（版本日志见文末，最新 v3.25：搜索词精准扩词双来源）
> **版本**: v3.25
> **分支**: chenv3.2

---

## 1. 项目背景与目标

### 1.1 背景

**项目**：广告活动调整 Agent，面向亚马逊广告运营的 AI 辅助决策系统（原名"广告方向决策子智能体"，已过时；该项目已扩展为综合性的 Campaign/Target/Portfolio/Budget 全维度调整 Agent）。

**数据源**：纯 MCP StarRocks 网关（2026-06-30 已切除 Doris，失败返零值不回落）。

**技术栈**：Python 3.13 / FastAPI / PyMySQL / DeepSeek API / Pydantic

### 1.2 Campaign 模块目标

自动化分析和调整所有广告活动（Campaign），核心能力：

- **自动发现**：从 MCP 获取 ASIN 下所有广告活动的维度信息
- **AI 分析**：基于 KB 知识库规则（18/17/15/19/22/21/31/32，精准/广泛分流切片），对每个活动给出淘汰/调整/保持建议
- **分流策略**：精准广告（EXACT）和广泛广告（BROAD/PHRASE/AUTO）使用不同的调整维度
- **单轮直判 + 护栏重试**：R1 单轮 LLM 直判 → 护栏拦截后带 retry_instruction 回灌重判（R2/R3/R4），R4 后不再 R5，最终护栏兜底（2026-07-31 切除 R1+R2 双轮投票）
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
  ├─ MCP 上下文查询 → 维度字段 (campaign_name/campaign_id/child_asin/keyword_text/match_type/keyword_bid)
  ├─ Campaign 预过滤 → 排除 budget≈$1 & bid≈$0.2 的疑似已淘汰活动 + 多关键词活动 + 否定词
  ├─ MCP basic_info + product_report 并行
  ├─ 组装 CampaignUnit[] (campaign_key = "活动名 × 子ASIN#匹配类型#关键词ID"，关键词级)
  ├─ ★核心词标签注入: 读 ERP 核心词表 → 回填 is_core (v3.18 新增)
  ├─ ★组合预分类 → 4 类判定: 主推/广泛自动/测试新增/淘汰 (campaign_portfolio.py)
  ├─ ★拉取组合预算 (ad_portfolio_list MCP, fail-open → 回退 60/20/20 兜底, 2026-07-09 新增)
  ├─ ★三股并行 (strategic_overview 后, 共享 ctx_dict/posture_brief, return_exceptions 隔离):
  │   ├─ 精准流: EXACT, 预取 placement → _EXACT_PROMPT → R1 单轮直判
  │   ├─ 广泛流: BROAD+PHRASE+AUTO, 预取 7d/14d search_term bundle → _BROAD_PROMPT → R1 单轮直判 + 提精准候选标注 (exact_promotion_candidates)
  │   └─ ★新增活动线: 候选词发现→硬过滤→双轮 LLM 选词取交集 + 搜索词提精准 双来源合流 (KB16+06+28, campaign_new.py, 详见 §15)
  ├─ 合并两流结果 + ★组合终分类 (LLM action 补淘汰判定); new_campaigns 独立挂载
  ├─ ★action 归一化 (_normalize_action: proposed vs current 差值 derive 权威 action)
  ├─ ★护栏裁决 → 冲突修正 + R3/R4 LLM 重试 (campaign_guardrails.py, v3.15)
  ├─ ★预过滤可见化: 淘汰池/多词活动打 __prefiltered, 合并 excluded → 前端灰卡 (§9.3)
  ├─ Sanity check (仅低置信项，≤10/批，并行)
  ├─ ★组合预算汇总 (campaign_budget_summary.py: 优先 MCP 真实值 > 60/20/20 兜底)
  ├─ ★组合预算回算 (campaign_budget_reallocation.py: 起点=current_group_budget, 增量约束)
  └─ AI 汇总合成 (按共同原因分组叙事) — 2026-06-08 已恢复 (§7.1)
```

### 2.2 核心设计决策

| 决策 | 说明 |
|------|------|
| campaign_key = "活动名 × 子ASIN#匹配类型#关键词ID"（关键词级，2026-07-16 改） | 关键词投放单元的唯一标识；⚠ 同活动多关键词单元合并时 card 聚合有后写覆盖风险，见 §4.2 待办 |
| 精准/广泛分流 | 不同匹配类型使用不同 prompt 和调整维度（Placement vs SearchTerm） |
| 分批大小 = 10 | 每批 10 个活动送入 LLM，平衡覆盖率和输出质量 |
| R1 单轮直判 | 每流单轮 LLM 直判（2026-07-31 切除 R1+R2 双轮投票）；护栏拦截后带 retry_instruction 回灌重判（R2/R3/R4），R4 后不再 R5 |
| 护栏兜底 | R4 后不再 R5，最终由 `apply_all()` 12 条规则裁决（§10.2） |
| Sanity 仅校验低置信 | 高/中置信跳过，节省 LLM 调用 |
| 策略上下文注入 | 每批 LLM 看到产品阶段/广告目的/目标ACOS/毛利率等 12 个字段 |
| 组合预算来源 | ★ v3.13: ad_portfolio_list MCP 拉取真实 portfolio 预算（按 4 关键词模糊匹配），fail-open 回退 60/20/20 兜底；KB23 回算起点=current_group_budget |
| is_core 核心词判定 | ★ v3.18: 离线 LLM 语义判定（KB29），ERP 表落库，campaign 主流程读回填；替代了从 v2.0 起一直硬编码 False 的方案 |
| 护栏优先级 + R3/R4 重试 | ★ v3.15: P3 硬淘汰 > P1 样本保护；护栏拦截后带 retry_instruction 回灌 LLM 重判，最多 R3→R4 两轮 |
| Product Identity 注入 | ★ v3.17: 所有层请求模型强制携带 (asin, shop_id, parent_seller_sku)，缓存无 identity 自动丢弃 |

### 2.3 置信度划分

> ⚠ 2026-07-31 切除 R1+R2 双轮投票后，主调整项经单轮直判统一打 `confidence="medium"`（`campaign.py` 单轮路径硬编码，见 `:978`/`:1776`）。`_sanity_check_batched` 仍只校验 `confidence=low` 项——当前 `low_conf` 恒空，Sanity 对主调整项为 no-op。置信度枚举仍保留，仅新增流（`campaign_new.py`）继续使用 `high`（双轮 keyword_class 一致）/ `low`（退化单轮）。

| 等级 | 现状 |
|------|------|
| **high** | 仅新增流双轮 LLM keyword_class 一致时打（主调整项不再使用） |
| **medium** | 主调整项单轮直判统一等级 |
| **low** | 仅新增流退化单轮时打（主调整项恒空） |

### 2.4 调整类型

| action | 说明 | 精准流额外字段 | 广泛流额外字段 |
|--------|------|--------------|--------------|
| `eliminate_to_low_bid_pool` | 淘汰至低竞价池 (Bid=$0.20, Budget=$1) | placement_adjustments | negative_keywords |
| `adjust_bid` | 调整出价 | placement_adjustments | negative_keywords |
| `adjust_budget` | 调整预算 | placement_adjustments | negative_keywords |
| `adjust_placement` | 调整广告位分配（仅精准） | placement_adjustments | — |
| `reactivate_budget_only` | 复评：仅恢复预算（不改 Bid） | — | — |
| `reactivate_with_calibrated_bid` | 复评：恢复预算+标定 Bid | placement_adjustments | — |
| `keep` | 保持现状 | — | — |

---

## 3. 代码文件与改动

### 3.1 核心文件总览

> 行数为 2026-07-30 实测（`wc -l`）。行号易漂移，引用 `file:line` 前请先 Grep 实测。

| 文件 | 行数 | 角色 |
|------|------|------|
| `app/workflow/steps/campaign.py` | 2846 | ★编排引擎：分流→分批→投票→校验→合成；action 归一化；组合分类调度；portfolio 拉取+透传；`_resolve_budget_conflicts` 预算冲突修正 + 护栏 `_apply_campaign_guardrails` 附加校验 + R3/R4 LLM 重试编排；operating_mode 接入；`campaign_exact_transition` 调度 |
| `app/workflow/steps/campaign_guardrails.py` | 504 | ★护栏独立模块（v3.14 新增，v3.15 增强）：`apply_all()` + 12 条规则（P0-P11）含 retry_instruction 回灌；P3 硬淘汰 > P1 样本保护优先级；`_sample_insufficient`/`_p3_should_force_eliminate` 公共谓词 |
| `app/workflow/steps/campaign_exact_transition.py` | — | ★精准组合确定性升降级（v3.22 新增）：EXACT 活动四组升降级逻辑，ACOS 约束驱动 |
| `app/core/acos_constraints.py` | — | ★ACOS 约束核心模块（v3.22 新增）：目标 ACOS/预算的硬约束计算 |
| `app/llm/reasoner.py` | 2295 | LLM Prompt 构建 + `recommend_campaign_batch()` + `recommend_campaign_synthesis()` + `recommend_semantic_core()`（含四层工作流+核心词语义判定全部 prompt）；护栏告警注入；**v3.25**：广泛流搜索词行渲染（`render_search_term_prompt_lines`）+ LLM 回吐 `exact_promotion_candidates` 按原始搜索词报表权威回填校验 |
| `app/llm/kb_loader.py` | 327 | KB 加载器。**权威预设定义见 `docs/knowledge_base/00-知识库总纲与切片覆盖矩阵.md` §4.1（KB v3.4.8）**。完整 preset 清单：`campaign_overview`(02/04/09)、`campaign_adjustment_exact`(18/17/15/19/22/21/10/03/14/30/29)、`campaign_adjustment_broad`(18/17/15/19/22/21/10/03/14/30/31/28)、`campaign_adjustment_product_targeting`(18/17/15/19/22/08/07/30)、`budget_reallocation`(23/30)、`portfolio_degrade`(32/21/23/30)、`new_campaign`(16/06/02/28/24)、`semantic_core`(29/28/24)、`p3_recommend`(01/03/05/09/10/11/14/25)、`purpose_tactics`(01/02/03/04/06/09/14)、`execution_direction`(01/02/03/04/05/09/14/22)、`analyze_report`(02/05/09/12/14)、`chat`(02/14)。大部分预设使用节级切片（如 `18:1,3`），非整文件注入。 |
| `app/models/campaign.py` | 336 | 全部 Campaign 数据模型 (含 portfolio/ai_portfolio_class/budget_summary/NewCampaignCandidate/NewKeywordCandidate/**SearchTermPromotionCandidate**/**NewCampaignDecision**) |
| `app/data/campaign_fetcher.py` | 1433 | 数据编排器：MCP 上下文→预筛选→MCP→回落（含排名旁路 `_fetch_keyword_ranks` + 竞品词源 `discover_competitor_keywords` + `fetch_portfolio_list` 组合预算 + spend 多周期窗口）；**v3.25 增强**：`build_search_term_bundle` 活动级 7d/14d 搜索词 bundle（KB31 样本不足标注 `search_term_fetch_status`），供广泛流 LLM 标注提精准候选 |
| `app/data/campaign_prefilter.py` | 130 | 硬过滤纯函数（`filter_campaigns` 多词去重+否定词排除+补维度字段+`__prefiltered` 标记 + `filter_eliminated_pool` 淘汰池预过滤，供前端预过滤卡展示） |
| `app/api/campaign.py` | 634 | API 端点（6 个）：`/campaign/analyze`·`/viewmodel`·`/snapshot`·`/confirm`·`/execute`·`/execute-portfolio-budget`（详见 §3.3） |
| `app/api/campaign_viewmodel.py` | 376 | DB 快照→ViewModel 反向 mapper；审核等级转中文标签（`_REVIEW_LEVEL_LABELS`），兼容旧枚举 `AUTO_BATCHABLE`/`SENIOR_APPROVAL` |
| `app/llm/client.py` | 275 | DeepSeek API 客户端 + KeyPool 轮询(Rlock) |
| `app/config/settings.py` | 283 | Campaign 相关配置项 (含 portfolio shares/fallback_multiplier/portfolio_fetch 开关 + `meta_filter_dashboard_light`) |
| `demo/ad-asisitant-agent.html` | 3853 | ★主前端（合并到主看板第5 tab，含侧栏折叠/Toast/降级兜底/手动输入保护/经营模式 radio 组） |
| `demo/access-guard.js` | — | ★前端入口守卫（v3.22 新增）：页面加载前校验 product identity 三元组 |
| `app/workflow/analysis_run_guard.py` | — | ★分析运行闸门（v3.23 新增）：防重复分析、session 取消态管理、运行前幂等校验 |
| `app/data/mcp_adapter.py` | 536 | MCP 适配器：`campaign_call_tool` 透传 `qryFixedPortfolio`；`_resolve_context` 缓存 `product_name` 供扩词锚点；meta_filter 透传 |
| `app/data/mcp_mapping.py` | 258 | MCP 工具注册 + 入参构造：`ad_portfolio_list` 接入 (2026-07-09)；`bootstrap_tools_for_meta()` 按 meta_filter 按需跳过 campaign keyword bootstrap |
| `app/data/mcp_registry.py` | — | ★MCP 工具注册集中化（v3.21 新增）：单一真源管理 MCP 工具定义，`mcp_adapter` 适配 |
| `app/api/config_mirror.py` | — | ★配置保存镜像（v3.23 新增）：Agent 配置双向同步 API，保证运营配置与 state DB/ERP 一致性 |
| `app/workflow/steps/campaign_portfolio.py` | — | ★组合分类器 + 双向映射归一化来源（`GROUP_CODE_TO_LABEL`/`GROUP_LABEL_TO_CODE`）；阈值常量从 guardrails re-export |
| `app/workflow/steps/campaign_budget_summary.py` | — | ★预算汇总：3 组约束分配 (主力/测试/广泛)，优先 MCP portfolio 真实值，淘汰不参与约束 |
| `app/workflow/steps/campaign_new.py` | — | ★新增活动分析线 (KB 16/28)：候选词发现(flow/own/竞品)→硬过滤→双轮 LLM 选词取交集→**与搜索词提精准双来源合流**（`merge_new_campaign_decisions`）→统一组装（`finalize_new_campaign_decisions`，bid/预算/命名代码确定性产出）；**v3.25**：`_derive_match_type` 对 `source=flow` 候选一律先建 BROAD（词形分类不再决定 EXACT） |
| `app/data/new_keyword_fetcher.py` | — | ★多源候选词发现统一编排器（v3.21 新增）：flow_keywords/own_keyword_flow/competitor_reverse 三源并行 + 配额分配 + 来源合并去重（供流量来源） |
| `app/workflow/steps/campaign_search_term_promotion.py` | 104 | ★搜索词提精准确定性准入（v3.25 新增）：`build_search_term_promotion_decisions()` 按 KB23 §3.4 订单通道（7d 订单≥3 且 ACOS≤目标）/词根通道（聚合订单≥3 或 ACOS ok）从搜索词报告候选生成 EXACT 决策，`source=SRC_CONVERTED`/`SRC_BROAD_DERIVED`；CVR 通道缺品类基准刻意不伪造 |
| `app/core/campaign_sample.py` | 68 | ★活动级样本不足判定（v3.25 新增）：KB17 事实判定（`assess_campaign_sample`：上线<3 天 / 7d 花费<max($5,CPA×0.5) / 点击<10），不阻止 MCP、不清空候选，仅作搜索词取数门禁（`SKIPPED_CAMPAIGN_SAMPLE_INSUFFICIENT`） |
| `app/workflow/steps/campaign_budget_reallocation.py` | 304 | ★组合预算回算（KB23）：优先 MCP portfolio 真实值 > 60/20/20 兜底；`available_for_increase` 增量约束；validate 加正增长额度校验 |
| `app/workflow/steps/portfolio_execution.py` | — | ★组合预算调整真实执行（`/campaign/execute-portfolio-budget` 后端，06-17 新增） |
| `app/workflow/steps/advert_execution.py` | — | ★广告调整 MCP 真实执行（Part 6，6 工具→落 4 record 表）；**v3.22 增强**：immediate_exit 确定性执行 + 灰度卡执行钩子；**v3.24 重构**：终态异步轮询拆出 `task_poll_scheduler.py`，提交/轮询/回写三阶段解耦 |
| `app/workflow/steps/task_poll_scheduler.py` | 206 | ★taskId 轮询调度器（v3.24 新增）：进程内有界队列 + 固定协程消费者（默认 2 消费者 × 20 队列），提交前 reserve 名额，按 3m/6m/12m/24m 最多查 4 次终态，精确回写每行 pending，耗尽按 FAIL 回写 |
| `app/persistence/erp_writer/advert_exec_mapper.py` | — | ★ERP 执行硬护栏 + 请求映射唯一真源 |
| `app/persistence/erp_writer/repository.py` | — | ★ERP 读写仓库；**v3.22 增强**：灰度卡读写+池表同步+组合执行记录 |
| `app/data/core_keyword_fetcher.py` | 414 | ★核心词发现数据编排器（v3.18 新增）：MCP 拉关键词+listing→LLM semantic_core 判定→落库 ERP `t_advert_agent_core_keyword` |
| `app/workflow/steps/core_keyword.py` | 309 | ★核心词语义判定工作流（v3.18 新增）：数据编排+语义判定+批量落库+主流程 is_core 回填 |
| `app/api/core_keyword.py` | 62 | ★核心词 API 端点（v3.18 新增，v3.19 扩展）：`POST /analyze` 离线分析 + `/status` `/enable` `/disable` `/groups` 管理接口 |
| `app/core/core_keyword_policy.py` | 25 | ★核心词策略引擎（v3.19 新增）：按 campaign 分组聚合 semantics 判定结果 |
| `scripts/erp_db/migrate_core_keyword.sql` | 45 | ★核心词表 DDL（v3.18 新增） |
| `batch_core_keyword.py` / `batch_core_keyword.sh` | — | ★离线批跑脚本（v3.18 新增） |
| `tests/workflow/test_core_keyword.py` | 627 | ★核心词全链路单测（v3.18 新增） |
| `demo/campaign-panel/` | ~2465 | ★前端合并模块 (ES module + CSS `.camp-` 前缀 + 事件委托 + 密度切换 + 卡片点选 + 确认详情表)。**v3.19 新增核心词管理弹窗**：state.js(+60)+render.js(+53)+panel.css(+49)+events.js(+12) |

### 3.2 关键配置项（settings.py）

```python
# Campaign LLM
campaign_llm_concurrency: int = 50  # 单 ASIN 批次并发数（per-stream sem；2026-06-18 实测值）
campaign_batch_size: int = 10        # 每批活动数
campaign_llm_temperature: float = 0.3
# ★ v3.25: 每个活动进入广泛流 LLM 的搜索词技术容量上限；按 7d 订单/花费/点击排序后截取。
search_term_llm_max_terms_per_campaign: int = 20

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

# ★ ad_portfolio_list MCP 接入（2026-07-09）
campaign_portfolio_fetch_enabled: bool = True  # 拉取真实组合预算替换 60/20/20；关闭则纯走旧 share 兜底

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
| MCP 上下文解析 | `mcp_context_timeout` | 30s | settings.py |

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
| 06-01 | `fetch_campaigns` 加 wait_for(300s) | MCP 子调用挂死防护，超时返回降级 result |
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
| 07-01 | **ad_campaign_basic_info 批量查询** | `build_campaign_tool_args` 加 `campaign_name_list` 参数（逗号分隔，≤20），`campaign_fetcher` 删 `_fetch_basic_one` 新增 `_fetch_basic_batch`（分批 `asyncio.gather` 并行 + key 用入参名闭环），basic_info 调用从 N 次降为 `ceil(N/20)` 次（~95% 降幅），超时 300→420s。**已部署上线。** |
| 07-01 | **MCP 字段映射补全** | `_MCP_CAMPAIGN_KEY_MAP` 加 `关键词ID`/`广告活动ID`/`子ASIN`/`子卖家SKU` 新别名，修复 `关键词D`→`关键词ID` MCP 改名导致 keyword_id 全部丢失 |
| 07-01 | **ACOS 百分数归一** | `normalize_ad_summary`/`normalize_ad_keywords` 的 `acos`/`ctr`/`cvr` 从 `_float`→`_pct`（×100），修复数据监控 ACOS 显示 0.几 的 bug |
| 07-01 | **汇总 LLM 临时禁用** | `_SYNTHESIS_ENABLED = False`，临时跳过 campaign synthesis 调用 |
| 07-09 | **多词活动判定排除否定词** | `campaign_prefilter.py:_is_multi_keyword_campaign`：广泛活动（BROAD/PHRASE/AUTO）额外检查关键词总数，扣除否定关键词数后判定，避免自带大量否词的广泛活动被误判为多关键词而跳过分析 |
| 07-09 | **池同步 helper 提取** | `campaign.py:_sync_pool_entries_if_needed` 内嵌函数：全预过滤 early return 前和正常路径复评前共用同一份 discovery 入池+手动复评离池逻辑，消除两份代码漂移风险。同时全预过滤后无 LLM 分析可跑时仍调用同步，保证 KB21§7 复评路径不断 |
| 07-09 | **扩词相关性锚点补线** | `mcp_query.py`：`McpQuerySkillExecutor` 把 `db_ctx.product_name` 注入 `data.title`，供新增扩词流作相关性锚点（KB28 §2），复用 context 阶段已解析值，零额外 MCP 调用 |
| 07-09 | **★ ad_portfolio_list MCP 接入** | `campaign_fetcher.fetch_portfolio_list`：拉取 4 组真实组合预算（按关键词模糊匹配），fail-open 回退 60/20/20 兜底。`campaign_budget_reallocation.aggregate` 优先 MCP 真实值作 `current_group_budget`，新增 `available_for_increase` 增量约束；`validate` 加正增长额度校验。`campaign_budget_summary.build_summary` 优先 MCP 真实值。`reasoner` KB23 prompt 重写——起点从 base_constraint 改为 current_group_budget。`settings` 新增 `campaign_portfolio_fetch_enabled` 开关。`mcp_adapter`/`mcp_mapping` 接线 `ad_portfolio_list` 工具含 `qryFixedPortfolio` |
| 07-10 | **前端 UX 全面升级** | ①侧栏折叠持久化（localStorage + 切换按钮）②全局 Toast 替换 alert ③MCP 网关 502/503/504 快速降级兜底（40s 超时返 degraded result）④请求加载提示（底部浮层）⑤Campaign 分析遮罩含放弃确认（60s 自动消失）⑥P3 手动输入防 AI 刷新覆盖（`preserveManualInputs`/`restoreManualInputs`/`_allowAiOverwriteOnce`）⑦`updateMultiTags` 程序调用不标手动（`fromUser:false`）⑧按钮脉冲/筛选器闪烁/batchBar 切换动效 ⑨版本号 v3.1→v2 |
| 07-10 | **campaign-panel 前端重构** | ①卡片密度三档切换（紧凑/舒适/详细）localStorage 持久化 + `_syncDensityDetails` JS 联动详情区 ②meta 行彩色 chips（`_renderMetaChips`）替换旧管道文本 ③卡片 body 点击勾选（文字拖选保护）④选中卡片高亮（蓝色左边框+背景）⑤确认执行弹窗表格详情（活动/关键词/动作/预算/出价/广告位）⑥密度切换不覆盖用户手动操作过的卡片（`_detailUserToggled`）⑦筛选器/按钮动效（`camp-filter-flash`/`camp-btn-pulse`/`camp-batch-flash`） |
| 07-10 | **清理 demo 旧数据** | 删除 `demo/campaign_sample.json`（4995 行旧样本）、`demo/campaign_test.html`（1464 行旧调试页）、`docs/README.md`（过时文档索引）、`scripts/synthesis_output.txt`（旧样本输出）；新增 `demo/codex_ux_patch.js`、`tests/test_demo_ux_migration.py`、`docs/superpowers/` 计划文档 |
| 07-10 | **护栏独立模块 + 组合映射归一化** | ★ `campaign_guardrails.py`（新，384 行）：从 `_resolve_budget_conflicts` 提取全部护栏逻辑为 `apply_all()`，含淘汰保护/强制淘汰/淘汰硬填充/日预算上限/库存退货评分护栏；阈值常量+谓词为 `campaign_portfolio` 侧单一真相源。`campaign_portfolio.py`：新增 `GROUP_CODE_TO_LABEL`/`GROUP_LABEL_TO_CODE` 双向映射；`campaign_viewmodel.py`/`text_utils.py` 删除本地硬编码映射改为 import。`campaign.py`：`_resolve_budget_conflicts` 委托给 guardrails，新增 `inventory_days`/`refund_rate`/`rating` 参数。`reasoner.py`：精准/广泛 prompt 示例输出删 child_asin/keyword_text/match_type/current_budget/current_bid（cid 句柄已回填）；`recommend_new_campaigns` 加 JSONDecodeError 重试+max_tokens 4096→8192。阈值修正：Bid≤$0.21→$0.20、Budget≤$1.01→$1.00、退货率阻断 25%→30% |
| 07-10 | **KB 阈值同步 + 审核等级统一** | 全部 17 个执行规则 KB 文件阈值对齐：Bid≤$0.21→$0.20、Budget≤$1.01→$1.00、退货率 25%→30%。`campaign_new.py`/`campaign_restart.py`：`AUTO_BATCHABLE`→`AUTO_APPROVED`（与 ERP pending 表消费端约定统一） |
| 07-10 | **审核等级中文标签** | `campaign_viewmodel.py`：新增 `_REVIEW_LEVEL_LABELS` 映射（`AUTO_APPROVED`→可直接执行、`MANUAL_REVIEW`→需人工审核、`HIGH_RISK_REVIEW`→高风险审核、`BLOCKED`→应阻断执行）；兼容旧枚举 `AUTO_BATCHABLE`/`SENIOR_APPROVAL`；`from_db_snapshot` 的 `review_level` 输出中文标签 |
| 07-10 | **★ 护栏优先级重构 + R3/R4 LLM 重试编排** | `campaign_guardrails.py`：全部 12 条规则新增 `retry_instruction`+`campaign_key` 字段；P3 硬淘汰（无单且 bid≤$0.10/budget≤$1）优先级升至 P1 样本不足之上，P0/P2 仍高于 P3；P4/P7/P8/P10/P11 修复边界 bug；提取 `_sample_insufficient`/`_p3_should_force_eliminate` 公共谓词。`campaign.py`：R3/R4 重试编排循环——护栏拦截后按 campaign_key 带告警回灌 LLM 重判，最多 R3→R4 两轮，R4 后不再 R5，最终护栏兜底；`_apply_campaign_guardrails` 返回 `(GuardrailPass, warnings)`；`_backfill_campaign_adjustment_context` 提取复用。`reasoner.py`：`_guardrail_instruction` 注入批次提示 + `_guardrail_alert` 注入单活动告警。新增 `test_campaign_guardrails.py`(497行) + `test_campaign_guardrail_retry.py`(210行)；`test_campaign_resolve_conflicts.py` 更新 P3>P1 预期。6 files +1206/-111。 |
| 07-11 | **★ 秒开优化：策略层轻量化 + meta_filter 分级缓存** | `tactics.py`：`run_get_tactics_options` 替换为纯缓存+长期配置读取版本，零 MCP/LLM 调用（旧版保存为 `_run_get_tactics_options_legacy`）。`diagnosis.py`：删除关键词 AI 分类自动回访逻辑（25行），避免缓存 miss 时触发重量级 MCP 拉取。`workflow_orchestrator.py`：缓存 key 加入 `meta_filter` 维度（`_data_task_key`），不同 filter 组合独立缓存不互串。`mcp_mapping.py`：BOOTSTRAP_TOOLS 拆为 `BASIC_BOOTSTRAP_TOOLS` + `CAMPAIGN_KEYWORD_BOOTSTRAP_TOOLS`；新增 `bootstrap_tools_for_meta()` 按 meta_filter 按需跳过 campaign keyword bootstrap。`settings.py`：新增 `meta_filter_dashboard_light` = [META_AD_PRODUCT, META_TREND]。新增 `test_lightweight_loading.py`(116行) + MCP 诊断文档 2 篇。18 files +466/-64。 |
| 07-11 | **护栏告警仅用 retry_instruction** | `_build_guardrail_alerts` 不再回落 `message`（含规则编号的内部消息不应灌入 LLM prompt），`retry_instruction` 为空时直接跳过。test 补空值断言。2 files +25/-4。 |
| 07-12 | **挪组 portfolioId 解析重构** | `advert_execution.py`：`_resolve_modify_portfolios` 移回 MCP 调用前执行，按 portfolioId 拆分 paramsVoList，匹配失败写入 `move_errors` 分类（不存在/多个/ID缺失），失败活动移除 campaignGroupType 后其他字段继续下发。`ExecPlan` 新增 `move_errors` 字段，confirm API 透传。`_match_portfolio` 改为单向子串匹配 + 返回 `(match, count)` 元组。`state.js` 前端按 group+reason 去重展示 sticky toast 含具体活动名。6+4+1 files。 |
| 07-12 | **★ Product Identity 全线注入** | `product_identity.py`(新)：GET/POST 解析 `(asin, shop_id, parent_seller_sku)` 三元组，`require_product_identity_dict` 支持 path-param asin。`layers.py`：`ProductIdentityMixin` 注入 Strategy/Tactics/Execution/P3 全部 Confirm/Select 模型。`workflow_orchestrator`：缓存无 identity 数据自动丢弃重拉。`mysql_state_manager`/`schema.sql`：state DB 加 identity 列+写回。`purpose_adapter`：删 60 行重量级回退逻辑。删除过时交接文档。新增项目知识图谱 16 篇+计划文档+5 个测试。51 files +4669/-247。 |
| 07-12 | **_snapshot_action 防御加固** | 补 `cat="ADJUST"` 显式映射；淘汰回退判定加 `not cat` 条件，避免 REACTIVATE/ADJUST 卡因 group=low_bid 被误判为淘汰。1 file +4/-2。 |
| 07-12 | **KB29 核心词定义规则接入** | `kb_loader.py`：KB29 从预留位激活接入 `29-核心词定义规则.md`。`repository.py`：summary warnings 字段截断至 500 字符防超长写入。知识图谱 08 大幅扩充。4 files +375/-46。 |
| 07-12 | **★ 核心词管理系统（is_core 真实数据源闭环）** | `core_keyword_fetcher.py`(新,414行)：MCP 拉关键词+listing→LLM `recommend_semantic_core()` 判定语义冲突(4种)+R1 精确相关→落库。`core_keyword.py` workflow(新,309行)：数据编排+语义判定+批量落库。`core_keyword.py` API(新,62行)：`POST /core-keyword/analyze` 离线 endpoint，`core_keyword_analyze_enabled` 闸控。`reasoner.py`：新增 `_SEMANTIC_CORE_PROMPT`(KB29 §1-6) + `recommend_semantic_core()`。`campaign.py`：主流程入口读核心词标签注入 `is_core`（填了从 v2.0 起一直硬编码 False 的坑）。`repository.py`：核心词表读/写方法。`settings.py`：`core_keyword_*` 4 项配置 + `azlisting_mcp_*` 独立 MCP 连接。`migrate_core_keyword.sql`(新) + `batch_core_keyword.py/.sh`(新) + `test_core_keyword.py`(新,627行)。18 files +2022/-8。 |
| 07-16 | **★ campaign_key 关键词级重构** | `campaign_fetcher`：campaign_key 从 `"活动名×ASIN"` 改为 `"活动名×ASIN#match_type#keyword_id"`，消除同活动同词 BROAD/PHRASE 的 unit_by_key 碰撞。`models/campaign.py`：文档同步关键词级语义+聚合覆盖风险说明。`campaign_prefilter`：新增规则 0——否定词不进入 LLM 分析；match_type 读取优先级补 raw 字段。`campaign.py`/`mappers`：同步适配新 key 格式。`render.js`：前端适配。删除 `demo/codex_ux_patch.js`(已内化)。12 files +438/-33。 |
| 07-16 | **★ 核心词后端：policy 引擎 + API 扩展 + 分组落库** | `core_keyword_policy.py`(新)：核心词策略引擎，按 campaign 分组聚合 semantics 判定结果供前端渲染。`core_keyword.py` API 扩展：`GET /status` 任务状态+`POST /enable`/`POST /disable` 手动标注、`GET /groups` 按 campaign_id 分组查询。`repository.py`(+281行)：`query_core_keyword_groups` 分组查询、`batch_update_core_keyword` 批量更新人工标注、`get_core_keyword_task_status` 刷新机制。`core_keyword_fetcher.py`(+19行)：数据编排器增加分组信号。`core_keyword.py` workflow(+27行)：state 表改名+唯一任务 ID+重复变更防护。 |
| 07-16 | **★ 核心词前端：Web 管理弹窗** | `state.js`(+60行)：`_coreKeywordTab`/`_coreKeywordGroups`/`_coreKeywordTaskId` 状态管理 + `loadCoreKeywordGroups()`/`toggleCoreKeywordEnabled()` 操作。`render.js`(+53行)：`_renderCoreKeywordModal()` 渲染弹窗——语义判定结果展示(semantic_conflict pass/fail badge + semantic_core 标记)、按 campaign 分组折叠面板、手动开关控件、空态提示。`panel.css`(+49行)：弹窗样式(v3 设计)、分组折叠动画、badge 色值。`events.js`(+12行)：事件委托 `camp-toggle-core-keyword` 等。`panel.js`(+1行)：挂载核心词入口。 |
| 07-17 | **Codex 批跑 + 复盘记忆 + SkillOpt** | `batch_via_api_codex.py`(新)：Codex 复核批量调度入口，替代旧 batch_via_api.py 集成 deepseek-v4-pro review hook。`batch_night_monitor_codex.sh`(新)：守夜监控适配 Codex 批跑。`split_review_memory.py`(新)：复盘记忆加工脚本。新增 SkillOpt 实施方案文档 3 篇 + 复盘记忆落地实施方案文档 2 篇 + 链路跑通测试记录。 |
| 07-17 | **批跑脚本迁移 scripts/** | 7 个批跑脚本从仓库根目录迁移到 `scripts/` 子目录（batch_core_keyword/batch_monitor/batch_night_monitor/batch_via_api 等）。9 files。 |
| 07-17 | **预过滤逻辑收口 + 删死代码** | `campaign_prefilter`：淘汰池预过滤从 campaign.py 迁入独立函数 `filter_eliminated_pool()`；删除非 ENABLED/近7天无数据两个死代码分支。`campaign.py`：编排层委托给 prefilter。`test_campaign_prefilter.py`(新)。7 files +374/-146。 |
| 07-17 | **Codex 复核 hook 预埋 + decisionId 追踪** | `campaign.py`：Codex review hook 代码预埋（暂注释，待 CODEX_FORCE 修复后启用）。`advert_exec_mapper`：paramsVoList 补 decisionId 字段。3 files +139/-6。 |
| 07-17 | **KB 细粒度切片 + core_keyword_policy 迁移 + 文档清理** | `kb_loader.py`(+97行)：KB 切片粒度细化。`core_keyword_policy.py` 从 `app/` 移到 `app/core/`。删除 6 篇过期文档。`test_campaign_cache_context.py`(新) + `test_kb_slicing.py`(+54行)。28 files +542/-1312。 |
| 07-17 | **组合预算 spend 多周期窗口** | `to_budget_summary`/`build_summary` 新增 `portfolio_spend_{1d,3d,7d,14d,30d}` 五个周期。`campaign_fetcher.fetch_portfolio_list` 补 spend 字段提取。前端组合预算卡多周期对比。`migrate_portfolio_spend_windows.sql`(新)。10 files +351/-78。 |
| 07-22 | **★ KB30/31/32 新规则 + 广泛否词执行链路** | KB30(新)：广泛广告否词与搜索词治理规则。KB31(新)：样本窗口与生命周期状态规则。KB32(新)：自动广告调整规则。`reasoner`：广泛流 prompt 集成 KB30 否词指令。`advert_execution`：`agent_create_negative_keywords` MCP 真实否词下发。`campaign_viewmodel`/`repository`/`mappers`：灰卡补否词字段+落库。`render.js`(+66行)：前端展示否词建议列表。`campaign_fetcher`(+29行)：补 search_term 数据源。`test_negative_keyword_persistence.py`(新)。24+8 files +1696/-289。 |
| 07-25 | **MCP registry 集中化 + 新增活动选词重构** | `mcp_registry.py`(新)：MCP 工具注册收口到单文件，`mcp_adapter` 适配。`new_keyword_fetcher.py`(新)：多源候选词发现统一编排器，`campaign_new.py` -130 行委托给 fetcher。`models/campaign.py`：新增 `NewKeywordCandidate` 模型含 priority_caps。`campaign_portfolio.py`：补混合匹配冲突阻断 filter 调用。`test_new_keyword_priority_caps.py`(新) + `test_portfolio_match_and_exec.py` 补冲突用例。18 files。 |
| 07-25 | **★ 战略层新增经营模式(operating_mode)** | `layers.py`：`OperatingMode`(6值)+`AdPermission`(3级)+`operating_mode_to_permission()` 纯函数。`layer_options.toml`：第 4 个 radio 组。`text_utils.py`：`map_operating_mode`/`unmap_operating_mode` 中英文互转。`repository`/`auto_push`/`decision_config_reader`：ERP 决策表读写。`decision`/`long_term_config`：API 透传。`mysql_state_manager`/`schema.sql`：state DB 加列。`campaign.py`：`build_campaign_strategy_context` 接入 operating_mode。`migrate_operating_mode.sql` + `migrate_state_schema_columns.sql`(新)。test 5 个。28 files +524/-178。 |
| 07-25 | **.gitignore + core_keyword limit 扩大** | `.gitignore` 加 `logs/` 目录。`repository.py`：核心词策略查询 limit 30→60。 |
| 07-28 | **★ KB v2.0 重构：总纲+ontology 补全+切片矩阵** | KB 知识库全面重组：新增 `00-知识库总纲与切片覆盖矩阵.md`（KB 全景索引+切片矩阵）、`30-动作词表与映射.md`；新增 ontology `runtime_contract.yaml`（运行时契约）、`evidence.yaml`（证据类型）；全部 01-32 号 KB 文件+7 个 ontology YAML 大范围更新；`kb_loader.py` preset 切片粒度调整（+63/-28）。前端 `ad-asisitant-agent.html` 新增 operating_mode radio 组；`layers.py` AdPermission 枚举+operating_mode_to_permission 纯函数；全部 5 个工作流 step（strategy/tactics/diagnosis/execution/wizard）同步接入 operating_mode。9+4 files +4741/-685。 |
| 07-28 | **KB 旧切片清理** | 删除 3 个已过时/重复的 KB 执行规则文件：`30-广泛广告否词与搜索词治理规则.md`（内容已迁移）、`31-样本窗口与生命周期状态规则.md`（合并到新 31）、`32-自动广告调整规则.md`（合并到新 32）。新增统一版 `25-目标ACOS预算Bid推荐.md`（从执行规则升到 KB 根目录）、`31-广泛自动词组调整规则.md`、`32-精准组合升降级规则.md`。5 个 ontology YAML + 13 个 KB 切片同步更新。31 files +189/-1015。 |
| 07-28 | **★ ERP 执行状态管理+测试大面积扩展** | `advert_execution.py`（+439）：immediate_exit 确定性执行链路。`repository.py`（+368）：灰度卡读写+池表同步+组合执行记录。`advert_exec_mapper.py`（+99）：ERP 硬护栏增强。`decision.py`（+929）：决策流程重构。`campaign_fetcher.py`（+38）：补三日验证数据。测试大面积扩展：`test_portfolio_match_and_exec.py`（+1128）、`test_product_identity_required.py`（+908）、`test_erp_gray_cards.py`（+359）、`test_mcp_campaign_discover.py`（+338）、`test_advert_exec_child_asin.py`（+177）。前端 `ad-asisitant-agent.html`（+75）。新增实施方案文档 `2026-07-28-immediate-exit-deterministic-execution.md`。17 files +6161/-114。 |
| 07-28 | **kb_loader 预设精简 + access-guard.js** | `kb_loader.py`：切片预设精简（-28 行冗余）。新增 `demo/access-guard.js`：前端入口守卫，页面加载前校验 product identity 三元组。`settings.py`（+2）、`campaign.py` 微调。5 files +44/-28。 |
| 07-29 | **★ 精准组合确定性升降级** | `acos_constraints.py`（新）：ACOS 约束核心模块。`campaign_exact_transition.py`（新）：精准组合升降级逻辑（EXACT 活动四组升降级判定）。`campaign_fetcher.py`（+201）：增强数据编排。`campaign.py`（+330）：策略上下文扩展 + 升降级调度。`repository.py`（+246）：ERP 读写增强。`text_utils.py`（+58/-20）：映射表扩展。`campaign_portfolio.py`（+102）：组合路由增强。`advert_execution.py`（+52）、`portfolio_execution.py`（+24）：执行链路同步。`layers.py`（+62）、`campaign.py` models（+11）：模型扩展。测试：5 个文件 +420。新增 `docs/精准组合确定性升降级实施方案.md`。27 files +2616/-235。 |
| 07-30 | **★ 分析事件取消 + 运行闸门** | `analysis_run_guard.py`（新）：分析运行闸门模块，防重复分析+session 取消态管理+运行前幂等校验。`mysql_state_manager.py`（+128）：session 取消/运行状态持久化。`state_manager.py`（+105）：运行闸门接口。`schema.sql`（+11）：analysis_session 加 cancelled 列。`decision.py`（+59）：取消事件 API + new-event 闸门校验。`campaign.py` API（+87）：运行前闸门接入。`campaign_new.py`（+22）：新增活动线闸门适配。`ad-asisitant-agent.html`（+74）：前端放弃确认闭环。新增测试 `test_campaign_cancellation_fencing.py`（144行）+ `docs/sql/2026-07-30-analysis-session-cancellation.sql` + `docs/superpowers/plans/2026-07-30-analysis-event-cancellation-run-fencing.md`。17+3 files +1280/-99。 |
| 07-30 | **配置镜像 API + Codex 批跑** | `config_mirror.py`（新）：Agent 配置保存镜像 API，双向同步运营配置到 state DB + ERP。`decision_config_reader.py`（+44）：批量读取增强。`batch_via_api_codex.py`（新，308行）：Codex 复核批量调度入口，替代旧 batch_via_api.py 集成 deepseek-v4-pro review hook。新增测试 `test_config_mirror.py` + `test_agent_config_mirror.py` + `test_agent_config_batch_reader.py`。6 files +370/-14。 |
| 07-30 | **交接文档 v3.22 全面重构** | 历史迭代日志 §7-§27（~800行）压缩为 §7 摘要（40行）+ 新增 §8-§13 当前架构说明（经营模式/精准升降级/护栏/复评/执行层/核心词）。修正过时阈值（LOW_BID_MAX 0.21→0.20）、护栏规则数（11→12）、KB 预设表（6→13项完整预设）、_SYNTHESIS_ENABLED→settings。详见 commit `d0958c4`。 |
| 07-30 | **分析闸门加固 + session 修复** | `mysql_state_manager.py`/`state_manager.py`/`schema.sql`：取消态补全。`campaign.py`/`decision.py` API：取消逻辑修复。`campaign_new.py`：闸门适配。`ad-asisitant-agent.html`：前端适配。新增 `test_campaign_cancellation_fencing.py` + `batch_via_api_codex.py`。13 files +681/-73。 |
| 07-31 | **Campaign 双轮投票切除** | `campaign.py`（-510 行）：切除 R1+R2 双轮投票 → 单轮 LLM 直判。`campaign_guardrails.py`（+55）：护栏增强。`campaign_exact_transition.py`（+11）、`reasoner.py`、`models/campaign.py`、`settings.py`、`mappers.py`：适配单轮模式。`test_campaign_guardrails.py`（+47）。新增 `docs/Campaign切除双轮投票方案.md`，删除过期代码审计报告。17 files +827/-680。 |
| 08-03 | **★ pending taskId 轮询调度器** | `task_poll_scheduler.py`（新，206 行）：进程内有界队列 + 固定协程消费者（2×20），提交前 reserve 名额，按 3m/6m/12m/24m 最多查 4 次终态，精确回写每行 pending，耗尽按 FAIL 回写。`advert_execution.py`（720 行重构）：提交/轮询/回写三阶段解耦。`repository.py`（309 行）：执行仓库适配 + `write_pending_terminal` 精确回写。`decision.py`（+36）、`campaign.py`（+25）、`settings.py`（+5：`advert_task_poll_workers`/`advert_task_poll_queue_capacity`）、前端（+31）。测试重构：`test_task_poll_scheduler.py`（新，127 行）、`test_portfolio_match_and_exec.py`（-591 精简）、`test_erp_gray_cards.py`（-120）。新增 spec `2026-08-03-pending-taskid-polling-design.md`。13 files +1271/-1160。 |
| 08-04 | **★ 搜索词精准扩词双来源 + 样本过滤** | `campaign_search_term_promotion.py`（新，104 行）：搜索词提精准确定性准入（KB23 §3.4 订单/词根两通道，CVR 缺品类基准刻意不伪造），产出 EXACT 决策（`SRC_CONVERTED`/`SRC_BROAD_DERIVED`）。`campaign_sample.py`（新，68 行）：KB17 活动级样本不足判定；样本不足活动搜索词取数标 `SKIPPED_CAMPAIGN_SAMPLE_INSUFFICIENT` 仅观察、不进提精准。`campaign_fetcher.py`（+284）：`build_search_term_bundle` 活动级 7d/14d 搜索词 bundle。`reasoner.py`（+207）：广泛流搜索词行渲染 + LLM 回吐 `exact_promotion_candidates` 按原始搜索词报表权威回填校验。`campaign_new.py`（+244）：`merge_new_campaign_decisions` 双来源合流（搜索词提精准覆盖同词 flow 探索项）+ `finalize_new_campaign_decisions` 统一组装一次；`_derive_match_type` 对 `source=flow` 候选一律先建 BROAD（词形分类不再决定 EXACT）。`models/campaign.py`（+37）：`SearchTermPromotionCandidate`/`NewCampaignDecision` 模型。`settings.py`（+4）：`search_term_llm_max_terms_per_campaign=20`、`campaign_new_max_creates 20→15`。`campaign.py`（+176）/`campaign_guardrails.py`（+51）：编排接入。测试 8 个（`test_campaign_search_term_promotion.py`+367、`test_campaign_dual_source_orchestration.py`+95、`test_campaign_search_term_bundle.py`+101 等）。20 files +1733/-166。 |

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
| ~~Synthesis 汇总合成恢复~~ | ✅ 已完成 | 2026-06-08 恢复 `settings.campaign_synthesis_enabled=True`（当前默认 False），并改造(去 KB / 组数 5-7 / special≤5-15 / max_tokens 8192)。详见 §7.1 |
| ~~R3 tiebreaker 端到端验证~~ | ✅ 已移除 | 2026-07-31 切除 R1+R2 双轮投票，tiebreaker 机制随之删除，由护栏 R2/R3/R4 重试替代 |
| 策略上下文→决策联动 | P1 | KB 19/21/22 缺策略联动规则（KB 03 已在 campaign preset 外） |
| ~~**新增广告活动分析**~~ | ✅ 已完成（2026-06-10/11） | 三股并行独立分析线。**剩余子项**（2026-07-04 核实）：① suggestedBid 数据源仍未接入（bid 占位 $0.30，改 `_calc_initial_bid` 一处即可）；② KEYWORD_PROMOTED_FROM_BROAD 等 5 个触发场景确认仅作展示标签、不做门禁（设计决策，非待办）；③ ~~新增活动预算接入组合回算~~ → ✅ 已实现（`campaign_budget_reallocation.py:160-178`，new_campaigns 整笔 proposed 入组 delta）；④ ERP 写入 → 已通过 `write_full` 统一落库 |
| ~~DB 落库~~ | ✅ 已完成 | `t_advert_agent_campaign_analysis` + `_adjustment` 表已通过 `write_full` → `_upsert_modern_summary` + `_upsert_campaign_cards` 全链路落库 |
| ~~L420 投票 key 同源化~~ | ✅ 已解决（2026-06-26） | 改 cid 句柄方案：LLM 回吐批内 `C1..Cn`，代码 `cid_map` 权威回填 campaign_key（不再依赖 LLM 复现长串）；并补缺轮兜底（单轮/双轮缺失送 R3） |
| ~~**`is_core` 核心词真实数据源**~~ | ✅ **v3.18 已实现** | ★核心词管理系统上线：`core_keyword_fetcher` → `recommend_semantic_core()` 语义判定 → ERP 落库 → campaign 主流程 `is_core` 回填。离线 7 天一批，`batch_core_keyword.py` + crontab 定时跑。详见 §3.4 07-12 条目 |
| ~~`POST /campaign/confirm` 落地~~ | ✅ 已完成 | campaign.py:433 已实现，confirm_decisions 落 ERP pending 表 + 推送 |
| KB 遵循度评分器 | P2 | 消费实验 JSONL。2026-07-04 核实：全工程 0 引用，未实现 |
| ~~A1 修复~~ | ✅ 已完成 | `_ensure_data` 已支持 meta_filter 按 filter 分 key 缓存（§15.5 确认） |
| campaign_key 关键词级 → card 聚合覆盖 | P1 | v3.19 将 campaign_key 改为关键词级(加 #match_type#keyword_id)消除碰撞，但同活动多关键词单元合并为一张 card 时 budget/campaign_pending/placements 仍后写覆盖。长期应拆 campaign_key(活动级) + campaign_unit_key(关键词级) |
| **campaign_key 粒度拆分** | P2 | ★2026-07-16：`campaign_key` 已从活动级改为关键词级（含 `#match_type#keyword_id`），消除了同活动同词 BROAD/PHRASE 的 `unit_by_key` 碰撞（A04）。但下游 card/预算/广告位/复盘仍按 `campaign_id` 聚合（一张 card），同活动多关键词单元合并时 `campaign_pending`(预算) 和 `placements_by_type`(广告位) 存在后写覆盖，`synthesis key_to_card` 映射可能漏掉非主 key。长期应拆为 `campaign_key`（活动级，聚合用）+ `campaign_unit_key`（关键词级，回填/定位用）。当前影响面小，代码点位已标 ⚠ 注释。详见 `mappers.py:369-372`, `repository.py:1050-1052`, `campaign_fetcher.py:1003-1007`。 |

### 4.3 设计决策汇总

| 决策 | 说明 |
|------|------|
| action 归一化 | LLM 只给淘汰判据 (`eliminate_to_low_bid_pool`),其余 action 由代码从 proposed vs current 差值 derive |
| 组合分类顺序 | 低价捡漏组 → 自动广泛组 → 精准测试组 → 精准主力组(命中即止)。精准组按 **KB23 §3.1 纯预算 $5 分界**(≥$5 主力 / <$5 测试),**不再用 days_online 判新建**。枚举名 2026-06-05 已改(详见 §3.4) |
| 淘汰组不参与预算约束 | KB 21 §6 每活动固定 $1,与运营策略预算无关。3 组预算来源：★ v3.13 起优先 MCP ad_portfolio_list 真实值，失败回退 settings 60/20/20 兜底 |
| 组合预算回算 | ★ v3.13: KB23 回算起点从 base_constraint 改为 current_group_budget（portfolio 真实值）；新增 available_for_increase 增量约束（淘汰释放+允许净增）；validate 校验正增长合计不超可用额度 |
| meta_filter 优化 | ASIN data 只拉 META_AD_PRODUCT,砍掉 Campaign 用不到的 6 个 META(natural_rankings/flow_keywords 等 150s 慢查询) |
| 空 CampaignData 不入 Redis | 防 MCP 临时失败毒化缓存(30min 空窗口) |

### 4.4 失败路径覆盖矩阵（多层防护后）

| 失败点 | 旧行为 | 新行为 |
|---|---|---|
| MCP 上下文挂起 | analyze 永远挂 | 300s timeout → 返回空 CampaignData + warning |
| `aggregator.fetch` 120s 超时 | 500 | warning「数据拉取超时」+ 空 result |
| LLM 单批挂 60s | 该 batch 失败但其他 OK | 同（既有） |
| LLM 返回非 dict | AttributeError → 整流崩 | 转单批失败 warning |
| LLM action 不一致 | 投票阶段分歧增多 | `_normalize_action()` 代码 derive 权威 action，护栏兜底（切单轮后无投票分歧） |
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
http://localhost:8008/demo/ad-asisitant-agent.html

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
Campaign timing [ASIN] +Xs: DONE fetch_portfolio     # 组合预算拉取耗时(v3.13 新增)
Campaign timing [ASIN] +Xs: DONE budget_summary      # 预算汇总耗时
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

## 7. 历史迭代摘要（2026-06-08 ~ 06-25）

> 以下为早期迭代的关键成果摘要，详细逐日记录已移除。完整 git 历史见 `git log --oneline`。

### 7.1 核心架构形成（06-08 ~ 06-11）

- **总览 + 汇总恢复**：`campaign_overview_enabled=True`(settings.py) + `campaign_synthesis_enabled`(settings.py，当前默认 False)。overview 从"现状数字复述"改为"KB×现状 → 判断 → 定调"；synthesis 只做语义聚类写叙事，不重复加载 KB。
- **新增活动分析线**：三股并行（精准流/广泛流/新增流），候选词发现→硬过滤→双轮 LLM 取交集→代码补齐 bid/budget/placement。`campaign_new.py` 独立模块。
- **广告位加价比例**：MCP 字段接入 + 代码回填 `current_pct`（LLM 只判方向、代码算数值）。
- **前端合并**：`campaign-panel/` ES module 独立模块，挂载到主看板第 5 tab。原 `demo/campaign_test.html` / `demo/campaign_sample.json` 已删除（2026-07-10）。
- **预过滤可见化**：淘汰池/多词活动打 `__prefiltered` 标记，前端灰卡展示。

### 7.2 决策批次 + 执行层（06-12 ~ 06-15）

- **决策批次状态机**：A/B/C 三态页面（首访/快照只读/进行中锁定），`run_id` 批次句柄，`decision_id` ERP 主键。
- **快照回读**：`campaign_viewmodel.from_db_snapshot` 21 表→ViewModel 反向映射。
- **Part 6 真实执行**：confirm → MCP 6 工具 → 4 张 `_record` 表。默认 `dry_run=true` 安全闸。
- **ERP 全链路落库**：cards + summary + synthesis + pending 完整写回。
- **双轨读回**：Tab1(核心词)/Tab3(P3)/Tab4(方向) 快照富化。
- **自然排名接入**：`_fetch_keyword_ranks` 旁路拉取（纯 MCP、纯精准），超时 45s fail-open。

### 7.3 可靠性与稳定性（06-16 ~ 06-25）

- **data_unavailable 区分**：fetch 失败不再伪装 "no adjustments"。
- **StarRocks BE 重试**：`starrocks_retry.py` 单一真源，带签名 1064 自动重试。
- **MCP 日期窗口按站点**：`make_date_window(days, site_code)` 6 站点硬编码时区。
- **override 持久化**：acos/预算 override 跨日/跨事件保留（删过期 DELETE 分支）。
- **目标 ACOS 区间化**：`compute_target_acos_band()` 算法+prompt 双口对齐，阶段优先+层级兜底。
- **新增词长尾优先**：排序改词数多者优先，去 category/brand 品类锚点误导。
- **库存口径统一**：两路径均用 `can_sale_num`。

---

## 8. 经营模式（Operating Mode）

> v3.21 新增，详见 commit `e2cffa0` 和 `01fca90`。

### 8.1 模型定义

`app/models/layers.py`：

| 枚举 | 值 | 说明 |
|------|------|------|
| `OperatingMode` | `IMMEDIATE_EXIT` / `CONTROLLED_CLEARANCE` / `LIMITED_REPAIR` / `STABLE_OPERATION` / `ACTIVE_PUSH` / `PROFIT_HARVEST` | 6 种经营模式，人工录入 |
| `AdPermission` | `STOP` / `CLEARANCE_ONLY` / `NORMAL` | 3 级广告权限，由经营模式推导 |

`operating_mode_to_permission(mode)` 纯函数：`IMMEDIATE_EXIT` → STOP / `CONTROLLED_CLEARANCE` → CLEARANCE_ONLY / 其余 → NORMAL。

### 8.2 数据流

```
前端 radio 组 (layer_options.toml)
  → API (decision/long_term_config) 透传
  → ERP 库 t_advert_agent_decision_config 落库 (text_utils map/unmap)
  → state DB strategy_config 表 (mysql_state_manager)
  → campaign.py build_campaign_strategy_context 接入 operating_mode
  → LLM prompt 注入经营模式上下文
```

### 8.3 工作流注入

全部 5 个工作流 step（`strategy/tactics/diagnosis/execution/wizard`）均从 `ProductIdentityMixin` 继承，接收 operating_mode 字段。

---

## 9. 精准组合确定性升降级

> v3.22 新增，详见 commit `cbeb14c`。核心模块：`acos_constraints.py` + `campaign_exact_transition.py`。

### 9.1 ACOS 约束模块 (`app/core/acos_constraints.py`)

目标 ACOS/预算的硬约束计算引擎。独立于 LLM，纯代码判定：
- 输入：目标 ACOS、实际 ACOS、经营模式、产品阶段
- 输出：升降级判定 + 约束边界值

### 9.2 精准组合升降级 (`app/workflow/steps/campaign_exact_transition.py`)

EXACT 活动在四组间的确定性升降级逻辑：
- **升级路径**：低价捡漏 → 精准测试 → 精准主力
- **降级路径**：精准主力 → 精准测试 → 低价捡漏
- **自动广泛组**：仅 BROAD/PHRASE/AUTO，不参与精准升降级

判定依据：ACOS 约束 + 3 日验证数据（连续 3 完整站点日，每日花费>$5，3 日聚合 ACOS < 目标 ACOS）。

### 9.3 精准迁组与 LLM 调整的列级合并（最小实现，2026-08-03）

**职责边界**：精准升降级规则只拥有活动迁组字段 `target_campaign_group_type`；LLM 只拥有预算、Bid、状态、Placement、否词及其原因/证据等调整字段。两者不得在 `campaign.py` 通过同一 `CampaignAdjustmentItem` 相互覆盖。

**合并键**：同一 `decision_id + campaign_id`。最终只有一张建议卡和一组对应 Pending：

- 已有 LLM 建议卡：复用该卡，Exact 仅对 `campaign_pending.target_campaign_group_type` 做迁组字段 upsert；不得改写 LLM 的 `action`、预算/Bid/Placement、`triggered_rule`、`reason`、`evidence`、`review_level`。
- LLM 未返回该活动：仅在 Exact 命中真实迁组（升级/降级/淘汰）时建立一张纯迁组卡；该卡不携带规则引擎的预算、Bid、Placement 调整值。
- `stay_with_adjustment` 无迁组 action：不建卡、不建 Pending、不改写已有 LLM 建议。

**持久化规则**：迁组补丁与 LLM 调整通过 `campaign_pending` 的列级 upsert 合并。`target_campaign_group_type` 有值时写入；Exact 纯迁组写入不得将已有的 `old_state/new_state/old_budget/new_budget` 覆盖为 `NULL`，LLM 调整写入也不得将已有迁组目标清空。建议使用 `COALESCE(VALUES(column), column)` 保留另一来源的非空列值。

**执行语义**：`advert_exec_mapper.py` 仍只读取 Pending；MCP 的组合迁移仅消费 `target_campaign_group_type`，预算/Bid 等仅消费 LLM 已确认写入的对应 Pending 字段。该合并不新增执行入口，也不改变 taskId 轮询状态机。

---

## 10. 护栏系统

> v3.14 独立模块化，v3.15 增强优先级 + R3/R4 重试。

### 10.1 护栏规则 (`campaign_guardrails.py`, 504 行)

`apply_all()` 统一入口，12 条规则（P0-P11），每条含 `retry_instruction` + `campaign_key`：

| 优先级 | 规则 | 说明 |
|--------|------|------|
| P0 | 日预算上限 | 父 ASIN 预算硬顶 |
| P1 | 样本不足保护 | 新活动(≤3天)/测试期(<14天)/复评防抖(≤3天)禁止淘汰 |
| P2 | 库存/退货/评分 | inventory_days/refund_rate/rating 硬护栏 |
| P3 | 硬淘汰 | 无单且 bid≤$0.10 或 budget≤$1 → 强制淘汰（优先级 > P1） |
| P4-P11 | 预算/Bid/广告位/否词 | 各维度边界修正 |

### 10.2 R3/R4 LLM 重试编排

护栏拦截后按 `campaign_key` 带 `retry_instruction` 回灌 LLM 重判，最多 R3→R4 两轮。R4 后不再 R5，最终护栏兜底。`_build_guardrail_alerts` 仅用 `retry_instruction`，不回落 `message`（避免规则编号泄露给 LLM）。

### 10.3 淘汰多环节阈值（有意不同）

`campaign_guardrails.py` 常量：`LOW_BID_MAX = 0.20`、`LOW_BID_MIN = 0.10`、`LOW_BUDGET_MAX = 1.00`。

| 环节 | 位置 | 判据 | 用途 |
|------|------|------|------|
| 预过滤 | `is_strictly_in_low_bid_pool` | bid ≤ LOW_BID_MAX(0.20) **AND** budget ≤ LOW_BUDGET_MAX(1.00) | 判定"已执行淘汰"→灰卡剔除 |
| 归组 | `_is_in_elimination_pool` | bid ≤ LOW_BID_MIN(0.10) **OR** budget ≤ LOW_BUDGET_MAX(1.00) | 预分类/终分类/强制修正 |
| 强制修正 | `_resolve_budget_conflicts` | OR 命中即翻 eliminate + 硬填 $1/$0.20 | LLM 不听话兜底 |

> **勿统一阈值**：预过滤(AND)用 LOW_BID_MAX=0.20，归组/强制修正(OR)用 LOW_BID_MIN=0.10，是有意设计，不是 bug。两种常量的不同组合实现了不同的环节语义。

---

## 11. 淘汰复评（KB21 §7）

> v3.10 实现。ERP 库 `t_advert_agent_pool_entry` 表 + 双向 sync + 执行钩子。

### 11.1 入池同步

- **discovery 入池**：分析后取全量 live 活动 vs 池表 `exit_date IS NULL`，live 在池底 → INSERT `source='discovery'`
- **离池**：池表有但 live 无或已恢复 → UPDATE `exit_date=NOW()`
- **执行钩子**：ELIMINATE 卡 MCP 真跑后 `upsert_pool_entry(source='execution')`；REACTIVATE 卡真跑后 `mark_pool_exit`

### 11.2 复评保护护栏

三种条件 OR，命中任一禁强制淘汰：
1. `days_online ≤ 3` — 新活动样本不足
2. 测试期 + `days_online < 14` — 测试期样本保护
3. `days_since_reactivation ≤ 3` — 复评后防抖

### 11.3 复评卡归类

`reactivate_budget_only` / `reactivate_with_calibrated_bid` → `suggest_category='REACTIVATE'`。`summary_stats` 独立 `to_reactivate` 桶。ERP summary 表含 `reactivate_count` 列。

### 11.4 未尽事项

- 回算口径修复（释放预算入池 + cap 锚定活动之和）
- 余量出口（复评捞回 + 二次新增 + 缓冲）

---

## 12. 分析运行闸门 + 配置镜像

### 12.1 分析运行闸门 (`app/workflow/analysis_run_guard.py`)

> v3.23 新增。解决重复分析触发、session 取消态不一致、前端放弃分析后后端仍运行等问题。

**核心职责**：
- **防重复运行**：同 ASIN 已有进行中分析时拒绝新的分析请求（幂等校验）
- **取消态管理**：session 新增 `cancelled` 列，前端放弃分析时写 `cancelled=1`，后端各阶段检查取消标记
- **运行前校验**：`campaign.py` 入口加闸门调用，analysis_start 前检查是否已有 running session 或已取消

**关键文件**：

| 文件 | 变更 |
|------|------|
| `app/workflow/analysis_run_guard.py`（新） | 运行闸门核心逻辑 |
| `app/persistence/mysql_state_manager.py` | +128：取消/运行状态持久化 |
| `app/persistence/state_manager.py` | +105：闸门接口抽象层 |
| `app/persistence/schema.sql` | +11：`analysis_session` 加 `cancelled` 列 |
| `app/api/decision.py` | +59：`POST /decision/cancel-event` 增强 + new-event 闸门校验 |
| `app/api/campaign.py` | +87：运行前闸门校验 |
| `app/workflow/steps/campaign.py` | +46：编排层闸门接入点 |
| `app/workflow/steps/campaign_new.py` | +22：新增活动线闸门适配 |
| `demo/ad-asisitant-agent.html` | +74：前端放弃确认弹窗闭环 |
| `tests/api/test_campaign_cancellation_fencing.py`（新） | 144 行：取消闸门全链路测试 |

### 12.2 配置保存镜像 (`app/api/config_mirror.py`)

> v3.23 新增。保证前端运营配置（战略/策略/P3/方向）保存后同步写入 state DB 和 ERP 库，消除"保存成功但下次加载丢失"的一致性问题。

**设计**：每次配置保存操作同时写两份——state DB（实时读取路径）+ ERP `t_advert_agent_decision_config`（批次快照路径）。双写任一失败回滚 + 告警。

**关键文件**：

| 文件 | 变更 |
|------|------|
| `app/api/config_mirror.py`（新） | 配置镜像 API |
| `app/data/decision_config_reader.py` | +44：批量读取增强 |
| `tests/api/test_config_mirror.py`（新） | 镜像 API 测试 |
| `tests/persistence/test_agent_config_mirror.py`（新） | 镜像持久化测试 |
| `tests/data/test_agent_config_batch_reader.py`（新） | 批量读测试 |

### 12.3 Codex 批跑 (`batch_via_api_codex.py`)

> v3.23 新增。Codex 复核批量调度入口（308 行），集成 deepseek-v4-pro review hook，替代旧 `batch_via_api.py` 基础调度。

---

## 13. 执行层

### 13.1 广告调整执行 (`advert_execution.py`)

confirm(CONFIRMED) → 调 `whp-advert-agent` MCP（6 工具）→ 落 4 张 `_record` 表。`advert_exec_mapper.py` 为唯一映射点。默认 `advert_mcp_enabled=false` + `advert_exec_dry_run=true` 安全闸。

**v3.22 immediate_exit 确定性执行**：经营模式为 `IMMEDIATE_EXIT` 时，BROAD/PHRASE/AUTO 暂停 + EXACT 迁入低价捡漏组，不走 LLM 分析、直接生成确定性 ActionBundle。

**v3.24 taskId 异步轮询调度器**（`task_poll_scheduler.py`，spec `2026-08-03-pending-taskid-polling-design.md`）：提交、轮询、回写三阶段解耦——

- 每 Web 进程独立：固定数量 asyncio 协程消费者（`advert_task_poll_workers=2`）从有界队列（`advert_task_poll_queue_capacity=20`）取任务；单进程已受理上限 = 2+20 = 22。
- 提交阶段先 `reserve()` 预留名额；容量满 → 拒绝提交，pending 保持 PENDING。
- 消费者对每个 taskId 按 **3m/6m/12m/24m** 最多查询四次结果 MCP；每次只回写已终态行（未终态保持 IN_PROGRESS）。
- 四次耗尽仍无终态 → 写 FAIL，文案 `POLL_EXHAUSTED_MSG` 标注需人工复核（区别于平台明确失败）。
- 不持久化调度状态；进程重启丢失内存队列，已落库的 `pending.task_id` 供人工核对。

### 13.2 灰度卡

ERP 执行过程中对部分失败的活动打灰度标记（gray card），分类展示：
- `move_errors`：挪组失败（portfolioId 匹配失败）
- 部分执行失败：不影响已成功的活动

### 13.3 组合预算执行 (`portfolio_execution.py`)

`/campaign/execute-portfolio-budget` → 实时查 portfolioId → MCP 更新组合预算。

### 13.4 经营模式 × 执行闭环状态（待办，2026-07-30）

当前三个链路的执行闭环完整度不一致。核心问题：正常/清货优先链路把"提交成功"误写为"已生效"。

#### 各链路对比

| 链路 | pending 构参来源 | 自动下发 | taskId 终态轮询 | "提交"与"生效"区分 |
|---|---|---|---|---|
| 立即退出 | 是（确定性 ActionBundle → pending） | 是 | 有，`task_poll_scheduler` 3m/6m/12m/24m 异步轮询（v3.24） | 基本有，依赖 ERP 回写 taskId |
| 清货优先 | 是（LLM 分析 → pending） | 否，前端人工确认 | 无 | 无，提交成功即写 SUCCESS |
| 正常 Campaign | 是（LLM 分析 → pending） | 否，前端人工确认 | 无 | 无，提交成功即写 SUCCESS |

#### pending → 执行 MCP 的统一性

执行 mapper（`advert_exec_mapper.py`）以 pending 为唯一构参来源：

- `campaign_pending`：活动预算、活动状态、组合迁移目标 `target_campaign_group_type`
- `keyword_pending`：关键词状态、Bid
- `placement_pending`：广告位比例

mapper 不感知 `operating_mode`，不根据经营模式重算业务规则。组合迁移同样是 `campaign_pending.target_campaign_group_type → 解析真实 portfolioId → MCP payload`。

两个执行入口存在状态处理差异：
- 立即退出：`submit_execution()` — 提交后 `IN_PROGRESS` → 轮询终态 → `SUCCESS/FAIL`
- 前端人工确认：`submit_execution_direct()` — 提交后直接写 `SUCCESS`

#### 必要语义与当前差距

建议的字段状态机（不区分经营模式，适用全部链路）：

| 阶段 | `confirm_status` | `execute_status` | 含义 |
|---|---|---|---|
| 待人工确认 | PENDING | PENDING | 未授权下发 |
| 已确认待提交 | CONFIRMED | PENDING | 可执行但未提交 |
| 已提交异步任务 | CONFIRMED | IN_PROGRESS | 已拿到 taskId，等待终态 |
| 广告侧终态成功 | CONFIRMED | SUCCESS | MCP 查询确认成功 |
| 广告侧终态失败 | CONFIRMED | FAIL | MCP 查询确认失败 |
| 演练 | CONFIRMED/PENDING | DRY_RUN | 未真实下发 |

当前差距：
1. 清货优先/正常人工执行在 MCP 返回异步任务后，直接将 `execute_status` 写为 `SUCCESS`，`execute_msg` 写为 `"direct execution via MCP"`。实际仅完成提交(submitted)，不等于广告平台执行完成(effective)。
2. 本地代码不写执行记录表 `t_advert_agent_modify_advert_record.task_id`，依赖 ERP/MCP 根据 `decisionId` 回写。若外部未正确回写，后续无法追溯 taskId。
3. `execute_time` 在提交时和终态时都会写，不能单独解释为"生效时间"。若需严格审计，后续应拆为 `submitted_at` 与 `effective_at`，或以 task 子记录时间线为权威来源。

#### 涉及字段汇总

pending 三表共享字段：
- `execute_status`：PENDING / IN_PROGRESS / SUCCESS / FAIL
- `execute_msg`
- `execute_time`

确认字段：
- `confirm_status`
- `confirm_user_id`
- `confirm_time`

决策卡汇总字段（`t_advert_agent_decision_cards`）：
- `execute_status`（汇总值）

执行记录表（`t_advert_agent_modify_advert_record`）：
- `task_id`（当前依赖外部回写）

---

## 14. 核心词管理系统

> v3.18 实现，v3.19 扩展。

### 14.1 数据流

```
MCP 拉关键词+listing → LLM recommend_semantic_core() (KB29)
  → semantic_conflict(4种) + R1 精确相关判定
  → 落库 ERP t_advert_agent_core_keyword
  → campaign 主流程读回填 is_core
```

### 14.2 关键文件

| 文件 | 角色 |
|------|------|
| `app/data/core_keyword_fetcher.py` (414行) | 数据编排器 |
| `app/workflow/steps/core_keyword.py` (309行) | 语义判定工作流 |
| `app/api/core_keyword.py` (62行) | API: `/analyze` `/status` `/enable` `/disable` `/groups` |
| `app/core/core_keyword_policy.py` (25行) | 策略引擎：按 campaign 分组聚合 |

### 14.3 离线批跑

`batch_core_keyword.py` + `batch_core_keyword.sh`：离线 7 天一批，crontab 定时跑。

---

## 15. 搜索词精准扩词双来源（v3.25）

> 2026-08-04 接入。补新增活动线"只靠流量词库探索、未利用搜索词报告已验证词"的缺口：把搜索词报告里有成交 / 词根聚合可验证的词，确定性提升为 EXACT 精准活动，与流量词库探索（流量来源）双来源合流。

### 15.1 数据流

```
三股并行中的新增活动线（双来源合流）:
  ├─ 流量来源（流量词库探索）: NewKeywordFetcher (flow/own/竞品三源) → 硬过滤 → 双轮 LLM 选词取交集
  │     → NewCampaignDecision (source=flow/ranking_opportunity/competitor; flow 一律 BROAD 起步)
  ├─ 搜索词来源（搜索词提精准）: 广泛流 LLM 对 search_term bundle 标注 → exact_promotion_candidates
  │     → build_search_term_promotion_decisions() 确定性准入 (KB23 §3.4 订单/词根两通道)
  │     → EXACT 决策 (source=SRC_CONVERTED / SRC_BROAD_DERIVED)
  └─ merge_new_campaign_decisions(流量来源, 搜索词来源) → 搜索词提精准覆盖同词 flow 探索项
        → finalize_new_campaign_decisions() 统一补齐 bid/预算/命名/投放子ASIN（一次组装）
```

### 15.2 关键模块

| 文件 | 角色 |
|------|------|
| `app/workflow/steps/campaign_search_term_promotion.py` | 确定性准入：`build_search_term_promotion_decisions(candidates, existing_exact_keywords, target_acos)` |
| `app/core/campaign_sample.py` | KB17 活动级样本不足判定 `assess_campaign_sample`；样本不足 → 搜索词标 `SKIPPED_CAMPAIGN_SAMPLE_INSUFFICIENT` 仅观察、不产生提精准候选 |
| `app/workflow/steps/campaign_new.py` | `merge_new_campaign_decisions` 合流 + `finalize_new_campaign_decisions` 统一组装（bid/预算/命名代码确定性产出） |
| `app/data/campaign_fetcher.py` | `build_search_term_bundle`：活动级 7d/14d 搜索词 bundle（按 7d 订单/花费/点击排序，`search_term_llm_max_terms_per_campaign=20` 截断） |
| `app/llm/reasoner.py` | 广泛流搜索词行渲染（`render_search_term_prompt_lines`）+ LLM 回吐 `exact_promotion_candidates` 按原始搜索词报表权威回填校验（防 LLM 改词） |

### 15.3 确定性准入规则（KB23 §3.4，`campaign_search_term_promotion.py`）

- **订单通道**：搜索词 7d 订单 ≥ 3 且 ACOS ≤ 目标 ACOS → `source=SRC_CONVERTED`，EXACT。
- **词根通道**：按 `keyword_root` 聚合同一词根的多条搜索词，聚合订单 ≥ 3 或 ACOS ok → EXACT。
- **CVR 通道**：缺品类基准，刻意不在此处伪造（代码明确不做）。
- **样本不足**：活动级样本不足（`assess_campaign_sample`）的活动，搜索词只观察、不进提精准。
- **去重**：已存在 EXACT 活动的词（`existing_exact_keywords`）跳过；候选按 `(campaign_key, search_term)` 去重。

### 15.4 匹配方式来源优先（KB16 §4 / KB28 SRC_FLOW_EXPLORATION）

- `source=flow`（流量词库，未经搜索词表现验证）候选：`_derive_match_type` 一律返回 `BROAD` 先拿搜索词样本；`long_tail` 词形分类只描述词形/相关性，不单独证明应建 EXACT。
- 搜索词提精准候选：固定 `prescribed_match_type=EXACT`，进入精准测试组。
- 排名机会词 / 竞品词：各自专门规则判定，不被本节覆盖。

---


*v3.7: 选词/投票质量 + 新增扩词治不准（2026-06-26 上线 chenv31，详见主交接 06-26 条）—— ①逐活动 **cid 句柄**根治 campaign_key 漂移（LLM 回吐 `C1..Cn`，代码 `cid_map` 权威回填结构/现状字段，越界/重复 cid 丢弃→进 R3）；②双轮投票**缺轮兜底**（单轮缺失=分歧送 R3=Level A；两轮都漏种占位送 R3、R3 仍缺删占位还原"未分析"不伪造 keep=Level B）；③删 LLM 自报 **confidence**（死字段，投票一致性已定档）；④新增扩词**接入 KB28**（`new_campaign` 预设 +`08`+`28:0,2,3`）：按 §2 综合权衡自然位+周排名+搜索量+标题属性判 R1-R4 `relevance_tier`，候选补 own_keyword_flow 周排名/周搜索量信号，目标词类型软引导；相关性/词类型判断**全交 LLM**，代码只记录不硬判；⑤推自然位删占比判据（recommender+thresholds，另一窗口）。待核：own_keyword_flow 三排名字段语义 live 终核；竞品源仍默认关（direct_competitors 无词字段，启用需配 KB28 §4.1）*
最后更新：2026-07-17

*v3.25: 搜索词精准扩词双来源 + 样本过滤（2026-08-04，本地未部署服务器）—— ①`campaign_search_term_promotion.py` 搜索词提精准确定性准入(新,104行)：KB23 §3.4 订单/词根两通道，CVR 刻意不伪造 ②`campaign_sample.py` 活动级样本不足判定(新,68行)：样本不足活动搜索词标 `SKIPPED_CAMPAIGN_SAMPLE_INSUFFICIENT` 仅观察 ③`campaign_fetcher.py`(+284) `build_search_term_bundle` 活动级 7d/14d 搜索词 bundle ④`reasoner.py`(+207) 广泛流搜索词行渲染+`exact_promotion_candidates` 权威回填校验 ⑤`campaign_new.py`(+244) 双来源合流 `merge_new_campaign_decisions`+统一组装 `finalize_new_campaign_decisions`；`_derive_match_type` 对 flow 一律 BROAD ⑥`models/campaign.py`(+37) `SearchTermPromotionCandidate`/`NewCampaignDecision` ⑦`settings.py`(+4) `search_term_llm_max_terms_per_campaign=20`、`campaign_new_max_creates 20→15` ⑧`campaign.py`(+176)/`campaign_guardrails.py`(+51) 编排接入 ⑨测试 8 个（`test_campaign_search_term_promotion`+367 等）。20 files +1733/-166。*

最后更新：2026-08-05

*v3.24: pending taskId 轮询调度器（2026-08-03，本地未部署服务器）—— ①`task_poll_scheduler.py` 轮询调度器(新,206行)：进程内有界队列+固定协程消费者(2×20)，提交前 reserve 名额，按 3m/6m/12m/24m 最多查 4 次终态，精确回写每行 pending，耗尽按 FAIL 回写 ②`advert_execution.py` 720 行重构：提交/轮询/回写三阶段解耦 ③`repository.py`(+309) 执行仓库适配+`write_pending_terminal` ④`settings.py` 新增 `advert_task_poll_workers`/`advert_task_poll_queue_capacity` ⑤测试重构：`test_task_poll_scheduler`(新)+`test_portfolio_match_and_exec`(-591)+`test_erp_gray_cards`(-120)。13 files +1271/-1160。*

最后更新：2026-08-03

*v3.23: 分析运行闸门 + 配置镜像 + Codex 批跑（2026-07-30~31，本地未部署服务器）—— ①`analysis_run_guard.py` 分析运行闸门(新)：防重复分析+session 取消态+前端放弃确认闭环 ②`config_mirror.py` 配置保存镜像(新)：运营配置双写 state DB+ERP ③`batch_via_api_codex.py` Codex 批跑入口(新,308行) ④`decision_config_reader.py` 批量读增强(+44) ⑤`mysql_state_manager`/`state_manager`/`schema.sql` session 取消态全链路 ⑥前端放弃确认弹窗闭环 ⑦测试 4 个新文件。17+6+13+4 files +1701/-173。*

最后更新：2026-07-31

*v3.22: 精准组合升降级 + ACOS约束 + KB v2.0 重构收尾（2026-07-28~30，本地未部署服务器）—— ①`acos_constraints.py` ACOS约束核心模块(新) ②`campaign_exact_transition.py` 精准组合四组升降级逻辑(新) ③KB v2.0 重构：新增 00-总纲+30-动作词表+runtime_contract.yaml+evidence.yaml；精简过时 KB30/31/32 旧版(否词/样本窗口/自动广告)；全部 KB 切片+7 个 ontology YAML 大范围更新 ④ERP 执行状态管理：immediate_exit 确定性执行+灰度卡+组合匹配执行 ⑤`access-guard.js` 前端入口守卫(新) ⑥测试大面积扩展：test_portfolio_match_and_exec(+1328)+test_product_identity_required(+908)+test_erp_gray_cards(+359)+test_mcp_campaign_discover(+380) ⑦kb_loader 预设精简。27+17+31+5 files +8781/-1349。*

最后更新：2026-07-30

*v3.21: 战略层经营模式 + 新增活动选词重构 + MCP registry（2026-07-25，本地未部署服务器）—— ①`OperatingMode` 6值+`AdPermission` 3级+`operating_mode_to_permission()` ②ERP/state DB 全链路持久化 ③`new_keyword_fetcher.py` 统一多源候选词发现+`campaign_new.py` -130 行 ④`mcp_registry.py` MCP 工具集中注册 ⑤`.gitignore` 加 logs/。28+18+7 files。*

最后更新：2026-07-25

*v3.20: KB30/31/32 新规则 + 组合 spend 窗口 + 预过滤收口 + KB 切片细化（2026-07-17~22，本地未部署服务器）—— ①KB30(否词治理)/KB31(样本窗口)/KB32(自动广告)新规则+广泛否词 MCP 真实下发链路（注：KB30/31/32 后在 v3.22 重构中清理替换为新版）②组合预算 spend 多周期窗口(1d/3d/7d/14d/30d)+前端展示 ③预过滤收口 campaign_prefilter+删死代码 ④KB 细粒度切片+core_keyword_policy 迁移 app/core/ ⑤Codex hook 预埋+decisionId 追踪 ⑥批跑脚本迁移 scripts/+清理过期文档。24+10+8+3+28+7+9 files。*

最后更新：2026-07-22

*v3.19: 核心词管理面板(前端+后端) + campaign_key 关键词级重构（2026-07-16/17，本地未部署服务器）—— ①campaign_key 从活动级改为关键词级(加#match_type#keyword_id)消除 BROAD/PHRASE 碰撞+否定词不入 LLM ②**核心词后端**：`core_keyword_policy.py` 策略引擎+campaign 分组聚合；API 扩展(手动标注/分组查询/任务刷新)；repository +281行(分组查询/批量更新) ③**核心词前端**：Web 管理弹窗(state.js+60/render.js+53/panel.css+49/events.js+12)——语义判定结果展示、按 campaign 分组折叠面板、手动开关控件、空态/loading/error 三态 ④Codex 批跑脚本+SkillOpt 方案+复盘记忆加工。12+10+7 files +1128/-68。*

*v3.18: 核心词管理系统 — is_core 真实数据源闭环（2026-07-12，本地未部署服务器）—— ①核心词发现数据编排器+LLM 语义判定(semantic_conflict 4种+R1 精确相关)→落库 ②campaign 主流程 is_core 回填（填了从 v2.0 起一直硬编码 False 的坑）③KB29 核心词定义规则接入 ④azlisting MCP 独立连接 ⑤离线批跑脚本+DDL。18+4 files +2397/-54。*

*v3.17: Product Identity 全线注入 + 挪组执行修复（2026-07-12，本地未部署服务器）—— ①`product_identity.py` 解析 (asin,shop_id,parent_seller_sku) 三元组 ②`ProductIdentityMixin` 注入所有层请求模型 ③缓存无 identity 自动丢弃 ④挪组 portfolioId 解析前移+按 pid 拆请求+失败分类告警 ⑤`_snapshot_action` 补 ADJUST 映射+淘汰判定加固 ⑥知识图谱 16 篇+5 新测试。51+3+1 files +4691/-256。*

最后更新：2026-07-12

*v3.16: 秒开优化 + meta_filter 分级缓存（2026-07-11，本地未部署服务器）—— ①策略层轻量化：`run_get_tactics_options` 零 MCP/LLM 纯缓存读取，删除 diagnosis 自动回访逻辑 ②缓存 key 加入 meta_filter 维度 ③BOOTSTRAP_TOOLS 分级 + bootstrap_tools_for_meta() 按需跳过 campaign keyword 拉取 ④`_build_guardrail_alerts` 仅用 retry_instruction 不回落 message。18+2 files +491/-68。*

*v3.15: 护栏优先级重构 + R3/R4 LLM 重试编排（2026-07-10，本地未部署服务器）—— ①P3 硬淘汰（无单且触底）优先级升至 P1 样本不足之上，P0/P2 仍高于 P3 ②R3/R4 重试编排：护栏拦截后带 retry_instruction 回灌 LLM 重判，最多两轮，最终护栏兜底 ③全部 12 条护栏规则新增 retry_instruction/campaign_key 字段 ④KB 阈值全线对齐 + AUTO_BATCHABLE→AUTO_APPROVED ⑤campaign_viewmodel 审核等级转中文标签。6+17+1 files +1298/-184。*

*v3.14: 护栏独立模块 + 组合映射归一化 + prompt 瘦身（2026-07-10，本地未部署服务器）—— ①`campaign_guardrails.py` 新模块：`apply_all()` 统一护栏入口，从 `_resolve_budget_conflicts` 提取淘汰保护/强制淘汰/淘汰硬填充/日预算上限/库存退货评分护栏，阈值常量+谓词为 `campaign_portfolio` 侧唯一来源 ②`campaign_portfolio.py` 新增 `GROUP_CODE_TO_LABEL`/`GROUP_LABEL_TO_CODE` 双向映射作为归一化来源；`campaign_viewmodel.py`/`text_utils.py` 删除本地硬编码映射改 import，消除三处漂移 ③`campaign.py`：`_resolve_budget_conflicts` 委托 guardrails，新增 inventory_days/refund_rate/rating 参数 ④`reasoner.py`：精准/广泛 prompt 示例输出删冗余字段（cid 句柄已回填）；`recommend_new_campaigns` JSONDecodeError 重试 2 次+max_tokens 8192 ⑤阈值修正：Bid≤$0.21→$0.20、Budget≤$1.01→$1.00、退货率阻断 25%→30%。7 files +546/-227。*

*v3.13: ad_portfolio_list MCP 接入 + 前端 UX 全面升级（2026-07-09/10，本地未部署服务器）*

*v3.12: 扩词相关性锚点 product_name 接线（2026-07-07，已部署 chenv31）—— 新增扩词流 LLM 判词相关性依赖 `asin_data.title` 作锚点，但 v3.2 切除 Doris 后该字段恒为 None（listing_basic_info_v2 normalizer 不提取 title，parent_listing_detail 的 product_name 只到 McpDbContext 就断了）。修复：`mcp_adapter._resolve_context` 把 `McpDbContext.product_name` 缓存到实例变量，`fetch_asin_data` 注入 `data.title`。零额外 MCP 调用（parent_listing_detail 本就在 context 阶段已调过）。1 文件 +5 行。*
*v3.11: 复评生产库确证 + pending execute_time 回写 §27（2026-07-06，本地未部署）—— ①生产库确认旧 logic 曾生效(B0B7S3PWWB 有 77 条 CONFIRMED ELIMINATE)；②执行钩子补 `update_pending_execute_status('SUCCESS')` 回写 execute_time,discovery 路径可读到真实入池时间不再寄望 NOW()；③sync_pool_entries discovery 入池日改用 `COALESCE(execute_time,confirm_time)` > NOW() 两级回落；④复评 badge/card 边框改克莱因蓝 #2563EB；⑤护栏测试补全(44 passed) + 文档同步*
*v3.10: 淘汰复评全链路 + 护栏 + 统计 §27（2026-07-06，本地未部署服务器）—— ①建表 `t_advert_agent_pool_entry`+双向 sync+ON DUPLICATE KEY 防复淘汰 + `parent_sku/shop_id/keyword_text` 全透传；②执行钩子接入 `submit_execution_direct/submit_execution` 真跑后(ELIMINATE→upsert source=execution / REACTIVATE_*→mark_pool_exit，async_batch MCP 整批成功才写)；③淘汰护栏三条件(`days_online≤3`/测试期`<14`/`days_since_reactivation≤3` 防淘汰↔复评抖动)，`product_stage` 从 strategy_context 透传；④`days_since_reactivation` 新模型字段+`get_recently_reactivated` 查池表 exit_date 反算；⑤reactivate_* 映射为 REACTIVATE 类别 +`to_reactivate` 独立桶 +ERP summary 加列 reactivate_count + 前端「新增/复评」合并展示位；⑥删死代码 `get_elimination_entry_dates`；⑦`load_pending_by_card_ids` SELECT 补 perf_json/trigger_rule。待部署服务器。*
*v3.9: 原淘汰复评初版(2026-07-05，已废弃) — 建表 state 库 + pool_entry_repository 独立文件 + sync 双向同步；v3.10 迁回 ERP 库并补全钩子/护栏/统计*
*v3.8: 待办全量核实+文档更新（2026-07-04，已上线 chenv31）—— ①逐条代码核实 §4.2/§15.5/§17/§19/§21.3/§22.4，5条标记已完成(DONE)修正为已核实真实状态；②basic_info MCP 批量失败→丢失灰卡（`campaign_fetcher.py:215-237`，不进 LLM 防误淘汰）；③`has_config` 改为 ERP 批次判定（`decision.py:75`，修 state DB strategy_config 缺行→空壳 A 态）；④§19 定时分析不采信 config acos/预算→已解决(state DB override 路径替代，config 表两列是死列但不影响功能)；⑤§17 ②Tab4 方向卡→已解决(ERP direction_recommend_detail 表路径)；⑥§15.5 新增活动预算回算→已实现；⑦clear_analysis_session 成功路径已清，失败路径仍未清（有12h TTL兜底）*
*最后更新：2026-07-10
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
> **2026-06-30 (v3.2)**：**切除全部 Doris 数仓依赖，纯 MCP 数据源**。删除 7 个 Doris 模块、手术 mcp_db_context.py（只留 MCP parent_listing_detail）、campaign_fetcher 移除 prefer_db/回落、全链清理透传参数、BOOTSTRAP_TOOLS/MCP_TOOL_TO_META 内联、服务器热修复合并（MCP 优先解析/API 超时上调/perf_json+trigger_rule 补列）。125 个 .py 全编译通过。详见主交接文档 2026-06-30 条目。
