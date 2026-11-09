"""Recording from the app (M4, docs/LAUNCHER-PLAN.md): the recorder the window starts, follows and answers.

The app never runs a Session (LAUNCHER-PLAN, "Three processes, one recording path"). "Record the game that's running"
starts `lagdoctor watch --control stdio --elevate` as the window's own child, with no window of its own, and follows
it over JSON lines (lagdoctor/control.py, DESIGN.md "The control channel"):
- the recorder holds the capture lock, finds the game, records, saves and analyses;
- the app shows where it has got to (RecordingState, which views.recording_view turns into the running screen), plays
  the sounds, and passes on Stop and Cancel;
- when the recorder asks for the relay, the app checks that the command is lag-doctor's relay for this very recorder
  and account (relay_problem), raises the UAC prompt with its own window as the prompt's owner, and hands the relay's
  process handle to the recorder (elevation.ElevatedChild.hand_over), which then checks the relay as it always has.
Closing the window never ends a recording that has begun: the recorder's stdin ends, and it records, saves and
analyses on its own. Closed before the recording begins, it is a Cancel (control.Channel).
"""
from __future__ import annotations

import collections
import os
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from lagdoctor import control

ROOT = Path(__file__).resolve().parents[2]      # the folder holding lagdoctor/: the recorder runs this tree's code
LENGTH_S = 600                                  # a recording's length unless stopped first: the plan's 10 minutes
LOG_NAME = "recorder.log"                       # the recorder's stderr, under lag-doctor's home
OLD_LOG_NAME = "recorder-previous.log"
RECORDER_HEAD = ["-B", "-E", "-s", "-m", "lagdoctor", "watch", "--control", "stdio", "--elevate"]
RELAY_HEAD = ["-B", "-E", "-s", "-m", "lagdoctor.relay"]    # relay/client.relay_argv's, before the relay's own
MAX_SAID = 40                                   # lines the recorder said, kept for the running screen
EXIT_WAIT_S = 10.0                              # for the recorder to end once its output has ended
NOT_THE_RECORDER_PID = "the process that answered is not the recorder lag-doctor started"
SECOND_HELLO = "the recorder said who it is a second time"
OK_REASONS = ("completed", "stopped", "game_exited")    # control.REASONS that leave an analysed recording


def python() -> Path:
    """pythonw.exe beside the base interpreter, as the relay's (relay/client.relay_python): no console window, and the
    process started is the recorder itself, so its parent is this window (control.window_of) and the relay's handle
    goes to it. A venv's python.exe is a launcher, which would start the recorder as a child of its own."""
    return Path(sys.base_exec_prefix) / "pythonw.exe"


def recorder_argv(length_s: float = LENGTH_S, interpreter: Path | None = None) -> list[str]:
    """The recorder's command line: -B writes no bytecode (an installed copy is never written to), -E -s read no
    PYTHON* variable and no user site-packages; the rest is watch's."""
    return [str(interpreter or python()), *RECORDER_HEAD, "--duration", f"{length_s:g}"]


def _same_path(a: Any, b: Any) -> bool:
    return isinstance(a, str) and os.path.normcase(a) == os.path.normcase(str(b))


def relay_problem(argv: Any, cwd: Any, *, recorder: tuple[int, int], sid: str, python: Path,
                  root: Path = ROOT) -> str | None:
    """Why the app will not raise the UAC prompt for this command, in plain words, or None: only lag-doctor's relay
    (relay/client.relay_argv), run by this Python from this tree, for this recorder (pid and create time) and this
    account, as the relay itself would accept it (relay/server.parse_args)."""
    if not isinstance(argv, list) or len(argv) <= 1 + len(RELAY_HEAD) or not all(isinstance(a, str) for a in argv):
        return "the command is not lag-doctor's relay"
    if not _same_path(argv[0], python):
        return "the command does not run lag-doctor's own Python"
    if argv[1:1 + len(RELAY_HEAD)] != RELAY_HEAD:
        return "the command is not lag-doctor's relay"
    if not _same_path(cwd, root):
        return "the command does not run from lag-doctor's own folder"
    from lagdoctor.relay.server import parse_args
    try:
        cfg = parse_args(argv[1 + len(RELAY_HEAD):])
    except ValueError as exc:
        return f"the relay would refuse its command line ({exc})"
    if tuple(cfg.recorder) != tuple(recorder):
        return "the command names another process as the recorder"
    if cfg.sid != sid:
        return "the command lets another account in"
    return None


