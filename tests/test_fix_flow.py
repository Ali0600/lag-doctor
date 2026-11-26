"""The fix flow both front ends share (lagdoctor/fixes/flow.py): which offers a report makes, an apply or undo whose
journal fails said as an outcome, the case each undo met (fixes/base.FixResult.why), the journal's settling at a start,
and the window's real fixes (flow.Fixes), which no test reaches (tests/conftest.py's _no_real_fixes)."""
from __future__ import annotations

import pytest

from lagdoctor import paths
from lagdoctor.fixes import base, catalog, flow
from lagdoctor.fixes.base import GONE, Fix
from lagdoctor.fixes.journal import Journal, JournalCorrupt
from tests.conftest import fixes_guard
from tests.fake_fixes import AFTER, BEFORE, StandIn


def journal(tmp_path) -> Journal:
    return Journal(tmp_path / "journal.json", lock_timeout=0.3)


# ---------------------------------------------------------------- offers ---

def test_a_report_offers_only_fixes_of_a_known_shape_and_never_the_sweeps():
    diag = {"findings": [
        "not a finding", {"fixes": "not a list of fixes"}, {"fixes": None},
        {"fixes": ["not a fix", {"fix_id": 7, "params": {}}, {"fix_id": "", "params": {}},
                   {"fix_id": "a", "params": ["not", "an", "object"]}, {"fix_id": "a", "params": "x=1"},
                   {"fix_id": "game_setting", "title": "A sweep's", "params": {"key": "x"}},
                   {"fix_id": "a", "title": "A", "params": {"x": 1}}, {"fix_id": "b"}]}]}
    assert flow.offers(diag) == [("a", "A", {"x": 1}), ("b", "b", {})]
    for odd in ({"findings": 5}, {"findings": True}, {"findings": "abc"}, {"findings": {"fixes": []}},
                {"findings": [{"fixes": True}]}, {"findings": [{"fixes": 7}]}, {"findings": [{"fixes": "ab"}]},
                {"findings": [{"fixes": {"fix_id": "a"}}]}):
        assert flow.offers(odd) == [], odd                # a report of another shape offers nothing, never a traceback


def test_a_fix_is_applied_from_a_report_only_when_this_lag_doctor_knows_it_and_its_params_are_an_object():
    ids = flow.known()
    assert ids == frozenset(catalog.ids()) - {"game_setting"} and "power_plan" in ids and "game_setting" not in ids
    assert flow.offerable("power_plan", {"scheme": "x"}, ids)
    assert not flow.offerable("game_setting", {}, ids | {"game_setting"})        # never the sweep's own
    assert not flow.offerable("from_a_newer_lag_doctor", {}, ids)
    assert not flow.offerable("power_plan", ["scheme"], ids)
    assert not flow.offerable("power_plan", None, ids)
    assert not flow.offerable(None, {}, ids)
    assert not flow.offerable(["power_plan"], {}, ids)
    assert not flow.offerable("from_a_newer_lag_doctor", {}, ids | {"from_a_newer_lag_doctor"})   # not in the catalogue
    assert not flow.offerable("power_plan", {}, frozenset())              # a window that applies none


def test_a_fix_is_applied_from_any_report_only_when_its_params_say_what_it_changes_on_their_own():
    ids = flow.known()
    screen = {"device": "\\\\.\\DISPLAY1", "hz": 144}
    monitor = "\\\\?\\DISPLAY#ABC1234#5&1a2b3c4d&0&UID4352#{e6f07b5f-ee97-4a90-b076-33f57bf4eaa7}"
    assert flow.why_not_offered("refresh_rate", dict(screen, monitor=monitor), ids) is None
    # An older report's display name belongs to whichever monitor Windows lists first, which may be another by now.
    for odd in (screen, dict(screen, monitor=""), dict(screen, monitor=5), dict(screen, monitor="x" * 200)):
        assert flow.why_not_offered("refresh_rate", odd, ids) == "unnamed", odd
        assert not flow.offerable("refresh_rate", odd, ids)
    assert flow.why_not_offered("refresh_rate", ["device"], ids) == "params"
    assert flow.why_not_offered("from_a_newer_lag_doctor", {}, ids) == "unknown"
    assert flow.why_not_offered("game_setting", {}, ids | {"game_setting"}) == "unknown"
    assert all(catalog.find(i).stands_alone({}) for i in ids if i != "refresh_rate")    # what else they name lasts


