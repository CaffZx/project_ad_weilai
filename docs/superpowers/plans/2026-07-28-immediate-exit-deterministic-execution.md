# “立即退出”确定性执行链路 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 运营保存“立即退出”后，不进入任何 Campaign LLM，通过已有数据 MCP、ERP Decision/Pending 和广告执行 MCP，完成可追踪、可确认、可重试、可查询终态的确定性退出链路。

**Architecture:** 保留 `/strategy/confirm` 作为战略层唯一保存入口，在其成功后调用新增的 `/decision/immediate-exit`。后端在一个专用编排中校验已持久化经营模式，按最小依赖拉取活动、关键词和基础广告数据，生成现有 `CanonicalRun`、card 与 pending，落库并置 latest，结束分析事件后自动确认，再强制从数据库回读 pending 构造执行参数。异步提交只写 `IN_PROGRESS/FAIL`；终态由 `agent_batch_update_advert_result` 查询，成功后再写 `SUCCESS` 和低价池。

**Tech Stack:** FastAPI、Python asyncio、Pydantic/dataclass、PyMySQL、pytest、原生 HTML/JavaScript、现有数据 MCP 与 `whp-advert-agent` MCP。

---

所有命令均在以下目录执行：

```text
D:\project\AD_Agent_work_place6.17\AD_assistant_agent-v3.2\ad-direction-agent
```

执行协议：一次只实施一个 Task；完成该 Task 的失败测试、最小实现和通过验证后立即停止，向用户报告改动文件、真实调用链、测试命令和输出。未获得继续指示前不进入下一 Task。

## 0. 实施边界与不可破坏项

本计划按以下真实调用顺序实施，不允许横向跳步：

```text
保存“立即退出”
  → /strategy/confirm 保存战略层配置
  → /decision/immediate-exit
  → 后端回读并校验 operating_mode=立即退出
  → 拉取活动全集 + 关键词明细
  → 按活动 ID 拉 basic_info_v2
  → 丢弃否词并处理单词/多词/商品投放活动
  → 写 decision + summary + card + campaign_pending/keyword_pending
  → finalize_batch 置 latest
  → clear_analysis_session 结束分析事件
  → confirm_decisions 自动 CONFIRMED
  → load_confirmed_pending 从数据库回读
  → build_exec_plan 构参
  → agent_async_batch_update_advert
  → execute_status=IN_PROGRESS/FAIL
  → 按 decision_id 回读 ERP taskId
  → agent_batch_update_advert_result
  → execute_status=SUCCESS/FAIL
```

必须保持：

- 普通经营模式继续走现有 Campaign 分析，不受本链路影响。
- `OperatingMode` 仍属于战略层；广告权限只由 `operating_mode_to_permission()` 现场映射，不新增持久化权限字段。
- 实时 Campaign 与定时 Campaign 遇到“立即退出”仍只短路 LLM，不自动重复执行退出。真正执行只由保存“立即退出”触发。
- 不调用 `DataAggregator.fetch()`，不调用 Campaign LLM、Purpose Agent、护栏、搜索词、排名、竞品、指标聚合等无关资源。
- 不调用 `submit_execution_direct()`；该路径绕过本方案要求的“先 CONFIRMED、再从数据库回读 pending”。
- 不新建 `target_pending`，不把 `targetId/targetBid` 塞入 `keyword_pending`，不生成 `targetShowVoList`。
- 本期商品投放活动只能执行活动级降预算和迁组，不能宣称完成 Target Bid 低价化。
- 不创建新的业务源码模块；专用私有函数放入现有 `app/api/decision.py`，避免扩大公共 API 和影响正常 Campaign Fetcher。
- 当前工作区有用户未提交改动；每个任务只修改列出的文件，完成定向测试后停下报告，不自动提交。

## 1. 文件改动地图

| 文件 | 职责与改动 |
|---|---|
| `ad-direction-agent/demo/ad-asisitant-agent.html` | 保存战略层成功后调用立即退出接口；不再提前 cancel；刷新批次、快照和执行状态 |
| `ad-direction-agent/app/api/decision.py` | 新增立即退出 API、严格校验、数据编排、确定性动作生成、落库/确认/执行串联、状态查询 API |
| `ad-direction-agent/app/data/campaign_fetcher.py` | 给现有两个私有 MCP 读取方法增加默认关闭的严格失败模式；普通 Campaign 行为不变 |
| `ad-direction-agent/app/persistence/erp_writer/repository.py` | 最小确定性写入、读取经营模式、回读 ERP taskId、按活动更新 pending/card 终态 |
| `ad-direction-agent/app/persistence/erp_writer/advert_exec_mapper.py` | 保持数据库 pending 为唯一构参源；补充终态结果的严格解析函数 |
| `ad-direction-agent/app/workflow/steps/advert_execution.py` | 复用预查询 portfolio、立即退出多命中取第一条、异步提交只写 IN_PROGRESS/FAIL、终态轮询与成功后入池 |
| `ad-direction-agent/app/api/campaign_viewmodel.py` | 仅在现有映射不能反映 card 终态时做最小修正；不引入新展示模型 |
| `ad-direction-agent/tests/api/test_product_identity_required.py` | 保存顺序、接口门禁、不进 LLM、前端触发回归 |
| `ad-direction-agent/tests/test_mcp_campaign_discover.py` | 严格拉数、批量 basic_info、否词/多词分类测试 |
| `ad-direction-agent/tests/persistence/test_erp_gray_cards.py` | 最小落库 SQL 范围、Decision/Pending 数量、CONFIRMED 和 latest 顺序 |
| `ad-direction-agent/tests/workflow/test_portfolio_match_and_exec.py` | portfolio 0/1/多命中、数据库回读构参、MCP 提交、IN_PROGRESS/FAIL、终态回写 |
| `ad-direction-agent/tests/test_advert_exec_child_asin.py` | 保持现有执行 payload 身份字段和子 ASIN 行为不回归 |

## 2. 关键数据口径

### 2.1 活动与关键词来源

第一阶段并行：

```python
campaign_list_task = asyncio.create_task(
    fetcher._fetch_campaign_list(
        parent_asin, parent_seller_sku, shop_account, strict=True
    )
)
keyword_task = asyncio.create_task(
    fetcher._discover_context_from_mcp(
        parent_asin, shop_account, parent_seller_sku, strict=True
    )
)
campaign_name_to_id, keyword_rows = await asyncio.gather(
    campaign_list_task, keyword_task
)
```

第二阶段在取得活动 ID 后调用：

```python
id_list = sorted(campaign_name_to_id.items())
basic_by_name = await fetcher._fetch_basic_batch_v2(id_list, shop_account)
```

