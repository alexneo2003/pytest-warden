import contextlib
import json
import math
import os
import statistics
import subprocess
import sys
import tempfile
import time
import warnings
from dataclasses import dataclass

import pytest
from _pytest.reports import TestReport

from pytest_warden import coordination
from pytest_warden.history import HistoryStore
from pytest_warden.jobobject import JobObject

_POLL_INTERVAL = 0.05
_HISTORY_WINDOW = 20
_DEFAULT_WEIGHT = 1.0
_MAXFAIL_GRACE_PERIOD = 2.0

# Every _Worker spawned by the current _run_controller call, so a
# KeyboardInterrupt (or any other exception) reaching _run_controller can
# hard-kill every still-running worker before propagating -- workers run in
# their own process group specifically so a hung one can be killed without
# touching the controller, but that same isolation means Ctrl-C on the
# controller doesn't reach them either unless something does this explicitly.
_ACTIVE_WORKERS = []


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
        type=str,
        default="1",
        help="Number of hard-killable pytest worker subprocesses to distribute "
        "tests across. Accepts a positive integer, a percentage of the "
        "available CPU count (e.g. '50%%'), 'auto' (physical CPU count, "
        "requires psutil, falls back to logical), or 'logical' (logical "
        "CPU count).",
    )
    group.addoption(
        "--timeout",
        dest="warden_timeout",
        action="store",
        type=float,
        default=None,
        help="Default per-test timeout in seconds, for any test without its own "
        "@pytest.mark.timeout(seconds); a test exceeding its timeout hard-kills "
        "its whole worker subprocess.",
    )
    parser.addini(
        "timeout",
        default=None,
        help="Default per-test timeout in seconds (same as --timeout), for any "
        "test without its own @pytest.mark.timeout(seconds); used only when "
        "--timeout isn't passed on the command line.",
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
    group.addoption(
        "--warden-dist",
        dest="warden_dist",
        action="store",
        choices=["test", "loadfile", "loadscope", "loadgroup"],
        default="test",
        help="Which tests must land on the same worker subprocess together. "
        "'test' (default): no grouping, today's per-test distribution. "
        "'loadfile': every test in the same file. 'loadscope': every test in "
        "the same class, or the same module for top-level functions. "
        "'loadgroup': tests sharing @pytest.mark.warden_group(name=...); "
        "ungrouped tests are unaffected. Orthogonal to --warden-work-stealing "
        "(this controls WHICH tests must stay together; that controls HOW "
        "batches/chunks get dispatched).",
    )
    group.addoption(
        "--warden-disable-worker-plugin",
        dest="warden_disable_worker_plugin",
        action="append",
        default=None,
        metavar="NAME",
        help="Disable a NAMED third-party pytest plugin inside worker "
        "subprocesses only (passed to each worker as `-p no:NAME`), so its "
        "hooks fire once via the controller's replay instead of once for "
        "real in the worker AND once via replay. Repeatable. Only works for "
        "plugins registered under a name (a pytest11 entry point, or an "
        "explicit pluginmanager.register(..., name=...) call) -- it cannot "
        "disable a bare hookimpl defined directly in a conftest.py; see the "
        "PYTEST_WARDEN_WORKER environment variable in the README for that case.",
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
    # Registered unconditionally (not just under --warden) so the mark is
    # recognized -- and PytestUnknownMarkWarning stays quiet -- in a bare
    # run too; it's simply inert there, same as --timeout itself.
    config.addinivalue_line(
        "markers",
        "timeout(seconds): under --warden, hard-kill this test's whole worker "
        "subprocess if it runs longer than `seconds`, overriding --timeout for "
        "this test only. Has no effect without --warden.",
    )
    config.addinivalue_line(
        "markers",
        "warden_group(name): under --warden --warden-dist=loadgroup, run this "
        "test on the same worker subprocess as every other test sharing the "
        "same group `name`. Has no effect otherwise.",
    )
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


@pytest.fixture(scope="session")
def warden_run_once(request, tmp_path_factory):
    """Session-scoped fixture handing back a `run_once(key, fn)` callable
    that runs `fn` exactly once across every worker of a single `--warden`
    invocation, with every caller (including the one that actually ran
    `fn`) getting the same result. Works unmodified in bare (non-`--warden`)
    runs too -- there's no cross-process contention there, but it goes
    through the exact same `coordination.run_once` code path rather than a
    separate no-op shortcut, so there's exactly one implementation to keep
    correct. Registered here (not in worker.py) specifically so it's
    available in every process, not just worker subprocesses.
    """
    run_dir = request.config.getoption("warden_run_dir", default=None)
    if not run_dir:
        # Bare run, or the top-level controller process itself (which
        # never gets --warden-run-dir -- only workers do): no other
        # process to coordinate with, but route through the same shared-dir
        # convention pytest-xdist's own community recipe uses for this
        # exact problem (tmp_path_factory's own base temp dir's parent is
        # stable for the whole session).
        run_dir = str(tmp_path_factory.getbasetemp().parent)

    def _call(key, fn):
        return coordination.run_once(key, run_dir, fn)

    return _call


def _history_db_path(session):
    override = session.config.getoption("warden_history_db")
    if override:
        return override
    return str(session.config.rootpath / ".pytest_warden" / "history.sqlite3")


def _group_node_ids(node_ids, group_key_fn):
    groups = {}
    for node_id in node_ids:
        groups.setdefault(group_key_fn(node_id), []).append(node_id)
    return groups


def _lpt_batch(
    node_ids, numprocesses, history_store, previously_failed=frozenset(), group_key_fn=None
):
    group_key_fn = group_key_fn or (lambda node_id: node_id)
    weights = {}
    for node_id in node_ids:
        durations = history_store.get_durations(node_id, window=_HISTORY_WINDOW)
        weights[node_id] = statistics.median(durations) if durations else _DEFAULT_WEIGHT

    groups = _group_node_ids(node_ids, group_key_fn)
    group_weight = {key: sum(weights[nid] for nid in members) for key, members in groups.items()}
    group_failed = {
        key: any(nid in previously_failed for nid in members) for key, members in groups.items()
    }
    n = max(1, min(numprocesses, len(groups)))

    def sort_key(key):
        # Groups containing a previously-failed test sort first (True > False),
        # then by total weight descending within each group -- preserves
        # --failed-first's intent without giving up load-balancing among the rest.
        return (group_failed[key], group_weight[key])

    order = sorted(groups, key=sort_key, reverse=True)
    loads = [0.0] * n
    batches = [[] for _ in range(n)]
    for key in order:
        i = min(range(n), key=lambda k: loads[k])
        batches[i].extend(groups[key])
        loads[i] += group_weight[key]
    return [batch for batch in batches if batch]


def _marker_timeout_seconds(item):
    marker = item.get_closest_marker("timeout")
    if marker is None:
        return None
    if not marker.args:
        raise pytest.UsageError(
            f"{item.nodeid}: @pytest.mark.timeout(...) requires a seconds argument"
        )
    value = float(marker.args[0])
    if value <= 0:
        raise pytest.UsageError(
            f"{item.nodeid}: @pytest.mark.timeout seconds must be positive, got {value}"
        )
    return value


def _group_key(item, dist_mode):
    nodeid = item.nodeid
    if dist_mode == "loadfile":
        return nodeid.split("::", 1)[0]
    if dist_mode == "loadscope":
        return nodeid.rsplit("::", 1)[0]
    if dist_mode == "loadgroup":
        marker = item.get_closest_marker("warden_group")
        if marker is not None:
            name = marker.kwargs.get("name") or (marker.args[0] if marker.args else None)
            if not name:
                raise pytest.UsageError(
                    f"{nodeid}: @pytest.mark.warden_group(...) requires a name argument"
                )
            # NUL-prefixed so a group name can never collide with a real nodeid.
            return f"\0{name}"
    return nodeid


def _available_cpu_count():
    # sched_getaffinity respects cgroup/container CPU limits (Linux only);
    # everywhere else, cpu_count() is the best available signal.
    sched_getaffinity = getattr(os, "sched_getaffinity", None)
    if sched_getaffinity is not None:
        try:
            return len(sched_getaffinity(0))
        except OSError:
            pass
    return os.cpu_count() or 1


def _detect_physical_cpu_count():
    # A pure probe: None means "couldn't tell" (psutil missing, or unable to
    # determine physical count), leaving the fallback decision to the caller
    # rather than baking one in here.
    try:
        import psutil
    except ImportError:
        return None
    return psutil.cpu_count(logical=False) or None


def _resolve_numprocesses(raw, cpu_count_fn=None, physical_cpu_count_fn=None):
    cpu_count_fn = cpu_count_fn or _available_cpu_count
    physical_cpu_count_fn = physical_cpu_count_fn or _detect_physical_cpu_count
    text = raw.strip()
    error = pytest.UsageError(
        "--numprocesses must be a positive integer, a percentage like '50%', "
        f"'auto', or 'logical', got {raw!r}"
    )
    lowered = text.lower()
    if lowered == "auto":
        return physical_cpu_count_fn() or cpu_count_fn()
    if lowered == "logical":
        return cpu_count_fn()
    if text.endswith("%"):
        try:
            pct = float(text[:-1])
        except ValueError:
            raise error from None
        if pct <= 0:
            raise error
        # A tiny-but-positive percentage rounding down to 0 workers still
        # means "as few as possible", not "none" -- clamp up to 1 rather
        # than treating it as an error, unlike an explicitly non-positive
        # request (0%/negative), which IS rejected above.
        return max(1, round(cpu_count_fn() * pct / 100))

    try:
        value = int(text)
    except ValueError:
        raise error from None
    if value <= 0:
        raise error
    return value


def _default_chunk_size(total, numprocesses):
    n = max(1, numprocesses)
    return max(1, math.ceil(total / (n * 4)))


def _chunk_queue(
    node_ids, history_store, chunk_size, previously_failed=frozenset(), group_key_fn=None
):
    group_key_fn = group_key_fn or (lambda node_id: node_id)
    weights = {}
    for node_id in node_ids:
        durations = history_store.get_durations(node_id, window=_HISTORY_WINDOW)
        weights[node_id] = statistics.median(durations) if durations else _DEFAULT_WEIGHT

    groups = _group_node_ids(node_ids, group_key_fn)
    group_weight = {key: sum(weights[nid] for nid in members) for key, members in groups.items()}
    group_failed = {
        key: any(nid in previously_failed for nid in members) for key, members in groups.items()
    }

    def sort_key(key):
        # Same priority rule as _lpt_batch: groups containing a
        # previously-failed test sort first, then by total weight --
        # work-stealing must not lose --failed-first's intent just because
        # it uses a different batching function than static mode does.
        return (group_failed[key], group_weight[key])

    order = sorted(groups, key=sort_key, reverse=True)
    chunks = []
    current = []
    for key in order:
        members = groups[key]
        # A single oversized group still becomes its own one chunk rather
        # than being split -- only flush when there's already something in
        # `current` to flush.
        if current and len(current) + len(members) > chunk_size:
            chunks.append(current)
            current = []
        current.extend(members)
    if current:
        chunks.append(current)
    return chunks


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
            outcome=getattr(report, "_warden_original_outcome", report.outcome),
        )
        for report in collector.reports
    ]
    history_store.record_run(results)


