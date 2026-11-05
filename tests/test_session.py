"""The capture session, with fake collectors: where results land, one clock, and how a capture ends.

Also lag-doctor's own processes (target["tool_pids"]), and scripts/e2e.py's use of the one-recording lock.
"""
from __future__ import annotations

import _winapi
import importlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

from lagdoctor import clock
from lagdoctor.capture import session as sess
from lagdoctor.capture.base import Collector
from lagdoctor.capture.session import Session, SessionOptions, session_dir, status_line
from lagdoctor.model import Capture
from lagdoctor.winapi import ntquery

TARGET = {"pid": 4242, "create_time": 1, "name": "Game.exe", "exe": r"C:\Games\Game.exe", "adapter_luid": None}
INVENTORY = {"os": {"name": "Windows 11 Home", "build": 26200}, "adapters": []}
BASE_PY = getattr(sys, "_base_executable", sys.executable)
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


class Fake(Collector):
    def __init__(self, name, result=None, live=None, result_raises=False, stop_delay=0.0, period=0.02):
        super().__init__()
        self.name = name
        self._result = result or {}
        self._live_values = live or {}
        self.result_raises = result_raises
        self.stop_delay = stop_delay
        self.period = period
        self.started_with = None
        self.servers = []

    def start(self, origin):
        self.started_with = origin
        super().start(origin)

    def run(self):
        self.set_status("ok", tool="fake")
        for _ in self.ticks(self.period):
            self.publish(**self._live_values)

    def on_stop(self):
        time.sleep(self.stop_delay)

    def result(self):
        if self.result_raises:
            raise RuntimeError("boom")
        return self._result

    def set_server(self, ip):
        self.servers.append(ip)


class NoHotkey:
    def __init__(self, hotkey, on_press):
        self.on_press = on_press
        self.error = "taken by another program"

    def start(self):
        return False

    def stop(self):
        pass


class PressAt:
    """A hotkey listener that 'presses' once, at a chosen clock time, as soon as it starts."""

    def __init__(self, when_after_start):
        self.when_after_start = when_after_start

    def __call__(self, hotkey, on_press):
        outer = self

        class L:
            error = None

            def start(self_inner):
                on_press(clock.now() + outer.when_after_start)
                return True

            def stop(self_inner):
                pass
        return L()


def run_session(collectors, duration=0.2, alive=lambda: True, hotkey_factory=NoHotkey, opts=None, stop=None):
    opts = opts or SessionOptions(duration_s=duration, hotkey="ctrl+alt+l")
    s = Session(TARGET, opts, collectors=collectors, inventory=INVENTORY, alive=alive,
                hotkey_factory=hotkey_factory, printer=lambda line: None)
    safety = threading.Timer(5.0, (stop or threading.Event()).set) if stop else None
    if safety:
        safety.start()
    try:
        return s, s.run(stop)
    finally:
        if safety:
            safety.cancel()


def full_set():
    return [
        Fake("frames", result={"columns": {"t": [0.1], "frame_ms": [16.7]}, "swap_chains": {"0x1": {"frames": 1}},
                               "main_chain": "0x1", "presentmon": {"version": "2.6.0", "exit_code": 0}}),
        Fake("system", result={"columns": {"t": [0.0], "cpu_total_pct": [12.0]}, "others": [[0.0, [[7, "x.exe", 50.0, 0, 0]]]],
                               "game_adapter_luid": "0x00000000_0x00010f83", "game_pids": {4242, 4243}}),
        Fake("gpu", result={"columns": {"t": [0.0], "nv_util_pct": [90.0]}, "device": {"name": "RTX 3070"}}),
        Fake("network", result={"columns": {"t": [0.0], "gw_rtt_ms": [2.0]}, "targets": {"gateway": "192.168.0.1"},
                                "probes": {"gateway": {"sent": 1, "lost": 0, "from_t": 0.0}}, "lost": {"gateway": []},
                                "wifi_scans": [12.5], "route": {"vpn": False}, "downloads": [],
                                "interface": {"name": "Wi-Fi", "kind": "wifi"}}),
        Fake("net_flows", result={"server": {"ip": "203.0.113.7"}, "talkers": [], "gaps": [[3.0, 300.0, "loss"]],
                                  "server_rx": [], "layout": "unverified", "health": {"events": 10}}),
    ]


