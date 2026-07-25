def test_last_failed_reruns_only_the_previously_failed_test(pytester):
    pytester.makepyfile(
        test_mod="""
        def test_ok():
            assert True

        def test_bad():
            assert False
        """
    )
    first = pytester.runpytest("--warden")
    first.assert_outcomes(passed=1, failed=1)

    second = pytester.runpytest("--warden", "--last-failed")
    second.assert_outcomes(failed=1)
    second.stdout.fnmatch_lines(["collected 1 item"])


def test_failed_first_still_runs_everything(pytester):
    pytester.makepyfile(
        test_mod="""
        def test_a():
            assert True

        def test_b():
            assert False

        def test_c():
            assert True
        """
    )
    first = pytester.runpytest("--warden")
    first.assert_outcomes(passed=2, failed=1)

    second = pytester.runpytest("--warden", "--failed-first")
    second.assert_outcomes(passed=2, failed=1)


def test_lpt_batching_puts_previously_failed_tests_first_even_when_slower(pytester):
    # -q quiet mode never prints a *passing* test's name at all, so
    # scraping stdout for "test_a"/"test_b" would trivially find only the
    # failing one regardless of real execution order -- a false-positive
    # waiting to happen. Have each test record its own name to a shared
    # file instead, so order is observed directly, not inferred from output.
    pytester.makepyfile(
        test_mod="""
        import time

        def _record(name):
            with open("order.txt", "a") as fh:
                fh.write(name + "\\n")

        def test_a():
            time.sleep(0.2)  # recorded as slower -- would sort first by weight alone
            _record("test_a")

        def test_b():
            _record("test_b")
            assert False  # previously failed, but faster than test_a
        """
    )
    first = pytester.runpytest("--warden", "--numprocesses=1")
    first.assert_outcomes(passed=1, failed=1)

    (pytester.path / "order.txt").unlink()

    second = pytester.runpytest("--warden", "--numprocesses=1", "--failed-first")
    order = (pytester.path / "order.txt").read_text().splitlines()
    assert order[0] == "test_b", f"expected test_b (failed-first) to run before test_a, got: {order}"
