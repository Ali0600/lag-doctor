"""Time behind another window (frames.HIDDEN; DESIGN, "Time behind another window"): while another window covers the
game's, Windows shows none of its frames, so their display times say nothing the player saw. Those stretches are left
out of every frame figure, and the time left out is said.

The e2e run of 2026-11-09 read LagLab's covered window as "2 stutters (worst 9483 ms) and low frame rate (13 FPS
average)" while LagLab drew 120 FPS: the few frames Windows showed carried display gaps of 1.3, 9.5 and 2.1 s.
"""
from __future__ import annotations

import math

import pytest

from lagdoctor.analysis import crop, rules
from lagdoctor.analysis import events as ev
from lagdoctor.analysis import frames as fa
from lagdoctor.analysis.diagnose import diagnose
from lagdoctor.model import F, S, SeriesBuilder

from tests import synth

AWAY = "another window was in front and Windows showed almost none of the game's frames, so that time is not judged."


def chain(cap):
    fr = fa.main_chain(cap)
    return fr, [x is True for x in fr[fa.HIDDEN]]


def stutters(d):
    return [e for e in d.events if e.kind == "stutter"]


def set_focus(cap, f):
    """game_focused at each sampler tick: f(t, value) gives the new value."""
    col = cap.system[S.GAME_FOCUSED]
    cap.system[S.GAME_FOCUSED] = [f(t, v) for t, v in zip(cap.system[S.T], col)]


def game(segments, focus, secs=30.0):
    """A capture of a game presenting by `segments` [(until s, frame ms, shown?)], each shown frame a display change,
    with foreground readings focus(t) every 0.25 s."""
    b = SeriesBuilder(F.ALL)
    t, last = 0.0, None
    for until, ms, shown in segments:
        while t + ms / 1000.0 <= until + 1e-9:
            t += ms / 1000.0
            row = dict(t=t, frame_ms=ms, cpu_busy_ms=ms, cpu_wait_ms=0.0, gpu_busy_ms=1.0, displayed=1 if shown else 0,
                       swap_chain="0x0", frame_type="Application", runtime="Other", present_mode="Composed: Flip")
            if shown:
                row["display_ms"] = (t - last) * 1000.0 if last is not None else ms
                last = t
            b.add(**row)
    return synth.capture(b.build(), synth.system_table(secs, game_focused=focus), hz=144.0, duration=secs)


# ------------------------------------------------------------------------------------- the stretch ---

def test_a_covered_window_is_never_judged_and_the_time_left_out_is_said():
    d = diagnose(synth.laglab("smooth", covered=[(10.0, 20.0)]).cap)
    st = d.stats
    assert st["hidden_s"] == pytest.approx(10.0, abs=0.05)
    assert st["worst_frame_ms"] < 20.0 and st["avg_fps"] == pytest.approx(120.0, abs=3.0), st
    assert not [e for e in d.events if e.kind in ("stutter", "fps_drop")], d.events
    assert d.headline.endswith(f"; for 10s of the capture {AWAY}"), d.headline
    shown = diagnose(synth.laglab("smooth", unfocused=[(10.0, 20.0)]).cap)    # behind, yet shown: a second screen
    assert shown.stats["hidden_s"] == 0.0 and "not judged" not in shown.headline
    assert st["dropped_pct"] == pytest.approx(shown.stats["dropped_pct"], abs=0.5)   # the covered frames are not "dropped"


@pytest.mark.parametrize("start", [10.0, 10.05, 10.1, 10.15, 10.2, 10.249])
def test_an_alt_tab_hides_the_cover_at_any_sampler_phase(start):
    # The window loses the foreground as it is covered, so the readings next to the cover's ends say "in front": a rule
    # that asked them hid almost no real Alt+Tab. Only the readings a tick or more inside the cover are asked
    cap = synth.laglab("smooth", covered=[(start, start + 5.0)]).cap
    fr, hid = chain(cap)
    t = fr[F.T]
    assert all(h for x, h in zip(t, hid) if start + 0.02 < x < start + 4.98)
    assert not any(h for x, h in zip(t, hid) if x < start - 0.02 or x > start + 5.02)
    assert [v for x, v in zip(cap.system[S.T], cap.system[S.GAME_FOCUSED]) if start - 0.25 <= x < start] == [1]
    # the foreground moves a little after the screen does, both ways (a restore shows the window as it takes the
    # foreground): readings just inside the cover's ends say "in front", and they are not asked either
    set_focus(cap, lambda x, v: None if v is None else 0 if start + 0.2 <= x <= start + 4.8 else 1)
    fr, hid = chain(cap)
    assert all(h for x, h in zip(fr[F.T], hid) if start + 0.02 < x < start + 4.98)
    assert fa.hidden_spans(fr) == [(max(x for x in fr[F.T] if x <= start), min(x for x in fr[F.T] if x >= start + 5.0))]


@pytest.mark.parametrize("length, hidden", [(0.9, False), (1.1, True)])
def test_a_stretch_lasts_a_second(length, hidden):
    # a frame or two Windows did not show while the game was behind a window is a hitch it may have shown; a second
    # of none is a covered window
    d = diagnose(synth.laglab("smooth", covered=[(10.0, 10.0 + length)]).cap)
    if hidden:
        assert d.stats["hidden_s"] == pytest.approx(length, abs=0.05) and not stutters(d), d.events
    else:
        assert d.stats["hidden_s"] == 0.0
        assert [round(e.severity, -2) for e in stutters(d)] == [900.0], d.events


