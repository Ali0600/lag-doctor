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


@pytest.mark.parametrize("said, kept", [(True, True), (False, None), ("yes", None), (None, None)])
def test_the_target_says_when_the_folder_check_covered_the_windows_folder(said, kept):
    # rules._windows_own judges explorer.exe by where it runs only when the check looked at the Windows folder
    collectors = full_set()
    if said is not None:
        collectors[1]._result["windows_folder"] = said
    _, cap = run_session(collectors)
    assert cap.target.get("windows_folder") is kept
    assert ("windows_folder" in cap.target) is (kept is not None)


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
    # wording: the reports list it as "frame timing (PresentMon): <reason>", which once began "frame timing was skipped"
    assert not cap.sources["frames"].detail.lower().startswith("frame timing")


def test_network_checks_turned_off_are_reported_as_turned_off():
    opts = SessionOptions(duration_s=0.1, hotkey=None, network=False)
    _, cap = run_session(full_set()[:3], opts=opts)
    for name in ("network", "net_flows"):
        assert cap.sources[name].state == "off" and cap.sources[name].detail == "turned off for this recording " \
                                                                                 "(--no-network)", name
    _, cap = run_session(full_set())                                  # on: whatever the collectors said
    assert cap.sources["network"].state == "ok" and cap.sources["net_flows"].state == "ok"


def test_the_capture_survives_a_save_and_load(tmp_path):
    _, cap = run_session(full_set())
    back = Capture.load(cap.save(tmp_path / "c.ldcap.json.gz"))
    assert back.net_flows["gaps"] == [[3.0, 300.0, "loss"]] and back.target["pids"] == [4242, 4243]


LIVE = {"frames": {"fps": 143.6}, "system": {"game_top_thread_pct": 61.0, "game_gpu_pct": 97.2},
        "network": {"inet_rtt_ms": 24.4}}


def test_status_line_literal_points():
    # Changed on purpose (wording): the keys are in the "Recording." line, printed once; this line once carried them
    # too, 95 characters in a 71-column console
    assert status_line(75.4, LIVE) == "1:15 | 144 fps | main thread 61% | GPU 97% | ping 24 ms"
    # Running but nothing yet: a dash. Not running at all this time (frame timing off, --no-network): "off".
    assert status_line(0, {"frames": {}, "system": {}, "network": {}}) == "0:00 | fps - | main thread - | GPU - | ping -"
    assert status_line(0, {"system": {}}) == "0:00 | fps off | main thread - | GPU - | ping off"


@pytest.mark.parametrize("width, shown", [(71, "1:15 | 144 fps | main thread 61% | GPU 97% | ping 24 ms"),
                                          (54, "1:15 | 144 fps | main thread 61% | GPU 97%"),
                                          (30, "1:15 | 144 fps"), (3, "1:")])
def test_the_status_line_fits_the_console_so_a_carriage_return_rewrites_it(width, shown):
    # Wording: a line as wide as the console wraps, and "\r" then rewrites only its last row while the screen scrolls a
    # row a second. Whole readouts go from the end, and the rest is padded so a shorter line leaves nothing behind
    line = status_line(75.4, LIVE, width)
    assert len(line) == width - 1 and line.rstrip() == shown


def test_the_recording_loop_prints_status_lines_no_wider_than_the_console(monkeypatch):
    import lagdoctor.capture.session as sess
    monkeypatch.setattr(sess, "console_width", lambda: 40)
    lines = []
    s = Session(TARGET, SessionOptions(duration_s=0.3, hotkey="ctrl+alt+l"), collectors=[Fake("system")],
                inventory=INVENTORY, alive=lambda: True, hotkey_factory=NoHotkey, printer=lines.append)
    s.run()
    status = [ln[1:] for ln in lines if ln.startswith("\r")]
    assert status and all(len(ln) == 39 for ln in status), status
    assert not any("Ctrl" in ln for ln in status)


class FramesState(Collector):
    """The frame source as _check_frames sees it: a fixed live readout and state. `finish`: its run ends at once,
    as PresentMon's does once it exits (Collector.ended)."""
    name = "frames"

    def __init__(self, live, state="ok", detail="", finish=False):
        super().__init__()
        self._fixed, self._state, self._finish = dict(live), (state, detail), finish
        self.set_status(state, detail)
        if finish and live:
            self.publish(**live)

    def run(self):
        self.set_status(*self._state)
        if self._finish:
            return
        for _ in self.ticks(0.02):
            if self._fixed:
                self.publish(**self._fixed)


def _ended(src):
    src.start(0.0)
    deadline = time.monotonic() + 5.0
    while not src.ended:
        assert time.monotonic() < deadline, "the collector's run did not end"
        time.sleep(0.01)
    return src


class InFront(Collector):
    """The system sampler as the session sees it live: the game alive and in front the whole time."""
    name = "system"

    def run(self):
        self.set_status("ok", tool="stub")
        for _ in self.ticks(0.05):
            self.publish(game_focused=1, game_alive=True)


def _record_with_presentmon(mode, tmp_path, monkeypatch, seconds):
    """A real Session over the real PresentMon collector running the repo's stand-in PresentMon (its modes are in
    tests/fixtures/presentmon/fake_presentmon.py), the game in front throughout. Returns what was printed."""
    from tests import test_presentmon_source as tps
    monkeypatch.setattr(sess, "STATUS_PERIOD_S", 0.1)
    state = tmp_path / f"pm-{mode}"
    state.mkdir()
    lines: list[str] = []
    target = {"pid": tps.PID, "create_time": 1, "name": "PresentBench.exe", "exe": r"C:\x\PresentBench.exe",
              "adapter_luid": None}
    s = Session(target, SessionOptions(duration_s=seconds, hotkey=None), collectors=[tps.Fake(mode, state), InFront()],
                inventory=INVENTORY, alive=lambda: True, printer=lines.append, processes=lambda: {})
    s.run()
    return lines


def test_presentmon_that_crashes_after_frames_came_is_said_at_once_and_its_rate_not_frozen(tmp_path, monkeypatch):
    # The real collector settles "partial", never "failed", once rows came: here 10 rows, then 0xC0000005.
    lines = _record_with_presentmon("crash", tmp_path, monkeypatch, seconds=4.0)
    said = [line for line in lines if "Frame timing is not working" in line]
    assert len(said) == 1 and "PresentMon ended with exit code" in said[0], said
    stop = lines.index(sess.STOPPING_MESSAGE + "\n")
    after = [line for line in lines[lines.index(said[0]):stop] if line.startswith("\r")]
    assert after and all(" | fps - | " in line for line in after), after[-3:]       # no frame rate frozen in place


def test_presentmon_that_keeps_running_is_no_warning(tmp_path, monkeypatch):
    lines = _record_with_presentmon("breakable", tmp_path, monkeypatch, seconds=2.0)
    assert not [line for line in lines if "Frame timing is not working" in line]
    assert any(" fps | " in line and " fps - " not in line for line in lines if line.startswith("\r"))


NOT_WORKING_END = ". Everything else is still being recorded; stop with Ctrl+C if you need frame timing.\n"


