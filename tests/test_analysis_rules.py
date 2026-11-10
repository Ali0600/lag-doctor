"""Session rules and LC 5 scoring on hand-built captures: every threshold pinned at points just either side.

Each rule runs through rules.run_rule (sources first, then the rule, then its gate) with a literal ctx, so a
test controls exactly what the frames analysis would have said. The diagnosis end to end is in
test_analysis_rules_diagnose.py; the network rules are in test_analysis_rules_network.py.
"""
from __future__ import annotations

import copy
import re

import pytest

from lagdoctor.analysis import attribute, rules
from lagdoctor.model import F, G, S, Finding, LagEvent, SourceStatus
from lagdoctor.report import labels
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


def test_a_pacing_event_flagged_by_its_animation_error_reads_as_animation_error():
    # A11: an anim-only pacing event's severity is the animation-error p95, never "0.0 ms swings"
    from lagdoctor.report import labels
    assert labels.severity_text("pacing", 6.41, {"criterion": "anim"}) == "6.4 ms animation error"
    assert labels.severity_text("pacing", 6.41, {"criterion": "both"}) == "6.4 ms swings"
    assert labels.severity_text("pacing", 6.41) == "6.4 ms swings"              # the old two-argument call
    assert labels.severity_text("pacing", 6.41, "junk") == "6.4 ms swings"


def test_absurd_values_from_a_corrupt_capture_are_missing_never_evidence():
    # A15: a hand-edited or corrupt capture's 1e308 must not crash a rule or turn up as a number in the report
    huge = 1e308
    sys_ = synth.system_table(30, ram_avail_mb=lambda t: huge if t < 3 else 8000.0, game_shared_mb=huge,
                              vram_used_mb=huge, hard_faults_ps=huge, disk_read_ms=huge, cpu_perf_pct=huge,
                              cpu_perf_limit_pct=huge, thermal_limit_pct=huge, commit_pct=huge, game_focused=huge,
                              game_gpu_pct=huge, gpu_other_pct=huge, game_top_thread_pct=huge)
    cap = make(adapter=NV, ok=("frames", "system", "gpu", "network", "net_flows"), system=sys_,
               gpu=synth.gpu_table(30, nv_temp_c=huge, nv_throttle_pct=huge, nv_power_cap_pct=huge,
                                   nv_util_pct=huge, nv_power_limit_w=huge),
               inventory={"ram_gb": huge, "nvidia": {"temp_target_c": 75, "power_default_w": huge}},
               others=[[0.25 * k, [[9000, "hog.exe", huge, huge, huge, 134000000000009000]]] for k in range(1, 121)])
    cap.network = synth.network_table(30, inet_rtt_ms=huge, gw_rtt_ms=huge, server_rtt_ms=huge, wifi_rssi_dbm=huge,
                                      wifi_signal_pct=huge, wifi_channel=huge, nic_rx_bps=huge, nic_tx_bps=huge)
    cap.net_flows = {"downloads": [{"t": 5.0, "source": "steam", "mbps": huge}], "download_sources": ["steam"],
                     "talkers": [{"pid": 9000, "name": "hog.exe", "rx_bytes": huge, "tx_bytes": huge}],
                     "wifi_scans": [huge, 5.0], "server": {"ip": "203.0.113.7", "first_rx_t": huge, "last_rx_t": huge}}
    findings, cleared, unchecked = full_run(cap, stats={"avg_fps": 40.0, "dropped_pct": huge},
                                            bound={"verdict": "gpu", "r": 0.95})
    absurd = re.compile(r"\binf\b|\bnan\b|e\+\d|\d{16}")         # inf, nan, 1e+308, or a 300-digit number
    assert not [w for w in unchecked.values() if w.startswith("the check failed")], unchecked
    for f in findings:
        for ev in f.evidence:
            assert ev.value is None or abs(ev.value) <= 1e15, (f.cause, ev.text, ev.value)
            assert not absurd.search(ev.text), (f.cause, ev.text)
    assert not [t for t in list(cleared.values()) + list(unchecked.values()) if absurd.search(t)]


def test_sentence_keeps_a_process_name_lower_case():
    assert rules._sentence("python.exe rose") == "python.exe rose."
    assert rules._sentence("the GPU was busy") == "The GPU was busy."


# ------------------------------------------------------------------------- run_rule plumbing ---

def test_a_rule_whose_source_was_not_measured_is_unchecked_with_its_reason():
    # Changed on purpose (wording): the reason chained the source's own message after a colon ("needs frame timing
    # (PresentMon), which was not measured: frame timing was skipped: ..."), path and command included, once per
    # cause; the Not measured list gives it once (test_a_source_that_ran_but_recorded_nothing_is_not_measured)
    cap = make(ok=("system",), src={"frames": NO_FRAMES})
    res = run("cpu_limit", cap, bound={"verdict": "cpu_main", "r": 0.1, "busiest_thread": 0.99})
    assert isinstance(res, rules.Unchecked)
    assert res.text == "needs frame timing (PresentMon), which was not recorded (see Not measured)" and "admin rights" not in res.text


def test_a_source_missing_from_the_capture_reads_not_recorded():
    cap = make(ok=())
    res = run("ram_pressure", cap)
    assert isinstance(res, rules.Unchecked) and res.text == ("needs the system sampler, which was not recorded (see "
                                                             "Not measured)")


def test_a_source_that_ran_but_recorded_nothing_is_not_measured():
    from lagdoctor.analysis.diagnose import measured
    from lagdoctor.report import labels
    cap = make(ok=("frames", "system"))                     # system "ok" but no rows
    res = run("ram_pressure", cap)
    assert isinstance(res, rules.Unchecked) and res.text.endswith("which was not recorded (see Not measured)")
    # ... and the Not measured list it points at names it, though the source's own state is "ok"
    m = measured(cap, {"frames": 3000})
    assert m["system"] == "it ran but recorded nothing"
    assert ("the system sampler", "it ran but recorded nothing") in labels.not_measured(cap.sources, m)


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
    bound = {"verdict": verdict, "r": 0.97} if verdict == "gpu" else {"verdict": verdict}   # frame timing's
    assert isinstance(rules.run_rule(rule, make(), {"bound": bound}), kind)


PROBABLE = {"seconds": 173.0, "share": 0.205, "windows": 845, "marks": 3, "marks_busy": 3, "holds": True}


def gated_gpu_finding(probable, frames=False, gate="gpu"):
    """A side-gated finding through run_rule with an open ("unknown") verdict and frames.probable_gpu's reading."""
    def found(cap, ctx):
        return Finding(cause="gpu_throttle", title="t", category="fps", confidence="high", score=1.0, summary="")
    rule = rules.Rule("gpu_throttle", "gpu_telemetry", (), gate, found)
    cap = make() if frames else make(ok=("system",), src={"frames": NO_FRAMES})
    return rules.run_rule(rule, cap, {"stats": {"frames": 3000 if frames else 0},
                                      "bound": {"verdict": "unknown", "gpu_probable": probable,
                                                "notes": ["frame timing not measured"]}})


def test_without_frame_timing_a_gpu_side_finding_passes_where_the_gpu_was_probably_the_limit():
    # Item 1: the gate read only the whole-capture median of the 3D engine (71 % in the first real capture), so GPU
    # heat and video memory went to "could not check" though the GPU was pegged in play and at every mark
    res = gated_gpu_finding(PROBABLE)
    assert is_finding(res, "high")
    assert res.evidence[-1].text == ("Frame timing was not measured, but the game kept its GPU's 3D engine 95% or more "
                                     "busy for 173 s (20% of the capture) and at 3 of the 3 moments you marked, so the "
                                     "GPU probably limited the frame rate then.")
    res = gated_gpu_finding(dict(PROBABLE, seconds=5.0, share=0.006, marks_busy=0, holds=False))
    assert isinstance(res, rules.Unchecked)
    assert res.text == ("whether the GPU limits the frame rate could not be judged (frame timing not measured, the "
                        "game kept its GPU's 3D engine 95% or more busy for only 5 s (1% of the capture), though at "
                        "none of the 3 moments you marked)")


def test_the_per_second_gpu_reading_never_opens_the_gate_with_frame_timing_or_for_the_cpu_side():
    assert isinstance(gated_gpu_finding(PROBABLE, frames=True), rules.Unchecked)               # frames say: unknown
    assert is_finding(gated_gpu_finding(PROBABLE, gate="cpu"))                                 # "unknown" passes it
    assert not any("probably limited" in e.text for e in gated_gpu_finding(PROBABLE, gate="cpu").evidence)
    # _gated applies the GPU reading to whatever gate refused an unknown verdict: that must stay the gpu gate alone
    assert [g for g, allowed in rules.GATES.items() if "unknown" not in allowed] == ["gpu"]


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
    assert isinstance(res, rules.Unchecked) and res.text == ("which GPU the game rendered on is unknown: needs the "
                                                             "system sampler, which was not recorded (see Not measured)")


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


@pytest.mark.parametrize("reasons,held", [("sw_thermal_slowdown", "heat"),
                                          ("sw_thermal_slowdown,hw_power_brake_slowdown", "heat or its power brake")])
def test_gpu_limit_names_only_what_the_readings_named(reasons, held):
    cap = make(adapter=NV, ok=("frames", "system", "gpu"), system=engine(70.0),
               gpu=synth.gpu_table(30, nv_util_pct=99.0, nv_throttle_pct=80.0, nv_reasons=reasons))
    res = run("gpu_limit", cap, stats={"avg_fps": 40.0}, bound={"verdict": "gpu", "r": 0.99, "gpu_ms": 24.8,
                                                                "ft_ms": 25.0})
    texts = [e.text for e in res.evidence]
    assert any(t.startswith(f"The GPU was also held down by {held} 80% of the time") for t in texts), texts


def test_a_gpu_verdict_under_point_nine_says_which_rule_made_it():
    # The 1 ms rule (r above 0.75, the frame within 1 ms of the GPU's busy time) and VSync at k refreshes call the GPU
    # the limit under r = 0.90, and the evidence then read "(r = 0.84); GPU-bound at 0.90 or more" beside the verdict.
    def on(hz):
        return {"displays": [dict(DISPLAY, hz=hz, max_hz=int(hz), modes_hz=[60, int(hz)])]}

    res = run("gpu_limit", make(system=engine(99.0), inventory=on(240.0)), stats={"avg_fps": 166.0},
              bound={"verdict": "gpu", "r": 0.85, "gpu_ms": 5.1, "ft_ms": 6.0})
    first = res.evidence[0]
    assert first.text == ("The GPU was busy 5.1 ms of each 6.0 ms frame (r = 0.85): the GPU idled 1 ms a frame or less, "
                          "so it sets the pace.")
    assert first.value == pytest.approx(0.9) and first.unit == "ms" and first.threshold == 1.0
    # exactly 1 ms is GPU-bound too (frames.GPU_GAP_MS is a <=), so never "under 1 ms"
    res = run("gpu_limit", make(system=engine(99.0), inventory=on(240.0)), stats={"avg_fps": 200.0},
              bound={"verdict": "gpu", "r": 0.80, "gpu_ms": 4.0, "ft_ms": 5.0})
    assert "under" not in res.evidence[0].text and res.evidence[0].value == pytest.approx(1.0)
    held = ("VSync rounds the frame rate down to 30 FPS (refresh / 2): the GPU needs 17.0 ms a frame, more than 1 "
            "refresh period(s) (16.7 ms)")
    res = run("gpu_limit", make(system=engine(99.0), inventory=on(60.0)), stats={"avg_fps": 30.0},
              bound={"verdict": "gpu", "r": 0.51, "gpu_ms": 17.0, "ft_ms": 33.3, "vsync": True, "notes": [held]})
    assert res.evidence[0].text == f"{held}."
    # with HAGS on, the busy time after its margin (16.5 ms) never stands beside the note's raw 17.0 ms
    res = run("gpu_limit", make(system=engine(99.0), inventory=on(60.0)), stats={"avg_fps": 30.0},
              bound={"verdict": "gpu", "r": 0.495, "gpu_ms": 16.5, "ft_ms": 33.3, "vsync": True, "notes": [held]})
    assert res.evidence[0].text == f"{held}."
    assert not any("0.90 or more" in e.text for e in res.evidence)
    # r at 0.90 or more keeps its threshold line
    res = run("gpu_limit", make(system=engine(99.0)), stats={"avg_fps": 40.0},
              bound={"verdict": "gpu", "r": 0.99, "gpu_ms": 24.8, "ft_ms": 25.0})
    assert res.evidence[0].text.endswith("(r = 0.99); GPU-bound at 0.90 or more.")


def test_a_gpu_verdict_from_too_few_steady_frames_never_claims_frame_timing():
    # Under 300 steady frames bound() never computes r: the GPU's 3D engine at 95 % names the GPU instead, and the
    # evidence said "Frame timing shows the GPU as the limit", counted twice (frame timing and the engine) as likely.
    note = "only 120 steady frames over 2.0 s; a verdict needs 300 frames and 10 s"
    res = run("gpu_limit", make(system=engine(99.0)), stats={"avg_fps": 40.0},
              bound={"verdict": "gpu", "r": None, "notes": [note, "probably GPU-limited: the game kept its GPU engine "
                                                                    "99% busy (>= 95%)"]})
    texts = [e.text for e in res.evidence]
    assert not any("frame timing shows" in t.lower() for t in texts)
    fallback = f"Frame timing could not judge the limit ({note}), so the verdict comes from the GPU's 3D engine."
    assert fallback in texts
    assert is_finding(res, "medium")                     # the engine alone: possible, never likely
    # the engine line says the verdict's own 95 %, not the 90 % frame timing would have needed
    assert "The game kept its GPU's 3D engine 99% busy (95% or more): probably GPU-limited." in texts
    # nor do the other rules read it as frame timing: the CPU is ruled out by its thread alone, never by "the frame
    # rate is limited by the graphics card", and the wrong GPU gets no second point for the one reading
    bound = {"verdict": "gpu", "r": None, "notes": [note], "busiest_thread": 0.30}
    cpu = run("cpu_limit", make(system=engine(99.0)), stats={"avg_fps": 40.0}, bound=bound)
    assert isinstance(cpu, rules.Cleared) and cpu.text == "The game's busiest thread ran only 30% of a CPU."
    wrong = run("wrong_gpu", make(system=engine(99.0), adapter=INTEL), stats={"avg_fps": 40.0}, bound=bound)
    assert fallback in [e.text for e in wrong.evidence]
    assert "is probably what holds the frame rate down" in wrong.summary
    cap = run("frame_cap", make(system=engine(99.0)), stats={"avg_fps": 40.0}, bound=bound)
    assert not isinstance(cap, rules.Cleared), cap                 # the cadence was never judged
    assert not any("is what limits the frame rate" in e.text for e in wrong.evidence)


