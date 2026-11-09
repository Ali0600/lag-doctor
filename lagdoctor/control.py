"""`lagdoctor watch --control stdio`: a recording that lag-doctor's window drives (M4, docs/LAUNCHER-PLAN.md).

The window starts this recorder with no console and two pipes. They carry JSON lines both ways: one object a line,
ASCII, at most MAX_LINE bytes, each with "t" naming it. The recorder runs `cli.cmd_watch` as a console does, through
its seams: every line it says goes to the window as `say`, the session's events as messages of their own, and nobody
is asked anything. The window's Stop and Cancel come back on stdin, and so does its answer to the relay's UAC prompt,
which the window raises with its own window as the prompt's owner (Microsoft's guidance: without a parent window the
prompt only blinks on the taskbar).

Before anything can print, `serve` keeps a private copy of stdout for the channel and points stdout itself at stderr
(the window's log file), so a stray print anywhere cannot corrupt the channel. One writer thread drains a bounded
queue, so nothing that tells the window anything ever waits on the pipe; a full queue drops lines, counted, and a write
that fails ends the channel quietly. The recording goes on when the window is gone (stdin ends): a closed window cannot
lose a recording.

From the recorder (`from_recorder` checks them):
  hello        {v, pid}                         the channel is up
  waiting      {}                               looking for the game
  target       {name, pid, how}                 the game was chosen
  elevate      {argv: [str], cwd}               frame timing needs the relay: raise the UAC prompt for this command
  recording    {duration_s, hotkey, frames, network}   everything has started
  mark         {at}                             the lag key, seconds from the start
  frames_lost  {why}                            frame timing stopped working
  stopping     {reason}                         the recording is stopping
  saving       {frames}; saved {session}        the capture is being saved; it is on disk (its folder's name)
  say          {text}                           a line watch said, in plain words
  done         {reason, code, session}          the end: one of REASONS, watch's exit code, the session or null
From the window (`from_app` checks them):
  stop         {}                               stop the recording (before it began: the same as cancel)
  cancel       {}                               before the recording: end with nothing recorded; after: stop
  elevated     {handle, pid}                    the relay runs: its process handle, put into this process
  declined     {}                               the prompt was declined
  elevate_refused {why}; elevate_failed {why}   the window refused the command; the prompt could not be shown
"""
from __future__ import annotations

import json
import math
import os
import queue
import sys
import threading
import traceback
from typing import Any, Callable

VERSION = 1
MAX_LINE = 65536                 # bytes in one line, its newline included, both ways
MAX_TEXT = 4000                  # a reason or a name
MAX_SAY = 32000                  # a line watch says (its summary runs to a few thousand characters)
MAX_ARGS = 64
MAX_QUEUED = 10_000              # lines waiting for the writer; past that the window is not reading, and lines drop
MAX_PID = 0xFFFFFFFF
MAX_HANDLE = (1 << 64) - 1
CLOSE_WAIT_S = 5.0               # for the last lines (done) to reach the window
STOPPED_REASON = "stopped from the window"
WEBVIEW2_EXE = "msedgewebview2.exe"
WINDOW_EXES = ("python.exe", "pythonw.exe")       # what lag-doctor's window runs as

# How a recording ended, for the window's own words (`done_reason`).
REASONS = ("completed", "stopped", "game_exited", "cancelled", "busy", "not_ready", "no_game", "refused",
           "game_changed", "relay_failed", "analysis_failed", "private_copy", "failed")


class ProtocolError(ValueError):
    """A line the protocol does not allow; its text says what, in plain words."""


# --------------------------------------------------------------------------------------------- lines ---

