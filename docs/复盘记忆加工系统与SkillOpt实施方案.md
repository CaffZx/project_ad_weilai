# 复盘记忆加工系统与 SkillOpt 实施方案

> 版本：v1.0  
> 日期：2026-07-17  
> 状态：讨论稿，可直接用于短期 POC 启动  
> 总目标：通过可追溯的真实复盘记忆，提高广告 Agent 后续推荐建议的准确率。

## 1. 结论先行

目前的数据已经足够启动“复盘记忆加工系统”的最小可行性验证，不需要先完成执行表治理，也不需要先接入 SkillOpt。

当前具备的核心数据包括：

- 活动级：运营同意且最终执行生效的决策、决策时活动数据、所用知识库版本、执行后 T+3 数据。
- 产品级：被同意执行的活动比例、产品定位/阶段/淡旺季/广告目标/方向/目标 ACOS/目标预算、决策时产品数据、未来 T+3 数据。
- 可排除或标记的干扰：外部改价、人工二次调整等。
- 可确认的执行时间：Amazon 最终生效时间，而不是仅以 MCP 提交时间代替。

因此，短期缺的不是新的外部数据源，而是三样内部产物：

1. 一份固定的数据样本结构；
2. 一套把事实加工为结论和记忆的规则；
3. 一套人工可以判断“这段记忆是否合格”的验收标准。

长期正式实施仍需补齐自动化治理：

- 执行子表写入 `decision_id`；
- 每次分析完成时冻结当时的数据快照；
- 自动生成 T+3 复盘任务；
- 对复盘记忆进行版本、审核、启停和过期管理；
- 用真实案例构建 benchmark，再由 SkillOpt 离线优化文本对象；
- 最后验证记忆确实提升了 Agent 的推荐准确率，而不只是让复盘文本“看起来更好”。

## 2. 本方案的基本约定

### 2.1 复盘单位

一次复盘样本以 `decision_id` 为主线，包含：

- 一个产品级复盘对象；
- 本次决策下一个或多个活动级复盘对象；
- 本次被同意并实际执行的复合动作；
- 决策前快照和执行后 T+3 结果。

活动级对象使用 `decision_id + campaign_id` 定位。本期及当前业务前提下，不要求依赖各类 `pending_id` 才能完成复盘。

### 2.2 时间口径

- POC 只使用 T+3，且必须至少有完整 3 天数据。
- T+3 的起点使用 Amazon 最终生效时间。
- 正式版第一阶段仍以 T+3 为必选复盘点。
- 后续是否增加 T+7，只是扩展项，不是当前依赖。

### 2.3 两层输出

- 活动级记忆：说明某个活动采取了哪些组合动作、结果如何、以后遇到什么条件可以参考或应避免。
- 产品级记忆：结合整个产品的活动执行比例、产品阶段和整体数据，判断方向是否有效，以及下一次产品级策略应注意什么。

两者分开存储、分开注入，但可以来自同一个复盘样本。

### 2.4 AI 的职责边界

AI 不直接决定原始数字是否正确，也不负责自行猜测缺失事实。

系统先通过确定性程序完成：

- 数据校验；
- 前后指标计算；
- 执行动作汇总；
- 干扰项检查；
- 证据字段整理。

AI 负责：

- 在给定证据内解释结果；
- 形成结构化复盘结论；
- 生成可被后续 Agent 使用的简洁记忆文本。

## 3. 当前依赖是否闭合

| 依赖 | 短期 POC | 正式自动化 | 说明 |
|---|---|---|---|
| 运营同意的决策 | 已具备 | 已具备 | 不能把“运营同意”直接当作结果正确，只能证明该动作真实进入执行流程 |
| Amazon 最终执行生效 | 可手工确认 | 需稳定落库 | 用于确定 T+3 起点 |
| 决策时活动数据 | 可反查 | 需自动冻结 | POC 允许重建，但必须标记为重建快照 |
| 决策时产品数据和产品基线 | 已具备 | 需自动冻结 | 正式版应与决策永久绑定 |
| 知识库版本 | 已具备 | 需保存版本和内容哈希 | 只有版本号仍可能无法证明内容未被覆盖 |
| T+3 活动/产品数据 | 已具备 | 需自动调度采集 | 至少完整 3 天 |
| 干扰因素 | 可人工排除 | 需结构化记录 | 如改价、人工二次调整、断货等 |
| 复盘结果标签 | 需本期设计 | 需规则化和版本化 | 这是加工规则，不是外部数据缺口 |
| 合格记忆标准 | 需人工建立 | 形成 benchmark 和 scorer | 是质量保证的核心 |

结论：POC 的数据依赖已经闭合；正式系统缺的是自动记录、关联、调度和评测能力。

---

# 方案一：短期最小验证方案

## 4. 短期目标

短期只回答两个问题：

1. 是否可以把一份真实、完整的数据快照稳定加工成可信的复盘记忆？
2. SkillOpt 是否可以在不篡改事实的前提下，提高这类记忆生成文本的质量？

