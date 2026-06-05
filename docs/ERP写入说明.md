# Agent → ERP 数据库写入说明

> **版本**: v1.0 · **日期**: 2026-06-03  
> **代码分支**: `feature/erp-writer`  
> **关联文档**: [ERP数据库接入文档1.md](../ERP数据库接入文档1.md)（pending 基础表）、[ERP数据库接入文档2_campaign.md](../ERP数据库接入文档2_campaign.md)（分析建议 Tab）

---

## 1. 概述

本模块将 Agent 产出的 **Campaign 分析 JSON**（及可选的 **四层向导 JSON**）写入 ERP MySQL，供 WHP「分析建议」等页面只读展示；运营在 ERP 侧做「同意/忽略」与 Amazon API 执行，**不由本模块执行改价/改预算**。

| 项 | 说明 |
|----|------|
| 目标库 | `erp_agentadvert`（测试环境默认 `192.168.2.51:3306`） |
| 驱动 | PyMySQL，手写 SQL，`INSERT ... ON DUPLICATE KEY UPDATE` |
| 触发方式 | **命令行脚本**；可选 **`POST /campaign/analyze`** 成功后自动 `write_full`（`ERP_AUTO_WRITE=true` 或请求体 `write_erp: true`） |
| 数仓依赖 | 写入前需 Doris 提供真实 `campaign_id` / `keyword_id`（见 §5） |

---

## 2. 代码位置

```
ad-direction-agent/
├── app/persistence/erp_writer/          # 核心包
│   ├── __init__.py                    # 导出 ErpDualWriterRepository、canonicalize_payload
│   ├── repository.py                  # 连接 ERP、事务、INSERT/DELETE
│   ├── mappers.py                     # JSON → CanonicalRun
│   ├── models.py                      # 数据结构、stable_id / warehouse_pending_id
│   ├── text_utils.py                  # 枚举与 ERP 字段映射
│   ├── auto_push.py                   # analyze → write_full（API / 脚本共用）
│   └── listing_context.py             # shop_id、parent_seller_sku、site_code 解析
└── scripts/erp_db/                    # ★ 所有 ERP 库相关脚本（写入 + 校验 + schema）
    ├── README.md                      # 脚本索引
    ├── write_*.py / run_erp_batch_*   # 写入
    ├── verify_* / audit_* / db_health_check.py  # 校验审计
    ├── dump_erp_* / gen_erp_db_summary_md.py    # 结构导出与文档
    ├── resolve_listing_context.py / query_erp_empty_asins.py
    └── _bootstrap.py / _erp_conn.py   # 路径与默认连接
```

Git 独立分支：`feature/erp-writer`（仅含上述 ERP 写入相关文件）。

---

## 3. 数据流

```
Campaign KB JSONL（campaign_kb_experiment 等产出）
        │
        ├─ resolve_listing_context(asin)     ← Doris / ERP summary 兜底
        │
        ▼
canonicalize_payload(kb_json)              ← mappers.py
        │
        ▼
CanonicalRun（decision_id、cards、pending、metrics…）
        │
        ├─ write_dual()     ← 分析建议 + legacy 方向 + metrics
        └─ write_full()     ← 上者 + decision 主表 + 向导表（需 wizard JSONL）
        │
        ▼
ERP MySQL（单事务 commit / 失败 rollback）
```

### 3.1 `decision_id` 与主键

- **`decision_id`**: `stable_id("dec", parent_asin, experiment_id, run_number)`（MD5 派生，同一实验可复现）。
- **`batch_no`**: `{experiment_id}-{run_number:02d}`。
- **summary / card**: `stable_id("sum"|"car", decision_id, …)`。
- **pending 行**: `warehouse_pending_id("mkp"|"mcp"|"mpl", decision_id, campaign_id, …)`，优先绑定数仓 ID。

重跑同一 `decision_id` 时，会先 **DELETE** 该决策下的 card 与三类 pending，再重新 INSERT，避免旧哈希 ID 残留。

---

## 4. 两种写入模式

### 4.1 `write_dual()` — Campaign 分析建议（常用）

**入口**: `ErpDualWriterRepository.write_dual(run)`  
**脚本**: `scripts/erp_db/write_erp_from_jsonl.py`

| 顺序 | 表 | 说明 |
|------|-----|------|
| 1 | `t_advert_agent_direction_recommend` | Legacy 方向汇总（可选 `include_legacy_direction=False` 跳过） |
| 2 | `t_advert_agent_direction_recommend_detail` | Legacy 方向明细 |
| 3 | `t_advert_agent_data_metrics` | 指标快照 |
| 4 | `t_advert_agent_modify_suggest_summary` | 分析概览（总数/淘汰/调整/置信/预算影响） |
| 5 | DELETE | `keyword_pending`、`campaign_pending`、`placement_pending`、`suggest_card`（同 decision_id） |
| 6 | `t_advert_agent_modify_suggest_card` | 1 campaign = 1 卡 |
| 7 | `t_advert_agent_modify_keyword_pending` | Bid/State |
| 8 | `t_advert_agent_modify_campaign_pending` | Budget/State |
| 9 | `t_advert_agent_modify_placement_pending` | 广告位加价（最多 3 行） |

