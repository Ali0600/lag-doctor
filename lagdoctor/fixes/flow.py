"""The fix flow both of lag-doctor's front ends take: `lagdoctor fix` and `undo` in a terminal (cli.py), and Apply and
Undo in lag-doctor's window (app/main.py).

The safety around each change is the fix's own (fixes/base.py: the plan, the journal first, the readback, an undo
that leaves alone a value someone changed since). This module holds what the two front ends must agree on: which
offers a report may make, which errors a plan raises for a refusal, an apply or an undo whose journal another
lag-doctor holds (or a disk refuses) said as an outcome rather than raised, and the settling of the journal at a start.
Nothing here prints or asks: each front end words its own questions and answers.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from lagdoctor import paths
from lagdoctor.fixes import catalog
from lagdoctor.fixes.base import Fix, FixPlan, FixResult
from lagdoctor.fixes.journal import Journal, JournalCorrupt, JournalEntry
from lagdoctor.fixes.priority import Refused

# The settings sweep's own fix (fixes/game_setting.py): no report offers it, so neither front end applies it from one.
NEVER_OFFERED = frozenset({"game_setting"})
# What a plan raises for a refusal (a protected process, a display that is not connected, a value of another shape):
# said in words by each front end, never a traceback.
PLAN_ERRORS = (Refused, OSError, ValueError, LookupError, RuntimeError)
PENDING_GRACE_S = 60.0         # a pending entry younger than this may be a change another window is making now


def offers(diagnosis: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """(fix_id, title, params) for every automatic fix a report suggested, without duplicates. A report is a file any
    program of the player's can write, so an offer whose id is not text or whose params are not an object is none,
    and the sweep's own fix is never offered."""
    out, seen = [], set()
    findings = diagnosis.get("findings")
    for f in findings if isinstance(findings, list) else []:
        fixes = f.get("fixes") if isinstance(f, dict) else None
        for fx in fixes if isinstance(fixes, list) else []:
            if not isinstance(fx, dict):
                continue
            fid, params = fx.get("fix_id"), fx.get("params") or {}
            if not isinstance(fid, str) or not fid or fid in NEVER_OFFERED or not isinstance(params, dict):
                continue
            key = (fid, json.dumps(params, sort_keys=True))
            if key not in seen:
                seen.add(key)
                out.append((fid, fx.get("title") or fid, dict(params)))
    return out


def known() -> frozenset[str]:
    """The ids of the fixes this lag-doctor applies from a report: the catalogue's, less the sweep's own."""
    return frozenset(catalog.ids()) - NEVER_OFFERED


def why_not_offered(fix_id: Any, params: Any, ids: frozenset[str]) -> str | None:
    """Why lag-doctor's window does not apply an offer, or None when it does, from whichever recording made it.
    "unknown": no id among `ids` (known(): a report a newer lag-doctor wrote may name a fix this one does not know), or
    the sweep's own; "params": params that are not an object; "dated": params that name something as the PC was when
    the report was written (Fix.stands_alone: a display name may belong to another monitor by now, a background program
    may be the game), which the window applies from the last recording made only (applies)."""
    if not isinstance(fix_id, str) or fix_id not in ids or fix_id in NEVER_OFFERED:
        return "unknown"
    if not isinstance(params, dict):
        return "params"
    cls = catalog.find(fix_id)
    if cls is None:
        return "unknown"
    return None if cls.stands_alone(params) else "dated"


def offerable(fix_id: Any, params: Any, ids: frozenset[str]) -> bool:
    """Whether lag-doctor's window applies an offer from any recording (why_not_offered says no reason not to)."""
    return why_not_offered(fix_id, params, ids) is None


def applies(fix_id: Any, params: Any, ids: frozenset[str], is_last: bool) -> bool:
    """Whether lag-doctor's window applies an offer from the recording that made it: any recording's that stands
    alone, and the last recording's (`is_last`, the one `lagdoctor fix` acts on) that is "dated", as M7 applied them."""
    why = why_not_offered(fix_id, params, ids)
    return why is None or (why == "dated" and is_last)


def apply(fix: Fix, plan: FixPlan, journal: Journal) -> FixResult:
    """fix.apply(plan), with what the journal raises said as an outcome. "busy": the journal could not be read before
    anything was changed (another lag-doctor held it, or the disk refused), so nothing was. "unsure": the journal or
    the disk failed once the change may have begun; an entry written by then stays pending, and the next start settles
    it by the live value (settle). A journal that cannot be read raises JournalCorrupt, which each front end says."""
    try:
        journal.entries()
    except OSError as exc:                       # TimeoutError included: another lag-doctor holds the journal
        return FixResult(False, f"the fix journal could not be opened ({exc}); nothing was changed", state="busy")
    try:
        return fix.apply(plan, journal)
    except (OSError, KeyError) as exc:
        return FixResult(False, f"{plan.target}: the fix journal failed while the change was made ({exc}); whether it "
                                "was made is checked at the next start", state="unsure")


