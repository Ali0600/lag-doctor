"""capture/target.py: picking the game, following its process tree, never mistaking a compute job for it."""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lagdoctor.capture import target as tgt
from lagdoctor.capture.target import Candidate, TargetNotFound
from lagdoctor.winapi import ntquery
from lagdoctor.winapi.ntquery import ProcInfo

ROOT = Path(__file__).resolve().parents[1]
BASE_PYTHON = getattr(sys, "_base_executable", sys.executable)
NV, INTEL = "0x00000000_0x00010f83", "0x00000000_0x00010c49"


def proc(pid: int, ppid: int = 4, create: int | None = None, name: str = "x.exe") -> ProcInfo:
    return ProcInfo(pid, ppid, 1000 + pid if create is None else create, name, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
                    1, None)


# ---------------------------------------------------------------- counter instance names ---

def test_luid_text_is_lower_case_high_part_first():
    assert tgt.luid_text(0, 0x10F83) == NV
    assert tgt.luid_text(0x1A, 0xABCDEF12) == "0x0000001a_0xabcdef12"


@pytest.mark.parametrize("name, parsed", [
    ("pid_656_luid_0x00000000_0x00010F83_phys_0_eng_0_engtype_3D", (656, NV, 0, 0, "3D")),
    ("pid_8_luid_0x00000000_0x00010F83_phys_0_eng_7_engtype_OFA_0", (8, NV, 0, 7, "OFA_0")),
    ("pid_8_luid_0x00000000_0x00010C49_phys_0_eng_10_engtype_GDI Render", (8, INTEL, 0, 10, "GDI Render")),
    ("pid_8_luid_0x00000000_0x00010C49_phys_0_eng_5_engtype_", (8, INTEL, 0, 5, "")),
    ("pid_x_luid_0x00000000_0x00010C49_phys_0_eng_5_engtype_3D", None),
    ("pid_8_luid_00000000_0x00010C49_phys_0_eng_5_engtype_3D", None),
    ("pid_8_luid_0x00000000_0x00010C49_phys_0_eng_5", None),
    ("luid_0x00000000_0x00010C49_phys_0", None),
])
def test_parse_engine_literal_points(name, parsed):
    assert tgt.parse_engine(name) == parsed


def test_parse_memory_instances():
    assert tgt.parse_adapter_memory("luid_0x00000000_0x00010F83_phys_0") == (NV, 0)
    assert tgt.parse_adapter_memory("luid_0x00000000_0x00010F83_phys_0_part_0") is None
    assert tgt.parse_process_memory("pid_656_luid_0x00000000_0x00010F83_phys_0") == (656, NV, 0)
    assert tgt.parse_process_memory("pid_656_luid_0x0_zz_phys_0") is None


# ---------------------------------------------------------------- process trees ---

def test_descendants_follow_the_tree_and_refuse_a_reused_parent_pid():
    procs = {p.pid: p for p in [
        proc(100, create=1000),                      # the game
        proc(104, ppid=100, create=1100),            # its child
        proc(108, ppid=104, create=1200),            # grandchild
        proc(112, ppid=100, create=900),             # older than the game: its parent was an earlier PID 100
        proc(116, ppid=112, create=1300),            # ... so its child is not in the tree either
        proc(120, ppid=4, create=1050),
    ]}
    assert tgt.descendants(procs, 100, 1000) == {100, 104, 108}
    assert tgt.descendants(procs, 100, 999) == set()           # a different process behind the same PID
    assert tgt.descendants(procs, 200, 1000) == set()


def test_descendants_terminate_on_a_ppid_cycle():
    procs = {200: proc(200, ppid=204, create=5), 204: proc(204, ppid=200, create=5)}
    assert tgt.descendants(procs, 200, 5) == {200, 204}


def test_self_and_ancestors_stop_at_a_reused_parent():
    procs = {p.pid: p for p in [proc(30, ppid=20, create=300), proc(20, ppid=10, create=200),
                                proc(10, ppid=7, create=250)]}           # 10 started after 20: not its parent
    assert tgt.self_and_ancestors(procs, me=30) == {30, 20}


# ---------------------------------------------------------------- choosing ---

def cand(pid, gpu, window=True, fg=False, area=100_000, name="game.exe", create=1):
    return Candidate(pid, create, name, gpu, INTEL if gpu else None, window, fg, area)


def test_auto_never_picks_a_process_without_a_window():
    trainer = cand(656, 89.0, window=False)
    game = cand(16100, 7.0)
    assert tgt.choose_auto([trainer, game]) is game
    assert tgt.choose_auto([trainer]) is None


