"""ETW rights of this process, and the one UAC relaunch lag-doctor may ask for.

Starting an ETW session (PresentMon's, the network tracker's) needs an administrator or a member of
Performance Log Users (docs/research/presentmon.md 6.1). CheckTokenMembership counts a SID only when
it is present and enabled, so a UAC-filtered admin token, whose Administrators SID is deny-only,
answers False exactly as StartTraceW refuses it (PM 6.3; verification log C7). TokenElevation is
only a proxy for that rule, which is why can_trace() does not use it.

Also here, because both must work across the elevation boundary: the user SID of this token (an
over-the-shoulder elevation runs as another account), and a named mutex that an unelevated watch and an
elevated capture child both see (a lock file would not do: the elevated child may have another profile).

Group membership is copied into a token at sign-in, so an account that joins Performance Log Users gets the
right only from its next sign-in on; trace_rights() tells that state apart by reading the group's members
(NetLocalGroupGetMembers, which any local user may call) beside this token's own groups. It also reads the
token's whole group list (TokenGroups), which shows a group that is present but not enabled: one that a new
sign-in would not switch on either.
"""
from __future__ import annotations

import ctypes
import os
import re
import subprocess
import time
from ctypes import wintypes

ADMINISTRATORS_SID = "S-1-5-32-544"
PERFORMANCE_LOG_USERS_SID = "S-1-5-32-559"

TOKEN_USER_CLASS = 1                     # TokenUser in TOKEN_INFORMATION_CLASS (SDK 10.0.19041 winnt.h)
TOKEN_GROUPS_CLASS = 2                   # TokenGroups in TOKEN_INFORMATION_CLASS (SDK 10.0.19041 winnt.h)
TOKEN_ELEVATION_CLASS = 20               # TokenElevation in TOKEN_INFORMATION_CLASS (SDK 10.0.19041 winnt.h)
SE_GROUP_MANDATORY = 0x01                # winnt.h: SID_AND_ATTRIBUTES.Attributes bits of a token's group
SE_GROUP_ENABLED_BY_DEFAULT = 0x02
SE_GROUP_ENABLED = 0x04
SE_GROUP_USE_FOR_DENY_ONLY = 0x10        # present, but only ever matched by deny entries (UAC's filtered admin)
CURRENT_PROCESS_TOKEN = -4               # GetCurrentProcessToken(): pseudo-handle, never closed (processthreadsapi.h)
ERROR_ACCESS_DENIED = 5                  # winerror.h
ERROR_ALREADY_EXISTS = 183               # winerror.h
ERROR_MORE_DATA = 234                    # winerror.h
NERR_SUCCESS = 0                         # lmerr.h
MAX_PREFERRED_LENGTH = 0xFFFFFFFF        # lmcons.h: (DWORD)-1, "allocate whatever the whole list needs"
MAX_MEMBER_PAGES = 1000                  # NetLocalGroupGetMembers pages read before giving up (a bounded loop)

SEE_MASK_NOCLOSEPROCESS = 0x00000040     # shellapi.h: hand back hProcess
SEE_MASK_NOASYNC = 0x00000100            # finish the launch before returning (no message loop here)
SEE_MASK_NO_CONSOLE = 0x00008000         # the elevated child gets its own console
# Not SEE_MASK_FLAG_NO_UI: it is documented for error boxes only, and whether it also hides the consent
# prompt was not testable here (no test may raise one); missing files are refused before the call instead.
SW_SHOWNORMAL = 1
ERROR_CANCELLED = 1223                   # the user answered No at the UAC prompt (winerror.h)
WAIT_OBJECT_0, WAIT_TIMEOUT = 0x0, 0x102
INFINITE_POLL_MS = 250                   # wait in slices so Ctrl+C still reaches Python
_CONTROL = re.compile(r"[\x00-\x1f]")   # NUL, tab, newline ...: never in an argument of an elevated launch


class TOKEN_ELEVATION(ctypes.Structure):
    _fields_ = [("TokenIsElevated", wintypes.DWORD)]


class SID_AND_ATTRIBUTES(ctypes.Structure):
    # TOKEN_USER is exactly one of these (winnt.h).
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class TOKEN_GROUPS(ctypes.Structure):
    # winnt.h: GroupCount, then Groups[ANYSIZE_ARRAY]; the real array holds GroupCount entries, and the SIDs
    # they point at follow it in the same buffer.
    _fields_ = [("GroupCount", wintypes.DWORD), ("Groups", SID_AND_ATTRIBUTES * 1)]


