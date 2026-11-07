"""A named-pipe server (the E3b service) and client (drive.py), through ctypes, one message each way.

Both ends use overlapped I/O, so every wait has a timeout: the server's wait for a client is what lets it stop
itself when idle, and no read can hang a run. The server makes the first and only instance
(FILE_FLAG_FIRST_PIPE_INSTANCE), refuses remote clients and takes an explicit DACL; the client connects at
identification level (SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION), as Stage 4's CLI will
(docs/INSTALLER-PLAN.md). Layouts from minwinbase.h and winbase.h (Windows SDK 10.0.19041), x64.
"""
from __future__ import annotations

import ctypes
import time
from collections import Counter
from ctypes import wintypes

PIPE_ACCESS_DUPLEX = 0x3
FILE_FLAG_OVERLAPPED = 0x40000000
FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
PIPE_TYPE_MESSAGE, PIPE_READMODE_MESSAGE, PIPE_WAIT, PIPE_REJECT_REMOTE_CLIENTS = 0x4, 0x2, 0x0, 0x8
GENERIC_READ, GENERIC_WRITE, OPEN_EXISTING = 0x80000000, 0x40000000, 3
SECURITY_SQOS_PRESENT, SECURITY_IDENTIFICATION = 0x00100000, 0x00010000
ERROR_FILE_NOT_FOUND, ERROR_ACCESS_DENIED, ERROR_BROKEN_PIPE, ERROR_PIPE_BUSY = 2, 5, 109, 231
ERROR_MORE_DATA, ERROR_PIPE_CONNECTED, ERROR_IO_INCOMPLETE, ERROR_IO_PENDING = 234, 535, 996, 997
WAIT_OBJECT_0, WAIT_TIMEOUT = 0x0, 0x102
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
SDDL_REVISION_1 = 1
MAX_MESSAGE = 4096
IO_WAIT_S = 5.0                  # one read or write on a connected pipe


class OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t), ("Offset", wintypes.DWORD),
                ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p), ("bInheritHandle", wintypes.BOOL)]


assert ctypes.sizeof(OVERLAPPED) == 32
assert ctypes.sizeof(SECURITY_ATTRIBUTES) == 24

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_adv = ctypes.WinDLL("advapi32", use_last_error=True)
_k32.CreateNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(SECURITY_ATTRIBUTES)]
_k32.CreateNamedPipeW.restype = wintypes.HANDLE
_k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                             wintypes.DWORD, wintypes.HANDLE]
_k32.CreateFileW.restype = wintypes.HANDLE
_k32.ConnectNamedPipe.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED)]
_k32.ConnectNamedPipe.restype = wintypes.BOOL
_k32.DisconnectNamedPipe.argtypes = [wintypes.HANDLE]
_k32.DisconnectNamedPipe.restype = wintypes.BOOL
_k32.GetNamedPipeClientProcessId.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]
_k32.GetNamedPipeClientProcessId.restype = wintypes.BOOL
_k32.SetNamedPipeHandleState.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
                                         ctypes.c_void_p]
_k32.SetNamedPipeHandleState.restype = wintypes.BOOL
_k32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
_k32.WaitNamedPipeW.restype = wintypes.BOOL
_k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
_k32.CreateEventW.restype = wintypes.HANDLE
_k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_k32.WaitForSingleObject.restype = wintypes.DWORD
_k32.GetOverlappedResult.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED), ctypes.POINTER(wintypes.DWORD),
                                     wintypes.BOOL]
_k32.GetOverlappedResult.restype = wintypes.BOOL
_k32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED)]
_k32.CancelIoEx.restype = wintypes.BOOL
for _io in (_k32.ReadFile, _k32.WriteFile):
    _io.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                    ctypes.POINTER(OVERLAPPED)]
    _io.restype = wintypes.BOOL
