"""默认推荐算法 — 计算 4 个方向的适配度评分 + P3 上游推荐"""

from app.config.settings import settings
from app.models.asin_data import ASINData
from app.models.decision import DirectionScore, DataSummary, RecommendResponse
from app.models.layers import (
    TargetAcosRecommendation,
    TargetAcosStep,
    BudgetBidRecommendation,
    BudgetRecommendationDetail,
    BidAdjustment,
)


class Recommender:
    """广告方向推荐器

    基于 ASIN 数据对 4 个方向分别评分 (0-100)，选出最优推荐。
    评分逻辑: 每个方向检查若干关键指标，加权计算适配度。
    """

    def _score_push_natural(self, data: ASINData) -> DirectionScore:
        score = 0
        reasons = []

        has_keywords = len(data.keywords) > 0

        if not has_keywords:
            reasons.append("数据缺失：无关键词排名数据，无法评估自然位推进机会")

        # 有上升词或优质位词 +10~30
        if has_keywords:
            rising = [kw for kw in data.keywords if (kw.rank_change_14d or 0) >= 3]
            well_pos = [kw for kw in data.keywords if kw.natural_rank is not None and kw.natural_rank <= 20]
            candidates = len(set(kw.keyword for kw in rising + well_pos))
            score += min(candidates * 8, 30)
            if candidates >= 2:
                reasons.append(f"{candidates} 个词有推自然位基础（上升或优质位）")

        # 广告依赖词（ACOS好但自然位差）→ 推自然位有直接价值 +20
        if has_keywords:
            ad_driven = [
                kw for kw in data.keywords
                if kw.acos is not None and kw.acos <= 30
                and kw.natural_rank is not None and kw.natural_rank > 20
                and kw.spend >= 50
            ]
            if len(ad_driven) >= 2:
                score += 20
                reasons.append(f"{len(ad_driven)} 个广告依赖词可通过推自然位降低广告成本")

        # 核心排名有空间 +15
        if has_keywords:
            ranked = [kw for kw in data.keywords if kw.natural_rank is not None]
            if ranked:
                best = min(kw.natural_rank for kw in ranked)
                if best > 5:
                    score += 15
                    reasons.append(f"核心词自然位 (TOP {best}) 有上升空间")
                else:
                    score -= 10
                    reasons.append("核心词已进首页前列")

        if not reasons:
            reasons.append("当前数据不支撑该方向")

        level = "recommended" if score >= 70 else ("available" if score >= 40 else "not_recommended")
        return DirectionScore(
            id="push_natural",
            label="推进自然位",
            suitability_score=score,
            suitability=level,
            reason="；".join(reasons),
            default_sub_options={"top_keywords_count": 3, "budget_ratio": 20},
        )

    def _score_expand_keywords(self, data: ASINData) -> DirectionScore:
        score = 0
        reasons = []

        has_keywords = len(data.keywords) > 0

        # 有可用新词 +30
        avail = data.available_new_keywords or 0
        score += min(avail * 10, 30)
        if avail >= 2:
            reasons.append(f"有 {avail} 个高转化未收录词")

        # 覆盖率低 +20
        kw_count = data.keyword_count or len(data.keywords)
        if kw_count > 0 and kw_count < 10:
            score += 20
            reasons.append(f"当前仅 {kw_count} 个词，覆盖率偏低")
        elif kw_count >= 20:
            score -= 10
            reasons.append("覆盖率已充足")

        # 现有词 ACOS 健康 +15
        if has_keywords:
            existing = [kw for kw in data.keywords if kw.acos is not None]
            if existing:
                avg_acos = sum(kw.acos for kw in existing) / len(existing)
                if avg_acos < 30:
                    score += 15
                    reasons.append(f"现有词 ACOS 健康 ({avg_acos:.0f}%)")

        if not has_keywords and avail == 0:
            reasons.append("数据缺失：无关键词数据和可用新词数据，无法评估扩词机会")

        # 不是 盈利型 目的 +10
        if data.ad_purpose != "盈利型":
            score += 10

        if not reasons:
            reasons.append("当前数据不支撑该方向")

        level = "recommended" if score >= 70 else ("available" if score >= 40 else "not_recommended")
        return DirectionScore(
            id="expand_keywords",
            label="新增扩词",
            suitability_score=score,
            suitability=level,
            reason="；".join(reasons),
            default_sub_options={"sources": ["search_term_report", "auto_campaign"], "target_count": 5},
        )

    def _score_optimize_acos(self, data: ASINData) -> DirectionScore:
        score = 0
        reasons = []

        # ACOS 高 +30
        if data.ad_data and data.ad_data.acos is not None:
            acos = data.ad_data.acos
            if acos > 30:
                score += 30
                reasons.append(f"ACOS {acos:.0f}% 偏高需优化")
            elif acos < 20:
                score -= 10
                reasons.append(f"ACOS {acos:.0f}% 已较低")

        # 有超标词 +25
        over = [kw for kw in data.keywords if kw.acos is not None and kw.acos > 40]
        if over:
            score += min(len(over) * 8, 25)
            reasons.append(f"{len(over)} 个词 ACOS > 40%")

        # 有高花费零转化词 +20
        wasteful = [kw for kw in data.keywords if kw.orders == 0 and kw.spend >= 15]
        if wasteful:
            score += 20
            reasons.append(f"{len(wasteful)} 个高花费零转化词")

        # 有 Bid 调整空间 +10
        adjustable = [kw for kw in data.keywords if kw.bid and kw.clicks > 0 and kw.bid > (kw.spend / kw.clicks) * 1.5]
        if adjustable:
            score += 10

        level = "recommended" if score >= 70 else ("available" if score >= 40 else "not_recommended")
        return DirectionScore(
            id="optimize_acos",
            label="优化 ACOS",
            suitability_score=score,
            suitability=level,
            reason="；".join(reasons) if reasons else "当前 ACOS 在合理范围",
            default_sub_options={"methods": ["negative_keywords", "reduce_bid"], "acos_threshold": 40, "cvr_threshold": 3},
        )

    def _score_balance_maintain(self, data: ASINData) -> DirectionScore:
        score = 50
        reasons = []

        # ACOS 健康度（检查绝对值；acos_7d 无数据源，跳过波动对比）
        if data.ad_data and data.ad_data.acos is not None and data.ad_data.acos <= 25:
            score += 10
            reasons.append("ACOS 在目标范围内")

        # 无异常信号 +15（库存充足）
        if data.signals and data.signals.inventory_qty is not None and data.signals.inventory_qty > 50:
            score += 10
            reasons.append("库存充足")
        elif data.signals and data.signals.inventory_qty is not None and data.signals.inventory_qty > 0:
            score += 5
            reasons.append("有库存")

        # 排名稳定 +5
        ranked = [kw for kw in data.keywords if kw.natural_rank is not None]
        if len(ranked) >= 3:
            unstable = [kw for kw in data.keywords if kw.rank_change_14d is not None and abs(kw.rank_change_14d) >= 5]
            if not unstable:
                score += 5
                reasons.append("关键词排名稳定")

        # 有明确的扩词或优化机会 → 减分
        if data.available_new_keywords and data.available_new_keywords >= 2:
            score -= 15
            reasons.append("有扩词机会未利用")
        high_acos = [kw for kw in data.keywords if kw.acos is not None and kw.acos > 40]
        if len(high_acos) >= 2:
            score -= 10
            reasons.append(f"{len(high_acos)} 个词 ACOS > 40%，有优化空间")

        if not reasons:
            reasons.append("当前数据状态一般")

        level = "recommended" if score >= 70 else ("available" if score >= 40 else "not_recommended")
        return DirectionScore(
            id="balance_maintain",
            label="平衡维持",
            suitability_score=score,
            suitability=level,
            reason="；".join(reasons),
            default_sub_options={"acos_tolerance": 5},
        )

    def recommend(self, data: ASINData) -> RecommendResponse:
        """为 ASIN 计算推荐方向"""
        scores = [
            self._score_push_natural(data),
            self._score_expand_keywords(data),
            self._score_optimize_acos(data),
            self._score_balance_maintain(data),
        ]

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


