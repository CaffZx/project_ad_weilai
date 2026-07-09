# Campaign 护栏体系 + Codex 异步化 实施方案

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**目标：** 构建三层护栏体系（业务逻辑护栏 + Agent 行为护栏 + Codex 输出护栏），并将 Codex 复核从同步阻塞改为异步，消除效率瓶颈。

**架构：** 在现有 `_resolve_budget_conflicts` 基础上扩展为完整的三层护栏。Layer 0 在分析过程中做确定性规则修正（核心词保护/强制淘汰/预算上限），Layer 1 在基线 LLM 输出后做结构化校验（数量审计/字段完整性/语义自洽），Layer 2 在 Codex 合入后做二次校验。Codex 改为后台异步执行——基线分析完成后 result 对象保留在内存中，HTTP 立即返回，codex 复核在同一个进程内以 `asyncio.create_task` 后台执行，完成后通过 merge.py 对受影响的 card 做增量 UPDATE，而非全量重写。

**技术栈：** Python 3.11, Pydantic, asyncio, 现有 campaign.py 分析管线

**现状：** 已有部分护栏逻辑散落在 `_resolve_budget_conflicts`（KB21§2 新活动保护、淘汰值修正）和 `merge.py`（action=keep 语义自洽），但不成体系。已有 Tab1-Tab4 校验引擎（`validation_engine.py`）但针对策略层，不适用于 Campaign 活动级。

---

## 关键设计决策

### Codex 绝不碰数据库

Codex CLI 是一个纯文本输入 → JSON 输出的黑盒。它的 stdin 接收 prompt，stdout 返回 JSON 字符串。它只有修正 tool、没有 DB 连接、没有 MCP 权限。整个链路中：

```
codex CLI (纯文本→JSON)
  │ stdin 喂 prompt
  │ stdout 收 JSON
  ▼
Python (我们的 agent)
  │ merge.apply_review() 修改内存中的 result.adjustments
  │ repo.update_suggest_card() 单条 UPDATE 被改的 card
  ▼
MySQL
```

**给 codex 配置任何 DB tool 是绝对不能做的事**——一旦它有写权限，一次幻觉就可能毁掉整批数据。

### 异步化的实际实现：内存直改 + 增量 UPDATE

之前讨论的"Worker 从 DB 读 snapshot → 重建 result → 跑 codex → 写回"是过度设计。实际方案更简单：

**关键认知：codex 执行时，result 对象还在 8012 进程内存中。不需要从 DB 读回。**

```python
# 现在（同步）：HTTP 请求必须等 codex 跑完
result = await _do_analyze(...)           # 基线
outcome = await maybe_review(result, ...)  # ← 卡 3 分钟
erp = await _maybe_push_erp(result, ...)   # 落库
return response

# 改后（异步）：基线落库即返回，codex 后台补
result = await _do_analyze(...)           # 基线
erp = await _maybe_push_erp(result, ...)   # 基线立即落库
asyncio.create_task(                       # 后台 fire-and-forget
    _review_and_patch(result, erp["decision_id"], asin, target_acos)
)
return response                             # ← 立即返回，不等 codex

async def _review_and_patch(result, decision_id, asin, target_acos):
    outcome = await maybe_review(result, ...)   # 内存中的 result 直接改
    if outcome.reviewed:
        # 只 UPDATE 被 codex 修改过的 card（review_level='AI_REVIEWED'）
        for item in result.adjustments:
            if item.review_level == "AI_REVIEWED":
                repo.update_suggest_card(item, decision_id)
```

### 增量 UPDATE 由 merge.py 的返回值控制

`merge.py` 的 `apply_review()` 已经精确知道改了多少、删了多少：

```python
# merge.py apply_review() 返回值
stats = {
    "total": 100,     "kept": 55,
    "modified": 13,   "dropped": 0,   "uncovered": 0,
    "summary": "本批复核修改13条..."
}
```

被 codex 修改的 item 上已有标记——`review_level = "AI_REVIEWED"`。增量 UPDATE 就是遍历 `result.adjustments`，只对带这个标记的 card 做单条更新。

### 不会频繁读写库

| | 同步（现状） | 异步（改后） |
|---|---|---|
| DB 写 | 1 次全量（decision + card + pending + summary...） | 同上 + **1 次增量**（只 UPDATE 被 codex 改过的 card，每条一条 SQL） |
| DB 读 | 0 | **0**（result 在内存，不读 DB） |
| 瓶颈 | codex 占着 worker 3 分钟 | codex 不占 worker，worker 立即释放 |

