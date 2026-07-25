import json
import math
import os
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass

import pytest
from _pytest.reports import TestReport

from pytest_warden.history import HistoryStore
from pytest_warden.jobobject import JobObject

_POLL_INTERVAL = 0.05
_HISTORY_WINDOW = 20
_DEFAULT_WEIGHT = 1.0


def pytest_addoption(parser):
    group = parser.getgroup("warden")
    group.addoption(
        "--warden",
        action="store_true",
        default=False,
        help="Distribute and supervise this run's tests across hard-killable pytest subprocesses.",
    )
    group.addoption(
        "--numprocesses",
        dest="warden_numprocesses",
        action="store",
        type=int,
        default=1,
        help="Number of hard-killable pytest worker subprocesses to distribute tests across.",
    )
    group.addoption(
        "--timeout",
        dest="warden_timeout",
        action="store",
        type=float,
        default=None,
        help="Per-test timeout in seconds; a test exceeding it hard-kills its whole worker subprocess.",
    )
    group.addoption(
        "--warden-history-db",
        dest="warden_history_db",
        action="store",
        default=None,
        help="Path to warden's SQLite history store, used for LPT batch sizing "
        "(default: <rootdir>/.pytest_warden/history.sqlite3).",
    )
    group.addoption(
        "--warden-quarantine-flaky",
        dest="warden_quarantine_flaky",
        action="store_true",
        default=False,
        help="A failure on a test whose recent history has both passes and "
        "failures is reported as xfail instead of failed, and doesn't fail the build.",
    )
    group.addoption(
        "--warden-work-stealing",
        dest="warden_work_stealing",
        action="store_true",
        default=False,
        help="Use dynamic chunk-based work-stealing instead of static LPT "
        "batching -- workers that finish early pull more work instead of idling.",
    )
    group.addoption(
        "--warden-chunk-size",
        dest="warden_chunk_size",
        action="store",
        type=int,
        default=None,
        help="Chunk size for --warden-work-stealing (default: total tests "
        "divided across roughly 4 chunks per worker).",
    )


class _HistoryCollector:
    """Collects every 'call'-phase report replayed through the real pytest
    hooks during a warden-controlled run, purely by listening to the same
    pytest_runtest_logreport hook the controller already drives -- no
    separate instrumentation of _replay_event/_report_incident needed."""

    def __init__(self):
        self.reports = []

    def pytest_runtest_logreport(self, report):
        if report.when == "call":
            self.reports.append(report)


def pytest_configure(config):
    if config.getoption("warden"):
        collector = _HistoryCollector()
        config.pluginmanager.register(collector, "warden-history-collector")
        config._warden_history_collector = collector


@pytest.hookimpl(tryfirst=True)
def pytest_runtestloop(session):
    if not session.config.getoption("warden"):
        return None
    _run_controller(session)
    return True


def _history_db_path(session):
    override = session.config.getoption("warden_history_db")
    if override:
        return override
    return str(session.config.rootpath / ".pytest_warden" / "history.sqlite3")


def _lpt_batch(node_ids, numprocesses, history_store, previously_failed=frozenset()):
    n = max(1, min(numprocesses, len(node_ids)))
    weights = {}
    for node_id in node_ids:
        durations = history_store.get_durations(node_id, window=_HISTORY_WINDOW)
        weights[node_id] = statistics.median(durations) if durations else _DEFAULT_WEIGHT

    def sort_key(node_id):
        # Previously-failed tests sort first (True > False), then by weight
        # descending within each group -- preserves --failed-first's intent
        # without giving up load-balancing among the rest.
        return (node_id in previously_failed, weights[node_id])

    order = sorted(node_ids, key=sort_key, reverse=True)
    loads = [0.0] * n
    batches = [[] for _ in range(n)]
    for node_id in order:
        i = min(range(n), key=lambda k: loads[k])
        batches[i].append(node_id)
        loads[i] += weights[node_id]
    return [batch for batch in batches if batch]


def _default_chunk_size(total, numprocesses):
    n = max(1, numprocesses)
    return max(1, math.ceil(total / (n * 4)))


def _chunk_queue(node_ids, history_store, chunk_size):
    weights = {}
    for node_id in node_ids:
        durations = history_store.get_durations(node_id, window=_HISTORY_WINDOW)
        weights[node_id] = statistics.median(durations) if durations else _DEFAULT_WEIGHT
    order = sorted(node_ids, key=lambda nid: weights[nid], reverse=True)
    return [order[i : i + chunk_size] for i in range(0, len(order), chunk_size)]


