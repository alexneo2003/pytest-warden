import sys

pytest_plugins = ["pytester"]


def process_exists(pid):
    """Cross-platform 'is this PID still alive' check.

    os.kill(pid, 0) is the standard POSIX idiom (raises ProcessLookupError
    if the process is gone), but signal 0 isn't meaningfully supported by
    Windows -- os.kill() there raises OSError: [WinError 87] The parameter
    is incorrect regardless of whether the process actually exists, so the
    POSIX idiom can't be reused as-is.
    """
    if sys.platform == "win32":
        import win32api
        import win32con

        try:
            handle = win32api.OpenProcess(win32con.PROCESS_QUERY_INFORMATION, False, pid)
        except Exception:
            return False
        win32api.CloseHandle(handle)
        return True

    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours to signal
    return True
