"""校验结果运营可读文案"""

from app.core.validation_ops import format_validation_item_ops, enrich_validation_item
from app.llm.reasoner import LLMReasoner
from app.models.validation import ValidationItem


def test_format_validation_item_ops_no_rule_id():
    item = ValidationItem(
        rule_id="BM-3",
        level="confirmed",
        message="ACOS 28.2% 在合理区间 [12%, 30%]",
    )
    text = format_validation_item_ops(item)
    assert "BM-3" not in text
    assert "ACOS目标区间" in text
    assert "检查通过" in text
    assert "28.2%" in text


def test_enrich_validation_item_sets_display_message():
    item = enrich_validation_item(ValidationItem(
        rule_id="OA-1",
        level="force_correct",
        message="3 个词 ACOS ≥ 50%（严重超标），必须优化",
    ))
    assert item.display_message
    assert "OA-1" not in item.display_message
    assert "关键词ACOS" in item.display_message
    assert "需立即处理" in item.display_message


def test_sanitize_strips_llm_rule_id_echo():
    raw = "BM-1确认指标波动在容忍范围内；OA-1强制优化3个超标词"
    out = LLMReasoner._sanitize_ops_text(raw)
    assert "BM-1" not in out
    assert "OA-1" not in out
    assert "指标波动" in out or "容忍" in out
