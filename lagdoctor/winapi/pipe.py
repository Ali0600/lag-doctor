"""The locked named pipe: the one channel between a recording and the process that holds the ETW rights.

docs/LAUNCHER-PLAN.md M2 and docs/INSTALLER-PLAN.md Stage 4: the relay elevated per recording (M3) and the capture
service (Stage 4) talk to the recorder only through this. Thin wrappers with no policy: who may connect is the
caller's descriptor (`client_dacl` builds it from the SIDs the caller chose), and what a message means is
`lagdoctor/relay/`'s. Each property was measured on Windows 11 build 26200 (docs/research/install-and-rights.md,
section 11):

- The server is the first and only instance (FILE_FLAG_FIRST_PIPE_INSTANCE, one instance). Without the flag, a
  server whose name another process took first quietly becomes a second instance of that process's pipe, under that
  process's descriptor; with it, Windows refuses (ERROR_ACCESS_DENIED, or ERROR_PIPE_BUSY when that pipe allows one
  instance). One instance also keeps server_pid() meaningful: it names the pipe's first creator on every instance.
- Remote clients are refused (PIPE_REJECT_REMOTE_CLIENTS): an open through \\\\localhost\\pipe\\ fails.
- The descriptor is the caller's: a DACL, and an explicit medium label that denies anything below medium integrity
  every right (NWNRNX): a write label alone (NW) still lets a low-integrity process open the pipe to read, or just to
  wait on it, and so hold its one instance. A medium recorder reaches a pipe an elevated relay or the service made.
- A client asks for read, write-data and write-attributes (CLIENT_ACCESS; message read mode needs the last), never
  GENERIC_WRITE, which also carries FILE_CREATE_PIPE_INSTANCE; `client_dacl` grants exactly that.
- A client connects at identification level (SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION), so a process that
  took the name first cannot act as the client.
- Names are checked: "\\\\.\\pipe\\..\\C:\\x" opens the ordinary file C:\\x.
- No call leaves Windows writing into memory Python may free. Each direction's OVERLAPPED, event and buffer belong to
  the pipe; waits run in slices of SLICE_MS, so Ctrl+C and close() reach a waiting call within one; a call that an
  exception (Ctrl+C) interrupts is cancelled and waited out before the exception goes on, and a message it had
  received by then is the next read's. Windows cancels the I/O a thread started when the thread exits, so no call is
  left pending past the function that issued it.
- close() is safe from any thread, and from a signal handler that interrupted one of this pipe's calls: it cancels
  what runs, and the handle is closed as the last running call leaves.

Layouts from minwinbase.h, winbase.h and ntifs.h (Windows SDK 10.0.19041), x64.
"""
from __future__ import annotations

import contextlib
import ctypes
import math
import re
import threading
import time
from ctypes import wintypes

PIPE_ACCESS_DUPLEX = 0x00000003              # winbase.h: open mode
FILE_FLAG_OVERLAPPED = 0x40000000
FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000   # winbase.h: fail when the name exists (the squatting guard)
PIPE_TYPE_MESSAGE = 0x00000004               # winbase.h: pipe mode
PIPE_READMODE_MESSAGE = 0x00000002
PIPE_WAIT = 0x00000000
PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
GENERIC_READ = 0x80000000                    # winnt.h
FILE_WRITE_DATA = 0x0002
FILE_WRITE_ATTRIBUTES = 0x0100
CLIENT_ACCESS = GENERIC_READ | FILE_WRITE_DATA | FILE_WRITE_ATTRIBUTES   # what connect() asks for
CLIENT_GRANT = 0x0012018B   # CLIENT_ACCESS mapped: FILE_GENERIC_READ (0x120089) | FILE_WRITE_DATA | ..._ATTRIBUTES
OPEN_EXISTING = 3
SECURITY_SQOS_PRESENT = 0x00100000           # winbase.h: CreateFileW flags for a pipe client
SECURITY_IDENTIFICATION = 0x00010000
ERROR_FILE_NOT_FOUND = 2                     # winerror.h
ERROR_ACCESS_DENIED = 5
ERROR_INVALID_HANDLE = 6
ERROR_BROKEN_PIPE = 109
ERROR_PIPE_BUSY = 231
ERROR_NO_DATA = 232                          # the other end closed; for ConnectNamedPipe, a client came and went
ERROR_PIPE_NOT_CONNECTED = 233
ERROR_MORE_DATA = 234
ERROR_PIPE_CONNECTED = 535
ERROR_PIPE_LISTENING = 536
ERROR_OPERATION_ABORTED = 995
ERROR_IO_INCOMPLETE = 996
ERROR_IO_PENDING = 997
ERROR_NOT_FOUND = 1168                       # CancelIoEx: no call of that OVERLAPPED is running
PEER_GONE = (ERROR_BROKEN_PIPE, ERROR_NO_DATA, ERROR_PIPE_NOT_CONNECTED)   # the other end closed or disconnected
STATUS_PENDING = 0x103                       # ntstatus.h: OVERLAPPED.Internal while a call runs
NOT_ISSUED = 1 << 40                         # set before each call; an NTSTATUS has 32 bits, so Windows overwrites
                                             # it only by taking the call (measured: Internal is 0x103 from then on)
