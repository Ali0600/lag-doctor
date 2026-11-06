"""The drive a file lives on: SSD or HDD (the seek-penalty property, read with no access rights, so no admin),
its free space, and where a new file made in a folder really lands (new_file_path).

Recipe and constants: docs/research/windows-telemetry.md (WT) section 9.5, re-read in winioctl.h 10.0.19041;
the probe's flags are from fileapi.h and winnt.h (10.0.19041).
"""
from __future__ import annotations

import ctypes
import ntpath
import os
import re
from ctypes import wintypes
from typing import Any

IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400       # CTL_CODE(IOCTL_STORAGE_BASE 0x2D, 0x500, METHOD_BUFFERED, ANY)
STORAGE_DEVICE_SEEK_PENALTY_PROPERTY = 7        # STORAGE_PROPERTY_ID (WT 9.5, verify log #97)
PROPERTY_STANDARD_QUERY = 0
FILE_SHARE_READ, FILE_SHARE_WRITE = 0x1, 0x2
OPEN_EXISTING = 3
CREATE_NEW = 1
GENERIC_READ, GENERIC_WRITE, DELETE = 0x80000000, 0x40000000, 0x00010000
FILE_ATTRIBUTE_HIDDEN, FILE_ATTRIBUTE_TEMPORARY = 0x2, 0x100
FILE_FLAG_DELETE_ON_CLOSE = 0x04000000
FILE_NAME_NORMALIZED = 0x0
VOLUME_NAME_DOS, VOLUME_NAME_NT, VOLUME_NAME_NONE = 0x0, 0x2, 0x4      # winbase.h 10.0.19041
ERROR_INSUFFICIENT_BUFFER, APPMODEL_ERROR_NO_PACKAGE = 122, 15700     # winerror.h
PROBE_PREFIX = ".lagdoctor-probe-"
DRIVE_REMOVABLE, DRIVE_FIXED = 2, 3              # GetDriveTypeW; network, optical and RAM disks are not judged
MAX_PATH_CHARS = 32_767

_DRIVE_ROOT_RE = re.compile(r"[A-Za-z]:\\")
_VOLUME_GUID_RE = re.compile(r"\\\\\?\\Volume\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
                             r"[0-9A-Fa-f]{12}\}\\")


class STORAGE_PROPERTY_QUERY(ctypes.Structure):
    _fields_ = [("PropertyId", ctypes.c_int), ("QueryType", ctypes.c_int),
                ("AdditionalParameters", ctypes.c_ubyte * 1)]


class DEVICE_SEEK_PENALTY_DESCRIPTOR(ctypes.Structure):
    _fields_ = [("Version", wintypes.DWORD), ("Size", wintypes.DWORD), ("IncursSeekPenalty", ctypes.c_ubyte)]


assert ctypes.sizeof(STORAGE_PROPERTY_QUERY) == 12
assert ctypes.sizeof(DEVICE_SEEK_PENALTY_DESCRIPTOR) == 12     # Version = Size = 12 came back here (WT 9.5)
_SEEK_FIELD_END = DEVICE_SEEK_PENALTY_DESCRIPTOR.IncursSeekPenalty.offset + 1

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
_kernel32.CreateFileW.restype = wintypes.HANDLE
_kernel32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                      wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                      wintypes.LPVOID]
_kernel32.DeviceIoControl.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.GetVolumePathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
_kernel32.GetVolumePathNameW.restype = wintypes.BOOL
_kernel32.GetVolumeNameForVolumeMountPointW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
_kernel32.GetVolumeNameForVolumeMountPointW.restype = wintypes.BOOL
_kernel32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
_kernel32.GetDriveTypeW.restype = wintypes.UINT
_kernel32.GetDiskFreeSpaceExW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_ulonglong),
                                          ctypes.POINTER(ctypes.c_ulonglong), ctypes.POINTER(ctypes.c_ulonglong)]
