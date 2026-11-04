"""Statistics helpers, pinned at literal sample points."""
from __future__ import annotations

import math
import random

import pytest

from lagdoctor.analysis import stats


def test_percentile_matches_numpy_linear_method_at_literal_points():
    v = [10.0, 20.0, 30.0, 40.0]
    assert stats.percentile(v, 0) == 10.0
    assert stats.percentile(v, 100) == 40.0
    assert stats.percentile(v, 50) == 25.0
    assert stats.percentile(v, 25) == pytest.approx(17.5)    # numpy.percentile([10,20,30,40], 25) == 17.5
    assert stats.percentile([5.0], 99) == 5.0


def test_percentile_rejects_bad_input():
    with pytest.raises(ValueError):
        stats.percentile([], 50)
    with pytest.raises(ValueError):
        stats.percentile([1.0], 101)


def test_avg_fps_is_frames_over_time_not_mean_of_instant_fps():
    frames = [10.0, 30.0]                  # 2 frames in 40 ms
    assert stats.avg_fps(frames) == pytest.approx(50.0)
    naive = sum(1000.0 / f for f in frames) / len(frames)
    assert naive == pytest.approx(66.667, rel=1e-3)          # the wrong answer is clearly different


def test_low_fps_definitions_at_literal_points():
    frames = [10.0] * 99 + [50.0]          # one 50 ms stutter in 100 frames
    assert stats.low_fps_avg(frames, 1.0) == pytest.approx(20.0)     # slowest 1 frame: 1000/50
    assert stats.low_fps_avg(frames, 10.0) == pytest.approx(1000.0 / 14.0)  # (9*10+50)/10 = 14 ms
    # 99th percentile frame time = 10 + 0.01*(50-10) = 10.4 ms
    assert stats.low_fps_percentile(frames, 1.0) == pytest.approx(1000.0 / 10.4)


def test_low_fps_avg_uses_at_least_one_frame():
    assert stats.low_fps_avg([10.0, 20.0], 1.0) == pytest.approx(50.0)


def test_trailing_median_excludes_the_current_value():
    out = stats.trailing_median([10.0, 10.0, 10.0, 100.0, 10.0], window=3)
    assert out == [None, 10.0, 10.0, 10.0, 10.0]   # the 100 ms frame is compared with 10s, not itself


def test_trailing_median_window_slides():
    out = stats.trailing_median([1.0, 2.0, 3.0, 4.0, 5.0], window=2)
    assert out == [None, 1.0, 1.5, 2.5, 3.5]


def test_trailing_median_matches_brute_force_on_random_data():
    rng = random.Random(1234)
    vals = [rng.choice([5.0, 6.9, 16.6, 33.3, 120.0]) + rng.random() for _ in range(500)]
    for w in (1, 4, 31, 60):
        fast = stats.trailing_median(vals, w)
        for i in range(len(vals)):
            prev = vals[max(0, i - w):i]
            expect = stats.median(prev) if prev else None
            assert fast[i] == (pytest.approx(expect) if expect is not None else None), (w, i)


def test_mad_ignores_one_outlier():
    assert stats.mad([10.0, 10.0, 11.0, 9.0, 1000.0]) == 1.0


def test_clean_drops_none_and_nan():
    assert stats.clean([1.0, None, math.nan, 2]) == [1.0, 2.0]


def test_index_range_is_inclusive_of_both_ends():
    t = [0.0, 0.25, 0.5, 0.75, 1.0]
    assert stats.index_range(t, 0.25, 0.75) == (1, 4)
    assert stats.index_range(t, 2.0, 3.0) == (5, 5)
