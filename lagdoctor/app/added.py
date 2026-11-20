"""The games the player added by hand (screen 6, "Add a game"), kept in added-games.json in lag-doctor's folder.

Each is the program the player chose in Windows' Open dialog and the name it shows by (the program's own, or the one
the player gave it). Its folder is the program's: Record when it starts waits for the first program from that folder
that draws, as `watch --wait-folder` does, and the recordings made from it show under it (library.joined).

The file is lag-doctor's own, but any program the player runs can write it, so it is read as the store readers read
theirs: at most MAX_BYTES of it, at most MAX_GAMES entries, each one a program whose folder target.check_folder takes
and a name stores.name keeps, the rest left out and named. A file that could not be read whole, or held an entry left
out, is moved aside (UNREAD_NAME, with the time) before a new list replaces it, so no byte of it is lost to the
fallback; a new list is written to a file of its own and then moved over the old one.
"""
from __future__ import annotations

import json
import os
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from lagdoctor import stores
from lagdoctor.capture import target

NAME = "added-games.json"
UNREAD_NAME = "added-games.unread-{stamp}.json"
VERSION = 1
MAX_GAMES = 200
MAX_BYTES = 256 << 10
MAX_PATH_CHARS = target.MAX_FOLDER_CHARS + 1 + 255     # the folder, a separator and a file name
# Characters no program's path is taken with: controls, formatting marks (which reorder or hide text), a surrogate on
# its own (which no file can be written with as UTF-8), private and unassigned code points, line and paragraph breaks.
UNSHOWN = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
# Windows' reserved device names, which Windows 10 takes as the device whatever extension follows (COM1.exe is the
# serial port), so opening or looking at such a file would reach a device: never a program's name.
DEVICES = frozenset({"con", "prn", "aux", "nul"} | {f"{d}{n}" for d in ("com", "lpt")
                                                     for n in ("1", "2", "3", "4", "5", "6", "7", "8", "9",
                                                               "\u00b9", "\u00b2", "\u00b3")})


@dataclass(frozen=True)
class Kept:
    """One added game as the file keeps it: its program's path and the name it shows by."""
    exe: str
    name: str


def check_program(path: object) -> tuple[str, str]:
    """(the program's path as kept, its folder): a file ending in .exe whose folder target.check_folder takes, in
    Windows' own form. ValueError says why not, in plain words."""
    if not isinstance(path, str) or not 4 <= len(path) <= MAX_PATH_CHARS or \
            any(unicodedata.category(c) in UNSHOWN for c in path):
        raise ValueError("it is not the path of a program")
    cut = max(path.rfind("\\"), path.rfind("/"))
    folder, name = path[:cut], path[cut + 1:]
    if cut < 0 or len(name) <= 4 or not name.casefold().endswith(".exe") or any(c in '<>:"|?*' for c in name):
        raise ValueError(f"{path} is not a program (a file whose name ends in .exe)")
    if name.split(".", 1)[0].rstrip(" ").casefold() in DEVICES:
        raise ValueError(f"{path} names one of Windows' devices, not a program")
    if len(folder) == 2 and folder[1] == ":":
        folder += "\\"                                  # C:\game.exe: its folder is the drive, which check_folder names
    clean = target.check_folder(folder)
    return clean + "\\" + name, clean


def _entry(item: object) -> Kept:
    if not isinstance(item, dict) or set(item) != {"exe", "name"}:
        raise ValueError(f"an entry that is not a program and its name: {stores.shown(item)}")
    exe, _folder = check_program(item["exe"])
    name = stores.name(item["name"])
    if name is None:
        raise ValueError(f"an entry with no name to show: {stores.shown(item['name'])}")
    return Kept(exe=exe, name=name)


def load(home: Path) -> tuple[list[Kept], list[str]]:
    """The added games, in the order they were added, and what could not be read (plain words; none when the file is
    not there yet). The same program is taken once."""
    path = Path(home) / NAME
    problems = stores.Problems("the games added by hand")
    try:
        data = stores.read_bytes(path, MAX_BYTES)
    except ValueError as exc:
        try:
            there = stores.exists(path)            # never through a link onto a share: local_problem first
        except OSError:
            there = True
        return [], [str(exc)] if there else []
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return [], [f"{path} is not a list of games lag-doctor wrote"]
    if not isinstance(doc, dict) or doc.get("version") != VERSION or not isinstance(doc.get("games"), list):
        return [], [f"{path} is not a list of games lag-doctor wrote"]
    out, seen = [], set()
    for item in doc["games"]:
        if len(out) >= MAX_GAMES:
            problems.add(f"{path} lists more than {MAX_GAMES} games; the rest were left out")
            break
        try:
            kept = _entry(item)
        except ValueError as exc:
            problems.add(str(exc))
            continue
        if target.fold_path(kept.exe) in seen:
            problems.add(f"{kept.exe} is listed twice; it was taken once")
            continue
        seen.add(target.fold_path(kept.exe))
        out.append(kept)
    return out, problems.lines()


def _stamp() -> str:
    """The time an unread list is moved aside at, in its name (the tests replace this)."""
    return time.strftime("%Y%m%d-%H%M%S")


def save(home: Path, games: list[Kept], *, unread: bool) -> None:
    """Write the list in place of the old one: the new list is written beside it first, so a write that fails leaves
    the old one as it was. `unread`: the old file held something load left out (its problems), so its bytes are then
    moved aside, never overwritten. OSError, in plain words, for a home off the local disks (where load could never
    read the list back) or a write that failed."""
    home = Path(home)
    path = home / NAME
    why = target.local_problem(str(path))
    if why is not None:
        raise OSError(0, why)
    doc = {"version": VERSION, "games": [{"exe": g.exe, "name": g.name} for g in games[:MAX_GAMES]]}
    temp = home / (NAME + ".new")
    temp.write_bytes(json.dumps(doc, indent=1, ensure_ascii=False).encode("utf-8"))
    if unread and os.path.lexists(path):
        stamp = _stamp()
        aside, n = home / UNREAD_NAME.format(stamp=stamp), 2
        while os.path.lexists(aside):
            aside = home / UNREAD_NAME.format(stamp=f"{stamp}-{n}")
            n += 1
        os.replace(path, aside)
    os.replace(temp, path)
