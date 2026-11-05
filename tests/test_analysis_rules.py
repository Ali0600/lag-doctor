"""Session rules and LC 5 scoring on hand-built captures: every threshold pinned at points just either side.

Each rule runs through rules.run_rule (sources first, then the rule, then its gate) with a literal ctx, so a
test controls exactly what the frames analysis would have said. The diagnosis end to end is in
test_analysis_rules_diagnose.py; the network rules are in test_analysis_rules_network.py.
"""
from __future__ import annotations

import copy

import pytest

from lagdoctor.analysis import rules
from lagdoctor.model import F, S, Finding, LagEvent, SourceStatus
from tests import synth

NV, INTEL = synth.NVIDIA_LUID, synth.INTEL_LUID
ADAPTERS = [{"luid": NV, "name": "NVIDIA GeForce RTX 3070 Laptop GPU", "vendor": "nvidia", "vram_mb": 8192,
             "integrated": False, "software": False, "hybrid": "discrete"},
            {"luid": INTEL, "name": "Intel(R) UHD Graphics", "vendor": "intel", "vram_mb": 128,
             "integrated": True, "software": False, "hybrid": "integrated"}]
DISPLAY = {"device": r"\\.\DISPLAY1", "name": "Internal panel", "adapter_luid": INTEL, "internal": True,
           "primary": True, "width": 1920, "height": 1080, "hz": 144.0, "max_hz": 144, "modes_hz": [60, 144]}
SETTINGS = {"game_mode": True, "game_dvr": False, "background_recording": False, "hags": False,
            "windowed_optimizations": True, "vrr": None, "memory_integrity": True}
GAME = {"exe": r"C:\Games\game.exe", "drive": "C:", "media": "SSD", "free_gb": 200.0, "gpu_preference": None,
        "monitor_device": r"\\.\DISPLAY1", "launcher": None}
NO_FRAMES = SourceStatus("unavailable", "PresentMon needs admin rights or Performance Log Users: access denied")


def make(*, ok=("frames", "system"), src=None, system=None, gpu=None, adapter=INTEL, inventory=None,
         duration=30.0, others=None, target=None):
    """A capture whose sources in `ok` worked (src: other SourceStatus by name) on this laptop's hardware."""
    cap = synth.capture(system=system, duration=duration)
    cap.sources = synth.sources(ok, **(src or {}))
    cap.target = {"pid": 4242, "name": "game.exe", "exe": r"C:\Games\game.exe", "adapter_luid": adapter,
                  "pids": [4242], **(target or {})}
    inv = {"cpu": {"threads": 16}, "ram_gb": 16.0, "adapters": copy.deepcopy(ADAPTERS), "displays": [dict(DISPLAY)],
           "settings": dict(SETTINGS), "game": dict(GAME)}
    inv.update(inventory or {})
    cap.inventory = inv
    if gpu is not None:
        cap.gpu = gpu
    if others is not None:
        cap.others = others
    return cap


def run(cause, cap, *, stats=None, bound=None, events=(), causes=None, frames=None):
    st = {"frames": 3000, "avg_fps": 60.0, "duration_s": cap.duration_s}
    st.update(stats or {})
    b = {"verdict": "unknown", "notes": [], "refresh_hz": 144.0}
    b.update(bound or {})
    ctx = {"frames": frames or {}, "stats": st, "bound": b, "events": list(events), "event_causes": causes or {}}
    return rules.run_rule(rules.RULE_BY_CAUSE[cause], cap, ctx)


def stutter(i, t, cause=None, side="cpu", **detail):
    return LagEvent(id=i, kind="stutter", t_start=t, t_end=t + 0.15, severity=150.0,
                    detail={"side": side, "why": ["side line", f"signal line {i}"], **detail},
                    causes=[cause] if cause else [])


def lift(label, a=9, n=9, weight=3.0, lift_=20.0, **kw):
    return {"a": a, "n": n, "share": a / n if n else 0.0, "lift": lift_, "label": label, "b": 0.0,
            "weight": weight, **kw}


def is_finding(res, conf=None):
    return isinstance(res, Finding) and (conf is None or res.confidence == conf)


# ----------------------------------------------------------------------------- scoring ---

def case(cause="gpu_limit"):
    return rules._Case(synth.capture(), {}, cause)


@pytest.mark.parametrize("w, conf", [(4.0, "high"), (3.99, "medium"), (2.0, "medium"), (1.99, "low"), (0.5, "low")])
def test_labels_follow_lc_5_3_at_their_edges(w, conf):
    c = case()
    c.support("frame_timing", w, "x")
    assert is_finding(c.conclude("t", "s"), conf)


def test_minus_two_is_checked_and_fine_and_less_is_silence():
    c = case()
    c.against("frame_timing", 2.0, "measured and fine")
    res = c.conclude()
    assert isinstance(res, rules.Cleared) and res.text == "Measured and fine."
    c = case()
    c.against("frame_timing", 1.99, "x")
    assert c.conclude() is None
    c = case()
    c.neutral("frame_timing", "in range")
    assert c.conclude() is None


def test_only_the_strongest_item_per_family_counts():
    c = case()
    c.support("frame_timing", 2.0, "a")
    c.support("frame_timing", 3.0, "b")
    assert is_finding(c.conclude(), "medium")            # 3, not 5
    c = case()
    c.support("frame_timing", 2.0, "a")
    c.support("gpu_engine", 2.0, "b")
    assert is_finding(c.conclude(), "high")              # two families: 4


def test_a_contradiction_in_the_same_family_subtracts():
    c = case()
    c.support("frame_timing", 3.0, "a")
    c.against("frame_timing", 2.0, "b")
    assert is_finding(c.conclude(), "low")


def test_static_configuration_adds_at_most_two_and_never_makes_likely_alone():
    c = rules._Case(synth.capture(), {}, "power_plan")
    c.support("static", 4.0, "a quiet power plan")
    assert is_finding(c.conclude(), "medium")
    c = rules._Case(synth.capture(), {}, "power_plan")
    c.support("static", 4.0, "a quiet power plan")
    c.support("cpu_limits", 2.0, "and the CPU was held down")
    assert is_finding(c.conclude(), "high")


def test_likely_needs_the_key_family_supporting():
    c = case()                                             # key family: frame_timing
    c.support("gpu_engine", 2.0, "a")
    c.support("gpu_telemetry", 2.0, "b")
    assert is_finding(c.conclude(), "medium")


def test_a_key_family_that_was_not_measured_is_unchecked_with_the_reason():
    c = rules._Case(synth.capture(), {}, "cpu_limit")     # key family: cpu_threads, no stand-in
    c.support("frame_timing", 4.0, "x")
    c.lack("cpu_threads", 2.0, "the game's thread CPU use was not measured")
    res = c.conclude()
    assert isinstance(res, rules.Unchecked) and res.text == "the game's thread CPU use was not measured"


