# 分析建议 Tab — 表结构、DDL 与字段说明

> 版本：**v3.0** · 日期：2026-06-02  
> 数据源：**agentMysql**  
> 完整建表 DDL：`docs/sql/advert_agent_modify_suggest_full_ddl.sql`  
> 增量迁移（v2→v3）：`docs/sql/advert_agent_modify_suggest_ddl.sql`  
> 决策主表 DDL：`docs/sql/advert_agent_decision_ddl.sql`

---

## 1. 概述

「分析建议」Tab 分两层数据：

1. **分析概览**（1 行 summary）— 顶部四个统计数字 + 置信/预算影响/校验
2. **建议卡片**（N 行 card + 子表 pending）— **1 个广告活动 = 1 张卡**

**写入顺序：** `decision` → `summary` → `card` → `keyword/campaign/placement pending`

**写入方：** 第三方 Agent 推送；WHP 只读展示 + 用户「同意/忽略」更新 confirm 字段。

---

## 2. 表关系

```
t_advert_agent_decision
    ├── t_advert_agent_modify_suggest_summary      (1)
    └── t_advert_agent_modify_suggest_card         (N, 1 campaign = 1 card)
            ├── t_advert_agent_modify_keyword_pending    (0~n)
            ├── t_advert_agent_modify_campaign_pending   (0~n)
            └── t_advert_agent_modify_placement_pending  (0~3)
```

---

## 3. UI 区块 ↔ 字段对照（原型示例）

```
[淘汰] [low] 精准-3pcs-sexy halloween costumes plus size
× B09SGC3YZB | EXACT | 关键词: sexy halloween costumes plus size | 触发规则: NO_CVR_HIGH_SPEND
Bid: $0.42 → $0.20 ($-0.22)
Budget: $2.00 → $1.00 ($-1.00)
(1) 近7天花费$0... (2) 该词为季节性词... (3) 建议移入淘汰组...
证据:
  7天花费$0
  曝光3次，点击0
  季节性词，非旺季无转化
广告位调整:
  头部: 0% → 0% (维持) — 无花费，无数据
  其他: 0% → 0% (维持) — 无花费，无数据
  商品: 0% → 0% (维持) — 无花费，无数据
[待处理] [同意] [忽略]
```

| UI 内容 | 表 | 字段 |
|---------|-----|------|
| 102 活动总数 | `modify_suggest_summary` | `total_count` |
| 64 淘汰 / 38 调整 / 0 保持 | `modify_suggest_summary` | `eliminate_count` / `adjust_count` / `keep_count` |
| 置信: 高/中/低 | `modify_suggest_summary` | `confidence_high_count` / `confidence_medium_count` / `confidence_low_count` |
| 预算影响: -$98.41 | `modify_suggest_summary` | `budget_impact` |
| 校验 ✓/✗ | `modify_suggest_summary` | `validation_passed` |
| AI 方案分析说明 | `modify_suggest_summary` | `alert_msg` |
| 淘汰 Tag | `modify_suggest_card` | `suggest_category` = `ELIMINATE` |
| low Tag | `modify_suggest_card` | `confidence_level` = `low` |
| 活动名称 | `modify_suggest_card` | `campaign_name` |
| × B09SGC3YZB | `modify_suggest_card` | `asin` |
| 关键词: xxx | `modify_suggest_card` | `keyword` |
| EXACT | `modify_suggest_card` | `keyword_match_type` |
| 触发规则: NO_CVR_HIGH_SPEND | `modify_suggest_card` | `trigger_rule` |
| Bid: $0.42 → $0.20 | `modify_keyword_pending` | `old_bid` / `new_bid`（有值即 BID 调整） |
| Budget: $2.00 → $1.00 | `modify_campaign_pending` | `old_budget` / `new_budget`（有值即 BUDGET 调整） |
| (1)(2)(3) 分析说明 | `modify_suggest_card` | `description` |
| 证据列表 | `modify_suggest_card` | `evidence`（多行文本，换行分隔） |
| 广告位 头部/其他/商品 | `modify_placement_pending` | `placement_type` + `old_percent` + `new_percent` |
| (维持) | `modify_placement_pending` | `adjust_action` |
| — 无花费，无数据 | `modify_placement_pending` | `remark` |
| 待处理/已同意/已忽略 | `modify_suggest_card` | `confirm_status` |

