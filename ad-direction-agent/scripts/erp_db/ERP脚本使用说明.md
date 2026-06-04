# ERP 数据库脚本使用说明

> **适用分支**: chenv3.0  
> **目标库**: `erp_agentadvert` @ `192.168.2.51:3306`  
> **代码库**: `app/persistence/erp_writer/`  
> **最后更新**: 2026-06-04

---

## 1. 架构概览

```
┌─────────────────────────────────────────────────────────┐
│                    Agent 产出数据                         │
├───────────────────┬─────────────────────────────────────┤
│ Campaign 分析结果  │  四层向导 (wizard)                    │
│ (campaign_kb_     │  (wizard_kb_experiment.py)           │
│  experiment.py)   │                                     │
│   ↓               │   ↓                                 │
│ {asin}_kb.jsonl   │  {asin}_wizard.jsonl                │
└──────┬────────────┴──────┬──────────────────────────────┘
       │                   │
       ▼                   ▼
┌──────────────────────────────────────────────┐
│            erp_writer 库 (Python)             │
│  canonicalize_payload() → CanonicalRun       │
│  ErpDualWriterRepository.write_dual()         │
│  ErpDualWriterRepository.write_full()         │
└──────────────────┬───────────────────────────┘
                   │
                   ▼
┌──────────────────────────────────────────────┐
│            ERP MySQL (18 张表)                │
│  t_advert_agent_decision                     │
│  t_advert_agent_modify_suggest_summary        │
│  t_advert_agent_modify_suggest_card           │
│  t_advert_agent_modify_keyword_pending        │
│  t_advert_agent_modify_campaign_pending       │
│  t_advert_agent_modify_placement_pending      │
│  ... (共 18 张)                              │
└──────────────────────────────────────────────┘
```

**两层写入模式：**

| 模式 | 方法 | 写入内容 | 需要哪些数据 |
|------|------|---------|------------|
| **dual** | `write_dual()` | modify_suggest 5 张表 + data_metrics + legacy 方向推荐 | 仅 Campaign KB JSONL |
| **full** | `write_full()` | dual 全部 + decision 主表 + decision_config + wizard 关联表 | Campaign KB JSONL + Wizard JSONL |

**dual 模式**：轻量，只需 Campaign 分析结果，覆盖前端「分析建议」Tab 全部数据。  
**full 模式**：完整链路，额外写入 decision 主表、向导推荐表等，适合 "一次决策" 归档。

---

## 2. 输入格式：Campaign KB JSONL

`campaign_kb_experiment.py` 产出的 JSONL 文件，每行一个 JSON 对象，固定字段：

```json
{
  "parent_asin": "B0CGH9QRKK",
  "experiment_id": "20260604-1530",
  "run_number": 1,
  "temperature": 0.0,
  "timestamp": "2026-06-04T15:30:00Z",
  "batch_no": "20260604153000",
  "total_campaigns": 56,
  "sanity_check_passed": true,
  "summary": {
    "to_eliminate": 12,
    "to_adjust": 30,
    "to_keep": 14,
    "confidence_high": 20,
    "confidence_medium": 25,
    "confidence_low": 11,
    "estimated_budget_impact": -45.30
  },
  "adjustments": [
    {
      "campaign_name": "精准-example keyword",
      "campaign_key": "精准-example keyword × B09SGC3YZB",
      "child_asin": "B09SGC3YZB",
      "keyword_text": "example keyword",
      "match_type": "EXACT",
      "action": "eliminate_to_low_bid_pool",
      "direction": {"bid": "down", "budget": "down"},
      "triggered_rule": "NO_CVR_HIGH_SPEND",
      "reason": "(1) 近7天花费$0,无点击无订单...",
      "evidence": ["7天花费$0", "曝光3次，点击0"],
      "confidence": "low",
      "current_budget": 5.0,
      "proposed_budget": 1.0,
      "current_bid": 0.85,
      "proposed_bid": 0.20,
      "placement_adjustments": [
        {"placement": "头部", "current_pct": 10, "proposed_pct": 0, "action": "下调"}
      ],
      "negative_keywords": [
        {"keyword": "irrelevant term", "clicks_7d": 5, "orders_7d": 0, "reason": "不相关高点击"}
      ],
      "ai_portfolio_class": "淘汰",
      "review_level": "MANUAL_REVIEW"
    }
  ],
  "warnings": ["广泛流搜索词报告为空,否词不可用"],
  "budget_summary": {
    "target_budget": 200.0,
    "target_budget_source": "override",
    "portfolio_constraints": {"主推": 120.0, "广泛/自动": 40.0, "测试/新增": 40.0}
  },
  "keyword_analysis": {},
  "campaign_group_type": null
}
```

