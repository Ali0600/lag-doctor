"""The verdict on one capture: frames -> events -> bound -> attribution -> session rules -> ranking, lag type
and a one-sentence headline (DESIGN.md "Analysis", "Scoring", "Diagnosis.stats keys"; LC 5.3, 5.4).

Not measured is never fine: without frame timing the stats say nothing about stutter (None, not 0), the
lag type is "unknown" unless the network lagged, and the headline says what could not be checked.
"""
from __future__ import annotations

from typing import Any, Iterable

from lagdoctor.analysis import attribute, events, frames, rules
from lagdoctor.model import F, Capture, Diagnosis, Finding, LagEvent
from lagdoctor.report import labels
from lagdoctor.report.charts import fmt_elapsed

STANDARD_SOURCES = ("frames", "system", "gpu", "network", "net_flows")   # DESIGN, result() table
LOW_FPS_FRAC = 0.80          # LC 2.4 (S18, CHI '23), DECISIONS 2026-11-04: under 80 % of the target is low FPS
LOW_FPS_NO_REFRESH = 60.0    # [inferred] the target when the display's refresh rate is unknown
OWN_TARGET = ("capped", "vsync")   # the game holds its own rate (a cap, a VSync lock): that rate is its target
MIXED_FRAC = 0.5             # [inferred] a runner-up with half the leader's lag time or more makes the lag "mixed"
NET_MIN_EVENTS = 2           # [inferred] (A8) one network event never names the lag "network" on its own ...
NET_MIN_S = 1.0              # [inferred] ... unless it lasted max(1 s, 1 % of the capture): NE 2.2's levels are
NET_MIN_SHARE = 0.01         # 15 s window figures, and a single ICMP probe is often deprioritised (NE 2.4)
SEVERE_CLASSES = ("severe", "freeze")   # LC 2.3, 2.4: >= 150 ms cuts task success, >= 225 ms is never acceptable
CONFIDENT = ("high", "medium")
LAG_OF_CATEGORY = {"fps": "low_fps", "stutter": "stutter", "pacing": "pacing", "network": "network"}
CONNECT = {"high": "most likely", "medium": "possibly", "low": "worth checking:"}


def diagnose(cap: Capture) -> Diagnosis:
    fr = frames.main_chain(cap)
    stats = frames.frame_stats(fr, cap.duration_s)
    evs = events.find_events(cap, fr, stats)
    b = frames.bound(cap, fr, evs)
    summary = attribute.attribute(cap, fr, evs, b)
    stats.update(bound=b["verdict"], bound_r=b["r"], gpu_busy_pct=b["gpu_busy_pct"], refresh_hz=b["refresh_hz"])
    stats.update(headroom_stats(b))
    stats.update(attribute.nvidia_stats(cap))
    stats.update(events.summary_stats(cap, evs))
    stats["main_chain"] = next((c for c in fr.get(F.SWAP_CHAIN) or [] if isinstance(c, str)), None)
    ctx: dict[str, Any] = {"frames": fr, "stats": stats, "bound": b, "events": evs, "event_causes": summary}
    env = rules._env(cap, ctx)
    frames_ok = env.frames_measured
    if not frames_ok:
        stats["stutters"] = stats["stutters_per_min"] = None     # not measured, so never "0 stutters"
    findings, cleared, unchecked = rules.run_rules(cap, ctx)
    findings = rank(findings)
    stats["headroom"], stats["headroom_note"] = rules.headroom_line(env, findings)
    stats["marks"] = mark_list(evs)
    lag = lag_time(evs, findings, stats, b, frames_ok, dropped=_dropped_ids(env))
    lag_type = pick_lag_type(lag, frames_ok)
    return Diagnosis(lag_type=lag_type, headline=headline(lag_type, findings, stats, evs, lag, env),
                     stats=stats, events=evs, findings=findings, measured=measured(cap, stats),
                     cleared=cleared, unchecked=unchecked)


