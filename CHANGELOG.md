# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `--numprocesses` accepts a percentage (e.g. `--numprocesses=50%`),
  resolved against the available CPU count (`os.sched_getaffinity` where
  supported, else `os.cpu_count()`).
- `--warden-disable-worker-plugin=NAME` (repeatable): disables a named
  third-party plugin inside worker subprocesses only, via `-p no:NAME` --
  only works for plugins registered under a name (pytest11 entry point or
  explicit `pluginmanager.register(..., name=...)`), not bare
  `conftest.py` hookimpls.
- `PYTEST_WARDEN_WORKER=1` is now set in every worker subprocess's
  environment, so a `conftest.py` hookimpl can self-silence in workers
  (`if os.environ.get("PYTEST_WARDEN_WORKER"): return`) and rely solely on
  the controller's replay -- this works for bare conftest.py hookimpls,
  unlike `--warden-disable-worker-plugin`.
- `warden_run_once` fixture and `pytest_warden.coordination.run_once`: run
  a callable exactly once across an entire distributed `--warden`
  invocation (all workers get the identical cached result), using a real
  OS-level file lock (`fcntl.flock` / `msvcrt.locking`, no new dependency)
  rather than a spin-poll. Also works in bare (non-`--warden`) runs with
  zero contention, so fixture code doesn't need to branch on whether
  warden is active.
- `--warden-run-dir` (internal): the controller's per-invocation shared
  temp directory, forwarded to every worker subprocess.

### Changed

- `--numprocesses`: now rejects 0/negative/unparseable values with a
  `pytest.UsageError` instead of silently clamping to 1, matching
  `--timeout`/`--warden-chunk-size`'s existing validation convention.

## [0.1.0]

### Added

- `--warden`: distribute a pytest run across N hard-killable worker
  subprocesses, each wrapped in a Windows Job Object (POSIX process-group
  fallback), so a timeout guarantees a kill of the whole process tree
  instead of a best-effort thread-based interrupt.
- `--numprocesses` / `--timeout`: worker count and per-test timeout,
  overridable per test with `@pytest.mark.timeout(N)`.
- `--maxfail`: forwarded to workers and enforced across the whole
  distributed run.
- `--cov` support: coverage measured per worker and combined into a
  single `.coverage` file.
- LPT (longest-processing-time-first) batch scheduling using a SQLite
  history store (`--warden-history-db`) of past durations/outcomes, so
  historically-slow tests don't stack on the same worker.
- `--warden-quarantine-flaky`: a failure on a test with both passes and
  failures in its recent history reports as `xfail` instead of `failed`.
- `--warden-work-stealing` / `--warden-chunk-size`: an alternative,
  dynamic chunk-based scheduler for suites where static LPT batching's
  upfront duration estimates keep being wrong.
- `--last-failed` / `--failed-first` support (pytest's own cache-based
  rerun machinery works transparently; `--failed-first`'s priority is
  additionally respected by both scheduling modes).
- Report merging via real pytest hooks (`pytest_report_to_serializable` /
  `pytest_runtest_logreport`) — junitxml, terminal output, and exit codes
  all work unmodified, with no custom merge code.
- A hard-killed or crashed worker's never-started remainder gets exactly
  one retry on a fresh worker; a `KeyboardInterrupt` on the controller
  terminates every tracked worker instead of orphaning them.
