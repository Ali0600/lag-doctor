"""The registry fixes: gpu_preference, game_dvr_off, game_mode_on, windowed_optimizations_on.

They only ever run against reg_sandbox (HKCU\\Software\\LagDoctorTest\\<uuid>), and what is really
stored is checked with a raw winreg read of the sandbox key, not through the fix or its backend.
"""
from __future__ import annotations

import winreg

import pytest

from lagdoctor.fixes import catalog
from lagdoctor.fixes.base import ABSENT
from lagdoctor.fixes.gamebar import GameDvrOffFix, GameModeOnFix
from lagdoctor.fixes.gpu_preference import (GPU_PREFS_PATH, GpuPreferenceFix, get_field, remove_field,
                                            set_field)
from lagdoctor.fixes.journal import Journal
from lagdoctor.fixes.registry import RegistryBackend, dword, sz
from lagdoctor.fixes.windowed import GLOBAL_SETTINGS, WindowedOptimizationsFix

EXE = r"C:\Games\Example\game.exe"
DVR_PATH = r"System\GameConfigStore"
CAPTURE_PATH = r"Software\Microsoft\Windows\CurrentVersion\GameDVR"
GAMEBAR_PATH = r"Software\Microsoft\GameBar"


@pytest.fixture
def journal(tmp_path):
    return Journal(tmp_path / "journal.json", lock_timeout=2)


