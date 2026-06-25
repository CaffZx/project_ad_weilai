"""默认推荐算法 — 计算 4 个方向的适配度评分 + P3 上游推荐"""

from app.config.settings import settings
from app.core.metrics_ops_language import format_keyword_acos_change, period_labels
from app.models.asin_data import ASINData
from app.models.decision import DirectionScore, DataSummary, RecommendResponse
from app.models.layers import (
    TargetAcosRecommendation,
    TargetAcosStep,
    BudgetBidRecommendation,
    BudgetRecommendationDetail,
)


class Recommender:
    """广告方向推荐器

    基于 ASIN 数据对 4 个方向分别评分 (0-100)，选出最优推荐。
    评分参数来自 thresholds.toml [scoring]；阶段/淡旺季在 recommend() 末尾修正。
    """

    ELIGIBLE_MIN_SCORE = 40
    RECOMMENDED_MIN_SCORE = 70

    def __init__(self):
        self._scoring = settings.thresholds_config.get("scoring", {})

    def _cfg(self, section: str) -> dict:
        return self._scoring.get(section, {})

    @staticmethod
    def _best_natural_rank(data: ASINData) -> int | None:
        ranked = [kw.natural_rank for kw in data.keywords if kw.natural_rank is not None]
        return min(ranked) if ranked else None

    @staticmethod
    def _trend_judge(trend_fact: str | None, judgment: str) -> str:
        """运营可读：根据…趋势/变化，判断…"""
        if trend_fact:
            return f"根据{trend_fact}，{judgment}"
        return judgment

    @classmethod
    def _asin_trend_facts(cls, data: ASINData) -> list[str]:
        if not data.trend or len(data.trend) < 2:
            return []
        points = data.trend[:-1] if len(data.trend) > 2 else list(data.trend)
        facts: list[str] = []
        acos_vals = [(p.date, p.acos) for p in points if p.acos is not None]
        if len(acos_vals) >= 2:
            d0, a0 = acos_vals[0]
            d1, a1 = acos_vals[-1]
            n = len(acos_vals)
            if a1 > a0 + 2:
                facts.append(f"近{n}日ACOS由{a0}%升至{a1}%（{d0}→{d1}）")
            elif a1 < a0 - 2:
                facts.append(f"近{n}日ACOS由{a0}%降至{a1}%（{d0}→{d1}）")
            else:
                facts.append(f"近{n}日ACOS基本持平（约{a1}%）")
        cvr_vals = [(p.date, p.cvr) for p in points if p.cvr is not None]
        if len(cvr_vals) >= 2:
            d0, c0 = cvr_vals[0]
            d1, c1 = cvr_vals[-1]
            n = len(cvr_vals)
            if c1 > c0 + 2:
                facts.append(f"近{n}日CVR由{c0}%升至{c1}%（{d0}→{d1}）")
            elif c1 < c0 - 2:
                facts.append(f"近{n}日CVR由{c0}%降至{c1}%（{d0}→{d1}）")
        return facts

    @classmethod
    def _kw_acos_worsening_fact(cls, data: ASINData) -> str | None:
        worsening = [
            kw for kw in data.keywords
            if kw.acos_recent is not None and kw.acos_prior is not None
            and kw.acos_recent > kw.acos_prior + 5
        ]
        if not worsening:
            return None
        kw = max(worsening, key=lambda k: (k.acos_recent or 0) - (k.acos_prior or 0))
        days = 7
        _, _, _, recent_label, _ = period_labels(days)
        acos_part = format_keyword_acos_change(
            days, kw.acos, kw.acos_prior, kw.acos_recent, "ACOS上升"
        )
        if acos_part:
            return f"词「{kw.keyword}」{acos_part}"
        return (
            f"词「{kw.keyword}」{recent_label}ACOS由{kw.acos_prior:.0f}%升至{kw.acos_recent:.0f}%"
        )

    @staticmethod
    def _suitability_level(score: int) -> str:
        if score >= Recommender.RECOMMENDED_MIN_SCORE:
            return "recommended"
        if score >= Recommender.ELIGIBLE_MIN_SCORE:
            return "available"
        return "not_recommended"

    @staticmethod
    def _clamp_score(score: int) -> int:
        return max(0, min(100, score))

    def _finalize(self, direction_id: str, label: str, score: int, reasons: list[str],
                  default_sub_options: dict) -> DirectionScore:
        score = self._clamp_score(score)
        if not reasons:
            reasons.append("当前数据不支撑该方向")
        level = self._suitability_level(score)
        return DirectionScore(
            id=direction_id,
            label=label,
            suitability_score=score,
            suitability=level,
            reason="；".join(reasons),
            default_sub_options=default_sub_options,
        )

    def _apply_context_adjustments(self, data: ASINData, scores: list[DirectionScore]) -> list[DirectionScore]:
        """按产品阶段、淡旺季对分数做后处理修正（不向运营展示加减分细节）"""
        stage_adj = self._scoring.get("stage_adjustment", {}).get(data.product_stage or "", {})
        season_adj = self._scoring.get("season_adjustment", {}).get(data.season_stage or "", {})
        adjusted = []
        for s in scores:
            delta = int(stage_adj.get(s.id, 0)) + int(season_adj.get(s.id, 0))
            new_score = self._clamp_score(s.suitability_score + delta)
            adjusted.append(DirectionScore(
                id=s.id,
                label=s.label,
                suitability_score=new_score,
                suitability=self._suitability_level(new_score),
                reason=s.reason,
                default_sub_options=s.default_sub_options,
            ))
        return adjusted

    def _compose_display_reason(self, s: DirectionScore, data: ASINData) -> str:
        """将评分结果转为运营可读的一句话说明"""
        explainers = {
            "push_natural": self._explain_push_natural,
            "expand_keywords": self._explain_expand_keywords,
            "optimize_acos": self._explain_optimize_acos,
            "balance_maintain": self._explain_balance_maintain,
        }
        # 结论由卡片徽章展示，正文只保留依据要点（分号分隔，前端拆行）
        return explainers.get(s.id, lambda _d, _s: "请结合数据判断")(data, s)

    def _with_display_reason(self, s: DirectionScore, data: ASINData) -> DirectionScore:
        updated = s.model_copy()
        updated.reason = self._compose_display_reason(s, data)
        return updated

    def _explain_push_natural(self, data: ASINData, s: DirectionScore) -> str:
        best = self._best_natural_rank(data)
        stage = data.product_stage or ""
        trends = self._asin_trend_facts(data)
        acos_trend = trends[0] if trends else None
        if s.suitability == "not_recommended":
            parts = []
            if best is not None and best <= 5:
                parts.append(self._trend_judge(
                    f"核心词自然位稳定在约第{best}名",
                    "判断再加大广告推自然位边际收益有限",
                ))
            if acos_trend and "升" in acos_trend:
                parts.append(self._trend_judge(acos_trend, "判断宜先控 ACOS 而非继续推自然位"))
            if stage in ("收割利润期", "维持期"):
                parts.append(self._trend_judge(
                    "当前处于收割/维持阶段",
                    "判断应以控成本、守排名为主",
                ))
            return "；".join(parts) if parts else "根据当前数据，判断暂不适合将推自然位作为主方向"
        rising = sorted(
            [kw for kw in data.keywords if (kw.rank_change_14d or 0) >= 3],
            key=lambda k: k.rank_change_14d or 0,
            reverse=True,
        )
        if rising:
            kw = rising[0]
            return self._trend_judge(
                f"词「{kw.keyword}」近14日排名上升{kw.rank_change_14d}位",
                "判断可适度倾斜预算推进自然位",
            )
        if best and best > 5:
            return self._trend_judge(
                f"核心词自然位约第{best}名",
                "判断仍有上升空间，可小范围测试推自然位",
            )
        return self._trend_judge(acos_trend, "判断部分词具备推自然位基础，宜小预算试投") if acos_trend else "根据关键词排名变化，判断可小范围测试推自然位"

    def _explain_expand_keywords(self, data: ASINData, s: DirectionScore) -> str:
        avail = data.available_new_keywords or 0
        stage = data.product_stage or ""
        season = data.season_stage or ""
        acos = data.ad_data.acos if data.ad_data and data.ad_data.acos is not None else None
        trends = self._asin_trend_facts(data)
        if s.suitability == "not_recommended":
            return self._trend_judge(
                trends[0] if trends else "在投词承载力或新词机会不足",
                "判断暂不适合将扩词作为主方向",
            )
        parts = []
        if avail >= 2:
            parts.append(self._trend_judge(
                f"搜索词报告约有{avail}个高转化词尚未收录",
                "判断具备扩词空间",
            ))
        if acos is not None and acos < 30:
            parts.append(self._trend_judge(
                f"在投词整体 ACOS 约{acos:.0f}%",
                "判断现有词较健康，可承载少量新词",
            ))
        cvr_up = next((t for t in trends if "CVR" in t and "升" in t), None)
        if season == "旺季末期":
            parts.append(self._trend_judge(
                cvr_up or "旺季尾声流量趋缓",
                "判断宜小批量试词并设 ACOS 上限，不宜大规模上新",
            ))
        elif stage in ("收割利润期", "维持期"):
            parts.append(self._trend_judge(
                "收割/维持阶段",
                "判断扩词宜 5～10 个一批、观察 7 天再放量",
            ))
        elif s.suitability == "recommended":
            parts.append(self._trend_judge(
                cvr_up or trends[0] if trends else "转化效率尚可",
                "判断可作为本期重点方向之一",
            ))
        else:
            parts.append(self._trend_judge(
                trends[0] if trends else "整体指标平稳",
                "判断可作为备选，与控 ACOS、维持稳定搭配",
            ))
        return "；".join(parts) if parts else "根据搜索词与 ACOS 走势，判断可评估是否扩词"

    def _explain_optimize_acos(self, data: ASINData, s: DirectionScore) -> str:
        acos = data.ad_data.acos if data.ad_data and data.ad_data.acos is not None else None
        over = [kw for kw in data.keywords if kw.acos is not None and kw.acos > 40]
        wasteful = [kw for kw in data.keywords if kw.orders == 0 and kw.spend >= 15]
        stage = data.product_stage or ""
        trends = self._asin_trend_facts(data)
        kw_bad = self._kw_acos_worsening_fact(data)
        parts = []
        acos_rising = next((t for t in trends if "ACOS" in t and "升" in t), None)
        if acos_rising:
            parts.append(self._trend_judge(acos_rising, "判断账户效率承压，需优化 ACOS"))
        elif acos is not None and acos > 25:
            parts.append(self._trend_judge(
                f"账户 ACOS 约{acos:.0f}%",
                "判断仍有优化空间",
            ))
        if kw_bad:
            parts.append(self._trend_judge(kw_bad, "判断应优先否词或降价"))
        elif over:
            parts.append(self._trend_judge(
                f"{len(over)}个在投词 ACOS 超过40%",
                "判断建议否词或降价",
            ))
        if wasteful:
            parts.append(self._trend_judge(
                f"{len(wasteful)}个词高花费零转化",
                "判断建议优先清理无效花费",
            ))
        if stage in ("收割利润期", "维持期") and s.suitability != "not_recommended":
            parts.append(self._trend_judge(
                "收割/维持阶段",
                "判断适合通过优化 ACOS 保住利润",
            ))
        if not parts:
            return self._trend_judge(
                trends[0] if trends else "ACOS 指标",
                "判断处于合理区间，可按日常节奏微调",
            )
        return "；".join(parts)

    def _explain_balance_maintain(self, data: ASINData, s: DirectionScore) -> str:
        acos = data.ad_data.acos if data.ad_data and data.ad_data.acos is not None else None
        best = self._best_natural_rank(data)
        stage = data.product_stage or ""
        season = data.season_stage or ""
        trends = self._asin_trend_facts(data)
        acos_flat = next((t for t in trends if "ACOS" in t and ("持平" in t or "降" in t)), None)
        parts = []
        if acos_flat:
            parts.append(self._trend_judge(acos_flat, "判断整体表现稳定，适合维持策略"))
        elif acos is not None and acos <= 25:
            parts.append(self._trend_judge(
                f"ACOS 约{acos:.0f}%在目标内",
                "判断适合维持现有投放结构",
            ))
        if best is not None and best <= 5:
            parts.append(self._trend_judge(
                f"核心词自然位约第{best}名",
                "判断排名稳固，宜守不宜大动",
            ))
        if data.signals and data.signals.inventory_qty and data.signals.inventory_qty > 50:
            parts.append(self._trend_judge(
                f"库存约{int(data.signals.inventory_qty)}件充足",
                "判断无库存压力，可维持投放",
            ))
        if season == "旺季末期":
            parts.append(self._trend_judge(
                "旺季尾声",
                "判断宜控预算、稳指标，避免激进调整",
            ))
        elif stage in ("收割利润期", "维持期"):
            parts.append(self._trend_judge(
                "收割/维持阶段",
                "判断以守住排名和利润为主，小幅微调即可",
            ))
        if s.suitability == "recommended" and not parts:
            parts.append(self._trend_judge(
                trends[0] if trends else "近期指标平稳",
                "判断适合作为本期主方向",
            ))
        return "；".join(parts) if parts else self._trend_judge(
            trends[0] if trends else "近期数据",
            "判断以监控为主、按需微调",
        )

    @staticmethod
    def get_eligible_ids(scores: list[DirectionScore]) -> list[str]:
        return [s.id for s in scores if s.suitability_score >= Recommender.ELIGIBLE_MIN_SCORE]

    @staticmethod
    def filter_recommended(
        recommended: list[str],
        eligible_ids: list[str],
        scores: list[DirectionScore],
    ) -> list[str]:
        filtered = [d for d in recommended if d in eligible_ids]
        if filtered:
            return filtered
        if not eligible_ids:
            return ["balance_maintain"]
        score_by_id = {s.id: s.suitability_score for s in scores}
        return sorted(eligible_ids, key=lambda d: score_by_id.get(d, 0), reverse=True)[:2]

    def _score_push_natural(self, data: ASINData) -> DirectionScore:
        cfg = self._cfg("push_natural")
        score = 0
        reasons = []
        has_keywords = len(data.keywords) > 0
        rising_th = cfg.get("rising_rank_threshold", 3)

        if not has_keywords:
            reasons.append("数据缺失：无关键词排名数据，无法评估自然位推进机会")

        if has_keywords:
            rising = [kw for kw in data.keywords if (kw.rank_change_14d or 0) >= rising_th]
            well_pos = [kw for kw in data.keywords if kw.natural_rank is not None and kw.natural_rank <= 20]
            candidates = len(set(kw.keyword for kw in rising + well_pos))
            score += min(candidates * cfg.get("candidate_points_each", 8), cfg.get("candidate_points_max", 30))
            if candidates >= cfg.get("ad_driven_min_count", 2):
                reasons.append(f"{candidates} 个词有推自然位基础（上升或优质位）")

            ad_driven = [
                kw for kw in data.keywords
                if kw.acos is not None and kw.acos <= 30
                and kw.natural_rank is not None and kw.natural_rank > 20
                and kw.spend >= 50
            ]
            if len(ad_driven) >= cfg.get("ad_driven_min_count", 2):
                score += cfg.get("ad_driven_points", 20)
                reasons.append(f"{len(ad_driven)} 个广告依赖词可通过推自然位降低广告成本")

            best = self._best_natural_rank(data)
            if best is not None:
                if best > cfg.get("rank_space_threshold", 5):
                    score += cfg.get("rank_space_points", 15)
                    reasons.append(f"核心词自然位 (TOP {best}) 有上升空间")
                elif best <= cfg.get("top_rank_strong_penalty_threshold", 5):
                    score -= cfg.get("top_rank_strong_penalty", 25)
                    reasons.append(f"核心词已进首页前列 (TOP {best})，推自然位边际收益低")
                    cap = cfg.get("top_rank_score_cap", 35)
                    if score > cap:
                        score = cap

        return self._finalize(
            "push_natural", "推进自然位", score, reasons,
            {"top_keywords_count": 3, "budget_ratio": 20},
        )

    def _score_expand_keywords(self, data: ASINData) -> DirectionScore:
        cfg = self._cfg("expand_keywords")
        score = 0
        reasons = []
        has_keywords = len(data.keywords) > 0
        avail = data.available_new_keywords or 0

        score += min(avail * cfg.get("avail_points_per", 2), cfg.get("avail_points_max", 25))
        if avail >= 2:
            reasons.append(f"有 {avail} 个高转化未收录词")
        if avail >= cfg.get("many_new_keywords_threshold", 20):
            score += cfg.get("many_new_keywords_bonus", 10)
            reasons.append("可用新词数量充足，具备扩词储备")

        kw_count = data.keyword_count or len(data.keywords)
        if kw_count > 0 and kw_count < cfg.get("low_coverage_threshold", 10):
            score += cfg.get("low_coverage_points", 20)
            reasons.append(f"当前仅 {kw_count} 个词，覆盖率偏低")
        elif kw_count >= cfg.get("sufficient_coverage_threshold", 20):
            score -= cfg.get("sufficient_coverage_penalty", 5)
            reasons.append("覆盖率已充足（仍可按需小批量测试新词）")

        if has_keywords:
            existing = [kw for kw in data.keywords if kw.acos is not None]
            if existing:
                avg_acos = sum(kw.acos for kw in existing) / len(existing)
                if avg_acos < cfg.get("healthy_acos_threshold", 30):
                    score += cfg.get("healthy_acos_points", 15)
                    reasons.append(f"现有词 ACOS 健康 ({avg_acos:.0f}%)")

        if not has_keywords and avail == 0:
            reasons.append("数据缺失：无关键词数据和可用新词数据，无法评估扩词机会")

        if data.ad_purpose != "盈利型":
            score += cfg.get("non_profit_points", 10)

        return self._finalize(
            "expand_keywords", "新增扩词", score, reasons,
            {"sources": ["search_term_report", "auto_campaign"], "target_count": 5},
        )

    def _score_optimize_acos(self, data: ASINData) -> DirectionScore:
        cfg = self._cfg("optimize_acos")
        score = 0
        reasons = []
        over_th = cfg.get("over_keyword_acos_threshold", 40)
        over = [kw for kw in data.keywords if kw.acos is not None and kw.acos > over_th]
        wasteful = [
            kw for kw in data.keywords
            if kw.orders == 0 and kw.spend >= cfg.get("wasteful_min_spend", 15)
        ]

        account_acos = data.ad_data.acos if data.ad_data and data.ad_data.acos is not None else None
        if account_acos is not None:
            if account_acos > cfg.get("account_acos_high_threshold", 30):
                score += cfg.get("account_acos_high_points", 30)
                reasons.append(f"账户 ACOS {account_acos:.0f}% 偏高需优化")
            elif account_acos < cfg.get("account_acos_low_threshold", 20) and not over:
                score -= cfg.get("account_acos_low_penalty", 5)
                reasons.append(f"账户 ACOS {account_acos:.0f}% 已较低")
            elif account_acos < cfg.get("account_acos_low_threshold", 20) and over:
                score += cfg.get("word_level_focus_bonus", 15)
                reasons.append(f"账户 ACOS {account_acos:.0f}% 健康，但存在词级效率问题需优化")

        if over:
            score += min(len(over) * cfg.get("over_keyword_points_each", 8), cfg.get("over_keyword_points_max", 25))
            reasons.append(f"{len(over)} 个词 ACOS > {over_th}%")

        if wasteful:
            score += cfg.get("wasteful_points", 20)
            reasons.append(f"{len(wasteful)} 个高花费零转化词")

        adjustable = [
            kw for kw in data.keywords
            if kw.bid and kw.clicks > 0 and kw.bid > (kw.spend / kw.clicks) * 1.5
        ]
        if adjustable:
            score += cfg.get("bid_space_points", 10)
            reasons.append(f"{len(adjustable)} 个词存在 Bid 下调空间")

        if not reasons:
            reasons.append("当前 ACOS 在合理范围")

        return self._finalize(
            "optimize_acos", "优化 ACOS", score, reasons,
            {"methods": ["negative_keywords", "reduce_bid"], "acos_threshold": 40, "cvr_threshold": 3},
        )

    def _score_balance_maintain(self, data: ASINData) -> DirectionScore:
        cfg = self._cfg("balance_maintain")
        score = cfg.get("base_score", 50)
        reasons = []
        over_th = cfg.get("high_acos_keyword_threshold", 40)

        if data.ad_data and data.ad_data.acos is not None:
            if data.ad_data.acos <= cfg.get("healthy_acos_threshold", 25):
                score += cfg.get("healthy_acos_points", 10)
                reasons.append("ACOS 在目标范围内")

        if data.signals and data.signals.inventory_qty is not None:
            if data.signals.inventory_qty > cfg.get("inventory_high_qty", 50):
                score += cfg.get("inventory_high_points", 10)
                reasons.append("库存充足")
            elif data.signals.inventory_qty > 0:
                score += 5
                reasons.append("有库存")

        ranked = [kw for kw in data.keywords if kw.natural_rank is not None]
        if len(ranked) >= 3:
            unstable = [
                kw for kw in data.keywords
                if kw.rank_change_14d is not None and abs(kw.rank_change_14d) >= 5
            ]
            if not unstable:
                score += cfg.get("rank_stable_points", 5)
                reasons.append("关键词排名稳定")

        best = self._best_natural_rank(data)
        stage = data.product_stage or ""
        harvest_like = stage in ("收割利润期", "维持期")
        if best is not None and best <= 5 and not harvest_like:
            score += 8
            reasons.append("核心排名稳固，适合维持策略")
        elif best is not None and best <= 5 and harvest_like:
            score += 4
            reasons.append("核心排名稳固，收割期以维持效率为主")

        avail = data.available_new_keywords or 0
        high_acos = [kw for kw in data.keywords if kw.acos is not None and kw.acos > over_th]
        if avail >= 2 and not harvest_like:
            score -= cfg.get("expand_opportunity_penalty", 10)
            reasons.append("有扩词机会未利用")
        if len(high_acos) >= cfg.get("high_acos_min_keywords", 2) and not harvest_like:
            score -= cfg.get("high_acos_penalty", 8)
            reasons.append(f"{len(high_acos)} 个词 ACOS > {over_th}%，有优化空间")

        return self._finalize(
            "balance_maintain", "平衡维持", score, reasons,
            {"acos_tolerance": 5},
        )

    def recommend(self, data: ASINData) -> RecommendResponse:
        """为 ASIN 计算推荐方向（含阶段/淡旺季修正）"""
        scores = [
            self._score_push_natural(data),
            self._score_expand_keywords(data),
            self._score_optimize_acos(data),
            self._score_balance_maintain(data),
        ]
        scores = self._apply_context_adjustments(data, scores)
        scores = [self._with_display_reason(s, data) for s in scores]

        # 找到最高分方向
        best = max(scores, key=lambda s: s.suitability_score)
        if best.suitability_score >= 70:
            rec_level = "confirmed"
        elif best.suitability_score >= 40:
            rec_level = "suggest_optimize"
        else:
            rec_level = "force_correct"

        return RecommendResponse(
            asin=data.asin,
            recommended_direction=best.id,
            recommendation_reason=best.reason,
            recommendation_level=rec_level,
            directions=scores,
            data_summary=DataSummary(
                keyword_count=data.keyword_count or len(data.keywords),
                acos_14d=data.ad_data.acos if data.ad_data else None,
                acos_target=25,
                top_keyword_rank=min((kw.natural_rank for kw in data.keywords if kw.natural_rank), default=None),
                available_new_keywords=data.available_new_keywords,
                avg_daily_sales_30d=data.avg_daily_sales_30d,
                review_count=data.review_count,
                rating=data.rating,
                refund_rate=data.refund_rate,
                brand=data.brand,
                category_name=data.category_name,
                product_level=data.product_level,
                product_stage=data.product_stage,
                season_stage=data.season_stage,
                competition_count=len(data.competitors),
                ad_spend_14d=data.ad_data.spend if data.ad_data else None,
                inventory_qty=data.signals.inventory_qty if data.signals else None,
                price=data.price,
                rising_keywords=len([kw for kw in data.keywords if (kw.rank_change_14d or 0) >= 3]),
            ),
            data_completeness={
                "status": "missing" if data.data_missing else "complete",
                "missing_fields": data.missing_fields,
            },
        )


