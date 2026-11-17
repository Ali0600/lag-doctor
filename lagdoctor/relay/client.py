"""The recorder's side of the relay: start it through one UAC prompt, prove who answered, and feed what it sends to
the session's "frames" and "net_flows" collectors.

`launch()` builds the relay's command line (everything the prompt approves), starts it with `elevation.launch_elevated`
under pythonw (no console window), connects to its pipe and checks it: the server's pid must be the relay's own (the
process handle held since the launch pins that pid), and the pipe's owner must be Administrators, which Windows lets
only an elevated token set. The prompt approves either the running game or, for Play, the game's install folder
before the store starts it: then `RelayLink.aim` names the game the folder wait found, once the recorder's own check
(target.check_proposed, with the arguments the relay will use) passes, and returns when the relay has accepted it too
(GameRefused when either says no: the relay waits on for another). Then `RelayLink` reads the relay's messages on one
thread and hands them on:
- `RelayedFrames` (collector "frames") is PresentMonSource fed the game's rows from the pipe instead of a child's CSV:
  the same storage, live frame rate and result, with PresentMon's status and details as the relay reports them;
- `RelayedEtwSession` stands in for the ETW session inside FlowTracker (`FlowTracker(relay=link)`): its events, its
  health, and the per-program byte totals for this Windows session.
A link that breaks (the relay ended, a message the protocol does not allow) ends both with the reason; the rest of the
recording goes on.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from lagdoctor import clock
from lagdoctor.capture.presentmon import TOOL, PresentMonSource
from lagdoctor.relay import protocol

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONNECT_S = 30.0              # from the relay's start to its pipe answering (its own checks come first)
HELLO_S = 10.0
NET_START_S = 15.0            # the relay says whether its ETW session started
STOP_FRAMES_S = 20.0          # past the relay's worst PresentMon stop (about 10 s), inside the session's 25 s join
STOP_NET_S = 4.0              # inside the session's 5 s stop for net_flows
MAX_EARLY_EVENTS = 50_000     # network events that come before the tracker has made its session: kept until it has
AIM_S = 10.0                  # Play: for the relay's answer about a game (two snapshots and a read on its side)
GOODBYE_WAIT_S = 6.0          # past the relay's GOODBYE_S: once the link is over, how long its exit code is awaited
ADMINISTRATORS = "S-1-5-32-544"


class RelayFailed(Exception):
    """The relay did not start, or did not answer as it must. `code` is the relay's exit code when it ended with one."""

    def __init__(self, why: str, code: int | None = None) -> None:
        super().__init__(why)
        self.why, self.code = why, code


class GameRefused(Exception):
    """Play: the process named is not one the relay may record (RelayLink.aim). `why` in plain words; `retry`: asking
    again about the same process may change that (a read that failed); `sent`: the relay itself said so (False: the
    recorder's own check said it first, and nothing was sent). The relay waits on for another game."""

    def __init__(self, why: str, retry: bool = False, sent: bool = False) -> None:
        super().__init__(why)
        self.why, self.retry, self.sent = why, retry, sent


def relay_python() -> Path:
    """The interpreter that runs the relay: pythonw.exe beside the base interpreter, so no console window opens and
    the launched process is the relay itself (a venv's pythonw.exe is a launcher that starts another process)."""
    return Path(sys.base_exec_prefix) / "pythonw.exe"


