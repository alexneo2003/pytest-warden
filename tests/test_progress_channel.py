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

import json
import types
import warnings

import pytest

from pytest_warden.plugin import (
    _ACTIVE_WORKERS,
    _poll_once,
    _read_new_lines,
    _supervise,
    _terminate_and_verify,
    _Worker,
)


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

    def wait(self, timeout=None):
        return self.returncode


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
    assert rig.worker.byte_offset > 0

    # Simulate truncation: the file is now shorter than byte_offset implies.
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


def test_torn_line_without_trailing_newline_is_not_returned_until_completed(rig):
    # The essential correctness property of the byte-offset rewrite: a
    # partially-written line (no trailing "\n" yet) must not be returned,
    # and byte_offset must not advance past it, so the SAME bytes are
    # re-read (this time complete) on a later poll.
    with open(rig.progress_path, "a", encoding="utf-8") as fh:
        fh.write('{"kind": "logstart", "nodeid": "test_a"')  # no trailing \n

    first = _read_new_lines(rig.worker)
    assert first == []
    assert rig.worker.byte_offset == 0

    with open(rig.progress_path, "a", encoding="utf-8") as fh:
        fh.write(', "location": ["m.py", 0, "test_a"]}\n')

    second = _read_new_lines(rig.worker)
    assert len(second) == 1
    assert json.loads(second[0])["nodeid"] == "test_a"


def test_read_new_lines_handles_multibyte_utf8_nodeid_without_corrupting_the_offset(rig):
    # A manually-arithmetic byte offset (offset + len(str)) would silently
    # desync from a real seek()/tell() cookie the moment a multi-byte UTF-8
    # character is involved -- this pins that the implementation doesn't do
    # that, by proving full, correct consumption with no desync/duplicate.
    _write_lines(rig.progress_path, ['{"kind": "logstart", "nodeid": "test_ééé"}'])
    first = _read_new_lines(rig.worker)
    assert len(first) == 1
    assert json.loads(first[0])["nodeid"] == "test_ééé"
    second = _read_new_lines(rig.worker)
    assert second == []


def test_no_further_events_are_replayed_for_a_worker_once_it_is_marked_killed(rig):
    # Hard-kill termination isn't guaranteed instant (Job Object/process-
    # group termination can take a moment on some platforms) -- if the
    # underlying process manages to write a legitimate completion event for
    # the SAME test after the kill was already reported, it must not be
    # replayed too (that would silently double-report the test: once via
    # the hard-kill incident, once via its own late "real" outcome).
    _write_lines(
        rig.progress_path,
        ['{"kind": "logstart", "nodeid": "test_a", "location": ["m.py", 0, "test_a"]}'],
    )
    _poll_once(rig.session, [rig.worker])
    assert rig.worker.current == {"nodeid": "test_a", "location": ["m.py", 0, "test_a"]}

    # Simulate the hard-kill having already fired (mirrors what
    # _poll_once's own deadline branch does: mark killed, clear current).
    rig.worker.killed = True
    rig.worker.current = None
    calls_before = list(rig.hook.calls)

    # A late-arriving, otherwise-legitimate completion for the SAME test.
    _write_lines(
        rig.progress_path,
        [
            '{"kind": "logreport", "data": {"outcome": "passed"}}',
            '{"kind": "logfinish", "nodeid": "test_a", "location": ["m.py", 0, "test_a"]}',
        ],
    )
    _poll_once(rig.session, [rig.worker])
    assert rig.hook.calls == calls_before, (
        f"events arriving after worker.killed=True must not be replayed -- "
        f"new calls appeared: {rig.hook.calls[len(calls_before) :]}"
    )

    # Same guard must also apply on the "worker has now exited" branch.
    rig.proc.finish(returncode=-9)
    _poll_once(rig.session, [rig.worker])
    assert rig.hook.calls == calls_before


def _maxfail_incident_reports(hook):
    return [
        call
        for call in hook.calls
        if call[0] == "logreport"
        and "worker terminated because --maxfail" in str(call[1]["report"].longrepr)
    ]


