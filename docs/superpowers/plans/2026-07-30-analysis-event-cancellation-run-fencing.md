# 分析事件协作取消与 run_id 隔离 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `subagent-driven-development` (recommended) or `executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** “放弃分析事件”不再只是清除页面标记，而是向正在运行的 Campaign 分析发出可持久化的取消请求；后台任务在安全边界主动退出、释放自己的 Redis 幂等锁并按 `run_id` 清理自己的 session，前端等待该 run 真正结束后再恢复历史批次。

**Architecture:** State 库 `analysis_session` 继续作为分析事件真相源，只增加 `cancel_requested_at`。取消采用协作式取消，不跨进程强杀协程：API 写取消标记，Campaign 在 MCP、LLM、护栏和 ERP 落库等主要阶段边界检查标记并抛出专用取消异常。所有更新、清理都带 `asin + run_id` 条件，避免旧任务 A 误改或误删新任务 B。Redis 锁仍由持有锁的任务在既有 `finally` 中 compare-and-delete；取消端点不得替它释放。

**Tech Stack:** FastAPI、Python asyncio、PyMySQL、MySQL State DB、Redis 分布式锁、原生 HTML/JavaScript、pytest。

---

## 一、当前问题与修复边界

### 1. 当前真实问题

现有链路为：

```text
前端点击“放弃分析事件”
  → POST /decision/cancel-event
  → clear_analysis_session(asin)
  → 页面立即恢复历史批次
```

这只删除 `analysis_session` 行，不会：

- 中止已经运行的 `fetch_campaigns()`、MCP 请求或 LLM 请求；
- 阻止旧任务继续进入护栏和 ERP 写入；
- 释放 `campaign:running:{asin}` Redis 锁；
- 防止旧任务完成后按 ASIN 清掉后来创建的新 session；
- 防止旧任务的迟到响应重新覆盖前端已经恢复的历史批次。

因此，“页面看起来取消”与“后端任务真正停止”目前不是同一件事。

### 2. 本期取消语义

本期采用协作式取消：

```text
点击放弃
  → 标记 run A 的 cancel_requested_at
  → 前端显示“正在取消”并轮询 run A
  → run A 在最近一个安全检查点发现取消
  → 抛 AnalysisRunCancelled
  → run A 的 finally 释放自己持有的 Redis 锁
  → API 按 asin + run_id=A 删除自己的 session
  → session-status 返回 FINISHED
  → 前端恢复最近完成批次
```

已经发出的单次 MCP/LLM HTTP 请求不能被 State 标记瞬时物理终止。取消发生时允许该次在途调用返回或超时，但返回后必须立即检查取消状态，不能再发起下一次 MCP、LLM、护栏重试或 ERP 写入。

### 3. ERP 写入安全边界

取消必须在调用 `push_full_to_erp()` 前再检查一次。

- 检查时已取消：不调用 `push_full_to_erp()`，不生成新决策批次。
- `push_full_to_erp()` 已经开始：本期不尝试中断数据库事务，也不回滚已经完成的跨表写入；让既有写入与 `finalize_batch()` 完整收尾，避免制造半批次。

ERP 持久化区间很短，但这是明确的取消截止线。前端文案仍可使用“正在取消”，不得承诺已经开始的落库事务能够被撤销。

### 4. 不在本期做的事

- 不引入 Celery、任务队列或新的任务表。
- 不跨 worker 保存 `asyncio.Task` 对象，不使用进程内 task registry 作为唯一取消机制。
- 不从取消端点直接删除 Redis 锁。
- 不用 `clear_analysis_session(asin)` 清理正常运行中的事件。
- 不改变立即退出、清货优先、正常权限或 pending 执行规则。
- 不取消已经提交给广告执行 MCP 的广告执行任务；本方案只处理 Campaign 分析事件。

---

## 二、核心状态模型

### 1. State 表变更

修改 `ad-direction-agent/app/persistence/schema.sql`：

```sql
CREATE TABLE IF NOT EXISTS analysis_session (
    asin VARCHAR(20) PRIMARY KEY,
    shop_id BIGINT NULL,
    parent_seller_sku VARCHAR(128) NULL,
    run_id VARCHAR(64) NOT NULL,
    started_at DATETIME(6) NOT NULL,
    execution_started_at DATETIME(6) NULL,
    cancel_requested_at DATETIME(6) NULL
) ENGINE=InnoDB;
```

