"""diagnose() end to end on LagLab-like captures, judged by the real end-to-end judge, plus its honesty rules.

The judge (scripts/e2e_truth.py) takes its expected answers from LagLab's own log, never from lag-doctor, and
has_discrete_gpu is worked out from the inventory exactly as scripts/e2e.py does.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from e2e_truth import judge  # noqa: E402

from lagdoctor.analysis import attribute, events, frames, rules  # noqa: E402
from lagdoctor.analysis import diagnose as dg  # noqa: E402
from lagdoctor.analysis.diagnose import diagnose  # noqa: E402
from lagdoctor.model import F, N, S, Capture, Diagnosis, Finding, SourceStatus  # noqa: E402
from tests import synth  # noqa: E402


def discrete(cap) -> bool:
    """scripts/e2e.py's own test for a discrete GPU in the inventory."""
    return any(a.get("integrated") is False and not a.get("software") for a in cap.inventory.get("adapters") or [])


def causes(d: Diagnosis) -> set[str]:
    return {f.cause for f in d.findings}


# ----------------------------------------------------------------------------- acceptance ---

@pytest.mark.parametrize("hiccups", [0.0, synth.MEASURED_HICCUP_RATE])
@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
@pytest.mark.parametrize("mode", synth.MODES)
def test_every_laglab_scenario_passes_the_end_to_end_judge(mode, seed, hiccups):
    s = synth.laglab(mode, seed=seed, hiccups=hiccups)
    d = diagnose(s.cap)
    assert discrete(s.cap) and "intel" in s.log["gl_renderer"].lower()     # so the judge demands wrong_gpu too
    v = judge(mode, s.log, s.cap.clock_origin, d, has_discrete_gpu=discrete(s.cap))
    assert v.ok, [(c.name, c.detail) for c in v.checks if not c.ok]
    assert not [w for w in d.unchecked.values() if w.startswith("the check failed")]


# gpu and cpu: changed on purpose. Their frames stay up 3 or 4 of the 144 Hz refreshes (40 FPS), which is VSync judder
# vsync_judder used to miss once most frames took 2 or more, so they are low FPS and uneven pacing: "mixed".
@pytest.mark.parametrize("mode, lag, lead", [("smooth", "none", None), ("gpu", "mixed", "wrong_gpu"),
                                             ("cpu", "mixed", "cpu_limit"), ("hitch", "stutter", "game_cpu_spike"),
                                             ("hitch-io", "stutter", "asset_streaming"),
                                             ("hitch-wait", "stutter", "game_stall"), ("hog", "mixed", "background_hogs")])
def test_lag_type_and_the_finding_the_headline_names(mode, lag, lead):
    d = diagnose(synth.laglab(mode).cap)
    assert d.lag_type == lag
    if lead:
        assert d.findings[0].cause == lead and d.findings[0].confidence == "high"
        assert dg.labels.cause_text(lead) in d.headline


def test_laglabs_pace_mode_is_uneven_and_low_and_the_headline_names_the_uneven_pacing():
    # Changed on purpose (A1, A6). Pace mode sleeps to its own 4 / 20 ms deadlines: 66 FPS shown on the 144 Hz
    # panel with neither side at its limit ("waiting"), so it is low FPS as well as uneven: "mixed". One frame-time
    # pattern weighs W_STEADY now, so uneven_pacing is "possible" and wrong_gpu (same tier, an auto-fix) ranks
    # first; the headline still names uneven_pacing, the finding that claims the lag's own events.
    d = diagnose(synth.laglab("pace").cap)
    assert d.lag_type == "mixed" and d.stats["bound"] == "waiting"
    pacing = next(f for f in d.findings if f.cause == "uneven_pacing")
    assert pacing.confidence == "medium" and set(pacing.event_ids) & {e.id for e in d.events if e.kind == "pacing"}
    assert d.headline.startswith("Several problems: Low frame rate (66 FPS average on a 144 Hz screen; neither the "
                                 "CPU nor the GPU is at its limit) and uneven frame pacing over ")
    assert "the game itself hands over its frames at uneven intervals" in d.headline


def test_headlines_carry_the_numbers_and_the_rule_of_three():
    gpu = diagnose(synth.laglab("gpu").cap).headline
    assert gpu.startswith("Several problems: Low frame rate (40 FPS average on a 144 Hz screen) and uneven frame pacing "
                          "over 98% of the capture, most likely running on the wrong GPU; possible: VSync judder;")
    assert "no hitches ≥ 50 ms in" in gpu and "at 95% confidence" in gpu
    hitch = diagnose(synth.laglab("hitch").cap).headline
    assert hitch.startswith("9 stutters (18.0 a minute, worst ") and "no hitches" not in hitch
    smooth = diagnose(synth.laglab("smooth").cap).headline
    assert smooth.startswith("No lag stood out: no hitches ≥ 50 ms in ")


# ------------------------------------------------------------------------------- negatives ---

@pytest.mark.parametrize("seed", [1, 2, 3])
def test_the_smooth_control_gets_no_limit_pacing_or_stutter_finding(seed):
    d = diagnose(synth.laglab("smooth", seed=seed).cap)
    assert not causes(d) & {"gpu_limit", "cpu_limit", "uneven_pacing", "vsync_judder"}
    assert not [f.cause for f in d.findings if f.category in ("stutter", "pacing")]
    assert d.lag_type == "none" and d.stats["stutters"] == 0 and d.stats["hitches"] == 0
    assert {"gpu_limit", "cpu_limit", "uneven_pacing"} <= set(d.cleared)       # checked, with what was measured


FRAME_RULES = ("cpu_limit", "gpu_limit", "vsync_judder", "dropped_frames", "uneven_pacing", "frame_cap",
               "windowed_composition", "shader_compile", "background_hogs", "game_cpu_spike", "asset_streaming",
               "driver_dpc", "gpu_spike", "game_stall")


def test_without_frame_timing_frame_rules_are_unchecked_and_nothing_claims_no_stutter():
    s = synth.laglab("hitch", with_frames=False)
    why = s.cap.sources["frames"].detail
    d = diagnose(s.cap)
    for cause in FRAME_RULES:
        assert cause in d.unchecked, cause                  # the why is the Not measured list's, said once (wording)
        assert d.unchecked[cause].startswith(dg.labels.needs_source("frames")), (cause, d.unchecked[cause])
    assert not set(FRAME_RULES) & (set(d.cleared) | causes(d))
    assert not [f for f in d.findings if f.category in ("stutter", "pacing")]
    for key in ("hitches", "severe_hitches", "freezes", "stutters", "stutters_per_min", "hitch_rate_bound",
                "avg_fps", "low1_fps", "worst_frame_ms", "time_beyond_50_ms"):
        assert d.stats[key] is None, key
    assert d.stats["frames"] == 0 and d.lag_type == "unknown"
    text = d.headline.lower()
    assert "no hitch" not in text and "no lag" not in text and "no stutter" not in text
    assert d.headline.startswith(f"Frame timing was not measured ({dg.labels.short_reason(why)}), so stutter")
    assert d.measured["frames"] == why


def test_frame_rows_from_a_source_that_failed_never_back_a_no_hitch_claim():
    cap = synth.laglab("smooth").cap                         # rows are there, but the source says it broke
    cap.sources["frames"] = SourceStatus("failed", "PresentMon stopped after 4 s")
    d = diagnose(cap)
    assert "no hitch" not in d.headline.lower() and "no lag" not in d.headline.lower()
    assert d.lag_type == "unknown" and d.stats["stutters"] is None
    assert d.measured["frames"] == "PresentMon stopped after 4 s"           # the Not measured list's reason
    assert d.unchecked["uneven_pacing"] == dg.labels.needs_source("frames")


def test_the_headline_names_the_finding_that_claims_the_lags_own_events():
    from lagdoctor.model import LagEvent
    cap = synth.laglab("hitch").cap
    env = rules._Env(cap, {"stats": {"frames": 3000}})
    evs = [LagEvent(1, "stutter", 5.0, 5.15, 150.0, {})]
    other = Finding(cause="vram_overflow", title="t", category="stutter", confidence="high", score=0.9, summary="")
    claims = Finding(cause="asset_streaming", title="t", category="stutter", confidence="medium", score=0.4,
                     summary="", event_ids=[1])
    text = dg.headline("stutter", [other, claims], {"stutters_per_min": 2.0}, evs, {"stutter": 0.15}, env)
    assert text == "1 stutter (2.0 a minute, worst 150 ms), possibly loading from disk."
    text = dg.headline("none", [claims], {"hitches": 0, "frames": 3000, "hitch_rate_bound": 0.001}, [], {}, env)
    assert text == ("No lag stood out: no hitches ≥ 50 ms in 3,000 frames (at most 0.1% of frames at 95% "
                    "confidence); possible: loading from disk.")


