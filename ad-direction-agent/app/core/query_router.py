"""查询路由器 — 根据场景 + 执行方向，确定需要哪些元脚本

使用方式:
    router = QueryRouter()
    meta_ids = router.resolve(scenario_id="push_ranking",
                               directions=["expand_keywords"])
    # → ["META_KW_AD", "META_COMPETITOR", "META_AD_PRODUCT",
    #     "META_KW_COMPETITOR_RANK", "META_KW_SUB_ASIN_RANK",
    #     "META_FLOW_KEYWORD"]

设计原则:
    - 基础查询（listing + ad_product + keywords + competitors）始终执行
    - 场景决定追加哪些专项查询（如 ACOS 告急追加 placement + search_term）
    - 执行方向进一步补充（如选了 expand_keywords 追加 flow_keyword）
    - AI 不参与"选哪个脚本"的决策，只填参数值
"""

import logging
from typing import Iterable

from app.data.template_registry import get, list_all

logger = logging.getLogger(__name__)

# ── 基础元脚本（所有场景必查）───────────────────────────
# listing 主数据始终执行（由 DbAdapter._fetch_listing 固定处理）
BASE_META_SCRIPTS = [
    "META_KW_AD",        # 关键词级广告表现
    "META_COMPETITOR",   # 竞品列表
    "META_AD_PRODUCT",   # ASIN 级广告汇总
    "META_TREND",        # 趋势数据（按日聚合，供 ECharts 折线图）
]

# ── 场景 → 扩展元脚本 ──────────────────────────────────
# 8 个场景，每个场景需要哪些额外查询
# 注释说明: 为什么该场景需要这个数据
SCENARIO_EXTENSIONS: dict[str, list[str]] = {
    "acos_crisis": [
        "META_AD_PLACEMENT",    # 拆解精准/非精准 ACOS 来源
        "META_AD_SEARCH_TERM",  # 识别低效搜索词
    ],
    "clearance": [
        # 清仓场景仅需基础数据 + listing 库存
    ],
    "cold_start": [
        # 冷启动仅需基础数据 + listing 上架天数/销量
    ],
    "rapid_growth": [
        "META_KW_COMPETITOR_RANK",  # 监控关键词竞争态势
        "META_FLOW_KEYWORD",       # 快速扩词储备
    ],
    "push_ranking": [
        "META_KW_COMPETITOR_RANK",  # 竞品排名对比
        "META_KW_SUB_ASIN_RANK",   # 自家子ASIN排名变化
        "META_FLOW_KEYWORD",       # 可用的流量关键词
    ],
    "profit_harvest": [
        "META_AD_PLACEMENT",  # 拆分 TOS/ROS 成本，找优化空间
    ],
    "stable_conversion": [
        # 稳定态仅需基础数据监控波动
    ],
    "default": [
        # 默认综合看板
    ],
}

# ── 执行方向 → 补充元脚本 ──────────────────────────────
# 运营最终选定的执行方向，可能比场景检测的范围更广
DIRECTION_EXTENSIONS: dict[str, list[str]] = {
    "push_natural": [
        "META_KW_COMPETITOR_RANK",
        "META_KW_SUB_ASIN_RANK",
    ],
    "expand_keywords": [
        "META_FLOW_KEYWORD",
    ],
    "optimize_acos": [
        "META_AD_PLACEMENT",
        "META_AD_SEARCH_TERM",
    ],
    "balance_maintain": [],
}


class QueryRouter:
    """查询路由器"""

    @staticmethod
    def resolve(
        scenario_id: str,
        directions: Iterable[str] | None = None,
    ) -> list[str]:
        """解析场景+执行方向 → 元脚本ID列表（去重有序）"""
        scripts = list(BASE_META_SCRIPTS)

        # 场景扩展
        scripts.extend(SCENARIO_EXTENSIONS.get(scenario_id, []))

        # 执行方向扩展
        for d in (directions or []):
            scripts.extend(DIRECTION_EXTENSIONS.get(d, []))

        # 去重，保持首次出现顺序
        seen: set[str] = set()
        result: list[str] = []
        for s in scripts:
            if s not in seen:
                seen.add(s)
                result.append(s)

        return result

    @staticmethod
    def describe(meta_ids: list[str]) -> list[dict]:
        """返回元脚本的描述信息（给 LLM 上下文用）"""
        result = []
        for mid in meta_ids:
            defn = get(mid)
            if defn:
                result.append({
                    "meta_id": mid,
                    "description": defn.description,
                    "parameters": [
                        {"name": p.name, "type": p.type, "source": p.source}
                        for p in defn.parameters
                    ],
                })
            else:
                result.append({"meta_id": mid, "description": "未知"})
        return result

    @staticmethod
    def ai_decidable_params(meta_ids: list[str]) -> list[dict]:
        """返回AI可以填值的参数列表（给 LLM 输出格式参考）"""
        params = []
        for mid in meta_ids:
            defn = get(mid)
            if not defn:
                continue
            for p in defn.parameters:
                if p.source == "ai_decides":
                    params.append({
                        "meta_id": mid,
                        "param": p.name,
                        "type": p.type,
                        "description": p.description,
                    })
        return params
