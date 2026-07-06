"""Campaign 分析数据模型 — 与 ASINData 独立共存，不同的聚合粒度。

campaign_key = "活动名 × 子ASIN"，唯一标识一个广告活动。
每行 = 一个广告活动的完整画像。

同一个活动可能包含多个关键词，同一个关键词可能被多个活动投放——
仅靠 child_asin + match_type + keyword 不能区分。

──────────────────────────────────────────────────────────────────────
【关键词类型命名规范】(本模块强制，避免与既有 4 个混名概念再撞车)

  既有(勿在本模块复用其名)：
    ① 术语表       layer_options[tactics.keyword_types] / kb_loader.ENUM_MAP(中/英枚举)
    ② ASIN 策略选择 keyword_types: list  (该 ASIN 该投哪些类型，中文) → 本模块用 target_keyword_strategy
    ③ ASIN 简并串   ASINData.keyword_type: str (② join 成中文逗号串)
    ④ 逐词 AI 分类  keyword_analysis[].strategy_type: str (英文 Broad/Long-tail) → 本模块用 keyword_class

  本模块统一命名（对齐全项目规范）：
    target_keyword_strategy: list[str] ← 取自 ②，中文（概念1: 关键词策略）
    keyword_class: str                ← 取自 ④，值转中文  （概念3: 关键词类别）
    match_type: str                   ← 概念2: 匹配类型
    is_core: bool                     ← KB 21 Custom 核心词保护;本期留 False
──────────────────────────────────────────────────────────────────────
"""

from typing import Literal

from pydantic import BaseModel, Field


class CampaignPerf(BaseModel):
    """活动性能指标（单个时间窗）"""
    clicks: int = 0
    cost: float = 0.0
    sales: float = 0.0
    orders: int = 0
    acos: float | None = None                # 百分数口径，如 25.0 表示 25%
    cvr: float | None = None                 # 百分数口径
    cpc: float | None = None                 # KB 19 Bid 规则依赖
    ctr: float | None = None                 # 百分数口径
    impressions: int = 0


class CampaignUnit(BaseModel):
    """单条广告活动画像。campaign_key = "活动名 × 子ASIN"，全局唯一。"""
    campaign_name: str
    campaign_key: str = ""
    campaign_id: str = ""                     # Doris 上下文 (placement 懒加载/回落依赖)
    keyword_id: str = ""                       # Doris 上下文 (Amazon 关键词 ID, 写 ERP pending 用)
    child_asin: str
    seller_sku: str = ""
    keyword_text: str
    match_type: str = ""                     # EXACT / BROAD / PHRASE (Doris 上下文)
    current_bid: float = 0.0                 # MCP ad_campaign_basic_info「关键词BID」(缺失则 0)
    current_budget: float = 0.0              # MCP basic_info -> Doris 回落
    campaign_status: str = ""                # MCP basic_info -> Doris 回落
    days_online: int = -1                    # MCP basic_info；-1=未知(拿不到)，勿当"新活动"
    perf_7d: CampaignPerf = Field(default_factory=CampaignPerf)   # MCP product_report(7d)
    placements: dict[str, dict] = Field(default_factory=dict)     # MCP placement_report (懒加载)
    placement_data_available: bool = False   # 懒加载前为空
    # 广告位加价比例：MCP basic_info (头部/商品/其他位置加价比例)
    tos_bid_pct: float = 0.0                 # 头部位置加价比例 %
    pp_bid_pct: float = 0.0                  # 商品位置加价比例 %
    ros_bid_pct: float = 0.0                 # 其他位置加价比例 %
    # 自然排名（周排名）：仅 EXACT 填充；主源 MCP keyword_child_asins(有近次)，own_keyword_flow 补缺
    natural_rank: int | None = None          # 当前自然排名
    near_natural_rank: int | None = None     # 近次爬取排名（仅 child_asins 有）
    rank_change: int | None = None           # near - cur，正数=排名上升；own 补缺词为 None
    # 组合分类(AI 自造 4 类逻辑分类,非亚马逊后台 Portfolio):
    #   精准主力组 / 自动广泛组 / 精准测试组 / 低价捡漏组；空串=未分类
    portfolio: str = ""
    # 元数据
    source: str = "mcp"
    flags: list[str] = Field(default_factory=list)


class CampaignData(BaseModel):
    """顶层容器，对标 ASINData"""
    parent_asin: str
    shop_id: int = 0
    parent_seller_sku: str = ""
    site_code: str = "Amazon_US"
    total_campaigns: int = 0
    campaigns: list[CampaignUnit] = Field(default_factory=list)
    excluded: list[dict] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    fetch_source: str = "mcp"


# ── Campaign LLM 分析引擎模型 ──────────────────────────────────────────────


