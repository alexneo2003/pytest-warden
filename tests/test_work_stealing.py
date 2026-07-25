import time

from pytest_warden.history import HistoryStore
from pytest_warden.plugin import _chunk_queue, _default_chunk_size


def test_work_stealing_flag_is_recognized(pytester):
    pytester.makepyfile(
        """
        def test_ok():
            assert True
        """
    )
    result = pytester.runpytest("--warden", "--warden-work-stealing")
    result.stderr.no_fnmatch_line("*unrecognized arguments*")


def test_default_chunk_size_aims_for_about_four_chunks_per_worker():
    assert _default_chunk_size(total=40, numprocesses=2) == 5  # 40 / (2*4)
    assert _default_chunk_size(total=3, numprocesses=8) == 1  # never below 1


def test_chunk_queue_splits_weight_sorted_ids_into_fixed_size_chunks(tmp_path):
    store = HistoryStore(str(tmp_path / "history.sqlite3"))
    try:
        chunks = _chunk_queue(["a", "b", "c", "d", "e"], store, chunk_size=2)
        assert [len(c) for c in chunks] == [2, 2, 1]
        assert sum(chunks, []) == sorted(["a", "b", "c", "d", "e"])
    finally:
        store.close()


def test_work_stealing_keeps_all_workers_busy_until_the_queue_drains(pytester):
    # On a cold history store (no data yet), every test gets the same
    # default LPT weight, so static batching's "least-loaded, first index
    # on tie" rule degenerates to a plain alternating split by collection
    # position: with 2 workers, positions 0/2/4 land on worker 0 and
    # 1/3/5 on worker 1 -- each worker's WHOLE batch then runs as ONE
    # subprocess, sequentially. Putting both slow tests at positions 0
    # and 2 means static batching would stack them on the SAME worker,
    # running one after the other (~1.2s total). Work-stealing dispatches
    # one test per chunk, so the second slow test lands on whichever slot
    # happens to be free next -- here, the other slot, letting both slow
    # tests run concurrently instead of sequentially (~0.6-0.7s total).
    pytester.makepyfile(
        test_mod="""
        import time

        def test_slow_a():
            time.sleep(0.6)

        def test_fast_1():
            pass

        def test_slow_b():
            time.sleep(0.6)

        def test_fast_2():
            pass
        """
    )
    start = time.monotonic()
    result = pytester.runpytest(
        "--warden",
        "--numprocesses=2",
        "--warden-work-stealing",
        "--warden-chunk-size=1",
    )
    elapsed = time.monotonic() - start

    result.assert_outcomes(passed=4)
    assert elapsed < 1.15, f"took {elapsed}s -- looks like both slow tests ran sequentially"


def test_work_stealing_survives_a_crash_in_a_single_test_chunk(pytester):
    pytester.makepyfile(
        test_mod="""
        import os

        def test_a():
            assert True

        def test_b():
            os._exit(70)  # always crashes -- its own whole chunk at size 1

        def test_c():
            assert True
        """
    )
    result = pytester.runpytest(
        "--warden", "--numprocesses=1", "--warden-work-stealing", "--warden-chunk-size=1"
    )
    result.assert_outcomes(passed=2, failed=1)


def test_work_stealing_retries_the_never_started_remainder_of_a_crashed_chunk(pytester):
    pytester.makepyfile(
        test_mod="""
        import os

        def test_a():
            assert True

        def test_b():
            os._exit(70)  # crashes before test_c in the same chunk ever starts

        def test_c():
            assert True
        """
    )
    # chunk_size=3 puts all three tests in ONE chunk together -- test_a runs
    # and passes, test_b crashes while in flight (reported failed directly,
    # not retried), and test_c never gets a logstart in this attempt at all,
    # making it the genuine "never-started remainder" that should get
    # exactly one retry on a fresh chunk-worker.
    result = pytester.runpytest(
        "--warden", "--numprocesses=1", "--warden-work-stealing", "--warden-chunk-size=3"
    )
    result.assert_outcomes(passed=2, failed=1)
