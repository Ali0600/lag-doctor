"""`lagdoctor watch --control stdio` (lagdoctor/control.py): the messages both ways, the channel, the relay's prompt
raised by the window, and whole recordings driven the way lag-doctor's window drives them, here and in a real child."""
from __future__ import annotations

import _winapi
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from lagdoctor import cli, control
from lagdoctor.capture import session as sess
from lagdoctor.capture import target as tgt
from lagdoctor.winapi import elevation
from tests.test_cli import FAKE_GAME_S, GAME, fake_relay, fake_world  # noqa: F401 - fixtures used below

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------------------- the messages ---

@pytest.mark.parametrize("msg", [{"t": "stop"}, {"t": "cancel"}, {"t": "declined"},
                                 {"t": "elevated", "handle": 0x1A4, "pid": 7716},
                                 {"t": "elevate_refused", "why": "its command names another recorder"},
                                 {"t": "elevate_failed", "why": "the prompt could not be shown"}])
def test_every_message_the_window_may_send_is_read_back_as_sent(msg):
    assert control.from_app(control.encode(**msg)) == msg


@pytest.mark.parametrize("line", [
    b"not json\n", b"[1, 2]\n", b'{"stop": 1}\n', b'{"t": 5}\n',
    '{"t": "elevate_failed", "why": "caf\u00e9"}\n'.encode(),       # only its one byte is wrong
    b'{"t": "start"}\n',                                          # not one the window may send
    b'{"t": "stop", "now": true}\n',                              # a field it may not have
    b'{"t": "elevated", "handle": 420}\n',                        # one it must have
    b'{"t": "elevated", "handle": true, "pid": 7716}\n',          # a bool is not a number here
    b'{"t": "elevated", "handle": 0, "pid": 7716}\n',
    b'{"t": "elevated", "handle": 18446744073709551616, "pid": 7716}\n',
    b'{"t": "elevated", "handle": 420, "pid": 0}\n',
    b'{"t": "elevate_failed", "why": 5}\n',
    ('{"t": "elevate_failed", "why": "' + "x" * (control.MAX_TEXT + 1) + '"}\n').encode(),
    b'{"t": "stop"}' + b" " * control.MAX_LINE + b"\n",           # longer than a line may be
])
def test_anything_else_from_the_window_is_refused(line):
    with pytest.raises(control.ProtocolError):
        control.from_app(line)


def test_what_the_recorder_sends_is_checked_too():
    good = [dict(t="hello", v=control.VERSION, pid=4), dict(t="waiting"),
            dict(t="target", name="Game.exe", pid=4242, how="auto"), dict(t="target", name=None, pid=4242, how=None),
            dict(t="elevate", argv=[r"C:\Python313\pythonw.exe", "-B"], cwd=r"C:\lag-doctor"),
            dict(t="recording", duration_s=None, hotkey=True, frames=False, network=True), dict(t="mark", at=12.5),
            dict(t="frames_lost", why="PresentMon stopped"), dict(t="stopping", reason="duration"),
            dict(t="saving", frames=12_000), dict(t="saved", session="20261109-140000-Game"),
            dict(t="say", text="Recording. Play normally"),
            dict(t="done", reason="completed", code=0, session="20261109-140000-Game")]
    for msg in good:
        assert control.from_recorder(control.encode(**msg)) == msg
    for msg in (dict(t="hello", v=control.VERSION + 1, pid=4), dict(t="done", reason="exploded", code=0, session=None),
                dict(t="elevate", argv="pythonw.exe", cwd="C:\\"), dict(t="recording", duration_s=-1.0, hotkey=True,
                                                                         frames=True, network=True),
                dict(t="saving", frames=-1), dict(t="say", text="x" * (control.MAX_SAY + 1))):
        with pytest.raises(control.ProtocolError):
            control.from_recorder(control.encode(**msg))


def test_a_line_longer_than_the_protocol_allows_is_never_made():
    with pytest.raises(control.ProtocolError):
        control.encode("say", text="x" * control.MAX_LINE)
    with pytest.raises(ValueError):
        control.encode("mark", at=float("nan"))                 # no NaN on the wire


def test_a_mark_reaches_the_window_under_a_name_that_is_not_the_messages():
    window = Window()
    channel = _channel(window)
    channel.event("mark", {"t": 61.25})             # what session.Observers is told
    channel.close(5.0)
    window.finish()
    assert window.messages == [{"t": "mark", "at": 61.25}] and window.errors == []