def _frames_check(frames_live, focused, state="ok", detail=""):
    lines: list[str] = []
    s = Session(TARGET, SessionOptions(), collectors=[FramesState(frames_live, state, detail)], printer=lines.append)
    return s, lines, {"frames": dict(frames_live), "system": {"game_focused": focused}}


def test_no_frame_while_the_game_is_in_front_is_said_once_on_its_own_line():
    s, lines, live = _frames_check({"frames": 0, "fps": None}, focused=1)
    for t in (0.0, 4.0, 8.0, 9.9):
        s._check_frames(live, t)
    assert lines == []
    s._check_frames(live, 10.1)
    s._check_frames(live, 30.0)
    assert lines == ["\nFrame timing is not working: the game was in front for 10 s and PresentMon saw no frame from "
                     "it" + NOT_WORKING_END]


@pytest.mark.parametrize("frames_live,focused", [({"frames": 0}, 0), ({"frames": 0}, None), ({}, 0),
                                                 ({"frames": 3, "fps": 60.0}, 1)])
def test_a_game_behind_other_windows_or_already_drawing_is_no_warning(frames_live, focused):
    s, lines, live = _frames_check(frames_live, focused)
    for t in range(61):
        s._check_frames(live, float(t))
    assert lines == []


def test_only_time_with_the_game_in_front_counts_towards_the_wait():
    # The player alt-tabs to the terminal to start the recording: a minimised game draws nothing.
    s, lines, front = _frames_check({"frames": 0}, focused=1)
    behind = {"frames": {"frames": 0}, "system": {"game_focused": 0}}
    for t in range(9):
        s._check_frames(front, float(t))            # 8 s in front
    for t in range(9, 60):
        s._check_frames(behind, float(t))           # then a long while behind
    assert lines == []
    s._check_frames(front, 61.0)                    # 2 s more in front: 10 s in all
    assert lines == ["\nFrame timing is not working: the game was in front for 10 s and PresentMon saw no frame from "
                     "it" + NOT_WORKING_END]


def test_presentmon_that_ended_mid_recording_is_said_at_once_even_after_frames_came():
    stopped = ("120 frames from 1 swap chain(s) of PID 1; PresentMon stopped on its own at t=61.0 s while the game kept "
               "running")
    lines: list[str] = []
    s = Session(TARGET, SessionOptions(), printer=lines.append,
                collectors=[_ended(FramesState({"frames": 120, "fps": 60.0}, "partial", stopped, finish=True))])
    s._check_frames({"frames": {"frames": 120, "fps": 60.0}, "system": {"game_focused": 1}}, 0.0)
    assert lines == [f"\nFrame timing is not working: {stopped}" + NOT_WORKING_END]


def test_presentmon_that_ended_in_ok_is_the_games_own_exit_not_a_fault():
    lines: list[str] = []
    done = "120 frames from 1 swap chain(s) of PID 1; the game exited, so the capture was stopped"
    s = Session(TARGET, SessionOptions(), printer=lines.append,
                collectors=[_ended(FramesState({"frames": 120, "fps": 60.0}, "ok", done, finish=True))])
    for t in range(30):
        s._check_frames({"frames": {"frames": 120, "fps": 60.0}, "system": {"game_focused": 1}}, float(t))
    assert lines == []


def test_without_frame_timing_in_the_recording_there_is_nothing_to_warn_about():
    lines: list[str] = []
    s = Session(TARGET, SessionOptions(frames=False), collectors=[Fake("system")], printer=lines.append)
    for t in range(30):
        s._check_frames({"system": {"game_focused": 1}}, float(t))
    assert lines == []


def test_the_recording_loop_says_it_while_still_recording(monkeypatch):
    monkeypatch.setattr(sess, "STATUS_PERIOD_S", 0.05)
    lines: list[str] = []
    s = Session(TARGET, SessionOptions(duration_s=0.5, hotkey=None),
                collectors=[FramesState({}, "unavailable", "PresentMon needs admin rights (access denied)"),
                            Fake("system")], inventory=INVENTORY, alive=lambda: True, printer=lines.append)
    s.run()
    said = [line for line in lines if "Frame timing is not working" in line]
    assert said == ["\nFrame timing is not working: PresentMon needs admin rights (access denied)" + NOT_WORKING_END]
    assert lines.index(said[0]) < lines.index(sess.STOPPING_MESSAGE + "\n")


def test_the_no_frames_wait_is_a_few_seconds_of_play():
    assert 5.0 <= sess.NO_FRAMES_WARN_S <= 30.0


INTEL, NVIDIA = "0x00000000_0x00010c49", "0x00000000_0x00010f83"
HYBRID = {"adapters": [{"luid": INTEL, "vendor": "intel", "integrated": True},
                       {"luid": NVIDIA, "vendor": "nvidia", "integrated": False}]}


def names_of(target, inventory, **opts):
    from lagdoctor.capture.session import default_collectors
    return [c.name for c in default_collectors(target, inventory, SessionOptions(**opts))]


def test_default_collectors_follow_the_options_and_the_games_adapter():
    import os
    game = {"pid": os.getpid(), "create_time": 1, "name": "python.exe"}
    assert names_of(dict(game, adapter_luid=NVIDIA), HYBRID) == ["system", "frames", "gpu", "fans", "network",
                                                                 "net_flows"]
    assert names_of(dict(game, adapter_luid=None), HYBRID) == ["system", "frames", "gpu", "fans", "network",
                                                               "net_flows"]
    # A game on the Intel GPU: never poll (and maybe wake) the NVIDIA one.
    assert names_of(dict(game, adapter_luid=INTEL), HYBRID) == ["system", "frames", "fans", "network", "net_flows"]
    assert names_of(dict(game, adapter_luid=None), {"adapters": []}) == ["system", "frames", "fans", "network",
                                                                       "net_flows"]
    assert names_of(dict(game, adapter_luid=NVIDIA), HYBRID, network=False, frames=False) == ["system", "gpu", "fans"]


class RelayStub:
    """What session.default_collectors needs of a relay/client.RelayLink."""

    def __init__(self, frames=True, network=True):
        self.frames, self.network, self.made = frames, network, []

    def frames_collector(self, pid):
        self.made.append(pid)
        return Fake("frames")


