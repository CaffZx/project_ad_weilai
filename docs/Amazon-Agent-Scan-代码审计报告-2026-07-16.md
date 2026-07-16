# Amazon Agent 代码库明显问题扫描报告

## 结论摘要

本报告审计的是代码库，而不是知识图谱文档本身。知识图谱 `00` 至 `14` 仅被用作组件导航顺序；每个结论均以当前代码、SQL、前端调用或测试结果为证据。

- 审计日期：2026-07-16
- 主审计快照：`chenv3.2` / `38761e4fefb9b05065239f81572963637be92c03`
- 补查快照：`feature/core-keyword-policy` / `5c6c6573cba205953b3ba82675aef20efb7fa772`
- 未调用生产 MCP、ERP、Amazon API，也未写生产数据库。
- 未修改任何业务代码、配置、知识图谱或已有测试；本报告是本次唯一新增文件。
- 没有 P0 结论。确认 3 项 P1 代码缺陷、确认 1 项 P1 规格漂移、3 项 P1 可能风险，以及 7 项 P2/P3 或证据缺口。

最需要优先处理的是：执行状态的逐操作真相、前端对失败结果的误标、负向关键词的匹配类型归一化、跨店铺身份隔离，以及不足样本下的强制淘汰规则。

## 范围、限制与方法

### 审计范围

| 对象 | 状态 | 说明 |
| --- | --- | --- |
| `ad-direction-agent/` 主工作树 | 已审计 | Campaign、MCP、缓存、工作流、ERP/State、前端、KB 加载、定时任务、测试 |
| `.worktrees/core-keyword-policy/ad-direction-agent/` | 补查 | 独立分支新增的核心词人工策略、API、SQL、Campaign 接入、前端管理面板 |
| `docs/AD-Agent Project Knowledge Graph/` | 仅作导航 | 不把图谱格式、索引或措辞作为代码缺陷报告；仅在规则与实现冲突时引用其中指向的规范来源 |

主工作树在审计开始时已有用户未提交改动：`demo/codex_ux_patch.js` 删除、`tests/test_advert_exec_child_asin.py` 修改、知识图谱 `02` 修改及 `.superpowers/` 未跟踪。本次未覆盖、还原或混入这些改动。

### 方法与证据边界

1. 按图谱 `00` 至 `14` 的组件顺序，从入口、数据身份、MCP、缓存、规则、执行、前端和测试反向追踪决策链。
2. 对两份工作树分别运行 `amazon-agent-obvious-issue-scan` 文本扫描器。初次全仓扫描得到 1223 条候选，收敛到主活动应用为 296 条；核心词工作树为 406 条。补充报告时再次扫描当前主活动应用，得到 360 条候选。文件集合随工作树状态而变，这些数字只表示检索线索，均不直接作为缺陷。
3. 对候选逐条读取实现和调用方，必要时用本地 fixture 重现；不连接任何生产系统。
4. 结论分为：`confirmed-code-bug`（可由执行路径证明）、`probable-code-risk`（代码模式强但尚缺真实并发/样本）、`design-risk`、`test-gap`、`evidence-gap`。

### 按知识图谱顺序的代码覆盖

