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
STUTTER_X = 2.5              # LC 2.4 (CapFrameX, S12): D >= 2.5 x M ...
STUTTER_MIN_MS = 8.0         # LC 2.4 (S20): ... and D - M >= 8 ms
MERGE_S = 0.25               # LC 2.4: stutters closer than 250 ms (one 4 Hz sample) are one episode
STALL_COVER = 0.5            # [inferred] a jump in t that the row's own frame time covers less than half of
                             # means rows are missing: a stall (PM 10 parser rule 7, PM 2.4)
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
SPIKE_X_MEDIAN = 2.0         # DESIGN (Lag events) [inferred]: ... and the RTT is >= 2x the run's median
NET_ROUND_S = 0.5            # DESIGN (network): one probe round every 0.5 s
NET_MERGE_S = 1.0            # [inferred] samples up to two rounds apart are one episode
LOSS_WINDOW_S = 15.0         # NE 2.1, 2.2 (Teams): loss is judged per 15 s window ...
LOSS_PCT = 1.0               # NE 2.2: ... >= 1 % is bad ...
BURST_WINDOW_S = 0.2         # NE 2.1, 2.2: ... or >= 10 % lost inside any 200 ms (burst loss)
BURST_PCT = 10.0
GAP_MIN_MS = 100.0           # NE 4.6: a gap in the server's packets >= 100 ms is visible
# ---- marks (LC 2.4, S19) -------------------------------------------------------------------------
MARK_BEFORE_S = 3.0          # LC 2.4: a hotkey press covers [mark - 3 s, mark + 0.5 s] (reaction ~212 ms)
MARK_AFTER_S = 0.5

KIND_ORDER = {"stutter": 0, "fps_drop": 1, "pacing": 2, "net_spike": 3, "net_loss": 4, "net_gap": 5, "mark": 6}
_NET_COLS = {"gateway": N.GW_RTT_MS, "internet": N.INET_RTT_MS, "server": N.SERVER_RTT_MS}
_num = fa._num


def find_events(cap: Capture, fr: dict[str, list], stats: dict[str, Any] | None) -> list[LagEvent]:
    """Every lag event of the capture, in time order, numbered from 1.

    `stats` is frame_stats(fr, ...); its median frame time sizes the stutter baseline. The FPS-drop
    target needs the cap / VSync verdict, which is judged here on the frames outside stutters (the full
    bound() needs these events first).
    """
    stats = stats if isinstance(stats, dict) else {}
    stutters = _stutters(fr, stats)
    spans = fa._merged_spans(stutters, ("stutter",))
    tick_spans = fa._merged_spans(stutters, ("stutter",), pad=fa.TICK_PAD_S)
    pace = fa._classify(cap, fr, fa._steady_rows(fr, spans), tick_spans)
    evs = (stutters + _fps_drops(fr, pace) + _pacing(cap, fr) + _net_spikes(cap) + _net_losses(cap)
           + _net_gaps(cap) + _marks(cap))
    evs.sort(key=lambda e: (e.t_start, KIND_ORDER[e.kind], e.t_end))
    for i, e in enumerate(evs, 1):
        e.id = i
    _link_marks(evs, bool(fr.get(F.T)))
    return evs


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


