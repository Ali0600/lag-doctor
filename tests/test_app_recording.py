"""Recording from the app (M4): the recorder the window starts and follows (lagdoctor/app/recording.py), the running
screen's view-model (views.recording_view), the sounds (lagdoctor/app/sounds.py), and the window's hide-and-suspend
decision. The recorder here is tests/fake_recorder.py, speaking the real control channel through control.Channel; the
relay is a harmless process of the test's own, handed over exactly as the window hands the real one.
"""
from __future__ import annotations

import io
import json
import queue
import sys
import time
import types
import wave
from pathlib import Path

import pytest

from lagdoctor import cli, control
from lagdoctor.app import main as app_main
from lagdoctor.app import recording, sounds, views
from lagdoctor.app import window as app_window
from lagdoctor.relay import client, protocol
from lagdoctor.winapi import webview2 as wv
from tests import fake_recorder

ROOT = Path(__file__).resolve().parents[1]
SID = "S-1-5-21-1000000001-1000000002-1000000003-1001"       # made up
ME = (1234, 133_000_000_000_000_000)                          # a made-up recorder: (pid, create time)
WITHIN_S = 30.0
OTHER_PY = "C:\\Other\\pythonw.exe"


@pytest.fixture(autouse=True)
def _no_real_uac(monkeypatch):
    """No test here may raise Windows' real UAC prompt: the app's own default launcher refuses."""
    from lagdoctor.winapi import elevation

    def refuse(*args, **kwargs):
        raise AssertionError("a test reached the real UAC prompt")
    monkeypatch.setattr(elevation, "launch_elevated", refuse)


# ------------------------------------------------------------------------------------ the relay's command --

def relay_cmd(recorder=ME, sid=SID) -> list[str]:
    """The command relay/client.relay_argv builds for the made-up recorder: what a recorder sends with "elevate"."""
    return client.relay_argv(protocol.pipe_name(), recorder, sid, (4, 1), presentmon_exe=None, frames=False,
                             network=True)


def problem(argv, cwd=None, *, recorder=ME, sid=SID) -> str | None:
    return recording.relay_problem(argv, str(client.PROJECT_ROOT) if cwd is None else cwd, recorder=recorder,
                                   sid=sid, python=client.relay_python(), root=recording.ROOT)


def test_the_app_and_the_recorder_name_the_same_python_and_folder_for_the_relay():
    assert recording.python() == client.relay_python()
    assert recording.ROOT == client.PROJECT_ROOT
    assert recording.RELAY_HEAD == relay_cmd()[1:6]


def test_the_relays_own_command_for_this_recorder_and_account_is_accepted():
    assert problem(relay_cmd()) is None
    upper = relay_cmd()
    upper[0] = upper[0].upper()                       # one file on Windows, whatever its case
    assert problem(upper) is None


def _with(argv: list[str], i: int, value: str) -> list[str]:
    argv = list(argv)
    argv[i] = value
    return argv


@pytest.mark.parametrize("argv, cwd, why", [
    ("not a list", None, "not lag-doctor's relay"),
    ([], None, "not lag-doctor's relay"),
    (relay_cmd()[:6], None, "not lag-doctor's relay"),                     # nothing past the head
    (_with(relay_cmd(), 0, OTHER_PY), None, "lag-doctor's own Python"),
    (_with(relay_cmd(), 5, "lagdoctor.relayed"), None, "not lag-doctor's relay"),
    (_with(relay_cmd(), 2, "-I"), None, "not lag-doctor's relay"),
    (relay_cmd()[:2] + relay_cmd()[3:], None, "not lag-doctor's relay"),   # -E left out
    (relay_cmd(), str(ROOT / "tests"), "lag-doctor's own folder"),
    (_with(relay_cmd(), 7, "not-a-pipe"), None, "relay would refuse"),
    (relay_cmd() + ["--extra"], None, "relay would refuse"),
    (relay_cmd(recorder=(ME[0] + 1, ME[1])), None, "another process as the recorder"),
    (relay_cmd(recorder=(ME[0], ME[1] + 1)), None, "another process as the recorder"),
    (relay_cmd(sid=SID[:-4] + "1002"), None, "another account"),
])
def test_anything_but_that_command_is_refused_before_any_prompt(argv, cwd, why):
    said = problem(argv, cwd)
    assert said is not None and why in said, said


def test_the_recorder_runs_watch_in_control_mode_with_this_trees_code():
    argv = recording.recorder_argv()
    assert argv[0] == str(recording.python()) and argv[1:5] == ["-B", "-E", "-s", "-m"]
    args = cli.build_parser().parse_args(argv[6:])
    assert args.cmd == "watch" and args.control == "stdio" and args.elevate and args.duration == recording.LENGTH_S


# ------------------------------------------------------------------------------------------- the state --

def feed(state: recording.RecordingState, *msgs: dict, now: float = 100.0) -> list[str]:
    heard = []
    for i, m in enumerate(msgs):
        heard += state.apply(m, now + i)
    return heard


RUN = [{"t": "hello", "v": 1, "pid": 7}, {"t": "waiting"}, {"t": "target", "name": "Demo Game", "pid": 9, "how": "auto"},
       {"t": "elevate", "argv": ["x"], "cwd": "c"}, {"t": "prompt", "how": "approved"},
       {"t": "recording", "duration_s": 600.0, "hotkey": True, "frames": True, "network": True},
       {"t": "mark", "at": 3.0}, {"t": "mark", "at": 4.0}, {"t": "stopping", "reason": control.STOPPED_REASON},
       {"t": "saving", "frames": 10}, {"t": "saved", "session": "20261109-120000-demo"}, {"t": "say", "text": "Saved."},
       {"t": "done", "reason": "stopped", "code": 0, "session": "20261109-120000-demo"}]


