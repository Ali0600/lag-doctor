"""The Fix base class: plan, then apply (journal first, verify by readback), undo, heal.

A concrete fix supplies four things: what it targets, how to read the current
value, how to write a value, and which value it wants. This class supplies the
safety around them, identically for every fix:

- apply refuses if the live value moved since the plan was shown to the user;
- the original is journaled durably before anything is written;
- success means a readback equals the wanted value; otherwise the original is
  put back and the entry says so;
- undo restores only if the live value is still ours, unless forced, so a
  setting the user changed by hand afterwards is never clobbered;
- heal settles entries a crash left "pending", from the live value.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from lagdoctor.fixes.journal import Journal, JournalEntry

ABSENT: dict[str, bool] = {"__absent__": True}   # "this value did not exist" (JSON-safe)
GONE: dict[str, bool] = {"__gone__": True}       # "the thing it applied to no longer exists" (e.g. process exited)


def is_absent(value: Any) -> bool:
    return isinstance(value, dict) and value.get("__absent__") is True


def is_gone(value: Any) -> bool:
    return isinstance(value, dict) and value.get("__gone__") is True


@dataclass
class FixPlan:
    fix_id: str
    target: str
    params: dict[str, Any]
    before: Any
    after: Any
    summary: str
    needs_admin: bool = False
    needs_restart: str | None = None
    session_only: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def noop(self) -> bool:
        return self.before == self.after


@dataclass
class FixResult:
    ok: bool
    message: str
    entry_id: str | None = None
    state: str = ""


class Fix(ABC):
    id: str = ""
    title: str = ""
    needs_admin: bool = False
    needs_restart: str | None = None
    session_only: bool = False

    # ---- what each fix implements -------------------------------------
    @abstractmethod
    def target(self, params: dict[str, Any]) -> str:
        """Where the change happens, in words a person can find (a registry path, a power plan ...)."""

    @abstractmethod
    def read(self, params: dict[str, Any]) -> Any:
        """The current value: ABSENT if it does not exist, GONE if its subject no longer exists."""

    @abstractmethod
    def write(self, params: dict[str, Any], value: Any) -> None:
        """Set the value; writing ABSENT deletes it."""

    @abstractmethod
    def wanted(self, params: dict[str, Any], current: Any) -> Any:
        """The value this fix would set, given the current one."""

    def readback(self, params: dict[str, Any]) -> Any:
        """Confirm a write. Override with an independent readout when one exists."""
        return self.read(params)

    def describe(self, before: Any, after: Any) -> str:
        return f"{self.title}: {before!r} -> {after!r}"

    # ---- the safety, shared by every fix ------------------------------
    def plan(self, params: dict[str, Any]) -> FixPlan:
        before = self.read(params)
        after = self.wanted(params, before)
        return FixPlan(fix_id=self.id, target=self.target(params), params=dict(params), before=before,
                       after=after, summary=self.describe(before, after), needs_admin=self.needs_admin,
                       needs_restart=self.needs_restart, session_only=self.session_only)

    def apply(self, plan: FixPlan, journal: Journal) -> FixResult:
        if plan.noop:
            return FixResult(True, f"{plan.target} is already set; nothing changed", state="noop")
        if is_gone(plan.before):
            return FixResult(False, f"{plan.target} no longer exists; nothing changed", state="gone")
        live = self._safe_read(plan.params)
        if live != plan.before:
            return FixResult(False, f"{plan.target} changed since the plan was made (now {live!r}); "
                                    f"nothing was changed", state="stale")
        entry = journal.begin(plan.fix_id, plan.target, plan.params, plan.before, plan.after,
                              session_only=plan.session_only)
        try:
            self.write(plan.params, plan.after)
        except Exception as exc:  # noqa: BLE001 - report every write failure, then settle the entry
            return self._settle_bad_write(plan, entry, journal, f"the change failed: {exc}")
        got = self._safe_read_back(plan.params)
        if got != plan.after:
            return self._settle_bad_write(plan, entry, journal,
                                          f"the change did not take (read back {got!r}, wanted {plan.after!r})")
        journal.set_state(entry.id, "applied")
        return FixResult(True, plan.summary, entry.id, "applied")

    def undo(self, entry: JournalEntry, journal: Journal, force: bool = False) -> FixResult:
        live = self._safe_read(entry.params)
        if live == entry.before:
            journal.set_state(entry.id, "undone", "it was already back to the original")
            return FixResult(True, f"{entry.target} is already back to the original", entry.id, "undone")
        if is_gone(live):
            journal.set_state(entry.id, "undone", "its subject no longer exists")
            return FixResult(True, f"{entry.target} no longer exists; nothing to undo", entry.id, "undone")
        if live != entry.after and not force:
            return FixResult(False, f"{entry.target} was changed after lag-doctor set it (now {live!r}), so it "
                                    f"was left alone. Undo with --force to restore {entry.before!r} anyway.",
                             entry.id, "conflict")
        try:
            self.write(entry.params, entry.before)
        except Exception as exc:  # noqa: BLE001
            return FixResult(False, f"undo of {entry.target} failed: {exc}", entry.id, "applied")
        got = self._safe_read_back(entry.params)
        if got != entry.before:
            journal.set_state(entry.id, "applied", f"undo did not take (read back {got!r})")
            return FixResult(False, f"undo of {entry.target} did not take (read back {got!r})", entry.id, "applied")
        journal.set_state(entry.id, "undone")
        return FixResult(True, f"restored {entry.target} to {entry.before!r}", entry.id, "undone")

    def heal(self, entry: JournalEntry, journal: Journal) -> FixResult:
        """Settle an entry a crash left 'pending', by looking at the live value."""
        live = self._safe_read(entry.params)
        if live == entry.before:
            journal.set_state(entry.id, "failed", "never applied (found at start-up)")
            return FixResult(True, f"{entry.target}: the interrupted change never happened", entry.id, "failed")
        if live == entry.after:
            journal.set_state(entry.id, "applied", "confirmed at start-up")
            return FixResult(True, f"{entry.target}: the interrupted change did happen; it can be undone",
                             entry.id, "applied")
        if is_gone(live):
            journal.set_state(entry.id, "undone", "its subject no longer exists (found at start-up)")
            return FixResult(True, f"{entry.target} no longer exists", entry.id, "undone")
        journal.set_state(entry.id, "conflict", f"live value {live!r} is neither the original nor ours")
        return FixResult(False, f"{entry.target} is now {live!r}, neither the original {entry.before!r} nor "
                                f"lag-doctor's {entry.after!r}; left alone", entry.id, "conflict")

    # ---- helpers --------------------------------------------------------
    def _safe_read(self, params: dict[str, Any]) -> Any:
        try:
            return self.read(params)
        except Exception as exc:  # noqa: BLE001
            return {"__unreadable__": str(exc)}

    def _safe_read_back(self, params: dict[str, Any]) -> Any:
        try:
            return self.readback(params)
        except Exception as exc:  # noqa: BLE001
            return {"__unreadable__": str(exc)}

    def _settle_bad_write(self, plan: FixPlan, entry: JournalEntry, journal: Journal, why: str) -> FixResult:
        """A write failed or did not take: put the original back if anything moved, and say what happened."""
        if self._safe_read(plan.params) == plan.before:
            journal.set_state(entry.id, "failed", why)
            return FixResult(False, f"{plan.target}: {why}; nothing was changed", entry.id, "failed")
        try:
            self.write(plan.params, plan.before)
            restored = self._safe_read_back(plan.params) == plan.before
        except Exception:  # noqa: BLE001
            restored = False
        if restored:
            journal.set_state(entry.id, "reverted", why)
            return FixResult(False, f"{plan.target}: {why}; the original was put back", entry.id, "reverted")
        journal.set_state(entry.id, "conflict", why + "; restoring the original also failed")
        return FixResult(False, f"{plan.target}: {why}, and restoring the original failed. It was "
                                f"{plan.before!r}; see the journal at {journal.path}", entry.id, "conflict")
