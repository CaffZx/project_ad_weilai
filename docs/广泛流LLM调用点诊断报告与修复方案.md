# 广泛流 LLM 调用点诊断报告与修复方案

> 记录时间：2026-08-05。诊断对象：广泛流（BROAD/PHRASE/AUTO）LLM 调用点。
> 输入材料：`docs/广泛流LLM调用Prompt示例.md`（v3.25，2026-08-04）、`app/llm/kb_loader.py`、`app/llm/reasoner.py`、`app/workflow/steps/campaign.py`、`app/workflow/steps/campaign_search_term_promotion.py`。
> 触发现象：① 大量 7 天出单>1 的搜索词被 AI 推荐扩精准；② AI 对表现好的活动频繁同时加预算 + Bid。
> **v2 修订（2026-08-05）**：经外部审计核实——修正 4 处事实错误（IRRELEVANT_NO_IMPROVEMENT 有出处=21号 condition_code；stop_campaign 代码未接通不能直接改示例；预算规则缺数据 LLM 无法执行；词级样本不足词不一律禁标）、补 1 处遗漏（跨活动聚合放大因素）、采纳"投影层"方法论；新增 6 项隐患（P0 动作契约、P1 校正指标 raw、P1 输出契约漂移、P1 窗口校验、P1 预算利用率 0 省略、P2 容量）。详见各节"（审计修正/审计新增）"标注。

---

## 一、问题 1：知识库切片合理性

### 1.1 现象①（出单>1 被推荐扩精准）根因：三层割裂，非知识库无规则

**规则存在且清晰**：
- 31号§10 / 23号§3.4 三通道准入（订单通道：7天订单≥3 **且** ACOS≤目标）是唯一权威；
- 28号§1 SRC_CONVERTED 为最高可信度来源（R1，可 AUTO_APPROVED）；
- 切片**命中**：`campaign_adjustment_broad`（kb_loader.py:97-102）整文件注入 31号（含 §10）。

**但 prompt 内三层割裂，导致 LLM 无法/不愿执行准入**：

| 层 | 现状 | 问题 |
|---|---|---|
| LLM 标注层 | 输出约束（Prompt 示例 L110-114）只写"只输出语义相关且至少 R1 的正向候选；**禁止输出订单/花费/销售额/ACOS/CVR 等数值字段**" | 候选标准 = "语义相关 + R1"，**无任何订单门槛**；禁止 LLM 输出数值，却要它判断是否够格提精准——语义判据与数据判据分离 |
| 代码准入层 | `campaign_search_term_promotion.py` 按 23号§3.4 三通道过滤（订单≥3 且 ACOS≤目标；词根通道为 OR 逻辑） | 真正门槛在代码，LLM 不知道；词根通道"订单≥3 **或** ACOS≤目标"——订单不足但 ACOS 达标也能过 |
| 示例层 | 无"低信号词不标"负例；搜索词示例行（fishnet tights：6单、ACOS 48.5% > 目标 25%）本身会被 LLM 标为候选 | 无反向引导 |

**结论**：规则存在、切片命中，但"标注标准（语义 R1）"与"准入标准（订单≥3 且 ACOS≤目标）"在 prompt 内**没有桥接**——LLM 按更显式的输出约束执行（语义相关就标），代码再过滤。

**补充（审计核实）——跨活动聚合放大因素**：代码聚合键是**标准化搜索词/词根**，跨多个广泛活动合并（`campaign_search_term_promotion.py` `_aggregate` 按词/词根聚合、`(campaign_key, search_term)` 去重）。因此**单个活动只有 1-2 单，但同词或同词根在多个活动合计达到门槛时，仍可进入最终新增活动决策**。判断"AI 把 1-2 单词直接扩精准"必须区分 6 个环节：① LLM 标记候选 → ② 订单通道通过 → ③ 词根通道通过 → ④ 已有精准词排除 → ⑤ 跨活动合流 → ⑥ 最终新增活动截断。不能仅凭 LLM 输出下结论。

### 1.2 现象②（无关/冗余/错误内容注入）——实锤，大量

注入切片（kb_loader.py:97-102）：`18:1,3`、`17:1,2,3.x,4,5,7`、`15:1,2,4`、`19:1,2,3,4,6,7,8`、`22:0`、`21:0,1,2,3,4`、`10:1,2`、`03:7,8,10,12`、`14:1,9,16,20,21`、`30`（整文件）、`31`（整文件）+ Ontology Runtime Contract 卡。

