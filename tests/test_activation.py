import time

import pytest

from pytest_warden.plugin import _resolve_numprocesses


def test_resolve_numprocesses_accepts_plain_positive_int():
    assert _resolve_numprocesses("4") == 4


def test_resolve_numprocesses_rejects_zero_and_negative_int():
    for raw in ("0", "-1"):
        with pytest.raises(pytest.UsageError, match="positive integer"):
            _resolve_numprocesses(raw)


def test_resolve_numprocesses_rejects_non_numeric_garbage():
    with pytest.raises(pytest.UsageError, match="positive integer"):
        _resolve_numprocesses("abc")


def test_resolve_numprocesses_percentage_uses_injected_cpu_count():
    assert _resolve_numprocesses("50%", cpu_count_fn=lambda: 8) == 4
    assert _resolve_numprocesses("25%", cpu_count_fn=lambda: 8) == 2


def test_resolve_numprocesses_percentage_rounds_up_to_at_least_one_worker():
    assert _resolve_numprocesses("1%", cpu_count_fn=lambda: 4) == 1


def test_resolve_numprocesses_rejects_zero_and_negative_percentage():
    for raw in ("0%", "-10%"):
        with pytest.raises(pytest.UsageError, match="positive integer"):
            _resolve_numprocesses(raw, cpu_count_fn=lambda: 8)


def test_warden_flag_is_recognized(pytester):
    pytester.makepyfile(
        """
        def test_ok():
            assert True
        """
    )
    result = pytester.runpytest("--warden")
    result.stderr.no_fnmatch_line("*unrecognized arguments*")


def test_bare_run_prints_no_warden_terminal_summary_line(pytester):
    pytester.makepyfile(
        """
        def test_ok():
            assert True
        """
    )
    result = pytester.runpytest()
    result.stdout.no_fnmatch_line("*warden: distributed across*")


def test_warden_specific_flags_have_no_effect_without_the_warden_flag(pytester):
    # Without --warden, --timeout must NOT hard-kill anything -- a test
    # sleeping past the configured timeout should genuinely take that long,
    # proving the flag is truly inert rather than merely unprinted.
    pytester.makepyfile(
        """
        import time

        def test_slow():
            time.sleep(1.5)
        """
    )
    start = time.monotonic()
    result = pytester.runpytest("--timeout=1", "--numprocesses=4", "--warden-quarantine-flaky")
    elapsed = time.monotonic() - start
    result.assert_outcomes(passed=1)
    assert elapsed >= 1.4, (
        f"test_slow finished in {elapsed}s -- looks like --timeout hard-killed it "
        f"even though --warden was never passed"
    )


def test_negative_numprocesses_is_rejected_instead_of_silently_clamped(pytester):
    # Was previously silently clamped to 1, inconsistent with --timeout and
    # --warden-chunk-size's existing explicit-rejection convention -- now
    # matches them.
    pytester.makepyfile(
        """
        def test_ok():
            assert True
        """
    )
    result = pytester.runpytest("--warden", "--numprocesses=-1")
    assert result.ret != 0
    result.stderr.fnmatch_lines(["*--numprocesses must be a positive integer*"])


def test_zero_numprocesses_is_rejected(pytester):
    pytester.makepyfile(
        """
        def test_ok():
            assert True
        """
    )
    result = pytester.runpytest("--warden", "--numprocesses=0")
    assert result.ret != 0
    result.stderr.fnmatch_lines(["*--numprocesses must be a positive integer*"])


def test_non_numeric_numprocesses_is_rejected(pytester):
    pytester.makepyfile(
        """
        def test_ok():
            assert True
        """
    )
    result = pytester.runpytest("--warden", "--numprocesses=abc")
    assert result.ret != 0
    result.stderr.fnmatch_lines(["*--numprocesses must be a positive integer*"])


def test_numprocesses_percentage_is_accepted_end_to_end(pytester):
    pytester.makepyfile(
        """
        def test_a():
            assert True

        def test_b():
            assert True
        """
    )
    result = pytester.runpytest("--warden", "--numprocesses=50%")
    result.assert_outcomes(passed=2)


def test_help_lists_all_warden_flags(pytester):
    result = pytester.runpytest("--help")
    result.stdout.fnmatch_lines(
        [
            "*--warden*",
            "*--numprocesses=*",
            "*--timeout=*",
            "*--warden-history-db=*",
            "*--warden-quarantine-flaky*",
            "*--warden-work-stealing*",
            "*--warden-chunk-size=*",
            "*--warden-disable-worker-plugin=*",
        ]
    )
