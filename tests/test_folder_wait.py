"""Play's folder wait and folder rule (docs/LAUNCHER-PLAN.md, M6) in capture/target.py.

Most cases run on a made-up machine over time: processes start and exit, windows open and 3D use changes at given
seconds, and the wait's clock, its sleeps and each GPU sample move that machine's time. Two live cases check what
the fakes stand in for: the clock a process's create time is stamped with, and a real LagLab window found by its
folder. Games and folders here are made up.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path

import pytest

from lagdoctor.capture import target as tgt
from lagdoctor.capture.target import TargetNotFound, WaitCancelled
from lagdoctor.winapi import ntquery
from lagdoctor.winapi.ntquery import ProcInfo

ROOT = Path(__file__).resolve().parents[1]
BASE_PYTHON = getattr(sys, "_base_executable", sys.executable)
NV = "0x00000000_0x00010f83"
GAME_DIR = "C:\\Games\\Lantern Drift"
GAME = GAME_DIR + "\\Lantern\\Binaries\\Win64\\lantern_.exe"
STUB = GAME_DIR + "\\lantern.exe"
SINCE = 133_000_000_000_000_000     # a create time: the wait looks only at processes created after it
SECOND = 10_000_000                 # one second in create-time units
ME = os.getpid()


def proc(pid: int, create: int, name: str, session: int = 1, ppid: int = 4) -> ProcInfo:
    return ProcInfo(pid, ppid, create, name, session, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, None)


class World:
    """A made-up machine over time. Each plan entry happens at its second; the wait's clock, its sleeps and each GPU
    sample (which takes its length, and reads the 3D use at its end) move the time forward."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.t = 0.0
        self.plan: list[tuple[float, int, str, int, object]] = []
        self.procs = {ME: proc(ME, SINCE - 100 * SECOND, "python.exe")}
        self.images = {ME: "C:\\Python\\python.exe"}
        self.wins: dict[int, int] = {}
        self.gpu: dict[int, float] = {}
        self.reads: Counter[int] = Counter()
        self.samples = 0
        self.gpu_fails = False
        self.fail_on: set[int] = set()          # which samples (1, 2, ...) fail
        self.fg: int | None = None
        self.statuses: list[tuple[str, dict]] = []
        monkeypatch.setattr(tgt, "_snapshot", self.snapshot)
        monkeypatch.setattr(tgt.ntquery, "image_path", self.image_path)
        monkeypatch.setattr(tgt.windows, "top_windows", self.top_windows)
        monkeypatch.setattr(tgt.windows, "foreground_pid", lambda: self.fg)
        monkeypatch.setattr(tgt, "sample_3d", self.sample_3d)
        monkeypatch.setattr(tgt, "_real_folder", lambda folder: None)

    # ---- the plan
    def _at(self, at: float, kind: str, pid: int, value: object = None) -> None:
        self.plan.append((at, len(self.plan), kind, pid, value))

    def start(self, at: float, pid: int, image: str | None, *, name: str | None = None, session: int = 1,
              before: bool = False, window: float | None = None, draw: float | None = None) -> int:
        """A process started at `at` (or before the wait's `since`), optionally with a window and 3D use from then."""
        create = SINCE - SECOND if before else SINCE + int(at * SECOND) + 1
        nm = name or (image.rsplit("\\", 1)[-1] if image else "unknown.exe")
        self._at(at, "start", pid, (proc(pid, create, nm, session), image))
        if window is not None:
            self._at(at, "window", pid, window)
        if draw is not None:
            self._at(at, "draw", pid, draw)
        return create

    def window(self, at: float, pid: int, area: int = 2_073_600) -> None:
        self._at(at, "window", pid, area)

    def draw(self, at: float, pid: int, pct: float) -> None:
        self._at(at, "draw", pid, pct)

    def exit(self, at: float, pid: int) -> None:
        self._at(at, "exit", pid)

    def call(self, at: float, fn) -> None:
        self._at(at, "call", 0, fn)

    def _apply(self) -> None:
        self.plan.sort(key=lambda e: (e[0], e[1]))
        while self.plan and self.plan[0][0] <= self.t:
            _, _, kind, pid, value = self.plan.pop(0)
            if kind == "start":
                self.procs[pid], self.images[pid] = value
            elif kind == "window":
                self.wins[pid] = value
            elif kind == "draw":
                self.gpu[pid] = value
            elif kind == "exit":
                for table in (self.procs, self.wins, self.gpu):
                    table.pop(pid, None)
            else:
                value()

    # ---- what the wait reads
    def clock(self) -> float:
        self._apply()
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds
        self._apply()

    def snapshot(self, threads_for=()) -> dict[int, ProcInfo]:
        self._apply()
        return dict(self.procs)

    def image_path(self, pid: int) -> str | None:
        self.reads[pid] += 1
        return self.images.get(pid) if pid in self.procs else None

    def top_windows(self) -> list[dict]:
        return [{"hwnd": 1000 + pid, "pid": pid, "title": f"window of {pid}", "area": area, "minimized": False,
                 "monitor_device": "\\\\.\\DISPLAY1", "cls": "GameWindow"}
                for pid, area in self.wins.items() if pid in self.procs]

    def sample_3d(self, seconds: float, **kw) -> dict[tuple[int, str], float]:
        """The 3D use at the sample's end; a process that exited during the sample keeps the use it had, as the
        real counters' mean still holds the readings taken while it ran."""
        self.samples += 1
        at_start = {pid: v for pid, v in self.gpu.items() if pid in self.procs}
        self.t += seconds
        self._apply()
        if self.gpu_fails or self.samples in self.fail_on:
            raise OSError("the GPU Engine counters gave no reading in 1.0 s")
        out = {pid: v for pid, v in self.gpu.items() if pid in self.procs}
        for pid, v in at_start.items():
            if pid not in self.procs:
                out[pid] = v
        return {(pid, NV): v for pid, v in out.items()}

    def wait(self, folder: str = GAME_DIR, **kw) -> dict:
        kw.setdefault("since", SINCE)
        kw.setdefault("deadline_s", 60.0)
        kw.setdefault("on_status", lambda state, info: self.statuses.append((state, info)))
        return tgt.wait_for_target(folder, clock=self.clock, sleep=self.sleep, **kw)


