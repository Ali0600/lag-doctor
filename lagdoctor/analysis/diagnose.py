"""The verdict on one capture: frames -> events -> bound -> attribution -> session rules -> ranking, lag type
and a one-sentence headline (DESIGN.md "Analysis", "Scoring", "Diagnosis.stats keys"; LC 5.3, 5.4).

Not measured is never fine: without frame timing the stats say nothing about stutter (None, not 0), the
lag type is "unknown" unless the network lagged, and the headline says what could not be checked.
"""
from __future__ import annotations

from typing import Any

from lagdoctor.analysis import attribute, events, frames, rules
from lagdoctor.model import F, Capture, Diagnosis, Finding, LagEvent
from lagdoctor.report import labels

STANDARD_SOURCES = ("frames", "system", "gpu", "network", "net_flows")   # DESIGN, result() table
HARDWARE_LIMITS = ("gpu", "cpu_main", "cpu_all", "mixed")   # bound verdicts where the PC itself holds FPS down
LOW_FPS_FRAC = rules.LIMIT_FPS_FRAC   # a hardware-held frame rate under 90 % of the refresh is low-FPS lag time
LOW_FPS_NO_REFRESH = 60.0    # [inferred] the reference when the display's refresh rate is unknown
MIXED_FRAC = 0.5             # [inferred] a runner-up with half the leader's lag time or more makes the lag "mixed"
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
    stats.update(events.summary_stats(cap, evs))
    stats["main_chain"] = next((c for c in fr.get(F.SWAP_CHAIN) or [] if isinstance(c, str)), None)
    ctx: dict[str, Any] = {"frames": fr, "stats": stats, "bound": b, "events": evs, "event_causes": summary}
    env = rules._env(cap, ctx)
    frames_ok = env.frames_measured
    if not frames_ok:
        stats["stutters"] = stats["stutters_per_min"] = None     # not measured, so never "0 stutters"
    findings, cleared, unchecked = rules.run_rules(cap, ctx)
    findings = rank(findings)
    lag = lag_time(evs, findings, stats, b, frames_ok)
    lag_type = pick_lag_type(lag, frames_ok)
    return Diagnosis(lag_type=lag_type, headline=headline(lag_type, findings, stats, evs, lag, env),
                     stats=stats, events=evs, findings=findings, measured=measured(cap, stats),
                     cleared=cleared, unchecked=unchecked)


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
    return out


def _union(spans: list[tuple[float, float]]) -> float:
    return rules._union_s((a, b) for a, b in spans if b > a)


def lag_time(evs: list[LagEvent], findings: list[Finding], stats: dict[str, Any], b: dict[str, Any],
             frames_ok: bool) -> dict[str, float]:
    """Seconds of lag per lag type. Stutters, FPS drops and network events count as they are; pacing counts only
    where a confident pacing finding claims it (a cap's display judder is not the game's pacing); a frame rate
    the hardware holds under the refresh counts its steady time as low FPS."""
    spans: dict[str, list[tuple[float, float]]] = {"stutter": [], "low_fps": [], "pacing": [], "network": []}
    claimed = {i for f in findings if f.category == "pacing" and f.confidence in CONFIDENT for i in f.event_ids}
    for ev in evs:
        span = (float(ev.t_start), float(ev.t_end))
        if ev.kind == "stutter":
            spans["stutter"].append(span)
        elif ev.kind == "fps_drop":
            spans["low_fps"].append(span)
        elif ev.kind == "pacing" and ev.id in claimed:
            spans["pacing"].append(span)
        elif ev.kind.startswith("net_"):
            spans["network"].append(span)
    out = {k: _union(v) for k, v in spans.items()}
    fps, hz = rules._fin(stats.get("avg_fps")), rules._fin(b.get("refresh_hz"))
    ref = LOW_FPS_FRAC * hz if hz else LOW_FPS_NO_REFRESH
    if frames_ok and b.get("verdict") in HARDWARE_LIMITS and fps is not None and fps < ref:
        out["low_fps"] += rules._fin(b.get("steady_s")) or 0.0
    return out


def pick_lag_type(lag: dict[str, float], frames_ok: bool) -> str:
    """LC 5.4 / DESIGN: the category with the most lag time; "mixed" when a second one comes close."""
    ranked = sorted(((v, k) for k, v in lag.items() if v > 0), reverse=True)
    if not ranked:
        return "none" if frames_ok else "unknown"
    if len(ranked) > 1 and ranked[1][0] >= MIXED_FRAC * ranked[0][0]:
        return "mixed"
    return ranked[0][1]


