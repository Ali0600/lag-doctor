"""ETW rights of this process, and the one UAC relaunch lag-doctor may ask for.

Starting an ETW session (PresentMon's, the network tracker's) needs an administrator or a member of
Performance Log Users (docs/research/presentmon.md 6.1). CheckTokenMembership counts a SID only when
it is present and enabled, so a UAC-filtered admin token, whose Administrators SID is deny-only,
answers False exactly as StartTraceW refuses it (PM 6.3; verification log C7). TokenElevation is
only a proxy for that rule, which is why can_trace() does not use it.

Also here, because both must work across the elevation boundary: the user SID of this token (an
over-the-shoulder elevation runs as another account), and a named mutex that an unelevated watch and an
elevated capture child both see (a lock file would not do: the elevated child may have another profile).
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import time
from ctypes import wintypes

ADMINISTRATORS_SID = "S-1-5-32-544"
PERFORMANCE_LOG_USERS_SID = "S-1-5-32-559"

TOKEN_USER_CLASS = 1                     # TokenUser in TOKEN_INFORMATION_CLASS (SDK 10.0.19041 winnt.h)
TOKEN_ELEVATION_CLASS = 20               # TokenElevation in TOKEN_INFORMATION_CLASS (SDK 10.0.19041 winnt.h)
CURRENT_PROCESS_TOKEN = -4               # GetCurrentProcessToken(): pseudo-handle, never closed (processthreadsapi.h)
ERROR_ACCESS_DENIED = 5                  # winerror.h
ERROR_ALREADY_EXISTS = 183               # winerror.h

SEE_MASK_NOCLOSEPROCESS = 0x00000040     # shellapi.h: hand back hProcess
SEE_MASK_NOASYNC = 0x00000100            # finish the launch before returning (no message loop here)
SEE_MASK_NO_CONSOLE = 0x00008000         # the elevated child gets its own console
# Not SEE_MASK_FLAG_NO_UI: it is documented for error boxes only, and whether it also hides the consent
# prompt was not testable here (no test may raise one); missing files are refused before the call instead.
SW_SHOWNORMAL = 1
ERROR_CANCELLED = 1223                   # the user answered No at the UAC prompt (winerror.h)
WAIT_OBJECT_0, WAIT_TIMEOUT = 0x0, 0x102
INFINITE_POLL_MS = 250                   # wait in slices so Ctrl+C still reaches Python


class TOKEN_ELEVATION(ctypes.Structure):
    _fields_ = [("TokenIsElevated", wintypes.DWORD)]


class SID_AND_ATTRIBUTES(ctypes.Structure):
    # TOKEN_USER is exactly one of these (winnt.h).
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class SHELLEXECUTEINFOW(ctypes.Structure):
    # shellapi.h; the union of hIcon/hMonitor is one HANDLE.
    _fields_ = [("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG), ("hwnd", wintypes.HWND),
                ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
                ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]


# The research notes give no sizes for these two; these are the x64 sizes from the SDK headers above.
assert ctypes.sizeof(TOKEN_ELEVATION) == 4
assert ctypes.sizeof(SID_AND_ATTRIBUTES) == 16
assert ctypes.sizeof(SHELLEXECUTEINFOW) == 112

_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)

_advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.DWORD)]
_advapi32.GetTokenInformation.restype = wintypes.BOOL
_advapi32.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
_advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
_advapi32.CheckTokenMembership.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL)]
_advapi32.CheckTokenMembership.restype = wintypes.BOOL
_advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
_advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
_kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CreateMutexW.restype = wintypes.HANDLE
_kernel32.LocalFree.argtypes = [ctypes.c_void_p]
_kernel32.LocalFree.restype = ctypes.c_void_p
_kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_kernel32.WaitForSingleObject.restype = wintypes.DWORD
_kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
_kernel32.GetExitCodeProcess.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL
_shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
_shell32.ShellExecuteExW.restype = wintypes.BOOL

# Indirection so tests can replace the call; nothing in the test suite may raise a real UAC prompt.
_ShellExecuteExW = _shell32.ShellExecuteExW
_CreateMutexW = _kernel32.CreateMutexW            # tests replace it to stage error codes


def _winerror() -> OSError:
    return ctypes.WinError(ctypes.get_last_error())


def is_elevated() -> bool:
    """TokenElevation of our own process token: True when this process runs elevated."""
    info = TOKEN_ELEVATION()
    size = wintypes.DWORD()
    if not _advapi32.GetTokenInformation(wintypes.HANDLE(CURRENT_PROCESS_TOKEN), TOKEN_ELEVATION_CLASS,
                                         ctypes.byref(info), ctypes.sizeof(info), ctypes.byref(size)):
        raise _winerror()
    return bool(info.TokenIsElevated)


def _is_member(sid: str) -> bool:
    """CheckTokenMembership(NULL, sid): present AND enabled in the calling thread's token."""
    psid = ctypes.c_void_p()
    if not _advapi32.ConvertStringSidToSidW(sid, ctypes.byref(psid)):
        raise _winerror()
    try:
        member = wintypes.BOOL()
        if not _advapi32.CheckTokenMembership(None, psid, ctypes.byref(member)):
            raise _winerror()
        return bool(member.value)
    finally:
        _kernel32.LocalFree(psid)


def in_performance_log_users() -> bool:
    return _is_member(PERFORMANCE_LOG_USERS_SID)


def can_trace() -> bool:
    """StartTraceW's own rule: an enabled Administrators SID or Performance Log Users (PM 6.3)."""
    return _is_member(ADMINISTRATORS_SID) or _is_member(PERFORMANCE_LOG_USERS_SID)


