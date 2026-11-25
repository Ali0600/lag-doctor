"""Windows services through the service control manager, for lag-doctor's capture service (docs/INSTALLER-PLAN.md,
Stage 4).

Two sides, ctypes only (the installed runtime has no pywin32):
- the service's own: `run(main)` hands the process to the SCM (StartServiceCtrlDispatcherW), reports start, running
  and stop (SetServiceStatus), and sets the stop event `main` watches when the SCM asks it to stop
  (RegisterServiceCtrlHandlerExW). This is how E3c ran the embeddable runtime as a service (research note,
  section 8).
- a caller's: `query` (its state and process id) and `start` (StartServiceW), which an interactive user may call
  because the install grants interactive users SERVICE_START on the service and nothing more (E9, section 12;
  DECISIONS 2026-11-25). Nothing here stops, changes or deletes a service.

Layouts from winsvc.h (Windows SDK 10.0.26100), x64.
"""
from __future__ import annotations

import ctypes
import hashlib
import struct
import threading
from ctypes import wintypes
from typing import Callable

SC_MANAGER_CONNECT = 0x0001
SERVICE_QUERY_STATUS, SERVICE_START = 0x0004, 0x0010
SC_STATUS_PROCESS_INFO = 0
SERVICE_WIN32_OWN_PROCESS = 0x10
SERVICE_STOPPED, SERVICE_START_PENDING, SERVICE_STOP_PENDING, SERVICE_RUNNING = 1, 2, 3, 4
STATES = {1: "stopped", 2: "start_pending", 3: "stop_pending", 4: "running", 5: "continue_pending",
          6: "pause_pending", 7: "paused"}
SERVICE_ACCEPT_STOP, SERVICE_ACCEPT_SHUTDOWN = 0x1, 0x4
SERVICE_CONTROL_STOP, SERVICE_CONTROL_INTERROGATE, SERVICE_CONTROL_SHUTDOWN = 1, 4, 5
NO_ERROR, ERROR_CALL_NOT_IMPLEMENTED = 0, 120
ERROR_SERVICE_ALREADY_RUNNING = 1056
ERROR_SERVICE_DOES_NOT_EXIST = 1060
ERROR_FAILED_SERVICE_CONTROLLER_CONNECT = 1063   # StartServiceCtrlDispatcherW in a process the SCM did not start
ERROR_SERVICE_SPECIFIC_ERROR = 1066
START_WAIT_HINT_MS = 10_000
STOP_WAIT_HINT_MS = 30_000

SERVICE_MAIN_FUNCTION = ctypes.WINFUNCTYPE(None, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR))
HANDLER_FUNCTION_EX = ctypes.WINFUNCTYPE(wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                         ctypes.c_void_p)


class SERVICE_TABLE_ENTRYW(ctypes.Structure):
    _fields_ = [("lpServiceName", wintypes.LPWSTR), ("lpServiceProc", SERVICE_MAIN_FUNCTION)]


class SERVICE_STATUS(ctypes.Structure):
    _fields_ = [("dwServiceType", wintypes.DWORD), ("dwCurrentState", wintypes.DWORD),
                ("dwControlsAccepted", wintypes.DWORD), ("dwWin32ExitCode", wintypes.DWORD),
                ("dwServiceSpecificExitCode", wintypes.DWORD), ("dwCheckPoint", wintypes.DWORD),
                ("dwWaitHint", wintypes.DWORD)]


class SERVICE_STATUS_PROCESS(ctypes.Structure):
    _fields_ = [*SERVICE_STATUS._fields_, ("dwProcessId", wintypes.DWORD), ("dwServiceFlags", wintypes.DWORD)]


assert ctypes.sizeof(SERVICE_TABLE_ENTRYW) == 16
assert ctypes.sizeof(SERVICE_STATUS) == 28
assert ctypes.sizeof(SERVICE_STATUS_PROCESS) == 36

_adv = ctypes.WinDLL("advapi32", use_last_error=True)
_adv.StartServiceCtrlDispatcherW.argtypes = [ctypes.POINTER(SERVICE_TABLE_ENTRYW)]
_adv.StartServiceCtrlDispatcherW.restype = wintypes.BOOL
_adv.RegisterServiceCtrlHandlerExW.argtypes = [wintypes.LPCWSTR, HANDLER_FUNCTION_EX, ctypes.c_void_p]
_adv.RegisterServiceCtrlHandlerExW.restype = ctypes.c_void_p
_adv.SetServiceStatus.argtypes = [ctypes.c_void_p, ctypes.POINTER(SERVICE_STATUS)]
_adv.SetServiceStatus.restype = wintypes.BOOL
_adv.OpenSCManagerW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
_adv.OpenSCManagerW.restype = ctypes.c_void_p
_adv.OpenServiceW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, wintypes.DWORD]
_adv.OpenServiceW.restype = ctypes.c_void_p
_adv.QueryServiceStatusEx.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                      ctypes.POINTER(wintypes.DWORD)]
_adv.QueryServiceStatusEx.restype = wintypes.BOOL
_adv.StartServiceW.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
_adv.StartServiceW.restype = wintypes.BOOL
_adv.CloseServiceHandle.argtypes = [ctypes.c_void_p]
_adv.CloseServiceHandle.restype = wintypes.BOOL


def service_sid(name: str) -> str:
    """The SID Windows derives for NT SERVICE\\<name>: S-1-5-80- and five sub-authorities, the SHA-1 of the
    upper-cased name in UTF-16LE read as little-endian DWORDs (tests/test_service.py checks it against
    `sc.exe showsid`). The capture service owns its pipe as this SID, which no other process can set."""
    digest = hashlib.sha1(name.upper().encode("utf-16-le")).digest()
    return "S-1-5-80-" + "-".join(str(v) for v in struct.unpack("<5I", digest))


