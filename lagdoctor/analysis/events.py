"""Lag events: stutters, FPS drops, uneven pacing, ping spikes, packet loss, server gaps, the user's marks.

DESIGN.md "Lag events" (LC 2.4; NE 2.1, 2.2, 4.6). Pure functions over a Capture. Constants name the
research note section they come from: LC = docs/research/lag-causes.md, NE = network-and-etw.md,
PM = presentmon.md. Every value read from the capture is checked (see frames.py).
"""
from __future__ import annotations

import bisect
import math
from collections import deque
from typing import Any, Iterable

from lagdoctor.analysis import frames as fa
from lagdoctor.analysis import stats as st
from lagdoctor.model import F, N, Capture, LagEvent

# ---- stutters (LC 2.4) ---------------------------------------------------------------------------
STUTTER_X = fa.STUTTER_X     # LC 2.4: D >= 2.5 x M (frames.py holds the stutter and stall test both share)
STUTTER_MIN_MS = fa.STUTTER_MIN_MS   # LC 2.4 (S20): ... and D - M >= 8 ms, on a quantised display D - max(M, E)
STALL_COVER = fa.STALL_COVER
CADENCE_SLACK = 0.005        # [measured] (A2) the cadence's trailing median strays this far past k refresh
                             # periods by jitter alone: p99.9 +0.50 % on the refresh-locked LagLab model at 144 Hz
                             # with the measured wake-ups (tests/synth.py, 30 seeds); gold test_case_5's dwm.exe
                             # at 60 Hz stays within 0.32 %. Any slower cadence holds k + 1 refreshes regularly
MERGE_S = 0.25               # LC 2.4: stutters closer than 250 ms (one 4 Hz sample) are one episode
# ---- FPS drops (LC 2.4, S18) ---------------------------------------------------------------------
FPS_WINDOW_S = 1.0           # LC 2.4: 1 s rolling displayed FPS ...
FPS_DROP_FRAC = 0.80         # LC 2.4 (S18): ... below 80 % of the target ...
FPS_DROP_MIN_S = 2.0         # LC 2.4: ... for >= 2 s
FPS_STEP_S = 0.1             # [inferred] how often the rolling FPS is evaluated
MAX_GRID_POINTS = 864_000    # [inferred] a day of 0.1 s steps; a longer span (junk times) coarsens the grid
TARGET_PCTL = 90.0           # LC 2.4: with no cap and no VSync lock, the target is the run's p90 FPS
# ---- pacing (LC 2.4) -----------------------------------------------------------------------------
PACING_WINDOW_S = 2.0        # LC 2.4: over 2 s ...
PACING_SWING_MS = 4.0        # LC 2.4 (CapFrameX variance bins, S11): ... |D_i - D_(i-1)| >= 4 ms ...
PACING_SHARE = 0.10          # LC 2.4: ... on more than 10 % of intervals ...
PACING_ANIM_MS = 2.0         # LC 2.4: ... or p95 |MsAnimationError| > 2 ms
PACING_MIN_SAMPLES = 10      # [inferred] fewer intervals in a 2 s window say nothing about pacing
# ---- network (NE 2.1-2.3, 4.6) -------------------------------------------------------------------
SPIKE_RISE_MS = 30.0         # NE 2.2, 2.3: an RTT >= 30 ms above its idle minimum is noticeable ...
SPIKE_BASE_WINDOW_S = 120.0  # NE 2.3: ... the idle minimum is taken over the last 120 s ...
SPIKE_X_MEDIAN = 2.0         # DESIGN (Lag events) [inferred]: ... and the RTT is >= 2x the run's median ...
SPIKE_PERSIST_ROUNDS = 2     # [inferred] (A8) ... on at least 2 of 3 consecutive rounds: NE 2.2's levels are
SPIKE_PERSIST_SPAN = 3       # 15 s window figures (p50, jitter), so one slow ICMP reply (NE 2.4: often
                             # deprioritised) is no spike; two in about a second are a rise that lasted ...
SPIKE_ALONE_MS = 100.0       # ... unless one reply alone is >= 100 ms over the minimum: NE 2.2's "bad" rise
                             # under load, and Claypool's 100 ms for first-person games
