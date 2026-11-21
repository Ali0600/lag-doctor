"""A sweep run's numbers, the checks that keep a run out, and the ranking's statistics (lagdoctor/sweep/results.py)."""
from __future__ import annotations

import math
import random
import statistics

import pytest

from lagdoctor.analysis import frames as fa
from lagdoctor.sweep import plan, results
from lagdoctor.sweep.plan import Run
from tests import synth
from tests.sweep_world import PRESS_T, capture_at

WINDOW = 60.0


# ------------------------------------------------------------ the t distribution --

@pytest.mark.parametrize("df,t", [(1, 12.7062), (2, 4.3027), (3, 3.1824), (4, 2.7764), (5, 2.5706), (10, 2.2281),
                                  (30, 2.0423), (1000, 1.9623)])
def test_the_95_percent_t_values_are_the_tables(df, t):
    assert results.t_quantile(0.975, df) == pytest.approx(t, abs=1e-4)


def _t_density(x: float, df: float) -> float:
    c = math.exp(math.lgamma((df + 1) / 2) - math.lgamma(df / 2)) / math.sqrt(df * math.pi)
    return c * (1 + x * x / df) ** (-(df + 1) / 2)


@pytest.mark.parametrize("df", [1.7, 2.5, 6.3])
def test_the_tail_matches_the_density_integrated_independently(df):
    # Welch's degrees of freedom are not whole numbers, which no table lists: integrate the density instead
    # (Simpson's rule from 0 to t), so the incomplete beta function is checked by another road.
    t = results.t_quantile(0.975, df)
    n = 20_000
    h = t / n
    area = _t_density(0, df) + _t_density(t, df) + sum((4 if i % 2 else 2) * _t_density(i * h, df)
                                                       for i in range(1, n))
    assert 0.5 + area * h / 3 == pytest.approx(0.975, abs=1e-6)


def test_the_t_quantile_refuses_what_it_cannot_answer():
    for p, df in ((0.5, 3), (1.0, 3), (0.975, 0), (float("nan"), 3)):
        with pytest.raises(ValueError):
            results.t_quantile(p, df)


def test_welch_matches_a_hand_computed_interval():
    a, b = [60.1, 60.5, 59.8], [58.0, 58.4, 57.9]
    w = results.welch(a, b)
    va, vb = statistics.variance(a) / 3, statistics.variance(b) / 3
    df = (va + vb) ** 2 / (va ** 2 / 2 + vb ** 2 / 2)
    assert w["df"] == pytest.approx(df)
    assert w["diff"] == pytest.approx(statistics.mean(a) - statistics.mean(b))
    half = results.t_quantile(0.975, df) * math.sqrt(va + vb)
    assert (w["lo"], w["hi"]) == pytest.approx((w["diff"] - half, w["diff"] + half))
    assert half > 1.96 * math.sqrt(va + vb) * 1.2          # the t interval, much wider than a normal one at n = 3


def test_a_comparison_says_more_less_or_no_measurable_difference():
    base = [58.0, 58.4, 57.9]
    assert results.compare([60.1, 60.5, 59.8], base)["verdict"] == "more"
    assert results.compare([55.0, 55.5, 55.2], base)["verdict"] == "less"
    same = results.compare([58.1, 58.9, 57.5], base)
    assert same["verdict"] == "same" and same["lo"] < 0 < same["hi"]
    assert results.compare([60.0], base) is None and results.compare([60.0, 61.0], [58.0]) is None
    flat = results.compare([60.0, 60.0], [58.0, 58.0])
    assert flat["verdict"] == "more" and flat["lo"] == flat["hi"] == pytest.approx(100 * 2 / 58)


def test_comparing_several_settings_widens_each_interval_so_they_hold_together():
    a, b = [60.1, 60.5, 59.8], [58.0, 58.4, 57.9]
    one, five = results.welch(a, b, 1), results.welch(a, b, 5)
    assert five["hi"] - five["lo"] > one["hi"] - one["lo"]
    half = results.t_quantile(1 - 0.05 / 10, five["df"]) * math.sqrt(statistics.variance(a) / 3
                                                                       + statistics.variance(b) / 3)
    assert (five["hi"] - five["lo"]) / 2 == pytest.approx(half)


