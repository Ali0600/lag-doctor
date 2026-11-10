"""Frame analysis: pick the game's main swap chain, summarise its frame times, say what limits them.

Pure functions over a Capture (docs/DESIGN.md, "Analysis"). Every threshold is a module constant that
names the research note section it comes from: LC = docs/research/lag-causes.md, PM = presentmon.md.
LC tags these thresholds [inferred]: they are defaults to tune against LagLab, never silently.

A capture can come from disk (`lagdoctor analyze FILE`), so every value read here is checked: a value
that is not a finite number (or, for text columns, a string) counts as missing, and so does a time, a
millisecond or a percentage too large to be real (MAX_ABS_VALUE: a corrupt 1e308 would overflow a sum).
"""
from __future__ import annotations

import bisect
import math
from collections import Counter
from typing import Any, Iterable, Sequence

from lagdoctor.analysis import stats as st
from lagdoctor.model import F, S, Capture, LagEvent

# ---- frame-time statistics (LC 2.4, 2.5, 2.6) ----------------------------------------------------
MIN_FRAMES_1PCT = 1_000      # LC 2.6: a P99 or 1%-low figure needs at least 1,000 frames
MIN_FRAMES_01PCT = 10_000    # LC 2.6: a 0.1%-low figure needs at least 10,000 frames
RULE_OF_THREE_N = 30         # LC 2.6 (S45): 0 hitches in n > 30 frames -> rate <= 3/n at 95 %
HITCH_MS = 50.0              # LC 2.4 (S19): a hitch is D >= 50 ms ...
HITCH_X_BASELINE = 2.0       # LC 2.4: ... and D >= 2 x the trailing median M
SEVERE_MS = 150.0            # LC 2.4 (S19): severe hitch
FREEZE_MS = 500.0            # LC 2.4: freeze
BEYOND_MS = 50.0             # LC 2.4: time beyond 50 ms = sum of (D - 50) over D > 50
BASELINE_K = 10.0            # LC 2.4 (CapFrameX, S11): M over N = clamp(round(10 * sqrt(median_ms)), 20, 120)
BASELINE_MIN_FRAMES = 20     # LC 2.4: lower clamp of N
BASELINE_MAX_FRAMES = 120    # LC 2.4: upper clamp of N
# ---- stutters and stalls (LC 2.4; PM 2.4, PM 10 rule 7): one test for frame_stats and events -----
STUTTER_X = 2.5              # LC 2.4 (CapFrameX, S12): a stutter is D >= 2.5 x M ...
STUTTER_MIN_MS = 8.0         # LC 2.4 (S20): ... and D - M >= 8 ms (events.py: D - max(M, E) on a quantised display)
STALL_COVER = 0.5            # [inferred] a jump in t that the row's own frame time covers less than half of
                             # means rows are missing: a stall (PM 10 parser rule 7, PM 2.4)
# ---- value checks --------------------------------------------------------------------------------
MAX_ABS_VALUE = 1e7          # [inferred] seconds, ms and % past +-1e7 (115 days, a 2.8-hour frame, 100,000 CPUs)
                             # are corrupt; byte and count columns are not bounded (they can pass 1e7)

# ---- bound classifier (LC 3.1-3.4) ---------------------------------------------------------------
R_GPU = 0.90                 # LC 3.1: r = GPU busy / frame time >= 0.90 -> GPU-bound ...
GPU_GAP_MS = 1.0             # LC 3.1: ... or FT - G <= 1.0 ms
R_CPU = 0.75                 # LC 3.1: r <= 0.75 -> the CPU or something else; between -> "mixed"
HAGS_MARGIN_MS = 0.5         # LC 1.1 + 3.1 (S7): GPU busy reads ~0.5 ms high with HAGS on
THREAD_BOUND = 0.85          # LC 3.2: busiest thread >= 0.85 of a logical CPU -> main-thread bound
ALL_CORES_BOUND = 0.85       # LC 3.2: game CPU >= 0.85 x logical CPUs -> all-core bound
THREAD_WINDOW_S = 1.0        # LC 0.5, 3.2 (WT 3.5): judge thread load over windows of >= 1 s
TICK_PAD_S = 0.25            # one 4 Hz sampler tick (DESIGN, system): ticks this near an event are not steady
VSYNC_BAND_MS = 0.25         # LC 3.3: a displayed interval is "on" k*P within +-max(0.25 ms, 4 % of P)
VSYNC_BAND_FRAC = 0.04       # LC 3.3
VSYNC_SHARE = 0.90           # LC 3.3: >= 90 % of displayed intervals on k*P -> quantised
CAP_COV = 0.03               # LC 3.3: frame cap = MsBetweenPresents CoV < 3 % ...
CAP_WINDOW_S = 5.0           # LC 3.3: ... over 5 s ...
CAP_REFRESH_FRAC = 0.03      # LC 3.3: ... at a value C not within 3 % of k*P
CAP_WINDOW_MIN_FRAMES = 20   # [inferred] a 5 s window with fewer frames (< 4 FPS) says nothing about a cap
BOUND_MIN_S = 10.0           # LC 2.6: a bound verdict needs >= 10 s ...
BOUND_MIN_FRAMES = 300       # LC 2.6: ... and >= 300 app frames of steady state
ENGINE_GPU_PCT = 95.0        # LC 3.4: frames not measured -> 3D engine >= 95 % = "GPU-limited (probable)"
PROBABLE_GPU_SHARE = 0.10    # [inferred, the 10 % share the focus and pacing rules use] without frame timing, the GPU
                             # probably limits the game for part of the capture when the engine is at ENGINE_GPU_PCT in
                             # this share of its 1 s windows (and in BOUND_MIN_S of them) ...
PROBABLE_GPU_MARKS = 0.5     # [inferred] ... or at half of the moments the user marked, or more
CONF_MARGIN = 0.05           # [inferred] a ratio this close to a threshold makes the verdict "medium"
SHARED_GPU_PCT = 10.0        # LC 4.6, 10 row 4.6: other programs on the game adapter's engine >= 10 % share it;
                             # the game's own GPU busy then reads short (PM 3.2), whatever holds its frame rate
