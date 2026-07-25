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
