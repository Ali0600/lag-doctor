"""The command line: argument checks, the elevation choice, and fix / undo through the journal.

Watch tests run cmd_watch in this process with a fake target and fake collectors, and the elevated child is
faked too: nothing here raises a UAC prompt. The console-event tests run the real cli.main in a hidden console
of their own, so the events they send never reach this process's console.
"""
from __future__ import annotations

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
STORE: dict[str, object] = {}
GAME = {"pid": 4242, "create_time": 133000000000000001, "name": "Game.exe", "exe": r"C:\Games\Game.exe",
        "window_title": "Game", "monitor_device": None, "adapter_luid": None, "how": "pid", "candidates": []}
WATCH = ["watch", "--pid", "4242", "--duration", "0.2", "--no-network", "--hotkey", "none"]


@pytest.fixture(autouse=True)
def _private_capture_lock(monkeypatch):
    """Each test gets its own lock name, so it never meets a real recording (or another test run)."""
    monkeypatch.setattr(sess, "CAPTURE_LOCK_NAME", f"Local\\lagdoctor-test-{uuid.uuid4().hex}")


@pytest.fixture(autouse=True)
def _no_real_uac(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("a test reached the real elevated launch")
    monkeypatch.setattr(elevation, "_ShellExecuteExW", refuse)


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


def test_the_elevated_child_records_only_this_game_into_this_file(tmp_path):
    # Changed on purpose (LC-3, SEC-7, SEC-9): the argv now starts the interpreter with -E -s and carries the
    # create time, the verified PresentMon, the user's inventory and SID.
    args = cli.build_parser().parse_args(["watch", "--game", "x.exe", "--duration", "20", "--no-network",
                                          "--hotkey", "none", "--elevate"])
    exe, inv, cap = tmp_path / "PresentMon-2.6.0-x64.exe", tmp_path / "inv.json", tmp_path / "c.ldcap.json.gz"
    argv = cli.elevated_argv(args, {"pid": 4242, "create_time": 133000000000000001, "name": "x.exe"}, cap,
                             presentmon_exe=exe, inventory_file=inv, parent_sid="S-1-5-21-11-22-33-1001")
    assert argv[1:6] == ["-E", "-s", "-m", "lagdoctor", "watch"]
    assert ["--pid", "4242"] == argv[6:8] and "--capture-only" in argv
    assert argv[argv.index("--create-time") + 1] == "133000000000000001"
    assert argv[argv.index("--out-file") + 1] == str(cap)
    assert argv[argv.index("--presentmon-exe") + 1] == str(exe) and argv[argv.index("--inventory-file") + 1] == str(inv)
    assert argv[argv.index("--parent-sid") + 1] == "S-1-5-21-11-22-33-1001"
    assert "--elevate" not in argv and "--game" not in argv and "--no-network" in argv
    assert argv[argv.index("--duration") + 1] == "20.0" and argv[argv.index("--hotkey") + 1] == "none"
    assert all(isinstance(a, str) for a in argv)
    child = cli.build_parser().parse_args(argv[5:])                # and the child's own parser reads it back
    assert (child.capture_only, child.pid, child.create_time, child.out_file, child.presentmon_exe,
            child.inventory_file, child.parent_sid) == (True, 4242, 133000000000000001, cap, exe, inv,
                                                        "S-1-5-21-11-22-33-1001")


@pytest.mark.parametrize("which", ["cap_path", "presentmon_exe", "inventory_file"])
def test_the_elevated_child_is_never_handed_a_relative_path(tmp_path, which):
    args = cli.build_parser().parse_args(["watch", "--pid", "5"])
    paths_in = {"cap_path": tmp_path / "c.gz", "presentmon_exe": tmp_path / "pm.exe", "inventory_file": None}
    paths_in[which] = Path("relative") / "thing"
    cap_path = paths_in.pop("cap_path")
    with pytest.raises(ValueError, match="absolute"):
        cli.elevated_argv(args, {"pid": 5, "create_time": 1}, cap_path, **paths_in)


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
    monkeypatch.setattr(cli, "_presentmon_exe", lambda: (None, "frame timing was skipped: not installed (test)"))
    monkeypatch.setattr(sess, "_collect_inventory", lambda target: {"adapters": [], "who": "the user"})

    class QuickSession(sess.Session):
        def __init__(self, target, opts, inventory=None, **kw):
            seen["sessions"].append({"target": target, "opts": opts, "inventory": inventory})
            cols = [Quick("system", {"columns": {"t": [0.0, 0.25], "cpu_total_pct": [10.0, 11.0]}}),
                    Quick("frames", {"columns": {"t": [0.1, 0.12, 0.14], "frame_ms": [16.7, 16.6, 16.8]}})]
            super().__init__(target, opts, collectors=cols, inventory=inventory or {"adapters": []},
                             alive=lambda: True, printer=lambda line: None)
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
        as_child = cli.main([*WATCH, "--capture-only", "--create-time", str(GAME["create_time"]),
                             "--out-file", str(tmp_path / "s" / cli.CAPTURE_NAME)])
    finally:
        other.release()
    assert plain == as_child == cli.EXIT_BUSY
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


# ------------------------------------------------------------- the elevated child, faked ---

class FakeChild:
    """What launch_elevated returns. Its 'recording' already ran: `effect(argv, cwd)` wrote what it wrote."""

    def __init__(self, code=0, interrupts=0):
        self.code, self.interrupts, self.closed = code, interrupts, False

    def wait(self, timeout_s=None):
        if self.interrupts:
            self.interrupts -= 1
            raise KeyboardInterrupt                     # Ctrl+C in this (the first) window while it waits
        return self.code

    def close(self):
        self.closed = True


def child_capture(argv, cwd, target=None):
    """Write the capture the way the child would: --out-file resolved against the child's own folder."""
    out_file = Path(os.path.join(cwd, argv[argv.index("--out-file") + 1]))
    out_file.parent.mkdir(parents=True, exist_ok=True)
    Capture(started_at="2026-11-04T21:00:00+01:00", clock_origin=1.0, duration_s=1.0,
            target=dict(target or GAME, stop_reason="Ctrl+C"), sources={}, inventory={"adapters": []},
            system={"t": [0.0], "cpu_total_pct": [5.0]}).save(out_file)
    return out_file


@pytest.fixture
def fake_elevation(monkeypatch, fake_world, tmp_path):
    """The parent may trace only by elevating; launching runs `state['effect']` and returns `state['child']`."""
    state = {"launches": [], "child": FakeChild(), "effect": child_capture}
    monkeypatch.setattr(cli, "_presentmon_exe", lambda: (tmp_path / "PresentMon-2.6.0-x64.exe", ""))
    monkeypatch.setattr(elevation, "can_trace", lambda: False)

    def launch(argv, cwd):
        state["launches"].append((list(argv), cwd))
        state["effect"](argv, cwd)
        return state["child"]

    def relaunch(argv, cwd):                     # the same child, for code that waits in one call
        return launch(argv, cwd).wait()
    monkeypatch.setattr(elevation, "launch_elevated", launch, raising=False)
    monkeypatch.setattr(elevation, "relaunch_elevated", relaunch)
    return state


def test_a_relative_out_folder_reaches_the_elevated_child_as_an_absolute_path(fake_elevation, monkeypatch,
                                                                             tmp_path):
    here, project = tmp_path / "here", tmp_path / "project"
    here.mkdir()
    project.mkdir()
    monkeypatch.chdir(here)
    monkeypatch.setattr(cli, "PROJECT_ROOT", project)              # where the child runs
    assert cli.main(["watch", "--pid", "4242", "--out", "rel", "--elevate", "--no-network", "--hotkey", "none"]) == 0
    argv, cwd = fake_elevation["launches"][0]
    out_file = Path(argv[argv.index("--out-file") + 1])
    assert out_file.is_absolute() and out_file.parent.parent == (here / "rel").resolve()
    assert (out_file.parent / cli.DIAGNOSIS_NAME).is_file()        # found and analysed here
    assert not (out_file.parent / cli.INVENTORY_HANDOVER).exists()  # the handover is tidied once it ended
    assert not (project / "rel").exists()


def test_the_parent_hands_the_child_its_presentmon_inventory_and_sid(fake_elevation, monkeypatch, tmp_path):
    from lagdoctor.capture import netmon
    users_log = tmp_path / "Steam" / "logs" / "content_log.txt"
    monkeypatch.setattr(netmon, "find_steam_log", lambda: users_log)      # read in the user's own HKCU
    seen = {}

    def effect(argv, cwd):
        seen["inventory"] = json.loads(Path(argv[argv.index("--inventory-file") + 1]).read_text(encoding="utf-8"))
        probe = sess.CaptureLock()                     # the child takes the lock for its recording
        seen["lock_free"] = probe.acquire()
        probe.release()
        child_capture(argv, cwd)
    fake_elevation["effect"] = effect
    assert cli.main(["watch", "--pid", "4242", "--elevate", "--no-network", "--hotkey", "none",
                     "--out", str(tmp_path / "out")]) == 0
    argv, _ = fake_elevation["launches"][0]
    assert argv[argv.index("--presentmon-exe") + 1] == str(tmp_path / "PresentMon-2.6.0-x64.exe")
    assert seen["inventory"] == {"adapters": [], "who": "the user"}         # read here, in the user's account
    assert argv[argv.index("--parent-sid") + 1] == elevation.token_user_sid()
    assert argv[argv.index("--create-time") + 1] == str(GAME["create_time"])
    assert argv[argv.index("--steam-log") + 1] == str(users_log.resolve())
    assert seen["lock_free"] is True                       # the parent let go of it before the prompt


def test_a_declined_prompt_records_here_still_holding_the_lock(fake_elevation, monkeypatch, tmp_path, capsys):
    def declined(argv, cwd):
        raise PermissionError(13, "the administrator prompt was declined", None, 1223)
    monkeypatch.setattr(elevation, "launch_elevated", declined, raising=False)
    monkeypatch.setattr(elevation, "relaunch_elevated", declined)
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
    assert "declined" in capsys.readouterr().out and held_while_recording == [True]
    assert len(list((tmp_path / "out").glob(f"*/{cli.CAPTURE_NAME}"))) == 1


def test_a_relative_lagdoctor_home_still_hands_the_child_absolute_paths(fake_elevation, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LAGDOCTOR_HOME", "relative-home")
    monkeypatch.setattr(cli, "_presentmon_exe", lambda: (Path("relative-home") / "PresentMon-2.6.0-x64.exe", ""))
    assert cli.main(["watch", "--pid", "4242", "--elevate", "--no-network", "--hotkey", "none"]) == 0
    argv, _ = fake_elevation["launches"][0]
    for flag in ("--out-file", "--presentmon-exe", "--inventory-file"):
        assert Path(argv[argv.index(flag) + 1]).is_absolute(), flag


def test_the_parent_refuses_a_capture_of_another_process(fake_elevation, tmp_path, capsys):
    fake_elevation["effect"] = lambda argv, cwd: child_capture(argv, cwd, dict(GAME, create_time=7, name="Other.exe"))
    assert cli.main(["watch", "--pid", "4242", "--elevate", "--no-network", "--hotkey", "none",
                     "--out", str(tmp_path / "out")]) == 1
    (folder,) = (tmp_path / "out").iterdir()
    assert (folder / cli.CAPTURE_NAME).is_file() and not (folder / cli.DIAGNOSIS_NAME).exists()
    assert "different process" in capsys.readouterr().out
    assert not (paths.home() / cli.LAST_SESSION).exists()


@pytest.mark.parametrize("code,said", [(cli.EXIT_BUSY, "already running"),
                                       (cli.EXIT_TARGET_CHANGED, "exited while the UAC prompt was up"),
                                       (9, "exit code 9")])
def test_a_child_that_saved_nothing_is_explained(fake_elevation, tmp_path, capsys, code, said):
    fake_elevation["effect"] = lambda argv, cwd: None
    fake_elevation["child"] = FakeChild(code=code)
    assert cli.main(["watch", "--pid", "4242", "--elevate", "--no-network", "--hotkey", "none",
                     "--out", str(tmp_path / "out")]) == 1
    assert said in capsys.readouterr().out
    assert list((tmp_path / "out").iterdir()) == []            # its empty folder is not left behind


@pytest.mark.parametrize("presses", [1, 2])
def test_ctrl_c_in_the_first_window_says_where_the_recording_goes(fake_elevation, tmp_path, capsys, presses):
    fake_elevation["child"] = FakeChild(interrupts=presses)
    if presses == 2:
        fake_elevation["effect"] = lambda argv, cwd: None     # still recording: nothing saved yet
    code = cli.main(["watch", "--pid", "4242", "--elevate", "--no-network", "--hotkey", "none",
                     "--out", str(tmp_path / "out")])
    out = capsys.readouterr().out
    argv, _ = fake_elevation["launches"][0]
    cap_path = argv[argv.index("--out-file") + 1]
    assert "keeps running in the other" in out and cap_path in out
    if presses == 1:                                         # it kept waiting, then analysed as usual
        assert code == 0 and (Path(cap_path).parent / cli.DIAGNOSIS_NAME).is_file()
    else:
        assert code == 130 and f'lagdoctor analyze "{cap_path}"' in out
        # Never tidied away under a child that may still be recording into it.
        assert (Path(cap_path).parent / cli.INVENTORY_HANDOVER).is_file()
    assert fake_elevation["child"].closed


# --------------------------------------------------------------- the elevated child itself ---

def child_argv(tmp_path, *extra):
    return [*WATCH, "--capture-only", "--create-time", str(GAME["create_time"]),
            "--out-file", str(tmp_path / "s" / cli.CAPTURE_NAME), *extra]


def test_the_elevated_child_refuses_to_record_another_process(fake_world, tmp_path, capsys):
    argv = child_argv(tmp_path)
    argv[argv.index("--create-time") + 1] = "999"            # the game exited; its PID was reused
    assert cli.main(argv) == cli.EXIT_TARGET_CHANGED
    assert fake_world["sessions"] == [] and not (tmp_path / "s" / cli.CAPTURE_NAME).exists()
    assert "exited while the UAC prompt was up" in capsys.readouterr().out


def test_the_parent_hands_the_child_its_own_steam_log(tmp_path):
    args = cli.build_parser().parse_args(["watch", "--pid", "4242", "--hotkey", "none"])
    game = {"pid": 4242, "create_time": 1, "name": "x.exe"}
    log = tmp_path / "Steam" / "logs" / "content_log.txt"
    argv = cli.elevated_argv(args, game, tmp_path / "c.gz", steam_log=log)
    assert argv[argv.index("--steam-log") + 1] == str(log)
    # "" = this account has none: the child must not go looking in its own (perhaps another account's) HKCU.
    argv = cli.elevated_argv(args, game, tmp_path / "c.gz", steam_log="")
    assert argv[argv.index("--steam-log") + 1] == ""
    assert cli.build_parser().parse_args(argv[5:]).steam_log == ""
    with pytest.raises(ValueError):
        cli.elevated_argv(args, game, tmp_path / "c.gz", steam_log=Path("rel") / "content_log.txt")


def test_the_elevated_child_uses_the_steam_log_it_was_handed(fake_world, tmp_path):
    log = tmp_path / "Steam" / "logs" / "content_log.txt"
    assert cli.main(child_argv(tmp_path, "--steam-log", str(log))) == 0
    assert fake_world["sessions"][0]["opts"].steam_log == str(log)
    fake_world["sessions"].clear()
    assert cli.main(child_argv(tmp_path, "--steam-log", "")) == 0
    assert fake_world["sessions"][0]["opts"].steam_log == ""


def test_the_elevated_child_refuses_a_steam_log_that_is_not_a_local_file(fake_world, tmp_path):
    for bad in ("rel\\content_log.txt", r"\\server\share\content_log.txt"):
        assert cli.main(child_argv(tmp_path, "--steam-log", bad)) == cli.EXIT_BAD_ARGS
    assert fake_world["sessions"] == []


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


def test_the_elevated_child_needs_its_identity_and_absolute_paths(fake_world, tmp_path):
    for argv in ([*WATCH, "--capture-only", "--out-file", str(tmp_path / "c.gz")],                  # no create time
                 [*WATCH, "--capture-only", "--create-time", "1", "--out-file", "rel/c.gz"],
                 child_argv(tmp_path, "--inventory-file", "inv.json")):
        assert cli.main(argv) == cli.EXIT_BAD_ARGS
    assert fake_world["find"] == [] and fake_world["sessions"] == []


def test_the_elevated_child_uses_the_presentmon_the_parent_verified(fake_world, monkeypatch, tmp_path):
    from lagdoctor import setup as pm_setup
    exe = tmp_path / "users-profile" / "PresentMon-2.6.0-x64.exe"
    checked = []
    monkeypatch.setattr(pm_setup, "verify", lambda p, **kw: checked.append(Path(p)) or {"signer": "Intel Corporation"})
    # This (administrator) account has no PresentMon of its own.
    monkeypatch.setattr(cli, "_presentmon_exe", lambda: (None, "frame timing was skipped: PresentMon 2.6.0 is not "
                                                               "installed (C:/Users/Admin/...)"))
    assert cli.main(child_argv(tmp_path, "--presentmon-exe", str(exe))) == 0
    opts = fake_world["sessions"][0]["opts"]
    assert opts.frames is True and opts.presentmon_exe == exe and checked == [exe]


def test_a_handed_over_presentmon_that_fails_its_check_is_not_run(fake_world, monkeypatch, tmp_path):
    from lagdoctor import setup as pm_setup

    def bad(p, **kw):
        raise pm_setup.VerifyError("its SHA-256 does not match")
    monkeypatch.setattr(pm_setup, "verify", bad)
    assert cli.main(child_argv(tmp_path, "--presentmon-exe", str(tmp_path / "pm.exe"))) == 0
    assert fake_world["sessions"][0]["opts"].frames is False
    cap = Capture.load(tmp_path / "s" / cli.CAPTURE_NAME)
    assert cap.sources["frames"].state == "off" and "failed its check" in cap.sources["frames"].detail


def test_the_elevated_child_uses_the_inventory_read_in_the_users_account(fake_world, tmp_path):
    inv = {"adapters": [], "settings": {"game_mode": True}, "who": "the user"}
    f = tmp_path / "inv.json"
    f.write_text(json.dumps(inv), encoding="utf-8")
    assert cli.main(child_argv(tmp_path, "--inventory-file", str(f))) == 0
    assert fake_world["sessions"][0]["inventory"] == inv
    assert Capture.load(tmp_path / "s" / cli.CAPTURE_NAME).inventory["who"] == "the user"


@pytest.mark.parametrize("text,limit", [("[1, 2", None), ("[1, 2]", None),
                                        # valid JSON in the bytes read, more after: only the size check sees it
                                        ('{"adapters": []}' + " " * 200 + "junk", 100)])
def test_an_inventory_that_cannot_be_read_is_noted_not_trusted(fake_world, monkeypatch, tmp_path, text, limit):
    if limit:
        monkeypatch.setattr(cli, "MAX_INVENTORY_BYTES", limit)
    f = tmp_path / "inv.json"
    f.write_text(text, encoding="utf-8")
    assert cli.main(child_argv(tmp_path, "--inventory-file", str(f))) == 0
    assert fake_world["sessions"][0]["inventory"] is None                  # the session reads its own
    notes = Capture.load(tmp_path / "s" / cli.CAPTURE_NAME).notes
    assert any("could not be used" in n and "administrator window" in n for n in notes)


@pytest.mark.parametrize("parent,noted", [("S-1-5-21-1-2-3-1001", True), (None, False)])
def test_an_elevation_by_another_account_is_noted(fake_world, monkeypatch, tmp_path, parent, noted):
    mine = "S-1-5-21-9-9-9-500"
    monkeypatch.setattr(elevation, "token_user_sid", lambda: mine)
    assert cli.main(child_argv(tmp_path, "--parent-sid", parent or mine)) == 0
    notes = Capture.load(tmp_path / "s" / cli.CAPTURE_NAME).notes
    assert any("another Windows account" in n for n in notes) is noted


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