recommender = Recommender()


# ── P3 上游推荐器 ─────────────────────────────────────────────


def compute_target_acos_band(
    cfg: dict, stage: str | None, level: str | None, ad_purposes: list[str] | None
) -> tuple[int, int]:
    """目标ACOS 取值区间 (下限, 上限)。AI/算法须在此区间内给出单值（修复4，2026-06-18）。

    - 上限 = 阶段上限（KB03 §2「ACOS上限」，具体值，优先生效）。
      阶段未定义/未归一化时回落层级**默认**上限（KB03 §1「默认ACOS上限」：长尾 P3=30、
      其余 40，按子串匹配避免枚举键漂移）。
      ⚠ 2026-06-24 修正（运营确认）：原实现取 `min(阶段, 层级)`，使非长尾层级 40 永久封顶
      清货期60/测试期50，阶段上限永不生效。KB 层级栏措辞为「**默认**ACOS上限」=仅作 fallback，
      应被阶段具体值覆盖；故改为阶段优先、层级仅在阶段缺失时兜底（长尾 P3 清货期亦随阶段到 60%）。
    - 下限 = max(全局 min_acos, 各广告目的下限的最大值)，且不超过上限（上限以 KB 为准）。
    """
    global_min = int(cfg.get("min_acos", 25))
    global_max = int(cfg.get("max_acos", 100))
    stage_ceilings = cfg.get("stage_ceilings", {})
    stage_key = (stage or "").strip()
    if stage_key in stage_ceilings:
        acos_ceiling = min(int(stage_ceilings[stage_key]), global_max)
    else:
        lvl = str(level or "")
        level_default = 30 if ("P3" in lvl or "长尾" in lvl) else 40
        acos_ceiling = min(level_default, global_max)
    purpose_floors = cfg.get("purpose_floors", {})
    floors = [int(purpose_floors[p]) for p in (ad_purposes or []) if p in purpose_floors]
    acos_floor = max([global_min] + floors)
    acos_floor = min(acos_floor, acos_ceiling)  # 下限不得越过上限（KB 上限优先）
    return acos_floor, acos_ceiling


