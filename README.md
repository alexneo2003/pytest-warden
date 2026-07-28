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
terminal summary, coverage, `--lf`/`--ff`, and CLI-flag-gated third-party
plugins all keep working unmodified — see "Best practices" for a caveat on
plugins that hook into test reporting via `conftest.py` instead of a CLI
flag.

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

| Flag                               | Purpose                                                                                                                                       |
| ---------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `--warden`                         | Activates warden for this run. Without it, behavior is identical to bare pytest.                                                              |
| `--numprocesses`                   | Number of worker subprocesses to distribute tests across (default: 1). Accepts an integer, a percentage of available CPU count (e.g. `50%`), `auto` (physical CPU count, requires `psutil`), or `logical` (logical CPU count). |
| `--timeout`                        | Per-test timeout in seconds. A test exceeding it hard-kills its whole worker. Overridable per-test with `@pytest.mark.timeout(N)`. Falls back to the `timeout` ini option (e.g. `[tool.pytest.ini_options]` in `pyproject.toml`) when not passed on the command line. |
| `--maxfail`                        | Standard pytest flag — forwarded to workers and enforced across the whole distributed run, not just within one worker.                        |
| `--cov=<source>`                   | Standard pytest-cov flag — coverage is measured per worker and combined into a single `.coverage` file at the rootdir.                        |
| `--warden-history-db`              | Path to warden's SQLite timing/outcome store (default: `<rootdir>/.pytest_warden/history.sqlite3`).                                           |
| `--warden-quarantine-flaky`        | A failure on a test whose recent history has both passes and failures reports as `xfail` instead of `failed`, and doesn't fail the build.     |
| `--last-failed` / `--failed-first` | Standard pytest flags — work transparently, since warden never touches collection or pytest's own report hooks.                               |
| `--warden-work-stealing`           | Use dynamic chunk-based scheduling instead of static LPT batching — workers that finish early pull more work instead of idling.               |
| `--warden-chunk-size`              | Chunk size for `--warden-work-stealing` (default: ~4 chunks per worker).                                                                      |
| `--warden-dist`                    | Which tests must land on the same worker together: `test` (default, no grouping), `loadfile`, `loadscope`, or `loadgroup` (with `@pytest.mark.warden_group(name=...)`). Orthogonal to `--warden-work-stealing`. |

### Terminal output

By default (no `-v`/`-q`) and under `-v`/`-vv`, a `--warden` run prints more
than bare pytest does, since a plain dot stream — or even pytest's own
verbose per-test line — alone wouldn't tell you which of the N concurrent
workers is doing what:

```
warden: starting run with 4 worker(s) (static LPT scheduling)
[1/12] worker 0 -> tests/test_api.py::test_login STARTED
[1/12] worker 0 -> tests/test_api.py::test_login PASSED
[2/12] worker 2 -> tests/test_api.py::test_logout STARTED
[2/12] worker 2 -> tests/test_api.py::test_logout FAILED
warden: worker 2 didn't finish its batch (3 test(s) left) -- recreating a fresh worker to pick them up
...
warden: distributed across 5 worker(s)
```

- The startup banner (worker count, scheduling mode) and the final
  `distributed across N worker(s)` line.
- A `[n/total] worker N -> nodeid STARTED` line the moment a worker picks
  up a test, and a `[n/total] worker N -> nodeid RESULT` line once it
  finishes — each with its own independent `n` counter, so a test in
  flight doesn't advance the other one's fraction. This is the only place
  you see which worker is running (or ran) which test and how it came
  out — non-`-v` runs get no other per-test identification at all, and
  `-v`/`-vv` runs get pytest's own nodeid + outcome line but with no
  worker index. It replaces pytest's own bare dot/letter under default
  verbosity (which would otherwise still print alongside it).
- `warden: worker N didn't finish its batch (K test(s) left) --
recreating a fresh worker to pick them up`, whenever a crash or
  hard-kill orphans the rest of that worker's queued tests. This applies
  identically whichever way a test ends — hard-killed, worker crash, or
  never reached even after a retry.