# ---------------------------------------------------------------- apply ---

class HeldJournal(Journal):
    """A journal another lag-doctor holds: every read or write times out, as the lock's own wait does."""

    def entries(self):
        raise TimeoutError("another lag-doctor holds the journal's lock")


def test_an_apply_whose_journal_is_held_changes_nothing_and_says_so(tmp_path):
    store = {}
    fix = StandIn("test_flow", store)
    plan = fix.plan({})
    res = flow.apply(fix, plan, HeldJournal(tmp_path / "journal.json"))
    assert (res.ok, res.state) == (False, "busy") and "nothing was changed" in res.message
    assert "writes" not in store


def test_an_apply_whose_journal_fails_once_the_change_began_is_said_as_unsure(tmp_path):
    store = {}
    fix = StandIn("test_flow", store)
    plan = fix.plan({})

    class Failing(Journal):
        def set_state(self, entry_id, state, note=""):
            raise TimeoutError("another lag-doctor took the journal's lock")
    j = Failing(tmp_path / "journal.json", lock_timeout=0.3)
    res = flow.apply(fix, plan, j)
    assert (res.ok, res.state) == (False, "unsure") and "checked at the next start" in res.message
    assert store["test_flow"] == AFTER and [e.state for e in j.entries()] == ["pending"]


def test_an_apply_on_an_unreadable_journal_raises_for_its_front_end_to_say(tmp_path):
    j = journal(tmp_path)
    j.path.write_text("not a journal", encoding="utf-8")
    fix = StandIn("test_flow", {})
    with pytest.raises(JournalCorrupt):
        flow.apply(fix, fix.plan({}), j)


def test_an_apply_that_works_is_the_fixs_own(tmp_path):
    store = {}
    fix = StandIn("test_flow", store)
    j = journal(tmp_path)
    res = flow.apply(fix, fix.plan({}), j)
    assert (res.ok, res.state) == (True, "applied") and store["test_flow"] == AFTER
    assert [(e.id, e.state) for e in j.entries()] == [(res.entry_id, "applied")]


# ---------------------------------------------------------------- undo ---

def applied(tmp_path, store, fix_id="test_flow", session_only=False):
    fix = StandIn(fix_id, store, session_only=session_only)
    j = journal(tmp_path)
    res = flow.apply(fix, fix.plan({}), j)
    assert res.state == "applied"
    return fix, j, j.get(res.entry_id)


def test_each_case_an_undo_meets_is_named_for_the_front_end_to_word(tmp_path):
    """fixes/base.undo's cases, by FixResult.why, each made the only cause of its outcome."""
    def undo_with(tmp, setup, force=False, session_only=False):
        store = {}
        fix, j, e = applied(tmp, store, session_only=session_only)
        setup(store)
        return flow.undo(e, lambda fid: StandIn(fid, store, session_only=session_only), j, force=force), store

    cases = {
        "restored": (lambda s: None, {}, True),
        "already": (lambda s: s.update(test_flow=BEFORE), {}, True),
        "ended": (lambda s: s.update(test_flow=GONE), {"session_only": True}, True),
        "not_connected": (lambda s: s.update(test_flow=GONE), {}, False),
        "changed": (lambda s: s.update(test_flow="set by hand since"), {}, False),
        "unreadable": (lambda s: s.update({("refuse", "test_flow"): OSError("access denied")}), {}, False),
        "write_failed": (lambda s: s.update({("write", "test_flow"): OSError("access denied")}), {}, False),
    }
    for i, (why, (setup, kw, ok)) in enumerate(cases.items()):
        res, _ = undo_with(tmp_path / str(i), setup, **kw)
        assert (res.why, res.ok) == (why, ok), why
    res, store = undo_with(tmp_path / "forced", lambda s: s.update(test_flow="set by hand since"), force=True)
    assert (res.why, res.ok, store["test_flow"]) == ("restored", True, BEFORE)


def test_an_undo_that_does_not_take_is_named_so(tmp_path):
    store = {}
    _, j, e = applied(tmp_path, store)

    class Sticky(StandIn):                   # the write lands, the readback still shows lag-doctor's value
        def write(self, params, value):
            pass
    res = flow.undo(e, lambda fid: Sticky(fid, store), j)
    assert (res.ok, res.why) == (False, "no_readback") and j.get(e.id).state == "applied"