`ad_campaign_basic_info_v2` 每批最多 20 个活动 ID，继续复用 `_BASIC_BATCH_SIZE = 20`。它不是第一阶段并行调用，因为其入参依赖 `campaign_id_list`。

### 2.2 否词与活动类型

否词判定沿用现有上游口径：

```python
def _is_negative_match(match_type: object) -> bool:
    return "negative" in str(match_type or "").strip().lower()
```

否词在任何计数、分类、card 或 pending 生成前丢弃。它们不能进入 `advert_exec_mapper._is_negative()`，否则会被误构造成创建否词请求。

按 `campaign_id` 聚合正向关键词。基于已确认前提：

- 不存在只有否词、没有正向投放词的关键词活动；
- 同一活动不同时包含关键词投放和商品投放；
- 不存在 `EXACT` 与 `BROAD/PHRASE` 同时存在于一个活动；`BROAD + PHRASE` 可以共存，二者都执行暂停；
- `AUTO` 活动保证在 `ad_campaign_product_keyword_list` 中返回可识别行；
- 同一商品范围内活动名称唯一；新接线在 ID 与名称同样可用时优先使用 campaign ID，现有稳定的名称关联逻辑不为本期强行改写；
- 因此活动全集中没有正向关键词行的活动，本期视为商品投放活动。

但严格 MCP 调用失败必须抛错，不能以空列表继续；只有 MCP 成功且结果确实为空时，才能把活动全集视为商品投放活动。

### 2.3 确定性动作矩阵

| 场景 | campaign_pending | keyword_pending | 卡片类别 |
|---|---|---|---|
| 活动含 `BROAD` | `new_state=paused` | 无 | `ADJUST` |
| 活动含 `PHRASE` | `new_state=paused` | 无 | `ADJUST` |
| 活动含 `AUTO` | `new_state=paused` | 无 | `ADJUST` |
| `BROAD + PHRASE` 共存 | `new_state=paused` | 无 | `ADJUST` |
| 仅 `EXACT`，一个去重后的正向关键词 | `new_budget=1.00`，目标低价组 | 一条，`new_bid=min(current_bid, 0.20)` | 迁组可执行且 Bid 有效时 `ELIMINATE`，否则 `ADJUST` |
| 仅 `EXACT`，多个正向关键词 | `new_budget=1.00`，目标低价组 | 无 | `ADJUST` |
| 商品投放活动 | `new_budget=1.00`，目标低价组 | 无 | `ADJUST` |

去重键优先使用 `keyword_id`；缺 ID 时使用标准化后的 `keyword_text + match_type`。单词 EXACT 缺少 `keyword_id` 时直接中止，不能生成无法执行的 `keyword_pending`；多词 EXACT 不读取活动级 `keyword_bid` 来生成多条关键词记录。

Amazon 活动预算下限为 `$1`，所以低价处理的目标预算固定为 `1.00`，不依赖当前预算是否缺失，也不使用 `min(current_budget, 1.00)`。

`current_bid <= 0` 不能构造 Amazon Bid；该活动仍执行预算/迁组，但不写 `keyword_pending`，卡片为 `ADJUST`。这不是预过滤，活动仍有一次确定性活动级操作。

### 2.4 低价捡漏组

仅当存在需要迁组的 EXACT 或商品投放活动时调用一次：

```python
portfolios = _normalize_portfolio_list(
    await advert_client.query_portfolio_list(
        shop_id,
        parent_asin,
        parent_seller_sku,
        portfolio_name_like="低价捡漏组",
        current_user_id=operator,
    )
)
matches = [
    row for row in portfolios
    if "低价捡漏组" in str(_pf_field(row, "portfolioName", "name", "portfolio_name") or "")
]
portfolio_id = str(_pf_field(matches[0], "portfolioId") or "") if matches else ""
```

立即退出专用口径：

- 0 命中：不迁组；预算和单词 EXACT Bid 继续执行；
- 1 命中：使用该 portfolio；
- 多命中：使用 MCP 原始返回顺序第一条。

不能修改普通 `_match_portfolio()` 的“必须唯一”规则。预查询结果通过 `submit_execution(..., resolved_portfolio_ids=...)` 传入，避免执行阶段再次查询。

## 3. Task 1：冻结入口契约和经营模式门禁

**Files:**

- Modify: `ad-direction-agent/app/api/decision.py`
- Modify: `ad-direction-agent/tests/api/test_product_identity_required.py`

- [x] **Step 1: 写失败测试**

新增以下门禁行为测试：

```python
def test_validate_immediate_exit_rejects_when_saved_mode_is_not_immediate():
    state = FakeState(long_term={"operating_mode": "控制清货"})
    with pytest.raises(HTTPException) as exc:
        decision_api._validate_immediate_exit_request(
            {
                "asin": "B0TEST",
                "_shopId": 1622,
                "_parentSellerSku": "SKU-1",
                "_shopAccount": "US-account",
                "_siteCode": "Amazon_US",
                "_userId": "operator",
                "run_id": "20260728T010203Z",
            },
            state=state,
        )
    assert "经营模式" in str(exc.value.detail)
```

再锁定：

- 缺 `asin/shopId/parentSellerSku/shopAccount/userId/run_id` 时不进入拉数；
- 已保存值必须能解析为 `OperatingMode.IMMEDIATE_EXIT`；
- `operating_mode_to_permission(OperatingMode.IMMEDIATE_EXIT)` 必须为 `AdPermission.STOP`；
- 请求中的模式字段即便写“立即退出”，数据库不是立即退出也拒绝；
- 合法请求才进入后续私有编排函数。

- [x] **Step 2: 验证测试按预期失败**

Run:

```powershell
py -m pytest tests/api/test_product_identity_required.py -q
```

Expected: FAIL，原因是 `_validate_immediate_exit_request()` 尚不存在。

- [x] **Step 3: 实现纯门禁函数**

在 `app/api/decision.py` 增加：

