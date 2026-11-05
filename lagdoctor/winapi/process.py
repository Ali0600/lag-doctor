"""Priority class of the user's own background processes. The ONLY module that opens other processes.

lag-doctor never opens the game (DESIGN.md, Principle 1). This module serves one fix,
fixes/priority.py, which lowers the priority of one of the user's own background processes;
SetPriorityClass needs a handle with PROCESS_SET_INFORMATION (WT 9.1). Every function opens the
process with the least access its calls need and closes the handle before it returns. Which
processes may be changed at all is policy, decided in fixes/priority.py, never here.

Identity: Windows reuses a PID once its process is gone, so every function takes an optional
`create_time`, in FILETIME ticks (100 ns since 1601-01-01 UTC): the number
SYSTEM_PROCESS_INFORMATION.CreateTime carries (WT 3.1), which is what winapi/ntquery reports.
When it is given, GetProcessTimes on the handle already held must return it. Every function also
checks that the process is still running, because a process that exited stays openable while
anyone holds a handle to it. Either failure raises ProcessGone, so a check and the change it
guards always act on the same process.
"""
from __future__ import annotations

import ctypes
import errno
import os
from contextlib import contextmanager
from ctypes import wintypes
from typing import Iterator

# Priority classes (WT 9.1; SDK um\WinBase.h 625-637).
IDLE = 0x40
BELOW_NORMAL = 0x4000
NORMAL = 0x20
ABOVE_NORMAL = 0x8000
HIGH = 0x80
REALTIME = 0x100
CLASSES = {"idle": IDLE, "below_normal": BELOW_NORMAL, "normal": NORMAL,
           "above_normal": ABOVE_NORMAL, "high": HIGH, "realtime": REALTIME}
CLASS_NAMES = {value: name for name, value in CLASSES.items()}

# Access rights. PROCESS_SET_INFORMATION: WT 9.1. PROCESS_QUERY_LIMITED_INFORMATION: LC 1.5 (L18, L27).
# Both in SDK um\winnt.h 11424 and 11427; TOKEN_QUERY in um\winnt.h 10712.
PROCESS_SET_INFORMATION = 0x0200
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008

# TOKEN_INFORMATION_CLASS values (SDK um\winnt.h 10787-10799: TokenUser = 1, TokenSessionId the 12th).
_TOKEN_USER = 1
_TOKEN_SESSION_ID = 12

# GetExitCodeProcess reports STILL_ACTIVE (STATUS_PENDING, 0x103) while the process runs
# (SDK um\minwinbase.h 392, um\winnt.h 2514).
STILL_ACTIVE = 0x103

# Win32 errors (SDK shared\winerror.h 269, 828, 1035, 1863).
ERROR_ACCESS_DENIED = 5
ERROR_INVALID_PARAMETER = 87        # what opening a PID that does not exist fails with
ERROR_INSUFFICIENT_BUFFER = 122
ERROR_MORE_DATA = 234

# Service enumeration (WT 10; SDK um\winsvc.h 93, 159, 715 and um\winnt.h 21848-21850).
SC_MANAGER_ENUMERATE_SERVICE = 0x0004
SC_ENUM_PROCESS_INFO = 0
SERVICE_WIN32 = 0x30
SERVICE_ACTIVE = 0x1

_TOKEN_BUF_MAX = 4096               # a TOKEN_USER plus its SID is under 100 bytes
_SVC_BUF_START = 64 * 1024
_SVC_BUF_MAX = 256 * 1024           # EnumServicesStatusExW returns at most 256 KB per call
_SVC_MAX_CALLS = 64


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class TOKEN_USER(ctypes.Structure):          # SDK um\winnt.h 10844
    _fields_ = [("User", SID_AND_ATTRIBUTES)]


class SERVICE_STATUS_PROCESS(ctypes.Structure):   # SDK um\winsvc.h 733
    _fields_ = [("dwServiceType", wintypes.DWORD), ("dwCurrentState", wintypes.DWORD),
                ("dwControlsAccepted", wintypes.DWORD), ("dwWin32ExitCode", wintypes.DWORD),
                ("dwServiceSpecificExitCode", wintypes.DWORD), ("dwCheckPoint", wintypes.DWORD),
                ("dwWaitHint", wintypes.DWORD), ("dwProcessId", wintypes.DWORD),
                ("dwServiceFlags", wintypes.DWORD)]


