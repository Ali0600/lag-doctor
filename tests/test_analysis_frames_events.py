"""analysis/events.py: every event kind, its thresholds pinned at literal points either side."""
from __future__ import annotations

import math

import pytest

from lagdoctor.analysis import events as ev
from lagdoctor.analysis import frames as fa
from lagdoctor.model import F, N, SeriesBuilder

from tests.synth import capture as make_cap, frames_table as make_frames


def run(cap):
    fr = fa.main_chain(cap)
    return ev.find_events(cap, fr, fa.frame_stats(fr, cap.duration_s))


def kinds(events, kind):
    return [e for e in events if e.kind == kind]


def frames_cap(frame_ms, **kw):
    return make_cap(make_frames(frame_ms, **kw))


# -------------------------------------------------------------------------------------- stutters ---

def test_stutter_needs_25x_the_baseline():
    assert len(kinds(run(frames_cap([10.0] * 50 + [25.0] + [10.0] * 50)), "stutter")) == 1
    # 24.95 sits strictly between 2.49 M and 2.5 M (2.49 * 10 is 24.900000000000002 in floating point)
    assert kinds(run(frames_cap([10.0] * 50 + [24.95] + [10.0] * 50)), "stutter") == []


def test_stutter_needs_8_ms_above_the_baseline():
    assert len(kinds(run(frames_cap([4.0] * 50 + [12.0] + [4.0] * 50)), "stutter")) == 1   # 3x, +8 ms
    assert kinds(run(frames_cap([4.0] * 50 + [11.99] + [4.0] * 50)), "stutter") == []      # 3x, +7.99 ms


def test_stutter_detail_class_and_span():
    events = kinds(run(frames_cap([10.0] * 50 + [160.0] + [10.0] * 50)), "stutter")
    assert len(events) == 1
    e = events[0]
    t_worst = 1.0 + (50 * 10.0 + 160.0) / 1000.0
    assert e.detail["worst_t"] == pytest.approx(t_worst)            # the HTML report places the dot here
    assert e.severity == e.detail["worst_ms"] == pytest.approx(160.0)
    assert e.detail["baseline_ms"] == pytest.approx(10.0) and e.detail["class"] == "severe"
    assert e.detail["frames"] == 1 and e.detail["stall"] is False
    assert (e.t_start, e.t_end) == (pytest.approx(t_worst - 0.160), pytest.approx(t_worst))


@pytest.mark.parametrize("ms, cls", [(49.9, "stutter"), (50.0, "hitch"), (149.9, "hitch"), (150.0, "severe"),
                                     (499.9, "severe"), (500.0, "freeze")])
def test_stutter_class_boundaries(ms, cls):
    assert ev.stutter_class(ms, 10.0) == cls


def test_a_hitch_also_needs_twice_the_baseline():
    assert ev.stutter_class(50.0, 25.0) == "hitch"
    assert ev.stutter_class(50.0, 25.1) == "stutter"


def test_stutters_closer_than_250_ms_merge():
    def two(gap_s):
        # frame A (40 ms), n quick frames, then frame B (40 ms): B's span starts n * 10 ms after A ends
        return frames_cap([10.0] * 60 + [40.0] + [10.0] * round(gap_s / 0.010) + [40.0] + [10.0] * 60)
    assert len(kinds(run(two(0.24)), "stutter")) == 1
    assert len(kinds(run(two(0.26)), "stutter")) == 2


def test_a_jump_in_time_with_no_frame_to_cover_it_is_a_stall():
    fr = make_frames([10.0] * 100)
    fr[F.T] = [t + (0.3 if i >= 50 else 0.0) for i, t in enumerate(fr[F.T])]   # 300 ms of missing rows
    stalls = kinds(run(make_cap(fr)), "stutter")
    assert len(stalls) == 1 and stalls[0].detail["stall"] is True
    assert stalls[0].severity == pytest.approx(310.0) and stalls[0].detail["frames"] == 0
    # the same gap held by one long frame of its own is a plain stutter
    long_frame = kinds(run(frames_cap([10.0] * 50 + [310.0] + [10.0] * 49)), "stutter")
    assert len(long_frame) == 1 and long_frame[0].detail["stall"] is False


