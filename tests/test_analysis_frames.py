"""analysis/frames.py: main swap chain, frame statistics and the bound classifier, at literal points.

Thresholds are pinned just either side of their value; the live test runs LagLab's own frame-cap loop.
"""
from __future__ import annotations

import math
import time

import pytest

from lagdoctor.analysis import events as ev
from lagdoctor.analysis import frames as fa
from lagdoctor.model import F, S, Capture, LagEvent
from tests.synth import capture as make_cap, frames_table as make_frames, system_table as make_system

P144 = 1000.0 / 144.003


def bound_of(frame_ms=20.0, gpu_ms=None, thread=50.0, n=1200, cpu_pct=None, **kw):
    events = kw.pop("events", [])
    fr = make_frames([frame_ms] * n if not isinstance(frame_ms, list) else frame_ms, gpu_ms=gpu_ms,
                     display_ms=kw.pop("display_ms", None), displayed=kw.pop("displayed", None),
                     frame_type=kw.pop("frame_type", None), cpu_busy=kw.pop("cpu_busy", None))
    secs = 2.0 + sum(fr[F.FRAME_MS]) / 1000.0
    cols = {}
    if thread is not None:
        cols[S.GAME_TOP_THREAD_PCT] = thread
    if cpu_pct is not None:
        cols[S.GAME_CPU_PCT] = cpu_pct
    for k in [k for k in kw if k in S.ALL]:
        cols[k] = kw.pop(k)
    cap = make_cap(fr, make_system(secs, **cols) if cols else {}, **kw)
    return fa.bound(cap, fa.main_chain(cap), events)


# ----------------------------------------------------------------------------------- main chain ---

def test_main_chain_is_the_one_with_most_displayed_frames_not_most_rows():
    many_rows = make_frames([10.0] * 50, displayed=[1 if i < 5 else 0 for i in range(50)], chain="0x1A2B")
    shown = make_frames([10.0] * 20, displayed=1, chain="0x0", t0=1.003)
    frames = {c: many_rows[c] + shown[c] for c in F.ALL}
    fr = fa.main_chain(make_cap(frames))
    assert set(fr[F.SWAP_CHAIN]) == {"0x0"} and len(fr[F.T]) == 20


def test_main_chain_sorts_by_time_and_turns_junk_into_none():
    fr = make_frames([10.0, 11.0, 12.0])
    fr[F.T] = [3.0, 1.0, 2.0]
    fr[F.GPU_BUSY_MS] = ["7", float("nan"), True]
    fr[F.RUNTIME] = [5, "Other", None]
    out = fa.main_chain(make_cap(fr))
    assert out[F.T] == [1.0, 2.0, 3.0]
    assert out[F.FRAME_MS] == [11.0, 12.0, 10.0]
    assert out[F.GPU_BUSY_MS] == [None, 1, None]          # NaN and a string are missing; a bool is 0/1
    assert out[F.RUNTIME] == ["Other", None, None]


def test_main_chain_of_no_frames_is_empty_columns():
    fr = fa.main_chain(make_cap({}))
    assert set(fr) == set(F.ALL) and all(v == [] for v in fr.values())


# ----------------------------------------------------------------------------------- statistics ---

def test_one_percent_low_takes_exactly_ceil_of_one_percent_of_frames():
    d = [10.0] * 1040 + [50.0] * 10                      # n = 1050: ceil(10.5) = 11 frames, floor would be 10
    s = fa.frame_stats(make_frames(d), 20.0)
    assert s["low1_fps"] == pytest.approx(1000.0 / ((10 * 50.0 + 10.0) / 11))
    assert s["low1_fps"] != pytest.approx(20.0)


def test_one_percent_low_does_not_sweep_in_ties():
    # LC 2.5's tie case: 1990 frames of 6.94 ms + 10 hitches of 60 ms. "All frames >= the quantile" would
    # average every frame (138.8 FPS); exactly ceil(1 % of n) = 20 frames gives 29.9 FPS.
    s = fa.frame_stats(make_frames([6.94] * 1990 + [60.0] * 10), 20.0)
    assert s["low1_fps"] == pytest.approx(29.88, abs=0.01)
    assert s["avg_fps"] == pytest.approx(138.79, abs=0.01)
    assert s["p1_fps"] == pytest.approx(1000.0 / 6.94)     # R-7 P99 sits inside the 6.94 ms block: it hides them


def test_one_percent_figures_need_1000_frames():
    assert fa.frame_stats(make_frames([10.0] * 999), 10.0)["low1_fps"] is None
    assert fa.frame_stats(make_frames([10.0] * 999), 10.0)["p1_fps"] is None
    s = fa.frame_stats(make_frames([10.0] * 1000), 10.0)
    assert s["low1_fps"] == pytest.approx(100.0) and s["p99_frame_ms"] == pytest.approx(10.0)


def test_point_one_percent_low_needs_10000_frames():
    assert fa.frame_stats(make_frames([5.0] * 9999), 50.0)["low01_fps"] is None
    assert fa.frame_stats(make_frames([5.0] * 9990 + [40.0] * 10), 50.0)["low01_fps"] == pytest.approx(25.0)


def test_hitch_classes_nest_and_need_twice_the_baseline():
    base = [10.0] * 40
    for d, want in ((49.9, (0, 0, 0)), (50.0, (1, 0, 0)), (149.9, (1, 0, 0)), (150.0, (1, 1, 0)),
                    (499.9, (1, 1, 0)), (500.0, (1, 1, 1))):
        s = fa.frame_stats(make_frames(base + [d] + base), 5.0)
        assert (s["hitches"], s["severe_hitches"], s["freezes"]) == want, d
    # 50 ms against a 25.1 ms baseline is under 2 M: a slow game, not a hitch
    s = fa.frame_stats(make_frames([25.1] * 40 + [50.0] + [25.1] * 5), 5.0)
    assert s["hitches"] == 0
    s = fa.frame_stats(make_frames([24.9] * 40 + [50.0] + [24.9] * 5), 5.0)
    assert s["hitches"] == 1