def test_the_engine_fallback_never_rules_the_cpu_out():
    # The gate and LC 3.2's grey zone read the fallback's "gpu" as a verdict frame timing never gave: a 95 % thread was
    # "Cleared: the CPU is not what limits the frame rate: it is limited by the graphics card (GPU)"
    note = "only 240 steady frames over 8.0 s; a verdict needs 300 frames and 10 s"
    bound = {"verdict": "gpu", "r": None, "notes": [note], "busiest_thread": 0.95}
    res = run("cpu_limit", make(system=engine(99.0)), stats={"avg_fps": 30.0}, bound=bound)
    assert isinstance(res, rules.Unchecked)
    assert res.text == f"whether the CPU limits the frame rate could not be judged ({note.replace(';', ',')})"
    res = run("cpu_limit", make(system=engine(99.0)), stats={"avg_fps": 30.0}, bound=dict(bound, busiest_thread=0.70))
    assert isinstance(res, rules.Unchecked) and "so it is not ruled out" in res.text        # the grey zone stays open
    held = run("cpu_throttle", make(system=cpu_sys(limit=80.0, busiest=95.0), duration=60.0), bound=bound)
    assert isinstance(held, rules.Unchecked) and held.text.startswith("whether the CPU limits the frame rate")
    # no frame timing at all: the engine's "gpu" says nothing of the CPU either
    blind = make(ok=("system",), src={"frames": NO_FRAMES}, system=engine(99.0))
    res = run("cpu_limit", blind, stats={"frames": 0}, bound={"verdict": "gpu", "busiest_thread": 0.95})
    assert isinstance(res, rules.Unchecked), res


def test_a_cpu_that_cannot_be_ruled_out_is_never_cleared_by_any_cpu_side_rule():
    # LC 3.2's grey zone and an older capture past VSync's work edge held cpu_limit open, while the cpu gate cleared
    # cpu_throttle ("the CPU is not what limits the frame rate") and not_focused ruled the CPU out in the same report
    grey = {"verdict": "waiting", "r": 0.40, "busiest_thread": 0.75}
    res = run("cpu_throttle", make(system=cpu_sys(limit=80.0, busiest=75.0), duration=60.0), bound=grey)
    assert isinstance(res, rules.Unchecked) and "so it is not ruled out" in res.text
    sys_ = synth.system_table(30, game_focused=lambda t: 0 if t <= 6.0 else 1)       # 20 % behind another window
    assert is_finding(run("not_focused", make(system=sys_), bound=grey), "medium")
    # control: no thread in the grey zone, so the verdict rules the CPU out and the gate clears it, as before (a 40 %
    # thread would not reach the gate: cpu_throttle's own "only 40% of a CPU" cancels the limit it saw)
    calm = {"verdict": "waiting", "r": 0.40}
    res = run("cpu_throttle", make(system=cpu_sys(limit=80.0, busiest=40.0), duration=60.0), bound=calm)
    assert isinstance(res, rules.Cleared), res
    assert run("not_focused", make(system=sys_), bound=calm) is None


def test_an_old_capture_past_vsyncs_work_edge_on_its_upper_bound_is_never_called_fine():
    # A capture saved before the lead thread has each tick's busiest, which reads high (four threads at 40 % read
    # 53 %): past VSync's work edge on that figure, whether the CPU holds the game there cannot be told
    note = ("VSync rounds the frame rate down to 72 FPS (refresh / 2), but whether the CPU holds it there could not "
            "be told: the busiest game thread read 53% of a CPU, 7.4 ms a frame, more than 1 refresh period(s) "
            "(6.9 ms), " + rules.fa.THREAD_UPPER_NOTE)
    bound = {"verdict": "vsync", "vsync": True, "r": 0.29, "busiest_thread": 0.53, "notes": [note]}
    res = run("cpu_limit", make(system=cpu_sys(busiest=53.0), duration=60.0), stats={"avg_fps": 72.0}, bound=bound)
    assert isinstance(res, rules.Unchecked) and res.text == note
    sys_ = synth.system_table(30, game_focused=lambda t: 0 if t <= 6.0 else 1)
    assert is_finding(run("not_focused", make(system=sys_), bound=bound), "medium")       # nor ruled out there
    # control: under the edge it is fine, as before
    calm = dict(bound, busiest_thread=0.45, notes=[])
    assert isinstance(run("cpu_limit", make(system=cpu_sys(busiest=45.0), duration=60.0), stats={"avg_fps": 72.0},
                          bound=calm), rules.Cleared)


def test_vsyncs_verdict_on_an_unsaturated_thread_is_supported_not_contradicted():
    # A6: under VSync a thread whose own time a frame passes k - 1 refreshes is the limit though it is never saturated
    # (it waits for the refresh the rest of the frame); cpu_limit then read "only 60 % of a CPU" against its own
    # verdict, and cpu_throttle "so CPU speed is not what holds it back"
    held = ("VSync rounds the frame rate down to 30 FPS (refresh / 2): the busiest game thread runs 60% of a CPU, "
            "20.0 ms a frame, more than 1 refresh period(s) (16.7 ms)")
    bound = {"verdict": "cpu_main", "vsync": True, "r": 0.15, "busiest_thread": 0.60, "notes": [held]}
    res = run("cpu_limit", make(system=cpu_sys(busiest=60.0), duration=60.0), stats={"avg_fps": 30.0}, bound=bound)
    assert is_finding(res, "high") and res.title == "One game thread is too slow for every refresh at 30 FPS"
    assert f"{held}." in [e.text for e in res.evidence] and not any("ran only" in e.text for e in res.evidence)
    # without the GPU's busy time the verdict is never counted against itself
    res = run("cpu_limit", make(system=cpu_sys(busiest=60.0), duration=60.0), stats={"avg_fps": 30.0},
              bound=dict(bound, r=None))
    assert is_finding(res, "medium") and not any("limited by" in e.text for e in res.evidence)
    res = run("cpu_throttle", make(system=cpu_sys(limit=80.0, busiest=60.0), duration=60.0), bound=bound)
    assert is_finding(res, "medium") and not any("is not what holds it back" in e.text for e in res.evidence)
    # VRR or VSync off lets the thread run past the rounding: the advice says so first
    res = run("cpu_limit", make(system=cpu_sys(busiest=60.0), duration=60.0), stats={"avg_fps": 30.0}, bound=bound)
    assert [f.title for f in res.fixes][:2] == ["Turn on VRR (G-SYNC or FreeSync) if the screen supports it",
                                                "Or turn VSync off"]
    # at 5 refreshes a frame the GPU can be busy more than 75 % of it on a CPU verdict: never "0.75 or less"
    k5 = dict(bound, r=0.78, busiest_thread=0.82)
    res = run("cpu_limit", make(system=cpu_sys(busiest=82.0), duration=60.0), stats={"avg_fps": 29.0}, bound=k5)
    assert "The GPU was busy 78% of each frame (r = 0.78)." in [e.text for e in res.evidence]
    assert not any("or less" in e.text for e in res.evidence)
    busy = dict(k5, busiest_thread=0.90)                          # saturated: not VSync's wording, nor "idle"
    res = run("cpu_limit", make(system=cpu_sys(busiest=90.0), duration=60.0), stats={"avg_fps": 29.0}, bound=busy)
    assert res.summary == "The game's CPU work takes longer than the GPU's each frame, so the CPU decides the frame rate."
    # and the headroom says nothing, never "the GPU's own time per frame was not measured"
    room = {"steady_fps": 30.0, "gpu_fps": None, "cpu_fps": [42.5, 50.0], "display_fps": 60.0, "limit": "cpu",
            "next": None, "speedup": None}
    assert rules.headroom_text(rules._Env(make(), {"bound": dict(bound, headroom=room)})) is None


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


@pytest.mark.parametrize("verdict", ["waiting", "capped", "vsync"])
@pytest.mark.parametrize("busiest, kind", [(0.599, rules.Cleared), (0.60, rules.Unchecked), (0.75, rules.Unchecked),
                                           (0.849, rules.Unchecked)])
def test_cpu_limit_is_never_cleared_in_lc_3_2s_grey_zone(verdict, busiest, kind):
    # A6 (LC 3.2): "neither" (waiting) needs the busiest thread below 0.6; up to 0.85 the CPU is not ruled out
    res = run("cpu_limit", make(system=threads(100 * busiest)), stats={"avg_fps": 45.0},
              bound={"verdict": verdict, "r": 0.23, "busiest_thread": busiest})
    assert isinstance(res, kind)
    if kind is rules.Unchecked:
        assert f"{100 * busiest:.0f}% of a CPU" in res.text and "not ruled out" in res.text


def test_the_grey_zone_holds_neither_a_gpu_bound_verdict_nor_a_frame_rate_at_the_display():
    grey = make(system=threads(75.0))
    res = run("cpu_limit", grey, stats={"avg_fps": 45.0}, bound={"verdict": "gpu", "r": 0.97, "busiest_thread": 0.75})
    assert isinstance(res, rules.Cleared)                          # r >= 0.9: the GPU limits the game
    res = run("cpu_limit", grey, stats={"avg_fps": 140.0},
              bound={"verdict": "cpu_main", "r": 0.3, "busiest_thread": 0.75})
    assert isinstance(res, rules.Cleared) and "already reaches" in res.text


# ------------------------------------------------------------------------------- headroom ---

def room(limit="gpu", nxt="cpu", speedup=(1.07, 1.33), cpu=(66.3, 78.0), gpu=62.1, steady=58.5, hz=144.0, steps=None):
    """A bound's headroom (frames.headroom) as the rules read it."""
    return {"steady_fps": steady, "gpu_fps": gpu, "cpu_fps": list(cpu) if cpu else None, "cpu_from": "thread",
            "display_fps": hz, "steps_fps": steps, "limit": limit, "next": nxt,
            "speedup": list(speedup) if speedup else None}


GPU_BOUND = {"verdict": "gpu", "r": 0.95, "gpu_ms": 16.1, "ft_ms": 16.9, "busiest_thread": 0.75}
CPU_BOUND = {"verdict": "cpu_main", "r": 0.25, "gpu_ms": 10.5, "ft_ms": 25.0, "busiest_thread": 0.95}


def limit_texts(cause, h, bound=GPU_BOUND, cap=None, fps=58.0):
    res = run(cause, cap or make(system=engine(98.0) if cause == "gpu_limit" else threads(95.0)),
              stats={"avg_fps": fps}, bound={**bound, "headroom": h})
    assert is_finding(res), res
    return [e.text for e in res.evidence]


def test_a_gpu_limit_says_how_far_off_the_cpus_limit_is():
    # DESIGN's example sentence (Headroom): second, after the frame-timing line, with what it rests on said last
    texts = limit_texts("gpu_limit", room())
    assert texts[1] == ("At these settings the CPU could feed about 66-78 FPS, so a GPU about 1.1-1.3x faster would "
                        "make the CPU the limit.")
    assert texts[-1] == labels.HEADROOM_NOTE and texts[0].startswith("The GPU was busy 16.1 ms of each 16.9 ms frame")


@pytest.mark.parametrize("nxt, speedup, cpu, text", [
    ("display", (1.4, 1.44), (283.0, 333.0), "At these settings the CPU could feed about 283-333 FPS, more than the "
                                             "144 Hz screen shows, so a GPU about 1.4x faster would reach the screen's "
                                             "limit."),
    ("cpu_or_display", (1.6, 1.7), (131.0, 154.0), "At these settings the CPU could feed about 131-154 FPS, so a GPU "
                                                   "about 1.6-1.7x faster would make the CPU or the 144 Hz screen the "
                                                   "limit."),
    ("cpu", (0.99, 1.24), (59.0, 72.0), "At these settings the CPU could feed about 59-72 FPS, so a GPU at most about "
                                        "1.2x faster would make the CPU the limit."),
    ("cpu", (1.0, 1.04), (58.5, 61.0), "At these settings the CPU could feed about 58-61 FPS, so it is at its limit "
                                       "too: a faster GPU alone would gain little."),
    ("display", (9.96, 10.4), (1100.0, 1300.0), "At these settings the CPU could feed about 1,100-1,300 FPS, more than "
                                                "the 144 Hz screen shows, so a GPU about 10x faster would reach the "
                                                "screen's limit."),       # 9.96 rounds to 10: one form for both ends
])
def test_a_gpu_limit_names_the_next_limit_in_plain_words(nxt, speedup, cpu, text):
    assert limit_texts("gpu_limit", room(nxt=nxt, speedup=speedup, cpu=cpu))[1] == text


def test_a_cpu_limit_says_how_far_off_the_gpus_limit_is():
    one = limit_texts("cpu_limit", room("cpu", "gpu", (2.2, 2.4), (40.0, 42.0), 95.0, 40.0), CPU_BOUND, fps=40.0)
    assert one[1] == ("At these settings the GPU could draw about 95 FPS, so a CPU about 2.2-2.4x faster per core would "
                      "make the GPU the limit.") and one[-1] == labels.HEADROOM_NOTE
    every = limit_texts("cpu_limit", room("cpu", "display", (2.9, 3.0), (48.0, 50.0), 2215.0, 48.0),
                        {**CPU_BOUND, "verdict": "cpu_all"}, fps=48.0)
    assert every[1] == ("At these settings the GPU could draw about 2,215 FPS, more than the 144 Hz screen shows, so a "
                        "CPU about 2.9-3.0x faster would reach the screen's limit.")     # every core: no "per core"
    # its GPU busy within the HAGS margin (G = 0): no ceiling to give, but never the next limit either
    idle = limit_texts("cpu_limit", room("cpu", "display", (3.4, 3.6), (40.0, 42.0), None, 40.0),
                       {**CPU_BOUND, "gpu_ms": 0.0}, fps=40.0)
    assert idle[1] == ("At these settings the GPU was nearly idle, so a CPU about 3.4-3.6x faster per core would reach "
                       "the 144 Hz screen's limit.") and idle[-1] == labels.HEADROOM_NOTE


def test_without_the_other_side_the_headroom_says_it_is_unknown_and_claims_no_figures():
    texts = limit_texts("gpu_limit", room(nxt=None, speedup=None, cpu=None))
    assert texts[1] == "How much a faster GPU would help is unknown: the game's thread CPU use was not measured."
    assert labels.HEADROOM_NOTE not in texts
    texts = limit_texts("cpu_limit", room("cpu", None, None, (40.0, 42.0), None, 40.0), CPU_BOUND, fps=40.0)
    assert texts[1] == "How much a faster CPU would help is unknown: the GPU's own time per frame was not measured."
    shared = make(system=synth.system_table(30, game_top_thread_pct=95.0, gpu_other_pct=40.0))
    texts = limit_texts("cpu_limit", room("cpu", None, None, (40.0, 42.0), None, 40.0), CPU_BOUND, cap=shared, fps=40.0)
    assert texts[1] == ("How much a faster CPU would help is unknown: other programs shared the game's GPU, so its own "
                        "time per frame says too little.")


def test_a_borderline_verdict_gives_both_ceilings():
    texts = limit_texts("gpu_limit", room(None, None, None, (62.0, 73.0), 70.0, 60.0),
                        {**GPU_BOUND, "verdict": "mixed", "r": 0.82}, cap=make(system=engine(70.0)), fps=60.0)
    assert texts[1] == ("At these settings the GPU could draw about 70 FPS and the CPU could feed about 62-73 FPS, "
                        "against the 60 FPS it ran at.")
    # borderline on r alone, the CPU far from its limit: the figures say so, never "both close"
    texts = limit_texts("gpu_limit", room(None, None, None, (170.0, 200.0), 72.0, 60.0),
                        {**GPU_BOUND, "verdict": "mixed", "r": 0.83}, cap=make(system=engine(70.0)), fps=60.0)
    assert texts[1].endswith("the CPU could feed about 170-200 FPS, against the 60 FPS it ran at.")