def test_no_spread_at_all_is_said_as_every_run_not_as_an_interval():
    c = results.compare([72.0, 72.0], [60.0, 60.0])
    assert c["flat"] and results._verdict(c, "faster", "slower") == "+20.0 % in every run, faster"


def test_the_detectable_difference_follows_the_spread_and_the_runs():
    base = [58.0, 59.0, 60.0]
    see = results.detectable(base, 3)
    sd, m = statistics.stdev(base), statistics.mean(base)
    t = results.t_quantile(0.975, 4) + results.t_quantile(0.80, 4)
    assert see == pytest.approx(100 * t * sd * math.sqrt(2 / 3) / m)
    assert results.detectable(base, 10) < see and results.detectable([58.0], 3) is None


def test_an_interval_needs_two_runs():
    assert results.interval([60.0])["half"] is None and results.interval([]) is None
    two = results.interval([59.0, 61.0])
    assert two["half"] == pytest.approx(12.7062 * statistics.stdev([59.0, 61.0]) / math.sqrt(2), rel=1e-4)


# ---------------------------------------------------------------- one run --

def test_the_window_is_measured_from_the_start_key():
    cap = capture_at(60.0)
    m = results.window_metrics(cap, PRESS_T, WINDOW)
    assert m["avg_fps"] == pytest.approx(60.0, rel=1e-3) and m["low1_fps"] == pytest.approx(60.0, rel=1e-3)
    assert m["frames"] == pytest.approx(60 * WINDOW, abs=2)
    assert m["first_frame_s"] < 0.05 and m["last_gap_s"] == 0.0 and m["behind_s"] == 0.0 and m["focus_samples"] > 200
    assert (m["gpu_power_limit_w"], m["gpu_clock_mhz"], m["gpu_temp_c"]) == (103.0, 780.0, 87.0)
    assert results.problems(cap, m, PRESS_T, WINDOW) == []


def test_only_the_window_counts_not_the_startup_before_the_key():
    # Slow frames before the press (menus, loading) must not lower the window's frame rate.
    cap = capture_at(60.0)
    t = cap.frames["t"]
    slow = [i for i, x in enumerate(t) if x < PRESS_T - 1.0]
    for i in slow:
        cap.frames["display_ms"][i] = 200.0
    assert results.window_metrics(cap, PRESS_T, WINDOW)["avg_fps"] == pytest.approx(60.0, rel=1e-3)


def _problems(cap):
    return results.problems(cap, results.window_metrics(cap, PRESS_T, WINDOW), PRESS_T, WINDOW)


@pytest.mark.parametrize("knobs,why", [
    ({"frames_state": "failed"}, "frame timing was not recorded"),
    ({"duration": PRESS_T + 30}, "the recording ended 30 s into"),
    ({"draws_until": PRESS_T + 30}, "the game drew no frame for the window's last 30 s"),
    ({"focused": lambda t: 0 if PRESS_T + 20 <= t < PRESS_T + 30 else 1}, "behind another window for about 10 s"),
    ({"focused": lambda t: 0 if PRESS_T + 20 <= t < PRESS_T + 21 else 1}, "behind another window for about 1 s"),
    ({"focused": None}, "whether the game was in front could not be read"),
    ({"focused": lambda t: 0 if t < PRESS_T - 30 else 1}, "in front for only 30 s when you pressed the start key"),
    ({"other_pct": 35.0}, "another program used 10 % or more of the game's GPU for about 60 s"),
    ({"other_pct": lambda t: 35.0 if t < PRESS_T + 25 else 0.0}, "for about 25 s of the window"),
])
def test_a_run_that_moved_does_not_count(knobs, why):
    problems = _problems(capture_at(60.0, **knobs))
    assert any(why in p for p in problems), problems


def test_too_few_frames_for_a_1_percent_low_does_not_count():
    cap = capture_at(15.0)                      # 900 frames in the window, under the 1,000 a 1 % low needs
    assert any("too few for a 1 % low" in p for p in _problems(cap))