@pytest.fixture
def world(monkeypatch):
    return World(monkeypatch)


# ---------------------------------------------------------------- the folder ---

@pytest.mark.parametrize("given, clean", [
    ("C:\\Games\\Lantern Drift", "C:\\Games\\Lantern Drift"),
    ("c:/program files (x86)/steam/steamapps/common/Lantern Drift",
     "C:\\program files (x86)\\steam\\steamapps\\common\\Lantern Drift"),
    ("D:\\Lantern\\", "D:\\Lantern"),
    ("E:\\Games\\\\Lantern", "E:\\Games\\Lantern"),
    ("F:\\Games\\ Lantern", "F:\\Games\\ Lantern"),
])
def test_check_folder_takes_plain_local_folders_in_windows_own_form(given, clean):
    assert tgt.check_folder(given) == clean


@pytest.mark.parametrize("bad, words", [
    ("C:\\", "whole drive"),
    ("C:/", "whole drive"),
    ("C:", "printable"),
    ("C:Games\\Lantern", "local drive"),
    ("Games\\Lantern", "local drive"),
    ("\\\\server\\share\\Lantern", "local drive"),
    ("\\\\?\\C:\\Games\\Lantern", "local drive"),
    ("\\\\.\\C:\\Games\\Lantern", "local drive"),
    ("\\Device\\HarddiskVolume3\\Games", "local drive"),
    ("C:\\\\Games", "local drive"),
    ("C:\\Games\\..\\Windows\\System32", ".. part"),
    ("C:\\Games\\.\\Lantern", ". part"),
    ("C:\\Games\\Lantern.", "would not keep"),
    ("C:\\Games\\Lantern ", "would not keep"),
    ("C:\\Games\\Lantern:stream", "would not keep"),
    ("C:\\Games\\Lan*tern", "would not keep"),
    ("C:\\Games\\Lan\ttern", "printable"),
    ("C:\\Games\\" + "x" * 260, "printable"),
    ("C:\\Windows", "Windows folder"),
    ("c:/windows/System32/Lantern", "Windows folder"),
])
def test_check_folder_refuses_what_could_take_in_programs_that_are_not_the_game(bad, words):
    with pytest.raises(ValueError, match=words):
        tgt.check_folder(bad)


