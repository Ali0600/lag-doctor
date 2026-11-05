"""Displays: the exact refresh rate, adapter LUID and panel type (QueryDisplayConfig), the current mode and the
refresh rates offered at the current resolution (EnumDisplaySettingsW), and a refresh-rate change.

Layouts and constants: docs/research/windows-telemetry.md (WT) section 6 and docs/research/lag-causes.md (LC)
section 9, re-read in the 10.0.19041 SDK headers (wingdi.h, WinUser.h). Win32_VideoController is never used:
it reported a stale 2560x1440@143 Hz here (WT 6.3).
"""
from __future__ import annotations

import ctypes
import re
from ctypes import wintypes

from lagdoctor.winapi.dxgi import LUID, luid_str

ENUM_CURRENT_SETTINGS = 0xFFFFFFFF              # (DWORD)-1 (WT 6.1)
ENUM_REGISTRY_SETTINGS = 0xFFFFFFFE             # (DWORD)-2: the mode kept in the user's profile (WinUser.h; WT 6.1)
DISPLAY_DEVICE_ATTACHED_TO_DESKTOP = 0x1
DISPLAY_DEVICE_PRIMARY_DEVICE = 0x4
DISPLAY_DEVICE_MIRRORING_DRIVER = 0x8           # pseudo devices of mirror drivers: not a monitor
DM_PELSWIDTH = 0x00080000
DM_PELSHEIGHT = 0x00100000
DM_DISPLAYFREQUENCY = 0x00400000
DM_INTERLACED = 0x2                             # in dmDisplayFlags
CDS_UPDATEREGISTRY = 0x1                        # change now and store it in the user's profile (WT 6.4)
CDS_TEST = 0x2                                  # validate only; changes nothing (WT 6.1 negative control)
QDC_ONLY_ACTIVE_PATHS = 0x2
DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME = 1
DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME = 2
ERROR_INSUFFICIENT_BUFFER = 122
ERROR_DEVICE_NOT_CONNECTED = 1167

# ChangeDisplaySettingsExW results (WinUser.h); RESTART is a failure for us: it needs a reboot (WT 6.4).
DISP_CHANGE = {0: "successful", 1: "a restart is needed", -1: "the driver failed the mode", -2: "the mode is not "
               "supported", -3: "the setting could not be saved", -4: "invalid flags", -5: "invalid parameter",
               -6: "invalid dual-view setup"}
DISP_CHANGE_SUCCESSFUL = 0
DISP_CHANGE_BADMODE = -2

# DISPLAYCONFIG_VIDEO_OUTPUT_TECHNOLOGY: built-in panel values from WT 6.2; the external ones are the rest of the
# enum in wingdi.h 10.0.19041 (lines 2805-2821). Anything else (OTHER, newer values) is not judged.
_INTERNAL_TECH = {0x80000000, 6, 11, 13}        # INTERNAL, LVDS, DISPLAYPORT_EMBEDDED, UDI_EMBEDDED
_EXTERNAL_TECH = {0, 1, 2, 3, 4, 5, 8, 9, 10, 12, 14, 15, 16, 17}

MIN_HZ, MAX_HZ = 20, 2000       # a refresh request outside this is junk; 0 and 1 would mean "hardware default"
MAX_DEVICES = 64                # bound for EnumDisplayDevicesW
MAX_MODES = 10_000              # bound for EnumDisplaySettingsW (66 here, WT 6.1)
_DEVICE_RE = re.compile(r"\\\\\.\\DISPLAY[1-9][0-9]{0,2}", re.IGNORECASE)


class DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("DeviceName", ctypes.c_wchar * 32), ("DeviceString", ctypes.c_wchar * 128),
                ("StateFlags", wintypes.DWORD), ("DeviceID", ctypes.c_wchar * 128), ("DeviceKey", ctypes.c_wchar * 128)]


