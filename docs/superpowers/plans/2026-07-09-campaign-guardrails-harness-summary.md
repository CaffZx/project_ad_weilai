# Campaign 护栏体系 + Codex 异步化：工作计划

> 2026-07-09 | 针对 616 ASIN 批跑实测（8.7 小时，中位数 5 分钟/条）暴露的效率与质量问题

---

## 一、问题

### 效率
- 616 条 ASIN 批跑耗时 8.7 小时，中位数 5 分钟/条
- 瓶颈：Codex 复核同步串行在基线分析之后、落库之前，占着 worker 不放

### 质量（三层缺失）

| 层 | 缺失项 | 后果 |
|----|--------|------|
| 业务护栏 | 核心词保护不完整、预算花不完仍加预算、Bid 振幅无上限 | LLM 误判无法被纠正 |
| Agent 行为护栏 | 无输出数量审计、无字段完整性校验、无动作-理由一致性检查 | LLM 漏输出/输出矛盾字段时静默通过 |
| Codex 输出护栏 | 无覆盖率检查、无"判了 modify 但没改任何值"检测 | Codex 输出形同虚设 |

---

## 二、要做的事（4 件）

### 1. 确定性业务护栏（Layer 0）

**做什么：** 把散落在 2 个文件中的护栏规则提取、统一、扩充到一个新文件 `campaign_guardrails.py`。纯 Python 逻辑，8 条规则按优先级执行。

**提取来源：**

| 来源文件 | 提取内容 | 行号 |
|----------|------|:---:|
| `campaign.py` | `_resolve_budget_conflicts` 整段（KB21§2 保护、强制淘汰、淘汰值填充、日预算上限 $200） | ~2035-2140 |
| `campaign_portfolio.py` | `LOW_BID_MAX`/`LOW_BUDGET_MAX` 常量、`is_strictly_in_low_bid_pool()`、`_is_in_elimination_pool()` | 47-76 |

提取后，原位置的处理：
- `campaign.py` 的 `_resolve_budget_conflicts` **保留函数签名**，内部改为调用 `campaign_guardrails.apply_all()`，保证调用方零改动
- `campaign_portfolio.py` 的常量和函数**原地保留**，但改为 `from campaign_guardrails import ...` 重导出，不破坏现有 `from campaign_portfolio import LOW_BID_MAX` 的引用

**提取后统一扩充为 8 条规则：**

| 优先级 | 规则 | 来源 | 触发条件 | 动作 |
|:---:|------|:---:|------|------|
| P0 | 核心词禁淘汰 | **新增** | `is_core=True` 且被判淘汰 | 强制改 keep |
| P1 | 新活动保护 | 提取 | 上线 ≤3 天且被判淘汰 | 强制改 keep |
| P2 | 复评抖动保护 | 提取 | 复评离池 ≤3 天且被判淘汰 | 强制改 keep |
| P3 | 硬淘汰触发 | 提取 | bid≤$0.21 或 预算≤$1.01 | 强制淘汰 |
| P4 | 预算花不完禁加 | **新增** | 7d 花费 < 日预算×50% 却要加预算 | 封顶到当前值 |
| P5 | Bid 振幅上限 | **新增** | 变动>50% 且 clicks<10 | 收敛到 30% |
| P6 | 测试期保护 | 提取 | product_stage=测试期且上线<14天 | 强制改 keep |
| P7 | 日预算上限 $200 | 提取 | proposed_budget > $200 | 截断到 $200 |

**不动的部分：**

| 位置 | 内容 | 原因 |
|------|------|------|
| `campaign.py` 预过滤阶段 | `is_strictly_in_low_bid_pool` 判定 + `skipped_eliminated` | 是分析流程编排的一部分，不是"修正已产出结果"的护栏 |
| `merge.py` action=keep 自洽 | `proposed_bid`/`budget` 重置回 `current` | 是 Codex merge 层的契约逻辑，不应和业务护栏混在一起 |
| `reasoner.py` prompt 护栏 | `硬规则（最高优先）` 段落 | prompt 文本独立维护 |
| `campaign.py` 投票保守幅度 | R1/R2 不一致时取保守值 | 属于 LLM 投票协议，不是业务规则 |

**要改的文件：**
- 新建 `app/workflow/steps/campaign_guardrails.py`（8 条规则 + 阈值常量）
- 修改 `app/workflow/steps/campaign.py`（`_resolve_budget_conflicts` 改为调用新文件 + 新增 P0/P4/P5 未覆盖的调用）
- 修改 `app/workflow/steps/campaign_portfolio.py`（常量/函数改为从新文件 re-export）

**风险：** 低。提取+重导出不改变行为。纯 Python，不写 DB。

---

### 2. Agent 输出校验（Layer 1 + Layer 2）

**做什么：** 新建 `campaign_validation.py`，基线 LLM 和 Codex 输出后各跑一次校验。

**要加的检查项（Layer 1 — 基线输出）：**

| 编号 | 检查项 | 严重度 |
|:---:|------|:---:|
| A1 | 输入 N 个活动，输出必须是 N 条 | error |
| B1-B7 | campaign_key 非空非重、action/reason 必填、数值合法 | error/warning |
| C1 | action=keep 但 reason 写"淘汰" → 矛盾 | warning |