NET_ROUND_S = 0.5            # DESIGN (network): one probe round every 0.5 s
NET_MERGE_S = 1.0            # [inferred] samples up to two rounds apart are one episode
LOSS_WINDOW_S = 15.0         # NE 2.1, 2.2 (Teams): loss is judged per 15 s window ...
LOSS_PCT = 1.0               # NE 2.2: ... >= 1 % is bad ...
LOSS_MIN_PROBES = 2          # [inferred] (A8) ... from at least 2 lost probes: at 2 Hz one loss is already 3 %
                             # of a window, and one unanswered ICMP probe is no outage (NE 2.4)
BURST_ROUNDS = 2             # NE 2.1, 2.2 at 2 Hz: Teams' 200 ms burst window holds at most one probe, so a
                             # burst is >= 2 consecutive lost rounds (an outage of >= 1 s)
GAP_MIN_MS = 100.0           # NE 4.6: a gap in the server's packets >= 100 ms is visible
# ---- marks (LC 2.4, S19) -------------------------------------------------------------------------
MARK_BEFORE_S = 3.0          # LC 2.4: a hotkey press covers [mark - 3 s, mark + 0.5 s] (reaction ~212 ms)
MARK_AFTER_S = 0.5

KIND_ORDER = {"stutter": 0, "fps_drop": 1, "pacing": 2, "net_spike": 3, "net_loss": 4, "net_gap": 5, "mark": 6}
_NET_COLS = {"gateway": N.GW_RTT_MS, "internet": N.INET_RTT_MS, "server": N.SERVER_RTT_MS}
# NE 2.4, NE 7 row 9: game servers and routers often ignore pings. One that answered none of its probes was never
# measured by ping, so its loss is unknown, never 100 % (a silent server once read as "Network lag" over a whole
# capture while its game packets kept arriving). The internet anchor is not one of them: silence there is an outage.
SILENT_UNMEASURED = ("gateway", "server")
_num = fa._num


def find_events(cap: Capture, fr: dict[str, list], stats: dict[str, Any] | None) -> list[LagEvent]:
    """Every lag event of the capture, in time order, numbered from 1.

    `stats` (frame_stats(fr, ...)) is part of the DESIGN signature but no longer read: the stutter baseline
    is sized from the display series itself (frames.baseline), so these stutters and frame_stats' stalls
    come from one test. The FPS-drop target needs the cap / VSync verdict, which is judged here on the frames
    outside stutters (the full bound() needs these events first).
    """
    stutters = _stutters(cap, fr)
    spans = fa._merged_spans(stutters, ("stutter",))
    # the cap and VSync verdict that sets the FPS-drop target leaves time behind another window out too: a game that
    # slows itself down there would set a target of its background rate (bound() does the same)
    pace = fa._classify(cap, fr, fa._steady_rows(fr, fa.not_steady_spans(stutters, fr)),
                        fa.not_steady_spans(stutters, fr, pad=fa.TICK_PAD_S))
    evs = (stutters + _fps_drops(fr, pace) + _pacing(cap, fr, spans) + _net_spikes(cap) + _net_losses(cap)
           + _net_gaps(cap) + _marks(cap))
    evs = _finite(evs)
    evs.sort(key=lambda e: (e.t_start, KIND_ORDER[e.kind], e.t_end))
    for i, e in enumerate(evs, 1):
        e.id = i
    _link_marks(evs, bool(fr.get(F.T)))
    return evs


def _finite(evs: list[LagEvent]) -> list[LagEvent]:
    """A15: an event whose times or severity are not finite cannot be placed, ranked or printed, so it is
    left out. Unreachable while every reader is bounded (frames.MAX_ABS_VALUE); the last line of defence."""
    return [e for e in evs if all(isinstance(x, (int, float)) and math.isfinite(x)
                                  for x in (e.t_start, e.t_end, e.severity))]


# --------------------------------------------------------------------- stutters ---

def stutter_class(ms: float, baseline_ms: float) -> str:
    """LC 2.4 classes of a stutter's worst frame: hitch >= 50 ms and >= 2 M, severe >= 150, freeze >= 500."""
    if ms >= fa.FREEZE_MS:
        return "freeze"
    if ms >= fa.SEVERE_MS:
        return "severe"
    if ms >= fa.HITCH_MS and ms >= fa.HITCH_X_BASELINE * baseline_ms:
        return "hitch"
    return "stutter"


