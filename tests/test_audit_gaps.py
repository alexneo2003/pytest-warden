import os
import signal
import subprocess
import sys
import time

import pytest

from pytest_warden.history import HistoryStore
from pytest_warden.plugin import _max_concurrent_slots


def test_sigint_terminates_the_running_worker_instead_of_orphaning_it(pytester):
    # Workers run in their own process group (start_new_session=True) so a
    # hard-kill of one worker can never touch the controller or its
    # siblings -- but that same isolation means Ctrl-C on the controller
    # doesn't propagate to a worker either, unless the controller
    # explicitly terminates its own tracked workers on interrupt. This
    # needs a REAL separate OS process to receive a REAL signal --
    # pytester's in-process runpytest can't exercise this at all.
    pytester.makepyfile(
        test_mod="""
        import os
        import time

        def test_hangs():
            with open("worker.pid", "w") as fh:
                fh.write(str(os.getpid()))
            time.sleep(30)
        """
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "pytest", "--warden", "--numprocesses=1"],
        cwd=str(pytester.path),
    )
    try:
        pid_file = pytester.path / "worker.pid"
        deadline = time.monotonic() + 10
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert pid_file.exists(), "worker never started"
        worker_pid = int(pid_file.read_text().strip())

        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=10)

        time.sleep(0.3)
        with pytest.raises(ProcessLookupError):
            os.kill(worker_pid, 0)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_max_concurrent_slots_uses_numprocesses_not_initial_chunk_count():
    # A large --warden-chunk-size can mean few initial chunks, but the
    # concurrency cap should still track numprocesses/total tests, not the
    # (possibly much smaller) initial chunk count -- otherwise, if a crash
    # later requeues more, smaller chunks back into the queue, there's no
    # room to run them concurrently even though numprocesses allows it.
    assert _max_concurrent_slots(numprocesses=4, total_tests=8) == 4
    assert _max_concurrent_slots(numprocesses=4, total_tests=2) == 2  # never more than total tests
    assert _max_concurrent_slots(numprocesses=0, total_tests=8) == 1  # clamped to at least 1


def test_quarantining_a_failure_still_records_it_as_failed_in_history(pytester):
    # If quarantine's outcome-rewrite (failed -> skipped/xfail) leaks into
    # what gets recorded to history, a persistently-flaky-but-quarantined
    # test's "failed" outcomes get silently replaced by "skipped" ones over
    # time -- and since _is_flaky requires an actual "failed" entry in the
    # window, the test eventually (and wrongly) stops being detected as
    # flaky at all, purely as a side effect of having been quarantined.
    pytester.makepyfile(
        test_mod="""
        import os

        def test_flaky():
            counter_file = "invocations.txt"
            n = 0
            if os.path.exists(counter_file):
                n = int(open(counter_file).read())
            n += 1
            open(counter_file, "w").write(str(n))
            assert n % 2 == 1
        """
    )
    history_db = str(pytester.path / "history.sqlite3")

    pytester.runpytest("--warden", f"--warden-history-db={history_db}").assert_outcomes(
        passed=1
    )
    pytester.runpytest("--warden", f"--warden-history-db={history_db}").assert_outcomes(
        failed=1
    )
    pytester.runpytest("--warden", f"--warden-history-db={history_db}").assert_outcomes(
        passed=1
    )

    # Run 4 fails again, but this time quarantine is on -- reported as
    # xfail, build stays green. The bug: does history record this as
    # "skipped" (what the report became) or "failed" (what actually
    # happened)?
    result = pytester.runpytest(
        "--warden", f"--warden-history-db={history_db}", "--warden-quarantine-flaky"
    )
    assert result.ret == 0

    store = HistoryStore(history_db)
    try:
        outcomes = store.get_outcomes("test_mod.py::test_flaky", window=1)
    finally:
        store.close()
    assert outcomes[0]["outcome"] == "failed", (
        f"expected the quarantined run's real outcome (failed) to be recorded, got: {outcomes[0]}"
    )


