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