WAIT_OBJECT_0, WAIT_TIMEOUT = 0x0, 0x102
SLICE_MS = 100                               # the longest single wait: Ctrl+C and close() land within one
CANCEL_WAIT_S = 5.0                          # the longest wait for Windows to end a cancelled call
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
SDDL_REVISION_1 = 1
SE_KERNEL_OBJECT = 6                         # accctrl.h: SE_OBJECT_TYPE
OWNER_SECURITY_INFORMATION = 0x00000001      # winnt.h
DACL_SECURITY_INFORMATION = 0x00000004
LABEL_SECURITY_INFORMATION = 0x00000010
FILE_PIPE_LOCAL_INFORMATION_CLASS = 24       # FilePipeLocalInformation in FILE_INFORMATION_CLASS (wdm.h)
FILE_PIPE_REJECT_REMOTE_CLIENTS = 0x2        # ntifs.h: a bit of FILE_PIPE_LOCAL_INFORMATION.NamedPipeType
MEDIUM_LABEL = "S:(ML;;NWNRNX;;;ME)"         # mandatory label: medium; no write, read or execute up
MAX_MESSAGE = 65536                          # the default cap on one message, in bytes
MAX_MESSAGE_LIMIT = 1 << 20                  # no caller may ask for a bigger read buffer than this
RETRY_S = 0.05                               # connect(): the pause before trying a pipe that does not exist yet
BUSY_WAIT_MS = 200                           # connect(): the longest WaitNamedPipeW slice on a busy pipe
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")   # no separator, no leading dot: it stays in \\.\pipe\
_SID = re.compile(r"S-1-[0-9]{1,13}(?:-[0-9]{1,10}){1,15}")   # [0-9], not \d, which takes other scripts' digits
_PREFIX = "\\\\.\\pipe\\"
_STRANDED: list = []                         # calls Windows never ended: their memory is kept for the process's life


class OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t), ("Offset", wintypes.DWORD),
                ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                ("bInheritHandle", wintypes.BOOL)]


class IO_STATUS_BLOCK(ctypes.Structure):
    _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t)]


class FILE_PIPE_LOCAL_INFORMATION(ctypes.Structure):
    _fields_ = [(name, wintypes.ULONG) for name in (
        "NamedPipeType", "NamedPipeConfiguration", "MaximumInstances", "CurrentInstances", "InboundQuota",
        "ReadDataAvailable", "OutboundQuota", "WriteQuotaAvailable", "NamedPipeState", "NamedPipeEnd")]


assert ctypes.sizeof(OVERLAPPED) == 32
assert ctypes.sizeof(SECURITY_ATTRIBUTES) == 24
assert ctypes.sizeof(IO_STATUS_BLOCK) == 16
assert ctypes.sizeof(FILE_PIPE_LOCAL_INFORMATION) == 40

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_adv = ctypes.WinDLL("advapi32", use_last_error=True)
_ntdll = ctypes.WinDLL("ntdll")

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
_k32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
_k32.WaitNamedPipeW.restype = wintypes.BOOL
_k32.SetNamedPipeHandleState.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
                                         ctypes.c_void_p]
