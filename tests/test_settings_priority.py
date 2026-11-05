"""background_priority's policy, on an in-memory process table: what it refuses, and that it refuses
before any process would be opened. The live run on a real child process is in test_settings_process.py."""
from __future__ import annotations

import errno
import os

import pytest

from lagdoctor.fixes import catalog
from lagdoctor.fixes.base import GONE
from lagdoctor.fixes.journal import Journal
from lagdoctor.fixes.priority import BackgroundPriorityFix, Refused
from lagdoctor.winapi import process

GAME, GAME_CHILD, HOG, SERVICE, PROTECTED, OTHER_USER = 1000, 1004, 2000, 3000, 3004, 3008
CT = 134_000_000_000_000_000
NAMES = {GAME: "Game.exe", GAME_CHILD: "GameCrashHandler.exe", HOG: "OneDrive.exe", SERVICE: "SearchIndexer.exe",
         PROTECTED: "MsMpEng.exe", OTHER_USER: "explorer.exe"}


class FakeProc:
    """The winapi.process calls the fix makes, in memory. `opened` lists every PID a real call would open;
    running_name stands for the system snapshot, which opens nothing, so it is not listed there."""

    def __init__(self) -> None:
        self.classes = {pid: process.NORMAL for pid in (GAME, GAME_CHILD, HOG, SERVICE, PROTECTED, OTHER_USER)}
        self.ctimes = {pid: CT for pid in self.classes}
        self.names = dict(NAMES)
        self.services = {SERVICE: ["WSearch"]}
        self.protected = {PROTECTED}
        self.other_users = {OTHER_USER}
        self.owner_check_denied = False
        self.opened: list[int] = []
        self.snapshots: list[int] = []
        self.sets: list[tuple[int, int]] = []

    def _open(self, pid, create_time):
        self.opened.append(pid)
        if pid not in self.classes or (create_time is not None and self.ctimes[pid] != create_time):
            raise process.ProcessGone(errno.ESRCH, f"process {pid} is not running")

    def running_name(self, pid, create_time):
        self.snapshots.append(pid)
        if pid not in self.classes or self.ctimes[pid] != create_time:
            return None
        return self.names.get(pid, "python.exe")

    def is_running(self, pid, create_time):
        return self.running_name(pid, create_time) is not None

    def service_pids(self):
        return {pid: list(names) for pid, names in self.services.items()}

    def is_service_or_protected(self, pid, create_time=None):
        if pid in self.services:
            return True
        self._open(pid, create_time)
        return pid in self.protected

    def same_user(self, pid, create_time=None):
        self._open(pid, create_time)
        if self.owner_check_denied:
            raise PermissionError(errno.EACCES, "Access is denied")
        return pid not in self.other_users

    def priority_class(self, pid, create_time=None):
        self._open(pid, create_time)
        return self.classes[pid]

    def set_priority_class(self, pid, cls, create_time=None):
        self._open(pid, create_time)
        self.sets.append((pid, cls))
        self.classes[pid] = cls


@pytest.fixture
def fake():
    return FakeProc()


@pytest.fixture
def fix(fake):
    return BackgroundPriorityFix(proc=fake)


@pytest.fixture
def journal(tmp_path):
    return Journal(tmp_path / "journal.json", lock_timeout=2)


def params(pid=HOG, **extra):
    """What the report suggests: the process, its name as the report showed it, and the game's PIDs."""
    return {"pid": pid, "create_time": CT, "class": "below_normal", "exclude": [GAME, GAME_CHILD],
            "name": NAMES.get(pid, "python.exe"), **extra}


def test_lowers_a_background_process_for_the_session_and_undo_restores_it(fix, fake, journal):
    plan = fix.plan(params())
    assert (plan.before, plan.after, plan.session_only, plan.needs_restart) == ("normal", "below_normal", True, None)
    res = fix.apply(plan, journal)
    assert res.ok and res.state == "applied", res.message
    assert fake.classes[HOG] == process.BELOW_NORMAL
    [entry] = journal.undoable()
    assert entry.session_only and entry.before == "normal"
    assert fix.undo(entry, journal).ok
    assert fake.classes[HOG] == process.NORMAL
    assert all(pid == HOG for pid in fake.opened)


@pytest.mark.parametrize("pid", [GAME, GAME_CHILD])
def test_the_game_and_its_children_are_refused_before_anything_is_opened(fix, fake, pid):
    with pytest.raises(Refused, match="game"):
        fix.plan(params(pid))
    assert fake.opened == [] and fake.sets == []