def test_with_a_relay_frames_and_flows_come_through_it_and_only_what_it_runs():
    import os
    from lagdoctor.capture.netflows import FlowTracker
    from lagdoctor.capture.presentmon import PresentMonSource
    from lagdoctor.capture.session import default_collectors
    game = {"pid": os.getpid(), "create_time": 1, "name": "python.exe", "adapter_luid": INTEL}
    relay = RelayStub()
    cols = {c.name: c for c in default_collectors(game, HYBRID, SessionOptions(relay=relay))}
    assert relay.made == [os.getpid()] and not isinstance(cols["frames"], PresentMonSource)
    assert isinstance(cols["net_flows"], FlowTracker) and cols["net_flows"]._relay is relay
    # A relay that runs frame timing only: the tracker is this window's own (it then needs rights of its own).
    frames_only = RelayStub(network=False)
    cols = {c.name: c for c in default_collectors(game, HYBRID, SessionOptions(relay=frames_only))}
    assert cols["net_flows"]._relay is None and frames_only.made == [os.getpid()]
    # No frame timing asked for: the relay's frames are never made.
    assert "frames" not in names_of(game, HYBRID, relay=RelayStub(), frames=False)
    no_relay = {c.name: c for c in default_collectors(game, HYBRID, SessionOptions())}
    assert isinstance(no_relay["frames"], PresentMonSource) and no_relay["net_flows"]._relay is None


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
    P(100, 4, 50, "pythonw.exe"),                 # the relay: a tool root handed to the session
    P(105, 100, 55, "PresentMon-2.6.0-x64.exe"),  # the relay's PresentMon: joins with its root
    P(106, 100, 45, "old.exe"),                   # names the relay as parent but is older: not its child
    P(110, 4, 60, "python.exe"),                  # another root, handed over below with a stale create time
    P(120, 10, 70, "python.exe"),                 # someone else's Python
)}
IMAGES = {30: LAUNCHER, 40: BASE, 70: LAUNCHER, 80: BASE, 100: BASE, 110: LAUNCHER, 120: BASE}
GAME_AT_80 = {"pid": 80, "create_time": 430}


def test_lagdoctors_own_processes_literal_points():
    image = IMAGES.get
    assert sess.tool_pids(TABLE, GAME_AT_80, [(100, 50), (110, 61)], me=40, image_path=image,
                          launcher=LAUNCHER) == [40, 30, 100, 50, 60, 70, 105]
    # A root that started the game too never takes the game's tree with it.
    started_it = {**TABLE, 70: P(70, 100, 420, "python.exe")}
    assert 80 not in sess.tool_pids(started_it, GAME_AT_80, [(100, 50)], me=40, image_path=image, launcher=LAUNCHER)
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
    assert cap.target["tool_pids"] == sorted({me, 100, 105}), cap.target["tool_pids"]


# lag-doctor's window (M4) as the recorder's snapshot shows it: the window, its WebView2 browser process and that
# process's renderer, and a store client the window opened, which is not lag-doctor's.
WINDOW = {**TABLE, 200: P(200, 10, 500, "pythonw.exe"), 210: P(210, 200, 510, "msedgewebview2.exe"),
          211: P(211, 210, 520, "msedgewebview2.exe"), 220: P(220, 200, 530, "steam.exe")}


def test_the_window_counts_alone_and_its_webview2_browser_with_what_that_started():
    image = IMAGES.get
    assert sess.tool_pids(WINDOW, GAME_AT_80, [(210, 510)], alone=[(200, 500)], me=40, image_path=image,
                          launcher=LAUNCHER) == [40, 30, 210, 200, 50, 60, 70, 211]
    # Counted alone only while it is that process: a number Windows gave to a newer one is not lag-doctor's.
    assert 200 not in sess.tool_pids(WINDOW, GAME_AT_80, alone=[(200, 501)], me=40, image_path=image,
                                     launcher=LAUNCHER)
    # Never the game or one of its own processes, however it is handed over.
    assert 80 not in sess.tool_pids(WINDOW, GAME_AT_80, alone=[(80, 430)], me=40, image_path=image,
                                    launcher=LAUNCHER)
    assert 90 not in sess.tool_pids(WINDOW, GAME_AT_80, [(90, 440)], me=40, image_path=image, launcher=LAUNCHER)


SAFETY_S = 5.0          # ends a sabotaged run that never reaches its count
LONG_S = 30.0           # a set length a part fits in (sess.MIN_PART_S): its test stops the run itself first


def _counted(monkeypatch):
    """A tick every 0.05 s, and lag-doctor's own processes sighted from the first: these tests end a recording after
    a number of ticks, never at a time, so a slow runner cannot change what they count."""
    monkeypatch.setattr(sess, "STATUS_PERIOD_S", 0.05)
    monkeypatch.setattr(sess, "TOOL_SIGHT_S", 0.0)


def test_roots_found_in_each_snapshot_join_with_what_they_started(monkeypatch):
    _counted(monkeypatch)
    me = os.getpid()
    stop, snaps, looked_in = threading.Event(), [], []

    def processes():
        table = {me: P(me, 1, 1), 200: WINDOW[200], 220: WINDOW[220]}
        if len(snaps) < 3:                        # the first browser process and its renderer
            table.update({210: WINDOW[210], 211: WINDOW[211]})
        else:                                     # then a recovery replaced them
            table.update({212: P(212, 200, 540, "msedgewebview2.exe"), 213: P(213, 212, 550, "msedgewebview2.exe")})
        snaps.append(table)
        if len(snaps) == 5:
            sess.request_stop(stop, "enough")
        return table

    def webview(procs):
        looked_in.append(procs)
        return [(p.pid, p.create_time) for p in procs.values() if p.ppid == 200 and p.name == "msedgewebview2.exe"]
    opts = SessionOptions(duration_s=SAFETY_S, hotkey=None, tool_alone=((200, 500),), tool_roots=webview)
    s = Session(TARGET, opts, collectors=full_set(), inventory=INVENTORY, alive=lambda: True,
                printer=lambda line: None, processes=processes)
    pids = s.run(stop).target["tool_pids"]
    assert sorted(pids) == sorted([me, 200, 210, 211, 212, 213]), pids          # never the store client, 220
    assert (len(snaps), len(looked_in)) == (6, 6)                                # five ticks, and the last look
    assert all(given is taken for given, taken in zip(looked_in, snaps))         # each sighting's own snapshot


def test_a_root_finder_that_fails_leaves_the_rest_listed_and_is_said():
    me = os.getpid()
    table = {me: P(me, 1, 1), 100: P(100, 4, 50, "pythonw.exe"), 105: P(105, 100, 55, "PresentMon-2.6.0-x64.exe")}

    def broken(procs):
        raise KeyError("msedgewebview2.exe")
    opts = SessionOptions(duration_s=0.05, hotkey=None, tool_procs=((100, 50),), tool_roots=broken)
    s = Session(TARGET, opts, collectors=full_set(), inventory=INVENTORY, alive=lambda: True,
                printer=lambda line: None, processes=lambda: table)
    cap = s.run()
    assert sorted(cap.target["tool_pids"]) == sorted([me, 100, 105])
    assert [n for n in cap.notes if "could not find some of its own processes" in n] == [
        "lag-doctor could not find some of its own processes (KeyError: 'msedgewebview2.exe'), so the analysis may "
        "name them among the other programs."]


def test_a_process_of_lagdoctors_seen_only_mid_recording_is_listed(monkeypatch):
    _counted(monkeypatch)
    me = os.getpid()
    stop, calls = threading.Event(), []

    def processes():
        calls.append(1)
        table = {me: P(me, 1, 1)}
        if len(calls) in (3, 4):                  # a child of ours that runs for two ticks in the middle
            table[777] = P(777, me, 2)
        if len(calls) == 5:
            sess.request_stop(stop, "enough")
        return table
    s = Session(TARGET, SessionOptions(duration_s=SAFETY_S, hotkey=None), collectors=full_set(), inventory=INVENTORY,
                alive=lambda: True, printer=lambda line: None, processes=processes)
    pids = s.run(stop).target["tool_pids"]
    assert len(calls) == 6, len(calls)            # one sighting a tick, and one more at the stop
    assert 777 in pids, pids