# ------------------------------------------------------------------------------------------- the state --

@dataclass
class RecordingState:
    """Where a recording the app started has got to, from what its recorder said and what the app did. Changed on
    the window's thread only."""
    phase: str = "starting"      # starting, waiting, asking, preparing, recording, stopping, saving, analysing, done
    game: str | None = None
    hotkey: bool = False
    frames: bool | None = None   # from "recording": whether frame timing runs
    network: bool | None = None
    length_s: float | None = None
    started: float | None = None   # time.monotonic() when "recording" came
    ended: float | None = None     # and when "stopping" came
    marks: int = 0
    frames_lost: str | None = None
    prompt: str | None = None      # how the relay's prompt went: approved, declined, refused, failed
    stop_reason: str | None = None
    frames_saved: int | None = None
    session: str | None = None
    reason: str | None = None      # done's: one of control.REASONS
    code: int | None = None
    fault: str | None = None       # the app stopped the recording itself, and why
    unexplained: bool = False      # it ended without saying why (its details are only in the recorder's log)
    said: collections.deque = field(default_factory=lambda: collections.deque(maxlen=MAX_SAID))

    @property
    def active(self) -> bool:
        return self.phase != "done"

    @property
    def ok(self) -> bool:
        return self.reason in OK_REASONS and self.fault is None

    def apply(self, msg: dict[str, Any], now: float) -> list[str]:
        """Take one message, from the recorder (checked by control.from_recorder) or from the app's own side
        ("prompt", "fault", "exited"); returns the sounds it calls for. Nothing after "done" changes anything."""
        if self.phase == "done":
            return []
        t, sounds = msg["t"], []
        if t == "waiting":
            self.phase = "waiting"
        elif t == "target":
            self.game = msg["name"]
        elif t == "elevate":
            self.phase = "asking"
        elif t == "recording":
            self.phase, self.started = "recording", now
            self.length_s, self.hotkey = msg["duration_s"], msg["hotkey"]
            self.frames, self.network = msg["frames"], msg["network"]
            sounds.append("start")
        elif t == "mark":
            self.marks += 1
            sounds.append("mark")
        elif t == "frames_lost":
            self.frames_lost = msg["why"]
            sounds.append("problem")
        elif t == "stopping":
            self.phase, self.stop_reason, self.ended = "stopping", msg["reason"], now
        elif t == "saving":
            self.phase, self.frames_saved = "saving", msg["frames"]
        elif t == "saved":
            self.phase, self.session = "analysing", msg["session"]
        elif t == "say":
            self.said.append(msg["text"])
        elif t == "prompt":
            self.prompt = msg["how"]
            if self.phase == "asking":             # answered: the recorder starts what it can before recording
                self.phase = "preparing"
        elif t == "fault":
            self.fault = msg["why"]
        elif t in ("done", "exited"):
            reason = msg["reason"] if t == "done" else "failed"           # it ended without saying how
            self.unexplained = reason == "failed"
            if reason == "failed" and self.phase == "analysing":           # after the save: the capture is on disk
                reason = "analysis_failed"
            self.phase, self.code, self.reason = "done", msg["code"], reason
            self.session = (msg.get("session") or self.session) if reason in OK_REASONS + ("analysis_failed",) \
                else None
            if self.ended is None and self.started is not None:
                self.ended = now
            if self.reason != "cancelled" or self.fault is not None:
                sounds.append("done" if self.ok else "problem")
        return sounds


# ------------------------------------------------------------------------------------------- the process --

