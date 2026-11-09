"""Who may enable lag-doctor's trace providers on this PC: the permissions Windows checks, read once.

    cd <repo>; uv run python scripts/etw_access.py

Read-only: it asks Windows for each provider's security descriptor (EventAccessQuery) and lists which
providers have one of their own (the WMI\\Security registry key); it changes nothing. Windows shows these
only to administrators, so the script relaunches itself once through a UAC prompt; that elevated copy only reads
the descriptors and saves them as text. The verdicts are worked out back in the first window, by Windows' own
access check (authz.dll) on descriptors rebuilt from that text: for you as you are now plus Performance Log Users,
and for models of a LocalService and a SYSTEM service. Started from an administrator window it refuses, because
the verdict for you must come from your normal rights. It settles two open questions
(docs/research/install-and-rights.md, E1): may a member of Performance Log Users enable the game-server
tracker's provider, and may a service running as LocalService enable every provider PresentMon uses?

Exit codes: 0 the table was printed; 1 the access check could not run here (the elevated copy: reading failed);
2 refused before any prompt (an administrator window, or --out taken, in a missing folder, or in one this account
cannot create files in); 3 the prompt was declined, or the elevated copy could not start, failed or saved nothing
(the elevated copy: it is not elevated); 4 the elevated copy could not save its result.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import ctypes
import json
import os
import sys
import tempfile
import time
import traceback
import uuid
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lagdoctor.winapi import elevation  # noqa: E402
from lagdoctor.winapi.etw import GUID  # noqa: E402

DEFAULT_GUID = "0811c1af-7a07-4a06-82ed-869455cdf713"     # DefaultTraceSecurityGuid: for every GUID without its own
DEFAULT_ROW = "default (every GUID without its own)"

# What lag-doctor needs each row for: the summary's "needed for" column and its verdict lines.
FRAME_TIMING = "frame timing"
MAY_BE_REFUSED = "frame timing (may be refused)"
TRACKER = "game-server tracker"
UNUSED = "unused"
EVERY_SESSION = "every session"          # the default row: starting and reading each real-time session

# Name -> (GUID, needed for). The GUIDs are read from PresentMon v2.6.0's PresentData/ETW headers and, for the
# registered ones, from `logman query providers <name>` on this PC (both agree, 2026-11-06). lag-doctor starts
# PresentMon with --track_frame_type and without --track_pc_latency (lagdoctor/capture/presentmon.py FLAGS;
# DECISIONS 2026-11-09), so PresentMon 2.6.0 enables every provider here but Direct3D12, which it enables only for
# D3D12 shader-compilation tracking, a setting its console app never turns on, and Nvidia_PCL, which it enables only
# with --track_pc_latency (EnableProvidersListing in PresentData/PresentMonTraceSession.cpp). A refused enable aborts
# PresentMon's session, except on Kernel-Process: its ERROR_ACCESS_DENIED PresentMon tolerates, losing only process
# start and stop events. Kernel-Network is the game-server tracker's (lagdoctor/capture/netflows.py KERNEL_NETWORK).
# Direct3D12 and Nvidia_PCL are read anyway, in case the flags change.
PROVIDERS = {
    "Microsoft-Windows-Kernel-Network": ("7dd42a49-5329-4832-8dfd-43d979153a88", TRACKER),
    "Microsoft-Windows-Kernel-Process": ("22fb2cd6-0e7b-422b-a0c7-2fad1fd0e716", MAY_BE_REFUSED),
    "Microsoft-Windows-DxgKrnl": ("802ec45a-1e99-4b83-9920-87c98277ba9d", FRAME_TIMING),
    "Microsoft-Windows-DxgKrnl (Windows 7 classic)": ("65cd4c8a-0848-4583-92a0-31c0fbaf00c0", FRAME_TIMING),
    "Microsoft-Windows-Win32k": ("8c416c79-d49b-4f01-a467-e56d3aa8234c", FRAME_TIMING),
    "Microsoft-Windows-Dwm-Core": ("9e9bba3c-2e38-40cb-99f4-9e8281425164", FRAME_TIMING),
    "Microsoft-Windows-Dwm-Core (Windows 7 classic)": ("8c9dd1ad-e6e5-4b07-b455-684a9d879900", FRAME_TIMING),
    "Microsoft-Windows-DXGI": ("ca11c036-0102-4a2d-a6ad-f03cfed5d3c9", FRAME_TIMING),
    "Microsoft-Windows-D3D9": ("783aca0a-790e-4d7f-8451-aa850511c6b9", FRAME_TIMING),
    "Microsoft-Windows-Direct3D12": ("5d8087dd-3a9b-4f56-90df-49196cdc4f11", UNUSED),
    "Intel-PresentMon": ("ecaa4712-4644-442f-b94c-a32f6cf8a499", FRAME_TIMING),
    "NvidiaDisplayDriver": ("ae4f8626-8265-40d1-a70b-11b64240e8e9", FRAME_TIMING),
    "Nvidia_PCL": ("0d216f06-82a6-4d49-bc4f-8f38ae56efab", UNUSED),
}

YOU, LOCAL_SERVICE, SYSTEM = "You+group", "LocalService (model)", "SYSTEM (model)"
# Who asks: (user SID, or None for this process's own token; SIDs added as enabled groups). "You" is this account
# exactly as Windows sees it in this window (every group, deny-only ones too) plus Performance Log Users: what
# joining the group gives after the next sign-in. LocalService and SYSTEM are models, not real service tokens
# (E3 measures one): authz.dll on build 26200 already adds Everyone, Authenticated Users and Administrators (SYSTEM),
# or Everyone, Authenticated Users, SERVICE, This Organization and Users (LocalService), to a context made from
# these SIDs even with AUTHZ_SKIP_TOKEN_GROUPS (measured 2026-11-06), so most groups below repeat those.
CLIENTS = {
    YOU: (None, (elevation.PERFORMANCE_LOG_USERS_SID,)),
    LOCAL_SERVICE: ("S-1-5-19", ("S-1-1-0", "S-1-5-6", "S-1-5-11", "S-1-5-32-545", "S-1-2-0")),
    SYSTEM: ("S-1-5-18", ("S-1-5-32-544", "S-1-1-0", "S-1-5-11")),
}
TRACELOG_GUID_ENABLE = 0x80              # wmistr.h: enable this provider into a session
TRACELOG_CREATE_REALTIME = 0x20          # wmistr.h: start a real-time session with this GUID
TRACELOG_ACCESS_REALTIME = 0x400         # wmistr.h: consume a real-time session
REALTIME_SESSION = TRACELOG_CREATE_REALTIME | TRACELOG_ACCESS_REALTIME
SDDL_REVISION_1 = 1
# OWNER_/GROUP_/DACL_/LABEL_SECURITY_INFORMATION (winnt.h): the label too, so a descriptor carrying one is seen.
SDDL_PARTS = 0x1 | 0x2 | 0x4 | 0x10
ERROR_SUCCESS, ERROR_ACCESS_DENIED, ERROR_MORE_DATA = 0, 5, 234
WMI_SECURITY_KEY = r"SYSTEM\CurrentControlSet\Control\WMI\Security"
# ACE types (winnt.h) named in the saved list; any other type is saved as its number.
ACE_NAMES = {0x0: "allow", 0x1: "deny", 0x5: "allow-object", 0x6: "deny-object", 0x9: "allow-callback",
             0xA: "deny-callback", 0xB: "allow-callback-object", 0xC: "deny-callback-object"}
OBJECT_ACE_TYPES = {0x5, 0x6, 0x7, 0x8, 0xB, 0xC, 0xF, 0x10}   # Mask, Flags, then up to two GUIDs before the SID
CALLBACK_ACE_TYPES = {0x9, 0xA, 0xB, 0xC}                       # conditional ACEs
ACCESS_ALLOWED_COMPOUND_ACE_TYPE = 0x4                         # no SID at +8
SYSTEM_MANDATORY_LABEL_ACE_TYPE = 0x11
ACE_OBJECT_TYPE_PRESENT, ACE_INHERITED_OBJECT_TYPE_PRESENT = 0x1, 0x2
AUTHZ_RM_FLAG_NO_AUDIT = 0x1             # AuthZ.h (SDK 10.0.19041): no audit, so no SeAuditPrivilege needed
AUTHZ_SKIP_TOKEN_GROUPS = 0x2            # AuthZ.h: a context from a SID, groups not looked up
SE_GROUP_ENABLED_ALL = 0x1 | 0x2 | 0x4   # winnt.h: SE_GROUP_MANDATORY | SE_GROUP_ENABLED_BY_DEFAULT | SE_GROUP_ENABLED
TOKEN_QUERY = 0x8                        # winnt.h
CHILD_COULD_NOT_SAVE = 4                 # the elevated copy's exit code when its result could not be written


class ACE_HEADER(ctypes.Structure):
    _fields_ = [("AceType", ctypes.c_ubyte), ("AceFlags", ctypes.c_ubyte), ("AceSize", wintypes.WORD)]


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class AUTHZ_ACCESS_REQUEST(ctypes.Structure):
    _fields_ = [("DesiredAccess", wintypes.DWORD), ("PrincipalSelfSid", ctypes.c_void_p),
                ("ObjectTypeList", ctypes.c_void_p), ("ObjectTypeListLength", wintypes.DWORD),
                ("OptionalArguments", ctypes.c_void_p)]


class AUTHZ_ACCESS_REPLY(ctypes.Structure):
    _fields_ = [("ResultListLength", wintypes.DWORD), ("GrantedAccessMask", ctypes.POINTER(wintypes.DWORD)),
                ("SaclEvaluationResults", ctypes.POINTER(wintypes.DWORD)), ("Error", ctypes.POINTER(wintypes.DWORD))]


# winnt.h / AuthZ.h (SDK 10.0.19041), x64. ACCESS_ALLOWED_ACE and its kin: header, Mask at +4, SID at +8.
assert ctypes.sizeof(ACE_HEADER) == 4
assert ctypes.sizeof(LUID) == 8
assert ctypes.sizeof(SID_AND_ATTRIBUTES) == 16
assert ctypes.sizeof(AUTHZ_ACCESS_REQUEST) == 40
assert ctypes.sizeof(AUTHZ_ACCESS_REPLY) == 32

_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_authz = ctypes.WinDLL("authz", use_last_error=True)
_advapi32.EventAccessQuery.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p, ctypes.POINTER(wintypes.ULONG)]
_advapi32.EventAccessQuery.restype = wintypes.ULONG
for _get_acl in (_advapi32.GetSecurityDescriptorDacl, _advapi32.GetSecurityDescriptorSacl):
    _get_acl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(ctypes.c_void_p),
                         ctypes.POINTER(wintypes.BOOL)]
    _get_acl.restype = wintypes.BOOL
_advapi32.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
_advapi32.GetAce.restype = wintypes.BOOL
_advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
_advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
_advapi32.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
_advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
_advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
    ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.ULONG)]
_advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
_advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.ULONG)]
_advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
_advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
_advapi32.OpenProcessToken.restype = wintypes.BOOL
_kernel32.GetCurrentProcess.argtypes = []
_kernel32.GetCurrentProcess.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.LocalFree.argtypes = [ctypes.c_void_p]
_kernel32.LocalFree.restype = ctypes.c_void_p
_authz.AuthzInitializeResourceManager.argtypes = [wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                                  wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
_authz.AuthzInitializeResourceManager.restype = wintypes.BOOL
_authz.AuthzInitializeContextFromToken.argtypes = [wintypes.DWORD, wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                                   LUID, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
_authz.AuthzInitializeContextFromToken.restype = wintypes.BOOL
_authz.AuthzInitializeContextFromSid.argtypes = [wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                                 LUID, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
_authz.AuthzInitializeContextFromSid.restype = wintypes.BOOL
_authz.AuthzAddSidsToContext.argtypes = [ctypes.c_void_p, ctypes.POINTER(SID_AND_ATTRIBUTES), wintypes.DWORD,
                                         ctypes.POINTER(SID_AND_ATTRIBUTES), wintypes.DWORD,
                                         ctypes.POINTER(ctypes.c_void_p)]
_authz.AuthzAddSidsToContext.restype = wintypes.BOOL
_authz.AuthzAccessCheck.argtypes = [wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(AUTHZ_ACCESS_REQUEST),
                                    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                                    ctypes.POINTER(AUTHZ_ACCESS_REPLY), ctypes.c_void_p]
_authz.AuthzAccessCheck.restype = wintypes.BOOL
_authz.AuthzFreeContext.argtypes = [ctypes.c_void_p]
_authz.AuthzFreeContext.restype = wintypes.BOOL
_authz.AuthzFreeResourceManager.argtypes = [ctypes.c_void_p]
_authz.AuthzFreeResourceManager.restype = wintypes.BOOL


def _winerror() -> OSError:
    return ctypes.WinError(ctypes.get_last_error())


def _local_string(ptr: ctypes.c_void_p) -> str:
    try:
        return ctypes.wstring_at(ptr.value)
    finally:
        _kernel32.LocalFree(ptr)


def descriptor(guid: str) -> bytes:
    """The self-relative security descriptor ETW checks for this GUID (its own, or the default one).
    PermissionError when this token may not read it (anyone but an administrator)."""
    g = GUID.of(guid)
    size = wintypes.ULONG(0)
    rc = _advapi32.EventAccessQuery(ctypes.byref(g), None, ctypes.byref(size))
    for _attempt in range(3):                     # the descriptor could grow between the two calls
        if rc == ERROR_ACCESS_DENIED:
            raise PermissionError(13, "reading ETW security needs administrator rights", None, ERROR_ACCESS_DENIED)
        if rc not in (ERROR_SUCCESS, ERROR_MORE_DATA) or not size.value:
            raise ctypes.WinError(rc or 13)
        buf = ctypes.create_string_buffer(size.value)
        rc = _advapi32.EventAccessQuery(ctypes.byref(g), buf, ctypes.byref(size))
        if rc == ERROR_SUCCESS:
            return buf.raw[:size.value]
    raise OSError(f"EventAccessQuery kept asking for more room for {guid}")


def descriptor_from_sddl(sddl: str) -> bytes:
    """A self-relative descriptor built from SDDL text (to judge a saved result, and for the tests)."""
    psd, size = ctypes.c_void_p(), wintypes.ULONG(0)
    if not _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, SDDL_REVISION_1, ctypes.byref(psd),
                                                                         ctypes.byref(size)):
        raise _winerror()
    try:
        return ctypes.string_at(psd.value, size.value)
    finally:
        _kernel32.LocalFree(psd)


def sddl_of(sd: bytes) -> str:
    """The descriptor as SDDL text: owner, group, DACL and any integrity label."""
    buf = ctypes.create_string_buffer(sd, len(sd))
    text = ctypes.c_void_p()
    if not _advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(buf, SDDL_REVISION_1, SDDL_PARTS,
                                                                         ctypes.byref(text), None):
        raise _winerror()
    return _local_string(text)


def _ace_addresses(buf: ctypes.Array, sacl: bool = False) -> list[int] | None:
    """Where each entry of the descriptor's DACL (or SACL) starts, in order; None when it has no such list.
    The addresses point into `buf`, so the caller keeps `buf` alive while reading them."""
    present, defaulted, acl = wintypes.BOOL(), wintypes.BOOL(), ctypes.c_void_p()
    get = _advapi32.GetSecurityDescriptorSacl if sacl else _advapi32.GetSecurityDescriptorDacl
    if not get(buf, ctypes.byref(present), ctypes.byref(acl), ctypes.byref(defaulted)):
        raise _winerror()
    if not present.value or not acl.value:
        return None
    count = ctypes.c_ushort.from_address(acl.value + 4).value      # ACL: Revision, Sbz1, AclSize, AceCount, Sbz2
    out = []
    for i in range(count):
        ace = ctypes.c_void_p()
        if not _advapi32.GetAce(acl, i, ctypes.byref(ace)):
            raise _winerror()
        out.append(ace.value)
    return out


def _entry(address: int) -> dict:
    """One ACE as {"type", "flags", "mask", "sid"}; "mask" and "sid" only where this layout has them in bounds."""
    header = ACE_HEADER.from_address(address)
    entry: dict = {"type": ACE_NAMES.get(header.AceType, header.AceType), "flags": header.AceFlags}
    if header.AceSize < 8:
        return entry
    entry["mask"] = wintypes.DWORD.from_address(address + 4).value
    if header.AceType == ACCESS_ALLOWED_COMPOUND_ACE_TYPE:
        return entry
    sid_at = 8
    if header.AceType in OBJECT_ACE_TYPES:
        if header.AceSize < 12:
            return entry
        present = wintypes.DWORD.from_address(address + 8).value
        sid_at = (12 + (16 if present & ACE_OBJECT_TYPE_PRESENT else 0)
                  + (16 if present & ACE_INHERITED_OBJECT_TYPE_PRESENT else 0))
    if sid_at + 8 > header.AceSize:
        return entry
    sub_authorities = ctypes.c_ubyte.from_address(address + sid_at + 1).value   # SID: Revision, SubAuthorityCount
    if sid_at + 8 + 4 * sub_authorities > header.AceSize:
        return entry
    text = ctypes.c_void_p()
    if not _advapi32.ConvertSidToStringSidW(address + sid_at, ctypes.byref(text)):
        raise _winerror()
    entry["sid"] = _local_string(text)
    return entry


def aces(sd: bytes) -> list[dict] | None:
    """The descriptor's DACL entries in order, for reading by eye: {"type": "allow" | "deny" | ... | <number>,
    "flags", "mask", "sid"}. None for a descriptor without a DACL (everyone may do everything)."""
    buf = ctypes.create_string_buffer(sd, len(sd))
    addresses = _ace_addresses(buf)
    return None if addresses is None else [_entry(a) for a in addresses]


def not_weighed(sd: bytes) -> str | None:
    """Why Windows' own check here may answer differently from the kernel's for this descriptor, or None.

    Measured 2026-11-06 on build 26200 against AccessCheck (empty generic mapping) on this PC's own token, where
    authz.dll answered otherwise: it grants through an integrity label that AccessCheck refuses through, refuses
    under a conditional deny (a callback ACE) whose attribute the token lacks where AccessCheck grants, and grants
    past a deny for an object type that AccessCheck refuses on. ETW descriptors are not expected to carry any of
    these [inferred]; a row whose descriptor does reads "?".
    """
    buf = ctypes.create_string_buffer(sd, len(sd))
    if any(ACE_HEADER.from_address(a).AceType == SYSTEM_MANDATORY_LABEL_ACE_TYPE
           for a in _ace_addresses(buf, sacl=True) or ()):
        return "it carries an integrity label, which this check does not weigh as Windows' trace check does"
    for a in _ace_addresses(buf) or ():
        header = ACE_HEADER.from_address(a)
        if header.AceType in CALLBACK_ACE_TYPES:
            return "it has conditional entries, which this check does not weigh as Windows' trace check does"
        if (header.AceType in OBJECT_ACE_TYPES and header.AceSize >= 12
                and wintypes.DWORD.from_address(a + 8).value & ACE_OBJECT_TYPE_PRESENT):
            return "it has entries for an object type, which this check does not weigh as Windows' trace check does"
    return None


@contextlib.contextmanager
def _sids(texts):
    """Binary SIDs for these SID strings, freed when the block ends."""
    pointers: list[ctypes.c_void_p] = []
    try:
        for text in texts:
            psid = ctypes.c_void_p()
            if not _advapi32.ConvertStringSidToSidW(text, ctypes.byref(psid)):
                raise _winerror()
            pointers.append(psid)
        yield [p.value for p in pointers]
    finally:
        for p in pointers:
            _kernel32.LocalFree(p)


class Authz:
    """Windows' own access check (authz.dll), with one client context per entry of `clients` (see CLIENTS).

    Use it in a with-block: every Authz handle it made is freed when the block ends, and at once when making one
    fails. Read-only: it opens this process's own token (TOKEN_QUERY), never another process.
    """

    def __init__(self, clients: dict[str, tuple[str | None, tuple[str, ...]]] = CLIENTS) -> None:
        self._contexts: dict[str, int] = {}
        self._rm: int | None = None
        rm = ctypes.c_void_p()
        if not _authz.AuthzInitializeResourceManager(AUTHZ_RM_FLAG_NO_AUDIT, None, None, None, None, ctypes.byref(rm)):
            raise _winerror()
        self._rm = rm.value
        try:
            for name, (user, groups) in clients.items():
                self._contexts[name] = self._context(user, groups)
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> Authz:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        """Free every client context, then the resource manager. Idempotent."""
        contexts, self._contexts = self._contexts, {}
        for handle in contexts.values():
            _authz.AuthzFreeContext(handle)
        rm, self._rm = self._rm, None
        if rm:
            _authz.AuthzFreeResourceManager(rm)

    def _context(self, user: str | None, groups: tuple[str, ...]) -> int:
        base = ctypes.c_void_p()
        if user is None:                           # this process's own token: our own process only
            token = wintypes.HANDLE()
            if not _advapi32.OpenProcessToken(_kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
                raise _winerror()
            try:
                if not _authz.AuthzInitializeContextFromToken(0, token, self._rm, None, LUID(), None,
                                                              ctypes.byref(base)):
                    raise _winerror()
            finally:
                _kernel32.CloseHandle(token)
        else:
            with _sids([user]) as (psid,):
                if not _authz.AuthzInitializeContextFromSid(AUTHZ_SKIP_TOKEN_GROUPS, psid, self._rm, None, LUID(),
                                                            None, ctypes.byref(base)):
                    raise _winerror()
        if not groups:
            return base.value
        try:
            return self._add_groups(base.value, groups)
        finally:
            _authz.AuthzFreeContext(base)          # the new context is a copy: the base one is no longer needed

    @staticmethod
    def _add_groups(base: int, groups: tuple[str, ...]) -> int:
        with _sids(groups) as pointers:
            array = (SID_AND_ATTRIBUTES * len(pointers))()
            for i, psid in enumerate(pointers):
                array[i].Sid, array[i].Attributes = psid, SE_GROUP_ENABLED_ALL
            new = ctypes.c_void_p()
            if not _authz.AuthzAddSidsToContext(base, array, len(pointers), None, 0, ctypes.byref(new)):
                raise _winerror()
            return new.value

    def granted(self, client: str, sd: bytes, desired: int) -> bool:
        """Whether Windows grants this client every right in `desired` on this self-relative descriptor."""
        buf = ctypes.create_string_buffer(sd, len(sd))
        request = AUTHZ_ACCESS_REQUEST(DesiredAccess=desired)
        mask, error = wintypes.DWORD(0), wintypes.DWORD(ERROR_ACCESS_DENIED)
        reply = AUTHZ_ACCESS_REPLY(ResultListLength=1, GrantedAccessMask=ctypes.pointer(mask),
                                   SaclEvaluationResults=None, Error=ctypes.pointer(error))
        if not _authz.AuthzAccessCheck(0, self._contexts[client], ctypes.byref(request), None, buf, None, 0,
                                       ctypes.byref(reply), None):
            raise _winerror()
        return error.value == ERROR_SUCCESS and (mask.value & desired) == desired


def own_descriptors() -> set[str] | None:
    """GUIDs (lower case) with a descriptor of their own under WMI\\Security; None when it cannot be read whole."""
    import winreg
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, WMI_SECURITY_KEY)
    except OSError:
        return None
    with key:
        try:
            count = winreg.QueryInfoKey(key)[1]
            return {winreg.EnumValue(key, i)[0].strip("{}").lower() for i in range(count)}
        except OSError:                           # a list cut short would call a GUID's own descriptor "no"
            return None


def read_all() -> dict:
    """What the elevated copy saves: per row its GUID, whether it has a descriptor of its own, and that descriptor
    as SDDL with its DACL entries listed, or the error. No verdicts: those need the first window's token."""
    own = own_descriptors()
    rows = {}
    for name, guid in {DEFAULT_ROW: DEFAULT_GUID, **{n: g for n, (g, _need) in PROVIDERS.items()}}.items():
        row: dict = {"guid": guid, "own_descriptor": None if own is None else guid in own}
        try:
            sd = descriptor(guid)
            row["sddl"] = sddl_of(sd)
        except OSError as exc:
            row["error"] = str(exc)
        else:
            try:
                row["aces"] = aces(sd)
            except OSError as exc:                # the listing is for reading by eye; the SDDL is what counts
                row["aces_error"] = str(exc)
        rows[name] = row
    return {"build": sys.getwindowsversion().build, "elevated": elevation.is_elevated(), "providers": rows}