def test_time_beyond_50_ms_and_the_rule_of_three():
    s = fa.frame_stats(make_frames([10.0] * 30 + [60.0, 50.0, 70.0]), 5.0)
    assert s["time_beyond_50_ms"] == pytest.approx(30.0)            # (60-50) + 0 + (70-50)
    assert s["hitch_rate_bound"] is None                              # there were hitches
    assert fa.frame_stats(make_frames([10.0] * 31), 1.0)["hitch_rate_bound"] == pytest.approx(3 / 31)
    assert fa.frame_stats(make_frames([10.0] * 30), 1.0)["hitch_rate_bound"] is None   # n must exceed 30


def test_dropped_share_mode_and_runtime():
    fr = make_frames([10.0] * 8, displayed=[1, 0, 1, 1, 1, 0, 1, None], display_ms=[10.0, None] + [10.0] * 6)
    fr[F.PRESENT_MODE] = ["Composed: Flip"] * 5 + ["Hardware: Independent Flip"] * 3
    s = fa.frame_stats(fr, 1.0)
    assert s["dropped_pct"] == pytest.approx(100.0 * 2 / 7)
    assert s["present_mode"] == "Composed: Flip" and s["runtime"] == "Other"
    assert s["frames_source"] == "display" and s["frames"] == 6     # not dropped, with a display time


def test_display_series_falls_back_to_presents_when_display_tracking_is_missing():
    rows, t, d, src = fa.display_series(make_frames([8.0, 9.0, 10.0]))
    assert src == "present" and d == [8.0, 9.0, 10.0] and rows == [0, 1, 2]
    rows, t, d, src = fa.display_series(make_frames([8.0, 9.0, 10.0], display_ms=[None, 7.0, 14.0], displayed=1))
    assert src == "display" and d == [7.0, 14.0] and rows == [1, 2]


def test_baseline_window_size():
    assert fa.baseline_frames(4.0) == 20          # 10 * 2 = 20
    assert fa.baseline_frames(3.9) == 20          # clamped up
    assert fa.baseline_frames(16.0) == 40
    assert fa.baseline_frames(144.0) == 120       # 10 * 12 = 120
    assert fa.baseline_frames(146.0) == 120       # clamped down
    assert fa.baseline_frames(6.25) == 25


def test_frame_stats_of_nothing_is_not_measured():
    s = fa.frame_stats(make_frames([]), 30.0)
    assert s["frames"] == 0 and s["avg_fps"] is None and s["hitches"] is None and s["duration_s"] == 30.0


def stalled(seconds, n=3000, at=1500):
    """n frames of 10 ms whose rows from `at` on come `seconds` later: the rows of a stall are missing (PM 2.4)."""
    fr = make_frames([10.0] * n)
    fr[F.T] = [t + (seconds if i >= at else 0.0) for i, t in enumerate(fr[F.T])]
    return fr


def test_a_stall_counts_in_the_frame_statistics():
    # A4: 5 s of missing rows is one 5,010 ms interval (the gap holds the next row's own 10 ms)
    s = fa.frame_stats(stalled(5.0), 40.0)
    assert s["frames"] == 3000
    assert (s["hitches"], s["severe_hitches"], s["freezes"]) == (1, 1, 1)
    assert s["worst_frame_ms"] == pytest.approx(5010.0)
    assert s["time_beyond_50_ms"] == pytest.approx(4960.0)
    assert s["avg_fps"] == pytest.approx(3000 / 35.0)               # 2,999 x 10 ms + 5,010 ms
    assert s["low1_fps"] == pytest.approx(1000.0 / ((5010.0 + 29 * 10.0) / 30))
    assert s["hitch_rate_bound"] is None


def test_a_stall_too_short_to_be_a_hitch_still_withholds_the_rule_of_three():
    # 30 ms of missing rows at a 10 ms baseline is a stall (>= 2.5 M, +8 ms) but no hitch: the counts stay 0,
    # yet frames were not seen, so "no hitches in n frames" cannot be claimed
    s = fa.frame_stats(stalled(0.020), 1.0)
    assert s["hitches"] == 0 and s["worst_frame_ms"] == pytest.approx(30.0)
    assert s["hitch_rate_bound"] is None
    assert fa.frame_stats(stalled(0.0), 1.0)["hitch_rate_bound"] == pytest.approx(3 / 3000)   # the control


def test_frame_stats_and_the_stutter_events_see_the_same_stalls():
    fr = stalled(0.6, at=1000)
    fr[F.T] = [t + (0.3 if i >= 2000 else 0.0) for i, t in enumerate(fr[F.T])]
    gaps = fa.stall_gaps(fr)
    assert [round(g[2]) for g in gaps] == [610, 310]
    s = fa.frame_stats(fr, 40.0)
    assert s["hitches"] == 2 and s["freezes"] == 1 and s["worst_frame_ms"] == pytest.approx(610.0)
    cap = make_cap(fr)
    stalls = [e for e in ev.find_events(cap, fa.main_chain(cap), s) if e.kind == "stutter"]
    assert [(round(e.severity), e.detail["stall"]) for e in stalls] == [(610, True), (310, True)]


def test_stalls_with_no_frame_shown_between_them_are_one_frozen_screen():
    # rows 1500 and 1501 both follow a stall and row 1500 was never shown: the screen froze across both
    fr = make_frames([10.0] * 3000, displayed=[0 if i == 1500 else 1 for i in range(3000)], display_ms=10.0)
    fr[F.T] = [t + (0.3 if i >= 1500 else 0.0) + (0.4 if i >= 1501 else 0.0) for i, t in enumerate(fr[F.T])]
    assert len(fa.stall_gaps(fr)) == 2
    s = fa.frame_stats(fr, 40.0)
    assert s["worst_frame_ms"] == pytest.approx(720.0) and s["hitches"] == 1     # 310 + 410 ms, one interval


# ------------------------------------------------------------------------------------ the bound ---

def test_gpu_bound_at_r_090_and_mixed_just_below():
    assert bound_of(20.0, 18.0)["verdict"] == "gpu"           # r = 0.900
    b = bound_of(20.0, 17.9)                                  # r = 0.895
    assert b["verdict"] == "mixed" and b["r"] == pytest.approx(0.895)


def test_gpu_bound_when_frame_time_minus_gpu_busy_is_at_most_1_ms():
    assert bound_of(5.0, 4.0, n=2400)["verdict"] == "gpu"     # r = 0.80, but FT - G = 1.0 ms
    assert bound_of(5.0, 3.95, n=2400)["verdict"] == "mixed"  # FT - G = 1.05 ms


