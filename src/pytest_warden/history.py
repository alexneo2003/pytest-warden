"""
Historical timing/outcome store, vendored from ctrlrunner's
src/ctrlrunner/reporting/history.py with no import dependency on that
project. Trimmed to the storage/query core (HistoryStore +
compute_near_timeout_test_ids); ctrlrunner's config-file parsing
(HistoryConfig/resolve_history_config) and its ConsoleReporter-based
HistoryReporter aren't applicable here -- pytest-warden records history
itself, directly from its own controller loop, keyed by pytest node id
instead of ctrlrunner's own test_id format.

A single consolidated SQLite file (not one-file-per-test -- deliberately,
per the same Windows-CI-first principle the original module documents:
thousands of small files is a known pain point for AV scanners and
filesystem metadata overhead on Windows CI runners).
"""

import contextlib
import os
import sqlite3
import statistics
import sys
import time
import uuid
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    started_at REAL,
    project TEXT
);
CREATE TABLE IF NOT EXISTS test_runs (
    run_id TEXT,
    test_id TEXT,
    project TEXT,
    duration REAL,
    outcome TEXT,
    attempts INTEGER,
    retries_configured INTEGER,
    worker_id INTEGER,
    FOREIGN KEY(run_id) REFERENCES runs(run_id)
);
CREATE INDEX IF NOT EXISTS idx_test_runs_test_id ON test_runs(test_id, project);
"""

# Rows recorded with these outcomes never actually executed -- the LPT
# median and the flake-score denominator must both be computed only from
# rows that ran, so every READ path filters them out here. record_run
# deliberately does NOT filter on write -- history should still record
# everything for later inspection; only reads need the filter.
_NON_EXECUTED_OUTCOMES = ("not_run", "cancelled")
_NON_EXECUTED_OUTCOMES_SQL = ", ".join("?" for _ in _NON_EXECUTED_OUTCOMES)

# Duration reads additionally exclude skipped rows -- those record
# duration~0.0 for a test that never ran its body this time, and would
# otherwise drag the LPT median toward zero. Outcome reads (flake score)
# deliberately keep seeing them.
_NON_DURATION_OUTCOMES = ("not_run", "cancelled", "skipped", "fixme")
_NON_DURATION_OUTCOMES_SQL = ", ".join("?" for _ in _NON_DURATION_OUTCOMES)


class HistoryStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        try:
            self._open(db_path)
        except sqlite3.DatabaseError:
            # A truncated/garbage file must not permanently brick every
            # subsequent run -- history is an optimization. Quarantine the
            # unreadable file for post-mortem and start a fresh store in
            # its place.
            with contextlib.suppress(Exception):
                self._conn.close()
            os.replace(db_path, db_path + ".corrupt")
            self._open(db_path)
        self._migrate_schema()
        if sys.platform != "win32":
            try:
                if os.path.exists(db_path):
                    os.chmod(db_path, 0o600)
            except OSError:
                pass  # best-effort hardening

    def _open(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path, timeout=10.0)
        self._conn.execute("PRAGMA busy_timeout = 10000")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def _migrate_schema(self):
        cols = [row[1] for row in self._conn.execute("PRAGMA table_info(test_runs)")]
        if cols and "worker_id" not in cols:
            try:
                self._conn.execute("ALTER TABLE test_runs ADD COLUMN worker_id INTEGER")
                self._conn.commit()
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc):
                    raise

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def record_run(
        self,
        results: list,
        project: str | None = None,
        run_id: str | None = None,
        started_at: float | None = None,
    ) -> str:
        """Writes one `runs` row plus one `test_runs` row per result, all in
        a single transaction -- a run interrupted before this is called
        simply never gets recorded, rather than leaving a partial entry.
        Each result is expected to expose test_id, project, duration,
        outcome, attempts, retries_configured, and optionally worker_id."""
        if not results:
            return run_id or ""

        run_id = run_id or str(uuid.uuid4())
        started_at = started_at if started_at is not None else time.time()
        run_project = project
        for r in results:
            if getattr(r, "project", None):
                run_project = r.project
                break

        with self._conn:
            self._conn.execute(
                "INSERT INTO runs (run_id, started_at, project) VALUES (?, ?, ?)",
                (run_id, started_at, run_project),
            )
            self._conn.executemany(
                "INSERT INTO test_runs (run_id, test_id, project, duration, outcome, "
                "attempts, retries_configured, worker_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        run_id,
                        r.test_id,
                        r.project,
                        r.duration,
                        r.outcome,
                        r.attempts,
                        r.retries_configured,
                        getattr(r, "worker_id", None),
                    )
                    for r in results
                ],
            )
        return run_id

    def get_durations(self, test_id: str, project: str | None = None, window: int = 20) -> list:
        """Most recent `window` durations for a test, newest first --
        LPT sharding weight and flake-score window both consume this."""
        cur = self._conn.execute(
            "SELECT test_runs.duration FROM test_runs "
            "JOIN runs ON runs.run_id = test_runs.run_id "
            "WHERE test_runs.test_id = ? AND "
            "(test_runs.project = ? OR (test_runs.project IS NULL AND ? IS NULL)) AND "
            f"test_runs.outcome NOT IN ({_NON_DURATION_OUTCOMES_SQL}) "
            "ORDER BY runs.started_at DESC LIMIT ?",
            (test_id, project, project, *_NON_DURATION_OUTCOMES, window),
        )
        return [row[0] for row in cur.fetchall()]

    def get_outcomes(self, test_id: str, project: str | None = None, window: int = 20) -> list:
        """Most recent `window` (outcome, attempts, retries_configured)
        rows for a test, newest first -- flake-score input."""
        cur = self._conn.execute(
            "SELECT test_runs.outcome, test_runs.attempts, test_runs.retries_configured "
            "FROM test_runs "
            "JOIN runs ON runs.run_id = test_runs.run_id "
            "WHERE test_runs.test_id = ? AND "
            "(test_runs.project = ? OR (test_runs.project IS NULL AND ? IS NULL)) AND "
            f"test_runs.outcome NOT IN ({_NON_EXECUTED_OUTCOMES_SQL}) "
            "ORDER BY runs.started_at DESC LIMIT ?",
            (test_id, project, project, *_NON_EXECUTED_OUTCOMES, window),
        )
        return [
            {"outcome": row[0], "attempts": row[1], "retries_configured": row[2]}
            for row in cur.fetchall()
        ]

    def list_test_ids(self, project: str | None = None) -> list:
        """Every distinct test_id with at least one recorded run, scoped by
        project."""
        cur = self._conn.execute(
            "SELECT DISTINCT test_id FROM test_runs WHERE "
            "(project = ? OR (project IS NULL AND ? IS NULL)) ORDER BY test_id",
            (project, project),
        )
        return [row[0] for row in cur.fetchall()]


def compute_near_timeout_test_ids(
    test_ids: list,
    timeouts: dict,
    history_store: "HistoryStore",
    project: str | None = None,
    window: int = 20,
    threshold: float = 0.8,
) -> set:
    """Hang-risk heuristic: flags a test_id whose median historical
    duration sits at or above `threshold` (default 80%) of its configured
    timeout. A test with no history yet is never flagged (nothing to
    compare against), and a test with no configured timeout is never
    flagged (nothing to compare it to)."""
    flagged: set = set()
    for test_id in test_ids:
        durations = history_store.get_durations(test_id, project=project, window=window)
        if not durations:
            continue
        median = statistics.median(durations)
        timeout = timeouts.get(test_id)
        if timeout and median >= threshold * timeout:
            flagged.add(test_id)
    return flagged
