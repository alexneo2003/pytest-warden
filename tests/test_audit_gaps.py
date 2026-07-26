def test_negative_chunk_size_is_rejected_instead_of_silently_running_nothing(pytester):
    pytester.makepyfile(
        """
        def test_a():
            assert True

        def test_b():
            assert True
        """
    )
    result = pytester.runpytest(
        "--warden", "--warden-work-stealing", "--warden-chunk-size=-1"
    )
    assert result.ret != 0
    result.stderr.fnmatch_lines(["*--warden-chunk-size must be a positive integer*"])


def test_zero_chunk_size_is_rejected_instead_of_silently_substituting_the_default(pytester):
    pytester.makepyfile(
        """
        def test_a():
            assert True
        """
    )
    result = pytester.runpytest(
        "--warden", "--warden-work-stealing", "--warden-chunk-size=0"
    )
    assert result.ret != 0
    result.stderr.fnmatch_lines(["*--warden-chunk-size must be a positive integer*"])