def test_a_steady_low_frame_rate_is_led_by_the_part_that_holds_it_not_by_a_few_claimed_drops():
    # A real GPU-bound recording ran 58 FPS on a 144 Hz screen for 12 minutes with 6 FPS drops, 4 of them claimed by
    # background programs, and its headline blamed background programs: the steady low rate, most of the lag, was
    # the GPU's, and the finding that names it claims no events.
    from lagdoctor.model import LagEvent
    cap = synth.laglab("hitch").cap
    evs = [LagEvent(i, "fps_drop", 20.0 * i, 20.0 * i + 2.0, 30.0, {}) for i in range(1, 7)]
    hogs = Finding(cause="background_hogs", title="t", category="stutter", confidence="high", score=0.95, summary="",
                   event_ids=[1, 2, 3, 4])
    limit = Finding(cause="gpu_limit", title="t", category="fps", confidence="high", score=0.9, summary="")
    stats = {"avg_fps": 58.0, "refresh_hz": 144.0, "duration_s": 750.0}

    def text(verdict, steady_s, findings=(hogs, limit), events=evs):
        b = {"verdict": verdict, "steady_fps": 58.0, "refresh_hz": 144.0, "steady_s": steady_s}
        env = rules._Env(cap, {"stats": {"frames": 40000}, "bound": b})
        lag = dg.lag_time(events, list(findings), stats, b, True)
        return dg.headline(dg.pick_lag_type(lag, True), list(findings), stats, events, lag, env)

    # the claimer the headline used to name stays in it, as a side note
    assert text("gpu", 700.0) == ("Low frame rate (58 FPS average on a 144 Hz screen), most likely GPU at its limit; "
                                  "likely: background programs.")
    # the steady time is shorter than the drops the best-ranked claimer explains (8 s): the claimer leads, as before,
    # and the limit that holds the steady rate is the side note
    assert text("gpu", 5.0).endswith(", most likely background programs; likely: GPU at its limit.")
    # waiting: no part holds the steady rate, so no steady lead
    assert text("waiting", 700.0).endswith(", most likely background programs.")
    # a part that is not the limit's never leads, even when it ranks first
    cpu = Finding(cause="cpu_throttle", title="t", category="fps", confidence="high", score=0.99, summary="")
    assert text("gpu", 700.0, (cpu, hogs, limit)).endswith(", most likely GPU at its limit; likely: background programs.")
    # mixed with pacing: the steady time is set against what the best-ranked claimer explains, not every event ...
    pacing = evs + [LagEvent(7, "pacing", 0.0, 800.0, 8.0, {})]
    paced = Finding(cause="uneven_pacing", title="t", category="pacing", confidence="medium", score=0.5, summary="",
                    event_ids=[7])
    assert text("gpu", 700.0, (hogs, limit, paced), pacing).endswith(
        ", most likely GPU at its limit; likely: background programs.")
    # with no claimer there is no side note
    assert text("gpu", 700.0, (limit,)).endswith(", most likely GPU at its limit.")
    # ... and a claimer that explains more than the steady time leads
    first = Finding(cause="uneven_pacing", title="t", category="pacing", confidence="high", score=0.99, summary="",
                    event_ids=[7])
    assert text("gpu", 700.0, (first, limit), pacing).endswith(
        ": the game itself hands over its frames at uneven intervals; likely: GPU at its limit.")
    # no steady low frame rate (a cap is its own target): no limit to name beside the claimer
    assert text("capped", 700.0).endswith(", most likely background programs.")
    # a capped game is its own target: its steady time is never low FPS
    assert dg.steady_low_s({"verdict": "capped", "steady_fps": 58.0, "refresh_hz": 144.0, "steady_s": 700.0}, True) == 0


def gpu_bound_with_drops(drop_r: float, n: int = 6, dur: float = 120.0) -> Capture:
    """A GPU-bound game at ~51 FPS on the 144 Hz panel (r = 0.96) with n 3 s FPS drops to ~25 FPS (the GPU busy
    drop_r of each of their frames) while another program, hog.exe, uses 1.5 CPUs; nothing else moves."""
    import random
    rng = random.Random(7)
    spans = [(15.0 + i * (dur - 30.0) / (n - 1), 18.0 + i * (dur - 30.0) / (n - 1)) for i in range(n)]

    def hot(x):
        return any(a <= x <= b for a, b in spans)

    fms, gms, t = [], [], 1.0
    while t < dur - 0.5:
        f = (40.0 if hot(t) else 19.6) * rng.uniform(0.97, 1.03)
        fms.append(f)
        gms.append(f * (drop_r if hot(t) else 0.96))
        t += f / 1000.0
    fr = synth.frames_table(fms, gpu_ms=gms, display_ms=fms, displayed=1, mode="Hardware: Independent Flip",
                            runtime="DXGI")
    sysb = synth.system_table(dur, game_top_thread_pct=lambda x: 55.0 + rng.uniform(-3, 3), game_cpu_pct=120.0,
                              game_gpu_pct=lambda x: 80.0 if hot(x) else 97.0, cpu_total_pct=20.0, game_focused=1)
    cap = synth.capture(fr, sysb, hz=synth.REFRESH_HZ, duration=dur)
    cap.sources = synth.sources(("frames", "system"))
    cap.target = {"pid": 4242, "pids": [4242], "name": "game.exe"}
    cap.others = [[x, [[30000, "hog.exe", 150.0 if hot(x) else rng.uniform(0.5, 1.5), 0.0, 0.0],
                       [2656, "dwm.exe", rng.uniform(2.0, 4.5), 0.0, 2.0]]] for x in sysb[S.T]]
    return cap


def test_a_gpu_bound_recording_with_a_few_background_drops_is_headed_by_the_gpu():
    # Through diagnose(): the GPU limits every steady frame (r = 0.96, 51 FPS on 144 Hz, 100 of the 118 s), a background
    # program is behind all 6 FPS drops (18 s), and the GPU limit, which ranks first, claims no events
    d = diagnose(gpu_bound_with_drops(drop_r=0.80))
    drops = [e.id for e in d.events if e.kind == "fps_drop"]
    assert d.stats["bound"] == "gpu" and d.lag_type == "low_fps" and len(drops) == 6
    assert [(f.cause, f.confidence) for f in d.findings[:2]] == [("gpu_limit", "high"), ("background_hogs", "medium")]
    assert d.findings[0].event_ids == [] and d.findings[1].event_ids == drops
    assert d.headline.startswith("Low frame rate (47 FPS average on a 144 Hz screen), most likely GPU at its limit; "
                                 "possible: background programs"), d.headline


def test_when_the_drops_claimer_outranks_the_steady_limit_the_headline_still_names_it():
    # CPU-side drops give background_hogs frame-timing support (S = 5) over gpu_limit (S = 4, no NVIDIA telemetry), so
    # it ranks first and is the app's Top cause; the steady lead is still the GPU, and the headline names both
    d = diagnose(gpu_bound_with_drops(drop_r=0.40))
    assert [f.cause for f in d.findings[:2]] == ["background_hogs", "gpu_limit"]
    assert "most likely GPU at its limit; likely: background programs" in d.headline, d.headline


def held_two_or_three(seed=5, n=1700):
    """A GPU-bound game under VSync on the 144 Hz panel (a made-up shape): frames stay up two or three refreshes (now
    and then one or four) in runs, each presented that hold plus up to 1.5 ms of jitter; GPU busy 16.5 ms a frame."""
    import random
    rng = random.Random(seed)
    p = 1000.0 / synth.REFRESH_HZ
    holds = [2]
    while len(holds) < n:
        holds.append(holds[-1] if rng.random() < 0.5 else rng.choices([1, 2, 3, 4], [3, 52, 43, 2])[0])
    ms = [h * p + rng.uniform(-1.5, 1.5) for h in holds]
    fr = synth.frames_table(ms, gpu_ms=16.5, display_ms=[h * p for h in holds], displayed=1)
    cap = synth.capture(fr, hz=synth.REFRESH_HZ, duration=1.0 + sum(ms) / 1000.0)
    cap.sources = synth.sources(("frames",))
    return cap