为已有 State 库增加独立迁移文件：

`docs/sql/2026-07-30-analysis-session-cancellation.sql`

```sql
ALTER TABLE analysis_session
    ADD COLUMN cancel_requested_at DATETIME(6) NULL
    AFTER execution_started_at;
```

`ensure_schema()` 继续只负责新库建表，不在运行时查询 `information_schema`，也不执行 `ALTER TABLE`。上线顺序必须是先执行 State 库 DDL，再部署读取该列的代码。

### 2. 状态定义

| 条件 | API 状态 | 含义 |
|---|---|---|
| session 存在、run_id 相同、`cancel_requested_at IS NULL` | `RUNNING` | 该 run 仍有效 |
| session 存在、run_id 相同、`cancel_requested_at IS NOT NULL` | `CANCELLING` | 已请求取消，等待后台任务退出 |
| session 不存在 | `FINISHED` | 该 run 已结束或从未存在 |
| session 存在但 run_id 不同 | `FINISHED` | 被查询的旧 run 已结束；当前行属于另一个 run |

状态查询必须同时使用 `asin` 和 `run_id`。当前 `run_id` 是秒级时间字符串，不具备全局唯一约束，仅传 `run_id` 可能在不同 ASIN 间碰撞。

### 3. State Manager 公共契约

MySQL 与 JSON fallback 必须提供同样的方法：

```python
def request_analysis_cancel(self, asin: str, run_id: str) -> bool:
    """仅当 asin 当前仍是该 run 时写 cancel_requested_at；不删除 session。"""

def is_analysis_run_active(self, asin: str, run_id: str) -> bool:
    """仅当当前行属于该 run 且尚未请求取消时返回 True。"""

def clear_analysis_session_if_run(self, asin: str, run_id: str) -> bool:
    """仅删除当前仍属于该 run 的 session；受影响 0 行也视为幂等成功。"""

def clear_analysis_execution_started_if_run(self, asin: str, run_id: str) -> bool:
    """仅清理该 run 的 execution_started_at。"""
```

`get_analysis_session()` 返回值增加：

```python
{
    "run_id": "20260730T120000Z",
    "started_at": "2026-07-30T12:00:00+00:00",
    "execution_started_at": "2026-07-30T12:01:00+00:00",
    "cancel_requested_at": "2026-07-30T12:02:00+00:00",
}
```

旧的无条件方法可以暂时保留给兼容调用，但分析事件主链不得再调用：

```python
clear_analysis_session(asin)
clear_analysis_execution_started(asin)
```

---

## 三、并发与幂等原则

### 1. A/B run 隔离

所有会改变 session 的 SQL 必须带：

```sql
WHERE asin = %s AND run_id = %s
```

必须锁定以下竞态：

```text
A 正在运行
  → A 被取消
  → B 后来创建
  → A 的异常处理或 finally 到达
  → A 只能清理 run_id=A，不能删除或修改 B
```

### 2. 旧 session 的清理

当前 `get_analysis_session()` 的 12 小时 TTL 分支无条件执行：

```python
self.clear_analysis_session(asin)
```

必须改为先保存查到的旧 `run_id`，再执行：

```python
self.clear_analysis_session_if_run(asin, str(row["run_id"]))
```

JSON fallback 同样按文件中再次读到的 `run_id` 比较后删除，不能先读 A、后误删已经被 B 覆盖的文件。

### 3. 新事件创建

现有 `/decision/new-event` 对任何旧 session 都直接复用。修改后：

```text
无 session
  → 创建新 run

旧 session 未取消
  → 拒绝新建，返回“该 ASIN 已有分析运行中，请等待完成”

旧 session 已取消，且旧 run 的 Redis 锁仍存在
  → 拒绝新建，返回“旧分析正在取消，请等待退出”

旧 session 已取消，且旧 run 未持有 Redis 锁
  → clear_analysis_session_if_run(asin, old_run_id)
  → 创建新 run
```

不能只看到 `cancel_requested_at` 就立即删除旧 session。若 A 仍持有 Redis 锁，立即覆盖为 B 会产生两个问题：

- B 会被 A 的 Redis 锁拦截；
- 若 B 尚未进入 Campaign，而 A 仍继续执行，两个 run 的页面和 State 状态会交叉。

