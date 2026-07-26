import subprocess
import time

from conftest import process_exists

from pytest_warden.jobobject import JobObject


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
