"""Acceptance: LagLab-like synthetic captures through frames -> events -> bound -> attribute.

Each lag moment comes from LagLab's own (synthetic) log and is scored with the end-to-end judge's own
truth_moments() / match_events() (scripts/e2e_truth.py), so the bar is the judge's, not ours.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from e2e_truth import EXPECTATIONS, _overlaps, judge, match_events, truth_moments  # noqa: E402

from lagdoctor.analysis import attribute, events, frames  # noqa: E402
from lagdoctor.model import F, Capture, Diagnosis, SourceStatus  # noqa: E402
from tests import synth  # noqa: E402


def pipeline(s: synth.Synth):
    cap = s.cap
    fr = frames.main_chain(cap)
    stats = frames.frame_stats(fr, cap.duration_s)
    evs = events.find_events(cap, fr, stats)
    b = frames.bound(cap, fr, evs)
    summary = attribute.attribute(cap, fr, evs, b)
    return fr, stats, evs, b, summary


@pytest.mark.parametrize("seed", [1, 2, 3])
@pytest.mark.parametrize("mode, cause", [("hitch", "game_cpu_spike"), ("hitch-io", "asset_streaming"),
                                         ("hitch-wait", "game_stall"), ("hog", "background_cpu")])
def test_lag_moments_are_found_and_blamed_on_the_right_cause(mode, cause, seed):
    s = synth.laglab(mode, seed=seed)
    _, _, evs, _, summary = pipeline(s)
    exp = EXPECTATIONS[mode]
    assert exp.event_cause == cause                                   # the judge's own answer for this mode
    truth = truth_moments(s.log, s.cap.clock_origin)
    assert len(truth) == len(s.truth) >= 3
    picked = [e for e in evs if e.kind in exp.event_kinds]
    recall, precision = match_events(truth, [(e.t_start, e.t_end) for e in picked])
    assert recall >= 0.8 and precision >= 0.7, (recall, precision, len(picked))
    matched = [e for e in picked if any(_overlaps(t, (e.t_start, e.t_end), 0.15) for t in truth)]
    right = sum(1 for e in matched if e.causes and e.causes[0] == cause)
    assert matched and right / len(matched) >= 0.7, [e.causes for e in matched]
    assert summary[cause]["label"] == "supports"


@pytest.mark.parametrize("mode", ["hitch", "hitch-io", "hitch-wait"])
def test_the_end_to_end_judge_passes_the_hitch_modes(mode):
    s = synth.laglab(mode)
    _, _, evs, _, _ = pipeline(s)
    v = judge(mode, s.log, s.cap.clock_origin, Diagnosis(lag_type="stutter", headline="", events=evs),
              has_discrete_gpu=False, capture_s=s.cap.duration_s)
    assert v.ok, [(c.name, c.detail) for c in v.checks if not c.ok]


@pytest.mark.parametrize("mode", ["smooth", "gpu", "cpu", "pace"])
def test_steady_modes_have_no_stutters(mode):
    s = synth.laglab(mode)
    fr, stats, evs, _, _ = pipeline(s)
    assert [e for e in evs if e.kind == "stutter"] == []
    assert stats["hitches"] == 0 and stats["hitch_rate_bound"] == pytest.approx(3 / stats["frames"])
    if mode == "smooth":
        v = judge("smooth", s.log, s.cap.clock_origin, Diagnosis(lag_type="none", headline="", events=evs),
                  has_discrete_gpu=False, capture_s=s.cap.duration_s)
        assert v.ok, [(c.name, c.detail) for c in v.checks if not c.ok]


# hog: changed on purpose (2026-11-10). Its game work is now a fixed amount the hogs can slow (laglab.app.FixedWork),
# 6 ms of it, so that bursts drop below 80 % of the 120 FPS cap; the main thread then works and spins out the cap for
# 98 % of a CPU between bursts, and a capped verdict needs it under 85 % (LC 3.2).
@pytest.mark.parametrize("mode, verdict", [("smooth", "capped"), ("hitch", "capped"), ("hitch-io", "capped"),
                                           ("hitch-wait", "capped"), ("hog", "cpu_main"), ("gpu", "gpu"),
                                           ("cpu", "cpu_main"), ("pace", "waiting")])
def test_bound_verdict_per_mode(mode, verdict):
    _, _, _, b, _ = pipeline(synth.laglab(mode))
    assert b["verdict"] == verdict, b["notes"]
    if verdict == "capped":
        assert b["cap_fps"] == pytest.approx(120.0, abs=0.5)
    assert b["confidence"] == "high"


def test_pacing_detail_separates_uneven_presents_from_display_judder():
    # 120 FPS on the 144 Hz composed panel judders (5 frames per 6 refreshes) though every present is even;
    # LagLab's pace mode alternates its presents themselves
    judder = [e for e in pipeline(synth.laglab("smooth"))[2] if e.kind == "pacing"]
    uneven = [e for e in pipeline(synth.laglab("pace"))[2] if e.kind == "pacing"]
    assert judder and uneven
    assert all(e.detail["present_swing_pct"] <= 5.0 and e.detail["quantised_pct"] == 100.0 for e in judder)
    assert all(e.detail["present_swing_pct"] >= 50.0 for e in uneven)


def test_the_hog_events_name_the_hogs():
    _, _, evs, _, _ = pipeline(synth.laglab("hog"))
    blamed = [e for e in evs if e.causes and e.causes[0] == "background_cpu"]
    assert blamed and all(e.detail["process"] == "python.exe" and len(e.detail["process_pids"]) >= 5
                          for e in blamed)


def test_network_spikes_losses_and_server_gaps_become_events():
    s = synth.laglab("smooth", net_spikes=[(10.0, 11.0, 80.0)], net_losses=[20.0, 20.5],
                     server_gaps=[(15.0, 180.0, "delay"), (16.0, 60.0, "loss")])
    evs = pipeline(s)[2]
    spikes = [e for e in evs if e.kind == "net_spike"]
    assert len(spikes) == 1 and spikes[0].t_start <= 10.0 + 0.5 and spikes[0].t_end >= 11.0 - 0.5
    assert spikes[0].severity >= 80.0 and spikes[0].detail["target"] == "internet"
    loss = [e for e in evs if e.kind == "net_loss"]
    assert len(loss) == 1 and loss[0].t_start <= 20.0 <= loss[0].t_end and loss[0].detail["burst"]
    gaps = [e for e in evs if e.kind == "net_gap"]
    assert [(g.t_start, g.severity, g.detail["kind"]) for g in gaps] == [(15.0, 180.0, "delay")]


@pytest.mark.parametrize("label, kw", [("one lost probe", dict(net_losses=[60.2])),
                                       ("one 42 ms ping", dict(net_spikes=[(60.0, 60.4, 36.0)]))])
def test_one_lost_or_slow_ping_in_two_minutes_is_no_network_event(label, kw):
    # A8, at the network sampler's real 0.5 s rounds (synth._network)
    s = synth.laglab("smooth", duration_s=120.0, **kw)
    assert len(s.cap.net_flows["lost"]["internet"]) == len(kw.get("net_losses", ()))     # the fixture did it
    assert [e for e in pipeline(s)[2] if e.kind.startswith("net_")] == [], label


def test_four_lost_rounds_in_a_row_are_one_loss_burst():
    s = synth.laglab("smooth", duration_s=120.0, net_losses=[60.0, 60.5, 61.0, 61.5])
    loss = [e for e in pipeline(s)[2] if e.kind == "net_loss"]
    assert len(loss) == 1 and loss[0].detail["burst"] and loss[0].detail["lost"] == 4


def test_a_cap_above_a_60_hz_refresh_is_animation_judder_not_an_fps_drop():
    # A11: 86 FPS into a 60 Hz composed display drops every third frame; every shown frame holds one refresh,
    # so the swings are 0 ms and only the animation error says it is jerky
    s = synth.laglab("smooth", fps_cap=86.0, refresh_hz=60.0)
    fr, _, evs, _, _ = pipeline(s)
    pacing = [e for e in evs if e.kind == "pacing"]
    assert pacing and all(e.detail["criterion"] == "anim" and e.severity >= 2.0 for e in pacing)
    assert all(e.t_start >= min(fr[F.T]) for e in evs if e.kind != "mark")
    assert [e for e in evs if e.kind == "fps_drop"] == []


def test_marks_link_to_the_stutters_they_cover():
    s = synth.laglab("hitch", marks=[3.9])                     # pressed half a second after the first hitch
    evs = pipeline(s)[2]
    marks = [e for e in evs if e.kind == "mark"]
    assert marks[0].detail["visible_in_frames"] is True
    first = [e for e in evs if e.kind == "stutter"][0]
    assert first.id in marks[0].detail["frame_events"]


def test_a_decoy_swap_chain_does_not_move_the_main_chain():
    plain = pipeline(synth.laglab("hitch"))
    decoy_s = synth.laglab("hitch", decoy_chain=True)
    assert len(set(decoy_s.cap.frames[F.SWAP_CHAIN])) == 2
    decoy = pipeline(decoy_s)
    assert set(decoy[0][F.SWAP_CHAIN]) == {"0x0"} and decoy[1] == plain[1]
    assert [(e.kind, e.t_start, e.causes) for e in decoy[2]] == [(e.kind, e.t_start, e.causes) for e in plain[2]]


def test_without_frame_timing_only_marks_and_the_network_remain():
    s = synth.laglab("hitch", with_frames=False, marks=[3.9])
    fr, stats, evs, b, summary = pipeline(s)
    assert stats["frames"] == 0 and {e.kind for e in evs} == {"mark"}
    assert b["verdict"] == "unknown" and "frame timing not measured" in b["notes"]
    assert summary and all(v.get("half_weight") for v in summary.values())


def test_same_seed_same_answer():
    a, b = pipeline(synth.laglab("hog", seed=7)), pipeline(synth.laglab("hog", seed=7))
    assert [(e.kind, e.t_start, e.t_end, e.causes) for e in a[2]] == [(e.kind, e.t_start, e.t_end, e.causes)
                                                                       for e in b[2]]


def test_os_wakeup_hiccups_never_make_a_hitch_in_the_smooth_control():
    """With the wake-up delays measured on this laptop, the worst a smooth run shows is a frame held for
    3 refreshes (20.8 ms) where the 120 FPS cadence holds 2: one refresh late, which is no stutter at all (A2).
    This test used to allow such "stutter"-class events; that pinned the wrong behaviour."""
    slips = 0
    for seed in range(1, 6):
        s = synth.laglab("smooth", seed=seed, hiccups=synth.MEASURED_HICCUP_RATE)
        fr, stats, evs, _, _ = pipeline(s)
        slips += sum(1 for x in frames.display_series(fr)[2] if x > 2.5 * 1000.0 / 144.003)
        assert stats["hitches"] == 0
        assert [e for e in evs if e.kind == "stutter"] == []
    assert slips >= 3                                    # the 3-refresh holds this guards are in the data


@pytest.mark.parametrize("fps_cap", [100.0, 120.0, 130.0])
@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_the_smooth_control_with_the_measured_hiccups_has_no_stutters(fps_cap, seed):
    # A2: caps between 0.67 and 1 x the 144 Hz refresh hold frames 1 or 2 refreshes; a late wake-up adds one
    s = synth.laglab("smooth", seed=seed, fps_cap=fps_cap, hiccups=synth.MEASURED_HICCUP_RATE)
    _, _, evs, _, _ = pipeline(s)
    assert [e for e in evs if e.kind == "stutter"] == []


P144 = 1000.0 / synth.REFRESH_HZ


def three_refresh_holds(fr) -> int:
    return sum(1 for x in frames.display_series(fr)[2] if round(x / P144) == 3)


@pytest.mark.parametrize("fps_cap", [137.0, 138.0, 141.0, 142.0, 143.0])
def test_a_cap_just_under_the_refresh_has_no_stutters_with_the_measured_hiccups(fps_cap):
    # Item 8 (A2): under the refresh every cadence holds some frames two refreshes (one in 47 at the advised 141 FPS
    # cap on 144 Hz), so a late wake-up's 3-refresh hold is one refresh late. A 5 % slack read these caps as
    # one-refresh cadences and made those 20.8 ms frames "stutters"
    slips = 0
    for seed in range(1, 9):
        s = synth.laglab("smooth", seed=seed, fps_cap=fps_cap, hiccups=synth.MEASURED_HICCUP_RATE)
        fr, _, evs, _, _ = pipeline(s)
        slips += three_refresh_holds(fr)
        assert [e for e in evs if e.kind == "stutter"] == [], seed
    assert slips >= 3                                    # the slips this guards are in the data


def test_a_game_held_to_the_refresh_still_stutters_when_it_misses_two_refreshes():
    # Item 8: the slack absorbs only the jitter of the present cadence, so at a cadence on the refresh itself a
    # frame held 3 refreshes (20.8 ms, +13.9 ms over the one-refresh hold) is still a stutter, every time
    slips = stutters = 0
    for seed in range(1, 6):
        s = synth.laglab("smooth", seed=seed, fps_cap=synth.REFRESH_HZ, hiccups=synth.MEASURED_HICCUP_RATE)
        fr, _, evs, _, _ = pipeline(s)
        slips += three_refresh_holds(fr)
        found = [e for e in evs if e.kind == "stutter"]
        stutters += len(found)
        assert all(e.severity == pytest.approx(3 * P144, abs=0.05) for e in found), seed
    assert stutters == slips >= 5


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_the_end_to_end_judge_passes_the_smooth_control_with_the_measured_hiccups(seed):
    from lagdoctor.analysis.diagnose import diagnose
    s = synth.laglab("smooth", seed=seed, hiccups=synth.MEASURED_HICCUP_RATE)
    d = diagnose(s.cap)
    v = judge("smooth", s.log, s.cap.clock_origin, d, has_discrete_gpu=True, capture_s=s.cap.duration_s)
    assert v.ok, [(c.name, c.detail) for c in v.checks if not c.ok]


@pytest.mark.parametrize("seed", [1, 2, 3])
@pytest.mark.parametrize("mode", synth.HITCH_MODES)
def test_the_hitches_are_still_found_with_the_measured_hiccups(mode, seed):
    s = synth.laglab(mode, seed=seed, hiccups=synth.MEASURED_HICCUP_RATE)
    evs = pipeline(s)[2]
    truth = truth_moments(s.log, s.cap.clock_origin)
    recall, precision = match_events(truth, [(e.t_start, e.t_end) for e in evs if e.kind == "stutter"])
    assert recall == 1.0 and precision == 1.0, (recall, precision)     # no wake-up slip became a stutter


def test_a_stall_is_never_reported_as_no_hitches():
    # A4: the rows of two 5 s freezes are missing (PM 2.4); the headline once said "no hitches >= 50 ms"
    from lagdoctor.analysis.diagnose import diagnose
    fr = synth.frames_table([10.0] * 3900, display_ms=10.0, displayed=1, gpu_ms=0.5)
    fr[F.T] = [t + (5.0 if i >= 500 else 0.0) + (5.0 if i >= 2000 else 0.0) for i, t in enumerate(fr[F.T])]
    cap = synth.capture(fr, synth.system_table(50.0, game_top_thread_pct=20.0), duration=50.0)
    cap.sources = synth.sources(("frames", "system"))
    d = diagnose(cap)
    assert d.lag_type != "unknown"                                 # frame timing counts as measured
    assert d.stats["hitches"] == 2 and d.stats["freezes"] == 2 and d.stats["worst_frame_ms"] >= 5000.0
    assert d.stats["hitch_rate_bound"] is None and "no hitches" not in d.headline


# ------------------------------------------------------------------- PresentMon's own captures ---

GOLD = (("test_case_0", 1268), ("test_case_5", 24892))     # dwm.exe with four hitches; PresentBench at ~86 FPS


@pytest.fixture(scope="module")
def gold(presentmon_exe, gold_etl_dir):
    """PresentMon's gold ETW captures replayed (--etl_file needs no rights), on a 60 Hz panel, t rebased to 0."""
    from lagdoctor.capture import presentmon as pm
    out = {}
    for case, pid in GOLD:
        src = pm.PresentMonSource(pid, exe=presentmon_exe, etl_file=gold_etl_dir / f"{case}.etl")
        src.start(0.0)
        deadline = time.monotonic() + 60.0
        while src._thread is not None and src._thread.is_alive():
            assert time.monotonic() < deadline, f"the {case} replay did not finish in 60 s"
            time.sleep(0.1)
        src.stop()
        assert src.status().state == "ok", src.status()
        cols = {k: list(v) for k, v in src.result()["columns"].items()}
        t0 = min(x for x in cols[F.T] if isinstance(x, float))
        cols[F.T] = [x - t0 if isinstance(x, float) else x for x in cols[F.T]]
        inv = {"cpu": {"threads": 16}, "settings": {"hags": None},
               "displays": [{"device": r"\\.\DISPLAY1", "primary": True, "hz": 60.0}]}
        out[case] = Capture(started_at="2026-11-04T00:00:00+00:00", clock_origin=0.0,
                            duration_s=max(x for x in cols[F.T] if isinstance(x, float)), target={"pid": pid},
                            sources={"frames": SourceStatus("ok")}, inventory=inv, frames=cols)
    return out


