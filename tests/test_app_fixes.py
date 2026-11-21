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

def test_results_offer_apply_for_the_last_recordings_fixes_the_window_applies(tmp_path):
    app, _, sid = fixing(tmp_path, fixes=[[offer(), ADVICE, offer("game_setting", {"key": "x"})],
                                          [offer("from_a_newer_lag_doctor")], [offer(params=["not", "an", "object"])]])
    old = recorded(app.home, [[offer()]], last=False, when=datetime(2026, 11, 1, 20, 0))
    r = ask(app, "results", session=sid)
    assert [[fx["apply"] for fx in f["fixes"]] for f in r["findings"]] == [[True, False, False], [False], [False]]
    assert r["fix_note"] == ""
    o = ask(app, "results", session=old)
    assert [fx["apply"] for f in o["findings"] for fx in f["fixes"]] == [False] and o["fix_note"] == views.NO_FIX_HERE


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
    assert any("apply power_plan: applied" in line for line in log)
    rows = ask(app, "changes")["rows"]
    assert [(r["id"], r["title"], r["change"], r["where"], r["undo"], r["force"]) for r in rows] == [
        (e.id, "The stand-in for power_plan", "The stand-in for power_plan: off -> on",
         "the stand-in setting of power_plan", True, False)]
    u = ask(app, "undo_change", entry=e.id, force=False)
    assert plain(u["text"]) == "Undone: The stand-in for power_plan: off -> on. It is back to how it was."
    assert u["done"] and not u["force"] and fx.store["power_plan"] == BEFORE
    assert journal_of(app).get(e.id).state == "undone" and ask(app, "changes")["rows"] == []


def test_the_page_names_a_fix_only_by_its_place_on_the_last_recordings_results(tmp_path):
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


def test_a_newer_recording_since_stops_the_plan_and_the_change(tmp_path):
    app, fx, sid = fixing(tmp_path)
    token = planned(app, sid)["plan"]
    newer = recorded(app.home, [[offer()]], when=datetime(2026, 11, 5, 21, 0))      # the last one now
    assert ask(app, "apply_fix", plan=token) == views.NOT_LAST
    assert ask(app, "plan_fix", session=sid, finding=0, fix=0) == views.NOT_LAST
    assert "writes" not in fx.store and journal_of(app).entries() == []
    assert planned(app, newer)["plan"]


# ---------------------------------------------------------------- when nothing is changed ---

def test_nothing_is_changed_while_this_windows_recording_runs(tmp_path):
    app, fx, sid = fixing(tmp_path)
    token = planned(app, sid)["plan"]
    app.rec_state = recording.RecordingState()
    words = "A recording is running: apply or undo fixes once it has ended."
    assert ask(app, "plan_fix", session=sid, finding=0, fix=0) == words
    assert ask(app, "apply_fix", plan=token) == words
    assert ask(app, "undo_change", entry="0123456789ab", force=False) == words
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
    assert "writes" not in fx.store and journal_of(app).entries() == []


def test_one_change_at_a_time(tmp_path, monkeypatch):
    app, fx, sid = fixing(tmp_path)
    token = planned(app, sid)["plan"]
    assert app.fix_lock.acquire(blocking=False)
    try:
        assert ask(app, "apply_fix", plan=token) == app_main.BUSY
        assert ask(app, "plan_fix", session=sid, finding=0, fix=0) == app_main.BUSY
        assert ask(app, "undo_change", entry="0123456789ab", force=False) == app_main.BUSY
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
                 ask(app, "undo_change", entry="0123456789ab", force=False)):
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
    raw["entries"].append(dict(raw["entries"][0], id="../../x", target="hand-edited"))
    j.path.write_text(json.dumps(raw), encoding="utf-8")
    rows = ask(app, "changes")["rows"]
    by = {r["where"]: r for r in rows}
    assert [r["where"] for r in rows][0] == "hand-edited" and len(rows) == 7               # newest first
    assert by['<img src=x onerror="window.__pwned=1"> the active power plan']["undo"]      # outside text, as text
    assert (by["Game Mode"]["undo"], by["Game Mode"]["force"]) == (False, True)
    assert by["Game Mode"]["note"] == views.CONFLICT_NOTE
    assert by["the refresh rate of DISPLAY2"]["undo"] and "not connected" in by["the refresh rate of DISPLAY2"]["note"]
    assert by["priority of Discord.exe"]["note"] == "It lasts until that program exits."
    assert (by["sg.X in GameUserSettings.ini"]["undo"], by["sg.X in GameUserSettings.ini"]["note"]) == \
        (False, views.SWEEP_CHANGE)
    assert (by["something newer"]["undo"], by["something newer"]["title"], by["something newer"]["note"]) == \
        (False, "from_a_newer_lag_doctor", views.NEWER_CHANGE)
    assert (by["hand-edited"]["id"], by["hand-edited"]["undo"], by["hand-edited"]["force"]) == (None, False, False)
    assert {by[w]["id"] for w in by if w != "hand-edited"} == {lasting, unplugged, session, conflict, sweep, newer}
    assert all(re.fullmatch(r"\d{1,2} \w{3} \d{4}, \d\d:\d\d", r["when"]) for r in rows)


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
    for text in (views.NO_FIX_HERE, views.NOT_LAST, views.NEWER_CHANGE, views.CONFLICT_NOTE, app_main.BUSY,
                 app_main.NO_JOURNAL, app_main.NO_LONGER_PLANNED):
        plain(text)
    assert views.refused_text(ValueError("")) == "lag-doctor will not make this change."
