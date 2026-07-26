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
