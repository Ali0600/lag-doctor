"""Per-event causes: which measured signal moved when the game lagged (DESIGN "Attribution", LC 5.2).

For each stutter / FPS-drop event:
1. The worst frame's split says which side was slow: GPU busy for most of it -> the GPU side, else the
   CPU side (which includes waiting); between the two r thresholds of LC 3.1 -> either.
2. Each signal is "active" in the event window [t_start - 0.5 s, t_end + 0.25 s] when its level over the
   busiest second of that window clears BOTH a robust z-score (median / MAD of 1 s windows with no event
   nearby) AND an absolute floor in the signal's own units.
3. The lift test over all events (n >= 5) says which causes are linked to the lag at all.
4. Explaining away: each event's first cause is its single best one, weighted by LC 5.2's points.

Constants name their research section: LC = docs/research/lag-causes.md, WT = windows-telemetry.md.
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from lagdoctor.analysis import events as ev_mod
from lagdoctor.analysis import frames as fa
from lagdoctor.analysis import stats as st
from lagdoctor.model import F, G, S, Capture, LagEvent

# ---- windows and baselines (LC 5.2) --------------------------------------------------------------
WINDOW_BEFORE_S = 0.5        # LC 5.2: look at [t_start - 0.5 s, t_end + 0.25 s]
WINDOW_AFTER_S = 0.25
QUIET_GUARD_S = 2.0          # LC 5.2: baseline windows have no event within 2 s ...
QUIET_GUARD_MIN_S = 0.5      # ... halved down to this when events are too dense for that (see attribute())
BASE_WINDOW_S = 1.0          # LC 0.5, WT 3.5: thread time is only good over >= 1 s; every level is a 1 s mean
BASE_STRIDE_S = 0.25         # one 4 Hz sampler tick
MAX_SLIDES = 14_400          # [inferred] an hour of ticks: a longer event window slides in coarser steps
MAX_QUIET_WINDOWS = 345_600  # [inferred] a day of ticks: a longer span (junk times) is scanned coarser
MIN_BASE_WINDOWS = 8         # [inferred] fewer quiet windows than this give no usable median / MAD
Z_MIN = 3.5                  # [inferred] robust z a signal must reach (the usual modified-z outlier cut)
Z_CAP = 10.0                 # [inferred] beyond this, a stronger z adds no more evidence
# ---- lift test (LC 2.6, 5.2) ---------------------------------------------------------------------
LIFT_MIN_EVENTS = 5          # LC 2.6, 5.2: a stutter cause needs n >= 5 events ...
LIFT_MIN_A = 3               # LC 5.2: ... a >= 3 of them with the signal active ...
LIFT_MIN_SHARE = 0.30        # LC 5.2: ... a / n >= 0.30 ...
LIFT_MIN = 2.0               # LC 5.2: ... and lift >= 2 (weight 2)
LIFT_STRONG = 4.0            # LC 5.2: lift >= 4 is worth 3 points
LIFT_B_FLOOR = 0.02          # LC 5.2: lift = (a / n) / max(b, 0.02)
CONTRA_MIN_EVENTS = 10       # LC 5.2: contradicts when measured, n >= 10 ...
CONTRA_MAX_SHARE = 0.10      # LC 5.2: ... and a / n <= 0.10
# ---- absolute floors, per 1 s window -------------------------------------------------------------
THREAD_EXTRA_MS = 31.25      # WT 3.5, LC 0.5: two 15.625 ms accounting ticks of extra thread time
THREAD_BUSY = 0.85           # LC 3.2: a thread at >= 0.85 of a CPU is busy, not waiting
IO_EXTRA_BPS = 1 << 20       # [inferred] >= 1 MiB more game reads than normal in that second
FAULTS_RISE_PS = 1000.0      # LC 4.7 (PAL, S30): Pages Input/sec > 1000 is a warning
RAM_LOW_FRAC = 0.10          # LC 4.7 (PAL, S30): available < 10 % of RAM is a warning ...
RAM_LOW_MB = 64.0            # LC 4.7 (PAL, S30): ... < 64 MB critical (used when the RAM size is unknown)
OTHERS_ONE_PCT = 100.0       # LC 4.6: one non-game process using >= 1 logical CPU more ...
OTHERS_ALL_PCT = 200.0       # LC 4.6: ... or all of them >= 2 logical CPUs more
OTHERS_DISK_BPS = 20e6       # LC 4.6: a non-game process doing >= 20 MB/s
OTHERS_GPU_PCT = 10.0        # LC 4.6: another process using the game adapter's 3D engine >= 10 %
DPC_RISE_PCT = 10.0          # LC 4.10 (PAL, S30): % DPC Time > 10 % on a CPU
IRQ_RISE_PCT = 5.0           # LC 4.10 (PAL, S30): % Interrupt Time > 5 % (Processor Information)
PERF_DROP_FRAC = 0.20        # LC 4.5: % Processor Performance >= 20 % below normal
LIMIT_SHARE = 0.25           # LC 4.5: % Performance Limit < 100 on >= 25 % of samples
NV_THROTTLE_PCT = 30.0       # LC 10 (4.2): held down by heat or power >= 30 % of the time
VRAM_FULL_FRAC = 0.90        # LC 4.3, 10: dedicated VRAM >= 90 % full ...
SHARED_RISE_MB_PER_S = 256.0 / 60.0   # LC 4.3, 10: ... and the game's shared usage +256 MiB/min
DISK_BUSY_PCT = 90.0         # LC 4.8 (PAL, S30): disk idle < 10 %
DISK_READ_MS = 15.0          # LC 4.8 (PAL, S30): Avg. Disk sec/Read > 15 ms
UNFOCUSED_SHARE = 0.5        # [inferred] the game was not in front for at least half of that second
STALL_COVER = ev_mod.STALL_COVER   # a frame holding less than half of the lost time did not cause it

CPU_SIDE = ("game_cpu_spike", "asset_streaming", "memory_paging", "background_cpu", "cpu_throttle")
GPU_SIDE = ("gpu_throttle", "vram_overflow")
ANY_SIDE = ("background_disk", "background_gpu", "driver_dpc", "not_focused")
RESIDUAL = ("gpu_spike", "game_stall")
# Ties in evidence go to the more specific mechanism: reading assets explains the game's own CPU work,
# a background hog explains a slow game thread, and a driver can never be named (DESIGN: "possible").
CAUSE_ORDER = ("not_focused", "memory_paging", "asset_streaming", "vram_overflow", "gpu_throttle", "cpu_throttle",
               "background_disk", "background_gpu", "background_cpu", "game_cpu_spike", "driver_dpc",
               "gpu_spike", "game_stall")
CAPPED_AT_POSSIBLE = ("driver_dpc",)      # DESIGN: PDH cannot name the driver
ATTRIBUTED_KINDS = ("stutter", "fps_drop")
_num = fa._num


# ------------------------------------------------------------------- sampled signals ---

class _Series:
    """One sampled signal (None = missing), with its 1 s window means."""

    def __init__(self, t: list[float], v: list[float | None]) -> None:
        order = sorted(range(len(t)), key=lambda i: t[i])
        self.t = [t[i] for i in order]
        self.v = [v[i] for i in order]
        steps = [b - a for a, b in zip(self.t, self.t[1:]) if b > a]
        dt = st.median(steps) if steps else BASE_STRIDE_S
        self.pad = max(0.0, (dt - BASE_STRIDE_S) / 2.0)    # a 1 Hz series needs a wider look
        self.measured = any(x is not None for x in self.v)

    def mean(self, a: float, b: float) -> float | None:
        lo, hi = bisect.bisect_left(self.t, a - self.pad), bisect.bisect_right(self.t, b + self.pad)
        xs = [x for x in self.v[lo:hi] if x is not None]
        return math.fsum(xs) / len(xs) if xs else None

    def peak(self, a: float, b: float, direction: int) -> float | None:
        """The level over the busiest 1 s (lowest, for a falling signal) of [a, b].

        The 1 s window slides one tick at a time, at most MAX_SLIDES times: a longer window (only junk
        timestamps make one) slides in coarser steps.
        """
        if b - a <= BASE_WINDOW_S:
            return self.mean(a, b)
        span = b - a - BASE_WINDOW_S
        stride = max(BASE_STRIDE_S, span / (MAX_SLIDES - 1))
        vals = [x for x in (self.mean(a + k * stride, a + k * stride + BASE_WINDOW_S)
                            for k in range(int(math.floor(span / stride + 1e-9)) + 1)) if x is not None]
        if not vals:
            return None
        return max(vals) if direction > 0 else min(vals)


class _Sparse(_Series):
    """One process's readings from the top-8 `others` table: only the ticks it was listed on are stored, and
    a tick it was not listed on counts as 0 (it was below the 8th), so memory grows with the table, never
    with processes x ticks."""

    def __init__(self, ticks: list[float], pad: float, t: list[float], v: list[float]) -> None:
        self.ticks, self.pad, self.t, self.v = ticks, pad, t, v
        self.measured = bool(t)

    def mean(self, a: float, b: float) -> float | None:
        n = bisect.bisect_right(self.ticks, b + self.pad) - bisect.bisect_left(self.ticks, a - self.pad)
        if n <= 0:
            return None
        lo, hi = bisect.bisect_left(self.t, a - self.pad), bisect.bisect_right(self.t, b + self.pad)
        return math.fsum(self.v[lo:hi]) / n


def _system(cap: Capture, column: str, f: Callable[[float], float] | None = None) -> _Series:
    t, v = fa._col(cap.system, S.T), fa._col(cap.system, column)
    ts, vs = [], []
    for i, ti in enumerate(t):
        ti = _num(ti)
        if ti is None:
            continue
        x = _num(fa._at(v, i))
        ts.append(float(ti))
        vs.append(None if x is None else (f(float(x)) if f else float(x)))
    return _Series(ts, vs)


def _gpu(cap: Capture, column: str) -> _Series:
    t, v = fa._col(cap.gpu, G.T), fa._col(cap.gpu, column)
    ts, vs = [], []
    for i, ti in enumerate(t):
        ti = _num(ti)
        if ti is not None:
            x = _num(fa._at(v, i))
            ts.append(float(ti))
            vs.append(None if x is None else float(x))
    return _Series(ts, vs)


@dataclass
class _Others:
    """The `others` table: top non-game processes per tick ([pid, name, cpu_pct, io_bps, gpu_pct])."""

    total: _Series        # sum of the listed processes' CPU
    top: _Series          # the busiest one's CPU
    io_top: _Series       # the biggest I/O user's bytes/s
    per: dict[tuple[int, str], dict[str, _Series]]     # (pid, name) -> {"cpu", "io", "gpu"}; 0 when not listed
    by_name: dict[str, dict[str, _Series]]             # the same, summed over every process of one exe name


def _others(cap: Capture) -> _Others:
    rows = []
    for row in cap.others if isinstance(cap.others, list) else []:
        if isinstance(row, (list, tuple)) and len(row) == 2 and _num(row[0]) is not None \
                and isinstance(row[1], (list, tuple)):
            rows.append((float(row[0]), row[1]))
    rows.sort(key=lambda r: r[0])
    t = [r[0] for r in rows]
    total, top, io_top = [], [], []
    seen: dict[tuple[int, str], dict[str, dict[int, float]]] = {}       # tick index -> value, listed ticks only
    for k, (_ti, entries) in enumerate(rows):
        cpus, ios = [], []
        for e in entries:
            if not (isinstance(e, (list, tuple)) and len(e) >= 5):
                continue
            pid, name = e[0], e[1]
            if not isinstance(pid, int) or isinstance(pid, bool) or not isinstance(name, str):
                continue
            cpu, io, gpu = (_num(x) for x in e[2:5])
            cols = seen.setdefault((pid, name), {"cpu": {}, "io": {}, "gpu": {}})
            for key, x in (("cpu", cpu), ("io", io), ("gpu", gpu)):
                cols[key][k] = float(x) if x is not None and x >= 0 else 0.0
            if cpu is not None and cpu >= 0:
                cpus.append(float(cpu))
            if io is not None and io >= 0:
                ios.append(float(io))
        total.append(math.fsum(cpus) if cpus else None)
        top.append(max(cpus) if cpus else None)
        io_top.append(max(ios) if ios else None)
    total_s = _Series(t, total)
    sums: dict[str, dict[str, dict[int, float]]] = {}
    for (_pid, name), cols in seen.items():
        acc = sums.setdefault(name, {"cpu": {}, "io": {}, "gpu": {}})
        for c, vals in cols.items():
            for k, x in vals.items():
                acc[c][k] = acc[c].get(k, 0.0) + x

    def sparse(vals: dict[int, float]) -> _Sparse:
        ks = sorted(vals)
        return _Sparse(t, total_s.pad, [t[k] for k in ks], [vals[k] for k in ks])

    per = {key: {c: sparse(vals) for c, vals in cols.items()} for key, cols in seen.items()}
    by_name = {name: {c: sparse(vals) for c, vals in cols.items()} for name, cols in sums.items()}
    return _Others(total_s, _Series(t, top), _Series(t, io_top), per, by_name)


# --------------------------------------------------------------------------- checks ---

@dataclass
class _Part:
    """One way a cause can show: a series moving up (+1) or down (-1) by at least a floor."""

    series: _Series
    direction: int
    floor: Callable[[float], float]          # baseline median -> minimum change (signal units)
    label: str                               # plain English, "{v}" and "{n}" are filled in
    fmt: Callable[[float], str]
    level: Callable[[float], bool] | None = None   # an extra condition on the level itself


@dataclass
class _Hit:
    cause: str
    z: float
    value: float
    normal: float
    text: str


class _Baseline:
    def __init__(self, med: float, mad: float) -> None:
        self.med, self.scale = med, fa.MAD_TO_SD * mad


def _baseline(series: _Series, windows: list[tuple[float, float]]) -> _Baseline | None:
    xs = [x for x in (series.mean(a, b) for a, b in windows) if x is not None]
    if len(xs) < MIN_BASE_WINDOWS:
        return None
    return _Baseline(st.median(xs), st.mad(xs))


def _judge(part: _Part, base: _Baseline, x: float) -> tuple[bool, float]:
    change = (x - base.med) * part.direction
    if base.scale > 0:
        z = change / base.scale
    else:
        z = math.inf if change > 0 else 0.0
    ok = z >= Z_MIN and change >= part.floor(base.med) and (part.level is None or part.level(x))
    return ok, z


@dataclass
class _Cause:
    name: str
    parts: list[_Part]
    condition: Callable[[float, float], bool | None] | None = None   # (a, b) -> True / False / None unknown
    why_missing: str = ""


def _pct(v: float) -> str:
    return f"{v:.0f}%"


def _mbps(v: float) -> str:
    return f"{v / 1e6:.1f} MB/s"


def _causes(cap: Capture, others: _Others) -> list[_Cause]:
    inv = cap.inventory if isinstance(cap.inventory, dict) else {}
    thread = _system(cap, S.GAME_TOP_THREAD_PCT)
    io = _system(cap, S.GAME_IO_READ_BPS)
    disk_busy = _system(cap, S.DISK_BUSY_PCT)
    disk_ms = _system(cap, S.DISK_READ_MS)
    faults = _system(cap, S.HARD_FAULTS_PS)
    ram = _system(cap, S.RAM_AVAIL_MB)
    nlp = fa._logical_cpus(cap)
    total = _system(cap, S.CPU_TOTAL_PCT)
    game = _system(cap, S.GAME_CPU_PCT)
    machine_others = _Series(total.t, [None if x is None or g is None or not nlp else max(0.0, x * nlp - g)
                                       for x, g in zip(total.v, _align(game, total.t))])
    gpu_other = _system(cap, S.GPU_OTHER_PCT)
    dpc, irq = _system(cap, S.DPC_PCT), _system(cap, S.INTERRUPT_PCT)
    perf = _system(cap, S.CPU_PERF_PCT)
    limited = _system(cap, S.CPU_PERF_LIMIT_PCT, lambda x: 1.0 if x < 100.0 else 0.0)
    throttle = _gpu(cap, G.THROTTLE_PCT)
    shared, vram = _system(cap, S.GAME_SHARED_MB), _system(cap, S.VRAM_USED_MB)
    unfocused = _system(cap, S.GAME_FOCUSED, lambda x: 1.0 if x == 0 else 0.0)

    ram_gb = _num(inv.get("ram_gb"))
    ram_floor = RAM_LOW_FRAC * ram_gb * 1024.0 if ram_gb is not None and ram_gb > 0 else RAM_LOW_MB
    vram_mb = None
    luid = (cap.target or {}).get("adapter_luid") if isinstance(cap.target, dict) else None
    for a in inv.get("adapters") or []:
        if isinstance(a, dict) and luid and a.get("luid") == luid and _num(a.get("vram_mb")):
            vram_mb = float(a["vram_mb"])

    def ram_low(a: float, b: float) -> bool | None:
        lo, hi = bisect.bisect_left(ram.t, a), bisect.bisect_right(ram.t, b)
        xs = [x for x in ram.v[lo:hi] if x is not None]
        return min(xs) < ram_floor if xs else None

    def vram_full(a: float, b: float) -> bool | None:
        lo, hi = bisect.bisect_left(vram.t, a), bisect.bisect_right(vram.t, b)
        xs = [x for x in vram.v[lo:hi] if x is not None]
        return max(xs) >= VRAM_FULL_FRAC * vram_mb if xs and vram_mb else None

    thread_floor = 100.0 * THREAD_EXTRA_MS / (1000.0 * BASE_WINDOW_S)
    return [
        _Cause("game_cpu_spike", [_Part(thread, +1, lambda m: thread_floor,
                                        "the game's busiest thread ran {v} of a CPU (normally {n})", _pct)],
               why_missing="no per-thread CPU for the game"),
        _Cause("asset_streaming", [
            _Part(io, +1, lambda m: IO_EXTRA_BPS, "the game read {v} from disk (normally {n})", _mbps),
            _Part(disk_busy, +1, lambda m: 0.0, "the disk was {v} busy (normally {n})", _pct,
                  level=lambda x: x >= DISK_BUSY_PCT),
            _Part(disk_ms, +1, lambda m: 0.0, "disk reads took {v} (normally {n})", lambda v: f"{v:.1f} ms",
                  level=lambda x: x >= DISK_READ_MS)],
               why_missing="no game I/O or disk counters"),
        _Cause("memory_paging", [_Part(faults, +1, lambda m: FAULTS_RISE_PS,
                                       "{v} hard page faults (normally {n}) with RAM nearly full",
                                       lambda v: f"{v:,.0f}/s")],
               condition=ram_low, why_missing="no page-fault or free-RAM counters"),
        _Cause("background_cpu", [
            _Part(machine_others, +1, lambda m: OTHERS_ALL_PCT, "other programs used {v} of a CPU (normally {n})",
                  _pct),
            _Part(others.total, +1, lambda m: OTHERS_ALL_PCT,
                  "the busiest other programs used {v} of a CPU (normally {n})", _pct),
            _Part(others.top, +1, lambda m: OTHERS_ONE_PCT, "one other program used {v} of a CPU (normally {n})",
                  _pct)],
               why_missing="no CPU readings for other processes"),
        _Cause("background_disk", [_Part(others.io_top, +1, lambda m: OTHERS_DISK_BPS,
                                         "another program did {v} of I/O (normally {n})", _mbps)],
               why_missing="no I/O readings for other processes"),
        _Cause("background_gpu", [_Part(gpu_other, +1, lambda m: OTHERS_GPU_PCT,
                                        "other programs kept the game's GPU {v} busy (normally {n})", _pct)],
               why_missing="no GPU engine counters for other processes"),
        _Cause("driver_dpc", [
            _Part(dpc, +1, lambda m: DPC_RISE_PCT, "driver DPC time reached {v} of the busiest CPU (normally {n})",
                  _pct),
            _Part(irq, +1, lambda m: IRQ_RISE_PCT, "interrupt time reached {v} of the busiest CPU (normally {n})",
                  _pct)],
               why_missing="no DPC or interrupt counters"),
        _Cause("cpu_throttle", [
            _Part(perf, -1, lambda m: PERF_DROP_FRAC * m, "the CPU ran at {v} of its base speed (normally {n})",
                  _pct),
            _Part(limited, +1, lambda m: LIMIT_SHARE,
                  "the CPU was held below full speed in {v} of samples (normally {n})",
                  lambda v: f"{100 * v:.0f}%")],
               why_missing="no CPU frequency or limit counters"),
        _Cause("gpu_throttle", [_Part(throttle, +1, lambda m: NV_THROTTLE_PCT,
                                      "the NVIDIA GPU was held down by heat or power {v} of the time (normally {n})",
                                      _pct)],
               why_missing="no NVIDIA throttle readings"),
        _Cause("vram_overflow", [_Part(shared, +1, lambda m: SHARED_RISE_MB_PER_S * BASE_WINDOW_S,
                                       "the game's spill into shared memory rose to {v} (normally {n}) with VRAM full",
                                       lambda v: f"{v:.0f} MB")],
               condition=vram_full, why_missing="no GPU memory counters or VRAM size"),
        _Cause("not_focused", [_Part(unfocused, +1, lambda m: UNFOCUSED_SHARE,
                                     "the game was not the foreground window for {v} of the moment (normally {n})",
                                     lambda v: f"{100 * v:.0f}%")],
               why_missing="foreground window not recorded"),
    ]


def _align(series: _Series, times: list[float]) -> list[float | None]:
    """series values at exactly these times (same sampler ticks), else None."""
    pos = {t: v for t, v in zip(series.t, series.v)}
    return [pos.get(t) for t in times]


# ----------------------------------------------------------------------------- side ---

def _frame_ratio(fr: dict[str, list], rows: list[int], margin: float) -> float | None:
    fm, gb = fr.get(F.FRAME_MS) or [], fr.get(F.GPU_BUSY_MS) or []
    rs = []
    for i in rows:
        f, g = _num(fa._at(fm, i)), _num(fa._at(gb, i))
        if f is not None and f > 0 and g is not None and g > 0:
            rs.append(max(0.0, g - margin) / f)
    return st.median(rs) if rs else None


def _side(fr: dict[str, list], ev: LagEvent, verdict: str | None, margin: float) -> tuple[str | None, str]:
    """('gpu' | 'cpu' | 'mixed' | None, a why-line) for the slow side of an event (LC 3.1 thresholds)."""
    t = fr.get(F.T) or []
    fm = fr.get(F.FRAME_MS) or []
    rows: list[int] = []
    if ev.kind == "stutter":
        # The slowest frame PRESENTED in the event, not the displayed one: composition drops a late frame
        # when the next one arrives before the same refresh, and that next frame is itself quick.
        lo, hi = bisect.bisect_left(t, ev.t_start), bisect.bisect_right(t, ev.t_end)
        cand = [i for i in range(lo, hi) if fa._pos(fa._at(fm, i)) and fa._is_app(fr, i)]
        if cand:
            slow = max(cand, key=lambda i: fm[i])
            # when no frame holds most of the lost time (a stall, or the screen stopped updating while the
            # game kept presenting), neither side of the game was slow: leave it unknown
            if fm[slow] >= STALL_COVER * float(ev.severity):
                rows = [slow]
    elif ev.kind == "fps_drop":
        lo, hi = bisect.bisect_left(t, ev.t_start), bisect.bisect_right(t, ev.t_end)
        rows = [i for i in range(lo, hi) if fa._is_app(fr, i)]
    r = _frame_ratio(fr, rows, margin)
    if r is None:
        fallback = {"gpu": "gpu", "cpu_main": "cpu", "cpu_all": "cpu", "waiting": "cpu"}.get(verdict or "")
        return fallback, ("no frame held the lost time, or it had no GPU timing" +
                          (f"; the game is {verdict}-bound overall" if fallback else ""))
    what = f"its slowest frame ({float(fm[rows[0]]):.0f} ms)" if ev.kind == "stutter" else "its frames"
    if r >= fa.R_GPU:
        return "gpu", f"the GPU was busy for {100 * r:.0f}% of {what}, so the GPU side was slow"
    if r <= fa.R_CPU:
        return "cpu", f"the GPU was busy for only {100 * r:.0f}% of {what}, so the CPU side was slow (or waited)"
    return "mixed", f"the GPU was busy for {100 * r:.0f}% of {what}: either side could be slow"


def _allowed(side: str | None) -> set[str]:
    if side == "gpu":
        return set(GPU_SIDE) | set(ANY_SIDE)
    if side == "cpu":
        return set(CPU_SIDE) | set(ANY_SIDE)
    return set(CPU_SIDE) | set(GPU_SIDE) | set(ANY_SIDE)


# --------------------------------------------------------------------------- driver ---

def _quiet_windows(start: float, end: float, spans: list[tuple[float, float]], guard: float) -> list[tuple[float, float]]:
    """1 s windows from start to end, one per tick (at most MAX_QUIET_WINDOWS), that keep `guard` seconds
    away from every span."""
    merged: list[tuple[float, float]] = []
    for a, b in sorted(spans):
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    starts = [a for a, _ in merged]
    stride = max(BASE_STRIDE_S, (end - start) / MAX_QUIET_WINDOWS)
    out = []
    k = 0
    while True:
        w = start + k * stride
        if w + BASE_WINDOW_S > end + 1e-9:
            break
        a, b = w - guard, w + BASE_WINDOW_S + guard
        j = bisect.bisect_right(starts, b) - 1
        if j < 0 or merged[j][1] < a:
            out.append((w, w + BASE_WINDOW_S))
        k += 1
    return out


def _evaluate(cause: _Cause, bases: dict[int, _Baseline | None], a: float, b: float) -> tuple[_Hit | None, bool]:
    """(the strongest active part, measured?) of a cause over [a, b]."""
    best: _Hit | None = None
    measured = False
    for k, part in enumerate(cause.parts):
        base = bases.get(k)
        if base is None:
            continue
        x = part.series.peak(a, b, part.direction)
        if x is None:
            continue
        measured = True
        ok, z = _judge(part, base, x)
        if ok and (best is None or z > best.z):
            best = _Hit(cause.name, z, x, base.med, part.label.format(v=part.fmt(x), n=part.fmt(base.med)))
    if best is not None and cause.condition is not None and cause.condition(a, b) is not True:
        best = None
    return best, measured


def attribute(cap: Capture, fr: dict[str, list], events: Iterable[LagEvent], bound: dict | None) -> dict[str, dict]:
    """Fill ev.causes (best first) and ev.detail["why"] for every stutter and FPS-drop event; return the
    per-cause summary {cause: {a, n, share, lift, label, b, weight, time_share, baseline_guard_s, why}}.

    label is LC 5.2's verdict: "supports", "contradicts", "neutral", "too_few_events" (n < 5) or
    "not_measured". share is the explained share: events whose FIRST cause this is, over n.
    When frames were not measured, the user's marks are attributed instead (LC 5.2), at half weight.
    """
    events = list(events)
    targets = [e for e in events if e.kind in ATTRIBUTED_KINDS]
    half = False
    if not targets and not (fr.get(F.T) or []):
        targets, half = [e for e in events if e.kind == "mark"], True
    if not targets:
        return {}
    verdict = (bound or {}).get("verdict") if isinstance(bound, dict) else None
    margin = fa.HAGS_MARGIN_MS if fa.hags_on(cap) else 0.0
    others = _others(cap)
    causes = _causes(cap, others)
    thread = _system(cap, S.GAME_TOP_THREAD_PCT)

    # quiet 1 s windows: no attributed event (or mark) within the guard distance
    spans = sorted((e.t_start, e.t_end) for e in events if e.kind in ATTRIBUTED_KINDS + ("mark",))
    times = [x for c in causes for p in c.parts for x in p.series.t]
    start, end = (min(times), max(times)) if times else (0.0, 0.0)
    guard, windows = QUIET_GUARD_S, []
    while True:
        windows = _quiet_windows(start, end, spans, guard)
        if len(windows) >= MIN_BASE_WINDOWS or guard <= QUIET_GUARD_MIN_S:
            break
        guard = max(QUIET_GUARD_MIN_S, guard / 2.0)

    bases = {c.name: {k: _baseline(p.series, windows) for k, p in enumerate(c.parts)} for c in causes}
    thread_base = _baseline(thread, windows)

    # 1 + 2: per-event activity
    acts: list[tuple[LagEvent, str | None, str, dict[str, _Hit], set[str]]] = []
    for ev in targets:
        side, side_line = _side(fr, ev, verdict, margin) if not half else (None, "a moment you marked")
        a, b = ev.t_start - WINDOW_BEFORE_S, ev.t_end + WINDOW_AFTER_S
        allowed = _allowed(side)
        hits: dict[str, _Hit] = {}
        measured: set[str] = set()
        for c in causes:
            hit, was = _evaluate(c, bases[c.name], a, b)
            if was:
                measured.add(c.name)
            if hit is not None and c.name in allowed:
                hits[c.name] = hit
        acts.append((ev, side, side_line, hits, measured))

    # 3: lift test
    n = len(targets)
    summary: dict[str, dict] = {}
    weight: dict[str, float] = {}
    for c in causes:
        a_cnt = sum(1 for _, _, _, hits, _ in acts if c.name in hits)
        active_b = [bool(_evaluate(c, bases[c.name], wa, wb)[0]) for wa, wb in windows]
        b = sum(active_b) / len(active_b) if active_b and any(v is not None for v in bases[c.name].values()) else None
        lift = (a_cnt / n) / max(b, LIFT_B_FLOOR) if b is not None else None
        ever = any(c.name in m for *_, m in acts)
        label = _label(a_cnt, n, lift, measured=ever)
        w = (3.0 if lift is not None and lift >= LIFT_STRONG else 2.0) if label == "supports" else 1.0
        if c.name in CAPPED_AT_POSSIBLE:
            w = min(w, 1.0)
        weight[c.name] = w / 2.0 if half else w
        summary[c.name] = {"a": a_cnt, "n": n, "share": 0.0, "lift": lift, "label": label, "b": b,
                           "weight": weight[c.name], "time_share": 0.0, "baseline_guard_s": guard,
                           "why": "" if ever else c.why_missing}
        if c.name in CAPPED_AT_POSSIBLE:
            summary[c.name]["cap"] = "possible"

    # 4: explaining away, then the residual when nothing moved
    first_counts: dict[str, int] = {}
    first_time: dict[str, float] = {}
    residual_counts = {"gpu_spike": 0, "game_stall": 0, "game_cpu_spike": 0}
    blame_cache: dict = {}
    for ev, side, side_line, hits, measured in acts:
        ranked = sorted(hits.values(), key=lambda h: (-weight[h.cause] * min(h.z, Z_CAP) / Z_MIN,
                                                      CAUSE_ORDER.index(h.cause)))
        why = [side_line] + [h.text for h in ranked]
        signals = {h.cause: {"value": h.value, "normal": h.normal, "z": None if math.isinf(h.z) else h.z}
                   for h in ranked}
        names = [h.cause for h in ranked]
        if not names and half:
            why.append("nothing measured moved at this moment")
        elif not names:
            res, line = _residual(side, thread, thread_base, ev, measured)
            if res:
                names = [res]
                residual_counts[res] += 1
            why.append(line)
        for h in ranked:
            proc = _blame_process(h.cause, others, windows, ev, blame_cache)
            if proc:
                ev.detail["process"], ev.detail["process_pids"] = proc[1], proc[0]
                why.append(f"the program that rose the most: {proc[2]}")
                break
        ev.causes = names
        ev.detail["why"] = why
        ev.detail["side"] = side
        ev.detail["signals"] = signals
        if names:
            first_counts[names[0]] = first_counts.get(names[0], 0) + 1
            first_time[names[0]] = first_time.get(names[0], 0.0) + max(0.0, ev.t_end - ev.t_start)

    total_time = math.fsum(max(0.0, e.t_end - e.t_start) for e in targets) or 1.0
    for name, cnt in residual_counts.items():
        if name == "game_cpu_spike":          # `a` stays the signal's count; this is "thread saturated" only
            summary[name]["residual"] = cnt
            continue
        summary[name] = {"a": cnt, "n": n, "share": 0.0, "lift": None, "label": _label(cnt, n, None, True),
                         "b": None, "weight": 1.0, "time_share": 0.0, "baseline_guard_s": guard,
                         "why": "nothing else moved", "residual": cnt}
    for name, entry in summary.items():
        entry["share"] = first_counts.get(name, 0) / n
        entry["time_share"] = first_time.get(name, 0.0) / total_time
        if half:
            entry["half_weight"] = True
    return summary


def _label(a: int, n: int, lift: float | None, measured: bool) -> str:
    if not measured:
        return "not_measured"
    if n < LIFT_MIN_EVENTS:
        return "too_few_events"
    if a >= LIFT_MIN_A and a / n >= LIFT_MIN_SHARE and (lift is None or lift >= LIFT_MIN):
        return "supports"
    if n >= CONTRA_MIN_EVENTS and a / n <= CONTRA_MAX_SHARE:
        return "contradicts"
    return "neutral"


def _residual(side: str | None, thread: _Series, base: _Baseline | None, ev: LagEvent,
              measured: set[str]) -> tuple[str | None, str]:
    """When no signal moved: the GPU's own work (GPU side), the game's own work (its thread was busy) or
    the game waiting on itself (thread idle, GPU idle). Never a guess when the system was not measured."""
    if side == "gpu" or side == "mixed":
        return "gpu_spike", "nothing else moved: the GPU's own work took long"
    if "game_cpu_spike" not in measured or base is None:
        return None, "the game's CPU use was not measured at this moment, so the cause is unknown"
    a, b = ev.t_start - WINDOW_BEFORE_S, ev.t_end + WINDOW_AFTER_S
    level = thread.peak(a, b, +1)
    if level is not None and level >= 100.0 * THREAD_BUSY:
        return "game_cpu_spike", (f"nothing else moved, and the game's busiest thread was saturated "
                                  f"({level:.0f}% of a CPU): the game's own work")
    shown = f"{level:.0f}%" if level is not None else "unknown"
    return "game_stall", (f"nothing else moved: the busiest game thread was not busy ({shown} of a CPU, "
                          f"normally {base.med:.0f}%) and the GPU was idle, so the game waited on itself")


def _blame_process(cause: str, others: _Others, windows: list[tuple[float, float]], ev: LagEvent,
                   cache: dict) -> tuple[list[int], str, str] | None:
    """The program (every process of one exe name together) whose CPU / I/O / GPU rose most during the
    event, for the background_* causes: (pids that rose, name, a why-line with numbers)."""
    col = {"background_cpu": "cpu", "background_disk": "io", "background_gpu": "gpu"}.get(cause)
    if col is None:
        return None

    def normal_of(key: Any, series: _Series) -> float:
        if key not in cache:
            base = _baseline(series, windows)
            cache[key] = base.med if base is not None else 0.0
        return cache[key]

    a, b = ev.t_start - WINDOW_BEFORE_S, ev.t_end + WINDOW_AFTER_S
    best = None
    for name, series in others.by_name.items():
        x = series[col].peak(a, b, +1)
        normal = normal_of(("name", name, col), series[col])
        if x is not None and x > normal and (best is None or x - normal > best[0]):
            best = (x - normal, name, x, normal)
    if best is None:
        return None
    _, name, x, normal = best
    rises = []
    for (pid, pname), series in others.per.items():
        if pname == name:
            rise = (series[col].peak(a, b, +1) or 0.0) - normal_of(("pid", pid, pname, col), series[col])
            if rise > 0:
                rises.append((rise, pid))
    rises.sort(reverse=True)
    fmt = _mbps if col == "io" else _pct
    unit = "" if col == "io" else (" of a CPU" if col == "cpu" else " of a GPU engine")
    many = f" ({len(rises)} processes)" if len(rises) > 1 else ""
    return [pid for _, pid in rises[:8]], name, f"{name}{many} reached {fmt(x)}{unit}, against {fmt(normal)} normally"