**关键字段说明：**

| 字段 | 必填 | 说明 |
|------|------|------|
| `parent_asin` | Y | 父 ASIN |
| `adjustments[]` | Y | 每个活动一张卡片 |
| `adjustments[].action` | Y | `eliminate_to_low_bid_pool` / `adjust_bid` / `adjust_budget` / `adjust_placement` / `keep` |
| `adjustments[].ai_portfolio_class` | Y | `主推` / `广泛/自动` / `测试/新增` / `淘汰` |
| `adjustments[].campaign_key` | Y | `活动名 × 子ASIN` 唯一标识 |
| `summary` | Y | 汇总统计 |
| `budget_summary.portfolio_constraints` | N | 3 组预算约束（缺则用默认） |

---

## 3. 脚本清单

### 3.1 写入脚本

| 脚本 | 功能 | 写哪些表 | 输入 |
|------|------|---------|------|
| `write_erp_from_jsonl.py` | **dual 写入**：批量写入分析建议 | modify_suggest 5 表 + data_metrics + legacy 方向 | Campaign KB JSONL |
| `write_erp_all.py` | **full 写入**：单 ASIN 完整链路 | dual 全部 + decision + decision_config + wizard 表 | KB JSONL + Wizard JSONL |
| `run_erp_batch_sequential.py` | **批量编排**：串行跑 Campaign 实验 → Wizard → ERP 写入 | full 全部 | ASIN 列表 |

#### `write_erp_from_jsonl.py` — dual 写入（最常用）

```powershell
cd ad-direction-agent

# 单 ASIN
python scripts/erp_db/write_erp_from_jsonl.py `
  --input cursor临时文件\B0CGH9QRKK_kb.jsonl `
  --report cursor临时文件\B0CGH9QRKK_write_report.json `
  --shop-id 1622 `
  --parent-seller-sku "WY03235-80D新款" `
  --site-code "Amazon_US"
```

| 参数 | 说明 |
|------|------|
| `--input` | Campaign KB JSONL 路径 |
| `--report` | 写入报告 JSON 输出路径 |
| `--shop-id` | 店铺 ID（可选，不传则用 listing_context 自动解析） |
| `--parent-seller-sku` | 父 SKU（可选） |
| `--site-code` | 站点代码（可选） |

**报告输出示例：**
```json
[
  {
    "decision_id": "dec5da0a62fe024fa952815cd44be083",
    "modern_summary": 1,
    "modern_card": 56,
    "modern_keyword_pending": 18,
    "modern_campaign_pending": 30,
    "modern_placement_pending": 9,
    "legacy_recommend": 1,
    "legacy_detail": 4,
    "legacy_metrics": 1
  }
]
```

#### `write_erp_all.py` — full 写入

```powershell
cd ad-direction-agent

python scripts/erp_db/write_erp_all.py `
  --asin B0CGH9QRKK `
  --kb cursor临时文件\B0CGH9QRKK_kb.jsonl `
  --wizard cursor临时文件\B0CGH9QRKK_wizard.jsonl `
  --report cursor临时文件\B0CGH9QRKK_full_report.json
