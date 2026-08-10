# Campaign Portfolio / Target Card / Pending 语义修复实施方案

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修正活动当前组合、确定性目标组、建议卡和 Pending 的语义与落库边界，消除 `campaign_group_type` 写入原始组合名导致的 1406，并保证“需要挪组才写 Pending”的执行语义。

**Architecture:** 分析阶段只做一次当前组合的子串归一化，并由确定性规则生成目标组 ERP 码。建议卡始终保存目标组码，当前组保存归一化码或无法识别时的原始组合名；只有当前值与目标码不一致时才创建/填充 `campaign_pending.target_campaign_group_type`。执行阶段只消费 Pending 中的目标码，再把目标码匹配到真实 portfolioId；产出阶段不查询或匹配目标组合。

**Tech Stack:** Python 3.13, Pydantic models, MySQL ERP writer, pytest, existing Campaign MCP/ERP execution mapper.

---

## 一、诊断报告

### 1. 三个字段的正确语义

| 业务概念 | 载体 | 正确值 | 产生/消费位置 |
|---|---|---|---|
| 当前组合 `current_portfolio` | `CampaignUnit.current_portfolio_name` → card.`current_portfolio` | 原始 MCP 组合名命中四个标准组时写对应 ERP 码；未命中时写原始值 | 分析产出、卡片展示；不用于匹配目标组合 |
| 目标组 `target_group_type` | card.`campaign_group_type`；Pending.`target_campaign_group_type` | 始终是四个 ERP 码之一 | 确定性规则产生；卡片始终保存；Pending 只在实际需要迁移时保存 |
| 目标组执行匹配 | 无需新增列 | 目标 ERP 码 → 中文组名 → 真实 portfolioId | `advert_execution._resolve_modify_portfolios()`，只在执行阶段发生 |

### 2. 1406 的直接原因

当前 `canonicalize_payload()` 先读取 target，但随后在 target 为空时回落到当前组合：

- `mappers.py:456-462` 将 `target_campaign_group_type` 与 `current_portfolio` 混合，并执行 `campaign_group_type = target or map(current)`。
- `text_utils.py:176-182` 的 `_map_single()` 对未知值原样返回。因此 `宽三角比基尼-精准主力组` 这类未被完整枚举表命中的组合名会原样进入 `campaign_group_type`。
- ERP 表的 `campaign_group_type` 是 `varchar(32)`；最终在 `repository.py:1513` 的 card INSERT 处触发 `Data too long for column 'campaign_group_type'`。

因此，问题不是简单扩大 `campaign_group_type`，而是把“当前组合”和“目标组”混成了一个字段，并把 target 为空错误地解释为“当前组就是有效组”。

### 3. 当前链路中已确认的边界

- MCP 当前组合名称在 `campaign_fetcher.py:1591-1592` 进入 `CampaignUnit.current_portfolio_name/current_group_type`。
- `_reconcile_portfolio_targets()` 在 `campaign.py:2173` 后为广泛和精准项补目标；当前实现却在 `campaign.py:2203`、`2223` 把部分 current 改成标准中文标签，无法表达“标准码或原始值”的统一契约。
- 当前执行侧已经符合“消费 target 后再找真实 portfolio”的方向：`advert_execution.py:_resolve_modify_portfolios()` 将 ERP 码反向映射后调用 `match_unique_portfolio()`；本方案不把这一步提前到分析或 mapper。
- 当前测试明确禁止用伪造 ID 补现有活动：`tests/persistence/test_erp_warehouse_ids.py:94-115` 断言缺少 `campaign_id` 时不生成卡片。该契约必须保留，不能用 stable hash 冒充 Amazon/Doris 活动 ID。

### 4. 现有方案的关键错误（本方案不采用）

1. 新增 `classified_group_code` 并将 current 改为无限制原始值，会继续把原始值写入 `current_portfolio`；现有 DDL 与脚本将该列定义为 `varchar(32)` 的“当前组 ERP 码”（知识图谱 `02-数据库表结构说明-ERP库与State库.md:1209`、`scripts/erp_db/migrate_card_current_portfolio.sql:5`）。不改列长度仍有第二次 1406 风险。
2. 用合成 `campaign_id` 解决 mapper 丢项会把伪 ID 写进 card/pending；`advert_exec_mapper.py:143-168` 会把 Pending 的 `campaign_id`直接组成 MCP `campaignId`。这不是可追溯补救，而是可能向广告平台发送不存在的活动 ID，必须禁止。
3. 只在 target 为空时生成 card group 会保留错误前提；target 必须先产出并落 card，Pending 是否写入由 current 与 target 的比较决定。
4. 若 current 是未识别原始值，不能再对它做 target 子串匹配。target 已经是 ERP 码；需要匹配真实组合的动作只能在执行阶段根据 target 码做一次 portfolioId 解析。

