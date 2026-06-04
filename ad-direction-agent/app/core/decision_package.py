"""决策包生成 — 基于确认方向生成操作任务清单"""

from app.models.asin_data import ASINData
from app.models.decision import DecisionPackage, Task
from app.config.settings import settings


class DecisionPackageGenerator:
    """决策包生成器

    职责:
    1. 基于最终确认的方向 + 子选项生成操作任务
    2. 估算预算变化
    3. 组装最终决策包
    """

    @staticmethod
    def _is_harvest_like(data: ASINData) -> bool:
        stage = data.product_stage or ""
        return stage in ("收割利润期", "维持期")

    @staticmethod
    def _is_peak_end(data: ASINData) -> bool:
        return (data.season_stage or "") == "旺季末期"

    @staticmethod
    def _best_natural_rank(data: ASINData) -> int | None:
        ranked = [kw.natural_rank for kw in data.keywords if kw.natural_rank is not None]
        return min(ranked) if ranked else None

    def generate(
        self,
        data: ASINData,
        direction: str,
        sub_options: dict | None = None,
    ) -> DecisionPackage:
        generator = getattr(self, f"_generate_{direction}", self._generate_default)
        return generator(data, sub_options or {})

    def _generate_push_natural(self, data: ASINData, sub_options: dict) -> DecisionPackage:
        tasks = []
        top_n = sub_options.get("top_keywords_count", 3)
        budget_ratio = sub_options.get("budget_ratio", 20)
        best_rank = self._best_natural_rank(data)
        harvest = self._is_harvest_like(data)
        rising = []

        if harvest or (best_rank is not None and best_rank <= 5):
            tasks.append(Task(
                priority="medium",
                action="暂停大规模预算倾斜推自然位",
                details=(
                    f"核心词已在 TOP {best_rank or '?'}，收割/维持期以效率为先；"
                    "仅监控自然位，不新增倾斜预算"
                ),
                estimated_impact="避免低效花费，保护利润率",
            ))
        else:
            rising = sorted(
                [kw for kw in data.keywords if (kw.rank_change_14d or 0) >= 5],
                key=lambda kw: kw.rank_change_14d or 0,
                reverse=True,
            )[:top_n]

            if rising:
                kw_list = "、".join(f"{kw.keyword}(↑{kw.rank_change_14d}位)" for kw in rising)
                tasks.append(Task(
                    priority="high",
                    action=f"增加 TOP {top_n} 上升词预算",
                    details=f"重点关注词: {kw_list}，预算倾斜 {budget_ratio}%",
                    estimated_impact=f"预计日增 {top_n*2}-{top_n*5} 点击",
                ))

        tasks.append(Task(
            priority="medium",
            action="监控核心词自然位变化",
            details="重点关注 14 天内自然位变化趋势，若连续下滑再评估是否加预算",
            estimated_impact="确保推自然位策略有效",
        ))

        if not harvest:
            tasks.append(Task(
                priority="low",
                action="检查 TOS 位置占比变化",
                details="TOS 占比控制在 40% 以下，若超出则调整 Bid",
                estimated_impact="控制广告花费结构",
            ))

        return DecisionPackage(
            direction="推进自然位",
            tasks=tasks,
            key_metrics={
                "rising_keywords": len(rising),
                "budget_ratio": budget_ratio,
                "current_tos": data.ad_data.tos_ratio if data.ad_data else None,
            },
        )

    def _generate_expand_keywords(self, data: ASINData, sub_options: dict) -> DecisionPackage:
        sources = sub_options.get("sources", ["search_term_report"])
        target_count = sub_options.get("target_count", 5)
        tasks = []
        peak_end = self._is_peak_end(data)
        harvest = self._is_harvest_like(data)

        batch = target_count
        if peak_end or harvest:
            target_count = min(target_count, 10)
            batch = 5 if peak_end else min(8, target_count)

        source_labels = {
            "search_term_report": "搜索词报告",
            "competitor_listing": "竞品 Listing",
            "auto_campaign": "自动广告",
            "llm_generated": "LLM 生成词",
        }
        selected = [source_labels.get(s, s) for s in sources]

        action = (
            f"小批量测试扩词（首批 {batch} 个）"
            if (peak_end or harvest)
            else f"从{''.join(selected)}提取新词加入手动广告"
        )
        cand = getattr(data, "expand_keyword_candidates", None) or []
        cand_names = [
            (c.get("keyword") if isinstance(c, dict) else str(c))
            for c in cand
            if (c.get("keyword") if isinstance(c, dict) else c)
        ]
        if cand_names:
            listed = "、".join(f"「{n}」" for n in cand_names)
            details = (
                f"从搜索词报告筛选高转化未收录词（共 {len(cand_names)} 个，须全部试投或择优）：{listed}；"
                f"首批加入 {batch} 个，ACOS 目标 30%，7 天超 40% 则暂停"
                if (peak_end or harvest)
                else f"候选词：{listed}；计划新增 {target_count} 个"
            )
        else:
            details = (
                f"从搜索词报告等来源筛选高转化词，首批加入 {batch} 个；"
                f"ACOS 目标 30%，7 天超 40% 则暂停"
                if (peak_end or harvest)
                else f"计划新增 {target_count} 个关键词"
            )
        tasks.append(Task(
            priority="high" if not harvest else "medium",
            action=action,
            details=details,
            estimated_impact=f"预计日增 {batch if (peak_end or harvest) else target_count} 级点击，可控试错",
        ))

        if "competitor_listing" in sources:
            tasks.append(Task(
                priority="medium",
                action="从竞品 Listing 挖掘差异化词",
                details="分析竞品覆盖词中我方未覆盖的高相关词",
                estimated_impact="预计覆盖新流量入口，7 天内可评估效果",
            ))

        tasks.append(Task(
            priority="low",
            action="监控新词 7 天表现",
            details="新词观察期 7 天，ACOS > 40% 则降 Bid 或否定",
            estimated_impact="控制扩词质量",
        ))

        current_count = data.keyword_count or len(data.keywords)
        return DecisionPackage(
            direction="新增扩词",
            tasks=tasks,
            key_metrics={
                "current_keyword_count": current_count,
                "target_keyword_count": current_count + target_count,
                "estimated_budget_change": f"+${target_count*1}-{target_count*2}/日",
            },
        )

    def _generate_optimize_acos(self, data: ASINData, sub_options: dict) -> DecisionPackage:
        methods = sub_options.get("methods", ["negative_keywords", "reduce_bid"])
        acos_th = sub_options.get("acos_threshold", 40)
        cvr_th = sub_options.get("cvr_threshold", 3)
        tasks = []

        if "negative_keywords" in methods:
            over_acos = [kw for kw in data.keywords if kw.acos is not None and kw.acos > acos_th]
            candidates = [kw for kw in data.keywords if kw.orders == 0 and kw.spend >= 15]
            task_detail_parts = []
            if over_acos:
                task_detail_parts.append(f"ACOS > {acos_th}%: {len(over_acos)} 个")
            if candidates:
                task_detail_parts.append(f"高花费 0 转化: {len(candidates)} 个")
            tasks.append(Task(
                priority="high",
                action="否定低效词",
                details="；".join(task_detail_parts) if task_detail_parts else "分析搜索词报告添加否定词",
                estimated_impact=f"预计节省 ${len(candidates)*5}-{len(candidates)*15}/日" if candidates else "降低 ACOS",
            ))

        if "reduce_bid" in methods:
            reducible = [
                kw for kw in data.keywords
                if kw.bid and kw.suggested_bid and kw.bid > kw.suggested_bid
            ]
            details = f"{len(reducible)} 个词 Bid 可下调" if reducible else "检查关键词 Bid"
            tasks.append(Task(
                priority="medium",
                action="降低表现差的关键字 Bid",
                details=details,
                estimated_impact="提升广告效率",
            ))

        if "adjust_bidding_strategy" in methods:
            tasks.append(Task(
                priority="medium",
                action="调整竞价策略",
                details="高 ACOS 词改为 Dynamic-Down Only，优质词维持 Fixed",
                estimated_impact="控制花费，保护优质词曝光",
            ))

        if "pause_ad_group" in methods:
            high_acos_kw = [kw for kw in data.keywords if kw.acos is not None and kw.acos > 60]
            if high_acos_kw:
                tasks.append(Task(
                    priority="medium",
                    action="暂停高 ACOS 广告组",
                    details=f"涉及 {len(high_acos_kw)} 个 ACOS > 60% 的关键词",
                    estimated_impact="快速止血",
                ))

        tasks.append(Task(
            priority="low",
            action="持续监控 ACOS 变化",
            details="7 天后复检，确认优化效果",
            estimated_impact="评估优化策略有效性",
        ))

        current_acos = data.ad_data.acos if data.ad_data else None
        return DecisionPackage(
            direction="优化 ACOS",
            tasks=tasks,
            key_metrics={
                "current_acos": current_acos,
                "acos_target": 25,
                "estimated_savings": f"预计降低 ACOS 3-8%",
            },
        )

    def _generate_balance_maintain(self, data: ASINData, sub_options: dict) -> DecisionPackage:
        tolerance = sub_options.get("acos_tolerance", 5)
        tasks = []

        tasks.append(Task(
            priority="medium",
            action="维持当前广告参数",
            details=f"ACOS 波动 {tolerance}% 以内不触发调整",
            estimated_impact="保持稳定",
        ))

        tasks.append(Task(
            priority="medium",
            action="监控关键指标变化",
            details="每周复检 ACOS/CVR/销量，超出容忍范围则重新评估方向",
            estimated_impact="及时发现异常",
        ))

        return DecisionPackage(
            direction="平衡维持",
            tasks=tasks,
            key_metrics={
                "acos_tolerance": tolerance,
                "current_acos": data.ad_data.acos if data.ad_data else None,
                "current_budget": data.ad_data.daily_budget if data.ad_data else None,
            },
        )

    def _generate_default(self, data: ASINData, sub_options: dict) -> DecisionPackage:
        return DecisionPackage(
            direction="未知方向",
            tasks=[Task(priority="medium", action="请确认有效的广告方向", details="")],
            key_metrics={},
        )


decision_generator = DecisionPackageGenerator()