def test_a_frozen_screen_in_front_stays_a_freeze_and_so_does_one_nobody_can_place():
    # The same frames not shown, with the game in front: the player saw the screen stop. Without a foreground
    # reading, or with one "in front" inside, the cover cannot be told from that, so it is judged
    for change in (lambda t, v: 1 if v is not None else None,                       # in front all along
                   lambda t, v: None if 10.0 < t < 13.0 else v,                     # not read inside it
                   lambda t, v: 1 if 11.4 < t < 11.6 else v):                       # one reading "in front"
        cap = synth.laglab("smooth", covered=[(10.0, 13.0)]).cap
        set_focus(cap, change)
        d = diagnose(cap)
        assert d.stats["hidden_s"] == 0.0 and [round(e.severity, -2) for e in stutters(d)] == [3000.0], d.events
    cap = synth.laglab("smooth", covered=[(10.0, 13.0)]).cap
    cap.system = {}                                                                 # no foreground recorded at all
    fr, hid = chain(cap)
    assert fr[fa.HIDDEN] == [] and fa.frame_stats(fr, cap.duration_s)["hidden_s"] is None


def test_the_shown_bursts_and_short_runs_of_a_cover_belong_to_it():
    # A covered window's frames show in bursts (1-12 frames between runs of seconds in the e2e capture): a short run
    # within a tick of a stretch, with no reading "in front" between, is part of it, and so is the burst between them.
    # Half a second on, with the game in front between, it is the player's again
    cap = game([(10.0, 8.0, True), (14.0, 8.0, False), (14.08, 8.0, True), (14.16, 8.0, False), (16.0, 8.0, True),
                (16.08, 8.0, False), (30.0, 8.0, True)],
               focus=lambda t: 0 if 10.0 <= t <= 14.16 or 16.0 <= t <= 16.08 else 1)
    fr, hid = chain(cap)
    t = fr[F.T]
    assert all(h for x, h in zip(t, hid) if 10.01 < x < 14.17) and not any(h for x, h in zip(t, hid) if x > 14.2)
    evs = ev.find_events(cap, fr, fa.frame_stats(fr, cap.duration_s))
    assert [(round(e.t_end, 2), round(e.severity)) for e in evs if e.kind == "stutter"] == [(16.09, 88)]
    # within a tick of the stretch, but with a reading "in front" between (the player came back), or one unknown:
    # theirs again
    for back, between, joined in ((14.25, 1, False), (14.25, None, False), (14.3, 1, True)):
        cap = game([(10.0, 8.0, True), (14.0, 8.0, False), (14.2, 8.0, True), (14.28, 8.0, False), (30.0, 8.0, True)],
                   focus=lambda t, back=back, between=between: 0 if 10.0 <= t < back else between)
        fr, hid = chain(cap)
        evs = ev.find_events(cap, fr, fa.frame_stats(fr, cap.duration_s))
        assert bool([e for e in evs if e.kind == "stutter"]) is not joined, (back, between, evs)
        assert all(h for x, h in zip(fr[F.T], hid) if 14.21 < x < 14.28) is joined


def stutter_list(cap):
    fr, hid = chain(cap)
    evs = ev.find_events(cap, fr, fa.frame_stats(fr, cap.duration_s))
    return fr, hid, [(round(e.t_end, 2), round(e.severity)) for e in evs if e.kind == "stutter"]


def test_a_cover_shown_in_several_bursts_is_one_stretch():
    # Bursts between the runs of one cover: a run more than a tick from both stretches is still part of the cover when
    # every reading between them says another window and most frames there went unshown (a 312 ms "stutter" before)
    cap = game([(10.0, 8.0, True), (14.0, 8.0, False), (14.08, 8.0, True), (14.38, 8.0, False), (14.46, 8.0, True),
                (14.76, 8.0, False), (14.84, 8.0, True), (15.14, 8.0, False), (15.22, 8.0, True), (18.0, 8.0, False),
                (30.0, 8.0, True)], focus=lambda t: 0 if 10.0 <= t <= 18.0 else 1)
    fr, hid, sts = stutter_list(cap)
    assert sts == [] and all(h for x, h in zip(fr[F.T], hid) if 10.01 < x < 18.0)
    # a short run just before a stretch joins it too, every reading between them "another window"
    cap = game([(10.0, 8.0, True), (10.08, 8.0, False), (10.16, 8.0, True), (14.0, 8.0, False), (30.0, 8.0, True)],
               focus=lambda t: 0 if 10.0 <= t <= 14.0 else 1)
    fr, hid, sts = stutter_list(cap)
    assert sts == [] and all(h for x, h in zip(fr[F.T], hid) if 10.01 < x < 14.0)
    # two stretches with a reading "in front" between: the burst between them was the player's
    cap = game([(10.0, 8.0, True), (14.1, 8.0, False), (14.3, 8.0, True), (18.0, 8.0, False), (30.0, 8.0, True)],
               focus=lambda t: 1 if t < 10.0 or 14.2 < t < 14.4 or t > 18.0 else 0)
    fr, hid, _ = stutter_list(cap)
    assert not any(h for x, h in zip(fr[F.T], hid) if 14.12 < x < 14.29)
    assert all(h for x, h in zip(fr[F.T], hid) if 10.01 < x < 14.1 or 14.31 < x < 18.0)


