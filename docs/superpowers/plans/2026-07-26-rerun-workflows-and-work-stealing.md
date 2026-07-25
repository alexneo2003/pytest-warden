# Rerun Workflows and Work-Stealing Scheduling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Confirm and harden `--last-failed`/`--failed-first` support, and add an opt-in work-stealing scheduler as an alternative to warden's static LPT batching for suites where upfront duration estimates aren't good enough.

**Architecture:** Part A verifies that pytest's own cache-based rerun machinery already works transparently through warden's real-hook-replay design (it should, and the plan explains why), then adds one small scheduling refinement so `--failed-first`'s intent survives LPT's own reordering. Part B extracts the existing per-worker polling loop into a reusable primitive, then builds a chunk-based work-stealing scheduler on top of it as a new, explicitly opt-in mode (`--warden-work-stealing`) alongside the existing static-LPT mode — not a replacement for it.

**Tech Stack:** Python 3.9+, pytest 9.x plugin APIs (`_pytest.cacheprovider`, `_pytest.reports.TestReport`), `subprocess`, `uv`/`pytest` for the dev loop. No new third-party dependencies.

## Global Constraints

- Every new behavior gets a real subprocess-level test (`pytester` fixture, real `pytest --warden ...` invocations) — no mocking `subprocess.Popen` or fabricating fake reports where a real run can be exercised instead, matching every prior phase of this project.
- No dependency on any other project's source (nothing changes about that).
- Existing flags/behavior (`--numprocesses`, `--timeout`, `--maxfail`, `--cov`, `--warden-history-db`, `--warden-quarantine-flaky`, and static LPT batching) must keep passing unmodified — run `uv run pytest tests/ -v` after every task and confirm the full suite (31+ tests) is still green before moving on.
- New options follow the existing naming pattern: plugin-specific flags are prefixed `--warden-*`; flags that mirror real pytest/xdist/pytest-timeout options (like `--numprocesses`, `--timeout`) are NOT prefixed, matching what's already there.
- Lowercase short options (`-x`, `-n`, etc.) cannot be registered by this plugin — pytest 9.x reserves them for core. Every new flag in this plan is long-form only.

---

## Part A: `--last-failed` / `--failed-first`

### Background (why this is a verification task, not a build-from-scratch task)

pytest's own cache plugin (`_pytest/cacheprovider.py`, class `LFPlugin`, always registered — it's core, not optional) does three things relevant here:

1. `pytest_collection_modifyitems` — reorders/deselects `session.items` based on `--lf`/`--ff` and the on-disk cache, **during normal collection**, which warden's controller never touches (it only overrides `pytest_runtestloop`, which runs strictly after collection).
2. `pytest_runtest_logreport` — updates its internal `lastfailed` bookkeeping as reports arrive. warden's controller already calls `session.config.hook.pytest_runtest_logreport(report=report)` for every real and synthetic result (`src/pytest_warden/plugin.py:378`, `:403`, `:424`), so `LFPlugin`'s hookimpl fires exactly as it would in a normal run.
3. `pytest_sessionfinish` — persists the cache to `.pytest_cache/v/cache/lastfailed`. warden never overrides `pytest_sessionfinish`, so this fires normally once `pytest_runtestloop` returns `True`.

Because `_run_controller` reads its node id list from `session.items` (`plugin.py:214`) — the *already-filtered-and-reordered* list — `--lf` deselection and `--ff` reordering should already be reflected with zero code changes, the same way `-k`/`-m` passthrough and exit codes turned out to be free in earlier phases. Part A's job is to prove that with real tests, not assume it.

### Task A1: Verify `--last-failed` reruns only the failed test

**Files:**
- Test: `tests/test_rerun_workflows.py`

**Interfaces:**
- Consumes: `pytester` fixture (already used throughout `tests/`), no new production code.

- [ ] **Step 1: Write the test**

```python
def test_last_failed_reruns_only_the_previously_failed_test(pytester):
    pytester.makepyfile(
        test_mod="""
        def test_ok():
            assert True

        def test_bad():
            assert False
        """
    )
    first = pytester.runpytest("--warden")
    first.assert_outcomes(passed=1, failed=1)

    second = pytester.runpytest("--warden", "--last-failed")
    second.assert_outcomes(failed=1)
    second.stdout.fnmatch_lines(["*1 deselected*"])
```

- [ ] **Step 2: Run it**

