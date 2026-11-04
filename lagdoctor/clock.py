"""The capture clock: time.perf_counter() seconds, which is QueryPerformanceCounter on Windows.

PresentMon's --qpc_time column, LagLab's logs and every sampler use this one
clock, so frames and system samples line up without any guesswork.
"""
from __future__ import annotations

import ctypes
import time
from functools import lru_cache


def now() -> float:
    """Seconds on the shared clock (an arbitrary origin, the same in every process)."""
    return time.perf_counter()


@lru_cache(maxsize=1)
def qpc_frequency() -> int:
    freq = ctypes.c_longlong()
    if not ctypes.windll.kernel32.QueryPerformanceFrequency(ctypes.byref(freq)) or freq.value <= 0:
        raise OSError("QueryPerformanceFrequency failed")
    return freq.value


def qpc_now() -> int:
    ticks = ctypes.c_longlong()
    ctypes.windll.kernel32.QueryPerformanceCounter(ctypes.byref(ticks))
    return ticks.value


def qpc_to_seconds(ticks: int | float) -> float:
    """Convert a raw QueryPerformanceCounter value (e.g. PresentMon's --qpc_time) to now()'s scale."""
    return float(ticks) / qpc_frequency()