def test_the_time_in_front_at_a_covers_end_is_one_frozen_screen_and_the_rest_is_left_out():
    # One reading "in front" past a stretch's pad made the whole run a stutter as long as the cover: a freeze in front
    # before an Alt+Tab, or the foreground back before a frame showed, is a frozen screen of its own length
    cap = game([(10.0, 8.0, True), (10.6, 600.0, False), (16.0, 8.0, False), (30.0, 8.0, True)],
               focus=lambda t: 1 if t < 10.6 or t > 16.0 else 0)              # frozen in front, then covered
    fr, hid, sts = stutter_list(cap)
    assert sts == [(10.5, 500)], sts                                         # from the last frame to the last reading
    assert fa.frame_stats(fr, cap.duration_s)["hidden_s"] == pytest.approx(5.5, abs=0.02)
    for back, freeze in ((14.5, 1508), (15.8, None)):                       # back in front 1.5 s before a frame
        cap = game([(10.0, 8.0, True), (16.0, 8.0, False), (30.0, 8.0, True)],
                   focus=lambda t, back=back: 0 if 10.0 <= t < back else 1)
        fr, hid, sts = stutter_list(cap)
        assert sts == ([(16.01, freeze)] if freeze else []), (back, sts)    # to the frame at 16.008 s
        assert fa.frame_stats(fr, cap.duration_s)["hidden_s"] == pytest.approx(4.5 if freeze else 6.0, abs=0.02)
    cap = game([(10.0, 8.0, True), (16.0, 6000.0, True), (30.0, 8.0, True)],    # it drew nothing behind the window
               focus=lambda t: 0 if 10.0 <= t < 14.5 else 1)
    assert stutter_list(cap)[2] == [(16.0, 1500)]
    # not read at its start (the sampler could not tell): judged like a reading "in front", never taken for behind
    cap = game([(10.0, 8.0, True), (16.0, 8.0, False), (30.0, 8.0, True)],
               focus=lambda t: None if 10.0 <= t < 11.6 else 0 if t <= 16.0 else 1)
    assert stutter_list(cap)[2] == [(11.5, 1500)]


def unread(cap, lo, hi):
    """The sampler's rows in (lo, hi) taken out: a sampler that read nothing then."""
    keep = [i for i, x in enumerate(cap.system[S.T]) if not lo < x < hi]
    cap.system = {k: [v[i] for i in keep] for k, v in cap.system.items()}
    return cap


def test_a_sampler_that_did_not_read_the_foreground_inside_a_cover_hides_nothing():
    # two readings at least, none more than a second apart: a starved sampler cannot vouch for the time it did not read
    for gap, hidden in (((11.0, 12.0), True), ((11.0, 12.1), False),          # 1.0 s and 1.25 s between readings
                        ((10.0, 11.6), False)):                               # nothing read the cover's first 1.5 s
        cap = unread(game([(10.0, 8.0, True), (15.0, 8.0, False), (30.0, 8.0, True)],
                          focus=lambda t: 0 if 10.0 <= t <= 15.0 else 1), *gap)
        assert any(chain(cap)[1]) is hidden, gap
    # nor does a stretch beside it vouch for a long run the sampler did not read
    cap = game([(10.0, 8.0, True), (13.0, 8.0, False), (13.1, 8.0, True), (14.5, 8.0, False), (30.0, 8.0, True)],
               focus=lambda t: 0 if 10.0 <= t <= 14.5 else 1)
    keep = [i for i, x in enumerate(cap.system[S.T]) if not 13.05 < x < 14.55]
    cap.system = {k: [v[i] for i in keep] for k, v in cap.system.items()}
    fr, hid = chain(cap)
    assert any(hid) and not any(h for x, h in zip(fr[F.T], hid) if x > 13.15)
    cap = synth.laglab("smooth", covered=[(10.0, 11.1)]).cap       # one reading a tick inside it: too few to vouch
    keep = [i for i, x in enumerate(cap.system[S.T]) if not (10.2 < x < 10.3 or 10.7 < x < 10.8)]
    cap.system = {k: [v[i] for i in keep] for k, v in cap.system.items()}
    assert [round(x, 1) for x in cap.system[S.T] if 10.24 < x < 10.86] == [10.5] and not any(chain(cap)[1])


def test_a_game_that_stops_drawing_behind_another_window_is_no_freeze():
    # Many games draw nothing while covered or minimized: no frame at all, the next one spanning the time behind
    for focus, freeze in ((lambda t: 0 if 10.0 <= t <= 13.0 else 1, False), (lambda t: 1, True)):
        cap = game([(10.0, 8.0, True), (13.0, 3000.0, True), (30.0, 8.0, True)], focus=focus)
        fr, _ = chain(cap)
        st = fa.frame_stats(fr, cap.duration_s)
        assert (st["worst_frame_ms"] >= 3000.0) is freeze, st
        assert st["hidden_s"] == (0.0 if freeze else pytest.approx(3.0)), st


def test_frames_missing_inside_a_cover_are_no_stall():
    # PresentMon can lose frames it held back (its queue overflows): a jump in time inside the cover is nobody's freeze
    for covered, stall in (([(10.0, 16.0)], False), ([], True)):
        cap = synth.laglab("smooth", covered=covered).cap
        keep = [i for i, x in enumerate(cap.frames[F.T]) if not 12.0 < x < 13.0]
        cap.frames = {k: [v[i] for i in keep] for k, v in cap.frames.items()}
        fr, _ = chain(cap)
        assert bool(fa.stall_gaps(fr)) is stall


