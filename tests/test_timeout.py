import time

from conftest import process_exists


def test_hanging_test_is_hard_killed_after_timeout(pytester):
    pytester.makepyfile(
        """
        import time

        def test_hangs():
            time.sleep(30)
        """
    )
    start = time.monotonic()
    result = pytester.runpytest("--warden", "--timeout=1")
    elapsed = time.monotonic() - start

    assert elapsed < 15, f"took {elapsed}s, should have been hard-killed near the 1s timeout"
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*hard-killed*timeout*"])


def test_no_orphaned_process_survives_the_hard_kill(pytester):
    pytester.makepyfile(
        """
        import subprocess
        import time

        def test_hangs_with_a_child_process():
            child = subprocess.Popen(["sleep", "30"])
            with open("child.pid", "w") as fh:
                fh.write(str(child.pid))
            time.sleep(30)
        """
    )
    result = pytester.runpytest("--warden", "--timeout=1")
    result.assert_outcomes(failed=1)

    pid_file = pytester.path / "child.pid"
    assert pid_file.exists(), "test should have started writing the child pid before being killed"
    child_pid = int(pid_file.read_text().strip())

    time.sleep(0.3)
    assert not process_exists(child_pid)


def test_hard_kill_reaches_a_grandchild_process(pytester):
    # The process-group kill (POSIX) / Job Object kill (Windows) targets
    # the whole tree by group/job membership, not a snapshot of direct
    # children -- a grandchild (child-of-child) inherits membership the
    # same way a direct child does, so it must die too.
    pytester.makepyfile(
        """
        import subprocess
        import sys
        import time

        def test_hangs_with_a_grandchild_process():
            # child prints its own child's (the grandchild's) pid, then hangs
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import subprocess, time\\n"
                    "gc = subprocess.Popen(['sleep', '30'])\\n"
                    "print(gc.pid, flush=True)\\n"
                    "time.sleep(30)\\n",
                ],
                stdout=subprocess.PIPE,
                text=True,
            )
            grandchild_pid = int(child.stdout.readline().strip())
            with open("grandchild.pid", "w") as fh:
                fh.write(str(grandchild_pid))
            time.sleep(30)
        """
    )
    result = pytester.runpytest("--warden", "--timeout=2")
    result.assert_outcomes(failed=1)

    pid_file = pytester.path / "grandchild.pid"
    assert pid_file.exists(), "test should have written the grandchild pid before being killed"
    grandchild_pid = int(pid_file.read_text().strip())

    time.sleep(0.3)
    assert not process_exists(grandchild_pid)


def test_hard_kill_of_one_worker_does_not_affect_a_sibling_worker(pytester):
    pytester.makepyfile(
        """
        import time

        def test_hangs():
            time.sleep(30)

        def test_passes_quickly():
            time.sleep(0.1)
        """
    )
    result = pytester.runpytest("--warden", "--timeout=1", "--numprocesses=2")
    result.assert_outcomes(failed=1, passed=1)


def test_many_trivial_tests_on_one_fast_worker_are_all_accounted_for(pytester):
    # Stresses races on fast start/finish: many tests complete within a
    # single _POLL_INTERVAL (50ms), so _read_new_lines must correctly pick
    # up ALL buffered lines from one poll rather than losing or
    # misordering any.
    body = "\n".join(f"def test_{i}():\n    pass\n" for i in range(60))
    pytester.makepyfile(body)
    result = pytester.runpytest("--warden", "--numprocesses=1")
    result.assert_outcomes(passed=60)


def test_teardown_finalizer_does_not_run_after_a_hard_kill_documents_the_trade_off(pytester):
    # Inherent to SIGKILL/TerminateJobObject -- there's no reasonable fix
    # within this architecture. Documents the trade-off (matching the
    # existing coverage-loss precedent in test_audit_gaps.py) rather than
    # asserting a behavior that would require catching an unctachable
    # signal.
    pytester.makepyfile(
        """
        import time

        import pytest

        @pytest.fixture
        def marks_cleanup_on_teardown():
            yield
            with open("cleanup_ran.txt", "w") as fh:
                fh.write("yes")

        def test_hangs(marks_cleanup_on_teardown):
            time.sleep(30)
        """
    )
    result = pytester.runpytest("--warden", "--timeout=1")
    result.assert_outcomes(failed=1)
    assert not (pytester.path / "cleanup_ran.txt").exists(), (
        "a hard kill is a SIGKILL-equivalent -- teardown finalizers cannot run, "
        "by design; if this ever starts passing, the architecture changed"
    )


