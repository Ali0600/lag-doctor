"""A child process for the settings tests: it reports its own priority class, create time and pid.

It reads them through its own pseudo-handle (GetCurrentProcess), so the answers never pass through
lagdoctor.winapi.process, the code under test. One command per line on stdin; anything else, or EOF,
ends it. Standard library only: it runs on the base interpreter with -I.
"""
import ctypes
import os
import sys
from ctypes import wintypes

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.GetCurrentProcess.argtypes = []
k32.GetCurrentProcess.restype = wintypes.HANDLE
k32.GetPriorityClass.argtypes = [wintypes.HANDLE]
k32.GetPriorityClass.restype = wintypes.DWORD
k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
k32.GetProcessTimes.restype = wintypes.BOOL

me = k32.GetCurrentProcess()


def create_time() -> int:
    times = [wintypes.FILETIME() for _ in range(4)]
    if not k32.GetProcessTimes(me, *(ctypes.byref(t) for t in times)):
        return -1
    return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime


for line in sys.stdin:
    cmd = line.strip()
    if cmd == "prio":
        print(k32.GetPriorityClass(me), flush=True)
    elif cmd == "ctime":
        print(create_time(), flush=True)
    elif cmd == "pid":
        print(os.getpid(), flush=True)
    else:
        break
