import time


def test_lpt_uses_history_to_avoid_stacking_two_slow_tests_on_one_worker(pytester):
    pytester.makepyfile(
        test_mod="""
        import time

        def test_a():
            time.sleep(0.6)

        def test_b():
            pass

        def test_c():
            time.sleep(0.6)

        def test_d():
            pass
        """
    )
    history_db = str(pytester.path / "history.sqlite3")

    # First run: no history yet, so batching falls back to an even split
    # (which happens to stack test_a and test_c -- both slow -- on the same
    # worker under plain index-order round robin). This run's real
    # durations get recorded regardless of how it was batched.
    result1 = pytester.runpytest(
        "--warden", "--numprocesses=2", f"--warden-history-db={history_db}"
    )
    result1.assert_outcomes(passed=4)

    # Second run: with real history now available, LPT should balance the
    # two slow tests onto different workers instead of stacking them --
    # observable as total wall-clock time roughly halving.
    start = time.monotonic()
    result2 = pytester.runpytest(
        "--warden", "--numprocesses=2", f"--warden-history-db={history_db}"
    )
    elapsed = time.monotonic() - start

    result2.assert_outcomes(passed=4)
    assert elapsed < 1.0, f"took {elapsed}s -- looks like both slow tests landed on the same worker"
