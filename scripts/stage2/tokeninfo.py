"""An access token, read back: user, groups, privileges, restricted SIDs, session, integrity and elevation.

E3 must read the service's trimmed privileges and its restricted service SID back from the running token, not
from the service's configuration (docs/research/install-and-rights.md, E3), and E9 reads a pipe client's token as
the service sees it. Read-only: it opens this process's own token (TOKEN_QUERY), or a pipe client's through
ImpersonateNamedPipeClient at the level the client allowed (client_token), never another process. Layouts from
winnt.h (Windows SDK 10.0.19041 and 10.0.26100), x64.
"""
from __future__ import annotations

import contextlib
import ctypes
from ctypes import wintypes
from typing import Iterator

TOKEN_QUERY = 0x8
# TOKEN_INFORMATION_CLASS (winnt.h): TokenUser = 1, then in order.
TOKEN_USER, TOKEN_GROUPS, TOKEN_PRIVILEGES, TOKEN_TYPE, TOKEN_IMPERSONATION_LEVEL = 1, 2, 3, 8, 9
TOKEN_RESTRICTED_SIDS, TOKEN_SESSION_ID, TOKEN_ELEVATION, TOKEN_INTEGRITY_LEVEL = 11, 12, 20, 25
TOKEN_PRIMARY, TOKEN_IMPERSONATION = 1, 2                  # TOKEN_TYPE
# SECURITY_IMPERSONATION_LEVEL, in order from SecurityAnonymous = 0
LEVELS = {0: "anonymous", 1: "identification", 2: "impersonation", 3: "delegation"}
ERROR_BAD_LENGTH, ERROR_INSUFFICIENT_BUFFER, ERROR_NONE_MAPPED = 24, 122, 1332
# Group attributes (winnt.h)
SE_GROUP_ENABLED = 0x4
SE_GROUP_OWNER = 0x8
SE_GROUP_USE_FOR_DENY_ONLY = 0x10
SE_GROUP_LOGON_ID = 0xC0000000
# Privilege attributes (winnt.h)
SE_PRIVILEGE_ENABLED_BY_DEFAULT, SE_PRIVILEGE_ENABLED = 0x1, 0x2
SERVICE_SID_PREFIX = "S-1-5-80-"           # NT SERVICE\<name>


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Luid", LUID), ("Attributes", wintypes.DWORD)]


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


assert ctypes.sizeof(LUID) == 8
assert ctypes.sizeof(LUID_AND_ATTRIBUTES) == 12 and ctypes.alignment(LUID_AND_ATTRIBUTES) == 4
assert ctypes.sizeof(SID_AND_ATTRIBUTES) == 16 and ctypes.alignment(SID_AND_ATTRIBUTES) == 8
# TOKEN_GROUPS is a DWORD count, then SID_AND_ATTRIBUTES[] (8-aligned); TOKEN_PRIVILEGES a DWORD count, then
# LUID_AND_ATTRIBUTES[] (4-aligned); TOKEN_USER and TOKEN_MANDATORY_LABEL are one SID_AND_ATTRIBUTES.
GROUPS_AT, PRIVILEGES_AT = 8, 4

_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
_advapi32.OpenProcessToken.restype = wintypes.BOOL
_advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.DWORD)]
_advapi32.GetTokenInformation.restype = wintypes.BOOL
_advapi32.IsTokenRestricted.argtypes = [wintypes.HANDLE]
_advapi32.IsTokenRestricted.restype = wintypes.BOOL
_advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
_advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
_advapi32.LookupAccountSidW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p, wintypes.LPWSTR,
                                        ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR,
                                        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(ctypes.c_int)]
_advapi32.LookupAccountSidW.restype = wintypes.BOOL
_advapi32.LookupPrivilegeNameW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(LUID), wintypes.LPWSTR,
                                           ctypes.POINTER(wintypes.DWORD)]
_advapi32.LookupPrivilegeNameW.restype = wintypes.BOOL
_advapi32.ImpersonateNamedPipeClient.argtypes = [wintypes.HANDLE]
_advapi32.ImpersonateNamedPipeClient.restype = wintypes.BOOL
_advapi32.OpenThreadToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, ctypes.POINTER(wintypes.HANDLE)]
_advapi32.OpenThreadToken.restype = wintypes.BOOL
_advapi32.RevertToSelf.argtypes = []
_advapi32.RevertToSelf.restype = wintypes.BOOL
_kernel32.GetCurrentThread.argtypes = []
_kernel32.GetCurrentThread.restype = wintypes.HANDLE
_kernel32.GetCurrentProcess.argtypes = []
_kernel32.GetCurrentProcess.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.LocalFree.argtypes = [ctypes.c_void_p]
_kernel32.LocalFree.restype = ctypes.c_void_p