def relay_argv(pipe_name: str, recorder: tuple[int, int], sid: str, game: tuple[int, int] | None = None, *,
               presentmon_exe: Path | None, frames: bool, network: bool, folders: tuple[str, ...] | None = None,
               wait_s: float | None = None, python: Path | None = None) -> list[str]:
    """The relay's command line: what the UAC prompt approves. -B: it writes no bytecode (no file at all); -E -s: no
    PYTHON* variables and no user site-packages. Every path absolute (launch_elevated refuses a relative one). The
    game is the running one (`game`), or for Play the forms of its install folder (`folders`, target.folder_forms'
    tuple, written as target.check_forms gives them, so the prompt shows what the relay will use) with how long the
    recorder will wait for the store to start it (`wait_s`, at most target.MAX_WAIT_S)."""
    python = python if python is not None else relay_python()
    argv = [str(python), "-B", "-E", "-s", "-m", "lagdoctor.relay", "--pipe", pipe_name,
            "--recorder", f"{int(recorder[0])}:{int(recorder[1])}", "--sid", sid]
    if (game is None) == (folders is None):
        raise ValueError("the relay records the running game or one from its install folder: name one of the two")
    if game is not None:
        argv += ["--game", f"{int(game[0])}:{int(game[1])}"]
    else:
        from lagdoctor.capture.target import check_forms
        for form in check_forms(folders):
            argv += ["--folder", form]
        number = isinstance(wait_s, (int, float)) and not isinstance(wait_s, bool)
        argv += ["--wait-s", f"{wait_s:g}" if number else str(wait_s)]
    if frames:
        if presentmon_exe is None or not Path(presentmon_exe).is_absolute():
            raise ValueError("frame timing through the relay needs PresentMon's absolute path")
        argv += ["--frames", "--presentmon-exe", str(presentmon_exe)]
    if network:
        argv.append("--network")
    from lagdoctor.relay.server import parse_args
    parse_args(argv[6:])                          # what the relay would refuse is refused here, before any prompt
    return argv


def _exit_code(child, wait_s: float = 0.0) -> int | None:
    try:
        return child.wait(wait_s)
    except (OSError, ValueError):               # ValueError: its handle was closed already
        return None


def launch(target: dict[str, Any] | None = None, *, presentmon_exe: Path | None, frames: bool, network: bool,
           recorder: tuple[int, int], sid: str, launcher: Callable[..., Any] | None = None,
           owner: str | None = None, cwd: Path | None = None, python: Path | None = None,
           folders: tuple[str, ...] | None = None, wait_s: float | None = None) -> "RelayLink":
    """Start the relay for this recording (one UAC prompt), connect and check it: for the running game `target`, or for
    Play the forms of its install folder (`folders`, target.folder_forms' tuple) before the game exists, with how long
    the recorder will wait for the store to start it (`wait_s`); RelayLink.aim then names the game. PermissionError
    when the prompt is declined; ValueError for a command it refused to build; RelayFailed when the relay ended or
    answered wrongly. `owner`: the pipe owner to require (default ADMINISTRATORS); `cwd`: where it runs (default
    PROJECT_ROOT)."""
    if launcher is None:
        from lagdoctor.winapi.elevation import launch_elevated as launcher
    owner = ADMINISTRATORS if owner is None else owner
    cwd = PROJECT_ROOT if cwd is None else cwd
    name = protocol.pipe_name()
    game = None if target is None else (int(target["pid"]), int(target["create_time"]))
    if folders is not None:
        from lagdoctor.capture.target import check_forms
        folders = check_forms(folders)              # the forms the relay will use and its hello will name
    argv = relay_argv(name, recorder, sid, game, presentmon_exe=presentmon_exe, frames=frames, network=network,
                      folders=folders, wait_s=wait_s, python=python)
    child = launcher(argv, cwd=str(cwd))
    client = None
    try:
        client = _connect(name, child)
        if client.server_pid() != child.pid:
            raise RelayFailed(f"another program answered on the relay's pipe (process {client.server_pid()}, not the "
                              f"relay's {child.pid}), so nothing was recorded through it")
        if client.owner_sid() != owner:
            raise RelayFailed("the relay's pipe is not owned by Administrators, so it was not made with administrator "
                              "rights; nothing was recorded through it")
        hello = protocol.from_relay(client.read(HELLO_S))
        if (hello["t"] != "hello" or hello["v"] != protocol.VERSION or hello["pid"] != child.pid
                or hello["game"] != game or hello["frames"] != frames or hello["network"] != network):
            raise RelayFailed("the relay did not greet the recording window as expected")
        if hello["folders"] != folders:
            raise RelayFailed("the relay did not greet the recording window with the folder it was approved for")
        aim_by = time.monotonic() + wait_s + _game_slack() - AIM_S if folders is not None else None
        since, session = _relay_and_session(child.pid, recorder[0])
        if folders is not None and (since is None or session is None):
            raise RelayFailed("when the relay started could not be read here, so no game it might record could be "
                              "checked; nothing was recorded through it")
        link = RelayLink(client, child, hello, create_time=since, recorder=recorder, session=session, aim_by=aim_by)
        client = child = None
        return link
    except (TimeoutError, protocol.ProtocolError) as exc:
        raise RelayFailed(f"the relay did not answer as it must ({exc})", _exit_code(child)) from None
    except OSError as exc:
        raise RelayFailed(f"the connection to the relay failed ({exc})", _exit_code(child)) from None
    finally:
        if client is not None:
            client.close()
        if child is not None:
            child.close()


