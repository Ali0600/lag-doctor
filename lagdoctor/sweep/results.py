"""What each run of a settings sweep measured, the checks that keep a run out, and the comparison of the settings.

A run is a recording, and its measured part is the window from the start key: `window_s` seconds from the press, cut
out of the main swap chain and judged by the report's own frame statistics (analysis/frames.py): average FPS and the
1 % low. The window's edges are cut exactly: a frame whose time on screen began before the press counts only its part
after it, and the frame after the window's end counts its part up to it, so a freeze across either edge is seen.

A run counts only when, over that window: frame timing ran and the recording lasted it; it holds 1,000 frames or more
and they reach both its ends; the game was in front in every sample (half a second of grace at each end); other
programs took 10 % or more of the game's GPU in at most 5 % of the samples (the report's own threshold for sharing it);
and the NVIDIA GPU's clock held steady (its 90th percentile within 15 % of its 10th: on the owner's laptop a GPU that
has not heated up yet runs at twice the speed it settles at). It also has to match the first good run on the power
plan, Windows' power mode, mains power and the refresh rate (docs/research/settings-sweeps.md, section 7). The GPU's
power limit is recorded, not held: Dynamic Boost moves it by itself, and the laptop's own mode shows as a plan change.

Settings are put in order of average FPS and each compared with A (the file as it was) by Welch's t interval, which
holds at 2 to 10 runs where a bootstrap's interval is too narrow (research note, section 4), widened for the number of
settings compared (Bonferroni), so that all the comparisons hold together 95 % of the time. An interval that holds 0
is "no measurable difference", never a winner. The 1 % low gets the same comparison; picture quality is the player's.
"""
from __future__ import annotations

import math
import statistics
from typing import Any, Sequence

from lagdoctor.analysis import frames as fa
from lagdoctor.analysis.window import window
from lagdoctor.model import F, G, S, Capture
from lagdoctor.sweep import plan

EDGE_S = 0.5             # a sample this near either end of the window may still show the game coming to the front
OTHER_GPU_MAX = fa.SHARED_GPU_PCT      # another program at 10 % or more of the game's GPU shares it (the report's rule)
OTHER_SHARE_MAX = 0.05   # ... in at most this share of the window's samples
REACH_S = 1.0            # the game's frames must start and end this near the window's ends
CLOCK_SPREAD_MAX = 1.15  # the GPU clock's 90th percentile over its 10th: more means it moved during the window
REFRESH_TOL_HZ = 1.0
CONFIDENCE = 0.95
POWER = 0.80             # the chance a sweep has of seeing the difference it says it can see
DF_MAX = 1000.0          # Welch's degrees of freedom past this make no difference to the interval (t = 1.962)
FRAMES_OK = ("ok", "partial")
SAMPLE_S = 0.25          # the system sampler's period (DESIGN: 4 Hz)