## 二、修复后的不变量

1. `campaign_group_type` 永远是以下四个值之一：
   - `exact_core_group`
   - `exact_testing_group`
   - `auto_broad_group`
   - `low_bid_retention_group`
2. `card.current_portfolio` 为 `normalize_current_portfolio(raw_name)`：
   - 原始名称子串命中标准组 → 对应 ERP 码；
   - 未命中 → 原始名称本身；
   - 空值 → `NULL`。
3. `target_campaign_group_type` 在确定性产出阶段始终得到一个合法 ERP 码，并始终落 card.`campaign_group_type`。
4. 仅当 `current_portfolio != target_campaign_group_type` 时，才在 `campaign_pending.target_campaign_group_type` 写目标码；相等时 Pending 目标列保持 `NULL`，但 card 仍保存目标码。
5. current 的子串匹配只发生在 `normalize_current_portfolio()`；target 不参与子串匹配。
6. 缺少真实 `campaign_id` 的现有活动不生成可执行 card/pending，也不生成合成 Amazon ID；保留现有跳过契约。
7. 执行侧只读取 Pending 目标码，反向映射到组名并匹配一次真实 portfolioId；分析侧不新增 MCP 查询。

## 三、目标组判定规则

新增一个唯一公共 helper（建议放在 `campaign_portfolio.py`），避免在 mapper、预算回算、护栏中复制不同口径：

```python
def normalize_current_portfolio(raw_name: str | None) -> str:
    """标准组合名按子串映射成 ERP 码；未知组合原样保留。"""

def default_target_group_code(match_type: str | None) -> str:
    """非精准默认自动广泛；精准默认精准测试。"""

def group_code_to_label(code: str | None) -> str:
    """仅供预算/护栏内部把已归一化码转回现有中文组键。"""
```

规则顺序：

1. `current = normalize_current_portfolio(unit.current_portfolio_name)`。
2. 若有 32 号确定性迁移结果，`target = exact_group_targets[campaign_id]`。
3. 否则，BROAD/PHRASE/AUTO 的 `target = auto_broad_group`。
4. 否则若 current 已是四个 ERP 码，`target = current`（保持现组，不造 Pending）。
5. 否则 EXACT 的 `target = exact_testing_group`；其他未知匹配类型按非精准规则使用 `auto_broad_group`。
6. `pending_target = target if current != target else None`。

特别是未识别 current：

- 非精准活动：current 保留原始组合名，target=`auto_broad_group`，必写 Pending；
- 精准活动：current 保留原始组合名，target=`exact_testing_group`，必写 Pending。

## 四、文件级修复方案

### Task 1: 建立 current/target 单一归一化 helper

**Files:**

- Modify: `ad-direction-agent/app/workflow/steps/campaign_portfolio.py`
- Test: `ad-direction-agent/tests/workflow/test_portfolio_match_and_exec.py`
- Test: `ad-direction-agent/tests/workflow/test_campaign_group_semantics.py`（新建）

- [ ] **Step 1: 先写失败测试**

覆盖：标准名称子串映射到四个 ERP 码、未知名称原样返回、空值为空、默认目标组按 EXACT/非 EXACT 分界。

- [ ] **Step 2: 实现 helper**

复用现有 `find_portfolio_group_matches()`、`GROUP_LABEL_TO_CODE`、`GROUP_CODE_TO_LABEL`，不再新增第二套组枚举。

- [ ] **Step 3: 运行测试**

```powershell
py -m pytest -q tests/workflow/test_campaign_group_semantics.py
```

Expected: 新 helper 测试全部通过。

### Task 2: 在分析收尾阶段产出 current 与 target

**Files:**

- Modify: `ad-direction-agent/app/models/campaign.py`
- Modify: `ad-direction-agent/app/workflow/steps/campaign.py`
- Modify: `ad-direction-agent/app/workflow/steps/campaign_restart.py`
- Test: `ad-direction-agent/tests/workflow/test_portfolio_match_and_exec.py`

- [ ] **Step 1: 更新模型注释，不新增 `classified_group_code`**