```python
def _validate_immediate_exit_request(req: dict, *, state) -> tuple[str, dict, dict]:
    asin = str(req.get("asin") or "").strip()
    require_product_identity_dict(req, asin=asin)
    identity = {
        "shop_id": int(req.get("_shopId") or req.get("shopId") or 0),
        "parent_seller_sku": str(
            req.get("_parentSellerSku") or req.get("parent_seller_sku") or ""
        ).strip(),
        "shop_account": str(
            req.get("_shopAccount") or req.get("shopAccount") or ""
        ).strip(),
        "site_code": str(
            req.get("_siteCode") or req.get("siteCode") or ""
        ).strip(),
        "operator": str(
            req.get("_userId") or req.get("userId") or ""
        ).strip(),
        "run_id": str(req.get("run_id") or "").strip(),
    }
    missing = [key for key, value in identity.items() if not value]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"立即退出缺少必要上下文: {','.join(missing)}",
        )

    long_term = state.get_long_term_config(asin) or {}
    try:
        mode = OperatingMode(str(long_term.get("operating_mode") or "").strip())
    except ValueError:
        raise HTTPException(
            status_code=409,
            detail="已保存经营模式无效，拒绝执行立即退出",
        )
    if mode is not OperatingMode.IMMEDIATE_EXIT:
        raise HTTPException(
            status_code=409,
            detail="已保存经营模式不是“立即退出”，拒绝执行",
        )
    if operating_mode_to_permission(mode) is not AdPermission.STOP:
        raise HTTPException(
            status_code=409,
            detail="经营模式未映射到停止权限，拒绝执行",
        )
    return asin, identity, long_term
```

本任务不注册路由、不接前端、不调用 MCP；只完成最终 API 会复用的确定性门禁，避免暴露半成品接口。

- [x] **Step 4: 运行定向测试**

Run:

```powershell
py -m pytest tests/api/test_product_identity_required.py -q
```

Expected: PASS。

- [x] **Step 5: 停止并报告模块验收**

报告门禁输入、已保存配置读取位置、权限映射和“无合法配置不拉数”的测试证据。

## 4. Task 2：严格拉取最小必要数据

**Files:**

- Modify: `ad-direction-agent/app/data/campaign_fetcher.py`
- Modify: `ad-direction-agent/app/api/decision.py`
- Modify: `ad-direction-agent/tests/test_mcp_campaign_discover.py`

- [x] **Step 1: 写失败测试**

锁定：

- `ad_campaign_list` 与 `ad_campaign_product_keyword_list` 并行；
- `ad_campaign_basic_info_v2` 在活动 ID 已取得后调用；
- 每批不超过 20；
- 不调用 perf/rank/search-term/DataAggregator/LLM；
- `strict=True` 时 MCP 失败抛出，成功空结果返回空集合；
- 任一活动缺失 basic_info 时，在写库前整体失败并列出缺失活动 ID。

- [x] **Step 2: 运行测试并确认失败**

Run:

```powershell
py -m pytest tests/test_mcp_campaign_discover.py -q
```

Expected: FAIL，原因是现有私有方法吞掉 MCP 失败并返回空列表/空字典。

- [x] **Step 3: 给现有私有方法增加严格模式**

签名改为：

```python
async def _discover_context_from_mcp(
    self,
    parent_asin: str,
    shop_account: str,
    parent_seller_sku: str = "",
    *,
    strict: bool = False,
) -> list[dict]:
```

```python
async def _fetch_campaign_list(
    self,
    parent_asin: str,
    parent_seller_sku: str,
    shop_account: str,
    *,
    strict: bool = False,
) -> dict[str, str]:
```

只有 `strict=True` 时才把 `res.ok=False`、信封解析异常和调用异常转为 `RuntimeError`。默认值为 `False`，正常 Campaign 主链现有行为保持不变。

- [x] **Step 4: 在立即退出编排中按依赖顺序调用**

`app/api/decision.py` 的 `_fetch_immediate_exit_inputs()`：

```python
async def _fetch_immediate_exit_inputs(*, asin: str, identity: dict) -> dict:
    fetcher = CampaignFetcher()
    campaign_task = asyncio.create_task(
        fetcher._fetch_campaign_list(
            asin,
            identity["parent_seller_sku"],
            identity["shop_account"],
            strict=True,
        )
    )
    keyword_task = asyncio.create_task(
        fetcher._discover_context_from_mcp(
            asin,
            identity["shop_account"],
            identity["parent_seller_sku"],
            strict=True,
        )
    )
    campaign_name_to_id, keyword_rows = await asyncio.gather(
        campaign_task, keyword_task
    )
    if not campaign_name_to_id:
        raise RuntimeError("未获取到任何广告活动，立即退出未执行")

    id_list = sorted(campaign_name_to_id.items())
    basic_by_name = await fetcher._fetch_basic_batch_v2(
        id_list, identity["shop_account"]
    )
    missing = [
        {"campaign_name": name, "campaign_id": campaign_id}
        for name, campaign_id in id_list
        if name not in basic_by_name
    ]
    if missing:
        raise RuntimeError(f"basic_info_v2 缺少活动: {missing}")
    return {
        "campaign_name_to_id": campaign_name_to_id,
        "keyword_rows": keyword_rows,
        "basic_by_name": basic_by_name,
    }
```

- [x] **Step 5: 运行定向测试**

Run:

```powershell
py -m pytest tests/test_mcp_campaign_discover.py -q
```

Expected: PASS。

- [x] **Step 6: 停止并报告模块验收**

报告实际只调用的三个数据工具、并发关系、批量上限和失败时尚未发生任何写库/MCP 执行。

## 5. Task 3：否词、多词和商品投放的确定性归类

**Files:**

- Modify: `ad-direction-agent/app/api/decision.py`
- Modify: `ad-direction-agent/tests/test_mcp_campaign_discover.py`

- [x] **Step 1: 写动作矩阵失败测试**

构造包含以下活动的输入：

- 单词 EXACT，另带一条 `NEGATIVE_EXACT`；
- 两个不同 keyword_id 的多词 EXACT；
- BROAD；
- PHRASE；
- AUTO；
- BROAD + PHRASE 共存活动；
- 活动全集中存在、关键词行中不存在的商品投放活动；
- `campaign_budget` 缺失但仍应固定生成 `$1.00` 目标预算；
- `current_bid=0` 的单词 EXACT。

断言归类结果：

```python
assert single_exact["action_kind"] == "LOW_BID_SINGLE_EXACT"
assert single_exact["new_budget"] == Decimal("1.00")
assert single_exact["new_bid"] == Decimal("0.20")
assert multi_exact["action_kind"] == "LOW_BID_MULTI_EXACT"
assert multi_exact["new_budget"] == Decimal("1.00")
assert multi_exact["new_bid"] is None
assert phrase["action_kind"] == "PAUSE"
assert broad_phrase["action_kind"] == "PAUSE"
assert product_target["action_kind"] == "LOW_BID_PRODUCT_TARGET"
assert product_target["new_budget"] == Decimal("1.00")
assert all(
    "NEGATIVE" not in str(row.get("match_type") or "").upper()
    for action in actions
    for row in action["positive_keywords"]
)
```

- [x] **Step 2: 运行并确认失败**

