"""Safe forms for tests/test_no_handles.py's spawn scan. Never imported or run: only parsed.

Prose may name subprocess.Popen, os.startfile or ShellExecuteExW: docstrings, comments and messages start nothing,
and neither do annotations, subprocess's constants and exceptions, or another object's method of the same name.
"""
from __future__ import annotations

import os
import subprocess


def wait_for(proc: subprocess.Popen, timeout: float = 1.0) -> subprocess.Popen | None:
    return proc


keep: subprocess.Popen | None = None
flags = (subprocess.PIPE, subprocess.DEVNULL, subprocess.TimeoutExpired)
message = "lag-doctor never calls os.startfile on a store link, nor ShellExecuteExW"
where = os.path.join("a", "b")
# subprocess.Popen(["x"]) in a comment is not code


class Runner:
    def run(self):
        return 1

    def Popen(self):
        return 2


r = Runner()
r.run()
r.Popen()
names = {"Popen": 1, "startfile": 2, "IShellDispatch2::ShellExecute": 3}
