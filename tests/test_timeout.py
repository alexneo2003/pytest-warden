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
        import time

        def test_hangs_with_a_grandchild_process():
            # child prints its own child's (the grandchild's) pid, then hangs
            child = subprocess.Popen(
                [
                    "python3",
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


def test_no_duplicate_report_for_a_hard_killed_test(pytester):
    pytester.makepyfile(
        """
        import time

        def test_hangs():
            time.sleep(30)
        """
    )
    result = pytester.runpytest("--warden", "--timeout=1")
    result.assert_outcomes(failed=1)
    stdout = result.stdout.str()
    assert stdout.count("hard-killed") == 1, (
        f"expected exactly one hard-kill report, got {stdout.count('hard-killed')}:\n{stdout}"
    )
