"""The signed-in user's own folders from SHGetKnownFolderPath (shell32): where Windows keeps Documents and the local
AppData, wherever they were moved (OneDrive moves Documents into its own folder, so %USERPROFILE%\\Documents can be
the wrong place). No admin rights.

FOLDERID values are from the SDK's um\\KnownFolders.h. The caller frees the returned string with CoTaskMemFree
whether the call succeeds or not (the function's documentation).
"""
from __future__ import annotations

import ctypes
import uuid
from ctypes import wintypes

FOLDERID_DOCUMENTS = "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}"
FOLDERID_LOCAL_APP_DATA = "{F1B32785-6FBA-4FCF-9D55-7B8E7F157091}"
KF_FLAG_DEFAULT = 0


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


if ctypes.sizeof(GUID) != 16:
    raise RuntimeError(f"GUID is {ctypes.sizeof(GUID)} bytes, expected 16; the ctypes layout no longer matches")

_shell32 = ctypes.WinDLL("shell32", use_last_error=True)
_shell32.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE,
                                          ctypes.POINTER(ctypes.c_void_p)]
_shell32.SHGetKnownFolderPath.restype = ctypes.c_long          # HRESULT
_ole32 = ctypes.WinDLL("ole32", use_last_error=True)
_ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
_ole32.CoTaskMemFree.restype = None


def _guid(text: str) -> GUID:
    return GUID.from_buffer_copy(uuid.UUID(text).bytes_le)


def known_folder(folder_id: str) -> str:
    """The folder's path for this process's user. OSError (winerror = the HRESULT) when Windows cannot say."""
    guid = _guid(folder_id)
    out = ctypes.c_void_p()
    hr = _shell32.SHGetKnownFolderPath(ctypes.byref(guid), KF_FLAG_DEFAULT, None, ctypes.byref(out))
    try:
        path = ctypes.wstring_at(out.value) if hr == 0 and out.value else None
    finally:
        if out.value:
            _ole32.CoTaskMemFree(out)
    if hr != 0 or not path:
        err = OSError(0, f"SHGetKnownFolderPath({folder_id}) failed with HRESULT 0x{hr & 0xFFFFFFFF:08X}")
        err.winerror = hr & 0xFFFFFFFF
        raise err
    return path


def documents() -> str:
    return known_folder(FOLDERID_DOCUMENTS)


def local_app_data() -> str:
    return known_folder(FOLDERID_LOCAL_APP_DATA)