class TargetAcosRecommender:
    """目标 ACOS 推荐器 — 7 步规则链，纯算法

    输入: ASINData + ad_purposes
    输出: TargetAcosRecommendation (5% 粒度，落在 compute_target_acos_band 给出的[下限,上限]内)
    """

    def __init__(self):
        self.cfg = settings.thresholds_config.get("target_acos", {})

    @staticmethod
    def recommend_manual(asin: str, value: int) -> TargetAcosRecommendation:
        """运营手动设定值 — 跳过算法，直接返回"""
        return TargetAcosRecommendation(
            asin=asin,
            recommended_target=value,
            confidence="high",
            reasoning_chain=[TargetAcosStep(
                step=0, rule="manual_override",
                description="运营手动设定目标 ACOS，跳过算法计算",
                result=f"{value}%",
            )],
            manual_override=True,
        )

    def recommend(self, data: ASINData, ad_purposes: list[str]) -> TargetAcosRecommendation:
        steps = []
        violations = []

        # Step 1: 目标ACOS 取值区间（阶段×层级上限 + 目的下限）→ 基准取区间中点
        stage = data.product_stage or "推进期"
        acos_floor, acos_ceiling = compute_target_acos_band(
            self.cfg, stage, data.product_level, ad_purposes)
        target = (acos_floor + acos_ceiling) / 2
        steps.append(TargetAcosStep(
            step=1, rule="strategy_scope",
            description=f"阶段={stage}/定位={data.product_level or '?'}/目的={ad_purposes or []}，"
                        f"目标ACOS区间 {acos_floor}%-{acos_ceiling}%",
            result=f"初始基准 {target:.0f}%",
        ))

        # Step 2: Ad purpose correction
        purpose_corrections = self.cfg.get("purpose_corrections", {})
        for purpose in (ad_purposes or []):
            corr = purpose_corrections.get(purpose, {})
            direction = corr.get("direction", "moderate")
            magnitude = corr.get("magnitude", 0)
            if direction == "raise":
                target += magnitude
                steps.append(TargetAcosStep(
                    step=2, rule="purpose_correction",
                    description=f"广告目的={purpose}，上调目标ACOS",
                    result=f"调整+{magnitude}%，当前 {target:.0f}%",
                ))
            elif direction == "lower":
                target -= magnitude
                steps.append(TargetAcosStep(
                    step=2, rule="purpose_correction",
                    description=f"广告目的={purpose}，下调目标ACOS",
                    result=f"调整-{magnitude}%，当前 {target:.0f}%",
                ))

        # Step 3: Current ACOS correction — gradual approach, max ±{max_single_adjustment}%
        max_step = self.cfg.get("max_single_adjustment", 20)
        current_acos = data.ad_data.acos if data.ad_data and data.ad_data.acos else None
        if current_acos is not None and current_acos > 0:
            if target < current_acos - max_step:
                adjusted = current_acos - max_step
                steps.append(TargetAcosStep(
                    step=3, rule="current_acos_correction",
                    description=f"当前ACOS {current_acos:.0f}% >> 目标 {target:.0f}%，"
                                f"渐进调整，单次最大±{max_step}%",
                    result=f"平滑至 {adjusted:.0f}%",
                ))
                target = adjusted
            elif target > current_acos + max_step:
                adjusted = current_acos + max_step
                steps.append(TargetAcosStep(
                    step=3, rule="current_acos_correction",
                    description=f"当前ACOS {current_acos:.0f}% << 目标 {target:.0f}%，"
                                f"避免大幅跳跃",
                    result=f"平滑至 {adjusted:.0f}%",
                ))
                target = adjusted

        # Step 4: Trend micro-adjustment (linear regression slope over last 7 days)
        trend_cfg = self.cfg.get("trend_adjustment", {})
        if data.trend and len(data.trend) >= 3:
            acos_values = [t.acos for t in data.trend[-7:] if t.acos is not None]
            if len(acos_values) >= 3:
                slope = self._compute_slope(acos_values)
                threshold = trend_cfg.get("slope_threshold", 1.0)
                if slope < -threshold:
                    bonus = trend_cfg.get("improving_relax", 2)
                    target += bonus
                    steps.append(TargetAcosStep(
                        step=4, rule="trend_adjustment",
                        description=f"ACOS趋势改善 (斜率={slope:.2f})，略微放宽目标",
                        result=f"放宽+{bonus}%，当前 {target:.0f}%",
                    ))
                elif slope > threshold:
                    penalty = trend_cfg.get("worsening_tighten", 2)
                    target -= penalty
                    steps.append(TargetAcosStep(
                        step=4, rule="trend_adjustment",
                        description=f"ACOS趋势恶化 (斜率={slope:.2f})，收紧目标",
                        result=f"收紧-{penalty}%，当前 {target:.0f}%",
                    ))
        else:
            steps.append(TargetAcosStep(
                step=4, rule="trend_adjustment",
                description="趋势数据不足（<3天），跳过趋势微调",
                result=f"保持 {target:.0f}%",
            ))

        # Step 5: Natural order ratio correction
        nor = data.natural_order_ratio
        if nor is not None:
            high_threshold = self.cfg.get("high_natural_ratio_threshold", 50)
            low_threshold = self.cfg.get("low_natural_ratio_threshold", 20)
            nor_cfg = self.cfg.get("natural_ratio_adjustment", {})
            if nor > high_threshold:
                bonus = nor_cfg.get("high_ratio_bonus", 5)
                target += bonus
                steps.append(TargetAcosStep(
                    step=5, rule="natural_ratio_correction",
                    description=f"自然单占比 {nor:.0f}% > {high_threshold}%，"
                                f"可更激进的广告ACOS",
                    result=f"调整+{bonus}%，当前 {target:.0f}%",
                ))
            elif nor < low_threshold:
                penalty = nor_cfg.get("low_ratio_penalty", 5)
                target -= penalty
                steps.append(TargetAcosStep(
                    step=5, rule="natural_ratio_correction",
                    description=f"自然单占比 {nor:.0f}% < {low_threshold}%，"
                                f"需保守的广告ACOS",
                    result=f"调整-{penalty}%，当前 {target:.0f}%",
                ))

        # Step 6: Relative change constraint — 非测试期，相对变化 ≤ 40%
        stage = data.product_stage or "推进期"
        current_acos = data.ad_data.acos if data.ad_data and data.ad_data.acos else None
        if current_acos is not None and current_acos > 0 and stage not in ("测试期",):
            rel_change = abs(target - current_acos) / current_acos
            if rel_change > 0.40:
                max_deviation = current_acos * 0.40
                capped = current_acos + max_deviation if target > current_acos else current_acos - max_deviation
                violations.append(
                    f"相对变化约束：|{target:.0f}% - {current_acos:.0f}%| / {current_acos:.0f}% = {rel_change*100:.0f}% > 40%，调整为 {capped:.0f}%"
                )
                steps.append(TargetAcosStep(
                    step=6, rule="rel_change_constraint",
                    description=f"相对变化 {rel_change*100:.0f}% > 40% 上限（阶段={stage}），约束至 ±{max_deviation:.0f}%",
                    result=f"调整后 {capped:.0f}%",
                ))
                target = capped

        # Step 7: 钳到目标ACOS区间 [下限, 上限] + 5% 取整（先取整再钳，保证落在区间内）
        increment = self.cfg.get("increment", 5)
        target = round(target / increment) * increment
        target = max(acos_floor, min(acos_ceiling, target))
        steps.append(TargetAcosStep(
            step=7, rule="hard_cap",
            description=f"约束至目标ACOS区间 [{acos_floor}%, {acos_ceiling}%]，{increment}% 取整",
            result=f"最终推荐: {target}%",
        ))

        confidence = (
            "high" if len(violations) == 0
            else "medium" if len(violations) <= 1
            else "low"
        )

        return TargetAcosRecommendation(
            asin=data.asin,
            recommended_target=target,
            confidence=confidence,
            reasoning_chain=steps,
            constraint_violations=violations,
        )

    @staticmethod
    def _compute_slope(values: list[float]) -> float:
        """简单线性回归斜率"""
        n = len(values)
        if n < 2:
            return 0.0
        x_mean = (n - 1) / 2
        y_mean = sum(values) / n
        num = sum((i - x_mean) * (values[i] - y_mean) for i in range(n))
        den = sum((i - x_mean) ** 2 for i in range(n))
        return num / den if den != 0 else 0.0