_k32.SetNamedPipeHandleState.restype = wintypes.BOOL
for _query in ("GetNamedPipeClientProcessId", "GetNamedPipeServerProcessId", "GetNamedPipeClientSessionId",
               "GetNamedPipeServerSessionId"):
    getattr(_k32, _query).argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]
    getattr(_k32, _query).restype = wintypes.BOOL
for _io in (_k32.ReadFile, _k32.WriteFile):
    _io.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                    ctypes.POINTER(OVERLAPPED)]
    _io.restype = wintypes.BOOL
_k32.GetOverlappedResult.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED), ctypes.POINTER(wintypes.DWORD),
                                     wintypes.BOOL]
_k32.GetOverlappedResult.restype = wintypes.BOOL
_k32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED)]
_k32.CancelIoEx.restype = wintypes.BOOL
_k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
_k32.CreateEventW.restype = wintypes.HANDLE
_k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_k32.WaitForSingleObject.restype = wintypes.DWORD
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.CloseHandle.restype = wintypes.BOOL
_k32.LocalFree.argtypes = [ctypes.c_void_p]
_k32.LocalFree.restype = ctypes.c_void_p
_adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.ULONG)]
_adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
_adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
    ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(wintypes.ULONG)]
_adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
_adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
_adv.ConvertSidToStringSidW.restype = wintypes.BOOL
_adv.GetSecurityInfo.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
                                 ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
_adv.GetSecurityInfo.restype = wintypes.DWORD
_ntdll.NtQueryInformationFile.argtypes = [wintypes.HANDLE, ctypes.POINTER(IO_STATUS_BLOCK), ctypes.c_void_p,
                                          wintypes.ULONG, ctypes.c_int]
_ntdll.NtQueryInformationFile.restype = ctypes.c_long      # NTSTATUS
_ntdll.RtlNtStatusToDosError.argtypes = [ctypes.c_long]
_ntdll.RtlNtStatusToDosError.restype = wintypes.ULONG


def path(name: str) -> str:
    """\\\\.\\pipe\\<name>. ValueError unless the name is 1 to 100 letters, digits, dots, underscores and dashes,
    starting with a letter or digit: anything else could leave the pipe folder, since Windows folds ".." in a
    \\\\.\\ path before the open, so "..\\C:\\x" would open an ordinary file."""
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError(f"not a pipe name: {name!r}")
    return _PREFIX + name


def client_dacl(sids: list[str] | tuple[str, ...]) -> str:
    """The descriptor for a pipe that only these accounts or groups may open as clients: a protected DACL granting
    each SID exactly what connect() asks for (CLIENT_GRANT), and the medium label. ValueError on a SID that is not
    one: a "SID" such as "S-1-5-18)(A;;GA;;;WD" would add an entry of its own."""
    if isinstance(sids, str):
        raise ValueError("a list of SIDs, not one string")
    sids = list(sids)
    if not sids:
        raise ValueError("a pipe no account may open")
    for sid in sids:
        if not isinstance(sid, str) or not _SID.fullmatch(sid):
            raise ValueError(f"not a SID: {sid!r}")
    return "D:P" + "".join(f"(A;;0x{CLIENT_GRANT:x};;;{sid})" for sid in sids) + MEDIUM_LABEL


def _error(code: int | None = None, text: str | None = None) -> OSError:
    code = ctypes.get_last_error() if code is None else code
    return ctypes.WinError(code, text) if text else ctypes.WinError(code)


def _seconds(timeout_s: float) -> float:
    """A timeout in seconds. ValueError on NaN, infinity, a negative number or a bool."""
    if (not isinstance(timeout_s, (int, float)) or isinstance(timeout_s, bool) or not math.isfinite(timeout_s)
            or timeout_s < 0):
        raise ValueError(f"not a timeout in seconds: {timeout_s!r}")
    return float(timeout_s)