逐段核对发现的噪声：

**① 版本更新记录混入（最大噪声源）**——给知识库维护者看的变更史，对 LLM 无决策价值：
- 03号§10"口径沿革与为什么回到固定值"（v3.3.0→v3.4.0→v3.4.3 三段历史）
- 10号§1.6"v3.4.0 重写 C-03"、03号§12"v3.4.8 修正"、17号§5.3/19号§1/19号§4"v3.4.0 修正 C-07/C-11/C-24"、15号§4"v3.4.0 C-07"、30号§1"v3.3.0 的失效点"、28号§1"v3.4.0 修正"、21号§1 冲突说明

**② 代码/实现层引用直接注入**：
- 03号§10：`settings.campaign_exact_main_daily_spend_threshold`、测试名 `test_zero_threshold_must_never_be_treated_as_explicit`
- 03号§12：`app/core/attribution_maturity.py`、`fit_curve_from_restatements`
- 31号§9：`advert_exec_mapper.py`；10号§1.8：`placement_adjustment_allowed()`

**③ 代码负责的规则重复注入**——kb_loader 注释自称"代码护栏已有的不重复注入（10号§6-7）"，实际 10号§1 整节（含 1.6-1.12）全注入：
- 10号§1.9 组合预算超限（代码 GROUP 校验）、§1.10 断路器（代码 `circuit_breaker.py`）、§1.11 对账冻结（代码 `reconciliation.py`）、§1.12 顺序纪律（纯开发路线）
- 30号§3 复合动作展开、§4 生命周期 11 态、§5 ERP 字段映射——全部代码/ERP 层

**④ 未决待办注入**：30号§5"待 ERP 确认：stop_campaign 对应的 archived 状态是否可写"

**⑤ Ontology Runtime Contract 卡**：`portfolio_groups`/`portfolio_routing`/`execution_layers`/`action_bundle.lifecycle` 等 YAML 块是代码/ERP 契约；对 LLM 有用的只有 `hard_rules`（ONT-RUNTIME-001/004/007）与 `mode_policies` 部分

**⑥ 重复定义**：17号"问题类型表"与 21号/19号的淘汰条件、否词规则多处交叉重复（v3.4.0 已修正但历史表述仍残留）

---

## 二、问题 2：JSON 输出示例的枚举值标准

### 2.1 枚举标准缺失 + 非法值静默兜底

**prompt 输出格式（Prompt 示例 L71-100）只有示例值，没有枚举清单**：action / direction / triggered_rule / review_level 均无合法值表。30号（动作总表）与 17号（问题类型）虽注入码表，但 LLM 需跨文件自寻，输出约束节未固化。

**示例值本身有 2 处错误/混用**：
- `action: "eliminate_to_low_bid_pool"`（广泛流示例）：30号 `ONT-RUNTIME-007` 明确广泛/词组/自动退出动作是 **`stop_campaign`**，eliminate 是精准类动作——示例与知识库冲突（详见 P0-A：代码未接通 stop_campaign，不能简单改文案）
- `triggered_rule: "IRRELEVANT_NO_IMPROVEMENT"`：**并非无出处**——它存在于 21号§1 `condition_code` 示例（21-淘汰广告活动规则.md:286），是**淘汰条件码**；真正的问题是 prompt 把两个命名空间混用：
  - `triggered_rule`：问题类型码（17号，如 `HIGH_ACOS_NO_ORDER` / `BUDGET_EXHAUSTED`）
  - `condition_code`：淘汰条件码（21号，如 `NO_CVR_HIGH_SPEND` / `IRRELEVANT_NO_IMPROVEMENT`）
  - 当前 LLM 会把淘汰条件当成 triggered_rule 输出，代码原样落库展示，无统一校验——**数据契约问题，不是单纯非法枚举**

**解析端兜底现状**（逐字段核实）：