MAD_TO_SD = 1.4826           # MAD x 1.4826 estimates the standard deviation of normal data
IQR_TO_SD = 1.349            # the interquartile range of normal data is 1.349 standard deviations
# ---- headroom: how far each side is from its own limit (DESIGN, Headroom) -------------------------
STEP_MIN_FPS = 20.0          # [inferred] a quantised screen's even steps (refresh / k) under 20 FPS are no one's target

FRAME_EVENT_KINDS = ("stutter", "fps_drop")   # what "steady" excludes (DESIGN, bound)

FRAME_STAT_KEYS = ("frames", "duration_s", "avg_fps", "low1_fps", "low01_fps", "p1_fps", "median_frame_ms",
                   "p99_frame_ms", "worst_frame_ms", "hitches", "severe_hitches", "freezes", "time_beyond_50_ms",
                   "hitch_rate_bound", "dropped_pct", "present_mode", "runtime", "frames_source")

_TEXT_COLS = frozenset({F.PRESENT_MODE, F.RUNTIME, F.SWAP_CHAIN, F.FRAME_TYPE})


# ---------------------------------------------------------------- value checks ---

def _num(v: Any) -> float | int | None:
    """A finite number, else None. Bools count as 1/0; strings, NaN and infinities are missing."""
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    if isinstance(v, float) and math.isfinite(v):
        return v
    return None


def _bounded(v: Any) -> float | int | None:
    """_num for a time (s or ms) or a percentage: past +-MAX_ABS_VALUE the value is corrupt, so missing."""
    n = _num(v)
    return n if n is not None and abs(n) <= MAX_ABS_VALUE else None


def _num_col(name: str, v: Any) -> float | int | None:
    """A value of the F/S/N column `name`: times ("t"), "_ms" and "_pct" columns bounded, others only finite."""
    return _bounded(v) if name == "t" or name.endswith(("_ms", "_pct")) else _num(v)


def _pos(v: Any) -> bool:
    """A positive time in ms (every caller passes one), bounded like _bounded."""
    n = _bounded(v)
    return n is not None and n > 0


def _at(col: Sequence | None, i: int) -> Any:
    return col[i] if col is not None and 0 <= i < len(col) else None


def _clean(name: str, v: Any) -> Any:
    if name in _TEXT_COLS:
        return v if isinstance(v, str) else None
    return _num_col(name, v)


def _chain_key(v: Any) -> str | None:
    if isinstance(v, str):
        return v
    if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
        return f"0x{v:X}"      # PresentMon's own spelling (PM 2.1)
    return None


def _col(table: Any, name: str) -> list:
    if not isinstance(table, dict):
        return []
    col = table.get(name)
    return col if isinstance(col, list) else []


# ------------------------------------------------------------------ main chain ---

def main_chain(cap: Capture) -> dict[str, list]:
    """F columns of the swap chain with the most displayed frames (PM 8.3, parser rule 6).

    A game can have a few-row `0x0`/`Other` key next to its real chain, so rows are never the measure.
    Ties go to the chain with more rows, then to the one seen first. Rows are returned in time order
    with every value checked (junk becomes None).
    """
    t = _col(cap.frames, F.T)
    chains = _col(cap.frames, F.SWAP_CHAIN)
    shown_col = _col(cap.frames, F.DISPLAYED)
    rows: dict[str | None, list[int]] = {}
    shown: dict[str | None, int] = {}
    for i, ti in enumerate(t):
        if _bounded(ti) is None:
            continue
        key = _chain_key(_at(chains, i))
        rows.setdefault(key, []).append(i)
        if _num(_at(shown_col, i)) == 1:
            shown[key] = shown.get(key, 0) + 1
    if not rows:
        return {c: [] for c in F.ALL}
    best = max(rows, key=lambda k: (shown.get(k, 0), len(rows[k])))   # max() keeps the first of equals
    pick = sorted(rows[best], key=lambda i: t[i])
    out: dict[str, list] = {}
    for name in F.ALL:
        col = _col(cap.frames, name)
        out[name] = [_clean(name, _at(col, i)) for i in pick]
    return out


def display_series(fr: dict[str, list]) -> tuple[list[int], list[float], list[float], str]:
    """(row indexes, times, frame times D, source) of the series stutters are judged on (LC 2.1, 2.4).

    D = MsBetweenDisplayChange over displayed frames ("display"); when display tracking is missing
    altogether, MsBetweenPresents over every frame ("present").
    """
    t = fr.get(F.T) or []
    disp = fr.get(F.DISPLAY_MS) or []
    shown = fr.get(F.DISPLAYED) or []
    rows = [i for i in range(len(t)) if _bounded(t[i]) is not None and _pos(_at(disp, i)) and _at(shown, i) != 0]
    if rows:
        return rows, [float(t[i]) for i in rows], [float(disp[i]) for i in rows], "display"
    fm = fr.get(F.FRAME_MS) or []
    rows = [i for i in range(len(t)) if _bounded(t[i]) is not None and _pos(_at(fm, i))]
    return rows, [float(t[i]) for i in rows], [float(fm[i]) for i in rows], "present"


def baseline_frames(median_ms: float) -> int:
    """N, the number of earlier frames the stutter baseline M is the median of (LC 2.4)."""
    n = round(BASELINE_K * math.sqrt(max(0.0, median_ms)))
    return int(min(BASELINE_MAX_FRAMES, max(BASELINE_MIN_FRAMES, n)))


def baseline(d: list[float]) -> list[float | None]:
    """M for each frame of a display series: the median of the N frames before it, N sized by the series'
    own median (LC 2.4). frame_stats and events both call this, so they judge every frame alike."""
    return st.trailing_median(d, baseline_frames(st.median(d))) if d else []


def first_t(fr: dict[str, list]) -> float | None:
    """The time of the chain's first row: no event starts before it (nothing was measured earlier)."""
    return min((float(x) for x in fr.get(F.T) or [] if _bounded(x) is not None), default=None)