def test_pytest_mark_timeout_overrides_the_global_default_for_that_test_only(pytester):
    pytester.makepyfile(
        """
        import time

        import pytest

        @pytest.mark.timeout(1)
        def test_hangs():
            time.sleep(30)

        def test_passes_quickly():
            time.sleep(0.1)
        """
    )
    start = time.monotonic()
    # No --timeout at all: only the marker's own budget should apply to
    # test_hangs, hard-killing it near 1s instead of running unsupervised.
    result = pytester.runpytest("--warden", "--numprocesses=1")
    elapsed = time.monotonic() - start

    assert elapsed < 15, f"took {elapsed}s, should have been hard-killed near the marker's 1s timeout"
    result.assert_outcomes(failed=1, passed=1)
    result.stdout.fnmatch_lines(["*hard-killed*1.0s timeout*"])
    result.stdout.no_fnmatch_line("*PytestUnknownMarkWarning*")


def test_pytest_mark_timeout_is_recognized_without_warden_too(pytester):
    # The marker must be registered unconditionally, not only when --warden
    # is passed, or a bare (non-warden) run of the same suite would still
    # warn -- and the marker is genuinely inert there, same as --timeout.
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.timeout(5)
        def test_quick():
            pass
        """
    )
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)
    result.stdout.no_fnmatch_line("*PytestUnknownMarkWarning*")


def test_pytest_mark_timeout_with_non_positive_seconds_is_a_usage_error(pytester):
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.timeout(0)
        def test_quick():
            pass
        """
    )
    result = pytester.runpytest("--warden")
    assert result.ret != 0
    result.stderr.fnmatch_lines(["*@pytest.mark.timeout seconds must be positive*"])


def test_hard_killed_test_gets_its_own_progress_line_not_smashed_into_the_next_status_line(
    pytester,
):
    # Regression: _report_incident/_report_never_ran used to call
    # hook.pytest_runtest_logreport() directly, unlike _replay_event's own
    # logreport handling -- which meant pytest's raw, unsuppressed "F"
    # (written with no trailing newline) landed right before whatever text
    # printed next, producing garbage like "Fwarden: worker 0 didn't finish
    # its batch...". Two hanging tests on a single worker reproduces it:
    # the first hard-kill takes the whole worker down, orphaning the
    # second test, which immediately triggers that "didn't finish its
    # batch" message right after the kill.
    pytester.makepyfile(
        """
        import time

        def test_hangs_one():
            time.sleep(30)

        def test_hangs_two():
            time.sleep(30)
        """
    )
    result = pytester.runpytest("--warden", "--timeout=1", "--numprocesses=1")
    result.assert_outcomes(failed=2)
    stdout = str(result.stdout)
    assert "Fwarden:" not in stdout, "raw unsuppressed letter smashed into the next status line"
    # Every hard-killed test gets the same numbered progress line a
    # normally-finishing test would, not just a bare unsuppressed letter.
    result.stdout.fnmatch_lines(["*worker 0 -> *test_hangs_one FAILED*"])
    result.stdout.fnmatch_lines(["*worker 1 -> *test_hangs_two FAILED*"])


def test_hard_killed_test_does_not_smash_its_letter_into_the_next_line_under_dash_q(pytester):
    # Same underlying bug as the test above, but reproduced under -q: there
    # _print_progress_line's own [done/total] line is (correctly)
    # suppressed, same as pytest's own dot-stream output would be, but the
    # raw "F" _report_incident writes is legitimately NOT suppressed
    # either (matching vanilla `pytest -q`'s own plain-dots look) -- so the
    # smash-prevention can't rely on suppressing the letter here at all;
    # it must come from _warden_write_line itself ensuring a fresh line.
    pytester.makepyfile(
        """
        import time

        def test_hangs_one():
            time.sleep(30)

        def test_hangs_two():
            time.sleep(30)
        """
    )
    result = pytester.runpytest("--warden", "--timeout=1", "--numprocesses=1", "-q")
    result.assert_outcomes(failed=2)
    stdout = str(result.stdout)
    assert "Fwarden:" not in stdout
    assert "FFwarden:" not in stdout


def test_no_duplicate_report_for_a_hard_killed_test(pytester):
    pytester.makepyfile(
        """
        import time

        def test_hangs():
            time.sleep(30)
        """
    )
    result = pytester.runpytest("--warden", "--timeout=1")
    # assert_outcomes(failed=1) is itself the "no duplicate report" proof:
    # it reflects pytest's own report-stats accounting (one entry per
    # logreport event), so a genuine duplicate report for this one test
    # would show up as failed=2 here directly -- no need to additionally
    # scrape stdout text for it. (A prior version of this test counted
    # occurrences of the substring "hard-killed" in raw stdout instead,
    # which is NOT a reliable proxy: pytest's short summary info line
    # legitimately reproduces the same failure message a second time, and
    # whether that second occurrence survives terminal-width truncation
    # depends on the invoking environment's column width -- this passed
    # locally but failed on real CI purely because of a wider terminal,
    # with no actual duplicate report involved.)
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*hard-killed*"])