class BudgetBidRecommender:
    """预算和 Bid 推荐器 — 5 步规则链，纯算法

    输入: ASINData + ad_purposes + season_stage + last_adjustment
    输出: BudgetBidRecommendation
    """

    def __init__(self):
        self.cfg = settings.thresholds_config.get("budget_bid", {})

    @staticmethod
    def recommend_manual(asin: str, value: float) -> BudgetBidRecommendation:
        """运营手动设定值 — 跳过算法，直接返回"""
        return BudgetBidRecommendation(
            asin=asin,
            budget_recommendation=BudgetRecommendationDetail(
                current=value,
                suggested=value,
                direction="maintain",
                magnitude_pct=0,
                reason="运营手动设定日预算，跳过算法计算",
                manual_override=True,
            ),
            summary="运营手动设定",
            confidence="high",
        )

    def recommend(
        self,
        data: ASINData,
        ad_purposes: list[str],
        season_stage: str,
        last_adjustment: list[dict] | None = None,
    ) -> BudgetBidRecommendation:
        reasons = []

        # Step 1: Anchor on actual daily spend (7d avg)
        spend_7d = data.ad_data.spend or 0
        daily_budget = spend_7d / 7 if spend_7d > 0 else (data.ad_data.daily_budget or 50)
        suggested_budget = daily_budget
        stage = data.product_stage or "推进期"
        stage_budgets = self.cfg.get("stage_budgets", {})
        budget_range = stage_budgets.get(stage, {"min": 20, "max": 80})
        reasons.append(
            f"产品阶段={stage}，预算参考范围 {budget_range['min']}-{budget_range['max']} USD"
        )

        # Step 2: Seasonality correction — adjust relative to current
        season_cfg = self.cfg.get("seasonality", {})
        season_mult = season_cfg.get(season_stage, {}).get("multiplier", 1.0)
        if season_mult != 1.0:
            season_adjustment = (season_mult - 1.0) * 0.3  # dampen: apply 30% of seasonal signal
            suggested_budget *= (1.0 + season_adjustment)
            reasons.append(f"淡旺季={season_stage}，季节系数 {season_mult}x（调整幅度 {season_adjustment*100:.0f}%）")

        # Step 3: Burn rate correction — incremental adjustment from current
        max_adj_pct = self.cfg.get("max_budget_adjustment_pct", 30)
        if data.ad_data and data.ad_data.spend is not None and daily_budget > 0:
            spend = data.ad_data.spend
            burn_rate = spend / daily_budget * 100
            burn_high = self.cfg.get("burn_high_threshold", 85)
            burn_low = self.cfg.get("burn_low_threshold", 50)
            if burn_rate > burn_high:
                burn_adj = min((burn_rate - burn_high) / 100 * 0.5, 0.15)  # 50% of excess → max 15%
                suggested_budget *= (1.0 + burn_adj)
                reasons.append(
                    f"花费率 {burn_rate:.0f}% > {burn_high}%，建议增加 {burn_adj*100:.0f}%"
                )
            elif burn_rate < burn_low and burn_rate > 0:
                burn_adj = min((burn_low - burn_rate) / 100 * 0.3, 0.10)  # 30% of gap → max 10%
                suggested_budget *= (1.0 - burn_adj)
                reasons.append(
                    f"花费率 {burn_rate:.0f}% < {burn_low}%，建议缩减 {burn_adj*100:.0f}%"
                )
        else:
            reasons.append("预算或花费数据缺失，跳过花费率修正")

        # Step 4: Stage range as soft guardrail — limit total change to max_adjustment_pct
        total_change_pct = abs(suggested_budget - daily_budget) / daily_budget * 100
        if total_change_pct > max_adj_pct:
            direction_sign = 1 if suggested_budget >= daily_budget else -1
            suggested_budget = daily_budget * (1.0 + direction_sign * max_adj_pct / 100)
            reasons.append(f"总调整幅度超过 {max_adj_pct}%，限制为 ±{max_adj_pct}%")
        # Clamp to stage range
        if suggested_budget < budget_range["min"]:
            reasons.append(f"建议预算 ${suggested_budget:.0f} 低于阶段下限 ${budget_range['min']}，维持下限")
            suggested_budget = budget_range["min"]
        elif suggested_budget > budget_range["max"]:
            reasons.append(f"建议预算 ${suggested_budget:.0f} 高于阶段上限 ${budget_range['max']}，维持上限")
            suggested_budget = budget_range["max"]

        # Step 5: Last adjustment linkage
        if last_adjustment:
            latest_ts = last_adjustment[0].get("operated_at", "")
            hours_ago = self._hours_since(latest_ts)
            if hours_ago is not None and hours_ago < 24:
                # Halve the suggested change
                suggested_budget = daily_budget + (suggested_budget - daily_budget) * 0.5
                reasons.append("24h内有调整记录，建议幅度减半")

        # Determine direction and magnitude
        if daily_budget > 0:
            if suggested_budget > daily_budget * 1.03:
                direction = "increase"
            elif suggested_budget < daily_budget * 0.97:
                direction = "decrease"
            else:
                direction = "maintain"
            magnitude_pct = round(abs(suggested_budget - daily_budget) / daily_budget * 100, 1)
        else:
            direction = "maintain"
            magnitude_pct = 0.0

        return BudgetBidRecommendation(
            asin=data.asin,
            budget_recommendation=BudgetRecommendationDetail(
                current=round(daily_budget, 2),
                suggested=round(suggested_budget, 2),
                direction=direction,
                magnitude_pct=magnitude_pct,
                reason="；".join(reasons),
            ),
            summary="；".join(reasons),
            confidence="medium",
        )

    @staticmethod
    def _hours_since(timestamp: str) -> float | None:
        """计算距某 ISO 时间戳的小时数"""
        try:
            from datetime import datetime, timezone as tz

            dt = datetime.fromisoformat(timestamp)
            return (datetime.now(tz.utc) - dt).total_seconds() / 3600
        except (ValueError, TypeError):
            return None