class DEVMODEW(ctypes.Structure):
    # The display variant of the union, flattened (WT 6.1).
    _fields_ = [("dmDeviceName", ctypes.c_wchar * 32), ("dmSpecVersion", wintypes.WORD),
                ("dmDriverVersion", wintypes.WORD), ("dmSize", wintypes.WORD), ("dmDriverExtra", wintypes.WORD),
                ("dmFields", wintypes.DWORD), ("dmPositionX", wintypes.LONG), ("dmPositionY", wintypes.LONG),
                ("dmDisplayOrientation", wintypes.DWORD), ("dmDisplayFixedOutput", wintypes.DWORD),
                ("dmColor", ctypes.c_short), ("dmDuplex", ctypes.c_short), ("dmYResolution", ctypes.c_short),
                ("dmTTOption", ctypes.c_short), ("dmCollate", ctypes.c_short), ("dmFormName", ctypes.c_wchar * 32),
                ("dmLogPixels", wintypes.WORD), ("dmBitsPerPel", wintypes.DWORD), ("dmPelsWidth", wintypes.DWORD),
                ("dmPelsHeight", wintypes.DWORD), ("dmDisplayFlags", wintypes.DWORD),
                ("dmDisplayFrequency", wintypes.DWORD), ("dmICMMethod", wintypes.DWORD),
                ("dmICMIntent", wintypes.DWORD), ("dmMediaType", wintypes.DWORD), ("dmDitherType", wintypes.DWORD),
                ("dmReserved1", wintypes.DWORD), ("dmReserved2", wintypes.DWORD),
                ("dmPanningWidth", wintypes.DWORD), ("dmPanningHeight", wintypes.DWORD)]


class DISPLAYCONFIG_RATIONAL(ctypes.Structure):
    _fields_ = [("Numerator", ctypes.c_uint32), ("Denominator", ctypes.c_uint32)]


class DISPLAYCONFIG_PATH_SOURCE_INFO(ctypes.Structure):
    _fields_ = [("adapterId", LUID), ("id", ctypes.c_uint32), ("modeInfoIdx", ctypes.c_uint32),
                ("statusFlags", ctypes.c_uint32)]


class DISPLAYCONFIG_PATH_TARGET_INFO(ctypes.Structure):
    _fields_ = [("adapterId", LUID), ("id", ctypes.c_uint32), ("modeInfoIdx", ctypes.c_uint32),
                ("outputTechnology", ctypes.c_uint32), ("rotation", ctypes.c_uint32), ("scaling", ctypes.c_uint32),
                ("refreshRate", DISPLAYCONFIG_RATIONAL), ("scanLineOrdering", ctypes.c_uint32),
                ("targetAvailable", wintypes.BOOL), ("statusFlags", ctypes.c_uint32)]


class DISPLAYCONFIG_PATH_INFO(ctypes.Structure):
    _fields_ = [("sourceInfo", DISPLAYCONFIG_PATH_SOURCE_INFO), ("targetInfo", DISPLAYCONFIG_PATH_TARGET_INFO),
                ("flags", ctypes.c_uint32)]


class DISPLAYCONFIG_MODE_INFO(ctypes.Structure):
    # The 48-byte union (target / source / desktop-image mode) is not read here; the rate comes from the path.
    _fields_ = [("infoType", ctypes.c_uint32), ("id", ctypes.c_uint32), ("adapterId", LUID),
                ("_mode", ctypes.c_uint64 * 6)]


class DISPLAYCONFIG_DEVICE_INFO_HEADER(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("size", ctypes.c_uint32), ("adapterId", LUID), ("id", ctypes.c_uint32)]


class DISPLAYCONFIG_SOURCE_DEVICE_NAME(ctypes.Structure):
    _fields_ = [("header", DISPLAYCONFIG_DEVICE_INFO_HEADER), ("viewGdiDeviceName", ctypes.c_wchar * 32)]


class DISPLAYCONFIG_TARGET_DEVICE_NAME(ctypes.Structure):
    _fields_ = [("header", DISPLAYCONFIG_DEVICE_INFO_HEADER), ("flags", ctypes.c_uint32),
                ("outputTechnology", ctypes.c_uint32), ("edidManufactureId", ctypes.c_uint16),
                ("edidProductCodeId", ctypes.c_uint16), ("connectorInstance", ctypes.c_uint32),
                ("monitorFriendlyDeviceName", ctypes.c_wchar * 64), ("monitorDevicePath", ctypes.c_wchar * 128)]


# Sizes from WT 6.1 and LC 9 (PATH_INFO 72, MODE_INFO 64); the API rejects a wrong header.size with 87.
assert ctypes.sizeof(DISPLAY_DEVICEW) == 840
assert ctypes.sizeof(DEVMODEW) == 220
assert ctypes.sizeof(DISPLAYCONFIG_PATH_INFO) == 72
assert ctypes.sizeof(DISPLAYCONFIG_MODE_INFO) == 64
assert ctypes.sizeof(DISPLAYCONFIG_SOURCE_DEVICE_NAME) == 84
assert ctypes.sizeof(DISPLAYCONFIG_TARGET_DEVICE_NAME) == 420

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.EnumDisplayDevicesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(DISPLAY_DEVICEW),
                                        wintypes.DWORD]