| 图谱导航组件 | 实际检查的代码/资产 | 结果 |
| --- | --- | --- |
| 00 总览 | 路由、启动入口、实际工作树 | 完成，确定主链与独立核心词分支 |
| 01 领域模型 | `app/models/campaign.py`、预过滤和执行模型 | 发现活动名称/关键词分组身份风险（A04） |
| 02 ERP 与 State 库 | `schema.sql`、MySQL/Redis repository | 发现跨店铺 ASIN 隔离风险（A05） |
| 03 MCP Schema | MCP client、结果 envelope、映射器、核心词原始字段消费 | 发现结果聚合与提交/成功混淆（A01、A02），以及 campaign ID 返回契约证据缺口（B06） |
| 04 MCP 调用链 | `advert_execution.py`、`portfolio_execution.py` | 发现直接执行状态回写不完整（A01） |
| 05 超时与连接池 | `settings.py`、`batch_via_api.py` | 发现超时重试缺少稳定执行幂等键（A07） |
| 06 缓存 | `redis_cache.py`、`workflow_orchestrator.py` | 与 A05 一起确认身份维度不足 |
| 07 工作流引擎 | guardrails、validation、数据合同 | 发现不足样本淘汰规则与规范冲突（A06） |
| 08 Campaign 引擎 | fetcher、prefilter、grouping、budget | 发现负向匹配类型与活动键问题（A03、A04） |
| 09 KB 切片 | `kb_loader.py`、KB slicing tests | 发现预设长度回归未通过（B02） |
| 10 定时分析 | `batch_via_api.py`、`auto_push.py` | 与 A07 一起确认重试风险 |
| 11 前端 | `demo/campaign-panel/state.js` | 发现全量勾选成功的前端误标（A02） |
| 12 广告执行与安全开关 | 直接执行、组合预算执行、dry-run | 发现 A01、A02、B01 |
| 13 Codex 复核 | 目录、调用点、`maybe_review` 搜索 | 当前仓库缺少可审计运行组件（B04） |
| 14 测试地图 | pytest 及分支定向测试 | 记录 9 个主工作树失败和核心词分支通过情况 |

## P1：需要优先处理的结论

### A01：直接执行对不同操作类型的状态回写不一致，混合批次可把失败项标为成功

- 分类：`confirmed-code-bug`；P1；执行状态、幂等性。
- 证据：
  - `ad-direction-agent/app/workflow/steps/advert_execution.py:451-500` 分别调用异步修改、创建活动和创建否词，并把失败累积到 `errors`。
  - `advert_execution.py:511-518` 仅当 `results["async"]` 存在且无异步错误时，才调用 `_sync_pool_entries_from_exec`。
  - `_sync_pool_entries_from_exec` 在 `advert_execution.py:101-106` 对整个 `plan.ops` 统一写入 `execute_status='SUCCESS'`，没有按 MCP 子操作结果筛选。
  - `advert_execution.py:476-500` 的仅创建或仅否词计划不会产生 `results["async"]`；这些操作即使成功也不回写 pending 状态，可再次被加载。
  - `advert_execution.py:483-500` 中若异步修改成功、创建或否词失败，仍会因 `async_ok` 成立把该计划的全部操作写为 `SUCCESS`。
  - `app/persistence/erp_writer/repository.py:339-379` 的 `load_pending_by_card_ids` 以 pending 状态加载直接执行项，因而未回写项可被重复提交。
- 观察：旧注释 `advert_execution.py:411-413` 仍说直接执行“不写 pending.execute_status”，但实现已部分补写；真实状态是“只对 async 成功批次统一补写”。
- 初版结论复核：早期扫描文字把直接执行概括为“完全不写库”。以当前 `chenv3.2@38761e4fefb9` 代码为准，该说法已不完整：`advert_execution.py:511-518` 已存在部分回写钩子。A01 保留的原因不是旧注释，而是该钩子遗漏 create/negative-only 路径，并会在 mixed batch 中把未成功子操作一并写为成功。
- 推论：创建活动/否词的重复点击或网络重试可能重复下发；混合批次中失败的创建/否词可能无法重试，因为被错误标为成功。
- 未验证假设：MCP 是否天然拒绝重复创建或重复否词，需在非生产模拟环境确认；该外部防重不能替代本地逐项状态。
- 最小验证：构造一个 `create_calls` 或 `negative_calls` 非空、`params_vo_list` 为空的 fake plan，连续调用两次，断言第二次不会再调用 MCP；再构造 async 成功、create 失败的混合计划，断言只有真实成功的 card 状态更新。
- 建议回归：每一类 `plan.ops` 保留 card_id → MCP 结果的映射，使用 `SUCCESS`、`FAILED`、`PARTIAL_SUCCESS` 分别回写，禁止用批次级 `async_ok` 覆盖全计划。

