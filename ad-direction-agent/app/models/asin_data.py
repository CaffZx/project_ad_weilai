from pydantic import BaseModel, Field
from typing import Optional


class AdData(BaseModel):
    """广告表现数据"""
    acos: Optional[float] = None
    acos_7d: Optional[float] = None
    ctr: Optional[float] = None
    cvr: Optional[float] = None
    net_cvr: Optional[float] = None
    tacos: Optional[float] = None
    tos_ratio: Optional[float] = None
    pp_ratio: Optional[float] = None
    ros_ratio: Optional[float] = None
    impressions: Optional[int] = None
    clicks: Optional[int] = None
    spend: Optional[float] = None
    sales: Optional[float] = None
    orders: Optional[int] = None
    daily_budget: Optional[float] = None
    cpc: Optional[float] = None
    daily_ad_spend_ratio: Optional[float] = None
    # 精准/非精准广告拆分（按 match_type 划分: EXACT vs BROAD+PHRASE）
    precision_acos: Optional[float] = None
    precision_spend: Optional[float] = None
    precision_cpc: Optional[float] = None
    precision_spend_ratio: Optional[float] = None
    precision_order_ratio: Optional[float] = None
    broad_acos: Optional[float] = None
    broad_spend: Optional[float] = None
    broad_cpc: Optional[float] = None
    broad_spend_ratio: Optional[float] = None
    broad_order_ratio: Optional[float] = None
    # 广告位拆分（按 placement 划分: TOS vs ROS，来自 placement_report）
    placement_tos_acos: Optional[float] = None
    placement_tos_cpc: Optional[float] = None
    placement_tos_spend_ratio: Optional[float] = None
    placement_ros_acos: Optional[float] = None
    placement_ros_cpc: Optional[float] = None
    placement_ros_spend_ratio: Optional[float] = None


class KeywordData(BaseModel):
    """关键词表现数据"""
    keyword: str = ""
    impressions: int = 0
    clicks: int = 0
    spend: float = 0.0
    orders: int = 0
    acos: Optional[float] = None
    cvr: Optional[float] = None
    bid: Optional[float] = None
    suggested_bid: Optional[float] = None
    search_rank: Optional[int] = None
    natural_rank: Optional[int] = None
    near_natural_rank: Optional[int] = None
    sp_rank: Optional[int] = None
    rank_change_14d: Optional[int] = None
    rank_change_7d: Optional[int] = None
    # 时间窗对比：近半窗 vs 前半窗（用于趋势选词）
    acos_recent: Optional[float] = None
    acos_prior: Optional[float] = None
    spend_recent: Optional[float] = None
    spend_prior: Optional[float] = None
    orders_recent: Optional[int] = None
    orders_prior: Optional[int] = None
    is_manual: bool = True
    match_type: str = ""  # BROAD / EXACT / PHRASE


class CompetitorData(BaseModel):
    """竞品数据"""
    asin: str = ""
    price: Optional[float] = None
    rating: Optional[float] = None
    review_count: Optional[int] = None
    bsr: Optional[int] = None
    estimated_sales_7d: Optional[int] = None
    price_change_7d: Optional[float] = None


class TrendPoint(BaseModel):
    """单日趋势数据点"""
    date: str = ""
    acos: Optional[float] = None
    cvr: Optional[float] = None
    ctr: Optional[float] = None
    cpc: Optional[float] = None
    orders: Optional[int] = None
    ad_orders: Optional[int] = None
    spend: Optional[float] = None


class SpecialSignals(BaseModel):
    """特殊信号检测"""
    inventory_days: Optional[float] = None
    in_transit_inventory: Optional[int] = None
    recent_rating_change: Optional[float] = None
    recent_negative_reviews: Optional[int] = None
    has_coupon: Optional[bool] = None
    has_lightning_deal: Optional[bool] = None
    listing_modified_recently: Optional[bool] = None
    threat_score: Optional[float] = None
    inventory_qty: Optional[int] = None


class ASINData(BaseModel):
    """ASIN 聚合数据模型 — Agent 校验所需的所有字段"""
    asin: str
    sku: Optional[str] = None
    parent_asin: Optional[str] = None
    shop_id: Optional[int] = None
    parent_seller_sku: Optional[str] = None

    # 产品基本信息
    days_since_launch: Optional[int] = None
    title: Optional[str] = None
    price: Optional[float] = None
    cost: Optional[float] = None
    fulfillment_fee: Optional[float] = None
    margin: Optional[float] = None

    # 销量数据
    avg_daily_sales_7d: Optional[float] = None
    avg_daily_sales_30d: Optional[float] = None
    sales_trend_30d: Optional[float] = None
    natural_order_ratio: Optional[float] = None

    # 评论数据
    rating: Optional[float] = None
    review_count: Optional[int] = None

    # 数据库扩展字段（来自数仓）
    refund_rate: Optional[float] = None
    profit_per_unit: Optional[float] = None
    brand: Optional[str] = None
    product_line: Optional[str] = None
    category_name: Optional[str] = None
    asin_star: Optional[float] = None

    # 广告数据
    ad_data: AdData = Field(default_factory=AdData)

    # 关键词数据
    keywords: list[KeywordData] = Field(default_factory=list)
    keyword_count: Optional[int] = None
    available_new_keywords: Optional[int] = None
    expand_keyword_candidates: list[dict] = Field(default_factory=list)

    # 竞品数据
    competitors: list[CompetitorData] = Field(default_factory=list)
    competitor_price_p25: Optional[float] = None
    competitor_price_p50: Optional[float] = None
    competitor_price_p75: Optional[float] = None

    # 特殊信号
    signals: SpecialSignals = Field(default_factory=SpecialSignals)

    # 年度/供应链数据
    annual_sales_target: Optional[int] = None
    annual_sales_achieved: Optional[int] = None
    supply_chain_priority: Optional[str] = None

    # 长周期标签输入
    product_level: Optional[str] = None
    product_stage: Optional[str] = None
    season_stage: Optional[str] = None
    ad_purpose: Optional[str] = None
    # 概念1: 关键词策略的简并串（逗号分隔，供 cross_tag 规则使用）
    target_keyword_strategy: Optional[str] = None

    # 数据质量标记
    data_missing: bool = False
    missing_fields: list[str] = Field(default_factory=list)
    stale_fields: list[str] = Field(default_factory=list)
    partial_failures: list[str] = Field(default_factory=list)
    data_freshness: str = "fresh"  # fresh | partial | stale_cache

    # 趋势数据
    trend: list[TrendPoint] = Field(default_factory=list)
