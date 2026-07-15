# 数据库表结构说明：ERP库与State库

## 目的

本篇说明项目中的两个数据库定位、表责任和读写边界。这里的“ERP库”和“State库”是项目架构概念，不代表同一个物理实例。

## 当前代码真源

- State schema：`ad-direction-agent/app/persistence/schema.sql`
- State manager：`ad-direction-agent/app/persistence/mysql_state_manager.py`
- State factory：`ad-direction-agent/app/persistence/state_factory.py`
- ERP repository：`ad-direction-agent/app/persistence/erp_writer/repository.py`
- ERP mapper：`ad-direction-agent/app/persistence/erp_writer/mappers.py`
- 执行 mapper：`ad-direction-agent/app/persistence/erp_writer/advert_exec_mapper.py`
- 核心词 DDL：`ad-direction-agent/scripts/erp_db/migrate_core_keyword.sql`
- 配置：`ad-direction-agent/app/config/settings.py`

## 代码锚点地图

| 职责 | 代码位置 | 读写对象 |
| --- | --- | --- |
| State schema | `app/persistence/schema.sql:5` 起 | `strategy_config`、`tactics_config`、override、workflow、feedback 等 |
| State manager | `app/persistence/mysql_state_manager.py:21` `MySQLStateManager` | State 库所有表 |
| 长期配置读取 | `mysql_state_manager.py:108` `get_long_term_config()` | `strategy_config` + `tactics_config` + override |
| 长期配置写入 | `mysql_state_manager.py:173` `set_long_term_config()` | strategy/tactics/budget override upsert |
| workflow state 读写 | `mysql_state_manager.py:274` / `:283` | `workflow_meta`、`keyword_analysis`、`target_scores` |
| P3 cache/override | `mysql_state_manager.py:421`、`:458`、`:473`、`:494` | `p3_recommendation`、`acos_override` |
| 分析会话 | `mysql_state_manager.py:529`、`:554` | `analysis_session` |
| ERP repository | `erp_writer/repository.py:85` `ErpDualWriterRepository` | ERP 决策、卡片、pending、reason group |
| ERP 快照读 | `repository.py:154` `read_snapshot()` | decision + summary + cards + pending + groups |
| 确认/拒绝 | `repository.py:228` `confirm_decisions()` | card 和三类 pending 的 confirm_status |
| 待执行读取 | `repository.py:297` `load_confirmed_pending()` | CONFIRMED 且 `execute_status=PENDING` 的 pending |
| Campaign 落库映射 | `erp_writer/mappers.py:324` `canonicalize_payload()` | `CampaignAnalysisResult` 到 CanonicalRun |
| 执行映射 | `erp_writer/advert_exec_mapper.py:82` `build_exec_plan()` | pending 到 Advert MCP payload |
| 核心词批次写入 | `repository.py` `write_core_keyword_task()` | `t_advert_agent_core_keyword_task` + `label` 表 |
| 核心词标签读取 | `repository.py` `fetch_core_keyword_set()` | 按三元组读最近 DONE 批次的 `is_core=1` 集合 |

## 数据库定位

ERP 库：

- 面向业务系统展示和执行。
- 保存推荐批次、前端展示卡片、待确认项、待执行项、方向推荐、AI 建议等。
- 是前端 `snapshot/viewmodel` 的主要读源。
- 广告执行链路从 ERP pending 表读取已确认动作。

State 库：

- 面向 Agent 自有状态。
- 保存长期配置、工作流进度、用户覆盖值、缓存型推荐、分析会话、反馈。
- 默认数据库名为 `ad_agent_state`。
- 不承载 ERP 展示卡片和广告执行单据。

## State 库表

来自 `schema.sql`：

