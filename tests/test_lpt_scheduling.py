def test_lpt_uses_history_to_avoid_stacking_two_slow_tests_on_one_worker(pytester):
    # Each slow test records its own start/end monotonic timestamp to a
    # shared file. Overlapping intervals prove the two ran concurrently on
    # separate workers; non-overlapping proves they were stacked
    # sequentially on the same one. This is immune to how fast or slow the
    # CI runner happens to be -- a fixed wall-clock threshold isn't: a
    # loaded/slow runner (Windows CI in particular tends to have much
    # higher subprocess-spawn overhead than local dev) could blow past a
    # tight absolute threshold even when the scheduling itself is correct.
    pytester.makepyfile(
        test_mod="""
        import time

        def _record(name, value):
            with open("timing.txt", "a") as fh:
                fh.write(f"{name} {value}\\n")

        def test_a():
            _record("a_start", time.monotonic())
            time.sleep(0.6)
            _record("a_end", time.monotonic())

        def test_b():
            pass

        def test_c():
            _record("c_start", time.monotonic())
            time.sleep(0.6)
            _record("c_end", time.monotonic())

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

    (pytester.path / "timing.txt").unlink()

    # Second run: with real history now available, LPT should balance the
    # two slow tests onto different workers instead of stacking them.
    result2 = pytester.runpytest(
        "--warden", "--numprocesses=2", f"--warden-history-db={history_db}"
    )
    result2.assert_outcomes(passed=4)

    timing = dict(line.split() for line in (pytester.path / "timing.txt").read_text().splitlines())
    a_start, a_end = float(timing["a_start"]), float(timing["a_end"])
    c_start, c_end = float(timing["c_start"]), float(timing["c_end"])
    overlap = min(a_end, c_end) - max(a_start, c_start)
    assert overlap > 0, (
        f"test_a and test_c did not run concurrently -- looks like both landed "
        f"on the same worker: a=({a_start}, {a_end}) c=({c_start}, {c_end})"
    )