def test_a_screen_with_frames_on_whole_refreshes_names_its_even_steps_after_the_headroom():
    steps = [144.0 / k for k in range(1, 8)]                 # 144, 72, 48, 36, 28.8, 24, 20.6
    assert limit_texts("gpu_limit", room(steps=steps))[2] == (
        "On this 144 Hz screen each frame shows for whole refreshes, so the pace is even at 144, 72 or 48 FPS and "
        "uneven in between.")
    on_a_step = limit_texts("gpu_limit", room(steady=71.5, cpu=(80.0, 96.0), steps=steps))[2]   # within 1 % of 72
    assert "even at 144 or 72 FPS and" in on_a_step
    assert not any("whole refreshes" in t for t in limit_texts("gpu_limit", room(steps=None)))


def test_the_headroom_lines_are_context_and_never_move_the_score():
    cap = make(system=engine(98.0))
    plain = run("gpu_limit", cap, stats={"avg_fps": 58.0}, bound=GPU_BOUND)
    with_room = run("gpu_limit", cap, stats={"avg_fps": 58.0}, bound={**GPU_BOUND, "headroom": room(steps=[144.0, 72.0])})
    assert (plain.confidence, plain.score) == (with_room.confidence, with_room.score)
    assert len(with_room.evidence) == len(plain.evidence) + 3                    # the sentence, the steps, the note
    # a cleared cause carries no headroom: the game already reaches the screen
    res = run("gpu_limit", cap, stats={"avg_fps": 140.0}, bound={**GPU_BOUND, "headroom": room()})
    assert isinstance(res, rules.Cleared) and "could feed" not in res.text


def heat(clock=780, top=2100, watts=61.0, limit=102.8, reasons="sw_thermal_slowdown", share=97.0, idle=0):
    """Made-up NVIDIA readings of a laptop GPU held down by heat, the first `idle` of them idle at 210 MHz."""
    return synth.gpu_table(30, nv_util_pct=99.0, nv_throttle_pct=share, nv_power_cap_pct=0.0, nv_temp_c=88.0,
                           nv_clock_mhz=lambda t: 210 if t <= idle else clock, nv_clock_max_mhz=top,
                           nv_power_w=watts, nv_power_limit_w=limit,
                           nv_reasons=lambda t: "gpu_idle" if t <= idle else reasons)


def nv_cap(gpu, **inventory):
    return make(adapter=NV, ok=("frames", "system", "gpu"), system=engine(98.0), gpu=gpu,
                inventory={"nvidia": {"clock_max_mhz": 2100, "temp_target_c": 87, "power_default_w": 80.0},
                           **inventory})


def test_a_gpu_held_down_by_heat_says_how_deep_and_that_cooling_may_be_enough():
    texts = limit_texts("gpu_limit", room(), cap=nv_cap(heat()))
    assert texts[1] == ("At these settings the CPU could feed about 66-78 FPS, so a GPU about 1.1-1.3x faster would make "
                        "the CPU the limit; heat held this one at 37% of its top clock, so keeping it cooler may be "
                        "enough.")
    assert ("The GPU was also held down by heat 97% of the time, at a median 780 of its 2,100 MHz top clock (see GPU "
            "throttling).") in texts


@pytest.mark.parametrize("clock, reasons, named", [
    (1800, "sw_thermal_slowdown", False),                   # 2,100 / 1,800 = 1.17 of clock, short of the 1.33 needed
    (780, "sw_thermal_slowdown,hw_power_brake_slowdown", False),   # the power brake: cooling may not lift it
    (780, "sw_thermal_slowdown", True),
])
def test_cooling_is_named_only_where_the_clock_held_back_covers_the_speedup_and_heat_alone_held_it(clock, reasons, named):
    texts = limit_texts("gpu_limit", room(), cap=nv_cap(heat(clock=clock, reasons=reasons)))
    assert ("keeping it cooler may be enough" in texts[1]) is named


def test_gpu_throttling_says_how_deep_the_clock_and_the_power_went():
    res = run("gpu_throttle", nv_cap(heat()), stats={"avg_fps": 58.0}, bound=GPU_BOUND)
    assert is_finding(res, "high")
    assert res.evidence[1].text == ("While heat held it down, it ran at a median 780 MHz, 37% of its 2,100 MHz top "
                                    "clock, drawing 61 W of its 103 W power limit, so heat held it back, not its power "
                                    "limit.")
    # drawing its whole limit as well: then power is not ruled out
    res = run("gpu_throttle", nv_cap(heat(watts=101.5)), stats={"avg_fps": 58.0}, bound=GPU_BOUND)
    assert res.evidence[1].text.endswith("drawing 102 W, at its 103 W power limit.")


def test_a_gpu_at_a_lowered_power_cap_says_how_deep_it_ran_there():
    # LC 4.2's power-limit variant (Armoury Crate's quiet mode): the cap at 55 W of an 80 W default, named by the reasons
    gpu = synth.gpu_table(30, nv_util_pct=99.0, nv_throttle_pct=0.0, nv_power_cap_pct=98.0, nv_temp_c=70.0,
                          nv_clock_mhz=1200, nv_clock_max_mhz=2100, nv_power_w=54.8, nv_power_limit_w=55.0,
                          nv_reasons="sw_power_cap")
    res = run("gpu_throttle", nv_cap(gpu), stats={"avg_fps": 58.0}, bound=GPU_BOUND)
    assert is_finding(res)
    assert res.evidence[1].text == ("At its power cap, it ran at a median 1,200 MHz, 57% of its 2,100 MHz top clock, "
                                    "drawing 55 W, at its 55 W power limit.")


def test_the_clock_depth_leaves_idle_readings_out():
    # 16 of 30 readings idle at 210 MHz (gpu_idle): an idle GPU clocks down by design, so they say nothing of the depth
    cap = nv_cap(heat(idle=16))
    assert attribute.nvidia_depth(cap)["clock_mhz"] == 780.0 and attribute.nvidia_depth(cap)["readings"] == 14
    assert attribute.nvidia_stats(cap) == {"gpu_clock_mhz": 780.0, "gpu_clock_max_mhz": 2100.0, "gpu_power_w": 61.0,
                                           "gpu_power_limit_w": 102.8}


def test_the_clock_depth_takes_the_inventorys_top_clock_and_drops_absurd_readings():
    gpu = heat()
    gpu[G.CLOCK_MAX_MHZ] = [None] * len(gpu[G.T])            # an older capture: the top clock only in the inventory
    gpu[G.POWER_W] = [1e9 if i < 20 else 61.0 for i in range(len(gpu[G.T]))]    # 20 of 30 corrupt: missing, not 1 GW
    gpu[G.CLOCK_MHZ][24] = 5e6
    d = attribute.nvidia_depth(nv_cap(gpu))
    assert d["clock_max_mhz"] == 2100 and d["readings"] == 29 and d["power_w"] == 61.0
    assert attribute.nvidia_depth(make()) is None                                   # no NVIDIA readings at all


# ------------------------------------------------------------------------ GPU contention ---

def contention(other_pct, game_pct=18.0, others=None):
    sys_ = synth.system_table(30, gpu_other_pct=other_pct, game_gpu_pct=game_pct, game_top_thread_pct=30.0)
    rows = others if others is not None else \
        [[0.25 * k, [[7777, "python.exe", 100.0, 0.0, 100.0], [2656, "dwm.exe", 3.0, 0.0, 3.0]]] for k in range(1, 121)]
    return make(adapter=NV, system=sys_, others=rows)


STARVED = {"verdict": "capped", "r": 0.2, "cap_fps": 58.0, "busiest_thread": 0.3, "gpu_ms": 3.4, "ft_ms": 17.2}


@pytest.mark.parametrize("pct, kind", [(10.0, Finding), (9.9, rules.Cleared)])
def test_gpu_contention_from_ten_percent_of_the_games_gpu(pct, kind):
    # A7, LC 4.6: another process on the game adapter's engine >= 10 %
    res = run("gpu_contention", contention(pct), stats={"avg_fps": 130.0}, bound={"verdict": "gpu", "r": 0.95})
    assert isinstance(res, kind)
    if kind is Finding:
        assert res.confidence == "medium"


def test_gpu_contention_with_the_game_starved_is_likely_and_names_the_other_gpu_user_with_a_caveat():
    res = run("gpu_contention", contention(80.0), stats={"avg_fps": 58.0}, bound=STARVED)
    assert is_finding(res, "high") and res.title == "Another program kept the game's GPU 80% busy: python.exe"
    text = " ".join(e.text for e in res.evidence)
    assert "80% busy" in text and "58 FPS on a 144 Hz screen" in text
    assert "python.exe (process 7777" in text and "covers any GPU" in text


def test_a_game_held_by_its_own_cpu_thread_is_not_starved_of_the_gpu():
    res = run("gpu_contention", contention(80.0), stats={"avg_fps": 58.0}, bound=dict(STARVED, busiest_thread=0.85))
    assert is_finding(res, "medium")                               # the contention alone, without the starving
    res = run("gpu_contention", contention(80.0), stats={"avg_fps": 58.0}, bound=dict(STARVED, busiest_thread=0.84))
    assert is_finding(res, "high")


def on_the_games_adapter(game_gpu):
    """The others rows as the sampler writes them now (A10): create time, then each process's busiest engine on the
    game's own adapter (gpu_game_pct, field 7). The training job runs on the other GPU: 100 % there, none here."""
    return [[0.25 * k, [[7777, "python.exe", 100.0, 0.0, 100.0, 134000000000007777, None],
                        [2656, "dwm.exe", 3.0, 0.0, 3.0, 134000000000002656, game_gpu]]] for k in range(1, 121)]


def test_gpu_contention_names_the_user_of_the_games_own_gpu_where_the_table_says_it():
    # Item 12 (A10): field 7 is the game adapter's engine; field 5 covers any adapter and blamed the job on the other
    res = run("gpu_contention", contention(80.0, others=on_the_games_adapter(12.0)), stats={"avg_fps": 58.0},
              bound=STARVED)
    assert is_finding(res, "high") and res.title == "Another program kept the game's GPU 80% busy: dwm.exe"
    text = " ".join(e.text for e in res.evidence)
    assert "The busiest other user of the game's GPU in the process table was dwm.exe (process 2656, its busiest " \
           "engine there 12% busy, median)." in text
    assert "python.exe" not in text and "covers any GPU" not in text
    nobody = run("gpu_contention", contention(80.0, others=on_the_games_adapter(0.0)), stats={"avg_fps": 58.0},
                 bound=STARVED)
    assert nobody.title == "Another program kept the game's GPU 80% busy"   # field 7 says 0: nobody is named
    assert not any("python.exe" in e.text for e in nobody.evidence)


def test_gpu_contention_never_names_the_game_or_lag_doctor():
    rows = [[0.25 * k, [[4242, "game.exe", 50.0, 0.0, 100.0], [5555, "python.exe", 30.0, 0.0, 90.0],
                        [6000, "PresentMon-2.6.0-x64.exe", 5.0, 0.0, 80.0]]] for k in range(1, 121)]
    cap = contention(80.0, others=rows)
    cap.target.update(pids=[4242], tool_pids=[5555],
                      presentmon={"exe": r"C:\Users\x\AppData\Local\lag-doctor\PresentMon-2.6.0-x64.exe"})
    res = run("gpu_contention", cap, stats={"avg_fps": 58.0}, bound=STARVED)
    assert is_finding(res) and res.title == "Another program kept the game's GPU 80% busy"
    assert not any(n in e.text for e in res.evidence for n in ("game.exe", "python.exe", "PresentMon"))


def test_a_shared_gpu_is_never_checked_and_fine_as_the_limit_nor_read_as_a_frame_cap():
    # A7: other programs' GPU work makes a steady, low frame rate that reads like a cap with the GPU idle
    cap = contention(80.0)
    gpu = run("gpu_limit", cap, stats={"avg_fps": 58.0}, bound=STARVED)
    assert isinstance(gpu, rules.Unchecked) and "80% busy" in gpu.text
    assert isinstance(run("frame_cap", cap, stats={"avg_fps": 58.0}, bound=STARVED), rules.Unchecked)
    quiet = contention(2.0)                                         # control: the same capture without contention
    assert isinstance(run("gpu_limit", quiet, stats={"avg_fps": 58.0}, bound=STARVED), rules.Cleared)
    assert is_finding(run("frame_cap", quiet, stats={"avg_fps": 58.0}, bound=STARVED))


def test_a_shared_gpu_holds_the_gated_verdict_open_too():
    # the hold runs after the gate: a GPU-limit finding the "capped" verdict gates off is no "checked and fine"
    # either (the engine busy and NVIDIA 99 % busy make the finding; contention makes the cap verdict unsure)
    cap = make(adapter=NV, ok=("frames", "system", "gpu"), gpu=synth.gpu_table(30, nv_util_pct=99.0),
               system=synth.system_table(30, gpu_other_pct=40.0, game_gpu_pct=95.0, game_top_thread_pct=30.0))
    res = run("gpu_limit", cap, stats={"avg_fps": 58.0}, bound=STARVED)
    assert isinstance(res, rules.Unchecked) and "40% busy" in res.text
    cap.system = synth.system_table(30, gpu_other_pct=2.0, game_gpu_pct=95.0, game_top_thread_pct=30.0)
    res = run("gpu_limit", cap, stats={"avg_fps": 58.0}, bound=STARVED)      # control: the gate clears it
    assert isinstance(res, rules.Cleared) and res.text.startswith("The GPU is not what limits the frame rate")


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


@pytest.mark.parametrize("reasons", ["sw_power_cap", "applications_clocks_setting,sw_power_cap",
                                     "sync_boost,sw_power_cap", ""])
def test_gpu_throttle_at_the_power_limit_alone_is_normal_not_a_fault(reasons):
    # The collector keeps the SW power cap out of nv_throttle_pct (its own column, nv_power_cap_pct, LC 4.1, 4.2).
    # This test used to put the cap's share in nv_throttle_pct and lean on the reason names, which one idle
    # second (a gpu_idle sample) defeated; whatever the reasons say, the cap share is never throttling now.
    # Readings that name gpu_idle are left out altogether now (item 8): test_an_idle_gpu_is_never_read_as_throttled.
    cap = make(adapter=NV, ok=("frames", "system", "gpu"),
               gpu=nv(0.0, nv_power_cap_pct=80.0, nv_reasons=reasons, nv_power_limit_w=88.8))
    res = run("gpu_throttle", cap, bound={"verdict": "gpu"})
    assert isinstance(res, rules.Cleared) and "power brake only 0%" in res.text


def idle_every(k, working, idle=100.0, working_reasons="sw_thermal_slowdown"):
    """30 NVIDIA readings, every k-th one idle (gpu_idle, its throttle share `idle`), the others at `working`."""
    return nv(lambda t: idle if int(t) % k == 0 else working,
              nv_reasons=lambda t: "gpu_idle" if int(t) % k == 0 else working_reasons)


