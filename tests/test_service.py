"""The capture service (lagdoctor/service, lagdoctor/winapi/service.py): its host run on a thread here, never installed.

Stage 4 (docs/INSTALLER-PLAN.md). The host takes callers on a real pipe of the test's own name, owned by this token's
default owner (the service's own SID exists only in the service), and runs the relay core with the stand-ins of
tests/test_relay.py: no PresentMon, no ETW session. Each caller is this process, so its token is this test's: what
the host reads of it is checked against this token, and a caller of another session comes from a stand-in `caller`.
Nothing here installs, starts or stops a service; the SCM is only asked about services by name, read-only.
"""
from __future__ import annotations

import contextlib
import dataclasses
import os
import secrets
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from lagdoctor import clock
from lagdoctor.capture import target as tgt
from lagdoctor.capture import tracelock
from lagdoctor import service as svc
from lagdoctor.relay import protocol, server
from lagdoctor.winapi import elevation, ntquery, pipe
from lagdoctor.winapi import service as scm
from tests import test_relay as tr
from tests.test_relay import game  # noqa: F401 - a fixture: a running 'game' in this session

PRESENTMON = Path(r"C:\Program Files\lag-doctor\PresentMon\PresentMon.exe")   # never run: the frames are fakes
LOCAL_IP, SERVER_IP = "192.168.0.10", "203.0.113.5"


@contextlib.contextmanager
def running_host(**kw):
    """A Host serving on a pipe of this test's own, on a thread; stopped and joined when the block ends."""
    name = "lagdoctor-test-" + secrets.token_hex(8)
    stop, out = threading.Event(), {}
    kw.setdefault("presentmon_exe", PRESENTMON)
    kw.setdefault("trace_lock", tr.own_trace_lock)       # the service's own SID exists only in the service
    host = svc.Host(kw.pop("parts", None) or tr.parts(), pipe_name=name, owner="", **kw)
    t = threading.Thread(target=lambda: out.setdefault("code", host.serve(stop)), name="host-under-test", daemon=True)
    t.start()
    try:
        yield host, name, stop, out
    finally:
        stop.set()
        t.join(30.0)
        assert not t.is_alive(), "the host did not stop"


def ask_bytes(game=None, folders=None, wait_s=None, frames=False, network=True, **over) -> bytes:
    msg = {"t": "ask", "v": protocol.VERSION, "game": None if game is None else [game["pid"], game["create_time"]],
           "folders": folders, "wait_s": wait_s, "frames": frames, "network": network, **over}
    return protocol.encode(msg)


def messages(client, until: str, timeout: float = 20.0) -> list[dict]:
    """What the host sends, read until a message of type `until` (or `end`) arrives."""
    out, deadline = [], time.monotonic() + timeout
    while time.monotonic() < deadline:
        msg = protocol.from_relay(client.read(max(0.1, deadline - time.monotonic())))
        out.append(msg)
        if msg["t"] in (until, "end"):
            return out
    raise AssertionError(f"no {until} within {timeout} s: {[m['t'] for m in out]}")


def refusal(name: str, data: bytes, read_after_s: float = 0.0) -> str:
    """The host's refusal of one ask: its words, after `error` then `end`. read_after_s: wait that long before reading,
    as a caller busy elsewhere would (a host that disconnected at once would have dropped the words by then)."""
    with pipe.connect(name, 10.0) as c:
        c.write(data, 5.0)
        time.sleep(read_after_s)
        got = messages(c, "end")
    assert [m["t"] for m in got] == ["error", "end"], got
    return got[0]["why"]


# ------------------------------------------------------------------------------------------- callers ---

