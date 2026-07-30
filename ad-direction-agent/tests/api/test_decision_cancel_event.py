import asyncio

from app.api import decision


class _FakeState:
    def __init__(self):
        self.calls = []

    def get_analysis_session(self, asin):
        self.calls.append(("get_session", asin))
        return {"run_id": "RUN-1"}

    def cancel_and_release_analysis_session(self, asin, run_id):
        self.calls.append(("cancel_and_release", asin, run_id))
        return True


def test_cancel_event_tombstones_and_releases_current_run_for_immediate_restart(monkeypatch):
    state = _FakeState()
    monkeypatch.setattr(decision, "get_state_manager", lambda: state)

    res = asyncio.run(decision.cancel_decision_event({"asin": "B0TEST"}))

    assert res == {"ok": True, "run_id": "RUN-1", "status": "FINISHED"}
    assert state.calls == [
        ("get_session", "B0TEST"),
        ("cancel_and_release", "B0TEST", "RUN-1"),
    ]
