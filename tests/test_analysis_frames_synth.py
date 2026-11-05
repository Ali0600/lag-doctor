"""Acceptance: LagLab-like synthetic captures through frames -> events -> bound -> attribute.

Each lag moment comes from LagLab's own (synthetic) log and is scored with the end-to-end judge's own
truth_moments() / match_events() (scripts/e2e_truth.py), so the bar is the judge's, not ours.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from e2e_truth import EXPECTATIONS, _overlaps, judge, match_events, truth_moments  # noqa: E402

from lagdoctor.analysis import attribute, events, frames  # noqa: E402
from lagdoctor.model import F, Diagnosis  # noqa: E402
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
              has_discrete_gpu=False)
    assert v.ok, [(c.name, c.detail) for c in v.checks if not c.ok]


@pytest.mark.parametrize("mode", ["smooth", "gpu", "cpu", "pace"])
def test_steady_modes_have_no_stutters(mode):
    s = synth.laglab(mode)
    fr, stats, evs, _, _ = pipeline(s)
    assert [e for e in evs if e.kind == "stutter"] == []
    assert stats["hitches"] == 0 and stats["hitch_rate_bound"] == pytest.approx(3 / stats["frames"])
    if mode == "smooth":
        v = judge("smooth", s.log, s.cap.clock_origin, Diagnosis(lag_type="none", headline="", events=evs),
                  has_discrete_gpu=False)
        assert v.ok, [(c.name, c.detail) for c in v.checks if not c.ok]


@pytest.mark.parametrize("mode, verdict", [("smooth", "capped"), ("hitch", "capped"), ("hitch-io", "capped"),
                                           ("hitch-wait", "capped"), ("hog", "capped"), ("gpu", "gpu"),
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
    s = synth.laglab("smooth", net_spikes=[(10.0, 11.0, 80.0)], net_losses=[20.0],
                     server_gaps=[(15.0, 180.0, "delay"), (16.0, 60.0, "loss")])
    evs = pipeline(s)[2]
    spikes = [e for e in evs if e.kind == "net_spike"]
    assert len(spikes) == 1 and spikes[0].t_start <= 10.0 + 0.5 and spikes[0].t_end >= 11.0 - 0.5
    assert spikes[0].severity >= 80.0 and spikes[0].detail["target"] == "internet"
    loss = [e for e in evs if e.kind == "net_loss"]
    assert len(loss) == 1 and loss[0].t_start <= 20.0 <= loss[0].t_end and loss[0].detail["burst"]
    gaps = [e for e in evs if e.kind == "net_gap"]
    assert [(g.t_start, g.severity, g.detail["kind"]) for g in gaps] == [(15.0, 180.0, "delay")]


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
    3 refreshes (20.8 ms) instead of 1 or 2: a "stutter"-class event at most, never a hitch."""
    for seed in range(1, 6):
        s = synth.laglab("smooth", seed=seed, hiccups=synth.MEASURED_HICCUP_RATE)
        _, stats, evs, _, _ = pipeline(s)
        assert stats["hitches"] == 0
        assert all(e.detail["class"] == "stutter" and e.severity < 21.0 for e in evs if e.kind == "stutter")


def test_synth_rejects_unknown_modes_and_knobs():
    with pytest.raises(ValueError):
        synth.laglab("turbo")
    with pytest.raises(TypeError):
        synth.laglab("smooth", fps=60)