def _cov_sources(session):
    return getattr(session.config.option, "cov_source", None)


def _spawn_worker(session, batch, progress_path, cov_data_file, run_dir):
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        *batch,
        "-p",
        "pytest_warden.worker",
        f"--warden-progress-file={progress_path}",
        f"--warden-run-dir={run_dir}",
        "-q",
    ]
    maxfail = session.config.getvalue("maxfail")
    if maxfail:
        cmd.append(f"--maxfail={maxfail}")

    for name in session.config.getoption("warden_disable_worker_plugin") or []:
        cmd.extend(["-p", f"no:{name}"])

    # Always set, not just when coverage is active, so a conftest.py
    # hookimpl can self-silence in workers (see PYTEST_WARDEN_WORKER in the
    # README) and rely solely on the controller's replay -- dict(os.environ)
    # reproduces Popen's own default full-environment inheritance, so this
    # is a pure addition for the non-coverage path, not a behavior change.
    env = dict(os.environ)
    env["PYTEST_WARDEN_WORKER"] = "1"
    if cov_data_file:
        for src in _cov_sources(session):
            cmd.append(f"--cov={src}")
        cmd.append("--cov-report=")
        env["COVERAGE_FILE"] = cov_data_file

    proc = subprocess.Popen(
        cmd,
        cwd=str(session.config.rootpath),
        start_new_session=True,
        env=env,
        # Without this, the worker's own -q terminal output (session
        # banner, dots, tracebacks, summary line) inherits the controller's
        # real stdout/stderr fds and prints directly to the terminal, on
        # top of -- and interleaved with -- the controller's own replayed
        # reporting for the exact same tests. The replayed report already
        # carries the real traceback/outcome, so the worker's raw output
        # is pure duplication, not additional information.
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    job = JobObject()
    job.assign(proc.pid)
    return proc, job


class _Worker:
    def __init__(
        self,
        proc,
        job,
        worker_index,
        progress_path,
        timeout,
        batch,
        cov_data_file=None,
        is_retry=False,
    ):
        self.proc = proc
        self.job = job
        self.worker_index = worker_index
        self.progress_path = progress_path
        self.timeout = timeout
        self.batch = batch
        self.cov_data_file = cov_data_file
        self.is_retry = is_retry
        self.byte_offset = 0
        self.deadline = time.monotonic() + timeout if timeout else None
        # Budget for whichever test is currently in-flight (self.current) --
        # kept in lockstep with it by _replay_event's logstart branch, which
        # looks up that test's own @pytest.mark.timeout override (falling
        # back to this worker's `timeout` default). Starts equal to `timeout`
        # itself purely to cover the window before the first logstart event.
        self.current_timeout = timeout
        self.killed = False
        self.started_ids = set()
        self.current = None  # {"nodeid":..., "location":...} of the in-flight test
        _ACTIVE_WORKERS.append(self)


def _terminate_all(workers):
    for worker in workers:
        with contextlib.suppress(Exception):
            worker.job.terminate()
    for worker in workers:
        with contextlib.suppress(Exception):
            worker.proc.wait(timeout=5)


def _warden_write_line(session, message):
    # getattr-safe throughout: test_progress_channel.py drives _replay_event
    # (which uses this for the -v progress line) against a bare
    # types.SimpleNamespace(hook=hook) stand-in for session.config, with no
    # real `pluginmanager` -- this must degrade to a no-op there rather than
    # raise, since that rig is only testing controller parsing robustness,
    # not terminal output.
    pluginmanager = getattr(session.config, "pluginmanager", None)
    if pluginmanager is None:
        return
    reporter = pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        # reporter.write_line() calls TerminalReporter.ensure_newline(),
        # but that only emits a newline when self.currentfspath is set --
        # and warden always disables fspath grouping (showfspath = False,
        # set in _run_controller), so it's permanently None and
        # ensure_newline() is a permanent no-op here. Without checking the
        # writer's own line-position tracking instead, any raw,
        # not-yet-newline-terminated pytest output already on the current
        # line (a bare dot/letter under default or -q verbosity, always
        # possible from _report_incident/_report_never_ran, which don't go
        # through _replay_event's dot-suppression) would visually smash
        # into this message, e.g. "FFwarden: worker 0 didn't finish...".
        if reporter._tw.width_of_current_line:
            reporter._tw.line()
        reporter.write_line(message)


def _run_controller(session):
    node_ids = [item.nodeid for item in session.items]
    if not node_ids:
        return

    # A controller is never itself a worker for ITS OWN invocation -- but
    # if this process happens to be running AS a worker of some OUTER,
    # unrelated warden invocation (e.g. a test suite that dogfoods itself
    # via `pytester.runpytest("--warden", ...)` from inside a warden
    # worker), PYTEST_WARDEN_WORKER=1 would already be set in its ambient
    # environment, inherited from that outer context. Left alone, a
    # conftest.py hookimpl using the documented self-guard pattern would
    # incorrectly suppress THIS controller's own replay too. _spawn_worker
    # always re-sets it explicitly to "1" for each of ITS OWN workers, so
    # clearing it here only affects how this process's OWN in-process
    # hook calls (the replay) see it -- it does not affect real workers.
    os.environ.pop("PYTEST_WARDEN_WORKER", None)

    # Same fix xdist itself applies (xdist/plugin.py's own pytest_configure):
    # non-verbose TerminalReporter.pytest_runtest_logstart prints a bare
    # "path/to/file.py " line (no letter yet) any time the replayed nodeid's
    # file differs from the last one shown -- harmless for a single serial
    # worker, but with several concurrently-replayed workers interleaving
    # different files' events, nearly every single test trips this, turning
    # what should be a plain dot stream into "orphaned path, no dot" lines
    # scattered throughout the run. Suppressing it here only silences that
    # bare path line; the dot/letter itself (written unconditionally by
    # pytest_runtest_logreport, regardless of this flag) still prints.
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.showfspath = False

    numprocesses = _resolve_numprocesses(session.config.getoption("warden_numprocesses"))
    timeout = session.config.getoption("warden_timeout")
    if timeout is None:
        ini_timeout = session.config.getini("timeout")
        timeout = float(ini_timeout) if ini_timeout else None
    if timeout is not None and timeout <= 0:
        # `if worker.timeout:` elsewhere is a truthiness check, so an
        # unvalidated 0 would be silently indistinguishable from --timeout
        # not being given at all -- the opposite of what a user typing
        # --timeout=0 almost certainly means.
        raise pytest.UsageError(f"--timeout must be a positive number, got {timeout}")
    work_stealing = session.config.getoption("warden_work_stealing")

    # Precomputed once against real Item objects (only session.items has
    # markers/collection info) so downstream batching only ever deals with
    # plain nodeid strings, same shape as _warden_test_timeouts below.
    dist_mode = session.config.getoption("warden_dist")
    group_keys = {item.nodeid: _group_key(item, dist_mode) for item in session.items}

    # Per-test override: a test marked @pytest.mark.timeout(N) gets N as its
    # own deadline budget instead of the --timeout default -- looked up by
    # _replay_event on each logstart so _poll_once measures the CURRENTLY
    # in-flight test's own budget, not a single constant for the whole worker.
    session.config._warden_test_timeouts = {
        item.nodeid: timeout
        if (marker_seconds := _marker_timeout_seconds(item)) is None
        else marker_seconds
        for item in session.items
    }

    scheduling = "work-stealing" if work_stealing else "static LPT"
    if not _is_quiet(session.config):
        _warden_write_line(
            session,
            f"warden: starting run with {numprocesses} worker(s) ({scheduling} scheduling)",
        )

    session.config._warden_total_tests = len(node_ids)
    session.config._warden_progress_count = 0
    session.config._warden_started_count = 0

    _ACTIVE_WORKERS.clear()
    history_store = HistoryStore(_history_db_path(session))
    session.config._warden_history_store = history_store
    try:
        with tempfile.TemporaryDirectory(prefix="pytest-warden-") as tmpdir:
            try:
                if work_stealing:
                    worker_count, all_workers = _run_work_stealing(
                        session,
                        tmpdir,
                        node_ids,
                        numprocesses,
                        timeout,
                        history_store,
                        group_keys.get,
                    )
                else:
                    worker_count, all_workers = _run_static_lpt(
                        session,
                        tmpdir,
                        node_ids,
                        numprocesses,
                        timeout,
                        history_store,
                        group_keys.get,
                    )
            except BaseException:
                # Covers KeyboardInterrupt too (not an Exception subclass) --
                # without this, Ctrl-C on the controller orphans every
                # still-running worker instead of killing them, since they
                # live in their own process group.
                _terminate_all(_ACTIVE_WORKERS)
                raise

            session.config._warden_worker_count = worker_count
            _combine_coverage(session, all_workers)
            _record_history(session, history_store)
    finally:
        history_store.close()


def _run_static_lpt(session, tmpdir, node_ids, numprocesses, timeout, history_store, group_key_fn):
    previously_failed = frozenset(session.config.cache.get("cache/lastfailed", {}))
    batches = _lpt_batch(node_ids, numprocesses, history_store, previously_failed, group_key_fn)

    worker_count = 0
    all_workers = []

    workers, worker_count = _run_wave(session, tmpdir, worker_count, batches, timeout)
    all_workers.extend(workers)

    unfinished = [
        (worker, [nid for nid in worker.batch if nid not in worker.started_ids])
        for worker in workers
    ]
    unfinished = [(worker, ids) for worker, ids in unfinished if ids]
    for worker, ids in unfinished:
        _warden_write_line(
            session,
            f"warden: worker {worker.worker_index} didn't finish its batch "
            f"({len(ids)} test(s) left) -- recreating a fresh worker to pick them up",
        )
    # One retry batch per stranded GROUP, not per originally-affected worker
    # -- otherwise a hang in the first test of a re-bundled retry batch would
    # strand its retry batch-mates too, with no second retry to save them
    # (see the "never reached this test" report below). Splitting by group
    # (a single nodeid, unless --warden-dist groups tests that must share a
    # process) isolates every stranded test/group on its own retry worker.
    retry_batches = [
        members for _, ids in unfinished for members in _group_node_ids(ids, group_key_fn).values()
    ]

    if retry_batches and not session.shouldfail:
        retry_workers, worker_count = _run_wave(
            session, tmpdir, worker_count, retry_batches, timeout
        )
        all_workers.extend(retry_workers)
        for worker in retry_workers:
            still_missing = [nid for nid in worker.batch if nid not in worker.started_ids]
            for nid in still_missing:
                _report_never_ran(
                    session,
                    nid,
                    "warden: worker never reached this test even after one retry",
                    worker.worker_index,
                )

    return worker_count, all_workers


def _max_concurrent_slots(numprocesses, total_tests):
    # Deliberately based on numprocesses/total tests, NOT on the initial
    # chunk count -- a large --warden-chunk-size can mean few chunks up
    # front, but a crash later can requeue smaller remainder chunks back
    # into the queue, and the concurrency cap must not have been
    # permanently pinned to that smaller initial count in the meantime.
    return max(1, min(numprocesses, total_tests))


def _run_work_stealing(
    session, tmpdir, node_ids, numprocesses, timeout, history_store, group_key_fn
):
    chunk_size = session.config.getoption("warden_chunk_size")
    if chunk_size is None:
        chunk_size = _default_chunk_size(len(node_ids), numprocesses)
    elif chunk_size <= 0:
        # A falsy-coalescing `getoption(...) or default()` would silently
        # replace an explicit 0 with the default, and a negative value
        # makes range()'s step negative, so _chunk_queue silently returns an
        # empty queue -- the whole run then "succeeds" having run nothing at
        # all. Both are worse than a loud, immediate error.
        raise pytest.UsageError(f"--warden-chunk-size must be a positive integer, got {chunk_size}")
    previously_failed = frozenset(session.config.cache.get("cache/lastfailed", {}))
    # Each queue entry is (batch, is_retry) -- is_retry MUST travel with the
    # batch through the queue, not just live on the _Worker that ran it, or
    # a chunk that keeps crashing would get re-inserted as a fresh (untagged)
    # entry every time and retried forever instead of stopping after one.
    queue = [
        (chunk, False)
        for chunk in _chunk_queue(
            node_ids, history_store, chunk_size, previously_failed, group_key_fn
        )
    ]
    n = _max_concurrent_slots(numprocesses, len(node_ids))

    worker_count = 0
    all_workers = []
    active = []
    for _ in range(n):
        if not queue:
            break
        batch, is_retry = queue.pop(0)
        worker_count, worker = _spawn_chunk_worker(
            session, tmpdir, worker_count, batch, timeout, is_retry
        )
        all_workers.append(worker)
        active.append(worker)

    while active:
        still_running = _poll_once(session, active)
        finished = [w for w in active if w not in still_running]

        for worker in finished:
            worker.job.close()
            remainder = [nid for nid in worker.batch if nid not in worker.started_ids]
            if remainder and not worker.is_retry and not session.shouldfail:
                _warden_write_line(
                    session,
                    f"warden: worker {worker.worker_index} didn't finish its batch "
                    f"({len(remainder)} test(s) left) -- recreating a fresh worker to pick them up",
                )
                # One retry chunk per stranded GROUP, not one combined chunk
                # -- otherwise a hang in the first test of the re-bundled
                # chunk would strand its retry chunk-mates too, with no
                # second retry to save them (see the "never reached this
                # test" report below). Matches the same fix in
                # _run_static_lpt's retry_batches.
                stranded_groups = list(_group_node_ids(remainder, group_key_fn).values())
                queue[0:0] = [(members, True) for members in stranded_groups]
            elif remainder:
                for nid in remainder:
                    _report_never_ran(
                        session,
                        nid,
                        "warden: worker never reached this test even after one retry",
                        worker.worker_index,
                    )

        active = still_running
        if not session.shouldfail:
            while queue and len(active) < n:
                batch, is_retry = queue.pop(0)
                worker_count, worker = _spawn_chunk_worker(
                    session, tmpdir, worker_count, batch, timeout, is_retry
                )
                all_workers.append(worker)
                active.append(worker)

        if session.shouldfail and active:
            # See the matching comment in _supervise: a worker whose OWN
            # test just tripped --maxfail is very likely already exiting on
            # its own, and force-killing it immediately races its
            # still-in-flight logfinish write, double-reporting the same
            # test. Give it a short, bounded chance to settle through the
            # normal "worker exited" path in _poll_once first.
            grace_deadline = time.monotonic() + _MAXFAIL_GRACE_PERIOD
            while active and time.monotonic() < grace_deadline:
                time.sleep(_POLL_INTERVAL)
                still_running = _poll_once(session, active)
                finished = [w for w in active if w not in still_running]
                for worker in finished:
                    worker.job.close()
                    remainder = [nid for nid in worker.batch if nid not in worker.started_ids]
                    for nid in remainder:
                        _report_never_ran(
                            session,
                            nid,
                            "warden: worker never reached this test even after one retry",
                            worker.worker_index,
                        )
                active = still_running

        if session.shouldfail and active:
            # Unlike the "finished" branch above, and unlike _run_wave's
            # static-mode equivalent (which unconditionally closes every
            # worker's job after _supervise returns, regardless of exit
            # reason), this path used to leave these handles open --
            # harmless on POSIX (close() is a no-op there) but a real,
            # if bounded, handle leak on Windows.
            for worker in active:
                worker.job.terminate()
            for worker in active:
                worker.proc.wait()
                worker.job.close()
                # See the matching comment in _supervise: a test in-flight
                # on a worker terminated because ANOTHER worker tripped
                # --maxfail must still be reported, not silently dropped.
                _report_incident(
                    session, worker, "warden: worker terminated because --maxfail was reached"
                )
            active = []

        if active:
            time.sleep(_POLL_INTERVAL)

    return worker_count, all_workers


def _run_wave(session, tmpdir, worker_count_start, batches, timeout):
    worker_count = worker_count_start
    workers = []
    cov_sources = _cov_sources(session)
    for batch in batches:
        worker_index = worker_count
        progress_path = os.path.join(tmpdir, f"worker-{worker_index}.jsonl")
        cov_data_file = (
            os.path.join(tmpdir, f".coverage.worker-{worker_index}") if cov_sources else None
        )
        worker_count += 1
        open(progress_path, "a", encoding="utf-8").close()
        proc, job = _spawn_worker(session, batch, progress_path, cov_data_file, tmpdir)
        workers.append(
            _Worker(proc, job, worker_index, progress_path, timeout, batch, cov_data_file)
        )

    _supervise(session, workers)

    for worker in workers:
        worker.job.close()

    return workers, worker_count


def _spawn_chunk_worker(session, tmpdir, worker_count, batch, timeout, is_retry=False):
    cov_sources = _cov_sources(session)
    progress_path = os.path.join(tmpdir, f"worker-{worker_count}.jsonl")
    cov_data_file = (
        os.path.join(tmpdir, f".coverage.worker-{worker_count}") if cov_sources else None
    )
    open(progress_path, "a", encoding="utf-8").close()
    proc, job = _spawn_worker(session, batch, progress_path, cov_data_file, tmpdir)
    worker = _Worker(
        proc, job, worker_count, progress_path, timeout, batch, cov_data_file, is_retry
    )
    return worker_count + 1, worker


def _combine_coverage(session, workers):
    if not _cov_sources(session):
        return
    data_files = [
        w.cov_data_file for w in workers if w.cov_data_file and os.path.exists(w.cov_data_file)
    ]
    if not data_files:
        return

    import coverage

    combined_path = str(session.config.rootpath / ".coverage")
    cov = coverage.Coverage(data_file=combined_path)
    cov.combine(data_files, strict=True)
    cov.save()
    session.config._warden_cov_data_file = combined_path


def _replay_line(session, worker, line):
    # The progress file is written exclusively by warden's own worker.py,
    # so a line that's syntactically complete (newline-terminated, past
    # _read_new_lines' torn-read filter) but still fails to parse indicates
    # real corruption, not a race -- rare, but must degrade gracefully
    # rather than taking down the whole controller loop over one bad
    # event. If the corruption cost us the real outcome for the worker's
    # in-flight test, the existing "worker exited unexpectedly" fallback in
    # _poll_once still produces a visible report once the worker exits, so
    # skipping here doesn't risk a silent, permanent loss.
    try:
        event = json.loads(line)
        _replay_event(session, worker, event)
    except (json.JSONDecodeError, KeyError) as exc:
        warnings.warn(
            f"warden: ignoring unparseable progress-channel line from worker "
            f"(progress file {worker.progress_path!r}): {exc!r}",
            stacklevel=2,
        )


_KILL_VERIFY_TIMEOUT = 2.0
_KILL_MAX_ATTEMPTS = 3


def _terminate_and_verify(worker):
    # worker.job.terminate() is otherwise fire-and-forget -- nothing
    # confirms the process actually died, so if the underlying kill call
    # silently has no effect (observed in practice on Windows: an ambient
    # outer Job Object's own nesting/breakaway policy can swallow
    # TerminateJobObject without raising a Python-visible error), the
    # caller falls back to passively polling proc.poll() with no bound,
    # silently degrading into "wait for natural death" instead of a fast,
    # loud failure. Bound the wait and retry a few times, warning on every
    # attempt that doesn't take, so a genuinely-stuck kill is visible in CI
    # output. A single retry (confirmed against real Windows CI runs of
    # this project's own full test suite, under the heavy concurrent
    # process churn many simultaneous worker kills create) is sometimes not
    # enough -- multiple attempts, each independently bounded, still adds
    # only low-single-digit milliseconds on the happy path, since each
    # attempt's wait loop exits on the first poll() that returns non-None.
    for attempt in range(1, _KILL_MAX_ATTEMPTS + 1):
        worker.job.terminate()
        deadline = time.monotonic() + _KILL_VERIFY_TIMEOUT
        while time.monotonic() < deadline:
            if worker.proc.poll() is not None:
                return
            time.sleep(_POLL_INTERVAL)
        if worker.proc.poll() is not None:
            return
        retrying = attempt < _KILL_MAX_ATTEMPTS
        warnings.warn(
            f"warden: worker pid {worker.proc.pid} did not exit within "
            f"{_KILL_VERIFY_TIMEOUT}s of termination attempt {attempt}/{_KILL_MAX_ATTEMPTS}"
            + (" -- retrying" if retrying else " -- giving up, falling back to a passive wait"),
            stacklevel=2,
        )


def _poll_once(session, workers):
    now = time.monotonic()
    still_pending = []
    for worker in workers:
        # Once a worker has been marked killed, its hard-kill incident has
        # already been reported and worker.current already cleared -- stop
        # reading/replaying ANY further progress-channel content from it.
        # Job Object/process-group termination isn't guaranteed instant: if
        # the underlying OS process keeps running a little longer and
        # manages to write a legitimate completion event for the SAME test
        # before it actually dies, replaying that event would silently
        # double-report the test (once via the hard-kill incident, once
        # via its own late "real" outcome) -- this guard prevents that
        # regardless of how promptly the OS actually tears the process down.
        if not worker.killed:
            new_lines = _read_new_lines(worker)
            if new_lines:
                for line in new_lines:
                    _replay_line(session, worker, line)
                # Replayed above, so worker.current/current_timeout now
                # reflect whichever test is in-flight as of the LAST line
                # just replayed -- the deadline must be measured against
                # THAT test's own budget (its @pytest.mark.timeout override,
                # or the --timeout default), not a stale one left over from
                # whatever was running before these lines arrived.
                effective_timeout = (
                    worker.current_timeout if worker.current is not None else worker.timeout
                )
                if effective_timeout:
                    worker.deadline = now + effective_timeout

        if worker.proc.poll() is not None:
            if not worker.killed:
                for line in _read_new_lines(worker):
                    _replay_line(session, worker, line)
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
            _terminate_and_verify(worker)
            _report_incident(
                session,
                worker,
                f"warden: hard-killed after exceeding {worker.current_timeout}s timeout",
            )

        still_pending.append(worker)
    return still_pending


def _supervise(session, workers):
    pending = list(workers)
    while pending:
        pending = _poll_once(session, pending)

        if session.shouldfail and pending:
            # A worker whose OWN test just tripped this exact --maxfail
            # threshold (workers run with the same --maxfail=N locally --
            # see _spawn_worker) is very likely already exiting on its own.
            # Force-killing it immediately races its still-in-flight
            # logfinish write and double-reports the same test (once via
            # the real failure, once via this incident). Give it a short,
            # bounded chance to settle through the normal "worker exited"
            # path in _poll_once first, which already drains all remaining
            # lines before deciding whether a report is still owed.
            grace_deadline = time.monotonic() + _MAXFAIL_GRACE_PERIOD
            while pending and time.monotonic() < grace_deadline:
                time.sleep(_POLL_INTERVAL)
                pending = _poll_once(session, pending)

        if session.shouldfail and pending:
            for worker in pending:
                worker.job.terminate()
            for worker in pending:
                worker.proc.wait()
                # Unlike the timeout-kill branch in _poll_once, this
                # termination is triggered by ANOTHER worker's failure
                # tripping --maxfail, not by this worker's own test hanging
                # -- without this, a test that was genuinely in-flight here
                # vanishes from the final report entirely instead of
                # showing up as failed like every other incident path does.
                _report_incident(
                    session, worker, "warden: worker terminated because --maxfail was reached"
                )
            pending = []

        if pending:
            time.sleep(_POLL_INTERVAL)


def _read_new_lines(worker):
    # Resumes from worker.byte_offset instead of re-reading the whole file
    # from byte 0 every poll. worker.byte_offset is ONLY ever assigned from
    # this file object's own fh.tell(), called right after readline()
    # returns a confirmed-complete ("\n"-terminated) line -- never from
    # manual arithmetic (offset + len(line)), which is undefined once
    # CRLF-translation/multi-byte UTF-8 is involved for a text-mode file.
    # A torn/partial write (readline() returns a string NOT ending in
    # "\n", including "" at EOF) simply breaks the loop before its content
    # is ever appended or its position committed -- identical torn-write
    # safety to the previous line.endswith("\n") filter, applied
    # incrementally. Seeking past a truncated file's new EOF is legal and
    # readline() just returns "", so no special-casing is needed there
    # either.
    complete = []
    with open(worker.progress_path, encoding="utf-8") as fh:
        if worker.byte_offset:
            fh.seek(worker.byte_offset)
        while True:
            line = fh.readline()
            if not line.endswith("\n"):
                break
            complete.append(line.strip())
            worker.byte_offset = fh.tell()
    return complete


@contextlib.contextmanager
def _suppressed_dot_write(config):
    """While active, pytest's own raw per-test dot/letter -- written
    directly to the terminal by TerminalReporter.pytest_runtest_logreport,
    with no trailing newline, whenever verbosity is 0 -- is swallowed.
    Warden always prints its own richer `[done/total] worker N -> nodeid
    WORD` line instead (see _print_progress_line below); without this, that
    raw letter visually smashes into whatever text prints right after it
    (e.g. "FFFFwarden: worker 0 didn't finish its batch...").
    """
    verbose = getattr(getattr(config, "option", None), "verbose", 0)
    reporter = getattr(config, "pluginmanager", None)
    if reporter is not None:
        reporter = reporter.get_plugin("terminalreporter")
    if verbose or reporter is None:
        yield
        return
    tw = reporter._tw
    original_write = tw.write
    tw.write = lambda *args, **kwargs: None
    try:
        yield
    finally:
        tw.write = original_write


def _is_quiet(config):
    # -q/-qq (verbose < 0): warden's own lines stay suppressed, matching
    # pytest's own quiet dot-stream convention. -v/-vv (verbose > 0) and
    # the default (verbose == 0) both show them -- unlike the old bare
    # `if verbose:` check this replaced, which (since -1 is truthy in
    # Python) accidentally suppressed -q *and* -v alike, the opposite of
    # -v's own actual goal (worker attribution that pytest's own verbose
    # reporter doesn't carry). getattr-safe: test_progress_channel.py
    # drives some of these call sites against a bare stand-in `config`
    # with no `.option` at all.
    return getattr(getattr(config, "option", None), "verbose", 0) < 0


def _print_progress_line(session, config, worker_index, nodeid, word):
    # Shown by default and under -v/-vv; suppressed under -q/-qq (see
    # _is_quiet). This is warden's own worker-attributed line, distinct
    # from whatever pytest's own terminal reporter prints under -v (which
    # carries no worker index at all).
    if _is_quiet(config):
        return
    total = getattr(config, "_warden_total_tests", None)
    if total is None:
        return
    config._warden_progress_count = getattr(config, "_warden_progress_count", 0) + 1
    _warden_write_line(
        session,
        f"[{config._warden_progress_count}/{total}] worker {worker_index} -> {nodeid} {word}",
    )


def _print_started_line(session, config, worker_index, nodeid):
    # Mirrors _print_progress_line, but fires at logstart instead of
    # logfinish -- without this, nothing prints while a test is actually
    # running; the worker-attributed line only ever appeared once the test
    # was already done. Uses its own counter (_warden_started_count) rather
    # than sharing _warden_progress_count, so a test's STARTED line and its
    # later PASSED/FAILED line each get counted once, instead of one test
    # advancing the shared [n/total] fraction twice. Suppressed under
    # -q/-qq, same as _print_progress_line (see _is_quiet).
    if _is_quiet(config):
        return
    total = getattr(config, "_warden_total_tests", None)
    if total is None:
        return
    config._warden_started_count = getattr(config, "_warden_started_count", 0) + 1
    _warden_write_line(
        session,
        f"[{config._warden_started_count}/{total}] worker {worker_index} -> {nodeid} STARTED",
    )


def _replay_event(session, worker, event):
    hook = session.config.hook
    config = session.config
    kind = event["kind"]
    if kind == "logstart":
        worker.started_ids.add(event["nodeid"])
        worker.current = {"nodeid": event["nodeid"], "location": event["location"]}
        # getattr-safe: test_progress_channel.py drives this against a bare
        # stand-in config with no _warden_test_timeouts at all (real
        # controller runs always set it in _run_controller before any
        # worker is spawned).
        test_timeouts = getattr(config, "_warden_test_timeouts", None) or {}
        worker.current_timeout = test_timeouts.get(event["nodeid"], worker.timeout)
        _print_started_line(session, config, worker.worker_index, event["nodeid"])
        hook.pytest_runtest_logstart(nodeid=event["nodeid"], location=tuple(event["location"]))
    elif kind == "logreport":
        report = hook.pytest_report_from_serializable(config=config, data=event["data"])
        # A raw skip/xfail longrepr (no traceback -- just a bare
        # (path, lineno, reason) tuple, per _pytest.reports._to_json) is
        # left untouched at serialize time on the assumption that whatever
        # deserializes it preserves Python types. execnet (pytest-xdist's
        # channel) does; plain `json.dumps`/`json.loads` (worker.py's
        # progress channel) doesn't -- JSON has no tuple type, so it comes
        # back as a list. That silently fails pytest's own
        # `assert isinstance(report.longrepr, tuple)` in verbose-mode
        # skip-reason reporting (_pytest/terminal.py's
        # _get_raw_skip_reason) with an INTERNALERROR that aborts the
        # whole run. A list here is unambiguously a round-tripped tuple --
        # every other longrepr shape (None, a plain string, or a real
        # ExceptionChainRepr/ReprExceptionInfo) survives the round-trip as
        # its original type, never as a bare list.
        if isinstance(report.longrepr, list):
            report.longrepr = tuple(report.longrepr)
        # Captured before quarantine may rewrite report.outcome (failed ->
        # skipped/xfail) -- history must record what actually happened, not
        # what the report was rewritten to show, or a persistently-flaky
        # quarantined test's "failed" history entries get silently replaced
        # by "skipped" ones over time until _is_flaky no longer sees them.
        report._warden_original_outcome = report.outcome
        report = _maybe_quarantine(session, report)

        # TerminalReporter.pytest_runtest_logreport would otherwise write a
        # bare dot/letter (and occasionally the "[ X%]" progress marker) for
        # this same report -- pure duplication of our own [done/total] ...
        # RESULT line at logfinish below, which already conveys the same
        # outcome with an exact count instead of a rounded percentage.
        # `_add_stats` -- what the final short summary/FAILURES section
        # actually needs -- runs earlier in the same method, unguarded by
        # this write, so it's unaffected.
        with _suppressed_dot_write(config):
            hook.pytest_runtest_logreport(report=report)
        # Captures the human-readable word (PASSED/FAILED/SKIPPED/XFAIL/...)
        # for the progress line printed at logfinish below, once the
        # outcome is actually known -- reuses pytest's own status hook
        # rather than reinventing outcome -> word (report.outcome alone is
        # just "passed"/"failed"/"skipped", missing the xfail/xpass
        # distinction pytest_report_teststatus already knows how to make).
        # Passing setup/teardown phases report an empty word ("", "", "")
        # and must not blank out an already-known "PASSED" from the call
        # phase, so only a truthy word overwrites.
        if worker.current is not None:
            _category, _letter, word = hook.pytest_report_teststatus(report=report, config=config)
            if isinstance(word, tuple):
                word = word[0]
            if word:
                worker.current["outcome_word"] = word
    elif kind == "logfinish":
        current = worker.current
        worker.current = None
        # Without -v, pytest shows nothing test-identifying at all (just a
        # dot/letter, itself suppressed above) -- this is the only place
        # the test, its worker, and its outcome show up together.
        if current is not None:
            word = current.get("outcome_word", "?")
            _print_progress_line(session, config, worker.worker_index, event["nodeid"], word)
        hook.pytest_runtest_logfinish(nodeid=event["nodeid"], location=tuple(event["location"]))


def _report_incident(session, worker, message):
    current = worker.current
    if current is None:
        return
    config = session.config
    hook = config.hook
    location = tuple(current["location"])
    report = TestReport(
        nodeid=current["nodeid"],
        location=location,
        keywords={},
        outcome="failed",
        longrepr=message,
        when="call",
        sections=[],
        duration=worker.current_timeout or 0,
        user_properties=[],
    )
    with _suppressed_dot_write(config):
        hook.pytest_runtest_logreport(report=report)
    _print_progress_line(session, config, worker.worker_index, current["nodeid"], "FAILED")
    hook.pytest_runtest_logfinish(nodeid=current["nodeid"], location=location)
    worker.current = None


def _report_never_ran(session, nodeid, message, worker_index=None):
    config = session.config
    hook = config.hook
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
    with _suppressed_dot_write(config):
        hook.pytest_runtest_logreport(report=report)
    if worker_index is not None:
        _print_progress_line(session, config, worker_index, nodeid, "FAILED")
    hook.pytest_runtest_logfinish(nodeid=nodeid, location=location)


def pytest_terminal_summary(terminalreporter, config):
    if _is_quiet(config):
        return
    count = getattr(config, "_warden_worker_count", None)
    if count is not None:
        terminalreporter.write_line(f"warden: distributed across {count} worker(s)")
