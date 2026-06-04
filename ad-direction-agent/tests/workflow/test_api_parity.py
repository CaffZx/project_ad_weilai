"""Workflow 步骤与编排器委托一致性（mock 数据，无需 DB/HTTP）。"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("DATA_SOURCE", "mock")

from app.core.workflow_orchestrator import WorkflowOrchestrator
from app.models.asin_data import ASINData, AdData, SpecialSignals
from app.models.layers import StrategyOptionsResponse


def _minimal_asin_data() -> ASINData:
    return ASINData(
        asin="B0TEST123",
        ad_data=AdData(acos=25.0, cvr=10.0, spend=100.0, tacos=20.0, cpc=1.2),
        keywords=[],
        trend=[],
        competitors=[],
        signals=SpecialSignals(),
        data_missing=False,
    )


@pytest.fixture
def orchestrator():
    orch = WorkflowOrchestrator(
        aggregator=MagicMock(),
        reasoner=MagicMock(),
        state_manager=MagicMock(),
    )
    orch.state.get_long_term_config = MagicMock(return_value={})
    orch.state.get_workflow_state = MagicMock(return_value={})
    return orch


@pytest.mark.asyncio
async def test_strategy_options_delegates_to_steps(orchestrator):
    with patch(
        "app.workflow.steps.strategy.run_get_strategy_options",
        new_callable=AsyncMock,
    ) as mock_run:
        mock_run.return_value = StrategyOptionsResponse(
            asin="B0TEST123",
            dimensions=[],
            data_ok=True,
            missing_fields=[],
            days=7,
        )
        result = await orchestrator.get_strategy_options("B0TEST123", days=7)
        mock_run.assert_awaited_once()
        assert result.asin == "B0TEST123"
        assert result.days == 7


@pytest.mark.asyncio
async def test_execution_options_snapshot_keys(orchestrator):
    """execution/options 返回结构关键字段存在（mock recommender + reasoner）。"""
    data = _minimal_asin_data()
    orchestrator.aggregator.fetch = AsyncMock(return_value=data)
    orchestrator.recommender.recommend = MagicMock(
        return_value=MagicMock(directions=[], model_dump=lambda mode=None: [])
    )
    orchestrator.recommender.get_eligible_ids = MagicMock(return_value=[])
    orchestrator.recommender.filter_recommended = MagicMock(return_value=[])
    orchestrator.reasoner.recommend_execution = AsyncMock(
        return_value={"reasoning": "测试", "directions": []}
    )
    orchestrator.state.get_long_term_config = MagicMock(return_value={"ad_purposes": []})
    orchestrator.state.get_workflow_state = MagicMock(
        return_value={"current_layer": "execution", "layers_completed": ["strategy", "tactics"]}
    )

    with patch("app.config.settings.settings.use_langgraph", False):
        resp = await orchestrator.get_execution_options("B0TEST123", days=7)

    assert resp.asin == "B0TEST123"
    assert hasattr(resp, "directions")
    assert hasattr(resp, "recommendation_summary")


@pytest.mark.asyncio
async def test_langgraph_flag_uses_bridge(orchestrator):
    with patch("app.config.settings.settings.use_langgraph", True):
        with patch.object(
            orchestrator, "_get_bridge", return_value=MagicMock()
        ) as mock_bridge_factory:
            bridge = MagicMock()
            bridge.run_endpoint = AsyncMock(return_value={"ok": True})
            mock_bridge_factory.return_value = bridge
            result = await orchestrator.get_strategy_options("B0TEST123")
            bridge.run_endpoint.assert_awaited_with(
                "strategy.options", asin="B0TEST123", days=7
            )
            assert result == {"ok": True}