def test_a_caller_records_its_running_game_and_the_service_reads_who_it_is_from_its_token(game):  # noqa: F811
    me = tr._me()
    with running_host() as (host, name, _stop, _out):
        with pipe.connect(name, 10.0) as c:
            dacl = c.descriptor()                           # interactive users alone may open it
            assert dacl.startswith("D:P(A;;0x12018b;;;IU)") and dacl.count("(A;") == 1, dacl
            c.write(ask_bytes(game, frames=True, network=True), 5.0)
            hello = messages(c, "hello")[-1]
            assert hello == {"t": "hello", "v": protocol.VERSION, "pid": os.getpid(),
                             "game": (game["pid"], game["create_time"]), "folders": None, "frames": True,
                             "network": True}
            c.write(protocol.start(clock.now()), 5.0)
            got = messages(c, "rows")
            assert got[-1]["t"] == "rows" and {r["ProcessID"] for r in got[-1]["rows"]} == {game["pid"]}
            c.write(protocol.stop(), 5.0)
            assert messages(c, "end")[-1]["t"] == "end"
        assert host.served == 1 and host.refused == []
    assert me.pid == os.getpid()


def test_a_caller_that_says_nothing_is_let_go_and_the_next_one_gets_in(game):  # noqa: F811
    with running_host(first_ask_s=0.5) as (host, name, _stop, _out):
        with pipe.connect(name, 10.0) as silent:
            with pytest.raises(OSError) as let_go:          # let go: the pipe is disconnected under it
                silent.read(10.0)
            assert not isinstance(let_go.value, TimeoutError), "the silent caller was kept, not let go"
        with pipe.connect(name, 10.0) as c:
            c.write(ask_bytes(game), 5.0)
            assert messages(c, "hello")[-1]["t"] == "hello"
            c.write(protocol.stop(), 5.0)
            messages(c, "end")
        assert host.served == 1


def test_an_ask_from_another_version_or_of_another_shape_is_refused_in_words(game):  # noqa: F811
    with running_host() as (host, name, _stop, _out):
        why = refusal(name, ask_bytes(game, v=protocol.VERSION - 1), read_after_s=0.5)
        assert "different versions" in why
        assert "does not take" in refusal(name, protocol.start(1.0))           # a first message that is no ask
        assert "does not take" in refusal(name, ask_bytes(game, frames=False, network=False))
        assert host.served == 0 and len(host.refused) == 3


def test_a_caller_whose_token_names_another_session_than_its_process_is_refused(game):  # noqa: F811
    me = tr._me()

    def elsewhere(srv) -> pipe.Caller:
        real = srv.caller()
        return pipe.Caller(sid=real.sid, session=real.session + 1, elevated=real.elevated, level=real.level)

    with running_host(caller=elsewhere) as (host, name, _stop, _out):
        # Play's ask: no game is judged yet, so the session check alone can refuse it.
        why = refusal(name, ask_bytes(folders=[r"D:\Games\Blue"], wait_s=30.0))
        assert "session is not the one its account signed in to" in why, why
        assert host.served == 0 and me.session_id >= 0


def test_a_game_in_another_session_or_the_caller_itself_is_never_recorded(game):  # noqa: F811
    me = tr._me()
    other = tr._fake_proc(4_000_123, me.session_id + 7, create_time=5)
    snapshot = tr.snapshot_with(other)
    with running_host(parts=tr.parts(snapshot=snapshot)) as (host, name, _stop, _out):
        for pid, ct in ((other.pid, other.create_time), (game["pid"], game["create_time"] + 1)):   # pid reused
            expected = tgt.accept_target(snapshot(), pid, ct, session=me.session_id, approved=(pid, ct))
            assert expected and refusal(name, ask_bytes({"pid": pid, "create_time": ct})) == expected
        assert "the recording window is not the game" in refusal(name, ask_bytes({"pid": me.pid,
                                                                                  "create_time": me.create_time}))
        assert host.served == 0 and len(host.refused) == 3


def test_frame_timing_runs_only_the_presentmon_installed_beside_it_and_only_once_it_passes_its_check(game):  # noqa: F811
    with running_host(presentmon_exe=None) as (host, name, _stop, _out):
        assert "no PresentMon" in refusal(name, ask_bytes(game, frames=True))
    checked: list[Path] = []

    def failing(exe: Path) -> None:
        checked.append(exe)
        raise OSError("not the pinned build")

    with running_host(parts=tr.parts(verify=failing)) as (host, name, _stop, _out):
        assert "failed its check" in refusal(name, ask_bytes(game, frames=True))
    assert checked == [PRESENTMON]


