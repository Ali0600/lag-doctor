"""Session rules and scoring: what the whole capture says about each cause of lag.

DESIGN.md "Session rules" and "Scoring" (LC 4, 5; NE 2, 7). Each Rule is a pure function fn(cap, ctx) that
collects evidence items, each in one evidence family and weighted by LC 5.1's points, and hands them to the
scorer. The scorer returns a Finding (with a confidence), a Cleared entry (what was measured that rules the
cause out), an Unchecked entry (why it could not be checked), or None when the measurements said nothing
either way ("evidence or silence", DESIGN principle 2). run_rules() checks each rule's sources first and
holds side-specific findings to the bound verdict's gate (LC 5.1).

Constants cite their research section: LC = docs/research/lag-causes.md, NE = network-and-etw.md,
WT = windows-telemetry.md, PM = presentmon.md. Thresholds tagged [inferred] there are defaults to tune.

A capture can come from disk, so every value read here is checked (a non-number counts as missing), and
nothing taken from the capture (process names, adapter names, paths) is trusted beyond being text.
"""
from __future__ import annotations

import bisect
import math
import ntpath
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from lagdoctor.analysis import attribute
from lagdoctor.analysis import events as ev_mod
from lagdoctor.analysis import frames as fa
from lagdoctor.analysis import stats as st
from lagdoctor.model import F, G, N, S, Capture, Evidence, Finding, FixSuggestion, LagEvent
from lagdoctor.report import labels

# ---- scoring (LC 5.1, 5.3) -----------------------------------------------------------------------
W_STATIC = 1.0               # LC 5.1: a static setting that makes a cause more likely
W_STEADY = 2.0               # LC 5.1: a steady-state metric past its threshold
W_EVENT_STRONG = 3.0         # LC 5.1, 5.2: an event-linked signal with lift >= 4
W_DIRECT = 4.0               # LC 5.1: a direct readout of the cause
STATIC_CAP = 2.0             # LC 5.1: static configuration adds at most +2 (so it never makes "likely" alone)
LIKELY_S = 4.0               # LC 5.3: Likely needs S >= 4 ...
LIKELY_COVERAGE = 0.6        # LC 5.3: ... the key family supporting, and coverage >= 0.6
POSSIBLE_S = 2.0             # LC 5.3: Possible: 2 <= S < 4
CLEARED_S = -2.0             # LC 5.3: Checked, fine: S <= -2 ...
CLEARED_COVERAGE = 0.6       # LC 5.3: ... and coverage >= 0.6
MIN_COVERAGE = 0.5           # LC 5.3: Not enough data below coverage 0.5
CONTRA_WEIGHT = 2.0          # LC 5.2: an event-linked "contradicts" weighs 2
HALF = 0.5                   # LC 5.2: marks stand in for frame events at half weight

# Side-specific causes need a bound verdict on their side (LC 5.1; DESIGN, Session rules). "gpu_event" is for
# a GPU-side stutter cause: the stutter's own frame split already chose its side (attribute.py), but no
# GPU-side cause is reported while one CPU thread or every core limits the game.
GATES = {
    "gpu": frozenset({"gpu", "mixed"}),
    "cpu": frozenset({"cpu_main", "cpu_all", "mixed", "unknown"}),
    "gpu_event": frozenset({"gpu", "mixed", "capped", "vsync", "waiting", "unknown"}),
}

# ---- limits (LC 3.1, 3.4, 4.1, 4.4, 4.14) --------------------------------------------------------
LIMIT_FPS_FRAC = 0.90        # [inferred, LC 4.14's 0.9 x refresh]: at or above it the display sets the pace,
                             # so a GPU or CPU limit costs nothing the player sees
ENGINE_BUSY_PCT = 90.0       # LC 4.1: the game's 3D engine >= 90 % on its render adapter
ENGINE_IDLE_PCT = 50.0       # [inferred] a 3D engine under half busy cannot be the limit
ENGINE_PROBABLE_PCT = fa.ENGINE_GPU_PCT   # LC 3.4: no frame timing -> 3D engine >= 95 % = "GPU-limited (probable)"
NV_UTIL_PCT = 95.0           # LC 4.1: utilization.gpu >= 95 %
THREAD_WAITING = 0.60        # LC 3.2: "Neither" (waiting) needs the busiest thread below 0.6 of a CPU; from here to
                             # frames.THREAD_BOUND (0.85) is a grey zone where the CPU is not ruled out (A6)
OTHER_GPU_PCT = fa.SHARED_GPU_PCT             # LC 4.6, 10 row 4.6: another process on the game adapter's engine >= 10 %
# ---- throttling (LC 4.2, 4.5, 10) ----------------------------------------------------------------
NV_THROTTLE_PCT = attribute.NV_THROTTLE_PCT   # LC 10 (4.2): held down by heat or power >= 30 % of the time
CPU_LIMIT_SHARE = attribute.LIMIT_SHARE       # LC 4.5: % Performance Limit < 100 on >= 25 % of samples
CPU_PERF_DROP = attribute.PERF_DROP_FRAC      # LC 4.5: % Processor Performance >= 20 % below the start
CPU_BUSY_FOR_THROTTLE = 0.70  # LC 4.5: throttling matters when the game is CPU-bound or near it (thread >= 0.7)
PERF_BASE_S = 120.0          # LC 4.5: "the run's first two minutes"
PERF_MIN_SAMPLES = 8         # [inferred] fewer samples on either side of the comparison say nothing
CPU_SLOW_PCT = 100.0 * (1.0 - CPU_PERF_DROP)   # [inferred, LC 4.5's 20 % drop measured against the base clock]: a
                             # median % Processor Performance at or below 80 % of base ran below full speed most of the
                             # time. The first real capture: median 70 % for 14 minutes in a quiet mode Windows
                             # reported no limit for (% Performance Limit 100 throughout)
# ---- memory (LC 4.3, 4.7) ------------------------------------------------------------------------
VRAM_FULL_FRAC = attribute.VRAM_FULL_FRAC     # LC 4.3: dedicated VRAM >= 90 % of its size
SHARED_SPILL_MB = 512.0      # LC 4.3: the game's shared GPU memory > 512 MiB ...
SHARED_RISE_MB_PER_MIN = 256.0   # LC 4.3: ... or rising >= 256 MiB per minute
RAM_WARN_FRAC = attribute.RAM_LOW_FRAC        # LC 4.7 (PAL, S30): available < 10 % of RAM is a warning
RAM_CRIT_FRAC = 0.05         # LC 4.7 (PAL, S30): < 5 % is critical ...
RAM_CRIT_MB = attribute.RAM_LOW_MB            # LC 4.7 (PAL, S30): ... as is < 64 MB
FAULTS_WARN_PS = attribute.FAULTS_RISE_PS     # LC 4.7 (PAL, S30): Pages Input/sec > 1000 is a warning
COMMIT_WARN_PCT = 80.0       # LC 4.7 (PAL, S30): % Committed Bytes In Use > 80 % warning ...
COMMIT_CRIT_PCT = 90.0       # LC 4.7 (PAL, S30): ... > 90 % critical
DISK_READ_WARN_MS = attribute.DISK_READ_MS    # LC 4.8 (PAL, S30): Avg. Disk sec/Read > 15 ms is a warning
# ---- display and pacing (LC 2.4, 3.3, 4.13, 4.14; DESIGN) ----------------------------------------
REFRESH_SLACK_HZ = 1.0       # [inferred] 143.98 Hz on a "144 Hz" mode is the maximum, not below it
JUDDER_SHARE = 0.10          # LC 2.4: VSync judder = median D one period and >= 10 % of intervals at 2 periods
QUANTISED_PCT = 100.0 * fa.VSYNC_SHARE        # LC 3.3: >= 90 % of displayed intervals on whole refresh periods
CAP_FRAC = 0.90              # LC 4.14: a cap at C < 0.9 x refresh is misconfigured ...
CAP_R_MAX = 0.70             # LC 4.14: ... when r < 0.7 (the GPU has room to go faster)
UNEVEN_SHARE = 0.10          # DESIGN: uneven pacing covers >= 10 % of the capture
UNEVEN_PRESENT_PCT = 100.0 * ev_mod.PACING_SHARE   # LC 2.4's "> 10 % of swings >= 4 ms", on the game's presents
DROPPED_PCT = 100.0 * JUDDER_SHARE   # [inferred, LC 2.4's 10 % judder share] frames never shown while the game
                             # presents faster than a fixed refresh: dropped-frame judder (A11)
# ---- background, focus, disk, shaders (DESIGN; LC 4.6, 4.8, 4.9, 4.19) ---------------------------
HOG_MIN_CPU_PCT = 25.0       # DESIGN: non-game processes summing >= 25 % of one CPU across the lag events
UNFOCUSED_SHARE = 0.10       # [inferred] not the foreground window for >= 10 % of the capture
SIDE_SHARE = 0.70            # [inferred] the CPU side was slow in >= 70 % of a cause's events
EARLY_FRACTION = 1.0 / 3.0   # LC 4.9: shader stutter bunches up early ("first minutes") ...
EARLY_SHARE = 0.60           # [inferred] ... here: >= 60 % of the game's own stutters in the first third
MIN_EVENTS = attribute.LIFT_MIN_EVENTS        # LC 2.6, 5.2: a stutter cause needs >= 5 events
# ---- network (NE 2.2, 2.3, 3.3, 3.5, 7) ----------------------------------------------------------
INET_NOTICEABLE_MS = 60.0    # NE 2.2: internet / server RTT p50 >= 60 ms is noticeable ...
INET_BAD_MS = 100.0          # NE 2.2: ... > 100 ms is bad
GW_BAD_P95_MS = 30.0         # NE 2.2: gateway RTT on Wi-Fi p95 > 30 ms is bad ...
GW_GOOD_P95_MS = 10.0        # NE 2.2: ... < 10 ms is good
GW_LOCAL_MS = 10.0           # [inferred] a router median above this means the delay starts on the local hop
RSSI_WEAK_DBM = -70.0        # NE 7 row 2: RSSI < -70 dBm is a weak signal
BLOAT_UTIL = 0.70            # NE 2.3: loaded = throughput >= 70-80 % of capacity
BLOAT_IDLE_UTIL = 0.20       # NE 2.3: the idle baseline is taken while throughput < 20 %
BLOAT_RISE_MS = 30.0         # NE 2.3: anchor RTT p50 - idle baseline >= 30 ms
BLOAT_MIN_BPS = 1e6          # [inferred] a peak under 1 MB/s (8 Mbit/s) never loaded a broadband link
BLOAT_MIN_ROWS = 4           # [inferred] loaded and idle rounds each needed to compare
SCAN_BEFORE_S = 3.0          # NE 3.5, 7 row 1 (corrected window): a scan's cost shows in [-3 s, +1 s] ...
SCAN_AFTER_S = 1.0
SCAN_SPIKE_MS = 20.0         # [inferred] ... as a router ping >= 20 ms above (and >= 2x) its normal level
SCAN_ALIGNED_MIN = 3         # NE 7 row 1: high when >= 3 aligned spikes
ROAM_SPIKE_S = 3.0           # [inferred] a ping spike this close to an access-point change goes with it
SERVER_GAPS_MIN = 3          # [inferred] repeated gaps, not one, point at the server or its route
SERVER_COVER = 0.5           # [inferred] "the server's packets kept arriving" needs them seen over most of the capture
SERVER_GAP_KINDS = ("delay", "loss")   # NE 4.6, DESIGN net_flows: the server's packets late (a burst after), or lost
TALKER_MIN_BPS = 1e6         # [inferred] a non-game process moving >= 1 MB/s on average is a bandwidth user
NIC_MIN_ROWS = 10            # [inferred] probe rounds with the adapter's byte counters read, to clear a bandwidth hog
DOWNLOAD_NEAR_S = 5.0        # [inferred] pings this close to a logged download rate count as "during" it

OK_STATES = ("ok", "partial")
BALANCED_GUID = "381b4222-f694-41f0-9685-ff5bb260df2e"   # WT 7.1: GUID_TYPICAL_POWER_SAVINGS (Balanced)
QUIET_PLAN_WORDS = ("silent", "quiet", "power saver", "battery saver", "eco", "whisper")   # [inferred] whole words
PERFORMANCE_PLAN_WORDS = ("performance", "turbo")        # [inferred] plans worth switching to; whole words
# A plan's processor settings (inventory power.plan, winapi/power.PLAN_SETTINGS). An ASUS plan named "Silent" here
# set the CPU exactly as Balanced does (powercfg, 2026-11-05), so a plan is judged by these when they were read.
PLAN_FULL_CPU = 100          # maximum processor state, %
PLAN_BOOST_OFF = 0           # PERFBOOSTMODE 0 = Disabled
PLAN_COOLING_PASSIVE = 0     # SYSCOOLPOL 0 = Passive: slow the CPU before speeding up the fans (Windows' battery default)
LOW_POWER_MODES = ("battery_saver", "better_battery")    # LC 4.12: effective mode 0 / 1
HARD_DISK = "HDD"            # DESIGN inventory game.media (WT 9.5)
SOLID_STATE = "SSD"
# Values that end up in a fix's params come from the capture file, so they are checked for shape first.
DEVICE_RE = re.compile(r"\\\\\.\\DISPLAY\d{1,3}")                  # DESIGN refresh_rate: "\\.\DISPLAY1"
GUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
EXE_MAX_CHARS = 1024         # [inferred] longer than any real install path, short enough to bound

_num = fa._num


# ------------------------------------------------------------------------------- results ---

@dataclass
class Cleared:
    """A cause that was checked and ruled out, with what was measured. `firm`: a reason that stands even where
    the capture holds the cause open (_holds_open), e.g. the frame rate already reaching the display."""

    cause: str
    text: str
    firm: bool = False


@dataclass
class Unchecked:
    """A cause that could not be checked, with why."""

    cause: str
    text: str


@dataclass
class Rule:
    """DESIGN's rule record. `family` is the key family (LC 10); `alt` families may stand in for it, but only
    to support a cause (LC 4.1: "probable" from utilisation alone). `needs` are sources that must all have been
    measured ("a|b": either one); `gate` names the bound-verdict gate a Finding must pass."""

    cause: str
    family: str
    needs: tuple[str, ...]
    gate: str | None
    fn: Callable[[Capture, dict], Any]
    category: str = "setup"
    alt: tuple[str, ...] = ()


@dataclass
class _Item:
    family: str
    weight: float
    sign: int                 # +1 supports, -1 contradicts, 0 measured and neutral
    text: str
    value: float | None = None
    unit: str = ""
    threshold: float | None = None


# ------------------------------------------------------------------------- the capture ---

def _fin(v: Any) -> float | None:
    """A finite number within attribute.MAX_COUNT, else None: a corrupt 1e308 is missing, never evidence (A15)."""
    n = _num(v)
    return float(n) if n is not None and abs(n) <= attribute.MAX_COUNT else None


def _text(v: Any, limit: int = 200) -> str | None:
    """A string from the capture, cut to a sane length; None when it is not a non-empty string."""
    if not isinstance(v, str) or not v.strip():
        return None
    s = "".join(ch for ch in v.strip() if ch.isprintable())
    return s[:limit] or None


def _dict(v: Any) -> dict:
    return v if isinstance(v, dict) else {}


class _Env:
    """What every rule reads, derived once per capture."""

    def __init__(self, cap: Capture, ctx: dict) -> None:
        ctx = ctx if isinstance(ctx, dict) else {}
        self.cap = cap
        self.fr = _dict(ctx.get("frames"))
        self.stats = _dict(ctx.get("stats"))
        self.bound = _dict(ctx.get("bound"))
        self.events = [e for e in ctx.get("events") or [] if isinstance(e, LagEvent)]
        self.causes = _dict(ctx.get("event_causes"))
        v = self.bound.get("verdict")
        self.verdict = v if isinstance(v, str) and v else "unknown"
        self.inv = _dict(cap.inventory)
        self.target = _dict(cap.target)
        self.sources = cap.sources if isinstance(cap.sources, dict) else {}
        self.attributed = [e for e in self.events if e.kind in attribute.ATTRIBUTED_KINDS]
        # item 11: the events attribution judged, the user's marks when they stood in for the frames (half weight)
        self.half = any(isinstance(v, dict) and v.get("half_weight") for v in self.causes.values())
        self.judged = [e for e in self.events if e.kind == "mark"] if self.half and not self.attributed \
            else self.attributed
        self.tick_spans = fa._merged_spans(self.events, fa.FRAME_EVENT_KINDS, pad=fa.TICK_PAD_S)
        dur = fa._bounded(cap.duration_s)
        self.duration = float(dur) if dur is not None and dur > 0 else None
        self.memo: dict[str, Any] = {}         # values more than one rule reads, worked out once
        self.refresh = fa.refresh_hz(cap)
        self.display = fa.game_display(cap)
        self.adapters = [a for a in self.inv.get("adapters") or [] if isinstance(a, dict) and a.get("software") is not True]
        self.game_adapter = self.adapter(self.target.get("adapter_luid"))
        self.power = _dict(self.inv.get("power"))
        self.settings = _dict(self.inv.get("settings"))
        self.game = _dict(self.inv.get("game"))

    # sources ------------------------------------------------------------------------------
    def _rows(self, name: str) -> int:
        table = {"frames": self.fr, "system": self.cap.system, "gpu": self.cap.gpu, "network": self.cap.network}
        if name == "frames":
            n = _num(self.stats.get("frames"))
            return int(n) if n is not None else 0
        if name in table:
            return len(fa._col(table[name], "t"))
        return 1

    def measured(self, name: str) -> bool:
        s = self.sources.get(name)
        return s is not None and getattr(s, "state", None) in OK_STATES and self._rows(name) > 0

    def why(self, name: str) -> str:
        s = self.sources.get(name)
        if s is None:
            return "not recorded in this capture"
        if getattr(s, "state", None) in OK_STATES:
            return "it ran but recorded nothing" if name != "frames" else "PresentMon ran but recorded no frames from the game"
        return _text(getattr(s, "detail", None), 300) or str(getattr(s, "state", "not measured"))

    def need(self, name: str) -> str:
        return labels.needs_source(name)     # the source's own reason is in the Not measured list, once

    @property
    def frames_measured(self) -> bool:
        return self.measured("frames")

    # columns ------------------------------------------------------------------------------
    def sys_rows(self, column: str) -> list[tuple[float, float]]:
        t, v = fa._col(self.cap.system, S.T), fa._col(self.cap.system, column)
        out = [(float(a), float(b)) for a, b in ((fa._bounded(ti), attribute.read(column, fa._at(v, i)))
                                                 for i, ti in enumerate(t))
               if a is not None and b is not None]
        out.sort(key=lambda p: p[0])
        return out

    def sys(self, column: str) -> list[float]:
        return [v for _, v in self.sys_rows(column)]

    def steady(self, column: str) -> list[float]:
        """1 s means of a 4 Hz column, leaving out windows that touch a stutter or FPS drop."""
        return fa._window_means(self.cap, column, self.tick_spans)

    def gpu(self, column: str) -> list[float]:
        t, v = fa._col(self.cap.gpu, G.T), fa._col(self.cap.gpu, column)
        return [x for x in (attribute.read(column, fa._at(v, i)) for i, ti in enumerate(t)
                            if fa._bounded(ti) is not None) if x is not None]

    def gpu_text(self, column: str) -> list[str]:
        return [x for x in fa._col(self.cap.gpu, column) if isinstance(x, str)]

    def net(self, column: str) -> list[tuple[float, float | None]]:
        """An RTT column: events.py's reading, where a value <= 0 is a lost or unsent probe (None)."""
        return ev_mod._net_rows(self.cap, column)

    def net_values(self, column: str) -> list[float]:
        return [v for _, v in self.net(column) if v is not None]

    def net_readings(self, column: str) -> list[float]:
        """Every sane value of an N column whose readings can be zero or negative (RSSI in dBm, quality)."""
        t, v = fa._col(self.cap.network, N.T), fa._col(self.cap.network, column)
        return [x for x in (attribute.read(column, fa._at(v, i)) for i, ti in enumerate(t)
                            if fa._bounded(ti) is not None) if x is not None]

    # inventory ----------------------------------------------------------------------------
    def adapter(self, luid: Any) -> dict | None:
        if not isinstance(luid, str):
            return None
        return next((a for a in self.adapters if isinstance(a.get("luid"), str) and a["luid"].lower() == luid.lower()), None)

    @property
    def avg_fps(self) -> float | None:
        return _fin(self.stats.get("avg_fps"))

    def flows(self) -> dict:
        return _dict(self.cap.net_flows)

    def on_wifi(self) -> bool | None:
        kind = _dict(self.inv.get("network")).get("kind")
        if kind == "wifi":
            return True
        if kind in ("ethernet", "other"):
            return False
        if self.net_readings(N.WIFI_RSSI_DBM) or self.net_readings(N.WIFI_SIGNAL_PCT):
            return True
        return None


def _env(cap: Capture, ctx: dict) -> _Env:
    """The derived view, cached in ctx so the rules of one run_rules() call share it."""
    if isinstance(ctx, dict):
        env = ctx.get("_env")
        if isinstance(env, _Env) and env.cap is cap:
            return env
        env = _Env(cap, ctx)
        ctx["_env"] = env
        return env
    return _Env(cap, {})


def _is_integrated(a: dict | None) -> bool:
    return isinstance(a, dict) and (a.get("integrated") is True or a.get("hybrid") == "integrated")


