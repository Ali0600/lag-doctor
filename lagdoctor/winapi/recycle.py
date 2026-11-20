"""The one way lag-doctor's window deletes anything: a recording's folder, moved to Windows' Recycle Bin (Delete on a
game's page), where Windows can put it back.

SHFileOperationW, SHFILEOPSTRUCTW (packed to bytes on 32-bit Windows only) and the FO_ and FOF_ flags are
shellapi.h's (Windows SDK 10.0.26100.0). What Microsoft's pages on them say, and what this module does about it:
- FOF_ALLOWUNDO recycles only a fully qualified path: a name without one is deleted for good, even with the flag. So
  only a drive letter's full path is handed over, with no . or .. part, no wildcard (the call takes * and ?) and no
  \\\\?\\ prefix (the call fails on one), within MAX_PATH; anything else is refused before Windows is called.
- pFrom is a list of names, each ended by a NUL, the list by a second one: one name here.
- No progress, confirmation or error dialog shows (FOF_SILENT, FOF_NOCONFIRMATION, FOF_NOERRORUI), but
  FOF_WANTNUKEWARNING has Windows ask before it would delete the folder for good instead of recycling it (a folder too
  large for the Recycle Bin, a drive with none): shellapi.h's own comments say FOF_SILENT and FOF_NOERRORUI still let
  confirmations show, and that this warning partially overrides FOF_NOCONFIRMATION. The warning is owned by `hwnd`.
- The call returns its own codes, not Win32 errors (so never GetLastError), and it can return 0 for an operation that
  was cancelled: fAnyOperationsAborted is read too.
The caller checks the folder is gone afterwards (App.delete), and the tests never reach the real call
(tests/conftest.py's _no_real_recycle_bin; tests/test_recycle_live.py runs it on GitHub's runner only).
"""
from __future__ import annotations

import ctypes
import re
from ctypes import wintypes

FO_DELETE = 0x0003
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOERRORUI = 0x0400
FOF_WANTNUKEWARNING = 0x4000
FLAGS = FOF_ALLOWUNDO | FOF_WANTNUKEWARNING | FOF_SILENT | FOF_NOCONFIRMATION | FOF_NOERRORUI
DE_OPCANCELLED = 0x75                 # the page's code for an operation the user, or the flags, cancelled
MAX_PATH_CHARS = 259                  # MAX_PATH less its NUL
_FULL = re.compile(r"[A-Za-z]:\\[^\\/:*?\"<>|]+(\\[^\\/:*?\"<>|]+)*")


class SHFILEOPSTRUCTW(ctypes.Structure):
    if ctypes.sizeof(ctypes.c_void_p) == 4:          # shellapi.h: pshpack1.h unless _WIN64
        _pack_ = 1
    _fields_ = [
        ("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", ctypes.c_void_p), ("pTo", ctypes.c_void_p),
        ("fFlags", wintypes.WORD), ("fAnyOperationsAborted", wintypes.BOOL), ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


_shell32 = ctypes.WinDLL("shell32")
_SHFileOperationW = _shell32.SHFileOperationW
_SHFileOperationW.restype = ctypes.c_int
_SHFileOperationW.argtypes = (ctypes.POINTER(SHFILEOPSTRUCTW),)


def _file_operation(op) -> int:
    """SHFileOperationW, looked up when called (the tests replace this: the real one moves the folder into this PC's
    own Recycle Bin)."""
    return _SHFileOperationW(op)


def refused(path: object) -> str | None:
    """Why `path` is not handed to the Recycle Bin, in plain words; None for a drive letter's full path within
    MAX_PATH, below the drive's root, with no . or .. part, no wildcard and no character Windows refuses in a name."""
    if not isinstance(path, str) or not path or any(ord(c) < 32 for c in path):
        return "it is not a folder's path"
    if len(path) > MAX_PATH_CHARS:
        return f"its path is longer than {MAX_PATH_CHARS} characters"
    if not _FULL.fullmatch(path) or any(part != part.rstrip(" .") for part in path[3:].split("\\")):   # . and .. too
        return f"{path} is not a full path on a drive"
    return None


def names(path: str) -> str:
    """pFrom for one name: the name and the NUL that ends it; create_unicode_buffer adds the second, which ends the
    list."""
    return path + "\0"


def to_recycle_bin(path: str, hwnd: int | None) -> None:
    """Move the file or folder at `path` (a full path: refused) to the Recycle Bin, any question Windows asks owned by
    the window `hwnd`. ValueError for a path refused here; OSError, with Windows' code, when it was not moved."""
    why = refused(path)
    if why is not None:
        raise ValueError(why)
    listed = ctypes.create_unicode_buffer(names(path))
    op = SHFILEOPSTRUCTW()
    op.hwnd = hwnd or None
    op.wFunc = FO_DELETE
    op.pFrom = ctypes.addressof(listed)
    op.fFlags = FLAGS
    code = _file_operation(ctypes.byref(op))
    if op.fAnyOperationsAborted or code == DE_OPCANCELLED:
        raise OSError(0, "moving it to the Recycle Bin was cancelled")
    if code != 0:
        raise OSError(0, f"Windows could not move it to the Recycle Bin (code 0x{code:X})")
