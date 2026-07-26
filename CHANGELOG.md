# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
