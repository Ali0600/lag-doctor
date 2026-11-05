"""analysis/events.py: every event kind, its thresholds pinned at literal points either side."""
from __future__ import annotations

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


# -------------------------------------------------------------------------------------- network ---

def net_cap(inet, *, gw=2.0, server=None, net_flows=None, duration=300.0, step=0.5):
    b = SeriesBuilder(N.ALL)
    for i, v in enumerate(inet):
        b.add(t=(i + 1) * step, inet_rtt_ms=v, gw_rtt_ms=gw, server_rtt_ms=server[i] if server else None)
    return make_cap(duration=duration, network=b.build(), net_flows=net_flows or {})


def test_ping_spike_needs_30_ms_over_the_rolling_minimum():
    assert len(kinds(run(net_cap([20.0] * 100 + [50.0] + [20.0] * 100)), "net_spike")) == 1
    assert kinds(run(net_cap([20.0] * 100 + [49.9] + [20.0] * 100)), "net_spike") == []


def test_ping_spike_needs_twice_the_median():
    base = [10.0] * 5 + [40.0] * 200                         # minimum 10 ms, median 40 ms
    assert len(kinds(run(net_cap(base + [80.0] + [40.0] * 5)), "net_spike")) == 1
    assert kinds(run(net_cap(base + [79.9] + [40.0] * 5)), "net_spike") == []


def test_ping_spike_minimum_looks_back_120_s_only():
    # 49 ms is over 2x the 20 ms median either way; only a 5 ms low inside the last 120 s makes it +30
    inet = [5.0] + [20.0] * 242 + [49.0] + [20.0] * 10        # the low is 121.5 s back: +29 over 20 ms
    assert kinds(run(net_cap(inet)), "net_spike") == []
    inet = [5.0] + [20.0] * 238 + [49.0] + [20.0] * 10        # the low is 119.5 s back: +44 ms
    assert len(kinds(run(net_cap(inet)), "net_spike")) == 1


def test_ping_spike_detail_and_merge():
    inet = [20.0] * 50 + [90.0, 95.0, 20.0, 99.0] + [20.0] * 50
    spikes = kinds(run(net_cap(inet)), "net_spike")
    assert len(spikes) == 1                                  # samples within 1 s are one episode
    d = spikes[0].detail
    assert spikes[0].severity == d["worst_ms"] == 99.0 and d["baseline_ms"] == 20.0 and d["target"] == "internet"
    assert d["samples"] == 3 and d["gw_ms"] == 2.0


def test_server_rtt_spikes_are_their_own_events():
    server = [30.0] * 50 + [90.0] + [30.0] * 50
    spikes = kinds(run(net_cap([20.0] * 101, server=server)), "net_spike")
    assert [e.detail["target"] for e in spikes] == ["server"]


def test_a_lost_probe_is_a_loss_event():
    nf = {"probes": {"internet": {"sent": 100, "lost": 1, "from_t": 0.5}}, "lost": {"internet": [25.0]}}
    loss = kinds(run(net_cap([20.0] * 100, net_flows=nf)), "net_loss")
    assert len(loss) == 1
    d = loss[0].detail
    assert d["target"] == "internet" and d["burst"] is True and d["lost"] == 1
    assert loss[0].severity == pytest.approx(100.0 / 31)      # 1 of the 31 probes in the 15 s around it


def test_not_probed_yet_is_not_lost():
    inet = [None] * 20 + [20.0] * 80                         # probing began at 10.5 s
    nf = {"probes": {"internet": {"sent": 80, "lost": 0, "from_t": 10.5}}}
    assert kinds(run(net_cap(inet, net_flows=nf)), "net_loss") == []
    inet[50] = None
    loss = kinds(run(net_cap(inet, net_flows=nf)), "net_loss")
    assert len(loss) == 1 and loss[0].t_start == pytest.approx(25.25)
    assert kinds(run(net_cap(inet, net_flows={})), "net_loss") == []    # no from_t, no list: cannot tell


def test_lost_probes_can_be_keyed_by_ip():
    nf = {"targets": {"internet": "1.1.1.1"}, "probes": {"1.1.1.1": {"sent": 100, "lost": 1, "from_t": 0.5}},
          "lost": {"1.1.1.1": [25.0]}}
    assert len(kinds(run(net_cap([20.0] * 100, net_flows=nf)), "net_loss")) == 1


def test_loss_needs_1_percent_in_15_s_or_a_10_percent_burst():
    # a probe every 9.5 ms: a 15 s window holds ~1,579 and the 200 ms around a probe exactly 21
    step, n = 0.0095, 3158
    inet = [20.0] * n

    def at(i):
        return (i + 1) * step

    def losses(lost_rows):
        lost = [at(i) for i in lost_rows]
        nf = {"probes": {"internet": {"sent": n, "lost": len(lost), "from_t": at(0)}}, "lost": {"internet": lost}}
        return kinds(run(net_cap(inet, net_flows=nf, step=step, duration=30.0)), "net_loss")

    assert losses([1052]) == []                                           # 0.06 % and 1 in 21: neither
    spread = [842 + 42 * k for k in range(16)]                            # 0.4 s apart: never two in 200 ms
    got = losses(spread)
    assert len(got) == 1 and not got[0].detail["burst"] and got[0].severity >= 1.0   # 16 / ~1579
    assert losses(spread[:15]) == []                                      # 15 / ~1579 < 1 %
    burst = losses([1052, 1053, 1054])                                    # 3 in 21 = 14 % inside 200 ms
    assert len(burst) == 1 and burst[0].detail["burst"] and burst[0].severity < 1.0
    assert losses([1052, 1053]) == []                                     # 2 in 21 = 9.5 %


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