def _stutters(cap: Capture, fr: dict[str, list]) -> list[LagEvent]:
    rows, t, d, source = fa.display_series(fr)
    if len(d) < 2:
        return []
    m = fa.baseline(d)
    held = _regular_hold(cap, fr, rows, d, source)
    first = fa.first_t(fr)
    fm = fr.get(F.FRAME_MS) or []
    hits: list[dict[str, Any]] = []
    for j, (dj, mj) in enumerate(zip(d, m)):
        if mj is None or dj < STUTTER_X * mj:
            continue
        excess = dj - (mj if held[j] is None else max(mj, held[j]))   # A2: past the cadence's longest hold
        if excess < STUTTER_MIN_MS:
            continue
        own = fa._bounded(fa._at(fm, rows[j]))
        span = max(dj, float(own)) if own is not None and own > 0 else dj   # cover the frozen screen too
        hits.append({"start": max(t[j] - span / 1000.0, first), "end": t[j], "ms": dj, "base": mj, "t": t[j],
                     "stall": False})

    # A stall can show only as a jump in t between consecutive rows (frames.stall_gaps; PM 2.4: with several
    # target chains, PresentMon drops the frames around a > 4 s stall). frame_stats folds in the same ones.
    for a, b, gap_ms, mk in fa.stall_gaps(fr):
        hits.append({"start": a, "end": b, "ms": gap_ms, "base": mk, "t": b, "stall": True})

    hits.sort(key=lambda h: (h["start"], h["end"]))
    groups: list[list[dict[str, Any]]] = []
    group_end = -math.inf
    for h in hits:
        if groups and h["start"] - group_end < MERGE_S:
            groups[-1].append(h)
            group_end = max(group_end, h["end"])
        else:
            groups.append([h])
            group_end = h["end"]
    out = []
    for g in groups:
        worst = max(g, key=lambda h: h["ms"])
        out.append(LagEvent(
            id=0, kind="stutter", t_start=min(h["start"] for h in g), t_end=max(h["end"] for h in g),
            severity=worst["ms"],
            detail={"worst_t": worst["t"], "worst_ms": worst["ms"], "baseline_ms": worst["base"],
                    "frames": sum(1 for h in g if not h["stall"]), "class": stutter_class(worst["ms"], worst["base"]),
                    "stall": any(h["stall"] for h in g), "source": source}))
    return out


def _regular_hold(cap: Capture, fr: dict[str, list], rows: list[int], d: list[float],
                  source: str) -> list[float | None]:
    """E for each frame of a quantised display series (A2): the longest hold the game's regular present
    cadence produces, ceil(Mp / P - CADENCE_SLACK) * P, with Mp the trailing median of MsBetweenPresents over
    the same frames as M and P the refresh period. At 120 FPS on 144 Hz the screen holds frames for one or
    two refreshes, so M is one refresh (6.94 ms) while two (13.9 ms) are normal, and a frame held for three
    is only one refresh late: no stutter. A cap just under the refresh (141 FPS on 144 Hz) holds two as well,
    one frame in 47, so the slack absorbs only the jitter of Mp. None where this does not apply: the refresh is
    unknown, the series is presents only, or the display is not quantised (< 90 % of D on whole periods: VRR or
    tearing, LC 3.3).
    """
    none: list[float | None] = [None] * len(d)
    hz = fa.refresh_hz(cap)
    if not hz or source != "display":
        return none
    period = 1000.0 / hz
    q = fa._quantised_share(d, period)
    if q is None or q < fa.VSYNC_SHARE:
        return none
    fm = fr.get(F.FRAME_MS) or []
    presents = [float(fm[i]) if fa._pos(fa._at(fm, i)) else None for i in rows]
    mp = st.trailing_median(presents, fa.baseline_frames(st.median(d)))      # the same N as M
    return [None if x is None else math.ceil(x / period - CADENCE_SLACK) * period for x in mp]


# -------------------------------------------------------------------- FPS drops ---

