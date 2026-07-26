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
