"""The service control manager from Python: a service's state and PID, a start with arguments, its service SID.

The experiments configure their services with sc.exe (create, privs, sidtype, triggerinfo, delete), because
sc.exe's own read-back commands (qc, qprivs, qsidtype, qtriggerinfo, showsid) then give an independent record of
what was set. Only what sc.exe cannot do cleanly is here: polling the state with a deadline, and StartServiceW
with arguments (they reach the service's ServiceMain, not its command line). Layouts from winsvc.h (Windows SDK
10.0.19041), x64. status() only reads; start() is used on the throwaway runner only (drive.py).
"""
from __future__ import annotations

import ctypes
import hashlib
import struct
import subprocess
import time
from ctypes import wintypes

SC_MANAGER_CONNECT = 0x0001
SERVICE_QUERY_STATUS, SERVICE_START = 0x0004, 0x0010
SC_STATUS_PROCESS_INFO = 0
STATES = {1: "stopped", 2: "start_pending", 3: "stop_pending", 4: "running", 5: "continue_pending",
          6: "pause_pending", 7: "paused"}
ERROR_SERVICE_SPECIFIC_ERROR = 1066
POLL_S = 0.1


class SERVICE_STATUS_PROCESS(ctypes.Structure):
    _fields_ = [("dwServiceType", wintypes.DWORD), ("dwCurrentState", wintypes.DWORD),
                ("dwControlsAccepted", wintypes.DWORD), ("dwWin32ExitCode", wintypes.DWORD),
                ("dwServiceSpecificExitCode", wintypes.DWORD), ("dwCheckPoint", wintypes.DWORD),
                ("dwWaitHint", wintypes.DWORD), ("dwProcessId", wintypes.DWORD), ("dwServiceFlags", wintypes.DWORD)]


assert ctypes.sizeof(SERVICE_STATUS_PROCESS) == 36

_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
_advapi32.OpenSCManagerW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
_advapi32.OpenSCManagerW.restype = ctypes.c_void_p
_advapi32.OpenServiceW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, wintypes.DWORD]
_advapi32.OpenServiceW.restype = ctypes.c_void_p
_advapi32.QueryServiceStatusEx.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                           ctypes.POINTER(wintypes.DWORD)]
_advapi32.QueryServiceStatusEx.restype = wintypes.BOOL
_advapi32.StartServiceW.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.LPCWSTR)]
_advapi32.StartServiceW.restype = wintypes.BOOL
_advapi32.CloseServiceHandle.argtypes = [ctypes.c_void_p]
_advapi32.CloseServiceHandle.restype = wintypes.BOOL


class _Service:
    """An open service handle with `access`, closed with its manager when the block ends."""

    def __init__(self, name: str, access: int) -> None:
        self.name, self.access = name, access
        self.manager = self.handle = None

    def __enter__(self) -> int:
        self.manager = _advapi32.OpenSCManagerW(None, None, SC_MANAGER_CONNECT)
        if not self.manager:
            raise ctypes.WinError(ctypes.get_last_error())
        self.handle = _advapi32.OpenServiceW(self.manager, self.name, self.access)
        if not self.handle:
            err = ctypes.get_last_error()
            _advapi32.CloseServiceHandle(self.manager)
            raise ctypes.WinError(err)
        return self.handle

    def __exit__(self, *_exc) -> None:
        _advapi32.CloseServiceHandle(self.handle)
        _advapi32.CloseServiceHandle(self.manager)


def status(name: str) -> dict:
    """{state, pid, win32_exit_code, service_exit_code, checkpoint, wait_hint} for one service."""
    with _Service(name, SERVICE_QUERY_STATUS) as handle:
        st, needed = SERVICE_STATUS_PROCESS(), wintypes.DWORD(0)
        if not _advapi32.QueryServiceStatusEx(handle, SC_STATUS_PROCESS_INFO, ctypes.byref(st), ctypes.sizeof(st),
                                              ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
    return {"state": STATES.get(st.dwCurrentState, st.dwCurrentState), "pid": int(st.dwProcessId),
            "win32_exit_code": int(st.dwWin32ExitCode), "service_exit_code": int(st.dwServiceSpecificExitCode),
            "checkpoint": int(st.dwCheckPoint), "wait_hint": int(st.dwWaitHint)}


def start(name: str, args: list[str]) -> None:
    """StartServiceW: `args` reach ServiceMain after the service's own name."""
    if any(not isinstance(a, str) or "\x00" in a for a in args):
        raise ValueError(f"service arguments must be strings without NUL: {args!r}")
    array = (wintypes.LPCWSTR * max(len(args), 1))(*args)
    with _Service(name, SERVICE_START | SERVICE_QUERY_STATUS) as handle:
        if not _advapi32.StartServiceW(handle, len(args), array if args else None):
            raise ctypes.WinError(ctypes.get_last_error())


def wait_for(name: str, states: set[str], timeout: float) -> dict:
    """Poll until the service is in one of `states` or `timeout` seconds of wall clock have passed; the last
    status, with "timed_out" and "waited_s" added."""
    t0 = time.monotonic()
    while True:
        st = status(name)
        waited = time.monotonic() - t0
        if st["state"] in states or waited >= timeout:
            return {**st, "timed_out": st["state"] not in states, "waited_s": round(waited, 3)}
        time.sleep(POLL_S)


def service_sid(name: str) -> str:
    """The SID Windows derives for NT SERVICE\\<name>: S-1-5-80- and five sub-authorities, the SHA-1 of the
    upper-cased name in UTF-16LE read as little-endian DWORDs (checked against `sc.exe showsid` in the tests)."""
    digest = hashlib.sha1(name.upper().encode("utf-16-le")).digest()
    return "S-1-5-80-" + "-".join(str(v) for v in struct.unpack("<5I", digest))


def sc(*args: str, check: bool = True) -> str:
    """Run sc.exe with these arguments and return its output; RuntimeError with the output when it fails."""
    proc = subprocess.run(["sc.exe", *args], capture_output=True, text=True, timeout=60)
    out = (proc.stdout + proc.stderr).strip()
    if check and proc.returncode != 0:
        raise RuntimeError(f"sc.exe {' '.join(args)} failed with exit code {proc.returncode}: {out}")
    return out
