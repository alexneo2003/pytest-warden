# pytest-warden

A pytest plugin that distributes your test run across supervised worker
subprocesses and **guarantees** a timeout kills the whole process tree —
the test, any browser/Node/child processes it spawned, everything — instead
of a best-effort thread-based interrupt that can leave orphans behind.

`pytest --warden` stays your entry point. Under the hood, warden takes over
scheduling for that run: it batches your already-collected tests across N
real `pytest` subprocesses, wraps each one in a Windows Job Object (with a
POSIX process-group fallback), and tails a lightweight progress channel to
detect and hard-kill any subprocess that overruns its timeout. Results are
merged back through pytest's own real reporting hooks, so junitxml, the
terminal summary, coverage, and third-party plugins all keep working
unmodified.

## Why

Thread-based watchdogs (the mechanism most timeout plugins use) can't
reliably interrupt a genuinely hung process — if a test deadlocks holding
the GIL, or in native code, no thread in that same process can get
scheduled to kill it either. And a test that hangs after spawning a
browser or subprocess can leave orphans running long after pytest itself
gives up.

warden sidesteps both problems by never running your tests in the same
process that's watching them. The watchdog and the test always live in
different OS processes, and the process boundary is exactly what makes
a hard kill of the entire tree possible.

## Installation

```
pip install pytest-warden
```

No configuration needed — the plugin auto-registers. It stays completely
inert until you pass `--warden`.

## Usage

```
pytest --warden --numprocesses=4 --timeout=60
```

| Flag | Purpose |
|---|---|
| `--warden` | Activates warden for this run. Without it, behavior is identical to bare pytest. |
| `--numprocesses` | Number of worker subprocesses to distribute tests across (default: 1). |
| `--timeout` | Per-test timeout in seconds. A test exceeding it hard-kills its whole worker. Overridable per-test with `@pytest.mark.timeout(N)`. |
| `--maxfail` | Standard pytest flag — forwarded to workers and enforced across the whole distributed run, not just within one worker. |
| `--cov=<source>` | Standard pytest-cov flag — coverage is measured per worker and combined into a single `.coverage` file at the rootdir. |
| `--warden-history-db` | Path to warden's SQLite timing/outcome store (default: `<rootdir>/.pytest_warden/history.sqlite3`). |
| `--warden-quarantine-flaky` | A failure on a test whose recent history has both passes and failures reports as `xfail` instead of `failed`, and doesn't fail the build. |
| `--last-failed` / `--failed-first` | Standard pytest flags — work transparently, since warden never touches collection or pytest's own report hooks. |
| `--warden-work-stealing` | Use dynamic chunk-based scheduling instead of static LPT batching — workers that finish early pull more work instead of idling. |
| `--warden-chunk-size` | Chunk size for `--warden-work-stealing` (default: ~4 chunks per worker). |

### A hard-killed test in your report

When a test gets hard-killed for exceeding its timeout, it shows up as a
normal failure with a `longrepr` explaining why — distinguishable from an
assertion failure, visible in JUnit XML and the terminal summary like any
other failure. The remaining not-yet-run tests in that worker's batch get
exactly one retry on a fresh worker; a test that fails the same way twice
is marked failed and not retried again, so a genuinely broken test can
never loop a run forever.

## How it works

- **Scheduling.** By default, tests are batched once upfront by
  longest-processing-time-first (LPT): each test's historical median
  duration (from the history store) weights it, and tests are greedily
  assigned to whichever worker currently has the lightest load — so two
  historically-slow tests don't end up stacked on the same worker just
  because of collection order. With no history yet, this degenerates to an
  even split. `--warden-work-stealing` replaces this with dynamic
  chunk-based dispatch instead: tests are split into small chunks, each
  chunk is its own worker subprocess, and whichever worker finishes first
  pulls the next chunk from a shared queue — useful when duration
  estimates keep being wrong, since static LPT can't rebalance once a
  batch is already running but work-stealing continuously does.