Run: `cd /Users/oleksii.galagan/Documents/projects/alex_neo/pytest-warden && uv run pytest tests/test_rerun_workflows.py -v`
Expected: **PASS**, immediately, with no production code changes. If it fails, that's new information — stop and diagnose before continuing to Task A2 (likely culprits: `session.items` isn't what `_run_controller` thinks it is post-collection, or a synthetic report from `_report_incident`/`_report_never_ran` isn't shaped the way `LFPlugin` expects — check `report.when`/`report.outcome` on those two functions in `plugin.py` first).

- [ ] **Step 3: Commit**

```bash
cd /Users/oleksii.galagan/Documents/projects/alex_neo/pytest-warden
git add tests/test_rerun_workflows.py
git commit -m "test: verify --last-failed works transparently through warden"
```

### Task A2: Verify `--failed-first` runs everything, failed test included

**Files:**
- Test: `tests/test_rerun_workflows.py` (append)

- [ ] **Step 1: Write the test**

```python
def test_failed_first_still_runs_everything(pytester):
    pytester.makepyfile(
        test_mod="""
        def test_a():
            assert True

        def test_b():
            assert False

        def test_c():
            assert True
        """
    )
    first = pytester.runpytest("--warden")
    first.assert_outcomes(passed=2, failed=1)

    second = pytester.runpytest("--warden", "--failed-first")
    second.assert_outcomes(passed=2, failed=1)
```

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/test_rerun_workflows.py -v`
Expected: PASS (this only asserts totals, not execution order — see Task A3 for the ordering nuance).

- [ ] **Step 3: Commit**

```bash
git add tests/test_rerun_workflows.py
git commit -m "test: verify --failed-first runs the full suite through warden"
```

### Task A3: Make LPT batching respect `--failed-first`'s intent

**Problem:** `--failed-first` reorders `session.items` so previously-failed node ids come first, but `_lpt_batch` (`plugin.py:99-113`) immediately re-sorts every node id by historical duration, discarding that ordering. The result: `--ff`'s "see failures again soon" intent gets silently overridden by weight-based ordering whenever a worker's batch mixes previously-failed and previously-passed tests. This doesn't affect pass/fail correctness (Task A2 already confirms totals are right) — it's a UX regression for anyone piping warden's terminal output live and expecting to see the failed test near the top.

**Fix:** give previously-failed node ids priority in the sort, breaking ties by weight exactly as before.

**Files:**
- Modify: `src/pytest_warden/plugin.py:99-113` (`_lpt_batch`)
- Test: `tests/test_rerun_workflows.py` (append)

**Interfaces:**
- Consumes: `session.config.cache.get("cache/lastfailed", {})` — the same dict `LFPlugin` itself reads/writes (`_pytest/cacheprovider.py`), so no new state to maintain.
- Produces: `_lpt_batch(node_ids, numprocesses, history_store, previously_failed=frozenset())` — new optional 4th parameter, default preserves today's behavior exactly for every existing caller/test.

- [ ] **Step 1: Write the failing test**

```python
def test_lpt_batching_puts_previously_failed_tests_first_within_a_worker(pytester):
    pytester.makepyfile(
        test_mod="""
        def test_a():
            assert True

        def test_b():
            assert False

        def test_c():
            assert True
        """
    )
    first = pytester.runpytest("--warden", "--numprocesses=1")
    first.assert_outcomes(passed=2, failed=1)

    second = pytester.runpytest("--warden", "--numprocesses=1", "--failed-first")
    second.assert_outcomes(passed=2, failed=1)
    lines = [
        line for line in second.outlines
        if "test_a" in line or "test_b" in line or "test_c" in line
    ]
    # test_b (previously failed) must be the first test line reported.
    assert "test_b" in lines[0], f"expected test_b first, got: {lines[0]}"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_rerun_workflows.py::test_lpt_batching_puts_previously_failed_tests_first_within_a_worker -v`
Expected: **FAIL** — `_lpt_batch` currently sorts purely by weight (all three tests have the same default weight of `1.0` on a fresh history store, so the fallback order is whatever `session.items` gave it *before* `--ff` reordering was overridden... actually since weights tie, Python's stable sort should currently preserve `--ff`'s order already for same-weight items). If this unexpectedly passes already, that's useful: it means ties happen to preserve order today, but the fix is still needed for the common case where `test_a`/`test_c` have *different* recorded durations than `test_b` (which would break the tie-preservation this test happens to rely on). Replace the test's assertion with one that forces different weights (e.g., have `test_a` sleep briefly) if it passes without the code change, so the test actually exercises the priority logic:

```python
def test_lpt_batching_puts_previously_failed_tests_first_even_when_slower(pytester):
    pytester.makepyfile(
        test_mod="""
        import time

        def test_a():
            time.sleep(0.2)  # recorded as slower -- would sort first by weight alone

        def test_b():
            assert False  # previously failed, but faster than test_a
        """
    )
    first = pytester.runpytest("--warden", "--numprocesses=1")
    first.assert_outcomes(passed=1, failed=1)

    second = pytester.runpytest("--warden", "--numprocesses=1", "--failed-first")
    lines = [line for line in second.outlines if "test_a" in line or "test_b" in line]
    assert "test_b" in lines[0], f"expected test_b (failed-first) ahead of slower test_a, got: {lines[0]}"