def test_an_fps_drop_never_spans_a_cover():
    # 30 FPS before and after a cover is two drops, not one across time nobody was shown: a 1 s window reaching into
    # the cover counts the frames Windows did not show, so it is no reading
    cap = game([(7.0, 8.0, True), (10.0, 33.0, True), (20.0, 8.0, False), (23.0, 33.0, True), (30.0, 8.0, True)],
               focus=lambda t: 0 if 10.0 <= t <= 20.0 else 1)
    fr, _ = chain(cap)
    drops = [e for e in ev.find_events(cap, fr, fa.frame_stats(fr, cap.duration_s)) if e.kind == "fps_drop"]
    assert len(drops) == 2 and drops[0].t_end < 9.5 and drops[1].t_start > 20.5, drops     # half a window clear of it


def test_the_fps_target_is_never_read_from_a_cover():
    # Mostly covered: the 1 s windows over the cover count no frame shown, and as the target's own readings they
    # pulled it down to the 30 FPS the game showed after it, so that slump was no drop
    cap = game([(3.0, 8.0, True), (26.0, 8.0, False), (30.0, 33.0, True)],
               focus=lambda t: 0 if 3.0 <= t <= 26.0 else 1)
    fr, _ = chain(cap)
    drops = [e for e in ev.find_events(cap, fr, fa.frame_stats(fr, cap.duration_s)) if e.kind == "fps_drop"]
    assert [(e.detail["target_fps"] > 100, e.detail["target_from"], e.t_start > 26.0) for e in drops] == \
        [(True, "p90", True)], drops


def test_the_fps_targets_cap_is_never_read_from_a_cover():
    # A game that slows itself to 30 FPS behind other windows, mostly covered: its background cadence read as a 30 FPS
    # cap, and against that target a drop from 100 to 40 FPS in front was no drop
    cap = game([(5.0, 10.0, True), (8.0, 25.0, True), (12.0, 10.0, True), (52.0, 33.3, False), (60.0, 10.0, True)],
               focus=lambda t: 0 if 12.0 <= t <= 52.0 else 1, secs=60.0)
    fr, _ = chain(cap)
    drops = [e for e in ev.find_events(cap, fr, fa.frame_stats(fr, cap.duration_s)) if e.kind == "fps_drop"]
    assert [(round(e.t_start), round(e.t_end)) for e in drops] == [(5, 8)] and drops[0].detail["target_fps"] > 90, drops


def test_time_behind_another_window_counts_the_games_own_frames_once():
    # With frame generation one present can make two rows, each with its own MsBetweenPresents (PM 2.4): the time
    # behind is the stretch's own, never its rows' frame times summed
    cap = synth.laglab("smooth", covered=[(10.0, 20.0)]).cap
    rows = cap.frames
    n = len(rows[F.T])
    inside = [i for i, x in enumerate(rows[F.T]) if 10.0 < x < 20.0]
    for i in inside:                                     # each frame behind the window doubled by a generated one
        for k in F.ALL:
            rows[k].append(rows[k][i])
        rows[F.FRAME_TYPE][-1] = "AMD_AFMF"
    assert len(rows[F.T]) == n + len(inside)
    fr, _ = chain(cap)
    assert fa.frame_stats(fr, cap.duration_s)["hidden_s"] == pytest.approx(10.0, abs=0.05)


def test_stutters_a_minute_count_the_time_judged():
    d = diagnose(synth.laglab("hitch", covered=[(10.0, 20.0)]).cap)
    n, away = len(stutters(d)), d.stats["hidden_s"]
    assert n == 6 and away == pytest.approx(10.0, abs=0.05)
    assert d.stats["stutters_per_min"] == pytest.approx(60.0 * n / (30.0 - away))


def test_time_behind_another_window_is_never_steady():
    # the bound and the rules' 1 s means judge what limits the game on frames the player saw
    cap = synth.laglab("smooth", covered=[(10.0, 20.0)]).cap
    fr, hid = chain(cap)
    b = fa.bound(cap, fr, [])
    assert b["steady_s"] == pytest.approx(math.fsum(m for m, h in zip(fr[F.FRAME_MS], hid) if not h) / 1000.0, abs=0.1)
    env = rules._env(cap, {"frames": fr, "stats": {}, "bound": b, "events": []})
    assert any(a < 10.0 and b_ > 19.9 for a, b_ in env.tick_spans), env.tick_spans


def test_a_capture_covered_throughout_names_why_nothing_was_judged():
    # the frames are there, yet none was shown: never "recorded no frames", and not_focused cannot compare
    cap = synth.laglab("smooth").cap
    n = len(cap.frames[F.T])
    cap.frames[F.DISPLAYED] = [0] * (n - 1) + [1]
    cap.frames[F.DISPLAY_MS] = [None] * (n - 1) + [1000.0 * (cap.frames[F.T][-1] - cap.frames[F.T][0])]
    set_focus(cap, lambda t, v: 0 if v is not None else None)
    d = diagnose(cap)
    why = "every frame of the capture came while another window was in front, and Windows showed almost none of them"
    assert d.measured["frames"] == why and d.lag_type == "unknown"
    assert d.headline.startswith(f"Frame timing was not measured ({why}), so stutter and frame rate"), d.headline
    assert AWAY not in d.headline                                    # said once, by the reason
    assert d.unchecked["not_focused"].startswith("the game was behind another window for all of the capture")


