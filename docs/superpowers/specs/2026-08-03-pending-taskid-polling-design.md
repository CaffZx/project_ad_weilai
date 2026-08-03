# Pending 执行状态与 taskId 轮询设计

> 日期：2026-08-03  
> 范围：Campaign 广告调整执行链路的三张 pending 表。  
> 本文只定义方案，不包含代码或数据库变更。

## 1. 目标与边界

Campaign 分析完成并落库，与运营确认后真实下发广告调整是两个独立时序。`taskId` 只会在调用异步广告调整 MCP（`agent_async_batch_update_advert`）后返回，因此不能由分析落库阶段生成。

本方案目标：

1. 所有经营权限模式统一 pending 执行语义：提交成功不等于广告生效。
2. 三张 pending 表使用相同的 `PENDING → IN_PROGRESS → SUCCESS/FAIL` 状态语义。
3. 异步 MCP 返回的 `taskId` 必须立即与本次涉及的 pending 行关联；它既是轮询依据，也是服务重启后的人工可观测审计信息。
4. 获得 `taskId` 后由程序自动轮询结果 MCP，按 3 分钟、6 分钟、12 分钟、24 分钟间隔最多查询四次。
5. 终态回写必须按 pending 行主键精确更新，不得再用 `campaign_id` 批量覆盖同活动的全部 pending 行。

明确不做：

- 不新增执行任务账本、任务映射表或数据库调度器。
- 不将 `next_poll_at`、`last_poll_at`、`poll_count` 等轮询调度状态写入数据库。
- 不做服务重启后的自动续轮询或 taskId 恢复。
- 不修改 Campaign 分析、建议生成或运营确认的业务规则。

## 2. 受影响的数据对象

三张表都增加同一个可空字段：

| 表 | 一行代表 | `task_id` 的含义 |
| --- | --- | --- |
| `t_advert_agent_modify_campaign_pending` | 活动预算、活动状态、组合迁移 | 覆盖该行的异步批量调整任务 |
| `t_advert_agent_modify_keyword_pending` | 关键词 Bid/状态；否词为特殊行 | 覆盖该行的异步批量调整任务；否词同步接口时保持空 |
| `t_advert_agent_modify_placement_pending` | 单个广告位比例调整 | 覆盖该行的异步批量调整任务 |

建议 DDL：

```sql
ALTER TABLE t_advert_agent_modify_campaign_pending
    ADD COLUMN task_id varchar(64) DEFAULT NULL COMMENT '异步广告调整MCP返回的taskId' AFTER execute_status,
    ADD KEY idx_campaign_pending_task_id (task_id);

ALTER TABLE t_advert_agent_modify_keyword_pending
    ADD COLUMN task_id varchar(64) DEFAULT NULL COMMENT '异步广告调整MCP返回的taskId' AFTER execute_status,
    ADD KEY idx_keyword_pending_task_id (task_id);

ALTER TABLE t_advert_agent_modify_placement_pending
    ADD COLUMN task_id varchar(64) DEFAULT NULL COMMENT '异步广告调整MCP返回的taskId' AFTER execute_status,
    ADD KEY idx_placement_pending_task_id (task_id);
```

不新增 `submitted_at` 与 `effective_at`。`execute_time` 统一改为终态时间：只有写入 `SUCCESS` 或 `FAIL` 时才更新。异步 MCP 成功返回 `taskId` 后，必须立即把它写入精确 pending 行，但该次写入不得修改 `execute_status`（仍为 `IN_PROGRESS`）或 `execute_time`。

## 3. 统一状态机

`confirm_status` 和 `execute_status` 是独立维度。