| 表 | 主键/粒度 | 责任 |
| --- | --- | --- |
| `strategy_config` | `asin` | 产品等级、阶段、季节阶段等战略层长期配置 |
| `tactics_config` | `asin` | 广告目的、关键词策略等策略层长期配置 |
| `acos_override` | `asin` | 临时目标 ACOS 覆盖值，带过期时间 |
| `budget_override` | `asin` | 临时预算覆盖值，带过期时间 |
| `adjustment_history` | `asin + record_date` | 历史目标 ACOS 和预算调整记录 |
| `p3_recommendation` | `asin` | P3 推荐缓存，带过期时间 |
| `keyword_analysis` | `asin + days` | 关键词分析结果 |
| `target_scores` | `asin + days` | 目标评分结果 |
| `workflow_meta` | `asin` | 当前层级、已完成层级、执行选择 |
| `feedback` | `id` | 用户反馈 JSON |
| `analysis_session` | `asin` | 当前分析会话 run_id 和开始时间 |

## ERP 库表

项目代码直接读写的主要 ERP 表：

| 表 | 责任 |
| --- | --- |
| `t_advert_agent_decision` | 一次分析批次主表，保存父 ASIN、窗口、是否最新、快照等 |
| `t_advert_agent_modify_suggest_summary` | Campaign 调整建议汇总 |
| `t_advert_agent_modify_suggest_card` | Campaign 前端卡片和建议主项 |
| `t_advert_agent_modify_campaign_pending` | 活动预算、状态、分组等待确认/待执行项 |
| `t_advert_agent_modify_keyword_pending` | 关键词 bid、状态、否定词等待确认/待执行项 |
| `t_advert_agent_modify_placement_pending` | placement 溢价待确认/待执行项 |
| `t_advert_agent_modify_suggest_reason_group` | 调整原因分组 |
| `t_advert_agent_modify_suggest_reason_group_member` | 原因分组成员 |
| `t_advert_agent_modify_suggest_special` | 特殊展示块 |
| `t_advert_agent_direction_recommend` | 前置方向推荐主表 |
| `t_advert_agent_direction_recommend_detail` | 前置方向推荐明细 |
| `t_advert_agent_data_metrics` | 推荐使用的数据指标快照 |
| `t_advert_agent_decision_config` | 决策配置和定时批量 ASIN 来源 |
| `t_advert_agent_purpose_score` | 广告目的评分 |
| `t_advert_agent_core_keyword_tracking` | 核心关键词跟踪 |
| `t_advert_agent_ai_suggest` | AI 建议文本、风险提示 |
| `t_advert_agent_pool_entry` | 低价池/淘汰复评池条目 |
| `t_advert_agent_core_keyword_task` | 核心词判定批次主表，三元组 `(parent_asin, parent_seller_sku, shop_id)` 为产品标识，`started_at` 取最新 DONE 批次 |
| `t_advert_agent_core_keyword_label` | 核心词判定明细，每条一行关键词，含 `semantic_conflict`/`semantic_core`/`data_core`/`is_core`/`evidence` 等字段。`is_core=1` 的标签被主 Campaign 流程读取后注入护栏 P0/P3/P5 |

执行记录类表由 ERP 系统自身维护，当前 Python 执行链路多处明确跳过直接写入操作记录表。

### 核心词表设计说明

`t_advert_agent_core_keyword_task` 是批次主表（`task_id` 主键），`t_advert_agent_core_keyword_label` 是明细表（`(task_id, keyword_text)` 唯一键）。两表通过 `task_id` 外键关联。

任务独立于 campaign 分析的 `decision_id`，使用自己的 `task_id`（`ckt_{date}_{hash}`）命名空间。label 表冗余 `parent_seller_sku`/`shop_id`，支持主流程不 JOIN task 表直接按三元组 + `is_core=1` 查询最近 DONE 批次的核心词集合。

旧表 `t_advert_agent_core_keyword_tracking` 仅用于 Tab1 核心关键词监控的历史展示，不与新标签源合并。两套表各自独立。

## 关键写入模式

ERP 写入通常采用“批次快照 + 展示卡片 + pending 子表”的结构：

1. 写或更新 `t_advert_agent_decision`。
2. 写方向推荐或 Campaign summary。
3. 写 card。
4. 写 campaign/keyword/placement pending。
5. 写 reason group 和 special display。
6. 前端确认后更新 confirm_status。
7. 执行后更新 execute_status 和 execute_msg。

State 写入通常是按 ASIN upsert 或按 ASIN+days upsert。

## 状态字段流

