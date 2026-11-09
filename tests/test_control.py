"""`lagdoctor watch --control stdio` (lagdoctor/control.py): the messages both ways, the channel, the relay's prompt
raised by the window, and whole recordings driven the way lag-doctor's window drives them, here and in a real child."""
from __future__ import annotations

import _winapi
import ctypes
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

from lagdoctor import cli, control
from lagdoctor.capture import session as sess
from lagdoctor.capture import target as tgt
from lagdoctor.model import Capture
from lagdoctor.winapi import elevation
from tests.test_cli import (  # noqa: F401 - fixtures, and test_cli's own guards, autouse here too
    FAKE_GAME_S, GAME, _no_real_uac, _private_capture_lock, _unelevated_owner, fake_relay, fake_world)

ROOT = Path(__file__).resolve().parents[1]
WITHIN_S = 10.0                  # a wait in the prompt that has not ended by then is a failure, never a hang
# A real recorder, with a capture lock of its own (a monkeypatch cannot reach a child), as test_cli's console child.
CHILD = ("import sys\nfrom lagdoctor.capture import session\nsession.CAPTURE_LOCK_NAME = sys.argv[1]\n"
         "from lagdoctor import cli\nraise SystemExit(cli.main(sys.argv[2:]))\n")
NOT_ASCII = "Pok" + chr(0xE9) + "mon.exe"


# ---------------------------------------------------------------------------------------- the messages ---

@pytest.mark.parametrize("msg", [{"t": "stop"}, {"t": "cancel"}, {"t": "declined"},
                                 {"t": "elevated", "handle": 0x1A4, "pid": 7716},
                                 {"t": "elevate_refused", "why": "its command names another recorder"},
                                 {"t": "elevate_failed", "why": "the prompt could not be shown"}])
def test_every_message_the_window_may_send_is_read_back_as_sent(msg):
    assert control.from_app(control.encode(**msg)) == msg