---

## 4. 字段明细

### 4.1 `t_advert_agent_modify_suggest_summary` — 分析概览

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| id | VARCHAR(32) | Y | 主键 UUID |
| decision_id | VARCHAR(32) | Y | 关联决策，唯一 |
| shop_id | BIGINT | N | 店铺 ID |
| parent_asin | VARCHAR(64) | N | 父 ASIN（Listing 维度） |
| parent_seller_sku | VARCHAR(128) | N | 父 SellerSku |
| site_code | VARCHAR(32) | N | 站点 |
| batch_no | VARCHAR(64) | N | 批次号 |
| total_count | INT | N | 活动总数 |
| eliminate_count | INT | N | 淘汰数 |
| adjust_count | INT | N | 调整数 |
| keep_count | INT | N | 保持数 |
| confidence_high_count | INT | N | 置信-高 |
| confidence_medium_count | INT | N | 置信-中 |
| confidence_low_count | INT | N | 置信-低 |
| budget_impact | DECIMAL(18,4) | N | 预算影响（美元，可正可负） |
| validation_passed | TINYINT(1) | N | 1=校验通过 ✓，0=未通过 ✗ |
| alert_count | INT | N | 异常项数，可选 |
| alert_msg | TEXT | N | AI 方案分析说明 |
| create_by / editor_by / creator_id / editor_id | — | N | 审计字段 |
| create_time / update_time / version | — | N | 审计字段 |

**约束：** 每个 `decision_id` 仅 1 行。

---

### 4.2 `t_advert_agent_modify_suggest_card` — 活动卡片

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| id | VARCHAR(32) | Y | 主键 UUID，即子表 `suggest_card_id` |
| decision_id | VARCHAR(32) | Y | 关联决策 |
| shop_id | BIGINT | N | 店铺 ID |
| parent_asin | VARCHAR(64) | N | 父 ASIN |
| parent_seller_sku | VARCHAR(128) | N | 父 SellerSku |
| site_code | VARCHAR(32) | N | 站点 |
| batch_no | VARCHAR(64) | N | 批次号 |
| suggest_category | VARCHAR(32) | Y | `ELIMINATE` / `ADJUST` / `KEEP` |
| confidence_level | VARCHAR(32) | N | `high` / `medium` / `low` |
| campaign_group_type | VARCHAR(32) | N | 组合类型（Java 字段 `campaignGroupType`）：`exact_core_group` / `exact_testing_group` / `auto_broad_group` / `low_bid_retention_group`；来源 Agent `ai_portfolio_class`（精准主力组 / 精准测试组 / 自动广泛组 / 低价捡漏组） |
| campaign_id | VARCHAR(64) | Y | 活动 ID，同 decision 下唯一 |
| campaign_name | VARCHAR(512) | Y | 活动名称（卡片标题） |
| asin | VARCHAR(64) | N | 投放 ASIN，Meta 行「× B09SGC3YZB」 |
| keyword | VARCHAR(512) | N | 关键词文本，Meta 行「关键词: xxx」 |
| keyword_match_type | VARCHAR(32) | N | 匹配类型，Meta 行「EXACT」 |
| trigger_rule | VARCHAR(128) | N | 触发规则，如 `NO_CVR_HIGH_SPEND` |
| description | TEXT | N | 分析说明 (1)(2)(3)... |
| evidence | TEXT | N | 证据，多行文本（换行 `\n` 分隔） |
| confirm_status | VARCHAR(32) | Y | 推送时 `PENDING`；用户操作后 WHP 更新 |
| confirm_user_id | VARCHAR(32) | N | 确认人 ID |
| confirm_user_name | VARCHAR(128) | N | 确认人姓名 |
| confirm_time | DATETIME | N | 确认/忽略时间 |
| reject_reason | VARCHAR(512) | N | 忽略原因 |
| execute_status | VARCHAR(32) | Y | 推送时 `PENDING`；执行后 WHP 更新 |
| execute_msg | VARCHAR(1024) | N | 执行结果摘要 |
| execute_time | DATETIME | N | 执行完成时间 |
| sort_order | INT | N | 展示排序，升序 |

