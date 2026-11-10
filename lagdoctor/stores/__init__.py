"""The stores Play starts games through (docs/LAUNCHER-PLAN.md, M6): their links (launch.py), and the games each has
installed (steam.py, epic.py), read from the stores' own files without starting them.

Those files are untrusted (research note desktop-app.md, section 15): every local account can write Steam's folder,
its registry keys and the legacy Epic launcher's manifests, and any program the player runs can write the Epic
Preview's journal, which the Preview then adopts. So every reader takes only what its store writes, caps every size,
checks every field against a grammar, and skips what fails, naming it in Library.problems in plain words: an entry
is never guessed at, and one bad entry never hides the others in its file. A file that cannot be read as a whole (a
library list Steam would not write, a journal without its header) is named, and none of its entries is taken.

Every file and folder the readers look at goes through this module's scandir, stat, is_folder, exists and read_text,
which ask target.local_problem first: nothing is opened on a network drive, or through a junction or link that leads
to one, and nothing kept online only (tests/test_stores.py holds every reader module to that).
"""
from __future__ import annotations

import os
import stat as _stat_mode
from dataclasses import dataclass, field
from pathlib import Path

from lagdoctor import model
from lagdoctor.capture import target
from lagdoctor.capture.target import NotLocal

MAX_TITLE_CHARS = 200
UNNAMED = "Unnamed game"           # a game whose store and folder both give no character to show
MAX_PROBLEMS = 20                  # named per source (Steam; each of Epic's two launchers); the rest are counted
MAX_PROBLEM_CHARS = 1000           # one problem: room for two paths and a folder of 260 characters each
MAX_SHOWN_CHARS = 60               # of a refused value, as a problem quotes it


@dataclass(frozen=True)
class Game:
    """One installed game, as Play needs it. Nothing here reaches the page but through the app's own ids."""
    store: str          # "steam" or "epic"
    id: str             # the store's id: Steam's app id; Epic's "<namespace>:<catalog item id>:<app name>"
    title: str          # the store's name for it, else its folder's name, each cleaned for display (title)
    folder: str         # its install folder, as target.check_folder gives it; it existed when read
    link: str           # the link Play opens (launch.steam_link or launch.epic_link)


@dataclass
class Library:
    """What a store's files hold: its installed games, and what could not be read (plain words, one per item)."""
    games: list[Game] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- problems, in plain words ---

class Problems:
    """One source's problems (Steam, or one of Epic's two launchers) as Library.problems lists them: the first
    MAX_PROBLEMS, each one line (plain), then one line that counts the rest."""

    def __init__(self, source: str) -> None:
        self.source = source
        self.named: list[str] = []
        self.more = 0

    def add(self, text: str) -> None:
        if len(self.named) < MAX_PROBLEMS:
            self.named.append(plain(text))
        else:
            self.more += 1

    def lines(self) -> list[str]:
        if not self.more:
            return list(self.named)
        many = "problem" if self.more == 1 else "problems"
        return [*self.named, f"{self.more} more {many} with the files of {self.source}, not listed here"]


def plain(text: str) -> str:
    """A problem as one line of at most MAX_PROBLEM_CHARS: each character that is not printable (a line break, a mark
    that reorders text) becomes a ?, which no Windows path holds."""
    out = "".join(ch if ch.isprintable() else "?" for ch in text)
    return out if len(out) <= MAX_PROBLEM_CHARS else out[:MAX_PROBLEM_CHARS - 3] + "..."


def shown(value: object) -> str:
    """A refused value as a problem quotes it: "missing" for none (JSON's null), "true" and "false" as JSON writes
    them, "a list", "a block" for a block of keys (a JSON object, a KeyValues block), else its repr, cut to
    MAX_SHOWN_CHARS."""
    if value is None:
        return "missing"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "a list"
    if isinstance(value, dict):
        return "a block"
    if isinstance(value, str):
        value = value[:MAX_SHOWN_CHARS]         # the repr of a long text is never built whole
    text = repr(value)
    return text if len(text) <= MAX_SHOWN_CHARS else text[:MAX_SHOWN_CHARS - 3] + "..."