因此，用户提出的“旧 session 已标记取消即可清理”应实现为“已标记取消且后台运行锁已不存在时可清理”。若取消发生在 Campaign 执行启动前，尚无 Redis 锁，旧 run 可直接清理；之后旧页面即使迟到发起 `/campaign/viewmodel`，也会被 run_id 校验拒绝。

为此在 `app/persistence/redis_client.py` 增加只读方法：

```python
async def get_lock_holder(key: str) -> str | None:
    """返回 Redis/进程内降级锁当前 token，不修改锁。"""
```

新事件端点复用 `app.workflow.steps.campaign._running_key(asin)` 或把该 key 构造提升为公开、无业务副作用的 helper，避免在两处拼接 key。

### 4. 创建 session 不得覆盖未结束 run

`set_analysis_session()` 当前使用 `ON DUPLICATE KEY UPDATE run_id=VALUES(run_id)`，会直接覆盖 A。应修改为只负责“无现存 session 时创建”，MySQL 使用普通 `INSERT`；唯一键冲突返回明确失败，由 API 重新读取当前 session 并报告冲突。

不得采用“先 GET、后无条件 UPSERT”的非原子覆盖。JSON fallback 必须在已有合法 session 文件时拒绝覆盖。

---

## 四、API 契约

### 1. `POST /decision/cancel-event`

文件：`ad-direction-agent/app/api/decision.py`

请求保持不变：

```json
{
  "asin": "B0TEST"
}
```

处理顺序：

```text
读取当前 session
  → 无 session：幂等返回 FINISHED
  → 有 session：保存 run_id
  → request_analysis_cancel(asin, run_id)
  → 返回该 run_id 和 CANCELLING
```

响应：

```json
{
  "ok": true,
  "run_id": "20260730T120000Z",
  "status": "CANCELLING"
}
```

无 session 的幂等响应：

```json
{
  "ok": true,
  "run_id": "",
  "status": "FINISHED"
}
```

端点不得调用：

```python
clear_analysis_session(asin)
release_lock(running_key, run_id)
```

日志必须包含 ASIN、run_id 和状态：

```text
分析事件已请求取消 [B0TEST] run_id=20260730T120000Z
```

### 2. `GET /decision/session-status`

文件：`ad-direction-agent/app/api/decision.py`

新增查询端点：

```http
GET /decision/session-status?asin=B0TEST&run_id=20260730T120000Z
```

响应示例：

```json
{
  "ok": true,
  "asin": "B0TEST",
  "run_id": "20260730T120000Z",
  "status": "CANCELLING",
  "active": true
}
```

run 已退出：

```json
{
  "ok": true,
  "asin": "B0TEST",
  "run_id": "20260730T120000Z",
  "status": "FINISHED",
  "active": false
}
```

`active` 表示该 run 的 session 行仍存在，不等于“允许继续分析”。`CANCELLING` 时 `active=true`，但 Campaign 必须停止发新工作。

### 3. `/decision/context`

保持现有字段兼容，增量返回：

```json
{
  "in_progress": "20260730T120000Z",
  "cancel_requested_at": "2026-07-30T12:02:00+00:00",
  "cancelling": true
}
```

在 session 被后台任务清理前，`in_progress` 仍保留，历史批次的 `executable` 仍为 `false`。这样前端不会在 A 尚未退出时提前恢复执行权。

### 4. `/campaign/viewmodel`

进入分析前必须验证：

```text
请求 run_id 非空
且 session 当前 run_id 与请求一致
且 cancel_requested_at 为空
```

`mark_analysis_execution_started()` 返回 `False` 时不得继续 `_do_analyze()`。应返回明确的不可执行 ViewModel 或 API 错误：

```text
分析事件已取消或已被其他事件替代，请刷新页面
```

这一步阻止“取消发生在 Campaign 尚未启动、旧页面之后才发请求”的迟到启动。

---

## 五、Campaign 协作取消检查点

### 1. 专用异常与 checker

新增：

`ad-direction-agent/app/workflow/analysis_run_guard.py`

