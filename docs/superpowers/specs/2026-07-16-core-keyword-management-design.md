# 核心词人工状态管理 — 设计方案

**日期：** 2026-07-16

**范围：** `ad-direction-agent` 核心词离线任务、Campaign 分析读取链路、Tab5 `campaign-panel`。
**状态：** 已修订，待复核后实施。

## 1. 目标与边界

在 Tab5 批量工具栏的“同意所选”左侧增加“核心词管理”。运营可对已有活动关键词设置四种互斥状态：锁定、启用、不启用、否决。

人工状态与 AI 离线任务完全分离：

- AI 的语义核心、数据核心、冲突结论和证据仍落入既有 `task` / `label` 表，不被人工操作改写。
- 人工策略即时覆盖后续 Campaign 对 `is_core` 的读取；其 AI 基础集合必须与 Campaign 主分析当前实际消费的集合完全一致。
- Campaign LLM 仍分析全部活动；锁定并不把活动排除出 Campaign LLM。
- 锁定与否决仅把词排除出**核心词离线判定任务**；不启用仍参加下次离线判定。
- 本期仅允许从已有活动关键词池搜索、选择后添加；不支持自由输入。
- KB29 的有效核心词上限 30 条包含人工锁定词；超过 30 时拒绝新增或锁定。

不在本期范围内：操作历史审计表、自由录入新词、修改 Campaign 的审核/执行按钮语义。

## 2. 现状与问题

AI 核心词当前由两张表承载：

- `t_advert_agent_core_keyword_task`：一次离线任务及统计。
- `t_advert_agent_core_keyword_label`：逐词 `semantic_core`、`data_core`、证据、最终 `is_core`。

既有 AI 判定公式来自 KB29：

```text
ai_is_core = semantic_conflict != fail
             AND (semantic_core OR data_core)
```

Campaign 当前消费的 AI 基础集合不是“任意 DONE 历史任务”，而是现有 `fetch_core_keyword_set()` 的查询结果：同一产品三元身份下，`task.status='DONE'`、`task.started_at` 为该产品最大值、并且 `label.is_core=1` 的 `label.keyword_text`。概念 SQL 如下：

```sql
SELECT l.keyword_text
FROM t_advert_agent_core_keyword_label l
JOIN t_advert_agent_core_keyword_task t ON t.id = l.task_id
WHERE t.parent_asin = :parent_asin
  AND t.parent_seller_sku = :parent_seller_sku
  AND t.shop_id = :shop_id
  AND t.status = 'DONE'
  AND l.is_core = 1
  AND t.started_at = (
    SELECT MAX(t2.started_at)
    FROM t_advert_agent_core_keyword_task t2
    WHERE t2.parent_asin = :parent_asin
      AND t2.parent_seller_sku = :parent_seller_sku
      AND t2.shop_id = :shop_id
      AND t2.status = 'DONE'
  );
```

管理弹窗的 AI 基础列表和 Campaign 的默认核心集合都必须复用该口径。直接修改标签的 `is_core` 虽能短期生效，但会污染 AI 任务事实，且无法表示永久锁定、临时不启用和永久否决。因此新增独立策略表。

本地 ERP 数据还显示 `task.core_keyword_count` 与实际标签 `is_core=1` 的数量不一致。任何新 UI 的“有效数量”必须以 AI 标签和人工策略实时合并的结果计算，不可使用任务汇总列。

## 3. 人工状态模型

### 3.1 四种状态

| 状态 | UI 颜色 | 有效 `is_core` | 核心词离线任务 | Campaign LLM |
|---|---|---:|---|---|
| `LOCKED` 锁定 | 紫色 | `true` | 跳过该词 | 正常进入，携带 `is_core=true` |
| `ENABLED` 启用（默认） | 绿色 | 跟随 AI | 正常参与 | 正常进入 |
| `DISABLED` 不启用 | 灰色 | `false` | 正常参与下次判定 | 正常进入，携带 `is_core=false` |
| `VETOED` 否决 | 红色 | `false` | 跳过该词 | 正常进入，携带 `is_core=false` |

状态是互斥的。竖三点菜单永远只显示当前状态之外的另外三种状态，避免“取消”这一额外概念。

### 3.2 有效值优先级

```text
LOCKED                    -> effective_is_core = true
VETOED                    -> effective_is_core = false
DISABLED                  -> effective_is_core = false（仅至下一次成功离线任务）
ENABLED / 无策略           -> effective_is_core = ai_is_core
```

