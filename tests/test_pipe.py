"""The locked named pipe (lagdoctor/winapi/pipe.py): the channel the relay and the capture service will talk over.

Each lock is checked on a real pipe, and where a check could pass for the wrong reason a control runs first and must
show the hazard on this machine: a server made without FILE_FLAG_FIRST_PIPE_INSTANCE joins a squatter's pipe, a pipe
made without PIPE_REJECT_REMOTE_CLIENTS takes an open through \\\\localhost, a write-only label lets a low-integrity
process open the pipe to read, a raw "\\\\.\\pipe\\..\\<file>" opens the file, a client opened without
SECURITY_SQOS_PRESENT is impersonable. The raw pipes, opens and token calls here are the tests' own ctypes, never
pipe.py's. Ctrl+C is _thread.interrupt_main(), which runs the main thread's SIGINT handler as a real Ctrl+C would.
On CI the suite runs elevated (High integrity), so the integrity test's id names the server's level and its controls
measure there what docs/LAUNCHER-PLAN.md M3 asked: whether a medium recorder needs the label to reach an elevated
relay's pipe.
"""
from __future__ import annotations

import _thread
import contextlib
import ctypes
import faulthandler
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from ctypes import wintypes
from pathlib import Path

import pytest

from lagdoctor.winapi import elevation, pipe

ROOT = Path(__file__).resolve().parents[1]
PIPES = "\\\\.\\pipe\\"
LOOPBACK = "\\\\localhost\\pipe\\"
GENERIC_READ = 0x80000000
SYNCHRONIZE = 0x00100000
CLIENT_RIGHTS = GENERIC_READ | 0x0002 | 0x0100      # read, write data, write attributes: written out, not pipe's
LOW, MEDIUM = 0x1000, 0x2000                        # integrity RIDs (S-1-16-<rid>)
LEVELS = {0x1000: "Low", 0x2000: "Medium", 0x2100: "Medium Plus", 0x3000: "High", 0x4000: "System"}
SYSTEM_SID = "S-1-5-18"                             # never the account running the tests
INVALID = ctypes.c_void_p(-1).value

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_adv = ctypes.WinDLL("advapi32", use_last_error=True)
_ntdll = ctypes.WinDLL("ntdll")


class _SA(ctypes.Structure):
    _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                ("bInheritHandle", wintypes.BOOL)]


class _IOSB(ctypes.Structure):
    _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t)]


class _LocalInfo(ctypes.Structure):
    _fields_ = [(n, wintypes.ULONG) for n in ("type", "config", "max_instances", "instances", "in_quota",
                                               "readable", "out_quota", "writable", "state", "end")]


class _Label(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class _UnicodeString(ctypes.Structure):
    _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT), ("Buffer", ctypes.c_wchar_p)]


class _ObjectAttributes(ctypes.Structure):
    _fields_ = [("Length", wintypes.ULONG), ("RootDirectory", wintypes.HANDLE),
                ("ObjectName", ctypes.POINTER(_UnicodeString)), ("Attributes", wintypes.ULONG),
                ("SecurityDescriptor", ctypes.c_void_p), ("SecurityQualityOfService", ctypes.c_void_p)]


assert ctypes.sizeof(_UnicodeString) == 16 and ctypes.sizeof(_ObjectAttributes) == 48


_k32.CreateNamedPipeW.argtypes = [wintypes.LPCWSTR] + [wintypes.DWORD] * 6 + [ctypes.POINTER(_SA)]
_k32.CreateNamedPipeW.restype = wintypes.HANDLE
_k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                             wintypes.DWORD, wintypes.HANDLE]
_k32.CreateFileW.restype = wintypes.HANDLE
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.LocalFree.argtypes = [ctypes.c_void_p]
_k32.LocalFree.restype = ctypes.c_void_p
_k32.GetCurrentProcess.restype = wintypes.HANDLE
_k32.GetCurrentThread.restype = wintypes.HANDLE
_k32.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
_k32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                           ctypes.c_void_p]
_adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                                                      ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
_adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                                                      ctypes.POINTER(wintypes.LPWSTR), ctypes.c_void_p]
_adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
_adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
_adv.OpenThreadToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, ctypes.POINTER(wintypes.HANDLE)]
_adv.DuplicateTokenEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                  ctypes.POINTER(wintypes.HANDLE)]
_adv.SetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
_adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                     ctypes.POINTER(wintypes.DWORD)]
_adv.SetThreadToken.argtypes = [ctypes.c_void_p, wintypes.HANDLE]
_adv.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
_adv.GetLengthSid.argtypes = [ctypes.c_void_p]
_adv.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
_adv.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
_adv.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
_adv.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
_adv.ImpersonateNamedPipeClient.argtypes = [wintypes.HANDLE]
_ntdll.NtQueryInformationFile.argtypes = [wintypes.HANDLE, ctypes.POINTER(_IOSB), ctypes.c_void_p, wintypes.ULONG,
                                          ctypes.c_int]
_ntdll.NtQueryInformationFile.restype = ctypes.c_long
_ntdll.NtOpenFile.argtypes = [ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD, ctypes.POINTER(_ObjectAttributes),
                              ctypes.POINTER(_IOSB), wintypes.ULONG, wintypes.ULONG]
