"""反馈收集数据模型"""

from pydantic import AliasChoices, BaseModel, Field


class FeedbackModule(BaseModel):
    """单个模块的反馈条目"""
    module: str                                    # "广告目的推荐" | "目标关键词推荐" | "目标ACOS推荐" | "每日预算推荐" | "广告方向推荐"
    ai_judgment: str | list[str] | None = None     # AI 推荐值（前端自动填入）
    human_judgment: str | list[str] | None = None  # 人工选择/设定值，无覆盖时为 "未手动设定"
    adoption: str = ""                             # "采纳" | "可以优化" | "完全不采纳"（必填）
    correction_reason: str = ""                    # 纠错理由（可选）
    notes: str = ""                                # 备注/建议（可选）


class FeedbackSubmission(BaseModel):
    """一条完整的反馈记录"""
    id: str = ""                                   # {timestamp}_{asin}
    parent_asin: str
    shop_id: int | None = Field(
        default=None,
        validation_alias=AliasChoices("shop_id", "_shopId", "shopId"),
    )
    parent_seller_sku: str | None = Field(
        default=None,
        validation_alias=AliasChoices("parent_seller_sku", "_parentSellerSku", "parentSellerSku"),
    )
    session_start: str = ""                        # 前端首次加载的 ISO 时间戳
    submitted_at: str = ""                         # 提交时的 ISO 时间戳
    has_error: bool = False
    error_info: str | None = None
    modules: list[FeedbackModule] = Field(default_factory=list)


class FeedbackListResponse(BaseModel):
    """反馈列表查询响应"""
    asin: str
    total: int
    items: list[FeedbackSubmission]