`DISABLED` 是临时覆盖。每次成功持久化新的离线任务后，状态表会完成一次轮换：原 `DISABLED` 复位，新的 `label.is_core=1` 统一成为默认 `ENABLED`。新任务不再推荐的词不再属于 AI 基础列表；只有 `LOCKED`、`VETOED` 等人工例外继续保留。

### 3.3 为什么绑定任务版本

当前离线任务 ID 按“产品身份 + 日期”生成，同日重跑可能复用 task ID。因此只保存 `task_id` 不能识别同日的下一次离线结果。`base_task_id` 与 `base_task_finished_at` 记录策略写入时所见的 AI 任务版本，用于接口并发校验和追溯；POST 必须携带这两个最新版本值，若任务在弹窗打开后已重跑则拒绝旧页面写入并要求刷新。

## 4. 数据库设计

新增迁移文件：`scripts/erp_db/migrate_core_keyword_policy.sql`。

```sql
CREATE TABLE IF NOT EXISTS t_advert_agent_core_keyword_policy (
    id                    BIGINT NOT NULL AUTO_INCREMENT,

    parent_asin           VARCHAR(20)  NOT NULL,
    parent_seller_sku     VARCHAR(128) NOT NULL,
    shop_id               BIGINT       NOT NULL,

    keyword_text          VARCHAR(512) NOT NULL,
    keyword_norm          VARCHAR(512) NOT NULL,

    state                 ENUM('LOCKED', 'ENABLED', 'DISABLED', 'VETOED')
                          NOT NULL DEFAULT 'ENABLED',

    base_task_id          VARCHAR(32)  NULL,
    base_task_finished_at DATETIME(6)  NULL,

    operator              VARCHAR(64)  NULL,
    created_at            DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at            DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                                           ON UPDATE CURRENT_TIMESTAMP(6),

    PRIMARY KEY (id),
    UNIQUE KEY uk_product_keyword (
        parent_asin, parent_seller_sku, shop_id, keyword_norm
    ),
    KEY idx_product_state (
        parent_asin, parent_seller_sku, shop_id, state
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

说明：

- `keyword_text` 用于原样展示，`keyword_norm` 用于唯一性和运行时匹配。
- 既有 AI `label` 表只有 `keyword_text`，没有 `keyword_norm`：解析器读 label 时按同一规则即时计算规范值；策略表持久化两者。归一化规则为 Unicode 文本 `strip`、连续空白压为一个空格、`casefold`。Campaign 判断 `CampaignUnit.is_core` 时也必须以该规范值匹配，不能继续做原始字符串的精确集合判断。
- 表只保存“当前状态”，不保存完整操作日志。本期不需要第二张审计表。
- 无策略行在语义上等价于 `ENABLED`。每次离线任务写入后，会为本轮 `is_core=1` 标签刷新 `ENABLED` 行；旧的默认 `ENABLED` 行不跨轮次保留。
- `base_task_*` 保存写入或刷新该行时对应的最新任务版本；锁定、否决等跨轮次策略也可保留最后一次操作时的版本信息。
- 不建立到 `task` 的外键：`LOCKED` 与 `VETOED` 必须跨任务长期保留。

## 5. 后端设计

### 5.1 Repository

在 `app/persistence/erp_writer/repository.py` 增加以下职责：

1. `get_latest_core_keyword_task(identity)`：以现有 `MAX(started_at) + status='DONE'` 口径读取该产品当前 AI 任务的 ID、完成时间和全部标签。
2. `list_core_keyword_management(identity)`：以该任务 `is_core=1` 标签作为 AI 基础行，再合并策略表中的人工例外行，生成单列表。
3. `list_core_keyword_word_pool(identity)`：仅返回该任务**全部** `label.keyword_text`（不限 `is_core`），按 `keyword_norm` 去重；不读取 Campaign 快照、不读取历史任务、不额外调用 MCP。
4. `upsert_core_keyword_policy(identity, keyword, state, expected_task, operator)`：原子写入状态；写前验证关键词来自当前任务词池，并校验产品三元身份和任务版本。
5. `resolve_core_keyword_policy(identity)`：先复用现有 AI 基础集合，再叠加策略，返回 `effective_core` 规范化集合。逻辑为 `(ai_base_core ∪ locked) - disabled - vetoed`。
6. `sync_core_keyword_policies_after_task(identity, new_task)`：在新 task / label 成功写入的同一事务内，保留 `LOCKED`、`VETOED`，清理过期默认行和临时 `DISABLED`，再把本轮 `is_core=1` 标签刷新为 `ENABLED`（但不得覆盖锁定或否决）。

`resolve_core_keyword_policy()` 是 Campaign 和管理接口的唯一状态解析入口，避免各调用点自行拼优先级。

### 5.2 管理接口

在 `app/api/core_keyword.py` 增加：

#### `GET /core-keyword/management`

必填查询参数：`parent_asin`、`parent_seller_sku`、`shop_id`。

返回：

```json
{
  "ok": true,
  "effective_core_count": 8,
  "limit": 30,
  "latest_task": {"id": "ckt...", "finished_at": "..."},
  "rows": [
    {
      "keyword_text": "floral midi dress",
      "types": ["semantic", "data", "manual"],
      "semantic_evidence": [],
      "data_evidence": [],
      "manual_reason": "人工覆盖",
      "state": "LOCKED",
      "effective_is_core": true
    }
  ],
  "word_pool": ["..."]
}
```

列表的 AI 基础行严格等于 Campaign 当前消费的 `label.is_core=1` 集合。另追加存在人工策略的例外行：例如 `LOCKED` / `VETOED` 因而被排除出后续离线任务、在当前任务没有 label 时，仍必须显示，用户才可将其切换回其他状态。它们不是历史 AI 推荐行；不创建额外 Tab。

对于已被 `LOCKED`/`VETOED` 排除出最新离线任务的词，接口读取该词最近一条历史 AI label 用于展示原始语义/数据证据；无历史 AI label 的人工添加词显示“人工覆盖”。

#### `POST /core-keyword/policy`

请求体：

```json
{
  "parent_asin": "...",
  "parent_seller_sku": "...",
  "shop_id": 1622,
  "keyword_text": "...",
  "state": "LOCKED",
  "expected_task_id": "ckt...",
  "expected_task_finished_at": "...",
  "operator": "user-id"
}
```

服务端规则：

- `expected_task_id` 与 `expected_task_finished_at` 必须匹配当前最新 DONE 任务；不匹配返回 `409`，客户端刷新后重试。
- 四种状态均记录当前任务版本；`ENABLED` 恢复跟随当前 AI 结果。
- `LOCKED` 会使有效核心词数增加时，先计算合并后的数量；超过 30 返回业务错误，不写入。
- 不允许自由文本：新增策略时 `keyword_norm` 必须存在于服务端当前词池；对已有的 `LOCKED` / `VETOED` 例外行允许直接切换状态，即使它已被排除而不在当前词池中。
- 成功后返回该词的最新管理行和新的 `effective_core_count`。

### 5.3 Campaign 读取链路

当前 [campaign.py](../../../ad-direction-agent/app/workflow/steps/campaign.py) 读取的是一个核心词 `set`。改为调用策略解析器：

```text
AI 最新标签
  + 人工策略表
  -> CoreKeywordPolicyResolution
  -> effective_core_keyword_set
  -> CampaignUnit.is_core