def test_a_stand_in_family_counts_only_to_support():
    c = case()                                             # gpu_limit: gpu_engine may stand in (LC 4.1)
    c.support("gpu_engine", 2.0, "3D engine 98% busy")
    c.lack("frame_timing", 2.0, "no frame timing")
    assert is_finding(c.conclude(), "medium")
    c = case()
    c.against("gpu_engine", 2.0, "3D engine 5% busy")
    c.lack("frame_timing", 2.0, "no frame timing")
    res = c.conclude()
    assert isinstance(res, rules.Unchecked) and res.text == "no frame timing"


@pytest.mark.parametrize("lost, conf", [(2.66, "high"), (2.67, "medium")])
def test_likely_needs_coverage_of_point_six(lost, conf):
    c = case()
    c.support("frame_timing", 4.0, "x")
    c.lack("gpu_engine", lost, "not measured")
    assert is_finding(c.conclude(), conf)


@pytest.mark.parametrize("lost, kind", [(2.0, Finding), (2.01, rules.Unchecked)])
def test_coverage_under_one_half_is_not_enough_data(lost, kind):
    c = case()
    c.support("frame_timing", 2.0, "x")
    c.lack("gpu_engine", lost, "the GPU engine counters were not read")
    res = c.conclude()
    assert isinstance(res, kind)
    if kind is rules.Unchecked:
        assert res.text.startswith("not enough data") and "GPU engine counters" in res.text


@pytest.mark.parametrize("lost, cleared", [(1.33, True), (1.34, False)])
def test_cleared_needs_coverage_of_point_six(lost, cleared):
    c = case()
    c.against("frame_timing", 2.0, "fine")
    c.lack("gpu_engine", lost, "not measured")
    assert isinstance(c.conclude(), rules.Cleared) is cleared


def test_score_orders_by_label_then_s_then_share_then_fix():
    sc = rules._score
    assert sc("high", 0.0, 0.0, False) > sc("medium", 20.0, 1.0, True) > sc("low", 20.0, 1.0, True)
    assert sc("medium", 3.0, 0.0, False) > sc("medium", 2.5, 1.0, True)
    assert sc("medium", 2.0, 0.5, False) > sc("medium", 2.0, 0.4, True)
    assert sc("medium", 2.0, 0.5, True) > sc("medium", 2.0, 0.5, False)
    assert 0.0 <= sc("low", 0.0, 0.0, False) and sc("high", 99.0, 5.0, True) <= 1.0


def test_sentence_keeps_a_process_name_lower_case():
    assert rules._sentence("python.exe rose") == "python.exe rose."
    assert rules._sentence("the GPU was busy") == "The GPU was busy."


# ------------------------------------------------------------------------- run_rule plumbing ---

def test_a_rule_whose_source_was_not_measured_is_unchecked_with_its_reason():
    cap = make(ok=("system",), src={"frames": NO_FRAMES})
    res = run("cpu_limit", cap, bound={"verdict": "cpu_main", "r": 0.1, "busiest_thread": 0.99})
    assert isinstance(res, rules.Unchecked)
    assert "frame timing" in res.text and "PresentMon needs admin rights" in res.text


def test_a_source_missing_from_the_capture_reads_not_recorded():
    cap = make(ok=())
    res = run("ram_pressure", cap)
    assert isinstance(res, rules.Unchecked) and "not recorded in this capture" in res.text


def test_a_source_that_ran_but_recorded_nothing_is_not_measured():
    cap = make(ok=("frames", "system"))                     # system "ok" but no rows
    res = run("ram_pressure", cap)
    assert isinstance(res, rules.Unchecked) and "recorded nothing" in res.text


def test_either_source_satisfies_an_or_need():
    cap = make(ok=("system",), src={"frames": NO_FRAMES}, system=synth.system_table(30, game_gpu_pct=99.0))
    res = run("gpu_limit", cap, stats={"frames": 0}, bound={"verdict": "gpu"})
    assert is_finding(res, "medium")                        # LC 3.4: "GPU-limited (probable)" from the engine alone


def test_a_rule_that_crashes_is_unchecked_and_says_so(monkeypatch):
    def boom(cap, ctx):
        raise ZeroDivisionError("x")
    rule = rules.Rule("gpu_limit", "frame_timing", (), None, boom)
    res = rules.run_rule(rule, make(), {})
    assert isinstance(res, rules.Unchecked) and res.text.startswith("the check failed (ZeroDivisionError")


@pytest.mark.parametrize("verdict, kind", [("cpu_main", rules.Cleared), ("cpu_all", rules.Cleared),
                                           ("capped", rules.Cleared), ("unknown", rules.Unchecked),
                                           ("gpu", Finding), ("mixed", Finding)])
def test_a_gpu_side_finding_passes_only_a_gpu_or_mixed_verdict(verdict, kind):
    def found(cap, ctx):
        return Finding(cause="gpu_throttle", title="t", category="fps", confidence="high", score=1.0, summary="")
    rule = rules.Rule("gpu_throttle", "gpu_telemetry", (), "gpu", found)
    res = rules.run_rule(rule, make(), {"bound": {"verdict": verdict, "notes": ["only 10 steady frames"]}})
    assert isinstance(res, kind)
    if kind is rules.Cleared:
        assert res.text.startswith("The GPU is not what limits the frame rate")


@pytest.mark.parametrize("verdict, kind", [("gpu", rules.Cleared), ("capped", rules.Cleared),
                                           ("cpu_main", Finding), ("unknown", Finding)])
def test_a_cpu_side_finding_passes_a_cpu_mixed_or_unknown_verdict(verdict, kind):
    def found(cap, ctx):
        return Finding(cause="cpu_limit", title="t", category="fps", confidence="high", score=1.0, summary="")
    rule = rules.Rule("cpu_limit", "cpu_threads", (), "cpu", found)
    assert isinstance(rules.run_rule(rule, make(), {"bound": {"verdict": verdict}}), kind)


def test_gates_and_side_lists_match_lc_5_1():
    assert rules.GATES["gpu"] == {"gpu", "mixed"}
    assert rules.GATES["cpu"] == {"cpu_main", "cpu_all", "mixed", "unknown"}
    gates = {r.cause: r.gate for r in rules.RULES}
    for cause in ("gpu_limit", "gpu_throttle", "vram_overflow", "hybrid_copy"):    # LC 4.1, 4.2, 4.3, 4.11b
        assert gates[cause] == "gpu", cause
    for cause in ("cpu_limit", "cpu_throttle"):                                     # LC 4.4, 4.5
        assert gates[cause] == "cpu", cause
    assert gates["gpu_spike"] == "gpu_event" and not {"cpu_main", "cpu_all"} & rules.GATES["gpu_event"]


DESIGN_RULES = ("wrong_gpu hybrid_copy gpu_limit cpu_limit gpu_throttle cpu_throttle vram_overflow ram_pressure "
                "power_plan refresh_rate vsync_judder uneven_pacing frame_cap windowed_composition game_dvr "
                "game_mode_off not_focused background_hogs hdd_streaming shader_compile wifi_weak wifi_roaming "
                "wifi_scan_spikes bufferbloat isp_latency server_lag packet_loss vpn_route bandwidth_hog").split()