def _game_slack() -> float:
    """The relay's own GAME_SLACK_S, looked up when called: how long past the recorder's wait it waits for a game."""
    from lagdoctor.relay.server import GAME_SLACK_S
    return GAME_SLACK_S


def _snapshot_now() -> dict:
    from lagdoctor.winapi.ntquery import Snapshotter
    return Snapshotter().snapshot()


def _image_path(pid: int) -> str | None:
    from lagdoctor.winapi import ntquery
    return ntquery.image_path(pid)


def _relay_and_session(relay_pid: int, recorder_pid: int) -> tuple[int | None, int | None]:
    """The relay's create time (Play's `since`: its game must have started after it) and the recorder's Windows
    session, from one snapshot of this process's own; None for what it could not read."""
    try:
        procs = _snapshot_now()
    except OSError:
        return None, None
    relay, me = procs.get(relay_pid), procs.get(recorder_pid)
    return (relay.create_time if relay is not None else None), (me.session_id if me is not None else None)


def _connect(name: str, child):
    """The relay's pipe, once it is up; RelayFailed with its exit code when the relay ended first."""
    from lagdoctor.winapi import pipe
    deadline = time.monotonic() + CONNECT_S
    while True:
        code = _exit_code(child)
        if code is not None:
            raise RelayFailed(f"the relay ended before it answered (exit code {code})", code)
        try:
            return pipe.connect(name, min(0.5, max(0.05, deadline - time.monotonic())),
                                max_message=protocol.MAX_MESSAGE)
        except TimeoutError:
            if time.monotonic() >= deadline:
                raise RelayFailed(f"the relay did not answer within {CONNECT_S:.0f} s") from None


_UNREAD = object()