616 个 ASIN × codex 平均改 ~5 条 card = 每批 ~3000 条 UPDATE。MySQL 执行这个级别的 UPDATE 是毫秒级的，相对 codex 3 分钟的延迟可以忽略。

**真正的风险不是 DB 压力，而是 8012 重启会丢失后台 task。** 批跑场景下 8012 不会重启，问题不大。如需兜底，可在 cron 批跑结束后补跑一个扫尾脚本，检查 `review_level != 'AI_REVIEWED'` 的遗漏 card。

---

## 文件结构

```
app/workflow/steps/
├── campaign.py              # [修改] 接入 Layer 0 + Layer 1，新增 _review_and_patch 后台任务
├── campaign_guardrails.py   # [新建] Layer 0 确定性规则引擎
└── campaign_validation.py   # [新建] Layer 1/2 输出校验器

app/persistence/erp_writer/
└── repository.py            # [修改] 新增 update_suggest_card 单条更新方法

AD_Agent_codexV2/
├── review_hook.py           # [修改] 新增 maybe_review_async 入口
├── merge.py                 # [修改] 接入 Layer 2 校验
├── codex_client.py          # [修改] 增加耗时日志

prompts/
└── campaign-review.md       # [修改] 增加护栏提示 + 输出格式硬约束
```

---

## Phase 1: Layer 0 — 确定性业务护栏（无 LLM，纯 Python）

### Task 1: 新建 `campaign_guardrails.py`

**涉及文件：**
- 新建: `app/workflow/steps/campaign_guardrails.py`
- 测试: `tests/workflow/test_campaign_guardrails.py`

将所有"不需要 AI 判断"的硬规则集中到此模块。规则按优先级执行，后执行的规则看到的是前序规则已修正后的值。