def test_the_liveness_check_and_the_sighting_share_one_snapshot_a_tick(monkeypatch):
    _counted(monkeypatch)
    me = os.getpid()
    table = {me: P(me, 1, 1), 4242: P(4242, 10, 1, "Game.exe")}     # this process and the game (TARGET)
    stop, taken, ticks = threading.Event(), [], []

    def processes():
        taken.append(1)
        if len(taken) == 5:
            sess.request_stop(stop, "enough")
        return table
    s = Session(TARGET, SessionOptions(duration_s=SAFETY_S, hotkey=None), collectors=full_set(), inventory=INVENTORY,
                printer=lambda line: None, processes=processes,
                observers=sess.Observers(on_tick=lambda elapsed, live: ticks.append(elapsed)))
    cap = s.run(stop)
    # The default liveness check read the game from that snapshot: had it taken one of its own, this made-up game
    # would have "exited" at the first tick.
    assert s.stop_reason == "enough", s.stop_reason
    assert (len(ticks), len(taken)) == (5, 6)                       # one a tick, and one for the last look
    assert me in cap.target["tool_pids"] and 4242 not in cap.target["tool_pids"]


def test_one_failed_snapshot_does_not_end_a_recording_that_shares_it(monkeypatch):
    _counted(monkeypatch)
    me = os.getpid()
    table = {me: P(me, 1, 1), 4242: P(4242, 10, 1, "Game.exe")}
    stop, calls = threading.Event(), []

    def processes():
        calls.append(1)
        if len(calls) == 2:                       # the liveness check's, at the second tick
            raise OSError("NtQuerySystemInformation failed")
        if len(calls) == 6:
            sess.request_stop(stop, "enough")
        return table
    s = Session(TARGET, SessionOptions(duration_s=SAFETY_S, hotkey=None), collectors=full_set(), inventory=INVENTORY,
                printer=lambda line: None, processes=processes)
    cap = s.run(stop)
    assert s.stop_reason == "enough" and cap.frames, s.stop_reason
    assert me in cap.target["tool_pids"]          # the sighting took a snapshot again in that tick


def test_a_full_tool_list_is_said_and_one_that_just_fits_is_not(monkeypatch):
    _counted(monkeypatch)
    me = os.getpid()
    for children, noted in ((sess.MAX_TOOL_PIDS - 1, False), (sess.MAX_TOOL_PIDS, True)):
        table = {me: P(me, 1, 1)}
        table.update({p: P(p, me, 2) for p in range(1_000_000, 1_000_000 + 4 * children, 4)})
        stop, ticks = threading.Event(), []

        def alive():
            ticks.append(1)
            if len(ticks) == 4:
                sess.request_stop(stop, "enough")
            return True
        s = Session(TARGET, SessionOptions(duration_s=SAFETY_S, hotkey=None), collectors=full_set(),
                    inventory=INVENTORY, alive=alive, printer=lambda line: None, processes=lambda: table)
        cap = s.run(stop)                         # sighted at four ticks and at the stop: the same list is no new drop
        assert len(cap.target["tool_pids"]) == min(children + 1, sess.MAX_TOOL_PIDS)
        said = [n for n in cap.notes if f"listed only the first {sess.MAX_TOOL_PIDS} of its own processes" in n]
        assert len(said) == (1 if noted else 0), (children, cap.notes)


# ------------------------------------------------ who follows a recording besides its console (Observers) ---

def test_observers_hear_each_tick_and_every_event_in_order(monkeypatch):
    _counted(monkeypatch)
    stop, ticks, events, marked = threading.Event(), [], [], []

    def on_tick(elapsed, live):
        ticks.append((elapsed, sorted(live)))
        if len(ticks) == 3:                       # a mark from another thread, while it records
            presser = threading.Thread(target=lambda: marked.append(s.mark()))
            presser.start()
            presser.join(5)
        if len(ticks) == 6:
            sess.request_stop(stop, "enough")
    obs = sess.Observers(on_tick=on_tick, on_event=lambda kind, data: events.append((kind, data)))
    s = Session(TARGET, SessionOptions(duration_s=SAFETY_S, hotkey=None), collectors=full_set(), inventory=INVENTORY,
                alive=lambda: True, printer=lambda line: None, observers=obs)
    cap = s.run(stop)
    assert [k for k, _ in events] == ["recording", "mark", "stopping"], events
    assert events[0][1] == {"duration_s": SAFETY_S, "hotkey": False, "frames": True, "network": True}
    assert events[1][1] == {"t": marked[0]} and cap.marks == marked
    assert ticks[2][0] - 0.001 <= marked[0] < ticks[3][0], (ticks, marked)      # kept to the millisecond
    assert events[2][1] == {"reason": "enough"}
    assert len(ticks) == 6 and [e for e, _ in ticks] == sorted(e for e, _ in ticks)
    assert all(names == ["frames", "gpu", "net_flows", "network", "system"] for _, names in ticks)


class _Registered:
    """A lag key that registers."""
    error = None

    def start(self):
        return True

    def stop(self):
        pass


@pytest.mark.parametrize("factory,registered", [(lambda key, on_press: _Registered(), True), (NoHotkey, False)])
def test_the_recording_event_says_whether_the_lag_key_was_registered(factory, registered):
    events = []
    s = Session(TARGET, SessionOptions(duration_s=0.05, hotkey="ctrl+alt+l"), collectors=full_set(),
                inventory=INVENTORY, alive=lambda: True, hotkey_factory=factory, printer=lambda line: None,
                observers=sess.Observers(on_event=lambda kind, data: events.append((kind, data))))
    s.run()
    assert dict(events)["recording"]["hotkey"] is registered


def test_an_observer_that_raises_is_not_called_again_and_the_recording_goes_on(monkeypatch):
    _counted(monkeypatch)
    calls, stop, ticks = {"tick": 0, "event": 0}, threading.Event(), []

    def bad_tick(elapsed, live):
        calls["tick"] += 1
        raise RuntimeError("the window went away")

    def bad_event(kind, data):
        calls["event"] += 1
        raise BrokenPipeError(32, "The pipe is being closed")

    def alive():
        ticks.append(1)
        if len(ticks) == 4:
            sess.request_stop(stop, "enough")
        return True
    cols = full_set()
    s = Session(TARGET, SessionOptions(duration_s=SAFETY_S, hotkey=None), collectors=cols, inventory=INVENTORY,
                alive=alive, printer=lambda line: None,
                observers=sess.Observers(on_tick=bad_tick, on_event=bad_event))
    cap = s.run(stop)
    assert calls == {"tick": 1, "event": 1} and len(ticks) == 4
    assert s.stop_reason == "enough" and cap.frames and all(c._thread is None for c in cols)
    assert [n for n in cap.notes if "window following this recording" in n] == [
        "The window following this recording stopped being told what happened during it (BrokenPipeError); the "
        "recording itself went on.",
        "The window following this recording stopped being told its progress (RuntimeError); the recording itself "
        "went on."]