Run:

```powershell
py -m pytest tests/test_mcp_campaign_discover.py -q
```

Expected: FAIL，原因是 `_classify_immediate_exit_actions()` 尚不存在。

- [x] **Step 3: 实现 `_classify_immediate_exit_actions()`**

函数只消费 Task 2 的三个返回值，输出每活动一条内存动作规格：

```python
{
    "campaign_id": "123",
    "campaign_name": "campaign-a",
    "action_kind": "PAUSE"
        | "LOW_BID_SINGLE_EXACT"
        | "LOW_BID_MULTI_EXACT"
        | "LOW_BID_PRODUCT_TARGET",
    "current_state": "enabled",
    "new_state": "paused" | None,
    "current_budget": Decimal("8.00"),
    "new_budget": Decimal("1.00") | None,
    "current_bid": Decimal("0.50") | None,
    "new_bid": Decimal("0.20") | None,
    "positive_keywords": [...],
}
```

分类阶段不创建任何数据库模型，不查询 portfolio，不写库。否词在该函数入口立即丢弃；匹配类型优先级为 `PAUSE > LOW_BID_SINGLE_EXACT/LOW_BID_MULTI_EXACT > LOW_BID_PRODUCT_TARGET`。

动作对应的卡片描述在 Task 4 构建 Run 时生成：

```text
立即退出：活动已设为暂停
立即退出：单词精准活动降预算、降 Bid 并迁入低价捡漏组
立即退出：多词精准活动本期仅降预算并迁组，不调整关键词 Bid
立即退出：商品投放活动本期仅降预算并迁组，不调整 Target Bid
立即退出：未找到低价捡漏组，本次仅执行预算/Bid 调整
```

- [x] **Step 4: 运行定向测试**

Run:

```powershell
py -m pytest tests/test_mcp_campaign_discover.py -q
```

Expected: PASS。

- [x] **Step 5: 停止并报告模块验收**

逐项报告单词、多词、BROAD+PHRASE 共存、否词和商品投放输出了哪些 card/pending。

## 6. Task 4：查询低价组并纳入确定性 Run

**Files:**

- Modify: `ad-direction-agent/app/api/decision.py`
- Modify: `ad-direction-agent/tests/workflow/test_portfolio_match_and_exec.py`

- [x] **Step 1: 写 0/1/多命中失败测试**

断言：

- 无需迁组时不调用 `query_portfolio_list`；
- 0 命中时返回 `{"match_count": 0}`，预算/Bid pending 保留；
- 1 命中时在 `low_bid_portfolio` 元数据中记录 `portfolio_id`；
- 多命中时取 MCP 原始顺序第一条；
- 不调用普通 `_match_portfolio()` 修改其唯一命中语义；
- 查询仅发生一次。

- [x] **Step 2: 运行并确认失败**

Run:

```powershell
py -m pytest tests/workflow/test_portfolio_match_and_exec.py -q
```

Expected: FAIL，原因是立即退出专用组合解析尚不存在。

- [x] **Step 3: 实现供 `_run_immediate_exit()` 单次调用的查询函数**

新增私有函数：

```python
async def _resolve_immediate_exit_low_bid_portfolio(
    *, identity: dict, asin: str, required: bool
) -> dict:
    if not required:
        return {}
    client = AdvertMcpClient()
    try:
        raw = await client.query_portfolio_list(
            identity["shop_id"],
            asin,
            identity["parent_seller_sku"],
            portfolio_name_like="低价捡漏组",
            current_user_id=identity["operator"],
        )
        portfolios = _normalize_portfolio_list(raw)
        matches = [
            row for row in portfolios
            if "低价捡漏组" in str(
                _pf_field(row, "portfolioName", "name", "portfolio_name") or ""
            )
        ]
        if not matches:
            return {"match_count": 0}
        selected = matches[0]
        portfolio_id = str(_pf_field(selected, "portfolioId") or "").strip()
        return {
            "match_count": len(matches),
            "portfolio_id": portfolio_id,
            "portfolio_name": str(_pf_field(selected, "portfolioName", "name") or ""),
        }
    finally:
        await client.aclose()
```

- [x] **Step 4: 根据动作规格与组合结果构建 CanonicalRun**

在 `app/api/decision.py` 实现 `_build_immediate_exit_run()`。函数输入为 Task 3 的 `actions`、已保存战略配置、包含 run_id 的 identity，以及 `low_bid_portfolio` 元数据。生成：

- `decision_id = stable_id("dec", asin, run_id, 1)`；
- `batch_no` 由 run_id 派生，保持与现有批次展示格式一致；
- 每个活动一张 `SuggestCardCanonical`；
- 每张卡最多一条 `CampaignPendingCanonical`；
- 仅 `LOW_BID_SINGLE_EXACT` 且 Bid 有效时一条 `KeywordPendingCanonical`；
- 无 Placement、无否词、无 Target pending；
- `summary` 只写 total/eliminate/adjust/keep 及 validation；
- `decision_meta` 只带当前已保存战略配置和站点，不生成 AI 字段；
- 只有单词 EXACT、有效 Bid 且真实解析到低价组时为 `ELIMINATE`，其他均为 `ADJUST`。

- [x] **Step 5: 运行定向测试**

Run:

```powershell
py -m pytest tests/test_mcp_campaign_discover.py tests/workflow/test_portfolio_match_and_exec.py -q
```

Expected: PASS。

- [x] **Step 6: 停止并报告模块验收**

报告 0/1/多命中下迁组与预算/Bid 的独立行为，并确认无二次查询。

## 7. Task 5：最小落库、置 latest、结束事件、自动确认

**Files:**

- Modify: `ad-direction-agent/app/persistence/erp_writer/repository.py`
- Modify: `ad-direction-agent/app/api/decision.py`
- Modify: `ad-direction-agent/tests/persistence/test_erp_gray_cards.py`
- Modify: `ad-direction-agent/tests/api/test_product_identity_required.py`

- [x] **Step 1: 写最小写库失败测试**

断言 `write_immediate_exit()` 只调用：

```text
_upsert_decision
_upsert_decision_config
_upsert_modern_summary
_delete_modern_campaign_children
_upsert_modern_cards_and_pending
```

不调用：

```text
_upsert_legacy_metrics
_upsert_synthesis
_upsert_purpose_scores
_upsert_core_keywords
_upsert_ai_suggest
_upsert_wizard_direction
```

`_upsert_modern_summary` 是现有 `campaign_viewmodel.from_db_snapshot()` 正确展示 total/eliminate/adjust 所需，不是 AI 冗余数据。

- [x] **Step 2: 运行并确认失败**