def test_stall_needs_the_row_to_cover_less_than_half_of_the_gap():
    fr = make_frames([10.0] * 100)
    fr[F.T] = [t + (0.29 if i >= 50 else 0.0) for i, t in enumerate(fr[F.T])]
    fr[F.FRAME_MS][50] = 151.0                         # covers 151 of the 300 ms gap: its own long frame
    assert all(not e.detail["stall"] for e in kinds(run(make_cap(fr)), "stutter"))
    fr[F.FRAME_MS][50] = 149.0                         # less than half: rows are missing
    assert any(e.detail["stall"] for e in kinds(run(make_cap(fr)), "stutter"))


def test_stutters_use_displayed_frames_when_display_is_tracked():
    # presents are steady, but the screen held one frame for 50 ms: that is what the player saw
    disp = [10.0] * 50 + [50.0] + [10.0] * 50
    shown = kinds(run(frames_cap([10.0] * 101, display_ms=disp, displayed=1)), "stutter")
    assert len(shown) == 1 and shown[0].detail["source"] == "display"
    assert shown[0].t_end - shown[0].t_start == pytest.approx(0.050)   # covers the frozen screen


P144 = 1000.0 / 144.003


def held(present_ms, holds, hz=144.003):
    """A quantised display: every frame presented every present_ms and shown for holds[i] refresh periods."""
    p = 1000.0 / hz
    return make_cap(make_frames([present_ms] * len(holds), display_ms=[h * p for h in holds], displayed=1), hz=hz)


def cadence_120_on_144(slip_at=None, refreshes=3):
    """120 FPS on the 144 Hz panel: holds 1,1,1,1,2 (M = one refresh, the regular longest hold two)."""
    holds = [2 if i % 5 == 4 else 1 for i in range(600)]
    if slip_at is not None:
        holds[slip_at] = refreshes
    return held(1000.0 / 120.0, holds)


def test_a_frame_one_refresh_late_on_a_quantised_display_is_no_stutter():
    # A2: held 3 refreshes (20.8 ms) where the 120 FPS cadence holds 2 (13.9 ms): +6.9 ms, under LC 2.4's 8 ms,
    # though D >= 2.5 M and D - M >= 8 ms against M = one refresh
    assert kinds(run(cadence_120_on_144()), "stutter") == []
    assert kinds(run(cadence_120_on_144(slip_at=304)), "stutter") == []
    late = kinds(run(cadence_120_on_144(slip_at=304, refreshes=4)), "stutter")     # 27.8 ms: +13.9 ms
    assert len(late) == 1 and late[0].severity == pytest.approx(4 * P144)


def test_the_cadence_rule_holds_k_refreshes_only_within_half_a_percent_of_k_periods():
    # Changed on purpose (item 8, A2): a 5 % slack read every cap from 137 to 143 FPS on 144 Hz as a one-refresh
    # cadence, so their single slips were "stutters". Presents at 1.0049 P hold one refresh (measurement jitter), so
    # a 3-refresh hold is 2 late: a stutter; at 1.0051 P the cadence holds two now and then
    holds = [1] * 300 + [3] + [1] * 300
    assert len(kinds(run(held(1.0049 * P144, holds)), "stutter")) == 1
    assert kinds(run(held(1.0051 * P144, holds)), "stutter") == []
    assert kinds(run(held(1000.0 / 141.0, holds)), "stutter") == []      # the 141 FPS cap advised for 144 Hz


def test_the_cadence_rule_follows_the_games_frame_rate_where_it_is():
    # 400 frames presented at 1.004 P (one refresh each) with a 3-refresh hold, then 600 at 120 FPS: E follows the
    # trailing presents, so the early hold is a stutter though the run's overall cadence would hold two
    holds = [1] * 200 + [3] + [1] * 199 + [2 if i % 5 == 4 else 1 for i in range(600)]
    pres = [1.004 * P144] * 400 + [1000.0 / 120.0] * 600
    cap = make_cap(make_frames(pres, display_ms=[h * P144 for h in holds], displayed=1), hz=144.003)
    late = kinds(run(cap), "stutter")
    assert len(late) == 1 and late[0].severity == pytest.approx(3 * P144)


def test_the_cadence_rule_keeps_every_slip_at_60_hz():
    # 60 FPS VSync-locked on 60 Hz holds one refresh: a frame held 3 (50 ms) is 33 ms late
    late = kinds(run(held(1000.0 / 60.0, [1] * 300 + [3] + [1] * 300, hz=60.0)), "stutter")
    assert len(late) == 1 and late[0].detail["class"] == "hitch"


