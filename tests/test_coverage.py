import types

import coverage

from pytest_warden.plugin import _combine_coverage


def test_coverage_combines_across_workers(pytester):
    pytester.makepyfile(mod="def add(a, b):\n    return a + b\n")
    pytester.makepyfile(
        test_mod="""
        from mod import add

        def test_add():
            assert add(1, 2) == 3
        """
    )
    result = pytester.runpytest("--warden", "--numprocesses=1", "--cov=mod")
    result.assert_outcomes(passed=1)

    combined = pytester.path / ".coverage"
    assert combined.exists(), "expected a combined .coverage data file after the run"

    cov = coverage.Coverage(data_file=str(combined))
    cov.load()
    _, statements, _, missing, _ = cov.analysis2(str(pytester.path / "mod.py"))
    assert statements, "expected some statements to have been tracked at all"
    assert missing == [], f"expected full coverage of mod.py, missing lines: {missing}"


def test_coverage_is_untouched_when_cov_flag_not_used(pytester):
    pytester.makepyfile(
        """
        def test_ok():
            assert True
        """
    )
    result = pytester.runpytest("--warden", "--numprocesses=1")
    result.assert_outcomes(passed=1)
    assert not (pytester.path / ".coverage").exists()


def test_coverage_combines_across_two_parallel_workers(pytester):
    pytester.makepyfile(
        mod="""
        def add(a, b):
            return a + b

        def sub(a, b):
            return a - b
        """
    )
    pytester.makepyfile(
        test_mod="""
        from mod import add, sub

        def test_add():
            assert add(1, 2) == 3

        def test_sub():
            assert sub(3, 1) == 2
        """
    )
    result = pytester.runpytest("--warden", "--numprocesses=2", "--cov=mod")
    result.assert_outcomes(passed=2)

    combined = pytester.path / ".coverage"
    assert combined.exists()

    cov = coverage.Coverage(data_file=str(combined))
    cov.load()
    _, statements, _, missing, _ = cov.analysis2(str(pytester.path / "mod.py"))
    assert statements
    assert missing == [], f"expected full coverage of mod.py, missing lines: {missing}"


def test_combine_survives_a_worker_that_wrote_a_corrupt_not_just_missing_coverage_file(tmp_path):
    # Real subprocess timing can't reliably force a genuinely CORRUPT (not
    # just missing) coverage data file -- that would require killing a
    # worker at the exact instant coverage.py is mid-write, which isn't a
    # deterministic window to hit. Since the property under test is
    # coverage.py's own error handling inside _combine_coverage, a direct
    # unit test against a hand-written garbage-bytes file is the more
    # precise and less flaky tool here.
    good_data_file = str(tmp_path / "good.coverage")
    good_cov = coverage.Coverage(data_file=good_data_file)
    good_cov.start()
    good_cov.stop()
    good_cov.save()

    corrupt_data_file = tmp_path / "corrupt.coverage"
    corrupt_data_file.write_bytes(b"not a real sqlite coverage database")

    session = types.SimpleNamespace(
        config=types.SimpleNamespace(
            option=types.SimpleNamespace(cov_source=["."]),
            rootpath=tmp_path,
        )
    )
    workers = [
        types.SimpleNamespace(cov_data_file=good_data_file),
        types.SimpleNamespace(cov_data_file=str(corrupt_data_file)),
    ]

    _combine_coverage(session, workers)

    combined = tmp_path / ".coverage"
    assert combined.exists(), (
        "combine should still produce a usable combined file from the good "
        "data even though one worker's file was corrupt"
    )


def test_no_worker_coverage_temp_files_leak_into_the_rootdir_after_a_run(pytester):
    pytester.makepyfile(mod="def add(a, b):\n    return a + b\n")
    pytester.makepyfile(
        test_mod="""
        from mod import add

        def test_add():
            assert add(1, 2) == 3
        """
    )
    result = pytester.runpytest("--warden", "--numprocesses=2", "--cov=mod")
    result.assert_outcomes(passed=1)

    leaked = list(pytester.path.glob(".coverage.worker-*"))
    assert leaked == [], f"per-worker coverage temp files leaked into the rootdir: {leaked}"


def test_coverage_combine_handles_two_hard_killed_workers_simultaneously(pytester):
    pytester.makepyfile(
        test_mod="""
        import time

        def test_hangs_1():
            time.sleep(30)

        def test_hangs_2():
            time.sleep(30)
        """
    )
    result = pytester.runpytest(
        "--warden", "--numprocesses=2", "--timeout=1", "--cov=pytest_warden"
    )
    result.assert_outcomes(failed=2)
    # No data files survive at all (both workers were hard-killed before
    # coverage.py could flush) -- _combine_coverage's empty-data_files early
    # return must not crash the run.
