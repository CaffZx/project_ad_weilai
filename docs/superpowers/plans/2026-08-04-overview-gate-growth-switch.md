# 策略总览门禁收紧新增/复评 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `subagent-driven-development` (recommended) or `executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让策略总览(执行总纲)LLM 在判定"今日不应当做增长分析(新增扩词/淘汰复评)"时,输出 `allow_growth_analysis=false`,收紧下游的"新增活动分析线"与"淘汰复评"两道增长流;默认 `true`(fail-open)。经营模式门禁保持不动,最终门禁 = 经营模式门禁 AND 总览门禁。

**Architecture:** 总览 LLM 在 `_run_overview_gate` 内跑完,boolean 落到 `CampaignStrategicOverview.allow_growth_analysis`。campaign.py 提前初始化 `ctx_dict`,提前创建 gate,并引入单一权威函数 `_resolve_growth_analysis_enabled()`:首次调用 `await overview_gate` 并求 `final = growth_analysis_enabled and overview_allow_growth_analysis`,后续调用 `await` 同一 task returns 同一结果(asyncio task 重复 await 安全),无需 `resolved` 标志或多套 holder 状态。新增流经一个统一包装 `_run_new_campaigns_if_enabled()`:`final` 为 False 时返回 `([], [], {})`,否则调 `analyze_new_campaigns`。淘汰复评直接读 `_resolve_growth_analysis_enabled()` 结果。两条业务路径(正常 / 全预过滤)对称使用同一组函数,只因原调度结构不同一个在 `gather` 中一个直接 `await`。

**Tech Stack:** Pydantic v2、Python asyncio、pytest。

---

## 已确认边界