Run:

```powershell
py -m pytest tests/persistence/test_erp_gray_cards.py -q
```

Expected: FAIL，原因是 Repository 尚无专用最小写入方法。

- [x] **Step 3: 实现 `write_immediate_exit()`**

```python
def write_immediate_exit(
    self,
    run: CanonicalRun,
    *,
    operator: str,
) -> WriteReport:
    decision_meta = run.decision_meta or {}
    report = WriteReport(decision_id=run.decision_id)
    conn = self._connect()
    now = datetime.now()
    try:
        with conn.cursor() as cur:
            self._upsert_decision(
                cur, run, decision_meta, now, operator=operator
            )
            report.decision = 1
            self._upsert_decision_config(cur, run, decision_meta, now)
            report.decision_config = 1
            self._upsert_modern_summary(
                cur, run, now, operator=operator
            )
            report.modern_summary = 1
            self._delete_modern_campaign_children(cur, run.decision_id)
            (
                report.modern_card,
                report.modern_keyword_pending,
                report.modern_campaign_pending,
                report.modern_placement_pending,
            ) = self._upsert_modern_cards_and_pending(
                cur, run, now, operator=operator
            )
        conn.commit()
        return report
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
```

同时修正 `get_decision_preset()` 的 SELECT，加入 `operating_mode`，使确定性决策的战略层快照可回显。

- [x] **Step 4: 写并验证严格调用顺序**

API 编排顺序必须是：

```python
report = await asyncio.to_thread(
    repo.write_immediate_exit,
    run,
    operator=identity["operator"],
)
await asyncio.to_thread(
    repo.finalize_batch, report.decision_id, asin, "REALTIME"
)
cleared = await asyncio.to_thread(state.clear_analysis_session, asin)
if not cleared:
    raise RuntimeError("立即退出批次已落库，但分析事件清除失败")
confirm_items = [
    {"card_id": card.card_id, "decision": "approve"}
    for card in run.cards
]
confirmed = await asyncio.to_thread(
    repo.confirm_decisions,
    report.decision_id,
    confirm_items,
    identity["operator"],
    in_progress=False,
)
if not confirmed.get("ok") or confirmed.get("applied") != len(run.cards):
    raise RuntimeError(f"立即退出自动确认失败: {confirmed}")
```

测试使用调用记录列表，精确断言：

```python
assert calls == ["write", "finalize", "clear_session", "confirm"]
```

写库失败或 `finalize_batch` 失败时不得清除分析事件；`clear_analysis_session()` 必须对删除失败返回 `False`，事件未清时不得自动确认；确认失败时不得调用执行 MCP。立即退出写库必须把 operator 作为本次调用的局部参数传给既有 helper，不能依赖 Repository 单例上的临时字段。

- [x] **Step 5: 实现供 `_run_immediate_exit()` 拉数前调用的同 run_id 幂等恢复**

`decision_id` 由 `asin + run_id + run_number` 稳定派生。`_run_immediate_exit()` 在任何拉数和写库前先调用 `repo.get_decision_basic(decision_id)`：

- 不存在：执行完整链路；
- 已存在且 ASIN/店铺/SKU 与请求一致：禁止再次调用 `write_immediate_exit()`，避免 `_delete_modern_campaign_children()` 把已经 CONFIRMED/IN_PROGRESS 的行重建为 PENDING；
- 已存在且身份不一致：返回冲突，不执行；
- 已确认且 pending 仍为 PENDING：允许继续进入 `submit_execution()`；
- 已为 IN_PROGRESS/SUCCESS/FAIL：直接返回当前数据库状态，不重复调用广告执行 MCP。

首次持久化还必须使用同一个稳定 `decision_id` 获取 MySQL advisory lock。锁内二次调用 `get_decision_basic()`；只有仍不存在时，才依次执行 `write_immediate_exit → finalize_batch → clear_analysis_session → confirm_decisions`。锁覆盖完整首次写库生命周期，结束后显式 `RELEASE_LOCK`，避免两个并发请求同时通过锁外预检查并重复重建 pending。

新增测试模拟同一 `run_id` 并发请求，断言只有一个请求真实写库，另一个请求在获得锁后按已存在决策恢复。后续 Task 7 还需模拟“第一次 MCP 已提交但 HTTP 响应丢失，前端以相同 run_id 重试”，断言：

```python
repo.write_immediate_exit.assert_not_called()
advert_client.async_batch_update.assert_not_called()
```

- [x] **Step 6: 运行定向测试**

Run:

```powershell
py -m pytest tests/persistence/test_erp_gray_cards.py tests/api/test_product_identity_required.py -q
```

Expected: PASS。

- [x] **Step 7: 停止并报告模块验收**

报告实际落了哪些表、每类活动生成多少 pending，以及 latest/结束事件/CONFIRMED 的真实顺序。

## 8. Task 6：只从 CONFIRMED Pending 构造执行参数

> **2026-07-28 Task 5 停点复审：** Task 1–5 所需数据契约已确认：`AUTO` 活动保证返回可识别行；同一商品范围内活动名称唯一，但新接线在同等条件下优先使用 campaign ID；同 `run_id` 重试允许重新查询低价组，不持久化 portfolioId；低价处理的活动目标预算固定为 `$1.00`。Task 6 尚未开始，当前不得注册路由或接广告执行 MCP。

**Files:**

- Modify: `ad-direction-agent/app/persistence/erp_writer/advert_exec_mapper.py`
- Modify: `ad-direction-agent/tests/workflow/test_portfolio_match_and_exec.py`
- Modify: `ad-direction-agent/tests/test_advert_exec_child_asin.py`

- [x] **Step 1: 写数据库回读构参失败测试**

测试输入必须模拟 `repository.load_confirmed_pending()` 的真实字典，不复用 Task 3 的内存动作对象。

断言：

- BROAD、PHRASE、AUTO 各自输出 `campaignState="paused"`；
- 单词 EXACT 输出一个 `keywordShowVoList`；
- 多词 EXACT 不输出 `keywordShowVoList`；
- 商品投放不输出 `targetShowVoList`；
- 否词 pending 不存在；
- `shopId/parentAsin/parentSellerSku/currentUserId/decisionId/agentVersion` 正确；
- 活动预算固定写为 `$1.00`，关键词 Bid 使用 `min(current_bid, 0.20)`，不会因进入低价组而提高；
- 同一活动只出现一个 `campaignVo`。

- [x] **Step 2: 运行并确认失败或现状差异**

Run:

```powershell
py -m pytest tests/workflow/test_portfolio_match_and_exec.py tests/test_advert_exec_child_asin.py -q
```