class ENUM_SERVICE_STATUS_PROCESSW(ctypes.Structure):   # SDK um\winsvc.h 773
    _fields_ = [("lpServiceName", wintypes.LPWSTR), ("lpDisplayName", wintypes.LPWSTR),
                ("ServiceStatusProcess", SERVICE_STATUS_PROCESS)]


# x64 layouts. ENUM_SERVICE_STATUS_PROCESSW is 56 bytes with dwProcessId at +44 (WT 10, verify log #99).
# TOKEN_USER is one SID_AND_ATTRIBUTES: an 8-byte pointer and a DWORD, padded to 16 (SDK um\winnt.h 9164).
assert ctypes.sizeof(ctypes.c_void_p) == 8, "lag-doctor's ctypes layouts are for 64-bit Python"
assert ctypes.sizeof(wintypes.FILETIME) == 8
assert ctypes.sizeof(TOKEN_USER) == 16
assert ctypes.sizeof(SERVICE_STATUS_PROCESS) == 36
assert ctypes.sizeof(ENUM_SERVICE_STATUS_PROCESSW) == 56
assert (ENUM_SERVICE_STATUS_PROCESSW.ServiceStatusProcess.offset
        + SERVICE_STATUS_PROCESS.dwProcessId.offset) == 44

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_adv = ctypes.WinDLL("advapi32", use_last_error=True)

_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.CloseHandle.restype = wintypes.BOOL
_k32.GetPriorityClass.argtypes = [wintypes.HANDLE]
_k32.GetPriorityClass.restype = wintypes.DWORD
_k32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_k32.SetPriorityClass.restype = wintypes.BOOL
_k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
_k32.GetProcessTimes.restype = wintypes.BOOL
_k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
_k32.GetExitCodeProcess.restype = wintypes.BOOL
_k32.LocalFree.argtypes = [ctypes.c_void_p]
_k32.LocalFree.restype = ctypes.c_void_p
_adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
_adv.OpenProcessToken.restype = wintypes.BOOL
_adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                     ctypes.POINTER(wintypes.DWORD)]
_adv.GetTokenInformation.restype = wintypes.BOOL
_adv.IsValidSid.argtypes = [ctypes.c_void_p]
_adv.IsValidSid.restype = wintypes.BOOL
_adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
_adv.ConvertSidToStringSidW.restype = wintypes.BOOL
_adv.OpenSCManagerW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
_adv.OpenSCManagerW.restype = ctypes.c_void_p
_adv.EnumServicesStatusExW.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.DWORD, wintypes.DWORD,
                                       ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                       ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
                                       wintypes.LPCWSTR]
_adv.EnumServicesStatusExW.restype = wintypes.BOOL
_adv.CloseServiceHandle.argtypes = [ctypes.c_void_p]
_adv.CloseServiceHandle.restype = wintypes.BOOL


class ProcessGone(ProcessLookupError):
    """The PID is not running, or now names a different process than the create time given."""


class PriorityNotApplied(OSError):
    """SetPriorityClass reported success but the class read back differs (REALTIME becomes HIGH, WT 9.1)."""

    def __init__(self, pid: int, wanted: int, got: int) -> None:
        super().__init__(f"process {pid}: asked for priority class {_label(wanted)}, it has {_label(got)}")
        self.pid, self.wanted, self.got = pid, wanted, got


def _label(cls: int) -> str:
    return f"{CLASS_NAMES.get(cls, 'unknown')} (0x{cls:x})"


def _gone(message: str, winerror: int | None = None) -> ProcessGone:
    exc = ProcessGone(errno.ESRCH, message)
    exc.winerror = winerror
    return exc


def _last_error() -> OSError:
    return ctypes.WinError(ctypes.get_last_error())


def _check_pid(pid: object) -> None:
    if isinstance(pid, bool) or not isinstance(pid, int) or not 0 < pid <= 0xFFFFFFFF:
        raise ValueError(f"not a process id: {pid!r}")