def test_auto_needs_the_minimum_3d_use():
    floor = tgt.AUTO_MIN_3D_PCT
    assert floor == 2.0
    assert tgt.choose_auto([cand(1, 1.99)]) is None
    assert tgt.choose_auto([cand(1, 2.0)]).pid == 1
    assert tgt.choose_auto([cand(1, None)]) is None


def test_auto_ties_go_to_the_foreground_window():
    assert tgt.TIE_PCT == 1.0
    a, b = cand(1, 50.0), cand(2, 49.0, fg=True)               # exactly one point apart: a tie
    assert tgt.choose_auto([a, b]) is b
    c = cand(3, 48.9, fg=True)                                  # 1.1 points: not a tie
    assert tgt.choose_auto([a, c]) is a
    assert tgt.choose_auto([cand(4, 30.0, area=10), cand(5, 30.0, area=20)]).pid == 5


def test_by_name_the_most_3d_use_wins_then_the_foreground():
    assert tgt.choose_by_name([cand(1, 10.0), cand(2, 40.0)]).pid == 2
    assert tgt.choose_by_name([cand(1, 40.0), cand(2, 39.5, fg=True)]).pid == 2
    unsampled = [cand(1, None, window=False, create=5), cand(2, None, window=True, create=1)]
    assert tgt.choose_by_name(unsampled).pid == 2
    assert tgt.choose_by_name([cand(1, None, window=False, create=5),
                               cand(2, None, window=False, create=9)]).pid == 2      # newest


def test_console_and_zero_size_windows_do_not_count():
    wins = [{"pid": 1, "cls": "ConsoleWindowClass", "area": 500_000, "minimized": False},
            {"pid": 2, "cls": "PseudoConsoleWindow", "area": 500_000, "minimized": False},
            {"pid": 3, "cls": "ASHOTPLUGCTRL", "area": 0, "minimized": False},
            {"pid": 4, "cls": "GLFW30", "area": 921_600, "minimized": False}]
    assert set(tgt.windows_by_pid(wins)) == {4}


def test_the_ignore_list_covers_desktop_renderers_but_not_python():
    for name in ("dwm.exe", "explorer.exe", "steamwebhelper.exe", "discord.exe", "obs64.exe", "chrome.exe",
                 "msedge.exe", "msedgewebview2.exe", "claude.exe"):
        assert name in tgt.IGNORE_NAMES
    assert "python.exe" not in tgt.IGNORE_NAMES and "pythonw.exe" not in tgt.IGNORE_NAMES   # LagLab
    assert all(n == n.lower() for n in tgt.IGNORE_NAMES)


# ---------------------------------------------------------------- GPU sample ---

class FakeQuery:
    def __init__(self, reads):
        self.reads = list(reads)
        self.last_status = {}
        self.closed = False

    def collect(self):
        pass

    def read(self, path):
        return self.reads.pop(0) if self.reads else {}

    def close(self):
        self.closed = True


def eng(pid, luid_low, eng_no, typ):
    return f"pid_{pid}_luid_0x00000000_0x{luid_low:08X}_phys_0_eng_{eng_no}_engtype_{typ}"


def test_sample_3d_averages_unclamped_samples_then_clamps():
    reads = [{eng(1, 0x10C49, 0, "3D"): 300.0, eng(2, 0x10F83, 0, "3D"): 3652.0}] + \
            [{eng(1, 0x10C49, 0, "3D"): 0.0, eng(2, 0x10F83, 0, "3D"): 0.0}] * 3
    q = FakeQuery(reads)
    got = tgt.sample_3d(1.0, query_factory=lambda paths: q, sleep=lambda s: None)
    assert got == {(1, INTEL): 75.0, (2, NV): 100.0}
    assert q.closed


def test_sample_3d_takes_the_busiest_3d_engine_and_ignores_other_types():
    read = {eng(1, 0x10EF4, 0, "3D"): 30.0, eng(1, 0x10EF4, 1, "3D"): 40.0, eng(1, 0x10EF4, 2, "Copy"): 90.0,
            eng(3, 0x10EF4, 0, "VideoDecode"): 80.0}
    got = tgt.sample_3d(0.25, query_factory=lambda paths: FakeQuery([read]), sleep=lambda s: None)
    assert got == {(1, "0x00000000_0x00010ef4"): 40.0}


def test_sample_3d_skips_failed_reads_and_fails_loudly_when_all_fail():
    reads = [{eng(1, 0x10C49, 0, "3D"): 20.0}, {}, {eng(1, 0x10C49, 0, "3D"): 40.0}, {}]
    got = tgt.sample_3d(1.0, query_factory=lambda paths: FakeQuery(reads), sleep=lambda s: None)
    assert got == {(1, INTEL): 30.0}                       # the mean of the two valid reads, not of four
    with pytest.raises(OSError):
        tgt.sample_3d(1.0, query_factory=lambda paths: FakeQuery([]), sleep=lambda s: None)


