# Agent 配置保存镜像写入 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `subagent-driven-development` (recommended) or `executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变既有保存、State、旧 ERP 表或前端行为的前提下，让五类现有配置保存成功后按字段镜像写入 ERP 新表 `t_advet_agent_config`。

**Architecture:** API 端点仍先调用原有 `WorkflowOrchestrator` 或 P3 保存方法；旧链路成功后，API 层调用一个共享镜像 helper，以 `asyncio.to_thread()` 执行 ERP repository 的单次字段级 upsert。新表是未来批跑配置入口，本期只写不读；镜像失败只写后端 error 日志，旧响应体、HTTP 状态和前端行为完全不变。

**Tech Stack:** FastAPI、Pydantic v2、Python asyncio、PyMySQL、MySQL upsert、pytest。

---

## 已确认边界

- 新增且唯一目标表：`t_advet_agent_config`；表名按现有 DDL 保留 `advet` 拼写，不改名。
- 唯一键固定为 `(parent_asin, parent_seller_sku, shop_id)`；首次任一保存插入一行，后续保存只更新本次涉及列。
- 不新增前端 API 调用、不改按钮时序、不改 `demo/ad-asisitant-agent.html`。
- 不在响应 body 增加 `config_mirror_saved`，不增加 response header；镜像结果不回传前端。
- 不读取新表、不改 `cfg_source=config`、不改批跑、`apply_strategy_preset.py`、Wizard 实验脚本或 `t_advert_agent_decision_config`。
- 不新增 MCP、LLM、decisionId 或分析事件。
- `enabled`、`frequency` 只使用数据库首次插入默认值；普通保存绝不覆盖它们。
- 新表仍未上线；代码部署前必须先在目标 ERP 库核验完整 DDL 和唯一索引已存在。

## 保存动作与镜像字段

| 既有端点 | 成功后的镜像 patch | 新表列 |
|---|---|---|
| `POST /strategy/confirm` | `product_level`、`operating_mode`、`season_stage` | `product_position`、`operating_mode`、`season_type` |
| `POST /tactics/confirm` | `ad_purposes`、`target_keyword_strategy` | `advert_purposes`、`target_keyword_types` |
| `POST /execution/target-acos/override` | `value` | `target_acos_suggest` |
| `POST /execution/budget-bid/override` | `value` | `daily_budget_suggest` |
| `POST /execution/select` | `selected_directions` | `advert_direction_types` |
| `DELETE /execution/target-acos/override` | 显式 `None` | `target_acos_suggest = NULL` |
| `DELETE /execution/budget-bid/override` | 显式 `None` | `daily_budget_suggest = NULL` |

业务值到 ERP code 的映射**只由** `app/persistence/erp_writer/repository.py::upsert_agent_config()` 完成；API 仅传请求模型的原始业务值，不能在端点重复调用 mapper。repository 必须复用 `app/persistence/erp_writer/text_utils.py` 的既有 mapper：

```python
product_position = map_product_position(req.product_level.value)
operating_mode = map_operating_mode(req.operating_mode.value) if req.operating_mode else None
season_type = map_season_type(req.season_stage.value)
advert_purposes = to_enum_list([x.value for x in req.ad_purposes], map_purpose_target)
target_keyword_types = to_enum_list(
    [x.value for x in req.target_keyword_strategy], map_target_keyword_type
)
advert_direction_types = map_direction_types_json(req.selected_directions)
```

`target_acos_suggest` 写入整数，`daily_budget_suggest` 用 `Decimal(str(req.value))` 转换，避免二进制浮点值直接落入 `DECIMAL(12,2)`。

## 身份、基础字段与失败语义

每次镜像固定使用：

```text
parent_asin        = req.asin
parent_seller_sku  = req.parent_seller_sku
shop_id            = req.shop_id
shop_account       = req.shop_account
site_code          = normalize_site_code(req.site_code)
day_range          = DAY_7
operator           = req.user_id
```

`ProductIdentityMixin` 需要增量接收前端已经由 `callAPI()` 注入的 `_shopAccount`、`_siteCode`、`_userId`，而不是前端新增字段。`shop_id`、`parent_seller_sku`、`asin` 中任一为空时，repository 不得写入；镜像 helper 记录 `config mirror skipped: incomplete identity`。既有端点的原有结果不回滚也不改写。

