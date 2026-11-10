"""Did lag-doctor's periodic sampling make the game stutter? A phase-locking test on one capture.

While it records, lag-doctor wakes up on fixed cadences: the system sampler at 4 Hz, the NVML reader about once
a second, the network prober at 2 Hz. If a wake-up cost the game time, the frames that overlap it would be longer,
or stutter more often, than frames at any other moment of the same cadence.

Frame timing comes from the capture's own PresentMon columns (the main swap chain, as lagdoctor.analysis.frames
picks it), or from a LagLab ground-truth log (--laglab-log: "frames" is a list of frame START times in absolute
time.perf_counter seconds; frame k lasts frames[k+1] - frames[k]; both clocks are QueryPerformanceCounter, so
capture time = perf_counter - capture.clock_origin).

Statistics per event source:
  (a) mean frame time of the frames whose interval [t - frame_ms, t] overlaps an event's busy window
      (system: [t - 20 ms, t]; gpu and network: [t - 5 ms, t + 5 ms])
  (b) the stutter rate among those frames (frame time > 2 x the median of the 31 frames centred on it)
  (c) the phase profile: mean frame time by the frame END's phase after the latest event (10 ms bins over the
      period), tested family-wise with the largest |z| over bins

The null: the same statistic with all of a source's event times shifted by one constant offset, drawn uniformly
over the source's period, wrapping around the analysed span. A shift keeps the frame series, the event cadence,
length bias (a long frame is more likely to hold any fixed moment) and any fixed phase between the game's frame
cadence and the sampler (aliasing), and breaks only the alignment under test.
  * Literal null (as specified): the shifts at least --exclude-ms from zero; its mean and 2.5-97.5 % interval give
    the effect size. Its p-value is NOT used for the verdict: on heavy-tailed frame times (long frames make the
    statistic vary smoothly with phase) comparing the real phase only with far-away phases is anti-conservative
    (40 synthetic no-effect runs: p < 0.05 in 25-28 %; 100 decoys on a real game recording: 15-18 %).
  * Calibrated test (used for the verdict): every phase on the full circle, the real one included, gets the same
    treatment, D = its value minus the mean over the shifts at least --exclude-ms away from it; the real D is ranked
    among all of them. Exact for a perfectly periodic train (the @grid sources) when the sampler's start phase is
    random; a row's own jitter breaks the circle's seam a little (synthetic heavy-tailed runs: 5-12 % at a nominal
    5 %; decoys on the real capture: 3-8 %). Cost: shifts near zero share a spread-out effect (a long stutter frame
    overlaps many phases), so its p has a floor near 0.01-0.03 for stutter effects; dense effects reach 0.001.
  * Decoys (--decoys N): N trains at other periods (0.8-1.25 x, not within 4 % of the real one) with the same jitter,
    no link to lag-doctor; the share of them the test flags is its real false-positive rate on THIS frame series, and
    the real p (and the literal |z|) is ranked among theirs. 60 or more let a rank fall below 0.0167.

Reverse causation: a row's t is taken after its work, so a game stall that holds up the sampler pushes t (and its
window) onto the stall's end, which reads as an effect the sampler did not cause. The "@grid" sources put the
same windows on each row's fitted regular schedule (system: the exact 0.25 s grid; gpu: the driver's refresh
grid, fitted per stretch between half-period jumps), which a stall cannot move. An effect the sampler causes shows
in both; one that a stall causes shows only in the t-anchored source.

Run it from the lag-doctor checkout (it imports lagdoctor to read the capture and pick the main swap chain):
  uv run python scripts/overhead/phase_lock.py CAPTURE.ldcap.json.gz [--laglab-log LAGLAB.json] [--json OUT.json]

It prints only aggregates (counts, means, rates, per-bin means); no per-frame values leave the process.
tests/test_overhead_phase_lock.py runs it on synthetic series whose answer is known; docs/research/overhead.md,
section 4.2, has what it found on a real recording.
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import random
import statistics
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

# Each source: the capture table its times come from, its busy window around a row's t (seconds, [t+lo, t+hi]),
# its nominal period, and where in lag-doctor the timing comes from.
SOURCES = {
    "system": {"table": "system", "lo": -0.020, "hi": 0.0, "period": 0.25, "label": "system ticks (4 Hz)",
               "why": "t is the midpoint of the process snapshot, which follows the PDH collect "
                      "(lagdoctor/capture/sysmon.py _tick); the tick's work spans about the 16 ms before t"},
    "gpu": {"table": "gpu", "lo": -0.005, "hi": 0.005, "period": 1.0, "label": "gpu rows (NVML ~1 Hz)",
            "why": "t is read just after an NVML refresh (lagdoctor/capture/gpumon.py _read_after_refresh); "
                   "the thread polls NVML every 10 ms for up to 0.9 s before it"},
    "network": {"table": "network", "lo": -0.005, "hi": 0.005, "period": 0.5, "label": "network rounds (2 Hz)",
                "why": "t is the round's tick; its pings and link reads start there "
                       "(lagdoctor/capture/netmon.py _round)"},
}
DEFAULT_SOURCES = "system,gpu,network,system@grid,gpu@grid,network@grid"

DEFAULT_SHIFTS = 1000         # literal-null shifts per source (outside the exclusion zone); more are drawn inside it
DEFAULT_EXCLUDE_MS = 30.0
DEFAULT_BIN_MS = 10.0
DEFAULT_LOCAL_N = 31          # frames in the centred window whose median is a frame's local baseline
DEFAULT_STUTTER_X = 2.0       # a stutter frame is longer than this many times its local baseline
ALPHA = 0.05                  # per source, split over the three statistics (Bonferroni: each at ALPHA / 3)
MIN_HIT_FRAMES = 200          # fewer frames overlapping an event than this: "cannot tell"
CLEAR_MEAN_MS = 1.0           # a detected effect is "clear" at 1 ms or more on the mean of the frames it touches,
CLEAR_RATIO = 1.5             # or 1.5 x the null stutter rate with at least
CLEAR_MIN_EXCESS = 10         # 10 extra stutters,
CLEAR_BIN_MS = 2.0            # or one phase bin 2 ms or more off its null
HITCH_MARGIN_S = 0.1          # LagLab's planted hitches: frames up to this long after one are left out too
MIN_LAGLAB_FRAMES = 100       # a LagLab log with fewer frames inside the capture than this is refused
GRID_TOL = 0.15               # a row up to 15 % of a period early, or 85 % late, belongs to its own grid point
GRID_MIN_ROWS = 10            # stretches of the schedule shorter than this are not fitted (their rows are dropped)


def source_cfg(name: str) -> dict:
    base, _, variant = name.partition("@")
    if base not in SOURCES or variant not in ("", "grid"):
        raise KeyError(name)
    cfg = dict(SOURCES[base], base=base, grid=variant == "grid", name=name)
    if cfg["grid"]:
        cfg["label"] += ", windows on their fitted schedule"
    return cfg


DECOY_RATIOS = (0.80, 1.25)   # decoy periods: the source's period times a ratio in this range ...
DECOY_GAP = 0.04              # ... but not within 4 % of 1 (the sampler's own period)


def decoy_trains(rows: list[float], grid: list[float | None] | None, period: float, t0: float, t1: float,
                 n: int, seed: str) -> list[tuple[float, list[float]]]:
    """n event trains with no causal link to lag-doctor: regular at period * r (r spread over DECOY_RATIOS, away
    from 1), a random phase, and (for t-anchored sources) each event jittered by a residual drawn from the real
    rows' delays against their fitted schedule. Returns (period, times) per train."""
    rng = random.Random(f"{seed}:decoys")
    res = [t - g for t, g in zip(rows, grid or []) if g is not None] if grid is not None else []
    lo, hi = DECOY_RATIOS
    ratios = []
    while len(ratios) < n:
        r = math.exp(rng.uniform(math.log(lo), math.log(hi)))
        if abs(r - 1.0) >= DECOY_GAP:
            ratios.append(r)
    out = []
    for r in ratios:
        p = period * r
        start = t0 + rng.uniform(0.0, p)
        k_max = int((t1 - start) / p)
        ev = sorted(start + k * p + (rng.choice(res) if res else 0.0) for k in range(k_max + 1))
        out.append((p, [t for t in ev if t0 <= t < t1]))
    return out


