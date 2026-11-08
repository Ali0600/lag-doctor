"""The sessions on disk, as the app lists them: one Entry per session folder, grouped into games.

A folder's summary.json (report/summary.py, written at analysis time) says what it holds without loading the capture.
A folder saved before summaries existed is read from its name, <yyyymmdd-hhmmss>-<game>, as capture.session.
session_dir made it. Only recordings, folders holding a capture, are games' sessions: a check-up has none, and an
`analyze` folder holds a report of a capture kept elsewhere.

The page names a session by its folder's name and a game by an opaque id; both resolve only against this listing.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from lagdoctor import paths
from lagdoctor.app import bridge
from lagdoctor.report import summary as summary_mod

CAPTURE_NAME = "capture.ldcap.json.gz"        # cli.CAPTURE_NAME; the app never imports cli
DIAGNOSIS_NAME = "diagnosis.json"
REPORT_NAME = "report.html"
ANALYZED_FROM = "analyzed-capture.txt"
LAST_SESSION = "last-session.txt"
MAX_SESSIONS = 5000                           # folders read at most, newest names first
MAX_DIAGNOSIS_BYTES = 64 << 20                # a diagnosis.json past this is not read
_FOLDER = re.compile(r"([0-9]{8})-([0-9]{6})-(.+?)(?:-([2-9]|[1-9][0-9]))?")


@dataclass
class Entry:
    id: str                         # the folder's name: what the page names it by
    folder: Path
    kind: str                       # "watch" (a recording), "analyze", "check-up" or "unknown"
    game: str                       # the game as a player knows it
    game_key: str                   # what one game's sessions share: its exe's stem, case-folded
    exe_name: str | None
    started: datetime | None
    summary: dict[str, Any] | None  # the checked summary.json, or None (an older session)

    @property
    def game_id(self) -> str:
        return game_id(self.game_key)

    @property
    def capture(self) -> Path:
        return self.folder / CAPTURE_NAME

    @property
    def is_recording(self) -> bool:
        return self.kind == "watch" and self.capture.is_file()


def game_id(key: str) -> str:
    """The id the page names a game by: no name or path reaches the page as an id."""
    return "g-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def parse_folder_name(name: str) -> tuple[datetime | None, str] | None:
    """(when the folder was made, the game's stem) from a session folder's name, or None when it is not one."""
    m = _FOLDER.fullmatch(name)
    if not m:
        return None
    try:
        when = datetime.strptime(m[1] + m[2], "%Y%m%d%H%M%S")
    except ValueError:
        when = None
    return when, m[3]


def _started(text: Any) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def read_entry(folder: Path) -> Entry | None:
    """One session folder as an Entry, or None when its name is not a session folder's."""
    parsed = parse_folder_name(folder.name)
    if parsed is None or not bridge.SESSION_ID.fullmatch(folder.name):
        return None
    when, stem = parsed
    s = summary_mod.read(folder)
    if s is not None:
        kind = s["kind"]
        exe_name = s.get("exe_name")
        key = paths.folder_stem(exe_name).casefold() if exe_name else stem.casefold()
        game = s.get("game") or stem
        started = _started(s.get("started_at")) or when
    else:
        if (folder / CAPTURE_NAME).is_file():
            kind = "watch"
        elif (folder / ANALYZED_FROM).is_file():
            kind = "analyze"
        elif stem == "check-up":
            kind = "check-up"
        else:
            kind = "unknown"
        exe_name, key, game, started = None, stem.casefold(), stem, when
    return Entry(id=folder.name, folder=folder, kind=kind, game=game, game_key=key, exe_name=exe_name,
                 started=started, summary=s)


def _sort_key(e: Entry) -> tuple:
    """Newest first by when it started (a summary's time carries its offset; a folder's name is local time), then by
    name."""
    try:
        started = e.started.timestamp() if e.started else 0.0
    except (OverflowError, OSError, ValueError):
        started = 0.0
    return (started, e.id)


def scan(base: Path) -> list[Entry]:
    """Every session folder under `base` (not deeper), newest first. A folder that cannot be read is left out."""
    try:
        children = [p for p in Path(base).iterdir() if p.is_dir()]
    except OSError:
        return []
    children.sort(key=lambda p: p.name, reverse=True)
    out = []
    for folder in children[:MAX_SESSIONS]:
        try:
            entry = read_entry(folder)
        except OSError:
            continue
        if entry is not None:
            out.append(entry)
    out.sort(key=_sort_key, reverse=True)
    return out


def last_session(home: Path, base: Path) -> str | None:
    """The id of the session `lagdoctor fix` acts on (last-session.txt), when it is a folder under `base`."""
    try:
        text = (Path(home) / LAST_SESSION).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not text:
        return None
    try:
        folder = Path(text).resolve()
        if folder.parent != Path(base).resolve():
            return None
    except (OSError, ValueError):
        return None
    return folder.name


def read_diagnosis(entry: Entry) -> dict[str, Any] | None:
    """The session's diagnosis.json as saved, or None when there is none or it cannot be read."""
    path = entry.folder / DIAGNOSIS_NAME
    try:
        if path.stat().st_size > MAX_DIAGNOSIS_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        return None
    return data if isinstance(data, dict) else None
