"""规则注册表 — 装饰器模式

用法:
    @validation_rule(rule_id="PN-1", direction="push_natural", priority=1)
    async def check_something(data: ASINData, thresholds: dict) -> ValidationItem | None:
        ...

规则注册表规则:
    - rule_id 全局唯一，重复会启动报错
    - direction: "push_natural" | "expand_keywords" | "optimize_acos" | "balance_maintain" | "cross_tag"
    - priority: 0=最高(强制修正), 1=中(建议优化), 2=低(确认合理)
    - 函数返回 None 表示跳过（数据不足），返回 ValidationItem 表示判定结果
"""

from functools import wraps
from typing import Callable, Coroutine, Any

# 规则注册表: {rule_id: RuleEntry}
_registry: dict[str, dict] = {}


class RuleEntry:
    """规则条目"""
    def __init__(
        self,
        rule_id: str,
        direction: str,
        priority: int,
        applies_to: list[str] | None = None,
        description: str = "",
    ):
        if rule_id in _registry:
            raise ValueError(f"规则 ID 重复: {rule_id}")
        self.rule_id = rule_id
        self.direction = direction
        self.priority = priority
        self.applies_to = applies_to or []
        self.description = description
        self.fn: Callable[..., Coroutine[Any, Any, Any]] | None = None

    async def execute(self, *args, **kwargs):
        if self.fn is None:
            return None
        try:
            return await self.fn(*args, **kwargs)
        except Exception as e:
            import logging
            logging.getLogger(__name__).exception(
                "规则执行异常 [%s]: %s", self.rule_id, e
            )
            return None

    def __repr__(self):
        return f"<Rule {self.rule_id} [{self.direction}] pri={self.priority}>"


def validation_rule(
    rule_id: str,
    direction: str,
    priority: int = 1,
    applies_to: list[str] | None = None,
    description: str = "",
):
    """注册校验规则的装饰器"""
    def decorator(func):
        entry = RuleEntry(
            rule_id=rule_id,
            direction=direction,
            priority=priority,
            applies_to=applies_to,
            description=description or func.__doc__ or "",
        )
        entry.fn = func
        _registry[rule_id] = entry
        return func
    return decorator


def get_registry() -> dict[str, RuleEntry]:
    """获取全局规则注册表"""
    return dict(_registry)


def get_rules_by_direction(direction: str, priority: int | None = None) -> list[RuleEntry]:
    """获取指定方向的规则，可按优先级过滤"""
    rules = [r for r in _registry.values() if r.direction == direction]
    if priority is not None:
        rules = [r for r in rules if r.priority == priority]
    return sorted(rules, key=lambda r: r.priority)


def validate_registry():
    """启动时校验规则注册表的完整性"""
    ids = set()
    for rid, entry in _registry.items():
        if rid in ids:
            raise ValueError(f"重复的规则 ID: {rid}")
        ids.add(rid)
        if entry.fn is None:
            raise ValueError(f"规则 {rid} 没有绑定函数")
