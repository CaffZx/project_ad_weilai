"""四层工作流数据模型

Layer 1.1 战略层 — 产品定位 / 产品阶段 / 淡旺季（互斥，无AI推荐）
Layer 1.2 策略层 — 广告目的 / 关键词类型（多选，有AI推荐）
Layer 1.3 诊断层 — 产品数据摘要（只读）
Layer 1.4 执行层 — 广告方向（多选，有AI推荐，不持久化）
"""

import re
from enum import StrEnum

from pydantic import AliasChoices, BaseModel, Field


# ── 枚举定义 ────────────────────────────────────────────────


def normalize_product_level(value):
    """归一化产品定位原始输入，防全角/空格漂移导致枚举校验失败。

    处理：全角括号（）→ 半角()、全角空格(U+3000)→半角、首尾 trim、
    规范"中文(Px)"为"中文 (Px)"（括号前单空格）。
    仅做格式归一，不做旧值→新值映射（后者由 LEVEL_OLD_TO_NEW 负责）。
    """
    if not isinstance(value, str):
        return value
    s = value.replace("（", "(").replace("）", ")").replace("　", " ").strip()
    s = re.sub(r"\s*\(\s*", " (", s)   # 左括号前规范为单空格、去括号内前导空格
    s = re.sub(r"\s*\)", ")", s)        # 去右括号前空格
    return s.strip()


class ProductLevel(StrEnum):
    P0 = "战略级产品 (P0)"   # 全年核心主推/旺季爆款，准入：月订单≥3000 + BSR Top50 + 评≥4.0 + 退货率≤25% + 库存≥30天
    P1 = "重点产品 (P1)"     # 市场已验证可推主力款，P2→P1需满足任意3条：日均订单≥50/环比≥20%/自然单>30%/核心词Top50/退货率≤30%
    P2 = "常规产品 (P2)"     # 稳定出单、控制成本、保护利润（兜底类型）
    P3 = "长尾产品 (P3)"     # 高毛利/小众/风格化，不追大词排名，ACOS通常≤30%

    @classmethod
    def _missing_(cls, value):
        """枚举层统一容错：全角括号/空格漂移、旧 3 档值 → 归一到合法成员，防 422。

        任何 ProductLevel(value)（含 Pydantic 校验）走标准查找失败时触发。
        """
        if not isinstance(value, str):
            return None
        v = normalize_product_level(value)
        v = LEVEL_OLD_TO_NEW.get(v, v)
        for member in cls:
            if member.value == v:
                return member
        return None


class ProductStage(StrEnum):
    TEST = "测试期"           # 新品测试/起步验证
    GROW = "推进期"           # 增长推进/冲刺
    HARVEST = "收割利润期"    # 达成预期
    MAINTAIN = "维持期"       # 超预期维持
    LIQUIDATING = "清货期"    # 清仓止损


# 旧值 → 新值映射，用于 DB 存量数据和 long_term_config 旧值的透明转换
STAGE_OLD_TO_NEW: dict[str, str] = {
    # 7 值旧系统 → 当前
    "测试期": "测试期", "起步期": "测试期",
    "进展期": "推进期", "冲刺期": "推进期",
    "达成期": "收割利润期",
    "超预期": "维持期",
    # 5 值旧系统 → 当前
    "测试": "测试期",
    "推进": "推进期",
    "收割": "收割利润期",
    "维持": "维持期",
    "清货": "清货期",
    "清仓": "清货期",
}

# 产品定位旧值 → 新值透明迁移（2026-06 3 档→4 档，枚举值带 (Px) 后缀）
# 同时兼容「中间态裸中文 4 档」（无后缀）以防早期存量
LEVEL_OLD_TO_NEW: dict[str, str] = {
    # 旧 3 档 → 4 档（腰部统一迁 P2 常规产品，与兜底默认一致）
    "头部": "战略级产品 (P0)",
    "腰部": "常规产品 (P2)",
    "长尾": "长尾产品 (P3)",
    # 中间态裸中文 → 带后缀标准值
    "战略级产品": "战略级产品 (P0)",
    "重点产品": "重点产品 (P1)",
    "常规产品": "常规产品 (P2)",
    "长尾产品": "长尾产品 (P3)",
}