| 阶段 | `confirm_status` | `execute_status` | `task_id` | `execute_time` |
| --- | --- | --- | --- | --- |
| 分析落库 | `PENDING` | `PENDING` | `NULL` | `NULL` |
| 人工确认/自动确认后未提交 | `CONFIRMED` | `PENDING` | `NULL` | `NULL` |
| 已原子抢占、正在提交或等待 taskId | `CONFIRMED` | `IN_PROGRESS` | `NULL` | `NULL` |
| 已收到 taskId、等待轮询终态 | `CONFIRMED` | `IN_PROGRESS` | 返回的 taskId | `NULL` |
| 异步任务确认成功 | `CONFIRMED` | `SUCCESS` | 返回的 taskId | 终态确认时间 |
| 异步任务明确失败 | `CONFIRMED` | `FAIL` | 返回的 taskId（若有） | 终态确认时间 |
| 四次查询均未确认成功 | `CONFIRMED` | `FAIL` | 返回的 taskId | 第四次查询时间 |
| 同步 MCP（新建/否词）明确成功 | `CONFIRMED` | `SUCCESS` | `NULL` | 接口返回时间 |
| 同步 MCP 明确失败 | `CONFIRMED` | `FAIL` | `NULL` | 接口返回时间 |
| 演练 | `CONFIRMED` 或 `PENDING` | `DRY_RUN` | `NULL` | `NULL` |

四次未得到成功终态属于“轮询未确认”，不是广告平台明确失败；但为满足执行层只使用 `SUCCESS/FAIL` 终态的要求，状态写 `FAIL`，并且 `execute_msg` 必须明确写：

```text
异步任务在 4 次轮询（提交后 +3m、随后 +6m、随后 +12m、随后 +24m）内未返回成功终态；需人工复核 taskId=<taskId>
```

MCP 明确失败与轮询耗尽不得使用相同文案。

## 4. 所有权限模式的统一执行流程

经营模式仍保留原有权限差异；执行状态机不再有差异。

| 权限 | 对应经营模式 | 确认来源 | 提交来源 | taskId 轮询 |
| --- | --- | --- | --- | --- |
| `STOP` | 立即退出 | 系统自动确认 | 系统自动提交 | 使用统一后台轮询 |
| `CLEARANCE_ONLY` | 控制清货 | 运营人工确认 | 运营确认后的统一提交入口 | 使用统一后台轮询 |
| `NORMAL` | 限时修复、稳定经营、积极推进、获取利润 | 运营人工确认 | 运营确认后的统一提交入口 | 使用统一后台轮询 |

立即退出当前的专用入口可以保留，但只能负责确定性生成动作和自动确认；下发与轮询必须调用同一套执行服务。清货和正常不得再走“异步 MCP 提交成功即写 `SUCCESS`”的直接执行分支。

清货/正常的前端“同意所选 → 确认”维持一次用户操作、一次 `/campaign/confirm` 请求的现有交互，不改成要求用户再调用第二个执行接口。该 API 的 approve 分支改为：仅对本次选中的 card 原子确认，再调用统一服务处理其精确 pending 行；`submit_execution_direct()` 若保留，只能作为这一选中范围适配器，内部不得自行调用 MCP 或写终态。

## 5. 程序内自动轮询

### 5.1 提交阶段

1. **先分流 dry-run**：dry-run 只构造计划和写入既有的 `DRY_RUN` 结果（如业务仍需保留该记录），不得原子抢占、调用异步 MCP、写 taskId 或创建后台轮询任务。
2. 真实下发前，轮询调度器必须先预留一个有限容量名额；容量已满则直接返回“轮询容量不足”，pending 保持 `PENDING`，不得提交 MCP。这样不会产生“已下发但无轮询器”的任务。
3. 读取已确认且 `execute_status='PENDING'` 的 pending 行，生成 `ExecPlan`；立即退出的即时确认也必须已显式写为 `CONFIRMED`。
4. 使用 `pending_id` 原子抢占本次要下发的行：`PENDING → IN_PROGRESS`。抢占条件必须包含 `confirm_status='CONFIRMED'`；只有全部目标行抢占成功才允许继续。
5. 调用 `agent_async_batch_update_advert`。
6. **任何未拿到有效 taskId 的结果都是提交阶段终态失败**，包括 MCP 明确错误、网络异常、超时、响应无法解析、返回空 taskId。按本次 `ExecPlan` 的精确 pending 行回写 `FAIL`、失败原因和终态时间，并释放已预留的调度名额。对超时/网络异常，`execute_msg` 必须标注“提交结果未知，未获得 taskId，禁止自动重提，需人工核对”，不得伪称广告平台明确失败。
7. 若返回 taskId：先在精确 pending 行立即写入 `task_id`，保持 `execute_status='IN_PROGRESS'` 且 `execute_time=NULL`；该落库成功后才把 `(taskId, ExecPlan.ops)` 交给后台轮询队列。HTTP 接口返回 `IN_PROGRESS` 与 taskId。
8. 若“taskId 已从 MCP 返回但 task_id 落库失败”，不得静默当作成功：需同步有限重试并记录高优先级错误；在未持久化成功前不得宣称该任务可恢复。此场景仍存在“外部已受理、进程崩溃早于本地写入”的极窄窗口；不引入持久化提交账本时无法做到严格零丢失，需作为明确运行风险监控。

