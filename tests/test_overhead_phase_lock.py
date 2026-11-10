"""The phase-lock test in scripts/overhead, on synthetic series whose answer is known: a cost planted at the sampler's
moments is found and none planted reads as none; the null it compares against moves the events and nothing else; its
decoys keep away from the real period; a LagLab log is cut to the capture it belongs with; and the command reads a
saved capture through lag-doctor's own model. What it found on a real recording: docs/research/overhead.md, 4.2."""
from __future__ import annotations

import json
import math
import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from overhead import phase_lock as pl  # noqa: E402
from tests import synth  # noqa: E402

OPTS = {"shifts": 500, "exclude_ms": 30.0, "bin_ms": 10.0, "local_n": 31, "stutter_x": 2.0, "seed": "1",
        "workers": 1, "sources": ["system"]}


def series(seconds: float = 40.0, seed: int = 3) -> tuple[list[float], list[float], list[float]]:
    """A 60 FPS game with noise (frame ends and frame times) and 4 Hz sampler ticks with up to 3 ms of jitter."""
    rng = random.Random(seed)
    ms, ends, t = [], [], 1.0
    while t < seconds:
        d = 16.7 * math.exp(rng.gauss(0.0, 0.08))
        t += d / 1000.0
        ms.append(d)
        ends.append(t)
    phase = rng.uniform(0.0, 0.25)
    ticks = [1.0 + phase + 0.25 * k + rng.uniform(0.0, 0.003) for k in range(int((seconds - 1.0) / 0.25))]
    return ends, ms, [x for x in ticks if x < ends[-1]]


def test_a_cost_planted_at_the_sampler_ticks_is_found():
    ends, ms, ticks = series()
    changed = pl.plant(ends, ms, ticks, "system", 3.0, 1.0, "7")
    s = pl.analyze(ends, ms, [1] * len(ms), {"system": ticks}, OPTS)["sources"]["system"]
    assert changed == s["hit_frames"] >= pl.MIN_HIT_FRAMES
    assert s["verdict"] == "clear effect"
    assert s["a"]["p"] < pl.ALPHA / 3
    assert s["a"]["effect"] == pytest.approx(3.0, abs=0.25)


def test_the_same_series_with_no_cost_reads_as_none():
    ends, ms, ticks = series()
    s = pl.analyze(ends, ms, [1] * len(ms), {"system": ticks}, OPTS)["sources"]["system"]
    assert s["verdict"] == "no detectable effect"
    assert s["a"]["p"] > 0.05
    assert abs(s["a"]["effect"]) < 0.25


def test_a_shift_moves_every_event_and_wraps_the_ones_past_the_end():
    a = [0.1, 0.35, 0.6, 0.85]
    assert pl._shift(a, 0.0, 1.0, 0.3) == pytest.approx([0.15, 0.4, 0.65, 0.9])
    assert pl._shift(a, 0.0, 1.0, 0.0) == a


def test_the_schedule_fit_puts_held_up_rows_back_on_their_grid():
    rng = random.Random(5)
    late = {20, 60, 100, 140, 180}
    ts = [0.25 * (i + 1) + 0.004 + rng.uniform(-0.001, 0.001) + (0.040 if i in late else 0.0) for i in range(200)]
    grid, info = pl.fit_grid(ts, 0.25)
    assert info["rows_held_up_over_25_ms"] == 5
    assert all(abs(g - (0.25 * (i + 1) + 0.004)) < 0.002 for i, g in enumerate(grid))


def test_decoy_trains_keep_away_from_the_samplers_own_period():
    trains = pl.decoy_trains([0.25 * k for k in range(1, 200)], None, 0.25, 0.0, 50.0, 60, "s")
    assert len(trains) == 60
    for period, events in trains:
        assert pl.DECOY_RATIOS[0] - 1e-9 <= period / 0.25 <= pl.DECOY_RATIOS[1] + 1e-9
        assert abs(period / 0.25 - 1.0) >= pl.DECOY_GAP
        assert events == sorted(events) and all(0.0 <= t < 50.0 for t in events)


def laglab_log(tmp_path: Path, starts: list[float], hitches: list[dict] | None = None) -> Path:
    path = tmp_path / "laglab.json"
    path.write_text(json.dumps({"mode": "smooth", "frames": starts, "hitches": hitches or []}), encoding="utf-8")
    return path


def test_laglabs_planted_hitches_are_left_out_unless_kept(tmp_path):
    log = laglab_log(tmp_path, [1.0 + 0.010 * k for k in range(1001)], [{"start": 5.0, "end": 5.05}])
    ends, ms, use, info = pl.frames_from_laglab(log, 1.0, 10.0, keep_hitches=False)
    out = [e for e, u in zip(ends, use) if not u]
    assert info["laglab_planted_hitches"] == 1
    assert len(out) == info["frames_left_out_for_planted_hitches"] > 0
    assert 4.0 < min(out) and max(out) < 4.0 + 0.05 + pl.HITCH_MARGIN_S + 0.011    # the hitch, on the capture clock
    _, _, use_kept, info_kept = pl.frames_from_laglab(log, 1.0, 10.0, keep_hitches=True)
    assert all(use_kept) and info_kept["frames_left_out_for_planted_hitches"] == 0


def test_a_laglab_log_is_cut_to_the_capture_and_refused_when_it_misses_it(tmp_path):
    log = laglab_log(tmp_path, [100.0 + 0.010 * k for k in range(3001)])          # a 30 s run from t = 100 s
    ends, ms, use, info = pl.frames_from_laglab(log, 110.0, 10.0, keep_hitches=False)   # a capture of 10 s of it
    assert 0.0 <= ends[0] < 0.011 and 9.989 < ends[-1] <= 10.0
    assert len(ends) == len(ms) == len(use) == info["frames_inside_capture"]
    assert info["laglab_frames"] == 3001
    with pytest.raises(SystemExit, match="same run"):
        pl.frames_from_laglab(log, 500.0, 10.0, keep_hitches=False)                 # another run's clock
    with pytest.raises(SystemExit, match="same run"):
        pl.frames_from_laglab(log, 125.0, 20.0, keep_hitches=False)                 # covers 5 s of a 20 s capture


def test_the_command_reads_a_saved_capture_and_writes_its_aggregates(tmp_path, capsys):
    rng = random.Random(11)
    frame_ms = [16.7 * math.exp(rng.gauss(0.0, 0.08)) for _ in range(2400)]
    cap = synth.capture(synth.frames_table(frame_ms), synth.system_table(41.0), duration=41.0)
    path = cap.save(tmp_path / "x.ldcap.json.gz")
    out = tmp_path / "out.json"
    assert pl.main([str(path), "--sources", "system,system@grid", "--shifts", "500", "--workers", "1",
                    "--json", str(out)]) == 0
    res = json.loads(out.read_text(encoding="utf-8"))
    assert res["frames_used"] == 2400
    for name in ("system", "system@grid"):
        assert res["sources"][name]["events"] >= 155
        assert res["sources"][name]["verdict"] in ("no detectable effect", "small effect", "clear effect")
    assert "== system@grid:" in capsys.readouterr().out
