"""The relay process: PresentMon and the Kernel-Network session with administrator rights, for one recorder.

Run as `pythonw -B -E -s -m lagdoctor.relay ...` through one UAC prompt (relay/client.launch), with no console
window. Everything it may do comes from that command line, which the prompt approved: the pipe's random name, the
recorder (pid, create time, account SID), the game (a running one by pid and create time; or for Play the forms of its
install folder, with how long the recorder will wait for the store to start it), and whether to run frame timing
(with the PresentMon file the recorder verified) and the network tracker. The pipe brings only the capture clock's
origin and, for Play, the processes the recorder found, which the relay judges for itself. In order:

1. Its own handle-free snapshot must show the recorder running as exactly that process in the relay's own Windows
   session, and with --game the game too (target.accept_target, which the recorder called before the prompt).
2. PresentMon must pass its check again here (setup.verify).
3. It creates the pipe: first instance only, remote clients refused, a DACL for the recorder's account, a medium
   label, and Administrators as its owner, which only an elevated token may set (the recorder checks it).
4. Only the recorder may stay connected: its pid and session are checked; anyone else is let go.
5. For Play, each process the recorder names (`game`, up to protocol.MAX_GAMES) is judged by the folder rule on the
   relay's own snapshots and its own read of the program's path (target.check_proposed): started after the relay, in
   its session, a program under one of the forms, none of lag-doctor's own, and the recorder still running. It
   answers `game` (that one is recorded) or `refused` with why, and waits on; it gives up when none is accepted by the
   recorder's wait plus GAME_SLACK_S, and whenever the recorder is gone.
6. On `start` it runs PresentMon on the game (rows forwarded, not kept: PresentMonSource's `forward`) and the
   Kernel-Network session. It forwards the game's frame rows, the events of the game's processes (the game and what
   it started, from its own snapshot once a second) and of the recorder (its DNS self-test), whole; for every other
   program it counts bytes received and sent and passes on the totals only for programs in the recorder's session
   (DECISIONS 2026-11-08). Events of other programs never leave it.
7. On `stop`, or when the recorder hangs up, it stops both, sends what is left and the final statuses, then `end`.
   In the capture service it does so too when its caller's hold ends (Relay's `until`), and says why before `end`.

It writes no file and prints nothing (pythonw has nowhere to print): before the pipe is up its exit code says what
went wrong (protocol.EXIT_*), after that the pipe does.
"""
from __future__ import annotations

import argparse
import collections
import os
import queue
import re
import struct
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from lagdoctor.relay import protocol

CLIENT_WAIT_S = 30.0          # the recorder connects as soon as the prompt's launch returns
START_WAIT_S = 300.0          # the recorder reads the PC's details before its collectors start (Play: after the game)
# Play: how long past the recorder's own wait (--wait-s) the relay waits for a game it accepts: one round of the wait
# past its deadline, the re-arm's hold (target.REARM_HOLD_S) and the recorder's own check of the game (client.AIM_S).
# A test holds it above their sum and below GAME_SLACK_MAX_S: the relay's approval of the folder lasts no longer.
GAME_SLACK_S = 60.0
GAME_SLACK_MAX_S = 120.0
TICK_S = 0.1                  # rows and events go out this often
STATUS_EVERY_S = 1.0          # the game's processes, sessions, totals and statuses this often
WRITE_TIMEOUT_S = 10.0        # a recorder that reads nothing for this long is taken for gone
FRAMES_STOP_S = 25.0          # PresentMon's worst stop path (DESIGN.md, Collector contract)
GOODBYE_S = 5.0               # after `end`, how long the recorder has to hang up
MAX_QUEUED_ROWS = 100_000     # rows waiting for the pipe; past this they are counted as lost
MAX_QUEUED_EVENTS = 50_000
MAX_PROGRAMS = 4096           # programs whose session is remembered
OWNER = "O:BA"                # Administrators: Windows refuses that owner to an unelevated token (error 1307)
# What the capture service's caller is told when its hold ends (Relay's `until`, protocol.SERVICE_HOLD_S from its ask).
HELD = (f"the capture service records for at most {protocol.hold_text()} at a time, so it ended this "
        "recording")
_PID_SIZE = struct.Struct("<II")   # NE 4.3: the payload's PID and size
_SID = re.compile(r"S-1-[0-9]{1,13}(?:-[0-9]{1,10}){1,15}")
# PresentMon's path: a plain local drive path, never UNC or a device. Opening a network path as an administrator would
# hand that share the administrator's credentials, even if the file then failed its check.
_LOCAL_PATH = re.compile(r"[A-Za-z]:\\(?![\\/])[^\x00-\x1f]{1,255}")