def _size(max_message: int) -> int:
    if not isinstance(max_message, int) or isinstance(max_message, bool) or not 1 <= max_message <= MAX_MESSAGE_LIMIT:
        raise ValueError(f"a message cap is 1 to {MAX_MESSAGE_LIMIT} bytes, not {max_message!r}")
    return max_message


def _new_event() -> int:
    event = _k32.CreateEventW(None, True, False, None)       # manual reset: each overlapped call resets it as it starts
    if not event:
        raise _error()
    return event


class _Op:
    """One direction's overlapped call (read, write or connect). Its OVERLAPPED, event and buffer belong to the pipe,
    not to one call, so a call that an exception abandoned never completes into freed memory."""

    def __init__(self, event: int, size: int = 0) -> None:
        self.event = event
        self.ov = OVERLAPPED()
        self.buf = ctypes.create_string_buffer(size) if size else None
        self.size = 0                            # the bytes the call in flight was asked to move
        self.out = False                         # issued, and not yet seen to end
        self.result: tuple[int, int] | None = None   # (bytes, winerror) of a call an exception ended

    def running(self) -> bool:
        return self.ov.Internal == STATUS_PENDING


def _end(handle: int, op: _Op) -> tuple[int, int] | None:
    """Make sure op's call has ended, cancelling it if it still runs: (bytes moved, winerror, 0 when it succeeded).
    None when nothing came of it: it was never issued, or it failed at once (which leaves Internal at STATUS_PENDING
    with nothing to complete it), or Windows has not ended it within CANCEL_WAIT_S (op then stays out)."""
    if op.ov.Internal == NOT_ISSUED:
        op.out = False
        return None
    queued = bool(_k32.CancelIoEx(handle, ctypes.byref(op.ov))) or ctypes.get_last_error() != ERROR_NOT_FOUND
    give_up = time.monotonic() + (CANCEL_WAIT_S if queued else 0.05)
    while op.running() and time.monotonic() < give_up:
        _k32.WaitForSingleObject(op.event, 10)
    if op.running():
        op.out = queued                          # failed at once: nothing runs; else Windows has not ended it
        return None
    op.out = False
    done = wintypes.DWORD(0)
    ok = _k32.GetOverlappedResult(handle, ctypes.byref(op.ov), ctypes.byref(done), False)
    return int(done.value), (0 if ok else ctypes.get_last_error())