def encode(t: str, **fields: Any) -> bytes:
    """One message as one line."""
    line = json.dumps({"t": t, **fields}, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    data = line.encode("ascii") + b"\n"
    if len(data) > MAX_LINE:
        raise ProtocolError(f"a {t!r} message of {len(data)} bytes is longer than a line may be")
    return data


def _decode(line: bytes) -> dict[str, Any]:
    if not isinstance(line, bytes) or len(line) > MAX_LINE:
        raise ProtocolError("a line longer than the protocol allows")
    try:
        msg = json.loads(line.decode("ascii"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProtocolError(f"a line that is not ASCII JSON ({type(exc).__name__})") from None
    if not isinstance(msg, dict) or not isinstance(msg.get("t"), str):
        raise ProtocolError("a line that is not a message (an object with a name in 't')")
    return msg


def _fields(msg: dict[str, Any], *names: str) -> None:
    extra = sorted(set(msg) - {"t", *names})
    missing = sorted(set(names) - set(msg))
    if extra or missing:
        raise ProtocolError(f"a {msg['t']!r} message with fields {extra} it may not have, or without {missing}")


def _int(msg: dict[str, Any], name: str, lo: int, hi: int) -> int:
    v = msg[name]
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise ProtocolError(f"{msg['t']}.{name} is not a whole number from {lo} to {hi}")
    return v


def _text(msg: dict[str, Any], name: str, limit: int = MAX_TEXT, *, optional: bool = False) -> str | None:
    v = msg[name]
    if v is None and optional:
        return None
    if not isinstance(v, str) or len(v) > limit:
        raise ProtocolError(f"{msg['t']}.{name} is not text of at most {limit} characters")
    return v


def _bool(msg: dict[str, Any], name: str) -> bool:
    if not isinstance(msg[name], bool):
        raise ProtocolError(f"{msg['t']}.{name} is not true or false")
    return msg[name]


def _seconds(msg: dict[str, Any], name: str, *, optional: bool = False) -> float | None:
    v = msg[name]
    if v is None and optional:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
        raise ProtocolError(f"{msg['t']}.{name} is not a number of seconds")
    return float(v)


def from_app(line: bytes) -> dict[str, Any]:
    """A message from the window, checked; ProtocolError for anything else."""
    msg = _decode(line)
    t = msg["t"]
    if t in ("stop", "cancel", "declined"):
        _fields(msg)
    elif t == "elevated":
        _fields(msg, "handle", "pid")
        _int(msg, "handle", 1, MAX_HANDLE)
        _int(msg, "pid", 1, MAX_PID)
    elif t in ("elevate_refused", "elevate_failed"):
        _fields(msg, "why")
        _text(msg, "why")
    else:
        raise ProtocolError(f"a message the window may not send: {t[:40]!r}")
    return msg


def from_recorder(line: bytes) -> dict[str, Any]:
    """A message from the recorder, checked (the window's side, and the tests'); ProtocolError for anything else."""
    msg = _decode(line)
    t = msg["t"]
    if t == "hello":
        _fields(msg, "v", "pid")
        if _int(msg, "v", 0, 1 << 31) != VERSION:
            raise ProtocolError(f"a recorder that speaks version {msg['v']}, not {VERSION}")
        _int(msg, "pid", 1, MAX_PID)
    elif t == "waiting":
        _fields(msg)
    elif t == "target":
        _fields(msg, "name", "pid", "how")
        _text(msg, "name", optional=True)
        _int(msg, "pid", 1, MAX_PID)
        _text(msg, "how", optional=True)
    elif t == "elevate":
        _fields(msg, "argv", "cwd")
        argv = msg["argv"]
        if not isinstance(argv, list) or not 0 < len(argv) <= MAX_ARGS or not all(
                isinstance(a, str) and len(a) <= MAX_TEXT for a in argv):
            raise ProtocolError("elevate.argv is not a short list of short texts")
        _text(msg, "cwd")
    elif t == "recording":
        _fields(msg, "duration_s", "hotkey", "frames", "network")
        _seconds(msg, "duration_s", optional=True)
        for name in ("hotkey", "frames", "network"):
            _bool(msg, name)
    elif t == "mark":
        _fields(msg, "at")
        if isinstance(msg["at"], bool) or not isinstance(msg["at"], (int, float)) or not math.isfinite(msg["at"]):
            raise ProtocolError("mark.at is not a time")
    elif t == "frames_lost":
        _fields(msg, "why")
        _text(msg, "why")
    elif t == "stopping":
        _fields(msg, "reason")
        _text(msg, "reason")
    elif t == "saving":
        _fields(msg, "frames")
        _int(msg, "frames", 0, 1 << 40)
    elif t == "saved":
        _fields(msg, "session")
        _text(msg, "session", 260)
    elif t == "say":
        _fields(msg, "text")
        _text(msg, "text", MAX_SAY)
    elif t == "done":
        _fields(msg, "reason", "code", "session")
        if msg["reason"] not in REASONS:
            raise ProtocolError("done.reason is not one the window knows")
        _int(msg, "code", -(1 << 31), (1 << 32) - 1)
        _text(msg, "session", 260, optional=True)
    else:
        raise ProtocolError(f"a message the recorder may not send: {t[:40]!r}")
    return msg


def done_reason(code: int, seen: set[str] | frozenset[str], stopped_by: str, *, failed: bool = False) -> str:
    """How a recording ended, from watch's exit code, the events that went out and the stop's reason."""
    from lagdoctor import cli
    if failed:
        return "failed"
    if code == 0:
        return {"duration": "completed", "game exited": "game_exited"}.get(stopped_by, "stopped")
    if code == cli.EXIT_CANCELLED:
        return "cancelled"
    if code == cli.EXIT_BAD_ARGS:
        return "refused" if "target" in seen else "no_game"
    if code == 1 and "target" in seen:
        return "analysis_failed" if "saved" in seen else "relay_failed"
    return {cli.EXIT_BUSY: "busy", cli.EXIT_NOT_READY: "not_ready", cli.EXIT_TARGET_CHANGED: "game_changed",
            cli.EXIT_PRIVATE_COPY: "private_copy"}.get(code, "failed")


# ------------------------------------------------------------------------------------------- channel ---

class Channel:
    """The two pipes to the window. `send` never waits; `stop` and `cancel` are the events watch is given."""

    def __init__(self, inp: Any, out: Any, log: Callable[[str], None] | None = None) -> None:
        self._inp, self._out = inp, out
        self._log = log or _log
        self._lines: queue.Queue[bytes | None] = queue.Queue(MAX_QUEUED)
        self._replies: queue.Queue[dict[str, Any]] = queue.Queue()
        self.dropped = 0                       # lines the window was not reading fast enough for
        self.gone = threading.Event()          # what the window sends has ended (its end of stdin closed)
        self.broken = threading.Event()        # a write to the window failed: nothing more is written
        self.stop = threading.Event()          # watch's stop: the window's Stop (session.request_stop)
        self.cancel = threading.Event()        # watch's cancel: the window's Cancel, or a Stop before recording
        self.seen: set[str] = set()            # the events that went out
        self.stopped_by = ""                   # the recording's stop reason, from "stopping"
        self.session: str | None = None        # the saved session's folder name, from "saved"
        self._writer = threading.Thread(target=self._write_loop, name="lagdoctor-control-out", daemon=True)
        self._reader = threading.Thread(target=self._read_loop, name="lagdoctor-control-in", daemon=True)

    def start(self) -> None:
        self._writer.start()
        self._reader.start()

    # what goes to the window
    def send(self, t: str, **fields: Any) -> None:
        try:
            line = encode(t, **fields)
        except (ProtocolError, TypeError, ValueError) as exc:
            self._log(f"a {t!r} message could not be made: {exc}")
            return
        try:
            self._lines.put_nowait(line)
        except queue.Full:
            self.dropped += 1

    def say(self, text: str) -> None:
        """watch's say: never raises, never waits."""
        text = str(text)
        self.send("say", text=text if len(text) <= MAX_SAY else text[:MAX_SAY - 3] + "...")

    def event(self, kind: str, data: dict[str, Any]) -> None:
        """The session's and watch's events (session.Observers' on_event)."""
        self.seen.add(kind)
        if kind == "stopping":
            self.stopped_by = str(data.get("reason") or "")
        elif kind == "saved":
            self.session = data.get("session")
        elif kind == "mark":
            data = {"at": data.get("t")}            # "t" names the message here
        self.send(kind, **data)

    def close(self, wait_s: float = CLOSE_WAIT_S) -> None:
        """Let the writer send what is queued (done last), for at most wait_s."""
        try:
            self._lines.put(None, timeout=wait_s)
        except queue.Full:
            return
        self._writer.join(wait_s)

    def _write_loop(self) -> None:
        while True:
            line = self._lines.get()
            if line is None:
                return
            if self.broken.is_set():
                continue                          # the window can no longer read: drop the rest
            try:
                self._out.write(line)
                self._out.flush()
            except (OSError, ValueError):
                self.broken.set()

    # what comes from the window
    def _read_loop(self) -> None:
        try:
            while True:
                line = self._inp.readline(MAX_LINE + 1)
                if not line:
                    return                        # stdin ended: the window is gone
                if not line.endswith(b"\n"):
                    if len(line) > MAX_LINE:      # too long: drop it whole, up to its newline
                        self._log("a message from the window was longer than the protocol allows; it was ignored")
                        while line and not line.endswith(b"\n"):
                            line = self._inp.readline(MAX_LINE + 1)
                        continue
                    return                        # a last line without its newline, then the end
                try:
                    self._handle(from_app(line))
                except ProtocolError as exc:
                    self._log(f"a message from the window was ignored: {exc}")
        except (OSError, ValueError):
            return
        finally:
            self.gone.set()
            self._replies.put({"t": "gone"})      # wakes a prompt that waits

    def _handle(self, msg: dict[str, Any]) -> None:
        from lagdoctor.capture.session import request_stop
        t = msg["t"]
        if t in ("stop", "cancel"):
            self.cancel.set()                      # before the recording, nothing is recorded
            request_stop(self.stop, STOPPED_REASON)  # once it began, this is what stops it
        else:
            self._replies.put(msg)

    # the relay's UAC prompt, raised by the window
    def relay_launcher(self, argv: list[str], cwd: str) -> Any:
        """relay/client.launch's launcher here: the window raises the prompt with its own window and hands the relay's
        process handle to this process (elevation.ElevatedChild.hand_over). As launch_elevated does, it raises
        PermissionError when the prompt is declined, ValueError when the command was refused and OSError when the
        prompt could not be shown (or the window went away); cli.Cancelled for the window's Cancel."""
        from lagdoctor.cli import Cancelled
        from lagdoctor.winapi import elevation
        if self.cancel.is_set():
            raise Cancelled()
        while not self._replies.empty():           # nothing from an earlier prompt may answer this one
            stale = self._replies.get_nowait()
            if stale["t"] == "gone":
                self._replies.put(stale)
                break
        self.send("elevate", argv=[str(a) for a in argv], cwd=str(cwd))
        while True:
            try:
                reply = self._replies.get(timeout=0.25)
            except queue.Empty:
                if self.cancel.is_set():
                    raise Cancelled() from None
                continue
            t = reply["t"]
            if t == "gone":
                self._replies.put(reply)
                raise OSError("lag-doctor's window went away before the prompt was answered")
            if t == "declined":
                raise PermissionError(13, "the administrator prompt was declined", None, elevation.ERROR_CANCELLED)
            if t == "elevate_refused":
                raise ValueError(reply["why"])
            if t == "elevate_failed":
                raise OSError(reply["why"])
            child = elevation.ElevatedChild.adopt(reply["handle"])     # "elevated"
            if child.pid != reply["pid"]:
                child.close()
                raise OSError(f"the window's handle names process {child.pid}, not the relay's {reply['pid']}")
            if self.cancel.is_set():
                child.close()                      # the relay ends by itself once no recorder connects
                raise Cancelled()
            return child


def _log(text: str) -> None:
    """For the window's log file (stderr)."""
    try:
        print(f"lag-doctor recorder: {text}", file=sys.stderr, flush=True)
    except (OSError, ValueError, AttributeError):
        pass


# ------------------------------------------------------------------------------- lag-doctor's window ---

def window_of(procs: dict[int, Any], me: int | None = None) -> tuple[int, int] | None:
    """lag-doctor's window, the process that started this recorder: (pid, create_time) of this process's parent when
    it is a Python that started first (a parent's number may since have been given to another process). None when
    there is none, so watch counts nothing more as its own."""
    me = os.getpid() if me is None else me
    p = procs.get(me)
    parent = procs.get(p.ppid) if p is not None else None
    if (parent is None or parent.pid == me or parent.create_time > p.create_time
            or str(parent.name).lower() not in WINDOW_EXES):
        return None
    return parent.pid, parent.create_time


def webview_roots(window: tuple[int, int]) -> Callable[[dict[int, Any]], list[tuple[int, int]]]:
    """session.SessionOptions.tool_roots for the window: its WebView2 browser processes, its children named
    msedgewebview2.exe, found again in each snapshot (a recovery replaces them)."""
    pid, created = window

    def roots(procs: dict[int, Any]) -> list[tuple[int, int]]:
        return [(p.pid, p.create_time) for p in procs.values()
                if p.ppid == pid and p.create_time >= created and str(p.name).lower() == WEBVIEW2_EXE]
    return roots


# ----------------------------------------------------------------------------------------------- serve ---

def _take_std() -> tuple[Any, Any]:
    """The channel's own stdin and stdout. stdout itself (file 1) is pointed at stderr, the window's log file, so
    what anything else prints there never reaches the channel."""
    sys.stdout.flush()
    out = os.fdopen(os.dup(1), "wb", buffering=0)
    try:
        os.dup2(2, 1)
    except OSError:                                 # no stderr: what would print goes nowhere
        null = os.open(os.devnull, os.O_WRONLY)
        os.dup2(null, 1)
        os.close(null)
    return sys.stdin.buffer, out


def serve(args: Any, inp: Any = None, out: Any = None) -> int:
    """One recording for lag-doctor's window, then `done`; watch's exit code. `inp`/`out`: the pipes (default: stdin
    and stdout, taken by _take_std)."""
    from lagdoctor import cli
    from lagdoctor.capture import session as sess
    if inp is None or out is None:
        inp, out = _take_std()
    channel = Channel(inp, out)
    channel.start()
    code, failed = 1, False
    try:
        channel.send("hello", v=VERSION, pid=os.getpid())
        gate, private = cli._private_copy_gate(cli.PRIVATE_COPY_POLICY["watch"], show=channel.say)
        if gate is not None:
            code = gate
            return code
        if not private:
            cli._settle_journal()
        try:
            window = window_of(sess._snapshot())
        except OSError as exc:
            channel.say(f"(lag-doctor could not find its own window among the processes ({exc}), so the analysis "
                        "may name the window's processes among the other programs.)")
            window = None
        code = cli.cmd_watch(args, stop=channel.stop, say=channel.say, interactive=False,
                             observers=sess.Observers(on_event=channel.event), launcher=channel.relay_launcher,
                             cancel=channel.cancel, tool_alone=(window,) if window else (),
                             tool_roots=webview_roots(window) if window else None)
        return code
    except Exception as exc:  # noqa: BLE001 - the window is told, and the log gets the traceback
        failed = True
        _log("".join(traceback.format_exception(exc)))
        channel.say(f"lag-doctor stopped on an error it did not expect ({type(exc).__name__}: {str(exc)[:200]}).")
        return 1
    finally:
        channel.send("done", reason=done_reason(code, channel.seen, channel.stopped_by, failed=failed), code=code,
                     session=channel.session)
        if channel.dropped:
            _log(f"{channel.dropped} lines for the window were dropped: it was not reading")
        channel.close()
