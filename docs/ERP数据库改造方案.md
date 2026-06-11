# ERP Agent 广告决策库 — 数据库改造方案（彻底清爽版）

> **目标库**: `erp_agentadvert` @ `192.168.2.51:3306`（MySQL **8.0.46**）
> **核查时间**: 2026-06-11（连真库 dump DDL + 索引 + 行数 + 枚举采样，不依赖旧导出文档）
> **改造原则**: 不考虑现有写入脚本兼容，从头理一遍，彻底清爽无技术债。
> **连接注意**: 服务端 `caching_sha2_password`，pymysql 非 SSL 握手会 `Lost connection`，**必须用 TLS 连**（`ssl=CERT_NONE` context 即可）。

---

## 0. 这个库的使命（一切设计据此）

双向服务库，两个使命**同等重要**：

1. **后端方便传数存数，不丢字段** — 实时分析（`CampaignAnalysisResult` + 前置工作流 1~4 结果）和定时分析的产出，都要能**完整落库，不因表结构缺列而丢字段**。
2. **合并后的统一前端方便取用渲染** — 注意：前端不是只有 campaign 调试页，而是**合并后的主看板**（产品定位→广告目的→目标ACOS→广告方向→campaign 执行层，共 5 个 tab，合并工作尚未开始）。表结构要贴合这个统一前端的渲染需求，枚举统一、结构可直取。

→ 推论：表既要"装得下后端所有字段"，又要"前端能直接渲染"。两者冲突处（如纯渲染派生字段）由**前端现算**、不入库；结构臃肿处（拆列/拆 JSON）由**库层收敛**，让前后端都干净。

---

## 1. 现状总览

**21 张表**，分两层：

| 层 | 表 | 行数 | 角色 |
|---|---|---|---|
| **前置工作流** | `t_advert_agent_decision` | 543 | 决策快照主表（每次分析 1 行） |
| | `t_advert_agent_decision_config` | 353 | 长期配置（驱动定时分析） |
| | `t_advert_agent_purpose_score` | 1978 | 广告目的评分（每决策 ~4 行） |
| | `t_advert_agent_direction_recommend` | 543 | 方向推荐主表（3 段总述） |
| | `t_advert_agent_direction_recommend_detail` | 2172 | 方向明细（每决策 4 方向） |
| | `t_advert_agent_ai_suggest` | 543 | AI 综合建议（ACOS/预算） |
| | `t_advert_agent_data_metrics` | 2135 | 指标（SUMMARY+DAILY） |
| | `t_advert_agent_core_keyword_tracking` | 8958 | 核心词监控（**99% 空，疑废弃**） |
| **执行层** | `t_advert_agent_modify_suggest_summary` | 543 | 分析概览 |
| | `t_advert_agent_modify_suggest_card` | 27370 | 活动卡片（统一卡片源） |
| | `t_advert_agent_modify_keyword_pending` | 27373 | 关键词 Bid/状态 待确认 |
| | `t_advert_agent_modify_campaign_pending` | 27370 | 活动 Budget/状态 待确认 |
| | `t_advert_agent_modify_placement_pending` | 24003 | 广告位加价 待确认 |
| | `t_advert_agent_modify_advert_record` | 26 | 执行主记录 |
| | `t_advert_agent_modify_campaign_record` | 125 | 活动执行记录 |
| | `t_advert_agent_modify_keyword_record` | 125 | 关键词执行记录 |
| | `t_advert_agent_modify_placement_record` | 54 | 广告位执行记录 |
| | `t_advert_agent_modify_portfolio_record` | **0** | 组合执行记录（未用） |
| | `t_advert_agent_modify_suggest_reason_group` | **0** | synthesis 分组（未落库） |
| | `t_advert_agent_modify_suggest_reason_group_member` | **0** | synthesis 分组成员 |
| | `t_advert_agent_modify_suggest_special` | **0** | synthesis 特殊调整 |

**数据采样澄清的疑点**：