def test_one_recording_at_a_time_a_second_caller_finds_the_pipe_busy(game):  # noqa: F811
    with running_host() as (host, name, _stop, _out):
        with pipe.connect(name, 10.0) as first:
            first.write(ask_bytes(game), 5.0)
            messages(first, "hello")
            with pytest.raises(TimeoutError):
                pipe.connect(name, 1.0)
            first.write(protocol.stop(), 5.0)
            messages(first, "end")


def test_the_next_callers_ask_is_never_read_by_the_recording_before_it(game):  # noqa: F811
    with running_host() as (host, name, _stop, _out):
        first = pipe.connect(name, 10.0)
        try:
            first.write(ask_bytes(game), 5.0)
            messages(first, "hello")
            first.write(protocol.stop(), 5.0)
            messages(first, "end")                       # and it stays connected: its reader would still be reading
            with pipe.connect(name, 20.0) as second:     # once the host has let the first go
                second.write(ask_bytes(game), 5.0)
                assert messages(second, "hello")[-1]["t"] == "hello"
                second.write(protocol.stop(), 5.0)
                messages(second, "end")
        finally:
            first.close()
        assert host.served == 2


def test_a_stop_ends_a_recording_in_progress_and_the_service_with_it(game):  # noqa: F811
    with running_host() as (host, name, stop, out):
        with pipe.connect(name, 10.0) as c:
            c.write(ask_bytes(game), 5.0)
            messages(c, "hello")
            c.write(protocol.start(clock.now()), 5.0)
            stop.set()
            try:                                          # `end`, or the pipe gone: either way it ended
                ended = messages(c, "end", timeout=15.0)[-1]["t"] == "end"
            except TimeoutError:
                ended = False                             # still recording after the stop
            except OSError:
                ended = True
            assert ended, "the recording went on after the stop"
        tr._until(lambda: "code" in out, 30, "the host to stop")
    assert out["code"] == protocol.EXIT_OK


def test_the_service_stops_itself_when_no_caller_comes():
    with running_host(idle_s=0.5) as (host, name, _stop, out):
        tr._until(lambda: "code" in out, 10, "the idle host to stop")
    assert out["code"] == protocol.EXIT_OK and host.served == 0


def test_a_pipe_name_another_process_holds_is_never_joined():
    name = "lagdoctor-test-" + secrets.token_hex(8)
    with pipe.PipeServer(name, pipe.client_dacl([elevation.token_user_sid()])):
        host = svc.Host(tr.parts(), pipe_name=name, owner="", presentmon_exe=PRESENTMON)
        assert host.serve(threading.Event()) == protocol.EXIT_PIPE


def test_the_service_owns_its_pipe_as_its_own_service_sid_by_default():
    host = svc.Host(tr.parts())
    assert host.owner == "O:" + scm.service_sid(protocol.SERVICE_NAME) and host.pipe_name == protocol.SERVICE_PIPE


# ------------------------------------------------------------------------------- the game's tree ---

def test_only_the_game_and_programs_under_its_own_folder_count_as_the_game():
    procs = {1: tr._fake_proc(1, 1, 10), 2: tr._fake_proc(2, 1, 11), 3: tr._fake_proc(3, 1, 12),
             4: tr._fake_proc(4, 1, 13), 5: tr._fake_proc(5, 1, 14)}
    paths = {1: r"D:\Games\Blue\Bin\blue.exe", 2: r"d:/games/blue/bin/Helpers/crash.exe",
             3: r"D:\Games\Blue\BinX\other.exe", 4: r"C:\Windows\System32\cmd.exe", 5: None}
    reads: list[int] = []

    def image(pid: int):
        reads.append(pid)
        return paths.get(pid)

    rule = svc.GameFolderTree(image)
    assert rule(procs, (1, 10), {1, 2, 3, 4, 5}) == {1, 2}       # a sibling folder sharing the prefix stays out
    first = len(reads)
    assert rule(procs, (1, 10), {1, 2, 3, 4, 5}) == {1, 2} and len(reads) == first   # remembered per process
    paths[2] = r"C:\Elsewhere\x.exe"
    procs[2] = tr._fake_proc(2, 1, 99)                            # pid 2 is another process now: judged again
    assert rule(procs, (1, 10), {1, 2}) == {1}
    paths[1] = None
    assert svc.GameFolderTree(image)(procs, (1, 10), {1, 2, 3}) == {1}   # the game's folder unread: the game alone


