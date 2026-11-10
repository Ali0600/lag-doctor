"""What each run of a settings sweep measured, the checks that keep a run out of the ranking, and the ranking.

A run is a recording, and its measured part is the window from the start key: `window_s` seconds from the press, cut
out of the main swap chain (analysis.window) and judged by the report's own frame statistics (analysis/frames.py):
average FPS and the 1 % low. A run counts only when that window was recorded whole, with frame timing on, the game in
front and no other program on the game's GPU, and when what a sweep holds fixed (the power plan and its mode, mains
power, the refresh rate, the GPU's power limit) matches the first good run (docs/research/settings-sweeps.md, section 7).

Settings are ranked on average FPS and compared with A (the file as it was) by Welch's t interval, which holds at 2 to
10 runs where a bootstrap's interval is too narrow (research note, section 4). An interval that holds 0 is "no
measurable difference", never a winner. The 1 % low gets the same comparison; picture quality is the player's to judge.
"""
from __future__ import annotations

import math
import statistics
from typing import Any, Sequence

from lagdoctor.analysis import frames as fa
from lagdoctor.analysis.window import window
from lagdoctor.model import F, G, S, Capture
from lagdoctor.sweep import plan

COVER_MIN = 0.9          # the share of the window the game's frames must span: a game that froze or hid is not measured
FOCUS_MIN = 0.95         # the share of the window's samples with the game in front (Windows slows a game behind others)
OTHER_GPU_MAX = fa.SHARED_GPU_PCT      # another program at 10 % or more of the game's GPU shares it (the report's rule)
POWER_LIMIT_TOL = 0.10   # a GPU power limit this much off the first run's: the laptop's power mode changed
REFRESH_TOL_HZ = 1.0
CONFIDENCE = 0.95
DF_MAX = 1000.0          # Welch's degrees of freedom past this make no difference to the interval (t = 1.962)
FRAMES_OK = ("ok", "partial")