def test_coverage_combine_does_not_crash_when_a_worker_is_hard_killed(pytester):
    # coverage.py only flushes its data file at clean process exit; a Job
    # Object hard-kill skips that entirely (equivalent to SIGKILL, no atexit
    # handlers run), so a killed worker's coverage data file can be missing
    # or incomplete. _combine_coverage must tolerate that rather than
    # raising out of an otherwise-successful run.
    pytester.makepyfile(
        mod="def add(a, b):\n    return a + b\n",
    )
    pytester.makepyfile(
        test_mod="""
        import time
        from mod import add

        def test_a():
            assert add(1, 2) == 3

        def test_hangs():
            add(1, 2)
            time.sleep(30)
        """
    )
    result = pytester.runpytest(
        "--warden", "--numprocesses=1", "--timeout=1", "--cov=mod"
    )
    result.assert_outcomes(passed=1, failed=1)
    # The point: this must not raise (e.g. coverage.CoverageException) and
    # abort the whole run just because one worker's data was incomplete.
    assert (pytester.path / ".coverage").exists()


def test_coverage_loss_from_a_hard_kill_is_scoped_to_that_workers_batch(pytester):
    # Documents a real, known trade-off: killing a worker loses ALL of that
    # worker's buffered-but-unflushed coverage data, not just the killed
    # test's -- so a test that already passed can appear uncovered purely
    # because it shared a batch with one that later got killed. Putting
    # test_a on ITS OWN worker (numprocesses=2) avoids this; the paired
    # single-worker case is covered by the crash-safety test above.
    pytester.makepyfile(
        mod="def add(a, b):\n    return a + b\n",
    )
    pytester.makepyfile(
        test_mod="""
        import time
        from mod import add

        def test_a():
            assert add(1, 2) == 3

        def test_hangs():
            add(1, 2)
            time.sleep(30)
        """
    )
    result = pytester.runpytest(
        "--warden", "--numprocesses=2", "--timeout=1", "--cov=mod"
    )
    result.assert_outcomes(passed=1, failed=1)

    import coverage

    cov = coverage.Coverage(data_file=str(pytester.path / ".coverage"))
    cov.load()
    _, statements, _, missing, _ = cov.analysis2(str(pytester.path / "mod.py"))
    assert statements
    assert missing == [], (
        "test_a's own worker should have flushed real coverage for mod.py "
        f"even though test_hangs' worker was killed, missing: {missing}"
    )


def test_worker_that_never_starts_a_single_test_gets_its_whole_batch_retried_once(pytester):
    # A worker can fail before running ANY test at all -- e.g. a broken
    # conftest.py causing a collection error inside the worker specifically
    # (not the controller, which already collected successfully to get
    # here). Distinguishing marker: only worker invocations carry
    # --warden-progress-file, so this conftest only misbehaves there.
    pytester.makepyfile(
        conftest="""
        def pytest_configure(config):
            if config.getoption("warden_progress_file", None):
                import os
                os._exit(70)
        """,
    )
    pytester.makepyfile(
        test_mod="""
        def test_a():
            assert True

        def test_b():
            assert True
        """
    )
    result = pytester.runpytest("--warden", "--numprocesses=1")
    result.assert_outcomes(failed=2)
    result.stdout.fnmatch_lines(["*never reached this test even after one retry*"])


def test_zero_timeout_is_rejected_instead_of_silently_meaning_unlimited(pytester):
    # `if worker.timeout:` is a truthiness check, so --timeout=0 is
    # currently indistinguishable from --timeout not being given at all --
    # a user typing --timeout=0 almost certainly means "fail immediately",
    # not "disable the hard-kill guarantee entirely" (the opposite intent).
    pytester.makepyfile(
        """
        def test_ok():
            assert True
        """
    )
    result = pytester.runpytest("--warden", "--timeout=0")
    assert result.ret != 0
    result.stderr.fnmatch_lines(["*--timeout must be a positive number*"])


def test_negative_timeout_is_rejected(pytester):
    pytester.makepyfile(
        """
        def test_ok():
            assert True
        """
    )
    result = pytester.runpytest("--warden", "--timeout=-1")
    assert result.ret != 0
    result.stderr.fnmatch_lines(["*--timeout must be a positive number*"])


def test_negative_chunk_size_is_rejected_instead_of_silently_running_nothing(pytester):
    pytester.makepyfile(
        """
        def test_a():
            assert True

        def test_b():
            assert True
        """
    )
    result = pytester.runpytest(
        "--warden", "--warden-work-stealing", "--warden-chunk-size=-1"
    )
    assert result.ret != 0
    result.stderr.fnmatch_lines(["*--warden-chunk-size must be a positive integer*"])


def test_zero_chunk_size_is_rejected_instead_of_silently_substituting_the_default(pytester):
    pytester.makepyfile(
        """
        def test_a():
            assert True
        """
    )
    result = pytester.runpytest(
        "--warden", "--warden-work-stealing", "--warden-chunk-size=0"
    )
    assert result.ret != 0
    result.stderr.fnmatch_lines(["*--warden-chunk-size must be a positive integer*"])