_ntdll.NtOpenFile.restype = ctypes.c_long
_ntdll.RtlNtStatusToDosError.argtypes = [ctypes.c_long]
_ntdll.RtlNtStatusToDosError.restype = wintypes.ULONG


# ----------------------------------------------------------------------------------------------- helpers ---

def _name() -> str:
    return "lagdoctor-test-" + uuid.uuid4().hex


def _me() -> str:
    return elevation.token_user_sid()


def _dacl() -> str:
    return pipe.client_dacl([_me()])


class _Bg:
    """fn(*args) on a daemon thread, so a wait that never ends fails the test instead of hanging the suite."""

    def __init__(self, fn, *args) -> None:
        self._out: dict = {}

        def run() -> None:
            try:
                self._out["value"] = fn(*args)
            except BaseException as exc:  # noqa: BLE001 - handed to result()
                self._out["error"] = exc
        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def alive(self) -> bool:
        return self._thread.is_alive()

    def result(self, timeout_s: float = 10.0):
        self._thread.join(timeout_s)
        assert not self._thread.is_alive(), f"still waiting after {timeout_s} s"
        if "error" in self._out:
            raise self._out["error"]
        return self._out.get("value")


def _until(condition, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not condition():
        assert time.monotonic() < deadline, f"not true within {timeout_s} s"
        time.sleep(0.005)


@contextlib.contextmanager
def _pair(max_message: int = pipe.MAX_MESSAGE):
    """(name, server, client), connected."""
    name = _name()
    with pipe.PipeServer(name, _dacl(), max_message=max_message) as server:
        waiting = _Bg(server.wait_client, 10)
        with pipe.connect(name, 10, max_message=max_message) as client:
            assert waiting.result(10) is True
            yield name, server, client


@contextlib.contextmanager
def _ctrl_c_after(seconds: float):
    """A Ctrl+C on the main thread after `seconds`: Python runs the SIGINT handler there, as for a real one."""
    assert threading.current_thread() is threading.main_thread()
    timer = threading.Timer(seconds, _thread.interrupt_main)
    timer.start()
    try:
        yield
    finally:
        timer.cancel()


def _raw_server(name: str, sddl: str, *, first: bool = True, reject: bool = True, max_instances: int = 1):
    """CreateNamedPipeW without pipe.py: the squatter and the controls. (handle or None, winerror)."""
    sd = ctypes.c_void_p()
    assert _adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(sd), None), sddl
    try:
        sa = _SA(ctypes.sizeof(_SA), sd, False)
        mode = 0x3 | 0x40000000 | (0x00080000 if first else 0)       # duplex, overlapped, [first instance]
        kind = 0x4 | 0x2 | (0x8 if reject else 0)                    # message type and read mode, [reject remote]
        handle = _k32.CreateNamedPipeW(PIPES + name, mode, kind, max_instances, 4096, 4096, 0, ctypes.byref(sa))
        return (None, ctypes.get_last_error()) if handle in (None, INVALID) else (handle, 0)
    finally:
        _k32.LocalFree(sd)


def _raw_open(target: str, access: int, flags: int = 0):
    handle = _k32.CreateFileW(target, access, 0, None, 3, flags, None)         # OPEN_EXISTING
    return (None, ctypes.get_last_error()) if handle in (None, INVALID) else (handle, 0)


def _nt_open(name: str, access: int) -> int:
    """NtOpenFile on the pipe with exactly `access`: CreateFileW always adds FILE_READ_ATTRIBUTES, NtOpenFile does
    not, so only this asks for SYNCHRONIZE alone. The winerror, 0 when it opened (and was closed again)."""
    text = "\\Device\\NamedPipe\\" + name
    us = _UnicodeString(len(text) * 2, len(text) * 2, text)
    attributes = _ObjectAttributes(ctypes.sizeof(_ObjectAttributes), None, ctypes.pointer(us), 0x40, None, None)
    handle, iosb = wintypes.HANDLE(), _IOSB()                                  # 0x40: OBJ_CASE_INSENSITIVE
    status = _ntdll.NtOpenFile(ctypes.byref(handle), access, ctypes.byref(attributes), ctypes.byref(iosb), 3, 0)
    if status >= 0:
        _k32.CloseHandle(handle)
        return 0
    return _ntdll.RtlNtStatusToDosError(status)


def _local_info(handle: int) -> _LocalInfo:
    """The tests' own read of FilePipeLocalInformation (24)."""
    info, iosb = _LocalInfo(), _IOSB()
    status = _ntdll.NtQueryInformationFile(handle, ctypes.byref(iosb), ctypes.byref(info), ctypes.sizeof(info), 24)
    assert status >= 0, f"NtQueryInformationFile: 0x{status & 0xFFFFFFFF:08X}"
    return info


def _windows_spelling(dacl: str) -> str:
    """A DACL as Windows writes it back: aliases such as LA for this PC's built-in Administrator."""
    sd, text = ctypes.c_void_p(), wintypes.LPWSTR()
    assert _adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(dacl, 1, ctypes.byref(sd), None)
    try:
        assert _adv.ConvertSecurityDescriptorToStringSecurityDescriptorW(sd, 1, 0x4, ctypes.byref(text), None)
        try:
            return text.value
        finally:
            _k32.LocalFree(ctypes.cast(text, ctypes.c_void_p))
    finally:
        _k32.LocalFree(sd)