def _finite(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return float(v)


def _median(values: Sequence[Any]) -> float | None:
    clean = [x for x in (_finite(v) for v in values) if x is not None]
    return float(statistics.median(clean)) if clean else None


def _round(v: Any, digits: int = 3) -> float | None:
    x = _finite(v)
    return None if x is None else round(x, digits)


# ------------------------------------------------------------- one run --

def window_metrics(cap: Capture, t0: float, window_s: float) -> dict[str, Any]:
    """The numbers of the window [t0, t0 + window_s] (the recording's seconds): the frame statistics of the main swap
    chain, how much of the window its frames span, the share of samples with the game in front, and the GPU's medians."""
    t0, window_s = float(t0), float(window_s)
    cut = window(cap, t0, t0 + window_s, frames=fa.main_chain(cap))
    stats = fa.frame_stats(cut["frames"], window_s)
    times = [x for x in (_finite(t) for t in cut["frames"].get(F.T) or []) if x is not None]
    focus = [v for v in cut["system"].get(S.GAME_FOCUSED) or [] if v in (0, 1) and not isinstance(v, bool)]
    gpu = cut["gpu"]
    throttle = [x for x in (_finite(v) for v in gpu.get(G.THROTTLE_PCT) or []) if x is not None]
    return {
        "frames": stats.get("frames") or 0,
        "avg_fps": _round(stats.get("avg_fps")),
        "low1_fps": _round(stats.get("low1_fps")),
        "p1_fps": _round(stats.get("p1_fps")),
        "worst_frame_ms": _round(stats.get("worst_frame_ms")),
        "hitches": stats.get("hitches"),
        "covered_s": round(max(times) - min(times), 3) if len(times) >= 2 else 0.0,
        "focused": round(sum(focus) / len(focus), 4) if focus else None,
        "gpu_other_pct": _round(_median(cut["system"].get(S.GPU_OTHER_PCT) or [])),
        "gpu_clock_mhz": _round(_median(gpu.get(G.CLOCK_MHZ) or []), 1),
        "gpu_temp_c": _round(_median(gpu.get(G.TEMP_C) or []), 1),
        "gpu_power_limit_w": _round(_median(gpu.get(G.POWER_LIMIT_W) or []), 1),
        "gpu_throttle_pct": _round(math.fsum(throttle) / len(throttle), 1) if throttle else None,
    }


def problems(cap: Capture, metrics: dict[str, Any], start_t: float, window_s: float) -> list[str]:
    """Why a run cannot count, in plain words; empty when it can."""
    out = []
    frames_src = cap.sources.get("frames") if isinstance(cap.sources, dict) else None
    state = getattr(frames_src, "state", None)
    if state not in FRAMES_OK:
        why = getattr(frames_src, "detail", "") or "it did not run"
        out.append(f"frame timing was not recorded ({why})")
        return out
    duration = _finite(cap.duration_s) or 0.0
    if duration < start_t + window_s - 1.0:
        out.append(f"the recording ended {max(0.0, duration - start_t):.0f} s into the {window_s:.0f} s window")
    n = metrics.get("frames") or 0
    if n < fa.MIN_FRAMES_1PCT:
        out.append(f"the window holds only {n:,} frames, too few for a 1 % low ({fa.MIN_FRAMES_1PCT:,} at least)")
    covered = _finite(metrics.get("covered_s")) or 0.0
    if covered < COVER_MIN * window_s:
        out.append(f"the game drew frames for only {covered:.0f} s of the {window_s:.0f} s window")
    focused = _finite(metrics.get("focused"))
    if focused is not None and focused < FOCUS_MIN:
        out.append(f"the game was behind another window for about {(1.0 - focused) * window_s:.0f} s of the window")
    other = _finite(metrics.get("gpu_other_pct"))
    if other is not None and other >= OTHER_GPU_MAX:
        out.append(f"another program used {other:.0f} % of the game's GPU during the window")
    return out


def conditions(cap: Capture, metrics: dict[str, Any]) -> dict[str, Any]:
    """What a sweep holds fixed from run to run, as this run's recording saw it."""
    inv = cap.inventory if isinstance(cap.inventory, dict) else {}
    power = inv.get("power") if isinstance(inv.get("power"), dict) else {}
    game = inv.get("game") if isinstance(inv.get("game"), dict) else {}

    def text(v: Any) -> str | None:
        return v[:120] if isinstance(v, str) else None

    battery = power.get("on_battery")
    exe_bytes = game.get("exe_bytes")
    return {
        "power_plan": text(power.get("scheme_guid")),
        "power_plan_name": text(power.get("scheme_name")),
        "power_mode": text(power.get("slider")),
        "on_battery": battery if isinstance(battery, bool) else None,
        "refresh_hz": _round(fa.refresh_hz(cap)),
        "gpu_power_limit_w": _round(metrics.get("gpu_power_limit_w"), 1),
        "exe_bytes": exe_bytes if isinstance(exe_bytes, int) and not isinstance(exe_bytes, bool) else None,
        "exe_modified": text(game.get("exe_modified")),
    }


def moved(reference: dict[str, Any] | None, now: dict[str, Any]) -> list[str]:
    """What changed since the first good run, in plain words (empty when nothing did, or for the first run). Mains
    power is required of every run, the first one too."""
    out = []
    if now.get("on_battery") is True:
        out.append("the PC was on battery, which lowers what it can run")
    if not reference:
        return out
    plan_was, plan_now = reference.get("power_plan"), now.get("power_plan")
    if plan_was and plan_now and plan_was.lower() != plan_now.lower():
        out.append(f"the power plan changed from {reference.get('power_plan_name') or plan_was} to "
                   f"{now.get('power_plan_name') or plan_now}")
    mode_was, mode_now = reference.get("power_mode"), now.get("power_mode")
    if mode_was and mode_now and mode_was != mode_now:
        out.append(f"Windows' power mode changed from {mode_was} to {mode_now}")
    hz_was, hz_now = _finite(reference.get("refresh_hz")), _finite(now.get("refresh_hz"))
    if hz_was is not None and hz_now is not None and abs(hz_was - hz_now) > REFRESH_TOL_HZ:
        out.append(f"the screen's refresh rate changed from {hz_was:.0f} Hz to {hz_now:.0f} Hz")
    w_was, w_now = _finite(reference.get("gpu_power_limit_w")), _finite(now.get("gpu_power_limit_w"))
    if w_was and w_now is not None and abs(w_now - w_was) > POWER_LIMIT_TOL * w_was:
        out.append(f"the GPU's power limit changed from {w_was:.0f} W to {w_now:.0f} W (did the laptop's own power "
                   "or fan mode change?)")
    return out


def game_changed(reference: dict[str, Any] | None, now: dict[str, Any]) -> str | None:
    """Why the game itself is no longer the one the sweep began with (an update between runs), or None."""
    if not reference:
        return None
    for key, what in (("exe_bytes", "size"), ("exe_modified", "date")):
        was, is_now = reference.get(key), now.get(key)
        if was is not None and is_now is not None and was != is_now:
            return f"the game's exe has another {what} than at the sweep's first good run, so the game was updated"
    return None


# ------------------------------------------------------- the statistics --

def _betacf(a: float, b: float, x: float) -> float:
    """The continued fraction of the incomplete beta function (modified Lentz)."""
    tiny, eps = 1e-300, 1e-15
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 2001):
        m2 = 2 * m
        for aa in (m * (b - m) * x / ((qam + m2) * (a + m2)), -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))):
            d = 1.0 + aa * d
            d = 1.0 / (d if abs(d) > tiny else tiny)
            c = 1.0 + aa / c
            c = c if abs(c) > tiny else tiny
            step = d * c
            h *= step
        if abs(step - 1.0) < eps:
            return h
    raise ArithmeticError("the incomplete beta function did not converge")