def test_hags_margin_comes_off_gpu_busy():
    assert bound_of(20.0, 18.4, hags=False)["verdict"] == "gpu"          # 18.4 / 20 = 0.92
    b = bound_of(20.0, 18.4, hags=True)                                  # (18.4 - 0.5) / 20 = 0.895
    assert b["verdict"] == "mixed" and b["gpu_ms"] == pytest.approx(17.9)
    assert bound_of(20.0, 18.4, hags=None)["verdict"] == "gpu"           # unknown: no margin, said in notes


def test_cpu_main_needs_the_busiest_thread_at_085():
    jitter = [20.0 * (1.1 if i % 2 else 0.9) for i in range(1200)]       # irregular: not a frame cap
    assert bound_of(jitter, 10.0, thread=85.0)["verdict"] == "cpu_main"
    assert bound_of(jitter, 10.0, thread=84.9)["verdict"] == "waiting"
    assert bound_of(jitter, 15.0, thread=90.0)["verdict"] == "cpu_main"   # r = 0.75 still counts as CPU-or-other
    assert bound_of(jitter, 15.2, thread=90.0)["verdict"] == "mixed"      # r = 0.76


def test_cpu_all_cores_needs_085_of_every_logical_cpu():
    jitter = [20.0 * (1.1 if i % 2 else 0.9) for i in range(1200)]
    assert bound_of(jitter, 10.0, thread=50.0, cpu_pct=1360.0)["verdict"] == "cpu_all"   # 0.85 x 16 CPUs
    assert bound_of(jitter, 10.0, thread=50.0, cpu_pct=1359.0)["verdict"] == "waiting"


def test_without_thread_data_cpu_cannot_be_told_from_waiting():
    jitter = [20.0 * (1.1 if i % 2 else 0.9) for i in range(1200)]
    b = bound_of(jitter, 10.0, thread=None)
    assert b["verdict"] == "unknown" and b["busiest_thread"] is None and b["r"] == pytest.approx(0.5)


def test_the_busiest_thread_is_judged_over_1_s_means_not_single_ticks():
    # one tick in four at 100 %, the rest at 80 %: no single-tick read decides, the 1 s mean is 85 %
    jitter = [20.0 * (1.1 if i % 2 else 0.9) for i in range(1200)]
    b = bound_of(jitter, 10.0, thread=lambda t: 100.0 if round(t / 0.25) % 4 == 0 else 80.0)
    assert b["busiest_thread"] == pytest.approx(0.85) and b["verdict"] == "cpu_main"


def test_the_bound_reads_the_lead_thread_where_the_capture_has_it():
    # Each tick's busiest thread reads 93.75 % when four threads take turns at 75 %: the bound judges the lead thread
    # (75 %), which is no CPU limit; a capture from before that column is judged on the per-tick column, as before
    jitter = [20.0 * (1.1 if i % 2 else 0.9) for i in range(1200)]
    b = bound_of(jitter, 10.0, thread=93.75, game_lead_thread_pct=75.0)
    assert b["busiest_thread"] == pytest.approx(0.75) and b["verdict"] == "waiting"
    old = bound_of(jitter, 10.0, thread=93.75)
    assert old["busiest_thread"] == pytest.approx(0.9375) and old["verdict"] == "cpu_main"


def _wobble(n, base, rel):
    """Frame times cycling base*(1-rel), base, base*(1+rel): median base, MAD base*rel, so the robust
    CoV is 1.4826 * rel in any window (a two-value cycle would give MAD 0 in odd-sized windows)."""
    return [base * (1 + rel * ((i % 3) - 1)) for i in range(n)]


def test_frame_cap_is_a_regular_cadence_off_the_refresh_multiples():
    b = bound_of(_wobble(1500, 8.333, 0.0290 / 1.4826), 0.5, thread=30.0, hz=144.003)
    assert b["verdict"] == "capped" and b["cap_fps"] == pytest.approx(120.0, abs=0.1)
    b = bound_of(_wobble(1500, 8.333, 0.0310 / 1.4826), 0.5, thread=30.0, hz=144.003)
    assert b["verdict"] == "waiting" and b["cap_fps"] is None


def test_a_cadence_within_3_percent_of_a_refresh_multiple_is_not_a_cap():
    near = 2 * P144 * 1.029
    far = 2 * P144 * 1.031
    assert bound_of(near, 0.5, thread=30.0, hz=144.003)["verdict"] == "waiting"
    assert bound_of(far, 0.5, thread=30.0, hz=144.003)["verdict"] == "capped"


@pytest.mark.parametrize("shared, verdict", [(10.0, "unknown"), (9.9, "capped")])
def test_a_regular_cadence_on_a_shared_gpu_is_no_cap(shared, verdict):
    # Item 9 (A7, LC 4.6): other programs at 10 % or more of the game's GPU pace it as evenly as a cap would, and its
    # own GPU busy reads short (PM 3.2). It was "capped", the game's own target, so 58 FPS on 144 Hz was never lag
    b = bound_of(17.2, 3.4, thread=30.0, hz=144.003, gpu_other_pct=shared)
    assert b["verdict"] == verdict
    if verdict == "unknown":
        assert b["cap_fps"] is None and b["confidence"] == "low"
        assert b["notes"][0].startswith("other programs kept the game's GPU 10% busy (median of 1 s windows")
    else:
        assert b["cap_fps"] == pytest.approx(1000.0 / 17.2)


@pytest.mark.parametrize("shared, verdict", [(10.0, "unknown"), (9.9, "waiting")])
def test_a_short_gpu_busy_on_a_shared_gpu_is_not_waiting_on_something_else(shared, verdict):
    # A7: "neither the CPU nor the GPU is at its limit" cannot be said while other programs keep that GPU busy
    jitter = [17.2 * (1.1 if i % 4 < 2 else 0.9) for i in range(1200)]       # irregular: not a frame cap
    assert bound_of(jitter, 3.4, thread=30.0, hz=144.003, gpu_other_pct=shared)["verdict"] == verdict


def test_a_shared_gpu_leaves_the_other_verdicts_alone():
    for kw, verdict in ((dict(frame_ms=20.0, gpu_ms=19.5), "gpu"), (dict(frame_ms=26.0, gpu_ms=0.5, thread=99.0),
                                                                   "cpu_main")):
        assert bound_of(hz=144.003, gpu_other_pct=80.0, **{"thread": 30.0, **kw})["verdict"] == verdict