@pytest.mark.parametrize("bad", [None, 3, b"C:\\Games\\Lantern", Path("C:/Games/Lantern")])
def test_check_folder_takes_only_text(bad):
    with pytest.raises(ValueError, match="not a folder path"):
        tgt.check_folder(bad)


def test_the_windows_folder_is_the_one_windows_says(monkeypatch):
    monkeypatch.setenv("SystemRoot", "D:\\WinNT")
    assert tgt.check_folder("C:\\Windows\\Lantern") == "C:\\Windows\\Lantern"
    with pytest.raises(ValueError, match="Windows folder"):
        tgt.check_folder("d:/winnt/Lantern")


def test_in_folder_folds_case_and_slashes_and_needs_a_separator_after_the_folder():
    steam_form = "c:/program files (x86)/steam/steamapps/common/lantern drift"
    image = "C:\\Program Files (x86)\\Steam\\steamapps\\common\\Lantern Drift\\lantern.exe"
    assert tgt.in_folder(image, tgt.check_folder(steam_form))
    assert tgt.in_folder(GAME, GAME_DIR) and tgt.in_folder(STUB, GAME_DIR)
    assert not tgt.in_folder("C:\\Games\\Lantern Drift 2\\lantern.exe", GAME_DIR)     # a sibling sharing the prefix
    assert not tgt.in_folder("C:\\Games\\Lantern Drift", GAME_DIR)                    # the folder itself
    assert not tgt.in_folder("C:\\Games\\lantern.exe", GAME_DIR)


@pytest.mark.parametrize("image, words", [
    (None, "did not give the path"),
    ("", "did not give the path"),
    ("\\Device\\HarddiskVolume9\\Games\\Lantern Drift\\lantern.exe", "no drive letter"),
    ("\\\\server\\games\\Lantern Drift\\lantern.exe", "network share"),
    ("C:\\Games\\Lantern Drift\\..\\..\\Windows\\evil.exe", ". or .. part"),
    ("C:\\Games\\Lantern Drift\\.\\lantern.exe", ". or .. part"),
    ("D:\\Games\\Lantern Drift\\lantern.exe", "is not in the game's folder"),
])
def test_folder_reason_says_why_a_program_is_not_from_the_folder(image, words):
    why = tgt.folder_reason(image, GAME_DIR)
    assert why is not None and words in why
    assert not tgt.in_folder(image, GAME_DIR)


def test_accept_target_by_folder_takes_a_game_started_after_the_relay_from_under_the_folder():
    relay = SINCE
    procs = {80: proc(80, relay + 5 * SECOND, "lantern_.exe"), 81: proc(81, relay - 1, "lantern_.exe"),
             82: proc(82, relay + 5 * SECOND, "lantern_.exe", session=2), 83: proc(83, relay, "lantern_.exe")}

    def accept(pid, create, image=GAME, **kw):
        return tgt.accept_target(procs, pid, create, session=1, folder=GAME_DIR, since=relay, image=image, **kw)

    assert accept(80, relay + 5 * SECOND) is None
    assert "started before the UAC prompt was approved" in accept(81, relay - 1)
    assert "started before the UAC prompt was approved" in accept(83, relay)               # not after: the same time
    assert "another Windows session" in accept(82, relay + 5 * SECOND)
    assert "no longer running" in accept(80, relay + 5 * SECOND + 1)
    assert "no longer running" in accept(84, relay + 5 * SECOND)
    assert "is not the game: its program (D:\\x.exe) is not in the game's folder" in accept(
        80, relay + 5 * SECOND, image="D:\\x.exe")
    assert "no drive letter" in accept(80, relay + 5 * SECOND, image="\\Device\\HarddiskVolume9\\x.exe")
    assert "did not give the path" in accept(80, relay + 5 * SECOND, image=None)


