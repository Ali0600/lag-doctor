"""The part of a recording that is analysed: the capture cut to a span of its own time (docs/DESIGN.md, "Measuring
the right part"; docs/LAUNCHER-PLAN.md, M5).

A recording starts when the game is found, so it holds the game starting up: shader preparation, splash screens, menus
and loading. `crop(cap, t0, t1)` returns a new Capture that holds only what happened from t0 to t1, and the unchanged
diagnose() then runs on it. Times stay the recording's own, seconds from its t = 0: a cropped diagnosis's events, marks
and moments land on the whole recording's charts as they are, and a time the report shows is the time the player saw
on the status line. What the cut does:

- the four tables (frames, system, gpu, network), the other programs' samples and the lag-key presses keep their rows
  with t0 <= t <= t1, every column kept even where its cut is empty (analysis.window);
- a chain's first frame in the span is left out when its own interval began before t0, since it measured time outside
  the span (a start set at the end of a loading screen would make the whole wait its first frame);
- duration_s becomes the span's length, and `span` records the span, the recording's length and how it was set;
- net_flows: every list of times is cut to the span (lost probes, Wi-Fi scans, downloads, the server's packet bins),
  each silence of the game server is clipped to it, the probes are counted again over it, and the byte totals per
  program and over the whole recording become None, since they cannot be split by time (NET_FLOWS says which is which);
- a table source that recorded rows, but none in the span, is marked as having recorded nothing in the measured part.

Everything else (the inventory, the target, the notes, the start time and the clock origin) stays as recorded. Pure:
the capture it is given is never changed. A cropped capture is never saved (Capture.save refuses it), so the file
always holds the whole recording.
"""
from __future__ import annotations

import copy
import math
from dataclasses import replace
from typing import Any

from lagdoctor.analysis import frames as fa
from lagdoctor.analysis import window
from lagdoctor.model import F, N, Capture, SourceStatus
from lagdoctor.report.charts import fmt_elapsed

MIN_SPAN_S = fa.BOUND_MIN_S      # a shorter part could never get a verdict on what limits the game
HOWS = ("key", "set")            # from the start key's press; set by the player (analyze --from/--to, the app)
TABLES = ("frames", "system", "gpu", "network")
OK_STATES = ("ok", "partial")
EMPTY_IN_SPAN = "it recorded nothing in the measured part"
MARK_LATE_S = 1.0                # capture/session.py keeps a lag-key press up to 1 s after the recording stopped
SERVER_BIN_S = 1.0               # net_flows server_rx: [t, packets] for the second that ends at t
ROLE_COLUMNS = {"gateway": N.GW_RTT_MS, "internet": N.INET_RTT_MS, "server": N.SERVER_RTT_MS}
SERVER_TOTALS = ("rx_packets", "tx_packets", "rx_bytes", "tx_bytes")

# What the cut does to each net_flows key the collectors write (capture/session.py NETWORK_FLOW_KEYS and FLOW_KEYS).
# A key this version does not know becomes None: whatever it holds, nothing says it covers only the span.
NET_FLOWS = {
    "targets": "kept", "route": "kept", "download_sources": "kept", "talkers_scope": "kept", "layout": "kept",
    "health": "kept",
    "lost": "cut", "wifi_scans": "cut", "downloads": "cut", "server_rx": "cut",
    "gaps": "clipped", "server": "clipped",
    "probes": "counted again",
    "talkers": "dropped", "game_wire_bytes": "dropped",
}


def _time(v: Any) -> float | None:
    """A time from the capture when it is a real, finite number of seconds within frames' bounds."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    v = float(v)
    return v if abs(v) <= fa.MAX_ABS_VALUE else None


def recorded_s(cap: Capture) -> float:
    """The recording's length in seconds. ValueError when the capture does not say (a damaged one)."""
    dur = _time(cap.duration_s)
    if dur is None or dur <= 0:
        raise ValueError("the recording's length is not known, so no part of it can be picked out")
    return dur


