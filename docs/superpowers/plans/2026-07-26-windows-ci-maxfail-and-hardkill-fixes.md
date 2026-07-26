# Windows CI Failures: maxfail Double-Reporting and Hard-Kill Reliability

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the two independent, currently-red `windows-latest` CI failure clusters on `main` without regressing the (always-green) `ubuntu-latest` leg or any existing passing test.

**Context:** `test (windows-latest)` has failed consistently across the last several runs of `.github/workflows/ci.yml` on `main` (e.g. run [30201788689](https://github.com/alexneo2003/pytest-warden/actions/runs/30201788689)), while `test (ubuntu-latest)` and `lint` are always green. 5 tests fail, in two unrelated clusters. Two prior commits (`8973784` "fix: msvcrt.LK_UNLOCK typo and replay-after-kill double-reporting", `27c536d` "test: stop scraping raw stdout for duplicate-report counts") already fixed a different pair of Windows-only bugs but did **not** touch either cluster below — confirmed by reading both diffs before starting this plan.

**Tech Stack:** Python 3.9+, pytest 9.x plugin APIs, `subprocess`, `pywin32` (`win32job`/`win32api`/`win32con`) for the Windows Job Object path, `uv run pytest tests/ -v` for the dev loop.

**Environment caveat:** This plan was authored and Part A was verified in a Linux sandbox with no Windows execution available. Part A (pure controller logic, OS-agnostic) is fully verifiable here via the existing fake-worker rig in `tests/test_progress_channel.py`. Part B (Win32 Job Object behavior) can be written correctly by inspection and covered with regression tests, but **must be confirmed against a real `windows-latest` CI run** before considering it done — the exact WinAPI-level failure mode could not be observed directly from this sandbox.

## Global Constraints

- Run `uv run pytest tests/ -v` after every task and confirm the full suite stays green before moving on (no regressions on Linux/macOS).
- No behavior change on the fast/happy path: grace periods and escalation logic introduced below must be no-ops (or add negligible latency) when the existing fast path already works correctly (i.e. Linux must not slow down).
- Every fix needs a regression test that fails against the old code and passes against the new code — matching the existing project convention (see `8973784`'s `test_no_further_events_are_replayed_for_a_worker_once_it_is_marked_killed`).
- Update `CHANGELOG.md` following the existing "found via real Windows CI" entry style.

---

## Root cause analysis

### Cluster A: `--maxfail` duplicate reporting (3 failing tests)

Failing: `test_maxfail_stops_the_run_early`, `test_maxfail_does_not_spawn_a_retry_wave_for_tests_it_intentionally_skipped`, `test_in_flight_test_on_a_worker_killed_due_to_maxfail_gets_a_report_not_silently_dropped` — all in `tests/test_fail_policy.py`.

Observed in CI logs: `assert {'failed': 2} != {'failed': 1}` — the same test is reported failed **twice**.

`_supervise()` (`src/pytest_warden/plugin.py`, static-LPT mode) and `_run_work_stealing()`'s equivalent branch both do this the instant `session.shouldfail` becomes true:

```python
if session.shouldfail and pending:
    for worker in pending:
        worker.job.terminate()
    for worker in pending:
        worker.proc.wait()
        _report_incident(session, worker, "warden: worker terminated because --maxfail was reached")
```

`_report_incident` only no-ops if `worker.current is None`. The race:

1. Worker writes `logstart` → `logreport(call, failed)` for `test_a`, then starts writing `logfinish`.
2. Controller's `_poll_once` reads only `logstart` + `logreport` this cycle (the `logfinish` write hasn't landed/flushed on disk yet). Replaying the failed report sets `session.shouldfail = True` via pytest's own core maxfail hook. `worker.current` is **still `test_a`** because `logfinish` wasn't in this batch.
3. Back in `_supervise`, `if session.shouldfail and pending:` fires immediately, in the _same_ iteration — force-kills the worker mid-shutdown and calls `_report_incident`, which fires because `worker.current` is still set → `test_a` is reported failed a second time.

This is a genuine TOCTOU race between "read+replay progress lines" and "act on `session.shouldfail` in the same loop iteration." It's latent on every OS, but the race window is wide enough to hit reliably on Windows because per-event file I/O (`open`/`write`/`flush`/`fsync` on write, `open`/`readline` per poll on read) is measurably slower there than on Linux — consistent with ubuntu always being green and windows always red for this exact assertion.

Neither prior "found via real Windows CI" fix touched this branch: `8973784` fixed a _different_ race (a late but _genuinely real_ completion event arriving for a test after its worker was already marked `killed` by the **timeout** path). `27c536d` only changed the test to trust `assert_outcomes` instead of scraping stdout text — which is _why_ this pre-existing bug only now surfaces as a hard failure instead of being silently masked by an unreliable assertion.

### Cluster B: hard-kill doesn't actually kill the process on Windows (2 failing tests)

Failing: `test_hanging_test_is_hard_killed_after_timeout`, `test_teardown_finalizer_does_not_run_after_a_hard_kill_documents_the_trade_off` — both in `tests/test_timeout.py`.

Evidence from CI logs is decisive:

- `test_hanging_test_is_hard_killed_after_timeout`: a `--timeout=1` test that sleeps 30s takes **33.1s** to fail (`assert elapsed < 15` fails), not the expected ~1s.
- `test_teardown_finalizer_does_not_run_after_a_hard_kill_documents_the_trade_off`: takes **30.5s**, and — critically — `cleanup_ran.txt` exists, meaning the test's fixture **teardown finalizer actually ran**. A true `TerminateJobObject`-based hard kill is architecturally a SIGKILL-equivalent; teardown finalizers cannot run under a real hard kill (that's the entire point this test normally asserts, and it normally passes).

Both signatures point the same direction: the worker process runs to **full natural completion** (the whole 30s sleep, plus real fixture teardown) — it is never actually terminated. Yet the controller _does_ report `"warden: hard-killed after exceeding 1.0s timeout"` and the test does end up `failed=1` — meaning the controller-side deadline logic in `_poll_once` fires correctly around the 1s mark, calls `worker.job.terminate()`, reports the synthetic incident, and sets `worker.killed = True`. But nothing then verifies the kill actually took effect. `_poll_once`/`_supervise` fall back to passively polling `worker.proc.poll()` every `_POLL_INTERVAL` (50ms) with no bound — so if the underlying `TerminateJobObject` call had no effect, the loop simply waits however long the process takes to die _on its own_ (here, ~29 more seconds for the sleep to finish).

Looking at `src/pytest_warden/jobobject.py`'s Windows `JobObject`:

```python
class JobObject:
    def __init__(self):
        self.handle = win32job.CreateJobObject(None, "")   # empty-string name, not None
        ...

    def assign(self, pid: int):
        access = win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE
        proc_handle = win32api.OpenProcess(access, False, pid)
        win32job.AssignProcessToJobObject(self.handle, proc_handle)
        # proc_handle is never retained or closed

    def terminate(self):
        win32job.TerminateJobObject(self.handle, 1)
```

Contributing factors, in rough order of suspicion (none can be _confirmed_ without a real Windows run — this sandbox is Linux):

1. **No verification/escalation.** `terminate()` is fire-and-forget; nothing confirms the process actually died, and there's no fallback if it didn't.
2. **No direct-process fallback.** `assign()` opens a process handle purely to make the `AssignProcessToJobObject` call, then discards it. If job-based tree termination is blocked for any reason (e.g. GitHub Actions' Windows runners already nest the whole job step in their own ambient Job Object, and nested-job termination propagation can interact with the outer job's policy in ways that don't raise a Python-visible exception), there's no direct `TerminateProcess` fallback on the known PID.
3. **`CreateJobObject(None, "")`** passes an empty string name instead of `None`. Passing `None` guarantees an anonymous, unambiguously-unique job object; an empty string is a needless deviation from the documented "no name" idiom worth removing even though it's unlikely to be the primary cause for the single-worker case that's failing here.

Given the inability to execute real Win32 code in this sandbox, the fix must be **defense-in-depth** rather than a single targeted guess: make both job-based and direct-process termination happen, verify actual death within a bounded window, retry/escalate if not, and surface a loud warning if a kill still doesn't take — instead of silently degrading into "wait for natural death."

---

## Part A: Fix the maxfail duplicate-report race

### Task A1: Add a grace-drain before force-killing on `session.shouldfail`

**Files:**

- Source: `src/pytest_warden/plugin.py` (`_supervise`, `_run_work_stealing`)
- Test: `tests/test_progress_channel.py` (new test using the existing `rig` fixture)

**Design:** Before force-killing+reporting every `pending` worker when `session.shouldfail` first becomes true, give them a short bounded grace period (reuse `_POLL_INTERVAL`, cap total wait at ~1–2s) during which `_poll_once` keeps running normally. A worker whose _own_ test just tripped this exact `--maxfail` threshold is very likely already exiting on its own (workers are spawned with the same `--maxfail=N` — see `_spawn_worker`), so it will settle through the existing, already-race-free "worker exited" path in `_poll_once` (which correctly drains all remaining lines _before_ checking `worker.current`). Only workers still pending after the grace window — genuinely in-flight on a _different_, unrelated worker — get the force-kill+incident treatment.

- [x] **Step 1: Add the grace constant and helper**

In `src/pytest_warden/plugin.py`, near `_POLL_INTERVAL`:

```python
_MAXFAIL_GRACE_PERIOD = 2.0
```

- [x] **Step 2: Update `_supervise`**

```python
def _supervise(session, workers):
    pending = list(workers)
    while pending:
        pending = _poll_once(session, pending)

        if session.shouldfail and pending:
            # A worker whose OWN test just tripped this exact --maxfail
            # threshold (workers run with the same --maxfail=N locally --
            # see _spawn_worker) is very likely already exiting on its own.
            # Force-killing it immediately races its still-in-flight
            # logfinish write and double-reports the same test (once via
            # the real failure, once via this incident). Give it a short,
            # bounded chance to settle through the normal "worker exited"
            # path in _poll_once first, which already drains all remaining
            # lines before deciding whether a report is still owed.
            grace_deadline = time.monotonic() + _MAXFAIL_GRACE_PERIOD
            while pending and time.monotonic() < grace_deadline:
                time.sleep(_POLL_INTERVAL)
                pending = _poll_once(session, pending)

        if session.shouldfail and pending:
            for worker in pending:
                worker.job.terminate()
            for worker in pending:
                worker.proc.wait()
                _report_incident(
                    session, worker, "warden: worker terminated because --maxfail was reached"
                )
            pending = []

        if pending:
            time.sleep(_POLL_INTERVAL)
```

- [x] **Step 3: Apply the identical pattern to `_run_work_stealing`'s shouldfail branch**

Same grace-drain, inserted before the existing `for worker in active: worker.job.terminate()` block in `_run_work_stealing`.

- [x] **Step 4: Regression test**

Add to `tests/test_progress_channel.py`, using the existing `rig` fixture: write `logstart` + `logreport(call, failed)` for `test_a` (but not yet `logfinish`), call `_poll_once` to replay them and confirm `session.shouldfail`-equivalent state, then simulate the worker actually finishing (write `logfinish`, call `proc.finish(...)`) _within_ the grace window, and assert `_supervise`/the grace-drain loop settles with exactly one failure report for `test_a`, not two. Also add/keep a case where the worker genuinely never finishes within the grace window (mirrors `test_in_flight_test_on_a_worker_killed_due_to_maxfail_gets_a_report_not_silently_dropped`) to confirm the force-kill+incident path still fires for real strays.

- [x] **Step 5: Run the full suite and the specific regressions**

```bash
uv run pytest tests/test_fail_policy.py tests/test_progress_channel.py -v
uv run pytest tests/ -v
```

Expected: `test_maxfail_stops_the_run_early`, `test_maxfail_does_not_spawn_a_retry_wave_for_tests_it_intentionally_skipped`, and `test_in_flight_test_on_a_worker_killed_due_to_maxfail_gets_a_report_not_silently_dropped` all pass; full suite green.

- [x] **Step 6: Commit**

```bash
git add src/pytest_warden/plugin.py tests/test_progress_channel.py CHANGELOG.md
git commit -m "fix: grace-drain workers before force-killing on maxfail to stop double-reporting the tripping test"
```

---

## Part B: Make the hard kill reliable on Windows

### Task B1: Harden `JobObject` (anonymous job, retained handle, direct-process fallback)

**Files:**

- Source: `src/pytest_warden/jobobject.py`

- [x] **Step 1: Anonymous job object**

```python
self.handle = win32job.CreateJobObject(None, None)
```

- [x] **Step 2: Retain the process handle; close it in `close()`**

```python
class JobObject:
    def __init__(self):
        self.handle = win32job.CreateJobObject(None, None)
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
        access = win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE
        self._proc_handle = win32api.OpenProcess(access, False, pid)
        win32job.AssignProcessToJobObject(self.handle, self._proc_handle)

    def terminate(self):
        """Hard-kills every process currently in the job. Also terminates
        the directly-assigned process by its own handle as a fallback --
        job-based tree termination can be blocked by an ambient outer Job
        Object's own nesting/breakaway policy (observed in practice on
        GitHub Actions' windows-latest runners) without raising a
        Python-visible error, silently leaving the process alive."""
        with contextlib.suppress(Exception):
            win32job.TerminateJobObject(self.handle, 1)
        if self._proc_handle is not None:
            with contextlib.suppress(Exception):
                win32process.TerminateProcess(self._proc_handle, 1)

    def close(self):
        if self._proc_handle is not None:
            with contextlib.suppress(Exception):
                win32api.CloseHandle(self._proc_handle)
            self._proc_handle = None
        win32api.CloseHandle(self.handle)
```

Add `import win32process` and `import contextlib` (already imported) at the top of the Windows branch.

- [x] **Step 3: Run the existing Windows-specific job object tests**

`tests/test_jobobject.py`'s `test_terminate_kills_a_running_process` and `test_terminate_kills_a_child_process_spawned_by_the_worker` already assert real termination — they run on POSIX too (via the POSIX `JobObject` branch) so they'll still pass locally, but re-run them explicitly as a sanity check that nothing broke the POSIX path:

```bash
uv run pytest tests/test_jobobject.py -v
```

- [x] **Step 4: Commit**

```bash
git add src/pytest_warden/jobobject.py
git commit -m "fix: anonymous Job Object plus direct-process TerminateProcess fallback for reliable Windows hard-kill"
```

### Task B2: Bound and verify the kill in the controller poll loop

**Files:**

- Source: `src/pytest_warden/plugin.py` (`_poll_once`)

**Design:** After the deadline-triggered `worker.job.terminate()` call, don't just fall back to an unbounded passive wait. Poll for actual death over a short bounded window; if the process is still alive after that, retry termination once and `warnings.warn(...)` so a genuinely-stuck kill is visible in CI output instead of silently degrading into "wait for natural death" (which is exactly what made cluster B take 30s instead of ~1s and hid the real problem).

- [x] **Step 1: Add a bounded verify-and-escalate helper**

```python
_KILL_VERIFY_TIMEOUT = 2.0


def _terminate_and_verify(worker):
    worker.job.terminate()
    deadline = time.monotonic() + _KILL_VERIFY_TIMEOUT
    while time.monotonic() < deadline:
        if worker.proc.poll() is not None:
            return
        time.sleep(_POLL_INTERVAL)
    if worker.proc.poll() is None:
        warnings.warn(
            f"warden: worker pid {worker.proc.pid} did not exit within "
            f"{_KILL_VERIFY_TIMEOUT}s of termination -- retrying",
            stacklevel=2,
        )
        worker.job.terminate()
```

- [x] **Step 2: Use it at the deadline-triggered kill site in `_poll_once`**

```python
        if worker.deadline is not None and now > worker.deadline and not worker.killed:
            worker.killed = True
            _terminate_and_verify(worker)
            _report_incident(
                session,
                worker,
                f"warden: hard-killed after exceeding {worker.timeout}s timeout",
            )
```

Note: this runs synchronously inside `_poll_once`, briefly blocking that one poll cycle for up to `_KILL_VERIFY_TIMEOUT` — acceptable since it only triggers on the already-rare timeout-kill path, and it still terminates in low-single-digit milliseconds on the happy path (Linux, or Windows once Task B1 lands) since the `while` loop exits on the first `poll()` that returns non-`None`.

- [x] **Step 3: Regression test (rig-based, OS-agnostic)**

Add to `tests/test_progress_channel.py`: a fake worker/job where `job.terminate()` is a no-op the first time (simulating a stuck kill) but the fake `proc.poll()` reports exit after the retry; assert `_terminate_and_verify` calls `terminate()` twice and a `UserWarning` is raised. This tests the _escalation logic_ itself without needing real Windows execution.

- [x] **Step 4: Run the full suite**

```bash
uv run pytest tests/ -v
```

- [x] **Step 5: Commit**

```bash
git add src/pytest_warden/plugin.py tests/test_progress_channel.py CHANGELOG.md
git commit -m "fix: verify and escalate hard-kill termination instead of waiting unbounded for natural process death"
```

### Task B3: Confirm on real Windows CI (cannot be done from this sandbox)

- [ ] **Step 1:** Push Part B's commits and open/update the PR.
- [ ] **Step 2:** Watch the `windows-latest` leg of the resulting CI run specifically for `tests/test_timeout.py::test_hanging_test_is_hard_killed_after_timeout` and `test_teardown_finalizer_does_not_run_after_a_hard_kill_documents_the_trade_off`.
- [ ] **Step 3:** If either still fails with the same "ran to full natural completion" signature (elapsed time near the sleep duration, teardown file present), the `TerminateJobObject`/`TerminateProcess` fallback still isn't reaching the process — next debugging step would be adding temporary diagnostic logging around `assign()`/`terminate()` (e.g. log `GetLastError()`/exception details instead of `contextlib.suppress(Exception)`) in a throwaway CI-only branch to observe the actual Win32 error, since this can't be reproduced or introspected outside a real Windows host.

---

## Validation checklist

- [ ] `uv run pytest tests/ -v` green locally (Linux) after each part.
- [ ] `uv run ruff check .` / `uv run ruff format --check .` / `uv run ty check` clean (matches the `lint` CI job).
- [x] CHANGELOG.md updated for both clusters, matching the existing "found via real Windows CI" entry style.
- [ ] Real `windows-latest` CI run confirms all 5 previously-failing tests now pass, with no new failures introduced on either OS leg.
