"""The safety every fix shares: write-ahead journal, readback, undo, heal, corruption, sandboxed registry."""
from __future__ import annotations

import hashlib
import json
import threading
import time
import winreg

import pytest

from lagdoctor.fixes.base import ABSENT, GONE, NOT_CONNECTED, Fix
from lagdoctor.fixes.journal import Journal, JournalCorrupt
from lagdoctor.fixes.registry import RegistryBackend, dword, sz


class FakeFix(Fix):
    """An in-memory setting with switchable failure modes."""

    id = "fake"
    title = "Fake setting"

    def __init__(self, store: dict, journal: Journal | None = None) -> None:
        self.store = store
        self.journal = journal
        self.fail_write = False       # write raises before changing anything
        self.write_ignored = False    # write "succeeds" but nothing changes
        self.writes: list = []

    def target(self, params):
        return f"fake:{params['key']}"

    def read(self, params):
        return self.store.get(params["key"], dict(ABSENT))

    def write(self, params, value):
        if self.journal is not None:
            # Write-ahead: by the time we change anything, the original must be on disk.
            pending = [e for e in self.journal.entries() if e.state == "pending"]
            assert pending and pending[-1].before == self.read(params), "write happened before journaling"
        self.writes.append(value)
        if self.fail_write:
            raise OSError("access denied")
        if self.write_ignored:
            return
        if isinstance(value, dict) and value.get("__absent__"):
            self.store.pop(params["key"], None)
        else:
            self.store[params["key"]] = value

    def wanted(self, params, current):
        return params["want"]


@pytest.fixture
def journal(tmp_path):
    return Journal(tmp_path / "journal.json", lock_timeout=0.3)


def test_apply_journals_the_original_before_writing_then_marks_applied(journal):
    fix = FakeFix({"k": 1}, journal)
    plan = fix.plan({"key": "k", "want": 2})
    res = fix.apply(plan, journal)
    assert res.ok and res.state == "applied"
    assert fix.store["k"] == 2
    [entry] = journal.entries()
    assert (entry.before, entry.after, entry.state) == (1, 2, "applied")


def test_undo_restores_the_journaled_original(journal):
    fix = FakeFix({"k": 1})
    fix.apply(fix.plan({"key": "k", "want": 2}), journal)
    [entry] = journal.undoable()
    assert fix.undo(entry, journal).ok
    assert fix.store["k"] == 1
    assert journal.get(entry.id).state == "undone"
    assert journal.undoable() == []


def test_a_value_that_did_not_exist_is_deleted_again_on_undo(journal):
    fix = FakeFix({})
    fix.apply(fix.plan({"key": "k", "want": 5}), journal)
    assert fix.store == {"k": 5}
    fix.undo(journal.undoable()[0], journal)
    assert fix.store == {}


def test_apply_refuses_when_the_value_moved_after_the_plan(journal):
    fix = FakeFix({"k": 1})
    plan = fix.plan({"key": "k", "want": 2})
    fix.store["k"] = 7                       # the user changed it while reading the plan
    res = fix.apply(plan, journal)
    assert not res.ok and res.state == "stale"
    assert fix.store["k"] == 7 and journal.entries() == []


def test_noop_plan_changes_nothing_and_journals_nothing(journal):
    fix = FakeFix({"k": 2})
    res = fix.apply(fix.plan({"key": "k", "want": 2}), journal)
    assert res.ok and res.state == "noop" and fix.writes == [] and journal.entries() == []


def test_a_write_that_does_not_take_is_reported_and_journaled_failed(journal):
    fix = FakeFix({"k": 1})
    fix.write_ignored = True
    res = fix.apply(fix.plan({"key": "k", "want": 2}), journal)
    assert not res.ok and res.state == "failed"
    assert fix.store["k"] == 1
    assert journal.entries()[0].state == "failed"


def test_a_partial_write_is_rolled_back_to_the_original(journal):
    class HalfFix(FakeFix):
        def readback(self, params):
            return "garbled"                 # the change landed but reads back wrong
    fix = HalfFix({"k": 1})
    res = fix.apply(fix.plan({"key": "k", "want": 2}), journal)
    assert not res.ok
    assert fix.store["k"] == 1               # original put back
    assert fix.writes == [2, 1]