```python
"""Campaign 确定性护栏 —— 不需要 LLM 判断的硬规则，纯 Python 逻辑。

规则优先级（数字越小越先执行，P0-P2 先统一筛出受保护活动，P3-P6 再对剩余活动裁决）：
  P0: 核心词禁淘汰 (is_core=True → action 不得为 eliminate)
  P1: 新活动保护 (days_online ≤ 3 → 禁止淘汰)
  P2: 复评抖动保护 (days_since_reactivation ≤ 3 → 禁止淘汰)
  P3: 硬淘汰触发 (current_bid ≤ $0.21 或 current_budget ≤ $1.01 → 强制淘汰)
  P4: 预算花不完禁加 (spend_7d < budget × 0.5 → cap proposed_budget ≤ current)
  P5: 单次调整幅度上限 (Bid 变动 > 50% 且 clicks < 10 → 收敛到 30%)
  P6: 测试期保护 (product_stage=测试期 + days_online < 14 → 禁止淘汰)
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.campaign import CampaignAdjustmentItem


@dataclass
class GuardrailResult:
    """单条规则的执行结果"""
    rule_id: str          # 规则编号，如 "P0_CORE_PROTECT"
    corrected: bool       # 是否实际做了修正
    message: str          # 运营可读说明
    original_action: str = ""
    new_action: str = ""


@dataclass
class GuardrailPass:
    """一次护栏执行的完整结果集"""
    results: list[GuardrailResult] = field(default_factory=list)
    corrections: int = 0

    def add(self, result: GuardrailResult) -> None:
        self.results.append(result)
        if result.corrected:
            self.corrections += 1


def apply_all(
    adjustments: list,
    *,
    product_stage: str = "",
) -> GuardrailPass:
    """对所有 adjustments 按优先级依次执行所有护栏规则。

    每个规则都可以修改 item.action/proposed_bid/proposed_budget。
    后执行的规则看到的是前序规则已修正后的值。
    """
    gp = GuardrailPass()

    # 第一轮：保护类（禁止淘汰）
    for item in adjustments:
        _p0_core_protect(item, gp)
        _p1_new_campaign_protect(item, gp)
        _p2_reactivation_protect(item, gp)

    # 第二轮：裁决类（强制淘汰 / 预算上限 / 幅度收敛）
    for item in adjustments:
        _p3_force_eliminate(item, gp)
        _p4_budget_cap_when_low_spend(item, gp)
        _p5_bid_amplitude_cap(item, gp)
        _p6_testing_stage_protect(item, gp, product_stage)

    return gp


def _p0_core_protect(item, gp: GuardrailPass) -> None:
    """核心词禁止淘汰。

    is_core=True 的活动是产品的核心关键词，淘汰会导致自然排名下跌。
    如果 LLM 误判为 eliminate → 强制修正为 keep。
    """
    if not (item.is_core and item.action == "eliminate_to_low_bid_pool"):
        return
    original = item.action
    item.action = "keep"
    item.proposed_budget = item.current_budget
    item.proposed_bid = item.current_bid
    item.direction = {}
    item.placement_adjustments = []
    item.negative_keywords = []
    gp.add(GuardrailResult(
        rule_id="P0_CORE_PROTECT",
        corrected=True,
        original_action=original,
        new_action="keep",
        message=f"[{item.campaign_name}] 核心词受保护，已强制修正为 keep",
    ))


def _p1_new_campaign_protect(item, gp: GuardrailPass) -> None:
    """新活动保护：上线 ≤3 天的活动禁止淘汰。

    样本不足时 LLM 容易误判淘汰，必须硬保护。
    """
    days = item.days_online
    if not (days >= 0 and days <= 3 and item.action == "eliminate_to_low_bid_pool"):
        return
    item.action = "keep"
    item.proposed_budget = item.current_budget
    item.proposed_bid = item.current_bid
    item.direction = {}
    item.placement_adjustments = []
    item.negative_keywords = []
    gp.add(GuardrailResult(
        rule_id="P1_NEW_CAMPAIGN_PROTECT",
        corrected=True,
        original_action="eliminate_to_low_bid_pool",
        new_action="keep",
        message=(f"[{item.campaign_name}] 上线仅 {days} 天样本不足（KB21§2），"
                 "已强制修正为 keep"),
    ))


def _p2_reactivation_protect(item, gp: GuardrailPass) -> None:
    """复评抖动保护：最近 3 天内被复评捞回的活动禁止再次淘汰。

    防止淘汰→复评→淘汰的死循环。
    """
    days = item.days_since_reactivation
    if not (days >= 0 and days <= 3 and item.action == "eliminate_to_low_bid_pool"):
        return
    item.action = "keep"
    item.proposed_budget = item.current_budget
    item.proposed_bid = item.current_bid
    item.direction = {}
    item.placement_adjustments = []
    item.negative_keywords = []
    gp.add(GuardrailResult(
        rule_id="P2_REACTIVATION_PROTECT",
        corrected=True,
        original_action="eliminate_to_low_bid_pool",
        new_action="keep",
        message=f"[{item.campaign_name}] 复评后仅 {days} 天，等同新活动保护，已修正为 keep",
    ))


def _p3_force_eliminate(item, gp: GuardrailPass) -> None:
    """硬淘汰触发：当前 bid ≤ $0.21 或 budget ≤ $1.01 的活动必须淘汰。

    已经在池底的没有优化空间，不用再走调整诊断。
    但受 P0/P1/P2 保护的活动不触发（已在前序规则中强制修正为 keep，
    此处 action 已不再是 eliminate，会自然跳过）。
    """
    if item.action == "eliminate_to_low_bid_pool":
        return  # 已经是淘汰，无需再触发

    should_eliminate = (
        (item.current_bid is not None and item.current_bid <= 0.21)
        or (item.current_budget is not None and item.current_budget <= 1.01)
    )
    if not should_eliminate:
        return

    item.action = "eliminate_to_low_bid_pool"
    item.proposed_budget = 1.0
    item.proposed_bid = 0.20
    item.placement_adjustments = []
    item.negative_keywords = []
    gp.add(GuardrailResult(
        rule_id="P3_FORCE_ELIMINATE",
        corrected=True,
        message=(f"[{item.campaign_name}] bid=${item.current_bid}/"
                 f"budget=${item.current_budget} 已达淘汰阈值，强制淘汰"),
    ))


def _p4_budget_cap_when_low_spend(item, gp: GuardrailPass) -> None:
    """预算花不完禁止加预算。

    如果 7 天花费 < 当前日预算 × 0.5（即预算利用率 < 50%），
    说明当前预算都花不完，加预算没有意义。
    """
    if item.action == "eliminate_to_low_bid_pool":
        return
    if item.proposed_budget is None or item.current_budget is None:
        return
    if item.proposed_budget <= item.current_budget:
        return  # 没在加预算

    perf = item.perf_7d or {}
    spend = float(perf.get("cost") or perf.get("spend") or 0)
    if spend >= item.current_budget * 0.5:
        return  # 花费超过预算的 50%，加预算合理

    item.proposed_budget = item.current_budget
    gp.add(GuardrailResult(
        rule_id="P4_BUDGET_LOW_SPEND_CAP",
        corrected=True,
        message=(f"[{item.campaign_name}] 7d 花费 ${spend:.1f} < "
                 f"日预算 ${item.current_budget:.0f} × 50%，禁加预算"),
    ))


def _p5_bid_amplitude_cap(item, gp: GuardrailPass) -> None:
    """单次 Bid 调整幅度上限。

    Bid 变动 > 50% 且 clicks < 10（样本不足）→ 收敛到 30%。
    """
    if item.action == "eliminate_to_low_bid_pool":
        return
    if item.proposed_bid is None or item.current_bid is None:
        return
    if item.current_bid == 0:
        return

    change = abs(item.proposed_bid - item.current_bid) / item.current_bid
    if change <= 0.5:
        return

    perf = item.perf_7d or {}
    clicks = int(perf.get("clicks") or 0)
    if clicks >= 10:
        return  # 样本足够，允许大幅调整

    # 收敛到 30% 变动
    direction = 1 if item.proposed_bid > item.current_bid else -1
    capped = round(item.current_bid * (1 + direction * 0.30), 2)
    original = item.proposed_bid
    item.proposed_bid = max(0.21, capped)  # 不低于淘汰阈值
    gp.add(GuardrailResult(
        rule_id="P5_BID_AMPLITUDE_CAP",
        corrected=True,
        message=(f"[{item.campaign_name}] Bid 变动 {change:.0%} > 50% 且 clicks={clicks} < 10，"
                 f"已收敛: ${original:.2f} → ${item.proposed_bid:.2f}"),
    ))


def _p6_testing_stage_protect(item, gp: GuardrailPass, product_stage: str) -> None:
    """测试期保护：product_stage=测试期 且上线 <14 天 → 禁止淘汰。"""
    if product_stage != "测试期":
        return
    days = item.days_online
    if not (days >= 0 and days < 14 and item.action == "eliminate_to_low_bid_pool"):
        return
    item.action = "keep"
    item.proposed_budget = item.current_budget
    item.proposed_bid = item.current_bid
    item.direction = {}
    item.placement_adjustments = []
    item.negative_keywords = []
    gp.add(GuardrailResult(
        rule_id="P6_TESTING_PROTECT",
        corrected=True,
        original_action="eliminate_to_low_bid_pool",
        new_action="keep",
        message=f"[{item.campaign_name}] 测试期且上线仅 {days} 天，受样本保护，已修正为 keep",
    ))
```

