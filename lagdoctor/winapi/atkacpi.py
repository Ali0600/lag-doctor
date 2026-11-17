"""ASUS laptops' fan speeds, read through the ASUS System Control Interface driver's device (\\\\.\\ATKACPI).

Read only. The device takes one control code for every call, and the call's first four bytes name the firmware
method: DSTS ("device status") reads, DEVS ("device set") writes, and DEVS on these same fan ids can pin a fan at full
speed or hand it back to the firmware. So this module builds exactly one kind of buffer, DSTS on a fan id from
FAN_IDS, and `_call` refuses any other before it reaches DeviceIoControl (tests/test_fans.py holds both, and that
nothing else in lagdoctor/ names the device or its control code).

The handle is opened with no access rights: enough for this FILE_ANY_ACCESS control code, and it needs no admin
(measured on an ASUS laptop, 2026-11-17: an unelevated process read both fans, about 0.5 ms a call).

Facts and their sources (G-Helper's AsusACPI.cs, Linux's asus-wmi driver, a decompiled ASUS firmware):
docs/research/asus-fans.md.
"""
from __future__ import annotations

import ctypes
import struct
from ctypes import wintypes
from typing import Callable

PATH = "\\\\.\\ATKACPI"
# CTL_CODE(FILE_DEVICE_UNKNOWN 0x22, 0x903, METHOD_BUFFERED, FILE_ANY_ACCESS) (asus-fans.md, section 2)
IOCTL_ATK = 0x0022240C
DSTS = 0x53545344                    # "DSTS" as a little-endian u32: the firmware's device-status (read) method
# The fans' device ids (asus-fans.md, section 3): the same in G-Helper and in Linux's asus-wmi.
FAN_IDS = {"cpu": 0x00110013, "gpu": 0x00110014, "mid": 0x00110031}
PRESENT = 0x0001                     # the high word of a fan's status: the firmware has this fan
# 0xFFFFFFFE (no such method) and 0xFFFFFFFF (no such device) have bit 16 set, so a presence BIT test would take them
# for a fan at 6,553,400 RPM; the whole high word must be PRESENT instead, which refuses them and 0 (no such fan).
RPM_STEP = 100                       # the low word counts hundreds of RPM
BUFFER_BYTES = 16                    # method id, argument length, device id, a second argument (0)
ARGS_BYTES = 8
FILE_SHARE_READ, FILE_SHARE_WRITE = 0x1, 0x2
OPEN_EXISTING = 3
ERROR_INVALID_FUNCTION = 1           # what this driver answered for a fan id the laptop does not have (measured)

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

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

# Seams (tests replace them; tests/conftest.py fails any test that reaches the real _open).
_open: Callable[..., int | None] = _kernel32.CreateFileW
_ioctl: Callable[..., int] = _kernel32.DeviceIoControl
_close: Callable[[int], int] = _kernel32.CloseHandle


class NoSuchFan(Exception):
    """The firmware says this laptop has no such fan."""


def dsts_buffer(fan: str) -> bytes:
    """The 16 bytes of one DSTS call on a fan: method id, argument length 8, the fan's id, 0. KeyError for a name
    that is not in FAN_IDS."""
    return struct.pack("<IIII", DSTS, ARGS_BYTES, FAN_IDS[fan], 0)


def is_fan_read(buf: bytes) -> bool:
    """True only for a buffer dsts_buffer builds: DSTS, argument length 8, a fan id, 0."""
    if not isinstance(buf, (bytes, bytearray)) or len(buf) != BUFFER_BYTES:
        return False
    method, length, device, second = struct.unpack("<IIII", bytes(buf))
    return method == DSTS and length == ARGS_BYTES and device in FAN_IDS.values() and second == 0


def decode(raw: int) -> int:
    """RPM from a fan's 32-bit status. NoSuchFan when the firmware has no such fan (unsupported, or no presence
    bit). 0 is a fan standing still, never a missing reading."""
    raw &= 0xFFFFFFFF
    if raw >> 16 != PRESENT:
        raise NoSuchFan(f"the firmware has no such fan (status 0x{raw:08X})")
    return (raw & 0xFFFF) * RPM_STEP


def _call(handle: int, buf: bytes) -> int:
    """One read call: the status the firmware gave. Refuses anything but a fan read before it reaches the device.
    NoSuchFan when the driver does not know the id; OSError when the call fails otherwise."""
    if not is_fan_read(buf):
        raise ValueError("only a DSTS read of a fan is ever sent to the ASUS device")
    inp = ctypes.create_string_buffer(bytes(buf), BUFFER_BYTES)
    out = ctypes.create_string_buffer(BUFFER_BYTES)
    got = wintypes.DWORD(0)
    if not _ioctl(handle, IOCTL_ATK, inp, BUFFER_BYTES, out, BUFFER_BYTES, ctypes.pointer(got), None):
        err = ctypes.get_last_error()
        if err == ERROR_INVALID_FUNCTION:
            raise NoSuchFan("the ASUS driver does not know this fan")
        raise ctypes.WinError(err)
    if got.value < 4:
        raise OSError(f"the ASUS driver returned {got.value} bytes, too few for a fan's status")
    return struct.unpack_from("<I", out.raw)[0]


class FanReader:
    """An open ASUS device. read(fan) gives a fan's RPM. Close it when done (or use it in a with block)."""

    def __init__(self, handle: int) -> None:
        self._handle: int | None = handle

    @classmethod
    def open(cls) -> "FanReader":
        """OSError (its winerror set) when the device does not open: 2 on a PC without the ASUS driver."""
        handle = _open(PATH, 0, FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, 0, None)
        if handle in (None, INVALID_HANDLE_VALUE):
            raise ctypes.WinError(ctypes.get_last_error())
        return cls(handle)

    def read(self, fan: str) -> int:
        """The fan's speed in RPM (steps of 100). NoSuchFan when the laptop has no such fan; OSError when the call
        failed; ValueError when the reader is closed or the name is not a fan."""
        if self._handle is None:
            raise ValueError("the fan reader is closed")
        if fan not in FAN_IDS:
            raise ValueError(f"not a fan: {fan!r}")
        return decode(_call(self._handle, dsts_buffer(fan)))

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            _close(handle)

    def __enter__(self) -> "FanReader":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