| 疑点 | 结论 |
|---|---|
| card.keyword 存什么 | 实存关键词文本（`black lace stockings`），keyword_match_type 存 `EXACT` → **数据对、注释反** |
| decision.parent_asin | 543 行中 **227 行(42%) 非 `B0` 格式**（中文名）→ 真实数据问题 |
| ai_suggest "None" | execution_pace **全 NULL**，无 "None" 串 → 排除 |
| data_metrics day_str | DAILY 实存 `06-04` → **确无年份** |
| core_keyword | 8958 行，nature_rank 仅 1% 填充 → **空壳** |
| card.suggest_category | **只有 ADJUST/KEEP，无 ELIMINATE**；淘汰藏在 `campaign_group_type=low_bid_retention_group` |
| advert_purpose | 混进 **2 行 `Clearance`**（KB 已废除） |

---

## 2. 加什么表 —— 结论：**不新增业务表**

用户问"要加什么表"。经字段映射核查，**新增活动和预过滤活动都不需要新表**，全靠现有表加列 + 枚举扩展即可，这反而最清爽（避免双表合并/双 ViewModel）：

| 需求 | 不新增表的做法 |
|---|---|
| **新增活动**（CREATE） | `card.suggest_category` 加 `CREATE` 枚举 + `campaign_id` 改可空；新建活动的 bid/budget 走现有 `keyword_pending`/`campaign_pending`（`old_*=NULL` 天然表达"从无到有"）；专属字段 `keyword_class`/`primary_placement`/`negative_strategy` 落到 card 新列 / placement_pending（详见 §7） |
| **预过滤活动**（淘汰池/多词） | `card` 加 `is_prefiltered` + `prefilter_reason` 两列，不单独建表（预过滤活动本质也是"卡片"） |
| **synthesis 分组** | 三张表**已存在**（reason_group/member/special），无需新建，但 collation/长度要修（§6） |

> **唯一备选**：若后端写入端强烈希望把新增活动的专属字段与既有活动**物理隔离**，可加一张 `t_advert_agent_new_campaign_ext`（`suggest_card_id` 主键关联 card，存新增专属字段）。但**默认推荐不加**——card 加 3~4 列更简单，且前端一张 card 表查完全部卡片。

**净结论：0 新增业务表，全部通过 `ALTER` 完成。**

---

## 3. 全局规范统一（清爽根基，必须先做）

| # | 规范 | 现状 | 统一为 |
|---|---|---|---|
| **A1** | **Collation** 🔴最高优先 | 18 表 `utf8mb4_general_ci`；synthesis 3 表 `utf8mb4_bin` | **全库 `utf8mb4_general_ci`**。否则 `reason_group_member.suggest_card_id` JOIN `card.id` → `illegal mix of collations`，synthesis 一落库就报错 |
| **A2** | **主键/UUID 长度** | 多数 `varchar(32)`(无横线 UUID)；synthesis 3 表 `varchar(36)`(带横线) | 全统一 `varchar(32)` + 无横线 UUID |
| **A3** | **审计四件套类型** | `create_by/editor_by`: `int`(decision/ai_suggest/purpose_score/core_kw) vs `varchar(64)`；`creator_id/editor_id`: `bigint` vs `varchar(32/36)` | 全统一：`create_by`/`editor_by`/`creator_id`/`editor_id` 一律 **`bigint`**（用户 ID 数值） |
| **A4** | **decimal 精度** | pending `decimal(18,4)`；record `decimal(12,2)`/`(8,2)` | 金额(budget) `decimal(12,2)`；bid/比例(percent) `decimal(18,4)`；pending 与 record **对齐** |
| **A5** | **审计字段补全** | 部分表无 `version` 乐观锁 | 所有"可被前端确认/执行更新"的表（card + 3 pending）统一有 `version` |

---

## 4. 枚举权威表（**改什么枚举值**的核心）

以下是全库枚举的真实取值 + 问题 + 统一建议。**这张表应作为前端/后端/库三方的枚举唯一权威**。

### 4.1 执行层枚举

