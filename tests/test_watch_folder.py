"""watch --wait-folder: Play's folder wait from the console (docs/LAUNCHER-PLAN.md, M6e).

The order is Play's: the folder's forms and what already runs from it, then the UAC prompt, then the wait. These tests
pin what the wait is given with the relay and without it, and how each way of ending is said.

The relay's launch, the wait and the session are stand-ins here (test_cli's fake_world, a PlayLink, a stand-in
target.wait_for_game). The exception is the last test: it runs the real wait on test_folder_wait's made-up machine,
with a link whose aim asks the relay's own check (target.check_proposed). Nothing raises a UAC prompt, starts a game
or opens a store link. Games and folders are made up.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import uuid

import pytest

from lagdoctor import cli, control
from lagdoctor.capture import session as sess
from lagdoctor.capture import target as tgt
from lagdoctor.relay import client, protocol
from lagdoctor.winapi import elevation, windows
from tests.test_cli import FakeLink, P, _session_now, fake_world  # noqa: F401 - fake_world is a fixture
from tests import test_control as tc
from tests import test_folder_wait as fw

GAME_DIR = "C:\\Games\\Lantern Drift"
FORMS = (GAME_DIR,)
SECOND = 10_000_000
NOW = 133_000_000_000_000_000                   # the pre-flight's moment, as a create time
RELAY_CT = NOW + 40 * SECOND                    # the relay started after it, once the prompt was approved
FOUND = {"pid": 4242, "create_time": NOW + 90 * SECOND, "name": "lantern_.exe", "exe": GAME_DIR + "\\lantern_.exe",
         "window_title": "Lantern Drift", "monitor_device": None, "adapter_luid": None, "how": "folder",
         "candidates": [], "folder": GAME_DIR, "folders": FORMS, "judged_by": "3d"}
REAL_WAIT_FOR_GAME = tgt.wait_for_game
REAL_RUNNING_FROM = tgt.running_from


@pytest.fixture(autouse=True)
def _private_capture_lock(monkeypatch):
    monkeypatch.setattr(sess, "CAPTURE_LOCK_NAME", f"Local\\lagdoctor-test-{uuid.uuid4().hex}")


@pytest.fixture(autouse=True)
def _no_real_uac(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("a test reached the real elevated launch")
    monkeypatch.setattr(elevation, "_ShellExecuteExW", refuse)
    monkeypatch.setattr(elevation, "is_elevated", lambda: False)


class PlayLink(FakeLink):
    """What relay/client.launch returns for a folder: a relay approved for its forms that takes each game aim names,
    unless the test lined up what aim raises first (`fails`)."""

    def __init__(self, folders, events, fails, wait_s):
        super().__init__(True, events)
        self.create_time, self.folders, self.recorder = RELAY_CT, folders, (os.getpid(), 555)
        self.since = RELAY_CT                   # a relay's games start after its own start
        self.aim_by = time.monotonic() + wait_s + client._game_slack() - client.AIM_S      # as launch works it out
        self.ended = threading.Event()
        self.fails, self.aimed = list(fails), []

    def own(self):
        return (self.recorder, (self.pid, self.create_time))

    def failure(self):
        return client.RelayFailed("the relay ended: it stopped on an error it did not expect", protocol.EXIT_CRASH)

    def aim(self, game):
        self.aimed.append(game)
        self.events.append("aim")
        if self.fails:
            raise self.fails.pop(0)


@pytest.fixture
def play(monkeypatch, fake_world, tmp_path):
    """Frame timing needs the prompt here; the folder's forms are the folder; nothing runs from it; the launch, the
    wait and the pre-flight's readers are stand-ins that record what they were given. The stand-in wait names its
    game to the relay (`aim`) `state["aims"]` times, keeping each answer, then returns FOUND (or raises
    state["wait_raises"])."""
    state = {"events": [], "launches": [], "waits": [], "aims": 1, "answers": [], "link": None, "raise": None,
             "fails": [], "running": [], "windows": [], "wait_raises": None, "said": [], "resolves": [],
             "in_wait": None, "windows_seq": []}
    session = _session_now()
    monkeypatch.setattr(cli, "_presentmon_exe", lambda: (tmp_path / "PresentMon-2.6.0-x64.exe", ""))
    monkeypatch.setattr(elevation, "can_trace", lambda: False)
    monkeypatch.setattr(sess, "_snapshot", lambda threads_for=(): {os.getpid(): P(os.getpid(), 4, 555, session)})
    monkeypatch.setattr(tgt, "folder_forms", lambda folder: (tgt.check_folder(folder),))
    monkeypatch.setattr(tgt, "filetime_now", lambda: NOW)
    def running_from(forms, resolve=True):
        state["resolves"].append(resolve)
        return {"running": state["running"].pop(0) if state["running"] else [], "unreadable": []}
    monkeypatch.setattr(tgt, "running_from", running_from)
    monkeypatch.setattr(windows, "top_windows",
                        lambda: list(state["windows_seq"].pop(0) if state["windows_seq"] else state["windows"]))

    def launch(target, **kw):
        state["events"].append("prompt")
        state["launches"].append(dict(kw, target=target))
        if state.get("during_prompt") is not None:
            state["during_prompt"]()
        if state["raise"] is not None:
            raise state["raise"]
        state["link"] = PlayLink(kw["folders"], state["events"], state["fails"], kw["wait_s"])
        return state["link"]
    monkeypatch.setattr(client, "launch", launch)

    def wait_for_game(folder, **kw):
        state["events"].append("wait")
        state["waits"].append(dict(kw, folder=folder))
        if state["in_wait"] is not None:
            state["in_wait"]()
        if state["wait_raises"] is not None:
            raise state["wait_raises"]
        for _ in range(state["aims"] if kw.get("aim") else 0):
            state["answers"].append(kw["aim"](dict(FOUND)))
        return dict(FOUND)
    monkeypatch.setattr(tgt, "wait_for_game", wait_for_game)
    return state


def _args(tmp_path, *extra):
    return cli.build_parser().parse_args(["watch", "--wait-folder", GAME_DIR, "--duration", "0.2", "--no-network",
                                          "--hotkey", "none", "--start-key", "none", "--out", str(tmp_path / "out"),
                                          *extra])


def _watch(play, tmp_path, *extra, cancel=None):
    observers = sess.Observers(on_event=lambda kind, data: play["events"].append(kind))
    return cli.cmd_watch(_args(tmp_path, *extra), say=play["said"].append, observers=observers, cancel=cancel)


def _said(play) -> str:
    return "\n".join(play["said"])


PLAY_STEPS = ("prompt", "already_running", "launch", "waiting", "wait", "aim", "target", "recording")


def _steps(play) -> list[str]:
    """Play's order as it happened: the stand-ins' prompt, wait and aim among the events the observers were told."""
    return [e for e in play["events"] if e in PLAY_STEPS]


# ---------------------------------------------------------------- with the relay ---

def test_the_prompt_comes_first_then_the_wait_judges_as_the_relay_does_and_names_the_game_to_it(play, fake_world,
                                                                                                   tmp_path):
    assert _watch(play, tmp_path, "--elevate") == 0
    assert _steps(play) == ["prompt", "launch", "waiting", "wait", "aim", "target", "recording"]
    (launch,) = play["launches"]
    assert launch["target"] is None and launch["folders"] == FORMS and launch["wait_s"] == 300.0
    link = play["link"]
    (kw,) = play["waits"]
    assert kw["folder"] == GAME_DIR and kw["folders"] == FORMS
    assert kw["since"] == RELAY_CT and kw["own"] == link.own() and kw["resolve"] is False
    assert kw["expand_short"] is True and play["resolves"] == [True, True]     # the pre-flight and the second look
    assert not kw["cancel"].is_set()                                            # the relay's end, or a cancel
    assert 299.0 < kw["deadline_s"] <= 300.0 and kw["hold_s"] == tgt.REARM_HOLD_S
    assert link.aimed == [(FOUND["pid"], FOUND["create_time"])] and play["answers"] == [None]
    (session,) = fake_world["sessions"]
    assert session["target"]["pid"] == FOUND["pid"] and session["opts"].relay is link
    assert "Waiting up to 5 minutes for the game to start from C:\\Games\\Lantern Drift" in _said(play)
    assert "a store that first downloads an update can take longer: give it --wait-s" in _said(play)
    assert "Watching lantern_.exe (pid 4242, chosen by folder)." in play["said"]


def test_the_wait_ends_where_the_relays_own_does_less_its_slack(play, monkeypatch, tmp_path):
    real = PlayLink.__init__

    def shorter(self, *a):
        real(self, *a)
        self.aim_by = time.monotonic() + 100 + client._game_slack() - client.AIM_S     # 200 s went on something else
    monkeypatch.setattr(PlayLink, "__init__", shorter)
    assert _watch(play, tmp_path, "--elevate") == 0
    assert 99.0 < play["waits"][0]["deadline_s"] <= 100.0


def test_a_relay_whose_wait_is_over_before_this_one_begins_waits_for_nothing(play, monkeypatch, tmp_path):
    real = PlayLink.__init__

    def over(self, *a):
        real(self, *a)
        self.aim_by = time.monotonic() + client._game_slack() - client.AIM_S - 1
    monkeypatch.setattr(PlayLink, "__init__", over)
    assert _watch(play, tmp_path, "--elevate") == 1
    assert play["waits"] == [] and not any(line.startswith("Waiting up to") for line in play["said"])
    assert _steps(play) == ["prompt"]                   # no store link opened for a wait that cannot take the game
    assert play["said"][-1] == ("Not recorded: the relay's wait for the game was over before this one began. Run the "
                                "same `lagdoctor watch` again.")


def test_wait_s_is_both_how_long_the_relay_is_approved_for_and_how_long_watch_waits(play, tmp_path):
    assert _watch(play, tmp_path, "--elevate", "--wait-s", "1234.567") == 0
    assert play["launches"][0]["wait_s"] == 1234.567
    assert 1234.0 < play["waits"][0]["deadline_s"] <= 1234.567
    assert "Waiting up to 20 minutes 35 seconds for the game" in _said(play)


def test_wait_s_and_wait_folder_are_checked_before_anything_starts(play, tmp_path):
    p = cli.build_parser()
    assert p.parse_args(["watch", "--wait-folder", "c:/games/lantern drift"]).wait_folder == "C:\\games\\lantern drift"
    for bad in (["--wait-s", "0"], ["--wait-s", "3601"], ["--wait-s", "10.1234"], ["--wait-s", "nan"],
                ["--wait-s", "1e3"]):
        with pytest.raises(SystemExit):
            p.parse_args(["watch", "--wait-folder", GAME_DIR, *bad])
    for bad in (["--wait-folder", GAME_DIR, "--pid", "4"], ["--wait-folder", GAME_DIR, "--auto"],
                ["--wait-folder", "C:\\"], ["--wait-folder", "Games\\Lantern"]):
        with pytest.raises(SystemExit):
            p.parse_args(["watch", *bad])
    args = p.parse_args(["watch", "--pid", "4", "--wait-s", "30", "--hotkey", "none", "--start-key", "none"])
    assert cli.cmd_watch(args, say=play["said"].append) == cli.EXIT_BAD_ARGS
    assert play["said"] == ["--wait-s goes with --wait-folder: it is how long watch waits for the game to start from "
                            "its folder."]
    assert play["launches"] == [] and play["waits"] == []


def test_the_waits_numbers_are_the_folder_waits_own():
    assert (cli.WAIT_DEFAULT_S, cli.WAIT_MAX_S) == (tgt.WAIT_DEADLINE_S, tgt.MAX_WAIT_S)


def test_a_refused_game_re_arms_the_wait_and_never_reads_as_the_relay_failing(play, monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "_relay_failure", lambda *a, **k: pytest.fail("a refusal read as the relay failing"))
    play["fails"].append(client.GameRefused("it is not in the game's folder", retry=False, sent=True))
    play["fails"].append(client.GameRefused("it could not be checked", retry=True, sent=True))
    play["aims"] = 3
    assert _watch(play, tmp_path, "--elevate") == 0
    assert play["answers"] == [("it is not in the game's folder", False), ("it could not be checked", True), None]


def test_once_the_relay_takes_no_more_games_the_wait_ends_as_one_that_found_none(play, tmp_path):
    play["fails"].append(client.RelayFailed("the relay takes at most 8 games for one recording"))
    assert _watch(play, tmp_path, "--elevate") == cli.EXIT_BAD_ARGS
    assert "No game was found: the relay takes at most 8 games for one recording." in play["said"]
    assert play["link"].closed >= 1 and "recording" not in play["events"]


def test_a_link_that_ended_while_a_game_was_named_is_the_relay_failing_in_plays_words(play, monkeypatch, tmp_path):
    real = PlayLink.aim

    def ended(self, game):
        self.ended.set()
        return real(self, game)
    monkeypatch.setattr(PlayLink, "aim", ended)
    play["fails"].append(client.RelayFailed("the relay ended: its pipe broke", protocol.EXIT_TARGET))
    assert _watch(play, tmp_path, "--elevate") == 1
    text = _said(play)
    assert "this window was not running as it approved" in text and "while the UAC prompt was up" not in text
    assert play["link"].closed >= 1 and "recording" not in play["events"]


def test_a_relay_that_could_not_start_for_a_folder_is_said_in_plays_words(play, tmp_path):
    play["raise"] = client.RelayFailed("the relay ended before it answered (exit code 3)", protocol.EXIT_TARGET)
    assert _watch(play, tmp_path, "--elevate") == 1
    assert "this window was not running as it approved" in _said(play) and play["waits"] == []


def test_plays_relay_failure_never_says_the_game_exited_while_the_prompt_was_up():
    failed = client.RelayFailed("the relay ended", protocol.EXIT_TARGET)
    code, text = cli._relay_failure(failed, play=True)
    assert code == 1 and "while the UAC prompt was up" not in text and "Run the same `lagdoctor watch` again" in text
    code, text = cli._relay_failure(failed)
    assert code == cli.EXIT_TARGET_CHANGED and "exited while the UAC prompt was up" in text
    assert cli._relay_failure(client.RelayFailed("x", protocol.EXIT_NOT_READY), play=True)[0] == cli.EXIT_NOT_READY


def _early(pid, name):
    return {"pid": pid, "create_time": NOW + 10 * SECOND, "name": name, "exe": GAME_DIR + "\\" + name}


def _window_of(pid):
    return {"hwnd": pid, "pid": pid, "title": "Lantern Drift", "area": 2_073_600, "minimized": False,
            "monitor_device": None, "cls": "GameWindow"}


def test_a_game_that_started_with_a_window_while_the_prompt_was_up_is_said_at_once(play, tmp_path):
    play["running"] = [[], [_early(4299, "lantern.exe"), _early(4300, "lantern_.exe")]]   # after the pre-flight
    play["windows"] = [_window_of(4300)]
    assert _watch(play, tmp_path, "--elevate") == cli.EXIT_BAD_ARGS
    assert play["waits"] == [] and play["link"].closed >= 1
    assert _steps(play) == ["prompt", "already_running"]          # told, and no store link opened
    assert ("Not recorded: lantern_.exe started from the game's folder while the UAC prompt was up, and a recording "
            "started this way takes only a game that starts after it. Record it as it runs: lagdoctor watch --pid "
            "4300.") in play["said"]


def test_a_stub_that_started_without_a_window_while_the_prompt_was_up_is_said_and_the_wait_goes_on(play, tmp_path):
    play["running"] = [[], [_early(4299, "lantern.exe")]]          # its game may still start after the relay
    assert _watch(play, tmp_path, "--elevate") == 0 and len(play["waits"]) == 1
    assert _steps(play)[:3] == ["prompt", "launch", "waiting"]
    assert ("(lantern.exe, pid 4299, started from the game's folder while the UAC prompt was up, so this recording "
            "cannot take it. If it is the game itself, record it as it runs once it shows its window: lagdoctor "
            "watch --pid 4299.)") in play["said"]


def test_a_program_from_the_folder_newer_than_the_relay_is_left_to_the_wait(play, tmp_path):
    older = {"pid": 4300, "create_time": NOW - tgt.SINCE_MARGIN, "name": "helper.exe", "exe": GAME_DIR + "\\h.exe"}
    newer = {"pid": 4301, "create_time": RELAY_CT + 1, "name": "lantern.exe", "exe": GAME_DIR + "\\lantern.exe"}
    play["running"] = [[], [older, newer]]
    play["windows"] = [_window_of(4301)]          # it would end it, were it older than the relay
    assert _watch(play, tmp_path, "--elevate") == 0 and len(play["waits"]) == 1
    # A windowless program from before the pre-flight (a helper left running) is neither the game nor said.
    assert not [line for line in play["said"] if "while the UAC prompt was up" in line]
    assert _steps(play)[:3] == ["prompt", "launch", "waiting"]


@pytest.mark.parametrize("relayed", [True, False])
def test_a_game_from_before_the_pre_flight_that_has_a_window_now_is_already_running(play, monkeypatch, tmp_path,
                                                                                      relayed):
    # Started by hand, still loading at the pre-flight (no window yet), it showed its window while the prompt was up:
    # a store starts nothing for it, and the wait, which looks only at what starts after it, could never take it.
    if not relayed:
        monkeypatch.setattr(elevation, "can_trace", lambda: True)      # no prompt: the wait looks from the pre-flight
    loading = {"pid": 4300, "create_time": NOW - 60 * SECOND, "name": "lantern_.exe", "exe": FOUND["exe"]}
    play["running"] = [[dict(loading)], [dict(loading)]]
    play["windows_seq"] = [[]]                          # none at the pre-flight
    play["windows"] = [_window_of(4300)]                # and its window afterwards
    assert _watch(play, tmp_path, *(["--elevate"] if relayed else [])) == cli.EXIT_BAD_ARGS
    assert play["waits"] == [] and _steps(play) == (["prompt", "already_running"] if relayed else ["already_running"])
    assert ("Not recorded: lantern_.exe already runs from the game's folder with a window, and a store starts "
            "nothing for a game that is running. Record it as it runs: lagdoctor watch --pid 4300.") in play["said"]


# ---------------------------------------------------------------- as lag-doctor's window hears it ---

def _serve(play, window, tmp_path, *extra):
    """watch --control stdio --wait-folder, as the app runs it, over test_control's stand-in window."""
    code = control.serve(_args(tmp_path, "--control", "stdio", *extra), window.recorder_in, window.recorder_out)
    window.finish()
    assert window.errors == []
    return code