```

This version is guaranteed to fail against the current weight-only sort, since `test_a`'s recorded duration (~0.2s) outweighs `test_b`'s (~0s), putting `test_a` first under pure LPT.

- [ ] **Step 3: Implement the fix**

Replace `_lpt_batch` in `src/pytest_warden/plugin.py:99-113`:

```python
def _lpt_batch(node_ids, numprocesses, history_store, previously_failed=frozenset()):
    n = max(1, min(numprocesses, len(node_ids)))
    weights = {}
    for node_id in node_ids:
        durations = history_store.get_durations(node_id, window=_HISTORY_WINDOW)
        weights[node_id] = statistics.median(durations) if durations else _DEFAULT_WEIGHT

    def sort_key(node_id):
        # Previously-failed tests sort first (True > False), then by weight
        # descending within each group -- preserves --failed-first's intent
        # without giving up load-balancing among the rest.
        return (node_id in previously_failed, weights[node_id])

    order = sorted(node_ids, key=sort_key, reverse=True)
    loads = [0.0] * n
    batches = [[] for _ in range(n)]
    for node_id in order:
        i = min(range(n), key=lambda k: loads[k])
        batches[i].append(node_id)
        loads[i] += weights[node_id]
    return [batch for batch in batches if batch]
```

Update the one call site at `plugin.py:224`:

```python
        previously_failed = frozenset(session.config.cache.get("cache/lastfailed", {}))
        batches = _lpt_batch(node_ids, numprocesses, history_store, previously_failed)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_rerun_workflows.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest tests/ -v`
Expected: all tests pass (existing `_lpt_batch` callers rely on the default `previously_failed=frozenset()`, which reproduces prior behavior exactly).

- [ ] **Step 6: Commit**

```bash
git add src/pytest_warden/plugin.py tests/test_rerun_workflows.py
git commit -m "feat: LPT batching prioritizes previously-failed tests for --failed-first"
```

---

## Part B: Work-stealing scheduling

### Background and trigger condition

The original design note for this project already flagged work-stealing as conditional: build it only if static/LPT batching proves insufficient in practice. There's no real-world usage data yet (no target project has adopted warden), so treat this as **built and available, but not the default** — gate it behind `--warden-work-stealing`, leave LPT static batching as the default path, and let real usage decide which one teams reach for. The concrete scenario where this helps LPT can't: a test with **no history yet** that turns out to be much slower than its default weight assumed, discovered only once it's already running — a static batch can't rebalance around that; a worker that finishes early because its batch was over-estimated has no way to help a worker that's behind.

### Task B1: Extract the polling body into a reusable `_poll_once`

Pure refactor — no behavior change, no new test, existing tests are the safety net.

**Files:**
- Modify: `src/pytest_warden/plugin.py:308-353` (`_supervise`)

**Interfaces:**
- Produces: `_poll_once(session, workers) -> list` — takes a list of `_Worker` instances, does exactly one round of progress-tailing + timeout/crash detection (the current per-iteration body of `_supervise`'s `while pending:` loop), returns the subset that's still running (i.e., today's `still_pending`). Does **not** sleep and does **not** loop — callers own their own loop and sleep.

- [ ] **Step 1: Extract the function**

Replace `_supervise` (`plugin.py:308-353`) with:

```python
def _poll_once(session, workers):
    now = time.monotonic()
    still_pending = []
    for worker in workers:
        new_lines = _read_new_lines(worker)
        if new_lines:
            if worker.timeout:
                worker.deadline = now + worker.timeout
            for line in new_lines:
                _replay_event(session, worker, json.loads(line))

        if worker.proc.poll() is not None:
            for line in _read_new_lines(worker):
                _replay_event(session, worker, json.loads(line))
            if worker.current is not None:
                code = worker.proc.returncode
                _report_incident(
                    session,
                    worker,
                    f"warden: worker exited unexpectedly (code {code}) while running this test",
                )
            continue

        if worker.deadline is not None and now > worker.deadline and not worker.killed:
            worker.killed = True
            worker.job.terminate()
            _report_incident(
                session,
                worker,
                f"warden: hard-killed after exceeding {worker.timeout}s timeout",
            )

        still_pending.append(worker)
    return still_pending


