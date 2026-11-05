"""winapi/windows.py: top-level windows, the foreground PID and monitors.

The filters are proven on a window this test creates in its own process: hidden, shown (without taking
the focus), cloaked by DWM, minimised. No other process's window is touched.
"""
from __future__ import annotations

import ctypes
import os
import uuid
from ctypes import wintypes

import psutil
import pytest

from lagdoctor.winapi import windows

WS_OVERLAPPEDWINDOW = 0x00CF0000
SW_HIDE, SW_SHOWNOACTIVATE, SW_SHOWMINNOACTIVE = 0, 4, 7
DWMWA_CLOAK = 13               # um/dwmapi.h: the [set] attribute just before DWMWA_CLOAKED
W, H = 240, 140

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_dwm = ctypes.WinDLL("dwmapi")
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", ctypes.c_void_p), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


_k32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
_k32.GetModuleHandleW.restype = wintypes.HMODULE
_user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
_user32.RegisterClassW.restype = wintypes.ATOM
_user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
_user32.UnregisterClassW.restype = wintypes.BOOL
_user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                    wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
_user32.CreateWindowExW.restype = wintypes.HWND
_user32.DestroyWindow.argtypes = [wintypes.HWND]
_user32.DestroyWindow.restype = wintypes.BOOL
_user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.ShowWindow.restype = wintypes.BOOL
_user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.DefWindowProcW.restype = ctypes.c_ssize_t
_dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
_dwm.DwmSetWindowAttribute.restype = ctypes.c_long
MONITORENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT),
                                     wintypes.LPARAM)
_user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), MONITORENUMPROC,
                                        wintypes.LPARAM]
_user32.EnumDisplayMonitors.restype = wintypes.BOOL


@pytest.fixture
def own_window():
    """A plain top-level window of this process, created hidden. DefWindowProcW handles its messages."""
    hinst = _k32.GetModuleHandleW(None)
    cls_name = f"LagDoctorTest_{uuid.uuid4().hex[:12]}"
    title = f"lag-doctor test window {uuid.uuid4().hex[:8]}"
    wc = WNDCLASSW(lpfnWndProc=ctypes.cast(_user32.DefWindowProcW, ctypes.c_void_p).value, hInstance=hinst,
                   lpszClassName=cls_name)
    assert _user32.RegisterClassW(ctypes.byref(wc)), ctypes.get_last_error()
    hwnd = _user32.CreateWindowExW(0, cls_name, title, WS_OVERLAPPEDWINDOW, 0, 0, W, H, None, None, hinst, None)
    assert hwnd, ctypes.get_last_error()
    try:
        yield hwnd, title, cls_name
    finally:
        _user32.DestroyWindow(hwnd)
        _user32.UnregisterClassW(cls_name, hinst)


def _mine(hwnd: int) -> list[dict]:
    return [w for w in windows.top_windows() if w["hwnd"] == hwnd]


def _cloak(hwnd: int, on: bool) -> None:
    value = wintypes.BOOL(on)
    assert _dwm.DwmSetWindowAttribute(hwnd, DWMWA_CLOAK, ctypes.byref(value), ctypes.sizeof(value)) == 0


def _monitor_devices() -> set[str]:
    found = set()

    def cb(hmon, _hdc, _rect, _lp):
        mi = windows.MONITORINFOEXW()
        mi.cbSize = ctypes.sizeof(mi)
        if _user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            found.add(mi.szDevice)
        return True

    assert _user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(cb), 0)
    return found


def test_hidden_windows_are_not_listed_and_shown_ones_are(own_window):
    hwnd, title, cls_name = own_window
    assert _mine(hwnd) == [], "a hidden window was listed"
    _user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
    got = _mine(hwnd)
    assert len(got) == 1
    w = got[0]
    assert w["pid"] == os.getpid() and w["title"] == title and w["cls"] == cls_name
    assert w["area"] == W * H and w["minimized"] is False
    assert w["monitor_device"] in _monitor_devices()
    assert windows.window_monitor(hwnd) == w["monitor_device"]


def test_cloaked_windows_are_not_listed(own_window):
    hwnd, _title, _cls = own_window
    _user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
    assert _mine(hwnd)
    _cloak(hwnd, True)
    assert _mine(hwnd) == [], "a cloaked window was listed"
    _cloak(hwnd, False)
    assert _mine(hwnd)


def test_a_minimised_window_is_listed_as_minimised(own_window):
    hwnd, _title, _cls = own_window
    _user32.ShowWindow(hwnd, SW_SHOWMINNOACTIVE)
    got = _mine(hwnd)
    assert len(got) == 1 and got[0]["minimized"] is True
    _user32.ShowWindow(hwnd, SW_HIDE)
    assert _mine(hwnd) == []


def test_every_listed_window_belongs_to_a_running_process():
    running = set(psutil.pids())
    listed = windows.top_windows()
    assert listed, "no visible window at all on an interactive desktop"
    assert all(w["pid"] in running for w in listed)
    assert all(isinstance(w["hwnd"], int) and w["area"] >= 0 for w in listed)


def test_the_foreground_pid_owns_a_listed_window():
    for _ in range(3):                         # the user may switch windows between the two reads
        fg = windows.foreground_pid()
        if fg is None:
            pytest.skip("no window has the foreground right now (locked or switching)")
        if fg in {w["pid"] for w in windows.top_windows()}:
            return
    pytest.fail(f"foreground PID {fg} owns no visible top-level window")


@pytest.mark.parametrize("bad", [0, -1, 1 << 64, True, "1"])
def test_window_monitor_refuses_junk_handles(bad):
    with pytest.raises(ValueError):
        windows.window_monitor(bad)