def betainc(a: float, b: float, x: float) -> float:
    """The regularized incomplete beta function I_x(a, b), for a, b > 0 and 0 <= x <= 1."""
    if not (a > 0 and b > 0 and 0.0 <= x <= 1.0):
        raise ValueError("betainc needs a, b > 0 and 0 <= x <= 1")
    if x in (0.0, 1.0):
        return x
    front = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def t_sf(t: float, df: float) -> float:
    """P(T > t) for Student's t with `df` degrees of freedom, t >= 0."""
    if t < 0:
        raise ValueError("t_sf takes t >= 0")
    return 0.5 * betainc(df / 2.0, 0.5, df / (df + t * t))


def t_quantile(p: float, df: float) -> float:
    """The t below which a share `p` of Student's t lies (0.5 < p < 1), `df` > 0 degrees of freedom (capped at
    DF_MAX, where it equals the normal's to two decimals)."""
    if not 0.5 < p < 1.0 or not df > 0 or not math.isfinite(p):
        raise ValueError("t_quantile needs 0.5 < p < 1 and df > 0")
    df = min(float(df), DF_MAX)
    tail = 1.0 - p
    lo, hi = 0.0, 1.0
    while t_sf(hi, df) > tail:
        hi *= 2.0
        if hi > 1e12:
            raise ArithmeticError("t_quantile did not find its bracket")
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if t_sf(mid, df) > tail:
            lo = mid
        else:
            hi = mid
        if hi - lo <= 1e-12 * max(1.0, hi):
            break
    return (lo + hi) / 2.0


def interval(values: Sequence[float]) -> dict[str, Any] | None:
    """{n, mean, sd, half, cv}: the mean and its 95 % t interval's half width (None for a single run), the standard
    deviation and its share of the mean."""
    xs = [x for x in (_finite(v) for v in values) if x is not None]
    if not xs:
        return None
    mean = math.fsum(xs) / len(xs)
    if len(xs) < 2:
        return {"n": 1, "mean": mean, "sd": None, "half": None, "cv": None}
    sd = statistics.stdev(xs)
    half = t_quantile((1 + CONFIDENCE) / 2, len(xs) - 1) * sd / math.sqrt(len(xs))
    return {"n": len(xs), "mean": mean, "sd": sd, "half": half, "cv": sd / mean if mean else None}


def welch(a: Sequence[float], b: Sequence[float]) -> dict[str, Any] | None:
    """Welch's 95 % interval for mean(a) - mean(b): {diff, lo, hi, df}; None unless both have 2 runs or more. With no
    spread at all the interval is the difference itself (df None)."""
    xa = [x for x in (_finite(v) for v in a) if x is not None]
    xb = [x for x in (_finite(v) for v in b) if x is not None]
    if len(xa) < 2 or len(xb) < 2:
        return None
    ma, mb = math.fsum(xa) / len(xa), math.fsum(xb) / len(xb)
    sa, sb = statistics.variance(xa) / len(xa), statistics.variance(xb) / len(xb)
    diff = ma - mb
    se2 = sa + sb
    if se2 <= 0:
        return {"diff": diff, "lo": diff, "hi": diff, "df": None}
    df = se2 * se2 / (sa * sa / (len(xa) - 1) + sb * sb / (len(xb) - 1))
    half = t_quantile((1 + CONFIDENCE) / 2, df) * math.sqrt(se2)
    return {"diff": diff, "lo": diff - half, "hi": diff + half, "df": df}