| 字段 | 校验 | 兜底行为 | 危险度 |
|---|---|---|---|
| `action` | 无枚举校验 | `_normalize_action`（campaign.py:2485）按 proposed vs current **静默改写**为代码推导值（非 eliminate 时） | 中：静默改写，LLM 不知情；**eliminate 例外——LLM 可任意触发**，guardrails 只拦 P0 核心词 |
| `direction` | 无校验 | keep 时清空；非 keep 原样透传 | 低（不被下游消费） |
| `triggered_rule` | **无校验** | 原样透传落库/展示 | **高：非法码直接进入产出** |
| `review_level` | 无校验 | 原样透传 | 中：LLM 可自标 AUTO_APPROVED 绕过人工 |
| 候选 `relevance_tier` | 非 R1 丢弃 | 显式丢弃（有校验） | 低 |

**结论**：非法枚举不报错——`action` 被静默改写、`triggered_rule`/`review_level` 静默透传，LLM 无反馈闭环。

### 2.2 示例倾向引导——"表现好 → 同时加预算 + Bid"

**知识库其实有严格的利用率门槛（已注入）**：
- 19号§4：预算大涨 = **利用率>90%** + 近1天表现好；小涨 = **70-90%** + 表现好；维持 = 50-70%
- 31号§6：表现好且预算不足 = **利用率≥70% + 库存≥30天** 才加

**但 prompt 输出约束层完全没有固化这条**：
- "文本与数值自洽"约束只管 reason 与 proposed **同向**，不管"加预算的资格前提"
- 活动列表有"预算利用率: 91.7%"字段（Prompt 示例 L2073），但输出约束未要求"加预算必须引用预算利用率"
- 17号§3.1 矩阵 `BUDGET_EXHAUSTED → ① 加预算 ② 提Bid ③ TOS加价` 是**合法双涨**，前提是问题类型判对（利用率 100% 且 ACOS≤目标）——但 LLM 倾向跳过问题类型判定，把"表现好"直接映射为双涨

**结论**：规则有、切片命中，但**预算决策的资格前提（利用率）未在输出契约层固化**——LLM 的"表现好→双涨"常识倾向战胜了需自行查找的 KB 规则。

---

## 三、修复方案

> **本期范围（2026-08-05 更新）**：护栏与预算回算已纳入本期；知识库侧（版本记录 + stop_campaign 全部清理）已完成，无需核验。P0-A 动作契约定案、P0-B 枚举清单已完成。

### 修复建议总览