# 产品定位（带后缀枚举值）→ ERP code（与 text_utils._PRODUCT_POSITION_MAP 共用真源）
PRODUCT_LEVEL_TO_CODE: dict[str, str] = {
    "战略级产品 (P0)": "P0_PRODUCT",
    "重点产品 (P1)": "P1_PRODUCT",
    "常规产品 (P2)": "P2_PRODUCT",
    "长尾产品 (P3)": "P3_PRODUCT",
}


def product_level_with_code(value: str) -> str:
    """产品定位→LLM 展示格式，确保 KB 规则命中。

    枚举值已自带 (Px)，直接返回；若传入裸中文/旧值则归一为带后缀标准值。
    """
    if not value:
        return value
    if value in PRODUCT_LEVEL_TO_CODE:
        return value
    return LEVEL_OLD_TO_NEW.get(value, value)


class SeasonStage(StrEnum):
    OFF_SEASON = "淡季"
    PEAK_PREP = "旺季准备"
    PEAK = "大旺季"
    PEAK_END = "旺季末期"


class AdPurpose(StrEnum):
    TRAFFIC = "引流型"
    CONVERSION = "转化型"
    RANKING = "排名型"
    PROFIT = "盈利型"


class TargetKeywordStrategy(StrEnum):
    """已选关键词类别 — 运营在策略层选择的投放方向"""
    BROAD = "大词"
    LONG_TAIL = "长尾词"
    COMPETITOR = "竞品词"
    BRAND = "品牌词"
    CUSTOM = "自定义"


# ── 选项定义 — 前端用 ──────────────────────────────────────


class StrategyDimension(BaseModel):
    """战略层单个维度"""
    id: str
    label: str
    description: str
    options: list[dict]  # [{"id":"P0","label":"核心爆款","description":"..."}, ...]
    selection_type: str = "radio"  # 互斥单选


class TacticsDimension(BaseModel):
    """策略层单个维度"""
    id: str
    label: str
    description: str
    options: list[dict]
    selection_type: str = "multi_select"  # 可多选
    recommendations: list[str] = Field(default_factory=list)  # LLM推荐的选项id
    recommendation_reason: str = ""


class ProductIdentityMixin(BaseModel):
    shop_id: int | None = Field(
        default=None,
        validation_alias=AliasChoices("shop_id", "_shopId", "shopId"),
    )
    parent_seller_sku: str | None = Field(
        default=None,
        validation_alias=AliasChoices("parent_seller_sku", "_parentSellerSku", "parentSellerSku"),
    )


# ── Layer 1.1 战略层 请求/响应 ─────────────────────────────


class StrategyOptionsResponse(BaseModel):
    """GET 战略层选项"""
    asin: str
    dimensions: list[StrategyDimension]
    current_selection: dict | None = None  # 从long_term_config加载 {product_level, product_stage, season_stage}
    data_ok: bool = True
    missing_fields: list[str] = []
    days: int = 7
    partial_failures: list[str] = Field(default_factory=list)
    data_freshness: str = "fresh"


class StrategyConfirmRequest(ProductIdentityMixin):
    """POST 确认战略层选择"""
    asin: str
    product_level: ProductLevel
    product_stage: ProductStage
    season_stage: SeasonStage


class StrategyConfirmResponse(BaseModel):
    asin: str
    accepted: bool
    config_saved: bool
    next_layer: str = "tactics"
    reject_reason: str | None = None


# ── Layer 1.2 策略层 请求/响应 ─────────────────────────────


class TacticsOptionsResponse(BaseModel):
    """GET 策略层选项（含AI推荐）"""
    asin: str
    dimensions: list[TacticsDimension]
    strategy_context: StrategyConfirmRequest | None = None
    current_selection: dict | None = None
    target_scores: list[dict] = Field(default_factory=list)  # P1 广告目的评分卡片
    keyword_analysis: list[dict] = Field(default_factory=list)  # P1 关键词AI分类
    scoring_error: str = ""  # AI 评分失败原因（purpose-agent 超时/不可达时非空，前端据此提示重试）
    llm_status: str = "ok"
    data_completeness: dict = Field(default_factory=dict)
    partial_failures: list[str] = Field(default_factory=list)
    data_freshness: str = "fresh"