_k32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
_k32.FlushFileBuffers.restype = wintypes.BOOL
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.CloseHandle.restype = wintypes.BOOL
_k32.LocalFree.argtypes = [ctypes.c_void_p]
_k32.LocalFree.restype = ctypes.c_void_p
_adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.ULONG)]
_adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL


def _error(code: int | None = None) -> OSError:
    return ctypes.WinError(ctypes.get_last_error() if code is None else code)


def _overlapped_io(handle: int, event: int, call, buf, size: int, timeout_s: float) -> int:
    """One overlapped ReadFile or WriteFile, waited for at most timeout_s (then cancelled): bytes moved."""
    ov = OVERLAPPED(hEvent=event)
    done = wintypes.DWORD(0)
    if not call(handle, buf, size, None, ctypes.byref(ov)):
        err = ctypes.get_last_error()
        if err != ERROR_IO_PENDING:
            raise _error(err)
        if _k32.WaitForSingleObject(event, int(timeout_s * 1000)) != WAIT_OBJECT_0:
            _k32.CancelIoEx(handle, ctypes.byref(ov))
            _k32.GetOverlappedResult(handle, ctypes.byref(ov), ctypes.byref(done), True)   # the buffer is ours again
            raise TimeoutError(f"pipe I/O did not finish within {timeout_s:g} s")
    if not _k32.GetOverlappedResult(handle, ctypes.byref(ov), ctypes.byref(done), False):
        raise _error()
    return int(done.value)


def _read_message(handle: int, event: int, timeout_s: float = IO_WAIT_S) -> bytes:
    buf = ctypes.create_string_buffer(MAX_MESSAGE)
    n = _overlapped_io(handle, event, _k32.ReadFile, buf, MAX_MESSAGE, timeout_s)   # ERROR_MORE_DATA raises: bounded
    return buf.raw[:n]


def _write_message(handle: int, event: int, data: bytes, timeout_s: float = IO_WAIT_S) -> None:
    if len(data) > MAX_MESSAGE:
        raise ValueError(f"a message is at most {MAX_MESSAGE} bytes, not {len(data)}")
    buf = ctypes.create_string_buffer(data, len(data))
    if _overlapped_io(handle, event, _k32.WriteFile, buf, len(data), timeout_s) != len(data):
        raise OSError("a pipe write was cut short")


def _new_event() -> int:
    event = _k32.CreateEventW(None, True, False, None)       # manual reset: overlapped I/O resets it on start
    if not event:
        raise _error()
    return event