def _displayed_times(fr: dict[str, list]) -> list[float]:
    t = fr.get(F.T) or []
    shown = fr.get(F.DISPLAYED) or []
    tracked = any(_num(v) in (0, 1) for v in shown)
    return [float(t[i]) for i in range(len(t)) if fa._bounded(t[i]) is not None
            and (not tracked or _num(fa._at(shown, i)) == 1)]


def rolling_fps(times: list[float]) -> tuple[list[float], list[float]]:
    """(grid times, displayed FPS over the 1 s centred on each), every 0.1 s inside the frame data.

    The grid never exceeds MAX_GRID_POINTS: a capture whose times span more than a day is corrupt, and
    its grid is coarsened rather than allowed to grow with the span instead of the data.
    """
    if len(times) < 2 or times[-1] - times[0] < FPS_WINDOW_S:
        return [], []
    half = FPS_WINDOW_S / 2.0
    span = times[-1] - times[0] - FPS_WINDOW_S
    step = max(FPS_STEP_S, span / (MAX_GRID_POINTS - 1))
    g0, steps = times[0] + half, int(math.floor(span / step + 1e-9)) + 1
    grid, fps = [], []
    for k in range(steps):
        g = g0 + k * step
        n = bisect.bisect_left(times, g + half) - bisect.bisect_left(times, g - half)
        grid.append(g)
        fps.append(n / FPS_WINDOW_S)
    return grid, fps


def fps_target(pace: dict[str, Any], fps: list[float]) -> tuple[float | None, str | None]:
    """LC 2.4: the cap if one is detected, else the locked rate if VSync-locked, else the run's p90 FPS.

    A VSync lock at k refreshes per frame targets refresh / k (at k = 1, the refresh rate itself). A
    fixed-refresh display (refresh known, VRR not seen) never shows more frames than it refreshes, so the
    target is never above the refresh rate: an 86 FPS cap on 60 Hz is not an FPS drop to 60 (A11).
    """
    hz, ft = _num(pace.get("refresh_hz")), _num(pace.get("ft_ms"))
    if pace.get("verdict") == "capped" and _num(pace.get("cap_fps")):
        target, origin = float(pace["cap_fps"]), "cap"
    elif pace.get("verdict") == "vsync" and hz and ft:
        k = max(1, round(ft / (1000.0 / hz)))
        target, origin = hz / k, "refresh"
    elif fps:
        target, origin = st.percentile(fps, TARGET_PCTL), "p90"
    else:
        return None, None
    if hz and hz > 0 and pace.get("vrr") is not True and target > hz:
        target, origin = float(hz), "refresh"
    return target, origin


def _fps_drops(fr: dict[str, list], pace: dict[str, Any]) -> list[LagEvent]:
    grid, fps = rolling_fps(_displayed_times(fr))
    # a 1 s window reaching into a stretch behind another window counts the frames Windows did not show there: it is
    # no reading, so it never sets the target and it ends a drop (one never spans time behind another window)
    half = FPS_WINDOW_S / 2.0
    behind = [(a - half, b + half) for a, b in fa.hidden_spans(fr)]
    starts = [a for a, _ in behind]
    blind = [fa._in_spans(g, behind, starts) for g in grid]
    target, origin = fps_target(pace, [v for v, x in zip(fps, blind) if not x])
    if not grid or not target:
        return []
    step = grid[1] - grid[0] if len(grid) > 1 else FPS_STEP_S
    limit = FPS_DROP_FRAC * target
    out, run = [], []
    for g, v, x in zip(grid + [math.inf], fps + [math.inf], blind + [True]):
        if v < limit and not x:
            run.append((g, v))
            continue
        if run and run[-1][0] - run[0][0] + step >= FPS_DROP_MIN_S - 1e-9:
            vals = [x for _, x in run]
            med = st.median(vals)
            out.append(LagEvent(
                id=0, kind="fps_drop", t_start=run[0][0], t_end=run[-1][0], severity=100.0 * (1.0 - med / target),
                detail={"target_fps": target, "target_from": origin, "fps": med, "min_fps": min(vals)}))
        run = []
    return out


# ----------------------------------------------------------------------- pacing ---