class TacticsConfirmRequest(ProductIdentityMixin):
    """POST 确认策略层选择"""
    asin: str
    ad_purposes: list[AdPurpose]
    target_keyword_strategy: list[TargetKeywordStrategy] = Field(default_factory=list)


class TacticsConfirmResponse(BaseModel):
    asin: str
    accepted: bool
    config_saved: bool
    next_layer: str = "diagnosis"


# ── Layer 1.3 诊断层 响应 ──────────────────────────────────


class DiagnosisResponse(BaseModel):
    """GET 诊断数据（只读）"""
    asin: str
    data_summary: dict = Field(default_factory=dict)
    data_completeness: dict = Field(default_factory=dict)
    strategy_context: StrategyConfirmRequest | None = None
    tactics_context: TacticsConfirmRequest | None = None
    alert_signals: list[str] = Field(default_factory=list)
    metric_board: dict | None = None  # 北极星指标看板
    query_plan: list[dict] = Field(default_factory=list)  # 本次使用的元脚本清单
    keywords: list[dict] = Field(default_factory=list)  # 逐词监控表
    trend_data: list[dict] = Field(default_factory=list)  # 30天趋势数据
    partial_failures: list[str] = Field(default_factory=list)
    data_freshness: str = "fresh"


# ── Layer 1.4 执行层 请求/响应 ─────────────────────────────


class ExecutionDirection(BaseModel):
    """单个方向的评分+推荐信息"""
    id: str
    label: str
    suitability_score: float
    suitability: str  # recommended / available / not_recommended
    reason: str
    recommended: bool = False  # LLM是否推荐勾选
    recommendation_reason: str = ""
    default_sub_options: dict = Field(default_factory=dict)


class ExecutionOptionsResponse(BaseModel):
    """GET 执行层选项（含AI推荐）"""
    asin: str
    directions: list[ExecutionDirection]
    recommended_directions: list[str] = Field(default_factory=list)
    recommendation_summary: str = ""
    strategy_context: StrategyConfirmRequest | None = None
    tactics_context: TacticsConfirmRequest | None = None
    query_plan: list[dict] = Field(default_factory=list)  # 本次使用的元脚本清单
    llm_status: str = "ok"  # ok | degraded | blocked
    data_completeness: dict = Field(default_factory=dict)
    partial_failures: list[str] = Field(default_factory=list)
    data_freshness: str = "fresh"


class ExecutionSelectRequest(ProductIdentityMixin):
    """POST 确认执行层选择"""
    asin: str
    selected_directions: list[str]
    sub_options: dict = Field(default_factory=dict)


class ExecutionSelectResponse(BaseModel):
    asin: str
    accepted: bool
    next_layer: str = "validation"


# ── P3 上游推荐 — 目标 ACOS ──────────────────────────────────


class TargetAcosRequest(ProductIdentityMixin):
    """目标 ACOS 推荐请求"""
    asin: str
    days: int = 7


class TargetAcosStep(BaseModel):
    """推理链单步"""
    step: int
    rule: str = ""
    description: str = ""
    result: str = ""


class TargetAcosRecommendation(BaseModel):
    """目标 ACOS 推荐结果"""
    asin: str
    recommended_target: int = 0
    confidence: str = "medium"  # high / medium / low
    reasoning_chain: list[TargetAcosStep] = Field(default_factory=list)
    constraint_violations: list[str] = Field(default_factory=list)
    manual_override: bool = False  # 运营手动设定值时返回 True


class TargetAcosOverrideRequest(ProductIdentityMixin):
    """运营手动设定目标 ACOS"""
    asin: str
    value: int  # 5, 10, 15, ..., 35, 40


# ── P3 上游推荐 — 预算 & Bid ──────────────────────────────────


class BudgetBidRequest(ProductIdentityMixin):
    """预算/Bid 推荐请求"""
    asin: str
    days: int = 7


