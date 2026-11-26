"""Apply and Undo in lag-doctor's window (App.do_plan_fix, do_apply_fix, do_changes, do_undo_change): the plan shown
first, exactly that change made, the list of changes in Settings and each undo, with every refusal. The fixes are
stand-ins (tests/fake_fixes.py) whose values live in a dict, and the journal is the test home's own: no test changes
this PC's settings, and none reaches the window's real fixes (tests/conftest.py's _no_real_fixes)."""
from __future__ import annotations

import json
import msvcrt
import os
import re
import uuid
from datetime import datetime
from pathlib import Path

import pytest

from lagdoctor import paths
from lagdoctor.app import main as app_main
from lagdoctor.app import recording, views
from lagdoctor.analysis.diagnose import diagnose
from lagdoctor.capture.session import session_dir
from lagdoctor.fixes import flow
from lagdoctor.fixes.base import GONE, NOT_CONNECTED, FixResult
from lagdoctor.fixes.journal import Journal
from lagdoctor.fixes.priority import Refused
from tests import synth
from tests.fake_fixes import AFTER, BEFORE, StandInFixes
from tests.test_app_added import Window, ask, make_app


@pytest.fixture(autouse=True)
def _no_real_uac(monkeypatch):
    from lagdoctor.winapi import elevation
    monkeypatch.setattr(elevation, "launch_elevated", lambda *a, **k: pytest.fail("a test reached the UAC prompt"))


ADVICE = {"title": "Close the browser", "detail": "", "fix_id": None, "params": {}}


def offer(fix_id: str = "power_plan", params=None, title: str = "Use the Balanced power plan") -> dict:
    return {"title": title, "detail": "", "fix_id": fix_id, "params": {} if params is None else params,
            "needs_admin": False, "needs_restart": None}


def recorded(home: Path, fixes: list[list[dict]], *, last: bool = True, when=datetime(2026, 11, 4, 19, 45)) -> str:
    """A recording as `watch` leaves one, under the home's sessions, whose diagnosis holds a finding for each list of
    `fixes`; with `last`, the one `lagdoctor fix` and Apply act on (last-session.txt)."""
    cap = synth.laglab("smooth", duration_s=4.0).cap
    cap.target = dict(cap.target, name="Moss.exe", exe="C:\\Games\\Moss\\Moss.exe", window_title="Moss Garden")
    cap.started_at = when.astimezone().isoformat(timespec="seconds")
    folder = session_dir(home / "sessions", cap.target, when=when)
    cap.save(folder / "capture.ldcap.json.gz")
    d = diagnose(cap).to_json()
    d["findings"] = [{"cause": f"cause_{i}", "title": f"Finding {i}", "category": "setup", "confidence": "high",
                      "score": 0.5, "summary": "A summary.", "evidence": [], "fixes": list(fx), "event_ids": []}
                     for i, fx in enumerate(fixes)]
    (folder / "diagnosis.json").write_text(json.dumps(d), encoding="utf-8")
    if last:
        (home / "last-session.txt").write_text(str(folder.resolve()), encoding="utf-8")
    return folder.name


def fixing(tmp_path, fixes=((offer(),),), *, log=None, **kw):
    """An App whose fixes are stand-ins, with one recording, the last, offering `fixes`; (app, its stand-ins, sid)."""
    app = make_app(tmp_path, log=log)
    stand_ins = StandInFixes(app.home, **kw)
    app.fixes = stand_ins
    sid = recorded(app.home, [list(f) for f in fixes])
    return app, stand_ins, sid


def journal_of(app) -> Journal:
    return Journal(app.home / paths.JOURNAL_NAME)


def planned(app, sid, finding=0, fix=0):
    p = ask(app, "plan_fix", session=sid, finding=finding, fix=fix)
    assert isinstance(p, dict), p
    return p


PLAIN = re.compile(r"[{}]|'__|__\w+__|--force|\blagdoctor (fix|undo)\b|Traceback")


def plain(text: str) -> str:
    """Text the page shows: no internal value (a dict, a sentinel), no terminal command, no traceback."""
    assert isinstance(text, str) and text and not PLAIN.search(text), text
    return text


# ---------------------------------------------------------------- Results ---

SCREEN = {"device": "\\\\.\\DISPLAY1", "hz": 144}
MONITOR = "\\\\?\\DISPLAY#ABC1234#5&1a2b3c4d&0&UID4352#{e6f07b5f-ee97-4a90-b076-33f57bf4eaa7}"


def test_results_offer_apply_on_every_recording_for_the_fixes_the_window_applies(tmp_path):
    app, _, sid = fixing(tmp_path, fixes=[[offer(), ADVICE, offer("game_setting", {"key": "x"})],
                                          [offer("from_a_newer_lag_doctor")], [offer(params=["not", "an", "object"])]])
    old = recorded(app.home, [[offer(), offer("refresh_rate", dict(SCREEN, monitor=MONITOR))]], last=False,
                   when=datetime(2026, 11, 1, 20, 0))
    r = ask(app, "results", session=sid)
    assert [[fx["apply"] for fx in f["fixes"]] for f in r["findings"]] == [[True, False, False], [False], [False]]
    assert r["fix_note"] == ""
    o = ask(app, "results", session=old)
    assert [fx["apply"] for f in o["findings"] for fx in f["fixes"]] == [True, True] and o["fix_note"] == ""