def test_results_land_where_the_design_says():
    _, cap = run_session(full_set())
    assert cap.frames == {"t": [0.1], "frame_ms": [16.7]}
    assert cap.target["presentmon"] == {"version": "2.6.0", "exit_code": 0, "swap_chains": {"0x1": {"frames": 1}},
                                        "main_chain": "0x1"}
    assert cap.system == {"t": [0.0], "cpu_total_pct": [12.0]}
    assert cap.others == [[0.0, [[7, "x.exe", 50.0, 0, 0]]]]
    assert cap.target["adapter_luid"] == "0x00000000_0x00010f83" and cap.target["pids"] == [4242, 4243]
    assert cap.gpu == {"t": [0.0], "nv_util_pct": [90.0]} and cap.inventory["nvidia"] == {"name": "RTX 3070"}
    assert cap.network == {"t": [0.0], "gw_rtt_ms": [2.0]} and cap.inventory["network"]["kind"] == "wifi"
    assert cap.net_flows["targets"] == {"gateway": "192.168.0.1"} and cap.net_flows["wifi_scans"] == [12.5]
    assert cap.net_flows["server"] == {"ip": "203.0.113.7"} and cap.net_flows["gaps"] == [[3.0, 300.0, "loss"]]
    assert cap.net_flows["layout"] == "unverified" and cap.net_flows["health"] == {"events": 10}
    assert set(cap.sources) == {"frames", "system", "gpu", "network", "net_flows"}
    assert all(st.state == "ok" for st in cap.sources.values())


def test_every_collector_gets_the_same_origin():
    cols = full_set()
    s, cap = run_session(cols)
    assert {c.started_with for c in cols} == {s.origin} and cap.clock_origin == s.origin


def test_a_crashing_result_marks_only_that_source_failed():
    cols = full_set()
    cols[2].result_raises = True
    _, cap = run_session(cols)
    assert cap.sources["gpu"].state == "failed" and "boom" in cap.sources["gpu"].detail
    assert cap.gpu == {} and cap.frames == {"t": [0.1], "frame_ms": [16.7]}
    assert cap.sources["frames"].state == "ok"


def test_a_collector_that_cannot_start_does_not_stop_the_capture():
    class Broken(Fake):
        def start(self, origin):
            raise OSError("no such device")
    cols = full_set()[:2] + [Broken("gpu")]
    _, cap = run_session(cols)
    assert cap.sources["gpu"].state == "failed" and "no such device" in cap.sources["gpu"].detail
    assert cap.sources["system"].state == "ok"


def test_the_duration_ends_the_capture():
    stop = threading.Event()
    s, cap = run_session(full_set(), duration=0.3, stop=stop)
    assert s.stop_reason == "duration"
    assert 0.3 <= cap.duration_s < 1.5


def test_the_game_exiting_ends_the_capture():
    calls = []

    def alive():
        calls.append(1)
        return len(calls) < 3
    stop = threading.Event()
    s, cap = run_session(full_set(), duration=None, alive=alive,
                         opts=SessionOptions(duration_s=None, hotkey=None), stop=stop)
    assert s.stop_reason == "game exited"
    assert any("exited" in n for n in cap.notes)


def test_ctrl_c_ends_the_capture_and_still_assembles_it():
    def alive():
        raise KeyboardInterrupt
    s, cap = run_session(full_set(), duration=None, alive=alive, opts=SessionOptions(duration_s=None, hotkey=None))
    assert s.stop_reason == "Ctrl+C" and cap.frames


def test_marks_are_on_the_capture_clock():
    _, cap = run_session(full_set(), duration=0.3, hotkey_factory=PressAt(0.25))
    assert len(cap.marks) == 1 and cap.marks[0] == pytest.approx(0.25, abs=0.05)


def test_a_hotkey_that_cannot_be_registered_is_noted():
    _, cap = run_session(full_set(), hotkey_factory=NoHotkey)
    assert any("could not be registered" in n and "taken by another program" in n for n in cap.notes)
    assert cap.marks == []


def test_a_server_found_by_the_flow_tracker_is_pinged_too():
    cols = full_set()
    cols[4]._live_values = {"server_ip": "203.0.113.7"}
    stop = threading.Event()
    run_session(cols, duration=1.5, stop=stop)
    assert cols[3].servers == ["203.0.113.7"]           # routed once, not every second