def compare(values: Sequence[float], base: Sequence[float]) -> dict[str, Any] | None:
    """A setting against A, in % of A's mean: {pct, lo, hi, verdict}, verdict "more", "less" or "same" (the interval
    holds 0). None unless both have 2 good runs or more."""
    w = welch(values, base)
    base_mean = interval(base)
    if w is None or base_mean is None or not base_mean["mean"]:
        return None
    m = base_mean["mean"]
    verdict = "more" if w["lo"] > 0 else "less" if w["hi"] < 0 else "same"
    return {"pct": 100.0 * w["diff"] / m, "lo": 100.0 * w["lo"] / m, "hi": 100.0 * w["hi"] / m, "verdict": verdict}


# ---------------------------------------------------------- the ranking --

def ranking(sweep: plan.Sweep) -> list[dict[str, Any]]:
    """One row per setting, by average FPS, highest first (a setting without good runs last): its counted good runs'
    average FPS and 1 % low with their intervals, and, for B onwards, both compared with A."""
    base = sweep.variants[0].id
    values = {v.id: ([r.metrics.get("avg_fps") for r in plan.counted_runs(sweep, v.id)],
                     [r.metrics.get("low1_fps") for r in plan.counted_runs(sweep, v.id)]) for v in sweep.variants}
    rows = []
    for v in sweep.variants:
        avg, low = values[v.id]
        row = {"variant": v.id, "label": v.label, "avg": interval(avg), "low1": interval(low), "vs": None}
        if v.id != base:
            row["vs"] = {"avg": compare(avg, values[base][0]), "low1": compare(low, values[base][1])}
        rows.append(row)
    rows.sort(key=lambda r: (r["avg"] is None, -(r["avg"] or {}).get("mean", 0.0)))
    return rows


def _num(v: float | None, digits: int = 1) -> str:
    return "-" if v is None else f"{v:.{digits}f}"


def _pm(row: dict[str, Any] | None, unit: str = "") -> str:
    if row is None:
        return "-"
    text = f"{row['mean']:.1f}{unit}"
    return text + (f" +/- {row['half']:.1f}" if row.get("half") is not None else " (1 run)")


def _verdict(c: dict[str, Any] | None, more: str, less: str) -> str:
    if c is None:
        return "needs 2 good runs of each to compare"
    words = {"more": more, "less": less, "same": "no measurable difference"}[c["verdict"]]
    return f"{c['pct']:+.1f} % (95 %: {c['lo']:+.1f} to {c['hi']:+.1f} %), {words}"


def text_lines(sweep: plan.Sweep) -> list[str]:
    """The ranking for the terminal, in plain ASCII."""
    rows = ranking(sweep)
    base = sweep.variants[0]
    out = [f"Results: average FPS and the 1 % low over {sweep.window_s:.0f} s from the start key, each with its 95 % "
           f"interval, from the counted runs. Each setting is compared with {base.id}, the file as it was."]
    for r in rows:
        n = (r["avg"] or {}).get("n", 0)
        out.append(f"  {r['variant']}  {r['label']}")
        out.append(f"     {n} good run{'s' if n != 1 else ''}: {_pm(r['avg'], ' FPS')}, 1 % low {_pm(r['low1'])}")
        if r["vs"] is not None:
            out.append(f"     average FPS against {base.id}: {_verdict(r['vs']['avg'], 'faster', 'slower')}")
            out.append(f"     1 % low against {base.id}: {_verdict(r['vs']['low1'], 'higher', 'lower')}")
    spread = interval([r.metrics.get("avg_fps") for r in plan.counted_runs(sweep, base.id)])
    if spread is not None and spread.get("cv") is not None:
        out.append(f"Runs of {base.id} varied by {100 * spread['cv']:.1f} % in average FPS from run to run: a "
                   "difference smaller than that is hard to see with this many runs.")
    out.append("Picture quality is yours to judge: lag-doctor measures only speed.")
    return out
