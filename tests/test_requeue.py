def test_crashed_worker_requeues_the_never_started_remainder_onto_a_fresh_worker(
    pytester,
):
    pytester.makepyfile(
        """
        import os

        def test_a():
            assert True

        def test_b():
            os._exit(70)  # simulate an abrupt worker crash mid-test

        def test_c():
            assert True
        """
    )
    result = pytester.runpytest("--warden", "--numprocesses=1")
    # test_a already passed in the first attempt; test_b was in-flight when
    # the worker died, so per plan semantics it's marked failed directly (the
    # crashing test itself is never retried); test_c never even started in
    # the first attempt, so it's the "remainder" that gets exactly one
    # fresh-worker retry, and passes there.
    result.assert_outcomes(passed=2, failed=1)


def test_repeatedly_crashing_test_is_marked_failed_after_one_retry_not_requeued_forever(
    pytester,
):
    pytester.makepyfile(
        """
        import os

        def test_a():
            assert True

        def test_b():
            os._exit(70)  # always crashes -- must not loop forever

        def test_c():
            assert True
        """
    )
    result = pytester.runpytest("--warden", "--numprocesses=1")
    # test_a passes on the first attempt; test_b crashes every time so it's
    # marked failed after exactly one retry (not requeued again); test_c
    # never got to run in the first attempt (killed process before reaching
    # it) but does succeed on the single retry.
    result.assert_outcomes(passed=2, failed=1)