短期不做：

- 全量历史数据清洗；
- 生产链路自动调度；
- 复盘记忆自动注入线上 Agent；
- 自动修改知识库或主提示词；
- 根据 1 个案例直接判断广告效果获得了提升。

## 5. POC 数据库表

POC 建议先建 3 张兼容正式方案的表。可以建在测试库或独立 schema，避免一开始改生产执行链路。

### 5.1 `t_advert_agent_analysis_snapshot`

用途：保存“Agent 当时依据了什么做出决策”。POC 中允许把历史数据反查后手工写入，但必须标记为重建。

| 字段 | 用途 |
|---|---|
| `snapshot_id` | 快照主键 |
| `decision_id` | 关联本次决策 |
| `parent_asin` | 产品级归属 |
| `child_asins_json` | 本次涉及的子 ASIN |
| `analysis_at` | 当时分析时间 |
| `data_as_of` | 快照数据截止时间 |
| `snapshot_origin` | `LIVE` 或 `RECONSTRUCTED`；POC 历史反查写 `RECONSTRUCTED` |
| `kb_version` | 当时知识库版本 |
| `kb_content_hash` | 当时知识库内容哈希；POC 可先为空，但应同时保存知识库副本 |
| `product_baseline_json` | 产品定位、阶段、淡旺季、广告目的、方向、目标 ACOS、目标预算等 |
| `product_metrics_json` | 当时产品级数据 |
| `campaign_metrics_json` | 当时各活动数据 |
| `decision_cards_json` | 当时生成的活动卡片和建议动作 |
| `source_refs_json` | 数仓/MCP/业务表来源、查询时间和必要的查询条件 |
| `schema_version` | 快照结构版本 |
| `created_at` | 记录创建时间 |

约束：快照写入后不可原地修改；修正时新增版本。

### 5.2 `t_advert_agent_review_case`

用途：保存执行事实、T+3 结果、干扰项和确定性计算结果，是“复盘加工”的输入事实包。

| 字段 | 用途 |
|---|---|
| `review_case_id` | 复盘案例主键 |
| `snapshot_id` | 关联决策时快照 |
| `decision_id` | 冗余保留，便于检索 |
| `parent_asin` | 产品级归属 |
| `review_window` | POC 固定为 `T3` |
| `effective_at` | Amazon 最终生效时间 |
| `review_at` | 实际复盘时间 |
| `approval_evidence_json` | 运营同意记录及同意范围 |
| `execution_evidence_json` | 实际生效的预算、Bid、广告位、否词等动作 |
| `activity_after_json` | 各活动 T+3 数据 |
| `product_after_json` | 产品整体 T+3 数据 |
| `interference_json` | 改价、人工二次调整、库存等干扰证据 |
| `eligible` | 是否可进入有效复盘 |
| `exclusion_reason` | 不可复盘的明确原因 |
| `calculated_evidence_json` | 程序计算出的前后变化、目标偏差和样本充分性 |
| `outcome_label` | 人工最终确认的结果标签 |
| `attribution_confidence` | 对“结果由本次动作导致”的可信度 |
| `case_status` | `DRAFT`、`READY`、`REVIEWED`、`EXCLUDED` |
| `schema_version` | 案例结构版本 |
| `created_at` / `updated_at` | 时间戳 |

建议唯一约束：`decision_id + review_window`。一个案例内通过 JSON 数组包含该次决策涉及的所有活动，活动项必须分别保留 `campaign_id`。

### 5.3 `t_advert_agent_review_memory`

用途：保存 AI 生成的活动级/产品级记忆，以及人工审核结果。

| 字段 | 用途 |
|---|---|
| `review_memory_id` | 记忆主键 |
| `review_case_id` | 关联事实案例 |
| `memory_scope` | `ACTIVITY` 或 `PRODUCT` |
| `parent_asin` | 产品级归属 |
| `campaign_id` | 活动级记忆必填；产品级为空 |
| `structured_memory_json` | 结构化结论、证据和适用条件 |
| `memory_text` | 最终供 Agent 阅读的文本 |
| `renderer_version` | 生成该文本的 prompt/skill 版本 |
| `model_name` | 生成模型 |
| `human_status` | `PENDING`、`APPROVED`、`REVISED`、`REJECTED` |
| `human_comment` | 人工指出的问题 |
| `human_revised_text` | 人工修订后的标准文本 |
| `is_active` | 是否允许后续使用；POC 一律不直接用于生产 |
| `created_at` / `updated_at` | 时间戳 |

建议唯一约束：`review_case_id + memory_scope + campaign_id + renderer_version`。

## 6. 一个完整 POC 样本必须包含什么

选择案例时优先使用“精准词调整”场景，并满足：

- 有明确 `decision_id`；
- 运营明确同意；
- Amazon 最终执行生效；
- 已过完整 T+3；
- 决策前后数据范围一致；
- 没有改价或人工二次调整；
- 流量不为零，且不是明显数据不足案例；
- 可以获取完整知识库版本或当时文件副本；
- 产品基线完整。