| 级别 | 项 | 内容 | 状态 |
|---|---|---|---|
| P0-A | 动作契约定案 | 广泛流真实动作集 = keep/adjust_bid/adjust_budget/eliminate_to_low_bid_pool/**paused_campaign**；归一化保留 eliminate/paused；guardrails 注释对齐 | ✅ 已完成 |
| P0-B | 枚举清单 | reasoner broad prompt 输出约束补合法值表（action 含 paused_campaign、禁 adjust_placement；triggered_rule 与 condition_code 命名空间分离；review_level 枚举；修正非法示例触发码） | ✅ 已完成 |
| P0-C | **护栏（paused 纳入本期）** | paused 走 guardrails：核心词/保护词所在活动不得 paused；样本不足不得暂停；KB31 §13 停止投放前诊断路径（高点击无单→否词→降 bid/预算→仍无改善才暂停）+ HIGH_RISK_REVIEW 人工确认 | 本期 |
| P0-D | **预算回算（paused 纳入本期）** | `campaign_budget_reallocation` 识别 paused：暂停释放金额 = current_daily_budget，计入 `other_campaign_budget_decrease`（与 eliminate 释放一致，但 eliminate 还迁低价池、暂停纯释放） | 本期 |
| P0-E | 预算资格代码化 | 透传已算好的 `effective_acos_tolerance`/`target_cpa`/`avg_order_value`（helper 已存在，LLM 不必重算）+ 代码预计算利用率档位/库存门槛；近1/3天暂不支持（无数据源，退化为 7 天+利用率+容忍上限） | 本期 |
| P1 | 候选标注桥接 | 输出约束写明"标注≠准入"（LLM 只做语义识别+R1，准入由代码计算）；词级样本不足词**保留审阅**（14天辅助观察），不一律禁标 | 本期 |
| P1 | 切片净化（投影层） | 建"广泛流决策投影层"，只注入当前有效规则/动作集/准入语义/窗口口径/输出契约；版本历史、代码实现、ERP 映射、待确认项排除（知识库已清理版本记录/stop） | 本期 |
| P1 | 校正指标接入准入 | bundle 已取 acos_corrected/cvr_corrected/data_maturity，但 `campaign_search_term_promotion._metrics` 仍用 raw——准入须改用校正口径 | 本期 |
| P1 | 输出契约对齐后端 | 输出契约合法值 = 后端代码消费值（P0-B 枚举）；KB 投影标注 KB31 字段（action_code 等）非输出，不诱导 LLM 输出代码不消费字段 | 本期 |
| P1 | 数值窗口校验 | reason/evidence 的 7d/14d 标注无结构化校验——候选事实回填与落库前保留结构化窗口事实，LLM 文案视为解释文本 | 本期 |
| P1 | 预算利用率 0 省略 | `_campaign_to_prompt_dict` 仅 cost>0 时注入利用率，0 花费活动字段缺失、LLM 无法区分"0% 与 N/A"——显式携带 0 或 N/A | 本期 |
| P2 | 容量风险 | 广泛流 KB 注入实测 59,378 字符/1,869 行，含大量版本历史——投影层优先于扩注入 | **延后** |

### P0-A：动作契约与执行代码不一致（审计新增，最高优先）

现状四方不一致：
- 知识库（30号/31号/10号§1.6）：广泛退出动作 = `stop_campaign`
- Prompt 示例：`eliminate_to_low_bid_pool`
- `_normalize_action`（campaign.py:2485）：只特殊保留 `eliminate_to_low_bid_pool`，其余按 proposed/current 差异推导——**`stop_campaign` 会被静默改写为 adjust_budget/keep，无法形成退出执行**
- guardrails（campaign_guardrails.py:208/275）：注释明确"stop_campaign 当前引擎未实现，不做强制淘汰，留给 LLM 判断"

**正确顺序**：① 确定广泛流是否允许产生退出建议；② 若允许，先把 `stop_campaign` 完整接通（模型 → `_normalize_action` 保留 → guardrails → Mapper → ERP 执行 → 前端展示）；③ 接通前，prompt 不得声明其为可用动作，示例保持现状或改中性动作。

### P0-B：输出约束补枚举清单

**位置**：`reasoner.py` 广泛流 prompt 的"输出约束"节（`_CAMPAIGN_BROAD_PROMPT`）。

在"必填结构字段"后直接列出合法枚举表（不从注入 KB 跨文件查找）：
- `action`：keep / adjust_bid / adjust_budget / eliminate_to_low_bid_pool（现状可执行集）。**不含 adjust_placement**（ONT-002 明确广泛/词组/自动禁止广告位调整，诊断报告原枚举含此项为错误）；`stop_campaign` 待代码接通后（P0-A）再加入
- `direction`：`{"bid": "up"|"down"|"keep", "budget": "up"|"down"|"keep"}`（bid/budget 各自独立）
- `triggered_rule`：**仅限 17号问题类型码**（BLOCKED_INVENTORY / SAMPLE_INSUFFICIENT / HIGH_ACOS_* / BUDGET_* / RANK_* / KEYWORD_POOL_* / ALL_HEALTHY 等）；淘汰条件码（21号 condition_code）**不得填入该字段**——二选一：新增独立字段 `condition_code` 承接淘汰条件，或明确命名空间
- `review_level`：AUTO_APPROVED / MANUAL_REVIEW / HIGH_RISK_REVIEW

**修正示例**：广泛流输出示例的 `action` 由 `eliminate_to_low_bid_pool` 改为 keep/adjust 中性示例；`triggered_rule` 示例替换为 17号问题类型码（如 `HIGH_ACOS_NO_ORDER`）；淘汰卡同时携带问题类型与淘汰条件（若新增 condition_code 字段）。

### P0-C：护栏——paused 纳入 guardrails（本期新增）

**解决问题**：暂停动作绕过护栏，核心词/样本不足等活动可能被 LLM 误暂停。

当前 `campaign_guardrails` 只处理 `eliminate_to_low_bid_pool`，paused 不经过任何保护。纳入本期后：

- [ ] `campaign_guardrails.py`：增加 `paused` 分支——
  - **P0 核心词保护**：`is_core=True` 或 29号 核心词所在活动**不得 paused**（与 eliminate 同类保护，`_p0_core_protect` 的 action 判断加 `paused`）
  - **样本不足保护**：活动上线 <3 天或命中 `SAMPLE_INSUFFICIENT` **不得 paused**（与样本保护 `_p1_new_campaign_protect` 对齐）
  - **KB31 §13 停止投放前诊断路径**：高点击无单 → 先否词 → 降 bid/预算 → 仍无改善才允许 paused；paused 建议必须 `HIGH_RISK_REVIEW`（人工确认）
- [ ] 测试：guardrails 对 `paused` 的核心词保护 / 样本保护 / 诊断路径拦截

### P0-D：预算回算——paused 纳入 budget_reallocation（本期新增）

**解决问题**：暂停关停后预算不释放、组合/父级预算不回算——暂停活动空占预算。

当前 `campaign_budget_reallocation.py:47` 只识别 `eliminate_to_low_bid_pool`。纳入本期后：

- [ ] `campaign_budget_reallocation.py`：识别 `paused`——暂停释放金额 = `current_daily_budget`，计入 `other_campaign_budget_decrease`（与停止投放语义一致，见 30号/31号§13）
  - 与 eliminate 的区别：eliminate 还迁低价池（预算 $1 + bid min）并触发复评体系；paused 是纯释放、无迁池、无复评
- [ ] 暂停不进入低价捡漏组复评（reactivate 体系只服务淘汰池）——释放金额不回流低价池
- [ ] 测试：paused 触发预算释放 + 回算，组合/父级预算正确更新

### P0-E：预算资格由代码预计算（审计修正——原"补利用率门槛文案"不完整）

原方案建议在输出约束补"利用率>90% 才可大涨"文案，**审计核实不可行**：当前 prompt **未注入**有效容忍上限、目标CPA、平均订单金额——这些**代码已算好但未透传**（`fill_acos_constraints` campaign.py:1462 填充 `strat_ctx.effective_acos_tolerance/target_cpa/avg_order_value`，prompt 只告诉 LLM"先算容忍度"却没给现成值）。而**近1天/近3天活动级数据暂不支持**（活动级仅 `perf_7d`，无 1d/3d 数据源）。

**改为代码先算/透传预算资格**，LLM 只在资格范围内选幅度和解释：

```text
# 代码已算好 → 透传 prompt（helper 已存在，无需 LLM 重算）
effective_acos_tolerance   # 有效容忍上限（fill_acos_constraints 已算）
target_cpa                 # 目标 CPA（已算）
avg_order_value            # 平均订单金额（已算）

# 代码预计算（当前可算集合）
budget_utilization_tier    # >90% | 70-90% | 50-70% | <50%（现有利率）
budget_increase_eligible   # 布尔：利用率档位 + 库存门槛 是否放行加预算
inventory_gate             # 库存≥30天（strat_ctx.inventory_days）
```

**暂不支持（待数据源）**：`recent_1d_good` / `recent_3d_trend` 依赖近1天/近3天活动级数据（当前无源）——预算幅度判断退化为基于 7 天 + 利用率档位 + 有效容忍上限；数据源接入后再补。

prompt 输出约束改为："加预算仅允许在 `budget_increase_eligible=true` 时；幅度按 `budget_utilization_tier` 档位与 `effective_acos_tolerance` 对照；reason 必须引用代码给出的资格字段，不得自行推导门槛。"

### P1-1：候选标注层桥接 31号§10 准入（审计修正——原"样本不足词一律不标"不采纳）

原方案建议"词级样本不足词不得标为候选"，**审计核实与现行搜索词方案冲突，不采纳**：当前设计明确区分活动级/词级样本不足——词级样本不足词**保留进入审阅**（14 天同键命中时附加 14 天事实作辅助观察），仅 14 天不能独立授权动作；纯低信号词已在取数层（bundle 排序截 20）删除。

修正后的约束：
```
- 标注 ≠ 准入：订单/ACOS 门槛由代码按 23号§3.4 三通道校验，LLM 只判语义相关性与 R1。
- 词级样本不足词（term_sample_insufficient=true）可保留审阅；14 天仅作辅助观察，
  不能独立触发否词/扩词/出价动作；其转精准仍由代码按 7d 校正指标与准入通道决定。
- 活动级样本不足（SKIPPED_CAMPAIGN_SAMPLE_INSUFFICIENT）才禁止输出搜索词来源动作。
```

### P1-2：切片净化——建"广泛流决策投影层"（审计修正——不直接改权威 KB 正文）

**审计建议采纳**：不建议直接大规模修改权威知识库正文（版本记录与规则同文件是维护者的现实约束），而是增加一个**广泛流决策投影层**——由投影层从 KB 提取"当前有效规则"生成注入文本，只向 LLM 注入：

- 当前有效规则（无版本沿革表述）
- 当前活动类型允许/禁止动作集
- 当前搜索词准入语义（23号§3.4 三通道）
- 当前指标窗口及字段口径（7d 基线 / 14d 辅助 / 校正指标）
- 当前输出契约（字段 + 枚举命名空间）

**投影层排除**：版本历史（v3.x.x 修正/重写/失效点、C-xx 编号）、代码/测试/设置名（`settings.*`、`app/core/*.py`、函数名、测试名、文件路径）、ERP 映射、待确认事项（如"待 ERP 确认 archived"）。

**投影来源清单**（对应诊断 1.2 的 6 类噪声）：
1. 版本记录：03号§10 口径沿革 / 03号§12 修正说明 / 10号§1.6-1.12 v3.x.x 说明 / 17号§5.3、19号§1/§4/§8 v3.4.0 注 / 15号§4 C-07 / 30号§1/§2 v3.3.0 失效点 / 28号§1 修正 / 21号§1 冲突说明 → 投影层过滤
2. 代码/测试/设置引用：`settings.campaign_exact_main_daily_spend_threshold`、`test_zero_threshold_must_never_be_treated_as_explicit`、`app/core/attribution_maturity.py`、`fit_curve_from_restatements`、`advert_exec_mapper.py`、`placement_adjustment_allowed()` → 投影层过滤
3. 代码负责的护栏：10号§1.9-1.12（组合超限/断路器/对账/顺序纪律）、30号§3-5（复合动作/生命周期/ERP 映射）→ 投影层过滤
4. 未决待办：30号§5"待 ERP 确认" → 投影层过滤
5. Ontology Contract 卡：只投影 `hard_rules`（ONT-RUNTIME-*）与 `mode_policies` 中 LLM 需遵守部分；`portfolio_groups`/`portfolio_routing`/`execution_layers`/`action_bundle.lifecycle` 过滤
6. 重复定义：17号问题类型表与 19号/21号/31号 交叉表述 → 投影层收敛为单份（不改 KB 原文）

**容量基线**：当前广泛流 KB 注入实测 **59,378 字符 / 1,869 行**（`kb.build_campaign_adjustment('broad', None)`），其中版本历史与实现层引用占比可观——投影层上线后对比 token 长度作为验收指标（见 V4）。

**注意**：投影层改动需同步 `kb_loader.py` preset/`build_campaign_adjustment()` 与 `build_ontology_card()`；改后跑 `test_kb_slicing.py` 与广泛流相关测试 + 注入文本目检。

### P1-3：校正指标接入精准扩词准入（审计新增）

bundle 取数层已携带校正指标（campaign_fetcher.py:53-70：`acos_corrected` / `cvr_corrected` / `sales_corrected` / `data_maturity`），但 `campaign_search_term_promotion.py:29` `_metrics()` 仍直接汇总 raw `orders/cost/sales` 并重算 raw ACOS，订单/词根通道准入也用它——**与 03号§12"ACOS/CVR 判定优先使用成熟度校正后指标"不一致**。

修正：`_metrics()` 改用校正口径（校正后 ACOS/CVR 参与通道判定；校正指标不可用时按 03号§12 标数据缺失而非 raw 顶替），并保留 `data_maturity` 到 evidence。

### P1-4：输出契约与后端代码合法值对齐（用户定夺——对齐后端，非对齐 KB31）

**用户定夺**：输出契约的合法值**以后端代码实际消费为准**（reasoner 解析 + Pydantic 字段 + `_normalize_action` 消费的 `action`/`direction`/`triggered_rule`/`review_level`，即 P0-B 枚举表）——**不是去适配 KB31 字段**。

KB31 的 `action_code`/`physical_action_code`/`branch_hit`/`search_term_action`/`promotion_channel`/`lookback_window` 是知识库业务语言，**代码不消费**；LLM 若输出会被静默忽略（或把 `condition_code` 当 `triggered_rule`）。

修正：① prompt 输出契约 = 后端合法值（P0-B 已完成）；② KB 投影（P1-2）标注 KB31 字段仅为业务规则说明、非本调用点输出字段，**不诱导 LLM 输出代码不消费的字段**。

### P1-5：数值证据窗口无结构化校验（审计新增）

Prompt 要求数字证据标注 7d/14d、renderer 已输出窗口标签，但 LLM 返回的 reason/evidence **没有结构化窗口校验**——模型仍可能把 14 天订单写成"7 天订单"、合并两窗口数字、或在无 14 天数据时推断"14 天无数据"。

修正：候选事实回填与最终落库前保留**结构化窗口事实**（bundle 的 metrics_7d/metrics_14d 作为权威），LLM 文案视为解释文本而非指标来源；落库时用结构化值覆盖文案数字（或至少告警不一致）。

### P1-6：预算利用率为 0 时被省略（审计新增）

`_campaign_to_prompt_dict`（reasoner.py:1709）仅 `cu.current_budget > 0 and p.cost > 0` 时注入 `budget_utilization_pct`——"预算为正但 7 天无花费"的活动**字段缺失**而非显式 0%，共享 prompt 又规定缺失数据不得当 0，LLM 无法区分"已知 0% / 未取到 / 数据缺失"三种状态。

修正：预算利用率为 0 时显式携带 `0`（或 `N/A` + 原因），不得静默省略。

### P2-1：解析端枚举校验 + 显式告警

**位置**：`campaign.py` 解析端（`CampaignAdjustmentItem(**adj)` 之后）与 `_normalize_action`。

- `triggered_rule` / `review_level` 增加合法值校验：非法值 → warning + 按保守默认（review_level → MANUAL_REVIEW；triggered_rule → 空串或"UNKNOWN"）落库，**不静默放行**
- `_normalize_action` 改写时记 warning（当前只记统计计数，LLM 不知情）
- `direction` 值域校验：非 up/down/keep → 清空并 warning

### P2-2：候选准入日志观测（待实测项）

- 记录：LLM 标注候选数 vs 代码准入通过数 vs 词根通道通过数（含 OR 逻辑命中），用于确认"出单 1-2 单词是否漏过准入"
- 若确认词根通道 OR 逻辑放宽是主因，与运营确认是否收紧为 AND（订单≥3 **且** ACOS≤目标）

---

## 四、实施顺序建议

> P0-A 动作契约定案、P0-B 枚举清单 **已完成**。以下为剩余项顺序：

1. **P0-C 护栏**（paused 纳入 guardrails：核心词/样本保护 + KB31§13 诊断路径）→ 单测覆盖
2. **P0-D 预算回算**（paused 纳入 budget_reallocation：预算释放 + 回算）→ 单测覆盖
3. **P0-E 预算资格代码化**（prompt + 资格预计算，可快速验证）→ 跑广泛流相关测试 + 样例 prompt 目检
4. **P1-1 候选标注桥接** + **P1-5 窗口事实结构化** + **P1-6 预算利用率 0 显式化**（prompt + 解析端小改）→ 同上
5. **P1-3 校正指标接入准入**（campaign_search_term_promotion._metrics 改校正口径）→ 单测覆盖
6. **P1-2 投影层**（改动面最大）→ kb_loader 投影 + `test_kb_slicing.py` 回归 + 注入文本目检 + token 对比
7. **P1-4 输出契约收敛** + **P2-1 解析端校验** → 单元测试覆盖非法值路径
8. **P2-2 观测**（含跨活动聚合统计）→ 上线后看日志

每步独立 commit，便于回滚。P0 三项先行，P1 按依赖顺序，P2 最后。

---

## 五、待核实项（实现前用 Grep/日志实测）

- [ ] V1：最终建议中是否真实出现订单 1-2 的扩精准词（**区分 6 环节**：LLM 标注 / 订单通道 / 词根通道 / 已有精准词排除 / 跨活动合流 / 最终截断——不能仅凭 LLM 输出下结论）
- [ ] V2：词根通道 OR 逻辑（订单≥3 或 ACOS≤目标）与**跨活动聚合**在真实数据中的命中占比
- [ ] V3：`triggered_rule` 混用问题类型码/淘汰条件码（condition_code）在落库/展示中的实际占比
- [ ] V4：投影层上线后 prompt token 长度对比（基线 59,378 字符/1,869 行，量化"版本记录/代码引用"的浪费）