Expected: 新增用例在现有 mapper 上暴露差异；既有用例保持通过。

- [x] **Step 3: 对 mapper 做最小修正**

继续由 `build_exec_plan()` 聚合，不新增第二套构参器。只修复新增测试证明的差异：

```python
vo = {
    "campaignId": campaign_id,
    "campaignBudget": new_budget,      # 有值才写
    "campaignState": "paused",         # 有值才写
    "keywordShowVoList": [...],        # 仅 keyword_pending 存在才写
}
```

`campaignGroupType` 仍是内部代码，调用广告 MCP 前由 portfolio 解析移除并在 `paramsVo` 顶层写 `portfolioId`。

> **Task 6 实施结果：** 现有 `build_exec_plan()` 已满足立即退出的数据库回读构参合同，无需修改生产 mapper；本 Task 仅补充真实 `load_confirmed_pending()` 字典形状的回归测试。

- [x] **Step 4: 运行定向测试**

Run:

```powershell
py -m pytest tests/workflow/test_portfolio_match_and_exec.py tests/test_advert_exec_child_asin.py -q
```

Expected: PASS。

- [x] **Step 5: 停止并报告模块验收**

给出四类活动的最终 `campaignVo` 示例，确认所有操作字段来自数据库 pending。

## 9. Task 7：执行 MCP，只写 IN_PROGRESS/FAIL

**Files:**

- Modify: `ad-direction-agent/app/workflow/steps/advert_execution.py`
- Modify: `ad-direction-agent/app/api/decision.py`
- Modify: `ad-direction-agent/tests/workflow/test_portfolio_match_and_exec.py`

- [x] **Step 1: 写异步提交状态失败测试**

锁定：

- 立即退出调用 `submit_execution()`，不调用 `submit_execution_direct()`；
- `submit_execution()` 第一件事是 `load_confirmed_pending(decision_id)`；
- 传入 `resolved_portfolio_ids={"low_bid_retention_group": "pf-1"}` 时不再查 portfolio MCP；
- 传入空字典时不迁组，但预算/Bid/暂停继续下发；
- MCP 成功且返回 taskId → 本批 ops 为 `IN_PROGRESS`；
- MCP 信封失败、抛异常或成功但无 taskId → 本批 ops 为 `FAIL`；
- 提交阶段不写 `SUCCESS`、不调用 `upsert_pool_entry()`。

- [x] **Step 2: 运行并确认失败**

Run:

```powershell
py -m pytest tests/workflow/test_portfolio_match_and_exec.py -q
```

Expected: FAIL。现有代码会在提交后调用 `_sync_pool_entries_from_exec()`，该函数会过早写池并把 pending 改为 `SUCCESS`。

- [x] **Step 3: 扩展执行函数的专用入参**

```python
async def submit_execution(
    decision_id: str,
    *,
    operator: str,
    resolved_portfolio_ids: dict[str, str] | None = None,
    wait_for_terminal: bool = False,
) -> dict:
```

语义：

- `resolved_portfolio_ids is None`：普通路径，沿用现有实时查询；
- `{}`：已查过但 0 命中，不再查询，移除迁组字段并继续其他调整；
- 非空字典：按 group code 注入对应 portfolioId；
- `wait_for_terminal=True`：本次异步提交由终态查询负责 SUCCESS/入池。

修改 `_resolve_modify_portfolios()` 接受该映射，且不改变普通路径的唯一匹配规则。

- [x] **Step 4: 修正提交状态**

```python
ok, msg = parse_result_envelope(res)
task_ids = extract_task_ids(res)
status = "IN_PROGRESS" if ok and task_ids else "FAIL"
for op in async_ops:
    op["execute_status"] = status
    op["modify_result"] = "PENDING" if status == "IN_PROGRESS" else "FAIL"
    if status == "FAIL":
        op["error_msg"] = msg or "异步提交未返回 taskId"
repo.update_pending_execute_status(plan.ops, status, operator=operator)
```

`wait_for_terminal=True` 时不得调用 `_sync_pool_entries_from_exec()`。普通路径是否同步修正为终态后入池，另开兼容性任务，不在本期扩大。

- [x] **Step 5: 注册 API 并在完整编排中调用**

在 `app/api/decision.py` 中注册入口；此时 Task 1–7 的所有下游依赖均已存在：

```python
@router.post("/decision/immediate-exit")
async def immediate_exit(req: dict):
    state = get_state_manager()
    asin, identity, long_term = _validate_immediate_exit_request(
        req, state=state
    )
    return await _run_immediate_exit(
        asin=asin,
        identity=identity,
        long_term=long_term,
        state=state,
    )
```

`_run_immediate_exit()` 严格按以下顺序调用已实现函数：

```text
stable_id 派生 decision_id
→ get_decision_basic 检查是否为同一 run_id 的恢复请求
→ 若已存在则按 Task 5 幂等规则恢复或直接返回
→ 若不存在才调用 _fetch_immediate_exit_inputs
→ _classify_immediate_exit_actions
→ _resolve_immediate_exit_low_bid_portfolio
→ _build_immediate_exit_run
→ write_immediate_exit
→ finalize_batch
→ clear_analysis_session
→ confirm_decisions
→ submit_execution
```

```python
execution = await submit_execution(
    run.decision_id,
    operator=identity["operator"],
    resolved_portfolio_ids=resolved_portfolio_ids,
    wait_for_terminal=True,
)
if not execution.get("ok"):
    return {
        "ok": False,
        "decision_id": run.decision_id,
        "error": execution.get("error") or "广告执行提交失败",
    }
return {
    "ok": True,
    "decision_id": run.decision_id,
    "task_ids": execution.get("task_ids") or [],
    "execute_status": "IN_PROGRESS",
    "move_errors": execution.get("move_errors") or [],
}
```

- [x] **Step 6: 运行定向测试**

Run:

```powershell
py -m pytest tests/workflow/test_portfolio_match_and_exec.py -q
```

Expected: PASS。

- [x] **Step 7: 停止并报告模块验收**

报告真实 MCP payload、taskId、pending/card 当前状态，明确此时尚未声称 Amazon 执行成功。

## 10. Task 8：按 taskId 查询终态并回写 SUCCESS/FAIL

**Files:**

- Modify: `ad-direction-agent/app/persistence/erp_writer/repository.py`
- Modify: `ad-direction-agent/app/persistence/erp_writer/advert_exec_mapper.py`
- Modify: `ad-direction-agent/app/workflow/steps/advert_execution.py`
- Modify: `ad-direction-agent/app/api/decision.py`
- Modify: `ad-direction-agent/tests/workflow/test_portfolio_match_and_exec.py`

