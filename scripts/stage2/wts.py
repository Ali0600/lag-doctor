"""Each process's account as WTSEnumerateProcessesExW reports it at level 1 (WTS_PROCESS_INFO_EXW).

Stage 4's service may relay a game's whole network events only for processes that run as the caller's account
(docs/INSTALLER-PLAN.md, Stage 4), and it must learn that account without a handle to the game. E9 asks whether this
call answers that for a LocalService service: Microsoft's page says "The caller must be a member of the Administrators
group to enumerate processes that are running under another user session". Unelevated on build 26200 it listed every
process of every session but gave a SID only for those this account may query (174 of 285 came back NULL, measured
2026-11-25): it reads each token with the caller's own access, so it opens the processes, and lag-doctor must never
point it at a game. E9 only records what a LocalService service gets from it. Read-only. Layouts and constants from
WtsApi32.h (Windows SDK 10.0.26100), x64.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

WTS_CURRENT_SERVER_HANDLE = None          # ((HANDLE)NULL)
WTS_ANY_SESSION = 0xFFFFFFFE              # ((DWORD)-2)
LEVEL_EX = 1                              # pLevel: an array of WTS_PROCESS_INFO_EX
WTS_TYPE_PROCESS_INFO_LEVEL1 = 1          # WTS_TYPE_CLASS: WTSTypeProcessInfoLevel0, WTSTypeProcessInfoLevel1, ...


class WTS_PROCESS_INFO_EXW(ctypes.Structure):
    _fields_ = [("SessionId", wintypes.DWORD), ("ProcessId", wintypes.DWORD), ("pProcessName", wintypes.LPWSTR),
                ("pUserSid", ctypes.c_void_p), ("NumberOfThreads", wintypes.DWORD), ("HandleCount", wintypes.DWORD),
                ("PagefileUsage", wintypes.DWORD), ("PeakPagefileUsage", wintypes.DWORD),
                ("WorkingSetSize", wintypes.DWORD), ("PeakWorkingSetSize", wintypes.DWORD),
                ("UserTime", ctypes.c_longlong), ("KernelTime", ctypes.c_longlong)]


assert ctypes.sizeof(WTS_PROCESS_INFO_EXW) == 64

_wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)
_wtsapi32.WTSEnumerateProcessesExW.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD), wintypes.DWORD,
                                               ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
_wtsapi32.WTSEnumerateProcessesExW.restype = wintypes.BOOL
_wtsapi32.WTSFreeMemoryExW.argtypes = [ctypes.c_int, ctypes.c_void_p, wintypes.ULONG]
_wtsapi32.WTSFreeMemoryExW.restype = wintypes.BOOL


def processes(pids: list[int], session: int = WTS_ANY_SESSION) -> dict:
    """{"count": entries listed, "sessions": the sessions they came from, "null_sids": entries with no SID,
    "by_pid": {pid: {"session", "name", "sid"} or None when not listed}} for the pids asked about, or
    {"winerror": ...} when the call failed."""
    from stage2 import tokeninfo
    level, buf, count = wintypes.DWORD(LEVEL_EX), ctypes.c_void_p(), wintypes.DWORD(0)
    if not _wtsapi32.WTSEnumerateProcessesExW(WTS_CURRENT_SERVER_HANDLE, ctypes.byref(level), session,
                                              ctypes.byref(buf), ctypes.byref(count)):
        return {"winerror": ctypes.get_last_error(), "level_after": level.value}
    try:
        rows = (WTS_PROCESS_INFO_EXW * count.value).from_address(buf.value) if count.value else []
        listed = {}
        sessions, null_sids = set(), 0
        for r in rows:
            sessions.add(int(r.SessionId))
            null_sids += not r.pUserSid
            if int(r.ProcessId) in pids:
                listed[int(r.ProcessId)] = {"session": int(r.SessionId), "name": r.pProcessName,
                                            "sid": tokeninfo.sid_text(r.pUserSid) if r.pUserSid else None}
        return {"count": count.value, "sessions": sorted(sessions), "null_sids": null_sids,
                "by_pid": {p: listed.get(p) for p in pids}}
    finally:
        _wtsapi32.WTSFreeMemoryExW(WTS_TYPE_PROCESS_INFO_LEVEL1, buf, count.value)
