"""ERP auto_push unit tests."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.models.campaign import CampaignAnalysisResult, CampaignAdjustmentItem
from app.persistence.erp_writer.auto_push import (
    analysis_to_kb_payload,
    push_full_to_erp,
    should_push_to_erp,
    wizard_payload_from_state,
)
from app.persistence.erp_writer.repository import WriteReport


class _FakeState:
    def get_long_term_config(self, asin: str) -> dict:
        return {
            "product_level": "腰部",
            "product_stage": "推进期",
            "season_stage": "旺季准备",
            "ad_purposes": ["转化型"],
            "target_keyword_strategy": ["大词"],
        }

    def get_workflow_state(self, asin: str) -> dict:
        return {
            "target_scores": {"7": [{"target": "Conversion", "level": "推荐"}]},
            "keyword_analysis": {"7": [{"word": "test kw", "rank": "10"}]},
            "execution": {"selected_directions": ["boost_sales"]},
        }

    def get_p3_recommendation(self, asin: str) -> dict | None:
        return {"target_acos": {"recommended_target": 25}}


def test_analysis_to_kb_payload_uses_run_id():
    result = CampaignAnalysisResult(
        parent_asin="B0TEST",
        run_id="20260604T120000Z",
        adjustments=[
            CampaignAdjustmentItem(
                campaign_name="c1",
                campaign_id="123",
                child_asin="B0CHILD",
            )
        ],
        summary={"to_adjust": 1},
    )
    payload = analysis_to_kb_payload(result, temperature=0.3)
    assert payload["experiment_id"] == "20260604T120000Z"
    assert payload["run_number"] == 1
    assert payload["parent_asin"] == "B0TEST"
    assert len(payload["adjustments"]) == 1
    assert payload["adjustments"][0]["campaign_id"] == "123"


def test_wizard_payload_from_state():
    state = _FakeState()
    wizard, partial = wizard_payload_from_state("B0TEST", 7, state)
    assert wizard["parent_asin"] == "B0TEST"
    assert len(wizard["target_scores"]) == 1
    assert len(wizard["keyword_analysis"]) == 1
    assert wizard["decision_meta"]["product_position"] == "腰部"
    assert partial is False


def test_wizard_payload_partial_when_empty():
    class EmptyState(_FakeState):
        def get_workflow_state(self, asin: str) -> dict:
            return {"execution": {}}

        def get_p3_recommendation(self, asin: str) -> dict | None:
            return None

    wizard, partial = wizard_payload_from_state("B0TEST", 7, EmptyState())
    assert partial is True


def test_should_push_to_erp_gates():
    ok, _ = should_push_to_erp(
        CampaignAnalysisResult(
            parent_asin="X",
            sanity_check_passed=False,
            adjustments=[CampaignAdjustmentItem(campaign_name="c", campaign_id="1")],
        )
    )
    assert ok is False

    ok, reason = should_push_to_erp(
        CampaignAnalysisResult(
            parent_asin="X",
            adjustments=[
                CampaignAdjustmentItem(campaign_name="c", campaign_id=""),
            ],
        )
    )
    assert ok is False
    assert "campaign_id" in reason

    ok, _ = should_push_to_erp(
        CampaignAnalysisResult(
            parent_asin="X",
            adjustments=[
                CampaignAdjustmentItem(campaign_name="c", campaign_id="99"),
            ],
        )
    )
    assert ok is True


@patch("app.persistence.erp_writer.auto_push.resolve_listing_context")
@patch("app.persistence.erp_writer.auto_push.ErpDualWriterRepository")
def test_push_full_to_erp(mock_repo_cls, mock_resolve):
    mock_resolve.return_value = MagicMock(
        shop_id=1622,
        parent_seller_sku="SKU1",
        site_code="Amazon_US",
    )
    mock_repo = MagicMock()
    mock_repo.write_full.return_value = WriteReport(decision_id="dec_test", modern_card=2)
    mock_repo_cls.return_value = mock_repo

    kb = {
        "parent_asin": "B0TEST",
        "experiment_id": "run1",
        "run_number": 1,
        "adjustments": [{"campaign_id": "1", "campaign_name": "c"}],
        "summary": {},
    }
    wizard = {"decision_meta": {}, "long_term_config": {}}

    report = push_full_to_erp(kb, wizard, conn_kwargs={
        "host": "h", "port": 3306, "user": "u", "password": "p", "database": "d",
    })
    assert report.decision_id == "dec_test"
    mock_repo.write_full.assert_called_once()