def test_the_steady_frame_rate_is_the_displayed_rate_outside_lag_events():
    # Item 6 (A1): what lag time judges low FPS on. A 600 ms freeze pulls the average down, never the steady rate;
    # frames never shown do not count where display is tracked, as in avg_fps
    ms = [8.0] * 600 + [600.0] + [8.0] * 600
    cap = make_cap(make_frames(ms, gpu_ms=7.8, display_ms=ms, displayed=1), hz=144.003)
    fr = fa.main_chain(cap)
    evs = ev.find_events(cap, fr, None)
    assert [e.severity for e in evs if e.kind == "stutter"] == [600.0]
    assert fa.bound(cap, fr, evs)["steady_fps"] == pytest.approx(125.0)
    assert fa.frame_stats(fr, cap.duration_s)["avg_fps"] == pytest.approx(1000.0 * 1201 / 10200.0)
    shown = [1 if i % 2 else 0 for i in range(2400)]                       # every other frame dropped
    half = make_cap(make_frames([4.0] * 2400, gpu_ms=0.5, display_ms=[8.0 if s else None for s in shown],
                                displayed=shown), hz=144.003)
    assert fa.bound(half, fa.main_chain(half), [])["steady_fps"] == pytest.approx(125.0)       # not the 250 presented
    plain = make_cap(make_frames([10.0] * 1200, gpu_ms=0.5))                 # no display tracking: the presents
    assert fa.bound(plain, fa.main_chain(plain), [])["steady_fps"] == pytest.approx(100.0)


def test_a_regular_cadence_with_a_saturated_thread_is_cpu_bound_not_capped():
    assert bound_of(26.0, 0.5, thread=99.0, hz=144.003)["verdict"] == "cpu_main"
    assert bound_of(26.0, 0.5, thread=84.0, hz=144.003)["verdict"] == "capped"


def test_vsync_needs_90_percent_on_refresh_multiples_and_the_app_locked_to_one():
    p = 1000.0 / 60.0
    n = 1200
    on = [p] * n

    def disp(share_off):
        k = round(share_off * n)
        return [p + 3.0 if i < k else p for i in range(n)]          # 3 ms off the grid

    assert bound_of(on, 5.0, thread=30.0, hz=60.0, display_ms=disp(0.10), displayed=1)["verdict"] == "vsync"
    b = bound_of(on, 5.0, thread=30.0, hz=60.0, display_ms=disp(0.11), displayed=1)
    assert b["verdict"] != "vsync" and b["quantised_pct"] == pytest.approx(89.0)
    # displayed frames on the grid, but the game presents freely at 18 ms: composition, not VSync
    b = bound_of(18.0, 5.0, thread=30.0, hz=60.0, display_ms=[p] * n, displayed=1)
    assert b["verdict"] != "vsync" and b["vsync"] is False and b["vrr"] is False


def test_vsync_band_is_the_larger_of_025_ms_and_4_percent_of_a_period():
    p = 1000.0 / 60.0                                               # 4 % = 0.667 ms > 0.25 ms
    assert bound_of([p] * 1200, 5.0, thread=30.0, hz=60.0, display_ms=p + 0.66, displayed=1)["vsync"] is True
    assert bound_of([p] * 1200, 5.0, thread=30.0, hz=60.0, display_ms=p + 0.68, displayed=1)["vsync"] is False
    p = 1000.0 / 240.0                                              # 4 % = 0.167 ms < 0.25 ms
    assert bound_of([p] * 3000, 1.0, thread=30.0, hz=240.0, display_ms=p + 0.24, displayed=1)["vsync"] is True
    assert bound_of([p] * 3000, 1.0, thread=30.0, hz=240.0, display_ms=p + 0.26, displayed=1)["vsync"] is False


def test_bound_needs_300_steady_frames_and_10_s():
    assert bound_of(34.0, 33.0, n=299)["verdict"] == "unknown"     # 10.2 s but 299 frames
    assert bound_of(34.0, 33.0, n=300)["verdict"] == "gpu"
    assert bound_of(16.6, 16.0, n=602)["verdict"] == "unknown"     # 602 frames but 9.99 s
    assert bound_of(16.6, 16.0, n=603)["verdict"] == "gpu"         # 10.01 s


def test_event_frames_are_not_steady():
    # 1200 GPU-bound frames, but an FPS-drop event covers all but 299 of them
    frames = [20.0] * 1200
    t_end_of_frame_900 = 1.0 + 901 * 0.020
    ev = LagEvent(id=1, kind="fps_drop", t_start=0.0, t_end=t_end_of_frame_900, severity=30.0)
    assert bound_of(frames, 19.0, events=[ev])["verdict"] == "unknown"
    pacing = LagEvent(id=2, kind="pacing", t_start=0.0, t_end=1e9, severity=6.9)
    assert bound_of(frames, 19.0, events=[pacing])["verdict"] == "gpu"      # pacing frames are still steady


def test_generated_frames_are_left_out_of_the_bound():
    # every other frame generated by the driver, with no CPU or GPU timing of its own
    frames = [10.0] * 2400
    gpu = [9.5 if i % 2 == 0 else None for i in range(2400)]
    ftype = ["Application" if i % 2 == 0 else "AMD AFMF" for i in range(2400)]
    b = bound_of(frames, gpu, frame_type=ftype)
    assert b["verdict"] == "gpu" and b["steady_frames"] == 1200


def test_r_needs_gpu_busy_on_most_frames():
    # PresentMon prints 0 for an unknown GPU busy (PM 2.3), so a few timed frames are not a verdict
    jitter = [10.0 * (1.1 if i % 4 < 2 else 0.9) for i in range(1200)]       # irregular: not a frame cap
    half = [9.5 if i % 2 == 0 else None for i in range(1200)]
    third = [9.5 if i % 3 == 0 else None for i in range(1200)]
    assert bound_of(jitter, half, thread=50.0)["verdict"] == "gpu"            # half the frames: enough
    b = bound_of(jitter, third, thread=50.0)
    assert b["r"] is None and b["verdict"] == "unknown"


