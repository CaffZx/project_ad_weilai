# 广泛类活动暂停（paused_campaign）全链路接线方案 v2

> 记录时间：2026-08-05。决策：本期让广泛类活动支持输出"暂停"动作。
> 分两期：**本期**只做后端功能支持（代码能解析/落库/执行/前端显示 paused）；**延后**做提示词治理与 LLM 真正产出。
> **业务定夺（2026-08-05）**：亚马逊客观上不存在 `stop`（archived）动作，`stop` 就等于 `paused`。知识库 30号 区分的 `stop_campaign`(archived) / `pause_campaign`(paused) **是错误表述**，需修正。因此：
> - **LLM 动作码统一用 `paused_campaign`**（不用 `stop_campaign`）
> - 映射：LLM `paused_campaign` → 归一化层翻译为 `paused` → 后端/落库/执行/前端统一感知 `paused`/`PAUSED`
> - 知识库修正（30号 及相关引用 `stop_campaign` 的章节）单独列任务，见 §10

---

## 1. 现状：两个链路，多张表

前端渲染与执行是**两个独立链路**，各自涉及多张 ERP 表——不是"两张表"。

### 1.1 前端渲染链路（分析结果 → 落库 → snapshot 读回 → 前端渲染）

| 表 | 角色 |
|---|---|
| `t_advert_agent_decision` | 决策总表（id=decision_id） |
| `t_advert_agent_modify_suggest_summary` | 汇总 |
| `t_advert_agent_modify_suggest_card` | 展示卡主体：`suggest_category` + `description` + `trigger_rule`/`confidence`/数值 |
| `t_advert_agent_modify_campaign_pending` / `keyword_pending` / `placement_pending` | 3 张 pending 也进 snapshot |

读回：`repository.read_snapshot` → `campaign_viewmodel.from_db_snapshot` → 前端。

**注意**：`suggest_category` 是 **`varchar(32)` 自由文本**（[DDL 证据](docs/AD-Agent Project Knowledge Graph/02-数据库表结构说明-ERP库与State库.md:1207)），非枚举列——新增 `PAUSED` 分类**零 DDL 变更**。

### 1.2 执行链路（pending 表 → 读 → MCP 构参 → ERP 异步执行）

`t_advert_agent_modify_campaign_pending`（活动级 `new_state`）/ `keyword_pending` / `placement_pending` → `advert_execution.submit_execution` → `advert_exec_mapper.build_exec_plan` → `agent_async_batch_update_advert`。

### 1.3 当前 eliminate 链路（对照模板）

```
LLM 输出 action=eliminate_to_low_bid_pool
  → _normalize_action (campaign.py:2485/2697)：eliminate 特殊保留（不推导覆盖）
  → mappers._ACTION_TO_CATEGORY："eliminate_to_low_bid_pool" → "ELIMINATE"
  → suggest_card.suggest_category="ELIMINATE" → 前端"淘汰"
  → CampaignPendingCanonical：复合动作（move_portfolio + 预算$1 + bid min）
```

---

## 2. MCP server 端 schema 证据（已实测确认）

服务器 `tools/list` 只读探针取得 `agent_async_batch_update_advert` 原始 inputSchema：

```json
"campaignState": { "type": "string", "description": "活动状态: enabled(启用) / paused(暂停)" }
"keywordState":  { "type": "string", "description": "关键词状态: enabled(启用) / paused(暂停)" }
```