普通保存端点已经调用 `require_product_identity()`，所以正常 UI 保存必然具备身份。两个 DELETE 端点保留原先不强制身份的行为：请求有完整身份时镜像 `NULL`；没有身份时旧清除照常完成，但记录镜像跳过日志。这样不让新增表反向改变历史 API 契约。

镜像异常（连接、SQL、映射等）处理方式：

```text
旧保存成功
  → 尝试镜像
  → 成功：info 日志（操作类型、ASIN、shop_id、更新列）
  → 失败：logger.exception error 日志（同样的可检索上下文）
  → 返回旧保存的原始响应
```

不得把异常转换为 4xx/5xx，不得修改 Pydantic 响应模型，不得给前端加提示、header 或重试机制。

## 文件职责

- 修改 `ad-direction-agent/app/models/layers.py`：为已有产品身份模型接收既有前端附带的店铺账号、站点和用户 ID。
- 修改 `ad-direction-agent/app/persistence/erp_writer/repository.py`：新增独立的 `upsert_agent_config()`，负责新表的白名单字段映射、SQL、事务提交和连接关闭。
- 新增 `ad-direction-agent/app/api/config_mirror.py`：只负责从请求组装 identity/patch，异步调用 repository，并吞掉镜像失败且记录日志。
- 修改 `ad-direction-agent/app/api/strategy.py`、`app/api/tactics.py`、`app/api/execution.py`：在对应旧保存成功后调用 helper；不改原请求/响应模型和旧保存顺序。
- 新增 `ad-direction-agent/tests/persistence/test_agent_config_mirror.py`：锁定新表 SQL 的字段级更新、编码、审计字段及身份拒写。
- 新增 `ad-direction-agent/tests/api/test_config_mirror.py`：锁定 API 只在旧保存成功后镜像、镜像失败不影响旧响应、DELETE 的身份缺失跳过语义。

### Task 1: 锁定 repository 的字段级 upsert 契约

**Files:**
- Create: `ad-direction-agent/tests/persistence/test_agent_config_mirror.py`
- Modify: `ad-direction-agent/app/persistence/erp_writer/repository.py`

- [ ] **Step 1: 写出失败的 repository 测试**

使用现有 `tests/persistence/test_erp_operating_mode_write.py` 的离线 cursor 范式。新测试中的连接替身必须提供 `cursor()`、`commit()`、`rollback()`、`close()`，不得连接真实 ERP：

```python
def test_upsert_agent_config_updates_only_patch_columns_and_maps_values():
    repo = _repo_with_fake_connection()

    repo.upsert_agent_config(
        identity=_identity(),
        patch={
            "product_position": "重点产品 (P1)",
            "operating_mode": "控制清货",
            "season_type": "旺季准备",
        },
    )

    sql, params = repo._fake_cursor.calls[0]
    assert "INSERT INTO t_advet_agent_config" in sql
    assert "product_position=VALUES(product_position)" in sql
    assert "operating_mode=VALUES(operating_mode)" in sql
    assert "season_type=VALUES(season_type)" in sql
    assert "advert_purposes=VALUES(advert_purposes)" not in sql
    assert "P1_PRODUCT" in params
    assert "CONTROLLED_CLEARANCE" in params
    assert "PEAK_SEASON_PREPARE" in params
    assert repo._fake_connection.committed is True
    assert repo._fake_connection.closed is True
```

再分别写：策略后保存 tactics 不覆盖策略列；ACOS / 预算 / 方向只更新各自一列；`None` 会生成 `target_acos_suggest=VALUES(target_acos_suggest)` 并传参 `None`；非数值 `user_id` 写 `NULL`；缺失唯一键字段抛出 `ValueError` 且不获取连接。

- [ ] **Step 2: 运行测试并确认其因方法不存在而失败**

Run:

```powershell
py -m pytest tests/persistence/test_agent_config_mirror.py -q
```

Expected: 失败原因为 `ErpDualWriterRepository` 尚无 `upsert_agent_config`，而非真实数据库连接或导入错误。

- [ ] **Step 3: 在 repository 增加最小实现**

在 `ErpDualWriterRepository._connect()` 附近新增公开方法。方法签名和白名单必须固定如下：

```python
def upsert_agent_config(self, *, identity: dict[str, Any], patch: dict[str, Any]) -> None:
    allowed = {
        "product_position", "operating_mode", "season_type",
        "advert_purposes", "target_keyword_types",
        "target_acos_suggest", "daily_budget_suggest", "advert_direction_types",
    }
    invalid = set(patch) - allowed
    if invalid:
        raise ValueError(f"unsupported agent config fields: {sorted(invalid)}")
    if not patch:
        raise ValueError("agent config patch is empty")

    parent_asin = str(identity.get("parent_asin") or "").strip()
    parent_seller_sku = str(identity.get("parent_seller_sku") or "").strip()
    shop_id = identity.get("shop_id")
    if not parent_asin or not parent_seller_sku or not shop_id:
        raise ValueError("agent config identity is incomplete")
```