def test_a_descendant_from_another_folder_never_carries_its_events_out_of_the_service(game):  # noqa: F811
    me = tr._me()
    real = ntquery.image_path(game["pid"])
    assert real, "the game's program path could not be read"
    helper = dataclasses.replace(tr._fake_proc(4_000_201, me.session_id, create_time=game["create_time"] + 1),
                                 ppid=game["pid"])
    stranger = dataclasses.replace(tr._fake_proc(4_000_202, me.session_id, create_time=game["create_time"] + 1),
                                   ppid=game["pid"])
    images = {helper.pid: str(Path(real).parent / "helper.exe"), stranger.pid: r"C:\Elsewhere\admin-tool.exe"}
    script = [(42, 0, tr.payload(42, pid, 100, SERVER_IP, LOCAL_IP, 27015, 50000))
              for pid in (game["pid"], helper.pid, stranger.pid)]
    parts = tr.parts(snapshot=tr.snapshot_with(helper, stranger),
                     etw=lambda on_event: tr.FakeEtw(on_event, script))
    parts.image = lambda pid: images.get(pid) or ntquery.image_path(pid)
    with running_host(parts=parts) as (host, name, _stop, _out):
        with pipe.connect(name, 10.0) as c:
            c.write(ask_bytes(game, network=True), 5.0)
            messages(c, "hello")
            c.write(protocol.start(clock.now()), 5.0)
            time.sleep(0.5)
            c.write(protocol.stop(), 5.0)
            got = messages(c, "end")
    pids = {struct.unpack_from("<I", ev[3], 0)[0] for m in got if m["t"] == "events" for ev in m["ev"]}
    assert pids == {game["pid"], helper.pid}
    totals = {pid for m in got if m["t"] == "net" for pid, _rx, _tx in m["totals"]}
    assert stranger.pid in totals and helper.pid not in totals


# ------------------------------------------------------------------------------------------ the ask ---

@pytest.mark.parametrize("over", [{"v": "3"}, {"game": None}, {"wait_s": 5.0}, {"frames": 1}, {"extra": True},
                                  {"folders": [r"D:\Games\Blue"], "wait_s": None},
                                  {"game": None, "folders": [r"D:\Games\Blue"], "wait_s": 0.0},
                                  {"game": None, "folders": [r"D:\Games\Blue"], "wait_s": 1.2345},
                                  {"game": None, "folders": [r"D:\Games\Blue"], "wait_s": 10 ** 400}])
def test_an_ask_is_taken_only_in_the_shape_the_recorder_builds(over):
    good = {"t": "ask", "v": protocol.VERSION, "game": [4242, 7], "folders": None, "wait_s": None, "frames": False,
            "network": True}
    assert protocol.from_caller(protocol.encode(good))["game"] == (4242, 7)      # the control: this one is taken
    with pytest.raises(protocol.ProtocolError):
        protocol.from_caller(protocol.encode({**good, **over}))


def test_an_ask_built_by_the_protocol_reads_back_as_it_was_built():
    running = protocol.from_caller(protocol.ask((4242, 7), None, None, True, False))
    assert running == {"t": "ask", "v": protocol.VERSION, "game": (4242, 7), "folders": None, "wait_s": None,
                       "frames": True, "network": False}
    play = protocol.from_caller(protocol.ask(None, (r"D:\Games\Blue",), 300.5, False, True))
    assert play["folders"] == (r"D:\Games\Blue",) and play["wait_s"] == 300.5 and play["game"] is None


# ------------------------------------------------------------------------------- the SCM, read-only ---

def test_the_service_sid_is_the_one_windows_derives():
    for name in (protocol.SERVICE_NAME, "EventLog"):
        out = subprocess.run(["sc.exe", "showsid", name], capture_output=True, text=True, check=True).stdout
        assert f"SERVICE SID: {scm.service_sid(name)}" in out, out