# ------------------------------------------------------------------------------------------------ loading ---

def _finite(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def load_capture(path: Path):
    try:
        from lagdoctor.model import Capture
    except ImportError as exc:
        sys.exit(f"phase_lock: cannot import lagdoctor ({exc}); run this with `uv run --no-sync python` from the "
                 "lag-doctor checkout")
    return Capture.load(path)


def frames_from_capture(cap) -> tuple[list[float], list[float], list[int], dict]:
    """(frame end times s, frame times ms, use mask, info) of the main swap chain, in time order."""
    from lagdoctor.analysis.frames import main_chain
    from lagdoctor.model import F
    fr = main_chain(cap)
    chain = next((c for c in fr.get(F.SWAP_CHAIN) or [] if isinstance(c, str)), None)
    pm = cap.target.get("presentmon") if isinstance(cap.target, dict) else None
    recorded = pm.get("main_chain") if isinstance(pm, dict) else None
    chains = {c for c in (cap.frames.get(F.SWAP_CHAIN) or []) if c is not None}
    ends, ms = [], []
    rows = fr.get(F.T) or []
    for t, d in zip(rows, fr.get(F.FRAME_MS) or []):
        if t is None or d is None or not d > 0:
            continue
        ends.append(float(t))
        ms.append(float(d))
    info = {"frames_source": "capture (PresentMon, main swap chain)", "chain_rows": len(rows),
            "swap_chains_in_capture": len(chains),
            "chain_agrees_with_recorded_main_chain": None if recorded is None or chain is None else recorded == chain,
            "rows_without_frame_time": len(rows) - len(ends)}
    return ends, ms, [1] * len(ends), info


def frames_from_laglab(path: Path, origin: float, duration_s: float, keep_hitches: bool
                       ) -> tuple[list[float], list[float], list[int], dict]:
    """The same from a LagLab log: frame k spans [frames[k], frames[k+1]]; planted hitches left out by default.

    LagLab logs its whole run, and a capture may cover only part of it (bench.py's attached windows), so only the
    frames ending inside the capture are kept; a log that covers less than half of the capture, or under 100 of its
    frames, is refused: most likely another run's."""
    log = json.loads(Path(path).read_text(encoding="utf-8"))
    raw = log.get("frames") if isinstance(log, dict) else None
    if not isinstance(raw, list):
        sys.exit("phase_lock: the LagLab log has no 'frames' list")
    starts = [float(x) for x in raw if _finite(x)]
    unsorted = sum(1 for a, b in zip(starts, starts[1:]) if b < a)
    if unsorted:
        starts.sort()
    if len(starts) < 3:
        sys.exit("phase_lock: the LagLab log holds fewer than 3 frames")
    ends = [b - origin for b in starts[1:]]
    ms = [(b - a) * 1000.0 for a, b in zip(starts, starts[1:])]
    keep = [i for i, e in enumerate(ends) if 0.0 <= e <= duration_s]       # ends rise, so one index range
    covered = ends[keep[-1]] - ends[keep[0]] if keep else 0.0
    if len(keep) < MIN_LAGLAB_FRAMES or covered < 0.5 * duration_s:
        sys.exit(f"phase_lock: {len(keep)} of {len(ends)} LagLab frames, over {covered:.1f} s, fall inside the "
                 f"capture (0 to {duration_s:.1f} s on its clock); is the log from the same run as the capture?")
    inside = len(keep)
    ends, ms = ends[keep[0]:keep[-1] + 1], ms[keep[0]:keep[-1] + 1]
    use = [1] * len(ends)
    windows = sorted((h["start"] - origin, h["end"] - origin + HITCH_MARGIN_S)
                     for h in (log.get("hitches") or [])
                     if isinstance(h, dict) and _finite(h.get("start")) and _finite(h.get("end")))
    excluded = 0
    if windows and not keep_hitches:
        starts_rel = [e - d / 1000.0 for e, d in zip(ends, ms)]
        for a, b in windows:          # frames tile here (start k+1 = end k), so the overlap is one index range
            i0 = bisect.bisect_right(ends, a)
            i1 = bisect.bisect_left(starts_rel, b, i0)
            for i in range(i0, i1):
                excluded += use[i]
                use[i] = 0
    info = {"frames_source": f"LagLab log (mode {log.get('mode')!r})", "laglab_frames": len(starts),
            "laglab_out_of_order_starts": unsorted, "laglab_planted_hitches": len(windows),
            "frames_left_out_for_planted_hitches": excluded, "frames_inside_capture": inside}
    return ends, ms, use, info


def events_of(cap, table: str) -> list[float]:
    col = (getattr(cap, table, None) or {}).get("t") or []
    return sorted(float(x) for x in col if _finite(x))


# ------------------------------------------------------------------------------------------ the schedule ---

def fit_grid(ts: list[float], p_nom: float) -> tuple[list[float | None], dict]:
    """Per row, the time it would have had on its own regular schedule (None where no stretch could be fitted).

    The schedule is split where an interval is not near a whole number of periods (the NVML reader jumps half a
    period when the driver's refresh crosses its 1 s tick); in each stretch t = a + b * k is fitted with outliers
    (rows held up) left out, and a is the median phase, so a grid point is a typical row's time."""
    grid: list[float | None] = [None] * len(ts)
    if len(ts) < GRID_MIN_ROWS:
        return grid, {"stretches": 0}
    segs, cur = [], [0]
    for i in range(1, len(ts)):
        r = (ts[i] - ts[i - 1]) / p_nom
        if round(r) == 0 or abs(r - round(r)) > 0.25:
            segs.append(cur)
            cur = [i]
        else:
            cur.append(i)
    segs.append(cur)
    fitted, periods = 0, []
    for seg in segs:
        if len(seg) < GRID_MIN_ROWS:
            continue
        t = [ts[i] for i in seg]
        a, b = t[0], p_nom
        for _ in range(5):
            k = [math.floor((x - a) / b + GRID_TOL) for x in t]
            a = statistics.median(x - b * kk for x, kk in zip(t, k))
            res = [x - a - b * kk for x, kk in zip(t, k)]
            mad = statistics.median(abs(r) for r in res)
            keep = [j for j, r in enumerate(res) if abs(r) <= 4 * mad + 0.002]
            if len(keep) >= GRID_MIN_ROWS and len({k[j] for j in keep}) > 1:
                km = statistics.fmean(k[j] for j in keep)
                tm = statistics.fmean(t[j] for j in keep)
                b = (sum((k[j] - km) * (t[j] - tm) for j in keep) / sum((k[j] - km) ** 2 for j in keep))
                a = statistics.median(t[j] - b * k[j] for j in keep)
        k = [math.floor((x - a) / b + GRID_TOL) for x in t]
        for i, kk in zip(seg, k):
            grid[i] = a + b * kk
        fitted += len(seg)
        periods.append(b)
    res = [t - g for t, g in zip(ts, grid) if g is not None]
    info = {"stretches": len(segs), "rows_fitted": fitted, "rows_dropped": len(ts) - fitted,
            "fitted_periods_s": [round(p, 7) for p in periods][:10]}
    if res:
        q = statistics.quantiles(res, n=100, method="inclusive")
        info["delay_vs_schedule_ms"] = {"p1": round(q[0] * 1000, 2), "p5": round(q[4] * 1000, 2),
                                        "p50": round(statistics.median(res) * 1000, 2),
                                        "p95": round(q[94] * 1000, 2), "p99": round(q[98] * 1000, 2),
                                        "max": round(max(res) * 1000, 2)}
        info["rows_held_up_over_25_ms"] = sum(1 for r in res if r > 0.025)
    return grid, info


# ------------------------------------------------------------------------------------------- frame series ---

def stutter_flags(ms: list[float], n: int, x: float) -> list[int]:
    """1 where a frame is longer than x times the median of the n frames centred on it (fewer at the ends)."""
    h = n // 2
    out = [0] * len(ms)
    for i, d in enumerate(ms):
        w = sorted(ms[max(0, i - h): i + h + 1])
        k = len(w)
        med = w[k // 2] if k % 2 else 0.5 * (w[k // 2 - 1] + w[k // 2])
        out[i] = 1 if d > x * med else 0
    return out


def hit_indices(ends: list[float], starts: list[float], events: list[float], lo: float, hi: float) -> list[int]:
    """Indexes of frames whose [start, end] overlaps some [t+lo, t+hi] (exact; no ordering of starts assumed)."""
    a = sorted(t + lo for t in events)
    w = hi - lo
    out = []
    for i, (s, e) in enumerate(zip(starts, ends)):
        j = bisect.bisect_right(a, s - w)            # first window start > s - w, i.e. first window ending after s
        if j < len(a) and a[j] < e:
            out.append(i)
    return out


def build_state(ends, ms, use, events_by_source, opts) -> dict:
    """Everything the per-shift statistics read: geometry, values, prefix sums, events within the span."""
    starts = [e - d / 1000.0 for e, d in zip(ends, ms)]
    stut = stutter_flags(ms, opts["local_n"], opts["stutter_x"])
    pu, pd, ps = [0], [0.0], [0]
    for d, u, s in zip(ms, use, stut):
        pu.append(pu[-1] + u)
        pd.append(pd[-1] + (d if u else 0.0))
        ps.append(ps[-1] + (1 if u and s else 0))
    mono_breaks = sum(1 for a, b in zip(starts, starts[1:]) if b < a - 1e-6)
    t0, t1 = min(starts), max(ends)
    events = {name: sorted(t for t in ev if t0 <= t < t1) for name, ev in events_by_source.items()}
    return {"E": ends, "S": starts, "D": ms, "use": use, "stut": stut, "pu": pu, "pd": pd, "ps": ps,
            "mono": mono_breaks == 0, "mono_breaks": mono_breaks, "T0": t0, "T1": t1, "events": events}


# ------------------------------------------------------------------------- per-shift statistics (workers) ---

_ST: dict | None = None


def _init(state: dict) -> None:
    global _ST
    _ST = state


def _hits(a: list[float], lo: float, hi: float) -> tuple[int, float, int]:
    """(frames used, their summed frame time ms, their stutters) over frames overlapping any window."""
    st = _ST
    E, S, pu, pd, ps = st["E"], st["S"], st["pu"], st["pd"], st["ps"]
    n, sd, ns = 0, 0.0, 0
    if st["mono"]:          # starts never go back: the frames a window overlaps are one index range
        br, bl = bisect.bisect_right, bisect.bisect_left
        last, ne = 0, len(E)
        for t in a:
            i0 = br(E, t + lo, last)                 # first frame (not yet counted) ending after the window starts
            if i0 >= ne:
                break
            i1 = bl(S, t + hi, i0)                   # first frame starting at or after the window ends
            if i1 > i0:
                n += pu[i1] - pu[i0]
                sd += pd[i1] - pd[i0]
                ns += ps[i1] - ps[i0]
                last = i1
            else:
                last = i0
        return n, sd, ns
    D, use, stut = st["D"], st["use"], st["stut"]
    for i in hit_indices(E, S, a, lo, hi):
        if use[i]:
            n += 1
            sd += D[i]
            ns += stut[i]
    return n, sd, ns


def _profile(a: list[float], nb: int, inv_w: float) -> tuple[list[float], list[int]]:
    """Per phase bin: summed frame time ms and frame count, by the frame end's phase after the latest event."""
    st = _ST
    E, D, use = st["E"], st["D"], st["use"]
    sums, cnts = [0.0] * nb, [0] * nb
    bl = bisect.bisect_left
    ne, m = len(E), len(a)
    i = 0
    for j in range(m):
        t = a[j]
        nxt = a[j + 1] if j + 1 < m else math.inf
        i = bl(E, t, i)                              # first frame ending at or after this event
        while i < ne:
            e = E[i]
            if e >= nxt:
                break
            b = int((e - t) * inv_w)
            if b >= nb:                              # past the period (a skipped tick): no bin
                i = bl(E, nxt, i)
                break
            if use[i]:
                sums[b] += D[i]
                cnts[b] += 1
            i += 1
    return sums, cnts


def _shift(a: list[float], t0: float, t1: float, delta: float) -> list[float]:
    if delta == 0.0:
        return a
    span = t1 - t0
    s = bisect.bisect_left(a, t1 - delta)            # events pushed past the end wrap to the start: still sorted
    return [t + delta - span for t in a[s:]] + [t + delta for t in a[:s]]


def _work(task):
    """One chunk of offsets for one source; offset 0.0 is the real alignment. A decoy task (src["decoy"]) holds all
    of its offsets and returns only its summary: p-values, literal z and verdict."""
    src, items = task
    st = _ST
    a = st["events"][src["name"]]
    out = []
    for k, delta in items:
        b = _shift(a, st["T0"], st["T1"], delta)
        n, sd, ns = _hits(b, src["lo"], src["hi"])
        sums, cnts = _profile(b, src["nb"], src["inv_w"])
        out.append((k, delta, n, sd, ns, sums, cnts))
    if src.get("decoy"):
        res = summarize_source(src, sorted(out), st["T1"] - st["T0"])
        return [{"p": {k: res[k].get("p") for k in ("a", "b", "c")},
                 "z": {k: res[k].get("z_literal") for k in ("a", "b")},
                 "verdict": verdict(res, len(a))[0]}]
    return out


# ---------------------------------------------------------------------------------------------- summaries ---

def _q(xs: list[float]) -> tuple[float | None, float | None]:
    xs = [x for x in xs if x is not None]
    if not xs:
        return None, None
    if len(xs) == 1:
        return xs[0], xs[0]
    q = statistics.quantiles(xs, n=40, method="inclusive")       # cut points every 2.5 %
    return q[0], q[-1]


def _circ(a: float, b: float, period: float) -> float:
    d = abs(a - b) % period
    return min(d, period - d)


class _Arc:
    """For each point on the circle, the points at least `excl` away from it, as index ranges in sorted order."""

    def __init__(self, deltas: list[float], period: float, excl: float) -> None:
        self.order = sorted(range(len(deltas)), key=deltas.__getitem__)
        self.ds = [deltas[i] for i in self.order]
        self.period, self.excl = period, excl

    def near(self, d: float) -> list[tuple[int, int]]:
        """Index ranges (sorted order) of the points closer than excl to d, around the circle."""
        ds, p, e = self.ds, self.period, self.excl
        lo, hi = d - e, d + e
        out = [(bisect.bisect_right(ds, lo) if lo >= 0 else 0, bisect.bisect_left(ds, hi) if hi <= p else len(ds))]
        if lo < 0:
            out.append((bisect.bisect_right(ds, lo + p), len(ds)))
        if hi > p:
            out.append((0, bisect.bisect_left(ds, hi - p)))
        return out


def _d_values(arc: _Arc, deltas: list[float], vals: list[float | None]) -> list[float | None]:
    """D per point: its value minus the mean value of the points at least excl away from it (None = missing)."""
    ps_, pc = [0.0], [0]
    for i in arc.order:
        v = vals[i]
        ps_.append(ps_[-1] + (v if v is not None else 0.0))
        pc.append(pc[-1] + (v is not None))
    ts, tc = ps_[-1], pc[-1]
    out = []
    for d, v in zip(deltas, vals):
        if v is None:
            out.append(None)
            continue
        es, ec = 0.0, 0
        for a, b in arc.near(d):
            if b > a:
                es += ps_[b] - ps_[a]
                ec += pc[b] - pc[a]
        n = tc - ec
        out.append(v - (ts - es) / n if n else None)
    return out


def _scalar(arc: _Arc, deltas: list[float], sums: list[float], cnts: list[float], period: float,
            excl: float) -> dict:
    """Index 0 is the real alignment. The literal null (shifts >= excl from 0) and the calibrated test."""
    vals = [s / c if c else None for s, c in zip(sums, cnts)]
    real = vals[0]
    if real is None:
        return {"real": None}
    lit = [v for v, d in zip(vals[1:], deltas[1:]) if v is not None and _circ(d, 0.0, period) >= excl]
    m = statistics.fmean(lit)
    lo, hi = _q(lit)
    dl = abs(real - m)
    sd = statistics.pstdev(lit)
    out = {"real": real, "null_mean": m, "null_lo": lo, "null_hi": hi, "null_n": len(lit), "effect": real - m,
           "effect_lo": lo - m, "effect_hi": hi - m, "z_literal": (real - m) / sd if sd else None,
           "p_literal": (1 + sum(1 for v in lit if abs(v - m) >= dl - 1e-12)) / (1 + len(lit))}
    D = _d_values(arc, deltas, vals)
    d0 = D[0]
    dn = [x for x in D[1:] if x is not None]
    clo, chi = _q(dn)
    out.update(d_real=d0, cal_lo=clo, cal_hi=chi, cal_n=len(dn),
               p=(1 + sum(1 for x in dn if abs(x) >= abs(d0) - 1e-12)) / (1 + len(dn)))
    return out


def summarize_source(src: dict, rows: list, span_s: float) -> dict:
    """rows: (k, delta, n, sd, ns, sums, cnts), row 0 the real alignment."""
    period, excl, nb = src["period"], src["excl"], src["nb"]
    deltas = [r[1] for r in rows]
    arc = _Arc(deltas, period, excl)
    n_hit = [r[2] for r in rows]
    a = _scalar(arc, deltas, [r[3] for r in rows], n_hit, period, excl)
    b = _scalar(arc, deltas, [float(r[4]) for r in rows], n_hit, period, excl)
    n0, ns0 = rows[0][2], rows[0][4]
    if a.get("effect") is not None:
        a["excess_ms_per_s"] = a["effect"] * n0 / span_s          # inferred: extra frame time per second of play
    if b.get("null_mean"):
        m = b["null_mean"]
        b.update(ratio=b["real"] / m, ratio_lo=b["null_lo"] / m, ratio_hi=b["null_hi"] / m,
                 cal_ratio_lo=(m + b["cal_lo"]) / m, cal_ratio_hi=(m + b["cal_hi"]) / m, stutters=ns0,
                 excess_stutters=ns0 - m * n0, excess_stutters_per_min=(ns0 - m * n0) / (span_s / 60.0))
    elif b.get("real") is not None:
        b["stutters"] = ns0
    # (c) phase profile
    bins, D_bins = [], []
    for j in range(nb):
        sums = [r[5][j] for r in rows]
        cnts = [r[6][j] for r in rows]
        vals = [s / c if c else None for s, c in zip(sums, cnts)]
        lit = [v for v, d in zip(vals[1:], deltas[1:]) if v is not None and _circ(d, 0.0, period) >= excl]
        lm = statistics.fmean(lit) if lit else None
        llo, lhi = _q(lit)
        D = _d_values(arc, deltas, vals)
        D_bins.append(D)
        dn = [x for x in D[1:] if x is not None]
        sd = statistics.pstdev(dn) if len(dn) > 1 else None
        abs95 = sorted(abs(x) for x in dn)[int(0.95 * (len(dn) - 1))] if dn else None
        bins.append({"bin_ms": [round(j * src["bin_ms"], 3), round((j + 1) * src["bin_ms"], 3)],
                     "frames": cnts[0], "real": vals[0], "null_mean": lm, "null_lo": llo, "null_hi": lhi,
                     "d_real": D[0], "d_sd": sd,
                     "outside": D[0] is not None and abs95 is not None and abs(D[0]) > abs95})

    def maxz(i):
        zs = [abs(D_bins[j][i]) / bins[j]["d_sd"] for j in range(nb)
              if D_bins[j][i] is not None and bins[j]["d_sd"]]
        return max(zs) if zs else None
    z0 = maxz(0)
    zn = [z for z in (maxz(i) for i in range(1, len(rows))) if z is not None]
    c = {"bins": bins, "bins_n": nb, "max_abs_z": z0,
         "p": (1 + sum(1 for z in zn if z >= z0 - 1e-12)) / (1 + len(zn)) if z0 is not None and zn else None,
         "outside_count": sum(1 for x in bins if x["outside"])}
    lit_z = [abs(x["real"] - x["null_mean"]) / x["d_sd"] for x in bins
             if x["real"] is not None and x["null_mean"] is not None and x["d_sd"]]
    worst = max((x for x in bins if x["d_real"] is not None and x["d_sd"]),
                key=lambda x: abs(x["d_real"]) / x["d_sd"], default=None)
    if worst is not None:
        c["worst_bin_ms"] = worst["bin_ms"]
        c["worst_bin_excess_ms"] = worst["d_real"]
    c["max_abs_z_literal"] = max(lit_z) if lit_z else None
    c["frames_per_bin_min"] = min(rows[0][6]) if nb else None
    c["frames_per_bin_max"] = max(rows[0][6]) if nb else None
    c["empty_bins"] = sum(1 for x in rows[0][6] if not x)
    return {"a": a, "b": b, "c": c, "hit_frames": n0, "shifts_total": len(rows) - 1}


def verdict(res: dict, n_events: int) -> tuple[str, str]:
    if n_events == 0:
        return "cannot tell", "no events of this source in the analysed span"
    if res["hit_frames"] < MIN_HIT_FRAMES:
        return "cannot tell", f"only {res['hit_frames']} frames overlap an event (need {MIN_HIT_FRAMES})"
    a, b, c = res["a"], res["b"], res["c"]
    each = ALPHA / 3
    sig = [k for k, p in (("a", a.get("p")), ("b", b.get("p")), ("c", c.get("p"))) if p is not None and p < each]
    if not sig:
        return "no detectable effect", f"no calibrated p below {each:.4f} (alpha {ALPHA} over 3 statistics)"
    clear = (("a" in sig and abs(a["effect"]) >= CLEAR_MEAN_MS)
             or ("b" in sig and (b.get("ratio") or 0) >= CLEAR_RATIO
                 and (b.get("excess_stutters") or 0) >= CLEAR_MIN_EXCESS)
             or ("c" in sig and abs(c.get("worst_bin_excess_ms") or 0) >= CLEAR_BIN_MS))
    return ("clear effect" if clear else "small effect"), f"calibrated p below {each:.4f} for: {', '.join(sig)}"


# ------------------------------------------------------------------------------------------------ analyse ---

def _chunks(items: list, n: int) -> list[list]:
    size = max(1, math.ceil(len(items) / n))
    return [items[i:i + size] for i in range(0, len(items), size)]


def make_events(rows_by_base: dict[str, list[float]], names: list[str]) -> tuple[dict, dict]:
    """Event times per source name (grid sources from their base's rows) and the schedule fits."""
    events, fits = {}, {}
    for name in names:
        cfg = source_cfg(name)
        rows = rows_by_base.get(cfg["base"]) or []
        if cfg["grid"]:
            grid, info = fit_grid(rows, cfg["period"])
            events[name] = sorted(g for g in grid if g is not None)
            fits[name] = info
        else:
            events[name] = list(rows)
    return events, fits


def _offsets(seed: str, period: float, excl: float, shifts: int) -> list[float]:
    """Offsets drawn uniformly over the whole period until `shifts` of them lie at least excl from zero."""
    rng = random.Random(seed)
    offsets, outside = [], 0
    while outside < shifts:
        d = rng.uniform(0.0, period)
        offsets.append(d)
        outside += _circ(d, 0.0, period) >= excl
    return offsets


def analyze(ends: list[float], ms: list[float], use: list[int], events_by_source: dict[str, list[float]],
            opts: dict, decoy_inputs: dict | None = None) -> dict:
    """The whole test on one frame series and its events. opts: shifts, exclude_ms, bin_ms, local_n, stutter_x,
    seed, workers, sources (names; a name's events must be in events_by_source), and optionally decoys (trains per
    source) and decoy_shifts; decoy_inputs[name] = (rows, grid or None) gives the jitter the decoys copy."""
    order = sorted(range(len(ends)), key=ends.__getitem__)
    if any(order[i] != i for i in range(len(order))):
        ends, ms, use = [ends[i] for i in order], [ms[i] for i in order], [use[i] for i in order]
    state = build_state(ends, ms, use, events_by_source, opts)
    n_decoys = opts.get("decoys", 0)
    decoys: dict[str, list[str]] = {}
    if n_decoys and decoy_inputs:
        for name in opts["sources"]:
            if name not in decoy_inputs or not state["events"].get(name):
                continue
            rows, grid = decoy_inputs[name]
            period = source_cfg(name)["period"]
            decoys[name] = []
            for i, (p, ev) in enumerate(decoy_trains(rows, grid, period, state["T0"], state["T1"], n_decoys,
                                                     f"{opts['seed']}:{name}")):
                dname = f"{name}#d{i}"
                state["events"][dname] = ev
                decoys[name].append((dname, p))
    used = [d for d, u in zip(ms, use) if u]
    total_s = sum(used) / 1000.0
    span_s = state["T1"] - state["T0"]
    stut_used = sum(s for s, u in zip(state["stut"], use) if u)
    out = {"frames_used": len(used), "frames_left_out": len(ms) - len(used), "span_s": span_s,
           "fps_mean": len(used) / total_s if total_s else None,
           "median_frame_ms": statistics.median(used) if used else None,
           "mean_frame_ms": statistics.fmean(used) if used else None,
           "stutter_share": stut_used / len(used) if used else None, "stutter_frames": stut_used,
           "starts_out_of_order": state["mono_breaks"], "sources": {}}
    excl = opts["exclude_ms"] / 1000.0
    tasks, meta = [], {}
    for name in opts["sources"]:
        cfg = source_cfg(name)
        ev = state["events"][name]
        raw = events_by_source.get(name) or []
        diffs = [b - a for a, b in zip(raw, raw[1:])]
        regular = [d for d in diffs if 0.5 * cfg["period"] < d < 1.5 * cfg["period"]]
        period = statistics.fmean(regular) if len(regular) >= 20 else cfg["period"]
        nb = max(1, math.ceil(period / (opts["bin_ms"] / 1000.0) - 1e-9))
        src = {"name": name, "lo": cfg["lo"], "hi": cfg["hi"], "period": period, "excl": excl, "nb": nb,
               "inv_w": 1000.0 / opts["bin_ms"], "bin_ms": opts["bin_ms"]}
        meta[name] = {"src": src, "events": len(ev), "events_in_capture": len(raw), "period_s": period,
                      "nominal_period_s": cfg["period"], "label": cfg["label"], "window_s": [cfg["lo"], cfg["hi"]],
                      "median_interval_s": statistics.median(diffs) if diffs else None,
                      "irregular_intervals": len(diffs) - len(regular)}
        if not ev or period <= 2 * excl:
            continue
        items = [(0, 0.0)] + list(enumerate(_offsets(f"{opts['seed']}:{name}", period, excl, opts["shifts"]), 1))
        tasks += [(src, c) for c in _chunks(items, max(1, opts["workers"]) * 3)]
        for dname, p in decoys.get(name, []):
            dsrc = dict(src, name=dname, period=p, nb=max(1, math.ceil(p / (opts["bin_ms"] / 1000.0) - 1e-9)),
                        decoy=True)
            meta[dname] = {"src": dsrc, "events": len(state["events"][dname])}
            ditems = [(0, 0.0)] + list(enumerate(_offsets(f"{opts['seed']}:{dname}", p, excl,
                                                          opts.get("decoy_shifts", 300)), 1))
            tasks.append((dsrc, ditems))
    t_start = time.perf_counter()
    if opts["workers"] > 1 and tasks:
        with ProcessPoolExecutor(opts["workers"], initializer=_init, initargs=(state,)) as ex:
            results = list(ex.map(_work, tasks))
    else:
        _init(state)
        results = [_work(t) for t in tasks]
    out["compute_s"] = time.perf_counter() - t_start
    gathered: dict[str, list] = {}
    for (src, _), rows in zip(tasks, results):
        gathered.setdefault(src["name"], []).extend(rows)
    for name in opts["sources"]:
        m = meta[name]
        entry = {k: v for k, v in m.items() if k != "src"}
        rows = sorted(gathered.get(name) or [])
        if not rows:
            entry["verdict"], entry["verdict_reason"] = verdict({"hit_frames": 0}, 0)
            out["sources"][name] = entry
            continue
        res = summarize_source(m["src"], rows, span_s)
        entry.update(res)
        entry["verdict"], entry["verdict_reason"] = verdict(res, m["events"])
        if decoys.get(name):
            entry["decoys"] = _decoy_summary(entry, [(gathered.get(d) or [None])[0] for d, _ in decoys[name]])
        out["sources"][name] = entry
    return out


def _decoy_summary(entry: dict, decoy_results: list) -> dict:
    """How the same test reads trains with no link to lag-doctor on this frame series, and where the real
    source's calibrated p-values (and literal |z|) rank among theirs."""
    ps = {"a": [], "b": [], "c": []}
    zs = {"a": [], "b": []}
    flagged = 0
    for res in decoy_results:
        if not res:
            continue
        flagged += res["verdict"] not in ("no detectable effect", "cannot tell")
        for k in ps:
            p = res["p"].get(k)
            if p is not None:
                ps[k].append(p)
        for k in zs:
            z = res["z"].get(k)
            if z is not None:
                zs[k].append(abs(z))
    n = len([r for r in decoy_results if r])
    out = {"n": n, "flagged_share": flagged / n if n else None,
           "share_p_below_0.05": {k: sum(1 for p in v if p < 0.05) / len(v) if v else None for k, v in ps.items()},
           "real_rank_p": {},
           # the literal test (shifts >= excl only), judged by |effect / literal null sd| against the decoys' own
           "share_literal_abs_z_over_1.96": {k: sum(1 for z in v if z >= 1.96) / len(v) if v else None
                                             for k, v in zs.items()},
           "real_rank_literal_z": {}}
    for k, v in ps.items():
        real = entry[k].get("p")
        if real is not None and v:
            out["real_rank_p"][k] = (1 + sum(1 for p in v if p <= real)) / (1 + len(v))
    for k, v in zs.items():
        real = entry[k].get("z_literal")
        if real is not None and v:
            out["real_rank_literal_z"][k] = (1 + sum(1 for z in v if z >= abs(real))) / (1 + len(v))
    return out


def plant(ends, ms, events, src_name, add_ms, prob, seed) -> int:
    """Self-check: add add_ms to the frame time (value only) of frames overlapping the source's windows around
    `events`, each with probability prob. Returns the number of frames changed."""
    cfg = source_cfg(src_name)
    starts = [e - d / 1000.0 for e, d in zip(ends, ms)]
    rng = random.Random(f"{seed}:plant")
    k = 0
    for i in hit_indices(ends, starts, events, cfg["lo"], cfg["hi"]):
        if rng.random() < prob:
            ms[i] += add_ms
            k += 1
    return k


# --------------------------------------------------------------------------------------------------- text ---

def _f(x, nd=2, sign=False):
    if x is None:
        return "n/a"
    return f"{x:+.{nd}f}" if sign else f"{x:.{nd}f}"


def phase_summary(c: dict) -> str:
    return (f"max |z| {_f(c['max_abs_z'])} over {c['bins_n']} bins, calibrated family-wise p = {_f(c['p'], 3)}; "
            f"{c['outside_count']} bins beyond their own 95 % (about {0.05 * c['bins_n']:.1f} by chance); largest: "
            f"{c.get('worst_bin_ms')} ms after the event, {_f(c.get('worst_bin_excess_ms'), 2, True)} ms")


def report_text(out: dict, head: dict) -> str:
    L = []
    L.append(f"capture: {head['capture_name']} ({_f(head['duration_s'], 1)} s); frames from "
             f"{head['frames_source']}")
    L.append(f"frames used {out['frames_used']:,} (left out {out['frames_left_out']:,}); span "
             f"{_f(out['span_s'], 1)} s; fps {_f(out['fps_mean'], 1)} (mean), median frame "
             f"{_f(out['median_frame_ms'])} ms, mean {_f(out['mean_frame_ms'])} ms")
    L.append(f"stutters (> {head['stutter_x']:g} x median of the {head['local_n']} frames around): "
             f"{out['stutter_frames']:,} = {_f(100 * out['stutter_share'], 3)} % of frames")
    L.append(f"null: shifts drawn uniformly over each period, {head['shifts']} of them at least "
             f"{head['exclude_ms']:g} ms from zero (the literal null); seed {head['seed']!r}; frame starts out of "
             f"order: {out['starts_out_of_order']}")
    for name, s in out["sources"].items():
        L.append("")
        L.append(f"== {name}: {s['label']}: {s['events']:,} events in the span ({s['events_in_capture']:,} rows), "
                 f"period {_f(s['period_s'], 4)} s, busy window [t{1000 * s['window_s'][0]:+.0f} ms, "
                 f"t{1000 * s['window_s'][1]:+.0f} ms]")
        if s.get("schedule"):
            L.append(f"   schedule fit: {json.dumps(s['schedule'])}")
        if "a" not in s:
            L.append(f"   verdict: {s['verdict']} ({s['verdict_reason']})")
            continue
        a, b, c = s["a"], s["b"], s["c"]
        L.append(f"   frames overlapping an event: {s['hit_frames']:,} "
                 f"({_f(100 * s['hit_frames'] / max(1, out['frames_used']), 1)} %); naive comparison: their mean "
                 f"{_f(a['real'])} ms vs {_f(out['mean_frame_ms'])} ms over all frames (length bias included); "
                 f"{s['shifts_total']} shifts in all")
        L.append(f"   (a) mean frame time {_f(a['real'], 3)} ms; literal null mean {_f(a.get('null_mean'), 3)} ms "
                 f"(95 %: {_f(a.get('null_lo'), 3)} to {_f(a.get('null_hi'), 3)}) -> effect "
                 f"{_f(a.get('effect'), 3, True)} ms (literal null 95 %: {_f(a.get('effect_lo'), 3, True)} to "
                 f"{_f(a.get('effect_hi'), 3, True)}; calibrated 95 %: {_f(a.get('cal_lo'), 3, True)} to "
                 f"{_f(a.get('cal_hi'), 3, True)}), calibrated p = {_f(a.get('p'), 3)} (literal p = "
                 f"{_f(a.get('p_literal'), 3)}); extra frame time {_f(a.get('excess_ms_per_s'), 3, True)} ms per s "
                 f"of play (inferred)")
        L.append(f"   (b) stutter rate {_f(100 * (b['real'] or 0), 3)} % ({b.get('stutters', 'n/a')} stutters); "
                 f"literal null {_f(100 * (b.get('null_mean') or 0), 3)} % (95 %: "
                 f"{_f(100 * (b.get('null_lo') or 0), 3)} to {_f(100 * (b.get('null_hi') or 0), 3)}) -> ratio "
                 f"{_f(b.get('ratio'), 3)} (literal null 95 %: {_f(b.get('ratio_lo'), 3)} to "
                 f"{_f(b.get('ratio_hi'), 3)}; calibrated 95 %: {_f(b.get('cal_ratio_lo'), 3)} to "
                 f"{_f(b.get('cal_ratio_hi'), 3)}), calibrated p = {_f(b.get('p'), 3)} (literal p = "
                 f"{_f(b.get('p_literal'), 3)}); excess {_f(b.get('excess_stutters'), 1, True)} stutters "
                 f"({_f(b.get('excess_stutters_per_min'), 2, True)} per min)")
        L.append(f"   (c) phase profile: {phase_summary(c)}; frames per bin {c['frames_per_bin_min']}-"
                 f"{c['frames_per_bin_max']}, empty bins {c['empty_bins']}")
        cells = [f"{x['bin_ms'][0]:g}:{_f(x['d_real'], 2, True)}{'*' if x['outside'] else ''}" for x in c["bins"]]
        for i in range(0, len(cells), 10):
            L.append("       " + "  ".join(cells[i:i + 10]))
        dc = s.get("decoys")
        if dc:
            sh, rk = dc["share_p_below_0.05"], dc["real_rank_p"]
            L.append(f"   decoys ({dc['n']} trains at other periods, no link to lag-doctor): share with calibrated "
                     f"p < 0.05: a {_f(sh.get('a'))}, b {_f(sh.get('b'))}, c {_f(sh.get('c'))}; flagged by the "
                     f"verdict rule: {_f(dc['flagged_share'])}; the real p ranks at a {_f(rk.get('a'), 3)}, "
                     f"b {_f(rk.get('b'), 3)}, c {_f(rk.get('c'), 3)} among them")
            lz, lr = dc["share_literal_abs_z_over_1.96"], dc["real_rank_literal_z"]
            L.append(f"   decoys, literal test: share with |z| >= 1.96: a {_f(lz.get('a'))}, b {_f(lz.get('b'))}; the "
                     f"real |z| (a {_f(abs(a.get('z_literal') or 0))}, b {_f(abs(b.get('z_literal') or 0))}) ranks at "
                     f"a {_f(lr.get('a'), 3)}, b {_f(lr.get('b'), 3)} among them")
        L.append(f"   verdict: {s['verdict']} ({s['verdict_reason']})")
    L.append("")
    L.append(f"computed in {_f(out['compute_s'], 1)} s")
    return "\n".join(L)


# --------------------------------------------------------------------------------------------------- main ---

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("capture", type=Path, help="a lag-doctor capture (.ldcap.json.gz)")
    ap.add_argument("--laglab-log", type=Path, help="take frame times from this LagLab log instead")
    ap.add_argument("--keep-laglab-hitches", action="store_true",
                    help="keep the frames of LagLab's planted hitches (by default they are left out: hitches every "
                         "N s keep a fixed phase with the samplers and would read as an effect)")
    ap.add_argument("--sources", default=DEFAULT_SOURCES,
                    help=f"comma-separated, from system, gpu, network, each optionally @grid (default "
                         f"{DEFAULT_SOURCES})")
    ap.add_argument("--shifts", type=int, default=DEFAULT_SHIFTS,
                    help="shifts per source at least --exclude-ms from zero (>= 500); about 30 %% more are drawn "
                         "inside that zone for the calibrated test")
    ap.add_argument("--exclude-ms", type=float, default=DEFAULT_EXCLUDE_MS)
    ap.add_argument("--bin-ms", type=float, default=DEFAULT_BIN_MS)
    ap.add_argument("--local-n", type=int, default=DEFAULT_LOCAL_N)
    ap.add_argument("--stutter-x", type=float, default=DEFAULT_STUTTER_X)
    ap.add_argument("--seed", default="1")
    ap.add_argument("--workers", type=int, default=max(1, min(8, (os.cpu_count() or 2) - 2)))
    ap.add_argument("--plant", metavar="SOURCE:MS[:PROB]",
                    help="self-check: add MS to the frame time of frames overlapping SOURCE's real windows "
                         "(each with probability PROB, default 1) before testing")
    ap.add_argument("--decoys", type=int, default=0,
                    help="per source, also test this many trains at other periods (0.8-1.25 x, not within 4 %% of "
                         "the real one) with the same jitter: how often the test fires on this frame series with no "
                         "link to lag-doctor (60 or more lets the real p rank below 0.0167)")
    ap.add_argument("--decoy-shifts", type=int, default=300, help="shifts per decoy (default 300)")
    ap.add_argument("--span", metavar="START:END", help="only frames ending in this capture-time range (seconds)")
    ap.add_argument("--json", type=Path, help="also write every aggregate here as JSON")
    args = ap.parse_args(argv)
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    for s in sources:
        try:
            source_cfg(s)
        except KeyError:
            ap.error(f"unknown source {s!r}; use system, gpu or network, optionally with @grid")
    if args.shifts < 500:
        print(f"phase_lock: note: {args.shifts} shifts is below the 500 the test is designed for", file=sys.stderr)

    cap = load_capture(args.capture)
    if args.laglab_log:
        ends, ms, use, finfo = frames_from_laglab(args.laglab_log, float(cap.clock_origin), float(cap.duration_s),
                                                  args.keep_laglab_hitches)
    else:
        ends, ms, use, finfo = frames_from_capture(cap)
    if args.span:
        try:
            s0, s1 = (float(x) for x in args.span.split(":"))
        except ValueError:
            ap.error("--span takes START:END in seconds")
        keep = [i for i, e in enumerate(ends) if s0 <= e <= s1]
        ends, ms, use = [ends[i] for i in keep], [ms[i] for i in keep], [use[i] for i in keep]
        finfo["span"] = [s0, s1]
    if len(ends) < 100:
        st = cap.sources.get("frames")
        print(f"phase_lock: the capture has {len(ends)} usable frames; nothing to test (frame timing source: "
              f"{st.state if st else 'absent'})")
        if args.json:
            args.json.write_text(json.dumps({"frames_used": len(ends), "frames_info": finfo}, indent=1),
                                 encoding="utf-8")
        return 3
    rows_by_base = {b: events_of(cap, SOURCES[b]["table"]) for b in SOURCES}
    events, fits = make_events(rows_by_base, sources)
    planted = None
    if args.plant:
        parts = args.plant.split(":")
        if len(parts) not in (2, 3) or parts[0] not in SOURCES:
            ap.error("--plant takes SOURCE:MS[:PROB] with SOURCE one of system, gpu, network")
        planted = plant(ends, ms, rows_by_base[parts[0]], parts[0], float(parts[1]),
                        float(parts[2]) if len(parts) == 3 else 1.0, args.seed)
    opts = {"shifts": args.shifts, "exclude_ms": args.exclude_ms, "bin_ms": args.bin_ms, "local_n": args.local_n,
            "stutter_x": args.stutter_x, "seed": args.seed, "workers": args.workers, "sources": sources,
            "decoys": args.decoys, "decoy_shifts": args.decoy_shifts}
    decoy_inputs = {}
    if args.decoys:
        for name in sources:
            cfg = source_cfg(name)
            rows = rows_by_base[cfg["base"]]
            decoy_inputs[name] = (rows, None if cfg["grid"] else fit_grid(rows, cfg["period"])[0])
    out = analyze(ends, ms, use, events, opts, decoy_inputs)
    for name, info in fits.items():
        out["sources"][name]["schedule"] = info
    head = {"capture_name": args.capture.parent.name or args.capture.name, "duration_s": float(cap.duration_s),
            "frames_source": finfo["frames_source"],
            **{k: opts[k] for k in ("shifts", "exclude_ms", "bin_ms", "local_n", "stutter_x", "seed")}}
    out["frames_info"] = finfo
    if planted is not None:
        out["planted"] = {"spec": args.plant, "frames_changed": planted}
    for name in sources:
        st = cap.sources.get(SOURCES[source_cfg(name)["base"]]["table"])
        out["sources"][name]["source_state"] = st.state if st else "absent"
        out["sources"][name]["source_detail"] = (st.detail[:160] if st and name in SOURCES else None)
    print(report_text(out, head))
    print(f"frames info: {json.dumps(finfo)}")
    if planted is not None:
        print(f"planted: {args.plant} on {planted} frames (self-check; not the real data)")
    for name in sources:
        print(f"source {name}: state {out['sources'][name]['source_state']}")
    if args.json:
        args.json.write_text(json.dumps({"head": head, **out}, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