def test_an_idle_gpu_is_never_read_as_throttled():
    # Item 8: NVML's throttle counters keep growing while the GPU idles. In the first real capture its 5 idle readings
    # (gpu_idle, no heat named) read 99-100 % each, so idle seconds counted as heat. 20 readings at 10 % and 10 idle
    # at 100 % once averaged 40 % ("held down 40% of the time"); they are 10 %
    cap = make(adapter=NV, ok=("frames", "system", "gpu"), gpu=idle_every(3, 10.0))
    res = run("gpu_throttle", cap, bound={"verdict": "gpu"})
    assert isinstance(res, rules.Cleared) and "power brake only 10% of the time" in res.text
    working = make(adapter=NV, ok=("frames", "system", "gpu"), gpu=idle_every(3, 40.0))
    assert is_finding(run("gpu_throttle", working, bound={"verdict": "gpu"}), "high")   # control: real throttling
    for reasons in ("gpu_idle", "gpu_idle,sw_power_cap"):     # idle in every reading: nothing to tell either way
        cap = make(adapter=NV, ok=("frames", "system", "gpu"),
                   gpu=nv(100.0, nv_power_cap_pct=80.0, nv_reasons=reasons, nv_power_limit_w=88.8))
        res = run("gpu_throttle", cap, bound={"verdict": "gpu"})
        assert isinstance(res, rules.Unchecked) and res.text == ("the NVIDIA GPU was idle in all of its 30 readings, "
                                                                 "so whether heat held it down could not be told")


def test_the_per_event_gpu_heat_signal_leaves_idle_readings_out_too():
    # Item 8: the per-event check reads the same readings as the rule (attribute.throttle_readings), so an idle
    # second at 100 % is no "GPU slowing down" at a stutter either
    from lagdoctor.analysis import attribute
    cap = make(adapter=NV, gpu=idle_every(3, 10.0))
    heat = attribute._gpu_heat(cap)
    assert [v for t, v in zip(heat.t, heat.v) if int(t) % 3 == 0] == [None] * 10
    assert {v for t, v in zip(heat.t, heat.v) if int(t) % 3} == {10.0}


def cap_named_in(k, share=90.0, limit_w=35.0):
    """30 readings at the SW power cap's counter `share`, the cap named in the reasons of every k-th one."""
    return make(adapter=NV, ok=("frames", "system", "gpu"), inventory={"nvidia": {"temp_target_c": 75,
                                                                                    "power_default_w": 80.0}},
                gpu=nv(0.0, nv_power_cap_pct=share, nv_power_limit_w=limit_w,
                       nv_reasons=lambda t: "sw_power_cap" if int(t) % k == 0 else ""))


def test_the_power_cap_share_counts_only_readings_that_named_the_cap():
    # Item 9: on driver 591.86 the counter behind nv_power_cap_pct (NVML field 74) read 42 % over 169 readings that never
    # named sw_power_cap. 30 readings at 90 % with the cap named in 6 of them is 18 %, under 30 %: no power-limit variant
    res = run("gpu_throttle", cap_named_in(5), bound={"verdict": "gpu"})
    assert isinstance(res, rules.Cleared) and "power brake only 0%" in res.text
    half = run("gpu_throttle", cap_named_in(2), bound={"verdict": "gpu"})       # 15 of 30 named: 45 %
    assert is_finding(half, "medium")
    line = half.evidence[0].text
    assert line.startswith("The GPU sat at its power cap 45% of the time with the cap at 35 W, below its 80 W default")
    assert "counting only the 15 of 30 readings that named it" in line
    every = run("gpu_throttle", cap_named_in(1), bound={"verdict": "gpu"})      # the counter and the reasons agree
    assert is_finding(every, "medium") and "counting only" not in every.evidence[0].text


@pytest.mark.parametrize("reasons,held", [
    ("sw_thermal_slowdown", "heat"),                                  # this laptop in FF7 Rebirth: 840 of 845 readings
    ("sw_thermal_slowdown,sw_power_cap", "heat"),
    ("hw_thermal_slowdown", "heat"),
    ("sw_thermal_slowdown,hw_power_brake_slowdown", "heat or its power brake"),
    ("sw_thermal_slowdown,hw_slowdown", "heat or its power brake"),  # HW slowdown may be either
    ("sw_power_cap", "heat or its power brake"),                      # the share counts heat and brake; neither named
])
def test_gpu_throttle_names_only_what_the_readings_named(reasons, held):
    cap = make(adapter=NV, ok=("frames", "system", "gpu"), gpu=nv(80.0, nv_reasons=reasons))
    res = run("gpu_throttle", cap, bound={"verdict": "gpu"})
    assert is_finding(res, "high") and res.evidence[0].text.startswith(f"The NVIDIA GPU was held down by {held} 80%")
    assert rules.attribute.held_by(make(adapter=NV, gpu=synth.gpu_table(30, nv_throttle_pct=80.0))) \
        == "heat or its power brake"                                     # no reasons recorded: either


@pytest.mark.parametrize("reasons", ["sw_power_cap", ""])
def test_gpu_throttle_reads_the_heat_and_brake_share_whatever_the_reason_names_say(reasons):
    # nv_throttle_pct is heat plus the HW power brake only, a direct readout (LC 4.2): no reason-name heuristic
    # may turn it into "the normal power limit". Only gpu_idle leaves a reading out (item 8: the counters keep growing
    # at idle), in test_an_idle_gpu_is_never_read_as_throttled
    cap = make(adapter=NV, ok=("frames", "system", "gpu"), gpu=nv(80.0, nv_reasons=reasons))
    res = run("gpu_throttle", cap, bound={"verdict": "gpu"})
    assert is_finding(res, "high") and "heat or its power brake 80%" in res.evidence[0].text


def test_the_power_cap_is_context_and_the_power_limit_variant_needs_a_limit_below_its_default():
    # LC 4.2: sw_power_cap counts only with the enforced limit under the GPU's default (battery, a quiet mode)
    def cap_of(limit_w, default_w):
        inv = {"nvidia": {"temp_target_c": 75, "power_default_w": default_w}} if default_w else None
        return make(adapter=NV, ok=("frames", "system", "gpu"), inventory=inv,
                    gpu=nv(0.0, nv_power_cap_pct=90.0, nv_reasons="sw_power_cap", nv_power_limit_w=limit_w))
    low = run("gpu_throttle", cap_of(35.0, 80.0), bound={"verdict": "gpu"})
    assert is_finding(low, "medium") and "below its 80 W default" in low.evidence[0].text
    for limit, default in ((88.8, 80.0), (80.0, 80.0), (35.0, None)):        # above, at, or default unknown
        res = run("gpu_throttle", cap_of(limit, default), bound={"verdict": "gpu"})
        assert isinstance(res, rules.Cleared), (limit, default)


def saved_before_the_power_cap_split(share, reasons, heat_every=0):
    """An NVIDIA table as the earlier build wrote it: nv_throttle_pct held the SW power cap too, and there is no
    nv_power_cap_pct column at all (DECISIONS 2026-11-04). Every `heat_every`-th row names a thermal slowdown."""
    g = nv(share, nv_reasons=lambda t: "sw_thermal_slowdown,sw_power_cap" if heat_every and int(t) % heat_every == 0
           else reasons)
    del g[G.POWER_CAP_PCT]
    return g


def test_an_earlier_captures_power_cap_is_never_read_as_throttling(tmp_path):
    # Item 11: `lagdoctor analyze` on a capture saved before the split read a GPU-bound laptop's normal power cap
    # (sw_power_cap in every reading, 100 % of the time) as "likely throttling", through the real load path
    from lagdoctor.analysis.diagnose import diagnose
    from lagdoctor.model import Capture
    cap = make(adapter=NV, ok=("frames", "system", "gpu"), gpu=saved_before_the_power_cap_split(100.0, "sw_power_cap"),
               inventory={"power": dict(POWER)})
    cap.frames = synth.frames_table([20.0] * 1500, gpu_ms=19.6)
    cap.system = synth.system_table(30, game_gpu_pct=97.0, game_top_thread_pct=40.0)
    loaded = Capture.load(cap.save(tmp_path / "old.ldcap.json.gz"))
    assert G.POWER_CAP_PCT not in loaded.gpu                              # the column is missing, not None
    d = diagnose(loaded)
    assert d.stats["bound"] == "gpu" and "gpu_throttle" not in {f.cause for f in d.findings}
    assert d.unchecked["gpu_throttle"].startswith("this capture was saved by an earlier lag-doctor")
    plan = next(f for f in d.findings if f.cause == "power_plan")          # the quiet plan, but no GPU support
    assert not any("GPU" in e.text for e in plan.evidence)


@pytest.mark.parametrize("heat_every, kind", [(2, Finding), (4, rules.Unchecked)])
def test_an_earlier_capture_counts_only_the_readings_that_named_heat_or_the_brake(heat_every, kind):
    # Item 11: half the readings naming a thermal slowdown at 100 % count as 50 % (30 % or more); 7 of 30, 23 %
    cap = make(adapter=NV, ok=("frames", "system", "gpu"), gpu=saved_before_the_power_cap_split(100.0, "sw_power_cap",
                                                                                                 heat_every))
    res = run("gpu_throttle", cap, bound={"verdict": "gpu"})
    assert isinstance(res, kind)
    if kind is Finding:
        assert res.confidence == "high" and "counting only the 15 of 30 readings that named one" in res.evidence[0].text
    else:
        assert "only 7 of 30 readings named heat or the power brake" in res.text


def test_an_earlier_captures_low_share_still_rules_throttling_out():
    # the old share held the power cap as well, so under 30 % it bounds heat and the brake from above
    cap = make(adapter=NV, ok=("frames", "system", "gpu"), gpu=saved_before_the_power_cap_split(20.0, "sw_power_cap"))
    res = run("gpu_throttle", cap, bound={"verdict": "gpu"})
    assert isinstance(res, rules.Cleared) and res.text == ("The GPU was held down by heat or its power brake only 20% "
                                                           "of the time (under 30%, the power cap included).")


def test_the_heat_reason_names_cover_the_collectors():
    from lagdoctor.analysis import attribute
    from lagdoctor.capture import gpumon
    assert set(gpumon.THROTTLE_REASONS) <= set(attribute.HEAT_REASONS)
    assert not set(gpumon.POWER_CAP_REASONS) & set(attribute.HEAT_REASONS)


def test_power_plan_never_counts_the_gpu_as_support():
    # Changed on purpose: GPU heat counted here once, and made a plan that set the CPU exactly as Balanced does
    # the top cause ([likely]). A Windows plan sets no GPU limit; a hot GPU is gpu_throttle's evidence.
    quiet = {"power": dict(POWER)}
    for gpu in (nv(0.0, nv_power_cap_pct=100.0, nv_reasons="sw_power_cap"), nv(60.0), nv(100.0)):
        res = run("power_plan", make(adapter=NV, ok=("frames", "system", "gpu"), inventory=quiet, gpu=gpu))
        assert is_finding(res, "medium") and not any("GPU" in e.text for e in res.evidence)


# This laptop's ASUS "Silent" plan, read on 2026-11-05: the CPU set exactly as Balanced sets it.
SILENT_SETTINGS = {"max_cpu_ac": 100, "max_cpu_dc": 100, "max_cpu1_ac": 100, "max_cpu1_dc": 100, "boost_ac": 2,
                   "boost_dc": 2, "cooling_ac": 1, "cooling_dc": 0}
ARMOURY = {"tools": ["ArmouryCrate.Service.exe", "ArmouryCrate.UserSessionHelper.exe"]}


def test_a_plan_is_judged_by_its_settings_not_its_name():
    res = run("power_plan", make(inventory={"power": dict(POWER, plan=SILENT_SETTINGS)}))
    assert isinstance(res, rules.Cleared) and 'plan "Silent", whose own settings let the CPU run at full speed' in res.text
    for change, said in (({"max_cpu_ac": 50}, 'The active power plan "Silent" caps the CPU at 50% of its speed.'),
                         ({"boost_ac": 0}, 'The active power plan "Silent" turns CPU boost off.'),
                         ({"cooling_ac": 0}, 'The active power plan "Silent" slows the CPU before it speeds up the fans.')):
        res = run("power_plan", make(inventory={"power": dict(POWER, plan=dict(SILENT_SETTINGS, **change))}))
        assert is_finding(res, "medium") and said in res.evidence[0].text, change
        assert [f.fix_id for f in res.fixes if f.fix_id] == ["power_plan"]          # then the switch is offered
    named = {"power": dict(POWER, scheme_name="Max power", plan=dict(SILENT_SETTINGS, boost_ac=0, max_cpu_ac=99))}
    res = run("power_plan", make(inventory=named))
    assert 'plan "Max power" caps the CPU at 99% of its speed and turns CPU boost off' in res.evidence[0].text


def test_armoury_crates_quiet_mode_is_read_through_the_plan_it_switches_to():
    # This laptop, as its next capture will read it: "Silent" set the CPU as Balanced does, but Armoury Crate switches
    # the Windows plan to "Silent" in its quiet mode (163 switches in the System log), which lowers the fan speed and
    # power limits; here the GPU's limit sat at 55 W against an 80 W default.
    inv = {"power": dict(POWER, plan=SILENT_SETTINGS), "oem": ARMOURY}
    res = run("power_plan", make(inventory=inv))
    assert is_finding(res, "medium") and res.title == "Armoury Crate's quiet mode is holding the laptop back"
    assert res.evidence[0].text.startswith("Armoury Crate's quiet mode looks to be on: the active power plan \"Silent\"")
    assert [(f.title, f.fix_id) for f in res.fixes] == [("Set Armoury Crate's operating mode to Performance or Turbo",
                                                         None)]     # no switch of Windows plan: it would not change it
    capped = make(adapter=NV, ok=("frames", "system", "gpu"), gpu=synth.gpu_table(30, nv_power_limit_w=55.0),
                  inventory=dict(inv, nvidia={"power_default_w": 80.0}))
    res = run("power_plan", capped)
    assert is_finding(res, "high")
    assert "The GPU's power limit sat at 55 W for most of the capture, below its 80 W default." in [e.text for e in res.evidence]
    at_default = make(adapter=NV, ok=("frames", "system", "gpu"), gpu=synth.gpu_table(30, nv_power_limit_w=80.0),
                      inventory=dict(inv, nvidia={"power_default_w": 80.0}))
    assert is_finding(run("power_plan", at_default), "medium")
    # Without Armoury Crate running, the same plan is only a name: its settings hold nothing back.
    assert isinstance(run("power_plan", make(inventory={"power": dict(POWER, plan=SILENT_SETTINGS)})), rules.Cleared)
    # An older capture, without the plan's settings: the same quiet mode, still no switch of plan.
    res = run("power_plan", make(inventory={"power": dict(POWER), "oem": ARMOURY}))
    assert is_finding(res) and not any(f.fix_id for f in res.fixes) and len(res.fixes) == 1


def test_a_plan_that_caps_the_performance_cores_counts_only_on_a_hybrid_cpu():
    # Windows' own Power saver keeps the maximum state at 100 % and caps efficiency class 1 (the P-cores) at 75 %.
    plan = dict(SILENT_SETTINGS, max_cpu1_ac=75, max_cpu1_dc=75, cooling_ac=1)
    hybrid = run("power_plan", make(inventory={"power": dict(POWER, plan=plan), "cpu": {"hybrid": True}}))
    assert is_finding(hybrid) and "caps the CPU's performance cores at 75% of their speed" in hybrid.evidence[0].text
    for cpu in ({"hybrid": False}, {}):
        assert isinstance(run("power_plan", make(inventory={"power": dict(POWER, plan=plan), "cpu": cpu})), rules.Cleared)


