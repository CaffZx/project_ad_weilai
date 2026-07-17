# 复盘记忆短期 POC 实施方案与链路测试记录

> 版本：v1.0  
> 日期：2026-07-17  
> 状态：已完成 SkillOpt + DeepSeek 最小链路验证；复盘业务样本仍处于 POC 阶段。

## 1. POC 只验证什么

短期只验证两件事：

1. 是否能把一个真实、结构完整的数据快照稳定加工为可信的产品级和活动级复盘记忆；
2. SkillOpt 是否能在不改变输入事实的前提下，优化“证据 → 复盘记忆”的文本生成规范。

短期不做全量历史清洗、生产自动调度、自动注入线上 Agent、自动改知识库/主提示词，也不以单个案例判断业务效果已经提升。

当前 POC 数据依赖已闭合：可取得决策时活动/产品数据、产品基线、知识库版本、运营同意/执行证据、执行后 T+3 数据及干扰信息。当前仍以手工导出和反查为主；正式自动化治理见《复盘记忆长期落地实现方案》。

## 2. POC 数据表

POC 先在测试库或独立 schema 建三张兼容正式版的表，不改生产执行链路。

### 2.1 `t_advert_agent_analysis_snapshot`

保存“Agent 当时依据什么做出决策”。字段至少包括：`snapshot_id`、`decision_id`、`parent_asin`、`child_asins_json`、`analysis_at`、`data_as_of`、`snapshot_origin`、`kb_version`、`kb_content_hash`、`product_baseline_json`、`product_metrics_json`、`campaign_metrics_json`、`decision_cards_json`、`source_refs_json`、`schema_version`、`created_at`。

历史反查样本的 `snapshot_origin` 写 `RECONSTRUCTED`；快照不可原地修改，修正时新增版本。

### 2.2 `t_advert_agent_review_case`

保存执行事实、T+3 结果、干扰项和确定性计算结果。字段至少包括：`review_case_id`、`snapshot_id`、`decision_id`、`parent_asin`、`review_window`、`effective_at`、`review_at`、`approval_evidence_json`、`execution_evidence_json`、`activity_after_json`、`product_after_json`、`interference_json`、`eligible`、`exclusion_reason`、`calculated_evidence_json`、`outcome_label`、`attribution_confidence`、`case_status`、`schema_version`、时间戳。

建议唯一约束：`decision_id + review_window`。一个案例的活动通过 JSON 数组保存，并各自保留 `campaign_id`。

### 2.3 `t_advert_agent_review_memory`

保存 AI 生成的活动级/产品级记忆及人工审核结果。字段至少包括：`review_memory_id`、`review_case_id`、`memory_scope`、`parent_asin`、`campaign_id`、`structured_memory_json`、`memory_text`、`renderer_version`、`model_name`、`human_status`、`human_comment`、`human_revised_text`、`is_active`、时间戳。

建议唯一约束：`review_case_id + memory_scope + campaign_id + renderer_version`。POC 记忆一律不直接用于生产。

## 3. 样本选择、结果标签与人工审核

首场景选择精准词，并优先满足：明确 `decision_id`、运营同意、Amazon 最终生效、完整 T+3、前后口径一致、无改价/人工二次调整、流量非零、产品基线完整、可定位知识库版本。

结果标签：

- `EFFECTIVE`：目标方向明确改善且无不可接受副作用；
- `PARTIAL`：部分改善但仍有明显未达标或副作用；
- `INEFFECTIVE`：已执行但未形成有意义改善；
- `HARMFUL`：关键指标恶化或明显偏离产品目标；
- `INSUFFICIENT_DATA`：数据不足，不能判断；
- `EXCLUDED`：改价、人工二次调整、执行不完整等重大干扰，不进入 benchmark。

人工审核至少检查：事实和数字正确、结果标签一致、关键结论有证据、因果表达克制、适用条件明确、可供 Agent 使用且不冗余。AI 不得自行定义阈值或补全缺失事实。

## 4. 最小 AI 输入 JSON 契约

交给 AI 的是最小事实投影，而不是完整存储快照。JSON 固定最多三层：