def test_the_cadence_rule_needs_a_quantised_display():
    # VRR or tearing: displayed intervals follow the presents (7.5 ms, not on 6.94 ms periods), so a 19 ms frame
    # is judged against M alone (19 >= 2.5 x 7.5 and +11.5 ms), not against the cadence's 2-refresh hold
    d = [7.5] * 300 + [19.0] + [7.5] * 300
    cap = make_cap(make_frames([7.5] * 601, display_ms=d, displayed=1), hz=144.003)
    assert len(kinds(run(cap), "stutter")) == 1
    cap = make_cap(make_frames([7.5] * 601, display_ms=d, displayed=1))          # refresh unknown: the same
    assert len(kinds(run(cap), "stutter")) == 1


# ------------------------------------------------------------------------------------- FPS drops ---

def drop_cap(fps_low, seconds_low, *, base_ms=10.0, total_s=30.0, irregular=False):
    """base_ms frames, then seconds_low at fps_low, then base again."""
    out, t = [], 0.0
    i = 0
    while t < total_s:
        ms = 1000.0 / fps_low if 10.0 <= t < 10.0 + seconds_low else base_ms
        if irregular:
            ms *= (0.8, 1.0, 1.2)[i % 3]
        out.append(ms)
        t += ms / 1000.0
        i += 1
    return frames_cap(out, displayed=1, display_ms=out)


def test_fps_drop_below_80_percent_of_the_cap():
    # cap = 100 FPS (regular cadence): the limit is 80 FPS; a 1 s window then counts 78-79 or 81-82 frames
    low = kinds(run(drop_cap(78.5, 6.0)), "fps_drop")
    assert len(low) == 1 and low[0].detail["target_from"] == "cap"
    assert low[0].detail["target_fps"] == pytest.approx(100.0, abs=0.01)
    assert low[0].severity == pytest.approx(21.5, abs=1.0)
    assert kinds(run(drop_cap(81.0, 6.0)), "fps_drop") == []


def test_fps_drop_must_last_2_s_of_rolling_fps():
    # at 50 % of target the 1 s rolling FPS is under 80 % from 0.1 s before the drop to 0.1 s after it
    assert kinds(run(drop_cap(50.0, 1.6)), "fps_drop") == []        # about 1.8 s below
    assert len(kinds(run(drop_cap(50.0, 2.0)), "fps_drop")) == 1    # about 2.2 s below


def test_fps_drop_target_without_a_cap_is_the_p90_fps():
    low = kinds(run(drop_cap(40.0, 5.0, irregular=True)), "fps_drop")
    assert len(low) == 1 and low[0].detail["target_from"] == "p90"


def test_fps_target_rules():
    assert ev.fps_target({"verdict": "capped", "cap_fps": 141.0}, [100.0]) == (141.0, "cap")
    assert ev.fps_target({"verdict": "vsync", "refresh_hz": 144.0, "ft_ms": 6.94}, []) == (144.0, "refresh")
    hz, src = ev.fps_target({"verdict": "vsync", "refresh_hz": 144.0, "ft_ms": 13.9}, [])
    assert (hz, src) == (72.0, "refresh")                      # locked at 2 refreshes per frame
    fps = [float(x) for x in range(1, 101)]
    assert ev.fps_target({"verdict": "gpu"}, fps) == (pytest.approx(90.1), "p90")
    assert ev.fps_target({"verdict": "gpu"}, []) == (None, None)


def test_the_fps_target_never_tops_a_fixed_refresh():
    # A11: an 86 FPS cap on a 60 Hz panel shows 60 frames a second; that is the most the screen can show
    assert ev.fps_target({"verdict": "capped", "cap_fps": 86.0, "refresh_hz": 60.0}, []) == (60.0, "refresh")
    assert ev.fps_target({"verdict": "gpu", "refresh_hz": 60.0}, [float(x) for x in range(1, 101)]) == \
        (60.0, "refresh")
    assert ev.fps_target({"verdict": "capped", "cap_fps": 59.0, "refresh_hz": 60.0}, []) == (59.0, "cap")
    # with VRR seen, the refresh is only the ceiling of a range the display follows: keep the cap
    assert ev.fps_target({"verdict": "capped", "cap_fps": 86.0, "refresh_hz": 60.0, "vrr": True}, []) == \
        (86.0, "cap")


