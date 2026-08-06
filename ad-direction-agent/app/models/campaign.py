"""Campaign 分析数据模型 — 与 ASINData 独立共存，不同的聚合粒度。

campaign_key = "活动名 × 子ASIN[#match_type#keyword_id]"，关键词级标识。
每行 = 一个关键词投放单元的完整画像。

⚠ 粒度说明：campaign_key 已从活动级改为关键词级（后缀 #match_type#keyword_id），
以消除同活动同词不同匹配类型（BROAD/PHRASE）的 unit_by_key 碰撞。
但下游 card / 预算 / 广告位 / 复盘仍按 campaign_id 聚合（一个活动一张 card）。
同活动多个关键词单元建议合并时，budget/campaign_pending/placements 存在后写覆盖，
synthesis key_to_card 映射可能漏掉非主 key。
长期应拆为 campaign_key（活动级） + campaign_unit_key（关键词级），分别用于聚合与定位。

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
    campaign_created_at: str = ""            # MCP basic_info「广告活动创建日期」；供 32号§2.4 原生型观察窗
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
    # ad_campaign_list 返回的当前真实广告组合名称；用于尊重运营手动挪组。
    current_portfolio_name: str = ""
    current_group_type: str = ""          # 由 current_portfolio_name 经公共 helper 解析；UNKNOWN=未识别
    # 元数据
    source: str = "mcp"
    flags: list[str] = Field(default_factory=list)


class CampaignData(BaseModel):
    """顶层容器，对标 ASINData"""
    parent_asin: str
    shop_id: int = 0
    shop_account: str = ""
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
    operating_mode: str = ""
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
    # ── ACOS 约束预计算（15号§4 + 03号§7，build_campaign_strategy_context 后立即填充）──
    effective_acos_tolerance: float | None = None   # 有效容忍上限（百分点），不是倍率
    tolerance_components: dict[str, float] = Field(default_factory=dict)  # 每个加/减项明细
    target_cpa: float | None = None                 # 目标 CPA = 平均订单金额 × target_acos
    avg_order_value: float | None = None            # 平均订单金额（7日 sales/orders）；无订单时 None
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
    # 代码规则判定的活动级目标组 ERP 码；空值表示本轮不执行挪组。
    target_campaign_group_type: str = ""
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
    # 仅广泛流携带：LLM 标注后已由 reasoner 按原始搜索词报表回填并校验的提精准候选。
    exact_promotion_candidates: list["SearchTermPromotionCandidate"] = Field(default_factory=list)
    raw_llm_output: str = ""
    temperature: float = 0.3
    llm_success: bool = True
    llm_error: str = ""


class SearchTermPromotionCandidate(BaseModel):
    """广泛流 LLM 标注、代码按原始搜索词报告回填后的提精准候选。"""
    campaign_key: str
    campaign_name: str
    campaign_match_type: str
    search_term: str
    keyword_root: str = ""
    keyword_class: str = ""
    relevance_tier: str = ""
    reason: str = ""
    evidence: list[str] = Field(default_factory=list)
    clicks: int = 0
    orders: int = 0
    cost: float = 0.0
    sales: float = 0.0


class NewCampaignDecision(BaseModel):
    """已完成来源和匹配方式判断、待统一补齐执行参数的新增活动决策。"""
    keyword_text: str
    keyword_class: str = ""
    relevance_tier: str = ""
    source: str = ""
    trigger_scene: str = ""
    prescribed_match_type: Literal["EXACT", "BROAD"] = "BROAD"
    reason: str = ""
    evidence: list[str] = Field(default_factory=list)
    confidence: str = "medium"
    review_level: str = "MANUAL_REVIEW"
    negative_strategy: str = ""
    search_volume: int = 0
    natural_rank: int | None = None
    suggested_bid: float | None = None
    color_flags: dict[str, bool] | None = None   # LLM 输出：{black: true, red: true}
    holiday_flags: dict[str, bool] | None = None  # LLM 输出：{halloween: true}
    assigned_child_asin: str = ""                 # 代码回填：颜色→子ASIN 指派（空=用默认 target_child_asin）


class NewCampaignCandidate(BaseModel):
    """新增活动候选词（NewKeywordRecord → LLM 投影结构）。

    keyword_class / match_type 由 LLM 基于 KB 06 判定，不在候选阶段填。
    """
    keyword_text: str
    search_volume: int = 0                  # flow_keywords 提供
    search_rank: int | None = None          # flow_keywords.搜索排名
    natural_rank: int | None = None         # 当前自然位（统一业务口径）
    rank_trend: str | None = None           # 近7天自然位趋势, "5→6→9→3→3→6→6"
    rank_tier: str | None = None            # history.crawNatureRankPosition
    sponsored_rank: int | None = None       # history.crawSpRank
    week_rank: int | None = None            # own_keyword_flow.词的周排名
    week_search_volume: int | None = None   # own_keyword_flow.周搜索量
    history_state: str = ""                 # 趋势/分位/广告排位补充数据状态
    # ★KB 16 §3「建议竞价(suggestedBid)」：MCP whp_amazon_advert_keyword_suggest_bid 批量查询填入。
    #   命中→_calc_initial_bid 按公式 min(0.5, bid×0.5) 计算；未命中→降级占位 $0.30。
    suggested_bid: float | None = None
    trigger_scene: str = ""                 # KB 16 §1 场景码（展示标签，非筛选门禁）
    source: str = "flow"                    # 候选来源: flow / ranking_opportunity / competitor (多源配额分桶用)
    source_reason: str = ""                 # 来源说明 (如 "竞品B0XXX反查·搜索量1200")，喂 LLM 作参考


class NewKeywordRecord(BaseModel):
    """一词一份，跨源归一化后的统一事实对象。"""
    keyword_text: str
    # flow_keywords
    search_volume: int = 0
    search_rank: int | None = None
    # own_keyword_flow
    week_rank: int | None = None
    week_search_volume: int | None = None
    own_natural_rank: int | None = None          # own 自然位，预过滤/补漏信号
    own_rank_tier: str | None = None             # own 自然位排位
    is_own_only: bool = False                    # own 有、flow 无的补漏词
    # erp_listing_asin_keyword_rank_history enrichment
    natural_rank: int | None = None              # history.crawNatureRank 最新日
    rank_trend: str | None = None                # 近7天自然位趋势, "5→6→9→3→3→6→6"
    rank_tier: str | None = None                 # history.crawNatureRankPosition
    sponsored_rank: int | None = None            # history.crawSpRank
    history_state: str = "not_queried"           # not_queried | ok | empty | query_failed | not_eligible
    # code-generated
    source: str = "flow"                         # flow / ranking_opportunity
    source_reason: str = ""
    trigger_scene: str = ""


class NewKeywordData(BaseModel):
    """一次新增扩词分析的容器。"""
    parent_asin: str
    shop_id: int = 0
    shop_account: str = ""
    parent_seller_sku: str = ""
    site_code: str = "Amazon_US"
    records: list[NewKeywordRecord] = Field(default_factory=list)
    search_volume_map: dict[str, int] = Field(default_factory=dict)
    total_discovered: int = 0
    total_own: int = 0
    history_queried: int = 0
    history_success: int = 0
    errors: list[str] = Field(default_factory=list)


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
    allow_growth_analysis: bool = True               # 策略总览增长门禁：LLM 明确判断不应当新增扩词/淘汰复评时输出 false；默认 true(fail-open)
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
    campaign_group_targets: dict[str, str] = Field(default_factory=dict)
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
    llm_rounds_completed: int = 0
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