1. 顶层：样本合法性、时间、版本和执行证据状态；
2. `product`：产品身份、策略上下文、决策前和 T+3 产品指标；
3. `product.activities[]`：活动级数据。精准词当前默认一词、一匹配类型、一活动。

父 ASIN、父 AKU 和店铺保留给人工定位，不作为 AI 判断成功与否的依据。比例一律为小数，例如 `0.4` 表示 40%。计划动作不得伪装为已执行事实。

## 5. 已导出的生产 POC 样本

样本来自只读生产 ERP 与数仓/MCP 查询：

- 店铺：`am_vivibeautyus`，`shop_id=1622`；
- 产品：父 ASIN `B0B7S3PWWB`，父 AKU `FS02721-3pcs`；
- 决策：`dec098372cfa7d32b0180c0acea985d0`，创建于 `2026-07-01 00:04:01`；
- 精准活动：`fish net-31253-精准`，活动 ID `430023578889110`，关键词 `fish net`，匹配类型 `EXACT`。

重要限制：生产 pending 仍是 `PENDING`，没有 Amazon 最终生效证据。因此样本的 `action_evidence_status` 必须为 `ASSUMED_EFFECTIVE_FOR_POC`，仅用来验证“数据 → 复盘 JSON/文本”链路，绝不能作为真实效果 benchmark 的标准答案。

```json
{
  "sample_id": "poc-dec098372cfa7d32b0180c0acea985d0",
  "decision_id": "dec098372cfa7d32b0180c0acea985d0",
  "knowledge_base_version": null,
  "review_window": "T3",
  "effective_at": null,
  "assumed_effective_date_for_poc": "2026-07-01",
  "review_end_date": "2026-07-03",
  "is_valid_sample": true,
  "validity_basis": "POC_USER_DEFAULT",
  "has_interference": false,
  "interference_summary": "按本期 POC 约定默认无干扰；尚未自动核验改价和人工二次调整",
  "action_evidence_status": "ASSUMED_EFFECTIVE_FOR_POC",
  "production_execution_status": "PENDING",
  "product": {
    "store_id": 1622,
    "store_name": "am_vivibeautyus",
    "marketplace": "Amazon_US",
    "parent_asin": "B0B7S3PWWB",
    "parent_aku": "FS02721-3pcs",
    "product_positioning": "P0_PRODUCT",
    "product_stage": "MAINTAINING",
    "seasonality_stage": "OFF_SEASON",
    "advertising_objective": "CONVERSION",
    "primary_advertising_direction": "BALANCE_MAINTAIN",
    "target_budget": 70.0,
    "target_acos": 0.4,
    "pre_ad_spend": 460.06,
    "pre_ad_sales": 1577.88,
    "pre_ad_orders": 206,
    "pre_acos": 0.2915684336,
    "pre_total_sales": 4856.46,
    "pre_total_orders": 640,
    "pre_tacos": 0.0947315534,
    "pre_gross_margin_rate": 0.1663174473,
    "t3_ad_spend": 222.34,
    "t3_ad_sales": 794.92,
    "t3_ad_orders": 105,
    "t3_acos": 0.279701102,
    "t3_total_sales": 2362.89,
    "t3_total_orders": 309,
    "t3_tacos": 0.0940966359,
    "t3_gross_margin_rate": 0.1703129014,
    "recommended_activity_count": 405,
    "approved_activity_count": 0,
    "effectively_executed_activity_count": 0,
    "activities": [
      {
        "campaign_id": "430023578889110",
        "campaign_name": "fish net-31253-精准",
        "child_asin": "B09SGC3YZB",
        "keyword_text": "fish net",
        "match_type": "EXACT",
        "action_summary": "计划：Bid 0.40→0.38；预算维持 3.00；REST_OF_SEARCH 广告位 50%→40%",
        "action_evidence_status": "计划动作，生产 pending 未确认、未执行",
        "pre_ad_spend": 4.86,
        "pre_ad_sales": 6.99,
        "pre_ad_orders": 1,
        "pre_acos": 0.6953,
        "pre_impressions": 385,
        "pre_clicks": 8,
        "pre_cpc": 0.6075,
        "pre_cvr": 0.125,
        "t3_ad_spend": 8.32,
        "t3_ad_sales": 20.97,
        "t3_ad_orders": 2,
        "t3_acos": 0.3967572723,
        "t3_impressions": 518,
        "t3_clicks": 14,
        "t3_cpc": 0.5942857143,
        "t3_cvr": 0.1428571429
      }
    ]
  }
}
```

