"""The command line: argument checks, the elevation choice, and fix / undo through the journal.

Watch tests run cmd_watch in this process with a fake target and fake collectors, and the relay is faked too:
nothing here raises a UAC prompt. One elevated test keeps the real launch_elevated and replaces only ShellExecuteExW,
with a stand-in that runs the relay's own code on a thread of this process. The console-event tests run the real
cli.main in a hidden console of their own, so the events they send never reach this process's console.
"""
from __future__ import annotations

import _winapi
import copy
import ctypes
import gzip
import json
import math
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from ctypes import wintypes
from pathlib import Path

import pytest

from lagdoctor import cli, paths
from lagdoctor.capture import session as sess
from lagdoctor.capture import target as tgt
from lagdoctor.capture.base import Collector
from lagdoctor.fixes import catalog
from lagdoctor.fixes.base import GONE, Fix
from lagdoctor.fixes.journal import Journal
from lagdoctor.model import Capture
from lagdoctor.winapi import elevation

ROOT = Path(__file__).resolve().parents[1]
BASE_PY = getattr(sys, "_base_executable", sys.executable)
STORE: dict[str, object] = {}
GAME = {"pid": 4242, "create_time": 133000000000000001, "name": "Game.exe", "exe": r"C:\Games\Game.exe",
        "window_title": "Game", "monitor_device": None, "adapter_luid": None, "how": "pid", "candidates": []}
WATCH = ["watch", "--pid", "4242", "--duration", "0.2", "--no-network", "--hotkey", "none"]
FAKE_GAME_S = 10.0             # the faked game exits after this (see fake_world)


@pytest.fixture(autouse=True)
def _private_capture_lock(monkeypatch):
    """Each test gets its own lock name, so it never meets a real recording (or another test run)."""
    monkeypatch.setattr(sess, "CAPTURE_LOCK_NAME", f"Local\\lagdoctor-test-{uuid.uuid4().hex}")


