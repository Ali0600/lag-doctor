"""The relay process: PresentMon and the Kernel-Network session with administrator rights, for one recorder.

Run as `pythonw -B -E -s -m lagdoctor.relay ...` through one UAC prompt (relay/client.launch), with no console
window. Everything it does comes from that command line, which the prompt approved: the pipe's random name, the
recorder (pid, create time, account SID), the running game (pid, create time), and whether to run frame timing (with
the PresentMon file the recorder verified) and the network tracker. In order:

1. Its own handle-free snapshot must show the recorder and the game running as exactly those processes, in the
   relay's own Windows session (target.accept_target, which the recorder called before the prompt).
2. PresentMon must pass its check again here (setup.verify).
3. It creates the pipe: first instance only, remote clients refused, a DACL for the recorder's account, a medium
   label, and Administrators as its owner, which only an elevated token may set (the recorder checks it).
4. Only the recorder may stay connected: its pid and session are checked; anyone else is let go.
5. On `start` it runs PresentMon on the game (rows forwarded, not kept: PresentMonSource's `forward`) and the
   Kernel-Network session. It forwards the game's frame rows, the events of the game's processes (the game and what
   it started, from its own snapshot once a second) and of the recorder (its DNS self-test), whole; for every other
   program it counts bytes received and sent and passes on the totals only for programs in the recorder's session
   (DECISIONS 2026-11-08). Events of other programs never leave it.
6. On `stop`, or when the recorder hangs up, it stops both, sends what is left and the final statuses, then `end`.

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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from lagdoctor.relay import protocol

CLIENT_WAIT_S = 30.0          # the recorder connects as soon as the prompt's launch returns
START_WAIT_S = 300.0          # the recorder reads the PC's details before its collectors start
TICK_S = 0.1                  # rows and events go out this often
STATUS_EVERY_S = 1.0          # the game's processes, sessions, totals and statuses this often
WRITE_TIMEOUT_S = 10.0        # a recorder that reads nothing for this long is taken for gone
FRAMES_STOP_S = 25.0          # PresentMon's worst stop path (DESIGN.md, Collector contract)
GOODBYE_S = 5.0               # after `end`, how long the recorder has to hang up
MAX_QUEUED_ROWS = 100_000     # rows waiting for the pipe; past this they are counted as lost
MAX_QUEUED_EVENTS = 50_000
MAX_PROGRAMS = 4096           # programs whose session is remembered
OWNER = "O:BA"                # Administrators: Windows refuses that owner to an unelevated token (error 1307)
_PID_SIZE = struct.Struct("<II")   # NE 4.3: the payload's PID and size
_SID = re.compile(r"S-1-[0-9]{1,13}(?:-[0-9]{1,10}){1,15}")


@dataclass(frozen=True)
class Config:
    pipe: str
    recorder: tuple[int, int]               # (pid, create_time)
    sid: str                                # the recorder's account: the only one the pipe lets in
    game: tuple[int, int]
    frames: bool
    network: bool
    presentmon_exe: Path | None


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


def parse_args(argv: list[str]) -> Config:
    """The relay's command line, as relay/client.relay_argv builds it; ValueError for anything else."""
    p = _Parser(prog="lagdoctor.relay", add_help=False, allow_abbrev=False)
    p.add_argument("--pipe", required=True)
    p.add_argument("--recorder", required=True, type=_identity)
    p.add_argument("--sid", required=True)
    p.add_argument("--game", required=True, type=_identity)
    p.add_argument("--frames", action="store_true")
    p.add_argument("--presentmon-exe")
    p.add_argument("--network", action="store_true")
    a = p.parse_args(argv)
    if not protocol.is_pipe_name(a.pipe):
        raise ValueError("not a relay pipe name")
    if not _SID.fullmatch(a.sid):
        raise ValueError("not a SID")
    if not (a.frames or a.network):
        raise ValueError("nothing to run: neither --frames nor --network")
    exe = None
    if a.frames:
        if not a.presentmon_exe or not os.path.isabs(a.presentmon_exe):
            raise ValueError("--frames needs an absolute --presentmon-exe")
        exe = Path(a.presentmon_exe)
    elif a.presentmon_exe is not None:
        raise ValueError("--presentmon-exe without --frames")
    if a.recorder[0] == a.game[0]:
        raise ValueError("the recorder is not the game")
    return Config(a.pipe, a.recorder, a.sid, a.game, a.frames, a.network, exe)


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
    from lagdoctor import setup
    setup.verify(exe)


def _frames_source(cfg: Config, forward: Callable[[dict[str, Any]], None], alive: Callable[[], bool]):
    from lagdoctor.capture.presentmon import PresentMonSource
    return PresentMonSource(cfg.game[0], exe=cfg.presentmon_exe, is_alive=alive, forward=forward)


def _etw_session(on_event: Callable[[Any], None]):
    from lagdoctor.capture import netflows
    from lagdoctor.winapi import etw
    providers = [(netflows.KERNEL_NETWORK, netflows.LEVEL_INFORMATIONAL, netflows.KW_IPV4 | netflows.KW_IPV6)]
    return etw.RealtimeSession(netflows.SESSION_NAME, providers, on_event)