@pytest.mark.parametrize("change,counts", [({"max_cpu_ac": 50}, False), ({"boost_ac": 0}, False),
                                           ({"cooling_ac": 0}, True), ({}, False)])
def test_gpu_throttle_takes_no_cpu_only_plan_setting_as_a_hint(change, counts):
    # A Windows plan's CPU cap or boost off sets no GPU limit; passive cooling or the maker's quiet mode do touch it.
    # Borderline on purpose: the target temperature (+2) against a low throttle share (-2), so a hint (+1) tips it.
    inv = {"nvidia": {"temp_target_c": 75}, "power": dict(POWER, plan=dict(SILENT_SETTINGS, **change))}
    cap = make(adapter=NV, ok=("frames", "system", "gpu"), gpu=nv(10.0, nv_temp_c=80.0), inventory=inv)
    res = run("gpu_throttle", cap, bound={"verdict": "gpu"})
    assert is_finding(res) is counts, change
    if counts:
        assert any("fans" in e.text for e in res.evidence)


def test_on_battery_the_plans_battery_settings_count_and_passive_cooling_is_windows_own():
    on_battery = dict(POWER, on_battery=True, battery_pct=60, plan=dict(SILENT_SETTINGS, max_cpu_dc=80))
    texts = " ".join(e.text for e in run("power_plan", make(inventory={"power": on_battery})).evidence)
    assert "caps the CPU at 80% of its speed" in texts and "fans" not in texts and "on battery" in texts
    # On battery with a plan that holds nothing back: plug in, yes; a switch of plan, no (whatever its name).
    res = run("power_plan", make(inventory={"power": dict(on_battery, plan=SILENT_SETTINGS)}))
    assert is_finding(res) and "power plan" not in res.evidence[0].text.lower()
    assert [f.title for f in res.fixes] == ["Plug in the charger while gaming"]


@pytest.mark.parametrize("plan", [{}, {"max_cpu_ac": None, "boost_ac": None, "cooling_ac": None},
                                  {"max_cpu_ac": True, "boost_ac": "0", "cooling_ac": 7, "max_cpu_dc": -1}])
def test_without_readable_settings_only_the_plans_name_is_to_go_on(plan):
    # An older capture, or Windows would not say: a quiet-sounding name stays a medium finding, as before.
    res = run("power_plan", make(inventory={"power": dict(POWER, plan=plan)}))
    assert is_finding(res, "medium") and "a quiet, power-saving plan" in res.evidence[0].text


def test_plan_names_match_whole_words_only():
    assert rules._has_word("Eco mode", rules.QUIET_PLAN_WORDS) and rules._has_word("SILENT", rules.QUIET_PLAN_WORDS)
    assert not rules._has_word("ASUS Recommended", rules.QUIET_PLAN_WORDS)          # r-eco-mmended
    assert rules._has_word("High performance", rules.PERFORMANCE_PLAN_WORDS)
    assert not rules._has_word("Outperformance", rules.PERFORMANCE_PLAN_WORDS)
    # This laptop's other ASUS plan, in an older capture without the settings: not taken for a quiet plan.
    other = dict(POWER, scheme_guid="a5a5a5a5-0000-4000-8000-000000000001", scheme_name="ASUS Recommended")
    assert isinstance(run("power_plan", make(inventory={"power": other})), rules.Cleared)


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
    assert isinstance(res, rules.Unchecked) and res.text == ("needs NVIDIA telemetry (NVML), which was not recorded "
                                                             "(see Not measured)")


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


def test_throttling_held_all_the_time_is_never_contradicted_by_stutters_it_cannot_rise_at():
    # LC 4.2, 4.5: throttling is a steady cause. A real recording's GPU was held down by heat 97 % of the time, so it
    # could not rise at any stutter; the lift test read "contradicts" (-2), and the finding fell from likely to
    # possible and out of the summary's top three.
    events = [stutter(i, 1.0 + i) for i in range(1, 21)]
    never = lift("contradicts", a=0, n=20, lift_=None)
    gpu = make(adapter=NV, ok=("frames", "system", "gpu"), gpu=nv(97.0, nv_reasons="sw_thermal_slowdown"))
    res = run("gpu_throttle", gpu, bound={"verdict": "gpu"}, events=events, causes={"gpu_throttle": never})
    assert is_finding(res, "high")
    # nor a line: "0 of 20 coincided" under "held down 97 % of the time" read as a GPU that never slowed down
    assert not [e.text for e in res.evidence if "coincided" in e.text]
    cpu = make(system=cpu_sys(limit=80.0), duration=60.0)
    res = run("cpu_throttle", cpu, bound={"verdict": "cpu_main", "busiest_thread": 0.95}, events=events,
              causes={"cpu_throttle": never})
    assert is_finding(res, "medium")                      # held down in every sample (+2), no longer cancelled
    assert not [e.text for e in res.evidence if "coincided" in e.text]
    # an earlier capture whose share could not be told from the power cap stays "could not check", with its reason
    old = saved_before_the_power_cap_split(100.0, "sw_power_cap", heat_every=4)
    res = run("gpu_throttle", make(adapter=NV, ok=("frames", "system", "gpu"), gpu=old), bound={"verdict": "gpu"},
              events=events, causes={"gpu_throttle": never})
    assert isinstance(res, rules.Unchecked) and "only 7 of 30 readings named heat" in res.text
    # a lift that supports still counts
    res = run("gpu_throttle", make(adapter=NV, ok=("frames", "system", "gpu"), gpu=nv(10.0)), bound={"verdict": "gpu"},
              events=events, causes={"gpu_throttle": lift("supports", a=12, n=20)})
    assert "12 of 20 stutters and FPS drops coincided with the GPU slowing down (lift 20.0)." in \
        [e.text for e in res.evidence]


@pytest.mark.parametrize("later, kind", [(80.0, Finding), (80.1, rules.Cleared)])
def test_cpu_throttle_counts_a_speed_drop_of_twenty_percent_from_the_start(later, kind):
    perf = (lambda t: 100.0 if t <= 20.0 else later)
    res = run("cpu_throttle", make(system=cpu_sys(perf=perf), duration=60.0),
              bound={"verdict": "cpu_main", "busiest_thread": 0.95})
    assert isinstance(res, kind)


@pytest.mark.parametrize("perf, kind", [(80.0, Finding), (80.1, rules.Cleared)])
def test_cpu_throttle_counts_a_cpu_held_below_its_base_clock(perf, kind):
    # Item 2: the first real capture's CPU ran at a median 70 % of its base clock for 14 minutes while Windows reported
    # no limit and no drop from the start, and the rule said "the CPU ran at full speed". The level itself counts now.
    res = run("cpu_throttle", make(system=cpu_sys(perf=perf), duration=60.0),
              bound={"verdict": "cpu_main", "busiest_thread": 0.95})
    assert isinstance(res, kind)
    if kind is Finding:
        assert res.title == "The CPU ran below its base clock" and res.confidence == "medium"
        assert res.evidence[0].text == ("The CPU ran at a median 80% of its base clock (80% or less), below full speed "
                                        "for most of the capture, though Windows reported no power or thermal limit.")
    else:
        assert res.text == ("Windows reported no power or thermal limit, and the CPU ran at a median 80% of its base "
                            "clock with no speed drop.")


def test_cpu_throttle_never_calls_a_cpu_it_did_not_see_at_full_speed():
    # Item 2: "ran at full speed" stood for any CPU without a limit flag or a drop; what was measured is said instead
    res = run("cpu_throttle", make(system=cpu_sys(), duration=60.0), bound={"verdict": "cpu_main", "busiest_thread": 0.95})
    assert isinstance(res, rules.Cleared) and "full speed" not in res.text
    assert res.text.startswith("Windows reported no power or thermal limit, and the CPU ran at a median 105% of its base")
    short = synth.system_table(3, cpu_perf_limit_pct=100.0, thermal_limit_pct=100.0, cpu_perf_pct=105.0)  # 12 samples
    res = run("cpu_throttle", make(system=short, duration=3.0), bound={"verdict": "cpu_main", "busiest_thread": 0.95})
    assert isinstance(res, rules.Cleared) and "speed drop" not in res.text      # too few samples to compare: unsaid
    held = run("cpu_throttle", make(system=cpu_sys(limit=80.0, perf=70.0), duration=60.0),
               bound={"verdict": "cpu_main", "busiest_thread": 0.95})            # a limit Windows saw, and the level
    assert held.title == "The CPU slows down from heat or power limits"
    assert not any("though Windows reported no" in e.text for e in held.evidence)


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


@pytest.mark.parametrize("commit, kind", [(90.1, Finding), (90.0, type(None))])
def test_free_ram_never_cancels_a_commit_charge_at_the_limit(commit, kind):
    # Item 7: the first real capture held 89-99.9 % of the commit limit all session while 7.9 GB or more of RAM stayed
    # free (a small page file). Free RAM (against) and the commit charge (for) cancelled in one family, so 99.9 % was
    # never reported. The commit limit is RAM plus the page file: free RAM is no evidence against reaching it.
    res = run("ram_pressure", make(system=ram(8000.0, commit_pct=commit, hard_faults_ps=5000.0)))
    assert isinstance(res, kind)
    if kind is Finding:
        assert res.confidence == "medium"
        assert res.title == "Windows nearly ran out of memory to hand out: the page file is small"
        assert res.evidence[0].text == "Committed memory reached 90% of the limit (critical over 90%)."
        assert any(e.text.endswith("so what ran short was the page file.") for e in res.evidence)
        assert any(fx.title.startswith("Let Windows manage the page file size") for fx in res.fixes)
        assert any("file reads" in e.text for e in res.evidence)      # the hard faults, with RAM free: no support


def test_paging_that_cannot_coincide_with_the_stutters_never_cancels_the_page_file_warning():
    # Attribution counts paging only while RAM is nearly full, so with RAM free it can never coincide with a stutter.
    # A real recording with free RAM and dozens of stutters read "contradicts" (-2) there, which cancelled its commit
    # charge at 99.9 % (+2), so the small page file was never reported.
    events = [stutter(i, 1.0 + i) for i in range(1, 21)]
    res = run("ram_pressure", make(system=ram(8000.0, commit_pct=99.9)), events=events,
              causes={"memory_paging": lift("contradicts", a=0, n=20, lift_=None)})
    assert is_finding(res, "medium")
    assert res.title == "Windows nearly ran out of memory to hand out: the page file is small"
    assert "0 of 20 stutters and FPS drops coincided with Windows paging memory to disk." in [e.text for e in res.evidence]
    # control: with RAM nearly full paging could have coincided, so the same lift test still counts against (-2)
    assert run("ram_pressure", make(system=ram(1500.0, commit_pct=99.9)), events=events,
               causes={"memory_paging": lift("contradicts", a=0, n=20, lift_=None)}) is None
    assert is_finding(run("ram_pressure", make(system=ram(1500.0, commit_pct=99.9))), "medium")


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
    # Whole words: "Outperformance" is not a performance plan, "High performance" is (listed after it on purpose).
    power["schemes"] = [["11111111-2222-3333-4444-555555555555", "Outperformance"],
                        ["8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c", "High performance"],
                        ["64a64f24-65b9-4b56-befd-5ec1eaced9b3", "Silent"]]
    res = run("power_plan", make(inventory={"power": power}))
    assert next(f for f in res.fixes if f.fix_id).params == {"scheme": "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"}


def test_power_plan_fine_is_cleared_and_unread_is_unchecked():
    fine = dict(POWER, scheme_name="Balanced", scheme_guid="381b4222-f694-41f0-9685-ff5bb260df2e")
    res = run("power_plan", make(inventory={"power": fine}))
    assert isinstance(res, rules.Cleared) and res.text.startswith("Plugged in") and "Balanced" in res.text
    assert isinstance(run("power_plan", make(inventory={"power": {}})), rules.Unchecked)


HIGH_PERFORMANCE = dict(POWER, scheme_name="High performance", scheme_guid="8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c",
                        personality="high_performance", effective_mode="high_performance")
BALANCED_PLAN = dict(POWER, scheme_name="Balanced", scheme_guid="381b4222-f694-41f0-9685-ff5bb260df2e",
                     personality="balanced")


def test_power_plan_never_cites_a_power_mode_on_a_plan_without_one():
    # The capture holds a slider position, but a High performance type plan has no slider: it is not the PC's mode.
    res = run("power_plan", make(inventory={"power": dict(HIGH_PERFORMANCE, slider="best_efficiency")}))
    assert isinstance(res, rules.Cleared) and "power mode" not in res.text.lower()
    res = run("power_plan", make(inventory={"power": dict(BALANCED_PLAN, slider="best_performance")}))
    assert isinstance(res, rules.Cleared) and "power mode best performance" in res.text


def test_power_plan_never_advises_the_power_mode_on_a_plan_without_one():
    saver = dict(HIGH_PERFORMANCE, slider=None, saver=True, effective_mode="battery_saver")
    res = run("power_plan", make(inventory={"power": saver}))
    assert is_finding(res) and "battery saver" in res.evidence[0].text
    titles = [f.title for f in res.fixes]
    assert "Turn off battery saver while gaming" in titles and "Set the power mode to Best performance" not in titles
    # The same capture on a Balanced type plan, and saved before the type was read: there the slider can be set
    for kind in ("balanced", None):
        res = run("power_plan", make(inventory={"power": dict(saver, personality=kind)}))
        assert "Set the power mode to Best performance" in [f.title for f in res.fixes], kind


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


def test_vsync_judder_leaves_caps_and_a_games_own_uneven_presents_to_their_own_rules():
    assert run("vsync_judder", make(), frames=judder_frames(80, 20),
               bound={"verdict": "capped", "quantised_pct": 100.0}) is None
    uneven = LagEvent(1, "pacing", 0.0, 29.0, 8.0, {"present_swing_pct": 60.0})
    assert run("vsync_judder", make(), frames=judder_frames(80, 20), events=[uneven],      # one refresh: as before
               bound={"verdict": "gpu", "quantised_pct": 100.0}) is None


def holds_frames(holds, jitter=(0.0,)):
    """Frames each shown for the given whole number of the 144 Hz panel's refreshes and presented that hold plus the
    next jitter value (ms, repeating): presents that follow the display, displayed intervals on whole periods."""
    p = 1000.0 / 144.0
    return synth.frames_table([h * p + jitter[i % len(jitter)] for i, h in enumerate(holds)],
                              display_ms=[h * p for h in holds], displayed=1)


def spread(**counts):
    """Holds by count, e.g. spread(two=52, three=48) -> [2] * 52 + [3] * 48."""
    words = {"one": 1, "two": 2, "three": 3, "four": 4}
    return [h for name, n in counts.items() for h in [words[name]] * n]


@pytest.mark.parametrize("holds, title, evidence, cap", [
    (spread(one=20, two=80), "one and two", "20% of frames stayed on screen for one refresh (6.9 ms) while most took "
                                            "two (13.9 ms)", "72 FPS (half the refresh)"),
    (spread(two=52, three=48), "two and three", "48% of frames stayed on screen for 3 or more refreshes (20.8 ms) while "
                                                "most took two (13.9 ms)", "48 FPS (a third of the refresh)"),
    (spread(three=41, four=59), "three and four", "41% of frames stayed on screen for 3 or fewer refreshes (20.8 ms) "
                                                  "while most took four (27.8 ms)", "36 FPS (a quarter of the refresh)")])
