def test_last_failed_reruns_only_the_previously_failed_test(pytester):
    pytester.makepyfile(
        test_mod="""
        def test_ok():
            assert True

        def test_bad():
            assert False
        """
    )
    first = pytester.runpytest("--warden")
    first.assert_outcomes(passed=1, failed=1)

    second = pytester.runpytest("--warden", "--last-failed")
    second.assert_outcomes(failed=1)
    second.stdout.fnmatch_lines(["collected 1 item"])


def test_failed_first_still_runs_everything(pytester):
    pytester.makepyfile(
        test_mod="""
        def test_a():
            assert True

        def test_b():
            assert False

        def test_c():
            assert True
        """
    )
    first = pytester.runpytest("--warden")
    first.assert_outcomes(passed=2, failed=1)

    second = pytester.runpytest("--warden", "--failed-first")
    second.assert_outcomes(passed=2, failed=1)