---

## Phase 2: Layer 1 — 基线 LLM 输出校验

### Task 2: 新建 `campaign_validation.py`

**涉及文件：**
- 新建: `app/workflow/steps/campaign_validation.py`
- 测试: `tests/workflow/test_campaign_validation.py`

校验基线 LLM 和 Codex 的输出格式/语义。纯 Python，只打日志和返回报告，不阻断流程，不写 DB。

```python
"""Campaign 输出校验器 —— 基线 LLM 和 Codex 的输出格式/语义审计。

校验类型:
  A. 数量审计 (输入 N 个活动 → 输出必须有 N 条)
  B. 字段完整性 (必填字段非空)
  C. 动作-理由一致性 (action=keep 但 reason 说"淘汰" → 矛盾)
  D. 数值合法性 (Bid > 0, 预算在合理范围)
  E. campaign_key 唯一性
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ValidationIssue:
    """单条校验问题"""
    rule_id: str          # A1/B3/C1 等
    severity: str         # "error" | "warning"
    campaign_key: str     # 关联的活动，空字符串=全局问题
    message: str


@dataclass
class ValidationReport:
    """一次校验的完整报告"""
    issues: list[ValidationIssue] = field(default_factory=list)
    error_count: int = 0
    warning_count: int = 0
    passed: bool = True

    def add(self, issue: ValidationIssue) -> None:
        self.issues.append(issue)
        if issue.severity == "error":
            self.error_count += 1
            self.passed = False
        else:
            self.warning_count += 1


def validate_baseline_output(
    adjustments: list[Any],
    input_campaign_count: int,
) -> ValidationReport:
    """校验基线 LLM 产出的 adjustments 列表。

    Args:
        adjustments: LLM 投票合并后的 CampaignAdjustmentItem 列表
        input_campaign_count: 喂给 LLM 的活动总数

    Returns:
        ValidationReport，passed=False 表示存在 error 级别问题
    """
    report = ValidationReport()

    # ── A1: 数量审计 ──
    output_count = len(adjustments)
    if output_count != input_campaign_count:
        report.add(ValidationIssue(
            rule_id="A1_COUNT_MISMATCH",
            severity="error",
            campaign_key="",
            message=(f"输出数量不匹配：输入 {input_campaign_count} 个活动，"
                     f"产出 {output_count} 条 adjustment"),
        ))

    # ── B 类: 逐字段完整性 ──
    seen_keys: set[str] = set()
    for adj in adjustments:
        key = getattr(adj, "campaign_key", "") or ""
        name = getattr(adj, "campaign_name", "?")

        # B1: campaign_key 必填
        if not key:
            report.add(ValidationIssue(
                rule_id="B1_MISSING_KEY", severity="error",
                campaign_key="", message=f"缺少 campaign_key: {name}",
            ))
            continue

        # B2: campaign_key 重复
        if key in seen_keys:
            report.add(ValidationIssue(
                rule_id="B2_DUPLICATE_KEY", severity="error",
                campaign_key=key, message=f"重复的 campaign_key: {key}",
            ))
        seen_keys.add(key)

        # B3: action 必填
        action = getattr(adj, "action", "") or ""
        if not action:
            report.add(ValidationIssue(
                rule_id="B3_MISSING_ACTION", severity="error",
                campaign_key=key, message=f"[{name}] 缺少 action 字段",
            ))

        # B4: reason 必填
        reason = getattr(adj, "reason", "") or ""
        if not reason:
            report.add(ValidationIssue(
                rule_id="B4_MISSING_REASON", severity="warning",
                campaign_key=key, message=f"[{name}] 缺少 reason",
            ))

        # B5: 淘汰活动必须有 triggered_rule
        if action == "eliminate_to_low_bid_pool":
            triggered = getattr(adj, "triggered_rule", "") or ""
            if not triggered:
                report.add(ValidationIssue(
                    rule_id="B5_ELIMINATE_NO_TRIGGER", severity="warning",
                    campaign_key=key,
                    message=f"[{name}] 淘汰活动缺少 triggered_rule",
                ))

        # B6: proposed_bid 合法性
        pbid = getattr(adj, "proposed_bid", None)
        if pbid is not None and pbid <= 0:
            report.add(ValidationIssue(
                rule_id="B6_INVALID_BID", severity="error",
                campaign_key=key,
                message=f"[{name}] proposed_bid={pbid} 必须 > 0",
            ))

        # B7: proposed_budget 范围检查
        pbudget = getattr(adj, "proposed_budget", None)
        if pbudget is not None:
            if pbudget <= 0:
                report.add(ValidationIssue(
                    rule_id="B7_INVALID_BUDGET", severity="error",
                    campaign_key=key,
                    message=f"[{name}] proposed_budget={pbudget} 必须 > 0",
                ))
            elif pbudget > 200:
                report.add(ValidationIssue(
                    rule_id="B7_INVALID_BUDGET", severity="warning",
                    campaign_key=key,
                    message=f"[{name}] proposed_budget={pbudget} > $200 上限",
                ))

        # ── C1: 动作-理由一致性 ──
        if action == "keep" and reason:
            eliminate_keywords = ["淘汰", "eliminate", "关闭", "暂停", "废弃"]
            if any(kw in reason for kw in eliminate_keywords):
                report.add(ValidationIssue(
                    rule_id="C1_ACTION_REASON_MISMATCH",
                    severity="warning",
                    campaign_key=key,
                    message=(f"[{name}] action=keep 但 reason 含淘汰语义: "
                             f"{reason[:80]}"),
                ))

    return report


def validate_codex_output(
    codex_json: dict,
    baseline_adjustments: list[Any],
) -> ValidationReport:
    """校验 Codex 复核输出的 JSON。

    检查: 覆盖率(是否每条 baseline adjustment 都有对应 decision)、
    modify 判定的实际变更、语义自洽。
    """
    report = ValidationReport()
    decisions = codex_json.get("decisions", [])

    # D1: 覆盖率
    baseline_keys = {getattr(a, "campaign_key", "") for a in baseline_adjustments}
    codex_keys = {d.get("campaign_key", "") for d in decisions}
    missing = baseline_keys - codex_keys
    extra = codex_keys - baseline_keys

    if missing:
        report.add(ValidationIssue(
            rule_id="D1_UNCOVERED_ITEMS",
            severity="warning",
            campaign_key="",
            message=f"Codex 未覆盖 {len(missing)} 条: {sorted(missing)[:5]}...",
        ))
    if extra:
        report.add(ValidationIssue(
            rule_id="D1_EXTRA_ITEMS",
            severity="error",
            campaign_key="",
            message=f"Codex 输出了 {len(extra)} 条无效 campaign_key",
        ))

    # D2-D4: 逐条校验
    for d in decisions:
        key = d.get("campaign_key", "?")
        verdict = d.get("verdict", "")

        # D2: modify 必须有实际变更
        if verdict == "modify":
            has_change = any([
                d.get("proposed_bid") is not None,
                d.get("proposed_budget") is not None,
                d.get("action") is not None,
                isinstance(d.get("proposed_placements"), list),
                isinstance(d.get("proposed_negatives"), list),
            ])
            if not has_change:
                report.add(ValidationIssue(
                    rule_id="D2_MODIFY_NO_CHANGE",
                    severity="warning",
                    campaign_key=key,
                    message=f"[{key}] Codex 判 modify 但未给出任何实际变更",
                ))

        # D3: action=keep 但给了 proposed_* → 语义不自洽
        if verdict == "keep" or d.get("action") == "keep":
            if d.get("proposed_bid") is not None:
                report.add(ValidationIssue(
                    rule_id="D3_KEEP_WITH_BID",
                    severity="warning",
                    campaign_key=key,
                    message=f"[{key}] action=keep 却给了 proposed_bid={d['proposed_bid']}",
                ))
            if d.get("proposed_budget") is not None:
                report.add(ValidationIssue(
                    rule_id="D3_KEEP_WITH_BUDGET",
                    severity="warning",
                    campaign_key=key,
                    message=f"[{key}] action=keep 却给了 proposed_budget={d['proposed_budget']}",
                ))

        # D4: final_reason 必填（modify 时）
        if verdict == "modify" and not d.get("final_reason", "").strip():
            report.add(ValidationIssue(
                rule_id="D4_MODIFY_NO_REASON",
                severity="warning",
                campaign_key=key,
                message=f"[{key}] modify 但 final_reason 为空",
            ))

    return report
```