def test_a_recording_moves_through_its_phases_and_calls_for_its_sounds():
    s = recording.RecordingState()
    phases = []
    heard = []
    for i, m in enumerate(RUN):
        heard += s.apply(m, 100.0 + i)
        phases.append(s.phase)
    assert phases == ["starting", "waiting", "waiting", "asking", "preparing", "recording", "recording", "recording",
                      "stopping", "saving", "analysing", "analysing", "done"]
    assert heard == ["start", "mark", "mark", "done"]
    assert s.ok and s.session == "20261109-120000-demo" and s.marks == 2 and s.game == "Demo Game"
    assert (s.started, s.ended) == (105.0, 108.0)
    assert s.apply({"t": "mark", "at": 9.0}, 200.0) == [] and s.marks == 2           # nothing after the end


@pytest.mark.parametrize("how", ["approved", "declined", "refused", "failed", "withdrawn"])
def test_an_answered_prompt_moves_the_screen_on_to_getting_ready(how):
    s = recording.RecordingState()
    feed(s, *RUN[:4], {"t": "prompt", "how": how})
    assert s.phase == "preparing" and s.prompt == how
    words = views.recording_view(s, 200.0, "ctrl+alt+l")
    assert words["title"] == "Getting ready to record" and words["action"] == "cancel"
    assert "answer Windows' prompt" not in words["detail"]
    # Only what the prompt decided: an account in Performance Log Users records frames all the same.
    assert ("permission off" in words["detail"]) is (how in ("declined", "refused", "failed"))
    assert "frame timing" not in words["detail"] or how == "approved"


def test_a_recorder_that_ends_without_done_ends_the_recording_as_failed():
    s = recording.RecordingState()
    heard = feed(s, *RUN[:6], {"t": "exited", "code": 3})
    assert s.phase == "done" and s.reason == "failed" and s.code == 3 and not s.ok and heard[-1] == "problem"
    assert s.ended is not None


@pytest.mark.parametrize("fault, sound", [(None, []), ("it was not the recorder", ["problem"])])
def test_a_cancel_is_silent_unless_the_app_made_it(fault, sound):
    s = recording.RecordingState()
    msgs = RUN[:3] + ([{"t": "fault", "why": fault}] if fault else [])
    heard = feed(s, *msgs, {"t": "done", "reason": "cancelled", "code": 130, "session": None})
    assert heard == sound and s.phase == "done" and not s.ok


@pytest.mark.parametrize("reason", ["no_game", "busy", "relay_failed", "failed", "cancelled"])
def test_a_recording_that_ended_before_its_save_names_no_session(reason):
    s = recording.RecordingState()
    feed(s, *RUN[:3], {"t": "done", "reason": reason, "code": 1, "session": "20261109-120000-demo"})
    assert s.session is None and s.reason == reason


def test_an_analysis_that_failed_still_names_the_saved_session():
    s = recording.RecordingState()
    feed(s, *RUN[:11], {"t": "done", "reason": "analysis_failed", "code": 1, "session": "20261109-120000-demo"})
    assert s.session == "20261109-120000-demo" and not s.ok


@pytest.mark.parametrize("end", [{"t": "done", "reason": "failed", "code": 1, "session": "20261109-120000-demo"},
                                 {"t": "exited", "code": 3}])
def test_a_recording_that_failed_after_its_save_is_saved_not_analysed_and_still_opens(end):
    # The capture is on disk once "saved" came (the report's writing or a crash came after): the screen says so,
    # and offers it, rather than "Not recorded".
    s = recording.RecordingState()
    heard = feed(s, *RUN[:11], end)
    assert s.reason == "analysis_failed" and s.session == "20261109-120000-demo" and heard[-1] == "problem"
    v = views.recording_view(s, 300.0, "ctrl+alt+l")
    assert v["title"] == "Saved, not analysed" and v["session"] == "20261109-120000-demo"


# ------------------------------------------------------------------------------------ the running screen --

def test_every_way_a_recording_can_end_has_its_own_words():
    assert set(views.RECORDING_ENDS) == set(control.REASONS)
    phases = {"starting", "waiting", "asking", "preparing", "recording", "stopping", "saving", "analysing"}
    assert set(views.RECORDING_TITLES) == phases == set(views.RECORDING_DETAILS)
    assert set(views.PREPARING_DETAILS) == {"declined", "refused", "failed", "withdrawn"}


def test_no_recording_is_no_screen():
    assert views.recording_view(None, 0.0, "ctrl+alt+l") == {"active": False, "phase": None}


def test_the_running_screen_offers_cancel_then_stop_then_nothing():
    s = recording.RecordingState()
    actions = []
    for i, m in enumerate(RUN):
        s.apply(m, 100.0 + i)
        actions.append(views.recording_view(s, 100.0 + i, "ctrl+alt+l")["action"])
    assert actions == ["cancel"] * 5 + ["stop"] * 3 + [None] * 5


def test_the_clock_runs_while_recording_and_stops_with_the_recording():
    s = recording.RecordingState()
    feed(s, *RUN[:6], now=100.0)                      # recording from 105
    v = views.recording_view(s, 172.5, "ctrl+alt+l")
    assert v["elapsed_s"] == 67.5 and v["length_s"] == 600.0
    assert "Ctrl+Alt+L" in v["detail"] and "after 10 minutes" in v["detail"]
    s.length_s = 90.0
    assert "after 1m 30s" in views.recording_view(s, 172.5, "ctrl+alt+l")["detail"]
    s.apply({"t": "stopping", "reason": "duration"}, 705.0)
    assert views.recording_view(s, 900.0, "ctrl+alt+l")["elapsed_s"] == 600.0


@pytest.mark.parametrize("how, words", [("declined", "declined"), ("failed", "could not be shown"),
                                        ("refused", "refused"), (None, "could not start")])