def _finite(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return float(v)


def _numbers(values: Sequence[Any]) -> list[float]:
    return [x for x in (_finite(v) for v in values) if x is not None]


def _median(values: Sequence[Any]) -> float | None:
    clean = _numbers(values)
    return float(statistics.median(clean)) if clean else None


def _round(v: Any, digits: int = 3) -> float | None:
    x = _finite(v)
    return None if x is None else round(x, digits)


# ------------------------------------------------------------- one run --

def clipped_frames(cap: Capture, t0: float, t1: float) -> dict[str, list]:
    """The main swap chain's rows inside (t0, t1], plus the first row after t1, with the edges cut to the window: the
    first row's frame and display times are cut to the part after t0, and the row after t1 becomes a row at t1 holding
    the part of its interval before t1. So a freeze across either edge counts only its part inside the window."""
    fr = fa.main_chain(cap)
    ts = fr.get(F.T) or []
    inside = [i for i, t in enumerate(ts) if (v := _finite(t)) is not None and t0 < v <= t1]
    after = next((i for i, t in enumerate(ts) if (v := _finite(t)) is not None and v > t1), None)
    keep = inside + ([after] if after is not None else [])
    out = {name: [col[i] if i < len(col) else None for i in keep] for name, col in fr.items() if isinstance(col, list)}
    if not keep:
        return out
    cols = (F.FRAME_MS, F.DISPLAY_MS)
    if inside:
        first = 0
        part = (float(ts[inside[0]]) - t0) * 1000.0
        for name in cols:
            v = _finite(out[name][first]) if name in out else None
            if v is not None and v > part:
                out[name][first] = round(part, 6)
    if after is not None:
        last = len(keep) - 1
        prev = float(ts[inside[-1]]) if inside else t0
        part = (t1 - prev) * 1000.0
        out[F.T][last] = t1
        for name in cols:
            if name in out:
                out[name][last] = round(part, 6) if part > 0 else None
    return out


def window_metrics(cap: Capture, t0: float, window_s: float) -> dict[str, Any]:
    """The numbers of the window [t0, t0 + window_s] (the recording's seconds): the frame statistics of the main swap
    chain cut exactly to it, how near its ends the frames reach, the seconds the game was behind another window,
    the share of samples with another program on the game's GPU, and the GPU's clock and temperature."""
    t0, window_s = float(t0), float(window_s)
    t1 = t0 + window_s
    fr = clipped_frames(cap, t0, t1)
    stats = fa.frame_stats(fr, window_s)
    cut = window(cap, t0, t1, frames=fr)
    times = _numbers(fr.get(F.T) or [])
    system = cut["system"]
    focus = [(t, v) for t, v in zip(system.get(S.T) or [], system.get(S.GAME_FOCUSED) or [])
             if _finite(t) is not None and v in (0, 1) and not isinstance(v, bool)]
    behind = [t for t, v in focus if v == 0 and t0 + EDGE_S <= t <= t1 - EDGE_S]
    other = _numbers(system.get(S.GPU_OTHER_PCT) or [])
    gpu = cut["gpu"]
    clocks = sorted(_numbers(gpu.get(G.CLOCK_MHZ) or []))
    throttle = _numbers(gpu.get(G.THROTTLE_PCT) or [])
    return {
        "frames": stats.get("frames") or 0,
        "avg_fps": _round(stats.get("avg_fps")),
        "low1_fps": _round(stats.get("low1_fps")),
        "p1_fps": _round(stats.get("p1_fps")),
        "worst_frame_ms": _round(stats.get("worst_frame_ms")),
        "hitches": stats.get("hitches"),
        "first_frame_s": round(min(times) - t0, 3) if times else None,
        "last_gap_s": round(t1 - max(times), 3) if times else None,
        "focus_samples": len(focus),
        "behind_s": round(len(behind) * SAMPLE_S, 2),
        "gpu_other_share": round(sum(1 for x in other if x >= OTHER_GPU_MAX) / len(other), 4) if other else None,
        "gpu_other_pct": _round(_median(other)),
        "gpu_clock_mhz": _round(_median(clocks), 1),
        "gpu_clock_p10": _round(_percentile(clocks, 10.0), 1),
        "gpu_clock_p90": _round(_percentile(clocks, 90.0), 1),
        "gpu_temp_c": _round(_median(gpu.get(G.TEMP_C) or []), 1),
        "gpu_power_limit_w": _round(_median(gpu.get(G.POWER_LIMIT_W) or []), 1),
        "gpu_throttle_pct": _round(math.fsum(throttle) / len(throttle), 1) if throttle else None,
    }


def _percentile(sorted_values: list[float], p: float) -> float | None:
    if not sorted_values:
        return None
    pos = (len(sorted_values) - 1) * p / 100.0
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


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
    first, gap = _finite(metrics.get("first_frame_s")), _finite(metrics.get("last_gap_s"))
    if first is not None and first > REACH_S:
        out.append(f"the game drew no frame for the window's first {first:.0f} s")
    if gap is not None and gap > REACH_S:
        out.append(f"the game drew no frame for the window's last {gap:.0f} s")
    if not metrics.get("focus_samples"):
        out.append("whether the game was in front could not be read")
    elif (_finite(metrics.get("behind_s")) or 0.0) > 0:
        out.append(f"the game was behind another window for about {max(1.0, metrics['behind_s']):.0f} s of the window")
    share = _finite(metrics.get("gpu_other_share"))
    if share is not None and share > OTHER_SHARE_MAX:
        out.append(f"another program used {OTHER_GPU_MAX:.0f} % or more of the game's GPU for about "
                   f"{share * window_s:.0f} s of the window")
    lo, hi = _finite(metrics.get("gpu_clock_p10")), _finite(metrics.get("gpu_clock_p90"))
    if lo and hi and hi > CLOCK_SPREAD_MAX * lo:
        out.append(f"the GPU's clock moved between {lo:.0f} and {hi:.0f} MHz during the window, so it was still "
                   "heating up: play a little longer at the spot before you press the start key")
    return out


def unchecked(metrics: dict[str, Any], conditions: dict[str, Any]) -> list[str]:
    """What could not be checked in this run, in plain words (it still counts)."""
    out = []
    if metrics.get("gpu_other_share") is None:
        out.append("other programs on the GPU")
    if metrics.get("gpu_clock_p10") is None:
        out.append("the GPU's clock (no NVIDIA readings)")
    if conditions.get("on_battery") is None:
        out.append("mains power")
    if conditions.get("refresh_hz") is None:
        out.append("the refresh rate")
    if conditions.get("power_plan") is None:
        out.append("the power plan")
    return out


def conditions(cap: Capture, metrics: dict[str, Any]) -> dict[str, Any]:
    """What a sweep holds fixed from run to run, as this run's recording saw it, and the GPU's power limit (recorded,
    not held)."""
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


def _mode_words(mode: str) -> str:
    return mode.replace("_", " ")


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
                   f"{now.get('power_plan_name') or plan_now} (the laptop's own power mode moves it too)")
    mode_was, mode_now = reference.get("power_mode"), now.get("power_mode")
    if mode_was and mode_now and mode_was != mode_now:
        out.append(f"Windows' power mode changed from {_mode_words(mode_was)} to {_mode_words(mode_now)}")
    hz_was, hz_now = _finite(reference.get("refresh_hz")), _finite(now.get("refresh_hz"))
    if hz_was is not None and hz_now is not None and abs(hz_was - hz_now) > REFRESH_TOL_HZ:
        out.append(f"the screen's refresh rate changed from {hz_was:.0f} Hz to {hz_now:.0f} Hz")
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