def stall_gaps(fr: dict[str, list]) -> list[tuple[float, float, float, float]]:
    """(t before, t after, gap ms, baseline M) of each stall: a jump in t between consecutive rows of the
    chain, as big as a stutter (gap >= 2.5 M and gap - M >= 8 ms), that the later row's own frame time
    covers less than half of, so rows are missing (PM 2.4: a chain stalled > 4 s is pruned; PM 10 rule 7).

    The one stall test: events turns each into a stutter event and frame_stats folds it into its series (A4).
    """
    _, t, d, _ = display_series(fr)
    if len(d) < 2:
        return []
    m = baseline(d)
    tail = st.median(d[-baseline_frames(st.median(d)):])     # the baseline after the last displayed frame
    tt, fm = fr.get(F.T) or [], fr.get(F.FRAME_MS) or []
    out = []
    for i in range(1, len(tt)):
        a, b = _bounded(tt[i - 1]), _bounded(tt[i])
        if a is None or b is None:
            continue
        gap_ms = (b - a) * 1000.0
        k = bisect.bisect_left(t, b)
        mk = m[k] if k < len(m) else tail
        if mk is None or gap_ms < STUTTER_X * mk or gap_ms - mk < STUTTER_MIN_MS:
            continue
        own = _bounded(_at(fm, i))
        if own is not None and own >= STALL_COVER * gap_ms:
            continue      # a long frame of its own: the display series judges it
        out.append((float(a), float(b), gap_ms, float(mk)))
    return out


def _fold_stalls(t: list[float], d: list[float], gaps: list[tuple[float, float, float, float]]) -> list[float]:
    """The display series with each stall as one interval (A4). The stall replaces the interval of the first
    displayed frame after it, whose own interval lies inside it; stalls with no displayed frame between them
    are one frozen screen. A stall after the last displayed frame ends the series."""
    span: dict[int, tuple[float, float]] = {}
    for a, b, _gap, _base in gaps:
        k = bisect.bisect_left(t, b)
        lo, hi = span.get(k, (a, b))
        span[k] = (min(lo, a), max(hi, b))
    out = list(d)
    for k in sorted(span):
        ms = (span[k][1] - span[k][0]) * 1000.0
        if k < len(out):
            out[k] = max(out[k], ms)
        else:
            out.append(ms)
    return out


def _is_app(fr: dict[str, list], i: int) -> bool:
    ft = _at(fr.get(F.FRAME_TYPE), i)
    return ft is None or ft == "Application"      # PM 2.1: generated frames name their generator


# ------------------------------------------------------------------- statistics ---

def _low_fps(d: list[float], per: int) -> float:
    """FPS of the mean of exactly ceil(n / per) slowest frames (LC 2.4, 2.5: never 'all frames >= the
    quantile', which sweeps in every tie). per=100 is the 1% low, per=1000 the 0.1% low. One implementation
    with stats.low_fps_avg, so the two can never disagree."""
    return st.low_fps_avg(d, 100.0 / per)


def frame_stats(fr: dict[str, list], duration_s: float) -> dict[str, Any]:
    """The frame keys of Diagnosis.stats (DESIGN). Missing or None = not measured.

    hitches / severe_hitches / freezes count frames, and the classes nest: a frame is a hitch when
    D >= 50 ms and D >= 2 M (LC 2.4); a hitch of >= 150 ms is also severe, one of >= 500 ms also a freeze.
    Each stall (rows missing, stall_gaps) counts as one interval of the series, so a freeze that shows only
    as a jump in t is in the FPS, the lows, the worst frame and the hitch counts, and no rule-of-three bound
    is claimed over a capture with frames missing (A4).
    """
    out: dict[str, Any] = dict.fromkeys(FRAME_STAT_KEYS)
    rows, t, d, source = display_series(fr)
    n = len(d)
    out["frames"] = n
    dur = _bounded(duration_s)
    out["duration_s"] = float(dur) if dur is not None and dur >= 0 else None

    shown = [_num(v) for v in fr.get(F.DISPLAYED) or []]
    known = [v for v in shown if v in (0, 1)]
    if known:
        out["dropped_pct"] = 100.0 * sum(1 for v in known if v == 0) / len(known)
    for key, col in (("present_mode", F.PRESENT_MODE), ("runtime", F.RUNTIME)):
        vals = Counter(v for v in fr.get(col) or [] if isinstance(v, str))
        out[key] = vals.most_common(1)[0][0] if vals else None
    if n == 0:
        return out

    out["frames_source"] = source
    gaps = stall_gaps(fr)
    series = _fold_stalls(t, d, gaps)
    med = st.median(series)
    out["avg_fps"] = st.avg_fps(series)
    out["median_frame_ms"] = med
    out["worst_frame_ms"] = max(series)
    if n >= MIN_FRAMES_1PCT:
        p99 = st.percentile(series, 99.0)
        out["p99_frame_ms"] = p99
        out["p1_fps"] = 1000.0 / p99            # a percentile: labelled separately from the 1% low
        out["low1_fps"] = _low_fps(series, 100)
    if n >= MIN_FRAMES_01PCT:
        out["low01_fps"] = _low_fps(series, 1000)

    m = st.trailing_median(series, baseline_frames(med))
    hitches = severe = freezes = 0
    for di, mi in zip(series, m):
        if mi is None or di < HITCH_MS or di < HITCH_X_BASELINE * mi:
            continue
        hitches += 1
        severe += di >= SEVERE_MS
        freezes += di >= FREEZE_MS
    out["hitches"], out["severe_hitches"], out["freezes"] = hitches, severe, freezes
    out["time_beyond_50_ms"] = math.fsum(di - BEYOND_MS for di in series if di > BEYOND_MS)
    if hitches == 0 and not gaps and n > RULE_OF_THREE_N:
        out["hitch_rate_bound"] = 3.0 / n       # rows missing: no bound can be claimed over what was not seen
    return out


