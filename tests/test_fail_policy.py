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