def test_vsync_judder_is_judged_between_any_two_adjacent_whole_numbers_of_refreshes(holds, title, evidence, cap):
    # Most frames held two refreshes or more used to read as low FPS, not judder, so the rule gave up: 80 FPS on
    # 144 Hz (1 or 2), 58 FPS (2 or 3), 40 FPS (3 or 4). The cap that ends it is refresh / (k + 1)
    res = run("vsync_judder", make(), frames=holds_frames(holds), bound={"verdict": "gpu", "quantised_pct": 100.0})
    assert is_finding(res, "medium") and res.title == f"VSync judder: frames alternate between {title} refreshes"
    assert res.evidence[0].text == evidence + ": judder (10% or more)."
    assert [f.title for f in res.fixes] == ["Turn on VRR (G-SYNC or FreeSync) if the screen supports it",
                                            f"Or cap the frame rate at {cap}", "Or turn VSync off"]


def test_a_steady_lock_at_two_refreshes_is_no_judder():
    # what the old "median hold 1" guard protected: 72 FPS on 144 Hz, every frame two refreshes, is even
    res = run("vsync_judder", make(), frames=holds_frames(spread(two=100)),
              bound={"verdict": "gpu", "quantised_pct": 100.0})
    assert isinstance(res, rules.Cleared) and res.text == "100% of frames were shown for two refreshes: no judder."


FOLLOWS = ([2, 3] * 43 + [2] * 7 + [1] * 4 + [4] * 3) * 10         # per 100 frames: 2: 50, 3: 43, 1: 4, 4: 3
OWN = ([1] * 37 + [2] * 8 + [3] * 55) * 10          # a game's own 4 / 20 ms swings spread over three counts (pace mode)
SWUNG = LagEvent(1, "pacing", 0.0, 29.0, 6.9, {"present_swing_pct": 30.0, "swing_pct": 30.0, "criterion": "swing"})


def test_presents_that_swing_with_two_and_three_refreshes_are_vsync_judder_not_the_games_own_pacing():
    # A GPU-bound game under VSync (made-up numbers of a real recording's shape): 93 % of its frames stay up two or
    # three refreshes, and its presents swing, so the pacing events flag uneven presents. vsync_judder gave up twice
    # (most frames past one refresh; uneven presents), and uneven_pacing called the swing the game's own
    frames, bound = holds_frames(FOLLOWS, jitter=(0.9, -0.6, 0.2, -1.1, 0.5)), {"verdict": "gpu", "quantised_pct": 100.0}
    res = run("vsync_judder", make(), frames=frames, bound=bound, events=[SWUNG])
    assert is_finding(res, "medium") and res.event_ids == [1]
    assert res.title == "VSync judder: frames alternate between two and three refreshes"
    assert res.evidence[0].text == ("46% of frames stayed on screen for 3 or more refreshes (20.8 ms) while 50% took "
                                    "two (13.9 ms): judder (10% or more).")
    assert "Or cap the frame rate at 48 FPS (a third of the refresh)" in [f.title for f in res.fixes]
    assert run("uneven_pacing", make(), frames=frames, bound=bound, events=[SWUNG]) is None    # never "fine" either
    # control: the game's own swings spread its holds over three counts; that stays uneven pacing, as before
    own = holds_frames(OWN)
    assert run("vsync_judder", make(), frames=own, bound=bound, events=[SWUNG]) is None
    assert is_finding(run("uneven_pacing", make(), frames=own, bound=bound, events=[SWUNG]), "medium")


def test_holds_that_slip_both_ways_are_never_called_fine_and_never_silence_uneven_pacing():
    # 8 % on two and 8 % on four refreshes around a usual three: each side alone is under 10 %, so one side's share read
    # "no judder" while the holds' shape silenced uneven_pacing, and the pacing lag vanished from the report. Slips on
    # both sides are not the alternation the rule names (its texts and cap would be wrong), and not steady either.
    bound = {"verdict": "gpu", "quantised_pct": 100.0}
    slips = holds_frames(([3] * 84 + [2] * 8 + [4] * 8) * 10)
    assert run("vsync_judder", make(), frames=slips, bound=bound) is None
    assert run("vsync_judder", make(), frames=slips, bound=bound, events=[SWUNG]) is None
    assert is_finding(run("uneven_pacing", make(), frames=slips, bound=bound, events=[SWUNG]), "medium")
    # a game capped at 72 FPS whose frames slip one refresh either way 5 % of the time: neither judder nor fine
    capped = holds_frames(([2] * 90 + [1] * 5 + [3] * 5) * 10)
    assert run("vsync_judder", make(), frames=capped, bound=bound) is None
    # one refresh away on one side only, where the texts are true: 10 % held one refresh around a usual three is
    # no alternation of adjacent counts either
    assert run("vsync_judder", make(), frames=holds_frames(([3] * 90 + [1] * 10) * 10), bound=bound) is None
    # nor on the pair's side: 10 % held four refreshes around a usual two, none held three, read "two and three", 20.8 ms
    # and a 48 FPS cap that leaves those frames at four
    assert run("vsync_judder", make(), frames=holds_frames(([2] * 90 + [4] * 10) * 10), bound=bound) is None
    # 2 % on each side is no judder, so the swinging presents stay uneven_pacing's: never silenced where judder says none
    calm = holds_frames(([3] * 96 + [2] * 2 + [4] * 2) * 10)
    assert run("vsync_judder", make(), frames=calm, bound=bound, events=[SWUNG]) is None
    assert is_finding(run("uneven_pacing", make(), frames=calm, bound=bound, events=[SWUNG]), "medium")


def test_a_shared_gpu_never_lets_the_judder_shape_silence_uneven_pacing():
    # A7: under a shared GPU vsync_judder is "could not be told", so a shape that silenced uneven_pacing too left the
    # pacing lag claimed by nothing
    frames, bound = holds_frames(FOLLOWS, jitter=(0.9, -0.6, 0.2, -1.1, 0.5)), {"verdict": "unknown", "quantised_pct": 100.0}
    shared = contention(80.0)
    assert run("vsync_judder", shared, frames=frames, bound=bound, events=[SWUNG]) is None     # uneven_pacing's case
    assert is_finding(run("uneven_pacing", shared, frames=frames, bound=bound, events=[SWUNG]), "medium")
    assert isinstance(run("vsync_judder", shared, frames=frames, bound=bound), rules.Unchecked)  # steady presents: A7
    assert run("uneven_pacing", contention(2.0), frames=frames, bound=bound, events=[SWUNG]) is None   # control


def test_the_judder_summary_says_how_far_below_the_refresh_the_game_runs():
    # most frames held two refreshes (about 80 FPS on 144 Hz) is no "little slower than the screen refreshes"
    res = run("vsync_judder", make(), frames=holds_frames(spread(one=20, two=80)),
              bound={"verdict": "gpu", "quantised_pct": 100.0})
    assert res.summary == ("The game runs between 72 and 144 FPS on a 144 Hz screen, so with VSync some frames stay up "
                           "one refresh longer than others, which looks jerky.")
    res = run("vsync_judder", make(), frames=judder_frames(80, 20), bound={"verdict": "gpu", "quantised_pct": 100.0})
    assert res.summary.startswith("The game runs a little slower than the screen refreshes")


@pytest.mark.parametrize("off, judder", [(100, True), (101, False)])     # 900 or 899 of 1,000 holds on two and three
def test_swinging_presents_are_judder_only_with_ninety_percent_of_the_holds_on_two_counts(off, judder):
    holds = [2] * 500 + [3] * (500 - off) + [1] * 50 + [4] * (off - 50)
    frames, bound = holds_frames(holds), {"verdict": "gpu", "quantised_pct": 100.0}
    got = (is_finding(run("vsync_judder", make(), frames=frames, bound=bound, events=[SWUNG])),
           is_finding(run("uneven_pacing", make(), frames=frames, bound=bound, events=[SWUNG])))
    assert got == (judder, not judder)


def anim_pacing(t0=0.5, t1=29.5, ms=6.4, i=1):
    """A pacing event only the animation-error test flagged (events.py detail["criterion"] "anim")."""
    return LagEvent(i, "pacing", t0, t1, ms, {"criterion": "anim", "anim_p95_ms": ms, "swing_pct": 0.0,
                                              "present_swing_pct": 0.0, "quantised_pct": 100.0})


def sixty_hz(**kw):
    return make(inventory={"displays": [dict(DISPLAY, hz=60.0, max_hz=60, modes_hz=[60])]}, **kw)


ABOVE = {"quantised_pct": 100.0, "vrr": False, "ft_ms": 11.1, "r": 0.2}          # ~90 FPS presented
ONE_REFRESH = synth.frames_table([11.1] * 300, display_ms=1000.0 / 60.0, displayed=1)


@pytest.mark.parametrize("dropped, kind", [(10.0, Finding), (9.9, rules.Cleared)])
def test_dropped_frames_is_its_own_cause_above_a_fixed_refresh(dropped, kind):
    # A11: ~90 FPS into a 60 Hz composed screen: every shown frame holds one refresh (no 1P/2P judder), a third of
    # the frames are never shown, and only the animation error sees the jerky motion. Changed on purpose (item 13):
    # vsync_judder claimed this, so the headline said "possibly VSync judder" where VSync is not involved
    def judged(cause):
        return run(cause, sixty_hz(), frames=ONE_REFRESH, stats={"avg_fps": 60.0, "dropped_pct": dropped},
                   bound=dict(ABOVE, verdict="waiting", refresh_hz=60.0), events=[anim_pacing()])
    res = judged("dropped_frames")
    assert isinstance(res, kind)
    if kind is Finding:
        assert res.event_ids == [1] and res.title == "Frames are dropped: the game runs faster than the 60 Hz screen"
        assert any(f.title.startswith("Cap the frame rate at 60 FPS") for f in res.fixes)
    else:
        assert res.text == "Only 9.9% of the frames were never shown (under 10%)."
    vsync = judged("vsync_judder")                                      # no frame held two refreshes: no VSync judder
    assert isinstance(vsync, rules.Cleared) and "shown for one refresh" in vsync.text


def test_dropped_frame_judder_needs_presents_faster_than_the_refresh_and_no_vrr():
    def judged(fps=90.0, **bound):
        frames = synth.frames_table([1000.0 / fps] * 300, display_ms=1000.0 / 60.0, displayed=1)
        return run("dropped_frames", sixty_hz(), frames=frames, stats={"avg_fps": 60.0, "dropped_pct": 33.0},
                   bound={**ABOVE, "verdict": "waiting", "refresh_hz": 60.0, **bound}, events=[anim_pacing()])
    assert is_finding(judged(61.1))                                     # 61.1 FPS: past the 1 Hz slack
    slower = judged(60.9)                                               # within it: no frame to drop
    assert isinstance(slower, rules.Cleared) and slower.text.startswith("The game presented about 61 FPS, no faster")
    vrr = judged(vrr=True)                                              # VRR shows frames as they come
    assert isinstance(vrr, rules.Cleared) and vrr.text.startswith("VRR was on")
    short = judged(quantised_pct=None, ft_ms=None, verdict="unknown")   # too short for a bound verdict (LC 2.6)
    assert is_finding(short) and short.event_ids == [1]
    smeared = run("dropped_frames", sixty_hz(), frames=ONE_REFRESH, stats={"avg_fps": 60.0, "dropped_pct": 33.0},
                  bound=dict(ABOVE, verdict="waiting", refresh_hz=60.0),
                  events=[LagEvent(1, "pacing", 0.5, 29.5, 6.4, {"criterion": "anim", "quantised_pct": 89.9})])
    assert not is_finding(smeared)                                      # not on whole refreshes: tearing, not drops
    capped = judged(verdict="capped", cap_fps=90.0)                     # under a cap the frame_cap rule claims it
    assert capped is None


@pytest.mark.parametrize("cap_fps", [86.0, 61.1])
def test_a_cap_above_the_refresh_is_not_a_sensible_cap(cap_fps):
    # A11: frames past a fixed refresh are never shown; with the dropped-frame judder in the frames, the cap claims it
    bound = dict(ABOVE, verdict="capped", cap_fps=cap_fps, refresh_hz=60.0, ft_ms=1000.0 / cap_fps)
    res = run("frame_cap", sixty_hz(), stats={"dropped_pct": 30.0}, bound=bound, events=[anim_pacing()])
    assert is_finding(res, "medium") and res.event_ids == [1]
    assert res.title == f"A frame-rate cap above the refresh rate drops frames: {cap_fps:.0f} FPS on a 60 Hz screen"
    assert [f.title for f in res.fixes][:2] == ["Cap the frame rate at 60 FPS (the refresh rate)", "Or turn on VSync"]
    quiet = run("frame_cap", sixty_hz(), stats={"dropped_pct": 30.0}, bound=bound)       # no judder seen
    assert quiet is None


def test_a_cap_at_the_refresh_is_still_a_sensible_one():
    bound = {"verdict": "capped", "cap_fps": 61.0, "refresh_hz": 60.0, "r": 0.2}       # within the 1 Hz slack
    res = run("frame_cap", sixty_hz(), stats={"dropped_pct": 30.0}, bound=bound, events=[anim_pacing()])
    assert isinstance(res, rules.Cleared) and "a sensible cap" in res.text


def full_run(cap, *, stats=None, bound=None, events=()):
    st = {"frames": 3000, "avg_fps": 60.0, "duration_s": cap.duration_s}
    st.update(stats or {})
    b = {"verdict": "unknown", "notes": [], "refresh_hz": 144.0}
    b.update(bound or {})
    return rules.run_rules(cap, {"frames": {}, "stats": st, "bound": b, "events": list(events), "event_causes": {}})


def test_unexplained_pacing_keeps_the_pacing_causes_open():
    # A11: an anim-only pacing event over the whole capture that no rule claims (too few frames dropped for the
    # dropped-frame judder) once left "frame pacing: checked and fine" under a capture that juddered throughout
    findings, cleared, unchecked = full_run(sixty_hz(), stats={"dropped_pct": 5.0},
                                            bound=dict(ABOVE, verdict="waiting", refresh_hz=60.0),
                                            events=[anim_pacing()])
    assert not any(1 in f.event_ids for f in findings)
    assert "uneven_pacing" not in cleared and "6.4 ms animation error" in unchecked["uneven_pacing"]
    # the 5 % of frames never shown might be part of it too, so dropped frames are not ruled out either (item 13)
    assert "dropped_frames" not in cleared and "6.4 ms animation error" in unchecked["dropped_frames"]
    short = anim_pacing(0.5, 3.4)                                    # 9.7 % of the capture: under the uneven share
    _, cleared, _ = full_run(sixty_hz(), stats={"dropped_pct": 5.0}, bound=dict(ABOVE, verdict="waiting",
                                                                                 refresh_hz=60.0), events=[short])
    assert "uneven_pacing" in cleared
    claimed = anim_pacing()                                          # a cap finding claims it: fine to clear
    findings, cleared, _ = full_run(make(), bound={"verdict": "capped", "cap_fps": 100.0, "r": 0.1}, events=[claimed])
    assert any(f.cause == "frame_cap" and 1 in f.event_ids for f in findings) and "uneven_pacing" in cleared


# -------------------------------------------------------------------------- uneven pacing ---

def pacing(t0, t1, present_swing):
    return LagEvent(1, "pacing", t0, t1, 9.0, {"present_swing_pct": present_swing, "swing_pct": 60.0})


