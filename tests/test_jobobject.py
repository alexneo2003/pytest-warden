import os
import subprocess
import time

import pytest

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
    proc = subprocess.Popen(
        [
            "python3",
            "-c",
            "import subprocess, time\n"
            "child = subprocess.Popen(['sleep', '30'])\n"
            "print(child.pid, flush=True)\n"
            "time.sleep(30)\n",
        ],
        start_new_session=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    child_pid = int(proc.stdout.readline().strip())

    job = JobObject()
    job.assign(proc.pid)
    job.terminate()
    proc.wait(timeout=5)

    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)