def _token_info(token: int, info_class: int) -> ctypes.Array:
    size = wintypes.DWORD()
    _adv.GetTokenInformation(token, info_class, None, 0, ctypes.byref(size))
    buf = ctypes.create_string_buffer(size.value)
    assert _adv.GetTokenInformation(token, info_class, buf, size, ctypes.byref(size)), ctypes.get_last_error()
    return buf


def _sid_text(sid: int) -> str:
    text = wintypes.LPWSTR()
    assert _adv.ConvertSidToStringSidW(sid, ctypes.byref(text))
    try:
        return text.value
    finally:
        _k32.LocalFree(ctypes.cast(text, ctypes.c_void_p))


def _level(token: int) -> int:
    """A token's integrity level: the last sub-authority of its TokenIntegrityLevel (25) SID."""
    buf = _token_info(token, 25)                        # the SID lives inside buf: keep it until the read is done
    sid = ctypes.c_void_p.from_buffer(buf).value
    count = _adv.GetSidSubAuthorityCount(sid).contents.value
    level = _adv.GetSidSubAuthority(sid, count - 1).contents.value
    del buf
    return level


@contextlib.contextmanager
def _process_token():
    token = wintypes.HANDLE()
    assert _adv.OpenProcessToken(_k32.GetCurrentProcess(), 0x8, ctypes.byref(token))   # TOKEN_QUERY
    try:
        yield token
    finally:
        _k32.CloseHandle(token)


def _own_integrity() -> int:
    with _process_token() as token:
        return _level(token)


def _token_owner() -> str:
    """The owner this process's token gives what it creates (TokenOwner, 4)."""
    with _process_token() as token:
        buf = _token_info(token, 4)                     # the SID lives inside buf
        owner = _sid_text(ctypes.c_void_p.from_buffer(buf).value)
        del buf
        return owner


@contextlib.contextmanager
def _impersonating(rid: int):
    """This thread runs as a copy of the process token at integrity `rid`, and only inside the block."""
    token, copy = wintypes.HANDLE(), wintypes.HANDLE()
    assert _adv.OpenProcessToken(_k32.GetCurrentProcess(), 0x2 | 0x8, ctypes.byref(token))   # DUPLICATE, QUERY
    try:
        # TOKEN_ALL_ACCESS; SecurityImpersonation (2); TokenImpersonation (2)
        assert _adv.DuplicateTokenEx(token, 0xF01FF, None, 2, 2, ctypes.byref(copy)), ctypes.get_last_error()
    finally:
        _k32.CloseHandle(token)
    try:
        sid = ctypes.c_void_p()
        assert _adv.ConvertStringSidToSidW(f"S-1-16-{rid}", ctypes.byref(sid))
        try:
            label = _Label(sid, 0x20)                                 # SE_GROUP_INTEGRITY
            assert _adv.SetTokenInformation(copy, 25, ctypes.byref(label),
                                            ctypes.sizeof(label) + _adv.GetLengthSid(sid)), ctypes.get_last_error()
        finally:
            _k32.LocalFree(sid)
        assert _adv.SetThreadToken(None, copy), ctypes.get_last_error()
        try:
            yield
        finally:
            assert _adv.RevertToSelf()
    finally:
        _k32.CloseHandle(copy)


def _thread_integrity() -> int:
    token = wintypes.HANDLE()
    assert _adv.OpenThreadToken(_k32.GetCurrentThread(), 0x8, True, ctypes.byref(token)), ctypes.get_last_error()
    try:
        return _level(token)
    finally:
        _k32.CloseHandle(token)


def _client_level(server_handle: int) -> int:
    """The impersonation level the client granted: the server impersonates it and reads the thread token's level."""
    assert _adv.ImpersonateNamedPipeClient(server_handle), ctypes.get_last_error()
    try:
        token = wintypes.HANDLE()
        assert _adv.OpenThreadToken(_k32.GetCurrentThread(), 0x8, True, ctypes.byref(token)), ctypes.get_last_error()
        try:
            level, size = ctypes.c_int(), wintypes.DWORD()
            assert _adv.GetTokenInformation(token, 9, ctypes.byref(level), 4, ctypes.byref(size))  # ImpersonationLevel
            return level.value
        finally:
            _k32.CloseHandle(token)
    finally:
        assert _adv.RevertToSelf()


def _own_session() -> int:
    session = wintypes.DWORD()
    assert _k32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session))
    return session.value


def _hidden(text: str) -> str:
    """This PC's account and machine SIDs out of a failure message."""
    me = _me()
    return text.replace(me, "<this account>").replace(me.rsplit("-", 1)[0], "<this PC>")


# ------------------------------------------------------------------------------------- the locks ---