**v3 变更（相对 v2）：**

- 移除：`tags_json`、`suggest_remark`、`evidence_json`、`approval_status`
- 新增：`asin`、`keyword`、`keyword_match_type`、`description`、`evidence`、`campaign_group_type`
- Meta 行（ASIN/匹配类型/关键词）从 keyword_pending 上移到 card 表

---

### 4.3 `t_advert_agent_modify_keyword_pending` — 关键词修改

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| id | VARCHAR(32) | Y | 主键 |
| shop_id / parent_asin / parent_seller_sku / site_code | — | N | 冗余维度 |
| decision_id | VARCHAR(32) | Y | 关联决策 |
| batch_no | VARCHAR(64) | N | 批次号 |
| suggest_card_id | VARCHAR(32) | Y | 关联卡片 |
| campaign_id | VARCHAR(64) | N | 活动 ID |
| campaign_name | VARCHAR(512) | N | 活动名称冗余 |
| keyword_id | VARCHAR(64) | Y* | 调 Amazon API 必填 |
| keyword_text | VARCHAR(512) | N | 关键词文本（筛选搜索） |
| match_type | VARCHAR(32) | N | EXACT/PHRASE/BROAD |
| old_state / new_state | VARCHAR(32) | N | 状态变更（淘汰/暂停），有值即 STATE 调整 |
| old_bid / new_bid | DECIMAL(18,4) | N | BID 变更（美元），有值即 BID 调整 |
| submit_user_id / submit_user_name | — | N | Agent 提交人 |
| confirm_status | VARCHAR(32) | Y | 推送 `PENDING` |
| confirm_user_id / confirm_user_name / confirm_time | — | N | WHP 写入 |
| reject_reason | VARCHAR(512) | N | 拒绝原因 |
| execute_status / execute_msg / execute_time | — | N | 单条 API 执行结果 |
| remark | VARCHAR(512) | N | 备注 |

**v3 变更：** 移除 `ad_group_id`、`ad_group_name`、`target_asin`、`modify_sub_type`。BID/STATE 通过字段是否有值推断。

---

### 4.4 `t_advert_agent_modify_campaign_pending` — 活动 Budget/状态

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| id | VARCHAR(32) | Y | 主键 |
| shop_id / parent_asin / parent_seller_sku | — | N | 冗余维度 |
| decision_id | VARCHAR(32) | Y | 关联决策 |
| suggest_card_id | VARCHAR(32) | Y | 关联卡片 |
| batch_no | VARCHAR(64) | N | 批次号 |
| site_code | VARCHAR(32) | N | 站点 |
| portfolio_id | VARCHAR(64) | N | 广告组合，一般留空 |
| campaign_id | VARCHAR(64) | N | 活动 ID |
| campaign_name | VARCHAR(512) | N | 活动名称冗余 |
| old_state / new_state | VARCHAR(32) | N | 活动暂停，有值即 STATE 调整 |
| old_budget / new_budget | DECIMAL(18,4) | N | 日预算（美元），有值即 BUDGET 调整 |
| confirm_status ~ remark | — | — | 同 keyword_pending |

**v3 变更：** 移除 `modify_sub_type`。

---

