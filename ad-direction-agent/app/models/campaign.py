"""Campaign 分析数据模型 — 与 ASINData 独立共存，不同的聚合粒度。

campaign_key = child_asin + match_type + keyword_text
每行 = 一个广告活动的完整画像
"""

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
    """campaign_key = child_asin + match_type + keyword_text"""
    campaign_name: str
    campaign_key: str = ""
    campaign_id: str = ""                     # Doris 上下文 (placement 懒加载/回落依赖)
    child_asin: str
    seller_sku: str = ""
    keyword_text: str
    match_type: str = ""                     # EXACT / BROAD / PHRASE (Doris 上下文)
    current_bid: float = 0.0                 # Doris 上下文 (MCP 不返回)
    current_budget: float = 0.0              # MCP basic_info -> Doris 回落
    campaign_status: str = ""                # MCP basic_info -> Doris 回落
    days_online: int = -1                    # MCP basic_info；-1=未知(拿不到)，勿当"新活动"
    perf_7d: CampaignPerf = Field(default_factory=CampaignPerf)   # MCP product_report(7d)
    placements: dict[str, dict] = Field(default_factory=dict)     # MCP placement_report (懒加载)
    placement_data_available: bool = False   # 懒加载前为空
    # 元数据
    source: str = "mcp"                      # "mcp" | "doris" | "mixed"
    flags: list[str] = Field(default_factory=list)


class CampaignData(BaseModel):
    """顶层容器，对标 ASINData"""
    parent_asin: str
    total_campaigns: int = 0
    campaigns: list[CampaignUnit] = Field(default_factory=list)
    excluded: list[dict] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    fetch_source: str = "mcp"
