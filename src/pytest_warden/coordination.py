"""Cross-worker "run this exactly once for the whole invocation" primitive.

Each warden worker is a fully independent pytest subprocess, so there's no
in-process shared state to coordinate through (unlike pytest-xdist's
worker_id-fixture world, where workers are threads/processes forked from a
single controlling run). This uses a real OS-level file lock (fcntl.flock /
msvcrt.locking) plus a cached JSON result file under a shared directory, so
concurrent callers genuinely block on the lock rather than racing or
busy-polling for the result.
"""

import contextlib
import json
import os
import sys
import time

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    import msvcrt

    @contextlib.contextmanager
    def _exclusive_lock(fh):
        # msvcrt.locking(..., LK_LOCK, 1) blocks internally but gives up
        # after ~10s of contention (its own documented behavior) rather
        # than blocking indefinitely -- this retry loop compensates for
        # that ceiling so a still-running fn() elsewhere doesn't cause a
        # spurious failure here. It retries acquisition of the OS lock
        # itself; it does not poll the result file.
        deadline = time.monotonic() + 120
        while True:
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
        try:
            yield
        finally:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    @contextlib.contextmanager
    def _exclusive_lock(fh):
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)  # genuinely blocks, no polling
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def run_once(key, base_dir, fn):
    """Run fn() exactly once across every caller sharing base_dir with the
    same key, returning its (JSON-serializable) result to every caller,
    including the one that actually ran fn()."""
    os.makedirs(base_dir, exist_ok=True)
    lock_path = os.path.join(base_dir, f"{key}.lock")
    result_path = os.path.join(base_dir, f"{key}.result.json")

    with open(lock_path, "a+") as lock_fh, _exclusive_lock(lock_fh):
        if os.path.exists(result_path):
            with open(result_path, encoding="utf-8") as rf:
                return json.load(rf)["result"]
        result = fn()
        tmp_path = f"{result_path}.tmp-{os.getpid()}"
        with open(tmp_path, "w", encoding="utf-8") as wf:
            json.dump({"result": result}, wf)
        os.replace(tmp_path, result_path)  # atomic on the same filesystem
        return result