```python
class AnalysisRunCancelled(RuntimeError):
    def __init__(self, asin: str, run_id: str):
        super().__init__(f"analysis run cancelled: asin={asin} run_id={run_id}")
        self.asin = asin
        self.run_id = run_id


async def ensure_analysis_run_active(state, asin: str, run_id: str) -> None:
    active = await asyncio.to_thread(state.is_analysis_run_active, asin, run_id)
    if not active:
        raise AnalysisRunCancelled(asin, run_id)
```

checker 只读 State，不修改 session，不释放 Redis 锁。

### 2. API 到 workflow 的透传

`app/api/campaign.py::_do_analyze()` 根据当前 `state + asin + run_id` 构造：

```python
async def cancel_check() -> None:
    await ensure_analysis_run_active(state, asin, run_id)
```

将其与 `_do_analyze()` 当前已经准备好的参数一起传给：

```python
await analyze_campaigns(
    fetcher=fetcher,
    reasoner=reasoner,
    parent_asin=asin,
    asin_data=asin_data,
    strategy_context=strategy_context,
    run_id=run_id,
    cancel_check=cancel_check,
)
```

`app/workflow/steps/campaign.py::analyze_campaigns()` 和 `_analyze_campaigns_impl()` 新增可选参数：

```python
cancel_check: Callable[[], Awaitable[None]] | None = None
```

实验脚本或不属于前端分析事件的既有调用可以继续传 `None`，行为不变。

### 3. 必须布置的检查点

检查点至少覆盖：

1. 获取 Redis 锁后、开始任何数据工作前；
2. 读取 Campaign Redis 缓存前；
3. `fetch_campaigns()` 前和返回后；
4. 复用/拉取 `own_keyword_flow` 等既有 Campaign 数据依赖前后；
5. 每个 LLM batch 发起前；
6. 每个 LLM batch 返回后；
7. R3、R4 护栏重试每轮开始前；
8. synthesis 和 budget reallocation LLM 前后；
9. 最终规则、快照组装完成后；
10. 回到 API 后、调用 `push_full_to_erp()` 前。

不要在每个纯 Python 行或每条 adjustment 上查 State DB。按外部调用与阶段边界检查即可，避免取消功能把 State DB 变成高频热点。

### 4. Redis 锁释放

保留既有结构：

```python
acquired, holder = await acquire_lock(key, run_id, ttl)
try:
    return await _analyze_campaigns_impl(
        fetcher=fetcher,
        reasoner=reasoner,
        parent_asin=parent_asin,
        asin_data=asin_data,
        strategy_context=strategy_context,
        days=days,
        bs=bs,
        cc=cc,
        temperature=temperature,
        refresh=refresh,
        campaign_data=campaign_data,
        keyword_analysis=keyword_analysis,
        run_id=run_id,
        cancel_check=cancel_check,
        _t=_t,
        erp_override=erp_override,
        elimination_entry_dates=elimination_entry_dates,
    )
finally:
    await release_lock(key, run_id)
```

`AnalysisRunCancelled` 必须向上抛出，经过该 `finally`，由 A 自己释放 token=A 的锁。compare-and-delete 会保证 A 无法删除 B 的锁。

### 5. API 取消收尾

`campaign_viewmodel()` 捕获 `AnalysisRunCancelled` 后：

```text
clear_analysis_session_if_run(asin, run_id)
  → 返回“本次分析已取消”的空、不可执行 ViewModel
```

该清理发生在 `analyze_campaigns()` 的 Redis `finally` 执行之后。

一般异常仍沿用现有“清 execution_started_at、保留 session 供重试”的语义，但清理必须改为：

```python
clear_analysis_execution_started_if_run(asin, run_id)
```

不能让 A 的失败处理清掉 B 的启动状态。

---

## 六、所有旧清理点的 run_id 化

必须逐一修改以下点位，禁止只修取消端点：

| 文件 | 当前点位 | 修改 |
|---|---|---|
| `app/persistence/mysql_state_manager.py` | TTL 过期清理 | `clear_analysis_session_if_run(asin, expired_run_id)` |
| `app/persistence/state_manager.py` | JSON TTL 过期清理 | 比较文件当前 run_id 后删除 |
| `app/api/campaign.py` | ERP finalize 后清 session | 传本次 `run_id`，按 run 清理 |
| `app/api/campaign.py` | 分析失败清 execution_started | 按 run 清理 |
| `app/api/decision.py` | 立即退出预检失败清 session | 用 immediate-exit run_id 条件清理 |
| `app/api/decision.py` | 立即退出落库确认后清 session | 用 immediate-exit run_id 条件清理 |
| `app/api/decision.py` | cancel-event | 改为写取消标记，不删除 |

