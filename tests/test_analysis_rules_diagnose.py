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
from lagdoctor.model import F, S, Capture, Diagnosis, Finding, SourceStatus  # noqa: E402
from tests import synth  # noqa: E402


def discrete(cap) -> bool:
    """scripts/e2e.py's own test for a discrete GPU in the inventory."""
    return any(a.get("integrated") is False and not a.get("software") for a in cap.inventory.get("adapters") or [])


def causes(d: Diagnosis) -> set[str]:
    return {f.cause for f in d.findings}


# ----------------------------------------------------------------------------- acceptance ---

@pytest.mark.parametrize("seed", [1, 2])
@pytest.mark.parametrize("mode", synth.MODES)
def test_every_laglab_scenario_passes_the_end_to_end_judge(mode, seed):
    s = synth.laglab(mode, seed=seed)
    d = diagnose(s.cap)
    assert discrete(s.cap) and "intel" in s.log["gl_renderer"].lower()     # so the judge demands wrong_gpu too
    v = judge(mode, s.log, s.cap.clock_origin, d, has_discrete_gpu=discrete(s.cap))
    assert v.ok, [(c.name, c.detail) for c in v.checks if not c.ok]
    assert not [w for w in d.unchecked.values() if w.startswith("the check failed")]


@pytest.mark.parametrize("mode, lag, lead", [("smooth", "none", None), ("gpu", "low_fps", "wrong_gpu"),
                                             ("cpu", "low_fps", "cpu_limit"), ("hitch", "stutter", "game_cpu_spike"),
                                             ("hitch-io", "stutter", "asset_streaming"),
                                             ("hitch-wait", "stutter", "game_stall"), ("hog", "mixed", "background_hogs"),
                                             ("pace", "pacing", "uneven_pacing")])
def test_lag_type_and_the_finding_the_headline_names(mode, lag, lead):
    d = diagnose(synth.laglab(mode).cap)
    assert d.lag_type == lag
    if lead:
        assert d.findings[0].cause == lead and d.findings[0].confidence == "high"
        assert dg.labels.cause_text(lead) in d.headline or lead == "uneven_pacing"


def test_headlines_carry_the_numbers_and_the_rule_of_three():
    gpu = diagnose(synth.laglab("gpu").cap).headline
    assert gpu.startswith("Low frame rate (40 FPS average on a 144 Hz screen), most likely running on the wrong GPU")
    assert "no hitches ≥ 50 ms in" in gpu and "at 95% confidence" in gpu
    hitch = diagnose(synth.laglab("hitch").cap).headline
    assert hitch.startswith("9 stutters (18.0 a minute, worst ") and "no hitches" not in hitch
    smooth = diagnose(synth.laglab("smooth").cap).headline
    assert smooth.startswith("No lag stood out: no hitches ≥ 50 ms in ")
    pace = diagnose(synth.laglab("pace").cap).headline
    assert pace.startswith("Uneven frame pacing over ") and "uneven intervals" in pace


# ------------------------------------------------------------------------------- negatives ---

@pytest.mark.parametrize("seed", [1, 2, 3])
def test_the_smooth_control_gets_no_limit_pacing_or_stutter_finding(seed):
    d = diagnose(synth.laglab("smooth", seed=seed).cap)
    assert not causes(d) & {"gpu_limit", "cpu_limit", "uneven_pacing", "vsync_judder"}
    assert not [f.cause for f in d.findings if f.category in ("stutter", "pacing")]
    assert d.lag_type == "none" and d.stats["stutters"] == 0 and d.stats["hitches"] == 0
    assert {"gpu_limit", "cpu_limit", "uneven_pacing"} <= set(d.cleared)       # checked, with what was measured


FRAME_RULES = ("cpu_limit", "gpu_limit", "vsync_judder", "uneven_pacing", "frame_cap", "windowed_composition",
               "shader_compile", "background_hogs", "game_cpu_spike", "asset_streaming", "driver_dpc", "gpu_spike",
               "game_stall")


def test_without_frame_timing_frame_rules_are_unchecked_and_nothing_claims_no_stutter():
    s = synth.laglab("hitch", with_frames=False)
    why = s.cap.sources["frames"].detail
    d = diagnose(s.cap)
    for cause in FRAME_RULES:
        assert cause in d.unchecked, cause
        assert why in d.unchecked[cause], (cause, d.unchecked[cause])
    assert not set(FRAME_RULES) & (set(d.cleared) | causes(d))
    assert not [f for f in d.findings if f.category in ("stutter", "pacing")]
    for key in ("hitches", "severe_hitches", "freezes", "stutters", "stutters_per_min", "hitch_rate_bound",
                "avg_fps", "low1_fps", "worst_frame_ms", "time_beyond_50_ms"):
        assert d.stats[key] is None, key
    assert d.stats["frames"] == 0 and d.lag_type == "unknown"
    text = d.headline.lower()
    assert "no hitch" not in text and "no lag" not in text and "no stutter" not in text
    assert d.headline.startswith("Frame timing was not measured (" + why)
    assert d.measured["frames"] == why


