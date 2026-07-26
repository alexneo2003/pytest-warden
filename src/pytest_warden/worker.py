"""Worker-side plugin, loaded into each supervised subprocess via ``-p
pytest_warden.worker``. Reports every real pytest hook event (per-test
start/finish plus full serialized reports) as a flushed JSON line, so the
controller can both reset its watchdog deadline and reconstruct real
TestReport objects to replay through its own top-level session.
"""

import json
import os


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


def pytest_configure(config):
    path = config.getoption("warden_progress_file")
    if path:
        config.pluginmanager.register(_ProgressReporter(path, config), "warden-progress-reporter")