| 业务状态 | 真源表 | 写入点 | 读取点 | 注意 |
| --- | --- | --- | --- | --- |
| 战略层选择 | `strategy_config` | `strategy.py:129` 经 `set_long_term_config()` | Tactics/Diagnosis/Execution/P3/Campaign context | 用户选择是真源，ASINData 不覆盖 |
| 策略层选择 | `tactics_config` | `tactics.py:540` | CampaignStrategyContext、前置后续层 | options 只读，不重跑推荐 |
| 当前层级 | `workflow_meta.current_layer` | `advance_layer()` | wizard/state API | 不代表业务动作已经执行 |
| purpose-agent 推荐缓存 | `workflow_meta` + `keyword_analysis` / `target_scores` | `tactics.py` 推荐路径 | tactics options/recommend | days 是缓存维度 |
| target ACOS override | `acos_override` | P3 override API | Execution/P3/Campaign context | 带过期时间 |
| daily budget override | `budget_override` | P3 budget override | P3/Campaign context | 长期配置读取时合并 |
| Campaign 最新批次 | `t_advert_agent_decision.is_latest` | `repository.py:557` `finalize_batch()` | `/campaign/snapshot`、viewmodel、confirm 门禁 | 同 ASIN 只应一个最新批次 |
| 用户确认 | card + 三类 pending `confirm_status` | `/campaign/confirm` | `load_confirmed_pending()` | UPDATE 限 `PENDING`，重复确认会 skipped |
| 执行状态 | 三类 pending `execute_status` | `advert_execution.py` / repository 更新 | viewmodel、执行幂等 | `DRY_RUN` 不等于真实执行 |
| 核心词标签 | `t_advert_agent_core_keyword_label.is_core` + `t_advert_agent_core_keyword_task.status='DONE'` | 离线 `POST /core-keyword/analyze` | Campaign `_analyze_campaigns_impl()` → `item.is_core` → 护栏 P0/P3/P5 + ERP card `is_core` 列 | 任务状态过滤 RUNNING，防读到半截数据；fail-soft |

## ERP card/pending 关系

Campaign 分析写 ERP 时，一张前端 card 可以对应多类 pending：

| card 类型 | pending 子表 | 典型动作 |
| --- | --- | --- |
| MODIFY existing campaign | `campaign_pending` | budget、campaign state、portfolio/group |
| keyword bid/state | `keyword_pending` | bid 调整、关键词暂停/启用 |
| negative keyword | `keyword_pending` | 新建否定词，执行时走 negative MCP |
| placement | `placement_pending` | TOS/ROS/Product Page 溢价 |
| CREATE new campaign | `campaign_pending` + `keyword_pending` + 可选 `placement_pending` | 新建活动和关键词 |
| 灰卡/预过滤 | 通常无 pending | 展示，不可执行 |

`campaign_viewmodel.py` 用 card id 索引三类 pending。前端提交确认时的 `campaign_key` 已收敛为 card id，不应再按活动名猜 pending。

## 常见误区

- ERP 库不是 Agent 的临时状态库。不要把工作流临时元数据随手加到 ERP 展示表。
- State 库不是业务展示库。前端展示 Campaign 卡片时应优先看 ERP 快照/viewmodel。
- `t_advert_agent_pool_entry` 在代码注释中有 ERP/State 表述交错，维护时要以当前 repository 实际连接和部署配置为准。
- 定时批量脚本读取 `t_advert_agent_decision_config`，不是从 State 库取 ASIN 列表。
- `t_advert_agent_core_keyword_tracking`（旧）和 `t_advert_agent_core_keyword_label`（新）不是同一套数据：前者是 per-decision 快照供 Tab1 历史展示，后者是 per-task 持久标签供护栏消费。不要试图"统一两套表"。

## 更新检查清单

- 新增 ERP 表读写时，补充本篇 ERP 表清单和代码锚点地图。
- 新增 State 表时，同步更新 `schema.sql`、manager、迁移说明和本篇表格。
- 修改核心词表结构时，同步更新 DDL 迁移文件、本篇核心词表设计说明段和 `07` 核心词离线判定段。
- 修改 pending 状态枚举时，同步检查前端、确认接口、执行链路和测试。
- 修改数据库连接配置时，同步检查 `05-网关连接池信号量与超时设置.md`。
