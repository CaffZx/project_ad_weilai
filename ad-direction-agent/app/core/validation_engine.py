"""校验规则引擎 — 核心调度器

流程:
1. 收集指定方向 + cross_tag 的所有规则
2. 按优先级排序执行
3. 聚合结果，输出 overall_level
"""

import logging
from app.rules import get_registry, get_rules_by_direction
from app.config.settings import settings
from app.models.asin_data import ASINData
from app.core.validation_ops import enrich_validation_item
from app.models.validation import ValidationItem, ValidationResult, DataCompleteness

logger = logging.getLogger(__name__)


class ValidationEngine:
    """校验规则引擎"""

    @staticmethod
    def _resolve_overall_level(items: list[ValidationItem]) -> str:
        """从所有规则结果中合成整体等级"""
        levels = {"force_correct": 0, "suggest_optimize": 1, "confirmed": 2}
        worst = 2
        for item in items:
            lvl = levels.get(item.level, 2)
            if lvl < worst:
                worst = lvl
        return {0: "force_correct", 1: "suggest_optimize", 2: "confirmed"}[worst]

    async def validate(
        self,
        data: ASINData,
        direction: str,
        sub_options: dict | None = None,
    ) -> ValidationResult:
        """校验指定方向的所有适用规则"""
        items: list[ValidationItem] = []

        # 收集规则: 目标方向规则 + cross_tag 通用约束
        rules = get_rules_by_direction(direction)
        rules.extend(get_rules_by_direction("cross_tag"))

        rules.sort(key=lambda r: r.priority)

        thresholds = settings.thresholds_config

        for rule in rules:
            try:
                kwargs = {"data": data, "thresholds": thresholds}
                if rule.applies_to or rule.rule_id in ("PN-2", "KE-4", "BM-5"):
                    kwargs["sub_options"] = sub_options or {}

                result = await rule.execute(**kwargs)
                if result is not None:
                    items.append(enrich_validation_item(result))
            except Exception:
                logger.exception("规则 [%s] 执行异常，中止校验", rule.rule_id)
                raise

        # 补充数据完整性信息
        completeness = DataCompleteness(
            status="missing" if data.data_missing else "complete",
            missing_fields=data.missing_fields,
            stale_fields=data.stale_fields,
        )

        overall = self._resolve_overall_level(items)

        return ValidationResult(
            asin=data.asin,
            direction=direction,
            overall_level=overall,
            items=items,
            data_completeness=completeness,
        )


validation_engine = ValidationEngine()