def _stutters(fr: dict[str, list], stats: dict[str, Any]) -> list[LagEvent]:
    rows, t, d, source = fa.display_series(fr)
    if len(d) < 2:
        return []
    med = _num(stats.get("median_frame_ms"))
    m = st.trailing_median(d, fa.baseline_frames(med if med is not None and med > 0 else st.median(d)))
    fm = fr.get(F.FRAME_MS) or []
    hits: list[dict[str, Any]] = []
    for j, (dj, mj) in enumerate(zip(d, m)):
        if mj is None or dj < STUTTER_X * mj or dj - mj < STUTTER_MIN_MS:
            continue
        own = _num(fa._at(fm, rows[j]))
        span = max(dj, float(own)) if own is not None and own > 0 else dj   # cover the frozen screen too
        hits.append({"start": t[j] - span / 1000.0, "end": t[j], "ms": dj, "base": mj, "t": t[j], "stall": False})

    # A stall can show only as a jump in t between consecutive rows (PM 2.4: with several target chains,
    # PresentMon drops the frames around a > 4 s stall).
    tt = fr.get(F.T) or []
    tail = st.median(d[-fa.baseline_frames(st.median(d)):])     # baseline after the last displayed frame
    for i in range(1, len(tt)):
        a, b = _num(tt[i - 1]), _num(tt[i])
        if a is None or b is None:
            continue
        gap_ms = (b - a) * 1000.0
        k = bisect.bisect_left(t, b)
        mk = m[k] if k < len(m) else tail
        if mk is None or gap_ms < STUTTER_X * mk or gap_ms - mk < STUTTER_MIN_MS:
            continue
        own = _num(fa._at(fm, i))
        if own is not None and own >= STALL_COVER * gap_ms:
            continue      # a long frame of its own: the display series judges it
        hits.append({"start": float(a), "end": float(b), "ms": gap_ms, "base": mk, "t": float(b), "stall": True})

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


# -------------------------------------------------------------------- FPS drops ---

