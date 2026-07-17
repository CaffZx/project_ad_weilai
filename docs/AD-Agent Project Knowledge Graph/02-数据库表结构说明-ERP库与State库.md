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

> 现状快照：2026-07-16 通过生产服务器连接到 ERP 数据库 `erp_agentadvert` 执行 `SHOW CREATE TABLE` 获取。以下包含该库当前的全部 30 张表（包括仍存在的备份表），不是迁移脚本推测结果；本次只记录现状，不涉及新增表计划。

### 表索引（功能、维护方、项目代码锚点）

| 表 | 功能 | 写入/维护方 | 本项目代码锚点 |
| --- | --- | --- | --- |
| `t_advert_agent_ai_suggest` | AI综合建议（ACOS、预算、风险与节奏文本） | 我方 AD-Agent | `ad-direction-agent/app/persistence/erp_writer/repository.py:1440 _upsert_ai_suggest()`（由 `write_full()` 调用） |
| `t_advert_agent_core_keyword_label` | 核心词判定明细，每个关键词一行，保存语义/数据证据与最终 `is_core` | 我方 AD-Agent 离线核心词任务 | `repository.py:1932 write_core_keyword_task()` |
| `t_advert_agent_core_keyword_task` | 核心词判定任务批次及状态统计 | 我方 AD-Agent 离线核心词任务 | `repository.py:1932 write_core_keyword_task()` |
| `t_advert_agent_core_keyword_tracking` | 旧版按 decision 的核心词监控展示快照 | 我方 AD-Agent（旧链路） | `repository.py:1393 _upsert_core_keywords()` |
| `t_advert_agent_core_keyword_state` | 核心词人工状态管理（锁定/否决/本周启用/禁用），跨批次持久化，LOCKED/VETOED 不在下次分析中覆盖 | 我方 AD-Agent + 人工运营 | `repository.py:2121 _sync_core_keyword_policies_after_task()`；`repository.py:2067 upsert_core_keyword_policy()` |
| `t_advert_agent_create_advert_record` | 创建广告活动的调用/结果记录 | ERP 执行系统 | 本项目 `repository.py:397 insert_advert_record()` 明确跳过写入；无项目 INSERT |
| `t_advert_agent_create_keyword_record` | 创建关键词/否定词的调用/结果记录 | ERP 执行系统 | 本项目不写；创建流程只调用 MCP，未发现 INSERT |
| `t_advert_agent_data_metrics` | 每个 decision 的 SUMMARY/DAILY 指标快照 | 我方 AD-Agent | `repository.py:1188 _upsert_legacy_metrics()` |
| `t_advert_agent_decision` | 一次广告分析决策批次主表，含产品/阶段/季节/目标等快照 | 我方 AD-Agent | `repository.py:1234 _upsert_decision()` |
| `t_advert_agent_decision_config` | 运营配置、定时分析开关及 ASIN 来源；最近决策回写字段也在此表 | 混合：ERP/运营维护配置，我方回写运行状态 | 读取：`app/data/decision_config_reader.py`；写入：`repository.py:1297 _upsert_decision_config()` |
| `t_advert_agent_decision_config_bak_drop_codex` | `decision_config` 的历史备份表（当前生产库仍存在） | ERP/运维备份 | 本项目无读写锚点 |
| `t_advert_agent_direction_recommend` | 前置方向推荐主表（结论 JSON） | 我方 AD-Agent | `repository.py:1112 _upsert_legacy_recommend()`；兼容路径 `:1520 _upsert_wizard_direction()` |
| `t_advert_agent_direction_recommend_detail` | 前置方向推荐明细 | 我方 AD-Agent | `repository.py:1150 _upsert_legacy_details()`；兼容路径 `:1520 _upsert_wizard_direction()` |
| `t_advert_agent_keyword_suggest_bid_record` | 关键词建议竞价查询的请求/响应记录 | ERP 执行系统 | 本项目通过 MCP 查询（`app/data/campaign_fetcher.py`），未发现写入该表 |
| `t_advert_agent_modify_advert_record` | 广告修改执行主记录 | ERP 执行系统 | 本项目 `repository.py:397 insert_advert_record()` / `:406 update_advert_record_result()` 明确不写 |
| `t_advert_agent_modify_campaign_pending` | 活动预算/状态等待确认与待执行动作 | 我方 AD-Agent 生成，ERP/前端确认并执行 | `repository.py:802 _upsert_modern_cards_and_pending()`（INSERT `:867`） |
| `t_advert_agent_modify_campaign_record` | 活动修改的实际执行明细 | ERP 执行系统 | 本项目 `repository.py:413 insert_exec_sub_records()` 明确不写 |
| `t_advert_agent_modify_keyword_pending` | 关键词 bid/状态/否定词等待确认动作 | 我方 AD-Agent 生成，ERP/前端确认并执行 | `repository.py:802 _upsert_modern_cards_and_pending()`（INSERT `:845`） |
| `t_advert_agent_modify_keyword_record` | 关键词修改实际执行明细 | ERP 执行系统 | 本项目 `repository.py:413 insert_exec_sub_records()` 明确不写 |
| `t_advert_agent_modify_placement_pending` | 广告位加价等待确认动作 | 我方 AD-Agent 生成，ERP/前端确认并执行 | `repository.py:802 _upsert_modern_cards_and_pending()`（INSERT `:885`） |
| `t_advert_agent_modify_placement_record` | 广告位加价实际执行明细 | ERP 执行系统 | 本项目 `repository.py:413 insert_exec_sub_records()` 明确不写 |
| `t_advert_agent_modify_portfolio_record` | 广告组合修改实际执行明细 | ERP 执行系统 | 本项目 `repository.py:418 insert_portfolio_records()` 明确不写 |
| `t_advert_agent_modify_suggest_card` | 每个 Campaign 的建议卡（动作、原因、证据、确认/执行状态） | 我方 AD-Agent 生成，ERP/前端维护确认与执行状态 | `repository.py:802 _upsert_modern_cards_and_pending()`（INSERT `:809`） |
| `t_advert_agent_modify_suggest_reason_group` | 建议原因分组主表 | 我方 AD-Agent | `repository.py:1024 _upsert_synthesis()`（INSERT `:1067`） |
| `t_advert_agent_modify_suggest_reason_group_member` | 原因分组与建议卡的关联明细 | 我方 AD-Agent | `repository.py:1024 _upsert_synthesis()`（INSERT `:1085`） |
| `t_advert_agent_modify_suggest_special` | 特殊调整说明展示块 | 我方 AD-Agent | `repository.py:1024 _upsert_synthesis()`（INSERT `:1098`） |
| `t_advert_agent_modify_suggest_summary` | 一次 decision 的调整建议汇总统计 | 我方 AD-Agent | `repository.py:685 _upsert_modern_summary()`（INSERT `:713`） |
| `t_advert_agent_modify_target_record` | 商品投放（target）修改实际执行明细 | ERP 执行系统 | 本项目未写入；执行记录由 ERP 系统维护 |
| `t_advert_agent_pool_entry` | 淘汰/复评池条目及入池、出池、花费快照 | 我方 AD-Agent 执行钩子 | `repository.py:1816 upsert_pool_entry()`；`workflow/steps/advert_execution.py` |
| `t_advert_agent_purpose_score` | 每个广告目的的评分、依据、建议与后续关注 | 我方 AD-Agent | `repository.py:1353 _upsert_purpose_scores()` |
| `t_advert_agent_switch_keyword_record` | 关键词匹配类型切换实际执行明细 | ERP 执行系统 | 本项目未写入；执行记录由 ERP 系统维护 |

维护方说明：**我方 AD-Agent** 表示本项目直接写入；**ERP 执行系统**表示本项目明确不写，由 ERP/执行服务落库；**混合**表示运营/ERP 维护配置、本项目只回写运行字段。DDL 中的索引、唯一键、外键和字符集均按生产库原样保留。

### 完整 DDL

#### `t_advert_agent_ai_suggest`

- 功能：AI综合建议（ACOS、预算、风险与节奏文本）
- 写入/维护方：我方 AD-Agent
- 代码锚点：`ad-direction-agent/app/persistence/erp_writer/repository.py:1440 _upsert_ai_suggest()`（由 `write_full()` 调用）