---

## Phase 3: 接入护栏到现有管线

### Task 3: 修改 `campaign.py` — 接入 Layer 0 + Layer 1 + 异步化入口

**涉及文件：**
- 修改: `app/workflow/steps/campaign.py`

在 `_resolve_budget_conflicts` 调用之前插入 Layer 0 护栏，之后插入 Layer 1 校验。护栏修正结果计入 warnings_list 供前端可见。

```python
    # ── 6d. Layer 0: 确定性护栏（核心词保护/强制淘汰/预算上限等）──
    from app.workflow.steps.campaign_guardrails import apply_all as apply_guardrails
    guardrail_pass = apply_guardrails(
        adjustments,
        product_stage=strategy_context.product_stage,
    )
    if guardrail_pass.corrections > 0:
        logger.info(
            "Guardrails [%s]: 护栏修正 %d 条 (共 %d 条)",
            parent_asin, guardrail_pass.corrections, len(adjustments),
        )
        for r in guardrail_pass.results:
            if r.corrected:
                logger.debug("  [%s] %s", r.rule_id, r.message[:120])
        warnings_list.extend(
            r.message for r in guardrail_pass.results if r.corrected
        )

    # 7. 预算冲突裁决（保留原有逻辑——淘汰值硬填充等）
    budget_warnings = _resolve_budget_conflicts(adjustments, product_stage=strategy_context.product_stage)
    warnings_list.extend(budget_warnings)

    # ── 7a. Layer 1: 基线输出校验 ──
    from app.workflow.steps.campaign_validation import (
        validate_baseline_output,
    )
    baseline_report = validate_baseline_output(adjustments, len(llm_campaigns))
    if not baseline_report.passed:
        logger.warning(
            "Baseline validation [%s]: %d errors, %d warnings",
            parent_asin, baseline_report.error_count, baseline_report.warning_count,
        )
        for issue in baseline_report.issues:
            if issue.severity == "error":
                logger.warning("  VALIDATION %s: %s", issue.rule_id, issue.message[:150])
                warnings_list.append(f"[校验{issue.rule_id}] {issue.message[:150]}")
    elif baseline_report.warning_count > 0:
        logger.info(
            "Baseline validation [%s]: %d warnings (all passed)",
            parent_asin, baseline_report.warning_count,
        )
```

