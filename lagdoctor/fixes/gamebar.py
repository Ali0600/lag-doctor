"""Game Bar settings: game_dvr_off and game_mode_on, plain HKCU DWORDs (WT 8).

read() maps each value name to the stored {type, data}, or ABSENT, so undo puts back exactly what
was there: the original type and data, or no value at all.
"""
from __future__ import annotations

from typing import Any

from lagdoctor.fixes.base import ABSENT, Fix, FixPlan, is_absent
from lagdoctor.fixes.catalog import register
from lagdoctor.fixes.gpu_preference import HIVE, no_params, raw_get
from lagdoctor.fixes.registry import RegistryBackend, dword


def _shown(value: Any) -> str:
    if is_absent(value):
        return "not set"
    if isinstance(value, dict) and "data" in value:
        return repr(value["data"])
    return repr(value)


class DwordValuesFix(Fix):
    """Sets a fixed group of HKCU values together (subclasses list (path, name, data))."""

    values: tuple[tuple[str, str, int], ...] = ()
    description = ""
    caveat = ""

    def __init__(self, reg: RegistryBackend | None = None) -> None:
        self.reg = reg if reg is not None else RegistryBackend()

    def target(self, params: dict[str, Any]) -> str:
        no_params(params)
        return "; ".join(f"{HIVE}\\{path}\\{name}" for path, name, _ in self.values)

    def read(self, params: dict[str, Any]) -> Any:
        no_params(params)
        return {name: self.reg.get(HIVE, path, name) for path, name, _ in self.values}

    def readback(self, params: dict[str, Any]) -> Any:
        no_params(params)
        return {name: raw_get(self.reg, HIVE, path, name) for path, name, _ in self.values}

    def wanted(self, params: dict[str, Any], current: Any) -> Any:
        return {name: dword(data) for _, name, data in self.values}

    def write(self, params: dict[str, Any], value: Any) -> None:
        no_params(params)
        names = [name for _, name, _ in self.values]
        if not isinstance(value, dict) or sorted(value) != sorted(names):
            raise ValueError(f"expected values for {names}, got {value!r}")
        for name in names:
            v = value[name]
            if not is_absent(v) and not (isinstance(v, dict) and isinstance(v.get("type"), int) and "data" in v):
                raise ValueError(f"not a registry value for {name}: {v!r}")
        for path, name, _ in self.values:
            self.reg.set(HIVE, path, name, value[name])

    def describe(self, before: Any, after: Any) -> str:
        changes = [f"{name} {_shown(before.get(name, ABSENT))} -> {_shown(after.get(name, ABSENT))}"
                   for _, name, _ in self.values]
        return f"{self.title}: " + "; ".join(changes)

    def plan(self, params: dict[str, Any]) -> FixPlan:
        plan = super().plan(params)
        if self.caveat:
            plan.notes.append(self.caveat)
        return plan


@register
class GameDvrOffFix(DwordValuesFix):
    id = "game_dvr_off"
    title = "Turn off Game Bar recording"
    needs_restart = "game"
    values = ((r"System\GameConfigStore", "GameDVR_Enabled", 0),
              (r"Software\Microsoft\Windows\CurrentVersion\GameDVR", "HistoricalCaptureEnabled", 0))
    caveat = ("Community-sourced (unverified): Microsoft does not document what GameDVR_Enabled and "
              "HistoricalCaptureEnabled do; community sources say 1 = on. Restart the game afterwards. The real "
              "cost of recording shows as VideoEncode GPU use, which the next capture measures.")
    description = ("Turns off Game Bar game recording and background recording (\"record what happened\"): "
                   "GameDVR_Enabled = 0 and HistoricalCaptureEnabled = 0, both under your own user (HKCU). "
                   + caveat)


@register
class GameModeOnFix(DwordValuesFix):
    id = "game_mode_on"
    title = "Turn on Game Mode"
    values = ((r"Software\Microsoft\GameBar", "AutoGameModeEnabled", 1),)
    caveat = ("Community-sourced (unverified): Microsoft does not document AutoGameModeEnabled; community "
              "sources say 1 or no value = on, 0 = off, and when it takes effect is not known. The next capture "
              "checks that Game Mode engaged (the effective power mode reads game mode while you play).")
    description = "Turns on Game Mode: AutoGameModeEnabled = 1 under your own user (HKCU). " + caveat
