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


def test_corrupt_history_db_file_is_quarantined_and_a_fresh_store_opens_successfully(tmp_path):
    db_path = tmp_path / "history.sqlite3"
    db_path.write_bytes(b"this is not a sqlite database")

    store = HistoryStore(str(db_path))
    try:
        corrupt_path = tmp_path / "history.sqlite3.corrupt"
        assert corrupt_path.exists(), "the garbage file should have been quarantined"
        assert corrupt_path.read_bytes() == b"this is not a sqlite database"

        # the fresh store in its place must be fully usable
        store.record_run(
            [_Result(test_id="test_mod.py::test_a", project=None, duration=0.2, outcome="passed")]
        )
        assert store.get_durations("test_mod.py::test_a") == [0.2]
    finally:
        store.close()


def test_history_db_missing_parent_directory_is_created(tmp_path):
    db_path = tmp_path / "nested" / "dirs" / "history.sqlite3"
    store = HistoryStore(str(db_path))
    try:
        assert db_path.exists()
        store.record_run(
            [_Result(test_id="test_mod.py::test_a", project=None, duration=0.1, outcome="passed")]
        )
        assert store.get_durations("test_mod.py::test_a") == [0.1]
    finally:
        store.close()


def test_concurrent_history_store_access_does_not_deadlock_or_corrupt(pytester):
    # Two real `pytest --warden` subprocess runs sharing one history DB
    # path, launched concurrently -- SQLite's file-locking semantics are
    # process-boundary-sensitive (busy_timeout papers over contention), so
    # this needs real separate OS processes, not two connections in the
    # same process, to exercise the actual lock path.
    import subprocess
    import sys

    pytester.makepyfile(
        test_mod="""
        def test_ok():
            assert True
        """
    )
    history_db = str(pytester.path / "shared_history.sqlite3")
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "--warden",
        f"--warden-history-db={history_db}",
        "-q",
        str(pytester.path),
    ]
    procs = [
        subprocess.Popen(cmd, cwd=str(pytester.path), env=None) for _ in range(3)
    ]
    returncodes = [p.wait(timeout=30) for p in procs]
    assert all(rc == 0 for rc in returncodes), f"expected all runs to succeed, got {returncodes}"

    store = HistoryStore(history_db)
    try:
        durations = store.get_durations("test_mod.py::test_ok")
        assert len(durations) == 3, (
            f"expected all 3 concurrent runs to have recorded history, got {durations}"
        )
    finally:
        store.close()