def test_messages_cross_whole_and_one_by_one_both_ways():
    with _pair() as (_, server, client):
        for size in (1, 1000, 4096):
            client.write(bytes([size % 251]) * size, 5)
            assert server.read(5) == bytes([size % 251]) * size
        server.write(b"first", 5)
        server.write(b"second", 5)                      # both queued before either is read
        assert client.read(5) == b"first"
        assert client.read(5) == b"second"
        client.write(b"one", 5)
        client.write(b"two", 5)
        assert server.read(5) == b"one"
        assert server.read(5) == b"two"


@pytest.mark.parametrize("squatter_instances, refusal", [(255, pipe.ERROR_ACCESS_DENIED), (1, pipe.ERROR_PIPE_BUSY)])
def test_a_name_another_process_took_first_is_refused_not_joined(squatter_instances, refusal):
    name = _name()
    squatter, err = _raw_server(name, "D:(A;;GA;;;WD)", first=False, reject=False, max_instances=squatter_instances)
    assert squatter, f"the squatter's pipe could not be made (winerror {err})"
    try:
        if squatter_instances > 1:
            # The control: a server made without FILE_FLAG_FIRST_PIPE_INSTANCE becomes the squatter's second
            # instance, under the squatter's descriptor. Were that refused here, the refusal below would prove nothing.
            joined, err = _raw_server(name, _dacl(), first=False)
            assert joined, f"the control was refused (winerror {err}): this machine does not show the hazard"
            _k32.CloseHandle(joined)
        with pytest.raises(OSError) as refused:
            pipe.PipeServer(name, _dacl())
        assert refused.value.winerror == refusal
    finally:
        _k32.CloseHandle(squatter)


def test_windows_holds_the_server_as_one_instance_refusing_remote_clients():
    plain, err = _raw_server(_name(), _dacl(), reject=False, max_instances=2)
    assert plain, err
    with pipe._Pipe(plain, 4096) as plain_end:         # it closes the raw handle
        info = _local_info(plain)                       # the instruments see the opposites too
        assert not info.type & 0x2 and info.max_instances == 2
        assert plain_end.remote_clients_rejected() is False
    with pipe.PipeServer(_name(), _dacl()) as server:
        info = _local_info(server._handle)              # the tests' own read of the server's handle
        assert info.type & 0x2 and info.max_instances == 1
        assert server.remote_clients_rejected() is True


def test_an_open_through_the_network_path_is_refused():
    name = _name()
    plain, err = _raw_server(name, _dacl(), reject=False)
    assert plain, err
    try:
        # The control: through \\localhost the same open reaches a pipe made without PIPE_REJECT_REMOTE_CLIENTS.
        client, err = _raw_open(LOOPBACK + name, CLIENT_RIGHTS)
        assert client, (f"the control could not open its pipe through \\\\localhost (winerror {err}), so the refusal "
                        "below would prove nothing")
        _k32.CloseHandle(client)
    finally:
        _k32.CloseHandle(plain)
    name = _name()
    with pipe.PipeServer(name, _dacl()):
        client, err = _raw_open(LOOPBACK + name, CLIENT_RIGHTS)
        if client:
            _k32.CloseHandle(client)
        assert client is None and err == pipe.ERROR_ACCESS_DENIED, f"opened: {bool(client)}, winerror {err}"


def test_the_pipe_holds_exactly_the_dacl_and_label_it_was_given():
    me = _me()
    accounts = [me]
    if me.startswith("S-1-5-21-") and not me.endswith("-500"):
        accounts.append(me.rsplit("-", 1)[0] + "-500")   # this PC's built-in Administrator, which Windows writes LA
    for sid in accounts:
        with pipe.PipeServer(_name(), pipe.client_dacl([sid])) as server:
            sddl = server.descriptor()
        dacl, _, sacl = sddl.partition("S:")
        # 0x12018b written out: read, write data, write attributes; never FILE_CREATE_PIPE_INSTANCE (0x4)
        assert dacl == _windows_spelling(f"D:P(A;;0x12018b;;;{sid})"), _hidden(sddl)
        assert sacl.endswith("(ML;;NWNRNX;;;ME)") and sacl.count("(") == 1, _hidden(sddl)
    assert any(_windows_spelling(f"D:P(A;;0x12018b;;;{sid})").endswith(";LA)") for sid in accounts), \
        "the alias case never ran"


def test_an_account_the_dacl_does_not_name_is_refused_at_once():
    name = _name()
    with pipe.PipeServer(name, pipe.client_dacl([SYSTEM_SID])):
        started = time.monotonic()
        with pytest.raises(OSError) as refused:
            pipe.connect(name, 5)
        assert refused.value.winerror == pipe.ERROR_ACCESS_DENIED
        assert time.monotonic() - started < 1.0        # a refusal is never retried until the deadline


def _open_at(rid: int, name: str, access: int) -> int:
    """A raw open of the pipe on a thread running at integrity `rid`: CreateFileW, or NtOpenFile for SYNCHRONIZE
    alone. The winerror, 0 when it opened."""
    with _impersonating(rid):
        assert _thread_integrity() == rid               # the instrument: the open really runs at that level
        if access == SYNCHRONIZE:
            return _nt_open(name, access)
        handle, err = _raw_open(PIPES + name, access)
        if handle:
            _k32.CloseHandle(handle)
        return err