def test_every_rule_in_design_is_registered_once_with_a_label():
    from lagdoctor.report import labels
    causes = [r.cause for r in rules.RULES]
    assert len(causes) == len(set(causes))
    assert set(DESIGN_RULES) <= set(causes)
    assert all(c in labels.CAUSES for c in causes)
    assert all(r.category in ("fps", "stutter", "pacing", "latency", "network", "setup") for r in rules.RULES)


# ------------------------------------------------------------------------------- wrong GPU ---

def test_wrong_gpu_on_the_integrated_gpu_while_it_limits_the_game_is_likely_with_the_fix():
    res = run("wrong_gpu", make(), stats={"avg_fps": 40.0}, bound={"verdict": "gpu", "r": 0.99})
    assert is_finding(res, "high")
    fix = next(f for f in res.fixes if f.fix_id)
    assert fix.fix_id == "gpu_preference" and fix.params == {"exe": r"C:\Games\game.exe", "preference": 2}
    assert fix.needs_restart == "game" and "community-documented" in fix.detail


def test_wrong_gpu_while_capped_is_still_reported_but_only_possible():
    res = run("wrong_gpu", make(), bound={"verdict": "capped"})
    assert is_finding(res, "medium")
    assert any("not by the GPU" in e.text for e in res.evidence)


@pytest.mark.parametrize("adapter, adapters, text", [
    (NV, None, "the discrete GPU"),
    (INTEL, [ADAPTERS[1]], "no discrete GPU")])
def test_wrong_gpu_is_ruled_out_on_the_discrete_gpu_or_with_no_discrete_one(adapter, adapters, text):
    inv = {"adapters": copy.deepcopy(adapters)} if adapters else None
    res = run("wrong_gpu", make(adapter=adapter, inventory=inv), bound={"verdict": "gpu"})
    assert isinstance(res, rules.Cleared) and text in res.text


def test_wrong_gpu_without_the_game_adapter_is_unchecked():
    cap = make(adapter=None, ok=("frames",), src={"system": SourceStatus("failed", "sampler did not run")})
    res = run("wrong_gpu", cap)
    assert isinstance(res, rules.Unchecked) and "sampler did not run" in res.text


def test_wrong_gpu_offers_advice_not_the_fix_when_the_preference_is_already_set():
    res = run("wrong_gpu", make(inventory={"game": dict(GAME, gpu_preference=2)}), bound={"verdict": "gpu"})
    assert is_finding(res) and not any(f.fix_id for f in res.fixes) and "ignore" in res.fixes[0].detail


@pytest.mark.parametrize("exe", ["game.exe", r"C:\Games\game.bat", r"C:\Games\..\Windows\evil.exe",
                                 "C:\\Games\\ga\x00me.exe", None, 42])
def test_wrong_gpu_never_offers_the_fix_for_a_path_that_is_not_a_plain_absolute_exe(exe):
    cap = make(target={"exe": exe}, inventory={"game": dict(GAME, exe=exe)})
    res = run("wrong_gpu", cap, bound={"verdict": "gpu"})
    assert is_finding(res) and not any(f.fix_id for f in res.fixes)


# ----------------------------------------------------------------------------- hybrid copy ---

def test_hybrid_copy_on_the_dgpu_with_the_screen_on_the_igpu():
    res = run("hybrid_copy", make(adapter=NV), bound={"verdict": "gpu"})
    assert is_finding(res, "medium") and "copied across" in res.evidence[0].text
    frames = synth.frames_table([10.0] * 20, gpu_ms=9.5)
    frames[F.HYBRID] = [1] * 20
    assert is_finding(run("hybrid_copy", make(adapter=NV), bound={"verdict": "gpu"}, frames=frames), "high")


def test_hybrid_copy_is_ruled_out_on_the_gpu_that_drives_the_screen_and_gated_off_the_gpu_side():
    assert isinstance(run("hybrid_copy", make(adapter=INTEL), bound={"verdict": "gpu"}), rules.Cleared)
    res = run("hybrid_copy", make(adapter=NV), bound={"verdict": "cpu_main"})
    assert isinstance(res, rules.Cleared) and "not what limits" in res.text


# ------------------------------------------------------------------------------- GPU limit ---

def engine(pct):
    return synth.system_table(30, game_gpu_pct=pct)


@pytest.mark.parametrize("fps, kind", [(129.5, Finding), (129.7, rules.Cleared)])
def test_gpu_limit_costs_nothing_once_the_game_reaches_ninety_percent_of_the_refresh(fps, kind):
    res = run("gpu_limit", make(system=engine(99.0)), stats={"avg_fps": fps},
              bound={"verdict": "gpu", "r": 0.98, "gpu_ms": 7.6, "ft_ms": 7.7})
    assert isinstance(res, kind)


@pytest.mark.parametrize("pct, conf", [(90.0, "high"), (89.9, "medium")])
def test_gpu_limit_is_likely_with_the_3d_engine_at_ninety_percent(pct, conf):
    res = run("gpu_limit", make(system=engine(pct)), stats={"avg_fps": 40.0},
              bound={"verdict": "gpu", "r": 0.99, "gpu_ms": 24.8, "ft_ms": 25.0})
    assert is_finding(res, conf)


@pytest.mark.parametrize("pct, kind", [(95.0, Finding), (94.9, rules.Unchecked)])
def test_gpu_limit_without_frame_timing_needs_the_engine_at_ninety_five_percent(pct, kind):
    cap = make(ok=("system",), src={"frames": NO_FRAMES}, system=engine(pct))
    res = run("gpu_limit", cap, stats={"frames": 0}, bound={"verdict": "gpu"})
    assert isinstance(res, kind)
    if kind is Finding:
        assert res.confidence == "medium" and "probably" in res.evidence[0].text


def test_gpu_limit_counts_nvidia_utilisation_at_ninety_five_percent():
    for util, conf in ((95.0, "high"), (94.9, "medium")):
        cap = make(adapter=NV, ok=("frames", "system", "gpu"), system=engine(70.0),
                   gpu=synth.gpu_table(30, nv_util_pct=util))
        res = run("gpu_limit", cap, stats={"avg_fps": 40.0}, bound={"verdict": "gpu", "r": 0.99, "gpu_ms": 24.8,
                                                                    "ft_ms": 25.0})
        assert is_finding(res, conf), util


def test_gpu_limit_on_a_borderline_split_is_only_worth_checking():
    res = run("gpu_limit", make(system=engine(70.0)), stats={"avg_fps": 40.0}, bound={"verdict": "mixed", "r": 0.82})
    assert is_finding(res, "low") and "borderline" in res.evidence[0].text.lower()


def test_gpu_limit_is_ruled_out_when_capped_and_the_engine_idles():
    res = run("gpu_limit", make(system=engine(5.0)), stats={"avg_fps": 120.0}, bound={"verdict": "capped", "r": 0.05})
    assert isinstance(res, rules.Cleared) and "frame-rate cap" in res.text and "5% busy" in res.text


# ------------------------------------------------------------------------------- CPU limit ---