`_maybe_push_erp()` 必须显式接收 `run_id`，不能从“此刻按 ASIN查到的 session”反推，因为该行可能已经属于 B。

---

## 七、前端时序

文件：`ad-direction-agent/demo/ad-asisitant-agent.html`

### 1. 点击放弃

修改 `onCancelEventClick()`：

```text
POST /decision/cancel-event
  → 读取响应 run_id
  → 将 run_id 加入前端 canceled-run 集合
  → 卸载当前 Campaign panel
  → 保持遮罩，文案改为“正在取消分析，请稍候”
  → 每 2.5 秒调用 session-status
```

取消请求成功后不得立即：

- 恢复历史批次；
- 重新开放执行按钮；
- 把 `in_progress` 本地强行设空。

### 2. 轮询结束

当 `session-status.status === "FINISHED"`：

```text
refreshDecisionContext()
  → applyBatchBarFromCtx(ctx)
  → 若 ctx.in_progress 为空，渲染 latest_completed_id
  → 隐藏遮罩
  → 提示“已放弃本次分析，已恢复最近一次历史数据”
```

若刷新后发现 `ctx.in_progress` 是另一个 run B，只渲染 B 的进行中态，不把历史批次开放为可执行。

轮询网络失败时保留“取消状态待确认”的提示，不假定已完成；用户刷新页面后由 `/decision/context.cancelling` 恢复遮罩。

### 3. 防迟到响应覆盖

维护：

```javascript
window._cancelledCampaignRunIds = new Set();
```

`_mountCampaignRealtime({run_id})` 的 `onComplete` 和 `catch` 在修改 DOM 前检查：

```javascript
if (window._cancelledCampaignRunIds.has(run_id)) return;
```

此外，收到取消响应后主动 `unmount()` 当前 panel。这样旧 run A 的迟到 ViewModel 不会覆盖已经恢复的历史批次或新 run B。

### 4. 页面重进恢复

`loadAll()` / tab5 初始挂载读取 `/decision/context`：

- `in_progress` 且 `cancelling=false`：沿用当前分析中遮罩；
- `in_progress` 且 `cancelling=true`：显示“正在取消”遮罩并进入 session-status 轮询；
- 无 `in_progress`：渲染最新完成批次。

---

## 八、实施任务

### Task 1: 先用测试锁定 State run fencing

**Files:**
- Modify: `ad-direction-agent/tests/persistence/test_analysis_session_execution_state.py`
- Modify: `ad-direction-agent/tests/persistence/test_mysql_state_identity_write.py`
- Modify: `ad-direction-agent/app/persistence/state_manager.py`
- Modify: `ad-direction-agent/app/persistence/mysql_state_manager.py`

- [ ] **Step 1: 写失败测试**

覆盖以下具名测试：

- `test_cancel_marks_matching_run_without_deleting_session`
- `test_cancel_does_not_mark_newer_run`
- `test_clear_session_if_run_cannot_delete_newer_run`
- `test_clear_execution_started_if_run_cannot_clear_newer_run`
- `test_mark_execution_started_rejects_cancelled_run`
- `test_set_session_refuses_to_overwrite_existing_run`

- [ ] **Step 2: 运行失败测试**

```powershell
cd D:\project\AD_Agent_work_place6.17\AD_assistant_agent-v3.2\ad-direction-agent
py -m pytest tests/persistence/test_analysis_session_execution_state.py tests/persistence/test_mysql_state_identity_write.py -q
```

预期：因新方法和取消字段尚不存在而失败。

- [ ] **Step 3: 实现 MySQL 与 JSON fallback 的同构方法**

MySQL 更新示例：

```sql
UPDATE analysis_session
SET cancel_requested_at = COALESCE(cancel_requested_at, %s)
WHERE asin = %s AND run_id = %s;
```

```sql
DELETE FROM analysis_session
WHERE asin = %s AND run_id = %s;
```

`mark_analysis_execution_started()` 增加：

```sql
AND cancel_requested_at IS NULL
```

- [ ] **Step 4: 运行测试直至通过**

