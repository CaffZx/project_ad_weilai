# Agent 广告修改待确认表 — 表结构说明文档

> 对应实体包：`com.erp.whp.advert.domain.advertagent.pending`  
> DDL 脚本：[advert_agent_modify_pending_ddl.sql](./advert_agent_modify_pending_ddl.sql)  
> 生成日期：2026-06-01

## 1. 概述

本模块包含 4 张「待确认」表，用于存储 Agent 决策产生的广告修改建议。业务流程为：

1. Agent 提交修改建议 → 写入待确认表（`confirm_status = PENDING`）
2. 运营人员确认或拒绝 → 更新确认状态
3. 确认通过后执行 Amazon API 调用 → 更新执行状态（`execute_status`）

四张表结构高度一致，共享「商品维度 + 审批流 + 执行流」字段，仅在业务对象与修改内容字段上存在差异。

```
Agent 决策(decision_id)
        │
        ├── 关键词修改待确认  t_advert_agent_modify_keyword_pending
        ├── 活动修改待确认    t_advert_agent_modify_campaign_pending
        ├── 广告位加价待确认  t_advert_agent_modify_placement_pending
        └── 组合预算待确认    t_advert_agent_modify_portfolio_pending
```

---

## 2. 公共字段说明

以下字段在四张表中均存在，继承自 `AbstractJpaMyBatisPlusBaseDomain` 的审计字段一并列出。

### 2.1 商品与站点维度

| 字段名 | Java 属性 | 类型 | 必填 | 说明 |
|--------|-----------|------|------|------|
| `shop_id` | shopId | BIGINT | 是 | 店铺 ID |
| `parent_asin` | parentAsin | VARCHAR(20) | 否 | 父 ASIN |
| `parent_seller_sku` | parentSellerSku | VARCHAR(128) | 否 | 卖家 SKU |
| `site_code` | siteCode | VARCHAR(16) | 否 | 站点编码，如 US、UK、DE |

### 2.2 决策与批次

| 字段名 | Java 属性 | 类型 | 必填 | 说明 |
|--------|-----------|------|------|------|
| `decision_id` | decisionId | VARCHAR(64) | 否 | 关联 Agent 决策 ID，对应 `t_advert_agent_decision.id` |
| `batch_no` | batchNo | VARCHAR(64) | 否 | 批次号，同一批提交的修改共享 |

### 2.3 提交信息

| 字段名 | Java 属性 | 类型 | 必填 | 说明 |
|--------|-----------|------|------|------|
| `submit_user_id` | submitUserId | VARCHAR(64) | 否 | 提交人 ID |
| `submit_user_name` | submitUserName | VARCHAR(128) | 否 | 提交人姓名 |

### 2.4 确认流程

| 字段名 | Java 属性 | 类型 | 必填 | 说明 |
|--------|-----------|------|------|------|
| `confirm_status` | confirmStatus | VARCHAR(32) | 是 | 确认状态，默认 `PENDING` |
| `confirm_user_id` | confirmUserId | VARCHAR(64) | 否 | 确认人 ID |
| `confirm_user_name` | confirmUserName | VARCHAR(128) | 否 | 确认人姓名 |
| `confirm_time` | confirmTime | DATETIME | 否 | 确认/拒绝时间 |
| `reject_reason` | rejectReason | VARCHAR(512) | 否 | 拒绝原因（`confirm_status = REJECTED` 时填写） |

**confirm_status 枚举值**（`ConfirmStatusEnum`）：

| 值 | 含义 |
|----|------|
| `PENDING` | 待确认 |
| `CONFIRMED` | 已确认 |
| `REJECTED` | 已拒绝 |

### 2.5 执行流程

| 字段名 | Java 属性 | 类型 | 必填 | 说明 |
|--------|-----------|------|------|------|
| `execute_status` | executeStatus | VARCHAR(32) | 否 | 执行状态，默认 `PENDING` |
| `execute_msg` | executeMsg | VARCHAR(1024) | 否 | 执行结果信息或错误描述 |
| `execute_time` | executeTime | DATETIME | 否 | 执行完成时间 |

**execute_status 枚举值**（`ExecuteStatusEnum`）：

