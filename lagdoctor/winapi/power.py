"""Power: the active plan and the installed plans, switching plans, AC/battery/saver, the Windows 11 power-mode
slider, and the effective power mode (which includes "Game Mode is engaged").

Sources: docs/research/windows-telemetry.md (WT) section 7 and its verification log #66-#78, with signatures
re-read in the 10.0.19041 SDK (powersetting.h, powrprof.h, WinBase.h). The slider getters are not in that SDK;
they are guarded and answer None on any doubt.
"""
from __future__ import annotations

import collections
import ctypes
import re
import threading
import uuid
from ctypes import wintypes

LOAD_LIBRARY_SEARCH_SYSTEM32 = 0x00000800
ACCESS_SCHEME = 16                  # POWER_DATA_ACCESSOR: the 17th member of the enum in powrprof.h
ERROR_NO_MORE_ITEMS = 259
EFFECTIVE_POWER_MODE_V2 = 2         # WT 7.3
MAX_SCHEMES = 64
MAX_NAME_BYTES = 4096

# EFFECTIVE_POWER_MODE 0..6 as in powersetting.h. The online docs interleave prerelease aliases and would
# renumber them if read literally, so the header values are hard-coded (WT 7.3, verify log #74-#75).
EFFECTIVE_MODES = ("battery_saver", "better_battery", "balanced", "high_performance", "max_performance",
                   "game_mode", "mixed_reality")

# Power-mode slider overlay GUIDs (WT 7.2). GUID_NULL is what the getters return in Balanced here; the two
# "Better performance" GUIDs are the documented defaults for the same middle position.
SLIDER_MODES = {
    "961cc777-2547-4f9d-8174-7d86181b8a7a": "best_efficiency",
    "00000000-0000-0000-0000-000000000000": "balanced",
    "3af9b8d9-7c97-431d-ad78-34a8bfea439f": "balanced",
    "381b4222-f694-41f0-9685-ff5bb260df2e": "balanced",
    "ded574b5-45a0-4f42-8737-46345c09c238": "best_performance",
}

# The processor settings of a plan that can hold the PC back (WT 7.1; GUIDs and value lists re-read with
# `powercfg /qh SCHEME_CURRENT SUB_PROCESSOR <alias>` on 2026-11-05).
SUB_PROCESSOR = "54533251-82be-4824-96c1-47b60b740d00"
PLAN_SETTINGS = {
    "max_cpu": "bc5038f7-23e0-4960-96da-33abaf5935ec",   # PROCTHROTTLEMAX: maximum processor state, 0..100 %
    "max_cpu1": "bc5038f7-23e0-4960-96da-33abaf5935ed",  # PROCTHROTTLEMAX1: the same for efficiency class 1, the
                                                         # performance cores of a hybrid CPU (Power saver: 75 %)
    "boost": "be337238-0d82-4146-a960-4f3749d470c7",     # PERFBOOSTMODE: 0 Disabled, 1 Enabled, 2 Aggressive, 3..6
    "cooling": "94d3a615-a899-4ac5-ae2b-e4d8f634367f",   # SYSCOOLPOL: 0 Passive, 1 Active
}

AC_LINE = {0: True, 1: False}       # ACLineStatus -> on_battery; 255 = unknown (WT 7.4)
BATTERY_FLAG_NO_BATTERY = 0x80
BATTERY_FLAG_UNKNOWN = 0xFF
SAVER = {0: False, 1: True}         # SystemStatusFlag: 1 = battery saver on

_GUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


class SYSTEM_POWER_STATUS(ctypes.Structure):
    # c_ubyte, not wintypes.BYTE: that one is signed in ctypes and would turn 255 ("unknown") into -1.
    _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", wintypes.DWORD), ("BatteryFullLifeTime", wintypes.DWORD)]


assert ctypes.sizeof(GUID) == 16
assert ctypes.sizeof(SYSTEM_POWER_STATUS) == 12      # WT 7.4

