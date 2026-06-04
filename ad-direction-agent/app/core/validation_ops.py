"""校验结果 — 运营可读文案（不含 BM-1 / OA-1 等内部编号）"""

from __future__ import annotations

from app.models.validation import ValidationItem

# 规则 ID → 运营能看懂的检查项名称（仅内部 rule_id 映射，不对外展示编号）
RULE_OPS_TITLES: dict[str, str] = {
  # 推进自然位
    "PN-1": "上升词与优质词",
    "PN-2": "预算倾斜比例",
    "PN-3": "广告依赖词",
    "PN-4": "上升词ACOS",
    "PN-5": "核心词自然排名",
    # 新增扩词
    "KE-1": "可扩新词数量",
    "KE-2": "关键词覆盖率",
    "KE-3": "在投词健康度",
    "KE-4": "单次扩词数量",
    # 优化 ACOS
    "OA-1": "关键词ACOS",
    "OA-2": "高花费零转化词",
    "OA-3": "Bid下调空间",
    "OA-4": "否定词机会",
    # 平衡维持
    "BM-1": "指标波动",
    "BM-2": "库存与竞品",
    "BM-3": "ACOS目标区间",
    "BM-4": "排名稳定性",
    "BM-5": "波动触发阈值",
    # 标签联动
    "CROSS-1": "产品阶段与广告目的",
    "CROSS-2": "产品阶段与广告目的",
    "CROSS-4": "关键词类型",
    "CROSS-5": "库存天数",
    "CROSS-7": "淡旺季品牌防守",
    "CROSS-8": "产品阶段与盈利目的",
    "CROSS-10": "旺季末期推自然位",
}

LEVEL_OPS_VERB: dict[str, str] = {
    "confirmed": "检查通过",
    "suggest_optimize": "需关注",
    "force_correct": "需立即处理",
}


def format_validation_item_ops(item: ValidationItem | dict) -> str:
    """单条校验 → 运营可读一句（无规则编号）"""
    if isinstance(item, ValidationItem):
        rule_id = item.rule_id
        level = item.level
        message = (item.message or "").strip()
    else:
        rule_id = item.get("rule_id", "")
        level = item.get("level", "confirmed")
        message = (item.get("message") or "").strip()

    title = RULE_OPS_TITLES.get(rule_id, "")
    verb = LEVEL_OPS_VERB.get(level, "需关注")

    if title:
        return f"【{title}】{verb}：{message}" if message else f"【{title}】{verb}"
    return f"{verb}：{message}" if message else verb


def enrich_validation_item(item: ValidationItem) -> ValidationItem:
    """为校验项附加运营可读 display_message"""
    return item.model_copy(
        update={"display_message": format_validation_item_ops(item)}
    )