def test_frame_rows_from_a_source_that_failed_never_back_a_no_hitch_claim():
    cap = synth.laglab("smooth").cap                         # rows are there, but the source says it broke
    cap.sources["frames"] = SourceStatus("failed", "PresentMon stopped after 4 s")
    d = diagnose(cap)
    assert "no hitch" not in d.headline.lower() and "no lag" not in d.headline.lower()
    assert d.lag_type == "unknown" and d.stats["stutters"] is None
    assert "PresentMon stopped after 4 s" in d.unchecked["uneven_pacing"]


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


# ------------------------------------------------------------------------------ the fixes ---

# DESIGN.md "Fixes": each fix id and its exact params; the only cause that may offer each.
FIX_PARAMS = {"power_plan": {"scheme": str}, "refresh_rate": {"device": str, "hz": int},
              "gpu_preference": {"exe": str, "preference": int}, "game_dvr_off": {}, "game_mode_on": {},
              "windowed_optimizations_on": {}, "background_priority": {"pid": int, "create_time": int, "class": str}}
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
    cap.others = [[t, [list(p) + [134000000000000000 + p[0]] for p in procs]] for t, procs in cap.others]
    return cap


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


@pytest.mark.parametrize("mode", synth.MODES)
def test_advice_only_causes_never_carry_a_fix_id(mode):
    for f in diagnose(synth.laglab(mode).cap).findings:
        ids = {fx.fix_id for fx in f.fixes if fx.fix_id}
        assert ids <= {FIX_OF_CAUSE[f.cause]} if f.cause in FIX_OF_CAUSE else not ids, (f.cause, ids)


# ------------------------------------------------------------------- stats, measured, JSON ---

STATS_KEYS = ("frames duration_s avg_fps low1_fps low01_fps p1_fps median_frame_ms p99_frame_ms worst_frame_ms "
              "stutters stutters_per_min hitches severe_hitches freezes time_beyond_50_ms hitch_rate_bound "
              "dropped_pct bound bound_r gpu_busy_pct refresh_hz present_mode runtime swap_chains main_chain "
              "ping_gw_ms ping_inet_ms ping_server_ms jitter_ms loss_pct").split()       # DESIGN, Diagnosis.stats


def test_the_stats_hold_every_key_design_lists():
    d = diagnose(synth.laglab("hitch").cap)
    assert set(STATS_KEYS) <= set(d.stats)
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
    assert "recorded no frames" in d.unchecked["uneven_pacing"]


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


# --------------------------------------------------------------------- lag type and lag time ---

@pytest.mark.parametrize("lag, frames_ok, want", [
    ({"stutter": 2.0, "low_fps": 1.0}, True, "mixed"), ({"stutter": 2.0, "low_fps": 0.99}, True, "stutter"),
    ({"network": 3.0}, False, "network"), ({}, True, "none"), ({}, False, "unknown"),
    ({"stutter": 0.0, "pacing": 0.0}, True, "none")])
def test_lag_type_is_the_category_with_the_most_lag_time(lag, frames_ok, want):
    assert dg.pick_lag_type(lag, frames_ok) == want


@pytest.mark.parametrize("verdict, fps, hz, steady", [("gpu", 129.5, 144.0, 25.0), ("gpu", 129.7, 144.0, 0.0),
                                                      ("cpu_main", 59.9, None, 25.0), ("cpu_main", 60.1, None, 0.0),
                                                      ("capped", 30.0, 144.0, 0.0), ("waiting", 30.0, 144.0, 0.0)])
def test_a_frame_rate_the_hardware_holds_under_the_refresh_counts_as_low_fps_time(verdict, fps, hz, steady):
    lag = dg.lag_time([], [], {"avg_fps": fps}, {"verdict": verdict, "refresh_hz": hz, "steady_s": 25.0}, True)
    assert lag["low_fps"] == steady


def test_pacing_counts_only_where_a_confident_pacing_finding_claims_it():
    from lagdoctor.model import LagEvent
    pac = [LagEvent(1, "pacing", 0.0, 29.0, 7.0, {})]
    for conf, want in (("medium", 29.0), ("low", 0.0)):
        f = Finding(cause="uneven_pacing", title="t", category="pacing", confidence=conf, score=0.5, summary="",
                    event_ids=[1])
        assert dg.lag_time(pac, [f], {}, {"verdict": "waiting"}, True)["pacing"] == want


def test_ranking_follows_the_score():
    fs = [Finding(cause=c, title=c, category="fps", confidence="low", score=sc, summary="")
          for c, sc in (("a", 0.2), ("b", 0.9), ("c", 0.5))]
    assert [f.cause for f in dg.rank(fs)] == ["b", "c", "a"]