def test_a_write_error_leaves_the_value_untouched(journal):
    fix = FakeFix({"k": 1})
    fix.fail_write = True
    res = fix.apply(fix.plan({"key": "k", "want": 2}), journal)
    assert not res.ok and "access denied" in res.message
    assert fix.store["k"] == 1 and journal.entries()[0].state == "failed"


def test_undo_never_clobbers_a_value_the_user_changed_later(journal):
    fix = FakeFix({"k": 1})
    fix.apply(fix.plan({"key": "k", "want": 2}), journal)
    fix.store["k"] = 9                       # the user picked something else afterwards
    entry = journal.undoable()[0]
    res = fix.undo(entry, journal)
    assert not res.ok and res.state == "conflict" and fix.store["k"] == 9
    assert fix.undo(entry, journal, force=True).ok
    assert fix.store["k"] == 1


def test_undoing_newest_first_restores_the_first_original(journal):
    fix = FakeFix({"k": "A"})
    fix.apply(fix.plan({"key": "k", "want": "B"}), journal)
    fix.apply(fix.plan({"key": "k", "want": "C"}), journal)
    for entry in journal.undoable():         # newest first
        assert fix.undo(entry, journal).ok
    assert fix.store["k"] == "A"


class SessionFix(FakeFix):
    session_only = True                      # like a process priority: it goes away with its subject


def test_undo_of_a_session_only_change_whose_subject_is_gone(journal):
    # Changed on purpose (review finding SEC-4): this used FakeFix, which is not session-only, so it pinned
    # "undone" for every fix. Only a session-only change goes away with its subject.
    fix = SessionFix({"k": 1})
    fix.apply(fix.plan({"key": "k", "want": 2}), journal)
    fix.store["k"] = dict(GONE)              # e.g. the process it changed exited
    res = fix.undo(journal.undoable()[0], journal)
    assert res.ok and journal.entries()[0].state == "undone"


def test_undo_of_a_lasting_change_whose_subject_is_not_there_keeps_the_entry(journal):
    fix = FakeFix({"k": 1})                  # not session-only: e.g. an unplugged display keeps its mode
    fix.apply(fix.plan({"key": "k", "want": 2}), journal)
    fix.store["k"] = dict(GONE)
    [entry] = journal.undoable()
    for force in (False, True):
        res = fix.undo(entry, journal, force=force)
        assert not res.ok and res.state == "applied" and "run undo again when it is connected" in res.message
    assert journal.get(entry.id).state == "applied" and journal.get(entry.id).note == NOT_CONNECTED
    assert fix.writes == [2]                 # nothing written while it was away
    fix.store["k"] = 2                       # back, with lag-doctor's value
    assert fix.undo(journal.undoable()[0], journal).ok and fix.store["k"] == 1


@pytest.mark.parametrize("live, state", [(1, "failed"), (2, "applied"), (5, "conflict")])
def test_heal_settles_an_entry_left_pending_by_a_crash(journal, live, state):
    fix = FakeFix({"k": live})
    entry = journal.begin("fake", "fake:k", {"key": "k"}, before=1, after=2)  # crash right after this
    fix.heal(entry, journal)
    assert journal.get(entry.id).state == state
    assert fix.store["k"] == live            # healing never writes


@pytest.mark.parametrize("cls, state", [(SessionFix, "undone"), (FakeFix, "applied")])
def test_heal_of_an_entry_whose_subject_is_not_there(journal, cls, state):
    fix = cls({"k": dict(GONE)})
    entry = journal.begin("fake", "fake:k", {"key": "k"}, before=1, after=2,
                          session_only=cls.session_only)
    fix.heal(entry, journal)
    assert journal.get(entry.id).state == state   # a lasting change stays on the undo list, outcome unknown
    assert fix.writes == []


