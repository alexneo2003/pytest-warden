from pytest_warden.history import HistoryStore
from pytest_warden.plugin import _chunk_queue, _lpt_batch


def _loadscope_key(node_id):
    return node_id.rsplit("::", 1)[0]


def _loadfile_key(node_id):
    return node_id.split("::", 1)[0]


def _assert_no_group_split(node_ids, batches, group_key_fn):
    batch_of = {}
    for i, batch in enumerate(batches):
        for nid in batch:
            batch_of[nid] = i

    groups = {}
    for nid in node_ids:
        groups.setdefault(group_key_fn(nid), []).append(nid)

    for key, members in groups.items():
        landed_in = {batch_of[nid] for nid in members}
        assert len(landed_in) == 1, f"group {key!r} split across batches: {members}"


def test_lpt_batch_keeps_a_loadscope_group_whole(tmp_path):
    store = HistoryStore(str(tmp_path / "history.sqlite3"))
    try:
        node_ids = [
            "a.py::TestX::test_1",
            "a.py::TestX::test_2",
            "a.py::test_3",
            "b.py::test_4",
        ]
        batches = _lpt_batch(
            node_ids, numprocesses=4, history_store=store, group_key_fn=_loadscope_key
        )
        assert sorted(sum(batches, [])) == sorted(node_ids)
        _assert_no_group_split(node_ids, batches, _loadscope_key)
    finally:
        store.close()


def test_lpt_batch_keeps_a_loadfile_group_whole(tmp_path):
    store = HistoryStore(str(tmp_path / "history.sqlite3"))
    try:
        node_ids = [
            "a.py::TestX::test_1",
            "a.py::TestX::test_2",
            "a.py::test_3",
            "b.py::test_4",
        ]
        batches = _lpt_batch(
            node_ids, numprocesses=4, history_store=store, group_key_fn=_loadfile_key
        )
        # loadfile merges TestX's two methods AND a.py::test_3 into one group --
        # all three must land in the same batch as each other.
        _assert_no_group_split(node_ids, batches, _loadfile_key)
        a_py_batch = next(b for b in batches if "a.py::test_3" in b)
        assert "a.py::TestX::test_1" in a_py_batch
        assert "a.py::TestX::test_2" in a_py_batch
    finally:
        store.close()


def test_lpt_batch_with_default_group_key_fn_is_unchanged(tmp_path):
    # No group_key_fn (or None) must behave exactly like today's per-test
    # granularity -- every node id is its own group of one.
    store = HistoryStore(str(tmp_path / "history.sqlite3"))
    try:
        node_ids = [f"test_mod.py::test_{i}" for i in range(10)]
        batches = _lpt_batch(node_ids, numprocesses=3, history_store=store)
        assert sorted(sum(batches, [])) == sorted(node_ids)
        assert len(set(sum(batches, []))) == len(node_ids)
    finally:
        store.close()


def test_chunk_queue_keeps_a_group_whole_even_past_the_chunk_size_boundary(tmp_path):
    store = HistoryStore(str(tmp_path / "history.sqlite3"))
    try:
        node_ids = [
            "a.py::TestX::test_1",
            "a.py::TestX::test_2",
            "a.py::TestX::test_3",
            "b.py::test_4",
        ]
        # chunk_size=2 would ordinarily split a group of 3 -- it must not.
        chunks = _chunk_queue(node_ids, store, chunk_size=2, group_key_fn=_loadscope_key)
        assert sorted(sum(chunks, [])) == sorted(node_ids)
        group_chunk = next(c for c in chunks if "a.py::TestX::test_1" in c)
        assert "a.py::TestX::test_2" in group_chunk
        assert "a.py::TestX::test_3" in group_chunk
    finally:
        store.close()


def test_chunk_queue_with_default_group_key_fn_is_unchanged(tmp_path):
    store = HistoryStore(str(tmp_path / "history.sqlite3"))
    try:
        chunks = _chunk_queue(["a", "b", "c", "d", "e"], store, chunk_size=2)
        assert [len(c) for c in chunks] == [2, 2, 1]
        assert sum(chunks, []) == sorted(["a", "b", "c", "d", "e"])
    finally:
        store.close()


def test_warden_dist_flag_is_recognized(pytester):
    pytester.makepyfile(
        """
        def test_ok():
            assert True
        """
    )
    result = pytester.runpytest("--warden", "--warden-dist=loadfile")
    result.stderr.no_fnmatch_line("*unrecognized arguments*")


