import time


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


def test_negative_numprocesses_is_silently_clamped_to_one_documents_current_behavior(pytester):
    # Unlike --timeout=0/negative and --warden-chunk-size=0/negative, which
    # are explicitly rejected with a UsageError (see test_audit_gaps.py),
    # --numprocesses has no such validation: _lpt_batch's
    # `max(1, min(numprocesses, len(node_ids)))` and _default_chunk_size's
    # `max(1, numprocesses)` both silently clamp 0/negative to 1 instead of
    # raising. This documents that inconsistency as CURRENT behavior rather
    # than assuming either way is correct -- surfaced to the maintainer as
    # a judgment call (arguably fine, since "at least 1 worker" is a
    # reasonable reading of a nonsensical request, unlike --timeout=0's
    # actively misleading "unlimited" reading) rather than auto-fixed here.
    pytester.makepyfile(
        """
        def test_ok():
            assert True
        """
    )
    result = pytester.runpytest("--warden", "--numprocesses=-1")
    result.assert_outcomes(passed=1)
    result.stderr.no_fnmatch_line("*UsageError*")


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
        ]
    )