def test_frame_timing_off_says_why(how, words):
    s = recording.RecordingState()
    msgs = RUN[:4] + ([{"t": "prompt", "how": how}] if how else [])
    feed(s, *msgs, {"t": "recording", "duration_s": None, "hotkey": True, "frames": False, "network": True})
    v = views.recording_view(s, 200.0, "ctrl+alt+l")
    assert any(n.startswith("Frame timing is off") and words in n for n in v["notes"]), v["notes"]
    # The game-server check needs the same permission: off whenever the prompt did not start the relay.
    assert any(n.startswith("The game-server check is off") for n in v["notes"]) is (how is not None)
    assert "after" not in v["detail"]


def test_the_game_server_check_is_said_off_even_when_frame_timing_is_on():
    # An account in Performance Log Users records frames without the relay, but declined, it has no tracker.
    s = recording.RecordingState()
    feed(s, *RUN[:4], {"t": "prompt", "how": "declined"},
         {"t": "recording", "duration_s": 600.0, "hotkey": True, "frames": True, "network": True})
    notes = views.recording_view(s, 200.0, "ctrl+alt+l")["notes"]
    assert not any(n.startswith("Frame timing is off") for n in notes)
    assert any(n.startswith("The game-server check is off") for n in notes)


@pytest.mark.parametrize("hotkey", [True, False])
def test_a_lag_key_windows_did_not_give_is_said_off(hotkey):
    s = recording.RecordingState()
    feed(s, *RUN[:5], {"t": "recording", "duration_s": 600.0, "hotkey": hotkey, "frames": True, "network": True})
    v = views.recording_view(s, 200.0, "ctrl+alt+l")
    assert ("Press Ctrl+Alt+L" in v["detail"]) is hotkey
    assert any(n.startswith("The lag key is off") and "Ctrl+Alt+L" in n for n in v["notes"]) is not hotkey


def test_what_the_recorder_said_shows_only_when_a_recording_failed():
    ok = recording.RecordingState()
    feed(ok, *RUN)
    assert views.recording_view(ok, 300.0, "ctrl+alt+l")["said"] == []
    bad = recording.RecordingState()
    feed(bad, *RUN[:3], {"t": "say", "text": "Could not find the game: none has a window."},
         {"t": "done", "reason": "no_game", "code": 2, "session": None})
    v = views.recording_view(bad, 300.0, "ctrl+alt+l")
    assert v["said"] == ["Could not find the game: none has a window."] and v["title"] == "No game found"
    assert v["detail"].endswith("What lag-doctor said is below.")
    assert v["session"] is None and v["ok"] is False and v["active"] is False


# Lines cli.cmd_watch and its helpers say for a console, word for word: the window cannot take what they advise.
CONSOLE_LINES = [
    "Start the game first, or name it with --game NAME.exe or --pid PID.",
    "Nothing was recorded. Start the recording again once that is done, or use --no-elevate instead of --elevate "
    "to record everything else without frame timing.",
    "Recording. Play normally; press Ctrl+Alt+L the moment it lags; Ctrl+C stops. Stops by itself after 600 s.",
    "When the relay checked, the game or this window was no longer running as approved (the game may have exited "
    "while the UAC prompt was up), so nothing was recorded. Start the game again, then run `lagdoctor watch` again.",
]


def test_lines_the_recorder_says_for_a_console_are_left_out_and_the_rest_are_shown():
    s = recording.RecordingState()
    feed(s, *RUN[:3], {"t": "say", "text": "Could not find the game: no program with a window draws."},
         *({"t": "say", "text": line} for line in CONSOLE_LINES),
         {"t": "done", "reason": "no_game", "code": 2, "session": None})
    assert views.recording_view(s, 300.0, "ctrl+alt+l")["said"] == [
        "Could not find the game: no program with a window draws."]


def test_a_failure_with_nothing_said_names_the_log_and_promises_nothing_below():
    s = recording.RecordingState()
    feed(s, *RUN[:3], {"t": "exited", "code": 1})
    v = views.recording_view(s, 300.0, "ctrl+alt+l", log="C:\\x\\recorder.log")
    assert v["said"] == [] and "below" not in v["detail"]
    assert v["detail"].endswith("The details are in C:\\x\\recorder.log.")
    no_game = recording.RecordingState()
    feed(no_game, *RUN[:3], {"t": "done", "reason": "no_game", "code": 2, "session": None})
    assert "recorder.log" not in views.recording_view(no_game, 300.0, "ctrl+alt+l", log="C:\\x\\recorder.log")["detail"]
    explained = recording.RecordingState()                 # a failure the recorder said in its own lines
    feed(explained, *RUN[:3], {"t": "say", "text": "Frame timing could not be started: it ended. Nothing was recorded."},
         {"t": "done", "reason": "relay_failed", "code": 1, "session": None})
    v = views.recording_view(explained, 300.0, "ctrl+alt+l", log="C:\\x\\recorder.log")
    assert v["said"] and "recorder.log" not in v["detail"]


def test_a_fault_of_the_apps_own_leads_the_notes():
    s = recording.RecordingState()
    feed(s, *RUN[:2], {"t": "fault", "why": recording.NOT_THE_RECORDER_PID},
         {"t": "done", "reason": "cancelled", "code": 130, "session": None})
    v = views.recording_view(s, 300.0, "ctrl+alt+l")
    assert v["notes"][0].startswith("lag-doctor stopped this recording") and v["title"] == "Not recorded"


# ------------------------------------------------------------------------------- a recorder, for real --

STARTED: list = []                  # every stand-in recorder started, ended after its test (the `ended` fixture)


@pytest.fixture(autouse=True)
def ended():
    yield
    while STARTED:
        p = STARTED.pop()
        if p.poll() is None:
            p.kill()
            p.wait(10)