def token_user_sid() -> str:
    """The user SID of this process's token, e.g. "S-1-5-21-...-1001" (TokenUser)."""
    token = wintypes.HANDLE(CURRENT_PROCESS_TOKEN)
    size = wintypes.DWORD()
    _advapi32.GetTokenInformation(token, TOKEN_USER_CLASS, None, 0, ctypes.byref(size))   # asks for the size
    if not size.value:
        raise _winerror()
    buf = ctypes.create_string_buffer(size.value)
    if not _advapi32.GetTokenInformation(token, TOKEN_USER_CLASS, buf, size, ctypes.byref(size)):
        raise _winerror()
    text = ctypes.c_void_p()
    if not _advapi32.ConvertSidToStringSidW(SID_AND_ATTRIBUTES.from_buffer(buf).Sid, ctypes.byref(text)):
        raise _winerror()
    try:
        return ctypes.wstring_at(text.value)
    finally:
        _kernel32.LocalFree(text)


def hold_named_mutex(name: str) -> int | None:
    """Create the named mutex and return its handle (keep it open while holding), or None when one of that
    name already exists. Both ERROR_ALREADY_EXISTS and ERROR_ACCESS_DENIED mean that: a copy made by an
    elevated process, or by another account, exists but this token may not open it. OSError otherwise.

    The handle is the lock, not the mutex's ownership: the object lives exactly as long as some process
    holds a handle to it, and Windows closes a dead process's handles.
    """
    if not isinstance(name, str) or not name or "\0" in name or len(name) > 260:
        raise ValueError(f"bad mutex name: {name!r}")
    ctypes.set_last_error(0)
    handle = _CreateMutexW(None, False, name)
    err = ctypes.get_last_error()
    if not handle:
        if err == ERROR_ACCESS_DENIED:
            return None
        raise ctypes.WinError(err)
    if err == ERROR_ALREADY_EXISTS:
        _kernel32.CloseHandle(handle)
        return None
    return handle


def release_named_mutex(handle: int | None) -> None:
    if handle:
        _kernel32.CloseHandle(handle)


class ElevatedChild:
    """The elevated child's process handle (from ShellExecuteExW): wait for it, then close the handle."""

    def __init__(self, info: SHELLEXECUTEINFOW) -> None:
        self._info: SHELLEXECUTEINFOW | None = info

    def wait(self, timeout_s: float | None = None) -> int | None:
        """The exit code once the child has ended, or None if it still runs after timeout_s.

        Waits in short slices, so Ctrl+C reaches Python as KeyboardInterrupt; the handle stays open then.
        """
        info = self._info
        if info is None:
            raise ValueError("the process handle was already closed")
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        while True:
            r = _kernel32.WaitForSingleObject(info.hProcess, INFINITE_POLL_MS)
            if r == WAIT_OBJECT_0:
                break
            if r != WAIT_TIMEOUT:
                raise _winerror()
            if deadline is not None and time.monotonic() >= deadline:
                return None
        code = wintypes.DWORD()
        if not _kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code)):
            raise _winerror()
        return code.value

    def close(self) -> None:
        """Close the handle (the child itself keeps running). Idempotent."""
        info, self._info = self._info, None
        if info is None or not info.hProcess:
            return
        _kernel32.CloseHandle(info.hProcess)


def relaunch_elevated(argv: list[str], cwd: str) -> int:
    """Run argv elevated in a new console (one UAC prompt), wait for it, and return its exit code.

    launch_elevated() plus ElevatedChild.wait(); see there.
    """
    child = launch_elevated(argv, cwd)
    try:
        return child.wait()
    finally:
        child.close()


def launch_elevated(argv: list[str], cwd: str) -> ElevatedChild:
    """Start argv elevated in a new console (one UAC prompt) and return its handle without waiting.

    Raises PermissionError (winerror 1223) when the user declines the prompt, OSError otherwise.
    argv[0] and cwd must be absolute: an elevated launch must never search PATH for its program.
    """
    if not argv or not all(isinstance(a, str) and a and "\0" not in a for a in argv):
        raise ValueError(f"bad argv for an elevated launch: {argv!r}")
    if not isinstance(cwd, str) or "\0" in cwd:
        raise ValueError(f"bad folder for an elevated launch: {cwd!r}")
    # Absolute even when a relative name exists here: ShellExecuteExW resolves names its own way (App Paths).
    if not os.path.isabs(argv[0]) or not os.path.isabs(cwd):
        raise ValueError(f"an elevated launch needs an absolute program and folder: {argv[0]!r} in {cwd!r}")
    if not os.path.isfile(argv[0]) or not os.path.isdir(cwd):
        raise ValueError(f"nothing to launch: {argv[0]!r} or the folder {cwd!r} does not exist")
    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC | SEE_MASK_NO_CONSOLE
    info.lpVerb = "runas"
    info.lpFile = argv[0]
    info.lpParameters = subprocess.list2cmdline(argv[1:])     # the MS C runtime quoting python.exe parses
    info.lpDirectory = cwd
    info.nShow = SW_SHOWNORMAL
    if not _ShellExecuteExW(ctypes.byref(info)):
        err = ctypes.get_last_error()
        if err == ERROR_CANCELLED:
            raise PermissionError(13, "the administrator prompt was declined", None, ERROR_CANCELLED)
        raise ctypes.WinError(err)
    if not info.hProcess:
        raise OSError("ShellExecuteExW started the program but returned no process handle")
    return ElevatedChild(info)
