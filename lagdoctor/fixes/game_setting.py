"""game_setting: one value in a game's own settings file, set for the length of a settings sweep.

DESIGN keeps in-game settings to advice, with one exception the owner asked for (DECISIONS 2026-11-09): a settings
sweep (lagdoctor/sweep/) writes each setting it tries into the game's settings file while the game is closed and puts
the original back at the end. This fix is how it writes, through the journal like every other change, so a sweep that
dies leaves an entry that the next start settles and `lagdoctor undo` puts back. No report suggests it, so `lagdoctor
fix` never offers it.

params: {"file": the .ini's full path, "section": "ScalabilityGroups", "key": "sg.StaticMeshQuality", "value": "1",
"game": "ff7rebirth_.exe"}.
- Only one value's characters change (lagdoctor/sweep/inifile.py), and the file is written whole: a temp file beside
  it, flushed to disk, then os.replace, so the game never finds half a file. The replaced file gets the temp file's
  attributes and identity; Unreal reads it by its name.
- read() raises when the file, or the key in its section, is not there: a sweep never adds or removes a line, so plan()
  refuses, and undo leaves such an entry alone with the reason.
- A file OneDrive keeps online only is never opened (opening it downloads it: capture/gamefiles.py), and a file marked
  read-only is left alone: a player who set it so wants the game, and lag-doctor, to keep off it.
- write() refuses while a process with the game's image name runs (from the process snapshot, no handle): the game
  reads its settings as it starts and writes them back as it exits, so a value written while it runs would not be used,
  and an undo then would be overwritten at its exit.
"""
from __future__ import annotations

import ntpath
import os
import re
import stat
import threading
import time
from typing import Any, Callable

from lagdoctor.capture.gamefiles import ONLINE_ONLY
from lagdoctor.fixes.base import Fix
from lagdoctor.fixes.catalog import register
from lagdoctor.sweep import inifile

FILE_ATTRIBUTE_READONLY = 0x1           # winnt.h
PATH_MAX = 1024
PARAM_KEYS = frozenset({"file", "section", "key", "value", "game"})
TEMP_SUFFIX = ".lagdoctor.tmp"          # the whole new file, beside the old one, until it replaces it
REPLACE_TRIES = 5                       # OneDrive and virus scanners hold a file open for a moment after a change
REPLACE_WAIT_S = 0.2
EXE_NAME = re.compile(r'[^\\/:*?"<>|\x00-\x1f]{1,251}\.exe', re.IGNORECASE)


def check_file(path: object) -> str:
    """The settings file's full path, normalised. ValueError unless it is a plain path on a drive letter (never a
    share or a device path) to an .ini file."""
    if not isinstance(path, str) or not path or len(path) > PATH_MAX or any(ord(c) < 32 for c in path):
        raise ValueError(f"not a settings file's path: {path!r}")
    norm = ntpath.normpath(path)
    drive, rest = ntpath.splitdrive(norm)
    if len(drive) != 2 or drive[1] != ":" or not drive[0].isalpha() or not rest.startswith("\\"):
        raise ValueError(f"the settings file must be a full path on a drive, such as C:\\..., not {path!r}")
    if not norm.lower().endswith(".ini"):
        raise ValueError(f"the settings file must be an .ini file, not {path!r}")
    return norm


def check_game(name: object) -> str:
    """The game's exe name (no folder). ValueError for anything else."""
    if not isinstance(name, str) or not EXE_NAME.fullmatch(name):
        raise ValueError(f"not a game's exe name: {name!r}")
    return name


def _params(params: Any) -> tuple[str, str, str, str, str]:
    """(file, section, key, value, game), each checked. ValueError for params this fix did not make."""
    if not isinstance(params, dict) or set(params) != PARAM_KEYS:
        raise ValueError(f"game_setting takes exactly {sorted(PARAM_KEYS)}, got {params!r}")
    section, key, value = params["section"], params["key"], params["value"]
    if not isinstance(section, str) or not inifile.SECTION.fullmatch(section):
        raise ValueError(f"not a settings section: {section!r}")
    if not isinstance(key, str) or not inifile.KEY.fullmatch(key):
        raise ValueError(f"not a setting's name: {key!r}")
    if not isinstance(value, str) or not inifile.VALUE.fullmatch(value):
        raise ValueError(f"not a value a sweep writes: {value!r}")
    return check_file(params["file"]), section, key, value, check_game(params["game"])