def add_verdicts(result: dict) -> dict:
    """A copy of the elevated copy's result with Windows' own verdicts on every row it could read: "rights" =
    {client: {"enable": bool, "realtime_session": bool}} for each client in CLIENTS, on the descriptor rebuilt
    from the saved SDDL; or "check_error" when that descriptor cannot be rebuilt or holds something this check
    does not weigh as the kernel does. OSError when the check itself cannot start."""
    judged = copy.deepcopy(result)
    with Authz() as authz:
        for row in judged["providers"].values():
            if not isinstance(row, dict) or not isinstance(row.get("sddl"), str):
                continue
            try:
                sd = descriptor_from_sddl(row["sddl"])
                why = not_weighed(sd)
                if why:
                    row["check_error"] = why
                    continue
                row["rights"] = {c: {"enable": authz.granted(c, sd, TRACELOG_GUID_ENABLE),
                                     "realtime_session": authz.granted(c, sd, REALTIME_SESSION)} for c in CLIENTS}
            except OSError as exc:
                row["check_error"] = str(exc)
    return judged


def needed_for(name: str) -> str:
    if name == DEFAULT_ROW:
        return EVERY_SESSION
    entry = PROVIDERS.get(name)
    return entry[1] if entry else "?"


def _right(row: object, client: str, key: str) -> bool | None:
    """One verdict of a judged row; None when the row could not be read or checked."""
    if not isinstance(row, dict) or not isinstance(row.get("rights"), dict):
        return None
    value = row["rights"].get(client, {}).get(key)
    return value if isinstance(value, bool) else None