def threads(pct):
    return synth.system_table(30, game_top_thread_pct=pct, game_cpu_pct=pct + 5.0)


def test_cpu_limit_with_a_saturated_thread_and_an_idle_gpu_is_likely():
    res = run("cpu_limit", make(system=threads(99.0)), stats={"avg_fps": 39.0},
              bound={"verdict": "cpu_main", "r": 0.02, "busiest_thread": 0.99})
    assert is_finding(res, "high") and res.title.startswith("One game thread")


def test_cpu_limit_is_silent_when_the_thread_is_not_saturated_and_cleared_when_capped():
    assert run("cpu_limit", make(system=threads(80.0)), stats={"avg_fps": 39.0},
               bound={"verdict": "cpu_main", "r": 0.02, "busiest_thread": 0.84}) is None
    res = run("cpu_limit", make(system=threads(25.0)), stats={"avg_fps": 120.0},
              bound={"verdict": "capped", "r": 0.05, "busiest_thread": 0.25})
    assert isinstance(res, rules.Cleared)


@pytest.mark.parametrize("fps, kind", [(129.5, Finding), (129.7, rules.Cleared)])
def test_cpu_limit_costs_nothing_once_the_game_reaches_ninety_percent_of_the_refresh(fps, kind):
    res = run("cpu_limit", make(system=threads(99.0)), stats={"avg_fps": fps},
              bound={"verdict": "cpu_main", "r": 0.3, "busiest_thread": 0.99})
    assert isinstance(res, kind)


# ------------------------------------------------------------------------------ throttling ---

def nv(share, **cols):
    return synth.gpu_table(30, nv_throttle_pct=share, **cols)


@pytest.mark.parametrize("share, kind", [(30.0, Finding), (29.9, rules.Cleared)])
def test_gpu_throttle_needs_thirty_percent_of_the_time_held_down(share, kind):
    cap = make(adapter=NV, ok=("frames", "system", "gpu"), gpu=nv(share, nv_reasons="sw_thermal_slowdown"))
    res = run("gpu_throttle", cap, bound={"verdict": "gpu"})
    assert isinstance(res, kind)
    if kind is Finding:
        assert res.confidence == "high"


def test_gpu_throttle_at_the_power_limit_alone_is_normal_not_a_fault():
    cap = make(adapter=NV, ok=("frames", "system", "gpu"), gpu=nv(80.0, nv_reasons="sw_power_cap"))
    assert run("gpu_throttle", cap, bound={"verdict": "gpu"}) is None


def test_gpu_throttle_counts_the_target_temperature():
    cap = make(adapter=NV, ok=("frames", "system", "gpu"), gpu=nv(10.0, nv_temp_c=75.0),
               inventory={"nvidia": {"temp_target_c": 75}})
    res = run("gpu_throttle", cap, bound={"verdict": "gpu"})
    assert res is None                      # the target temperature (+2) against a low throttle share (-2)
    cap.gpu = nv(10.0, nv_temp_c=74.9)
    assert isinstance(run("gpu_throttle", cap, bound={"verdict": "gpu"}), rules.Cleared)


def test_gpu_throttle_is_unchecked_when_the_game_is_not_on_the_nvidia_gpu():
    cap = make(src={"gpu": SourceStatus("off", "the game renders on the Intel GPU; NVML not started")})
    res = run("gpu_throttle", cap, bound={"verdict": "gpu"})
    assert isinstance(res, rules.Unchecked) and "NVML not started" in res.text


def cpu_sys(limit=100.0, thermal=100.0, perf=105.0, busiest=95.0):
    return synth.system_table(60, cpu_perf_limit_pct=limit, thermal_limit_pct=thermal, cpu_perf_pct=perf,
                              game_top_thread_pct=busiest)


@pytest.mark.parametrize("held, kind", [(0.25, Finding), (0.24, rules.Cleared)])
def test_cpu_throttle_needs_a_quarter_of_samples_below_full_speed(held, kind):
    n = len(synth.system_table(60)["t"])
    below = round(held * n)
    limit = (lambda t: 80.0 if t <= below * 0.25 else 100.0)
    res = run("cpu_throttle", make(system=cpu_sys(limit=limit), duration=60.0),
              bound={"verdict": "cpu_main", "busiest_thread": 0.95})
    assert isinstance(res, kind), (below, n)


def test_cpu_throttle_counts_a_thermal_passive_limit():
    res = run("cpu_throttle", make(system=cpu_sys(thermal=85.0), duration=60.0),
              bound={"verdict": "cpu_main", "busiest_thread": 0.95})
    assert is_finding(res, "medium") and "85%" in res.evidence[0].text


@pytest.mark.parametrize("later, kind", [(80.0, Finding), (80.1, rules.Cleared)])
def test_cpu_throttle_counts_a_speed_drop_of_twenty_percent_from_the_start(later, kind):
    perf = (lambda t: 100.0 if t <= 20.0 else later)
    res = run("cpu_throttle", make(system=cpu_sys(perf=perf), duration=60.0),
              bound={"verdict": "cpu_main", "busiest_thread": 0.95})
    assert isinstance(res, kind)


@pytest.mark.parametrize("busiest, kind", [(0.69, type(None)), (0.70, Finding)])
def test_cpu_throttle_needs_the_game_near_cpu_bound(busiest, kind):
    res = run("cpu_throttle", make(system=cpu_sys(thermal=85.0, busiest=100 * busiest), duration=60.0),
              bound={"verdict": "mixed", "busiest_thread": busiest})
    assert isinstance(res, kind)


# -------------------------------------------------------------------------------- memory ---

def vram(used, shared=140.0):
    return synth.system_table(60, vram_used_mb=used, game_shared_mb=shared)


@pytest.mark.parametrize("used, kind", [(7373.0, Finding), (7372.0, rules.Cleared)])
def test_vram_overflow_needs_ninety_percent_of_video_memory(used, kind):      # 0.9 x 8192 = 7372.8
    res = run("vram_overflow", make(adapter=NV, system=vram(used), duration=60.0), bound={"verdict": "gpu"})
    assert isinstance(res, kind)


def test_vram_overflow_counts_shared_spill_only_while_video_memory_is_full():
    full = run("vram_overflow", make(adapter=NV, system=vram(8000.0, shared=900.0), duration=60.0),
               bound={"verdict": "gpu"})
    assert is_finding(full) and any("spilled 900 MB" in e.text for e in full.evidence)
    roomy = run("vram_overflow", make(adapter=NV, system=vram(4000.0, shared=900.0), duration=60.0),
                bound={"verdict": "gpu"})
    assert isinstance(roomy, rules.Cleared)


@pytest.mark.parametrize("peak, spilled", [(512.1, True), (512.0, False)])
def test_vram_overflow_spill_is_over_512_mb(peak, spilled):
    res = run("vram_overflow", make(adapter=NV, system=vram(8000.0, shared=peak), duration=60.0),
              bound={"verdict": "gpu"})
    assert any("spilled" in e.text for e in res.evidence) is spilled