def start_fake(tmp_path: Path, plan: dict, *, launcher=None, snapshot=None):
    plan = dict(plan, report=str(tmp_path / "report.json"))
    (tmp_path / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    got: queue.Queue = queue.Queue()
    logs: list[str] = []
    rec = recording.Recorder(home=tmp_path, post=got.put, log=logs.append, hwnd=lambda: None, cwd=ROOT,
                             argv=[str(fake_recorder.BASE_PY), "-B", "-E", "-s", "-m", "tests.fake_recorder",
                                   str(tmp_path / "plan.json")], launcher=launcher, snapshot=snapshot)
    rec.start()
    STARTED.append(rec.proc)
    return rec, got, logs


def take(got: queue.Queue, until: str, seen: list | None = None) -> list[dict]:
    """Messages up to and including the first of type `until`, waiting at most WITHIN_S."""
    seen = [] if seen is None else seen
    deadline = time.monotonic() + WITHIN_S
    while time.monotonic() < deadline:
        try:
            m = got.get(timeout=0.2)
        except queue.Empty:
            continue
        seen.append(m)
        if m["t"] == until:
            return seen
    raise AssertionError(f"no {until!r} within {WITHIN_S} s: {seen}")


def report(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))


@pytest.fixture
def relays():
    """The stand-in relays the prompt started, ended afterwards."""
    children: list = []
    yield children
    for p in children:
        p.kill()
        p.wait(10)


def test_the_relays_handle_reaches_the_recorder_the_app_started(tmp_path, relays):
    calls: list = []
    rec, got, logs = start_fake(tmp_path, {"elevate": True, "end": "complete", "session": "20261109-120000-demo"},
                                launcher=fake_recorder.stand_in_prompt(calls, relays))
    msgs = take(got, "exited")
    kinds = [m["t"] for m in msgs]
    assert kinds[:4] == ["hello", "waiting", "say", "target"] and kinds[-2:] == ["done", "exited"]
    elevate = next(m for m in msgs if m["t"] == "elevate")
    assert calls == [{"argv": elevate["argv"], "cwd": elevate["cwd"], "hwnd": None}]
    assert {"t": "prompt", "how": "approved"} in msgs
    r = report(tmp_path)
    assert r["prompt"] == "approved" and r["relay_pid"] == relays[0].pid and r["relay_running"] is True
    assert [g["t"] for g in r["got"]] == ["elevated"] and r["got"][0]["pid"] == relays[0].pid
    recorded = next(m for m in msgs if m["t"] == "recording")
    assert recorded["frames"] is True
    done = next(m for m in msgs if m["t"] == "done")
    assert done == {"t": "done", "reason": "completed", "code": 0, "session": "20261109-120000-demo"}
    assert msgs[-1] == {"t": "exited", "code": 0} and not logs


@pytest.mark.parametrize("raised, prompt, how", [
    (PermissionError(13, "the administrator prompt was declined", None, 1223), "declined", "declined"),
    (OSError("no desktop"), "failed: Windows' prompt could not be shown (no desktop)", "failed"),
    (ValueError("bad argv for an elevated launch"), "refused: bad argv for an elevated launch", "refused"),
    (RuntimeError("an error nobody expected"), "failed: lag-doctor's window hit an error raising the prompt",
     "failed"),                                         # still answered: the recorder waits for the answer
])
def test_a_prompt_that_did_not_start_the_relay_is_told_to_the_recorder(tmp_path, raised, prompt, how):
    def launcher(argv, cwd, *, hwnd=None):
        raise raised
    rec, got, _ = start_fake(tmp_path, {"elevate": True, "end": "complete"}, launcher=launcher)
    msgs = take(got, "exited")
    assert report(tmp_path)["prompt"] == prompt
    assert {"t": "prompt", "how": how} in msgs
    assert next(m for m in msgs if m["t"] == "recording")["frames"] is False


def test_a_command_that_is_not_lag_doctors_relay_never_reaches_a_prompt(tmp_path, relays):
    calls: list = []
    rec, got, logs = start_fake(tmp_path, {"elevate": True, "end": "complete", "relay_cwd": str(ROOT / "tests")},
                                launcher=fake_recorder.stand_in_prompt(calls, relays))
    msgs = take(got, "exited")
    assert calls == [] and {"t": "prompt", "how": "refused"} in msgs
    assert report(tmp_path)["prompt"] == ("refused: lag-doctor's window refused the command: the command does not "
                                          "run from lag-doctor's own folder")
    assert any("refused the relay's prompt" in line for line in logs)


def test_a_recorder_that_names_another_pid_is_cancelled_and_never_handed_a_relay(tmp_path, relays):
    calls: list = []
    rec, got, logs = start_fake(tmp_path, {"hello_pid": "wrong", "elevate": True, "pause_s": 10, "end": "complete"},
                                launcher=fake_recorder.stand_in_prompt(calls, relays))
    msgs = take(got, "exited")
    assert {"t": "fault", "why": recording.NOT_THE_RECORDER_PID} in msgs
    assert calls == [] and report(tmp_path)["got"] == [{"t": "cancel"}]
    assert next(m for m in msgs if m["t"] == "done")["reason"] == "cancelled"


def test_a_recorder_that_asks_for_the_relay_without_saying_who_it_is_is_refused(tmp_path, relays):
    # Deaf: it takes the window's Cancel without acting on it, and asks for the relay all the same.
    calls: list = []
    rec, got, _ = start_fake(tmp_path, {"hello_pid": "wrong", "elevate": True, "deaf": True, "end": "complete"},
                             launcher=fake_recorder.stand_in_prompt(calls, relays))
    msgs = take(got, "exited")
    assert calls == [] and {"t": "prompt", "how": "refused"} in msgs
    assert report(tmp_path)["prompt"] == ("refused: lag-doctor's window refused the command: the recorder did not "
                                          "say who it is first")


