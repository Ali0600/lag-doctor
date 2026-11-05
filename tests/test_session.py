"""The capture session, with fake collectors: where results land, one clock, and how a capture ends."""
from __future__ import annotations

import threading
import time

import pytest

from lagdoctor import clock
from lagdoctor.capture.base import Collector
from lagdoctor.capture.session import Session, SessionOptions, session_dir, status_line
from lagdoctor.model import Capture

TARGET = {"pid": 4242, "create_time": 1, "name": "Game.exe", "exe": r"C:\Games\Game.exe", "adapter_luid": None}
INVENTORY = {"os": {"name": "Windows 11 Home", "build": 26200}, "adapters": []}


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


def test_session_dir_never_reuses_a_folder(tmp_path):
    from datetime import datetime
    when = datetime(2026, 11, 4, 18, 0, 0)
    a = session_dir(tmp_path, {"name": "My Game.exe"}, when)
    b = session_dir(tmp_path, {"name": "My Game.exe"}, when)
    assert a != b and a.name == "20261104-180000-My_Game" and b.name == "20261104-180000-My_Game-2"
    evil = session_dir(tmp_path, {"name": "a/../../b.exe"}, when)        # a process name is untrusted text
    assert evil.resolve().parent == tmp_path.resolve() and evil.name == "20261104-180000-a_.._.._b"