@pytest.mark.parametrize("code,seen,stopped_by,failed,reason", [
    (0, {"target", "recording"}, "duration", False, "completed"),
    (0, {"target", "recording"}, "game exited", False, "game_exited"),
    (0, {"target", "recording"}, control.STOPPED_REASON, False, "stopped"),
    (0, {"target", "recording"}, "", False, "stopped"),
    (cli.EXIT_CANCELLED, {"target"}, "", False, "cancelled"),
    (cli.EXIT_BAD_ARGS, set(), "", False, "no_game"),
    (cli.EXIT_BAD_ARGS, {"target"}, "", False, "refused"),
    (1, {"target"}, "", False, "relay_failed"),
    (1, {"target", "recording", "saved"}, "duration", False, "analysis_failed"),
    (1, set(), "", False, "failed"),
    (cli.EXIT_BUSY, set(), "", False, "busy"),
    (cli.EXIT_PRIVATE_COPY, set(), "", False, "private_copy"),
    (cli.EXIT_NOT_READY, set(), "", False, "not_ready"),
    (cli.EXIT_TARGET_CHANGED, {"target"}, "", False, "game_changed"),
    (0, {"target", "recording", "saved"}, "duration", True, "failed"),
    (99, set(), "", False, "failed"),
])
def test_how_a_recording_ended_literal_points(code, seen, stopped_by, failed, reason):
    assert control.done_reason(code, seen, stopped_by, failed=failed) == reason
    assert reason in control.REASONS


# ----------------------------------------------------------------------------------------- the channel ---

def _pipe():
    r, w = os.pipe()
    return os.fdopen(r, "rb"), os.fdopen(w, "wb", buffering=0)


def test_the_channel_never_waits_for_a_window_that_does_not_read(monkeypatch):
    monkeypatch.setattr(control, "MAX_QUEUED", 50)
    _, idle_in = _pipe()                          # nothing ever comes from the window
    unread, out = _pipe()                         # and nothing it is sent is ever read
    channel = control.Channel(_pipe()[0], out, log=lambda text: None)
    channel.start()
    started = time.monotonic()
    for n in range(400):                          # far past the pipe's buffer and the queue
        channel.say(f"line {n} " + "x" * 200)
    assert time.monotonic() - started < 2.0
    assert channel.dropped >= 400 - 50 - 60, channel.dropped


def test_a_window_that_goes_away_ends_the_channel_quietly():
    gone_in, out = _pipe()
    gone_in.close()                               # the window's end of the pipe is closed
    channel = control.Channel(_pipe()[0], out, log=lambda text: None)
    channel.start()
    channel.say("anyone there?")
    deadline = time.monotonic() + 5
    while not channel.broken.is_set() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert channel.broken.is_set()
    channel.say("still not raising")              # and nothing it does after that raises
    channel.close(1.0)


def test_a_window_that_stops_sending_is_still_written_to():
    inp, to_recorder = _pipe()
    to_recorder.close()                           # what the window sends ends at once
    from_recorder, out = _pipe()
    channel = control.Channel(inp, out, log=lambda text: None)
    channel.start()
    assert channel.gone.wait(5)
    channel.send("waiting")
    channel.close(5.0)
    out.close()
    assert from_recorder.read() == b'{"t":"waiting"}\n' and not channel.broken.is_set()


def test_stop_and_cancel_from_the_window_reach_watchs_events_and_bad_lines_are_skipped():
    for t in ("stop", "cancel"):
        inp, to_recorder = _pipe()
        logged = []
        channel = control.Channel(inp, _pipe()[1], log=logged.append)
        channel.start()
        to_recorder.write(b"garbage\n")
        to_recorder.write(b'{"t": "stop"}' + b" " * control.MAX_LINE + b"\n")   # too long: skipped whole
        to_recorder.write(control.encode(t))
        assert channel.stop.wait(5) and channel.cancel.is_set()
        assert channel.stop.reason == control.STOPPED_REASON
        assert len(logged) == 2 and all("ignored" in line for line in logged), logged
        to_recorder.close()                       # the window's end goes away: the channel knows
        assert channel.gone.wait(5)


# ------------------------------------------------------- the relay's UAC prompt, raised by the window ---