- **Supervision.** Each worker subprocess is wrapped in a Job Object the
  moment it's spawned. A companion plugin loaded into the worker
  (`-p pytest_warden.worker`) appends one JSON line per test start/finish to
  a progress file; the controller tails it to reset each worker's deadline
  and detect hangs from outside the process that might be stuck.
- **Reporting.** For every result a worker produces, the controller
  reconstructs and replays pytest's own real hook calls
  (`pytest_runtest_logstart` / `pytest_runtest_logreport` /
  `pytest_runtest_logfinish`) against its own top-level session — so
  anything that consumes those hooks (junitxml, terminal reporting,
  `--lf`/`--ff` caching, most third-party plugins) works exactly as it
  would in a normal run, without warden needing its own merge logic.

## Best practices

- **Remove `pytest-xdist` and `pytest-timeout` before adopting warden.**
  Both become redundant, and pytest reserves flag names for whichever
  plugin registers them first — running warden alongside either one
  risks a confusing conflict rather than a clean handoff.
- **Commit `.pytest_warden/` to `.gitignore`, not to your repo.** The
  history store is a local performance cache, not a build artifact —
  treat it like `.pytest_cache/`. If you want LPT scheduling to actually
  help in CI, persist it across runs via your CI cache mechanism (keyed
  on branch or job name) rather than starting cold every time.
- **Start with `--numprocesses` close to your CPU core count**, and adjust
  from real wall-clock numbers rather than guessing — LPT scheduling only
  optimizes the split you already have, it can't fix a worker count that's
  fundamentally too high for the machine running the tests, and spawning
  more worker subprocesses than the machine can actually run in parallel
  just adds startup overhead without shortening the run.
- **Treat `--warden-quarantine-flaky` as a visibility tool, not a fix.** A
  quarantined test still shows up as `xfail` in every report — it's meant
  to stop a known-flaky test from blocking a build while it's investigated,
  not to hide it. Un-quarantine (i.e., let it fail the build again) once
  it's been fixed, or it'll quietly stop getting attention.
- **Give tests a real `--timeout`.** Without one, a hung test blocks its
  worker indefinitely just like bare pytest would — warden's hard-kill
  guarantee only fires once a timeout is actually configured.
- **Know that a hard kill loses coverage for the whole batch it was in, not
  just the killed test.** `coverage.py` only flushes its data to disk at
  clean process exit; a Job Object kill skips that entirely, so any test
  that already passed in the same worker can end up looking uncovered too.
  If you're combining `--cov` with `--timeout` and coverage accuracy
  matters, prefer more, smaller batches (a higher `--numprocesses`, or
  `--warden-work-stealing` with a small `--warden-chunk-size`) so a kill
  only ever costs you one test's worth of coverage data.
- **Reach for `--warden-work-stealing` only once plain LPT batching
  demonstrably isn't enough.** It helps specifically when tests have no
  history yet, or when a test's duration varies a lot run to run, so a
  static upfront estimate keeps missing. If your suite has stable,
  well-established timing history, static LPT batching already balances
  it and work-stealing just adds chunk-restart overhead for no benefit.

## Platform notes

Developed and tested on macOS/Linux, exercising the POSIX process-group
fallback in `jobobject.py`. The Windows-specific `win32job`-based branch
has not yet been exercised against a real Windows CI run — a
`windows-latest` job is scaffolded in `.github/workflows/ci.yml`, pending
this repo having a CI-connected remote.

pytest reserves all lowercase short options (`-x`, `-n`, etc.) for its own
core plugins as of pytest 9.x — only long-form flags (`--numprocesses`,
`--timeout`) are available here, matching xdist/pytest-timeout's names but
not their short aliases.

## Development

```
uv sync
uv run pytest tests/
```

Every feature is verified with real subprocesses — real hangs killed for
real, real crashes, real coverage combining — not mocks.

## Roadmap

UI Mode (a live web dashboard) is the remaining unplanned item — everything
else from the original phased plan is implemented. See
`docs/superpowers/plans/` for implementation notes on rerun workflows and
work-stealing.
