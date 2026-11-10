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
    assert m["covered_s"] == pytest.approx(WINDOW, abs=0.1) and m["focused"] == 1.0
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


@pytest.mark.parametrize("knobs,why", [
    ({"frames_state": "failed"}, "frame timing was not recorded"),
    ({"duration": 50.0}, "the recording ended 30 s into"),
    ({"draws_until": 50.0}, "drew frames for only"),
    ({"focused": lambda t: 0 if 40 <= t < 50 else 1}, "behind another window for about 10 s"),
    ({"other_pct": 35.0}, "another program used 35 % of the game's GPU"),
])
def test_a_run_that_moved_does_not_count(knobs, why):
    cap = capture_at(60.0, **knobs)
    problems = results.problems(cap, results.window_metrics(cap, PRESS_T, WINDOW), PRESS_T, WINDOW)
    assert any(why in p for p in problems), problems


def test_too_few_frames_for_a_1_percent_low_does_not_count():
    cap = capture_at(15.0)                      # 900 frames in the window, under the 1,000 a 1 % low needs
    problems = results.problems(cap, results.window_metrics(cap, PRESS_T, WINDOW), PRESS_T, WINDOW)
    assert any("too few for a 1 % low" in p for p in problems)


def test_a_short_wobble_behind_another_window_still_counts():
    cap = capture_at(60.0, focused=lambda t: 0 if 40 <= t < 41 else 1)     # one second: under 5 %
    assert results.problems(cap, results.window_metrics(cap, PRESS_T, WINDOW), PRESS_T, WINDOW) == []


def _conditions(**knobs):
    cap = capture_at(60.0, **knobs)
    return results.conditions(cap, results.window_metrics(cap, PRESS_T, WINDOW))


def test_what_the_sweep_holds_fixed_is_compared_with_the_first_good_run():
    ref = _conditions()
    assert results.moved(None, ref) == [] and results.moved(ref, _conditions()) == []
    assert results.moved(ref, _conditions(power_limit_w=98.0)) == []                # within 10 %
    for knobs, why in (({"power_limit_w": 55.0}, "power limit changed from 103 W to 55 W"),
                       ({"plan_guid": "bbbb", "plan_name": "Silent"}, "from Turbo to Silent"),
                       ({"hz": 60.0}, "from 144 Hz to 60 Hz"),
                       ({"on_battery": True}, "on battery")):
        assert any(why in m for m in results.moved(ref, _conditions(**knobs))), knobs
    assert any("on battery" in m for m in results.moved(None, _conditions(on_battery=True)))


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


def test_a_setting_without_good_runs_ranks_last_and_says_why():
    s = _sweep_with({"A": [58.0, 58.4, 57.9], "B": [63.0, 63.4, 62.7], "C": [58.2, 58.9, 57.4]})
    s.runs = [r for r in s.runs if not (r.variant == "C" and r.slot in {x.n for x in s.slots if x.counted})]
    rows = results.ranking(s)
    assert rows[-1]["variant"] == "C" and rows[-1]["avg"] is None
    assert "needs 2 good runs of each" in "\n".join(results.text_lines(s))