def test_a_game_vsync_holds_two_and_three_refreshes_is_named_vsync_judder_with_the_48_fps_cap():
    # A real recording held 92 % of its frames two or three refreshes on a 144 Hz screen with VSync on, and its report
    # said "the game itself hands over its frames at uneven intervals": vsync_judder gave up once most frames held two
    d = diagnose(held_two_or_three())
    pace = [e for e in d.events if e.kind == "pacing"]
    assert d.stats["bound"] == "gpu" and pace
    assert max(e.detail["present_swing_pct"] for e in pace) > 10.0        # the presents swing too
    judder = [f for f in d.findings if f.cause == "vsync_judder"]
    assert judder and judder[0].title == "VSync judder: frames alternate between two and three refreshes"
    assert "Or cap the frame rate at 48 FPS (a third of the refresh)" in [x.title for x in judder[0].fixes]
    assert set(judder[0].event_ids) == {e.id for e in pace}
    assert "uneven_pacing" not in causes(d) and "uneven_pacing" not in d.cleared
    assert "the game itself hands over its frames at uneven intervals" not in d.headline


def test_every_frame_rate_rule_is_sorted_into_a_steady_lead_or_left_out_on_purpose():
    fps_rules = {r.cause for r in rules.RULES if r.category == "fps"}
    sorted_in = {c for causes in dg.STEADY_LEADS.values() for c in causes} | set(dg.NOT_STEADY_LEADS)
    assert sorted_in == fps_rules


def test_without_frame_timing_marks_stand_in_at_half_weight_and_the_lag_stays_unknown():
    s = synth.laglab("hitch", with_frames=False, marks=[3.9, 6.9, 9.9, 12.9, 15.9, 18.9])
    d = diagnose(s.cap)
    assert d.lag_type == "unknown" and "Frame timing was not measured" in d.headline
    own = [f for f in d.findings if f.cause == "game_cpu_spike"]
    assert own and own[0].confidence != "high" and "marked moments" in own[0].evidence[0].text


GPU_SIDE = ("gpu_limit", "gpu_throttle", "vram_overflow", "hybrid_copy", "gpu_spike")    # LC 5.1 + attribute GPU_SIDE


def gpu_heavy_cpu_bound():
    """LagLab's cpu mode, re-pointed at the NVIDIA GPU with every GPU-side cause's own evidence planted."""
    s = synth.laglab("cpu")
    cap = s.cap
    cap.target["adapter_luid"] = synth.NVIDIA_LUID                         # renders on the dGPU, screen on the iGPU
    cap.sources["gpu"] = SourceStatus("ok", tool="NVML")
    cap.gpu = synth.gpu_table(30, nv_throttle_pct=80.0, nv_reasons="sw_thermal_slowdown", nv_util_pct=99.0,
                              nv_temp_c=85.0)
    cap.inventory["nvidia"] = {"temp_target_c": 75}
    n = len(cap.system[S.T])
    cap.system[S.VRAM_USED_MB] = [8000.0] * n                               # 8 GB card nearly full ...
    cap.system[S.GAME_SHARED_MB] = [900.0] * n                              # ... and spilling
    cap.system[S.GAME_GPU_PCT] = [99.0] * n
    return cap


def test_a_gpu_side_cause_is_never_reported_while_one_cpu_thread_limits_the_game():
    cap = gpu_heavy_cpu_bound()
    d = diagnose(cap)
    assert d.stats["bound"] == "cpu_main"
    assert not causes(d) & set(GPU_SIDE)
    for cause in ("gpu_throttle", "vram_overflow", "hybrid_copy", "gpu_limit"):
        assert "not what limits the frame rate" in d.cleared[cause], (cause, d.cleared.get(cause))
    assert "cpu_limit" in causes(d)


def test_the_planted_gpu_evidence_is_strong_enough_to_report_without_the_gate():
    """Control for the test above: the same capture with the verdict on the GPU side reports every one."""
    cap = gpu_heavy_cpu_bound()
    fr = frames.main_chain(cap)
    st = frames.frame_stats(fr, cap.duration_s)
    evs = events.find_events(cap, fr, st)
    b = frames.bound(cap, fr, evs)
    st.update(events.summary_stats(cap, evs))
    b["verdict"] = "gpu"
    found, _, _ = rules.run_rules(cap, {"frames": fr, "stats": st, "bound": b, "events": evs,
                                        "event_causes": attribute.attribute(cap, fr, evs, b)})
    assert {"gpu_throttle", "vram_overflow", "hybrid_copy"} <= {f.cause for f in found}


def test_without_frame_timing_gpu_heat_is_reported_where_the_gpu_was_pegged():
    # Item 1, through the real path (frames.bound's per-second reading, then run_rule's gate): the first real capture
    # kept its GPU at a median 71 % and 98-99 % in its last minutes, and GPU heat went to "could not check"
    cap = synth.laglab("smooth", duration_s=60.0, with_frames=False).cap
    cap.target["adapter_luid"] = synth.NVIDIA_LUID
    cap.sources["gpu"] = SourceStatus("ok", tool="NVML")
    cap.gpu = synth.gpu_table(60, nv_throttle_pct=80.0, nv_reasons="sw_thermal_slowdown", nv_temp_c=85.0)
    cap.inventory["nvidia"] = {"temp_target_c": 75}
    cap.system[S.GAME_GPU_PCT] = [99.0 if t >= 45.0 else 60.0 for t in cap.system[S.T]]   # the last quarter pegged
    d = diagnose(cap)
    assert d.stats["bound"] == "unknown" and "gpu_throttle" not in d.unchecked
    heat = next(f for f in d.findings if f.cause == "gpu_throttle")
    assert heat.confidence == "high" and heat.evidence[-1].text.startswith(
        "Frame timing was not measured, but the game kept its GPU's 3D engine 95% or more busy for 15 s (25% of the "
        "capture)")
    cap.system[S.GAME_GPU_PCT] = [99.0 if t >= 55.0 else 60.0 for t in cap.system[S.T]]   # 5 s: too little to tell
    d = diagnose(cap)
    assert "gpu_throttle" not in causes(d) and "busy for only 5 s" in d.unchecked["gpu_throttle"]


# ------------------------------------------------------------------------------ the fixes ---

# DESIGN.md "Fixes": each fix id and its exact params; the only cause that may offer each.
FIX_PARAMS = {"power_plan": {"scheme": str}, "refresh_rate": {"device": str, "hz": int},
              "gpu_preference": {"exe": str, "preference": int}, "game_dvr_off": {}, "game_mode_on": {},
              "windowed_optimizations_on": {},
              "background_priority": {"pid": int, "create_time": int, "class": str, "exclude": list, "name": str,
                                      "game_name": (str, type(None))}}
FIX_OF_CAUSE = {"power_plan": "power_plan", "refresh_rate": "refresh_rate", "wrong_gpu": "gpu_preference",
                "game_dvr": "game_dvr_off", "game_mode_off": "game_mode_on",
                "windowed_composition": "windowed_optimizations_on", "background_hogs": "background_priority"}


def everything_wrong():
    s = synth.laglab("hog")
    cap = s.cap
    inv = cap.inventory
    inv["power"] = {"scheme_guid": "64a64f24-65b9-4b56-befd-5ec1eaced9b3", "scheme_name": "Silent",
                    "schemes": [["381b4222-f694-41f0-9685-ff5bb260df2e", "Balanced"],
                                ["64a64f24-65b9-4b56-befd-5ec1eaced9b3", "Silent"]],
                    "slider": "balanced", "effective_mode": "balanced", "on_battery": False, "saver": False}
    inv["displays"][0].update(hz=60.0, max_hz=144)
    inv["settings"].update(game_dvr=True, background_recording=True, game_mode=False, windowed_optimizations=False)
    cap.frames[F.PRESENT_MODE] = ["Composed: Copy with GPU GDI"] * len(cap.frames[F.T])
    cap.frames[F.RUNTIME] = ["DXGI"] * len(cap.frames[F.T])
    # synth's hogs (pid 20000 and up) share LagLab's image name, python.exe, which never gets a priority fix
    # (SEC-2): name them as the separate program they stand for
    cap.others = [[t, [[p[0], "hog.exe" if p[0] >= 20000 else p[1], *p[2:], 134000000000000000 + p[0]]
                       for p in procs]] for t, procs in cap.others]
    return cap