### 4.5 `t_advert_agent_modify_placement_pending` — 广告位加价

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| id | VARCHAR(32) | Y | 主键 |
| shop_id / parent_asin / parent_seller_sku | — | N | 冗余维度 |
| decision_id | VARCHAR(32) | Y | 关联决策 |
| suggest_card_id | VARCHAR(32) | Y | 关联卡片 |
| batch_no | VARCHAR(64) | N | 批次号 |
| site_code | VARCHAR(32) | N | 站点 |
| campaign_id / campaign_name | — | N | 活动冗余 |
| placement_type | VARCHAR(32) | Y | 见下表 |
| old_percent / new_percent | DECIMAL(18,4) | Y | 加价比例 % |
| adjust_action | VARCHAR(32) | N | 如「维持」 |
| confirm_status ~ remark | — | — | 同 keyword_pending |

**placement_type 枚举：**

| 值 | UI 显示 |
|----|---------|
| TOP_OF_SEARCH | 头部 |
| REST_OF_SEARCH | 其他 |
| PRODUCT_PAGE | 商品 |

**v3 变更：** 移除 `ad_group_id`。

---

## 5. 枚举速查

**suggest_category（卡片分类）：**

| 值 | UI |
|----|-----|
| ELIMINATE | 淘汰 |
| ADJUST | 调整 |
| KEEP | 保持 |

**confidence_level：** `high` / `medium` / `low`

**confirm_status：** `PENDING` / `CONFIRMED` / `REJECTED`

**execute_status：** `PENDING` / `PARTIAL` / `SUCCESS` / `FAIL`

**修改类型推断（v3 无 modify_sub_type）：**

| 表 | 判断条件 | 含义 |
|----|----------|------|
| keyword_pending | `old_bid` 或 `new_bid` 有值 | BID 调整 |
| keyword_pending | `old_state` 或 `new_state` 有值 | 关键词暂停 |
| campaign_pending | `old_budget` 或 `new_budget` 有值 | Budget 调整 |
| campaign_pending | `old_state` 或 `new_state` 有值 | 活动暂停 |

---

## 6. 完整入库示例

### Step 1 — 分析概览

```sql
INSERT INTO t_advert_agent_modify_suggest_summary
(id, decision_id, shop_id, parent_asin, parent_seller_sku, site_code, batch_no,
 total_count, eliminate_count, adjust_count, keep_count,
 confidence_high_count, confidence_medium_count, confidence_low_count,
 budget_impact, validation_passed, create_time)
VALUES
('sum-001', 'dec-001', 12345, 'B0C5H9PJN2', 'PN22731-US', 'US', '20260601184352',
 102, 64, 38, 0, 0, 65, 37, -98.41, 0, NOW());
```

### Step 2 — 卡片主表

```sql
INSERT INTO t_advert_agent_modify_suggest_card
(id, decision_id, shop_id, parent_asin, parent_seller_sku, site_code, batch_no,
 suggest_category, confidence_level, campaign_id, campaign_name,
 asin, keyword, keyword_match_type, trigger_rule, description, evidence,
 confirm_status, execute_status, sort_order, create_time)
VALUES
('card-001', 'dec-001', 12345, 'B0C5H9PJN2', 'PN22731-US', 'US', '20260601184352',
 'ELIMINATE', 'low', 'camp-100', '精准-3pcs-sexy halloween costumes plus size',
 'B09SGC3YZB', 'sexy halloween costumes plus size', 'EXACT', 'NO_CVR_HIGH_SPEND',
 '(1) 近7天花费$0，曝光仅3次，无点击，无订单；上线658天，长期无转化。(2) 该词为季节性词（万圣节），当前非旺季，且长期无数据，无保留价值。(3) 建议移入淘汰组，预算$1.00，Bid $0.20，等待旺季自动恢复。',
 '7天花费$0\n曝光3次，点击0\n季节性词，非旺季无转化',
 'PENDING', 'PENDING', 1, NOW());
```

### Step 3 — 关键词 Bid