def test_vram_overflow_spill_rate_of_256_mb_a_minute():
    for per_min, grew in ((257.0, True), (250.0, False)):
        sys_ = synth.system_table(60, vram_used_mb=8000.0, game_shared_mb=lambda t: 100.0 + per_min * t / 60.0)
        sys_[S.GAME_SHARED_MB] = [min(v, 500.0) for v in sys_[S.GAME_SHARED_MB]]   # stay under the 512 MB level
        res = run("vram_overflow", make(adapter=NV, system=sys_, duration=60.0), bound={"verdict": "gpu"})
        assert any("grew" in e.text for e in res.evidence) is grew, per_min


def test_vram_overflow_on_an_integrated_gpu_is_ruled_out():
    res = run("vram_overflow", make(adapter=INTEL, system=vram(8000.0, shared=900.0)), bound={"verdict": "gpu"})
    assert isinstance(res, rules.Cleared) and "integrated" in res.text


def ram(avail, **cols):
    return synth.system_table(30, ram_avail_mb=avail, **cols)


@pytest.mark.parametrize("avail, kind", [(1638.0, Finding), (1639.0, rules.Cleared)])   # 10 % of 16 GB = 1638.4
def test_ram_pressure_warns_under_ten_percent_free(avail, kind):
    assert isinstance(run("ram_pressure", make(system=ram(avail))), kind)


@pytest.mark.parametrize("avail, critical", [(819.0, True), (820.0, False)])            # 5 % = 819.2 MB
def test_ram_pressure_is_critical_under_five_percent(avail, critical):
    res = run("ram_pressure", make(system=ram(avail)))
    assert is_finding(res) and ("critical" in res.evidence[0].text) is critical


def test_ram_pressure_is_critical_under_64_mb_when_the_ram_size_is_unknown():
    cap = make(system=ram(63.0), inventory={"ram_gb": None})
    assert is_finding(run("ram_pressure", cap))
    cap = make(system=ram(64.0), inventory={"ram_gb": None})
    assert run("ram_pressure", cap) is None


@pytest.mark.parametrize("faults, kind", [(1000.1, Finding), (1000.0, type(None))])
def test_ram_pressure_counts_over_1000_hard_faults_a_second(faults, kind):
    # with the RAM size unknown, free memory is no evidence either way, so the fault rate alone decides
    cap = make(system=ram(8000.0, hard_faults_ps=faults), inventory={"ram_gb": None})
    assert isinstance(run("ram_pressure", cap), kind)


def test_ram_pressure_plenty_of_free_ram_outweighs_hard_faults_from_file_reads():
    # LC 4.7: file-backed reads count as hard faults too, so with RAM free they are no sign of pressure
    assert run("ram_pressure", make(system=ram(8000.0, hard_faults_ps=5000.0))) is None


@pytest.mark.parametrize("commit, conf", [(90.1, "medium"), (90.0, "low"), (80.1, "low")])
def test_ram_pressure_counts_commit_charge_over_80_and_90_percent(commit, conf):
    res = run("ram_pressure", make(system=ram(1000.0, commit_pct=commit), inventory={"ram_gb": None}))
    # 1000 MB free with no RAM size is neutral, so commit alone decides the confidence
    assert is_finding(res, conf)


def test_ram_pressure_counts_event_linked_paging():
    res = run("ram_pressure", make(system=ram(1500.0)), events=[stutter(1, 5.0, "memory_paging")],
              causes={"memory_paging": lift("supports")})
    assert is_finding(res, "medium") and any("paging" in e.text for e in res.evidence)


# ---------------------------------------------------------------------------------- power ---

POWER = {"scheme_guid": "64a64f24-65b9-4b56-befd-5ec1eaced9b3", "scheme_name": "Silent",
         "schemes": [["381b4222-f694-41f0-9685-ff5bb260df2e", "Balanced"],
                     ["64a64f24-65b9-4b56-befd-5ec1eaced9b3", "Silent"],
                     ["a5a5a5a5-0000-4000-8000-000000000001", "ASUS Recommended"]],
         "slider": "balanced", "effective_mode": "balanced", "on_battery": False, "battery_pct": 100, "saver": False}


def test_power_plan_quiet_plan_offers_the_switch_to_balanced():
    res = run("power_plan", make(inventory={"power": dict(POWER)}))
    assert is_finding(res, "medium") and "Silent" in res.evidence[0].text
    fix = next(f for f in res.fixes if f.fix_id)
    assert fix.fix_id == "power_plan" and fix.params == {"scheme": "381b4222-f694-41f0-9685-ff5bb260df2e"}


def test_power_plan_with_throttling_evidence_is_likely():
    cap = make(inventory={"power": dict(POWER)}, system=synth.system_table(30, cpu_perf_limit_pct=70.0))
    assert is_finding(run("power_plan", cap), "high")


@pytest.mark.parametrize("change, text", [({"on_battery": True}, "battery"), ({"saver": True}, "battery saver"),
                                          ({"slider": "best_efficiency"}, "Best power efficiency"),
                                          ({"effective_mode": "better_battery"}, "better battery")])
def test_power_plan_flags_battery_saver_and_power_modes(change, text):
    power = dict(POWER, scheme_name="Balanced", scheme_guid="381b4222-f694-41f0-9685-ff5bb260df2e", **change)
    res = run("power_plan", make(inventory={"power": power}))
    assert is_finding(res) and text.lower() in res.evidence[0].text.lower()
    assert not any(f.fix_id for f in res.fixes)                 # only the plan itself is changed automatically


def test_power_plan_picks_a_performance_plan_when_balanced_is_not_listed_and_never_a_junk_guid():
    power = dict(POWER, schemes=[["not-a-guid", "High performance"], ["8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c",
                                                                      "High performance"],
                                 ["64a64f24-65b9-4b56-befd-5ec1eaced9b3", "Silent"]])
    res = run("power_plan", make(inventory={"power": power}))
    assert next(f for f in res.fixes if f.fix_id).params == {"scheme": "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"}
    power["schemes"] = [["not-a-guid", "Balanced"], ["64a64f24-65b9-4b56-befd-5ec1eaced9b3", "Silent"]]
    assert not any(f.fix_id for f in run("power_plan", make(inventory={"power": power})).fixes)


def test_power_plan_fine_is_cleared_and_unread_is_unchecked():
    fine = dict(POWER, scheme_name="Balanced", scheme_guid="381b4222-f694-41f0-9685-ff5bb260df2e")
    res = run("power_plan", make(inventory={"power": fine}))
    assert isinstance(res, rules.Cleared) and res.text.startswith("Plugged in") and "Balanced" in res.text
    assert isinstance(run("power_plan", make(inventory={"power": {}})), rules.Unchecked)


# ---------------------------------------------------------------------------- refresh rate ---

@pytest.mark.parametrize("hz, kind", [(142.9, Finding), (143.0, rules.Cleared)])
def test_refresh_rate_below_its_maximum_by_more_than_one_hertz(hz, kind):
    res = run("refresh_rate", make(inventory={"displays": [dict(DISPLAY, hz=hz)]}))
    assert isinstance(res, kind)