@pytest.fixture
def prompted(play, monkeypatch):
    """client.launch has the window raise the prompt first, as the real one does (Channel.relay_launcher), and the
    window answers it with a harmless process of the test's own in the relay's place; then the play fixture's link."""
    stand_in = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"])
    play_launch = client.launch

    def through_the_window(target, **kw):
        relay = kw["launcher"]([r"C:\Python313\pythonw.exe", "-B", "-m", "lagdoctor.relay"], cwd="C:\\")
        relay.close()
        return play_launch(target, **kw)
    monkeypatch.setattr(client, "launch", through_the_window)

    def answer(window, msg):
        if msg["t"] == "elevate":
            window.send("elevated", handle=tc._dup_here(stand_in._handle), pid=stand_in.pid)
    try:
        yield answer
    finally:
        stand_in.kill()
        stand_in.wait(10)


def test_a_relay_that_fails_for_plays_folder_reaches_the_window_as_the_relay_failing(play, prompted, tmp_path):
    # The relay is asked for before the game exists (no "target" yet): its failure is still the relay's, not a
    # failure lag-doctor cannot explain.
    play["raise"] = client.RelayFailed("the relay ended before it answered (exit code 3)", protocol.EXIT_TARGET)
    window = tc.Window(prompted)
    assert _serve(play, window, tmp_path, "--elevate") == 1
    assert window.kinds() == ["hello", "elevate", "done"] and window.messages[-1]["reason"] == "relay_failed"