def test_lag_doctor_itself_is_refused(fix, fake):
    me = os.getpid()
    fake.classes[me], fake.ctimes[me] = process.NORMAL, CT
    with pytest.raises(Refused, match="lag-doctor itself"):
        fix.plan(params(me))
    assert fake.opened == []


def test_a_service_is_refused_without_being_opened(fix, fake):
    with pytest.raises(Refused, match="WSearch"):
        fix.plan(params(SERVICE))
    assert fake.opened == []


def test_a_protected_process_is_refused(fix, fake):
    with pytest.raises(Refused, match="protected"):
        fix.plan(params(PROTECTED))
    assert fake.sets == []


def test_another_users_process_is_refused(fix, fake):
    with pytest.raises(Refused, match="another user"):
        fix.plan(params(OTHER_USER))
    assert fake.sets == []


def test_an_owner_check_that_is_denied_is_a_refusal(fix, fake):
    fake.owner_check_denied = True
    with pytest.raises(Refused, match="cannot be checked"):
        fix.plan(params())


def test_a_process_that_exited_is_gone_and_nothing_is_written(fix, fake, journal):
    del fake.classes[HOG]
    plan = fix.plan(params())
    assert (plan.before, plan.after) == (GONE, "below_normal")
    assert "exited" in plan.summary
    res = fix.apply(plan, journal)
    assert not res.ok and res.state == "gone" and fake.sets == []


def test_a_reused_pid_is_gone(fix, fake):
    fake.ctimes[HOG] = CT + 1                      # same pid, another process (it could be the game)
    assert fix.plan(params()).before == GONE
    assert fake.opened == []                       # told apart by the snapshot, never by opening it
    assert HOG in fake.snapshots


def test_a_process_that_exits_after_the_snapshot_is_gone(fix, fake):
    # The snapshot still showed it; by the handle checks it has exited.
    fake.running_name = lambda pid, create_time: NAMES[pid]
    del fake.classes[HOG]
    assert fix.plan(params()).before == GONE
    assert fake.sets == []


def test_a_process_with_another_name_than_the_report_showed_is_refused(fix, fake):
    # The report blamed hog.exe, but (pid, create time) belong to OneDrive.exe: never change the wrong one.
    with pytest.raises(Refused, match="OneDrive.exe, not hog.exe"):
        fix.plan(params(name="hog.exe"))
    assert fake.opened == [] and fake.sets == []


@pytest.mark.parametrize("game_name", ["OneDrive.exe", "ONEDRIVE.EXE"])
def test_a_process_with_the_games_name_is_refused_before_anything_is_opened(fix, fake, game_name):
    # A same-named sibling of the game (Electron, CEF) is not its child, so "exclude" misses it.
    with pytest.raises(Refused, match="game"):
        fix.plan(params(game_name=game_name))
    assert fake.opened == [] and fake.snapshots == [] and fake.sets == []


def test_image_names_compare_without_case(fix, fake):
    plan = fix.plan(params(name="onedrive.EXE", game_name="Game.exe"))
    assert (plan.before, plan.after) == ("normal", "below_normal")


def test_ended_is_decided_by_the_snapshot_without_opening_anything(fix, fake):
    assert fix.ended(params()) is False
    fake.ctimes[HOG] = CT + 1                      # the PID now belongs to another process
    assert fix.ended(params()) is True
    del fake.classes[HOG]
    assert fix.ended(params()) is True
    # An entry an older lag-doctor journaled has no name or exclude list; it still settles.
    assert fix.ended({"pid": HOG, "create_time": CT, "class": "below_normal"}) is True
    assert fake.opened == []
    with pytest.raises(ValueError):
        fix.ended({"pid": HOG})


def test_a_process_that_exits_between_the_checks_and_the_read_is_gone(fix, fake):
    checked = fake.priority_class

    def exits_now(pid, create_time=None):
        del fake.classes[pid]
        return checked(pid, create_time)

    fake.priority_class = exits_now
    assert fix.plan(params()).before == GONE


def test_write_refuses_a_process_that_is_gone(fix, fake):
    del fake.classes[HOG]
    with pytest.raises(process.ProcessGone):
        fix.write(params(), "below_normal")
    assert fake.sets == []


def test_an_unknown_priority_class_is_reported_not_guessed(fix, fake):
    fake.classes[HOG] = 0x1234
    with pytest.raises(OSError, match="unknown priority class"):
        fix.plan(params())


