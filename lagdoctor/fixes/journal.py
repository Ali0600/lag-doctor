"""The fix journal: every change lag-doctor makes, with the value it replaced.

Write-ahead: an entry is saved as "pending" with the original value BEFORE the
change is made, and only marked "applied" once a readback confirms it. So if
the tool dies mid-change, the next start finds the pending entry and can put
things back (see Fix.heal). Writes are atomic (temp file + fsync + replace)
and guarded by a lock file, so two lag-doctor windows cannot interleave.

A journal that cannot be parsed is never silently replaced with an empty one:
that would forget how to undo the user's changes. It stays where it is, a copy
of its bytes is saved next to it (once: the same bytes are never copied twice),
and the load fails loudly, so nothing is changed or undone until it is readable.
Keys a newer lag-doctor added to an entry are kept as they are, not refused.
"""
from __future__ import annotations

import hashlib
import json
import msvcrt
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal

from lagdoctor import paths

EntryState = Literal["pending", "applied", "failed", "reverted", "undone", "conflict"]
JOURNAL_SCHEMA = "lagdoctor.journal/1"


class JournalCorrupt(RuntimeError):
    """The journal file could not be read; a copy of its bytes is at `parked`."""

    def __init__(self, message: str, parked: Path, path: Path | None = None, reason: str = "") -> None:
        super().__init__(message)
        self.parked = parked
        self.path = path
        self.reason = reason

    def plain(self) -> str:
        """What to tell the user, in one paragraph."""
        where = f" ({self.path})" if self.path else ""
        return (f"lag-doctor cannot read its fix journal{where}: {self.reason}. It was left where it is and "
                f"copied to {self.parked}. Fixes and undo are paused until the journal can be read again, so "
                f"nothing was changed. If a newer lag-doctor wrote it, undo with that version.")


@dataclass
class JournalEntry:
    id: str
    fix_id: str
    target: str                 # what was changed, in words a person can find
    params: dict[str, Any]      # what the fix needs to redo/undo it (exe path, scheme GUID ...)
    before: Any                 # the original value (ABSENT_JSON if it did not exist)
    after: Any                  # the value lag-doctor set
    state: EntryState
    created_at: str
    updated_at: str
    note: str = ""
    session_only: bool = False  # the change disappears by itself (e.g. a process priority)
    history: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)   # keys from a newer lag-doctor, written back as they were


_KNOWN = frozenset(f.name for f in fields(JournalEntry)) - {"extra"}


def _load_entry(raw: Any) -> JournalEntry:
    """One entry from its JSON object. Unknown keys go to `extra`; a missing field is still an error."""
    if not isinstance(raw, dict):
        raise ValueError(f"an entry is not an object: {raw!r:.80}")
    known = {k: v for k, v in raw.items() if k in _KNOWN}
    return JournalEntry(**known, extra={k: v for k, v in raw.items() if k not in _KNOWN})