# ------------------------------------------------------------------------------------- a caller's side ---

def _open(name: str, access: int):
    manager = _adv.OpenSCManagerW(None, None, SC_MANAGER_CONNECT)
    if not manager:
        raise ctypes.WinError(ctypes.get_last_error())
    handle = _adv.OpenServiceW(manager, name, access)
    if not handle:
        err = ctypes.get_last_error()
        _adv.CloseServiceHandle(manager)
        raise ctypes.WinError(err)
    return manager, handle


def _close(manager, handle) -> None:
    _adv.CloseServiceHandle(handle)
    _adv.CloseServiceHandle(manager)


def query(name: str) -> tuple[str, int]:
    """(state, process id) of one service: ("running", 1234), ("stopped", 0). OSError ERROR_SERVICE_DOES_NOT_EXIST
    when it is not installed."""
    manager, handle = _open(name, SERVICE_QUERY_STATUS)
    try:
        st, needed = SERVICE_STATUS_PROCESS(), wintypes.DWORD(0)
        if not _adv.QueryServiceStatusEx(handle, SC_STATUS_PROCESS_INFO, ctypes.byref(st), ctypes.sizeof(st),
                                         ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        _close(manager, handle)
    return STATES.get(st.dwCurrentState, str(st.dwCurrentState)), int(st.dwProcessId)


def start(name: str) -> bool:
    """StartServiceW with no arguments: True when this call started it, False when it was running already. OSError
    for anything else (ERROR_SERVICE_DOES_NOT_EXIST: not installed; ERROR_ACCESS_DENIED: not granted)."""
    manager, handle = _open(name, SERVICE_START | SERVICE_QUERY_STATUS)
    try:
        if _adv.StartServiceW(handle, 0, None):
            return True
        err = ctypes.get_last_error()
        if err == ERROR_SERVICE_ALREADY_RUNNING:
            return False
        raise ctypes.WinError(err)
    finally:
        _close(manager, handle)


# ----------------------------------------------------------------------------------- the service's side ---

class _Service:
    """One own-process service: ServiceMain runs `main(stop)` on the SCM's thread and reports its exit code."""

    def __init__(self, main: Callable[[threading.Event], int]) -> None:
        self.main = main
        self.stop = threading.Event()
        self.code: int | None = None
        self._handle = None
        self._lock = threading.Lock()
        self._checkpoint = 0
        self.main_cb = SERVICE_MAIN_FUNCTION(self._main)       # kept referenced for the process's life
        self._handler_cb = HANDLER_FUNCTION_EX(self._handler)

    def report(self, state: int, exit_code: int = 0, wait_hint_ms: int = 0) -> None:
        with self._lock:
            pending = state in (SERVICE_START_PENDING, SERVICE_STOP_PENDING)
            self._checkpoint = self._checkpoint + 1 if pending else 0
            accepted = 0 if state in (SERVICE_START_PENDING, SERVICE_STOPPED) else (SERVICE_ACCEPT_STOP
                                                                                    | SERVICE_ACCEPT_SHUTDOWN)
            st = SERVICE_STATUS(SERVICE_WIN32_OWN_PROCESS, state, accepted,
                                ERROR_SERVICE_SPECIFIC_ERROR if exit_code else NO_ERROR, exit_code,
                                self._checkpoint, wait_hint_ms)
            _adv.SetServiceStatus(self._handle, ctypes.byref(st))

    def _handler(self, control, _event_type, _data, _context) -> int:
        if control in (SERVICE_CONTROL_STOP, SERVICE_CONTROL_SHUTDOWN):
            self.stop.set()
            self.report(SERVICE_STOP_PENDING, wait_hint_ms=STOP_WAIT_HINT_MS)
            return NO_ERROR
        if control == SERVICE_CONTROL_INTERROGATE:
            return NO_ERROR
        return ERROR_CALL_NOT_IMPLEMENTED

    def _main(self, argc, argv) -> None:
        name = argv[0] if argc else ""
        self._handle = _adv.RegisterServiceCtrlHandlerExW(name, self._handler_cb, None)
        if not self._handle:
            return                                             # nothing can be reported without it
        code = 1
        try:
            self.report(SERVICE_START_PENDING, wait_hint_ms=START_WAIT_HINT_MS)
            self.report(SERVICE_RUNNING)
            code = int(self.main(self.stop))
        finally:
            self.code = code
            self.report(SERVICE_STOPPED, exit_code=code)


def run(name: str, main: Callable[[threading.Event], int]) -> int:
    """Hand this process to the SCM as the service `name`, and run main(stop) as its ServiceMain: its return value is
    the service's own exit code (0 when all went well), and `stop` is set when the SCM asks it to stop. Returns once
    the service has stopped: 0, or main's code; ERROR_FAILED_SERVICE_CONTROLLER_CONNECT when the SCM did not start
    this process (then nothing ran)."""
    service = _Service(main)
    table = (SERVICE_TABLE_ENTRYW * 2)()                      # zeroed: the second entry ends the table
    table[0].lpServiceName = name                              # ignored for an own-process service, never NULL
    table[0].lpServiceProc = service.main_cb
    if not _adv.StartServiceCtrlDispatcherW(table):
        return ctypes.get_last_error() or ERROR_FAILED_SERVICE_CONTROLLER_CONNECT
    return service.code or 0
