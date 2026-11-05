"""Top-level windows, the foreground window's process, and which monitor a window is on.

Only window calls (user32, dwmapi): no process handle is opened. GetWindowTextW on another process's
window reads the caption Windows stores for it without sending that process a message, so a hung game
cannot block us. Constants and struct sizes are from the Windows SDK 10.0.19041 headers: DWMWA_CLOAKED = 14
(um/dwmapi.h, DWMWINDOWATTRIBUTE counted from DWMWA_NCRENDERING_ENABLED = 1), MONITOR_DEFAULTTONEAREST = 2
and MONITORINFOEXW = MONITORINFO (40 bytes) + WCHAR[CCHDEVICENAME = 32] (um/winuser.h lines 13499-13573).
"""
from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes

DWMWA_CLOAKED = 14
MONITOR_DEFAULTTONEAREST = 0x00000002
CCHDEVICENAME = 32
_TITLE_CHARS = 512
_CLASS_CHARS = 256
_MAX_WINDOWS = 16384          # EnumWindows stops here: a runaway window count must not grow our list forever


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD), ("szDevice", wintypes.WCHAR * CCHDEVICENAME)]


assert ctypes.sizeof(wintypes.RECT) == 16
assert ctypes.sizeof(MONITORINFOEXW) == 104

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_dwm = ctypes.WinDLL("dwmapi")

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
_user32.EnumWindows.restype = wintypes.BOOL
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.IsWindowVisible.restype = wintypes.BOOL
_user32.IsIconic.argtypes = [wintypes.HWND]
_user32.IsIconic.restype = wintypes.BOOL
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindowTextW.restype = ctypes.c_int
_user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetClassNameW.restype = ctypes.c_int
_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.GetWindowRect.restype = wintypes.BOOL
_user32.GetForegroundWindow.argtypes = []
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
_user32.MonitorFromWindow.restype = wintypes.HMONITOR
_user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFOEXW)]
_user32.GetMonitorInfoW.restype = wintypes.BOOL
_dwm.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
_dwm.DwmGetWindowAttribute.restype = ctypes.c_long           # HRESULT

_lock = threading.Lock()      # the text buffers below are shared
_title = ctypes.create_unicode_buffer(_TITLE_CHARS)
_cls = ctypes.create_unicode_buffer(_CLASS_CHARS)


def _check_hwnd(hwnd: int) -> int:
    if isinstance(hwnd, bool) or not isinstance(hwnd, int) or not 0 < hwnd < (1 << 64):
        raise ValueError(f"not a window handle: {hwnd!r}")
    return hwnd


def _window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD(0)
    if not _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)):
        return 0
    return pid.value


def _cloaked(hwnd: int) -> bool | None:
    """True if DWM hides the window (another virtual desktop, a suspended UWP app); None if DWM cannot say."""
    value = wintypes.DWORD(0)
    hr = _dwm.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(value), ctypes.sizeof(value))
    if hr < 0:
        return None
    return value.value != 0


def top_windows() -> list[dict]:
    """Visible, uncloaked top-level windows: {hwnd, pid, title, area, minimized, monitor_device, cls}.

    A window DWM cannot report on is left out (it cannot be shown to count as shown); so is one whose
    owner cannot be read (it closed while we looked). `cls` is the window class name.
    """
    hwnds: list[int] = []

    def collect(hwnd, _lparam):
        if hwnd:
            hwnds.append(hwnd)
        return len(hwnds) < _MAX_WINDOWS

    cb = WNDENUMPROC(collect)
    if not _user32.EnumWindows(cb, 0) and len(hwnds) < _MAX_WINDOWS:
        err = ctypes.get_last_error()
        if err:
            raise ctypes.WinError(err)
    out = []
    rect = wintypes.RECT()
    for hwnd in hwnds:
        if not _user32.IsWindowVisible(hwnd) or _cloaked(hwnd) is not False:
            continue
        pid = _window_pid(hwnd)
        if not pid:
            continue
        with _lock:
            n = _user32.GetWindowTextW(hwnd, _title, _TITLE_CHARS)
            title = _title.value if n > 0 else ""
            n = _user32.GetClassNameW(hwnd, _cls, _CLASS_CHARS)
            cls = _cls.value if n > 0 else ""
        area = 0
        if _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            area = max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
        try:
            monitor = window_monitor(hwnd)
        except OSError:
            monitor = None
        out.append({"hwnd": hwnd, "pid": pid, "title": title, "area": area,
                    "minimized": bool(_user32.IsIconic(hwnd)), "monitor_device": monitor, "cls": cls})
    return out


def foreground_pid() -> int | None:
    """The PID owning the foreground window, or None when no window has the foreground (e.g. while locked)."""
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return None
    return _window_pid(hwnd) or None


def window_monitor(hwnd: int) -> str | None:
    """The monitor a window is on (the nearest one), e.g. "\\\\.\\DISPLAY1"; None for a window that is gone."""
    _check_hwnd(hwnd)
    hmon = _user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    if not hmon:
        return None
    mi = MONITORINFOEXW()
    mi.cbSize = ctypes.sizeof(MONITORINFOEXW)
    if not _user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
        raise ctypes.WinError(ctypes.get_last_error())
    return mi.szDevice or None
