"""Safe forms for tests/test_no_handles.py. Never imported or run: only parsed.

Prose may name psutil.Process(pid), psutil.process_iter() or OpenProcess: docstrings, comments and
messages are not calls.
"""
import multiprocessing
import os
from multiprocessing import Process

import psutil

me = psutil.Process()  # our own process: allowed
pids = psutil.pids()  # the one psutil call allowed on other processes
# psutil.Process(1234) in a comment is not code
message = "never call OpenProcess or psutil.pid_exists on the game"
worker = multiprocessing.Process(target=print)
other = Process(target=print)


class Tracker:
    def pid_exists(self, pid):
        return pid in pids

    def process_iter(self):
        return iter(pids)


tracker = Tracker()
tracker.pid_exists(os.getpid())
names = {"process_iter": 1, "Process": 2}