def check_span(cap: Capture, t0: Any, t1: Any) -> tuple[float, float]:
    """The span [t0, t1] of `cap` that crop cuts, as seconds into the recording: an end past the recording's comes
    back as its end. ValueError, as one clause that reads after "cannot be analysed: ", for anything that is not a
    span of it: ends that are not numbers, a start before 0 or after the end, an end not after the start, or a part
    shorter than MIN_SPAN_S. The CLI and the app's Results check a span through this, so both refuse the same ones."""
    recorded = recorded_s(cap)
    lo, hi = _time(t0), _time(t1)
    if lo is None or hi is None:
        raise ValueError("its start and end must be numbers of seconds into the recording")
    if lo < 0:
        raise ValueError("it would start before the recording began")
    if lo >= recorded:
        raise ValueError(f"it would start at {fmt_elapsed(lo)}, and the recording ended at {fmt_elapsed(recorded)}")
    if hi <= lo:
        raise ValueError(f"its end ({fmt_elapsed(hi)}) is not after its start ({fmt_elapsed(lo)})")
    hi = min(hi, recorded)
    if hi - lo < MIN_SPAN_S:
        raise ValueError(f"it would be {hi - lo:.1f} s long, and a part needs at least {MIN_SPAN_S:.0f} s")
    return lo, hi


def crop(cap: Capture, t0: Any, t1: Any, how: str = "set") -> Capture:
    """`cap` cut to [t0, t1] (check_span's span), for diagnose(). `how`: "key" (from the start key's press) or "set"
    (the player chose it). ValueError for a span check_span refuses. The result's `span` is
    {"from_s", "to_s", "recorded_s", "how"}, which diagnose() copies into Diagnosis.stats["span"]."""
    if how not in HOWS:
        raise ValueError(f"not a way a span is set: {how!r}")
    lo, hi = check_span(cap, t0, t1)
    recorded = recorded_s(cap)
    w = window.window(cap, lo, hi)
    frames = _without_early_firsts(w["frames"], lo)
    late = hi + MARK_LATE_S if hi >= recorded else hi
    marks = [m for m in _list(cap.marks) if (v := _time(m)) is not None and lo <= v <= late]
    others = [row for row in _list(cap.others)
              if isinstance(row, (list, tuple)) and len(row) == 2 and (v := _time(row[0])) is not None
              and lo <= v <= hi]
    cut = {"frames": frames, "system": w["system"], "gpu": w["gpu"], "network": w["network"]}
    out = replace(cap, duration_s=hi - lo, others=others, marks=marks,
                  net_flows=_net_flows(cap.net_flows, lo, hi, w["network"]), sources=_sources(cap, cut),
                  target=copy.deepcopy(cap.target), inventory=copy.deepcopy(cap.inventory), notes=list(cap.notes),
                  **cut)
    out.span = {"from_s": lo, "to_s": hi, "recorded_s": recorded, "how": how}
    return out


# ------------------------------------------------------------------------------ tables ---

def _list(v: Any) -> list:
    return v if isinstance(v, list) else []


def _at(table: dict[str, list], name: str, i: int) -> Any:
    col = table.get(name)
    return col[i] if isinstance(col, list) and i < len(col) else None


def _rows(table: Any) -> int:
    """How many rows of a table have a time: a source with none recorded nothing."""
    return sum(1 for t in _list(table.get("t")) if _time(t) is not None) if isinstance(table, dict) else 0


def _without_early_firsts(fr: dict[str, list], lo: float) -> dict[str, list]:
    """The cut frame table less each chain's first row whose own interval began before `lo`: MsBetweenPresents (time
    since the chain's previous present) or MsBetweenDisplayChange (how long the frame before it stayed on screen). From
    the recording's own start (lo = 0) every row is kept, as the whole recording's analysis keeps PresentMon's first."""
    ts = fr.get(F.T)
    if lo <= 0 or not isinstance(ts, list):
        return fr
    first: dict[Any, tuple[float, int]] = {}
    for i, t in enumerate(ts):
        v = _time(t)
        if v is None:
            continue
        key = fa._chain_key(_at(fr, F.SWAP_CHAIN, i))
        if key not in first or v < first[key][0]:
            first[key] = (v, i)
    drop = set()
    for v, i in first.values():
        reach = max((x for x in (_time(_at(fr, F.FRAME_MS, i)), _time(_at(fr, F.DISPLAY_MS, i)))
                     if x is not None and x > 0), default=0.0)
        if v - reach / 1000.0 < lo:
            drop.add(i)
    if not drop:
        return fr
    return {name: [x for i, x in enumerate(col) if i not in drop] for name, col in fr.items()}


def _sources(cap: Capture, cut: dict[str, dict]) -> dict[str, Any]:
    """The capture's sources, each copied; a table source that recorded rows, but none in the span, says so."""
    sources = cap.sources if isinstance(cap.sources, dict) else {}
    out: dict[str, Any] = {}
    for name, s in sources.items():
        if not isinstance(s, SourceStatus):
            out[name] = s
        elif name in TABLES and s.state in OK_STATES and _rows(getattr(cap, name)) and not _rows(cut[name]):
            out[name] = SourceStatus("unavailable", EMPTY_IN_SPAN, s.tool)
        else:
            out[name] = replace(s)
    return out


