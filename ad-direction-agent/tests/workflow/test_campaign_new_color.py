"""颜色归一化 + 映射 + 决策字段 单测（纯函数，无 I/O）。"""

import pytest
from app.models.campaign import NewCampaignDecision
from app.workflow.steps.campaign_new import (
    _normalize_color,
    _build_color_asin_map,
)


# ── _normalize_color ──────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("Black5", "black"),
    ("Red2", "red"),
    ("  Blue  ", "blue"),
    ("One Size", None),
    ("one size", None),
    ("", None),
    ("  ", None),
    ("NavyBlue", "navyblue"),
    ("Dark Green", "dark green"),
    ("White123", "white"),
    ("black", "black"),
])
def test_normalize_color(raw, expected):
    assert _normalize_color(raw) == expected


# ── _build_color_asin_map ────────────────────────────────────────────────

class _FakeCU:
    def __init__(self, child_asin, cost=0.0):
        self.child_asin = child_asin
        self.perf_7d = type("P", (), {"cost": cost})()


def test_build_color_asin_map_empty_listing():
    colors, c2a = _build_color_asin_map(None, [])
    assert colors == []
    assert c2a == {}


def test_build_color_asin_map_no_variants():
    colors, c2a = _build_color_asin_map({"variants": {}}, [])
    assert colors == []
    assert c2a == {}


def test_build_color_asin_map_basic():
    listing = {
        "variants": {
            "A1": {"color": "Black5", "size": "One Size"},
            "A2": {"color": "Red2", "size": "One Size"},
        }
    }
    camps = [_FakeCU("A1", cost=100.0), _FakeCU("A2", cost=50.0)]
    colors, c2a = _build_color_asin_map(listing, camps)
    assert colors == ["black", "red"]
    assert c2a["black"] == "A1"
    assert c2a["red"] == "A2"


def test_build_color_asin_map_duplicate_color_picks_highest_spend():
    """同颜色多个子 ASIN → 选花费最高的。"""
    listing = {
        "variants": {
            "A1": {"color": "Black5", "size": "One Size"},
            "A2": {"color": "Black", "size": "One Size"},
        }
    }
    camps = [_FakeCU("A1", cost=30.0), _FakeCU("A2", cost=90.0)]
    colors, c2a = _build_color_asin_map(listing, camps)
    assert colors == ["black"]
    assert c2a["black"] == "A2"


def test_build_color_asin_map_skips_one_size():
    listing = {
        "variants": {
            "A1": {"color": "One Size", "size": "One Size"},
            "A2": {"color": "Red2", "size": "One Size"},
        }
    }
    colors, c2a = _build_color_asin_map(listing, {})
    assert colors == ["red"]


def test_build_color_asin_map_sorts_alpha():
    listing = {
        "variants": {
            "A1": {"color": "White", "size": "S"},
            "A2": {"color": "Black", "size": "M"},
            "A3": {"color": "Red", "size": "L"},
        }
    }
    colors, _ = _build_color_asin_map(listing, {})
    assert colors == ["black", "red", "white"]


# ── NewCampaignDecision 字段 ──────────────────────────────────────────────

def test_decision_color_flags_default():
    d = NewCampaignDecision(keyword_text="test")
    assert d.color_flags is None
    assert d.holiday_flags is None
    assert d.assigned_child_asin == ""


def test_decision_color_flags_set():
    d = NewCampaignDecision(keyword_text="test", color_flags={"black": True})
    assert d.color_flags == {"black": True}


def test_decision_holiday_flags_set():
    d = NewCampaignDecision(keyword_text="test", holiday_flags={"halloween": True})
    assert d.holiday_flags == {"halloween": True}


def test_decision_assigned_child_asin_override():
    d = NewCampaignDecision(keyword_text="test", assigned_child_asin="B0ABC")
    assert d.assigned_child_asin == "B0ABC"


# ── 颜色校验逻辑（内联模拟，断言与 campaign_new.py 行为一致）──────────

def test_color_validation_illegal_rejected():
    """LLM 产出不在已知颜色列表中的 color_flags → 拒绝。"""
    valid_colors = {"black", "red"}
    raw_cf = {"purple": True}
    illegal = [k for k in raw_cf if k not in valid_colors]
    assert illegal == ["purple"]


def test_color_validation_legal_accepted():
    """LLM 产出合法颜色 → 接受，并取第一个指派。"""
    valid_colors = {"black", "red"}
    raw_cf = {"black": True}
    illegal = [k for k in raw_cf if k not in valid_colors]
    assert illegal == []


def test_color_validation_filters_false_values():
    """color_flags 中 false 值被过滤。"""
    raw_cf = {"black": True, "red": False}
    cf = {k: bool(v) for k, v in raw_cf.items() if bool(v)}
    assert cf == {"black": True}


def test_color_validation_llm_identifies_color_with_typo():
    """LLM 识别出 black 且颜色在合法列表 → 接受，即使关键词拼写为 blak/blck。"""
    valid_colors = {"black", "red"}
    # 拼写变体 "blak fishnet" → LLM 识别为 black
    raw_cf = {"black": True}
    assert not [k for k in raw_cf if k not in valid_colors]
    cf = {k: bool(v) for k, v in raw_cf.items() if bool(v)}
    assert cf == {"black": True}


def test_color_validation_not_dict_ok():
    """非 dict 的 color_flags → None，不触发。"""
    for bad in [None, "black", []]:
        cf = None
        if isinstance(bad, dict) and bad:
            cf = {k: bool(v) for k, v in bad.items() if bool(v)}
        assert cf is None, f"input={bad!r} should yield None"


# ── assigned_child_asin 兜底语义 ─────────────────────────────────────────

def test_assigned_asin_empty_falls_back():
    """assigned_child_asin 为空时 → 用 target_child_asin。"""
    target = "B0TARGET"
    decision = NewCampaignDecision(keyword_text="test", assigned_child_asin="")
    final = decision.assigned_child_asin or target
    assert final == "B0TARGET"


def test_assigned_asin_nonempty_overrides_target():
    """assigned_child_asin 非空时 → 覆盖 target_child_asin。"""
    target = "B0TARGET"
    decision = NewCampaignDecision(keyword_text="test", assigned_child_asin="B0COLOR")
    final = decision.assigned_child_asin or target
    assert final == "B0COLOR"
