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
- Live terminal reporting: a run previously stayed completely silent until
  the final summary line, giving no sign of how many workers were in play
  or what they were doing. `--warden` now prints a one-line startup banner
  (worker count and scheduling mode) before dispatch begins, shown by
  default and suppressed under `-v`/`-vv` (see below); a
  `[done/total] worker N -> nodeid RESULT` line as each test finishes
  (`RESULT` being pytest's own PASSED/FAILED/SKIPPED/XFAIL/... word, via
  the same `pytest_report_teststatus` hook the builtin terminal reporter
  itself uses), likewise shown by default (this is the only place a bare,
  non-`-v` run learns which test ran on which worker and how it came out)
  and suppressed under `-v`/`-vv` (pytest's own verbose reporter already
  prints that same nodeid plus outcome on its own line, so ours would
  just repeat it) and under `-q`/`-qq`; and an
  always-on `warden: worker N didn't finish its batch (K test(s) left) --
  recreating a fresh worker to pick them up` line wherever a worker crash
  or hard-kill silently requeued the rest of its batch, previously visible
  only as an unexplained gap before the retry's own results appeared. The
  final `warden: distributed across N worker(s)` summary line is likewise
  now suppressed under `-v`/`-vv` -- with `-v`, per-test worker attribution
  is already visible throughout the run, so restating the worker count
  again at the end is redundant. Also (the same fix `pytest-xdist` itself
  applies in `xdist/plugin.py`'s `pytest_configure`): non-verbose runs no
  longer show pytest's own bare `path/to/file.py ` line with the concurrent
  replay across several workers constantly switching files -- disables
  `TerminalReporter.showfspath`, same as xdist, so a plain dot stream shows
  instead of that line getting orphaned (no letter ever lands on it before
  the next file switches it away) once or twice per test. In non-verbose
  mode, the dot/letter itself (and the occasional "[ X%]" marker) is now
  also suppressed for the same reason our own `[done/total] ... RESULT`
  line already exists -- pytest core has no public flag for "run
  `pytest_runtest_logreport` but skip just its own write" (verbosity is
  the only knob it exposes, and it's all-or-nothing across far more than
  this one write), so `_replay_event` temporarily swallows
  `TerminalReporter`'s own `_tw.write` for the duration of that single
  replayed call, restoring it immediately after. `_add_stats` -- what the
  final short summary/FAILURES section actually depends on -- runs
  earlier in the same method, unguarded by this write, so it's unaffected.

### Changed

- `requires-python` raised from `>=3.9` to `>=3.10` (Python 3.9 reached
  its own upstream end-of-life in October 2025). This wasn't just a
  formality: `history.py`/`plugin.py` already use bare `str | None`-style
  union annotations (PEP 604) with no `from __future__ import
  annotations`, which raises `TypeError: unsupported operand type(s) for
  |: 'type' and 'NoneType'` at import time on real Python 3.9 -- the
  package's `>=3.9` claim was already false and the plugin could never
  actually load there. Confirmed by installing a real 3.9 interpreter and
  importing `pytest_warden.plugin` directly.
- `--numprocesses`: now rejects 0/negative/unparseable values with a
  `pytest.UsageError` instead of silently clamping to 1, matching
  `--timeout`/`--warden-chunk-size`'s existing validation convention.
- `_read_new_lines`: resumes from a saved `seek()`/`tell()`-derived byte
  offset instead of re-reading a worker's entire progress file from byte
  0 on every poll, without changing the torn-write safety guarantee (an
  incomplete, non-newline-terminated line is never returned until
  complete).

### Fixed