def test_accept_target_takes_one_mode_and_checks_the_folder():
    procs = {80: proc(80, SINCE + SECOND, "lantern_.exe")}
    for kw in [{}, {"approved": (80, SINCE + SECOND), "folder": GAME_DIR, "since": SINCE},
               {"approved": (80, SINCE + SECOND), "since": SINCE}, {"approved": (80, SINCE + SECOND), "image": GAME},
               {"folder": GAME_DIR}, {"folder": GAME_DIR, "since": -1}, {"folder": GAME_DIR, "since": True},
               {"folder": "C:\\", "since": SINCE}, {"folder": "\\\\server\\share\\x", "since": SINCE}]:
        with pytest.raises(ValueError):
            tgt.accept_target(procs, 80, SINCE + SECOND, session=1, **kw)
    assert tgt.accept_target(procs, 80, SINCE + SECOND, session=1, approved=(80, SINCE + SECOND)) is None


# ---------------------------------------------------------------- the wait ---

def test_the_game_is_the_new_process_from_the_folder_that_draws_on_two_samples_in_a_row(world):
    stub = 41
    world.start(1.0, stub, STUB)                                  # Unreal's stub: no window, never draws, stays
    world.start(2.0, 50, "C:\\Tools\\Painter\\painter.exe", window=900_000, draw=90.0)    # not the game's folder
    game_ct = world.start(3.0, 42, GAME)
    world.window(4.0, 42)
    world.draw(4.0, 42, 40.0)
    found = world.wait()
    assert (found["pid"], found["create_time"]) == (42, game_ct)
    assert found["how"] == "folder" and found["folder"] == GAME_DIR and found["judged_by"] == "3d"
    assert found["exe"] == GAME and found["name"] == "lantern_.exe"
    assert found["window_title"] == "window of 42" and found["monitor_device"] == "\\\\.\\DISPLAY1"
    assert found["adapter_luid"] == NV
    assert found["candidates"][0] == {"pid": 42, "name": "lantern_.exe", "gpu_pct": 40.0, "has_window": True}
    assert [s for s, _ in world.statuses] == ["waiting", "started", "window"]
    assert world.statuses[1][1] == {"pid": stub, "name": "lantern.exe"}
    assert world.statuses[2][1] == {"pid": 42, "name": "lantern_.exe"}
    assert world.reads == Counter({stub: 1, 50: 1, 42: 1})       # each new process's path, read once
    assert 5.5 <= world.t <= 7.0                                  # found on its second drawing sample


def test_one_drawing_sample_is_not_enough(world):
    world.start(0.9, 42, GAME, window=2_073_600, draw=40.0)
    world.draw(2.5, 42, 0.0)                                      # drew through one sample, then stopped
    with pytest.raises(TargetNotFound, match="drew only in short bursts"):
        world.wait(deadline_s=20.0)


def test_a_sample_without_drawing_starts_the_count_over(world):
    world.start(0.9, 42, GAME, window=2_073_600, draw=40.0)
    for t in (2.5, 4.5, 6.5, 8.5):                                # draws for one sample, then not for the next
        world.draw(t, 42, 0.0)
        world.draw(t + 1.0, 42, 40.0)
    with pytest.raises(TargetNotFound, match="short bursts"):
        world.wait(deadline_s=10.0)


def test_an_older_copy_of_the_game_is_never_found_nor_even_read(world):
    world.start(0.0, 42, GAME, before=True, window=2_073_600, draw=60.0)    # still closing, or started by hand
    with pytest.raises(TargetNotFound, match="no program from the game's folder"):
        world.wait(deadline_s=5.0)
    assert world.reads[42] == 0 and world.samples == 0