### A02：后端和前端把“已提交/计划数”当成“已成功执行”，失败项会在界面中被标记为已批准

- 分类：`confirmed-code-bug`；P1；批次结果真实性。
- 证据：
  - 直接执行返回 `ops=len(plan.ops)`，即使 `errors` 非空也不扣除失败项：`advert_execution.py:504-522`。
  - 前端在 `demo/campaign-panel/state.js:276-286` 中，只要 `ops > 0` 或有 `task_ids` 就把所有 `selectedKeys` 写为 `approve`，并提示“部分下发成功”；它不根据 card 级结果过滤。
  - 组合预算执行的 MCP envelope 失败或异常会把子操作标为 `FAIL`：`app/workflow/steps/portfolio_execution.py:183-200`，但函数仍在 `:203-209` 无条件返回 `ok: true` 和完整 `applied` 数。
  - 前端仅以 `body.ok === false` 判失败，成功提示采用 `applied`：`state.js:431-442`。
  - 常规 `/campaign/execute` 路径也不保留真实聚合：`advert_execution.py:309-328` 虽为修改操作写入 `FAIL`/`IN_PROGRESS`，但 `:385-386` 随后把整份 `plan.ops` 统一更新为 `IN_PROGRESS`；`errors` 只初始化于 `:294`、未在这条路径追加失败，`async_ok` 仅检查是否拿到 async 响应（`:394-401`），最终 `:403-405` 无条件返回 `ok: true`。这一路径目前没有被 Campaign panel 调用，但仍是已注册的可执行后端链路。
- 推论：操作员会看到“已下发”或本地“已批准”，却无法从界面区分未执行、提交中、部分成功、失败；后续人工重试和审计会被误导。
- 未验证假设：MCP 异步 task 是否在后续有独立回查。即使有，当前同步响应和 UI 仍没有表达该状态。
- 最小验证：让 fake `async_batch_update` 返回失败 envelope 或抛异常；断言接口 `ok=false` 或明确 `PARTIAL_SUCCESS`，前端只更新实际成功 item 的 reviewState。
- 建议回归：API 返回 `submitted_count`、`success_count`、`failed_count`、每 card 状态和 task 查询状态；前端以每个 card 的终态更新，不能以计划数量推断成功。

### A03：预过滤器没有识别已规范化的负向匹配类型，负向关键词可能被当作普通关键词进入候选

- 分类：`confirmed-code-bug`；P1；证据归一化、实体语义。
- 证据：
  - `app/data/campaign_fetcher.py:1055-1080` 会把原始中文匹配类型归一为 `match_type`。
  - 正常读取流程在 `campaign_fetcher.py:304` 已先做归一化。
  - `app/workflow/campaign_prefilter.py:28-51` 只查看中文类型值及 `keyword_match_type`，不查看归一后的 `match_type`。
- 本地复现：对同一关键词提供一条 `BROAD` 和一条已归一的 `NEGATIVEEXACT` 行时，预过滤未将其排除，且会按关键词数判为多词活动；预期应把负向行排除后保留正向单词活动。
- 推论：错误的活动候选、关键词数量、诊断证据会向后传播到策略、预算和执行卡片。
- 最小验证：把上述两行写入 `campaign_prefilter` 单测，断言负向行不计入活动关键词集合；覆盖原始中文和归一英文两种输入。
- 建议回归：先将 match type 统一到一个规范字段，再由预过滤器只读取该字段；对 `NEGATIVEEXACT`、`NEGATIVEPHRASE` 和空值建立参数化测试。

### A04：活动分组键缺少稳定广告实体维度，重名活动或同名不同匹配类型可发生覆盖或预算重复计入

