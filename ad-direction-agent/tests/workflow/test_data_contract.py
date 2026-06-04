"""Tests for LLM data completeness gate."""

from app.models.asin_data import ASINData, AdData, KeywordData, TrendPoint
from app.workflow.data_contract import (
    evaluate_completeness,
    field_labels,
    get_module_contract,
)


def _full_data() -> ASINData:
    return ASINData(
        asin="B0TEST",
        margin=0.28,
        natural_order_ratio=68.0,
        ad_data=AdData(
            acos=28.0,
            cvr=11.0,
            cpc=0.52,
            spend=120.0,
            ctr=0.5,
            tacos=15.0,
            placement_tos_acos=30.0,
            placement_ros_acos=35.0,
        ),
        keywords=[KeywordData(keyword="test kw", acos=25.0, spend=10.0)],
        trend=[TrendPoint(date="2026-05-01", acos=30.0, cvr=10.0)],
        competitors=[],
    )


def test_execution_ok_with_placement_empty():
    data = _full_data()
    data.ad_data.placement_tos_acos = None
    data.ad_data.placement_ros_acos = None
    v = evaluate_completeness("execution", data)
    assert v.status == "ok"


def test_execution_blocked_missing_keywords():
    data = _full_data()
    data.keywords = []
    v = evaluate_completeness("execution", data)
    assert v.status == "blocked"
    assert "keywords" in v.missing_required


def test_execution_degraded_missing_margin():
    data = _full_data()
    data.margin = None
    v = evaluate_completeness("execution", data)
    assert v.status == "degraded"
    assert "margin" in v.missing_important


def test_p3_blocked_missing_spend():
    data = _full_data()
    data.ad_data.spend = None
    v = evaluate_completeness("p3", data)
    assert v.status == "blocked"
    assert "ad_data.spend" in v.missing_required


def test_retryable_on_partial_failure_timeout():
    data = _full_data()
    data.partial_failures = ["META_KW_AD:timeout"]
    data.data_freshness = "partial"
    v = evaluate_completeness("execution", data)
    assert v.status in ("ok", "degraded")


def test_not_retryable_asin_not_found():
    data = ASINData(asin="X", data_missing=True, missing_fields=["asin_not_found"])
    v = evaluate_completeness("tactics", data)
    assert v.status == "blocked"
    assert v.retryable is False


def test_field_labels_chinese():
    labels = field_labels(["ad_data.acos", "keywords"])
    assert "ACOS" in labels[0]
    assert "关键词" in labels[1]


def test_contracts_loaded():
    c = get_module_contract("report")
    assert "ad_data.acos" in c["required"]