def _winerror(code: int | None = None) -> OSError:
    return ctypes.WinError(ctypes.get_last_error() if code is None else code)


@contextlib.contextmanager
def own_token() -> Iterator[wintypes.HANDLE]:
    """This process's own token, opened for reading only and closed when the block ends."""
    token = wintypes.HANDLE()
    if not _advapi32.OpenProcessToken(_kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
        raise _winerror()
    try:
        yield token
    finally:
        _kernel32.CloseHandle(token)


def _info(token: wintypes.HANDLE, cls: int) -> ctypes.Array:
    """GetTokenInformation's buffer for one class (fixed-size classes answer ERROR_BAD_LENGTH to a short buffer)."""
    size = wintypes.DWORD(0)
    for _attempt in range(4):                     # the answer could grow between two calls
        buf = ctypes.create_string_buffer(max(size.value, 4))
        if _advapi32.GetTokenInformation(token, cls, buf, len(buf), ctypes.byref(size)):
            return buf
        err = ctypes.get_last_error()
        if err not in (ERROR_INSUFFICIENT_BUFFER, ERROR_BAD_LENGTH) or size.value <= len(buf):
            raise _winerror(err)
    raise OSError(f"token information class {cls} kept growing")


def sid_text(psid: int) -> str:
    text = ctypes.c_void_p()
    if not _advapi32.ConvertSidToStringSidW(psid, ctypes.byref(text)):
        raise _winerror()
    try:
        return ctypes.wstring_at(text.value)
    finally:
        _kernel32.LocalFree(text)


def sid_name(psid: int) -> str | None:
    """"DOMAIN\\name" for a SID, or None when Windows has no name for it (a logon SID, for one)."""
    name_len, domain_len, use = wintypes.DWORD(256), wintypes.DWORD(256), ctypes.c_int()
    name, domain = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(256)
    if not _advapi32.LookupAccountSidW(None, psid, name, ctypes.byref(name_len), domain, ctypes.byref(domain_len),
                                       ctypes.byref(use)):
        return None
    return f"{domain.value}\\{name.value}" if domain.value else name.value


def _sid_entry(entry: SID_AND_ATTRIBUTES) -> dict:
    attrs = int(entry.Attributes)
    return {"sid": sid_text(entry.Sid), "name": sid_name(entry.Sid), "attributes": attrs,
            "enabled": bool(attrs & SE_GROUP_ENABLED), "deny_only": bool(attrs & SE_GROUP_USE_FOR_DENY_ONLY),
            "owner": bool(attrs & SE_GROUP_OWNER), "logon_id": (attrs & SE_GROUP_LOGON_ID) == SE_GROUP_LOGON_ID}


def _groups(buf: ctypes.Array) -> list[dict]:
    count = wintypes.DWORD.from_buffer(buf, 0).value
    if count == 0:                                 # an empty list may come back as the count alone
        return []
    if GROUPS_AT + count * ctypes.sizeof(SID_AND_ATTRIBUTES) > len(buf):
        raise OSError(f"a TOKEN_GROUPS of {count} entries does not fit its {len(buf)}-byte buffer")
    return [_sid_entry(g) for g in (SID_AND_ATTRIBUTES * count).from_buffer(buf, GROUPS_AT)]


def privilege_name(luid: LUID) -> str:
    size = wintypes.DWORD(64)
    for _attempt in range(3):
        buf = ctypes.create_unicode_buffer(size.value + 1)
        size = wintypes.DWORD(len(buf))
        if _advapi32.LookupPrivilegeNameW(None, ctypes.byref(luid), buf, ctypes.byref(size)):
            return buf.value
        err = ctypes.get_last_error()
        if err != ERROR_INSUFFICIENT_BUFFER:
            raise _winerror(err)
    raise OSError("a privilege name kept growing")


def _privileges(buf: ctypes.Array) -> list[dict]:
    count = wintypes.DWORD.from_buffer(buf, 0).value
    if PRIVILEGES_AT + count * ctypes.sizeof(LUID_AND_ATTRIBUTES) > len(buf):
        raise OSError(f"a TOKEN_PRIVILEGES of {count} entries does not fit its {len(buf)}-byte buffer")
    out = []
    for p in (LUID_AND_ATTRIBUTES * count).from_buffer(buf, PRIVILEGES_AT):
        attrs = int(p.Attributes)
        out.append({"name": privilege_name(p.Luid), "attributes": attrs, "enabled": bool(attrs & SE_PRIVILEGE_ENABLED),
                    "enabled_by_default": bool(attrs & SE_PRIVILEGE_ENABLED_BY_DEFAULT)})
    return out


def _dword(token: wintypes.HANDLE, cls: int) -> int:
    return wintypes.DWORD.from_buffer(_info(token, cls), 0).value


def read(token: wintypes.HANDLE) -> dict:
    """{"user": {sid, name}, "groups": [...], "privileges": [{name, attributes, enabled, enabled_by_default}],
    "restricted_sids": [...], "restricted": IsTokenRestricted, "session_id", "integrity": SID text, "elevated",
    "type": "primary" or "impersonation", "level": the impersonation level's name, None for a primary token}.
    Groups and restricted SIDs are {sid, name, attributes, enabled, deny_only, owner, logon_id}."""
    user = SID_AND_ATTRIBUTES.from_buffer(_info(token, TOKEN_USER), 0)
    label = SID_AND_ATTRIBUTES.from_buffer(_info(token, TOKEN_INTEGRITY_LEVEL), 0)
    kind = _dword(token, TOKEN_TYPE)
    level = _dword(token, TOKEN_IMPERSONATION_LEVEL) if kind == TOKEN_IMPERSONATION else None
    return {
        "user": {"sid": sid_text(user.Sid), "name": sid_name(user.Sid)},
        "groups": _groups(_info(token, TOKEN_GROUPS)),
        "privileges": _privileges(_info(token, TOKEN_PRIVILEGES)),
        "restricted_sids": _groups(_info(token, TOKEN_RESTRICTED_SIDS)),
        "restricted": bool(_advapi32.IsTokenRestricted(token)),
        "session_id": _dword(token, TOKEN_SESSION_ID),
        "integrity": sid_text(label.Sid),
        "elevated": bool(_dword(token, TOKEN_ELEVATION)),
        "type": {TOKEN_PRIMARY: "primary", TOKEN_IMPERSONATION: "impersonation"}.get(kind, kind),
        "level": None if level is None else LEVELS.get(level, level),
    }


def snapshot() -> dict:
    """This process's own token, read (see read)."""
    with own_token() as token:
        return read(token)


def thread_token_error() -> int | None:
    """None when this thread holds an impersonation token, else OpenThreadToken's error (ERROR_NO_TOKEN when it acts as
    the process): the check that client_token left the thread as it found it."""
    token = wintypes.HANDLE()
    if _advapi32.OpenThreadToken(_kernel32.GetCurrentThread(), TOKEN_QUERY, True, ctypes.byref(token)):
        _kernel32.CloseHandle(token)
        return None
    return ctypes.get_last_error()


def client_token(pipe_handle: int) -> dict:
    """The connected client's token as a pipe server sees it: ImpersonateNamedPipeClient, OpenThreadToken with
    OpenAsSelf (the check runs against this process, so an identification-level token opens), RevertToSelf at once,
    then read. Windows impersonates the sender of the last message read; on build 26200 it also impersonated a client
    that had sent nothing yet (measured 2026-11-25). {"step": ..., "winerror": ...} when a step failed; RuntimeError
    when the thread could not stop impersonating, since everything it did after that would run as the client."""
    if not _advapi32.ImpersonateNamedPipeClient(pipe_handle):
        return {"step": "ImpersonateNamedPipeClient", "winerror": ctypes.get_last_error()}
    token, err = wintypes.HANDLE(), None
    try:
        if not _advapi32.OpenThreadToken(_kernel32.GetCurrentThread(), TOKEN_QUERY, True, ctypes.byref(token)):
            err = ctypes.get_last_error()
    finally:
        if not _advapi32.RevertToSelf():
            if token:
                _kernel32.CloseHandle(token)
            raise RuntimeError(f"RevertToSelf failed (error {ctypes.get_last_error()}): the thread still acts as the "
                               "pipe's client")
    if err is not None:
        return {"step": "OpenThreadToken", "winerror": err}
    try:
        return read(token)
    finally:
        _kernel32.CloseHandle(token)


def service_sids(entries: list[dict]) -> list[str]:
    """The NT SERVICE\\<name> SIDs among token entries (groups or restricted SIDs)."""
    return [e["sid"] for e in entries if e["sid"].startswith(SERVICE_SID_PREFIX)]