@dataclass
class _HistoryResult:
    test_id: str
    project: str | None
    duration: float
    outcome: str
    attempts: int = 1
    retries_configured: int = 0


def _is_flaky(history_store, node_id, window=_HISTORY_WINDOW):
    outcomes = {row["outcome"] for row in history_store.get_outcomes(node_id, window=window)}
    return "passed" in outcomes and "failed" in outcomes


def _maybe_quarantine(session, report):
    if report.when != "call" or report.outcome != "failed":
        return report
    if not session.config.getoption("warden_quarantine_flaky"):
        return report
    history_store = getattr(session.config, "_warden_history_store", None)
    if history_store is None or not _is_flaky(history_store, report.nodeid):
        return report
    report.outcome = "skipped"
    report.wasxfail = "warden: quarantined (historically flaky)"
    return report


def _record_history(session, history_store):
    collector = getattr(session.config, "_warden_history_collector", None)
    if collector is None:
        return
    results = [
        _HistoryResult(
            test_id=report.nodeid,
            project=None,
            duration=report.duration,
            outcome=report.outcome,
        )
        for report in collector.reports
    ]
    history_store.record_run(results)


def _cov_sources(session):
    return getattr(session.config.option, "cov_source", None)


def _spawn_worker(session, batch, progress_path, cov_data_file):
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        *batch,
        "-p",
        "pytest_warden.worker",
        f"--warden-progress-file={progress_path}",
        "-q",
    ]
    maxfail = session.config.getvalue("maxfail")
    if maxfail:
        cmd.append(f"--maxfail={maxfail}")

    env = None
    if cov_data_file:
        for src in _cov_sources(session):
            cmd.append(f"--cov={src}")
        cmd.append("--cov-report=")
        env = dict(os.environ)
        env["COVERAGE_FILE"] = cov_data_file

    proc = subprocess.Popen(
        cmd,
        cwd=str(session.config.rootpath),
        start_new_session=True,
        env=env,
    )
    job = JobObject()
    job.assign(proc.pid)
    return proc, job


class _Worker:
    def __init__(self, proc, job, progress_path, timeout, batch, cov_data_file=None):
        self.proc = proc
        self.job = job
        self.progress_path = progress_path
        self.timeout = timeout
        self.batch = batch
        self.cov_data_file = cov_data_file
        self.lines_consumed = 0
        self.deadline = time.monotonic() + timeout if timeout else None
        self.killed = False
        self.started_ids = set()
        self.current = None  # {"nodeid":..., "location":...} of the in-flight test


def _run_controller(session):
    node_ids = [item.nodeid for item in session.items]
    if not node_ids:
        return

    numprocesses = session.config.getoption("warden_numprocesses")
    timeout = session.config.getoption("warden_timeout")

    history_store = HistoryStore(_history_db_path(session))
    session.config._warden_history_store = history_store
    try:
        previously_failed = frozenset(session.config.cache.get("cache/lastfailed", {}))
        batches = _lpt_batch(node_ids, numprocesses, history_store, previously_failed)

        with tempfile.TemporaryDirectory(prefix="pytest-warden-") as tmpdir:
            worker_count = 0
            all_workers = []

            workers, worker_count = _run_wave(
                session, tmpdir, worker_count, batches, timeout
            )
            all_workers.extend(workers)

            retry_batches = [
                [nid for nid in worker.batch if nid not in worker.started_ids]
                for worker in workers
            ]
            retry_batches = [b for b in retry_batches if b]

            if retry_batches and not session.shouldfail:
                retry_workers, worker_count = _run_wave(
                    session, tmpdir, worker_count, retry_batches, timeout
                )
                all_workers.extend(retry_workers)
                for worker in retry_workers:
                    still_missing = [
                        nid for nid in worker.batch if nid not in worker.started_ids
                    ]
                    for nid in still_missing:
                        _report_never_ran(
                            session,
                            nid,
                            "warden: worker never reached this test even after one retry",
                        )

            session.config._warden_worker_count = worker_count
            _combine_coverage(session, all_workers)
            _record_history(session, history_store)
    finally:
        history_store.close()


def _run_wave(session, tmpdir, worker_count_start, batches, timeout):
    worker_count = worker_count_start
    workers = []
    cov_sources = _cov_sources(session)
    for batch in batches:
        progress_path = os.path.join(tmpdir, f"worker-{worker_count}.jsonl")
        cov_data_file = (
            os.path.join(tmpdir, f".coverage.worker-{worker_count}")
            if cov_sources
            else None
        )
        worker_count += 1
        open(progress_path, "a", encoding="utf-8").close()
        proc, job = _spawn_worker(session, batch, progress_path, cov_data_file)
        workers.append(_Worker(proc, job, progress_path, timeout, batch, cov_data_file))

    _supervise(session, workers)

    for worker in workers:
        worker.job.close()

    return workers, worker_count


