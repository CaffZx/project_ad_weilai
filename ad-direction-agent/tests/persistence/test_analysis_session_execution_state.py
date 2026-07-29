from pathlib import Path

from app.persistence.state_manager import StateManager


def test_json_state_manager_tracks_and_clears_execution_started(tmp_path):
    m = StateManager(base_dir=tmp_path)

    assert m.set_analysis_session("B0TEST", "RUN1")
    sess = m.get_analysis_session("B0TEST")
    assert sess
    assert sess["run_id"] == "RUN1"
    assert sess.get("execution_started_at") is None

    assert m.mark_analysis_execution_started("B0TEST", "RUN1")
    sess = m.get_analysis_session("B0TEST")
    assert sess
    assert sess["run_id"] == "RUN1"
    assert sess.get("execution_started_at")

    assert m.clear_analysis_execution_started("B0TEST")
    sess = m.get_analysis_session("B0TEST")
    assert sess
    assert sess["run_id"] == "RUN1"
    assert sess.get("execution_started_at") is None


def test_json_state_manager_does_not_mark_mismatched_run_id(tmp_path):
    m = StateManager(base_dir=tmp_path)

    assert m.set_analysis_session("B0TEST", "RUN1")
    assert not m.mark_analysis_execution_started("B0TEST", "RUN2")

    sess = m.get_analysis_session("B0TEST")
    assert sess
    assert sess["run_id"] == "RUN1"
    assert sess.get("execution_started_at") is None


def test_json_state_manager_clear_reports_unlink_failure(tmp_path, monkeypatch):
    m = StateManager(base_dir=tmp_path)
    assert m.set_analysis_session("B0TEST", "RUN1")

    def _raise_unlink(_self):
        raise OSError("disk failure")

    monkeypatch.setattr(Path, "unlink", _raise_unlink)

    assert m.clear_analysis_session("B0TEST") is False