def test_a_mark_before_the_recording_starts_is_refused():
    s = _session(full_set())
    assert s.mark() is None and s.marks == []
    assert s.run().marks == []


def test_frame_timing_that_stops_working_is_an_event_too():
    events = []
    s, lines, live = _frames_check({"frames": 0, "fps": None}, focused=1)
    s.observers = sess.Observers(on_event=lambda kind, data: events.append((kind, data)))
    for t in (0.0, 5.0, 10.1, 30.0):
        s._check_frames(live, t)
    assert len(lines) == 1 and events == [
        ("frames_lost", {"why": "the game was in front for 10 s and PresentMon saw no frame from it"})]


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
    monkeypatch.setattr(e2e, "preflight", lambda: None)         # PresentMon ready (test_e2e_preflight_* own it)
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
    monkeypatch.setattr(e2e, "preflight", lambda: None)
    monkeypatch.setattr(sys, "executable", "")           # Python could not tell its own path
    assert e2e.main(["--modes", "smooth", "--duration", "0.1", "--out", str(tmp_path / "e2e")]) == 3
    out = capsys.readouterr().out
    assert "could not be started" in out and "Traceback" not in out


def test_e2e_preflight_stops_before_any_prompt_when_presentmon_is_missing(monkeypatch, tmp_path, capsys):
    # The first real run asked for UAC, then failed all 8 scenarios on the same missing PresentMon.
    e2e = _e2e()
    from lagdoctor.winapi import elevation

    def never(*a, **k):
        raise AssertionError("asked for elevation without a PresentMon to run")
    monkeypatch.setattr(elevation, "relaunch_elevated", never)
    monkeypatch.setattr(elevation, "can_trace", lambda: False)
    assert e2e.main(["--modes", "smooth", "--out", str(tmp_path / "e2e")]) == 3     # the test home has none
    assert capsys.readouterr().out.startswith("Nothing ran: PresentMon 2.6.0 is not installed (")
    assert not (tmp_path / "e2e").exists()


def test_e2e_preflight_in_the_elevated_child_hands_its_reason_back(tmp_path, capsys):
    e2e = _e2e()
    out = tmp_path / "e2e"
    assert e2e.main(["--modes", "smooth", "--out", str(out), "--child"]) == 3
    (result,) = json.loads((out / "e2e-results.json").read_text(encoding="utf-8"))
    assert result["mode"] == "all" and result["ok"] is False and "is not installed" in result["error"]
    capsys.readouterr()
    assert e2e.print_summary(out, 3) == 1                    # the first window shows it, not "no results"
    assert "ERROR: PresentMon 2.6.0 is not installed" in capsys.readouterr().out


def test_e2e_preflight_refuses_a_private_copy(monkeypatch, tmp_path, capsys):
    e2e = _e2e()
    from lagdoctor import paths
    monkeypatch.setattr(paths, "private_copy", lambda folder=None: paths.PrivateCopy("App_1", r"C:\x\LocalCache\ld"))
    assert e2e.main(["--modes", "smooth", "--out", str(tmp_path / "e2e")]) == 3
    out = capsys.readouterr().out
    assert out.startswith("Nothing ran: Windows keeps the files") and "App_1" in out and "Start menu" in out


# ------------------------------------------------------------------------- the start key (M5) ---

START = "ctrl+alt+s"


class Keys:
    """Every key's listener the session makes, one per key: press(key) delivers a press from another thread, as
    Windows does, at the clock's now. Windows will not give the keys in `refuse`."""

    def __init__(self, refuse=()):
        self.made, self.refuse, self.live = [], set(refuse), {}

    def __call__(self, key, on_press):
        keys = self
        self.made.append(key)

        class Listener:
            error = f"{key} is already used by another program" if key in keys.refuse else None
            taken = key in keys.refuse                 # as HotkeyListener says for Windows' "already used"

            def start(self):
                if key in keys.refuse:
                    return False
                keys.live[key] = on_press
                return True

            def stop(self):
                keys.live.pop(key, None)
        return Listener()

    def press(self, key, when=None):
        on_press = self.live[key]
        t = threading.Thread(target=on_press, args=(clock.now() if when is None else when,))
        t.start()
        t.join(5)


def _with_start(**kw):
    opts = dict(duration_s=SAFETY_S, hotkey="ctrl+alt+l", start_key=START, window_s=600.0, start_by_s=1200.0)
    opts.update(kw)
    return SessionOptions(**opts)


def test_the_start_keys_first_press_after_recording_starts_the_measured_window(monkeypatch):
    _counted(monkeypatch)
    keys, events, ticks, stop = Keys(), [], [], threading.Event()

    def on_tick(elapsed, live):
        ticks.append(elapsed)
        if len(ticks) == 2:
            keys.press(START)
            keys.press(START)                      # a second press changes nothing
        if len(ticks) == 4:
            sess.request_stop(stop, "enough")
    s = Session(TARGET, _with_start(duration_s=LONG_S), collectors=full_set(), inventory=INVENTORY, alive=lambda: True,
                hotkey_factory=keys, printer=lambda line: None,
                observers=sess.Observers(on_tick=on_tick, on_event=lambda kind, data: events.append((kind, data))))
    cap = s.run(stop)
    assert [k for k, _ in events] == ["recording", "start_key", "measuring", "stopping"], events
    # The recording's own set length (LONG_S) comes before the 20 minutes, so no press is waited for past it.
    assert events[1][1] == {"key": START, "registered": True, "window_s": 600.0, "start_by_s": None}
    at = cap.start_key["at"]
    assert events[2][1] == {"at": at, "until": LONG_S}            # the set length ends it before the window
    assert cap.start_key == {"key": START, "registered": True, "window_s": 600.0, "at": at} and at == s.start_at
    assert ticks[1] - 0.001 <= at < ticks[2] and at == round(at, 3)      # on the capture's clock, to the millisecond
    assert cap.marks == []                         # the start key's press is never a lag mark (DECISIONS 2026-11-09)
    assert keys.made == ["ctrl+alt+l", START] and keys.live == {}         # one listener per key, both let go


def test_a_start_press_before_the_recording_began_or_after_it_stopped_changes_nothing():
    events = []
    stop = threading.Event()
    # No set length, so every press leaves room for a part: only the key being shut can refuse these.
    s = Session(TARGET, _with_start(duration_s=None), collectors=full_set(), inventory=INVENTORY,
                alive=lambda: True, hotkey_factory=Keys(), printer=lambda line: None,
                observers=sess.Observers(on_event=lambda kind, data: events.append(kind),
                                         on_tick=lambda elapsed, live: sess.request_stop(stop, "enough")))
    assert s.start() is None                       # before the recording began
    cap = s.run(stop)
    assert s.start() is None and s.start_at is None           # after it stopped
    assert "measuring" not in events and cap.start_key["at"] is None


