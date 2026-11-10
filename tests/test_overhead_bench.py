"""The overhead benchmark's own logic (scripts/overhead): the order of attached and detached windows, the frame
figures, the paired summary and what it leaves out, the recorder's process tree, the collectors the recorder runs, and
the frame path's timing. The benchmark itself drives LagLab for minutes and is run by hand (docs/research/overhead.md,
section 8)."""
from __future__ import annotations

import os
import statistics
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from overhead import bench, pm_parse_bench, recorder  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "presentmon" / "tc5_pid24892.csv"


def starts_of(durations_ms: list[float], t0: float = 0.0) -> list[float]:
    """Frame start times (s) for frames lasting `durations_ms`, summed in whole microseconds so nothing drifts."""
    out, us = [t0], 0
    for d in durations_ms:
        us += round(d * 1000)
        out.append(t0 + us / 1e6)
    return out


def test_two_conditions_alternate_and_swap_order_every_cycle():
    assert [bench.order_for("AB", i) for i in range(4)] == ["AB", "BA", "AB", "BA"]


def test_three_conditions_rotate_so_each_takes_every_place():
    orders = [bench.order_for("APF", i) for i in range(3)]
    assert orders == ["APF", "PFA", "FAP"]
    assert all({o[place] for o in orders} == set("APF") for place in range(3))
    assert bench.order_for("APF", 3) == "APF"


def test_frame_figures_read_only_the_window_and_count_the_long_frames():
    d = [8.0] * 1500                  # 12 s of 8 ms frames
    d[100] = 30.0                     # about 0.8 s in: before the window
    d[500] = d[700] = 18.0            # 2.25 x the local median: stutters at 2 x, not at 4 x
    d[900] = 40.0                     # 5 x: a stutter at both
    m = bench.frame_metrics(starts_of(d), 2.0, 10.0)
    assert m["median_ms"] == pytest.approx(8.0)
    assert m["max_ms"] == pytest.approx(40.0)
    assert (m["over_10ms"], m["over_20ms"]) == (3, 1)
    assert m["stutter2x_per_min"] / m["stutter4x_per_min"] == pytest.approx(3.0)
    assert m["frames"] == pytest.approx(m["fps"] * 8.0, rel=0.01)


def test_a_window_with_too_few_frames_says_so_instead_of_giving_figures():
    assert bench.frame_metrics(starts_of([8.0] * 40), 0.0, 1.0) == {"frames": 40,
                                                                    "error": "too few frames in the window"}


def test_the_bootstrap_interval_brackets_the_median_and_reads_the_same_each_time():
    xs = [0.4, -0.2, 0.1, 0.3, -0.1, 0.2, 0.0, 0.5, -0.3, 0.6, 0.1]
    lo, hi = bench.boot_ci(xs)
    assert lo < statistics.median(xs) < hi
    assert bench.boot_ci(xs) == (lo, hi)


def test_the_recorders_tree_is_its_children_by_parent_and_start_time():
    p = lambda pid, ppid, created: SimpleNamespace(pid=pid, ppid=ppid, create_time=created)  # noqa: E731
    procs = {x.pid: x for x in (p(10, 4, 100), p(11, 10, 110), p(12, 11, 120),
                                p(13, 10, 90),     # older than PID 10's process: its parent was an earlier PID 10
                                p(14, 1, 130))}
    assert sorted(x.pid for x in bench.tree(procs, 10)) == [10, 11, 12]
    assert bench.tree(procs, 99) == []


def window(cycle: int, cond: str, mean_ms: float, **extra) -> dict:
    metrics = dict.fromkeys(bench.METRICS, 1.0)
    metrics["mean_ms"] = mean_ms
    rec = {"suite": "intel-cpu", "run": 0, "cycle": cycle, "cond": cond, "renderer": "test GL", "metrics": metrics,
           "acct": {"recorder_cpu_pct": 0.0 if cond == "A" else 5.0, "background_cpus": 1.0, "laglab_cpu_pct": 90.0}}
    if cond != "A":
        rec.update(recorder_exit=0, origin_lead_s=3.0)
    rec.update(extra)
    return rec


