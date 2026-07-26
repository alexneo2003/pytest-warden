from pytest_warden.history import HistoryStore
from pytest_warden.plugin import _max_concurrent_slots


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