```

| 参数 | 说明 |
|------|------|
| `--asin` | 父 ASIN（用于 listing 上下文解析） |
| `--kb` | Campaign KB JSONL |
| `--wizard` | Wizard JSONL（可选，不传则跳过向导表） |
| `--report` | 报告输出路径 |

#### `run_erp_batch_sequential.py` — 全自动批量编排

```powershell
cd ad-direction-agent

# 完整流程：预设战略 → Campaign 实验 → Wizard 实验 → ERP full 写入
python scripts/erp_db/run_erp_batch_sequential.py `
  --asins "B0B7S3PWWB,B0CGH9QRKK,B0DNZNWGX5"

# 仅写 ERP（跳过 Campaign/Wizard，要求 kb.jsonl 和 wizard.jsonl 已存在）
python scripts/erp_db/run_erp_batch_sequential.py `
  --asins "B0B7S3PWWB,B0CGH9QRKK" `
  --erp-only

# 跳过 Campaign（kb.jsonl 已有）
python scripts/erp_db/run_erp_batch_sequential.py `
  --asins "B0B7S3PWWB" `
  --skip-campaign
```

| 参数 | 说明 |
|------|------|
| `--asins` | 逗号分隔的父 ASIN 列表 |
| `--output-dir` | 中间文件目录（默认 `cursor临时文件/`） |
| `--skip-campaign` | 跳过 Campaign KB 实验（已有 kb.jsonl） |
| `--skip-wizard` | 跳过 Wizard 实验（已有 wizard.jsonl） |
| `--skip-preset` | 跳过战略预设 |
| `--erp-only` | 只写 ERP（kb+wizard 必须已存在） |

### 3.2 校验 / 审计脚本

| 脚本 | 功能 | 示例 |
|------|------|------|
| `verify_erp_write.py` | 对照 JSONL 预期行数 vs 库内实际（dual 写入后） | `python scripts/erp_db/verify_erp_write.py --input kb.jsonl --decision-id decxxx` |
| `verify_erp_full_write.py` | 按 `decision_id` 检查 full 模式 9 表是否齐全 | `python scripts/erp_db/verify_erp_full_write.py --decision-id decxxx --expected-cards 56` |
| `verify_keyword_columns.py` | 检查 card 表 `keyword`/`keyword_match_type` 语义是否正确 | `python scripts/erp_db/verify_keyword_columns.py` |
| `audit_latest_writes.py` | 指定 decision 的完整数据审计 | `python scripts/erp_db/audit_latest_writes.py --decision-id decxxx` |
| `audit_all_erp_tables.py` | 18 表总行数 + 按 decision 分组统计 | `python scripts/erp_db/audit_all_erp_tables.py` |
| `audit_data_metrics.py` | metrics 表空值与 KB 源数据对照 | `python scripts/erp_db/audit_data_metrics.py` |
| `db_health_check.py` | 结构健康：孤儿行、枚举值、summary 一致性 | `python scripts/erp_db/db_health_check.py` |

### 3.3 Schema / 文档脚本

| 脚本 | 功能 |
|------|------|
| `dump_erp_schema.py` | 导出 `t_advert_agent*` 全部 DDL |
| `dump_erp_schema_live_full.py` | 结构 + 行数 + 样例 → JSON |
| `gen_erp_db_summary_md.py` | 由 JSON 生成 Markdown 现状报告 |

### 3.4 运维脚本

| 脚本 | 功能 | 注意 |
|------|------|------|
| `truncate_erp_agent_tables.py` | **清空全部 18 张 Agent 表** | ⚠️ 必须传 `--yes` |
| `resolve_listing_context.py` | 解析 ASIN 的 shop_id/SKU/site_code | 调试用 |
| `query_erp_empty_asins.py` | 查若干 ASIN 的 summary/card 行数 | 快速抽查 |

---

## 4. 完整工作流

### 4.1 标准流程（全自动）

```powershell
# 第一步：串行跑所有 ASIN 的 Campaign + Wizard + ERP 写入
cd ad-direction-agent
python scripts/erp_db/run_erp_batch_sequential.py `
  --asins "B0B7S3PWWB,B0CGH9QRKK,B0DNZNWGX5"