def _two_sided(comparisons: int) -> float:
    """The t quantile's p for a two-sided interval that holds 95 % of the time for all `comparisons` together."""
    return 1.0 - (1.0 - CONFIDENCE) / (2 * max(1, comparisons))


def interval(values: Sequence[float]) -> dict[str, Any] | None:
    """{n, mean, sd, half, cv}: the mean and its 95 % t interval's half width (None for a single run), the standard
    deviation and its share of the mean."""
    xs = _numbers(values)
    if not xs:
        return None
    mean = math.fsum(xs) / len(xs)
    if len(xs) < 2:
        return {"n": 1, "mean": mean, "sd": None, "half": None, "cv": None}
    sd = statistics.stdev(xs)
    half = t_quantile((1 + CONFIDENCE) / 2, len(xs) - 1) * sd / math.sqrt(len(xs))
    return {"n": len(xs), "mean": mean, "sd": sd, "half": half, "cv": sd / mean if mean else None}


def welch(a: Sequence[float], b: Sequence[float], comparisons: int = 1) -> dict[str, Any] | None:
    """Welch's interval for mean(a) - mean(b), at 95 % for `comparisons` intervals together (Bonferroni):
    {diff, lo, hi, df}; None unless both have 2 runs or more. With no spread at all the interval is the difference
    itself (df None)."""
    xa, xb = _numbers(a), _numbers(b)
    if len(xa) < 2 or len(xb) < 2:
        return None
    ma, mb = math.fsum(xa) / len(xa), math.fsum(xb) / len(xb)
    sa, sb = statistics.variance(xa) / len(xa), statistics.variance(xb) / len(xb)
    diff = ma - mb
    se2 = sa + sb
    if se2 <= 0:
        return {"diff": diff, "lo": diff, "hi": diff, "df": None}
    df = se2 * se2 / (sa * sa / (len(xa) - 1) + sb * sb / (len(xb) - 1))
    half = t_quantile(_two_sided(comparisons), df) * math.sqrt(se2)
    return {"diff": diff, "lo": diff - half, "hi": diff + half, "df": df}


def compare(values: Sequence[float], base: Sequence[float], comparisons: int = 1) -> dict[str, Any] | None:
    """A setting against A, in % of A's mean: {pct, lo, hi, verdict, flat}, verdict "more", "less" or "same" (the
    interval holds 0), flat True when neither has any spread. None unless both have 2 good runs or more."""
    w = welch(values, base, comparisons)
    base_mean = interval(base)
    if w is None or base_mean is None or not base_mean["mean"]:
        return None
    m = base_mean["mean"]
    verdict = "more" if w["lo"] > 0 else "less" if w["hi"] < 0 else "same"
    return {"pct": 100.0 * w["diff"] / m, "lo": 100.0 * w["lo"] / m, "hi": 100.0 * w["hi"] / m, "verdict": verdict,
            "flat": w["df"] is None}