def test_slow_frames_behind_another_window_keep_the_run_out_as_the_1_percent_low_would_show():
    # A game behind another window may draw at 5 FPS: even 1 s of it ruins the 1 % low, so no such second counts.
    behind = lambda t: PRESS_T + 20 <= t < PRESS_T + 21
    slow = dict(focused=lambda t: 0 if behind(t) else 1, frame_ms=lambda t: 200.0 if behind(t) else 1000 / 60)
    cap = capture_at(60.0, **slow)
    m = results.window_metrics(cap, PRESS_T, WINDOW)
    assert m["low1_fps"] < 30 and results.problems(cap, m, PRESS_T, WINDOW)


def test_a_sample_at_the_windows_very_edges_may_still_show_the_game_coming_to_the_front():
    # In front for the 2 minutes before the press (the reading at the press itself is the window's), then one reading
    # behind just after the press and those of the window's last quarter second.
    cap = capture_at(60.0, focused=lambda t: 0 if PRESS_T <= t <= PRESS_T + 0.25 or t >= PRESS_T + WINDOW - 0.25 else 1)
    assert _problems(cap) == []


def test_a_freeze_across_the_windows_end_is_seen_and_one_before_it_counts_only_its_part_inside():
    # 1 s without a new frame, starting 0.5 s before the end (or the start) of the window.
    end = capture_at(60.0, frame_ms=lambda t: 1000.0 if PRESS_T + WINDOW - 0.6 < t < PRESS_T + WINDOW - 0.4
                     else 1000 / 60)
    m = results.window_metrics(end, PRESS_T, WINDOW)
    assert 400 <= m["worst_frame_ms"] <= 600 and m["avg_fps"] == pytest.approx(60, abs=1.0)
    start = capture_at(60.0, frame_ms=lambda t: 1000.0 if PRESS_T - 0.6 < t < PRESS_T - 0.4 else 1000 / 60)
    m = results.window_metrics(start, PRESS_T, WINDOW)
    assert 400 <= m["worst_frame_ms"] <= 600


FRAME_S = 1 / 60
END = PRESS_T + WINDOW


def _frozen(*, gap=None, unshown=None, focused=1, tracking=True):
    """capture_at's recording at 60 FPS with its frames made again: none presented inside `gap` (from, to), and those
    inside `unshown` presented but never shown, the next frame shown carrying the wait (MsBetweenDisplayChange, PM 2.4);
    no display tracking at all unless `tracking`. `focused`: the game in front (1) or behind another window (0)."""
    times, t = [], 1.0
    while t + FRAME_S <= END + 20.0:
        t += FRAME_S
        if gap is None or not gap[0] < t < gap[1]:
            times.append(t)
    frame_ms, display_ms, shown = [], [], []
    prev = last_shown = 1.0
    for x in times:
        hide = unshown is not None and unshown[0] < x < unshown[1]
        frame_ms.append((x - prev) * 1000)
        display_ms.append(0.0 if hide else (x - last_shown) * 1000)
        shown.append(0 if hide else 1)
        prev, last_shown = x, last_shown if hide else x
    cap = capture_at(60.0, focused=focused)
    cap.frames = synth.frames_table(frame_ms, t0=1.0, display_ms=display_ms if tracking else None,
                                    displayed=shown if tracking else None)
    return cap


def _window(cap, window=WINDOW):
    """window_metrics, and its problems, after checking every frozen screen the window holds lies inside it."""
    fronts = [iv for iv in results.clipped_frames(cap, PRESS_T, PRESS_T + window)[fa.FRONT] if iv is not None]
    assert all(PRESS_T <= a < b <= PRESS_T + window for a, b in fronts), fronts
    m = results.window_metrics(cap, PRESS_T, window)
    return m, results.problems(cap, m, PRESS_T, window)


PRESSED_TOO_SOON = "after you pressed the start key, as it came back to the front: wait a moment at your spot"
NOT_SETTLED = "when you pressed the start key: stay at your spot for 2 minutes before you press it"