最终导出的样本包应具有以下结构：

```text
review_case_bundle/
├── manifest.json                 # decision_id、ASIN、时间范围、版本、来源
├── analysis_snapshot.json        # 决策时产品和活动数据
├── approved_decision.json        # 卡片、动作、运营同意范围
├── execution_evidence.json       # Amazon 最终生效动作和时间
├── t3_result.json                # 活动级和产品级 T+3 数据
├── interference_check.json       # 干扰检查结果
├── knowledge_base/               # 当时知识库副本或可校验引用
└── expected_review.json          # 人工复盘后形成，最初允许为空
```

## 7. 复盘加工中间件的最小形态

POC 阶段不应先做复杂服务，也不应只依赖一段自由发挥的提示词。

建议做成一个可重复执行的“复盘编译器”，最初可以是一个命令行脚本，内部调用 AI；待输入输出稳定后，再把使用说明和生成规范封装为 skill。

处理顺序固定为：

1. 读取样本包；
2. 校验字段、时间窗口和来源；
3. 汇总实际执行动作；
4. 计算前后指标变化；
5. 检查干扰和样本是否充分；
6. 形成只包含事实的结构化证据；
7. 将结构化证据交给 AI；
8. AI 输出严格 JSON 和记忆文本；
9. 人工审核并给出通过、修订或拒绝；
10. 把审核结果写入 `t_advert_agent_review_memory`。

推荐的 AI 输出结构：

```json
{
  "activity_reviews": [
    {
      "campaign_id": "...",
      "outcome": "EFFECTIVE|PARTIAL|INEFFECTIVE|HARMFUL|INSUFFICIENT_DATA",
      "evidence": [],
      "attribution_confidence": "HIGH|MEDIUM|LOW",
      "applicable_conditions": [],
      "avoid_conditions": [],
      "memory_text": "..."
    }
  ],
  "product_review": {
    "outcome": "EFFECTIVE|PARTIAL|INEFFECTIVE|HARMFUL|INSUFFICIENT_DATA",
    "evidence": [],
    "attribution_confidence": "HIGH|MEDIUM|LOW",
    "memory_text": "..."
  }
}
```

## 8. 结果标签和人工审核

### 8.1 结果标签

- `EFFECTIVE`：目标方向得到明确改善，且没有不可接受的副作用。
- `PARTIAL`：部分目标改善，但仍有明显未达目标或副作用。
- `INEFFECTIVE`：执行有效，但 T+3 没有形成有意义的改善。
- `HARMFUL`：关键业务指标明显恶化，或偏离产品目标。
- `INSUFFICIENT_DATA`：曝光、点击、订单或时间窗口不足，不能可靠判断。
- `EXCLUDED`：存在改价、人工二次调整、执行不完整等干扰，不进入 benchmark。

具体阈值不能让 AI 自行发明，应由程序根据目标 ACOS、预算、流量和产品阶段计算，再由人工在 POC 中校准。

### 8.2 人工审核项

每段记忆至少审核以下项目：

| 审核项 | 判断标准 |
|---|---|
| 事实正确 | 数字、动作、时间、活动和 ASIN 无错误；出现虚构事实直接失败 |
| 结果判断 | 与人工对本案例的结果标签一致 |
| 证据完整 | 关键结论能指向明确的前后数据和执行动作 |
| 因果克制 | 不把短期相关性夸大为确定因果 |
| 适用条件 | 说明该经验在什么产品阶段、目标和流量条件下适用 |
| 可执行性 | 后续 Agent 能从中得到清晰约束或参考，而不是空泛总结 |
| 简洁性 | 不重复原始数据，不把整份报告塞入记忆 |

## 9. 最小 benchmark

一个案例只能验证链路跑通，不能构成有效 benchmark。

第一版 benchmark 建议使用 10—20 个真实“精准词”案例：

- 全部来自运营同意且最终执行生效的真实决策；
- 同时包含有效、部分有效、无效、有害和数据不足情况；
- 排除存在重大干扰的案例；
- 每个案例由人工确认结果标签，并保存“AI 原文、人工意见、人工修订文本”；
- 同一父 ASIN 的所有案例只能进入 train 或 validation 一侧；
- POC 按父 ASIN 约 80/20 切分，不再强制对同一 ASIN 做时间切分。

注意：运营同意记录只是样本来源，不是标准答案。标准答案必须结合 T+3 结果和人工复盘形成。

## 10. 最小打分逻辑

建议把打分分为“硬门槛”和“质量分”。

### 10.1 硬门槛

任一项失败，则该候选记为不合格：

- 输出 JSON 无法解析；
- 出现输入中不存在的事实或数字；
- 关联错 ASIN、活动或动作；
- 把 `EXCLUDED`/`INSUFFICIENT_DATA` 案例写成确定性成功经验；
- 漏掉人工指定的关键风险。

