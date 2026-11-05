"""PresentMonSource: command, stop protocol, start failures, and live runs of the real exe.

The stop protocol runs against a stand-in child (tests/fixtures/presentmon/fake_presentmon.py) that
handles console control events natively, as PresentMon does, and plays real replay rows into the
pipe (PM 7.3 did the same). Real PresentMon runs only as an ETL replay (no rights needed) and as the
unelevated negative test of PM 6.2, which fails before any capture starts.
"""
from __future__ import annotations

import contextlib
import csv
import ctypes
import os
import shutil
import statistics
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

import psutil
import pytest

from lagdoctor.capture import presentmon as pm
from lagdoctor.model import F
from lagdoctor.winapi import elevation

FIX = Path(__file__).parent / "fixtures" / "presentmon"
FAKE = FIX / "fake_presentmon.py"
# The venv's python.exe is a launcher; the stand-in must be the real interpreter so signals reach it.
BASE_PY = getattr(sys, "_base_executable", sys.executable)
PID = 24892                                  # PresentBench.exe in the replay rows the stand-in plays
PID_ROWS = 265
needs_console = pytest.mark.skipif(not pm.has_console(), reason="CTRL_BREAK needs a console shared with the child")


def wait_for(predicate, timeout: float = 10.0, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout} s waiting for {what}")
        time.sleep(0.02)


class Drain:
    """Read a child's stdout and stderr to EOF on threads, as the collector does."""

    def __init__(self, proc: subprocess.Popen) -> None:
        self.out, self.err = bytearray(), bytearray()
        self.threads = [threading.Thread(target=self._pump, args=(s, b), daemon=True)
                        for s, b in ((proc.stdout, self.out), (proc.stderr, self.err))]
        for t in self.threads:
            t.start()

    @staticmethod
    def _pump(stream, buf: bytearray) -> None:
        for chunk in iter(lambda: stream.read1(65536), b""):
            buf.extend(chunk)

    def join(self) -> None:
        for t in self.threads:
            t.join(10)