def test_rolling_fps_stays_bounded_on_a_junk_time_span():
    # two frames two days apart (a corrupt capture): the grid coarsens instead of growing with the span.
    # The bound is a literal (a day of 0.1 s steps), never read back from the module under test.
    grid, fps = ev.rolling_fps([0.0, 2 * 86400.0])
    assert 2 <= len(grid) <= 864_000
    assert all(v <= 1.0 for v in fps)


def test_rolling_fps_is_centred_and_stays_inside_the_data():
    times = [i * 0.01 for i in range(301)]                     # 100 FPS for 3 s
    grid, fps = ev.rolling_fps(times)
    assert grid[0] == pytest.approx(0.5) and grid[-1] == pytest.approx(2.5) and len(grid) == 21
    assert all(99.0 <= v <= 101.0 for v in fps)                # a frame on the window's edge may go either way


# --------------------------------------------------------------------------------------- pacing ---

def pacing_cap(bumps, *, bump_ms=14.0, anim=None, frame_type=None):
    """600 frames of 10 ms (three 2 s windows); `bumps` non-adjacent bumps in the middle window."""
    d = [10.0] * 600
    for j in range(bumps):
        d[210 + 8 * j] = bump_ms
    fr = make_frames([10.0] * 600, display_ms=d, displayed=1, frame_type=frame_type)
    fr[F.T] = [i * 0.01 for i in range(600)]
    if anim is not None:
        fr[F.ANIM_ERROR_MS] = anim
    return make_cap(fr)


def test_pacing_needs_more_than_10_percent_of_swings_at_4_ms():
    # the middle window has 199 intervals; each bump makes two 4 ms swings
    assert len(kinds(run(pacing_cap(10)), "pacing")) == 1         # 20 / 199 = 10.05 %
    assert kinds(run(pacing_cap(9)), "pacing") == []              # 18 / 199 = 9.05 %
    assert kinds(run(pacing_cap(10, bump_ms=13.99)), "pacing") == []   # swings under 4 ms do not count


def test_pacing_by_animation_error():
    assert len(kinds(run(pacing_cap(0, anim=[2.01] * 600)), "pacing")) == 1
    assert kinds(run(pacing_cap(0, anim=[2.0] * 600)), "pacing") == []
    assert len(kinds(run(pacing_cap(0, anim=[-2.01] * 600)), "pacing")) == 1    # |error|


def test_pacing_detail_tells_uneven_presents_from_display_judder():
    e = kinds(run(pacing_cap(20)), "pacing")[0]
    assert e.detail["swing_pct"] == pytest.approx(100 * 40 / 199) and e.detail["present_swing_pct"] == 0.0
    assert e.severity == pytest.approx(4.0)                      # the median of the swings that counted


def test_pacing_ignores_generated_frames():
    ftype = ["Application" if i % 2 else "Intel XeSS-FG" for i in range(600)]
    # all bumps sit on generated frames (even indexes): app frames are perfectly even
    assert kinds(run(pacing_cap(10, frame_type=ftype)), "pacing") == []


def hitchy(every=12, base=25.0, hitch=70.0, n=1200):
    """A steady 40 FPS game with a 70 ms hitch every 12th frame: separate stutters (275 ms apart), whose swings
    into and out of them are 15 % of all swings."""
    d = [hitch if i % every == every - 1 else base for i in range(n)]
    return frames_cap(d, display_ms=d, displayed=1)


def test_the_swings_around_a_stutter_are_not_pacing():
    # A1: the hitches are stutters; their own swings must not also make (and then outrank them as) pacing
    evs = run(hitchy())
    assert len(kinds(evs, "stutter")) == 100
    assert kinds(evs, "pacing") == []


def test_real_pacing_between_stutters_is_still_found():
    # presents alternate 20 / 30 ms (uneven by themselves), with 100 ms hitches: pacing stays, criterion swing
    d = [100.0 if i % 12 == 11 else (20.0 if i % 2 else 30.0) for i in range(1200)]
    evs = run(frames_cap(d, display_ms=d, displayed=1))
    pacing = kinds(evs, "pacing")
    assert kinds(evs, "stutter") and pacing and all(e.detail["criterion"] == "swing" for e in pacing)
    assert all(e.severity == pytest.approx(10.0) for e in pacing)