def _all(values) -> bool | None:
    """True when every value is True; False as soon as one is False (one refusal settles it); else None."""
    values = list(values)
    if any(v is False for v in values):
        return False
    if any(v is None for v in values):
        return None
    return True


def outcome(judged: dict, client: str) -> dict[str, bool | None]:
    """What this client would get: frame timing and the game-server tracker each need the default row's
    real-time session and every provider they use; "process events" is Kernel-Process, which PresentMon may
    lose without stopping."""
    rows = judged.get("providers", {})
    session = _right(rows.get(DEFAULT_ROW), client, "realtime_session")

    def enables(purpose: str) -> list[bool | None]:
        return [_right(rows.get(name), client, "enable") for name, (_g, need) in PROVIDERS.items() if need == purpose]

    return {FRAME_TIMING: _all([session, *enables(FRAME_TIMING)]), TRACKER: _all([session, *enables(TRACKER)]),
            "process events": _all(enables(MAY_BE_REFUSED))}


def _word(value: object) -> str:
    return {True: "yes", False: "no"}[value] if isinstance(value, bool) else "?"


def _verdict_line(lead: str, got: dict[str, bool | None]) -> str:
    frames = _word(got[FRAME_TIMING])
    if got[FRAME_TIMING] and got["process events"] is False:
        frames += " (without process start and stop events)"
    return f"{lead}: frame timing {frames}; the game-server tracker {_word(got[TRACKER])}"


