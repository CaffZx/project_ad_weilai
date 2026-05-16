"""场景分析器 — 根据产品阶段+广告目的+信号检测场景，输出北极星指标看板

场景定义（8种，按优先级排列）:
  1. ACOS告急  — ACOS>40% 覆盖其他场景（清货除外）
  2. 清仓甩货  — 清货或库存积压
  3. 新品冷启动 — 测试 + 引流/卡位
  4. 快速起量  — 推进 + 引流
  5. 推自然排名 — 推进 + 卡位
  6. 盈利收割  — 收割/维持 + 盈利
  7. 稳定转化  — 收割/维持 + 转化
  8. 默认      — 无匹配
"""

from app.models.asin_data import ASINData


# ── 场景定义 ─────────────────────────────────────────────

SCENARIOS = [
    {"id": "acos_crisis", "name": "ACOS 告急", "priority": 1},
    {"id": "clearance", "name": "清仓甩货", "priority": 2},
    {"id": "cold_start", "name": "新品冷启动", "priority": 3},
    {"id": "rapid_growth", "name": "快速起量", "priority": 4},
    {"id": "push_ranking", "name": "推自然排名", "priority": 5},
    {"id": "profit_harvest", "name": "盈利收割", "priority": 6},
    {"id": "stable_conversion", "name": "稳定转化", "priority": 7},
    {"id": "default", "name": "综合看板", "priority": 8},
]

# 各场景的北极星指标定义 — 与数据血缘可用字段对齐
# 格式: [字段key, 显示标签, 单位, 阈值]
METRIC_DEFS = {
    "cold_start": [
        ("days_since_launch", "上架天数", "天", {"warning": 30}),
        ("cvr", "整体转化率", "%", {"warning": 5}),
        ("acos", "ACOS", "%", {"warning": 40}),
        ("daily_ad_spend_ratio", "单日广告花费占比", "%", {"warning": 20}),
        ("tacos", "TACOS", "%", {"warning": 30}),
    ],
    "rapid_growth": [
        ("avg_daily_sales_30d", "日均销量", "单", {"warning": 30}),
        ("precision_cpc", "精准CPC", "$", {"warning": 1.0}),
        ("cvr", "整体转化率", "%", {"warning": 8}),
        ("natural_order_ratio", "自然订单占比", "%", {"warning": 30}),
        ("acos", "ACOS", "%", {"warning": 40}),
        ("tacos", "TACOS", "%", {"warning": 25}),
    ],
    "push_ranking": [
        ("acos", "ACOS", "%", {"warning": 40}),
        ("natural_order_ratio", "自然订单占比", "%", {"warning": 30}),
        ("precision_acos", "精准ACOS", "%", {"warning": 30}),
        ("broad_acos", "非精准ACOS", "%", {"warning": 40}),
        ("avg_daily_sales_30d", "日均销量", "单", {}),
        ("tacos", "TACOS", "%", {"warning": 25}),
        ("cpc", "CPC", "$", {}),
        ("margin", "毛利率", "%", {"danger": 0, "warning": 15}),
    ],
    "profit_harvest": [
        ("acos", "ACOS", "%", {"warning": 40}),
        ("margin", "毛利率", "%", {"danger": 0, "warning": 15}),
        ("precision_acos", "精准ACOS", "%", {"warning": 25}),
        ("broad_acos", "非精准ACOS", "%", {"warning": 30}),
        ("natural_order_ratio", "自然订单占比", "%", {"warning": 40}),
        ("tacos", "TACOS", "%", {"warning": 20}),
    ],
    "stable_conversion": [
        ("cvr", "整体转化率", "%", {"warning": 8}),
        ("acos", "ACOS", "%", {"warning": 40}),
        ("natural_order_ratio", "自然订单占比", "%", {"warning": 40}),
        ("avg_daily_sales_30d", "日均销量", "单", {}),
        ("tacos", "TACOS", "%", {"warning": 20}),
    ],
    "clearance": [
        ("inventory_qty", "库存量", "件", {"danger": 0, "warning": 100}),
        ("avg_daily_sales_30d", "日均销量", "单", {"warning": 10}),
        ("acos", "ACOS", "%", {"warning": 40}),
        ("margin", "毛利率", "%", {"danger": 0, "warning": 10}),
        ("tacos", "TACOS", "%", {"warning": 25}),
    ],
    "acos_crisis": [
        ("acos", "ACOS", "%", {"danger": 50, "warning": 40}),
        ("precision_acos", "精准ACOS", "%", {"danger": 50, "warning": 40}),
        ("broad_acos", "非精准ACOS", "%", {"danger": 50, "warning": 40}),
        ("precision_cpc", "精准CPC", "$", {"warning": 1.5}),
        ("broad_cpc", "非精准CPC", "$", {"warning": 1.0}),
        ("precision_spend_ratio", "精准花费占比", "%", {"warning": 60}),
        ("tacos", "TACOS", "%", {"warning": 30}),
    ],
    "default": [
        ("avg_daily_sales_30d", "日均销量", "单", {}),
        ("acos", "ACOS", "%", {"warning": 40}),
        ("natural_order_ratio", "自然订单占比", "%", {"warning": 30}),
        ("tacos", "TACOS", "%", {"warning": 25}),
        ("cvr", "整体转化率", "%", {"warning": 8}),
    ],
}