- 分类：`probable-code-risk`；P1；实体身份、分组。
- 证据：
  - `app/models/campaign.py:3,44` 将 CampaignUnit 的业务键表述为“活动名 × child ASIN”。
  - `campaign_prefilter.py:39-51` 按 `keyword_text` 聚合，并未把 `match_type` 纳入活动内关键词身份。
  - `campaign_fetcher.py:149-158` 建立名称到 ID 的映射，`campaign_fetcher.py:352-401` 仍以名称索引输出行。
  - `campaign_fetcher.py:1003-1011` 的活动键没有纳入 campaign_id、keyword_id 和 match_type。
  - `app/workflow/steps/campaign.py:651,1213,1371,1870` 以该键建立 `unit_by_key`、汇总预算、校验期望集合和回填执行上下文。
- 本地复现：同一 campaign_id、同一 keyword_text、`EXACT` 与 `BROAD` 两条记录可同时存活，但产生同一个分组键；`unit_by_key` 只保留一个，而预算汇总仍遍历两条。
- 推论：有重名活动、同词多匹配类型或跨子体重复记录时，详情可能“最后写入者覆盖”，总预算却重复计算，执行目标也可能错位。
- 未验证假设：实际 MCP 数据是否允许这些组合；不能把“不常见”当作身份约束。
- 最小验证：用两个相同活动名但不同 `campaign_id` 的单词活动，以及 EXACT/BROAD 同词 fixture，断言生成键唯一、预算只计算一次、执行卡可准确回查到 campaign_id/keyword_id。
- 建议回归：以 shop/profile + campaign_id + ad_group_id + keyword/target_id + match_type 构造稳定键；名称只用于展示。

### A05：State、缓存、锁和部分数据读取以 ASIN 单维度隔离，跨店铺同 ASIN 可能串读、串锁或串写

- 分类：`probable-code-risk`；P1；身份隔离、缓存、持久化。
- 证据：
  - `app/persistence/schema.sql:5-108` 的 State 表主键或唯一维度以 ASIN 为核心；`budget_override` 位于 `:34-37`，调整记录唯一维度位于 `:51`。
  - `app/persistence/mysql_state_manager.py` 多处 `WHERE asin` 读取/更新；`app/persistence/redis_cache.py:22-45,73-119` 的 cache key 只包含 ASIN/日期/筛选。
  - `app/workflow/workflow_orchestrator.py:75-105` 的缓存键只有 ASIN，命中时仅检查存在任一 identity 数据，而非与当前 shop/profile 完整匹配。
  - `app/workflow/steps/campaign.py:84-91` 的锁与缓存同样以 ASIN 为键，并以“部署假设一个父 ASIN 对应一个店铺”解释，没有运行时强制。
  - `app/data/mcp_db_context.py:49-92` 只以 parent ASIN 查询上下文并取 `rows[0]`；`app/api/campaign.py:308-316,361-365,394-407` 接受 ERP 身份覆盖，但基础聚合并非始终把覆盖一路带入。
- 推论：若同一个 parent ASIN 出现在两个 shop/profile，可能复用错误决策快照、锁住另一店铺任务、取错产品上下文或持久化覆盖。
- 未验证假设：生产数据是否存在跨店同 ASIN；这需要只读数据核验，不应由本审计连接生产。
- 最小验证：在隔离测试库插入同 ASIN、不同 shop/profile 的两套记录；断言缓存键、锁键、State 主键和 MCP 查询都可并存且互不命中。另申请只读统计：`GROUP BY parent_asin HAVING COUNT(DISTINCT shop_id)>1`。
- 建议回归：将 shop_id/profile/site 与 parent_asin 组成全链路身份对象，并用于表唯一键、缓存、锁、repository WHERE 条件和所有 MCP 入参。

### A06：不足样本时的强制淘汰规则与当前知识规则冲突