将 patch 中的业务字段先映射成 ERP 列值，再动态生成 **仅包含基础 identity 列与本次 patch 列** 的 `INSERT` 列表，并用同一 patch 列生成 `ON DUPLICATE KEY UPDATE`。插入列始终额外带 `create_by`、`creator_id`、`editor_by`、`editor_id`、`create_time`、`update_time`；重复更新仅更新 patch 列、`editor_by`、`editor_id`、`update_time`。

SQL 结构必须满足：

```sql
INSERT INTO t_advet_agent_config (
    shop_id, shop_account, parent_asin, parent_seller_sku, site_code, day_range,
    product_position, operating_mode, season_type,
    create_by, editor_by, creator_id, editor_id, create_time, update_time
) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON DUPLICATE KEY UPDATE
    product_position=VALUES(product_position),
    operating_mode=VALUES(operating_mode),
    season_type=VALUES(season_type),
    editor_by=VALUES(editor_by),
    editor_id=VALUES(editor_id),
    update_time=VALUES(update_time)
```

不能用全列 `COALESCE`，不能在普通 patch 中加入 `enabled` 或 `frequency`。连接管理按本文件既有形式实现：`conn = self._connect()`，`try` 中执行与 `conn.commit()`，异常时 `conn.rollback()` 后重新抛出，`finally: conn.close()`；不引入连接池。

- [ ] **Step 4: 运行 repository 测试并确认通过**

Run:

```powershell
py -m pytest tests/persistence/test_agent_config_mirror.py tests/persistence/test_erp_operating_mode_write.py -q
```

Expected: 全部通过，且没有真实数据库连接。

### Task 2: 接住现有前端已携带的 identity 元数据

**Files:**
- Modify: `ad-direction-agent/app/models/layers.py:250-258`
- Test: `ad-direction-agent/tests/api/test_config_mirror.py`

- [ ] **Step 1: 写出失败的别名解析测试**

```python
def test_strategy_request_accepts_existing_call_api_identity_extras():
    req = StrategyConfirmRequest.model_validate({
        "asin": "B0TEST",
        "product_level": "重点产品 (P1)",
        "season_stage": "淡季",
        "_shopId": 1622,
        "_parentSellerSku": "SKU-1",
        "_shopAccount": "demo-shop",
        "_siteCode": "US",
        "_userId": "42",
    })
    assert req.shop_account == "demo-shop"
    assert req.site_code == "US"
    assert req.user_id == "42"
```

- [ ] **Step 2: 运行测试并确认失败**

Run:

```powershell
py -m pytest tests/api/test_config_mirror.py::test_strategy_request_accepts_existing_call_api_identity_extras -q
```

Expected: 失败原因为 `ProductIdentityMixin` 尚未定义三个属性。

- [ ] **Step 3: 最小扩展 `ProductIdentityMixin`**

新增三个可选字段，不修改任何现有字段、默认值或 `require_product_identity()`：

```python
shop_account: str | None = Field(
    default=None,
    validation_alias=AliasChoices("shop_account", "_shopAccount", "shopAccount"),
)
site_code: str | None = Field(
    default=None,
    validation_alias=AliasChoices("site_code", "_siteCode", "siteCode"),
)
user_id: str | int | None = Field(
    default=None,
    validation_alias=AliasChoices("user_id", "_userId", "userId"),
)
```

- [ ] **Step 4: 重新运行 Task 2 测试**

Run:

```powershell
py -m pytest tests/api/test_config_mirror.py::test_strategy_request_accepts_existing_call_api_identity_extras -q
```

Expected: PASS。

### Task 3: 新增 API 层镜像 helper，并锁定失败不影响旧保存

**Files:**
- Create: `ad-direction-agent/app/api/config_mirror.py`
- Modify: `ad-direction-agent/tests/api/test_config_mirror.py`

- [ ] **Step 1: 写出失败的 helper 测试**

通过 monkeypatch stub `_get_repository()` 与 `asyncio.to_thread()`，验证 helper 传给 repository 的 identity 和 patch；再验证 repository 抛异常时 helper 返回，不抛出：