def test_a_conflict_is_left_alone_and_restored_with_force(journal):
    # A crash left the value neither the original nor ours (game_dvr_off between its two writes).
    fix = FakeFix({"k": 5})
    entry = journal.begin("fake", "fake:k", {"key": "k"}, before=1, after=2)
    fix.heal(entry, journal)
    [entry] = journal.outstanding()
    assert entry.state == "conflict" and journal.undoable() == []
    res = fix.undo(entry, journal)
    assert not res.ok and fix.store["k"] == 5 and journal.get(entry.id).state == "conflict"
    res = fix.undo(entry, journal, force=True)
    assert res.ok and res.state == "undone" and fix.store["k"] == 1
    assert journal.outstanding() == []


def test_a_forced_restore_of_a_conflict_that_does_not_take_stays_a_conflict(journal):
    fix = FakeFix({"k": 5})
    entry = journal.begin("fake", "fake:k", {"key": "k"}, before=1, after=2)
    fix.heal(entry, journal)
    fix.write_ignored = True
    res = fix.undo(journal.outstanding()[0], journal, force=True)
    assert not res.ok and res.state == "conflict" and "did not take" in res.message
    assert journal.get(entry.id).state == "conflict" and fix.store["k"] == 5


def test_a_failed_restore_that_leaves_the_change_in_place_stays_undoable(journal):
    class StoredForLater(FakeFix):
        """Each write lands in the store, but readback says it has not taken yet (and the restore fails)."""

        def write(self, params, value):
            if value == 1:
                raise OSError("the original could not be put back")
            super().write(params, value)

        def readback(self, params):
            return {"pending": self.read(params)}

        def also_ours(self, live, before, after):
            return live == {"pending": after}

    fix = StoredForLater({"k": 1})
    res = fix.apply(fix.plan({"key": "k", "want": 2}), journal)
    assert not res.ok and res.state == "applied" and f"lagdoctor undo {res.entry_id}" in res.message
    assert "could not be put back" in res.message
    assert journal.get(res.entry_id).state == "applied"


def test_the_default_ended_check_reads_the_live_value():
    assert FakeFix({"k": dict(GONE)}).ended({"key": "k"}) is True
    assert FakeFix({"k": 1}).ended({"key": "k"}) is False


def test_undo_says_plainly_when_the_value_cannot_be_read(journal):
    class Unreadable(FakeFix):
        def read(self, params):
            raise OSError("Windows did not answer")
    fix = FakeFix({"k": 1})
    fix.apply(fix.plan({"key": "k", "want": 2}), journal)
    res = Unreadable({"k": 2}).undo(journal.undoable()[0], journal)
    assert not res.ok and "could not be read (Windows did not answer)" in res.message


def test_a_corrupt_journal_is_parked_never_replaced(journal):
    journal.path.write_bytes(b'{"schema": "lagdoctor.journal/1", "entries": [ {broken')
    original = journal.path.read_bytes()
    with pytest.raises(JournalCorrupt) as err:
        journal.entries()
    assert err.value.parked.read_bytes() == original
    assert journal.path.read_bytes() == original          # still there, untouched
    with pytest.raises(JournalCorrupt):
        journal.begin("fake", "t", {}, 1, 2)             # and nothing gets written over it
    assert journal.path.read_bytes() == original
    assert "copied to" in str(err.value) and str(err.value.parked) in str(err.value)


def _parked(journal: Journal) -> list:
    return sorted(journal.path.parent.glob(f"{journal.path.stem}.corrupt-*"))


def test_a_corrupt_journal_is_copied_aside_once_not_on_every_read(journal):
    journal.path.write_bytes(b'{"schema": "lagdoctor.journal/2", "entries": []}')    # a newer format
    for _ in range(4):                                   # every command reads it again
        with pytest.raises(JournalCorrupt) as err:
            journal.entries()
    assert _parked(journal) == [err.value.parked]
    journal.path.write_bytes(b"\x00\xffdamaged")         # other bad bytes get their own copy
    with pytest.raises(JournalCorrupt):
        journal.entries()
    assert len(_parked(journal)) == 2
    message = err.value.plain()
    assert str(err.value.parked) in message and "paused" in message and "lagdoctor.journal/2" in message