def test_stop_reaches_the_recorder_once_it_records(tmp_path):
    rec, got, _ = start_fake(tmp_path, {"marks": 2, "frames_lost": "PresentMon ended", "session": "20261109-1-x"})
    seen = take(got, "recording")
    assert rec.stop()
    msgs = take(got, "exited", seen)
    assert [m["t"] for m in msgs].count("mark") == 2 and {"t": "frames_lost", "why": "PresentMon ended"} in msgs
    assert next(m for m in msgs if m["t"] == "stopping")["reason"] == control.STOPPED_REASON
    assert next(m for m in msgs if m["t"] == "done")["reason"] == "stopped"
    assert report(tmp_path)["got"] == [{"t": "stop"}]
    assert not rec.stop()                                   # it has ended: nothing more reaches it


def test_cancel_reaches_the_recorder_before_it_records(tmp_path):
    rec, got, _ = start_fake(tmp_path, {"pause_s": 20})
    seen = take(got, "target")
    assert rec.cancel()
    msgs = take(got, "exited", seen)
    assert "recording" not in [m["t"] for m in msgs]
    assert next(m for m in msgs if m["t"] == "done") == {"t": "done", "reason": "cancelled", "code": 130,
                                                        "session": None}


def test_a_recorder_that_exits_without_done_is_seen_to_exit(tmp_path):
    rec, got, _ = start_fake(tmp_path, {"end": "exit"})
    msgs = take(got, "exited")
    assert "done" not in [m["t"] for m in msgs] and msgs[-1] == {"t": "exited", "code": 3}


def test_lines_that_break_the_protocol_are_logged_and_the_rest_still_read(tmp_path):
    rec, got, logs = start_fake(tmp_path, {"garbage": True, "end": "complete"})
    msgs = take(got, "exited")
    assert next(m for m in msgs if m["t"] == "done")["reason"] == "completed"
    assert sum("was ignored" in line for line in logs) == 4, logs
    assert (tmp_path / recording.LOG_NAME).is_file()


# ------------------------------------------------------------------- the prompt's guards, without a child --

class Sink:
    """A recorder's stdin: what the app wrote to it, as messages."""

    def __init__(self) -> None:
        self.lines: list[dict] = []

    def write(self, line: bytes) -> int:
        self.lines.append(control.from_app(line))
        return len(line)

    def flush(self) -> None:
        pass


def bare_recorder(posted: list, *, launcher=None):
    """A Recorder whose process is a stand-in (pid 10): _take and _answer driven directly."""
    rec = recording.Recorder(home=Path("."), post=posted.append, log=lambda s: None, hwnd=lambda: None, argv=["x"],
                             launcher=launcher)
    rec.proc = types.SimpleNamespace(pid=10, stdin=Sink(), poll=lambda: None, _handle=0)
    return rec


def test_a_second_hello_is_a_fault_and_never_trusted():
    posted: list = []
    rec = bare_recorder(posted)
    rec._take({"t": "hello", "v": 1, "pid": 10})
    assert rec.hello is True and rec.proc.stdin.lines == []
    rec._take({"t": "hello", "v": 1, "pid": 10})          # the right pid again, from whatever wrote it
    assert rec.hello is False and {"t": "fault", "why": recording.SECOND_HELLO} in posted
    assert rec.proc.stdin.lines == [{"t": "cancel"}] and rec.withdrawn


def test_one_prompt_a_recording_and_a_second_request_is_refused_at_once():
    posted: list = []
    calls: list = []
    rec = bare_recorder(posted, launcher=lambda *a, **k: calls.append(a))
    elevate = {"t": "elevate", "argv": relay_cmd(), "cwd": str(client.PROJECT_ROOT)}
    rec._take(elevate)                                    # no hello first: its prompt thread refuses it
    rec._take(elevate)
    deadline = time.monotonic() + WITHIN_S
    while len(rec.proc.stdin.lines) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    whys = sorted(m["why"] for m in rec.proc.stdin.lines)
    assert [m["t"] for m in rec.proc.stdin.lines] == ["elevate_refused", "elevate_refused"] and calls == []
    assert whys == ["lag-doctor's window raises one prompt a recording",
                    "lag-doctor's window refused the command: the recorder did not say who it is first"]


def test_every_answer_fits_the_protocol_so_the_recorder_always_hears_it():
    posted: list = []
    rec = bare_recorder(posted)
    rec._answer("refused", "elevate_refused", why="x" * (control.MAX_TEXT + 500))
    [answer] = rec.proc.stdin.lines
    assert answer["t"] == "elevate_refused" and len(answer["why"]) == control.MAX_TEXT
    assert posted == [{"t": "prompt", "how": "refused"}]


class Gate:
    """A step that waits until the test lets it go, and says when it has been reached."""

    def __init__(self) -> None:
        import threading
        self.reached, self.go = threading.Event(), threading.Event()

    def __call__(self):
        self.reached.set()
        assert self.go.wait(WITHIN_S)


def test_a_cancel_before_the_prompt_is_raised_means_no_prompt(tmp_path, relays):
    # The prompt thread is held while it takes its snapshot; the player's Cancel comes; then it goes on.
    calls: list = []
    gate = Gate()
    from lagdoctor.winapi.ntquery import Snapshotter
    real = Snapshotter()

    def snapshot():
        gate()
        return real.snapshot()
    rec, got, logs = start_fake(tmp_path, {"elevate": True, "end": "complete"},
                                launcher=fake_recorder.stand_in_prompt(calls, relays), snapshot=snapshot)
    assert gate.reached.wait(WITHIN_S)
    assert rec.cancel()
    gate.go.set()
    msgs = take(got, "exited")
    assert calls == [] and next(m for m in msgs if m["t"] == "done")["reason"] == "cancelled"
    assert any("before Windows' prompt was raised" in line for line in logs)


