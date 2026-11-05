"""The power_plan and refresh_rate fixes, on fake backends only.

An autouse tripwire replaces the real setters (PowerSetActiveScheme, ChangeDisplaySettingsExW), so a test that
forgot its fake fails instead of changing this PC's power plan or screen.
"""
from __future__ import annotations

import pytest

from lagdoctor.fixes import catalog
from lagdoctor.fixes.base import GONE
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


_FOLLOW = object()     # QueryDisplayConfig (or the stored mode) follows the mode on screen
PANEL = r"\\?\DISPLAY#CMN153C#4&25e05db5&0&UID8388688#{e6f07b5f-ee97-4a90-b076-33f57bf4eaa7}"
OTHER = r"\\?\DISPLAY#GSM5B7F#5&1a2b3c4d&0&UID4352#{e6f07b5f-ee97-4a90-b076-33f57bf4eaa7}"


class FakeDisplay:
    """One 1920x1080 panel offering 60 and 144 Hz; QueryDisplayConfig reads the exact rate (+0.003 Hz).

    `stored` is the mode Windows keeps in the profile (ENUM_REGISTRY_SETTINGS): CDS_UPDATEREGISTRY stores
    the new mode on success and on DISP_CHANGE_RESTART, whether or not it is on screen yet. `path` is the
    monitor at DEV (its monitorDevicePath)."""

    def __init__(self) -> None:
        self.mode = {"width": 1920, "height": 1080, "hz": 60}
        self.offered = [60, 144]
        self.exact_hz: object = _FOLLOW        # or pin QueryDisplayConfig's answer (stale, odd, or None)
        self.stored: object = _FOLLOW          # or pin the profile's mode (a dict, or None for none stored)
        self.path = PANEL
        self.connected = True
        self.calls: list[tuple[str, int]] = []
        self.test_result: dict[int, int] = {}
        self.set_result: dict[int, int] = {}
        self.applies_on = {0}                  # result codes after which the new rate is on screen
        self.stores_on = {0, 1}                # result codes after which the new rate is in the profile

    def current_mode(self, device):
        if not self.connected:
            raise OSError(1167, "The device is not connected")
        return dict(self.mode)

    def stored_mode(self, device):
        return dict(self.mode) if self.stored is _FOLLOW else (dict(self.stored) if self.stored else None)

    def monitor_path(self, device):
        return self.path if self.connected else None

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
        if rc in self.stores_on:
            self.stored = {**self.mode, "hz": hz}
        if rc in self.applies_on:
            self.mode["hz"] = hz
        if self.stored == self.mode:
            self.stored = _FOLLOW
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


def test_refresh_rate_undo_while_the_display_is_unplugged_keeps_the_entry(journal):
    # Changed on purpose (review finding SEC-4): this used to expect "undone". Windows keeps the stored mode for
    # that monitor, so it comes back at lag-doctor's rate when plugged in again; the entry must stay undoable.
    fake = FakeDisplay()
    fix = RefreshRateFix(display=fake)
    assert fix.apply(fix.plan({"device": DEV, "hz": 144}), journal).ok
    fake.connected = False
    [entry] = journal.undoable()
    for force in (False, True):
        res = fix.undo(entry, journal, force=force)
        assert not res.ok and res.state == "applied", res.message
        assert "run undo again when it is connected" in res.message
    assert journal.get(entry.id).state == "applied" and "not connected" in journal.get(entry.id).note
    assert fake.calls == [("test", 144), ("set", 144)]          # nothing was written while it was away
    fake.connected = True                                        # back, still at lag-doctor's 144 Hz
    [entry] = journal.undoable()
    res = fix.undo(entry, journal)
    assert res.ok and res.state == "undone", res.message
    assert fake.mode == mode(60)


def test_refresh_rate_an_entry_from_before_monitor_identities_still_undoes(journal):
    # An entry an earlier lag-doctor journaled has no "monitor": the GDI name alone is used, as it was then.
    fake = FakeDisplay()
    fix = RefreshRateFix(display=fake)
    entry = journal.begin("refresh_rate", "t", {"device": DEV, "hz": 144}, before=mode(60), after=mode(144))
    journal.set_state(entry.id, "applied")
    fake.mode = mode(144)
    fake.connected = False
    res = fix.undo(journal.get(entry.id), journal)
    assert not res.ok and res.state == "applied" and "when it is connected" in res.message, res.message
    fake.connected = True
    assert fix.undo(journal.get(entry.id), journal).ok and fake.mode == mode(60)