- 分类：`design-risk`（实现路径已确认）；P1；不足数据保护。
- 证据：
  - 规则 KB `docs/knowledge_base/执行规则/21-淘汰广告活动规则.md:30-38` 写明样本少于 3 天不淘汰，未声明价格/预算例外。
  - `docs/knowledge_base/执行规则/17-问题诊断与动作优先级.md:61,112,145` 要求样本不足时观察，不做大的 bid/budget 动作或淘汰。
  - `app/workflow/campaign_guardrails.py:134-156` 对 P1 有样本不足保护；但 `:177-220` 在无订单且 bid ≤ 0.10 或 budget ≤ 1.00 时强制 P3 淘汰，辅助判断在 `:492-496`。
  - `tests/workflow/test_campaign_guardrails.py:461-470` 明确把“样本不足 + 低 bid”断言为 P3 `ELIMINATE`。
- 推论：当前实现并非漏实现，而是“当前规则文本”和“已测试的代码策略”存在优先级冲突；若无经批准的例外，低价探索活动可被过早淘汰。
- 未验证假设：P3 是否有业务批准但未补入知识规则。需要产品/策略负责人确认，而非由代码审计自行裁决。
- 最小验证：确认 P3 例外的规范地位；若保留例外，在 KB、引擎规则说明和测试中统一写明适用条件与最小观察窗口。
- 建议回归：为 `days_online=2`、无订单、低 bid/低 budget 建立明确的“观察”与“例外淘汰”边界矩阵。

### A07：定时批量重试没有贯穿网关到执行层的稳定幂等键

- 分类：`probable-code-risk`；P1；定时执行、重复提交。
- 证据：
  - 根目录 `batch_via_api.py:43,112-126,168-177` 使用 240 秒请求超时并重试，但请求体没有稳定 `run_id`/idempotency key。
  - `app/config/settings.py:124` 的 Campaign 内部总超时为 900 秒；图谱导航的定时模块也记录了该内外超时差异。
  - `app/workflow/steps/campaign.py:280-307` 自行生成时间戳 run_id，并在 ERP 写入前释放锁。
  - `app/workflow/auto_push.py:49-50` 仅在上游传入 run_id 时才稳定复用；否则使用当前时间。
- 推论：网关在 240 秒超时后重试时，首个请求仍可能继续运行，第二个请求会得到新的 run_id 并重复分析、写入或触发后续自动动作。
- 未验证假设：上游调度器是否已有幂等 headers 或请求去重；当前代码路径未显示其被接收并持久化。
- 最小验证：本地将首请求人为阻塞超过 240 秒后重发同一调度事件，断言只有一个 run_id、一个决策批次和一组后续动作。
- 建议回归：调度器生成稳定 run_id/idempotency key，网关和 repository 以该键原子领取任务；将“已提交、运行中、完成、可安全重试”显式持久化。

## P2/P3、测试缺口与证据缺口

### B01：广告执行 dry-run 在组合归属解析之前提前返回，现有回归测试失配

- 分类：`confirmed-code-bug`；P2；测试/模拟语义。
- 证据：`advert_execution.py:278-288` 在 dry-run 时提前返回，未执行后续 `:296-336` 的组合查询、严格匹配和 move error 收集。
- 测试结果：`tests/workflow/test_portfolio_match_and_exec.py` 有 3 项失败，均期望 dry-run 仍进行组合查询/暴露 move errors。
- 最小验证：明确 dry-run 的产品语义：若应预演验证，保留归属解析但禁止写 MCP；若不应预演，调整测试和接口说明以明确“仅构建计划”。

### B02：知识库预设长度与数据合同的测试未跟随当前实现

- 分类：`test-gap`；P2。
- 证据：
  - `tests/test_kb_slicing.py:42-47` 断言 `new_campaign` 不超过 11000 字符；当前 `app/knowledge/kb_loader.py:82` 组装结果为 13360 字符，测试失败。
  - `tests/test_data_contract.py:40-45` 仍期望缺关键词为 `blocked`，而当前数据合同和规则把该场景降级为 `degraded`，定向测试失败。
- 最小验证：明确 KB token/字符上限与缺关键词的业务门禁等级，再更新实现或测试，不应只为绿灯删除断言。

### B03：核心词策略的 30 词上限在并发写入下没有数据库级保护

