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


def test_hidden_rows_need_display_tracking_and_a_foreground_reading():
    assert fa.hidden_rows([1.0, 2.0, 3.0], [None, None, None], [(1.5, 0), (2.0, 0), (2.5, 0)]) == ([], [])
    assert fa.hidden_rows([1.0, 2.0, 3.0], [1, 0, 1], [(1.5, None), (2.0, None)]) == ([], [])
    assert fa.hidden_rows([1.0, 2.0, 3.0], [1, 0, 1], []) == ([], [])
