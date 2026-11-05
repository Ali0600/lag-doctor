"""Fix "power_plan": switch the active Windows power plan, journaled and reversible.

PowerSetActiveScheme is allowed for standard users (the ActionSetActive security descriptor grants Builtin
Users read and write, WT 7.5), so no elevation. The value journaled and compared is the plan's GUID in lower
case; the readback is active_scheme() (DESIGN.md, Fixes).
"""
from __future__ import annotations

from typing import Any

from lagdoctor.fixes.base import Fix
from lagdoctor.fixes.catalog import register
from lagdoctor.winapi.power import guid_from_str, guid_str


def _guid(value: Any) -> str:
    """Canonical lower-case GUID text; ValueError for anything else."""
    return guid_str(guid_from_str(value))


@register
class PowerPlanFix(Fix):
    id = "power_plan"
    title = "Power plan"
    needs_admin = False
    needs_restart = None

    def __init__(self, power: Any = None) -> None:
        """`power` provides active_scheme, list_schemes and set_active_scheme; default the real winapi.power."""
        if power is None:
            from lagdoctor.winapi import power as power_api
            power = power_api
        self.power = power

    def target(self, params: dict[str, Any]) -> str:
        return "the active power plan (Control Panel > Hardware and Sound > Power Options)"

    def read(self, params: dict[str, Any]) -> str:
        return _guid(self.power.active_scheme()[0])

    def write(self, params: dict[str, Any], value: Any) -> None:
        self.power.set_active_scheme(_guid(value))

    def wanted(self, params: dict[str, Any], current: Any) -> str:
        guid = _guid(params.get("scheme"))
        if guid not in self._names():
            raise ValueError(f"power plan {guid} is not installed on this PC")
        return guid

    def describe(self, before: Any, after: Any) -> str:
        names = self._names()
        return f"{self.title}: {names.get(before) or before} -> {names.get(after) or after}"

    def _names(self) -> dict[str, str]:
        return {_guid(g): name for g, name in self.power.list_schemes()}