后台协程不查询数据库来发现任务，也不依赖 `t_advert_agent_modify_advert_record` 的外部回写。

### 5.2 轮询节奏

对每个 taskId：

1. 提交成功后等待 3 分钟，第一次调用 `agent_batch_update_advert_result`。
2. 未获得明确成功或失败终态，等待 6 分钟后第二次查询。
3. 仍未获得明确终态，等待 12 分钟后第三次查询。
4. 仍未获得明确终态，等待 24 分钟后第四次查询。
5. 第四次仍未确认成功或明确失败，按“轮询耗尽”规则回写 `FAIL`。

总共最多四次结果查询；该后台协程不阻塞 API 请求。

### 5.2 轮询并发上限与队列

不得对每个 taskId 无上限地 `asyncio.create_task()` 并休眠 45 分钟。实现一个进程内轮询调度器：默认 `ADVERT_TASK_POLL_WORKERS=10` 个 worker，以及 `ADVERT_TASK_POLL_QUEUE_CAPACITY=100` 的有界队列；worker 只从队列取任务并执行 3/6/12/24 分钟轮询。提交阶段先预留总容量（默认最多 110 个已受理任务）之一；容量已满则不抢占、不提交 MCP，Pending 保持 `PENDING` 并返回容量不足。MCP 未返回 taskId 时释放名额；taskId 落库后再入队；终态回写后由 worker 释放。队列等待时间计入从 MCP 提交成功起算的轮询时间，逾期的首次查询应立即执行。

这样同时受控的是活跃轮询任务总数（最多 `N+M`）和实际发往 MCP 的并发查询数（最多 `N`）。进程重启会丢失内存队列，但不会丢失已经落入 pending 的 `task_id`；本方案仍不自动恢复队列。

### 5.3 结果映射与精确回写

当前结果解析可按 `campaign_id` 得到 MCP 的活动级状态，但实际回写必须使用提交时保留的 `ExecPlan.ops`：

1. 仅处理本次走 `agent_async_batch_update_advert` 的操作；新建活动、否词等同步工具不纳入该 taskId。
2. 先以任务结果中的 `campaign_id` 找到本次内存操作列表。
3. 再以每个操作的 `(record_kind, pending_id)` 更新对应表中的一行。
4. 不使用 `WHERE decision_id=? AND campaign_id=?` 更新所有行。
5. 同一 taskId 覆盖多条 campaign、keyword、placement pending 时，三张表都写入相同的 taskId；每行根据对应活动的结果单独写 `SUCCESS` 或 `FAIL`。

需要新增一个 repository 级精确回写方法，入参为操作列表、taskId、逐活动状态和错误消息。该方法统一维护三张表，避免三个调用路径各自拼 SQL。

## 6. 现有代码需要替换的行为