def _combine_coverage(session, workers):
    if not _cov_sources(session):
        return
    data_files = [
        w.cov_data_file
        for w in workers
        if w.cov_data_file and os.path.exists(w.cov_data_file)
    ]
    if not data_files:
        return

    import coverage

    combined_path = str(session.config.rootpath / ".coverage")
    cov = coverage.Coverage(data_file=combined_path)
    cov.combine(data_files, strict=True)
    cov.save()
    session.config._warden_cov_data_file = combined_path


def _poll_once(session, workers):
    now = time.monotonic()
    still_pending = []
    for worker in workers:
        new_lines = _read_new_lines(worker)
        if new_lines:
            if worker.timeout:
                worker.deadline = now + worker.timeout
            for line in new_lines:
                _replay_event(session, worker, json.loads(line))

        if worker.proc.poll() is not None:
            for line in _read_new_lines(worker):
                _replay_event(session, worker, json.loads(line))
            if worker.current is not None:
                code = worker.proc.returncode
                _report_incident(
                    session,
                    worker,
                    f"warden: worker exited unexpectedly (code {code}) while running this test",
                )
            continue

        if worker.deadline is not None and now > worker.deadline and not worker.killed:
            worker.killed = True
            worker.job.terminate()
            _report_incident(
                session,
                worker,
                f"warden: hard-killed after exceeding {worker.timeout}s timeout",
            )

        still_pending.append(worker)
    return still_pending


def _supervise(session, workers):
    pending = list(workers)
    while pending:
        pending = _poll_once(session, pending)

        if session.shouldfail and pending:
            for worker in pending:
                worker.job.terminate()
            for worker in pending:
                worker.proc.wait()
            pending = []

        if pending:
            time.sleep(_POLL_INTERVAL)


def _read_new_lines(worker):
    with open(worker.progress_path, "r", encoding="utf-8") as fh:
        all_lines = fh.readlines()
    complete = [line for line in all_lines if line.endswith("\n")]
    new = complete[worker.lines_consumed :]
    worker.lines_consumed = len(complete)
    return [line.strip() for line in new]


def _replay_event(session, worker, event):
    hook = session.config.hook
    config = session.config
    kind = event["kind"]
    if kind == "logstart":
        worker.started_ids.add(event["nodeid"])
        worker.current = {"nodeid": event["nodeid"], "location": event["location"]}
        hook.pytest_runtest_logstart(
            nodeid=event["nodeid"], location=tuple(event["location"])
        )
    elif kind == "logreport":
        report = hook.pytest_report_from_serializable(config=config, data=event["data"])
        report = _maybe_quarantine(session, report)
        hook.pytest_runtest_logreport(report=report)
    elif kind == "logfinish":
        worker.current = None
        hook.pytest_runtest_logfinish(
            nodeid=event["nodeid"], location=tuple(event["location"])
        )


def _report_incident(session, worker, message):
    current = worker.current
    if current is None:
        return
    hook = session.config.hook
    location = tuple(current["location"])
    report = TestReport(
        nodeid=current["nodeid"],
        location=location,
        keywords={},
        outcome="failed",
        longrepr=message,
        when="call",
        sections=[],
        duration=worker.timeout or 0,
        user_properties=[],
    )
    hook.pytest_runtest_logreport(report=report)
    hook.pytest_runtest_logfinish(nodeid=current["nodeid"], location=location)
    worker.current = None


def _report_never_ran(session, nodeid, message):
    hook = session.config.hook
    fspath = nodeid.split("::", 1)[0]
    location = (fspath, None, nodeid)
    hook.pytest_runtest_logstart(nodeid=nodeid, location=location)
    report = TestReport(
        nodeid=nodeid,
        location=location,
        keywords={},
        outcome="failed",
        longrepr=message,
        when="call",
        sections=[],
        duration=0,
        user_properties=[],
    )
    hook.pytest_runtest_logreport(report=report)
    hook.pytest_runtest_logfinish(nodeid=nodeid, location=location)


def pytest_terminal_summary(terminalreporter, config):
    count = getattr(config, "_warden_worker_count", None)
    if count is not None:
        terminalreporter.write_line(f"warden: distributed across {count} worker(s)")