- **作用范围**: 总览 `allow_growth_analysis=false` 同时收紧"新增活动分析线"(`analyze_new_campaigns`)与"淘汰复评"(`_maybe_restart_review`)。两者都是"增长流"。精准流/广泛流不受此布尔影响——它们是存量活动优化,非增长。
- **失败默认**: `fail-open = True`。总览 LLM 失败/超时/异常、`generated_by=fallback`、`settings.campaign_overview_enabled=False` 关闭时,`overview_allow_growth_analysis` 视为 `True`,不收紧。理由:LLM 一过激关掉增长流风险高于放跑几个候选词,与现有总览/各流 fail-open 风格一致。
- **类型严格**: 解析只接受 JSON boolean。LLM 输出 `"false"`(字符串)等非 bool 值一律按 `True` fail-open;缺键亦 `True`。
- **经营模式门禁不动**: [campaign.py:424-433](AD_assistant_agent-v3.2/ad-direction-agent/app/workflow/steps/campaign.py#L424-L433) 的 `growth_analysis_enabled`(经营模式派生)保持同步求值、原语义。最终门禁 `final = growth_analysis_enabled and overview_allow_growth_analysis` 在 `_resolve_growth_analysis_enabled()` 内求值。
- **字段命名**: 模型字段名 `allow_growth_analysis`(非 `allow_new_keywords`),与作用范围(新增+复评两道增长流)对齐,避免名实不符。
- **序列化传递**: `CampaignStrategicOverview.model_dump()` 会把 `allow_growth_analysis` 键带入 `strategic_overview` dict,经 [campaign.py:1182](AD_assistant_agent-v3.2/ad-direction-agent/app/workflow/steps/campaign.py#L1182) 进入 `CampaignAnalysisResult.strategic_overview`,并由 [mappers.py:637](AD_assistant_agent-v3.2/ad-direction-agent/app/persistence/erp_writer/mappers.py#L637) 的 ERP payload 继续传递。前端/ERP 按现状不消费该键即可;不刻意剥离,不新增前端契约。
- **正常路径不阻塞精准/广泛流**: 精准/广泛流的 prefetch/LLM 轮在三流 `gather` 内跑,total览 gate 的 `await` 只发生在新增流管道内部(`_run_new_campaigns_if_enabled`)和复评前。精准/广泛流本就在自身 LLM 轮前 `await gate`([campaign.py:1689](AD_assistant_agent-v3.2/ad-direction-agent/app/workflow/steps/campaign.py#L1689))——这是架构既定,不在本方案新增阻塞。新增流管道内 `await gate` 也本是其既有行为(取 `posture_brief`),封入包装后零额外阻塞。
- **不做的事**: 不改 `growth_analysis_enabled` 求值逻辑/语义;不改精准/广泛流 gating;不改 `_is_blocked_by_asin` 内 `product_stage=="清货期"` 阻断(独立硬过滤);不动 `campaign_overview_enabled` / `campaign_new_enabled` / `campaign_restart_enabled` 三个配置开关。

## 已知事实 (无需再核实)

- **F1 立即退出权限**: `OperatingMode.IMMEDIATE_EXIT → AdPermission.STOP`([layers.py:144](AD_assistant_agent-v3.2/ad-direction-agent/app/models/layers.py#L144))。单看 `analyze_campaigns`,`growth_analysis_enabled = (STOP != CLEARANCE_ONLY) = True`——即立即退出经营模式下 `analyze_campaigns` 函数本身不会因经营模式门禁关掉增长流。**主链路安全**因 [api/campaign.py:408](AD_assistant_agent-v3.2/ad-direction-agent/app/api/campaign.py#L408) 在 API 层就直接拒绝立即退出发起 Campaign 分析(`"经营模式为"立即退出"，拒绝发起 Campaign 分析"`),根本到不了 `analyze_campaigns`。本方案不改这条守护链,但记录此事实,避免后人误以为经营模式门禁已覆盖立即退出。
- **F2 ctx_dict 初始化**: [campaign.py:604](AD_assistant_agent-v3.2/ad-direction-agent/app/workflow/steps/campaign.py#L604) `ctx_dict = strategy_context.model_dump()`。gate 内 [campaign.py:622](AD_assistant_agent-v3.2/ad-direction-agent/app/workflow/steps/campaign.py#L622) `_run_overview(reasoner, parent_asin, _ov_facts, ctx_dict, ...)` 闭包读 `ctx_dict`。全预过滤分支在 L565 return,**改前到不了 L604**(gate 也不起);改后要让全预过滤路径起 gate,**必须把 `ctx_dict` 提前到 gate 创建之前**,否则 task 在 `await asyncio.to_thread`(复评内)处让出循环时,gate 会读未赋值的 `ctx_dict` → `NameError`。
- **F3 asyncio task 重复 await**: 已完成的 asyncio task 重复 `await` 直接返回结果、不重复执行 Python 层 gate 逻辑。故 `_resolve_growth_analysis_enabled()` 多次调用安全,无需 `resolved` 标志。
- **F4 真正的串行化来源是 `await`,不是 `create_task`**: `asyncio.create_task` 立即 sched 后台跑,不阻塞。阻塞只发生在 `await overview_gate` 处。本方案把 `await` 仅放在新增流管道内部和复评前,精准/广泛流 prefetch 不受影响。

---

## 当前时序 (基线,改前)

按 [campaign.py](AD_assistant_agent-v3.2/ad-direction-agent/app/workflow/steps/campaign.py) 执行顺序:

1. **L424-433** 经营模式门禁同步求值 `growth_analysis_enabled`。
2. **L484-511** `_maybe_restart_review` 定义(内部 L487 读 `growth_analysis_enabled` 做门禁)。
3. **L515-565** 全预过滤分支(total==0):
   - L521 `await _maybe_restart_review()` —— 复评此处跑。
   - L528 `if settings.campaign_new_enabled and growth_analysis_enabled` —— 新增流此处跑。
4. **L570+** 正常路径(total>0):portfolio、精准/广泛两流预取……
5. **L604** `ctx_dict = strategy_context.model_dump()`。
6. **L617-633** `overview_gate = asyncio.create_task(_run_overview_gate())` —— gate 起跑,与下面三流 prefetch 重叠。
7. **L643-677** 三流 gather:
   - 精准/广泛流在各自 LLM 轮前 `await overview_gate`(L1689)。
   - 新增流在 [campaign_new.py:475-476](AD_assistant_agent-v3.2/ad-direction-agent/app/workflow/steps/campaign_new.py#L475-L476) LLM 轮前 `await overview_gate`。
   - L675 `... if settings.campaign_new_enabled and growth_analysis_enabled else _no_op_new_campaigns()` —— **新增流启停在 gather 前同步定,用第 1 步布尔,不读总览**。
8. **L679-685** gate 兜底 `await overview_gate` + 取 `_ov_holder["overview"]`。
9. **L1025** 正常路径复评 `await _maybe_restart_review()` —— **不 await gate**,读同步的 `growth_analysis_enabled`。

**关键卡点**:复评的两个调用点(L521、L1025)都拿不到总览布尔——L521 在 gate 创建(L617)之前;L1025 不 await gate。F2:全预过滤分支连 `ctx_dict` 都没初始化(L604 在它 return 之后)。

---

## 目标时序 (改后)

1. **L424-433** `growth_analysis_enabled`(经营模式)同步求值 —— 不变。
2. **新增:ctx_dict 提前**到 gate 创建之前(F2 强制)。
3. **新增:gate 创建提前**到 original L617 块的位置不变也可——但为让全预过滤分支能用,需保证 gate 在 L484 之前已创建。具体落点:在 `growth_analysis_enabled` 求值后、`_maybe_restart_review` 定义前,先 `ctx_dict = strategy_context.model_dump()`,再 `if settings.campaign_overview_enabled: overview_gate = create_task(_run_overview_gate())`。
4. **新增:定义 `_resolve_growth_analysis_enabled()`** —— 单一权威 final 求值函数(见改动 5)。`_maybe_restart_review` 内部调它。
5. **新增:定义 `_run_new_campaigns_if_enabled()`** —— 统一新增流包装(见改动 5)。
6. **全预过滤分支 L515-565**:
   - L521 复评 → `_maybe_restart_review()` 内部按 final 门禁(它调 `_resolve_growth_analysis_enabled()`)。
   - L528 新增流 → `await _run_new_campaigns_if_enabled(...)`(包装内 `await _resolve_growth_analysis_enabled()` → False 则返回空)。
7. **正常路径**:
   - L675 三流 gather 中新增流那一支 → `_run_new_campaigns_if_enabled(...)`(包装内 await gate + 判 final,精准/广泛流照常并发)。
   - L1025 复评 → `_maybe_restart_review()`(读同一 final)。
8. **L679-685 gate 兜底 await** 不变(已 await 过 = no-op,F3)。

> **时序对比**: 正常路径下,精准/广泛流在三流 gather 内启动,prefetch 与 gate 重叠(本就如此);新增流进入 `_run_new_campaigns_if_enabled` 后 `await _resolve_growth_analysis_enabled()` 才 `await gate`(新增流本就要 await gate 拿 posture_brief,零额外阻塞)。全预过滤分支下,gate 在 L521 之前被 `await`(经 `_resolve_growth_analysis_enabled`),复评/新增流读 final;此分支边缘,gate 串行一次可接受。

---

## 文件改动清单

### 改动 1: 模型层加字段 — [campaign.py:280](AD_assistant_agent-v3.2/ad-direction-agent/app/models/campaign.py#L280) `CampaignStrategicOverview`

- [ ] 加字段:
  ```python
  allow_growth_analysis: bool = True
  ```
  命名 `allow_growth_analysis`(非 `allow_new_keywords`),作用范围对齐"新增 + 复评"两道增长流。默认 `True` 对齐 fail-open。`model_dump()` 会带入 `strategic_overview` dict,经 ERP payload 传递(F1 之外,前端/ERP 按现状不消费该键)。

### 改动 2: LLM 输出层 — [reasoner.py:2035](AD_assistant_agent-v3.2/ad-direction-agent/app/llm/reasoner.py#L2035) `recommend_campaign_overview`

- [ ] prompt 加输出契约:在 [reasoner.py:510](AD_assistant_agent-v3.2/ad-direction-agent/app/llm/reasoner.py#L510) 起的 `_build_campaign_overview_prompt` 里,JSON schema 增键:
  ```
  "allow_growth_analysis": false   // 仅当明确判断"今日不应当做增长分析(新增扩词/淘汰复评)"才输出 false
  ```
- [ ] prompt 加判定准则段(指令式):
  - 当**明确不应当做增长分析**时输出 `false`:库存可售天数低、退货率/评分触红线、经营模式已是清货优先、ACOS 危机未解、预算吃紧、需收缩而非扩张的运行态等。
  - 当**应当/不确定**时输出 `true` 或不出现该键:旺季准备、词池机会、淘汰补位、量价齐升、扩词有助于补位/引流等扩张场景。
  - 准则由 LLM 自洽:复用已注入的经营模式/库存/退货率/评分/旺季/目标 ACOS 等上下文,不在 Python 里加硬规则。
- [ ] 解析处 [reasoner.py:2102-2106](AD_assistant_agent-v3.2/ad-direction-agent/app/llm/reasoner.py#L2102-L2106) **严格类型判**(关键):
  ```python
  raw_flag = parsed.get("allow_growth_analysis", True)
  out["allow_growth_analysis"] = raw_flag if isinstance(raw_flag, bool) else True
  ```
  即:只接受 JSON `true`/`false`;字符串 `"false"`、数字、缺失一律 `True`(fail-open)。
- [ ] LLM 失败分支 [reasoner.py:2109-2111](AD_assistant_agent-v3.2/ad-direction-agent/app/llm/reasoner.py#L2109-L2111) 返回 `{"error": ...}` 不含此键 → 调用方默认 True。

### 改动 3: gate 落地 — [campaign.py:620-633](AD_assistant_agent-v3.2/ad-direction-agent/app/workflow/steps/campaign.py#L620-L633) `_run_overview_gate`

- [ ] gate 内 `obj.allow_growth_analysis` 随 `obj.model_dump()` 自然进 `_ov_holder["overview"]`,无需单独存键。
- [ ] 失败分支(`except Exception`)不写 holder → `_resolve_growth_analysis_enabled` 读不到 overview 时按 `True` fail-open(见改动 5)。

### 改动 4: `_run_overview` fail-open 核实 — [campaign.py:1608](AD_assistant_agent-v3.2/ad-direction-agent/app/workflow/steps/campaign.py#L1608)

- [ ] 读 [campaign.py:1608-1640](AD_assistant_agent-v3.2/ad-direction-agent/app/workflow/steps/campaign.py#L1608-L1640) 区域,确认 `recommend_campaign_overview` 返回 `{"error": ...}` 时构造的 fallback `CampaignStrategicOverview` 走 `generated_by="fallback"`,`allow_growth_analysis` 默认 `True` 由 Pydantic 兜底。无需改代码,只需核实并在此条目记一行"已核实"。

### 改动 5: campaign.py 主流程 — ctx_dict 提前 + gate 提前 + final 求值函数 + 新增流包装 (核心改动)

- [ ] **5a. ctx_dict 提前**:把 [campaign.py:604](AD_assistant_agent-v3.2/ad-direction-agent/app/workflow/steps/campaign.py#L604) `ctx_dict = strategy_context.model_dump()` 移到 `growth_analysis_enabled` 求值(L433)之后、`_maybe_restart_review` 定义(L484)之前。**F2 强制,不可漏**。
- [ ] **5b. gate 创建提前**:把 [campaign.py:617-633](AD_assistant_agent-v3.2/ad-direction-agent/app/workflow/steps/campaign.py#L617-L633) 的 `if settings.campaign_overview_enabled:` 整块(含 `_ov_facts`、`_run_overview_gate`、`create_task`)上移到 5a 之后、L484 之前。`_ov_holder` / `overview_gate` 变量在更早位置初始化为 `{}` / `None`。`strategic_overview` 取值时机不变(仍在 L679 兜底处取 `_ov_holder["overview"]`)。
- [ ] **5c. 定义 `_resolve_growth_analysis_enabled()`**:
  ```python
  async def _resolve_growth_analysis_enabled() -> bool:
      """最终增长门禁 = 经营模式门禁 AND 总览门禁。
      首次调用 await overview_gate 求总览布尔;后续调用 await 同一 task returns 同一结果(F3)。
      fail-open: gate 未起 / 失败 / 缺键 / 类型错 / fallback → True。
      """
      if not growth_analysis_enabled:           # 经营模式门禁先短路
          return False
      if overview_gate is None:                  # 总览未启用 → 仅经营模式
          return True
      try:
          await overview_gate                    # 已完成则 no-op (F3)
      except Exception:
          pass                                   # gate 内已吞异常并 warning
      ov = _ov_holder.get("overview")
      if not isinstance(ov, dict):
          return True                            # gate 失败 → fail-open
      flag = ov.get("allow_growth_analysis", True)
      return flag if isinstance(flag, bool) else True
  ```
  不用 `nonlocal`,不写 holder 多份字段,不引入 `resolved` 标志——单一权威,单一求值(每次 await 同一 task 同一结果)。
- [ ] **5d. 定义 `_run_new_campaigns_if_enabled()`**:
  ```python
  async def _run_new_campaigns_if_enabled(...) -> tuple[list, list, dict]:
      """新增流统一包装:final 门禁 False 时返回空,否则调 analyze_new_campaigns。
      正常路径进三流 gather,全预过滤路径直接 await —— 两路径业务语义对称。"""
      if not await _resolve_growth_analysis_enabled():
          return [], [], {}
      return await analyze_new_campaigns(
          fetcher=fetcher, reasoner=reasoner, parent_asin=parent_asin,
          shop_id=campaign_data.shop_id,
          parent_seller_sku=campaign_data.parent_seller_sku,
          site_code=campaign_data.site_code,
          shop_account=shop_account,
          existing_keywords=existing_kws,
          pre_eliminated_count=pre_eliminated_count,
          strategy_context=strategy_context,
          ctx_dict=ctx_dict,
          temperature=temperature,
          target_child_asin=target_child_asin,
          days=days,
          sem=asyncio.Semaphore(cc),
          product_title=(asin_data.title or "") if asin_data else "",
          cancel_check=cancel_check,
      )
  ```
  省略号参数按现有调用点对齐(`analyze_new_campaigns` 完整签名见 [campaign_new.py:264](AD_assistant_agent-v3.2/ad-direction-agent/app/workflow/steps/campaign_new.py#L264));正常 / 全预过滤两路径共用同一包装,差异只在调度(进 gather 还是直接 await)。
- [ ] **5e. `_maybe_restart_review` 内门禁换 final**:[campaign.py:487](AD_assistant_agent-v3.2/ad-direction-agent/app/workflow/steps/campaign.py#L487) 把 `growth_analysis_enabled` 改为调 `_resolve_growth_analysis_enabled()`:
  ```python
  if not (await _resolve_growth_analysis_enabled()
          and settings.campaign_restart_enabled and pool_units):
      return [], set()
  ```
  (改 async 内部 await,函数本就是 async,无新增开销。)
- [ ] **5f. 全预过滤分支新增流换包装**:[campaign.py:528-548](AD_assistant_agent-v3.2/ad-direction-agent/app/workflow/steps/campaign.py#L528-L548) 整块 `if settings.campaign_new_enabled and growth_analysis_enabled: try: analyze_new_campaigns(...)` 换为单行:
  ```python
  new_campaigns, nc_warnings, _ = await _run_new_campaigns_if_enabled(...)
  ```
  (`settings.campaign_new_enabled` 与经营模式/总览门禁一并由包装内部 `_resolve_growth_analysis_enabled` + `analyze_new_campaigns` 既有内部检查承担;若需保留 `campaign_new_enabled` 外层硬开关,在包装内首句加 `if not settings.campaign_new_enabled: return [], [], {}`。)
- [ ] **5g. 正常路径新增流换包装**:[campaign.py:656-675](AD_assistant_agent-v3.2/ad-direction-agent/app/workflow/steps/campaign.py#L656-L675) 三流 gather 中新增流那支:
  ```python
  _run_new_campaigns_if_enabled(...)
  # 替换原: analyze_new_campaigns(...) if settings.campaign_new_enabled and growth_analysis_enabled else _no_op_new_campaigns()
  ```
  精准流 / 广泛流两支不变;`_no_op_new_campaigns` 可保留(总览/经营模式门禁在包装内判,False 返回空)或删除(若包装已覆盖)。删则少一个无用 helper。
- [ ] **5h. 正常路径复评读 final**:[campaign.py:1025](AD_assistant_agent-v3.2/ad-direction-agent/app/workflow/steps/campaign.py#L1025) `await _maybe_restart_review()` 不变调用,但内部已改为读 final(5e)。

### 改动 6: 注释/文档同步

- [ ] [campaign.py:424-433](AD_assistant_agent-v3.2/ad-direction-agent/app/workflow/steps/campaign.py#L424-L433) 经营模式门禁处补注释:指明这是"经营模式那一支",`_resolve_growth_analysis_enabled()` 才是下游用的最终门禁(还需 await 总览 gate)。
- [ ] [campaign.py:611-613](AD_assistant_agent-v3.2/ad-direction-agent/app/workflow/steps/campaign.py#L611-L613) 原 gate 注释更新:gate 现在用于 `_resolve_growth_analysis_enabled`,不再仅是 posture_brief 注入;位置已提前到经营模式门禁之后。
- [ ] [campaign.py:487](AD_assistant_agent-v3.2/ad-direction-agent/app/workflow/steps/campaign.py#L487) 注释:复评门禁现受经营模式 + 总览两路约束。
- [ ] 交接文档 `交接文档.md` 对应章节(若有"策略总览"/"新增/复评门禁"小节)补一行;无则不动,不做范围外修改。

---

## 测试计划

### 单元测试

- [ ] **T1 模型默认**:`CampaignStrategicOverview()` 默认 `allow_growth_analysis is True`;`model_dump()` 含该键且值为 `True`。
- [ ] **T2 reasoner 严格类型**:
  - mock LLM 返回 `{"allow_growth_analysis": false, ...}` → 输出键为 `False`。
  - 返回 `{"allow_growth_analysis": "false"}`(字符串) → 输出 `True`(fail-open)。
  - 返回缺键 → `True`。
  - 返回 `{"allow_growth_analysis": 0}` → `True`(非 bool)。
- [ ] **T3 _run_overview fail-open**:LLM 抛异常 → fallback overview `allow_growth_analysis is True`。

### 集成测试(campaign.py 主流程)

- [ ] **T4 经营模式清货优先**:无论总览输出什么,`_resolve_growth_analysis_enabled()` 短路返回 False(经营模式那一支),新增流与复评都不跑。
- [ ] **T5 总览 false + 经营模式非清货**:final=False → `_run_new_campaigns_if_enabled` 返回空;复评跳过;`strategic_overview` 落库含 `allow_growth_analysis=False`。
- [ ] **T6 总览 true + 经营模式非清货**:final=True → 新增流与复评按现状跑。
- [ ] **T7 总览 LLM 超时**:gate 抛超时 → `_resolve_growth_analysis_enabled` 读不到 overview dict → True(fail-open)→ 行为与改前一致。
- [ ] **T8 总览关闭**:`settings.campaign_overview_enabled=False` → `overview_gate=None` → final = `growth_analysis_enabled`(经营模式那一支)→ 与改前一致。
- [ ] **T9 正常路径不阻塞精准/广泛**:用 monkeypatch 让总览 LLM 延迟(如 2s),断言三流 gather 启动后精准/广泛流先于 gate 完成开始 prefetch;新增流进入包装后才 await gate。可用 `asyncio` 钩子或时间断言。
- [ ] **T10 全预过滤路径**:total==0 时,gate 在 L521 之前 await 到位(用 monkeypatch 让 LLM 返回 false),断言复评与新增流均读 final=False,restart 跳过,新增流返回空。
- [ ] **T11 `_resolve_growth_analysis_enabled` 幂等**:多次调用返回同一值,`overview_gate` task 不重复执行(用 monkeypatch 计数 `_run_overview_gate` 体内执行次数)。

### 回归

- [ ] **R1 精准/广泛流**:总览布尔不进这两个流的 gating,行为不变;`posture_brief` 仍正确注入。
- [ ] **R2 strategic_overview 序列化**:前端/ERP payload 拿到含 `allow_growth_analysis` 键的 `strategic_overview`,按现状不消费;无 schema 错误。
- [ ] **R3 既有测试保绿**:`test_campaign_new_quota.py`、`test_campaign_cache_context.py`、`test_campaign_cancellation_fencing.py` 若 mock 了 `recommend_campaign_overview` 或 `analyze_new_campaigns`,补 `allow_growth_analysis=True` 默认返回以保绿;mock `_run_overview` 时也补。

---

## 风险与缓解

- **R-全预过滤路径 gate 串行**:改前全预过滤分支不起 gate;改后此分支也要等一次总览 LLM(最多 180s 超时)。**缓解**:fail-open(failure → True 仅 warning,不阻塞);此分支边缘(全部活动被预过滤),可接受。若发现长尾,可把总览 `timeout_override` 在此分支调小(已有机制)。
- **R-LLM 误判收紧**:LLM 一过激输出 `false` 会关掉本该跑的增长流。**缓解**:fail-open + prompt 准则"不确定→true";运营可临时把 `campaign_overview_enabled=False` 回到改前行为。
- **R-立即退出 F1**:经营模式门禁不覆盖立即退出(`STOP != CLEARANCE_ONLY` → True),API 层守护([api/campaign.py:408](AD_assistant_agent-v3.2/ad-direction-agent/app/api/campaign.py#L408))是当前唯一防线。**缓解**:本方案不改此守护链;但记录 F1 事实,后人若动 API 层守护需注意经营模式门禁不会代偿。可考虑(本期不做)在 `growth_analysis_enabled` 求值处加 `ad_permission != AdPermission.STOP` 一支,作为纵深防御——超出本方案范围,留待新工单。
- **R-回归 existing tests**:多个集成测试 mock overview 返回值,新键可能让 `assert` 失配。**缓解**:T3/R3 已覆盖;改前先跑现有 campaign 测试集记绿基线,改后再跑对比。

---

## 实现顺序建议

1. 先跑现有 campaign 测试集,记录绿基线。
2. 改动 1 + 改动 2(模型字段 + LLM 输出 + 严格类型),跑 T1-T3。
3. 改动 3 + 改动 4(gate 落地 + `_run_overview` fail-open 核实),跑 T3。
4. 改动 5(主流程核心:ctx_dict 提前 → gate 提前 → `_resolve_growth_analysis_enabled` → `_run_new_campaigns_if_enabled` → 四处消费点),跑 T4-T11 + R1-R3。
5. 改动 6(注释/文档),收尾。

每步独立 commit,便于回滚。