def test_the_summary_pairs_windows_by_cycle_and_leaves_out_a_recorder_that_did_not_record():
    recs = [window(0, "A", 8.0), window(0, "B", 8.08),
            window(1, "B", 8.16), window(1, "A", 8.0),
            window(2, "A", 8.0), window(2, "B", 80.0, origin_lead_s=0.5),     # its recording began 0.5 s before
            window(3, "B", 8.24), window(3, "A", 8.0),
            window(4, "A", 8.0), window(4, "B", 0.8, recorder_exit=3)]       # the recorder failed
    s = bench.summarize_cycles(recs)["intel-cpu"]
    assert s["excluded"] == [(0, 2, "B", "origin lead 0.5"), (0, 4, "B", "recorder exit 3")]
    assert (s["A"]["n"], s["B"]["n"]) == (5, 3)
    change = s["B_vs_A"]["mean_ms_change_pct"]
    assert s["B_vs_A"]["pairs"] == 3
    assert change["each"] == [1.0, 2.0, 3.0]          # in % of the A window of the same cycle
    assert change["median"] == 2.0
    assert s["A_vs_next_A_mean_ms_change_pct"]["each"] == [0.0, 0.0, 0.0, 0.0]


def test_a_suite_that_cannot_run_here_fails_the_command(monkeypatch, tmp_path, capsys):
    from lagdoctor import setup
    from lagdoctor.winapi import elevation

    def never(*a, **k):
        raise AssertionError("a suite that cannot run here was started")
    monkeypatch.setattr(bench, "run_cycles", never)
    monkeypatch.setattr(elevation, "can_trace", lambda: False)
    assert bench.main(["cycles", "--suite", "pm-intel-smooth", "--out", str(tmp_path)]) == 2
    assert "pm-intel-smooth: needs ETW rights" in capsys.readouterr().out
    monkeypatch.setattr(elevation, "can_trace", lambda: True)
    monkeypatch.setattr(setup, "expected_exe", lambda: tmp_path / "no-such-presentmon.exe")
    assert bench.main(["cycles", "--suite", "pm-nvidia-144", "--out", str(tmp_path)]) == 2
    assert "PresentMon not found" in capsys.readouterr().out
    assert not (tmp_path / "cycles.jsonl").exists()


def own_target() -> dict:
    from lagdoctor.winapi import ntquery
    me = ntquery.Snapshotter().snapshot()[os.getpid()]
    return {"pid": me.pid, "create_time": me.create_time, "adapter_luid": None}


def test_the_recorder_runs_lag_doctors_own_collectors():
    target = own_target()
    opts, cols = recorder.build_collectors(target, {"adapters": []}, frames=False, presentmon_exe=None,
                                           network=False, only="")
    assert [c.name for c in cols] == ["system"]
    assert (opts.hotkey, opts.duration_s, opts.frames) == (None, None, False)
    _, cols = recorder.build_collectors(target, {"adapters": []}, frames=True, presentmon_exe=None, network=False,
                                        only="frames")
    assert [c.name for c in cols] == ["frames"]


def test_the_recorder_refuses_a_collector_it_would_not_have():
    target = own_target()
    with pytest.raises(ValueError, match="no collector named sytem"):
        recorder.build_collectors(target, {"adapters": []}, frames=False, presentmon_exe=None, network=False,
                                  only="system,sytem")
    with pytest.raises(ValueError, match="no collector named frames"):
        recorder.build_collectors(target, {"adapters": []}, frames=False, presentmon_exe=None, network=False,
                                  only="frames")


def test_the_parse_timing_runs_lag_doctors_reader_over_every_row():
    lines = FIXTURE.read_bytes().splitlines(keepends=True)
    rows = 3 * (len(lines) - 1)
    r = pm_parse_bench.parse_cost(lines[0] + b"".join(lines[1:]) * 3, rows, read_mb=lambda: 0.0)
    assert r["rows_fed"] == r["rows_parsed"] == r["frames_stored"] == rows == 840