DATED = [[offer("refresh_rate", dict(SCREEN)), offer("background_priority"), offer()]]


def test_an_offer_that_names_the_pc_as_it_was_then_is_applied_from_the_last_recording_only(tmp_path):
    """A display by the name Windows gave it (an older report's refresh rate, which names no monitor), or a program
    that was running, which may be the game by now: as M7 applied every offer."""
    app, fx, sid = fixing(tmp_path, fixes=DATED)
    old = recorded(app.home, DATED, last=False, when=datetime(2026, 11, 1, 20, 0))
    r = ask(app, "results", session=sid)
    assert [fx["apply"] for fx in r["findings"][0]["fixes"]] == [True, True, True] and r["fix_note"] == ""
    o = ask(app, "results", session=old)
    assert [fx["apply"] for fx in o["findings"][0]["fixes"]] == [False, False, True]
    assert plain(o["fix_note"]) == views.LAST_ONLY_FIX
    assert ask(app, "plan_fix", session=old, finding=0, fix=0) == views.NOT_LAST
    assert ask(app, "plan_fix", session=old, finding=0, fix=1) == views.NOT_LAST
    assert fx.asked == [] and app.planned is None
    assert planned(app, old, 0, 2)["plan"]                                       # one that stands alone: planned
    token = planned(app, sid, 0, 1)["plan"]                                      # the last one's: planned
    recorded(app.home, [[offer()]], when=datetime(2026, 11, 5, 21, 0))          # and then a newer recording
    assert ask(app, "apply_fix", plan=token) == views.NOT_LAST
    assert "writes" not in fx.store and journal_of(app).entries() == []


def test_a_window_without_fixes_offers_no_apply_and_says_so(tmp_path):
    app, _, sid = fixing(tmp_path)
    app.fixes = None
    assert [fx["apply"] for f in ask(app, "results", session=sid)["findings"] for fx in f["fixes"]] == [False]
    words = "Applying and undoing fixes is not available in this window."
    assert ask(app, "plan_fix", session=sid, finding=0, fix=0) == words
    assert ask(app, "apply_fix", plan="p-0123456789abcdef") == words
    assert ask(app, "undo_change", entry="0123456789ab", force=False) == words
    c = ask(app, "changes")
    assert c["rows"] == [] and c["paused"] and c["text"] == "Undoing changes is not available in this window."


# ---------------------------------------------------------------- the plan, then the change ---

def test_apply_shows_the_plan_first_then_makes_exactly_that_change_and_undo_puts_it_back(tmp_path):
    log = []
    app, fx, sid = fixing(tmp_path, log=log, restart={"power_plan": "game"})
    p = planned(app, sid)
    assert p["plan"] and re.fullmatch(r"p-[0-9a-f]{16}", p["plan"])
    assert p["where"] == "the stand-in setting of power_plan" and p["change"] == "The stand-in for power_plan: off -> on"
    assert p["notes"] == ["A stand-in's note, shown before anything changes."] and p["text"] == ""
    assert p["restart"] == "It takes effect the next time the game starts."
    assert "writes" not in fx.store and journal_of(app).entries() == []           # showing it changed nothing
    a = ask(app, "apply_fix", plan=p["plan"])
    assert plain(a["text"]) == ("Done: The stand-in for power_plan: off -> on. It takes effect the next time the game "
                                "starts.")
    assert a["done"] and fx.store["writes"] == [("power_plan", AFTER)]
    (e,) = journal_of(app).entries()
    assert (e.id, e.state, e.before, e.after) == (a["entry"], "applied", BEFORE, AFTER)
    assert any(line.startswith(f"apply power_plan (offered by {sid}): applied") for line in log)
    rows = ask(app, "changes")["rows"]
    assert [(r["id"], r["title"], r["change"], r["where"], r["undo"], r["force"]) for r in rows] == [
        (e.id, "The stand-in for power_plan", "The stand-in for power_plan: off -> on",
         "the stand-in setting of power_plan", True, False)]
    u = ask(app, "undo_change", entry=e.id, force=False)
    assert plain(u["text"]) == "Undone: The stand-in for power_plan: off -> on. It is back to how it was."
    assert u["done"] and not u["force"] and fx.store["power_plan"] == BEFORE
    assert journal_of(app).get(e.id).state == "undone" and ask(app, "changes")["rows"] == []