def test_a_service_is_queried_by_name_and_one_not_installed_says_so():
    state, pid = scm.query("EventLog")
    assert state == "running" and pid > 0
    with pytest.raises(OSError) as missing:
        scm.query("lagdoctor-test-" + secrets.token_hex(8))
    assert missing.value.winerror == scm.ERROR_SERVICE_DOES_NOT_EXIST
    with pytest.raises(OSError) as not_there:
        scm.start("lagdoctor-test-" + secrets.token_hex(8))      # refused before anything could start
    assert not_there.value.winerror == scm.ERROR_SERVICE_DOES_NOT_EXIST


def test_the_service_started_by_anything_but_the_scm_runs_nothing():
    proc = subprocess.run([sys.executable, "-I", "-m", "lagdoctor.service"], capture_output=True, text=True,
                          timeout=60, cwd=str(Path(__file__).resolve().parents[1]))
    assert proc.returncode == scm.ERROR_FAILED_SERVICE_CONTROLLER_CONNECT, (proc.returncode, proc.stderr)


# ------------------------------------------------------------------------------- the pipe's caller ---

def test_a_pipe_server_reads_its_clients_account_and_session_from_the_clients_own_token():
    me = tr._me()
    name = "lagdoctor-test-" + secrets.token_hex(8)
    seen: dict = {}

    def serve(srv) -> None:
        assert srv.wait_client(10.0)
        assert srv.read(5.0) == b"hi"
        seen["caller"] = srv.caller()
        seen["after"] = _thread_impersonates()
        srv.write(b"ok", 5.0)

    with pipe.PipeServer(name, pipe.client_dacl([elevation.token_user_sid()])) as srv:
        t = threading.Thread(target=serve, args=(srv,))   # its own thread: what a broken revert leaves dies with it
        t.start()
        with pipe.connect(name, 5.0) as c:
            c.write(b"hi", 5.0)
            assert c.read(5.0) == b"ok"
        t.join(10.0)
    assert seen["caller"] == pipe.Caller(sid=elevation.token_user_sid(), session=me.session_id,
                                         elevated=elevation.is_elevated(), level="identification")
    assert seen["after"] is False                         # the thread acts as the process again


def _thread_impersonates() -> bool:
    import ctypes
    from ctypes import wintypes
    adv, k32 = ctypes.WinDLL("advapi32", use_last_error=True), ctypes.WinDLL("kernel32")
    adv.OpenThreadToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, ctypes.POINTER(wintypes.HANDLE)]
    k32.GetCurrentThread.restype = wintypes.HANDLE
    token = wintypes.HANDLE()
    if adv.OpenThreadToken(k32.GetCurrentThread(), 0x0008, True, ctypes.byref(token)):
        k32.CloseHandle(token)
        return True
    return False


# ------------------------------------------------------------------------ what the review found ---

@pytest.mark.parametrize("program", [r"C:\Windows\explorer.exe", r"D:\x.exe"])
def test_a_running_game_whose_program_sits_in_the_windows_folder_or_a_drives_root_is_refused(game, program):  # noqa: F811
    parts = tr.parts()
    parts.image = lambda pid: program if pid == game["pid"] else ntquery.image_path(pid)
    with running_host(parts=parts) as (host, name, _stop, _out):
        why = refusal(name, ask_bytes(game))
        assert "records no program from there" in why, why
        assert host.served == 0


def test_a_tree_whose_paths_cannot_be_read_keeps_to_the_game_and_never_raises():
    procs = {1: tr._fake_proc(1, 1, 10), 2: tr._fake_proc(2, 1, 11)}

    def broken(pid: int):
        raise OSError("the read failed")

    assert svc.GameFolderTree(broken)(procs, (1, 10), {1, 2}) == {1}
    windows = svc.GameFolderTree(lambda pid: {1: r"C:\Windows\explorer.exe", 2: r"C:\Windows\System32\cmd.exe"}[pid])
    assert windows(procs, (1, 10), {1, 2}) == {1}                 # the Windows folder is no game's own