```python
@pytest.mark.asyncio
async def test_mirror_failure_is_logged_and_not_raised(monkeypatch, caplog):
    monkeypatch.setattr(config_mirror, "_get_repository", lambda: _FailingRepo())

    await config_mirror.mirror_agent_config(
        req=_request_with_identity(),
        patch={"target_acos_suggest": 25},
        operation="target_acos_override_save",
    )

    assert "agent config mirror failed" in caplog.text
```

并写一个身份不全测试，断言 repository 不被调用，日志包含 `agent config mirror skipped`。

- [ ] **Step 2: 运行 helper 测试并确认失败**

Run:

```powershell
py -m pytest tests/api/test_config_mirror.py -q
```

Expected: 失败原因为 `app.api.config_mirror` 不存在。

- [ ] **Step 3: 实现最小 helper**

`app/api/config_mirror.py` 只定义如下接口：

```python
async def mirror_agent_config(*, req: Any, patch: dict[str, Any], operation: str) -> None:
    identity = {
        "parent_asin": getattr(req, "asin", None),
        "parent_seller_sku": getattr(req, "parent_seller_sku", None),
        "shop_id": getattr(req, "shop_id", None),
        "shop_account": getattr(req, "shop_account", None),
        "site_code": getattr(req, "site_code", None),
        "user_id": getattr(req, "user_id", None),
        "day_range": "DAY_7",
    }
    if not _has_complete_identity(identity):
        logger.warning(
            "agent config mirror skipped: operation=%s asin=%r shop_id=%r",
            operation,
            identity["parent_asin"],
            identity["shop_id"],
        )
        return
    try:
        await asyncio.to_thread(_get_repository().upsert_agent_config, identity=identity, patch=patch)
        logger.info(
            "agent config mirror saved: operation=%s asin=%s shop_id=%s columns=%s",
            operation,
            identity["parent_asin"],
            identity["shop_id"],
            sorted(patch),
        )
    except Exception:
        logger.exception(
            "agent config mirror failed: operation=%s asin=%s shop_id=%s columns=%s",
            operation,
            identity["parent_asin"],
            identity["shop_id"],
            sorted(patch),
        )
```

从 `app.persistence.erp_writer.repository` 导入 `_get_repository`。不得从 helper 返回任何状态，不得发送请求、读取新表或增加重试。

其中身份判断必须实际定义为：

```python
def _has_complete_identity(identity: dict[str, Any]) -> bool:
    if not str(identity.get("parent_asin") or "").strip():
        return False
    if not str(identity.get("parent_seller_sku") or "").strip():
        return False
    try:
        return int(identity.get("shop_id")) > 0
    except (TypeError, ValueError):
        return False
```

- [ ] **Step 4: 重新运行 Task 3 测试**

Run:

```powershell
py -m pytest tests/api/test_config_mirror.py -q
```

Expected: PASS，日志断言覆盖 saved、skipped、failed 三种情形。

### Task 4: 在五类既有保存后的成功路径追加镜像

**Files:**
- Modify: `ad-direction-agent/app/api/strategy.py:28-36`
- Modify: `ad-direction-agent/app/api/tactics.py:36-44`
- Modify: `ad-direction-agent/app/api/execution.py:25-35,80-128`
- Modify: `ad-direction-agent/tests/api/test_config_mirror.py`

- [ ] **Step 1: 写出失败的端点顺序和 patch 测试**

用假的 orchestrator 与 monkeypatch `mirror_agent_config`，直接调用端点函数。每个测试先令旧调用成功，再断言 mirror 收到如下内容：

```python
assert mirror.call_args.kwargs["patch"] == {
    "product_position": "重点产品 (P1)",
    "operating_mode": "控制清货",
    "season_type": "淡季",
}
```

另外覆盖：

```python
{"advert_purposes": ["转化型"], "target_keyword_types": ["长尾词"]}
{"target_acos_suggest": 25}
{"daily_budget_suggest": Decimal("12.5")}
{"advert_direction_types": ["push_natural", "optimize_acos"]}
{"target_acos_suggest": None}
{"daily_budget_suggest": None}
```

对每个旧保存失败的测试，断言 `mirror_agent_config` 未被调用且异常/旧失败响应仍保留。

- [ ] **Step 2: 运行端点测试并确认失败**

Run:

```powershell
py -m pytest tests/api/test_config_mirror.py -q
```

Expected: 失败原因为五个端点尚未调用 mirror，而不是接口模型或前端相关错误。

