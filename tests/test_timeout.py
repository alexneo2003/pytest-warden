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
