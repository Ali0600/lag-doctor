"""The power_plan and refresh_rate fixes, on fake backends only.

An autouse tripwire replaces the real setters (PowerSetActiveScheme, ChangeDisplaySettingsExW), so a test that
forgot its fake fails instead of changing this PC's power plan or screen.
"""
from __future__ import annotations

import pytest

from lagdoctor.fixes import catalog
from lagdoctor.fixes.journal import Journal
from lagdoctor.fixes.power import PowerPlanFix
from lagdoctor.fixes.refresh_rate import QDC_TOLERANCE_HZ, RefreshRateFix
from lagdoctor.winapi import display as display_api
from lagdoctor.winapi import power as power_api

SILENT = "64a64f24-65b9-4b56-befd-5ec1eaced9b3"
BALANCED = "381b4222-f694-41f0-9685-ff5bb260df2e"
SCHEMES = [(BALANCED, "Balanced"), (SILENT, "Silent"), ("a5a5a5a5-0000-4000-8000-000000000001", "ASUS Recommended")]
DEV = "\\\\.\\DISPLAY1"


@pytest.fixture(autouse=True)
def _tripwire(monkeypatch):
    """Replace the real setters; any hit fails the test at teardown, even one Fix.apply caught and reported."""
    hits: list = []

    def real_call(*args, **kwargs):
        hits.append(args)
        raise AssertionError(f"a real setter was called from a test: {args!r}")
    for module, name in [(power_api, "set_active_scheme"), (power_api, "_PowerSetActiveScheme"),
                         (display_api, "set_refresh"), (display_api, "_ChangeDisplaySettingsExW")]:
        monkeypatch.setattr(module, name, real_call)
    yield hits
    assert not hits, f"a real setter was reached from a test: {hits}"


@pytest.fixture
def journal(tmp_path):
    return Journal(tmp_path / "journal.json", lock_timeout=0.3)


class FakePower:
    def __init__(self, active: str = SILENT) -> None:
        self.active = active
        self.sets: list[str] = []
        self.ignore = False             # the call "works" but nothing changes
        self.fail: Exception | None = None
        self.upper = False              # report the active GUID in upper case

    def active_scheme(self):
        guid = self.active.upper() if self.upper else self.active
        return guid, dict(SCHEMES).get(self.active, "")

    def list_schemes(self):
        return list(SCHEMES)

    def set_active_scheme(self, guid):
        self.sets.append(guid)
        if self.fail:
            raise self.fail
        if not self.ignore:
            self.active = guid


_FOLLOW = object()     # QueryDisplayConfig follows the mode


class FakeDisplay:
    """One 1920x1080 panel offering 60 and 144 Hz; QueryDisplayConfig reads the exact rate (+0.003 Hz)."""

    def __init__(self) -> None:
        self.mode = {"width": 1920, "height": 1080, "hz": 60}
        self.offered = [60, 144]
        self.exact_hz: object = _FOLLOW        # or pin QueryDisplayConfig's answer (stale, odd, or None)
        self.connected = True
        self.calls: list[tuple[str, int]] = []
        self.test_result: dict[int, int] = {}
        self.set_result: dict[int, int] = {}
        self.applies_on = {0}                  # result codes after which the new rate is on screen

    def current_mode(self, device):
        if not self.connected:
            raise OSError(1167, "The device is not connected")
        return dict(self.mode)

    def list_displays(self):
        if not self.connected:
            return []
        hz = self.exact_hz if self.exact_hz is not _FOLLOW else (
            self.mode["hz"] + 0.003 if self.mode["hz"] is not None else None)
        return [{"device": DEV, "name": "Generic PnP Monitor", "adapter_luid": "0x00000000_0x00010c49",
                 "internal": True, "primary": True, "width": self.mode["width"], "height": self.mode["height"],
                 "hz": hz, "max_hz": max(self.offered), "modes_hz": list(self.offered)}]

    def set_refresh(self, device, hz, test_only):
        self.calls.append(("test" if test_only else "set", hz))
        if test_only:
            return self.test_result.get(hz, 0 if hz in self.offered else -2)
        rc = self.set_result.get(hz, 0)
        if rc in self.applies_on:
            self.mode["hz"] = hz
        return rc


def mode(hz, width=1920, height=1080):
    return {"width": width, "height": height, "hz": hz}


# ======================================================================== catalogue