# --------------------------------------------------------------- capture facts ---

def game_display(cap: Capture) -> dict | None:
    """The display the game is on: by the target's monitor device, else the primary, else the only one."""
    inv = cap.inventory if isinstance(cap.inventory, dict) else {}
    displays = [x for x in inv.get("displays") or [] if isinstance(x, dict)]
    target = cap.target if isinstance(cap.target, dict) else {}
    game = inv.get("game") if isinstance(inv.get("game"), dict) else {}
    want = target.get("monitor_device") or game.get("monitor_device")
    if want:
        for x in displays:
            if x.get("device") == want:
                return x
    for x in displays:
        if x.get("primary") is True:
            return x
    return displays[0] if len(displays) == 1 else None


def refresh_hz(cap: Capture) -> float | None:
    """The game display's refresh rate from QueryDisplayConfig's rational (LC 3.3), e.g. 144.003."""
    disp = game_display(cap)
    hz = _num(disp.get("hz")) if disp else None
    return float(hz) if hz is not None and 1.0 <= hz <= 1000.0 else None


def hags_on(cap: Capture) -> bool | None:
    """Hardware GPU scheduling for the game's adapter: that adapter's own D3DKMT readout first, found by the
    LUID the session filled in after the capture; then the inventory's `settings.hags`, which falls back to a
    discrete adapter when the game's adapter was unknown at start (A12, inventory.hags_for)."""
    inv = cap.inventory if isinstance(cap.inventory, dict) else {}
    settings = inv.get("settings") if isinstance(inv.get("settings"), dict) else {}
    luid = cap.target.get("adapter_luid") if isinstance(cap.target, dict) else None
    for a in inv.get("adapters") or []:
        if isinstance(a, dict) and luid and a.get("luid") == luid and isinstance(a.get("hags"), bool):
            return a["hags"]
    return settings["hags"] if isinstance(settings.get("hags"), bool) else None


def _logical_cpus(cap: Capture) -> int | None:
    inv = cap.inventory if isinstance(cap.inventory, dict) else {}
    cpu = inv.get("cpu") if isinstance(inv.get("cpu"), dict) else {}
    n = _num(cpu.get("threads"))
    return int(n) if n is not None and n >= 1 else None


# ------------------------------------------------------------------ time spans ---

def _merged_spans(events: Iterable[LagEvent], kinds: Sequence[str], pad: float = 0.0) -> list[tuple[float, float]]:
    spans = sorted((float(e.t_start) - pad, float(e.t_end) + pad) for e in events
                   if e.kind in kinds and _num(e.t_start) is not None and _num(e.t_end) is not None)
    out: list[tuple[float, float]] = []
    for a, b in spans:
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _in_spans(x: float, spans: list[tuple[float, float]], starts: list[float]) -> bool:
    i = bisect.bisect_right(starts, x) - 1
    return i >= 0 and x <= spans[i][1]


def _steady_rows(fr: dict[str, list], spans: list[tuple[float, float]]) -> list[int]:
    starts = [a for a, _ in spans]
    t = fr.get(F.T) or []
    return [i for i in range(len(t)) if _bounded(t[i]) is not None and _is_app(fr, i)
            and not _in_spans(float(t[i]), spans, starts)]


def _window_bins(cap: Capture, column: str, spans: list[tuple[float, float]]) -> dict[int, float]:
    """{1 s window index: mean} of a 4 Hz system column, skipping any window that touches an event."""
    t, v = _col(cap.system, S.T), _col(cap.system, column)
    starts = [a for a, _ in spans]
    bins: dict[int, list[float]] = {}
    tainted: set[int] = set()
    for ti, vi in zip(t, v):
        ti, vi = _bounded(ti), _num_col(column, vi)
        if ti is None:
            continue
        b = int(math.floor(ti / THREAD_WINDOW_S))
        if _in_spans(float(ti), spans, starts):
            tainted.add(b)
        elif vi is not None:
            bins.setdefault(b, []).append(float(vi))
    return {b: math.fsum(xs) / len(xs) for b, xs in bins.items() if b not in tainted and len(xs) >= 3}


def busiest_column(cap: Capture) -> str:
    """The column the busiest thread is judged on: the lead thread's (S.GAME_LEAD_THREAD_PCT), where the capture has
    it. The per-tick busiest thread reads high whenever several threads are about as busy (four at 75 % read 86 %,
    over THREAD_BOUND), so a capture saved before the lead column gives only an upper bound."""
    lead = _col(cap.system, S.GAME_LEAD_THREAD_PCT)
    return S.GAME_LEAD_THREAD_PCT if any(_num(v) is not None for v in lead) else S.GAME_TOP_THREAD_PCT


def _window_means(cap: Capture, column: str, spans: list[tuple[float, float]]) -> list[float]:
    """Means over 1 s windows of a 4 Hz system column, skipping any window that touches an event."""
    return list(_window_bins(cap, column, spans).values())


def probable_gpu(cap: Capture, events: Iterable[LagEvent], tick_spans: list[tuple[float, float]]) -> dict[str, Any]:
    """Item 1: LC 3.4's no-frames fallback taken per second, not over the whole capture. The 1 s windows in which
    the game kept its 3D engine ENGINE_GPU_PCT or more busy, when the GPU was probably the limit, and the marks
    at which it did (any 1 s window their span touches). A game that idles in menus and loading screens has a low
    median while the GPU limits it in play: the first real capture's median was 71 %, yet 98-99 % through its
    last three minutes and at all three marks. `holds`: enough of the capture (PROBABLE_GPU_SHARE and BOUND_MIN_S)
    or of the marks (PROBABLE_GPU_MARKS) for a GPU-side cause to matter there."""
    bins = _window_bins(cap, S.GAME_GPU_PCT, tick_spans)
    busy = {b for b, m in bins.items() if m >= ENGINE_GPU_PCT}
    marks = [e for e in events if e.kind == "mark" and _bounded(e.t_start) is not None and _bounded(e.t_end) is not None]
    at_marks = sum(1 for e in marks if any(b in busy for b in range(int(math.floor(e.t_start / THREAD_WINDOW_S)),
                                                                    int(math.floor(e.t_end / THREAD_WINDOW_S)) + 1)))
    share = len(busy) / len(bins) if bins else 0.0
    holds = ((len(busy) * THREAD_WINDOW_S >= BOUND_MIN_S and share >= PROBABLE_GPU_SHARE)
             or (bool(marks) and at_marks >= PROBABLE_GPU_MARKS * len(marks)))
    return {"seconds": len(busy) * THREAD_WINDOW_S, "share": share, "windows": len(bins), "marks": len(marks),
            "marks_busy": at_marks, "holds": holds}