def _pacing(cap: Capture, fr: dict[str, list], spans: list[tuple[float, float]]) -> list[LagEvent]:
    """LC 2.4 pacing over app frames (DESIGN: bound and pacing use app frames only).

    Frames inside a stutter or stall span (`spans`) are left out of the swings and the animation errors: a
    hitch's swings into and out of it belong to the stutter, and counting them made hitches look like the
    pacing that then outranked them (A1). detail["criterion"] says which test fired ("swing", "anim" or
    "both"); severity is the median swing >= 4 ms when the swing test fired, else the animation error p95.
    """
    rows, t, d, _source = fa.display_series(fr)
    keep = [j for j, i in enumerate(rows) if fa._is_app(fr, i)]
    if len(keep) < 2:
        return []
    fm, anim = fr.get(F.FRAME_MS) or [], fr.get(F.ANIM_ERROR_MS) or []
    hz = fa.refresh_hz(cap)
    period = 1000.0 / hz if hz else None
    starts = [a for a, _ in spans]
    calm = [not fa._in_spans(x, spans, starts) for x in t]       # outside every stutter and stall
    first = fa.first_t(fr)
    t0 = t[keep[0]]
    windows: dict[int, list[int]] = {}
    for j in keep:
        windows.setdefault(int(math.floor((t[j] - t0) / PACING_WINDOW_S)), []).append(j)

    def swings_of(js: list[int]) -> list[float]:
        return [abs(d[b] - d[a]) for a, b in zip(js, js[1:]) if calm[a] and calm[b]]

    def errors_of(js: list[int]) -> list[float]:
        return [abs(float(v)) for v in (fa._bounded(fa._at(anim, rows[j])) for j in js if calm[j]) if v is not None]

    flagged: list[tuple[int, list[int], bool, bool]] = []
    for k in sorted(windows):
        js = windows[k]
        swings, errs = swings_of(js), errors_of(js)
        uneven = len(swings) >= PACING_MIN_SAMPLES and \
            sum(1 for s in swings if s >= PACING_SWING_MS) / len(swings) > PACING_SHARE
        jerky = len(errs) >= PACING_MIN_SAMPLES and st.percentile(errs, 95.0) > PACING_ANIM_MS
        if uneven or jerky:
            flagged.append((k, js, uneven, jerky))

    behind = fa.hidden_spans(fr)

    def apart(x0: float, x1: float) -> bool:          # time behind another window between them: nobody saw it
        return any(x0 < b and a < x1 for a, b in behind)

    out = []
    i = 0
    while i < len(flagged):                  # adjacent flagged windows are one episode, never across time behind
        js, by_swing, by_anim = list(flagged[i][1]), flagged[i][2], flagged[i][3]
        while (i + 1 < len(flagged) and flagged[i + 1][0] == flagged[i][0] + 1
               and not apart(t[js[-1]], t[flagged[i + 1][1][0]])):
            i += 1
            js += flagged[i][1]
            by_swing, by_anim = by_swing or flagged[i][2], by_anim or flagged[i][3]
        i += 1
        swings, errs = swings_of(js), errors_of(js)
        big = [s for s in swings if s >= PACING_SWING_MS]
        anim_p95 = st.percentile(errs, 95.0) if errs else None
        pres_sw = [abs(float(fm[rows[b]]) - float(fm[rows[a]])) for a, b in zip(js, js[1:])
                   if calm[a] and calm[b] and fa._pos(fa._at(fm, rows[a])) and fa._pos(fa._at(fm, rows[b]))]
        criterion = "both" if by_swing and by_anim else ("swing" if by_swing else "anim")
        if criterion == "anim" and anim_p95 is not None:
            severity = anim_p95              # A11: an even display swings ~0 ms; the error is what was jerky
        else:
            severity = st.median(big) if big else (st.median(swings) if swings else 0.0)
        detail = {
            "criterion": criterion,
            "swing_pct": 100.0 * len(big) / len(swings) if swings else None,
            "anim_p95_ms": anim_p95,
            "present_swing_pct": (100.0 * sum(1 for s in pres_sw if s >= PACING_SWING_MS) / len(pres_sw)
                                  if pres_sw else None),
            "quantised_pct": (100.0 * fa._quantised_share([d[j] for j in js], period) if period else None),
            "frames": len(js)}
        out.append(LagEvent(id=0, kind="pacing", t_start=max(t[js[0]] - d[js[0]] / 1000.0, first),
                            t_end=t[js[-1]], severity=severity, detail=detail))
    return out