def test_no_finding_is_titled_with_its_own_evidence_line():
    # Wording: the Game Bar cause's title was its only evidence line, word for word ("Game Bar capture is on; whether
    # it records in the background is unknown" twice). Every LagLab mode, everything wrong at once, and Windows'
    # default Game Bar state (capture on, the background setting absent)
    caps = [synth.laglab(mode).cap for mode in synth.MODES] + [everything_wrong()]
    default = synth.laglab("smooth").cap
    default.inventory["settings"].update(game_dvr=True, background_recording=None)
    caps.append(default)
    seen = set()
    for cap in caps:
        for f in diagnose(cap).findings:
            seen.add(f.cause)
            lines = {e.text.rstrip(".").casefold() for e in f.evidence}
            assert f.title.rstrip(".").casefold() not in lines, (f.cause, f.title)
    assert "game_dvr" in seen                       # the fixture's own premise: the Game Bar finding was among them


def test_every_fix_offered_matches_designs_fix_table_exactly():
    d = diagnose(everything_wrong())
    offered = {}
    for f in d.findings:
        for fx in f.fixes:
            if fx.fix_id is None:
                continue
            assert FIX_OF_CAUSE.get(f.cause) == fx.fix_id, (f.cause, fx.fix_id)
            want = FIX_PARAMS[fx.fix_id]
            assert set(fx.params) == set(want), (fx.fix_id, fx.params)
            assert all(isinstance(fx.params[k], t) and not isinstance(fx.params[k], bool) for k, t in want.items())
            offered[fx.fix_id] = fx.params
    assert set(offered) == set(FIX_PARAMS)
    assert offered["power_plan"] == {"scheme": "381b4222-f694-41f0-9685-ff5bb260df2e"}
    assert offered["refresh_rate"] == {"device": r"\\.\DISPLAY1", "hz": 144}
    assert offered["gpu_preference"] == {"exe": r"C:\Python313\python.exe", "preference": 2}
    assert offered["background_priority"]["class"] == "below_normal"
    assert offered["background_priority"]["pid"] != synth.GAME_PID
    assert synth.GAME_PID in offered["background_priority"]["exclude"]
    assert offered["background_priority"]["name"] == "hog.exe"
    assert offered["background_priority"]["game_name"] == "python.exe"


@pytest.mark.parametrize("mode", synth.MODES)
def test_advice_only_causes_never_carry_a_fix_id(mode):
    for f in diagnose(synth.laglab(mode).cap).findings:
        ids = {fx.fix_id for fx in f.fixes if fx.fix_id}
        assert ids <= {FIX_OF_CAUSE[f.cause]} if f.cause in FIX_OF_CAUSE else not ids, (f.cause, ids)


# ------------------------------------------------------------------- stats, measured, JSON ---

STATS_KEYS = ("frames duration_s avg_fps low1_fps low01_fps p1_fps median_frame_ms p99_frame_ms worst_frame_ms "
              "stutters stutters_per_min hitches severe_hitches freezes time_beyond_50_ms hitch_rate_bound "
              "dropped_pct bound bound_r gpu_busy_pct refresh_hz present_mode runtime swap_chains main_chain "
              "ping_gw_ms ping_inet_ms ping_server_ms jitter_ms loss_pct steady_fps bound_gpu_ms bound_frame_ms "
              "busiest_thread_pct gpu_ceiling_fps cpu_ceiling_low_fps cpu_ceiling_high_fps cpu_ceiling_from "
              "display_steps_fps next_limit speedup_low speedup_high headroom headroom_note gpu_clock_mhz "
              "gpu_clock_max_mhz gpu_power_w gpu_power_limit_w").split()       # DESIGN, Diagnosis.stats


def test_the_stats_hold_every_key_design_lists():
    d = diagnose(synth.laglab("hitch").cap)
    assert set(STATS_KEYS) <= set(d.stats)
    assert d.stats["marks"] == []                                  # CC-5: the user's presses, none here
    assert d.stats["main_chain"] == "0x0" and d.stats["bound"] == "capped" and d.stats["stutters"] == 9
    assert d.stats["ping_inet_ms"] is not None and d.stats["refresh_hz"] == pytest.approx(144.003)


def test_measured_says_ok_or_why_for_every_source():
    cap = synth.laglab("smooth").cap
    cap.sources["system"] = SourceStatus("partial", "the sampler fell behind for 2 s")
    del cap.sources["net_flows"]
    d = diagnose(cap)
    assert d.measured["frames"] == "ok" and d.measured["network"] == "ok"
    assert d.measured["system"] == "partial: the sampler fell behind for 2 s"
    assert d.measured["gpu"] == "the game renders on the Intel GPU; NVML not started"
    assert d.measured["net_flows"] == "not recorded in this capture"


def test_frames_that_ran_but_recorded_nothing_are_not_measured():
    cap = synth.laglab("smooth").cap
    cap.frames = {}
    d = diagnose(cap)
    assert d.measured["frames"] == "PresentMon ran but recorded no frames from the game"
    assert d.lag_type == "unknown" and d.stats["stutters"] is None
    assert d.unchecked["uneven_pacing"] == dg.labels.needs_source("frames")
    # the frame source's own state is "ok", yet the Not measured list the reason points at names it
    missing = dg.labels.not_measured(cap.sources, d.measured)
    assert ("frame timing (PresentMon)", "PresentMon ran but recorded no frames from the game") in missing


def test_the_diagnosis_survives_json_and_the_reports_render_it():
    from lagdoctor.report import console, html
    s = synth.laglab("hog")
    d = diagnose(s.cap)
    data = json.loads(json.dumps(d.to_json(), allow_nan=False))
    assert data["lag_type"] == "mixed" and data["findings"][0]["cause"] == "background_hogs"
    text = console.render(d, s.cap)
    assert "Background programs" in text and "Checked and fine" in text and "Could not check" in text
    page = html.render(d, s.cap)
    assert "Background programs" in page and "<script" not in page


# ------------------------------------------------------------------------------------ headroom ---

def heat_bound(fps_ms: float = 16.9, n: int = 3000) -> Capture:
    """A made-up capture: GPU-bound on a fixed 144 Hz screen, frames held 2 or 3 refreshes, the busiest game thread at
    75 % of a CPU, PresentMon's CPU Busy at nearly the whole frame (wall time), and an NVIDIA GPU held at 780 of its
    2,100 MHz by heat while drawing 61 of its 103 W."""
    p = 1000.0 / 144.003
    shown = [2 * p if i % 2 == 0 else 3 * p for i in range(n)]
    secs = 2.0 + n * fps_ms / 1000.0
    cap = synth.capture(synth.frames_table([fps_ms] * n, gpu_ms=16.1, display_ms=shown, displayed=1, runtime="DXGI",
                                           cpu_busy=16.55),
                        synth.system_table(secs, game_top_thread_pct=75.0, game_cpu_pct=300.0, game_gpu_pct=98.0),
                        hz=144.003, duration=secs,
                        gpu=synth.gpu_table(secs, nv_util_pct=99.0, nv_clock_mhz=780, nv_clock_max_mhz=2100,
                                            nv_power_w=61.0, nv_power_limit_w=102.8, nv_throttle_pct=97.0,
                                            nv_power_cap_pct=0.0, nv_reasons="sw_thermal_slowdown", nv_temp_c=88.0))
    cap.sources = synth.sources(("frames", "system", "gpu"))
    cap.inventory["nvidia"] = {"clock_max_mhz": 2100, "temp_target_c": 87, "power_default_w": 80.0}
    return cap