def _is_discrete(a: dict | None) -> bool:
    return isinstance(a, dict) and a.get("software") is not True and (a.get("integrated") is False
                                                                      or a.get("hybrid") == "discrete")


def _name(a: dict | None, fallback: str = "a GPU") -> str:
    return _text(a.get("name"), 80) if isinstance(a, dict) and _text(a.get("name"), 80) else fallback


def _exe_path(v: Any) -> str | None:
    """The game's exe as a fix may use it: an absolute Windows path to an .exe, printable, no '..' parts."""
    if not isinstance(v, str) or not v or len(v) > EXE_MAX_CHARS or not v.isprintable():
        return None
    if not ntpath.isabs(v) or not v.lower().endswith(".exe") or ".." in re.split(r"[\\/]", v):
        return None
    return v


def _union_s(spans: Iterable[tuple[float, float]]) -> float:
    total, end = 0.0, -math.inf
    for a, b in sorted(spans):
        if b <= end:
            continue
        total += b - max(a, end)
        end = b
    return total


# --------------------------------------------------------------------------------- scoring ---

class _Case:
    """One rule's evidence for one capture, and LC 5's verdict on it."""

    def __init__(self, cap: Capture, ctx: dict, cause: str) -> None:
        self.env = _env(cap, ctx)
        self.cause = cause
        self.rule = RULE_BY_CAUSE[cause]
        self.items: list[_Item] = []
        self.missing: dict[str, tuple[float, str]] = {}

    def support(self, family: str, weight: float, text: str, value: float | None = None, unit: str = "",
                threshold: float | None = None) -> None:
        self.items.append(_Item(family, weight, +1, text, value, unit, threshold))

    def against(self, family: str, weight: float, text: str, value: float | None = None, unit: str = "",
                threshold: float | None = None) -> None:
        self.items.append(_Item(family, weight, -1, text, value, unit, threshold))

    def neutral(self, family: str, text: str, value: float | None = None, unit: str = "",
                threshold: float | None = None, weight: float = W_STEADY) -> None:
        self.items.append(_Item(family, weight, 0, text, value, unit, threshold))

    def lack(self, family: str, weight: float, why: str) -> None:
        """A family this rule would have used but that was not measured (it lowers coverage)."""
        if family not in self.missing:
            self.missing[family] = (weight, why)

    def score(self) -> tuple[float, float, dict[str, list[_Item]]]:
        """(S, coverage C, items by family): per family only the strongest supporting and the strongest
        contradicting item count, static support is capped at +2 (LC 5.1, 5.3)."""
        fams: dict[str, list[_Item]] = {}
        for it in self.items:
            fams.setdefault(it.family, []).append(it)
        s = 0.0
        for fam, its in fams.items():
            sup = max((i.weight for i in its if i.sign > 0), default=0.0)
            con = max((i.weight for i in its if i.sign < 0), default=0.0)
            if fam == "static":
                sup = min(sup, STATIC_CAP)
            s += sup - con
        got = sum(max(i.weight for i in its) for its in fams.values())
        lost = sum(w for fam, (w, _) in self.missing.items() if fam not in fams)
        coverage = got / (got + lost) if got + lost > 0 else 0.0
        return s, coverage, fams

    def conclude(self, title: str = "", summary: str = "", fixes: Iterable[FixSuggestion] = (),
                 event_ids: Iterable[int] = ()) -> Any:
        s, coverage, fams = self.score()
        rule = self.rule
        key_measured = rule.family in fams or any(i.sign > 0 for f in rule.alt for i in fams.get(f, []))
        if not key_measured:
            why = next((self.missing[f][1] for f in (rule.family,) + rule.alt if f in self.missing), None)
            return Unchecked(self.cause, why or "not measured")
        if coverage < MIN_COVERAGE:
            gaps = "; ".join(w for f, (_, w) in self.missing.items() if f not in fams)
            return Unchecked(self.cause, f"not enough data: {gaps}")
        key_supports = any(i.sign > 0 for i in fams.get(rule.family, []))
        if s >= LIKELY_S and key_supports and coverage >= LIKELY_COVERAGE:
            conf = "high"
        elif s >= POSSIBLE_S:
            conf = "medium"
        elif s > 0:
            conf = "low"
        elif s <= CLEARED_S and coverage >= CLEARED_COVERAGE:
            against = sorted((i for i in self.items if i.sign < 0), key=lambda i: -i.weight)
            return Cleared(self.cause, _sentence("; ".join(i.text for i in against[:2])))
        else:
            return None                      # measured, but no evidence either way: silence
        ordered = (sorted((i for i in self.items if i.sign > 0), key=lambda i: -i.weight)
                   + [i for i in self.items if i.sign == 0]
                   + sorted((i for i in self.items if i.sign < 0), key=lambda i: -i.weight))
        evidence = [Evidence(_sentence(i.text), i.value, i.unit, i.threshold) for i in ordered]
        fixes = list(fixes)
        ids = sorted({int(i) for i in event_ids})
        attributed = {e.id for e in self.env.attributed}
        share = len(attributed & set(ids)) / len(attributed) if attributed else 0.0
        return Finding(cause=self.cause, title=title or labels.cause_text(self.cause).capitalize(),
                       category=rule.category, confidence=conf,
                       score=_score(conf, s, share, any(f.fix_id for f in fixes)),
                       summary=summary, evidence=evidence, fixes=fixes, event_ids=ids)


TIERS = {"low": 0, "medium": 1, "high": 2}


def _score(conf: str, s: float, share: float, has_fix: bool) -> float:
    """0..1 in DESIGN's ranking order: label, then S, then explained share of events, then an auto-fix.
    Each key outweighs everything after it: S moves in steps of 0.5 (20 points), more than a whole share
    (10 points); one event in 1,000 of share (0.01 points) outweighs the auto-fix bonus (0.005)."""
    tier = TIERS.get(conf, 0)
    raw = (tier * 1000.0 + min(max(s, 0.0), 20.0) * 40.0 + min(max(share, 0.0), 1.0) * 10.0
           + (0.005 if has_fix else 0.0))
    return round(raw / (2 * 1000.0 + 20.0 * 40.0 + 10.0 + 0.005), 9)


def _sentence(text: str) -> str:
    """Capitalised, with a full stop; a first word that is a file or process name ("python.exe") keeps its case."""
    text = text.strip()
    if not text:
        return text
    first = text.split(" ", 1)[0]
    if "." not in first.rstrip(".,;:") and "\\" not in first:
        text = text[0].upper() + text[1:]
    return text if text.endswith((".", "!", "?")) else text + "."


def _advice(title: str, detail: str = "") -> FixSuggestion:
    return FixSuggestion(title=title, detail=detail)


# ------------------------------------------------------------------------------- run_rules ---

def run_rules(cap: Capture, ctx: dict) -> tuple[list[Finding], dict[str, str], dict[str, str]]:
    """Every rule over one capture: (findings best first, cleared {cause: what was measured},
    unchecked {cause: why it could not be checked}). ctx is DESIGN's {"frames", "stats", "bound", "events",
    "event_causes"}."""
    env = _env(cap, ctx)
    findings: list[Finding] = []
    cleared: dict[str, str] = {}
    unchecked: dict[str, str] = {}
    for rule in RULES:
        res = run_rule(rule, cap, ctx, env)
        if isinstance(res, Finding):
            findings.append(res)
        elif isinstance(res, Cleared):
            cleared[rule.cause] = res.text
        elif isinstance(res, Unchecked):
            unchecked[rule.cause] = res.text
    _hold_unexplained_pacing(env, findings, cleared, unchecked)
    findings.sort(key=lambda f: -f.score)
    return findings, cleared, unchecked


def run_rule(rule: Rule, cap: Capture, ctx: dict, env: _Env | None = None) -> Any:
    """One rule: its sources first (not measured -> Unchecked), then the rule, then its gate; a cause the
    capture holds open is never checked and fine (_holds_open)."""
    env = env or _env(cap, ctx)
    for need in rule.needs:
        names = need.split("|")
        if not any(env.measured(n) for n in names):
            return Unchecked(rule.cause, env.need(names[0]))
    try:
        res = rule.fn(cap, ctx)
        if isinstance(res, Finding) and rule.gate and env.verdict not in GATES[rule.gate]:
            res = _gated(rule, env, res)
        held = _holds_open(rule.cause, env) if isinstance(res, Cleared) and not res.firm else None
    except Exception as exc:  # noqa: BLE001 - one broken rule must not hide the rest; it says it failed
        return Unchecked(rule.cause, f"the check failed ({type(exc).__name__}: {exc})")
    return Unchecked(rule.cause, held) if held else res


def _holds_open(cause: str, e: _Env) -> str | None:
    """Why `cause` must not be listed as checked and fine in this capture even where its own evidence says so:
    the measurement it would be cleared on cannot tell. One predicate for every Cleared path of a rule, its gate's
    included."""
    if cause == "gpu_limit":
        m = _contention(e)
        if m is not None:      # A7: the game's GPU time was shared, so "the GPU is not the limit" cannot be told
            return (f"other programs kept the game's GPU {m:.0f}% busy (median of 1 s windows), so whether the "
                    "game's own GPU work is the limit could not be told")
    if cause == "cpu_limit" and e.verdict != "gpu":
        busiest = _fin(e.bound.get("busiest_thread"))
        if busiest is not None and THREAD_WAITING <= busiest < fa.THREAD_BOUND:      # A6, LC 3.2
            return (f"the game's busiest thread ran {100 * busiest:.0f}% of a CPU, between "
                    f"{100 * THREAD_WAITING:.0f}% (waiting) and {100 * fa.THREAD_BOUND:.0f}% (CPU-bound): the CPU "
                    "may be close to its limit, so it is not ruled out")
    return None


def _contention(e: _Env) -> float | None:
    """A7 (LC 4.6): the median 1 s share other programs kept the game adapter's engine busy, outside lag events,
    when it is OTHER_GPU_PCT or more; else None. frames.gpu_shared, the predicate the bound verdict reads too."""
    if "contention" not in e.memo:
        e.memo["contention"] = fa.gpu_shared(e.cap, e.tick_spans)
    return e.memo["contention"]


def _hold_unexplained_pacing(e: _Env, findings: list[Finding], cleared: dict[str, str],
                             unchecked: dict[str, str]) -> None:
    """A11: uneven pacing that no finding claims, over UNEVEN_SHARE of the capture or more, leaves the pacing
    causes open: "frame pacing: checked and fine" would deny what the frames showed."""
    claimed = {i for f in findings for i in f.event_ids}
    loose = [ev for ev in e.events if ev.kind == "pacing" and ev.id not in claimed]
    if not loose or e.duration is None:
        return
    share = min(1.0, _union_s((ev.t_start, ev.t_end) for ev in loose) / e.duration)
    if share < UNEVEN_SHARE:
        return
    worst = max(loose, key=lambda ev: ev.severity)
    why = (f"uneven pacing covered {100 * share:.0f}% of the capture (worst "
           f"{labels.severity_text('pacing', worst.severity, worst.detail)}) and no rule explained it, so it is not "
           "ruled out")
    for cause in ("uneven_pacing", "vsync_judder", "dropped_frames"):
        if cause in cleared:
            del cleared[cause]
            unchecked[cause] = why


def _gated(rule: Rule, env: _Env, finding: Finding) -> Any:
    """LC 5.1: the bound verdict is a gate, not evidence. Without frame timing a GPU-side finding still passes where
    the GPU probably was the limit for part of the capture (_probable_gpu), saying so in its evidence."""
    v = env.verdict
    side = "the CPU" if rule.gate == "cpu" else "the GPU"
    probable = _probable_gpu(env)       # an unknown verdict reaches here only through the gpu gate (GATES)
    if probable is not None and probable[0]:
        finding.evidence.append(Evidence(_sentence(probable[1])))
        return finding
    if v == "unknown":
        notes = [n for n in env.bound.get("notes") or [] if isinstance(n, str)]
        why = notes[:1] + ([probable[1]] if probable is not None else [])
        return Unchecked(rule.cause, f"whether {side} limits the frame rate could not be judged"
                         + (f" ({'; '.join(why)})" if why else ""))
    return Cleared(rule.cause, _sentence(f"{side} is not what limits the frame rate: "
                                         f"it is limited by {labels.BOUND.get(v, v)}"))


def _probable_gpu(e: _Env) -> tuple[bool, str] | None:
    """Item 1, LC 3.4 per second: without frame timing, (whether the GPU probably limited the frame rate for enough
    of the capture for a GPU-side cause to matter, the measured words for it), from frames.probable_gpu; None when
    frame timing was measured or the bound verdict is not unknown."""
    p = _dict(e.bound.get("gpu_probable"))
    seconds, share, n, busy = (_fin(p.get(k)) for k in ("seconds", "share", "marks", "marks_busy"))
    if e.frames_measured or e.verdict != "unknown" or seconds is None or share is None:
        return None
    if not n or busy is None:
        marks = ""
    elif n == 1:
        marks = " and at the moment you marked" if busy else ", though not at the moment you marked"
    else:
        marks = f" and at {busy:.0f} of the {n:.0f} moments you marked" if busy else \
            f", though at none of the {n:.0f} moments you marked"
    if p.get("holds") is True:
        return True, (f"frame timing was not measured, but the game kept its GPU's 3D engine "
                      f"{fa.ENGINE_GPU_PCT:.0f}% or more busy for {seconds:.0f} s ({100 * share:.0f}% of the capture)"
                      f"{marks}, so the GPU probably limited the frame rate then")
    return False, (f"the game kept its GPU's 3D engine {fa.ENGINE_GPU_PCT:.0f}% or more busy for only {seconds:.0f} s "
                   f"({100 * share:.0f}% of the capture){marks}")


# ----------------------------------------------------------------------------- GPU and CPU ---

def _wrong_gpu(cap: Capture, ctx: dict) -> Any:
    c = _Case(cap, ctx, "wrong_gpu")
    e = c.env
    if not e.adapters:
        c.lack("gpu_engine", W_DIRECT, "the list of graphics adapters was not read")
        return c.conclude()
    discrete = [a for a in e.adapters if _is_discrete(a)]
    if not discrete:
        c.against("gpu_engine", W_DIRECT, f"this PC has no discrete GPU ({', '.join(_name(a) for a in e.adapters)}), "
                                          "so the game cannot be on the wrong one")
        return c.conclude()
    game = e.game_adapter
    if game is None:
        why = e.need("system") if not e.measured("system") else "the game's 3D engine was never seen busy"
        c.lack("gpu_engine", W_DIRECT, f"which GPU the game rendered on is unknown: {why}")
        return c.conclude()
    if _is_discrete(game):
        c.against("gpu_engine", W_DIRECT, f"the game rendered on {_name(game)}, the discrete GPU")
        return c.conclude()
    if not _is_integrated(game):
        c.lack("gpu_engine", W_DIRECT, f"whether {_name(game)} is the integrated or the discrete GPU is unknown")
        return c.conclude()
    best = max(discrete, key=lambda a: _fin(a.get("vram_mb")) or 0.0)
    c.support("gpu_engine", W_DIRECT, f"the game's 3D work ran on {_name(game)} (integrated) although "
                                      f"{_name(best)} (discrete) is in this PC")
    v, fps = e.verdict, e.avg_fps
    if not e.frames_measured:
        c.lack("frame_timing", W_STEADY, e.need("frames"))
    elif v == "gpu":
        c.support("frame_timing", W_STEADY, "and the integrated GPU is what limits the frame rate"
                  + (f" ({fps:.0f} FPS)" if fps else ""))
    elif v == "mixed":
        c.neutral("frame_timing", "the integrated GPU shares the limit with the CPU")
    elif v == "unknown":
        c.lack("frame_timing", W_STEADY, "whether the GPU limits the frame rate could not be judged")
    else:
        c.against("frame_timing", W_STEADY, f"right now the frame rate is limited by {labels.BOUND.get(v, v)}, "
                                            "not by the GPU, so the faster GPU would mostly add headroom")
    fixes = [_gpu_preference_fix(e, best)]
    limited = v == "gpu"
    summary = (f"Windows ran the game on {_name(game)} instead of {_name(best)}. "
               + ("That integrated GPU is what holds the frame rate down." if limited else
                  "It is not the limit at the moment, but the discrete GPU would give the game far more headroom."))
    return c.conclude(f"The game ran on the integrated GPU, not on {_name(best)}", summary, fixes)


def _gpu_preference_fix(e: _Env, best: dict) -> FixSuggestion:
    exe = _exe_path(e.target.get("exe")) or _exe_path(e.game.get("exe"))
    pref = _num(e.game.get("gpu_preference"))
    if pref == 2:
        return _advice(f"Pick {_name(best)} in the game's own settings or in the GPU maker's control panel",
                       "Windows is already set to High performance for this game, yet it still rendered on the "
                       "integrated GPU: OpenGL and Vulkan games may ignore the Windows setting.")   # WT 8
    if exe:
        return FixSuggestion(
            title=f"Run the game on {_name(best)} (Windows graphics preference: High performance)",
            detail="The registry format behind this setting is "
                   "community-documented, not Microsoft-documented; OpenGL and Vulkan games may ignore it.",   # WT 8
            fix_id="gpu_preference", params={"exe": exe, "preference": 2}, needs_restart="game")
    return _advice("Set the game to High performance in Settings > System > Display > Graphics",
                   "lag-doctor could not tell which program file the game runs from, so it cannot set this itself.")


def _hybrid_copy(cap: Capture, ctx: dict) -> Any:
    c = _Case(cap, ctx, "hybrid_copy")
    e = c.env
    game = e.game_adapter
    if game is None:
        c.lack("static", W_DIRECT, "which GPU the game rendered on is unknown"
               + (f": {e.need('system')}" if not e.measured("system") else ""))
        return c.conclude()
    disp = e.display
    shown_on = e.adapter(disp.get("adapter_luid")) if disp else None
    if shown_on is None:
        c.lack("static", W_DIRECT, "which GPU drives the game's display is unknown")
        return c.conclude()
    screen = _text(disp.get("name"), 80) or _text(disp.get("device"), 40) or "the game's display"
    if shown_on is game:
        c.against("static", W_DIRECT, f"the game renders on {_name(game)}, which also drives {screen}: "
                                      "no copy between GPUs")
        return c.conclude()
    c.support("static", W_DIRECT, f"the game renders on {_name(game)} but {screen} is driven by {_name(shown_on)}, "
                                  "so every frame is copied across")
    hybrid = [_num(x) for x in e.fr.get(F.HYBRID) or []]
    if any(x == 1 for x in hybrid):
        c.support("frame_timing", W_DIRECT, "PresentMon marked the game's frames as cross-adapter presents "
                                            "(HybridPresent = 1)")
    elif e.frames_measured:
        c.neutral("frame_timing", "PresentMon did not mark the frames as cross-adapter, but it only can when the "
                                  "game creates its swap chain during the capture")   # PM 9
    oem = [t for t in _dict(e.inv.get("oem")).get("tools") or [] if isinstance(t, str)]
    armoury = any("armoury" in t.lower() for t in oem)
    fixes = [_advice("Let the discrete GPU drive the screen directly",
                     "Use the laptop's MUX / dGPU-only mode" + (" (Armoury Crate GPU mode \"Ultimate\")" if armoury
                                                                else "") + "; switching usually needs a reboot."),
             _advice("Or play on a monitor wired to the discrete GPU", "Its frames then need no copy.")]
    return c.conclude(f"Frames are copied from {_name(game)} to {_name(shown_on)}, which drives the screen",
                      "The game renders on one GPU while the screen hangs off the other, so every frame is copied "
                      "across before it is shown. That costs frame rate and adds delay.", fixes)


def _fps_reaches_display(e: _Env) -> bool:
    fps, hz = e.avg_fps, e.refresh
    return fps is not None and hz is not None and fps >= LIMIT_FPS_FRAC * hz