_kernel32.GetDiskFreeSpaceExW.restype = wintypes.BOOL
_kernel32.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
_kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
_kernel32.GetCurrentPackageFamilyName.argtypes = [ctypes.POINTER(ctypes.c_uint32), wintypes.LPWSTR]
_kernel32.GetCurrentPackageFamilyName.restype = wintypes.LONG

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


# ---------------------------------------------------------------- pure helpers --

def check_path(path: str) -> str:
    """An absolute path (a relative one would silently answer for the current drive)."""
    if not isinstance(path, str) or not path or "\0" in path or len(path) > MAX_PATH_CHARS \
            or not ntpath.isabs(path):
        raise ValueError(f"not an absolute path: {path!r}")
    return path


def judged_drive_type(drive_type: int) -> bool:
    """Only local fixed and removable disks are asked; a network, optical or RAM drive stays Unknown."""
    return drive_type in (DRIVE_REMOVABLE, DRIVE_FIXED)


def media_from_seek_penalty(incurs_seek_penalty: bool | None) -> str:
    """No seek penalty means solid state (WT 9.5); unknown stays Unknown."""
    if incurs_seek_penalty is None:
        return "Unknown"
    return "HDD" if incurs_seek_penalty else "SSD"


def volume_device(volume_root: str, volume_guid_path: str | None) -> str | None:
    r"""The device to open for a volume: '\\.\C:' for a drive root, '\\.\Volume{…}' for a folder mount point.

    `volume_guid_path` is GetVolumeNameForVolumeMountPointW's answer ('\\?\Volume{…}\'), used when the root
    is not a drive letter. The trailing backslash must go: with it, the path names the root directory, not the
    volume device.
    """
    if _DRIVE_ROOT_RE.fullmatch(volume_root):
        return "\\\\.\\" + volume_root[:2].upper()
    if volume_guid_path and _VOLUME_GUID_RE.fullmatch(volume_guid_path):
        return "\\\\.\\" + volume_guid_path[4:-1]
    return None


# ---------------------------------------------------------------- readers -------

def _volume_root(path: str) -> str | None:
    buf = ctypes.create_unicode_buffer(1024)
    if not _kernel32.GetVolumePathNameW(path, buf, len(buf)):
        return None
    return buf.value


def _device_for(path: str) -> str | None:
    root = _volume_root(check_path(path))
    if not root or not judged_drive_type(_kernel32.GetDriveTypeW(root)):
        return None
    guid_path = None
    if not _DRIVE_ROOT_RE.fullmatch(root):
        buf = ctypes.create_unicode_buffer(64)
        if _kernel32.GetVolumeNameForVolumeMountPointW(root, buf, len(buf)):
            guid_path = buf.value
    return volume_device(root, guid_path)


def _seek_penalty(device: str) -> bool | None:
    # Access 0: enough for IOCTL_STORAGE_QUERY_PROPERTY, and needs no admin (WT 9.5).
    handle = _kernel32.CreateFileW(device, 0, FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, 0, None)
    if handle in (None, INVALID_HANDLE_VALUE):
        return None
    try:
        query = STORAGE_PROPERTY_QUERY(STORAGE_DEVICE_SEEK_PENALTY_PROPERTY, PROPERTY_STANDARD_QUERY)
        desc = DEVICE_SEEK_PENALTY_DESCRIPTOR()
        got = wintypes.DWORD(0)
        ok = _kernel32.DeviceIoControl(handle, IOCTL_STORAGE_QUERY_PROPERTY, ctypes.byref(query),
                                       ctypes.sizeof(query), ctypes.byref(desc), ctypes.sizeof(desc),
                                       ctypes.byref(got), None)
        if not ok or got.value < _SEEK_FIELD_END or desc.Size < _SEEK_FIELD_END:
            return None
        return bool(desc.IncursSeekPenalty)
    finally:
        _kernel32.CloseHandle(handle)