def test_thread_load_inside_events_does_not_count():
    # the game's thread is saturated only during three long FPS drops; outside them it runs at 80 %
    spans = [(2.0, 8.0), (12.0, 18.0), (22.0, 28.0)]
    jitter = [20.0 * (1.1 if i % 2 else 0.9) for i in range(1500)]
    drops = [LagEvent(id=i, kind="fps_drop", t_start=a, t_end=b, severity=30.0) for i, (a, b) in enumerate(spans)]
    b = bound_of(jitter, 10.0, thread=lambda t: 100.0 if any(a - 0.3 <= t <= b + 0.3 for a, b in spans) else 80.0,
                 events=drops)
    assert b["busiest_thread"] == pytest.approx(0.80) and b["verdict"] == "waiting"


def test_no_frames_falls_back_to_the_3d_engine():
    cap = make_cap({}, make_system(20.0, game_gpu_pct=95.0, game_top_thread_pct=30.0))
    b = fa.bound(cap, fa.main_chain(cap), [])
    assert b["verdict"] == "gpu" and b["confidence"] == "low"
    cap = make_cap({}, make_system(20.0, game_gpu_pct=94.9, game_top_thread_pct=30.0))
    assert fa.bound(cap, fa.main_chain(cap), [])["verdict"] == "unknown"


def busy_engine(seconds, busy, marks=()):
    """A capture without frame timing whose game kept its GPU 99 % busy in the 1 s windows in `busy` (indices) and
    60 % in the others (so the median says nothing), and LagEvents for lag-key presses at `marks`."""
    cap = make_cap({}, make_system(seconds, game_gpu_pct=lambda t: 99.0 if int(t) in busy else 60.0,
                                   game_top_thread_pct=30.0))
    evs = [LagEvent(id=i, kind="mark", t_start=m - 3.0, t_end=m + 0.5, severity=0.0) for i, m in enumerate(marks)]
    return fa.bound(cap, fa.main_chain(cap), evs)


@pytest.mark.parametrize("seconds, busy, holds", [
    (100.0, range(10), True),              # 10 of 100 windows: 10 % and 10 s
    (100.0, range(9), False),              # 9 %
    (50.0, range(6), False),               # 12 %, but only 6 s
])
def test_without_frame_timing_the_gpu_limit_is_also_read_per_second(seconds, busy, holds):
    # Item 1: the first real capture's game kept its GPU at a median 71 %, yet 98-99 % through its last three
    # minutes; only the whole-capture median was ever asked, so GPU heat and video memory could not be checked
    b = busy_engine(seconds, set(busy))
    p = b["gpu_probable"]
    assert b["verdict"] == "unknown" and p["holds"] is holds
    assert p["seconds"] == len(busy) and p["windows"] == int(seconds) and p["share"] == pytest.approx(len(busy) / seconds)


@pytest.mark.parametrize("busy, holds", [({40, 60}, True), ({40}, False)])
def test_without_frame_timing_the_marks_say_where_the_gpu_was_the_limit_too(busy, holds):
    # Item 1: the first real capture's GPU was 96-100 % busy at all three marks. Here 2 of 4 marks (half) hold
    b = busy_engine(100.0, busy, marks=(41.0, 61.0, 81.0, 91.0))
    p = b["gpu_probable"]
    assert p["share"] < fa.PROBABLE_GPU_SHARE and p["holds"] is holds
    assert p["marks"] == 4 and p["marks_busy"] == len(busy)


def test_the_per_second_reading_is_only_for_a_capture_without_frame_timing_and_an_open_verdict():
    cap = make_cap({}, make_system(20.0, game_gpu_pct=95.0))               # the median already says "gpu"
    assert fa.bound(cap, fa.main_chain(cap), [])["gpu_probable"] is None
    assert bound_of(20.0, 19.5)["gpu_probable"] is None                    # frames measured: the verdict decides


def test_the_1_ms_gap_never_calls_a_gpu_idle_a_quarter_of_the_frame_the_limit():
    # A14: at 500 FPS a GPU busy 1.0 of 2.0 ms (r = 0.5) idles half of every frame: the saturated thread limits it
    assert bound_of(2.0, 1.0, thread=95.0, n=6000)["verdict"] == "cpu_main"
    # the gap still settles the borderline zone: r just over 0.75 with FT - G <= 1 ms is GPU-bound ...
    assert bound_of(4.0, 3.004, thread=95.0, n=3000)["verdict"] == "gpu"          # r = 0.751, gap 0.996 ms
    # ... but at r = 0.75 itself the ratio says CPU-or-other, whatever the gap
    assert bound_of(4.0, 3.0, thread=95.0, n=3000)["verdict"] == "cpu_main"       # r = 0.750, gap 1.0 ms


def test_vsync_needs_the_game_to_ask_for_it():
    # A14 / LC 3.3: frames on whole periods with FT on 2 P is VSync only when SyncInterval >= 1 and tearing is off
    p = 1000.0 / 60.0

    def verdict(sync, tearing):
        fr = make_frames([2 * p] * 1200, gpu_ms=5.0, display_ms=2 * p, displayed=1)
        fr[F.SYNC_INTERVAL], fr[F.ALLOWS_TEARING] = [sync] * 1200, [tearing] * 1200
        cap = make_cap(fr, make_system(2.0 + 1200 * 2 * p / 1000.0, game_top_thread_pct=30.0), hz=60.0)
        return fa.bound(cap, fa.main_chain(cap), [])

    assert verdict(1, 0)["verdict"] == "vsync"
    assert verdict(-1, 0)["verdict"] == "vsync"           # unknown (OpenGL, Vulkan) rules nothing out
    assert verdict(None, None)["verdict"] == "vsync"      # nor do missing columns
    b = verdict(0, 0)
    assert b["verdict"] != "vsync" and b["vsync"] is False and "SyncInterval 0" in " ".join(b["notes"])
    assert verdict(1, 1)["verdict"] != "vsync"


def vsync_halved(gpu_ms=None, thread=40.0, cpu_busy=None, cpu_wait=0.0, runtime="Other", k=2, hz=60.0, n=900,
                 lead=None):
    """A game held by VSync at k refreshes a frame (FT = k P, every frame shown k periods); `lead`: the capture's
    lead-thread column, which a capture saved before it lacks."""
    p = 1000.0 / hz
    fr = make_frames([k * p] * n, gpu_ms=gpu_ms, display_ms=k * p, displayed=1, cpu_busy=cpu_busy, runtime=runtime)
    fr[F.CPU_WAIT_MS] = [cpu_wait] * n
    cols = {"game_top_thread_pct": thread} | ({"game_lead_thread_pct": lead} if lead is not None else {})
    cap = make_cap(fr, make_system(2.0 + n * k * p / 1000.0, **cols), hz=hz)
    return fa.bound(cap, fa.main_chain(cap), [])


