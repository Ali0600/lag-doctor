"""Stand-in fixes for lag-doctor's window (App(fixes=)): what a test's App and the window's harness apply instead of the
catalogue's real fixes, so that no test changes this PC's settings. A stand-in fix of any id keeps its value in a dict
of the test's own, under the fix's id; its journal is a real one, under the test's home (fixes/journal.py)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from lagdoctor import paths
from lagdoctor.fixes import flow
from lagdoctor.fixes.base import Fix, is_absent, is_gone
from lagdoctor.fixes.journal import Journal

BEFORE = "as it was"
AFTER = "as lag-doctor sets it"


class StandIn(Fix):
    """A fix of any id that changes `store[fix_id]` (BEFORE until it is set) to AFTER. `store[("refuse", fix_id)]`, an
    exception, is what its read raises; `store[("write", fix_id)]` what its write raises."""

    def __init__(self, fix_id: str, store: dict, session_only: bool = False, needs_restart: str | None = None) -> None:
        self.id = fix_id
        self.title = f"The stand-in for {fix_id}"
        self.store = store
        self.session_only = session_only
        self.needs_restart = needs_restart

    def target(self, params: dict[str, Any]) -> str:
        return f"the stand-in setting of {self.id}"

    def read(self, params: dict[str, Any]) -> Any:
        refusal = self.store.get(("refuse", self.id))
        if refusal is not None:
            raise refusal
        return self.store.get(self.id, BEFORE)

    def write(self, params: dict[str, Any], value: Any) -> None:
        failure = self.store.get(("write", self.id))
        if failure is not None:
            raise failure
        self.store[self.id] = value
        self.store.setdefault("writes", []).append((self.id, value))

    def wanted(self, params: dict[str, Any], current: Any) -> Any:
        return AFTER

    def describe(self, before: Any, after: Any) -> str:
        def shown(v: Any) -> str:
            if isinstance(v, str):
                return {BEFORE: "off", AFTER: "on"}.get(v, v)
            return "not set" if is_absent(v) else "gone" if is_gone(v) else str(v)
        return f"{self.title}: {shown(before)} -> {shown(after)}"

    def plan(self, params: dict[str, Any]):
        plan = super().plan(params)
        plan.notes.append("A stand-in's note, shown before anything changes.")
        return plan


class StandInFixes:
    """App(fixes=)'s stand-in for fixes/flow.Fixes: `ids` the fixes it applies from a report (every real id but the
    sweep's by default), stand-ins of each (and of the sweep's own, which only the journal names), the home's journal
    with a short lock wait, and `elevated` as the window would read it (False, True, or None for cannot tell)."""

    def __init__(self, home: Path, store: dict | None = None, ids: frozenset[str] | None = None,
                 elevated: bool | None = False, session_only: frozenset[str] = frozenset(),
                 restart: dict[str, str] | None = None) -> None:
        self.home = Path(home)
        self.store = {} if store is None else store
        self.ids = flow.known() if ids is None else frozenset(ids)
        self.is_elevated = elevated
        self.session_only = session_only
        self.restart = restart or {}
        self.asked: list[str] = []              # the ids fix() was asked for, in order

    def known(self) -> frozenset[str]:
        return self.ids

    def _builds(self, fix_id: str) -> bool:
        return fix_id in self.ids or fix_id in flow.NEVER_OFFERED

    def fix(self, fix_id: str) -> Fix:
        self.asked.append(fix_id)
        if not self._builds(fix_id):
            raise KeyError(f"unknown fix {fix_id!r}")
        return StandIn(fix_id, self.store, fix_id in self.session_only, self.restart.get(fix_id))

    def title(self, fix_id: str) -> str | None:
        return f"The stand-in for {fix_id}" if self._builds(fix_id) else None

    def journal(self) -> Journal:
        return Journal(self.home / paths.JOURNAL_NAME, lock_timeout=0.3)

    def elevated(self) -> bool | None:
        return self.is_elevated