def _file_stat(path: str) -> os.stat_result:
    """os.stat, as this module's own seam: a test that patched os.stat itself would reach every module."""
    return os.stat(path)


def _move(src: str, dst: str) -> None:
    """os.replace, as this module's own seam (see _file_stat)."""
    os.replace(src, dst)


def read_bytes(file: str, *, for_write: bool = False) -> bytes:
    """The file's bytes. Refuses a file OneDrive keeps online only (opening it would download it), one larger than a
    settings file, and, when it is about to be written, one marked read-only. FileNotFoundError when it is gone."""
    st = _file_stat(file)
    if not stat.S_ISREG(st.st_mode):
        raise ValueError(f"{file} is not a file")
    attributes = getattr(st, "st_file_attributes", 0)
    if attributes & ONLINE_ONLY:
        raise ValueError(f"{file} is kept online only by OneDrive, so lag-doctor does not open it (that would "
                         "download it); make it available on this PC first")
    if for_write and attributes & FILE_ATTRIBUTE_READONLY:
        raise PermissionError(f"{file} is marked read-only, so lag-doctor leaves it as it is")
    if st.st_size > inifile.MAX_BYTES:
        raise ValueError(f"{file} holds {st.st_size:,} bytes, more than a settings file would")
    with open(file, "rb") as f:
        raw = f.read(inifile.MAX_BYTES + 1)
    if len(raw) > inifile.MAX_BYTES:
        raise ValueError(f"{file} grew past {inifile.MAX_BYTES:,} bytes while it was read")
    return raw


def read_text(file: str) -> str:
    """The file's text (inifile.decode), refused as read_bytes and decode refuse."""
    return inifile.decode(read_bytes(file))[0]


def replace_bytes(file: str, data: bytes, *, sleep: Callable[[float], None] = time.sleep) -> None:
    """Write the file whole: a temp file beside it, flushed to disk, then moved over it. A move refused for a moment
    (another program has the file open) is tried again a few times; the temp file never stays behind."""
    tmp = file + TEMP_SUFFIX
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(REPLACE_TRIES):
            try:
                _move(tmp, file)
                return
            except PermissionError:
                if attempt == REPLACE_TRIES - 1:
                    raise
                sleep(REPLACE_WAIT_S)
    finally:
        if os.path.lexists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


_snapshot_lock = threading.Lock()
_snapshotter: Any = None


def processes_named(name: str, procs: dict[int, Any] | None = None) -> list[int]:
    """The pids of running processes with this image name, compared without regard to case. One snapshot of every
    process (winapi/ntquery.py), which opens none."""
    global _snapshotter
    if procs is None:
        from lagdoctor.winapi import ntquery
        with _snapshot_lock:
            if _snapshotter is None:
                _snapshotter = ntquery.Snapshotter()
            procs = _snapshotter.snapshot()
    want = check_game(name).casefold()
    return sorted(p.pid for p in procs.values() if isinstance(p.name, str) and p.name.casefold() == want)


@register
class GameSettingFix(Fix):
    id = "game_setting"
    title = "A setting in the game's own settings file"
    needs_restart = "game"
    description = ("Changes one value in the game's own settings file while the game is closed. Only `lagdoctor "
                   "sweep` uses it, and the sweep puts the original back when it ends.")

    def __init__(self, running: Callable[[str], bool] | None = None) -> None:
        self.running = running if running is not None else (lambda game: bool(processes_named(game)))

    def target(self, params: dict[str, Any]) -> str:
        file, section, key, _value, _game = _params(params)
        return f"{file} [{section}] {key}"

    def read(self, params: dict[str, Any]) -> Any:
        file, section, key, _value, _game = _params(params)
        value = inifile.get_value(read_text(file), section, key)
        if value is None:
            raise LookupError(f"{key} is not set in [{section}] of {file}")
        return value

    def wanted(self, params: dict[str, Any], current: Any) -> Any:
        return _params(params)[3]

    def write(self, params: dict[str, Any], value: Any) -> None:
        file, section, key, _value, game = _params(params)
        if not inifile.line_safe(value):
            raise ValueError(f"{value!r} cannot be written as a value")
        if self.running(game):
            raise RuntimeError(f"{game} is running: close it first, because the game writes its settings back when "
                               "it exits")
        text, enc = inifile.decode(read_bytes(file, for_write=True))
        replace_bytes(file, inifile.encode(inifile.set_value(text, section, key, value), enc))

    def describe(self, before: Any, after: Any) -> str:
        return f"from {before} to {after}"