- [ ] **Step 3: 以“旧调用成功后、原样 return 前”为唯一接入位置实现**

每个端点遵循下列结构：

```python
result = await orchestrator.confirm_strategy(req)
await mirror_agent_config(
    req=req,
    patch={
        "product_position": req.product_level.value,
        "operating_mode": req.operating_mode.value if req.operating_mode else None,
        "season_type": req.season_stage.value,
    },
    operation="strategy_confirm",
)
return result
```

`strategy_confirm`、`tactics_confirm`、`execution_select` 的 `result` 保持原 Pydantic 响应对象，不序列化、不添加字段。P3 POST 端点在既有保存函数返回 `ok=True` 后才镜像；它们原有 `{asin, saved, value}` body 必须逐字保持。DELETE 端点也只在既有清除函数返回 `ok=True` 后请求镜像；若请求身份不全，由 helper 记录 skipped，不增加 `require_product_identity()`。

四个 P3 override/clear 接入点的结构必须固定为：

```python
ok = orchestrator.save_target_acos_override(
    req.asin, req.value, shop_id=req.shop_id, parent_seller_sku=req.parent_seller_sku
)
if ok:
    await mirror_agent_config(
        req=req,
        patch={"target_acos_suggest": req.value},
        operation="target_acos_override_save",
    )
return {"asin": req.asin, "saved": ok, "value": req.value}
```

预算保存将 patch 改为 `{"daily_budget_suggest": Decimal(str(req.value))}`；两个清除端点分别传 `{"target_acos_suggest": None}` 与 `{"daily_budget_suggest": None}`，并保留原 `{asin, cleared}` response body。

- [ ] **Step 4: 重新运行端点测试**

Run:

```powershell
py -m pytest tests/api/test_config_mirror.py tests/api/test_product_identity_required.py -q
```

Expected: PASS；原有身份门禁用例不变，DELETE 的既有无身份行为未被改写。

### Task 5: 回归验证与上线前表结构核验

**Files:**
- No production-file changes.

- [ ] **Step 1: 运行定向完整测试集**

Run:

```powershell
py -m pytest tests/persistence/test_agent_config_mirror.py tests/api/test_config_mirror.py tests/persistence/test_erp_operating_mode_write.py tests/persistence/test_erp_enums.py tests/persistence/test_override_persistence.py tests/api/test_product_identity_required.py -q
```

Expected: 全部 PASS；测试中不连接真实 ERP。

- [ ] **Step 2: 执行 Python 语法校验**

Run:

```powershell
py -m py_compile app/models/layers.py app/persistence/erp_writer/repository.py app/api/config_mirror.py app/api/strategy.py app/api/tactics.py app/api/execution.py
```

Expected: exit code 0。

- [ ] **Step 3: 审查最终 diff 是否符合边界**

Run:

```powershell
git diff -- ad-direction-agent/app/models/layers.py ad-direction-agent/app/persistence/erp_writer/repository.py ad-direction-agent/app/api/config_mirror.py ad-direction-agent/app/api/strategy.py ad-direction-agent/app/api/tactics.py ad-direction-agent/app/api/execution.py ad-direction-agent/tests/persistence/test_agent_config_mirror.py ad-direction-agent/tests/api/test_config_mirror.py
```

Expected: 不含 `demo/ad-asisitant-agent.html`、批跑脚本、旧 State 写入、旧决策配置表读取/迁移、任何 MCP/LLM 调用、响应模型字段或 header 变更。

- [ ] **Step 4: 上线前在目标 ERP 库做只读 DDL 核验**

Run:

```sql
SHOW CREATE TABLE t_advet_agent_config;
SHOW INDEX FROM t_advet_agent_config WHERE Key_name = 'uk_asin_sku_shop';
```

Expected: 表名、八个业务列、四个审计列、`enabled`、`frequency` 和唯一键 `(parent_asin, parent_seller_sku, shop_id)` 与本方案一致。若不存在或索引字段顺序不同，停止部署，不在应用运行时建表或迁移。

## 自检结论

- 前端、响应 body 和 header 均不在任何任务的修改清单中。
- 每个保存只写自身 patch；不会因后保存而清空其他列。
- 两个清除端点通过显式 `None` 避免未来批跑读取过期覆盖值；身份缺失时保持旧 API 的清除语义并留后端可检索日志。
- 新表失败不会回滚旧保存；由于本期新表尚未被消费，这是刻意的非阻塞双写策略。
