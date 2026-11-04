"""A global hotkey ("it lagged just now") that works while a fullscreen game has focus.

RegisterHotKey delivers WM_HOTKEY to the message queue of the thread that
registered it, so the listener runs its own thread with a GetMessage loop.
Stopping posts WM_QUIT to that thread, which unregisters the key on its way out.
"""
from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes
from typing import Callable

from lagdoctor import clock

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY, WM_QUIT, PM_NOREMOVE = 0x0312, 0x0012, 0x0000
ERROR_HOTKEY_ALREADY_REGISTERED = 1409

_MODS = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT, "win": MOD_WIN}
_NAMED_KEYS = {"scrolllock": 0x91, "pause": 0x13, "insert": 0x2D, "home": 0x24, "end": 0x23,
               "pageup": 0x21, "pagedown": 0x22, "space": 0x20, "tab": 0x09}

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
_user32.RegisterHotKey.restype = wintypes.BOOL
_user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.UnregisterHotKey.restype = wintypes.BOOL
_user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
_user32.GetMessageW.restype = wintypes.BOOL
_user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
                                 wintypes.UINT]
_user32.PeekMessageW.restype = wintypes.BOOL
_user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.PostThreadMessageW.restype = wintypes.BOOL
_kernel32.GetCurrentThreadId.restype = wintypes.DWORD


def parse_hotkey(text: str) -> tuple[int, int]:
    """'ctrl+alt+l' -> (MOD_CONTROL | MOD_ALT, VK for L). Raises ValueError on anything unclear."""
    parts = [p.strip().lower() for p in text.split("+") if p.strip()]
    if not parts:
        raise ValueError("empty hotkey")
    *mods, key = parts
    flags = 0
    for m in mods:
        if m not in _MODS:
            raise ValueError(f"unknown modifier {m!r} in {text!r} (use ctrl, alt, shift, win)")
        flags |= _MODS[m]
    if len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    elif key in _NAMED_KEYS:
        vk = _NAMED_KEYS[key]
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x70 + int(key[1:]) - 1
    else:
        raise ValueError(f"unknown key {key!r} in {text!r}")
    return flags, vk


class HotkeyListener:
    """Calls on_press(clock.now()) each time the hotkey is pressed, from its own thread."""

    _ID = 0x4C44  # "LD"

    def __init__(self, hotkey: str, on_press: Callable[[float], None]) -> None:
        self.hotkey = hotkey
        self._mods, self._vk = parse_hotkey(hotkey)
        self._on_press = on_press
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self.registered = False
        self.error: str | None = None

    def start(self, timeout: float = 5.0) -> bool:
        """Register the hotkey. False (with .error set) if another program already owns it."""
        self._thread = threading.Thread(target=self._loop, name="lagdoctor-hotkey", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            self.error = "the hotkey thread did not start"
        return self.registered

    def stop(self, timeout: float = 5.0) -> None:
        if self._thread is None:
            return
        if self._thread_id:
            _user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        self._thread.join(timeout)
        self._thread = None

    def _loop(self) -> None:
        msg = wintypes.MSG()
        _user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_NOREMOVE)  # create this thread's queue
        self._thread_id = _kernel32.GetCurrentThreadId()
        if not _user32.RegisterHotKey(None, self._ID, self._mods | MOD_NOREPEAT, self._vk):
            err = ctypes.get_last_error()
            self.error = (f"{self.hotkey} is already used by another program" if err == ERROR_HOTKEY_ALREADY_REGISTERED
                          else f"RegisterHotKey failed (error {err})")
            self._ready.set()
            return
        self.registered = True
        self._ready.set()
        try:
            while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY and msg.wParam == self._ID:
                    try:
                        self._on_press(clock.now())
                    except Exception:  # noqa: BLE001 - a bad callback must not kill the listener
                        pass
        finally:
            # Windows also frees a thread's hotkeys when the thread exits (seen in testing: a
            # sabotage that removed this call left the key free after stop()), so this is the
            # documented pairing rather than the only release.
            _user32.UnregisterHotKey(None, self._ID)
            self.registered = False

    # Test hook: deliver a press the way Windows would, without synthesising real key input.
    def _simulate_press(self) -> bool:
        return bool(self._thread_id and _user32.PostThreadMessageW(self._thread_id, WM_HOTKEY, self._ID, 0))
