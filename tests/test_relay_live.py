"""The real relay, started the way `lagdoctor watch --elevate` starts it: on GitHub's runner only.

There the account is an administrator with UAC off (CLAUDE.md), so the real launch_elevated starts the relay under
pythonw with administrator rights and no prompt, its pipe is really owned by Administrators, and its PresentMon and
Kernel-Network session are real. Anywhere else this test is skipped before anything starts: on a PC someone uses it
would raise a UAC prompt (scripts/stage2/guard.py says how the runner is told apart).
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from stage2 import guard  # noqa: E402

from lagdoctor import clock  # noqa: E402
from lagdoctor.capture import netflows as nf  # noqa: E402
from lagdoctor.relay import client, protocol  # noqa: E402
from lagdoctor.winapi import elevation, ntquery  # noqa: E402

BASE_PY = getattr(sys, "_base_executable", sys.executable)
SENT = 40                                    # UDP datagrams this process sends while the relay listens
pytestmark = pytest.mark.skipif(not guard.on_throwaway_runner(),
                                reason="starts the relay with administrator rights: GitHub's runner only")


def _until(pred, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.1)


def test_the_real_relay_runs_presentmon_and_the_network_trace_for_this_process(presentmon_exe):
    assert elevation.is_elevated(), "GitHub's runner account is an administrator with UAC off"
    snap = ntquery.Snapshotter()
    game = subprocess.Popen([BASE_PY, "-I", "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
    link = None
    try:
        _until(lambda: game.pid in snap.snapshot(), 10, "the stand-in game")
        procs = snap.snapshot()
        target = {"pid": game.pid, "create_time": procs[game.pid].create_time, "name": "python.exe"}
        me = procs[os.getpid()]
        link = client.launch(target, presentmon_exe=Path(presentmon_exe).resolve(), frames=True, network=True,
                             recorder=(me.pid, me.create_time), sid=elevation.token_user_sid())
        relay = snap.snapshot().get(link.pid)
        assert link.pid != os.getpid() and relay is not None and relay.name.lower() == "pythonw.exe", relay
        assert link._client.owner_sid() == client.ADMINISTRATORS
        frames = link.frames_collector(game.pid)
        tracker = nf.FlowTracker(lambda: {game.pid}, selftest=False, relay=link)
        origin = clock.now()
        frames.start(origin)
        tracker.start(origin)
        _until(lambda: tracker.status().state == "ok", 20, "the relay's network trace to start")
        _until(lambda: any(p.ppid == link.pid and "presentmon" in p.name.lower()
                           for p in snap.snapshot().values()), 20, "the relay's PresentMon")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            for _ in range(SENT):                        # to a documentation address: nothing need answer
                s.sendto(b"x" * 100, ("203.0.113.1", 9))
                time.sleep(0.02)

        def mine() -> int:                               # any rank: the runner's own programs talk too
            with tracker._lock:
                return (tracker._table._all_talkers().get(os.getpid()) or [0, 0])[1]
        _until(lambda: mine() >= SENT * 100, 20, "this process's own traffic, through the relay")
        frames.stop()
        tracker.stop()
    finally:
        code = link.close(wait_s=5) if link is not None else None
        game.stdin.close()
        game.wait(10)
    r = tracker.result()
    st = tracker.status()
    assert "stopped by itself" not in st.detail and r["health"]["consumer_stopped_t"] is None, st
    assert r["talkers_scope"] == "session" and r["health"]["relayed_events"] >= SENT, r["health"]
    assert r["health"]["relay"]["events_lost"] == 0 and r["health"]["relay"]["rows_lost"] == 0
    st = frames.status()
    # The stand-in game never draws: PresentMon ran with rights, saw no frame, and stopped cleanly without a console.
    # On a process that never presents, PresentMon writes no CSV at all, not even its header (measured on the runner,
    # PR #23), so the detail is "wrote no CSV" there; "no frames came from PID" would mean a header came first.
    assert st.state == "failed" and st.detail.startswith("PresentMon ran but"), st
    info = frames.result()["presentmon"]
    assert info["exit_code"] == 0 and info["stop_path"] != "kill", info
    assert info["relay"]["pid"] == link.pid and info["relay"]["rows_received"] == 0
    assert code == protocol.EXIT_OK


def _lock_owner() -> str | None:
    """The owner of this PC's trace lock, read with the rights everyone has on it (tracelock.OTHERS); None when there is
    no lock of that name."""
    from lagdoctor.capture import tracelock
    handle = elevation.open_mutex(tracelock.NAME, tracelock.OTHERS)
    if handle is None:
        return None
    try:
        return elevation.mutex_owner(handle)
    finally:
        elevation.release_named_mutex(handle)


@pytest.mark.real_trace_lock
def test_a_second_real_relay_ends_busy_before_its_pipe_while_the_first_holds_this_pcs_trace_lock(presentmon_exe):
    """The machine-wide trace lock under its real name, the one the real relay takes (tests/conftest.py leaves it to
    this test, on the runner only): busy and owned by Administrators while one relay records, so a second relay for the
    same game ends with EXIT_BUSY before it makes its pipe, and free once the first has ended."""
    from lagdoctor.capture import tracelock
    from tests import conftest
    assert elevation.is_elevated(), "GitHub's runner account is an administrator with UAC off"
    assert tracelock.NAME == conftest.REAL_TRACE_LOCK, "the conftest guard did not leave this test the real name"
    _until(lambda: tracelock.check() == tracelock.FREE, 15, "this PC's trace lock to be free before the first relay")
    snap = ntquery.Snapshotter()
    game = subprocess.Popen([BASE_PY, "-I", "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
    first = second = busy = None
    during: tuple = ()
    try:
        _until(lambda: game.pid in snap.snapshot(), 10, "the stand-in game")
        procs = snap.snapshot()
        target = {"pid": game.pid, "create_time": procs[game.pid].create_time, "name": "python.exe"}
        me = procs[os.getpid()]
        how = dict(presentmon_exe=Path(presentmon_exe).resolve(), frames=True, network=True,
                   recorder=(me.pid, me.create_time), sid=elevation.token_user_sid())
        first = client.launch(target, **how)
        frames = first.frames_collector(game.pid)
        tracker = nf.FlowTracker(lambda: {game.pid}, selftest=False, relay=first)
        origin = clock.now()
        frames.start(origin)
        tracker.start(origin)
        _until(lambda: tracker.status().state == "ok", 20, "the first relay's network trace to start")
        _until(lambda: any(p.ppid == first.pid and "presentmon" in p.name.lower()
                           for p in snap.snapshot().values()), 20, "the first relay's PresentMon")
        during = (tracelock.check(), _lock_owner())
        try:
            second = client.launch(target, **how)
        except client.RelayFailed as exc:
            busy = exc
        frames.stop()
        tracker.stop()
    finally:
        code = first.close(wait_s=5) if first is not None else None
        if second is not None:
            second.close(wait_s=5)
        game.stdin.close()
        game.wait(10)
    assert during == (tracelock.BUSY, tracelock.ADMINISTRATORS), f"the lock while the first relay recorded: {during}"
    assert second is None, f"a second relay (process {second.pid}) started beside the first"
    assert busy is not None and busy.code == protocol.EXIT_BUSY, (busy.why, busy.code) if busy else None
    assert code == protocol.EXIT_OK
    _until(lambda: tracelock.check() == tracelock.FREE, 10, "this PC's trace lock to be free once the relay ended")


def _detached(argv: list[str]) -> int:
    """Start `argv` through a short-lived process that exits at once, so the new process is nobody's child here (Play's
    folder rule never takes one of lag-doctor's own, and this test is the recorder): its pid."""
    code = ("import subprocess, sys\n"
            "p = subprocess.Popen(sys.argv[1:], creationflags=0x00000008 | 0x00000200, stdin=subprocess.DEVNULL, "
            "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            "print(p.pid)")
    out = subprocess.run([BASE_PY, "-I", "-c", code, *argv], capture_output=True, text=True, timeout=60, check=True)
    return int(out.stdout.strip())


def test_the_real_relay_for_a_folder_judges_each_game_by_its_own_read_and_records_the_one_it_took(presentmon_exe):
    from lagdoctor.capture import target as tgt
    from lagdoctor.winapi import folders as winfolders
    assert elevation.is_elevated(), "GitHub's runner account is an administrator with UAC off"
    snap = ntquery.Snapshotter()
    forms = tgt.folder_forms(os.path.dirname(BASE_PY))           # the stand-in's folder, and its real path
    me = snap.snapshot()[os.getpid()]
    link = client.launch(None, folders=forms, wait_s=60, presentmon_exe=Path(presentmon_exe).resolve(), frames=True,
                         network=True, recorder=(me.pid, me.create_time), sid=elevation.token_user_sid())
    started: list[int] = []
    procs: dict = {}
    try:
        assert link.folders == forms and link.game is None
        relay_ct = snap.snapshot()[link.pid].create_time             # Play's `since`: the relay's, not this one's
        assert link.create_time == relay_ct != me.create_time
        # Both started after the relay, outside this test's tree: one from outside the folder, one from inside it.
        ping = os.path.join(winfolders.system_folders()[0], "PING.EXE")
        started.append(_detached([ping, "-n", "90", "127.0.0.1"]))
        started.append(_detached([BASE_PY, "-I", "-c", "import time; time.sleep(90)"]))
        _until(lambda: all(p in snap.snapshot() for p in started), 20, "the stand-ins")
        procs = snap.snapshot()
        outside, game = ((p, procs[p].create_time) for p in started)
        recorder, relay = (me.pid, me.create_time), (link.pid, link.create_time)
        assert all(p not in tgt.descendants(procs, *root) for p in started for root in (recorder, relay))
        # The recorder's own check is told the program sits in the folder; the relay reads it for itself.
        with pytest.raises(client.GameRefused) as no:
            link.aim(outside, image=lambda pid: forms[0] + "\\lantern.exe")
        assert no.value.sent is True and "is not in the game's folder" in no.value.why, no.value.why
        link.aim(game)
        assert link.game == game
        frames = link.frames_collector(game[0])
        tracker = nf.FlowTracker(lambda: {game[0]}, selftest=False, relay=link)
        origin = clock.now()
        frames.start(origin)
        tracker.start(origin)
        _until(lambda: tracker.status().state == "ok", 20, "the relay's network trace to start")
        _until(lambda: any(p.ppid == link.pid and "presentmon" in p.name.lower()
                           for p in snap.snapshot().values()), 20, "the relay's PresentMon on the game it took")
        frames.stop()
        tracker.stop()
    finally:
        code = link.close(wait_s=5)
        now = snap.snapshot()
        for pid, ct in ((p, procs[p].create_time) for p in started if p in procs):
            if now.get(pid) is not None and now[pid].create_time == ct:       # still the test's own stand-in
                try:
                    os.kill(pid, 9)
                except OSError:
                    pass
    st = frames.status()
    assert st.state == "failed" and st.detail.startswith("PresentMon ran but"), st      # it never draws
    assert frames.result()["presentmon"]["relay"]["pid"] == link.pid
    assert code == protocol.EXIT_OK