# --------------------------------------------------------------------------------- headline ---

def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _rule_of_three(stats: dict[str, Any]) -> str | None:
    """LC 5.4: zero hitches in n frames bounds the rate at 3/n (95 %), when n > 30 (frames.py sets the bound)."""
    n, bound = stats.get("frames"), rules._fin(stats.get("hitch_rate_bound"))
    if stats.get("hitches") == 0 and isinstance(n, int) and bound is not None:
        return f"no hitches ≥ 50 ms in {n:,} frames (at most {100 * bound:.2g}% of frames at 95% confidence)"
    return None


def _lag_part(kind: str, stats: dict[str, Any], evs: list[LagEvent], lag: dict[str, float],
              duration: float | None) -> str:
    if kind == "low_fps":
        fps, hz = rules._fin(stats.get("avg_fps")), rules._fin(stats.get("refresh_hz"))
        return "Low frame rate" + (f" ({fps:.0f} FPS average" + (f" on a {hz:.0f} Hz screen)" if hz else ")")
                                   if fps is not None else "")
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
        counts = [(sum(1 for e in evs if e.kind == k), w) for k, w in
                  (("net_spike", "ping spike"), ("net_loss", "packet-loss burst"), ("net_gap", "server gap"))]
        parts = [_plural(n, w) for n, w in counts if n]
        return "Network lag" + (f" ({', '.join(parts)})" if parts else "")
    return ""


LAG_EVENT_KINDS = {"stutter": ("stutter",), "low_fps": ("fps_drop",), "pacing": ("pacing",),
                   "network": ("net_spike", "net_loss", "net_gap")}
ALSO = {"high": "likely:", "medium": "possible:", "low": "worth checking:"}


def _lead(kinds: list[str], findings: list[Finding], evs: list[LagEvent]) -> tuple[Finding | None, bool]:
    """(the finding the headline names, whether it explains the lag): first the best-ranked finding that claims
    the lag's own events, then one of the lag's category, else the top finding as a side note."""
    ids = {e.id for e in evs for k in kinds if e.kind in LAG_EVENT_KINDS.get(k, ())}
    for f in findings:
        if ids & set(f.event_ids):
            return f, True
    for f in findings:
        if LAG_OF_CATEGORY.get(f.category) in kinds:
            return f, True
    return (findings[0], False) if findings else (None, False)


def headline(lag_type: str, findings: list[Finding], stats: dict[str, Any], evs: list[LagEvent],
             lag: dict[str, float], env: rules._Env) -> str:
    """One sentence: the lag, the finding that best explains it, and the rule-of-three line when there were no
    hitches (DESIGN, Scoring; LC 5.4)."""
    three = _rule_of_three(stats) if env.frames_measured else None
    if lag_type in LAG_OF_CATEGORY.values():
        kinds = [lag_type]
    elif lag_type == "mixed":
        kinds = [k for _, k in sorted(((v, k) for k, v in lag.items() if v > 0), reverse=True)][:2]
    else:
        kinds = []
    lead, explains = _lead(kinds, findings, evs)
    if lag_type == "mixed":
        parts = [_lag_part(k, stats, evs, lag, env.duration) for k in kinds]
        text = "Several problems: " + " and ".join([parts[0]] + [p[0].lower() + p[1:] for p in parts[1:]])
    elif kinds:
        text = _lag_part(kinds[0], stats, evs, lag, env.duration)
    elif lag_type == "unknown":
        text = f"Frame timing was not measured ({env.why('frames')}), so stutter and frame rate could not be checked"
    else:
        text = "No lag stood out"
        hitches = stats.get("hitches")
        if isinstance(hitches, int) and hitches > 0:
            text += f", though {_plural(hitches, 'frame')} took 50 ms or more"
        elif three:
            text += f": {three}"
            three = None
    if lead is not None:
        phrase = labels.cause_text(lead.cause)
        if lead.cause == "uneven_pacing" and lag_type == "pacing":
            text += ": the game itself hands over its frames at uneven intervals"
        elif explains:
            text += f", {CONNECT.get(lead.confidence, 'worth checking:')} {phrase}"
        else:
            text += f"; {ALSO.get(lead.confidence, 'worth checking:')} {phrase}"
    if lag_type == "network" and not env.frames_measured:
        text += "; frame timing was not measured, so stutter could not be checked"
    if three:
        text += f"; {three}"
    return rules._sentence(text)
