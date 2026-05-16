from pydantic import BaseModel, Field
from typing import Literal


class SubOptionConfig(BaseModel):
    """子选项配置模型"""
    id: str
    type: Literal["integer", "percent", "multi_choice"]
    label: str
    default: int | list[str] | float = 0
    min: int | float | None = None
    max: int | float | None = None
    options: list[dict] | None = None


class DirectionConstraint(BaseModel):
    """方向联动约束"""
    id: str
    description: str
    condition: str
    level: Literal["force_correct", "suggest_optimize"]


class DirectionConfig(BaseModel):
    """广告方向配置"""
    id: str
    label: str
    description: str = ""
    sub_options: list[SubOptionConfig] = Field(default_factory=list)
    constraints: list[DirectionConstraint] = Field(default_factory=list)


class AdDirectionConfig(BaseModel):
    """广告方向配置根模型"""
    directions: list[DirectionConfig]


class RecommendRequest(BaseModel):
    """获取推荐的请求"""
    asin: str


class ValidateRequest(BaseModel):
    """校验方向的请求"""
    asin: str
    direction: str
    sub_options: dict = Field(default_factory=dict)
    long_term_tags: dict = Field(default_factory=dict)
    special_scenario: str = "无"


class ConfirmRequest(BaseModel):
    """确认方向的请求"""
    asin: str
    direction: str
    sub_options: dict = Field(default_factory=dict)
    long_term_tags: dict = Field(default_factory=dict)
    special_scenario: str = "无"