### 10.2 质量分（100 分）

| 项目 | 分值 |
|---|---:|
| 结果判断与人工标签一致 | 30 |
| 证据覆盖和数字引用正确 | 25 |
| 因果表达克制 | 20 |
| 适用/避免条件清楚 | 15 |
| 文本简洁、可供 Agent 使用 | 10 |

初期以人工评分为主；当积累了稳定标注后，再用确定性检查 + LLM Judge 复现人工标准。LLM Judge 不能成为唯一裁判。

## 11. POC 接入 SkillOpt 的㭭ࢇ৲ڮ݆୹մ T+3、无明显干扰、执行可确认；
- 手工导出完整样本包并写入 POC 表。

### 第 2 步：人工配合 AI 完成一次复盘

- 程序计算事实；
- AI 生成活动级和产品级记忆；
- 人工审核、修订并说明错误原因。

### 第 3 步：验证“加工系统是否可行”

通过条件：

- 所有结论都能追溯到输入事实；
- 不出现虚构数字或动作；
- 人工只需小改即可接受文本；
- 同样输入可以稳定得到同结构输出。

### 第 4 步：扩到 10—20 个精准案例

- 形成 train/validation；
- 补齐不同结果类型；
- 固化人工标准答案和错误分类。

### 第 5 步：实现 scorer

- 先实现确定性硬检查；
- 再加入基于人工标准的质量评分；
- 记录当前 `initial.md` 的基线分。

### 第 6 步：接入 SkillOpt

- 只优化复盘生成规范；
- 对比 validation；
- 人工确认最佳候选；
- POC 结果不自动进入生产。

## 13. POC 的最终验收

POC 通过至少应满足：

- 10—20 个真实案例均可被统一结构读取；
- 结构化输出成功率 100%；
- 事实虚构为 0；
- 人工结果标签一致率达到约定门槛，建议首轮不低于 85%；
- 至少 80% 的记忆无需实质性改写即可接受；
- SkillOpt 最佳候选在父 ASIN 隔离的 validation 上优于初始版本；
- 最佳候选经人工检查没有通过“写得更保守但失去作用”等方式刷分。

---

# 方案二：正式实施“复盘记忆 + SkillOpt”方案

## 14. 正式系统目标

正式系统需要形成完整闭环：

```text
当次分析数据冻结
        ↓
运营同意 + Amazon 最终执行生效
        ↓
到期自动采集 T+3 结果
        ↓
确定性复盘计算 + AI 记忆生成
        ↓
审核、版本和生命周期管理
        ↓
按父 ASIN 注入后续分析
        ↓
离线 benchmark 验证推荐准确率
        ↓
SkillOpt 优化文本对象并受控发布
```

主广告分析流程不能等待复盘完成。快照写入、T+3 采集、复盘生成和 SkillOpt 都应异步运行；复盘失败不得阻断新一轮广告分析。

## 15. 长期需要修改的现有执行记录

执行记录子表只补充 `decision_id`，本方案不要求新增不同类型的 `pending_id`。

| 表 | 新增字段 |
|---|---|
| `t_advert_agent_modify_campaign_record` | `decision_id` |
| `t_advert_agent_modify_keyword_record` | `decision_id` |
| `t_advert_agent_modify_placement_record` | `decision_id` |

同时必须保证：

- 执行 MCP 请求中的同一次运营同意，无论拆成几次 MCP 调用，都传同一个 `decision_id`；
- ERP 执行主表已有的 `decision_id` 被真实写入，不能继续为空；
- ERP 在写入各执行子表时同步保存该 `decision_id`；
- 执行动作仍通过已有 `campaign_id`、关键词 ID/匹配类型、广告位等字段区分；
- 不用 `decision_id` 替代动作自身的业务 ID。

## 16. 正式数据表

正式版继续使用 POC 的 3 张核心表，并增加调度和审计能力。

### 16.1 核心表

- `t_advert_agent_analysis_snapshot`：不可变的决策时快照；正式数据一律为 `LIVE`。
- `t_advert_agent_review_case`：执行结果、T+3 数据、干扰检查和确定性结论。
- `t_advert_agent_review_memory`：活动级/产品级记忆及当前生效版本。

### 16.2 新增 `t_advert_agent_review_task`

用途：管理 T+3 到期任务和重试。

关键字段：

- `task_id`
- `decision_id`
- `snapshot_id`
- `parent_asin`
- `review_window`
- `effective_at`
- `due_at`
- `task_status`
- `retry_count`
- `last_error`
- `locked_by` / `locked_at`
- `created_at` / `updated_at`

唯一约束：`decision_id + review_window`，确保幂等。

### 16.3 新增 `t_advert_agent_review_memory_audit`

用途：保留每次人工审核、启用、停用、替换和回滚记录，避免只在记忆主表保留最后状态。

关键字段：

- `audit_id`
- `review_memory_id`
- `operation`
- `before_json` / `after_json`
- `operator`
- `reason`
- `created_at`