def test_the_page_names_a_fix_only_by_its_place_on_a_recordings_results(tmp_path):
    app, fx, sid = fixing(tmp_path, fixes=[[ADVICE, offer()]])
    gone = "That is not a fix lag-doctor can apply from this recording: open its results again."
    assert ask(app, "plan_fix", session=sid, finding=0, fix=0) == gone           # advice is no fix to apply
    assert ask(app, "plan_fix", session=sid, finding=0, fix=2) == gone           # past the finding's fixes
    assert ask(app, "plan_fix", session=sid, finding=1, fix=0) == gone           # past the findings
    assert ask(app, "plan_fix", session="20261104-194500-nobody", finding=0, fix=1) == \
        "That recording is no longer there."
    assert planned(app, sid, 0, 1)["plan"] and fx.asked == ["power_plan"]


def test_each_plan_applies_once_and_only_the_newest_one(tmp_path):
    app, fx, sid = fixing(tmp_path, fixes=[[offer(), offer("game_mode_on")]])
    first = planned(app, sid, 0, 0)["plan"]
    second = planned(app, sid, 0, 1)["plan"]
    assert ask(app, "apply_fix", plan=first) == app_main.NO_LONGER_PLANNED          # replaced by the newer one
    assert ask(app, "apply_fix", plan="p-0123456789abcdef") == app_main.NO_LONGER_PLANNED
    assert ask(app, "apply_fix", plan=second)["done"]
    assert ask(app, "apply_fix", plan=second) == app_main.NO_LONGER_PLANNED         # once
    assert fx.store["writes"] == [("game_mode_on", AFTER)]


def test_a_plan_another_request_applied_meanwhile_is_not_applied_again(tmp_path):
    app, fx, sid = fixing(tmp_path)
    token = planned(app, sid)["plan"]

    class Meanwhile:
        """The fix lock, got by this request just after another one with the same token applied it (two workers)."""

        def __init__(self, real):
            self.real = real

        def acquire(self, *args, **kwargs):
            got = self.real.acquire(*args, **kwargs)
            with app.lock:
                app.planned = None                 # what the other request's apply left
            return got

        def release(self):
            self.real.release()

        def locked(self):
            return self.real.locked()
    app.fix_lock = Meanwhile(app.fix_lock)
    assert ask(app, "apply_fix", plan=token) == app_main.NO_LONGER_PLANNED
    assert "writes" not in fx.store and journal_of(app).entries() == [] and not app.fix_lock.locked()


def test_an_older_recordings_fix_is_applied_as_the_last_ones_is(tmp_path):
    app, fx, sid = fixing(tmp_path)
    token = planned(app, sid)["plan"]
    recorded(app.home, [[offer("game_mode_on")]], when=datetime(2026, 11, 5, 21, 0))      # the last one now
    a = ask(app, "apply_fix", plan=token)                                             # shown before it: still applied
    assert a["done"] and fx.store["writes"] == [("power_plan", AFTER)]
    assert ask(app, "undo_change", entry=a["entry"], force=False)["done"]
    again = ask(app, "apply_fix", plan=planned(app, sid)["plan"])                        # and planned after it
    assert again["done"] and fx.store["writes"][-1] == ("power_plan", AFTER)


# ---------------------------------------------------------------- when nothing is changed ---

def test_nothing_is_changed_while_this_windows_recording_runs(tmp_path):
    app, fx, sid = fixing(tmp_path)
    token = planned(app, sid)["plan"]
    app.rec_state = recording.RecordingState()
    words = "A recording is running: apply or undo fixes once it has ended."
    assert ask(app, "plan_fix", session=sid, finding=0, fix=0) == words
    assert ask(app, "apply_fix", plan=token) == words
    assert ask(app, "undo_change", entry="0123456789ab", force=False) == words
    assert ask(app, "undo_all", entries=["0123456789ab"]) == words
    assert "writes" not in fx.store


def test_nothing_is_changed_while_another_process_records(tmp_path):
    from lagdoctor.winapi import elevation
    app, fx, sid = fixing(tmp_path)
    token = planned(app, sid)["plan"]
    app.capture_lock = f"Local\\lag-doctor-test-capture-{uuid.uuid4().hex}"
    held = elevation.hold_named_mutex(app.capture_lock)
    try:
        words = ("Another lag-doctor recording is running, in a terminal or from an earlier window: apply or undo "
                 "fixes once it has ended.")
        assert ask(app, "apply_fix", plan=token) == words
        assert ask(app, "plan_fix", session=sid, finding=0, fix=0) == words
        assert ask(app, "undo_change", entry="0123456789ab", force=False) == words
        assert ask(app, "undo_all", entries=["0123456789ab"]) == words
    finally:
        elevation.release_named_mutex(held)
    assert "writes" not in fx.store
    assert ask(app, "apply_fix", plan=token)["done"]                         # once it has ended


