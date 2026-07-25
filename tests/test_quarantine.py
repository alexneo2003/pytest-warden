def test_historically_flaky_failure_is_quarantined_not_counted_as_a_build_failure(
    pytester,
):
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
            assert n % 2 == 1  # passes on odd invocations, fails on even
        """
    )
    history_db = str(pytester.path / "history.sqlite3")

    r1 = pytester.runpytest("--warden", f"--warden-history-db={history_db}")
    r1.assert_outcomes(passed=1)

    r2 = pytester.runpytest("--warden", f"--warden-history-db={history_db}")
    r2.assert_outcomes(failed=1)  # no quarantine flag yet -- counts as a real failure

    r3 = pytester.runpytest("--warden", f"--warden-history-db={history_db}")
    r3.assert_outcomes(passed=1)

    r4 = pytester.runpytest(
        "--warden", f"--warden-history-db={history_db}", "--warden-quarantine-flaky"
    )
    assert r4.ret == 0
    r4.stdout.fnmatch_lines(["*xfail*"])


def test_consistently_failing_test_is_never_quarantined(pytester):
    pytester.makepyfile(
        """
        def test_always_fails():
            assert False
        """
    )
    history_db = str(pytester.path / "history.sqlite3")

    for _ in range(3):
        result = pytester.runpytest(
            "--warden", f"--warden-history-db={history_db}", "--warden-quarantine-flaky"
        )
        result.assert_outcomes(failed=1)
        assert result.ret == 1