def test_the_tripwire_covers_the_default_backends(_tripwire):
    """A fix built without a fake reaches its setter through the patched name, so the tripwire catches it."""
    assert PowerPlanFix().power.set_active_scheme.__name__ == "real_call"
    assert RefreshRateFix().display.set_refresh.__name__ == "real_call"
    with pytest.raises(AssertionError, match="real setter"):
        power_api.set_active_scheme(power_api.active_scheme()[0])   # the plan already active: a no-op anyway
    assert len(_tripwire) == 1
    _tripwire.clear()                                           # this hit was the point of the test


def test_both_fixes_are_in_the_catalogue_with_the_real_backends_by_default():
    assert {"power_plan", "refresh_rate"} <= set(catalog.ids())
    assert catalog.get("power_plan").power is power_api
    assert catalog.get("refresh_rate").display is display_api
    for cls in (PowerPlanFix, RefreshRateFix):
        assert (cls.needs_admin, cls.needs_restart, cls.session_only) == (False, None, False)


# ======================================================================== power_plan

def test_power_plan_journals_switches_and_undo_restores(journal):
    fake = FakePower()
    fix = PowerPlanFix(power=fake)
    plan = fix.plan({"scheme": BALANCED})
    assert (plan.before, plan.after, plan.summary) == (SILENT, BALANCED, "Power plan: Silent -> Balanced")
    res = fix.apply(plan, journal)
    assert res.ok and res.state == "applied" and fake.active == BALANCED
    [entry] = journal.entries()
    assert (entry.fix_id, entry.before, entry.after, entry.state) == ("power_plan", SILENT, BALANCED, "applied")
    assert fix.undo(entry, journal).ok
    assert fake.active == SILENT and fake.sets == [BALANCED, SILENT]
    assert journal.get(entry.id).state == "undone"


def test_power_plan_refuses_a_plan_that_is_not_installed(journal):
    fake = FakePower()
    with pytest.raises(ValueError, match="not installed"):
        PowerPlanFix(power=fake).plan({"scheme": "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"})
    assert fake.sets == [] and journal.entries() == []


@pytest.mark.parametrize("bad", ["Balanced", "{381b4222-f694-41f0-9685-ff5bb260df2e}", "", None])
def test_power_plan_refuses_a_malformed_guid(bad):
    with pytest.raises(ValueError):
        PowerPlanFix(power=FakePower()).plan({"scheme": bad})


def test_power_plan_guid_case_does_not_matter(journal):
    fake = FakePower()
    fake.upper = True                                   # Windows' spelling and ours differ only in case
    fix = PowerPlanFix(power=fake)
    plan = fix.plan({"scheme": SILENT.upper()})
    assert plan.noop
    assert fix.apply(plan, journal).state == "noop" and fake.sets == []


def test_power_plan_a_switch_that_does_not_take_is_failed(journal):
    fake = FakePower()
    fake.ignore = True
    fix = PowerPlanFix(power=fake)
    res = fix.apply(fix.plan({"scheme": BALANCED}), journal)
    assert not res.ok and res.state == "failed" and fake.active == SILENT
    assert journal.entries()[0].state == "failed"


def test_power_plan_a_refused_switch_is_reported(journal):
    fake = FakePower()
    fake.fail = OSError(5, "Access is denied")
    fix = PowerPlanFix(power=fake)
    res = fix.apply(fix.plan({"scheme": BALANCED}), journal)
    assert not res.ok and "Access is denied" in res.message and fake.active == SILENT


# ======================================================================== refresh_rate

def test_refresh_rate_tests_first_then_sets_and_undo_restores(journal):
    fake = FakeDisplay()
    fix = RefreshRateFix(display=fake)
    plan = fix.plan({"device": DEV, "hz": 144})
    assert (plan.before, plan.after) == (mode(60), mode(144))
    assert plan.summary == "Refresh rate: 60 Hz -> 144 Hz at 1920x1080"
    res = fix.apply(plan, journal)
    assert res.ok and res.state == "applied" and fake.mode == mode(144)
    assert fake.calls == [("test", 144), ("set", 144)]          # CDS_TEST always before the real change
    [entry] = journal.entries()
    assert (entry.before, entry.after) == (mode(60), mode(144))
    assert fix.undo(entry, journal).ok
    assert fake.mode == mode(60) and fake.calls[2:] == [("test", 60), ("set", 60)]


def test_refresh_rate_a_rate_windows_rejects_in_the_test_is_never_set(journal):
    fake = FakeDisplay()
    fake.test_result[144] = -2                                  # DISP_CHANGE_BADMODE
    fix = RefreshRateFix(display=fake)
    res = fix.apply(fix.plan({"device": DEV, "hz": 144}), journal)
    assert not res.ok and res.state == "failed" and "rejected 144 Hz" in res.message
    assert fake.calls == [("test", 144)] and fake.mode == mode(60)