class RelayLink:
    """One relay, connected and checked: its messages go to the collectors made through it. `create_time`: the
    relay's, as launch read it (default: read now); for Play also `recorder` (pid, create_time), its Windows
    `session` and `aim_by` (time.monotonic), past which the relay no longer takes a game."""

    def __init__(self, client, child, hello: dict[str, Any], *, create_time: Any = _UNREAD,
                 recorder: tuple[int, int] | None = None, session: int | None = None,
                 aim_by: float | None = None) -> None:
        self.pid: int = hello["pid"]
        self.frames: bool = hello["frames"]
        self.network: bool = hello["network"]
        self.folders: tuple[str, ...] | None = hello.get("folders")     # Play: the forms the prompt approved
        # The game it records: the running one the prompt approved, or Play's, once the relay accepted it (aim).
        self.game: tuple[int, int] | None = hello.get("game")
        self.recorder, self.session, self.aim_by = recorder, session, aim_by
        self._aiming: tuple[int, int] | None = None         # Play: the game named, until the relay answers
        self._reply: dict[str, Any] | None = None
        self._replied = threading.Event()
        self._client, self._child = client, child
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._started = self._stop_sent = self._closed = False
        self._frames_sink: RelayedFrames | None = None
        self._net_sink: RelayedEtwSession | None = None
        self._dispatch_lock = threading.Lock()  # a sink made while messages arrive gets every one, in order
        self._early: list[dict[str, Any]] = []  # network messages from before the tracker's session existed
        self._early_events = self._early_dropped = 0
        self.ended = threading.Event()          # `end` came, or the link broke
        self.why: str | None = None             # why it broke; None after a clean end
        self.relay_error: str | None = None     # what the relay said when it stopped on a problem
        self.create_time = _create_time(self.pid) if create_time is _UNREAD else create_time
        self._reader = threading.Thread(target=self._read_loop, name="lagdoctor-relay", daemon=True)
        self._reader.start()

    # ---- for the session ------------------------------------------------------------------------------
    def frames_collector(self, pid: int) -> "RelayedFrames":
        self._frames_sink = RelayedFrames(self, pid)
        if self.folders is not None and (self.game is None or pid != self.game[0]):
            self._give_up(f"the recording's game (process {pid}) is not the one the relay accepted, so nothing was "
                          "recorded through it")
        self._tell_if_over(self._frames_sink)
        return self._frames_sink

    def etw_session(self, on_event: Callable[[Any], None], origin: float,
                    stopping: Callable[[], bool] | None = None) -> "RelayedEtwSession":
        """FlowTracker's session. It makes it only when it starts, after the frames may have started the relay: the
        network messages that came before are handed to it first, in order. `stopping`: whether the tracker itself is
        stopping, so a tracker that ends on an error never stops the relay's frame timing with it."""
        sink = RelayedEtwSession(self, on_event, origin, stopping)
        with self._dispatch_lock:
            early, self._early = self._early, []
            sink.early_dropped = self._early_dropped
            for msg in early:
                self._deliver(sink, msg)
            self._net_sink = sink
        self._tell_if_over(sink)
        return sink

    def _tell_if_over(self, sink) -> None:
        """A collector made after the link ended (the tracker makes its session only when it starts) learns why at
        once, instead of waiting out its own deadline."""
        if self.ended.is_set():
            sink.relay_gone(self.why or (f"the relay ended: {self.relay_error}" if self.relay_error else
                                         "the relay ended"))

    def tool_procs(self) -> tuple[tuple[int, int], ...]:
        """The relay as (pid, create_time): lag-doctor's own, with its PresentMon, never blamed for the lag."""
        return ((self.pid, self.create_time),) if self.create_time is not None else ()

    # ---- commands ---------------------------------------------------------------------------------------
    def aim(self, game: tuple[int, int], *, snapshot: Callable[[], dict] | None = None,
            image: Callable[[int], str | None] | None = None) -> None:
        """Play: name the game the folder wait found, (pid, create_time). The recorder's own check comes first
        (target.check_proposed with the arguments the relay will use, on `snapshot` and `image`: this process's own
        by default), so a game the relay would refuse is never sent; then the relay judges it on its own snapshots.
        Returns once the relay has accepted it: the recording may start. GameRefused when either says no (the relay
        waits on for another); RelayFailed when the link ended, the relay did not answer within AIM_S or its wait
        for a game is over (the link is closed then, and the relay's exit code is in it); ValueError for a relay
        approved for the running game itself; RuntimeError while another aim is under way. Each aim takes the place
        of the one before, as on the relay's side, until the recording starts."""
        from lagdoctor.capture.target import check_proposed
        proposed = (int(game[0]), int(game[1]))
        with self._state_lock:
            if self.folders is None:
                raise ValueError("the relay was approved for the running game itself, so it takes no other")
            if self._aiming is not None:
                raise RuntimeError("another game is being named to the relay")
            over = self._started or self._stop_sent or self.ended.is_set()
        if over or (self.aim_by is not None and time.monotonic() > self.aim_by):
            raise RelayFailed(self.why or "the relay no longer takes a game for this recording")
        why, retry = check_proposed(snapshot or _snapshot_now, image or _image_path, *proposed, session=self.session,
                                    folders=self.folders, since=self.create_time,
                                    own=(self.recorder, (self.pid, self.create_time)))
        if why is not None:
            raise GameRefused(why, retry, sent=False)
        with self._state_lock:
            if self._aiming is not None or self._started or self._stop_sent:
                raise RuntimeError("the relay's game changed hands while it was being named")
            self._aiming, self._reply = proposed, None
            self._replied.clear()
            self.game = None                        # the relay forgets the one before as this one arrives
        self._send(protocol.game(proposed))
        deadline = time.monotonic() + AIM_S
        while not self._replied.wait(0.05):
            if self.ended.is_set() or time.monotonic() >= deadline:
                break
        with self._state_lock:
            reply, self._aiming = self._reply, None
            if reply is not None and reply["t"] == "game":
                self.game = proposed
        if reply is None:
            self._broke(f"the relay did not answer about the game within {AIM_S:.0f} s")
            code = self.close(wait_s=GOODBYE_WAIT_S)
            raise RelayFailed(self.why or "the relay ended", code)
        if reply["t"] == "refused":
            raise GameRefused(reply["why"], reply["retry"], sent=True)

    def _take_reply(self, msg: dict[str, Any]) -> bool:
        """The relay's answer about a game (`game` or `refused`), on the reader's thread: kept for aim when it names
        the game being named, once; False for any other (the link then breaks)."""
        with self._state_lock:
            if self._aiming is None or msg["game"] != self._aiming or self._reply is not None:
                return False
            self._reply = msg
        self._replied.set()
        return True

    def start(self, origin: float) -> None:
        """Start the relay's recording on the capture clock's origin; the first call does it. For Play only on the
        game the relay accepted (aim): with none, the relay is told to stop instead and the link ends with the reason,
        so nothing is recorded through it."""
        with self._state_lock:
            if self._started or self._stop_sent:
                return
            self._started = True
            unaimed = self.folders is not None and (self.game is None or self._aiming is not None)
        if unaimed:
            self._give_up("the recording started before the relay had accepted its game, so nothing was recorded "
                          "through it")
            return
        self._send(protocol.start(origin))

    def _give_up(self, why: str) -> None:
        """End the link with `why`, and tell the relay to stop, so it ends at once instead of waiting out its deadline
        (the reason first: the relay's `end` to that stop must not read as a clean end)."""
        with self._state_lock:
            stop, self._stop_sent = not self._stop_sent, True
        self._broke(why)
        if stop:
            with self._write_lock:
                try:
                    self._client.write(protocol.stop(), 5.0)
                except (OSError, ValueError):
                    pass                            # it is gone already: it ends by itself

    def stop(self) -> None:
        """Ask the relay to stop and send what is left; the first call does it. Said first to the flow tracker (as the
        console's close handler does): the relay's network trace then ends on lag-doctor's own request, which its
        consumer's end must not read as stopping by itself."""
        with self._state_lock:
            if self._stop_sent:
                return
            self._stop_sent = True
        from lagdoctor.capture import netflows
        netflows.expect_external_stop()
        self._send(protocol.stop())

    def _send(self, data: bytes) -> None:
        if self.ended.is_set():
            return
        with self._write_lock:
            try:
                self._client.write(data, 5.0)
            except (OSError, ValueError) as exc:
                self._broke(f"the recording window could not reach the relay ({exc})")

    def close(self, wait_s: float = 0.0) -> int | None:
        """Hang up and close the relay's handle; its exit code if it has ended within `wait_s` (0: never waited for,
        so a window that is closing saves at once: the relay ends by itself once it sees the hang-up). The reader is
        let finish first: it may be reading the relay's exit code through that handle."""
        with self._state_lock:
            if self._closed:
                return None
            self._closed = True
        self._client.close()
        if self._reader is not threading.current_thread():
            self._reader.join(3.0)
        code = _exit_code(self._child, wait_s)
        self._child.close()
        return code

    # ---- the reader -------------------------------------------------------------------------------------
    def _read_loop(self) -> None:
        while True:
            try:
                data = self._client.read(1.0)
            except TimeoutError:
                continue
            except OSError:
                if self._closed:
                    self._broke("the recording window hung up")
                    return
                code = _exit_code(self._child, 1.0)
                self._broke("the relay ended" + (f" (exit code {code})" if code is not None else ""))
                return
            try:
                msg = protocol.from_relay(data)
                if msg["t"] == "hello":
                    raise protocol.ProtocolError("a second hello")
                if msg["t"] in ("game", "refused") and not self._take_reply(msg):
                    raise protocol.ProtocolError("an answer about a game the recording window did not name")
            except protocol.ProtocolError as exc:
                self._broke(f"the relay sent a message the recording window cannot use ({exc})")
                return
            try:
                with self._dispatch_lock:
                    self._dispatch(msg)
            except Exception as exc:  # noqa: BLE001 - a sink's bug must not leave the link reading forever
                self._broke(f"a message from the relay could not be used ({type(exc).__name__}: {exc})")
                return
            if msg["t"] == "end":
                self._ended_cleanly()
                return

    def _dispatch(self, msg: dict[str, Any]) -> None:
        t = msg["t"]
        if t == "rows" and self._frames_sink is not None:
            self._frames_sink.feed(msg["rows"])
        elif t == "frames" and self._frames_sink is not None:
            self._frames_sink.relay_status(msg)
        elif t in ("events", "net") and self._net_sink is not None:
            self._deliver(self._net_sink, msg)
        elif t in ("events", "net") and self.network:
            n = len(msg["ev"]) if t == "events" else 0
            if self._early_events + n > MAX_EARLY_EVENTS:
                self._early_dropped += n
            else:
                self._early_events += n
                self._early.append(msg)
        elif t == "error":
            self.relay_error = msg["why"]

    @staticmethod
    def _deliver(sink: "RelayedEtwSession", msg: dict[str, Any]) -> None:
        if msg["t"] == "events":
            sink.feed(msg["ev"])
        else:
            sink.relay_status(msg)

    def _ended_cleanly(self) -> None:
        """`end` came: a collector still waiting for its final status gets the relay's reason (it stopped before the
        recording began, or on a problem it named); one that has its final keeps it."""
        self.ended.set()
        # A link this window ended first (_give_up: it told the relay to stop) keeps its own reason.
        why = self.why or (f"the relay ended: {self.relay_error}" if self.relay_error else "the relay ended")
        for sink in (self._frames_sink, self._net_sink):
            if sink is not None:
                sink.relay_gone(why)

    def _broke(self, why: str) -> None:
        if self.ended.is_set():
            return
        self.why = why if self.relay_error is None else f"{why}: {self.relay_error}"
        self.ended.set()
        for sink in (self._frames_sink, self._net_sink):
            if sink is not None:
                sink.relay_gone(self.why)


