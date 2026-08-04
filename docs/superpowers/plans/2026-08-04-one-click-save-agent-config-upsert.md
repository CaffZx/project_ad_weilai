> **For agentic workers:** REQUIRED SUB-SKILL: Use `subagent-driven-development` (recommended) or `executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **实施纠偏（2026-08-04，优先于下文旧伪代码）**：后续诊断/P3/方向加载不再等待 `confirmTactics`；`btnTactics` 隐藏后由 `loadTactics()` 直接加载后续面板。ERP `t_advert_agent_config` 单行 upsert 是唯一原子边界，state DB 为逐项检查结果、失败可幂等重试的镜像，不再宣称跨库整体 atomic。方向写入使用 `save_execution({selected_directions, sub_options})`，不存在 `set_execution_selection`。`GET latest` 按 `(parent_asin,parent_seller_sku,shop_id)` 查询并反向映射 ERP 枚举。保存栏显式受 C 态 `in_progress` 门禁控制，不能仅跟随 `cardP3Override`。具体实现与测试以本轮代码为准。

**Goal:** 在主看板 `demo/ad-asisitant-agent.html` 左侧配置栏「配置可修改态」(C 态)下,新增 1 个底部 sticky 「一键保存」按钮**取代**当前 4 个分散保存键(`btnTactics` / `p3AcosLeftBtn` / `p3BudgetLeftBtn` / `btnSaveDirections`);一键保存触发**单后端端点** `POST /long-term-config/{asin}/save-all`,该端点**一次性 atomic upsert 全部配置字段到 Agent 配置表**:state DB 三处写(语义同原 4 端点) + ERP 配置表全字段 upsert(战略层 3 列从 state DB `long_term_config` 读补齐凑成 8 列)。保存后端点回读并返回最新 snapshot,前端**所有可修改态的渲染统一改为从该 snapshot(agent_config 真源)回读,为空显空**。原 4 键的全部工作流副作用(tab 解锁 / `loadMainInsightPanels` / `renderP3` / 反馈捕获 / `setSaveTag` / 徽标)**完整保留**,集中到一键保存成功回调里执行一遍。**严禁动快照态**(B 态 `renderReadonlyPreset`)。

**Architecture:** 后端复用现有 `repository.upsert_agent_config` 方法(已支持 8 列任选子集 atomic upsert,`INSERT ... ON DUPLICATE KEY UPDATE`),新增 `POST /long-term-config/{asin}/save-all` 端点 + `GET /long-term-config/{asin}/latest` 端点 + `SaveAllConfigRequest`/`ConfigSnapshotResponse` 模型。state DB 三处写复用现有 `state_manager` 方法(`set_long_term_config` / `set_target_acos_override` / `set_execution_selection`),不新写 SQL。前端新增 `saveAllConfig()` + `applySaveAllSideEffects(snapshot)` + `applyConfigSnapshotToEditable(snapshot)`;C 态初始化(`loadAll` 路径)在战略/策略/D3 各自加载完成后**额外**调一次 `GET .../latest` 把 agent_config 真源值回填到可编辑控件(空则显空),与"保存后回读"用同一套 `applyConfigSnapshotToEditable` 对称。战略层 `btnStrategy` 保留原键原行为不进一键。原 4 键 DOM `hidden` 起手、函数体保留(防外部引用崩 + 便于回滚)。

**Tech Stack:** FastAPI、Pydantic v2、Python asyncio、PyMySQL、JavaScript(原生,无框架)、`demo/ad-asisitant-agent.html` 单文件前端。

---

## 1. 现状报告

### 1.1 前端 ABC 三态(已核实 [ad-asisitant-agent.html:1040-1124](AD_assistant_agent-v3.2/ad-direction-agent/demo/ad-asisitant-agent.html#L1040-L1124))

| 态 | 判定(`bootstrapFromContext`/`applyBatchBarFromCtx`) | 页面形态 | 本方案权限 |
|---|---|---|---|
| **A 态**未配置 | `!has_config && !in_progress` | `renderEmptyStateA` [L1061](AD_assistant_agent-v3.2/ad-direction-agent/demo/ad-asisitant-agent.html#L1061):隐藏战略/策略层 card、隐藏 tab 栏、显示中心引导,左侧只留基础信息卡 | 不涉及:本键在 A 态不显示(无 `cardP3Override`) |
| **B 态**已配置·无进行中 | `has_config && !in_progress && selectedDecisionId()` | `renderReadonlyPreset` [L1193](AD_assistant_agent-v3.2/ad-direction-agent/demo/ad-asisitant-agent.html#L1193):全栏只读快照,战略/策略 `_snapshotRow` 大字展示,P3 `p3ReadonlyView` 只读大字 [L759-769](AD_assistant_agent-v3.2/ad-direction-agent/demo/ad-asisitant-agent.html#L759-L769),`sec1Done/sec2Done` 文案改"快照" | **严禁改动**。本方案不动 `renderReadonlyPreset`/`setSnapshotReadonlyControls`/`p3ReadonlyView` 任何渲染分支 |
| **C 态**进行中 | `in_progress` | `loadAll(true)` [L2070](AD_assistant_agent-v3.2/ad-direction-agent/demo/ad-asisitant-agent.html#L2070):`setSnapshotReadonlyControls(false)` [L2098](AD_assistant_agent-v3.2/ad-direction-agent/demo/ad-asisitant-agent.html#L2098) + `p3EditRows` 可编辑 [L2101](AD_assistant_agent-v3.2/ad-direction-agent/demo/ad-asisitant-agent.html#L2101) + `leftDirectionsEdit` 可编辑 [L2104](AD_assistant_agent-v3.2/ad-direction-agent/demo/ad-asisitant-agent.html#L2104);`show('cardP3Override')` 发生在 `confirmTactics` 成功后 [L2619](AD_assistant_agent-v3.2/ad-direction-agent/demo/ad-asisitant-agent.html#L2619) | **只允许在此态改**。本键显示条件 = `cardP3Override` 可见;可修改态渲染回读从 agent_config |

> 三态切换由 `shouldUseSnapshotMode` [L1151](AD_assistant_agent-v3.2/ad-direction-agent/demo/ad-asisitant-agent.html#L1151) 与 `applyBatchBarFromCtx` [L1119](AD_assistant_agent-v3.2/ad-direction-agent/demo/ad-asisitant-agent.html#L1119) 统一驱动。C/ B 切换走 `switchBatch` [L1126](AD_assistant_agent-v3.2/ad-direction-agent/demo/ad-asisitant-agent.html#L1126) → `renderReadonlyDecisionPreset`;新建事件 C→`onComplete` 后回 B。本方案不新增任何态、不改切换逻辑。

### 1.2 配置可修改态(C 态)现状的 4 个保存键 + 各自回读来源(已核实)

| 键 | DOM:行 / 函数:行 | 调的端点 | state DB 写 | ERP 镜像列(`mirror_agent_config` 副作用) | 成功后前端副作用 |
|---|---|---|---|---|---|
| `btnStrategy` **战略层(保留)** | [L712](AD_assistant_agent-v3.2/ad-direction-agent/demo/ad-asisitant-agent.html#L712) / `confirmStrategy` [L2383](AD_assistant_agent-v3.2/ad-direction-agent/demo/ad-asisitant-agent.html#L2383) | `POST /strategy/confirm` | `long_term_config`: `product_level`/`operating_mode`/`season_stage` | `product_position`/`operating_mode`/`season_type` | `_setSavedStrategyBaseline`、解锁 Tab1/Tab5、`loadTactics`、`立即退出`分支 |
| `btnTactics` 策略层 | [L2533](AD_assistant_agent-v3.2/ad-direction-agent/demo/ad-asisitant-agent.html#L2533) / `confirmTactics` [L2590](AD_assistant_agent-v3.2/ad-direction-agent/demo/ad-asisitant-agent.html#L2590) | `POST /tactics/confirm` | `long_term_config`: `ad_purposes`/`target_keyword_strategy` | `advert_purposes`/`target_keyword_types` | `setTabEnabled('tab2','tab3',true)`、`show('cardP3Override')`、`showMainContent`、`loadMainInsightPanels`、`setTabEnabled('tab4',true)`、`fbCaptureTactics`、`_strategyLayerSaved.tactics=true`、`setSaveTag('purpose'/'keyword','saved')`、`refreshStrategyLayerBadge` |
| `p3AcosLeftBtn` ACOS | [L744](AD_assistant_agent-v3.2/ad-direction-agent/demo/ad-asisitant-agent.html#L744) / `saveP3AcosLeft` [L3054](AD_assistant_agent-v3.2/ad-direction-agent/demo/ad-asisitant-agent.html#L3054) | `POST /execution/target-acos/override` | `target_acos_override.json` | `target_acos_suggest` | `renderP3`、`fbCaptureP3Acos`、`_strategyLayerSaved.acos=true`、`setSaveTag('acos','saved')`、`refreshStrategyLayerBadge` |
| `p3BudgetLeftBtn` 预算 | [L754](AD_assistant_agent-v3.2/ad-direction-agent/demo/ad-asisitant-agent.html#L754) / `saveP3BudgetLeft` [L3077](AD_assistant_agent-v3.2/ad-direction-agent/demo/ad-asisitant-agent.html#L3077) | `POST /execution/budget-bid/override` | `long_term_config.daily_budget_override` | `daily_budget_suggest` | 同上,`fbCaptureP3Budget` |
| `btnSaveDirections` 方向 | [L785](AD_assistant_agent-v3.2/ad-direction-agent/demo/ad-asisitant-agent.html#L785) / `saveDirectionsLeft` [L3386](AD_assistant_agent-v3.2/ad-direction-agent/demo/ad-asisitant-agent.html#L3386) | `POST /execution/select` | `workflow_state.json.execution` | `advert_direction_types` | `fbCaptureExecution`、`_strategyLayerSaved.directions=true`、`setSaveTag('directions','saved')`、`refreshStrategyLayerBadge` |

**可修改态当前的回读(渲染)来源不统一**——这是本方案要解决的另一面:

| 字段 | 当前回读来源 | 行 |
|---|---|---|
| 战略层 3 字段 | `GET /strategy/options`(`loadStrategy`) | [L2172](AD_assistant_agent-v3.2/ad-direction-agent/demo/ad-asisitant-agent.html#L2172) |
| 策略层 2 字段 | `GET /tactics/options`(`loadTactics`) | [L2495](AD_assistant_agent-v3.2/ad-direction-agent/demo/ad-asisitant-agent.html#L2495) |
| ACOS/预算 placeholder | `GET /execution/recommend` 的 `target_acos.manual_override` / `budget_bid.manual_override`(`autoFillLeftInputs`) | [L3034-3043](AD_assistant_agent-v3.2/ad-direction-agent/demo/ad-asisitant-agent.html#L3034-L3043) |
| 方向 placeholder 勾选 | 4 固定方向预填 `_dirFallbackOptsHtml` [L3133](AD_assistant_agent-v3.2/ad-direction-agent/demo/ad-asisitant-agent.html#L3133);若 state DB 有 `execution.selected_directions` 才回填 | [L2105](AD_assistant_agent-v3.2/ad-direction-agent/demo/ad-asisitant-agent.html#L2105) |

> 4 个回读 API 走 3 套数据源(state DB `long_term_config` / `target_acos_override.json` / `workflow_state.json.execution`)与/或 LLM 现算,**没有一处从 Agent 配置表**回读。这意味着:运营上次保存后,下次回访看到的值未必等于落库的配置表值(尤其方向字段——state DB `execution.selected_directions` 与 ERP `advert_direction_types` 不一定同步)。

### 1.3 后端 Agent 配置表写入能力(已核实 [repository.py:132-237](AD_assistant_agent-v3.2/ad-direction-agent/app/persistence/erp_writer/repository.py#L132-L237))

- `upsert_agent_config`:签名为 8 列任选子集,`INSERT ... ON DUPLICATE KEY UPDATE`(atomic),唯一键 `(parent_asin, parent_seller_sku, shop_id)`(**三列,不含 `site_code`**,[repository.py:159-165](AD_assistant_agent-v3.2/ad-direction-agent/app/persistence/erp_writer/repository.py#L159-L165) 校验也只查这三),8 列 = 战略层 3 + 策略层 2 + P3 2 + 方向 1。`site_code` 在 base_columns 但**不进唯一键**,只作 base 字段写入。写入表名 `t_advert_agent_config`(已改名,见上核实段)。
- mapper 齐全(`map_product_position`/`map_operating_mode`/`map_season_type`/`map_purpose_target`/`map_target_keyword_type`/`map_direction_types_json`),前端传中文值即可。
- 当前被 `mirror_agent_config` [config_mirror.py:25](AD_assistant_agent-v3.2/ad-direction-agent/app/api/config_mirror.py#L25) 在 4 个分散端点里当副作用调,各传 1-2 列 partial patch。

> ⚠ **表名与状态(已连本地库核验,2026-08-04)**:目标 ERP 库(`127.0.0.1:3307`,`erp_agentadvert_chen`)中 Agent 配置表**存在且 DDL 完整**——8 业务列 + `enabled`(默认 1)+ `frequency`(默认 `DAILY`)+ 审计列齐全,唯一键 `uk_asin_sku_shop (parent_asin, parent_seller_sku, shop_id)`(**三列,不含 `site_code`**)。表内有 1 行真实数据(`US-单肩带分体高腰泳衣` ASIN,8 列大部分已填,`update_time=2026-08-03 22:51:05`),证明分散保存的 4 个 `mirror_agent_config` 调用链一直在正常落库(没静默失败)。**但该行 `advert_purposes`/`target_keyword_types` 两列为 `NULL`**——业务真相是该 ASIN 当时只跑过战略+P3+方向保存、**没跑过策略层 `confirmTactics`**,所以这两列从未被写。这恰好佐证 §2.1 的核心痛点:**分散保存导致同一行 8 列时间戳错位、列填充参差**——一键 atomic upsert 8 列正好根治。**R1 风险解除,表已上线**。
>
> **表名拼写订正(2026-08-04 已落地)**:该表原名 `t_advet_agent_config`(`advet` 历史缩写)是全库唯一异例,其它 `t_advert_agent_*` 表均用全拼 `advert`。趁本方案新增对这表的读路径,已一并把表名理顺为 `t_advert_agent_config`:代码侧 [repository.py:223](AD_assistant_agent-v3.2/ad-direction-agent/app/persistence/erp_writer/repository.py#L223) + [decision_config_reader.py:56](AD_assistant_agent-v3.2/ad-direction-agent/app/data/decision_config_reader.py#L56) + 2 个测试已改;DB 侧 migrate 脚本 [scripts/erp_db/2026-08-04-rename-advet-to-advert.sql](AD_assistant_agent-v3.2/ad-direction-agent/scripts/erp_db/2026-08-04-rename-advet-to-advert.sql) `RENAME TABLE` 已在本地库执行验证(数据 1 行无损保留,`mirror` 测试 11 个全绿)。**生产库部署顺序**:维护窗口内先 `RENAME TABLE`,再部署改名后的代码(窗口期内新代码访问旧表名会报 Table 不存在,但被 `mirror_agent_config` 的 `logger.exception` 吞掉,主保存链路无影响,仅日志告警)。本方案下文所有对 `t_advert_agent_config` 的引用均为改名后的表名。

---

## 2. 要解决的问题

1. **保存入口分散且分裂**:同一配置栏 4 个保存键,运营要逐个点;每键只更新 ERP 配置表的 1-2 列,产生 4 段 partial upsert,列时间戳错位(正是 [load_latest_config.py:1-77](AD_assistant_agent-v3.2/ad-direction-agent/scripts/load_latest_config.py#L1-L77) 注释抱怨的"逐字段取最近非空"问题)。一键 atomic upsert 全 8 列直接根治。
2. **可修改态回读来源不统一**:4 字段回读走 3 套源 + 1 套 LLM 现算,与落库的 Agent 配置表无回读关系——保存后下次回访看到的"前次配置值"可能不等于落库值。统一改成"可修改态从 agent_config 真源回读,空则显空",与"保存后回读"对称。
3. **补丁 [codex_ux_patch.js:2372-2425](临时文件/codexvender/v2前端补丁/codex_ux_patch.js#L2372-L2425) 的"一键保存"逻辑错误**:它用"串联调 4 个 vendor 函数 + 各自前置校验"的方式实现,会照旧产生 4 段 partial ERP 镜像(没解决 §2.1),且 4 段串行可靠性差。要换成"单后端端点一次 atomic 写"。

不解决(明确边界):
- 不改快照态(B 态 `renderReadonlyPreset`)任何渲染分支。
- 不改战略层 `btnStrategy` 键及 `confirmStrategy`(它是工作流入口 + 立即退出分支,不与纯 SQL 保存混)。
- 不改后端 4 个原端点(`/tactics/confirm` 等)——它们仍服务其它路径(`generateReport` 用 `/execution/select`、批跑可能用其它)。
- 不改 `mirror_agent_config` 调用链——其它路径若仍单点保存可用;一键保存新端点走自己的全字段 upsert,不依赖 mirror。

---

## 3. 实施方案

### 3.1 后端(3 文件)

#### `app/models/layers.py` — 新模型

```python
class SaveAllConfigRequest(ProductIdentityMixin):
    ad_purposes: list[str] | None = None
    target_keyword_strategy: list[str] | None = None
    target_acos: int | None = Field(default=None, ge=5, le=100)
    daily_budget: float | None = Field(default=None, gt=0)
    directions: list[str] | None = None