```

完成后在 `cursor临时文件/` 目录下产出：
- `{ASIN}_kb.jsonl` — Campaign 分析结果
- `{ASIN}_wizard.jsonl` — 四层向导结果
- `{ASIN}_write_report_batch.json` — ERP 写入报告
- `erp_batch_sequential_status.json` — 批次状态汇总

### 4.2 验证流程

```powershell
# 找 decision_id
python -c "import json; print(json.load(open('cursor临时文件/B0CGH9QRKK_write_report_batch.json'))['write']['decision_id'])"

# 全量验证
python scripts/erp_db/verify_erp_full_write.py `
  --decision-id dec5da0a62fe024fa952815cd44be083 `
  --expected-cards 56

# 结构健康检查
python scripts/erp_db/db_health_check.py
```

### 4.3 调试流程（手动分步）

```powershell
cd ad-direction-agent

# Step 1: 只跑 Campaign 实验（产出 kb.jsonl）
python scripts/campaign_kb_experiment.py `
  --asins B0B7S3PWWB --temperatures 0.0 --runs 1 `
  --output cursor临时文件\B0B7S3PWWB_kb.jsonl

# Step 2: 验证 kb.jsonl 内容
python -c "import json; d=json.loads(open('cursor临时文件/B0B7S3PWWB_kb.jsonl').readline()); print(f'cards={len(d[\"adjustments\"])} summary={d[\"summary\"]}')"

# Step 3: 跑 Wizard（产出 wizard.jsonl）
python scripts/wizard_kb_experiment.py `
  --asins B0B7S3PWWB --output-dir cursor临时文件 --days 7

# Step 4: ERP 写入
python scripts/erp_db/write_erp_all.py `
  --asin B0B7S3PWWB `
  --kb cursor临时文件\B0B7S3PWWB_kb.jsonl `
  --wizard cursor临时文件\B0B7S3PWWB_wizard.jsonl `
  --report cursor临时文件\B0B7S3PWWB_full_report.json

# Step 5: 验证
python scripts/erp_db/verify_erp_full_write.py `
  --decision-id <从报告里取> `
  --expected-cards <kb.jsonl 的 adjustments 长度>
```

### 4.4 环境重置

```powershell
# ⚠️ 危险操作：清空全部 Agent 表
python scripts/erp_db/truncate_erp_agent_tables.py --yes
```

---

## 5. 公共模块

| 文件 | 说明 |
|------|------|
| `_bootstrap.py` | 路径初始化：`sys.path` 加入 `ad-direction-agent/` |
| `_erp_conn.py` | 默认连接配置 + `erp_pymysql_conn()` 连接工厂 |

所有脚本通过 `_bootstrap.bootstrap_sys_path()` 确保能 import `app.persistence.erp_writer`。

自定义数据库连接（覆盖默认）：
```powershell
python scripts/erp_db/write_erp_from_jsonl.py `
  --host 192.168.2.51 --port 3306 `
  --user erp_agentadvert --password "erp_agentadvert#weilai123" `
  ...
```

---

## 6. 数据库连接信息

| 项 | 值 |
|----|-----|
| IP | `192.168.2.51` |
| 端口 | `3306` |
| 库名 | `erp_agentadvert` |
| 账号 | `erp_agentadvert` |
| 密码 | `erp_agentadvert#weilai123` |
| 字符集 | `utf8mb4` |

---

## 7. 写入的 18 张表