def _displayed_times(fr: dict[str, list]) -> list[float]:
    t = fr.get(F.T) or []
    shown = fr.get(F.DISPLAYED) or []
    tracked = any(_num(v) in (0, 1) for v in shown)
    return [float(t[i]) for i in range(len(t)) if _num(t[i]) is not None
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

    A VSync lock at k refreshes per frame targets refresh / k (at k = 1, the refresh rate itself).
    """
    if pace.get("verdict") == "capped" and _num(pace.get("cap_fps")):
        return float(pace["cap_fps"]), "cap"
    hz, ft = _num(pace.get("refresh_hz")), _num(pace.get("ft_ms"))
    if pace.get("verdict") == "vsync" and hz and ft:
        k = max(1, round(ft / (1000.0 / hz)))
        return hz / k, "refresh"
    if fps:
        return st.percentile(fps, TARGET_PCTL), "p90"
    return None, None


def _fps_drops(fr: dict[str, list], pace: dict[str, Any]) -> list[LagEvent]:
    grid, fps = rolling_fps(_displayed_times(fr))
    target, origin = fps_target(pace, fps)
    if not grid or not target:
        return []
    step = grid[1] - grid[0] if len(grid) > 1 else FPS_STEP_S
    limit = FPS_DROP_FRAC * target
    out, run = [], []
    for g, v in zip(grid + [math.inf], fps + [math.inf]):
        if v < limit:
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

def _pacing(cap: Capture, fr: dict[str, list]) -> list[LagEvent]:
    """LC 2.4 pacing over app frames (DESIGN: bound and pacing use app frames only)."""
    rows, t, d, _source = fa.display_series(fr)
    keep = [j for j, i in enumerate(rows) if fa._is_app(fr, i)]
    if len(keep) < 2:
        return []
    fm, anim = fr.get(F.FRAME_MS) or [], fr.get(F.ANIM_ERROR_MS) or []
    hz = fa.refresh_hz(cap)
    period = 1000.0 / hz if hz else None
    t0 = t[keep[0]]
    windows: dict[int, list[int]] = {}
    for j in keep:
        windows.setdefault(int(math.floor((t[j] - t0) / PACING_WINDOW_S)), []).append(j)

    flagged: list[tuple[int, list[int]]] = []
    for k in sorted(windows):
        js = windows[k]
        swings = [abs(d[b] - d[a]) for a, b in zip(js, js[1:])]
        errs = [abs(float(v)) for v in (_num(fa._at(anim, rows[j])) for j in js) if v is not None]
        uneven = len(swings) >= PACING_MIN_SAMPLES and \
            sum(1 for s in swings if s >= PACING_SWING_MS) / len(swings) > PACING_SHARE
        jerky = len(errs) >= PACING_MIN_SAMPLES and st.percentile(errs, 95.0) > PACING_ANIM_MS
        if uneven or jerky:
            flagged.append((k, js))

    out = []
    i = 0
    while i < len(flagged):                  # adjacent flagged windows are one episode
        js = list(flagged[i][1])
        while i + 1 < len(flagged) and flagged[i + 1][0] == flagged[i][0] + 1:
            i += 1
            js += flagged[i][1]
        i += 1
        swings = [abs(d[b] - d[a]) for a, b in zip(js, js[1:])]
        big = [s for s in swings if s >= PACING_SWING_MS]
        errs = [abs(float(v)) for v in (_num(fa._at(anim, rows[j])) for j in js) if v is not None]
        pres = [float(v) for v in (_num(fa._at(fm, rows[j])) for j in js) if v is not None and v > 0]
        pres_sw = [abs(b - a) for a, b in zip(pres, pres[1:])]
        detail = {
            "swing_pct": 100.0 * len(big) / len(swings) if swings else None,
            "anim_p95_ms": st.percentile(errs, 95.0) if errs else None,
            "present_swing_pct": (100.0 * sum(1 for s in pres_sw if s >= PACING_SWING_MS) / len(pres_sw)
                                  if pres_sw else None),
            "quantised_pct": (100.0 * fa._quantised_share([d[j] for j in js], period) if period else None),
            "frames": len(js)}
        out.append(LagEvent(id=0, kind="pacing", t_start=t[js[0]] - d[js[0]] / 1000.0, t_end=t[js[-1]],
                            severity=st.median(big) if big else (st.median(swings) if swings else 0.0),
                            detail=detail))
    return out


# ---------------------------------------------------------------------- network ---

def _net_rows(cap: Capture, column: str) -> list[tuple[float, float | None]]:
    t, v = fa._col(cap.network, N.T), fa._col(cap.network, column)
    out = []
    for i, ti in enumerate(t):
        ti = _num(ti)
        if ti is None:
            continue
        vi = _num(fa._at(v, i))
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
    out = []
    gw = [(t, v) for t, v in _net_rows(cap, N.GW_RTT_MS) if v is not None]
    gw_t = [t for t, _ in gw]
    for target in ("internet", "server"):
        pts = [(t, v) for t, v in _net_rows(cap, _NET_COLS[target]) if v is not None]
        if len(pts) < 2:
            continue
        med = st.median([v for _, v in pts])
        floor: deque[tuple[float, float]] = deque()     # rolling minimum of the earlier samples
        hits: dict[float, tuple[float, float]] = {}
        for ti, vi in pts:
            while floor and floor[0][0] < ti - SPIKE_BASE_WINDOW_S:
                floor.popleft()
            if floor and vi - floor[0][1] >= SPIKE_RISE_MS and vi >= SPIKE_X_MEDIAN * med:
                hits[ti] = (vi, floor[0][1])
            while floor and floor[-1][1] >= vi:
                floor.pop()
            floor.append((ti, vi))
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


def _net_losses(cap: Capture) -> list[LagEvent]:
    nf = cap.net_flows if isinstance(cap.net_flows, dict) else {}
    targets = nf.get("targets") if isinstance(nf.get("targets"), dict) else {}
    out = []
    for role, col in _NET_COLS.items():
        rows = _net_rows(cap, col)
        probe = _role_entry(nf.get("probes"), role, targets)
        from_t = _num(probe.get("from_t")) if isinstance(probe, dict) else None
        listed = _role_entry(nf.get("lost"), role, targets)
        if isinstance(listed, list):
            lost = sorted(float(x) for x in listed if _num(x) is not None)
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
        for g in _group(lost, NET_MERGE_S):
            mid = (g[0] + g[-1]) / 2.0
            lo, hi = mid - LOSS_WINDOW_S / 2, mid + LOSS_WINDOW_S / 2
            lost_w = _count(lost, lo, hi)
            sent_w = max(lost_w, _count(sent_t, lo, hi))
            loss_pct = 100.0 * lost_w / sent_w
            burst = False
            for x in g:
                a, b = x - BURST_WINDOW_S / 2, x + BURST_WINDOW_S / 2
                lw = _count(lost, a, b)
                sw = max(lw, _count(sent_t, a, b))
                burst = burst or 100.0 * lw / sw >= BURST_PCT
            if loss_pct < LOSS_PCT and not burst:
                continue
            out.append(LagEvent(
                id=0, kind="net_loss", t_start=g[0] - NET_ROUND_S / 2, t_end=g[-1] + NET_ROUND_S / 2,
                severity=loss_pct, detail={"target": role, "lost": len(g), "lost_15s": lost_w, "sent_15s": sent_w,
                                           "loss_pct": loss_pct, "burst": burst}))
    return out


def _net_gaps(cap: Capture) -> list[LagEvent]:
    """Each gap the net_flows tracker reported, [t, ms, kind] with t the moment the server went quiet."""
    nf = cap.net_flows if isinstance(cap.net_flows, dict) else {}
    gaps = nf.get("gaps")
    out = []
    for g in gaps if isinstance(gaps, list) else []:
        if not isinstance(g, (list, tuple)) or len(g) < 2:
            continue
        t, ms = _num(g[0]), _num(g[1])
        kind = g[2] if len(g) > 2 and isinstance(g[2], str) else None
        if t is None or ms is None or ms < GAP_MIN_MS:
            continue
        out.append(LagEvent(id=0, kind="net_gap", t_start=float(t), t_end=float(t) + ms / 1000.0,
                            severity=float(ms), detail={"ms": float(ms), "kind": kind}))
    return out


# ------------------------------------------------------------------------ marks ---

def _marks(cap: Capture) -> list[LagEvent]:
    marks = cap.marks if isinstance(cap.marks, list) else []
    return [LagEvent(id=0, kind="mark", t_start=float(m) - MARK_BEFORE_S, t_end=float(m) + MARK_AFTER_S,
                     severity=0.0, detail={"mark_t": float(m)})
            for m in marks if _num(m) is not None]


def _link_marks(evs: list[LagEvent], frames_measured: bool) -> None:
    """LC 5.2: a mark whose window holds no frame event is lag that frame timing did not see."""
    for mk in (e for e in evs if e.kind == "mark"):
        over = [e for e in evs if e.kind != "mark" and e.t_start <= mk.t_end and mk.t_start <= e.t_end]
        frame_ids = [e.id for e in over if e.kind in ("stutter", "fps_drop", "pacing")]
        mk.detail["frame_events"] = frame_ids
        mk.detail["net_events"] = [e.id for e in over if e.kind.startswith("net_")]
        mk.detail["visible_in_frames"] = bool(frame_ids) if frames_measured else None


# ------------------------------------------------------------------ stats keys ---

def summary_stats(cap: Capture, events: Iterable[LagEvent]) -> dict[str, Any]:
    """The Diagnosis.stats keys that need events or the network table (DESIGN, Diagnosis.stats keys):
    stutters, stutters_per_min, swap_chains, ping_gw_ms / ping_inet_ms / ping_server_ms (medians),
    jitter_ms (internet anchor: mean consecutive difference, NE 2.1) and loss_pct (internet: lost / sent)."""
    out: dict[str, Any] = {}
    n = sum(1 for e in events if e.kind == "stutter")
    dur = _num(cap.duration_s)
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
    nf = cap.net_flows if isinstance(cap.net_flows, dict) else {}
    targets = nf.get("targets") if isinstance(nf.get("targets"), dict) else {}
    probe = _role_entry(nf.get("probes"), "internet", targets)
    sent = _num(probe.get("sent")) if isinstance(probe, dict) else None
    lost = _num(probe.get("lost")) if isinstance(probe, dict) else None
    out["loss_pct"] = 100.0 * lost / sent if sent and lost is not None and 0 <= lost <= sent else None
    return out
