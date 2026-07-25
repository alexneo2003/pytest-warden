def test_single_passing_test_is_actually_run_and_reported_passed(pytester):
    pytester.makepyfile(
        """
        def test_ok():
            assert True
        """
    )
    result = pytester.runpytest("--warden")
    result.assert_outcomes(passed=1)


def test_bare_run_without_warden_flag_is_unaffected(pytester):
    pytester.makepyfile(
        """
        def test_ok():
            assert True
        def test_bad():
            assert False
        """
    )
    result = pytester.runpytest()
    result.assert_outcomes(passed=1, failed=1)


def test_single_failing_test_is_reported_failed_with_real_traceback(pytester):
    pytester.makepyfile(
        """
        def test_bad():
            assert 1 == 2
        """
    )
    result = pytester.runpytest("--warden")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*assert 1 == 2*"])


def test_exit_code_matches_bare_pytest_on_failure(pytester):
    pytester.makepyfile(
        """
        def test_bad():
            assert False
        """
    )
    result = pytester.runpytest("--warden")
    assert result.ret == 1


def test_exit_code_matches_bare_pytest_on_success(pytester):
    pytester.makepyfile(
        """
        def test_ok():
            assert True
        """
    )
    result = pytester.runpytest("--warden")
    assert result.ret == 0