def test_the_window_hears_plays_order_elevate_then_launch_and_waiting_then_target_and_recording(play, prompted,
                                                                                                 tmp_path):
    window = tc.Window(prompted)
    seen_at_wait = []
    deadline = time.monotonic() + tc.WITHIN_S

    def in_wait():                                      # the wait cannot go on until the window has heard both
        while "waiting" not in window.kinds() and time.monotonic() < deadline:
            time.sleep(0.01)
        seen_at_wait.extend(window.kinds())
    play["in_wait"] = in_wait
    assert _serve(play, window, tmp_path, "--elevate") == 0
    assert seen_at_wait == ["hello", "elevate", "launch", "waiting"]
    order = [k for k in window.kinds() if k in ("hello", "elevate", "already_running", "launch", "waiting", "target",
                                                "recording", "done")]
    assert order == ["hello", "elevate", "launch", "waiting", "target", "recording", "done"]
    assert {"t": "launch"} in window.messages and window.messages[-1]["reason"] == "completed"


def test_a_game_already_running_reaches_the_window_as_such_with_no_launch(play, tmp_path):
    play["running"] = [[{"pid": 4242, "create_time": NOW - 60 * SECOND, "name": "lantern_.exe", "exe": FOUND["exe"]}]]
    play["windows"] = [_window_of(4242)]
    window = tc.Window()
    assert _serve(play, window, tmp_path, "--elevate") == cli.EXIT_BAD_ARGS
    assert window.kinds() == ["hello", "already_running", "done"]
    heard = [m for m in window.messages if m["t"] != "say"]
    assert heard[-2:] == [{"t": "already_running", "name": "lantern_.exe", "pid": 4242},
                          {"t": "done", "reason": "game_running", "code": cli.EXIT_BAD_ARGS, "session": None}]
    assert "Not started: lantern_.exe already runs from the game's folder with a window" in window.said()