- [x] **Step 1: 写 ERP taskId 回读失败测试**

执行记录表 14–18 由 ERP 执行侧写，Agent 不改写。新增只读方法：

```python
def list_execution_task_ids(self, decision_id: str) -> list[str]:
    cur.execute(
        "SELECT task_id FROM t_advert_agent_modify_advert_record "
        "WHERE decision_id=%s AND task_id IS NOT NULL AND task_id<>'' "
        "ORDER BY create_time",
        (decision_id,),
    )
```

测试断言去重、保持创建顺序，并确认没有 INSERT/UPDATE `*_record`。

- [x] **Step 2: 写终态解析失败测试**

以现有真实脚本验证过的结构为固定契约：

```json
[
  {
    "result": {
      "detailVoList": [
        {
          "campaignResList": [
            {
              "campaignId": "123",
              "updateCampaignState": "success"
            }
          ]
        }
      ]
    }
  }
]
```

实现并测试：

```python
def parse_batch_update_terminal(res: object) -> dict[str, str]:
    # 返回 campaign_id -> SUCCESS/FAIL/IN_PROGRESS
```

映射：

- `updateCampaignState == "success"` → `SUCCESS`；
- 明确失败状态或错误消息 → `FAIL`；
- task/result/detail/campaign 缺失、`unknown`、`submitted`、查询返回“未找到相关记录” → 保持 `IN_PROGRESS`，不能猜成失败或成功。

由于当前已知回包以活动为聚合单位，同一 campaign 下的 campaign_pending 与 keyword_pending 使用相同终态。若后续接口提供关键词级结果，再单独扩展，不能预先猜测字段。

- [x] **Step 3: 实现按活动更新 pending 与 card**

Repository 增加：

```python
def update_campaign_terminal_status(
    self,
    decision_id: str,
    campaign_statuses: dict[str, str],
    *,
    operator: str,
    message_by_campaign: dict[str, str] | None = None,
) -> None:
```

对三张现有 pending 中属于该 `decision_id + campaign_id` 的行更新状态；当前立即退出不会生成 placement_pending，但保持统一方法。然后按 card 聚合：

```text
任一 pending=FAIL       → card.execute_status=FAIL
任一 pending=IN_PROGRESS → card.execute_status=IN_PROGRESS
全部 pending=SUCCESS    → card.execute_status=SUCCESS
否则                    → card.execute_status=PENDING
```

- [x] **Step 4: 实现查询编排**

`app/workflow/steps/advert_execution.py` 新增：

```python
async def poll_execution_result(
    decision_id: str,
    *,
    operator: str,
) -> dict:
    repo = _get_repository()
    task_ids = repo.list_execution_task_ids(decision_id)
    if not task_ids:
        return {
            "ok": True,
            "decision_id": decision_id,
            "execute_status": "IN_PROGRESS",
            "task_ids": [],
        }
    client = AdvertMcpClient()
    try:
        raw = await client.batch_update_result(task_ids)
    finally:
        await client.aclose()
    statuses = parse_batch_update_terminal(raw)
    repo.update_campaign_terminal_status(
        decision_id, statuses, operator=operator
    )
    return {
        "ok": True,
        "decision_id": decision_id,
        "execute_status": aggregate_status(statuses),
        "task_ids": task_ids,
        "campaign_statuses": statuses,
    }
```

- [x] **Step 5: 终态成功后才写低价池**

从当前提交成功钩子中移除立即退出的入池动作。`poll_execution_result()` 完成 DB 终态回写后：

- 只对 card `suggest_category=ELIMINATE`；
- 且其 campaign 最终为 `SUCCESS`；
- 才调用现有 `upsert_pool_entry()`；
- 多词 EXACT、商品投放、无低价组的单词 EXACT 都是 `ADJUST`，不会错误入池。

- [x] **Step 6: 暴露状态查询 API**

在 `app/api/decision.py` 增加：

```python
@router.post("/decision/immediate-exit/status")
async def immediate_exit_status(req: dict):
    decision_id = str(req.get("decision_id") or "").strip()
    operator = str(req.get("_userId") or req.get("userId") or "").strip()
    if not decision_id:
        return {"ok": False, "error": "decision_id 必填"}
    return await poll_execution_result(decision_id, operator=operator or "system")
```

- [x] **Step 7: 运行定向测试**

Run:

```powershell
py -m pytest tests/workflow/test_portfolio_match_and_exec.py -q
```

Expected: PASS，包括：

- taskId 尚未出现在 ERP record → 保持 IN_PROGRESS；
- 部分活动成功、部分失败 → 分活动回写；
- “未找到相关记录” → 保持 IN_PROGRESS；
- 全成功 → pending/card SUCCESS，ELIMINATE 卡入池；
- 明确失败 → pending/card FAIL，不入池。

- [x] **Step 8: 停止并报告模块验收**

报告 taskId 权威来源、终态回包原文结构、每个 campaign 的结果和数据库回写结果。

## 11. Task 9：前端接线与状态渲染

**Files:**

- Modify: `ad-direction-agent/demo/ad-asisitant-agent.html`
- Modify: `ad-direction-agent/app/api/campaign_viewmodel.py`
- Modify: `ad-direction-agent/tests/api/test_product_identity_required.py`

- [x] **Step 1: 写保存顺序失败测试**

从 `confirmStrategy()` 源码中精确断言：

```text
showImmediateExitConfirm
  < callAPI('/strategy/confirm')
  < callAPI('/decision/immediate-exit')
  < refreshDecisionContext/loadAll
```

并断言立即退出分支不再调用：

```javascript
await onCancelEventClick({ skipConfirm: true });
await loadTactics();
```

- [x] **Step 2: 运行并确认失败**

Run:

```powershell
py -m pytest tests/api/test_product_identity_required.py -q
```

Expected: FAIL。当前实现保存后直接 `cancel-event`，没有确定性执行。

- [x] **Step 3: 替换立即退出保存分支**

```javascript
if (om === '立即退出') {
  const runId = ((_decisionContext || {}).in_progress || '').trim();
  const exitResult = await callAPI('/decision/immediate-exit', {
    asin,
    run_id: runId,
  });
  if (!exitResult.ok) {
    throw new Error(exitResult.error || '立即退出执行失败');
  }
  showToast(
    exitResult.execute_status === 'IN_PROGRESS'
      ? '立即退出操作已提交，正在等待广告平台返回结果'
      : '立即退出操作已完成',
    'success',
    5000,
  );
  await refreshDecisionContext();
  await loadAll(true);
  startImmediateExitStatusPolling(exitResult.decision_id);
  return;
}
```

