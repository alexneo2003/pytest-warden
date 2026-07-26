def test_crashed_worker_requeues_the_never_started_remainder_onto_a_fresh_worker(
    pytester,
):
    pytester.makepyfile(
        """
        import os

        def test_a():
            assert True

        def test_b():
            os._exit(70)  # simulate an abrupt worker crash mid-test

        def test_c():
            assert True
        """
    )
    result = pytester.runpytest("--warden", "--numprocesses=1")
    # test_a already passed in the first attempt; test_b was in-flight when
    # the worker died, so per plan semantics it's marked failed directly (the
    # crashing test itself is never retried); test_c never even started in
    # the first attempt, so it's the "remainder" that gets exactly one
    # fresh-worker retry, and passes there.
    result.assert_outcomes(passed=2, failed=1)


def test_repeatedly_crashing_test_is_marked_failed_after_one_retry_not_requeued_forever(
    pytester,
):
    pytester.makepyfile(
        """
        import os

        def test_a():
            assert True

        def test_b():
            os._exit(70)  # always crashes -- must not loop forever

        def test_c():
            assert True
        """
    )
    result = pytester.runpytest("--warden", "--numprocesses=1")
    # test_a passes on the first attempt; test_b crashes every time so it's
    # marked failed after exactly one retry (not requeued again); test_c
    # never got to run in the first attempt (killed process before reaching
    # it) but does succeed on the single retry.
    result.assert_outcomes(passed=2, failed=1)


def test_timeout_killed_test_remainder_gets_exactly_one_retry_like_a_crash_does(pytester):
    # Same "never-started remainder gets exactly one retry" guarantee as the
    # crash-based tests above, but triggered by a --timeout hard-kill rather
    # than os._exit -- proving the retry logic is uniform regardless of WHY
    # the worker died.
    pytester.makepyfile(
        """
        import time

        def test_a():
            assert True

        def test_hangs():
            time.sleep(30)

        def test_c():
            assert True
        """
    )
    result = pytester.runpytest("--warden", "--numprocesses=1", "--timeout=1")
    result.assert_outcomes(passed=2, failed=1)
    result.stdout.fnmatch_lines(["*hard-killed*timeout*"])


def test_no_duplicate_report_for_a_test_that_times_out_again_during_its_own_retry(pytester):
    # The retried remainder test itself hangs again -- it must be reported
    # exactly once (via the timeout path on the retry worker), not twice,
    # and not additionally reported as "never ran".
    pytester.makepyfile(
        """
        import time

        def test_a():
            assert True

        def test_hangs_1():
            time.sleep(30)

        def test_hangs_2():
            time.sleep(30)
        """
    )
    result = pytester.runpytest("--warden", "--numprocesses=1", "--timeout=1")
    # assert_outcomes(passed=1, failed=2) is itself the "no duplicate
    # report, no lost report" proof, reflecting pytest's own report-stats
    # accounting directly -- a duplicate report for either hung test would
    # show up as failed=3 here. (A prior version additionally counted
    # occurrences of "hard-killed" in raw stdout, which depends on the
    # invoking environment's terminal width via pytest's short-summary-line
    # truncation -- not a reliable signal; see test_timeout.py's
    # test_no_duplicate_report_for_a_hard_killed_test for the same fix.)
    result.assert_outcomes(passed=1, failed=2)
    stdout = result.stdout.str()
    assert "never reached this test" not in stdout, (
        "test_hangs_2 started on its retry worker -- it must be reported via "
        "the timeout path, not the never-ran path"
    )