def test_a_wait_that_finds_no_game_reaches_the_window_after_its_launch_as_no_game(play, tmp_path):
    play["wait_raises"] = tgt.TargetNotFound("no program from the game's folder started within 5 minutes")
    window = tc.Window()
    assert _serve(play, window, tmp_path, "--no-elevate") == cli.EXIT_BAD_ARGS
    assert window.kinds() == ["hello", "launch", "waiting", "done"] and window.messages[-1]["reason"] == "no_game"


# ---------------------------------------------------------------- before the prompt ---

@pytest.mark.parametrize("windowed", [True, False])
def test_a_game_already_running_from_the_folder_with_a_window_is_refused_before_any_prompt(play, tmp_path, windowed):
    play["running"] = [[{"pid": 4242, "create_time": NOW - 60 * SECOND, "name": "lantern_.exe", "exe": FOUND["exe"]}]]
    if windowed:
        play["windows"] = [{"hwnd": 1, "pid": 4242, "title": "Lantern Drift", "area": 2_073_600, "minimized": False,
                            "monitor_device": None, "cls": "GameWindow"}]
    code = _watch(play, tmp_path, "--elevate")
    if windowed:
        assert code == cli.EXIT_BAD_ARGS and play["launches"] == [] and _steps(play) == ["already_running"]
        assert ("Not started: lantern_.exe already runs from the game's folder with a window, and a store starts "
                "nothing for a game that is running. Close it and run this again, or record it as it runs: lagdoctor "
                "watch --pid 4242.") in play["said"]
    else:                                               # a helper left running: the wait passes over it anyway
        assert code == 0 and len(play["launches"]) == 1