def test_refresh_rate_plan_stores_which_monitor_it_read(journal):
    fake = FakeDisplay()
    fix = RefreshRateFix(display=fake)
    plan = fix.plan({"device": DEV, "hz": 144})                  # the report's suggestion has no identity
    assert plan.params == {"device": DEV, "hz": 144, "monitor": PANEL}
    fix.apply(plan, journal)
    assert journal.entries()[0].params["monitor"] == PANEL       # so undo can check it
    fake.path = None                                             # Windows did not say which monitor it is
    with pytest.raises(LookupError, match="same screen"):
        fix.plan({"device": DEV, "hz": 144})


def test_refresh_rate_a_name_that_now_shows_another_monitor_is_not_read_or_written(journal):
    fake = FakeDisplay()
    fix = RefreshRateFix(display=fake)
    fix.apply(fix.plan({"device": DEV, "hz": 144}), journal)
    [entry] = journal.undoable()
    # Docked: Windows gives DISPLAY1 to an external monitor that also runs 1920x1080 at 144 Hz.
    fake.path = OTHER
    calls = list(fake.calls)
    assert fix.read(entry.params) == GONE                        # to the fix, its monitor is not connected
    with pytest.raises(RuntimeError, match="no longer shows the monitor"):
        fix.write(entry.params, mode(60))
    assert fake.calls == calls


def test_refresh_rate_undo_leaves_another_monitor_at_the_same_name_alone(journal):
    fake = FakeDisplay()
    fix = RefreshRateFix(display=fake)
    fix.apply(fix.plan({"device": DEV, "hz": 144}), journal)
    [entry] = journal.undoable()
    fake.path = OTHER
    calls = list(fake.calls)
    for force in (False, True):
        res = fix.undo(entry, journal, force=force)
        assert not res.ok and res.state == "applied", res.message
    assert fake.calls == calls and fake.mode == mode(144)        # that monitor was never touched
    fake.path = PANEL                                            # the right one is back on DISPLAY1
    assert fix.undo(journal.undoable()[0], journal).ok and fake.mode == mode(60)


@pytest.mark.parametrize("monitor", ["", 5, "x" * 200, "bad\npath"])
def test_refresh_rate_refuses_a_junk_monitor_identity(monitor):
    fake = FakeDisplay()
    with pytest.raises(ValueError):
        RefreshRateFix(display=fake).plan({"device": DEV, "hz": 144, "monitor": monitor})
    assert fake.calls == []


def test_refresh_rate_restart_needed_puts_the_stored_mode_back(journal):
    # DISP_CHANGE_RESTART: Windows did not switch, but it stored 144 Hz for the next sign-in (review finding CC-9).
    fake = FakeDisplay()
    fake.set_result[144] = 1
    fix = RefreshRateFix(display=fake)
    res = fix.apply(fix.plan({"device": DEV, "hz": 144}), journal)
    assert not res.ok and res.state == "reverted" and "restart" in res.message, res.message
    assert fake.stored_mode(DEV) == mode(60) and fake.mode == mode(60)
    assert fake.calls == [("test", 144), ("set", 144), ("test", 60), ("set", 60)]
    assert journal.entries()[0].state == "reverted"


def test_refresh_rate_a_restart_that_cannot_be_put_back_stays_undoable(journal):
    fake = FakeDisplay()
    fake.set_result[144] = 1                                     # stored for after a restart
    fake.set_result[60] = -1                                     # and storing 60 Hz back fails
    fix = RefreshRateFix(display=fake)
    res = fix.apply(fix.plan({"device": DEV, "hz": 144}), journal)
    assert not res.ok and res.state == "applied", res.message
    assert f"lagdoctor undo {res.entry_id}" in res.message
    assert fake.stored_mode(DEV) == mode(144) and fake.mode == mode(60)
    [entry] = journal.undoable()
    assert entry.id == res.entry_id
    assert fix.read(entry.params) == {**mode(60), "after_restart": mode(144)}
    del fake.set_result[60]                                      # later the driver takes it
    res = fix.undo(entry, journal)                               # ours, so no --force needed
    assert res.ok and res.state == "undone", res.message
    assert fake.stored_mode(DEV) == mode(60) and fake.mode == mode(60)


def test_refresh_rate_a_restore_that_also_needs_a_restart_counts_once_the_original_is_stored(journal):
    # Storing 60 Hz back returns DISP_CHANGE_RESTART too, but it did store it: the readback is what decides.
    fake = FakeDisplay()
    fake.set_result[144] = 1
    fake.set_result[60] = 1
    fix = RefreshRateFix(display=fake)
    res = fix.apply(fix.plan({"device": DEV, "hz": 144}), journal)
    assert not res.ok and res.state == "reverted", res.message
    assert fake.stored_mode(DEV) == mode(60) and fake.mode == mode(60)


