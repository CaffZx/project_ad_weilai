import asyncio

from app.api import decision


class _FakeState:
    def __init__(self):
        self.calls = []

    def clear_analysis_execution_started(self, asin):
        self.calls.append(("clear_execution_started", asin))

    def clear_analysis_session(self, asin):
        self.calls.append(("clear_session", asin))
        return True


def test_cancel_event_clears_session_without_redundant_execution_clear(monkeypatch):
    state = _FakeState()
    monkeypatch.setattr(decision, "get_state_manager", lambda: state)

    res = asyncio.run(decision.cancel_decision_event({"asin": "B0TEST"}))

    assert res == {"ok": True}
    assert state.calls == [("clear_session", "B0TEST")]
