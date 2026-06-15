# ERP 测试库 vs 正式库 结构对比

> **日期**: 2026-06-15
> **测试库**: `erp_agentadvert@192.168.2.51:3306`，MySQL **8.0.46**，**实时 dump**（26 张 `t_advert_agent_*` 表）
> **正式库**: 以 `D:/cor_wkplace/AD_assistant_agent-v3.0.2/cursor临时文件/ERP正式数据库表结构文档.md` 为基准（18 张表）
> **连接注意**: 测试库需 TLS（caching_sha2_password，`ssl=CERT_NONE` 即可）；正式库本机不可达，本对比基于文档而非实时 dump

---

## 一、总览

| 维度 | 测试库 | 正式库（文档） | 差额 |
|---|---:|---:|---:|
| `t_advert_agent_*` 表 | **26** | 18 | **+8** |
| sql_mode | `STRICT_TRANS_TABLES,...`（严格） | （未知，按 MySQL 默认推断） | — |

**测试库 = 正式库结构 + `migrate_v2_clean.sql` 9 阶段改造 + 8 张新增执行专用表**。

> ⚠️ **文档不全的提示**：改造方案（2026-06-11）记载正式库实际有 **21 张表**，文档漏记了 `modify_suggest_reason_group / _member / modify_suggest_special` 三张 synthesis 表。本对比里这 3 张算"测试库独有"，但**实际可能正式库也有**——需连正式库 dump 一次校准。

---

## 二、共同表的差异（11 张表，按 migrate_v2_clean.sql 改造）

### 2.1 前置工作流（6 张）

| 表 | 新增列 | 类型变更（旧 → 新） |
|---|---|---|
| `t_advert_agent_decision` | `product_name`、**`is_latest`**、**`analysis_mode`** | `target_acos_suggest` varchar(500) → **int**；`daily_budget_suggest` → **decimal(12,2)** |
| `t_advert_agent_decision_config` | **`enabled`**、**`frequency`**、**`last_run_time`**、**`last_decision_id`**（驱动定时分析） | 同 decision 两列 |
| `t_advert_agent_ai_suggest` | — | `suggest_acos` varchar(50) → **int**；`suggest_budget` → **decimal(12,2)** |
| `t_advert_agent_purpose_score` | — | `score` varchar(20) → **int** |
| `t_advert_agent_modify_suggest_card` | `keyword_class`、`review_level`、`is_core`、`perf_json`、`is_prefiltered`、`prefilter_reason` | — |
| `t_advert_agent_modify_suggest_summary` | `analysis_overview`、`create_count` | — |

### 2.2 执行回写表（5 张 `_record`）— 共同改动

**全部 5 张表（advert/campaign/keyword/placement/portfolio）共同变化**：
- **删除列**：`version`
- **类型变更**：
  - `create_by` / `editor_by`：varchar(64) → **int**
  - `creator_id` / `editor_id`：varchar(32) → **bigint**
  - `risk_check_pass`：tinyint(4) → **int**
  - 数值精度加宽：`old/new_budget` decimal(12,2) → **decimal(18,4)**；`old/new_percent` decimal(8,2) → **decimal(10,4)**；`old/new_bid` decimal(12,2) → **decimal(18,4)**

**额外**：
- 4 张子表（campaign/keyword/placement/portfolio）**新增** `shop_id`、`parent_asin`、`parent_seller_sku`、`retried`、`retry_record_id`
- `modify_advert_record` **新增** `retry_from_record_id`、`retry_from_record_type`、`skip_risk_check`

### 2.3 pending 表（3 张）

| 表 | 差异 |
|---|---|
| `modify_keyword_pending` / `_campaign_pending` / `_placement_pending` | **结构与文档一致**（confirm/execute 双状态机），无列变更 |

---

## 三、测试库独有的 8 张表（文档无）

### 3.1 ★ MCP 工具专用执行表（5 张）—— 对接广告调整 MCP