@pytest.fixture
def state(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    return d


def start_fake(mode: str, state: Path, console: bool | None = None) -> tuple[subprocess.Popen, Drain]:
    console = pm.has_console() if console is None else console
    proc = pm.spawn([BASE_PY, "-I", str(FAKE), mode, str(state), *pm.live_args(PID)], console)
    drain = Drain(proc)
    wait_for(lambda: (state / "running").exists() or proc.poll() is not None, what="the stand-in to start")
    return proc, drain


def fake_terminate(state: Path, calls: list):
    def terminate(timeout: float) -> int:
        calls.append(timeout)
        return subprocess.run([BASE_PY, "-I", str(FAKE), "-", str(state), *pm.terminate_args()],
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout).returncode
    return terminate


def lines_of(path: Path) -> list[str]:
    return path.read_text().split() if path.exists() else []


QUICK = dict(ctrl_break_wait=1.0, session_cmd_timeout=5.0, session_stop_wait=1.0, kill_wait=1.0)


# --------------------------------------------------------------------- the command ---

def test_the_live_command_is_exactly_the_designed_one():
    assert pm.live_args(1234) == [
        "--process_id", "1234", "--output_stdout", "--no_console_stats", "--qpc_time", "--track_frame_type",
        "--track_pc_latency", "--track_hybrid_present", "--session_name", "lagdoctor", "--stop_existing_session",
        "--terminate_on_proc_exit"]
    assert pm.terminate_args() == ["--terminate_existing_session", "--session_name", "lagdoctor"]
    replay = pm.replay_args(Path("C:/x/test_case_5.etl"))
    assert replay[:2] == ["--etl_file", str(Path("C:/x/test_case_5.etl"))]
    assert not {"--process_id", "--session_name", "--stop_existing_session", "--terminate_on_proc_exit"} & set(replay)


@pytest.mark.parametrize("bad", [0, -1, 0x1_0000_0000, True, "1234", 12.0, None])
def test_a_pid_that_presentmon_would_read_as_every_process_is_refused(bad):
    with pytest.raises(ValueError):
        pm.live_args(bad)
    with pytest.raises(ValueError):
        pm.PresentMonSource(bad)


def test_the_pid_range_ends_are_accepted():
    assert pm.live_args(1)[1] == "1" and pm.live_args(0xFFFFFFFF)[1] == "4294967295"
    assert pm.PresentMonSource(None, etl_file="x.etl").pid is None     # a replay may keep every process


def test_the_exe_is_launched_by_the_absolute_path_that_was_verified(presentmon_exe, tmp_path, monkeypatch):
    # A bare name would be looked up by CreateProcess (the Python folder comes first), not opened as checked.
    shutil.copyfile(presentmon_exe, tmp_path / presentmon_exe.name)
    monkeypatch.chdir(tmp_path)
    src = pm.PresentMonSource(1234, exe=presentmon_exe.name)
    with src._exe_guard() as prefix:
        assert prefix == [str(tmp_path / presentmon_exe.name)]


def test_an_etl_path_can_never_read_as_an_option(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    src = pm.PresentMonSource(None, etl_file="-process_id.etl")
    value = pm.replay_args(src.etl_file)[1]
    assert value == str(tmp_path / "-process_id.etl") and not value.startswith(("-", "/"))


def test_the_stop_budget_fits_the_collector_contract():
    # DESIGN: CTRL_BREAK first, the second instance after 10 s; stop() returns within 25 s.
    s = pm.PresentMonSource
    assert s.CTRL_BREAK_WAIT_S == 10.0
    worst = s.CTRL_BREAK_WAIT_S + s.SESSION_CMD_TIMEOUT_S + s.SESSION_STOP_WAIT_S + s.KILL_WAIT_S + s.SESSION_CMD_TIMEOUT_S
    assert worst + 5.0 <= 25.0                # + the base class's 5 s join


# ---------------------------------------------------------------- start failures ---

def test_access_denied_means_no_rights():
    state, detail, retry = pm.classify_start_failure(6, (FIX / "access_denied.stderr").read_text("latin-1"))
    assert state == "unavailable" and "Performance Log Users" in detail and "access denied" in detail and not retry


def test_a_leftover_session_is_retried_once():
    stderr = ('warning: a trace session named "lagdoctor" is already running and it will be stopped.\n'
              "error: failed to start trace session: error code 183.\n")
    assert pm.classify_start_failure(6, stderr) == (
        "failed", "a trace session named 'lagdoctor' is already running and could not be replaced", True)


def test_a_leftover_session_we_may_not_stop_is_a_rights_problem():
    stderr = ('warning: a trace session named "lagdoctor" is already running and it will be stopped.\n'
              "error: failed to start trace session: access denied.\n")
    assert pm.classify_start_failure(6, stderr)[0] == "unavailable"


@pytest.mark.parametrize("code,stderr,expect", [
    (6, "error: failed to start trace session: file not found.\n", "file not found"),
    (1, "error: unrecognized option '--no_such_option'.\n", "rejected its command line: error: unrecognized option"),
    (6, "error: failed to start trace session: error code 1450.\n", "exit code 6: error: failed to start trace session"),
    (3, "", "exit code 3: no message"),
])
def test_other_start_failures_say_what_presentmon_said(code, stderr, expect):
    state, detail, retry = pm.classify_start_failure(code, stderr)
    assert state == "failed" and expect in detail and not retry


# ------------------------------------------------------- the stop protocol (stand-in) ---

@needs_console
def test_ctrl_break_stops_it_cleanly_and_nothing_else_is_tried(state):
    proc, drain = start_fake("breakable", state)
    calls: list = []
    assert pm.stop_child(proc, console=True, terminate_session=fake_terminate(state, calls), **QUICK) == "ctrl_break"
    drain.join()
    assert proc.returncode == 0 and lines_of(state / "ctrl") == ["1"]      # 1 = CTRL_BREAK_EVENT, handled
    assert calls == [] and not (state / "running").exists()


@needs_console
def test_a_child_deaf_to_ctrl_break_is_stopped_through_its_session_not_killed(state):
    proc, drain = start_fake("deaf", state)
    calls: list = []
    path = pm.stop_child(proc, console=True, terminate_session=fake_terminate(state, calls), **QUICK)
    drain.join()
    assert path == "terminate_session"
    assert lines_of(state / "ctrl") == ["1"] and len(calls) == 1          # CTRL_BREAK first, then the session
    assert proc.returncode == 0                                            # it shut down; it was not killed


@needs_console
def test_only_a_child_that_ignores_both_is_killed_and_its_session_is_cleaned_up(state):
    proc, drain = start_fake("hung", state)
    calls: list = []
    path = pm.stop_child(proc, console=True, terminate_session=fake_terminate(state, calls), **QUICK)
    drain.join()
    assert path == "kill" and proc.returncode == 1                         # TerminateProcess exit code
    assert len(calls) == 2 and len(lines_of(state / "terminate_calls")) == 2
    assert not (state / "running").exists()                               # no session left behind


def test_without_a_console_it_goes_straight_to_the_session_stop(state):
    proc, drain = start_fake("breakable", state, console=False)
    calls: list = []
    started = time.monotonic()
    path = pm.stop_child(proc, console=False, terminate_session=fake_terminate(state, calls),
                         **{**QUICK, "ctrl_break_wait": 5.0})
    took = time.monotonic() - started
    drain.join()
    assert path == "terminate_session" and not (state / "ctrl").exists() and len(calls) == 1
    assert proc.returncode == 0 and took < 4.0                # no CTRL_BREAK wait spent on a signal that cannot land


@needs_console
def test_a_ctrl_break_that_cannot_be_sent_falls_back_to_the_session_stop(state):
    proc, drain = start_fake("breakable", state)

    def no_console(sig):
        raise OSError(None, "The handle is invalid", None, 6)              # what os.kill raises without a console

    proc.send_signal = no_console
    calls: list = []
    path = pm.stop_child(proc, console=True, terminate_session=fake_terminate(state, calls), **QUICK)
    drain.join()
    assert path == "terminate_session" and len(calls) == 1 and proc.returncode == 0


def test_a_child_that_already_ended_is_left_alone(state):
    proc, drain = start_fake("denied", state)
    proc.wait(10)
    calls: list = []
    assert pm.stop_child(proc, console=True, terminate_session=fake_terminate(state, calls), **QUICK) == "exited"
    drain.join()
    assert calls == [] and proc.returncode == 6


@needs_console
def test_a_replay_has_no_session_so_a_stuck_one_is_simply_killed(state):
    proc, drain = start_fake("hung", state)
    started = time.monotonic()
    assert pm.stop_child(proc, console=True, terminate_session=None, **QUICK) == "kill"
    drain.join()
    assert time.monotonic() - started < 5 and not (state / "terminate_calls").exists()


# --------------------------------------------------- the collector, with the stand-in ---

class Fake(pm.PresentMonSource):
    CTRL_BREAK_WAIT_S = 1.0
    SESSION_STOP_WAIT_S = 1.0
    SESSION_CMD_TIMEOUT_S = 5.0
    WATCH_INTERVAL_S = 0.1

    def __init__(self, mode: str, state: Path, pid: int = PID, **kw) -> None:
        kw.setdefault("is_alive", lambda: True)      # PID 24892 is the replay's game, not a process here
        super().__init__(pid, **kw)
        self.mode, self.state = mode, state

    @contextlib.contextmanager
    def _exe_guard(self):
        self._exe_used = str(FAKE)
        yield [BASE_PY, "-I", str(FAKE), self.mode, str(self.state)]


def run_to_end(src: pm.PresentMonSource, timeout: float = 20.0) -> None:
    src.start(0.0)
    wait_for(lambda: src._thread is not None and not src._thread.is_alive(), timeout, "the collector to finish")
    src.stop()


@needs_console
def test_rows_stream_in_and_ctrl_break_drains_the_rest(state):
    src = Fake("breakable", state)
    src.start(0.0)
    wait_for(lambda: (src.live().get("frames") or 0) > 0, what="the first frames")
    src.stop()
    r = src.result()
    info = r["presentmon"]
    assert info["stop_path"] == "ctrl_break" and info["exit_code"] == 0
    assert info["rows"] == PID_ROWS == len(r["columns"][F.T])             # rows written after CTRL_BREAK were kept
    assert r["swap_chains"]["0x2A70D2CAC00"] == {"frames": 258, "displayed": 174, "runtime": "DXGI"}
    assert r["main_chain"] == "0x2A70D2CAC00"
    assert src.status().state == "ok" and info["headers"] == 1 and info["malformed"] == 0


@needs_console
def test_the_game_exiting_stops_the_capture_without_the_session_asking(state):
    src = Fake("breakable", state, is_alive=lambda: not src.live().get("frames"))   # "exits" once frames flow
    src.start(0.0)
    wait_for(lambda: not src._thread.is_alive(), 15, "the capture to stop by itself")
    st, r = src.status(), src.result()
    src.stop()
    assert r["presentmon"]["stop_path"] == "ctrl_break" and r["presentmon"]["rows"] == PID_ROWS
    assert st.state == "ok" and "game exited" in st.detail


def test_a_broken_liveness_check_does_not_end_the_capture(state):
    def broken():
        raise RuntimeError("snapshot failed")

    src = Fake("breakable", state, is_alive=broken)
    src.start(0.0)
    wait_for(lambda: (src.live().get("frames") or 0) > 0, what="the first frames")
    time.sleep(0.6)                           # several watch intervals
    assert src._thread.is_alive()
    src.stop()
    assert src.result()["presentmon"]["rows"] == PID_ROWS


@needs_console
def test_the_game_is_still_watched_after_the_check_failed(state):
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) <= 3:
            raise RuntimeError("snapshot failed")
        return not src.live().get("frames")

    src = Fake("breakable", state, is_alive=flaky)
    src.start(0.0)
    wait_for(lambda: not src._thread.is_alive(), 15, "the capture to stop once the game is gone")
    src.stop()
    assert len(calls) > 3 and "game exited" in src.status().detail


def test_presentmon_stopped_from_outside_while_the_game_runs_is_partial_and_says_when(state):
    # Another capture's --terminate_existing_session (or --stop_existing_session) stops our 'lagdoctor'
    # session: PresentMon then exits 0 by itself (PM 5, 7.1) while the game plays on, and frames stop there.
    src = Fake("breakable", state)
    src.start(0.0)
    wait_for(lambda: (src.live().get("frames") or 0) > 0, what="the first frames")
    other = subprocess.run([BASE_PY, "-I", str(FAKE), "-", str(state), *pm.terminate_args()],
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
    assert other.returncode == 0                                       # it found our session and stopped it
    wait_for(lambda: not src._thread.is_alive(), 15, "the collector to end by itself")
    st, info = src.status(), src.result()["presentmon"]
    src.stop()
    assert info["exit_code"] == 0 and info["stop_path"] == "exited" and info["rows"] == PID_ROWS
    assert st.state == "partial", st
    assert "PresentMon stopped on its own at t=" in st.detail and "while the game kept running" in st.detail
    assert "no frames after t=" in st.detail


class EndsWithTheGame(Fake):
    """The game exits `after` seconds after PresentMon's output ends: its --terminate_on_proc_exit got there first."""

    def __init__(self, mode: str, state: Path, after: float) -> None:
        self.gone = threading.Event()
        self.after = after
        super().__init__(mode, state, is_alive=lambda: not self.gone.is_set())

    def _read_stdout(self, stream) -> None:
        super()._read_stdout(stream)
        threading.Timer(self.after, self.gone.set).start()


@pytest.mark.parametrize("after", [0.0, 0.6])        # at once, or within the grace our own check gets
def test_presentmon_ending_because_the_game_exited_stays_ok(state, after):
    src = EndsWithTheGame("eof", state, after)
    run_to_end(src)
    st = src.status()
    assert src.result()["presentmon"]["rows"] == PID_ROWS
    assert st.state == "ok" and "game exited" in st.detail, st


def test_presentmon_ending_by_itself_with_no_working_liveness_check_is_partial(state):
    def broken():
        raise RuntimeError("snapshot failed")

    src = Fake("eof", state, is_alive=broken)
    run_to_end(src)
    st = src.status()
    assert st.state == "partial" and "could not be checked" in st.detail, st


def test_the_default_liveness_check_uses_the_pid_list_only(state, monkeypatch):
    seen = []
    monkeypatch.setattr(psutil, "pids", lambda: seen.append(1) or [1, 2, 3])
    src = Fake("breakable", state, is_alive=None)
    src.start(0.0)
    wait_for(lambda: not src._thread.is_alive(), 15, "the capture to stop when PID 24892 is not listed")
    src.stop()
    assert seen and "game exited" in src.status().detail


def test_no_rights_is_unavailable_with_presentmons_own_reason(state):
    src = Fake("denied", state)
    run_to_end(src)
    st, info = src.status(), src.result()["presentmon"]
    assert st.state == "unavailable" and "Performance Log Users" in st.detail
    assert info["exit_code"] == 6 and info["rows"] == 0
    assert any("access denied" in ln for ln in info["stderr_tail"])
    assert src.result()["columns"] == {c: [] for c in F.ALL}


@needs_console
def test_a_session_left_running_is_replaced_once_and_the_capture_goes_on(state):
    src = Fake("busy_once", state)
    src.start(0.0)
    wait_for(lambda: (src.live().get("frames") or 0) > 0, 15, "the second attempt's frames")
    src.stop()
    assert len(lines_of(state / "terminate_calls")) == 1
    assert src.status().state == "ok" and "already running" in src.status().detail
    assert src.result()["presentmon"]["rows"] == PID_ROWS


def test_a_retried_capture_shows_its_first_frames_at_once(state):
    # The failed first attempt published its (zero) count as it ended; the retry is a new run, whose first
    # frame must show at once, not wait out the publish interval (a long one here, so it cannot slip through).
    src = Fake("busy_once", state)
    src.PUBLISH_EVERY_S = 60.0
    src.start(0.0)
    try:
        wait_for(lambda: (src.live().get("frames") or 0) > 0, 15, "the retry's first frames on the status line")
    finally:
        src.stop()
    assert len(lines_of(state / "terminate_calls")) >= 1


def test_a_crash_is_reported_and_its_session_stopped_by_name(state):
    src = Fake("crash", state)
    run_to_end(src)
    info, st = src.result()["presentmon"], src.status()
    assert info["exit_code"] == 0xC0000005 and info["stop_path"] == "exited"
    assert st.state == "partial" and "exit code" in st.detail and "crashed" in st.detail
    assert len(lines_of(state / "terminate_calls")) == 1 and not (state / "running").exists()


def test_malformed_rows_make_the_capture_partial(state):
    src = Fake("garbled", state)
    run_to_end(src)
    st, info = src.status(), src.result()["presentmon"]
    assert info["malformed"] == 1 and info["rows"] == PID_ROWS - 1
    assert st.state == "partial" and "1 malformed CSV row(s) skipped" in st.detail


def test_lost_events_make_the_capture_partial(state):
    src = Fake("lossy", state)
    run_to_end(src)
    st = src.status()
    assert st.state == "partial" and "12 ETW events were lost" in st.detail
    assert src.result()["presentmon"]["rows"] == PID_ROWS


@needs_console
def test_a_capture_that_had_to_be_killed_is_partial_and_cleaned_up(state):
    src = Fake("hung", state)
    src.start(0.0)
    wait_for(lambda: (src.live().get("frames") or 0) > 0, what="the first frames")
    src.stop()
    info, st = src.result()["presentmon"], src.status()
    assert info["stop_path"] == "kill" and st.state == "partial" and "killed" in st.detail
    assert len(lines_of(state / "terminate_calls")) == 2 and not (state / "running").exists()


def test_without_a_console_the_collector_stops_through_the_session(state, monkeypatch):
    monkeypatch.setattr(pm, "has_console", lambda: False)
    src = Fake("breakable", state)
    src.start(0.0)
    wait_for(lambda: (src.live().get("frames") or 0) > 0, what="the first frames")
    src.stop()
    assert src.result()["presentmon"]["stop_path"] == "terminate_session" and not (state / "ctrl").exists()
    assert src.result()["presentmon"]["rows"] == PID_ROWS


@needs_console
def test_a_reader_failure_still_drains_the_pipe_while_presentmon_is_stopped(state, monkeypatch):
    real_parse = pm.parse

    def parser_bug(chunks, parser=None):
        for i, row in enumerate(real_parse(chunks, parser)):
            if i == 3:
                raise RuntimeError("parser bug")
            yield row

    monkeypatch.setattr(pm, "parse", parser_bug)
    src = Fake("breakable", state)
    src.EXIT_WAIT_S = 1.0
    src.start(0.0)
    wait_for(lambda: not src._thread.is_alive(), 20, "the collector to give up")
    st, info = src.status(), src.result()["presentmon"]
    src.stop()
    assert st.state == "failed" and "parser bug" in st.detail
    assert info["stop_path"] == "ctrl_break"      # it could still write its last rows and exit; no kill needed
    assert not (state / "running").exists()


def test_a_capture_stopped_before_any_output_is_not_called_a_start_failure(state):
    src = Fake("silent", state)
    src.start(0.0)
    wait_for(lambda: (state / "running").exists(), what="the stand-in to start")
    src.stop()
    st, info = src.status(), src.result()["presentmon"]
    assert info["stop_path"] == "kill" and info["exit_code"] == 1      # we ended it: exit 1 is ours, not PresentMon's
    assert st.state == "failed" and "wrote no CSV" in st.detail and "exit code" not in st.detail


def test_a_later_header_without_needed_columns_keeps_the_earlier_frames(state):
    src = Fake("reheader", state)
    run_to_end(src)
    st, info = src.status(), src.result()["presentmon"]
    first_part = (FIX / "tc5_pid24892.csv").read_bytes().split(b"\r\n")[1:20]
    assert info["rows"] == sum(ln.split(b",")[1] == b"24892" for ln in first_part) > 0
    assert info["headers"] == 2 and info["malformed"] == 3
    assert st.state == "partial" and "lacks TimeInQPC" in st.detail


def test_a_capture_with_no_frames_from_the_target_is_not_measured(state):
    src = Fake("eof", state, pid=4)
    run_to_end(src)
    st = src.status()
    assert st.state == "failed" and "no frames came from PID 4" in st.detail and "draws the game" in st.detail
    assert src.result()["presentmon"]["headers"] == 1


def test_stop_and_result_are_safe_before_start():
    src = pm.PresentMonSource(1234)
    src.stop()
    r = src.result()
    assert r["columns"] == {c: [] for c in F.ALL} and r["main_chain"] is None and r["swap_chains"] == {}
    assert set(r) == {"columns", "swap_chains", "main_chain", "presentmon"}
    assert set(r["presentmon"]) == {"version", "exe", "args", "exit_code", "stop_path", "rows", "malformed",
                                    "rejoined", "headers", "stderr_tail"}


# ------------------------------------------------------------- the real PresentMon ---

def test_a_missing_exe_is_unavailable_and_points_at_setup():
    src = pm.PresentMonSource(1234)               # LAGDOCTOR_HOME is a fresh temp folder in every test
    run_to_end(src)
    st = src.status()
    assert st.state == "unavailable" and "not installed" in st.detail and "lagdoctor setup" in st.detail
    assert src.child_pid is None


def test_an_exe_that_fails_its_check_is_never_run(tmp_path):
    # The right size but the wrong bytes. All zeros, so even a broken guard could not run anything.
    bad = tmp_path / "PresentMon-2.6.0-x64.exe"
    bad.write_bytes(b"\0" * pm.setup.SIZE)
    src = pm.PresentMonSource(1234, exe=bad)
    run_to_end(src)
    st = src.status()
    assert st.state == "unavailable" and "integrity" in st.detail and "SHA-256" in st.detail
    assert src.child_pid is None and src.result()["presentmon"]["exe"] is None


def test_gold_replay_gives_the_reference_numbers(presentmon_exe, gold_etl_dir):
    src = pm.PresentMonSource(PID, exe=presentmon_exe, etl_file=gold_etl_dir / "test_case_5.etl")
    run_to_end(src, timeout=60)
    r, st = src.result(), src.status()
    info, cols = r["presentmon"], r["columns"]
    assert st.state == "ok", st
    assert info["exit_code"] == 0 and info["stop_path"] == "exited" and info["malformed"] == 0 and info["headers"] == 1
    assert info["args"][:2] == ["--etl_file", str(gold_etl_dir / "test_case_5.etl")]
    assert r["swap_chains"] == {"0x2A70D2CAC00": {"frames": 258, "displayed": 174, "runtime": "DXGI"},
                                "0x0": {"frames": 7, "displayed": 5, "runtime": "Other"}}
    assert r["main_chain"] == "0x2A70D2CAC00"
    main = [i for i, c in enumerate(cols[F.SWAP_CHAIN]) if c == "0x2A70D2CAC00"]
    assert round(statistics.median(cols[F.FRAME_MS][i] for i in main), 3) == 11.051
    # t comes from TimeInQPC: within the chain its steps equal MsBetweenPresents (both 10 MHz here).
    t = [cols[F.T][i] for i in main]
    steps = [(b - a) * 1000 for a, b in zip(t, t[1:])]
    assert max(abs(s - cols[F.FRAME_MS][i]) for s, i in zip(steps, main[1:])) < 0.01
    # Independent readout: PresentMon's own expected CSV for this capture, read with the csv module.
    with open(gold_etl_dir / "test_case_5.csv", encoding="utf-8-sig", newline="") as f:
        gold = [g for g in csv.DictReader(f) if g["ProcessID"] == str(PID) and g["SwapChainAddress"] == "0x2A70D2CAC00"]
    assert len(gold) == 258 and sum(g["MsUntilDisplayed"] == "NA" for g in gold) == 258 - 174


def test_an_unfiltered_replay_keys_chains_by_process(presentmon_exe, gold_etl_dir):
    src = pm.PresentMonSource(None, exe=presentmon_exe, etl_file=gold_etl_dir / "test_case_5.etl")
    run_to_end(src, timeout=60)
    chains = src.result()["swap_chains"]
    assert chains["24892:0x2A70D2CAC00"]["frames"] == 258
    assert chains["24892:0x0"]["frames"] == 7 and chains["2656:0x0"]["frames"] == 5   # two "0x0" chains, kept apart
    assert sum(c["frames"] for c in chains.values()) == 647


# ETW session query by name (NE 5.1 layouts): 4201 = no such session, 5 = one exists that is not ours.
class _WNODE_HEADER(ctypes.Structure):
    _fields_ = [("BufferSize", wintypes.ULONG), ("ProviderId", wintypes.ULONG), ("HistoricalContext", ctypes.c_uint64),
                ("TimeStamp", ctypes.c_int64), ("Guid", ctypes.c_byte * 16), ("ClientContext", wintypes.ULONG),
                ("Flags", wintypes.ULONG)]


class _EVENT_TRACE_PROPERTIES(ctypes.Structure):
    _fields_ = [("Wnode", _WNODE_HEADER), ("BufferSize", wintypes.ULONG), ("MinimumBuffers", wintypes.ULONG),
                ("MaximumBuffers", wintypes.ULONG), ("MaximumFileSize", wintypes.ULONG), ("LogFileMode", wintypes.ULONG),
                ("FlushTimer", wintypes.ULONG), ("EnableFlags", wintypes.ULONG), ("FlushThreshold", ctypes.c_long),
                ("NumberOfBuffers", wintypes.ULONG), ("FreeBuffers", wintypes.ULONG), ("EventsLost", wintypes.ULONG),
                ("BuffersWritten", wintypes.ULONG), ("LogBuffersLost", wintypes.ULONG),
                ("RealTimeBuffersLost", wintypes.ULONG), ("LoggerThreadId", wintypes.HANDLE),
                ("LogFileNameOffset", wintypes.ULONG), ("LoggerNameOffset", wintypes.ULONG)]


assert ctypes.sizeof(_WNODE_HEADER) == 48 and ctypes.sizeof(_EVENT_TRACE_PROPERTIES) == 120
_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
_advapi32.ControlTraceW.argtypes = [ctypes.c_uint64, wintypes.LPCWSTR, ctypes.c_void_p, wintypes.ULONG]
_advapi32.ControlTraceW.restype = wintypes.ULONG


def query_session(name: str) -> int:
    base = ctypes.sizeof(_EVENT_TRACE_PROPERTIES)
    buf = ctypes.create_string_buffer(base + 2 * 1024 * ctypes.sizeof(ctypes.c_wchar))
    props = _EVENT_TRACE_PROPERTIES.from_buffer(buf)
    props.Wnode.BufferSize, props.Wnode.Flags = len(buf), 0x00020000     # WNODE_FLAG_TRACED_GUID
    props.LoggerNameOffset, props.LogFileNameOffset = base, base + 1024 * ctypes.sizeof(ctypes.c_wchar)
    return _advapi32.ControlTraceW(0, name, ctypes.addressof(buf), 0)   # EVENT_TRACE_CONTROL_QUERY


def test_the_session_probe_can_see_a_session_that_exists():
    # The control for the "nothing left behind" check below: Eventlog-Security always runs (PM 6.3).
    assert query_session("Eventlog-Security") in (0, 5)
    assert query_session("lagdoctor_probe_that_never_existed") == 4201


def test_unelevated_live_start_is_unavailable_fast_and_leaves_nothing_behind(presentmon_exe):
    if elevation.can_trace():
        pytest.skip("this token can trace; the negative test needs one without ETW rights")
    if query_session(pm.SESSION_NAME) != 4201:
        pytest.skip("a 'lagdoctor' trace session is running (a real capture?); not touching it")
    src = pm.PresentMonSource(os.getpid(), exe=presentmon_exe)
    started = time.monotonic()
    run_to_end(src, timeout=15)
    took = time.monotonic() - started
    st, info = src.status(), src.result()["presentmon"]
    assert st.state == "unavailable" and "access denied" in st.detail and "Performance Log Users" in st.detail
    assert took < 5, took
    assert info["exit_code"] == 6 and info["stop_path"] == "exited" and info["rows"] == 0
    assert any("access denied" in ln for ln in info["stderr_tail"])
    assert query_session(pm.SESSION_NAME) == 4201
    assert src.child_pid is not None and src.child_pid not in psutil.pids()
