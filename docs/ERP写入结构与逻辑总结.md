# ERP 写入结构与逻辑总结

> **日期**: 2026-06-05  
> **代码分支**: `feature/erp-writer`

---

## 一、目录结构

```
ad-direction-agent/
├── app/persistence/erp_writer/          # 核心包
│   ├── listing_context.py   # ASIN → shop_id / sku / site_code 解析
│   ├── mappers.py           # KB JSON → CanonicalRun 规范化
│   ├── models.py            # CanonicalRun 等数据结构 + stable_id 主键生成
│   ├── text_utils.py        # 枚举映射、文本拆分、P3 段落解析引擎
│   ├── erp_display.py       # WHP 三列展示字段构建（唯一入口）
│   ├── repository.py        # MySQL 连接、事务、15 张表 INSERT ... ON DUPLICATE KEY UPDATE
│   └── auto_push.py         # 编排入口 push_full_to_erp()
│
└── scripts/erp_db/          # 脚本层
    ├── write_erp_all.py     # 命令行全量写入（kb + wizard JSONL → ERP）
    └── _erp_conn.py         # 数据库连接配置
```

### 各文件职责速览

| 文件 | 一句话职责 |
|------|-----------|
| `listing_context.py` | 根据 ASIN 查 Doris/ERP 拿到 shop_id、seller_sku、site_code |
| `mappers.py` | 把 Campaign KB JSON 转成 `CanonicalRun`（cards、pending、指标等规范化结构） |
| `models.py` | `CanonicalRun` 等 dataclass + `stable_id()` 确定性主键生成 |
| `text_utils.py` | 枚举映射（方向类型/产品阶段/季节等）、文本拆分、P3 解析（综合判断/执行节奏/风险提示） |
| `erp_display.py` | WHP 前端三列（决策依据/建议/后续关注）的唯一构建入口，内部调用 `text_utils` 引擎 |
| `repository.py` | 连接 MySQL、管理事务、手写 SQL 写 13 张表 |
| `auto_push.py` | 编排：resolve → canonicalize → write_full，供脚本和 API 共用 |

### 触发入口

| 方式 | 说明 |
|------|------|
| `python scripts/erp_db/write_erp_all.py --asin xxx --kb xxx.jsonl --wizard xxx.jsonl` | 命令行 |
| `POST /campaign/analyze` 且 `write_erp: true` | API 自动写入 |

---

## 二、写入逻辑

### 2.1 输入

| 输入 | 内容 |
|------|------|
| **KB payload** | Campaign 分析结果：`adjustments[]`（每条广告调价建议）、summary、warnings |
| **Wizard payload** | 四层向导结果：`directions[]`（4 个方向）、`analysis`（overall_analysis）、`p3`（建议+后续关注文案） |

### 2.2 整体流程

```
KB JSON + Wizard JSON
  │
  ├─① resolve_listing_context(asin)      → shop_id, sku, site_code
  ├─② canonicalize_payload(kb)           → CanonicalRun（cards、pending、metrics）
  │
  └─③ repo.write_full(run, wizard)       → 单事务写 13 张表
       │
       ├── 决策主表 + 决策配置               (2 表)
       ├── 指标快照                          (1 表)
       ├── 分析建议汇总                      (1 表)
       ├── 建议卡 + 三类 pending             (4 表)  ← 来自 KB adjustments
       │
       └── 有 wizard 时追加 ────────────────────┐
           ├── 广告目的评分 / 核心词 / AI 建议   (3 表)
           └── 方向推荐 + 方向明细              (2 表)  ← 来自 wizard directions
```

### 2.3 写入的表一览

| 来源 | 表名 | 说明 |
|------|------|------|
| wizard | `t_advert_agent_decision` | 决策主表（产品阶段/方向类型等） |
| wizard | `t_advert_agent_decision_config` | 决策配置快照 |
| kb | `t_advert_agent_data_metrics` | ACOS/CVR 等指标 |
| kb | `t_advert_agent_modify_suggest_summary` | 分析概览（淘汰/调整/保留数） |
| kb | `t_advert_agent_modify_suggest_card` | 每个 campaign 一张建议卡 |
| kb | `t_advert_agent_modify_keyword_pending` | Bid/State 变更 |
| kb | `t_advert_agent_modify_campaign_pending` | Budget/State 变更 |
| kb | `t_advert_agent_modify_placement_pending` | 广告位加价 |
| wizard | `t_advert_agent_purpose_score` | 广告目的评分 |
| wizard | `t_advert_agent_core_keyword_tracking` | 核心词跟踪 |
| wizard | `t_advert_agent_ai_suggest` | AI 建议段落 |
| wizard | `t_advert_agent_direction_recommend` | WHP 顶部三块文案 |
| wizard | `t_advert_agent_direction_recommend_detail` | Tab4 四个方向明细 |

### 2.4 WHP 三列展示字段

`erp_display.build_whip_display_fields(analysis, wizard)` 一次调用产出三列：

| DB 字段 | 内容 | 数据来源 |
|---------|------|----------|
| `decision_basis_json` | `string[]` | `analysis.overall_analysis` 按句号分句 |
| `conclusion_json` | `string[]` | P3 中 `【执行节奏】` 段（兜底：`action_priorities`） |
| `data_focus_json` | `string[]` | P3 中 `【风险提示】` 段（兜底：`risk_warnings`） |

### 2.5 关键设计

- **全部 `INSERT ... ON DUPLICATE KEY UPDATE`**，主键由 ASIN + 实验ID + 数仓ID 确定性派生
- **单事务**：成功 commit，异常 rollback
- **幂等**：同一 `decision_id` 重跑时先 DELETE 旧的 card/pending 再 INSERT
- **只写不执行**：Agent 产出建议写入 ERP，运营在 WHP 同意后由另一系统调 Amazon API