def _connect_at(rid: int, name: str) -> int:
    with _impersonating(rid):
        assert _thread_integrity() == rid
        try:
            pipe.connect(name, 2).close()
        except OSError as exc:
            return exc.winerror
        return 0


# The id names the server's level, so CI's verbose log shows which case ran: "server at High" there.
@pytest.mark.parametrize("server_level", [_own_integrity()], ids=lambda rid: "server at " + LEVELS.get(rid, hex(rid)))
def test_below_medium_integrity_the_pipe_cannot_be_opened_at_all(server_level):
    own = _own_integrity()
    assert own == server_level
    assert own >= MEDIUM, f"this process runs at 0x{own:x}: a medium copy of its token would be a raise"
    bare = f"D:P(A;;0x12018b;;;{_me()})"
    # The controls. A write label alone (NW) lets a low process open the pipe to read, and NWNR still lets it open
    # the pipe just to wait on it: either open takes the pipe's one instance. Without any label, a pipe made at this
    # process's level takes a medium client, since an unlabelled object counts as medium.
    for label, access in (("S:(ML;;NW;;;ME)", GENERIC_READ), ("S:(ML;;NWNR;;;ME)", SYNCHRONIZE)):
        name = _name()
        raw, err = _raw_server(name, bare + label)
        assert raw, err
        try:
            assert _Bg(_open_at, LOW, name, access).result() == 0, f"the {label} control was refused"
        finally:
            _k32.CloseHandle(raw)
    name = _name()
    raw, err = _raw_server(name, bare)
    assert raw, err
    try:
        assert _Bg(_connect_at, MEDIUM, name).result() == 0, f"an unlabelled pipe made at 0x{own:x}"
    finally:
        _k32.CloseHandle(raw)
    # The pipe: a low process is refused whatever it asks for, and a medium one connects.
    for access in (CLIENT_RIGHTS, GENERIC_READ, SYNCHRONIZE):
        name = _name()
        with pipe.PipeServer(name, _dacl()):
            assert _Bg(_open_at, LOW, name, access).result() == pipe.ERROR_ACCESS_DENIED, hex(access)
    name = _name()
    with pipe.PipeServer(name, _dacl()):
        assert _Bg(_connect_at, MEDIUM, name).result() == 0, f"a medium client, the server at 0x{own:x}"


def test_the_client_connects_at_identification_level_only():
    with _pair() as (_, server, client):
        client.write(b"x", 5)
        assert server.read(5) == b"x"                    # a server may impersonate only after a read
        assert _Bg(_client_level, server._handle).result() == 1     # SecurityIdentification
    # The control: a client opened without SECURITY_SQOS_PRESENT grants impersonation (2), which the instrument sees.
    name = _name()
    with pipe.PipeServer(name, _dacl()) as server:
        raw, err = _raw_open(PIPES + name, CLIENT_RIGHTS)
        assert raw, err
        try:
            written = wintypes.DWORD()
            assert _k32.WriteFile(raw, b"x", 1, ctypes.byref(written), None)
            assert server.wait_client(5)
            assert server.read(5) == b"x"
            assert _Bg(_client_level, server._handle).result() == 2  # SecurityImpersonation
        finally:
            _k32.CloseHandle(raw)


CHILD = """
import ctypes, json, os, sys
from ctypes import wintypes
from lagdoctor.winapi import pipe
session = wintypes.DWORD()
assert ctypes.WinDLL("kernel32").ProcessIdToSessionId(os.getpid(), ctypes.byref(session))
with pipe.connect(sys.argv[1], 20) as c:
    c.write(json.dumps({"pid": os.getpid(), "session": session.value, "server_pid": c.server_pid(),
                        "server_session": c.server_session()}).encode(), 10)
    assert c.read(10) == b"bye"
"""