class _Pipe:
    """One end of a pipe. read() and write() may run at the same time from two threads; two reads (or two writes)
    wait for each other. Every call on a closed pipe raises OSError ERROR_INVALID_HANDLE."""

    def __init__(self, handle: int, max_message: int) -> None:
        self.max_message = max_message
        self._handle: int | None = handle
        self._state = threading.RLock()          # _closing, _active, _handle; re-entrant for close() in a signal
        self._read_lock, self._write_lock = threading.Lock(), threading.Lock()   # handler on the same thread
        self._closing = False
        self._active = 0                         # calls inside the pipe now
        self._unreadable: str | None = None      # why reads stopped: an over-long message's rest is still queued
        self._unwritable: str | None = None      # why writes stopped: the other end may hold part of a message
        self._held: bytes | None = None          # a message a read had received when an exception ended it
        self._ops: list[_Op] = []
        try:
            self._reader = self._op(max_message)
            self._writer = self._op()
        except BaseException:
            self.close()
            raise

    def _op(self, size: int = 0) -> _Op:
        op = _Op(_new_event(), size)
        self._ops.append(op)
        return op

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def __del__(self) -> None:
        with contextlib.suppress(Exception):     # a finalizer must not raise, and at exit ctypes may be gone
            self.close()

    @contextlib.contextmanager
    def _using(self, lock: threading.Lock | None = None):
        """The handle for one call, with that direction's lock held. After close(), the last call to leave closes
        the pipe."""
        if lock is not None:
            lock.acquire()
        try:
            with self._state:
                if self._closing:
                    raise _error(ERROR_INVALID_HANDLE, "the pipe is closed")
                self._active += 1
            try:
                yield self._handle
            finally:
                with self._state:
                    self._active -= 1
                    last = self._closing and self._active == 0
                if last:
                    self._release()
        finally:
            if lock is not None:
                lock.release()

    def _run(self, op: _Op, handle: int, issue, timeout: float) -> int:
        """Issue one call on op, issue(pointer to op.ov), and see it end: the bytes it moved. TimeoutError when it had
        not ended in `timeout` seconds (it is cancelled; if it ended first, it counts), OSError ERROR_OPERATION_ABORTED
        when close() cancelled it, OSError with Windows' code when it failed. An exception while it waits (Ctrl+C)
        cancels it and waits it out before going on, leaving its end in op.result."""
        deadline = time.monotonic() + timeout
        op.result = None
        ctypes.memset(ctypes.addressof(op.ov), 0, ctypes.sizeof(OVERLAPPED))
        op.ov.Internal, op.ov.hEvent = NOT_ISSUED, op.event
        op.out = True
        cause = None
        try:
            if not issue(ctypes.byref(op.ov)):
                err = ctypes.get_last_error()
                if err != ERROR_IO_PENDING:
                    op.out = False                   # it failed at once, and nothing runs
                    raise _error(err)
            while op.running():
                if self._closing:
                    cause = "closed"
                elif time.monotonic() >= deadline:
                    cause = "timeout"
                if cause:
                    ended = _end(handle, op)
                    if ended is None:
                        raise _error(ERROR_IO_INCOMPLETE, "Windows did not end a cancelled pipe call")
                    n, err = ended
                    break
                left_ms = math.ceil((deadline - time.monotonic()) * 1000)
                if _k32.WaitForSingleObject(op.event, max(1, min(SLICE_MS, left_ms))) not in (WAIT_OBJECT_0,
                                                                                               WAIT_TIMEOUT):
                    raise _error()
            else:
                done = wintypes.DWORD(0)
                ok = _k32.GetOverlappedResult(handle, ctypes.byref(op.ov), ctypes.byref(done), False)
                n, err = int(done.value), (0 if ok else ctypes.get_last_error())
                op.out = False
        except BaseException:
            if op.out:
                op.result = _end(handle, op)
            raise
        if err == ERROR_OPERATION_ABORTED and cause == "timeout":
            raise TimeoutError(f"the pipe did not finish within {timeout:g} s")
        if err == ERROR_OPERATION_ABORTED and cause == "closed":
            raise _error(ERROR_OPERATION_ABORTED, "the pipe was closed")
        if err:
            raise _error(err)
        return n

    def _leftover(self, handle: int, op: _Op) -> tuple[int, int] | None:
        """The end of a call an exception left out on op (cancelled and waited out here), or None."""
        if not op.out:
            return None
        ended = _end(handle, op)
        if op.out:
            raise _error(ERROR_IO_INCOMPLETE, "an earlier call on this pipe has not ended")
        return ended

    def _keep_read(self, ended: tuple[int, int] | None) -> None:
        """What a read an exception ended had received: a whole message for the next read, or an over-long one."""
        if ended is not None:
            n, err = ended
            if err == 0:
                self._held = self._reader.buf.raw[:n]
            elif err == ERROR_MORE_DATA:
                self._unreadable = f"a message longer than {self.max_message} bytes came; this pipe reads nothing more"

    def read(self, timeout_s: float) -> bytes:
        """The next whole message, waited for at most timeout_s; then TimeoutError (an OSError with no winerror), and
        the pipe stays usable. Once the other end has gone, OSError with a winerror in PEER_GONE. A message longer
        than max_message raises OSError ERROR_MORE_DATA, and so does every read after it, until disconnect(): its
        rest, still queued, would otherwise come back as a message of its own."""
        timeout = _seconds(timeout_s)
        with self._using(self._read_lock) as handle:
            op = self._reader
            self._keep_read(self._leftover(handle, op))
            if self._held is not None:
                message, self._held = self._held, None
                return message
            if self._unreadable:
                raise _error(ERROR_MORE_DATA, self._unreadable)
            try:
                n = self._run(op, handle, lambda ov: _k32.ReadFile(handle, op.buf, len(op.buf), None, ov), timeout)
            except BaseException as exc:
                if isinstance(exc, OSError) and exc.winerror == ERROR_MORE_DATA:
                    self._keep_read((0, ERROR_MORE_DATA))
                    raise _error(ERROR_MORE_DATA, self._unreadable) from None
                self._keep_read(op.result)           # Ctrl+C as a message came: the next read returns it
                raise
            return op.buf.raw[:n]

    def _keep_write(self, ended: tuple[int, int] | None) -> None:
        if ended is not None and ended != (self._writer.size, 0):
            self._unwritable = "an interrupted write may have sent part of a message"

    def write(self, data: bytes, timeout_s: float) -> None:
        """Send one message of 1 to max_message bytes (ValueError otherwise, before anything is sent), waiting at most
        timeout_s for room. Once the other end has gone, OSError with a winerror in PEER_GONE. After a write that
        timed out, came up short or was interrupted, every write raises OSError ERROR_BROKEN_PIPE: the other end may
        hold part of a message."""
        data = bytes(data)
        if not 1 <= len(data) <= self.max_message:
            raise ValueError(f"a message is 1 to {self.max_message} bytes, not {len(data)}")
        timeout = _seconds(timeout_s)
        with self._using(self._write_lock) as handle:
            op = self._writer
            self._keep_write(self._leftover(handle, op))
            if self._unwritable:
                raise _error(ERROR_BROKEN_PIPE, self._unwritable)
            op.buf, op.size = ctypes.create_string_buffer(data, len(data)), len(data)   # kept while the call may run
            try:
                n = self._run(op, handle, lambda ov: _k32.WriteFile(handle, op.buf, op.size, None, ov), timeout)
            except TimeoutError:
                self._unwritable = "a write did not finish in time, so the other end may hold part of a message"
                raise
            except BaseException:
                self._keep_write(op.result)
                raise
            if n != len(data):
                self._unwritable = f"a write sent {n} of {len(data)} bytes"
                raise _error(ERROR_BROKEN_PIPE, self._unwritable)

    def _query(self, function: str) -> int:
        """One of the GetNamedPipe*ProcessId / *SessionId queries."""
        with self._using() as handle:
            value = wintypes.ULONG(0)
            if not getattr(_k32, function)(handle, ctypes.byref(value)):
                raise _error()
            return int(value.value)

    def descriptor(self) -> str:
        """The pipe's DACL and integrity label as Windows holds them, in SDDL (GetSecurityInfo)."""
        with self._using() as handle:
            info = DACL_SECURITY_INFORMATION | LABEL_SECURITY_INFORMATION
            sd = ctypes.c_void_p()
            rc = _adv.GetSecurityInfo(handle, SE_KERNEL_OBJECT, info, None, None, None, None, ctypes.byref(sd))
            if rc:
                raise _error(rc)
            text = wintypes.LPWSTR()
            try:
                if not _adv.ConvertSecurityDescriptorToStringSecurityDescriptorW(sd, SDDL_REVISION_1, info,
                                                                                 ctypes.byref(text), None):
                    raise _error()
                return text.value
            finally:
                if text:
                    _k32.LocalFree(ctypes.cast(text, ctypes.c_void_p))
                _k32.LocalFree(sd)

    def owner_sid(self) -> str:
        """The pipe's owner as a SID ("S-1-..."; never an SDDL alias): set by the process that made the pipe, from
        its own token. A process cannot make a pipe owned by a group it does not hold, so a client can check the
        owner as well as server_pid()."""
        with self._using() as handle:
            owner, sd = ctypes.c_void_p(), ctypes.c_void_p()
            rc = _adv.GetSecurityInfo(handle, SE_KERNEL_OBJECT, OWNER_SECURITY_INFORMATION, ctypes.byref(owner), None,
                                      None, None, ctypes.byref(sd))
            if rc:
                raise _error(rc)
            text = wintypes.LPWSTR()
            try:
                if not _adv.ConvertSidToStringSidW(owner, ctypes.byref(text)):
                    raise _error()
                return text.value
            finally:
                if text:
                    _k32.LocalFree(ctypes.cast(text, ctypes.c_void_p))
                _k32.LocalFree(sd)

    def remote_clients_rejected(self) -> bool:
        """Whether Windows holds this pipe as refusing remote clients (FilePipeLocalInformation's NamedPipeType)."""
        with self._using() as handle:
            info, iosb = FILE_PIPE_LOCAL_INFORMATION(), IO_STATUS_BLOCK()
            status = _ntdll.NtQueryInformationFile(handle, ctypes.byref(iosb), ctypes.byref(info), ctypes.sizeof(info),
                                                   FILE_PIPE_LOCAL_INFORMATION_CLASS)
            if status < 0:
                raise _error(_ntdll.RtlNtStatusToDosError(status), f"NtQueryInformationFile: NTSTATUS "
                                                                    f"0x{status & 0xFFFFFFFF:08X}")
            return bool(info.NamedPipeType & FILE_PIPE_REJECT_REMOTE_CLIENTS)

    def close(self) -> None:
        """Stop the pipe. No call starts after it; a running read, write or wait sees it within one slice, is
        cancelled and raises OSError ERROR_OPERATION_ABORTED; the handle is closed as the last running call leaves,
        or here when none runs. Idempotent; safe from any thread, and from a signal handler that interrupted one of
        this pipe's calls on its own thread (it never waits)."""
        with self._state:
            if self._closing:
                return
            self._closing = True
            last = self._active == 0
        if last:
            self._release()

    def _release(self) -> None:
        """Close the handle and the events. A call an exception left out is ended first: its memory is the pipe's."""
        with self._state:
            handle, self._handle = self._handle, None
        if handle is None:
            return
        for op in self._ops:
            if op.out:
                _end(handle, op)
            if op.out:
                _STRANDED.append(op)             # Windows never ended it: never free what it may still write
        _k32.CloseHandle(handle)
        for op in self._ops:
            _k32.CloseHandle(op.event)