def test_a_frozen_screen_as_the_game_comes_back_to_the_front_keeps_the_run_out_and_counts_from_the_press():
    # The sweep covers the game just before the press (Enter in its own window). Back in front, Windows may show
    # nothing new for a while (exclusive fullscreen can take a second or two): pressed then, the window would score the
    # way back from the sweep's window as the setting's own stutter, so the run is out, and the player told why. Its
    # numbers still count the part after the press: no frame before it in the window can judge it as a stall, so it
    # is the window's first frame (DECISIONS 2026-11-20). Pressed that soon, the game had not been in front long
    # enough either (DECISIONS 2026-11-21), and the player hears both.
    behind = lambda t: 0 if PRESS_T - 5 <= t < PRESS_T - 1 else 1
    for cap in (_frozen(gap=(PRESS_T - 5, PRESS_T + 0.8), focused=behind),
                _frozen(unshown=(PRESS_T - 5, PRESS_T + 0.8), focused=behind)):
        m, problems = _window(cap)
        assert 780 <= m["worst_frame_ms"] <= 820 and m["frozen_at_press_ms"] == pytest.approx(800, abs=20), m
        assert len(problems) == 2 and NOT_SETTLED in problems[0], problems
        assert "nothing new for 0.8 s " + PRESSED_TOO_SOON in problems[1], problems
    # Covered across the press itself, within the half second of grace: the time behind is no frame, the frozen
    # screen after it (from 0.5 s after the press, the first reading in front, to the frame shown 1.5 s after it) is.
    m, problems = _window(_frozen(unshown=(PRESS_T - 2, PRESS_T + 1.5),
                                  focused=lambda t: 0 if PRESS_T - 2 <= t < PRESS_T + 0.4 else 1))
    assert 980 <= m["worst_frame_ms"] <= 1020 and len(problems) == 2 and PRESSED_TOO_SOON in problems[1], problems
    # Frozen across the press, then behind from 0.5 s after it: its part from the press; the time behind keeps the run
    # out, and the game did not come back to the front then.
    m, problems = _window(_frozen(unshown=(PRESS_T - 0.5, PRESS_T + 2),
                                  focused=lambda t: 0 if PRESS_T + 0.6 <= t < PRESS_T + 2 else 1))
    assert 480 <= m["worst_frame_ms"] <= 520 and problems and m["frozen_at_press_ms"] == 0, (m, problems)
    # Frozen across the press with nothing else in front: the game's own, counted, and the run counts.
    m, problems = _window(_frozen(gap=(PRESS_T - 0.5, PRESS_T + 0.5)))
    assert 480 <= m["worst_frame_ms"] <= 540 and m["frozen_at_press_ms"] == 0 and problems == [], (m, problems)


def test_a_frozen_screen_across_the_windows_end_counts_only_its_part_before_it():
    # Frozen in front from 1 s before the window's end, then behind another window from 0.5 s after it.
    behind = lambda t: 0 if END + 0.5 <= t < END + 3 else 1
    for cap in (_frozen(gap=(END - 1, END + 3), focused=behind), _frozen(unshown=(END - 1, END + 3), focused=behind)):
        m, problems = _window(cap)
        assert 980 <= m["worst_frame_ms"] <= 1020 and problems == [], (m, problems)    # not to 0.25 s past, nor missed
    # Frozen in front from 1 s before the end, then behind from 0.1 s before it (the last half second's grace): to
    # 0.25 s before the end, the last reading in front, though the frame that ended it came after the window.
    m, problems = _window(_frozen(unshown=(END - 1, END + 3), focused=lambda t: 0 if END - 0.1 <= t < END + 3 else 1))
    assert 730 <= m["worst_frame_ms"] <= 770 and problems == [], (m, problems)
    # ... also when no frame was shown after it at all: PresentMon loses the last frame shown and the frames after it,
    # so the recording can end on frames never shown, and the last of them holds the frozen screen.
    m, problems = _window(_frozen(unshown=(END - 1, END + 21), focused=lambda t: 0 if t >= END - 0.1 else 1))
    assert 730 <= m["worst_frame_ms"] <= 770 and problems == [], (m, problems)