对应 ERP 文档 2「分析建议 Tab」；pending 子表结构同文档 1。

### 4.2 `write_full()` — 全量（Campaign + 四层向导）

**入口**: `ErpDualWriterRepository.write_full(run, wizard_payload=..., decision_meta=...)`  
**脚本**: `scripts/erp_db/write_erp_all.py`

在 `write_dual` 同等表基础上 **增加**（不重复写 legacy direction recommend，由 wizard 方向表承担）：

| 表 | 说明 |
|----|------|
| `t_advert_agent_decision` | 决策主表 |
| `t_advert_agent_decision_config` | 决策配置快照 |
| `t_advert_agent_purpose_score` | 广告目的评分（wizard） |
| `t_advert_agent_core_keyword_tracking` | 核心词跟踪（wizard） |
| `t_advert_agent_ai_suggest` | AI 建议文案（wizard） |
| `t_advert_agent_direction_recommend` / `_detail` | 向导方向推荐（`_upsert_wizard_direction`） |

**方向推荐字段格式**（WHP 对接必读）：

- [广告方向推荐-ERP存储格式说明.md](./广告方向推荐-ERP存储格式说明.md) — 表/字段/内容、改后 `string[]` 契约、B0CGH9 样例  
- [WHP-Tab4渲染与组合枚举说明.md](./WHP-Tab4渲染与组合枚举说明.md) — Tab4 渲染问题与组合枚举  
- [ERP方向枚举对照.md](./ERP方向枚举对照.md) — `direction_type` / `advert_direction_types` 统一码表  

`direction_recommend_detail.content_json` 为 **JSON 字符串数组**（`["句1","句2"]`），由 `direction.reason` 按 `;` / `；` 拆分；标题/分数/状态用列 `direction_type`、`suggest_score`、`recommend_tag`。

### 4.3 未实现的表

- `t_advert_agent_modify_portfolio_pending`（组合预算待确认）— 当前代码 **无写入**。

---

## 5. 输入数据要求

### 5.1 Campaign KB JSONL（`write_dual` / `write_full` 必需）

典型字段（单行 JSON）：

| 字段 | 必填 | 说明 |
|------|------|------|
| `parent_asin` | 是 | 父 ASIN |
| `experiment_id` / `run_number` | 建议 | 决定 decision_id、batch_no |
| `summary` | 是 | `to_eliminate` / `to_adjust` / `to_keep`、置信计数、`estimated_budget_impact` 等 |
| `adjustments[]` | 是 | 每条建议；**必须含 Doris `campaign_id`**，否则该条跳过 |
| `adjustments[].keyword_id` | 建议 | 写 `keyword_pending`；缺则无法调 Amazon 关键词 API |
| `adjustments[].campaign_name` / `child_asin` / `match_type` / `keyword_text` | 是 | 卡片展示与分组 |
| `adjustments[].action` | 是 | `eliminate` / `adjust` / `keep` → `suggest_category` |
| `adjustments[].ai_portfolio_class` | 建议 | 组合标签 → card.`campaign_group_type`（`campaignGroupType`）：精准主力组/`exact_core_group`、精准测试组/`exact_testing_group`、自动广泛组/`auto_broad_group`、低价捡漏组/`low_bid_retention_group` |
| `adjustments[].bid_change` / `budget_change` / `placement_changes` | 视情况 | 驱动三类 pending |
| `shop_id` | 可脚本注入 | `write_erp_all` 通过 `resolve_listing_context` 写入 |

`canonicalize_payload` 按 **`campaign_id` 分组**：同一活动多条 adjustment 合并为一张 card + 多条 pending。

### 5.2 Wizard JSONL（仅 `write_full`）

由 `wizard_kb_experiment` 等产出，需包含 `decision_meta`（产品阶段、广告目的、方向等）、`long_term_config`、`p3`、`target_scores` 等；`write_erp_all` 会把 `long_term_config` 合并进 `decision_meta`。

### 5.3 Listing 上下文

`listing_context.resolve_listing_context(asin)` 解析顺序：

1. Doris（`mcp_db_context`）
2. ERP `t_advert_agent_modify_suggest_summary` 最近一条
3. 代码内 `_KNOWN_LISTINGS` 硬编码兜底

---

## 6. 使用方法

### 6.1 仅 Campaign（dual）