# ---------------------------------------------------------------------- network ---

def _net_rows(cap: Capture, column: str) -> list[tuple[float, float | None]]:
    t, v = fa._col(cap.network, N.T), fa._col(cap.network, column)
    out = []
    for i, ti in enumerate(t):
        ti = fa._bounded(ti)
        if ti is None:
            continue
        vi = fa._num_col(column, fa._at(v, i))
        out.append((float(ti), float(vi) if vi is not None and vi > 0 else None))
    out.sort(key=lambda p: p[0])
    return out


def _count(sorted_times: list[float], lo: float, hi: float) -> int:
    return bisect.bisect_right(sorted_times, hi) - bisect.bisect_left(sorted_times, lo)


def _group(times: list[float], gap: float) -> list[list[float]]:
    groups: list[list[float]] = []
    for x in sorted(times):
        if groups and x - groups[-1][-1] <= gap:
            groups[-1].append(x)
        else:
            groups.append([x])
    return groups


def _net_spikes(cap: Capture) -> list[LagEvent]:
    """RTT >= 30 ms over its rolling 120 s minimum and >= 2x the median, on at least 2 of 3 consecutive
    rounds (one probe round per row, lost ones included), or on one round >= 100 ms over the minimum (A8)."""
    out = []
    gw = [(t, v) for t, v in _net_rows(cap, N.GW_RTT_MS) if v is not None]
    gw_t = [t for t, _ in gw]
    for target in ("internet", "server"):
        rounds = _net_rows(cap, _NET_COLS[target])
        pts = [(r, t, v) for r, (t, v) in enumerate(rounds) if v is not None]
        if len(pts) < 2:
            continue
        med = st.median([v for _, _, v in pts])
        floor: deque[tuple[float, float]] = deque()     # rolling minimum of the earlier samples
        past: dict[int, tuple[float, float, float]] = {}   # round -> (t, RTT, minimum) past the threshold
        for r, ti, vi in pts:
            while floor and floor[0][0] < ti - SPIKE_BASE_WINDOW_S:
                floor.popleft()
            if floor and vi - floor[0][1] >= SPIKE_RISE_MS and vi >= SPIKE_X_MEDIAN * med:
                past[r] = (ti, vi, floor[0][1])
            while floor and floor[-1][1] >= vi:
                floor.pop()
            floor.append((ti, vi))
        hits: dict[float, tuple[float, float]] = {}
        for r, (ti, vi, base) in past.items():
            # some run of SPIKE_PERSIST_SPAN consecutive rounds through this one holds enough rounds past it
            lasted = any(sum(1 for q in range(a, a + SPIKE_PERSIST_SPAN) if q in past) >= SPIKE_PERSIST_ROUNDS
                         for a in range(r - SPIKE_PERSIST_SPAN + 1, r + 1))
            if lasted or vi - base >= SPIKE_ALONE_MS:
                hits[ti] = (vi, base)
        for g in _group(list(hits), NET_MERGE_S):
            worst_t = max(g, key=lambda x: hits[x][0])
            worst, base = hits[worst_t]
            k = bisect.bisect_left(gw_t, worst_t - NET_ROUND_S / 2)
            gw_ms = gw[k][1] if k < len(gw) and abs(gw[k][0] - worst_t) <= NET_ROUND_S / 2 else None
            out.append(LagEvent(
                id=0, kind="net_spike", t_start=g[0] - NET_ROUND_S / 2, t_end=g[-1] + NET_ROUND_S / 2,
                severity=worst, detail={"target": target, "worst_ms": worst, "baseline_ms": base, "median_ms": med,
                                        "samples": len(g), "gw_ms": gw_ms}))
    return out


def _role_entry(table: Any, role: str, targets: dict) -> Any:
    """`probes`/`lost` are keyed by target role; accept the target's IP as the key too."""
    if not isinstance(table, dict):
        return None
    if role in table:
        return table[role]
    ip = targets.get(role)
    return table.get(ip) if isinstance(ip, str) else None


