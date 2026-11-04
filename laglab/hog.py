"""Background CPU hog for LagLab's "hog" mode. Standard library only.

Burns one CPU core in bursts: busy for --on seconds, idle for --off seconds,
repeating from --t0 (a time.perf_counter value from the parent; QPC is the same
clock in every process, so all hogs burst together). Exits when --duration has
passed or the parent process is gone, whichever comes first.
"""
from __future__ import annotations

import argparse
import ctypes
import sys
import time
from ctypes import wintypes

SYNCHRONIZE = 0x00100000
WAIT_TIMEOUT = 0x102

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.WaitForSingleObject.restype = wintypes.DWORD
_kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--t0", type=float, required=True)
    ap.add_argument("--on", type=float, required=True)
    ap.add_argument("--off", type=float, required=True)
    ap.add_argument("--duration", type=float, required=True)
    ap.add_argument("--parent", type=int, required=True)
    args = ap.parse_args(argv)

    parent = _kernel32.OpenProcess(SYNCHRONIZE, False, args.parent)
    end = args.t0 + args.duration
    period = args.on + args.off
    x = 0
    next_parent_check = 0.0
    while (now := time.perf_counter()) < end:
        if now >= next_parent_check:
            next_parent_check = now + 0.25
            if not parent or _kernel32.WaitForSingleObject(parent, 0) != WAIT_TIMEOUT:
                return 0  # parent gone (or never opened): stop burning
        phase = (now - args.t0) % period
        if phase < args.on:
            stop = min(now + 0.02, args.t0 + (now - args.t0) // period * period + args.on)
            while time.perf_counter() < stop:
                x += 1
        else:
            time.sleep(min(0.02, period - phase))
    return 0


if __name__ == "__main__":
    sys.exit(main())