def _supervise(session, workers):
    pending = list(workers)
    while pending:
        pending = _poll_once(session, pending)

        if session.shouldfail and pending:
            for worker in pending:
                worker.job.terminate()
            for worker in pending:
                worker.proc.wait()
            pending = []

        if pending:
            time.sleep(_POLL_INTERVAL)
```

- [ ] **Step 2: Run the full suite to confirm no regression**

Run: `uv run pytest tests/ -v`
Expected: all existing tests still pass, unchanged (this is a pure refactor — `_supervise`'s externally-visible behavior is identical).

- [ ] **Step 3: Commit**

```bash
git add src/pytest_warden/plugin.py
git commit -m "refactor: extract _poll_once from _supervise for reuse by work-stealing"
```

### Task B2: Add `--warden-work-stealing` and `--warden-chunk-size` options

**Files:**
- Modify: `src/pytest_warden/plugin.py:21-60` (`pytest_addoption`)
- Test: `tests/test_work_stealing.py`

- [ ] **Step 1: Write the failing test**

```python
def test_work_stealing_flag_is_recognized(pytester):
    pytester.makepyfile(
        """
        def test_ok():
            assert True
        """
    )
    result = pytester.runpytest("--warden", "--warden-work-stealing")
    result.stderr.no_fnmatch_line("*unrecognized arguments*")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_work_stealing.py -v`
Expected: FAIL — `error: unrecognized arguments: --warden-work-stealing`.

- [ ] **Step 3: Add the options**

In `src/pytest_warden/plugin.py`, inside `pytest_addoption`, after the existing `--warden-quarantine-flaky` block (`plugin.py:53-60`):

```python
    group.addoption(
        "--warden-work-stealing",
        dest="warden_work_stealing",
        action="store_true",
        default=False,
        help="Use dynamic chunk-based work-stealing instead of static LPT "
        "batching -- workers that finish early pull more work instead of idling.",
    )
    group.addoption(
        "--warden-chunk-size",
        dest="warden_chunk_size",
        action="store",
        type=int,
        default=None,
        help="Chunk size for --warden-work-stealing (default: total tests "
        "divided across roughly 4 chunks per worker).",
    )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_work_stealing.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pytest_warden/plugin.py tests/test_work_stealing.py
git commit -m "feat: add --warden-work-stealing and --warden-chunk-size options"
```

### Task B3: Chunk queue construction

**Files:**
- Modify: `src/pytest_warden/plugin.py` (add near `_lpt_batch`, e.g. after line 113)
- Test: `tests/test_work_stealing.py` (append)

**Interfaces:**
- Produces: `_default_chunk_size(total, numprocesses) -> int`; `_chunk_queue(node_ids, history_store, chunk_size) -> list[list[str]]` — a list of chunks (each a list of node id strings), heaviest chunk first.

- [ ] **Step 1: Write the failing test**

```python
from pytest_warden.plugin import _chunk_queue, _default_chunk_size
from pytest_warden.history import HistoryStore


def test_default_chunk_size_aims_for_about_four_chunks_per_worker():
    assert _default_chunk_size(total=40, numprocesses=2) == 5  # 40 / (2*4)
    assert _default_chunk_size(total=3, numprocesses=8) == 1  # never below 1


def test_chunk_queue_splits_weight_sorted_ids_into_fixed_size_chunks(tmp_path):
    store = HistoryStore(str(tmp_path / "history.sqlite3"))
    try:
        chunks = _chunk_queue(["a", "b", "c", "d", "e"], store, chunk_size=2)
        assert [len(c) for c in chunks] == [2, 2, 1]
        assert sum(chunks, []) == sorted(["a", "b", "c", "d", "e"])  # every id present exactly once
    finally:
        store.close()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_work_stealing.py -v`
Expected: FAIL — `ImportError: cannot import name '_chunk_queue'`.

- [ ] **Step 3: Implement**

Add to `src/pytest_warden/plugin.py`, after `_lpt_batch` (after line 113), and add `import math` to the top-level imports (near the other stdlib imports at the top of the file):

```python
def _default_chunk_size(total, numprocesses):
    n = max(1, numprocesses)
    return max(1, math.ceil(total / (n * 4)))


