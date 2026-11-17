"""Judge a Diagnosis against LagLab's ground-truth log.

Both sides use the QPC clock: LagLab logs time.perf_counter() values, and a
capture's times are perf_counter() minus Capture.clock_origin. The expected
answer for each mode is written here, from LagLab's own log, never read back
from the doctor's output: an expectation taken from the thing under test is
satisfied by construction. A mode is judged only on a run that kept LagLab's
window in front for the whole capture (front_check, also from LagLab's log):
otherwise the run, not the doctor, is what failed.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from lagdoctor.model import Diagnosis


@dataclass(frozen=True)
class Expectation:
    required_findings: tuple[str, ...] = ()   # a finding with each of these cause ids must exist
    forbidden_findings: tuple[str, ...] = ()  # and none of these
    event_cause: str | None = None            # truth moments must be matched by events naming this cause
    event_kinds: tuple[str, ...] = ("stutter",)
    min_recall: float = 0.8                   # share of truth moments the doctor found
    min_precision: float = 0.7                # share of the doctor's events that were real


EXPECTATIONS: dict[str, Expectation] = {
    "smooth": Expectation(forbidden_findings=("gpu_limit", "cpu_limit", "uneven_pacing", "game_cpu_spike")),
    "gpu": Expectation(required_findings=("gpu_limit",), forbidden_findings=("cpu_limit",)),
    "cpu": Expectation(required_findings=("cpu_limit",), forbidden_findings=("gpu_limit",)),
    "hitch": Expectation(event_cause="game_cpu_spike"),
    "hitch-io": Expectation(event_cause="asset_streaming"),
    "hitch-wait": Expectation(event_cause="game_stall"),
    "hog": Expectation(required_findings=("background_hogs",), event_cause="background_cpu",
                       event_kinds=("stutter", "fps_drop"), min_recall=0.6, min_precision=0.6),
    "pace": Expectation(required_findings=("uneven_pacing",)),
}


FRONT = "LagLab's window stayed in front"
WINDOWS_ONLY = "no stutters in the control but those Windows made"
# The causes that blame the game itself for a stutter: its own CPU or GPU work, its waiting, its reading from disk,
# its spill out of VRAM. Every other cause attribute.py names is in NOT_GAME (tests/test_e2e_truth.py holds the two
# together equal to attribute.CAUSE_ORDER, so a new cause has to take a side).
GAME_CAUSES = ("game_cpu_spike", "game_stall", "gpu_spike", "asset_streaming", "vram_overflow")
NOT_GAME = ("not_focused", "memory_paging", "gpu_throttle", "cpu_throttle", "background_disk", "background_gpu",
            "background_cpu", "driver_dpc")
# LC 2.4's stutter: a frame at least 2.5 times the usual and at least 8 ms over it, asked of LagLab's own frame starts
OWN_GAP_X = 2.5
OWN_GAP_MS = 8.0
OWN_MIN_FRAMES = 10           # fewer frame starts than this say nothing of LagLab's pace


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class Verdict:
    mode: str
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)


def time_away(front: Any, t0: float, t1: float) -> tuple[float, float | None]:
    """(seconds of [t0, t1] during which LagLab's window was not the foreground window, when the first of them
    began), from LagLab's log["front"]: [[perf_counter time, in front], ...], one entry per change, the first at its
    first frame. Before that frame LagLab had no window, so nothing counts there; a log without a usable list, or
    without any entry, counts as away from t0: unknown is not fine.

    The judge needs this because a mode run while LagLab's window was not in front says nothing about the doctor.
    The owner's run of 2026-11-09 lost frames in exactly the modes and stretches where the window was not in front:
    Windows showed nothing of the covered window, and PresentMon (default metrics) holds every frame that was not
    shown behind the last one that was, so hitch and hitch-wait got no rows at all; and the one stutter of the smooth
    control came 150 ms after the window lost the focus.
    """
    def entry(e: Any) -> bool:
        return (isinstance(e, (list, tuple)) and len(e) == 2 and isinstance(e[0], (int, float))
                and not isinstance(e[0], bool) and math.isfinite(e[0]) and isinstance(e[1], bool))

    changes = sorted((float(t), v) for t, v in front) if isinstance(front, list) and all(map(entry, front)) else []
    if not changes:
        return max(0.0, t1 - t0), t0
    away, first = 0.0, None
    state: bool | None = None                      # before the first entry LagLab had no window yet
    since = t0
    for t, v in changes + [(t1, None)]:
        end = min(max(t, t0), t1)
        if state is False and end > since:
            away += end - since
            first = since if first is None else first
        if t >= t1:
            break
        since, state = end, v
    return away, first


def front_check(log: dict[str, Any], clock_origin: float, capture_s: float) -> Check:
    """Whether LagLab's window was the foreground window for the whole capture (time_away)."""
    if isinstance(capture_s, bool) or not isinstance(capture_s, (int, float)) or not math.isfinite(capture_s) \
            or capture_s <= 0:
        return Check(FRONT, False, f"the capture has no length to judge ({capture_s!r} s)")
    away, first = time_away(log.get("front"), clock_origin, clock_origin + capture_s)
    if away == 0.0:
        return Check(FRONT, True, f"its window had the focus for all {capture_s:.1f} s of the capture")
    if "front" not in log:
        return Check(FRONT, False, "LagLab's log does not say whether its window was in front")
    if not isinstance(log["front"], list) or not log["front"]:
        return Check(FRONT, False, "LagLab's log of its window is empty, so whether it was in front is unknown")
    since = (first or clock_origin) - clock_origin
    return Check(FRONT, False, f"its window was not in front for {away:.1f} of {capture_s:.1f} s of the capture, "
                               f"from {since:.1f} s on: its frames may not have reached the screen (a covered "
                               "window shows none) and a change of focus makes it hitch; run it again and leave the "
                               "PC alone meanwhile")


