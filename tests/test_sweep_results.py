"""A sweep run's numbers, the checks that keep a run out, and the ranking's statistics (lagdoctor/sweep/results.py)."""
from __future__ import annotations

import math
import random
import statistics

import pytest

from lagdoctor.sweep import plan, results
from lagdoctor.sweep.plan import Run
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
    ({"duration": 50.0}, "the recording ended 30 s into"),
    ({"draws_until": 50.0}, "the game drew no frame for the window's last 30 s"),
    ({"focused": lambda t: 0 if 40 <= t < 50 else 1}, "behind another window for about 10 s"),
    ({"focused": lambda t: 0 if 40 <= t < 41 else 1}, "behind another window for about 1 s"),
    ({"focused": None}, "whether the game was in front could not be read"),
    ({"other_pct": 35.0}, "another program used 10 % or more of the game's GPU for about 60 s"),
    ({"other_pct": lambda t: 35.0 if t < 45 else 0.0}, "for about 25 s of the window"),
    ({"clock_mhz": lambda t: 1700.0 if t < 50 else 780.0}, "moved between 780 and 1700 MHz"),
])
def test_a_run_that_moved_does_not_count(knobs, why):
    problems = _problems(capture_at(60.0, **knobs))
    assert any(why in p for p in problems), problems


def test_too_few_frames_for_a_1_percent_low_does_not_count():
    cap = capture_at(15.0)                      # 900 frames in the window, under the 1,000 a 1 % low needs
    assert any("too few for a 1 % low" in p for p in _problems(cap))


def test_slow_frames_behind_another_window_keep_the_run_out_as_the_1_percent_low_would_show():
    # A game behind another window may draw at 5 FPS: even 1 s of it ruins the 1 % low, so no such second counts.
    slow = dict(focused=lambda t: 0 if 40 <= t < 41 else 1, frame_ms=lambda t: 200.0 if 40 <= t < 41 else 1000 / 60)
    cap = capture_at(60.0, **slow)
    m = results.window_metrics(cap, PRESS_T, WINDOW)
    assert m["low1_fps"] < 30 and results.problems(cap, m, PRESS_T, WINDOW)


def test_a_sample_at_the_windows_very_edges_may_still_show_the_game_coming_to_the_front():
    cap = capture_at(60.0, focused=lambda t: 0 if t <= PRESS_T + 0.25 or t >= PRESS_T + WINDOW - 0.25 else 1)
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
    assert results.unchecked(m, results.conditions(cap, m)) == ["the GPU's clock (no NVIDIA readings)", "mains power"]


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