### Task 4: 修改 `campaign.py` API — 异步化入口

**涉及文件：**
- 修改: `app/api/campaign.py`（vendor 中的 codex hook 段）

在 `_maybe_push_erp` 之后，条件启用后台 codex：

```python
# 在 _maybe_push_erp 调用后，codex 复核 hook 段改为:
if settings.codex_async_enabled:
    # 异步模式：基线落库即返回，codex 后台补
    import asyncio as _asyncio
    _asyncio.create_task(_review_and_patch(
        result, erp["decision_id"], asin, target_acos,
    ))
elif use_codex_review:
    # 同步模式（现状）：等 codex 完成再返回
    outcome = await maybe_review(result, asin=asin, target_acos=target_acos, force=force)
    if outcome.reviewed:
        # 同步模式下 result 已被就地修改，直接落库
        pass
```

其中 `_review_and_patch` 定义在 campaign.py 中：

```python
async def _review_and_patch(result, decision_id: str, asin: str, target_acos):
    """后台执行 codex 复核并增量写回被修改的 card。fail-open，不抛异常。"""
    try:
        from review_hook import maybe_review
        outcome = await maybe_review(result, asin=asin, target_acos=target_acos, force=True)
        if not outcome.reviewed:
            return
        # 只 UPDATE 被 codex 修改过的 card
        from app.persistence.erp_writer.repository import _get_repository
        repo = _get_repository()
        for item in result.adjustments:
            if getattr(item, "review_level", "") == "AI_REVIEWED":
                repo.update_suggest_card_after_review(item, decision_id)
        logger.info("codex async [%s]: patched %d cards", asin,
                    sum(1 for a in result.adjustments if getattr(a, "review_level", "") == "AI_REVIEWED"))
    except Exception as e:
        logger.warning("codex async [%s] failed (fail-open): %s", asin, e)
```

