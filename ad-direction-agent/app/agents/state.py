"""LangGraph AgentState — 与 HTTP 分步请求对齐。"""

from typing import Any, TypedDict


class AgentState(TypedDict, total=False):
    asin: str
    days: int
    refresh: bool
    endpoint: str
    req: Any
    result: Any