def test_a_part_cut_inside_a_cover_hides_what_the_whole_recording_hides():
    cap = synth.laglab("smooth", covered=[(5.0, 25.0)]).cap
    wfr, whid = chain(cap)
    whole = dict(zip(wfr[F.T], whid))
    for t0, t1 in ((3.0, 22.0), (9.5, 20.5)):            # one edge of the cover inside the part; the part all cover
        fr, hid = chain(crop.crop(cap, t0, t1))
        assert all(h == whole[x] for x, h in zip(fr[F.T], hid) if 5.3 < x < 24.7), (t0, t1)
        assert all(hid[i] for i, x in enumerate(fr[F.T]) if 5.3 < x < 24.7)
    d = diagnose(crop.crop(cap, 9.5, 20.5))               # said of the part, never of the whole recording
    assert d.measured["frames"].startswith("every frame of the measured part came while another window was in front")


BURSTS = [(10.0, 8.0, True), (14.0, 8.0, False), (14.08, 8.0, True), (14.38, 8.0, False), (14.46, 8.0, True),
          (14.76, 8.0, False), (14.84, 8.0, True), (15.14, 8.0, False), (15.22, 8.0, True), (18.0, 8.0, False),
          (30.0, 8.0, True)]                         # covered from 10 s to 18 s, shown in three bursts in between


def bursts_focus(t):
    return 0 if 10.0 <= t <= 18.0 else 1


def test_a_part_judges_each_row_of_a_cover_its_edge_cuts_as_the_whole_recording_does():
    # Cut at 14 s, the bursts' short runs lost the stretch they bridged to and were judged again on what was left: "1
    # stutter (4.7 a minute, worst 312 ms)", where the whole recording named none
    cap = game(BURSTS, focus=bursts_focus)
    wfr, whid = chain(cap)
    whole = dict(zip(wfr[F.T], whid))
    assert len(whole) == len(wfr[F.T]) and stutter_list(cap)[2] == []
    assert fa.frame_stats(wfr, cap.duration_s)["dropped_pct"] == 0.0
    cuts = ([(t0, 30.0) for t0 in (13.5, 13.9, 14.0, 14.05, 14.3, 14.5, 14.9, 15.18)]
            + [(3.0, t1) for t1 in (14.1, 14.5, 14.9, 15.18, 15.5)])
    for t0, t1 in cuts:
        part = crop.crop(cap, t0, t1)
        fr, hid = chain(part)
        assert len(hid) == len(fr[F.T]) > 0 and all(h == whole[x] for x, h in zip(fr[F.T], hid)), (t0, t1)
        assert stutter_list(part)[2] == [], (t0, t1)
        assert fa.frame_stats(fr, part.duration_s)["dropped_pct"] == 0.0, (t0, t1)
    fr, hid = chain(crop.crop(cap, 14.0, 30.0))         # the frames that ended the bursts' runs are in it, and hidden
    assert [h for x, h in zip(fr[F.T], hid) if abs(x - 14.384) < 0.004 or abs(x - 14.768) < 0.004] == [True, True]


def test_the_covers_a_part_carries_are_judged_chain_by_chain_as_main_chain_judges_its_own():
    # an overlay's chain shown every 0.1 s beside the game's: judged with the game's frames, its rows would split the
    # cover into runs a tenth of a second long
    cap = game(BURSTS, focus=bursts_focus)
    overlay = [0.05 + 0.1 * k for k in range(300)]
    for name in cap.frames:
        extra = {F.T: overlay, F.SWAP_CHAIN: ["0x1"] * 300, F.DISPLAYED: [1] * 300, F.FRAME_MS: [100.0] * 300,
                 F.DISPLAY_MS: [100.0] * 300}.get(name, [None] * 300)
        cap.frames[name] = list(cap.frames[name]) + list(extra)
    wfr, whid = chain(cap)
    assert set(wfr[F.SWAP_CHAIN]) == {"0x0"} and any(whid)
    fr, hid = chain(crop.crop(cap, 0.0, 30.0))          # from the recording's own start: every row of it
    assert hid == whid
    assert stutter_list(crop.crop(cap, 14.0, 30.0))[2] == []


def test_a_part_never_hides_what_the_whole_recording_judged():
    # a reading "in front" in a run's middle leaves it judged; cut off by the part's start, it no longer read so, and the
    # part called that time behind another window
    cap = game([(10.0, 8.0, True), (16.0, 8.0, False), (30.0, 8.0, True)],
               focus=lambda t: 1 if 11.4 < t < 11.6 else (0 if 10.0 <= t <= 16.0 else 1))
    cap.sources = synth.sources(("frames", "system"))
    assert diagnose(cap).stats["hidden_s"] == 0.0
    d = diagnose(crop.crop(cap, 12.0, 30.0))
    assert d.stats["hidden_s"] == 0.0 and AWAY not in d.headline, d.headline


def test_a_part_never_judges_a_frozen_screen_that_began_before_it():
    # frozen in front from 10.0 s to 10.5 s, then covered; the frame ending it kept by the part (its display change was
    # not recorded, so nothing says it reached back): its frozen screen began before the part, as no frame of it may
    cap = game([(10.0, 8.0, True), (10.6, 600.0, False), (16.0, 8.0, False), (30.0, 8.0, True)],
               focus=lambda t: 1 if t < 10.6 or t > 16.0 else 0)
    k = next(i for i, x in enumerate(cap.frames[F.T]) if x > 16.0 and cap.frames[F.DISPLAYED][i] == 1)
    cap.frames[F.DISPLAY_MS][k] = None
    fr, _ = chain(cap)
    assert fr[fa.FRONT][fr[F.T].index(cap.frames[F.T][k])] == pytest.approx((10.0, 10.5), abs=0.01)
    part = crop.crop(cap, 10.3, 30.0)
    fr, _ = chain(part)
    assert cap.frames[F.T][k] in fr[F.T] and not [f for f in fr[fa.FRONT] if f is not None], fr[fa.FRONT]
    assert not [e for e in ev.find_events(part, fr, fa.frame_stats(fr, part.duration_s)) if e.t_start < 10.3]