def test_nothing_is_changed_when_whether_a_recording_runs_cannot_be_told(tmp_path, monkeypatch):
    from lagdoctor.capture import session as sess
    log = []
    app, fx, sid = fixing(tmp_path, log=log)
    app.capture_lock = f"Local\\lag-doctor-test-capture-{uuid.uuid4().hex}"

    def fails(self):
        raise OSError("the lock could not be looked at")
    monkeypatch.setattr(sess.CaptureLock, "acquire", fails)
    assert ask(app, "plan_fix", session=sid, finding=0, fix=0) == \
        "lag-doctor could not tell whether a recording is running, so it changed nothing."
    assert any(line.startswith("looking for a recording before changing a setting:") for line in log)


@pytest.mark.parametrize("elevated, words", [
    (True, "lag-doctor is running as administrator, so it changed nothing: fixes change your own user's settings. "
           "Start lag-doctor normally to apply or undo them."),
    (None, "lag-doctor could not tell whether it runs as administrator, so it changed nothing."),
])
def test_nothing_is_changed_as_administrator_or_when_that_cannot_be_told(tmp_path, elevated, words):
    app, fx, sid = fixing(tmp_path)
    token = planned(app, sid)["plan"]
    fx.is_elevated = elevated
    assert ask(app, "apply_fix", plan=token) == words
    assert ask(app, "plan_fix", session=sid, finding=0, fix=0) == words
    assert ask(app, "undo_change", entry="0123456789ab", force=False) == words
    assert ask(app, "undo_all", entries=["0123456789ab"]) == words
    assert "writes" not in fx.store and journal_of(app).entries() == []


def test_one_change_at_a_time(tmp_path, monkeypatch):
    from tests import fake_recorder
    app, fx, sid = fixing(tmp_path)
    # A recorder that records nothing: were the refusal below to slip, the real one would raise Windows' UAC prompt.
    app.recorder = lambda: [str(fake_recorder.BASE_PY), "-I", "-c", "pass"]
    token = planned(app, sid)["plan"]
    assert app.fix_lock.acquire(blocking=False)
    try:
        assert ask(app, "apply_fix", plan=token) == app_main.BUSY
        assert ask(app, "plan_fix", session=sid, finding=0, fix=0) == app_main.BUSY
        assert ask(app, "undo_change", entry="0123456789ab", force=False) == app_main.BUSY
        assert ask(app, "undo_all", entries=["0123456789ab"]) == app_main.BUSY
        monkeypatch.setattr(app_main, "LIST_WAIT_S", 0.05)
        assert ask(app, "changes") == "lag-doctor is still changing a setting: open Settings again in a moment."
        assert ask(app, "record") == "lag-doctor is changing a setting: record once it is done."
    finally:
        app.fix_lock.release()
    assert "writes" not in fx.store and app.rec is None
    assert ask(app, "apply_fix", plan=token)["done"]                          # the token waited


def test_a_setting_that_moved_since_the_plan_is_left_alone(tmp_path):
    app, fx, sid = fixing(tmp_path)
    token = planned(app, sid)["plan"]
    fx.store["power_plan"] = "set by hand meanwhile"
    a = ask(app, "apply_fix", plan=token)
    assert plain(a["text"]).startswith("It changed since lag-doctor looked at it, so nothing was changed.")
    assert not a["done"] and a["entry"] is None and fx.store["power_plan"] == "set by hand meanwhile"
    assert journal_of(app).entries() == []


def test_a_fix_set_already_or_whose_subject_is_gone_offers_nothing_to_apply(tmp_path):
    app, fx, sid = fixing(tmp_path, fixes=[[offer(), offer("background_priority")]],
                          session_only=frozenset({"background_priority"}))
    fx.store["power_plan"] = AFTER
    p = planned(app, sid, 0, 0)
    assert p["plan"] is None and p["text"] == "This is set that way already, so there is nothing to change."
    fx.store["background_priority"] = GONE
    p = planned(app, sid, 0, 1)
    assert p["plan"] is None and p["text"].startswith("The stand-in for background_priority: ")
    assert app.planned is None and "writes" not in fx.store


def test_a_plan_that_refuses_is_said_in_words(tmp_path):
    app, fx, sid = fixing(tmp_path)
    fx.store[("refuse", "power_plan")] = Refused("that process is a Windows service")
    assert ask(app, "plan_fix", session=sid, finding=0, fix=0) == \
        "lag-doctor will not make this change: that process is a Windows service."
    fx.store[("refuse", "power_plan")] = PermissionError(5, "Access is denied")
    assert ask(app, "plan_fix", session=sid, finding=0, fix=0) == \
        "lag-doctor could not read the setting this fix would change (Access is denied)."
    assert app.planned is None


def test_an_unreadable_journal_pauses_apply_undo_and_the_list(tmp_path):
    app, fx, sid = fixing(tmp_path)
    token = planned(app, sid)["plan"]
    journal_of(app).path.write_text("not a journal", encoding="utf-8")
    for said in (ask(app, "plan_fix", session=sid, finding=0, fix=0), ask(app, "apply_fix", plan=token),
                 ask(app, "undo_change", entry="0123456789ab", force=False), ask(app, "undo_all", entries=["0123456789ab"])):
        assert said.startswith("lag-doctor cannot read its fix journal") and "nothing was changed" in said, said
    c = ask(app, "changes")
    assert c["paused"] and c["rows"] == [] and c["text"].startswith("lag-doctor cannot read its fix journal")
    assert "writes" not in fx.store and journal_of(app).path.read_text(encoding="utf-8") == "not a journal"


