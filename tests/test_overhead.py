"""Performance/overhead sanity checks -- not micro-benchmarks. These use
generous bounds deliberately to avoid CI flakiness; the point is to catch an
obviously pathological regression (a dead sleep, an accidental O(n^2)), not
to enforce a tight performance budget.
"""

import inspect
import time

from pytest_warden.plugin import _read_new_lines


def test_read_new_lines_currently_rereads_the_whole_file_every_call_documents_current_behavior():
    # _read_new_lines has no seek()-based resume -- it calls fh.readlines()
    # on the ENTIRE progress file every poll (every _POLL_INTERVAL, for the
    # life of the worker), then slices to only the new lines. For a worker
    # with a long progress history this is a real, moderate-priority
    # inefficiency (each poll re-reads and re-decodes bytes it already
    # consumed), but switching to byte-offset/seek()-based incremental
    # reads is a genuine behavior change to a correctness-verified, central
    # code path -- NOT fixed in this pass; surfaced as a documented
    # follow-up recommendation instead, consistent with area 12 being
    # explicitly lowest-priority. This test pins down the CURRENT
    # implementation choice as a source-level fact so a future change is a
    # deliberate, visible decision rather than an accidental behavior
    # change nobody notices.
    source = inspect.getsource(_read_new_lines)
    assert "readlines()" in source
    assert "seek(" not in source, (
        "looks like _read_new_lines now uses seek()-based incremental reads -- "
        "the O(total-lines-so-far) per-poll re-read finding this test documents "
        "has apparently been fixed; update README/this test accordingly"
    )


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
