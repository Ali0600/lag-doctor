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
- a subject that is not there at undo keeps its entry (an unplugged display
  comes back with lag-doctor's value), unless the change was session-only and
  went away with it (a process that exited);
- heal settles entries a crash left "pending", from the live value.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from lagdoctor.fixes.journal import Journal, JournalEntry

ABSENT: dict[str, bool] = {"__absent__": True}   # "this value did not exist" (JSON-safe)
GONE: dict[str, bool] = {"__gone__": True}       # "the thing it applied to no longer exists" (e.g. process exited)
NOT_CONNECTED = "not connected; run undo again when it is"


def is_absent(value: Any) -> bool:
    return isinstance(value, dict) and value.get("__absent__") is True


def is_gone(value: Any) -> bool:
    return isinstance(value, dict) and value.get("__gone__") is True


def _unreadable(value: Any) -> str | None:
    return value.get("__unreadable__") if isinstance(value, dict) else None


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

    def also_ours(self, live: Any, before: Any, after: Any) -> bool:
        """True when the live value shows this fix's change in a form other than `after` (a refresh rate
        Windows stored for after a restart, while the screen still shows the original). No other form by
        default."""
        return False

    def ended(self, params: dict[str, Any]) -> bool:
        """For a session-only change: True once its subject is gone. This default reads the live value; a fix
        whose read opens something that must not be opened (a process) overrides it with a check that opens
        nothing, because start-up runs this on every journaled entry."""
        return is_gone(self._safe_read(params))

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
        """Restore the journaled original. Works on 'applied' and 'conflict' entries; a conflict (or a value
        the user changed since) is restored only with force, and every restore is read back."""
        live = self._safe_read(entry.params)
        if live == entry.before:
            journal.set_state(entry.id, "undone", "it was already back to the original")
            return FixResult(True, f"{entry.target} is already back to the original", entry.id, "undone")
        if is_gone(live):
            if self.session_only:                # the change went away with its subject (a process that exited)
                journal.set_state(entry.id, "undone", "its subject no longer exists")
                return FixResult(True, f"{entry.target} no longer exists; nothing to undo", entry.id, "undone")
            # Not there now (an unplugged display) but it may come back with lag-doctor's value: keep the entry.
            journal.set_state(entry.id, entry.state, NOT_CONNECTED)
            return FixResult(False, f"{entry.target} is not connected now, so it was left alone; run undo again "
                                    f"when it is connected", entry.id, entry.state)
        if not self._ours(live, entry.before, entry.after) and not force:
            why = _unreadable(live)
            if why is not None:
                return FixResult(False, f"{entry.target} could not be read ({why}), so it was left alone",
                                 entry.id, "conflict")
            return FixResult(False, f"{entry.target} was changed after lag-doctor set it (now {live!r}), so it "
                                    f"was left alone. Undo with --force to restore {entry.before!r} anyway.",
                             entry.id, "conflict")
        try:
            self.write(entry.params, entry.before)
        except Exception as exc:  # noqa: BLE001
            return FixResult(False, f"undo of {entry.target} failed: {exc}", entry.id, entry.state)
        got = self._safe_read_back(entry.params)
        if got != entry.before:
            journal.set_state(entry.id, entry.state, f"undo did not take (read back {got!r})")
            return FixResult(False, f"undo of {entry.target} did not take (read back {got!r})", entry.id,
                             entry.state)
        journal.set_state(entry.id, "undone")
        return FixResult(True, f"restored {entry.target} to {entry.before!r}", entry.id, "undone")

    def heal(self, entry: JournalEntry, journal: Journal) -> FixResult:
        """Settle an entry a crash left 'pending', by looking at the live value."""
        live = self._safe_read(entry.params)
        if live == entry.before:
            journal.set_state(entry.id, "failed", "never applied (found at start-up)")
            return FixResult(True, f"{entry.target}: the interrupted change never happened", entry.id, "failed")
        if self._ours(live, entry.before, entry.after):
            journal.set_state(entry.id, "applied", "confirmed at start-up")
            return FixResult(True, f"{entry.target}: the interrupted change did happen; it can be undone",
                             entry.id, "applied")
        if is_gone(live):
            if self.session_only:
                journal.set_state(entry.id, "undone", "its subject no longer exists (found at start-up)")
                return FixResult(True, f"{entry.target} no longer exists", entry.id, "undone")
            # Whether the change happened is unknown until it is connected again: keep it on the undo list.
            journal.set_state(entry.id, "applied", "not connected at start-up, so whether the change happened is "
                                                   "unknown; run undo when it is connected")
            return FixResult(False, f"{entry.target} is not connected, so it is not known whether the interrupted "
                                    f"change happened; it stays on the undo list (run undo when it is connected)",
                             entry.id, "applied")
        journal.set_state(entry.id, "conflict", f"live value {live!r} is neither the original nor ours")
        return FixResult(False, f"{entry.target} is now {live!r}, neither the original {entry.before!r} nor "
                                f"lag-doctor's {entry.after!r}; left alone. `lagdoctor undo` lists it, and "
                                f"`lagdoctor undo {entry.id} --force` puts the original back.", entry.id, "conflict")

    # ---- helpers --------------------------------------------------------
    def _ours(self, live: Any, before: Any, after: Any) -> bool:
        return live == after or self.also_ours(live, before, after)

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
        failure = ""
        try:
            self.write(plan.params, plan.before)
        except Exception as exc:  # noqa: BLE001 - the readback below decides whether the original is back
            failure = f" ({exc})"
        got = self._safe_read_back(plan.params)
        if got == plan.before:
            journal.set_state(entry.id, "reverted", why)
            return FixResult(False, f"{plan.target}: {why}; the original was put back", entry.id, "reverted")
        if self._ours(got, plan.before, plan.after):
            # The change is in place after all (a refresh rate stored for after a restart): keep it undoable.
            journal.set_state(entry.id, "applied", why + "; putting the original back failed" + failure)
            return FixResult(False, f"{plan.target}: {why}. Putting the original back failed{failure}, so "
                                    f"lag-doctor's change is in place; undo it with: lagdoctor undo {entry.id}",
                             entry.id, "applied")
        journal.set_state(entry.id, "conflict", why + "; restoring the original also failed" + failure)
        return FixResult(False, f"{plan.target}: {why}, and restoring the original failed{failure}. It was "
                                f"{plan.before!r}; see the journal at {journal.path}", entry.id, "conflict")