def test_pacing_says_which_test_fired_and_animation_error_sets_its_severity():
    # A11: an even display (swings 0 ms) judged jerky by animation error alone reports the error, not 0 ms
    anim = kinds(run(pacing_cap(0, anim=[2.5] * 600)), "pacing")
    assert [e.detail["criterion"] for e in anim] == ["anim"] and anim[0].severity == pytest.approx(2.5)
    swing = kinds(run(pacing_cap(20)), "pacing")
    assert [e.detail["criterion"] for e in swing] == ["swing"] and swing[0].severity == pytest.approx(4.0)
    both = kinds(run(pacing_cap(20, anim=[2.5] * 600)), "pacing")
    assert both[0].detail["criterion"] == "both" and both[0].severity == pytest.approx(4.0)


def test_no_event_starts_before_the_chains_first_row():
    # pacing_cap's first frame is at t = 0 and was shown for 10 ms: the episode may not begin at -0.01 s
    e = kinds(run(pacing_cap(0, anim=[2.5] * 600)), "pacing")[0]
    assert e.t_start == pytest.approx(0.0)
    # the second frame held the screen 400 ms but was presented 300 ms after the first row: its span would
    # reach back 90 ms before anything was measured
    fr = make_frames([10.0, 300.0] + [10.0] * 40, display_ms=[10.0, 400.0] + [10.0] * 40, displayed=1, t0=0.0)
    stutters = kinds(run(make_cap(fr)), "stutter")
    assert len(stutters) == 1 and stutters[0].t_start == pytest.approx(fr[F.T][0])


# -------------------------------------------------------------------------------------- network ---

def net_cap(inet, *, gw=2.0, server=None, net_flows=None, duration=300.0, step=0.5):
    b = SeriesBuilder(N.ALL)
    for i, v in enumerate(inet):
        b.add(t=(i + 1) * step, inet_rtt_ms=v, gw_rtt_ms=gw, server_rtt_ms=server[i] if server else None)
    return make_cap(duration=duration, network=b.build(), net_flows=net_flows or {})


# Every network test below probes at the product's real rate: one round every 0.5 s (DESIGN, network).

def test_ping_spike_needs_30_ms_over_the_rolling_minimum():
    assert len(kinds(run(net_cap([20.0] * 100 + [50.0] * 2 + [20.0] * 100)), "net_spike")) == 1
    assert kinds(run(net_cap([20.0] * 100 + [49.9] * 2 + [20.0] * 100)), "net_spike") == []


def test_ping_spike_needs_twice_the_median():
    base = [10.0] * 5 + [40.0] * 200                         # minimum 10 ms, median 40 ms
    assert len(kinds(run(net_cap(base + [80.0] * 2 + [40.0] * 5)), "net_spike")) == 1
    assert kinds(run(net_cap(base + [79.9] * 2 + [40.0] * 5)), "net_spike") == []


def test_ping_spike_minimum_looks_back_120_s_only():
    # 49 ms is over 2x the 20 ms median either way; only a 5 ms low inside the last 120 s makes it +30
    inet = [5.0] + [20.0] * 242 + [49.0] * 2 + [20.0] * 10    # the low is 121.5 s back: +29 over 20 ms
    assert kinds(run(net_cap(inet)), "net_spike") == []
    inet = [5.0] + [20.0] * 237 + [49.0] * 2 + [20.0] * 10    # the low is 119.0 and 119.5 s back: +44 ms
    assert len(kinds(run(net_cap(inet)), "net_spike")) == 1


def test_one_slow_reply_is_no_ping_spike():
    # A8: one probe 36 ms slow (one 0.5 s round) moves no 15 s figure (NE 2.2) and ICMP is often deprioritised
    assert kinds(run(net_cap([20.0] * 100 + [56.0] + [20.0] * 100)), "net_spike") == []


def test_a_ping_spike_lasts_2_of_3_rounds():
    def spikes(mid):
        return kinds(run(net_cap([20.0] * 100 + mid + [20.0] * 100)), "net_spike")
    assert len(spikes([60.0, 60.0])) == 1                    # two rounds in a row
    assert len(spikes([60.0, 20.0, 60.0])) == 1              # two of three, a normal round between
    assert spikes([60.0, 20.0, 20.0, 60.0]) == []            # two of four: each stands alone
    assert spikes([60.0, None, None, 60.0]) == []            # lost rounds still count as rounds