# --- Round 2: feature parity between the two scheduling modes ---
#
# --warden-work-stealing is a newer, separate code path from the default
# static-LPT one (_run_work_stealing vs _run_static_lpt/_run_wave). Several
# cross-cutting features were built and tested against the static path
# only; this section checks whether they actually carry over.


def test_work_stealing_also_prioritizes_previously_failed_tests_for_failed_first(pytester):
    # _lpt_batch (static mode) was fixed to prioritize previously-failed
    # node ids (see test_rerun_workflows.py) -- but _run_work_stealing
    # calls _chunk_queue, a completely separate function that never
    # learned about previously_failed at all. Same order-recording
    # technique as the static-mode version: -q mode never prints a
    # *passing* test's name, so scraping stdout would be a false-positive
    # waiting to happen.
    pytester.makepyfile(
        test_mod="""
        import time

        def _record(name):
            with open("order.txt", "a") as fh:
                fh.write(name + "\\n")

        def test_a():
            time.sleep(0.2)  # recorded as slower -- would sort first by weight alone
            _record("test_a")

        def test_b():
            _record("test_b")
            assert False  # previously failed, but faster than test_a
        """
    )
    first = pytester.runpytest("--warden", "--numprocesses=1")
    first.assert_outcomes(passed=1, failed=1)

    (pytester.path / "order.txt").unlink()

    second = pytester.runpytest(
        "--warden",
        "--numprocesses=1",
        "--failed-first",
        "--warden-work-stealing",
        "--warden-chunk-size=2",
    )
    order = (pytester.path / "order.txt").read_text().splitlines()
    assert order[0] == "test_b", (
        f"expected test_b (failed-first) to run before test_a under "
        f"work-stealing too, got: {order}"
    )


def test_maxfail_stops_work_stealing_early(pytester):
    # _run_work_stealing has its own, separate shouldfail-handling branch
    # from _supervise's (static mode) -- never exercised by any existing
    # test.
    pytester.makepyfile(
        test_mod="""
        def test_a():
            assert False

        def test_b():
            assert False

        def test_c():
            assert True
        """
    )
    result = pytester.runpytest(
        "--warden",
        "--numprocesses=1",
        "--maxfail=1",
        "--warden-work-stealing",
        "--warden-chunk-size=1",
    )
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*stopping after 1 failures*"])


def test_quarantine_works_under_work_stealing(pytester):
    # _maybe_quarantine is only reached via _replay_event, which IS shared
    # by both scheduling modes through _poll_once -- so this should already
    # work, but the combination was never actually exercised by a test.
    pytester.makepyfile(
        test_mod="""
        import os

        def test_flaky():
            counter_file = "invocations.txt"
            n = 0
            if os.path.exists(counter_file):
                n = int(open(counter_file).read())
            n += 1
            open(counter_file, "w").write(str(n))
            assert n % 2 == 1
        """
    )
    history_db = str(pytester.path / "history.sqlite3")

    pytester.runpytest(
        "--warden", f"--warden-history-db={history_db}", "--warden-work-stealing"
    ).assert_outcomes(passed=1)
    pytester.runpytest(
        "--warden", f"--warden-history-db={history_db}", "--warden-work-stealing"
    ).assert_outcomes(failed=1)
    pytester.runpytest(
        "--warden", f"--warden-history-db={history_db}", "--warden-work-stealing"
    ).assert_outcomes(passed=1)

    result = pytester.runpytest(
        "--warden",
        f"--warden-history-db={history_db}",
        "--warden-work-stealing",
        "--warden-quarantine-flaky",
    )
    assert result.ret == 0
    result.stdout.fnmatch_lines(["*xfail*"])


def test_sigint_terminates_running_workers_under_work_stealing_too(pytester):
    pytester.makepyfile(
        test_mod="""
        import os
        import time

        def test_hangs():
            with open("worker.pid", "w") as fh:
                fh.write(str(os.getpid()))
            time.sleep(30)
        """
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "pytest",
            "--warden",
            "--numprocesses=1",
            "--warden-work-stealing",
        ],
        cwd=str(pytester.path),
    )
    try:
        pid_file = pytester.path / "worker.pid"
        deadline = time.monotonic() + 10
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert pid_file.exists(), "worker never started"
        worker_pid = int(pid_file.read_text().strip())

        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=10)

        time.sleep(0.3)
        with pytest.raises(ProcessLookupError):
            os.kill(worker_pid, 0)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