**要加的检查项（Layer 2 — Codex 输出）：**

| 编号 | 检查项 | 严重度 |
|:---:|------|:---:|
| D1 | decisions 是否覆盖了所有 baseline campaign_key | warning/error |
| D2 | verdict=modify 但没给出任何实际变更 → 空判 | warning |
| D3 | action=keep 却给了 proposed_bid/budget → 不自洽 | warning |
| D4 | modify 但 final_reason 为空 | warning |

**要改的文件：**
- 新建 `app/workflow/steps/campaign_validation.py`
- 修改 `app/workflow/steps/campaign.py`（基线后插一行校验调用）
- 修改 `AD_Agent_codexV2/merge.py`（Codex merge 后插一行校验调用）

**风险：** 零。只打日志和返回报告，不阻断主流程。

---

### 3. Codex 异步化

**做什么：** 把 Codex 复核从 HTTP 请求的同步链路中拆出来，改为后台 fire-and-forget。

**怎么做：** 基线分析 + ERP 落库完成后，HTTP 立即返回。Codex 复核以 `asyncio.create_task` 在同一个进程内后台执行，完成后对受影响的 card 做增量 UPDATE。

**关键设计：**
- Codex 执行时 result 对象还在 8012 进程内存中，不需要从 DB 读回
- 增量 UPDATE 只针对被 Codex 修改过的 card（`review_level='AI_REVIEWED'`），每条一条 SQL
- Codex 不接触数据库，纯文本输入 → JSON 输出，Python 侧负责 merge + UPDATE
- 用 `codex_async_enabled` 开关控制，默认 false（保持同步行为）

**要改的文件：**
- 修改 `app/api/campaign.py`（vendor 中的 codex hook 段）：加异步分支 + 开关判断
- 修改 `app/workflow/steps/campaign.py`：新增 `_review_and_patch` 后台任务函数
- 修改 `app/persistence/erp_writer/repository.py`：新增 `update_suggest_card_after_review` 方法（一条 UPDATE SQL，~10 行）
- 修改 `AD_Agent_codexV2/codex_client.py`：加耗时日志

**风险：** 中等。8012 重启会丢失后台 task（但批跑场景不会重启）。兜底方案：cron 批跑结束后补跑扫尾脚本检查遗漏 card。

---

### 4. Prompt 强化

**做什么：** 在 `campaign-review.md` 末尾追加护栏硬约束段，让 Codex 自己尽量遵守。

**追加内��：** 数量完整、modify 必须实质变更、action 标签自洽、final_reason 必填、输出前逐条回查、禁止死循环。

**要改的文件：**
- 修改 `prompts/campaign-review.md`

**风险：** 零。只增加 prompt 文本。

---

### 5. Codex 沙箱收紧

**做什么：** 当前 `--sandbox danger-full-access` 给了 codex CLI 完整的文件系统、shell、网络权限。Prompt 里写了"不要使用任何工具"但这是软约束，无法 100% 保证 codex 不自行跑命令或读文件。

**要改的：**
- 调研 codex CLI 是否有更受限的 sandbox 模式（如 `--sandbox read-only` 或无 `danger-full-access`），在不影响 stdin/stdout 交互的前提下收紧权限
- 在 `codex_client.py` 的 `_extract_json()` 中增加工具调用痕迹检测：如果输出中包含 `<function_calls>` 或 `Tool:` 等 codex 工具调用标记，记录 warning 并尝试剔除噪声后再解析 JSON
- 此改动和护栏体系无耦合，可独立评估和实施

---
## 三、文件变更清单

| 操作 | 文件 | 说明 |
|:---:|------|------|
| 新建 | `app/workflow/steps/campaign_guardrails.py` | 提取+扩充为 8 条确定性护栏规则 + 阈值常量 |
| 新建 | `app/workflow/steps/campaign_validation.py` | 基线+Codex 输出校验（~10 条检查项） |
| 修改 | `app/workflow/steps/campaign.py` | `_resolve_budget_conflicts` 改为调用新文件；接入 Layer 1；新增 `_review_and_patch` |
| 修改 | `app/workflow/steps/campaign_portfolio.py` | 常量/函数改为从 `campaign_guardrails` re-export |
| 修改 | `app/api/campaign.py` | 异步化入口 + 开关判断 |
| 修改 | `app/persistence/erp_writer/repository.py` | 新增 `update_suggest_card_after_review` |
| 修改 | `AD_Agent_codexV2/merge.py` | 接入 Layer 2 校验 |
| 修改 | `AD_Agent_codexV2/codex_client.py` | 增加耗时日志；工具调用痕迹检测 |
| 修改 | `prompts/campaign-review.md` | 追加护栏硬约束段 |

共 **2 个新文件 + 7 个修改文件**。零 DDL 变更，零新增依赖。

---

## 四、上线顺序

```
Phase 1-2（护栏 + 校验）  →  先上，观察日志 1-2 天
        ↓
Phase 3（异步化，开关关着）→  部署但不开启，确认无副作用
        ↓
Phase 3（开关打开）        →  先在 8012 手动测试，再开批跑
        ↓
Phase 4（Prompt）          →  随时上，独立生效
```

每阶段可独立回滚。