_user32.EnumDisplayDevicesW.restype = wintypes.BOOL
_user32.EnumDisplaySettingsW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(DEVMODEW)]
_user32.EnumDisplaySettingsW.restype = wintypes.BOOL
_user32.ChangeDisplaySettingsExW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(DEVMODEW), wintypes.HWND,
                                             wintypes.DWORD, wintypes.LPVOID]
_user32.ChangeDisplaySettingsExW.restype = wintypes.LONG
_user32.GetDisplayConfigBufferSizes.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
                                                ctypes.POINTER(ctypes.c_uint32)]
_user32.GetDisplayConfigBufferSizes.restype = wintypes.LONG
_user32.QueryDisplayConfig.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
                                       ctypes.POINTER(DISPLAYCONFIG_PATH_INFO), ctypes.POINTER(ctypes.c_uint32),
                                       ctypes.POINTER(DISPLAYCONFIG_MODE_INFO), ctypes.c_void_p]
_user32.QueryDisplayConfig.restype = wintypes.LONG
_user32.DisplayConfigGetDeviceInfo.argtypes = [ctypes.POINTER(DISPLAYCONFIG_DEVICE_INFO_HEADER)]
_user32.DisplayConfigGetDeviceInfo.restype = wintypes.LONG

# The one call that can change the screen, as a module attribute so a test can swap it for a recorder.
_ChangeDisplaySettingsExW = _user32.ChangeDisplaySettingsExW


# ---------------------------------------------------------------- pure helpers --

def check_device(device: str) -> str:
    r"""Return the GDI name ('\\.\DISPLAY1') in upper case, or raise ValueError for anything else."""
    if not isinstance(device, str) or not _DEVICE_RE.fullmatch(device):
        raise ValueError(f"not a display device name: {device!r} (expected like \\\\.\\DISPLAY1)")
    return device.upper()


def check_hz(hz: int) -> int:
    # True/False are ints (1/0) and fall below MIN_HZ, so the range check refuses them too.
    if not isinstance(hz, int) or not MIN_HZ <= hz <= MAX_HZ:
        raise ValueError(f"refresh rate must be a whole number of Hz from {MIN_HZ} to {MAX_HZ}, not {hz!r}")
    return hz


def cds_flags(test_only: bool) -> int:
    """The only two flags this module ever passes: CDS_TEST, or CDS_UPDATEREGISTRY (never CDS_GLOBAL)."""
    if not isinstance(test_only, bool):
        raise TypeError(f"test_only must be True or False, not {test_only!r}")
    return CDS_TEST if test_only else CDS_UPDATEREGISTRY


def rational_hz(numerator: int, denominator: int) -> float | None:
    if denominator == 0 or numerator == 0:
        return None
    return round(numerator / denominator, 3)


def is_internal(output_technology: int) -> bool | None:
    """True for a built-in panel, False for an external output, None when the value says neither."""
    if output_technology in _INTERNAL_TECH:
        return True
    if output_technology in _EXTERNAL_TECH:
        return False
    return None


def rates_at(modes: list[tuple[int, int, int, int, int]], width: int, height: int, bpp: int) -> list[int]:
    """Progressive refresh rates offered at this resolution and colour depth, ascending, without repeats.

    `modes` holds (width, height, bpp, hz, display_flags) per enumerated mode. 0 and 1 Hz mean "hardware
    default" in a DEVMODE, so they are not rates.
    """
    return sorted({hz for w, h, b, hz, flags in modes
                   if (w, h, b) == (width, height, bpp) and hz > 1 and not flags & DM_INTERLACED})


def merge_rates(current_hz: int, offered: list[int], exact_hz: float | None) -> tuple[float | None, list[int]]:
    """(the display's rate, the rates offered) from the current whole-Hz rate, the enumerated rates and QDC's
    exact rate. The rate in use is offered by definition; without an exact rate the whole-Hz one stands in.
    """
    if current_hz > 1 and current_hz not in offered:
        offered = sorted({*offered, current_hz})
    if exact_hz is None and current_hz > 1:
        exact_hz = float(current_hz)
    return exact_hz, offered