class ConfigSnapshotResponse(BaseModel):
    asin: str
    product_level: str | None = None
    operating_mode: str | None = None
    season_stage: str | None = None
    ad_purposes: list[str] = []
    target_keyword_strategy: list[str] = []
    target_acos: int | None = None
    daily_budget: float | None = None
    directions: list[str] = []
    p3: dict | None = None   # manual_override flag + recommended(给 renderP3);可空
    last_modified: str = ""
```

#### `app/api/long_term_config.py` — 新端点 + 回读 helper

```python
@router.post("/long-term-config/{asin}/save-all", response_model=ConfigSnapshotResponse)
async def save_all_config(asin, req: SaveAllConfigRequest, sm=Depends(get_state_manager)):
    require_product_identity_dict(req.model_dump(), asin=asin)
    # 1) state DB 三处写(语义同原 4 端点对 state DB 的写;不发 LLM/MCP、不进工作流推进)
    long_updates = {}
    if req.ad_purposes is not None: long_updates["ad_purposes"] = req.ad_purposes
    if req.target_keyword_strategy is not None: long_updates["target_keyword_strategy"] = req.target_keyword_strategy
    if req.daily_budget is not None: long_updates["daily_budget_override"] = req.daily_budget
    if long_updates: sm.set_long_term_config(asin, long_updates)
    if req.target_acos is not None: sm.set_target_acos_override(asin, req.target_acos)
    if req.directions is not None: sm.set_execution_selection(asin, req.directions)
    # 2) 战略层 3 列从 state DB 读补齐,凑 8 列 patch
    base = sm.get_long_term_config(asin)
    patch = {
        "product_position":     base.get("product_level"),
        "operating_mode":       base.get("operating_mode"),
        "season_type":         base.get("season_stage"),
        "advert_purposes":     req.ad_purposes,
        "target_keyword_types": req.target_keyword_strategy,
        "target_acos_suggest":  req.target_acos,
        "daily_budget_suggest": req.daily_budget,
        "advert_direction_types": req.directions,
    }
    patch = {k: v for k, v in patch.items() if v is not None}
    # 3) 单条全字段 atomic upsert → Agent 配置表(复用 repository.upsert_agent_config,无需新 SQL)
    identity = {"parent_asin": asin, "parent_seller_sku": req.parent_seller_sku,
                "shop_id": req.shop_id, "shop_account": req.shop_account,
                "site_code": req.site_code, "user_id": req.user_id}
    await asyncio.to_thread(_get_repository().upsert_agent_config,
                            identity=identity, patch=patch)
    # 4) 回读 snapshot(单一真源 = Agent 配置表 + state DB override flag)
    return await _build_config_snapshot(asin, identity, sm)