@pytest.mark.parametrize("span, kind", [(3.01, Finding), (2.99, rules.Cleared)])     # 10 % of 30 s
def test_uneven_pacing_needs_ten_percent_of_the_capture(span, kind):
    assert isinstance(run("uneven_pacing", make(), events=[pacing(5.0, 5.0 + span, 50.0)]), kind)


@pytest.mark.parametrize("swing, kind", [(10.1, Finding), (10.0, rules.Cleared)])
def test_uneven_pacing_counts_only_uneven_presents_not_display_judder(swing, kind):
    assert isinstance(run("uneven_pacing", make(), events=[pacing(0.0, 29.0, swing)]), kind)


@pytest.mark.parametrize("span", [14.9, 15.0, 29.0])
def test_uneven_pacing_is_a_steady_metric_never_a_direct_readout(span):
    # A1 (LC 5.1): weight 4 is for a direct readout (a throttle counter, FrameType); one frame-time pattern is a
    # steady metric past its threshold (2), so it is "possible" on its own however long it lasts. This test used to
    # pin "likely" from half the capture on, which made hitches' own swings outrank the hitches.
    res = run("uneven_pacing", make(), events=[pacing(1.0, 1.0 + span, 70.0)])
    assert is_finding(res, "medium") and res.event_ids == [1]


def test_uneven_pacing_never_gives_the_screens_swing_as_the_games_own():
    # a pacing event's severity is the median swing of the displayed frames (events.py), which this text printed as
    # "the game's own frame times ... (median swing 9.0 ms)"
    res = run("uneven_pacing", make(), events=[pacing(1.0, 29.0, 70.0)])
    assert res.evidence[0].text == ("The game's own frame times swung by 4 ms or more on up to 70% of frames, over 93% "
                                    "of the capture (10% or more).")


def test_uneven_pacing_never_covers_more_than_the_whole_capture():
    # A11: an event can start before the first row (a replay); the share is clamped to 100 %
    res = run("uneven_pacing", make(), events=[pacing(-0.5, 30.5, 70.0)])
    assert is_finding(res) and res.evidence[0].value == pytest.approx(100.0)
    assert "over 100% of the capture" in res.evidence[0].text


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
    assert isinstance(run("frame_cap", make(), bound={"verdict": "gpu", "r": 0.97}), rules.Cleared)    # frame timing's
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
        assert isinstance(res, rules.Unchecked) and res.text == "needs frame timing (PresentMon), which was not recorded (see Not measured)"


@pytest.mark.parametrize("settings, kind, conf", [
    ({"background_recording": True}, Finding, "medium"), ({"game_dvr": True, "background_recording": True}, Finding,
                                                           "medium"),
    ({}, rules.Cleared, None), ({"game_dvr": None, "background_recording": None}, rules.Unchecked, None)])
def test_game_dvr(settings, kind, conf):
    res = run("game_dvr", make(inventory={"settings": dict(SETTINGS, **settings)}))
    assert isinstance(res, kind)
    if kind is Finding:
        assert res.confidence == conf
        fix = next(f for f in res.fixes if f.fix_id)
        assert fix.fix_id == "game_dvr_off" and fix.params == {} and fix.needs_restart == "game"


def test_game_dvr_with_background_recording_known_off_is_checked_and_fine():
    # CC-6: this used to read "Background recording is on" with the fix offered, while the setting said off
    res = run("game_dvr", make(inventory={"settings": dict(SETTINGS, game_dvr=True, background_recording=False)}))
    assert isinstance(res, rules.Cleared) and res.text.startswith("Background recording is off")


def test_game_dvr_with_background_recording_unknown_is_advice_only_and_says_so():
    # CC-6: Windows' default state (GameDVR_Enabled = 1, the capture values absent, WT 8): unknown, not "on"
    res = run("game_dvr", make(inventory={"settings": dict(SETTINGS, game_dvr=True, background_recording=None)}))
    assert is_finding(res, "low")
    # wording: the title was the finding's only evidence line, word for word; now each says its own part
    assert res.title == "Game Bar may be recording in the background"
    assert [e.text for e in res.evidence] == ["Game Bar capture is on, and its background-recording setting was not "
                                              "there to read, so whether Windows records gameplay in the background "
                                              "is unknown."]
    assert not any(f.fix_id for f in res.fixes) and "Background recording is on" not in res.title


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


@pytest.mark.parametrize("verdict, kind", [("gpu", type(None)), ("capped", type(None)), ("vsync", type(None)),
                                           ("waiting", type(None)), ("cpu_main", Finding), ("cpu_all", Finding),
                                           ("mixed", Finding), ("unknown", Finding)])
def test_not_focused_steady_share_counts_only_while_the_cpu_side_limits_the_game(verdict, kind):
    # A9 (LC 5.1): a window behind others gets less CPU time, which costs frames only on the CPU side
    sys_ = synth.system_table(30, game_focused=lambda t: 0 if t <= 6.0 else 1)       # 20 % of the capture
    res = run("not_focused", make(system=sys_), bound={"verdict": verdict, "r": 0.97 if verdict == "gpu" else None})
    assert isinstance(res, kind), res
    if verdict == "gpu":    # the 3D engine's "gpu" (no r: too few steady frames) says nothing of the CPU side
        assert is_finding(run("not_focused", make(system=sys_), bound={"verdict": "gpu", "r": None}), "medium")


def test_not_focused_linked_to_the_stutters_counts_on_any_side():
    sys_ = synth.system_table(30, game_focused=lambda t: 0 if t <= 6.0 else 1)
    res = run("not_focused", make(system=sys_), bound={"verdict": "gpu"},
              events=[stutter(i, 3.0 * i, "not_focused") for i in range(1, 7)],
              causes={"not_focused": lift("supports", a=6, n=6)})
    assert is_finding(res, "medium") and res.event_ids == [1, 2, 3, 4, 5, 6]


def marked(n, unfocused=(), cause="not_focused"):
    """n lag-key presses (frames not measured), every 3 s from 3 s; those in `unfocused` came while another window
    was in front, and name `cause` first."""
    return [LagEvent(i, "mark", 3.0 * i - 3.0, 3.0 * i + 0.5, 0.0, {"why": ["a moment you marked", f"signal {i}"]},
                     causes=[cause] if i in unfocused else []) for i in range(1, n + 1)]


def test_mostly_in_front_is_no_evidence_when_the_marks_came_while_it_was_behind():
    # Item 3: the first real capture's game was in front 94 % of the time, but 2 of its 3 marks fell in its one
    # stretch behind another window; too few marks for the lift test, so the session share alone cleared it
    sys_ = synth.system_table(30, game_focused=lambda t: 0 if t <= 1.5 else 1)            # 5 % behind
    nofr = dict(ok=("system",), src={"frames": NO_FRAMES})
    behind = {"not_focused": lift("too_few_events", a=2, n=3, half_weight=True)}
    res = run("not_focused", make(system=sys_, **nofr), stats={"frames": 0}, events=marked(3, {2, 3}), causes=behind)
    assert isinstance(res, rules.Unchecked) and res.text == ("the game was the active window 95% of the time, but 2 of "
                                                             "the 3 marked moments came while another window was in "
                                                             "front: too few to tell (at least 5 needed)")
    quiet = {"not_focused": lift("too_few_events", a=0, n=3, half_weight=True)}          # control: none behind
    res = run("not_focused", make(system=sys_, **nofr), stats={"frames": 0}, events=marked(3), causes=quiet)
    assert isinstance(res, rules.Cleared) and res.text == "The game was the active window 95% of the time."
    many = {"not_focused": lift("neutral", a=2, n=6)}                    # enough events, and 33 % behind: no sample
    evs = [stutter(i, 3.0 * i, "not_focused" if i <= 2 else None) for i in range(1, 7)]
    assert run("not_focused", make(system=sys_), events=evs, causes=many) is None


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
    assert not any(f.fix_id for f in res.fixes)                    # an old capture's rows carry no creation time
    res = run("background_hogs", hogs(hog_others(300.0, create=True)), events=hog_events(), causes=causes)
    fix = next(f for f in res.fixes if f.fix_id)
    assert fix.fix_id == "background_priority"
    # SEC-2: the params fixes/priority.py now requires; game_name is the target's own name
    assert fix.params == {"pid": 20000, "create_time": 134000000000020000, "class": "below_normal", "exclude": [4242],
                          "name": "hog.exe", "game_name": "game.exe"}
    game = hogs(hog_others(300.0, create=True), target={"pids": [4242, 20000, 20004]})
    assert not any(f.fix_id for f in run("background_hogs", game, events=hog_events(), causes=causes).fixes)


@pytest.mark.parametrize("name, want", [("game.exe", "game.exe"), (None, None), ("", None), (42, None),
                                        ("evil\nname.exe", None)])
def test_the_priority_fix_names_the_game_only_by_a_real_image_name_and_never_as_empty(name, want):
    # SEC-2: game_name is the target's image name or None when unknown, never '' (the fix refuses '')
    cap = hogs(hog_others(300.0, create=True), target={"name": name})
    res = run("background_hogs", cap, events=hog_events(), causes={"background_cpu": lift("supports", a=5, n=5)})
    fix = next(f for f in res.fixes if f.fix_id)
    assert fix.params["game_name"] == want and fix.params["name"] == "hog.exe"


def test_no_priority_fix_without_the_games_pids_to_exclude():
    # SEC-2: the fix refuses an empty "exclude"; without the game's PIDs it could not tell the game apart
    cap = hogs(hog_others(300.0, create=True), target={"pid": None, "pids": []})
    res = run("background_hogs", cap, events=hog_events(), causes={"background_cpu": lift("supports", a=5, n=5)})
    assert is_finding(res) and not any(f.fix_id for f in res.fixes)


def reused_others(later_name):
    """hog.exe (pid 1000) busy at the lag moments; later the same PID listed as `later_name`, a new process."""
    rows = []
    for k in range(1, 121):
        t = k * 0.25
        hot = any(te - 0.5 <= t <= te + 0.4 for te in (5.0, 10.0, 15.0, 20.0, 25.0))
        procs = [[7777, "python.exe", 100.0, 0.0, 100.0, 134000000000007777]]
        if t < 26.0:
            procs.append([1000, "hog.exe", 300.0 if hot else 0.0, 0.0, 0.0, 134000000000001000])
        else:
            procs.append([1000, later_name, 1.0, 0.0, 0.0, 134000000000009999])
        rows.append([t, procs])
    return rows


def test_a_reused_pid_never_pairs_the_hogs_name_with_another_processes_identity():
    # SEC-2: hog.exe (pid 1000) exits and updater.exe gets pid 1000; the fix must never lower updater.exe
    causes = {"background_cpu": lift("supports", a=5, n=5)}
    evs = [stutter(i, 5.0 * i, "background_cpu", process="hog.exe", process_pids=[1000],
                   process_rises=[[1000, 300.0]]) for i in range(1, 6)]
    res = run("background_hogs", hogs(reused_others("updater.exe")), events=evs, causes=causes)
    assert is_finding(res) and not any(f.fix_id for f in res.fixes)
    same = run("background_hogs", hogs(reused_others("hog.exe")), events=evs, causes=causes)   # reused, same name
    assert not any(f.fix_id for f in same.fixes)
    rows = [[t, [p for p in procs if p[1] != "updater.exe"]] for t, procs in reused_others("updater.exe")]
    fix = next(f for f in run("background_hogs", hogs(rows), events=evs, causes=causes).fixes if f.fix_id)
    assert (fix.params["pid"], fix.params["create_time"], fix.params["name"]) == (1000, 134000000000001000, "hog.exe")


def test_a_process_with_the_games_own_name_is_never_offered_nor_told_to_close():
    # SEC-2: a multi-process game (Electron, CEF): the chosen process is the GPU one, its renderer a sibling
    others = [[t, [[p[0], "Game.exe" if p[1] == "hog.exe" else p[1]] + p[2:] for p in procs]]
              for t, procs in hog_others(300.0, create=True)]
    evs = [stutter(i, 5.0 * i, "background_cpu", process="Game.exe", process_pids=[20000, 20004],
                   process_rises=[[20000, 150.0], [20004, 150.0]]) for i in range(1, 6)]
    res = run("background_hogs", hogs(others, target={"name": "game.exe"}), events=evs,
              causes={"background_cpu": lift("supports", a=5, n=5)})
    assert is_finding(res) and not any(f.fix_id for f in res.fixes)
    assert not any(f.title.startswith("Pause or close Game.exe") for f in res.fixes)
    assert any(f.title.startswith("Check what Game.exe") for f in res.fixes)


def test_the_fix_targets_the_process_that_rose_most_never_the_lowest_pid():
    # A5: python.exe 7777 (a steady training job) wobbles +1 %; python.exe 20004 rose 50 % at every stutter
    rows = []
    for k in range(1, 121):
        t = k * 0.25
        hot = any(te - 0.5 <= t <= te + 0.4 for te in (5.0, 10.0, 15.0, 20.0, 25.0))
        rows.append([t, [[7777, "python.exe", 101.0 if hot else 100.0, 0.0, 100.0, 134000000000007777],
                         [20004, "python.exe", 50.0 if hot else 0.0, 0.0, 0.0, 134000000000020004]]])
    evs = [stutter(i, 5.0 * i, "background_cpu", process="python.exe", process_pids=[20004, 7777],
                   process_rises=[[20004, 50.0], [7777, 1.0]]) for i in range(1, 6)]
    res = run("background_hogs", hogs(rows), events=evs, causes={"background_cpu": lift("supports", a=5, n=5)})
    fix = next(f for f in res.fixes if f.fix_id)
    assert fix.params["pid"] == 20004 and "up 50% of a CPU" in fix.title
    advice = next(f for f in res.fixes if f.title.startswith("Pause or close"))
    assert advice.title.endswith("(process 20004, 7777)")          # the biggest rise first


def test_the_games_own_helper_process_is_named_as_such_and_never_told_to_close():
    # A10: a child of the game (in target "pids") that works at each stutter is part of the game
    evs = [stutter(i, 5.0 * i, "background_cpu", process="ShaderCompileWorker.exe", process_role="game_helper",
                   process_pids=[8888], process_rises=[[8888, 300.0]]) for i in range(1, 6)]
    others = [[t, [[8888, "ShaderCompileWorker.exe"] + p[2:] if p[1] == "hog.exe" else p for p in procs]]
              for t, procs in hog_others(300.0, pids=(8888,), create=True)]
    res = run("background_hogs", hogs(others, target={"pids": [4242, 8888]}), events=evs,
              causes={"background_cpu": lift("supports", a=5, n=5)})
    assert is_finding(res) and res.title.startswith("The game's own helper processes got busy")
    assert not any(f.fix_id for f in res.fixes)
    assert not any(f.title.startswith("Pause or close") for f in res.fixes)
    assert any(f.title.startswith("ShaderCompileWorker.exe (process 8888) is the game's own helper") for f in res.fixes)
    assert any("helper process of the game, ShaderCompileWorker.exe used 300%" in e.text for e in res.evidence)


# Windows' own System process blamed the most, two programs after it, as in a real recording: the report offered to
# lower System's priority and told the player to pause or close it.
KERNEL_HOGS = [(4, "System", (2.5, 5.0, 7.5, 10.0, 12.5)), (20000, "hog.exe", (15.0, 17.5, 20.0)),
               (30000, "other.exe", (22.5, 25.0))]