def gpu_others_median(cap: Capture, tick_spans: list[tuple[float, float]]) -> float | None:
    """The median share other programs kept the game adapter's engine busy (S.GPU_OTHER_PCT, 1 s windows that
    touch no lag event); None when it was not measured."""
    vals = _window_means(cap, S.GPU_OTHER_PCT, tick_spans)
    return st.median(vals) if vals else None


def gpu_shared(cap: Capture, tick_spans: list[tuple[float, float]]) -> float | None:
    """A7 (LC 4.6): gpu_others_median when it is SHARED_GPU_PCT or more, else None. The one predicate for the bound
    verdict below and rules' gpu_contention: a GPU shared this much paces a game as evenly as a frame-rate cap."""
    m = gpu_others_median(cap, tick_spans)
    return m if m is not None and m >= SHARED_GPU_PCT else None


# ----------------------------------------------------------------- the classifier ---

def _quantised_share(d: list[float], period: float) -> float | None:
    if not d or period <= 0:
        return None
    band = max(VSYNC_BAND_MS, VSYNC_BAND_FRAC * period)
    on = sum(1 for x in d if round(x / period) >= 1 and abs(x - round(x / period) * period) <= band)
    return on / len(d)


def _near_multiple(x: float, period: float, tol: float) -> bool:
    k = round(x / period)
    return k >= 1 and abs(x - k * period) <= tol


def _on_refresh_multiple(c: float, period: float) -> bool:
    """LC 3.3: is the cadence C within 3 % of some k * P?"""
    k = max(1, round(c / period))
    return abs(c - k * period) <= CAP_REFRESH_FRAC * k * period


def _robust_cov(xs: list[float]) -> float:
    """LC 3.3's coefficient of variation, estimated as (IQR / 1.349) / median.

    A stray late frame (below the stutter threshold, so still "steady") must not hide a frame cap: one
    19 ms wake-up in 1,200 frames of LagLab's real cap loop pushed a plain stdev / mean past 3 %. The MAD
    is no good either: on a two-value cadence (pace mode's 4 / 20 ms) with a one-frame majority it is 0.
    """
    med = st.median(xs)
    if med <= 0:
        return math.inf
    return (st.percentile(xs, 75.0) - st.percentile(xs, 25.0)) / IQR_TO_SD / med


def _cap_cadence(fr: dict[str, list], rows: list[int]) -> tuple[float | None, float | None]:
    """(median 5 s-window CoV of MsBetweenPresents, median frame ms C) over the given rows (LC 3.3)."""
    t, fm = fr.get(F.T) or [], fr.get(F.FRAME_MS) or []
    windows: dict[int, list[float]] = {}
    every: list[float] = []
    for i in rows:
        if _pos(_at(fm, i)):
            windows.setdefault(int(math.floor(float(t[i]) / CAP_WINDOW_S)), []).append(float(fm[i]))
            every.append(float(fm[i]))
    covs = [_robust_cov(xs) for xs in windows.values() if len(xs) >= CAP_WINDOW_MIN_FRAMES]
    if not covs or not every:
        return None, None
    return st.median(covs), st.median(every)


def bound(cap: Capture, fr: dict[str, list], events: Iterable[LagEvent]) -> dict[str, Any]:
    """What limits the frame rate (LC 3.4, in order), judged on steady frames only.

    Frames inside stutter or FPS-drop events, and sampler ticks within one tick of them, are left out.
    busiest_thread is a fraction of one logical CPU (0.85 = LC 3.2's main-thread limit); r is
    (median GPU busy - HAGS margin) / median app frame time; steady_fps is the frame rate of those steady
    frames (displayed ones where display is tracked). While other programs share the game's GPU (gpu_shared),
    a regular cadence is not taken for a cap, nor a short GPU busy for waiting: the verdict is "unknown" (A7).
    cpu_share is the game's CPU over every logical CPU (LC 3.2's all-core figure), and headroom how far the other
    sides are from their limits (headroom()).
    """
    events = list(events)
    spans = _merged_spans(events, FRAME_EVENT_KINDS)
    tick_spans = _merged_spans(events, FRAME_EVENT_KINDS, pad=TICK_PAD_S)
    out = _classify(cap, fr, _steady_rows(fr, spans), tick_spans)
    # item 1: without frame timing the whole-capture median is only a verdict; the seconds say where the GPU limited
    out["gpu_probable"] = (probable_gpu(cap, events, tick_spans) if not fr.get(F.T) and out["verdict"] == "unknown"
                           else None)
    return out


def _too_short_note(n: int, seconds: float) -> str:
    """What a steady stretch too short for a verdict fell short on, in words the rules quote to the player. Its seconds
    round down: a 10 s measured part sums 9.96-9.99 s of steady frames, which read "10.0 s" beside "needs 10 s"."""
    shown = math.floor(seconds * 10.0) / 10.0 if seconds < BOUND_MIN_S else seconds     # under 10 s: 9.9 at most
    frames = f"{n} steady frame{'' if n == 1 else 's'}"
    if n < BOUND_MIN_FRAMES and seconds < BOUND_MIN_S:
        return (f"only {frames} over {shown:.1f} s; a verdict needs {BOUND_MIN_FRAMES} frames and "
                f"{BOUND_MIN_S:.0f} s")
    if n < BOUND_MIN_FRAMES:
        return f"only {frames}; a verdict needs {BOUND_MIN_FRAMES}"
    return f"only {shown:.1f} s of steady frames; a verdict needs {BOUND_MIN_S:.0f} s"


