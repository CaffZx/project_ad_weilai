from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.asin_data import ASINData, AdData, KeywordData
from app.workflow.context import WorkflowContext
from app.workflow.steps import diagnosis, strategy, tactics


class _State:
    def __init__(self, long_term: dict | None = None, workflow: dict | None = None):
        self.long_term = long_term or {}
        self.workflow = workflow or {}
        self.set_workflow_state = MagicMock()

    def get_long_term_config(self, asin: str) -> dict:
        return dict(self.long_term)

    def get_workflow_state(self, asin: str) -> dict:
        return dict(self.workflow)

    def get_target_acos_override(self, asin: str):
        return None


def _ctx(*, state: _State, ensure_data=None, preload_data=None) -> WorkflowContext:
    return WorkflowContext(
        aggregator=MagicMock(),
        recommender=MagicMock(),
        reasoner=MagicMock(),
        state=state,
        validator=MagicMock(),
        decision_gen=MagicMock(),
        ensure_data=ensure_data or AsyncMock(),
        preload_data=preload_data or AsyncMock(),
    )


@pytest.mark.asyncio
async def test_strategy_options_preloads_dashboard_light_filter_only():
    preload = AsyncMock()
    ctx = _ctx(state=_State(), preload_data=preload)

    resp = await strategy.run_get_strategy_options(ctx, "B0TEST", days=7)

    assert resp.asin == "B0TEST"
    preload.assert_awaited_once_with(
        "B0TEST",
        days=7,
        meta_filter=["META_AD_PRODUCT", "META_TREND"],
    )


@pytest.mark.asyncio
async def test_tactics_options_reads_cache_without_fetch_or_llm():
    ensure_data = AsyncMock(side_effect=AssertionError("tactics/options must not fetch MCP data"))
    state = _State(
        long_term={
            "product_level": "常规产品 (P2)",
            "product_stage": "推进期",
            "season_stage": "淡季",
            "ad_purposes": ["转化型"],
            "target_keyword_strategy": ["大词"],
        },
        workflow={
            "target_scores": {"7": [{"target": "Conversion", "level": "推荐"}]},
            "keyword_analysis": {"7": [{"word": "test keyword", "keyword_class": "大词"}]},
        },
    )
    ctx = _ctx(state=state, ensure_data=ensure_data)

    resp = await tactics.run_get_tactics_options(ctx, "B0TEST", days=7)

    ensure_data.assert_not_awaited()
    state.set_workflow_state.assert_not_called()
    assert resp.current_selection == {
        "ad_purposes": ["转化型"],
        "target_keyword_strategy": ["大词"],
    }
    assert resp.target_scores == [{"target": "Conversion", "level": "推荐"}]
    assert resp.keyword_analysis == [{"word": "test keyword", "keyword_class": "大词"}]


@pytest.mark.asyncio
async def test_diagnosis_does_not_generate_keyword_ai_cache(monkeypatch):
    data = ASINData(
        asin="B0TEST",
        ad_data=AdData(acos=25.0, spend=10.0),
        keywords=[KeywordData(keyword="test keyword", natural_rank=8)],
        data_missing=False,
    )
    ensure_data = AsyncMock(return_value=data)
    state = _State(
        long_term={
            "product_level": "常规产品 (P2)",
            "product_stage": "推进期",
            "season_stage": "淡季",
            "ad_purposes": ["转化型"],
            "target_keyword_strategy": ["大词"],
        },
        workflow={},
    )

    async def fail_recommend(*args, **kwargs):
        raise AssertionError("diagnosis must not call purpose-agent")

    monkeypatch.setattr("app.llm.purpose_adapter.recommend_tactics_from_purpose", fail_recommend)
    ctx = _ctx(state=state, ensure_data=ensure_data)

    resp = await diagnosis.run_get_diagnosis(ctx, "B0TEST", days=7)

    state.set_workflow_state.assert_not_called()
    assert resp.keywords[0]["keyword"] == "test keyword"
    assert resp.keywords[0]["keyword_class"] == ""