def test_a_journal_another_lag_doctor_holds_changes_nothing(tmp_path):
    app, fx, sid = fixing(tmp_path)
    token = planned(app, sid)["plan"]
    j = journal_of(app)
    j.path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(j.path.with_name(j.path.name + ".lock"), os.O_RDWR | os.O_CREAT)
    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)                 # as another lag-doctor's Journal holds it
    try:
        a = ask(app, "apply_fix", plan=token)
        assert plain(a["text"]).startswith("lag-doctor could not open its fix journal") and not a["done"]
        assert ask(app, "plan_fix", session=sid, finding=0, fix=0) == app_main.NO_JOURNAL
        assert ask(app, "undo_change", entry="0123456789ab", force=False) == app_main.NO_JOURNAL
        assert ask(app, "undo_all", entries=["0123456789ab"]) == app_main.NO_JOURNAL
        assert "could not open its fix journal" in ask(app, "changes")["text"]
    finally:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        os.close(fd)
    assert "writes" not in fx.store


# ---------------------------------------------------------------- undo and the list ---

def applied_once(app, sid) -> str:
    return ask(app, "apply_fix", plan=planned(app, sid)["plan"])["entry"]


def test_undo_leaves_a_value_changed_since_alone_until_it_is_put_back_anyway(tmp_path):
    app, fx, sid = fixing(tmp_path)
    entry = applied_once(app, sid)
    fx.store["power_plan"] = "set by hand since"
    u = ask(app, "undo_change", entry=entry, force=False)
    assert plain(u["text"]).startswith("It was changed after lag-doctor set it, so it was left alone.")
    assert u["force"] and not u["done"] and fx.store["power_plan"] == "set by hand since"
    u = ask(app, "undo_change", entry=entry, force=True)
    assert u["done"] and fx.store["power_plan"] == BEFORE and journal_of(app).get(entry).state == "undone"


def test_a_recording_is_looked_for_with_the_change_lock_held(tmp_path):
    """So that a recording that starts meanwhile waits the change out (App._start), and never starts in its middle."""
    app, fx, sid = fixing(tmp_path)
    held, real = [], app.change_refusal
    app.change_refusal = lambda fixes: held.append(app.fix_lock.locked()) or real(fixes)
    entry = ask(app, "apply_fix", plan=planned(app, sid)["plan"])["entry"]
    assert ask(app, "undo_change", entry=entry, force=False)["done"]
    entry = ask(app, "apply_fix", plan=planned(app, sid)["plan"])["entry"]
    assert ask(app, "undo_all", entries=[entry])["done"] == 1
    assert held == [True] * 6                             # each plan, apply and undo, Undo all's included


def test_a_change_that_left_neither_value_offers_only_to_put_it_back_anyway(tmp_path):
    app, fx, sid = fixing(tmp_path)
    token = planned(app, sid)["plan"]

    class Stuck(type(fx.fix("power_plan"))):
        """Its write lands a third value, and putting the original back fails too."""

        def write(self, params, value):
            if value == BEFORE:
                raise OSError("the setting is locked")
            self.store[self.id] = "a third value"
    fx.fix = lambda fid: Stuck(fid, fx.store)
    a = ask(app, "apply_fix", plan=token)
    assert plain(a["text"]).startswith("The change did not take, and putting back how it was did not work either.")
    assert a["entry"] and a["force"] and journal_of(app).get(a["entry"]).state == "conflict"
    assert not views.applied_view(FixResult(True, "", "0123456789ab", "applied"), planned_plan())["force"]


def planned_plan():
    return type("Plan", (), {"summary": "Power plan: Silent -> Balanced", "needs_restart": None})()


def test_an_undo_names_only_a_change_still_on_the_list_and_never_a_sweeps(tmp_path):
    app, fx, sid = fixing(tmp_path)
    entry = applied_once(app, sid)
    assert ask(app, "undo_change", entry=entry, force=False)["done"]
    assert ask(app, "undo_change", entry=entry, force=False) == \
        "That change is not on the list any more: it was undone already."
    assert ask(app, "undo_change", entry="0123456789ab", force=False) == \
        "That change is not on the list any more: it was undone already."
    j = journal_of(app)
    sweep = j.begin("game_setting", "sg.X in GameUserSettings.ini", {"key": "sg.X"}, "3", "1")
    j.set_state(sweep.id, "applied")
    assert ask(app, "undo_change", entry=sweep.id, force=True) == views.SWEEP_CHANGE
    assert j.get(sweep.id).state == "applied"