def test_time_behind_another_window_before_the_windows_end_is_never_a_frame_of_it():
    # Behind another window from 3.5 s to 0.25 s before the window's end, then frozen in front until the frame shown
    # 1 s after it. The time behind keeps the run out, and the window's frames never count it: only the frozen screen
    # from 0.25 s before the end to the end.
    m, problems = _window(_frozen(unshown=(END - 3.5, END + 1),
                                  focused=lambda t: 0 if END - 3.5 <= t < END - 0.4 else 1))
    assert 230 <= m["worst_frame_ms"] <= 270, m
    assert any("behind another window" in p for p in problems), problems
    # Behind from 2 s to 0.5 s before the end, then nothing shown until the recording ends: from the end of the time
    # behind.
    m, problems = _window(_frozen(unshown=(END - 2, END + 21), focused=lambda t: 0 if END - 2 <= t < END - 0.5 else 1))
    assert 480 <= m["worst_frame_ms"] <= 540 and problems, (m, problems)


def test_frames_not_shown_across_either_edge_count_only_their_part_inside():
    # Frames presented but none shown with the game in front: the screen froze. The frame shown after it carries the
    # whole wait, and the frame across the window's end is the time since the last one shown.
    assert 480 <= _window(_frozen(unshown=(PRESS_T - 0.5, PRESS_T + 0.5)))[0]["worst_frame_ms"] <= 540   # from press
    assert 980 <= _window(_frozen(unshown=(END - 1, END + 0.5)))[0]["worst_frame_ms"] <= 1020    # to the end, not missed
    # The first frame shown 40 ms after the press, whose screen changed only 20 ms before it: its own 20 ms.
    cap = _frozen(gap=(PRESS_T - 0.03, PRESS_T + 0.04))
    first = next(i for i, t in enumerate(cap.frames["t"]) if t > PRESS_T)
    cap.frames["display_ms"][first] = 20.0
    assert _window(cap)[0]["worst_frame_ms"] == pytest.approx(20.0, abs=0.01)


def test_a_frozen_screen_outside_the_window_is_none_of_it_and_one_inside_is_judged_as_the_report_judges_it():
    # Frozen from 3 s to 2.25 s before the press, then behind until the frame 0.6 s after it, the window's first; and
    # behind from 1 s before the window's end to 1.5 s after it, then frozen until the frame 3 s after it, the one after
    # the window. Each sits on a frame the window keeps.
    before = lambda t: 0 if PRESS_T - 2 <= t < PRESS_T + 0.6 else 1
    for cap in (_frozen(gap=(PRESS_T - 3, PRESS_T + 0.6), focused=before),
                _frozen(gap=(END - 1, END + 3), focused=lambda t: 0 if END - 1 <= t < END + 1.5 else 1)):
        assert len([iv for iv in fa.main_chain(cap)[fa.FRONT] if iv]) == 1      # the recording holds it
        m, _ = _window(cap)
        assert m["worst_frame_ms"] == pytest.approx(1000 / 60, abs=0.5) and m["hitches"] == 0, m
    # Frozen 22 s to 23 s into the window after a cover from 20 s, frames before it in the window: a stall, as in the
    # report.
    cap = _frozen(unshown=(PRESS_T + 20, PRESS_T + 23), focused=lambda t: 0 if PRESS_T + 20 <= t < PRESS_T + 22 else 1)
    whole, fr = fa.main_chain(cap), results.clipped_frames(cap, PRESS_T, PRESS_T + WINDOW)
    kept = [i for i, iv in enumerate(fr[fa.FRONT]) if iv]
    assert len(kept) == 1 and fr[fa.HIDDEN][kept[0]] is True, kept
    assert fr[fa.FRONT][kept[0]] in [iv for iv in whole[fa.FRONT] if iv]


def test_a_recording_without_display_tracking_is_judged_by_its_presents():
    # Without display tracking the window is judged by the time between presents, as the report judges it: the frame
    # across its end never makes up a display time of its own (a series of one frame, which keeps every run out).
    m, problems = _window(_frozen(tracking=False), WINDOW + 0.008)
    assert m["frames"] >= 3600 and m["avg_fps"] == pytest.approx(60.0, rel=1e-3) and problems == [], (m, problems)
    # A freeze across the press counts only its part after it there too, and one across the end its part before it.
    assert 480 <= _window(_frozen(gap=(PRESS_T - 0.5, PRESS_T + 0.5), tracking=False))[0]["worst_frame_ms"] <= 540
    assert 480 <= _window(_frozen(gap=(END - 0.5, END + 0.5), tracking=False))[0]["worst_frame_ms"] <= 540