def test_refresh_rate_fix_names_the_device_and_the_rate_and_vsync_makes_it_likely():
    cap = make(inventory={"displays": [dict(DISPLAY, hz=60.0)]})
    res = run("refresh_rate", cap, stats={"avg_fps": 60.0}, bound={"verdict": "vsync"})
    assert is_finding(res, "high")
    fix = next(f for f in res.fixes if f.fix_id)
    assert fix.fix_id == "refresh_rate" and fix.params == {"device": r"\\.\DISPLAY1", "hz": 144}
    assert is_finding(run("refresh_rate", cap, stats={"avg_fps": 40.0}, bound={"verdict": "gpu"}), "medium")


@pytest.mark.parametrize("device", [r"\\.\DISPLAY1; rm", "DISPLAY1", r"\\?\DISPLAY1", None])
def test_refresh_rate_never_offers_the_fix_for_an_odd_device_name(device):
    res = run("refresh_rate", make(inventory={"displays": [dict(DISPLAY, hz=60.0, device=device)]}))
    assert is_finding(res) and not any(f.fix_id for f in res.fixes)


# --------------------------------------------------------------------------- VSync judder ---

def judder_frames(n_one, n_two, swing=0.0):
    """Displayed intervals of one refresh (6.94 ms) and of two, with even presents unless swing is given."""
    p = 1000.0 / 144.0
    disp = [p] * n_one + [2 * p] * n_two
    return synth.frames_table([p + (swing if i % 2 else 0.0) for i in range(len(disp))], display_ms=disp,
                              displayed=1)


@pytest.mark.parametrize("n_two, kind", [(10, Finding), (9, rules.Cleared)])
def test_vsync_judder_needs_ten_percent_of_frames_held_two_refreshes(n_two, kind):
    res = run("vsync_judder", make(), frames=judder_frames(100 - n_two, n_two),
              bound={"verdict": "vsync", "quantised_pct": 100.0})
    assert isinstance(res, kind)


@pytest.mark.parametrize("q, kind", [(90.0, Finding), (89.9, rules.Cleared)])
def test_vsync_judder_needs_ninety_percent_of_frames_on_whole_refreshes(q, kind):
    res = run("vsync_judder", make(), frames=judder_frames(80, 20), bound={"verdict": "gpu", "quantised_pct": q})
    assert isinstance(res, kind)


def test_vsync_judder_leaves_caps_low_fps_and_uneven_presents_to_their_own_rules():
    assert run("vsync_judder", make(), frames=judder_frames(80, 20),
               bound={"verdict": "capped", "quantised_pct": 100.0}) is None
    assert run("vsync_judder", make(), frames=judder_frames(20, 80),
               bound={"verdict": "gpu", "quantised_pct": 100.0}) is None
    uneven = LagEvent(1, "pacing", 0.0, 29.0, 8.0, {"present_swing_pct": 60.0})
    assert run("vsync_judder", make(), frames=judder_frames(80, 20), events=[uneven],
               bound={"verdict": "gpu", "quantised_pct": 100.0}) is None


# -------------------------------------------------------------------------- uneven pacing ---

def pacing(t0, t1, present_swing):
    return LagEvent(1, "pacing", t0, t1, 9.0, {"present_swing_pct": present_swing, "swing_pct": 60.0})


@pytest.mark.parametrize("span, kind", [(3.01, Finding), (2.99, rules.Cleared)])     # 10 % of 30 s
def test_uneven_pacing_needs_ten_percent_of_the_capture(span, kind):
    assert isinstance(run("uneven_pacing", make(), events=[pacing(5.0, 5.0 + span, 50.0)]), kind)


@pytest.mark.parametrize("swing, kind", [(10.1, Finding), (10.0, rules.Cleared)])
def test_uneven_pacing_counts_only_uneven_presents_not_display_judder(swing, kind):
    assert isinstance(run("uneven_pacing", make(), events=[pacing(0.0, 29.0, swing)]), kind)


@pytest.mark.parametrize("span, conf", [(15.0, "high"), (14.9, "medium")])           # half of 30 s
def test_uneven_pacing_over_half_the_capture_is_a_direct_readout(span, conf):
    res = run("uneven_pacing", make(), events=[pacing(1.0, 1.0 + span, 70.0)])
    assert is_finding(res, conf) and res.event_ids == [1]


# ------------------------------------------------------------------------------ frame cap ---

@pytest.mark.parametrize("cap_fps, kind", [(129.5, Finding), (129.7, rules.Cleared)])
def test_frame_cap_under_ninety_percent_of_the_refresh_is_misconfigured(cap_fps, kind):
    res = run("frame_cap", make(), bound={"verdict": "capped", "cap_fps": cap_fps, "r": 0.1})
    assert isinstance(res, kind)
    if kind is Finding:
        assert res.confidence == "medium" and not any(f.fix_id for f in res.fixes)   # caps are advice only


@pytest.mark.parametrize("r, kind", [(0.69, Finding), (0.70, type(None))])
def test_frame_cap_needs_the_gpu_to_have_room(r, kind):
    assert isinstance(run("frame_cap", make(), bound={"verdict": "capped", "cap_fps": 60.0, "r": r}), kind)


def test_frame_cap_without_a_cap_or_a_verdict():
    assert isinstance(run("frame_cap", make(), bound={"verdict": "gpu"}), rules.Cleared)
    assert isinstance(run("frame_cap", make(), bound={"verdict": "unknown", "notes": ["too short"]}), rules.Unchecked)
    nodisp = make(inventory={"displays": []})
    assert isinstance(run("frame_cap", nodisp, bound={"verdict": "capped", "cap_fps": 60.0, "r": 0.1}), rules.Unchecked)


# ------------------------------------------------------------------- windowed, DVR, Game Mode ---

def test_windowed_composition_flags_the_blt_model_and_offers_the_switch_only_for_dxgi():
    off = {"settings": dict(SETTINGS, windowed_optimizations=False)}
    res = run("windowed_composition", make(inventory=off),
              stats={"present_mode": "Composed: Copy with GPU GDI", "runtime": "DXGI"})
    assert is_finding(res, "medium")
    fix = next(f for f in res.fixes if f.fix_id)
    assert fix.fix_id == "windowed_optimizations_on" and fix.params == {} and fix.needs_restart == "game"
    gl = run("windowed_composition", make(inventory=off),
             stats={"present_mode": "Composed: Copy with GPU GDI", "runtime": "Other"})
    assert is_finding(gl) and not any(f.fix_id for f in gl.fixes)


def test_windowed_composition_is_fine_with_flip_and_unchecked_without_frame_timing():
    assert isinstance(run("windowed_composition", make(), stats={"present_mode": "Composed: Flip"}), rules.Cleared)
    off = {"settings": dict(SETTINGS, windowed_optimizations=False)}
    for cap in (make(ok=("system",), src={"frames": NO_FRAMES}, inventory=off),
                make(ok=("system",), src={"frames": NO_FRAMES})):
        res = run("windowed_composition", cap, stats={"frames": 0})
        assert isinstance(res, rules.Unchecked) and "PresentMon needs admin rights" in res.text