def _check_create_time(create_time: object) -> None:
    if create_time is None:
        return
    if isinstance(create_time, bool) or not isinstance(create_time, int) or not 0 < create_time < 2**63:
        raise ValueError(f"not a process create time (FILETIME ticks): {create_time!r}")


def _open(pid: int, access: int) -> int:
    """A handle with exactly `access`; ProcessGone if there is no such process, else OSError."""
    handle = _k32.OpenProcess(access, False, pid)
    if not handle:
        err = ctypes.get_last_error()
        if err == ERROR_INVALID_PARAMETER:
            raise _gone(f"process {pid} is not running", err)
        raise ctypes.WinError(err)
    return handle


def _creation_time(handle: int) -> int:
    created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
    if not _k32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel),
                                ctypes.byref(user)):
        raise _last_error()
    return (created.dwHighDateTime << 32) | created.dwLowDateTime


def _confirm(handle: int, pid: int, create_time: int | None) -> None:
    """Raise ProcessGone unless the handle is a running process with that create time."""
    code = wintypes.DWORD()
    if not _k32.GetExitCodeProcess(handle, ctypes.byref(code)):
        raise _last_error()
    if code.value != STILL_ACTIVE:
        raise _gone(f"process {pid} has exited (exit code {code.value})")
    if create_time is not None:
        got = _creation_time(handle)
        if got != create_time:
            raise _gone(f"process id {pid} now belongs to another process (created {got}, expected {create_time})")


@contextmanager
def _held(pid: int, access: int, create_time: int | None) -> Iterator[int]:
    _check_pid(pid)
    _check_create_time(create_time)
    handle = _open(pid, access)
    try:
        _confirm(handle, pid, create_time)
        yield handle
    finally:
        _k32.CloseHandle(handle)


def _class_of(handle: int) -> int:
    cls = _k32.GetPriorityClass(handle)
    if not cls:
        raise _last_error()
    return cls


# ------------------------------------------------------------------ public --

def creation_time(pid: int) -> int:
    """The process's create time in FILETIME ticks (GetProcessTimes), for callers that hold only a PID."""
    with _held(pid, PROCESS_QUERY_LIMITED_INFORMATION, None) as handle:
        return _creation_time(handle)


def priority_class(pid: int, create_time: int | None = None) -> int:
    """GetPriorityClass through a PROCESS_QUERY_LIMITED_INFORMATION handle."""
    with _held(pid, PROCESS_QUERY_LIMITED_INFORMATION, create_time) as handle:
        return _class_of(handle)


def set_priority_class(pid: int, cls: int, create_time: int | None = None) -> None:
    """SetPriorityClass, then read the class back on the same handle.

    A success return proves nothing (REALTIME silently becomes HIGH, WT 9.1), so a class that reads
    back different raises PriorityNotApplied. Only the six real classes are accepted: the
    PROCESS_MODE_BACKGROUND_* values are not classes and apply only to the caller's own process.
    """
    if isinstance(cls, bool) or cls not in CLASS_NAMES:
        raise ValueError(f"not a priority class: {cls!r}")
    with _held(pid, PROCESS_SET_INFORMATION | PROCESS_QUERY_LIMITED_INFORMATION, create_time) as handle:
        if not _k32.SetPriorityClass(handle, cls):
            raise _last_error()
        got = _class_of(handle)
    if got != cls:
        raise PriorityNotApplied(pid, cls, got)


def _token_identity(pid: int, create_time: int | None) -> tuple[str, int]:
    """(user SID as text, session id) from the process token; PermissionError if either open is refused."""
    with _held(pid, PROCESS_QUERY_LIMITED_INFORMATION, create_time) as handle:
        token = wintypes.HANDLE()
        if not _adv.OpenProcessToken(handle, TOKEN_QUERY, ctypes.byref(token)):
            raise _last_error()
        try:
            return _token_sid(token), _token_session(token)
        finally:
            _k32.CloseHandle(token)