def test_a_part_that_cannot_tell_carries_nothing():
    # the sampler read nothing in the part: whatever the whole recording judged, the part says it could not tell
    part = crop.crop(unread(game(BURSTS, focus=bursts_focus), 13.0, 31.0), 14.0, 30.0)
    fr, _ = chain(part)
    assert fr[fa.HIDDEN] == [] and fa.frame_stats(fr, part.duration_s)["hidden_s"] is None


def test_crop_carries_the_covers_of_a_capture_that_cannot_tell():
    # a capture with no foreground reading at all: no cover to carry, and nothing that fails for it
    cap = synth.laglab("hitch").cap
    cap.system[S.GAME_FOCUSED] = [None] * len(cap.system[S.T])
    part = crop.crop(cap, 5.0, 25.0)
    assert part.covers is not None and not any(part.covers[0]) and fa.main_chain(part)[fa.HIDDEN] == []


def test_frames_presentmon_lost_inside_a_cover_are_time_behind_it_all_the_same():
    cap = synth.laglab("smooth", covered=[(10.0, 16.0)]).cap
    keep = [i for i, x in enumerate(cap.frames[F.T]) if not 11.0 < x < 15.0]
    cap.frames = {k: [v[i] for i in keep] for k, v in cap.frames.items()}
    fr, _ = chain(cap)
    assert fa.frame_stats(fr, cap.duration_s)["hidden_s"] == pytest.approx(6.0, abs=0.05)


def test_a_partial_presentmon_with_every_frame_covered_names_the_cover_first():
    from lagdoctor.model import SourceStatus
    cap = synth.laglab("smooth").cap
    n = len(cap.frames[F.T])
    cap.frames[F.DISPLAYED] = [0] * (n - 1) + [1]
    cap.frames[F.DISPLAY_MS] = [None] * (n - 1) + [1000.0 * (cap.frames[F.T][-1] - cap.frames[F.T][0])]
    set_focus(cap, lambda t, v: 0 if v is not None else None)
    cap.sources["frames"] = SourceStatus("partial", "warning: 236 overflowed present events detected.")
    d = diagnose(cap)
    assert d.measured["frames"] == ("every frame of the capture came while another window was in front, and Windows "
                                    "showed almost none of them; partial: warning: 236 overflowed present events "
                                    "detected."), d.measured["frames"]
    fr = fa.main_chain(cap)
    assert fa.bound(cap, fr, [])["notes"][0] == ("no steady frames: the ones outside lag events came while another "
                                                 "window was in front")


def test_a_pacing_episode_never_spans_a_cover():
    cap = synth.laglab("pace", covered=[(10.0, 11.5)]).cap
    fr, _ = chain(cap)
    pacing = [e for e in ev.find_events(cap, fr, fa.frame_stats(fr, cap.duration_s)) if e.kind == "pacing"]
    assert len(pacing) >= 2 and not [e for e in pacing if e.t_start < 10.0 and e.t_end > 11.5], pacing


def test_the_lift_test_never_says_no_moment_came_behind_a_window_it_was_behind_most_of_the_time():
    # Behind another window in most quiet seconds, the "behind" signal sits at its top: no moment can rise above it,
    # so "only 0 of 12 coincided with another window being in front" was false (it was behind for all 12)
    from lagdoctor.analysis import attribute
    cap = synth.laglab("hitch", unfocused=[(0.0, 32.0)], duration_s=40.0).cap        # behind 80 %, still shown
    fr = fa.main_chain(cap)
    evs = ev.find_events(cap, fr, fa.frame_stats(fr, cap.duration_s))
    entry = attribute.attribute(cap, fr, evs, fa.bound(cap, fr, evs))["not_focused"]
    assert entry["n"] >= attribute.CONTRA_MIN_EVENTS and entry["a"] == 0, entry
    assert entry.get("blind") is True and entry["label"] != "contradicts", entry
    # in front all the time: the signal never moved off nothing, and "0 of 12" is true, so it still counts against
    cap = synth.laglab("hitch", duration_s=40.0).cap
    fr = fa.main_chain(cap)
    evs = ev.find_events(cap, fr, fa.frame_stats(fr, cap.duration_s))
    entry = attribute.attribute(cap, fr, evs, fa.bound(cap, fr, evs))["not_focused"]
    assert entry["label"] == "contradicts" and "blind" not in entry, entry


def test_the_bounds_one_second_windows_leave_time_behind_another_window_out():
    # a game thread busy only while covered is not what limits the game the player saw
    cap = synth.laglab("smooth", covered=[(5.0, 25.0)]).cap
    for col in (S.GAME_TOP_THREAD_PCT, S.GAME_LEAD_THREAD_PCT):
        cap.system[col] = [100.0 if 5.0 < t < 25.0 else v for t, v in zip(cap.system[S.T], cap.system[col])]
    fr = fa.main_chain(cap)
    assert fa.bound(cap, fr, [])["busiest_thread"] < 0.5


# ------------------------------------------------------------- before PresentMon's first row, after its last ---

def drop_rows(cap, keep):
    """The capture's frame rows whose time `keep` accepts: PresentMon writes none before a covered window's first shown
    frame, and loses those after its last shown one (PM 2.4)."""
    rows = [i for i, x in enumerate(cap.frames[F.T]) if keep(x)]
    cap.frames = {k: [v[i] for i in rows] for k, v in cap.frames.items()}
    return cap