class SHELLEXECUTEINFOW(ctypes.Structure):
    # shellapi.h; the union of hIcon/hMonitor is one HANDLE.
    _fields_ = [("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG), ("hwnd", wintypes.HWND),
                ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
                ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]


class LOCALGROUP_MEMBERS_INFO_0(ctypes.Structure):
    # lmaccess.h: one PSID per member.
    _fields_ = [("lgrmi0_sid", ctypes.c_void_p)]


# The research notes give no sizes for these; these are the x64 sizes from the SDK headers above.
assert ctypes.sizeof(TOKEN_ELEVATION) == 4
assert ctypes.sizeof(SID_AND_ATTRIBUTES) == 16
assert ctypes.sizeof(TOKEN_GROUPS) == 24 and TOKEN_GROUPS.Groups.offset == 8     # the array is pointer-aligned
assert ctypes.sizeof(SHELLEXECUTEINFOW) == 112
assert ctypes.sizeof(LOCALGROUP_MEMBERS_INFO_0) == 8

_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)
_netapi32 = ctypes.WinDLL("netapi32", use_last_error=True)

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
_advapi32.LookupAccountSidW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p, wintypes.LPWSTR,
                                        ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR,
                                        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(ctypes.c_int)]
_advapi32.LookupAccountSidW.restype = wintypes.BOOL
_netapi32.NetLocalGroupGetMembers.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                              ctypes.POINTER(ctypes.c_void_p), wintypes.DWORD,
                                              ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
                                              ctypes.POINTER(ctypes.c_size_t)]
_netapi32.NetLocalGroupGetMembers.restype = wintypes.DWORD        # NET_API_STATUS
_netapi32.NetApiBufferFree.argtypes = [ctypes.c_void_p]
_netapi32.NetApiBufferFree.restype = wintypes.DWORD

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


def _token_information(info_class: int) -> ctypes.Array:
    """One variable-size TOKEN_INFORMATION_CLASS of this process's token, in a buffer of the size Windows asks."""
    token = wintypes.HANDLE(CURRENT_PROCESS_TOKEN)
    size = wintypes.DWORD()
    _advapi32.GetTokenInformation(token, info_class, None, 0, ctypes.byref(size))   # asks for the size
    if not size.value:
        raise _winerror()
    buf = ctypes.create_string_buffer(size.value)
    if not _advapi32.GetTokenInformation(token, info_class, buf, size, ctypes.byref(size)):
        raise _winerror()
    return buf


def token_user_sid() -> str:
    """The user SID of this process's token, e.g. "S-1-5-21-...-1001" (TokenUser)."""
    return _sid_text(SID_AND_ATTRIBUTES.from_buffer(_token_information(TOKEN_USER_CLASS)).Sid)


def _token_groups() -> dict[str, int]:
    """Every group in this process's token (TokenGroups): SID text -> its SE_GROUP_* attribute bits.

    Unlike CheckTokenMembership, this also lists a group that is present but not enabled (deny-only). A SID
    listed twice (never seen) keeps the bits of both entries. OSError when Windows cannot say."""
    buf = _token_information(TOKEN_GROUPS_CLASS)
    start = TOKEN_GROUPS.Groups.offset
    count = wintypes.DWORD.from_buffer(buf).value if len(buf) >= start else None      # TOKEN_GROUPS.GroupCount
    if count is None or start + count * ctypes.sizeof(SID_AND_ATTRIBUTES) > len(buf):
        raise OSError(f"this token's group list ({count} groups) does not fit the {len(buf)} bytes Windows returned")
    groups: dict[str, int] = {}
    for entry in (SID_AND_ATTRIBUTES * count).from_buffer(buf, start):
        sid = _sid_text(entry.Sid)
        groups[sid] = groups.get(sid, 0) | entry.Attributes
    return groups


def _enabled(attributes: int) -> bool:
    """A token group that counts for CheckTokenMembership: enabled, and not deny-only."""
    return bool(attributes & SE_GROUP_ENABLED) and not attributes & SE_GROUP_USE_FOR_DENY_ONLY


def _sid_text(psid: int | None) -> str:
    text = ctypes.c_void_p()
    if not _advapi32.ConvertSidToStringSidW(psid, ctypes.byref(text)):
        raise _winerror()
    try:
        return ctypes.wstring_at(text.value)
    finally:
        _kernel32.LocalFree(text)