### 16.4 新增 `t_advert_agent_text_artifact_version`

用途：统一记录会影响结果的文本对象版本。

覆盖对象：

- 知识库；
- 广告 Agent 提示词；
- 复盘生成 skill/prompt；
- 记忆注入模板；
- SkillOpt 产出的 `best_skill.md`。

关键字段：

- `artifact_id`
- `artifact_type`
- `artifact_version`
- `content_hash`
- `storage_uri`
- `source_run_id`
- `status`
- `created_by`
- `created_at`

正文不一定放数据库，但数据库必须能通过版本和哈希找到当时真实内容。

### 16.5 可选 `t_advert_agent_skillopt_run`

用于保存优化运行的配置、训练集版本、验证集版本、初始分、最佳分、候选产物地址和人工发布结论。POC 可先只保存文件和运行报告，正式接入发布流程后再建。

## 17. 正式复盘加工链路

### 17.1 分析完成时

主流程直接复用本轮已经取得的数据，异步写入 `analysis_snapshot`：

- 不为保存快照再次调用同样的 MCP；
- 同时保存数据截止时间、来源和结构版本；
- 保存卡片、建议动作、产品基线、知识库版本和内容哈希；
- 写失败时告警和补偿，但不阻断用户拿到分析结果。

### 17.2 运营同意并执行时

- 将同一个 `decision_id` 传给所有属于本次同意的执行 MCP 调用；
- ERP 主表和动作子表共同写入 `decision_id`；
- 记录 Amazon 最终生效状态和生效时间；
- 只有最终生效的动作进入复盘范围；
- 部分成功时，必须按实际成功动作复盘，不能按原建议全量复盘。

### 17.3 T+3 到期时

- 由异步 worker 拉取到期任务；
- 获取活动级和产品级 T+3 数据；
- 校验价格、人工调整、库存和执行完整性；
- 将证据和计算结果写入 `review_case`；
- 数据不足时写 `INSUFFICIENT_DATA`，不强行形成成功/失败结论。

### 17.4 生成记忆时

- 确定性计算器先生成证据；
- AI/复盘 skill 只读取标准证据结构；
- 活动级和产品级分别生成记忆；
- 自动生成的记忆先处于待审核或影子状态；
- 达到稳定质量后，才允许按规则自动启用低风险记忆。

## 18. 记忆的使用和生命周期

### 18.1 管理单位

父 ASIN 是记忆生命周期和训练/验证隔离的管理单位。

- 产品级记忆直接归属于父 ASIN；
- 活动级记忆同时保存父 ASIN 和 `campaign_id`；
- 同一父 ASIN 的历史活动可共同为该产品提供上下文；
- 活动级事实不能无条件推广到所有产品。

### 18.2 注入位置

- 产品级记忆：注入产品方向、预算和整体策略分析环节。
- 活动级记忆：只在对应活动或高度相似的活动调整环节使用。
- 记忆必须带适用条件、避免条件、证据时间和可信度。
- `INSUFFICIENT_DATA`、`EXCLUDED`、未审核或停用记忆不得作为正向经验注入。

### 18.3 版本和回滚

每次分析需要记录实际使用的：

- 记忆 ID 和版本；
- 知识库版本；
- 主提示词版本；
- 复盘生成器版本；
- Agent 代码版本或可定位的发布版本。

任何 SkillOpt 候选发布后都必须可以一键切回上一版本。

## 19. 正式 benchmark 体系

正式体系不能只有“复盘文本 benchmark”，至少需要两层。

### 19.1 第一层：复盘加工 benchmark

验证输入快照能否被正确加工成记忆，评测：

- 事实忠实度；
- 结果标签；
- 证据覆盖；
- 因果克制；
- 适用条件；
- 文本质量。

该层可以优化复盘生成 skill，但不能证明 Agent 建议准确率已经提高。

### 19.2 第二层：推荐建议 benchmark

验证“使用该记忆后，Agent 是否做出更准确的下一次建议”。

每个案例至少保存：

- 决策时可见数据；
- 当时允许使用的知识库和历史记忆；
- Agent 生成的建议；
- 运营是否同意；
- 最终实际动作；
- T+3 结果；
- 人工对建议合理性的标注。

同一个案例分别运行：

1. 不注入复盘记忆；
2. 注入当前复盘记忆；
3. 注入 SkillOpt 候选记忆/提示词。

只有第 2/3 组在隔离验证集上提高建议准确率且没有扩大风险，才能说明复盘记忆真正产生业务价值。

### 19.3 数据切分

- 第一优先：按父 ASIN 分组切分，建议约 80/20；同一父 ASIN 不得跨两侧。
- 后续补充：未来时间段验证，用较早案例构建系统，再用之后新产生的案例验证是否受市场和季节变化影响。
- “未来时间段验证”是在验证系统面对未来数据是否仍有效，不表示一条产品记忆只对某个日期有效，也不替代产品阶段/淡旺季字段。