def test_the_headroom_reaches_the_stats_the_console_and_the_report_once_each():
    from lagdoctor.report import console, html, labels
    cap = heat_bound()
    d = diagnose(cap)
    s = d.stats
    assert s["bound"] == "gpu" and s["next_limit"] == "cpu" and s["cpu_ceiling_from"] == "thread"
    assert s["steady_fps"] == pytest.approx(1000.0 / (2.5 * 1000.0 / 144.003))       # 2 and 3 refreshes in turn
    assert (s["bound_gpu_ms"], s["bound_frame_ms"], s["busiest_thread_pct"]) == pytest.approx((16.1, 16.55, 75.0))
    assert s["gpu_ceiling_fps"] == pytest.approx(1000.0 / 16.1)
    assert (s["cpu_ceiling_low_fps"], s["cpu_ceiling_high_fps"]) == pytest.approx(
        (s["steady_fps"] * 0.85 / 0.75, s["steady_fps"] / 0.75))
    assert (s["gpu_clock_mhz"], s["gpu_clock_max_mhz"], s["gpu_power_w"], s["gpu_power_limit_w"]) == (
        780.0, 2100.0, 61.0, 102.8)
    gpu_limit = next(f for f in d.findings if f.cause == "gpu_limit")
    assert s["headroom"] == gpu_limit.evidence[1].text and s["headroom_note"] == labels.HEADROOM_NOTE
    assert s["headroom"].startswith("At these settings the CPU could feed about 65-77 FPS, so a GPU about 1.1-1.3x "
                                    "faster would make the CPU the limit; heat held this one at 37%")
    flat = " ".join(console.render(d, cap, width=100).split())
    assert flat.count(s["headroom"]) == 1 and flat.count(labels.HEADROOM_NOTE) == 1      # under the stats, not again
    assert f"Headroom: {s['headroom']} {labels.HEADROOM_NOTE}" in flat
    assert flat.index("limited by GPU") < flat.index("Headroom:") < flat.index("What is causing it")
    page = html.render(d, cap)
    assert page.count(html.prose(s["headroom"])) == 1 and page.count(html.prose(labels.HEADROOM_NOTE)) == 1


def test_no_headroom_line_shows_where_no_finding_carries_it():
    # GPU-bound at 139 FPS on the 144 Hz screen: the GPU limit costs nothing there, so it is checked and fine, and the
    # summary keeps the figures but no sentence the findings do not show
    n = 3000
    cap = synth.capture(synth.frames_table([7.2] * n, gpu_ms=7.0),
                        synth.system_table(2.0 + n * 0.0072, game_top_thread_pct=30.0, game_gpu_pct=98.0),
                        hz=144.003, duration=2.0 + n * 0.0072)
    cap.sources = synth.sources(("frames", "system"))
    d = diagnose(cap)
    assert d.stats["bound"] == "gpu" and "gpu_limit" in d.cleared
    assert d.stats["gpu_ceiling_fps"] == pytest.approx(1000.0 / 7.0) and d.stats["next_limit"] is not None
    assert d.stats["headroom"] is None and d.stats["headroom_note"] is None
    env = rules._env(cap, {"bound": {"verdict": "gpu", "headroom": {"steady_fps": 60.0}}})
    assert rules.headroom_line(env, []) == (None, None)


# ----------------------------------------------------------------- settings check, junk input ---

def test_a_settings_check_without_a_game_reports_settings_and_checks_nothing_else():
    inv = copy.deepcopy(synth.laglab("smooth").cap.inventory)
    inv["power"] = {"scheme_guid": "64a64f24-65b9-4b56-befd-5ec1eaced9b3", "scheme_name": "Silent",
                    "schemes": [["381b4222-f694-41f0-9685-ff5bb260df2e", "Balanced"]], "slider": "balanced",
                    "on_battery": False, "saver": False}
    inv["settings"].update(game_mode=False)
    cap = Capture(started_at="2026-11-04T12:00:00+00:00", clock_origin=0.0, duration_s=0.0,
                  target={"name": "settings check-up"}, sources={}, inventory=inv)       # as cli.cmd_check builds it
    d = diagnose(cap)
    assert {"power_plan", "game_mode_off"} <= causes(d)
    assert all(f.confidence != "high" for f in d.findings)        # static settings alone never make "likely"
    assert d.lag_type == "unknown" and set(d.measured.values()) == {"not recorded in this capture"}
    assert "frame timing (PresentMon)" in d.unchecked["cpu_limit"]
    assert not [w for w in d.unchecked.values() if w.startswith("the check failed")]


def junk_capture():
    cap = synth.laglab("hitch").cap
    cap.frames[F.FRAME_MS][5] = "lots"
    cap.frames[F.T][7] = float("nan")
    cap.frames[F.SWAP_CHAIN][9] = 12345
    cap.system[S.GAME_TOP_THREAD_PCT][3] = "busy"
    cap.system[S.RAM_AVAIL_MB][4] = float("inf")
    cap.inventory["adapters"].append("not an adapter")
    cap.inventory["adapters"][0]["vram_mb"] = "eight"
    cap.inventory["displays"].append({"hz": "fast"})
    cap.inventory["power"] = {"schemes": [["{GUID}", None], 7], "scheme_name": 42, "on_battery": "yes"}
    cap.inventory["settings"]["game_mode"] = "maybe"
    cap.inventory["game"]["media"] = ["HDD"]
    cap.others.append([1.0, "nobody"])
    cap.others.append(["t", [[1, 2, 3]]])
    cap.net_flows.update(route="vpn", downloads=[{"t": "now", "mbps": "fast"}, 5], wifi_scans="often",
                         gaps=[[1.0], ["x", 200.0, "delay"]], talkers=[{"pid": "x"}])
    cap.marks.append("later")
    return cap


def test_a_corrupt_capture_degrades_without_crashing_any_rule():
    d = diagnose(junk_capture())
    assert d.lag_type == "stutter"
    assert not [(c, w) for c, w in d.unchecked.items() if w.startswith("the check failed")]
    json.dumps(d.to_json(), allow_nan=False)


def test_a_corrupt_capture_still_gets_both_reports_whole(tmp_path):
    # A15: html.write crashed on a string in a system column, and the user lost the report of a capture
    # the analysis had read
    from lagdoctor.report import console, html
    cap = junk_capture()
    d = diagnose(cap)
    text = console.render(d, cap)
    assert "VERDICT [STUTTER]" in text and "could not be shown" not in text
    page = html.write(d, cap, tmp_path / "report.html").read_text(encoding="utf-8")
    assert "could not be drawn" not in page and page.endswith("</html>")
    assert "<h2>CPU and GPU</h2><svg" in page and "<h2>Frame times</h2><svg" in page


# --------------------------------------------------------------------- lag type and lag time ---

@pytest.mark.parametrize("lag, frames_ok, want", [
    ({"stutter": 2.0, "low_fps": 1.0}, True, "mixed"), ({"stutter": 2.0, "low_fps": 0.99}, True, "stutter"),
    ({"network": 3.0}, False, "network"), ({}, True, "none"), ({}, False, "unknown"),
    ({"stutter": 0.0, "pacing": 0.0}, True, "none")])
def test_lag_type_is_the_category_with_the_most_lag_time(lag, frames_ok, want):
    assert dg.pick_lag_type(lag, frames_ok) == want


# Changed on purpose, twice. The first points pinned LC 4.14's 0.9 x refresh (a cap rule), and "waiting" at 30 FPS on
# 144 Hz counting nothing (A6). Now: steady time under 80 % of the target (LC 2.4, S18; DECISIONS 2026-11-04) counts,
# whatever holds the rate there; a cap or a VSync lock is the game's own target. Item 6: the rate judged is the
# steady one (bound "steady_fps"), never avg_fps, which holds every stutter and freeze; this test once fed avg_fps
# alone, so it could not see that. avg_fps is set to the far side of each point here, and is never read.
@pytest.mark.parametrize("verdict, fps, hz, steady", [
    ("gpu", 115.1, 144.0, 25.0), ("gpu", 115.3, 144.0, 0.0),            # 0.8 x 144 = 115.2
    ("gpu", 120.0, 144.0, 0.0),                                         # DECISIONS: not "low" for the whole capture
    ("cpu_main", 47.9, None, 25.0), ("cpu_main", 48.1, None, 0.0),      # no refresh: 0.8 x 60 = 48
    ("cpu_all", 30.0, 60.0, 25.0), ("mixed", 30.0, 60.0, 25.0),
    ("waiting", 30.0, 144.0, 25.0), ("waiting", 120.0, 144.0, 0.0),     # A6: neither side at its limit
    ("unknown", 30.0, 144.0, 25.0),                                     # the frame rate is measured all the same
    ("capped", 30.0, 144.0, 0.0), ("vsync", 30.0, 60.0, 0.0)])         # their own target: never low FPS