def _chunk_queue(node_ids, history_store, chunk_size):
    weights = {}
    for node_id in node_ids:
        durations = history_store.get_durations(node_id, window=_HISTORY_WINDOW)
        weights[node_id] = statistics.median(durations) if durations else _DEFAULT_WEIGHT
    order = sorted(node_ids, key=lambda nid: weights[nid], reverse=True)
    return [order[i : i + chunk_size] for i in range(0, len(order), chunk_size)]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_work_stealing.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pytest_warden/plugin.py tests/test_work_stealing.py
git commit -m "feat: chunk queue construction for work-stealing"
```

### Task B4: `_Worker.is_retry` and a chunk-worker spawn helper

**Files:**
- Modify: `src/pytest_warden/plugin.py:198-210` (`_Worker`)
- Modify: `src/pytest_warden/plugin.py:264-285` (near `_run_wave`, add a sibling helper)

**Interfaces:**
- Consumes: `_spawn_worker(session, batch, progress_path, cov_data_file)` (existing, unchanged, `plugin.py:164-195`).
- Produces: `_Worker.is_retry: bool` (new field, default `False`); `_spawn_chunk_worker(session, tmpdir, worker_count, batch, timeout, is_retry=False) -> (int, _Worker)` — spawns one worker for one chunk and returns the incremented counter plus the `_Worker`, mirroring the per-worker setup already inside `_run_wave`'s loop body but reusable one chunk at a time.

- [ ] **Step 1: Add `is_retry` to `_Worker`**

In `plugin.py:198-210`, add the field:

```python
class _Worker:
    def __init__(self, proc, job, progress_path, timeout, batch, cov_data_file=None, is_retry=False):
        self.proc = proc
        self.job = job
        self.progress_path = progress_path
        self.timeout = timeout
        self.batch = batch
        self.cov_data_file = cov_data_file
        self.is_retry = is_retry
        self.lines_consumed = 0
        self.deadline = time.monotonic() + timeout if timeout else None
        self.killed = False
        self.started_ids = set()
        self.current = None  # {"nodeid":..., "location":...} of the in-flight test
```

- [ ] **Step 2: Add `_spawn_chunk_worker`**

Add after `_run_wave` (after `plugin.py:285`):

```python
def _spawn_chunk_worker(session, tmpdir, worker_count, batch, timeout, is_retry=False):
    cov_sources = _cov_sources(session)
    progress_path = os.path.join(tmpdir, f"worker-{worker_count}.jsonl")
    cov_data_file = (
        os.path.join(tmpdir, f".coverage.worker-{worker_count}") if cov_sources else None
    )
    open(progress_path, "a", encoding="utf-8").close()
    proc, job = _spawn_worker(session, batch, progress_path, cov_data_file)
    worker = _Worker(proc, job, progress_path, timeout, batch, cov_data_file, is_retry)
    return worker_count + 1, worker
```

No test needed for this step alone — it's exercised end-to-end by Task B5's test. Run the full suite once to confirm nothing broke from the `_Worker` signature change:

- [ ] **Step 3: Run the full suite**

Run: `uv run pytest tests/ -v`
Expected: all pass (`is_retry` has a default, every existing `_Worker(...)` call site is unaffected).

- [ ] **Step 4: Commit**

```bash
git add src/pytest_warden/plugin.py
git commit -m "feat: add chunk-worker spawn helper and per-worker retry tracking"
```

### Task B5: The work-stealing loop

**Files:**
- Modify: `src/pytest_warden/plugin.py` (add new function, wire into `_run_controller`)
- Test: `tests/test_work_stealing.py` (append)

**Interfaces:**
- Consumes: `_poll_once` (Task B1), `_chunk_queue`/`_default_chunk_size` (Task B3), `_spawn_chunk_worker` (Task B4), `_report_never_ran` (existing, `plugin.py:408-425`).
- Produces: `_run_work_stealing(session, tmpdir, node_ids, numprocesses, timeout, history_store) -> (int, list[_Worker])` — same return shape as today's `_run_wave`/`_run_controller` combination (final worker count, flat list of every `_Worker` that ever ran, for `_combine_coverage` to read `cov_data_file` off of).

- [ ] **Step 1: Write the failing test**

This proves the actual point of work-stealing: a big chunk and several tiny chunks, more workers than would be needed if the tiny chunks were pre-assigned evenly — under static per-worker batching each worker gets a fixed share up front (no rebalancing once running), but the work-stealing loop should keep every idle worker pulling more chunks until the queue drains, so total wall-clock time tracks the slowest *chunk*, not the slowest *worker's static pre-assignment*.

```python
import time