| 当前位置 | 当前问题 | 目标行为 |
| --- | --- | --- |
| `app/workflow/steps/advert_execution.py:submit_execution()` | 已可解析 taskId，但执行记录本地写入为空；通用回写会给 `IN_PROGRESS` 写 `execute_time` | 抢占后启动后台轮询；终态才写 taskId、`execute_time` 与状态 |
| `app/workflow/steps/advert_execution.py:submit_execution_direct()` | 异步提交成功后通过 `_sync_pool_entries_from_exec()` 直接将 pending 写为 `SUCCESS` | 删除提交即成功回写，改为调用统一提交/轮询服务 |
| `app/workflow/steps/advert_execution.py:poll_execution_result()` | 只允许立即退出，taskId 从 ERP 执行主记录反查，终态按 campaign 批量更新 | 改为可由所有权限调用；直接使用内存捕获的 taskId 与精确 pending 行 |
| `app/persistence/erp_writer/repository.py:update_pending_execute_status()` | 任何状态都写 `execute_time` | `IN_PROGRESS` 不写 `execute_time`；终态才写 |
| `app/persistence/erp_writer/repository.py:update_campaign_terminal_status()` | 按 `decision_id + campaign_id` 更新三张表全部 pending 行 | 以 `pending_id` 精确回写，不再作为通用终态回写工具 |
| `app/api/campaign.py:/campaign/confirm` | approve 走一步式真实下发，确认失败被吞后仍可能执行 | 先确认成功，再交给统一提交入口；失败不得下发 |

### 6.1 旧链路切除与单写者约束（必须随实现一并完成）

本方案不能只新增后台轮询器，而保留旧的“提交后即成功”或“前端轮询”写入链路；否则同一条 pending 可能被两个执行者以不同语义回写。上线时按以下规则切换：

1. **唯一提交入口**：立即退出、清货优先、正常经营的 API 最终都只能调用同一个“领取 pending → 异步提交 → 启动后台轮询”服务。`submit_execution_direct()` 不得保留独立的异步提交实现；当前前端仍经它发起“同意所选”，本次改造中保留为调用统一服务的选中范围薄包装，后续再随重命名清理。
2. **删除提交即成功**：移除异步路径中 `_sync_pool_entries_from_exec()` 对 pending 写 `SUCCESS` 的行为。该方法若仍服务同步 MCP 工具，必须按工具类型隔离，异步广告批量调整不得进入该分支。
3. **停用旧 taskId 反查**：删除 `submit_execution(wait_for_terminal=...)` 这一旧控制分支，并删除 `poll_execution_result()`、`list_execution_task_ids()` 及对 `t_advert_agent_modify_advert_record.task_id` 的依赖；它们不得再参与当前 pending 的终态判定。外部执行记录可以保留作历史审计，但不能作为 taskId 的来源或回写依据。
4. **移除前端 MCP 轮询**：立即退出页面现有的 5 秒/12 次状态轮询不得继续调用结果 MCP。删除该轮询；若前端需要刷新状态，只能读取 pending 已落库的状态快照，不能再次驱动执行或终态回写。
5. **废弃泛更新终态方法**：`update_campaign_terminal_status()` 不得再被任何执行入口调用；删除，或改为内部禁止使用并由按 `(record_kind, pending_id)` 的精确回写方法完全替代。
6. **单一终态写入器**：所有异步任务的 `SUCCESS/FAIL` 必须只经新的 repository 精确回写方法写入。旧 `update_pending_execute_status()` 不得在 `IN_PROGRESS` 时写 `execute_time`；同步工具若继续使用它，也必须遵守这一规则。
7. **防重复启动**：只有原子抢占 `PENDING → IN_PROGRESS` 成功的请求才允许提交 MCP 并创建后台轮询协程。抢占失败、重复点击、旧前端请求均只能读取当前状态，绝不能新建第二个轮询器或再次下发。
8. **先切调用方、后删实现**：先让三种模式全部接入统一服务并完成回归验证；确认无调用方后，再删除旧直写、旧前端轮询和泛更新代码。切换期间不得同时启用新旧终态回写。

`confirm_status` 不属于上述轮询器的写入范围：除授权动作将其从 `PENDING` 原子置为 `CONFIRMED/REJECTED` 外，提交、轮询、终态回写均不得修改它。立即退出若业务上视为自动确认，也必须先显式落为 `CONFIRMED`，才能参与统一抢占条件。

### 6.2 发起端点的保留与删除判定

经代码检索后的结论如下，实施时按此切换，避免误删当前前端主流程：

