"""`lagdoctor undo` and the journal check every command runs first (cli.cmd_undo, cli._settle_journal).

LAGDOCTOR_HOME is redirected for every test (conftest.py), so Journal() here is a temporary journal. The one
real process involved is a child these tests start; it is never opened by the code under test.
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from lagdoctor import cli
from lagdoctor.fixes import catalog
from lagdoctor.fixes.base import GONE, Fix
from lagdoctor.fixes.journal import Journal
from lagdoctor.winapi import ntquery, process

BASE_PYTHON = getattr(sys, "_base_executable", sys.executable)   # not the venv launcher (docs/learnings.md)
STORE: dict[str, object] = {}


class LastingFix(Fix):
    """An in-memory setting that outlives lag-doctor (like a refresh rate): read GONE means 'not connected'."""

    id = "test_cli_undo_lasting"
    title = "A lasting fake setting"

    def target(self, params):
        return f"fake setting {params['name']}"

    def read(self, params):
        return STORE.get(params["name"], GONE)

    def write(self, params, value):
        STORE[params["name"]] = value

    def wanted(self, params, current):
        return 5


@pytest.fixture
def lasting():
    STORE.clear()
    catalog.register(LastingFix)
    yield LastingFix
    catalog._REGISTRY.pop(LastingFix.id, None)
    STORE.clear()


@pytest.fixture
def opens(monkeypatch):
    """Every PID passed to the real OpenProcess from here on (the call still goes to Windows)."""
    seen: list[int] = []
    real = process._k32.OpenProcess

    def spy(access, inherit, pid):
        seen.append(pid)
        return real(access, inherit, pid)
    monkeypatch.setattr(process._k32, "OpenProcess", spy)
    return seen


@pytest.fixture
def child():
    """A process of our own that just waits; its create time comes from the snapshot, which opens nothing."""
    proc = subprocess.Popen([BASE_PYTHON, "-I", "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
    try:
        info = None
        for _ in range(50):
            info = ntquery.Snapshotter().snapshot().get(proc.pid)
            if info is not None:
                break
        assert info is not None, "the child never showed up in the snapshot"
        yield proc.pid, info.create_time
    finally:
        proc.kill()
        proc.wait(10)
        proc.stdin.close()


def _priority_entry(journal: Journal, pid: int, ctime: int):
    params = {"pid": pid, "create_time": ctime, "class": "below_normal", "exclude": [4], "name": "python.exe"}
    e = journal.begin("background_priority", f"priority class of python.exe (process {pid})", params,
                      before="normal", after="below_normal", session_only=True)
    journal.set_state(e.id, "applied")
    return e


# ------------------------------------------------------------- start-up check --

def test_start_up_settles_a_reused_pid_without_opening_any_process(child, opens):
    # Review finding SEC-1: the PID of a journaled priority change may belong to the game by now.
    pid, ctime = child
    j = Journal()
    reused = _priority_entry(j, pid, ctime + 1)           # that process ended; the PID went to another one
    running = _priority_entry(j, pid, ctime)              # this one still runs
    cli._settle_journal()
    assert Journal().get(reused.id).state == "undone"
    assert Journal().get(running.id).state == "applied"
    assert pid not in opens and opens == []


def test_start_up_says_the_journal_was_copied_and_fixes_are_paused(capsys):
    Journal().path.write_bytes(b'{"schema": "lagdoctor.journal/2", "entries": []}')
    cli._settle_journal()
    out = capsys.readouterr().out
    assert "copied to" in out and "paused" in out and "Nothing was lost" not in out


def test_start_up_carries_on_past_an_entry_it_cannot_settle(lasting, capsys):
    j = Journal()
    j.begin("fix_from_a_newer_lag_doctor", "something", {}, 1, 2)       # left pending, fix unknown here
    pending = j.begin(LastingFix.id, "fake setting a", {"name": "a"}, 1, 5)
    STORE["a"] = 5                                        # the write happened, then the tool crashed
    cli._settle_journal()
    assert Journal().get(pending.id).state == "applied"
    assert "fix_from_a_newer_lag_doctor" in capsys.readouterr().out


# ------------------------------------------------------------------- undo --

def test_undo_on_an_unreadable_journal_says_so_plainly(capsys):
    # Review finding CC-3: no traceback, one copy of the bad bytes, and the journal itself left in place.
    path = Journal().path
    path.write_bytes(b'{"schema": "lagdoctor.journal/1", "entries": [ {broken')
    original = path.read_bytes()
    for argv in (["undo"], ["undo", "--all"], ["undo", "--all", "--force"]):
        assert cli.main(argv) == 1
    out = capsys.readouterr().out
    copies = sorted(path.parent.glob(f"{path.stem}.corrupt-*"))
    assert len(copies) == 1 and copies[0].read_bytes() == original
    assert str(copies[0]) in out and "paused" in out and "Traceback" not in out
    assert path.read_bytes() == original


def test_undo_lists_a_conflict_and_restores_it_only_with_force(lasting, capsys):
    # Review finding SEC-4: a crash left the setting neither the original nor ours; heal marked it a conflict.
    j = Journal()
    entry = j.begin(LastingFix.id, "fake setting a", {"name": "a"}, 1, 5)
    STORE["a"] = 3
    cli._settle_journal()
    assert Journal().get(entry.id).state == "conflict"
    capsys.readouterr()
    assert cli.main(["undo"]) == 0
    listing = capsys.readouterr().out
    [line] = [ln for ln in listing.splitlines() if entry.id in ln]
    assert "CONFLICT" in line and "--force" in listing           # labelled on its own line, with the way out
    assert cli.main(["undo", "--all"]) == 1                # without --force it is left alone
    assert STORE["a"] == 3 and "Left alone" in capsys.readouterr().out
    assert cli.main(["undo", "--all", "--force"]) == 0
    assert STORE["a"] == 1 and Journal().get(entry.id).state == "undone"


def test_undo_by_id_with_force_restores_a_conflict(lasting):
    j = Journal()
    entry = j.begin(LastingFix.id, "fake setting a", {"name": "a"}, 1, 5)
    STORE["a"] = 3
    LastingFix().heal(entry, j)
    assert cli.main(["undo", entry.id, "--force"]) == 0
    assert STORE["a"] == 1 and Journal().get(entry.id).state == "undone"


def test_undo_of_a_lasting_change_that_is_not_connected_is_left_alone(lasting, capsys):
    j = Journal()
    entry = j.begin(LastingFix.id, "fake setting a", {"name": "a"}, 1, 5)
    j.set_state(entry.id, "applied")                      # then the device was unplugged (reads GONE)
    assert cli.main(["undo", "--all"]) == 1
    out = capsys.readouterr().out
    assert "Left alone" in out and "Restored" not in out and "when it is connected" in out
    assert Journal().get(entry.id).state == "applied"
    STORE["a"] = 5                                        # plugged in again
    assert cli.main(["undo", "--all"]) == 0 and STORE["a"] == 1


def test_undo_of_a_fix_this_version_does_not_know_is_reported(capsys):
    j = Journal()
    entry = j.begin("fix_from_a_newer_lag_doctor", "something new", {}, 1, 2)
    j.set_state(entry.id, "applied")
    assert cli.main(["undo", "--all"]) == 1
    assert "does not know the fix 'fix_from_a_newer_lag_doctor'" in capsys.readouterr().out
    assert Journal().get(entry.id).state == "applied"


def test_undo_keeps_what_a_newer_lag_doctor_wrote_in_an_entry(lasting):
    j = Journal()
    entry = j.begin(LastingFix.id, "fake setting a", {"name": "a"}, 1, 5)
    j.set_state(entry.id, "applied")
    STORE["a"] = 5
    data = json.loads(j.path.read_text(encoding="utf-8"))
    data["entries"][0]["from_the_future"] = [1, 2]
    j.path.write_text(json.dumps(data), encoding="utf-8")
    assert cli.main(["undo", "--all"]) == 0 and STORE["a"] == 1
    saved = json.loads(j.path.read_text(encoding="utf-8"))["entries"][0]
    assert saved["state"] == "undone" and saved["from_the_future"] == [1, 2]