```

Campaign 不删除、跳过或隐藏任何活动。所有活动仍进入既有 LLM 分析；变化仅是进入 prompt 与护栏的 `is_core` 值：

- `LOCKED` 词为 `true`，保留 P0 核心词禁淘汰保护。
- `DISABLED` 和 `VETOED` 词为 `false`，不享受核心词保护。
- `ENABLED` 跟随 AI 标签。

### 5.4 核心词离线任务

在 `run_core_keyword_analysis()` 拉数前读取 `LOCKED` 与 `VETOED` 的规范化关键词集合，并把它们传给 `CoreKeywordFetcher`：

1. 仍拉取产品上下文、Listing 与活动清单。
2. 在活动关键词去重后、性能/排名查询与语义 LLM 调用前，过滤 `LOCKED`、`VETOED` 词。
3. `DISABLED` 与 `ENABLED` 词继续走完整离线分析。
4. 成功写完新的 AI task / label 后，在同一 ERP 事务调用 `sync_core_keyword_policies_after_task()`：复位临时不启用、刷新本轮推荐词为默认启用、保留锁定与否决。

这保证锁定、否决词不浪费核心词离线计算；同时不会影响 Campaign LLM 的分析覆盖面。

## 6. 前端设计

### 6.1 入口与加载

在 `campaign-panel/panel.js` 静态工具栏内，把“核心词管理”按钮放在“同意所选”左侧。该按钮与“同意所选”使用同一最新批次门禁（仅 `state.executable && vm.is_latest` 时展示）。点击后：

1. 从当前页面 ERP 参数取产品三元身份。
2. 请求 `GET /core-keyword/management`。
3. 在现有 `camp-modal-mount` 渲染一个管理弹窗；不新增 Tab，不影响原有“明细/汇总/告警”。

身份参数缺失时按钮禁用并提示，不能以仅 ASIN 的不完整身份读取或写入策略。

### 6.2 单列表

列固定为：

| 列 | 展示规则 |
|---|---|
| 核心词 | `keyword_text` 原样显示 |
| 核心类型 | 可多标签：语义核心、数据核心、人工 |
| 核心原因 | 同时展示语义证据与数据证据；有人工状态时补“人工覆盖” |
| 状态 | 锁定=紫、启用=绿、不启用=灰、否决=红 |
| 操作 | 竖三点，菜单仅显示其余三种状态 |

弹窗标题右侧只显示“当前有效核心词 N / 30”，不显示 AI 推荐数量。

### 6.3 添加核心词

列表底部显示 `＋ 添加核心词`：

- 打开支持模糊搜索的可选下拉框。
- 只展示 API 返回的当前最新核心词离线任务的 label 词池。
- 选中后默认提交 `LOCKED`。
- 不允许自由键入，也不在前端伪造候选词。

### 6.4 操作体验

- 点击状态后立即禁用当前行菜单并显示提交态；成功后使用后端返回的行替换局部状态，并更新有效数量。
- 锁定超过 30 时展示后端错误，保持原状态。
- 标签 JSON 解析失败、AI 历史证据缺失时，保留状态操作但将原因显示为“无可用 AI 证据”。
- 延续现有固定宽度、可复制的 Toast 行为来展示操作结果。

## 7. 数据迁移与兼容

1. 先执行新增表 DDL；不迁移、不回写 AI `label` 数据。
2. 初始策略表为空，所有词等价于 `ENABLED`，上线不会改变当前 Campaign 行为；首个成功离线任务会按新规则刷新默认 `ENABLED` 状态行。
3. 旧 `fetch_core_keyword_set()` 可在内部暂时调用新解析器的 `effective_core` 集合，保证调用方平滑迁移，同时保持原 AI 基础查询口径。
4. 管理数量永远从当前 AI 基础标签和策略合并计算，避开现存 `core_keyword_count` 汇总不一致问题。

## 8. 测试与验收

### Repository / API

- 产品三元身份隔离、关键词归一化、同词 UPSERT。
- 四种状态的解析优先级正确。
- 每个成功离线任务后，所有临时 `DISABLED` 均复位；本轮 `is_core=1` 标签默认刷新为 `ENABLED`，同日重跑和跨日重跑都覆盖验证。
- `LOCKED` 与 `VETOED` 跨离线任务持续存在。
- 锁定 / 添加超过 30 条被拒绝；从锁定切回启用后可重新新增。
- 词池外关键词被拒绝；词池严格只含最新任务全部 label；人工例外行能返回历史 AI 证据。

### 核心词离线任务 / Campaign

- 锁定、否决词不参与核心词离线的性能、排名、语义 LLM 请求。
- 不启用词仍在下次离线任务中得到新 AI 标签。
- Campaign 全量活动仍进入 LLM；锁定词 `is_core=true`，不启用/否决词 `is_core=false`。
- P0 核心词禁淘汰护栏仅对有效核心词继续生效。

### 前端

- 按钮位置正确，身份缺失门禁正确。
- 单列表显示多类型、多证据、四种状态颜色和互斥菜单。
- 添加框只允许选择词池项，模糊搜索可用。
- 成功、失败、30 条上限均能刷新行和数量，且不影响批量审核工具栏。

## 9. 实施文件清单

- 新增：`scripts/erp_db/migrate_core_keyword_policy.sql`
- 修改：`app/persistence/erp_writer/repository.py`
- 修改：`app/api/core_keyword.py`
- 修改：`app/workflow/steps/core_keyword.py`
- 修改：`app/data/core_keyword_fetcher.py`
- 修改：`app/workflow/steps/campaign.py`
- 修改：`demo/campaign-panel/panel.js`
- 修改：`demo/campaign-panel/state.js`
- 修改：`demo/campaign-panel/events.js`
- 修改：`demo/campaign-panel/render.js`
- 修改：`demo/campaign-panel/panel.css`
- 新增 / 修改：覆盖上述行为的 repository、workflow、API 与前端测试。
