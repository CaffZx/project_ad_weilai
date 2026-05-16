from pydantic import BaseModel, Field
from typing import Literal


class DirectionScore(BaseModel):
    """单个方向适配度评分"""
    id: str
    label: str
    suitability_score: float
    suitability: Literal["recommended", "available", "not_recommended"]
    reason: str
    default_sub_options: dict = Field(default_factory=dict)


class DataSummary(BaseModel):
    """数据摘要"""
    keyword_count: int | None = None
    acos: float | None = None
    acos_target: float | None = None
    top_keyword_rank: int | None = None
    available_new_keywords: int | None = None
    avg_daily_sales_30d: float | None = None
    review_count: int | None = None
    rating: float | None = None
    refund_rate: float | None = None
    brand: str | None = None
    category_name: str | None = None
    product_level: str | None = None
    product_stage: str | None = None
    season_stage: str | None = None
    competition_count: int | None = None
    ad_spend: float | None = None
    inventory_qty: int | None = None
    price: float | None = None
    rising_keywords: int | None = None

    class Config:
        extra = "allow"


class Task(BaseModel):
    """决策任务"""
    priority: Literal["high", "medium", "low"] = "medium"
    action: str
    details: str = ""
    estimated_impact: str = ""


class DecisionPackage(BaseModel):
    """决策包"""
    direction: str
    tasks: list[Task] = Field(default_factory=list)
    key_metrics: dict = Field(default_factory=dict)


class RecommendResponse(BaseModel):
    """推荐接口响应"""
    asin: str
    recommended_direction: str
    recommendation_reason: str
    recommendation_level: Literal["force_correct", "suggest_optimize", "confirmed"] = "suggest_optimize"
    directions: list[DirectionScore] = Field(default_factory=list)
    data_summary: DataSummary = Field(default_factory=DataSummary)
    data_completeness: dict = Field(default_factory=dict)


class ConfirmResponse(BaseModel):
    """确认接口响应"""
    asin: str
    direction: str
    sub_options: dict = Field(default_factory=dict)
    decision_package: DecisionPackage = Field(default_factory=DecisionPackage)


# ── LLM 分析 ────────────────────────────────────────────────

class LLMReportRequest(BaseModel):
    """LLM 综合分析请求"""
    asin: str
    data_summary: dict = Field(default_factory=dict)
    scores: list[dict] = Field(default_factory=list)
    validations: dict[str, dict] = Field(default_factory=dict)
    decisions: dict[str, dict] = Field(default_factory=dict)


class LLMDirectionAnalysis(BaseModel):
    """LLM 单方向分析"""
    direction: str = ""
    analysis: str = ""
    suggestions: list[str] = Field(default_factory=list)


class LLMActionPriority(BaseModel):
    """LLM 优先级行动项"""
    action: str = ""
    priority: str = ""
    expected_impact: str = ""


class LLMReportResponse(BaseModel):
    """LLM 综合分析响应"""
    asin: str
    overall_analysis: str = ""
    direction_analyses: list[LLMDirectionAnalysis] = Field(default_factory=list)
    action_priorities: list[LLMActionPriority] = Field(default_factory=list)
    risk_warnings: list[str] = Field(default_factory=list)