`callAPI()` 已统一注入 shopId、SKU、shopAccount、siteCode、userId，不重复手工拼接。

- [x] **Step 4: 实现有界状态查询**

每 5 秒查询一次，最多 12 次：

```javascript
async function startImmediateExitStatusPolling(decisionId) {
  for (let attempt = 0; attempt < 12; attempt += 1) {
    await new Promise(resolve => setTimeout(resolve, 5000));
    const result = await callAPI('/decision/immediate-exit/status', {
      decision_id: decisionId,
    });
    if (!result.ok) return;
    await window._mountCampaignBatch({
      decision_id: decisionId,
      executable: false,
    });
    if (result.execute_status === 'SUCCESS' ||
        result.execute_status === 'FAIL') {
      return;
    }
  }
}
```

超过 60 秒仍无终态时停止轮询，页面保留 `IN_PROGRESS`；不把超时改成失败。用户刷新页面后，批次快照继续显示数据库状态。

- [x] **Step 5: 校验 ViewModel**

`from_db_snapshot()` 已读取 card 的 `confirm_status/execute_status`。只有测试证明现有前端未刷新该字段时，才做最小修正；不新增第二套立即退出 ViewModel。

- [x] **Step 6: 运行定向测试**

Run:

```powershell
py -m pytest tests/api/test_product_identity_required.py -q
```

Expected: PASS。

- [x] **Step 7: 停止并报告模块验收**

报告浏览器真实顺序、接口响应、批次 ID、卡片/Pending 状态和未触发的 LLM 请求。

## 12. Task 10：端到端回归和人工验收

**Files:**

- Verify only unless测试暴露本计划范围内缺陷。

- [ ] **Step 1: 运行完整定向测试**

Run:

```powershell
py -m pytest `
  tests/api/test_product_identity_required.py `
  tests/test_mcp_campaign_discover.py `
  tests/persistence/test_erp_gray_cards.py `
  tests/workflow/test_portfolio_match_and_exec.py `
  tests/test_advert_exec_child_asin.py `
  -q
```

Expected: 全部 PASS。

- [ ] **Step 2: 编译检查**

Run:

```powershell
py -m compileall -q app
```

Expected: exit code 0。

- [ ] **Step 3: 差异检查**

Run:

```powershell
git diff --check
git status --short
```

Expected:

- `git diff --check` 无新增格式错误；
- 只出现计划中列出的实现文件和既有用户改动；
- 没有 `target_pending` 表、迁移脚本或新业务源码文件。

- [ ] **Step 4: 浏览器人工验收**

使用测试 ASIN，广告执行开关先保持 dry-run：

1. 新建分析事件；
2. 选择“立即退出”；
3. 确认高危弹窗；
4. 点击保存；
5. 检查 `/strategy/confirm` 成功后才调用 `/decision/immediate-exit`；
6. 检查 Network 中没有 `/tactics/recommend`、`/campaign/viewmodel` 分析请求或 LLM 请求；
7. 检查新 decision 为 latest、分析事件消失；
8. 检查卡片和 pending 已自动 CONFIRMED；
9. 检查 dry-run payload：
   - BROAD/PHRASE/AUTO 为 paused；
   - 单词 EXACT 有预算与一个 keyword Bid；
   - 多词 EXACT 无 keywordShowVoList；
   - 商品投放无 targetShowVoList；
10. 再在明确授权的测试店铺关闭 dry-run，验证 taskId、IN_PROGRESS 和终态回写。

- [ ] **Step 5: 最终链路验收报告**

报告必须逐节点给证据：

```text
战略配置保存结果
→ 数据 MCP 调用与返回行数
→ 否词/单词/多词/商品投放分类数
→ decision/card/pending 行数
→ latest/analysis_session/CONFIRMED 状态
→ DB 回读后的执行 payload
→ async taskId
→ IN_PROGRESS/FAIL 提交状态
→ batch result 原始结构
→ SUCCESS/FAIL 终态
→ 低价池是否按终态写入
```

## 13. 实施时必须特别关注的现有缺口

### 13.1 提交成功不等于执行成功

当前 `advert_execution.py` 在 async 提交后会调用 `_sync_pool_entries_from_exec()`；该函数还会把 pending 写成 `SUCCESS`。对本链路这是错误的：taskId 只证明已提交。立即退出必须通过 `wait_for_terminal=True` 禁止该行为，直到查询到 `updateCampaignState=success`。

### 13.2 taskId 不由 Agent 写执行记录

`repository.insert_advert_record()`、`update_advert_record_result()`、`insert_exec_sub_records()` 当前是 no-op，因为 `t_advert_agent_modify_*_record` 由 ERP 执行侧写。立即退出不得重新取得这些表的写权限。

`agent_async_batch_update_advert` 的 `paramsVo` 已包含 `decisionId`。终态查询通过 ERP 执行记录表按 `decision_id` 只读回 taskId，再调用 `agent_batch_update_advert_result`。

### 13.3 结果查询可能过期

现有执行脚本记录了：异步结果可能被清理，返回“未找到相关记录”，但 Amazon 实际可能已执行。因此该结果只能保持 `IN_PROGRESS` 并提示待人工核验，不能擅自写 `FAIL` 或 `SUCCESS`。

### 13.4 商品投放仍不是完整低价闭环

本期没有 `target_pending`，所以商品投放活动只降活动预算并尝试迁组。未来若要求 Target Bid 真实下调，必须单独获得数据库改造授权，新增 Target 级 pending、确认、构参和回写；不能在本计划中偷偷扩展。

## 14. 最终验收标准

只有同时满足以下条件，才能宣称本链路完成：

- 前端先保存战略层，再调用立即退出接口；
- 后端校验的是已保存经营模式及其权限映射；
- 没有任何 LLM、Campaign 护栏或无用 MCP 被调用；
- 活动全集没有因普通预过滤、多词规则或否词而漏执行；
- 多词 EXACT 没有错误复制活动级 Bid；
- 所有执行字段在 MCP 下发前均从已 CONFIRMED 的数据库 pending 回读；
- 0 个低价组不阻塞预算/Bid，多命中使用第一条；
- 提交阶段只有 `IN_PROGRESS/FAIL`；
- SUCCESS 只能来自 `agent_batch_update_advert_result`；
- 低价池只在真实终态成功后写入；
- 前端能渲染 decision、战略层快照、CONFIRMED 和 execute_status；
- 没有新增 `target_pending`、没有把 Target 操作伪装成 Keyword 操作；
- 每个 Task 均有先失败、后通过的定向测试和一次独立验收报告。
