"""The scripts that record as watch does honour the machine-wide trace lock (lagdoctor/capture/tracelock.py) as watch
does (cli._TraceGuard.here): the end-to-end run (scripts/e2e.py) for each mode, and the overhead benchmark
(scripts/overhead/bench.py) for its whole command, or its recorder when it runs on its own. Their PresentMon and network
trace have fixed session names, so starting them would stop another Windows session's lag-doctor recording, which the
capture lock (this Windows session's) does not see.

Nothing here starts LagLab, PresentMon or a trace session: each test stops the script at the step after the lock.
tests/conftest.py gives every test a trace lock name of its own; the capture lock's is the test's own too.
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import e2e  # noqa: E402
from overhead import bench, recorder  # noqa: E402

from lagdoctor import cli  # noqa: E402
from lagdoctor.capture import session as sess  # noqa: E402
from lagdoctor.capture import target as tgt  # noqa: E402
from lagdoctor.capture import tracelock  # noqa: E402
from lagdoctor.winapi import elevation  # noqa: E402
from tests.test_overhead_bench import stub_runs  # noqa: E402,F401 - a fixture: the benchmark with LagLab left out

ME = elevation.token_user_sid()
MINE = {ME}                       # trusted: this account's own locks read as lag-doctor recordings here


@pytest.fixture
def capture_lock(monkeypatch):
    """A capture lock name of the test's own (session.CaptureLock), never this Windows session's."""
    monkeypatch.setattr(sess, "CAPTURE_LOCK_NAME", f"Local\\lagdoctor-test-{uuid.uuid4().hex}")


@pytest.fixture
def elevated(monkeypatch):
    """An administrator's window as this token can stand in for one: the lock it holds is owned by this account, the
    one owner an unelevated token may set, and trusted as MINE."""
    monkeypatch.setattr(elevation, "is_elevated", lambda: True)
    monkeypatch.setattr(elevation, "can_trace", lambda: True)
    monkeypatch.setattr(cli, "_trace_lock", lambda: tracelock.TraceLock(ME, trusted=MINE))


@pytest.fixture
def other_session():
    """Another Windows session's lag-doctor recording, holding the lock."""
    other = tracelock.TraceLock(ME, trusted=MINE)
    assert other.acquire() == tracelock.HELD
    yield other
    other.release()


def _capture_lock_is_free() -> bool:
    probe = sess.CaptureLock()
    try:
        return probe.acquire()
    finally:
        probe.release()


# ----------------------------------------------------------------------------------------- the e2e run ---

def test_an_e2e_mode_beside_another_sessions_recording_stops_before_laglab_starts(capture_lock, elevated,
                                                                                  other_session, monkeypatch,
                                                                                  tmp_path):
    monkeypatch.setattr(e2e.subprocess, "Popen", lambda *a, **k: pytest.fail("LagLab was started"))
    with pytest.raises(e2e.Busy) as busy:
        e2e.run_mode("smooth", e2e.parse([]), tmp_path)
    assert str(busy.value) == cli.TRACE_BUSY
    assert _capture_lock_is_free()


def test_an_e2e_mode_holds_the_trace_lock_from_before_laglab_starts_and_lets_it_go(capture_lock, elevated,
                                                                                   monkeypatch, tmp_path):
    seen = []

    def popen(*a, **k):
        seen.append(tracelock.check(trusted=MINE))
        raise RuntimeError("no LagLab in this test")
    monkeypatch.setattr(e2e.subprocess, "Popen", popen)
    with pytest.raises(RuntimeError):
        e2e.run_mode("smooth", e2e.parse([]), tmp_path)
    assert seen == [tracelock.BUSY]
    assert tracelock.check(trusted=MINE) == tracelock.FREE and _capture_lock_is_free()


# ----------------------------------------------------------------------------------- the benchmark ---

def test_the_benchmark_beside_another_sessions_recording_starts_nothing_and_lets_the_capture_lock_go(
        elevated, other_session, stub_runs, tmp_path, capsys):  # noqa: F811
    assert bench.main(["cycles", "--suite", "intel-cpu", "--out", str(tmp_path)]) == 4
    out = capsys.readouterr().out
    assert cli.TRACE_BUSY in out and "nothing was started" in out
    assert stub_runs.calls == [] and stub_runs.released == 1


def test_the_benchmark_holds_the_trace_lock_for_its_whole_command(elevated, stub_runs, tmp_path,  # noqa: F811
                                                                  monkeypatch):
    seen = []
    stubbed = bench.run_cycles

    def run_cycles(*a):
        seen.append(tracelock.check(trusted=MINE))
        return stubbed(*a)
    monkeypatch.setattr(bench, "run_cycles", run_cycles)
    assert bench.main(["cycles", "--suite", "intel-cpu", "--runs", "2", "--gap", "0", "--out", str(tmp_path)]) == 0
    assert seen == [tracelock.BUSY, tracelock.BUSY]                  # held, by the command, across both runs
    assert tracelock.check(trusted=MINE) == tracelock.FREE and stub_runs.released == 1


def _recorder_argv(tmp_path: Path, *extra: str) -> list[str]:
    return ["--pid", "4242", "--capture", str(tmp_path / "capture.ldcap.json.gz"), "--stats",
            str(tmp_path / "stats.json"), "--win-start", "0", "--win-end", "1", "--frames", *extra]


def test_the_recorder_on_its_own_beside_another_sessions_recording_records_nothing(capture_lock, elevated,
                                                                                    other_session, monkeypatch,
                                                                                    tmp_path):
    monkeypatch.setattr(tgt, "find_target", lambda **kw: pytest.fail("the recorder went on"))
    assert recorder.main(_recorder_argv(tmp_path)) == 3
    assert json.loads((tmp_path / "stats.json").read_text(encoding="utf-8"))["error"] == cli.TRACE_BUSY
    assert _capture_lock_is_free()


def test_the_recorder_on_its_own_holds_the_trace_lock_and_leaves_it_to_the_benchmark_that_started_it(
        capture_lock, elevated, monkeypatch, tmp_path):
    seen = []

    def find_target(**kw):
        seen.append(tracelock.check(trusted=MINE))
        raise RuntimeError("no game in this test")
    monkeypatch.setattr(tgt, "find_target", find_target)
    with pytest.raises(RuntimeError):
        recorder.main(_recorder_argv(tmp_path))
    assert seen == [tracelock.BUSY] and tracelock.check(trusted=MINE) == tracelock.FREE
    # Started by the benchmark (--no-lock), which holds both locks itself: it takes neither.
    monkeypatch.setattr(cli, "_trace_lock", lambda: pytest.fail("the recorder took the trace lock"))
    with pytest.raises(RuntimeError):
        recorder.main(_recorder_argv(tmp_path, "--no-lock"))
    assert seen == [tracelock.BUSY, tracelock.FREE]
