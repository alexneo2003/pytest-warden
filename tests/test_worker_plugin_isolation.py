"""Opt-in mitigations for the double-firing limitation documented in
tests/test_reporting_compat.py::test_third_party_logreport_consumer_plugin_observes_each_report_twice_known_limitation
and README.md's "Best practices" section: a worker is a fully independent
pytest subprocess with the same conftest.py loaded as the controller, so a
conftest hookimpl fires once for real in the worker and once more via the
controller's replay.

Two independent mechanisms are tested here:
- --warden-disable-worker-plugin=NAME (-p no:NAME in the worker) -- only
  works for NAMED plugins, not bare conftest.py hookimpls.
- PYTEST_WARDEN_WORKER=1 in the worker's environment -- works for ANY
  hookimpl (named plugin or bare conftest.py) that checks for it.
"""


def test_named_worker_plugin_fires_twice_without_the_disable_flag(pytester):
    pytester.makeconftest(
        """
        class MyPlugin:
            def pytest_runtest_logreport(self, report):
                if report.when == "call":
                    with open("observed.txt", "a") as fh:
                        fh.write(report.nodeid + "\\n")

        def pytest_configure(config):
            config.pluginmanager.register(MyPlugin(), name="myplugin")
        """
    )
    pytester.makepyfile(
        """
        def test_ok():
            assert True
        """
    )
    result = pytester.runpytest("--warden")
    result.assert_outcomes(passed=1)
    observed = (pytester.path / "observed.txt").read_text().splitlines()
    assert len(observed) == 2, f"expected the un-mitigated double-fire, got {observed!r}"


def test_warden_disable_worker_plugin_makes_a_named_plugin_fire_exactly_once(pytester):
    pytester.makeconftest(
        """
        class MyPlugin:
            def pytest_runtest_logreport(self, report):
                if report.when == "call":
                    with open("observed.txt", "a") as fh:
                        fh.write(report.nodeid + "\\n")

        def pytest_configure(config):
            config.pluginmanager.register(MyPlugin(), name="myplugin")
        """
    )
    pytester.makepyfile(
        """
        def test_ok():
            assert True
        """
    )
    result = pytester.runpytest("--warden", "--warden-disable-worker-plugin=myplugin")
    result.assert_outcomes(passed=1)
    observed = (pytester.path / "observed.txt").read_text().splitlines()
    assert len(observed) == 1, (
        f"myplugin should have been blocked in the worker (-p no:myplugin), "
        f"leaving only the controller's replay -- got {observed!r}"
    )


def test_warden_disable_worker_plugin_does_not_affect_a_bare_conftest_hookimpl(pytester):
    # A bare hookimpl (not registered under a name) is NOT a nameable
    # plugin -p no:NAME can block -- passing a made-up/unrelated name must
    # not accidentally "fix" this by coincidence.
    pytester.makeconftest(
        """
        def pytest_runtest_logreport(report):
            if report.when == "call":
                with open("observed.txt", "a") as fh:
                    fh.write(report.nodeid + "\\n")
        """
    )
    pytester.makepyfile(
        """
        def test_ok():
            assert True
        """
    )
    result = pytester.runpytest("--warden", "--warden-disable-worker-plugin=some_unrelated_name")
    result.assert_outcomes(passed=1)
    observed = (pytester.path / "observed.txt").read_text().splitlines()
    assert len(observed) == 2, (
        f"a bare conftest.py hookimpl has no name to block -- the flag must "
        f"not change this documented limitation, got {observed!r}"
    )


def test_pytest_warden_worker_env_var_is_set_for_workers_and_absent_bare(pytester, monkeypatch):
    pytester.makepyfile(
        """
        import os

        def test_records_env_var():
            with open("env_value.txt", "w") as fh:
                fh.write(repr(os.environ.get("PYTEST_WARDEN_WORKER")))
        """
    )
    warden_result = pytester.runpytest("--warden")
    warden_result.assert_outcomes(passed=1)
    assert (pytester.path / "env_value.txt").read_text() == "'1'"

    (pytester.path / "env_value.txt").unlink()
    # A bare (non-`--warden`) run is fully inert -- it has no reason to
    # touch PYTEST_WARDEN_WORKER at all, so this check is only meaningful
    # against a clean ambient environment. Without this, running this
    # whole suite nested inside an outer `--warden` invocation (this
    # project's own self-hosted dogfood run) would inherit the OUTER
    # invocation's PYTEST_WARDEN_WORKER=1 and produce a false failure here
    # -- unrelated to whether THIS bare sub-run is itself inert.
    monkeypatch.delenv("PYTEST_WARDEN_WORKER", raising=False)
    bare_result = pytester.runpytest()
    bare_result.assert_outcomes(passed=1)
    assert (pytester.path / "env_value.txt").read_text() == "None"


def test_pytest_warden_worker_self_guard_pattern_produces_exactly_one_observed_event(pytester):
    # The documented README pattern: a bare conftest.py hookimpl that
    # checks PYTEST_WARDEN_WORKER to self-silence in workers -- this is the
    # mechanism that works even though --warden-disable-worker-plugin can't
    # touch it.
    pytester.makeconftest(
        """
        import os

        def pytest_runtest_logreport(report):
            if os.environ.get("PYTEST_WARDEN_WORKER"):
                return
            if report.when == "call":
                with open("observed.txt", "a") as fh:
                    fh.write(report.nodeid + "\\n")
        """
    )
    pytester.makepyfile(
        """
        def test_ok():
            assert True
        """
    )
    result = pytester.runpytest("--warden")
    result.assert_outcomes(passed=1)
    observed = (pytester.path / "observed.txt").read_text().splitlines()
    assert len(observed) == 1, (
        f"the self-guard pattern should have suppressed the worker's own "
        f"firing, leaving only the controller's replay -- got {observed!r}"
    )