def _classify(cap: Capture, fr: dict[str, list], rows: list[int],
              tick_spans: list[tuple[float, float]]) -> dict[str, Any]:
    hz = refresh_hz(cap)
    period = 1000.0 / hz if hz else None
    notes: list[str] = []
    out: dict[str, Any] = {"verdict": "unknown", "r": None, "ft_ms": None, "gpu_ms": None, "busiest_thread": None,
                           "cap_fps": None, "refresh_hz": hz, "vsync": None, "vrr": None, "confidence": "low",
                           "notes": notes, "gpu_busy_pct": None, "quantised_pct": None, "steady_frames": 0,
                           "steady_s": 0.0, "steady_fps": None, "cpu_share": None, "headroom": None}

    thread_col = busiest_column(cap)
    means = _window_means(cap, thread_col, tick_spans)
    busiest = st.median(means) / 100.0 if means else None
    out["busiest_thread"] = busiest
    nlp = _logical_cpus(cap)
    proc_means = _window_means(cap, S.GAME_CPU_PCT, tick_spans)
    proc_share = st.median(proc_means) / 100.0 / nlp if proc_means and nlp else None
    out["cpu_share"] = proc_share

    fm = fr.get(F.FRAME_MS) or []
    frame_ms = [float(fm[i]) for i in rows if _pos(_at(fm, i))]
    out["steady_frames"] = len(frame_ms)
    out["steady_s"] = math.fsum(frame_ms) / 1000.0
    row_set = set(rows)
    d_rows, _, d_vals, source = display_series(fr)
    d = [x for i, x in zip(d_rows, d_vals) if i in row_set] if source == "display" else []
    # A1, A6: the frame rate outside stutters and FPS drops, counted like avg_fps (displayed frames where display
    # is tracked), so freezes never make a steady game "low FPS"
    shown = d if source == "display" else frame_ms
    out["steady_fps"] = 1000.0 * len(shown) / math.fsum(shown) if shown else None
    if len(frame_ms) < BOUND_MIN_FRAMES or out["steady_s"] < BOUND_MIN_S:
        if frame_ms:
            notes.append(_too_short_note(len(frame_ms), out["steady_s"]))
        elif fr.get(F.T):
            notes.append("no steady frames: every frame was inside a lag event")
        else:
            notes.append("frame timing not measured")
        engine = _window_means(cap, S.GAME_GPU_PCT, tick_spans)
        if engine and st.median(engine) >= ENGINE_GPU_PCT:
            out["verdict"] = "gpu"
            notes.append(f"probably GPU-limited: the game kept its GPU engine {st.median(engine):.0f}% busy "
                         f"(>= {ENGINE_GPU_PCT:.0f}%)")
        return out

    # FT = median MsBetweenAppStart (= MsCPUBusy + MsCPUWait, PM 2.2), else MsBetweenPresents (PM 3.3)
    cb, cw = fr.get(F.CPU_BUSY_MS) or [], fr.get(F.CPU_WAIT_MS) or []
    app_ms = []
    for i in rows:
        busy, wait = _bounded(_at(cb, i)), _bounded(_at(cw, i))
        if busy is not None and busy > 0:
            app_ms.append(float(busy) + float(wait if wait is not None and wait > 0 else 0.0))
        elif _pos(_at(fm, i)):
            app_ms.append(float(fm[i]))
    ft = st.median(app_ms)
    out["ft_ms"] = ft

    gb_col = fr.get(F.GPU_BUSY_MS) or []
    gpu_busy = [float(gb_col[i]) for i in rows if _pos(_at(gb_col, i))]
    hags = hags_on(cap)
    margin = HAGS_MARGIN_MS if hags else 0.0
    r = raw = None
    if len(gpu_busy) * 2 >= len(rows):   # GPU busy prints 0 when unknown (PM 2.3): need it on most frames
        raw = st.median(gpu_busy)
        out["gpu_busy_pct"] = 100.0 * raw / ft
        g = max(0.0, raw - margin)
        out["gpu_ms"] = g
        r = g / ft
        out["r"] = r
        notes.append(f"GPU busy {g:.1f} ms of a {ft:.1f} ms frame (r = {r:.2f}"
                     + (f", after the {HAGS_MARGIN_MS} ms HAGS margin)" if margin else ")"))
        if hags is None:
            notes.append("hardware GPU scheduling unknown: no HAGS margin taken")
    else:
        notes.append("no GPU busy timing for most frames")

    vsync = False
    if period and d:
        q = _quantised_share(d, period)
        out["quantised_pct"] = 100.0 * q
        band = max(VSYNC_BAND_MS, VSYNC_BAND_FRAC * period)
        vsync = q >= VSYNC_SHARE and _near_multiple(ft, period, band)
        if vsync and _sync_off(fr, rows):
            vsync = False    # A14: composition quantises a game that asked for no VSync; its rate is its own
            notes.append("the game presents with SyncInterval 0 or allows tearing: not VSync, though Windows "
                         "shows its frames on whole refresh periods")
        out["vsync"] = vsync
        out["vrr"] = False if q >= VSYNC_SHARE else _vrr_hint(cap, ft, period)
        notes.append(f"{100 * q:.0f}% of displayed frames on whole refresh periods ({period:.2f} ms at {hz:.0f} Hz)")
    elif not period:
        notes.append("refresh rate of the game's display unknown: VSync not checked")

    cov, c = _cap_cadence(fr, rows)
    regular = cov is not None and c is not None and cov < CAP_COV
    if regular and period and _on_refresh_multiple(c, period):
        regular = False       # a cadence on a refresh multiple is VSync's, not a cap's (LC 3.3)
    if cov is not None:
        notes.append(f"present cadence varies {100 * cov:.1f}% around {c:.2f} ms ({1000 / c:.0f} FPS)")
    if busiest is not None:
        notes.append(f"busiest game thread {100 * busiest:.0f}% of a CPU (median of 1 s windows)")
    else:
        notes.append("game thread load not measured")

    def near(x: float | None, edge: float) -> bool:
        return x is not None and abs(x - edge) < CONF_MARGIN

    verdict, conf = "unknown", "low"
    if vsync:
        verdict, conf = _vsync_limit(fr, rows, ft, hz, raw, busiest, notes,
                                     lead=thread_col == S.GAME_LEAD_THREAD_PCT)
    elif regular and (r is None or r < R_CPU) and (busiest is None or busiest < THREAD_BOUND):
        verdict = "capped"
        out["cap_fps"] = 1000.0 / c
        conf = "high" if r is not None and busiest is not None and not near(busiest, THREAD_BOUND) else "medium"
    elif r is None:
        verdict, conf = "unknown", "low"
    # A14: the 1 ms gap only settles the borderline zone; it never calls a GPU idle >= 25 % of a frame the limit
    elif r >= R_GPU or (r > R_CPU and ft - (out["gpu_ms"] or 0.0) <= GPU_GAP_MS):
        verdict, conf = "gpu", ("medium" if near(r, R_GPU) else "high")
    elif r <= R_CPU and busiest is not None and busiest >= THREAD_BOUND:
        verdict, conf = "cpu_main", ("medium" if near(r, R_CPU) or near(busiest, THREAD_BOUND) else "high")
    elif r <= R_CPU and proc_share is not None and proc_share >= ALL_CORES_BOUND:
        verdict, conf = "cpu_all", ("medium" if near(r, R_CPU) else "high")
    elif r <= R_CPU and busiest is None:
        verdict, conf = "unknown", "low"
        notes.append("CPU or something else (r <= 0.75), but without thread data CPU cannot be told from waiting")
    elif r <= R_CPU:
        verdict, conf = "waiting", ("medium" if near(r, R_CPU) or near(busiest, THREAD_BOUND) else "high")
    else:
        verdict, conf = "mixed", "medium"
    shared = gpu_shared(cap, tick_spans)
    if shared is not None and verdict in ("capped", "waiting"):
        # A7 (LC 4.6, PM 3.2): other programs' GPU work paces the game as evenly as a cap and leaves its own GPU
        # busy short, so neither "a cap" nor "waiting on something else" can be told from a shared GPU
        notes.insert(0, f"other programs kept the game's GPU {shared:.0f}% busy (median of 1 s windows; "
                        f"{SHARED_GPU_PCT:.0f}% or more), so a frame-rate cap, the shared GPU or something else "
                        "may hold the frame rate")
        verdict, conf = "unknown", "low"
        out["cap_fps"] = None
    out["verdict"], out["confidence"] = verdict, conf
    out["headroom"] = headroom(out, shared)
    return out