@pytest.mark.parametrize("settings, kind, conf", [
    ({"background_recording": True}, Finding, "medium"), ({"game_dvr": True}, Finding, "low"),
    ({}, rules.Cleared, None), ({"game_dvr": None, "background_recording": None}, rules.Unchecked, None)])
def test_game_dvr(settings, kind, conf):
    res = run("game_dvr", make(inventory={"settings": dict(SETTINGS, **settings)}))
    assert isinstance(res, kind)
    if kind is Finding:
        assert res.confidence == conf
        fix = next(f for f in res.fixes if f.fix_id)
        assert fix.fix_id == "game_dvr_off" and fix.params == {} and fix.needs_restart == "game"


@pytest.mark.parametrize("mode, kind", [(False, Finding), (True, rules.Cleared), (None, rules.Unchecked)])
def test_game_mode_off(mode, kind):
    res = run("game_mode_off", make(inventory={"settings": dict(SETTINGS, game_mode=mode)}))
    assert isinstance(res, kind)
    if kind is Finding:
        assert res.confidence == "low"
        assert [(f.fix_id, f.params) for f in res.fixes if f.fix_id] == [("game_mode_on", {})]


# ---------------------------------------------------------------------------------- focus ---

@pytest.mark.parametrize("behind_s, kind", [(3.0, Finding), (2.9, rules.Cleared)])       # 10 % of 30 s
def test_not_focused_needs_ten_percent_of_the_capture_behind_another_window(behind_s, kind):
    sys_ = synth.system_table(30, game_focused=lambda t: 0 if t <= behind_s + 1e-9 else 1)
    res = run("not_focused", make(system=sys_))
    assert isinstance(res, kind), sum(1 for v in sys_[S.GAME_FOCUSED] if v == 0)


def test_not_focused_linked_to_the_stutters_counts_even_when_mostly_in_front():
    sys_ = synth.system_table(30, game_focused=1)
    res = run("not_focused", make(system=sys_), events=[stutter(i, 3.0 * i, "not_focused") for i in range(1, 7)],
              causes={"not_focused": lift("supports", a=6, n=6)})
    assert is_finding(res, "medium") and res.event_ids == [1, 2, 3, 4, 5, 6]


# ----------------------------------------------------------------------- background hogs ---

def hogs(others, **kw):
    """A capture with the system sampler's table (background_hogs needs it) and this others table."""
    return make(system=synth.system_table(30), others=others, **kw)


def hog_others(cpu_during, pids=(20000, 20004), t_events=(5.0, 10.0, 15.0, 20.0, 25.0), create=False):
    rows = []
    for k in range(1, 121):
        t = k * 0.25
        hot = any(te - 0.5 <= t <= te + 0.4 for te in t_events)
        procs = [[7777, "python.exe", 100.0, 0.0, 100.0]]
        for p in pids:
            entry = [p, "hog.exe", cpu_during / len(pids) if hot else 0.0, 0.0, 0.0]
            if create:
                entry.append(134000000000000000 + p)
            procs.append(entry)
        rows.append([t, procs])
    return rows


def hog_events(n=5, cause="background_cpu"):
    return [stutter(i, 5.0 * i, cause, process="hog.exe", process_pids=[20000, 20004]) for i in range(1, n + 1)]


@pytest.mark.parametrize("cpu, named", [(25.0, True), (24.9, False)])
def test_background_hogs_names_programs_using_a_quarter_cpu_during_the_lag(cpu, named):
    res = run("background_hogs", hogs(hog_others(cpu)), events=hog_events(),
              causes={"background_cpu": lift("supports", a=5, n=5),
                      "background_disk": lift("neutral", a=0, n=5), "background_gpu": lift("neutral", a=0, n=5)})
    assert is_finding(res)
    assert ("hog.exe" in res.title) is named


def test_background_hogs_quiet_disk_and_gpu_are_no_evidence_against_a_cpu_hog():
    causes = {"background_cpu": lift("supports", a=12, n=12),
              "background_disk": lift("contradicts", a=0, n=12), "background_gpu": lift("contradicts", a=0, n=12)}
    res = run("background_hogs", hogs(hog_others(300.0)), events=hog_events(12), causes=causes)
    assert is_finding(res, "high")       # lift >= 4 (3) + the CPU side of the slow frames (2)


def test_background_hogs_cleared_when_every_mechanism_contradicts_and_unchecked_with_too_few():
    causes = {k: lift("contradicts", a=0, n=12) for k in ("background_cpu", "background_disk", "background_gpu")}
    res = run("background_hogs", hogs(hog_others(0.0)), events=[stutter(i, 2.0 * i) for i in range(12)],
              causes=causes)
    assert isinstance(res, rules.Cleared) and "0 of 12" in res.text
    few = {k: lift("too_few_events", a=0, n=3) for k in ("background_cpu", "background_disk", "background_gpu")}
    res = run("background_hogs", hogs([]), events=[stutter(i, 2.0 * i) for i in range(3)], causes=few)
    assert isinstance(res, rules.Unchecked) and "at least 5" in res.text
    assert run("background_hogs", hogs([]), events=[], causes={}) is None


def test_background_hogs_offers_the_priority_fix_only_with_a_creation_time_and_never_for_the_game():
    causes = {"background_cpu": lift("supports", a=5, n=5)}
    res = run("background_hogs", hogs(hog_others(300.0)), events=hog_events(), causes=causes)
    assert not any(f.fix_id for f in res.fixes)                    # DESIGN's others rows carry no creation time
    res = run("background_hogs", hogs(hog_others(300.0, create=True)), events=hog_events(), causes=causes)
    fix = next(f for f in res.fixes if f.fix_id)
    assert fix.fix_id == "background_priority"
    assert fix.params == {"pid": 20000, "create_time": 134000000000020000, "class": "below_normal"}
    game = hogs(hog_others(300.0, create=True), target={"pids": [4242, 20000, 20004]})
    assert not any(f.fix_id for f in run("background_hogs", game, events=hog_events(), causes=causes).fixes)


# --------------------------------------------------------------------------------- disks ---

def test_hdd_streaming():
    hdd = {"game": dict(GAME, media="HDD")}
    assert is_finding(run("hdd_streaming", make(inventory=hdd)), "medium")
    linked = run("hdd_streaming", make(inventory=hdd), events=hog_events(6, "asset_streaming"),
                 causes={"asset_streaming": lift("supports", a=6, n=6)})
    assert is_finding(linked, "high") and linked.event_ids == [1, 2, 3, 4, 5, 6]
    assert isinstance(run("hdd_streaming", make()), rules.Cleared)
    assert isinstance(run("hdd_streaming", make(inventory={"game": dict(GAME, media="Unknown")})), rules.Unchecked)


@pytest.mark.parametrize("ms, conf", [(15.1, "high"), (15.0, "medium")])
def test_hdd_streaming_counts_slow_disk_reads_over_15_ms(ms, conf):
    res = run("hdd_streaming", make(inventory={"game": dict(GAME, media="HDD")},
                                    system=synth.system_table(30, disk_read_ms=ms)))
    assert is_finding(res, conf)


