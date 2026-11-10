"""The overhead benchmark's own logic (scripts/overhead): the order of attached and detached windows, the frame
figures and the windows they refuse, the paired summary and what it leaves out, the command's checks before it starts
and its exit codes, the recorder's process tree, the collectors the recorder runs, and the frame path's timing. The
benchmark itself drives LagLab for minutes and is run by hand (docs/research/overhead.md, section 8)."""
from __future__ import annotations

import json
import os
import statistics
import sys
import tracemalloc
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from overhead import bench, pm_parse_bench, recorder  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "presentmon" / "tc5_pid24892.csv"
NOT_COVERED = {"error": "the window is not inside LagLab's frames"}


def starts_of(durations_ms: list[float], t0: float = 0.0) -> list[float]:
    """Frame start times (s) for frames lasting `durations_ms`, summed in whole microseconds so nothing drifts."""
    out, us = [t0], 0
    for d in durations_ms:
        us += round(d * 1000)
        out.append(t0 + us / 1e6)
    return out


def window(cycle: int, cond: str, mean_ms: float, **extra) -> dict:
    """One window as `cycles` writes it: run 0 of intel-cpu unless `extra` says otherwise."""
    metrics = dict.fromkeys(bench.METRICS, 1.0)
    metrics["mean_ms"] = mean_ms
    rec = {"suite": "intel-cpu", "run": 0, "cycle": cycle, "cond": cond, "renderer": "test GL", "metrics": metrics,
           "acct": {"recorder_cpu_pct": 0.0 if cond == "A" else 5.0, "background_cpus": 1.0, "laglab_cpu_pct": 90.0}}
    if cond != "A":
        rec.update(recorder_exit=0, origin_lead_s=3.0)
    rec.update(extra)
    return rec


@pytest.fixture
def stub_runs(monkeypatch):
    """`cycles` with LagLab left out: run_cycles records each call and returns what the test queued (else one good
    pair), and the capture lock is a stand-in, since the real one is a named mutex for the whole Windows session."""
    from lagdoctor.capture import session
    state = SimpleNamespace(calls=[], queued=[], free=True, released=0)

    class Lock:
        def acquire(self) -> bool:
            return state.free

        def release(self) -> None:
            state.released += 1

    def run_cycles(suite, idx, *args):
        state.calls.append((suite, idx))
        if state.queued:
            return state.queued.pop(0)
        return [window(0, "A", 8.0, suite=suite, run=idx), window(0, "B", 8.08, suite=suite, run=idx)]

    monkeypatch.setattr(session, "CaptureLock", Lock)
    monkeypatch.setattr(bench, "run_cycles", run_cycles)
    return state


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


def test_a_window_that_laglab_did_not_cover_from_end_to_end_gets_no_figures():
    starts = starts_of([8.0] * 1000)                      # LagLab drew from 0 to 8 s
    assert "error" not in bench.window_metrics(starts, 1.0, 7.0)
    assert bench.window_metrics(starts, 7.0, 9.0) == NOT_COVERED     # LagLab stopped inside it
    assert bench.window_metrics(starts, -1.0, 1.0) == NOT_COVERED    # it began before the first frame
    assert bench.window_metrics([], 0.0, 1.0) == NOT_COVERED


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


def test_the_summary_pairs_windows_within_each_run_and_leaves_out_a_recorder_that_did_not_record():
    recs = [window(0, "A", 8.0), window(0, "B", 8.08),
            window(1, "B", 8.16), window(1, "A", 8.0),
            window(2, "A", 8.0), window(2, "B", 80.0, origin_lead_s=0.5),     # its recording began 0.5 s before
            window(3, "B", 8.24), window(3, "A", 8.0),
            window(4, "A", 8.0), window(4, "B", 0.8, recorder_exit=3),       # the recorder failed
            window(0, "A", 10.0, run=1), window(0, "B", 10.4, run=1),        # a second LagLab run, its own pace
            window(1, "B", 10.5, run=1), window(1, "A", 10.0, run=1)]
    s = bench.summarize_cycles(recs)["intel-cpu"]
    assert s["excluded"] == [(0, 2, "B", "origin lead 0.5"), (0, 4, "B", "recorder exit 3")]
    assert (s["A"]["n"], s["B"]["n"]) == (7, 5)
    change = s["B_vs_A"]["mean_ms_change_pct"]
    assert s["B_vs_A"]["pairs"] == 5
    assert change["each"] == [1.0, 2.0, 3.0, 4.0, 5.0]       # in % of the A window of the same run and cycle
    assert change["median"] == 3.0
    # The noise floor compares consecutive A windows of one run: never run 0's last with run 1's first (+25 %).
    assert s["A_vs_next_A_mean_ms_change_pct"]["each"] == [0.0, 0.0, 0.0, 0.0, 0.0]