def test_a_cancel_while_windows_asks_lets_the_relay_go_and_never_hands_it_over(tmp_path, relays):
    gate = Gate()
    calls: list = []
    stand_in = fake_recorder.stand_in_prompt(calls, relays)

    def launcher(argv, cwd, *, hwnd=None):
        gate()                                            # Windows asks; the player presses Cancel meanwhile
        return stand_in(argv, cwd, hwnd=hwnd)
    rec, got, logs = start_fake(tmp_path, {"elevate": True, "end": "complete"}, launcher=launcher)
    assert gate.reached.wait(WITHIN_S)
    assert rec.cancel()
    gate.go.set()
    msgs = take(got, "exited")
    assert len(calls) == 1 and {"t": "prompt", "how": "withdrawn"} in msgs
    assert "elevated" not in [g["t"] for g in report(tmp_path)["got"]]
    assert next(m for m in msgs if m["t"] == "done")["reason"] == "cancelled"


# ------------------------------------------------------------------------------------------- the app --

class Window:
    """What App.on_recorder uses of the window, recorded."""

    def __init__(self, in_front: bool) -> None:
        self.front, self.posted, self.recording, self.attention, self.queued = in_front, [], [], 0, []
        self.dropped = 0
        self.view_hidden = False

    def post(self, obj):
        self.posted.append(obj)
        return True

    def set_recording(self, on):
        self.recording.append(on)

    def in_front(self):
        return self.front

    def want_attention(self):
        self.attention += 1

    def drop_attention(self):
        self.dropped += 1

    def call_soon(self, fn):
        self.queued.append(fn)


def app_for(tmp_path, in_front: bool, recorder=None):
    """An App with no window, whose recorder would be a Python that does nothing and whose prompt refuses."""
    played = []

    def no_prompt(*args, **kwargs):
        raise OSError("no prompt in this test")
    app = app_main.App(home=tmp_path, runtime={"version": "1.0.0.0", "build": 4000}, create_fn=None,
                       log=lambda s: None, play=played.append, flash=lambda on: None, launcher=no_prompt,
                       recorder=recorder or (lambda: [str(fake_recorder.BASE_PY), "-I", "-c", "pass"]))
    app.window = Window(in_front)
    app.rec, app.rec_state = object(), recording.RecordingState()
    return app, played


@pytest.mark.parametrize("in_front", [False, True])
def test_the_app_follows_a_recording_and_wants_attention_only_with_another_window_in_front(tmp_path, in_front):
    app, played = app_for(tmp_path, in_front)
    for m in RUN[:-1]:
        app.on_recorder(app.rec, m)
    assert app.window.attention == (0 if in_front else 1)      # the prompt, raised behind the game
    assert app.window.dropped == 1                             # and dropped once the prompt was answered
    app.on_recorder(app.rec, RUN[-1])
    assert app.window.attention == (0 if in_front else 2)      # and the end
    assert played == ([] if in_front else ["ask"]) + ["start", "mark", "mark", "done"]
    views_ = [p["data"] for p in app.window.posted]
    assert all(p["type"] == "push" and p["kind"] == "recording" for p in app.window.posted)
    assert [v["seq"] for v in views_] == sorted({v["seq"] for v in views_})         # each newer than the last
    assert views_[-1]["phase"] == "done" and views_[-1]["session"] == "20261109-120000-demo"
    hidden_while = [on for on, m in zip(app.window.recording, RUN) if on]
    assert len(hidden_while) == 3                           # recording, and its two marks: the view may hide
    assert app.window.recording[-1] is False
    app.on_recorder(app.rec, {"t": "exited", "code": 0})
    assert app.window.attention == (0 if in_front else 2) and len(app.window.posted) == len(RUN)


def test_nothing_is_posted_to_a_hidden_view(tmp_path):
    app, played = app_for(tmp_path, False)
    for m in RUN[:6]:
        app.on_recorder(app.rec, m)
    posted = len(app.window.posted)
    app.window.view_hidden = True                          # the player is in the game
    app.on_recorder(app.rec, RUN[6])                       # a lag mark
    assert len(app.window.posted) == posted and played[-1] == "mark"
    app.window.view_hidden = False                         # back: the window pushes the newest itself (on_shown)
    app.push_recording()
    assert app.window.posted[-1]["data"]["notes"][-1] == "Lag marked 1 time."


def test_an_older_recordings_late_line_changes_nothing(tmp_path):
    app, played = app_for(tmp_path, False)
    app.on_recorder(object(), RUN[5])
    assert app.rec_state.phase == "starting" and played == [] and app.window.posted == []


def test_only_one_recording_at_a_time_and_none_to_stop_when_none_runs(tmp_path):
    app, _ = app_for(tmp_path, False)
    with pytest.raises(app_main.Gone, match="already running"):
        app.do_record()
    app.rec_state.apply({"t": "done", "reason": "stopped", "code": 0, "session": None}, 1.0)
    with pytest.raises(app_main.Gone, match="No recording is running"):
        app.do_stop_recording()
    with pytest.raises(app_main.Gone, match="No recording is running"):
        app.do_cancel_recording()


def test_a_recorder_that_cannot_start_leaves_record_free_for_the_next_try(tmp_path):
    argv = [[str(tmp_path / "no-such-python.exe")], [str(fake_recorder.BASE_PY), "-I", "-c", "pass"]]
    app, _ = app_for(tmp_path, False, recorder=lambda: argv.pop(0))
    app.rec, app.rec_state = None, None
    with pytest.raises(app_main.Gone, match="could not start"):
        app.do_record()
    assert app.rec is None and app.rec_state is None
    assert app.do_record()["phase"] == "starting"          # the next press starts one
    app.rec.proc.wait(30)


# ------------------------------------------------------------------------------- hiding the view --

class Com:
    """A stand-in view: records what was called on it, in order."""

    def __init__(self, calls: list, suspended: bool = False) -> None:
        self.calls, self.suspended = calls, suspended

    def __bool__(self):
        return True

    def put_bool(self, name, value):
        self.calls.append((name, value))
        if name == "put_IsVisible" and value:
            self.suspended = False

    def get_bool(self, name):
        self.calls.append((name,))
        return self.suspended

    def call(self, name, argtypes=(), *args):
        self.calls.append((name,))
        if name == "TrySuspend":
            self.suspended = True
        return 0

    def do(self, name, argtypes=(), *args):
        self.calls.append((name,))