@dataclass(frozen=True)
class Config:
    """What the UAC prompt approved (parse_args)."""
    pipe: str
    recorder: tuple[int, int]               # (pid, create_time)
    sid: str                                # the recorder's account: the only one the pipe lets in
    game: tuple[int, int] | None            # the running game (--game, M3); None for Play
    frames: bool
    network: bool
    presentmon_exe: Path | None
    folders: tuple[str, ...] | None = None  # Play: the forms of the game's install folder (--folder)
    wait_s: float | None = None             # Play: how long the recorder waits for the store to start the game


class _Parser(argparse.ArgumentParser):
    """argparse that raises instead of printing: under pythonw there is nowhere to print."""

    def error(self, message: str):  # noqa: D401 - argparse's hook
        raise ValueError(message)

    def exit(self, status: int = 0, message: str | None = None):
        raise ValueError(message or f"exit {status}")


def _identity(text: str) -> tuple[int, int]:
    m = re.fullmatch(r"([0-9]{1,10}):([0-9]{1,20})", text)
    if not m or not 0 < int(m.group(1)) <= protocol.MAX_PID or int(m.group(2)) > protocol.MAX_QPC:
        raise ValueError(f"not pid:create_time: {text!r}")
    return int(m.group(1)), int(m.group(2))


def _seconds(text: str) -> float:
    """Play's --wait-s: a number of seconds the folder wait may take (more than 0, at most target.MAX_WAIT_S)."""
    from lagdoctor.capture.target import MAX_WAIT_S
    if not re.fullmatch(r"[0-9]{1,4}(?:\.[0-9]{1,3})?", text) or not 0 < float(text) <= MAX_WAIT_S:
        raise ValueError(f"not a wait of more than 0 and at most {MAX_WAIT_S:g} seconds: {text!r}")
    return float(text)


def parse_args(argv: list[str]) -> Config:
    """The relay's command line, as relay/client.relay_argv builds it; ValueError for anything else. The game is
    either the running one (--game) or, for Play, the forms of its install folder (--folder, once per form, checked
    by target.check_forms and never resolved here) with how long the recorder waits for it (--wait-s)."""
    p = _Parser(prog="lagdoctor.relay", add_help=False, allow_abbrev=False)
    p.add_argument("--pipe", required=True)
    p.add_argument("--recorder", required=True, type=_identity)
    p.add_argument("--sid", required=True)
    p.add_argument("--game", type=_identity)
    p.add_argument("--folder", action="append")
    p.add_argument("--wait-s", type=_seconds)
    p.add_argument("--frames", action="store_true")
    p.add_argument("--presentmon-exe")
    p.add_argument("--network", action="store_true")
    a = p.parse_args(argv)
    if not protocol.is_pipe_name(a.pipe):
        raise ValueError("not a relay pipe name")
    if not _SID.fullmatch(a.sid):
        raise ValueError("not a SID")
    if (a.game is None) == (a.folder is None):
        raise ValueError("name the running game (--game) or its install folder (--folder), one of the two")
    folders = None
    if a.folder is not None:
        from lagdoctor.capture.target import check_forms
        folders = check_forms(a.folder)
        if a.wait_s is None:
            raise ValueError("--folder needs --wait-s")
    elif a.wait_s is not None:
        raise ValueError("--wait-s goes with --folder")
    if not (a.frames or a.network):
        raise ValueError("nothing to run: neither --frames nor --network")
    exe = None
    if a.frames:
        if not a.presentmon_exe or not _LOCAL_PATH.fullmatch(a.presentmon_exe):
            raise ValueError("--frames needs --presentmon-exe as a plain local drive path")
        exe = Path(a.presentmon_exe)
    elif a.presentmon_exe is not None:
        raise ValueError("--presentmon-exe without --frames")
    if a.game is not None and a.recorder[0] == a.game[0]:
        raise ValueError("the recorder is not the game")
    return Config(a.pipe, a.recorder, a.sid, a.game, a.frames, a.network, exe, folders, a.wait_s)


# ------------------------------------------------------------------------------------------ the parts ---

def _snapshot_fn() -> Callable[[], dict]:
    from lagdoctor.winapi.ntquery import Snapshotter
    snap = Snapshotter()
    lock = threading.Lock()

    def snapshot() -> dict:
        with lock:
            return snap.snapshot()
    return snapshot


def _verify(exe: Path) -> None:
    """PresentMon's check, on a local drive only (the path's form was checked already; a drive letter can still be
    mapped to a share): never open a network path with administrator rights."""
    from lagdoctor import setup
    from lagdoctor.winapi import storage
    if storage.drive_type(str(exe)[:3]) not in (storage.DRIVE_FIXED, storage.DRIVE_REMOVABLE):
        raise OSError(f"{exe} is not on a local drive")
    setup.verify(exe)