| 值 | 含义 |
|----|------|
| `PENDING` | 未执行 |
| `SUCCESS` | 执行成功 |
| `FAIL` | 执行失败 |

### 2.6 其他

| 字段名 | Java 属性 | 类型 | 必填 | 说明 |
|--------|-----------|------|------|------|
| `remark` | remark | VARCHAR(512) | 否 | 备注 |

### 2.7 审计字段（基类）

| 字段名 | Java 属性 | 类型 | 必填 | 说明 |
|--------|-----------|------|------|------|
| `id` | id | VARCHAR(32) | 是 | 主键，UUID（`IdType.ASSIGN_UUID`） |
| `creator_id` | creatorId | VARCHAR(64) | 否 | 创建人 ID |
| `editor_id` | editorId | VARCHAR(64) | 否 | 最后修改人 ID |
| `create_time` | createTime | DATETIME | 是 | 创建时间 |
| `update_time` | updateTime | DATETIME | 是 | 更新时间 |
| `version` | version | INT | 是 | 乐观锁版本号，默认 0 |

---

## 3. 各表详细说明

### 3.1 t_advert_agent_modify_keyword_pending

**表说明**：Agent 修改广告关键词待确认表  
**实体类**：`AdvertAgentModifyKeywordPending`  
**用途**：存储关键词状态（STATE）或竞价（BID）的修改建议，待人工确认后执行。

| 字段名 | Java 属性 | 类型 | 必填 | 说明 |
|--------|-----------|------|------|------|
| `campaign_id` | campaignId | VARCHAR(64) | 否 | 所属广告活动 ID |
| `keyword_id` | keywordId | VARCHAR(64) | 否 | Amazon 关键词 ID |
| `keyword_text` | keywordText | VARCHAR(512) | 否 | 关键词文本（冗余，方便列表展示） |
| `modify_sub_type` | modifySubType | VARCHAR(32) | 否 | 修改子类型：`STATE` / `BID` |
| `old_state` | oldState | VARCHAR(32) | 否 | 修改前关键词状态 |
| `new_state` | newState | VARCHAR(32) | 否 | 请求修改后的状态 |
| `old_bid` | oldBid | DECIMAL(18,4) | 否 | 修改前竞价 |
| `new_bid` | newBid | DECIMAL(18,4) | 否 | 请求修改后的竞价 |

**常用查询条件**（见 `AdvertAgentModifyKeywordPendingServiceImpl`）：`shop_id`、`parent_asin`、`campaign_id`、`keyword_id`、`confirm_status`、`modify_sub_type`、`batch_no`、`decision_id`

---

### 3.2 t_advert_agent_modify_campaign_pending

**表说明**：Agent 修改广告活动待确认表  
**实体类**：`AdvertAgentModifyCampaignPending`  
**用途**：存储广告活动状态（STATE）或预算（BUDGET）的修改建议。

| 字段名 | Java 属性 | 类型 | 必填 | 说明 |
|--------|-----------|------|------|------|
| `portfolio_id` | portfolioId | VARCHAR(64) | 否 | 所属广告组合 ID |
| `campaign_id` | campaignId | VARCHAR(64) | 否 | 广告活动 ID |
| `modify_sub_type` | modifySubType | VARCHAR(32) | 否 | 修改子类型：`STATE` / `BUDGET` |
| `old_state` | oldState | VARCHAR(32) | 否 | 修改前活动状态 |
| `new_state` | newState | VARCHAR(32) | 否 | 请求修改后的状态 |
| `old_budget` | oldBudget | DECIMAL(18,2) | 否 | 修改前活动预算 |
| `new_budget` | newBudget | DECIMAL(18,2) | 否 | 请求修改后的预算 |

**常用查询条件**（见 `AdvertAgentModifyCampaignPendingServiceImpl`）：`shop_id`、`parent_asin`、`campaign_id`、`confirm_status`、`modify_sub_type`、`batch_no`、`decision_id`

---

### 3.3 t_advert_agent_modify_placement_pending

**表说明**：Agent 修改广告位加价待确认表  
**实体类**：`AdvertAgentModifyPlacementPending`  
**用途**：存储广告位竞价加价比例（Placement Bid Adjustment）的修改建议。