def test_the_list_says_each_change_and_offers_undo_only_where_it_can(tmp_path):
    app, fx, sid = fixing(tmp_path, session_only=frozenset({"background_priority"}))
    j = journal_of(app)

    def made(fix_id, target, state="applied", note="", session_only=False):
        e = j.begin(fix_id, target, {}, BEFORE, AFTER, session_only=session_only)
        j.set_state(e.id, state, note)
        return e.id
    lasting = made("power_plan", '<img src=x onerror="window.__pwned=1"> the active power plan')
    unplugged = made("refresh_rate", "the refresh rate of DISPLAY2", note=NOT_CONNECTED)
    fx.store["background_priority"] = AFTER                    # its program still runs
    session = made("background_priority", "priority of Discord.exe", session_only=True)
    conflict = made("game_mode_on", "Game Mode", state="conflict")
    sweep = made("game_setting", "sg.X in GameUserSettings.ini")
    newer = made("from_a_newer_lag_doctor", "something newer")
    raw = json.loads(j.path.read_text(encoding="utf-8"))      # an id the page could not name back: a hand-edited file
    # Times Windows cannot show in this time zone (before 1970, past the year 9999): said as unknown, never an error.
    for i, when in enumerate(("1960-01-01T00:00:00+00:00", "9999-12-31T23:59:59-12:00", "not a time", None)):
        raw["entries"].insert(0, dict(raw["entries"][0], id=f"00000000000{i}", target=f"odd time {i}", created_at=when))
    raw["entries"].append(dict(raw["entries"][-1], id="../../x", target="hand-edited"))
    j.path.write_text(json.dumps(raw), encoding="utf-8")
    rows = ask(app, "changes")["rows"]
    by = {r["where"]: r for r in rows}
    assert [r["where"] for r in rows][0] == "hand-edited" and len(rows) == 11              # newest first
    assert [by[f"odd time {i}"]["when"] for i in range(4)] == ["-"] * 4
    rows = [r for r in rows if not r["where"].startswith("odd time")]
    assert by['<img src=x onerror="window.__pwned=1"> the active power plan']["undo"]      # outside text, as text
    assert (by["Game Mode"]["undo"], by["Game Mode"]["force"]) == (False, True)
    assert by["Game Mode"]["note"] == views.CONFLICT_NOTE
    # Left alone at an undo while unplugged: listed with its Undo, and no note that a later undo would make untrue.
    assert (by["the refresh rate of DISPLAY2"]["undo"], by["the refresh rate of DISPLAY2"]["note"]) == (True, "")
    assert by["priority of Discord.exe"]["note"] == "It lasts until that program exits."
    assert (by["sg.X in GameUserSettings.ini"]["undo"], by["sg.X in GameUserSettings.ini"]["note"]) == \
        (False, views.SWEEP_CHANGE)
    assert (by["something newer"]["undo"], by["something newer"]["title"], by["something newer"]["note"]) == \
        (False, "from_a_newer_lag_doctor", views.NEWER_CHANGE)
    assert (by["hand-edited"]["id"], by["hand-edited"]["undo"], by["hand-edited"]["force"]) == (None, False, False)
    assert {r["id"] for r in rows if r["where"] != "hand-edited"} == {lasting, unplugged, session, conflict, sweep, newer}
    assert all(re.fullmatch(r"\d{1,2} \w{3} \d{4}, \d\d:\d\d", r["when"]) for r in rows)


THREE = [[offer("power_plan")], [offer("game_mode_on")], [offer("game_dvr_off")]]


def made_changes(app, sid, count):
    """A change applied through Apply for each of the recording's first `count` findings, the first one first; their
    journal ids, newest first."""
    out = []
    for k in range(count):
        out.insert(0, ask(app, "apply_fix", plan=planned(app, sid, k, 0)["plan"])["entry"])
    return out


def test_undo_all_puts_back_each_change_the_list_named_newest_first(tmp_path):
    log = []
    app, fx, sid = fixing(tmp_path, fixes=THREE, log=log)
    ids = made_changes(app, sid, 3)
    c = ask(app, "changes")
    assert c["all"] == ids == [r["id"] for r in c["rows"]]
    u = ask(app, "undo_all", entries=c["all"])
    assert u["done"] == 3 and u["force"] == []
    assert [plain(line) for line in u["lines"]] == [
        f"Undone: The stand-in for {fid}: off -> on. It is back to how it was."
        for fid in ("game_dvr_off", "game_mode_on", "power_plan")]
    assert fx.store["writes"][-3:] == [("game_dvr_off", BEFORE), ("game_mode_on", BEFORE), ("power_plan", BEFORE)]
    assert ask(app, "changes")["rows"] == [] and all(journal_of(app).get(i).state == "undone" for i in ids)
    assert [line for line in log if line.startswith("undo ")] == [
        f"undo {fid} ({i}): restored: restored the stand-in setting of {fid} to {BEFORE!r}"
        for fid, i in zip(("game_dvr_off", "game_mode_on", "power_plan"), ids)]


