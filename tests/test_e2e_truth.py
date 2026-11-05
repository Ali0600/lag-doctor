"""The end-to-end judge itself: it must fail a wrong diagnosis, not just pass a right one."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from e2e_truth import judge, match_events  # noqa: E402

from lagdoctor.model import Diagnosis, Finding, LagEvent  # noqa: E402

ORIGIN = 1000.0
LOG = {"gl_renderer": "Intel(R) UHD Graphics",
       "hitches": [{"start": ORIGIN + t, "end": ORIGIN + t + 0.15} for t in (3.0, 6.0, 9.0, 12.0)]}


def diag(events=(), findings=(), lag_type="stutter"):
    return Diagnosis(lag_type=lag_type, headline="x", events=list(events),
                     findings=[Finding(cause=c, title=c, category="stutter", confidence="high", score=1, summary="")
                               for c in findings])


def stutter(i, t, cause):
    return LagEvent(id=i, kind="stutter", t_start=t, t_end=t + 0.16, severity=160, causes=[cause])


def test_match_events_literal_points():
    truth = [(3.0, 3.15), (6.0, 6.15)]
    assert match_events(truth, [(3.05, 3.2), (6.0, 6.1)]) == (1.0, 1.0)
    assert match_events(truth, [(3.05, 3.2), (20.0, 20.1)]) == (0.5, 0.5)
    assert match_events(truth, []) == (0.0, 0.0)
    assert match_events([], []) == (1.0, 1.0)
    assert match_events([], [(1.0, 1.1)]) == (1.0, 0.0)


def test_a_correct_diagnosis_passes():
    d = diag([stutter(i, t, "game_cpu_spike") for i, t in enumerate((3.0, 6.0, 9.0, 12.0))], ["wrong_gpu"])
    v = judge("hitch", LOG, ORIGIN, d, has_discrete_gpu=True)
    assert v.ok, [c for c in v.checks if not c.ok]


def test_the_right_moments_with_the_wrong_cause_fail():
    d = diag([stutter(i, t, "asset_streaming") for i, t in enumerate((3.0, 6.0, 9.0, 12.0))], ["wrong_gpu"])
    v = judge("hitch", LOG, ORIGIN, d, has_discrete_gpu=True)
    assert not v.ok and [c.name for c in v.checks if not c.ok] == ["blamed game_cpu_spike"]


def test_missed_moments_and_invented_events_fail():
    d = diag([stutter(1, 3.0, "game_cpu_spike"), stutter(2, 20.0, "game_cpu_spike"),
              stutter(3, 25.0, "game_cpu_spike")], ["wrong_gpu"])
    failed = {c.name for c in judge("hitch", LOG, ORIGIN, d, has_discrete_gpu=True).checks if not c.ok}
    assert {"found the lag moments", "no invented lag"} <= failed


def test_wrong_gpu_is_required_only_when_laglab_ran_on_intel_with_a_discrete_gpu_present():
    d = diag([stutter(i, t, "game_cpu_spike") for i, t in enumerate((3.0, 6.0, 9.0, 12.0))])
    assert not judge("hitch", LOG, ORIGIN, d, has_discrete_gpu=True).ok
    assert judge("hitch", LOG, ORIGIN, d, has_discrete_gpu=False).ok
    nvidia_log = dict(LOG, gl_renderer="NVIDIA GeForce RTX 3070 Laptop GPU/PCIe/SSE2")
    assert judge("hitch", nvidia_log, ORIGIN, d, has_discrete_gpu=True).ok


def test_the_smooth_control_fails_on_any_stutter_or_limit_finding():
    log = {"gl_renderer": "NVIDIA"}
    assert judge("smooth", log, ORIGIN, diag(lag_type="none"), has_discrete_gpu=True).ok
    assert not judge("smooth", log, ORIGIN, diag([stutter(1, 5.0, "game_stall")], lag_type="none"),
                     has_discrete_gpu=True).ok
    assert not judge("smooth", log, ORIGIN, diag(findings=["gpu_limit"], lag_type="none"), has_discrete_gpu=True).ok


@pytest.mark.parametrize("lag_type", ["stutter", "pacing", "low_fps", "network", "mixed", "unknown"])
def test_the_smooth_control_fails_on_any_lag_verdict(lag_type):
    # no stutter event and no finding, but the verdict still names lag: the user would read "lag" (A2, A8)
    v = judge("smooth", {"gl_renderer": "NVIDIA"}, ORIGIN, diag(lag_type=lag_type), has_discrete_gpu=True)
    assert [c.name for c in v.checks if not c.ok] == ["no lag in the control"]


def test_a_verdict_with_no_checks_is_not_a_pass():
    from e2e_truth import Verdict
    assert not Verdict("smooth").ok