def test_programs_outside_the_folder_or_with_an_unusable_path_never_count(world):
    for pid, image in [(61, "C:\\Games\\Lantern Drift 2\\lantern_.exe"), (62, "C:\\Games\\lantern_.exe"),
                       (63, "\\Device\\HarddiskVolume9\\Games\\Lantern Drift\\lantern_.exe"), (64, None),
                       (65, "C:\\Games\\Lantern Drift\\..\\x\\lantern_.exe")]:
        world.start(1.0, pid, image, name="lantern_.exe", window=2_073_600, draw=70.0)
    with pytest.raises(TargetNotFound, match="no program from the game's folder") as info:
        world.wait(deadline_s=5.0)
    assert info.value.candidates == [] and world.samples == 0


def test_another_session_ignored_names_and_lag_doctor_itself_are_never_the_game(world, monkeypatch):
    world.start(1.0, 71, GAME, session=2, window=2_073_600, draw=70.0)
    world.start(1.0, 72, GAME_DIR + "\\steamwebhelper.exe", window=2_073_600, draw=70.0)
    world.procs[ME] = proc(ME, SINCE + 1, "python.exe")            # this process, as if run from the folder
    world.images[ME] = GAME_DIR + "\\python.exe"
    world.wins[ME] = 2_073_600
    world.gpu[ME] = 70.0
    with pytest.raises(TargetNotFound, match="no program from the game's folder"):
        world.wait(deadline_s=5.0)
    assert world.reads[ME] == 0


def test_a_launcher_that_exits_between_samples_is_passed_over(world):
    world.start(0.9, 43, GAME_DIR + "\\launcher.exe", window=800_000, draw=30.0)
    world.exit(2.5, 43)                                           # drew on one sample, then started the game
    game_ct = world.start(2.8, 42, GAME, window=2_073_600, draw=50.0)
    found = world.wait()
    assert (found["pid"], found["create_time"]) == (42, game_ct)


def test_exclude_skips_a_process_the_caller_already_tried(monkeypatch):
    def machine():
        w = World(monkeypatch)
        launcher_ct = w.start(0.9, 43, GAME_DIR + "\\launcher.exe", window=800_000, draw=30.0)
        game_ct = w.start(6.0, 42, GAME, window=2_073_600, draw=50.0)
        return w, launcher_ct, game_ct

    w, launcher_ct, _ = machine()
    assert w.wait()["pid"] == 43                                  # a launcher that draws can win ...
    w, launcher_ct, game_ct = machine()
    found = w.wait(exclude=[(43, launcher_ct)])                   # ... and once tried, it is skipped
    assert (found["pid"], found["create_time"]) == (42, game_ct)


def test_when_several_draw_the_busiest_wins_unless_pick_chooses(world):
    world.start(0.9, 42, GAME, window=2_073_600, draw=60.0)
    world.start(0.9, 44, GAME_DIR + "\\editor.exe", window=800_000, draw=30.0)
    assert world.wait()["pid"] == 42
    seen = []

    def pick(cands):
        seen.append(sorted(c.pid for c in cands))
        return min(cands, key=lambda c: c.gpu_pct)

    assert world.wait(pick=pick)["pid"] == 44
    assert seen == [[42, 44]]
    with pytest.raises(WaitCancelled, match="no game was chosen"):
        world.wait(pick=lambda cands: None)
    stranger = tgt.Candidate(9, 9, "x.exe", 50.0, NV, True, False, 1)
    with pytest.raises(ValueError, match="one of the candidates"):
        world.wait(pick=lambda cands: stranger)


def test_between_two_that_draw_about_as_much_the_foreground_one_wins(world):
    world.start(0.9, 42, GAME, window=2_073_600, draw=50.0)
    world.start(0.9, 44, GAME_DIR + "\\editor.exe", window=800_000, draw=49.5)
    world.fg = 44
    assert world.wait()["pid"] == 44


def test_the_gpu_is_sampled_only_while_a_window_from_the_folder_exists(world):
    world.start(1.0, 41, STUB)
    world.start(1.0, 50, "C:\\Tools\\Painter\\painter.exe", window=900_000, draw=90.0)
    with pytest.raises(TargetNotFound, match="lantern.exe started from the game's folder but showed no window"):
        world.wait(deadline_s=8.0)
    assert world.samples == 0