def summary(judged: dict) -> str:
    rows = judged.get("providers", {})
    lines = [
        f"ETW permissions on Windows build {judged.get('build', '?')}, as Windows' own access check (authz.dll) "
        "answers them",
        "for the descriptors the administrator copy read. Y = allowed, - = refused, ? = not known.",
        "Default row: may start and read a real-time session (frame timing and the game-server tracker each",
        "start one). Every other row: may enable that provider into a session.",
        f"{YOU}: you as you are in this window, plus Performance Log Users (what joining it gives after",
        "you sign in again).",
        f"{LOCAL_SERVICE}, {SYSTEM}: built from the groups such a service usually has, not taken",
        "from a real service, so only a real service can confirm them.",
        "",
    ]
    header = ("provider", "own", "needed for", *CLIENTS)
    widths = [max([len(header[0]), *(len(n) for n in rows)]), len(header[1]),
              max([len(header[2]), *(len(needed_for(n)) for n in rows)]), *(len(c) for c in CLIENTS)]

    def line(cells, rest: str = "") -> str:
        text = "  ".join(f"{cell:<{width}}" for cell, width in zip(cells, widths))
        return f"{text}  {rest}".rstrip()

    lines.append(line(header))
    for name, row in rows.items():
        row = row if isinstance(row, dict) else {}
        start = (name, _word(row.get("own_descriptor")), needed_for(name))
        if isinstance(row.get("rights"), dict):
            key = "realtime_session" if name == DEFAULT_ROW else "enable"
            marks = ({True: "Y", False: "-"}.get(_right(row, c, key), "?") for c in CLIENTS)
            lines.append(line((*start, *marks)))
        elif "error" in row:
            lines.append(line(start, f"could not read: {row['error']}"))
        else:
            lines.append(line(start, f"could not check: {row.get('check_error', 'no descriptor was saved')}"))
    lines += ["", _verdict_line("In Performance Log Users, you would get", outcome(judged, YOU)),
              _verdict_line("A LocalService service would get", outcome(judged, LOCAL_SERVICE))]
    return "\n".join(lines)