```powershell
py -m pytest tests/persistence/test_analysis_session_execution_state.py tests/persistence/test_mysql_state_identity_write.py -q
```

### Task 2: 增加 DDL 与防运行时迁移回归

**Files:**
- Modify: `ad-direction-agent/app/persistence/schema.sql`
- Create: `docs/sql/2026-07-30-analysis-session-cancellation.sql`
- Modify: `ad-direction-agent/tests/persistence/test_mysql_state_schema_no_runtime_migration.py`

- [ ] **Step 1: 测试 schema.sql 含新列、ensure_schema 不含 ALTER**
- [ ] **Step 2: 更新新库 schema 与独立迁移 SQL**
- [ ] **Step 3: 运行测试**

```powershell
py -m pytest tests/persistence/test_mysql_state_schema_no_runtime_migration.py -q
```

### Task 3: 实现取消与状态查询 API

**Files:**
- Modify: `ad-direction-agent/tests/api/test_decision_cancel_event.py`
- Modify: `ad-direction-agent/app/api/decision.py`

- [ ] **Step 1: 将旧“立即删除”测试改为“标记取消并返回 run_id”**
- [ ] **Step 2: 增加无 session 幂等、run 不匹配和状态查询测试**
- [ ] **Step 3: 实现 `cancel-event` 与 `session-status`**
- [ ] **Step 4: 运行测试**

```powershell
py -m pytest tests/api/test_decision_cancel_event.py -q
```

### Task 4: 修复新事件创建竞态

**Files:**
- Create: `ad-direction-agent/tests/api/test_decision_new_event_concurrency.py`
- Modify: `ad-direction-agent/app/api/decision.py`
- Modify: `ad-direction-agent/app/persistence/redis_client.py`
- Modify: `ad-direction-agent/app/workflow/steps/campaign.py`

- [ ] **Step 1: 测试活动 A 不得被覆盖**
- [ ] **Step 2: 测试取消中的 A 持锁时拒绝 B**
- [ ] **Step 3: 测试取消中的 A 已无锁时按 run 清理后允许 B**
- [ ] **Step 4: 增加只读 `get_lock_holder()` 和共享 running-key helper**
- [ ] **Step 5: 运行测试**

```powershell
py -m pytest tests/api/test_decision_new_event_concurrency.py -q
```

### Task 5: 接入 Campaign 协作取消

**Files:**
- Create: `ad-direction-agent/app/workflow/analysis_run_guard.py`
- Create: `ad-direction-agent/tests/workflow/test_campaign_cancellation.py`
- Modify: `ad-direction-agent/app/workflow/steps/campaign.py`
- Modify: `ad-direction-agent/app/api/campaign.py`

- [ ] **Step 1: 测试取消检查异常和 Redis 锁 finally 释放**
- [ ] **Step 2: 测试取消后不进入下一轮 LLM**
- [ ] **Step 3: 测试取消发生在 ERP 前时不调用 `push_full_to_erp()`**
- [ ] **Step 4: 在主要外部调用与重试边界接入 checker**
- [ ] **Step 5: 运行测试**

```powershell
py -m pytest tests/workflow/test_campaign_cancellation.py -q
```

### Task 6: 把全部 session 清理改为 run-specific

**Files:**
- Modify: `ad-direction-agent/app/api/campaign.py`
- Modify: `ad-direction-agent/app/api/decision.py`
- Modify: `ad-direction-agent/tests/api/test_product_identity_required.py`
- Create: `ad-direction-agent/tests/api/test_analysis_run_fencing.py`

- [ ] **Step 1: 测试 A 的 finalize/failure 不能清 B**
- [ ] **Step 2: 测试立即退出 A 不能清 B**
- [ ] **Step 3: 修改 `_maybe_push_erp()`、失败清理和立即退出清理签名**
- [ ] **Step 4: 搜索确认主链无无条件清理**

```powershell
rg -n "clear_analysis_session\(|clear_analysis_execution_started\(" app/api app/workflow
```

允许保留方法定义和明确的管理脚本；分析事件主链调用必须全部是 `*_if_run`。

- [ ] **Step 5: 运行测试**

```powershell
py -m pytest tests/api/test_analysis_run_fencing.py tests/api/test_product_identity_required.py -q
```

### Task 7: 前端等待真实退出并防迟到渲染