def test_a_folder_that_cannot_be_waited_on_is_refused_before_any_prompt(play, monkeypatch, tmp_path):
    def forms(folder):
        raise ValueError("C:\\Games\\Lantern Drift leads onto a network share")
    monkeypatch.setattr(tgt, "folder_forms", forms)
    assert _watch(play, tmp_path, "--elevate") == cli.EXIT_BAD_ARGS
    assert play["launches"] == [] and play["waits"] == [] and _steps(play) == []
    assert play["said"][-1] == ("Not started: the game's folder cannot be waited on (C:\\Games\\Lantern Drift leads "
                                "onto a network share).")


def test_a_cancel_before_the_prompt_raises_none(play, tmp_path):
    cancel = threading.Event()
    cancel.set()
    assert _watch(play, tmp_path, "--elevate", cancel=cancel) == cli.EXIT_CANCELLED
    assert play["launches"] == [] and play["waits"] == []


def test_a_cancel_during_the_prompt_waits_for_nothing_and_lets_the_relay_go(play, tmp_path):
    cancel = threading.Event()
    play["during_prompt"] = cancel.set
    assert _watch(play, tmp_path, "--elevate", cancel=cancel) == cli.EXIT_CANCELLED
    assert len(play["launches"]) == 1 and play["waits"] == [] and play["link"].closed >= 1
    assert _steps(play) == ["prompt"]