def detectable(base: Sequence[float], n: int, comparisons: int = 1) -> float | None:
    """The difference, in % of A's mean, that a comparison with `n` runs a side finds 80 % of the time at A's own
    spread: (t at the comparison's level + t at 80 %) x sd x sqrt(2 / n)."""
    spread = interval(base)
    if spread is None or spread.get("sd") is None or not spread["mean"] or n < 2:
        return None
    df = 2 * n - 2
    t = t_quantile(_two_sided(comparisons), df) + t_quantile(POWER, df)
    return 100.0 * t * spread["sd"] * math.sqrt(2.0 / n) / spread["mean"]


# ---------------------------------------------------------- the ranking --

def ranking(sweep: plan.Sweep) -> list[dict[str, Any]]:
    """One row per setting, in order of average FPS, highest first (a setting without good runs last): its counted
    good runs' average FPS and 1 % low with their intervals, and, for B onwards, both compared with A, the intervals
    widened for the number of settings compared."""
    base = sweep.variants[0].id
    values = {v.id: ([r.metrics.get("avg_fps") for r in plan.counted_runs(sweep, v.id)],
                     [r.metrics.get("low1_fps") for r in plan.counted_runs(sweep, v.id)]) for v in sweep.variants}
    m = len(sweep.variants) - 1
    rows = []
    for v in sweep.variants:
        avg, low = values[v.id]
        row = {"variant": v.id, "label": v.label, "avg": interval(avg), "low1": interval(low), "vs": None}
        if v.id != base:
            row["vs"] = {"avg": compare(avg, values[base][0], m), "low1": compare(low, values[base][1], m)}
        rows.append(row)
    rows.sort(key=lambda r: (r["avg"] is None, -(r["avg"] or {}).get("mean", 0.0)))
    return rows


def _pm(row: dict[str, Any] | None, unit: str = "") -> str:
    if row is None:
        return "-"
    text = f"{row['mean']:.1f}{unit}"
    return text + (f" +/- {row['half']:.1f}" if row.get("half") is not None else " (1 run)")


def _verdict(c: dict[str, Any] | None, more: str, less: str) -> str:
    if c is None:
        return "needs 2 good runs of each to compare"
    words = {"more": more, "less": less, "same": "no measurable difference"}[c["verdict"]]
    if c.get("flat"):
        return f"{c['pct']:+.1f} % in every run, {words}"
    return f"{c['pct']:+.1f} % ({c['lo']:+.1f} to {c['hi']:+.1f} %), {words}"


def text_lines(sweep: plan.Sweep) -> list[str]:
    """The comparison for the terminal, in plain ASCII."""
    rows = ranking(sweep)
    base = sweep.variants[0]
    m = len(sweep.variants) - 1
    together = (f"; the {m} comparisons hold together 95 % of the time" if m > 1 else "")
    out = [f"Results, in order of average FPS: average FPS and the 1 % low over {sweep.window_s:.0f} s from the start "
           f"key, each +/- its 95 % interval, from the counted runs. Each setting is compared with {base.id}, the file "
           f"as it was{together}."]
    for r in rows:
        n = (r["avg"] or {}).get("n", 0)
        out.append(f"  {r['variant']}  {r['label']}")
        out.append(f"     {n} good run{'s' if n != 1 else ''}: {_pm(r['avg'], ' FPS')}, 1 % low {_pm(r['low1'])}")
        if r["vs"] is not None:
            out.append(f"     average FPS against {base.id}: {_verdict(r['vs']['avg'], 'faster', 'slower')}")
            out.append(f"     1 % low against {base.id}: {_verdict(r['vs']['low1'], 'higher', 'lower')}")
    base_avg = [r.metrics.get("avg_fps") for r in plan.counted_runs(sweep, base.id)]
    spread = interval(base_avg)
    see = detectable(base_avg, sweep.runs_per_variant, m)
    if spread is not None and spread.get("cv") is not None and see is not None:
        out.append(f"Runs of {base.id} varied by {100 * spread['cv']:.1f} % in average FPS from run to run: with "
                   f"{sweep.runs_per_variant} runs of each setting, this sweep sees a difference of about {see:.1f} % "
                   "or more; smaller ones may be there and not show.")
    skipped = sorted({u for r in sweep.runs if r.state == "good"
                      for u in (r.conditions.get("unchecked") or "").split("; ") if u})
    if skipped:
        out.append("Not checked in some runs (lag-doctor could not read them): " + ", ".join(skipped) + ".")
    for note in sweep.notes:
        out.append(f"Note: {note}")
    out.append("Picture quality is yours to judge: lag-doctor measures only speed.")
    return out
