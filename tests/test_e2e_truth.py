"""The end-to-end judge itself: it must fail a wrong diagnosis, not just pass a right one."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from e2e_truth import FRONT, judge, match_events, time_away  # noqa: E402

from lagdoctor.model import Diagnosis, Finding, LagEvent  # noqa: E402

ORIGIN = 1000.0
CAPTURE_S = 23.4
IN_FRONT = [[ORIGIN - 2.0, True]]          # LagLab's window took the focus at its first frame, before the capture
LOG = {"gl_renderer": "Intel(R) UHD Graphics", "front": IN_FRONT,
       "hitches": [{"start": ORIGIN + t, "end": ORIGIN + t + 0.15} for t in (3.0, 6.0, 9.0, 12.0)]}


def diag(events=(), findings=(), lag_type="stutter"):
    return Diagnosis(lag_type=lag_type, headline="x", events=list(events),
                     findings=[Finding(cause=c, title=c, category="stutter", confidence="high", score=1, summary="")
                               for c in findings])


def stutter(i, t, cause):
    return LagEvent(id=i, kind="stutter", t_start=t, t_end=t + 0.16, severity=160, causes=[cause])


def hitches_found():
    return diag([stutter(i, t, "game_cpu_spike") for i, t in enumerate((3.0, 6.0, 9.0, 12.0))], ["wrong_gpu"])


def test_match_events_literal_points():
    truth = [(3.0, 3.15), (6.0, 6.15)]
    assert match_events(truth, [(3.05, 3.2), (6.0, 6.1)]) == (1.0, 1.0)
    assert match_events(truth, [(3.05, 3.2), (20.0, 20.1)]) == (0.5, 0.5)
    assert match_events(truth, []) == (0.0, 0.0)
    assert match_events([], []) == (1.0, 1.0)
    assert match_events([], [(1.0, 1.1)]) == (1.0, 0.0)


def test_a_correct_diagnosis_passes():
    v = judge("hitch", LOG, ORIGIN, hitches_found(), has_discrete_gpu=True, capture_s=CAPTURE_S)
    assert v.ok, [c for c in v.checks if not c.ok]


def test_the_right_moments_with_the_wrong_cause_fail():
    d = diag([stutter(i, t, "asset_streaming") for i, t in enumerate((3.0, 6.0, 9.0, 12.0))], ["wrong_gpu"])
    v = judge("hitch", LOG, ORIGIN, d, has_discrete_gpu=True, capture_s=CAPTURE_S)
    assert not v.ok and [c.name for c in v.checks if not c.ok] == ["blamed game_cpu_spike"]


def test_missed_moments_and_invented_events_fail():
    d = diag([stutter(1, 3.0, "game_cpu_spike"), stutter(2, 20.0, "game_cpu_spike"),
              stutter(3, 25.0, "game_cpu_spike")], ["wrong_gpu"])
    failed = {c.name for c in judge("hitch", LOG, ORIGIN, d, has_discrete_gpu=True, capture_s=CAPTURE_S).checks
              if not c.ok}
    assert {"found the lag moments", "no invented lag"} <= failed


def test_wrong_gpu_is_required_only_when_laglab_ran_on_intel_with_a_discrete_gpu_present():
    d = diag([stutter(i, t, "game_cpu_spike") for i, t in enumerate((3.0, 6.0, 9.0, 12.0))])
    assert not judge("hitch", LOG, ORIGIN, d, has_discrete_gpu=True, capture_s=CAPTURE_S).ok
    assert judge("hitch", LOG, ORIGIN, d, has_discrete_gpu=False, capture_s=CAPTURE_S).ok
    nvidia_log = dict(LOG, gl_renderer="NVIDIA GeForce RTX 3070 Laptop GPU/PCIe/SSE2")
    assert judge("hitch", nvidia_log, ORIGIN, d, has_discrete_gpu=True, capture_s=CAPTURE_S).ok


def test_the_smooth_control_fails_on_any_stutter_or_limit_finding():
    log = {"gl_renderer": "NVIDIA", "front": IN_FRONT}
    assert judge("smooth", log, ORIGIN, diag(lag_type="none"), has_discrete_gpu=True, capture_s=CAPTURE_S).ok
    assert not judge("smooth", log, ORIGIN, diag([stutter(1, 5.0, "game_stall")], lag_type="none"),
                     has_discrete_gpu=True, capture_s=CAPTURE_S).ok
    assert not judge("smooth", log, ORIGIN, diag(findings=["gpu_limit"], lag_type="none"), has_discrete_gpu=True,
                     capture_s=CAPTURE_S).ok


@pytest.mark.parametrize("lag_type", ["stutter", "pacing", "low_fps", "network", "mixed", "unknown"])
def test_the_smooth_control_fails_on_any_lag_verdict(lag_type):
    # no stutter event and no finding, but the verdict still names lag: the user would read "lag" (A2, A8)
    v = judge("smooth", {"gl_renderer": "NVIDIA", "front": IN_FRONT}, ORIGIN, diag(lag_type=lag_type),
              has_discrete_gpu=True, capture_s=CAPTURE_S)
    assert [c.name for c in v.checks if not c.ok] == ["no lag in the control"]


def test_a_verdict_with_no_checks_is_not_a_pass():
    from e2e_truth import Verdict
    assert not Verdict("smooth").ok


# ---------------------------------------------------------------------------- the window in front ---

@pytest.mark.parametrize("front, away, first", [
    (IN_FRONT, 0.0, None),                                              # in front for the whole capture
    ([[ORIGIN - 2.0, True], [ORIGIN + 5.3, False]], CAPTURE_S - 5.3, ORIGIN + 5.3),   # the owner's smooth run
    ([[ORIGIN - 2.0, False]], CAPTURE_S, ORIGIN),                       # never had the focus (hitch, hog)
    ([[ORIGIN - 2.0, True], [ORIGIN + 5.0, False], [ORIGIN + 7.0, True]], 2.0, ORIGIN + 5.0),   # and came back
    ([[ORIGIN - 2.0, True], [ORIGIN + 17.0, False], [ORIGIN + 18.0, True], [ORIGIN + 20.0, False]],
     1.0 + CAPTURE_S - 20.0, ORIGIN + 17.0),                            # two stretches away
    ([[ORIGIN - 5.0, False], [ORIGIN - 1.0, True]], 0.0, None),         # away only before the capture began
    ([[ORIGIN - 2.0, True], [ORIGIN + 30.0, False]], 0.0, None),        # and only after it ended
    ([[ORIGIN + 1.0, True]], 0.0, None),                                # before LagLab's first frame: no window yet
    ([[ORIGIN + 1.0, False]], CAPTURE_S - 1.0, ORIGIN + 1.0),           # its first frame was already not in front
    ([[ORIGIN + 5.3, False], [ORIGIN - 2.0, True]], CAPTURE_S - 5.3, ORIGIN + 5.3),   # entries out of order
])
def test_time_away_literal_points(front, away, first):
    got = time_away(front, ORIGIN, ORIGIN + CAPTURE_S)
    assert got[0] == pytest.approx(away, abs=1e-9) and got[1] == first


@pytest.mark.parametrize("front", [None, [], "yes", {}, [[ORIGIN, "yes"]], [[float("nan"), True]], [[True, True]],
                                   [[ORIGIN - 2.0, True, 1]], [[ORIGIN - 2.0, 1]], [IN_FRONT[0], None]])
def test_time_away_counts_a_log_it_cannot_read_as_away(front):
    assert time_away(front, ORIGIN, ORIGIN + CAPTURE_S) == (pytest.approx(CAPTURE_S), ORIGIN)


def test_a_run_whose_window_lost_the_focus_is_not_judged_a_pass():
    log = dict(LOG, front=[[ORIGIN - 2.0, True], [ORIGIN + 5.3, False]])
    v = judge("hitch", log, ORIGIN, hitches_found(), has_discrete_gpu=True, capture_s=CAPTURE_S)
    assert not v.ok and [c.name for c in v.checks if not c.ok] == [FRONT]
    assert v.checks[0].name == FRONT                # read first: it says whether the run can be judged at all
    assert "not in front for 18.1 of 23.4 s" in v.checks[0].detail and "from 5.3 s on" in v.checks[0].detail


@pytest.mark.parametrize("mode", ["smooth", "gpu", "cpu", "hitch", "hitch-io", "hitch-wait", "hog", "pace"])
def test_every_mode_checks_the_window_first(mode):
    v = judge(mode, dict(LOG, front=[[ORIGIN - 2.0, False]]), ORIGIN, diag(), has_discrete_gpu=False,
              capture_s=CAPTURE_S)
    assert v.checks[0].name == FRONT and not v.checks[0].ok and not v.ok


def test_a_log_without_the_window_state_fails_closed():
    log = {k: v for k, v in LOG.items() if k != "front"}
    v = judge("hitch", log, ORIGIN, hitches_found(), has_discrete_gpu=True, capture_s=CAPTURE_S)
    assert not v.ok and v.checks[0].detail == "LagLab's log does not say whether its window was in front"
    for empty in ([], None):
        v = judge("hitch", dict(LOG, front=empty), ORIGIN, hitches_found(), has_discrete_gpu=True, capture_s=CAPTURE_S)
        assert not v.ok and "empty, so whether it was in front is unknown" in v.checks[0].detail


@pytest.mark.parametrize("capture_s", [0.0, -1.0, float("nan"), float("inf"), True])
def test_a_capture_without_a_length_fails_the_front_check(capture_s):
    v = judge("hitch", LOG, ORIGIN, hitches_found(), has_discrete_gpu=True, capture_s=capture_s)
    assert not v.checks[0].ok and "no length" in v.checks[0].detail