def test_a_recording_whose_tree_cannot_be_judged_ends_and_says_why(game, monkeypatch):  # noqa: F811
    real, calls = svc.GameFolderTree.__call__, []

    def boom(self, procs, game_id, pids):          # the first refresh, before the loop, goes through
        calls.append(game_id)
        if len(calls) > 1:
            raise RuntimeError("cannot judge")
        return real(self, procs, game_id, pids)

    with running_host() as (host, name, _stop, _out):
        with pipe.connect(name, 10.0) as c:
            c.write(ask_bytes(game), 5.0)
            messages(c, "hello")
            monkeypatch.setattr(svc.GameFolderTree, "__call__", boom)
            c.write(protocol.start(clock.now()), 5.0)
            got = messages(c, "end", timeout=15.0)
    errors = [m["why"] for m in got if m["t"] == "error"]
    assert errors and "stopped checking the game's processes" in errors[-1], got


def test_a_recorder_that_started_after_its_connection_was_made_is_not_the_one_that_connected(game):  # noqa: F811
    me = tr._me()
    later = dataclasses.replace(me, create_time=me.create_time + 10 ** 15)   # a process that took this pid later
    with running_host(parts=tr.parts(snapshot=tr.snapshot_with(later))) as (host, name, _stop, _out):
        assert "started after the connection was made" in refusal(name, ask_bytes(game))


def test_plays_folder_is_checked_when_it_is_asked_for():
    with running_host() as (host, name, _stop, _out):
        why = refusal(name, ask_bytes(folders=[r"C:\Windows\Games"], wait_s=30.0))
        assert "does not wait on that folder" in why, why


def test_a_newer_recorder_hears_that_the_versions_differ_whatever_its_ask_holds():
    with running_host() as (host, name, _stop, _out):
        why = refusal(name, ask_bytes({"pid": 4242, "create_time": 7}, v=protocol.VERSION + 1, newfield=True))
        assert "different versions" in why, why


def test_a_refusal_never_carries_control_characters_to_the_recording_window(game):  # noqa: F811
    def unreadable(srv):
        raise OSError("a name with \x1b[2J in it")

    with running_host(caller=unreadable) as (host, name, _stop, _out):
        why = refusal(name, ask_bytes(game))
    assert "\x1b" not in why and "?[2J" in why, why


def test_a_recording_that_fails_unexpectedly_says_so_before_the_pipe_goes(game, monkeypatch):  # noqa: F811
    def broken(self):
        raise RuntimeError("the core broke")

    monkeypatch.setattr(server.Relay, "serve", broken)
    with running_host() as (host, name, _stop, _out):
        why = refusal(name, ask_bytes(game), read_after_s=0.5)
    assert "did not expect" in why and "the core broke" in why, why


def test_a_recording_whose_reader_would_not_leave_ends_the_service(game, monkeypatch):  # noqa: F811
    monkeypatch.setattr(server.Relay, "close", lambda self, wait_s=5.0: False)
    with running_host() as (host, name, _stop, out):
        with pipe.connect(name, 10.0) as c:
            c.write(ask_bytes(game), 5.0)
            messages(c, "hello")
            c.write(protocol.stop(), 5.0)
            messages(c, "end")
        tr._until(lambda: "code" in out, 20, "the host to stop")
    assert out["code"] == protocol.EXIT_CRASH


def test_a_game_that_exits_while_its_program_is_read_is_refused(game):  # noqa: F811
    read, real = {"done": False}, tr.snapshot_with()

    def snapshot():
        procs = real()
        if read["done"]:                                  # it exited once its path had been read
            procs.pop(game["pid"], None)
        return procs

    def image(pid: int):
        read["done"] = read["done"] or pid == game["pid"]
        return ntquery.image_path(pid)

    parts = tr.parts(snapshot=snapshot)
    parts.image = image
    with running_host(parts=parts) as (host, name, _stop, _out):
        assert "exited while the capture service checked it" in refusal(name, ask_bytes(game))


# ------------------------------------------------------------------------------------------ the hold ---

def _codes(monkeypatch) -> list[int]:
    """What each recording's relay core returned, newest last: the host itself keeps no exit code."""
    codes: list[int] = []
    real = server.Relay.serve

    def serve(self):
        codes.append(real(self))
        return codes[-1]
    monkeypatch.setattr(server.Relay, "serve", serve)
    return codes