class CampaignStrategyContext(BaseModel):
    """ASIN 级共享上下文，注入每批 LLM 分析。

    从 ASINData + long_term_config + keyword_analysis 组装，不含 campaign 级字段。
    """
    parent_asin: str = ""
    product_stage: str = ""
    product_level: str = ""
    season_stage: str = ""
    ad_purposes: list[str] = Field(default_factory=list)
    ad_directions: list[str] = Field(default_factory=list)   # 广告方向选择(运营 tab4 已选)：取自 workflow_state.execution.selected_directions
    target_keyword_strategy: list[str] = Field(default_factory=list)
    # 诊断层 (ASINData)
    margin: float | None = None
    natural_order_ratio: float | None = None
    rating: float | None = None
    refund_rate: float | None = None
    inventory_qty: int | None = None
    inventory_days: float | None = None          # ← 计算: qty / avg_daily_sales
    avg_daily_sales_30d: float | None = None
    target_acos: int | None = None
    daily_budget: float | None = None            # 目标/当前每日预算基准：long_term daily_budget_override → asin_data.daily_budget → 兜底
    daily_budget_source: str = ""                # "override" | "asin_data" | "fallback_spend_x1.15" | "" (全失败)
    warning_flags: list[str] = Field(default_factory=list)


class CampaignAdjustmentItem(BaseModel):
    """LLM 输出的单活动调整建议 (对齐 KB 21 §6 / 22 §4)"""
    campaign_name: str
    campaign_key: str = ""
    campaign_id: str = ""                        # Doris 上下文, 写 ERP campaign_pending 用
    child_asin: str = ""
    seller_sku: str = ""                         # Doris 上下文
    keyword_text: str = ""
    keyword_id: str = ""                         # Doris 上下文, 写 ERP keyword_pending 用
    match_type: str = ""
    keyword_class: str = ""
    is_core: bool = False
    # 自然排名（仅 EXACT；代码回填，LLM 不输出）
    natural_rank: int | None = None
    near_natural_rank: int | None = None
    rank_change: int | None = None
    action: str = ""                             # eliminate_to_low_bid_pool | adjust_bid | adjust_budget | adjust_placement | keep
    direction: dict[str, str] = Field(default_factory=dict)
    triggered_rule: str = ""
    reason: str = ""
    evidence: list[str] = Field(default_factory=list)
    confidence: str = "medium"
    current_budget: float | None = None
    current_bid: float | None = None
    proposed_budget: float | None = None
    proposed_bid: float | None = None
    placement_adjustments: list[dict] = Field(default_factory=list)
    negative_keywords: list[dict] = Field(default_factory=list)
    elimination_values: dict | None = None
    round_votes: dict[str, str] = Field(default_factory=dict)
    review_level: str = "MANUAL_REVIEW"
    # 组合分类(AI 自造 4 类,非后台 Portfolio): 精准主力组 / 自动广泛组 / 精准测试组 / 低价捡漏组
    ai_portfolio_class: str = ""
    # KB 18/21 原字段(后台真实 Portfolio); 当前数据层无该字段,留空待后续接入
    portfolio_or_group: str = ""
    # 逐活动 7 天指标快照(代码回填自 CampaignUnit.perf_7d)→ 落 card.perf_json;
    # 淘汰卡借此存淘汰前花费,供 KB21§7 情况二复评读取(perf_json 列既有,零加列)
    perf_7d: dict = Field(default_factory=dict)
    # 活动上线天数（MCP ad_campaign_basic_info），-1=未知；供 KB 21 §2 新活动保护
    days_online: int = -1
    # 距最近一次复评离池的天数，-1=从未复评/未知；供复评后 N 天保护（防淘汰↔复评抖动）
    days_since_reactivation: int = -1


class CampaignBatchResult(BaseModel):
    """单批 LLM 返回 (投票中间产物)"""
    batch_id: int = 0
    round_number: int = 0
    items: list[CampaignAdjustmentItem] = Field(default_factory=list)
    raw_llm_output: str = ""
    temperature: float = 0.3
    llm_success: bool = True
    llm_error: str = ""


class NewCampaignCandidate(BaseModel):
    """新增活动候选词（数据层产出，喂给 LLM 前的中间结构）。

    keyword_class / match_type 由 LLM 基于 KB 06 判定，不在候选阶段填。
    """
    keyword_text: str
    search_volume: int = 0                  # flow_keywords 提供
    natural_rank: int | None = None         # own_keyword_flow 提供，无则 None
    week_rank: int | None = None            # own_keyword_flow「词的周排名」（周排名信号，KB28 §2 相关性判据之一）
    week_search_volume: int | None = None   # own_keyword_flow「周搜索量」（与 flow 的搜索量互补）
    # ★KB 16 §3「建议竞价(suggestedBid)」：MCP whp_amazon_advert_keyword_suggest_bid 批量查询填入。
    #   命中→_calc_initial_bid 按公式 min(0.5, bid×0.5) 计算；未命中→降级占位 $0.30。
    suggested_bid: float | None = None
    trigger_scene: str = ""                 # KB 16 §1 场景码（展示标签，非筛选门禁）
    source: str = "flow"                    # 候选来源: flow / ranking_opportunity / competitor (多源配额分桶用)
    source_reason: str = ""                 # 来源说明 (如 "竞品B0XXX反查·搜索量1200")，喂 LLM 作参考