class Window:
    """lag-doctor's window's side of the two pipes: what the recorder sends it, and what it answers."""

    def __init__(self, answer=None):
        self.recorder_in, self.to_recorder = _pipe()
        self.from_recorder, self.recorder_out = _pipe()
        self.messages, self.errors = [], []
        self.answer = answer or (lambda window, msg: None)
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self):
        try:
            for line in iter(self.from_recorder.readline, b""):
                try:
                    msg = control.from_recorder(line)
                except control.ProtocolError as exc:
                    self.errors.append(f"{exc}: {line[:200]!r}")
                    continue
                self.messages.append(msg)
                try:
                    self.answer(self, msg)
                except OSError:
                    pass                           # the recorder has gone
        except (OSError, ValueError):
            pass                                   # this window closed its own end (it went away)

    def send(self, t, **fields):
        self.to_recorder.write(control.encode(t, **fields))

    def finish(self):
        self.recorder_out.close()                  # what serve wrote is all there is
        self._reader.join(10)

    def kinds(self):
        return [m["t"] for m in self.messages if m["t"] != "say"]

    def said(self):
        return "\n".join(m["text"] for m in self.messages if m["t"] == "say")


def _channel(window):
    channel = control.Channel(window.recorder_in, window.recorder_out, log=lambda text: None)
    channel.start()
    return channel


def _dup_here(handle):
    me = _winapi.GetCurrentProcess()
    return _winapi.DuplicateHandle(me, handle, me, 0, False, _winapi.DUPLICATE_SAME_ACCESS)


def test_the_window_raises_the_prompt_and_hands_the_relays_handle_over():
    child = subprocess.Popen([sys.executable, "-I", "-c", "import sys, time; time.sleep(0.4); sys.exit(7)"])
    try:
        def answer(window, msg):
            if msg["t"] == "elevate":
                window.send("elevated", handle=_dup_here(child._handle), pid=child.pid)
        window = Window(answer)
        channel = _channel(window)
        relay = channel.relay_launcher([r"C:\Python313\pythonw.exe", "-B", "-m", "lagdoctor.relay"], r"C:\lag-doctor")
        try:
            assert relay.pid == child.pid
            assert relay.wait(10) == 7                 # the handle is a real one, held here
        finally:
            relay.close()
        elevate = next(m for m in window.messages if m["t"] == "elevate")
        assert elevate == {"t": "elevate", "argv": [r"C:\Python313\pythonw.exe", "-B", "-m", "lagdoctor.relay"],
                           "cwd": r"C:\lag-doctor"}
    finally:
        child.wait(10)


@pytest.mark.parametrize("reply,raises", [
    ({"t": "declined"}, PermissionError), ({"t": "elevate_refused", "why": "not its own recorder"}, ValueError),
    ({"t": "elevate_failed", "why": "the prompt could not be shown"}, OSError), ({"t": "cancel"}, cli.Cancelled),
])
def test_what_the_window_answers_is_what_launch_elevated_would_raise(reply, raises):
    window = Window(lambda w, msg: msg["t"] == "elevate" and w.send(**reply))
    with pytest.raises(raises) as info:
        _channel(window).relay_launcher([r"C:\x.exe"], "C:\\")
    if raises is PermissionError:
        assert info.value.winerror == elevation.ERROR_CANCELLED       # cli._start_relay reads it as declined
    if raises in (ValueError, OSError) and reply.get("why"):
        assert str(info.value) == reply["why"]


def test_a_window_that_goes_away_during_the_prompt_lets_the_recording_go_on_without_it():
    window = Window(lambda w, msg: msg["t"] == "elevate" and w.to_recorder.close())
    with pytest.raises(OSError, match="went away"):
        _channel(window).relay_launcher([r"C:\x.exe"], "C:\\")


def test_a_handle_that_names_another_process_or_none_is_refused():
    child = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(0.3)"])
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateEventW.restype = ctypes.c_void_p
    event = k32.CreateEventW(None, True, False, None)
    try:
        window = Window(lambda w, msg: msg["t"] == "elevate"
                        and w.send("elevated", handle=_dup_here(child._handle), pid=child.pid + 4))
        with pytest.raises(OSError, match="names process"):
            _channel(window).relay_launcher([r"C:\x.exe"], "C:\\")
        window = Window(lambda w, msg: msg["t"] == "elevate" and w.send("elevated", handle=event, pid=child.pid))
        with pytest.raises(ValueError, match="not a process"):
            _channel(window).relay_launcher([r"C:\x.exe"], "C:\\")
    finally:
        k32.CloseHandle(ctypes.c_void_p(event))
        child.wait(10)


def test_a_cancel_already_given_raises_no_prompt():
    window = Window()
    channel = _channel(window)
    channel.cancel.set()
    with pytest.raises(cli.Cancelled):
        channel.relay_launcher([r"C:\x.exe"], "C:\\")
    window.finish()
    assert "elevate" not in window.kinds()


# ------------------------------------------------------------ whole recordings, as the window drives them ---