# ---------------------------------------------------------------- find_target with fake sources ---

@pytest.fixture
def fake_machine(monkeypatch):
    """A made-up machine: a game, a windowless compute job, dwm, and this test process with a window."""
    me = os.getpid()
    procs = {p.pid: p for p in [
        proc(4, ppid=0, create=1, name="System"),
        proc(500, name="dwm.exe"),
        proc(656, name="python.exe"),                        # the training job: busy 3D, no window
        proc(900, name="Game.exe"),
        proc(904, ppid=900, create=2000, name="Game.exe"),   # a second process with the same name
        proc(me, ppid=4, create=1500, name="python.exe"),
    ]}
    gpu = {(500, INTEL): 60.0, (656, NV): 89.0, (900, NV): 35.0, (904, NV): 3.0, (me, INTEL): 95.0}
    wins = [{"hwnd": 11, "pid": 500, "title": "", "area": 2_000_000, "minimized": False,
             "monitor_device": "\\\\.\\DISPLAY1", "cls": "Dwm"},
            {"hwnd": 22, "pid": 900, "title": "The Game", "area": 2_073_600, "minimized": False,
             "monitor_device": "\\\\.\\DISPLAY1", "cls": "UnrealWindow"},
            {"hwnd": 33, "pid": me, "title": "pytest", "area": 50_000, "minimized": False,
             "monitor_device": "\\\\.\\DISPLAY1", "cls": "Tk"}]
    monkeypatch.setattr(tgt, "_snapshot", lambda threads_for=(): dict(procs))
    monkeypatch.setattr(tgt, "sample_3d", lambda seconds, **kw: dict(gpu))
    monkeypatch.setattr(tgt.windows, "top_windows", lambda: list(wins))
    monkeypatch.setattr(tgt.windows, "foreground_pid", lambda: me)
    monkeypatch.setattr(tgt.ntquery, "image_path", lambda pid: f"C:\\Games\\{procs[pid].name}")
    return procs


def test_auto_picks_the_windowed_game_over_ignored_and_windowless_and_ourselves(fake_machine):
    t = tgt.find_target(auto=True, sample_s=1.0)
    assert t["pid"] == 900 and t["how"] == "auto"
    assert t["create_time"] == 1900 and t["exe"] == "C:\\Games\\Game.exe"
    assert t["window_title"] == "The Game" and t["monitor_device"] == "\\\\.\\DISPLAY1"
    assert t["adapter_luid"] == NV
    listed = {c["pid"]: c for c in t["candidates"]}
    assert listed[656]["has_window"] is False and listed[656]["gpu_pct"] == 89.0
    assert 500 not in listed and os.getpid() not in listed


def test_by_name_skips_ourselves_and_prefers_the_busier_match(fake_machine):
    t = tgt.find_target(name="game", sample_s=1.0)
    assert t["pid"] == 900 and t["how"] == "name"
    assert [c["pid"] for c in t["candidates"]] == [900, 904]
    py = tgt.find_target(name="python.exe", sample_s=1.0)
    assert py["pid"] == 656                                   # never this test process, though it is busier


def test_by_pid_works_without_a_window_or_a_sample(fake_machine):
    t = tgt.find_target(pid=656, sample_s=0)
    assert t["pid"] == 656 and t["window_title"] is None and t["adapter_luid"] is None
    assert t["candidates"] == [{"pid": 656, "name": "python.exe", "gpu_pct": None, "has_window": False}]


def test_refusals_carry_a_reason(fake_machine):
    for kwargs, words in [({"pid": 4}, "kernel"), ({"pid": os.getpid()}, "lag-doctor itself"),
                          ({"pid": 4_000_000}, "no process"), ({"pid": -1}, "not a process id"),
                          ({"name": "nope.exe"}, "no running process"), ({"name": ""}, "not an exe name")]:
        with pytest.raises(TargetNotFound, match=words):
            tgt.find_target(sample_s=0, **kwargs)
    for kwargs in [{}, {"name": "a", "pid": 1}, {"auto": True, "sample_s": 0}, {"pid": 900, "sample_s": -1},
                   {"pid": 900, "sample_s": math.nan}, {"pid": 900, "sample_s": 31}]:
        with pytest.raises(ValueError):
            tgt.find_target(**kwargs)


def test_auto_explains_when_only_a_windowless_process_renders(fake_machine, monkeypatch):
    monkeypatch.setattr(tgt, "sample_3d", lambda seconds, **kw: {(656, NV): 89.0})
    with pytest.raises(TargetNotFound) as info:
        tgt.find_target(auto=True, sample_s=1.0)
    assert "PID 656" in info.value.reason and "no visible window" in info.value.reason
    assert {"pid": 656, "name": "python.exe", "gpu_pct": 89.0, "has_window": False} in info.value.candidates