def headroom(b: dict[str, Any], shared: float | None = None) -> dict[str, Any] | None:
    """How close the sides that do not limit the frame rate are to their own limits, from the steady frames bound()
    judged (DESIGN, Headroom): an estimate for this game at these settings, never a benchmark. None without a steady
    stretch long enough for a verdict (BOUND_MIN_FRAMES and BOUND_MIN_S). `shared`: gpu_shared's reading.

    - gpu_fps: 1000 / G, the frame rate the GPU's own work per frame allows (G as r uses it: the median GPU busy less
      the HAGS margin). None while other programs share the game's GPU: G leaves their work out (PM 3.2).
    - cpu_fps: [low, high], steady_fps scaled by how busy the CPU was. u is the busiest game thread's share of one
      logical CPU, or the game's share of every logical CPU (cpu_share) where that is busier (`cpu_from` "thread" or
      "all_cores"): steady_fps * [max(1, 0.85 / u), 1 / u], since from 0.85 a thread counts as saturated (LC 3.2) and
      at 1 it is. Never MsCPUBusy: it is wall time from one Present() to the next, so it fills the frame of a GPU-bound
      game and 1000 / MsCPUBusy only restates the frame rate (PM 3.2, LC 0 item 3, learnings).
    - display_fps: the refresh rate. steps_fps, where 90 % or more of the displayed frames sit on whole refreshes (a
      fixed-refresh screen, under VSync or Windows' composition): the rates at which the pace is even, refresh / k for
      k = 1, 2, ..., down to STEP_MIN_FPS.
    - limit, next, speedup: for a GPU or CPU verdict, that side ("gpu" or "cpu"); the next limit, the lowest other
      ceiling ("cpu", "gpu", "display", or "cpu_or_display" where the CPU's range spans the refresh rate); and
      [low, high], how many times faster the limiting part must get for that to take over: [next low / limit high,
      next high / limit low], never under 1. The limit's own range runs from steady_fps to its ceiling, or is its
      ceiling alone under a VSync lock, which rounds steady_fps down to refresh / k. A GPU busy no longer than the HAGS
      margin (G = 0) is never the next limit: the screen is. next and speedup stay None where the other side was not
      measured, or the CPU's threads were not what saturated it.
    """
    steady, n, secs = _num(b.get("steady_fps")), _num(b.get("steady_frames")), _num(b.get("steady_s"))
    if steady is None or steady <= 0 or n is None or n < BOUND_MIN_FRAMES or secs is None or secs < BOUND_MIN_S:
        return None
    steady = float(steady)
    g = _num(b.get("gpu_ms"))
    gpu = 1000.0 / g if g is not None and g > 0 and shared is None else None
    idle_gpu = g is not None and g <= 0 and shared is None      # GPU busy within the HAGS margin: never the next limit
    cpu, cpu_from, saturated = None, None, False
    load = [(float(u), edge, name) for u, edge, name in ((_num(b.get("busiest_thread")), THREAD_BOUND, "thread"),
                                                         (_num(b.get("cpu_share")), ALL_CORES_BOUND, "all_cores"))
            if u is not None and u > 0]
    if load:
        u, edge, cpu_from = max(load, key=lambda x: x[0])      # the busier one saturates first; a tie keeps the thread
        u = min(u, 1.0)
        cpu = [steady * max(1.0, edge / u), steady / u]
        saturated = u >= edge
    hz = _num(b.get("refresh_hz"))
    display = float(hz) if hz is not None and hz > 0 else None
    q = _num(b.get("quantised_pct"))
    steps = ([display / k for k in range(1, int(display // STEP_MIN_FPS) + 1)]
             if display is not None and q is not None and q >= 100.0 * VSYNC_SHARE else [])
    out: dict[str, Any] = {"steady_fps": steady, "gpu_fps": gpu, "cpu_fps": cpu, "cpu_from": cpu_from,
                           "display_fps": display, "steps_fps": steps or None, "limit": None, "next": None,
                           "speedup": None}
    side = {"gpu": "gpu", "cpu_main": "cpu", "cpu_all": "cpu"}.get(b.get("verdict"))
    if side is None:
        return out
    out["limit"] = side
    if side == "gpu":
        if cpu is None:
            return out
        if b.get("vsync") is True and gpu is not None:
            own = (gpu, gpu)
        else:
            own = (min(steady, gpu or steady), max(steady, gpu or steady))
        if display is None or cpu[1] <= display:
            nxt, want = "cpu", (cpu[0], cpu[1])
        elif cpu[0] >= display:
            nxt, want = "display", (display, display)
        else:
            nxt, want = "cpu_or_display", (cpu[0], display)
    else:
        if cpu is None or not saturated or (gpu is None and not (idle_gpu and display is not None)):
            return out
        own = (cpu[0], cpu[1])
        nxt, want = (("gpu", (gpu, gpu)) if gpu is not None and (display is None or gpu <= display)
                     else ("display", (display, display)))
    out["next"] = nxt
    out["speedup"] = [max(1.0, want[0] / own[1]), max(1.0, want[1] / own[0])]
    return out


THREAD_UPPER_NOTE = ("a figure that reads high when several threads are busy (this capture was saved by an earlier "
                     "lag-doctor)")                                   # rules._holds_open finds the note by it


def _vsync_limit(fr: dict[str, list], rows: list[int], ft: float, hz: float, raw_gpu: float | None,
                 busiest: float | None, notes: list[str], lead: bool = False) -> tuple[str, str]:
    """(verdict, confidence) for a game locked to k refreshes a frame (A6). From k = 2, VSync rounds the rate
    down to refresh / k, so whatever needs more than k - 1 periods is what holds it there: the GPU when its
    raw busy time does, the CPU when the busiest thread is saturated or its own CPU time a frame does (a thread VSync
    holds waits for the refresh the rest of the frame, so it is never saturated). Never MsCPUBusy: it counts the waits
    outside Present() (a waitable swap chain, a limiter's sleep), so a game VSync holds reads busy for the whole
    frame (PM 3.2). Only when neither does is the display itself the limit. The time a frame is judged only on the
    lead thread (`lead`): a capture saved before it has each tick's busiest, an upper bound (four threads at 40 %
    read 53 %, past half of a two-refresh frame)."""
    period = 1000.0 / hz
    k = round(ft / period)
    if k < 2:
        return "vsync", "high"
    one = (k - 1) * period
    rounds = f"VSync rounds the frame rate down to {hz / k:.0f} FPS (refresh / {k})"
    if raw_gpu is not None and raw_gpu > one:
        notes.append(f"{rounds}: the GPU needs {raw_gpu:.1f} ms a frame, more than {k - 1} refresh period(s) "
                     f"({one:.1f} ms)")
        return "gpu", ("medium" if raw_gpu / one - 1.0 < CONF_MARGIN else "high")
    work = busiest * ft if busiest is not None and lead else None      # the lead thread's own CPU time a frame
    if busiest is not None and (busiest >= THREAD_BOUND or (work is not None and work > one)):
        notes.append(f"{rounds}: the busiest game thread runs {100 * busiest:.0f}% of a CPU"
                     + ("" if busiest >= THREAD_BOUND else f", {work:.1f} ms a frame, more than {k - 1} refresh "
                                                     f"period(s) ({one:.1f} ms)"))
        margin = busiest - THREAD_BOUND if busiest >= THREAD_BOUND else work / one - 1.0
        return "cpu_main", ("medium" if margin < CONF_MARGIN else "high")
    if busiest is not None and not lead and busiest * ft > one:      # each tick's busiest: it may pass, or not
        notes.append(f"{rounds}, but whether the CPU holds it there could not be told: the busiest game thread read "
                     f"{100 * busiest:.0f}% of a CPU, {busiest * ft:.1f} ms a frame, more than {k - 1} refresh "
                     f"period(s) ({one:.1f} ms), {THREAD_UPPER_NOTE}")
    return "vsync", "high"


def _sync_off(fr: dict[str, list], rows: list[int]) -> bool:
    """LC 3.3 (PM 2.1): VSync needs SyncInterval >= 1 and AllowsTearing = 0. True when the chain's usual values
    rule it out: SyncInterval 0, or tearing allowed. -1 (unknown, e.g. OpenGL) and missing columns do not."""
    def usual(col: str) -> float | int | None:
        seen = Counter(v for v in (_num(_at(fr.get(col), i)) for i in rows) if v is not None)
        return seen.most_common(1)[0][0] if seen else None
    return usual(F.SYNC_INTERVAL) == 0 or usual(F.ALLOWS_TEARING) == 1


def _vrr_hint(cap: Capture, ft: float, period: float) -> bool | None:
    """VRR shows as continuous displayed intervals below the refresh rate (LC 3.3, PM 8.5). No column
    reports it and tearing looks the same, so this is True only when Windows' own VRR setting is on."""
    inv = cap.inventory if isinstance(cap.inventory, dict) else {}
    settings = inv.get("settings") if isinstance(inv.get("settings"), dict) else {}
    if settings.get("vrr") is True and ft > period:
        return True
    return None