@router.get("/long-term-config/{asin}/latest", response_model=ConfigSnapshotResponse)
async def get_latest_config(asin, shop_id=None, parent_seller_sku=None, sm=Depends(get_state_manager)):
    """C 态可修改态初始化回读:从 Agent 配置表读 8 列,与 state DB override merge"""
    identity = {"parent_asin": asin, "shop_id": shop_id, "parent_seller_sku": parent_seller_sku}
    return await _build_config_snapshot(asin, identity, sm)
```

`_build_config_snapshot`:从 Agent 配置表读最新一行 8 列(新增 `repository.get_agent_config_row(identity)`,SELECT WHERE `(parent_asin, parent_seller_sku, shop_id)` ORDER BY `update_time DESC LIMIT 1`,**不用 site_code**),与 state DB `target_acos_override` `manual_override` flag 合成 `p3` dict(`manual_override = (target_acos_suggest 或 daily_budget_suggest 非空)`,`recommended_target = target_acos_suggest`,`suggested = daily_budget_suggest`),组成 `ConfigSnapshotResponse`。空行/无 row → 所有字段 null/[]。

> identity 在 `GET /latest` 上:前端 `loadAll` 进入 C 态时已能从 `strategy_context`/`decision_context` 拿到完整三元组,作为 query 参数传;若不全则返空 snapshot(各字段 null/[])——"为空显空"语义自然成立。`site_code` 在查询里**不作为过滤条件**,只读 `identity` 已确认存在的行。

#### `app/persistence/erp_writer/repository.py` — 加读方法

- `get_agent_config_row(identity) -> dict | None`:SELECT 8 列 WHERE `(parent_asin, parent_seller_sku, shop_id, site_code)` ORDER BY `update_time DESC LIMIT 1`。复用 `_connect`。
- `upsert_agent_config` 零改动。

### 3.2 前端(1 文件:`ad-asisitant-agent.html`)

#### A. HTML

1. [L744](AD_assistant_agent-v3.2/ad-direction-agent/demo/ad-asisitant-agent.html#L744) `p3AcosLeftBtn`、[L754](AD_assistant_agent-v3.2/ad-direction-agent/demo/ad-asisitant-agent.html#L754) `p3BudgetLeftBtn`、[L785](AD_assistant_agent-v3.2/ad-direction-agent/demo/ad-asisitant-agent.html#L785) `btnSaveDirections` 加 `class="hidden"`(保留 id 防 vendor 别处引用崩)。
2. [L2533](AD_assistant_agent-v3.2/ad-direction-agent/demo/ad-asisitant-agent.html#L2533) `btnTactics` 注入模板加 `hidden`。
3. 侧栏底部加(战略层 card 之后、`#sidebar` 末尾、`cardP3Override` 同级):
   ```html
   <div id="codexSaveAllBar">
     <button id="codexSaveAllBtn" type="button" onclick="saveAllConfig()">💾 一键保存</button>
   </div>
   ```
   显示条件 = `cardP3Override` 可见(`show/hide` 跟随现有 `cardP3Override` 的 show 调用点,即 `confirmTactics` 后 show + `renderReadonlyPreset` 后 hide)。