def test_time_behind_another_window_at_either_end_of_presentmons_rows_is_left_out_too():
    # The hitch capture covered from 20 s to its end read "6 stutters (12.0 a minute, ...)" with no time left out, and
    # the same cover from its start likewise, while that cover in the middle read 18.0 a minute and 10 s left out
    mid = diagnose(synth.laglab("hitch", covered=[(10.0, 20.0)]).cap)
    end = diagnose(synth.laglab("hitch", covered=[(20.0, 30.0)]).cap)        # synth's rows stop at 19.99 s, as real ones
    start = diagnose(drop_rows(synth.laglab("hitch", covered=[(0.0, 10.0)]).cap, lambda x: x >= 10.0))
    for d, away in ((end, 10.0), (start, 9.75)):             # LagLab starts at 0.4 s: no reading says either way before
        assert len(stutters(d)) == 6 and d.stats["hidden_s"] == pytest.approx(away, abs=0.05), d.stats
        assert d.stats["stutters_per_min"] == pytest.approx(60.0 * 6 / (30.0 - d.stats["hidden_s"]))
        assert d.headline.endswith(AWAY), d.headline
    assert end.headline == mid.headline, (end.headline, mid.headline)
    spans = fa.hidden_spans(fa.main_chain(drop_rows(synth.laglab("hitch", covered=[(0.0, 5.0), (12.0, 18.0)]).cap,
                                                    lambda x: x >= 5.0)))
    assert len(spans) == 2 and spans == sorted(spans) and spans[0][1] <= 5.01, spans     # in time order: bisected


def test_an_edge_is_judged_by_the_stretchs_own_rule():
    # the foreground says how much of an end had another window in front: never the time PresentMon was starting with
    # the game in front, nor time it never read, nor less than a second
    def ended(focus, upto=30.0, rows_from=0.0, secs=30.0):
        cap = game([(20.0, 8.0, True), (secs, 8.0, False)], focus, secs=secs)
        cap = drop_rows(cap, lambda x: rows_from <= x <= 20.0)        # nothing written after the last shown frame
        if upto < secs:                                               # the sampler stopped before the recording did
            keep = [i for i, t in enumerate(cap.system[S.T]) if t <= upto]
            cap.system = {k: [v[i] for i in keep] if isinstance(v, list) else v for k, v in cap.system.items()}
        return fa.frame_stats(fa.main_chain(cap), cap.duration_s)["hidden_s"]

    assert ended(lambda t: 0 if t > 20.0 else 1) == pytest.approx(10.0, abs=0.05)
    assert ended(lambda t: 0 if 20.0 < t < 27.0 else 1) == pytest.approx(7.0, abs=0.05)       # back in front at 27 s
    assert ended(lambda t: 0 if t > 20.0 else 1, upto=27.0) == pytest.approx(7.0, abs=0.05)   # read until 27 s only
    assert ended(lambda t: 0 if t > 29.3 else 1) == 0.0                                      # under a second
    assert ended(lambda t: 1) == 0.0                                                         # in front: not behind
    # before the first row: PresentMon still starting with the game in front is no time behind; behind it is
    assert ended(lambda t: 1, rows_from=3.0) == 0.0
    assert ended(lambda t: 0 if t < 3.0 else 1, rows_from=3.0) == pytest.approx(3.0, abs=0.05)    # from its start
    # cut at the last reading in front (0.75 s), as a run is: the game went behind somewhere after it
    assert ended(lambda t: 1 if t < 1.0 else (0 if t < 3.0 else 1), rows_from=3.0) == pytest.approx(2.25, abs=0.05)


def test_a_part_ends_where_the_part_does():
    # a part from 5 s to 25 s of a recording covered from 20 s on: 5 s of the part was behind, never 10 s of it
    cap = synth.laglab("hitch", covered=[(20.0, 30.0)]).cap
    part = crop.crop(cap, 5.0, 25.0)
    # the whole recording's judgement of that time, cut at the part's end (its foreground was read on past it)
    assert fa.frame_stats(fa.main_chain(part), part.duration_s)["hidden_s"] == pytest.approx(5.0, abs=0.05)
    assert diagnose(part).headline.endswith(AWAY)


def test_the_bound_never_judges_a_cover_at_the_recordings_end():
    # a game thread busy only while covered, from 20 s to the end, read "cpu_main" (busiest thread 100 %)
    cap = synth.laglab("smooth", covered=[(20.0, 60.0)], duration_s=60.0).cap
    for col in (S.GAME_TOP_THREAD_PCT, S.GAME_LEAD_THREAD_PCT):
        cap.system[col] = [100.0 if t > 20.0 else v for t, v in zip(cap.system[S.T], cap.system[col])]
    fr = fa.main_chain(cap)
    assert fa.bound(cap, fr, [])["busiest_thread"] < 0.5


def test_a_parts_ends_are_judged_as_the_whole_recording_judged_that_time():
    # a 15 s gap with no frame, a reading "in front" in its middle: the whole recording judged it a frozen screen; a
    # part from 13 s, past that reading, judged its own start edge and called 12 s of it time behind another window
    cap = game([(10.0, 8.0, True), (25.0, 15000.0, True), (30.0, 8.0, True)],
               focus=lambda t: 1 if t < 10.0 or abs(t - 12.0) < 0.1 or t > 25.0 else 0)
    cap.sources = synth.sources(("frames", "system"))
    whole = diagnose(cap)
    assert whole.stats["hidden_s"] == 0.0 and [round(e.severity) for e in stutters(whole)] == [15000], whole.headline
    d = diagnose(crop.crop(cap, 13.0, 30.0))
    assert d.stats["hidden_s"] == 0.0 and AWAY not in d.headline, d.headline