@pytest.fixture(autouse=True)
def _no_real_uac(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("a test reached the real elevated launch")
    monkeypatch.setattr(elevation, "_ShellExecuteExW", refuse)


@pytest.fixture(autouse=True)
def _unelevated_owner(monkeypatch):
    """Every test here runs as the owner's unelevated terminal unless it says otherwise. CI's runner is an
    administrator with UAC off, and `fix` refuses to run elevated (test_fix_refuses_to_run_elevated covers that)."""
    monkeypatch.setattr(elevation, "is_elevated", lambda: False)


class FakeFix(Fix):
    id = "test_cli_fake"
    title = "Turn the fake setting up"

    def target(self, params):
        return f"fake setting {params['name']}"

    def read(self, params):
        return STORE.get(params["name"], GONE if params.get("gone") else 1)

    def write(self, params, value):
        STORE[params["name"]] = value

    def wanted(self, params, current):
        return 5


@pytest.fixture
def fake_fix():
    STORE.clear()
    catalog.register(FakeFix)
    yield FakeFix
    catalog._REGISTRY.pop(FakeFix.id, None)
    STORE.clear()


def write_last_diagnosis(fixes):
    d = paths.sessions_dir() / "20261104-180000-Game"
    d.mkdir(parents=True, exist_ok=True)
    diag = {"findings": [{"cause": "x", "title": "x", "fixes": fixes}]}
    (d / cli.DIAGNOSIS_NAME).write_text(json.dumps(diag), encoding="utf-8")
    (paths.home() / cli.LAST_SESSION).write_text(str(d), encoding="utf-8")
    return d


def test_watch_arguments_are_checked():
    p = cli.build_parser()
    a = p.parse_args(["watch", "--pid", "5", "--duration", "30", "--elevate", "--server", " 203.0.113.7 "])
    assert (a.pid, a.duration, a.elevate, a.server) == (5, 30.0, True, "203.0.113.7")
    for bad in (["watch", "--elevate", "--no-elevate"], ["watch", "--duration", "0"], ["watch", "--duration", "nan"],
                ["watch", "--server", "evil.example; calc"], ["watch", "--pid", "5", "--game", "x.exe"]):
        with pytest.raises(SystemExit):
            p.parse_args(bad)


def test_elevation_choice_literal_points():
    never = lambda *a, **k: pytest.fail("must not ask")  # noqa: E731
    assert cli.elevation_choice(True, False, False, never) == "elevate"
    assert cli.elevation_choice(False, True, True, never) == "skip"
    assert cli.elevation_choice(False, False, False, never) == "skip"       # no console to ask on: never elevate
    assert cli.elevation_choice(False, False, True, lambda *a, **k: True) == "elevate"
    assert cli.elevation_choice(False, False, True, lambda *a, **k: False) == "skip"


def test_a_mistyped_lag_key_is_refused_before_anything_starts():
    p = cli.build_parser()
    for bad in ("ctrl+alt+;", "ctl+alt+l", "ctrl+alt+f25", "ctrl+alt+lag"):
        with pytest.raises(SystemExit):
            p.parse_args(["watch", "--hotkey", bad])
    assert [p.parse_args(["watch", "--hotkey", k]).hotkey for k in ("none", "OFF", "", "Ctrl+Alt+K")] == \
        ["none", "none", "none", "ctrl+alt+k"]
    assert p.parse_args(["watch"]).hotkey == cli.DEFAULT_HOTKEY


def test_a_pid_of_zero_or_an_empty_game_name_is_refused():
    p = cli.build_parser()
    for bad in (["--pid", "0"], ["--pid", "-5"], ["--pid", "4294967296"], ["--pid", "x"], ["--game", ""],
                ["--game", "   "], ["--game", "a\x07.exe"]):
        with pytest.raises(SystemExit):
            p.parse_args(["watch", *bad])
    assert p.parse_args(["watch", "--pid", "4294967295"]).pid == 0xFFFFFFFF


def test_the_out_folder_is_made_absolute_where_it_comes_in(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert cli.build_parser().parse_args(["watch", "--out", "rel"]).out == (tmp_path / "rel").resolve()


def test_a_note_is_kept_as_one_clean_line_and_one_too_long_is_refused_before_anything_starts(capsys):
    p = cli.build_parser()
    assert p.parse_args(["watch"]).note is None
    assert p.parse_args(["watch", "--note", "  shadows\tto  low "]).note == "shadows to low"
    assert p.parse_args(["watch", "--note", " \x07 "]).note is None             # nothing printable: no note
    assert p.parse_args(["watch", "--note", "x" * 200]).note == "x" * 200      # exactly the limit
    # The limit counts what is kept: the spaces and control characters around a note do not count.
    assert p.parse_args(["watch", "--note", "\x07  " + "y" * 200 + "  \n"]).note == "y" * 200
    with pytest.raises(SystemExit):
        p.parse_args(["watch", "--note", "z" * 201])                         # refused, never cut
    assert "a note can be at most 200 characters, and this one has 201" in capsys.readouterr().err


def test_fix_then_undo_round_trip(fake_fix):
    write_last_diagnosis([{"title": "Turn it up", "fix_id": "test_cli_fake", "params": {"name": "a"}}])
    assert cli.main(["fix", "test_cli_fake", "--yes"]) == 0
    assert STORE["a"] == 5
    entries = Journal().undoable()
    assert [e.fix_id for e in entries] == ["test_cli_fake"] and entries[0].before == 1
    assert cli.main(["undo", "--all"]) == 0
    assert STORE["a"] == 1 and Journal().undoable() == []


def test_fix_asks_first_and_a_no_changes_nothing(fake_fix, monkeypatch):
    write_last_diagnosis([{"title": "Turn it up", "fix_id": "test_cli_fake", "params": {"name": "a"}}])
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert cli.main(["fix", "test_cli_fake"]) == 0
    assert "a" not in STORE and Journal().entries() == []


def test_fix_refuses_what_the_report_did_not_suggest(fake_fix):
    write_last_diagnosis([{"title": "Advice only", "fix_id": None, "params": {}}])
    assert cli.main(["fix", "test_cli_fake", "--yes"]) == 2
    assert STORE == {}


def test_fix_refuses_to_run_elevated(fake_fix, monkeypatch):
    write_last_diagnosis([{"title": "Turn it up", "fix_id": "test_cli_fake", "params": {"name": "a"}}])
    monkeypatch.setattr(cli, "_elevated", lambda: True)
    assert cli.main(["fix", "test_cli_fake", "--yes"]) == 2
    assert STORE == {}


def test_start_up_settles_an_interrupted_change_and_ended_session_changes(fake_fix):
    j = Journal()
    pending = j.begin("test_cli_fake", "fake setting a", {"name": "a"}, 1, 5)
    STORE["a"] = 5                                            # the write happened, then the tool crashed
    ended = j.begin("test_cli_fake", "fake setting b", {"name": "b", "gone": True}, 1, 5, session_only=True)
    j.set_state(ended.id, "applied")                          # its process has since exited (reads GONE)
    cli._settle_journal()
    assert Journal().get(pending.id).state == "applied"
    assert Journal().get(ended.id).state == "undone"


def test_a_fix_that_refuses_at_plan_time_is_reported_and_changes_nothing(fake_fix, monkeypatch, capsys):
    from lagdoctor.fixes.priority import Refused

    def refuse(self, params):
        raise Refused("that process is a Windows service, so lag-doctor leaves it alone")
    monkeypatch.setattr(FakeFix, "plan", refuse)
    write_last_diagnosis([{"title": "Turn it up", "fix_id": "test_cli_fake", "params": {"name": "a"}}])
    assert cli.main(["fix", "test_cli_fake", "--yes"]) == 1
    assert "Not changed: that process is a Windows service" in capsys.readouterr().out
    assert STORE == {} and Journal().entries() == []


def test_a_plans_notes_are_shown_before_asking(fake_fix, monkeypatch, capsys):
    real_plan = FakeFix.plan

    def with_note(self, params):
        p = real_plan(self, params)
        p.notes.append("this value's meaning is community-sourced")
        return p
    monkeypatch.setattr(FakeFix, "plan", with_note)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    write_last_diagnosis([{"title": "Turn it up", "fix_id": "test_cli_fake", "params": {"name": "a"}}])
    cli.main(["fix", "test_cli_fake"])
    assert "note:   this value's meaning is community-sourced" in capsys.readouterr().out


def test_fix_offers_are_deduplicated():
    diag = {"findings": [{"fixes": [{"fix_id": "a", "title": "A", "params": {"x": 1}}, {"fix_id": None}]},
                         {"fixes": [{"fix_id": "a", "title": "A again", "params": {"x": 1}},
                                    {"fix_id": "a", "title": "A other", "params": {"x": 2}}]}]}
    assert cli.fix_offers(diag) == [("a", "A", {"x": 1}), ("a", "A other", {"x": 2})]


def test_a_remembered_report_that_is_gone_is_said_and_not_replaced(tmp_path, fake_fix, capsys):
    # Replaces a test that pinned the old silent fallback to the newest other session (relative-out-elevated,
    # SEC-5): that session may be another game's, or an `analyze` of someone else's capture.
    d = paths.sessions_dir() / "20261104-170000-Old"
    d.mkdir(parents=True)
    (d / cli.DIAGNOSIS_NAME).write_text(json.dumps({"findings": [{"fixes": [
        {"title": "From an older, other report", "fix_id": "test_cli_fake", "params": {"name": "old"}}]}]}),
        encoding="utf-8")
    (paths.home() / cli.LAST_SESSION).write_text(str(tmp_path / "gone"), encoding="utf-8")
    assert cli.main(["fix", "--list"]) == 2
    out = capsys.readouterr().out
    assert "is gone" in out and str(tmp_path / "gone") in out
    assert "From an older, other report" not in out
    assert cli.main(["fix", "test_cli_fake", "--yes"]) == 2 and STORE == {}


def test_the_remembered_folder_is_absolute_whatever_folder_fix_runs_in(monkeypatch, tmp_path):
    a = tmp_path / "a"
    (a / "rel").mkdir(parents=True)
    (a / "rel" / cli.DIAGNOSIS_NAME).write_text(json.dumps({"findings": []}), encoding="utf-8")
    monkeypatch.chdir(a)
    cli._remember(Path("rel"))
    monkeypatch.chdir(tmp_path)
    folder, diag, problem = cli.remembered_report(cli.LAST_SESSION, "the last watch")
    assert problem is None and diag == {"findings": []} and folder == (a / "rel").resolve()


def test_a_relative_remembered_folder_is_not_read_against_the_current_folder(monkeypatch, tmp_path):
    (tmp_path / "rel").mkdir()
    (tmp_path / "rel" / cli.DIAGNOSIS_NAME).write_text(json.dumps({"findings": []}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    (paths.home() / cli.LAST_SESSION).write_text("rel", encoding="utf-8")       # as older versions wrote it
    folder, diag, problem = cli.remembered_report(cli.LAST_SESSION, "the last watch")
    assert diag is None and "is gone" in problem


def write_checkup(fixes):
    d = paths.sessions_dir() / "20261104-190000-check-up"
    d.mkdir(parents=True, exist_ok=True)
    (d / cli.DIAGNOSIS_NAME).write_text(json.dumps({"findings": [{"fixes": fixes}]}), encoding="utf-8")
    (paths.home() / cli.LAST_CHECKUP).write_text(str(d), encoding="utf-8")
    return d


def _age(folder: Path, seconds: float) -> None:
    t = time.time() - seconds
    os.utime(folder / cli.DIAGNOSIS_NAME, (t, t))


@pytest.mark.parametrize("newer", ["check-up", "watch"])
def test_fix_merges_the_last_watch_and_the_last_check_up_newest_first(fake_fix, capsys, newer):
    watch = write_last_diagnosis([{"title": "A from the watch", "fix_id": "test_cli_fake", "params": {"name": "w"}},
                                  {"title": "B", "fix_id": "only_in_the_watch", "params": {}}])
    checkup = write_checkup([{"title": "A from the check-up", "fix_id": "test_cli_fake", "params": {"name": "c"}},
                             {"title": "C", "fix_id": "only_in_the_check_up", "params": {}}])
    _age(watch if newer == "check-up" else checkup, 100)
    offers = cli.merged_offers(cli.fix_sources()[0])
    a = (("test_cli_fake", "A from the check-up", {"name": "c"}, "the last check-up") if newer == "check-up" else
         ("test_cli_fake", "A from the watch", {"name": "w"}, "the last watch"))
    assert sorted(offers) == sorted([a, ("only_in_the_watch", "B", {}, "the last watch"),
                                     ("only_in_the_check_up", "C", {}, "the last check-up")])
    assert cli.main(["fix", "--list"]) == 0
    listed = capsys.readouterr().out
    assert f"Fixes suggested by the last watch: {watch}" in listed and f"by the last check-up: {checkup}" in listed
    assert re.search(r"only_in_the_check_up +C +\[the last check-up\]", listed)
    assert cli.main(["fix", "test_cli_fake", "--yes"]) == 0
    assert STORE == ({"c": 5} if newer == "check-up" else {"w": 5})


def test_the_fix_commands_a_check_up_prints_can_be_run(monkeypatch, capsys):
    from tests import synth
    inv = copy.deepcopy(synth.laglab("smooth").cap.inventory)
    inv["power"] = {"scheme_guid": "64a64f24-65b9-4b56-befd-5ec1eaced9b3", "scheme_name": "Silent",
                    "schemes": [["381b4222-f694-41f0-9685-ff5bb260df2e", "Balanced"]], "slider": "balanced",
                    "on_battery": False, "saver": False}
    inv["settings"].update(game_mode=False)
    monkeypatch.setattr(sess, "_collect_inventory", lambda target: copy.deepcopy(inv))
    assert cli.main(["check"]) == 0
    printed = re.findall(r"run: lagdoctor fix (\S+)", capsys.readouterr().out)
    assert "power_plan" in printed
    assert cli.main(["fix", "--list"]) == 0
    listed = capsys.readouterr().out
    assert "the last check-up" in listed
    assert all(re.search(rf"^\s+{re.escape(fid)}\s", listed, flags=re.M) for fid in printed), listed
    assert not (paths.home() / cli.LAST_SESSION).exists()          # a check-up never replaces the last watch


# ------------------------------------------------------------------ watch, with fakes ---

class Quick(Collector):
    def __init__(self, name, result):
        super().__init__()
        self.name, self._result = name, result

    def run(self):
        self.set_status("ok", tool="fake")
        for _ in self.ticks(0.02):
            pass

    def result(self):
        return self._result


@pytest.fixture
def fake_world(monkeypatch):
    """A running 'game' (GAME), no PresentMon, and a session of quick fake collectors. Records what was asked."""
    seen: dict[str, list] = {"find": [], "sessions": []}

    def find_target(**kw):
        seen["find"].append(kw)
        return dict(GAME)
    monkeypatch.setattr(tgt, "find_target", find_target)
    monkeypatch.setattr(cli, "_presentmon_exe", lambda: (None, "PresentMon is not installed (test)"))
    from lagdoctor import setup as pm_setup             # a PresentMon handed to the elevated child passes its check
    monkeypatch.setattr(pm_setup, "verify", lambda p, **kw: {"signer": "Intel Corporation", "note": ""})
    monkeypatch.setattr(sess, "_collect_inventory", lambda target: {"adapters": [], "who": "the user"})

    class QuickSession(sess.Session):
        def __init__(self, target, opts, inventory=None, printer=None, observers=None, **kw):
            seen["sessions"].append({"target": target, "opts": opts, "inventory": inventory, "printer": printer})
            cols = [Quick("system", {"columns": {"t": [0.0, 0.25], "cpu_total_pct": [10.0, 11.0]}}),
                    Quick("frames", {"columns": {"t": [0.1, 0.12, 0.14], "frame_ms": [16.7, 16.6, 16.8]}})]
            # The fake game ends by itself: a recording a test did not expect (no --duration) ends, never hangs.
            ends = time.monotonic() + FAKE_GAME_S
            super().__init__(target, opts, collectors=cols, inventory=inventory or {"adapters": []},
                             alive=lambda: time.monotonic() < ends, printer=lambda line: None, observers=observers)
    monkeypatch.setattr(sess, "Session", QuickSession)
    return seen


def test_auto_is_chosen_only_when_no_game_was_named(fake_world, tmp_path):
    for argv, want in ((["--auto"], {"auto": True, "pid": None, "name": None}),
                       ([], {"auto": True, "pid": None, "name": None}),
                       (["--pid", "4242"], {"auto": False, "pid": 4242, "name": None}),
                       (["--game", "Game.exe"], {"auto": False, "pid": None, "name": "Game.exe"})):
        fake_world["find"].clear()
        code = cli.main(["watch", *argv, "--duration", "0.1", "--no-network", "--hotkey", "none", "--no-elevate",
                         "--out", str(tmp_path / "out")])
        assert code == 0 and fake_world["find"] == [want]


@pytest.mark.parametrize("flags", [[], ["--elevate"], ["--game", "Game.exe"]])
def test_a_watch_without_presentmon_stops_before_it_even_looks_for_the_game(fake_world, capsys, tmp_path, flags):
    # A 14-minute recording was once made this way, its frames missing, the reason only in the report afterwards.
    assert cli.main(["watch", *flags, "--no-network", "--hotkey", "none",
                     "--out", str(tmp_path / "out")]) == cli.EXIT_NOT_READY
    out = capsys.readouterr().out
    assert out.startswith("Frame timing is not available: PresentMon is not installed (test)")
    assert "Nothing was recorded." in out
    # --elevate and --no-elevate cannot go together: the advice swaps one for the other.
    assert ("use --no-elevate instead of --elevate" if "--elevate" in flags else "add --no-elevate") in out
    assert out.count("lagdoctor setup") == 0              # the reason itself says what to run (here: the fake's)
    assert fake_world["find"] == [] and fake_world["sessions"] == [] and not (tmp_path / "out").exists()


def test_a_watch_without_frame_timing_says_so_before_it_records(fake_world, capsys, tmp_path):
    assert cli.main(["watch", "--pid", "4242", "--no-elevate", "--duration", "0.1", "--no-network",
                     "--hotkey", "none", "--out", str(tmp_path / "out")]) == 0
    out = capsys.readouterr().out
    said = out.find("Frame timing is off for this recording: PresentMon is not installed (test)")
    assert 0 <= said < out.index("Recording. Play normally")


def test_a_zero_pid_that_got_past_the_parser_is_not_turned_into_auto(capsys):
    args = cli.build_parser().parse_args(["watch", "--pid", "5", "--no-elevate"])
    args.pid = 0                                   # another caller: the real find_target must refuse it plainly
    assert cli.cmd_watch(args) == cli.EXIT_BAD_ARGS
    assert "Could not find the game" in capsys.readouterr().out


def test_a_lock_that_cannot_be_checked_refuses_to_record(fake_world, monkeypatch, tmp_path, capsys):
    def broken(name):
        raise OSError(6, "The handle is invalid")
    monkeypatch.setattr(elevation, "hold_named_mutex", broken)
    assert cli.main([*WATCH, "--no-elevate", "--out", str(tmp_path / "out")]) == cli.EXIT_BUSY
    assert "could not check" in capsys.readouterr().out and fake_world["sessions"] == []


def test_a_second_watch_is_refused_before_anything_starts(fake_world, tmp_path, capsys):
    other = sess.CaptureLock()                    # another recording, holding this test's lock name
    assert other.acquire()
    try:
        plain = cli.main([*WATCH, "--no-elevate", "--out", str(tmp_path / "out")])
        elevating = cli.main([*WATCH, "--elevate", "--out", str(tmp_path / "out")])
    finally:
        other.release()
    assert plain == elevating == cli.EXIT_BUSY
    assert "already running" in capsys.readouterr().out
    assert fake_world["find"] == [] and fake_world["sessions"] == []          # nothing sampled, nothing started
    assert cli.main([*WATCH, "--no-elevate", "--out", str(tmp_path / "out")]) == 0    # free again


def test_ctrl_c_during_the_save_does_not_lose_the_capture(fake_world, monkeypatch, tmp_path, capsys):
    real_save = Capture.save

    def save_with_a_press(self, path):
        signal.raise_signal(signal.SIGINT)              # the user presses Ctrl+C while the file is written
        return real_save(self, path)
    monkeypatch.setattr(Capture, "save", save_with_a_press)
    assert cli.main([*WATCH, "--no-elevate", "--out", str(tmp_path / "out")]) == 0
    assert len(list((tmp_path / "out").glob(f"*/{cli.CAPTURE_NAME}"))) == 1
    text = capsys.readouterr().out
    assert "Saving 3 frames..." in text and "Analysing..." in text
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def test_ctrl_c_during_the_analysis_says_where_the_saved_capture_is(fake_world, monkeypatch, tmp_path, capsys):
    from lagdoctor.analysis import diagnose as diagnose_mod

    def interrupted(cap):
        raise KeyboardInterrupt
    monkeypatch.setattr(diagnose_mod, "diagnose", interrupted)
    assert cli.main([*WATCH, "--no-elevate", "--out", str(tmp_path / "out")]) == 130
    (cap_path,) = (tmp_path / "out").glob(f"*/{cli.CAPTURE_NAME}")
    assert f'lagdoctor analyze "{cap_path}"' in capsys.readouterr().out


def test_a_watch_registers_a_console_handler_only_while_it_records_and_saves(fake_world, monkeypatch, tmp_path):
    calls = []
    real = cli._kernel32.SetConsoleCtrlHandler

    def spy(routine, add):
        calls.append((bool(add), Path(tmp_path / "out").exists() and bool(list((tmp_path / "out").glob("*/*.gz")))))
        return real(routine, add)
    monkeypatch.setattr(cli._kernel32, "SetConsoleCtrlHandler", spy)
    assert cli.main([*WATCH, "--no-elevate", "--out", str(tmp_path / "out")]) == 0
    assert calls == [(True, False), (False, True)]          # added before recording, removed after the save


def test_the_console_handler_stops_on_ctrl_break_and_waits_for_the_save_on_close(monkeypatch):
    from lagdoctor.winapi import etw
    stopped = []
    monkeypatch.setattr(etw, "stop_session", lambda name: stopped.append(name) or 0)
    stop, saved = threading.Event(), threading.Event()
    ctl = cli._ConsoleControl(stop, saved, network=True)
    assert ctl._handle(cli.CTRL_C_EVENT) is False and not stop.is_set()        # Ctrl+C stays with Python
    assert ctl._handle(cli.CTRL_BREAK_EVENT) is True and stop.is_set() and stop.reason == "Ctrl+Break"
    assert stopped == []
    stop, saved = threading.Event(), threading.Event()
    ctl = cli._ConsoleControl(stop, saved, network=True)
    threading.Timer(0.3, saved.set).start()
    t0 = time.monotonic()
    assert ctl._handle(cli.CTRL_CLOSE_EVENT) is True
    waited = time.monotonic() - t0
    assert stop.reason == "window closed" and stopped == ["lag-doctor-net"] and 0.25 <= waited < 2.0
    monkeypatch.setattr(cli, "CLOSE_SAVE_WAIT_S", 0.2)                          # a save that never comes
    t0 = time.monotonic()
    assert cli._ConsoleControl(threading.Event(), threading.Event(), network=False)._handle(cli.CTRL_CLOSE_EVENT)
    assert time.monotonic() - t0 < 1.0


def test_closing_the_window_is_a_stop_the_flow_tracker_asked_for(monkeypatch):
    """The real console handler beside a real FlowTracker over a fake ETW session whose consumer ends when the
    session is stopped by name. The handler must say it is ours before it stops it: a consumer check in
    between would otherwise read 'the ETW consumer stopped by itself' and mark the flows partial."""
    from lagdoctor import clock
    from lagdoctor.capture import netflows as nf
    from lagdoctor.winapi import etw
    made = []

    class NamedSession:
        def __init__(self, name, providers, on_event):
            self.name, self.alive = name, False
            made.append(self)

        def start(self):
            self.alive = True

        def stop(self):
            self.alive = False

        def health(self):
            return {"events": 0, "lost": 0, "callback_errors": 0, "pump_alive": self.alive,
                    "process_trace_rc": None if self.alive else 0}

    def stop_by_name(name):
        for s in made:
            if s.name == name:
                s.alive = False
        time.sleep(0.6)                        # the tracker checks its consumer meanwhile (every 0.25 s here)
        return 0
    monkeypatch.setattr(nf, "PUMP_EVERY_S", 0.1)
    monkeypatch.setattr(etw, "stop_session", stop_by_name)
    tr = nf.FlowTracker((4242,), selftest=False, local_addrs=lambda: {"127.0.0.1"}, session_factory=NamedSession)
    tr.start(clock.now())
    try:
        deadline = time.monotonic() + 5
        while not (made and made[0].alive) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert made and made[0].name == "lag-doctor-net"
        stop, saved = threading.Event(), threading.Event()
        saved.set()
        assert cli._ConsoleControl(stop, saved, network=True)._handle(cli.CTRL_CLOSE_EVENT)
        assert stop.reason == "window closed" and not made[0].alive
    finally:
        tr.stop()
    st = tr.status()
    assert "stopped by itself" not in st.detail, st
    assert tr.result()["health"]["consumer_stopped_t"] is None


# ------------------------------------------------------------------ frame timing through the relay ---

class P:
    """A process as the handle-free snapshot lists it (the fields the relay's checks read)."""

    def __init__(self, pid, ppid, create_time, session_id, name="python.exe"):
        self.pid, self.ppid, self.create_time, self.session_id, self.name = pid, ppid, create_time, session_id, name


def _session_now() -> int:
    from lagdoctor.winapi import ntquery
    return ntquery.Snapshotter().snapshot()[os.getpid()].session_id


class FakeLink:
    """What relay/client.launch returns: a relay that runs frame timing (and the tracker) for this recording."""

    def __init__(self, network, events):
        self.pid, self.create_time, self.frames, self.network = 7777, 1234, True, network
        self.events, self.closed = events, 0

    def tool_procs(self):
        return ((self.pid, self.create_time),)

    def close(self):
        self.closed += 1
        self.events.append("relay let go")


@pytest.fixture
def fake_relay(monkeypatch, fake_world, tmp_path):
    """This window may trace only by elevating; relay/client.launch is replaced: it records its arguments and returns
    a FakeLink, or raises state['raise']. The snapshot shows this process and GAME in this Windows session."""
    from lagdoctor.relay import client
    session = _session_now()
    state = {"launches": [], "link": None, "raise": None, "events": [], "session": session,
             "procs": {os.getpid(): P(os.getpid(), 4, 555, session), GAME["pid"]: P(GAME["pid"], 4, GAME["create_time"],
                                                                                     session, GAME["name"])}}
    monkeypatch.setattr(cli, "_presentmon_exe", lambda: (tmp_path / "PresentMon-2.6.0-x64.exe", ""))
    monkeypatch.setattr(elevation, "can_trace", lambda: False)
    monkeypatch.setattr(sess, "_snapshot", lambda threads_for=(): dict(state["procs"]))

    def launch(target, **kw):
        state["launches"].append(dict(kw, target=dict(target)))
        if state["raise"] is not None:
            raise state["raise"]
        state["link"] = FakeLink(kw["network"], state["events"])
        return state["link"]
    monkeypatch.setattr(client, "launch", launch)
    real_save = Capture.save

    def save(self, path):
        state["events"].append("saved")
        return real_save(self, path)
    monkeypatch.setattr(Capture, "save", save)
    return state


def test_an_elevated_watch_records_here_with_frame_timing_through_the_relay(fake_relay, fake_world, tmp_path):
    held = []
    real_run = sess.Session.run

    def run(self, *a, **k):
        probe = sess.CaptureLock()
        held.append(not probe.acquire())               # this window holds the lock while it records
        probe.release()
        return real_run(self, *a, **k)
    sess.Session.run = run
    try:
        assert cli.main(["watch", "--pid", "4242", "--elevate", "--duration", "0.2", "--hotkey", "none",
                         "--out", str(tmp_path / "out")]) == 0
    finally:
        sess.Session.run = real_run
    (launch,) = fake_relay["launches"]
    assert launch["target"]["pid"] == GAME["pid"] and launch["target"]["create_time"] == GAME["create_time"]
    assert launch["presentmon_exe"] == (tmp_path / "PresentMon-2.6.0-x64.exe").resolve()
    assert launch["frames"] is True and launch["network"] is True
    assert launch["recorder"] == (os.getpid(), 555) and launch["sid"] == elevation.token_user_sid()
    (session,) = fake_world["sessions"]
    link = fake_relay["link"]
    assert session["opts"].relay is link and session["opts"].frames is True
    assert session["opts"].tool_procs == ((7777, 1234),) and session["inventory"] is None   # read here, by the session
    (cap,) = (tmp_path / "out").glob(f"*/{cli.CAPTURE_NAME}")
    assert (cap.parent / cli.DIAGNOSIS_NAME).is_file() and held == [True]
    events = fake_relay["events"]
    assert events[:2] == ["relay let go", "saved"] and set(events) == {"relay let go", "saved"}   # let go, then saved


def test_without_the_network_the_relay_runs_frame_timing_only(fake_relay, tmp_path):
    assert cli.main([*WATCH, "--elevate", "--out", str(tmp_path / "out")]) == 0
    assert fake_relay["launches"][0]["network"] is False


def test_the_note_stays_with_the_recorder_and_never_goes_to_the_relay(fake_relay, tmp_path):
    note = "shadows to low"
    assert cli.main([*WATCH, "--elevate", "--note", note, "--out", str(tmp_path / "out")]) == 0
    (launch,) = fake_relay["launches"]
    assert note not in repr(launch)                         # nothing the relay is started with holds it
    (path,) = (tmp_path / "out").glob(f"*/{cli.CAPTURE_NAME}")
    assert Capture.load(path).note == note                  # the recorder kept it


def test_a_watch_keeps_its_note_in_the_capture_the_summary_and_the_report_and_analyze_keeps_it(fake_world,
                                                                                            tmp_path):
    from lagdoctor.report import summary
    note = "installed FrameFix"
    assert cli.main([*WATCH, "--no-elevate", "--note", note, "--out", str(tmp_path / "out")]) == 0
    (path,) = (tmp_path / "out").glob(f"*/{cli.CAPTURE_NAME}")
    assert Capture.load(path).note == note and summary.read(path.parent)["note"] == note
    shown = f"<strong>Your note:</strong> {note}</p>"
    assert shown in (path.parent / cli.REPORT_NAME).read_text(encoding="utf-8")
    # The note is the capture's: analysing the capture again keeps it.
    assert cli.main(["analyze", str(path)]) == 0
    (again,) = [d for d in paths.sessions_dir().iterdir() if (d / cli.ANALYZED_FROM).is_file()]
    assert summary.read(again)["kind"] == "analyze" and summary.read(again)["note"] == note
    assert shown in (again / cli.REPORT_NAME).read_text(encoding="utf-8")
    assert cli.main([*WATCH, "--no-elevate", "--out", str(tmp_path / "plain")]) == 0          # without --note
    (plain,) = (tmp_path / "plain").glob(f"*/{cli.CAPTURE_NAME}")
    assert Capture.load(plain).note is None and summary.read(plain.parent)["note"] is None


def test_with_a_relay_closing_the_window_leaves_the_network_trace_to_the_relay(fake_relay, monkeypatch, tmp_path):
    seen = []
    real = cli._ConsoleControl.__init__

    def init(self, stop, saved, network, **kw):
        seen.append(network)
        real(self, stop, saved, network, **kw)
    monkeypatch.setattr(cli._ConsoleControl, "__init__", init)
    assert cli.main(["watch", "--pid", "4242", "--elevate", "--duration", "0.2", "--hotkey", "none",
                     "--out", str(tmp_path / "out")]) == 0
    assert seen == [False]                  # this window never stops the relay's ETW session by name


def test_a_declined_prompt_records_here_still_holding_the_lock(fake_relay, monkeypatch, tmp_path, capsys):
    fake_relay["raise"] = PermissionError(13, "the administrator prompt was declined", None, 1223)
    held_while_recording = []
    real_run = sess.Session.run

    def run(self, *a, **k):
        probe = sess.CaptureLock()
        held_while_recording.append(not probe.acquire())
        probe.release()
        return real_run(self, *a, **k)
    monkeypatch.setattr(sess.Session, "run", run)
    assert cli.main(["watch", "--pid", "4242", "--duration", "0.1", "--elevate", "--no-network", "--hotkey", "none",
                     "--out", str(tmp_path / "out")]) == 0
    out = capsys.readouterr().out
    assert "The UAC prompt was declined, so lag-doctor records without frame timing." in out
    assert held_while_recording == [True]
    said = out.find("Frame timing is off for this recording: it needs admin rights")
    assert 0 <= said < out.index("Recording. Play normally")          # said before, not only in the report
    assert len(list((tmp_path / "out").glob(f"*/{cli.CAPTURE_NAME}"))) == 1


@pytest.mark.parametrize("code,exit_code,said", [
    (6, cli.EXIT_NOT_READY, "PresentMon failed its check"),
    (3, cli.EXIT_TARGET_CHANGED, "exited while the UAC prompt was up"),
    (4, 1, "Frame timing could not be started: the relay ended before it answered"),
    (None, 1, "Frame timing could not be started: another program answered")])
def test_a_relay_that_could_not_record_is_explained_and_nothing_is_recorded(fake_relay, fake_world, tmp_path, capsys,
                                                                            code, exit_code, said):
    from lagdoctor.relay import client
    why = "another program answered on the relay's pipe" if code is None else f"the relay ended before it answered"
    fake_relay["raise"] = client.RelayFailed(why, code)
    assert cli.main([*WATCH, "--elevate", "--out", str(tmp_path / "out")]) == exit_code
    assert said in capsys.readouterr().out
    assert fake_world["sessions"] == [] and not (tmp_path / "out").exists()


def test_a_relay_command_that_was_refused_is_said_plainly_and_records_here(fake_relay, fake_world, tmp_path, capsys):
    fake_relay["raise"] = ValueError("frame timing through the relay needs PresentMon's absolute path")
    assert cli.main([*WATCH, "--elevate", "--out", str(tmp_path / "out")]) == 0
    out = capsys.readouterr().out
    assert "was not started, because its command was refused" in out and "Traceback" not in out
    (session,) = fake_world["sessions"]
    assert session["opts"].frames is False and session["opts"].relay is None


def test_a_game_the_relay_would_refuse_is_refused_before_the_prompt(fake_relay, fake_world, tmp_path, capsys):
    fake_relay["procs"][GAME["pid"]] = P(GAME["pid"], 4, GAME["create_time"], fake_relay["session"] + 1, "Game.exe")
    assert cli.main([*WATCH, "--elevate", "--out", str(tmp_path / "out")]) == cli.EXIT_BAD_ARGS
    assert "another Windows session" in capsys.readouterr().out
    assert fake_relay["launches"] == [] and fake_world["sessions"] == []


@pytest.mark.parametrize("where", ["recording", "before it records"])
def test_the_relay_is_let_go_when_the_recording_fails(fake_relay, monkeypatch, tmp_path, where):
    def broken(*a, **k):
        raise RuntimeError("a bug")
    if where == "recording":
        monkeypatch.setattr(sess.Session, "run", broken)
    else:                                                  # the session folder cannot be made: no session at all
        monkeypatch.setattr(sess, "session_dir", broken)
    with pytest.raises(RuntimeError):
        cli.main([*WATCH, "--elevate", "--out", str(tmp_path / "out")])
    assert fake_relay["link"].closed >= 1


def test_a_relative_lagdoctor_home_still_hands_the_relay_an_absolute_presentmon(fake_relay, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LAGDOCTOR_HOME", "relative-home")
    monkeypatch.setattr(cli, "_presentmon_exe", lambda: (Path("relative-home") / "PresentMon-2.6.0-x64.exe", ""))
    assert cli.main(["watch", "--pid", "4242", "--elevate", "--duration", "0.1", "--no-network", "--hotkey",
                     "none"]) == 0
    exe = fake_relay["launches"][0]["presentmon_exe"]
    assert exe.is_absolute() and exe == (tmp_path / "relative-home" / "PresentMon-2.6.0-x64.exe").resolve()


# A member of Performance Log Users may trace frames unelevated, but Windows does not let the group enable the
# game-server tracker's provider (measured, docs/research/install-and-rights.md E1): only --elevate brings it.

@pytest.fixture
def group_member(fake_relay, monkeypatch):
    monkeypatch.setattr(elevation, "can_trace", lambda: True)
    monkeypatch.setattr(elevation, "is_elevated", lambda: False)
    return fake_relay


def test_a_group_member_records_frames_here_without_a_prompt(group_member, tmp_path, capsys):
    assert cli.main([*WATCH, "--out", str(tmp_path / "out")]) == 0
    out = capsys.readouterr().out
    assert group_member["launches"] == [] and "Frame timing is off" not in out
    assert len(list((tmp_path / "out").glob(f"*/{cli.CAPTURE_NAME}"))) == 1


def test_elevate_still_elevates_a_group_member_for_the_game_server_tracker(group_member, tmp_path):
    assert cli.main([*WATCH, "--elevate", "--out", str(tmp_path / "out")]) == 0
    assert len(group_member["launches"]) == 1


def test_an_administrator_window_is_never_elevated_again(group_member, monkeypatch, tmp_path):
    monkeypatch.setattr(elevation, "is_elevated", lambda: True)
    assert cli.main([*WATCH, "--elevate", "--out", str(tmp_path / "out")]) == 0
    assert group_member["launches"] == []


@pytest.mark.parametrize("network, said", [(False, "records here, with frame timing"),
                                           (True, "records without the game-server tracker")])
def test_a_group_member_who_declines_still_records_frames_here(group_member, tmp_path, capsys, network, said):
    group_member["raise"] = PermissionError(13, "the administrator prompt was declined", None, 1223)
    argv = [a for a in WATCH if network is False or a != "--no-network"]
    assert cli.main([*argv, "--elevate", "--out", str(tmp_path / "out")]) == 0
    out = capsys.readouterr().out
    assert f"The UAC prompt was declined, so lag-doctor {said}." in out
    assert "Frame timing is off" not in out and "without frame timing" not in out   # the group still gives it
    assert len(list((tmp_path / "out").glob(f"*/{cli.CAPTURE_NAME}"))) == 1


def _split_command_line(program: str, params: str) -> list[str]:
    """How the relay's command line is split (CommandLineToArgvW), program first."""
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    k32 = ctypes.WinDLL("kernel32")
    k32.LocalFree.argtypes = [ctypes.c_void_p]
    n = ctypes.c_int()
    argv = shell32.CommandLineToArgvW(f'"{program}" {params}', ctypes.byref(n))
    assert argv, f"CommandLineToArgvW failed: {ctypes.get_last_error()}"
    try:
        return [argv[i] for i in range(n.value)]
    finally:
        k32.LocalFree(ctypes.cast(argv, ctypes.c_void_p))


class ShellExecuteRelay:
    """Stands in for ShellExecuteExW behind the real launch_elevated: records what would run elevated, runs the relay's
    own main() on a thread of this process (unelevated, its PresentMon and ETW faked), and hands back a handle to this
    process, whose pid is the relay's here."""

    def __init__(self, parts) -> None:
        self.parts = parts
        self.calls: list[dict] = []
        self.codes: list[int] = []
        self.threads: list[threading.Thread] = []

    def __call__(self, ref) -> bool:
        from lagdoctor.relay import server
        info = ref._obj
        argv = _split_command_line(info.lpFile, info.lpParameters)
        self.calls.append({"verb": info.lpVerb, "file": info.lpFile, "params": info.lpParameters,
                           "dir": info.lpDirectory, "argv": argv, "show": info.nShow})
        t = threading.Thread(target=lambda: self.codes.append(server.main(argv[6:], owner="", parts=self.parts)),
                             daemon=True)
        t.start()
        self.threads.append(t)
        me = _winapi.GetCurrentProcess()
        info.hProcess = _winapi.DuplicateHandle(me, me, me, 0, False, _winapi.DUPLICATE_SAME_ACCESS)
        return True


def test_an_elevated_watch_starts_the_real_relay_through_the_real_launch(fake_world, monkeypatch, tmp_path):
    """Everything but the prompt is real: cli -> relay/client.launch -> launch_elevated -> the relay's own checks and
    pipe -> the recorder's checks of it. Only ShellExecuteExW and the relay's PresentMon and ETW are stand-ins, and the
    owner the recorder requires is this token's (unelevated, no pipe here can be owned by Administrators)."""
    from lagdoctor.relay import client, server
    from lagdoctor.winapi import ntquery, pipe
    monkeypatch.setattr(elevation, "can_trace", lambda: False)
    monkeypatch.setattr(cli, "_presentmon_exe", lambda: (Path(sys.executable).resolve(), ""))
    with pipe.PipeServer(f"lagdoctor-owner-{uuid.uuid4().hex[:16]}",
                         pipe.client_dacl([elevation.token_user_sid()])) as probe:
        monkeypatch.setattr(client, "ADMINISTRATORS", probe.owner_sid())
    game = subprocess.Popen([BASE_PY, "-I", "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
    try:
        snap = ntquery.Snapshotter()
        deadline = time.monotonic() + 10
        while game.pid not in snap.snapshot() and time.monotonic() < deadline:
            time.sleep(0.02)
        real_game = dict(GAME, pid=game.pid, create_time=snap.snapshot()[game.pid].create_time)
        monkeypatch.setattr(tgt, "find_target", lambda **kw: dict(real_game))

        class Etw:
            def __init__(self, on_event):
                pass

            def start(self):
                pass

            def stop(self):
                pass

            def health(self):
                return {"pump_alive": True}
        rec = ShellExecuteRelay(server.Parts(verify=lambda exe: None, etw=Etw))
        monkeypatch.setattr(elevation, "_ShellExecuteExW", rec)
        assert cli.main(["watch", "--pid", str(game.pid), "--elevate", "--duration", "0.2", "--hotkey", "none",
                         "--out", str(tmp_path / "out")]) == 0
    finally:
        game.stdin.close()
        game.wait(10)
    (call,) = rec.calls
    assert call["verb"] == "runas" and call["file"] == str(client.relay_python())
    assert call["dir"] == str(client.PROJECT_ROOT) and Path(call["dir"], "lagdoctor", "relay").is_dir()
    assert call["argv"][1:6] == ["-B", "-E", "-s", "-m", "lagdoctor.relay"] and all(call["argv"])
    cfg = server.parse_args(call["argv"][6:])
    assert cfg.game == (real_game["pid"], real_game["create_time"]) and cfg.frames and cfg.network
    assert cfg.recorder[0] == os.getpid() and cfg.sid == elevation.token_user_sid()
    rec.threads[0].join(15)
    assert rec.codes == [0]                                # the relay ended cleanly once it was let go
    (session,) = fake_world["sessions"]
    assert session["opts"].relay is not None and session["opts"].relay.pid == os.getpid()
    assert len(list((tmp_path / "out").glob(f"*/{cli.CAPTURE_NAME}"))) == 1


# ------------------------------------------- watch for a caller other than this console (M2's seams) ---

def _watch_args(*extra):
    return cli.build_parser().parse_args(["watch", "--pid", "4242", "--hotkey", "none", *extra])


def test_a_watch_says_every_line_through_the_say_it_was_given(fake_world, tmp_path, capsys):
    lines = []
    args = _watch_args("--no-elevate", "--duration", "0.1", "--out", str(tmp_path / "out"))
    assert cli.cmd_watch(args, say=lines.append) == 0
    assert capsys.readouterr().out == ""                  # nothing reached this console
    said = "\n".join(lines)
    for text in ("Watching Game.exe (pid 4242", "Frame timing is off for this recording", "Recording. Play normally",
                 "Saving", "Analysing...", cli.REPORT_NAME):     # the summary names the report
        assert text in said, (text, lines)


def test_a_busy_recording_is_said_through_the_given_say(fake_world, tmp_path, capsys):
    other = sess.CaptureLock()
    assert other.acquire()
    lines = []
    try:
        assert cli.cmd_watch(_watch_args("--no-elevate", "--out", str(tmp_path / "out")),
                             say=lines.append) == cli.EXIT_BUSY
    finally:
        other.release()
    assert capsys.readouterr().out == "" and any("already running" in line for line in lines), lines


def test_an_external_stop_ends_the_recording_and_is_its_reason(fake_world, tmp_path):
    stop, events = threading.Event(), []
    observers = sess.Observers(on_event=lambda kind, data: events.append((kind, data)))
    timer = threading.Timer(0.5, sess.request_stop, (stop, "stopped from the window"))
    started = time.monotonic()
    timer.start()
    try:                                   # no --duration: only the stop, or the fake game's own end, ends it
        assert cli.cmd_watch(_watch_args("--no-elevate", "--out", str(tmp_path / "out")), stop=stop,
                             say=lambda line: None, observers=observers) == 0
    finally:
        timer.cancel()
    assert time.monotonic() - started < FAKE_GAME_S / 2
    assert [kind for kind, _ in events] == ["target", "recording", "stopping", "saving", "saved"], events
    told = dict(events)
    assert told["target"] == {"name": "Game.exe", "pid": 4242, "how": "pid"}
    assert told["stopping"] == {"reason": "stopped from the window"}
    assert (tmp_path / "out" / told["saved"]["session"] / cli.CAPTURE_NAME).is_file()


class _Terminal:
    def isatty(self):
        return True


def test_a_watch_nobody_attends_never_asks_and_draws_no_status_line(fake_relay, fake_world, monkeypatch, tmp_path):
    asked, consoles = [], []
    monkeypatch.setattr(cli, "_ask", lambda prompt, default=False: asked.append(prompt) or True)
    monkeypatch.setattr(sys, "stdin", _Terminal())          # a person would be asked here
    real = cli._ConsoleControl
    monkeypatch.setattr(cli, "_ConsoleControl", lambda *a, **kw: consoles.append(1) or real(*a, **kw))
    out = str(tmp_path / "out")
    assert cli.cmd_watch(_watch_args("--duration", "0.1", "--out", out), interactive=False,
                         say=lambda line: None) == 0
    assert asked == [] and consoles == [] and fake_relay["launches"] == []
    assert fake_world["sessions"][-1]["printer"] is cli._silent
    assert fake_world["sessions"][-1]["opts"].frames is False   # not asked, so recorded without frame timing
    # The same watch with someone at this terminal: asked, and the console's own handling and status line.
    assert cli.cmd_watch(_watch_args("--duration", "0.1", "--out", out), say=lambda line: None) == 0
    assert len(asked) == 1 and consoles == [1] and len(fake_relay["launches"]) == 1
    assert fake_world["sessions"][-1]["printer"] is None


def test_without_any_stdin_nobody_is_asked(monkeypatch):
    monkeypatch.setattr(sys, "stdin", None)               # pythonw: no console, no stdin at all
    assert cli._interactive(None) is False
    monkeypatch.setattr(sys, "stdin", _Terminal())
    assert cli._interactive(None) is True and cli._interactive(False) is False and cli._interactive(True) is True


def test_a_console_handler_that_cannot_be_set_is_said_through_the_given_say(monkeypatch, capsys):
    monkeypatch.setattr(cli._kernel32, "SetConsoleCtrlHandler", lambda routine, add: False)
    lines = []
    with cli._ConsoleControl(threading.Event(), threading.Event(), network=False, say=lines.append) as control:
        assert not control.registered
    assert capsys.readouterr().out == "" and len(lines) == 1 and "could not be set" in lines[0], lines


def test_the_launcher_given_to_watch_is_the_one_that_starts_the_relay(fake_relay, fake_world, tmp_path):
    def launcher(argv, cwd):
        raise AssertionError("the faked relay/client.launch never calls it")
    args = _watch_args("--elevate", "--duration", "0.1", "--out", str(tmp_path / "out"))
    assert cli.cmd_watch(args, launcher=launcher, say=lambda line: None) == 0
    assert cli.cmd_watch(args, say=lambda line: None) == 0
    assert [launch["launcher"] for launch in fake_relay["launches"]] == [launcher, None]


def test_a_change_in_place_but_not_confirmed_is_not_called_unchanged(fake_fix, monkeypatch, capsys):
    from lagdoctor.fixes.base import FixResult

    def stored_for_restart(self, plan, journal):
        return FixResult(False, "Windows stored 144 Hz for after a restart (lagdoctor undo e1)", "e1", "applied")
    monkeypatch.setattr(FakeFix, "apply", stored_for_restart)
    write_last_diagnosis([{"title": "Turn it up", "fix_id": "test_cli_fake", "params": {"name": "a"}}])
    assert cli.main(["fix", "test_cli_fake", "--yes"]) == 1
    out = capsys.readouterr().out
    assert "Applied, not yet confirmed: Windows stored 144 Hz" in out and "Not changed" not in out
    assert "Undo it any time with: lagdoctor undo e1" in out


# ------------------------------------------------------------------------- analyze ---

def small_capture(name="Friend.exe") -> Capture:
    return Capture(started_at="2026-11-04T21:00:00+01:00", clock_origin=1.0, duration_s=1.0,
                   target={"pid": 20000, "create_time": 5, "name": name}, sources={},
                   inventory={"adapters": []}, system={"t": [0.0], "cpu_total_pct": [5.0]})


def test_analyze_never_writes_beside_the_capture_or_changes_what_fix_uses(tmp_path, capsys):
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / cli.REPORT_NAME).write_text("my own page", encoding="utf-8")
    (downloads / cli.DIAGNOSIS_NAME).write_text('{"mine": 1}', encoding="utf-8")
    small_capture().save(downloads / "friend.ldcap.json.gz")
    before = {p.name: p.read_bytes() for p in downloads.iterdir()}
    last = write_last_diagnosis([])
    marker = (paths.home() / cli.LAST_SESSION).read_text(encoding="utf-8")
    assert cli.main(["analyze", str(downloads / "friend.ldcap.json.gz")]) == 0
    assert {p.name: p.read_bytes() for p in downloads.iterdir()} == before
    made = [d for d in paths.sessions_dir().iterdir() if d != last]
    assert len(made) == 1 and made[0].name.endswith("-Friend") and (made[0] / cli.REPORT_NAME).is_file()
    assert (made[0] / cli.ANALYZED_FROM).read_text(encoding="utf-8").strip() == str(downloads / "friend.ldcap.json.gz")
    assert (paths.home() / cli.LAST_SESSION).read_text(encoding="utf-8") == marker
    assert "not used by `lagdoctor fix`" in capsys.readouterr().out


def _gz(path: Path, payload) -> None:
    with gzip.open(path, "wb") as f:
        f.write(payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8"))


@pytest.mark.parametrize("make,said", [
    (lambda p: p.write_text('{"findings": []}', encoding="utf-8"), "not a lag-doctor capture"),   # diagnosis.json
    (lambda p: _gz(p, {"schema": "something/else"}), "not a lag-doctor capture"),
    (lambda p: _gz(p, dict(small_capture().to_json(), schema="lagdoctor.capture/2")), "newer version"),
    (lambda p: _gz(p, b"\x00 not json"), "damaged"),
    (lambda p: p.write_bytes(gzip.compress(json.dumps(small_capture().to_json()).encode())[:40]), "damaged"),
    (lambda p: _gz(p, dict(small_capture().to_json(), target=[1, 2])), "damaged"),
])
def test_analyze_says_in_one_line_why_a_file_cannot_be_read(tmp_path, capsys, make, said):
    p = tmp_path / "x.ldcap.json.gz"
    make(p)
    assert cli.main(["analyze", str(p)]) == 2
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1 and said in lines[0] and str(p) in lines[0]
    assert [d for d in paths.sessions_dir().iterdir()] == []


def test_a_capture_that_breaks_the_analysis_is_called_damaged_and_kept(tmp_path, capsys, monkeypatch):
    from lagdoctor.analysis import diagnose as diagnose_mod
    monkeypatch.setattr(diagnose_mod, "diagnose", lambda cap: math.fsum([1e308, 1e308]))   # OverflowError
    p = small_capture().save(tmp_path / "c.ldcap.json.gz")
    before = p.read_bytes()
    assert cli.main(["analyze", str(p)]) == 1
    out = capsys.readouterr().out
    assert "looks damaged" in out and "OverflowError" in out and "Traceback" not in out
    assert p.read_bytes() == before and [d for d in paths.sessions_dir().iterdir()] == []


# ---------------------------------------------------------------------- fix, say ---

def test_an_unreadable_journal_pauses_fixes_with_a_plain_message(fake_fix, capsys):
    write_last_diagnosis([{"title": "Turn it up", "fix_id": "test_cli_fake", "params": {"name": "a"}}])
    paths.journal_path().write_text("{ this is not json", encoding="utf-8")
    assert cli.main(["fix", "test_cli_fake", "--yes"]) == 1
    out = capsys.readouterr().out
    assert "Fixes are paused" in out and "could not be read" in out and STORE == {}


def test_say_shows_control_characters_as_question_marks(capsys):
    cli.say("Game\x1b]52;c;ZXZpbA==\x07.exe\tx\r\nnext\x7f\x9b2J")
    assert capsys.readouterr().out == "Game?]52;c;ZXZpbA==?.exe?x?\nnext??2J\n"


# ------------------------------------------------- real console events, in a console of their own ---

CONSOLE_CHILD = r'''
import ctypes, os, sys, threading, time
root, out, mode, lock_name = sys.argv[1:5]
sys.path.insert(0, root)
os.environ["LAGDOCTOR_HOME"] = os.path.join(out, "home")
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.GetConsoleWindow.restype = ctypes.c_void_p
k32.SetConsoleCtrlHandler.argtypes = [ctypes.c_void_p, ctypes.c_int]
k32.GenerateConsoleCtrlEvent.argtypes = [ctypes.c_uint, ctypes.c_uint]
k32.SetConsoleCtrlHandler(None, 0)          # handle Ctrl+C as a user's own terminal does (not inherited "ignore")


def mark(name, text):
    with open(os.path.join(out, name), "w", encoding="utf-8") as f:
        f.write(text)


def press_soon(event, delay):
    def go():
        time.sleep(delay)
        k32.GenerateConsoleCtrlEvent(event, 0)   # 0 = every process on THIS console, which only this test made
    threading.Thread(target=go, daemon=True).start()


from lagdoctor import cli
from lagdoctor.capture import session as sess
from lagdoctor.capture import target as tgt
from lagdoctor.capture.base import Collector

sess.CAPTURE_LOCK_NAME = lock_name


class Slow(Collector):
    name = "system"

    def run(self):
        self.set_status("ok", tool="fake")
        for _ in self.ticks(0.05):
            pass

    def on_stop(self):
        if mode == "ctrl_c_twice":
            press_soon(0, 0.0)                   # the user presses again while it stops
        time.sleep(1.0)

    def result(self):
        return {"columns": {"t": [0.0, 0.25], "cpu_total_pct": [10.0, 11.0]}}


class Fake(sess.Session):
    def __init__(self, target, opts, **kw):
        super().__init__(target, opts, collectors=[Slow()], inventory={"adapters": []}, alive=lambda: True,
                         printer=lambda line: None)

    def run(self, stop=None, ctrl_c=None):
        mark("recording", str(k32.GetConsoleWindow() or 0))
        if mode == "ctrl_break":
            press_soon(1, 1.0)
        elif mode == "ctrl_c_twice":
            press_soon(0, 1.0)
        return super().run(stop) if ctrl_c is None else super().run(stop, ctrl_c)


sess.Session = Fake
tgt.find_target = lambda **kw: {"pid": 4242, "create_time": 1, "name": "Game.exe", "how": "pid"}
cli._presentmon_exe = lambda: (None, "frame timing was skipped: test")
code = cli.main(["watch", "--pid", "4242", "--no-elevate", "--no-network", "--hotkey", "none",
                 "--out", os.path.join(out, "sessions")])
mark("main_returned", str(code))
raise SystemExit(code)
'''
STOP_REASON = {"close": "window closed", "ctrl_break": "Ctrl+Break", "ctrl_c_twice": "Ctrl+C"}


@pytest.mark.parametrize("mode", ["close", "ctrl_break", "ctrl_c_twice"])
def test_a_real_console_event_while_recording_still_saves_the_capture(tmp_path, mode):
    """The real cli.main in a hidden console of its own: closing that window, Ctrl+Break in it, and Ctrl+C
    pressed twice (once more while it stops) must each still leave the capture on disk."""
    script, out = tmp_path / "child.py", tmp_path / "out"
    script.write_text(CONSOLE_CHILD, encoding="utf-8")
    out.mkdir()
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0                                                    # SW_HIDE
    site = [p for p in sys.path if p.lower().endswith("site-packages")]
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT), *site]))
    base = getattr(sys, "_base_executable", sys.executable)              # no venv launcher in between
    with open(out / "stdout.txt", "wb") as log:
        p = subprocess.Popen([base, str(script), str(ROOT), str(out), mode, f"Local\\lagdoctor-test-{uuid.uuid4().hex}"],
                             creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=si, env=env,
                             stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        try:
            marker = out / "recording"
            deadline = time.monotonic() + 60
            while not (marker.is_file() and marker.read_text(encoding="utf-8")) and time.monotonic() < deadline \
                    and p.poll() is None:
                time.sleep(0.05)
            assert marker.is_file(), f"the child never started recording: {(out / 'stdout.txt').read_bytes()[-800:]!r}"
            if mode == "close":
                hwnd = int(marker.read_text(encoding="utf-8"))
                k32 = ctypes.WinDLL("kernel32")
                k32.GetConsoleWindow.restype = ctypes.c_void_p
                if not hwnd:
                    pytest.skip("the hidden console has no window to close here")
                assert hwnd != (k32.GetConsoleWindow() or 0), "refusing to close this process's own console"
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
                user32.PostMessageW.restype = wintypes.BOOL
                time.sleep(0.5)
                assert user32.PostMessageW(hwnd, 0x0010, 0, 0)          # WM_CLOSE: the window's X button
            code = p.wait(60)
        finally:
            if p.poll() is None:
                p.kill()
                p.wait(10)
    caps = list((out / "sessions").glob(f"*/{cli.CAPTURE_NAME}"))
    assert len(caps) == 1, (f"no capture saved (exit {code & 0xFFFFFFFF:#x}): "
                            f"{(out / 'stdout.txt').read_bytes()[-800:]!r}")
    assert Capture.load(caps[0]).target["stop_reason"] == STOP_REASON[mode]
    if mode != "close":                     # after a close Windows ends the process once the save is done
        assert code == 0 and (out / "main_returned").read_text(encoding="utf-8") == "0"