| 枚举 | 列 | 库现状（真实值） | 问题 | 统一建议 |
|---|---|---|---|---|
| **活动动作** | `card.suggest_category` | `ADJUST`/`KEEP`（**无 ELIMINATE/CREATE**） | 淘汰藏在 group_type；新增无枚举 | **`ELIMINATE`/`ADJUST`/`KEEP`/`CREATE`** 四值，与 group_type 解耦 |
| **组合分组** | `card.campaign_group_type` | `exact_core_group`/`auto_broad_group`/`exact_testing_group`/`low_bid_retention_group` | 值对，**注释写旧枚举**(core/auto_broad/test/eliminate) | 值不变，**注释改为新枚举** |
| **置信** | `card.confidence_level` | `high`/`medium`/`low` | OK | 不变 |
| **确认状态** | `*.confirm_status` | `PENDING`/`CONFIRMED`（REJECTED 未出现） | OK | `PENDING`/`CONFIRMED`/`REJECTED` |
| **执行状态** | `*.execute_status` | `PENDING`/`SUCCESS`（PARTIAL/FAIL 未出现） | OK | `PENDING`/`PARTIAL`/`SUCCESS`/`FAIL` |
| **广告位** 🔴 | `placement_pending.placement_type` | `TOP_OF_SEARCH`/`REST_OF_SEARCH`/`PRODUCT_PAGE` | **pending 全名、record 缩写，两套** | 统一 **`TOP_OF_SEARCH`/`REST_OF_SEARCH`/`PRODUCT_PAGE`** |
| | `placement_record.placement_type` | `TOP`/`REST`/`PRODUCT_PAGE` | 同上 | record 改为全名 |
| **匹配类型** | `keyword_pending.match_type` | `EXACT`/`BROAD`/`PHRASE` | OK | 不变 |
| **执行结果** | `*_record.modify_result` | `SUCCESS`/`FAIL`/`SKIP`/`REJECTED` | OK | 不变 |

### 4.2 前置工作流枚举

| 枚举 | 列 | 库现状（真实值） | 问题 | 统一建议 |
|---|---|---|---|---|
| **产品定位** 🔴 | `decision.product_position` | `TOP`/`WAIST`/`LONG_TAIL`（旧 3 级：头部/腰部/长尾） | **没跟上实时端 4 级迁移**（战略级/重点/常规/长尾产品），且缺"常规" | 对齐实时端 4 级：`P0_STRATEGIC`/`P1_KEY`/`P2_NORMAL`/`P3_LONGTAIL`（或与实时端 ProductLevel 最终码对齐） |
| **产品阶段** | `decision.product_stage` | `TESTING`/`PROMOTING`/`HARVEST_PROFIT`/`MAINTAINING`/`LIQUIDATING` | 命名风格与实时端不一致（`PROMOTING` vs `pushing`，`HARVEST_PROFIT` vs `harvesting`） | 二选一并锁定：建议库码 `TESTING`/`PUSHING`/`HARVESTING`/`MAINTAINING`/`LIQUIDATING`，mapper 映射实时端中文 |
| **淡旺季** | `decision.season_type` | `OFF_SEASON`/`PEAK_SEASON_PREPARE`/`BIG_PEAK_SEASON`/`LATE_PEAK_SEASON` | OK，命名规范 | 不变（建议作为其它枚举的命名范式） |
| **广告目的** 🔴 | `decision.advert_purposes` / `purpose_score.advert_purpose` | `TRAFFIC`/`CONVERSION`/`RANKING`/`PROFIT` + **`Clearance`(2 行脏数据)** | KB 已废除 Clearance | **清除 Clearance 行**，锁定 4 值，加应用层校验 |
| **关键词类型** | `decision.target_keyword_types` | `GENERIC`/`LONG_TAIL`/`COMPETITOR`/`BRAND`/`CUSTOM` | OK | 不变 |
| **广告方向** 🔴 | `decision.advert_direction_types` / `direction_detail.direction_type` | `PUSH_NATURAL`/`OPTIMIZE_ACOS`/`EXPAND_KEYWORDS`/`BALANCE_MAINTAIN` | **DDL 注释三个名字全错**（注释写 PROMOTE_NATURAL_RANK/ADD_KEYWORD_EXPANSION/BALANCE_MAINTENANCE） | 值不变，**注释改为实际值**；与实时端 `push_natural_rank` 等的映射在 mapper |
| **推荐程度** 🔴 | `direction_detail.recommend_tag` | `available`/`not_recommended`/`recommended`（英文） | 与 `purpose_score.recommend_level`（中文 可选/不推荐/推荐）**语义重复、一中一英** | 统一英文：`OPTIONAL`/`NOT_RECOMMENDED`/`RECOMMENDED`，两表同枚举 |
| **分析天数** | `decision.day_range` | `DAY_7`/`DAY_14`（DAY_30 未出现） | OK | `DAY_7`/`DAY_14`/`DAY_30` |