def _dump_entry(e: JournalEntry) -> dict[str, Any]:
    d = asdict(e)
    extra = d.pop("extra")
    return {**d, **{k: v for k, v in extra.items() if k not in d}}     # a known field always wins


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Journal:
    def __init__(self, path: Path | None = None, lock_timeout: float = 10.0) -> None:
        self.path = Path(path) if path else paths.journal_path()
        self._lock_path = self.path.with_name(self.path.name + ".lock")
        self._lock_timeout = lock_timeout
        self._top_extra: dict[str, Any] = {}     # top-level keys a newer lag-doctor wrote, kept on rewrite

    # ------------------------------------------------------------ storage --
    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            deadline = time.monotonic() + self._lock_timeout
            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"another lag-doctor holds {self._lock_path}") from None
                    time.sleep(0.05)
            try:
                yield
            finally:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(fd)

    def _read(self) -> list[JournalEntry]:
        self._top_extra = {}
        if not self.path.exists():
            return []
        raw = self.path.read_bytes()
        try:
            data = json.loads(raw.decode("utf-8"))
            if data.get("schema") != JOURNAL_SCHEMA:
                raise ValueError(f"unexpected schema {data.get('schema')!r}")
            if not isinstance(data["entries"], list):
                raise ValueError("'entries' is not a list")
            entries = [_load_entry(e) for e in data["entries"]]
        except Exception as exc:  # noqa: BLE001 - any parse failure must park the bytes
            parked = self._park(raw)
            raise JournalCorrupt(
                f"the fix journal {self.path} could not be read ({exc}); it was left in place and copied to "
                f"{parked}. Nothing was changed.", parked, self.path, str(exc)) from exc
        self._top_extra = {k: v for k, v in data.items() if k not in ("schema", "entries")}
        return entries

    def _park(self, raw: bytes) -> Path:
        """Copy unreadable bytes next to the journal, once: the copy is named by their hash, so reading the
        same bad file again (every command does) finds the copy instead of adding another."""
        digest = hashlib.sha256(raw).hexdigest()[:16]
        parked = self.path.with_name(f"{self.path.stem}.corrupt-{digest}.json")
        if parked.is_file() and parked.read_bytes() == raw:
            return parked
        if parked.exists():                      # the same name with other bytes: never overwrite it
            parked = self.path.with_name(f"{self.path.stem}.corrupt-{digest}-{uuid.uuid4().hex[:6]}.json")
        tmp = parked.with_name(parked.name + ".tmp")
        tmp.write_bytes(raw)
        os.replace(tmp, parked)                  # whole or absent: a half copy never sits under the hash name
        return parked

    def _write(self, entries: list[JournalEntry]) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        payload = json.dumps({**self._top_extra, "schema": JOURNAL_SCHEMA,
                              "entries": [_dump_entry(e) for e in entries]}, indent=1)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    # ------------------------------------------------------------- public --
    def entries(self) -> list[JournalEntry]:
        with self._locked():
            return self._read()

    def begin(self, fix_id: str, target: str, params: dict[str, Any], before: Any, after: Any,
              session_only: bool = False) -> JournalEntry:
        """Record the original value durably. Call BEFORE making the change."""
        now = _now_iso()
        entry = JournalEntry(id=uuid.uuid4().hex[:12], fix_id=fix_id, target=target, params=params,
                             before=before, after=after, state="pending", created_at=now, updated_at=now,
                             session_only=session_only, history=[f"{now} pending"])
        with self._locked():
            entries = self._read()
            entries.append(entry)
            self._write(entries)
        return entry

    def set_state(self, entry_id: str, state: EntryState, note: str = "") -> JournalEntry:
        with self._locked():
            entries = self._read()
            for e in entries:
                if e.id == entry_id:
                    now = _now_iso()
                    e.state = state
                    e.updated_at = now
                    if note:
                        e.note = note
                    e.history.append(f"{now} {state}" + (f": {note}" if note else ""))
                    self._write(entries)
                    return e
        raise KeyError(f"no journal entry {entry_id}")

    def get(self, entry_id: str) -> JournalEntry:
        for e in self.entries():
            if e.id == entry_id:
                return e
        raise KeyError(f"no journal entry {entry_id}")

    def pending(self) -> list[JournalEntry]:
        """Entries left mid-change by a crash: their outcome is unknown until healed."""
        return [e for e in self.entries() if e.state in ("pending", "conflict")]

    def undoable(self) -> list[JournalEntry]:
        """Applied changes that have not been undone, newest first (undo in that order)."""
        return [e for e in reversed(self.entries()) if e.state == "applied"]

    def outstanding(self) -> list[JournalEntry]:
        """What `undo` offers, newest first: applied changes, and conflicts (a value that is neither the
        original nor lag-doctor's, e.g. after a crash between two writes), which undo restores with --force."""
        return [e for e in reversed(self.entries()) if e.state in ("applied", "conflict")]