_powrprof = ctypes.WinDLL("powrprof", use_last_error=True, winmode=LOAD_LIBRARY_SEARCH_SYSTEM32)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_powrprof.PowerGetActiveScheme.argtypes = [wintypes.HKEY, ctypes.POINTER(ctypes.POINTER(GUID))]
_powrprof.PowerGetActiveScheme.restype = wintypes.DWORD
_powrprof.PowerSetActiveScheme.argtypes = [wintypes.HKEY, ctypes.POINTER(GUID)]
_powrprof.PowerSetActiveScheme.restype = wintypes.DWORD
_powrprof.PowerReadFriendlyName.argtypes = [wintypes.HKEY, ctypes.POINTER(GUID), ctypes.POINTER(GUID),
                                            ctypes.POINTER(GUID), ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
_powrprof.PowerReadFriendlyName.restype = wintypes.DWORD
_powrprof.PowerEnumerate.argtypes = [wintypes.HKEY, ctypes.POINTER(GUID), ctypes.POINTER(GUID), ctypes.c_int,
                                     wintypes.ULONG, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
_powrprof.PowerEnumerate.restype = wintypes.DWORD
for _read in (_powrprof.PowerReadACValueIndex, _powrprof.PowerReadDCValueIndex):
    _read.argtypes = [wintypes.HKEY, ctypes.POINTER(GUID), ctypes.POINTER(GUID), ctypes.POINTER(GUID),
                      ctypes.POINTER(wintypes.DWORD)]
    _read.restype = wintypes.DWORD
_kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
_kernel32.LocalFree.restype = wintypes.HLOCAL
_kernel32.GetSystemPowerStatus.argtypes = [ctypes.POINTER(SYSTEM_POWER_STATUS)]
_kernel32.GetSystemPowerStatus.restype = wintypes.BOOL

# The setter is a module attribute so a test can swap it for a recorder and never reach the real one.
_PowerSetActiveScheme = _powrprof.PowerSetActiveScheme

_EFFECTIVE_MODE_CALLBACK = ctypes.WINFUNCTYPE(None, ctypes.c_int, ctypes.c_void_p)
# Callbacks "can be called concurrently" on another thread (LC 1.5), so each one stays referenced after
# unregistering. Bounded: effective_mode() is called a few times per session at most.
_callbacks_alive: collections.deque = collections.deque(maxlen=32)
_optional: dict[str, object] = {}


# ---------------------------------------------------------------- pure helpers --

def guid_str(g: GUID) -> str:
    return str(uuid.UUID(bytes_le=bytes(g)))


def guid_from_str(text: str) -> GUID:
    """Canonical 8-4-4-4-12 form only (no braces, no 'urn:'), any case."""
    if not isinstance(text, str) or not _GUID_RE.fullmatch(text):
        raise ValueError(f"not a GUID: {text!r}")
    return GUID.from_buffer_copy(uuid.UUID(text).bytes_le)


def mode_name(mode: int) -> str | None:
    return EFFECTIVE_MODES[mode] if 0 <= mode < len(EFFECTIVE_MODES) else None


def slider_name(guid: str) -> str | None:
    return SLIDER_MODES.get(guid.lower())


def decode_status(ac_line: int, battery_flag: int, percent: int, system_flag: int) -> dict:
    """SYSTEM_POWER_STATUS -> {on_battery, battery_pct, saver}; unknown values become None (WT 7.4)."""
    pct = percent if 0 <= percent <= 100 and battery_flag not in (BATTERY_FLAG_NO_BATTERY, BATTERY_FLAG_UNKNOWN) \
        else None
    return {"on_battery": AC_LINE.get(ac_line), "battery_pct": pct, "saver": SAVER.get(system_flag)}


def decode_battery_present(battery_flag: int) -> bool | None:
    if battery_flag == BATTERY_FLAG_UNKNOWN:
        return None
    return battery_flag != BATTERY_FLAG_NO_BATTERY


# ---------------------------------------------------------------- plans ---------

def _friendly_name(g: GUID) -> str:
    size = wintypes.DWORD(0)
    rc = _powrprof.PowerReadFriendlyName(None, ctypes.byref(g), None, None, None, ctypes.byref(size))
    if rc != 0 or not 0 < size.value <= MAX_NAME_BYTES:
        return ""
    buf = ctypes.create_string_buffer(size.value)
    if _powrprof.PowerReadFriendlyName(None, ctypes.byref(g), None, None, buf, ctypes.byref(size)) != 0:
        return ""
    return buf.raw[:size.value].decode("utf-16-le", errors="replace").split("\0", 1)[0]


def active_scheme() -> tuple[str, str]:
    """(GUID in lower case, friendly name) of the active power plan."""
    ptr = ctypes.POINTER(GUID)()
    rc = _powrprof.PowerGetActiveScheme(None, ctypes.byref(ptr))
    if rc != 0:
        raise ctypes.WinError(rc, f"PowerGetActiveScheme failed ({rc})")
    try:
        g = GUID.from_buffer_copy(ptr.contents)
    finally:
        _kernel32.LocalFree(ctypes.cast(ptr, ctypes.c_void_p))   # the API's page says LocalFree (WT 7.1)
    return guid_str(g), _friendly_name(g)


def list_schemes() -> list[tuple[str, str]]:
    """Installed power plans as (GUID, friendly name), in the power service's order."""
    out: list[tuple[str, str]] = []
    g = GUID()
    for index in range(MAX_SCHEMES):
        size = wintypes.DWORD(ctypes.sizeof(GUID))
        rc = _powrprof.PowerEnumerate(None, None, None, ACCESS_SCHEME, index, ctypes.byref(g), ctypes.byref(size))
        if rc == ERROR_NO_MORE_ITEMS:
            break
        if rc != 0:
            raise ctypes.WinError(rc, f"PowerEnumerate({index}) failed ({rc})")
        copy = GUID.from_buffer_copy(g)
        out.append((guid_str(copy), _friendly_name(copy)))
    return out


def plan_settings(scheme: str) -> dict[str, int | None]:
    """A plan's processor settings, plugged in and on battery: {"max_cpu_ac", "max_cpu_dc", "boost_ac", ...}, each
    the stored index (PLAN_SETTINGS says what it means) or None when Windows would not read it. Read-only."""
    g, sub = guid_from_str(scheme), guid_from_str(SUB_PROCESSOR)
    out: dict[str, int | None] = {}
    for key, setting in PLAN_SETTINGS.items():
        s = guid_from_str(setting)
        for side, read in (("ac", _powrprof.PowerReadACValueIndex), ("dc", _powrprof.PowerReadDCValueIndex)):
            value = wintypes.DWORD(0)
            rc = read(None, ctypes.byref(g), ctypes.byref(sub), ctypes.byref(s), ctypes.byref(value))
            out[f"{key}_{side}"] = int(value.value) if rc == 0 else None
    return out


def set_active_scheme(guid: str) -> None:
    """PowerSetActiveScheme: allowed for standard users (WT 7.5). Raises OSError if Windows refuses."""
    g = guid_from_str(guid)
    rc = _PowerSetActiveScheme(None, ctypes.byref(g))
    if rc != 0:
        raise ctypes.WinError(rc, f"PowerSetActiveScheme({guid}) failed ({rc})")


# ---------------------------------------------------------------- status --------

def _status() -> SYSTEM_POWER_STATUS:
    st = SYSTEM_POWER_STATUS()
    if not _kernel32.GetSystemPowerStatus(ctypes.byref(st)):
        raise ctypes.WinError(ctypes.get_last_error())
    return st


def power_status() -> dict:
    st = _status()
    return decode_status(st.ACLineStatus, st.BatteryFlag, st.BatteryLifePercent, st.SystemStatusFlag)


def battery_present() -> bool | None:
    """True when the PC has a system battery (a laptop); None when Windows does not know."""
    return decode_battery_present(_status().BatteryFlag)


# ---------------------------------------------------------------- slider --------

def _optional_export(name: str, argtypes: list, restype):
    """An export that may be missing on this Windows build; None if so."""
    if name not in _optional:
        try:
            fn = getattr(_powrprof, name)
        except AttributeError:
            fn = None
        else:
            fn.argtypes, fn.restype = argtypes, restype
        _optional[name] = fn
    return _optional[name]


def slider_mode(on_ac: bool) -> str | None:
    """The power-mode slider the user set for AC (True) or battery (False), or None on any doubt.

    PowerGetUserConfigured{AC,DC}PowerMode are exported but not in the 19041 SDK; the signature
    DWORD f(GUID*) is the one that ran here (WT 7.2, verify log #68-#72).
    """
    if not isinstance(on_ac, bool):
        raise TypeError(f"on_ac must be True or False, not {on_ac!r}")
    name = "PowerGetUserConfiguredACPowerMode" if on_ac else "PowerGetUserConfiguredDCPowerMode"
    fn = _optional_export(name, [ctypes.POINTER(GUID)], wintypes.DWORD)
    if fn is None:
        return None
    g = GUID()
    try:
        rc = fn(ctypes.byref(g))
    except OSError:
        return None
    if rc != 0:
        return None
    return slider_name(guid_str(g))


# ---------------------------------------------------------------- effective -----

def effective_mode(timeout_s: float = 1.0) -> str | None:
    """The effective power mode now, from the registration callback (V2); None if none arrived in time.

    Windows calls the callback once on registration with the current mode (WT 7.3); we then unregister.
    """
    if not 0 < timeout_s <= 30:
        raise ValueError(f"timeout_s must be in (0, 30], not {timeout_s!r}")
    register = _optional_export("PowerRegisterForEffectivePowerModeNotifications",
                                [wintypes.ULONG, _EFFECTIVE_MODE_CALLBACK, ctypes.c_void_p,
                                 ctypes.POINTER(ctypes.c_void_p)], ctypes.c_long)
    unregister = _optional_export("PowerUnregisterFromEffectivePowerModeNotifications", [ctypes.c_void_p],
                                  ctypes.c_long)
    if register is None or unregister is None:
        return None
    got: list[int] = []
    arrived = threading.Event()

    def on_mode(mode: int, _context: int) -> None:
        if not got:
            got.append(mode)
        arrived.set()

    callback = _EFFECTIVE_MODE_CALLBACK(on_mode)
    _callbacks_alive.append(callback)
    handle = ctypes.c_void_p()
    if register(EFFECTIVE_POWER_MODE_V2, callback, None, ctypes.byref(handle)) < 0:
        return None
    try:
        arrived.wait(timeout_s)
    finally:
        unregister(handle)
    return mode_name(got[0]) if got else None