def test_vsync_at_two_refreshes_names_the_gpu_that_needs_more_than_one():
    # A6: a GPU needing 17 ms on 60 Hz (P = 16.67 ms) cannot make every refresh, so VSync rounds 60 down to 30
    b = vsync_halved(gpu_ms=17.0)
    assert b["verdict"] == "gpu" and b["vsync"] is True
    assert any("rounds the frame rate down to 30 FPS" in x for x in b["notes"])
    assert vsync_halved(gpu_ms=16.5)["verdict"] == "vsync"        # fits in one period: the display holds it
    assert vsync_halved(gpu_ms=17.0, k=1)["verdict"] == "vsync"   # k = 1 is the refresh rate itself


def test_vsync_at_two_refreshes_names_the_cpu_that_needs_more_than_one():
    assert vsync_halved(gpu_ms=5.0, thread=90.0)["verdict"] == "cpu_main"      # the busiest thread saturated


def test_vsync_at_two_refreshes_names_a_thread_that_needs_more_than_one_period():
    # A thread VSync holds at 30 FPS waits for the refresh the rest of each frame, so it is never saturated: at 60 %
    # of a CPU it works 20.0 ms a 33.3 ms frame, more than one 16.7 ms period, and that is what holds the game at 30.
    # Once read from MsCPUBusy (which counts waits too); with that branch gone the game read "VSync".
    b = vsync_halved(gpu_ms=5.0, thread=60.0, lead=60.0)
    assert b["verdict"] == "cpu_main"
    assert any(x.endswith("of a CPU, 20.0 ms a frame, more than 1 refresh period(s) (16.7 ms)") for x in b["notes"])
    assert vsync_halved(gpu_ms=5.0, thread=51.0, lead=51.0)["verdict"] == "cpu_main"     # 17.0 ms
    assert vsync_halved(gpu_ms=5.0, thread=49.0, lead=49.0)["verdict"] == "vsync"        # 16.3 ms fits in one period
    assert vsync_halved(gpu_ms=5.0, thread=80.0, lead=80.0, k=1)["verdict"] == "vsync"   # k = 1: the refresh rate
    # a capture saved before the lead column: each tick's busiest thread is an upper bound (four threads at 40 %
    # read 53 %), so it counts only when saturated, as before
    assert vsync_halved(gpu_ms=5.0, thread=60.0)["verdict"] == "vsync"


def test_cpu_busy_never_calls_a_game_vsync_holds_cpu_bound():
    # PM 3.2: MsCPUBusy runs from the last Present() return to the next call, so a game waiting outside Present()
    # (a waitable swap chain, a limiter's sleep) reads busy for most of the frame, even on DXGI where MsCPUWait is
    # measured. 20 ms of it once called a game VSync held at 30 FPS, its threads at 40 %, CPU-bound.
    p2 = 2 * 1000.0 / 60.0
    for runtime in ("DXGI", "Other"):
        b = vsync_halved(gpu_ms=5.0, cpu_busy=20.0, cpu_wait=p2 - 20.0, runtime=runtime)
        assert b["verdict"] == "vsync" and b["vsync"] is True, runtime
        assert not any("CPU works" in x for x in b["notes"])


def test_hags_comes_from_the_games_own_adapter_first():
    # A12: settings.hags fell back to the NVIDIA GPU at start; the game rendered on the Intel GPU (HAGS off)
    cap = make_cap({}, hags=True)
    cap.inventory["adapters"] = [{"luid": "NV", "hags": True}, {"luid": "INTEL", "hags": False}]
    cap.target = {"pid": 1, "adapter_luid": "INTEL"}
    assert fa.hags_on(cap) is False
    cap.target = {"pid": 1}                                       # adapter unknown: the inventory's value
    assert fa.hags_on(cap) is True
    cap.target = {"pid": 1, "adapter_luid": "INTEL"}
    cap.inventory["adapters"][1]["hags"] = None                   # the adapter's own readout failed
    assert fa.hags_on(cap) is True


def test_times_and_milliseconds_past_1e7_are_missing():
    # A15: a corrupt 1e308 must not overflow a sum; a byte count past 1e7 is real and kept
    assert fa._bounded(1e7) == 1e7 and fa._bounded(-1e7) == -1e7
    assert fa._bounded(1.0000001e7) is None and fa._bounded(1e308) is None
    assert fa._num_col("gpu_busy_ms", 2e7) is None and fa._num_col("t", 2e7) is None
    assert fa._num_col("game_cpu_pct", 2e7) is None and fa._num_col("disk_read_bps", 2e9) == 2e9
    fr = make_frames([10.0] * 50, display_ms=10.0, displayed=1)
    fr[F.T][-1] = 1e308
    fr[F.DISPLAY_MS][10] = 1e308
    fr[F.FRAME_MS][20] = 1e308
    out = fa.main_chain(make_cap(fr))
    assert len(out[F.T]) == 49 and out[F.DISPLAY_MS][10] is None and out[F.FRAME_MS][20] is None
    s = fa.frame_stats(out, 1e308)
    assert s["frames"] == 48 and s["avg_fps"] == pytest.approx(100.0) and s["duration_s"] is None


def test_refresh_comes_from_the_games_display():
    inv = {"displays": [{"device": r"\\.\DISPLAY1", "primary": True, "hz": 144.003},
                        {"device": r"\\.\DISPLAY2", "primary": False, "hz": 59.94}]}
    cap = Capture(started_at="x", clock_origin=0.0, duration_s=1.0, target={"monitor_device": r"\\.\DISPLAY2"},
                  sources={}, inventory=inv)
    assert fa.refresh_hz(cap) == pytest.approx(59.94)
    cap.target = {}
    assert fa.refresh_hz(cap) == pytest.approx(144.003)
    cap.inventory = {"displays": [{"device": "a", "hz": 60.0}, {"device": "b", "hz": 75.0}]}
    assert fa.refresh_hz(cap) is None                      # two displays, none primary, no target: unknown