def test_undo_all_puts_back_two_changes_of_one_setting_in_turn(tmp_path):
    """Newest first: the older change's own value is back before it is undone, so neither reads as changed since."""
    app, fx, _ = fixing(tmp_path)
    j = journal_of(app)
    older = j.begin("power_plan", "the stand-in setting of power_plan", {}, BEFORE, "between")
    j.set_state(older.id, "applied")
    newer = j.begin("power_plan", "the stand-in setting of power_plan", {}, "between", AFTER)
    j.set_state(newer.id, "applied")
    fx.store["power_plan"] = AFTER
    c = ask(app, "changes")
    assert c["all"] == [newer.id, older.id]
    u = ask(app, "undo_all", entries=list(reversed(c["all"])))          # whatever order the page names them in
    assert u["done"] == 2 and fx.store["writes"] == [("power_plan", "between"), ("power_plan", BEFORE)]
    assert fx.store["power_plan"] == BEFORE and ask(app, "changes")["rows"] == []


def test_undo_all_leaves_what_the_list_gave_no_undo_and_what_it_did_not_name(tmp_path):
    app, fx, _ = fixing(tmp_path)
    j = journal_of(app)

    def made(fix_id, state="applied"):
        e = j.begin(fix_id, f"the stand-in setting of {fix_id}", {"key": "sg.X"}, BEFORE, AFTER)
        j.set_state(e.id, state)
        fx.store[fix_id] = AFTER
        return e.id
    sweep, newer, conflict = made("game_setting"), made("from_a_newer_lag_doctor"), made("game_mode_on", "conflict")
    left_out, named = made("game_dvr_off"), made("power_plan")
    c = ask(app, "changes")
    assert c["all"] == [named, left_out]                      # only the rows with an Undo
    u = ask(app, "undo_all", entries=[named, sweep, newer, conflict])     # a page that names more than it was given
    assert u["done"] == 1 and len(u["lines"]) == 1 and fx.store["writes"] == [("power_plan", BEFORE)]
    assert {i: j.get(i).state for i in (sweep, newer, conflict, left_out)} == {
        sweep: "applied", newer: "applied", conflict: "conflict", left_out: "applied"}
    assert ask(app, "undo_all", entries=[named]) == {
        "lines": ["Those changes are not on the list any more: they were undone already."], "done": 0, "force": []}


def test_undo_all_leaves_a_change_made_since_alone_and_its_row_may_put_it_back_anyway(tmp_path):
    app, fx, sid = fixing(tmp_path, fixes=THREE)
    ids = made_changes(app, sid, 2)
    fx.store["power_plan"] = "set by hand since"
    u = ask(app, "undo_all", entries=ids)
    assert (u["done"], u["force"]) == (1, [ids[1]])
    assert plain(u["lines"][1]) == ("The stand-in for power_plan: It was changed after lag-doctor set it, so it was "
                                    "left alone. Put it back anyway restores how it was before lag-doctor's change.")
    assert fx.store["power_plan"] == "set by hand since" and fx.store["game_mode_on"] == BEFORE
    assert ask(app, "undo_change", entry=ids[1], force=True)["done"] and fx.store["power_plan"] == BEFORE


def test_undo_all_stops_where_the_journal_turns_unreadable_and_says_what_it_did(tmp_path, monkeypatch):
    from lagdoctor.fixes import flow
    app, fx, sid = fixing(tmp_path, fixes=THREE)
    ids = made_changes(app, sid, 3)
    real, calls = flow.undo, []

    def then_unreadable(entry, fix_for, journal, force=False):
        calls.append(entry.id)
        res = real(entry, fix_for, journal, force=force)
        journal.path.write_text("not a journal", encoding="utf-8")       # something else broke the file meanwhile
        return res
    monkeypatch.setattr(flow, "undo", then_unreadable)
    u = ask(app, "undo_all", entries=ids)
    assert calls == ids[:2] and u["done"] == 1                # the second undo could not write itself down
    assert plain(u["lines"][0]) == "Undone: The stand-in for game_dvr_off: off -> on. It is back to how it was."
    assert u["lines"][1:] == [views.UNDO_ALL_STOPPED.format(title="The stand-in for game_mode_on")]


def test_undo_all_stops_at_an_undo_it_could_not_write_down(tmp_path, monkeypatch):
    from lagdoctor.fixes import flow
    app, fx, sid = fixing(tmp_path, fixes=THREE)
    ids = made_changes(app, sid, 3)
    real, calls = flow.undo, []

    def unsure_second(entry, fix_for, journal, force=False):
        calls.append(entry.id)
        if len(calls) == 2:                 # another lag-doctor held the journal: whether it went back is not known
            return FixResult(False, "the fix journal failed", entry.id, entry.state, "unsure")
        return real(entry, fix_for, journal, force=force)
    monkeypatch.setattr(flow, "undo", unsure_second)
    u = ask(app, "undo_all", entries=ids)
    assert calls == ids[:2] and u["done"] == 1
    assert u["lines"][1:] == ["The stand-in for game_mode_on: " + views.UNDONE["unsure"], views.UNDO_ALL_UNSURE]
    assert fx.store["power_plan"] == AFTER                    # the one after it, left as it is