def test_steady_time_under_80_percent_of_the_refresh_counts_as_low_fps(verdict, fps, hz, steady):
    other_side = {"avg_fps": 1000.0 if steady else 1.0}
    b = {"verdict": verdict, "refresh_hz": hz, "steady_s": 25.0, "steady_fps": fps}
    assert dg.lag_time([], [], other_side, b, True)["low_fps"] == steady
    assert dg.lag_time([], [], other_side, b, False)["low_fps"] == 0.0       # frames not measured: nothing to count
    assert dg.lag_time([], [], {"avg_fps": fps}, dict(b, steady_fps=None), True)["low_fps"] == 0.0   # no steady rate


@pytest.mark.parametrize("ft_ms, lag", [(1000.0 / 120.1, "stutter"), (10.0, "low_fps")])
def test_freezes_never_make_a_steady_frame_rate_low(ft_ms, lag):
    # Item 6 (A1): six 600 ms freezes pull a steady GPU-bound 120.1 FPS on the 144 Hz panel to a 113 FPS average,
    # and that once counted all 55 s of 120 FPS as low frame rate; 100 FPS steady is low (under 0.8 x 144 = 115.2)
    d = diagnose(gpu_bound_with_freezes(600.0, n=6, ft=ft_ms))
    assert d.stats["bound"] == "gpu" and d.stats["freezes"] == 6
    assert d.lag_type == lag, d.headline
    if lag == "stutter":
        assert d.stats["avg_fps"] < 0.8 * synth.REFRESH_HZ                   # the average alone would say low FPS
        assert d.headline.startswith("6 stutters (") and "Low frame rate" not in d.headline
    else:
        assert d.headline.startswith("Low frame rate (") and "; also 6 freezes, worst 600 ms" in d.headline


def test_a_steady_rate_other_programs_gpu_work_may_set_is_no_cap_and_counts_as_low_fps():
    # Item 9 (A7): the ML job keeps the game's GPU 80 % busy while the game runs a regular 58 FPS on the 144 Hz panel
    # with its own GPU work short, which is what a frame-rate cap looks like. It was "capped", the game's own target,
    # so lag time counted nothing ("No lag stood out") and the report said "limited by FPS cap"
    from lagdoctor.report import console
    s = synth.laglab("smooth", fps_cap=58.0, gpu_other_pct=80.0)
    d = diagnose(s.cap)
    assert d.stats["bound"] == "unknown" and d.lag_type == "low_fps"
    assert d.headline.startswith("Low frame rate (58 FPS average on a 144 Hz screen)"), d.headline
    assert next(f for f in d.findings if f.cause == "gpu_contention").confidence == "high"
    text = console.render(d, s.cap)
    assert "limited by -" in text and "limited by FPS cap" not in text and "frame-rate cap" not in d.headline
    assert not [c for c, t in d.cleared.items() if "frame-rate cap" in t], d.cleared
    assert "80% busy" in d.unchecked["frame_cap"] and "80% busy" in d.unchecked["gpu_limit"]
    quiet = diagnose(synth.laglab("smooth", fps_cap=58.0).cap)        # control: the job on the other GPU
    assert quiet.stats["bound"] == "capped" and quiet.lag_type == "none"


def test_a_shared_gpu_never_turns_a_cadence_into_vsync_judder():
    # A7: with the cap verdict gone, vsync_judder would have called the cadence's 1- and 2-refresh holds "VSync
    # judder"; a shared GPU hides whether VSync, a cap or the GPU sets that cadence
    d = diagnose(synth.laglab("smooth", gpu_other_pct=80.0).cap)
    assert d.stats["bound"] == "unknown" and d.lag_type == "none", d.headline
    assert "vsync_judder" not in causes(d) and "80% busy" in d.unchecked["vsync_judder"]


def test_pacing_counts_only_where_a_confident_pacing_finding_claims_it():
    from lagdoctor.model import LagEvent
    pac = [LagEvent(1, "pacing", 0.0, 29.0, 7.0, {})]
    for conf, want in (("medium", 29.0), ("low", 0.0)):
        f = Finding(cause="uneven_pacing", title="t", category="pacing", confidence=conf, score=0.5, summary="",
                    event_ids=[1])
        assert dg.lag_time(pac, [f], {}, {"verdict": "waiting"}, True)["pacing"] == want


def test_pacing_lag_time_leaves_out_the_stutters_and_fps_drops_inside_it():
    # A1: a pacing window around a hitch counted the hitch's own time twice, and window-sized pacing outweighed it
    from lagdoctor.model import LagEvent
    evs = [LagEvent(1, "pacing", 0.0, 10.0, 7.0, {}), LagEvent(2, "stutter", 2.0, 2.5, 450.0, {}),
           LagEvent(3, "fps_drop", 5.0, 7.0, 30.0, {}), LagEvent(4, "stutter", 9.5, 11.0, 300.0, {}),
           LagEvent(5, "pacing", 20.0, 24.0, 7.0, {})]          # after every stutter: kept whole
    f = Finding(cause="uneven_pacing", title="t", category="pacing", confidence="medium", score=0.5, summary="",
                event_ids=[1, 5])
    lag = dg.lag_time(evs, [f], {}, {"verdict": "capped"}, True)
    assert lag["pacing"] == pytest.approx(10.0 - 0.5 - 2.0 - 0.5 + 4.0)
    assert lag["stutter"] == pytest.approx(2.0) and lag["low_fps"] == pytest.approx(2.0)
    assert dg._minus([(5.0, 10.0)], [(1.0, 2.0), (12.0, 13.0)]) == [(5.0, 10.0)]       # cuts outside it
    assert dg._minus([(5.0, 10.0)], [(4.0, 6.0), (9.0, 11.0)]) == [(6.0, 9.0)]         # cuts over its ends


def test_dropped_frame_judder_counts_as_pacing_whichever_rule_claims_it():
    # A11: frames made faster than a fixed refresh are never shown and the picture jerks. With a cap above the
    # refresh the claim is frame_cap's ("setup"), and before this the verdict said no lag stood out
    from lagdoctor.model import LagEvent
    pac = [LagEvent(1, "pacing", 0.0, 29.0, 6.7, {"criterion": "anim"})]
    cap_finding = Finding(cause="frame_cap", title="t", category="setup", confidence="medium", score=0.5, summary="",
                          event_ids=[1])
    assert dg.lag_time(pac, [cap_finding], {}, {"verdict": "capped"}, True, dropped={1})["pacing"] == 29.0
    assert dg.lag_time(pac, [cap_finding], {}, {"verdict": "capped"}, True)["pacing"] == 0.0   # a cap's own judder
    d = diagnose(synth.laglab("smooth", fps_cap=86.0, refresh_hz=60.0).cap)
    assert d.lag_type == "pacing" and d.stats["bound"] == "capped"
    assert d.headline.startswith("Uneven frame pacing over ") and "possibly a frame-rate cap" in d.headline
    assert not [e for e in d.events if e.kind == "fps_drop"]


# --------------------------------------------------------------------- severe hitches (A1) ---

def gpu_bound_with_freezes(freeze_ms: float, n: int = 8, dur: float = 60.0, ft: float = 19.6):
    """A GPU-bound game at ~51 FPS on the 144 Hz panel, with n frames of freeze_ms spread over the capture."""
    import random
    rng = random.Random(7)
    fms, gms, t = [], [], 1.0
    at = [5.0 + i * (dur - 10) / n for i in range(n)]
    while t < dur - 0.5:
        if at and t >= at[0]:
            at.pop(0)
            f, g = freeze_ms, 3.0
        else:
            f = ft * rng.uniform(0.97, 1.03)
            g = f * 0.96
        fms.append(f)
        gms.append(g)
        t += f / 1000.0
    fr = synth.frames_table(fms, gpu_ms=gms, display_ms=fms, displayed=1, mode="Hardware: Independent Flip",
                            runtime="DXGI")
    spans = [(fr[F.T][i] - f / 1000, fr[F.T][i]) for i, f in enumerate(fms) if f >= 250]

    def thread(x):
        return 100.0 if any(a - 0.1 <= x <= b + 0.1 for a, b in spans) else 55.0 + rng.uniform(-3, 3)

    cap = synth.capture(fr, synth.system_table(dur, game_top_thread_pct=thread, game_cpu_pct=lambda x: thread(x) + 60,
                                               game_gpu_pct=96.0, cpu_total_pct=20.0, game_focused=1),
                        hz=synth.REFRESH_HZ, duration=dur)
    cap.sources = synth.sources(("frames", "system"))
    return cap