## 20. SkillOpt 的正式优化范围

SkillOpt 每轮只优化一个边界明确的文本对象，禁止同时改多个对象后无法判断提升来自哪里。

推荐顺序：

### 第一阶段：复盘生成 skill

优化“结构化证据如何变为活动级/产品级记忆”。冻结数据、计算、标签规则、知识库和主 Agent 提示词。

### 第二阶段：记忆注入模板

优化“如何把已经审核的记忆提供给广告 Agent”，避免信息过载、冲突和错误泛化。

### 第三阶段：广告 Agent 提示词的指定片段

仅开放与记忆使用相关的片段，使用推荐建议 benchmark 评测。

### 第四阶段：知识库的指定模块

只有 benchmark 足够大、文本版本治理成熟后才进入。知识库不能整库自由改写，应按模块设置可编辑范围，并由业务人员审核。

永远不交给 SkillOpt 修改的内容：

- 原始业务数据；
- 指标计算代码；
- 数据关联规则；
- 安全阈值和执行 guardrail；
- train/validation 划分；
- scorer 的硬门槛。

## 21. SkillOpt 发布门槛

一个候选版本进入生产前必须同时满足：

- held-out validation 高于当前生产版本；
- 硬门槛零失败；
- 不降低高风险案例的识别能力；
- 人工抽检通过；
- 推荐建议 benchmark 有改善或至少不回退；
- 完整记录训练集、验证集、scorer、模型和候选内容版本；
- 先影子运行，再小范围启用；
- 具备明确回滚版本。

SkillOpt 不得直接把 `best_skill.md` 自动覆盖生产文件。

## 22. 正式落地阶段

### 阶段 A：数据治理

- 执行主表实际写入 `decision_id`；
- 三张执行子表增加并写入 `decision_id`；
- 主分析流程自动冻结快照；
- 保存知识库、提示词和代码版本；
- 验证从一条执行记录可以回到完整决策和快照。

验收：随机抽取执行记录，能够无人工猜测地恢复决策前证据、实际动作和版本。

### 阶段 B：自动 T+3 复盘，暂不注入

- 建立 `review_task`；
- 到期自动采集；
- 自动计算并生成记忆；
- 全部由人工审核；
- 只做影子运行。

验收：连续运行一段时间无重复任务、无错关联、无阻塞主流程，人工通过率达到 POC 门槛。

### 阶段 C：受控记忆注入

- 只启用人工审核通过的记忆；
- 记录每次分析实际使用的记忆版本；
- 对比有/无记忆建议；
- 建立冲突、过期和停用规则。

验收：隔离 benchmark 和影子流量均无明显回退。

### 阶段 D：SkillOpt 离线优化

- 固化 benchmark 版本；
- 先优化复盘生成 skill；
- 再优化记忆注入模板；
- 保留所有运行和候选产物；
- 人工批准后进入影子版本。

验收：validation 提升、硬错误为零、人工确认、可回滚。

### 阶段 E：扩大场景

按照精准词、广泛词、扩词、预算、广告位、否词和复合动作逐步增加案例。每扩一个场景，都先补 benchmark，再允许 SkillOpt 优化。

## 23. 两套方案的关系

| 项目 | 短期最小验证 | 正式实施 |
|---|---|---|
| 数据来源 | 手工导出、历史反查 | 分析时自动冻结、T+3 自动采集 |
| 执行关联 | 手工通过主表和现有字段恢复 | 主表和三个子表稳定写入 `decision_id` |
| 复盘触发 | 人工选择案例 | 异步任务自动触发 |
| 加工中间件 | 命令行脚本 + AI + 人工审核 | 确定性编译器 + 复盘 skill + worker |
| 记忆使用 | 不进生产 | 审核、版本化后按父 ASIN 注入 |
| benchmark | 10—20 个精准真实案例 | 多场景、持续扩展、双层 benchmark |
| SkillOpt | 只优化复盘生成规范 | 分阶段优化复盘 skill、注入模板、提示词片段、知识库模块 |
| 最终判断 | 能否稳定生成可信记忆 | 是否提高推荐建议准确率且不增加风险 |

## 24. 现在马上执行的动作清单

1. 审核并冻结本文件中的 3 张 POC 表和结果标签。
2. 在测试库建立 3 张表，不改生产执行表。
3. 选择 1 个已过完整 T+3 的精准词真实案例。
4. 手工导出并校验 `review_case_bundle`。
5. 写最小确定性计算脚本，先输出证据 JSON。
6. 用固定 prompt 让 AI 生成活动级和产品级记忆。
7. 由业务人员审核，记录通过、修订、拒绝和原因。
8. 若单案例链路成立，扩到 10—20 个案例。
9. 固化 benchmark 和 scorer，得到未优化基线分。
10. 接入 SkillOpt，只优化 `initial.md`，验证 held-out validation。

当前最优先的下一步不是接入 SkillOpt，而是完成第 1—7 项，得到第一份结构完整、人工认可的真实复盘记忆。