---

## 5. 前置工作流表逐表改造

### 5.1 `t_advert_agent_decision`（决策快照主表）

| 改动 | 说明 |
|---|---|
| 🔴 `parent_asin` 数据 | 42% 存中文名。排查写入端；`parent_asin` 应恒为 `B0…`，中文名移入新列 `product_name varchar(200)` |
| 类型 | `target_acos_suggest varchar(500)` → **`int`**；`daily_budget_suggest varchar(500)` → **`decimal(12,2)`**（数值别存超长 varchar） |
| 注释 | `advert_purposes`/`target_keyword_types`/`advert_direction_types` 注释"多个以逗号分隔" → 改 **"JSON 字符串数组"**（实存 JSON） |
| 加列 | `is_latest tinyint(1) DEFAULT 1`（标记该 ASIN 最新决策，前端取快照免按 create_time 猜）；`analysis_mode varchar(16)`（`REALTIME`/`SCHEDULED`，区分实时/定时跑） |
| 审计 | `create_by/editor_by` int → bigint（A3） |

### 5.2 `t_advert_agent_decision_config`（长期配置）

| 改动 | 说明 |
|---|---|
| 加列（驱动定时分析） | `enabled tinyint(1) DEFAULT 1`；`schedule_cron varchar(64)` 或 `frequency varchar(16)`；`last_run_time datetime`；`last_decision_id varchar(32)`（指向最近一次产出） |
| 类型 | `target_acos_suggest`/`daily_budget_suggest` 同 5.1 改数值类型 |
| 注释 | 同 5.1 改 JSON 说明 |

### 5.3 `t_advert_agent_purpose_score`

| 改动 | 说明 |
|---|---|
| 类型 | `score varchar(20)` → **`int`**；`advert_purpose varchar(200)` → `varchar(32)`（枚举码不需 200） |
| 枚举 | `recommend_level` 中文 → 英文 `OPTIONAL`/`NOT_RECOMMENDED`/`RECOMMENDED`（与 detail 表统一）；补 122 个 NULL 的默认值 |
| 数据 | 清除 `advert_purpose='Clearance'` 的 2 行 |
| 结构（可选） | `decision_basis`/`suggest`/`future_attention` 三段式与 ai_suggest 重复，可保留（清晰）或收 `analysis_json` |
| 审计 | int → bigint |

### 5.4 `t_advert_agent_direction_recommend`（方向主表）

| 改动 | 说明 |
|---|---|
| 结构 | `decision_basis_json`/`conclusion_json`/`data_focus_json` **三列 JSON** → 收成 1 列 `content_json text`（`{"basis":[],"conclusion":[],"data_focus":[]}`）。前端一次解析 |

### 5.5 `t_advert_agent_direction_recommend_detail`（方向明细）

| 改动 | 说明 |
|---|---|
| 注释 🔴 | `direction_type` 注释三个名字全错 → 改 `PUSH_NATURAL/OPTIMIZE_ACOS/EXPAND_KEYWORDS/BALANCE_MAINTAIN`（实际值） |
| 字段语义 | `suggest_score` 注释"分数85；非建议条数"一列两义 → 明确单一语义（推荐分），条数走 `item_count` |
| 枚举 | `recommend_tag` 已英文，保留 `OPTIONAL/NOT_RECOMMENDED/RECOMMENDED`（统一大写） |