@dataclass
class Parts:
    """What the relay runs, made replaceable for tests; the defaults are the real ones."""
    snapshot: Callable[[], dict] | None = None              # pid -> ntquery.ProcInfo
    verify: Callable[[Path], Any] = _verify
    frames: Callable[..., Any] = _frames_source             # (cfg, forward, alive) -> a frames Collector
    etw: Callable[..., Any] = _etw_session                  # (on_event) -> an ETW session
    me: int = field(default_factory=os.getpid)


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
        relay = Relay(cfg, server, me.session_id, parts)
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
    """One recording: started by `start`, ended by `stop` or by the recorder hanging up."""

    def __init__(self, cfg: Config, server, session: int, parts: Parts) -> None:
        self.cfg, self.server, self.session, self.parts = cfg, server, session, parts
        self._commands: queue.Queue = queue.Queue()
        self._gone = threading.Event()          # the recorder hung up, or a write could not reach it
        self._broke = ""                        # the recorder sent what the protocol does not allow
        self._write_lock = threading.Lock()
        self._lock = threading.Lock()           # the queues and counters the source threads fill
        self._rows: collections.deque = collections.deque()
        self._events: collections.deque = collections.deque()
        self._rows_lost = self._events_lost = 0
        self._pending: dict[int, list[int]] = {}
        self._game_pids: frozenset[int] = frozenset({cfg.game[0]})
        self._programs: dict[int, tuple[int, bool]] = {}     # pid -> (create_time, in the recorder's session)
        self._totals: dict[int, list[int]] = {}
        self._counts = {"ignored": 0, "unattributed_bytes": 0, "other_session_bytes": 0, "programs_dropped": 0}
        self._layout_bad: dict[str, int] = {}
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
                self._commands.put(protocol.from_recorder(data))
            except protocol.ProtocolError as exc:
                self._broke = str(exc)
                self._commands.put({"t": "broken"})
                return

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
        if not self._write(protocol.hello(self.parts.me, self.cfg.game, self.cfg.frames, self.cfg.network)):
            return protocol.EXIT_OK
        threading.Thread(target=self._read_loop, name="relay-read", daemon=True).start()
        deadline = time.monotonic() + START_WAIT_S
        while True:
            cmd = self._command(min(TICK_S * 5, max(0.0, deadline - time.monotonic())))
            if cmd is not None:
                break
            if self._gone.is_set():
                return protocol.EXIT_OK
            if time.monotonic() >= deadline:
                self._write(protocol.error("the recording did not start in time"))
                self._write(protocol.end())
                return protocol.EXIT_NO_CLIENT
        if cmd["t"] == "broken":
            self.fail(f"the recording window sent a message the relay does not take ({self._broke})")
            return protocol.EXIT_PROTOCOL
        if cmd["t"] == "stop":
            self._write(protocol.end())
            self._goodbye()
            return protocol.EXIT_OK
        try:
            self._start_sources(cmd["origin"])
            code = self._run()
        finally:
            self._stop_sources()
        if code == protocol.EXIT_PROTOCOL:
            self._write(protocol.error(f"the recording window sent a message the relay does not take "
                                       f"({self._broke})"))
        self._write(protocol.end())
        self._goodbye()
        return code

    def _run(self) -> int:
        next_status = 0.0
        while True:
            cmd = self._command(TICK_S)
            if cmd is not None:
                if cmd["t"] == "stop":
                    return protocol.EXIT_OK
                self._broke = self._broke or "a second start"
                return protocol.EXIT_PROTOCOL
            if self._gone.is_set():
                return protocol.EXIT_OK
            self._flush()
            now = time.monotonic()
            if now >= next_status:
                next_status = now + STATUS_EVERY_S
                self._refresh()
                self._send_frames()
                self._send_net()

    def _start_sources(self, origin: float) -> None:
        if self.cfg.network:
            self._refresh()                               # the game's processes before its first event
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
            self._frames = self.parts.frames(self.cfg, self._on_row, self._game_alive)
            self._frames.start(origin)
            self._send_frames()

    def _stop_sources(self) -> None:
        """The network trace first (milliseconds; its last events are delivered inside stop), then PresentMon (up to
        FRAMES_STOP_S); each one's last data, then its final status. Safe to call again."""
        etw_session, self._etw = self._etw, None
        if etw_session is not None:
            try:
                etw_session.stop()
            except Exception as exc:  # noqa: BLE001 - PresentMon must still be stopped
                self._net_why = f"the network trace did not stop cleanly: {type(exc).__name__}: {exc}"
            self._refresh()
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
            p = self.parts.snapshot().get(self.cfg.game[0])
        except OSError:
            return True                            # cannot tell this second: ask again the next
        return p is not None and p.create_time == self.cfg.game[1]

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
        st = source.status()
        state, detail = st.state, st.detail
        with self._lock:
            lost = self._rows_lost
        if lost:
            detail += f"; {lost} frame rows could not be passed on to the recording window"
            state = "partial" if state == "ok" else state
        ended = final or bool(getattr(source, "ended", False))
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
        """Once a second: the game's processes now, and whose session each counted program runs in."""
        from lagdoctor.capture.target import descendants
        try:
            procs = self.parts.snapshot()
        except OSError:
            return                                  # the next second tries again; pending bytes wait
        game = descendants(procs, *self.cfg.game)
        if game:
            self._game_pids = frozenset(game)
        with self._lock:
            pending, self._pending = self._pending, {}
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
        ranked = sorted(self._totals.items(), key=lambda kv: (-(kv[1][0] + kv[1][1]), kv[0]))
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