| # | 表名 | 写入模式 | 说明 |
|---|------|---------|------|
| 1 | `t_advert_agent_decision` | full | 决策主表（1 ASIN = 1 次分析 = 1 行） |
| 2 | `t_advert_agent_decision_config` | full | 决策配置（产品阶段/目的等） |
| 3 | `t_advert_agent_modify_suggest_summary` | dual/full | 分析概览统计 |
| 4 | `t_advert_agent_modify_suggest_card` | dual/full | 活动卡片（1 活动 = 1 行） |
| 5 | `t_advert_agent_modify_keyword_pending` | dual/full | 关键词 Bid/状态变更 |
| 6 | `t_advert_agent_modify_campaign_pending` | dual/full | 活动 Budget/状态变更 |
| 7 | `t_advert_agent_modify_placement_pending` | dual/full | 广告位加价变更 |
| 8 | `t_advert_agent_data_metrics` | dual/full | 数据指标（ACOS/CPC 等快照） |
| 9 | `t_advert_agent_purpose_score` | full | 广告目的评分 |
| 10 | `t_advert_agent_core_keyword_tracking` | full | 核心关键词追踪 |
| 11 | `t_advert_agent_ai_suggest` | full | AI 综合建议 |
| 12 | `t_advert_agent_direction_recommend` | dual/full | 方向推荐（legacy） |
| 13 | `t_advert_agent_direction_recommend_detail` | dual/full | 方向推荐明细（legacy） |
| 14 | `t_advert_agent_modify_advert_record` | — | 执行主记录（ERP 写） |
| 15 | `t_advert_agent_modify_keyword_record` | — | 关键词执行记录 |
| 16 | `t_advert_agent_modify_campaign_record` | — | 活动执行记录 |
| 17 | `t_advert_agent_modify_placement_record` | — | 广告位执行记录 |
| 18 | `t_advert_agent_modify_portfolio_record` | — | 组合执行记录 |

> 表 14-18（`*_record`）由 **ERP 执行侧**写入（运营同意后→Amazon API 回调），**Agent 不写**。

---

## 8. 常见问题

### Q1: write_dual 和 write_full 的区别？什么时候用哪个？

| 场景 | 用 |
|------|-----|
| 只需要前端「分析建议」Tab 的数据 | `write_dual` |
| 需要完整决策归档（含 decision 主表 + 向导表） | `write_full` |
| 批量编排 | `run_erp_batch_sequential`（内部调 `write_full`） |

### Q2: 写入失败如何排查？

1. 看 report JSON 中的 `error` 字段
2. 跑 `db_health_check.py` 看是否有孤儿行/枚举异常
3. 跑 `verify_erp_full_write.py --decision-id <id>` 确认哪些表缺行

### Q3: 同 ASIN 多次跑会产生重复行吗？

不会。`modify_suggest_*` 表都用 `INSERT ... ON DUPLICATE KEY UPDATE`，同一 `decision_id + campaign_id` 后续写入会覆盖前一次（幂等）。

### Q4: 如何只更新某个 ASIN 而不重新跑 Campaign？

```powershell
# kb.jsonl 和 wizard.jsonl 已存在时
python scripts/erp_db/run_erp_batch_sequential.py `
  --asins B0B7S3PWWB --erp-only
```

### Q5: kb.jsonl 的 adjustments 数量与 card 表行数对不上？

跑 `verify_erp_full_write.py --decision-id <id> --expected-cards <n>`，它会报告实际卡片数。

常见原因：
- `adjustments` 中有 `action=keep` 且 `proposed == current` 的活动不会生成 pending 子行（但有 card 行）
- `_normalize_action` 把 LLM 的 keep 改写为 adjust_X → 产生 pending 行

### Q6: 为什么 record 表一直是空的？

`*_record` 表由 **ERP 执行侧**在运营点击「同意」后写入。Agent 只负责 pending 表（建议阶段），不参与执行。

---

## 9. 相关文档

| 文档 | 路径 |
|------|------|
| ERP 写入说明（完整） | `docs/ERP写入说明.md` |
| 分析建议表设计 | `docs/sql/advert_agent_modify_suggest_full_ddl.sql` |
| 表字段说明 | 钉钉群文件 `advert_agent_modify_suggest_data_integration.md` |
| ERP 数据库接入 1 | `docs/ERP数据库接入文档1.md` |
| ERP 数据库接入 2 | `docs/ERP数据库接入文档2_campaign.md` |