## 25. 本方案采用的默认决策

- POC 首场景使用精准词。
- POC 和正式第一阶段统一使用 T+3。
- 复盘以 `decision_id` 为主线，以父 ASIN 管理生命周期。
- 活动卡片通过 `decision_id + campaign_id` 定位，不把 `pending_id` 设为 POC 依赖。
- 运营同意是入选条件，不是结果正确的标准答案。
- 先验证复盘记忆质量，再验证其对推荐建议的提升。
- SkillOpt 始终离线运行，不自动修改生产文本。
- 历史方案文档仅作参考；如与本文件冲突，以本文件后续确认版本为准。

## 26. 本期最小 AI 复盘输入契约与生产样例

### 26.1 输入契约

本期交给 AI 的不是完整存储快照，而是一份最小事实投影。完整快照、查询条件、执行原始记录和人工审核记录仍保存在数据库或原始样本包中。

该 JSON 固定只有三层：

1. 最外层：样本合法性、时间、版本和执行证据状态；
2. `product`：产品定位信息、策略上下文、产品决策前数据和 T+3 数据；
3. `product.activities[]`：本次活动级数据。当前精准词场景中，一个活动默认对应一个关键词和一个匹配类型。

父 ASIN、父 AKU（当前生产字段为 `parent_seller_sku`）和店铺信息保留在 `product` 中，便于人定位记忆归属。它们不是 AI 判断成败的依据。

比例统一使用小数：`0.4` 表示 40%。动作字段记录实际动作；如果 POC 只能拿到计划动作，必须通过最外层 `action_evidence_status` 明确标示，不能伪装成已执行事实。

### 26.2 真实生产样例（POC 默认合法）

以下样例来自生产 ERP 和数仓/MCP 的只读查询：

- 店铺：`am_vivibeautyus`，`shop_id=1622`；
- 产品：`B0B7S3PWWB` / `FS02721-3pcs`；
- 决策：`dec098372cfa7d32b0180c0acea985d0`，创建于 2026-07-01 00:04:01；
- 精准活动卡：`fish net-31253-精准`，关键词 `fish net`，`EXACT`；
- 产品前后数据及活动 T+3 数据来自 MCP；活动决策前数据来自当时 card 的 `perf_json`。

按本期 POC 约定，样本先标记为合法且无干扰；但生产 pending 记录实际仍为 `PENDING`，没有 Amazon 最终生效证据。因此 `action_evidence_status` 明确写为 `ASSUMED_EFFECTIVE_FOR_POC`。此样例只用于验证“数据 → 复盘文本”链路，不能作为“真实已执行动作效果”的 benchmark 标准答案。

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
    "product_name": null,
    "product_positioning": "P0_PRODUCT",
    "product_stage": "MAINTAINING",
    "seasonality_stage": "OFF_SEASON",
    "advertising_objective": "CONVERSION",
    "primary_advertising_direction": "BALANCE_MAINTAIN",
    "target_budget": 70.0,
    "target_acos": 0.4,
    "pre_period": "2026-06-24 至 2026-06-30（MCP 反查）",
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
    "executed_activity_spend_share_before": null,
    "activities": [
      {
        "campaign_id": "430023578889110",
        "campaign_name": "fish net-31253-精准",
        "child_asin": "B09SGC3YZB",
        "keyword_text": "fish net",
        "match_type": "EXACT",
        "action_summary": "计划：Bid 0.40→0.38；预算维持 3.00；REST_OF_SEARCH 广告位 50%→40%",
        "action_evidence_status": "计划动作，生产 pending 未确认、未执行",
        "pre_period": "决策时 card 快照，近 7 天（数据截止日未单独落库）",
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
        "t3_cvr": 0.1428571429,
        "pre_product_ad_spend_share": null,
        "pre_product_ad_sales_share": null
      }
    ]
  }
}
```

### 26.3 后续真实样本的替换规则

拿到真正被运营同意、Amazon 最终执行生效的案例后，只替换下列字段，不改变 JSON 结构：

- `is_valid_sample`、`has_interference`、`interference_summary`；
- `effective_at`、`review_end_at`；
- `action_evidence_status`、`production_execution_status`；
- `approved_activity_count`、`effectively_executed_activity_count`、`executed_activity_spend_share_before`；
- 每个活动的 `action_summary` 和 `action_evidence_status`。

届时样本才能进入真实复盘 benchmark；本样例仅用于先验证复盘加工器能否按固定结构工作。

## 27. 复盘记忆输出 JSON 与临时拆分脚本

### 27.1 输出边界

复盘加工器的输出必须是一份统一 JSON，其中明确区分 `product_memory` 与 `activity_memories`。前者供产品策略类 LLM 调用点使用，后者按 `campaign_id + keyword_text + match_type` 供对应活动分析调用点使用。

代码负责拆分、筛选和注入；AI 只负责生成记忆内容。来源 ID、父 ASIN、父 AKU、店铺、窗口和知识库版本需要随两个输出共同保留，便于追溯。`SHADOW_ONLY`、未审核或被停用的记忆不得进入生产 LLM 调用。

### 27.2 当前 POC 的复盘记忆输出示例

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
    "applicable_conditions": [
      "MAINTAINING",
      "OFF_SEASON",
      "CONVERSION",
      "BALANCE_MAINTAIN"
    ],
    "memory_text": "该产品在维持期、淡季、转化型目标下，产品整体 ACOS 从 29.2% 降至 28.0%，TACOS 基本稳定在约 9.4%，毛利率略升；但本次决策对应的生产 pending 未确认、未执行，不能将产品变化归因于本次精准词调整。该案例仅保留为待验证观察，不作为正式策略经验注入。"
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
      "applicable_conditions": [
        "精准词已有订单",
        "ACOS 高于目标",
        "维持期",
        "淡季"
      ],
      "memory_text": "关键词 fish net 的精准活动在决策时 ACOS 为 69.5%，建议小幅下调 Bid（0.40→0.38）并下调无转化广告位；后续观察窗口 ACOS 为 39.7%，但生产记录未证明该动作实际生效，且前后观察周期不同，不能形成“降 Bid 有效”的正式结论。后续需在确认执行且同口径 T+3 数据完整时重新验证。"
    }
  ]
}
```

