def test_warden_flag_is_recognized(pytester):
    pytester.makepyfile(
        """
        def test_ok():
            assert True
        """
    )
    result = pytester.runpytest("--warden")
    result.stderr.no_fnmatch_line("*unrecognized arguments*")