- GitHub Dependabot flagged `pytest < 9.0.3` (GHSA-6w46-j5rx-g56g /
  CVE-2025-71176, predictable `/tmp/pytest-of-{user}` naming on UNIX)
  as a moderate-severity vulnerability in this repo's own `uv.lock`: with
  `requires-python = ">=3.9"`, uv resolved pytest 8.4.2 (vulnerable, and
  the last release supporting 3.9 -- pytest never backported the fix to
  the 8.x line) for that environment marker, alongside 9.1.1 (already
  fixed) for 3.10+. Raising the floor to `>=3.10` (see "Changed" -- this
  was independently warranted anyway) leaves only the patched 9.1.1 in
  the lockfile.
- `@pytest.mark.timeout(N)` was documented (0.1.0's own changelog entry)
  as a per-test override of `--timeout`, but no code ever read the marker
  -- pytest correctly flagged it as unrecognized
  (`PytestUnknownMarkWarning: Unknown pytest.mark.timeout`), and every
  test in a `--warden` run silently shared the single global `--timeout`
  value regardless of any `timeout` mark on it. The marker is now
  registered (so it's recognized with or without `--warden`) and actually
  read per test, overriding `--timeout` for that test only; a hard-kill's
  reported timeout (in both its message and the synthetic report's
  duration) now reflects the specific budget that was in force for the
  test that got killed, not the worker's global default.
- A hard-killed test (or one reported via "worker never reached this
  test") printed pytest's own raw, unsuppressed dot/letter (no trailing
  newline) because `_report_incident`/`_report_never_ran` called
  `hook.pytest_runtest_logreport()` directly instead of going through the
  same terminal-write suppression `_replay_event` already applies to
  normal completions. Without `-v` this visually smashed into whatever
  warden text printed right after it (e.g. `FFFFwarden: worker 0 didn't
  finish its batch...`), and these tests never got the numbered `[done/
  total] worker N -> nodeid WORD` progress line every other test gets.
  Both paths now share the same suppression and progress-line logic, so
  every test -- however it ends -- reports the same way.
- The smash above could still happen under `-q`/`-qq` even after the fix
  directly above: that verbosity level intentionally still shows a raw
  dot/letter (matching vanilla `pytest -q`'s own plain-dots look), so
  suppressing it isn't an option there. `_warden_write_line` now checks
  the terminal writer's own `width_of_current_line` and emits a newline
  first whenever anything is already sitting on the current line,
  regardless of which verbosity level or code path put it there --
  `TerminalReporter.ensure_newline()` doesn't help here since it only
  acts when `currentfspath` is set, and warden permanently disables that
  (`showfspath = False`) to avoid a different, unrelated multi-worker
  interleaving artifact.
- `pytest --warden -v` could crash the whole run with an `INTERNALERROR`
  the moment any test was skipped via `@pytest.mark.skipif`/`pytest.skip()`
  (no `wasxfail`, so a bare `(path, lineno, reason)` longrepr rather than a
  full traceback object). `worker.py`'s progress channel round-trips every
  report through plain `json.dumps`/`json.loads`, which has no tuple type
  -- unlike `pytest-xdist`'s `execnet` channel, which `_pytest.reports`'
  serializable-report format was designed against and which does preserve
  tuples, `_report_kwargs_from_json` leaves a raw (non-traceback) longrepr
  untouched on the assumption the round-trip preserves its type, so it came
  back on the controller side as a list instead of the original tuple.
  That silently failed pytest's own `assert isinstance(report.longrepr,
  tuple)` in verbose-mode skip-reason reporting
  (`_pytest/terminal.py::_get_raw_skip_reason`), aborting the entire run.
  `_replay_event` now converts a replayed report's `longrepr` back to a
  tuple whenever it comes back as a list -- every other longrepr shape
  (`None`, a plain string, or a real exception-repr object) already
  survives the round-trip as its original type, so a list is unambiguously
  a tuple that lost its type over JSON.
- `test_terminate_kills_a_child_process_spawned_by_the_worker` and
  `test_hard_kill_reaches_a_grandchild_process` spawned their synthetic
  child/grandchild via the bareword `"python3"` instead of `sys.executable`.
  On a machine where `python3` resolves (ahead of the real interpreter) to
  a Windows App Execution Alias stub -- e.g. `PythonSoftwareFoundation.
  PythonManager`'s `WindowsApps\python3.exe` -- that stub hands off to a
  separate, reparented real interpreter process that is never a descendant
  of the PID `Popen` returns, so it (and anything it spawns) never joins
  the Job Object `job.assign()`/`job.terminate()` were targeting, and the
  test's own hard-kill assertion failed as a result. `_spawn_worker`
  already used `sys.executable` for real workers, so production was never
  affected -- both tests now do the same.
- A warden controller running nested inside an outer warden worker (this
  project's own self-hosted dogfood run does this) no longer inherits
  `PYTEST_WARDEN_WORKER=1` from the outer invocation's environment, which
  previously caused a `conftest.py` self-guard hookimpl to incorrectly
  suppress the inner controller's own report replay.
- `coordination.py`'s Windows lock path referenced `msvcrt.LK_UNLOCK`,
  which doesn't exist (the real constant is `LK_UNLCK`) -- every call to
  `run_once` on Windows raised `AttributeError` on unlock. Found via real
  Windows CI, not locally (macOS/Linux have no `msvcrt` module to catch
  this against).
- A worker marked as hard-killed could still have a later, legitimate
  completion event for the SAME test replayed if the underlying OS
  process didn't die instantly (Job Object/process-group termination
  isn't guaranteed instant) -- silently double-reporting that test once
  via the hard-kill incident and once via its own late "real" outcome.
  `_poll_once` now stops reading/replaying progress-channel content from
  a worker entirely once it's been marked killed. Found via real Windows
  CI, where Job Object termination latency made this pre-existing gap
  manifest consistently; not previously caught locally.
- `--maxfail` could double-report the very test that tripped it: replaying
  its failing report and flipping `session.shouldfail` could land in the
  same poll cycle where the worker's own `logfinish` write for that test
  hadn't reached disk yet, so the immediate force-kill-and-report path in
  `_supervise` / `_run_work_stealing` reported it a second time. Both now
  give still-pending workers a short, bounded grace period to settle
  through the normal "worker exited" path (which already drains all
  remaining lines before deciding a report is owed) before force-killing.
  Found via real Windows CI, where slower per-event file I/O widened the
  race window enough to hit it reliably; the race is latent on every OS.
- A hard-killed worker on Windows could keep running to full natural
  completion instead of actually dying: `JobObject.assign` opened a
  process handle purely to make the `AssignProcessToJobObject` call and
  then discarded it, leaving no direct fallback if job-based tree
  termination didn't take (an ambient outer Job Object -- e.g. the one
  GitHub Actions' `windows-latest` runners already wrap the whole job step
  in -- can silently block `TerminateJobObject` without raising a
  Python-visible error), and `_poll_once` had no bound on how long it
  would passively wait for the process to exit on its own afterward.
  `JobObject` now retains the process handle and also calls
  `TerminateProcess` on it directly as a fallback, and the controller now
  verifies the kill actually took effect within a short bounded window,
  retrying (up to `_KILL_MAX_ATTEMPTS`, currently 3) and emitting a
  `UserWarning` on every attempt that doesn't take. Found via real Windows
  CI: a `--timeout=1` hanging test took over 30s (the full sleep duration)
  to fail instead of the expected ~1s, and its fixture teardown finalizer
  visibly ran -- proof the process was never actually killed, since a true
  hard kill cannot run teardown code. Neither `TerminateJobObject` nor
  `TerminateProcess` ever raised an exception on real Windows CI (both
  reported success every time), yet the process kept running regardless --
  `JobObject.terminate()` now also runs `taskkill /F /T` by PID as a third,
  independent fallback that doesn't depend on Job Object membership/nesting
  at all, which is what actually made the kill take effect; confirmed on a
  real `windows-latest` CI run (previously ~442s with the two hanging
  tests failing, now 102s with all 134 tests passing).

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