拿到真正最终生效的案例后，只替换合法性、干扰、有效时间、执行证据、执行活动比例和各活动实际动作相关字段，不改变 JSON 结构。

## 6. 复盘记忆输出与临时拆分验证

AI 输出统一 JSON，包含 `product_memory` 与 `activity_memories`。代码负责按身份拆分、筛选和注入；AI 只生成记忆内容。共同来源字段（案例、决策、父 ASIN/AKU、店铺、窗口、知识库版本）必须随两个输出保留。

```json
{
  "schema_name": "ad_agent_review_memory",
  "schema_version": "1.0",
  "source_review_case_id": "poc-dec098372cfa7d32b0180c0acea985d0",
  "source_decision_id": "dec098372cfa7d32b0180c0acea985d0",
  "parent_asin": "B0B7S3PWWB",
  "parent_aku": "FS02721-3pcs",
  "store_name": "am_vivibeautyus",
  "review_window": "T3",
  "knowledge_base_version": null,
  "product_memory": {
    "scope": "PRODUCT",
    "memory_status": "SHADOW_ONLY",
    "outcome": "INSUFFICIENT_EXECUTION_EVIDENCE",
    "memory_text": "该产品整体 ACOS 从 29.2% 降至 28.0%，TACOS 基本稳定、毛利率略升；但本次决策对应的生产 pending 未确认、未执行，不能将产品变化归因于本次精准词调整。该案例仅保留为待验证观察，不作为正式策略经验注入。"
  },
  "activity_memories": [
    {
      "scope": "ACTIVITY",
      "memory_status": "SHADOW_ONLY",
      "campaign_id": "430023578889110",
      "campaign_name": "fish net-31253-精准",
      "keyword_text": "fish net",
      "match_type": "EXACT",
      "outcome": "INSUFFICIENT_EXECUTION_EVIDENCE",
      "memory_text": "关键词 fish net 的精准活动在决策时 ACOS 为 69.5%，建议小幅下调 Bid 并下调无转化广告位；后续 ACOS 为 39.7%，但未证明该动作实际生效且前后观察周期不同，不能形成正式结论。"
    }
  ]
}
```

临时验证脚本：[split_review_memory.py](../ad-direction-agent/scripts/split_review_memory.py)。它只把统一 JSON 拆成两个本地 JSON 文件：

```powershell
cd D:\project\AD_Agent_work_place6.17\AD_assistant_agent-v3.2\ad-direction-agent
python scripts\split_review_memory.py review_memory.json `
  --product-output product_memory.json `
  --activity-output activity_memories.json
```

输出：

- `product_memory.json`：共同来源字段和唯一产品级 `memory`；
- `activity_memories.json`：共同来源字段和活动级 `memories` 数组。

该脚本不访问数据库、不调用 LLM、不改变文本、不把 `SHADOW_ONLY` 变成可注入记忆。正式版只将其输出目标从本地 JSON 改为记忆表落库。

## 7. 最小 benchmark 与 scorer

单案例只验证链路，不能构成 benchmark。首版应积累 10—20 个真实精准词案例：涵盖有效、部分有效、无效、有害和数据不足，排除重大干扰；每个案例保留 AI 原文、人工意见、人工修订文本和人工结果标签。

按父 ASIN 约 80/20 切分，同一父 ASIN 不得同时进入训练和验证。运营同意只是样本来源，不是标准答案。

评分分两层：