class Recorder:
    """The recorder process of one recording: started by start(), its lines read on a thread of its own, and the
    app's answers written to its stdin. Each message, checked, goes to `post` (the window's thread, through
    AppWindow.call_soon), with the app's own "prompt", "fault" and "exited" among them.

    `launcher` raises the UAC prompt (elevation.launch_elevated), `hwnd()` names the window it belongs to, `sid()` is
    this account's SID and `snapshot()` every process (ntquery), for the recorder's create time."""

    def __init__(self, *, home: Path, post: Callable[[dict[str, Any]], None], log: Callable[[str], None],
                 hwnd: Callable[[], int | None], argv: list[str] | None = None, cwd: Path = ROOT,
                 launcher: Callable[..., Any] | None = None, sid: Callable[[], str] | None = None,
                 snapshot: Callable[[], dict[int, Any]] | None = None, interpreter: Path | None = None) -> None:
        self.home = Path(home)
        self.post = post
        self.log = log
        self.hwnd = hwnd
        self.argv = argv if argv is not None else recorder_argv()
        self.cwd = Path(cwd)
        self.launcher = launcher
        self.sid = sid
        self.snapshot = snapshot
        self.interpreter = interpreter          # the relay's Python the recorder names (default: python())
        self.proc: subprocess.Popen | None = None
        self.hello: bool | None = None          # None until hello; False when its pid was not the recorder's
        self.withdrawn = False                  # a Stop or Cancel was sent, or the recorder ended: no prompt now
        self.asked = False                      # the recorder asked for the relay: one prompt per recording
        self._lock = threading.Lock()           # one writer to the recorder's stdin at a time
        self._closed = False

    # ---------------------------------------------------------------- starting it

    def start(self) -> None:
        """Start the recorder; OSError when it cannot be started."""
        log_path = self.home / LOG_NAME
        try:
            if log_path.is_file():
                os.replace(log_path, self.home / OLD_LOG_NAME)
        except OSError:
            pass
        env = dict(os.environ, LAGDOCTOR_HOME=str(self.home))     # it saves where this window reads
        with open(log_path, "ab") as err:
            self.proc = subprocess.Popen(self.argv, cwd=str(self.cwd), env=env, stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=err,
                                         creationflags=subprocess.CREATE_NO_WINDOW)
        threading.Thread(target=self._read, name="lagdoctor-app-recorder", daemon=True).start()

    # ---------------------------------------------------------------- what goes to it

    def send(self, t: str, **fields: Any) -> bool:
        """One message to the recorder; False when it can no longer be told anything."""
        try:
            line = control.encode(t, **fields)
            control.from_app(line)              # only what the protocol allows the window to say
        except (control.ProtocolError, TypeError, ValueError) as exc:
            self.log(f"a {t!r} message for the recorder could not be made: {exc}")
            return False
        with self._lock:
            if self._closed or self.proc is None or self.proc.stdin is None:
                return False
            try:
                self.proc.stdin.write(line)
                self.proc.stdin.flush()
                return True
            except (OSError, ValueError):
                self._closed = True
                return False

    def stop(self) -> bool:
        self.withdrawn = True
        return self.send("stop")

    def cancel(self) -> bool:
        self.withdrawn = True
        return self.send("cancel")

    # ---------------------------------------------------------------- what comes from it

    def _read(self) -> None:
        out = self.proc.stdout
        try:
            while True:
                line = out.readline(control.MAX_LINE + 1)
                if not line:
                    break
                if not line.endswith(b"\n"):
                    if len(line) <= control.MAX_LINE:
                        break                   # a last line without its newline, then the end
                    self.log("the recorder sent a line longer than the protocol allows; it was ignored")
                    while line and not line.endswith(b"\n"):
                        line = out.readline(control.MAX_LINE + 1)
                    continue
                try:
                    msg = control.from_recorder(line)
                except control.ProtocolError as exc:
                    self.log(f"the recorder sent a line that was ignored: {exc}")
                    continue
                self._take(msg)
        except (OSError, ValueError) as exc:
            self.log(f"reading the recorder: {exc}")
        finally:
            with self._lock:
                self._closed = self.withdrawn = True
                for pipe in (self.proc.stdin, out):
                    try:
                        if pipe is not None:
                            pipe.close()
                    except OSError:
                        pass
            try:
                code = self.proc.wait(EXIT_WAIT_S)
            except subprocess.TimeoutExpired:
                code = None
            self.post({"t": "exited", "code": code})

    def _take(self, msg: dict[str, Any]) -> None:
        t = msg["t"]
        if t == "hello":
            first = self.hello is None
            self.hello = first and msg["pid"] == self.proc.pid       # said once, and never trusted again after
            if not self.hello:                  # a launcher in between, or something else on the pipe
                self.log(f"the recorder said its pid is {msg['pid']}, but lag-doctor started {self.proc.pid}"
                         if first else "the recorder said who it is a second time")
                self.post({"t": "fault", "why": NOT_THE_RECORDER_PID if first else SECOND_HELLO})
                self.cancel()
        self.post(msg)
        if t == "elevate":                      # after the message itself, so the screen says "asking" first
            if self.asked:
                self.log("the recorder asked for the relay a second time; lag-doctor raises one prompt a recording")
                self.send("elevate_refused", why="lag-doctor's window raises one prompt a recording")
                return
            self.asked = True
            threading.Thread(target=self._elevate, args=(msg,), name="lagdoctor-app-prompt", daemon=True).start()

    # ---------------------------------------------------------------- the relay's prompt

    def _recorder_identity(self) -> tuple[int, int] | None:
        snapshot = self.snapshot
        if snapshot is None:
            from lagdoctor.winapi.ntquery import Snapshotter
            snapshot = Snapshotter().snapshot
        p = snapshot().get(self.proc.pid)
        return (p.pid, p.create_time) if p is not None else None

    def _answer(self, how: str, t: str, **fields: Any) -> None:
        if "why" in fields:                     # every answer fits the protocol, so the recorder always hears it
            fields["why"] = str(fields["why"])[:control.MAX_TEXT]
        self.send(t, **fields)
        self.post({"t": "prompt", "how": how})

    def _elevate(self, msg: dict[str, Any]) -> None:
        """Raise the relay's UAC prompt for the recorder and hand it the relay; on its own thread, since the prompt
        waits for the player. The recorder is always answered: it waits for the answer."""
        try:
            self._ask(msg)
        except Exception as exc:  # noqa: BLE001 - logged; the recorder records without the relay
            import traceback
            self.log("raising the relay's prompt:\n" + "".join(traceback.format_exception(exc)))
            self._answer("failed", "elevate_failed", why="lag-doctor's window hit an error raising the prompt")

    def _ask(self, msg: dict[str, Any]) -> None:
        try:
            if self.hello is not True:
                why = "the recorder did not say who it is first"
            else:
                me = self._recorder_identity()
                sid = (self.sid or _account_sid)()
                why = ("the recorder is not running" if me is None else
                       relay_problem(msg["argv"], msg["cwd"], recorder=me, sid=sid,
                                     python=self.interpreter or python(), root=self.cwd))
        except OSError as exc:
            why = f"lag-doctor could not check the command ({exc})"
        if why is not None:
            self.log(f"refused the relay's prompt: {why}")
            self._answer("refused", "elevate_refused", why=f"lag-doctor's window refused the command: {why}")
            return
        if self.withdrawn or self.proc.poll() is not None:
            self.log("the recording was stopped before Windows' prompt was raised; none was")
            self._answer("withdrawn", "elevate_refused", why="the recording was stopped before Windows asked")
            return
        launcher = self.launcher
        if launcher is None:
            from lagdoctor.winapi.elevation import launch_elevated as launcher
        from lagdoctor.winapi import webview2 as wv
        com = wv.CoInitializeEx(None, wv.COINIT_APARTMENTTHREADED)   # ShellExecuteEx may need COM on its thread
        try:
            child = launcher(msg["argv"], msg["cwd"], hwnd=self.hwnd() or None)
        except PermissionError:
            self._answer("declined", "declined")
            return
        except ValueError as exc:
            self._answer("refused", "elevate_refused", why=str(exc))
            return
        except OSError as exc:
            self._answer("failed", "elevate_failed", why=f"Windows' prompt could not be shown ({exc})")
            return
        finally:
            if com >= 0:
                wv.CoUninitialize()
        if self.withdrawn:                      # stopped while Windows asked: nobody records with this relay
            child.close()                       # it ends by itself once no recorder connects
            self.log("the recording was stopped while Windows asked; the relay was let go")
            self._answer("withdrawn", "elevate_failed", why="the recording was stopped while Windows asked")
            return
        handle = getattr(self.proc, "_handle", None)    # CreateProcess's handle: full access to this window's child
        try:
            if not handle:
                raise OSError("the recorder's process handle is not available")
            copy = child.hand_over(int(handle))
        except (OSError, ValueError) as exc:
            child.close()                          # the relay ends by itself once no recorder connects
            self._answer("failed", "elevate_failed", why=f"the relay could not be handed to the recorder ({exc})")
            return
        if not self.send("elevated", handle=copy, pid=child.pid):
            self.log("the recorder went away while Windows asked; the relay ends by itself")
        self.post({"t": "prompt", "how": "approved"})


def _account_sid() -> str:
    from lagdoctor.winapi.elevation import token_user_sid
    return token_user_sid()
