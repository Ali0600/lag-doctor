"""Small, pure statistics helpers. Standard library only.

Frame-rate conventions (they differ between tools, so both are offered):
- avg_fps is frames divided by elapsed time. Averaging 1000/frame_ms per frame
  would overweight fast frames and overstate the frame rate.
- "1% low" has two common definitions: the average of the slowest 1% of frames
  (low_fps_avg, FrameView's "1% Low FPS", taking exactly ceil(p * n) frames as
  LC 2.4 and 2.5 require) and the 99th-percentile frame time turned into FPS
  (low_fps_percentile). CapFrameX's "1% low average" is neither: it averages
  every frame >= the quantile, which sweeps in ties (LC 2.5).
"""
from __future__ import annotations

import bisect
import heapq
import math
from typing import Iterable, Sequence


def clean(values: Iterable[float | None]) -> list[float]:
    """Drop None and NaN."""
    return [float(v) for v in values if v is not None and not (isinstance(v, float) and math.isnan(v))]


def mean(values: Sequence[float]) -> float:
    if not values:
        raise ValueError("mean of no values")
    return math.fsum(values) / len(values)


def percentile(values: Sequence[float], p: float) -> float:
    """Linear interpolation between closest ranks (numpy's default method)."""
    if not values:
        raise ValueError("percentile of no values")
    if not 0.0 <= p <= 100.0:
        raise ValueError(f"p must be within 0..100, got {p}")
    s = sorted(values)
    pos = (len(s) - 1) * p / 100.0
    lo = math.floor(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def median(values: Sequence[float]) -> float:
    return percentile(values, 50.0)


def mad(values: Sequence[float]) -> float:
    """Median absolute deviation: a spread measure that one huge stutter cannot inflate."""
    m = median(values)
    return median([abs(v - m) for v in values])


def avg_fps(frame_ms: Sequence[float]) -> float:
    total = math.fsum(frame_ms)
    if total <= 0:
        raise ValueError("frame times must add up to more than zero")
    return 1000.0 * len(frame_ms) / total


def low_fps_avg(frame_ms: Sequence[float], pct: float) -> float:
    """FPS of the mean of exactly ceil(pct% of n) slowest frames (LC 2.4, 2.5), so at least one frame."""
    if not frame_ms:
        raise ValueError("no frames")
    if not 0.0 < pct <= 100.0:
        raise ValueError(f"pct must be within (0, 100], got {pct}")
    k = math.ceil(len(frame_ms) * pct / 100.0)
    return 1000.0 / mean(heapq.nlargest(k, frame_ms))


def low_fps_percentile(frame_ms: Sequence[float], pct: float) -> float:
    """FPS at the (100 - pct)th percentile frame time."""
    return 1000.0 / percentile(frame_ms, 100.0 - pct)


def trailing_median(values: Sequence[float | None], window: int) -> list[float | None]:
    """Median of the `window` values before each position (excluding it); None until one exists.

    Excluding the current value matters: a stutter must be compared with the frames
    before it, not with a baseline it has already dragged upward. A None value is
    missing: it keeps its place in the window but never enters the median.
    """
    if window < 1:
        raise ValueError("window must be at least 1")
    out: list[float | None] = []
    ordered: list[float] = []
    for i, v in enumerate(values):
        if ordered:
            n = len(ordered)
            mid = n // 2
            out.append(ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0)
        else:
            out.append(None)
        if v is not None:
            bisect.insort(ordered, v)
        if i >= window:
            old = values[i - window]
            if old is not None:
                del ordered[bisect.bisect_left(ordered, old)]
    return out


def index_range(t_sorted: Sequence[float], start: float, end: float) -> tuple[int, int]:
    """Half-open index range [lo, hi) of times within [start, end]; t_sorted must be ascending."""
    return bisect.bisect_left(t_sorted, start), bisect.bisect_right(t_sorted, end)