def test_warden_group_marker_without_name_is_a_usage_error(pytester):
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.warden_group()
        def test_ok():
            assert True
        """
    )
    result = pytester.runpytest("--warden", "--warden-dist=loadgroup")
    assert result.ret != 0
    result.stderr.fnmatch_lines(["*@pytest.mark.warden_group(...) requires a name argument*"])


def test_loadscope_keeps_a_module_scoped_fixture_consistent_across_its_own_tests(pytester):
    # Inverts test_conftest_hierarchy.py's
    # test_module_scoped_fixture_is_not_shared_across_separate_workers: with
    # --warden-dist=loadscope, both tests in this module are one scope, so
    # they must land on the SAME worker -- the module-scoped counter's state
    # IS shared between them, reaching 2.
    pytester.makepyfile(
        test_mod="""
        import pytest

        @pytest.fixture(scope="module")
        def counter():
            return {"n": 0}

        def test_a(counter):
            counter["n"] += 1
            with open(f"seen_{counter['n']}.txt", "w") as fh:
                fh.write("a")

        def test_b(counter):
            counter["n"] += 1
            with open(f"seen_{counter['n']}.txt", "w") as fh:
                fh.write("b")
        """
    )
    result = pytester.runpytest("--warden", "--numprocesses=2", "--warden-dist=loadscope")
    result.assert_outcomes(passed=2)
    assert (pytester.path / "seen_2.txt").exists(), (
        "seen_2.txt missing -- test_a and test_b must share one worker's "
        "module-scoped counter under --warden-dist=loadscope"
    )


def test_loadfile_forces_multiple_classes_in_one_file_onto_the_same_worker(pytester):
    # loadscope groups by class (TestA and TestB would be free to land on
    # separate workers), but loadfile groups the WHOLE file as one unit --
    # both classes must end up on the identical worker subprocess. Each test
    # records its own pid; with --warden-dist=loadfile they must all match.
    pytester.makepyfile(
        test_mod="""
        import os

        class TestA:
            def test_1(self):
                with open("pid_a1.txt", "w") as fh:
                    fh.write(str(os.getpid()))

            def test_2(self):
                with open("pid_a2.txt", "w") as fh:
                    fh.write(str(os.getpid()))

        class TestB:
            def test_1(self):
                with open("pid_b1.txt", "w") as fh:
                    fh.write(str(os.getpid()))

            def test_2(self):
                with open("pid_b2.txt", "w") as fh:
                    fh.write(str(os.getpid()))
        """
    )
    result = pytester.runpytest("--warden", "--numprocesses=4", "--warden-dist=loadfile")
    result.assert_outcomes(passed=4)
    pids = {
        (pytester.path / name).read_text()
        for name in ("pid_a1.txt", "pid_a2.txt", "pid_b1.txt", "pid_b2.txt")
    }
    assert len(pids) == 1, f"expected all four tests on one worker, saw pids {pids}"


def test_loadscope_does_not_force_separate_classes_onto_the_same_worker(pytester):
    # The negative case distinguishing loadscope from loadfile: with exactly
    # as many workers as scopes (2 classes, numprocesses=2), the two
    # class-scopes are deterministically assigned to separate workers by
    # LPT's least-loaded-first-index rule, proving loadscope does NOT merge
    # different classes the way loadfile does.
    pytester.makepyfile(
        test_mod="""
        import os

        class TestA:
            def test_1(self):
                with open("pid_a.txt", "w") as fh:
                    fh.write(str(os.getpid()))

        class TestB:
            def test_1(self):
                with open("pid_b.txt", "w") as fh:
                    fh.write(str(os.getpid()))
        """
    )
    result = pytester.runpytest("--warden", "--numprocesses=2", "--warden-dist=loadscope")
    result.assert_outcomes(passed=2)
    pid_a = (pytester.path / "pid_a.txt").read_text()
    pid_b = (pytester.path / "pid_b.txt").read_text()
    assert pid_a != pid_b


def test_loadgroup_co_locates_tests_sharing_a_marker_across_files(pytester):
    pytester.makepyfile(
        test_one="""
        import os
        import pytest

        @pytest.mark.warden_group(name="shared")
        def test_marked():
            with open("pid_one.txt", "w") as fh:
                fh.write(str(os.getpid()))
        """,
        test_two="""
        import os
        import pytest

        @pytest.mark.warden_group(name="shared")
        def test_marked():
            with open("pid_two.txt", "w") as fh:
                fh.write(str(os.getpid()))

        def test_unmarked():
            pass
        """,
    )
    result = pytester.runpytest("--warden", "--numprocesses=4", "--warden-dist=loadgroup")
    result.assert_outcomes(passed=3)
    pid_one = (pytester.path / "pid_one.txt").read_text()
    pid_two = (pytester.path / "pid_two.txt").read_text()
    assert pid_one == pid_two


def test_work_stealing_also_keeps_a_loadfile_group_whole(pytester):
    # Grouping applies to both schedulers -- confirm it holds under
    # --warden-work-stealing too, not just static LPT.
    pytester.makepyfile(
        test_mod="""
        import os

        class TestA:
            def test_1(self):
                with open("pid_1.txt", "w") as fh:
                    fh.write(str(os.getpid()))

            def test_2(self):
                with open("pid_2.txt", "w") as fh:
                    fh.write(str(os.getpid()))
        """
    )
    result = pytester.runpytest(
        "--warden",
        "--numprocesses=4",
        "--warden-work-stealing",
        "--warden-dist=loadfile",
    )
    result.assert_outcomes(passed=2)
    pid_1 = (pytester.path / "pid_1.txt").read_text()
    pid_2 = (pytester.path / "pid_2.txt").read_text()
    assert pid_1 == pid_2