# ---------------------------------------------------------------- live ---

# The child sleeps until the test kills it; the grandchild ends by itself a few seconds later.
SLEEPER = r"""
import subprocess, sys, time
depth = int(sys.argv[1])
if depth:
    kid = subprocess.Popen([sys.executable, "-I", "-c", sys.argv[2], str(depth - 1), sys.argv[2]])
    print(kid.pid, flush=True)
time.sleep(30 if depth else 6)
"""


def _reap(*procs):
    for p in procs:
        if p is not None and p.poll() is None:
            p.kill()
            p.wait(timeout=5)


def test_a_spawned_child_is_found_by_pid_and_followed_until_it_dies():
    child = subprocess.Popen([BASE_PYTHON, "-I", "-c", SLEEPER, "1", SLEEPER], stdout=subprocess.PIPE, text=True)
    try:
        gpid = int(child.stdout.readline())
        t = tgt.find_target(pid=child.pid, sample_s=0)
        assert t["pid"] == child.pid and t["how"] == "pid" and t["name"].lower() == "python.exe"
        assert os.path.normcase(t["exe"]) == os.path.normcase(os.path.realpath(BASE_PYTHON))
        assert tgt.is_alive(child.pid, t["create_time"])
        assert not tgt.is_alive(child.pid, t["create_time"] + 1)        # same PID, other process
        assert tgt.game_pids(child.pid, t["create_time"]) == {child.pid, gpid}
        child.kill()
        child.wait(timeout=5)
        assert not tgt.is_alive(child.pid, t["create_time"])
        assert tgt.game_pids(child.pid, t["create_time"]) == set()
        assert gpid in ntquery.Snapshotter().snapshot()              # the orphan lives on, outside any tree
    finally:
        _reap(child)


@pytest.mark.parametrize("bad", [(0, 1), (-4, 1), (4, -1), (True, 1), (4, 1.5)])
def test_is_alive_and_game_pids_refuse_junk(bad):
    with pytest.raises(ValueError):
        tgt.is_alive(*bad)
    with pytest.raises(ValueError):
        tgt.game_pids(*bad)


def test_auto_picks_a_windowed_renderer_and_never_a_windowless_gpu_job(tmp_path):
    """Live: LagLab (a small window, light load on the Intel GPU) must win over any windowless 3D user,
    such as this machine's ML training job, however much busier that job is."""
    pid_file = tmp_path / "laglab.pid"
    lab = subprocess.Popen([sys.executable, "-m", "laglab", "--mode", "smooth", "--duration", "6",
                            "--width", "480", "--height", "270", "--pid-file", str(pid_file)],
                           cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.perf_counter() + 15
        while not pid_file.exists() and time.perf_counter() < deadline and lab.poll() is None:
            time.sleep(0.1)
        assert pid_file.exists(), f"LagLab never started: {lab.stderr.read().decode(errors='replace')[-500:]}"
        lab_pid = json.loads(pid_file.read_text())["pid"]
        t = tgt.find_target(auto=True, sample_s=1.5)
        assert t["pid"] == lab_pid, t
        assert t["window_title"] and t["window_title"].startswith("LagLab")
        assert t["adapter_luid"] and t["adapter_luid"] == t["adapter_luid"].lower()
        lab_gpu = next(c["gpu_pct"] for c in t["candidates"] if c["pid"] == lab_pid)
        busier = [c for c in t["candidates"] if not c["has_window"] and c["gpu_pct"] > lab_gpu]
        # When the training job runs it is far busier than LagLab: seen, listed as windowless, not picked.
        print(f"LagLab {lab_gpu} %; windowless 3D users busier than the pick: {busier}")
    finally:
        lab.wait(timeout=20)


def test_auto_without_a_game_never_picks_a_windowless_gpu_job():
    """Live: with no game running, auto either refuses or picks a windowed process; the training job (a
    windowless CUDA user, booked to the 3D engine) is listed as seen but never chosen."""
    try:
        t = tgt.find_target(auto=True, sample_s=1.5)
    except TargetNotFound as exc:
        t, listed = None, exc.candidates
    else:
        listed = t["candidates"]
        assert t["pid"] in tgt.windows_by_pid(tgt.windows.top_windows()), t
    jobs = [c for c in listed if not c["has_window"] and c["gpu_pct"] >= tgt.AUTO_MIN_3D_PCT]
    if t is not None:
        assert t["pid"] not in {c["pid"] for c in jobs}, (t, jobs)
    if not jobs:
        pytest.skip("no windowless 3D user ran during the sample (the training job is not running), so only "
                    "the unit tests prove the window rule here")
    print(f"seen and not picked: {jobs}; picked: {t and t['pid']}")