def test_work_stealing_keeps_all_workers_busy_until_the_queue_drains(pytester):
    pytester.makepyfile(
        test_mod="""
        import time

        def test_slow():
            time.sleep(0.6)

        def test_fast_1():
            pass

        def test_fast_2():
            pass

        def test_fast_3():
            pass

        def test_fast_4():
            pass

        def test_fast_5():
            pass
        """
    )
    start = time.monotonic()
    result = pytester.runpytest(
        "--warden",
        "--numprocesses=2",
        "--warden-work-stealing",
        "--warden-chunk-size=1",
    )
    elapsed = time.monotonic() - start

    result.assert_outcomes(passed=6)
    # test_slow (0.6s) runs on one worker; the other worker should chew
    # through all 5 fast chunks well within that same window instead of
    # taking a fixed, pre-assigned share -- total time should track ~0.6s,
    # not stall waiting on a static split.
    assert elapsed < 1.2, f"took {elapsed}s -- work-stealing doesn't seem to be rebalancing"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_work_stealing.py::test_work_stealing_keeps_all_workers_busy_until_the_queue_drains -v`
Expected: FAIL — `--warden-work-stealing` is accepted (Task B2) but `_run_controller` doesn't branch on it yet, so this runs through the existing static-LPT path. With `--numprocesses=2` and 6 tests, static LPT would actually already balance this particular case reasonably (it's a similar shape to the Phase 2 LPT test) — if it unexpectedly passes here, tighten the assertion to `elapsed < 0.9` and/or add a second slow test to force a real gap between the two scheduling strategies before moving on, the same way Task A3's test was tightened when the first version didn't reliably distinguish behaviors.

- [ ] **Step 3: Implement `_run_work_stealing`**

Add after `_spawn_chunk_worker` (from Task B4):

```python
def _run_work_stealing(session, tmpdir, node_ids, numprocesses, timeout, history_store):
    chunk_size = session.config.getoption("warden_chunk_size") or _default_chunk_size(
        len(node_ids), numprocesses
    )
    # Each queue entry is (batch, is_retry) -- is_retry MUST travel with the
    # batch through the queue, not just live on the _Worker that ran it, or
    # a chunk that keeps crashing would get re-inserted as a fresh (untagged)
    # entry every time and retried forever instead of stopping after one.
    queue = [(chunk, False) for chunk in _chunk_queue(node_ids, history_store, chunk_size)]
    n = max(1, min(numprocesses, len(queue)))

    worker_count = 0
    all_workers = []
    active = []
    for _ in range(n):
        if not queue:
            break
        batch, is_retry = queue.pop(0)
        worker_count, worker = _spawn_chunk_worker(
            session, tmpdir, worker_count, batch, timeout, is_retry
        )
        all_workers.append(worker)
        active.append(worker)

    while active:
        still_running = _poll_once(session, active)
        finished = [w for w in active if w not in still_running]

        for worker in finished:
            worker.job.close()
            remainder = [nid for nid in worker.batch if nid not in worker.started_ids]
            if remainder and not worker.is_retry and not session.shouldfail:
                queue.insert(0, (remainder, True))
            elif remainder:
                for nid in remainder:
                    _report_never_ran(
                        session,
                        nid,
                        "warden: worker never reached this test even after one retry",
                    )

        active = still_running
        if not session.shouldfail:
            while queue and len(active) < n:
                batch, is_retry = queue.pop(0)
                worker_count, worker = _spawn_chunk_worker(
                    session, tmpdir, worker_count, batch, timeout, is_retry
                )
                all_workers.append(worker)
                active.append(worker)

        if session.shouldfail and active:
            for worker in active:
                worker.job.terminate()
            for worker in active:
                worker.proc.wait()
            active = []

        if active:
            time.sleep(_POLL_INTERVAL)

    return worker_count, all_workers
