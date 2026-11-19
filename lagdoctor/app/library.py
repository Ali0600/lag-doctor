"""The sessions on disk, as the app lists them: one Entry per session folder, grouped into games.

A folder's summary.json (report/summary.py, written at analysis time) says what it holds without loading the capture.
A folder saved before summaries existed is read from its name, <yyyymmdd-hhmmss>-<game>, as capture.session.
session_dir made it. Only recordings, folders holding a capture, are games' sessions: a check-up has none, and an
`analyze` folder holds a report of a capture kept elsewhere.

The page names a session by its folder's name and a game by an opaque id; both resolve only against this listing.

Play (M6) adds the games Steam and the Epic launchers list as installed (lagdoctor/stores), each an Installed here.
A recording shows under an installed game when the program it ran (summary.json's exe) is under that game's install
folder, in any of its forms (target.folder_forms: the folder, and its real path through a junction); a folder that
holds another listed game's, or a Program Files folder, takes none (store files are writable by any local account). An older recording, with no summary, follows the newest recording of its
game that joined one. The installed game's id is its store's, not its exe's.
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


STORE_NAMES = {"steam": "Steam", "epic": "Epic Games"}


@dataclass(frozen=True)
class Installed:
    """An installed store game as the app lists it: what its store's files say (stores.Game: its store, id, title,
    install folder and the link Play opens) and the forms of its folder that the programs of its recordings ran from.
    Nothing here reaches the page but its title, its store's name and its id."""
    game: Any                         # stores.Game
    forms: tuple[str, ...]

    @property
    def key(self) -> str:
        return f"store:{self.game.store}:{self.game.id}"

    @property
    def game_id(self) -> str:
        return game_id(self.key)

    @property
    def store_name(self) -> str:
        return STORE_NAMES.get(self.game.store, "")


def installed_games(log: Any = None) -> list[Any]:
    """The games Steam and the Epic launchers list as installed (stores/steam.py and epic.py, read from the stores'
    own files without starting either), each once; what could not be read is logged. The app's tests never come here:
    they give App made-up games (tests/app_demo.py), so no test lists the PC's own library."""
    from lagdoctor.stores import epic, steam
    games, seen = [], set()
    for reader in (steam.read, epic.read):
        try:
            lib = reader()
        except Exception as exc:  # noqa: BLE001 - one store's failure never hides the other's games
            if log is not None:
                log(f"reading a store's library failed: {type(exc).__name__}: {exc}")
            continue
        for problem in lib.problems:
            if log is not None:
                log(f"store library: {problem}")
        for g in lib.games:
            if (g.store, g.id) not in seen:
                seen.add((g.store, g.id))
                games.append(g)
    return games


def installed(games: list[Any], forms_of: Any = None) -> list[Installed]:
    """Each store game with the forms of its folder (`forms_of(folder)`; target.folder_forms by default, or the folder
    alone when they cannot be worked out: a recording joins only by the folder as its store wrote it then)."""
    if forms_of is None:
        from lagdoctor.capture import target
        forms_of = target.folder_forms
    out = []
    for g in games:
        try:
            forms = tuple(forms_of(g.folder))
        except (OSError, ValueError):
            forms = (g.folder,)
        out.append(Installed(game=g, forms=forms or (g.folder,)))
    return out


def shared_roots() -> list[str]:
    """Folders that hold many programs, never one game: the Program Files folders, as this account's environment names
    them."""
    import os
    names = ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432")
    return [os.environ[n] for n in names if os.environ.get(n)]


def _broad(games: list[Installed], roots: list[str]) -> set[str]:
    """The keys of installed games whose folder holds another listed game's folder, or is a shared root: a store file
    that names one (any local account can write them) must not take every recording made under it."""
    from lagdoctor.capture import target
    folded = {g.key: [target.fold_path(f) for f in g.forms] for g in games}
    shared = {target.fold_path(r) for r in roots}
    out = set()
    for g in games:
        mine = folded[g.key]
        if any(f in shared for f in mine):
            out.add(g.key)
            continue
        for other in games:
            if other.key != g.key and any(o.startswith(f + "\\") for f in mine for o in folded[other.key]):
                out.add(g.key)
                break
    return out


def _holder(entry: Entry, games: list[Installed]) -> Installed | None:
    """The installed game a recording ran from: the one whose folder, in one of its forms, holds its exe; None when
    none does or it names no exe (an older recording). Given games with no broad folder (_broad), two that hold it share
    one folder (two store entries for one install): the first by key is taken."""
    from lagdoctor.capture import target
    exe = (entry.summary or {}).get("exe")
    if not isinstance(exe, str):
        return None
    holders = [g for g in games if any(target.in_folder(exe, form) for form in g.forms)]
    return min(holders, key=lambda g: g.key) if holders else None


def joined(entries: list[Entry], games: list[Installed], roots: list[str] | None = None) -> dict[str, Installed]:
    """{session id: the installed game the recording shows under}. A recording that names its exe joins the installed
    game whose folder holds it (_holder), never one with a broad folder (_broad; `roots`: shared_roots() when None); one that names
    none (an older recording, with no summary) follows the newest recording of its game (its exe's stem) that joined
    one. Any other recording stays with its recorded game."""
    usable = [g for g in games if g.key not in _broad(games, shared_roots() if roots is None else roots)]
    out: dict[str, Installed] = {}
    newest: dict[str, Installed] = {}
    for e in entries:                                     # newest first
        if not e.is_recording:
            continue
        found = _holder(e, usable)
        if found is not None:
            out[e.id] = found
            newest.setdefault(e.game_key, found)
    for e in entries:
        if (e.is_recording and e.id not in out and not isinstance((e.summary or {}).get("exe"), str)
                and e.game_key in newest):
            out[e.id] = newest[e.game_key]
    return out


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