def test_the_game_must_have_been_in_front_two_minutes_before_the_press():
    # The GPU settles to its heat limit within about 2 minutes of play (the owner's recordings, DECISIONS
    # 2026-11-21), so the key counts only after the game has been in front that long, as the sweep's own samples read
    # it: counted back from the press to the last one with the game behind, or to the first one readable.
    def front(focused, t0=PRESS_T):
        cap = capture_at(60.0, focused=focused)
        m = results.window_metrics(cap, t0, WINDOW)
        return m["front_before_s"], results.problems(cap, m, t0, WINDOW)

    for focused, seconds in ((lambda t: 0 if t < PRESS_T - 121 else 1, 121.0),
                             (lambda t: 0 if t < PRESS_T - 119 else 1, 119.0),
                             (lambda t: 0 if PRESS_T - 60 <= t < PRESS_T - 59 else 1, 59.0),      # a cover restarts it
                             (lambda t: 0 if PRESS_T + 20 <= t < PRESS_T + 21 else 1, PRESS_T - 0.25),   # one after: no
                             (lambda t: None if t < PRESS_T - 30 else 1, 30.0),     # unreadable is not in front
                             (lambda t: 0 if t >= PRESS_T - 0.25 else 1, 0.0)):     # behind as the key was pressed
        got, problems = front(focused)
        assert got == pytest.approx(seconds, abs=0.25), (seconds, got)
        assert any(NOT_SETTLED in p for p in problems) == (seconds < 120), (seconds, problems)
    got, problems = front(1, t0=60.0)                       # in front all along, but the recording began 60 s before
    assert got == pytest.approx(60.0, abs=0.25) and any("in front for only 60 s" in p for p in problems), problems
    assert any("in front for under a second when you pressed" in p for p in front(lambda t: 0 if t >= PRESS_T - 0.25
                                                                                     else 1)[1])


def test_a_clock_swinging_at_the_heat_limit_or_falling_within_the_window_is_recorded_not_held():
    # At its heat limit the owner's GPU swings 780 to 1,000 MHz at a flat temperature and never settles further; a
    # rule on the clock's spread kept 53 of 55 such minutes out. The clock is recorded with each run.
    for clock in (lambda t: 780.0 if int(t) % 10 < 5 else 1000.0, lambda t: 1700.0 if t < PRESS_T + 30 else 780.0):
        cap = capture_at(60.0, clock_mhz=clock)
        m = results.window_metrics(cap, PRESS_T, WINDOW)
        assert results.problems(cap, m, PRESS_T, WINDOW) == [] and m["gpu_clock_p10"] == 780.0, m
        assert m["gpu_clock_p90"] in (1000.0, 1700.0)


def _conditions(**knobs):
    cap = capture_at(60.0, **knobs)
    return results.conditions(cap, results.window_metrics(cap, PRESS_T, WINDOW))


def test_what_the_sweep_holds_fixed_is_compared_with_the_first_good_run():
    ref = _conditions()
    assert results.moved(None, ref) == [] and results.moved(ref, _conditions()) == []
    # The GPU's power limit moves by itself (Dynamic Boost): recorded, not held.
    assert results.moved(ref, _conditions(power_limit_w=55.0)) == [] and _conditions(power_limit_w=55.0)[
        "gpu_power_limit_w"] == 55.0
    for knobs, why in (({"plan_guid": "bbbb", "plan_name": "Silent"}, "from Turbo to Silent"),
                       ({"hz": 60.0}, "from 144 Hz to 60 Hz"),
                       ({"on_battery": True}, "on battery")):
        assert any(why in m for m in results.moved(ref, _conditions(**knobs))), knobs
    assert any("on battery" in m for m in results.moved(None, _conditions(on_battery=True)))
    best, quiet = dict(ref, power_mode="best_performance"), dict(ref, power_mode="balanced")
    assert results.moved(best, quiet) == ["Windows' power mode changed from best performance to balanced"]