### 5.6 `t_advert_agent_ai_suggest`

| 改动 | 说明 |
|---|---|
| 类型 | `suggest_acos varchar(50)` → `int`；`suggest_budget varchar(50)` → `decimal(12,2)` |
| 结构 | `suggest_acos`+`_tag`+`_decision_basis`+`_suggest`+`_future_attention` **5 列** → 收 `acos_json`；`suggest_budget_*` 5 列 → 收 `budget_json`（各含 `{value,tag,decision_basis,suggest,future_attention}`）。**大幅减列、前端一次取** |
| 文案 | `comprehensive_judgment` 正文嵌 `【综合判断】` 前缀 → 写入端去前缀（前端自加标题） |
| 审计 | int → bigint |

### 5.7 `t_advert_agent_data_metrics`

| 改动 | 说明 |
|---|---|
| 🔴 `day_str` | DAILY 存 `06-04` 缺年份 → 改存 **`yyyy-MM-dd`**（跨年隐患） |
| 结构（可选） | SUMMARY/DAILY 稀疏宽表，可接受；洁癖可拆两表 |
| 口径 | 注释"百分比展示值"——与实时端口径对齐（避免 ×100 错位），mapper 统一 |
| 已有优点 | `uk(decision_id,metrics_type,day_str)` 唯一键设计良好，保留 |

### 5.8 `t_advert_agent_core_keyword_tracking`

| 改动 | 说明 |
|---|---|
| 🔴 用途确认 | 8958 行 99% 空（nature_rank 仅 1%）→ **确认启用还是废弃**。废弃则 DROP；启用则补写入 |
| 类型 | `nature_rank`/`nature_rank_change varchar(50)` → `int`（排名是数值） |

---

## 6. 执行层表逐表改造

### 6.1 `t_advert_agent_modify_suggest_card`（统一卡片，核心）

| 改动 | 说明 |
|---|---|
| 🔴 枚举 | `suggest_category` 加 `CREATE`，并**启用 ELIMINATE**（淘汰不再只靠 group_type） |
| 🔴 约束 | `campaign_id` NOT NULL → **可空**（新增活动无 ID，创建后回填）；唯一键 `uk(decision_id,campaign_id)` 保留（NULL 不约束），新增活动加 `uk(decision_id,campaign_name)` 防重 |
| 🔴 注释 | `keyword` 注释"匹配类型 EXACT" → 改 **"关键词文本"**；`keyword_match_type` 注释"关键词文本" → 改 **"匹配类型 EXACT/PHRASE/BROAD"**（数据本身对，仅注释互换） |
| 字段 | `keyword_match_type varchar(512)` → `varchar(32)`（匹配类型不需 512）；`keyword` 列删多余的显式 `CHARACTER SET utf8mb4`（继承表默认） |
| 注释 | `campaign_group_type` 注释旧枚举 → 新枚举（§4.1） |
| 加列（补缺口，前端要） | `keyword_class varchar(32)`（KB06 五类）；`review_level varchar(32)`；`is_core tinyint(1) DEFAULT 0`；`perf_json text`（逐活动 7 天指标 `{cost,acos,cvr,cpc,ctr,clicks,orders,impressions}`） |
| 加列（预过滤可见化） | `is_prefiltered tinyint(1) DEFAULT 0`；`prefilter_reason varchar(255)`（"已入淘汰池…"/"多关键词活动…"） |

### 6.2 三张 pending 表（keyword/campaign/placement）

| 改动 | 说明 |
|---|---|
| 精度 | 与 record 对齐（A4） |
| 新增活动表达 | 新建活动：`old_bid/old_budget = NULL`、`new_* = 提案值`（天然表示"从无到有"），无需新列 |
| placement 主投位 | 新增活动 `primary_placement` → placement_pending 写 `placement_type=TOP_OF_SEARCH` + `adjust_action='首轮主投'`；其余两位不写 |
| 否词策略 | 新增活动 `negative_strategy` → 落 `keyword_pending.remark`（或 placement_pending.remark），不新列 |