def _frames_source(cfg: Config, forward: Callable[[dict[str, Any]], None], alive: Callable[[], bool]):
    from lagdoctor.capture.presentmon import PresentMonSource
    return PresentMonSource(cfg.game[0], exe=cfg.presentmon_exe, is_alive=alive, forward=forward)


def _etw_session(on_event: Callable[[Any], None]):
    from lagdoctor.capture import netflows
    from lagdoctor.winapi import etw
    providers = [(netflows.KERNEL_NETWORK, netflows.LEVEL_INFORMATIONAL, netflows.KW_IPV4 | netflows.KW_IPV6)]
    return etw.RealtimeSession(netflows.SESSION_NAME, providers, on_event)


def _image_path(pid: int) -> str | None:
    from lagdoctor.winapi import ntquery
    return ntquery.image_path(pid)


@dataclass
class Parts:
    """What the relay runs, made replaceable for tests; the defaults are the real ones."""
    snapshot: Callable[[], dict] | None = None              # pid -> ntquery.ProcInfo
    verify: Callable[[Path], Any] = _verify
    frames: Callable[..., Any] = _frames_source             # (cfg, forward, alive) -> a frames Collector
    etw: Callable[..., Any] = _etw_session                  # (on_event) -> an ETW session
    me: int = field(default_factory=os.getpid)
    image: Callable[[int], str | None] = _image_path        # Play: a process's program path, read without a handle


# ---------------------------------------------------------------------------------------- the process ---

def main(argv: list[str] | None = None, *, owner: str | None = None, parts: Parts | None = None) -> int:
    """Run the relay; its exit code (protocol.EXIT_*). `owner`: the pipe's owner in SDDL (default OWNER)."""
    owner = OWNER if owner is None else owner
    try:
        cfg = parse_args(sys.argv[1:] if argv is None else argv)
    except ValueError:
        return protocol.EXIT_BAD_ARGS
    parts = parts or Parts()
    if parts.snapshot is None:
        parts.snapshot = _snapshot_fn()
    from lagdoctor.capture import target
    try:
        procs = parts.snapshot()
    except OSError:
        return protocol.EXIT_TARGET               # it cannot see the processes it must check: record nothing
    me = procs.get(parts.me)
    rec = procs.get(cfg.recorder[0])
    if me is None or rec is None or rec.create_time != cfg.recorder[1] or rec.session_id != me.session_id:
        return protocol.EXIT_TARGET
    if cfg.game is not None:                      # Play's game does not exist yet: it is judged when it is named
        if target.accept_target(procs, *cfg.game, session=me.session_id, approved=cfg.game) is not None:
            return protocol.EXIT_TARGET
    if cfg.frames:
        try:
            parts.verify(cfg.presentmon_exe)
        except Exception:  # noqa: BLE001 - SetupError, or anything that stops the check: never run it unchecked
            return protocol.EXIT_NOT_READY
    from lagdoctor.winapi import pipe
    try:
        server = pipe.PipeServer(cfg.pipe, owner + pipe.client_dacl([cfg.sid]), max_message=protocol.MAX_MESSAGE)
    except OSError:
        return protocol.EXIT_PIPE
    with server:
        if not wait_for_recorder(server, cfg, me.session_id):
            return protocol.EXIT_NO_CLIENT
        relay = Relay(cfg, server, me.session_id, parts, since=me.create_time)
        try:
            return relay.serve()
        except Exception as exc:  # noqa: BLE001 - say why on the pipe if it is still there; the code says the rest
            relay.fail(f"the relay stopped on an error it did not expect ({type(exc).__name__}: {exc})")
            return protocol.EXIT_CRASH


def wait_for_recorder(server, cfg: Config, session: int, wait_s: float | None = None) -> bool:
    """True once the recorder itself is connected: its pid and its session, read from the pipe (no handle to it).
    Anyone else that connects first is let go. False when the recorder has not come by the deadline (CLIENT_WAIT_S,
    read when called)."""
    deadline = time.monotonic() + (CLIENT_WAIT_S if wait_s is None else wait_s)
    while True:
        left = deadline - time.monotonic()
        if left <= 0 or not server.wait_client(left):
            return False
        try:
            if server.client_pid() == cfg.recorder[0] and server.client_session() == session:
                return True
        except OSError:
            pass
        server.disconnect()