def test_what_could_not_be_read_is_listed_as_not_checked():
    cap = capture_at(60.0, nvidia=False, on_battery=None)
    m = results.window_metrics(cap, PRESS_T, WINDOW)
    assert results.problems(cap, m, PRESS_T, WINDOW) == []
    # The NVIDIA clock is only recorded, so no NVIDIA readings leave nothing unchecked.
    assert results.unchecked(m, results.conditions(cap, m)) == ["mains power"] and m["gpu_clock_p10"] is None


def test_an_updated_game_is_told_apart():
    ref = _conditions()
    assert results.game_changed(ref, _conditions()) is None and results.game_changed(None, ref) is None
    assert "updated" in results.game_changed(ref, _conditions(exe_bytes=82_000_000))


# ---------------------------------------------------------------- ranking --

def _sweep_with(fps: dict[str, list[float]]) -> plan.Sweep:
    text = "[S]\r\nsg.X=3\r\n"
    resolved, original = plan.resolve(text, [plan.parse_try("sg.X=1"), plan.parse_try("sg.X=2")])
    variants = plan.make_variants(resolved, original)
    slots = plan.make_slots([v.id for v in variants], 3, True, random.Random(3))
    s = plan.Sweep(game="Game.exe", settings_file=r"C:\x\a.ini", window_s=WINDOW, runs_per_variant=3, warm_up=True,
                   start_key="ctrl+alt+s", seed=3, variants=variants, original=original, slots=slots)
    for slot in slots:
        if not slot.counted:
            s.runs.append(Run(slot.n, slot.variant, "good", metrics={"avg_fps": 10.0, "low1_fps": 5.0}))
            continue
        values = fps[slot.variant]
        avg = values.pop(0)
        s.runs.append(Run(slot.n, slot.variant, "good", metrics={"avg_fps": avg, "low1_fps": avg - 12.0}))
    return s


def test_the_ranking_puts_the_fastest_first_and_compares_each_with_the_file_as_it_was():
    s = _sweep_with({"A": [58.0, 58.4, 57.9], "B": [63.0, 63.4, 62.7], "C": [58.2, 58.9, 57.4]})
    rows = results.ranking(s)
    assert [r["variant"] for r in rows] == ["B", "C", "A"]
    by = {r["variant"]: r for r in rows}
    assert by["A"]["vs"] is None and by["A"]["avg"]["n"] == 3
    assert by["B"]["vs"]["avg"]["verdict"] == "more" and by["C"]["vs"]["avg"]["verdict"] == "same"
    assert by["A"]["avg"]["mean"] == pytest.approx(statistics.mean([58.0, 58.4, 57.9]))  # warm-up's 10 FPS left out
    lines = results.text_lines(s)
    text = "\n".join(lines)
    assert text.isascii()
    assert "faster" in text and "no measurable difference" in text and "Picture quality is yours" in text
    assert lines[0].startswith("Results, in order of average FPS") and "the 2 comparisons hold together" in text
    assert "this sweep sees a difference of about" in text


def test_conditions_not_checked_in_good_runs_are_said_with_the_results():
    s = _sweep_with({"A": [58.0, 58.4, 57.9], "B": [63.0, 63.4, 62.7], "C": [58.2, 58.9, 57.4]})
    s.runs[-1].conditions = {"unchecked": "the GPU's clock (no NVIDIA readings); mains power"}
    text = "\n".join(results.text_lines(s))
    assert "Not checked in some runs (lag-doctor could not read them): mains power, the GPU's clock" in text


def test_a_setting_without_good_runs_ranks_last_and_says_why():
    s = _sweep_with({"A": [58.0, 58.4, 57.9], "B": [63.0, 63.4, 62.7], "C": [58.2, 58.9, 57.4]})
    s.runs = [r for r in s.runs if not (r.variant == "C" and r.slot in {x.n for x in s.slots if x.counted})]
    rows = results.ranking(s)
    assert rows[-1]["variant"] == "C" and rows[-1]["avg"] is None
    assert "needs 2 good runs of each" in "\n".join(results.text_lines(s))