class PipeServer:
    """The one instance of `path` (\\\\.\\pipe\\<name>), protected by the DACL in `sddl`. Use it in a with-block."""

    def __init__(self, path: str, sddl: str) -> None:
        self.path = path
        self._sd = ctypes.c_void_p()
        self._handle = self._event = None
        self._connect: OVERLAPPED | None = None              # a ConnectNamedPipe still pending
        if not _adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, SDDL_REVISION_1,
                                                                         ctypes.byref(self._sd), None):
            raise _error()
        try:
            sa = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), self._sd, False)
            handle = _k32.CreateNamedPipeW(path, PIPE_ACCESS_DUPLEX | FILE_FLAG_OVERLAPPED | FILE_FLAG_FIRST_PIPE_INSTANCE,
                                           PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE | PIPE_WAIT
                                           | PIPE_REJECT_REMOTE_CLIENTS, 1, MAX_MESSAGE, MAX_MESSAGE, 0,
                                           ctypes.byref(sa))
            if handle in (None, INVALID_HANDLE_VALUE):
                raise _error()
            self._handle = handle
            self._event = _new_event()
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> PipeServer:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def wait_client(self, timeout_s: float) -> bool:
        """True once a client is connected; False when none came within timeout_s (the wait then stays armed)."""
        if self._connect is None:
            ov = OVERLAPPED(hEvent=self._event)
            if not _k32.ConnectNamedPipe(self._handle, ctypes.byref(ov)):
                err = ctypes.get_last_error()
                if err == ERROR_PIPE_CONNECTED:              # the client came between CreateNamedPipe and here
                    return True
                if err != ERROR_IO_PENDING:
                    raise _error(err)
            self._connect = ov
        r = _k32.WaitForSingleObject(self._event, max(0, int(timeout_s * 1000)))
        if r == WAIT_TIMEOUT:
            return False
        if r != WAIT_OBJECT_0:
            raise _error()
        ov, self._connect = self._connect, None
        done = wintypes.DWORD(0)
        if not _k32.GetOverlappedResult(self._handle, ctypes.byref(ov), ctypes.byref(done), False):
            raise _error()
        return True

    def client_pid(self) -> int | None:
        pid = wintypes.ULONG(0)
        return int(pid.value) if _k32.GetNamedPipeClientProcessId(self._handle, ctypes.byref(pid)) else None

    def serve_one(self, reply_for) -> bytes:
        """Read the connected client's message, write reply_for(message), then disconnect. The message."""
        try:
            message = _read_message(self._handle, self._event)
            _write_message(self._handle, self._event, reply_for(message))
            _k32.FlushFileBuffers(self._handle)              # the client reads it before the disconnect drops it
            return message
        finally:
            _k32.DisconnectNamedPipe(self._handle)

    def close(self) -> None:
        """Cancel a pending wait, close the pipe and free the descriptor. Idempotent."""
        handle, self._handle = self._handle, None
        if handle is not None:
            if self._connect is not None:
                _k32.CancelIoEx(handle, ctypes.byref(self._connect))
                done = wintypes.DWORD(0)
                _k32.GetOverlappedResult(handle, ctypes.byref(self._connect), ctypes.byref(done), True)
                self._connect = None
            _k32.CloseHandle(handle)
        event, self._event = self._event, None
        if event:
            _k32.CloseHandle(event)
        if self._sd.value:
            _k32.LocalFree(self._sd)
            self._sd = ctypes.c_void_p()


def request(path: str, message: bytes, timeout_s: float, retry_s: float = 0.02) -> dict:
    """Connect to `path`, retrying while it is missing or busy for at most timeout_s, and trade one message.

    {"reply": bytes, "attempts", "errors": {winerror: count} before the connect, "first_error",
    "connect_s": from the first attempt to the open handle, "total_s"}. TimeoutError (with the same counts in
    its .details) when no connect succeeded in time."""
    t0 = time.monotonic()
    attempts, errors, first = 0, Counter(), None
    while True:
        attempts += 1
        handle = _k32.CreateFileW(path, GENERIC_READ | GENERIC_WRITE, 0, None, OPEN_EXISTING,
                                  FILE_FLAG_OVERLAPPED | SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION, None)
        if handle not in (None, INVALID_HANDLE_VALUE):
            break
        err = ctypes.get_last_error()
        errors[err] += 1
        first = err if first is None else first
        if time.monotonic() - t0 >= timeout_s:
            exc = TimeoutError(f"{path} did not accept a connection within {timeout_s:g} s")
            exc.details = {"attempts": attempts, "errors": dict(errors), "first_error": first}
            raise exc
        if err == ERROR_PIPE_BUSY:
            _k32.WaitNamedPipeW(path, 200)
        else:
            time.sleep(retry_s)
    connect_s = time.monotonic() - t0
    event = None
    try:
        mode = wintypes.DWORD(PIPE_READMODE_MESSAGE)
        if not _k32.SetNamedPipeHandleState(handle, ctypes.byref(mode), None, None):
            raise _error()
        event = _new_event()
        _write_message(handle, event, message)
        reply = _read_message(handle, event)
    finally:
        _k32.CloseHandle(handle)
        if event:
            _k32.CloseHandle(event)
    return {"reply": reply, "attempts": attempts, "errors": dict(errors), "first_error": first,
            "connect_s": round(connect_s, 4), "total_s": round(time.monotonic() - t0, 4)}
