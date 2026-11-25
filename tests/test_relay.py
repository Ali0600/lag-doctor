"""The relay (lagdoctor/relay/): the relay process's checks, the recorder's checks of it, and recordings through it.

The relay's own code runs on a thread of this process over a real pipe, unelevated, and stands in for the elevated
process `launch_elevated` would start (`InProcessRelay`): nothing here raises a UAC prompt. Its frame source is a real
PresentMon replaying one of PresentMon's gold captures (no rights needed), or a stand-in; its network source is a
stand-in ETW session that delivers Kernel-Network events built here. Its pipe is owned by this token's default owner
instead of Administrators, which only an elevated token may set: `test_only_an_elevated_relay_can_own_its_pipe`
checks that owner on both sides of elevation.
"""
from __future__ import annotations

import ctypes
import dataclasses
import json
import os
import secrets
import socket
import struct
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

import pytest

from lagdoctor import clock
from lagdoctor.capture import netflows as nf
from lagdoctor.capture import presentmon as pm
from lagdoctor.capture import session as sess
from lagdoctor.capture import target as tgt
from lagdoctor.capture import tracelock
from lagdoctor.capture.base import Collector
from lagdoctor.model import F
from lagdoctor.relay import client, protocol, server
from lagdoctor.winapi import elevation, ntquery, pipe

BASE_PY = getattr(sys, "_base_executable", sys.executable)
GOLD_PID = 24892                         # PresentBench.exe in test_case_5.etl: 265 rows (tests/test_presentmon_source)
SERVER_IP, LOCAL_IP = "203.0.113.5", "192.168.0.10"
IN_SESSION, OTHER_SESSION = 0xFFFFFF00, 0xFFFFFF04          # programs that exist only in the stand-in snapshot


# ------------------------------------------------------------------------------------------ helpers ---

def _until(pred, timeout: float = 10.0, what: str = "the condition") -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.02)


def _me() -> ntquery.ProcInfo:
    return ntquery.Snapshotter().snapshot()[os.getpid()]


def _fake_proc(pid: int, session: int, create_time: int = 1, name: str = "x.exe") -> ntquery.ProcInfo:
    return ntquery.ProcInfo(pid=pid, ppid=4, create_time=create_time, name=name, session_id=session, user_100ns=0,
                            kernel_100ns=0, ws_bytes=0, private_bytes=0, page_faults=0, read_bytes=0, write_bytes=0,
                            other_bytes=0, read_ops=0, write_ops=0, other_ops=0, thread_count=1, threads=None)


def snapshot_with(*fakes: ntquery.ProcInfo):
    """The real process list, plus processes that exist only here."""
    snap = ntquery.Snapshotter()
    lock = threading.Lock()

    def snapshot():
        with lock:
            procs = snap.snapshot()
        for f in fakes:
            procs[f.pid] = f
        return procs
    return snapshot


def _default_owner() -> str:
    """The owner this token gives a pipe that names none: what the in-process relay's pipe is owned by."""
    with pipe.PipeServer(f"lagdoctor-owner-{secrets.token_hex(8)}", pipe.client_dacl([elevation.token_user_sid()])) as s:
        return s.owner_sid()


