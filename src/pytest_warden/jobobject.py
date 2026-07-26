"""
Windows Job Objects give us what pytest-timeout's thread-mode kill cannot:
a guaranteed kill of the *entire* process tree (worker interpreter +
Chromium + any Node helper processes a test spawns), even if the worker
is stuck mid-teardown holding stdout/stderr handles.

Any process created by a process that's already assigned to the Job
Object automatically becomes part of the job (unless it explicitly uses
CREATE_BREAKAWAY_FROM_JOB), so as long as we assign the worker's PID to
the job *before* it does anything else, terminating the job kills
everything underneath it in one shot.

Vendored from ctrlrunner's src/ctrlrunner/execution/jobobject.py, with no
import dependency on that project. Adapted for pytest-warden's own spawn
mechanism (subprocess.Popen(..., start_new_session=True)) rather than
ctrlrunner's multiprocessing.Process worker model.
"""

import contextlib
import sys
import warnings

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    import win32api
    import win32con
    import win32job
    import win32process

    class JobObject:
        def __init__(self):
            # An empty string, not None, is the correct pywin32 idiom for
            # an anonymous job object here -- pywin32's CreateJobObject
            # binding requires the name argument to be a string (raises
            # TypeError on None), even though the underlying WinAPI accepts
            # a NULL name pointer. Confirmed empirically on real Windows;
            # this isn't the "needless deviation" it looks like from the
            # WinAPI docs alone.
            self.handle = win32job.CreateJobObject(None, "")
            info = win32job.QueryInformationJobObject(
                self.handle, win32job.JobObjectExtendedLimitInformation
            )
            info["BasicLimitInformation"]["LimitFlags"] = (
                win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
            win32job.SetInformationJobObject(
                self.handle, win32job.JobObjectExtendedLimitInformation, info
            )
            self._proc_handle = None

        def assign(self, pid: int):
            # Least privilege: AssignProcessToJobObject only needs the
            # rights to set quota/limits and to terminate the process, so
            # open the worker with just those instead of PROCESS_ALL_ACCESS.
            # The handle is retained (not just used-and-discarded) so
            # terminate() below has a direct fallback on the worker's own
            # PID, independent of Job Object tree termination.
            access = win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE
            self._proc_handle = win32api.OpenProcess(access, False, pid)
            win32job.AssignProcessToJobObject(self.handle, self._proc_handle)

        def terminate(self):
            """Hard-kills every process currently in the job. Also
            terminates the directly-assigned process by its own handle as a
            fallback -- job-based tree termination can be blocked by an
            ambient outer Job Object's own nesting/breakaway policy
            (observed in practice on GitHub Actions' windows-latest
            runners) without raising a Python-visible error, silently
            leaving the process alive."""
            # TEMPORARY diagnostic: real Windows CI shows every single kill
            # in the suite needing all 3 verify/retry attempts in
            # _terminate_and_verify, with two specific tests
            # (single-worker, no children, pure time.sleep(30)) still never
            # actually dying within the full 30s sleep -- yet other, more
            # complex hard-kill tests (multi-worker, child-spawning) in the
            # same run recover and pass. That split isn't explained by the
            # "swallowed by an ambient job's breakaway policy" theory alone
            # -- surface the actual Win32 error/exception instead of
            # silently suppressing it, to find out what's really happening
            # in this specific shape of failure. Remove once understood.
            try:
                win32job.TerminateJobObject(self.handle, 1)
            except Exception as exc:
                warnings.warn(
                    f"warden: TEMP DIAGNOSTIC -- TerminateJobObject failed: {exc!r}",
                    stacklevel=2,
                )
            if self._proc_handle is not None:
                try:
                    win32process.TerminateProcess(self._proc_handle, 1)
                except Exception as exc:
                    warnings.warn(
                        f"warden: TEMP DIAGNOSTIC -- TerminateProcess failed: {exc!r}",
                        stacklevel=2,
                    )

        def close(self):
            if self._proc_handle is not None:
                with contextlib.suppress(Exception):
                    win32api.CloseHandle(self._proc_handle)
                self._proc_handle = None
            win32api.CloseHandle(self.handle)

else:
    # POSIX fallback (dev/testing off Windows): real process-group kill.
    # Not used in the CI path this project targets, but keeps the engine
    # runnable/testable on non-Windows machines.
    import os
    import signal

    class JobObject:
        def __init__(self):
            self._pid = None

        def assign(self, pid: int):
            # Workers are spawned with subprocess.Popen(...,
            # start_new_session=True), which makes each worker its own
            # process-group leader (pgid == pid) before it execs anything
            # -- unlike a fork-based multiprocessing worker, there's no
            # "already exec'd" race here, so simply recording the pid is
            # enough: terminate() below targets that same pgid.
            self._pid = pid

        def terminate(self):
            if self._pid is None:
                return
            try:
                os.killpg(self._pid, signal.SIGKILL)
            except ProcessLookupError:
                pass  # already gone -- nothing left to kill
            except PermissionError:
                # Killed extremely early, before the process group was
                # fully established, or unsupported in this sandboxed
                # environment -- fall back to killing just the leader
                # rather than raising out of a hard-kill path.
                with contextlib.suppress(ProcessLookupError):
                    os.kill(self._pid, signal.SIGKILL)

        def close(self):
            pass