def test_each_end_names_the_other_process_and_its_session():
    name = _name()
    base = getattr(sys, "_base_executable", sys.executable)             # no venv launcher in between
    with pipe.PipeServer(name, _dacl()) as server:
        child = subprocess.Popen([base, "-c", CHILD, name], cwd=ROOT, env=dict(os.environ, PYTHONPATH=str(ROOT)),
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            assert server.wait_client(20), "the child never connected"
            said = json.loads(server.read(10))
            assert said["pid"] != os.getpid()
            assert server.client_pid() == said["pid"]
            assert server.client_session() == said["session"]
            assert said["server_pid"] == os.getpid()
            assert said["server_session"] == _own_session()
            server.write(b"bye", 10)
            out, _ = child.communicate(timeout=20)
            assert child.returncode == 0, out[-800:]
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(10)


def test_each_end_asks_windows_about_the_other_end(monkeypatch):
    # Both ends of every pipe a test can make share one session, so the values above cannot tell the session queries
    # apart; in Stage 4 the service runs in session 0. So the queries themselves are pinned here.
    asked = []
    real = pipe._Pipe._query

    def spy(self, function):
        asked.append(function)
        return real(self, function)
    monkeypatch.setattr(pipe._Pipe, "_query", spy)
    with _pair() as (_, server, client):
        server.client_pid()
        server.client_session()
        client.server_pid()
        client.server_session()
    assert asked == ["GetNamedPipeClientProcessId", "GetNamedPipeClientSessionId", "GetNamedPipeServerProcessId",
                     "GetNamedPipeServerSessionId"]


def test_both_ends_read_the_owner_this_process_gives_what_it_makes():
    with _pair() as (_, server, client):
        assert server.owner_sid() == _token_owner()
        assert client.owner_sid() == _token_owner()


def test_a_name_that_could_leave_the_pipe_folder_is_refused(tmp_path):
    plain = tmp_path / "plain.txt"
    plain.write_bytes(b"an ordinary file")
    escape = "..\\" + str(plain)
    # The control: Windows folds ".." in a \\.\ path before the open, so this raw "pipe" open reaches the file.
    opened, err = _raw_open(PIPES + escape, GENERIC_READ)
    assert opened, f"the escape did not reach the file here (winerror {err}), so the refusals would prove nothing"
    _k32.CloseHandle(opened)
    for bad in (escape, "", ".", "..", "a\\b", "a/b", "a:b", "a b", "-a", "x" * 101, "caf" + chr(0xE9), "a\n", None):
        with pytest.raises(ValueError):
            pipe.path(bad)
        with pytest.raises(ValueError):
            pipe.connect(bad, 0.1)
        with pytest.raises(ValueError):
            pipe.PipeServer(bad, _dacl())
    assert pipe.path("x" * 100) == PIPES + "x" * 100


def test_client_dacl_takes_only_well_formed_sids():
    label = "S:(ML;;NWNRNX;;;ME)"
    assert pipe.client_dacl([SYSTEM_SID]) == f"D:P(A;;0x12018b;;;{SYSTEM_SID}){label}"
    assert pipe.client_dacl(("S-1-5-4", "S-1-5-21-1-2-3-1001")) == \
        f"D:P(A;;0x12018b;;;S-1-5-4)(A;;0x12018b;;;S-1-5-21-1-2-3-1001){label}"
    with pytest.raises(ValueError, match="not one string"):
        pipe.client_dacl(SYSTEM_SID)
    for bad in ([], ["S-1-5-18)(A;;GA;;;WD"], ["WD"], [""], ["s-1-5-18"], ["S-1-5"], [18],
                ["S-1-5-" + chr(0x661) + chr(0x662)], ["S-1-5-18 "]):
        with pytest.raises(ValueError):
            pipe.client_dacl(bad)


# ----------------------------------------------------------------------------------- the messages ---

def test_an_over_long_message_stops_the_reads_and_one_at_the_cap_arrives_whole():
    name = _name()
    with pipe.PipeServer(name, _dacl(), max_message=64) as server:
        waiting = _Bg(server.wait_client, 10)
        with pipe.connect(name, 10, max_message=128) as client:          # a client with a bigger cap of its own
            assert waiting.result()
            sending = _Bg(client.write, b"a" * 65, 10)                   # waits for room: the server takes 64
            with pytest.raises(OSError) as first:
                server.read(10)
            assert first.value.winerror == pipe.ERROR_MORE_DATA
            with pytest.raises(OSError) as again:                       # its 1-byte rest is queued: never a message
                server.read(0.5)
            assert again.value.winerror == pipe.ERROR_MORE_DATA
            sending.result()
    with _pair(max_message=64) as (_, server, client):
        sending = _Bg(client.write, b"b" * 64, 10)
        assert server.read(10) == b"b" * 64
        sending.result()


def test_disconnect_clears_what_the_last_client_left():
    name = _name()
    with pipe.PipeServer(name, _dacl(), max_message=64) as server:
        waiting = _Bg(server.wait_client, 10)
        with pipe.connect(name, 10, max_message=128) as first:
            assert waiting.result()
            sending = _Bg(first.write, b"a" * 65, 10)
            with pytest.raises(OSError) as too_long:
                server.read(10)
            assert too_long.value.winerror == pipe.ERROR_MORE_DATA
            sending.result()
        server.disconnect()
        waiting = _Bg(server.wait_client, 10)
        with pipe.connect(name, 10) as second:
            assert waiting.result()
            second.write(b"fresh", 5)
            assert server.read(5) == b"fresh"            # the first client's over-long message stopped nothing here


def test_a_write_over_the_cap_or_empty_is_refused_before_anything_is_sent():
    with _pair(max_message=64) as (_, server, client):
        with pytest.raises(ValueError):
            client.write(b"x" * 65, 1)
        with pytest.raises(ValueError):
            client.write(b"", 1)
        with pytest.raises(TimeoutError):
            server.read(0.3)                            # nothing went out
        client.write(b"y" * 64, 5)
        assert server.read(5) == b"y" * 64


def test_a_write_that_timed_out_stops_the_writes():
    with _pair(max_message=64) as (_, server, client):
        client.write(b"x" * 64, 5)                      # fills the server's 64-byte quota; nothing reads it yet
        with pytest.raises(TimeoutError):
            client.write(b"y" * 64, 0.3)
        with pytest.raises(OSError) as stopped:
            client.write(b"z", 5)
        assert not isinstance(stopped.value, TimeoutError)
        assert stopped.value.winerror == pipe.ERROR_BROKEN_PIPE
        assert server.read(5) == b"x" * 64              # the message that went out whole still arrives whole


def test_the_end_of_a_conversation_reads_as_the_other_end_gone():
    with _pair() as (_, server, client):
        server.disconnect()
        for call in (lambda: client.read(1), lambda: client.write(b"x", 1)):
            with pytest.raises(OSError) as gone:
                call()
            assert gone.value.winerror in pipe.PEER_GONE, gone.value.winerror
    with _pair() as (_, server, client):
        client.close()
        with pytest.raises(OSError) as gone:
            server.read(1)
        assert gone.value.winerror in pipe.PEER_GONE, gone.value.winerror


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, pipe.MAX_MESSAGE_LIMIT + 1, "64"])
def test_a_message_cap_is_a_whole_number_of_bytes_up_to_one_mebibyte(bad):
    with pytest.raises(ValueError):
        pipe.PipeServer(_name(), _dacl(), max_message=bad)
    with pytest.raises(ValueError):
        pipe.connect(_name(), 0.1, max_message=bad)