# --------------------------------------------------------------------------- shader compile ---

def own_stutters(n, early):
    """n stutters of the game's own work in a 30 s capture, `early` of them in the first third."""
    times = [1.0 + 8.0 * i / max(1, early) for i in range(early)] + \
            [12.0 + 17.0 * i / max(1, n - early) for i in range(n - early)]
    return [stutter(i + 1, t, "game_cpu_spike") for i, t in enumerate(times)]


@pytest.mark.parametrize("early, kind", [(6, Finding), (5, rules.Cleared)])
def test_shader_compile_needs_sixty_percent_of_the_games_own_stutters_early(early, kind):
    res = run("shader_compile", make(), events=own_stutters(10, early))
    assert isinstance(res, kind)
    if kind is Finding:
        assert res.confidence == "low" and not any(f.fix_id for f in res.fixes)    # advice only


@pytest.mark.parametrize("n, kind", [(5, Finding), (4, rules.Unchecked)])
def test_shader_compile_needs_five_of_the_games_own_stutters(n, kind):
    assert isinstance(run("shader_compile", make(), events=own_stutters(n, n)), kind)


# --------------------------------------------------------------------------- stutter causes ---

def test_a_supported_stutter_cause_with_the_cpu_side_slow_is_likely():
    evs = [stutter(i, 3.0 * i, "game_cpu_spike") for i in range(1, 10)]
    res = run("game_cpu_spike", make(), events=evs, causes={"game_cpu_spike": lift("supports")})
    assert is_finding(res, "high") and res.category == "stutter" and res.event_ids == list(range(1, 10))
    assert any("CPU side was slow" in e.text for e in res.evidence)


@pytest.mark.parametrize("cpu_side, conf", [(7, "high"), (6, "medium")])
def test_the_cpu_side_counts_when_seventy_percent_of_the_events_show_it(cpu_side, conf):
    evs = [stutter(i, 3.0 * i, "game_cpu_spike", side="cpu" if i <= cpu_side else "gpu") for i in range(1, 11)]
    res = run("game_cpu_spike", make(), events=evs, causes={"game_cpu_spike": lift("supports", a=10, n=10)})
    assert is_finding(res, conf)


def test_stutter_cause_verdicts_from_the_lift_test():
    evs = [stutter(i, 2.0 * i) for i in range(1, 13)]
    res = run("asset_streaming", make(), events=evs, causes={"asset_streaming": lift("contradicts", a=0, n=12)})
    assert isinstance(res, rules.Cleared) and "0 of 12" in res.text
    assert run("asset_streaming", make(), events=evs, causes={"asset_streaming": lift("neutral", a=1, n=12)}) is None
    few = [stutter(1, 2.0, "asset_streaming"), stutter(2, 4.0)]
    res = run("asset_streaming", make(), events=few, causes={"asset_streaming": lift("too_few_events", a=1, n=2)})
    assert isinstance(res, rules.Unchecked) and "at least 5" in res.text
    assert run("asset_streaming", make(), events=[stutter(1, 2.0)],
               causes={"asset_streaming": lift("too_few_events", a=0, n=1)}) is None
    res = run("asset_streaming", make(), events=evs,
              causes={"asset_streaming": lift("not_measured", a=0, n=12, why="no game I/O or disk counters")})
    assert isinstance(res, rules.Unchecked) and res.text == "no game I/O or disk counters"


def test_stutter_causes_without_frame_timing_are_unchecked_and_with_no_stutters_silent():
    res = run("game_cpu_spike", make(ok=("system",), src={"frames": NO_FRAMES}), stats={"frames": 0})
    assert isinstance(res, rules.Unchecked) and "PresentMon needs admin rights" in res.text
    assert run("game_cpu_spike", make()) is None


def test_the_game_waiting_on_itself_needs_its_thread_idle_too():
    evs = [stutter(i, 3.0 * i, "game_stall") for i in range(1, 10)]
    causes = {"game_stall": lift("supports", weight=1.0, lift_=None), "asset_streaming": lift("neutral", a=0)}
    idle = run("game_stall", make(system=synth.system_table(30, game_top_thread_pct=20.0)), events=evs, causes=causes)
    assert is_finding(idle, "high")
    busy = run("game_stall", make(system=synth.system_table(30, game_top_thread_pct=95.0)), events=evs, causes=causes)
    assert is_finding(busy, "medium")


def test_a_residual_cause_loses_coverage_when_another_cause_was_not_measured():
    evs = [stutter(i, 3.0 * i, "game_stall") for i in range(1, 10)]
    causes = {"game_stall": lift("supports", weight=1.0, lift_=None),
              "asset_streaming": lift("not_measured", a=0, why="no game I/O or disk counters"),
              "game_cpu_spike": lift("not_measured", a=0, why="no per-thread CPU for the game"),
              "driver_dpc": lift("not_measured", a=0, why="no DPC counters")}
    res = run("game_stall", make(system=synth.system_table(30, game_top_thread_pct=20.0)), events=evs, causes=causes)
    assert is_finding(res, "medium")        # S = 4, but disk and DPCs unmeasured: coverage 4 / 8 < 0.6


def test_a_gpu_spike_is_never_reported_while_the_cpu_limits_the_game():
    evs = [stutter(i, 3.0 * i, "gpu_spike", side="gpu") for i in range(1, 10)]
    causes = {"gpu_spike": lift("supports", weight=1.0, lift_=None)}
    assert is_finding(run("gpu_spike", make(), events=evs, causes=causes, bound={"verdict": "capped"}), "medium")
    res = run("gpu_spike", make(), events=evs, causes=causes, bound={"verdict": "cpu_main"})
    assert isinstance(res, rules.Cleared) and "not what limits" in res.text


def test_marks_stand_in_for_frames_at_half_weight():
    marks = [LagEvent(i, "mark", 3.0 * i - 3.0, 3.0 * i + 0.5, 0.0, {"why": ["a moment you marked", "x"]},
                      causes=["background_cpu"]) for i in range(1, 7)]
    causes = {"game_cpu_spike": lift("supports", a=6, n=6, weight=1.5, half_weight=True)}
    res = run("game_cpu_spike", make(ok=("system",), src={"frames": NO_FRAMES}), stats={"frames": 0}, events=marks,
              causes=causes)
    assert is_finding(res, "low") and "marked moments" in res.evidence[0].text


def test_marks_never_rule_out_the_residual_causes_they_were_not_judged_for():
    marks = [LagEvent(i, "mark", 3.0 * i - 3.0, 3.0 * i + 0.5, 0.0, {}) for i in range(1, 13)]
    causes = {"game_stall": lift("contradicts", a=0, n=12, weight=1.0, lift_=None, half_weight=True)}
    res = run("game_stall", make(ok=("system",), src={"frames": NO_FRAMES}), stats={"frames": 0}, events=marks,
              causes=causes)
    assert res is None