def test_a_parked_copy_is_never_overwritten(journal):
    bad = b'{"schema": "lagdoctor.journal/1", "entries": [ {broken'
    journal.path.write_bytes(bad)
    taken = journal.path.with_name(f"{journal.path.stem}.corrupt-{hashlib.sha256(bad).hexdigest()[:16]}.json")
    taken.write_bytes(b"something else under that name")
    with pytest.raises(JournalCorrupt) as err:
        journal.entries()
    assert err.value.parked != taken and err.value.parked.read_bytes() == bad
    assert taken.read_bytes() == b"something else under that name"


def test_unknown_entry_keys_are_kept_not_called_corrupt(journal):
    # A newer lag-doctor added a field: this one still reads the journal and writes the field back unchanged.
    entry = journal.begin("fake", "fake:k", {"key": "k"}, 1, 2)
    data = json.loads(journal.path.read_text(encoding="utf-8"))
    data["entries"][0]["restart_pending"] = {"hz": 144}
    data["written_by"] = "lag-doctor 9.9"
    journal.path.write_text(json.dumps(data), encoding="utf-8")
    [loaded] = journal.entries()
    assert loaded.extra == {"restart_pending": {"hz": 144}} and loaded.state == "pending"
    journal.set_state(entry.id, "applied")
    data = json.loads(journal.path.read_text(encoding="utf-8"))
    assert data["entries"][0]["restart_pending"] == {"hz": 144} and "extra" not in data["entries"][0]
    assert data["entries"][0]["state"] == "applied" and data["written_by"] == "lag-doctor 9.9"
    assert _parked(journal) == []


@pytest.mark.parametrize("bad", [
    b'{"schema": "lagdoctor.journal/1", "entries": [{"id": "x"}]}',          # fields missing
    b'{"schema": "lagdoctor.journal/1", "entries": {"id": "x"}}',            # not a list
    b'{"schema": "lagdoctor.journal/1", "entries": {}}',                     # not a list, even when empty
    b'{"schema": "lagdoctor.journal/1", "entries": ["x"]}',                  # an entry that is not an object
    b'["lagdoctor.journal/1"]',
])
def test_a_journal_that_cannot_be_understood_is_still_corrupt(journal, bad):
    journal.path.write_bytes(bad)
    with pytest.raises(JournalCorrupt):
        journal.entries()
    assert journal.path.read_bytes() == bad


def test_the_journal_lock_blocks_a_second_writer(journal):
    held = threading.Event()
    release = threading.Event()

    def holder():
        with journal._locked():
            held.set()
            release.wait(5)

    t = threading.Thread(target=holder)
    t.start()
    try:
        assert held.wait(5)
        start = time.monotonic()
        with pytest.raises(TimeoutError):
            Journal(journal.path, lock_timeout=0.3).begin("fake", "t", {}, 1, 2)
        assert time.monotonic() - start >= 0.25
    finally:
        release.set()
        t.join(5)
    Journal(journal.path, lock_timeout=0.3).begin("fake", "t", {}, 1, 2)   # free again afterwards


@pytest.fixture
def sandbox(reg_sandbox):
    return reg_sandbox


def test_registry_backend_round_trips_types_inside_its_sandbox(sandbox):
    path = r"Software\Microsoft\GameBar"
    assert sandbox.get("HKCU", path, "AutoGameModeEnabled")["__absent__"]
    sandbox.set("HKCU", path, "AutoGameModeEnabled", dword(1))
    sandbox.set("HKLM", r"SYSTEM\Fake", "Name", sz("x"))
    assert sandbox.get("HKCU", path, "AutoGameModeEnabled") == {"type": winreg.REG_DWORD, "data": 1}
    assert sandbox.get("HKLM", r"SYSTEM\Fake", "Name") == {"type": winreg.REG_SZ, "data": "x"}
    # Independent readout: the value really is under the sandbox key, in HKCU.
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, f"{sandbox.prefix}\\HKCU\\{path}") as k:
        assert winreg.QueryValueEx(k, "AutoGameModeEnabled") == (1, winreg.REG_DWORD)
    sandbox.set("HKCU", path, "AutoGameModeEnabled", dict(ABSENT))
    assert sandbox.get("HKCU", path, "AutoGameModeEnabled")["__absent__"]


def test_registry_backend_will_not_delete_a_tree_outside_a_sandbox():
    with pytest.raises(RuntimeError):
        RegistryBackend().delete_tree()