def test_one_reply_100_ms_over_the_minimum_is_a_spike_alone():
    # NE 2.2: a rise of 100 ms is "bad" even for one reply (Claypool: 100 ms for first-person games)
    assert len(kinds(run(net_cap([20.0] * 100 + [120.0] + [20.0] * 100)), "net_spike")) == 1
    assert kinds(run(net_cap([20.0] * 100 + [119.9] + [20.0] * 100)), "net_spike") == []


def test_ping_spike_detail_and_merge():
    inet = [20.0] * 50 + [90.0, 95.0, 20.0, 99.0] + [20.0] * 50
    spikes = kinds(run(net_cap(inet)), "net_spike")
    assert len(spikes) == 1                                  # samples within 1 s are one episode
    d = spikes[0].detail
    assert spikes[0].severity == d["worst_ms"] == 99.0 and d["baseline_ms"] == 20.0 and d["target"] == "internet"
    assert d["samples"] == 3 and d["gw_ms"] == 2.0


def test_server_rtt_spikes_are_their_own_events():
    server = [30.0] * 50 + [90.0] * 2 + [30.0] * 50
    spikes = kinds(run(net_cap([20.0] * 102, server=server)), "net_spike")
    assert [e.detail["target"] for e in spikes] == ["server"]


def lost_at(*times, sent=100):
    return {"probes": {"internet": {"sent": sent, "lost": len(times), "from_t": 0.5}},
            "lost": {"internet": list(times)}}


def test_two_lost_rounds_in_a_row_are_a_loss_burst():
    inet = [20.0] * 100
    inet[49] = inet[50] = None                               # the rounds at 25.0 and 25.5 s
    loss = kinds(run(net_cap(inet, net_flows=lost_at(25.0, 25.5))), "net_loss")
    assert len(loss) == 1
    d = loss[0].detail
    assert d["target"] == "internet" and d["burst"] is True and d["lost"] == 2
    assert loss[0].severity == pytest.approx(100.0 * 2 / 30)  # 2 of the 30 probes in the 15 s around them
    assert (loss[0].t_start, loss[0].t_end) == (pytest.approx(24.75), pytest.approx(25.75))


def test_one_lost_probe_is_no_loss_event():
    # A8: at 2 Hz one loss is already 3 % of a 15 s window; one unanswered ICMP probe is no outage (NE 2.4)
    inet = [20.0] * 100
    inet[49] = None
    assert kinds(run(net_cap(inet, net_flows=lost_at(25.0))), "net_loss") == []


def test_two_lost_probes_apart_are_loss_but_no_burst():
    inet = [20.0] * 100
    inet[39] = inet[49] = None                               # 20.0 and 25.0 s: one 15 s window, answers between
    loss = kinds(run(net_cap(inet, net_flows=lost_at(20.0, 25.0))), "net_loss")
    assert len(loss) == 2 and not any(e.detail["burst"] for e in loss)
    assert all(e.detail["lost_15s"] == 2 for e in loss)
    inet[39] = 20.0                                          # 9 s apart: never two in one 15 s window
    inet[67] = None
    assert kinds(run(net_cap(inet, net_flows=lost_at(25.0, 34.0))), "net_loss") == []
    inet = [None if i in (49, 51) else 20.0 for i in range(100)]    # 25.0 and 26.0 s, 25.5 answered between
    loss = kinds(run(net_cap(inet, net_flows=lost_at(25.0, 26.0))), "net_loss")
    assert len(loss) == 1 and loss[0].detail["lost"] == 2 and loss[0].detail["burst"] is False


def test_not_probed_yet_is_not_lost():
    inet = [None] * 20 + [20.0] * 80                         # probing began at 10.5 s
    nf = {"probes": {"internet": {"sent": 80, "lost": 0, "from_t": 10.5}}}
    assert kinds(run(net_cap(inet, net_flows=nf)), "net_loss") == []
    inet[50] = inet[51] = None
    loss = kinds(run(net_cap(inet, net_flows=nf)), "net_loss")
    assert len(loss) == 1 and loss[0].t_start == pytest.approx(25.25) and loss[0].detail["burst"]
    assert kinds(run(net_cap(inet, net_flows={})), "net_loss") == []    # no from_t, no list: cannot tell