```

- [ ] **Step 4: Wire it into `_run_controller`**

In `plugin.py`, `_run_controller` currently (lines 213-261) always calls `_lpt_batch` + the two-wave static logic inline. Extract that existing static path into its own function first, so `_run_controller` can cleanly branch:

Replace the body from `batches = _lpt_batch(...)` (`plugin.py:224`) through `_combine_coverage(session, all_workers)` / `_record_history(...)` (`plugin.py:258-259`) with a call to one of two functions:

```python
def _run_static_lpt(session, tmpdir, node_ids, numprocesses, timeout, history_store):
    previously_failed = frozenset(session.config.cache.get("cache/lastfailed", {}))
    batches = _lpt_batch(node_ids, numprocesses, history_store, previously_failed)

    worker_count = 0
    all_workers = []

    workers, worker_count = _run_wave(session, tmpdir, worker_count, batches, timeout)
    all_workers.extend(workers)

    retry_batches = [
        [nid for nid in worker.batch if nid not in worker.started_ids] for worker in workers
    ]
    retry_batches = [b for b in retry_batches if b]

    if retry_batches and not session.shouldfail:
        retry_workers, worker_count = _run_wave(
            session, tmpdir, worker_count, retry_batches, timeout
        )
        all_workers.extend(retry_workers)
        for worker in retry_workers:
            still_missing = [nid for nid in worker.batch if nid not in worker.started_ids]
            for nid in still_missing:
                _report_never_ran(
                    session,
                    nid,
                    "warden: worker never reached this test even after one retry",
                )

    return worker_count, all_workers
```

And `_run_controller` becomes:

```python
def _run_controller(session):
    node_ids = [item.nodeid for item in session.items]
    if not node_ids:
        return

    numprocesses = session.config.getoption("warden_numprocesses")
    timeout = session.config.getoption("warden_timeout")
    work_stealing = session.config.getoption("warden_work_stealing")

    history_store = HistoryStore(_history_db_path(session))
    session.config._warden_history_store = history_store
    try:
        with tempfile.TemporaryDirectory(prefix="pytest-warden-") as tmpdir:
            if work_stealing:
                worker_count, all_workers = _run_work_stealing(
                    session, tmpdir, node_ids, numprocesses, timeout, history_store
                )
            else:
                worker_count, all_workers = _run_static_lpt(
                    session, tmpdir, node_ids, numprocesses, timeout, history_store
                )

            session.config._warden_worker_count = worker_count
            _combine_coverage(session, all_workers)
            _record_history(session, history_store)
    finally:
        history_store.close()
```

- [ ] **Step 5: Run the new test to verify it passes**

Run: `uv run pytest tests/test_work_stealing.py -v`
Expected: PASS.

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest tests/ -v`
Expected: all tests pass — the default path (`--warden-work-stealing` absent) goes through `_run_static_lpt`, which is a straight extraction of the previously-inline logic with no behavior change.

- [ ] **Step 7: Commit**

```bash
git add src/pytest_warden/plugin.py tests/test_work_stealing.py
git commit -m "feat: chunk-based work-stealing scheduler behind --warden-work-stealing"
```

### Task B6: Work-stealing + timeout/crash interaction tests

Two distinct things need proving here, and they're easy to conflate:

1. A chunk with only one test in it, that test crashes — there's no
   "never-started remainder" at all (the crashing test already got its
   `logstart`, so `_report_incident` handles it directly), but the queue
   must still continue to the next chunk instead of hanging.
2. A chunk with *multiple* tests where the crash happens before the last
   one starts — genuinely exercises the `queue.insert(0, (remainder, True))`
   path fixed above, proving the `is_retry` tag actually survives the round
   trip through the shared queue.

Both matter; a chunk size of 1 (as a first draft of this task assumed)
only exercises case 1, and would have silently never tested the requeue
path this whole task exists to cover.

**Files:**
- Test: `tests/test_work_stealing.py` (append)

- [ ] **Step 1: Write the "single-test chunk crash doesn't hang the queue" test**

```python
def test_work_stealing_survives_a_crash_in_a_single_test_chunk(pytester):
    pytester.makepyfile(
        test_mod="""
        import os

        def test_a():
            assert True

        def test_b():
            os._exit(70)  # always crashes -- its own whole chunk at size 1

        def test_c():
            assert True
        """
    )
    result = pytester.runpytest(
        "--warden", "--numprocesses=1", "--warden-work-stealing", "--warden-chunk-size=1"
    )
    result.assert_outcomes(passed=2, failed=1)
```