def kernel_hog_capture(hogs_=KERNEL_HOGS):
    """Each (pid, name, stutter times) at 150 % of a CPU around its own stutters; one event per stutter time."""
    rows = []
    for k in range(1, 121):
        t = k * 0.25
        procs = [[7777, "python.exe", 100.0, 0.0, 100.0, 134000000000007777]]
        for pid, prog, times in hogs_:
            hot = any(te - 0.5 <= t <= te + 0.4 for te in times)
            procs.append([pid, prog, 150.0 if hot else 0.0, 0.0, 0.0, 134000000000000000 + pid])
        rows.append([t, procs])
    evs = [stutter(i, te, "background_cpu", process=prog, process_pids=[pid], process_rises=[[pid, 150.0]])
           for i, (pid, prog, te) in enumerate(((p, n, te) for p, n, ts in hogs_ for te in ts), 1)]
    return hogs(rows), evs


def with_name(name):
    return [(pid, name if pid == 4 else prog, times) for pid, prog, times in KERNEL_HOGS]


@pytest.mark.parametrize("name, means", [("System", "driver"), ("system", "driver"), ("Registry", "part of Windows"),
                                         ("Memory Compression", "RAM runs short"), ("Secure System", "part of Windows")])
def test_windows_own_processes_are_never_offered_a_priority_fix_nor_told_to_close(name, means):
    cap, evs = kernel_hog_capture(with_name(name))
    res = run("background_hogs", cap, events=evs, causes={"background_cpu": lift("supports", a=10, n=10)})
    assert is_finding(res)
    assert res.title == f"Background programs got busy when the game lagged: Windows itself ({name}), hog.exe, other.exe"
    said_it = f"{name} used 150% of a CPU during 5 of 10".casefold()       # a sentence starts with a capital
    assert any(said_it in e.text.casefold() for e in res.evidence)        # it is still the evidence
    pids = [f.params["pid"] for f in res.fixes if f.fix_id == "background_priority"]
    assert pids == [20000, 30000]            # the two slots go to programs that can be lowered, in blame order
    titles = [f.title for f in res.fixes]
    assert not any(t.startswith(f"Pause or close {name}") for t in titles), titles
    said = next(f for f in res.fixes if f.title == f"{name} (process 4) is Windows itself, not a program to close")
    assert means in said.detail
    assert any(t.startswith("Pause or close hog.exe") for t in titles)
    assert any(t.startswith("Pause or close other.exe") for t in titles)


def test_a_program_merely_named_like_windows_own_processes_is_still_a_program():
    cap, evs = kernel_hog_capture(with_name("System.exe"))
    res = run("background_hogs", cap, events=evs, causes={"background_cpu": lift("supports", a=10, n=10)})
    assert res.title == "Background programs got busy when the game lagged: System.exe, hog.exe, other.exe"
    assert [f.params["pid"] for f in res.fixes if f.fix_id] == [4, 20000]
    assert any(f.title.startswith("Pause or close System.exe") for f in res.fixes)


def test_a_priority_fix_goes_only_to_a_program_the_title_names():
    # Two of Windows' own lead: the title names three, so other.exe (the fourth) gets no fix and no advice either
    four = [(4, "System", (2.5, 5.0, 7.5, 10.0, 12.5)), (300, "Memory Compression", (13.0, 14.0, 15.0, 16.0)),
            (20000, "hog.exe", (18.0, 19.0, 20.0)), (30000, "other.exe", (22.5, 25.0))]
    cap, evs = kernel_hog_capture(four)
    res = run("background_hogs", cap, events=evs, causes={"background_cpu": lift("supports", a=14, n=14)})
    assert res.title == ("Background programs got busy when the game lagged: Windows itself (System), "
                         "Windows itself (Memory Compression), hog.exe")
    assert [f.params["pid"] for f in res.fixes if f.fix_id] == [20000]
    assert not any("other.exe" in f.title for f in res.fixes)


def test_a_lag_only_windows_itself_explains_is_not_put_on_background_programs():
    cap, evs = kernel_hog_capture([KERNEL_HOGS[0]])
    res = run("background_hogs", cap, events=evs, causes={"background_cpu": lift("supports", a=5, n=5)})
    assert res.title == "Windows itself got busy when the game lagged: System"
    assert res.summary.startswith("Windows' own work rose") and "program" not in res.summary
    assert not any(f.fix_id for f in res.fixes)
    assert [f.title for f in res.fixes][0] == "System (process 4) is Windows itself, not a program to close"


def test_gpu_contention_never_tells_the_player_to_close_windows_itself():
    rows = [[0.25 * k, [[7777, "python.exe", 1.0, 0.0, 0.0, 134000000000007777, None],
                        [4, "System", 3.0, 0.0, 3.0, 134000000000000004, 12.0]]] for k in range(1, 121)]
    res = run("gpu_contention", contention(80.0, others=rows), stats={"avg_fps": 58.0}, bound=STARVED)
    assert is_finding(res) and res.title == "Another program kept the game's GPU 80% busy: Windows itself (System)"
    assert res.fixes[0].title == "System (process 4) is Windows itself, not a program to close"
    assert not any(f.title.startswith("Pause or close") for f in res.fixes)


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


def test_a_driver_holding_up_the_cpu_reaches_possible_and_no_further():
    # DESIGN: driver_dpc is capped at "possible" (PDH cannot name the driver). Its lift weight is 1 so it ranks last
    # when explaining away, and that 1 point was its whole score: it could never be more than "worth checking".
    evs = [stutter(i, 3.0 * i, "driver_dpc") for i in range(1, 10)]
    strong = lift("supports", weight=1.0, lift_=50.0, cap="possible")       # attribute.py's summary for a capped cause
    res = run("driver_dpc", make(), events=evs, causes={"driver_dpc": strong})
    assert is_finding(res, "medium") and res.event_ids == list(range(1, 10))
    marked = [LagEvent(id=i, kind="mark", t_start=3.0 * i, t_end=3.0 * i + 4.25, severity=0.0, detail={},
                       causes=["driver_dpc"]) for i in range(1, 10)]
    half = dict(strong, weight=0.5, half_weight=True)                      # marks stand in at half weight
    assert is_finding(run("driver_dpc", make(), events=marked, causes={"driver_dpc": half}), "low")


@pytest.mark.parametrize("cpu_side, conf", [(7, "high"), (6, "medium")])
def test_the_cpu_side_counts_when_seventy_percent_of_the_events_show_it(cpu_side, conf):
    evs = [stutter(i, 3.0 * i, "game_cpu_spike", side="cpu" if i <= cpu_side else "gpu") for i in range(1, 11)]
    res = run("game_cpu_spike", make(), events=evs, causes={"game_cpu_spike": lift("supports", a=10, n=10)})
    assert is_finding(res, conf)


def test_a_side_the_verdict_stood_in_for_is_no_frame_timing_evidence():
    # A3: attribute.py marks a side taken from the overall verdict (its frames had no GPU timing); only the
    # event's own frame split is a separate instrument from the CPU counters that named the cause
    evs = [stutter(i, 3.0 * i, "game_cpu_spike", side_source="verdict") for i in range(1, 10)]
    res = run("game_cpu_spike", make(), events=evs, causes={"game_cpu_spike": lift("supports")})
    assert is_finding(res, "medium") and not any("CPU side was slow" in e.text for e in res.evidence)
    evs = [stutter(i, 3.0 * i, "game_cpu_spike", side_source="frames") for i in range(1, 10)]
    assert is_finding(run("game_cpu_spike", make(), events=evs, causes={"game_cpu_spike": lift("supports")}), "high")


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
    assert isinstance(res, rules.Unchecked) and res.text.startswith("needs frame timing (PresentMon), which was not recorded (see Not measured)")
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


def test_a_cause_the_marks_named_is_listed_even_with_too_few_marks():
    # Item 11: with only marks, the stutter-cause rules looked for its best-explained events among the stutters and
    # FPS drops, found none, and so a cause too few marks named went to neither list
    few = {"asset_streaming": lift("too_few_events", a=2, n=3, weight=0.5, half_weight=True)}
    res = run("asset_streaming", make(ok=("system",), src={"frames": NO_FRAMES}), stats={"frames": 0},
              events=marked(3, {1, 3}, "asset_streaming"), causes=few)
    assert isinstance(res, rules.Unchecked) and res.text == "only 3 marked moments: blaming a cause needs at least 5"
    none = {"asset_streaming": lift("too_few_events", a=0, n=3, weight=0.5, half_weight=True)}
    assert run("asset_streaming", make(ok=("system",), src={"frames": NO_FRAMES}), stats={"frames": 0},
               events=marked(3), causes=none) is None                              # none of them named it: silent
    enough = {"asset_streaming": lift("supports", a=5, n=6, weight=1.5, half_weight=True)}
    res = run("asset_streaming", make(ok=("system",), src={"frames": NO_FRAMES}), stats={"frames": 0},
              events=marked(6, {1, 2, 3, 4, 5}, "asset_streaming"), causes=enough)
    assert is_finding(res) and res.title == "Lag you marked while the game loads from disk (5 of 6)"
    assert res.event_ids == [1, 2, 3, 4, 5] and any(e.text.startswith("For example at 0.0 s: signal 1")
                                                    for e in res.evidence)


def test_lag_doctors_own_load_is_no_other_program_beside_a_cpu_limit():
    # Item 10: "other programs used X% of a CPU meanwhile" summed lag-doctor's own capture process (target tool_pids)
    own = [[0.25 * k, [[5555, "python.exe", 150.0, 0.0, 0.0], [2656, "dwm.exe", 3.0, 0.0, 3.0]]] for k in range(1, 121)]

    def lines(target):
        res = run("cpu_limit", make(system=synth.system_table(30, game_top_thread_pct=95.0), others=own, target=target),
                  bound={"verdict": "cpu_main", "r": 0.3, "busiest_thread": 0.95})
        return [e.text for e in res.evidence if "other programs used" in e.text.lower()]
    assert lines({"tool_pids": [5555]}) == []
    assert lines({}) == ["Other programs used 153% of a CPU meanwhile (100% = one logical CPU)."]    # control


def test_marks_never_rule_out_the_residual_causes_they_were_not_judged_for():
    marks = [LagEvent(i, "mark", 3.0 * i - 3.0, 3.0 * i + 0.5, 0.0, {}) for i in range(1, 13)]
    causes = {"game_stall": lift("contradicts", a=0, n=12, weight=1.0, lift_=None, half_weight=True)}
    res = run("game_stall", make(ok=("system",), src={"frames": NO_FRAMES}), stats={"frames": 0}, events=marks,
              causes=causes)
    assert res is None


# ------------------------------------------------------- end to end on LagLab-like captures ---

def judged(s):
    """diagnose() of a synthetic LagLab capture and the real end-to-end judge's verdict on it."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from e2e_truth import judge
    from lagdoctor.analysis.diagnose import diagnose
    d = diagnose(s.cap)
    return d, judge(s.log["mode"], s.log, s.cap.clock_origin, d, has_discrete_gpu=True)


@pytest.mark.parametrize("seed", [1, 2, 3])
@pytest.mark.parametrize("mode", synth.MODES)
def test_every_laglab_scenario_passes_the_judge_with_the_measured_os_wakeup_hiccups(mode, seed):
    d, v = judged(synth.laglab(mode, seed=seed, hiccups=synth.MEASURED_HICCUP_RATE))
    assert v.ok, [(c.name, c.detail) for c in v.checks if not c.ok]
    assert not [w for w in d.unchecked.values() if w.startswith("the check failed")]


def test_the_training_job_on_the_games_gpu_is_named_and_no_cap_or_idle_gpu_is_claimed():
    # A7, the review's scenario A2: the ML job keeps the game's GPU 80 % busy; the game runs steadily under 90 % of
    # the refresh with its own GPU work short. Before: "a frame-rate cap", and the GPU "checked and fine".
    d, _ = judged(synth.laglab("smooth", gpu_other_pct=80.0))
    f = next(f for f in d.findings if f.cause == "gpu_contention")
    assert f.confidence == "high" and f.title == "Another program kept the game's GPU 80% busy: python.exe"
    assert "gpu_limit" not in d.cleared and "gpu_limit" in d.unchecked
    assert not [f for f in d.findings if f.cause == "frame_cap"] and "frame_cap" in d.unchecked
    quiet, _ = judged(synth.laglab("smooth"))                      # control: the job on the other GPU
    assert "gpu_contention" in quiet.cleared and "gpu_limit" in quiet.cleared


def test_a_cap_above_a_60_hz_refresh_gets_the_cap_advice():
    # A11: 86 FPS into a 60 Hz composed screen drops every third frame; the cap rule claims the judder
    d, _ = judged(synth.laglab("smooth", fps_cap=86.0, refresh_hz=60.0))
    pacing = [e for e in d.events if e.kind == "pacing"]
    f = next(f for f in d.findings if f.cause == "frame_cap")
    assert pacing and set(f.event_ids) == {e.id for e in pacing}
    assert f.title == "A frame-rate cap above the refresh rate drops frames: 86 FPS on a 60 Hz screen"
    assert f.fixes[0].title == "Cap the frame rate at 60 FPS (the refresh rate)"
    assert not [e for e in d.events if e.kind == "fps_drop"]


@pytest.fixture(scope="module")
def gold_case_5(presentmon_exe, gold_etl_dir):
    """PresentMon's gold test_case_5 (PresentBench, ~86 FPS) replayed (--etl_file needs no rights), 60 Hz panel."""
    import time
    from lagdoctor.capture import presentmon as pm
    from lagdoctor.model import Capture
    src = pm.PresentMonSource(24892, exe=presentmon_exe, etl_file=gold_etl_dir / "test_case_5.etl")
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
    inv = {"cpu": {"threads": 16}, "settings": {"hags": None},
           "displays": [{"device": r"\\.\DISPLAY1", "primary": True, "hz": 60.0}]}
    return Capture(started_at="2026-11-04T00:00:00+00:00", clock_origin=0.0,
                   duration_s=max(x for x in cols[F.T] if isinstance(x, float)), target={"pid": 24892},
                   sources={"frames": SourceStatus("ok")}, inventory=inv, frames=cols)


def test_gold_dropped_frame_judder_is_claimed_not_checked_and_fine(gold_case_5):
    # A11 on PresentMon's own output: 2.9 s is too short for a bound verdict (LC 2.6), yet a third of the frames
    # were never shown; before, uneven_pacing was "checked and fine" and the headline said no lag stood out
    from lagdoctor.analysis.diagnose import diagnose
    from lagdoctor.report import labels
    d = diagnose(gold_case_5)
    pacing = [e for e in d.events if e.kind == "pacing"]
    assert len(pacing) == 1
    assert labels.severity_text("pacing", pacing[0].severity, pacing[0].detail).endswith("ms animation error")
    f = next(f for f in d.findings if f.cause == "dropped_frames")
    assert f.event_ids == [pacing[0].id] and f.title.startswith("Frames are dropped")
    # item 13: its own cause, so the verdict no longer says "possibly VSync judder" where VSync is not involved
    assert "possibly frames dropped (the game runs faster than the screen)" in d.headline, d.headline
    assert "VSync" not in d.headline and not [f for f in d.findings if f.cause == "vsync_judder"]
