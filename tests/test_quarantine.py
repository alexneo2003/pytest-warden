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


def test_quarantine_does_not_suppress_an_unrelated_tests_real_failure_in_the_same_run(pytester):
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

        def test_genuinely_broken():
            assert False  # never passes -- must never be quarantined
        """
    )
    history_db = str(pytester.path / "history.sqlite3")

    # Build up flaky history for test_flaky only.
    pytester.runpytest("--warden", f"--warden-history-db={history_db}", "-k", "test_flaky")
    pytester.runpytest("--warden", f"--warden-history-db={history_db}", "-k", "test_flaky")
    pytester.runpytest("--warden", f"--warden-history-db={history_db}", "-k", "test_flaky")

    result = pytester.runpytest(
        "--warden", f"--warden-history-db={history_db}", "--warden-quarantine-flaky"
    )
    assert result.ret != 0, "the unrelated genuinely-broken test must still fail the build"
    result.stdout.fnmatch_lines(["*test_genuinely_broken*"])


def test_setup_phase_failure_is_never_quarantined_even_with_flaky_call_phase_history(pytester):
    pytester.makepyfile(
        test_mod="""
        import os

        import pytest

        @pytest.fixture
        def maybe_broken_fixture():
            if os.path.exists("break_setup.txt"):
                raise RuntimeError("setup blew up")
            yield

        def test_it(maybe_broken_fixture):
            counter_file = "invocations.txt"
            n = 0
            if os.path.exists(counter_file):
                n = int(open(counter_file).read())
            n += 1
            open(counter_file, "w").write(str(n))
            assert n % 2 == 1  # flaky at the CALL phase across runs
        """
    )
    history_db = str(pytester.path / "history.sqlite3")
    pytester.runpytest("--warden", f"--warden-history-db={history_db}")
    pytester.runpytest("--warden", f"--warden-history-db={history_db}")
    pytester.runpytest("--warden", f"--warden-history-db={history_db}")

    (pytester.path / "break_setup.txt").write_text("x")
    result = pytester.runpytest(
        "--warden", f"--warden-history-db={history_db}", "--warden-quarantine-flaky"
    )
    # A setup-phase error must be reported as a real error, never rewritten
    # to xfail/skipped just because the same test's CALL phase has flaky
    # history.
    result.assert_outcomes(errors=1)
    assert result.ret != 0


def test_is_flaky_returns_false_on_an_empty_history_store(tmp_path):
    from pytest_warden.history import HistoryStore
    from pytest_warden.plugin import _is_flaky

    store = HistoryStore(str(tmp_path / "history.sqlite3"))
    try:
        assert _is_flaky(store, "test_mod.py::test_anything") is False
    finally:
        store.close()