@pytest.mark.parametrize("line", [
    b"not json\n", b"[1, 2]\n", b'{"stop": 1}\n', b'{"t": 5}\n',
    ('{"t": "elevate_failed", "why": "caf' + chr(0xE9) + '"}\n').encode(),   # only its one byte is wrong
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
    b"[" * 5000 + b"]" * 5000 + b"\n",                            # nested past Python's recursion limit
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
                dict(t="elevate", argv="pythonw.exe", cwd="C:\\"), dict(t="elevate", argv=[], cwd="C:\\"),
                dict(t="elevate", argv=[5], cwd="C:\\"),
                dict(t="recording", duration_s=-1.0, hotkey=True, frames=True, network=True),
                dict(t="recording", duration_s=True, hotkey=True, frames=True, network=True),
                dict(t="recording", duration_s=None, hotkey="yes", frames=True, network=True),
                dict(t="mark", at="soon"), dict(t="mark", at=True),
                dict(t="saving", frames=-1), dict(t="say", text="x" * (control.MAX_SAY + 1))):
        with pytest.raises(control.ProtocolError):
            control.from_recorder(control.encode(**msg))


def test_a_line_longer_than_the_protocol_allows_is_never_made():
    with pytest.raises(control.ProtocolError):
        control.encode("say", text="x" * control.MAX_LINE)
    with pytest.raises(ValueError):
        control.encode("mark", at=float("nan"))                 # no NaN on the wire


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
    unread, out = _pipe()                         # nothing it is sent is ever read
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
    """lag-doctor's window's side of the two pipes: what the recorder sends it, and what it answers. A line it cannot
    read, or an answer that fails, closes its end of stdin, so a recorder waiting on it hears that at once."""

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
                    self._give_up()
                    continue
                self.messages.append(msg)
                try:
                    self.answer(self, msg)
                except OSError as exc:
                    self.errors.append(f"the answer failed: {exc}")
                    self._give_up()
        except (OSError, ValueError):
            pass                                   # this window closed its own end (it went away)

    def _give_up(self):
        try:
            self.to_recorder.close()
        except OSError:
            pass

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


def _launch(channel, argv=(r"C:\x.exe",), cwd="C:\\"):
    """relay_launcher, on a thread with a deadline: (the child, None) or (None, what it raised)."""
    got = {}

    def run():
        try:
            got["child"] = channel.relay_launcher(list(argv), cwd)
        except BaseException as exc:  # noqa: BLE001 - handed to the test
            got["raised"] = exc
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(WITHIN_S)
    assert not t.is_alive(), f"relay_launcher still waits after {WITHIN_S:.0f} s"
    return got.get("child"), got.get("raised")


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
        relay, raised = _launch(_channel(window), [r"C:\Python313\pythonw.exe", "-B", "-m", "lagdoctor.relay"],
                                r"C:\lag-doctor")
        assert raised is None, raised
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
    child, raised = _launch(_channel(window))
    assert child is None and type(raised) is raises, raised
    if raises is PermissionError:
        assert raised.winerror == elevation.ERROR_CANCELLED          # cli._start_relay reads it as declined
    if raises in (ValueError, OSError) and reply.get("why"):
        assert str(raised) == reply["why"]


def test_a_window_that_goes_away_during_the_prompt_lets_the_recording_go_on_without_it():
    window = Window(lambda w, msg: msg["t"] == "elevate" and w.to_recorder.close())
    child, raised = _launch(_channel(window))
    assert isinstance(raised, OSError) and "went away" in str(raised), raised


def test_a_window_that_cannot_read_the_request_is_given_up_on():
    inp, to_recorder = _pipe()                    # the window's stdin end stays open and says nothing
    unread, out = _pipe()
    unread.close()                                # but its end of what the recorder writes is gone
    channel = control.Channel(inp, out, log=lambda text: None)
    channel.start()
    child, raised = _launch(channel)
    assert isinstance(raised, OSError) and "cannot be reached" in str(raised), raised
    to_recorder.close()


def test_a_handle_that_names_another_process_or_none_is_refused():
    child = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(0.3)"])
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateEventW.restype = ctypes.c_void_p
    event = k32.CreateEventW(None, True, False, None)
    try:
        window = Window(lambda w, msg: msg["t"] == "elevate"
                        and w.send("elevated", handle=_dup_here(child._handle), pid=child.pid + 4))
        _, raised = _launch(_channel(window))
        assert isinstance(raised, OSError) and "names process" in str(raised), raised
        window = Window(lambda w, msg: msg["t"] == "elevate" and w.send("elevated", handle=event, pid=child.pid))
        _, raised = _launch(_channel(window))
        assert isinstance(raised, ValueError) and "not a process" in str(raised), raised
    finally:
        k32.CloseHandle(ctypes.c_void_p(event))
        child.wait(10)


def test_a_cancel_already_given_raises_no_prompt():
    window = Window()
    channel = _channel(window)
    channel.cancel.set()
    _, raised = _launch(channel)
    window.finish()
    assert isinstance(raised, cli.Cancelled) and "elevate" not in window.kinds()


def test_a_cancel_that_comes_with_the_approval_lets_the_relay_go():
    child = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(0.3)"])
    try:
        def answer(w, msg):                        # the Cancel is read before the approval it came with
            if msg["t"] == "elevate":
                w.to_recorder.write(control.encode("cancel") + control.encode("elevated", handle=_dup_here(
                    child._handle), pid=child.pid))
        relay, raised = _launch(_channel(Window(answer)))
        assert relay is None and isinstance(raised, cli.Cancelled), (relay, raised)
    finally:
        child.wait(10)


def test_a_stray_answer_from_before_the_prompt_does_not_answer_it():
    child = subprocess.Popen([sys.executable, "-I", "-c", "import sys, time; time.sleep(0.3); sys.exit(4)"])
    try:
        window = Window(lambda w, msg: msg["t"] == "elevate"
                        and w.send("elevated", handle=_dup_here(child._handle), pid=child.pid))
        channel = _channel(window)
        window.send("declined")                    # from no prompt this recorder asked for
        deadline = time.monotonic() + 5
        while channel._replies.empty() and time.monotonic() < deadline:
            time.sleep(0.01)
        relay, raised = _launch(channel)
        assert raised is None and relay.pid == child.pid, raised
        relay.close()
    finally:
        child.wait(10)


def test_a_name_that_is_not_ascii_reaches_the_window_as_it_was():
    window = Window()
    channel = _channel(window)
    channel.event("target", {"name": NOT_ASCII, "pid": 4242, "how": "auto"})
    channel.say("Watching " + NOT_ASCII)
    channel.close(5.0)
    window.finish()
    assert window.messages == [{"t": "target", "name": NOT_ASCII, "pid": 4242, "how": "auto"},
                               {"t": "say", "text": "Watching " + NOT_ASCII}] and window.errors == []


def test_a_mark_reaches_the_window_under_a_name_that_is_not_the_messages():
    window = Window()
    channel = _channel(window)
    channel.event("mark", {"t": 61.25})             # what session.Observers is told
    channel.close(5.0)
    window.finish()
    assert window.messages == [{"t": "mark", "at": 61.25}] and window.errors == []


# ------------------------------------------------------------ whole recordings, as the window drives them ---

def _args(tmp_path, *extra):
    return cli.build_parser().parse_args(["watch", "--control", "stdio", "--pid", "4242", "--hotkey", "none",
                                          "--out", str(tmp_path / "out"), *extra])


def _serve(window, args):
    code = control.serve(args, window.recorder_in, window.recorder_out)
    window.finish()
    assert window.errors == []
    return code


def _no_session(tmp_path):
    return not (tmp_path / "out").exists() or not any((tmp_path / "out").iterdir())


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


def test_the_journal_settled_on_the_way_in_is_told_to_the_window(fake_world, monkeypatch, tmp_path):
    def settle(say=None):
        (say or cli.say)("Settled a change that was interrupted last time: the refresh rate is back at 144 Hz")
    monkeypatch.setattr(cli, "_settle_journal", settle)
    window = Window()
    assert _serve(window, _args(tmp_path, "--no-elevate", "--duration", "0.1")) == 0
    assert "Settled a change that was interrupted last time" in window.said()


def test_the_windows_stop_ends_the_recording(fake_world, tmp_path):
    window = Window(lambda w, msg: msg["t"] == "recording" and w.send("stop"))
    started = time.monotonic()
    assert _serve(window, _args(tmp_path, "--no-elevate")) == 0      # no --duration: only Stop ends it
    assert time.monotonic() - started < FAKE_GAME_S / 2
    assert window.messages[-1]["reason"] == "stopped"
    assert {"t": "stopping", "reason": control.STOPPED_REASON} in window.messages


def _channel_made(monkeypatch):
    made = []
    real_init = control.Channel.__init__

    def init(self, *a, **kw):
        real_init(self, *a, **kw)
        made.append(self)
    monkeypatch.setattr(control.Channel, "__init__", init)
    return made


def test_the_windows_cancel_before_the_recording_records_nothing(fake_world, monkeypatch, tmp_path):
    made = _channel_made(monkeypatch)

    def find_target(**kw):                        # the window's Cancel lands during the game's GPU sample
        assert made[0].cancel.wait(5)
        return dict(GAME)
    monkeypatch.setattr(tgt, "find_target", find_target)
    window = Window(lambda w, msg: msg["t"] == "waiting" and w.send("cancel"))
    assert _serve(window, _args(tmp_path, "--no-elevate")) == cli.EXIT_CANCELLED
    assert window.kinds() == ["hello", "waiting", "target", "done"]
    assert window.messages[-1] == {"t": "done", "reason": "cancelled", "code": cli.EXIT_CANCELLED, "session": None}
    assert "Cancelled: nothing was recorded." in window.said()
    assert _no_session(tmp_path)


def test_a_cancel_while_the_recording_starts_records_nothing_either(fake_world, monkeypatch, tmp_path):
    made = _channel_made(monkeypatch)
    real_start = sess.Session._start_collectors

    def slow_start(self):                         # the inventory and the relay's trace take seconds
        assert made[0].cancel.wait(5)
        real_start(self)
    monkeypatch.setattr(sess.Session, "_start_collectors", slow_start)
    window = Window(lambda w, msg: msg["t"] == "target" and w.send("cancel"))
    assert _serve(window, _args(tmp_path, "--no-elevate")) == cli.EXIT_CANCELLED
    assert "recording" not in window.kinds() and window.messages[-1]["reason"] == "cancelled"
    assert _no_session(tmp_path) and fake_world["sessions"]                   # it had begun to start


def test_a_window_that_goes_away_does_not_end_the_recording(fake_world, tmp_path):
    def answer(w, msg):
        if msg["t"] == "recording":
            w.to_recorder.close()                 # the window closed: its end of stdin goes
            w.from_recorder.close()               # and it reads nothing more
    window = Window(answer)
    code = control.serve(_args(tmp_path, "--no-elevate", "--duration", "0.5"), window.recorder_in,
                         window.recorder_out)
    assert code == 0
    (path,) = (tmp_path / "out").glob(f"*/{cli.CAPTURE_NAME}")
    assert Capture.load(path).target["stop_reason"] == "duration"            # it ran to its end, all the same


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
    # The window closed and its number went to a newer program that hosts WebView2: none of that is lag-doctor's.
    reused = {**procs, 800: Proc(800, 4, 500, "Widgets.exe"), 840: Proc(840, 800, 510, "msedgewebview2.exe")}
    assert control.webview_roots((800, 100))(reused) == []


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


def _recorder(tmp_path):
    env = {**os.environ, "LAGDOCTOR_HOME": str(tmp_path / "home"), "TEMP": str(tmp_path), "TMP": str(tmp_path)}
    lock = f"Local\\lagdoctor-test-{uuid.uuid4().hex}"
    argv = [sys.executable, "-c", CHILD, lock, "watch", "--control", "stdio", "--pid", "4000000000", "--no-elevate",
            "--hotkey", "none"]
    return argv, env


def test_a_real_recorder_speaks_only_json_lines_on_stdout(tmp_path):
    argv, env = _recorder(tmp_path)
    proc = subprocess.run(argv, cwd=ROOT, env=env, input=b"", capture_output=True, timeout=120)
    messages = [control.from_recorder(line + b"\n") for line in proc.stdout.splitlines()]   # each one, or it fails
    kinds = [m["t"] for m in messages if m["t"] != "say"]
    assert kinds == ["hello", "waiting", "done"], (kinds, proc.stderr[-2000:])
    assert messages[-1] == {"t": "done", "reason": "no_game", "code": cli.EXIT_BAD_ARGS, "session": None}
    assert proc.returncode == cli.EXIT_BAD_ARGS
    assert any("Could not find the game" in m["text"] for m in messages if m["t"] == "say")


def test_a_real_recorder_exits_with_watchs_code_while_the_window_still_holds_its_stdin(tmp_path):
    argv, env = _recorder(tmp_path)
    log = tmp_path / "recorder.log"
    with open(log, "wb") as err:                  # a file, as the window's log is: a full pipe could block the child
        proc = subprocess.Popen(argv, cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err)
        lines = []
        try:
            timer = threading.Timer(120, proc.kill)            # never a hang
            timer.start()
            for line in iter(proc.stdout.readline, b""):      # stdin stays open all the while, as the window's does
                lines.append(control.from_recorder(line))
            code = proc.wait(60)
        finally:
            timer.cancel()
            if proc.poll() is None:
                proc.kill()
            proc.stdin.close()
    said = log.read_bytes()
    assert lines and lines[-1]["t"] == "done" and lines[-1]["code"] == cli.EXIT_BAD_ARGS, lines
    assert code == cli.EXIT_BAD_ARGS, (code, said[-2000:])
    assert b"Fatal Python error" not in said