def test_undo_all_copes_with_a_journal_whose_ids_are_odd(tmp_path):
    """Any program of the player's can write the journal: an id that is no text, and one written twice."""
    app, fx, sid = fixing(tmp_path, fixes=THREE)
    ids = made_changes(app, sid, 2)
    j = journal_of(app)
    raw = json.loads(j.path.read_text(encoding="utf-8"))
    raw["entries"].append(dict(raw["entries"][0], id=["not", "text"], target="no text"))
    raw["entries"].append(dict(raw["entries"][1], target="written twice"))       # the newest change's id again
    raw["entries"].append(dict(raw["entries"][1], fix_id=["no", "text"], target="no fix id"))       # and once more
    j.path.write_text(json.dumps(raw), encoding="utf-8")
    c = ask(app, "changes")
    assert c["all"] == ids                                    # each id once, and never the one that is no text
    u = ask(app, "undo_all", entries=c["all"])
    assert isinstance(u, dict) and fx.store["power_plan"] == BEFORE and fx.store["game_mode_on"] == BEFORE


def test_the_list_settles_the_journal_first_and_logs_what_settling_said(tmp_path):
    log = []
    app, fx, sid = fixing(tmp_path, log=log, session_only=frozenset({"background_priority"}))
    j = journal_of(app)
    crashed = j.begin("power_plan", "the active power plan", {}, BEFORE, AFTER)          # left pending by a crash
    fx.store["power_plan"] = AFTER                                                      # after its write
    ended = j.begin("background_priority", "priority of Discord.exe", {}, "normal", "below_normal", session_only=True)
    j.set_state(ended.id, "applied")
    fx.store["background_priority"] = GONE                     # its program has exited
    rows = ask(app, "changes")["rows"]
    assert [r["id"] for r in rows] == [crashed.id]
    assert (j.get(crashed.id).state, j.get(ended.id).state) == ("applied", "undone")
    assert any(line.startswith("the fix journal: Settled a change that was interrupted") for line in log)


# ---------------------------------------------------------------- the words ---

def test_every_answer_to_an_apply_or_an_undo_is_in_plain_words():
    plan = type("Plan", (), {"summary": "Power plan: Silent -> Balanced", "needs_restart": "game"})()
    for state in ("applied", "noop", "gone", "stale", "failed", "reverted", "conflict", "busy", "unsure", "new"):
        for ok in (True, False):
            v = views.applied_view(FixResult(ok, "x {'__absent__': True} --force lagdoctor undo", "0123456789ab",
                                             state), plan)
            plain(v["text"])
            assert v["entry"] == ("0123456789ab" if state in ("applied", "conflict") else None), state
    assert views.applied_view(FixResult(True, "", "../x", "applied"), plan)["entry"] is None
    for why in ("restored", "already", "ended", "not_connected", "changed", "unreadable", "write_failed",
                "no_readback", "unknown", "unsure", ""):
        for ok in (True, False):
            v = views.undone_view(FixResult(ok, "{'__gone__': True} --force", "0123456789ab", "applied", why),
                                  "Power plan: Silent -> Balanced")
            plain(v["text"])
            assert v["force"] == (why == "changed")
    for text in (views.LAST_ONLY_FIX, views.NOT_LAST, views.UNDO_ALL_STOPPED.format(title="Power plan"),
                 views.UNDO_ALL_UNSURE, views.NEWER_CHANGE, views.CONFLICT_NOTE, app_main.BUSY,
                 app_main.NO_JOURNAL, app_main.NO_LONGER_PLANNED):
        plain(text)
    assert views.refused_text(ValueError("")) == "lag-doctor will not make this change."


def test_each_outcome_is_said_by_its_own_words():
    plan = type("Plan", (), {"summary": "Power plan: Silent -> Balanced", "needs_restart": None})()
    assert views.applied_view(FixResult(True, "", "0123456789ab", "applied"), plan)["text"] == \
        "Done: Power plan: Silent -> Balanced."                                  # no restart sentence for it
    assert views.applied_view(FixResult(False, "", "0123456789ab", "applied"), plan)["text"] == views.UNCONFIRMED
    for state, text in views.APPLIED.items():
        assert views.applied_view(FixResult(False, "", None, state), plan)["text"] == text, state
    for why, text in views.UNDONE.items():
        ok = why in ("already", "ended")
        assert views.undone_view(FixResult(ok, "", "0123456789ab", "undone" if ok else "applied", why),
                                 "Power plan: Silent -> Balanced")["text"] == text, why
    assert views.undone_view(FixResult(True, "", "0123456789ab", "undone", "restored"),
                             "Power plan: Silent -> Balanced")["text"] == \
        "Undone: Power plan: Silent -> Balanced. It is back to how it was."
