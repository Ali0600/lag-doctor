"""The samples around a moment: every table's rows whose time falls in a span of the capture.

The app's Results screen shows what the CPU, GPU, disk and network did around a lag moment (docs/LAUNCHER-PLAN.md,
screen 4); this cuts those rows out. Pure: it reads the capture and returns new lists, and changes nothing.
"""
from __future__ import annotations

import math
from typing import Any

from lagdoctor.model import Capture

TABLES = ("system", "gpu", "network")


def _time(v: Any) -> float | None:
    """A row's time when it is a real, finite number; anything a damaged capture holds instead is None, an int too
    large for a float included (math.isfinite raises on one, and a file can hold it)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        v = float(v)
    except OverflowError:
        return None
    return v if math.isfinite(v) else None


def _cut(table: Any, t0: float, t1: float) -> dict[str, list]:
    """The rows of one column table with t0 <= t <= t1, every column kept aligned to `t` (a short column reads as
    None past its end). Rows are scanned, not bisected: a damaged capture's times need not be sorted."""
    if not isinstance(table, dict) or not isinstance(table.get("t"), list):
        return {}
    ts = table["t"]
    keep = [i for i, t in enumerate(ts) if (v := _time(t)) is not None and t0 <= v <= t1]
    return {name: [col[i] if i < len(col) else None for i in keep]
            for name, col in table.items() if isinstance(col, list)}


def window(cap: Capture, t0: float, t1: float, frames: dict[str, list] | None = None) -> dict[str, Any]:
    """{"t0", "t1", "frames", "system", "gpu", "network", "others", "marks"} for the span [t0, t1] in capture seconds,
    both ends included.

    `frames` is the frame table to cut: the analysis's main swap chain (analysis.frames.main_chain), which a caller
    showing several moments computes once; by default the capture's own frame table, every chain. "others" keeps the
    other-process samples ([t, rows]) inside the span, and "marks" the lag-key presses. ValueError for a span that is
    not two finite numbers in order."""
    lo, hi = _time(t0), _time(t1)
    if lo is None or hi is None or hi < lo:
        raise ValueError(f"not a span of the capture: {t0!r} to {t1!r}")
    out: dict[str, Any] = {"t0": lo, "t1": hi,
                           "frames": _cut(cap.frames if frames is None else frames, lo, hi)}
    for name in TABLES:
        out[name] = _cut(getattr(cap, name), lo, hi)
    others = cap.others if isinstance(cap.others, list) else []
    out["others"] = [row for row in others
                     if isinstance(row, list) and len(row) == 2 and (v := _time(row[0])) is not None and lo <= v <= hi]
    out["marks"] = [v for m in (cap.marks if isinstance(cap.marks, list) else [])
                    if (v := _time(m)) is not None and lo <= v <= hi]
    return out