@pytest.fixture
def view(monkeypatch):
    calls: list = []
    w = app_window.AppWindow(runtime={"version": "1.0.0.0"}, create_fn=None, data_folder=Path("."), assets=Path("."),
                             icon=None, class_name="test", on_message=lambda r: None, on_failed=lambda w: None,
                             log=calls.append)
    w.hwnd = 1                                            # never a real window: nothing reaches Windows for it
    w.controller = Com(calls)
    w.webview3 = Com(calls)
    shown, titles, painted, flashes = [], [], [], []
    w.on_shown = lambda: shown.append(1)
    w.flasher = flashes.append
    iconic = {"on": False}
    front = {"hwnd": 0}
    notify = {"state": wv.QUNS_ACCEPTS_NOTIFICATIONS}
    w.notify_state = lambda: notify["state"]
    monkeypatch.setattr(wv, "IsIconic", lambda hwnd: iconic["on"])
    monkeypatch.setattr(wv, "PostMessageW", lambda *a: 1)
    monkeypatch.setattr(wv, "SetWindowTextW", lambda hwnd, text: titles.append(text))
    monkeypatch.setattr(wv, "InvalidateRect", lambda *a: painted.append("invalidated"))
    monkeypatch.setattr(wv, "GetForegroundWindow", lambda: front["hwnd"])
    monkeypatch.setattr(wv, "BeginPaint", lambda hwnd, ps: 1)
    monkeypatch.setattr(wv, "EndPaint", lambda hwnd, ps: painted.append("end"))
    monkeypatch.setattr(wv, "CreateFontW", lambda *a: 0)
    monkeypatch.setattr(wv, "SetBkMode", lambda *a: 0)
    monkeypatch.setattr(wv, "SetTextColor", lambda *a: 0)
    monkeypatch.setattr(wv, "DrawTextW", lambda hdc, text, n, rc, flags: painted.append(text))
    return types.SimpleNamespace(w=w, calls=calls, shown=shown, iconic=iconic, titles=titles, painted=painted,
                                 front=front, notify=notify, flashes=flashes)


def activate(w, on: bool) -> None:
    w.on_window_message(w.hwnd, wv.WM_ACTIVATE, 1 if on else wv.WA_INACTIVE, 0)


def drain(w) -> None:
    while w.calls:
        w.calls.popleft()()


def test_the_view_is_hidden_then_suspended_while_a_game_records_and_the_window_is_not_shown(view):
    w = view.w
    activate(w, False)
    assert view.calls == []                               # no recording: nothing hides
    w.set_recording(True)
    assert view.calls[:2] == [("put_IsVisible", False), ("TrySuspend",)] and w.view_hidden
    view.calls.clear()
    activate(w, True)
    assert view.calls[0] == ("put_IsVisible", True) and not w.view_hidden
    drain(w)
    assert view.shown == [1]                              # the app pushes the running screen again
    view.calls.clear()
    view.iconic["on"] = True                              # minimized while active
    w.on_window_message(w.hwnd, wv.WM_SIZE, wv.SIZE_MINIMIZED, 0)
    assert ("put_IsVisible", False) in view.calls and w.view_hidden
    view.calls.clear()
    w.set_recording(False)                                # the recording stopped: the view comes back at once
    assert view.calls[0] == ("put_IsVisible", True) and not w.view_hidden


def test_a_suspended_view_is_resumed_when_it_comes_back(view):
    w = view.w
    w.set_recording(True)                                 # never activated: hidden and suspended at once
    assert w.webview3.suspended
    view.calls.clear()
    activate(w, True)
    assert view.calls == [("put_IsVisible", True), ("get_IsSuspended",), ("Resume",)]


def test_the_view_is_never_hidden_while_no_game_records(view):
    w = view.w
    for on in (False, True, False):
        activate(w, on)
    view.iconic["on"] = True
    w.on_window_message(w.hwnd, wv.WM_SIZE, wv.SIZE_MINIMIZED, 0)
    assert not any(c[0] == "put_IsVisible" and c[1] is False for c in view.calls if len(c) > 1)
    assert not w.view_hidden


@pytest.mark.parametrize("last_try, again", [(True, True), (False, False)])
def test_a_hidden_view_something_woke_is_suspended_again_unless_suspending_failed(view, last_try, again):
    w = view.w
    w.set_recording(True)
    w.suspended = last_try                                # what the last TrySuspend answered
    w.webview3.suspended = False                          # and the view is awake now
    view.calls.clear()
    w.on_window_message(w.hwnd, wv.WM_SIZE, wv.SIZE_RESTORED, 0)
    assert (("TrySuspend",) in view.calls) is again


def test_a_webview2_call_that_fails_while_hiding_is_logged_and_the_caller_goes_on(view):
    w = view.w

    def broken(name, value):
        raise app_window.ComError("put_IsVisible", -2147023649)
    w.controller.put_bool = broken
    w.set_recording(True)                                 # raises nothing
    assert any("showing or hiding the view" in str(c) for c in view.calls)


def test_the_title_says_recording_and_a_hidden_view_draws_one_still_line(view):
    w = view.w
    w.set_recording(True)
    w.set_recording(True)
    w.on_window_message(w.hwnd, wv.WM_PAINT, 0, 0)
    assert view.titles == [app_window.RECORDING_TITLE]
    assert view.painted == ["invalidated", app_window.HIDDEN_TEXT, "end"]     # hidden: drawn again, by the window
    w.set_recording(False)
    view.painted.clear()
    assert w.on_window_message(w.hwnd, wv.WM_PAINT, 0, 0) is None and view.painted == []   # Windows paints it
    assert view.titles == [app_window.RECORDING_TITLE, app_window.WINDOW_TITLE]


