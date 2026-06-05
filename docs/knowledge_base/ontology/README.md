# Ontology 第一期 — 结构化落地说明

> 本目录为 Ontology 结构化文件，**不修改**现有 SOP Markdown 知识库，**不接入** Agent 运行时代码（Phase 2 再实施代码注入）。
>
> **权威部署说明**见 [部署方案.md](./部署方案.md)。

## 1. 目标

让广告调整 Agent 在生成建议前，能稳定判断：

- 当前对象是什么、能不能被调整
- 当前 Campaign Type 允许哪些动作
- 广告目的与广告方向是否合法
- 当前动作是否有足够证据
- 是否违反 ontology 硬规则
- **（Phase 1b）** 父 ASIN 下 Campaign 归属哪个预算组合、组合/父级预算是否回算一致

知识库负责「怎么决策」，Ontology 负责「先别认错对象」；预算组合层负责「活动层合理但整体预算不失控」。

## 2. 文件清单

| 文件 | 职责 |
| --- | --- |
| [entities.yaml](./entities.yaml) | 对象层级、OBJ/CAT 边界、AdBudgetGroup、广告目的/方向、输出字段 |
| [campaign_types.yaml](./campaign_types.yaml) | 5 种 `campaign_type` 的允许/禁止动作、目标类型、所需报表 |
| [actions.yaml](./actions.yaml) | 9 个 `action_code` 的作用对象、增长型、证据、预算回算 post_conditions |
| [evidence.yaml](./evidence.yaml) | 报表/信号可证明内容与可触发动作、动作→证据索引 |
| [validation_rules.yaml](./validation_rules.yaml) | ONT-001 ~ ONT-010；GROUP-001 ~ GROUP-007（预算） |
| [budget_groups.yaml](./budget_groups.yaml) | **Phase 1b** 四类组合、归类优先级、回算公式、输出契约 |
| [部署方案.md](./部署方案.md) | 分阶段路线图、Prompt 注入模板、验收清单 |

## 3. 来源映射

| 结构化文件 | 方案 / KB 来源 |
| --- | --- |
| entities.yaml | `ontology方案/20-亚马逊广告Ontology补充.md` §2/§3/§5/§6/§11；`22-Ontology第一期补充内容清单.md` §2/§4 |
| campaign_types.yaml | 20 §4；22 §3 |
| actions.yaml | 20 §7；22 §5/§6.3；23 §5/§8（post_conditions） |
| evidence.yaml | 20 §8；22 §6 |
| validation_rules.yaml | 20 §9；22 §7；23 §10（GROUP-*） |
| budget_groups.yaml | [23-广告组合与预算分配规则.md](../执行规则/23-广告组合与预算分配规则.md) 全文 |
| 部署方案.md | `ontology方案/21-Ontology第一期落地方案.md` + Phase 1b 扩展 |

草案 Markdown 仍保留于 `cursor临时文件/reference_sources/ontology方案/`，本目录为**可机器读取**的交付物。

## 4. 范围（已做 / 未做）

### 已做 — Phase 1

- 对象层级（ASIN / Campaign / Keyword / SearchTerm / Placement 等）
- Campaign Type 动作合法性表
- 广告目的 × 广告方向枚举与映射
- 动作合法性（9 个 action_code）
- 报表证据绑定
- 校验规则 ONT-001 ~ ONT-010

### 已做 — Phase 1b（本期文档）

- 预算控制层级：ParentASIN → AdBudgetGroup → Campaign
- 四类组合类型与归类优先级
- GROUP-BUDGET-001 ~ 005 与 GROUP-001 ~ 007 校验
- `portfolio_budget_summary` / `budget_groups[]` 输出契约
- 比例分配与 60/20/20 加权分配公式
- 推广组保护（GROUP-BUDGET-004）按实际花费分档

### 未做（后续阶段）

- Neo4j / 企业级数据血缘
- JSON Schema 自动校验器
- `kb_loader.py` / `reasoner.py` 注入与 GROUP 输出校验（Phase 2）
- Memory / 因果图谱
- OWL / RDF 标准本体
- ERP 表结构扩展（组合级字段落库，属 WHP/DBA）

## 5. 与现有知识库对接（说明性，本期不改 KB 文件）

