"""Campaign 确定性护栏 —— 不依赖 LLM 的硬规则，纯 Python 逻辑。

规则优先级（数字越小越先执行）：
  P0: 核心词禁淘汰 (is_core=True → action 不得为 eliminate)
  P1: 新活动保护 (days_online ≤ 3 → 禁止淘汰)
  P2: 复评抖动保护 (days_since_reactivation ≤ 3 → 禁止淘汰)
  P3: 硬淘汰触发 (无出单 且 (bid ≤ $0.10 或 budget ≤ $1.00) → 强制淘汰, OR 口径; 仅限精准 EXACT, KB10 §1.6/ONT-016)
  P4: 淘汰值硬填充 ($1.00/$0.20, 清 placement/neg_kw)
  P5: 淘汰反修正 (受保护活动/非精准活动被误判 → 强制改 keep)
  P6: 日预算上限 ($200, KB15 §1.4)
  P7: 预算花不完禁加 (7d 花费 / 日预算×7 < 70% → cap)
  P8: Bid 振幅上限 (>50% 且 clicks<10 → 收敛到 30%)
  P9: Bid 硬上限 ($3.00, KB15 §1.4)
  P10: 广告位 TOS 加价阻断 (库存<15天 / 退货率≥30% / 评分<3.8, KB15 §3.2)
  P11: 新活动禁大降 Bid (上线≤3天 → 降幅收窄至 ≤$0.05, KB15 §1.3 / KB19 §3)

来源: 从 campaign.py `_resolve_budget_conflicts` + campaign_portfolio.py 阈值常量提取归一化。
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.campaign import CampaignAdjustmentItem


# ── 阈值常量（归一化唯一来源）──────────────────────────────

LOW_BID_MIN = 0.10       # 淘汰池 bid 下限（OR 归组/强制淘汰用）
LOW_BID_MAX = 0.20       # 严格在池判定: bid ≤ 此值 且 budget ≤ LOW_BUDGET_MAX → 已淘汰
LOW_BUDGET_MAX = 1.00    # 严格在池判定: 预算 ≤ 此值 且 bid ≤ LOW_BID_MAX → 已淘汰
BID_HARD_CAP = 3.00      # Bid 硬上限 (KB15 §1.4)
BUDGET_CAP = 200.0       # 日预算硬上限 (KB15 §1.4 / KB19 §10)


def is_strictly_in_low_bid_pool(bid: float | None, budget: float | None) -> bool:
    """严格在池（AND）：bid ≤ LOW_BID_MAX 且 budget ≤ LOW_BUDGET_MAX。

    供预过滤剔除 + 复评判"确实已入池执行"，与归组 OR 口径区分。
    """
    return (
        bid is not None and bid <= LOW_BID_MAX
        and budget is not None and budget <= LOW_BUDGET_MAX
    )


def _is_in_elimination_pool(bid: float | None, budget: float | None) -> bool:
    """归组淘汰判定 (OR)：bid ≤ $0.10 或 budget ≤ LOW_BUDGET_MAX → 归低价捡漏组。

    bid 阈值 $0.10 是【归组路径】独立口径，严于预过滤 AND 的 LOW_BID_MAX($0.20)。
    bid∈(LOW_BID_MIN, 0.20] 且预算正常 → 不单凭 bid 归组。
    """
    return (bid is not None and bid <= LOW_BID_MIN) or (budget is not None and budget <= LOW_BUDGET_MAX)


# ── 护栏结果 ──────────────────────────────────────────

@dataclass
class GuardrailResult:
    rule_id: str          # "P0_CORE_PROTECT"
    corrected: bool
    message: str
    retry_instruction: str = ""
    campaign_key: str = ""
    original_action: str = ""
    new_action: str = ""


@dataclass
class GuardrailPass:
    results: list[GuardrailResult] = field(default_factory=list)
    corrections: int = 0

    def add(self, result: GuardrailResult) -> None:
        self.results.append(result)
        if result.corrected:
            self.corrections += 1


# ── 主入口 ────────────────────────────────────────────

def apply_all(
    adjustments: list,
    *,
    product_stage: str = "",
    target_cpa: float | None = None,
    inventory_days: float | None = None,
    refund_rate: float | None = None,
    rating: float | None = None,
) -> GuardrailPass:
    """对所有 adjustments 按优先级执行全部护栏规则。后执行规则看到前序修正后的值。"""
    gp = GuardrailPass()

    # 第一轮：保护类（禁止淘汰）—— P0-P2
    for item in adjustments:
        _p0_core_protect(item, gp)
        _p1_new_campaign_protect(
            item, gp, product_stage=product_stage, target_cpa=target_cpa,
        )
        _p2_reactivation_protect(item, gp)

    # 第二轮：裁决类 —— P3-P10
    for item in adjustments:
        _p3_force_eliminate(item, gp, product_stage=product_stage)
        _p5_protection_reversal(item, gp)
        _p4_elimination_fill(item, gp)
        _p6_budget_cap(item, gp)
        _p7_budget_low_spend(item, gp)
        _p8_bid_amplitude(item, gp)
        _p9_bid_cap(item, gp)
        _p10_placement_block(item, gp, inventory_days, refund_rate, rating)
        _p11_new_campaign_bid_protect(item, gp)

    return gp


# ── 规则函数 ───────────────────────────────────────────

def _p0_core_protect(item, gp: GuardrailPass) -> None:
    """核心词禁淘汰/禁暂停。is_core=True → action 不得为 eliminate 或 paused。"""
    if not (getattr(item, "is_core", False) and item.action in ("eliminate_to_low_bid_pool", "paused")):
        return
    original = item.action
    _force_keep(item)
    gp.add(GuardrailResult(
        rule_id="P0_CORE_PROTECT", corrected=True,
        campaign_key=getattr(item, "campaign_key", ""),
        original_action=original, new_action="keep",
        message=f"[{item.campaign_name}] 核心词受保护，已强制修正为 keep",
        retry_instruction=(
            f"[{item.campaign_name}] 核心词不得淘汰/暂停。若当前表现偏弱，可结合活动事实评估小幅降 bid、"
            "降预算、调整广告位或维持观察。"
        ),
    ))


def _p1_new_campaign_protect(
    item,
    gp: GuardrailPass,
    *,
    product_stage: str = "",
    target_cpa: float | None = None,
) -> None:
    """消费 KB17 活动样本口径，样本不足时禁止非硬淘汰/暂停。"""
    if item.action not in ("eliminate_to_low_bid_pool", "paused"):
        return
    # P3 硬淘汰优先级高于 P1。触底场景不在 P1 写"禁止淘汰/暂停"告警，交给 P3/P4 处理。
    if _p3_should_force_eliminate(item):
        return
    sample_insufficient, reasons = _sample_insufficient(
        item, product_stage=product_stage, target_cpa=target_cpa,
    )
    if not sample_insufficient:
        return

    # 仅禁淘汰/暂停，不改非淘汰调整；若 LLM 已判淘汰/暂停，则强制恢复到 keep 基准态。
    original = item.action
    _force_keep(item)
    gp.add(GuardrailResult(
        rule_id="P1_SAMPLE_INSUFFICIENT", corrected=True,
        campaign_key=getattr(item, "campaign_key", ""),
        original_action=original, new_action="keep",
        message=f"[{item.campaign_name}] 样本不足({'; '.join(reasons)})，受样本保护，禁止淘汰/暂停，已强制修正为 keep (KB17 §1.2)",
        retry_instruction=(
            f"[{item.campaign_name}] 样本不足({'; '.join(reasons)})时不得直接淘汰/暂停；若同时命中无订单且 bid/预算触底，"
            "按硬淘汰判断。其他情况下，可结合事实评估小幅 bid、预算、广告位调整或维持。"
        ),
    ))


def _p2_reactivation_protect(item, gp: GuardrailPass) -> None:
    """复评抖动保护：离池 ≤3 天 → 禁止再次淘汰。"""
    days = getattr(item, "days_since_reactivation", -1)
    if not (days >= 0 and days <= 3 and item.action == "eliminate_to_low_bid_pool"):
        return
    _force_keep(item)
    gp.add(GuardrailResult(
        rule_id="P2_REACTIVATION_PROTECT", corrected=True,
        campaign_key=getattr(item, "campaign_key", ""),
        original_action="eliminate_to_low_bid_pool", new_action="keep",
        message=f"[{item.campaign_name}] 复评后仅 {days} 天，等同新活动保护，已修正为 keep",
        retry_instruction=(
            f"[{item.campaign_name}] 复评后仅 {days} 天，短期内不得再次淘汰。若表现仍弱，"
            "可结合事实评估轻量收敛 bid、预算或维持观察，避免进出池抖动。"
        ),
    ))


def _p3_force_eliminate(item, gp: GuardrailPass, *, product_stage: str = "") -> None:
    """硬淘汰触发 (OR)：无出单 且 (bid ≤ $0.10 或 budget ≤ LOW_BUDGET_MAX) → 强制淘汰。

    有出单的活动即使 bid/预算触底也不强制淘汰（可能仍在产出，留给 LLM 判断）。
    归组/强制修正用 OR 口径（严于预过滤 AND 的 is_strictly_in_low_bid_pool）。
    P0-P2 已保护的不会被触达（action 已变 keep）。
    """
    if item.action == "eliminate_to_low_bid_pool":
        return  # 已是淘汰，无需再判
    perf = getattr(item, "perf_7d", {}) or {}
    has_orders = int(perf.get("orders") or 0) > 0
    if has_orders:
        return  # 有出单，不强制淘汰
    # 仅 P0(核心词) 和 P2(复评保护) 高于 P3；P1(样本不足) 不拦 P3 硬淘汰
    if getattr(item, "is_core", False):
        return
    days_since_reactivation = getattr(item, "days_since_reactivation", -1)
    if days_since_reactivation >= 0 and days_since_reactivation <= 3:
        return
    # KB10 §1.6 / ONT-016: 低价捡漏组仅允许精准活动；BROAD/PHRASE/AUTO 退出动作 =
    # paused_campaign（LLM 动作码，出 LLM 翻译为 paused，见 _normalize_action）。
    # 本期护栏不处理 paused（应当经过护栏，延后立项），此处不做强制淘汰、留给后续判断。
    if (item.match_type or "").upper() != "EXACT":
        return
    if not _p3_should_force_eliminate(item):
        return
    item.action = "eliminate_to_low_bid_pool"
    item.proposed_budget = 1.0
    # 淘汰 bid 收敛到 [LOW_BID_MIN, LOW_BID_MAX] 区间
    cbid = item.current_bid or 0
    if cbid > LOW_BID_MAX:
        item.proposed_bid = LOW_BID_MAX
    elif cbid < LOW_BID_MIN:
        item.proposed_bid = LOW_BID_MIN
    else:
        item.proposed_bid = cbid
    item.placement_adjustments = []
    item.negative_keywords = []
    gp.add(GuardrailResult(
        rule_id="P3_FORCE_ELIMINATE", corrected=True,
        campaign_key=getattr(item, "campaign_key", ""),
        new_action="eliminate_to_low_bid_pool",
        message=(f"[{item.campaign_name}] bid=${item.current_bid}/"
                 f"budget=${item.current_budget} 已达淘汰阈值，强制淘汰 → bid=${item.proposed_bid:.2f}"),
        retry_instruction=(
            f"[{item.campaign_name}] 无订单且 bid=${item.current_bid}/budget=${item.current_budget} 已触及淘汰阈值，"
            "应进入低价捡漏/淘汰判断，不要仅因样本不足改回 keep。"
        ),
    ))


def _p4_elimination_fill(item, gp: GuardrailPass) -> None:
    """淘汰值填充：预算 $1.00，Bid = max($0.10, min(current_bid, proposed_bid, $0.20))。

    取 current/proposed/0.20 三者的最小值（宁可保守），但不低于 $0.10。
    清 placement/neg_kw。
    """
    if item.action != "eliminate_to_low_bid_pool":
        return
    changed = False
    if item.proposed_budget != 1.0:
        item.proposed_budget = 1.0; changed = True
    cbid = item.current_bid or 0
    pbid = item.proposed_bid if item.proposed_bid is not None else LOW_BID_MAX
    target = max(LOW_BID_MIN, min(cbid, pbid, LOW_BID_MAX))
    if item.proposed_bid != target:
        item.proposed_bid = target; changed = True
    if item.placement_adjustments:
        item.placement_adjustments = []; changed = True
    if item.negative_keywords:
        item.negative_keywords = []; changed = True
    if changed:
        gp.add(GuardrailResult(
            rule_id="P4_ELIMINATION_FILL", corrected=True,
            campaign_key=getattr(item, "campaign_key", ""),
            message=f"[{item.campaign_name}] 淘汰值已补齐为 $1.00/${item.proposed_bid:.2f}",
            retry_instruction=(
                f"[{item.campaign_name}] 若判断为淘汰，预算应为 $1.00，bid 应落在低价捡漏区间，"
                "且不应附带广告位加价或否词调整。"
            ),
        ))


def _p5_protection_reversal(item, gp: GuardrailPass) -> None:
    """淘汰反修正：绝对保护项（核心词/复评≤3天）或非精准活动被误判淘汰 → 强制改回 keep。

    P0(核心词) 和 P2(复评保护) 高于 P3，P1(样本不足) 低于 P3。
    非精准活动（BROAD/PHRASE/AUTO/PRODUCT_TARGETING）不得迁入低价捡漏组
    （KB10 §1.6 / ONT-016），退出动作 = paused_campaign → paused（已接入 _normalize_action）；
    本期护栏不处理 paused（应当经过护栏，延后立项）。
    此处兜底处理：若 P3 之后的规则链把受保护项又变成淘汰，则在 P5 阶段拉回。
    """
    if item.action != "eliminate_to_low_bid_pool":
        return
    core = getattr(item, "is_core", False)
    react = getattr(item, "days_since_reactivation", -1)
    react_protected = react >= 0 and react <= 3
    non_exact = (item.match_type or "").upper() != "EXACT"
    if not (core or react_protected or non_exact):
        return
    if core:
        reason = "核心词"
        instruction = (
            f"[{item.campaign_name}] 核心词不得淘汰。若当前表现偏弱，可结合活动事实评估小幅降 bid、"
            "降预算、调整广告位或维持观察。"
        )
    elif react_protected:
        reason = "复评"
        instruction = (
            f"[{item.campaign_name}] 复评后短期内不得再次淘汰。若表现仍弱，"
            "可评估轻量收敛 bid、预算或维持观察，避免进出池抖动。"
        )
    else:
        reason = "非精准活动(低价捡漏组仅限精准)"
        instruction = (
            f"[{item.campaign_name}] 非精准活动不得迁入低价捡漏组。"
            "可结合事实评估否词、降Bid、降预算或维持。"
        )
    _force_keep(item)
    gp.add(GuardrailResult(
        rule_id="P5_PROTECTION_REVERSAL", corrected=True,
        campaign_key=getattr(item, "campaign_key", ""),
        original_action="eliminate_to_low_bid_pool", new_action="keep",
        message=f"[{item.campaign_name}] 受{reason}保护，已强制修正回 keep",
        retry_instruction=instruction,
    ))


def _p6_budget_cap(item, gp: GuardrailPass) -> None:
    """日预算硬上限 $200 (KB 19 §10)。"""
    if item.proposed_budget is not None and item.proposed_budget > BUDGET_CAP:
        item.proposed_budget = BUDGET_CAP
        gp.add(GuardrailResult(
            rule_id="P6_BUDGET_CAP", corrected=True,
            campaign_key=getattr(item, "campaign_key", ""),
            message=f"[{item.campaign_name}] proposed_budget > ${BUDGET_CAP:.0f}，已截断",
            retry_instruction=(
                f"[{item.campaign_name}] 日预算不得超过 ${BUDGET_CAP:.0f}。如仍需加预算，"
                "请在上限内给出合规值；也可按事实选择维持或下调。"
            ),
        ))


def _p7_budget_low_spend(item, gp: GuardrailPass) -> None:
    """预算花不完禁加：近 7 天预算利用率 < 70% → cap proposed_budget ≤ current。"""
    if item.action in ("eliminate_to_low_bid_pool", "paused"):
        return
    if item.proposed_budget is None or item.current_budget is None:
        return
    if item.proposed_budget <= item.current_budget:
        return
    perf = getattr(item, "perf_7d", {}) or {}
    spend_raw = perf.get("cost", perf.get("spend"))
    if spend_raw is None:
        return
    spend = float(spend_raw or 0)
    current_budget = float(item.current_budget or 0)
    if current_budget <= 0:
        return
    budget_utilization_pct = spend / (current_budget * 7) * 100
    if budget_utilization_pct >= 70:
        return
    item.proposed_budget = item.current_budget
    gp.add(GuardrailResult(
        rule_id="P7_BUDGET_LOW_SPEND", corrected=True,
        campaign_key=getattr(item, "campaign_key", ""),
        message=(f"[{item.campaign_name}] 预算利用率 {budget_utilization_pct:.1f}% < 70%，"
                 "禁加预算"),
        retry_instruction=(
            f"[{item.campaign_name}] 近 7 天总花费 ${spend:.1f}，当前日预算 ${current_budget:.0f}，"
            f"预算利用率 {budget_utilization_pct:.1f}% 低于 70%，"
            "不应上调预算。可结合事实评估维持预算、下调预算、调整 bid 或广告位。"
        ),
    ))


def _p8_bid_amplitude(item, gp: GuardrailPass) -> None:
    """Bid 振幅上限：变动 >50% 且 clicks<10 → 收敛到 30%。"""
    if item.action in ("eliminate_to_low_bid_pool", "paused"):
        return
    if item.proposed_bid is None or item.current_bid is None or item.current_bid == 0:
        return
    change = abs(item.proposed_bid - item.current_bid) / item.current_bid
    if change <= 0.5:
        return
    perf = getattr(item, "perf_7d", {}) or {}
    if "clicks" not in perf:
        return
    clicks = int(perf.get("clicks") or 0)
    if clicks >= 10:
        return
    direction = 1 if item.proposed_bid > item.current_bid else -1
    capped = round(item.current_bid * (1 + direction * 0.30), 2)
    original = item.proposed_bid
    item.proposed_bid = max(0.20, capped)
    gp.add(GuardrailResult(
        rule_id="P8_BID_AMPLITUDE", corrected=True,
        campaign_key=getattr(item, "campaign_key", ""),
        message=(f"[{item.campaign_name}] Bid 变动 {change:.0%} > 50% 且 clicks={clicks} < 10，"
                 f"已收敛: ${original:.2f} → ${item.proposed_bid:.2f}"),
        retry_instruction=(
            f"[{item.campaign_name}] clicks={clicks} 的样本下 bid 变动 {change:.0%} 偏大。"
            "可保留原调整方向，但幅度应更收敛；若事实支持，也可维持。"
        ),
    ))


def _p9_bid_cap(item, gp: GuardrailPass) -> None:
    """Bid 硬上限 $3.00 (KB15 §1.4)。"""
    if item.proposed_bid is not None and item.proposed_bid > BID_HARD_CAP:
        original = item.proposed_bid
        item.proposed_bid = BID_HARD_CAP
        gp.add(GuardrailResult(
            rule_id="P9_BID_CAP", corrected=True,
            campaign_key=getattr(item, "campaign_key", ""),
            message=f"[{item.campaign_name}] proposed_bid=${original:.2f} > ${BID_HARD_CAP:.0f}，已截断",
            retry_instruction=(
                f"[{item.campaign_name}] bid 不得超过 ${BID_HARD_CAP:.0f}。如仍需加 bid，"
                "请在上限内重新给值；否则按事实选择维持或其他调整。"
            ),
        ))


def _p10_placement_block(
    item, gp: GuardrailPass,
    inventory_days: float | None,
    refund_rate: float | None,
    rating: float | None,
) -> None:
    """广告位 TOS 加价阻断 (KB15 §3.2)。

    库存<15天 / 退货率≥30% / 评分<3.8 → 阻断所有 TOS(头部)加价，强制改 维持。
    """
    placements = getattr(item, "placement_adjustments", None)
    if not placements:
        return

    blocks: list[str] = []
    if inventory_days is not None and inventory_days < 15:
        blocks.append(f"库存仅{inventory_days:.0f}天")
    if refund_rate is not None and refund_rate >= 30:
        blocks.append(f"退货率{refund_rate:.0f}%≥30%")
    if rating is not None and rating < 3.8:
        blocks.append(f"评分{rating}")

    if not blocks:
        return

    changed = False
    for plc in placements:
        if not isinstance(plc, dict):
            continue
        pos = str(plc.get("placement", "")).strip()
        act = str(plc.get("action", "")).strip()
        if pos in ("头部", "TOS", "TOP_OF_SEARCH") and act not in ("维持", "", None):
            plc["action"] = "维持"
            # 同步 proposed_pct 回到 current_pct，避免 action=维持 但百分比仍显示加价
            if "current_pct" in plc:
                plc["proposed_pct"] = plc["current_pct"]
            plc["evidence"] = f"{plc.get('evidence', '')} [阻断: {'; '.join(blocks)}]".strip()
            changed = True

    if changed:
        gp.add(GuardrailResult(
            rule_id="P10_PLACEMENT_BLOCK", corrected=True,
            campaign_key=getattr(item, "campaign_key", ""),
            message=f"[{item.campaign_name}] TOS 加价被阻断: {'; '.join(blocks)} (KB15 §3.2)",
            retry_instruction=(
                f"[{item.campaign_name}] 因 {'; '.join(blocks)}，不得上调头部 TOS 加价。"
                "这不代表商品位、其他位、bid 或预算必须维持；请按各自数据继续判断。"
            ),
        ))


def _p11_new_campaign_bid_protect(item, gp: GuardrailPass) -> None:
    """新活动禁止大幅降 Bid：上线≤3天 → 降幅收窄至 ≤$0.05 (KB19 §3 大降阈值)。

    KB15 §1.3 禁止降 Bid，此处放宽至允许小降（≤$0.05），打断超过 $0.05 的下降。
    """
    days = getattr(item, "days_online", -1)
    if not (days >= 0 and days <= 3):
        return
    if item.action in ("eliminate_to_low_bid_pool", "paused"):
        return  # P4 已处理淘汰值；暂停不调 bid
    if item.action == "keep" and getattr(item, "days_online", -1) <= 3:
        # P1 只改了 action=keep，没动 proposed 值 → 仍须检查降幅
        pass
    elif item.action == "keep":
        return  # 非新活动且已 keep，无需振幅检查
    if item.proposed_bid is None or item.current_bid is None or item.current_bid == 0:
        return
    drop = item.current_bid - item.proposed_bid
    if drop <= 0:  # 没降 → 放行
        return
    # 允许最大降幅 = min(10%×当前bid, $0.05)
    max_drop = min(item.current_bid * 0.10, 0.05)
    if drop <= max_drop:  # 在允许范围内 → 放行
        return
    item.proposed_bid = round(item.current_bid - max_drop, 2)
    gp.add(GuardrailResult(
        rule_id="P11_NEW_CAMPAIGN_BID", corrected=True,
        campaign_key=getattr(item, "campaign_key", ""),
        message=f"[{item.campaign_name}] 上线仅{days}天，Bid降幅从${drop:.2f}收窄至${max_drop:.2f}（KB15 §1.3）",
        retry_instruction=(
            f"[{item.campaign_name}] 上线仅 {days} 天，bid 不应大幅下调。若确需降 bid，"
            f"可收敛到不超过 ${max_drop:.2f} 的降幅；也可按事实选择维持或其他轻量调整。"
        ),
    ))


# ── helper ────────────────────────────────────────────

def _force_keep(item) -> None:
    """将数值/广告位动作重置为 keep，保留独立否词建议。"""
    item.action = "keep"
    item.proposed_budget = item.current_budget
    item.proposed_bid = item.current_bid
    if hasattr(item, "direction"):
        item.direction = {}
    item.placement_adjustments = []


def _sample_insufficient(
    item,
    *,
    product_stage: str = "",
    target_cpa: float | None = None,
) -> tuple[bool, list[str]]:
    from app.core.campaign_sample import assess_campaign_sample

    days = getattr(item, "days_online", -1)
    perf = getattr(item, "perf_7d", {}) or {}
    assessment = assess_campaign_sample(
        days_online=days,
        clicks_7d=perf.get("clicks"),
        cost_7d=perf.get("cost"),
        target_cpa=target_cpa,
    )
    reasons: list[str] = []
    for reason in assessment.reasons:
        if reason == "days_online_lt_3":
            reasons.append(f"上线仅 {days} 天")
        elif reason == "cost_7d_below_threshold":
            reasons.append(f"7d花费${float(perf.get('cost') or 0):.1f}<${assessment.threshold:.1f}")
        elif reason == "clicks_7d_lt_10":
            reasons.append(f"7d点击{int(perf.get('clicks') or 0)}<10")
    # 测试期保护是独立消费策略，不并入 KB17 活动 helper 的事实口径。
    if "测试" in (product_stage or "") and days >= 0 and days < 14:
        reasons.append(f"测试期且上线仅 {days} 天")
    return bool(reasons), reasons


def _p3_should_force_eliminate(item) -> bool:
    """P3 强制淘汰的完整前置条件：无出单 且 (bid ≤ $0.10 或 budget ≤ $1.00)。

    与 _p3_force_eliminate 的调用方守卫保持语义一致——有出单的活动即使 bid/预算触底
    也可能仍在产出，不应被强制淘汰也不应绕过 P1 样本保护。
    """
    perf = getattr(item, "perf_7d", {}) or {}
    has_orders = int(perf.get("orders") or 0) > 0
    if has_orders:
        return False
    return (
        (item.current_bid is not None and item.current_bid <= LOW_BID_MIN)
        or (item.current_budget is not None and item.current_budget <= LOW_BUDGET_MAX)
    )