class NewCampaignItem(BaseModel):
    """新增活动建议（LLM 判 keyword_class/取舍/文本 + 代码补齐数值字段）。"""
    keyword_text: str
    child_asin: str = ""                    # 投放目标子 ASIN（代码选历史活动数最多/花费最高的子 ASIN，非父 ASIN）
    action_type: str = "create_campaign"    # KB 16 §5: create_campaign | create_ad_group
    campaign_name: str                      # 代码生成: {匹配类型中文}-{kw}-{YYYY-MM-DD}
    campaign_type: str = ""                 # "精准广告" | "广泛广告"
    match_type: str = ""                    # EXACT | BROAD | PHRASE（代码从 keyword_class 推导）
    keyword_class: str = ""                 # ★LLM 判(KB 06): generic/long_tail/competitor/brand/custom
    relevance_tier: str = ""                # ★LLM 判(KB28 §2): R1精确/R2扩展/R3试探（相关性档位留痕）
    keywords_or_targets: list[str] = Field(default_factory=list)
    proposed_daily_budget: float = 3.0      # KB 16 §2（代码定）
    proposed_base_bid: float = 0.30         # KB 16 §3（代码定），真实建议竞价到位后重算
    primary_placement: str = "头部"          # 仅 EXACT；代码默认"头部"(KB 16 §4 首轮主投位)
    placement_adjustment: str = "N/A"       # 首轮统一不输出 (KB 16 §5)
    negative_strategy: str = ""             # 仅 BROAD/PHRASE 必填 (KB 16 §5, LLM 产出)
    trigger_scene: str = ""                 # KB 16 §1 场景码（展示标签）
    source: str = ""                        # 候选来源 flow/ranking_opportunity/competitor（输出排序 H5 用）
    reason: str = ""                        # LLM 文本
    evidence: list[str] = Field(default_factory=list)
    ai_portfolio_class: str = ""            # 归组: 精准测试组 / 自动广泛组
    confidence: str = "medium"              # 双轮 keyword_class 一致=high, 不一致=low
    review_level: str = "MANUAL_REVIEW"
    suggested_bid_source: str = "placeholder"   # "amazon_api" | "placeholder" | "actual_cpc"


class CampaignStrategicOverview(BaseModel):
    """策略总览(执行总纲) — 明细前的宏观方向。

    数字(facts)由 Python 确定性算；三段叙事 + posture_brief 由 LLM 生成。
    LLM 失败时 generated_by='fallback'，仅 facts 可用、文本段为空。
    """
    facts: dict = Field(default_factory=dict)        # 现状数字(确定性)：仅作 LLM 判断输入，不在总览展示
    assessment_text: str = ""                        # 1. 核心判断(KB×现状匹配 → 关键矛盾/机会 + 定调)
    direction_text: str = ""                         # 2. 宏观方向 + 原因
    posture_brief: str = ""                          # 注入后续逐活动分析的判断基准(指令式)
    generated_by: str = "ai"                         # "ai" | "fallback"


class CampaignAnalysisResult(BaseModel):
    """顶层分析返回"""
    parent_asin: str = ""
    shop_id: int = 0
    shop_account: str = ""
    parent_seller_sku: str = ""
    site_code: str = "Amazon_US"
    days: int = 7
    run_id: str = ""                                                  # 本次分析唯一 ID (e.g. "20260601T123456Z")，前端 localStorage 隔离用
    total_campaigns: int = 0
    adjustments: list[CampaignAdjustmentItem] = Field(default_factory=list)
    new_campaigns: list[NewCampaignItem] = Field(default_factory=list)  # KB 16 新增活动建议（独立分析线）
    new_campaigns_warnings: list[str] = Field(default_factory=list)     # 新增线专属 warning（聚合也进 warnings）
    skipped_campaigns: list[dict] = Field(default_factory=list)       # 整批 LLM 失败或未返回，需人工补救
    strategic_overview: dict | None = None                            # 策略总览(执行总纲)；失败时为 facts-only 或 None
    synthesis: dict | None = None                                     # AI 汇总分析 (groups + special_cases)；失败时为 None
    budget_summary: dict | None = None                                # 4 组合预算汇总 + 占比 + 告警；开关关闭时 None
    summary: dict = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    sanity_check_passed: bool = True
    data_unavailable: bool = False                                    # True=上游数据(数仓/MCP)拉取失败/超时，本次未真正分析；区别于"无调整/无活动"业务态，供 ERP 门禁、批量统计与告警区分
    llm_rounds_completed: int = 2
    rounds_detail: dict = Field(default_factory=dict)


# ── 审核占位（本期 stub，后续生产化写表 + 推 ERP）────────────────────────────


class CampaignDecision(BaseModel):
    """单活动审核决定。"""
    campaign_key: str
    decision: Literal["approve", "reject"]
    note: str = ""


class CampaignConfirmRequest(BaseModel):
    """运营批量审核提交。"""
    asin: str
    days: int = 7
    run_id: str = ""                          # 关联到 analyze 那次的 run_id；生产化时作幂等键
    decisions: list[CampaignDecision] = Field(default_factory=list)
    operator: str = ""