def _token_sid(token: wintypes.HANDLE) -> str:
    size = 256
    while True:
        buf = ctypes.create_string_buffer(size)
        need = wintypes.DWORD()
        if _adv.GetTokenInformation(token, _TOKEN_USER, buf, size, ctypes.byref(need)):
            break
        err = ctypes.get_last_error()
        if err != ERROR_INSUFFICIENT_BUFFER or not size < need.value <= _TOKEN_BUF_MAX:
            raise ctypes.WinError(err)
        size = need.value
    sid = TOKEN_USER.from_buffer(buf).User.Sid      # points into buf, which stays alive below
    if not sid or not _adv.IsValidSid(sid):
        raise OSError("the process token holds no valid user SID")
    text = ctypes.c_void_p()
    if not _adv.ConvertSidToStringSidW(sid, ctypes.byref(text)):
        raise _last_error()
    try:
        return ctypes.wstring_at(text.value)
    finally:
        _k32.LocalFree(text)


def _token_session(token: wintypes.HANDLE) -> int:
    session, need = wintypes.DWORD(), wintypes.DWORD()
    if not _adv.GetTokenInformation(token, _TOKEN_SESSION_ID, ctypes.byref(session), ctypes.sizeof(session),
                                    ctypes.byref(need)):
        raise _last_error()
    return session.value


def user_sid(pid: int, create_time: int | None = None) -> str:
    """The token user's SID as text ("S-1-5-21-..."); PermissionError if the process or its token refuses."""
    return _token_identity(pid, create_time)[0]


def session_id(pid: int, create_time: int | None = None) -> int:
    """The token's session (0 = services and system processes)."""
    return _token_identity(pid, create_time)[1]


_own: list[str] = []


def _own_sid() -> str:
    if not _own:
        _own.append(_token_identity(os.getpid(), None)[0])
    return _own[0]


def same_user(pid: int, create_time: int | None = None) -> bool:
    """True when the process token's user SID equals ours."""
    return user_sid(pid, create_time) == _own_sid()


def service_pids() -> dict[int, list[str]]:
    """PID -> names of the active Win32 services it hosts, from the service manager (no process handles)."""
    scm = _adv.OpenSCManagerW(None, None, SC_MANAGER_ENUMERATE_SERVICE)
    if not scm:
        raise _last_error()
    try:
        found: dict[int, list[str]] = {}
        resume, need, count = wintypes.DWORD(0), wintypes.DWORD(0), wintypes.DWORD(0)
        size = _SVC_BUF_START
        entry = ctypes.sizeof(ENUM_SERVICE_STATUS_PROCESSW)
        for _ in range(_SVC_MAX_CALLS):
            buf = ctypes.create_string_buffer(size)
            ok = _adv.EnumServicesStatusExW(scm, SC_ENUM_PROCESS_INFO, SERVICE_WIN32, SERVICE_ACTIVE, buf, size,
                                            ctypes.byref(need), ctypes.byref(count), ctypes.byref(resume), None)
            err = 0 if ok else ctypes.get_last_error()
            if not ok and err != ERROR_MORE_DATA:
                raise ctypes.WinError(err)
            if count.value * entry > size:
                raise OSError(f"EnumServicesStatusExW reported {count.value} entries for a {size}-byte buffer")
            for i in range(count.value):
                svc = ENUM_SERVICE_STATUS_PROCESSW.from_buffer(buf, i * entry)
                pid = svc.ServiceStatusProcess.dwProcessId
                if pid:
                    found.setdefault(pid, []).append(svc.lpServiceName or "")
            if ok:
                return found
            size = min(max(size, need.value), _SVC_BUF_MAX)
        raise OSError(f"the service list did not end after {_SVC_MAX_CALLS} calls")
    finally:
        _adv.CloseServiceHandle(scm)


def is_service_or_protected(pid: int, create_time: int | None = None) -> bool:
    """True for a service's process, a session-0 process, or one that refuses a limited query of itself
    or its token (protected processes such as MsMpEng.exe do, WT 9.2). Service PIDs are recognised
    without opening the process. ProcessGone when it is not running (or the create time differs)."""
    _check_pid(pid)
    _check_create_time(create_time)
    if pid in service_pids():
        return True
    try:
        session = session_id(pid, create_time)
    except PermissionError:
        return True
    return session == 0