# 字段取值函数（从 ASINData 提取各字段）
FIELD_GETTERS = {
    # 基础字段
    "acos": lambda d: round(d.ad_data.acos, 1) if d.ad_data and d.ad_data.acos is not None else None,
    "tacos": lambda d: round(d.ad_data.tacos, 1) if d.ad_data and d.ad_data.tacos is not None else None,
    "cpc": lambda d: round(d.ad_data.cpc, 2) if d.ad_data and d.ad_data.cpc is not None else None,
    "cvr": lambda d: round(d.ad_data.cvr, 1) if d.ad_data and d.ad_data.cvr is not None else None,
    "spend": lambda d: round(d.ad_data.spend, 2) if d.ad_data and d.ad_data.spend is not None else None,
    "daily_ad_spend_ratio": lambda d: round(d.ad_data.daily_ad_spend_ratio, 1) if d.ad_data and d.ad_data.daily_ad_spend_ratio is not None else None,
    "impressions": lambda d: d.ad_data.impressions if d.ad_data else None,
    "clicks": lambda d: d.ad_data.clicks if d.ad_data else None,
    "orders": lambda d: d.ad_data.orders if d.ad_data else None,
    # 精准/非精准拆分
    "precision_acos": lambda d: round(d.ad_data.precision_acos, 1) if d.ad_data and d.ad_data.precision_acos is not None else None,
    "broad_acos": lambda d: round(d.ad_data.broad_acos, 1) if d.ad_data and d.ad_data.broad_acos is not None else None,
    "precision_cpc": lambda d: round(d.ad_data.precision_cpc, 2) if d.ad_data and d.ad_data.precision_cpc is not None else None,
    "broad_cpc": lambda d: round(d.ad_data.broad_cpc, 2) if d.ad_data and d.ad_data.broad_cpc is not None else None,
    "precision_spend_ratio": lambda d: round(d.ad_data.precision_spend_ratio, 1) if d.ad_data and d.ad_data.precision_spend_ratio is not None else None,
    "broad_spend_ratio": lambda d: round(d.ad_data.broad_spend_ratio, 1) if d.ad_data and d.ad_data.broad_spend_ratio is not None else None,
    # 产品字段
    "keyword_count": lambda d: d.keyword_count or len(d.keywords) if d.keywords else 0,
    "available_new_keywords": lambda d: d.available_new_keywords,
    "avg_daily_sales_30d": lambda d: round(d.avg_daily_sales_30d, 1) if d.avg_daily_sales_30d is not None else None,
    "natural_order_ratio": lambda d: round(d.natural_order_ratio, 1) if d.natural_order_ratio is not None else None,
    "margin": lambda d: round(d.margin * 100, 1) if d.margin is not None else None,
    "price": lambda d: d.price,
    "rating": lambda d: d.rating,
    "days_since_launch": lambda d: d.days_since_launch,
    # 信号字段
    "inventory_qty": lambda d: d.signals.inventory_qty if d.signals else None,
    "in_transit_inventory": lambda d: d.signals.in_transit_inventory if d.signals else None,
    "top_keyword_rank": lambda d: min((k.natural_rank for k in d.keywords if k.natural_rank), default=None) if d.keywords else None,
}


def _get_value(data: ASINData, field: str):
    """从 ASINData 提取字段值，字段不存在时返回 None"""
    getter = FIELD_GETTERS.get(field)
    if getter:
        return getter(data)
    return None


