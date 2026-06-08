"""广告方向评分与门禁 — 单元测试"""

import pytest

from app.core.recommender import Recommender
from app.models.asin_data import ASINData, AdData, KeywordData, SpecialSignals


def _harvest_peak_end_asin() -> ASINData:
    """对齐截图场景：收割利润期、旺季末期、核心 TOP3、76 新词、ACOS 22%"""
    keywords = [
        KeywordData(keyword=f"kw{i}", natural_rank=3, rank_change_14d=1, acos=22, spend=80, orders=5)
        for i in range(22)
    ]
    keywords.extend([
        KeywordData(keyword="bad1", acos=45, spend=20, orders=1),
        KeywordData(keyword="bad2", acos=48, spend=18, orders=0),
        KeywordData(keyword="bad3", acos=42, spend=25, orders=1),
        KeywordData(keyword="bad4", acos=50, spend=30, orders=0),
    ])
    return ASINData(
        asin="B0TESTHARVEST",
        keywords=keywords,
        keyword_count=26,
        available_new_keywords=76,
        natural_order_ratio=76.2,
        product_stage="收割利润期",
        season_stage="旺季末期",
        product_level="战略级产品 (P0)",
        ad_purpose="盈利型",
        ad_data=AdData(acos=22.0, spend=500, orders=40),
        signals=SpecialSignals(inventory_qty=100, inventory_days=45),
    )


def _test_stage_asin() -> ASINData:
    return ASINData(
        asin="B0TESTNEW",
        keywords=[],
        keyword_count=3,
        available_new_keywords=8,
        product_stage="测试期",
        season_stage="淡季",
        ad_data=AdData(acos=35.0),
    )


@pytest.fixture
def recommender():
    return Recommender()


def test_harvest_scenario_push_natural_ineligible(recommender):
    data = _harvest_peak_end_asin()
    resp = recommender.recommend(data)
    by_id = {s.id: s for s in resp.directions}
    assert by_id["push_natural"].suitability_score < Recommender.ELIGIBLE_MIN_SCORE
    assert by_id["push_natural"].suitability == "not_recommended"
    assert "修正" not in by_id["push_natural"].reason
    assert by_id["push_natural"].label == "推进自然位"


def test_harvest_scenario_expand_and_optimize_eligible(recommender):
    data = _harvest_peak_end_asin()
    resp = recommender.recommend(data)
    by_id = {s.id: s for s in resp.directions}
    assert by_id["expand_keywords"].suitability_score >= Recommender.ELIGIBLE_MIN_SCORE
    assert by_id["optimize_acos"].suitability_score >= Recommender.ELIGIBLE_MIN_SCORE
    assert by_id["optimize_acos"].suitability_score >= by_id["expand_keywords"].suitability_score


def test_filter_recommended_respects_eligible(recommender):
    data = _harvest_peak_end_asin()
    resp = recommender.recommend(data)
    eligible = Recommender.get_eligible_ids(resp.directions)
    filtered = Recommender.filter_recommended(
        ["push_natural", "expand_keywords"],
        eligible,
        resp.directions,
    )
    assert "push_natural" not in filtered
    assert "expand_keywords" in filtered


def test_test_stage_boosts_expand(recommender):
    data = _test_stage_asin()
    resp = recommender.recommend(data)
    by_id = {s.id: s for s in resp.directions}
    assert by_id["expand_keywords"].suitability_score >= by_id["balance_maintain"].suitability_score


def test_decision_package_harvest_push_natural_monitor_only():
    from app.core.decision_package import DecisionPackageGenerator

    gen = DecisionPackageGenerator()
    data = _harvest_peak_end_asin()
    pkg = gen.generate(data, "push_natural", {})
    actions = [t.action for t in pkg.tasks]
    assert any("暂停" in a or "监控" in a for a in actions)
    assert not any("增加 TOP" in a for a in actions)


def test_decision_package_peak_end_expand_small_batch():
    from app.core.decision_package import DecisionPackageGenerator

    gen = DecisionPackageGenerator()
    data = _harvest_peak_end_asin()
    pkg = gen.generate(data, "expand_keywords", {})
    assert any("小批量" in t.action for t in pkg.tasks)