def events_of(cap):
    fr = frames.main_chain(cap)
    return events.find_events(cap, fr, frames.frame_stats(fr, cap.duration_s))


def test_gold_dropped_frame_judder_reports_its_animation_error(gold):
    # A11 on PresentMon's own output: PresentBench presents ~86 FPS into a 60 Hz composed screen and a third of
    # its frames are dropped; every shown frame holds one refresh, so only the animation error is jerky
    pacing = [e for e in events_of(gold["test_case_5"]) if e.kind == "pacing"]
    assert len(pacing) == 1 and pacing[0].detail["criterion"] == "anim"
    assert pacing[0].severity == pytest.approx(pacing[0].detail["anim_p95_ms"]) and pacing[0].severity > 2.0
    assert pacing[0].t_start >= 0.0                                   # the first row, rebased to 0


def test_gold_hitches_do_not_count_as_their_own_pacing(gold):
    # A1 on PresentMon's own output: dwm.exe's four hitches (100 to 450 ms) are stutters; without their swings
    # under 10 % of the swings reach 4 ms (15 % with them)
    evs = events_of(gold["test_case_0"])
    assert sorted(round(e.severity) for e in evs if e.kind == "stutter") == [100, 150, 284, 450]
    pacing = [e for e in evs if e.kind == "pacing"]
    assert pacing and all(e.detail["swing_pct"] < 10.0 for e in pacing)


def test_synth_rejects_unknown_modes_and_knobs():
    with pytest.raises(ValueError):
        synth.laglab("turbo")
    with pytest.raises(TypeError):
        synth.laglab("smooth", fps=60)