```sql
CREATE TABLE `t_advert_agent_ai_suggest` (
  `id` char(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT '主键ID(UUID)',
  `decision_id` char(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '决策ID',
  `suggest_acos` int DEFAULT NULL COMMENT '推荐ACOS',
  `suggest_acos_tag` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '推荐ACOS标签',
  `suggest_acos_decision_basis` text CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci COMMENT '推荐ACOS决策依据',
  `suggest_acos_suggest` text CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci COMMENT '推荐ACOS建议',
  `suggest_acos_future_attention` text CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci COMMENT '推荐ACOS后续关注',
  `suggest_budget` decimal(12,2) DEFAULT NULL COMMENT '推荐预算',
  `suggest_budget_tag` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '推荐预算标签',
  `suggest_budget_decision_basis` text CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci COMMENT '推荐预算决策依据',
  `suggest_budget_suggest` text CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci COMMENT '推荐预算建议',
  `suggest_budget_future_attention` text CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci COMMENT '推荐预算后续关注',
  `suggest_keyword_adjust_list_json` mediumtext CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci COMMENT '建议关键词BID调整JSON',
  `comprehensive_judgment` text CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci COMMENT '综合判断',
  `execution_pace` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '执行节奏',
  `risk_warning` text CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci COMMENT '风险提示',
  `tip_msg` text CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci COMMENT '提示信息',
  `create_by` int DEFAULT NULL COMMENT '创建人ID',
  `editor_by` int DEFAULT NULL COMMENT '修改人ID',
  `creator_id` bigint DEFAULT NULL COMMENT '创建人',
  `editor_id` bigint DEFAULT NULL COMMENT '修改人',
  `create_time` datetime DEFAULT NULL COMMENT '创建时间',
  `update_time` datetime DEFAULT NULL COMMENT '更新时间',
  PRIMARY KEY (`id`) USING BTREE,
  KEY `idx_decision_id` (`decision_id`) USING BTREE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci ROW_FORMAT=DYNAMIC COMMENT='agent决策AI推荐表'
```

#### `t_advert_agent_core_keyword_label`

- 功能：核心词判定明细，每个关键词一行，保存语义/数据证据与最终 `is_core`
- 写入/维护方：我方 AD-Agent 离线核心词任务
- 代码锚点：`repository.py:1932 write_core_keyword_task()`

```sql
CREATE TABLE `t_advert_agent_core_keyword_label` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `task_id` varchar(32) NOT NULL,
  `parent_asin` varchar(20) NOT NULL,
  `parent_seller_sku` varchar(128) NOT NULL,
  `shop_id` bigint NOT NULL,
  `keyword_text` varchar(512) NOT NULL,
  `semantic_conflict` varchar(10) NOT NULL DEFAULT 'pass',
  `conflict_reason` text,
  `semantic_core` tinyint(1) NOT NULL DEFAULT '0',
  `semantic_evidence` json DEFAULT NULL,
  `data_core` tinyint(1) NOT NULL DEFAULT '0',
  `data_evidence` json DEFAULT NULL,
  `is_core` tinyint(1) NOT NULL DEFAULT '0',
  `source_refs` json DEFAULT NULL,
  `created_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_task_keyword` (`task_id`,`keyword_text`),
  KEY `idx_product_keyword` (`parent_asin`,`parent_seller_sku`,`shop_id`,`keyword_text`),
  CONSTRAINT `fk_core_keyword_task` FOREIGN KEY (`task_id`) REFERENCES `t_advert_agent_core_keyword_task` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
```

#### `t_advert_agent_core_keyword_task`

- 功能：核心词判定任务批次及状态统计
- 写入/维护方：我方 AD-Agent 离线核心词任务
- 代码锚点：`repository.py:1932 write_core_keyword_task()`