### 6.3 四张 record 表

| 改动 | 说明 |
|---|---|
| 精度 | `decimal(12,2)`/`(8,2)` → 与 pending 对齐 decimal(18,4)/(12,2)（A4） |
| 枚举 | `placement_record.placement_type` 缩写 → 全名（§4.1） |
| `portfolio_record` | 0 行未用 → 确认保留/DROP |

### 6.4 `t_advert_agent_modify_suggest_summary`

| 改动 | 说明 |
|---|---|
| 结构 | portfolio **8 列**（main_push_count/budget + broad_auto_* + test_new_* + eliminate_bubble_*）→ 收 `portfolio_summary_json`；**并补新增组** `create_count` + `create_budget`（新增活动统计） |
| 加列 | `create_count int DEFAULT 0`（与 card 的 CREATE 数对齐） |
| 已有 | `analysis_overview text`（策略总览）、`uk(decision_id)` 保留 |

### 6.5 synthesis 三表（reason_group / member / special）🔴

| 改动 | 说明 |
|---|---|
| **collation** | `utf8mb4_bin` → `utf8mb4_general_ci`（A1，否则 JOIN card 报错） |
| **主键长度** | `id`/`decision_id`/`suggest_card_id`/`group_id` `varchar(36)` → `varchar(32)`（A2，对齐 card） |
| 审计 | `creator_id/editor_id varchar(36)` → bigint（A3） |
| 状态 | 三表 0 行（synthesis 未真落库）→ 改完结构后接通写入 |

---

## 7. 新增活动落库 —— 字段映射（0 新增表）

`NewCampaignItem` → 现有表，全字段有处可落：

| NewCampaignItem | 落到 | 说明 |
|---|---|---|
| (动作) | `card.suggest_category='CREATE'` | 新枚举 |
| keyword_text | `card.keyword` | |
| child_asin | `card.asin` | 投放子 ASIN（活动数最多/花费最高，代码选） |
| match_type | `card.keyword_match_type` | |
| keyword_class | `card.keyword_class`（新列） | |
| ai_portfolio_class | `card.campaign_group_type` | exact_testing_group/auto_broad_group |
| trigger_scene | `card.trigger_rule` | 复用 |
| reason | `card.description` | |
| evidence | `card.evidence` | |
| confidence | `card.confidence_level` | |
| review_level | `card.review_level`（新列） | |
| campaign_name | `card.campaign_name` | `精准-关键词-日期`，唯一 |
| campaign_id | `card.campaign_id=NULL` | 创建后回填 |
| proposed_daily_budget | `campaign_pending.new_budget`（old=NULL） | |
| proposed_base_bid | `keyword_pending.new_bid`（old=NULL） | |
| primary_placement | `placement_pending`(TOP_OF_SEARCH + adjust_action) | |
| negative_strategy | `keyword_pending.remark` | |

执行引擎按 `card.suggest_category` 分流：`CREATE` → create API（无 campaign_id 入参，成功回填）；其余 → update API。

---

## 8. 写入侧完整性自检（不丢字段）

后端实时模型 → 表字段，逐项核对**有没有列可落**：

| 模型 | 关键字段 | 落点 | 缺列？ |
|---|---|---|---|
| `CampaignAnalysisResult.summary` | total/eliminate/adjust/keep/create 计数 | summary（**补 create_count**） | ⚠️ 补 |
| `.strategic_overview` | assessment/direction/facts | summary.analysis_overview（或 direction_recommend） | ✓ |
| `.adjustments[]` | 见 card + 3 pending | card + pending | ⚠️ 补 keyword_class/review_level/perf_json |
| `.new_campaigns[]` | 见 §7 | card(CREATE)+pending | ⚠️ 补列 |
| `.skipped_campaigns[]`（预过滤） | is_prefiltered/reason | card（**补 is_prefiltered/prefilter_reason**） | ⚠️ 补 |
| `.synthesis` | groups/special_cases | reason_group/member/special | ⚠️ 修 collation 后接通 |
| `.budget_summary` | portfolio_constraints | summary（收 portfolio_summary_json） | ⚠️ 改结构 |