def test_worker_that_settles_within_the_maxfail_grace_period_is_not_double_reported(
    rig, monkeypatch
):
    # Reproduces the TOCTOU race: the poll that reads test_a's failing
    # logreport (which is what flips session.shouldfail, via pytest's own
    # maxfail hook in a real run) can land in the SAME cycle where
    # worker.current is still set, because the worker's logfinish write for
    # that same test hasn't hit disk yet. The old code force-killed and
    # reported an incident immediately in that situation, double-reporting
    # test_a. The fix must give the worker a brief window to actually exit
    # on its own first.
    _write_lines(
        rig.progress_path,
        ['{"kind": "logstart", "nodeid": "test_a", "location": ["m.py", 0, "test_a"]}'],
    )
    pending = _poll_once(rig.session, [rig.worker])
    assert pending == [rig.worker]
    assert rig.worker.current == {"nodeid": "test_a", "location": ["m.py", 0, "test_a"]}

    # Simulate session.shouldfail flipping true as a side effect of
    # replaying test_a's failed report (pytest's own maxfail hook does
    # this) in that exact poll -- while the worker's logfinish for test_a
    # is still in flight, not yet on disk.
    rig.session.shouldfail = True

    settled = False

    def fake_sleep(_seconds):
        nonlocal settled
        if not settled:
            settled = True
            _write_lines(
                rig.progress_path,
                ['{"kind": "logfinish", "nodeid": "test_a", "location": ["m.py", 0, "test_a"]}'],
            )
            rig.proc.finish(returncode=0)

    monkeypatch.setattr("pytest_warden.plugin.time.sleep", fake_sleep)

    _supervise(rig.session, pending)

    assert _maxfail_incident_reports(rig.hook) == [], (
        "worker settled on its own within the grace period -- test_a must not "
        "also get a synthetic maxfail incident report on top of its real failure"
    )


def test_worker_still_in_flight_after_the_maxfail_grace_period_is_still_force_killed_and_reported(
    rig, monkeypatch
):
    # Mirrors test_in_flight_test_on_a_worker_killed_due_to_maxfail_gets_a_report_not_silently_dropped:
    # a worker that is genuinely still running a DIFFERENT, unrelated test
    # when another worker trips --maxfail must still be force-killed and
    # reported once the grace period expires -- the grace period must not
    # turn into an unbounded wait or silently drop the report.
    monkeypatch.setattr("pytest_warden.plugin._MAXFAIL_GRACE_PERIOD", 0.05)
    monkeypatch.setattr("pytest_warden.plugin._POLL_INTERVAL", 0.01)

    _write_lines(
        rig.progress_path,
        ['{"kind": "logstart", "nodeid": "test_a", "location": ["m.py", 0, "test_a"]}'],
    )
    pending = _poll_once(rig.session, [rig.worker])
    rig.session.shouldfail = True

    # Worker never finishes on its own -- proc.poll() stays None throughout.
    _supervise(rig.session, pending)

    reports = _maxfail_incident_reports(rig.hook)
    assert len(reports) == 1, (
        f"expected exactly one maxfail incident report for the stray in-flight "
        f"worker, got {len(reports)}"
    )


class _StuckJob:
    """job.terminate() never actually kills anything by itself -- the fake
    proc below only reports exit once terminate() has been called twice,
    simulating a first TerminateJobObject/TerminateProcess call that
    silently has no effect (the scenario that made cluster B's hard-kill
    take ~30s -- the full sleep duration -- instead of ~1s on Windows)."""

    def __init__(self):
        self.terminate_calls = 0

    def terminate(self):
        self.terminate_calls += 1


class _StuckProc:
    pid = 4242

    def __init__(self, dies_after_n_terminates):
        self._dies_after = dies_after_n_terminates
        self.job = None  # set by the test after construction

    def poll(self):
        if self.job.terminate_calls >= self._dies_after:
            return 1
        return None


def test_terminate_and_verify_retries_and_warns_when_the_first_kill_does_not_take(
    monkeypatch,
):
    monkeypatch.setattr("pytest_warden.plugin._KILL_VERIFY_TIMEOUT", 0.05)
    monkeypatch.setattr("pytest_warden.plugin._POLL_INTERVAL", 0.01)

    job = _StuckJob()
    proc = _StuckProc(dies_after_n_terminates=999)  # never actually dies here
    proc.job = job
    worker = types.SimpleNamespace(job=job, proc=proc)

    with pytest.warns(UserWarning, match="did not exit within"):
        _terminate_and_verify(worker)

    assert job.terminate_calls == 2, (
        f"expected one initial terminate() plus one retry after the verify "
        f"window expired, got {job.terminate_calls} call(s)"
    )


def test_terminate_and_verify_does_not_warn_or_retry_once_the_process_actually_exits(
    monkeypatch,
):
    monkeypatch.setattr("pytest_warden.plugin._KILL_VERIFY_TIMEOUT", 2.0)
    monkeypatch.setattr("pytest_warden.plugin._POLL_INTERVAL", 0.01)

    job = _StuckJob()
    proc = _StuckProc(dies_after_n_terminates=1)  # dies right after the first terminate()
    proc.job = job
    worker = types.SimpleNamespace(job=job, proc=proc)

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _terminate_and_verify(worker)

    assert job.terminate_calls == 1, "the happy path must not retry once poll() confirms death"