```sql
CREATE TABLE `t_advert_agent_core_keyword_task` (
  `id` varchar(32) NOT NULL,
  `parent_asin` varchar(20) NOT NULL,
  `parent_seller_sku` varchar(128) NOT NULL,
  `shop_id` bigint NOT NULL,
  `site_code` varchar(10) DEFAULT NULL,
  `status` varchar(16) NOT NULL DEFAULT 'RUNNING',
  `total_keyword_count` int NOT NULL DEFAULT '0',
  `core_keyword_count` int NOT NULL DEFAULT '0',
  `error_message` text,
  `triggered_by` varchar(64) DEFAULT NULL,
  `started_at` datetime(6) NOT NULL,
  `finished_at` datetime(6) DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_product` (`parent_asin`,`parent_seller_sku`,`shop_id`),
  KEY `idx_status` (`status`),
  KEY `idx_latest` (`parent_asin`,`parent_seller_sku`,`shop_id`,`started_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
```

#### `t_advert_agent_core_keyword_tracking`

- 功能：旧版按 decision 的核心词监控展示快照
- 写入/维护方：我方 AD-Agent（旧链路）
- 代码锚点：`repository.py:1393 _upsert_core_keywords()`

```sql
CREATE TABLE `t_advert_agent_core_keyword_tracking` (
  `id` char(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT '主键ID(UUID)',
  `decision_id` char(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '决策ID',
  `keyword` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '关键词',
  `nature_rank` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '自然位排名',
  `nature_rank_change` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '自然位排名变化(距离上一次)',
  `keyword_type` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '关键词类型',
  `suggest` text CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci COMMENT '投放建议',
  `create_by` int DEFAULT NULL COMMENT '创建人ID',
  `editor_by` int DEFAULT NULL COMMENT '修改人ID',
  `creator_id` bigint DEFAULT NULL COMMENT '创建人',
  `editor_id` bigint DEFAULT NULL COMMENT '修改人',
  `create_time` datetime DEFAULT NULL COMMENT '创建时间',
  `update_time` datetime DEFAULT NULL COMMENT '更新时间',
  PRIMARY KEY (`id`) USING BTREE,
  KEY `idx_decision_id` (`decision_id`) USING BTREE,
  KEY `idx_keyword` (`keyword`) USING BTREE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci ROW_FORMAT=DYNAMIC COMMENT='agent决策核心关键词监控表'
```

#### `t_advert_agent_core_keyword_state`

- 功能：核心词人工状态管理（锁定/本周启用/本周不启用/否决），跨批次持久化，LOCKED/VETOED 不会被下次离线分析覆盖
- 写入/维护方：我方 AD-Agent + 人工运营
- 代码锚点：`repository.py:2121 _sync_core_keyword_policies_after_task()`；`repository.py:2067 upsert_core_keyword_policy()`

```sql
CREATE TABLE `t_advert_agent_core_keyword_state` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `parent_asin` varchar(20) NOT NULL,
  `parent_seller_sku` varchar(128) NOT NULL,
  `shop_id` bigint NOT NULL,
  `keyword_text` varchar(512) NOT NULL,
  `keyword_norm` varchar(512) NOT NULL,
  `state` enum('LOCKED','ENABLED','DISABLED','VETOED') NOT NULL DEFAULT 'ENABLED',
  `base_task_id` varchar(32) DEFAULT NULL,
  `base_task_finished_at` datetime(6) DEFAULT NULL,
  `operator` varchar(64) DEFAULT NULL,
  `created_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  `updated_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_product_keyword` (`parent_asin`,`parent_seller_sku`,`shop_id`,`keyword_norm`),
  KEY `idx_product_state` (`parent_asin`,`parent_seller_sku`,`shop_id`,`state`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
```

#### `t_advert_agent_create_advert_record`

- 功能：创建广告活动的调用/结果记录
- 写入/维护方：ERP 执行系统
- 代码锚点：本项目 `repository.py:397 insert_advert_record()` 明确跳过写入；无项目 INSERT

```sql
Exit code: 0
Wall time: 1 seconds
Output:
CREATE TABLE `t_advert_agent_create_advert_record` (
  `id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT '主键(UUID)',
  `shop_id` bigint DEFAULT NULL COMMENT '店铺ID',
  `parent_asin` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '父ASIN',
  `parent_seller_sku` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '父卖家SKU',
  `asin` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT 'ASIN',
  `seller_sku` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '卖家SKU',
  `current_user_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '当前用户ID',
  `keyword` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '关键词（首个关键词或代表词）',
  `campaign_id` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '创建的广告活动ID',
  `portfolio_id` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '广告组合ID',
  `budget` decimal(18,4) DEFAULT NULL COMMENT '活动预算',
  `bid` decimal(18,4) DEFAULT NULL COMMENT '关键词/投放竞价',
  `position_percent` decimal(10,4) DEFAULT NULL COMMENT '广告位加价百分比',
  `suggest_bid` decimal(18,4) DEFAULT NULL COMMENT '建议竞价',
  `promotion_type` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '推广类型（如 manual/auto）',
  `keyword_library_type` varchar(5000) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '词库/创建原因类型',
  `create_portfolio_result` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '创建组合结果 SUCCESS/FAIL',
  `create_portfolio_msg` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '创建组合信息',
  `create_result` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '创建活动结果 SUCCESS/FAIL',
  `create_msg` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '创建活动信息',
  `request_params_json` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci COMMENT '请求参数JSON',
  `response_params_json` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci COMMENT '响应参数JSON',
  `retried` int DEFAULT '0' COMMENT '重试次数',
  `retry_record_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '重试记录ID',
  `create_by` int DEFAULT NULL COMMENT '创建人',
  `editor_by` int DEFAULT NULL COMMENT '修改人',
  `creator_id` bigint DEFAULT NULL COMMENT '创建人ID',
  `editor_id` bigint DEFAULT NULL COMMENT '修改人ID',
  `create_time` datetime DEFAULT NULL COMMENT '创建时间',
  `update_time` datetime DEFAULT NULL COMMENT '更新时间',
  `campaign_name` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '广告活动名称',
  `agent_version` varchar(10) COLLATE utf8mb4_general_ci DEFAULT 'V1' COMMENT 'Agent版本(V1/V2)',
  PRIMARY KEY (`id`) USING BTREE,
  KEY `idx_shop_id` (`shop_id`) USING BTREE,
  KEY `idx_campaign_id` (`campaign_id`) USING BTREE,
  KEY `idx_create_time` (`create_time`) USING BTREE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci ROW_FORMAT=DYNAMIC COMMENT='Agent创建广告调用记录'
```

#### `t_advert_agent_create_keyword_record`

- 功能：创建关键词/否定词的调用/结果记录
- 写入/维护方：ERP 执行系统
- 代码锚点：本项目不写；创建流程只调用 MCP，未发现 INSERT

```sql
CREATE TABLE `t_advert_agent_create_keyword_record` (
  `id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT '主键(UUID)',
  `shop_id` bigint DEFAULT NULL COMMENT '店铺ID',
  `parent_asin` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '父ASIN',
  `parent_seller_sku` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '父卖家SKU',
  `keyword_type` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '关键词类型：positive/negative',
  `current_user_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '当前用户ID',
  `campaign_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '广告活动ID',
  `keyword` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '关键词/否词文本',
  `match_type` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '匹配类型',
  `bid` decimal(18,4) DEFAULT NULL COMMENT '关键词竞价（否词为空）',
  `keyword_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '创建成功后的关键词ID',
  `create_result` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '创建结果 SUCCESS/FAIL',
  `create_msg` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '创建信息',
  `request_params_json` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci COMMENT '请求参数JSON',
  `response_params_json` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci COMMENT '响应参数JSON',
  `create_by` int DEFAULT NULL COMMENT '创建人',
  `editor_by` int DEFAULT NULL COMMENT '修改人',
  `creator_id` bigint DEFAULT NULL COMMENT '创建人ID',
  `editor_id` bigint DEFAULT NULL COMMENT '修改人ID',
  `create_time` datetime DEFAULT NULL COMMENT '创建时间',
  `update_time` datetime DEFAULT NULL COMMENT '更新时间',
  `agent_version` varchar(10) COLLATE utf8mb4_general_ci DEFAULT 'V1' COMMENT 'Agent版本(V1/V2)',
  PRIMARY KEY (`id`) USING BTREE,
  KEY `idx_shop_id` (`shop_id`) USING BTREE,
  KEY `idx_campaign_id` (`campaign_id`) USING BTREE,
  KEY `idx_create_time` (`create_time`) USING BTREE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci ROW_FORMAT=DYNAMIC COMMENT='Agent创建关键词记录'
```

#### `t_advert_agent_data_metrics`

- 功能：每个 decision 的 SUMMARY/DAILY 指标快照
- 写入/维护方：我方 AD-Agent
- 代码锚点：`repository.py:1188 _upsert_legacy_metrics()`

```sql
CREATE TABLE `t_advert_agent_data_metrics` (
  `id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT '主键 UUID',
  `decision_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT '决策ID，关联 t_advert_agent_decision.id',
  `metrics_type` varchar(16) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT '记录类型：SUMMARY=周期汇总 KPI；DAILY=日趋势',
  `day_str` varchar(16) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '日期键：DAILY=yyyy-MM-dd；SUMMARY=固定值 SUMMARY',
  `avg_daily_sale_num` decimal(18,4) DEFAULT NULL COMMENT '日均销量（SUMMARY）',
  `acos` decimal(18,4) DEFAULT NULL COMMENT 'ACOS，百分比展示值（SUMMARY + DAILY）',
  `organic_order_rate` decimal(18,4) DEFAULT NULL COMMENT '自然订单占比，百分比展示值（SUMMARY）',
  `tacos` decimal(18,4) DEFAULT NULL COMMENT 'TACOS，百分比展示值（SUMMARY）',
  `overall_cvr` decimal(18,4) DEFAULT NULL COMMENT '整体转化率，百分比展示值（SUMMARY）',
  `cvr` decimal(18,4) DEFAULT NULL COMMENT 'CVR，百分比展示值（DAILY）',
  `ctr` decimal(18,4) DEFAULT NULL COMMENT 'CTR，百分比展示值（DAILY）',
  `cpc` decimal(18,4) DEFAULT NULL COMMENT '单次点击成本 CPC，金额（DAILY）',
  `daily_cost` decimal(18,4) DEFAULT NULL COMMENT '每日广告花费，金额（DAILY）',
  `create_by` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '创建人',
  `editor_by` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '修改人',
  `creator_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '创建人ID',
  `editor_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '修改人ID',
  `create_time` datetime DEFAULT NULL COMMENT '创建时间',
  `update_time` datetime DEFAULT NULL COMMENT '更新时间',
  `version` int DEFAULT NULL COMMENT '乐观锁版本号',
  PRIMARY KEY (`id`) USING BTREE,
  UNIQUE KEY `uk_metrics_decision_type_day` (`decision_id`,`metrics_type`,`day_str`) USING BTREE,
  KEY `idx_metrics_decision` (`decision_id`) USING BTREE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci ROW_FORMAT=DYNAMIC COMMENT='广告辅助决策-数据监控（展示指标）'
```

#### `t_advert_agent_decision`

- 功能：一次广告分析决策批次主表，含产品/阶段/季节/目标等快照
- 写入/维护方：我方 AD-Agent
- 代码锚点：`repository.py:1234 _upsert_decision()`

```sql
CREATE TABLE `t_advert_agent_decision` (
  `id` char(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT '主键ID(UUID)',
  `parent_asin` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '父ASIN',
  `parent_seller_sku` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '卖家SKU',
  `shop_id` bigint DEFAULT NULL COMMENT '店铺ID',
  `site_code` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '站点',
  `day_range` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '分析天数：DAY_7/DAY_14/DAY_30',
  `product_position` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '产品定位',
  `product_stage` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '产品阶段',
  `season_type` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '淡旺季阶段',
  `advert_purposes` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '广告目的，多个以逗号分隔',
  `target_keyword_types` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '目标关键词类型，多个以逗号分隔',
  `target_acos_suggest` int DEFAULT NULL COMMENT '推荐ACOS',
  `daily_budget_suggest` decimal(12,2) DEFAULT NULL COMMENT '推荐每日预算',
  `advert_direction_types` varchar(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '广告方向快照，JSON数组，见 AgentDirectionTypeEnum',
  `batch_no` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '批次号',
  `create_by` int DEFAULT NULL COMMENT '创建人ID',
  `editor_by` int DEFAULT NULL COMMENT '修改人ID',
  `creator_id` bigint DEFAULT NULL COMMENT '创建人',
  `editor_id` bigint DEFAULT NULL COMMENT '修改人',
  `create_time` datetime DEFAULT NULL COMMENT '创建时间',
  `update_time` datetime DEFAULT NULL COMMENT '更新时间',
  `analysis_mode` varchar(16) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT 'REALTIME' COMMENT '分析模式 REALTIME/SCHEDULED/MANUAL',
  `is_latest` tinyint(1) NOT NULL DEFAULT '0' COMMENT '该ASIN最新批次(我方 finalize_batch 维护)',
  `product_name` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '产品名',
  PRIMARY KEY (`id`) USING BTREE,
  KEY `idx_parent_asin` (`parent_asin`) USING BTREE,
  KEY `idx_shop_id` (`shop_id`) USING BTREE,
  KEY `idx_batch_no` (`batch_no`) USING BTREE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci ROW_FORMAT=DYNAMIC COMMENT='agent决策表'
```

#### `t_advert_agent_decision_config`

- 功能：运营配置、定时分析开关及 ASIN 来源；最近决策回写字段也在此表
- 写入/维护方：混合：ERP/运营维护配置，我方回写运行状态
- 代码锚点：读取：`app/data/decision_config_reader.py`；写入：`repository.py:1297 _upsert_decision_config()`

```sql
CREATE TABLE `t_advert_agent_decision_config` (
  `id` char(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT '主键ID(UUID)',
  `shop_id` bigint DEFAULT NULL COMMENT '店铺ID',
  `parent_asin` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '父ASIN',
  `parent_seller_sku` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '卖家SKU',
  `site_code` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '站点',
  `day_range` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '分析天数',
  `product_position` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '产品定位',
  `product_stage` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '产品阶段',
  `season_type` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '淡旺季阶段',
  `advert_purposes` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '广告目的，多个以逗号分隔',
  `target_keyword_types` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '目标关键词类型，多个以逗号分隔',
  `target_acos_suggest` int DEFAULT NULL COMMENT '推荐ACOS',
  `daily_budget_suggest` decimal(12,2) DEFAULT NULL COMMENT '推荐每日预算',
  `advert_direction_types` varchar(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '广告方向，JSON数组，见 AgentDirectionTypeEnum',
  `create_by` int DEFAULT NULL COMMENT '创建人ID',
  `editor_by` int DEFAULT NULL COMMENT '修改人ID',
  `creator_id` bigint DEFAULT NULL COMMENT '创建人',
  `editor_id` bigint DEFAULT NULL COMMENT '修改人',
  `create_time` datetime DEFAULT NULL COMMENT '创建时间',
  `update_time` datetime DEFAULT NULL COMMENT '更新时间',
  `enabled` tinyint(1) DEFAULT '1' COMMENT '是否启用定时',
  `frequency` varchar(16) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT 'DAILY' COMMENT '调度频率',
  `last_decision_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '上次决策ID',
  `last_run_time` datetime DEFAULT NULL COMMENT '上次执行时间',
  PRIMARY KEY (`id`) USING BTREE,
  KEY `idx_shop_id` (`shop_id`) USING BTREE,
  KEY `idx_parent_asin` (`parent_asin`) USING BTREE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci ROW_FORMAT=DYNAMIC COMMENT='agent决策配置表'
```

#### `t_advert_agent_decision_config_bak_drop_codex`

- 功能：`decision_config` 的历史备份表（当前生产库仍存在）
- 写入/维护方：ERP/运维备份
- 代码锚点：本项目无读写锚点

```sql
Exit code: 0
Wall time: 0.9 seconds
Output:
CREATE TABLE `t_advert_agent_decision_config_bak_drop_codex` (
  `id` char(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT '主键ID(UUID)',
  `shop_id` bigint DEFAULT NULL COMMENT '店铺ID',
  `parent_asin` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '父ASIN',
  `parent_seller_sku` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '卖家SKU',
  `site_code` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '站点',
  `day_range` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '分析天数',
  `product_position` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '产品定位',
  `product_stage` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '产品阶段',
  `season_type` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '淡旺季阶段',
  `advert_purposes` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '广告目的，多个以逗号分隔',
  `target_keyword_types` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '目标关键词类型，多个以逗号分隔',
  `target_acos_suggest` int DEFAULT NULL COMMENT '推荐ACOS',
  `daily_budget_suggest` decimal(12,2) DEFAULT NULL COMMENT '推荐每日预算',
  `advert_direction_types` varchar(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '广告方向，JSON数组，见 AgentDirectionTypeEnum',
  `create_by` int DEFAULT NULL COMMENT '创建人ID',
  `editor_by` int DEFAULT NULL COMMENT '修改人ID',
  `creator_id` bigint DEFAULT NULL COMMENT '创建人',
  `editor_id` bigint DEFAULT NULL COMMENT '修改人',
  `create_time` datetime DEFAULT NULL COMMENT '创建时间',
  `update_time` datetime DEFAULT NULL COMMENT '更新时间',
  `enabled` tinyint(1) DEFAULT '1' COMMENT '是否启用定时',
  `frequency` varchar(16) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT 'DAILY' COMMENT '调度频率',
  `last_decision_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '上次决策ID',
  `last_run_time` datetime DEFAULT NULL COMMENT '上次执行时间',
  `enable_codex` tinyint(1) DEFAULT '0' COMMENT '是否启用 codex 复核(1=启用, 0=关闭)',
  PRIMARY KEY (`id`) USING BTREE,
  KEY `idx_shop_id` (`shop_id`) USING BTREE,
  KEY `idx_parent_asin` (`parent_asin`) USING BTREE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci ROW_FORMAT=DYNAMIC COMMENT='agent决策配置表'
```

#### `t_advert_agent_direction_recommend`

- 功能：前置方向推荐主表（结论 JSON）
- 写入/维护方：我方 AD-Agent
- 代码锚点：`repository.py:1112 _upsert_legacy_recommend()`；兼容路径 `:1520 _upsert_wizard_direction()`

```sql
CREATE TABLE `t_advert_agent_direction_recommend` (
  `id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT '主键',
  `decision_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT '关联决策ID',
  `shop_id` bigint DEFAULT NULL COMMENT '店铺ID',
  `parent_asin` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '父ASIN',
  `parent_seller_sku` varchar(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '父SellerSku',
  `site_code` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '站点',
  `batch_no` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '批次号',
  `decision_basis_json` text CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci COMMENT '【决策依据】JSON字符串数组，如 ["...", "..."]',
  `conclusion_json` text CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci COMMENT '【结论】JSON字符串数组',
  `data_focus_json` text CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci COMMENT '【数据关注】JSON字符串数组',
  `create_by` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '创建人',
  `editor_by` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '修改人',
  `create_time` datetime DEFAULT NULL COMMENT '创建时间',
  `update_time` datetime DEFAULT NULL COMMENT '更新时间',
  `creator_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '创建人ID',
  `editor_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '修改人ID',
  `version` int DEFAULT NULL COMMENT '版本号',
  PRIMARY KEY (`id`) USING BTREE,
  KEY `idx_dir_rec_decision` (`decision_id`) USING BTREE,
  KEY `idx_dir_rec_listing` (`shop_id`,`parent_asin`,`parent_seller_sku`) USING BTREE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci ROW_FORMAT=DYNAMIC COMMENT='广告方向推荐-主表'
```

#### `t_advert_agent_direction_recommend_detail`

- 功能：前置方向推荐明细
- 写入/维护方：我方 AD-Agent
- 代码锚点：`repository.py:1150 _upsert_legacy_details()`；兼容路径 `:1520 _upsert_wizard_direction()`

```sql
CREATE TABLE `t_advert_agent_direction_recommend_detail` (
  `id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT '主键',
  `direction_recommend_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT '主表ID',
  `decision_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '关联决策ID（冗余）',
  `direction_type` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT '方向类型：PROMOTE_NATURAL_RANK/OPTIMIZE_ACOS/ADD_KEYWORD_EXPANSION/BALANCE_MAINTENANCE',
  `item_count` int DEFAULT '0' COMMENT '条目数量，页面展示如 5个、69个',
  `recommend_tag` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '推荐标签文案，Agent自由推送，如暂不建议、可考虑',
  `suggest_score` int DEFAULT NULL COMMENT '推荐分数，页面展示如85分；非建议条数',
  `content_json` text CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci COMMENT '卡片正文 JSON字符串数组',
  `sort_order` int DEFAULT '0' COMMENT '展示排序 1~4',
  `create_by` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '创建人',
  `editor_by` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '修改人',
  `create_time` datetime DEFAULT NULL COMMENT '创建时间',
  `update_time` datetime DEFAULT NULL COMMENT '更新时间',
  `creator_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '创建人ID',
  `editor_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '修改人ID',
  `version` int DEFAULT NULL COMMENT '版本号',
  PRIMARY KEY (`id`) USING BTREE,
  KEY `idx_dir_rec_detail_recommend` (`direction_recommend_id`) USING BTREE,
  KEY `idx_dir_rec_detail_decision` (`decision_id`) USING BTREE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci ROW_FORMAT=DYNAMIC COMMENT='广告方向推荐-方向明细（固定4方向各1行）'
```

#### `t_advert_agent_keyword_suggest_bid_record`

- 功能：关键词建议竞价查询的请求/响应记录
- 写入/维护方：ERP 执行系统
- 代码锚点：本项目通过 MCP 查询（`app/data/campaign_fetcher.py`），未发现写入该表

```sql
CREATE TABLE `t_advert_agent_keyword_suggest_bid_record` (
  `id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT '主键(UUID)',
  `shop_id` bigint DEFAULT NULL COMMENT '店铺ID',
  `parent_asin` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '父ASIN',
  `parent_seller_sku` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '父卖家SKU',
  `request_params_json` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci COMMENT '请求参数JSON',
  `response_params_json` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci COMMENT '响应参数JSON',
  `error_msg` varchar(1000) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '错误信息',
  `create_by` int DEFAULT NULL COMMENT '创建人',
  `editor_by` int DEFAULT NULL COMMENT '修改人',
  `creator_id` bigint DEFAULT NULL COMMENT '创建人ID',
  `editor_id` bigint DEFAULT NULL COMMENT '修改人ID',
  `create_time` datetime DEFAULT NULL COMMENT '创建时间',
  `update_time` datetime DEFAULT NULL COMMENT '更新时间',
  PRIMARY KEY (`id`) USING BTREE,
  KEY `idx_shop_id` (`shop_id`) USING BTREE,
  KEY `idx_create_time` (`create_time`) USING BTREE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci ROW_FORMAT=DYNAMIC COMMENT='Agent关键词建议竞价查询记录'
```

#### `t_advert_agent_modify_advert_record`

- 功能：广告修改执行主记录
- 写入/维护方：ERP 执行系统
- 代码锚点：本项目 `repository.py:397 insert_advert_record()` / `:406 update_advert_record_result()` 明确不写

```sql
CREATE TABLE `t_advert_agent_modify_advert_record` (
  `id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT '主键',
  `task_id` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '异步任务ID',
  `decision_id` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '关联agent决策ID',
  `shop_id` bigint DEFAULT NULL COMMENT '店铺ID',
  `parent_asin` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '父ASIN',
  `parent_seller_sku` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '父卖家SKU',
  `current_user_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '当前用户ID',
  `request_params_json` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci COMMENT '请求参数JSON',
  `response_params_json` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci COMMENT '响应结果JSON',
  `create_by` int DEFAULT NULL COMMENT '创建人',
  `editor_by` int DEFAULT NULL COMMENT '修改人',
  `creator_id` bigint DEFAULT NULL COMMENT '创建人ID',
  `editor_id` bigint DEFAULT NULL COMMENT '修改人ID',
  `create_time` datetime DEFAULT NULL,
  `update_time` datetime DEFAULT NULL,
  `version` int DEFAULT '0',
  `retry_from_record_id` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '重试来源记录ID',
  `retry_from_record_type` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '重试来源记录类型',
  `skip_risk_check` int DEFAULT '0' COMMENT '跳过风控检查',
  `agent_version` varchar(10) COLLATE utf8mb4_general_ci DEFAULT 'V1' COMMENT 'Agent版本(V1/V2)',
  PRIMARY KEY (`id`) USING BTREE,
  KEY `idx_task_id` (`task_id`) USING BTREE,
  KEY `idx_decision_id` (`decision_id`) USING BTREE,
  KEY `idx_shop_asin` (`shop_id`,`parent_asin`) USING BTREE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci ROW_FORMAT=DYNAMIC COMMENT='Agent修改广告主记录'
```

#### `t_advert_agent_modify_campaign_pending`

- 功能：活动预算/状态等待确认与待执行动作
- 写入/维护方：我方 AD-Agent 生成，ERP/前端确认并执行
- 代码锚点：`repository.py:802 _upsert_modern_cards_and_pending()`（INSERT `:867`）

```sql
Exit code: 0
Wall time: 1.2 seconds
Output:
CREATE TABLE `t_advert_agent_modify_campaign_pending` (
  `id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT '主键',
  `shop_id` bigint DEFAULT NULL COMMENT '店铺ID',
  `parent_asin` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '父ASIN',
  `parent_seller_sku` varchar(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '父SellerSku',
  `decision_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT '关联决策ID',
  `suggest_card_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT '关联卡片ID',
  `batch_no` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '批次号',
  `site_code` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '站点',
  `portfolio_id` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '广告组合ID，可选',
  `campaign_id` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '广告活动ID',
  `campaign_name` varchar(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '广告活动名称',
  `old_state` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '修改前状态',
  `new_state` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '修改后状态',
  `old_budget` decimal(18,4) DEFAULT NULL COMMENT '修改前Budget',
  `new_budget` decimal(18,4) DEFAULT NULL COMMENT '修改后Budget',
  `submit_user_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '提交人ID',
  `submit_user_name` varchar(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '提交人姓名',
  `confirm_status` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT 'PENDING' COMMENT '确认状态',
  `confirm_user_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '确认人ID',
  `confirm_user_name` varchar(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '确认人姓名',
  `confirm_time` datetime DEFAULT NULL COMMENT '确认时间',
  `reject_reason` varchar(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '拒绝原因',
  `execute_status` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT 'PENDING' COMMENT '执行状态',
  `execute_msg` varchar(1024) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '执行结果',
  `execute_time` datetime DEFAULT NULL COMMENT '执行时间',
  `remark` varchar(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '备注',
  `create_by` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '创建人',
  `editor_by` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '修改人',
  `creator_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '创建人ID',
  `editor_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '修改人ID',
  `create_time` datetime DEFAULT NULL COMMENT '创建时间',
  `update_time` datetime DEFAULT NULL COMMENT '更新时间',
  `version` int DEFAULT NULL COMMENT '版本号',
  PRIMARY KEY (`id`) USING BTREE,
  KEY `idx_camp_pending_card` (`suggest_card_id`) USING BTREE,
  KEY `idx_camp_pending_decision` (`decision_id`) USING BTREE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci ROW_FORMAT=DYNAMIC COMMENT='分析建议-活动Budget/状态修改'
```

#### `t_advert_agent_modify_campaign_record`

- 功能：活动修改的实际执行明细
- 写入/维护方：ERP 执行系统
- 代码锚点：本项目 `repository.py:413 insert_exec_sub_records()` 明确不写

```sql
CREATE TABLE `t_advert_agent_modify_campaign_record` (
  `id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL,
  `record_id` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '关联主记录ID',
  `decision_id` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '决策批次ID',
  `portfolio_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '所属广告组合ID',
  `campaign_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '广告活动ID',
  `risk_check_pass` int DEFAULT NULL COMMENT '风控是否通过（1=通过，0=不通过）',
  `risk_reject_reason` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '风控拒绝原因',
  `old_budget` decimal(18,4) DEFAULT NULL COMMENT '修改前活动预算',
  `new_budget` decimal(18,4) DEFAULT NULL COMMENT '请求修改的活动预算',
  `old_strategy` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '修改前竞价策略',
  `new_strategy` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '请求修改的竞价策略',
  `old_state` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '修改前活动状态',
  `new_state` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '请求修改的活动状态',
  `modify_result` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '修改结果 SUCCESS/FAIL/SKIP/REJECTED',
  `error_msg` varchar(1000) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL,
  `create_by` int DEFAULT NULL COMMENT '创建人',
  `editor_by` int DEFAULT NULL COMMENT '修改人',
  `creator_id` bigint DEFAULT NULL COMMENT '创建人ID',
  `editor_id` bigint DEFAULT NULL COMMENT '修改人ID',
  `create_time` datetime DEFAULT NULL,
  `update_time` datetime DEFAULT NULL,
  `version` int DEFAULT '0',
  `shop_id` bigint DEFAULT NULL COMMENT '店铺ID',
  `parent_asin` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '父ASIN',
  `parent_seller_sku` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '父卖家SKU',
  `retried` int DEFAULT '0' COMMENT '重试次数',
  `retry_record_id` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '重试记录ID',
  `agent_version` varchar(10) COLLATE utf8mb4_general_ci DEFAULT 'V1' COMMENT 'Agent版本(V1/V2)',
  PRIMARY KEY (`id`) USING BTREE,
  KEY `idx_record_id` (`record_id`) USING BTREE,
  KEY `idx_campaign_id` (`campaign_id`) USING BTREE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci ROW_FORMAT=DYNAMIC COMMENT='Agent修改活动记录'
```

#### `t_advert_agent_modify_keyword_pending`

- 功能：关键词 bid/状态/否定词等待确认动作
- 写入/维护方：我方 AD-Agent 生成，ERP/前端确认并执行
- 代码锚点：`repository.py:802 _upsert_modern_cards_and_pending()`（INSERT `:845`）

```sql
CREATE TABLE `t_advert_agent_modify_keyword_pending` (
  `id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT '主键',
  `shop_id` bigint DEFAULT NULL COMMENT '店铺ID',
  `parent_asin` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '父ASIN',
  `parent_seller_sku` varchar(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '父SellerSku',
  `site_code` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '站点',
  `decision_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT '关联决策ID',
  `batch_no` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '批次号',
  `suggest_card_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT '关联卡片ID',
  `campaign_id` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '广告活动ID',
  `campaign_name` varchar(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '广告活动名称',
  `keyword_id` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '关键词ID',
  `keyword_text` varchar(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '关键词文本',
  `match_type` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT 'EXACT/PHRASE/BROAD',
  `old_state` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '修改前状态',
  `new_state` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '修改后状态',
  `old_bid` decimal(18,4) DEFAULT NULL COMMENT '修改前Bid',
  `new_bid` decimal(18,4) DEFAULT NULL COMMENT '修改后Bid',
  `submit_user_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '提交人ID',
  `submit_user_name` varchar(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '提交人姓名',
  `confirm_status` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT 'PENDING' COMMENT '确认状态',
  `confirm_user_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '确认人ID',
  `confirm_user_name` varchar(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '确认人姓名',
  `confirm_time` datetime DEFAULT NULL COMMENT '确认时间',
  `reject_reason` varchar(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '拒绝原因',
  `execute_status` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT 'PENDING' COMMENT '执行状态',
  `execute_msg` varchar(1024) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '执行结果',
  `execute_time` datetime DEFAULT NULL COMMENT '执行时间',
  `remark` varchar(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '备注',
  `create_by` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '创建人',
  `editor_by` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '修改人',
  `creator_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '创建人ID',
  `editor_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '修改人ID',
  `create_time` datetime DEFAULT NULL COMMENT '创建时间',
  `update_time` datetime DEFAULT NULL COMMENT '更新时间',
  `version` int DEFAULT NULL COMMENT '版本号',
  PRIMARY KEY (`id`) USING BTREE,
  KEY `idx_kw_pending_card` (`suggest_card_id`) USING BTREE,
  KEY `idx_kw_pending_decision` (`decision_id`) USING BTREE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci ROW_FORMAT=DYNAMIC COMMENT='分析建议-关键词修改'
```

#### `t_advert_agent_modify_keyword_record`

- 功能：关键词修改实际执行明细
- 写入/维护方：ERP 执行系统
- 代码锚点：本项目 `repository.py:413 insert_exec_sub_records()` 明确不写

```sql
CREATE TABLE `t_advert_agent_modify_keyword_record` (
  `id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL,
  `record_id` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '关联主记录ID',
  `decision_id` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '决策批次ID',
  `campaign_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '所属活动ID',
  `keyword_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '关键词ID',
  `keyword_text` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '关键词文本',
  `risk_check_pass` int DEFAULT NULL COMMENT '风控是否通过（1=通过，0=不通过）',
  `risk_reject_reason` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '风控拒绝原因',
  `old_bid` decimal(18,4) DEFAULT NULL COMMENT '修改前BID',
  `new_bid` decimal(18,4) DEFAULT NULL COMMENT '请求修改的BID',
  `old_state` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '修改前状态',
  `new_state` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '请求修改的状态',
  `modify_result` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '修改结果 SUCCESS/FAIL/SKIP/REJECTED',
  `error_msg` varchar(1000) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL,
  `create_by` int DEFAULT NULL COMMENT '创建人',
  `editor_by` int DEFAULT NULL COMMENT '修改人',
  `creator_id` bigint DEFAULT NULL COMMENT '创建人ID',
  `editor_id` bigint DEFAULT NULL COMMENT '修改人ID',
  `create_time` datetime DEFAULT NULL,
  `update_time` datetime DEFAULT NULL,
  `version` int DEFAULT '0',
  `shop_id` bigint DEFAULT NULL COMMENT '店铺ID',
  `parent_asin` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '父ASIN',
  `parent_seller_sku` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '父卖家SKU',
  `retried` int DEFAULT '0' COMMENT '重试次数',
  `retry_record_id` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '重试记录ID',
  `agent_version` varchar(10) COLLATE utf8mb4_general_ci DEFAULT 'V1' COMMENT 'Agent版本(V1/V2)',
  PRIMARY KEY (`id`) USING BTREE,
  KEY `idx_record_id` (`record_id`) USING BTREE,
  KEY `idx_campaign_id` (`campaign_id`) USING BTREE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci ROW_FORMAT=DYNAMIC COMMENT='Agent修改关键词记录'
```

#### `t_advert_agent_modify_placement_pending`

- 功能：广告位加价等待确认动作
- 写入/维护方：我方 AD-Agent 生成，ERP/前端确认并执行
- 代码锚点：`repository.py:802 _upsert_modern_cards_and_pending()`（INSERT `:885`）

```sql
CREATE TABLE `t_advert_agent_modify_placement_pending` (
  `id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT '主键',
  `shop_id` bigint DEFAULT NULL COMMENT '店铺ID',
  `parent_asin` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '父ASIN',
  `parent_seller_sku` varchar(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '父SellerSku',
  `decision_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT '关联决策ID',
  `suggest_card_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT '关联卡片ID',
  `batch_no` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '批次号',
  `site_code` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '站点',
  `campaign_id` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '广告活动ID',
  `campaign_name` varchar(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '广告活动名称',
  `placement_type` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT 'TOP_OF_SEARCH/REST_OF_SEARCH/PRODUCT_PAGE',
  `old_percent` decimal(18,4) DEFAULT NULL COMMENT '修改前加价%',
  `new_percent` decimal(18,4) DEFAULT NULL COMMENT '修改后加价%',
  `adjust_action` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '调整动作，如维持',
  `submit_user_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '提交人ID',
  `submit_user_name` varchar(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '提交人姓名',
  `confirm_status` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT 'PENDING' COMMENT '确认状态',
  `confirm_user_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '确认人ID',
  `confirm_user_name` varchar(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '确认人姓名',
  `confirm_time` datetime DEFAULT NULL COMMENT '确认时间',
  `reject_reason` varchar(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '拒绝原因',
  `execute_status` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT 'PENDING' COMMENT '执行状态',
  `execute_msg` varchar(1024) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '执行结果',
  `execute_time` datetime DEFAULT NULL COMMENT '执行时间',
  `remark` varchar(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '说明，如「无花费，无数据」',
  `create_by` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '创建人',
  `editor_by` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '修改人',
  `creator_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '创建人ID',
  `editor_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '修改人ID',
  `create_time` datetime DEFAULT NULL COMMENT '创建时间',
  `update_time` datetime DEFAULT NULL COMMENT '更新时间',
  `version` int DEFAULT NULL COMMENT '版本号',
  PRIMARY KEY (`id`) USING BTREE,
  KEY `idx_plc_pending_card` (`suggest_card_id`) USING BTREE,
  KEY `idx_plc_pending_decision` (`decision_id`) USING BTREE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci ROW_FORMAT=DYNAMIC COMMENT='分析建议-广告位加价修改'
```

#### `t_advert_agent_modify_placement_record`

- 功能：广告位加价实际执行明细
- 写入/维护方：ERP 执行系统
- 代码锚点：本项目 `repository.py:413 insert_exec_sub_records()` 明确不写

```sql
Exit code: 0
Wall time: 0.9 seconds
Output:
CREATE TABLE `t_advert_agent_modify_placement_record` (
  `id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL,
  `record_id` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '关联主记录ID',
  `decision_id` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '决策批次ID',
  `campaign_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '所属活动ID',
  `placement_type` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '广告位类型：TOP/PRODUCT_PAGE/REST',
  `risk_check_pass` int DEFAULT NULL COMMENT '风控是否通过（1=通过，0=不通过）',
  `risk_reject_reason` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '风控拒绝原因',
  `old_percent` decimal(10,4) DEFAULT NULL COMMENT '修改前加价比例',
  `new_percent` decimal(10,4) DEFAULT NULL COMMENT '请求修改的加价比例',
  `modify_result` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '修改结果 SUCCESS/FAIL/SKIP/REJECTED',
  `error_msg` varchar(1000) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL,
  `create_by` int DEFAULT NULL COMMENT '创建人',
  `editor_by` int DEFAULT NULL COMMENT '修改人',
  `creator_id` bigint DEFAULT NULL COMMENT '创建人ID',
  `editor_id` bigint DEFAULT NULL COMMENT '修改人ID',
  `create_time` datetime DEFAULT NULL,
  `update_time` datetime DEFAULT NULL,
  `version` int DEFAULT '0',
  `shop_id` bigint DEFAULT NULL COMMENT '店铺ID',
  `parent_asin` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '父ASIN',
  `parent_seller_sku` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '父卖家SKU',
  `retried` int DEFAULT '0' COMMENT '重试次数',
  `retry_record_id` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '重试记录ID',
  `agent_version` varchar(10) COLLATE utf8mb4_general_ci DEFAULT 'V1' COMMENT 'Agent版本(V1/V2)',
  PRIMARY KEY (`id`) USING BTREE,
  KEY `idx_record_id` (`record_id`) USING BTREE,
  KEY `idx_campaign_id` (`campaign_id`) USING BTREE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci ROW_FORMAT=DYNAMIC COMMENT='Agent修改广告位记录'
```

#### `t_advert_agent_modify_portfolio_record`

- 功能：广告组合修改实际执行明细
- 写入/维护方：ERP 执行系统
- 代码锚点：本项目 `repository.py:418 insert_portfolio_records()` 明确不写

```sql
CREATE TABLE `t_advert_agent_modify_portfolio_record` (
  `id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL,
  `record_id` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '关联主记录ID',
  `portfolio_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '广告组合ID',
  `risk_check_pass` int DEFAULT NULL COMMENT '风控是否通过（1=通过，0=不通过）',
  `risk_reject_reason` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '风控拒绝原因',
  `old_budget` decimal(18,4) DEFAULT NULL COMMENT '修改前组合预算',
  `new_budget` decimal(18,4) DEFAULT NULL COMMENT '请求修改的组合预算',
  `modify_result` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '修改结果 SUCCESS/FAIL/SKIP/REJECTED',
  `error_msg` varchar(1000) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL,
  `create_by` int DEFAULT NULL COMMENT '创建人',
  `editor_by` int DEFAULT NULL COMMENT '修改人',
  `creator_id` bigint DEFAULT NULL COMMENT '创建人ID',
  `editor_id` bigint DEFAULT NULL COMMENT '修改人ID',
  `create_time` datetime DEFAULT NULL,
  `update_time` datetime DEFAULT NULL,
  `version` int DEFAULT '0',
  `shop_id` bigint DEFAULT NULL COMMENT '店铺ID',
  `parent_asin` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '父ASIN',
  `parent_seller_sku` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '父卖家SKU',
  `retried` int DEFAULT '0' COMMENT '重试次数',
  `retry_record_id` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '重试记录ID',
  `agent_version` varchar(10) COLLATE utf8mb4_general_ci DEFAULT 'V1' COMMENT 'Agent版本(V1/V2)',
  PRIMARY KEY (`id`) USING BTREE,
  KEY `idx_record_id` (`record_id`) USING BTREE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci ROW_FORMAT=DYNAMIC COMMENT='Agent修改组合记录'
```

#### `t_advert_agent_modify_suggest_card`

- 功能：每个 Campaign 的建议卡（动作、原因、证据、确认/执行状态）
- 写入/维护方：我方 AD-Agent 生成，ERP/前端维护确认与执行状态
- 代码锚点：`repository.py:802 _upsert_modern_cards_and_pending()`（INSERT `:809`）

```sql
CREATE TABLE `t_advert_agent_modify_suggest_card` (
  `id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT '主键，即 suggest_card_id',
  `decision_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT '关联决策ID',
  `shop_id` bigint DEFAULT NULL COMMENT '店铺ID',
  `parent_asin` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '父ASIN',
  `parent_seller_sku` varchar(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '父SellerSku',
  `site_code` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '站点',
  `batch_no` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '批次号',
  `suggest_category` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT 'ELIMINATE/ADJUST/KEEP',
  `campaign_group_type` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '活动分组：core-主推 auto_broad-广泛/自动 test-测试/新增 eliminate-淘汰',
  `confidence_level` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '置信等级：high/medium/low',
  `campaign_id` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '活动ID',
  `campaign_name` varchar(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '广告活动名称',
  `asin` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '投放ASIN，UI「× B09SGC3YZB」',
  `keyword` varchar(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '匹配类型，UI「EXACT」',
  `keyword_match_type` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '关键词匹配类型',
  `trigger_rule` varchar(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '触发规则，如 NO_CVR_HIGH_SPEND',
  `description` text CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci COMMENT '分析说明(1)(2)(3)...',
  `evidence` text CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci COMMENT '证据，多行文本（换行分隔）',
  `confirm_status` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT 'PENDING' COMMENT 'PENDING/CONFIRMED/REJECTED',
  `confirm_user_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '确认人ID',
  `confirm_user_name` varchar(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '确认人姓名',
  `confirm_time` datetime DEFAULT NULL COMMENT '确认时间',
  `reject_reason` varchar(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '忽略原因',
  `execute_status` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT 'PENDING' COMMENT 'PENDING/PARTIAL/SUCCESS/FAIL',
  `execute_msg` text CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci COMMENT '执行信息（含完整错误原因）',
  `execute_time` datetime DEFAULT NULL COMMENT '执行完成时间',
  `sort_order` int DEFAULT '0' COMMENT '展示排序',
  `create_by` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '创建人',
  `editor_by` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '修改人',
  `creator_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '创建人ID',
  `editor_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '修改人ID',
  `create_time` datetime DEFAULT NULL COMMENT '创建时间',
  `update_time` datetime DEFAULT NULL COMMENT '更新时间',
  `version` int DEFAULT NULL COMMENT '版本号',
  `is_core` tinyint(1) DEFAULT '0' COMMENT '是否核心词',
  `is_prefiltered` tinyint(1) DEFAULT '0' COMMENT '是否预过滤',
  `keyword_class` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '关键词分类',
  `perf_json` text CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci COMMENT '性能指标JSON',
  `prefilter_reason` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '预过滤原因',
  `review_level` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '复评等级',
  PRIMARY KEY (`id`) USING BTREE,
  UNIQUE KEY `uk_modify_suggest_card_campaign` (`decision_id`,`campaign_id`) USING BTREE,
  KEY `idx_modify_suggest_card_decision` (`decision_id`) USING BTREE,
  KEY `idx_modify_suggest_card_listing` (`shop_id`,`parent_asin`,`parent_seller_sku`) USING BTREE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci ROW_FORMAT=DYNAMIC COMMENT='分析建议-活动卡片'
```

#### `t_advert_agent_modify_suggest_reason_group`

- 功能：建议原因分组主表
- 写入/维护方：我方 AD-Agent
- 代码锚点：`repository.py:1024 _upsert_synthesis()`（INSERT `:1067`）

```sql
CREATE TABLE `t_advert_agent_modify_suggest_reason_group` (
  `id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT 'UUID',
  `decision_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT '决策批次ID',
  `shop_id` bigint DEFAULT NULL,
  `parent_asin` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL,
  `parent_seller_sku` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL,
  `site_code` varchar(16) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL,
  `batch_no` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL,
  `group_title` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT '分组标题',
  `suggest_category` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT 'ELIMINATE/ADJUST/KEEP',
  `adjust_type` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT 'BID/BUDGET/PLACEMENT，仅 ADJUST 类使用',
  `member_count` int NOT NULL DEFAULT '0' COMMENT '成员数量',
  `description` text CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci COMMENT '分组说明/策略解释',
  `sort_order` int NOT NULL DEFAULT '0' COMMENT '展示排序',
  `create_by` varchar(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '创建人姓名',
  `editor_by` varchar(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '修改人姓名',
  `creator_id` varchar(36) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '创建人ID',
  `editor_id` varchar(36) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '修改人ID',
  `create_time` datetime DEFAULT NULL,
  `update_time` datetime DEFAULT NULL,
  PRIMARY KEY (`id`) USING BTREE,
  KEY `idx_decision_id` (`decision_id`) USING BTREE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci ROW_FORMAT=DYNAMIC COMMENT='分析建议-原因分组'
```

#### `t_advert_agent_modify_suggest_reason_group_member`

- 功能：原因分组与建议卡的关联明细
- 写入/维护方：我方 AD-Agent
- 代码锚点：`repository.py:1024 _upsert_synthesis()`（INSERT `:1085`）

```sql
CREATE TABLE `t_advert_agent_modify_suggest_reason_group_member` (
  `id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL,
  `group_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT '关联 reason_group.id',
  `suggest_card_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT '关联 modify_suggest_card.id',
  `campaign_id` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '冗余，展开成员列表展示',
  `campaign_name` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '冗余，展开成员列表展示',
  `asin` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '冗余',
  `keyword_text` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '冗余，精准类活动可选',
  `sort_order` int NOT NULL DEFAULT '0' COMMENT '组内展示排序',
  `create_by` varchar(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '创建人姓名',
  `editor_by` varchar(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '修改人姓名',
  `creator_id` varchar(36) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '创建人ID',
  `editor_id` varchar(36) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '修改人ID',
  `create_time` datetime DEFAULT NULL,
  `update_time` datetime DEFAULT NULL,
  PRIMARY KEY (`id`) USING BTREE,
  UNIQUE KEY `uk_group_card` (`group_id`,`suggest_card_id`) USING BTREE,
  KEY `idx_group_id` (`group_id`) USING BTREE,
  KEY `idx_card_id` (`suggest_card_id`) USING BTREE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci ROW_FORMAT=DYNAMIC COMMENT='分析建议-原因分组成员'
```

#### `t_advert_agent_modify_suggest_special`

- 功能：特殊调整说明展示块
- 写入/维护方：我方 AD-Agent
- 代码锚点：`repository.py:1024 _upsert_synthesis()`（INSERT `:1098`）

```sql
Exit code: 0
Wall time: 0.8 seconds
Output:
CREATE TABLE `t_advert_agent_modify_suggest_special` (
  `id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL,
  `decision_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT '决策批次ID',
  `shop_id` bigint DEFAULT NULL,
  `parent_asin` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL,
  `parent_seller_sku` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL,
  `site_code` varchar(16) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL,
  `batch_no` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL,
  `display_text` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT '行首展示文本',
  `metrics_text` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '指标摘要，如 ACOS=41.6%',
  `recommendation` text CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci COMMENT '建议文案',
  `sort_order` int NOT NULL DEFAULT '0' COMMENT '展示排序',
  `create_by` varchar(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '创建人姓名',
  `editor_by` varchar(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '修改人姓名',
  `creator_id` varchar(36) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '创建人ID',
  `editor_id` varchar(36) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '修改人ID',
  `create_time` datetime DEFAULT NULL,
  `update_time` datetime DEFAULT NULL,
  PRIMARY KEY (`id`) USING BTREE,
  KEY `idx_decision_id` (`decision_id`) USING BTREE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci ROW_FORMAT=DYNAMIC COMMENT='分析建议-特殊调整'
```

#### `t_advert_agent_modify_suggest_summary`

- 功能：一次 decision 的调整建议汇总统计
- 写入/维护方：我方 AD-Agent
- 代码锚点：`repository.py:685 _upsert_modern_summary()`（INSERT `:713`）

```sql
CREATE TABLE `t_advert_agent_modify_suggest_summary` (
  `id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT '主键',
  `decision_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT '关联决策ID',
  `shop_id` bigint DEFAULT NULL COMMENT '店铺ID',
  `parent_asin` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '父ASIN',
  `parent_seller_sku` varchar(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '父SellerSku',
  `site_code` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '站点',
  `batch_no` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '批次号',
  `total_count` int DEFAULT '0' COMMENT '活动总数',
  `eliminate_count` int DEFAULT '0' COMMENT '淘汰数',
  `adjust_count` int DEFAULT '0' COMMENT '调整数',
  `keep_count` int DEFAULT '0' COMMENT '保持数',
  `reactivate_count` int NOT NULL DEFAULT '0' COMMENT 'KB21 §7 复评活动数（reactivate_budget_only / reactivate_with_calibrated_bid）',
  `confidence_high_count` int DEFAULT '0' COMMENT '置信-高',
  `confidence_medium_count` int DEFAULT '0' COMMENT '置信-中',
  `confidence_low_count` int DEFAULT '0' COMMENT '置信-低',
  `budget_impact` decimal(18,4) DEFAULT NULL COMMENT '预算影响，可正可负',
  `validation_passed` tinyint(1) DEFAULT NULL COMMENT '校验是否通过：1=✓ 0=✗',
  `alert_count` int DEFAULT '0' COMMENT '异常项数，可选',
  `alert_msg` text CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci COMMENT 'AI方案分析说明，可选',
  `analysis_overview` text CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci COMMENT '分析总览',
  `create_by` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '创建人',
  `editor_by` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '修改人',
  `creator_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '创建人ID',
  `editor_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '修改人ID',
  `create_time` datetime DEFAULT NULL COMMENT '创建时间',
  `update_time` datetime DEFAULT NULL COMMENT '更新时间',
  `version` int DEFAULT NULL COMMENT '版本号',
  `main_push_count` int DEFAULT NULL COMMENT '主推活动数量',
  `main_push_budget` decimal(12,2) DEFAULT NULL COMMENT '主推约束预算',
  `broad_auto_count` int DEFAULT NULL COMMENT '广泛/自动活动数量',
  `broad_auto_budget` decimal(12,2) DEFAULT NULL COMMENT '广泛/自动约束预算',
  `test_new_count` int DEFAULT NULL COMMENT '测试/新增活动数量',
  `test_new_budget` decimal(12,2) DEFAULT NULL COMMENT '测试/新增约束预算',
  `eliminate_bubble_count` int DEFAULT NULL COMMENT '淘汰活动数量',
  `eliminate_bubble_budget` decimal(12,2) DEFAULT NULL COMMENT '淘汰约束预算',
  `create_count` int DEFAULT '0' COMMENT '新建数量',
  PRIMARY KEY (`id`) USING BTREE,
  UNIQUE KEY `uk_modify_suggest_summary_decision` (`decision_id`) USING BTREE,
  KEY `idx_modify_suggest_summary_listing` (`shop_id`,`parent_asin`,`parent_seller_sku`) USING BTREE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci ROW_FORMAT=DYNAMIC COMMENT='分析建议-分析概览汇总'
```

#### `t_advert_agent_modify_target_record`

- 功能：商品投放（target）修改实际执行明细
- 写入/维护方：ERP 执行系统
- 代码锚点：本项目未写入；执行记录由 ERP 系统维护

```sql
CREATE TABLE `t_advert_agent_modify_target_record` (
  `id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT '主键(UUID)',
  `shop_id` bigint DEFAULT NULL COMMENT '店铺ID',
  `parent_asin` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '父ASIN',
  `parent_seller_sku` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '父卖家SKU',
  `record_id` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '关联主记录ID',
  `campaign_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '所属活动ID',
  `target_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '投放ID',
  `risk_check_pass` int DEFAULT NULL COMMENT '风控是否通过（1=通过，0=不通过）',
  `risk_reject_reason` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '风控拒绝原因',
  `old_bid` decimal(18,4) DEFAULT NULL COMMENT '修改前BID',
  `new_bid` decimal(18,4) DEFAULT NULL COMMENT '请求修改的BID',
  `old_state` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '修改前状态',
  `new_state` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '请求修改的状态',
  `modify_result` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '修改结果 SUCCESS/FAIL/SKIP/REJECTED',
  `error_msg` varchar(1000) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '错误信息',
  `retried` int DEFAULT '0' COMMENT '重试次数',
  `retry_record_id` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '重试记录ID',
  `create_by` int DEFAULT NULL COMMENT '创建人',
  `editor_by` int DEFAULT NULL COMMENT '修改人',
  `creator_id` bigint DEFAULT NULL COMMENT '创建人ID',
  `editor_id` bigint DEFAULT NULL COMMENT '修改人ID',
  `create_time` datetime DEFAULT NULL COMMENT '创建时间',
  `update_time` datetime DEFAULT NULL COMMENT '更新时间',
  `agent_version` varchar(10) COLLATE utf8mb4_general_ci DEFAULT 'V1' COMMENT 'Agent版本(V1/V2)',
  PRIMARY KEY (`id`) USING BTREE,
  KEY `idx_shop_id` (`shop_id`) USING BTREE,
  KEY `idx_record_id` (`record_id`) USING BTREE,
  KEY `idx_campaign_id` (`campaign_id`) USING BTREE,
  KEY `idx_create_time` (`create_time`) USING BTREE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci ROW_FORMAT=DYNAMIC COMMENT='Agent修改商品投放记录'
```

#### `t_advert_agent_pool_entry`

- 功能：淘汰/复评池条目及入池、出池、花费快照
- 写入/维护方：我方 AD-Agent 执行钩子
- 代码锚点：`repository.py:1816 upsert_pool_entry()`；`workflow/steps/advert_execution.py`

```sql
CREATE TABLE `t_advert_agent_pool_entry` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `shop_id` int DEFAULT NULL,
  `shop_account` varchar(64) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `parent_asin` varchar(50) COLLATE utf8mb4_unicode_ci NOT NULL,
  `parent_sku` varchar(64) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `child_asin` varchar(64) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `campaign_id` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `campaign_key` varchar(600) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `campaign_name` varchar(512) COLLATE utf8mb4_unicode_ci NOT NULL,
  `keyword_text` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `decision_id` varchar(64) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `source` varchar(16) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'discovery',
  `entry_date` datetime(6) NOT NULL,
  `exit_date` datetime(6) DEFAULT NULL,
  `eliminate_spend_7d` decimal(12,2) DEFAULT NULL,
  `create_time` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  `update_time` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_asin_cid` (`parent_asin`,`campaign_id`),
  KEY `idx_parent_exit` (`parent_asin`,`exit_date`)
) ENGINE=InnoDB AUTO_INCREMENT=17534 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
```

#### `t_advert_agent_purpose_score`

- 功能：每个广告目的的评分、依据、建议与后续关注
- 写入/维护方：我方 AD-Agent
- 代码锚点：`repository.py:1353 _upsert_purpose_scores()`

```sql
CREATE TABLE `t_advert_agent_purpose_score` (
  `id` char(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT '主键ID(UUID)',
  `decision_id` char(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '决策ID',
  `advert_purpose` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '广告目的',
  `score` int DEFAULT NULL COMMENT '评分',
  `recommend_level` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '推荐等级',
  `decision_basis` text CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci COMMENT '决策依据',
  `suggest` text CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci COMMENT '建议',
  `future_attention` text CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci COMMENT '后续关注',
  `create_by` int DEFAULT NULL COMMENT '创建人ID',
  `editor_by` int DEFAULT NULL COMMENT '修改人ID',
  `creator_id` bigint DEFAULT NULL COMMENT '创建人',
  `editor_id` bigint DEFAULT NULL COMMENT '修改人',
  `create_time` datetime DEFAULT NULL COMMENT '创建时间',
  `update_time` datetime DEFAULT NULL COMMENT '更新时间',
  PRIMARY KEY (`id`) USING BTREE,
  KEY `idx_decision_id` (`decision_id`) USING BTREE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci ROW_FORMAT=DYNAMIC COMMENT='agent决策广告目的评分表'
```

#### `t_advert_agent_switch_keyword_record`

- 功能：关键词匹配类型切换实际执行明细
- 写入/维护方：ERP 执行系统
- 代码锚点：本项目未写入；执行记录由 ERP 系统维护

```sql
Exit code: 0
Wall time: 0.8 seconds
Output:
CREATE TABLE `t_advert_agent_switch_keyword_record` (
  `id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT '主键(UUID)',
  `shop_id` bigint DEFAULT NULL COMMENT '店铺ID',
  `parent_asin` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '父ASIN',
  `parent_seller_sku` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '父卖家SKU',
  `current_user_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '当前用户ID',
  `campaign_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '广告活动ID',
  `old_keyword_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '原关键词ID',
  `new_keyword_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '新关键词ID',
  `keyword` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '关键词文本',
  `old_match_type` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '原匹配类型',
  `new_match_type` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '新匹配类型',
  `bid` decimal(18,4) DEFAULT NULL COMMENT '新竞价',
  `create_result` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '切换结果 SUCCESS/FAIL',
  `create_msg` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci DEFAULT NULL COMMENT '切换信息',
  `request_params_json` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci COMMENT '请求参数JSON',
  `response_params_json` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci COMMENT '响应参数JSON',
  `create_by` int DEFAULT NULL COMMENT '创建人',
  `editor_by` int DEFAULT NULL COMMENT '修改人',
  `creator_id` bigint DEFAULT NULL COMMENT '创建人ID',
  `editor_id` bigint DEFAULT NULL COMMENT '修改人ID',
  `create_time` datetime DEFAULT NULL COMMENT '创建时间',
  `update_time` datetime DEFAULT NULL COMMENT '更新时间',
  `agent_version` varchar(10) COLLATE utf8mb4_general_ci DEFAULT 'V1' COMMENT 'Agent版本(V1/V2)',
  PRIMARY KEY (`id`) USING BTREE,
  KEY `idx_shop_id` (`shop_id`) USING BTREE,
  KEY `idx_campaign_id` (`campaign_id`) USING BTREE,
  KEY `idx_create_time` (`create_time`) USING BTREE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci ROW_FORMAT=DYNAMIC COMMENT='Agent切换关键词匹配类型记录'
```

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