def test_collectors_stop_in_parallel():
    cols = [Fake(f"c{i}", stop_delay=0.8) for i in range(3)]
    t0 = time.perf_counter()
    run_session(cols, duration=0.05)
    assert time.perf_counter() - t0 < 2.0, "three 0.8 s stops took as long as running them one after another"


def test_skipped_frame_timing_is_reported_not_silent():
    opts = SessionOptions(duration_s=0.1, hotkey=None, frames=False)
    _, cap = run_session(full_set()[1:], opts=opts)
    assert cap.sources["frames"].state == "off" and "admin rights" in cap.sources["frames"].detail


def test_the_capture_survives_a_save_and_load(tmp_path):
    _, cap = run_session(full_set())
    back = Capture.load(cap.save(tmp_path / "c.ldcap.json.gz"))
    assert back.net_flows["gaps"] == [[3.0, 300.0, "loss"]] and back.target["pids"] == [4242, 4243]


def test_status_line_literal_points():
    line = status_line(75.4, {"frames": {"fps": 143.6}, "system": {"game_top_thread_pct": 61.0, "game_gpu_pct": 97.2},
                              "network": {"inet_rtt_ms": 24.4}}, "ctrl+alt+l")
    assert line.startswith("1:15 | 144 fps | main thread 61% | GPU 97% | ping 24 ms")
    assert "Ctrl+Alt+L = it lagged now" in line
    assert status_line(0, {}, None).startswith("0:00 | fps - | main thread - | GPU - | ping -")


INTEL, NVIDIA = "0x00000000_0x00010c49", "0x00000000_0x00010f83"
HYBRID = {"adapters": [{"luid": INTEL, "vendor": "intel", "integrated": True},
                       {"luid": NVIDIA, "vendor": "nvidia", "integrated": False}]}


def names_of(target, inventory, **opts):
    from lagdoctor.capture.session import default_collectors
    return [c.name for c in default_collectors(target, inventory, SessionOptions(**opts))]


def test_default_collectors_follow_the_options_and_the_games_adapter():
    import os
    game = {"pid": os.getpid(), "create_time": 1, "name": "python.exe"}
    assert names_of(dict(game, adapter_luid=NVIDIA), HYBRID) == ["system", "frames", "gpu", "network", "net_flows"]
    assert names_of(dict(game, adapter_luid=None), HYBRID) == ["system", "frames", "gpu", "network", "net_flows"]
    # A game on the Intel GPU: never poll (and maybe wake) the NVIDIA one.
    assert names_of(dict(game, adapter_luid=INTEL), HYBRID) == ["system", "frames", "network", "net_flows"]
    assert names_of(dict(game, adapter_luid=None), {"adapters": []}) == ["system", "frames", "network", "net_flows"]
    assert names_of(dict(game, adapter_luid=NVIDIA), HYBRID, network=False, frames=False) == ["system", "gpu"]


def test_the_steam_log_and_its_sources_reach_the_capture():
    import os
    from lagdoctor.capture.session import default_collectors
    game = {"pid": os.getpid(), "create_time": 1, "name": "python.exe", "adapter_luid": None}
    net = next(c for c in default_collectors(game, {"adapters": []}, SessionOptions(steam_log=""))
               if c.name == "network")
    assert net._steam_arg == ""                       # "" = none: never this account's HKCU
    cols = full_set()
    cols[3]._result = dict(cols[3]._result, downloads=None, download_sources=[])
    _, cap = run_session(cols)
    assert cap.net_flows["downloads"] is None and cap.net_flows["download_sources"] == []


def test_one_unanswerable_liveness_check_does_not_end_the_capture(monkeypatch):
    from lagdoctor.capture import session as sess
    from lagdoctor.capture import target as target_mod

    def broken(pid, created):
        raise OSError("NtQuerySystemInformation failed")
    monkeypatch.setattr(target_mod, "is_alive", broken)
    assert sess._default_alive({"pid": 5, "create_time": 1})() is True
    monkeypatch.setattr(target_mod, "is_alive", lambda pid, created: False)
    assert sess._default_alive({"pid": 5, "create_time": 1})() is False