# ---------------------------------------------------------------- titles ---

def name(value: object) -> str | None:
    """A store's name for a game, cleaned for display (model.clean_note: one line, every character one that can be
    shown) and cut to MAX_TITLE_CHARS; None when it has no character to show, or is no text."""
    return model.clean_note(value, MAX_TITLE_CHARS)


def title(value: object, fallback: str) -> str:
    """A game's title: the store's name for it, else `fallback` (its folder's name), each cleaned by name; UNNAMED
    when neither has a character to show."""
    return name(value) or name(fallback) or UNNAMED


# ---------------------------------------------------------------- the readers' file access ---

def _open(path: Path):
    """open(path, "rb"), looked up when called (the tests replace this, never the built-in open)."""
    return open(path, "rb")


def _stat(path: str) -> os.stat_result:
    """os.stat, looked up when called (the tests replace this, never os.stat itself)."""
    return os.stat(path)


def _scandir(path: str):
    """os.scandir, looked up when called (the tests replace this, never os.scandir itself)."""
    return os.scandir(path)


def failure(exc: OSError, path: object, what: str = "looked at") -> str:
    """An OSError from this module's file access, as a problem's words: NotLocal's own reason, else "<path> could not
    be <what> (Windows' own words)"."""
    if isinstance(exc, NotLocal):
        return exc.strerror
    return f"{path} could not be {what} ({exc.strerror or exc})"


def _local(path: str | os.PathLike) -> str:
    """`path` as text, once target.local_problem has no objection to it; NotLocal (an OSError) says why when it has,
    for anything that is no path at all (None, say) too."""
    text = os.fspath(path) if isinstance(path, os.PathLike) else path
    why = target.local_problem(text)
    if why is not None:
        raise NotLocal(0, why)
    return text


def scandir(path: str | os.PathLike):
    """os.scandir of a folder on the local disks; NotLocal (an OSError) when the path leads off them."""
    return _scandir(_local(path))


def stat(path: str | os.PathLike) -> os.stat_result:
    """os.stat of a file or folder on the local disks; NotLocal (an OSError) when the path leads off them."""
    return _stat(_local(path))


def is_folder(path: str | os.PathLike) -> bool:
    """True when `path` is a folder on the local disks, False when nothing is there; OSError (NotLocal among them)
    when it cannot be looked at."""
    try:
        return _stat_mode.S_ISDIR(stat(path).st_mode)
    except FileNotFoundError:
        return False


def exists(path: str | os.PathLike) -> bool:
    """True when something is at `path` on the local disks, False when nothing is; OSError (NotLocal among them)
    when it cannot be looked at."""
    try:
        stat(path)
    except FileNotFoundError:
        return False
    return True


def read_text(path: Path, max_bytes: int) -> str:
    """A store's file as text. ValueError, in plain words, when its path leads off the local disks or the file is
    kept online only (target.local_problem, which looks at the file itself too: reading it would download it), when
    it is larger than `max_bytes`, is not UTF-8, or cannot be read."""
    try:
        st = stat(path)
    except NotLocal as exc:
        raise ValueError(exc.strerror) from exc
    except OSError as exc:
        raise ValueError(f"{path} could not be read ({exc.strerror or exc})") from exc
    if st.st_size > max_bytes:
        raise ValueError(f"{path} is larger than {max_bytes} bytes, so it was not read")
    try:
        with _open(path) as f:
            data = f.read(max_bytes + 1)            # one byte past the cap: a file that grew since the stat shows
    except OSError as exc:
        raise ValueError(f"{path} could not be read ({exc.strerror or exc})") from exc
    if len(data) > max_bytes:
        raise ValueError(f"{path} is larger than {max_bytes} bytes, so it was not read")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path} is not UTF-8 text") from exc
    return text[1:] if text.startswith(chr(0xFEFF)) else text