def test_a_recording_held_past_the_services_hold_is_ended_with_its_sources_stopped_and_why_said(game, monkeypatch):  # noqa: F811
    codes, stops = _codes(monkeypatch), []
    parts = tr.parts(frames=lambda cfg, forward, alive: tr.FakeFrames(cfg, forward, alive, stops=stops),
                     etw=lambda on_event: tr.FakeEtw(on_event, [], stops=stops))
    with running_host(parts=parts, hold_s=3.0) as (host, name, _stop, _out):
        with pipe.connect(name, 10.0) as c:
            c.write(ask_bytes(game, frames=True, network=True), 5.0)
            asked = time.monotonic()
            messages(c, "hello")
            c.write(protocol.start(clock.now()), 5.0)
            assert messages(c, "rows")[-1]["t"] == "rows"           # it records, and this caller never says stop
            got = messages(c, "end", timeout=20.0)
            took = time.monotonic() - asked
    assert [m["t"] for m in got[-2:]] == ["error", "end"], got
    assert got[-2]["why"] == server.HELD and "at most 2 hours at a time" in server.HELD, got[-2]
    assert [m["t"] for m in got if m.get("final")] == ["net", "frames"], got     # each source's last word came first
    assert stops == ["net", "frames"] and codes == [protocol.EXIT_HELD]
    assert 2.5 < took < 15.0, took


def test_a_caller_that_never_starts_or_never_names_a_game_is_let_go_at_the_hold_not_at_its_own_wait(game, monkeypatch):  # noqa: F811
    codes = _codes(monkeypatch)
    with running_host(hold_s=1.0) as (host, name, _stop, _out):
        # Play's wait for a game: 30 s, and the relay's slack past it; the running game's wait for its start: 5 minutes.
        # The second caller reads only well after the hold: the service waits for it to read why before letting go.
        for ask, read_after_s in ((ask_bytes(folders=[r"D:\Games\Blue"], wait_s=30.0), 0.0), (ask_bytes(game), 3.0)):
            with pipe.connect(name, 20.0) as c:
                c.write(ask, 5.0)
                asked = time.monotonic()
                assert messages(c, "hello")[-1]["t"] == "hello"
                time.sleep(read_after_s)
                got = messages(c, "end", timeout=15.0)
                took = time.monotonic() - asked
            assert got == [{"t": "error", "why": server.HELD}, {"t": "end"}], got
            assert took < 10.0, took
    assert codes == [protocol.EXIT_HELD, protocol.EXIT_HELD]


def test_a_hold_that_ends_after_the_recording_changes_nothing(game, monkeypatch):  # noqa: F811
    codes = _codes(monkeypatch)
    with running_host(hold_s=60.0) as (host, name, _stop, _out):
        with pipe.connect(name, 10.0) as c:
            c.write(ask_bytes(game), 5.0)
            messages(c, "hello")
            c.write(protocol.start(clock.now()), 5.0)
            time.sleep(0.3)
            c.write(protocol.stop(), 5.0)
            got = messages(c, "end")
        assert not [m for m in got if m["t"] == "error"], got
        monkeypatch.setattr(server, "GAME_SLACK_S", 0.3)
        with pipe.connect(name, 20.0) as c:               # Play's own wait ends first, and says so in its own words
            c.write(ask_bytes(folders=[r"D:\Games\Blue"], wait_s=0.5), 5.0)
            messages(c, "hello")
            got = messages(c, "end", timeout=15.0)
        assert got == [{"t": "error", "why": "no game the relay could record started in time"}, {"t": "end"}], got
    assert codes == [protocol.EXIT_OK, protocol.EXIT_NO_CLIENT]


# ---------------------------------------------------------------------------- the machine-wide trace lock ---

MINE = {elevation.token_user_sid()}         # trusted: this account's own locks read as lag-doctor recordings here