**结论**：传输层 `campaignState` 合法值 = `enabled`/`paused`；代码 `_norm_state`（advert_exec_mapper.py:37-47）已保留 `paused`，`build_exec_plan` 已能生成 `campaignState:"paused"`（现有测试 [test_advert_exec_child_asin.py:233](ad-direction-agent/tests/test_advert_exec_child_asin.py#L233) 验证过）。**执行层 MCP 构参零新增改动**。

---

## 3. 命名口径（用户定夺 + 审计修正）

| 层 | 值 |
|---|---|
| LLM 语境动作码 | **`paused_campaign`**（prompt/LLM 输出；不出现 `stop_campaign`） |
| 归一化后 action | `paused` |
| `suggest_category`（展示分类码） | **`PAUSED`**（与 `ELIMINATE` 区分，保护淘汰复评 `WHERE='ELIMINATE'` 查询） |
| `campaign_pending.new_state` | `paused`（→ MCP `campaignState:"paused"`） |
| 前端 action | `paused` → `_actionLabel` 显示"暂停" |

**单一翻译点**：LLM `paused_campaign` 出 LLM 即在归一化层翻译为 `paused`，此后后端各层/前端只处理 `paused`/`PAUSED`。**不存在 `paused_campaign`/`stop_campaign` 直接落库的链路。**

---

## 4. 落库枚举定夺

| 链路 | 表 | 字段 | 取值 |
|---|---|---|---|
| 前端渲染 | `modify_suggest_card` | `suggest_category` | **`PAUSED`** |
| 前端渲染 | `modify_suggest_card` | `description` | "活动关停（暂停）" |
| 执行 | `modify_campaign_pending` | `new_state` | **`paused`** |
| 执行 | `modify_campaign_pending` | `old_budget/new_budget` | 不传/None（暂停不改预算） |

> **old_state 不处理**：`t_advert_agent_modify_campaign_pending.old_state` 列存在，但现有 eliminate/adjust 生成 pending 时本就传 `old_state=None`（mappers.py:290-315）；暂停同样留 None，**不引入数据源、不写默认值逻辑**。能被分析的活动处于 enabled 运行态，old_state 对暂停无实际意义。

**与淘汰的关系**：淘汰（`eliminate_to_low_bid_pool`→`ELIMINATE`）是"迁入低价捡漏组"复合动作；暂停（`paused_campaign`→`paused`→`PAUSED`）是"关停暂停"。后端枚举统一（`paused`/`PAUSED`），前端渲染/筛选**同级**（同为退出类、共用红色警示样式），仅文案不同："暂停" vs "淘汰"。

---

## 5. 本期后端功能支持改动清单（按审计 P0/P1 补全）

### 5.1 归一化层（P0-1：二次归一化会把 paused 改回 keep）

`_normalize_action`（campaign.py:2485）在开头同时处理翻译 + 保留，**保证一次（:1883）与二次（:2697）归一化都不改写**：

```python
if item.action == "paused_campaign":
    item.action = "paused"        # LLM 动作码 → 后端统一枚举（唯一翻译点）
    return False                   # paused 不参与 proposed/current 推导
if item.action in ("eliminate_to_low_bid_pool", "paused"):
    return False                   # 已翻译的 paused 也保留（二次归一化安全）
```

> 审计证据：campaign.py:2691 注释"淘汰组 action 不变"只豁免 eliminate；第二次 `_normalize_action`（:2697）会把 `paused` 按 proposed==current 推导成 `keep`。**必须把 `paused` 加入保留集。**

### 5.2 护栏层（拍板：本期先不管，延后处理）

- [ ] `campaign_guardrails.py` 当前注释"stop_campaign 未实现"→ 更新为"已接入（paused）"。**本期护栏不处理 paused**（应当经过护栏，但本期不实现 paused 的 guardrails 分支，延后立项）；将来接入点在 guardrails 的 action 判断处补 `paused` 分支即可。

### 5.3 落库分流（P0-2：新增"纯状态"通道，非现有 budget/bid 分支）

> **关键事实**：常规链路 `_pending_lists_from_adjustment`（mappers.py:271-317）**只在 budget/bid 任一非 None 时生成 pending**；`campaign_pending.new_state` 在常规链路**恒 None**（全库写 new_state 的只有 keyword "NEGATIVE" 和 CREATE "ENABLED"）。因此 paused 只写 new_state、不填 budget/bid 时，现有函数一个 pending 都不会生成，paused 会**直接消失**——必须在 `_pending_lists_from_adjustment` **之前**新增一个 state 专用前置分支。

- [ ] `mappers.py` `_ACTION_TO_CATEGORY`：加 `"paused": "PAUSED"`
- [ ] `mappers.py` `_CATEGORY_PRIORITY`：加 `"PAUSED": 0`（与 ELIMINATE 同级）
- [ ] **state 专用通道（核心改动）**：在 adjustments → pending 生成处（mappers.py:271 之前）判断 `action == "paused"`：**直接构造** `CampaignPendingCanonical(new_state="paused")`（不填 budget/bid/placement/keyword），`suggest_card.suggest_category="PAUSED"`。**同时不填 `target_campaign_group_type`**（暂停不触发组合迁移）——分析层 `_reconcile_portfolio_targets`（campaign.py:2095-2115）会给广泛活动填 `target_campaign_group_type`，若状态通道把它带进 pending，MCP vo 会多出 `campaignGroupType`（advert_exec_mapper.py:128 非空即写）。**这是新增的状态数据入口**（类比立即退出链路 decision.py:389 的独立分类构造），不经过 `_pending_lists_from_adjustment` 的 budget/bid 判断。
  > 审计证据：mappers.py:271-317 仅 budget/bid 非 None 生成 pending；paused 纯状态不填数值 → 现有函数零生成。广泛流 prompt（reasoner.py:409）要求 proposed 必填数值，更需前置分支阻断默认 pending 生成。

### 5.4 前端渲染链路映射点补全（P0-4）

- [ ] `mappers.py` `_ACTION_TO_CATEGORY`：`"paused": "PAUSED"`（§5.3）
- [ ] `campaign_viewmodel.py` `_snapshot_action`（:86，**主渲染落点，必须改**）：加 `if cat == "PAUSED": return "paused"`——否则 PAUSED 卡落 `:106 return "adjust"` 兜底，前端显示"调整"
  > 审计修正：主渲染链路 action 派生是 `_snapshot_action`（campaign_viewmodel.py:86-106），**不是** `_CAT_TO_ACTION`（:331 仅用于 synthesis reason_group 分组的 action 字段，fallback "adjust_bid"）。`_CAT_TO_ACTION` 加 `"PAUSED": "paused"` 为顺带项（reason_group 分组准确），不影响主渲染。
- [ ] `campaign_viewmodel.py` `_action_klass`（:23）：加 `if action == "paused": return "eliminate_or_paused"`（否则落 `skipped`）
- [ ] `campaign_viewmodel.py` `_CAT_TO_ACTION`（:331）：加 `"PAUSED": "paused"`（**顺带项**：仅影响 synthesis reason_group 分组的 action 字段，非主渲染）
- [ ] `scripts/erp_db/db_health_check.py`（:123）：`VALID_CAT` 加 `"PAUSED"`
- [ ] `repository.py:1671`：reason group 汇总映射 `_cat = {"eliminate_to_low_bid_pool": "ELIMINATE", "keep": "KEEP"}` 加 `"paused": "PAUSED"`（否则 reason group 分组落未知默认）
- [ ] 前端 demo（**防误显，非美化**）：`render.js` `_actionLabel` 加 `paused: '暂停'`（不加映射则 fallback 显示英文 `"paused"`）；`_badgeKlass` 返回中性 `'eliminate_or_paused'`（paused 与 eliminate 共用）；`viewmodel.js ACTION_KLASS_MAP` 加 `'paused': 'eliminate_or_paused'`（不加则落 `'skipped'` 丢失样式）
- [ ] 前端筛选器：paused 与 eliminate 归同一筛选/分组，仅文案不同

### 5.5 汇总统计桶（P1-5）

- [ ] `campaign.py:1217` 区域：加 `"to_paused": sum(1 for a in adjustments if a.action == "paused")`
- [ ] `repository.py:1189` 区域：桶集合校验把 `paused` 计入（否则 `declared_total != categorized_total` 触发 summary 总数按缺失桶修正）

### 5.6 执行层（MCP 构参）

> **先例**：立即退出链路已产出 `action_kind="PAUSE"` → `campaign_pending.new_state="paused"`（decision.py:283-298/389）→ 同走 `submit_execution`（decision.py:912）→ `_norm_state` → MCP `campaignState:"paused"`。**执行侧 paused 通道已被生产先例验证**；区别仅在 pending 生成通道（立即退出有独立分类构造，不走 `_pending_lists_from_adjustment`）。

- [ ] `advert_exec_mapper.py` `_norm_state`：**零改动**（已保留 `paused`，立即退出先例验证过）
- [ ] `advert_exec_mapper.py` `build_exec_plan`：**代码无需改**。执行构参路径**已有测试覆盖**——[test_advert_exec_child_asin.py:187](ad-direction-agent/tests/test_advert_exec_child_asin.py#L187) `test_immediate_exit_maps_loaded_confirmed_pending_shape` 构造 `new_state="paused"` 的 pending 并断言 `campaignState=="paused"`（L294）。
  > **核实修正**：本轮审计称"该分支无任何测试覆盖"**不准确**（构参有覆盖，走立即退出 pending 形状）；真正缺覆盖的是**常规分析链路**的 paused 状态通道（paused_campaign → 归一化 → mappers 状态通道 → pending），由 T2 补。
- [ ] `advert_execution.py`：确认 submit_execution 对"仅状态变更"正常执行（立即退出先例证明可行，测试固化）

### 5.7 后端 viewmodel（snapshot → 前端）

- [ ] `campaign_viewmodel.py`：`PAUSED` → `paused`（§5.4 已含）

---

## 6. 本期边界声明（P1-9：不含预算回算）

**本期只实现"暂停状态下发"链路**（LLM → 归一化 → 落库 → 前端显示 → MCP `campaignState:"paused"`）。

**不含**：
- 暂停的预算释放/回算（`campaign_budget_reallocation.py:47` 目前只识别 `eliminate_to_low_bid_pool`；暂停不触发回算，属后续语义）
- 暂停的复盘/恢复（reactivate 体系只服务淘汰池）

知识库 30号 的 `stop_campaign` 语义修正后，预算回算对 `paused` 的处理另行立项，不在本期。

---

## 7. 延后项（提示词治理 / LLM 产出）

- [ ] `reasoner.py` 广泛流输出约束：把 `paused_campaign` 加入 action 合法枚举（与 P0-A 诊断报告枚举清单一起做）；LLM 产出 `paused_campaign` 后由归一化层（5.1）翻译为 `paused`
- [ ] 知识库投影层对齐：广泛/词组/自动退出动作统一 `paused_campaign`（不再提 `stop_campaign`）
- [ ] LLM 真正产出 `paused_campaign` 后端到端回归

---

## 8. 测试计划

- [ ] **T1 归一化（完整链路）**：`_normalize_action` 对 `paused_campaign` → `paused` 且不被改写；**完整链路测试**（首次归一化 → 护栏 → 终态归一化 :2697）断言终态仍为 `paused`（非 keep/adjust）
- [ ] **T2 落库分类**：`paused` → `suggest_category="PAUSED"`、`campaign_pending.new_state="paused"`；**不产生** keyword/placement/budget/Bid pending（即使 LLM 填了 proposed 数值）
- [ ] **T3 执行构参**：new_state="paused" → MCP vo **仅含** `{campaignId, campaignState:"paused"}`——**不含** budget/bid/placement，**也不含 `campaignGroupType`**（状态通道不填 `target_campaign_group_type`，暂停不触发组合迁移）
- [ ] **T4 快照读回（全映射链）**：`suggest_category=PAUSED` → `_snapshot_action`→"paused" → `_action_klass`→"eliminate_or_paused" → reason group `_cat`（repository.py:1671）→"PAUSED" → 前端 action="paused"
- [ ] **T5 前端**：_actionLabel "暂停"、badge `eliminate_or_paused`、筛选器与淘汰同级
- [ ] **T6 统计桶**：to_paused 计入，`declared_total == categorized_total`
- [ ] **T7 健康检查**：db_health_check 对 PAUSED 不报 INVALID

回归：`test_campaign_cancellation_fencing.py`、`test_campaign_guardrails.py`、`test_advert_exec_child_asin.py`、erp_writer 落库相关测试。

---

## 9. 待确认项

- [ ] 前端 demo 归属：demo/campaign-panel 是本地演示面板；生产前端需同步"暂停"文案与 `eliminate_or_paused` 样式（确认落点）
- [ ] ERP 侧关停行为：`campaignState:"paused"` 后活动是否整体停（关键词是否需逐条 paused）——本期只传 campaignState，由 ERP 执行语义决定

---

## 10. 知识库修正任务（业务定夺：亚马逊无 stop，只有 paused）

知识库 30号（动作词表）及引用 `stop_campaign`/`archived` 的章节为**错误表述**，需修正为"暂停= `paused`"统一口径：

- [ ] `docs/knowledge_base/30-动作词表与映射.md`：删除/改写 `stop_campaign`(archived) 条目与 §2 的 stop/pause 区分表（:60、:157）——统一为 `paused_campaign`（LLM 动作码）→ `paused`（物理）
- [ ] `docs/knowledge_base/执行规则/31-广泛自动词组调整规则.md` §13：`stop_campaign` 退出表述 → `paused_campaign`
- [ ] `docs/knowledge_base/10-安全护栏.md` §1.6：`stop_campaign`/`pause_campaign` 区分表述 → 统一 paused
- [ ] `docs/knowledge_base/执行规则/18-广告执行调整流程.md`：引用 `stop_campaign` 处 → 统一
- [ ] `docs/knowledge_base/ontology/actions.yaml`：`stop_campaign` 动作码 → `paused_campaign`（LLM 侧）
- [ ] 以上修正后，`campaign_budget_reallocation.py` 对 `paused` 的预算回算语义另行立项（本期不含）

> 知识库修正与代码本期解耦：代码本期先支持 `paused_campaign`→`paused` 链路；知识库/投影层/提示词统一为 `paused_campaign` 在延后项与本节完成。