| 端点/函数 | 调用证据 | 处理决定 |
| --- | --- | --- |
| `POST /campaign/confirm` | `demo/campaign-panel/state.js` 的“同意所选 → 确认”直接调用；当前 approve 分支调用 `submit_execution_direct()` | **保留**端点和一次确认交互；重写其 approve 实现接入统一服务。 |
| `POST /campaign/execute` | 在 `app/api/campaign.py` 注册并调用 `submit_execution()`；应用前端源码中无调用点；生产 `preview.log` 与 2026-07-26 至 08-03 的归档日志检索均为 0 次访问，而当前 `/campaign/confirm` 有 130 次访问 | **删除**该历史端点及对应 API 测试；不得再把它作为第二条人工执行路径。共享执行服务本身保留，供立即退出和 `/campaign/confirm` 调用。 |
| `POST /decision/immediate-exit` | 主页面调用，且包含立即退出特有的确定性动作生成和自动确认 | **保留**为立即退出入口，内部改接统一服务。 |
| `POST /decision/immediate-exit/status` 及其前端 5 秒/12 次循环 | 主页面当前仍调用；其实现反查 ERP 执行记录并调用 MCP | 与新后台轮询同时切除；前端如需展示进度，只读新的 pending 状态查询接口。 |

删除 `/campaign/execute` 前须再运行一次全仓库调用检索；本次已补充生产访问日志证据（2026-07-26 至 08-03 均为 0 次），可以随本改造删除。若上线前的最后一次日志复查出现仓外调用，先迁移调用者到 `/campaign/confirm` 的同一交互语义后再删除。

## 7. 已接受的运行边界

本方案刻意不持久化轮询调度信息，因此后台协程只在当前服务进程存活期间有效。

若服务进程在 taskId 返回后、终态写回前重启：

- 该后台轮询与内存队列会停止；
- 三张 pending 行保留 `IN_PROGRESS`，并保留已立即写入的 `task_id`；
- 人工可直接以 pending.`task_id` 调用结果 MCP 核对；`t_advert_agent_modify_advert_record` 仅可作辅助历史审计，不能再作为当前流程的 taskId 来源；
- 系统不得自动重新提交，也不得自动恢复轮询，以避免重复修改广告。

仍存在极窄的崩溃窗口：外部 MCP 已受理、但进程在本地 `task_id` 写入前退出。没有持久化提交账本或 MCP 幂等回执时，无法做到严格零丢失；该情况必须高优先级记录/告警，并由外部执行记录人工核对。

这是用“无需任务表、无需调度字段、无需数据库扫描”换取的明确限制。若未来要求服务重启后自动恢复，才需要另行引入持久化调度状态；taskId 提交即写入 pending 已在本方案中实现。

## 8. 验收标准

1. 三张 pending 表都存在可空 `task_id`；异步 MCP 一返回有效 taskId 即写入精确 pending 行，且不再依赖 ERP 执行主记录表反查 taskId。
2. 立即退出、控制清货、正常权限都使用同一提交与轮询服务。
3. 异步提交成功后，相关 pending 必须保持 `IN_PROGRESS`，不得立刻写 `SUCCESS`。
4. 轮询调度器采用有界队列和固定数量 worker；按 3m、6m、12m、24m 最多查询四次，且不阻塞 HTTP 请求。
5. MCP 明确成功/失败时，按 `pending_id` 精确回写三张表的 `task_id`、`execute_status`、`execute_msg`、`execute_time`；任意未获得 taskId 的提交异常/超时也必须精确写 `FAIL`，且文案表明提交结果未知。
6. 第四次查询仍未成功时，相关 pending 写 `FAIL`，且文案明确为“轮询未确认”，不能伪称广告平台明确失败。
7. 同步新建活动、否词等无 taskId 路径，仍按精确 pending 行写入 `SUCCESS/FAIL`，`task_id` 保持 `NULL`。
8. dry-run 不抢占、不调用异步 MCP、不入轮询队列；`task_id` 保持 `NULL`。
9. 服务进程重启期间的轮询中断不触发自动重提；遗留 `IN_PROGRESS` 可凭已持久化的 `task_id` 由人工核对处理。