def test_refresh_rate_restart_needed_counts_as_a_failure(journal):
    fake = FakeDisplay()
    fake.set_result[144] = 1                                    # DISP_CHANGE_RESTART
    fake.applies_on = {0, 1}                                    # even if the rate shows up right away
    fix = RefreshRateFix(display=fake)
    res = fix.apply(fix.plan({"device": DEV, "hz": 144}), journal)
    assert not res.ok and res.state == "reverted" and "restart" in res.message
    assert fake.mode == mode(60)


def test_refresh_rate_needs_both_readouts_to_agree(journal):
    fake = FakeDisplay()
    fake.exact_hz = 60.003                                      # QueryDisplayConfig still sees the old rate
    fix = RefreshRateFix(display=fake)
    res = fix.apply(fix.plan({"device": DEV, "hz": 144}), journal)
    assert not res.ok and res.state == "reverted" and "display_config_hz" in res.message
    assert fake.mode == mode(60)


@pytest.mark.parametrize("exact, agrees", [(145.4, True), (145.5, False), (142.6, True), (142.5, False),
                                           (144.003, True), (None, False)])
def test_refresh_rate_readback_tolerance(exact, agrees):
    assert QDC_TOLERANCE_HZ == 1.5
    fake = FakeDisplay()
    fake.mode = mode(144)
    fake.exact_hz = exact
    got = RefreshRateFix(display=fake).readback({"device": DEV, "hz": 144})
    assert (got == mode(144)) is agrees


def test_refresh_rate_refuses_a_rate_the_display_does_not_offer(journal):
    fake = FakeDisplay()
    with pytest.raises(ValueError, match=r"does not offer 120 Hz.*\[60, 144\]"):
        RefreshRateFix(display=fake).plan({"device": DEV, "hz": 120})
    assert fake.calls == []


@pytest.mark.parametrize("params", [{"device": DEV, "hz": True}, {"device": DEV, "hz": 1},
                                    {"device": DEV, "hz": 144.0}, {"device": DEV, "hz": "144"},
                                    {"device": DEV, "hz": None}, {"device": DEV, "hz": 5000},
                                    {"device": "DISPLAY1", "hz": 144}, {"device": r"\\.\DISPLAY1;x", "hz": 144},
                                    {"hz": 144}])
def test_refresh_rate_refuses_junk_params(params):
    fake = FakeDisplay()
    with pytest.raises(ValueError):
        RefreshRateFix(display=fake).plan(params)
    assert fake.calls == []


def test_refresh_rate_refuses_when_the_current_rate_is_unknown():
    fake = FakeDisplay()
    fake.mode = mode(None)                                       # "hardware default": nothing to restore
    with pytest.raises(ValueError, match="hardware default"):
        RefreshRateFix(display=fake).plan({"device": DEV, "hz": 144})


def test_refresh_rate_never_changes_the_resolution_on_undo(journal):
    fake = FakeDisplay()
    fix = RefreshRateFix(display=fake)
    fix.apply(fix.plan({"device": DEV, "hz": 144}), journal)
    fake.mode = mode(144, 1280, 720)                             # the user changed the resolution later
    calls_before = list(fake.calls)
    res = fix.undo(journal.undoable()[0], journal, force=True)
    assert not res.ok and "never changes the resolution" in res.message
    assert fake.calls == calls_before and fake.mode == mode(144, 1280, 720)


def test_refresh_rate_undo_after_the_display_was_unplugged(journal):
    fake = FakeDisplay()
    fix = RefreshRateFix(display=fake)
    fix.apply(fix.plan({"device": DEV, "hz": 144}), journal)
    fake.connected = False
    res = fix.undo(journal.undoable()[0], journal)
    assert res.ok and res.state == "undone" and "no longer exists" in res.message
    assert fake.calls == [("test", 144), ("set", 144)]


def test_refresh_rate_plan_for_a_display_that_is_not_connected():
    fake = FakeDisplay()
    fake.connected = False
    with pytest.raises(LookupError):
        RefreshRateFix(display=fake).plan({"device": DEV, "hz": 144})


def test_refresh_rate_heal_finds_a_change_a_crash_interrupted(journal):
    fake = FakeDisplay()
    fix = RefreshRateFix(display=fake)
    entry = journal.begin("refresh_rate", "t", {"device": DEV, "hz": 144}, before=mode(60), after=mode(144))
    fake.mode = mode(144)                                        # the write landed, then the tool died
    assert fix.heal(entry, journal).ok and journal.get(entry.id).state == "applied"