| 表 | 行数 | 对应 MCP 工具 | 关键列 |
|---|---:|---|---|
| `t_advert_agent_create_advert_record` | 14 | `agent_create_portfolio_campaign`（新建活动） | `create_portfolio_result/msg`、`create_result/msg`、`campaign_id/_name`（创建后回填）、`budget/bid/position_percent`、`request/response_params_json` |
| `t_advert_agent_create_keyword_record` | 2 | `agent_create_negative_keywords`（否定词/关键词） | `keyword_type`、`campaign_id`、`keyword/match_type/bid`、`create_result/msg` |
| `t_advert_agent_keyword_suggest_bid_record` | 3 | `agent_query_keyword_suggest_bid`（建议竞价查询） | `request/response_params_json`、`error_msg` |
| `t_advert_agent_modify_target_record` | 0 | 商品/ASIN 投放(target)修改 | `target_id`、`old/new_bid`、`old/new_state`、`modify_result/error_msg`、`risk_check_pass` |
| `t_advert_agent_switch_keyword_record` | 0 | 关键词切换（修改 match_type） | `old_keyword_id → new_keyword_id`、`old/new_match_type`、`create_result/msg` |

**共性**：全部含 `shop_id`、`parent_asin`、`parent_seller_sku`、`current_user_id`、`request_params_json`、`response_params_json`、审计四件套（int/bigint）。

### 3.2 ★ Synthesis 分组叙事（3 张）—— ⚠️ 实际可能正式库也有

| 表 | 用途 |
|---|---|
| `t_advert_agent_modify_suggest_reason_group` | AI 汇总叙事的分组主表 |
| `t_advert_agent_modify_suggest_reason_group_member` | 分组 → suggest_card 多对多 |
| `t_advert_agent_modify_suggest_special` | 特殊调整说明 |

> 改造方案 §阶段1 表明这 3 张表 **原本就存在**，改造仅做了 collation 归一（`utf8mb4_bin → utf8mb4_general_ci`）+ id 长度调整（`varchar(36) → varchar(32)`）。文档很可能漏记。

---

## 四、关键迁移影响（推生产时要做的）

| 类别 | 动作 |
|---|---|
| **DDL 迁移** | 跑 `scripts/erp_db/migrate_v2_clean.sql`（11 表的列/类型改动） |
| **新表迁移** | 8 张表（5 MCP 执行专用 + 3 synthesis）需 `CREATE TABLE`；synthesis 若已有则跳过、跑 collation 修复 |
| **数据回填** | `decision.is_latest` 需按窗口函数回填（改造方案已附 SQL）；`is_latest` 默认 0；`finalize_batch` 维护 |
| **数据清洗** | `purpose_score.advert_purpose='Clearance'` 2 行需删除（KB 已废弃） |
| **写入端兼容** | 现 `repository.py` 写入需注意：①类型收窄列（int/decimal）不能传空串；②审计列要数值；③`version` 列已删（INSERT 不能含）；④record 表已按 MCP 工具分流（5 张专用），写入路径应按工具区分（不再一张 modify 通吃） |

---

## 五、连接示例

```python
import pymysql, ssl
ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
conn = pymysql.connect(
    host='192.168.2.51', port=3306,
    user='erp_agentadvert', password='erp_agentadvert#weilai123',
    database='erp_agentadvert', charset='utf8mb4', ssl=ctx,
)
```

代码内开关：`settings.erp_use_tls=True`（本机/VPN 必须；服务器同内网可关）。

---

## 六、待办

- [ ] **连正式库实时 dump**，校准本对比里"测试库独有 8 张"的实际差额（尤其 synthesis 三表）。
- [ ] 推生产前再核一次 sql_mode（生产侧若严格 → 写入端类型收窄缺陷必须先修；非严格则静默转 0，仍要修但不阻塞跑通）。
- [ ] 跑 `SHOW CREATE TABLE` 而非 `SHOW COLUMNS` 把 charset/collation/索引/外键也覆盖（本对比仅列名+类型）。

---

*最后更新：2026-06-15 · 基于测试库实时 dump + 正式库结构文档（2026-06-09 版）*