def test_a_caller_is_refused_while_another_recording_holds_the_trace_lock_and_served_once_it_is_free(game):  # noqa: F811
    other = tr.own_trace_lock(MINE)              # an elevated relay's recording in another Windows session, say
    assert other.acquire() == tracelock.HELD
    stops: list[str] = []
    parts = tr.parts(frames=lambda cfg, forward, alive: tr.FakeFrames(cfg, forward, alive, stops=stops),
                     etw=lambda on_event: tr.FakeEtw(on_event, [], stops=stops))
    try:
        with running_host(parts=parts, trace_lock=lambda: tr.own_trace_lock(MINE)) as (host, name, _stop, _out):
            why = refusal(name, ask_bytes(game, frames=True))
            assert "another lag-doctor recording on this PC" in why and "would stop it" in why, why
            assert host.served == 0 and stops == []
            other.release()
            with pipe.connect(name, 10.0) as c:
                c.write(ask_bytes(game, frames=True), 5.0)
                messages(c, "hello")
                assert tracelock.check(trusted=MINE) == tracelock.BUSY      # this caller's recording holds it
                c.write(protocol.start(clock.now()), 5.0)
                messages(c, "rows")
                c.write(protocol.stop(), 5.0)
                messages(c, "end")
            tr._until(lambda: tracelock.check(trusted=MINE) == tracelock.FREE, 15, "the lock to be let go")
            assert host.served == 1 and sorted(stops) == ["frames", "net"]
    finally:
        other.release()


def test_the_trace_lock_is_the_last_check_and_let_go_when_the_recording_fails(game, monkeypatch):  # noqa: F811
    made: list = []

    def lock():
        made.append(tr.own_trace_lock(MINE))
        return made[-1]

    with running_host(presentmon_exe=None, trace_lock=lock) as (host, name, _stop, _out):
        assert "no PresentMon" in refusal(name, ask_bytes(game, frames=True))
        assert made == []                         # refused before it: never taken for that caller

    def broken(self):
        assert tracelock.check(trusted=MINE) == tracelock.BUSY
        raise RuntimeError("the core broke")

    monkeypatch.setattr(server.Relay, "serve", broken)
    with running_host(trace_lock=lock) as (host, name, _stop, _out):
        assert "the core broke" in refusal(name, ask_bytes(game), read_after_s=0.5)
        tr._until(lambda: tracelock.check(trusted=MINE) == tracelock.FREE, 15, "the lock to be let go")
    assert len(made) == 1


def test_a_trace_lock_windows_cannot_answer_about_refuses_the_caller_with_why(game):  # noqa: F811
    calls: list[str] = []
    broken = OSError(None, "a device attached to the system is not functioning", None, 31)
    with running_host(trace_lock=lambda: tr.StandInTraceLock(broken, calls)) as (host, name, _stop, _out):
        why = refusal(name, ask_bytes(game))
        assert "could not check whether another recording runs on this PC" in why and "not functioning" in why, why
        assert host.served == 0 and calls == ["acquire"]


def test_another_programs_hold_on_the_lock_name_does_not_stop_the_service_recording(game):  # noqa: F811
    calls: list[str] = []
    with running_host(trace_lock=lambda: tr.StandInTraceLock(tracelock.TAKEN, calls)) as (host, name, _stop, _out):
        with pipe.connect(name, 10.0) as c:
            c.write(ask_bytes(game), 5.0)
            assert messages(c, "hello")[-1]["t"] == "hello"
            c.write(protocol.stop(), 5.0)
            messages(c, "end")
        tr._until(lambda: calls == ["acquire", "release"], 15, "the lock to be let go")
        assert host.served == 1


def test_the_services_trace_lock_is_owned_by_its_own_service_sid_by_default():
    assert svc.Host(tr.parts()).trace_lock().owner == scm.service_sid(protocol.SERVICE_NAME)


def test_a_relay_core_that_cannot_be_made_once_the_lock_is_held_lets_it_go(game, monkeypatch):  # noqa: F811
    def broken(self, *a, **k):
        assert tracelock.check(trusted=MINE) == tracelock.BUSY          # taken just before
        raise RuntimeError("no relay core")

    monkeypatch.setattr(server.Relay, "__init__", broken)
    with running_host(trace_lock=lambda: tr.own_trace_lock(MINE)) as (host, name, _stop, _out):
        assert "could not judge what was asked" in refusal(name, ask_bytes(game))
        assert tracelock.check(trusted=MINE) == tracelock.FREE and host.served == 0