将 `CampaignAdjustmentItem.current_portfolio` 注释改为“标准组 ERP 码或未识别时的原始组合名”，将 `target_campaign_group_type` 注释改为“确定性目标码，始终为四码之一”。

- [ ] **Step 2: 修改 `_reconcile_portfolio_targets()`**

函数接收 `exact_group_targets`，对每个 adjustment 无条件设置 `current_portfolio` 和 `target_campaign_group_type`。不读取或信任 LLM 的 portfolio 字段，不把 target 当当前组合。

- [ ] **Step 3: 处理补建和复评项**

广泛补建项沿用同一 helper；`campaign_restart._build_item()` 使用真实 `CampaignUnit.current_portfolio_name` 归一化为 current，并把复评后的目标组作为 target，避免复评项绕过统一契约。

- [ ] **Step 4: 保持确定性目标向后兼容**

`CampaignAnalysisResult.campaign_group_targets` 可以继续输出，但只作为已有 exact transition 的兼容载体；当 adjustment 自身已有确定性 target 时，mapper 以 adjustment 字段为准，不能被 current 覆盖。

### Task 3: 修复 mapper 的 card/pending 分流

**Files:**

- Modify: `ad-direction-agent/app/persistence/erp_writer/mappers.py`
- Modify: `ad-direction-agent/app/persistence/erp_writer/models.py`
- Test: `ad-direction-agent/tests/persistence/test_campaign_group_semantics.py`（新建）
- Test: `ad-direction-agent/tests/persistence/test_erp_warehouse_ids.py`

- [ ] **Step 1: 删除 current 回落**

删除 `campaign_group_type = target or map(current)`。card 的 `campaign_group_type` 只读取合法 target；兼容旧 payload 时只能用确定性 helper 生成 target，不能把任意 current 原样写入 card group。

- [ ] **Step 2: 归一化 card.current_portfolio**

使用 `normalize_current_portfolio(portfolio_label)`；标准组合落 ERP 码，未知组合保留原始值，不调用“未知值原样返回”的 `map_campaign_group_type()` 作为 card group 生成逻辑。

- [ ] **Step 3: 按 current/target 比较生成 Pending**

先聚合活动的 target 与 current，再把 `target_campaign_group_type` 写入已有 `campaign_pending` 行；只有二者不相等才写。无预算/状态动作但确实挪组时，仍创建一个空预算/状态的活动级 Pending 占位。

- [ ] **Step 4: 禁止合成现有活动 ID**

保留缺少真实 `campaign_id` 的跳过行为，不把 `stable_id()` 结果赋给现有活动 card 或 pending。若要留痕，只能走现有不可执行灰卡契约，不能进入 `advert_exec_mapper` 的已有活动 `campaignVoList`。

- [ ] **Step 5: 更新 mapper 测试**

验证：

- current=`宽三角比基尼-精准主力组`、target=`exact_core_group` → card group 为 `exact_core_group`，current 为 `exact_core_group`，不报长度错误；
- current=`自定义组合`、target=`auto_broad_group` → card group 为 `auto_broad_group`，current 保留自定义组合，Pending 写 target；
- current=`auto_broad_group`、target=`auto_broad_group` → card 仍有 group，Pending 不写 target；
- payload 缺 campaign_id → 不产生可执行现有活动 card/pending。

### Task 4: 让预算回算、护栏和摘要消费新的 current 形态

**Files:**

- Modify: `ad-direction-agent/app/workflow/steps/campaign_budget_reallocation.py`
- Modify: `ad-direction-agent/app/workflow/steps/campaign_guardrails.py`
- Modify: `ad-direction-agent/app/persistence/erp_writer/mappers.py`
- Test: `ad-direction-agent/tests/workflow/test_budget_reallocation.py`
- Test: `ad-direction-agent/tests/workflow/test_campaign_group_semantics.py`

- [ ] **Step 1: 统一内部组键**

预算回算和 P12 护栏内部仍使用现有中文组键时，先调用 `group_code_to_label()`；current 是未知原始值时，按已有 match/action 兜底，不对 target 做 current 子串匹配。

- [ ] **Step 2: 修复摘要统计**

`_portfolio_groups_from_payload()` 先把标准 current ERP 码转成中文组键，再做统计；未知 current 的活动按其确定性 target 归入结果组，避免 raw 值导致计数被静默丢弃。

- [ ] **Step 3: 回归预算/护栏行为**

增加“标准 current 码”和“未知 current + 必须迁移”的断言，确认暂停释放、淘汰释放、P12 组合利用率和现有预算守恒不变。