- 硬门槛：JSON 可解析、无虚构事实/数字、身份/动作关联正确、数据不足或排除案例不被写成确定成功、关键风险不遗漏；任一失败即不合格。
- 质量分（100）：标签一致 30、证据与数字正确 25、因果克制 20、适用/避免条件清楚 15、简洁可用 10。

初期以人工评分为准；积累稳定标注后再加入确定性检查和 LLM Judge。LLM Judge 不是唯一裁判。

## 8. SkillOpt POC 范围

SkillOpt 仅优化“结构化证据 → 复盘记忆”的文本对象，冻结数据计算、标签规则、知识库和主 Agent 提示词。

目标 benchmark 目录：

```text
review_memory_benchmark/
├── initial.md
├── dataloader.py
├── rollout.py
├── scorer.py
├── cases/
└── labels/
```

只有 validation 提升、硬门槛零失败并通过人工复核的候选，才可以成为新的复盘生成版本。POC 结果不自动进入生产。

## 9. 已完成的 SkillOpt + DeepSeek 链路测试

### 9.1 环境与数据

- SkillOpt 官方源码已克隆至 `D:\project\AD_Agent_work_place6.17\skillopt`；
- 已直接安装 `.[searchqa]` 依赖；
- 已配置 `openai_compatible`：`https://api.deepseek.com/v1`、模型 `deepseek-chat`；密钥只保存在被 Git 忽略的 `.env`；
- 已执行 `python scripts/materialize_searchqa.py`，生成 `data/searchqa_split/train`、`val`、`test`。

### 9.2 实际执行命令

为控制成本，使用一条样本、一轮、单并发：

```powershell
Get-Content .env | ForEach-Object {
  if ($_ -match '^\s*export\s+([^=]+)=(.*)$') {
    Set-Item -Path "Env:$($matches[1])" -Value $matches[2]
  }
}

python scripts/train.py `
  --config configs/searchqa/default.yaml `
  --limit 1 `
  --num_epochs 1 `
  --out_root outputs/searchqa_deepseek_smoke `
  --cfg-options `
    model.optimizer_backend=openai_compatible `
    model.target_backend=openai_compatible `
    model.optimizer=deepseek-chat `
    model.target=deepseek-chat `
    env.workers=1 `
    gradient.analyst_workers=1 `
    train.train_size=1
```

`train.train_size=1` 是 smoke test 必需覆盖项：默认配置为 400，而 `--limit 1` 后加载训练集为 1，未覆盖时 SkillOpt 会在本地配置校验阶段拒绝运行。

### 9.3 测试结果

- 训练与目标两个角色均确认是 `deepseek-chat (openai_compatible)`；
- 完成基线评测、rollout、反思、聚合、候选 skill 更新、验证 gate 与测试评测；
- 共 8 次 DeepSeek 调用，约 10,019 tokens，耗时约 9 秒；
- 输出目录：[searchqa_deepseek_smoke](../../skillopt/outputs/searchqa_deepseek_smoke)；
- 最佳验证 skill 仍为初始 skill，1 条测试样本上的最终分数为 0。

最后一项不是模型/接入故障：只有一条训练、验证、测试样本，测试分数不具统计意义。本次已证明的只是 SkillOpt → DeepSeek → 评测 → 反思 → 修改 skill → 再评测链路可正确运行。

## 10. 立即执行顺序与 POC 验收

1. 建三张 POC 表并冻结本文件 JSON 与标签；
2. 选择一个真正已执行且完整 T+3 的精准词案例；
3. 手工导出事实包，先由确定性程序输出证据 JSON；
4. AI 生成产品级和活动级记忆，人工审核并记录修订原因；
5. 验证同样输入稳定输出、零虚构、人工只需小改；
6. 扩至 10—20 个案例；
7. 实现 scorer，得到 `initial.md` 基线分；
8. 将 SearchQA adapter 替换为复盘记忆 benchmark，再运行 SkillOpt。

POC 最终验收：统一结构可读取、结构化输出成功率 100%、事实虚构为 0、人工标签一致率首轮建议不低于 85%、至少 80% 记忆无需实质性改写、SkillOpt 候选在父 ASIN 隔离验证集上优于初始版本且未靠失去可用性刷分。