---

## 9. 读取侧（合并前端 ViewModel）

前端只认统一 ViewModel，**字段差异/拆列/枚举翻译全在后端 mapper 消化**：

- 拆列/拆 JSON（direction 三列、ai_suggest 各 5 列、summary portfolio 8 列）→ mapper 组装回结构对象。
- 枚举（库英文码 → 前端中文 / 实时端枚举）→ mapper 按 §4 权威表翻译。
- 纯渲染派生字段（卡片颜色 klass、箭头）→ **前端现算，不入库**。
- 快照轨字段缺失（如某 perf 未存）→ 前端**按字段有无降级**，不按来源分支。

---

## 10. 完整迁移 DDL（节选骨架，按彻底重构出，不写兼容层）

```sql
-- ========== A. 全局 collation / 主键 / 审计 统一 ==========
-- A1: synthesis 三表 collation 对齐（否则 JOIN card 报错）
ALTER TABLE t_advert_agent_modify_suggest_reason_group        CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;
ALTER TABLE t_advert_agent_modify_suggest_reason_group_member CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;
ALTER TABLE t_advert_agent_modify_suggest_special             CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;
-- A2: synthesis 三表 id/关联键 varchar(36) → varchar(32)
ALTER TABLE t_advert_agent_modify_suggest_reason_group
  MODIFY id varchar(32) NOT NULL, MODIFY decision_id varchar(32) NOT NULL;
ALTER TABLE t_advert_agent_modify_suggest_reason_group_member
  MODIFY id varchar(32) NOT NULL, MODIFY group_id varchar(32) NOT NULL, MODIFY suggest_card_id varchar(32) NOT NULL;
ALTER TABLE t_advert_agent_modify_suggest_special
  MODIFY id varchar(32) NOT NULL, MODIFY decision_id varchar(32) NOT NULL;

-- ========== B. card 表（新增活动 + 缺列 + 注释 + 约束） ==========
ALTER TABLE t_advert_agent_modify_suggest_card
  MODIFY campaign_id varchar(64) NULL COMMENT '广告活动ID(新增活动为空,创建后回填)',
  MODIFY suggest_category varchar(32) COMMENT 'ELIMINATE/ADJUST/KEEP/CREATE',
  MODIFY campaign_group_type varchar(32) COMMENT '组合:exact_core_group/auto_broad_group/exact_testing_group/low_bid_retention_group',
  MODIFY keyword varchar(512) COMMENT '关键词文本',
  MODIFY keyword_match_type varchar(32) COMMENT '匹配类型 EXACT/PHRASE/BROAD',
  ADD COLUMN keyword_class varchar(32) NULL COMMENT 'KB06:generic/long_tail/competitor/brand/custom',
  ADD COLUMN review_level varchar(32) NULL COMMENT 'AUTO_BATCHABLE/MANUAL_REVIEW/SENIOR_APPROVAL',
  ADD COLUMN is_core tinyint(1) DEFAULT 0 COMMENT 'Custom核心词保护',
  ADD COLUMN perf_json text NULL COMMENT '逐活动7天指标 {cost,acos,cvr,cpc,ctr,clicks,orders,impressions}',
  ADD COLUMN is_prefiltered tinyint(1) DEFAULT 0 COMMENT '预过滤(淘汰池/多词,不可操作)',
  ADD COLUMN prefilter_reason varchar(255) NULL COMMENT '预过滤原因',
  ADD UNIQUE KEY uk_card_decision_name (decision_id, campaign_name);

-- ========== C. summary（新增统计 + portfolio 收 JSON） ==========
ALTER TABLE t_advert_agent_modify_suggest_summary
  ADD COLUMN create_count int DEFAULT 0 COMMENT '新增活动数',
  ADD COLUMN portfolio_summary_json text NULL COMMENT '4组 {组:{count,budget}} + create';
-- （旧 8 列保留过渡或一并 DROP，视前端切换节奏）

-- ========== D. placement record 枚举统一 + 精度 ==========
UPDATE t_advert_agent_modify_placement_record SET placement_type='TOP_OF_SEARCH' WHERE placement_type='TOP';
UPDATE t_advert_agent_modify_placement_record SET placement_type='REST_OF_SEARCH' WHERE placement_type='REST';
ALTER TABLE t_advert_agent_modify_placement_record
  MODIFY old_percent decimal(18,4), MODIFY new_percent decimal(18,4);

-- ========== E. 前置工作流类型/数据清洗 ==========
ALTER TABLE t_advert_agent_decision
  MODIFY target_acos_suggest int COMMENT '目标ACOS推荐(%)',
  MODIFY daily_budget_suggest decimal(12,2) COMMENT '每日预算推荐',
  ADD COLUMN product_name varchar(200) NULL COMMENT '产品名(parent_asin 存中文名时迁移至此)',
  ADD COLUMN is_latest tinyint(1) DEFAULT 1 COMMENT '该ASIN最新决策',
  ADD COLUMN analysis_mode varchar(16) DEFAULT 'REALTIME' COMMENT 'REALTIME/SCHEDULED';
ALTER TABLE t_advert_agent_ai_suggest
  MODIFY suggest_acos int, MODIFY suggest_budget decimal(12,2);
ALTER TABLE t_advert_agent_purpose_score MODIFY score int;
DELETE FROM t_advert_agent_purpose_score WHERE advert_purpose='Clearance';  -- KB 已废除
ALTER TABLE t_advert_agent_decision_config
  ADD COLUMN enabled tinyint(1) DEFAULT 1 COMMENT '是否参与定时分析',
  ADD COLUMN frequency varchar(16) DEFAULT 'DAILY' COMMENT '调度频率',
  ADD COLUMN last_run_time datetime NULL,
  ADD COLUMN last_decision_id varchar(32) NULL;

-- ========== F. direction_recommend 三 JSON 列收一列（重构，需迁数据） ==========
-- ALTER TABLE t_advert_agent_direction_recommend ADD COLUMN content_json text COMMENT '{basis,conclusion,data_focus}';
-- 迁移脚本: 读三列 → 组装 JSON → 写 content_json → DROP 旧三列

-- ========== G. data_metrics day_str 补年份（写入端改 yyyy-MM-dd） ==========
```

