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
