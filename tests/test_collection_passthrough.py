def test_k_selection_passes_through_untouched(pytester):
    pytester.makepyfile(
        """
        def test_alpha():
            assert True

        def test_beta():
            assert True
        """
    )
    result = pytester.runpytest("--warden", "-k", "alpha")
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["*1 deselected*"])


def test_no_tests_collected_behaves_like_bare_pytest(pytester):
    pytester.makepyfile(
        """
        # no tests here
        """
    )
    result = pytester.runpytest("--warden")
    assert result.ret == 5  # pytest's own "no tests collected" exit code