def parse(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, help="where to save the full result as JSON (default: a new file in %%TEMP%%)")
    ap.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    return ap.parse_args(argv)


def _save(out: Path, data: dict) -> None:
    """Write the result into a NEW file only: run elevated, this must never overwrite anything."""
    text = json.dumps(data, indent=1)            # first, so a failure here leaves no empty file behind
    with open(out, "x", encoding="utf-8") as f:
        f.write(text)


def _load(out: Path) -> dict | None:
    """The elevated copy's saved result (a reading or its failure), or None when there is none."""
    try:
        data = json.loads(out.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not ("failed" in data or isinstance(data.get("providers"), dict)):
        return None
    return data


def _prove_creatable(folder: Path) -> None:
    """Create a new, uniquely named file in `folder` and delete it again, as this account. os.access cannot
    answer this: it does not read the folder's permissions. OSError when either step fails."""
    probe = folder / f"lag-doctor-write-test-{uuid.uuid4().hex}.tmp"
    os.close(os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    os.unlink(probe)


def _out_problem(out: Path) -> str | None:
    """Why the result could not be saved at `out` and read back here, found before the prompt; None when it can."""
    if os.path.lexists(out):
        return f"{out} already exists; name a new file with --out."
    folder = out.parent
    if not folder.is_dir():
        return f"{folder} is not an existing folder; name a file in an existing folder with --out."
    try:
        _prove_creatable(folder)
    except OSError as exc:
        return (f"This account could not create and delete a test file in {folder} ({exc}); name a file in a "
                "folder of yours with --out.")
    return None


def _child(out: Path) -> int:
    """The elevated copy: read, save into a new file, and print nothing when that worked (its window closes)."""
    if not elevation.is_elevated():
        print("The elevated copy is not elevated; nothing was read.")
        return 3
    try:
        data = read_all()
    except Exception:  # noqa: BLE001 - the elevated window closes at once: hand the reason to the first one
        data = {"failed": traceback.format_exc()}
    try:
        _save(out, data)
    except OSError as exc:
        print(f"Could not save the result to {out}: {exc}")
        return CHILD_COULD_NOT_SAVE
    return 1 if "failed" in data else 0


def _parent(out: Path) -> int:
    if elevation.is_elevated():
        print("This window runs as administrator. Run this from a normal, not administrator, PowerShell window: "
              "the verdict for you must come from your normal rights. Nothing was read.")
        return 2
    problem = _out_problem(out)
    if problem:
        print(f"{problem} Nothing was read.")
        return 2
    print("Reading these needs administrator rights: approve the UAC prompt. Nothing is changed.")
    child = [sys.executable, str(Path(__file__).resolve()), "--out", str(out), "--child"]
    try:
        code = elevation.relaunch_elevated(child, cwd=str(ROOT))
    except PermissionError:
        print("The UAC prompt was declined; nothing was read.")
        return 3
    except (OSError, ValueError) as exc:
        print(f"The elevated copy could not be started ({exc}); nothing was read.")
        return 3
    if code == CHILD_COULD_NOT_SAVE:
        print(f"The elevated copy could not save its result to {out}.")
        return 3
    result = _load(out)
    if code != 0 or result is None or "failed" in result:
        print(f"The elevated copy ended with code {code} and saved no result.")
        if result and isinstance(result.get("failed"), str):
            print(result["failed"])
        return 3
    try:
        judged = add_verdicts(result)
    except OSError as exc:
        print(f"Windows' own access check could not run here ({exc}); what was read is saved in {out}.")
        return 1
    print(summary(judged))
    print(f"\nFull result (paste it back to the agent): {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse(argv)
    out = (args.out or Path(tempfile.gettempdir()) / f"lag-doctor-etw-access-{time.strftime('%Y%m%d-%H%M%S')}.json")
    out = out.resolve()                           # absolute: the elevated copy works in another folder
    return _child(out) if args.child else _parent(out)


if __name__ == "__main__":
    raise SystemExit(main())
