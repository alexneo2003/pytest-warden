"""Worker-side plugin, loaded into each supervised subprocess via ``-p
pytest_warden.worker``. Reports every real pytest hook event (per-test
start/finish plus full serialized reports) as a flushed JSON line, so the
controller can both reset its watchdog deadline and reconstruct real
TestReport objects to replay through its own top-level session.
"""

import json
import os
from pathlib import Path

import pytest


class _ProgressReporter:
    def __init__(self, path, config):
        self.path = path
        self.config = config

    def pytest_runtest_logstart(self, nodeid, location):
        self._write({"kind": "logstart", "nodeid": nodeid, "location": list(location)})

    def pytest_runtest_logreport(self, report):
        data = self.config.hook.pytest_report_to_serializable(config=self.config, report=report)
        self._write({"kind": "logreport", "data": data})

    def pytest_runtest_logfinish(self, nodeid, location):
        self._write({"kind": "logfinish", "nodeid": nodeid, "location": list(location)})

    def _write(self, event):
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")
            fh.flush()
            os.fsync(fh.fileno())


def pytest_addoption(parser):
    parser.addoption(
        "--warden-progress-file",
        action="store",
        default=None,
        help="Internal: path warden's worker-side plugin appends per-test JSON events to.",
    )
    parser.addoption(
        "--warden-run-dir",
        action="store",
        default=None,
        help="Internal: shared per-invocation directory forwarded by the "
        "controller for cross-worker coordination (see "
        "pytest_warden.coordination.run_once / the warden_run_once fixture).",
    )


def pytest_configure(config):
    path = config.getoption("warden_progress_file")
    if path:
        config.pluginmanager.register(_ProgressReporter(path, config), "warden-progress-reporter")


@pytest.fixture(scope="session")
def warden_run_dir(request):
    run_dir = request.config.getoption("warden_run_dir")
    if not run_dir:
        pytest.fail(
            "warden_run_dir was requested but --warden-run-dir wasn't set -- "
            "this fixture is only meaningful inside a warden worker subprocess."
        )
    return Path(run_dir)
