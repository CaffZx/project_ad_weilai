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
            "product_level": "重点产品 (P1)",
            "product_stage": "推进期",
            "season_stage": "旺季准备",
            "operating_mode": "稳定经营",
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
    assert wizard["decision_meta"]["product_position"] == "重点产品 (P1)"
    assert wizard["decision_meta"]["operating_mode"] == "稳定经营"
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
    # sanity 失败不再阻断落库（sanity 只衡量旁路步骤成败，非结果质量）。
    # 有 adjustments + campaign_id 即可落库；sanity 结论另落 validation_passed。
    ok, _ = should_push_to_erp(
        CampaignAnalysisResult(
            parent_asin="X",
            sanity_check_passed=False,
            adjustments=[CampaignAdjustmentItem(campaign_name="c", campaign_id="1")],
        )
    )
    assert ok is True

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


def test_should_push_to_erp_data_unavailable_distinct():
    """上游数据拉取失败必须区别于"无调整"业务态，给出 data_unavailable 原因。"""
    # 数据不可用 → 明确 data_unavailable（不是 "no adjustments"）
    ok, reason = should_push_to_erp(
        CampaignAnalysisResult(parent_asin="X", data_unavailable=True)
    )
    assert ok is False
    assert reason == "data_unavailable"

    # 回归：真正无调整仍返回 "no adjustments"，且不带 data_unavailable 信号
    ok, reason = should_push_to_erp(CampaignAnalysisResult(parent_asin="X"))
    assert ok is False
    assert reason == "no adjustments"

    # data_unavailable 优先于 adjustments 判断（即便残留 adjustments 也按数据失败处理，不误落库）
    ok, reason = should_push_to_erp(
        CampaignAnalysisResult(
            parent_asin="X",
            data_unavailable=True,
            adjustments=[CampaignAdjustmentItem(campaign_name="c", campaign_id="1")],
        )
    )
    assert ok is False
    assert reason == "data_unavailable"


def test_maybe_push_erp_surfaces_data_unavailable():
    """_maybe_push_erp 的 erp_write 字典必须带 data_unavailable=True，供批量区分。"""
    import asyncio

    from app.api.campaign import _maybe_push_erp

    out = asyncio.run(
        _maybe_push_erp(
            CampaignAnalysisResult(parent_asin="X", data_unavailable=True),
            asin="X", days=7, temperature=0.3, write_erp=True, state=None,
        )
    )
    assert out["attempted"] is False
    assert out["ok"] is False
    assert out["data_unavailable"] is True

    # 回归：普通"无调整"不带 data_unavailable，仍是良性跳过
    out2 = asyncio.run(
        _maybe_push_erp(
            CampaignAnalysisResult(parent_asin="X"),
            asin="X", days=7, temperature=0.3, write_erp=True, state=None,
        )
    )
    assert out2["attempted"] is False
    assert out2.get("data_unavailable") is not True
    assert out2["skipped"] == "no adjustments"


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