def _longest_outage(lost: list[float], answered: list[float]) -> int:
    """The most lost probes in a row: consecutive lost probes with no answered probe between them."""
    best = run = 0
    prev = None
    for x in lost:
        between = bisect.bisect_left(answered, x) - bisect.bisect_right(answered, prev) if prev is not None else 0
        run = run + 1 if prev is not None and between == 0 else 1
        best = max(best, run)
        prev = x
    return best


def _net_losses(cap: Capture) -> list[LagEvent]:
    """At least LOSS_MIN_PROBES lost probes in the 15 s around a loss, and >= 1 % of that window (A8, NE 2.2);
    detail["burst"] when >= BURST_ROUNDS consecutive rounds were lost (an outage of >= 1 s at 2 Hz). A game server or
    router that answered none of its probes has none (SILENT_UNMEASURED): its loss was not measured."""
    nf = cap.net_flows if isinstance(cap.net_flows, dict) else {}
    targets = nf.get("targets") if isinstance(nf.get("targets"), dict) else {}
    out = []
    for role, col in _NET_COLS.items():
        rows = _net_rows(cap, col)
        probe = _role_entry(nf.get("probes"), role, targets)
        from_t = fa._bounded(probe.get("from_t")) if isinstance(probe, dict) else None
        listed = _role_entry(nf.get("lost"), role, targets)
        if isinstance(listed, list):
            lost = sorted(float(x) for x in listed if fa._bounded(x) is not None)
        elif from_t is not None:
            lost = [t for t, v in rows if v is None and t >= from_t]    # never "not probed yet"
        else:
            continue
        if not lost:
            continue
        if from_t is not None:
            sent_t = [t for t, _ in rows if t >= from_t]
        else:
            sent_t = sorted([t for t, v in rows if v is not None] + lost)
        answered = [t for t, v in rows if v is not None and (from_t is None or t >= from_t)]
        if role in SILENT_UNMEASURED and not answered:
            continue
        for g in _group(lost, NET_MERGE_S):
            mid = g[0] + (g[-1] - g[0]) / 2.0
            lo, hi = mid - LOSS_WINDOW_S / 2, mid + LOSS_WINDOW_S / 2
            lost_w = _count(lost, lo, hi)
            sent_w = max(lost_w, _count(sent_t, lo, hi))
            loss_pct = 100.0 * lost_w / sent_w if sent_w else 0.0
            if lost_w < LOSS_MIN_PROBES or loss_pct < LOSS_PCT:
                continue
            burst = _longest_outage(g, answered) >= BURST_ROUNDS
            out.append(LagEvent(
                id=0, kind="net_loss", t_start=g[0] - NET_ROUND_S / 2, t_end=g[-1] + NET_ROUND_S / 2,
                severity=loss_pct, detail={"target": role, "lost": len(g), "lost_15s": lost_w, "sent_15s": sent_w,
                                           "loss_pct": loss_pct, "burst": burst}))
    return out


def _net_gaps(cap: Capture) -> list[LagEvent]:
    """Each gap the net_flows tracker reported, [t, ms, kind] with t the moment the server went quiet. A fourth field
    is a measured part's (analysis/crop.py): the gap began before the part at that time, and was clipped to the part's
    start, so its detail keeps it as "since" for the words."""
    nf = cap.net_flows if isinstance(cap.net_flows, dict) else {}
    gaps = nf.get("gaps")
    out = []
    for g in gaps if isinstance(gaps, list) else []:
        if not isinstance(g, (list, tuple)) or len(g) < 2:
            continue
        t, ms = fa._bounded(g[0]), fa._bounded(g[1])
        kind = g[2] if len(g) > 2 and isinstance(g[2], str) else None
        if t is None or ms is None or ms < GAP_MIN_MS:
            continue
        detail: dict[str, Any] = {"ms": float(ms), "kind": kind}
        since = fa._bounded(g[3]) if len(g) > 3 else None
        if since is not None and since < t:
            detail["since"] = float(since)
        out.append(LagEvent(id=0, kind="net_gap", t_start=float(t), t_end=float(t) + ms / 1000.0,
                            severity=float(ms), detail=detail))
    return out