def headroom_stats(b: dict[str, Any]) -> dict[str, Any]:
    """Diagnosis.stats' bound and headroom figures (DESIGN, Diagnosis.stats keys; frames.headroom), each None where
    it was not measured. The sentence comes later, from the findings that carry it (rules.headroom_line)."""
    h = b.get("headroom") if isinstance(b.get("headroom"), dict) else {}
    cpu = h.get("cpu_fps") or (None, None)
    speedup = h.get("speedup") or (None, None)
    busiest = rules._fin(b.get("busiest_thread"))
    return {"steady_fps": b.get("steady_fps"), "bound_gpu_ms": b.get("gpu_ms"), "bound_frame_ms": b.get("ft_ms"),
            "busiest_thread_pct": 100.0 * busiest if busiest is not None else None,
            "gpu_ceiling_fps": h.get("gpu_fps"), "cpu_ceiling_low_fps": cpu[0], "cpu_ceiling_high_fps": cpu[1],
            "cpu_ceiling_from": h.get("cpu_from"), "display_steps_fps": h.get("steps_fps"),
            "next_limit": h.get("next"), "speedup_low": speedup[0], "speedup_high": speedup[1]}


def rank(findings: list[Finding]) -> list[Finding]:
    """DESIGN: by label, then S, then explained share of events, then whether an auto-fix exists. Finding.score
    encodes exactly that order (rules._score); ties keep the rule order."""
    return sorted(findings, key=lambda f: -f.score)


def measured(cap: Capture, stats: dict[str, Any]) -> dict[str, str]:
    """source -> "ok" or why it was not measured, for every source the capture lists and the standard ones."""
    out: dict[str, str] = {}
    for name, s in (cap.sources or {}).items():
        state = getattr(s, "state", None)
        detail = rules._text(getattr(s, "detail", None), 300)
        if state == "ok":
            out[name] = "ok"
        elif state == "partial":
            out[name] = f"partial: {detail}" if detail else "partial"
        else:
            out[name] = detail or (state if isinstance(state, str) else "not measured")
    for name in STANDARD_SOURCES:
        out.setdefault(name, "not recorded in this capture")
    if out.get("frames") == "ok" and not stats.get("frames"):
        out["frames"] = "PresentMon ran but recorded no frames from the game"
    for name, table in (("system", cap.system), ("gpu", cap.gpu), ("network", cap.network)):
        if out.get(name) == "ok" and not frames._col(table, "t"):   # the rules point here for why (rules._Env.need)
            out[name] = "it ran but recorded nothing"
    return out


# ------------------------------------------------------------------------------ marks ---

def mark_list(evs: list[LagEvent]) -> list[dict[str, Any]]:
    """CC-5: the user's lag-key presses for the report, each {t, events, causes}: the stutters and FPS drops in
    its window (never pacing, which can span the whole capture) and the network events in it, except a router
    that ignored pings (NE 7 row 9). `causes` are all the signals attribution found when frames were not measured,
    its best first: the first two once dropped other programs at 602 % of a CPU behind two ties (item 6)."""
    ignored = _ignored_losses(evs)
    out = []
    for mk in evs:
        if mk.kind != "mark":
            continue
        det = mk.detail if isinstance(mk.detail, dict) else {}
        t = rules._fin(det.get("mark_t"))
        ids = _ids(det.get("frame_events")) + [i for i in _ids(det.get("net_events")) if i not in ignored]
        out.append({"t": t if t is not None else float(mk.t_end) - events.MARK_AFTER_S, "events": sorted(set(ids)),
                    "causes": list(dict.fromkeys(c for c in mk.causes if isinstance(c, str)))})
    return out


def _ids(v: Any) -> list[int]:
    return [i for i in v if isinstance(i, int) and not isinstance(i, bool)] if isinstance(v, list) else []


# --------------------------------------------------------------------------- lag time ---

def _union(spans: list[tuple[float, float]]) -> float:
    return rules._union_s((a, b) for a, b in spans if b > a)