#### B. CSS

照搬补丁 [codex_ux_patch.js:2283-2361](临时文件/codexvender/v2前端补丁/codex_ux_patch.js#L2283-L2361) 的 `#codexSaveAllBar`(sticky bottom+z-index)/`#codexSaveAllBtn`(蓝底 `#1D4ED8` + hover/`[disabled]`)/`.spinner-mini`/`#codexSaveToast` + `@keyframes codexSaveSpin` 贴进主看板 `<style>`。**删** `#btnTactics { display:none }` 那几行(已用 `hidden` class 处理)。

#### C. JS — 新函数

`saveAllConfig()` + `applySaveAllSideEffects(snap, saved)` + `applyConfigSnapshotToEditable(snap)` + 辅助 `_checked` / `_numOr` / `_lockSaveAllBtn` / `_showToast`。核心:

```js
let _saveAllBusy = false;
async function saveAllConfig() {
  if (_saveAllBusy) return;
  const ap = _checked('ad_purposes'), kt = _checked('target_keyword_strategy');
  const dirs = _checked('directions');
  const acos = _numOr('p3AcosLeftInput', 5, 100);
  const budget = _numOr('p3BudgetLeftInput', 1, null);
  if (ap.length === 0 || kt.length === 0) { _showToast('请至少选择一项广告目的/关键词策略', 'error'); return; }
  if (dirs.length === 0) { _showToast('请至少选择一个广告方向', 'error'); return; }
  _saveAllBusy = true; _lockSaveAllBtn(true);
  try {
    const body = { asin, shop_id, parent_seller_sku, shop_account, site_code,
                   ad_purposes: ap, target_keyword_strategy: kt,
                   target_acos: acos, daily_budget: budget, directions: dirs };
    const snap = await callAPI(`/long-term-config/${encodeURIComponent(asin)}/save-all`, body, API_TIMEOUT.default);
    applyConfigSnapshotToEditable(snap);     // 回读真源统一回填所有控件 + 标签
    await applySaveAllSideEffects(snap, { ap, kt, dirs, acos, budget });  // 保留全部副作用
    _showToast('✓ 已保存', 'success');
  } catch (e) {
    _showToast('✗ 保存失败: ' + (e.message || ''), 'error');
  }
  _lockSaveAllBtn(false); _saveAllBusy = false;
}
```

`applyConfigSnapshotToEditable(snap)` —— **统一回读**(C 态可修改态 + 保存后):把 snap 的 8 字段回填战略层下拉(`applyPersistedStrategySelection`)、策略层复选(`applyPersistedTacticsSelection`,新增 helper)、ACOS/预算 input placeholder(`snap.target_acos/daily_budget` 空则 placeholder 清空)、方向勾选(`applyPersistedDirectionSelection`,新增)。空 snap → 全显空。所有标签 `setSaveTag` 与 `_saved._dirty` 由这里统一刷新——对称"保存后照样回读"。

`applySaveAllSideEffects(snap, saved)` —— **副作用完整保留**(把 4 个原函数尾段搬到此一处):
- `setTabEnabled('tab2','tab3',true)` + `show('cardP3Override')` + `setTabEnabled('tab4',false)` + `showMainContent()` + `if (!activeTab) switchTab('tab1')`(来自 `confirmTactics`)
- `fbCaptureTactics(_lastTacticsAiRec.ad_purposes, _lastTacticsAiRec.target_keyword_strategy, saved.ap, saved.kt)`
- `fbCaptureP3Acos(window._p3data?.target_acos?.recommended_target ?? null, saved.acos)` + `fbCaptureP3Budget(window._p3data?.budget_bid?.suggested ?? null, saved.budget)`
- `fbCaptureExecution(_lastExecAiDirs, saved.dirs)`
- `_strategyLayerSaved.{tactics,acos,budget,directions}` 按原语义置 true + `refreshStrategyLayerBadge`
- 若 `snap.p3`:`window._p3data = snap.p3; if (snap.p3.target_acos) snap.p3.target_acos.manual_override = (saved.acos!=null); renderP3(window._p3data)`(来自 `saveP3AcosLeft/saveP3BudgetLeft`)
- `setSaveTag('purpose','keyword','acos','budget','directions', 'saved')` + `_saved.* = 当前值` + `_dirty.* = false` + `_manualTouched.* = false`
- `try { await loadMainInsightPanels(); setTabEnabled('tab4', true); } catch(...) err('errGlobal', ...)`(来自 `confirmTactics` 末段,完整保留)

#### D. 可修改态初始化回读改造

`loadAll` [L2070](AD_assistant_agent-v3.2/ad-direction-agent/demo/ad-asisitant-agent.html#L2070) 在 `shouldUseSnapshotMode()` [L2137](AD_assistant_agent-v3.2/ad-direction-agent/demo/ad-asisitant-agent.html#L2137) false 分支末尾、`loadStrategy`/`loadTactics` 完成后,新增一次:
```js
try {
  const snap = await callAPI(`/long-term-config/${encodeURIComponent(asin)}/latest`, null, API_TIMEOUT.default, 'GET');
  applyConfigSnapshotToEditable(snap);   // 用 agent_config 真源覆盖各 API 现算值,空则显空
} catch (_) { /* 回读失败不阻塞,沿用各 API 已渲染值 */ }
```
这步让"C 态可修改态回读"也走 agent_config 真源,与"保存后回读"用**同一套 `applyConfigSnapshotToEditable`** 对称——满足"对所有配置标签都统一对称"。

`loadExecution` [L2907](AD_assistant_agent-v3.2/ad-direction-agent/demo/ad-asisitant-agent.html#L2907) 末尾的 `autoFillLeftInputs` 调用 **保留**(拿 LLM AI 推荐),紧接其后 `latest` 回读覆盖 placeholder(若 agent_config 有 user 保存值则 placeholder=该值,否则保持 AI 推荐/空)。`loadStrategy`/`loadTactics` 各自加载完后也保留原加载,只让 `latest` 在最后覆盖真源值。

#### E. 旧 4 函数怎么处理

- `confirmTactics` / `saveP3AcosLeft` / `saveP3BudgetLeft` / `saveDirectionsLeft` 函数体**保留不动**(防 `generateReport` [L3422](AD_assistant_agent-v3.2/ad-direction-agent/demo/ad-asisitant-agent.html#L3422) 用 `/execution/select`、防外部脚本、便于回滚)。
- 4 个按钮 DOM `hidden` 起手 → 运营只能点一键保存 → 不会再产生分散 partial ERP 镜像(顺带消除"一键后误点单键 partial 分裂")。
- `clearP3Acos` / `clearP3Budget` 保留(取消覆盖走 DELETE override,一键不覆盖该语义)。

### 3.3 不改动的部分(明确边界)

- `btnStrategy` + `confirmStrategy` 一字不动(战略层工作流入口 + 立即退出分支)。
- `renderReadonlyPreset` / `setSnapshotReadonlyControls` / `p3ReadonlyView` / B 态所有快照渲染分支一字不动。
- 后端 4 个原端点 + `mirror_agent_config` + `upsert_agent_config` SQL 不动。
- `guardUnsavedBeforeRun` [L2367](AD_assistant_agent-v3.2/ad-direction-agent/demo/ad-asisitant-agent.html#L2367) 脏检测拦截一字不动(一键保存成功后全字段置 `saved`,跑分析前拦截照常)。

---

## 4. 验证

- [ ] **T1 后端单端点 atomic**:mock repo,POST `/save-all` 传 5 列 → 验证 repo `upsert_agent_config` 收到 8 列 patch(战略 3 从 state DB 补齐)、state DB `set_long_term_config`/`set_target_acos_override`/`set_execution_selection` 各调一次。
- [ ] **T2 后端校验**:ACOS=200 → 422 + `detail` 含 `ge/le`;budget=0 → 422;没传 directions 不报错(null)。
- [ ] **T3 后端回读**:写完立即 `GET /latest` → 8 列等于刚写值;agent_config 表无 row → 8 列全 null/[](`为空则显空`)。
- [ ] **T4 前端 C 态一键保存**:走完战略+策略 confirm → `cardP3Override` 可见 → `#codexSaveAllBtn` 出现。改任一字段 → 标签翻黄 → 点一键 → spinner → 全 5 标签翻绿 + toast 绿。
- [ ] **T5 ERP 单一真源**:`SELECT 8 列 FROM t_advert_agent_config WHERE parent_asin=?` → 8 列 `update_time` 同一时刻(atomic 证据),无 partial 列残影。
- [ ] **T6 state DB 同步**:`long_term_config`(策略 2 + budget_override)+ `target_acos_override.json` + `workflow_state.json.execution` 均已写。
- [ ] **T7 副作用完整**:一键成功 → `renderP3` 重渲(manual_override badge)、Tab2/3 解锁、`loadMainInsightPanels` 触发后 Tab4 解锁、反馈弹窗数据已捕获。
- [ ] **T8 回读对称(关键)**:刷新页面进 C 态 → 战略/策略下拉 + P3 input placeholder + 方向勾选 = agent_config 真源值(空则显空);一键保存后再刷新,值与保存值一致。
- [ ] **T9 快照态零改动**:进 B 态 → `renderReadonlyPreset` 行为不变,战略/策略 `_snapshotRow` + P3 `p3ReadonlyView` 只读大字 + `sec1Done`="快照" 文案不变。
- [ ] **T10 失败路径**:端点 500 → toast 红 + 标签保持黄;ACOS=200 → 前端 toast 红(轻校验)+ 后端 422(若绕过前端)。
- [ ] **T11 守门不漏**:`guardUnsavedBeforeRun` 在一键未保存时仍拦截 `运行执行层分析`。
- [ ] **T12 立即退出** `btnStrategy` 路径不变:选经营模式=立即退出 → 原确认弹窗 → `/decision/immediate-exit`,一键不参与。

---

## 5. 改动清单

| 文件 | 改动 |
|---|---|
| `app/models/layers.py` | 新增 `SaveAllConfigRequest`(继承 `ProductIdentityMixin`,5 列可选 + Field 校验)+ `ConfigSnapshotResponse` |
| **(改名已完成 2026-08-04)** `scripts/erp_db/2026-08-04-rename-advet-to-advert.sql`(新)+ `app/persistence/erp_writer/repository.py:223` + `app/data/decision_config_reader.py:56` + 2 测试 + 知识图谱文档 | `t_advet_agent_config → t_advert_agent_config`,代码 5 处机械改名 + 1 migrate 脚本,本地库 RENAME 验证通过、数据无损、mirror 测试 11 个全绿。生产待 RENAME |
| `app/api/long_term_config.py` | 新增 `POST /long-term-config/{asin}/save-all` + `GET /long-term-config/{asin}/latest` + `_build_config_snapshot` |
| `app/persistence/erp_writer/repository.py` | 加 `get_agent_config_row(identity)` 读方法;`upsert_agent_config` 零改动 |
| `demo/ad-asisitant-agent.html` | (a) 4 键 `hidden` class;(b) `#codexSaveAllBar/Btn` DOM;(c) 补丁 CSS 块贴进 `<style>`;(d) 新 JS `saveAllConfig`/`applySaveAllSideEffects`/`applyConfigSnapshotToEditable` + `_checked`/`_numOr`/`_lockSaveAllBtn`/`_showToast` + `applyPersistedTacticsSelection`/`applyPersistedDirectionSelection`;(e) `loadAll` 末尾加 `GET .../latest` 回读覆盖;(f) 4 个旧函数体保留 |
| `临时文件/codexvender/v2前端补丁/codex_ux_patch.js` | 不引入主看板,仅作 CSS 样式参考源 |

---

## 6. 风险与必须先做的前置核实

- **R1 — Agent 配置表存在性(已解除,2026-08-04 连本地库核验)**:`t_advert_agent_config` 存在且 DDL 完整,内有真实数据。详见 §1.3 末段核实记录。原"必须先核实"前置条件已闭环。
- **R2 — 表名 `advet` 缩写(已闭环)**:已借本方案落地一并改名 `t_advet_agent_config → t_advert_agent_config`(代码 5 处 + 1 migrate + 文档 3 处),本地库 RENAME 验证通过,测试 11 个全绿。生产部署顺序见 §1.3 末段。本方案下文不再重复提及改名,聚焦前端。
- **R3 — 回读覆盖时序**:C 态 `loadStrategy`/`loadTactics`/`loadExecution` 各自加载完后再调 `latest` 覆盖,需保证 `latest` 在最后执行。当前 `loadAll` 是 await 串行,简单加在末尾即可;若日后改为并行需用 Promise.all 兜底回读时序。
- **R4 — identity 在 `GET /latest` 完整性**:C 态加载时若 `shop_id`/`parent_seller_sku` 尚未解析完整(战略层 confirm 前),`latest` 应返空 snapshot,前端显空;不阻塞工作流。等 `confirmStrategy` 写完 state DB 后,后续 `loadAll` 回访 identity 完整,回读才有值——与现有"未配置即空"语义自然对齐。
- **R5 — 一键与原副作用顺序**:`confirmTactics` 原 `loadMainInsightPanels` 在保存后触发(拉 MCP/LLM 数据),本方案把它放 `applySaveAllSideEffects` 末段同样触发。若为真·return-visit 重复触发会被 `loadMainInsightPanels` 内部缓存挡住(待核,[L2062](AD_assistant_agent-v3.2/ad-direction-agent/demo/ad-asisitant-agent.html#L2062) 实际行为需实测)。命中则保留;若重复触发重拉数据耗时,加 `if (!diagnosisLoaded) await loadMainInsightPanels()` 守护。
- **R6 — `renderP3` 在快照态的不变式**:`renderP3` 在 `manual_override=false` 分支不读 DOM 输入(快照态安全,见 [L1248](AD_assistant_agent-v3.2/ad-direction-agent/demo/ad-asisitant-agent.html#L1248) 注释),`applySaveAllSideEffects` 只在 C 态调用,B 态 `renderReadonlyPreset` 走自己的 `renderP3(data.p3_recommend)` 分支——双路径不串。

---

## 7. 已定夺(2026-08-04)

1. **`applyConfigSnapshotToEditable` 调用点** = `loadAll` 末尾统一调一次。`loadStrategy/loadTactics/loadExecution` 各自加载不变,末尾 `GET .../latest` 回读覆盖真源值,空则显空。与"保存后回读"用同一套 `applyConfigSnapshotToEditable`,对称。三段加载会先渲染 AI 推荐/option,末尾被真源覆盖——接受一次"先闪 AI 值再变配置值"的视觉跳变,换取回读点单一。
2. **`loadMainInsightPanels` 重复触发守护** = 加 `if (!diagnosisLoaded)` 守护。落地时先读 `loadMainInsightPanels`/`diagnosisLoaded` 实际行为,若代码不支持守护则退回无条件触发(保原行为)并标注。
3. **空数组处理** = 单一配置不允许保存为空。前端接线现有"已保存/未保存"校验逻辑(`recomputeTacticsDirty`/`recomputeDirectionsDirty`/`recomputeNumDirty` + `setSaveTag`)到 `saveAllConfig` 前置校验,空即拦截不发起请求;后端 Pydantic `min_length=1` 双保险(空 list 直接 422)。
</content>
</invoke>