### Task 5: 修改 `repository.py` — 新增单条 card 更新方法

**涉及文件：**
- 修改: `app/persistence/erp_writer/repository.py`

这是一个很薄的方法——就一条 UPDATE SQL，10 行代码。不需要新增列、不需要改表结构。

```python
def update_suggest_card_after_review(self, item, decision_id: str) -> None:
    """Codex 复核后增量更新单条 card。

    只更新被 codex 修改过的字段：reason、proposed_bid、proposed_budget、
    action、review_level。不重写整张表。
    """
    conn = self._connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE t_advert_agent_modify_suggest_card
                   SET description = %s,
                       proposed_budget = %s,
                       proposed_bid = %s,
                       suggest_category = %s,
                       review_level = 'AI_REVIEWED',
                       update_time = NOW()
                   WHERE decision_id = %s AND campaign_key = %s""",
                (
                    getattr(item, "reason", "") or "",
                    getattr(item, "proposed_budget", None),
                    getattr(item, "proposed_bid", None),
                    getattr(item, "action", "") or "",
                    decision_id,
                    getattr(item, "campaign_key", "") or "",
                ),
            )
        conn.commit()
    finally:
        conn.close()
```

### Task 6: 修改 `merge.py` — 接入 Layer 2 校验

**涉及文件：**
- 修改: `AD_Agent_codexV2/merge.py`

在 `apply_review` 函数末尾，merge 完成后增加 Codex 输出校验。校验问题附加到 stats 中供审计，不影响 merge 主流程。

```python
# merge.py 中 apply_review 函数的 return 之前插入:

    # ── Layer 2: Codex 输出校验 ──
    try:
        from app.workflow.steps.campaign_validation import (
            validate_codex_output,
        )
        codex_report = validate_codex_output(review, result.adjustments)
        if not codex_report.passed or codex_report.warning_count > 0:
            import logging
            _log = logging.getLogger(__name__)
            _log.info(
                "Codex validation: %d errors, %d warnings",
                codex_report.error_count, codex_report.warning_count,
            )
            # 将校验问题附加到 stats 中供审计
            stats["validation_issues"] = [
                {"rule": i.rule_id, "severity": i.severity,
                 "key": i.campaign_key, "msg": i.message}
                for i in codex_report.issues
            ]
    except ImportError:
        pass  # vendor 环境可能未部署，fail-open
```