def _minus(spans: list[tuple[float, float]], cut: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """The parts of `spans` outside every span in `cut`."""
    out = []
    for a, b in spans:
        pieces = [(a, b)]
        for c, d in cut:
            pieces = [p for x, y in pieces for p in ((x, min(y, c)), (max(x, d), y)) if p[1] > p[0]]
        out += pieces
    return out


def _detail(ev: LagEvent) -> dict:
    return ev.detail if isinstance(ev.detail, dict) else {}


def _overlaps(a: LagEvent, b: LagEvent) -> bool:
    return a.t_start <= b.t_end and b.t_start <= a.t_end


def _local_gap(ev: LagEvent) -> bool:
    """NE 4.6, NE 7 row 7: a gap in which the game's own sending paused too is the game (or the PC) stalling."""
    return ev.kind == "net_gap" and _detail(ev).get("kind") == "local"


def _ignored_losses(evs: list[LagEvent]) -> set[int]:
    """A8, NE 7 row 9: packet loss on the router alone is the router ignoring pings to itself, no lag. It counts
    only where the internet or the server lagged at the same time (a ping spike, lost pings, a server gap)."""
    net = [e for e in evs if e.kind.startswith("net_")]
    gateway = [e for e in net if e.kind == "net_loss" and _detail(e).get("target") == "gateway"]
    others = [e for e in net if e not in gateway and not _local_gap(e)]
    return {g.id for g in gateway if not any(_overlaps(g, o) for o in others)}


def network_events(evs: list[LagEvent]) -> list[LagEvent]:
    """A8: the network events that are network lag. Not a router that ignored pings (_ignored_losses), and not a
    "local" gap, which is the game pausing its own traffic (_local_gap). An "open" gap, the server gone silent
    while the game kept sending, is a lost connection and counts."""
    ignored = _ignored_losses(evs)
    return [e for e in evs if e.kind.startswith("net_") and e.id not in ignored
            and not _local_gap(e)]


def _dropped_ids(env: rules._Env) -> set[int]:
    """A11: the pacing events of dropped-frame judder (frames made faster than a fixed refresh, never shown),
    by the one predicate dropped_frames and frame_cap share."""
    try:
        dj = rules._dropped_judder(env)
    except Exception:  # noqa: BLE001 - the rules that use it already reported their failure as "the check failed"
        return set()
    return {e.id for e in dj.events} if dj is not None else set()


def lag_time(evs: list[LagEvent], findings: list[Finding], stats: dict[str, Any], b: dict[str, Any],
             frames_ok: bool, dropped: Iterable[int] = ()) -> dict[str, float]:
    """Seconds of lag per lag type.

    - stutter: the stutter events' spans; low_fps: the FPS drops, plus the steady time of a game whose steady
      frame rate (bound "steady_fps": outside stutters and FPS drops, so freezes never lower it, A1) is under 80 %
      of its target (the refresh rate; a cap or a VSync lock is its own target, so never low FPS), whatever holds
      it there, the CPU, the GPU or neither (A6). A cadence other programs' GPU work may set is no cap (A7:
      frames.bound says "unknown" then).
    - pacing: only where a confident pacing finding claims it (a cap's uneven display hold is the cap's setting,
      not the game's pacing), or where any confident finding claims dropped-frame judder (`dropped`: frames never
      shown jerk the picture whichever rule names them, A11); never the time of a stutter or FPS drop (A1).
    - network: network_events(); none of it unless there were >= 2 such events or they lasted max(1 s, 1 % of the
      capture) (A8).
    """
    dropped = set(dropped)
    confident = [f for f in findings if f.confidence in CONFIDENT]
    paced = {i for f in confident for i in f.event_ids if f.category == "pacing" or i in dropped}
    spans: dict[str, list[tuple[float, float]]] = {"stutter": [], "low_fps": [], "pacing": [], "network": []}
    for ev in evs:
        span = (float(ev.t_start), float(ev.t_end))
        if ev.kind == "stutter":
            spans["stutter"].append(span)
        elif ev.kind == "fps_drop":
            spans["low_fps"].append(span)
        elif ev.kind == "pacing" and ev.id in paced:
            spans["pacing"].append(span)
    net = network_events(evs)
    spans["network"] = [(float(e.t_start), float(e.t_end)) for e in net]
    spans["pacing"] = _minus(spans["pacing"], spans["stutter"] + spans["low_fps"])
    out = {k: _union(v) for k, v in spans.items()}
    dur = rules._fin(stats.get("duration_s"))
    floor = max(NET_MIN_S, NET_MIN_SHARE * dur) if dur is not None and dur > 0 else NET_MIN_S
    if len(net) < NET_MIN_EVENTS and out["network"] < floor:
        out["network"] = 0.0
    out["low_fps"] += steady_low_s(b, frames_ok)
    return out


def steady_low_s(b: dict[str, Any], frames_ok: bool) -> float:
    """The steady seconds that count as low FPS (A6): the steady time of a game whose steady frame rate is under 80 %
    of its target, whatever holds it there; none for a cap or a VSync lock, which is its own target."""
    fps, hz = rules._fin(b.get("steady_fps")), rules._fin(b.get("refresh_hz"))
    target = hz if hz is not None and hz > 0 else LOW_FPS_NO_REFRESH
    if frames_ok and b.get("verdict") not in OWN_TARGET and fps is not None and fps < LOW_FPS_FRAC * target:
        return max(0.0, rules._fin(b.get("steady_s")) or 0.0)
    return 0.0


def pick_lag_type(lag: dict[str, float], frames_ok: bool) -> str:
    """LC 5.4 / DESIGN: the category with the most lag time; "mixed" when a second one comes close."""
    ranked = sorted(((v, k) for k, v in lag.items() if v > 0), reverse=True)
    if not ranked:
        return "none" if frames_ok else "unknown"
    if len(ranked) > 1 and ranked[1][0] >= MIXED_FRAC * ranked[0][0]:
        return "mixed"
    return ranked[0][1]


# --------------------------------------------------------------------------------- headline ---

def _plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def _rule_of_three(stats: dict[str, Any], evs: list[LagEvent]) -> str | None:
    """LC 5.4: zero hitches in n frames bounds the rate at 3/n (95 %), when n > 30 (frames.py sets the bound).
    Never next to a stutter event (a stall included) of 50 ms or more (A4)."""
    n, bound = stats.get("frames"), rules._fin(stats.get("hitch_rate_bound"))
    if any(e.kind == "stutter" and (rules._fin(e.severity) or 0.0) >= frames.HITCH_MS for e in evs):
        return None
    if stats.get("hitches") == 0 and isinstance(n, int) and bound is not None:
        return f"no hitches ≥ 50 ms in {n:,} frames (at most {100 * bound:.2g}% of frames at 95% confidence)"
    return None


def _count(v: Any) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0


def _severe(stats: dict[str, Any], evs: list[LagEvent]) -> str | None:
    """A1: "N severe hitches and M freezes, worst X ms" (LC 2.4 classes: severe >= 150 ms, freeze >= 500 ms). The
    frame stats count frames (severe_hitches holds the freezes too), the stutter events of those classes count
    episodes; the larger count is taken, so neither a missing stat nor a merged episode hides one."""
    worst_evs = [e for e in evs if e.kind == "stutter" and _detail(e).get("class") in SEVERE_CLASSES]
    severe = max(_count(stats.get("severe_hitches")), len(worst_evs))
    freezes = min(severe, max(_count(stats.get("freezes")),
                              sum(1 for e in worst_evs if _detail(e).get("class") == "freeze")))
    if severe <= 0:
        return None
    parts = ([_plural(severe - freezes, "severe hitch", "severe hitches")] if severe > freezes else []) + \
            ([_plural(freezes, "freeze")] if freezes > 0 else [])
    worst = max((v for v in (rules._fin(e.severity) for e in worst_evs) if v is not None), default=None)
    worst = worst if worst is not None else rules._fin(stats.get("worst_frame_ms"))
    return " and ".join(parts) + (f", worst {worst:.0f} ms" if worst is not None else "")


def _lag_part(kind: str, stats: dict[str, Any], evs: list[LagEvent], lag: dict[str, float],
              duration: float | None, bound: dict[str, Any] | None = None) -> str:
    if kind == "low_fps":
        fps, hz = rules._fin(stats.get("avg_fps")), rules._fin(stats.get("refresh_hz"))
        inner = [f"{fps:.0f} FPS average" + (f" on a {hz:.0f} Hz screen" if hz else "")] if fps is not None else []
        if stats.get("bound") == "waiting":      # A6, LC 3.2: the game waits on something else
            busiest = rules._fin((bound or {}).get("busiest_thread"))
            if busiest is not None and busiest >= rules.THREAD_WAITING:    # LC 3.2's grey zone: the CPU stays open
                inner.append(f"the GPU is not at its limit and the CPU may be close to it, its busiest thread at "
                             f"{100 * busiest:.0f}%")
            else:
                inner.append("neither the CPU nor the GPU is at its limit")
        return "Low frame rate" + (f" ({'; '.join(inner)})" if inner else "")
    if kind == "stutter":
        st_evs = [e for e in evs if e.kind == "stutter"]
        per_min = rules._fin(stats.get("stutters_per_min"))
        worst = max((float(e.severity) for e in st_evs), default=0.0)
        return f"{_plural(len(st_evs), 'stutter')} (" + (f"{per_min:.1f} a minute, " if per_min is not None else "") + \
            f"worst {worst:.0f} ms)"
    if kind == "pacing":
        share = lag.get("pacing", 0.0) / duration if duration else None
        return "Uneven frame pacing" + (f" over {100 * min(share, 1.0):.0f}% of the capture" if share else "")
    if kind == "network":
        net = network_events(evs)
        lost = [e for e in net if e.kind == "net_gap" and _detail(e).get("kind") == "open"]
        counts = [(sum(1 for e in net if e.kind == "net_spike"), "ping spike"),
                  (sum(1 for e in net if e.kind == "net_loss"), "packet-loss episode"),
                  (sum(1 for e in net if e.kind == "net_gap") - len(lost), "server gap")]
        parts = [_plural(n, w) for n, w in counts if n]
        if lost:                                 # an "open" gap runs to the end: the connection was lost
            parts.append(f"the game server stopped answering at {fmt_elapsed(min(e.t_start for e in lost))}")
        return "Network lag" + (f" ({', '.join(parts)})" if parts else "")
    return ""


LAG_EVENT_KINDS = {"stutter": ("stutter",), "low_fps": ("fps_drop",), "pacing": ("pacing",),
                   "network": ("net_spike", "net_loss", "net_gap")}
ALSO = {"high": "likely:", "medium": "possible:", "low": "worth checking:"}
# A6: the findings that explain a steady frame rate the bound verdict's part holds: that part at its limit, or what
# slows it. Every "fps" rule is in one of these or in NOT_STEADY_LEADS (a test holds the split whole).
STEADY_LEADS = {"gpu": ("gpu_limit", "gpu_throttle", "wrong_gpu", "hybrid_copy", "gpu_contention"),
                "cpu_main": ("cpu_limit", "cpu_throttle"), "cpu_all": ("cpu_limit", "cpu_throttle")}
NOT_STEADY_LEADS = ("not_focused",)      # a frame-rate cause, but only while the game is in the background


def _lead(kinds: list[str], findings: list[Finding], evs: list[LagEvent], verdict: str = "unknown",
          steady_s: float = 0.0) -> tuple[Finding | None, bool, Finding | None]:
    """(the finding the headline names, whether it explains the lag, a side note): the best-ranked finding that claims
    the lag's own events, unless the steady low-FPS time (`steady_s`) is longer than the time of the events it claims:
    then the best confident finding of the part the bound verdict names, which holds that steady rate (A6). With both,
    the other is the side note, so a mixed lag names the cause of each part and the claimer the headline used to name
    stays in it; never one finding twice. Then one of the lag's category, else the top finding as a side note."""
    ids = {e.id for e in evs for k in kinds if e.kind in LAG_EVENT_KINDS.get(k, ())}
    claimer = next((f for f in findings if ids & set(f.event_ids)), None)
    holder = (next((f for f in findings if f.cause in STEADY_LEADS.get(verdict, ()) and f.confidence in CONFIDENT), None)
              if steady_s > 0 else None)
    claimed = set(claimer.event_ids) & ids if claimer is not None else set()
    if holder is not None and steady_s > _union([(e.t_start, e.t_end) for e in evs if e.id in claimed]):
        return holder, True, claimer if claimer is not holder else None
    if claimer is not None:
        return claimer, True, holder if holder is not claimer else None
    for f in findings:
        if LAG_OF_CATEGORY.get(f.category) in kinds:
            return f, True, None
    return (findings[0], False, None) if findings else (None, False, None)


def headline(lag_type: str, findings: list[Finding], stats: dict[str, Any], evs: list[LagEvent],
             lag: dict[str, float], env: rules._Env) -> str:
    """One sentence: the lag, the finding that best explains it, severe hitches and freezes whatever the lag type
    (A1), and the rule-of-three line when there were no hitches (DESIGN, Scoring; LC 5.4)."""
    three = _rule_of_three(stats, evs) if env.frames_measured else None
    if lag_type in LAG_OF_CATEGORY.values():
        kinds = [lag_type]
    elif lag_type == "mixed":
        kinds = [k for _, k in sorted(((v, k) for k, v in lag.items() if v > 0), reverse=True)][:2]
    else:
        kinds = []
    steady = steady_low_s(env.bound, env.frames_measured) if "low_fps" in kinds else 0.0
    lead, explains, also = _lead(kinds, findings, evs, env.verdict, steady)
    severe = _severe(stats, evs) if env.frames_measured and "stutter" not in kinds else None
    if lag_type == "mixed":
        parts = [_lag_part(k, stats, evs, lag, env.duration, env.bound) for k in kinds]
        text = "Several problems: " + " and ".join([parts[0]] + [p[0].lower() + p[1:] for p in parts[1:]])
    elif kinds:
        text = _lag_part(kinds[0], stats, evs, lag, env.duration, env.bound)
    elif lag_type == "unknown":       # a short reason: its path and command are in the Not measured list, once
        text = (f"Frame timing was not measured ({labels.short_reason(env.why('frames'))}), so stutter and frame rate "
                "could not be checked")
    else:
        text = "No lag stood out"
        hitches = stats.get("hitches")
        if isinstance(hitches, int) and hitches > 0:
            text += f", though {_plural(hitches, 'frame')} took 50 ms or more"
            worst = rules._fin(stats.get("worst_frame_ms"))
            text += f" (worst {worst:.0f} ms)" if worst is not None else ""
            severe = None                        # the worst frame is named already
        elif three:
            text += f": {three}"
            three = None
    if lead is not None:
        phrase = labels.cause_text(lead.cause)
        if lead.cause == "uneven_pacing" and "pacing" in kinds:    # "uneven pacing, possibly uneven pacing" no more
            text += ": the game itself hands over its frames at uneven intervals"
        elif explains:
            text += f", {CONNECT.get(lead.confidence, 'worth checking:')} {phrase}"
        else:
            text += f"; {ALSO.get(lead.confidence, 'worth checking:')} {phrase}"
    if also is not None:                         # what claims the events, beside the limit that took the lead
        text += f"; {ALSO.get(also.confidence, 'worth checking:')} {labels.cause_text(also.cause)}"
    if severe:
        text += f"; also {severe}"
    if lag_type == "network" and not env.frames_measured:
        text += "; frame timing was not measured, so stutter could not be checked"
    if three:
        text += f"; {three}"
    return rules._sentence(text)