**Files:**
- Modify: `ad-direction-agent/demo/ad-asisitant-agent.html`
- Modify: `ad-direction-agent/tests/test_demo_ux_migration.py`

- [ ] **Step 1: 添加静态契约测试**

断言：

- cancel 响应读取 `run_id`；
- 调用 `/decision/session-status`；
- `FINISHED` 前不恢复历史批次；
- canceled run 的 `onComplete` 不更新 DOM；
- `/decision/context.cancelling` 可恢复取消遮罩。

- [ ] **Step 2: 实现取消轮询与迟到响应 fencing**
- [ ] **Step 3: 运行测试**

```powershell
py -m pytest tests/test_demo_ux_migration.py -q
```

### Task 8: 集成回归与人工验收

**Files:**
- Verify only.

- [ ] **Step 1: 语法编译**

```powershell
py -m compileall app tests
```

- [ ] **Step 2: 聚焦回归**

```powershell
py -m pytest `
  tests/api/test_decision_cancel_event.py `
  tests/api/test_decision_new_event_concurrency.py `
  tests/api/test_analysis_run_fencing.py `
  tests/workflow/test_campaign_cancellation.py `
  tests/persistence/test_analysis_session_execution_state.py `
  tests/persistence/test_mysql_state_identity_write.py `
  tests/persistence/test_mysql_state_schema_no_runtime_migration.py `
  tests/test_demo_ux_migration.py -q
```

- [ ] **Step 3: 代码卫生**

```powershell
git diff --check
rg -n "clear_analysis_session\(|clear_analysis_execution_started\(" app/api app/workflow app/persistence
```

- [ ] **Step 4: 本地人工时序验收**

场景 A：

```text
创建 run A
→ 启动 Campaign
→ LLM 运行中点击放弃
→ 前端显示“正在取消”
→ A 不再发下一轮 LLM
→ A 释放 Redis 锁
→ A session 删除
→ 前端恢复历史批次
```

场景 B：

```text
创建 run A
→ 放弃 A
→ A 尚未退出时尝试新建 B
→ 返回“旧分析正在取消”
→ A 退出后新建 B 成功
```

场景 C：

```text
A 取消后 B 已创建
→ A 的迟到异常/finally 到达
→ B session 与 execution_started_at 均保持不变
```

场景 D：

```text
创建 A 后关闭页面
→ 重进页面
→ context 显示 A 正在运行
→ 点击放弃
→ 轮询 A 至 FINISHED
→ 恢复最新历史批次
```

场景 E：

```text
取消发生在 push_full_to_erp 前
→ 不新增 decision/card/pending
```

---

## 九、验收标准

功能完成必须同时满足：

1. 点击放弃后，后台分析在最近一个阶段检查点退出，而不是继续完整跑完。
2. 取消端点返回被取消的 `run_id`。
3. 前端在该 `run_id` 真正结束前保持取消遮罩，不提前开放历史批次执行权。
4. 取消端点不主动释放 Redis 锁；锁由原任务的 `finally` 释放。
5. 旧 run A 的任何清理、失败处理和 finalize 都不能修改或删除新 run B。
6. 已取消但尚未退出的 A 不能被新事件 B 直接覆盖。
7. 已取消的迟到 `/campaign/viewmodel` 请求不得启动分析。
8. 取消发生在 ERP 落库前时，不生成新 decision、card 或 pending。
9. JSON fallback 与 MySQL State 的取消和 run fencing 语义一致。
10. 立即退出既有流程只改为 run-specific 清理，不改变其确定性执行业务。

---

## 十、上线顺序

```text
1. 在目标 State DB 备份 SHOW CREATE TABLE analysis_session
2. 执行 docs/sql/2026-07-30-analysis-session-cancellation.sql
3. 用 information_schema.COLUMNS 验证 cancel_requested_at
4. 部署后端与前端代码
5. 重启目标服务
6. 执行场景 A、B、C 的最小生产验收
7. 检查日志中 cancel requested / cancelled exit / lock released 的同一 run_id 闭环
```

不得先部署读取 `cancel_requested_at` 的代码再补 State DDL，否则 `get_analysis_session()` 会因未知列降级为 `None`，反而绕过进行中事件保护。

本方案文件只定义实现与验收步骤，不授权执行数据库迁移、部署、重启或提交 Git commit。