def test_inventory_sections_that_failed_are_noted():
    s = Session(TARGET, SessionOptions(duration_s=0.05, hotkey=None), collectors=full_set(),
                inventory={"errors": {"displays": "QueryDisplayConfig failed"}}, alive=lambda: True,
                printer=lambda line: None)
    cap = s.run()
    assert "System details: displays could not be read (QueryDisplayConfig failed)." in cap.notes


# ------------------------------------------------- Ctrl+C, and stopping whatever happens (LC-1, CC-4) ---

def _session(cols, opts=None, printer=None):
    return Session(TARGET, opts or SessionOptions(duration_s=0.1, hotkey=None), collectors=cols,
                   inventory=INVENTORY, alive=lambda: True, hotkey_factory=NoHotkey,
                   printer=printer or (lambda line: None))


def _wait_for(pred, seconds=3.0):
    deadline = time.monotonic() + seconds
    while not pred() and time.monotonic() < deadline:
        time.sleep(0.02)
    return pred()


class PressWhileStopping(Fake):
    """The user presses Ctrl+C again while this collector is being stopped (SIGINT in this process only)."""

    def on_stop(self):
        signal.raise_signal(signal.SIGINT)
        time.sleep(0.5)


def test_a_second_ctrl_c_while_stopping_does_not_lose_the_capture():
    cols = full_set()[:2] + [PressWhileStopping("gpu", result={"columns": {"t": [0.0]}})]
    lines = []
    s = _session(cols, printer=lines.append)
    try:
        cap = s.run()
    except KeyboardInterrupt:
        pytest.fail("Ctrl+C while the collectors stopped escaped Session.run(): the capture was lost")
    assert cap.frames == {"t": [0.1], "frame_ms": [16.7]} and s.stop_reason == "duration"
    assert sess.STOPPING_MESSAGE + "\n" in lines                          # said when the stop began
    assert _wait_for(lambda: sess.STILL_STOPPING_MESSAGE + "\n" in lines)  # and the press was answered
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler   # the old handler is back


def test_the_first_ctrl_c_stops_the_recording_without_raising():
    calls = []

    def alive():
        calls.append(1)
        if len(calls) == 2:
            signal.raise_signal(signal.SIGINT)                # handled at once, on this (the main) thread
        return True
    s, cap = run_session(full_set(), duration=None, alive=alive, opts=SessionOptions(duration_s=None, hotkey=None),
                         stop=threading.Event())
    assert s.stop_reason == "Ctrl+C" and cap.target["stop_reason"] == "Ctrl+C" and cap.frames


def test_ctrl_c_while_starting_still_stops_what_was_started():
    class Interrupted(Fake):
        def start(self, origin):
            raise KeyboardInterrupt
    cols = full_set()[:2] + [Interrupted("gpu")] + full_set()[3:4]
    try:
        with pytest.raises(KeyboardInterrupt):
            _session(cols).run()
        running = [c.name for c in cols if c._thread is not None]
        assert running == [], f"{running} were started and never stopped"
    finally:
        for c in cols:
            c.stop()


def test_ctrl_c_while_the_hotkey_starts_still_stops_the_collectors():
    class Interrupting:
        def __init__(self, hotkey, on_press):
            pass

        def start(self):
            raise KeyboardInterrupt

        def stop(self):
            pass
    cols = full_set()[:2]
    s = Session(TARGET, SessionOptions(duration_s=0.1, hotkey="ctrl+alt+l"), collectors=cols, inventory=INVENTORY,
                alive=lambda: True, hotkey_factory=Interrupting, printer=lambda line: None)
    try:
        with pytest.raises(KeyboardInterrupt):
            s.run()
        assert [c.name for c in cols if c._thread is not None] == []
    finally:
        for c in cols:
            c.stop()


def test_a_lag_key_that_cannot_be_used_is_noted_and_the_capture_goes_on():
    def bad_key(hotkey, on_press):
        raise ValueError("unknown key 'lag' in 'ctrl+alt+lag'")
    cols = full_set()
    s = Session(TARGET, SessionOptions(duration_s=0.1, hotkey="ctrl+alt+lag"), collectors=cols, inventory=INVENTORY,
                alive=lambda: True, hotkey_factory=bad_key, printer=lambda line: None)
    cap = s.run()
    assert any("could not be used" in n and "unknown key" in n for n in cap.notes)
    assert cap.frames and all(c._thread is None for c in cols)