- [ ] **Step 2: Write the "remainder within a chunk gets exactly one retry" test**

```python
def test_work_stealing_retries_the_never_started_remainder_of_a_crashed_chunk(pytester):
    pytester.makepyfile(
        test_mod="""
        import os

        def test_a():
            assert True

        def test_b():
            os._exit(70)  # crashes before test_c in the same chunk ever starts

        def test_c():
            assert True
        """
    )
    # chunk_size=3 puts all three tests in ONE chunk together -- test_a runs
    # and passes, test_b crashes while in flight (reported failed directly,
    # not retried), and test_c never gets a logstart in this attempt at all,
    # making it the genuine "never-started remainder" that should get
    # exactly one retry on a fresh chunk-worker.
    result = pytester.runpytest(
        "--warden", "--numprocesses=1", "--warden-work-stealing", "--warden-chunk-size=3"
    )
    result.assert_outcomes(passed=2, failed=1)
```

- [ ] **Step 3: Run both**

Run: `uv run pytest tests/test_work_stealing.py -v`
Expected: both PASS given Task B5's fixed implementation. If the second test fails with `test_c` missing from the results entirely (neither passed nor failed), that's the exact bug this task's background section describes — check that `queue.insert(0, (remainder, True))` actually runs (not `queue.insert(0, remainder)` without the tuple) and that the backfill loop unpacks `batch, is_retry = queue.pop(0)` rather than treating the popped entry as a bare list.

- [ ] **Step 4: Commit**

```bash
git add tests/test_work_stealing.py
git commit -m "test: verify work-stealing's per-chunk retry bound, including the is_retry tag surviving the queue round trip"
```

### Task B7: Document the new flags

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add the two new flags to the flag table**

In the `## Usage` flag table, add rows:

```markdown
| `--warden-work-stealing` | Use dynamic chunk-based scheduling instead of static LPT batching — workers that finish early pull more work instead of idling. |
| `--warden-chunk-size` | Chunk size for `--warden-work-stealing` (default: ~4 chunks per worker). |
```

- [ ] **Step 2: Add guidance to `## Best practices`**

Add a bullet:

```markdown
- **Reach for `--warden-work-stealing` only once plain LPT batching
  demonstrably isn't enough** — it helps specifically when tests have no
  history yet, or when a test's duration varies a lot run to run, so a
  static upfront estimate keeps missing. If your suite has stable,
  well-established timing history, static LPT batching already balances
  it and work-stealing just adds chunk-restart overhead for no benefit.
```

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: document work-stealing flags and when to use them"
```

---

## Self-review notes

- **Spec coverage:** Part A covers `--last-failed`/`--failed-first` (verify + the LPT-ordering fix). Part B covers work-stealing scheduling end to end (options, chunk queue, spawn helper, the loop itself, retry-bound parity with static batching, docs). UI Mode is intentionally excluded per the request.
- **No placeholders:** every step has complete, real code or a real command with a stated expected result — including the two places (Task A1 Step 2, Task B5 Step 2) where the expected result is "should already pass" or "might need tightening," both of which give a concrete diagnostic path rather than leaving a TBD.
- **Type/signature consistency:** `_lpt_batch`'s new `previously_failed` parameter defaults to `frozenset()` so every pre-existing call and test keeps working; `_Worker`'s new `is_retry` field defaults to `False` for the same reason; `_run_work_stealing` and `_run_static_lpt` share the exact `(worker_count, all_workers)` return shape `_run_controller` already expects from today's inline logic, so `_combine_coverage`/`_record_history` need no changes at all.
- **A real bug caught on review, fixed before finalizing:** the first draft of `_run_work_stealing` (Task B5) requeued a crashed chunk's remainder as a bare `queue.insert(0, remainder)` — losing the fact that it was already a retry. A chunk that kept crashing across multiple never-started tests could have been requeued forever instead of stopping after one retry, exactly the infinite-loop failure mode `--warden-timeout`'s batch-requeue bound was built to prevent in the first place (see `tests/test_requeue.py`). Fixed by making every queue entry a `(batch, is_retry)` tuple so the tag survives the round trip; Task B6 was also rewritten because its original single-item-chunk test couldn't have caught this (a size-1 chunk never produces a "never-started remainder" to requeue at all) — the added multi-item-chunk test in Task B6 Step 2 is the one that actually exercises this path.
