"""Local, mock-based supplement to the Windows Job Object branch of
jobobject.py -- NOT a replacement for real Windows coverage. That branch is
already exercised for real on Windows CI (.github/workflows/ci.yml runs the
full suite on windows-latest). These tests only run on macOS/Linux dev
machines where the real win32 APIs don't exist, and only prove that the
Windows-branch code calls the win32 functions it's supposed to, in the
order/with the arguments it claims to in its own comments -- a "did we wire
this up right" contract check, not a behavioral one.
"""

import importlib
import sys
import types
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def windows_jobobject_module(monkeypatch):
    fake_win32job = types.ModuleType("win32job")
    fake_win32job.CreateJobObject = MagicMock(return_value="job-handle")
    fake_win32job.QueryInformationJobObject = MagicMock(return_value={"BasicLimitInformation": {}})
    fake_win32job.SetInformationJobObject = MagicMock()
    fake_win32job.AssignProcessToJobObject = MagicMock()
    fake_win32job.TerminateJobObject = MagicMock()
    fake_win32job.JobObjectExtendedLimitInformation = "JobObjectExtendedLimitInformation"
    fake_win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

    fake_win32api = types.ModuleType("win32api")
    fake_win32api.OpenProcess = MagicMock(return_value="proc-handle")
    fake_win32api.CloseHandle = MagicMock()

    fake_win32con = types.ModuleType("win32con")
    fake_win32con.PROCESS_SET_QUOTA = 0x0100
    fake_win32con.PROCESS_TERMINATE = 0x0001

    fake_win32process = types.ModuleType("win32process")
    fake_win32process.TerminateProcess = MagicMock()

    monkeypatch.setitem(sys.modules, "win32job", fake_win32job)
    monkeypatch.setitem(sys.modules, "win32api", fake_win32api)
    monkeypatch.setitem(sys.modules, "win32con", fake_win32con)
    monkeypatch.setitem(sys.modules, "win32process", fake_win32process)
    monkeypatch.setattr(sys, "platform", "win32")

    import pytest_warden.jobobject as jobobject_module

    importlib.reload(jobobject_module)
    fake_subprocess_run = MagicMock()
    monkeypatch.setattr(jobobject_module.subprocess, "run", fake_subprocess_run)
    try:
        yield (
            jobobject_module,
            fake_win32job,
            fake_win32api,
            fake_win32con,
            fake_win32process,
            fake_subprocess_run,
        )
    finally:
        # Restore the real (POSIX, on this dev machine) branch so no other
        # test in this process sees a module stuck in fake-Windows mode --
        # monkeypatch undoes sys.modules/sys.platform automatically, but it
        # does NOT undo the in-place `importlib.reload` mutation of the
        # already-imported jobobject module object, so that has to be
        # reloaded back explicitly.
        importlib.reload(jobobject_module)


def test_windows_job_object_assign_then_terminate_calls_expected_win32_apis_in_order(
    windows_jobobject_module,
):
    jobobject_module, fake_win32job, fake_win32api, _, fake_win32process, fake_subprocess_run = (
        windows_jobobject_module
    )
    job = jobobject_module.JobObject()

    fake_win32job.CreateJobObject.assert_called_once()
    fake_win32job.SetInformationJobObject.assert_called_once()
    limit_flags = fake_win32job.SetInformationJobObject.call_args[0][2]["BasicLimitInformation"][
        "LimitFlags"
    ]
    assert limit_flags == fake_win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE

    job.assign(12345)
    fake_win32api.OpenProcess.assert_called_once()
    fake_win32job.AssignProcessToJobObject.assert_called_once_with("job-handle", "proc-handle")

    job.terminate()
    fake_win32job.TerminateJobObject.assert_called_once_with("job-handle", 1)
    # Direct-process fallback: terminate() also targets the retained
    # process handle by itself, independent of job-based tree termination.
    fake_win32process.TerminateProcess.assert_called_once_with("proc-handle", 1)
    # Third independent fallback: taskkill /F /T by PID, which doesn't
    # depend on Job Object membership/nesting at all -- confirmed on real
    # Windows CI to be what actually made the kill take effect when both
    # win32 calls above reported success but the process kept running.
    taskkill_args = fake_subprocess_run.call_args[0][0]
    assert taskkill_args == ["taskkill", "/F", "/T", "/PID", "12345"]

    job.close()
    # Both the retained process handle and the job handle are closed now
    # (the process handle used to be leaked -- opened in assign() and never
    # retained or closed at all).
    assert fake_win32api.CloseHandle.call_args_list == [
        (("proc-handle",), {}),
        (("job-handle",), {}),
    ]


def test_windows_job_object_assign_uses_least_privilege_access_rights(windows_jobobject_module):
    jobobject_module, _, fake_win32api, fake_win32con, _, _ = windows_jobobject_module
    job = jobobject_module.JobObject()
    job.assign(999)

    args, kwargs = fake_win32api.OpenProcess.call_args
    access = args[0] if args else kwargs["access"]
    expected = fake_win32con.PROCESS_SET_QUOTA | fake_win32con.PROCESS_TERMINATE
    assert access == expected, (
        f"expected least-privilege access rights {expected!r}, got {access!r} -- "
        f"broader access than PROCESS_SET_QUOTA | PROCESS_TERMINATE would be an "
        f"unnecessary escalation for a worker-supervision handle"
    )
