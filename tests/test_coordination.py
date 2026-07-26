import subprocess
import sys

from pytest_warden import coordination


def test_run_once_calls_fn_exactly_once_for_repeated_same_key_calls(tmp_path):
    calls = []

    def fn():
        calls.append(1)
        return "result"

    first = coordination.run_once("k", str(tmp_path), fn)
    second = coordination.run_once("k", str(tmp_path), fn)

    assert first == "result"
    assert second == "result"
    assert len(calls) == 1


def test_run_once_uses_a_separate_result_per_key(tmp_path):
    calls = {"a": 0, "b": 0}

    def make_fn(name):
        def fn():
            calls[name] += 1
            return name

        return fn

    assert coordination.run_once("a", str(tmp_path), make_fn("a")) == "a"
    assert coordination.run_once("b", str(tmp_path), make_fn("b")) == "b"
    assert calls == {"a": 1, "b": 1}


def test_run_once_round_trips_json_serializable_result_shapes(tmp_path):
    result = {"nested": [1, 2, "three"], "flag": True}
    first = coordination.run_once("shape", str(tmp_path), lambda: result)
    second = coordination.run_once(
        "shape",
        str(tmp_path),
        lambda: (_ for _ in ()).throw(AssertionError("fn must not be called again")),
    )
    assert first == result
    assert second == result


def test_run_once_across_real_concurrent_subprocesses_runs_fn_exactly_once(tmp_path):
    # Real separate OS processes (not threads), mirroring
    # test_history.py::test_concurrent_history_store_access_does_not_deadlock_or_corrupt
    # -- the property under test is real OS-level file locking, which
    # thread-based concurrency within one process wouldn't meaningfully
    # exercise.
    shared_dir = tmp_path / "shared"
    log_path = tmp_path / "calls.log"
    worker_script = tmp_path / "worker.py"
    worker_script.write_text(
        f"""
import sys
from pytest_warden import coordination

def fn():
    with open({str(log_path)!r}, "a") as fh:
        fh.write("called\\n")
    return "the-one-true-result"

result = coordination.run_once("shared-key", {str(shared_dir)!r}, fn)
with open(sys.argv[1], "w") as fh:
    fh.write(result)
"""
    )
    output_paths = [tmp_path / f"out-{i}.txt" for i in range(3)]
    procs = [
        subprocess.Popen([sys.executable, str(worker_script), str(out)]) for out in output_paths
    ]
    returncodes = [p.wait(timeout=30) for p in procs]
    assert all(rc == 0 for rc in returncodes), returncodes

    assert log_path.read_text().count("called") == 1, "fn() should have run exactly once"
    for out in output_paths:
        assert out.read_text() == "the-one-true-result"


def test_warden_run_once_fixture_executes_the_wrapped_function_exactly_once_across_workers(
    pytester,
):
    pytester.makeconftest(
        """
        import uuid

        def do_expensive_setup():
            with open("calls.log", "a") as fh:
                fh.write(str(uuid.uuid4()) + "\\n")
            return "shared-setup-result"
        """
    )
    pytester.makepyfile(
        test_mod="""
        from conftest import do_expensive_setup

        def test_a(warden_run_once):
            result = warden_run_once("shared_setup", do_expensive_setup)
            with open("observed_a.txt", "w") as fh:
                fh.write(result)

        def test_b(warden_run_once):
            result = warden_run_once("shared_setup", do_expensive_setup)
            with open("observed_b.txt", "w") as fh:
                fh.write(result)

        def test_c(warden_run_once):
            result = warden_run_once("shared_setup", do_expensive_setup)
            with open("observed_c.txt", "w") as fh:
                fh.write(result)
        """
    )
    # numprocesses=3 with 3 tests and no history degenerates to one test per
    # worker -- guarantees each call happens in a SEPARATE worker subprocess.
    result = pytester.runpytest("--warden", "--numprocesses=3")
    result.assert_outcomes(passed=3)

    # calls.log and the per-test output files are written under
    # pytester.path (the stable rootdir each worker's cwd is set to), NOT
    # under the forwarded warden_run_dir -- that TemporaryDirectory is
    # deleted when _run_controller returns, before this assertion runs.
    calls_log = (pytester.path / "calls.log").read_text().splitlines()
    assert len(calls_log) == 1, (
        f"do_expensive_setup should have run exactly once, got {calls_log!r}"
    )

    observed = {(pytester.path / f"observed_{name}.txt").read_text() for name in ("a", "b", "c")}
    assert observed == {"shared-setup-result"}


def test_warden_run_once_fixture_works_in_bare_mode_with_zero_contention(pytester):
    pytester.makeconftest(
        """
        def do_setup():
            with open("calls.log", "a") as fh:
                fh.write("called\\n")
            return "bare-result"
        """
    )
    pytester.makepyfile(
        """
        from conftest import do_setup

        def test_a(warden_run_once):
            assert warden_run_once("k", do_setup) == "bare-result"

        def test_b(warden_run_once):
            assert warden_run_once("k", do_setup) == "bare-result"
        """
    )
    result = pytester.runpytest()  # no --warden
    result.assert_outcomes(passed=2)
    assert (pytester.path / "calls.log").read_text().splitlines() == ["called"]


def test_windows_lock_only_references_real_msvcrt_attribute_names():
    # A strict fake msvcrt exposing ONLY the real module's actual constant
    # names (LK_LOCK/LK_UNLCK, not a typo like LK_UNLOCK) -- unlike a
    # MagicMock, referencing a wrong attribute name here raises
    # AttributeError immediately, exactly catching a typo that a
    # permissive mock would silently paper over. This is a real bug this
    # test would have caught before it ever reached Windows CI: the
    # _exclusive_lock finally-block referenced msvcrt.LK_UNLOCK, which
    # doesn't exist (the real constant is LK_UNLCK).
    #
    # Deliberately does NOT use monkeypatch for sys.platform/sys.modules --
    # fixture teardown order relative to a manual importlib.reload() inside
    # a plain try/finally is unclear enough to risk leaking Windows-mode
    # into later tests, so this restores both explicitly, in a known order,
    # itself.
    import importlib
    import sys
    import types

    real_platform = sys.platform
    had_msvcrt = "msvcrt" in sys.modules
    real_msvcrt = sys.modules.get("msvcrt")

    calls = []

    def fake_locking(fd, mode, nbytes):
        calls.append((mode, nbytes))

    fake_msvcrt = types.SimpleNamespace(LK_LOCK=1, LK_UNLCK=0, locking=fake_locking)

    import pytest_warden.coordination as coordination_module

    try:
        sys.modules["msvcrt"] = fake_msvcrt
        sys.platform = "win32"
        importlib.reload(coordination_module)

        class _FakeFile:
            def fileno(self):
                return 42

            def seek(self, pos):
                pass

        with coordination_module._exclusive_lock(_FakeFile()):
            pass

        assert calls == [(fake_msvcrt.LK_LOCK, 1), (fake_msvcrt.LK_UNLCK, 1)]
    finally:
        sys.platform = real_platform
        if had_msvcrt:
            sys.modules["msvcrt"] = real_msvcrt
        else:
            sys.modules.pop("msvcrt", None)
        importlib.reload(coordination_module)
