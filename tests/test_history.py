from dataclasses import dataclass

from pytest_warden.history import HistoryStore


@dataclass
class _Result:
    test_id: str
    project: str | None
    duration: float
    outcome: str
    attempts: int = 1
    retries_configured: int = 0
    worker_id: int | None = None


def test_record_and_read_back_durations(tmp_path):
    db_path = str(tmp_path / "history.sqlite3")
    store = HistoryStore(db_path)
    try:
        store.record_run(
            [_Result(test_id="test_mod.py::test_a", project=None, duration=0.5, outcome="passed")]
        )
        store.record_run(
            [_Result(test_id="test_mod.py::test_a", project=None, duration=0.7, outcome="passed")]
        )

        durations = store.get_durations("test_mod.py::test_a")
        assert durations == [0.7, 0.5]  # newest first
    finally:
        store.close()


def test_get_durations_excludes_skipped_rows(tmp_path):
    db_path = str(tmp_path / "history.sqlite3")
    store = HistoryStore(db_path)
    try:
        store.record_run(
            [_Result(test_id="test_mod.py::test_a", project=None, duration=0.0, outcome="skipped")]
        )
        assert store.get_durations("test_mod.py::test_a") == []
    finally:
        store.close()


def test_get_outcomes_round_trip(tmp_path):
    db_path = str(tmp_path / "history.sqlite3")
    store = HistoryStore(db_path)
    try:
        store.record_run(
            [_Result(test_id="test_mod.py::test_a", project=None, duration=0.1, outcome="failed")]
        )
        outcomes = store.get_outcomes("test_mod.py::test_a")
        assert outcomes == [{"outcome": "failed", "attempts": 1, "retries_configured": 0}]
    finally:
        store.close()


def test_history_persists_across_store_instances(tmp_path):
    db_path = str(tmp_path / "history.sqlite3")
    store_a = HistoryStore(db_path)
    try:
        store_a.record_run(
            [_Result(test_id="test_mod.py::test_a", project=None, duration=0.3, outcome="passed")]
        )
    finally:
        store_a.close()

    store_b = HistoryStore(db_path)
    try:
        assert store_b.get_durations("test_mod.py::test_a") == [0.3]
    finally:
        store_b.close()