class BudgetRecommendationDetail(BaseModel):
    """预算调整详情"""
    current: float = 0.0
    suggested: float = 0.0
    direction: str = "maintain"  # increase / decrease / maintain
    magnitude_pct: float = 0.0
    reason: str = ""
    manual_override: bool = False  # 运营手动设定值时返回 True


class BudgetBidRecommendation(BaseModel):
    """预算 & Bid 推荐结果"""
    asin: str
    budget_recommendation: BudgetRecommendationDetail = Field(default_factory=BudgetRecommendationDetail)
    summary: str = ""
    confidence: str = "medium"  # high / medium / low


class BudgetOverrideRequest(ProductIdentityMixin):
    """运营手动设定日预算"""
    asin: str
    value: float  # 日预算金额（USD）


# ── P3 统一推荐（LLM驱动）───────────────────────────────────


class UnifiedRecommendRequest(ProductIdentityMixin):
    """P3 统一推荐请求"""
    asin: str
    refresh: bool = False  # True = 跳过缓存，强制 LLM
    days: int = 7


class TargetAcosResult(BaseModel):
    """目标 ACOS 推荐结果（LLM 输出）"""
    recommended_target: int = 0
    reasoning: str = ""
    confidence: str = "medium"  # high / medium / low
    manual_override: bool = False  # 运营手动设定时为 True


class BudgetBidResult(BaseModel):
    """预算/Bid 推荐结果（LLM 输出）"""
    current: float = 0.0
    suggested: float = 0.0
    direction: str = "maintain"  # increase / decrease / maintain
    magnitude_pct: float = 0.0
    reason: str = ""
    manual_override: bool = False  # 运营手动设定时为 True


class UnifiedRecommendResponse(BaseModel):
    """P3 统一推荐响应"""
    asin: str
    target_acos: TargetAcosResult = Field(default_factory=TargetAcosResult)
    budget_bid: BudgetBidResult = Field(default_factory=BudgetBidResult)
    overall_reasoning: str = ""
    risk_warnings: list[str] = Field(default_factory=list)
    from_cache: bool = False
    cached_until: str = ""  # ISO timestamp
    partial_failures: list[str] = Field(default_factory=list)
    data_freshness: str = "fresh"
    status: str = "ok"  # ok | blocked (llm skipped)
    llm_status: str = "ok"
    message: str = ""
    data_completeness: dict = Field(default_factory=dict)
    missing_required_labels: list[str] = Field(default_factory=list)


# ── 向导状态 ───────────────────────────────────────────────


class WizardStateResponse(BaseModel):
    """当前ASIN的完整向导状态"""
    asin: str
    current_layer: str  # strategy / tactics / diagnosis / execution / validation
    layers_completed: list[str] = Field(default_factory=list)
    strategy: StrategyConfirmRequest | None = None
    tactics: TacticsConfirmRequest | None = None
    execution: ExecutionSelectRequest | None = None
    long_term_config_exists: bool = False
    last_updated: str = ""
    days: int = 7
    # 运营手动设置值 (供前端"当前状态"只读展示;均仅显示 override,未设时 None)
    target_acos_override: int | None = None        # 来源: state.get_target_acos_override
    daily_budget_override: float | None = None     # 来源: long_term.daily_budget_override


# ── 长期配置 ───────────────────────────────────────────────


class LongTermConfigResponse(BaseModel):
    """持久化的战略+策略配置"""
    asin: str
    product_level: ProductLevel | None = None
    product_stage: ProductStage | None = None
    season_stage: SeasonStage | None = None
    ad_purposes: list[AdPurpose] = Field(default_factory=list)
    target_keyword_strategy: list[TargetKeywordStrategy] = Field(default_factory=list)
    last_modified: str = ""


class LongTermConfigUpdateRequest(ProductIdentityMixin):
    """手动更新长期配置"""
    product_level: ProductLevel | None = None
    product_stage: ProductStage | None = None
    season_stage: SeasonStage | None = None
    ad_purposes: list[AdPurpose] | None = None
    target_keyword_strategy: list[TargetKeywordStrategy] | None = None