def pacing_with_hitches():
    """30 s of uneven 12 / 20 ms frames on 60 Hz, with five hitches of 150 to 450 ms."""
    ms, t, i = [], 0.0, 0
    at, sizes = [5.0, 11.0, 17.0, 22.0, 27.0], [150.0, 300.0, 450.0, 200.0, 350.0]
    while t < 30.0:
        if at and t >= at[0]:
            at.pop(0)
            f = sizes.pop(0)
        else:
            f = 12.0 if i % 2 == 0 else 20.0
            i += 1
        ms.append(f)
        t += f / 1000.0
    cap = synth.capture(synth.frames_table(ms, t0=0.2, gpu_ms=5.0, frame_type="Application", displayed=1),
                        duration=30.5, hz=60.0)
    cap.sources = synth.sources(("frames",))
    return cap


@pytest.mark.parametrize("make, lag, named", [
    (lambda: gpu_bound_with_freezes(600.0), "low_fps", "; also 8 freezes, worst 600 ms."),
    (lambda: gpu_bound_with_freezes(300.0), "low_fps", "; also 8 severe hitches, worst 300 ms."),
    (pacing_with_hitches, "pacing", "; also 5 severe hitches, worst 450 ms.")])
def test_severe_hitches_and_freezes_are_named_whatever_the_lag_type(make, lag, named):
    # A1 (LC 2.3: spikes >= 225 ms are never acceptable): the headline once named only the frame rate or the pacing
    d = diagnose(make())
    assert d.lag_type == lag and d.headline.endswith(named), d.headline


def test_the_severe_hitch_clause_counts_from_the_stats_or_else_the_events():
    from lagdoctor.model import LagEvent
    evs = [LagEvent(1, "stutter", 1.0, 1.2, 180.0, {"class": "severe"}),
           LagEvent(2, "stutter", 5.0, 5.7, 640.0, {"class": "freeze"}),
           LagEvent(3, "stutter", 9.0, 9.1, 70.0, {"class": "hitch"})]
    assert dg._severe({"severe_hitches": 3, "freezes": 1}, evs) == "2 severe hitches and 1 freeze, worst 640 ms"
    assert dg._severe({}, evs) == "1 severe hitch and 1 freeze, worst 640 ms"           # no stats: the events
    assert dg._severe({"severe_hitches": 1, "freezes": 0, "worst_frame_ms": 160.0}, []) == "1 severe hitch, worst 160 ms"
    assert dg._severe({"severe_hitches": 0, "freezes": 0}, evs[2:]) is None              # a 70 ms hitch: not severe
    env = rules._Env(synth.laglab("hitch").cap, {"stats": {"frames": 3000}})
    stutter = dg.headline("stutter", [], {"stutters_per_min": 2.0, "severe_hitches": 1, "freezes": 1}, evs[1:2],
                          {"stutter": 0.7}, env)
    assert "also" not in stutter                                     # the stutter part names them already


def test_gold_hitches_inside_uneven_pacing_are_named(presentmon_exe, gold_etl_dir):
    # A1 on PresentMon's own output: test_case_4 (dwm.exe, 60 Hz) holds two ~420 ms hitches inside pacing that
    # covers the whole replay; the headline once read only "Uneven frame pacing over 100% of the capture"
    import time
    from lagdoctor.capture import presentmon as pm
    src = pm.PresentMonSource(10376, exe=presentmon_exe, etl_file=gold_etl_dir / "test_case_4.etl")
    src.start(0.0)
    deadline = time.monotonic() + 60.0
    while src._thread is not None and src._thread.is_alive():
        assert time.monotonic() < deadline, "the replay did not finish in 60 s"
        time.sleep(0.1)
    src.stop()
    assert src.status().state == "ok", src.status()
    cols = {k: list(v) for k, v in src.result()["columns"].items()}
    t0 = min(x for x in cols[F.T] if isinstance(x, float))
    cols[F.T] = [x - t0 if isinstance(x, float) else x for x in cols[F.T]]
    cap = Capture(started_at="2026-11-04T00:00:00+00:00", clock_origin=0.0,
                  duration_s=max(x for x in cols[F.T] if isinstance(x, float)), target={"pid": 10376},
                  sources={"frames": SourceStatus("ok")}, frames=cols,
                  inventory={"cpu": {"threads": 16}, "settings": {"hags": None},
                             "displays": [{"device": r"\\.\DISPLAY1", "primary": True, "hz": 60.0}]})
    d = diagnose(cap)
    assert sorted(round(e.severity) for e in d.events if e.kind == "stutter") == [416, 434]
    assert "worst 434 ms" in d.headline, d.headline


# --------------------------------------------------------------------- low FPS, waiting (A6) ---

def waiting_game(fps: float = 45.0, thread: float = 75.0):
    """A steady game under the refresh with neither side at its limit: r = 0.23 and its busiest thread at
    `thread` % (LC 3.2's grey zone at 75), the process using 2.6 CPUs, on the 144 Hz panel."""
    import random
    rng = random.Random(3)
    ft = 1000.0 / fps
    fms = [ft * rng.uniform(0.93, 1.07) for _ in range(int(38.5 / (ft / 1000.0)))]
    fr = synth.frames_table(fms, gpu_ms=[0.23 * f for f in fms], display_ms=fms, displayed=1,
                            mode="Hardware: Independent Flip", runtime="DXGI")
    sysb = synth.system_table(40.0, game_top_thread_pct=lambda t: thread + rng.uniform(-4, 4), game_cpu_pct=260.0,
                              game_gpu_pct=25.0, cpu_total_pct=25.0, game_focused=1)
    cap = synth.capture(fr, sysb, hz=synth.REFRESH_HZ, duration=40.0)
    cap.sources = synth.sources(("frames", "system"))
    return cap


@pytest.mark.parametrize("thread, said", [
    (55.0, "neither the CPU nor the GPU is at its limit"),                          # LC 3.2's "Neither" (< 0.6)
    (75.0, "the GPU is not at its limit and the CPU may be close to it, its busiest thread at 7")])   # grey zone
def test_a_low_frame_rate_while_waiting_is_low_fps_and_says_neither_side_is_the_limit(thread, said):
    # A6: a steady 45 FPS on 144 Hz with r = 0.23 got "No lag stood out" and CPU and GPU "checked and fine"
    d = diagnose(waiting_game(thread=thread))
    assert d.stats["bound"] == "waiting" and d.lag_type == "low_fps"
    assert d.headline.startswith(f"Low frame rate (45 FPS average on a 144 Hz screen; {said}"), d.headline
    assert "No lag" not in d.headline
    # the headline and the lists agree: the grey zone keeps the CPU open, under 0.6 it is checked and fine
    assert ("cpu_limit" in d.cleared) == (thread < 100 * rules.THREAD_WAITING) and "gpu_limit" in d.cleared
    assert not diagnose(waiting_game(fps=120.0)).lag_type == "low_fps"              # 120 of 144: no low FPS


# ------------------------------------------------------------------------- no hitches (A4) ---

def test_the_rule_of_three_is_never_claimed_next_to_a_50_ms_stutter():
    # A4: a stall's freeze event next to frame stats that missed it read "no hitches >= 50 ms in N frames"
    from lagdoctor.model import LagEvent
    env = rules._Env(synth.laglab("hitch").cap, {"stats": {"frames": 3000}})
    stats = {"hitches": 0, "frames": 3000, "hitch_rate_bound": 0.001, "avg_fps": 40.0, "refresh_hz": 144.0}
    stall = [LagEvent(1, "stutter", 5.0, 10.0, 5010.0, {"class": "freeze", "stall": True})]
    assert "no hitches" not in dg.headline("low_fps", [], stats, stall, {"low_fps": 20.0}, env)
    short = [LagEvent(1, "stutter", 5.0, 5.03, 30.0, {"class": "stutter"})]       # under 50 ms: the bound stands
    assert "no hitches ≥ 50 ms in 3,000 frames" in dg.headline("low_fps", [], stats, short, {"low_fps": 20.0}, env)


# ---------------------------------------------------------------------------- network (A8) ---