| 字段名 | Java 属性 | 类型 | 必填 | 说明 |
|--------|-----------|------|------|------|
| `campaign_id` | campaignId | VARCHAR(64) | 否 | 所属广告活动 ID |
| `placement_type` | placementType | VARCHAR(64) | 否 | 广告位类型 |
| `old_percent` | oldPercent | DECIMAL(10,2) | 否 | 修改前加价比例（%） |
| `new_percent` | newPercent | DECIMAL(10,2) | 否 | 请求修改后的加价比例（%） |

**placement_type 枚举值**：

| 值 | 含义 |
|----|------|
| `TOP_OF_SEARCH` | 搜索结果顶部 |
| `PRODUCT_PAGE` | 商品详情页 |
| `REST_OF_SEARCH` | 其余搜索位置 |

**常用查询条件**（见 `AdvertAgentModifyPlacementPendingServiceImpl`）：`shop_id`、`parent_asin`、`campaign_id`、`placement_type`、`confirm_status`、`batch_no`、`decision_id`

---

### 3.4 t_advert_agent_modify_portfolio_pending

**表说明**：Agent 修改广告组合待确认表  
**实体类**：`AdvertAgentModifyPortfolioPending`  
**用途**：存储广告组合预算的修改建议。

| 字段名 | Java 属性 | 类型 | 必填 | 说明 |
|--------|-----------|------|------|------|
| `portfolio_id` | portfolioId | VARCHAR(64) | 否 | 广告组合 ID |
| `old_budget` | oldBudget | DECIMAL(18,2) | 否 | 修改前组合预算 |
| `new_budget` | newBudget | DECIMAL(18,2) | 否 | 请求修改后的组合预算 |

**常用查询条件**（见 `AdvertAgentModifyPortfolioPendingServiceImpl`）：`shop_id`、`parent_asin`、`portfolio_id`、`confirm_status`、`batch_no`、`decision_id`

---

## 4. 表关系

| 关联对象 | 关联字段 | 说明 |
|----------|----------|------|
| `t_advert_agent_decision` | `decision_id` | 一条决策可产生多条待确认记录 |
| Amazon 广告组合 | `portfolio_id` | 活动/组合维度修改时使用 |
| Amazon 广告活动 | `campaign_id` | 活动/关键词/广告位维度修改时使用 |
| Amazon 关键词 | `keyword_id` | 关键词维度修改时使用 |

同一 `batch_no` 下的记录通常属于同一次 Agent 批量提交，可一并确认或执行。

---

## 5. 索引设计

各表均建立以下通用索引（命名前缀因表而异）：

| 索引 | 字段 | 用途 |
|------|------|------|
| 主键 | `id` | 按 ID 查询/更新 |
| 店铺+ASIN | `shop_id`, `parent_asin` | 按商品维度列表查询 |
| 决策 | `decision_id` | 按决策回溯待确认记录 |
| 批次 | `batch_no` | 按批次批量操作 |
| 确认状态 | `confirm_status` | 筛选待确认/已处理记录 |
| 创建时间 | `create_time` | 按时间排序 |

各表额外索引：

| 表 | 额外索引字段 |
|----|-------------|
| keyword_pending | `campaign_id`, `keyword_id` |
| campaign_pending | `campaign_id`, `portfolio_id` |
| placement_pending | `campaign_id` |
| portfolio_pending | `portfolio_id` |

---

## 6. 注意事项

1. **命名策略**：Java 驼峰属性通过 MyBatis-Plus 默认策略映射为下划线列名（如 `shopId` → `shop_id`）。
2. **基类字段**：`creator_id`、`editor_id`、`create_time`、`update_time`、`version` 来自 `AbstractJpaMyBatisPlusBaseDomain`，由框架自动维护。
3. **状态流转**：`confirm_status` 与 `execute_status` 独立维护；仅 `CONFIRMED` 的记录才会进入执行阶段。
4. **字符集**：DDL 使用 `utf8mb4`，支持关键词文本中的特殊字符。
5. **与 record 表区别**：`*_pending` 表存待确认数据；确认执行后的结果记录在 `t_advert_agent_modify_*_record` 系列表中。