def own_frames(log: dict[str, Any], clock_origin: float) -> list[float] | None:
    """LagLab's own frame starts (its log["frames"]) on the capture clock, sorted, or None when the log holds fewer
    than OWN_MIN_FRAMES of them or any that is no finite number."""
    frames = log.get("frames")
    if not isinstance(frames, list) or len(frames) < OWN_MIN_FRAMES or not all(
            isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in frames):
        return None
    return sorted(float(x) - clock_origin for x in frames)


def own_gaps(log: dict[str, Any], clock_origin: float) -> list[tuple[float, float]] | None:
    """(from, to) on the capture clock of each gap between LagLab's own frame starts as big as a stutter, or None
    without usable frame starts (own_frames). These are LagLab's own: a frame Windows held on screen while LagLab kept
    drawing is not among them (the owner's smooth run of 2026-11-11 held one frame 36 ms, while LagLab's longest gap
    was 16.4 ms against its usual 8.3)."""
    fs = own_frames(log, clock_origin)
    if fs is None:
        return None
    steps = sorted(b - a for a, b in zip(fs, fs[1:]))
    usual = steps[len(steps) // 2]
    return [(a, b) for a, b in zip(fs, fs[1:])
            if b - a >= OWN_GAP_X * usual and (b - a - usual) * 1000.0 >= OWN_GAP_MS]


def truth_moments(log: dict[str, Any], clock_origin: float) -> list[tuple[float, float]]:
    """When LagLab deliberately lagged, on the capture clock."""
    if log.get("hitches"):
        return [(h["start"] - clock_origin, h["end"] - clock_origin) for h in log["hitches"]]
    return [(a - clock_origin, b - clock_origin) for a, b in log.get("hog_bursts") or []]


def _overlaps(a: tuple[float, float], b: tuple[float, float], slack: float) -> bool:
    return a[0] - slack <= b[1] and b[0] - slack <= a[1]


def match_events(truth: list[tuple[float, float]], events: list[tuple[float, float]],
                 slack: float = 0.15) -> tuple[float, float]:
    """(recall, precision): truth moments with an overlapping event, events with an overlapping moment."""
    if not truth:
        return 1.0, (0.0 if events else 1.0)
    found = sum(1 for t in truth if any(_overlaps(t, e, slack) for e in events))
    real = sum(1 for e in events if any(_overlaps(t, e, slack) for t in truth))
    return found / len(truth), (real / len(events) if events else 0.0)


def judge(mode: str, log: dict[str, Any], clock_origin: float, d: Diagnosis,
          has_discrete_gpu: bool, *, capture_s: float) -> Verdict:
    """The checks for one mode. The first says whether the run itself can be judged (front_check): the capture's
    span, capture_s from clock_origin on, is when LagLab's window had to be in front."""
    exp = EXPECTATIONS[mode]
    v = Verdict(mode)
    v.checks.append(front_check(log, clock_origin, capture_s))
    causes = {f.cause for f in d.findings}
    for c in exp.required_findings:
        v.checks.append(Check(f"finding {c}", c in causes, f"findings: {sorted(causes)}"))
    for c in exp.forbidden_findings:
        v.checks.append(Check(f"no finding {c}", c not in causes, f"findings: {sorted(causes)}"))
    renderer = (log.get("gl_renderer") or "").lower()
    if has_discrete_gpu and "intel" in renderer:
        v.checks.append(Check("finding wrong_gpu", "wrong_gpu" in causes,
                              f"LagLab rendered on {log.get('gl_renderer')!r}; findings: {sorted(causes)}"))
    if exp.event_cause:
        truth = truth_moments(log, clock_origin)
        evs = [(e.t_start, e.t_end) for e in d.events if e.kind in exp.event_kinds]
        recall, precision = match_events(truth, evs)
        v.checks.append(Check("found the lag moments", recall >= exp.min_recall,
                              f"recall {recall:.2f} over {len(truth)} moments (need {exp.min_recall})"))
        v.checks.append(Check("no invented lag", precision >= exp.min_precision,
                              f"precision {precision:.2f} over {len(evs)} events (need {exp.min_precision})"))
        matched = [e for e in d.events if e.kind in exp.event_kinds
                   and any(_overlaps(t, (e.t_start, e.t_end), 0.15) for t in truth)]
        right = sum(1 for e in matched if e.causes and e.causes[0] == exp.event_cause)
        v.checks.append(Check(f"blamed {exp.event_cause}", bool(matched) and right / len(matched) >= 0.7,
                              f"{right}/{len(matched)} matched events name {exp.event_cause} first"))
    elif mode == "smooth":
        # LagLab's control never lags on purpose, but Windows can still hold its window's frames off the screen (the
        # owner chose, 2026-11-11, to pass such a stutter): one passes only where LagLab's own frame starts span it and
        # held no gap as big as a stutter there, and the doctor blamed nothing of the game's for it. A log without
        # frame starts cannot show that, so every stutter then fails, and so does one past the frames it logged.
        stutters = [e for e in d.events if e.kind == "stutter"]
        fs, gaps = own_frames(log, clock_origin), own_gaps(log, clock_origin)
        own = [e for e in stutters if fs is None or gaps is None or not fs[0] <= e.t_start <= e.t_end <= fs[-1]
               or any(_overlaps(g, (e.t_start, e.t_end), 0.15) for g in gaps)]
        blamed = [e for e in stutters if any(c in GAME_CAUSES for c in e.causes)]
        windows_only = [e for e in stutters if e not in own and e not in blamed]
        n = len(stutters)
        none_logged = " (its log has no usable frame starts)" if gaps is None and stutters else ""
        v.checks.append(Check(WINDOWS_ONLY, len(windows_only) == n,
                              f"{n} stutter{'' if n == 1 else 's'}: {len(own)} where LagLab's own frame starts held a "
                              f"gap that big or do not cover it{none_logged}, {len(blamed)} blamed on the game"))
        # nor may the verdict name lag of any kind, but for those stutters Windows made, with nothing of another kind
        # blamed on the game beside them (an FPS drop it was blamed for once passed under a "stutter" verdict). The
        # pacing a 120 FPS cap gives on a 144 Hz screen names no cause, and the verdict leaves it out.
        others = [e for e in d.events if e.kind not in ("stutter", "mark") and any(c in GAME_CAUSES for c in e.causes)]
        v.checks.append(Check("no lag in the control",
                              d.lag_type == "none" or (d.lag_type == "stutter" and bool(stutters)
                                                       and len(windows_only) == n and not others),
                              f"lag type {d.lag_type!r}" + (f", beside {len(others)} events of other kinds blamed on "
                                                            "the game" if others and d.lag_type == "stutter" else "")))
    return v