# --------------------------------------------------------------------------- net_flows ---

def _net_flows(nf: Any, lo: float, hi: float, network: dict[str, list]) -> dict[str, Any]:
    """net_flows for the span, key by key as NET_FLOWS says. None stays None (it was not recorded), and a list whose
    cut is empty stays a list (it was recorded, and nothing fell in the span)."""
    nf = nf if isinstance(nf, dict) else {}
    out: dict[str, Any] = {}
    for key, value in nf.items():
        what = NET_FLOWS.get(key)
        if what == "kept":
            out[key] = copy.deepcopy(value)
        elif key == "lost":
            out[key] = {role: _times(v, lo, hi) for role, v in value.items()} if isinstance(value, dict) else None
        elif key == "wifi_scans":
            out[key] = _times(value, lo, hi)
        elif key == "downloads":
            out[key] = None if not isinstance(value, list) else [
                dict(d) for d in value if isinstance(d, dict) and (t := _time(d.get("t"))) is not None
                and lo <= t <= hi]
        elif key == "server_rx":
            out[key] = None if not isinstance(value, list) else [
                list(r) for r in value if isinstance(r, (list, tuple)) and len(r) >= 2
                and (t := _time(r[0])) is not None and t > lo and t - SERVER_BIN_S < hi]
        elif key == "gaps":
            out[key] = _gaps(value, lo, hi)
        elif key == "server":
            out[key] = _server(value, lo, hi)
        elif key == "probes":
            out[key] = _probes(value, nf.get("lost"), nf.get("targets"), lo, hi, network)
        else:                            # "dropped", and any key this version does not know
            out[key] = None
    return out


def _times(v: Any, lo: float, hi: float) -> list | None:
    if not isinstance(v, list):
        return None
    return [x for x in v if (t := _time(x)) is not None and lo <= t <= hi]


def _gaps(v: Any, lo: float, hi: float) -> list | None:
    """Each silence of the game server ([t, ms, kind]: t the last packet before it) clipped to the span: one that
    began before it starts at its start, one that outlasts it ends at its end, and one wholly outside it is gone."""
    if not isinstance(v, list):
        return None
    out = []
    for g in v:
        if not isinstance(g, (list, tuple)) or len(g) < 2:
            continue
        t, ms = _time(g[0]), _time(g[1])
        if t is None or ms is None or ms < 0:
            continue
        a, b = max(t, lo), min(t + ms / 1000.0, hi)
        if b > a:
            out.append([round(a, 6), round((b - a) * 1000.0, 3), *list(g[2:3])])
    return out


def _server(v: Any, lo: float, hi: float) -> dict | None:
    """The game server's flow for the span: its packet and byte counts, which cover the whole recording, become None,
    and when its packets came is clipped to the span. A flow whose packets all came outside the span was not seen in
    it, which is what None says."""
    if not isinstance(v, dict):
        return None
    out = {k: copy.deepcopy(x) for k, x in v.items() if k not in SERVER_TOTALS}
    out.update(dict.fromkeys(SERVER_TOTALS))
    first, last = _time(v.get("first_rx_t")), _time(v.get("last_rx_t"))
    if first is not None and last is not None:
        if last < lo or first > hi:
            return None
        out["first_rx_t"], out["last_rx_t"] = max(first, lo), min(last, hi)
    return out


def _probes(v: Any, lost: Any, targets: Any, lo: float, hi: float, network: dict[str, list]) -> dict | None:
    """The probes counted again over the span: lost, the target's lost-probe times in it; sent, those plus the span's
    rows that hold a round trip to that target. That is exact, because the network collector counts a probe as sent
    only when it was answered (its time in its round's row) or lost (its round's time in `lost`). Without the times of
    the lost probes (an older capture) the counts cannot be split by time, so they become None."""
    if not isinstance(v, dict) or not isinstance(lost, dict):
        return None
    targets = targets if isinstance(targets, dict) else {}
    out: dict[str, Any] = {}
    for key, probe in v.items():
        role = key if key in ROLE_COLUMNS else next(
            (r for r, ip in targets.items() if r in ROLE_COLUMNS and isinstance(ip, str) and ip == key), None)
        times = _times(lost.get(key), lo, hi)
        if not isinstance(probe, dict) or role is None or times is None:
            out[key] = None
            continue
        answered = sum(1 for x in _list(network.get(ROLE_COLUMNS[role])) if _time(x) is not None)
        out[key] = {"sent": answered + len(times), "lost": len(times), "from_t": probe.get("from_t")}
    return out