def test_the_largest_message_cap_is_allowed():
    with _pair(max_message=pipe.MAX_MESSAGE_LIMIT) as (_, server, client):
        client.write(b"z" * 10, 5)
        assert server.read(5) == b"z" * 10


# -------------------------------------------------------------------------------------- the waits ---

def test_every_wait_ends_and_a_timed_out_read_leaves_the_pipe_usable():
    name = _name()
    with pipe.PipeServer(name, _dacl()) as server:
        started = time.monotonic()
        assert _Bg(server.wait_client, 0.2).result(5) is False
        assert time.monotonic() - started < 2.0
        with pipe.connect(name, 5) as client:
            assert server.wait_client(5) is True        # the client that came between two waits is seen
            started = time.monotonic()
            with pytest.raises(TimeoutError):
                _Bg(server.read, 0.2).result(5)
            assert 0.15 < time.monotonic() - started < 2.0
            client.write(b"after", 5)
            assert server.read(5) == b"after"           # the timed-out read lost nothing and broke nothing
    started = time.monotonic()
    with pytest.raises(TimeoutError) as gone:
        _Bg(pipe.connect, _name(), 0.3).result(5)
    assert time.monotonic() - started < 2.0
    assert gone.value.attempts >= 2 and gone.value.errors == {pipe.ERROR_FILE_NOT_FOUND: gone.value.attempts}


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1, True, "1"])
def test_a_timeout_must_be_a_finite_number_of_seconds(bad):
    with pytest.raises(ValueError):
        _Bg(pipe.connect, _name(), bad).result(5)
    with _pair() as (_, server, client):
        for call in (lambda: server.wait_client(bad), lambda: server.read(bad), lambda: client.write(b"x", bad)):
            with pytest.raises(ValueError):
                _Bg(call).result(5)


def test_connect_waits_for_a_late_server_and_for_a_busy_one():
    name = _name()

    def late_server():
        time.sleep(0.4)
        server = pipe.PipeServer(name, _dacl())
        assert server.wait_client(10)
        return server

    starting = _Bg(late_server)
    with pipe.connect(name, 10) as client:
        server = starting.result()
        try:
            assert client.errors.get(pipe.ERROR_FILE_NOT_FOUND, 0) >= 1    # there was no pipe at first
            client.write(b"late", 5)
            assert server.read(5) == b"late"
        finally:
            server.close()

    with _pair() as (name, server, first):
        with pytest.raises(TimeoutError) as busy:
            _Bg(pipe.connect, name, 0.5).result(5)
        assert busy.value.errors.get(pipe.ERROR_PIPE_BUSY, 0) >= 1     # waited on, not refused
        first.close()
        server.disconnect()
        waiting = _Bg(server.wait_client, 10)
        with pipe.connect(name, 10) as second:
            assert waiting.result() is True
            second.write(b"next", 5)
            assert server.read(5) == b"next"


def test_a_client_that_came_and_went_is_let_go_and_the_wait_goes_on():
    name = _name()
    with pipe.PipeServer(name, _dacl()) as server:
        with pipe.connect(name, 5) as early:            # it opens the listening pipe, speaks and leaves
            early.write(b"gone before the wait", 5)
        started = time.monotonic()
        assert server.wait_client(0.5) is False         # it waited its time rather than give up at once
        assert time.monotonic() - started >= 0.45
        waiting = _Bg(server.wait_client, 10)
        with pipe.connect(name, 5) as late:              # the instance the first one left is free again
            assert waiting.result() is True
            late.write(b"here", 5)
            assert server.read(5) == b"here"


def test_a_read_and_a_write_run_at_once_on_one_end():
    with _pair() as (_, server, client):
        reading = _Bg(server.read, 10)
        _until(server._reader.running)                  # the server's read is waiting
        server.write(b"to the client", 5)               # a write on the same end meanwhile
        time.sleep(0.3)
        assert reading.alive()                          # the write did not end the read
        assert client.read(5) == b"to the client"
        client.write(b"to the server", 5)
        assert reading.result(10) == b"to the server"