# ---------------------------------------------------------------- readers -------

def _devmode() -> DEVMODEW:
    dm = DEVMODEW()
    dm.dmSize = ctypes.sizeof(DEVMODEW)
    return dm


def _current_devmode(device: str) -> DEVMODEW:
    dm = _devmode()
    if not _user32.EnumDisplaySettingsW(device, ENUM_CURRENT_SETTINGS, ctypes.byref(dm)):
        err = ctypes.get_last_error() or ERROR_DEVICE_NOT_CONNECTED
        raise ctypes.WinError(err, f"EnumDisplaySettingsW({device}, current) failed: no such attached display")
    return dm


def _modes(device: str) -> list[tuple[int, int, int, int, int]]:
    dm = _devmode()
    out = []
    for k in range(MAX_MODES):
        dm.dmSize = ctypes.sizeof(DEVMODEW)       # the call rewrites it to 188 (WT 6.1)
        if not _user32.EnumDisplaySettingsW(device, k, ctypes.byref(dm)):
            break
        out.append((dm.dmPelsWidth, dm.dmPelsHeight, dm.dmBitsPerPel, dm.dmDisplayFrequency, dm.dmDisplayFlags))
    return out


def _monitor_name(device: str) -> str:
    mon = DISPLAY_DEVICEW()
    mon.cb = ctypes.sizeof(DISPLAY_DEVICEW)
    if _user32.EnumDisplayDevicesW(device, 0, ctypes.byref(mon), 0):
        return mon.DeviceString
    return ""


def _qdc_paths() -> dict[str, dict]:
    r"""Active paths keyed by source GDI name ('\\.\DISPLAY1'): target adapter LUID, panel type, exact Hz."""
    for _attempt in range(4):        # the topology can change between the two calls
        n_paths, n_modes = ctypes.c_uint32(0), ctypes.c_uint32(0)
        rc = _user32.GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS, ctypes.byref(n_paths), ctypes.byref(n_modes))
        if rc != 0:
            raise ctypes.WinError(rc, f"GetDisplayConfigBufferSizes failed ({rc})")
        paths = (DISPLAYCONFIG_PATH_INFO * max(n_paths.value, 1))()
        modes = (DISPLAYCONFIG_MODE_INFO * max(n_modes.value, 1))()
        rc = _user32.QueryDisplayConfig(QDC_ONLY_ACTIVE_PATHS, ctypes.byref(n_paths), paths, ctypes.byref(n_modes),
                                        modes, None)
        if rc == ERROR_INSUFFICIENT_BUFFER:
            continue
        if rc != 0:
            raise ctypes.WinError(rc, f"QueryDisplayConfig failed ({rc})")
        break
    else:
        raise ctypes.WinError(ERROR_INSUFFICIENT_BUFFER, "QueryDisplayConfig kept changing size")

    out: dict[str, dict] = {}
    for p in paths[:n_paths.value]:
        src = DISPLAYCONFIG_SOURCE_DEVICE_NAME()
        src.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME
        src.header.size = ctypes.sizeof(src)
        src.header.adapterId = p.sourceInfo.adapterId
        src.header.id = p.sourceInfo.id
        if _user32.DisplayConfigGetDeviceInfo(ctypes.byref(src.header)) != 0:
            continue
        name = src.viewGdiDeviceName.upper()
        if name in out:              # a cloned source: keep its first target
            continue
        tgt = DISPLAYCONFIG_TARGET_DEVICE_NAME()
        tgt.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME
        tgt.header.size = ctypes.sizeof(tgt)
        tgt.header.adapterId = p.targetInfo.adapterId
        tgt.header.id = p.targetInfo.id
        named = _user32.DisplayConfigGetDeviceInfo(ctypes.byref(tgt.header)) == 0
        friendly = tgt.monitorFriendlyDeviceName if named else ""
        t = p.targetInfo
        out[name] = {"adapter_luid": luid_str(t.adapterId.HighPart, t.adapterId.LowPart),
                     "internal": is_internal(t.outputTechnology),
                     "hz": rational_hz(t.refreshRate.Numerator, t.refreshRate.Denominator),
                     "friendly": friendly,
                     "device_path": tgt.monitorDevicePath if named else ""}
    return out


# ---------------------------------------------------------------- public ---------

