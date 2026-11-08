"""Inline SVG charts for the HTML report: no JavaScript, no external files.

Downsampling keeps the maximum of each pixel column by default. A frame-time
chart that averaged its buckets would hide exactly the spikes the report is
about; one 400 ms stutter among 100 000 smooth frames must still reach the top.

A capture can be damaged (A15), so every value is checked with number() first:
a point that is not a sane finite number is left out, never drawn or crashed on.
"""
from __future__ import annotations

import math
from html import escape
from typing import Any, Sequence

W, H = 960, 220
LEFT, RIGHT, TOP, BOTTOM = 52, 14, 12, 28
MAX_VALUE = 1e7              # analysis.frames.MAX_ABS_VALUE: a time, ms, % or MB past it is corrupt


def number(v: Any) -> float | None:
    """v as a float when it is a finite number within +-MAX_VALUE, else None (strings, bools, NaN, 1e308)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if math.isfinite(v) and abs(v) <= MAX_VALUE else None


def fmt_elapsed(seconds: Any) -> str:
    """Time into the recording, or a length of time, never like a time of day: "13:22" beside a 12:17 start read as
    a clock. "45s", "13m 22s", "14m" on the minute, "1h 02m 03s"; "-" for a time that is not a sane number."""
    seconds = number(seconds)
    if seconds is None:
        return "-"
    h, rest = divmod(int(round(max(0.0, seconds))), 3600)
    m, s = divmod(rest, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s" if s else f"{m}m"
    return f"{s}s"


def nice_ticks(lo: float, hi: float, n: int = 5) -> list[float]:
    """About n round-numbered ticks (steps of 1, 2, 2.5 or 5 times a power of ten) covering lo..hi."""
    if hi <= lo:
        return [lo]
    raw = (hi - lo) / max(1, n)
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    first = math.ceil(lo / step) * step
    ticks, v = [], first
    while v <= hi + step * 1e-9:
        ticks.append(round(v, 10))
        v += step
    return ticks


def time_ticks(lo: float, hi: float, n: int = 8) -> list[float]:
    """Ticks for a time axis in seconds, on steps a clock reader expects (15 s, 30 s, 1 min, 5 min ...)."""
    if hi <= lo:
        return [lo]
    raw = (hi - lo) / max(1, n)
    steps = (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200)
    step = next((s for s in steps if s >= raw), steps[-1] * math.ceil(raw / steps[-1]))
    first = math.ceil(lo / step) * step
    return [float(first + i * step) for i in range(int((hi - first) // step) + 1)]


def downsample(xs: Sequence[float], ys: Sequence[float | None], x0: float, x1: float, buckets: int,
               agg: str = "max") -> list[tuple[float, float] | None]:
    """One point per bucket (max or mean of its values).

    None in the output marks a real gap: a bucket whose samples were all missing
    (None or NaN), such as a lost ping. A bucket with no samples at all is simply
    skipped, so a line sampled more sparsely than the chart has pixels stays
    continuous instead of turning into dots.
    """
    if x1 <= x0 or buckets < 1:
        return []
    sums = [0.0] * buckets
    counts = [0] * buckets
    missing = [False] * buckets
    peaks: list[float | None] = [None] * buckets
    span = x1 - x0
    for x, y in zip(xs, ys):
        x = number(x)
        if x is None or x < x0 or x > x1:
            continue
        b = min(buckets - 1, int((x - x0) / span * buckets))
        y = number(y)
        if y is None:                     # missing, or junk from a damaged capture
            missing[b] = True
            continue
        counts[b] += 1
        sums[b] += y
        if peaks[b] is None or y > peaks[b]:
            peaks[b] = y
    out: list[tuple[float, float] | None] = []
    for b in range(buckets):
        if counts[b]:
            y = peaks[b] if agg == "max" else sums[b] / counts[b]
            out.append((x0 + (b + 0.5) * span / buckets, y))
        elif missing[b]:
            out.append(None)
    return out


class Chart:
    """A time chart: lines, dots, vertical marks, shaded bands and reference lines."""

    def __init__(self, title: str, x0: float, x1: float, y0: float, y1: float, y_unit: str = "") -> None:
        x0, y0 = number(x0) or 0.0, number(y0) or 0.0
        x1, y1 = number(x1), number(y1)
        if x1 is None or x1 <= x0:
            x1 = x0 + 1.0
        if y1 is None or y1 <= y0:
            y1 = y0 + 1.0
        self.title, self.x0, self.x1, self.y0, self.y1, self.y_unit = title, x0, x1, y0, y1, y_unit
        self._back: list[str] = []
        self._front: list[str] = []
        self.legend: list[tuple[str, str]] = []   # (css class, label) for the HTML legend

    # coordinates
    def px(self, x: float) -> float:
        return LEFT + (x - self.x0) / (self.x1 - self.x0) * (W - LEFT - RIGHT)

    def py(self, y: float) -> float:
        y = min(max(y, self.y0), self.y1)          # clamp: a spike past the top is drawn at the top
        return TOP + (1 - (y - self.y0) / (self.y1 - self.y0)) * (H - TOP - BOTTOM)

    def line(self, xs: Sequence[float], ys: Sequence[float | None], cls: str, label: str,
             agg: str = "max") -> "Chart":
        pts = downsample(xs, ys, self.x0, self.x1, W - LEFT - RIGHT, agg)
        d, pen_down = [], False
        for p in pts:
            if p is None:
                pen_down = False
                continue
            d.append(f"{'L' if pen_down else 'M'}{self.px(p[0]):.1f},{self.py(p[1]):.1f}")
            pen_down = True
        if d:
            self._front.append(f'<path class="ln {cls}" d="{" ".join(d)}"><title>{escape(label)}</title></path>')
            self.legend.append((cls, label))
        return self

    def dots(self, points: Sequence[tuple[float, float]], cls: str, label: str) -> "Chart":
        drawn = False
        for x, y in points:
            x, y = number(x), number(y)
            if x is None or y is None:
                continue
            self._front.append(f'<circle class="dot {cls}" cx="{self.px(x):.1f}" cy="{self.py(y):.1f}" r="3.5">'
                               f'<title>{escape(label)} at {fmt_elapsed(x)}: {y:.0f}{escape(self.y_unit)}</title></circle>')
            drawn = True
        if drawn:
            self.legend.append((cls, label))
        return self

    def vline(self, x: float, cls: str, label: str) -> "Chart":
        x = number(x)
        if x is None or not self.x0 <= x <= self.x1:
            return self
        X = self.px(x)
        self._front.append(f'<line class="vl {cls}" x1="{X:.1f}" x2="{X:.1f}" y1="{TOP}" y2="{H - BOTTOM}">'
                           f'<title>{escape(label)} at {fmt_elapsed(x)}</title></line>')
        if (cls, label) not in self.legend:
            self.legend.append((cls, label))
        return self

    def band(self, xa: float, xb: float, cls: str, label: str) -> "Chart":
        xa, xb = number(xa), number(xb)
        if xa is None or xb is None or xb < self.x0 or xa > self.x1:
            return self
        a, b = self.px(max(xa, self.x0)), self.px(min(xb, self.x1))
        self._back.append(f'<rect class="band {cls}" x="{a:.1f}" y="{TOP}" width="{max(1.0, b - a):.1f}" '
                          f'height="{H - TOP - BOTTOM}"><title>{escape(label)}</title></rect>')
        if (cls, label) not in self.legend:
            self.legend.append((cls, label))
        return self

    def hline(self, y: float, cls: str, label: str) -> "Chart":
        y = number(y)
        if y is not None and self.y0 <= y <= self.y1:
            Y = self.py(y)
            self._back.append(f'<line class="hl {cls}" x1="{LEFT}" x2="{W - RIGHT}" y1="{Y:.1f}" y2="{Y:.1f}">'
                              f'<title>{escape(label)}</title></line>')
            self.legend.append((cls, label))
        return self

    def svg(self) -> str:
        # Inline SVG inside HTML needs no xmlns, which keeps the page free of any URL at all.
        parts = [f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" aria-label="{escape(self.title)}">'
                 f'<title>{escape(self.title)}</title>']
        for t in nice_ticks(self.y0, self.y1, 4):
            Y = self.py(t)
            parts.append(f'<line class="grid" x1="{LEFT}" x2="{W - RIGHT}" y1="{Y:.1f}" y2="{Y:.1f}"/>'
                         f'<text class="tick" x="{LEFT - 6}" y="{Y + 4:.1f}" text-anchor="end">'
                         f'{t:g}{escape(self.y_unit)}</text>')
        for t in time_ticks(self.x0, self.x1, 8):
            X = self.px(t)
            parts.append(f'<text class="tick" x="{X:.1f}" y="{H - 8}" text-anchor="middle">{fmt_elapsed(t)}</text>')
        parts += self._back + self._front
        parts.append(f'<rect class="frame" x="{LEFT}" y="{TOP}" width="{W - LEFT - RIGHT}" '
                     f'height="{H - TOP - BOTTOM}"/></svg>')
        return "".join(parts)