### Task 7: 修改 `campaign-review.md` — 强化 Prompt 护栏

**涉及文件：**
- 修改: `prompts/campaign-review.md`

在现有 prompt 的"执行约束"段落后增加护栏硬约束：

```markdown
## 护栏硬约束（最高优先级，违反视为复核失败）

1. **数量完整**：`decisions` 数组必须覆盖输入 JSON 里**每一条** `campaign_key`，不得遗漏。
2. **modify 必须实质变更**：判 `verdict=modify` 时，至少给出 `proposed_bid`/`proposed_budget`/`action`/`proposed_placements`/`proposed_negatives` 之一。无实质变更的 modify 应降级为 keep。
3. **action 标签自洽**：若 `action="keep"`，不得同时给出 `proposed_bid`/`proposed_budget`（除非你**有意识地**想"保持淘汰判断但顺便改 bid"这种极罕见场景）。
4. **final_reason 必填**：`verdict=modify` 时 `final_reason` 必须非空，直接写最终建议的理由（基线风格，不含复核口吻）。
5. **verify 回查**：输出前逐条回查——你修正了 action 标签吗？修正了数值吗？标签和数值一致吗？如果没有，回到那条重新修正。
6. **禁止死循环**：每条只判一次。不要在 keep/modify/drop 之间反复横跳。判定完就输出。
```

---

## Phase 4: Codex 耗时日志

### Task 8: 修改 `codex_client.py` — 增加耗时日志

**涉及文件：**
- 修改: `AD_Agent_codexV2/codex_client.py`

在 `review_with_codex` 函数中增加耗时记录，输出到 preview.log：

```python
async def review_with_codex(prompt_text: str, *, timeout: int = 240) -> dict:
    import time as _time
    _t0 = _time.monotonic()
    # ... 原有逻辑（创建子进程、通信、解析 JSON）...
    _elapsed = _time.monotonic() - _t0
    import logging
    _log = logging.getLogger("codex_client")
    _log.info("codex exec 完成, 耗时 %.1fs, prompt_len=%d", _elapsed, len(prompt_text))
    return _extract_json(raw)
```

---

## 集成顺序与验收

### 部署顺序

| Phase | 内容 | 依赖 | 可独立上线？ |
|-------|------|------|:---:|
| Phase 1 | Layer 0 确定性护栏 | 无 | ✓ |
| Phase 2 | Layer 1 输出校验 | 无 | ✓ |
| Phase 3 | 接入管线 + 异步化入口 | Phase 1+2 | 一起上 |
| Phase 4 | Codex 耗时日志 | 无 | ✓ |

### 回滚安全

- Layer 0/1/2 是纯 Python 校验，不写 DB。出问题最多在日志里打 warning，不影响现有分析流程。
- Codex 异步化用 `settings.codex_async_enabled` 开关控制。默认 `False`（保持同步行为，和现在完全一样）。
- `update_suggest_card_after_review` 是新增方法，不会影响现有 `write_full` 逻辑。
- 没有 DDL 变更，不需要加列。

### 验收标准

1. **Layer 0**: 对一个 `is_core=True` 的活动，LLM 输出 `action=eliminate` → 护栏后自动变为 `action=keep`，warnings_list 中包含 P0_CORE_PROTECT 消息
2. **Layer 1**: 故意构造一个少了 2 条输出的 adjustments 列表 → 日志输出 `A1_COUNT_MISMATCH` error
3. **Layer 2**: Codex 返回 `verdict=modify` 但无任何字段变更 → 日志输出 `D2_MODIFY_NO_CHANGE` warning，stats 中附加 validation_issues
4. **异步化**: `codex_async_enabled=true` 时，HTTP 响应在 codex 完成前返回（<2s），后台 task 完成后 card 被增量 UPDATE
5. **耗时日志**: `preview.log` 中出现 `codex exec 完成, 耗时 X.Xs` 日志行
6. **开关回退**: `codex_async_enabled=false` 时行为和现在完全一致，基线→codex→落库同步串行