def test_an_undo_of_a_fix_this_lag_doctor_does_not_know_leaves_it_alone(tmp_path):
    store = {}
    _, j, e = applied(tmp_path, store)

    def unknown(fid):
        raise KeyError(fid)
    res = flow.undo(e, unknown, j)
    assert (res.ok, res.why, res.state) == (False, "unknown", "applied") and store["test_flow"] == AFTER
    assert "does not know the fix 'test_flow'" in res.message and j.get(e.id).state == "applied"


def test_an_undo_whose_journal_fails_keeps_the_entry_as_it_was(tmp_path):
    store = {}
    _, j, e = applied(tmp_path, store)

    class Failing(Journal):
        def set_state(self, entry_id, state, note=""):
            raise TimeoutError("another lag-doctor took the journal's lock")
    res = flow.undo(e, lambda fid: StandIn(fid, store), Failing(j.path, lock_timeout=0.3))
    assert (res.ok, res.why) == (False, "unsure") and j.get(e.id).state == "applied"


# ---------------------------------------------------------------- settling ---

def test_settling_uses_the_journal_and_the_fixes_it_is_given(tmp_path):
    """The window settles its own home's journal with its own fixes (App.do_changes), never the default ones."""
    store = {"test_flow": AFTER}                 # a crash after the write: the change did happen
    j = journal(tmp_path)
    pending = j.begin("test_flow", "the stand-in setting", {}, BEFORE, AFTER)
    said, asked = [], []
    flow.settle(said.append, journal=j, fix_for=lambda fid: asked.append(fid) or StandIn(fid, store))
    assert asked == ["test_flow"] and [(e.id, e.state) for e in j.entries()] == [(pending.id, "applied")]
    assert len(said) == 1 and said[0].startswith("Settled a change that was interrupted last time")
    assert not paths.journal_path().exists()     # the home's default journal was never read or written


# ---------------------------------------------------------------- the window's real fixes ---

def test_the_windows_real_fixes_keep_the_homes_journal_and_name_each_fix(tmp_path):
    fixes = flow.Fixes(tmp_path)
    assert fixes.journal().path == tmp_path / paths.JOURNAL_NAME == tmp_path / paths.journal_path().name
    assert fixes.known() == flow.known()
    assert fixes.title("power_plan") == catalog.find("power_plan").title == "Power plan"
    assert fixes.title("from_a_newer_lag_doctor") is None


def test_the_window_changes_nothing_when_it_cannot_tell_whether_it_runs_as_administrator(monkeypatch, tmp_path):
    from lagdoctor.winapi import elevation
    fixes = flow.Fixes(tmp_path)
    monkeypatch.setattr(elevation, "is_elevated", lambda: True)
    assert fixes.elevated() is True
    monkeypatch.setattr(elevation, "is_elevated", lambda: False)
    assert fixes.elevated() is False

    def fails():
        raise OSError("the token could not be read")
    monkeypatch.setattr(elevation, "is_elevated", fails)
    assert fixes.elevated() is None


def test_conftest_keeps_every_test_off_the_windows_real_fixes():
    # its guard is in place here too: a real fix asked for reads as one this lag-doctor does not know
    assert flow.Fixes.fix.__name__ == "refuse"


def test_no_test_reaches_the_windows_real_fixes(monkeypatch, tmp_path):
    """tests/conftest.py's guard, driven by hand, with the catalogue stubbed one layer below its seam: a sabotage of
    the guard then builds no real fix either."""
    built = []
    monkeypatch.setattr(flow.catalog, "get", lambda fid, **kw: built.append(fid))
    guard = fixes_guard(monkeypatch)
    caught = next(guard)
    with pytest.raises(KeyError):
        flow.Fixes(tmp_path).fix("power_plan")
    assert caught and "power_plan" in caught[0] and built == []
    with pytest.raises(AssertionError):
        next(guard)


def test_a_fix_class_is_found_by_its_id_without_being_built():
    assert catalog.find("power_plan") is catalog.find("power_plan") and issubclass(catalog.find("power_plan"), Fix)
    assert catalog.find("from_a_newer_lag_doctor") is None
    assert base.FixResult(True, "x").why == ""