def _create_time(pid: int) -> int | None:
    try:
        from lagdoctor.winapi.ntquery import Snapshotter
        p = Snapshotter().snapshot().get(pid)
    except OSError:
        return None
    return p.create_time if p is not None else None


class RelayedFrames(PresentMonSource):
    """Collector "frames" for a relayed recording: PresentMonSource's storage, live frame rate and result, fed the
    game's rows from the relay; its status is PresentMon's as the relay reports it."""

    def __init__(self, link: RelayLink, pid: int) -> None:
        super().__init__(pid)
        self._link = link
        self._final = threading.Event()
        self._relay_final: dict[str, Any] | None = None
        self._gone: str | None = None
        self._refused = 0                       # rows of another process, or that could not be stored

    def run(self) -> None:
        self.set_status("ok", "starting PresentMon in the relay", tool=TOOL)
        self._link.start(self.origin)
        stop_by = None
        while not self._final.wait(0.25):
            if self._gone is not None:
                break
            if self.stopping:
                stop_by = stop_by if stop_by is not None else clock.now() + STOP_FRAMES_S
                if clock.now() >= stop_by:
                    break
        self._publish(force=True)
        self._settle_relayed()

    def on_stop(self) -> None:
        self._link.stop()

    def feed(self, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            if row["ProcessID"] != self.pid:
                self._refused += 1
                continue
            try:
                self._add(row)
            except Exception:  # noqa: BLE001 - one row must not stop the rest
                self._refused += 1

    def relay_status(self, msg: dict[str, Any]) -> None:
        if self._final.is_set():
            return
        if msg["final"]:
            self._relay_final = msg
            self._final.set()
        else:
            self.set_status(msg["state"], msg["detail"], msg["tool"] or TOOL)

    def relay_gone(self, why: str) -> None:
        self._gone = why

    def _settle_relayed(self) -> None:
        final = self._relay_final
        note = f"; {self._refused} row(s) from the relay could not be used" if self._refused else ""
        if final is not None:
            state = final["state"] if not (note and final["state"] == "ok") else "partial"
            self.set_status(state, final["detail"] + note, final["tool"] or TOOL)
            return
        with self._lock:
            rows, last = self._rows, self._last_frame_t
        why = self._gone or f"the relay did not say how frame timing ended within {STOP_FRAMES_S:.0f} s"
        if rows:
            self.set_status("partial", f"{rows} frames, then frame timing stopped after t={last:.1f} s: {why}{note}",
                            tool=TOOL)
        else:
            self.set_status("failed", f"no frames came through the relay: {why}{note}", tool=TOOL)

    def result(self) -> dict[str, Any]:
        out = super().result()
        final = self._relay_final
        info = dict(final["info"]) if final is not None and final["info"] else dict(out["presentmon"])
        info["relay"] = {"pid": self._link.pid, "rows_received": out["presentmon"]["rows"], "rows_refused": self._refused}
        out["presentmon"] = info
        return out


class RelayedEtwSession:
    """What FlowTracker uses as its ETW session when a relay runs it: start, stop, health, the events (on the link's
    thread, as a real session's consumer thread would deliver them) and the per-program totals of this session."""

    talker_scope = "session"

    def __init__(self, link: RelayLink, on_event: Callable[[Any], None], origin: float,
                 stopping: Callable[[], bool] | None = None) -> None:
        self._link, self._on_event, self._origin, self._stopping = link, on_event, origin, stopping
        self.early_dropped = 0                  # network events that came before it existed and could not be kept
        self._lock = threading.Lock()
        self._status = threading.Event()        # the relay said whether its session started
        self._final = threading.Event()
        self._started: bool | None = None
        self._why = ""
        self._health: dict[str, Any] = {}
        self._totals: list[tuple[int, int, int]] = []
        self._gone: str | None = None
        self._events = 0

    def start(self) -> None:
        self._link.start(self._origin)
        deadline = time.monotonic() + NET_START_S
        while not self._status.wait(0.1):
            if self._gone is not None:
                raise OSError(f"the relay ended before its network trace started ({self._gone})")
            if time.monotonic() >= deadline:
                raise OSError(f"the relay did not say within {NET_START_S:.0f} s whether its network trace started")
        if not self._started:
            raise OSError(self._why or "the relay could not start its network trace")

    def stop(self) -> None:
        if self._stopping is not None and not self._stopping():
            return                              # the tracker ended on an error: frame timing goes on in the relay
        self._link.stop()
        deadline = time.monotonic() + STOP_NET_S
        while not self._final.wait(0.05):
            if self._gone is not None or time.monotonic() >= deadline:
                return

    def health(self) -> dict[str, Any]:
        with self._lock:
            h = dict(self._health)
            h["relayed_events"] = self._events
            h["early_dropped"] = self.early_dropped
            if self._gone is not None and not self._final.is_set():
                h["pump_alive"] = False
                h["stopped_why"] = f"the relay stopped ({self._gone})"
        return h

    def talker_totals(self) -> list[tuple[int, int, int]]:
        with self._lock:
            return list(self._totals)

    def layout_mismatch(self) -> dict[str, int]:
        with self._lock:
            bad = (self._health.get("relay") or {}).get("layout_mismatch") or {}
        return {str(k): v for k, v in bad.items() if isinstance(v, int) and not isinstance(v, bool)}

    # ---- from the link's thread ---------------------------------------------------------------------------
    def feed(self, events: list[tuple[int, int, int, bytes]]) -> None:
        from lagdoctor.capture.netflows import KERNEL_NETWORK
        from lagdoctor.winapi.etw import EtwEvent
        for ts, eid, ver, data in events:
            with self._lock:
                self._events += 1
            self._on_event(EtwEvent(ts, KERNEL_NETWORK, eid, ver, 0, 0, 0, 0, 0, 0, data))

    def relay_status(self, msg: dict[str, Any]) -> None:
        with self._lock:
            self._health = dict(msg["health"])
            self._totals = list(msg["totals"])
            if msg["started"] is not None:
                self._started, self._why = msg["started"], msg["why"]
        if msg["started"] is not None:
            self._status.set()
        if msg["final"]:
            self._final.set()

    def relay_gone(self, why: str) -> None:
        with self._lock:
            self._gone = why