def test_a_stop_request_carries_its_reason():
    stop = threading.Event()
    sess.request_stop(stop, "window closed")
    sess.request_stop(stop, "Ctrl+C")                          # the first reason wins
    s, cap = run_session(full_set(), duration=None, opts=SessionOptions(duration_s=None, hotkey=None), stop=stop)
    assert s.stop_reason == "window closed" and cap.target["stop_reason"] == "window closed"


def test_a_callers_ctrl_c_guard_must_share_the_stop_event():
    with pytest.raises(ValueError):
        _session(full_set()).run(threading.Event(), ctrl_c=sess.CtrlC(threading.Event()))


def test_a_console_that_cannot_be_written_does_not_end_the_capture():
    def broken(line):
        raise OSError(6, "The handle is invalid")
    cols = full_set()
    cap = _session(cols, printer=broken).run()
    assert cap.frames and all(c._thread is None for c in cols)
    assert any("console could not be written" in n for n in cap.notes)


def test_only_one_capture_lock_at_a_time():
    name = f"Local\\lagdoctor-test-{uuid.uuid4().hex}"
    first, second = sess.CaptureLock(name), sess.CaptureLock(name)
    try:
        assert first.acquire() and first.held
        assert second.acquire() is False and not second.held
        first.release()
        assert second.acquire()                                  # free again once released
    finally:
        first.release()
        second.release()


def test_the_capture_lock_sees_another_process_holding_it():
    name = f"Local\\lagdoctor-test-{uuid.uuid4().hex}"
    holder = subprocess.Popen(
        [sys.executable, "-c", "import ctypes, sys; k = ctypes.WinDLL('kernel32');"
         "k.CreateMutexW.restype = ctypes.c_void_p; h = k.CreateMutexW(None, False, sys.argv[1]);"
         "print('held' if h else 'failed', flush=True); sys.stdin.read()", name],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    lock = sess.CaptureLock(name)
    try:
        assert holder.stdout.readline().strip() == "held"
        assert lock.acquire() is False
        holder.stdin.close()
        holder.wait(10)
        assert lock.acquire()                                    # its handle closed with its process
    finally:
        lock.release()
        if holder.poll() is None:
            holder.kill()


def test_session_dir_names_a_folder_for_any_target_name(tmp_path):
    from datetime import datetime
    d = session_dir(tmp_path, {"name": 12345}, datetime(2026, 11, 4, 18, 0, 0))   # a capture holds any JSON
    assert d.name == "20261104-180000-game"


def test_session_dir_never_reuses_a_folder(tmp_path):
    from datetime import datetime
    when = datetime(2026, 11, 4, 18, 0, 0)
    a = session_dir(tmp_path, {"name": "My Game.exe"}, when)
    b = session_dir(tmp_path, {"name": "My Game.exe"}, when)
    assert a != b and a.name == "20261104-180000-My_Game" and b.name == "20261104-180000-My_Game-2"
    evil = session_dir(tmp_path, {"name": "a/../../b.exe"}, when)        # a process name is untrusted text
    assert evil.resolve().parent == tmp_path.resolve() and evil.name == "20261104-180000-a_.._.._b"


# ------------------------------------------------------------- lag-doctor's own processes (tool_pids) ---

class P:
    def __init__(self, pid, ppid, create_time, name="x.exe"):
        self.pid, self.ppid, self.create_time, self.name = pid, ppid, create_time, name


LAUNCHER, BASE = r"C:\lab\.venv\Scripts\python.exe", r"C:\Python313\python.exe"
TABLE = {p.pid: p for p in (
    P(10, 4, 10, "explorer.exe"),
    P(20, 10, 100, "uv.exe"),
    P(30, 20, 200, "python.exe"),                 # the venv launcher that started lag-doctor
    P(40, 30, 300, "python.exe"),                 # lag-doctor (the base interpreter)
    P(50, 40, 400, "PresentMon-2.6.0-x64.exe"),
    P(60, 40, 410, "nvidia-smi.exe"),
    P(70, 40, 420, "python.exe"),                 # LagLab's venv launcher (scripts/e2e.py starts the game)
    P(80, 70, 430, "python.exe"),                 # the game
    P(90, 80, 440, "hog.exe"),                    # the game's own child
    P(95, 40, 250, "old.exe"),                    # names PID 40 as parent but is older: an earlier PID 40's
    P(100, 4, 50, "python.exe"),                  # the waiting window of an elevated recording
    P(110, 4, 60, "python.exe"),                  # its launcher (handed over below with a stale create time)
    P(120, 10, 70, "python.exe"),                 # someone else's Python
)}
IMAGES = {30: LAUNCHER, 40: BASE, 70: LAUNCHER, 80: BASE, 100: BASE, 110: LAUNCHER, 120: BASE}
GAME_AT_80 = {"pid": 80, "create_time": 430}


def test_lagdoctors_own_processes_literal_points():
    image = IMAGES.get
    assert sess.tool_pids(TABLE, GAME_AT_80, [(100, 50), (110, 61)], me=40, image_path=image,
                          launcher=LAUNCHER) == [40, 30, 100, 50, 60, 70]
    assert sess.own_processes(TABLE, me=40, image_path=image, launcher=LAUNCHER) == [(40, 300), (30, 200)]
    # Not started through the launcher: a parent running our own exe is just a parent.
    assert sess.own_processes(TABLE, me=40, image_path={30: BASE, 40: BASE}.get, launcher=BASE) == [(40, 300)]
    assert sess.own_processes(TABLE, me=40, image_path={30: BASE, 40: BASE}.get, launcher=LAUNCHER) == [(40, 300)]

    def unreadable(pid):
        raise OSError("NtQuerySystemInformation(SystemProcessIdInformation) failed")
    assert sess.own_processes(TABLE, me=40, image_path=unreadable, launcher=LAUNCHER) == [(40, 300)]
    assert sess.tool_pids(TABLE, GAME_AT_80, me=999, image_path=image, launcher=LAUNCHER) == []   # not listed


class Spawner(Fake):
    """A collector that starts a real child process `at` seconds in, living `life` seconds (or until stopped)."""

    def __init__(self, name, at, life=None):
        super().__init__(name, result={"columns": {"t": [0.0]}})
        self.at, self.life, self.child = at, life, None

    def run(self):
        self.set_status("ok", tool="fake")
        for t in self.ticks(0.02):
            if self.child is None and t >= self.at:
                code = f"import time; time.sleep({self.life!r})" if self.life else "import sys; sys.stdin.read()"
                self.child = subprocess.Popen([BASE_PY, "-I", "-c", code], stdin=subprocess.PIPE)

    def on_stop(self):
        if self.child is not None and self.child.poll() is None:
            self.child.kill()


def _parent_exe_by_wmi() -> str:
    """Our parent's exe as WMI reports it: a readout independent of ntquery."""
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                          f"(Get-CimInstance Win32_Process -Filter 'ProcessId={os.getppid()}').ExecutablePath"],
                         capture_output=True, text=True, timeout=60)
    return out.stdout.strip()