# 各字段的状态评估函数: (value, threshold_dict) → "good"|"warning"|"danger"|"neutral"
STATUS_EVAL = {
    # 越高越差
    "acos": lambda v, t: ("danger" if v >= t.get("danger", 999) else "warning" if v >= t.get("warning", 999) else "good") if v else "neutral",
    "tacos": lambda v, t: ("danger" if v >= t.get("danger", 999) else "warning" if v >= t.get("warning", 999) else "good") if v else "neutral",
    "cpc": lambda v, t: ("warning" if v >= t.get("warning", 999) else "good") if v else "neutral",
    "precision_acos": lambda v, t: ("danger" if v >= t.get("danger", 999) else "warning" if v >= t.get("warning", 999) else "good") if v else "neutral",
    "broad_acos": lambda v, t: ("danger" if v >= t.get("danger", 999) else "warning" if v >= t.get("warning", 999) else "good") if v else "neutral",
    "precision_cpc": lambda v, t: ("warning" if v >= t.get("warning", 999) else "good") if v else "neutral",
    "broad_cpc": lambda v, t: ("warning" if v >= t.get("warning", 999) else "good") if v else "neutral",
    "precision_spend_ratio": lambda v, t: ("warning" if v >= t.get("warning", 999) else "good") if v else "neutral",
    "daily_ad_spend_ratio": lambda v, t: ("warning" if v >= t.get("warning", 999) else "good") if v else "neutral",
    # 越低越差
    "margin": lambda v, t: ("danger" if v <= t.get("danger", -999) else "warning" if v <= t.get("warning", 999) else "good") if v else "neutral",
    "cvr": lambda v, t: ("warning" if v <= t.get("warning", -999) else "good") if v else "neutral",
    "natural_order_ratio": lambda v, t: ("warning" if v <= t.get("warning", -999) else "good") if v else "neutral",
    "inventory_qty": lambda v, t: ("danger" if v <= t.get("danger", -999) else "warning" if v <= t.get("warning", 999) else "good") if v else "neutral",
    "rating": lambda v, t: ("warning" if v <= t.get("warning", 999) else "good") if v else "neutral",
    "days_since_launch": lambda v, t: ("warning" if v >= t.get("warning", 999) else "good") if v else "neutral",
    "top_keyword_rank": lambda v, t: ("danger" if v >= t.get("danger", 999) else "warning" if v >= t.get("warning", 999) else "good") if v else "neutral",
}


def _metric_status(field: str, value, thresholds: dict) -> str:
    if value is None:
        return "neutral"
    evaluator = STATUS_EVAL.get(field)
    if evaluator:
        return evaluator(value, thresholds)
    return "neutral"


def _build_metric(data: ASINData, field: str, label: str, unit: str, thresholds: dict) -> dict:
    value = _get_value(data, field)
    return {
        "field": field,
        "label": label,
        "value": value,
        "unit": unit,
        "status": _metric_status(field, value, thresholds),
    }


def detect_scenario(data: ASINData, product_stage: str | None = None, ad_purposes: list[str] | None = None) -> dict:
    """检测当前属于哪个场景

    优先级:
    1. ACOS>40% → ACOS告急（除非是清货中/淘汰）
    2. 清货中/淘汰 或 库存为0 → 清仓甩货
    3. 测试期/起步期 + 引流/卡位 → 新品冷启动
    4. 进展期/冲刺期 + 引流 → 快速起量
    5. 进展期/冲刺期 + 卡位 → 推自然排名
    6. 达成期/超预期 + 盈利 → 盈利收割
    7. 达成期/超预期 + 转化 → 稳定转化
    8. → 默认
    """
    acos = data.ad_data.acos if data.ad_data else None
    inv_qty = data.signals.inventory_qty if data.signals else 0
    purposes = ad_purposes or []

    # Priority 1: ACOS告急
    if acos is not None and acos > 40 and product_stage != "清货期":
        return {"id": "acos_crisis", "name": "ACOS 告急", "trigger": f"ACOS {acos:.0f}% > 40%"}

    # Priority 2: 清仓甩货
    if product_stage == "清货期" or (inv_qty is not None and inv_qty == 0):
        return {"id": "clearance", "name": "清仓甩货", "trigger": f"{'清货期' if product_stage == '清货期' else '库存为0'}"}

    # Priority 3: 新品冷启动
    if product_stage == "测试期" and ("引流型" in purposes or "排名型" in purposes):
        return {"id": "cold_start", "name": "新品冷启动", "trigger": f"{product_stage} + {'/'.join(purposes)}"}

    # Priority 4: 快速起量
    if product_stage == "推进期" and "引流型" in purposes:
        return {"id": "rapid_growth", "name": "快速起量", "trigger": f"{product_stage} + 引流型"}

    # Priority 5: 推自然排名
    if product_stage == "推进期" and "排名型" in purposes:
        return {"id": "push_ranking", "name": "推自然排名", "trigger": f"{product_stage} + 排名型"}

    # Priority 6: 盈利收割
    if product_stage in ("收割利润期", "维持期") and "盈利型" in purposes:
        return {"id": "profit_harvest", "name": "盈利收割", "trigger": f"{product_stage} + 盈利型"}

    # Priority 7: 稳定转化
    if product_stage in ("收割利润期", "维持期") and "转化型" in purposes:
        return {"id": "stable_conversion", "name": "稳定转化", "trigger": f"{product_stage} + 转化型"}

    # Priority 8: 默认
    return {"id": "default", "name": "综合看板", "trigger": "无特殊匹配"}


def build_metric_board(data: ASINData, product_stage: str | None = None, ad_purposes: list[str] | None = None) -> dict:
    """构建北极星指标看板"""
    scenario = detect_scenario(data, product_stage, ad_purposes)
    scenario_id = scenario["id"]

    metric_defs = METRIC_DEFS.get(scenario_id, METRIC_DEFS["default"])
    metrics = [_build_metric(data, f, l, u, t) for f, l, u, t in metric_defs]

    return {
        "scenario_id": scenario_id,
        "scenario_name": scenario["name"],
        "trigger": scenario["trigger"],
        "metrics": metrics,
    }