def stored(sandbox: RegistryBackend, path: str, name: str, hive: str = "HKCU"):
    """What really sits in the sandbox: (data, type), or None when the value does not exist."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, f"{sandbox.prefix}\\{hive}\\{path}") as key:
            return winreg.QueryValueEx(key, name)
    except FileNotFoundError:
        return None


def apply_ok(fix, params, journal):
    plan = fix.plan(params)
    res = fix.apply(plan, journal)
    assert res.ok and res.state == "applied", res.message
    return plan


def undo_ok(fix, journal):
    [entry] = journal.undoable()
    res = fix.undo(entry, journal)
    assert res.ok and res.state == "undone", res.message


# ----------------------------------------------------------- field parsing --

@pytest.mark.parametrize("text, expected", [
    ("GpuPreference=1;", "1"),
    ("GpuPreference=1; ", "1"),                     # a real value here has that trailing space (WT 8)
    ("AppStatus=4;", None),
    ("", None),
    ("SwapEffectUpgradeEnable=1;GpuPreference=2;VRROptimizeEnable=0;", "2"),
    (" gpupreference = 0 ;", "0"),                  # the key matches ignoring case and spacing
    ("GpuPreference=2", "2"),                       # an unterminated last field
    ("GpuPreference=1;GpuPreference=1;", "1"),      # duplicates that agree
    ("XGpuPreference=1;GpuPreferenceX=2;", None),   # other keys that merely contain the name
    ("GpuPreference;", None),                       # no "=": not a field
])
def test_get_field(text, expected):
    assert get_field(text, "GpuPreference") == expected


def test_get_field_refuses_duplicates_that_disagree():
    with pytest.raises(ValueError):
        get_field("GpuPreference=1;GpuPreference=2;", "GpuPreference")


@pytest.mark.parametrize("text, expected", [
    ("", "GpuPreference=2;"),
    ("GpuPreference=1;", "GpuPreference=2;"),
    ("GpuPreference=1; ", "GpuPreference=2; "),
    ("AppStatus=4;", "AppStatus=4;GpuPreference=2;"),
    ("AppStatus=4", "AppStatus=4;GpuPreference=2;"),
    ("VRROptimizeEnable=1; ", "VRROptimizeEnable=1;GpuPreference=2; "),
    ("A=1;GpuPreference=0;B=2;", "A=1;GpuPreference=2;B=2;"),
    (" GpuPreference = 1 ;X=1", " GpuPreference = 2 ;X=1"),
    ("gpupreference=1;", "gpupreference=2;"),
    ("GpuPreference=;", "GpuPreference=2;"),
    ("GpuPreference=1;;A=1;", "GpuPreference=2;;A=1;"),
    ("GpuPreference=1;GpuPreference=0;", "GpuPreference=2;GpuPreference=2;"),
])
def test_set_field_changes_only_its_own_field(text, expected):
    assert set_field(text, "GpuPreference", "2") == expected


@pytest.mark.parametrize("text, expected", [
    ("GpuPreference=2;", ""),
    ("AppStatus=4;GpuPreference=2;", "AppStatus=4;"),
    ("VRROptimizeEnable=1;GpuPreference=2; ", "VRROptimizeEnable=1; "),
    ("A=1;GpuPreference=2;B=2;", "A=1;B=2;"),
    ("A=1;GpuPreference=2", "A=1;"),
    ("A=1;gpupreference=2;GpuPreference=2;", "A=1;"),
    ("A=1;", "A=1;"),
])
def test_remove_field_keeps_everything_else(text, expected):
    assert remove_field(text, "GpuPreference") == expected


@pytest.mark.parametrize("original", ["AppStatus=4;", "VRROptimizeEnable=1; ", "A=1;;B=2;", ""])
def test_adding_then_removing_a_field_gives_back_the_original_exactly(original):
    assert remove_field(set_field(original, "GpuPreference", "2"), "GpuPreference") == original


# ---------------------------------------------------------- gpu_preference --

def test_gpu_preference_creates_the_value_and_undo_deletes_it(reg_sandbox, journal):
    fix = GpuPreferenceFix(reg=reg_sandbox)
    plan = apply_ok(fix, {"exe": EXE, "preference": 2}, journal)
    assert (plan.before, plan.after, plan.needs_restart) == (ABSENT, "2", "game")
    assert stored(reg_sandbox, GPU_PREFS_PATH, EXE) == ("GpuPreference=2;", winreg.REG_SZ)
    undo_ok(fix, journal)
    assert stored(reg_sandbox, GPU_PREFS_PATH, EXE) is None


def test_gpu_preference_keeps_spacing_and_leaves_other_values_alone(reg_sandbox, journal):
    tool = r"C:\Program Files\ASUS\tool.exe"
    reg_sandbox.set("HKCU", GPU_PREFS_PATH, EXE, sz("GpuPreference=1; "))
    reg_sandbox.set("HKCU", GPU_PREFS_PATH, tool, sz("GpuPreference=1; "))
    reg_sandbox.set("HKCU", GPU_PREFS_PATH, "GraphicsFeaturesNotificationConfig", dword(1))
    fix = GpuPreferenceFix(reg=reg_sandbox)
    plan = apply_ok(fix, {"exe": EXE, "preference": 2}, journal)
    assert (plan.before, plan.after) == ("1", "2")
    assert stored(reg_sandbox, GPU_PREFS_PATH, EXE) == ("GpuPreference=2; ", winreg.REG_SZ)
    assert stored(reg_sandbox, GPU_PREFS_PATH, tool) == ("GpuPreference=1; ", winreg.REG_SZ)
    assert stored(reg_sandbox, GPU_PREFS_PATH, "GraphicsFeaturesNotificationConfig") == (1, winreg.REG_DWORD)
    undo_ok(fix, journal)
    assert stored(reg_sandbox, GPU_PREFS_PATH, EXE) == ("GpuPreference=1; ", winreg.REG_SZ)


def test_gpu_preference_adds_its_field_and_undo_keeps_a_later_edit_to_another_field(reg_sandbox, journal):
    reg_sandbox.set("HKCU", GPU_PREFS_PATH, EXE, sz("AppStatus=4;"))
    fix = GpuPreferenceFix(reg=reg_sandbox)
    apply_ok(fix, {"exe": EXE, "preference": 2}, journal)
    assert stored(reg_sandbox, GPU_PREFS_PATH, EXE) == ("AppStatus=4;GpuPreference=2;", winreg.REG_SZ)
    # Windows (or the user) edits other fields of the same value afterwards; undo must keep that.
    reg_sandbox.set("HKCU", GPU_PREFS_PATH, EXE, sz("AppStatus=0;GpuPreference=2;X=1;"))
    undo_ok(fix, journal)
    assert stored(reg_sandbox, GPU_PREFS_PATH, EXE) == ("AppStatus=0;X=1;", winreg.REG_SZ)


def test_gpu_preference_undo_leaves_a_later_change_to_its_own_field_alone(reg_sandbox, journal):
    fix = GpuPreferenceFix(reg=reg_sandbox)
    apply_ok(fix, {"exe": EXE, "preference": 2}, journal)
    reg_sandbox.set("HKCU", GPU_PREFS_PATH, EXE, sz("GpuPreference=1;"))   # the user picked power saving
    [entry] = journal.undoable()
    res = fix.undo(entry, journal)
    assert not res.ok and res.state == "conflict"
    assert stored(reg_sandbox, GPU_PREFS_PATH, EXE) == ("GpuPreference=1;", winreg.REG_SZ)


def test_gpu_preference_already_set_is_a_noop(reg_sandbox, journal):
    reg_sandbox.set("HKCU", GPU_PREFS_PATH, EXE, sz("GpuPreference=2;AppStatus=4; "))
    fix = GpuPreferenceFix(reg=reg_sandbox)
    res = fix.apply(fix.plan({"exe": EXE, "preference": 2}), journal)
    assert res.ok and res.state == "noop" and journal.entries() == []
    assert stored(reg_sandbox, GPU_PREFS_PATH, EXE) == ("GpuPreference=2;AppStatus=4; ", winreg.REG_SZ)


def test_gpu_preference_finds_the_value_by_the_normalised_path(reg_sandbox, journal):
    reg_sandbox.set("HKCU", GPU_PREFS_PATH, EXE, sz("GpuPreference=1;"))
    fix = GpuPreferenceFix(reg=reg_sandbox)
    plan = apply_ok(fix, {"exe": "C:/Games/Example/game.exe", "preference": 2}, journal)
    assert plan.before == "1"
    assert stored(reg_sandbox, GPU_PREFS_PATH, EXE) == ("GpuPreference=2;", winreg.REG_SZ)


@pytest.mark.parametrize("value", [dword(2), {"type": winreg.REG_MULTI_SZ, "data": ["GpuPreference=2;"]},
                                   {"type": winreg.REG_BINARY, "data": b"GpuPreference=2;"}])
def test_gpu_preference_refuses_a_value_that_is_not_text(reg_sandbox, journal, value):
    reg_sandbox.set("HKCU", GPU_PREFS_PATH, EXE, value)
    before = stored(reg_sandbox, GPU_PREFS_PATH, EXE)
    with pytest.raises(ValueError, match="not a text value"):
        GpuPreferenceFix(reg=reg_sandbox).plan({"exe": EXE, "preference": 2})
    assert stored(reg_sandbox, GPU_PREFS_PATH, EXE) == before


def test_gpu_preference_refuses_a_field_that_appears_twice_with_different_values(reg_sandbox):
    reg_sandbox.set("HKCU", GPU_PREFS_PATH, EXE, sz("GpuPreference=1;GpuPreference=2;"))
    with pytest.raises(ValueError, match="more than once"):
        GpuPreferenceFix(reg=reg_sandbox).plan({"exe": EXE, "preference": 2})


def test_gpu_preference_refuses_an_absurdly_long_value(reg_sandbox):
    reg_sandbox.set("HKCU", GPU_PREFS_PATH, EXE, sz("A=1;" * 17_000))          # 68,000 characters
    with pytest.raises(ValueError, match="characters"):
        GpuPreferenceFix(reg=reg_sandbox).plan({"exe": EXE, "preference": 2})


@pytest.mark.parametrize("value", ["2;X=1", "2\0", "9" * 300, 2, None])
def test_a_field_fix_refuses_to_write_a_value_that_is_not_one_field(reg_sandbox, value):
    with pytest.raises(ValueError):
        GpuPreferenceFix(reg=reg_sandbox).write({"exe": EXE, "preference": 2}, value)
    assert stored(reg_sandbox, GPU_PREFS_PATH, EXE) is None


@pytest.mark.parametrize("value", [{"AutoGameModeEnabled": 1}, {"AutoGameModeEnabled": {"data": 1}},
                                   {"Other": dword(1)}, {}, dword(1), None])
def test_a_dword_fix_refuses_to_write_malformed_values(reg_sandbox, value):
    with pytest.raises(ValueError):
        GameModeOnFix(reg=reg_sandbox).write({}, value)
    assert stored(reg_sandbox, GAMEBAR_PATH, "AutoGameModeEnabled") is None


@pytest.mark.parametrize("params", [
    {"exe": "game.exe", "preference": 2},
    {"exe": r"Games\game.exe", "preference": 2},
    {"exe": r"C:Games\game.exe", "preference": 2},
    {"exe": r"C:\Games\game.bat", "preference": 2},
    {"exe": r"C:\Games\game.exe ", "preference": 2},
    {"exe": "\\\\?\\C:\\Games\\game.exe", "preference": 2},
    {"exe": "\\\\.\\C:\\Games\\game.exe", "preference": 2},
    {"exe": "C:\\Ga|mes\\game.exe", "preference": 2},
    {"exe": "C:\\" + "a" * 1100 + ".exe", "preference": 2},
    {"exe": 5, "preference": 2},
    {"exe": EXE, "preference": 3},
    {"exe": EXE, "preference": True},
    {"exe": EXE, "preference": 2.0},
    {"exe": EXE, "preference": "2"},
    {"exe": EXE},
    {"exe": EXE, "preference": 2, "extra": 1},
])
def test_gpu_preference_rejects_bad_params(reg_sandbox, params):
    with pytest.raises(ValueError):
        GpuPreferenceFix(reg=reg_sandbox).plan(params)
    with pytest.raises(FileNotFoundError):                         # nothing was created
        winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_sandbox.prefix)


@pytest.mark.parametrize("exe", [r"C:\Program Files (x86)\Steam\steamapps\common\Some Game\Game-Win64.EXE",
                                 r"\\server\share\games\game.exe", r"E:\game.exe"])
def test_gpu_preference_accepts_real_paths(reg_sandbox, journal, exe):
    fix = GpuPreferenceFix(reg=reg_sandbox)
    apply_ok(fix, {"exe": exe, "preference": 2}, journal)
    assert stored(reg_sandbox, GPU_PREFS_PATH, exe) == ("GpuPreference=2;", winreg.REG_SZ)


# --------------------------------------------- windowed_optimizations_on --

def test_windowed_optimizations_creates_the_global_value_and_undo_deletes_it(reg_sandbox, journal):
    fix = WindowedOptimizationsFix(reg=reg_sandbox)
    plan = apply_ok(fix, {}, journal)
    assert (plan.before, plan.after, plan.needs_restart) == (ABSENT, "1", "game")
    assert stored(reg_sandbox, GPU_PREFS_PATH, GLOBAL_SETTINGS) == ("SwapEffectUpgradeEnable=1;", winreg.REG_SZ)
    undo_ok(fix, journal)
    assert stored(reg_sandbox, GPU_PREFS_PATH, GLOBAL_SETTINGS) is None


@pytest.mark.parametrize("original, changed", [
    ("SwapEffectUpgradeEnable=0;VRROptimizeEnable=0;", "SwapEffectUpgradeEnable=1;VRROptimizeEnable=0;"),
    ("VRROptimizeEnable=1; ", "VRROptimizeEnable=1;SwapEffectUpgradeEnable=1; "),
    ("AutoHDREnable=1;SwapEffectUpgradeEnable = 0 ;", "AutoHDREnable=1;SwapEffectUpgradeEnable = 1 ;"),
])
def test_windowed_optimizations_keeps_the_other_fields_and_undo_restores_exactly(reg_sandbox, journal, original,
                                                                                 changed):
    reg_sandbox.set("HKCU", GPU_PREFS_PATH, GLOBAL_SETTINGS, sz(original))
    fix = WindowedOptimizationsFix(reg=reg_sandbox)
    apply_ok(fix, {}, journal)
    assert stored(reg_sandbox, GPU_PREFS_PATH, GLOBAL_SETTINGS) == (changed, winreg.REG_SZ)
    undo_ok(fix, journal)
    assert stored(reg_sandbox, GPU_PREFS_PATH, GLOBAL_SETTINGS) == (original, winreg.REG_SZ)


def test_windowed_optimizations_keeps_a_rare_expand_sz_type(reg_sandbox, journal):
    reg_sandbox.set("HKCU", GPU_PREFS_PATH, GLOBAL_SETTINGS, {"type": winreg.REG_EXPAND_SZ, "data": "A=1;"})
    fix = WindowedOptimizationsFix(reg=reg_sandbox)
    apply_ok(fix, {}, journal)
    assert stored(reg_sandbox, GPU_PREFS_PATH, GLOBAL_SETTINGS) == ("A=1;SwapEffectUpgradeEnable=1;",
                                                                     winreg.REG_EXPAND_SZ)


# ----------------------------------------------------------- game_dvr_off --

def test_game_dvr_off_sets_both_values_and_undo_restores_each(reg_sandbox, journal):
    reg_sandbox.set("HKCU", DVR_PATH, "GameDVR_Enabled", dword(1))       # as found on this machine (WT 8)
    fix = GameDvrOffFix(reg=reg_sandbox)
    plan = apply_ok(fix, {}, journal)
    assert plan.needs_restart == "game"
    assert stored(reg_sandbox, DVR_PATH, "GameDVR_Enabled") == (0, winreg.REG_DWORD)
    assert stored(reg_sandbox, CAPTURE_PATH, "HistoricalCaptureEnabled") == (0, winreg.REG_DWORD)
    undo_ok(fix, journal)
    assert stored(reg_sandbox, DVR_PATH, "GameDVR_Enabled") == (1, winreg.REG_DWORD)
    assert stored(reg_sandbox, CAPTURE_PATH, "HistoricalCaptureEnabled") is None


def test_game_dvr_off_puts_the_first_value_back_when_the_second_write_fails(reg_sandbox, journal):
    class SecondWriteFails(RegistryBackend):
        def set(self, hive, path, name, value):
            if name == "HistoricalCaptureEnabled" and value == dword(0):
                raise PermissionError(13, "Access is denied")
            super().set(hive, path, name, value)

    reg_sandbox.set("HKCU", DVR_PATH, "GameDVR_Enabled", dword(1))
    fix = GameDvrOffFix(reg=SecondWriteFails(prefix=reg_sandbox.prefix))
    res = fix.apply(fix.plan({}), journal)
    assert not res.ok and res.state == "reverted", res.message
    assert stored(reg_sandbox, DVR_PATH, "GameDVR_Enabled") == (1, winreg.REG_DWORD)
    assert stored(reg_sandbox, CAPTURE_PATH, "HistoricalCaptureEnabled") is None


def test_game_dvr_off_undo_leaves_a_later_change_alone(reg_sandbox, journal):
    fix = GameDvrOffFix(reg=reg_sandbox)
    apply_ok(fix, {}, journal)
    reg_sandbox.set("HKCU", DVR_PATH, "GameDVR_Enabled", dword(1))       # the user turned it back on
    [entry] = journal.undoable()
    res = fix.undo(entry, journal)
    assert not res.ok and res.state == "conflict"
    assert stored(reg_sandbox, CAPTURE_PATH, "HistoricalCaptureEnabled") == (0, winreg.REG_DWORD)


def test_game_dvr_off_noop_when_both_are_off(reg_sandbox, journal):
    reg_sandbox.set("HKCU", DVR_PATH, "GameDVR_Enabled", dword(0))
    reg_sandbox.set("HKCU", CAPTURE_PATH, "HistoricalCaptureEnabled", dword(0))
    fix = GameDvrOffFix(reg=reg_sandbox)
    res = fix.apply(fix.plan({}), journal)
    assert res.ok and res.state == "noop" and journal.entries() == []


# ----------------------------------------------------------- game_mode_on --

@pytest.mark.parametrize("original", [None, dword(0), sz("0")])
def test_game_mode_on_sets_one_and_undo_restores_the_original(reg_sandbox, journal, original):
    if original is not None:
        reg_sandbox.set("HKCU", GAMEBAR_PATH, "AutoGameModeEnabled", original)
    reg_sandbox.set("HKCU", GAMEBAR_PATH, "GamepadDoublePressIntervalMs", dword(1))   # a neighbour, as here
    fix = GameModeOnFix(reg=reg_sandbox)
    plan = apply_ok(fix, {}, journal)
    assert plan.needs_restart is None
    assert stored(reg_sandbox, GAMEBAR_PATH, "AutoGameModeEnabled") == (1, winreg.REG_DWORD)
    undo_ok(fix, journal)
    want = None if original is None else (original["data"], original["type"])
    assert stored(reg_sandbox, GAMEBAR_PATH, "AutoGameModeEnabled") == want
    assert stored(reg_sandbox, GAMEBAR_PATH, "GamepadDoublePressIntervalMs") == (1, winreg.REG_DWORD)


def test_game_mode_on_noop_when_on(reg_sandbox, journal):
    reg_sandbox.set("HKCU", GAMEBAR_PATH, "AutoGameModeEnabled", dword(1))
    fix = GameModeOnFix(reg=reg_sandbox)
    res = fix.apply(fix.plan({}), journal)
    assert res.ok and res.state == "noop" and journal.entries() == []


@pytest.mark.parametrize("cls", [GameDvrOffFix, GameModeOnFix, WindowedOptimizationsFix])
@pytest.mark.parametrize("params", [{"x": 1}, None, []])
def test_fixes_without_params_refuse_any(reg_sandbox, cls, params):
    with pytest.raises(ValueError):
        cls(reg=reg_sandbox).plan(params)


# ------------------------------------------------------------ all of them --

ALL = [(GpuPreferenceFix, {"exe": EXE, "preference": 2}), (WindowedOptimizationsFix, {}),
       (GameDvrOffFix, {}), (GameModeOnFix, {})]


class MemoryOnlyBackend(RegistryBackend):
    """Keeps writes in memory and reports them back, but never stores them: a readback that trusted
    this backend would call a lost write a success."""

    def __init__(self, prefix: str) -> None:
        super().__init__(prefix)
        self.memory: dict = {}

    def get(self, hive, path, name):
        if (hive, path, name) in self.memory:
            return self.memory[(hive, path, name)]
        return super().get(hive, path, name)

    def set(self, hive, path, name, value):
        self.memory[(hive, path, name)] = value


@pytest.mark.parametrize("cls, params", ALL)
def test_readback_reads_the_registry_itself_not_the_backend(reg_sandbox, journal, cls, params):
    fix = cls(reg=MemoryOnlyBackend(reg_sandbox.prefix))
    res = fix.apply(fix.plan(params), journal)
    assert not res.ok and res.state != "applied", res.message
    assert journal.undoable() == []


@pytest.mark.parametrize("cls, params", ALL)
def test_registry_fixes_write_only_under_hkcu(reg_sandbox, journal, cls, params):
    apply_ok(cls(reg=reg_sandbox), params, journal)
    with pytest.raises(FileNotFoundError):
        winreg.OpenKey(winreg.HKEY_CURRENT_USER, f"{reg_sandbox.prefix}\\HKLM")
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, f"{reg_sandbox.prefix}\\HKCU"):
        pass


@pytest.mark.parametrize("fix_id, params", [("gpu_preference", {"exe": EXE, "preference": 2}),
                                            ("windowed_optimizations_on", {}), ("game_dvr_off", {}),
                                            ("game_mode_on", {})])
def test_each_fix_says_plainly_what_is_community_sourced(reg_sandbox, fix_id, params):
    fix = catalog.get(fix_id, reg=reg_sandbox)
    assert fix.reg is reg_sandbox
    for text in (fix.description, " ".join(fix.plan(params).notes)):
        assert "community" in text.lower() and "unverified" in text.lower(), text


def test_the_catalog_lists_the_settings_fixes():
    assert {"gpu_preference", "game_dvr_off", "game_mode_on", "windowed_optimizations_on",
            "background_priority"} <= set(catalog.ids())


def test_a_fix_built_without_a_backend_uses_the_real_registry_unsandboxed():
    # Only constructed, never planned or applied: proves the default, touches nothing.
    for cls, _ in ALL:
        assert cls().reg.prefix == ""
