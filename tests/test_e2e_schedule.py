"""The end-to-end run's hog schedule against the doctor's own minimums, and the synthetic model that predicts it.

The doctor names background programs only from LIFT_MIN_EVENTS lag moments, and each hog burst makes one FPS drop,
which needs FPS_DROP_MIN_S below 80 % of the cap in a FPS_WINDOW_S rolling window: so the hog run must leave that many
whole bursts of that length inside its capture. Nothing at the point where the schedule is set shows this; before
2026-11-10 the run held 2 bursts, and no capture of it could ever have named the hogs. All three numbers are read from
the code that uses them: LagLab's defaults (laglab.app), the run's length (scripts/e2e.py) and the doctor's constants.
"""
from __future__ import annotations

import sys
from dataclasses import fields
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import e2e  # noqa: E402
from e2e_truth import judge  # noqa: E402

from laglab import app  # noqa: E402
from lagdoctor.analysis import attribute, events  # noqa: E402
from lagdoctor.analysis.diagnose import diagnose  # noqa: E402
from tests import synth  # noqa: E402


def hog_run() -> tuple[float, list[list[float]]]:
    """(LagLab seconds, its whole bursts on LagLab's clock from 0) for the e2e run's hog mode, from both defaults."""
    seconds = e2e.seconds_for("hog", e2e.parse([]).duration)
    lab = app._parse_args(["--mode", "hog", "--duration", repr(seconds)])
    return seconds, app.hog_bursts(0.0, lab.duration, lab.warmup, lab.hog_on, lab.hog_off)


def test_the_e2e_hog_run_holds_enough_whole_bursts_inside_its_capture():
    seconds, bursts = hog_run()
    need_s = events.FPS_DROP_MIN_S + events.FPS_WINDOW_S
    # a burst counts once the capture has run a whole rolling window before it, and only if it ends before LagLab
    inside = [b for b in bursts if b[0] - e2e.CAPTURE_DELAY_S >= events.FPS_WINDOW_S and b[1] <= seconds
              and b[1] - b[0] >= need_s]
    assert len(inside) >= attribute.LIFT_MIN_EVENTS, (seconds, bursts)
    assert e2e.CAPTURE_DELAY_S >= 2.7                  # no shorter than the slowest start the owner's run measured


def test_the_other_modes_keep_the_run_length_they_were_given():
    duration = e2e.parse([]).duration
    assert [m for m in e2e.ORDER if e2e.seconds_for(m, duration) != duration] == ["hog"]
    assert e2e.seconds_for("hog", 60.0) == 60.0        # a longer --duration is kept


@pytest.mark.parametrize("mode", ["hog", "hitch"])
def test_laglab_is_started_for_the_mode_s_own_length(mode, tmp_path):
    argv = e2e.laglab_argv(mode, e2e.parse([]).duration, tmp_path / "pid.json", tmp_path / "log.json")
    assert argv[argv.index("--mode") + 1] == mode
    seconds = float(argv[argv.index("--duration") + 1])
    assert seconds == e2e.seconds_for(mode, 25.0) and app._parse_args(argv[3:]).duration == seconds
    assert seconds == (40.0 if mode == "hog" else 25.0)


def test_the_models_hog_bursts_match_what_was_measured():
    # synth.HOG_STRETCH's fit (2026-11-10): 11.4 ms frames in the bursts against the 8.34 ms cap between them, and
    # 0 to 3 of a burst's ~340 frames over 20 ms; the old guess (2-3x, and a 15-50 ms wait on 10 % of frames) fails both
    # bounds below.
    s = synth.laglab("hog", seed=1)
    fr = s.log["frames"]
    gaps = [(a, 1000.0 * (b - a)) for a, b in zip(fr, fr[1:])]
    inside = sorted(ms for a, ms in gaps if any(x + 0.5 <= a < y for x, y in s.log["hog_bursts"]))
    between = sorted(ms for a, ms in gaps if not any(x - 0.5 <= a < y + 0.5 for x, y in s.log["hog_bursts"])
                     and a > s.log["t_start"] + 0.5)
    assert 10.8 <= inside[len(inside) // 2] <= 12.0, inside[len(inside) // 2]
    assert sum(1 for ms in inside if ms > 20.0) <= 0.01 * len(inside)
    assert between[len(between) // 2] == pytest.approx(1000.0 / 120.0, abs=0.1)


def test_the_synthetic_model_mirrors_laglabs_defaults():
    lab = app._parse_args([])
    names = {"duration_s": None, "warmup_s": "warmup", "fps_cap": "fps_cap", "target_ms": "target_ms",
             "hitch_ms": "hitch_ms", "every_s": "every", "io_reads": "io_reads", "hog_on_s": "hog_on",
             "hog_off_s": "hog_off", "hog_game_ms": "hog_game_ms", "pace_short_ms": "pace_short_ms",
             "pace_long_ms": "pace_long_ms"}
    knobs = {f.name: f.default for f in fields(synth.Knobs)}
    for knob, arg in names.items():
        if arg is not None:
            assert knobs[knob] == getattr(lab, arg), knob


@pytest.mark.parametrize("delay", [2.1, 2.7])
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_the_e2e_hog_run_passes_the_judge_on_the_contention_measured_on_this_laptop(seed, delay):
    # The e2e's capture starts 2.1-2.7 s after LagLab's first frame (synth.HOG_STRETCH has the measured contention):
    # the model's LagLab starts with its capture, so its warmup is what is left of LagLab's after that delay
    seconds, _ = hog_run()
    lab = app._parse_args(["--mode", "hog"])
    s = synth.laglab("hog", seed=seed, start_s=0.0, duration_s=seconds - delay, warmup_s=lab.warmup - delay)
    assert len(s.truth) == len(app.hog_bursts(0.0, seconds, lab.warmup, lab.hog_on, lab.hog_off))   # as many bursts
    d = diagnose(s.cap)
    v = judge("hog", s.log, s.cap.clock_origin, d, has_discrete_gpu=False, capture_s=s.cap.duration_s)
    assert v.ok, [(c.name, c.detail) for c in v.checks if not c.ok]
    assert sum(1 for e in d.events if e.kind in ("stutter", "fps_drop")) >= attribute.LIFT_MIN_EVENTS + 1
