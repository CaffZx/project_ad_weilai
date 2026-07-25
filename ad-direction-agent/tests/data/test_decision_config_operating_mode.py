"""ERP decision_config 的经营模式读回契约。"""
from __future__ import annotations

from app.data.decision_config_reader import load_layer14


class _Cursor:
    def __init__(self, row: dict):
        self._row = row

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, *_args):
        return None

    def fetchone(self):
        return self._row


class _Conn:
    def __init__(self, row: dict):
        self._row = row

    def cursor(self):
        return _Cursor(self._row)

    def close(self):
        return None


def test_load_layer14_reads_nullable_operating_mode(monkeypatch):
    row = {
        "product_position": "P1_PRODUCT",
        "product_stage": "PROMOTING",
        "season_type": "PEAK_SEASON_PREPARE",
        "operating_mode": "CONTROLLED_CLEARANCE",
        "advert_purposes": "[]",
        "target_keyword_types": "[]",
        "advert_direction_types": "[]",
        "day_range": "DAY_7",
    }
    monkeypatch.setattr(
        "app.data.decision_config_reader._get_repository",
        lambda: type("Repo", (), {"_connect": lambda _self: _Conn(row)})(),
    )

    result = load_layer14("B0TEST")

    assert result is not None
    assert result["long_term"]["operating_mode"] == "控制清货"