# ---------------------------------------------------------------- how the wait ends ---

@pytest.mark.parametrize("raised, code, words", [
    (tgt.TargetNotFound("no program from the game's folder started within 5 minutes"), cli.EXIT_BAD_ARGS,
     "No game was found: no program from the game's folder started within 5 minutes."),
    (tgt.WaitCancelled("the wait for the game was cancelled"), cli.EXIT_CANCELLED, "Cancelled: nothing was recorded."),
    (OSError("Windows' process list or windows could not be read 3 times in a row"), 1,
     "The wait for the game stopped: Windows' process list or windows could not be read 3 times in a row. Nothing "
     "was recorded."),
])
def test_a_wait_that_ends_without_a_game_says_why_records_nothing_and_lets_the_relay_go(play, tmp_path, raised, code,
                                                                                         words):
    play["wait_raises"] = raised
    assert _watch(play, tmp_path, "--elevate") == code
    assert words in play["said"] and play["link"].closed >= 1 and "recording" not in play["events"]


def test_a_cancel_wins_over_a_relay_that_ended_with_it(play, tmp_path):
    cancel = threading.Event()

    def both():
        cancel.set()
        play["link"].ended.set()
    play["in_wait"] = both
    play["wait_raises"] = tgt.WaitCancelled("the wait for the game was cancelled")
    assert _watch(play, tmp_path, "--elevate", cancel=cancel) == cli.EXIT_CANCELLED