class TargetAcosRecommender:
    """目标 ACOS 推荐器 — 7 步规则链，纯算法

    输入: ASINData + ad_purposes
    输出: TargetAcosRecommendation (5% 粒度 ACOS 目标值, ≥5% 且 ≤100%)
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

        # Step 1: Strategy scope — product_stage → ACOS 基准范围
        stage = data.product_stage or "推进期"
        stage_scopes = self.cfg.get("stage_scopes", {})
        scope = stage_scopes.get(stage, {"min": 5, "max": 25})
        target = (scope["min"] + scope["max"]) / 2
        steps.append(TargetAcosStep(
            step=1, rule="strategy_scope",
            description=f"产品阶段={stage}，目标ACOS范围 {scope['min']}%-{scope['max']}%",
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

        # Step 6: Relative change constraint — 非清货/测试期，相对变化 ≤ 40%
        stage = data.product_stage or "推进期"
        current_acos = data.ad_data.acos if data.ad_data and data.ad_data.acos else None
        if current_acos is not None and current_acos > 0 and stage not in ("清货期", "测试期"):
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

        # Step 7: Hard cap + snap to 5% increment
        min_acos = self.cfg.get("min_acos", 5)
        max_acos = self.cfg.get("max_acos", 100)
        increment = self.cfg.get("increment", 5)
        target = max(min_acos, min(max_acos, target))
        target = round(target / increment) * increment
        steps.append(TargetAcosStep(
            step=7, rule="hard_cap",
            description=f"最终范围约束 [{min_acos}%, {max_acos}%]，"
                        f"取整到{increment}%增量",
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

        # Step 5: Bid vs CPC adjustments per keyword
        bid_adjustments = self._compute_bid_adjustments(data)

        # Step 6: Last adjustment linkage
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
            bid_adjustments=bid_adjustments,
            summary="；".join(reasons),
            confidence="medium",
        )

    def _compute_bid_adjustments(self, data: ASINData) -> list[BidAdjustment]:
        """逐词 Bid 调整建议"""
        adjustments = []
        bid_cpc_reduce = self.cfg.get("bid_cpc_reduction_ratio", 1.5)
        bid_cpc_incr = self.cfg.get("bid_cpc_increase_ratio", 1.1)

        for kw in data.keywords:
            if not kw.bid or kw.clicks <= 0:
                continue
            effective_cpc = kw.spend / kw.clicks
            if kw.bid > effective_cpc * bid_cpc_reduce:
                suggested = round(effective_cpc * 1.3, 2)
                adjustments.append(BidAdjustment(
                    keyword=kw.keyword,
                    current_bid=kw.bid,
                    suggested_bid=suggested,
                    direction="decrease",
                    magnitude_pct=round(
                        (kw.bid - suggested) / kw.bid * 100, 1
                    ),
                    reason=(
                        f"Bid (${kw.bid:.2f}) >> 实际CPC "
                        f"(${effective_cpc:.2f}) × {bid_cpc_reduce}"
                    ),
                ))
            elif (
                kw.bid < effective_cpc * bid_cpc_incr
                and kw.acos is not None
                and kw.acos < 25
            ):
                suggested = round(effective_cpc * 1.2, 2)
                adjustments.append(BidAdjustment(
                    keyword=kw.keyword,
                    current_bid=kw.bid,
                    suggested_bid=suggested,
                    direction="increase",
                    magnitude_pct=round(
                        (suggested - kw.bid) / kw.bid * 100, 1
                    ),
                    reason=(
                        f"Bid (${kw.bid:.2f}) < 实际CPC "
                        f"(${effective_cpc:.2f})，ACOS健康 ({kw.acos:.0f}%)"
                    ),
                ))

        return adjustments[:10]

    @staticmethod
    def _hours_since(timestamp: str) -> float | None:
        """计算距某 ISO 时间戳的小时数"""
        try:
            from datetime import datetime, timezone as tz

            dt = datetime.fromisoformat(timestamp)
            return (datetime.now(tz.utc) - dt).total_seconds() / 3600
        except (ValueError, TypeError):
            return None