| 现有 KB 文件 | Ontology 补充作用 |
| --- | --- |
| [02-标签维度定义.md](../02-标签维度定义.md) | 对齐 `entities.yaml`；可补充 AdBudgetGroup |
| [05-动作规则.md](../05-动作规则.md) | 对齐 `actions.yaml`；预算动作引用组合回算 |
| [07-广告位规则.md](../07-广告位规则.md) | 对齐 `campaign_types.yaml` + ONT-002 |
| [10-安全护栏.md](../10-安全护栏.md) | 对齐 `validation_rules.yaml`；组合/父 ASIN 预算护栏 |
| [12-输出规范.md](../12-输出规范.md) | 对齐 `output_fields` + `budget_groups.yaml` 输出契约 |
| [执行规则/17-问题诊断与动作优先级.md](../执行规则/17-问题诊断与动作优先级.md) | 对齐 `evidence.yaml`；Campaign 后组合二次分配 |
| [执行规则/18-广告执行调整流程.md](../执行规则/18-广告执行调整流程.md) | 动作前 ontology 校验；Step 9 后组合回算 |
| [执行规则/21-淘汰广告活动规则.md](../执行规则/21-淘汰广告活动规则.md) | 淘汰释放预算进入 `eliminated_campaign_release` |
| [执行规则/23-广告组合与预算分配规则.md](../执行规则/23-广告组合与预算分配规则.md) | **权威 SOP** → `budget_groups.yaml` + GROUP-* |

## 6. 验收标准

### Phase 1（ONT）

1. 不会对 `SearchTerm` 直接调 Bid（ONT-001）
2. 不会对 `broad_keyword` / `phrase_keyword` / `auto_discovery` 输出 `placement_adjustment`（ONT-002）
3. 不会输出 `ad_purpose=maintain`（ONT-003 / ONT-008）
4. 缺 `inventory_status` 时不输出增长型动作（ONT-004）
5. 缺 `placement_report` 时只阻断 Placement，不阻断 Bid / Budget / 否词（ONT-005）
6. 每条动作可附带：`object_class`、`campaign_type`、`ontology_validation_status`、`ontology_rule_refs`、`source_report_refs`

### Phase 1b（GROUP）

7. 每个 Campaign 有明确 `group_type`（AC-07 / GROUP-001）
8. Campaign 预算变更后含 `portfolio_budget_summary` 且回算组合（AC-08 / GROUP-002）
9. 回算父 ASIN 总预算（AC-09 / GROUP-003）
10. 淘汰组 $1 预算、$0.20 Bid，不参与增预算（AC-10 / GROUP-004）
11. 推广组有稳定花费时不被挤断供（AC-11 / GROUP-005）
12. 不允许净增时已压缩正向增量（AC-12 / GROUP-006）
13. 输出含 `budget_groups[]` 明细（AC-13）

详见 [validation_rules.yaml](./validation_rules.yaml) 末尾 `acceptance_checklist`。

## 7. 使用方式（本期）

### 人工 / Prompt 注入

按任务检索相关片段，组装 **Ontology Card**（勿整包注入）。

**对象 / 动作合法性：**

```text
【Ontology Card】
campaign_type: broad_keyword
forbidden_actions: [placement_adjustment]
ontology_rule_refs: [ONT-002]
allowed_actions: [bid_down_for_acos, add_negative_search_term, ...]
```

**预算组合（父 ASIN 多 Campaign）：**

```text
【Budget Ontology Card】
hierarchy: ParentASIN → AdBudgetGroup → Campaign
group_types: [promotion_group, testing_group, auto_broad_group, elimination_group]
classification_priority: elimination > auto_broad > promotion > testing
principles: [GROUP-BUDGET-001..005]
output_required: portfolio_budget_summary, budget_groups[]
ontology_rule_refs: [GROUP-002, GROUP-003]
```

检索路径：

- 输入含 `SearchTerm` → `entities.yaml` + ONT-001
- 输入含 `campaign_type=broad_keyword` → `campaign_types.yaml` + ONT-002
- 父 ASIN 多活动预算调整 → `budget_groups.yaml` + GROUP-002/003
- 拟输出淘汰 → `budget_groups.yaml` elimination_group + GROUP-004/004A

### 案例

- **ONT**：`campaign_type=broad_keyword`, `suggested_action=placement_adjustment` → blocked，ONT-002
- **GROUP**：50 Campaign 总和超父 ASIN 目标 → 必须输出 `portfolio_budget_summary`，按 GROUP-006 压缩

## 8. 后续接入（见部署方案.md）

| 阶段 | 内容 | 改代码 |
| --- | --- | --- |
| Phase 1 | 5 YAML + ONT-001~010 | 否 |
| Phase 1b | budget_groups.yaml + GROUP-* + 本文档 | 否 |
| Phase 2 | kb_loader ontology preset；输出校验 ONT+GROUP | 是 |
| Phase 3 | 规则版本、预算回算回归用例 | 是 |
| Phase 4 | Neo4j / GraphRAG（可选） | 是 |

## 9. 版本

- Phase 1: `version: phase_1`，对齐知识库 `v3.1.0`（`kb_loader.KnowledgeBase.VERSION`）
- Phase 1b: `budget_groups.yaml` → `version: phase_1b`