# ---------------------------------------------------------------- without the relay ---

def test_without_the_relay_the_wait_looks_from_the_pre_flight_and_names_no_game(play, monkeypatch, fake_world,
                                                                                 tmp_path):
    monkeypatch.setattr(elevation, "can_trace", lambda: True)          # frame timing in this window: no prompt
    assert _watch(play, tmp_path) == 0
    assert play["launches"] == [] and _steps(play) == ["launch", "waiting", "wait", "target", "recording"]
    (kw,) = play["waits"]
    assert kw["since"] == NOW - tgt.SINCE_MARGIN and kw["resolve"] is True and "aim" not in kw and "own" not in kw
    assert 299.0 < kw["deadline_s"] <= 300.0
    assert fake_world["sessions"][0]["opts"].frames is True


@pytest.mark.parametrize("elevated", [lambda: True, lambda: 1 / 0])      # an administrator's, or one it cannot tell
def test_an_administrator_window_never_writes_a_short_name_out_long(play, monkeypatch, tmp_path, elevated):
    monkeypatch.setattr(elevation, "can_trace", lambda: True)
    monkeypatch.setattr(elevation, "is_elevated", elevated)
    assert _watch(play, tmp_path) == 0
    # the pre-flight's too, and the look before the wait
    assert play["waits"][0]["resolve"] is False and play["resolves"] == [False, False]


def test_with_the_relay_in_an_administrator_window_nothing_is_written_out_long(play, monkeypatch, tmp_path):
    monkeypatch.setattr(elevation, "is_elevated", lambda: True)          # the prompt here: frame timing needs it
    assert _watch(play, tmp_path, "--elevate") == 0
    (kw,) = play["waits"]
    assert kw["resolve"] is False and kw["expand_short"] is False and play["resolves"] == [False, False]


def test_a_declined_prompt_still_waits_and_records_without_frame_timing(play, fake_world, tmp_path):
    play["raise"] = PermissionError("declined")
    assert _watch(play, tmp_path, "--elevate") == 0
    assert _steps(play) == ["prompt", "launch", "waiting", "wait", "target", "recording"]
    (kw,) = play["waits"]
    assert kw["resolve"] is True and "aim" not in kw
    assert fake_world["sessions"][0]["opts"].frames is False


# ---------------------------------------------------------------- what it says while it waits ---

def test_the_wait_says_each_step_once_for_each_program_and_every_hold_re_arm_and_refusal():
    said = []
    status = cli._PlayStatus(said.append)
    for state, info in [("waiting", {}), ("started", {"pid": 41, "name": "launcher.exe"}),
                        ("started", {"pid": 41, "name": "launcher.exe"}), ("window", {"pid": 41, "name": "launcher.exe"}),
                        ("holding", {"pid": 41, "name": "launcher.exe", "seconds": 20.0}),
                        ("rearm", {"pid": 41, "name": "launcher.exe",
                                   "why": "launcher.exe (process 41) exited 5 seconds after it was chosen"}),
                        ("waiting", {}), ("window", {"pid": 41, "name": "launcher.exe"}),
                        ("holding", {"pid": 41, "name": "launcher.exe", "seconds": 20.0}),
                        ("short-name", {"pid": 43, "name": "lantern_.exe"}),
                        ("short-name", {"pid": 43, "name": "lantern_.exe"}),
                        ("refused", {"pid": 42, "name": "lantern_.exe", "why": "it could not be checked",
                                     "retry": True}),
                        ("windows-only", {"why": "the GPU counters gave no reading"})]:
        status(state, info)
    assert said == [
        "launcher.exe started from the game's folder: waiting for its window.",
        "launcher.exe has a window: waiting for it to draw.",
        "launcher.exe (pid 41) draws: watching it for 20 s, to tell the game from a launcher.",
        "Not the game: launcher.exe (process 41) exited 5 seconds after it was chosen, so it was taken for a launcher. "
        "Waiting for the game.",
        "launcher.exe (pid 41) draws: watching it for 20 s, to tell the game from a launcher.",
        "(lantern_.exe, pid 43, started from a path Windows gave in 8.3 short names, which this recording compares as "
        "written, so it is not taken. If it is the game, record it as it runs: lagdoctor watch --pid 43.)",
        "The relay refused lantern_.exe (pid 42): it could not be checked. Waiting for the game.",
        "The GPU counters give no reading (the GPU counters gave no reading), so a window from the game's folder that "
        "stays open is taken as the game.",
    ]


