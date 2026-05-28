from pydantic import BaseModel, Field
from typing import Literal, Any


ValidationLevel = Literal["force_correct", "suggest_optimize", "confirmed"]
DataCompletenessStatus = Literal["complete", "partial", "missing"]


class Evidence(BaseModel):
    """规则判定的数据证据"""
    current_value: float | str | None = None
    threshold: float | str | None = None
    detail: str | None = None


class ValidationItem(BaseModel):
    """单条规则校验结果"""
    rule_id: str
    level: ValidationLevel
    message: str
    display_message: str = ""
    evidence: Evidence | None = None
    suggestion: Any = None
    data_missing: bool = False


class DataCompleteness(BaseModel):
    """数据完整性报告"""
    status: DataCompletenessStatus = "complete"
    missing_fields: list[str] = Field(default_factory=list)
    stale_fields: list[str] = Field(default_factory=list)


class ValidationResult(BaseModel):
    """完整的方向校验结果"""
    asin: str
    direction: str
    overall_level: ValidationLevel = "confirmed"
    items: list[ValidationItem] = Field(default_factory=list)
    data_completeness: DataCompleteness = Field(default_factory=DataCompleteness)
