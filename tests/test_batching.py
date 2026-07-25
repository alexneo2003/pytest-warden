def test_tests_are_distributed_across_the_requested_worker_count(pytester):
    pytester.makepyfile(
        test_a="def test_a():\n    assert True\n",
        test_b="def test_b():\n    assert True\n",
    )
    result = pytester.runpytest("--warden", "--numprocesses", "2")
    result.assert_outcomes(passed=2)
    result.stdout.fnmatch_lines(["*warden: distributed across 2 worker(s)*"])


def test_default_worker_count_is_one(pytester):
    pytester.makepyfile(
        test_a="def test_a():\n    assert True\n",
    )
    result = pytester.runpytest("--warden")
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["*warden: distributed across 1 worker(s)*"])


def test_worker_count_is_capped_at_number_of_collected_tests(pytester):
    pytester.makepyfile(
        test_a="def test_a():\n    assert True\n",
    )
    result = pytester.runpytest("--warden", "--numprocesses", "5")
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["*warden: distributed across 1 worker(s)*"])