- 分类：`probable-code-risk`；P2；仅针对未合并的 `feature/core-keyword-policy` 工作树。
- 已验证的正面事实：`scripts/erp_db/migrate_core_keyword_policy.sql:21-24` 的唯一键包含 `parent_asin + parent_seller_sku + shop_id + keyword_norm`，可防止同一产品同一规范化关键词的重复策略行；核心词定向测试 27 项全部通过。
- 风险证据：`app/persistence/erp_writer/repository.py:2081-2113` 先读取最新任务、策略和有效词数量，在 `:2100-2101` 做 Python 层 `<=30` 判断，再 upsert；读取没有 `FOR UPDATE`，表也没有可表达“每产品最多 30 个有效词”的约束。
- 推论：两个并发请求都从 29 看到 29，并分别锁定不同词时，最终可能得到 31 个有效核心词。
- 最小验证：在测试库用两个独立连接并发提交第 30 和第 31 个 `LOCKED`，断言最多一个成功；若产品要求强上限，则需事务锁、版本 CAS 或单独的聚合约束。

### B04：Codex 复核组件在当前代码库中缺少可审计运行证据

- 分类：`evidence-gap`；P2。
- 证据：图谱导航指向的 `AD_Agent_codexV2` 目录在当前工作区和本仓库均不存在；对活动代码搜索未找到当前运行链上的 `maybe_review` 调用。
- 结论：不能据此断定复核功能有缺陷，也不能把说明文档当作已部署实现。
- 最小验证：提供实际部署仓库/工作树、服务入口、调用日志或运行配置后，再追踪其输入快照、超时、隔离和结果消费路径。

### B05：金额和出价仍混用 float/DOUBLE，尚未复现精度错误

- 分类：`evidence-gap`；P3。
- 证据：Campaign 模型、执行映射和 State `budget_override` 使用 float/DOUBLE；ERP 写入侧存在 Decimal 转换但未看到统一执行精度/舍入模式的端到端约定。
- 未将其升级为代码缺陷：本次没有观察到 0.01 级金额或 bid 被错误下发的可复现样本。
- 最小验证：用 `0.1 + 0.2`、三位小数 bid、百分比边界值贯穿 API JSON、MCP 参数、ERP DTO、数据库和 UI，确认刻度与舍入模式。

### B06：核心词消费的 campaign ID 字段没有由可版本化 MCP Schema 固定

- 分类：`evidence-gap` / `test-gap`；P2；MCP Schema、核心词离线分析。
- 证据：`app/data/core_keyword_fetcher.py:192-200` 为 `ad_campaign_product_keyword_list` 同时尝试 `campaign_id`、两个中文别名，并在 campaign ID、关键词或活动名任一缺失时静默跳过该行；图谱的 MCP 说明列出了该工具的功能和入参，但没有给出该返回行的字段契约或版本号。
- 已观察到的测试信号：主工作树的 4 项核心词测试 fixture 未给出当前 fetcher 所要求的 campaign ID，触发“核心词候选为空”；独立 `feature/core-keyword-policy` 分支已把相应 fixture 和策略路径补到 27 项通过，但这不证明外部 MCP 的生产字段恒定。
- 推论：若上游字段改名、大小写变化或在某类店铺响应中缺失，核心词离线任务会把有效活动静默过滤，随后只返回“候选为空”，而不是可诊断的 Schema 错误。
- 最小验证：从非生产或脱敏日志保存一份真实 `ad_campaign_product_keyword_list` 响应，建立 JSON Schema/字段别名契约测试；至少覆盖 `campaign_id`、活动名、关键词、match type、child ASIN。

### B07：知识图谱维护入口存在编号漂移（不作为代码缺陷）

- 分类：`spec-drift`；P3；仅文档卫生，不影响 A01-A07 的代码结论。
- 证据：`docs/AD-Agent Project Knowledge Graph/README.md:22` 的目录仍列出 `15-项目知识图谱维护规则.md`，而当前目录中的实际文件为 `99-项目知识图谱维护规则.md`。
- 推论：后续维护者可能按 README 访问不存在路径，漏读维护规则；这不会改变本次代码审计的优先级。
- 最小验证：将 README 条目更新为实际文件名，并运行一次目录链接存在性检查。