@pytest.mark.parametrize("setup, words", [
    (lambda w: None, "no program from the game's folder (C:\\Games\\Lantern Drift) started within 30 seconds. "
                     "The store may still be downloading an update or waiting for an answer in its own window"),
    (lambda w: w.start(1.0, 41, STUB), "lantern.exe started from the game's folder but showed no window within "
                                       "30 seconds"),
    (lambda w: w.start(1.0, 42, GAME, window=600_000, draw=0.5),
     "a window from the game's folder (lantern_.exe) never drew within 30 seconds: it may be showing a message "
     "instead, such as a missing runtime"),
])
def test_the_deadline_says_how_far_the_wait_got(world, setup, words):
    setup(world)
    with pytest.raises(TargetNotFound) as info:
        world.wait(deadline_s=30.0)
    assert words in info.value.reason
    assert 30.0 <= world.t <= 31.5


def test_the_deadline_lists_the_folders_programs_it_saw(world):
    world.start(1.0, 42, GAME, window=600_000, draw=0.5)
    with pytest.raises(TargetNotFound) as info:
        world.wait(deadline_s=10.0)
    assert info.value.candidates == [{"pid": 42, "name": "lantern_.exe", "gpu_pct": 0.5, "has_window": True}]
    assert tgt._duration_words(300) == "5 minutes" and tgt._duration_words(1) == "1 second"


def test_cancel_ends_the_wait(world):
    cancel = threading.Event()
    world.start(1.0, 41, STUB)
    world.call(3.0, cancel.set)
    with pytest.raises(WaitCancelled, match="cancelled"):
        world.wait(cancel=cancel)
    assert world.t < 4.0


def test_when_the_gpu_counters_fail_a_window_that_stays_is_enough_and_it_is_said(world):
    world.gpu_fails = True
    game_ct = world.start(0.9, 42, GAME, window=2_073_600)
    found = world.wait()
    assert (found["pid"], found["create_time"]) == (42, game_ct)
    assert found["judged_by"] == "window" and found["adapter_luid"] is None
    assert world.samples == tgt.GPU_FAILS_TO_WINDOWS                # no sample after it judged by windows
    states = [s for s, _ in world.statuses]
    assert states == ["waiting", "started", "window", "windows-only"]
    assert "gave no reading" in world.statuses[3][1]["why"]


def test_two_failed_samples_are_not_yet_enough_to_judge_by_windows(world):
    world.gpu_fails = True
    world.start(0.9, 42, GAME, window=2_073_600)
    world.call(3.5, lambda: setattr(world, "gpu_fails", False))  # the counters come back after two failures
    with pytest.raises(TargetNotFound, match="never drew"):
        world.wait(deadline_s=10.0)
    assert "windows-only" not in [s for s, _ in world.statuses]


def test_failed_samples_count_only_in_a_row(world):
    world.fail_on = {1, 2, 4, 5, 6}                               # the third sample reads, and the game is not drawing
    game_ct = world.start(0.9, 42, GAME, window=2_073_600)
    found = world.wait()
    assert found["create_time"] == game_ct and found["judged_by"] == "window"
    assert world.samples == 6                                     # judged by windows only after 4, 5 and 6 failed


def test_a_junction_or_subst_folder_matches_by_its_real_path(world, monkeypatch):
    monkeypatch.setattr(tgt, "_real_folder", lambda folder: "E:\\Library\\Lantern Drift")
    game_ct = world.start(0.9, 42, "E:\\Library\\Lantern Drift\\lantern_.exe", window=2_073_600, draw=50.0)
    assert world.wait()["create_time"] == game_ct


