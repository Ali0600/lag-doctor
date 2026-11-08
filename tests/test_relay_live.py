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