def test_lost_probes_can_be_keyed_by_ip():
    nf = {"targets": {"internet": "1.1.1.1"}, "probes": {"1.1.1.1": {"sent": 100, "lost": 2, "from_t": 0.5}},
          "lost": {"1.1.1.1": [25.0, 25.5]}}
    assert len(kinds(run(net_cap([20.0] * 100, net_flows=nf)), "net_loss")) == 1


def test_a_server_or_router_that_never_answered_a_ping_has_no_loss_events():
    # NE 2.4, NE 7 row 9: game servers and routers often never answer ICMP. One that answered none of its probes was
    # not measured by ping: its loss is unknown, never 100 %
    t = [0.5 * (i + 1) for i in range(200)]

    def server(rtt):
        lost = [x for x, v in zip(t[20:], rtt[20:]) if v is None]
        return {"targets": {"server": "203.0.113.7"}, "lost": {"server": lost},
                "probes": {"server": {"sent": 180, "lost": len(lost), "from_t": 10.5}}}

    never = [None] * 200
    assert kinds(run(net_cap([20.0] * 200, server=never, net_flows=server(never))), "net_loss") == []
    stopped = [None] * 20 + [30.0 if x < 60.0 else None for x in t[20:]]       # answered until 60 s, then silent
    loss = kinds(run(net_cap([20.0] * 200, server=stopped, net_flows=server(stopped))), "net_loss")
    assert [(e.detail["target"], e.detail["lost"], e.severity, e.detail["burst"]) for e in loss] == [
        ("server", 81, 100.0, True)]
    # a router that never answered is no loss either; an internet anchor that never answered is an outage
    nf = {"probes": {r: {"sent": 200, "lost": 200, "from_t": 0.5} for r in ("gateway", "internet")},
          "lost": {r: list(t) for r in ("gateway", "internet")}}
    loss = kinds(run(net_cap([None] * 200, gw=None, net_flows=nf)), "net_loss")
    assert sorted({e.detail["target"] for e in loss}) == ["internet"]


def test_loss_also_needs_1_percent_of_its_15_s_window():
    # The 1 % rule (NE 2.2) never binds at 2 Hz, where two losses are 6 % of a window. This pins it with a probe
    # every 9.5 ms (a 15 s window holds ~1,579), a rate the product does not use.
    step, n = 0.0095, 3158
    inet = [20.0] * n

    def at(i):
        return (i + 1) * step

    def losses(lost_rows):
        lost = [at(i) for i in lost_rows]
        nf = {"probes": {"internet": {"sent": n, "lost": len(lost), "from_t": at(0)}}, "lost": {"internet": lost}}
        return kinds(run(net_cap(inet, net_flows=nf, step=step, duration=30.0)), "net_loss")

    spread = [842 + 42 * k for k in range(16)]                            # 0.4 s apart
    got = losses(spread)
    assert len(got) == 1 and got[0].severity >= 1.0                       # 16 / ~1579
    assert losses(spread[:15]) == []                                      # 15 / ~1579 < 1 %


def test_server_gaps_of_100_ms_or_more_are_events():
    nf = {"gaps": [[5.0, 100.0, "delay"], [9.0, 99.9, "loss"], ["junk"], [12.0, 250.0, None]]}
    gaps = kinds(run(net_cap([20.0] * 40, net_flows=nf)), "net_gap")
    assert [(g.t_start, g.t_end, g.severity, g.detail["kind"]) for g in gaps] == [
        (5.0, pytest.approx(5.1), 100.0, "delay"), (12.0, pytest.approx(12.25), 250.0, None)]


# ---------------------------------------------------------------------------------------- marks ---

def test_mark_window_and_what_it_overlaps():
    cap = frames_cap([10.0] * 500 + [100.0] + [10.0] * 500)    # a stutter ending at 6.1 s
    cap.marks = [8.0, 20.0, "junk"]
    evs = run(cap)
    marks = kinds(evs, "mark")
    assert [(m.t_start, m.t_end) for m in marks] == [(5.0, 8.5), (17.0, 20.5)]
    stutter = kinds(evs, "stutter")[0]
    assert marks[0].detail["frame_events"] == [stutter.id] and marks[0].detail["visible_in_frames"] is True
    assert marks[1].detail["visible_in_frames"] is False
    cap = make_cap({})
    cap.marks = [3.0]
    assert kinds(run(cap), "mark")[0].detail["visible_in_frames"] is None    # frames not measured


