"""LagLab's safety mechanisms: its background CPU hogs must never outlive a test."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from laglab.app import UncachedReader
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
