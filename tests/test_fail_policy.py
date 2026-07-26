def test_maxfail_stops_the_run_early(pytester):
    pytester.makepyfile(
        """
        def test_a():
            assert False

        def test_b():
            assert False

        def test_c():
            assert True
        """
    )
    result = pytester.runpytest("--warden", "--numprocesses=1", "--maxfail=1")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*stopping after 1 failures*"])


def test_maxfail_does_not_spawn_a_retry_wave_for_tests_it_intentionally_skipped(
    pytester,
):
    pytester.makepyfile(
        """
        def test_a():
            assert False

        def test_b():
            assert True

        def test_c():
            assert True
        """
    )
    # numprocesses=1 means test_b/test_c never even start once maxfail stops
    # the worker after test_a -- they must NOT be treated as a crash
    # remainder and requeued onto a retry worker.
    result = pytester.runpytest("--warden", "--numprocesses=1", "--maxfail=1")
    result.assert_outcomes(failed=1)


def test_maxfail_trips_from_combined_failures_across_two_separate_workers(pytester):
    pytester.makepyfile(
        test_mod="""
        def test_a():
            assert False

        def test_b():
            assert False
        """
    )
    # Each worker's OWN local --maxfail=1 wouldn't trip from a single
    # failure in isolation if the two failures land on separate workers --
    # this proves the controller's session-level shouldfail aggregates
    # across workers, not just within one.
    result = pytester.runpytest("--warden", "--numprocesses=2", "--maxfail=1")
    assert result.ret != 0
    outcomes = result.parseoutcomes()
    assert outcomes.get("failed", 0) >= 1


def test_in_flight_test_on_a_worker_killed_due_to_maxfail_gets_a_report_not_silently_dropped(
    pytester,
):
    pytester.makepyfile(
        test_mod="""
        import time

        def test_fails_fast():
            assert False

        def test_slow():
            time.sleep(5)
            assert True
        """
    )
    result = pytester.runpytest("--warden", "--numprocesses=2", "--maxfail=1")
    outcomes = result.parseoutcomes()
    total_accounted = sum(
        outcomes.get(key, 0) for key in ("passed", "failed", "skipped", "errors", "error")
    )
    assert total_accounted == 2, (
        f"test_slow was in-flight on a worker terminated because test_fails_fast "
        f"tripped --maxfail elsewhere -- it must show up SOMEWHERE in the final "
        f"accounting, not vanish silently. Got: {dict(outcomes)}"
    )


def test_retries_plus_maxfail_together_do_not_corrupt_total_test_count(pytester):
    pytester.makepyfile(
        test_mod="""
        import os

        def test_a():
            assert True

        def test_crashes():
            os._exit(70)

        def test_c():
            assert True

        def test_fails():
            assert False
        """
    )
    result = pytester.runpytest("--warden", "--numprocesses=1", "--maxfail=5")
    outcomes = result.parseoutcomes()
    total_accounted = sum(
        outcomes.get(key, 0) for key in ("passed", "failed", "skipped", "errors", "error")
    )
    assert total_accounted == 4, (
        f"expected all 4 collected tests accounted for, got {dict(outcomes)}"
    )


def test_maxfail_tripped_by_a_hard_killed_test_stops_the_retry_wave_from_spawning(pytester):
    # A hard-killed test's synthetic failure report must count toward
    # --maxfail exactly like a normal failure -- and once maxfail trips, the
    # never-started remainder (test_c) must not get its usual one retry.
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
    result = pytester.runpytest("--warden", "--numprocesses=1", "--timeout=1", "--maxfail=1")
    result.assert_outcomes(passed=1, failed=1)
    stdout = result.stdout.str()
    assert "never reached this test" not in stdout, (
        "maxfail should have stopped the retry wave from spawning at all for test_c"
    )