# ------------------------------------------------------------------------ marks ---

def _marks(cap: Capture) -> list[LagEvent]:
    marks = cap.marks if isinstance(cap.marks, list) else []
    return [LagEvent(id=0, kind="mark", t_start=float(m) - MARK_BEFORE_S, t_end=float(m) + MARK_AFTER_S,
                     severity=0.0, detail={"mark_t": float(m)})
            for m in marks if fa._bounded(m) is not None]


def _link_marks(evs: list[LagEvent], frames_measured: bool) -> None:
    """LC 5.2: a mark whose window holds no frame event is lag that frame timing did not see.

    Only stutters and FPS drops count as seen (CC-5): a pacing event can span the whole capture (120 FPS on
    144 Hz judders throughout), so it would mark every press as seen. Pacing overlaps are listed apart.
    """
    for mk in (e for e in evs if e.kind == "mark"):
        over = [e for e in evs if e.kind != "mark" and e.t_start <= mk.t_end and mk.t_start <= e.t_end]
        frame_ids = [e.id for e in over if e.kind in ("stutter", "fps_drop")]
        mk.detail["frame_events"] = frame_ids
        mk.detail["pacing_events"] = [e.id for e in over if e.kind == "pacing"]
        mk.detail["net_events"] = [e.id for e in over if e.kind.startswith("net_")]
        mk.detail["visible_in_frames"] = bool(frame_ids) if frames_measured else None


# ------------------------------------------------------------------ stats keys ---

def summary_stats(cap: Capture, events: Iterable[LagEvent], hidden_s: Any = None) -> dict[str, Any]:
    """The Diagnosis.stats keys that need events or the network table (DESIGN, Diagnosis.stats keys):
    stutters, stutters_per_min, swap_chains, ping_gw_ms / ping_inet_ms / ping_server_ms (medians),
    jitter_ms (internet anchor: mean consecutive difference, NE 2.1) and loss_pct (internet: lost / sent).
    Stutters a minute count the time judged: `hidden_s` (frame_stats) behind another window is not."""
    out: dict[str, Any] = {}
    n = sum(1 for e in events if e.kind == "stutter")
    dur = fa._bounded(cap.duration_s)
    away = fa._bounded(hidden_s)
    if dur is not None and away is not None and away > 0:
        dur -= away
    out["stutters"] = n
    out["stutters_per_min"] = 60.0 * n / dur if dur is not None and dur > 0 else None
    chains = {fa._chain_key(v) for v, t in zip(fa._col(cap.frames, F.SWAP_CHAIN), fa._col(cap.frames, F.T))
              if _num(t) is not None}
    out["swap_chains"] = len(chains) if fa._col(cap.frames, F.T) else None
    for key, col in (("ping_gw_ms", N.GW_RTT_MS), ("ping_inet_ms", N.INET_RTT_MS), ("ping_server_ms", N.SERVER_RTT_MS)):
        vals = [v for _, v in _net_rows(cap, col) if v is not None]
        out[key] = st.median(vals) if vals else None
    inet = _net_rows(cap, N.INET_RTT_MS)
    diffs = [abs(b[1] - a[1]) for a, b in zip(inet, inet[1:]) if a[1] is not None and b[1] is not None]
    out["jitter_ms"] = st.mean(diffs) if diffs else None
    counts = probe_loss(cap, "internet")
    out["loss_pct"] = 100.0 * counts[0] / counts[1] if counts else None
    return out


def probe_loss(cap: Capture, role: str) -> tuple[float, float] | None:
    """(lost, sent) probes of one target over the capture, from the network collector's `probes` table (DESIGN,
    network); None when it was not recorded or does not add up. loss_pct and the packet_loss rule both read it."""
    nf = cap.net_flows if isinstance(cap.net_flows, dict) else {}
    targets = nf.get("targets") if isinstance(nf.get("targets"), dict) else {}
    probe = _role_entry(nf.get("probes"), role, targets)
    sent = _num(probe.get("sent")) if isinstance(probe, dict) else None
    lost = _num(probe.get("lost")) if isinstance(probe, dict) else None
    return (float(lost), float(sent)) if sent and lost is not None and 0 <= lost <= sent else None
