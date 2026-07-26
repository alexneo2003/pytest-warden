"""Controller-side robustness of the progress channel: _poll_once /
_read_new_lines / _replay_event against malformed, truncated, or
out-of-order input. These are semi-unit tests -- a real _Worker instance
pointed at a tmp_path file the test writes directly, bypassing real worker
subprocess timing entirely -- because the property under test is the
CONTROLLER's own parsing robustness, not real worker behavior, and
reliably forcing a real worker to emit a specific malformed byte sequence
at a specific instant isn't a stable, non-flaky thing to engineer via
subprocess timing.
"""

import types

import pytest

from pytest_warden.plugin import _ACTIVE_WORKERS, _poll_once, _read_new_lines, _Worker


class _FakeProc:
    """poll() returns None (still running) unless .finish() is called."""

    def __init__(self):
        self.returncode = None
        self._finished = False

    def finish(self, returncode=0):
        self._finished = True
        self.returncode = returncode

    def poll(self):
        return self.returncode if self._finished else None


class _FakeJob:
    def terminate(self):
        pass

    def close(self):
        pass


class _FakeHook:
    def __init__(self):
        self.calls = []

    def pytest_runtest_logstart(self, **kwargs):
        self.calls.append(("logstart", kwargs))

    def pytest_runtest_logreport(self, **kwargs):
        self.calls.append(("logreport", kwargs))

    def pytest_runtest_logfinish(self, **kwargs):
        self.calls.append(("logfinish", kwargs))


@pytest.fixture
def rig(tmp_path):
    progress_path = tmp_path / "worker-0.jsonl"
    progress_path.touch()
    proc = _FakeProc()
    worker = _Worker(
        proc=proc,
        job=_FakeJob(),
        progress_path=str(progress_path),
        timeout=None,
        batch=["test_a", "test_b"],
    )
    hook = _FakeHook()
    session = types.SimpleNamespace(config=types.SimpleNamespace(hook=hook))
    yield types.SimpleNamespace(
        worker=worker, session=session, hook=hook, progress_path=progress_path, proc=proc
    )
    _ACTIVE_WORKERS.clear()


def _write_lines(path, lines):
    with open(path, "a", encoding="utf-8") as fh:
        for line in lines:
            fh.write(line + "\n")


def test_malformed_json_line_does_not_abort_the_entire_run(rig):
    _write_lines(
        rig.progress_path,
        [
            '{"kind": "logstart", "nodeid": "test_a", "location": ["m.py", 0, "test_a"]}',
            "this is not json at all",
        ],
    )
    # Must not raise -- a single corrupt line must not abort the whole
    # controller loop out from under every other in-flight worker.
    with pytest.warns(UserWarning, match="unparseable progress-channel line"):
        _poll_once(rig.session, [rig.worker])


def test_malformed_json_line_is_skipped_and_later_valid_lines_still_replay(rig):
    _write_lines(
        rig.progress_path,
        [
            '{"kind": "logstart", "nodeid": "test_a", "location": ["m.py", 0, "test_a"]}',
            "this is not json at all",
        ],
    )
    with pytest.warns(UserWarning, match="unparseable progress-channel line"):
        _poll_once(rig.session, [rig.worker])
    assert ("logstart", {"nodeid": "test_a", "location": ("m.py", 0, "test_a")}) in rig.hook.calls

    _write_lines(
        rig.progress_path,
        ['{"kind": "logfinish", "nodeid": "test_a", "location": ["m.py", 0, "test_a"]}'],
    )
    _poll_once(rig.session, [rig.worker])
    assert (
        "logfinish",
        {"nodeid": "test_a", "location": ("m.py", 0, "test_a")},
    ) in rig.hook.calls


def test_progress_line_missing_required_kind_key_does_not_abort_the_run(rig):
    _write_lines(rig.progress_path, ['{"nodeid": "test_a"}'])
    with pytest.warns(UserWarning, match="unparseable progress-channel line"):
        _poll_once(rig.session, [rig.worker])


def test_progress_line_with_an_unrecognized_kind_is_silently_ignored_not_an_error(rig):
    # Distinct from a MISSING "kind" key: an unrecognized-but-present kind
    # already falls through _replay_event's if/elif chain with no `else`,
    # which is arguably intentional forward-compatibility -- confirm it
    # doesn't raise either.
    _write_lines(rig.progress_path, ['{"kind": "something_from_the_future"}'])
    _poll_once(rig.session, [rig.worker])
    assert rig.hook.calls == []


def test_truncated_progress_file_between_polls_does_not_crash(rig):
    _write_lines(
        rig.progress_path,
        [
            '{"kind": "logstart", "nodeid": "test_a", "location": ["m.py", 0, "test_a"]}',
            '{"kind": "logfinish", "nodeid": "test_a", "location": ["m.py", 0, "test_a"]}',
        ],
    )
    first = _read_new_lines(rig.worker)
    assert len(first) == 2
    assert rig.worker.lines_consumed == 2

    # Simulate truncation: the file is now shorter than lines_consumed implies.
    rig.progress_path.write_text("", encoding="utf-8")
    second = _read_new_lines(rig.worker)
    assert second == []


def test_duplicate_logstart_event_for_the_same_nodeid_does_not_corrupt_worker_state(rig):
    _write_lines(
        rig.progress_path,
        [
            '{"kind": "logstart", "nodeid": "test_a", "location": ["m.py", 0, "test_a"]}',
            '{"kind": "logstart", "nodeid": "test_a", "location": ["m.py", 0, "test_a"]}',
        ],
    )
    _poll_once(rig.session, [rig.worker])
    assert rig.worker.started_ids == {"test_a"}
    assert rig.worker.current == {"nodeid": "test_a", "location": ["m.py", 0, "test_a"]}


def test_logfinish_without_a_prior_logstart_does_not_raise(rig):
    _write_lines(
        rig.progress_path,
        ['{"kind": "logfinish", "nodeid": "test_a", "location": ["m.py", 0, "test_a"]}'],
    )
    _poll_once(rig.session, [rig.worker])
    assert rig.worker.current is None