def heartbeat(w) -> None:
    w.on_window_message(w.hwnd, wv.WM_TIMER, app_window.HEARTBEAT_TIMER, 0)


def test_the_flash_runs_only_while_windows_says_it_is_a_moment_for_one(view):
    w = view.w
    view.notify["state"] = wv.QUNS_RUNNING_D3D_FULL_SCREEN       # a full-screen game is in front
    w.want_attention()
    heartbeat(w)
    assert view.flashes == [] and w.attention
    view.notify["state"] = wv.QUNS_ACCEPTS_NOTIFICATIONS         # the player stepped out of the game
    heartbeat(w)
    heartbeat(w)
    assert view.flashes == [True] and w.attention                # started once, still wanted
    view.notify["state"] = wv.QUNS_BUSY                          # and went back in: the flash stops
    heartbeat(w)
    assert view.flashes == [True, False]
    view.notify["state"] = wv.QUNS_ACCEPTS_NOTIFICATIONS
    heartbeat(w)
    assert view.flashes == [True, False, True]
    w.drop_attention()                                           # the prompt it was for was answered
    heartbeat(w)
    assert view.flashes == [True, False, True, False] and not w.attention and not w.flashing


def test_a_hidden_windows_still_line_is_drawn_again_when_the_window_is_resized(view):
    w = view.w
    w.set_recording(True)
    view.painted.clear()
    w.on_window_message(w.hwnd, wv.WM_SIZE, wv.SIZE_RESTORED, 0)
    assert view.painted == ["invalidated"]
    w.set_recording(False)
    view.painted.clear()
    w.on_window_message(w.hwnd, wv.WM_SIZE, wv.SIZE_RESTORED, 0)
    assert view.painted == []                                    # the view draws itself


def test_coming_to_the_front_ends_the_wish_for_a_flash(view):
    w = view.w
    view.notify["state"] = wv.QUNS_ACCEPTS_NOTIFICATIONS
    w.want_attention()
    assert view.flashes == [True]
    activate(w, True)                                            # the player came: Windows stops the flash itself
    view.notify["state"] = wv.QUNS_ACCEPTS_NOTIFICATIONS
    heartbeat(w)
    assert view.flashes == [True] and not w.attention and not w.flashing
    view.front["hwnd"] = w.hwnd                                  # already in front: nothing to flash for
    w.want_attention()
    assert view.flashes == [True] and not w.attention


def test_windows_answers_whether_this_is_a_moment_to_draw_attention():
    assert wv.notification_state() in range(wv.QUNS_NOT_PRESENT, wv.QUNS_APP + 1)


# ------------------------------------------------------------------------------------------- sounds --

def _note_spans(notes) -> list[tuple[int, int]]:
    spans, at = [], 0
    for i, (_, secs) in enumerate(notes):
        n = int(sounds.RATE * secs)
        spans.append((at, at + n))
        at += n + (int(sounds.RATE * sounds.GAP_S) if i < len(notes) - 1 else 0)
    return spans


@pytest.mark.parametrize("name", sorted(sounds.SOUNDS))
def test_each_sound_is_a_short_quiet_wav_whose_notes_fade_in_and_out(name):
    data = sounds.wav(sounds.SOUNDS[name])
    with wave.open(io.BytesIO(data), "rb") as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, sounds.RATE)
        n = w.getnframes()
        frames = w.readframes(n)
    notes = sounds.SOUNDS[name]
    spans = _note_spans(notes)
    assert n == spans[-1][1]
    values = memoryview(frames).cast("h")
    assert len(values) == n
    assert 0.2 * 32767 < max(abs(v) for v in values) <= sounds.VOLUME * 32767 + 1
    assert n / sounds.RATE < 0.5                          # short: the game's own sound carries on over it
    fade = int(sounds.RATE * sounds.FADE_S)
    ramp = sounds.VOLUME * 32767 / fade
    for a, b in spans:                                    # no click: each note rises from silence and falls to it
        for k in range(fade):
            assert abs(values[a + k]) <= ramp * k + 1, (name, a, k)
            assert abs(values[b - 1 - k]) <= ramp * k + 1, (name, b, k)


def test_the_player_writes_each_sound_once_and_mends_a_changed_one(tmp_path, monkeypatch):
    played, writes = [], []
    import winsound
    monkeypatch.setattr(winsound, "PlaySound", lambda *a: played.append(a))
    real_replace = sounds.os.replace
    monkeypatch.setattr(sounds.os, "replace", lambda a, b: (writes.append(b), real_replace(a, b)))
    p = sounds.Player(tmp_path, log=lambda s: None)
    assert p.play("start") and p.play("start")
    path = tmp_path / sounds.FOLDER / "start.wav"
    assert played == [(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)] * 2
    assert writes == [path]                               # written once, played twice
    assert sounds.Player(tmp_path, log=lambda s: None).play("start") and writes == [path]   # intact: kept
    path.write_bytes(b"not a sound")
    assert sounds.Player(tmp_path, log=lambda s: None).play("start")
    assert path.read_bytes() == sounds.wav(sounds.SOUNDS["start"]) and writes == [path, path]


def test_a_sound_that_cannot_play_or_be_written_is_left_out_and_logged(tmp_path, monkeypatch):
    import winsound

    def broken(*a):
        raise RuntimeError("Failed to play sound")
    monkeypatch.setattr(winsound, "PlaySound", broken)
    logs: list[str] = []
    assert sounds.Player(tmp_path, log=logs.append).play("done") is False
    assert logs and "did not play" in logs[0]
    blocked = tmp_path / "file"
    blocked.write_text("x")
    logs.clear()
    assert sounds.Player(blocked, log=logs.append).play("mark") is False and "could not be written" in logs[0]