def test_the_time_after_the_games_last_frame_is_no_cover_when_the_game_exited():
    # a game shutting down closes its window first: the foreground reads another window until the process ends
    cap = synth.laglab("hitch", covered=[(20.0, 30.0)]).cap
    cap.target["stop_reason"] = "game exited"
    assert diagnose(cap).stats["hidden_s"] == 0.0
    cap.target["stop_reason"] = "duration"
    assert diagnose(cap).stats["hidden_s"] == pytest.approx(10.0, abs=0.05)
    # a cover from the start is no shutdown, whatever ended the recording
    cap = drop_rows(synth.laglab("hitch", covered=[(0.0, 10.0)]).cap, lambda x: x >= 10.0)
    cap.target["stop_reason"] = "game exited"
    assert diagnose(cap).stats["hidden_s"] == pytest.approx(9.75, abs=0.05)


def test_no_frame_of_the_chain_lies_inside_an_edge():
    # an edge from the chain's own last frame took that frame out of the frame chart's scale and its note, though the
    # analysis counted it (the worst frame, a stutter)
    for cov, keep in (([(20.0, 30.0)], lambda x: True), ([(0.0, 10.0)], lambda x: x >= 10.0)):
        fr = fa.main_chain(drop_rows(synth.laglab("hitch", covered=cov).cap, keep))
        assert fr[fa.EDGES], cov
        assert not [x for x in fr[F.T] for a, b in fr[fa.EDGES] if a <= x <= b], cov
        starts = [a for a, _ in fa.hidden_spans(fr)]
        assert not fa._in_spans(fr[F.T][-1 if cov[0][0] else 0], fa.hidden_spans(fr), starts), cov


def test_a_frame_shown_on_another_chain_ends_an_edge():
    # the game showed its menus on another swap chain until 20 s while another window had the foreground (a second
    # screen): the main chain's start edge read 20 s behind another window though Windows showed every frame
    cap = game([(20.0, 8.0, False), (30.0, 8.0, True)], focus=lambda t: 0 if t < 20.0 else 1)
    menus = [k * 0.1 for k in range(1, 200)]               # fewer shown frames than the game's own chain
    cap = drop_rows(cap, lambda x: x > 20.0)
    for name in cap.frames:
        extra = {F.T: menus, F.SWAP_CHAIN: ["0x1"] * len(menus), F.DISPLAYED: [1] * len(menus),
                 F.FRAME_MS: [100.0] * len(menus), F.DISPLAY_MS: [100.0] * len(menus)}.get(name, [None] * len(menus))
        cap.frames[name] = list(cap.frames[name]) + list(extra)
    fr = fa.main_chain(cap)
    assert set(fr[F.SWAP_CHAIN]) == {"0x0"} and fr[F.T][0] > 20.0
    assert fr[fa.EDGES] == () and fa.frame_stats(fr, cap.duration_s)["hidden_s"] == 0.0, fr[fa.EDGES]
    part = crop.crop(cap, 5.0, 30.0)                    # a part takes the whole recording's judgement of that time
    assert fa.frame_stats(fa.main_chain(part), part.duration_s)["hidden_s"] == 0.0
    # the same start with no other chain is time behind
    fr = fa.main_chain(drop_rows(cap, lambda x: x > 20.0))
    assert fa.frame_stats(fr, cap.duration_s)["hidden_s"] == pytest.approx(20.0, abs=0.05)
    # and the same at the end: the game's chain stops at 10 s, another shows on to the end
    cap = game([(10.0, 8.0, True), (30.0, 8.0, False)], focus=lambda t: 1 if t < 10.0 else 0)
    cap = drop_rows(cap, lambda x: x <= 10.0)
    later = [10.0 + k * 0.1 for k in range(1, 200)]
    for name in cap.frames:
        extra = {F.T: later, F.SWAP_CHAIN: ["0x1"] * len(later), F.DISPLAYED: [1] * len(later),
                 F.FRAME_MS: [100.0] * len(later), F.DISPLAY_MS: [100.0] * len(later)}.get(name, [None] * len(later))
        cap.frames[name] = list(cap.frames[name]) + list(extra)
    fr = fa.main_chain(cap)
    assert set(fr[F.SWAP_CHAIN]) == {"0x0"} and fr[fa.EDGES] == (), fr[fa.EDGES]


def test_hidden_spans_read_only_well_formed_edges():
    fr = {F.T: [], fa.HIDDEN: [], fa.FRONT: [], fa.EDGES: (("x", 2.0), (5.0, 4.0), (1.0,), (1.0, 2.0), None)}
    assert fa.hidden_spans(fr) == [(1.0, 2.0)]


def test_hidden_rows_need_display_tracking_and_a_foreground_reading():
    assert fa.hidden_rows([1.0, 2.0, 3.0], [None, None, None], [(1.5, 0), (2.0, 0), (2.5, 0)]) == ([], [])
    assert fa.hidden_rows([1.0, 2.0, 3.0], [1, 0, 1], [(1.5, None), (2.0, None)]) == ([], [])
    assert fa.hidden_rows([1.0, 2.0, 3.0], [1, 0, 1], []) == ([], [])