def net_event(i, event_kind, t0, t1, **detail):
    from lagdoctor.model import LagEvent
    return LagEvent(i, event_kind, t0, t1, 50.0, detail)


@pytest.mark.parametrize("dur, span, want", [(120.0, 1.19, 0.0), (120.0, 1.21, 1.21),    # 1 % of 120 s = 1.2 s
                                             (50.0, 0.99, 0.0), (50.0, 1.01, 1.01)])     # at least 1 s
def test_one_network_event_names_network_lag_only_past_one_percent_or_a_second(dur, span, want):
    lag = dg.lag_time([net_event(1, "net_spike", 60.0, 60.0 + span, target="internet")], [],
                      {"duration_s": dur}, {}, True)
    assert lag["network"] == pytest.approx(want)


def test_two_network_events_are_network_lag_however_short():
    evs = [net_event(1, "net_spike", 10.0, 10.5, target="internet"), net_event(2, "net_gap", 50.0, 50.3, kind="delay")]
    assert dg.lag_time(evs, [], {"duration_s": 120.0}, {}, True)["network"] == pytest.approx(0.8)


def test_a_router_that_ignores_pings_and_a_game_that_paused_are_no_network_lag():
    # A8, NE 7 row 9: lost router pings with the internet answering are the router ignoring ICMP; NE 4.6: a "local"
    # gap is the game's own sending paused (a hitch the frames show), not the network
    gw = [net_event(1, "net_loss", 30.0, 32.0, target="gateway"), net_event(2, "net_loss", 70.0, 72.0, target="gateway")]
    local = [net_event(3, "net_gap", 40.0, 43.0, kind="local"), net_event(4, "net_gap", 90.0, 92.0, kind="local")]
    assert dg.lag_time(gw + local, [], {"duration_s": 120.0}, {}, True)["network"] == 0.0
    both = gw[:1] + [net_event(5, "net_loss", 30.5, 31.5, target="internet")]       # the internet lost them too
    assert dg.lag_time(both, [], {"duration_s": 120.0}, {}, True)["network"] == pytest.approx(2.0)
    assert {e.id for e in dg.network_events(both)} == {1, 5}
    stalled = gw[:1] + [net_event(6, "net_gap", 30.5, 31.5, kind="local")]          # the PC stalled: no corroboration
    assert dg.network_events(stalled) == []


@pytest.mark.parametrize("label, kw, lag", [
    ("one slow ping, two rounds", dict(net_spikes=[(60.0, 60.9, 36.0)]), "none"),
    ("four lost rounds in a row", dict(net_losses=[60.2, 60.7, 61.2, 61.7]), "network"),
    ("the game paused its own traffic", dict(server_gaps=[(40.0, 3000.0, "local")]), "none"),
    ("the server stopped answering", dict(server_gaps=[(100.0, 20000.0, "open")]), "network"),
    ("two late bursts", dict(server_gaps=[(40.0, 300.0, "delay"), (80.0, 300.0, "delay")]), "network")])
def test_network_lag_needs_more_than_one_short_event(label, kw, lag):
    d = diagnose(synth.laglab("smooth", duration_s=120.0, **kw).cap)
    assert d.lag_type == lag, (label, d.headline)


def test_a_router_ignoring_pings_in_a_real_capture_is_no_network_lag():
    s = synth.laglab("smooth", duration_s=120.0)
    t = s.cap.network[N.T]
    for m in (60.2, 60.7, 61.2):                  # three router pings in a row unanswered, the internet answering
        k = min(range(len(t)), key=lambda i: abs(t[i] - m))
        s.cap.network[N.GW_RTT_MS][k] = None
        s.cap.net_flows["lost"]["gateway"].append(t[k])
    s.cap.net_flows["probes"]["gateway"]["lost"] = 3
    d = diagnose(s.cap)
    assert [e.detail["target"] for e in d.events if e.kind == "net_loss"] == ["gateway"]    # the event exists ...
    assert d.lag_type == "none"                                                           # ... and is no lag


def test_a_lost_connection_is_said_with_its_time():
    d = diagnose(synth.laglab("smooth", duration_s=120.0, server_gaps=[(100.0, 20000.0, "open")]).cap)
    assert d.headline.startswith("Network lag (the game server stopped answering at 1m 40s)")


# ------------------------------------------------------------------------------ marks (CC-5) ---

def test_the_marks_reach_the_diagnosis_with_what_matched_them():
    # LagLab's hitch mode hitches every 3 s from 3.4 s; a press covers the 3 s before it and 0.5 s after (LC 2.4)
    s = synth.laglab("hitch", marks=(2.5, 3.9, 12.0, 20.0))
    d = diagnose(s.cap)
    marks = d.stats["marks"]
    assert [m["t"] for m in marks] == [2.5, 3.9, 12.0, 20.0]
    kinds = [{next(e.kind for e in d.events if e.id == i) for i in m["events"]} for m in marks]
    assert kinds == [set(), {"stutter"}, {"stutter"}, {"stutter"}]          # 2.5 s: before the first hitch
    json.dumps(d.to_json(), allow_nan=False)


def test_a_mark_is_not_matched_by_a_router_that_ignored_pings():
    # NE 7 row 9: router-only loss is no lag, so it cannot explain the moment the user marked either
    from lagdoctor.model import LagEvent
    mk = LagEvent(3, "mark", 27.0, 30.5, 0.0, {"mark_t": 30.0, "frame_events": [], "net_events": [1]})
    gw = net_event(1, "net_loss", 29.0, 30.0, target="gateway")
    assert dg.mark_list([gw, mk]) == [{"t": 30.0, "events": [], "causes": []}]
    inet = net_event(2, "net_loss", 29.2, 29.8, target="internet")          # the internet lost them too
    mk.detail["net_events"] = [1, 2]
    assert dg.mark_list([gw, inet, mk])[0]["events"] == [1, 2]


def test_a_mark_keeps_every_signal_that_moved():
    # Item 6: a mark kept its first two causes, which CAUSE_ORDER picked where z tied at Z_CAP, so the first real
    # capture's mark at 13:27 dropped other programs at 602 % of a CPU (and the next one the CPU at 56 % of base)
    from lagdoctor.model import LagEvent
    moved = ["not_focused", "asset_streaming", "vram_overflow", "background_cpu", "asset_streaming"]
    mk = LagEvent(3, "mark", 804.1, 807.6, 0.0, {"mark_t": 807.1, "frame_events": [], "net_events": []}, causes=moved)
    assert dg.mark_list([mk]) == [{"t": 807.1, "events": [],
                                   "causes": ["not_focused", "asset_streaming", "vram_overflow", "background_cpu"]}]


def test_marks_are_reported_also_without_frame_timing():
    from lagdoctor.report import console, html
    s = synth.laglab("smooth", duration_s=40.0, marks=(8.0, 15.0, 22.0, 30.0), with_frames=False)
    d = diagnose(s.cap)
    assert [m["t"] for m in d.stats["marks"]] == [8.0, 15.0, 22.0, 30.0]
    text = console.render(d, s.cap)
    assert "You marked 4 moments: 4 showed nothing in what was measured. Frame timing was not measured" in \
        " ".join(text.split())
    assert "8s, 15s, 22s, 30s" in text
    page = html.render(d, s.cap)
    assert "<h2>Moments you marked</h2>" in page and page.count("Nothing in what was measured") == 4
    assert page.count('class="vl mark"') == 8          # on the CPU/GPU chart and the ping chart (no frame chart)


def test_marks_with_frames_say_which_matched_a_stutter():
    from lagdoctor.report import console, html
    s = synth.laglab("hitch", marks=(2.5, 3.9))
    d = diagnose(s.cap)
    text = " ".join(console.render(d, s.cap).split())
    assert ("You marked 2 moments: 1 matched a stutter, FPS drop or network event; 1 showed nothing in what was "
            "measured. No stutter or FPS drop was measured at those moments.") in text
    assert "- 4s: Stutter (" in text and "- nothing at 2s" in text
    page = html.render(d, s.cap)
    assert page.count('class="vl mark"') == 6          # frame, CPU/GPU and ping charts


def test_ranking_follows_the_score():
    fs = [Finding(cause=c, title=c, category="fps", confidence="low", score=sc, summary="")
          for c, sc in (("a", 0.2), ("b", 0.9), ("c", 0.5))]
    assert [f.cause for f in dg.rank(fs)] == ["b", "c", "a"]
