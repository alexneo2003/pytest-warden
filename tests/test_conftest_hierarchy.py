def _make_shadowed_fixture_project(pytester):
    (pytester.path / "conftest.py").write_text(
        "import pytest\n\n@pytest.fixture\ndef thing():\n    return 'top'\n"
    )
    (pytester.path / "test_top.py").write_text(
        "def test_uses_thing(thing):\n    assert thing == 'top'\n"
    )

    sub = pytester.path / "sub"
    sub.mkdir()
    (sub / "conftest.py").write_text(
        "import pytest\n\n@pytest.fixture\ndef thing():\n    return 'sub'\n"
    )
    (sub / "test_sub.py").write_text("def test_uses_thing(thing):\n    assert thing == 'sub'\n")


def test_shadowed_fixture_resolves_correctly_within_a_single_worker(pytester):
    # This is the case the original ctrlrunner-migration pivot was actually
    # worried about: one process running tests from both subtrees needs
    # pytest's own hierarchical (deepest-conftest-wins) fixture resolution,
    # not a flat last-import-wins namespace. Since the worker here is real,
    # unmodified pytest, this must pass by construction.
    _make_shadowed_fixture_project(pytester)
    result = pytester.runpytest("--warden", "--numprocesses=1")
    result.assert_outcomes(passed=2)


def test_shadowed_fixture_resolves_correctly_across_separate_workers(pytester):
    # Same fixture-shadowing setup, but with each test plausibly landing on
    # a different worker subprocess -- proves warden's distribution layer
    # doesn't disturb per-worker collection/fixture resolution either.
    _make_shadowed_fixture_project(pytester)
    result = pytester.runpytest("--warden", "--numprocesses=2")
    result.assert_outcomes(passed=2)


def test_module_scoped_fixture_is_not_shared_across_separate_workers(pytester):
    # Documents an inherent, expected trade-off of the multi-process
    # distribution model (identical to pytest-xdist's own well-known
    # behavior), not a bug: each worker is a fully separate `pytest`
    # subprocess, so a module-scoped fixture's state is scoped PER-WORKER,
    # not once for the whole warden run. A module-scoped counter fixture
    # split across two workers must show TWO separate "first call" results,
    # not one shared counter reaching 2.
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
    result = pytester.runpytest("--warden", "--numprocesses=2")
    result.assert_outcomes(passed=2)
    # If the fixture were truly shared across both workers, only
    # seen_1.txt would exist (one worker would see n=1, the other n=2). If
    # it's per-worker (as it must be), BOTH workers independently see
    # their own counter start at 0 -> 1, so only seen_1.txt exists for
    # each -- but written by each worker separately, so the file gets
    # overwritten by whichever runs second. What actually distinguishes
    # per-worker scoping here is that seen_2.txt does NOT exist at all,
    # since neither worker's own counter ever reaches 2.
    assert not (pytester.path / "seen_2.txt").exists(), (
        "seen_2.txt existing would mean both tests shared one module-scoped "
        "fixture instance across workers -- they must not"
    )
    assert (pytester.path / "seen_1.txt").exists()