def test_refresh_rate_a_stored_mode_that_differs_from_the_screen_is_left_alone():
    fake = FakeDisplay()
    fake.stored = mode(144)                                      # e.g. a full-screen game switched to 60 Hz
    fix = RefreshRateFix(display=fake)
    assert fix.read({"device": DEV, "hz": 144}) == {**mode(60), "after_restart": mode(144)}
    with pytest.raises(ValueError, match="stored for it"):
        fix.plan({"device": DEV, "hz": 144})
    fake.stored = None                                           # nothing stored at all
    with pytest.raises(ValueError, match="stored for it"):
        fix.plan({"device": DEV, "hz": 144})
    assert fake.calls == []


def test_refresh_rate_heal_finds_a_change_stored_for_after_a_restart(journal):
    fake = FakeDisplay()
    entry = journal.begin("refresh_rate", "t", {"device": DEV, "hz": 144, "monitor": PANEL}, before=mode(60),
                          after=mode(144))
    fake.stored = mode(144)                                      # the tool died right after DISP_CHANGE_RESTART
    res = RefreshRateFix(display=fake).heal(entry, journal)
    assert res.ok and journal.get(entry.id).state == "applied", res.message


def test_refresh_rate_heal_while_the_display_is_unplugged_keeps_the_entry(journal):
    fake = FakeDisplay()
    fake.connected = False
    entry = journal.begin("refresh_rate", "t", {"device": DEV, "hz": 144, "monitor": PANEL}, before=mode(60),
                          after=mode(144))
    res = RefreshRateFix(display=fake).heal(entry, journal)
    assert not res.ok and journal.get(entry.id).state == "applied", res.message
    assert fake.calls == []


# ======================================================================== display readers (read-only)

def _gdi_monitor_interface(device: str) -> str:
    """The monitor's device interface name from GDI (EnumDisplayDevicesW + EDD_GET_DEVICE_INTERFACE_NAME), an
    API independent of the QueryDisplayConfig path monitor_path() reads."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.EnumDisplayDevicesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                           ctypes.POINTER(display_api.DISPLAY_DEVICEW), wintypes.DWORD]
    user32.EnumDisplayDevicesW.restype = wintypes.BOOL
    mon = display_api.DISPLAY_DEVICEW()
    mon.cb = ctypes.sizeof(mon)
    assert user32.EnumDisplayDevicesW(device, 0, ctypes.byref(mon), 0x1), f"no monitor on {device}"
    return mon.DeviceID


def test_live_monitor_path_matches_gdi_and_names_a_monitor():
    displays = display_api.list_displays()
    assert displays
    for d in displays:
        path = display_api.monitor_path(d["device"])
        assert path == _gdi_monitor_interface(d["device"])
        assert path.upper().startswith("\\\\?\\DISPLAY#")
    assert display_api.monitor_path("\\\\.\\DISPLAY999") is None


def test_live_the_stored_mode_is_the_one_on_screen():
    # The fix relies on this: with no change pending, the profile's mode is the mode on screen. (A full-screen
    # game that switched modes, or a change waiting for a restart, would make it differ.)
    for d in display_api.list_displays():
        assert display_api.stored_mode(d["device"]) == display_api.current_mode(d["device"])


def test_monitor_path_is_the_qdc_targets_device_path(monkeypatch):
    for paths, want in (({"\\\\.\\DISPLAY1": {"device_path": PANEL}}, PANEL),
                        ({"\\\\.\\DISPLAY1": {"device_path": ""}}, None), ({}, None)):
        monkeypatch.setattr(display_api, "_qdc_paths", lambda paths=paths: paths)
        assert display_api.monitor_path("\\\\.\\display1") == want
    with pytest.raises(ValueError):
        display_api.monitor_path("DISPLAY1")


def test_stored_mode_reads_the_profile_and_an_empty_one_is_none(monkeypatch):
    seen: list = []

    class FakeUser32:
        def __init__(self, width, height, hz, ok=True):
            self.width, self.height, self.hz, self.ok = width, height, hz, ok

        def EnumDisplaySettingsW(self, device, which, dm_ref):
            seen.append((device, which))
            dm = dm_ref._obj                      # the DEVMODEW behind ctypes.byref()
            dm.dmPelsWidth, dm.dmPelsHeight, dm.dmDisplayFrequency = self.width, self.height, self.hz
            return self.ok

    for user32, want in ((FakeUser32(1920, 1080, 144), mode(144)), (FakeUser32(1920, 1080, 1), mode(None)),
                         (FakeUser32(0, 0, 0), None)):
        monkeypatch.setattr(display_api, "_user32", user32)
        assert display_api.stored_mode(DEV) == want
    assert seen[0] == ("\\\\.\\DISPLAY1", 0xFFFFFFFE)            # ENUM_REGISTRY_SETTINGS, (DWORD)-2
    monkeypatch.setattr(display_api, "_user32", FakeUser32(1920, 1080, 144, ok=False))
    with pytest.raises(OSError):
        display_api.stored_mode(DEV)


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