## 测试与扫描记录

| 工作树 | 命令/集合 | 结果 | 解读 |
| --- | --- | --- | --- |
| 主工作树 | KB、MCP discover、guardrails、conflicts、portfolio、persistence、orchestrator cache 等 8 组 | 131 通过，4 失败 | 1 个 KB 长度问题，3 个 dry-run 组合执行期望失配 |
| 主工作树 | data contract、lightweight loading、analysis state、override persistence | 17 通过，1 失败 | 缺关键词状态由 blocked 改为 degraded 后测试未同步 |
| 主工作树 | `tests/workflow/test_core_keyword.py` | 14 通过，4 失败 | 旧 fixture 缺少当前 fetcher 所要求的 campaign_id，导致候选为空 |
| 主工作树合计 | 上述定向集合 | 162 通过，9 失败 | 不是完整 pytest；失败已分别归入 B01/B02 或 B06 所述的核心词夹具/Schema 契约漂移 |
| 核心词工作树 | `python -m pytest -q -p no:cacheprovider tests/workflow/test_core_keyword.py` | 27 通过 | 新增策略相关单测通过；未覆盖 B03 的真实并发场景 |

扫描器的原始候选结果没有作为缺陷计数使用。报告中的每一项均经实现、调用链或本地测试复核。

## 推荐处理顺序

1. 先修 A01/A02：把执行结果改为每 card/per-operation 状态；禁止以 `ops`、task 提交或任一子批次成功推断全量成功。该项同时降低重复写和操作员误判风险。
2. 修 A03：统一负向匹配类型的规范字段，并加入原始/规范化输入的回归。
3. 由策略负责人裁决 A06，再让 KB、引擎、测试采用单一优先级；在结论未统一前，暂停把不足样本低价活动自动淘汰。
4. 设计并落地 A05 的复合身份迁移；先补 read-path/caching/locking 测试，再制定 State 表和缓存键的兼容迁移。
5. 为 A07 引入调度 run_id 和原子任务领取，再调整网关超时/重试策略。
6. 合并核心词策略分支前补 B03 的并发测试与保护，并修复 B01/B02/B06 的语义、测试或 Schema 契约漂移。

## 建议的最小回归用例清单

1. create-only、negative-only、async-only、mixed partial-failure 四类广告执行计划的逐 card 状态与二次重试。
2. MCP 返回成功 envelope、失败 envelope、异常、无 task_id、部分 task_id 的 UI/API 状态矩阵。
3. `BROAD + NEGATIVEEXACT`、`EXACT + NEGATIVEPHRASE`、中文原值与英文规范值的预过滤矩阵。
4. 相同名称不同 campaign_id、相同词不同 match_type、不同 child ASIN 的稳定 identity/property tests。
5. 同 ASIN、不同 shop/profile 的缓存、锁、State、MCP context、API identity 端到端隔离。
6. 240 秒网关超时下的重复调度测试，验证只产生一个稳定 run_id 与一个可执行批次。
7. 样本不足与低 bid/低 budget 的 guardrail 边界表，覆盖观察、例外淘汰和人工确认。
8. 核心词 29→30→31 的并发锁定、重复关键词规范化、最新任务切换 CAS。

## 需要补充的证据

- 只读的跨店铺同 parent ASIN 统计，确认 A05 的生产暴露面。
- 非生产 MCP sandbox 的重复创建/重复否词行为，确认 A01 外部幂等边界。
- 调度器实际请求体、headers 与运行日志，确认 A07 是否已有上游幂等键。
- P3 低价/低预算淘汰例外的业务批准来源，裁决 A06。
- 实际 Codex 复核服务的仓库或部署工件，解除 B04 的证据缺口。
