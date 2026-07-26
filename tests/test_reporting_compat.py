import xml.etree.ElementTree as ET


def test_junitxml_records_a_hard_killed_test_with_a_readable_message(pytester):
    pytester.makepyfile(
        """
        import time

        def test_hangs():
            time.sleep(30)
        """
    )
    result = pytester.runpytest("--warden", "--timeout=1", "--junitxml=report.xml")
    result.assert_outcomes(failed=1)

    tree = ET.parse(pytester.path / "report.xml")
    testcases = tree.findall(".//testcase")
    assert len(testcases) == 1
    failure = testcases[0].find("failure")
    assert failure is not None
    assert "hard-killed" in failure.get("message", "") + (failure.text or "")


def test_junitxml_records_a_never_ran_remainder_test(pytester):
    # test_a passes; test_crash1 crashes immediately, leaving [test_crash2,
    # test_never] as a never-started remainder -- requeued once. On that
    # retry, test_crash2 crashes again, leaving test_never as a never-started
    # remainder of an already-retried chunk: it's reported via
    # _report_never_ran, whose synthetic location has no real line number
    # (`location = (fspath, None, nodeid)`) -- this exercises junitxml's
    # tolerance for that.
    pytester.makepyfile(
        test_mod="""
        import os

        def test_a():
            assert True

        def test_crash1():
            os._exit(70)

        def test_crash2():
            os._exit(71)

        def test_never():
            assert True
        """
    )
    result = pytester.runpytest(
        "--warden",
        "--numprocesses=1",
        "--warden-work-stealing",
        "--warden-chunk-size=4",
        "--junitxml=report.xml",
    )
    result.assert_outcomes(passed=1, failed=3)

    tree = ET.parse(pytester.path / "report.xml")
    testcases = {tc.get("name"): tc for tc in tree.findall(".//testcase")}
    assert set(testcases) == {"test_a", "test_crash1", "test_crash2", "test_never"}
    assert testcases["test_a"].find("failure") is None
    for name in ("test_crash1", "test_crash2", "test_never"):
        assert testcases[name].find("failure") is not None, f"{name} should have a failure element"


def test_xfail_strict_survives_the_worker_round_trip(pytester):
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.xfail(strict=True, reason="expected to fail")
        def test_unexpectedly_passes():
            assert True

        @pytest.mark.xfail(strict=True, reason="expected to fail")
        def test_expectedly_fails():
            assert False
        """
    )
    bare = pytester.runpytest()
    warden = pytester.runpytest("--warden")
    assert warden.ret == bare.ret
    warden.assert_outcomes(failed=1, xfailed=1)


def test_xfail_non_strict_survives_the_worker_round_trip(pytester):
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.xfail(reason="expected to fail")
        def test_unexpectedly_passes():
            assert True

        @pytest.mark.xfail(reason="expected to fail")
        def test_expectedly_fails():
            assert False
        """
    )
    bare = pytester.runpytest()
    warden = pytester.runpytest("--warden")
    assert warden.ret == bare.ret
    warden.assert_outcomes(xpassed=1, xfailed=1)


def test_setup_phase_skip_is_reported_correctly_through_warden(pytester):
    pytester.makepyfile(
        """
        import pytest

        @pytest.fixture
        def skipping_fixture():
            pytest.skip("skipped in setup")

        def test_uses_it(skipping_fixture):
            assert True
        """
    )
    result = pytester.runpytest("--warden")
    result.assert_outcomes(skipped=1)


def test_worker_raw_terminal_output_does_not_leak_into_the_controllers_own_output(pytester):
    # _spawn_worker used to Popen() the worker with no stdout/stderr
    # redirection, so it inherited the controller's real fds -- the
    # worker's own -q session banner/summary line printed directly to the
    # terminal, on top of the controller's own replayed reporting for the
    # exact same tests. Assert the worker's own "session starts" banner and
    # summary line don't appear at all; only the controller's single,
    # correctly-replayed summary should be visible.
    pytester.makepyfile(
        """
        def test_a():
            assert True

        def test_b():
            assert False
        """
    )
    result = pytester.runpytest("--warden")
    result.assert_outcomes(passed=1, failed=1)
    stdout = result.stdout.str()
    assert stdout.count("test session starts") == 1, (
        f"worker's own raw session banner leaked into the controller's output:\n{stdout}"
    )
    assert stdout.count("1 failed, 1 passed") == 1, (
        f"worker's own raw summary line leaked into the controller's output:\n{stdout}"
    )


def test_parametrized_test_ids_with_special_characters_survive_the_worker_round_trip(pytester):
    pytester.makepyfile(
        test_mod="""
        import pytest

        @pytest.mark.parametrize("value", ["plain", "with space", "with-dash", "unicode-\\u00e9"])
        def test_value(value):
            assert isinstance(value, str)
        """
    )
    bare = pytester.runpytest()
    warden = pytester.runpytest("--warden")
    assert warden.ret == bare.ret
    warden.assert_outcomes(passed=4)


def test_teardown_phase_error_is_reported_correctly_through_warden(pytester):
    pytester.makepyfile(
        """
        import pytest

        @pytest.fixture
        def broken_teardown():
            yield
            raise RuntimeError("teardown blew up")

        def test_uses_it(broken_teardown):
            assert True
        """
    )
    result = pytester.runpytest("--warden")
    result.assert_outcomes(passed=1, errors=1)


def test_third_party_logreport_consumer_plugin_observes_each_report_twice_known_limitation(
    pytester,
):
    # KNOWN LIMITATION, not a bug this pass fixes: each worker is a fully
    # real, independent `pytest` subprocess with the SAME conftest.py (and
    # therefore the same non-CLI-gated third-party plugins/hookimpls)
    # loaded as the controller. A conftest-defined pytest_runtest_logreport
    # hookimpl genuinely fires once for real inside the worker (real
    # execution, real side effect) AND once again when the controller
    # replays that same report through its own hook manager -- so any
    # third-party plugin with side effects beyond passive terminal display
    # (e.g. writing files, emitting metrics) will observe each event TWICE
    # under warden, not once. This is fundamentally different from
    # CLI-flag-gated plugins like --junitxml or the --lf/--ff cache: their
    # flags are never forwarded to workers (see _spawn_worker), so they
    # only ever run in the controller and are unaffected -- see
    # test_junitxml_records_a_hard_killed_test_with_a_readable_message and
    # test_last_failed_reruns_only_the_previously_failed_test.
    # Documented in README.md's "Best practices" section; see also
    # test_worker_raw_terminal_output_does_not_leak_into_the_controllers_own_output
    # for the (fixed) cosmetic half of this same root cause.
    pytester.makeconftest(
        """
        def pytest_runtest_logreport(report):
            if report.when == "call":
                with open("observed.txt", "a") as fh:
                    fh.write(report.nodeid + " " + report.outcome + "\\n")
        """
    )
    pytester.makepyfile(
        """
        def test_a():
            assert True

        def test_b():
            assert False
        """
    )
    result = pytester.runpytest("--warden")
    result.assert_outcomes(passed=1, failed=1)
    observed = (pytester.path / "observed.txt").read_text().splitlines()

    from collections import Counter

    counts = Counter(observed)
    assert set(counts.values()) == {2}, (
        f"expected every call-phase report to be observed exactly twice "
        f"(once from the worker's real run, once from the controller's "
        f"replay) -- got {dict(counts)}. If this ever becomes 1, the "
        f"limitation this test documents has been fixed; update the "
        f"README caveat and this test together."
    )