# ---------------------------------------------------------------- through the real wait ---

def _on_world(monkeypatch, play) -> tuple:
    """test_folder_wait's made-up machine under the real wait and pre-flight, a relay (30) on it that started before
    the wait, and the PlayLink naming it and this made-up recorder as lag-doctor's own. Its time moves by 1 s once
    the prompt is approved, then with each GPU sample."""
    world = fw.World(monkeypatch)
    before = {}

    def counted(*a, **kw):
        before["reads"] = dict(world.reads)
        return REAL_WAIT_FOR_GAME(*a, **kw)
    monkeypatch.setattr(tgt, "wait_for_game", counted)
    monkeypatch.setattr(tgt, "running_from", REAL_RUNNING_FROM)
    monkeypatch.setattr(tgt, "filetime_now", lambda: fw.SINCE - 10 * SECOND)
    monkeypatch.setattr(tgt, "REARM_HOLD_S", 0.0)     # the hold has its own tests; here one look
    relay = 30
    world.procs[relay] = fw.proc(relay, fw.SINCE, "pythonw.exe")
    real_init = PlayLink.__init__

    def on_the_world(self, *a):
        real_init(self, *a)
        self.recorder, self.pid, self.create_time = (fw.ME, fw.SINCE - 100 * SECOND), relay, fw.SINCE
        self.since = fw.SINCE                  # a relay's games start after its own start
        world.sleep(1.0)                               # the prompt takes a while: the game starts after the relay
    monkeypatch.setattr(PlayLink, "__init__", on_the_world)
    return world, relay, before


def test_through_the_real_wait_watch_names_only_the_game_the_relays_check_takes(play, monkeypatch, fake_world,
                                                                               tmp_path):
    world, relay, before = _on_world(monkeypatch, play)
    world.start(0.5, 31, fw.GAME, ppid=relay, window=fw.FULL_HD, draw=90.0)   # the relay's child, from the folder
    game_ct = world.start(0.5, 42, fw.GAME, window=fw.FULL_HD, draw=60.0)
    real_aim = PlayLink.aim

    def judged(self, game):                            # the relay's own judgement, on the same machine
        why, retry = tgt.check_proposed(world.snapshot, world.image_path, *game, session=1, folders=self.folders,
                                        since=self.create_time, own=self.own())
        real_aim(self, game)
        if why is not None:
            raise client.GameRefused(why, retry, sent=True)
    monkeypatch.setattr(PlayLink, "aim", judged)
    assert _watch(play, tmp_path, "--elevate", "--wait-s", "60") == 0
    assert play["link"].aimed == [(42, game_ct)]                    # never the relay's own child, though it drew more
    assert fake_world["sessions"][0]["target"]["pid"] == 42
    assert world.reads[31] == before["reads"].get(31, 0)            # the wait never read its path


def test_a_relay_that_ends_while_watch_waits_ends_the_wait_as_the_relay_failing(play, monkeypatch, tmp_path):
    world, relay, before = _on_world(monkeypatch, play)              # nothing starts from the folder
    play["in_wait"] = None
    real_init = PlayLink.__init__

    def ends_soon(self, *a):
        real_init(self, *a)
        timer = threading.Timer(0.3, self.ended.set)
        timer.daemon = True
        timer.start()
    monkeypatch.setattr(PlayLink, "__init__", ends_soon)
    began = time.monotonic()
    assert _watch(play, tmp_path, "--elevate", "--wait-s", "5") == 1
    assert time.monotonic() - began < 4.0
    assert ("Frame timing could not be started: the relay ended: it stopped on an error it did not expect. Nothing "
            "was recorded.") in play["said"]


def test_the_wait_counts_from_the_links_since_which_for_the_capture_service_is_its_ask(play, fake_world, tmp_path,
                                                                                        monkeypatch):
    real = PlayLink.__init__

    def asked_later(self, *args, **kwargs):              # the capture service: it ran before the ask
        real(self, *args, **kwargs)
        self.since = RELAY_CT + 7

    monkeypatch.setattr(PlayLink, "__init__", asked_later)
    assert _watch(play, tmp_path, "--elevate") == 0
    (kw,) = play["waits"]
    assert kw["since"] == RELAY_CT + 7 and play["link"].create_time == RELAY_CT