def _args(tmp_path, *extra):
    return cli.build_parser().parse_args(["watch", "--control", "stdio", "--pid", "4242", "--hotkey", "none",
                                          "--out", str(tmp_path / "out"), *extra])


def _serve(window, args):
    code = control.serve(args, window.recorder_in, window.recorder_out)
    window.finish()
    assert window.errors == []
    return code


def test_a_recording_the_window_drives_runs_from_hello_to_done(fake_world, tmp_path):
    window = Window()
    assert _serve(window, _args(tmp_path, "--no-elevate", "--duration", "0.2")) == 0
    assert window.kinds() == ["hello", "waiting", "target", "recording", "stopping", "saving", "saved", "done"]
    done = window.messages[-1]
    assert done["reason"] == "completed" and done["code"] == 0
    assert (tmp_path / "out" / done["session"] / cli.CAPTURE_NAME).is_file()
    for text in ("Watching Game.exe", "Recording. Play normally", "Analysing...", cli.REPORT_NAME):
        assert text in window.said()
    assert fake_world["sessions"][-1]["printer"] is cli._silent        # no status line: nobody reads a console


def test_a_refusal_before_anything_starts_reaches_the_window(fake_world, monkeypatch, tmp_path):
    from lagdoctor import paths
    copy = paths.PrivateCopy(app="Some.App_123", folder=r"C:\Packages\Some.App_123\LocalCache\Local\lag-doctor")
    monkeypatch.setattr(paths, "private_copy", lambda folder=None: copy)
    window = Window()
    assert _serve(window, _args(tmp_path)) == cli.EXIT_PRIVATE_COPY
    assert window.kinds() == ["hello", "done"] and window.messages[-1]["reason"] == "private_copy"
    assert "Not started:" in window.said()


def test_the_windows_stop_ends_the_recording(fake_world, tmp_path):
    window = Window(lambda w, msg: msg["t"] == "recording" and w.send("stop"))
    started = time.monotonic()
    assert _serve(window, _args(tmp_path, "--no-elevate")) == 0      # no --duration: only Stop ends it
    assert time.monotonic() - started < FAKE_GAME_S / 2
    assert window.messages[-1]["reason"] == "stopped"
    assert {"t": "stopping", "reason": control.STOPPED_REASON} in window.messages


def test_the_windows_cancel_before_the_recording_records_nothing(fake_world, monkeypatch, tmp_path):
    channels = []
    real_init = control.Channel.__init__

    def init(self, *a, **kw):
        real_init(self, *a, **kw)
        channels.append(self)
    monkeypatch.setattr(control.Channel, "__init__", init)

    def find_target(**kw):                        # the window's Cancel lands during the game's GPU sample
        assert channels[0].cancel.wait(5)
        return dict(GAME)
    monkeypatch.setattr(tgt, "find_target", find_target)
    window = Window(lambda w, msg: msg["t"] == "waiting" and w.send("cancel"))
    assert _serve(window, _args(tmp_path, "--no-elevate")) == cli.EXIT_CANCELLED
    assert window.kinds() == ["hello", "waiting", "target", "done"]
    assert window.messages[-1] == {"t": "done", "reason": "cancelled", "code": cli.EXIT_CANCELLED, "session": None}
    assert "Cancelled: nothing was recorded." in window.said()
    assert not (tmp_path / "out").exists() or not any((tmp_path / "out").iterdir())


def test_a_window_that_goes_away_does_not_end_the_recording(fake_world, tmp_path):
    def answer(w, msg):
        if msg["t"] == "recording":
            w.to_recorder.close()                 # the window closed: its end of stdin goes
            w.from_recorder.close()               # and it reads nothing more
    window = Window(answer)
    code = control.serve(_args(tmp_path, "--no-elevate", "--duration", "0.5"), window.recorder_in,
                         window.recorder_out)
    assert code == 0
    assert len(list((tmp_path / "out").glob(f"*/{cli.CAPTURE_NAME}"))) == 1   # saved all the same


def test_a_recording_that_cannot_start_is_done_with_its_reason(fake_world, monkeypatch, tmp_path):
    other = sess.CaptureLock()
    assert other.acquire()
    try:
        window = Window()
        assert _serve(window, _args(tmp_path, "--no-elevate")) == cli.EXIT_BUSY
    finally:
        other.release()
    assert window.kinds() == ["hello", "done"] and window.messages[-1]["reason"] == "busy"

    def nowhere(**kw):
        raise tgt.TargetNotFound("no process with PID 4242 is running")
    monkeypatch.setattr(tgt, "find_target", nowhere)
    window = Window()
    assert _serve(window, _args(tmp_path, "--no-elevate")) == cli.EXIT_BAD_ARGS
    assert window.kinds() == ["hello", "waiting", "done"] and window.messages[-1]["reason"] == "no_game"
    assert "Could not find the game: no process with PID 4242 is running" in window.said()