def test_the_window_and_its_tail_or_no_press_in_time_end_the_recording_at_their_time():
    s = Session(TARGET, _with_start(window_s=10.0, start_by_s=30.0), collectors=[], inventory=INVENTORY)
    assert sess.WINDOW_TAIL_S == 3.0               # PresentMon's rows arrive up to 2 s late
    s._start_on = s._start_open = True
    assert s._start_stop(29.999) is None and s._start_stop(30.0) == sess.NO_START and not s._start_open
    s._start_open, s.start_at = True, 5.0          # pressed at 5 s: the window runs to 15 s, its tail to 18 s
    assert s._start_stop(17.999) is None and s._start_open
    assert s._start_stop(18.0) == sess.WINDOW_ENDED and not s._start_open     # no press counts once decided
    s.opts.window_s = None                         # measured to the recording's end: nothing ends it here
    assert s._start_stop(1e6) is None
    s._start_on, s.start_at = False, None          # a key Windows did not give: no press can be waited for
    assert s._start_stop(1e6) is None


def test_the_wait_for_the_next_tick_lands_on_the_start_keys_end(monkeypatch):
    monkeypatch.setattr(sess, "STATUS_PERIOD_S", 1.0)
    s = Session(TARGET, _with_start(duration_s=None, window_s=10.0, start_by_s=30.0), collectors=[],
                inventory=INVENTORY)
    assert s._next_wait(29.5) == 1.0               # Windows did not give the key: no time a press must come by
    s._start_on = s._start_open = True              # given, and offered: a press is awaited until 30 s
    assert s._next_wait(29.5) == pytest.approx(0.5) and s._next_wait(10.0) == 1.0 and s._next_wait(31.0) == 0.0
    s.start_at = 5.0
    assert s._next_wait(17.75) == pytest.approx(0.25) and s._next_wait(12.0) == 1.0


def test_no_press_in_time_stops_the_recording_and_says_so():
    events, lines = [], []
    s = Session(TARGET, _with_start(start_by_s=0.3, duration_s=LONG_S), collectors=full_set(), inventory=INVENTORY,
                alive=lambda: True, hotkey_factory=Keys(), printer=lines.append,
                observers=sess.Observers(on_event=lambda kind, data: events.append((kind, data))))
    cap = s.run()
    assert cap.target["stop_reason"] == sess.NO_START and cap.duration_s < SAFETY_S
    assert cap.start_key == {"key": START, "registered": True, "window_s": 600.0, "at": None}
    assert dict(events)["stopping"] == {"reason": sess.NO_START}
    assert ("\nThe start key was not pressed within 0.3 s, so the recording stops, and all of it will be analysed.\n"
            in lines)


def test_the_measured_window_stops_the_recording_and_both_are_said(monkeypatch):
    _counted(monkeypatch)
    monkeypatch.setattr(sess, "WINDOW_TAIL_S", 0.05)
    keys, lines, ticks = Keys(), [], []

    def on_tick(elapsed, live):
        ticks.append(elapsed)
        if len(ticks) == 1:
            keys.press(START)
    s = Session(TARGET, _with_start(window_s=0.3, duration_s=LONG_S), collectors=full_set(), inventory=INVENTORY,
                alive=lambda: True, hotkey_factory=keys, printer=lines.append,
                observers=sess.Observers(on_tick=on_tick))
    cap = s.run()
    assert cap.target["stop_reason"] == sess.WINDOW_ENDED
    assert cap.duration_s >= cap.start_key["at"] + 0.3 + 0.05 and cap.duration_s < SAFETY_S
    measuring = [line for line in lines if line.startswith("\nMeasuring from 0:00 for 0.3 s, until 0:00.")]
    ended = lines.index("\nMeasured 0.3 s from your start key, so the recording stops.\n")
    assert len(measuring) == 1 and lines.index(measuring[0]) < ended


def test_a_start_key_windows_will_not_give_is_said_and_noted_and_the_recording_goes_on():
    events, lines = [], []
    s = Session(TARGET, _with_start(duration_s=0.3, start_by_s=0.05), collectors=full_set(), inventory=INVENTORY,
                alive=lambda: True, hotkey_factory=Keys(refuse=[START]), printer=lines.append,
                observers=sess.Observers(on_event=lambda kind, data: events.append((kind, data))))
    cap = s.run()
    assert cap.target["stop_reason"] == "duration"               # no press can come, so none is waited for
    assert dict(events)["start_key"] == {"key": START, "registered": False, "window_s": 600.0, "start_by_s": None}
    assert cap.start_key == {"key": START, "registered": False, "window_s": 600.0, "at": None}
    said = "".join(lines)
    assert ("\nThe start key Ctrl+Alt+S could not be registered (ctrl+alt+s is already used by another program), so "
            "the whole recording will be analysed, and it stops only at its set length. To measure from a press, close "
            "the program that uses it, or record again with another --start-key.\n") in said
    assert "is ready" not in said                                   # never said for a key Windows did not give
    assert any(note.startswith("The start key ctrl+alt+s could not be registered") for note in cap.notes)


def test_a_start_key_that_cannot_be_used_at_all_never_ends_the_capture():
    def factory(key, on_press):
        if key == START:
            raise OSError("no message queue")
        return _Registered()
    s = Session(TARGET, _with_start(duration_s=0.05), collectors=full_set(), inventory=INVENTORY, alive=lambda: True,
                hotkey_factory=factory, printer=lambda line: None)
    cap = s.run()
    assert cap.start_key["registered"] is False and cap.target["stop_reason"] == "duration"
    assert any("OSError: no message queue" in note for note in cap.notes)


def test_without_a_start_key_there_is_no_second_listener_and_nothing_of_one_is_kept():
    keys, events = Keys(), []
    s = Session(TARGET, SessionOptions(duration_s=0.05, hotkey="ctrl+alt+l"), collectors=full_set(),
                inventory=INVENTORY, alive=lambda: True, hotkey_factory=keys, printer=lambda line: None,
                observers=sess.Observers(on_event=lambda kind, data: events.append(kind)))
    cap = s.run()
    assert keys.made == ["ctrl+alt+l"] and cap.start_key is None and "start_key" not in events


def test_the_recordings_own_duration_is_checked_before_the_start_keys_end():
    s = Session(TARGET, _with_start(duration_s=0.0, start_by_s=0.0), collectors=full_set(), inventory=INVENTORY,
                alive=lambda: True, hotkey_factory=Keys(), printer=lambda line: None)
    assert s.run().target["stop_reason"] == "duration"


def test_joined_observers_each_hear_everything_and_one_that_raises_silences_only_itself():
    heard_a, heard_b = [], []

    def bad(kind, data):
        heard_a.append(kind)
        raise RuntimeError("boom")
    a = sess.Observers(on_event=bad)
    b = sess.Observers(on_tick=lambda elapsed, live: heard_b.append("tick"),
                       on_event=lambda kind, data: heard_b.append(kind))
    both = sess.Joined(a, b)
    both.event("recording", x=1)
    both.tick(1.0, {})
    both.event("stopping", reason="r")
    assert heard_a == ["recording"] and heard_b == ["recording", "tick", "stopping"]
    assert both.notes() == a.notes() and len(both.notes()) == 1


@pytest.mark.parametrize("seconds,words", [(600, "10 minutes"), (60, "1 minute"), (1200.0, "20 minutes"),
                                           (90, "90 s"), (30.0, "30 s"), (0.3, "0.3 s"), (59.96, "1 minute"),
                                           (61, "61 s")])