class PipeServer(_Pipe):
    """The first and only instance of \\\\.\\pipe\\<name>: message mode, overlapped, remote clients refused, under the
    descriptor `sddl` (build it with client_dacl). OSError when the name exists already, whoever made it:
    ERROR_ACCESS_DENIED, or ERROR_PIPE_BUSY when that pipe allows one instance. One client at a time; use it in a
    with-block."""

    def __init__(self, name: str, sddl: str, max_message: int = MAX_MESSAGE) -> None:
        self.path = path(name)
        max_message = _size(max_message)
        sd = ctypes.c_void_p()
        if not _adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, SDDL_REVISION_1, ctypes.byref(sd),
                                                                         None):
            raise _error()
        try:
            sa = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), sd, False)
            handle = _k32.CreateNamedPipeW(
                self.path, PIPE_ACCESS_DUPLEX | FILE_FLAG_OVERLAPPED | FILE_FLAG_FIRST_PIPE_INSTANCE,
                PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
                1, max_message, max_message, 0, ctypes.byref(sa))
            if handle in (None, INVALID_HANDLE_VALUE):
                raise _error()
        finally:
            _k32.LocalFree(sd)                               # the pipe keeps a copy of its own
        super().__init__(handle, max_message)
        try:
            self._connector = self._op()
        except BaseException:
            self.close()
            raise

    def wait_client(self, timeout_s: float) -> bool:
        """True once a client is connected; False when none came within timeout_s. Any thread may call it, and a
        client that comes between two calls is not missed: its open completes against the listening pipe, and the
        next call sees it at once. A client that came and went before a call is let go, and the wait goes on."""
        timeout = _seconds(timeout_s)
        deadline = time.monotonic() + timeout
        with self._using(self._read_lock) as handle:
            op = self._connector
            self._leftover(handle, op)
            while True:
                try:
                    self._run(op, handle, lambda ov: _k32.ConnectNamedPipe(handle, ov),
                              max(0.0, deadline - time.monotonic()))
                    return True
                except TimeoutError:
                    return False
                except OSError as exc:
                    if exc.winerror == ERROR_PIPE_CONNECTED:     # it came before this call
                        return True
                    if exc.winerror != ERROR_NO_DATA:
                        raise
                if not _k32.DisconnectNamedPipe(handle):         # free the instance it left closing
                    raise _error()
                if time.monotonic() >= deadline:
                    return False

    def client_pid(self) -> int:
        """The connected client's process id (GetNamedPipeClientProcessId), read without opening it."""
        return self._query("GetNamedPipeClientProcessId")

    def client_session(self) -> int:
        """The connected client's Windows session (GetNamedPipeClientSessionId)."""
        return self._query("GetNamedPipeClientSessionId")

    def disconnect(self) -> None:
        """Drop the client, so wait_client can take the next. What it has not read yet is lost, so the protocol ends a
        conversation before either end lets go. Waits for a running read or write on this end, and clears what the
        last client left: a held message, and the stops after an over-long message or a broken write."""
        with self._read_lock, self._write_lock, self._using() as handle:
            for op in self._ops:
                self._leftover(handle, op)
            if not _k32.DisconnectNamedPipe(handle):
                err = ctypes.get_last_error()
                if err not in (ERROR_PIPE_NOT_CONNECTED, ERROR_PIPE_LISTENING):   # no client to drop
                    raise _error(err)
            self._unreadable = self._unwritable = self._held = None