# ---------------------------------------------------------------- public ---------

def media_type(path: str) -> str:
    """'SSD', 'HDD' or 'Unknown' (network drives, spanned volumes, anything the disk does not report)."""
    device = _device_for(path)
    return media_from_seek_penalty(_seek_penalty(device) if device else None)


def free_gb(path: str) -> float | None:
    """Space free to this user on the volume holding `path`, in GiB (as Explorer shows it); None if unknown."""
    root = _volume_root(check_path(path))
    if not root:
        return None
    free = ctypes.c_ulonglong(0)
    if not _kernel32.GetDiskFreeSpaceExW(root, ctypes.byref(free), None, None):
        return None
    return free.value / 2**30


def new_file_path(folder: str) -> str:
    r"""Where Windows really puts a new file made in `folder`: the final path of a probe file, "\\?\C:\...".

    It can lie outside `folder`: Windows may keep the AppData files of a program that a packaged app started
    in that app's private folder (paths.private_copy). The probe is hidden, marked temporary and deleted by
    Windows when its handle closes, even if this process dies first, so nothing is left behind. Raises OSError
    when the probe cannot be made or its path read.
    """
    name = ntpath.join(check_path(folder), f"{PROBE_PREFIX}{os.getpid()}-{os.urandom(4).hex()}")
    handle = _kernel32.CreateFileW(name, GENERIC_READ | GENERIC_WRITE | DELETE, 0, None, CREATE_NEW,
                                   FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_TEMPORARY | FILE_FLAG_DELETE_ON_CLOSE,
                                   None)
    if handle in (None, INVALID_HANDLE_VALUE):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        # A RAM disk with no Mount Manager entry (ImDisk, OSFMount) fails the drive-letter form with
        # ERROR_INVALID_FUNCTION; its NT or volume-less form still shows a private copy's \Packages\<app>\LocalCache\.
        first: OSError | None = None
        for volume in (VOLUME_NAME_DOS, VOLUME_NAME_NT, VOLUME_NAME_NONE):
            try:
                return _final_path_of(handle, FILE_NAME_NORMALIZED | volume)
            except OSError as exc:
                first = first or exc
        raise first or OSError(f"the path of a new file in {folder} could not be read")
    finally:
        _kernel32.CloseHandle(handle)


def _final_path_of(handle: Any, flags: int) -> str:
    """GetFinalPathNameByHandleW in the form `flags` asks for; OSError when Windows will not give it."""
    size = 512
    while size <= MAX_PATH_CHARS + 1:
        buf = ctypes.create_unicode_buffer(size)
        n = _kernel32.GetFinalPathNameByHandleW(handle, buf, size, flags)
        if n == 0:
            raise ctypes.WinError(ctypes.get_last_error())
        if n < size:              # done: n is the length without the terminating null
            return buf.value
        size = n + 1              # too small: n is the size needed (with the null)
    raise OSError(f"a path longer than {MAX_PATH_CHARS} characters")


def own_package_family() -> str | None:
    """This process's own package family name (e.g. a Microsoft Store Python's
    "PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0"), or None for a process with no package identity. A shell
    the Claude desktop app starts has none, though Windows moves its AppData files (paths.private_copy)."""
    n = ctypes.c_uint32(0)
    rc = _kernel32.GetCurrentPackageFamilyName(ctypes.byref(n), None)
    if rc == APPMODEL_ERROR_NO_PACKAGE:
        return None
    if rc != ERROR_INSUFFICIENT_BUFFER or not 0 < n.value <= 1024:
        raise OSError(f"GetCurrentPackageFamilyName failed ({rc})")
    buf = ctypes.create_unicode_buffer(n.value)
    rc = _kernel32.GetCurrentPackageFamilyName(ctypes.byref(n), buf)
    if rc != 0:
        raise OSError(f"GetCurrentPackageFamilyName failed ({rc})")
    return buf.value