def test_a_length_is_said_in_plain_words(seconds, words):
    assert sess.span_words(seconds) == words


@pytest.mark.parametrize("seconds,text", [(0, "0:00"), (192.9, "3:12"), (3792, "63:12"), (-1, "0:00")])
def test_a_time_is_said_as_the_status_line_shows_it(seconds, text):
    assert sess.clock_text(seconds) == text
    if seconds >= 0:
        assert status_line(seconds, {}).startswith(text + " |")


def test_a_press_is_waited_for_only_until_the_recordings_own_set_length():
    s = Session(TARGET, _with_start(duration_s=None, start_by_s=1200.0), collectors=[], inventory=INVENTORY)
    assert s._press_by() is None                    # Windows has not given the key (yet)
    s._start_on = True
    assert s._press_by() == 1200.0
    s.opts.duration_s = 1200.5
    assert s._press_by() == 1200.0
    s.opts.duration_s = 1200.0                      # the set length comes first: it, not the wait, ends the recording
    assert s._press_by() is None
    s.opts.duration_s, s.opts.start_by_s = None, None
    assert s._press_by() is None


def test_the_key_is_said_ready_only_once_windows_gave_it_and_a_press_before_that_changes_nothing():
    lines, events, presses, pressed = [], [], {}, []

    def factory(key, on_press):
        presses[key] = on_press

        class Listener:
            error = None

            def start(self):
                on_press(clock.now())                 # pressed while Windows gives it
                return True

            def stop(self):
                pass
        return Listener()

    stop = threading.Event()

    def on_event(kind, data):
        events.append(kind)
        if kind in ("recording", "start_key"):        # Windows has given it, but it is not said ready yet
            pressed.append(kind)
            presses[START](clock.now())
        if kind == "start_key":
            sess.request_stop(stop, "enough")         # once said ready, the run ends at its first tick
    # A set length a part fits in: a press then counts unless it came too early (a shorter one refuses every press).
    s = Session(TARGET, _with_start(duration_s=LONG_S), collectors=full_set(), inventory=INVENTORY,
                alive=lambda: True, hotkey_factory=factory, printer=lines.append,
                observers=sess.Observers(on_event=on_event))
    cap = s.run(stop)
    assert pressed == ["recording", "start_key"] and cap.target["stop_reason"] == "enough"
    assert cap.start_key["at"] is None and "measuring" not in events and s._late_at is None
    ready = lines.index("\nCtrl+Alt+S is ready: press it at the spot you want measured.\n")
    assert lines.index(sess.STOPPING_MESSAGE + "\n") > ready and lines.count(lines[ready]) == 1


def test_a_run_whose_caller_says_when_to_press_hears_only_that_the_key_is_ready():
    # A settings sweep's run (start_advice False) tells the player to wait at the spot before the press, so watch's
    # own "press it at the spot" would tell the opposite (DECISIONS 2026-11-21).
    lines, keys, stop = [], Keys(), threading.Event()

    def on_event(kind, data):
        if kind == "start_key":
            sess.request_stop(stop, "enough")
    s = Session(TARGET, _with_start(duration_s=LONG_S, start_advice=False), collectors=full_set(),
                inventory=INVENTORY, alive=lambda: True, hotkey_factory=keys, printer=lines.append,
                observers=sess.Observers(on_event=on_event))
    s.run(stop)
    assert "\nCtrl+Alt+S is ready.\n" in lines and not any("press it at the spot" in x for x in lines), lines


def test_a_window_to_the_recordings_end_waits_a_whole_tick_and_says_so(monkeypatch):
    monkeypatch.setattr(sess, "STATUS_PERIOD_S", 1.0)
    s = Session(TARGET, _with_start(duration_s=None, window_s=None), collectors=[], inventory=INVENTORY)
    s._start_on, s.start_at = True, 5.0
    assert s._window_end() is None and s._next_wait(1e6) == 1.0     # nothing of the start key's ends it
    assert s._measuring_line() == "Measuring from 0:05 to the end of the recording."
    s.opts.duration_s = 120.0
    assert s._measuring_line() == "Measuring from 0:05 until 2:00, when the recording stops at its set length."


@pytest.mark.parametrize("left,counts", [(11.0, True), (10.999, False), (0.0, False), (-0.004, False)])
def test_a_first_press_with_too_little_set_length_left_for_a_part_measures_nothing(left, counts):
    # The set length is 2:00; `left` is what is left of it at the press, past it included (the loop can wake late).
    from lagdoctor import control
    events = []
    s = Session(TARGET, _with_start(duration_s=120.0, window_s=600.0), collectors=[], inventory=INVENTORY,
                observers=sess.Observers(on_event=lambda kind, data: events.append((kind, data))))
    s.origin, s._start_on, s._start_open = 1000.0, True, True
    at = s.start(1000.0 + 120.0 - left)
    if counts:
        assert at == 109.0 and s.start_at == 109.0 and s._late_at is None
        assert events == [("measuring", {"at": 109.0, "until": 120.0})]
    else:
        assert at is None and s.start_at is None and s._late_at == round(120.0 - left, 3) and events == []
        assert s.start(1000.0 + 30.0) is None and s._late_at == round(120.0 - left, 3)   # the key is closed
    for kind, data in events:                        # what is told is what the window's reader takes
        control.from_recorder(control.encode(kind, **data))


def test_a_press_too_late_for_a_part_is_said_once_before_the_stop_and_measures_nothing(monkeypatch):
    # A 2.5 s part on a 3 s set length: offered at the start, a press after 0.5 s is too late. The run then goes on
    # for three more ticks (counted, never timed) and is stopped, so a line said at every tick would show.
    _counted(monkeypatch)
    monkeypatch.setattr(sess, "MIN_PART_S", 2.5)
    keys, lines, events, ticks, after = Keys(), [], [], [], []
    stop = threading.Event()

    def on_tick(elapsed, live):
        ticks.append(elapsed)
        if keys.pressed:
            after.append(elapsed)
            if len(after) == 3:
                sess.request_stop(stop, "enough")
        elif elapsed >= 0.6:
            keys.pressed = True
            keys.press(START)
            keys.press(START)                          # once refused, the key is closed
    keys.pressed = False
    s = Session(TARGET, _with_start(duration_s=3.0), collectors=full_set(), inventory=INVENTORY, alive=lambda: True,
                hotkey_factory=keys, printer=lines.append,
                observers=sess.Observers(on_tick=on_tick, on_event=lambda kind, data: events.append(kind)))
    cap = s.run(stop)
    assert keys.pressed and "start_key" in events and "measuring" not in events and cap.start_key["at"] is None
    assert cap.target["stop_reason"] == "enough" and len(after) == 3 and s._late_at is not None
    late = [line for line in lines if "was pressed only" in line]
    assert late == ["\n" + s._late_line() + "\n"] and lines.index(late[0]) < lines.index(sess.STOPPING_MESSAGE + "\n")