@pytest.fixture
def game():
    """A running 'game' in this session: a child process waiting on its stdin."""
    p = subprocess.Popen([BASE_PY, "-I", "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
    try:
        snap = ntquery.Snapshotter()
        _until(lambda: p.pid in snap.snapshot(), what="the game to start")
        yield {"pid": p.pid, "create_time": snap.snapshot()[p.pid].create_time, "name": "python.exe"}
    finally:
        p.stdin.close()
        p.wait(10)


class InProcessRelay:
    """launch_elevated's stand-in: runs the relay's main() on a thread here with `parts`, and is itself the 'process'
    launch_elevated hands back (this process's pid, the thread's exit code)."""

    def __init__(self, parts: server.Parts, owner: str = "", rewrite=None) -> None:
        self.parts, self.owner, self.rewrite = parts, owner, rewrite
        self.argv: list[str] | None = None
        self.code: int | None = None
        self.closed = False
        self.thread: threading.Thread | None = None
        self.pid = os.getpid()

    def __call__(self, argv, cwd):
        assert argv[1:6] == ["-B", "-E", "-s", "-m", "lagdoctor.relay"], argv
        assert Path(argv[0]).name.lower() == "pythonw.exe" and Path(cwd).is_absolute()
        self.argv = list(argv)
        args = self.rewrite(argv[6:]) if self.rewrite else argv[6:]
        self.thread = threading.Thread(target=self._run, args=(args,), name="relay-under-test", daemon=True)
        self.thread.start()
        return self

    def _run(self, args) -> None:
        self.code = server.main(args, owner=self.owner, parts=self.parts)

    def wait(self, timeout_s=None):
        self.thread.join(timeout_s)
        return None if self.thread.is_alive() else self.code

    def close(self) -> None:
        self.closed = True


def launch(game, relay: InProcessRelay, *, frames=False, network=True, exe=None, owner=None) -> client.RelayLink:
    me = _me()
    return client.launch(game, presentmon_exe=exe, frames=frames, network=network, recorder=(me.pid, me.create_time),
                         sid=elevation.token_user_sid(), launcher=relay,
                         owner=owner if owner is not None else _default_owner())


class FakeFrames(Collector):
    """The relay's frame source when no real PresentMon is wanted: a few rows of the game, then waits to be stopped."""

    name = "frames"

    def __init__(self, cfg, forward, alive, rows=5, stops=None):
        super().__init__()
        self.pid, self.forward, self.rows, self.stops = cfg.game[0], forward, rows, stops

    def run(self):
        self.set_status("ok", "starting PresentMon", tool=pm.TOOL)
        qpc = clock.qpc_now()
        for i in range(self.rows):
            self.forward({"Application": "game.exe", "ProcessID": self.pid, "SwapChainAddress": 0x1000,
                          "TimeInQPC": qpc + i * 10_000, "MsBetweenPresents": 16.5, "PresentMode": "Hardware: Independent Flip",
                          "PresentRuntime": "DXGI", "MsUntilDisplayed": None})
        self.set_status("ok", f"{self.rows} frames from 1 swap chain(s) of PID {self.pid}", tool=pm.TOOL)
        self._stop_event.wait()

    def on_stop(self):
        if self.stops is not None:
            self.stops.append("frames")

    def result(self):
        return {"columns": {}, "presentmon": {"version": "2.6.0", "rows": self.rows, "exit_code": 0}}


def payload(eid: int, pid: int, size: int, daddr: str, saddr: str, dport: int, sport: int) -> bytes:
    """A Kernel-Network IPv4 payload laid out as NE 4.3 says (the decoder's own tests prove that layout)."""
    data = (struct.pack("<II", pid, size) + socket.inet_aton(daddr) + socket.inet_aton(saddr)
            + struct.pack(">HH", dport, sport))
    return data + bytes(nf.EXPECTED_LENGTH[eid] - len(data))


class FakeEtw:
    """The relay's ETW session stand-in: delivers `script` ((id, version, payload) each) when started."""

    def __init__(self, on_event, script, stops=None, alive=True):
        self.on_event, self.script, self.stops = on_event, script, stops
        self.delivered = 0
        self.running = False
        self.alive = alive

    def start(self):
        self.running = True
        for eid, ver, data in self.script:
            self.on_event(nf.etw.EtwEvent(clock.qpc_now(), nf.KERNEL_NETWORK, eid, ver, 0, 4, 0, 0, 4, 0, data))
            self.delivered += 1

    def stop(self):
        self.running = False
        if self.stops is not None:
            self.stops.append("net")

    def health(self):
        return {"events": self.delivered, "lost": 0, "buffers_lost": 0, "callback_errors": 0,
                "pump_alive": self.running and self.alive, "process_trace_rc": None}


def own_trace_lock(trusted=None) -> tracelock.TraceLock:
    """The machine-wide trace lock as this token may hold it: owned by its own account, where the relay's is owned by
    Administrators (only an elevated token may set that), under the test's own name (tests/conftest.py). Trusted
    only when `trusted` names this account, so by default a second one reads TAKEN, never BUSY."""
    return tracelock.TraceLock(elevation.token_user_sid(), trusted=trusted)


def parts(*, snapshot=None, frames=None, etw=None, verify=None, trace_lock=None) -> server.Parts:
    return server.Parts(snapshot=snapshot or snapshot_with(), verify=verify or (lambda exe: None),
                        frames=frames or (lambda cfg, forward, alive: FakeFrames(cfg, forward, alive)),
                        etw=etw or (lambda on_event: FakeEtw(on_event, [])), trace_lock=trace_lock or own_trace_lock)


@pytest.fixture
def spy_writes(monkeypatch):
    """Every message the relay writes, decoded, in order."""
    sent: list[dict] = []
    real = pipe.PipeServer.write

    def write(self, data, timeout_s):
        sent.append(protocol.from_relay(bytes(data)))
        return real(self, data, timeout_s)
    monkeypatch.setattr(pipe.PipeServer, "write", write)
    return sent


# ------------------------------------------------------------------------- recordings through it ---

def test_frames_through_the_relay_are_the_frames_a_local_replay_keeps(presentmon_exe, gold_etl_dir, game):
    gold = gold_etl_dir / "test_case_5.etl"
    local = pm.PresentMonSource(GOLD_PID, exe=presentmon_exe, etl_file=gold)
    local.start(5.0)
    _until(lambda: local.ended, 60, "the local replay to end")
    local.stop()

    relay = InProcessRelay(parts(frames=lambda cfg, forward, alive: pm.PresentMonSource(
        GOLD_PID, exe=presentmon_exe, etl_file=gold, forward=forward)))
    link = launch(game, relay, frames=True, network=False, exe=presentmon_exe)
    try:
        relayed = link.frames_collector(GOLD_PID)
        relayed.start(5.0)                          # the same origin: the same times
        _until(lambda: relayed.ended, 60, "the relayed replay to end")
        relayed.stop()
    finally:
        code = link.close(wait_s=5)
    a, b = local.result(), relayed.result()
    assert len(a["columns"][F.T]) == 265
    assert b["columns"] == a["columns"] and b["swap_chains"] == a["swap_chains"] and b["main_chain"] == a["main_chain"]
    assert (relayed.status().state, relayed.status().detail) == (local.status().state, local.status().detail)
    assert b["presentmon"]["rows"] == 265 and b["presentmon"]["exit_code"] == 0
    assert b["presentmon"]["relay"] == {"pid": os.getpid(), "rows_received": 265, "rows_refused": 0}
    assert code == protocol.EXIT_OK and relay.code == protocol.EXIT_OK


def _net_script(game_pid: int, recorder: int) -> list[tuple[int, int, bytes]]:
    out = []
    for i in range(30):                                       # the game's UDP flow, both ways
        out.append((42, 0, payload(42, game_pid, 100, SERVER_IP, LOCAL_IP, 27015, 50000)))
        out.append((43, 0, payload(43, game_pid, 200, LOCAL_IP, SERVER_IP, 50000, 27015)))
    out.append((11, 0, payload(11, game_pid, 500, LOCAL_IP, "198.51.100.9", 50004, 443)))   # the game's TCP, too
    out.append((42, 0, payload(42, recorder, 40, "1.1.1.1", LOCAL_IP, 53, 50001)))   # the recorder's own (self-test)
    for _ in range(4):
        out.append((11, 0, payload(11, IN_SESSION, 1000, LOCAL_IP, "198.51.100.7", 50002, 443)))
        out.append((11, 0, payload(11, OTHER_SESSION, 5000, LOCAL_IP, "198.51.100.8", 50003, 443)))
    out.append((13, 0, b"\0" * 28))                             # an event the tracker does not decode
    out.append((42, 0, b"\0" * 27))                             # a layout other than the proven one
    return out


def test_only_the_games_and_the_recorders_events_leave_the_relay_and_totals_only_for_this_session(game, spy_writes):
    me = _me()
    stops: list[str] = []
    relay = InProcessRelay(parts(
        snapshot=snapshot_with(_fake_proc(IN_SESSION, me.session_id), _fake_proc(OTHER_SESSION, me.session_id + 99)),
        etw=lambda on_event: FakeEtw(on_event, _net_script(game["pid"], me.pid), stops)))
    link = launch(game, relay, network=True)
    tracker = nf.FlowTracker({game["pid"]}, selftest=False, local_addrs=lambda: {LOCAL_IP}, relay=link)
    try:
        tracker.start(clock.now())
        _until(lambda: any(m["t"] == "net" and m["totals"] for m in spy_writes), 10, "the first totals")
        tracker.stop()
    finally:
        code = link.close(wait_s=5)
    forwarded = [ev for m in spy_writes if m["t"] == "events" for ev in m["ev"]]
    pids = {struct.unpack_from("<I", data, 0)[0] for _, _, _, data in forwarded}
    assert pids == {game["pid"], me.pid} and len(forwarded) == 62          # 61 of the game's, 1 of the recorder's
    assert all(len(data) == nf.EXPECTED_LENGTH[eid] for _, eid, _, data in forwarded)
    totals = [t for m in spy_writes if m["t"] == "net" for t in m["totals"]]
    assert totals and {pid for pid, _, _ in totals} == {IN_SESSION}         # never the other session's program
    final = [m for m in spy_writes if m["t"] == "net"][-1]
    assert final["final"] and final["totals"] == [(IN_SESSION, 4000, 0)]
    counts = final["health"]["relay"]
    assert counts["other_session_bytes"] == 20000 and counts["ignored"] == 1 and counts["layout_mismatch"] == {"42": 1}
    r = tracker.result()
    talkers = {t["pid"]: (t["rx_bytes"], t["tx_bytes"]) for t in r["talkers"]}
    assert talkers[IN_SESSION] == (4000, 0) and OTHER_SESSION not in talkers
    assert talkers[game["pid"]] == (30 * 200 + 500, 30 * 100)              # counted here, from its whole events
    assert r["game_wire_bytes"] == [30 * 200 + 500, 30 * 100]               # all of it crossed the network
    assert r["talkers_scope"] == "session" and r["layout"] == "unverified"
    st = tracker.status()
    assert st.state == "partial" and "unknown layout" in st.detail and "ids 42" in st.detail, st
    assert stops == ["net"] and code == protocol.EXIT_OK
    assert [m["t"] for m in spy_writes][-2:] == ["net", "end"]


def test_a_relayed_recording_through_a_session_keeps_frames_flows_and_the_relay_among_its_own(game):
    me = _me()
    relay = InProcessRelay(parts(snapshot=snapshot_with(_fake_proc(IN_SESSION, me.session_id)),
                                 etw=lambda on_event: FakeEtw(on_event, _net_script(game["pid"], me.pid))))
    link = launch(game, relay, frames=True, network=True, exe=Path(sys.executable).resolve())
    try:
        target = dict(game)
        opts = sess.SessionOptions(duration_s=1.5, hotkey=None, relay=link, tool_procs=link.tool_procs())
        cols = [link.frames_collector(game["pid"]),
                nf.FlowTracker({game["pid"]}, selftest=False, local_addrs=lambda: {LOCAL_IP}, relay=link)]
        cap = sess.Session(target, opts, collectors=cols, inventory={"adapters": []}, alive=lambda: True,
                           printer=lambda line: None).run()
    finally:
        link.close()
    assert len(cap.frames[F.T]) == 5 and cap.sources["frames"].state == "ok", cap.sources["frames"]
    assert cap.target["presentmon"]["relay"]["rows_received"] == 5
    assert cap.net_flows["talkers_scope"] == "session"
    assert any(t["pid"] == IN_SESSION for t in cap.net_flows["talkers"])
    assert os.getpid() in cap.target["tool_pids"]                          # the relay here is this process


def test_the_relay_stops_the_network_trace_then_presentmon_and_says_end_last(game, spy_writes):
    stops: list[str] = []
    relay = InProcessRelay(parts(frames=lambda cfg, fw, alive: FakeFrames(cfg, fw, alive, stops=stops),
                                 etw=lambda on_event: FakeEtw(on_event, [], stops)))
    link = launch(game, relay, frames=True, network=True, exe=Path(sys.executable).resolve())
    frames = link.frames_collector(game["pid"])
    tracker = nf.FlowTracker({game["pid"]}, selftest=False, local_addrs=lambda: {LOCAL_IP}, relay=link)
    try:
        origin = clock.now()
        frames.start(origin)
        tracker.start(origin)
        _until(lambda: frames._rows == 5, 10, "the rows")
        frames.stop()
        tracker.stop()
    finally:
        link.close()
    assert stops == ["net", "frames"]
    assert tracker.status().state == "ok", tracker.status()                # the relay's stop is no death
    finals = [m["t"] for m in spy_writes if m.get("final") or m["t"] == "end"]
    assert finals == ["net", "frames", "end"], finals
    assert frames.status().state == "ok" and "5 frames" in frames.status().detail


def test_a_recorder_that_hangs_up_mid_recording_stops_the_relays_sources(game):
    stops: list[str] = []
    relay = InProcessRelay(parts(frames=lambda cfg, fw, alive: FakeFrames(cfg, fw, alive, stops=stops),
                                 etw=lambda on_event: FakeEtw(on_event, [], stops)))
    link = launch(game, relay, frames=True, network=True, exe=Path(sys.executable).resolve())
    link.start(clock.now())
    time.sleep(0.5)
    link._client.close()                                   # the recording window went away
    relay.thread.join(15)
    assert not relay.thread.is_alive() and relay.code == protocol.EXIT_OK
    assert sorted(stops) == ["frames", "net"]
    link.close()


def test_a_relay_that_breaks_off_ends_both_collectors_with_the_reason(game, monkeypatch):
    made: list = []
    real_init = pipe.PipeServer.__init__

    def keep(self, *a, **k):
        real_init(self, *a, **k)
        made.append(self)
    monkeypatch.setattr(pipe.PipeServer, "__init__", keep)
    relay = InProcessRelay(parts(etw=lambda on_event: FakeEtw(on_event, [])))
    link = launch(game, relay, frames=True, network=True, exe=Path(sys.executable).resolve())
    frames = link.frames_collector(game["pid"])
    tracker = nf.FlowTracker({game["pid"]}, selftest=False, local_addrs=lambda: {LOCAL_IP}, relay=link)
    try:
        origin = clock.now()
        frames.start(origin)
        tracker.start(origin)
        _until(lambda: frames._rows == 5, 10, "the rows")
        made[-1].close()                                   # the relay's end of the pipe is gone (made last)
        _until(lambda: link.ended.is_set(), 10, "the link to notice")
        _until(lambda: frames.ended, 10, "frames to end")
        time.sleep(1.2)                                    # the tracker checks its session's consumer each second
        tracker.stop()
    finally:
        link.close()
    st = frames.status()
    assert st.state == "partial" and "5 frames, then frame timing stopped" in st.detail and "relay ended" in st.detail
    fst = tracker.status()
    assert fst.state == "partial" and "the relay stopped" in fst.detail, fst
    assert tracker.result()["health"]["consumer_stopped_t"] is not None


# ------------------------------------------------------------------------------- the relay's checks ---

def _argv(game, **over) -> list[str]:
    me = _me()
    a = {"--pipe": protocol.pipe_name(), "--recorder": f"{me.pid}:{me.create_time}",
         "--sid": elevation.token_user_sid(), "--game": f"{game['pid']}:{game['create_time']}"}
    a.update(over)
    out = [x for k, v in a.items() if v is not None for x in (k, v)]
    return out + ["--network"]


@pytest.mark.parametrize("bad", [
    {"--pipe": "lagdoctor-relay-XYZ"}, {"--pipe": "..\\C:\\x"}, {"--sid": "S-1-5-21 1001"}, {"--sid": None},
    {"--recorder": "0:5"}, {"--recorder": "5"}, {"--game": "12:-1"}, {"--game": None}])
def test_the_relay_takes_only_a_command_line_lag_doctor_builds(game, bad, monkeypatch):
    monkeypatch.setattr(pipe, "PipeServer", lambda *a, **k: pytest.fail("a pipe was made"))
    assert server.main(_argv(game, **bad), owner="", parts=parts()) == protocol.EXIT_BAD_ARGS


def test_the_relay_refuses_frames_without_an_absolute_presentmon_and_a_lone_recorder_as_game(game, monkeypatch):
    monkeypatch.setattr(pipe, "PipeServer", lambda *a, **k: pytest.fail("a pipe was made"))
    base = _argv(game)
    for argv in (base + ["--frames"], base + ["--frames", "--presentmon-exe", "rel\\pm.exe"],
                 base + ["--presentmon-exe", str(Path(sys.executable).resolve())],
                 [a for a in base if a != "--network"],
                 _argv(game, **{"--game": _argv(game)[3]}), base + ["--extra"]):
        assert server.main(argv, owner="", parts=parts()) == protocol.EXIT_BAD_ARGS, argv


def test_the_relay_records_only_the_approved_game_and_recorder_in_its_own_session(game, monkeypatch):
    monkeypatch.setattr(pipe, "PipeServer", lambda *a, **k: pytest.fail("a pipe was made"))
    me = _me()
    stale_game = _argv(game, **{"--game": f"{game['pid']}:{game['create_time'] + 1}"})
    stale_me = _argv(game, **{"--recorder": f"{me.pid}:{me.create_time + 1}"})
    for argv in (stale_game, stale_me):
        assert server.main(argv, owner="", parts=parts()) == protocol.EXIT_TARGET
    elsewhere = _fake_proc(game["pid"], me.session_id + 1, game["create_time"])        # the game, in another session
    assert server.main(_argv(game), owner="", parts=parts(snapshot=snapshot_with(elsewhere))) == protocol.EXIT_TARGET


def test_a_presentmon_that_fails_its_check_here_is_never_run(game, monkeypatch):
    monkeypatch.setattr(pipe, "PipeServer", lambda *a, **k: pytest.fail("a pipe was made"))
    from lagdoctor import setup

    def bad(exe):
        raise setup.VerifyError("its SHA-256 does not match")
    argv = _argv(game) + ["--frames", "--presentmon-exe", str(Path(sys.executable).resolve())]
    assert server.main(argv, owner="", parts=parts(verify=bad)) == protocol.EXIT_NOT_READY


def test_a_pipe_name_someone_took_first_is_never_joined(game):
    argv = _argv(game)
    name = argv[argv.index("--pipe") + 1]
    with pipe.PipeServer(name, pipe.client_dacl([elevation.token_user_sid()])):
        assert server.main(argv, owner="", parts=parts()) == protocol.EXIT_PIPE


def test_a_recorder_that_never_connects_ends_the_relay(game, monkeypatch):
    monkeypatch.setattr(server, "CLIENT_WAIT_S", 0.5)
    t0 = time.monotonic()
    assert server.main(_argv(game), owner="", parts=parts()) == protocol.EXIT_NO_CLIENT
    assert time.monotonic() - t0 < 5


class StandInTraceLock:
    """The relay's trace lock with a set answer: acquire() returns it, or raises it; each call is noted in `calls`."""

    def __init__(self, answer, calls: list) -> None:
        self.answer, self.calls = answer, calls

    def acquire(self):
        self.calls.append("acquire")
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer

    def release(self) -> None:
        self.calls.append("release")


def test_a_relay_that_finds_another_recording_tracing_ends_before_its_pipe_exists(game, monkeypatch):
    mine = {elevation.token_user_sid()}
    other = own_trace_lock(mine)                    # another Windows session's recording holds the lock
    assert other.acquire() == tracelock.HELD
    try:
        monkeypatch.setattr(pipe, "PipeServer", lambda *a, **k: pytest.fail("a pipe was made"))
        assert server.main(_argv(game), owner="", parts=parts(trace_lock=lambda: own_trace_lock(mine))) == \
            protocol.EXIT_BUSY
    finally:
        other.release()
    calls: list[str] = []
    broken = OSError(None, "a device attached to the system is not functioning", None, 31)
    assert server.main(_argv(game), owner="", parts=parts(trace_lock=lambda: StandInTraceLock(broken, calls))) == \
        protocol.EXIT_CRASH                         # Windows could not say: nothing traced beside it
    assert calls == ["acquire"]


def test_the_relay_holds_the_trace_lock_from_before_its_pipe_until_it_ends(game, monkeypatch):
    mine = {elevation.token_user_sid()}
    seen: list[str] = []
    real_init = pipe.PipeServer.__init__

    def made(self, *a, **k):
        seen.append(tracelock.check(trusted=mine))   # the pipe comes once the lock is held
        real_init(self, *a, **k)
    monkeypatch.setattr(pipe.PipeServer, "__init__", made)
    monkeypatch.setattr(server, "CLIENT_WAIT_S", 0.5)
    assert server.main(_argv(game), owner="", parts=parts(trace_lock=lambda: own_trace_lock(mine))) == \
        protocol.EXIT_NO_CLIENT
    assert seen == [tracelock.BUSY] and tracelock.check(trusted=mine) == tracelock.FREE   # let go as it ended


def test_the_relay_takes_the_trace_lock_only_once_presentmon_passed_and_goes_on_when_another_program_holds_it(
        game, monkeypatch):
    from lagdoctor import setup
    calls: list[str] = []

    def bad(exe):
        raise setup.VerifyError("its SHA-256 does not match")
    argv = _argv(game) + ["--frames", "--presentmon-exe", str(Path(sys.executable).resolve())]
    assert server.main(argv, owner="", parts=parts(verify=bad, trace_lock=lambda: StandInTraceLock(
        tracelock.HELD, calls))) == protocol.EXIT_NOT_READY
    assert calls == []
    monkeypatch.setattr(server, "CLIENT_WAIT_S", 0.5)
    assert server.main(_argv(game), owner="", parts=parts(trace_lock=lambda: StandInTraceLock(
        tracelock.TAKEN, calls))) == protocol.EXIT_NO_CLIENT          # its pipe was made and waited on
    assert calls == ["acquire", "release"]


def test_the_relays_trace_lock_is_owned_by_administrators_as_its_pipe_is():
    assert server.Parts().trace_lock().owner == tracelock.ADMINISTRATORS == client.ADMINISTRATORS


def test_only_an_elevated_relay_can_own_its_pipe(game):
    """The relay's pipe is owned by Administrators (O:BA), which Windows refuses an unelevated token (error 1307):
    the recorder takes that owner as proof the relay runs elevated. Unelevated (the laptop), the real relay gives up
    before any pipe exists; elevated (CI's runner), its pipe exists and the recorder sees Administrators own it."""
    if not elevation.is_elevated():
        assert server.main(_argv(game), parts=parts()) == protocol.EXIT_PIPE
        return
    relay = InProcessRelay(parts(), owner=server.OWNER)
    link = launch(game, relay, owner=client.ADMINISTRATORS)
    assert link._client.owner_sid() == client.ADMINISTRATORS
    link.close()


def test_someone_else_on_the_pipe_is_let_go_and_the_recorder_still_gets_in(game):
    argv = _argv(game)
    name = argv[argv.index("--pipe") + 1]
    done: dict = {}
    t = threading.Thread(target=lambda: done.setdefault("code", server.main(argv, owner="", parts=parts())),
                         daemon=True)
    t.start()
    intruder = subprocess.run(
        [BASE_PY, "-c", "import sys\nfrom lagdoctor.winapi import pipe\nc = pipe.connect(sys.argv[1], 20)\n"
                        "try:\n    c.read(10)\n    print('read')\nexcept OSError as e:\n    print(e.winerror)\n", name],
        capture_output=True, text=True, timeout=60, cwd=str(Path(__file__).resolve().parents[1]))
    assert intruder.stdout.strip() in {str(c) for c in pipe.PEER_GONE}, intruder
    c = pipe.connect(name, 10, max_message=protocol.MAX_MESSAGE)        # the recorder, this process
    try:
        hello = protocol.from_relay(c.read(10))
        assert hello["t"] == "hello" and hello["pid"] == os.getpid()
        c.write(protocol.stop(), 5)
        assert protocol.from_relay(c.read(10))["t"] == "end"
    finally:
        c.close()
    t.join(15)
    assert done["code"] == protocol.EXIT_OK


@pytest.mark.parametrize("message", [b'{"t":"start","origin":1,"args":["--process_id","4"]}', b'{"t":"frames"}',
                                     b"not json", b'{"t":"start","origin":NaN}', b'{"t":"game","game":[5,1]}'])
def test_the_relay_takes_nothing_but_start_and_stop(game, message):
    relay = InProcessRelay(parts())
    link = launch(game, relay)
    link._client.write(message, 5)
    relay.thread.join(15)
    assert relay.code == protocol.EXIT_PROTOCOL
    _until(lambda: link.ended.is_set(), 5, "the end")
    assert link.relay_error and "does not take" in link.relay_error
    link.close()


def test_a_second_start_breaks_the_protocol(game):
    relay = InProcessRelay(parts())
    link = launch(game, relay)
    link._client.write(protocol.start(clock.now()), 5)
    link._client.write(protocol.start(clock.now()), 5)
    relay.thread.join(15)
    assert relay.code == protocol.EXIT_PROTOCOL and "second start" in (link.relay_error or "")
    link.close()


# ------------------------------------------------------------------------------ the recorder's checks ---

def test_launch_refuses_a_pipe_its_relay_did_not_make(game):
    relay = InProcessRelay(parts())
    relay.pid = os.getpid() + 4                               # the relay it started is not who answers
    with pytest.raises(client.RelayFailed, match="another program answered"):
        launch(game, relay)


def test_launch_refuses_a_pipe_not_owned_by_the_expected_owner(game):
    relay = InProcessRelay(parts())
    with pytest.raises(client.RelayFailed, match="not owned by Administrators"):
        launch(game, relay, owner="S-1-5-18")                # an owner no pipe here can have


def test_launch_refuses_a_relay_that_greets_with_another_game(game):
    other = subprocess.Popen([BASE_PY, "-I", "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
    try:
        snap = ntquery.Snapshotter()
        _until(lambda: other.pid in snap.snapshot())
        ct = snap.snapshot()[other.pid].create_time

        def swap_game(args):
            args = list(args)
            args[args.index("--game") + 1] = f"{other.pid}:{ct}"
            return args
        relay = InProcessRelay(parts(), rewrite=swap_game)
        with pytest.raises(client.RelayFailed, match="did not greet"):
            launch(game, relay)
    finally:
        other.stdin.close()
        other.wait(10)


def test_a_relay_that_ends_before_it_answers_is_reported_with_its_code(game):
    relay = InProcessRelay(parts(snapshot=snapshot_with(_fake_proc(game["pid"], 99, game["create_time"]))))
    with pytest.raises(client.RelayFailed) as info:
        launch(game, relay)
    assert info.value.code == protocol.EXIT_TARGET


def test_a_declined_prompt_reaches_the_caller(game):
    def declined(argv, cwd):
        raise PermissionError(13, "the administrator prompt was declined", None, 1223)
    me = _me()
    with pytest.raises(PermissionError):
        client.launch(game, presentmon_exe=None, frames=False, network=True, recorder=(me.pid, me.create_time),
                      sid=elevation.token_user_sid(), launcher=declined)


def test_the_relays_command_line_is_what_its_parser_takes_and_never_relative(tmp_path):
    exe = tmp_path / "PresentMon-2.6.0-x64.exe"
    argv = client.relay_argv(protocol.pipe_name(), (40, 300), "S-1-5-21-1-2-3-1001", (80, 430), presentmon_exe=exe,
                             frames=True, network=True)
    assert argv[0] == str(Path(sys.base_exec_prefix) / "pythonw.exe")
    assert argv[1:6] == ["-B", "-E", "-s", "-m", "lagdoctor.relay"] and all(isinstance(a, str) and a for a in argv)
    cfg = server.parse_args(argv[6:])
    assert (cfg.recorder, cfg.game, cfg.frames, cfg.network, cfg.presentmon_exe) == ((40, 300), (80, 430), True, True,
                                                                                    exe)
    assert "--presentmon-exe" not in client.relay_argv(protocol.pipe_name(), (40, 300), "S-1-5-21-1-2-3-1001",
                                                       (80, 430), presentmon_exe=None, frames=False, network=True)
    with pytest.raises(ValueError):
        client.relay_argv(protocol.pipe_name(), (40, 300), "S-1-5-21-1-2-3-1001", (80, 430),
                          presentmon_exe=Path("rel") / "pm.exe", frames=True, network=False)


def test_accept_target_literal_points():
    procs = {80: _fake_proc(80, 1, 430, "game.exe"), 90: _fake_proc(90, 2, 440, "other.exe")}
    assert tgt.accept_target(procs, 80, 430, session=1, approved=(80, 430)) is None
    assert "no longer running" in tgt.accept_target(procs, 80, 431, session=1, approved=(80, 431))
    assert "no longer running" in tgt.accept_target(procs, 81, 430, session=1, approved=(81, 430))
    assert "another Windows session" in tgt.accept_target(procs, 90, 440, session=1, approved=(90, 440))
    assert "not the game the UAC prompt approved" in tgt.accept_target(procs, 80, 430, session=1, approved=(80, 431))
    with pytest.raises(ValueError):
        tgt.accept_target(procs, 0, 430, session=1, approved=(0, 430))


def test_the_relay_refuses_a_recorder_in_another_session_than_its_own(game, monkeypatch):
    monkeypatch.setattr(pipe, "PipeServer", lambda *a, **k: pytest.fail("a pipe was made"))
    me = _me()
    relay_elsewhere = _fake_proc(0xFFFFFF10, me.session_id + 7)               # the relay, in another session ...
    game_there = _fake_proc(game["pid"], me.session_id + 7, game["create_time"])   # ... with the game beside it
    p = parts(snapshot=snapshot_with(relay_elsewhere, game_there))
    p.me = relay_elsewhere.pid
    assert server.main(_argv(game), owner="", parts=p) == protocol.EXIT_TARGET    # the recorder is not there


class FakeServer:
    """A pipe server whose clients are scripted: (pid, session) each."""

    def __init__(self, clients):
        self.clients, self.current, self.disconnects = list(clients), None, 0

    def wait_client(self, timeout_s):
        if not self.clients:
            return False
        self.current = self.clients.pop(0)
        return True

    def client_pid(self):
        return self.current[0]

    def client_session(self):
        return self.current[1]

    def disconnect(self):
        self.disconnects += 1


def test_the_recorders_pid_in_another_session_is_let_go_too():
    cfg = server.Config(protocol.pipe_name(), (40, 300), "S-1-5-21-1-2-3-1001", (80, 430), False, True, None)
    s = FakeServer([(40, 9), (41, 1), (40, 1)])
    assert server.wait_for_recorder(s, cfg, session=1, wait_s=5) is True and s.disconnects == 2
    assert server.wait_for_recorder(FakeServer([(40, 9)]), cfg, session=1, wait_s=5) is False


def test_launch_requires_administrators_as_the_owner_unless_told_otherwise(game):
    assert client.ADMINISTRATORS == "S-1-5-32-544"
    relay = InProcessRelay(parts())                          # a pipe with this token's default owner
    me = _me()
    kw = dict(presentmon_exe=None, frames=False, network=True, recorder=(me.pid, me.create_time),
              sid=elevation.token_user_sid(), launcher=relay)
    if _default_owner() == client.ADMINISTRATORS:            # elevated (CI): an elevated token's default owner
        client.launch(game, **kw).close()
        return
    with pytest.raises(client.RelayFailed, match="not owned by Administrators"):
        client.launch(game, **kw)


def test_a_command_the_relay_would_refuse_is_refused_before_any_prompt():
    for sid in ("S-1-5-21 1001", "not a sid"):
        with pytest.raises(ValueError):
            client.relay_argv(protocol.pipe_name(), (40, 300), sid, (80, 430), presentmon_exe=None, frames=False,
                              network=True)
    with pytest.raises(ValueError):                           # the recorder is never the game
        client.relay_argv(protocol.pipe_name(), (40, 300), "S-1-5-21-1-2-3-1001", (40, 300), presentmon_exe=None,
                          frames=False, network=True)


def test_relayed_rows_of_another_process_are_refused_and_counted():
    link = type("Link", (), {"pid": 7, "start": lambda self, o: None, "stop": lambda self: None})()
    frames = client.RelayedFrames(link, 4242)
    row = {"ProcessID": 4242, "SwapChainAddress": 1, "TimeInQPC": clock.qpc_now(), "MsBetweenPresents": 16.0}
    frames.feed([row, dict(row, ProcessID=4243), dict(row, TimeInQPC=row["TimeInQPC"] + 1)])
    r = frames.result()
    assert len(r["columns"][F.T]) == 2 and r["presentmon"]["relay"]["rows_refused"] == 1


def test_a_forwarding_presentmon_source_passes_rows_on_and_keeps_none():
    got: list = []
    src = pm.PresentMonSource(4242, forward=got.append)
    row = {"ProcessID": 4242, "SwapChainAddress": 1, "TimeInQPC": clock.qpc_now(), "MsBetweenPresents": 16.0}
    for i in range(3):
        src._add(dict(row, TimeInQPC=row["TimeInQPC"] + i))
    src._add(dict(row, ProcessID=99))                         # another process's row: neither passed on nor counted
    r = src.result()
    assert [g["ProcessID"] for g in got] == [4242] * 3 and r["presentmon"]["rows"] == 3
    assert not any(r["columns"].get(F.T) or []) and r["swap_chains"]["0x1"]["frames"] == 3


def test_a_relay_that_ends_before_the_recording_starts_ends_its_collectors_with_why(game, monkeypatch):
    monkeypatch.setattr(server, "START_WAIT_S", 0.3)
    relay = InProcessRelay(parts())
    link = launch(game, relay, frames=True, network=True, exe=Path(sys.executable).resolve())
    frames = link.frames_collector(game["pid"])
    tracker = nf.FlowTracker({game["pid"]}, selftest=False, local_addrs=lambda: {LOCAL_IP}, relay=link)
    _until(lambda: link.ended.is_set(), 10, "the relay to give up")
    t0 = time.monotonic()
    origin = clock.now()
    frames.start(origin)
    tracker.start(origin)
    _until(lambda: frames.ended, 5, "frames to end")
    tracker.stop()
    frames.stop()
    link.close()
    assert time.monotonic() - t0 < 5                       # never the 25 s stop deadline
    st = frames.status()
    assert st.state == "failed" and "the recording did not start in time" in st.detail, st
    assert tracker.status().state == "failed" and "the relay ended" in tracker.status().detail
    assert relay.code == protocol.EXIT_NO_CLIENT


def test_the_relays_interpreter_is_the_pythonw_beside_the_base_interpreter():
    # The relay runs with no console window; a venv's own pythonw.exe would be a launcher whose pid is not the relay's.
    exe = client.relay_python()
    assert exe.name == "pythonw.exe" and exe.is_file(), exe
    assert exe.parent == Path(sys.base_exec_prefix) and ".venv" not in exe.parts


def _child_of(p: ntquery.ProcInfo, ppid: int) -> ntquery.ProcInfo:
    import dataclasses
    return dataclasses.replace(p, ppid=ppid)


class LiveEtw(FakeEtw):
    """An ETW stand-in whose events come when the test says."""

    def emit(self, eid: int, data: bytes) -> None:
        self.on_event(nf.etw.EtwEvent(clock.qpc_now(), nf.KERNEL_NETWORK, eid, 0, 0, 4, 0, 0, 4, 0, data))


@pytest.mark.parametrize("who", ["another session's program", "another program of this session",
                                 "a child of the game in another session"])
def test_a_game_pid_windows_gave_to_another_program_never_carries_its_events(game, spy_writes, monkeypatch, who):
    # The game's processes are kept by identity, in the recorder's session: a number Windows gave to another program
    # is someone else's, and a process in another session naming the game as its parent is never the game's.
    monkeypatch.setattr(server, "STATUS_EVERY_S", 0.1)
    me = _me()
    swapped: dict[int, ntquery.ProcInfo] = {}
    real = snapshot_with()

    def snapshot():
        procs = real()
        procs.update(swapped)
        return procs
    made: list[LiveEtw] = []
    relay = InProcessRelay(parts(snapshot=snapshot, etw=lambda on_event: made.append(LiveEtw(on_event, [])) or made[-1]))
    link = launch(game, relay, network=True)
    tracker = nf.FlowTracker({game["pid"]}, selftest=False, local_addrs=lambda: {LOCAL_IP}, relay=link)

    def forwarded():
        return [struct.unpack_from("<II", d, 0)[1] for m in spy_writes if m["t"] == "events" for _, _, _, d in m["ev"]]
    try:
        tracker.start(clock.now())
        _until(lambda: made and made[0].running, 10, "the stand-in session")
        made[0].emit(42, payload(42, game["pid"], 100, SERVER_IP, LOCAL_IP, 27015, 50000))
        _until(lambda: forwarded() == [100], 10, "the game's event")
        whose = game["pid"]
        if who.startswith("a child"):
            whose = 0xFFFFFF20
            child = _fake_proc(whose, me.session_id + 5, game["create_time"] + 10, "child.exe")
            swapped[whose] = _child_of(child, game["pid"])      # names the game as its parent, started after it
        else:
            session = me.session_id + 5 if who.startswith("another session") else me.session_id
            swapped[whose] = _fake_proc(whose, session, game["create_time"] + 1, "someone.exe")
        time.sleep(0.5)                                   # a few of the relay's once-a-'second' refreshes (0.1 s here)
        made[0].emit(42, payload(42, whose, 999, SERVER_IP, LOCAL_IP, 27015, 50000))
        time.sleep(0.5)
        tracker.stop()
    finally:
        link.close()
    assert forwarded() == [100], forwarded()              # the 999-byte send was someone else's


def test_presentmon_never_starts_on_a_game_that_left_while_the_recording_got_ready(game):
    me = _me()
    swapped: dict[int, ntquery.ProcInfo] = {}
    real = snapshot_with()
    started: list = []

    def snapshot():
        return {**real(), **swapped}

    def frames(cfg, forward, alive):
        started.append(cfg)
        return FakeFrames(cfg, forward, alive)
    relay = InProcessRelay(parts(snapshot=snapshot, frames=frames))
    link = launch(game, relay, frames=True, network=False, exe=Path(sys.executable).resolve())
    rec = link.frames_collector(game["pid"])
    # Before `start`, Windows gives the game's number to another program (the game exited meanwhile).
    swapped[game["pid"]] = _fake_proc(game["pid"], me.session_id, game["create_time"] + 1, "someone.exe")
    rec.start(clock.now())
    _until(lambda: rec.ended, 10, "frames to end")
    rec.stop()
    link.close()
    st = rec.status()
    assert started == [] and st.state == "failed" and "PresentMon was not started" in st.detail, st
    assert "no longer running" in st.detail


def test_a_recorder_whose_pid_changed_hands_ends_the_recording_and_its_events_stay_in(game, spy_writes, monkeypatch):
    # A recorder can pass its pipe on to another process and exit: its number then names someone else.
    monkeypatch.setattr(server, "STATUS_EVERY_S", 0.1)
    me = _me()
    swapped: dict[int, ntquery.ProcInfo] = {}
    real = snapshot_with()
    made: list = []
    stops: list = []

    def snapshot():
        return {**real(), **swapped}
    relay = InProcessRelay(parts(snapshot=snapshot, etw=lambda on_event: made.append(LiveEtw(on_event, [], stops))
                                 or made[-1]))
    link = launch(game, relay, network=True)
    link.start(clock.now())
    _until(lambda: made and made[0].running, 10, "the stand-in session")
    swapped[me.pid] = _fake_proc(me.pid, me.session_id + 5, me.create_time + 1, "someone.exe")
    relay.thread.join(10)
    made[0].emit(42, payload(42, me.pid, 999, SERVER_IP, LOCAL_IP, 53, 50000))      # too late to leave: never sent
    assert not relay.thread.is_alive() and relay.code == protocol.EXIT_OK and stops == ["net"]
    assert not [m for m in spy_writes if m["t"] == "events"]
    link.close()


def test_the_once_a_second_refresh_goes_on_while_a_slow_recorder_holds_up_every_write(game, monkeypatch):
    monkeypatch.setattr(server, "STATUS_EVERY_S", 0.1)
    real_write = pipe.PipeServer.write
    slow = threading.Event()

    def write(self, data, timeout_s):
        if slow.is_set():
            time.sleep(0.6)                                # a recorder that reads one message now and then
        return real_write(self, data, timeout_s)
    monkeypatch.setattr(pipe.PipeServer, "write", write)
    snaps: list[float] = []
    real = snapshot_with()

    def snapshot():
        snaps.append(time.monotonic())
        return real()
    relay = InProcessRelay(parts(snapshot=snapshot))
    link = launch(game, relay, network=True)
    link.start(clock.now())
    time.sleep(0.5)
    slow.set()
    t0 = time.monotonic()
    time.sleep(2.0)
    during = [s for s in snaps if t0 <= s <= t0 + 2.0]
    slow.clear()
    link.close()
    relay.thread.join(15)
    assert len(during) >= 10, len(during)              # about every 0.1 s here, though each write took 0.6 s


class ScriptedPipe:
    """A pipe whose reads give these messages, then the other end hangs up."""

    def __init__(self, messages):
        self.messages = list(messages)

    def read(self, timeout_s):
        if self.messages:
            return self.messages.pop(0)
        raise OSError(109, "The pipe has been ended")


def _queued(relay: server.Relay) -> list[str]:
    out = []
    while not relay._commands.empty():
        out.append(relay._commands.get_nowait()["t"])
    return out


def test_the_relay_takes_start_then_stop_and_nothing_after_whatever_the_recorder_sends(game):
    cfg = server.parse_args(_argv(game))
    flood = [protocol.start(1.0), protocol.stop()] + [protocol.stop()] * 1000 + [protocol.start(2.0)]
    r = server.Relay(cfg, ScriptedPipe(flood), 1, parts())
    r._read_loop()
    assert _queued(r) == ["start", "stop"] and r._gone.is_set()          # still read to the end, to see the hang-up
    r = server.Relay(cfg, ScriptedPipe([protocol.start(1.0), protocol.start(2.0), protocol.stop()]), 1, parts())
    r._read_loop()
    assert _queued(r) == ["start", "broken"] and r._broke == "a second start"


_B = chr(92)                                         # one backslash, never typed (tool layers halve or eat them)


@pytest.mark.parametrize("exe", [
    _B * 2 + "server" + _B + "share" + _B + "PresentMon.exe",              # UNC: a share would get the admin's logon
    _B * 2 + "?" + _B + "C:" + _B + "pm" + _B + "PresentMon.exe",          # a device path
    _B * 2 + "." + _B + "C:" + _B + "pm.exe",
    "C:pm.exe",                                                            # relative to C:'s current folder
    "C:/pm/PresentMon.exe",
    "C:" + _B * 2 + "pm.exe"])
def test_presentmon_is_taken_only_from_a_plain_local_drive_path(game, exe, monkeypatch):
    assert len(exe.split(_B)) > 1 or "/" in exe or exe == "C:pm.exe"
    monkeypatch.setattr(pipe, "PipeServer", lambda *a, **k: pytest.fail("a pipe was made"))
    argv = _argv(game) + ["--frames", "--presentmon-exe", exe]
    assert server.main(argv, owner="", parts=parts()) == protocol.EXIT_BAD_ARGS, exe


def test_a_presentmon_on_a_network_drive_is_never_opened(monkeypatch):
    from lagdoctor import setup
    from lagdoctor.winapi import storage
    monkeypatch.setattr(storage, "drive_type", lambda root: 4)                 # Z: mapped to a share
    monkeypatch.setattr(setup, "verify", lambda exe: pytest.fail("the file was opened"))
    with pytest.raises(OSError, match="not on a local drive"):
        server._verify(Path("Z:\\") / "pm" / "PresentMon.exe")
    seen = []
    monkeypatch.setattr(storage, "drive_type", lambda root: seen.append(root) or storage.DRIVE_FIXED)
    monkeypatch.setattr(setup, "verify", lambda exe: seen.append("verified"))
    server._verify(Path("C:\\") / "pm" / "PresentMon.exe")
    assert seen == ["C:\\", "verified"]


def test_a_relays_stop_never_reads_as_its_network_trace_dying_by_itself(game):
    # The frames' stop stops the relay's ETW session too; the tracker's next consumer check then sees it ended.
    relay = InProcessRelay(parts(etw=lambda on_event: FakeEtw(on_event, [])))
    link = launch(game, relay, frames=True, network=True, exe=Path(sys.executable).resolve())
    frames = link.frames_collector(game["pid"])
    tracker = nf.FlowTracker({game["pid"]}, selftest=False, local_addrs=lambda: {LOCAL_IP}, relay=link)
    try:
        origin = clock.now()
        frames.start(origin)
        tracker.start(origin)
        _until(lambda: frames._rows == 5, 10, "the rows")
        frames.stop()
        time.sleep(1.3)                                    # the tracker checks its session's consumer every second
        tracker.stop()
    finally:
        link.close()
    st = tracker.status()
    assert st.state == "ok" and "stopped by itself" not in st.detail, st
    assert tracker.result()["health"]["consumer_stopped_t"] is None


def test_a_tracker_that_fails_leaves_frame_timing_running_in_the_relay(game, monkeypatch):
    stops: list[str] = []
    relay = InProcessRelay(parts(frames=lambda cfg, fw, alive: FakeFrames(cfg, fw, alive, stops=stops),
                                 etw=lambda on_event: FakeEtw(on_event, [], stops)))
    link = launch(game, relay, frames=True, network=True, exe=Path(sys.executable).resolve())
    frames = link.frames_collector(game["pid"])
    tracker = nf.FlowTracker({game["pid"]}, selftest=False, local_addrs=lambda: {LOCAL_IP}, relay=link)

    def broken():
        raise RuntimeError("a bug in the tracker")
    monkeypatch.setattr(tracker, "_publish", broken)
    try:
        origin = clock.now()
        frames.start(origin)
        tracker.start(origin)
        _until(lambda: tracker.status().state == "failed", 10, "the tracker to fail")
        time.sleep(0.5)
        assert stops == [] and not frames.ended and relay.thread.is_alive()   # frame timing goes on
        frames.stop()
        tracker.stop()
    finally:
        link.close()
    assert sorted(stops) == ["frames", "net"] and frames.status().state == "ok", frames.status()


class SlowStatusFrames(FakeFrames):
    """A frame source whose status is read late, and that crashes while a read is under way."""

    def __init__(self, cfg, forward, alive, reading, crash):
        super().__init__(cfg, forward, alive)
        self.reading, self.crash = reading, crash

    def status(self):
        st = super().status()
        self.reading.set()
        time.sleep(0.3)
        return st

    def run(self):
        self.set_status("ok", "PresentMon runs", tool=pm.TOOL)
        self.crash.wait(10)
        self.set_status("failed", "PresentMon crashed", tool=pm.TOOL)


def test_the_status_sent_as_final_is_the_last_one_presentmon_had(game, monkeypatch):
    monkeypatch.setattr(server, "STATUS_EVERY_S", 0.2)
    reading, crash = threading.Event(), threading.Event()
    relay = InProcessRelay(parts(frames=lambda cfg, fw, alive: SlowStatusFrames(cfg, fw, alive, reading, crash)))
    link = launch(game, relay, frames=True, network=False, exe=Path(sys.executable).resolve())
    frames = link.frames_collector(game["pid"])
    frames.start(clock.now())
    reading.clear()
    _until(lambda: reading.is_set(), 10, "a status read")
    crash.set()                                           # it settles and ends while that read is under way
    _until(lambda: frames.ended, 10, "frames to end")
    frames.stop()
    link.close()
    assert (frames.status().state, frames.status().detail) == ("failed", "PresentMon crashed")


def test_frames_give_up_on_a_silent_relay_before_the_session_stops_waiting():
    assert client.STOP_FRAMES_S + 1.0 < sess.STOP_TIMEOUT_S["frames"]


def test_a_closed_handle_is_no_exit_code_and_the_reader_is_done_before_the_handle_closes():
    class Closed:
        def wait(self, timeout_s=None):
            raise ValueError("the process handle was already closed")
    assert client._exit_code(Closed()) is None

    class Pipe:
        def __init__(self):
            self.closed = threading.Event()

        def read(self, timeout_s):
            self.closed.wait()
            raise OSError(995, "aborted")

        def close(self):
            self.closed.set()

    class Child:
        pid = 1

        def __init__(self):
            self.reader_alive_at_close = None
            self.waits = []

        def wait(self, timeout_s=None):
            self.waits.append(timeout_s)
            time.sleep(0.3)                               # a reader reading the exit code would be in here
            return None

        def close(self):
            self.reader_alive_at_close = link._reader.is_alive()
    child = Child()
    link = client.RelayLink(Pipe(), child, {"pid": os.getpid(), "frames": False, "network": True})
    link.close()
    assert child.reader_alive_at_close is False
    assert child.waits == [0.0]          # close never waits for the relay, nor does a reader the window hung up on


def test_network_messages_from_before_the_trackers_session_are_kept_for_it(game, spy_writes, monkeypatch):
    me = _me()
    relay = InProcessRelay(parts(etw=lambda on_event: FakeEtw(on_event, _net_script(game["pid"], me.pid))))
    link = launch(game, relay, frames=True, network=True, exe=Path(sys.executable).resolve())
    frames = link.frames_collector(game["pid"])
    origin = clock.now()
    frames.start(origin)                                  # the relay starts, and sends events nobody takes yet
    _until(lambda: any(m["t"] == "events" for m in spy_writes), 10, "the first events")
    _until(lambda: link._early, 10, "them to arrive")
    tracker = nf.FlowTracker({game["pid"]}, selftest=False, local_addrs=lambda: {LOCAL_IP}, relay=link)
    try:
        tracker.start(origin)
        time.sleep(0.5)
        frames.stop()
        tracker.stop()
    finally:
        link.close()
    talkers = {t["pid"]: (t["rx_bytes"], t["tx_bytes"]) for t in tracker.result()["talkers"]}
    assert talkers[game["pid"]] == (30 * 200 + 500, 30 * 100) and tracker.result()["health"]["early_dropped"] == 0


def test_network_messages_from_before_the_trackers_session_are_bounded(game, spy_writes, monkeypatch):
    monkeypatch.setattr(client, "MAX_EARLY_EVENTS", 10)
    me = _me()
    relay = InProcessRelay(parts(etw=lambda on_event: FakeEtw(on_event, _net_script(game["pid"], me.pid))))
    link = launch(game, relay, frames=True, network=True, exe=Path(sys.executable).resolve())
    frames = link.frames_collector(game["pid"])
    origin = clock.now()
    frames.start(origin)
    _until(lambda: link._early_dropped > 0, 10, "the early buffer to fill")
    tracker = nf.FlowTracker({game["pid"]}, selftest=False, local_addrs=lambda: {LOCAL_IP}, relay=link)
    try:
        tracker.start(origin)
        time.sleep(0.3)
        frames.stop()
        tracker.stop()
    finally:
        link.close()
    assert link._early_events <= 10 and tracker.result()["health"]["early_dropped"] == link._early_dropped > 0


class ScriptedRelay:
    """A stand-in relay that says `script(cfg)` (raw messages) once the recorder connects; also launch_elevated's
    stand-in (its 'process' is this one)."""

    def __init__(self, script):
        self.script = script
        self.pid = os.getpid()
        self.done = threading.Event()

    def __call__(self, argv, cwd):
        cfg = server.parse_args(argv[6:])
        self.server = pipe.PipeServer(cfg.pipe, pipe.client_dacl([cfg.sid]), max_message=protocol.MAX_MESSAGE)
        threading.Thread(target=self._serve, args=(cfg,), daemon=True).start()
        return self

    def _serve(self, cfg):
        try:
            if self.server.wait_client(10):
                for m in self.script(cfg):
                    self.server.write(m, 5)
                self.done.wait(10)
        finally:
            self.server.close()

    def wait(self, timeout_s=None):
        return None

    def close(self):
        self.done.set()


def _hello(cfg, **over):
    msg = {"t": "hello", "v": protocol.VERSION, "pid": os.getpid(), "game": None if cfg.game is None else list(cfg.game),
           "folders": None if cfg.folders is None else list(cfg.folders), "frames": cfg.frames, "network": cfg.network}
    msg.update(over)
    return json.dumps(msg).encode()


@pytest.mark.parametrize("over", [{"v": protocol.VERSION + 1}, {"frames": True}, {"network": False},
                                  {"pid": os.getpid() + 4}])
def test_launch_refuses_a_relay_whose_hello_is_not_the_one_it_launched(game, over):
    with pytest.raises(client.RelayFailed, match="did not greet"):
        launch(game, ScriptedRelay(lambda cfg: [_hello(cfg, **over)]))


def test_a_second_hello_breaks_the_link(game):
    relay = ScriptedRelay(lambda cfg: [_hello(cfg), _hello(cfg)])
    link = launch(game, relay)
    _until(lambda: link.ended.is_set(), 10, "the link to break")
    assert "a second hello" in (link.why or "")
    link.close()


def test_the_relays_pipe_lets_in_the_recorders_account_alone(game):
    relay = InProcessRelay(parts())
    link = launch(game, relay)
    try:
        got = link._client.descriptor()
    finally:
        link.close(wait_s=5)
    sid = elevation.token_user_sid()
    with pipe.PipeServer(f"lagdoctor-dacl-{secrets.token_hex(8)}", pipe.client_dacl([sid])) as want:
        assert got == want.descriptor()                   # Windows' own spelling of one ACE, for this account


def test_a_relay_that_dies_as_the_window_closes_keeps_its_handle_until_the_reader_is_done():
    # The relay's end goes first: the reader is reading its exit code through the process handle when close() comes.
    class Pipe:
        def read(self, timeout_s):
            raise OSError(109, "The pipe has been ended")

        def close(self):
            pass

    class Child:
        pid = 1

        def __init__(self):
            self.waits, self.in_wait = [], threading.Event()
            self.reader_alive_at_close = None

        def wait(self, timeout_s=None):
            self.waits.append(timeout_s)
            self.in_wait.set()
            if timeout_s:                    # the reader's wait on the relay's exit; close()'s own poll (0) returns
                time.sleep(0.5)              # at once, so only the join can outlast the reader (a race otherwise)
            return 7

        def close(self):
            self.reader_alive_at_close = link._reader.is_alive()
    child = Child()
    link = client.RelayLink(Pipe(), child, {"pid": os.getpid(), "frames": False, "network": True})
    _until(child.in_wait.is_set, 5, "the reader to wait on the relay's exit")
    link.close()
    assert child.reader_alive_at_close is False and child.waits[0] == 1.0
    assert link.why == "the relay ended (exit code 7)"


# ------------------------------------------------------------------- Play: a relay approved for a folder ---
# The UAC prompt comes before the store starts the game, so it approves the game's install folder; the recorder then
# names the game the folder wait found, and the relay judges it itself (DECISIONS 2026-11-17, M6d). The folder, its
# programs and the games are made up: nothing is read from the folder, and a game is a made-up process whose program
# path the relay's (and the recorder's) image read gives.

FOLDER = "C:" + _B + "Games" + _B + "Lantern Drift"
PROGRAM = FOLDER + _B + "Lantern" + _B + "Binaries" + _B + "Win64" + _B + "lantern_.exe"
ELSEWHERE = "D:" + _B + "Other" + _B + "lantern_.exe"
SECOND = 10_000_000                                   # one second in create-time units
PLAY, PLAY2, RELAY_PID = 0xFFFFFF30, 0xFFFFFF70, 0xFFFFFF20    # made up; the refusal cases use PLAY + 4 * i


def _play_argv(folders=(FOLDER,), wait_s="30", frames=True, **over) -> list[str]:
    me = _me()
    a = {"--pipe": protocol.pipe_name(), "--recorder": f"{me.pid}:{me.create_time}",
         "--sid": elevation.token_user_sid()}
    a.update(over)
    out = [x for k, v in a.items() if v is not None for x in (k, v)]
    for form in folders:
        out += ["--folder", form]
    out += ["--wait-s", wait_s] if wait_s is not None else []
    out += ["--frames", "--presentmon-exe", str(Path(sys.executable).resolve())] if frames else []
    return out + ["--network"]


def _played(pid: int, after_s: float, *, since: int, session: int | None = None, ppid: int = 4,
            name: str = "lantern_.exe") -> ntquery.ProcInfo:
    """A made-up process started `after_s` seconds after `since`, nobody's child unless `ppid` says so."""
    me = _me()
    return dataclasses.replace(_fake_proc(pid, me.session_id if session is None else session,
                                          since + int(after_s * SECOND), name), ppid=ppid)


class PlayRelay:
    """The relay approved for a folder (server.main, --folder) on a thread here, with this test as its recorder over a
    raw pipe: what the relay itself answers about each game named, with the recorder's own check left out. The relay's
    own process is made up (started after this one, in its session), so its create time, Play's `since`, is not the
    recorder's; `games` are made-up processes, `images` their program paths."""

    def __init__(self, games=(), images=None, *, wait_s="30", snapshot=None, image=None, argv=None):
        me = _me()
        self.relay = _fake_proc(RELAY_PID, me.session_id, me.create_time + 5 * SECOND, "pythonw.exe")
        self.since = self.relay.create_time
        self.images = dict(images or {})
        self.built: list = []
        games = [g(self.since) if callable(g) else g for g in games]
        self.parts = parts(snapshot=snapshot or snapshot_with(self.relay, *games), frames=self._frames,
                           etw=self._etw)
        self.parts.me = self.relay.pid
        self.parts.image = image or (lambda pid: self.images.get(pid))
        self.argv = argv or _play_argv(wait_s=wait_s)
        self.code = None
        self.thread = threading.Thread(target=self._run, name="play-relay", daemon=True)
        self.thread.start()
        self.c = pipe.connect(self.argv[self.argv.index("--pipe") + 1], 10, max_message=protocol.MAX_MESSAGE)
        self.hello = self.read()

    def _run(self):
        self.code = server.main(self.argv, owner="", parts=self.parts)

    def _frames(self, cfg, forward, alive):
        self.built.append(("frames", cfg.game))
        return FakeFrames(cfg, forward, alive)

    def _etw(self, on_event):
        self.built.append(("etw",))
        return FakeEtw(on_event, [])

    def send(self, data: bytes) -> None:
        self.c.write(data, 5)

    def read(self, timeout_s: float = 10) -> dict:
        return protocol.from_relay(self.c.read(timeout_s))

    def propose(self, pid: int, create_time: int) -> dict:
        self.send(protocol.game((pid, create_time)))
        return self.read()

    def to_end(self, timeout_s: float = 15) -> list[dict]:
        """Every message until `end`, which is the last."""
        got: list[dict] = []
        deadline = time.monotonic() + timeout_s
        while not got or got[-1]["t"] != "end":
            assert time.monotonic() < deadline, f"no end within {timeout_s} s: {got}"
            try:
                got.append(self.read(1))
            except TimeoutError:
                pass
        return got

    def close(self) -> int | None:
        self.c.close()
        self.thread.join(15)
        return self.code


def test_a_play_relay_greets_with_its_folder_and_records_the_game_it_accepted():
    r = PlayRelay([lambda since: _played(PLAY, 1, since=since)], {PLAY: PROGRAM})
    try:
        assert r.hello["game"] is None and r.hello["folders"] == (FOLDER,) and r.hello["pid"] == RELAY_PID
        ct = r.since + SECOND
        assert r.propose(PLAY, ct) == {"t": "game", "game": (PLAY, ct)}
        assert r.built == []                                     # nothing runs before the recording starts
        r.send(protocol.start(clock.now()))
        _until(lambda: ("frames", (PLAY, ct)) in r.built, 10, "PresentMon on the accepted game")
        r.send(protocol.stop())
        assert r.to_end()[-1]["t"] == "end"
    finally:
        assert r.close() == protocol.EXIT_OK


def test_a_play_relay_refuses_each_game_its_folder_rule_does_not_take_and_waits_on():
    me = _me()
    cases = [  # (the process, its program, what the refusal says)
        (lambda s: _played(PLAY, 1, since=s), ELSEWHERE, "is not in the game's folder"),
        (lambda s: _played(PLAY + 4, -1, since=s), PROGRAM, "started before the UAC prompt was approved"),
        (lambda s: _played(PLAY + 8, 1, since=s, ppid=RELAY_PID), PROGRAM, "one of lag-doctor's own processes"),
        (lambda s: _played(PLAY + 12, 1, since=s, ppid=me.pid), PROGRAM, "one of lag-doctor's own processes"),
        (lambda s: _played(PLAY + 16, 1, since=s, session=me.session_id + 9), PROGRAM, "another Windows session"),
        (lambda s: _played(PLAY + 20, 1, since=s, name="LANTER~1.EXE"),
         "C:" + _B + "GAMES~1" + _B + "LANTER~1" + _B + "lantern_.exe", "8.3 short names"),
    ]
    games = [c[0] for c in cases] + [lambda s: _played(PLAY2, 2, since=s)]
    images = {PLAY + 4 * i: c[1] for i, c in enumerate(cases)}
    r = PlayRelay(games, {**images, PLAY2: PROGRAM})
    try:
        for i, (_, _, says) in enumerate(cases):
            pid = PLAY + 4 * i
            ct = r.since + (-SECOND if i == 1 else SECOND)
            answer = r.propose(pid, ct)
            assert answer["t"] == "refused" and answer["game"] == (pid, ct) and says in answer["why"], (i, answer)
            assert answer["retry"] is False
        # The relay itself is refused too: it is lag-doctor's own before it is anything else.
        assert "one of lag-doctor's own" in r.propose(RELAY_PID, r.since)["why"]
        assert r.built == []                                     # nothing started for any of them
        assert r.propose(PLAY2, r.since + 2 * SECOND) == {"t": "game", "game": (PLAY2, r.since + 2 * SECOND)}
        r.send(protocol.stop())
        r.to_end()
    finally:
        assert r.close() == protocol.EXIT_OK
    assert r.built == []


def test_a_play_relays_refusal_shows_a_programs_name_as_plain_text():
    odd = "lantern" + chr(0x202E) + "exe.txt"                 # a name that turns the text after it around
    r = PlayRelay([lambda s: _played(PLAY, -1, since=s, name=odd)], {PLAY: PROGRAM})
    try:
        no = r.propose(PLAY, r.since - SECOND)
        assert "(lantern?exe.txt)" in no["why"] and chr(0x202E) not in no["why"], no
        r.send(protocol.stop())
        r.to_end()
    finally:
        assert r.close() == protocol.EXIT_OK


def test_a_play_relay_refuses_a_game_that_is_gone_and_one_it_could_not_read_and_may_be_asked_again():
    reads = {"left": 3}

    def image(pid):
        if pid == PLAY and reads["left"]:
            reads["left"] -= 1
            raise OSError(31, "A device attached to the system is not functioning")
        return PROGRAM
    r = PlayRelay([lambda s: _played(PLAY, 1, since=s)], image=image)
    try:
        ct = r.since + SECOND
        assert "no longer running" in r.propose(PLAY2, ct)["why"]
        no = r.propose(PLAY, ct)
        assert no["t"] == "refused" and no["retry"] is True and "could not be checked" in no["why"], no
        assert reads["left"] == 0 and r.propose(PLAY, ct)["t"] == "game"     # asked again, it reads and takes it
        r.send(protocol.stop())
        r.to_end()
    finally:
        assert r.close() == protocol.EXIT_OK


def test_a_play_relay_takes_nothing_once_the_recording_window_has_gone_and_ends():
    me = _me()
    gone = threading.Event()
    real = snapshot_with()

    def snapshot():
        procs = real()
        procs[RELAY_PID] = relay_proc
        procs[PLAY] = game
        if gone.is_set():
            procs.pop(me.pid, None)                  # the recorder's pipe handle lives on, in another process say
        return procs

    def image(pid):
        gone.set()                                   # between the relay's two snapshots: the second lacks it
        return PROGRAM
    relay_proc = _fake_proc(RELAY_PID, me.session_id, me.create_time + 5 * SECOND, "pythonw.exe")
    game = _played(PLAY, 1, since=relay_proc.create_time)
    r = PlayRelay(snapshot=snapshot, image=image)
    try:
        no = r.propose(PLAY, game.create_time)
        assert no["t"] == "refused" and no["why"] == "the recording window is no longer running", no
        said = r.to_end(10)                          # and the relay, looking for it once a second, ends
        assert any(m["t"] == "error" and "no longer running" in m["why"] for m in said), said
    finally:
        assert r.close() == protocol.EXIT_TARGET
    assert r.built == []


def test_a_play_relay_whose_recording_window_left_while_it_waited_for_the_game_ends():
    me = _me()
    gone = threading.Event()
    real = snapshot_with()

    def snapshot():
        procs = real()
        procs[RELAY_PID] = _fake_proc(RELAY_PID, me.session_id, me.create_time + 5 * SECOND, "pythonw.exe")
        if gone.is_set():
            procs[me.pid] = dataclasses.replace(procs[me.pid], create_time=me.create_time + 1)  # its number reused
        return procs
    r = PlayRelay(snapshot=snapshot)
    try:
        t0 = time.monotonic()
        gone.set()
        said = r.to_end(10)
        assert time.monotonic() - t0 < 5 and said[-2]["why"] == "the recording window is no longer running", said
    finally:
        assert r.close() == protocol.EXIT_TARGET


def test_a_play_relay_waits_for_its_game_as_long_as_the_recording_asked_and_no_longer(monkeypatch):
    monkeypatch.setattr(server, "GAME_SLACK_S", 0.3)
    r = PlayRelay(wait_s="0.5")
    try:
        t0 = time.monotonic()
        said = r.to_end(10)
        assert said[-2] == {"t": "error", "why": "no game the relay could record started in time"}, said
        assert 0.75 < time.monotonic() - t0 < 5             # the recorder's own wait and the slack
    finally:
        assert r.close() == protocol.EXIT_NO_CLIENT


def test_a_play_relays_wait_for_the_game_is_not_the_wait_for_the_start(monkeypatch):
    monkeypatch.setattr(server, "START_WAIT_S", 0.4)
    r = PlayRelay([lambda s: _played(PLAY, 1, since=s)], {PLAY: PROGRAM})
    try:
        time.sleep(1.0)                              # longer than START_WAIT_S: the game's wait is the recording's own
        assert r.propose(PLAY, r.since + SECOND)["t"] == "game"
        t0 = time.monotonic()
        said = r.to_end(10)                          # once a game is taken, START_WAIT_S is the time for `start`
        assert said[-2] == {"t": "error", "why": "the recording did not start in time"} and time.monotonic() - t0 < 5
    finally:
        assert r.close() == protocol.EXIT_NO_CLIENT
    assert r.built == []


def test_a_later_game_takes_the_place_of_the_one_before():
    images = {PLAY: PROGRAM, PLAY2: PROGRAM, PLAY + 4: ELSEWHERE}
    r = PlayRelay([lambda s: _played(PLAY, 1, since=s), lambda s: _played(PLAY2, 2, since=s),
                   lambda s: _played(PLAY + 4, 3, since=s)], images)
    try:
        assert r.propose(PLAY, r.since + SECOND)["t"] == "game"
        assert r.propose(PLAY2, r.since + 2 * SECOND)["t"] == "game"
        r.send(protocol.start(clock.now()))
        _until(lambda: any(b[0] == "frames" for b in r.built), 10, "PresentMon to start")
        assert [b for b in r.built if b[0] == "frames"] == [("frames", (PLAY2, r.since + 2 * SECOND))]
        r.send(protocol.stop())
        r.to_end()
    finally:
        assert r.close() == protocol.EXIT_OK
    # A refused one takes the place of an accepted one too: a start then records nothing.
    r = PlayRelay([lambda s: _played(PLAY, 1, since=s), lambda s: _played(PLAY + 4, 3, since=s)], images)
    try:
        assert r.propose(PLAY, r.since + SECOND)["t"] == "game"
        assert r.propose(PLAY + 4, r.since + 3 * SECOND)["t"] == "refused"
        r.send(protocol.start(clock.now()))
        said = r.to_end()
        assert any("before the relay had accepted a game" in m.get("why", "") for m in said), said
    finally:
        assert r.close() == protocol.EXIT_PROTOCOL
    assert r.built == []


@pytest.mark.parametrize("script, says", [
    (lambda since: [protocol.start(1.0)], "a start before any game"),
    (lambda since: [protocol.game((PLAY, since + SECOND)), protocol.start(1.0), protocol.game((PLAY, since + SECOND))],
     "a game after the start"),
    (lambda since: [protocol.game((PLAY, since + SECOND))] * (protocol.MAX_GAMES + 1), "more than 8 games"),
])
def test_a_play_relay_takes_its_messages_only_in_turn(script, says):
    r = PlayRelay([lambda s: _played(PLAY, 1, since=s)], {PLAY: PROGRAM})
    try:
        for m in script(r.since):
            r.send(m)
        said = r.to_end()
        assert any(m["t"] == "error" and says in m["why"] for m in said), said
    finally:
        assert r.close() == protocol.EXIT_PROTOCOL
    assert ("etw",) not in r.built or says == "a game after the start"


def test_a_play_relay_ends_on_a_stop_or_a_hang_up_before_any_game():
    r = PlayRelay()
    try:
        r.send(protocol.stop())
        assert r.to_end() == [{"t": "end"}]
    finally:
        assert r.close() == protocol.EXIT_OK and r.built == []
    r = PlayRelay()
    assert r.close() == protocol.EXIT_OK and r.built == []


def test_the_play_relays_reader_queues_games_only_before_the_start_and_at_most_max_games():
    cfg = server.parse_args(_play_argv())
    g = protocol.game((PLAY, 5))
    r = server.Relay(cfg, ScriptedPipe([g, g, protocol.start(1.0), g]), 1, parts())
    r._read_loop()
    assert _queued(r) == ["game", "game", "start", "broken"] and r._broke == "a game after the start"
    r = server.Relay(cfg, ScriptedPipe([protocol.start(1.0)]), 1, parts())
    r._read_loop()
    assert _queued(r) == ["broken"] and r._broke == "a start before any game"
    r = server.Relay(cfg, ScriptedPipe([g] * (protocol.MAX_GAMES + 1)), 1, parts())
    r._read_loop()
    assert _queued(r) == ["game"] * protocol.MAX_GAMES + ["broken"] and r._broke == "more than 8 games"
    r = server.Relay(cfg, ScriptedPipe([g, protocol.stop(), g, protocol.start(1.0)]), 1, parts())
    r._read_loop()
    assert _queued(r) == ["game", "stop"] and r._gone.is_set()          # after stop nothing more is taken


@pytest.mark.parametrize("over", [
    {"--game": "12:34"},                                                    # both modes
    {"--folder": None, "--game": "12:34"},                                 # a wait for a game already approved
    {"--wait-s": None}, {"--folder": None},                                 # a folder without its wait, and neither
    {"--wait-s": "0"}, {"--wait-s": "3601"}, {"--wait-s": "1e3"}, {"--wait-s": "-5"}, {"--wait-s": "300."},
    {"--folder": "C:" + _B}, {"--folder": _B * 2 + "server" + _B + "share" + _B + "x"}, {"--folder": "rel" + _B + "x"},
    {"--folder": "C:" + _B + "Windows" + _B + "Lantern"}, {"--folder": [FOLDER + str(i) for i in range(5)]},
])
def test_a_play_relay_takes_only_a_command_line_lag_doctor_builds(over, monkeypatch):
    monkeypatch.setattr(pipe, "PipeServer", lambda *a, **k: pytest.fail("a pipe was made"))
    folders = over.pop("--folder", (FOLDER,))
    folders = () if folders is None else [folders] if isinstance(folders, str) else folders
    argv = _play_argv(folders=folders, wait_s=over.pop("--wait-s", "30"), **over)
    assert server.main(argv, owner="", parts=parts()) == protocol.EXIT_BAD_ARGS, argv
    assert server.parse_args(_play_argv(wait_s="3600")).wait_s == 3600.0     # target.MAX_WAIT_S is the most


def test_a_play_relay_checks_its_recorder_before_any_pipe_exists(monkeypatch):
    monkeypatch.setattr(pipe, "PipeServer", lambda *a, **k: pytest.fail("a pipe was made"))
    me = _me()
    stale = _play_argv(**{"--recorder": f"{me.pid}:{me.create_time + 1}"})
    assert server.main(stale, owner="", parts=parts()) == protocol.EXIT_TARGET
    relay_elsewhere = _fake_proc(RELAY_PID, me.session_id + 7)
    p = parts(snapshot=snapshot_with(relay_elsewhere))
    p.me = relay_elsewhere.pid
    assert server.main(_play_argv(), owner="", parts=p) == protocol.EXIT_TARGET


def test_the_relays_command_line_for_a_folder_holds_its_checked_forms_and_survives_the_prompts_quoting():
    forms = ("c:/games/lantern drift", "D:" + _B + "Library" + _B + "Lantern Drift" + _B)
    argv = client.relay_argv(protocol.pipe_name(), (40, 300), "S-1-5-21-1-2-3-1001", folders=forms, wait_s=300,
                             presentmon_exe=None, frames=False, network=True)
    want = ("C:" + _B + "games" + _B + "lantern drift", "D:" + _B + "Library" + _B + "Lantern Drift")
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "--folder"] == list(want)
    assert argv[argv.index("--wait-s") + 1] == "300" and "--game" not in argv
    cfg = server.parse_args(argv[6:])
    assert (cfg.game, cfg.folders, cfg.wait_s) == (None, want, 300.0)
    for wait_s, written in [(1234.567, "1234.567"), (0.5, "0.5"), (300.0, "300"), (10.1, "10.1")]:
        longer = client.relay_argv(protocol.pipe_name(), (40, 300), "S-1-5-21-1-2-3-1001", folders=forms,
                                   wait_s=wait_s, presentmon_exe=None, frames=False, network=True)
        assert longer[longer.index("--wait-s") + 1] == written           # never rounded: the relay waits as long
        assert server.parse_args(longer[6:]).wait_s == wait_s
    # launch_elevated hands the prompt list2cmdline(argv[1:]); the relay reads it back as Windows splits it.
    assert _split_command_line(argv[0], subprocess.list2cmdline(argv[1:])) == argv
    for bad in ({}, {"game": (80, 430), "folders": forms, "wait_s": 30}, {"folders": forms},
                {"folders": forms, "wait_s": True}, {"folders": forms, "wait_s": 3601},
                {"folders": ("C:" + _B,), "wait_s": 30}):
        with pytest.raises(ValueError):
            client.relay_argv(protocol.pipe_name(), (40, 300), "S-1-5-21-1-2-3-1001", presentmon_exe=None,
                              frames=False, network=True, **bad)


def _split_command_line(program: str, params: str) -> list[str]:
    """How Windows splits the relay's command line (CommandLineToArgvW), program first."""
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    k32 = ctypes.WinDLL("kernel32")
    k32.LocalFree.argtypes = [ctypes.c_void_p]
    n = ctypes.c_int()
    argv = shell32.CommandLineToArgvW(f'"{program}" {params}', ctypes.byref(n))
    assert argv, f"CommandLineToArgvW failed: {ctypes.get_last_error()}"
    try:
        return [argv[i] for i in range(n.value)]
    finally:
        k32.LocalFree(ctypes.cast(argv, ctypes.c_void_p))


def test_the_relays_wait_for_a_game_leaves_room_for_the_recorders_own_steps():
    # Past the recorder's own wait (at most target.MAX_WAIT_S, which --wait-s may not pass): one round of the wait
    # past its deadline, the re-arm's hold and the recorder's own check of the game; and never long past them.
    # The recorder's wait ends where the relay's does less GAME_SLACK_S (watch: RelayLink.aim_by), then: a round of the
    # wait (its samples), the re-arm's hold and its last round, the recorder's own check (check_proposed's tries) and
    # the relay's answer.
    room = (tgt.DRAW_SAMPLE_S * (tgt.DRAW_SAMPLES + 1) + tgt.REARM_HOLD_S + tgt.DRAW_SAMPLE_S
            + tgt.IMAGE_TRIES * tgt.TRY_PAUSE_S + client.AIM_S)
    assert room <= server.GAME_SLACK_S <= server.GAME_SLACK_MAX_S <= 120.0
    assert client.GOODBYE_WAIT_S > server.GOODBYE_S


# ---- the recorder's side of Play ----------------------------------------------------------------------------

class PlayWorld:
    """A relay approved for a folder, in process (InProcessRelay: the relay is this process too, so its create time,
    Play's `since`, is this process's), and the made-up games the relay's and the recorder's snapshots and image reads
    both see unless a test gives the recorder its own."""

    def __init__(self, games=(), images=None, relay_image=None, *, frames=True):
        me = _me()
        self.since = me.create_time
        self.games = [g(self.since) if callable(g) else g for g in games]
        self.images = dict(images or {})
        self.snapshot = snapshot_with(*self.games)
        self.built: list = []
        p = parts(snapshot=self.snapshot, frames=self._frames, etw=lambda on_event: FakeEtw(on_event, []))
        p.image = relay_image or self.image
        self.relay = InProcessRelay(p)
        self.link = client.launch(None, folders=(FOLDER,), wait_s=30, presentmon_exe=Path(sys.executable).resolve()
                                  if frames else None, frames=frames, network=True,
                                  recorder=(me.pid, me.create_time), sid=elevation.token_user_sid(),
                                  launcher=self.relay, owner=_default_owner())

    def image(self, pid):
        return self.images.get(pid)

    def _frames(self, cfg, forward, alive):
        self.built.append(("frames", cfg.game))
        return FakeFrames(cfg, forward, alive)

    def aim(self, pid, after_s=1.0, **kw):
        kw.setdefault("snapshot", self.snapshot)
        kw.setdefault("image", self.image)
        return self.link.aim((pid, self.since + int(after_s * SECOND)), **kw)

    def close(self) -> int | None:
        self.link.close()
        self.relay.thread.join(15)
        return self.relay.code


def test_launch_for_a_folder_approves_the_forms_the_relay_will_use_and_knows_when_it_started():
    w = PlayWorld()
    try:
        assert w.relay.argv[w.relay.argv.index("--folder") + 1] == FOLDER
        assert w.link.folders == (FOLDER,) and w.link.game is None and w.link.create_time == w.since
        assert w.link.session == _me().session_id and w.link.recorder == (_me().pid, w.since)
        # The relay takes a game until the recorder's wait plus its slack: aim stops asking one answer's time before.
        assert 30 + server.GAME_SLACK_S - client.AIM_S - 5 < w.link.aim_by - time.monotonic() <= (
            30 + server.GAME_SLACK_S - client.AIM_S)
        # lag-doctor's own, as aim's check and the relay's take them, and as the folder wait passes over them.
        assert w.link.own() == ((_me().pid, w.since), (w.link.pid, w.link.create_time))
    finally:
        assert w.close() == protocol.EXIT_OK


def test_aim_names_the_game_the_relay_accepts_and_the_recording_records_it():
    w = PlayWorld([lambda s: _played(PLAY, 1, since=s)], {PLAY: PROGRAM})
    try:
        w.aim(PLAY)
        game = (PLAY, w.since + SECOND)
        assert w.link.game == game
        frames = w.link.frames_collector(PLAY)
        frames.start(clock.now())
        _until(lambda: frames._rows == 5, 10, "the accepted game's rows")
        assert w.built == [("frames", game)]
        frames.stop()
    finally:
        assert w.close() == protocol.EXIT_OK


def test_the_relay_and_the_recorder_pass_over_the_same_processes_of_lag_doctors_own(monkeypatch):
    w = PlayWorld([lambda s: _played(PLAY, 1, since=s)], {PLAY: PROGRAM})
    seen = []
    real = tgt.check_proposed
    monkeypatch.setattr(tgt, "check_proposed", lambda *a, **kw: seen.append(kw["own"]) or real(*a, **kw))
    try:
        w.aim(PLAY)
        assert seen == [w.link.own(), w.link.own()]                  # the recorder's check, then the relay's
    finally:
        assert w.close() == protocol.EXIT_OK


def test_aim_refuses_first_what_the_relay_would_refuse_and_sends_nothing(monkeypatch):
    w = PlayWorld([lambda s: _played(PLAY, 1, since=s), lambda s: _played(PLAY2, 2, since=s)],
                  {PLAY: ELSEWHERE, PLAY2: PROGRAM})
    sent: list = []
    real = w.link._client.write
    monkeypatch.setattr(w.link._client, "write", lambda data, t: sent.append(protocol.from_recorder(data)) or real(data, t))
    try:
        with pytest.raises(client.GameRefused) as no:
            w.aim(PLAY)
        assert no.value.sent is False and no.value.retry is False and "is not in the game's folder" in no.value.why
        assert sent == [] and not w.link.ended.is_set()          # the relay was never asked, and still waits
        w.aim(PLAY2, after_s=2)
        assert [m["t"] for m in sent] == ["game"] and w.link.game == (PLAY2, w.since + 2 * SECOND)
    finally:
        assert w.close() == protocol.EXIT_OK


def test_aim_hears_the_relays_own_no_and_the_relay_waits_on():
    # The recorder's read says the folder, the relay's own read does not: the relay's is the one that counts.
    w = PlayWorld([lambda s: _played(PLAY, 1, since=s)], {PLAY: PROGRAM}, relay_image=lambda pid: ELSEWHERE)
    try:
        with pytest.raises(client.GameRefused) as no:
            w.aim(PLAY)
        assert no.value.sent is True and "is not in the game's folder" in no.value.why and w.link.game is None
        assert not w.link.ended.is_set() and w.relay.thread.is_alive()
    finally:
        assert w.close() == protocol.EXIT_OK


def test_aim_is_for_a_relay_approved_for_a_folder_and_one_at_a_time(game):
    relay = InProcessRelay(parts())
    link = launch(game, relay)
    try:
        with pytest.raises(ValueError, match="running game itself, so it takes no other"):     # aim's own words
            link.aim((game["pid"], game["create_time"]))
        with pytest.raises(ValueError, match="no game to look for"):
            link.own()
    finally:
        link.close()
    w = PlayWorld([lambda s: _played(PLAY, 1, since=s)], {PLAY: PROGRAM})
    try:
        w.link._aiming = (PLAY2, 1)
        with pytest.raises(RuntimeError):
            w.aim(PLAY)
        w.link._aiming = None
        w.link.aim_by = time.monotonic() - 1                   # the relay's wait for a game is over
        with pytest.raises(client.RelayFailed, match="wait for the game is over"):
            w.aim(PLAY)
    finally:
        assert w.close() == protocol.EXIT_OK


def test_aim_without_an_answer_ends_the_link(monkeypatch):
    monkeypatch.setattr(client, "AIM_S", 0.5)
    me = _me()
    games = snapshot_with(_played(PLAY, 1, since=me.create_time))
    relay = ScriptedRelay(lambda cfg: [_hello(cfg)])
    link = client.launch(None, folders=(FOLDER,), wait_s=30, presentmon_exe=None, frames=False, network=True,
                         recorder=(me.pid, me.create_time), sid=elevation.token_user_sid(), launcher=relay,
                         owner=_default_owner())
    try:
        with pytest.raises(client.RelayFailed, match="did not answer about the game within"):
            link.aim((PLAY, me.create_time + SECOND), snapshot=games, image=lambda pid: PROGRAM)
        assert link.ended.is_set() and link.game is None
    finally:
        link.close()


def test_an_answer_about_a_game_nobody_named_breaks_the_link():
    me = _me()
    relay = ScriptedRelay(lambda cfg: [_hello(cfg), protocol.game((PLAY, 5))])
    link = client.launch(None, folders=(FOLDER,), wait_s=30, presentmon_exe=None, frames=False, network=True,
                         recorder=(me.pid, me.create_time), sid=elevation.token_user_sid(), launcher=relay,
                         owner=_default_owner())
    try:
        _until(lambda: link.ended.is_set(), 10, "the link to break")
        assert "a game the recording window did not name" in (link.why or "")
    finally:
        link.close()


@pytest.mark.parametrize("over, says", [({"folders": ["C:" + _B + "Other"]}, "folder it was approved for"),
                                        ({"folders": None, "game": [5, 1]}, "did not greet")])
def test_launch_for_a_folder_refuses_a_hello_with_another_folder_or_a_game(over, says):
    me = _me()
    with pytest.raises(client.RelayFailed, match=says):
        client.launch(None, folders=(FOLDER,), wait_s=30, presentmon_exe=None, frames=False, network=True,
                      recorder=(me.pid, me.create_time), sid=elevation.token_user_sid(),
                      launcher=ScriptedRelay(lambda cfg: [_hello(cfg, **over)]), owner=_default_owner())


def test_a_play_launch_that_cannot_tell_when_the_relay_started_leaves_no_relay_behind(monkeypatch):
    monkeypatch.setattr(client, "_relay_and_session", lambda relay, recorder: (None, None))
    me = _me()
    relay = InProcessRelay(parts())
    with pytest.raises(client.RelayFailed, match="start time could not be read here"):
        client.launch(None, folders=(FOLDER,), wait_s=30, presentmon_exe=None, frames=False, network=True,
                      recorder=(me.pid, me.create_time), sid=elevation.token_user_sid(), launcher=relay,
                      owner=_default_owner())
    relay.thread.join(15)
    assert not relay.thread.is_alive() and relay.code == protocol.EXIT_OK    # it saw the hang-up and ended


def test_a_recording_that_starts_before_the_relay_accepted_a_game_records_nothing():
    w = PlayWorld([lambda s: _played(PLAY, 1, since=s)], {PLAY: PROGRAM})
    try:
        w.link.start(clock.now())
        _until(lambda: w.link.ended.is_set(), 10, "the link to end")
        assert "before the relay had accepted its game" in (w.link.why or "")
        w.relay.thread.join(15)
        assert w.relay.code == protocol.EXIT_OK and w.built == []       # told to stop, it ended at once
    finally:
        w.close()


def test_frames_of_another_process_than_the_accepted_game_record_nothing():
    w = PlayWorld([lambda s: _played(PLAY, 1, since=s)], {PLAY: PROGRAM})
    try:
        w.aim(PLAY)
        frames = w.link.frames_collector(PLAY2)
        assert w.link.ended.is_set() and "is not the one the relay accepted" in (w.link.why or "")
        frames.start(clock.now())
        _until(lambda: frames.ended, 10, "frames to end")
        frames.stop()
        assert frames.status().state == "failed" and "is not the one the relay accepted" in frames.status().detail
        w.relay.thread.join(15)
        assert w.relay.code == protocol.EXIT_OK and w.built == []
    finally:
        w.close()


def test_a_relay_whose_recorder_is_gone_by_the_start_starts_nothing():
    me = _me()
    gone = threading.Event()
    real = snapshot_with()
    relay_proc = _fake_proc(RELAY_PID, me.session_id, me.create_time + 5 * SECOND, "pythonw.exe")
    game = _played(PLAY, 1, since=relay_proc.create_time)

    def snapshot():
        procs = real()
        procs[RELAY_PID], procs[PLAY] = relay_proc, game
        if gone.is_set():
            procs.pop(me.pid, None)                  # gone, while a copy of its pipe handle sends the start
        return procs
    r = PlayRelay(snapshot=snapshot, image=lambda pid: PROGRAM)
    try:
        assert r.propose(PLAY, game.create_time)["t"] == "game"
        gone.set()
        r.send(protocol.start(clock.now()))
        r.thread.join(15)
        assert not r.thread.is_alive()
    finally:
        r.close()
    assert r.built == []                             # neither the network trace nor PresentMon was started


def test_a_relay_for_the_running_game_ends_when_its_recorder_leaves_before_the_start(game):
    me = _me()
    gone = threading.Event()
    real = snapshot_with()

    def snapshot():
        procs = real()
        if gone.is_set():
            procs[me.pid] = dataclasses.replace(procs[me.pid], create_time=me.create_time + 1)
        return procs
    argv = _argv(game)
    p = parts(snapshot=snapshot)
    done: dict = {}
    t = threading.Thread(target=lambda: done.setdefault("code", server.main(argv, owner="", parts=p)), daemon=True)
    t.start()
    c = pipe.connect(argv[argv.index("--pipe") + 1], 10, max_message=protocol.MAX_MESSAGE)
    try:
        assert protocol.from_relay(c.read(10))["t"] == "hello"
        gone.set()
        said = [protocol.from_relay(c.read(10)) for _ in range(2)]
        assert said == [{"t": "error", "why": "the recording window is no longer running"}, {"t": "end"}], said
    finally:
        c.close()
        t.join(15)
    assert done["code"] == protocol.EXIT_TARGET


def test_the_relays_judgement_reads_no_folder_from_its_command_line_to_its_answer(monkeypatch):
    # With administrator rights the relay looks at no folder: not the forms, not the game's program path.
    for name in ("_lstat", "_readlink", "_real_path"):
        monkeypatch.setattr(tgt, name, lambda *a, _n=name: pytest.fail(f"the relay called target.{_n}"))
    monkeypatch.setattr(tgt.winfolders, "long_path", lambda path: pytest.fail("the relay read a folder"))
    short = "C:" + _B + "GAMES~1" + _B + "LANTER~1" + _B + "lantern_.exe"
    r = PlayRelay([lambda s: _played(PLAY, 1, since=s), lambda s: _played(PLAY2, 2, since=s)],
                  {PLAY: short, PLAY2: PROGRAM})
    try:
        assert "8.3 short names" in r.propose(PLAY, r.since + SECOND)["why"]
        assert r.propose(PLAY2, r.since + 2 * SECOND)["t"] == "game"
        r.send(protocol.start(clock.now()))
        _until(lambda: any(b[0] == "frames" for b in r.built), 10, "PresentMon to start")
        r.send(protocol.stop())
        r.to_end()
    finally:
        assert r.close() == protocol.EXIT_OK


def test_aim_names_at_most_max_games_and_asks_no_more():
    w = PlayWorld([lambda s: _played(PLAY, 1, since=s)], {PLAY: PROGRAM}, relay_image=lambda pid: ELSEWHERE)
    try:
        for _ in range(protocol.MAX_GAMES):
            with pytest.raises(client.GameRefused) as no:
                w.aim(PLAY)
            assert no.value.sent is True
        with pytest.raises(client.RelayFailed, match="at most 8 games"):
            w.aim(PLAY)
        assert not w.link.ended.is_set() and w.relay.thread.is_alive()     # nothing broke: no ninth was sent
    finally:
        assert w.close() == protocol.EXIT_OK


def test_aim_names_nothing_once_the_recording_stopped_while_it_checked_the_game():
    w = PlayWorld([lambda s: _played(PLAY, 1, since=s)], {PLAY: PROGRAM})
    sent: list = []
    real = w.link._client.write
    w.link._client.write = lambda data, t: sent.append(protocol.from_recorder(data)["t"]) or real(data, t)

    def snapshot():
        w.link.stop()                                # the window closes while the recorder checks the game
        return w.snapshot()
    try:
        with pytest.raises(client.RelayFailed, match="stopped while its game was being checked"):
            w.aim(PLAY, snapshot=snapshot)
        assert sent == ["stop"] and w.link.game is None
    finally:
        assert w.close() == protocol.EXIT_OK


def test_a_game_the_relay_refuses_takes_the_place_of_the_one_it_accepted_on_both_sides():
    relay_says = {PLAY: PROGRAM, PLAY2: ELSEWHERE}
    w = PlayWorld([lambda s: _played(PLAY, 1, since=s), lambda s: _played(PLAY2, 2, since=s)],
                  {PLAY: PROGRAM, PLAY2: PROGRAM}, relay_image=relay_says.get)
    try:
        w.aim(PLAY)
        assert w.link.game == (PLAY, w.since + SECOND)
        with pytest.raises(client.GameRefused):
            w.aim(PLAY2, after_s=2)
        assert w.link.game is None                   # as the relay now holds none: a start records nothing
    finally:
        assert w.close() == protocol.EXIT_OK


def test_the_relays_answer_counts_only_for_the_game_named_and_only_once():
    w = PlayWorld()
    try:
        link = w.link
        assert link._take_reply({"t": "game", "game": (PLAY, 5)}) is False          # nobody named one
        link._aiming = (PLAY, 5)
        assert link._take_reply({"t": "game", "game": (PLAY2, 5)}) is False         # another game
        assert link._take_reply({"t": "game", "game": (PLAY, 6)}) is False          # another create time
        assert link._take_reply({"t": "refused", "game": (PLAY, 5), "why": "", "retry": False}) is True
        assert link._take_reply({"t": "game", "game": (PLAY, 5)}) is False          # a second answer
        link._aiming = None
    finally:
        w.close()


def test_aim_says_the_relays_own_reason_and_exit_code_when_the_link_ends():
    me = _me()
    script = lambda cfg: [_hello(cfg), protocol.error("no game the relay could record started in time"),
                          protocol.end()]
    relay = ScriptedRelay(script)
    relay.wait = lambda timeout_s=None: protocol.EXIT_NO_CLIENT if timeout_s else None   # once waited for
    link = client.launch(None, folders=(FOLDER,), wait_s=30, presentmon_exe=None, frames=False, network=True,
                         recorder=(me.pid, me.create_time), sid=elevation.token_user_sid(), launcher=relay,
                         owner=_default_owner())
    try:
        _until(lambda: link.ended.is_set(), 10, "the relay's end")
        with pytest.raises(client.RelayFailed) as failed:
            link.aim((PLAY, me.create_time + SECOND), snapshot=snapshot_with(_played(PLAY, 1, since=me.create_time)),
                     image=lambda pid: PROGRAM)
        assert failed.value.why == "the relay ended: no game the relay could record started in time"
        assert failed.value.code == protocol.EXIT_NO_CLIENT
    finally:
        link.close()