def undo(entry: JournalEntry, fix_for: Callable[[str], Fix], journal: Journal, force: bool = False) -> FixResult:
    """Undo one change the journal holds. A fix this lag-doctor does not know (a newer one journaled it) is left
    alone, why "unknown"; a journal another lag-doctor holds, or a disk error, is said as why "unsure", the entry kept
    as it was. A journal that cannot be read raises JournalCorrupt."""
    try:
        fix = fix_for(entry.fix_id)
    except KeyError:                             # journaled by a lag-doctor that knows more fixes
        return FixResult(False, f"{entry.target}: this lag-doctor does not know the fix '{entry.fix_id}'", entry.id,
                         entry.state, why="unknown")
    try:
        return fix.undo(entry, journal, force=force)
    except (OSError, KeyError) as exc:
        return FixResult(False, f"undo of {entry.target}: the fix journal failed ({exc}); undoing it again finds where "
                                "it stands", entry.id, entry.state, why="unsure")


def settle(say: Callable[[str], None], journal: Journal | None = None,
           fix_for: Callable[[str], Fix] = catalog.get) -> None:
    """Settle changes a crash left half-done, and session-only changes whose subject has ended. `say`: where its lines
    go (the terminal; lag-doctor's window for `watch --control`; the window's log for its own list of changes)."""
    try:
        journal = journal if journal is not None else Journal()
        try:
            entries = journal.entries()
        except JournalCorrupt as exc:
            say(f"The fix journal could not be read; it was copied to {exc.parked} and left in place. Fixes and "
                f"undo are paused until it can be read again (`lagdoctor undo` says more).")
            return
        sweep_left = []
        for e in entries:
            try:
                if e.state == "pending":
                    if e.fix_id == "game_setting" and _just_begun(e.created_at):
                        continue                  # a sweep in another window may be making this change right now
                    res = fix_for(e.fix_id).heal(e, journal)
                    say(f"Settled a change that was interrupted last time: {res.message}")
                    if e.fix_id == "game_setting" and res.state == "applied":
                        sweep_left.append(e)
                elif e.state in ("applied", "conflict") and e.fix_id == "game_setting":
                    sweep_left.append(e)
                elif e.state == "applied" and e.session_only:
                    # Decided without read(): a journaled PID may belong to the game by now, and read() opens it.
                    if fix_for(e.fix_id).ended(e.params):
                        journal.set_state(e.id, "undone", "it ended with its process")
            except JournalCorrupt:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad entry is left as it is, and the rest still settle
                say(f"(Could not check the change {e.id} ({e.fix_id}) in the fix journal: {exc})")
        if sweep_left:
            # A sweep that ended without putting its setting back (its window was closed, the PC restarted): the
            # player plays with that setting until something puts it back, so every start says so.
            values = ", ".join(f"{e.params.get('key')}={e.after} (was {e.before})" for e in sweep_left
                               if isinstance(e.params, dict))
            say(f"A settings sweep left a change in a game's settings file: {values}. `lagdoctor sweep --resume` puts "
                "it back (and goes on with the sweep), and so does `lagdoctor undo game_setting`.")
    except Exception as exc:  # noqa: BLE001 - a journal problem must not block the tool
        say(f"(Could not check the fix journal: {exc})")


def _just_begun(created_at: object) -> bool:
    """True for a journal entry created under PENDING_GRACE_S ago: healing it now could read the value before the other
    window's write lands, and settle a change that then happens as one that never did."""
    try:
        made = datetime.fromisoformat(str(created_at))
    except ValueError:
        return False
    if made.tzinfo is None:
        return False
    return (datetime.now(made.tzinfo) - made).total_seconds() < PENDING_GRACE_S


class Fixes:
    """The real fixes, as lag-doctor's window applies them (App(fixes=), which only its run() passes: an App a test
    builds applies stand-ins, and tests/conftest.py's _no_real_fixes fails a test that reaches `fix`). `fix(id)` is the
    catalogue's fix with its real backends, `journal()` the home's own fix journal, `elevated()` whether this process
    runs as administrator (None: it cannot tell), and `known()` the ids it applies from a report."""

    def __init__(self, home: Path) -> None:
        self.home = Path(home)

    def known(self) -> frozenset[str]:
        return known()

    def fix(self, fix_id: str) -> Fix:
        return catalog.get(fix_id)

    def title(self, fix_id: str) -> str | None:
        """The fix's title, or None for a fix this lag-doctor does not know (a newer one journaled it)."""
        cls = catalog.find(fix_id)
        return cls.title if cls is not None else None

    def journal(self) -> Journal:
        return Journal(self.home / paths.JOURNAL_NAME)

    def elevated(self) -> bool | None:
        try:
            from lagdoctor.winapi import elevation
            return bool(elevation.is_elevated())
        except Exception:  # noqa: BLE001 - cannot tell: the window changes nothing then
            return None
