"""A Windows job object that kills every process in it when LagLab exits.

LagLab's background CPU hogs must never outlive the test, even if LagLab is
killed hard. Closing the last handle to a job created with
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE terminates its processes, and Windows closes
that handle for us when our process dies, however it dies.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


# Documented x64 size; a wrong layout would make SetInformationJobObject fail.
assert ctypes.sizeof(_ExtendedLimits) == 144, ctypes.sizeof(_ExtendedLimits)

_kernel32.CreateJobObjectW.restype = wintypes.HANDLE
_kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
_kernel32.SetInformationJobObject.restype = wintypes.BOOL
_kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
_kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


class KillOnCloseJob:
    """Processes added here die when close() runs or this process exits."""

    def __init__(self) -> None:
        self._handle = _kernel32.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = _kernel32.SetInformationJobObject(
            self._handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(limits), ctypes.sizeof(limits))
        if not ok:
            err = ctypes.get_last_error()
            _kernel32.CloseHandle(self._handle)
            raise ctypes.WinError(err)

    def add(self, pid: int) -> None:
        proc = _kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not proc:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not _kernel32.AssignProcessToJobObject(self._handle, proc):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            _kernel32.CloseHandle(proc)

    def close(self) -> None:
        if self._handle:
            _kernel32.CloseHandle(self._handle)
            self._handle = None