# ------------------------------------------------------------------------------------- headroom ---

def test_headroom_scales_the_steady_rate_by_the_busiest_threads_load():
    # GPU-bound at 16.9 ms frames, the GPU busy 16.1 ms, the busiest game thread at 75 % of a CPU. From 0.85 a thread
    # counts as saturated, at 1 it is: the CPU could feed 59.2 x [0.85 / 0.75, 1 / 0.75]
    b = bound_of(16.9, 16.1, thread=75.0, hz=144.003)
    h = b["headroom"]
    assert b["verdict"] == "gpu" and h["steady_fps"] == pytest.approx(1000.0 / 16.9)
    assert h["cpu_fps"] == pytest.approx([67.061, 78.895], abs=1e-3) and h["cpu_from"] == "thread"
    assert h["gpu_fps"] == pytest.approx(62.112, abs=1e-3)                    # 1000 / 16.1
    assert h["display_fps"] == pytest.approx(144.003) and h["steps_fps"] is None    # presents only: no display series
    # a GPU 67.06 / 62.11 to 78.90 / 59.17 times faster makes the CPU the limit
    assert (h["limit"], h["next"]) == ("gpu", "cpu") and h["speedup"] == pytest.approx([1.0797, 1.3333], abs=1e-4)


def test_headroom_never_reads_ms_cpu_busy():
    # PresentMon's CPU Busy is wall time from one Present() to the next: 16.55 ms of a 16.9 ms GPU-bound frame in a real
    # game (docs/learnings.md), so 1000 / CPU Busy restates the frame rate. The CPU's side comes from thread time alone
    wall = bound_of(16.9, 16.1, thread=75.0, hz=144.003, cpu_busy=16.55)["headroom"]
    short = bound_of(16.9, 16.1, thread=75.0, hz=144.003, cpu_busy=4.0)["headroom"]
    assert wall["cpu_fps"] == short["cpu_fps"] == pytest.approx([67.061, 78.895], abs=1e-3)


def test_headroom_takes_every_core_when_the_game_uses_them_more_than_one_thread():
    # LC 3.2's all-core figure: 1,280 % of 16 logical CPUs is 0.80 of all of them, busier than the 0.50 thread
    jitter = [20.0 * (1.1 if i % 2 else 0.9) for i in range(1200)]
    h = bound_of(jitter, 18.5, thread=50.0, cpu_pct=1280.0)["headroom"]
    assert h["cpu_from"] == "all_cores" and h["cpu_fps"] == pytest.approx([50.0 * 0.85 / 0.80, 50.0 / 0.80])
    h = bound_of(jitter, 18.5, thread=50.0, cpu_pct=640.0)["headroom"]      # 0.40 of them: the thread binds
    assert h["cpu_from"] == "thread" and h["cpu_fps"] == pytest.approx([50.0 * 0.85 / 0.50, 50.0 / 0.50])


def test_headroom_of_a_cpu_bound_game_starts_at_the_rate_it_reached():
    # The busiest thread at 95 %: the CPU feeds the 40 FPS it ran at, and at most 40 / 0.95. The GPU (10 ms) could draw
    # 100: a CPU 100 / 42.1 to 100 / 40 times faster makes the GPU the limit
    jitter = [25.0 * (1.1 if i % 2 else 0.9) for i in range(1200)]
    b = bound_of(jitter, 10.0, thread=95.0, hz=144.003)
    h = b["headroom"]
    assert b["verdict"] == "cpu_main" and h["cpu_fps"] == pytest.approx([40.0, 40.0 / 0.95])
    assert (h["limit"], h["next"]) == ("cpu", "gpu") and h["gpu_fps"] == pytest.approx(100.0)
    assert h["speedup"] == pytest.approx([100.0 * 0.95 / 40.0, 100.0 / 40.0])
    # its GPU busy 0.4 ms with HAGS on, inside the 0.5 ms margin (G = 0): no GPU ceiling, and the screen is next
    b = bound_of(jitter, 0.4, thread=95.0, hz=144.003, hags=True)
    h = b["headroom"]
    assert b["verdict"] == "cpu_main" and b["gpu_ms"] == 0.0 and h["gpu_fps"] is None and h["next"] == "display"
    assert h["speedup"] == pytest.approx([144.003 * 0.95 / 40.0, 144.003 / 40.0])
    b = bound_of(jitter, 0.4, thread=95.0, hags=True)                       # nor a refresh rate: nothing to say
    assert b["headroom"]["next"] is None


@pytest.mark.parametrize("thread, nxt, want", [
    (75.0, "cpu", (100 * 0.85 / 0.75, 100 / 0.75)),         # 113-133 FPS, under the 144 Hz screen
    (65.0, "cpu_or_display", (100 * 0.85 / 0.65, 144.003)),  # 131-154 FPS: either may come first
    (55.0, "display", (144.003, 144.003)),                   # 155-182 FPS: the screen comes first
])
def test_headroom_names_the_screen_when_the_cpu_could_outrun_it(thread, nxt, want):
    b = bound_of(10.0, 9.8, thread=thread, hz=144.003)
    h = b["headroom"]
    assert b["verdict"] == "gpu" and h["next"] == nxt
    gpu = 1000.0 / 9.8                                       # the limit's own range: from the 100 FPS it ran at to this
    assert h["speedup"] == pytest.approx([want[0] / gpu, want[1] / 100.0])


def test_headroom_never_asks_a_part_to_get_slower():
    # the thread at 99 %: the CPU could feed 100-101 FPS, under the GPU's own 102: the speedup bottoms out at 1
    h = bound_of(10.0, 9.8, thread=99.0, hz=144.003)["headroom"]
    assert h["next"] == "cpu" and h["speedup"][0] == 1.0 and h["speedup"][1] == pytest.approx(1 / 0.99)


def test_headroom_under_a_vsync_lock_measures_the_gpu_by_its_own_time():
    # 60 Hz, locked at 30 FPS because the GPU needs 17 ms: its own ceiling (58.8 FPS) is the base, never the 30 FPS the
    # lock rounded it down to. The CPU could feed 63.75-75 FPS, past 60: the screen is next, 60 / 58.8 away
    b = vsync_halved(gpu_ms=17.0)
    h = b["headroom"]
    assert b["verdict"] == "gpu" and h["steady_fps"] == pytest.approx(30.0) and h["next"] == "display"
    assert h["speedup"] == pytest.approx([17.0 * 60.0 / 1000.0] * 2)
    assert h["steps_fps"] == pytest.approx([60.0, 30.0, 20.0])          # 60 / k down to 20 FPS