def test_a_timed_out_read_leaves_a_waiting_write_alone():
    with _pair(max_message=64) as (_, server, client):
        client.write(b"x" * 64, 5)                      # the server's quota is full, so the next write waits
        writing = _Bg(client.write, b"w" * 64, 10)
        _until(client._writer.running)
        with pytest.raises(TimeoutError):
            client.read(0.3)                            # meanwhile this end's read times out and is cancelled
        assert server.read(5) == b"x" * 64
        assert server.read(5) == b"w" * 64
        writing.result(5)                               # the waiting write was never cancelled with it


def test_a_message_that_lands_as_a_read_times_out_is_kept(monkeypatch):
    with _pair() as (_, server, client):
        real_cancel = pipe._k32.CancelIoEx
        landed = []

        def cancel(handle, ov):
            if not landed:                              # the read's deadline has passed; a message lands now
                landed.append(True)
                server.write(b"just in time", 5)
                _until(lambda: not client._reader.running())
            return real_cancel(handle, ov)
        monkeypatch.setattr(pipe._k32, "CancelIoEx", cancel)
        assert client.read(0.2) == b"just in time"
        assert landed


# -------------------------------------------------------------------------------- close and Ctrl+C ---

def test_close_cancels_a_running_read_at_once_and_twice_is_harmless():
    with _pair() as (_, _server, client):
        reading = _Bg(client.read, 10)
        _until(client._reader.running)
        started = time.monotonic()
        client.close()
        with pytest.raises(OSError) as ended:
            reading.result(5)
        assert time.monotonic() - started < 1.0
        assert not isinstance(ended.value, TimeoutError)
        assert ended.value.winerror == pipe.ERROR_OPERATION_ABORTED
        assert client._handle is None                   # the last call to leave closed it
        client.close()
        with pytest.raises(OSError):
            client.read(0.1)


def test_close_reaches_a_read_that_starts_after_it(monkeypatch):
    with _pair() as (_, _server, client):
        closed = threading.Event()
        real_read = pipe._k32.ReadFile

        def late_read(*args):
            closed.wait(5)                              # inside the pipe, but reaching Windows only after close()
            return real_read(*args)
        monkeypatch.setattr(pipe._k32, "ReadFile", late_read)
        reading = _Bg(client.read, 10)
        _until(lambda: client._active == 1)
        client.close()
        closed.set()
        started = time.monotonic()
        with pytest.raises(OSError) as ended:
            reading.result(5)
        assert time.monotonic() - started < 1.0
        assert ended.value.winerror == pipe.ERROR_OPERATION_ABORTED


def test_close_ends_a_waiting_wait_client():
    with pipe.PipeServer(_name(), _dacl()) as server:
        waiting = _Bg(server.wait_client, 10)
        _until(server._connector.running)
        server.close()
        with pytest.raises(OSError) as ended:
            waiting.result(5)
        assert ended.value.winerror == pipe.ERROR_OPERATION_ABORTED


def test_ctrl_c_during_a_read_leaves_the_pipe_working():
    with _pair() as (_, server, client):
        started = time.monotonic()
        with _ctrl_c_after(0.2), pytest.raises(KeyboardInterrupt):
            client.read(5)
        assert time.monotonic() - started < 1.0         # within a slice of the Ctrl+C, not at the timeout
        assert not client._reader.out                   # its call was cancelled and waited out
        server.write(b"after the Ctrl+C", 5)
        assert client.read(2) == b"after the Ctrl+C"


def test_ctrl_c_during_wait_client_leaves_the_server_working():
    name = _name()
    with pipe.PipeServer(name, _dacl()) as server:
        with _ctrl_c_after(0.2), pytest.raises(KeyboardInterrupt):
            server.wait_client(5)
        assert not server._connector.out
        with pipe.connect(name, 5) as client:
            assert server.wait_client(2) is True
            client.write(b"still here", 5)
            assert server.read(5) == b"still here"


def test_a_message_that_came_as_ctrl_c_landed_is_the_next_reads(monkeypatch):
    with _pair() as (_, server, client):
        real_wait = pipe._k32.WaitForSingleObject
        hit = []

        def wait(event, ms):
            if not hit and event == client._reader.event:
                hit.append(True)
                server.write(b"caught in the middle", 5)            # the message lands and completes the read...
                _until(lambda: not client._reader.running())
                raise KeyboardInterrupt                             # ...just as Ctrl+C does
            return real_wait(event, ms)
        monkeypatch.setattr(pipe._k32, "WaitForSingleObject", wait)
        with pytest.raises(KeyboardInterrupt):
            client.read(5)
        monkeypatch.undo()
        assert client.read(1) == b"caught in the middle"


def test_close_from_a_ctrl_c_handler_ends_the_read_it_interrupted():
    with _pair() as (_, _server, client):
        previous = signal.signal(signal.SIGINT, lambda *_: client.close())
        faulthandler.dump_traceback_later(60, exit=True)    # a close() that waited on its own thread would hang here
        started = time.monotonic()
        try:
            with _ctrl_c_after(0.2), pytest.raises(OSError) as ended:
                client.read(5)
        finally:
            faulthandler.cancel_dump_traceback_later()
            signal.signal(signal.SIGINT, previous)
        assert time.monotonic() - started < 1.0
        assert ended.value.winerror == pipe.ERROR_OPERATION_ABORTED
        assert client._handle is None
