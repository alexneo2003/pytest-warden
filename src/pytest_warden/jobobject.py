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

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    import win32api
    import win32con
    import win32job

    class JobObject:
        def __init__(self):
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

        def assign(self, pid: int):
            # Least privilege: AssignProcessToJobObject only needs the
            # rights to set quota/limits and to terminate the process, so
            # open the worker with just those instead of PROCESS_ALL_ACCESS.
            access = win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE
            proc_handle = win32api.OpenProcess(access, False, pid)
            win32job.AssignProcessToJobObject(self.handle, proc_handle)

        def terminate(self):
            """Hard-kills every process currently in the job."""
            win32job.TerminateJobObject(self.handle, 1)

        def close(self):
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