def test_real_folder_names_only_another_plain_local_folder(monkeypatch):
    monkeypatch.setattr(tgt.os.path, "realpath", lambda p: "E:\\Library\\Lantern Drift")
    assert tgt._real_folder(GAME_DIR) == "E:\\Library\\Lantern Drift"
    monkeypatch.setattr(tgt.os.path, "realpath", lambda p: "c:/games/lantern drift/")
    assert tgt._real_folder(GAME_DIR) is None                     # itself
    monkeypatch.setattr(tgt.os.path, "realpath", lambda p: "\\\\server\\games\\Lantern Drift")
    assert tgt._real_folder(GAME_DIR) is None                     # a share is never taken


def test_the_wait_checks_its_arguments(world):
    for kw in [{"deadline_s": 0}, {"deadline_s": -1}, {"deadline_s": math.nan}, {"deadline_s": math.inf},
               {"deadline_s": tgt.MAX_WAIT_S + 1}, {"deadline_s": True}, {"since": -1}, {"since": True},
               {"since": 1.5}, {"exclude": [(0, 1)]}, {"exclude": [(4, -1)]}]:
        with pytest.raises(ValueError):
            world.wait(**kw)
    for folder in ["C:\\", "\\\\server\\share\\Lantern", "C:\\Games\\..\\Windows"]:
        with pytest.raises(ValueError):
            world.wait(folder)
    assert world.reads == Counter()


def test_by_default_a_process_from_just_before_the_wait_still_counts_and_an_older_one_does_not(world):
    now = tgt.filetime_now()
    for pid, create, pct in [(42, now - SECOND, 50.0), (43, now - 3 * SECOND, 90.0)]:
        world.procs[pid] = proc(pid, create, "lantern_.exe")      # 1 s before the call, and 3 s before it
        world.images[pid] = GAME
        world.wins[pid] = 2_073_600
        world.gpu[pid] = pct
    assert tgt.SINCE_MARGIN == 2 * SECOND
    found = world.wait(since=None)
    assert found["pid"] == 42 and world.reads[43] == 0


# ---------------------------------------------------------------- live ---

def test_filetime_now_is_the_clock_windows_stamps_process_create_times_with():
    tick = 200_000                                                # 20 ms: the coarsest system clock tick
    before = tgt.filetime_now()
    child = subprocess.Popen([BASE_PYTHON, "-I", "-c", "import time; time.sleep(20)"])
    try:
        after = tgt.filetime_now()
        p = ntquery.Snapshotter().snapshot().get(child.pid)
        assert p is not None
        assert before - tick <= p.create_time <= after + tick, (before, p.create_time, after)
    finally:
        child.kill()
        child.wait(timeout=5)


@pytest.mark.needs("gpu")
def test_a_real_laglab_window_is_found_by_its_folder(tmp_path):
    """Live: LagLab runs as the base interpreter, so its folder is that interpreter's. The wait must find the
    LagLab started after it began, by that folder, a window and real 3D use, and never this test process."""
    folder = tgt.check_folder(os.path.dirname(os.path.realpath(BASE_PYTHON)))
    pid_file = tmp_path / "laglab.pid"
    since = tgt.filetime_now()
    lab = subprocess.Popen([sys.executable, "-m", "laglab", "--mode", "smooth", "--duration", "8",
                            "--width", "480", "--height", "270", "--pid-file", str(pid_file)],
                           cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        states = []
        found = tgt.wait_for_target(folder, since=since, deadline_s=20.0,
                                    on_status=lambda state, info: states.append(state))
        assert pid_file.exists(), f"LagLab never started: {lab.stderr.read().decode(errors='replace')[-500:]}"
        lab_pid = json.loads(pid_file.read_text())["pid"]
        assert found["pid"] == lab_pid, found
        assert found["how"] == "folder" and found["judged_by"] == "3d"
        assert os.path.normcase(found["exe"]) == os.path.normcase(os.path.realpath(BASE_PYTHON))
        assert found["window_title"] and found["window_title"].startswith("LagLab")
        assert states[:1] == ["waiting"] and "window" in states
        procs = ntquery.Snapshotter().snapshot()
        assert tgt.accept_target(procs, found["pid"], found["create_time"], session=procs[os.getpid()].session_id,
                                 folder=folder, since=since, image=ntquery.image_path(found["pid"])) is None
    finally:
        lab.wait(timeout=20)