class Relay:
    """One recording: started by `start`, ended by `stop` or by the recorder hanging up. `session`: the recorder's
    Windows session, where the game must run. `since`: after which Play's game must have started (the relay's own
    create time; the capture service's, when the caller asked). `me`: this process as (pid, create time), one of
    lag-doctor's own that is never a game (default: parts.me started at `since`). `tree`: for the capture service,
    which processes of the game's tree may count as the game's (tree(procs, game, pids) -> pids); the relay, which
    the UAC prompt approved, counts the whole tree in the session. `until`: for the capture service, when its caller's
    hold ends (time.monotonic): every wait before the start ends by then, and the recording ends there (EXIT_HELD,
    said in HELD's words); None, the elevated relay's, has no such end."""

    def __init__(self, cfg: Config, server, session: int, parts: Parts, since: int | None = None,
                 me: tuple[int, int] | None = None,
                 tree: Callable[[dict, tuple[int, int], set[int]], set[int]] | None = None,
                 until: float | None = None) -> None:
        self.cfg, self.server, self.session, self.parts, self.since = cfg, server, session, parts, since
        self.me = me if me is not None else (parts.me, since)
        self.tree = tree
        self.until = until
        self._reader: threading.Thread | None = None
        self._failed = ""                       # why the recording stopped on its own (the refresh could not run)
        # Play's games (at most protocol.MAX_GAMES), then start, then stop: the reader queues nothing else.
        self._commands: queue.Queue = queue.Queue()
        self._took: set[str] = set()                # the commands the reader has queued (start, stop)
        self._games = 0                             # Play's game proposals the reader has queued
        self._gone = threading.Event()          # the recorder hung up, or a write could not reach it
        self._broke = ""                        # the recorder sent what the protocol does not allow
        self._write_lock = threading.Lock()
        self._lock = threading.Lock()           # the queues and counters the source threads fill
        self._rows: collections.deque = collections.deque()
        self._events: collections.deque = collections.deque()
        self._rows_lost = self._events_lost = 0
        self._pending: dict[int, list[int]] = {}
        # The game it records: the one the prompt approved, or for Play the one it last accepted (None until then).
        self.game: tuple[int, int] | None = cfg.game
        self._game: dict[int, int] = {} if cfg.game is None else {cfg.game[0]: cfg.game[1]}   # its processes
        self._game_pids: frozenset[int] = frozenset(self._game)
        self._programs: dict[int, tuple[int, bool]] = {}     # pid -> (create_time, in the recorder's session)
        self._totals: dict[int, list[int]] = {}
        self._counts = {"ignored": 0, "unattributed_bytes": 0, "other_session_bytes": 0, "programs_dropped": 0}
        self._layout_bad: dict[str, int] = {}
        self._refresher: threading.Thread | None = None
        self._refresh_stop = threading.Event()
        self._frames = None
        self._frames_sent: tuple | None = None
        self._frames_final = False
        self._etw = None
        self._net_started: bool | None = None
        self._net_why = ""

    # ---- the pipe -----------------------------------------------------------------------------------
    def _write(self, data: bytes) -> bool:
        if self._gone.is_set():
            return False
        with self._write_lock:
            try:
                self.server.write(data, WRITE_TIMEOUT_S)
                return True
            except (OSError, TimeoutError, ValueError):
                self._gone.set()
                return False

    def _read_loop(self) -> None:
        while not self._gone.is_set():
            try:
                data = self.server.read(1.0)
            except TimeoutError:
                continue
            except OSError:
                self._gone.set()                # hung up, or the pipe was closed under us
                return
            try:
                msg = protocol.from_recorder(data)
            except protocol.ProtocolError as exc:
                self._broke = str(exc)
                self._commands.put({"t": "broken"})
                return
            if "stop" in self._took:
                continue                        # after stop nothing more is taken; reading goes on to see the hang-up
            why = self._out_of_turn(msg["t"])
            if why is not None:
                self._broke = why
                self._commands.put({"t": "broken"})
                return
            if msg["t"] == "game":
                self._games += 1
            else:
                self._took.add(msg["t"])
            self._commands.put(msg)

    def close(self, wait_s: float = 5.0) -> bool:
        """After serve(): stop reading the pipe and wait for the reader to leave, so a server that takes another
        client next never hands that client's messages to this recording. True once it has left (its reads wait at
        most 1 s each)."""
        self._gone.set()
        reader = self._reader
        if reader is not None:
            reader.join(wait_s)
            return not reader.is_alive()
        return True

    def _out_of_turn(self, t: str) -> str | None:
        """Why the recorder may not send this now, or None. `game` only for Play (the prompt approved a folder), only
        before `start`, at most protocol.MAX_GAMES times; `start` and `stop` once each, and for Play `start` only once
        a `game` has come."""
        if t == "game":
            if self.cfg.folders is None:
                return "a game, though the UAC prompt approved the game itself"
            if "start" in self._took:
                return "a game after the start"
            if self._games >= protocol.MAX_GAMES:
                return f"more than {protocol.MAX_GAMES} games"
            return None
        if t in self._took:
            return f"a second {t}"
        if t == "start" and self.cfg.folders is not None and not self._games:
            return "a start before any game"
        return None

    def _command(self, timeout: float) -> dict | None:
        try:
            return self._commands.get(timeout=timeout)
        except queue.Empty:
            return None

    def fail(self, why: str) -> None:
        """Say why on the pipe (if it is still there), then make sure nothing keeps running."""
        self._write(protocol.error(why))
        self._stop_sources()
        self._write(protocol.end())

    # ---- the recording ------------------------------------------------------------------------------
    def serve(self) -> int:
        if not self._write(protocol.hello(self.parts.me, self.cfg.game, self.cfg.folders, self.cfg.frames,
                                          self.cfg.network)):
            return protocol.EXIT_OK
        self._reader = threading.Thread(target=self._read_loop, name="relay-read", daemon=True)
        self._reader.start()
        if self.cfg.folders is None:
            cmd = self._next(time.monotonic() + START_WAIT_S, "the recording did not start in time")
        else:
            cmd = self._play()
        if isinstance(cmd, int):
            if cmd == protocol.EXIT_HELD:
                self._goodbye()                       # the caller reads why before the service lets it go
            return cmd
        if cmd["t"] == "broken":
            self.fail(f"the recording window sent a message the relay does not take ({self._broke})")
            return protocol.EXIT_PROTOCOL
        if cmd["t"] == "stop":
            self._write(protocol.end())
            self._goodbye()
            return protocol.EXIT_OK
        if self.game is None:                         # never a recording of a game the relay did not accept
            self.fail("the recording window started the recording before the relay had accepted a game")
            self._goodbye()
            return protocol.EXIT_PROTOCOL
        try:
            self._start_sources(cmd["origin"])
            code = self._run()
        finally:
            self._stop_sources()
        if code == protocol.EXIT_PROTOCOL:
            self._write(protocol.error(f"the recording window sent a message the relay does not take "
                                       f"({self._broke})"))
        elif code == protocol.EXIT_CRASH:
            self._write(protocol.error(self._failed))
        elif code == protocol.EXIT_HELD:
            self._write(protocol.error(HELD))
        self._write(protocol.end())
        self._goodbye()
        return code

    def _next(self, deadline: float, late: str) -> dict | int:
        """The recorder's next command, or the exit code when there is none: it hung up (EXIT_OK), its snapshot shows
        it gone while its pipe lives on, perhaps in another process (EXIT_TARGET), or nothing came by `deadline` (then
        `late` is said, EXIT_NO_CLIENT), or by the end of the capture service's hold (`until`) when that comes first
        (then HELD, EXIT_HELD). The recorder is looked for once every STATUS_EVERY_S."""
        code = protocol.EXIT_NO_CLIENT
        if self.until is not None and self.until < deadline:      # the hold ends first: its words, not the wait's
            deadline, late, code = self.until, HELD, protocol.EXIT_HELD
        next_look = 0.0
        while True:
            cmd = self._command(min(TICK_S * 5, max(0.0, deadline - time.monotonic())))
            if cmd is not None:
                return cmd
            if self._gone.is_set():
                return protocol.EXIT_OK
            now = time.monotonic()
            if now >= deadline:
                self._write(protocol.error(late))
                self._write(protocol.end())
                return code
            if now >= next_look:
                next_look = now + STATUS_EVERY_S
                if self._recorder_left():
                    self._write(protocol.error("the recording window is no longer running"))
                    self._write(protocol.end())
                    return protocol.EXIT_TARGET

    def _recorder_left(self) -> bool:
        """True when the relay's own snapshot shows the recorder gone, someone else holding its number, or it in
        another session; a snapshot that fails says nothing (the next one is asked)."""
        try:
            rec = self.parts.snapshot().get(self.cfg.recorder[0])
        except OSError:
            return False
        return rec is None or rec.create_time != self.cfg.recorder[1] or rec.session_id != self.session

    def _play(self) -> dict | int:
        """Play: each `game` the recorder names is judged (_judge) and answered, and a later one takes the place of
        the one before; the game recorded is the last one accepted. With none accepted the relay gives up at the
        recorder's own wait plus GAME_SLACK_S, counted from its hello; once one is, it waits START_WAIT_S for `start`,
        never past that same point plus START_WAIT_S, nor past the capture service's hold (_next). Returns the command
        that ends the wait, or the exit code."""
        game_by = time.monotonic() + self.cfg.wait_s + GAME_SLACK_S
        by = game_by
        while True:
            late = ("no game the relay could record started in time" if self.game is None
                    else "the recording did not start in time")
            cmd = self._next(by, late)
            if isinstance(cmd, int) or cmd["t"] != "game":
                return cmd
            proposed = cmd["game"]
            self._take(None)                          # this one takes the place of any game accepted before it
            why, retry = self._judge(proposed)
            if why is None:
                self._take(proposed)
                self._write(protocol.game(proposed))
                by = min(game_by + START_WAIT_S, time.monotonic() + START_WAIT_S)
            else:
                self._write(protocol.refused(proposed, why, retry))
                by = game_by

    def _judge(self, proposed: tuple[int, int]) -> tuple[str | None, bool]:
        """Play's folder rule on a process the recorder named, on the relay's own snapshots and its own read of the
        program's path (target.check_proposed): in the relay's session, started after the relay (`since`), a program
        under one of the forms the prompt approved, none of lag-doctor's own (the recorder's tree and the relay's),
        and the recorder still running. (why not, in one line of plain text, and whether asking again may help), or
        (None, False). This is where Stage 4's service will put its own policy (the caller's session and account)."""
        from lagdoctor.capture import target
        return target.check_proposed(
            self.parts.snapshot, self.parts.image, *proposed, session=self.session, folders=self.cfg.folders,
            since=self.since, own=(self.cfg.recorder, self.me), alive=(self.cfg.recorder,))

    def _take(self, game: tuple[int, int] | None) -> None:
        """Play: the game it will record, accepted just now (None: none, until the next one is)."""
        self.game = None if game is None else (game[0], game[1])
        self._game = {} if game is None else {game[0]: game[1]}
        self._game_pids = frozenset(self._game)

    def _run(self) -> int:
        next_status = 0.0
        while True:
            cmd = self._command(TICK_S)
            if cmd is not None:
                if cmd["t"] == "failed":
                    return protocol.EXIT_CRASH
                return protocol.EXIT_OK if cmd["t"] == "stop" else protocol.EXIT_PROTOCOL
            if self.until is not None and time.monotonic() >= self.until:
                return protocol.EXIT_HELD             # the caller's hold is over: serve() stops the sources, says why
            if self._gone.is_set():
                return protocol.EXIT_OK
            self._flush()
            now = time.monotonic()
            if now >= next_status:
                next_status = now + STATUS_EVERY_S
                self._send_frames()
                self._send_net()

    def _refresh_loop(self) -> None:
        """The once-a-second refresh on a thread of its own, so a recorder that reads slowly (each write may wait
        WRITE_TIMEOUT_S) cannot stretch how long a pid keeps its standing."""
        while not self._refresh_stop.wait(STATUS_EVERY_S):
            try:
                self._refresh()
            except Exception as exc:  # noqa: BLE001 - unrefreshed, a reused pid would keep the game's standing
                self._failed = f"the relay stopped checking the game's processes ({type(exc).__name__}: {exc})"
                self._commands.put({"t": "failed"})
                return

    def _start_sources(self, origin: float) -> None:
        self._refresh()                                   # the game's processes before its first event
        if self._gone.is_set():                           # the recorder is gone (its pipe may live on elsewhere)
            return
        self._refresher = threading.Thread(target=self._refresh_loop, name="relay-refresh", daemon=True)
        self._refresher.start()
        if self.cfg.network:
            self._etw = self.parts.etw(self._on_event)
            try:
                self._etw.start()
                self._net_started, self._net_why = True, ""
            except OSError as exc:
                refused = getattr(exc, "winerror", None) == 5
                self._net_started = False
                self._net_why = ("Windows refused the relay the network trace (access denied)" if refused else
                                 f"the relay could not start the network trace: {exc}")
            self._send_net()
        if self.cfg.frames:
            why = self._game_refused()
            if why is not None:                           # it exited while the recording got ready: no PresentMon
                from lagdoctor.capture.presentmon import TOOL
                self._frames_final = True
                self._write(protocol.frames_status("failed", f"PresentMon was not started: {why}", TOOL, final=True))
                return
            # The frame source is told the game it records: the approved one, or Play's accepted one.
            self._frames = self.parts.frames(replace(self.cfg, game=self.game), self._on_row, self._game_alive)
            self._frames.start(origin)
            self._send_frames()

    def _game_refused(self) -> str | None:
        """accept_target again, on a fresh snapshot, just before PresentMon opens the game by its pid: the network
        trace's start took a moment, and with --game minutes may have passed since the relay's first check, and a pid
        Windows gave away must not be traced. The game is the one it accepted, as that exact process (Play's folder
        rule judged it already, and a process's program never changes)."""
        from lagdoctor.capture.target import accept_target
        try:
            procs = self.parts.snapshot()
        except OSError as exc:
            return f"the game could not be checked again ({exc})"
        return accept_target(procs, *self.game, session=self.session, approved=self.game)

    def _stop_sources(self) -> None:
        """The network trace first (milliseconds; its last events are delivered inside stop), then PresentMon (up to
        FRAMES_STOP_S); each one's last data, then its final status. Safe to call again."""
        self._refresh_stop.set()
        refresher, self._refresher = self._refresher, None
        if refresher is not None:
            refresher.join(10.0)
        etw_session, self._etw = self._etw, None
        if etw_session is not None:
            try:
                etw_session.stop()
            except Exception as exc:  # noqa: BLE001 - PresentMon must still be stopped
                self._net_why = f"the network trace did not stop cleanly: {type(exc).__name__}: {exc}"
            try:
                self._refresh()                       # the last events' programs placed before the final totals
            except Exception as exc:  # noqa: BLE001 - PresentMon must still be stopped, and the first reason said
                self._failed = self._failed or (f"the relay stopped checking the game's processes "
                                                f"({type(exc).__name__}: {exc})")
            self._flush()
            self._send_net(final=True, session=etw_session)
        frames, self._frames = self._frames, None
        if frames is not None:
            try:
                frames.stop(FRAMES_STOP_S)
            except Exception:  # noqa: BLE001 - its status says what happened
                pass
            self._flush()
            self._send_frames(final=True, source=frames)

    def _goodbye(self) -> None:
        """Wait (briefly) for the recorder to hang up after `end`, so it reads everything before the pipe goes."""
        deadline = time.monotonic() + GOODBYE_S
        while not self._gone.is_set() and time.monotonic() < deadline:
            time.sleep(0.05)

    # ---- frames ---------------------------------------------------------------------------------------
    def _game_alive(self) -> bool:
        try:
            p = self.parts.snapshot().get(self.game[0])
        except OSError:
            return True                            # cannot tell this second: ask again the next
        return p is not None and p.create_time == self.game[1]

    def _on_row(self, row: dict[str, Any]) -> None:
        """PresentMonSource's forward: one of the game's rows, on PresentMon's reader thread."""
        cols, values = protocol.row_values(row)
        with self._lock:
            if len(self._rows) >= MAX_QUEUED_ROWS:
                self._rows_lost += 1
                return
            self._rows.append((cols, values))

    def _send_frames(self, final: bool = False, source: Any = None) -> None:
        source = source if source is not None else self._frames
        if source is None or self._frames_final:
            return
        ended = final or bool(getattr(source, "ended", False))   # first: a status read after it is its last one
        st = source.status()
        state, detail = st.state, st.detail
        with self._lock:
            lost = self._rows_lost
        if lost:
            detail += f"; {lost} frame rows could not be passed on to the recording window"
            state = "partial" if state == "ok" else state
        if ended:
            self._flush()
            info = None
            try:
                info = dict(source.result().get("presentmon") or {})
            except Exception:  # noqa: BLE001 - the status still goes
                pass
            self._frames_final = True
            self._write(protocol.frames_status(state, detail, st.tool, final=True, info=info))
            return
        now = (state, detail, st.tool)
        if now != self._frames_sent:
            self._frames_sent = now
            self._write(protocol.frames_status(state, detail, st.tool))

    # ---- network ----------------------------------------------------------------------------------------
    def _on_event(self, ev: Any) -> None:
        """One Kernel-Network event, on the ETW consumer thread: the game's and the recorder's go on whole; for
        everyone else only bytes are counted, and only per program."""
        from lagdoctor.capture import netflows
        spec = netflows.EVENTS.get(ev.id)
        if spec is None:
            with self._lock:
                self._counts["ignored"] += 1
            return
        data = ev.data
        if not netflows.layout_ok(ev.id, ev.version, len(data)):
            with self._lock:                       # whose it was cannot be read: only counted, never passed on
                self._layout_bad[str(ev.id)] = self._layout_bad.get(str(ev.id), 0) + 1
            return
        pid, size = _PID_SIZE.unpack_from(data, 0)
        if pid in self._game_pids or pid == self.cfg.recorder[0]:
            with self._lock:
                if len(self._events) >= MAX_QUEUED_EVENTS:
                    self._events_lost += 1
                    return
                self._events.append((ev.timestamp, ev.id, ev.version, bytes(data)))
            return
        if spec[1] not in ("send", "recv"):
            return
        with self._lock:
            row = self._pending.get(pid)
            if row is None:
                row = self._pending[pid] = [0, 0]
            row[0 if spec[1] == "recv" else 1] += size

    def _refresh(self) -> None:
        """Once a second: the game's processes now, and whose session each counted program runs in.

        The game's processes are its tree while it runs, plus those seen before that still run as the same process;
        each in the recorder's session. A pid whose process changed is dropped at once: Windows gave the number to
        another program, perhaps another user's, whose events must not leave the relay. (Between two snapshots a reused
        number keeps its old standing for at most a second; a program's bytes are placed by the process holding the
        pid when they are, so a pid reused within that second can carry another program's count.)"""
        from lagdoctor.capture.target import descendants
        try:
            procs = self.parts.snapshot()
        except OSError:
            return                                  # the next second tries again; pending bytes wait
        rec = procs.get(self.cfg.recorder[0])
        if rec is None or rec.create_time != self.cfg.recorder[1]:
            # The recorder is gone (its pipe handle may live on in another process): its number is someone else's
            # now. The recording ends, and nothing more is written.
            self._gone.set()
        tree = descendants(procs, *self.game)
        if self.tree is not None:
            tree = self.tree(procs, self.game, tree)
        game: dict[int, int] = {}
        for pid in tree | set(self._game):
            p = procs.get(pid)
            if p is None or p.session_id != self.session:
                continue
            if pid in tree or self._game.get(pid) == p.create_time:
                game[pid] = p.create_time
        self._game, self._game_pids = game, frozenset(game)
        with self._lock:
            pending, self._pending = self._pending, {}
            self._place(pending, procs)

    def _place(self, pending: dict[int, list[int]], procs: dict) -> None:
        """Each program's new bytes into its total, by the session of the process holding its pid now (with the
        lock held: the main thread reads the totals)."""
        for pid, (rx, tx) in pending.items():
            p = procs.get(pid)
            if p is None:
                self._counts["unattributed_bytes"] += rx + tx      # gone before it could be placed: never passed on
                continue
            known = self._programs.get(pid)
            if known is None or known[0] != p.create_time:
                if known is None and len(self._programs) >= MAX_PROGRAMS:
                    self._counts["programs_dropped"] += 1
                    continue
                known = self._programs[pid] = (p.create_time, p.session_id == self.session)
            if not known[1]:
                self._counts["other_session_bytes"] += rx + tx
                continue
            total = self._totals.setdefault(pid, [0, 0])
            total[0] += rx
            total[1] += tx

    def _top_totals(self) -> list[list[int]]:
        with self._lock:
            ranked = sorted(((pid, tuple(row)) for pid, row in self._totals.items()),
                            key=lambda kv: (-(kv[1][0] + kv[1][1]), kv[0]))
        return [[pid, rx, tx] for pid, (rx, tx) in ranked[:protocol.MAX_TOTALS] if rx + tx > 0]

    def _send_net(self, final: bool = False, session: Any = None) -> None:
        if not self.cfg.network:
            return
        session = session if session is not None else self._etw
        health: dict[str, Any] = {}
        if session is not None:
            try:
                health = {k: v for k, v in dict(session.health()).items()
                          if v is None or isinstance(v, (bool, int, float, str))}
            except Exception as exc:  # noqa: BLE001
                health = {"health_error": f"{type(exc).__name__}: {exc}"}
        with self._lock:
            health["relay"] = dict(self._counts, rows_lost=self._rows_lost, events_lost=self._events_lost,
                                   layout_mismatch=dict(self._layout_bad))
        self._write(protocol.net_status(self._net_started, self._net_why, health, self._top_totals(), final=final))

    # ---- sending ----------------------------------------------------------------------------------------
    def _flush(self) -> None:
        with self._lock:
            rows, self._rows = self._rows, collections.deque()
            events, self._events = self._events, collections.deque()
        batch: list[list[Any]] = []
        cols: tuple[str, ...] | None = None
        for row_cols, values in rows:
            if row_cols != cols and batch:
                self._send_rows(cols, batch)
                batch = []
            cols = row_cols
            batch.append(values)
        if batch:
            self._send_rows(cols, batch)
        if events:
            messages, dropped = protocol.events_messages(events)
            with self._lock:
                self._events_lost += dropped
            for m in messages:
                self._write(m)

    def _send_rows(self, cols: tuple[str, ...], rows: list[list[Any]]) -> None:
        messages, dropped = protocol.rows_messages(cols, rows)
        with self._lock:
            self._rows_lost += dropped
        for m in messages:
            self._write(m)