def test_undo_after_the_process_exited_settles_the_entry_without_writing(fix, fake, journal):
    fix.apply(fix.plan(params()), journal)
    del fake.classes[HOG]
    writes = list(fake.sets)
    [entry] = journal.undoable()
    res = fix.undo(entry, journal)
    assert res.ok and fake.sets == writes
    settled = journal.get(entry.id)
    assert settled.state == "undone" and "no longer exists" in settled.note


def test_heal_settles_a_crash_left_entry_whose_process_is_gone(fix, fake, journal):
    entry = journal.begin("background_priority", "x", params(), before="normal", after="below_normal",
                          session_only=True)
    del fake.classes[HOG]
    assert fix.heal(entry, journal).ok
    assert journal.get(entry.id).state == "undone" and fake.sets == []


@pytest.mark.parametrize("current, target, wanted", [
    (process.NORMAL, "below_normal", "below_normal"),
    (process.HIGH, "below_normal", "below_normal"),
    (process.ABOVE_NORMAL, "idle", "idle"),
    (process.BELOW_NORMAL, "idle", "idle"),
    (process.BELOW_NORMAL, "below_normal", "below_normal"),     # already there: noop
    (process.IDLE, "below_normal", "idle"),                     # lower already: never raised
])
def test_the_class_only_ever_goes_down(fix, fake, current, target, wanted):
    fake.classes[HOG] = current
    assert fix.plan(params(**{"class": target})).after == wanted


@pytest.mark.parametrize("bad", [
    {"class": "high"}, {"class": "normal"}, {"class": "realtime"}, {"class": "above_normal"}, {"class": None},
    {"pid": True}, {"pid": 0}, {"pid": -4}, {"pid": 2**32}, {"pid": "2000"},
    {"create_time": None}, {"create_time": 0}, {"create_time": True}, {"create_time": 1.5},
    {"exclude": GAME}, {"exclude": [True]}, {"exclude": [0]}, {"exclude": list(range(4, 4 * 5000, 4))},
    {"exclude": []}, {"exclude": None},
    {"exlude": [GAME]}, {"name": "x" * 300}, {"name": "bad\nname"}, {"name": 5}, {"name": ""}, {"name": None},
    {"game_name": ""}, {"game_name": 5}, {"game_name": "x" * 300}, {"game_name": "bad\tname"},
])
def test_bad_params_are_refused_before_anything_is_opened(fix, fake, bad):
    with pytest.raises(ValueError):
        fix.plan(params(**bad))
    assert fake.opened == [] and fake.snapshots == []


def test_missing_params_are_refused(fix, fake):
    # Without "exclude" the game could not be told apart; without "name" the process could not be checked.
    for key in ("pid", "create_time", "class", "exclude", "name"):
        incomplete = params()
        del incomplete[key]
        with pytest.raises(ValueError):
            fix.plan(incomplete)
    assert fake.opened == [] and fake.snapshots == []


def test_game_name_is_optional(fix, fake):
    assert "game_name" not in params()
    assert fix.plan(params()).after == "below_normal"
    assert fix.plan(params(game_name=None)).after == "below_normal"


def test_a_forced_undo_never_reaches_an_excluded_process(fix, fake, journal):
    # A journal entry that names the game (hand-edited, or from a buggy caller) must not reach it.
    entry = journal.begin("background_priority", "x", params(GAME), before="normal", after="below_normal",
                          session_only=True)
    journal.set_state(entry.id, "applied")
    res = fix.undo(journal.get(entry.id), journal, force=True)
    assert not res.ok
    assert fake.opened == [] and fake.sets == []


def test_write_refuses_anything_but_a_class_name(fix, fake):
    for value in (GONE, {"__absent__": True}, 0x4000, "low", None):
        with pytest.raises(ValueError):
            fix.write(params(), value)
    assert fake.sets == []


def test_the_plan_reads_plainly(fix):
    plan = fix.plan(params(name="OneDrive.exe"))
    assert plan.target == f"priority class of OneDrive.exe (process {HOG})"
    assert "normal -> below normal" in plan.summary
    assert any("until that program exits" in note for note in plan.notes)


def test_the_catalog_builds_it_with_an_injected_backend(fake):
    fix = catalog.get("background_priority", proc=fake)
    assert isinstance(fix, BackgroundPriorityFix) and fix.proc is fake


def test_the_default_backend_is_the_real_process_module():
    assert BackgroundPriorityFix().proc is process
