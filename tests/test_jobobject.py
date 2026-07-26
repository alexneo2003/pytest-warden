import subprocess
import time
import types

from conftest import process_exists

from pytest_warden.jobobject import JobObject
from pytest_warden.plugin import _read_new_lines


def test_terminate_kills_a_running_process():
    proc = subprocess.Popen(
        ["python3", "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    job = JobObject()
    job.assign(proc.pid)

    job.terminate()
    proc.wait(timeout=5)

    assert proc.returncode is not None
    assert proc.returncode != 0


def test_terminate_kills_a_child_process_spawned_by_the_worker():
    # The worker process must not spawn its own child until AFTER it's
    # already a member of the Job Object. On POSIX this doesn't matter --
    # process groups are inherited automatically at fork time regardless of
    # any Job-Object-style assignment -- but Windows' "processes created by
    # an already-assigned process automatically join the job" rule only
    # applies going forward from the moment of assignment: a grandchild
    # spawned *before* its parent was assigned never joins. Real usage
    # (plugin.py's _spawn_worker) always assigns immediately after Popen
    # returns, before the worker has done anything else -- this test
    # synchronizes via stdin to guarantee the same ordering instead of
    # racing it, since letting the child print its own grandchild's pid
    # first (to identify it) would otherwise require the grandchild to
    # already exist before we could call job.assign().
    proc = subprocess.Popen(
        [
            "python3",
            "-c",
            "import subprocess, sys, time\n"
            "sys.stdin.readline()\n"  # wait for the parent's go-ahead
            "child = subprocess.Popen(['sleep', '30'])\n"
            "print(child.pid, flush=True)\n"
            "time.sleep(30)\n",
        ],
        start_new_session=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )

    job = JobObject()
    job.assign(proc.pid)  # happens-before the child's own Popen call below

    proc.stdin.write("go\n")
    proc.stdin.flush()
    child_pid = int(proc.stdout.readline().strip())

    job.terminate()
    proc.wait(timeout=5)

    time.sleep(0.2)
    assert not process_exists(child_pid)


def test_progress_lines_round_trip_correctly_regardless_of_platform_newline_translation(tmp_path):
    # worker.py's _write and plugin.py's _read_new_lines both open the
    # progress file in default text mode (no `newline=` override), which
    # means Python's own universal-newline translation applies on both
    # ends: on write, "\n" becomes os.linesep (== "\r\n" on Windows); on
    # read, any of "\r\n"/"\r"/"\n" is normalized back to "\n". This
    # simulates the Windows write path directly (writing "\r\n" bytes) to
    # confirm the read side still normalizes correctly and the
    # line.endswith("\n") completeness check still passes, without needing
    # an actual Windows machine.
    progress_path = tmp_path / "worker-0.jsonl"
    with open(progress_path, "wb") as fh:
        fh.write(b'{"kind": "logstart", "nodeid": "test_a"}\r\n')
        fh.write(b'{"kind": "logfinish", "nodeid": "test_a"}\r\n')

    worker = types.SimpleNamespace(progress_path=str(progress_path), lines_consumed=0)
    lines = _read_new_lines(worker)

    assert len(lines) == 2
    assert all("\r" not in line for line in lines), f"embedded \\r survived normalization: {lines}"
    import json

    assert json.loads(lines[0])["nodeid"] == "test_a"
    assert worker.lines_consumed == 2
