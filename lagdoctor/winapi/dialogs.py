"""The one dialog lag-doctor's window opens itself: Windows' own Open dialog, for the program a game is added by
(screen 6, "Add a game").

It is modal on the window's thread, where it runs its own message loop as a message box does (the window's posted
calls go on being answered meanwhile); it lists programs only (*.exe), takes only a file that exists, and changes
nothing: the working folder stays as it was, and the pick joins no recent list. It only returns the path the player
chose: nothing is opened or read here.

GetOpenFileNameW, OPENFILENAMEW and the OFN_ flags are commdlg.h's, CommDlgExtendedError's codes cderr.h's (Windows SDK
10.0.26100.0). With OFN_EXPLORER and no hook, Windows shows its newer Open dialog.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

OFN_HIDEREADONLY = 0x00000004
OFN_NOCHANGEDIR = 0x00000008
OFN_PATHMUSTEXIST = 0x00000800
OFN_FILEMUSTEXIST = 0x00001000
OFN_EXPLORER = 0x00080000
OFN_DONTADDTORECENT = 0x02000000
FLAGS = (OFN_EXPLORER | OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST | OFN_HIDEREADONLY | OFN_NOCHANGEDIR
         | OFN_DONTADDTORECENT)
FNERR_BUFFERTOOSMALL = 0x3003
MAX_PATH_CHARS = 1024             # the path's buffer: a game's folder is at most 260 (target.check_folder)
FILTER = "Programs (*.exe)\0*.exe\0"   # pairs of a name and its pattern; the buffer adds the last NUL
TITLE = "Add a game: choose the game's program"


class OPENFILENAMEW(ctypes.Structure):
    _fields_ = [
        ("lStructSize", wintypes.DWORD), ("hwndOwner", wintypes.HWND), ("hInstance", wintypes.HINSTANCE),
        ("lpstrFilter", ctypes.c_void_p), ("lpstrCustomFilter", ctypes.c_void_p), ("nMaxCustFilter", wintypes.DWORD),
        ("nFilterIndex", wintypes.DWORD), ("lpstrFile", ctypes.c_void_p), ("nMaxFile", wintypes.DWORD),
        ("lpstrFileTitle", ctypes.c_void_p), ("nMaxFileTitle", wintypes.DWORD), ("lpstrInitialDir", ctypes.c_void_p),
        ("lpstrTitle", wintypes.LPCWSTR), ("Flags", wintypes.DWORD), ("nFileOffset", wintypes.WORD),
        ("nFileExtension", wintypes.WORD), ("lpstrDefExt", ctypes.c_void_p), ("lCustData", wintypes.LPARAM),
        ("lpfnHook", ctypes.c_void_p), ("lpTemplateName", ctypes.c_void_p), ("pvReserved", ctypes.c_void_p),
        ("dwReserved", wintypes.DWORD), ("FlagsEx", wintypes.DWORD),
    ]


_comdlg32 = ctypes.WinDLL("comdlg32", use_last_error=True)
_GetOpenFileNameW = _comdlg32.GetOpenFileNameW
_GetOpenFileNameW.restype = wintypes.BOOL
_GetOpenFileNameW.argtypes = (ctypes.POINTER(OPENFILENAMEW),)
_CommDlgExtendedError = _comdlg32.CommDlgExtendedError
_CommDlgExtendedError.restype = wintypes.DWORD
_CommDlgExtendedError.argtypes = ()


def _get_open_file_name(ofn) -> int:
    """GetOpenFileNameW, looked up when called (the tests replace this: the real one waits for a person)."""
    return _GetOpenFileNameW(ofn)


def _extended_error() -> int:
    return _CommDlgExtendedError()


def pick_program(hwnd: int | None, title: str = TITLE) -> str | None:
    """The program the player chose in Windows' Open dialog, owned by the window `hwnd`; None when they closed it
    without choosing. OSError, with Windows' code, when the dialog could not be shown or the path did not fit."""
    path = ctypes.create_unicode_buffer(MAX_PATH_CHARS)
    patterns = ctypes.create_unicode_buffer(FILTER)
    ofn = OPENFILENAMEW()
    ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
    ofn.hwndOwner = hwnd or None
    ofn.lpstrFilter = ctypes.addressof(patterns)
    ofn.nFilterIndex = 1
    ofn.lpstrFile = ctypes.addressof(path)
    ofn.nMaxFile = MAX_PATH_CHARS
    ofn.lpstrTitle = title
    ofn.Flags = FLAGS
    if _get_open_file_name(ctypes.byref(ofn)):
        return path.value or None
    code = _extended_error()
    if code == 0:
        return None
    why = "the program's path is too long" if code == FNERR_BUFFERTOOSMALL else "Windows could not show it"
    raise OSError(0, f"the Open dialog failed: {why} (code 0x{code:04X})")