def test_an_error_inside_watch_is_said_and_done_as_failed(fake_world, monkeypatch, tmp_path):
    def broken(args, **kw):
        kw["observers"].event("target", name="Game.exe", pid=4242, how="pid")   # it failed after the game was found
        raise RuntimeError("an unexpected state")
    monkeypatch.setattr(cli, "cmd_watch", broken)
    window = Window()
    assert _serve(window, _args(tmp_path)) == 1
    assert window.messages[-1]["reason"] == "failed"
    assert "RuntimeError: an unexpected state" in window.said()


def test_the_relay_is_started_through_the_windows_prompt(fake_relay, fake_world, tmp_path):
    window = Window()
    assert _serve(window, _args(tmp_path, "--elevate", "--duration", "0.1")) == 0
    launcher = fake_relay["launches"][-1]["launcher"]
    assert getattr(launcher, "__func__", None) is control.Channel.relay_launcher


class Proc:
    def __init__(self, pid, ppid, create_time, name):
        self.pid, self.ppid, self.create_time, self.name = pid, ppid, create_time, name


def test_the_recorders_window_is_its_parent_and_its_webview2_children_are_roots():
    me = 900
    procs = {p.pid: p for p in (Proc(800, 4, 100, "pythonw.exe"), Proc(me, 800, 300, "pythonw.exe"),
                                Proc(810, 800, 200, "msedgewebview2.exe"), Proc(811, 810, 210, "msedgewebview2.exe"),
                                Proc(820, 800, 250, "steam.exe"), Proc(830, 800, 50, "msedgewebview2.exe"))}
    assert control.window_of(procs, me) == (800, 100)
    assert control.webview_roots((800, 100))(procs) == [(810, 200)]     # not its renderer, the store, or an older
    for changed in ({800: Proc(800, 4, 400, "pythonw.exe")},            # a newer process with the window's number
                    {800: Proc(800, 4, 100, "cmd.exe")},                # not a Python
                    {me: Proc(me, 777, 300, "pythonw.exe")}):           # no parent at all
        assert control.window_of({**procs, **changed}, me) is None


def test_the_window_and_its_webview2_processes_count_as_lagdoctors_own(fake_world, monkeypatch, tmp_path):
    me = os.getpid()
    table = {me: Proc(me, 800, 300, "python.exe"), 800: Proc(800, 4, 100, "pythonw.exe")}
    monkeypatch.setattr(sess, "_snapshot", lambda threads_for=(): dict(table))
    window = Window()
    assert _serve(window, _args(tmp_path, "--no-elevate", "--duration", "0.1")) == 0
    opts = fake_world["sessions"][-1]["opts"]
    assert opts.tool_alone == ((800, 100),) and opts.tool_roots is not None


# --------------------------------------------------------------------------------- a real recorder child ---

def test_stdout_is_taken_for_the_channel_before_anything_can_print_there(tmp_path):
    code = ("from lagdoctor import control\n"
            "inp, out = control._take_std()\n"
            "print('a stray line', flush=True)\n"
            "out.write(control.encode('waiting'))\n")
    done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert done.stdout == b'{"t":"waiting"}\n'
    assert b"a stray line" in done.stderr


def test_a_real_recorder_speaks_only_json_lines_on_stdout(tmp_path):
    env = {**os.environ, "LAGDOCTOR_HOME": str(tmp_path / "home"), "TEMP": str(tmp_path), "TMP": str(tmp_path)}
    proc = subprocess.run([sys.executable, "-m", "lagdoctor", "watch", "--control", "stdio", "--pid", "4000000000",
                           "--no-elevate", "--hotkey", "none"], cwd=ROOT, env=env, input=b"", capture_output=True,
                          timeout=120)
    messages = [control.from_recorder(line + b"\n") for line in proc.stdout.splitlines()]   # each one, or it fails
    kinds = [m["t"] for m in messages if m["t"] != "say"]
    assert kinds == ["hello", "waiting", "done"], (kinds, proc.stderr[-2000:])
    assert messages[-1] == {"t": "done", "reason": "no_game", "code": cli.EXIT_BAD_ARGS, "session": None}
    assert proc.returncode == cli.EXIT_BAD_ARGS
    assert any("Could not find the game" in m["text"] for m in messages if m["t"] == "say")