```sql
INSERT INTO t_advert_agent_modify_keyword_pending
(id, decision_id, suggest_card_id, shop_id, parent_asin, parent_seller_sku, site_code, batch_no,
 campaign_id, campaign_name, keyword_id, keyword_text, match_type,
 old_bid, new_bid, confirm_status, execute_status, create_time)
VALUES
('kw-001', 'dec-001', 'card-001', 12345, 'B0C5H9PJN2', 'PN22731-US', 'US', '20260601184352',
 'camp-100', '精准-3pcs-sexy halloween costumes plus size', 'kw-amz-999',
 'sexy halloween costumes plus size', 'EXACT',
 0.42, 0.20, 'PENDING', 'PENDING', NOW());
```

### Step 4 — 活动 Budget

```sql
INSERT INTO t_advert_agent_modify_campaign_pending
(id, decision_id, suggest_card_id, shop_id, parent_asin, parent_seller_sku, site_code, batch_no,
 campaign_id, campaign_name, old_budget, new_budget,
 confirm_status, execute_status, create_time)
VALUES
('camp-p-001', 'dec-001', 'card-001', 12345, 'B0C5H9PJN2', 'PN22731-US', 'US', '20260601184352',
 'camp-100', '精准-3pcs-sexy halloween costumes plus size', 2.00, 1.00,
 'PENDING', 'PENDING', NOW());
```

### Step 5 — 广告位（3 行）

```sql
INSERT INTO t_advert_agent_modify_placement_pending
(id, decision_id, suggest_card_id, shop_id, parent_asin, parent_seller_sku, site_code, batch_no,
 campaign_id, campaign_name, placement_type, old_percent, new_percent, adjust_action, remark,
 confirm_status, execute_status, create_time)
VALUES
('plc-001', 'dec-001', 'card-001', 12345, 'B0C5H9PJN2', 'PN22731-US', 'US', '20260601184352',
 'camp-100', '精准-3pcs-sexy halloween costumes plus size', 'TOP_OF_SEARCH', 0, 0, '维持', '无花费，无数据', 'PENDING', 'PENDING', NOW()),
('plc-002', 'dec-001', 'card-001', 12345, 'B0C5H9PJN2', 'PN22731-US', 'US', '20260601184352',
 'camp-100', '精准-3pcs-sexy halloween costumes plus size', 'REST_OF_SEARCH', 0, 0, '维持', '无花费，无数据', 'PENDING', 'PENDING', NOW()),
('plc-003', 'dec-001', 'card-001', 12345, 'B0C5H9PJN2', 'PN22731-US', 'US', '20260601184352',
 'camp-100', '精准-3pcs-sexy halloween costumes plus size', 'PRODUCT_PAGE', 0, 0, '维持', '无花费，无数据', 'PENDING', 'PENDING', NOW());
```

---

## 7. 注意事项

1. 同一 `decision_id + campaign_id` 只能 1 张 card。
2. 用户「同意/忽略」由 WHP 更新 card 及子表 `confirm_status`，第三方推送时填 `PENDING`。
3. `parent_asin` 是 Listing 父 ASIN；卡片 Meta 行的 `× B09SGC3YZB` 是 card 表的 `asin`（投放/子 ASIN），两者不同。
4. 卡片分类 `suggest_category` 与 summary 统计（eliminate/adjust/keep_count）口径应一致。
5. `evidence` 为多行纯文本，每行一条证据，前端按换行拆分展示。
6. 审计字段建议至少填 `create_time`；`create_by` 等按实际库表补齐。

---

## 8. 相关代码文件

| 文件 | 说明 |
|------|------|
| `docs/sql/advert_agent_modify_suggest_full_ddl.sql` | 完整建表 DDL |
| `docs/sql/advert_agent_modify_suggest_ddl.sql` | v2→v3 增量迁移 |
| `advert.domain/.../pending/*.java` | Domain 实体（字段权威来源） |
| `advert.dto/.../pending/*Dto.java` | DTO |
| `advert.biz/.../AdvertAgentDashboardServiceImpl.java` | Dashboard 聚合 |
| `advert.biz/.../AdvertAgentModifySuggestServiceImpl.java` | 同意/忽略/执行 |
| `whp-view/.../AdvertAgentPlanTab.vue` | Tab5 前端 |