def list_displays() -> list[dict]:
    """Every display attached to the desktop. Raises OSError if QueryDisplayConfig fails."""
    paths = _qdc_paths()
    out: list[dict] = []
    dd = DISPLAY_DEVICEW()
    for i in range(MAX_DEVICES):
        dd.cb = ctypes.sizeof(DISPLAY_DEVICEW)
        if not _user32.EnumDisplayDevicesW(None, i, ctypes.byref(dd), 0):
            break
        if not dd.StateFlags & DISPLAY_DEVICE_ATTACHED_TO_DESKTOP or dd.StateFlags & DISPLAY_DEVICE_MIRRORING_DRIVER:
            continue
        device = dd.DeviceName
        try:
            cur = _current_devmode(device)
        except OSError:
            continue                 # detached between the two calls
        path = paths.get(device.upper(), {})
        hz, rates = merge_rates(cur.dmDisplayFrequency,
                                rates_at(_modes(device), cur.dmPelsWidth, cur.dmPelsHeight, cur.dmBitsPerPel),
                                path.get("hz"))
        out.append({
            "device": device,
            "name": path.get("friendly") or _monitor_name(device),
            "adapter_luid": path.get("adapter_luid"),
            "internal": path.get("internal"),
            "primary": bool(dd.StateFlags & DISPLAY_DEVICE_PRIMARY_DEVICE),
            "width": cur.dmPelsWidth,
            "height": cur.dmPelsHeight,
            "hz": hz,
            "max_hz": max(rates) if rates else None,
            "modes_hz": rates,
        })
    return out


def current_mode(device: str) -> dict:
    """{width, height, hz} of the display now; hz None when the driver reports 'hardware default'."""
    dm = _current_devmode(check_device(device))
    return {"width": dm.dmPelsWidth, "height": dm.dmPelsHeight,
            "hz": dm.dmDisplayFrequency if dm.dmDisplayFrequency > 1 else None}


def monitor_path(device: str) -> str | None:
    r"""Which monitor this output shows now, as its device interface path (DISPLAYCONFIG_TARGET_DEVICE_NAME
    .monitorDevicePath, WT 6.2), e.g. '\\?\DISPLAY#CMN153C#4&25e05db5&0&UID8388688#{e6f07b5f-...}'. It stays
    the same for one monitor on one port, unlike the GDI name, which Windows hands out again when monitors
    change. None when no active path uses this name (unplugged, or switched off). OSError if QDC fails."""
    return _qdc_paths().get(check_device(device), {}).get("device_path") or None


def stored_mode(device: str) -> dict | None:
    """{width, height, hz} that Windows keeps for this display in the user's profile (ENUM_REGISTRY_SETTINGS):
    the mode it uses at the next sign-in. hz None for 'hardware default'; None when nothing is stored (an
    output that is not attached reads as all zeros here). OSError if Windows refuses the query."""
    device = check_device(device)
    dm = _devmode()
    if not _user32.EnumDisplaySettingsW(device, ENUM_REGISTRY_SETTINGS, ctypes.byref(dm)):
        err = ctypes.get_last_error() or ERROR_DEVICE_NOT_CONNECTED
        raise ctypes.WinError(err, f"EnumDisplaySettingsW({device}, registry) failed")
    if not dm.dmPelsWidth or not dm.dmPelsHeight:
        return None
    return {"width": dm.dmPelsWidth, "height": dm.dmPelsHeight,
            "hz": dm.dmDisplayFrequency if dm.dmDisplayFrequency > 1 else None}


def set_refresh(device: str, hz: int, test_only: bool) -> int:
    """Ask Windows for `hz` at the current resolution; returns the DISP_CHANGE_* code (0 = success).

    Built from the current mode, with only width, height and frequency flagged, so position, orientation and
    colour depth are left as they are (WT 6.4). test_only=True passes CDS_TEST, which changes nothing;
    False passes CDS_UPDATEREGISTRY, which changes the mode now and keeps it in the user's profile.
    """
    flags = cds_flags(test_only)
    device = check_device(device)
    hz = check_hz(hz)
    dm = _current_devmode(device)
    dm.dmDisplayFrequency = hz
    dm.dmFields = DM_PELSWIDTH | DM_PELSHEIGHT | DM_DISPLAYFREQUENCY
    return int(_ChangeDisplaySettingsExW(device, ctypes.byref(dm), None, flags, None))