```powershell
cd ad-direction-agent

python scripts/erp_db/write_erp_from_jsonl.py `
  --input ..\cursor临时文件\B0CGH9QRKK_kb.jsonl `
  --report ..\cursor临时文件\B0CGH9QRKK_write_report.json `
  --shop-id 1622 `
  --parent-seller-sku "你的父SKU" `
  --site-code Amazon_US
```

> 默认连接 `scripts/erp_db/_erp_conn.py`（ERP 测试库）；可用 `--host` 等覆盖。

### 6.2 Campaign + 向导（full）

```powershell
python scripts/erp_db/write_erp_all.py `
  --asin B0CGH9QRKK `
  --kb ..\cursor临时文件\B0CGH9QRKK_kb.jsonl `
  --wizard ..\cursor临时文件\B0CGH9QRKK_wizard.jsonl `
  --report ..\cursor临时文件\B0CGH9QRKK_write_full.json
```

默认 ERP 连接见 `scripts/erp_db/_erp_conn.py`。

### 6.3 批量

```powershell
python scripts/erp_db/run_erp_batch_sequential.py --asins B0CGH9QRKK,B0B7S3PWWB
```

按 ASIN 依次跑实验 → 生成 kb/wizard → `write_erp_all`。

### 6.4 在线接口自动写入（analyze 完成后）

在 `ad-direction-agent/.env` 中开启：

```env
ERP_AUTO_WRITE=true
ERP_HOST=192.168.2.51
ERP_DATABASE=erp_agentadvert
```

或单次请求强制写入：

```http
POST /api/v1/agent/ad-direction/campaign/analyze
Content-Type: application/json

{
  "asin": "B0CGH9QRKK",
  "days": 7,
  "refresh": true,
  "write_erp": true
}
```

**门禁**（全部满足才写库）：`sanity_check_passed=true`、存在 `adjustments`、至少一条含 `campaign_id`。ERP 失败不导致 analyze 返回 500，错误在响应字段 `erp_write` 中返回。

**响应扩展字段** `erp_write` 示例：

```json
{
  "erp_write": {
    "attempted": true,
    "ok": true,
    "decision_id": "dec...",
    "wizard_partial": false,
    "modern_card": 12
  }
}
```

实现代码：`app/persistence/erp_writer/auto_push.py`，挂接于 `app/api/campaign.py`。

---

## 7. 写入后状态字段

推送时固定：

| 字段 | 值 |
|------|-----|
| `confirm_status` | `PENDING` |
| `execute_status` | `PENDING` |

用户「同意/忽略」与执行结果由 **WHP/ERP 后端** 更新，本模块不修改。

---

## 8. 校验与排错

| 脚本 | 用途 |
|------|------|
| `scripts/erp_db/verify_erp_write.py` | 对照 JSONL 预期行数 |
| `scripts/erp_db/verify_erp_full_write.py` | 全量 18 表覆盖检查 |
| `scripts/erp_db/audit_latest_writes.py` | 最近写入审计 |
| `scripts/erp_db/db_health_check.py` | 结构健康检查 |
| `scripts/diag_campaign_asin.py` | Doris 侧 campaign_id/keyword_id（非 ERP，仍在 scripts/） |

常见问题：

1. **card 数量为 0**：`adjustments` 缺 `campaign_id` → 查数仓或 Campaign 分析是否带上 Doris 上下文。  
2. **summary 与 card 数不一致**：mapper 会写 `alert_msg` 警告，以 `eliminate+adjust+keep` 为准修正 `total_count`。  
3. **连错库**：确认连接 `erp_agentadvert`，不是 docker `ad_agent_state`（3307）。

---

## 9. 与在线服务的关系

| 组件 | 是否写 ERP |
|------|------------|
| FastAPI `app/api/*` | **否** |
| `app/workflow/steps/campaign.py` | 只分析 + Redis 缓存 |
| `app/persistence/mysql_state_manager.py` | 只写本地 Agent 状态库（3307） |
| `scripts/erp_db/*.py` | **是**（当前唯一入口） |

后续若接入 API，建议在 Campaign 分析完成或批处理任务中调用 `canonicalize_payload` + `write_dual` / `write_full`。

---

## 10. 表与 ERP 文档对照

| 本文 write 模式 | ERP 文档 |
|----------------|----------|
| summary + card + 3×pending | 文档 2 §分析建议 Tab |
| keyword/campaign/placement pending 字段 | 文档 2 §4.3–4.5（v3 无 `modify_sub_type`） |
| pending 审批/执行流 | 文档 1 §公共字段 |

---

## 11. 变更记录

| 日期 | 说明 |
|------|------|
| 2026-06-03 | 初版：梳理 `erp_writer` 包、双写/全量模式、脚本用法与分支 `feature/erp-writer` |
