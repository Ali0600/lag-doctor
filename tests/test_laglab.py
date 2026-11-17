"""LagLab's safety mechanisms (its background CPU hogs must never outlive a test) and the parts of its scenarios the
end-to-end judge relies on: hog mode's game work, its burst schedule, and the log of whether its window was in front."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from laglab import app
from laglab.app import FixedWork, FrontWatch, UncachedReader, hog_bursts
from laglab.jobs import KillOnCloseJob

BASE_PYTHON = getattr(sys, "_base_executable", sys.executable)
HOG = Path(__file__).resolve().parents[1] / "laglab" / "hog.py"


def _sleeper(seconds: float) -> subprocess.Popen:
    return subprocess.Popen([BASE_PYTHON, "-I", "-c", f"import time; time.sleep({seconds})"])


def _reap(*procs: subprocess.Popen) -> None:
    for p in procs:
        if p.poll() is None:
            p.kill()
            p.wait(timeout=5)


def test_job_kills_its_processes_when_closed():
    child = _sleeper(60)
    try:
        job = KillOnCloseJob()
        job.add(child.pid)
        time.sleep(0.2)
        assert child.poll() is None, "child died before the job was closed"
        job.close()
        child.wait(timeout=5)  # TimeoutExpired here means the job did not kill it
    finally:
        _reap(child)


def test_hog_exits_soon_after_its_parent_dies():
    parent = _sleeper(1.0)
    hog = subprocess.Popen([BASE_PYTHON, "-I", str(HOG), "--t0", repr(time.perf_counter()),
                            "--on", "0.2", "--off", "0.2", "--duration", "60", "--parent", str(parent.pid)])
    try:
        parent.wait(timeout=10)
        died_at = time.perf_counter()
        hog.wait(timeout=5)
        assert time.perf_counter() - died_at < 2.0
    finally:
        _reap(hog, parent)


def test_hog_burns_cpu_only_in_its_on_phase():
    parent = _sleeper(30)
    t0 = time.perf_counter() + 0.3
    hog = subprocess.Popen([BASE_PYTHON, "-I", str(HOG), "--t0", repr(t0), "--on", "1.0", "--off", "1.5",
                            "--duration", "20", "--parent", str(parent.pid)])
    try:
        ps = psutil.Process(hog.pid)
        time.sleep(max(0.0, t0 - time.perf_counter()) + 0.1)   # inside the on phase
        c0 = sum(ps.cpu_times()[:2])
        time.sleep(0.8)
        on_cpu = sum(ps.cpu_times()[:2]) - c0
        time.sleep(max(0.0, t0 + 1.2 - time.perf_counter()))   # inside the off phase
        c1 = sum(ps.cpu_times()[:2])
        time.sleep(1.0)
        off_cpu = sum(ps.cpu_times()[:2]) - c1
        assert on_cpu > 0.5, f"hog used only {on_cpu:.2f}s of CPU in 0.8s of its on phase"
        assert off_cpu < 0.2, f"hog used {off_cpu:.2f}s of CPU in 1s of its off phase"
    finally:
        _reap(hog, parent)


def test_uncached_reader_reads_whole_blocks(tmp_path):
    path = tmp_path / "blocks.bin"
    path.write_bytes(os.urandom(UncachedReader.BLOCK * 64))
    reader = UncachedReader(path)
    try:
        assert reader.read_random(10) == 10 * UncachedReader.BLOCK
    finally:
        reader.close()


def test_list_modes_needs_no_window():
    out = subprocess.run([sys.executable, "-m", "laglab", "--list-modes"], capture_output=True, text=True,
                         timeout=60, cwd=Path(__file__).resolve().parents[1])
    assert out.returncode == 0
    names = [line.split()[0] for line in out.stdout.splitlines() if line.strip()]
    assert names == sorted(["smooth", "gpu", "cpu", "hitch", "hitch-io", "hitch-wait", "hog", "pace"])


@pytest.mark.skipif(sys.platform != "win32", reason="Windows job objects")
def test_job_close_is_idempotent():
    job = KillOnCloseJob()
    job.close()
    job.close()


def test_a_hog_burns_nothing_before_its_start():
    # warmup 1.5 s, on 1.0, off 0.2: run backwards from t0, the old phase arithmetic put the warmup's first 0.5 s
    # in an on phase, and LagLab measures its game work in the warmup
    parent = _sleeper(30)
    t0 = time.perf_counter() + 1.5
    hog = subprocess.Popen([BASE_PYTHON, "-I", str(HOG), "--t0", repr(t0), "--on", "1.0", "--off", "0.2",
                            "--duration", "0", "--parent", str(parent.pid)])
    try:
        ps = psutil.Process(hog.pid)
        time.sleep(0.3)                                                # past the interpreter's own start-up
        c0 = sum(ps.cpu_times()[:2])
        time.sleep(max(0.0, t0 - 0.1 - time.perf_counter()))
        before = sum(ps.cpu_times()[:2]) - c0
        assert before < 0.2, f"hog used {before:.2f}s of CPU before its start"
        hog.wait(timeout=5)                                            # --duration 0: it ends at its start
    finally:
        _reap(hog, parent)


# ------------------------------------------------------------------------------ hog mode's game work ---

class FakeClock:
    """A clock that moves `step` seconds on each call: one call per loop turn, so `step` is how fast the CPU is."""

    def __init__(self, step: float) -> None:
        self.t, self.step, self.calls = 100.0, step, 0

    def __call__(self) -> float:
        self.t += self.step
        self.calls += 1
        return self.t


def test_fixed_work_takes_longer_on_a_slower_cpu_where_a_timed_burn_would_not():
    clock = FakeClock(1e-6)                    # 1 us a loop turn
    work = FixedWork(6.0, clock=clock)
    for _ in range(20):
        work.measure()
    turns = work.fix()
    assert 5990 <= turns <= 6000               # 6 ms of 1 us turns
    clock.step = 1.7e-6                        # a hog on the core's other thread, a lower clock
    t1 = clock.t
    work.measure()
    assert clock.t - t1 == pytest.approx(0.006, abs=1e-5)          # a timed burn ends on time: nothing to see
    t2, calls = clock.t, clock.calls
    work.run()
    assert clock.calls - calls == turns                            # the same work ...
    assert clock.t - t2 == pytest.approx(turns * 1.7e-6, rel=1e-6) and clock.t - t2 > 0.0101   # ... takes 1.7x
    assert work.frames == 1


def test_fixed_work_takes_the_90th_percentile_of_its_warmup():
    work = FixedWork(6.0, clock=FakeClock(1e-6))
    work.samples = [7, 3, 20, 11, 1, 19, 5, 14, 9, 16, 2, 18, 13, 6, 10, 4, 17, 8, 12, 15]
    assert work.fix() == 19 and work.turns == 19
    work.samples = [5]
    assert work.fix() == 5
    work.samples = [0]
    assert work.fix() == 1                     # never zero turns: the frame still does some work


def test_hog_frames_measure_in_the_warmup_then_run_the_measured_work():
    work = FixedWork(6.0, clock=FakeClock(1e-6))
    t0 = 50.0
    for start in (47.0, 48.0, t0 - app.HOG_MEASURE_GAP_S - 0.01):
        app.hog_work(work, start, t0)
    assert len(work.samples) == 3 and work.frames == 0
    for start in (t0 - app.HOG_MEASURE_GAP_S, t0, t0 + 1.0):
        app.hog_work(work, start, t0)
    assert len(work.samples) == 3 and work.frames == 3


def test_hog_frames_measure_once_when_the_first_one_comes_after_the_warmup():
    # 64 hogs can take longer to start than a short warmup: the first frame then measures, and the next ones run
    work = FixedWork(6.0, clock=FakeClock(1e-6))
    app.hog_work(work, 51.0, 50.0)
    app.hog_work(work, 51.1, 50.0)
    assert len(work.samples) == 1 and work.frames == 1


def test_fixed_work_never_measured_says_so():
    with pytest.raises(RuntimeError, match="lengthen --warmup"):
        FixedWork(6.0, clock=FakeClock(1e-6)).run()


def test_hog_mode_refuses_a_warmup_too_short_to_measure_its_work():
    with pytest.raises(SystemExit):
        app._parse_args(["--mode", "hog", "--warmup", "0.3"])
    assert app._parse_args(["--mode", "hog", "--warmup", str(app.HOG_MIN_WARMUP_S)]).warmup == app.HOG_MIN_WARMUP_S
    assert app._parse_args(["--mode", "hitch", "--warmup", "0.3"]).warmup == 0.3      # only hog mode measures


# ------------------------------------------------------------------------------- the burst schedule ---

@pytest.mark.parametrize("args, bursts", [
    ((0.0, 30.0, 3.0, 3.0, 2.0), [[3, 6], [8, 11], [13, 16], [18, 21], [23, 26]]),   # the defaults: 28 + 3 > 30
    ((0.0, 40.0, 3.0, 3.0, 2.0), [[3, 6], [8, 11], [13, 16], [18, 21], [23, 26], [28, 31], [33, 36]]),
    ((0.0, 26.0, 3.0, 3.0, 2.0), [[3, 6], [8, 11], [13, 16], [18, 21], [23, 26]]),   # one that ends at the stop
    ((10.0, 25.0, 3.0, 6.0, 6.0), [[13, 19], [25, 31]]),                              # the old 6/6 on LagLab's clock
    ((0.0, 5.0, 3.0, 3.0, 2.0), []),                                                  # no whole burst fits
    ((0.0, 30.0, 3.0, 0.0, 2.0), []),
    ((0.0, 30.0, 3.0, 3.0, -3.0), []),
])
def test_hog_bursts_are_whole_ones_only(args, bursts):
    assert hog_bursts(*args) == bursts


def test_the_hogs_stop_after_the_last_whole_burst():
    def duration(argv):
        return float(argv[argv.index("--duration") + 1])
    bursts = hog_bursts(100.0, 30.0, 3.0, 3.0, 2.0)                    # the last ends at 126, 4 s before LagLab
    argv = app.hog_args(103.0, 3.0, 2.0, bursts, 4242)
    assert Path(argv[0]).name == "hog.py" and argv[argv.index("--t0") + 1] == repr(103.0)
    assert duration(argv) == 23.0 and argv[argv.index("--parent") + 1] == "4242"
    assert duration(app.hog_args(103.0, 3.0, 2.0, [], 4242)) == 0.0     # no whole burst: nothing to burn


# -------------------------------------------------------------------------- the window in front ---

def test_front_watch_keeps_only_the_changes():
    states = iter([True, True, False, False, False, True, True])
    changes: list = []
    watch = FrontWatch(changes, probe=lambda: next(states))
    for t in range(1, 8):
        watch.note(float(t))
    assert changes == [[1.0, True], [3.0, False], [6.0, True]]


def test_front_watch_asks_windows_without_a_probe():
    changes: list = []
    FrontWatch(changes).note(1.0)              # the test's console process owns no window LagLab could be
    assert len(changes) == 1 and changes[0][0] == 1.0 and isinstance(changes[0][1], bool)


@pytest.mark.needs("gpu")
def test_hog_mode_runs_its_measured_work_after_the_warmup_and_logs_its_window(tmp_path):
    log_path = tmp_path / "laglab.json"
    out = subprocess.run([sys.executable, "-m", "laglab", "--mode", "hog", "--duration", "2.6", "--warmup", "0.8",
                          "--hog-on", "0.6", "--hog-off", "0.4", "--hogs", "1", "--log", str(log_path)],
                         capture_output=True, text=True, timeout=90, cwd=Path(__file__).resolve().parents[1])
    assert out.returncode == 0, out.stderr[-2000:]
    log = json.loads(log_path.read_text(encoding="utf-8"))
    t0 = log["t_start"] + 0.8
    after = sum(1 for f in log["frames"] if f >= t0 - app.HOG_MEASURE_GAP_S)
    work = log["game_work"]
    assert work["frames"] == after and work["measured"] + after == len(log["frames"])   # every frame after the warmup
    assert work["ms"] == 6.0 and work["turns"] is not None and work["turns"] >= 1 and work["measured"] >= 1
    assert log["hog_bursts"] == [pytest.approx([t0, t0 + 0.6], rel=0, abs=1e-6),
                                 pytest.approx([t0 + 1.0, t0 + 1.6], rel=0, abs=1e-6)]
    assert log["front"] and log["front"][0][0] == log["frames"][0] and isinstance(log["front"][0][1], bool)
    assert log["hog_survivors"] == []