def _gpu_limit(cap: Capture, ctx: dict) -> Any:
    c = _Case(cap, ctx, "gpu_limit")
    e = c.env
    b, v = e.bound, e.verdict
    r = _fin(b.get("r"))
    if e.frames_measured:
        if v in ("gpu", "mixed") and _fps_reaches_display(e):
            return Cleared("gpu_limit", _sentence(f"the GPU sets the pace, but at {e.avg_fps:.0f} FPS the game "
                                                  f"already reaches the {e.refresh:.0f} Hz display"), firm=True)
        g, ft = _fin(b.get("gpu_ms")), _fin(b.get("ft_ms"))
        detail = f" (r = {r:.2f})" if r is not None else ""
        if v == "gpu" and r is not None:
            busy = (f"the GPU was busy {g:.1f} ms of each {ft:.1f} ms frame" if g is not None and ft is not None
                    else f"the GPU was busy {100 * r:.0f}% of each frame")
            c.support("frame_timing", W_STEADY, f"{busy}{detail}; GPU-bound at {fa.R_GPU:.2f} or more", r, "", fa.R_GPU)
        elif v == "gpu":
            c.support("frame_timing", W_STEADY, "frame timing shows the GPU as the limit")
        elif v == "mixed":
            c.support("frame_timing", W_STATIC, f"borderline: the GPU was busy for {100 * (r or 0):.0f}% of each "
                                                f"frame (between {100 * fa.R_CPU:.0f}% and {100 * fa.R_GPU:.0f}%)",
                      r, "", fa.R_GPU)
        elif v == "unknown":
            notes = [n for n in b.get("notes") or [] if isinstance(n, str)]
            c.lack("frame_timing", W_STEADY, "the frame-rate limit could not be judged" + (f": {notes[0]}" if notes else ""))
        else:
            c.against("frame_timing", W_STEADY, f"the frame rate is limited by {labels.BOUND.get(v, v)}{detail}")
    else:
        c.lack("frame_timing", W_STEADY, e.need("frames"))
    engine = e.steady(S.GAME_GPU_PCT)
    if engine:
        m = st.median(engine)
        probable = not e.frames_measured
        need = ENGINE_PROBABLE_PCT if probable else ENGINE_BUSY_PCT
        if m >= need:
            c.support("gpu_engine", W_STEADY, f"the game kept its GPU's 3D engine {m:.0f}% busy ({need:.0f}% or more)"
                      + (": probably GPU-limited" if probable else ""), m, "%", need)
        elif m < ENGINE_IDLE_PCT:
            c.against("gpu_engine", W_STEADY, f"the game's 3D engine was only {m:.0f}% busy", m, "%", ENGINE_IDLE_PCT)
        else:
            c.neutral("gpu_engine", f"the game's 3D engine was {m:.0f}% busy", m, "%", ENGINE_BUSY_PCT)
    elif not e.measured("system"):
        c.lack("gpu_engine", W_STEADY, e.need("system"))
    shared = _contention(e)
    if shared is not None:
        c.neutral("gpu_engine", f"other programs kept the same GPU {shared:.0f}% busy as well (median of 1 s "
                                "windows), so the game shares it", shared, "%", OTHER_GPU_PCT)
    util = e.gpu(G.UTIL_PCT) if e.measured("gpu") else []
    if util:
        m = st.median(util)
        if m >= NV_UTIL_PCT:
            c.support("gpu_telemetry", W_STEADY, f"NVIDIA reported the GPU {m:.0f}% busy ({NV_UTIL_PCT:.0f}% or more)",
                      m, "%", NV_UTIL_PCT)
        heat = _heat_share(e)
        if heat is not None and heat[1] >= NV_THROTTLE_PCT:
            c.neutral("gpu_telemetry", f"the GPU was also held down by {attribute.held_by(e.cap)} "
                                       f"{heat[1]:.0f}% of the time (see GPU throttling)")
    fps = e.avg_fps
    fixes = [_advice("Lower the render resolution, or use an upscaler (DLSS, FSR or XeSS)"),
             _advice("Turn down the heaviest graphics settings", "Ray tracing, shadows and volumetric effects first."),
             _advice("Cap the frame rate a few FPS below the refresh rate if the screen supports VRR",
                     "An even frame rate feels smoother than one that swings at the GPU's limit.")]
    title = "The graphics card (GPU) is at its limit" + (f" at {fps:.0f} FPS" if fps else "")
    return c.conclude(title, "The GPU is busy for practically the whole of every frame, so it decides the frame "
                             "rate. Lighter graphics settings are the way to more FPS.", fixes)


def _cpu_limit(cap: Capture, ctx: dict) -> Any:
    c = _Case(cap, ctx, "cpu_limit")
    e = c.env
    b, v = e.bound, e.verdict
    if v in ("cpu_main", "cpu_all", "mixed", "unknown") and _fps_reaches_display(e):
        return Cleared("cpu_limit", _sentence(f"at {e.avg_fps:.0f} FPS the game already reaches the "
                                              f"{e.refresh:.0f} Hz display"), firm=True)
    r, busiest = _fin(b.get("r")), _fin(b.get("busiest_thread"))
    if v in ("cpu_main", "cpu_all") and r is not None:
        c.support("frame_timing", W_STEADY, f"the GPU was busy only {100 * r:.0f}% of each frame "
                                            f"(r = {r:.2f}, {fa.R_CPU:.2f} or less): it waits on the CPU",
                  r, "", fa.R_CPU)
    elif v == "mixed":
        c.support("frame_timing", W_STATIC, f"borderline: the GPU was busy {100 * (r or 0):.0f}% of each frame")
    elif v == "unknown":
        notes = [n for n in b.get("notes") or [] if isinstance(n, str)]
        c.lack("frame_timing", W_STEADY, "the frame-rate limit could not be judged" + (f": {notes[0]}" if notes else ""))
    else:
        c.against("frame_timing", W_STEADY, f"the frame rate is limited by {labels.BOUND.get(v, v)}")
    if busiest is not None:
        if busiest >= fa.THREAD_BOUND:
            c.support("cpu_threads", W_STEADY, f"the game's busiest thread ran {100 * busiest:.0f}% of a CPU "
                                               f"({100 * fa.THREAD_BOUND:.0f}% or more, 1 s windows)",
                      100 * busiest, "%", 100 * fa.THREAD_BOUND)
        elif v == "cpu_all":
            c.support("cpu_threads", W_STEADY, "the game kept every CPU core busy")
        else:          # in LC 3.2's grey zone (0.6 to 0.85) this still never clears the cause: _holds_open
            c.against("cpu_threads", W_STEADY, f"the game's busiest thread ran only {100 * busiest:.0f}% of a CPU",
                      100 * busiest, "%", 100 * fa.THREAD_BOUND)
    else:
        c.lack("cpu_threads", W_STEADY, "the game's thread CPU use was not measured")
    others = _others_steady(e)
    if others is not None and others >= attribute.OTHERS_ONE_PCT:
        c.neutral("background", f"other programs used {others:.0f}% of a CPU meanwhile (100% = one logical CPU)")
    fps = e.avg_fps
    one = v != "cpu_all"
    fixes = [_advice("Turn down CPU-heavy settings", "Crowd density, view distance, simulation detail and physics."),
             _advice("Close CPU-heavy programs while playing"),
             _advice("Frame generation can smooth the picture", "Only if the base frame rate is already acceptable: "
                                                                "the delay stays tied to the base rate.")]
    title = ("One game thread maxes out the CPU" if one else "The CPU is maxed out on every core") + \
            (f" at {fps:.0f} FPS" if fps else "")
    return c.conclude(title, "The GPU sits idle for most of each frame while the game's CPU work finishes, so the "
                             "CPU decides the frame rate.", fixes)


def _gpu_contention(cap: Capture, ctx: dict) -> Any:
    """A7 (LC 4.6 steady signature, LC 10 row 4.6): other programs keep the game adapter's engine busy. CUDA
    compute counts as 3D (WT verify log), so an ML job on the game's GPU shows here. No gate: whichever side
    limits the game, a shared GPU is worth knowing."""
    c = _Case(cap, ctx, "gpu_contention")
    e = c.env
    m = fa.gpu_others_median(cap, e.tick_spans)
    if m is None:
        c.lack("gpu_engine", W_STEADY, "other programs' use of the game's GPU was not measured"
               + ("" if e.target.get("adapter_luid") else ": which GPU the game rendered on is unknown"))
        return c.conclude()
    if _contention(e) is None:
        c.against("gpu_engine", W_STEADY, f"other programs used the game's GPU only {m:.0f}% of the time (median of "
                                          f"1 s windows; under {OTHER_GPU_PCT:.0f}%)", m, "%", OTHER_GPU_PCT)
        return c.conclude()
    c.support("gpu_engine", W_STEADY, f"other programs kept the game's GPU {m:.0f}% busy (median of 1 s windows; "
                                      f"{OTHER_GPU_PCT:.0f}% or more)", m, "%", OTHER_GPU_PCT)
    starved = _starved(e)
    if starved:
        c.support("frame_timing", W_STEADY, starved)
    top = _top_gpu_user(e)
    name = None
    if top is not None:
        name, pid, pct, anywhere = top
        if anywhere:           # an older capture: the table's GPU figure is the busiest engine on any adapter
            c.neutral("background", f"the busiest other GPU user in the process table was {name} (process {pid}, "
                                    f"its busiest engine {pct:.0f}% busy, median); that figure covers any GPU, so it "
                                    "may be on another one", pct, "%", OTHER_GPU_PCT)
        else:
            c.neutral("background", f"the busiest other user of the game's GPU in the process table was {name} "
                                    f"(process {pid}, its busiest engine there {pct:.0f}% busy, median)",
                      pct, "%", OTHER_GPU_PCT)
    else:
        c.neutral("background", "which program it was is not in the process table (it lists the 8 busiest by CPU)")
    fixes = [_advice(f"Pause or close {name} while you play" if name else "Find and pause the GPU-heavy program",
                     "Training, rendering, mining or video encoding on the same GPU takes time the game needs; "
                     "lag-doctor never closes programs itself."),
             _advice("Or let that work run on another GPU, if this PC has one")]
    return c.conclude(f"Another program kept the game's GPU {m:.0f}% busy" + (f": {name}" if name else ""),
                      "Other programs used the same GPU as the game, so the game's frames had to wait for it.", fixes)


def _starved(e: _Env) -> str | None:
    """The game looks starved of its GPU (PM 3.2: other processes' work shows as lower GPU busy): under 90 % of
    the refresh, its own GPU work short of each frame (r <= R_CPU) and no CPU thread saturated."""
    fps, hz, r = e.avg_fps, e.refresh, _fin(e.bound.get("r"))
    busiest = _fin(e.bound.get("busiest_thread"))
    if not e.frames_measured or fps is None or not hz or r is None:
        return None
    if fps >= LIMIT_FPS_FRAC * hz or r > fa.R_CPU or (busiest is not None and busiest >= fa.THREAD_BOUND):
        return None
    return (f"and the game ran at {fps:.0f} FPS on a {hz:.0f} Hz screen with its own GPU work only {100 * r:.0f}% of "
            "each frame and no CPU thread saturated: it waited for the GPU")


def _top_gpu_user(e: _Env) -> tuple[str, int, float, bool] | None:
    """(name, pid, median %, measured on any adapter?) of the other process with the most GPU in the others table,
    a tick it was not listed on counting as 0: its use of the game's adapter where the table has it, else of any
    adapter (attribute.game_gpu_pct, A10). Never the game's own processes or lag-doctor's (A10)."""
    game = attribute.game_pids(e.cap)
    tool_pids, tool_names = attribute.tool_processes(e.cap)
    ticks = 0
    anywhere = False
    seen: dict[tuple[int, str], list[float]] = {}
    for row in e.cap.others if isinstance(e.cap.others, list) else []:
        if not (isinstance(row, (list, tuple)) and len(row) == 2 and fa._bounded(row[0]) is not None
                and isinstance(row[1], (list, tuple))):
            continue
        ticks += 1
        for p in row[1]:
            if not (isinstance(p, (list, tuple)) and len(p) >= 5 and attribute._is_pid(p[0]) and isinstance(p[1], str)):
                continue
            if p[0] in game or p[0] in tool_pids or p[1].lower() in tool_names:
                continue
            gpu, any_adapter = attribute.game_gpu_pct(p)
            anywhere = anywhere or any_adapter
            seen.setdefault((p[0], p[1]), []).append(gpu)
    best = None
    for (pid, name), vals in seen.items():
        m = st.median(vals + [0.0] * (ticks - len(vals)))
        if m > 0 and (best is None or m > best[2]):
            best = (_text(name, 80) or f"process {pid}", pid, m, anywhere)
    return best


def _others_steady(e: _Env) -> float | None:
    """Median CPU of the listed non-game processes per tick (100 = one logical CPU), lag-doctor's own left out as in
    attribute's background signals (item 10)."""
    tool_pids, tool_names = attribute.tool_processes(e.cap)
    sums = []
    for row in e.cap.others if isinstance(e.cap.others, list) else []:
        if isinstance(row, (list, tuple)) and len(row) == 2 and isinstance(row[1], (list, tuple)):
            sums.append(math.fsum(float(x) for x in (fa._bounded(p[2]) for p in row[1]
                                                     if isinstance(p, (list, tuple)) and len(p) >= 3
                                                     and not ((attribute._is_pid(p[0]) and p[0] in tool_pids)
                                                              or (isinstance(p[1], str) and p[1].lower() in tool_names)))
                                  if x is not None and x >= 0))
    return st.median(sums) if sums else None


def _heat_share(e: _Env) -> tuple[float, float, int, int] | None:
    """(the mean nv_throttle_pct, the share of the time heat or the HW power brake held the NVIDIA GPU down, the
    readings naming heat or the brake, the readings); None without readings. A capture saved before the power-cap
    split (attribute.cap_in_throttle) counted the power cap in nv_throttle_pct too: there a reading counts toward the
    second share only when its reasons named heat or the brake, any other as 0 (LC 4.1, 4.2). Elsewhere the two
    shares are the same number."""
    rows = [(x, named) for _, x, named in attribute.throttle_readings(e.cap) if x is not None]
    if not rows:
        return None
    full = st.mean([x for x, _ in rows])
    share = st.mean([x if named else 0.0 for x, named in rows]) if attribute.cap_in_throttle(e.cap) else full
    return full, share, sum(1 for _, named in rows if named), len(rows)


def _gpu_throttle(cap: Capture, ctx: dict) -> Any:
    c = _Case(cap, ctx, "gpu_throttle")
    e = c.env
    heat = _heat_share(e)                  # heat and the HW power brake only: the power cap has its own column
    old = attribute.cap_in_throttle(cap)   # ... except in a capture saved before that split (item 11)
    limited = _power_cap_item(c)
    if heat is not None:
        full, share, named, n = heat
        if share >= NV_THROTTLE_PCT:
            c.support("gpu_telemetry", W_DIRECT, f"the NVIDIA GPU was held down by {attribute.held_by(cap)} "
                                                 f"{share:.0f}% of the time ({NV_THROTTLE_PCT:.0f}% or more)"
                      + (f", counting only the {named} of {n} readings that named one, since this capture's "
                         "throttle share also held the power cap" if old else ""), share, "%", NV_THROTTLE_PCT)
        elif old and full >= NV_THROTTLE_PCT:      # the share may be the normal power cap of a GPU-bound laptop
            c.lack("gpu_telemetry", W_DIRECT, f"this capture was saved by an earlier lag-doctor, whose throttle share "
                                              f"({full:.0f}% of the time) also held the GPU's normal power cap, and "
                                              f"only {named} of {n} readings named heat or the power brake, so "
                                              "throttling could not be told from the power cap")
        elif limited:        # held down by a lowered power cap instead: no heat is no evidence against that
            c.neutral("gpu_telemetry", f"heat and its power brake held it down only {share:.0f}% of the time",
                      share, "%", NV_THROTTLE_PCT)
        else:                # in a capture saved before the split the share held the power cap too: at most this
            c.against("gpu_telemetry", W_STEADY, f"the GPU was held down by heat or its power brake only "
                                                 f"{full:.0f}% of the time (under {NV_THROTTLE_PCT:.0f}%"
                                                 + (", the power cap included" if old else "") + ")",
                      full, "%", NV_THROTTLE_PCT)
    elif not limited:
        idle, rows = attribute.idle_readings(cap), len(attribute.throttle_readings(cap))
        if idle:          # the counters keep growing at idle, so those readings say nothing either way (item 8)
            c.lack("gpu_telemetry", W_DIRECT, f"the NVIDIA GPU was idle in {'all' if idle == rows else idle} of its "
                                              f"{rows} readings" + ("" if idle == rows else ", and the rest held no "
                                                                    "throttle share") + ", so whether heat held it "
                                              "down could not be told")
        else:
            c.lack("gpu_telemetry", W_DIRECT, "NVML reported no throttle readings")
    temps = e.gpu(G.TEMP_C)
    target = _fin(_dict(e.inv.get("nvidia")).get("temp_target_c"))
    if temps and target:
        hot = st.percentile(temps, 95.0)
        if hot >= target:
            c.support("gpu_telemetry", W_STEADY, f"the GPU reached {hot:.0f} °C, at or above its {target:.0f} °C "
                                                 "target temperature", hot, "°C", target)
    _event_link(c, "gpu_throttle", "gpu_telemetry", "the GPU slowing down")
    _quiet_power_hint(c, gpu=True)
    fixes = [_advice("Give the laptop more air", "Raise its back, keep the vents clear and clean."),
             _advice("Plug in, and use the maker's performance mode rather than a quiet or silent one"),
             _advice("Cap the frame rate a little lower", "A capped GPU runs cooler and holds its clock.")]
    return c.conclude("The GPU slows down from heat or power limits",
                      "The NVIDIA driver held the GPU's clock down for much of the capture, which lowers the frame "
                      "rate while the GPU is the limit.", fixes)


def _power_cap_item(c: _Case) -> bool:
    """The software power cap (nv_power_cap_pct). While GPU-bound it is the design limit, not throttling, so it
    is context (LC 4.1, 4.2); it counts only as LC 4.2's power-limit variant, the enforced limit held below the
    GPU's default (battery, a quiet mode). The default comes from the inventory's nvidia "power_default_w".
    A reading's share counts only where its own reasons named the cap (attribute.power_cap_readings): the counter
    alone once read 42 % over readings that never named it. True when that variant holds."""
    e = c.env
    rows = attribute.power_cap_readings(e.cap)
    if not rows:
        return False
    share = st.mean([x for _, x, _, _ in rows])
    named = sum(1 for *_, n in rows if n)
    moved = any(raw > 0 and not n for _, _, raw, n in rows)        # the counter moved where the reasons never named it
    only = (f", counting only the {named} of {len(rows)} readings that named it (the driver's counter also moved in "
            "readings that did not)" if moved else "")
    limits = [w for w in e.gpu(G.POWER_LIMIT_W) if w > 0]
    limit = st.median(limits) if limits else None
    default = _fin(_dict(e.inv.get("nvidia")).get("power_default_w"))
    if share >= NV_THROTTLE_PCT and limit is not None and default and limit < default:
        c.support("gpu_telemetry", W_STEADY, f"the GPU sat at its power cap {share:.0f}% of the time with the cap at "
                                             f"{limit:.0f} W, below its {default:.0f} W default (on battery or in a "
                                             f"quiet mode){only}", share, "%", NV_THROTTLE_PCT)   # LC 4.2
        return True
    if share > 0:
        c.neutral("gpu_telemetry", f"the GPU sat at its software power cap {share:.0f}% of the time: the design limit "
                                   "of a GPU-bound laptop, not throttling"   # LC 4.1
                  + ("" if default else "; whether the cap was below the GPU's default was not recorded") + only,
                  share, "%", NV_THROTTLE_PCT)
    return False


def _quiet_power_hint(c: _Case, *, gpu: bool) -> None:
    """LC 4.2, 4.5, 4.12: a quiet power plan, battery or power saving make throttling more likely (static, +1). For
    the GPU never a CPU-only plan setting (its cap, boost off): a Windows plan sets no GPU limit."""
    for kind, why in _power_problems(c.env):
        if gpu and kind == "plan_cpu":
            continue
        c.support("static", W_STATIC, why)
        return


def _cpu_throttle(cap: Capture, ctx: dict) -> Any:
    c = _Case(cap, ctx, "cpu_throttle")
    e = c.env
    limit = e.sys(S.CPU_PERF_LIMIT_PCT)
    thermal = e.sys(S.THERMAL_LIMIT_PCT)
    perf_rows = e.sys_rows(S.CPU_PERF_PCT)
    held = False
    if limit:
        share = sum(1 for x in limit if x < 100.0) / len(limit)
        if share >= CPU_LIMIT_SHARE:
            held = True
            c.support("cpu_limits", W_STEADY, f"the CPU was held below full speed (% Performance Limit under 100) in "
                                              f"{100 * share:.0f}% of samples ({100 * CPU_LIMIT_SHARE:.0f}% or more)",
                      100 * share, "%", 100 * CPU_LIMIT_SHARE)
    if thermal and min(thermal) < 100.0:
        held = True
        c.support("cpu_limits", W_STEADY, f"the CPU's thermal zone limited it to {min(thermal):.0f}% (passive limit)",
                  min(thermal), "%", 100.0)
    level = st.median([v for _, v in perf_rows]) if perf_rows else None
    slow = level is not None and level <= CPU_SLOW_PCT
    if slow:          # item 2: the level itself; the limit flags and the drop below can all miss a mode set outside Windows
        c.support("cpu_limits", W_STEADY, f"the CPU ran at a median {level:.0f}% of its base clock ({CPU_SLOW_PCT:.0f}% "
                                          "or less), below full speed for most of the capture"
                  + ("" if held else ", though Windows reported no power or thermal limit"), level, "%", CPU_SLOW_PCT)
    dropped = None                        # None: too few speed samples to compare the start with the rest
    if len(perf_rows) >= 2 * PERF_MIN_SAMPLES:
        t0 = perf_rows[0][0]
        split = t0 + min(PERF_BASE_S, (perf_rows[-1][0] - t0) / 3.0)
        first = [v for t, v in perf_rows if t <= split]
        rest = [v for t, v in perf_rows if t > split]
        if len(first) >= PERF_MIN_SAMPLES and len(rest) >= PERF_MIN_SAMPLES:
            a, b = st.median(first), st.median(rest)
            dropped = a > 0 and b <= (1.0 - CPU_PERF_DROP) * a
            if dropped:
                held = True
                c.support("cpu_limits", W_STEADY, f"the CPU's speed fell from {a:.0f}% to {b:.0f}% of its base clock "
                                                  f"({100 * CPU_PERF_DROP:.0f}% or more down)", b, "%", (1 - CPU_PERF_DROP) * a)
    if not (limit or thermal or perf_rows):
        c.lack("cpu_limits", W_STEADY, "no CPU speed or limit counters were read")
    elif not (held or slow):              # say what was measured: "full speed" once stood for a median 70 % of base
        speed = "" if level is None else f", and the CPU ran at a median {level:.0f}% of its base clock" + \
            (" with no speed drop" if dropped is False else "")
        c.against("cpu_limits", W_STEADY, "Windows reported no power or thermal limit" + speed)
    busiest = _fin(e.bound.get("busiest_thread"))
    if busiest is not None and busiest < CPU_BUSY_FOR_THROTTLE:
        c.against("cpu_threads", W_STEADY, f"the game's busiest thread used only {100 * busiest:.0f}% of a CPU, so CPU "
                                           "speed is not what holds it back", 100 * busiest, "%", 100 * CPU_BUSY_FOR_THROTTLE)
    _event_link(c, "cpu_throttle", "cpu_limits", "the CPU slowing down")
    _quiet_power_hint(c, gpu=False)
    fixes = [_advice("Plug in, and use the maker's performance mode rather than a quiet or silent one"),
             _advice("Give the laptop more air", "Raise its back, keep the vents clear and clean."),
             _advice("Cap the frame rate a little lower", "Less CPU work per second means less heat.")]
    if slow and not held:                 # only the level: no limit Windows saw, so no heat or power claim either
        return c.conclude("The CPU ran below its base clock",
                          "The processor ran below its base clock for most of the capture while Windows reported no "
                          "limit; a power mode set outside Windows, such as the laptop maker's quiet mode, can hold it "
                          "there.", fixes)
    return c.conclude("The CPU slows down from heat or power limits",
                      "Windows reported the processor running below its full speed while the game needed it.", fixes)