def test_a_presentmon_window_without_frame_timing_is_left_out():
    def pm(cycle, cond, mean_ms, state="ok", rows=900):
        return window(cycle, cond, mean_ms, suite="pm-intel-smooth",
                      recorder={"sources": {"frames": [state, ""]}, "presentmon": {"rows": rows}})
    recs = []
    for k in range(3):
        bad = k == 1                  # cycle 1: PresentMon never ran in P, and printed no row in F
        recs += [window(k, "A", 8.0, suite="pm-intel-smooth"),
                 pm(k, "P", 8.08, state="unavailable" if bad else "ok", rows=0 if bad else 900),
                 pm(k, "F", 8.16, rows=0 if bad else 900)]
    s = bench.summarize_cycles(recs)["pm-intel-smooth"]
    assert s["excluded"] == [(0, 1, "P", "no frame timing (frames unavailable, 0 rows)"),
                             (0, 1, "F", "no frame timing (frames ok, 0 rows)")]
    assert s["P_vs_A"]["pairs"] == s["F_vs_A"]["pairs"] == 2


def test_summarize_fails_when_a_condition_has_fewer_than_two_pairs(tmp_path, capsys):
    log = tmp_path / "cycles.jsonl"
    log.write_text("".join(json.dumps(w) + "\n" for w in (window(0, "A", 8.0), window(0, "B", 8.1))),
                   encoding="utf-8")
    assert bench.main(["summarize-cycles", "--out", str(tmp_path)]) == 1
    assert "intel-cpu: fewer than 2 pairs of B and A" in capsys.readouterr().err
    log.write_text("".join(json.dumps(w) + "\n" for w in (window(0, "A", 8.0), window(0, "B", 8.1),
                                                          window(1, "B", 8.1), window(1, "A", 8.0))),
                   encoding="utf-8")
    assert bench.main(["summarize-cycles", "--out", str(tmp_path)]) == 0
    assert json.loads((tmp_path / "cycles-summary.json").read_text(encoding="utf-8"))["intel-cpu"]["B_vs_A"]


def test_every_suite_is_checked_before_anything_starts(monkeypatch, tmp_path, capsys, stub_runs):
    from lagdoctor import setup
    from lagdoctor.winapi import elevation
    out = tmp_path / "out"
    monkeypatch.setattr(elevation, "is_elevated", lambda: False)
    assert bench.main(["cycles", "--suite", "intel-cpu", "--suite", "pm-intel-smooth", "--out", str(out)]) == 2
    assert "pm-intel-smooth: needs an administrator terminal" in capsys.readouterr().out

    def not_ready(allow_download):
        assert allow_download is False
        raise setup.SetupError("PresentMon 2.6.0 is not installed")
    monkeypatch.setattr(elevation, "is_elevated", lambda: True)
    monkeypatch.setattr(setup, "ensure_presentmon", not_ready)
    assert bench.main(["cycles", "--suite", "pm-nvidia-144", "--out", str(out)]) == 2
    assert "PresentMon is not ready: PresentMon 2.6.0 is not installed" in capsys.readouterr().out
    assert stub_runs.calls == [] and not out.exists()


def test_a_recording_already_running_stops_the_benchmark_before_it_starts(tmp_path, capsys, stub_runs):
    stub_runs.free = False
    assert bench.main(["cycles", "--suite", "intel-cpu", "--out", str(tmp_path)]) == 4
    assert "another lag-doctor recording is running" in capsys.readouterr().out
    assert stub_runs.calls == []


def test_runs_appended_to_one_folder_take_the_next_numbers_and_hold_the_lock_throughout(tmp_path, stub_runs):
    earlier = [window(0, "A", 8.0), window(0, "A", 8.0, run=1), window(0, "A", 8.0, suite="nvidia-144", run=4)]
    (tmp_path / "cycles.jsonl").write_text("".join(json.dumps(w) + "\n" for w in earlier), encoding="utf-8")
    assert bench.main(["cycles", "--suite", "intel-cpu", "--runs", "2", "--gap", "0", "--out", str(tmp_path)]) == 0
    assert stub_runs.calls == [("intel-cpu", 2), ("intel-cpu", 3)]
    assert stub_runs.released == 1


def test_a_recorder_that_did_not_record_fails_the_command(tmp_path, capsys, stub_runs):
    stub_runs.queued.append([window(0, "A", 8.0), window(0, "B", 8.1, recorder_exit=3)])
    assert bench.main(["cycles", "--suite", "intel-cpu", "--gap", "0", "--out", str(tmp_path)]) == 1
    assert "cycle 0 B: the recorder exited 3" in capsys.readouterr().out
    stub_runs.queued.append([])                                 # not one cycle fitted in the run
    assert bench.main(["cycles", "--suite", "intel-cpu", "--gap", "0", "--out", str(tmp_path)]) == 1
    assert "no cycle fitted" in capsys.readouterr().out


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


def test_the_parse_timing_runs_lag_doctors_reader_over_every_row_and_weighs_what_it_keeps():
    lines = FIXTURE.read_bytes().splitlines(keepends=True)
    rows = 3 * (len(lines) - 1)
    data = lines[0] + b"".join(lines[1:]) * 3        # held here for the whole call, as main() holds its input
    tracemalloc.start()
    try:
        r = pm_parse_bench.parse_cost(data, rows, read_mb=lambda: tracemalloc.get_traced_memory()[0] / 2**20)
    finally:
        tracemalloc.stop()
    assert r["rows_fed"] == r["rows_parsed"] == r["frames_stored"] == rows == 840
    assert 300 < r["bytes_per_stored_frame"] < 1500      # what each kept frame costs, the input excluded