> 完整可执行迁移脚本（含数据迁移 F、审计字段 A3 批量 MODIFY、portfolio 旧列下线）建议落到 `scripts/erp_db/migrate_clean_v2.sql`，分批执行 + 每步备份。

---

## 11. 优先级与执行顺序

| 阶段 | 内容 | 为什么这个顺序 |
|---|---|---|
| **第 1 步** | A1/A2 synthesis collation+长度 | synthesis 一落库做 JOIN 就报错，最高优先 |
| **第 2 步** | B card（CREATE/campaign_id/缺列/注释）+ C summary | 新增活动 + 预过滤 + 缺口落库的核心 |
| **第 3 步** | §4 枚举统一（placement record / direction 注释 / Clearance 清洗 / recommend 统一） | 前端 mapper 依赖枚举权威 |
| **第 4 步** | A3/A4 审计+decimal 全局统一 | 规范，批量 ALTER |
| **第 5 步** | E 前置工作流类型/调度字段 | 定时分析 + 数值类型 |
| **第 6 步** | F/G 拆列收 JSON、day_str 年份 | 需迁数据，最后做 |
| **确认项** | core_keyword 废弃？portfolio_record 废弃？parent_asin 中文成因？ | 需后端/业务确认 |

---

## 附：连接脚本（供 DBA 复用）

```python
import pymysql, ssl
ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
conn = pymysql.connect(host='192.168.2.51', port=3306, user='erp_agentadvert',
    password='erp_agentadvert#weilai123', database='erp_agentadvert',
    charset='utf8mb4', ssl=ctx)   # ← caching_sha2_password 必须走 TLS
```

---

*基于 2026-06-11 真库 DDL + 索引 + 21 表行数 + 枚举采样实测产出。所有"现状"列均来自真实数据，非推测。*
