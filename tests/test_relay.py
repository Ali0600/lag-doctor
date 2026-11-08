"""The relay (lagdoctor/relay/): the relay process's checks, the recorder's checks of it, and recordings through it.

The relay's own code runs on a thread of this process over a real pipe, unelevated, and stands in for the elevated
process `launch_elevated` would start (`InProcessRelay`): nothing here raises a UAC prompt. Its frame source is a real
PresentMon replaying one of PresentMon's gold captures (no rights needed), or a stand-in; its network source is a
stand-in ETW session that delivers Kernel-Network events built here. Its pipe is owned by this token's default owner
instead of Administrators, which only an elevated token may set: `test_only_an_elevated_relay_can_own_its_pipe`
checks that owner on both sides of elevation.
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from lagdoctor import clock
from lagdoctor.capture import netflows as nf
from lagdoctor.capture import presentmon as pm
from lagdoctor.capture import session as sess
from lagdoctor.capture import target as tgt
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


def parts(*, snapshot=None, frames=None, etw=None, verify=None) -> server.Parts:
    return server.Parts(snapshot=snapshot or snapshot_with(), verify=verify or (lambda exe: None),
                        frames=frames or (lambda cfg, forward, alive: FakeFrames(cfg, forward, alive)),
                        etw=etw or (lambda on_event: FakeEtw(on_event, [])))


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
                                     b"not json", b'{"t":"start","origin":NaN}'])
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
    msg = {"t": "hello", "v": protocol.VERSION, "pid": os.getpid(), "game": list(cfg.game), "frames": cfg.frames,
           "network": cfg.network}
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
            time.sleep(0.5)
            return 7

        def close(self):
            self.reader_alive_at_close = link._reader.is_alive()
    child = Child()
    link = client.RelayLink(Pipe(), child, {"pid": os.getpid(), "frames": False, "network": True})
    _until(child.in_wait.is_set, 5, "the reader to wait on the relay's exit")
    link.close()
    assert child.reader_alive_at_close is False and child.waits[0] == 1.0
    assert link.why == "the relay ended (exit code 7)"