### Task 5: 调整数据库与知识图谱口径

**Files:**

- Create: `ad-direction-agent/scripts/erp_db/alter_card_current_portfolio_raw.sql`
- Modify: `docs/AD-Agent Project Knowledge Graph/02-数据库表结构说明-ERP库与State库.md`
- Modify: `ad-direction-agent/app/persistence/erp_writer/models.py`

- [ ] **Step 1: 扩大 current_portfolio 列**

保持 `campaign_group_type` 为 `varchar(32)`；将 `current_portfolio` 从 `varchar(32)` 改为 `varchar(512)`，注释改为“标准组 ERP 码或 MCP 原始组合名”。迁移脚本使用：

```sql
ALTER TABLE t_advert_agent_modify_suggest_card
    MODIFY COLUMN current_portfolio VARCHAR(512)
    DEFAULT NULL COMMENT '当前组合：标准组ERP码或MCP原始组合名';
```

执行前先在目标库 `SHOW CREATE TABLE` 和 `information_schema.COLUMNS` 核对列现状；本方案不把 schema 文档当作生产已执行证据。

- [ ] **Step 2: 同步知识图谱**

更新 card 字段表、数据流表和执行链路说明：card.`campaign_group_type` 是 target，不再写 current；card.`current_portfolio` 可为 ERP 码或原始值；Pending target 仅表示确实需要迁组。

### Task 6: 验证完整链路

**Files:**

- Test: `ad-direction-agent/tests/workflow/test_portfolio_match_and_exec.py`
- Test: `ad-direction-agent/tests/persistence/test_campaign_group_semantics.py`
- Test: `ad-direction-agent/tests/persistence/test_erp_warehouse_ids.py`
- Test: `ad-direction-agent/tests/workflow/test_budget_reallocation.py`

- [ ] **Step 1: 运行静态与单元验证**

```powershell
py -m compileall app
py -m pytest -q tests/workflow/test_portfolio_match_and_exec.py tests/persistence/test_campaign_group_semantics.py tests/persistence/test_erp_warehouse_ids.py tests/workflow/test_budget_reallocation.py
```

Expected: 现有回归与新增语义测试全部通过；缺少真实 campaign_id 的测试仍断言不会生成可执行现有活动。

- [ ] **Step 2: 检查 SQL 参数**

用 fake cursor 断言 card INSERT 的 `campaign_group_type` 始终属于四码集合，`current_portfolio` 接收标准码或原始值，campaign pending 的 target 仅在 current/target 不一致时非空。

- [ ] **Step 3: 执行前验证**

用一条真实分析 payload（包含 `宽三角比基尼-精准主力组`）跑 `canonicalize_payload()` 和 fake `write_full()`；确认不再向 `campaign_group_type` 传入长中文组合名。生产库只在迁移脚本经人工核对后执行，不在本方案中自动改库或重启服务。

## 五、验收标准

1. 再现日志中的 payload 时，`card.campaign_group_type` 是 `exact_core_group`/`exact_testing_group`/`auto_broad_group`/`low_bid_retention_group` 之一，1406 不再出现。
2. 标准 current 组合保存为对应 ERP 码；非标准 current 保存原始 MCP 名称，且不被当作 target 参与匹配。
3. target 即使与 current 一致也落 card；一致时不生成 Pending target，不一致时 Pending target 必须是合法 ERP 码。
4. 非标准 current 的非精准活动默认 target=`auto_broad_group` 并产生 Pending；精准活动默认 target=`exact_testing_group` 并产生 Pending。
5. 执行阶段只用 Pending target 码匹配真实 portfolioId；分析和落库阶段不调用目标 portfolio 查询。
6. 旧 exact transition、广泛收拢、暂停/淘汰、预算回算、P12 护栏和复评链路的既有行为通过回归测试。
7. 缺少真实 campaign_id 的现有活动不会因合成 ID 进入执行 MCP。

## 六、明确不做的改动

- 不新增 `classified_group_code` 字段；它与本业务的 target 语义重复，会制造第三个组别来源。
- 不扩大 `campaign_group_type` 让它容纳原始组合名；该列继续保持四码契约。
- 不在产出阶段把 target 码匹配真实 portfolioId；不新增 MCP 调用。
- 不用合成 ID 修复现有活动身份缺失；身份缺失仍按不可执行/跳过契约处理。
- 不改 taskId、pending 状态机或执行 MCP 的异步轮询机制。