def test_the_too_late_line_says_the_time_that_was_left():
    s = Session(TARGET, _with_start(duration_s=120.0), collectors=[], inventory=INVENTORY)
    s._late_at = 109.4                                 # 10.6 s before 2:00, which 1:49 and 2:00 alone would hide
    assert s._late_line() == ("Ctrl+Alt+S was pressed only 10.6 s before the recording stops at its set length (2:00), "
                              "and a part needs at least 11 s, so all of it will be analysed.")
    for late in (119.95, 120.004):                     # 0.05 s before (never "only 0.0 s"), a few ms past
        s._late_at = late
        assert s._late_line().startswith("Ctrl+Alt+S was pressed as the recording reached its set length (2:00), and")
    s.opts.start_advice = False                        # a sweep's run: watch's advice is left to the sweep
    assert s._late_line().endswith("and a part needs at least 11 s.")


@pytest.mark.parametrize("duration,at,counts", [(16.4, 5.4, True), (16.4, 5.401, False), (120.0, 109.0, True)])
def test_the_room_for_a_part_is_judged_to_the_millisecond_as_a_part_is(duration, at, counts):
    from lagdoctor.analysis import crop
    s = Session(TARGET, _with_start(duration_s=duration), collectors=[], inventory=INVENTORY)
    s.origin, s._start_on, s._start_open = 1000.0, True, True
    assert (s.start(1000.0 + at) is not None) is counts
    from types import SimpleNamespace
    cap = SimpleNamespace(span=None, duration_s=duration)   # all check_span reads of a capture
    try:                                               # the analysis takes the same part, or refuses it
        crop.check_span(cap, at, duration)
        taken = True
    except ValueError:
        taken = False
    assert taken is counts


def test_a_press_refused_as_too_late_is_not_said_to_be_missing_and_the_loop_waits_a_whole_tick():
    s = Session(TARGET, _with_start(duration_s=120.0, start_by_s=115.0), collectors=[], inventory=INVENTORY)
    s.origin, s._start_on, s._start_open = 1000.0, True, True
    assert s._start_stop(116.0) == sess.NO_START          # no press by 1:55
    s = Session(TARGET, _with_start(duration_s=120.0, start_by_s=115.0), collectors=[], inventory=INVENTORY)
    s.origin, s._start_on, s._start_open = 1000.0, True, True
    assert s.start(1000.0 + 112.0) is None               # pressed at 1:52, too late: the set length ends it
    assert s._start_stop(116.0) is None
    assert s._next_wait(116.0) == sess.STATUS_PERIOD_S    # never 0 s from 1:55 on: no press is awaited


@pytest.mark.parametrize("when", ["before", "on recording", "refused"])
def test_a_recording_already_stopping_never_says_its_start_key_is_ready(when):
    keys, lines, events = Keys(refuse={START} if when == "refused" else ()), [], []
    stop = threading.Event()
    if when in ("before", "refused"):
        sess.request_stop(stop, "Ctrl+C")              # Ctrl+C while the PC's settings were read

    def on_event(kind, data):
        events.append(kind)
        if kind == "recording" and when == "on recording":
            sess.request_stop(stop, "frame timing is off")   # as a sweep closes a run on "recording"
    s = Session(TARGET, _with_start(duration_s=LONG_S), collectors=full_set(), inventory=INVENTORY,
                alive=lambda: True, hotkey_factory=keys, printer=lines.append, observers=sess.Observers(on_event=on_event))
    cap = s.run(stop)
    assert events[:2] == ["recording", "stopping"] and keys.made == ["ctrl+alt+l", START]
    assert not any("is ready" in line or "could not be registered" in line for line in lines)
    assert cap.start_key["at"] is None
    if when == "refused":                              # still noted for the report
        assert any("The start key ctrl+alt+s could not be registered" in note for note in cap.notes)


def test_the_measuring_line_names_the_set_length_when_it_comes_before_the_window():
    s = Session(TARGET, _with_start(duration_s=120.0, window_s=600.0), collectors=[], inventory=INVENTORY)
    s.start_at = 30.0
    assert s._measured_until(30.0) == 120.0
    assert s._measuring_line() == "Measuring from 0:30 until 2:00, when the recording stops at its set length."
    s.opts.duration_s = 700.0
    assert s._measured_until(30.0) == 630.0
    assert s._measuring_line() == "Measuring from 0:30 for 10 minutes, until 10:30."


@pytest.mark.parametrize("taken,duration,advice", [
    (False, None, "it no longer stops by itself: press Ctrl+C when you are done."),
    (True, None, "it no longer stops by itself: press Ctrl+C when you are done. To measure from a press, close the "
                 "program that uses it, or record again with another --start-key."),
    (False, 0.2, "it stops only at its set length."),
])
def test_a_refused_start_keys_advice_follows_why_windows_refused_it(taken, duration, advice):
    lines = []

    class Refused:
        def __init__(self, key, on_press):
            self.error, self.taken = "Windows refused it (error 5)", taken

        def start(self):
            return False

        def stop(self):
            pass
    stop = threading.Event()
    threading.Timer(0.2, stop.set).start()          # a recording without a set length ends when it is told to
    s = Session(TARGET, _with_start(duration_s=duration), collectors=full_set(), inventory=INVENTORY,
                alive=lambda: True, hotkey_factory=Refused, printer=lines.append)
    s.run(stop)
    [line] = [x for x in lines if "could not be registered" in x]
    assert line == (f"\nThe start key Ctrl+Alt+S could not be registered (Windows refused it (error 5)), so the whole "
                    f"recording will be analysed, and {advice}\n")


def test_a_sweep_runs_lines_say_only_what_happened():
    lines = []
    s = Session(TARGET, _with_start(start_advice=False, duration_s=0.3), collectors=full_set(), inventory=INVENTORY,
                alive=lambda: True, hotkey_factory=Keys(refuse=[START]), printer=lines.append)
    s.run()
    assert ("\nThe start key Ctrl+Alt+S could not be registered (ctrl+alt+s is already used by another program).\n"
            in lines)
    s = Session(TARGET, _with_start(start_advice=False, start_by_s=0.3), collectors=full_set(), inventory=INVENTORY)
    assert s._start_end_line(sess.NO_START) == "The start key was not pressed within 0.3 s, so the recording stops."


def test_a_start_key_that_gives_no_reason_or_cannot_be_let_go_is_noted_as_the_start_keys():
    class Silent:
        error = None

        def __init__(self, key, on_press):
            self.key = key

        def start(self):
            return self.key != START                    # the lag key registers; the start key gives no reason

        def stop(self):
            pass

    class Stuck(Silent):
        def start(self):
            return True

        def stop(self):
            if self.key == START:
                raise OSError("the thread is gone")
    lines = []
    cap = Session(TARGET, _with_start(duration_s=0.05), collectors=full_set(), inventory=INVENTORY, alive=lambda: True,
                  hotkey_factory=Silent, printer=lines.append).run()
    assert any("could not be registered (Windows gave no reason)" in line for line in lines)
    cap = Session(TARGET, _with_start(duration_s=0.05), collectors=full_set(), inventory=INVENTORY, alive=lambda: True,
                  hotkey_factory=Stuck, printer=lambda line: None).run()
    assert "The start key could not be released cleanly (OSError: the thread is gone)." in cap.notes