### 27.3 临时拆分脚本

脚本：[split_review_memory.py](../ad-direction-agent/scripts/split_review_memory.py)。当前版本只是 POC 验证工具：它把本节的统一输出 JSON 写成两个本地 JSON 文件，用于验证拆分契约，不访问数据库、不调用 LLM、不改变记忆文本，也不属于生产主流程。

```powershell
cd D:\project\AD_Agent_work_place6.17\AD_assistant_agent-v3.2\ad-direction-agent
python scripts\split_review_memory.py review_memory.json `
  --product-output product_memory.json `
  --activity-output activity_memories.json
```

输出文件的职责：

- `product_memory.json`：保留共同来源字段和唯一一段 `memory`，供产品级调用点加载；
- `activity_memories.json`：保留共同来源字段和 `memories` 数组，供代码按活动身份筛选、拆分后注入；
- 注入前仍由调用代码检查 `memory_status == "ACTIVE"` 及人工审核状态；脚本不负责把 `SHADOW_ONLY` 变为可用记忆。

### 27.4 临时脚本原文

```python
"""将统一复盘记忆 JSON 拆分为产品级与活动级注入文件。"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any


COMMON_FIELDS = (
    "source_review_case_id",
    "source_decision_id",
    "parent_asin",
    "parent_aku",
    "store_name",
    "review_window",
    "knowledge_base_version",
)


def split_review_memory(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """返回独立的产品级记忆和活动级记忆 JSON 对象。"""
    product_memory = payload.get("product_memory")
    activity_memories = payload.get("activity_memories")
    if not isinstance(product_memory, dict):
        raise ValueError("product_memory 必须是对象")
    if not isinstance(activity_memories, list) or not all(
        isinstance(item, dict) for item in activity_memories
    ):
        raise ValueError("activity_memories 必须是对象数组")

    common = {field: payload.get(field) for field in COMMON_FIELDS}
    schema_version = payload.get("schema_version", "1.0")
    product_output = {
        "schema_name": "ad_agent_product_memory",
        "schema_version": schema_version,
        **common,
        "memory": copy.deepcopy(product_memory),
    }
    activity_output = {
        "schema_name": "ad_agent_activity_memories",
        "schema_version": schema_version,
        **common,
        "memories": copy.deepcopy(activity_memories),
    }
    return product_output, activity_output


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("输入 JSON 顶层必须是对象")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="统一复盘记忆 JSON 文件")
    parser.add_argument("--product-output", required=True, type=Path, help="产品级输出文件")
    parser.add_argument("--activity-output", required=True, type=Path, help="活动级输出文件")
    args = parser.parse_args()

    try:
        product_output, activity_output = split_review_memory(_load_json(args.input))
        _write_json(args.product_output, product_output)
        _write_json(args.activity_output, activity_output)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        parser.error(str(exc))

    print(f"已写入产品级记忆：{args.product_output}")
    print(f"已写入活动级记忆：{args.activity_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

### 27.5 正式脚本的唯一职责

正式实施时，此脚本不再写本地 JSON 文件，改为：

```text
读取已审核的统一复盘记忆 JSON
        ↓
调用 split_review_memory() 拆出产品级与活动级对象
        ↓
分别写入复盘记忆表
        ↓
返回 review_memory_id / 写入状态
```

它只负责拆分和落库，明确不负责：

- 拉取广告或产品数据；
- 计算 T+3 指标、判断干扰或判定样本合法性；
- 调用 AI 生成复盘内容；
- 根据记忆直接下发广告动作；
- 决定记忆是否可注入。注入资格仍由人工审核状态、`memory_status` 和后续调用点共同控制。