def test_headroom_steps_need_ninety_percent_of_frames_on_whole_refreshes():
    p = 1000.0 / 60.0
    n = 1200

    def disp(share_off):
        k = round(share_off * n)
        return [p + 3.0 if i < k else p for i in range(n)]          # 3 ms off the grid

    on = bound_of([p] * n, 5.0, thread=30.0, hz=60.0, display_ms=disp(0.10), displayed=1)["headroom"]
    assert on["steps_fps"] == pytest.approx([60.0, 30.0, 20.0])
    off = bound_of([p] * n, 5.0, thread=30.0, hz=60.0, display_ms=disp(0.11), displayed=1)["headroom"]
    assert off["steps_fps"] is None and off["display_fps"] == pytest.approx(60.0)


@pytest.mark.parametrize("shared", [10.0, 9.9])
def test_headroom_leaves_the_gpu_out_while_other_programs_share_it(shared):
    # A7 (PM 3.2): the game's own GPU busy leaves other programs' work out, so it says too little about the GPU then
    h = bound_of(20.0, 19.5, thread=50.0, hz=144.003, gpu_other_pct=shared)["headroom"]
    assert (h["gpu_fps"] is None) == (shared >= fa.SHARED_GPU_PCT)
    assert h["cpu_fps"] is not None and h["next"] == "cpu"         # the GPU's verdict still names the CPU next


def test_headroom_needs_the_steady_stretch_a_verdict_needs():
    assert bound_of(34.0, 33.0, n=299)["headroom"] is None         # 10.2 s but 299 frames
    b = bound_of(34.0, 33.0, n=300)
    assert b["headroom"] is not None
    cap = make_cap({}, make_system(20.0, game_gpu_pct=99.0, game_top_thread_pct=30.0))
    assert fa.bound(cap, fa.main_chain(cap), [])["headroom"] is None  # the no-frames "gpu" verdict has no steady rate
    # headroom() itself, as any other caller would use it: the same two floors
    assert fa.headroom(dict(b, steady_frames=fa.BOUND_MIN_FRAMES - 1)) is None
    assert fa.headroom(dict(b, steady_s=fa.BOUND_MIN_S - 0.01)) is None
    assert fa.headroom(dict(b, steady_frames=fa.BOUND_MIN_FRAMES, steady_s=fa.BOUND_MIN_S)) is not None


def test_headroom_says_nothing_of_a_next_limit_it_cannot_see():
    # the GPU limits, but the game's threads were not measured: the CPU's ceiling, and so the next limit, is unknown
    h = bound_of(20.0, 19.5, thread=None, hz=144.003)["headroom"]
    assert h["limit"] == "gpu" and h["cpu_fps"] is None and h["next"] is None and h["speedup"] is None
    # a capped game has ceilings but no limiting part to speed up
    h = bound_of(_wobble(1500, 8.333, 0.0290 / 1.4826), 0.5, thread=30.0, hz=144.003)["headroom"]
    assert h["limit"] is None and h["gpu_fps"] == pytest.approx(2000.0) and h["speedup"] is None
    # VSync at 30 on 60 Hz names the CPU from its busiest thread's own work a frame (60 % of a CPU, 20.0 ms, more than
    # one 16.7 ms period), though the thread is not saturated: the saturation edge cannot say how much faster it must
    # get, so it says nothing (this case once came from MsCPUBusy, which no longer names the CPU)
    b = vsync_halved(gpu_ms=5.0, thread=60.0, lead=60.0)
    assert b["verdict"] == "cpu_main" and b["headroom"]["limit"] == "cpu"
    assert b["headroom"]["next"] is None and b["headroom"]["speedup"] is None


# ------------------------------------------------------------------------------ live, read-only ---

def test_live_laglab_frame_cap_reads_as_a_cap_despite_stray_late_frames():
    """LagLab's own 120 FPS cap loop on this machine (headless): its cadence must pass the cap test.

    The independent readout is the cap we asked for. A single OS-delayed wake-up (19 ms, measured here)
    pushes a plain stdev/mean CoV past 3 %, so this also guards the robust estimate.
    """
    from laglab.app import _burn, _wait_until

    starts = []
    end = time.perf_counter() + 5.6
    while (fs := time.perf_counter()) < end:
        starts.append(fs)
        _burn(0.0005)
        _wait_until(fs + 1 / 120)
    ms = [(b - a) * 1000.0 for a, b in zip(starts, starts[1:])]
    fr = {F.T: [s - starts[0] for s in starts[1:]], F.FRAME_MS: ms}
    cov, c = fa._cap_cadence(fr, list(range(len(ms))))
    assert c == pytest.approx(1000 / 120, abs=0.05)
    assert cov < 0.03, f"robust CoV {cov:.4f} of LagLab's real cap"          # LC 3.3's 3 %
    late = [x for x in ms if x > 1000 / 120 + 1.0]
    print(f"live cap: {len(ms)} frames, median {c:.4f} ms, robust CoV {100 * cov:.3f}%, {len(late)} late > 1 ms")


def test_robust_cov_ignores_one_stray_frame_that_a_plain_cov_cannot():
    xs = [8.3333] * 599 + [19.16]                          # what the live loop produced once in 1,200 frames
    plain = (sum((x - sum(xs) / len(xs)) ** 2 for x in xs) / len(xs)) ** 0.5 / (sum(xs) / len(xs))
    assert plain > 0.03                                    # LC 3.3's 3 %
    assert fa._robust_cov(xs) < 0.03
    assert math.isinf(fa._robust_cov([0.0, 0.0]))


def test_robust_cov_sees_a_two_value_cadence_even_with_a_one_frame_majority():
    xs = [4.0, 20.0] * 250 + [4.0]                         # LagLab pace mode in an odd-sized window: MAD = 0
    assert fa._robust_cov(xs) > 1.0
    assert fa.MAD_TO_SD * fa.st.mad(xs) / fa.st.median(xs) == 0.0     # what a MAD-based estimate would say
    assert fa._robust_cov([9.0] * 251 + [11.0] * 249) == pytest.approx(2.0 / 1.349 / 9.0)
