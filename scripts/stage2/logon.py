"""A throwaway standard account on the runner, and a program run as it (E9).

E9 needs a caller that is not an administrator: the service must tell who that caller is, and that account must be
able to start the service and nothing more. GitHub's job runs as the built-in Administrator, so drive.py makes a local
account with a random password (it is never printed or saved), runs E9's client as it through
CreateProcessWithLogonW, and deletes it. The new process gets the caller's logon SID, which gives it the job's own
window station and desktop (Microsoft's page: the function "uses the logon SID in the caller token"). Only on a
GitHub-hosted runner (drive.py's guard). Layouts from processthreadsapi.h and WinBase.h (Windows SDK 10.0.26100), x64.
"""
from __future__ import annotations

import ctypes
import secrets
import subprocess
import time
from ctypes import wintypes

LOGON_WITH_PROFILE = 0x00000001
CREATE_NO_WINDOW = 0x08000000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
WAIT_OBJECT_0, WAIT_TIMEOUT = 0, 0x102
STILL_ACTIVE = 259


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
                ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD), ("dwXCountChars", wintypes.DWORD),
                ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD), ("lpReserved2", ctypes.c_void_p),
                ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE), ("dwProcessId", wintypes.DWORD),
                ("dwThreadId", wintypes.DWORD)]


assert ctypes.sizeof(STARTUPINFOW) == 104
assert ctypes.sizeof(PROCESS_INFORMATION) == 24

_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_advapi32.CreateProcessWithLogonW.argtypes = [
    wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPWSTR,
    wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(STARTUPINFOW),
    ctypes.POINTER(PROCESS_INFORMATION)]
_advapi32.CreateProcessWithLogonW.restype = wintypes.BOOL
_kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_kernel32.WaitForSingleObject.restype = wintypes.DWORD
_kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
_kernel32.GetExitCodeProcess.restype = wintypes.BOOL
_kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
_kernel32.TerminateProcess.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL


def new_password() -> str:
    """A random password that meets Windows' complexity rule (three of: upper, lower, digit, symbol)."""
    return secrets.token_urlsafe(18) + "Aa1!"


def _net(*args: str, answer: str | None = None) -> dict:
    """net.exe with these arguments, and `answer` typed to its question if it asks one."""
    proc = subprocess.run(["net.exe", *args], capture_output=True, text=True, timeout=60, input=answer)
    return {"exit_code": proc.returncode, "output": (proc.stdout + proc.stderr).strip()[-400:]}


def add_user(name: str, password: str) -> dict:
    """A local account in Users only (net user's default), which an administrator did not make one of its own. For a
    password over 14 characters net.exe first asks whether to go on (older Windows could not use it), and without an
    answer it makes no account (E9's first run, 2026-11-25), so it is answered yes."""
    out = _net("user", name, password, "/add", answer="Y" + chr(10))
    if password in out["output"]:
        out["output"] = "(withheld: it named the password)"
    return out


def delete_user(name: str) -> dict:
    return _net("user", name, "/delete")


def run_as(user: str, password: str, argv: list[str], cwd: str, timeout_s: float) -> dict:
    """Run argv as `user` (a local account) with its profile loaded, no window, and wait for it: {"pid", "exit_code",
    "waited_s"}, or {"winerror"} when Windows refused to start it. A process still running at the deadline is ended
    and reported with "timed_out"."""
    command = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))     # the function may write into it
    si, pi = STARTUPINFOW(), PROCESS_INFORMATION()
    si.cb = ctypes.sizeof(si)
    if not _advapi32.CreateProcessWithLogonW(user, ".", password, LOGON_WITH_PROFILE, argv[0], command,
                                             CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT, None, cwd,
                                             ctypes.byref(si), ctypes.byref(pi)):
        return {"winerror": ctypes.get_last_error()}
    t0 = time.monotonic()
    try:
        timed_out = _kernel32.WaitForSingleObject(pi.hProcess, int(timeout_s * 1000)) == WAIT_TIMEOUT
        if timed_out:
            _kernel32.TerminateProcess(pi.hProcess, 1)
            _kernel32.WaitForSingleObject(pi.hProcess, 10_000)
        code = wintypes.DWORD(STILL_ACTIVE)
        _kernel32.GetExitCodeProcess(pi.hProcess, ctypes.byref(code))
        return {"pid": int(pi.dwProcessId), "exit_code": int(code.value), "timed_out": timed_out,
                "waited_s": round(time.monotonic() - t0, 3)}
    finally:
        _kernel32.CloseHandle(pi.hThread)
        _kernel32.CloseHandle(pi.hProcess)
