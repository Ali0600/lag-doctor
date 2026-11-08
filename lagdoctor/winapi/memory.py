"""Physical memory from GlobalMemoryStatusEx (kernel32): no admin rights, no process handles.

MEMORYSTATUSEX is 64 bytes (SDK um\\sysinfoapi.h:66-76). The call fails with ERROR_INVALID_PARAMETER (87) unless
dwLength is set first (seen here). Its total agrees to the byte with GetPerformanceInfo's PhysicalTotal times
PageSize, the readout psutil's virtual_memory() uses (measured on this laptop, 2026-11-07).
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD)] + [
        (name, ctypes.c_ulonglong) for name in ("ullTotalPhys", "ullAvailPhys", "ullTotalPageFile",
                                                "ullAvailPageFile", "ullTotalVirtual", "ullAvailVirtual",
                                                "ullAvailExtendedVirtual")]


def _check_layout() -> None:
    size, total = ctypes.sizeof(MEMORYSTATUSEX), MEMORYSTATUSEX.ullTotalPhys.offset
    if (size, total) != (64, 8):
        raise RuntimeError(f"MEMORYSTATUSEX is {size} bytes with ullTotalPhys at {total}, expected 64 and 8 "
                           "(SDK um\\sysinfoapi.h); the ctypes layout no longer matches Windows")


_check_layout()

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MEMORYSTATUSEX)]
_k32.GlobalMemoryStatusEx.restype = wintypes.BOOL


def total_physical_bytes() -> int:
    """The physical memory Windows can use, in bytes (ullTotalPhys). OSError when Windows cannot say."""
    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(status)
    if not _k32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise ctypes.WinError(ctypes.get_last_error())
    if status.ullTotalPhys == 0:            # a success that filled nothing in: no PC has no memory
        err = OSError(0, "GlobalMemoryStatusEx reported success with no physical memory")
        err.winerror = 13                   # ERROR_INVALID_DATA
        raise err
    return int(status.ullTotalPhys)
