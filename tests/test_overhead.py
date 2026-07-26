"""Performance/overhead sanity checks -- not micro-benchmarks. These use
generous bounds deliberately to avoid CI flakiness; the point is to catch an
obviously pathological regression (a dead sleep, an accidental O(n^2)), not
to enforce a tight performance budget.
"""

import inspect
import time

from pytest_warden.plugin import _read_new_lines


def test_read_new_lines_uses_seek_based_incremental_reads_not_full_reread():
    # _read_new_lines resumes from worker.byte_offset (a fh.tell() cookie)
    # instead of re-reading the whole progress file with fh.readlines()
    # every poll -- this pins the CURRENT implementation choice as a
    # source-level fact, inverted from the prior audit pass's pin test
    # (which documented the OLD full-reread behavior as a deliberately
    # unfixed finding). See tests/test_progress_channel.py for the
    # torn-write/multibyte-UTF-8 correctness proofs that motivated the
    # specific seek()/tell()-only design.
    source = inspect.getsource(_read_new_lines)
    assert "readlines()" not in source
    assert "seek(" in source


def test_no_pathological_sleep_beyond_the_documented_poll_interval(pytester):
    pytester.makepyfile(
        """
        def test_ok():
            assert True
        """
    )
    start = time.monotonic()
    result = pytester.runpytest("--warden")
    elapsed = time.monotonic() - start
    result.assert_outcomes(passed=1)
    assert elapsed < 5.0, (
        f"a single trivial test took {elapsed}s under warden -- looks like an "
        f"unexpected sleep somewhere beyond worker startup + _POLL_INTERVAL"
    )


def test_many_short_tests_wall_clock_overhead_is_reasonable_relative_to_bare_pytest(pytester):
    body = "\n".join(f"def test_{i}():\n    pass\n" for i in range(100))
    pytester.makepyfile(body)

    start = time.monotonic()
    bare = pytester.runpytest()
    bare_elapsed = time.monotonic() - start
    bare.assert_outcomes(passed=100)

    start = time.monotonic()
    warden = pytester.runpytest("--warden", "--numprocesses=1")
    warden_elapsed = time.monotonic() - start
    warden.assert_outcomes(passed=100)

    # Generous multiplier + additive slack deliberately -- this is a sanity
    # ceiling against a gross regression (e.g. an accidental O(n^2) or a
    # dead wait), not a tight performance budget subject to CI noise.
    assert warden_elapsed < bare_elapsed * 10 + 5.0, (
        f"warden overhead disproportionate to bare pytest: "
        f"bare={bare_elapsed:.2f}s warden={warden_elapsed:.2f}s"
    )