def _account_name(psid: int) -> str:
    """The account name of a SID on this PC (LookupAccountSidW), e.g. the local, translated group name."""
    name_len, domain_len, use = wintypes.DWORD(0), wintypes.DWORD(0), ctypes.c_int()
    _advapi32.LookupAccountSidW(None, psid, None, ctypes.byref(name_len), None, ctypes.byref(domain_len),
                                ctypes.byref(use))                               # asks for the sizes
    if not name_len.value:
        raise _winerror()
    name = ctypes.create_unicode_buffer(name_len.value)
    domain = ctypes.create_unicode_buffer(max(domain_len.value, 1))
    if not _advapi32.LookupAccountSidW(None, psid, name, ctypes.byref(name_len), domain, ctypes.byref(domain_len),
                                       ctypes.byref(use)):
        raise _winerror()
    return name.value


_NetLocalGroupGetMembers = _netapi32.NetLocalGroupGetMembers      # tests replace it to stage paging and errors,
_NetApiBufferFree = _netapi32.NetApiBufferFree                    # and this, since a staged buffer is not Windows'


def group_member_sids(group_sid: str) -> list[str]:
    """The SIDs of the direct members of the local group with this SID, as text. The group's name is translated
    on non-English Windows, so it is looked up from the SID first. Members that are themselves groups
    (INTERACTIVE, a domain group) are listed as such, not expanded. OSError when Windows cannot say."""
    psid = ctypes.c_void_p()
    if not _advapi32.ConvertStringSidToSidW(group_sid, ctypes.byref(psid)):
        raise _winerror()
    try:
        name = _account_name(psid.value)
    finally:
        _kernel32.LocalFree(psid)
    members: list[str] = []
    resume = ctypes.c_size_t(0)
    for _page in range(MAX_MEMBER_PAGES):
        buf = ctypes.c_void_p()
        read, total = wintypes.DWORD(0), wintypes.DWORD(0)
        status = _NetLocalGroupGetMembers(None, name, 0, ctypes.byref(buf), MAX_PREFERRED_LENGTH, ctypes.byref(read),
                                          ctypes.byref(total), ctypes.byref(resume))
        try:
            if status not in (NERR_SUCCESS, ERROR_MORE_DATA):
                raise ctypes.WinError(status)
            if read.value:
                entries = (LOCALGROUP_MEMBERS_INFO_0 * read.value).from_address(buf.value)
                members.extend(_sid_text(e.lgrmi0_sid) for e in entries)
        finally:
            if buf.value:
                _NetApiBufferFree(buf)
        if status == NERR_SUCCESS:
            return members
        if not read.value:                       # "more data" yet nothing read: it would never finish
            break
    raise OSError(f"the members of {group_sid} did not finish listing")


def _joined(group_sid: str) -> bool:
    """Whether the local group lists this account, or a group enabled in this token, among its direct members.
    A member group that is deny-only in this token does not count. OSError when either cannot be read."""
    me = token_user_sid()
    return any(sid == me or _is_member(sid) for sid in group_member_sids(group_sid))


def trace_rights() -> str:
    """Whether this account may start ETW sessions, for what setup says. Decided in this order:

    'now'           this token may (an elevated administrator, or a member of Performance Log Users);
    'filtered'      Performance Log Users is in this token but not enabled (deny-only, as UAC leaves
                    Administrators in a normal window): the next sign-in gets the same kind of token, so
                    signing out would not help, and only an elevated recording may trace;
    'after_sign_in' the account, or a group enabled in this token, is a member of Performance Log Users, but
                    this sign-in began before it joined: the token gets the group at the next sign-in;
    'no'            neither;
    'unknown'       Windows could not say (this token, its groups or the group's members could not be read).

    A member reached only through a group that is deny-only in this token (Administrators, nested into
    Performance Log Users) reads as 'no'.
    """
    try:
        if can_trace():
            return "now"
        attributes = _token_groups().get(PERFORMANCE_LOG_USERS_SID)
        if attributes is not None and not _enabled(attributes):
            return "filtered"
        joined = _joined(PERFORMANCE_LOG_USERS_SID)
    except OSError:
        return "unknown"
    return "after_sign_in" if joined else "no"


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
    argv[0] and cwd must be absolute: an elevated launch must never search PATH for its program. ValueError,
    before any prompt, for an empty argument or one holding a control character: lag-doctor never builds
    either, so one means a bug upstream, found here rather than in an elevated window that closes at once.
    """
    if not argv or not all(isinstance(a, str) and a and not _CONTROL.search(a) for a in argv):
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