def test_a_pacing_episode_does_not_make_a_mark_seen():
    # CC-5: judder can span the whole capture, so only stutters and FPS drops say frame timing saw the lag
    cap = pacing_cap(0, anim=[2.5] * 600)
    cap.marks = [3.0]
    evs = run(cap)
    mark, pacing = kinds(evs, "mark")[0], kinds(evs, "pacing")[0]
    assert mark.detail["visible_in_frames"] is False and mark.detail["frame_events"] == []
    assert mark.detail["pacing_events"] == [pacing.id]


def test_an_event_whose_numbers_are_not_finite_is_left_out():
    # A15: the last guard behind the bounded readers, tested at its own seam (they keep it unreachable)
    ok = ev.LagEvent(id=0, kind="stutter", t_start=1.0, t_end=1.1, severity=50.0)
    bad = [ev.LagEvent(id=0, kind="stutter", t_start=1.0, t_end=1.1, severity=float("inf")),
           ev.LagEvent(id=0, kind="pacing", t_start=float("nan"), t_end=1.1, severity=1.0),
           ev.LagEvent(id=0, kind="net_gap", t_start=1.0, t_end=float("-inf"), severity=1.0)]
    assert ev._finite([ok] + bad) == [ok]


def test_absurd_values_from_a_corrupt_capture_are_missing_not_events():
    # A15: 1e308 overflowed sums (and made infinite severities) before
    cap = frames_cap([10.0] * 500, display_ms=10.0, displayed=1)
    cap.frames[F.DISPLAY_MS][100:102] = [1e308, 1e308]
    cap.frames[F.T][-1] = 1e308
    nf = {"probes": {"internet": {"sent": 100, "lost": 3, "from_t": 0.5}},
          "lost": {"internet": [10.0, 1e308, 1e308]}, "gaps": [[1e308, 200.0, "delay"], [5.0, 1e308, "loss"]]}
    cap.network, cap.net_flows, cap.marks = net_cap([20.0] * 100).network, nf, [1e308, 2.0]
    evs = run(cap)
    assert evs and all(math.isfinite(x) for e in evs for x in (e.t_start, e.t_end, e.severity))
    assert [e.kind for e in evs] == ["mark"]


# ------------------------------------------------------------------------------------- the list ---

def test_events_are_time_ordered_and_numbered_from_one():
    cap = frames_cap([10.0] * 500 + [100.0] + [10.0] * 500)
    cap.marks = [2.0]
    evs = run(cap)
    assert [e.id for e in evs] == list(range(1, len(evs) + 1))
    assert [e.t_start for e in evs] == sorted(e.t_start for e in evs)


def test_junk_capture_does_not_crash_and_invents_nothing():
    cap = make_cap({F.T: [1.0, "x", None, 2.0], F.FRAME_MS: ["a", None, 5.0, -3.0]},
                   network={N.T: ["q", 1.0], N.INET_RTT_MS: [5.0, "bad"]},
                   net_flows={"gaps": "nope", "lost": {"internet": ["x"]}, "probes": []})
    cap.marks = [None, float("nan")]
    assert run(cap) == []


def test_summary_stats():
    cap = net_cap([20.0, 22.0, None, 30.0, 25.0, 26.0], gw=3.0, duration=120.0,
                  net_flows={"probes": {"internet": {"sent": 5, "lost": 1, "from_t": 0.5}}})
    stutter = ev.LagEvent(id=1, kind="stutter", t_start=1.0, t_end=1.1, severity=50.0)
    s = ev.summary_stats(cap, [stutter, stutter])
    assert s["stutters"] == 2 and s["stutters_per_min"] == pytest.approx(1.0)
    assert s["ping_inet_ms"] == pytest.approx(25.0) and s["ping_gw_ms"] == 3.0 and s["ping_server_ms"] is None
    assert s["jitter_ms"] == pytest.approx((2.0 + 5.0 + 1.0) / 3)   # the mean; pairs across the loss skipped
    assert s["loss_pct"] == pytest.approx(20.0)
    assert s["swap_chains"] is None                            # frames not measured
    cap = make_cap(make_frames([10.0] * 5, chain="0x0"))
    cap.frames = {c: v + v[:2] for c, v in cap.frames.items()}
    cap.frames[F.SWAP_CHAIN][-2:] = ["0xAB", "0xAB"]
    assert ev.summary_stats(cap, [])["swap_chains"] == 2