class PipeClient(_Pipe):
    """The client end, as connect() opens it. `attempts` and `errors` ({winerror: count} before the open that worked)
    say how the connect went."""

    def __init__(self, handle: int, max_message: int, path: str, attempts: int, errors: dict[int, int]) -> None:
        self.path, self.attempts, self.errors = path, attempts, errors
        super().__init__(handle, max_message)

    def server_pid(self) -> int:
        """The pipe's first creator (GetNamedPipeServerProcessId), read without opening it. With one instance, that is
        the server; it can be a process that has since exited, so check owner_sid() too."""
        return self._query("GetNamedPipeServerProcessId")

    def server_session(self) -> int:
        """The server end's Windows session (GetNamedPipeServerSessionId)."""
        return self._query("GetNamedPipeServerSessionId")


def connect(name: str, timeout_s: float, max_message: int = MAX_MESSAGE) -> PipeClient:
    """Open \\\\.\\pipe\\<name> as a client, at identification level and in message read mode. While the pipe does not
    exist yet, or its one instance is busy (a service its trigger is starting answers ERROR_PIPE_BUSY), it retries
    until timeout_s has passed, then raises TimeoutError carrying .attempts and .errors ({winerror: count}). Any other
    refusal raises at once: ERROR_ACCESS_DENIED when the DACL does not name this account or its integrity is below
    the label's."""
    target = path(name)
    max_message = _size(max_message)
    timeout = _seconds(timeout_s)
    deadline = time.monotonic() + timeout
    attempts, errors = 0, {}
    while True:
        attempts += 1
        handle = _k32.CreateFileW(target, CLIENT_ACCESS, 0, None, OPEN_EXISTING,
                                  FILE_FLAG_OVERLAPPED | SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION, None)
        if handle not in (None, INVALID_HANDLE_VALUE):
            break
        err = ctypes.get_last_error()
        if err not in (ERROR_FILE_NOT_FOUND, ERROR_PIPE_BUSY):
            raise _error(err)
        errors[err] = errors.get(err, 0) + 1
        left = deadline - time.monotonic()
        if left <= 0:
            exc = TimeoutError(f"{target} took no connection within {timeout:g} s")
            exc.attempts, exc.errors = attempts, errors
            raise exc
        if err == ERROR_PIPE_BUSY:
            _k32.WaitNamedPipeW(target, max(1, min(BUSY_WAIT_MS, int(left * 1000))))
        else:
            time.sleep(min(RETRY_S, left))
    try:
        mode = wintypes.DWORD(PIPE_READMODE_MESSAGE)
        if not _k32.SetNamedPipeHandleState(handle, ctypes.byref(mode), None, None):
            raise _error()
    except BaseException:
        _k32.CloseHandle(handle)
        raise
    return PipeClient(handle, max_message, target, attempts, errors)   # it closes the handle if it cannot start