def test_a_capture_lists_lagdoctors_own_processes_and_never_the_games():
    # The game here is a child of this process with a child of its own, as LagLab is under scripts/e2e.py.
    game = subprocess.Popen([BASE_PY, "-I", "-c",
                             "import subprocess, sys; c = subprocess.Popen([sys.executable, '-I', '-c', "
                             "'import sys; sys.stdin.read()'], stdin=subprocess.PIPE); print(c.pid, flush=True); "
                             "sys.stdin.read()"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    early = Spawner("frames", at=0.0, life=1.6)       # seen at the first listing (~1 s in), gone by the end
    late = Spawner("gpu", at=1.8)                     # started after it, running until the stop
    try:
        helper = int(game.stdout.readline())
        created = ntquery.Snapshotter().snapshot()[game.pid].create_time
        target = {"pid": game.pid, "create_time": created, "name": "python.exe"}
        s = Session(target, SessionOptions(duration_s=3.0, hotkey=None), collectors=[early, late],
                    inventory=INVENTORY, alive=lambda: True, printer=lambda line: None)
        cap = s.run()
    finally:
        game.stdin.close()                            # the game ends, and its child with it
        game.wait(10)
        for c in (early.child, late.child):
            if c is not None:
                c.wait(10)
    pids = cap.target["tool_pids"]
    assert early.child is not None and late.child is not None
    assert os.getpid() in pids and early.child.pid in pids and late.child.pid in pids, pids
    assert game.pid not in pids and helper not in pids, pids
    parent_is_launcher = os.path.normcase(_parent_exe_by_wmi()) == os.path.normcase(sys.executable)
    assert (os.getppid() in pids) is parent_is_launcher, (pids, os.getppid(), parent_is_launcher)


def test_a_snapshot_that_fails_leaves_no_tool_list_and_says_why():
    def broken():
        raise OSError("NtQuerySystemInformation failed")
    s = Session(TARGET, SessionOptions(duration_s=0.7, hotkey=None), collectors=full_set(), inventory=INVENTORY,
                alive=lambda: True, printer=lambda line: None, processes=broken)
    cap = s.run()
    assert "tool_pids" not in cap.target and cap.frames
    assert any("could not list its own processes" in n and "NtQuerySystemInformation failed" in n
               for n in cap.notes), cap.notes


def test_the_tool_list_is_bounded_and_keeps_lagdoctors_own_process():
    me = os.getpid()
    table = {me: P(me, 1, 1)}
    table.update({p: P(p, me, 2) for p in range(1_000_000, 1_000_000 + 4 * 100, 4)})    # 100 children
    s = Session(TARGET, SessionOptions(duration_s=0.05, hotkey=None), collectors=full_set(), inventory=INVENTORY,
                alive=lambda: True, printer=lambda line: None, processes=lambda: table)
    pids = s.run().target["tool_pids"]
    assert len(pids) == sess.MAX_TOOL_PIDS and me in pids


def test_handed_over_processes_count_only_while_they_are_the_same_processes():
    table = dict(TABLE)
    s = Session(GAME_AT_80, SessionOptions(duration_s=0.05, hotkey=None, tool_procs=((100, 50), (110, 61))),
                collectors=full_set(), inventory=INVENTORY, alive=lambda: True, printer=lambda line: None,
                processes=lambda: table)
    me = os.getpid()
    table[me] = P(me, 1, 1)                     # this process, as the snapshot would list it
    cap = s.run()
    assert cap.target["tool_pids"] == sorted({me, 100}), cap.target["tool_pids"]


# --------------------------------------------------------------------- scripts/e2e.py and the lock ---

def _e2e():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module("e2e")


class FakeLab:
    """LagLab's process: writes its pid file and log at once, and has already ended."""

    started: list = []

    def __init__(self, argv, cwd=None, env=None):
        FakeLab.started.append(list(argv))
        Path(argv[argv.index("--pid-file") + 1]).write_text(json.dumps({"pid": 4242}), encoding="utf-8")
        Path(argv[argv.index("--log") + 1]).write_text(json.dumps({"gl_renderer": "fake"}), encoding="utf-8")

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


@pytest.fixture
def e2e_world(monkeypatch, tmp_path):
    """scripts/e2e.py with ETW rights, a fake LagLab, and a session that notes whether the lock was held."""
    e2e = _e2e()
    from lagdoctor import setup as pm_setup
    from lagdoctor.capture import target as tgt
    from lagdoctor.winapi import elevation
    monkeypatch.setattr(sess, "CAPTURE_LOCK_NAME", f"Local\\lagdoctor-test-{uuid.uuid4().hex}")
    monkeypatch.setattr(e2e.subprocess, "Popen", FakeLab)
    monkeypatch.setattr(tgt, "find_target", lambda **kw: dict(TARGET))
    monkeypatch.setattr(pm_setup, "ensure_presentmon", lambda allow_download=False: tmp_path / "pm.exe")
    monkeypatch.setattr(elevation, "can_trace", lambda: True)
    FakeLab.started = []
    held: list[bool] = []

    class Probe(Session):
        def __init__(self, target, opts, **kw):
            super().__init__(target, opts, collectors=full_set(), inventory=INVENTORY, alive=lambda: False,
                             printer=lambda line: None)

        def run(self, *a, **k):
            other = sess.CaptureLock()
            held.append(not other.acquire())          # True: the run held the lock while it recorded
            other.release()
            return super().run(*a, **k)
    monkeypatch.setattr(sess, "Session", Probe)
    return e2e, held


def test_the_end_to_end_run_records_under_the_capture_lock_and_lets_it_go(e2e_world, tmp_path):
    e2e, held = e2e_world
    e2e.main(["--modes", "smooth", "--duration", "0.1", "--out", str(tmp_path / "e2e")])
    assert held == [True] and len(FakeLab.started) == 1
    after = sess.CaptureLock()
    assert after.acquire()                            # let go once the mode was recorded
    after.release()
    results = json.loads((tmp_path / "e2e" / "e2e-results.json").read_text(encoding="utf-8"))
    assert [r["mode"] for r in results] == ["smooth"] and "error" not in results[0], results


def test_the_end_to_end_run_refuses_plainly_while_another_recording_runs(e2e_world, tmp_path, capsys):
    e2e, held = e2e_world
    other = sess.CaptureLock()
    assert other.acquire()
    try:
        code = e2e.main(["--modes", "smooth,hitch", "--duration", "0.1", "--out", str(tmp_path / "e2e")])
    finally:
        other.release()
    out = capsys.readouterr().out
    assert code == 1 and "Another lag-doctor recording is running" in out and "Traceback" not in out
    assert FakeLab.started == [] and held == []       # LagLab never started; nothing recorded
    results = json.loads((tmp_path / "e2e" / "e2e-results.json").read_text(encoding="utf-8"))
    assert [r["mode"] for r in results] == ["smooth"] and "Another lag-doctor recording" in results[0]["error"]


def _split_command_line(params: str) -> list[str]:
    """How the elevated child's command line is split (CommandLineToArgvW), without the program."""
    import ctypes
    from ctypes import wintypes
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    k32 = ctypes.WinDLL("kernel32")
    k32.LocalFree.argtypes = [ctypes.c_void_p]
    n = ctypes.c_int()
    argv = shell32.CommandLineToArgvW(f"program.exe {params}", ctypes.byref(n))
    assert argv, f"CommandLineToArgvW failed: {ctypes.get_last_error()}"
    try:
        return [argv[i] for i in range(1, n.value)]
    finally:
        k32.LocalFree(ctypes.cast(argv, ctypes.c_void_p))


class ShellExecuteRecorder:
    """Stands in for ShellExecuteExW behind the real relaunch_elevated: records the command, and hands back the
    handle of a plain child process that exits at once."""

    def __init__(self):
        self.params: list[str] = []
        self.children: list[subprocess.Popen] = []

    def __call__(self, ref):
        info = ref._obj
        self.params.append(info.lpParameters)
        child = subprocess.Popen([BASE_PY, "-I", "-c", "pass"])
        self.children.append(child)
        me = _winapi.GetCurrentProcess()
        info.hProcess = _winapi.DuplicateHandle(me, child._handle, me, 0, False, _winapi.DUPLICATE_SAME_ACCESS)
        return True


def test_the_end_to_end_relaunch_hands_over_an_absolute_folder_and_no_empty_argument(monkeypatch, tmp_path):
    e2e = _e2e()
    from lagdoctor.winapi import elevation
    rec = ShellExecuteRecorder()
    monkeypatch.setattr(elevation, "_ShellExecuteExW", rec)
    monkeypatch.setattr(elevation, "can_trace", lambda: False)
    monkeypatch.chdir(tmp_path)
    try:
        e2e.main(["--modes", "smooth", "--duration", "0.1", "--out", "rel"])
    finally:
        for c in rec.children:
            c.wait(10)
    (params,) = rec.params
    argv = _split_command_line(params)
    out = argv[argv.index("--out") + 1]
    assert Path(out).is_absolute() and Path(out) == (tmp_path / "rel").resolve(), params
    assert all(argv), argv
    with pytest.raises(SystemExit):
        e2e.parse(["--modes", " , "])               # no modes: it would be an empty argument when elevated


def test_an_end_to_end_relaunch_that_cannot_start_says_so_plainly(monkeypatch, tmp_path, capsys):
    e2e = _e2e()
    from lagdoctor.winapi import elevation

    def never(ref):
        raise AssertionError("a refused command reached ShellExecuteExW")
    monkeypatch.setattr(elevation, "_ShellExecuteExW", never)
    monkeypatch.setattr(elevation, "can_trace", lambda: False)
    monkeypatch.setattr(sys, "executable", "")           # Python could not tell its own path
    assert e2e.main(["--modes", "smooth", "--duration", "0.1", "--out", str(tmp_path / "e2e")]) == 3
    out = capsys.readouterr().out
    assert "could not be started" in out and "Traceback" not in out