Under `-q`/`-qq`, all of the above is suppressed, same as pytest's own dot
stream would be — a quiet run stays quiet.

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
  `--lf`/`--ff` caching) works exactly as it would in a normal run,
  without warden needing its own merge logic. Worker subprocesses run
  fully quiet (`-q`, stdout/stderr discarded) so nothing from a worker's
  own raw output leaks into the controller's single, replayed terminal
  report. See "Best practices" below for a caveat on third-party plugins
  specifically.

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
  just adds startup overhead without shortening the run. `--numprocesses=auto`
  (physical CPU count, needs `psutil`; falls back to `logical` if it isn't
  installed) is the most direct spelling of "close to my CPU core count" if
  you're coming from pytest-xdist's `-n auto`. `--numprocesses=50%` is a
  percentage-based alternative that resolves against the available CPU
  count (respecting container/cgroup limits on Linux) at run time.
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
- **Know that conftest-loaded reporting plugins with side effects fire
  twice, not once.** Each worker is a fully real, independent `pytest`
  subprocess with the same `conftest.py` (and any auto-registered
  third-party plugins) loaded as the controller. A hookimpl like
  `pytest_runtest_logreport` defined in `conftest.py` genuinely executes
  once for real inside the worker (real execution, real side effect --
  e.g. writing a file or emitting a metric) and once more when the
  controller replays that same report through its own hook manager. This
  is different from CLI-flag-gated plugins (`--junitxml`, the `--lf`/`--ff`
  cache): their flags are never forwarded to workers, so they only ever
  run in the controller and observe each result exactly once. If a
  reporting plugin's side effects must fire exactly once under warden, two
  opt-in mitigations are available:
  - **`--warden-disable-worker-plugin=NAME`** (repeatable) disables a
    _named_ plugin inside worker subprocesses only (`-p no:NAME`), leaving
    only the controller's replay. This only works for plugins registered
    under a name — a `pytest11` entry-point install, or an explicit
    `pluginmanager.register(obj, name=...)` call — **not** a bare hookimpl
    defined directly in `conftest.py`, since `conftest.py` isn't itself a
    nameable/blockable plugin.
  - **The `PYTEST_WARDEN_WORKER` environment variable** is always set to
    `"1"` inside every worker subprocess. Any hookimpl — named plugin or
    bare `conftest.py` function — can check it to self-silence in workers
    and rely solely on the controller's replay:

    ```python
    # conftest.py, BEFORE: fires twice under --warden (once for real in
    # the worker, once more via the controller's replay)
    def pytest_runtest_logreport(report):
        if report.when == "call":
            send_to_metrics_backend(report)


    # conftest.py, AFTER: fires exactly once
    import os


    def pytest_runtest_logreport(report):
        if os.environ.get("PYTEST_WARDEN_WORKER"):
            return
        if report.when == "call":
            send_to_metrics_backend(report)
    ```

- **Know that session/module/class-scoped fixtures are scoped per worker,
  not once for the whole run.** Each worker is a fully separate `pytest`
  subprocess, so a `session`- or `module`-scoped fixture's state is created
  independently in every worker that ends up running part of that module —
  the same trade-off `pytest-xdist` has. Two different fixes for two
  different problems:
  - If tests sharing a `module`/`class`-scoped fixture (or an arbitrary
    marked group of tests) just need to stay **consistent with each
    other** — not run the fixture's setup exactly once globally, just
    never split them across workers — `--warden-dist=loadscope` (or
    `loadfile` for whole-file grouping, or `loadgroup` with
    `@pytest.mark.warden_group(name=...)` for cross-file grouping) is
    usually simpler than wrapping the fixture itself. It does **not** help
    a `session`-scoped fixture, though — grouping still spreads work
    across multiple workers, it just keeps each named group whole within
    one of them.
  - If a fixture's setup must run **exactly once across the entire run**
    (including `session` scope), regardless of how many workers touch it,
    use the **`warden_run_once`** fixture instead:
  ```python
  @pytest.fixture(scope="session")
  def my_fixture(warden_run_once):
      return warden_run_once("my_fixture", _do_expensive_setup)
  ```
  `_do_expensive_setup` runs exactly once across the whole distributed run
  (via a real OS-level file lock, not a spin-poll), and every worker's
  `my_fixture` gets the identical result. Works unmodified in bare
  (non-`--warden`) runs too, with zero contention. See
  `pytest_warden.coordination.run_once` for the underlying primitive if
  you need it outside a fixture.
- **`--warden-dist` grouping is a best-effort scheduling hint, not a hard
  guarantee under failure.** It's honored on a group's initial dispatch to
  a worker; if that worker is hard-killed mid-group (timeout, crash,
  `--maxfail`), the surviving remainder of the group is retried as its own,
  now-ungrouped batch rather than being re-grouped.
- **Reach for `--warden-work-stealing` only once plain LPT batching
  demonstrably isn't enough.** It helps specifically when tests have no
  history yet, or when a test's duration varies a lot run to run, so a
  static upfront estimate keeps missing. If your suite has stable,
  well-established timing history, static LPT batching already balances
  it and work-stealing just adds chunk-restart overhead for no benefit.

## Platform notes

Developed on macOS/Linux, exercising the POSIX process-group fallback in
`jobobject.py` locally. The Windows-specific `win32job`-based branch is
verified on real Windows CI — `.github/workflows/ci.yml` runs the full
suite on both `ubuntu-latest` and `windows-latest` on every push.

pytest reserves all lowercase short options (`-x`, `-n`, etc.) for its own
core plugins as of pytest 9.x — only long-form flags (`--numprocesses`,
`--timeout`) are available here, matching xdist/pytest-timeout's names but
not their short aliases.

## Development

```
uv sync --group dev
uv run pytest tests/
```

Install the pre-commit hooks once per clone so lint/format/type-check
issues are caught before they reach CI:

```
uv run pre-commit install
```

This runs `ruff check --fix`, `ruff format`, `ty check`, and a few basic
hygiene checks (trailing whitespace, merge-conflict markers, etc.) on
every commit — the same checks CI's `lint` job runs, so a failure here is
a failure there too. The real test suite (`pytest tests/`) is deliberately
*not* part of the pre-commit hook: it spawns real subprocesses and real
timeouts/hangs by design, which makes it too slow for every commit — run
it directly, or let CI run it on push.

Every feature is verified with real subprocesses — real hangs killed for
real, real crashes, real coverage combining — not mocks.

## Roadmap

UI Mode (a live web dashboard) is the remaining unplanned item — everything
else from the original phased plan is implemented. See
`docs/superpowers/plans/` for implementation notes on rerun workflows and
work-stealing.
