import coverage


def test_coverage_combines_across_workers(pytester):
    pytester.makepyfile(mod="def add(a, b):\n    return a + b\n")
    pytester.makepyfile(
        test_mod="""
        from mod import add

        def test_add():
            assert add(1, 2) == 3
        """
    )
    result = pytester.runpytest("--warden", "--numprocesses=1", "--cov=mod")
    result.assert_outcomes(passed=1)

    combined = pytester.path / ".coverage"
    assert combined.exists(), "expected a combined .coverage data file after the run"

    cov = coverage.Coverage(data_file=str(combined))
    cov.load()
    _, statements, _, missing, _ = cov.analysis2(str(pytester.path / "mod.py"))
    assert statements, "expected some statements to have been tracked at all"
    assert missing == [], f"expected full coverage of mod.py, missing lines: {missing}"


def test_coverage_is_untouched_when_cov_flag_not_used(pytester):
    pytester.makepyfile(
        """
        def test_ok():
            assert True
        """
    )
    result = pytester.runpytest("--warden", "--numprocesses=1")
    result.assert_outcomes(passed=1)
    assert not (pytester.path / ".coverage").exists()


def test_coverage_combines_across_two_parallel_workers(pytester):
    pytester.makepyfile(
        mod="""
        def add(a, b):
            return a + b

        def sub(a, b):
            return a - b
        """
    )
    pytester.makepyfile(
        test_mod="""
        from mod import add, sub

        def test_add():
            assert add(1, 2) == 3

        def test_sub():
            assert sub(3, 1) == 2
        """
    )
    result = pytester.runpytest("--warden", "--numprocesses=2", "--cov=mod")
    result.assert_outcomes(passed=2)

    combined = pytester.path / ".coverage"
    assert combined.exists()

    cov = coverage.Coverage(data_file=str(combined))
    cov.load()
    _, statements, _, missing, _ = cov.analysis2(str(pytester.path / "mod.py"))
    assert statements
    assert missing == [], f"expected full coverage of mod.py, missing lines: {missing}"