def _vram_overflow(cap: Capture, ctx: dict) -> Any:
    c = _Case(cap, ctx, "vram_overflow")
    e = c.env
    game = e.game_adapter
    if _is_integrated(game):
        return Cleared("vram_overflow", _sentence(f"the game runs on {_name(game)}, an integrated GPU that uses system "
                                                  "memory by design, so its video memory cannot overflow"))
    size = _fin(game.get("vram_mb")) if game else None
    used = e.sys(S.VRAM_USED_MB)
    shared_rows = e.sys_rows(S.GAME_SHARED_MB)
    full = False
    if not size or size <= 0:
        c.lack("gpu_engine", W_STEADY, "the video memory size of the game's GPU is unknown")
    elif used:
        top = st.percentile(used, 95.0)
        full = top >= VRAM_FULL_FRAC * size
        if full:
            c.support("gpu_engine", W_STEADY, f"video memory was {top / 1024:.1f} of {size / 1024:.1f} GB full "
                                              f"({100 * top / size:.0f}%, {100 * VRAM_FULL_FRAC:.0f}% or more)",
                      100 * top / size, "%", 100 * VRAM_FULL_FRAC)
        else:
            c.against("gpu_engine", W_STEADY, f"video memory was at most {100 * top / size:.0f}% full",
                      100 * top / size, "%", 100 * VRAM_FULL_FRAC)
    else:
        c.lack("gpu_engine", W_STEADY, "the GPU memory counters were not read")
    if shared_rows and full:     # LC 4.3: shared use only means a spill while dedicated memory is pinned
        vals = [v for _, v in shared_rows]
        peak = max(vals)
        k = max(1, len(vals) // 10)            # the median of the first and of the last tenth, a step apart in time
        head, tail = shared_rows[:k], shared_rows[-k:]
        minutes = (st.median([t for t, _ in tail]) - st.median([t for t, _ in head])) / 60.0
        rise = ((st.median([v for _, v in tail]) - st.median([v for _, v in head])) / minutes
                if minutes > 0 else 0.0)
        if peak > SHARED_SPILL_MB:
            c.support("gpu_engine", W_STEADY, f"the game spilled {peak:.0f} MB into shared system memory "
                                              f"(over {SHARED_SPILL_MB:.0f} MB)", peak, "MB", SHARED_SPILL_MB)
        elif rise >= SHARED_RISE_MB_PER_MIN:
            c.support("gpu_engine", W_STEADY, f"the game's shared GPU memory grew {rise:.0f} MB a minute "
                                              f"({SHARED_RISE_MB_PER_MIN:.0f} or more)", rise, "MB/min",
                      SHARED_RISE_MB_PER_MIN)
    _event_link(c, "vram_overflow", "gpu_engine", "video memory spilling over")
    fixes = [_advice("Lower texture quality and the render resolution"),
             _advice("Turn off ray tracing or frame generation", "Both take extra video memory."),
             _advice("Close other GPU-heavy programs while playing")]
    return c.conclude("Video memory is full", "The game needs more video memory than the GPU has, so data spills "
                                              "into slower system memory and frames stall while it moves.", fixes)


def _ram_pressure(cap: Capture, ctx: dict) -> Any:
    c = _Case(cap, ctx, "ram_pressure")
    e = c.env
    ram_gb = _fin(e.inv.get("ram_gb"))
    avail = e.sys(S.RAM_AVAIL_MB)
    commit = e.sys(S.COMMIT_PCT)
    commit_top = max(commit) if commit else None
    roomy = False                         # RAM stayed free: what ran short, if anything, was the commit limit
    if avail:
        low = min(avail)
        total = ram_gb * 1024.0 if ram_gb and ram_gb > 0 else None
        if total and low < RAM_CRIT_FRAC * total or low < RAM_CRIT_MB:
            c.support("memory", W_STEADY, f"free RAM fell to {low:.0f} MB" + (f" of {total / 1024:.1f} GB" if total else "")
                      + f" (critical: under {100 * RAM_CRIT_FRAC:.0f}% or {RAM_CRIT_MB:.0f} MB)", low, "MB",
                      RAM_CRIT_FRAC * total if total else RAM_CRIT_MB)
        elif total and low < RAM_WARN_FRAC * total:
            c.support("memory", W_STEADY, f"free RAM fell to {low:.0f} MB of {total / 1024:.1f} GB "
                                          f"(under {100 * RAM_WARN_FRAC:.0f}%)", low, "MB", RAM_WARN_FRAC * total)
        elif total and commit_top is not None and commit_top > COMMIT_CRIT_PCT:
            # item 7: the commit limit is RAM plus the page file, so free RAM is no evidence against reaching it
            # (the first real capture: 89-99.9 % committed all session with 7.9 GB or more of RAM free)
            roomy = True
            c.neutral("memory", f"at least {low / 1024:.1f} GB of {total / 1024:.1f} GB RAM stayed free "
                                f"({100 * low / total:.0f}%), so what ran short was the page file", low, "MB",
                      RAM_WARN_FRAC * total)
        elif total:
            c.against("memory", W_STEADY, f"at least {low / 1024:.1f} GB of {total / 1024:.1f} GB RAM stayed free "
                                          f"({100 * low / total:.0f}%)", low, "MB", RAM_WARN_FRAC * total)
        else:
            c.neutral("memory", f"at least {low:.0f} MB of RAM stayed free (the RAM size is unknown)")
    faults = e.sys(S.HARD_FAULTS_PS)
    if faults:
        hi = st.percentile(faults, 95.0)
        if hi > FAULTS_WARN_PS and roomy:    # LC 4.7: file reads count as hard faults too: with RAM free, no pressure
            c.neutral("memory", f"Windows read {hi:,.0f} pages a second from disk, which with RAM free is file reads "
                                "more than memory pressure", hi, "/s", FAULTS_WARN_PS)
        elif hi > FAULTS_WARN_PS:
            c.support("memory", W_STEADY, f"Windows read {hi:,.0f} pages a second from disk to satisfy memory "
                                          f"(over {FAULTS_WARN_PS:,.0f})", hi, "/s", FAULTS_WARN_PS)
    if commit_top is not None:
        if commit_top > COMMIT_CRIT_PCT:
            c.support("memory", W_STEADY, f"committed memory reached {commit_top:.0f}% of the limit (critical over "
                                          f"{COMMIT_CRIT_PCT:.0f}%)", commit_top, "%", COMMIT_CRIT_PCT)
        elif commit_top > COMMIT_WARN_PCT:
            c.support("memory", W_STATIC, f"committed memory reached {commit_top:.0f}% of the limit (over "
                                          f"{COMMIT_WARN_PCT:.0f}%)", commit_top, "%", COMMIT_WARN_PCT)
    if not (avail or faults or commit):
        c.lack("memory", W_STEADY, "no memory counters were read")
    _event_link(c, "memory_paging", "memory", "Windows paging memory to disk")
    if roomy:
        fixes = [_advice("Let Windows manage the page file size, or make it larger",
                         "Search the Start menu for \"View advanced system settings\", then Performance: Settings > "
                         "Advanced > Virtual memory."),
                 _advice("Close memory-heavy programs while playing", "Browsers with many tabs are the usual ones.")]
        return c.conclude("Windows nearly ran out of memory to hand out: the page file is small",
                          "Programs had reserved almost all the memory Windows can promise (RAM plus the page file) "
                          "while RAM itself stayed free. At that limit a program that asks for more, the game "
                          "included, can stall while the page file grows, or fail to get it.", fixes)
    fixes = [_advice("Close memory-heavy programs while playing", "Browsers with many tabs are the usual ones."),
             _advice("Keep the page file system-managed"), _advice("Add RAM if this happens often")]
    return c.conclude("The PC is low on RAM", "Free memory ran short, so Windows moved memory to and from the disk "
                                              "while the game ran, which stalls frames.", fixes)


# ---------------------------------------------------------------------------------- setup ---

def _power_problems(e: _Env) -> list[tuple[str, str]]:
    """What in the power settings holds the PC back, as (kind, plain words). Kinds: "battery", "saver", "mode",
    "plan_cpu" (the plan caps the CPU or turns boost off: CPU only), "plan_cooling", "oem" (the maker's quiet mode,
    seen through the plan its tool switches to), "plan_name" (a quiet name, in a capture without the plan's
    settings)."""
    p = e.power
    out: list[tuple[str, str]] = []
    if p.get("on_battery") is True:
        pct = fa._bounded(p.get("battery_pct"))
        out.append(("battery", "the laptop ran on battery" + (f" ({pct:.0f}%)" if pct is not None else "")))
    if p.get("saver") is True:
        out.append(("saver", "battery saver is on"))
    mode = p.get("effective_mode")
    if mode in LOW_POWER_MODES:
        out.append(("mode", f"Windows' effective power mode is {str(mode).replace('_', ' ')}"))
    if labels.power_mode(p) == "best_efficiency":
        out.append(("mode", "the power mode is set to Best power efficiency"))
    name = _text(p.get("scheme_name"), 80)
    plan = "the active power plan" + (f" \"{name}\"" if name else "")
    held = _plan_held_back(e)
    for kind in ("plan_cpu", "plan_cooling"):
        said = [text for k, text in held or [] if k == kind]
        if said:
            out.append((kind, f"{plan} " + " and ".join(said)))
    quiet_mode = _oem_quiet_mode(e)
    if quiet_mode:
        out.append(("oem", f"Armoury Crate's quiet mode looks to be on: the active power plan \"{quiet_mode}\" is the "
                           "one it switches to in that mode, which lowers fan speed and power limits outside Windows' "
                           "own plan settings"))
    elif held is None and name and _has_word(name, QUIET_PLAN_WORDS):      # its settings unknown: its name only
        out.append(("plan_name", f"the active power plan is \"{name}\", a quiet, power-saving plan"))
    return out


def _plan_held_back(e: _Env) -> list[tuple[str, str]] | None:
    """What the active plan's own processor settings hold back on the power source in use, as ("plan_cpu" or
    "plan_cooling", words) ([] = nothing); None when the capture has none of them (saved before lag-doctor read them,
    or Windows would not say). The performance-core cap counts only on a hybrid CPU, the only kind it limits."""
    plan = _dict(e.power.get("plan"))
    battery = e.power.get("on_battery") is True
    side = "dc" if battery else "ac"
    max_cpu = _setting_index(plan.get(f"max_cpu_{side}"), PLAN_FULL_CPU)
    max_p = _setting_index(plan.get(f"max_cpu1_{side}"), PLAN_FULL_CPU)
    boost = _setting_index(plan.get(f"boost_{side}"), 6)
    cooling = _setting_index(plan.get(f"cooling_{side}"), 1)
    if max_cpu is None and max_p is None and boost is None and cooling is None:
        return None
    out = []
    if max_cpu is not None and max_cpu < PLAN_FULL_CPU:
        out.append(("plan_cpu", f"caps the CPU at {max_cpu}% of its speed"))
    if max_p is not None and max_p < PLAN_FULL_CPU and _dict(e.inv.get("cpu")).get("hybrid") is True:
        out.append(("plan_cpu", f"caps the CPU's performance cores at {max_p}% of their speed"))
    if boost == PLAN_BOOST_OFF:
        out.append(("plan_cpu", "turns CPU boost off"))
    if not battery and cooling == PLAN_COOLING_PASSIVE:     # on battery, passive is Windows' own default
        out.append(("plan_cooling", "slows the CPU before it speeds up the fans"))
    return out


def _oem_quiet_mode(e: _Env) -> str | None:
    """The active plan's name when the maker's quiet mode looks to be on, else None. Armoury Crate switches the
    Windows plan to one named for its operating mode (Silent, Performance, Turbo: 163 such switches by
    ArmouryCrate.UserSessionHelper.exe in this laptop's System log, read 2026-11-05), so a quiet-named active plan
    while Armoury Crate runs is its quiet mode, whatever the plan's own Windows settings say."""
    name = _text(e.power.get("scheme_name"), 80)
    tools = [t for t in _dict(e.inv.get("oem")).get("tools") or [] if isinstance(t, str)]
    if name and _has_word(name, QUIET_PLAN_WORDS) and any("armoury" in t.lower() for t in tools):
        return name
    return None


def _gpu_limit_lowered(e: _Env) -> tuple[float, float] | None:
    """(the GPU's enforced power limit for most of the capture, its default) when the first is below the second
    (NVML; the default from inventory nvidia.power_default_w), else None."""
    limits = [w for w in e.gpu(G.POWER_LIMIT_W) if w > 0]
    default = _fin(_dict(e.inv.get("nvidia")).get("power_default_w"))
    if not limits or not default:
        return None
    limit = st.median(limits)
    return (limit, default) if limit < default else None


def _setting_index(value: Any, top: int) -> int | None:
    """A power setting's stored index, an int from 0 to `top`; anything else None."""
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= top else None


def _has_word(name: str, words: Iterable[str]) -> bool:
    """`name` holds one of `words` as a whole word: "eco" is in "Eco mode", never in "ASUS Recommended"."""
    return any(re.search(rf"\b{re.escape(w)}\b", name, flags=re.IGNORECASE) for w in words)


def _better_scheme(e: _Env) -> tuple[str, str] | None:
    """The plan to switch to: Balanced if this PC lists it (LC 4.12), else a listed performance plan."""
    active = str(e.power.get("scheme_guid") or "").lower()
    schemes = []
    for s in e.power.get("schemes") or []:
        if isinstance(s, (list, tuple)) and len(s) == 2 and isinstance(s[0], str) and isinstance(s[1], str):
            guid = s[0].strip("{}").lower()
            if GUID_RE.fullmatch(guid):
                schemes.append((guid, _text(s[1], 80) or guid))
    for guid, name in schemes:
        if guid == BALANCED_GUID and guid != active:
            return guid, name
    for guid, name in schemes:
        if guid != active and _has_word(name, PERFORMANCE_PLAN_WORDS):
            return guid, name
    return None


def _power_plan(cap: Capture, ctx: dict) -> Any:
    c = _Case(cap, ctx, "power_plan")
    e = c.env
    p = e.power
    known = [k for k in ("on_battery", "saver", "slider", "effective_mode", "scheme_name") if p.get(k) is not None]
    if not known:
        c.lack("static", W_DIRECT, "the power settings were not read")
        return c.conclude()
    problems = _power_problems(e)
    held = _plan_held_back(e)
    quiet_mode = _oem_quiet_mode(e)
    if not problems:
        name = _text(p.get("scheme_name"), 80)
        parts = ["plugged in" if p.get("on_battery") is False else "power source unknown"]
        if p.get("saver") is False:
            parts.append("battery saver off")
        if name:
            parts.append(f"power plan \"{name}\"" + (", whose own settings let the CPU run at full speed"
                                                     if held == [] else ""))
        mode = labels.power_mode(p)          # a plan type without the slider has no power mode to cite
        if mode:
            parts.append(f"power mode {mode.replace('_', ' ')}")
        c.against("static", W_DIRECT, ", ".join(parts))
        return c.conclude()
    c.support("static", W_DIRECT, "; ".join(text for _, text in problems))
    limit = e.sys(S.CPU_PERF_LIMIT_PCT)
    if limit:
        share = sum(1 for x in limit if x < 100.0) / len(limit)
        if share >= CPU_LIMIT_SHARE:
            c.support("cpu_limits", W_STEADY, f"and the CPU was held below full speed in {100 * share:.0f}% of samples",
                      100 * share, "%", 100 * CPU_LIMIT_SHARE)
    # No GPU heat here: a Windows plan sets no GPU limit, and a hot GPU is gpu_throttle's evidence, not the plan's.
    # Counting it once made a plan that set the CPU exactly as Balanced does the top cause, with a switch to
    # Balanced that changed only the screen brightness. The maker's quiet mode does set the GPU's power limit, so a
    # limit held below the GPU's default counts for it, and for it alone.
    lowered = _gpu_limit_lowered(e) if quiet_mode else None
    if lowered:
        c.support("gpu_telemetry", W_STEADY, f"the GPU's power limit sat at {lowered[0]:.0f} W for most of the "
                                             f"capture, below its {lowered[1]:.0f} W default", lowered[0], "W",
                  lowered[1])
    fixes: list[FixSuggestion] = []
    name = _text(p.get("scheme_name"), 80)
    # Switch plans only for a plan whose own settings hold the PC back. Under Armoury Crate the plan only marks its
    # mode: switching the Windows plan would not change the fan and power mode it sets.
    if held is not None:
        quiet = bool(held)
    else:
        quiet = quiet_mode is None and name is not None and _has_word(name, QUIET_PLAN_WORDS)
    better = _better_scheme(e) if quiet else None
    if better:
        fixes.append(FixSuggestion(title=f"Switch the power plan from \"{name}\" to \"{better[1]}\"",
                                   detail="A standard Windows setting; lag-doctor saves the current plan first.",
                                   fix_id="power_plan", params={"scheme": better[0]}))
    if p.get("on_battery") is True:
        fixes.append(_advice("Plug in the charger while gaming", "Windows and the GPU driver cap power on battery."))
    if p.get("saver") is True:
        fixes.append(_advice("Turn off battery saver while gaming"))
    # Never point at a control the plan does not have: a High performance or Power saver type plan has no power mode.
    if labels.plan_has_power_mode(p) is not False and (labels.power_mode(p) == "best_efficiency"
                                                         or p.get("effective_mode") in LOW_POWER_MODES):
        fixes.append(_advice("Set the power mode to Best performance", "Settings > System > Power & battery."))
    if quiet_mode:
        fixes.append(_advice("Set Armoury Crate's operating mode to Performance or Turbo",
                             "Its quiet mode lowers fan speed and power limits, which no Windows plan setting shows."))
    windows = [text for kind, text in problems if kind != "oem"]
    if not windows:                           # only the maker's quiet mode
        return c.conclude("Armoury Crate's quiet mode is holding the laptop back",
                          f"The active power plan \"{quiet_mode}\" is the one Armoury Crate switches to in its quiet "
                          "mode, which lowers fan speed and power limits, so the CPU and GPU run slower to stay quiet.",
                          fixes)
    return c.conclude("Power saving is holding the PC back", _sentence("; ".join(text for _, text in problems)) +
                      " Power saving lowers how fast the CPU and GPU may run.", fixes)


def _refresh_rate(cap: Capture, ctx: dict) -> Any:
    c = _Case(cap, ctx, "refresh_rate")
    e = c.env
    d = e.display
    if d is None:
        c.lack("static", W_DIRECT, "the game's display was not found")
        return c.conclude()
    hz, top = _fin(d.get("hz")), _fin(d.get("max_hz"))
    screen = _text(d.get("name"), 80) or _text(d.get("device"), 40) or "the game's display"
    if hz is None or top is None or hz <= 0 or top <= 0:
        c.lack("static", W_DIRECT, f"the refresh rates of {screen} were not read")
        return c.conclude()
    if top - hz <= REFRESH_SLACK_HZ:
        c.against("static", W_DIRECT, f"{screen} already runs at its maximum, {hz:.0f} Hz", hz, "Hz", top)
        return c.conclude()
    c.support("static", W_DIRECT, f"{screen} runs at {hz:.0f} Hz but offers {top:.0f} Hz at this resolution",
              hz, "Hz", top)
    fps = e.avg_fps
    if e.frames_measured and (e.verdict == "vsync" or (fps is not None and fps >= LIMIT_FPS_FRAC * hz)):
        c.support("frame_timing", W_STEADY, f"and the game's {fps:.0f} FPS is held to that {hz:.0f} Hz refresh"
                  if fps else "and the game is held to that refresh rate")
    device = d.get("device") if isinstance(d.get("device"), str) and DEVICE_RE.fullmatch(d["device"]) else None
    battery = e.power.get("on_battery") is True
    note = " A lower rate saves battery, so keep it if that is on purpose." if battery else ""
    fixes = ([FixSuggestion(title=f"Set {screen} to {top:.0f} Hz", detail="Windows tests the mode first." + note, fix_id="refresh_rate",
                            params={"device": device, "hz": int(round(top))})]
             if device else
             [_advice(f"Set {screen} to {top:.0f} Hz", "Settings > System > Display > Advanced display." + note)])
    return c.conclude(f"The screen runs at {hz:.0f} Hz, below its {top:.0f} Hz maximum",
                      "The display refreshes less often than it can, so it can show fewer frames per second than "
                      "the game makes.", fixes)


def _vsync_judder(cap: Capture, ctx: dict) -> Any:
    """LC 2.4, 4.14: VSync on a fixed refresh with the game a little slower than it, so frames alternate between
    one and two refreshes. Frames dropped because the game runs faster than the screen are dropped_frames'."""
    c = _Case(cap, ctx, "vsync_judder")
    e = c.env
    if e.verdict == "capped":
        return None                        # a cap that does not divide the refresh: the frame_cap rule's case
    q = _fin(e.bound.get("quantised_pct"))
    hz = e.refresh
    if not hz:
        c.lack("frame_timing", W_STEADY, "the display's refresh rate is unknown")
        return c.conclude()
    if q is None:
        notes = [n for n in e.bound.get("notes") or [] if isinstance(n, str)]
        c.lack("frame_timing", W_STEADY, "whether frames sat on whole refresh periods was not judged"
               + (f": {notes[0]}" if notes else ""))
        return c.conclude()
    if e.bound.get("vrr") is True or q < QUANTISED_PCT:
        c.against("frame_timing", W_STEADY, f"frames are not tied to refresh periods ({q:.0f}% on whole periods): "
                                            "VRR or VSync off", q, "%", QUANTISED_PCT)
        return c.conclude()
    period = 1000.0 / hz
    _, _, d, source = fa.display_series(e.fr)
    if source != "display" or not d:
        c.lack("frame_timing", W_STEADY, "the displayed frame times were not measured")
        return c.conclude()
    k = [max(1, round(x / period)) for x in d]
    if st.median(k) != 1:
        return None                        # under half the refresh rate: that is low FPS, not judder
    if _uneven_presents(e):
        return None                        # the game's own uneven frames: the uneven_pacing rule's case
    share = sum(1 for x in k if x >= 2) / len(k)
    ids = [ev.id for ev in e.events if ev.kind == "pacing"]
    shared = _contention(e)
    if share >= JUDDER_SHARE and shared is not None and e.verdict == "unknown":
        # A7: a shared GPU hid whether a cap (frame_cap's case, above), VSync or the GPU itself sets the cadence
        return Unchecked("vsync_judder", f"{100 * share:.0f}% of frames stayed on screen for 2 or more refreshes, but "
                                         f"other programs kept the game's GPU {shared:.0f}% busy, so whether VSync, a "
                                         "frame-rate cap or the shared GPU sets that cadence could not be told")
    if share >= JUDDER_SHARE:
        c.support("frame_timing", W_STEADY, f"{100 * share:.0f}% of frames stayed on screen for 2 or more refreshes "
                                            f"({2 * period:.1f} ms) while most took one ({period:.1f} ms): judder "
                                            f"({100 * JUDDER_SHARE:.0f}% or more)", 100 * share, "%", 100 * JUDDER_SHARE)
    else:
        c.against("frame_timing", W_STEADY, f"{100 * (1 - share):.0f}% of frames were shown for one refresh: no judder",
                  100 * share, "%", 100 * JUDDER_SHARE)
    fixes = [_advice("Turn on VRR (G-SYNC or FreeSync) if the screen supports it"),
             _advice(f"Or cap the frame rate at {hz / 2:.0f} FPS (half the refresh)", "Every frame then stays on "
                                                                                 "screen for the same time."),
             _advice("Or turn VSync off", "That trades judder for tearing.")]
    return c.conclude("VSync judder: frames alternate between one and two refreshes",
                      "The game runs a little slower than the screen refreshes, so with VSync some frames stay up "
                      "twice as long as others, which looks jerky.", fixes, ids)


@dataclass
class _Dropped:
    """A11: the game presents faster than a fixed refresh, so frames are never shown and motion jumps; only
    the animation error sees it (every shown frame holds one refresh)."""

    events: list[LagEvent]       # the pacing events only the animation-error test flagged
    share: float                 # of the capture they cover, 0..1
    present_fps: float
    dropped_pct: float
    anim_ms: float               # their worst animation-error p95


def _dropped_judder(e: _Env, cap_fps: float | None = None) -> _Dropped | None:
    """One predicate for dropped_frames and frame_cap: anim-only pacing events on whole refresh periods (their own
    quantised_pct, LC 3.3) over UNEVEN_SHARE of the capture or more, DROPPED_PCT of frames never shown, and the game
    presenting faster than a fixed refresh (VRR not seen) (A11, LC 2.4). It needs no bound verdict: a capture too
    short for one (LC 2.6) still shows this."""
    hz, dropped = e.refresh, _fin(e.stats.get("dropped_pct"))
    if not hz or e.duration is None or e.bound.get("vrr") is True or dropped is None or dropped < DROPPED_PCT:
        return None
    fps = cap_fps or _present_fps(e)
    if fps is None or fps <= hz + REFRESH_SLACK_HZ:
        return None
    evs = [ev for ev in e.events if ev.kind == "pacing" and _dict(ev.detail).get("criterion") == "anim"
           and (_fin(_dict(ev.detail).get("quantised_pct")) or 0.0) >= QUANTISED_PCT]
    share = min(1.0, _union_s((ev.t_start, ev.t_end) for ev in evs) / e.duration)
    if not evs or share < UNEVEN_SHARE:
        return None
    return _Dropped(evs, share, fps, dropped, max(float(ev.severity) for ev in evs))


def _dropped_frames(cap: Capture, ctx: dict) -> Any:
    """A11: the game makes frames faster than a fixed refresh, so some are never shown and motion jumps. Its own
    cause: VSync is not involved. Under a cap above the refresh the frame_cap rule claims it (one predicate,
    _dropped_judder); the measurement that rules it out is said in the order that predicate tests it."""
    c = _Case(cap, ctx, "dropped_frames")
    e = c.env
    if e.verdict == "capped":
        return None                        # frame_cap's case: a cap set above the refresh
    dj = _dropped_judder(e)
    if dj is not None:
        return _dropped_frames_finding(c, dj, cap_fps=None)
    hz, dropped, fps = e.refresh, _fin(e.stats.get("dropped_pct")), _present_fps(e)
    if not hz:
        c.lack("frame_timing", W_STEADY, "the display's refresh rate is unknown")
    elif e.bound.get("vrr") is True:
        c.against("frame_timing", W_STEADY, "VRR was on, which shows each frame as it comes instead of dropping it")
    elif fps is not None and fps <= hz + REFRESH_SLACK_HZ:
        c.against("frame_timing", W_STEADY, f"the game presented about {fps:.0f} FPS, no faster than the {hz:.0f} Hz "
                                            "refresh, so it made no frame the screen could not show", fps, "FPS", hz)
    elif dropped is None:
        c.lack("frame_timing", W_STEADY, "which frames reached the screen was not tracked")
    elif dropped < DROPPED_PCT:
        c.against("frame_timing", W_STEADY, f"only {dropped:.1f}% of the frames were never shown (under "
                                            f"{DROPPED_PCT:.0f}%)", dropped, "%", DROPPED_PCT)
    else:
        c.neutral("frame_timing", f"{dropped:.0f}% of the frames were never shown, but motion jumped over less than "
                                  f"{100 * UNEVEN_SHARE:.0f}% of the capture", dropped, "%", DROPPED_PCT)
    return c.conclude()


def _present_fps(e: _Env) -> float | None:
    """How fast the game presents: 1000 / the median MsBetweenPresents of its own frames, else the bound's FT."""
    fm, ft = e.fr.get(F.FRAME_MS) or [], _fin(e.bound.get("ft_ms"))
    ms = [float(x) for i, x in enumerate(fm) if fa._pos(x) and fa._is_app(e.fr, i)]
    if ms:
        return 1000.0 / st.median(ms)
    return 1000.0 / ft if ft and ft > 0 else None


def _dropped_frames_finding(c: _Case, dj: _Dropped, cap_fps: float | None) -> Any:
    """The finding for dropped-frame judder: dropped_frames' when the game runs uncapped, frame_cap's under a cap
    set above the refresh."""
    hz = c.env.refresh
    how = f"is capped at {cap_fps:.0f} FPS" if cap_fps is not None else f"presented about {dj.present_fps:.0f} FPS"
    c.support("frame_timing", W_STEADY, f"the game {how}, above the {hz:.0f} Hz refresh: {dj.dropped_pct:.0f}% of its "
                                        f"frames were never shown, and motion jumped (animation error up to "
                                        f"{dj.anim_ms:.1f} ms, p95) over {100 * dj.share:.0f}% of the capture",
              dj.dropped_pct, "%", DROPPED_PCT)
    fixes = [_advice(f"Cap the frame rate at {hz:.0f} FPS (the refresh rate)",
                     "In the game, the GPU driver or RTSS; then every frame made is a frame shown."),
             _advice("Or turn on VSync", "The game then waits for each refresh instead of making frames that are "
                                         "never shown."),
             _advice("Or turn on VRR (G-SYNC or FreeSync) if the screen supports it")]
    title = (f"A frame-rate cap above the refresh rate drops frames: {cap_fps:.0f} FPS on a {hz:.0f} Hz screen"
             if cap_fps is not None else
             f"Frames are dropped: the game runs faster than the {hz:.0f} Hz screen")
    return c.conclude(title, "The game makes more frames than the screen can show, so some are never shown and "
                             "motion jumps unevenly although every shown frame is on time.", fixes,
                      [ev.id for ev in dj.events])


def _uneven_present_events(e: _Env) -> list[LagEvent]:
    """Pacing events where the game's own presents swing, not just the display (frames.py's detail)."""
    out = []
    for ev in e.events:
        if ev.kind != "pacing":
            continue
        pct = _fin(ev.detail.get("present_swing_pct")) if isinstance(ev.detail, dict) else None
        if pct is not None and pct > UNEVEN_PRESENT_PCT:
            out.append(ev)
    return out


def _uneven_share(e: _Env) -> tuple[list[LagEvent], float | None]:
    """(the uneven-present pacing events, the share of the capture they cover); one predicate for both rules."""
    evs = _uneven_present_events(e)
    if e.duration is None:
        return evs, None
    return evs, min(1.0, _union_s((ev.t_start, ev.t_end) for ev in evs) / e.duration)    # A11: never past 100 %


def _uneven_presents(e: _Env) -> bool:
    _, share = _uneven_share(e)
    return share is not None and share >= UNEVEN_SHARE


def _uneven_pacing(cap: Capture, ctx: dict) -> Any:
    c = _Case(cap, ctx, "uneven_pacing")
    e = c.env
    uneven, share = _uneven_share(e)
    if share is None:
        c.lack("frame_timing", W_STEADY, "the capture's length is unknown")
        return c.conclude()
    if share >= UNEVEN_SHARE:
        swing = max(_fin(ev.detail.get("present_swing_pct")) or 0.0 for ev in uneven)
        sev = st.median([float(ev.severity) for ev in uneven])
        # A1: one frame-time pattern is a steady metric past its threshold, never a direct readout (LC 5.1)
        c.support("frame_timing", W_STEADY, f"the game's own frame times swung by {ev_mod.PACING_SWING_MS:.0f} ms or "
                                            f"more on up to {swing:.0f}% of frames (median swing {sev:.1f} ms), over "
                                            f"{100 * share:.0f}% of the capture ({100 * UNEVEN_SHARE:.0f}% or more)",
                  100 * share, "%", 100 * UNEVEN_SHARE)
    else:
        judder = [ev for ev in e.events if ev.kind == "pacing" and ev not in uneven]
        c.against("frame_timing", W_STEADY,
                  f"the game presented its frames evenly (uneven stretches: {100 * share:.0f}% of the capture)"
                  + ("; the uneven display comes from the screen's refresh, not the game" if judder else ""),
                  100 * share, "%", 100 * UNEVEN_SHARE)
    fixes = [_advice("Cap the frame rate a little below what the PC can hold", "In the game, the GPU driver or RTSS: "
                                                                               "an even cadence feels smoother."),
             _advice("Turn on VRR (G-SYNC or FreeSync) if the screen supports it")]
    return c.conclude("Uneven frame pacing: frame times keep swinging",
                      "The game hands over its frames at uneven intervals, so motion looks jerky even when the "
                      "average frame rate is fine.", fixes, [ev.id for ev in uneven])


def _frame_cap(cap: Capture, ctx: dict) -> Any:
    c = _Case(cap, ctx, "frame_cap")
    e = c.env
    v, b = e.verdict, e.bound
    if v == "unknown":
        notes = [n for n in b.get("notes") or [] if isinstance(n, str)]
        c.lack("frame_timing", W_STEADY, "the frame-rate limit could not be judged" + (f": {notes[0]}" if notes else ""))
        return c.conclude()
    if v != "capped":
        c.against("frame_timing", W_STEADY, f"no frame-rate cap: the frame rate is limited by {labels.BOUND.get(v, v)}")
        return c.conclude()
    fps, hz, r = _fin(b.get("cap_fps")), e.refresh, _fin(b.get("r"))
    if fps is None:
        c.lack("frame_timing", W_STEADY, "the cap's rate was not measured")
        return c.conclude()
    shared = _contention(e)
    if shared is not None:     # A7: a GPU other programs keep busy can pace the game as evenly as a cap does
        return Unchecked("frame_cap", f"the game ran at a steady {fps:.0f} FPS, but other programs kept its GPU "
                                      f"{shared:.0f}% busy, so that may be them rather than a frame-rate cap")
    if not hz:
        return Unchecked("frame_cap", f"the game is capped at {fps:.0f} FPS, but the display's refresh rate is "
                                      "unknown, so the cap cannot be compared with it")
    if fps > hz + REFRESH_SLACK_HZ:
        # A11: a cap above a fixed refresh is no sensible cap: the screen never shows the frames beyond it
        dropped = _dropped_judder(e, cap_fps=fps)
        if dropped is not None:
            return _dropped_frames_finding(c, dropped, cap_fps=fps)
        c.neutral("frame_timing", f"capped at {fps:.0f} FPS, above the {hz:.0f} Hz refresh: the screen shows at most "
                                  f"{hz:.0f} of those frames a second", fps, "FPS", hz)
        return c.conclude()
    if fps >= CAP_FRAC * hz:
        c.against("frame_timing", W_STEADY, f"capped at {fps:.0f} FPS, within {100 * (1 - CAP_FRAC):.0f}% of the "
                                            f"{hz:.0f} Hz refresh: a sensible cap", fps, "FPS", CAP_FRAC * hz)
        return c.conclude()
    if r is not None and r >= CAP_R_MAX:
        c.neutral("frame_timing", f"capped at {fps:.0f} FPS, but the GPU is already busy {100 * r:.0f}% of each "
                                  "frame, so raising the cap would gain little", fps, "FPS", CAP_FRAC * hz)
        return c.conclude()
    c.support("frame_timing", W_STEADY, f"the game is capped at {fps:.0f} FPS, under {100 * CAP_FRAC:.0f}% of the "
                                        f"{hz:.0f} Hz refresh ({CAP_FRAC * hz:.0f} FPS)", fps, "FPS", CAP_FRAC * hz)
    k = hz / fps
    if abs(k - round(k)) > 0.05:
        c.support("frame_timing", W_STEADY, f"{hz:.0f} is not a whole multiple of {fps:.0f}, so frames stay on screen "
                                            "for uneven times (some one refresh, some two)")
    ids = [ev.id for ev in e.events if ev.kind == "pacing"]
    fixes = [_advice(f"Raise the cap to {hz:.0f} FPS, or a few FPS below it with VRR",
                     "Caps live in the game's settings, the GPU driver (e.g. NVIDIA's Max Frame Rate) or RTSS."),
             _advice(f"Or cap at {hz / 2:.0f} FPS (half the refresh)", "Every frame then stays up equally long.")]
    return c.conclude(f"A frame-rate cap holds the game at {fps:.0f} FPS on a {hz:.0f} Hz screen",
                      "The game could run faster than its cap, and a cap that does not divide the refresh rate "
                      "shows frames for uneven times.", fixes, ids)


def _windowed_composition(cap: Capture, ctx: dict) -> Any:
    c = _Case(cap, ctx, "windowed_composition")
    e = c.env
    mode = _text(e.stats.get("present_mode"), 80)
    runtime = _text(e.stats.get("runtime"), 20)
    if e.frames_measured and mode:
        if mode.startswith("Composed: Copy"):
            c.support("frame_timing", W_STEADY, f"PresentMon shows \"{mode}\" (blt model): every frame is copied "
                                                "before Windows composes the screen, which adds delay")
        else:
            c.against("frame_timing", W_STEADY, f"the game presents with \"{mode}\", not the slower copy (blt) model")
    elif e.frames_measured:
        c.lack("frame_timing", W_STEADY, "PresentMon reported no present mode")
    else:
        c.lack("frame_timing", W_STEADY, e.need("frames"))
    setting = e.settings.get("windowed_optimizations")
    if setting is False:
        c.support("static", W_STATIC, "Windows' \"Optimizations for windowed games\" is off")
    elif setting is True:
        c.neutral("static", "Windows' \"Optimizations for windowed games\" is on", weight=W_STATIC)
    fixes: list[FixSuggestion] = []
    if setting is False and runtime in (None, "DXGI"):
        fixes.append(FixSuggestion(title="Turn on Windows' \"Optimizations for windowed games\"",
                                   detail="It moves DirectX 10 and 11 games in windowed and borderless mode to the "
                                          "faster flip model. The registry format behind it is community-documented.",
                                   fix_id="windowed_optimizations_on", needs_restart="game"))   # WT 8
    fixes.append(_advice("Or switch the game to exclusive fullscreen, or borderless with flip model"))
    return c.conclude("Windowed mode adds a copy and delay", "The game's frames are copied and then composed with the "
                                                              "desktop, which adds delay and costs frame rate.", fixes)


def _game_dvr(cap: Capture, ctx: dict) -> Any:
    c = _Case(cap, ctx, "game_dvr")
    e = c.env
    dvr, background = e.settings.get("game_dvr"), e.settings.get("background_recording")
    if dvr is None and background is None:
        c.lack("static", W_DIRECT, "the Game Bar capture settings were not read")
        return c.conclude()
    others = _advice("Also check NVIDIA Instant Replay or other recorders", "They cost the same kind of GPU time.")
    if background is True:
        c.support("static", W_STEADY, "background recording (\"record what happened\") is on, so Windows keeps "
                                      "encoding the screen while you play")
        # CC-6: the automatic change only where background recording is known to be on (a measured non-game
        # VideoEncode use would do too, but no collector records it yet)
        fixes = [FixSuggestion(title="Turn off Game Bar capture and background recording",
                               detail="Changes two of your own Windows settings; the registry meaning is community-"
                                      "documented.",   # WT 8
                               fix_id="game_dvr_off", needs_restart="game"), others]
        return c.conclude("Background recording is on", "Recording the screen in the background uses GPU time the "
                                                         "game could have.", fixes)
    if background is False:
        c.against("static", W_DIRECT, "background recording is off"
                  + (" (Game Bar capture itself is on, which costs nothing until you record)" if dvr is True else ""))
        return c.conclude()
    if dvr is not True:
        c.against("static", W_DIRECT, "Game Bar capture is off")
        return c.conclude()
    # CC-6: Windows' default state. The capture switch is on and the background setting was not there to read,
    # whose meaning is unverified (WT 8, LC 4.15), so this is advice to check, never an automatic change.
    c.support("static", W_STATIC, "Game Bar capture is on, and its background-recording setting was not there to "
                                  "read, so whether Windows records gameplay in the background is unknown")
    fixes = [_advice("Check whether Windows records gameplay in the background",
                     "In the Xbox Game Bar's capture settings, turn background recording off if you do not use it."),
             others]
    return c.conclude("Game Bar may be recording in the background",
                      "If Windows records gameplay in the background, that costs GPU time the game could have. "
                      "The setting could not be read, so check it.", fixes)


def _game_mode_off(cap: Capture, ctx: dict) -> Any:
    c = _Case(cap, ctx, "game_mode_off")
    e = c.env
    mode = e.settings.get("game_mode")
    if mode is None:
        c.lack("static", W_DIRECT, "the Game Mode setting was not read")
        return c.conclude()
    if mode is True:
        c.against("static", W_DIRECT, "Game Mode is on")
        return c.conclude()
    c.support("static", W_STATIC, "Windows' Game Mode setting is off, so Windows does not favour the game over "
                                  "background work")     # never the title again, word for word (wording)
    fixes = [FixSuggestion(title="Turn Game Mode on", detail="A standard Windows setting (Settings > Gaming > Game "
                                                             "Mode).",
                           fix_id="game_mode_on")]
    return c.conclude("Game Mode is off", "Game Mode lets Windows favour the game over background work.", fixes)


def _not_focused(cap: Capture, ctx: dict) -> Any:
    c = _Case(cap, ctx, "not_focused")
    e = c.env
    vals = [v for v in e.sys(S.GAME_FOCUSED) if v in (0.0, 1.0)]
    if not vals:
        c.lack("foreground", W_STEADY, "the foreground window was not recorded")
        return c.conclude()
    share = sum(1 for v in vals if v == 0.0) / len(vals)
    entry = _dict(e.causes.get("not_focused"))
    linked = entry.get("label") == "supports"
    a, n = _num(entry.get("a")) or 0, _num(entry.get("n")) or 0
    # item 3: lag moments that came while it was behind more often than the session share says are no sample of the
    # session; "in front 94 % of the time" was no evidence when 2 of 3 marks fell in its one stretch behind
    behind = a > 0 and n > 0 and a / n > share
    if share < UNFOCUSED_SHARE and not linked and behind and entry.get("label") == "too_few_events":
        what = "marked moments" if entry.get("half_weight") else "stutters and FPS drops"
        return Unchecked("not_focused", f"the game was the active window {100 * (1 - share):.0f}% of the time, but "
                                        f"{a:.0f} of the {n:.0f} {what} came while another window was in front: too "
                                        f"few to tell (at least {MIN_EVENTS} needed)")
    if share >= UNFOCUSED_SHARE and e.verdict in GATES["cpu"]:
        c.support("foreground", W_STEADY, f"the game was not the active window for {100 * share:.0f}% of the capture "
                                          f"({100 * UNFOCUSED_SHARE:.0f}% or more)", 100 * share, "%", 100 * UNFOCUSED_SHARE)
    elif share >= UNFOCUSED_SHARE:
        # A9 (LC 5.1): less CPU time for a window behind others costs frames only on the CPU side; the
        # event-linked item below still counts on any side
        c.neutral("foreground", f"the game was not the active window for {100 * share:.0f}% of the capture, but the "
                                f"frame rate is limited by {labels.BOUND.get(e.verdict, e.verdict)}, so less CPU time "
                                "is not what holds it back", 100 * share, "%", 100 * UNFOCUSED_SHARE)
    elif not linked and not behind:   # mostly in front is no evidence against lag that came while it was behind
        c.against("foreground", W_STEADY, f"the game was the active window {100 * (1 - share):.0f}% of the time",
                  100 * share, "%", 100 * UNFOCUSED_SHARE)
    _event_link(c, "not_focused", "foreground", "another window being in front")
    battery = e.power.get("on_battery") is True
    fixes = [_advice("Keep the game in front while you play", "Click into its window; Windows gives background "
                                                              "windows a lower priority"
                     + (", and on battery an even lower speed." if battery else "."))]
    ids = _first_cause_ids(e, ("not_focused",))
    return c.conclude("The game was not the active window", "Windows lowers the priority of windows that are not in "
                                                            "front, so the game got less CPU time.", fixes, ids)


# ------------------------------------------------------------------------- background, disk ---

def _event_link(c: _Case, cause: str, family: str, phrase: str, contradict: bool = True) -> bool:
    """Add attribute()'s lift-test verdict for `cause` as an event-linked item (LC 5.2). True if it supports.
    "neutral" and "too_few_events" were measured but say nothing either way; "not_measured" lowers coverage."""
    entry = c.env.causes.get(cause)
    if not isinstance(entry, dict):
        return False
    label, a, n = entry.get("label"), _num(entry.get("a")), _num(entry.get("n"))
    if label == "not_measured":
        c.lack(family, W_STEADY, _text(entry.get("why"), 200) or f"{phrase} was not measured")
        return False
    if a is None or not n:
        return False
    half = bool(entry.get("half_weight"))
    what = "marked moments" if half else "stutters and FPS drops"
    lift = _fin(entry.get("lift"))
    share = 100 * a / n
    if label == "supports":
        w = _fin(entry.get("weight")) or W_STEADY
        c.support(family, w, f"{a:.0f} of {n:.0f} {what} coincided with {phrase}"
                  + (f" (lift {lift:.1f})" if lift is not None else ""), share, "%", 100 * attribute.LIFT_MIN_SHARE)
        return True
    if label == "contradicts" and contradict:
        c.against(family, CONTRA_WEIGHT * (HALF if half else 1.0),
                  f"only {a:.0f} of {n:.0f} {what} coincided with {phrase}", share, "%",
                  100 * attribute.CONTRA_MAX_SHARE)
    else:
        c.neutral(family, f"{a:.0f} of {n:.0f} {what} coincided with {phrase}", share, "%",
                  100 * attribute.LIFT_MIN_SHARE)
    return False


def _first_cause_ids(e: _Env, causes: Iterable[str]) -> list[int]:
    """The judged events (the marks, when they stood in for the frames) whose best cause is one of `causes`. Read
    from the stutters and FPS drops alone, a capture with only marks named none, so the too-few-events branch never
    fired and the causes its marks named left both lists (item 11)."""
    want = set(causes)
    return [ev.id for ev in e.judged if ev.causes and ev.causes[0] in want]


def _name_cpu(e: _Env, name: str, spans: list[tuple[float, float]],
              pids: set[int] | None = None) -> tuple[float | None, float | None]:
    """(mean CPU % of the processes named `name` over the spans, median over the other ticks): 100 = one CPU.
    With `pids`, only those processes count, so a steady job of the same name never props up the figure (A5)."""
    during, normal = [], []
    starts = [a for a, _ in spans]
    for row in e.cap.others if isinstance(e.cap.others, list) else []:
        if not (isinstance(row, (list, tuple)) and len(row) == 2 and fa._bounded(row[0]) is not None
                and isinstance(row[1], (list, tuple))):
            continue
        t = float(row[0])
        total = math.fsum(float(x) for x in (fa._bounded(p[2]) for p in row[1] if isinstance(p, (list, tuple))
                                             and len(p) >= 3 and p[1] == name and (pids is None or p[0] in pids))
                          if x is not None and x >= 0)
        i = bisect.bisect_right(starts, t) - 1
        (during if i >= 0 and t <= spans[i][1] else normal).append(total)
    return (st.mean(during) if during else None), (st.median(normal) if normal else None)


@dataclass
class _Named:
    """A program the background_hogs finding names: the processes of one image name that rose (A5), the game's own
    child processes apart from other programs (A10)."""

    name: str                    # the image name exactly as the process table listed it
    role: str                    # "background", or "game_helper": a child process of the game
    events: list[LagEvent]
    pids: list[int]              # the biggest rise across the events first
    rise: dict[int, float]       # pid -> its median rise over the events it rose in
    col: str                     # what rose: "cpu", "io" or "gpu"


def _image_name(v: Any) -> str | None:
    """A process image name from the capture, exactly as listed, when it is one a fix may name: printable,
    1 to 260 characters, no path (fixes/priority.py checks it against the live process)."""
    if not isinstance(v, str) or not 0 < len(v) <= 260 or not v.isprintable() or v.strip() != v or "\\" in v:
        return None
    return v


def _same_image(a: str, b: str | None) -> bool:
    return b is not None and a.casefold() == b.casefold()          # Windows image names ignore case


def _named_programs(e: _Env, blamed: list[LagEvent]) -> list[_Named]:
    """The programs the background_* events blamed, most events first; within each, its processes ordered by how
    much they rose (attribute.py's process_rises), never by PID number (A5)."""
    groups: dict[tuple[str, str], list[LagEvent]] = {}
    for ev in blamed:
        d = _dict(ev.detail)
        name = _image_name(d.get("process"))
        if name:
            role = "game_helper" if d.get("process_role") == "game_helper" else "background"
            groups.setdefault((name, role), []).append(ev)
    out = []
    for (name, role), evs in groups.items():
        col = max(attribute.BLAME_COLUMNS.values(), key=lambda c: sum(
            1 for ev in evs if attribute.BLAME_COLUMNS.get(ev.causes[0]) == c))
        total: dict[int, float] = {}
        each: dict[int, list[float]] = {}
        for ev in evs:
            d = _dict(ev.detail)
            rises = [(p[0], _fin(p[1])) for p in d.get("process_rises") or []
                     if isinstance(p, (list, tuple)) and len(p) == 2 and attribute._is_pid(p[0])]
            if not rises:             # an older capture: its pids are listed biggest rise first, without numbers
                rises = [(p, None) for p in d.get("process_pids") or [] if attribute._is_pid(p)]
            same = attribute.BLAME_COLUMNS.get(ev.causes[0]) == col
            for pid, rise in rises:
                total.setdefault(pid, 0.0)
                if rise is not None and same:
                    total[pid] += rise
                    each.setdefault(pid, []).append(rise)
        pids = sorted(total, key=lambda p: -total[p])        # stable: ties keep the order they were listed in
        out.append(_Named(name, role, evs, pids, {p: st.median(v) for p, v in each.items()}, col))
    out.sort(key=lambda n: -len(n.events))
    return out


def _background_hogs(cap: Capture, ctx: dict) -> Any:
    c = _Case(cap, ctx, "background_hogs")
    e = c.env
    if not e.attributed:
        return None                          # no stutter or FPS drop to explain
    kinds = ("background_cpu", "background_disk", "background_gpu")
    entries = {k: e.causes.get(k) for k in kinds if isinstance(e.causes.get(k), dict)}
    if entries and all(v.get("label") == "too_few_events" for v in entries.values()):
        n = len(e.attributed)
        return Unchecked("background_hogs", f"only {n} stutter{'s' if n != 1 else ''} and FPS drops: linking them to "
                                            f"another program needs at least {MIN_EVENTS}")
    if not entries or all(v.get("label") == "not_measured" for v in entries.values()):
        c.lack("background", W_EVENT_STRONG, "no CPU, disk or GPU readings for other programs")
        return c.conclude()
    # Any one mechanism (CPU, disk, GPU) makes the cause; the others staying quiet is no evidence against it.
    some_support = any(v.get("label") == "supports" for v in entries.values())
    linked = False
    for k, phrase in (("background_cpu", "another program's CPU use rising"),
                      ("background_disk", "another program's disk use rising"),
                      ("background_gpu", "another program's GPU use rising")):
        linked = _event_link(c, k, "background", phrase, contradict=not some_support) or linked
    blamed = [ev for ev in e.attributed if ev.causes and ev.causes[0] in kinds]
    named = []
    for n in _named_programs(e, blamed):
        spans = sorted((ev.t_start - attribute.WINDOW_BEFORE_S, ev.t_end + attribute.WINDOW_AFTER_S) for ev in n.events)
        merged: list[tuple[float, float]] = []
        for a, b in spans:
            if merged and a <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        during, normal = _name_cpu(e, n.name, merged, set(n.pids) if n.pids else None)
        many = f" ({len(n.pids)} processes)" if len(n.pids) > 1 else ""
        shown = _text(n.name, 80) or "a program"
        who = shown if n.role == "background" else f"a helper process of the game, {shown}"
        if during is not None and during >= HOG_MIN_CPU_PCT:
            named.append(n)
            c.neutral("background", f"{who}{many} used {during:,.0f}% of a CPU during {len(n.events)} of "
                                    f"{len(e.attributed)} stutters and FPS drops, against {normal or 0:,.0f}% "
                                    "normally (100% = one logical CPU)", during, "%", HOG_MIN_CPU_PCT)
        elif during is None or n.col != "cpu":
            named.append(n)
            c.neutral("background", f"{who}{many} rose the most during {len(n.events)} of {len(e.attributed)} "
                                    "stutters and FPS drops (its disk or GPU use)")
    if not linked and named:
        c.support("background", W_STATIC, f"{len(blamed)} of {len(e.attributed)} stutters and FPS drops were best "
                                          "explained by another program, too few to be sure")
    if _dict(e.causes.get("background_cpu")).get("label") == "supports":
        _cpu_side_item(c, [ev.id for ev in blamed if ev.causes[0] == "background_cpu"])
    fixes = _hog_fixes(e, named)
    others = [_text(n.name, 80) for n in named if n.role == "background"][:3]
    helpers = [_text(n.name, 80) for n in named if n.role == "game_helper"][:3]
    if helpers and not others:     # A10: the game's own child processes are no background programs
        return c.conclude(f"The game's own helper processes got busy when it lagged: {', '.join(helpers)}",
                          "Processes the game itself started worked hard exactly when it stuttered or dropped frames, "
                          "taking CPU time its main thread needed.", fixes, [ev.id for ev in blamed])
    who = ", ".join(others) or "other programs"
    if helpers:
        who += f"; and the game's own {', '.join(helpers)}"
    return c.conclude(f"Background programs got busy when the game lagged: {who}",
                      "Other programs' work rose exactly when the game stuttered or dropped frames, taking time the "
                      "game needed.", fixes, [ev.id for ev in blamed])


RISE_TEXT = {"cpu": lambda r: f"{r:.0f}% of a CPU", "io": lambda r: f"{r / 1e6:.1f} MB/s",
             "gpu": lambda r: f"{r:.0f}% of a GPU engine"}


def _hog_fixes(e: _Env, named: list[_Named]) -> list[FixSuggestion]:
    game = attribute.game_pids(e.cap)
    game_name = _image_name(e.target.get("name"))
    offer = _offerable(e)
    fixes: list[FixSuggestion] = []
    for n in named[:2]:
        # SEC-2, A10: never the game or its children (the PID check below), nor a process with the game's own
        # image name (a sibling of a multi-process game is no child of the chosen process); the fix needs the PIDs
        if not game or _same_image(n.name, game_name):
            continue
        for pid in n.pids:
            ct = offer.get((pid, n.name))
            if pid in game or ct is None or not 0 < pid <= 0xFFFFFFFF:     # DESIGN: a valid, non-game PID
                continue
            rise = n.rise.get(pid)
            up = f", up {RISE_TEXT[n.col](rise)} when the game lagged" if rise is not None else ""
            fixes.append(FixSuggestion(title=f"Lower the priority of {_text(n.name, 80)} (process {pid}{up}) for this "
                                             "session",
                                       detail="Below-normal priority until it exits. "
                                              "lag-doctor refuses services, protected processes and the game.",
                                       fix_id="background_priority",
                                       # the fix refuses these too: the game and its children, any process with the
                                       # game's name, and a live process whose image name is not `name`
                                       params={"pid": pid, "create_time": ct, "class": "below_normal",
                                               "exclude": sorted(game)[:4096], "name": n.name,
                                               "game_name": game_name}))
            break
    for n in named[:3]:
        shown = ", ".join(str(p) for p in n.pids[:4]) + (" ..." if len(n.pids) > 4 else "")
        procs = f" (process {shown})" if shown else ""
        name = _text(n.name, 80)
        if n.role == "game_helper":
            fixes.append(_advice(f"{name}{procs} is the game's own helper process",
                                 "The game started it, so lag-doctor does not suggest closing it. Its work (often "
                                 "compiling shaders or loading) usually settles; if it keeps happening, check the "
                                 "game's own settings and forums."))
        elif _same_image(n.name, game_name):
            fixes.append(_advice(f"Check what {name}{procs} is before closing anything",
                                 "It has the game's own name, so it may be part of the game (a renderer or a launcher "
                                 "process); lag-doctor never closes programs itself."))
        else:
            fixes.append(_advice(f"Pause or close {name} while you play{procs}",
                                 "Check it is not something you need first; lag-doctor never closes programs itself."))
    fixes.append(_advice("Pause downloads and updates while playing", "Steam, Epic, the browser, Windows Update."))
    return fixes


def _offerable(e: _Env) -> dict[tuple[int, str], int]:
    """(pid, image name) -> creation time, for the processes a priority fix may name (SEC-2). From the others
    table's sixth field (older captures lack it), and only from the rows where that PID was listed under that
    name; none when that (pid, name) was listed with two creation times, or the PID was listed under another
    name later in the capture (a reused PID)."""
    times: dict[tuple[int, str], set[int]] = {}
    last: dict[int, tuple[float, str]] = {}
    for row in e.cap.others if isinstance(e.cap.others, list) else []:
        t = fa._bounded(row[0]) if isinstance(row, (list, tuple)) and len(row) == 2 else None
        if t is None or not isinstance(row[1], (list, tuple)):
            continue
        for p in row[1]:
            if not (isinstance(p, (list, tuple)) and len(p) >= 2 and attribute._is_pid(p[0])
                    and isinstance(p[1], str)):
                continue
            if p[0] not in last or t >= last[p[0]][0]:
                last[p[0]] = (t, p[1])
            ct = p[5] if len(p) >= 6 else None
            if isinstance(ct, int) and not isinstance(ct, bool) and 0 < ct < 2 ** 63:
                times.setdefault((p[0], p[1]), set()).add(ct)
    return {key: next(iter(cts)) for key, cts in times.items() if len(cts) == 1 and last[key[0]][1] == key[1]}


def _hdd_streaming(cap: Capture, ctx: dict) -> Any:
    c = _Case(cap, ctx, "hdd_streaming")
    e = c.env
    media = e.game.get("media")
    drive = _text(e.game.get("drive"), 8)
    where = f" ({drive})" if drive else ""
    if media == SOLID_STATE:
        c.against("static", W_DIRECT, f"the game is installed on an SSD{where}")
        return c.conclude()
    if media != HARD_DISK:
        c.lack("static", W_DIRECT, "the type of the game's drive is unknown")
        return c.conclude()
    c.support("static", W_DIRECT, f"the game is installed on a hard disk (HDD){where}")
    _event_link(c, "asset_streaming", "disk", "the game reading from disk")
    lat = e.sys(S.DISK_READ_MS)
    if lat and st.percentile(lat, 95.0) > DISK_READ_WARN_MS:
        p95 = st.percentile(lat, 95.0)
        c.support("disk", W_STEADY, f"disk reads took up to {p95:.0f} ms (95th percentile; over "
                                    f"{DISK_READ_WARN_MS:.0f} ms is slow)", p95, "ms", DISK_READ_WARN_MS)
    fixes = [_advice("Move the game to an SSD", "Most launchers can move an installed game (Steam: Properties > "
                                                "Installed Files)."),
             _advice("Close other disk-heavy programs while playing")]
    return c.conclude("The game is on a hard drive (HDD)", "Hard drives take far longer to load game data than "
                                                           "SSDs, so streaming new areas makes the game wait.",
                      fixes, _first_cause_ids(e, ("asset_streaming",)))


def _shader_compile(cap: Capture, ctx: dict) -> Any:
    c = _Case(cap, ctx, "shader_compile")
    e = c.env
    own = [ev for ev in e.attributed if ev.kind == "stutter" and ev.causes and ev.causes[0] == "game_cpu_spike"]
    if not own:
        return None
    if len(own) < MIN_EVENTS:
        return Unchecked("shader_compile", f"only {len(own)} stutters came from the game's own work: too few to tell "
                                           "whether they bunch up at the start")
    if e.duration is None:
        c.lack("frame_timing", W_STEADY, "the capture's length is unknown")
        return c.conclude()
    early = sum(1 for ev in own if ev.t_start < EARLY_FRACTION * e.duration)
    share = early / len(own)
    if share >= EARLY_SHARE:
        c.support("frame_timing", W_STATIC, f"{early} of {len(own)} stutters from the game's own work came in the first "
                                            f"third of the capture ({100 * EARLY_SHARE:.0f}% or more)",
                  100 * share, "%", 100 * EARLY_SHARE)
    else:
        c.against("frame_timing", W_STEADY, f"the game's own stutters were spread through the capture ({early} of "
                                            f"{len(own)} in the first third)", 100 * share, "%", 100 * EARLY_SHARE)
    fixes = [_advice("Let the game finish compiling shaders", "Many games do it in a loading step; play through "
                                                              "once and the stutter fades."),
             _advice("Record the same route a second time", "If the stutters drop by half or more, it was shader "
                                                            "compilation."),   # LC 4.9
             _advice("Expect it again after a game or GPU driver update", "Those clear the shader cache.")]
    return c.conclude("Possible shader-compilation stutter early in the session",
                      "The game's own stutters bunched up at the start, as when it compiles shaders the first time "
                      "it draws something.", fixes, [ev.id for ev in own])


# ------------------------------------------------------------------- stutter causes (LC 5.2) ---

STUTTER_CAUSES = {
    # cause: (key family, phrase, CPU side?, title, advice)
    "game_cpu_spike": ("cpu_threads", "the game's own CPU work", True,
                       "Stutters from the game's own CPU work",
                       [("Turn down CPU-heavy settings", "Simulation detail, crowds, view distance."),
                        ("Update the game and the GPU driver", "Loading and compile stutters are often fixed there.")]),
    "asset_streaming": ("disk", "the game reading from disk", True, "Stutters while the game loads from disk",
                        [("Install the game on a fast SSD", ""),
                         ("Lower texture quality or streaming settings", "Less data to load per second."),
                         ("Close other disk-heavy programs while playing", "")]),
    "driver_dpc": ("interrupts", "a driver holding up the CPU", False, "Stutters while a driver held up the CPU",
                   [("Update or briefly disable Wi-Fi, audio and RGB utilities, then test again", ""),
                    ("Run LatencyMon to name the driver", "It needs administrator rights; lag-doctor cannot name it.")]),
    "gpu_spike": ("frame_timing", "a moment of heavy GPU work", False, "Stutters from moments of heavy GPU work",
                  [("Turn down the heaviest graphics effects", "Spikes like these often come from effects or a "
                                                               "shader used for the first time.")]),
    "game_stall": ("frame_timing", "the game waiting on itself", True, "Stutters where the game waited on itself",
                   [("Nothing on this PC moved at those moments", "The game paused on its own (a lock, a sync, a "
                                                                  "server). Update the game; check its forums."),
                    ("Note where in the game it happens", "Stalls at the same spots point at the game.")]),
}
RESIDUAL_CAUSES = ("gpu_spike", "game_stall")


def _stutter_cause(cap: Capture, ctx: dict, cause: str) -> Any:
    c = _Case(cap, ctx, cause)
    e = c.env
    family, phrase, cpu_side, title, advice = STUTTER_CAUSES[cause]
    if not e.causes:
        if not e.frames_measured:
            return Unchecked(cause, e.need("frames") + "; press the lag key when it lags to mark moments instead")
        return None                          # no stutter or FPS drop to explain
    entry = e.causes.get(cause)
    if not isinstance(entry, dict):
        return None
    label, n = entry.get("label"), _num(entry.get("n")) or 0
    ids = _first_cause_ids(e, (cause,))
    half = bool(entry.get("half_weight"))
    what = "marked moments" if half else "stutters and FPS drops"
    if label == "not_measured":
        return Unchecked(cause, _text(entry.get("why"), 200) or "its signal was not measured")
    if label == "too_few_events":
        return (Unchecked(cause, f"only {n:.0f} {what}: blaming a cause needs at least {MIN_EVENTS}") if ids else None)
    if cause in RESIDUAL_CAUSES:
        if half:
            return None                      # attribute.py works out no residual for marks: nothing was judged
        _residual_items(c, entry, ids, phrase, what)
    else:
        _event_link(c, cause, family, phrase)
        if label not in ("supports", "contradicts") and ids:
            c.support(family, W_STATIC, f"{len(ids)} of {n:.0f} {what} were best explained by {phrase}, too few to be "
                                        "sure")
    if cpu_side and not half:
        _cpu_side_item(c, ids)
    for ev in e.judged:
        if ev.id in ids and isinstance(ev.detail.get("why"), list) and len(ev.detail["why"]) > 1:
            c.neutral(family, f"for example at {ev.t_start:.1f} s: {_text(ev.detail['why'][1], 200)}")
            break
    fixes = [_advice(t, d) for t, d in advice]
    share = _fin(entry.get("share")) or 0.0
    if half and title.startswith("Stutters"):     # no stutter was measured: the moments are the user's own
        title = "Lag you marked" + title[len("Stutters"):]
    return c.conclude(f"{title} ({len(ids)} of {n:.0f})",
                      _sentence(f"{len(ids)} of {n:.0f} {what} were best explained by {phrase} "
                                f"({100 * share:.0f}%)"), fixes, ids)


def _cpu_side_item(c: _Case, ids: list[int]) -> None:
    """Frame-timing evidence for a CPU-side cause: in most of its events the slow frame's GPU busy share was low
    (attribute.py's per-event split, LC 3.1), a separate instrument from the CPU counters that named the cause."""
    if not ids:
        return
    want = set(ids)
    # A3: a side the overall verdict stood in for (its frames had no GPU timing) is no frame-timing evidence
    cpu = sum(1 for ev in c.env.attributed if ev.id in want and _dict(ev.detail).get("side") == "cpu"
              and _dict(ev.detail).get("side_source") != "verdict")
    if cpu / len(want) >= SIDE_SHARE:
        c.support("frame_timing", W_STEADY, f"the GPU sat idle through most of the slow frame in {cpu} of "
                                            f"{len(want)} of them, so the CPU side was slow",
                  100 * cpu / len(want), "%", 100 * SIDE_SHARE)


def _residual_items(c: _Case, entry: dict, ids: list[int], phrase: str, what: str) -> None:
    """gpu_spike / game_stall have no signal of their own: they are what is left when nothing measured moved."""
    e = c.env
    label, a, n = entry.get("label"), _num(entry.get("a")) or 0, _num(entry.get("n")) or 0
    if label == "supports":
        c.support("frame_timing", W_STEADY, f"in {a:.0f} of {n:.0f} {what} no other measured cause moved, which "
                                            f"leaves {phrase}", 100 * a / n if n else None, "%",
                  100 * attribute.LIFT_MIN_SHARE)
    elif label == "contradicts":
        c.against("frame_timing", CONTRA_WEIGHT, f"only {a:.0f} of {n:.0f} {what} were left unexplained like this")
    elif ids:
        c.support("frame_timing", W_STATIC, f"{len(ids)} of {n:.0f} {what} were left unexplained like this, too few "
                                            "to be sure")
    else:
        c.neutral("frame_timing", f"{a:.0f} of {n:.0f} {what} were left unexplained like this")
    for other, (fam, _, _, _, _) in STUTTER_CAUSES.items():
        o = e.causes.get(other)
        if other not in RESIDUAL_CAUSES and isinstance(o, dict) and o.get("label") == "not_measured":
            c.lack(fam, W_STEADY, f"{labels.cause_text(other)} was not measured, so it was not ruled out")
    if c.cause == "game_stall" and ids:
        thread = e.sys_rows(S.GAME_TOP_THREAD_PCT)
        levels = []
        for ev in e.attributed:
            if ev.id in ids:
                xs = [v for t, v in thread if ev.t_start - attribute.WINDOW_BEFORE_S <= t <= ev.t_end + attribute.WINDOW_AFTER_S]
                if xs:
                    levels.append(max(xs))
        if levels and st.median(levels) < 100.0 * attribute.THREAD_BUSY:
            c.support("cpu_threads", W_STEADY, f"the game's busiest thread stayed under {100 * attribute.THREAD_BUSY:.0f}% "
                                               f"of a CPU through them (median peak {st.median(levels):.0f}%): it was "
                                               "waiting, not working", st.median(levels), "%", 100 * attribute.THREAD_BUSY)


def _stutter_rule(cause: str) -> Callable[[Capture, dict], Any]:
    def fn(cap: Capture, ctx: dict) -> Any:
        return _stutter_cause(cap, ctx, cause)
    fn.__name__ = f"_{cause}"
    return fn


# -------------------------------------------------------------------------------- network ---

def _not_wifi(c: _Case) -> Cleared | Unchecked | None:
    """For the Wi-Fi rules: the verdict when the PC is wired (ruled out) or its link type is unknown, else None."""
    on = c.env.on_wifi()
    if on is False:
        return Cleared(c.cause, "The PC is on a wired connection, not Wi-Fi.")
    if on is None:
        return Unchecked(c.cause, "no Wi-Fi readings: the PC may be wired, or its Wi-Fi could not be read")
    return None


def _wifi_weak(cap: Capture, ctx: dict) -> Any:
    c = _Case(cap, ctx, "wifi_weak")
    e = c.env
    done = _not_wifi(c)
    if done is not None:
        return done
    rssi = [x for x in e.net_readings(N.WIFI_RSSI_DBM) if -120.0 <= x <= 0.0]
    if not rssi:     # NE 3.3 (verify #50): link quality 0..100 maps linearly onto -100..-50 dBm
        rssi = [-100.0 + q / 2.0 for q in e.net_readings(N.WIFI_SIGNAL_PCT) if 0 <= q <= 100]
    if rssi:
        m = st.median(rssi)
        if m < RSSI_WEAK_DBM:
            c.support("wifi", W_STEADY, f"the Wi-Fi signal was {m:.0f} dBm (weaker than {RSSI_WEAK_DBM:.0f} dBm)",
                      m, "dBm", RSSI_WEAK_DBM)
        else:
            c.against("wifi", W_STEADY, f"the Wi-Fi signal was {m:.0f} dBm (stronger than {RSSI_WEAK_DBM:.0f} dBm)",
                      m, "dBm", RSSI_WEAK_DBM)
    else:
        c.lack("wifi", W_STEADY, "the Wi-Fi signal strength was not read")
    gw = e.net_values(N.GW_RTT_MS)
    if len(gw) >= 10:
        p95 = st.percentile(gw, 95.0)
        if p95 > GW_BAD_P95_MS:
            c.support("network_path", W_STEADY, f"pings to your router took up to {p95:.0f} ms (95th percentile; over "
                                                f"{GW_BAD_P95_MS:.0f} ms is bad on Wi-Fi)", p95, "ms", GW_BAD_P95_MS)
        elif p95 < GW_GOOD_P95_MS:
            c.against("network_path", W_STEADY, f"pings to your router took at most {p95:.0f} ms (95th percentile)",
                      p95, "ms", GW_GOOD_P95_MS)
    fixes = [_advice("Move closer to the router, or remove what is in between"),
             _advice("Use the 5 GHz band", "It is faster and less crowded than 2.4 GHz."),
             _advice("Use a network cable", "Ethernet takes Wi-Fi out of the picture.")]
    return c.conclude("Weak Wi-Fi signal", "A weak signal makes Wi-Fi resend packets, which shows as ping spikes and "
                                           "lag.", fixes)


def _changes(values: list[Any]) -> int:
    seen = [v for v in values if v is not None]
    return sum(1 for a, b in zip(seen, seen[1:]) if a != b)


def _wifi_roaming(cap: Capture, ctx: dict) -> Any:
    c = _Case(cap, ctx, "wifi_roaming")
    e = c.env
    done = _not_wifi(c)
    if done is not None:
        return done
    t = fa._col(cap.network, N.T)
    aps = [(float(ti), b) for ti, b in ((fa._bounded(ti), _text(x, 40))
                                        for ti, x in zip(t, fa._col(cap.network, N.WIFI_BSSID)))
           if ti is not None and b is not None]
    bssid = [b for _, b in aps]
    chans = [(fa._bounded(ti), attribute.read(N.WIFI_CHANNEL, ch)) for ti, ch in zip(t, fa._col(cap.network, N.WIFI_CHANNEL))]
    chans = [(float(a), int(b)) for a, b in chans if a is not None and b is not None]
    roams = _changes(bssid)
    hops = _changes([ch for _, ch in chans])
    if roams:       # never print the access point's address: it locates the user (NE 3.6)
        c.support("wifi", W_DIRECT, f"the PC switched Wi-Fi access points {roams} time{'s' if roams != 1 else ''} "
                                    "during the capture")
    elif hops:
        c.support("wifi", W_STEADY, f"the Wi-Fi channel changed {hops} time{'s' if hops != 1 else ''} "
                                    f"({chans[0][1]} to {chans[-1][1]}): most likely a switch of access point")
    elif chans:
        c.against("wifi", W_STEADY, f"the PC stayed on Wi-Fi channel {chans[0][1]} throughout"
                  + ("" if any(bssid) else " (the access point itself is only visible with location permission)"))
    else:
        c.lack("wifi", W_STEADY, "the Wi-Fi channel was not read")
    if roams or hops:
        series = aps if roams else chans          # when the switches happened, by what showed them
        times = [tb for (_, a), (tb, b) in zip(series, series[1:]) if a != b]
        spikes = [ev for ev in e.events if ev.kind in ("net_spike", "net_loss")]
        near = sum(1 for x in times if any(ev.t_start - ROAM_SPIKE_S <= x <= ev.t_end + ROAM_SPIKE_S for ev in spikes))
        if near:
            c.support("network_path", W_STEADY, f"pings spiked or were lost within {ROAM_SPIKE_S:.0f} s of {near} of "
                                                "those switches")
    fixes = [_advice("Stay close to one access point", "With a mesh or several access points, the PC hops between "
                                                       "them when the signal is borderline."),
             _advice("Use a network cable")]
    return c.conclude("Wi-Fi switched access points", "Each switch of access point briefly interrupts the "
                                                      "connection, which shows as a lag spike.", fixes)


def _peaks(rows: list[tuple[float, float | None]], times: list[float], before: float,
           after: float) -> list[float | None]:
    """The highest value of rows within [t - before, t + after] for each t."""
    ts = [t for t, _ in rows]
    out = []
    for x in times:
        lo, hi = bisect.bisect_left(ts, x - before), bisect.bisect_right(ts, x + after)
        vals = [v for _, v in rows[lo:hi] if v is not None]
        out.append(max(vals) if vals else None)
    return out


def _wifi_scan_spikes(cap: Capture, ctx: dict) -> Any:
    c = _Case(cap, ctx, "wifi_scan_spikes")
    e = c.env
    done = _not_wifi(c)
    if done is not None:
        return done
    scans_raw = e.flows().get("wifi_scans")
    if not isinstance(scans_raw, list):
        c.lack("network_path", W_DIRECT, "Wi-Fi scan notifications were not recorded")
        return c.conclude()
    scans = sorted(float(x) for x in scans_raw if fa._bounded(x) is not None)
    if not scans:
        dur = f"{e.duration:.0f} s " if e.duration else ""
        return Unchecked("wifi_scan_spikes", f"no Wi-Fi background scan happened during the {dur}capture (they come "
                                             "about once a minute)")
    gw = e.net(N.GW_RTT_MS)

    def near_scan(t: float) -> bool:
        i = bisect.bisect_right(scans, t + SCAN_BEFORE_S) - 1      # the last scan that starts a window holding t
        return i >= 0 and scans[i] - SCAN_BEFORE_S <= t <= scans[i] + SCAN_AFTER_S

    quiet = [v for t, v in gw if v is not None and not near_scan(t)]
    if len(quiet) < 10:
        c.lack("network_path", W_DIRECT, "too few router pings outside the scans to compare with")
        return c.conclude()
    base = st.median(quiet)
    peaks = _peaks(gw, scans, SCAN_BEFORE_S, SCAN_AFTER_S)
    hits = [p for p in peaks if p is not None and p >= max(2.0 * base, base + SCAN_SPIKE_MS)]
    if len(hits) >= SCAN_ALIGNED_MIN and len(hits) / len(scans) >= 0.5:
        c.support("network_path", W_DIRECT, f"pings to the router spiked around {len(hits)} of {len(scans)} Wi-Fi "
                                            f"background scans (up to {max(hits):.0f} ms against {base:.0f} ms "
                                            f"normally; {SCAN_ALIGNED_MIN} or more)", len(hits), "", SCAN_ALIGNED_MIN)
    elif hits:
        c.support("network_path", W_STATIC, f"pings to the router spiked around {len(hits)} of {len(scans)} Wi-Fi "
                                            f"background scans (up to {max(hits):.0f} ms)", len(hits), "", SCAN_ALIGNED_MIN)
    else:
        c.against("network_path", W_STEADY, f"no ping spike around any of the {len(scans)} Wi-Fi background scans",
                  0, "", SCAN_ALIGNED_MIN)
    fixes = [_advice("Turn off Wi-Fi background scanning while gaming", "Some Wi-Fi tools call it a media streaming "
                                                                        "or gaming mode; Windows turns scanning back "
                                                                        "on when Wi-Fi reconnects."),
             _advice("Use a network cable")]
    return c.conclude("Ping spikes during Wi-Fi background scans", "About once a minute Windows has the Wi-Fi "
                                                                  "adapter look for other networks, and the "
                                                                  "connection stalls while it does.", fixes)


def _bufferbloat(cap: Capture, ctx: dict) -> Any:
    c = _Case(cap, ctx, "bufferbloat")
    e = c.env
    t = fa._col(cap.network, N.T)
    names = (N.NIC_RX_BPS, N.NIC_TX_BPS, N.INET_RTT_MS, N.GW_RTT_MS)
    cols = [fa._col(cap.network, n) for n in names]
    rows = []
    for i, ti in enumerate(t):
        if fa._bounded(ti) is None:
            continue
        rx, tx, inet, gw = (attribute.read(n, fa._at(col, i)) for n, col in zip(names, cols))
        rows.append((rx, tx, inet if inet is not None and inet > 0 else None, gw if gw is not None and gw > 0 else None))
    rx_peak = max((r[0] for r in rows if r[0] is not None), default=None)
    tx_peak = max((r[1] for r in rows if r[1] is not None), default=None)
    if rx_peak is None and tx_peak is None:
        c.lack("network_path", W_EVENT_STRONG, "the network adapter's traffic counters were not read")
        return c.conclude()
    peak = max(rx_peak or 0.0, tx_peak or 0.0)
    if peak < BLOAT_MIN_BPS:
        return Unchecked("bufferbloat", f"the connection was never busy during the capture (peak "
                                        f"{8 * peak / 1e6:.1f} Mbit/s), so ping under load could not be measured")

    def util(r: tuple) -> float:
        return max((r[0] or 0.0) / rx_peak if rx_peak else 0.0, (r[1] or 0.0) / tx_peak if tx_peak else 0.0)

    loaded = [r for r in rows if util(r) >= BLOAT_UTIL]
    idle = [r for r in rows if util(r) < BLOAT_IDLE_UTIL]
    li, ii = [r[2] for r in loaded if r[2] is not None], [r[2] for r in idle if r[2] is not None]
    if len(li) < BLOAT_MIN_ROWS or len(ii) < BLOAT_MIN_ROWS:
        return Unchecked("bufferbloat", "the connection was busy too briefly to compare ping under load with idle")
    base, busy = min(ii), st.median(li)
    rise = busy - base
    lg, ig = [r[3] for r in loaded if r[3] is not None], [r[3] for r in idle if r[3] is not None]
    gw_rise = st.median(lg) - min(ig) if lg and ig else None
    mbps = 8 * peak / 1e6
    if rise >= BLOAT_RISE_MS and (gw_rise is None or gw_rise < GW_LOCAL_MS):
        c.support("network_path", W_EVENT_STRONG, f"while the connection was busy (up to {mbps:.0f} Mbit/s), ping to "
                                                  f"the internet rose {rise:.0f} ms over its idle {base:.0f} ms "
                                                  f"({BLOAT_RISE_MS:.0f} ms or more)"
                  + (f", while the router stayed within {gw_rise:.0f} ms" if gw_rise is not None else ""),
                  rise, "ms", BLOAT_RISE_MS)
    elif rise >= BLOAT_RISE_MS:
        c.neutral("network_path", f"ping rose {rise:.0f} ms under load, but so did the router's ({gw_rise:.0f} ms): "
                                  "the queue is on your own network (Wi-Fi), not the internet link")
    else:
        c.against("network_path", W_STEADY, f"while the connection was busy (up to {mbps:.0f} Mbit/s), ping rose only "
                                            f"{rise:.0f} ms", rise, "ms", BLOAT_RISE_MS)
    fixes = [_advice("Pause or limit the download or upload while playing"),
             _advice("Turn on smart queue management (SQM) or QoS on the router", "It keeps game packets from "
                                                                                    "waiting behind downloads.")]
    return c.conclude("Ping rises whenever the connection is busy (bufferbloat)",
                      "The router queues too much data when the line is full, so game packets wait behind downloads.",
                      fixes)


def _isp_latency(cap: Capture, ctx: dict) -> Any:
    c = _Case(cap, ctx, "isp_latency")
    e = c.env
    inet, gw = e.net_values(N.INET_RTT_MS), e.net_values(N.GW_RTT_MS)
    targets = _dict(e.flows().get("targets"))
    anchor = _text(targets.get("internet"), 60) or "the internet"
    if len(inet) < 10:
        c.lack("network_path", W_STEADY, "too few pings to the internet were answered")
        return c.conclude()
    m = st.median(inet)
    g = st.median(gw) if gw else None
    if m >= INET_NOTICEABLE_MS and (g is None or g < GW_LOCAL_MS):
        c.support("network_path", W_STEADY, f"ping to {anchor} was {m:.0f} ms (median; {INET_NOTICEABLE_MS:.0f}-"
                                            f"{INET_BAD_MS:.0f} ms is noticeable, over {INET_BAD_MS:.0f} bad)"
                  + (f" while your router answered in {g:.0f} ms" if g is not None else ""), m, "ms", INET_NOTICEABLE_MS)
    elif m >= INET_NOTICEABLE_MS:
        c.neutral("network_path", f"ping to {anchor} was {m:.0f} ms, but your router itself took {g:.0f} ms: the delay "
                                  "starts on your own network")
    else:
        c.against("network_path", W_STEADY, f"ping to {anchor} was {m:.0f} ms (median; under "
                                            f"{INET_NOTICEABLE_MS:.0f} ms is good)", m, "ms", INET_NOTICEABLE_MS)
    fixes = [_advice("Check at another time of day", "If evenings are worse, the provider's network is busy."),
             _advice("Contact your internet provider if it is always high")]
    return c.conclude(f"High ping to the internet ({m:.0f} ms)", "Every packet takes long to reach the internet, "
                                                                 "before it even gets to the game server.", fixes)


def _server_lag(cap: Capture, ctx: dict) -> Any:
    c = _Case(cap, ctx, "server_lag")
    e = c.env
    server = e.net_values(N.SERVER_RTT_MS)
    flows_ok = e.measured("net_flows")
    if not flows_ok and not server:
        return Unchecked("server_lag", "needs the game's own network traffic (ETW rights) or a game server that "
                                       f"answers pings: {e.why('net_flows')}")
    flow = e.flows().get("server")
    flow = flow if isinstance(flow, dict) else None
    if flows_ok and flow is None and not server:
        # CC-1: the tracker follows UDP only, so a TCP-only or offline game never has a server flow to judge
        return Unchecked("server_lag", "no game-server UDP traffic was seen: a TCP-only or offline game?")
    inet = e.net_values(N.INET_RTT_MS)
    inet_med = st.median(inet) if inet else None
    inet_spikes = [ev for ev in e.events if ev.kind == "net_spike" and _dict(ev.detail).get("target") == "internet"]
    inet_lost = [ev for ev in e.events if ev.kind == "net_loss" and _dict(ev.detail).get("target") == "internet"]
    gaps = [ev for ev in e.events if ev.kind == "net_gap" and _dict(ev.detail).get("kind") in SERVER_GAP_KINDS]
    local = [ev for ev in e.events if ev.kind == "net_gap" and _dict(ev.detail).get("kind") == "local"]
    # CC-1: "unknown" (late packets hid whether our own sending paused too) is never read as local, nor as the server
    unsure = [ev for ev in e.events if ev.kind == "net_gap" and _dict(ev.detail).get("kind") not in
              SERVER_GAP_KINDS + ("local", "open")]
    lost = [ev for ev in e.events if ev.kind == "net_gap" and _dict(ev.detail).get("kind") == "open"]
    flat = [g for g in gaps if not any(s.t_start <= g.t_end and g.t_start <= s.t_end for s in inet_spikes)]
    gone = [g for g in lost if not any(s.t_start <= g.t_end and g.t_start <= s.t_end for s in inet_lost)]
    ids = [g.id for g in flat + gone]
    if flows_ok and flow is None:
        c.neutral("network_path", "no game-server UDP traffic was seen in the game's own traffic (a TCP-only game?)")
    elif flows_ok:
        if len(flat) >= SERVER_GAPS_MIN:
            c.support("network_path", W_STEADY, f"the game server went quiet {len(flat)} times (longest "
                                                f"{max(g.severity for g in flat):.0f} ms) while the internet answered "
                                                "normally", len(flat), "", SERVER_GAPS_MIN)
        elif flat:
            c.support("network_path", W_STATIC, f"the game server went quiet {len(flat)} time"
                                                f"{'s' if len(flat) != 1 else ''} (longest "
                                                f"{max(g.severity for g in flat):.0f} ms)", len(flat), "", SERVER_GAPS_MIN)
        if gone:       # CC-1: an "open" gap is the server falling silent for good while the game kept sending to it
            c.support("network_path", W_STEADY, f"the game server stopped answering for good at {gone[0].t_start:.0f} s "
                                                "while the game kept sending to it and the internet answered: the "
                                                "connection to the server was lost")
        elif lost:
            c.neutral("network_path", f"the game server stopped answering for good at {lost[0].t_start:.0f} s, but "
                                      "pings to the internet were lost too: the PC's own connection dropped")
        if unsure:
            c.neutral("network_path", f"{len(unsure)} gap{'s' if len(unsure) != 1 else ''} in the server's packets "
                                      "could not be told apart: late packets hid whether the game or the network "
                                      "paused")
        if not (gaps or lost or unsure):
            _server_kept_arriving(c, flow, len(local))
        if local:
            c.neutral("network_path", f"{len(local)} gap{'s' if len(local) != 1 else ''} came from the game itself "
                                      "pausing its own sending, not the network")
        if _dict(e.flows()).get("layout") == "unverified":
            c.neutral("network_path", "which address is the server is not verified on this PC yet")   # NE 4.4
    if server:
        m = st.median(server)
        if m > INET_BAD_MS and (inet_med is None or inet_med < INET_NOTICEABLE_MS):
            c.support("network_path", W_STEADY, f"ping to the game server was {m:.0f} ms (median)"
                      + (f" against {inet_med:.0f} ms to the internet" if inet_med is not None else ""),
                      m, "ms", INET_BAD_MS)
        elif m <= INET_NOTICEABLE_MS:
            c.against("network_path", W_STEADY, f"ping to the game server was {m:.0f} ms (median)", m, "ms",
                      INET_NOTICEABLE_MS)
        else:
            c.neutral("network_path", f"ping to the game server was {m:.0f} ms (median)"
                      + (f" against {inet_med:.0f} ms to the internet" if inet_med is not None else ""),
                      m, "ms", INET_BAD_MS)
    fixes = [_advice("Pick a closer server or region in the game", "Nothing on this PC can fix the server's own lag."),
             _advice("Check the game's server status page")]
    return c.conclude("The game server or its route lags", "Your own connection answered normally while the game "
                                                           "server's packets were late or missing.", fixes, ids)


def _server_kept_arriving(c: _Case, flow: dict, local: int) -> None:
    """CC-1: "the server's packets kept arriving" only over the span they were seen: from the flow's first to
    its last packet, never past the moment the ETW consumer stopped by itself, and only when that is most of the
    capture. `local`: the gaps of GAP_MIN_MS or more in which the game's own sending paused too (NE 4.6), which
    the line then names as the only ones."""
    e = c.env
    first, last = fa._bounded(flow.get("first_rx_t")), fa._bounded(flow.get("last_rx_t"))
    stopped = fa._bounded(_dict(e.flows().get("health")).get("consumer_stopped_t"))
    if first is None or last is None or e.duration is None:
        c.neutral("network_path", "when the game server's packets arrived was not recorded, so no gap can be ruled out")
        return
    end = min(last, stopped) if stopped is not None else last
    share = max(0.0, min(1.0, (end - first) / e.duration))
    until = ", when the ETW consumer stopped" if stopped is not None and stopped <= last else ""
    gaps = (f"the only gap{'s' if local != 1 else ''} of {ev_mod.GAP_MIN_MS:.0f} ms or more came from the game "
            "pausing its own sending" if local else f"no gap of {ev_mod.GAP_MIN_MS:.0f} ms or more")
    if share >= SERVER_COVER:
        c.against("network_path", W_STEADY, f"the game server's packets kept arriving from {first:.0f} s to "
                                            f"{end:.0f} s{until} ({100 * share:.0f}% of the capture; {gaps})",
                  100 * share, "%", 100 * SERVER_COVER)
    else:
        c.neutral("network_path", f"the game server's packets were seen only from {first:.0f} s to {end:.0f} s{until} "
                                  f"({100 * share:.0f}% of the capture), so gaps outside that were not measured",
                  100 * share, "%", 100 * SERVER_COVER)


def _packet_loss(cap: Capture, ctx: dict) -> Any:
    c = _Case(cap, ctx, "packet_loss")
    e = c.env
    losses = [ev for ev in e.events if ev.kind == "net_loss"]
    far = [ev for ev in losses if _dict(ev.detail).get("target") in ("internet", "server")]
    near = [ev for ev in losses if _dict(ev.detail).get("target") == "gateway"]
    if far:
        worst = max(far, key=lambda ev: ev.severity)
        d = _dict(worst.detail)
        c.support("network_path", W_STEADY, f"{d.get('lost_15s', 0)} of {d.get('sent_15s', 0)} pings to the "
                                            f"{d.get('target')} were lost within 15 s ({worst.severity:.1f}%; "
                                            f"{ev_mod.LOSS_PCT:.0f}% or more is bad)" + (", in a burst" if d.get("burst")
                                                                                        else ""),
                  worst.severity, "%", ev_mod.LOSS_PCT)
    else:
        inet = [v for _, v in e.net(N.INET_RTT_MS)]
        if len(inet) < 10 and not near:
            c.lack("network_path", W_STEADY, "too few pings to the internet were sent")
            return c.conclude()
        _steady_loss(c, against=not near)
    if near and not far:
        c.neutral("network_path", "pings to your router were lost while the internet answered: routers often drop "
                                  "pings to themselves")   # NE 7 row 9
    wifi = e.on_wifi()
    fixes = ([_advice("On Wi-Fi: move closer to the router or use a cable")] if wifi else []) + \
        [_advice("Check the network cable and the router", "Restart the router; contact the provider if it keeps "
                                                           "happening.")]
    return c.conclude("Packet loss", "Some packets never arrived, so the game had to guess or wait, which shows as "
                                     "rubber-banding.", fixes, [ev.id for ev in far])


def _steady_loss(c: _Case, against: bool) -> None:
    """The internet probes lost over the whole capture (events.probe_loss, the loss_pct figure), where no 15 s
    window held a loss event: NE 2.2 rates >= 1 % of a stream bad, so steady low-rate loss is support, from at
    least LOSS_MIN_PROBES lost probes (one unanswered ping is no outage, NE 2.4). `against`: whether no or little
    loss may speak against the cause (not while the router's own pings were lost)."""
    counts = ev_mod.probe_loss(c.env.cap, "internet")
    rows = c.env.net(N.INET_RTT_MS)
    if counts is None and rows and all(v is not None for _, v in rows):
        counts = (0.0, float(len(rows)))       # no probe table, but every round was answered
    if counts is None:
        if against:
            c.lack("network_path", W_STEADY, "how many pings to the internet were lost was not recorded")
        return
    lost, sent = counts
    pct = 100.0 * lost / sent
    if lost >= ev_mod.LOSS_MIN_PROBES and pct >= ev_mod.LOSS_PCT:
        c.support("network_path", W_STEADY, f"{pct:.1f}% of pings to the internet were lost over the capture "
                                            f"({lost:.0f} of {sent:.0f}; {ev_mod.LOSS_PCT:.0f}% or more is bad)",
                  pct, "%", ev_mod.LOSS_PCT)
    elif not against:
        return
    elif lost == 0:
        c.against("network_path", W_STEADY, f"no ping to the internet was lost (0 of {sent:.0f})", 0.0, "%",
                  ev_mod.LOSS_PCT)
    elif pct < ev_mod.LOSS_PCT:
        c.against("network_path", W_STEADY, f"only {lost:.0f} of {sent:.0f} pings to the internet "
                                            f"{'was' if lost == 1 else 'were'} lost ({pct:.3g}% over the capture; "
                                            f"under {ev_mod.LOSS_PCT:.0f}%)", pct, "%", ev_mod.LOSS_PCT)
    else:                      # too few lost probes to count, however large a share of a short capture
        c.neutral("network_path", f"{lost:.0f} of {sent:.0f} pings to the internet {'was' if lost == 1 else 'were'} "
                                  f"lost ({pct:.1f}%), too few to be an outage", pct, "%", ev_mod.LOSS_PCT)   # NE 2.4


def _vpn_route(cap: Capture, ctx: dict) -> Any:
    c = _Case(cap, ctx, "vpn_route")
    e = c.env
    route = e.flows().get("route")
    if not isinstance(route, dict) or route.get("vpn") not in (True, False):
        c.lack("static", W_DIRECT, "the network route was not read")
        return c.conclude()
    desc = _text(route.get("description"), 80) or "the network adapter"
    if route["vpn"] is False:
        c.against("static", W_DIRECT, f"internet traffic leaves through \"{desc}\" directly, not a VPN")
        return c.conclude()
    c.support("static", W_DIRECT, f"internet traffic leaves through a VPN or tunnel (\"{desc}\")")
    inet = e.net_values(N.INET_RTT_MS)
    if len(inet) >= 10 and st.median(inet) >= INET_NOTICEABLE_MS:
        c.support("network_path", W_STEADY, f"and ping to the internet was {st.median(inet):.0f} ms (median)",
                  st.median(inet), "ms", INET_NOTICEABLE_MS)
    fixes = [_advice("Leave the game out of the VPN (split tunnelling), or disconnect the VPN while playing")]
    return c.conclude("Traffic goes through a VPN", "A VPN adds a detour and a hop to every packet the game sends.",
                      fixes)


def _bandwidth_hog(cap: Capture, ctx: dict) -> Any:
    c = _Case(cap, ctx, "bandwidth_hog")
    e = c.env
    flows = e.flows()
    downloads = flows.get("downloads")          # None: no download log was read (DESIGN network)
    talkers = flows.get("talkers")              # None: the ETW session never ran
    dl = []
    for d in downloads if isinstance(downloads, list) else []:
        mbps, t = (fa._bounded(d.get("mbps")), fa._bounded(d.get("t"))) if isinstance(d, dict) else (None, None)
        if mbps is not None and mbps > 0 and t is not None:
            dl.append((float(t), _text(d.get("source"), 40) or "a download", float(mbps)))
    if dl:
        _, src, rate = max(dl, key=lambda x: x[2])
        c.support("background", W_STEADY, f"{src} was downloading at up to {rate:.0f} Mbit/s during the capture")
        inet = e.net(N.INET_RTT_MS)
        times = [t for t, _, _ in dl]
        during = [v for t, v in inet if v is not None and any(abs(t - x) <= DOWNLOAD_NEAR_S for x in times)]
        other = [v for t, v in inet if v is not None and not any(abs(t - x) <= DOWNLOAD_NEAR_S for x in times)]
        if len(during) >= BLOAT_MIN_ROWS and len(other) >= BLOAT_MIN_ROWS and \
                st.median(during) - st.median(other) >= BLOAT_RISE_MS:
            c.support("network_path", W_STEADY, f"and ping to the internet rose to {st.median(during):.0f} ms while it "
                                                f"ran, from {st.median(other):.0f} ms")
    game = {p for p in e.target.get("pids") or [] if isinstance(p, int)}
    heavy = []
    for tk in talkers if isinstance(talkers, list) else []:
        if not isinstance(tk, dict) or tk.get("pid") in game or tk.get("pid") == e.target.get("pid"):
            continue
        moved = (_fin(tk.get("rx_bytes")) or 0.0) + (_fin(tk.get("tx_bytes")) or 0.0)
        if e.duration and moved / e.duration >= TALKER_MIN_BPS:
            heavy.append((moved, _text(tk.get("name"), 60) or f"process {tk.get('pid')}"))
    for moved, name in sorted(heavy, reverse=True)[:2]:
        c.support("background", W_STEADY, f"{name} moved {moved / 1e6:,.0f} MB ({8 * moved / e.duration / 1e6:.0f} "
                                          "Mbit/s on average)")
    if not dl and not heavy:
        # Only a source that sees every program's traffic can rule a hog out; a download log sees one launcher
        clear = _all_traffic_quiet(e, talkers)
        if isinstance(clear, Unchecked):
            return clear
        c.against("background", W_STEADY, clear)
    fixes = [_advice("Pause the download while you play", "Steam: Settings > Downloads > \"Allow downloads during "
                                                          "gameplay\" off."),
             _advice("Limit Windows Update's background bandwidth", "Settings > Windows Update > Advanced options > "
                                                                    "Delivery Optimization.")]
    return c.conclude("A download is using your bandwidth", "Another program's download or upload filled the "
                                                            "connection while you played.", fixes)


def _all_traffic_quiet(e: _Env, talkers: Any) -> str | Unchecked:
    """unmeasured-downloads-cleared: what rules a bandwidth hog out, saying which source saw it: the ETW per-program
    traffic, when its session ran for the whole capture and saw every program, else the network adapter's own byte
    counters (every program together) under TALKER_MIN_BPS. Unchecked when neither measured it. Through the relay the
    per-program traffic covers only the player's Windows session (`talkers_scope` "session", DECISIONS 2026-11-08),
    and services run outside it, so only the adapter can rule them out: what is left of its total once the game's own
    traffic over the network (`game_wire_bytes`, measured from the game's whole events) is taken off."""
    flows = e.flows()
    src = flows.get("download_sources")       # the download logs the network collector read
    logs = [s.capitalize() for s in (_text(x, 20) for x in src) if s] if isinstance(src, list) else []
    seen = f"; {' and '.join(logs)}'s download log showed no download" if logs else ""
    stopped = fa._bounded(_dict(flows.get("health")).get("consumer_stopped_t"))
    measured = isinstance(talkers, list) and e.measured("net_flows") and stopped is None
    session_only = flows.get("talkers_scope") == "session"      # older captures have no scope: they saw every program
    if measured and not session_only:
        return (f"no other program moved {TALKER_MIN_BPS / 1e6:.0f} MB/s or more on average (each program's traffic, "
                f"from ETW){seen}")
    t = fa._col(e.cap.network, N.T)
    rx, tx = fa._col(e.cap.network, N.NIC_RX_BPS), fa._col(e.cap.network, N.NIC_TX_BPS)
    nic = [a + b for a, b in ((attribute.read(N.NIC_RX_BPS, fa._at(rx, i)), attribute.read(N.NIC_TX_BPS, fa._at(tx, i)))
                              for i, ti in enumerate(t) if fa._bounded(ti) is not None)
           if a is not None and b is not None and a >= 0 and b >= 0]
    wire = flows.get("game_wire_bytes")
    game = 0.0
    if measured and e.duration and isinstance(wire, list) and len(wire) == 2:    # session only: else returned
        game = sum(max(0.0, _fin(x) or 0.0) for x in wire) / e.duration
    nic_mean = st.mean(nic) if nic else 0.0
    rest = max(0.0, nic_mean - game)
    if len(nic) >= NIC_MIN_ROWS and rest < TALKER_MIN_BPS:
        mine = (f"no program in your Windows session moved {TALKER_MIN_BPS / 1e6:.0f} MB/s or more on average (each "
                "one's traffic, from ETW), and " if measured else "")
        if game > 0 and nic_mean >= TALKER_MIN_BPS:
            return (f"{mine}the network adapter moved {nic_mean / 1e6:.2f} MB/s on average, every program together, "
                    f"{game / 1e6:.2f} MB/s of it the game's own: {rest / 1e6:.2f} MB/s for everything else (under "
                    f"{TALKER_MIN_BPS / 1e6:.0f} MB/s){seen}")
        return (f"{mine}the network adapter moved only {nic_mean / 1e6:.2f} MB/s on average, every program "
                f"together (under {TALKER_MIN_BPS / 1e6:.0f} MB/s){seen}")
    if measured:
        why = "programs outside your Windows session, Windows' own services among them, were not measured one by one"
    else:
        why = ("each program's traffic was not measured (" + (f"the ETW consumer stopped at {stopped:.0f} s"
                                                               if stopped is not None and isinstance(talkers, list)
                                                               else e.why("net_flows")) + ")")
    if len(nic) >= NIC_MIN_ROWS and game > 0:
        why += (f", and the network adapter moved {nic_mean / 1e6:.1f} MB/s on average, {game / 1e6:.1f} MB/s of it "
                "the game's own, so something else used the connection")
    elif len(nic) >= NIC_MIN_ROWS:
        why += (f", and the network adapter moved {nic_mean / 1e6:.1f} MB/s on average, so something used the "
                "connection")
    else:
        why += ", and the network adapter's byte counters were not read"
    return Unchecked("bandwidth_hog", why + seen)


# ----------------------------------------------------------------------------- the rule list ---

RULES: list[Rule] = [
    Rule("wrong_gpu", "gpu_engine", (), None, _wrong_gpu, "fps"),
    Rule("hybrid_copy", "static", (), "gpu", _hybrid_copy, "fps"),
    Rule("gpu_limit", "frame_timing", ("frames|system",), "gpu", _gpu_limit, "fps", alt=("gpu_engine",)),
    Rule("cpu_limit", "cpu_threads", ("frames", "system"), "cpu", _cpu_limit, "fps"),
    Rule("gpu_contention", "gpu_engine", ("system",), None, _gpu_contention, "fps"),
    Rule("gpu_throttle", "gpu_telemetry", ("gpu",), "gpu", _gpu_throttle, "fps"),
    Rule("cpu_throttle", "cpu_limits", ("system",), "cpu", _cpu_throttle, "fps"),
    Rule("vram_overflow", "gpu_engine", ("system",), "gpu", _vram_overflow, "stutter"),
    Rule("ram_pressure", "memory", ("system",), None, _ram_pressure, "stutter"),
    Rule("power_plan", "static", (), None, _power_plan, "setup"),
    Rule("refresh_rate", "static", (), None, _refresh_rate, "setup"),
    Rule("vsync_judder", "frame_timing", ("frames",), None, _vsync_judder, "pacing"),
    Rule("dropped_frames", "frame_timing", ("frames",), None, _dropped_frames, "pacing"),
    Rule("uneven_pacing", "frame_timing", ("frames",), None, _uneven_pacing, "pacing"),
    Rule("frame_cap", "frame_timing", ("frames",), None, _frame_cap, "setup"),
    Rule("windowed_composition", "frame_timing", (), None, _windowed_composition, "latency"),
    Rule("game_dvr", "static", (), None, _game_dvr, "setup"),
    Rule("game_mode_off", "static", (), None, _game_mode_off, "setup"),
    Rule("not_focused", "foreground", ("system",), None, _not_focused, "fps"),
    Rule("background_hogs", "background", ("frames", "system"), None, _background_hogs, "stutter"),
    Rule("hdd_streaming", "static", (), None, _hdd_streaming, "stutter", alt=("disk",)),
    Rule("shader_compile", "frame_timing", ("frames",), None, _shader_compile, "stutter"),
    Rule("game_cpu_spike", "cpu_threads", (), None, _stutter_rule("game_cpu_spike"), "stutter"),
    Rule("asset_streaming", "disk", (), None, _stutter_rule("asset_streaming"), "stutter"),
    Rule("driver_dpc", "interrupts", (), None, _stutter_rule("driver_dpc"), "stutter"),
    Rule("gpu_spike", "frame_timing", (), "gpu_event", _stutter_rule("gpu_spike"), "stutter"),
    Rule("game_stall", "frame_timing", (), None, _stutter_rule("game_stall"), "stutter"),
    Rule("wifi_weak", "wifi", ("network",), None, _wifi_weak, "network"),
    Rule("wifi_roaming", "wifi", ("network",), None, _wifi_roaming, "network"),
    Rule("wifi_scan_spikes", "network_path", ("network",), None, _wifi_scan_spikes, "network"),
    Rule("bufferbloat", "network_path", ("network",), None, _bufferbloat, "network"),
    Rule("isp_latency", "network_path", ("network",), None, _isp_latency, "network"),
    Rule("server_lag", "network_path", ("network|net_flows",), None, _server_lag, "network"),
    Rule("packet_loss", "network_path", ("network",), None, _packet_loss, "network"),
    Rule("vpn_route", "static", ("network",), None, _vpn_route, "network", alt=("network_path",)),
    Rule("bandwidth_hog", "background", ("network",), None, _bandwidth_hog, "network"),
]
RULE_BY_CAUSE: dict[str, Rule] = {r.cause: r for r in RULES}
