"""What lies in and around the game's own folders when a recording starts: the files a fan fix adds, and an Unreal
Engine game's user config. So two recordings, one made before a mod and one after it, can be told apart.

Read from the file system only: nothing here opens, reads or waits on the game's process (DESIGN.md principle 1).
A folder that cannot be listed raises OSError, and the inventory reports the section as not read with the reason
(principle 2); a folder that does not exist holds nothing. Each file is fingerprinted by SHA-256 within a byte budget
and a deadline taken at the start; a file it does not hash keeps the reason, and is compared by size and date.

Names (DECISIONS 2026-11-08, "Compare shows what changed in the game itself"): the DLL names Ultimate ASI Loader's
README lists that Windows itself keeps in System32, plus d3d10core.dll (DXVK's README) and dwmapi.dll (UE4SS's
default proxy); none is on Windows' KnownDLLs list (tests/test_gamefiles.py reads it). The loader looks for plugins
in the exe's folder and its scripts, plugins and update folders, one level of subfolders deep (LoadRecursively).
Unreal user config folders were measured on the owner's PC: <LOCALAPPDATA or Documents\\My Games>\\<project or
install folder>\\Saved\\Config\\<WindowsNoEditor | Windows | WinGDK>.
"""
from __future__ import annotations

import hashlib
import ntpath
import os
import stat
import time
from datetime import datetime, timezone
from typing import Any, Callable

PROXY_DLLS = frozenset({
    "d3d8.dll", "d3d9.dll", "d3d10.dll", "d3d10core.dll", "d3d11.dll", "d3d12.dll", "dxgi.dll", "ddraw.dll",
    "dinput.dll", "dinput8.dll", "dsound.dll", "msacm32.dll", "msvfw32.dll", "version.dll", "wininet.dll",
    "winmm.dll", "winhttp.dll", "xlive.dll", "xinput1_1.dll", "xinput1_2.dll", "xinput1_3.dll", "xinput1_4.dll",
    "xinput9_1_0.dll", "xinputuap.dll", "dwmapi.dll",
})
LOADER_CONFIGS = frozenset({"global.ini", "wndmode.ini", "dxvk.conf", "ue4ss-settings.ini"})
ASI_FOLDERS = ("scripts", "plugins", "update")
UE4SS_MODS_LIST = ntpath.join("Mods", "mods.txt")
UNREAL_BINARIES = ("win64", "wingdk")
UNREAL_PLATFORMS = ("WindowsNoEditor", "Windows", "WinGDK")
MOD_PAKS = ntpath.join("Content", "Paks", "~mods")
MOD_PAK_SUFFIXES = (".pak", ".utoc", ".ucas")
MOD_PAK_DEPTH = 3                     # ~mods and up to two levels of subfolders

MAX_FILES = 100                       # listed per section; the rest are counted in `more`
MAX_ENTRIES = 20_000                  # directory entries read per folder: past that the folder is not judged
MAX_HASH_BYTES = 32 << 20             # a larger file is compared by size and date
HASH_BUDGET = 256 << 20               # bytes hashed per recording
DEADLINE_S = 5.0                      # for all the hashing of one recording, from its start
CHUNK = 1 << 20

# A cloud file (OneDrive) that is online only: reading it would download it (winnt.h).
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
ONLINE_ONLY = FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS

WHY_ONLINE = "kept online only, so it was not read"
WHY_LARGE = "larger than 32 MB"
WHY_BUDGET = "past the 256 MB read for one recording"
WHY_TIME = "out of time"


def modified_text(ts: float) -> str | None:
    """A file's modified time as ISO 8601 in this PC's time zone, to the second."""
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


class Fingerprints:
    """Hashes files within one budget and one deadline, both taken when it is made."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, budget: int = HASH_BUDGET,
                 deadline_s: float = DEADLINE_S) -> None:
        self.clock = clock
        self.deadline = clock() + deadline_s
        self.left = budget

    def entry(self, path: str, name: str, st: os.stat_result) -> dict[str, Any]:
        """{path, name, bytes, modified, sha256, why}: sha256 None with the reason in why."""
        out: dict[str, Any] = {"path": path, "name": name, "bytes": int(st.st_size),
                               "modified": modified_text(st.st_mtime), "sha256": None, "why": None}
        if getattr(st, "st_file_attributes", 0) & ONLINE_ONLY:
            out["why"] = WHY_ONLINE
        elif st.st_size > MAX_HASH_BYTES:
            out["why"] = WHY_LARGE
        elif st.st_size > self.left:
            out["why"] = WHY_BUDGET
        elif self.clock() >= self.deadline:
            out["why"] = WHY_TIME
        else:
            out["sha256"], out["why"] = self._hash(path, st.st_size)
        return out

    def _hash(self, path: str, size: int) -> tuple[str | None, str | None]:
        h = hashlib.sha256()
        read = 0
        try:
            with open(path, "rb") as f:
                while read <= size:                 # one byte past the listed size tells a file that grew
                    if read and self.clock() >= self.deadline:
                        self.left -= read
                        return None, WHY_TIME
                    chunk = f.read(min(CHUNK, size - read + 1))
                    if not chunk:
                        break
                    read += len(chunk)
                    h.update(chunk)
        except OSError as exc:
            self.left -= read
            return None, f"could not be read ({type(exc).__name__}: {exc.strerror or exc})"
        self.left -= read
        if read != size:
            return None, "it changed while it was read"
        return h.hexdigest(), None


def _entries(folder: str) -> list[os.DirEntry]:
    """The folder's entries; [] when it does not exist. OSError when it cannot be listed or holds too many."""
    out: list[os.DirEntry] = []
    try:
        with os.scandir(folder) as it:
            for entry in it:
                if len(out) >= MAX_ENTRIES:
                    raise OSError(f"{folder} holds more than {MAX_ENTRIES} entries, so it was not looked through")
                out.append(entry)
    except (FileNotFoundError, NotADirectoryError):
        return []
    except OSError as exc:
        if exc.errno is None and exc.winerror is None:          # ours, above
            raise
        raise OSError(f"{folder} could not be read: {exc.strerror or exc}") from exc
    return out


def _stat(entry: os.DirEntry) -> os.stat_result | None:
    """A regular file's stat, None for anything else. A cloud file is judged from the folder listing alone, so
    it is not opened; a link is followed, as Windows' loader follows it."""
    try:
        st = entry.stat(follow_symlinks=False)
        if entry.is_symlink():
            st = entry.stat()
    except FileNotFoundError:                       # gone since the listing, or a link to nothing
        return None
    except OSError as exc:
        raise OSError(f"{entry.path} could not be read: {exc.strerror or exc}") from exc
    return st if stat.S_ISREG(st.st_mode) else None


def _is_dir(entry: os.DirEntry) -> bool:
    return entry.is_dir(follow_symlinks=False)          # from the listing itself: no call to Windows


class _Listing:
    def __init__(self, prints: Fingerprints) -> None:
        self.prints = prints
        self.files: list[dict[str, Any]] = []
        self.more = 0
        self.seen: set[str] = set()

    def add(self, path: str, name: str, st: os.stat_result) -> None:
        key = os.path.normcase(path)
        if key in self.seen:
            return
        self.seen.add(key)
        if len(self.files) >= MAX_FILES:
            self.more += 1
            return
        self.files.append(self.prints.entry(path, name, st))


def drop_ins(exe: str, prints: Fingerprints | None = None) -> dict[str, Any]:
    """{folder, files, more}: the DLLs, ASI plugins and loader settings beside the game's exe, by name relative to
    its folder. OSError when a folder that exists cannot be listed."""
    prints = prints or Fingerprints()
    folder = ntpath.dirname(exe)
    found = _Listing(prints)
    root = _entries(folder)
    by_name = {e.name.lower(): e for e in root}
    proxies = [n for n in by_name if n in PROXY_DLLS]
    asi_dirs = [("", root)]
    for sub in ASI_FOLDERS:
        entry = by_name.get(sub)
        if entry is None or not _is_dir(entry):
            continue
        inner = _entries(entry.path)
        asi_dirs.append((entry.name, inner))
        for child in inner:
            if _is_dir(child):
                asi_dirs.append((ntpath.join(entry.name, child.name), _entries(child.path)))
    wanted_configs = set(LOADER_CONFIGS) | {n[:-4] + ".ini" for n in proxies}
    for rel, entries in asi_dirs:
        names = {e.name.lower(): e for e in entries}
        plugins = [n for n in names if n.endswith(".asi")]
        for low, entry in sorted(names.items()):
            if not (low in plugins or (rel == "" and low in PROXY_DLLS) or low in wanted_configs
                    or (low.endswith(".ini") and low[:-4] + ".asi" in names)):
                continue
            st = _stat(entry)
            if st is not None:
                found.add(entry.path, ntpath.join(rel, entry.name) if rel else entry.name, st)
    mods_txt = ntpath.join(folder, UE4SS_MODS_LIST)
    try:
        st = os.stat(mods_txt) if "dwmapi.dll" in by_name else None
    except (FileNotFoundError, NotADirectoryError):
        st = None
    if st is not None and stat.S_ISREG(st.st_mode):
        found.add(mods_txt, UE4SS_MODS_LIST, st)
    return {"folder": folder, "files": found.files, "more": found.more}


def unreal_layout(exe: str, is_dir: Callable[[str], bool] = os.path.isdir) -> dict[str, str] | None:
    """{project, project_dir, install}, when the exe sits where an Unreal Engine game's does:
    <install>\\<project>\\Binaries\\Win64\\<exe>, with <project>\\Content\\Paks or <install>\\Engine\\Binaries there."""
    bin_dir = ntpath.dirname(exe)
    binaries = ntpath.dirname(bin_dir)
    if ntpath.basename(bin_dir).lower() not in UNREAL_BINARIES or ntpath.basename(binaries).lower() != "binaries":
        return None
    project_dir = ntpath.dirname(binaries)
    install = ntpath.dirname(project_dir)
    project = ntpath.basename(project_dir)
    if not project or not (is_dir(ntpath.join(project_dir, "Content", "Paks"))
                           or is_dir(ntpath.join(install, "Engine", "Binaries"))):
        return None
    return {"project": project, "project_dir": project_dir, "install": install}


def _user_folders() -> tuple[str, str]:
    """(local AppData, Documents) of the user this process runs as, wherever Windows keeps them."""
    from lagdoctor.winapi import folders
    return folders.local_app_data(), folders.documents()


def _folder_name(name: str) -> bool:
    return bool(name) and name not in (".", "..") and not any(ch in name for ch in "\\/:\0")


def unreal_config_bases(layout: dict[str, str], local_app_data: str, documents: str) -> list[str]:
    """The folders an Unreal game may keep its Saved folder in: one of the project's or the install folder's name
    in LOCALAPPDATA and in Documents' My Games folder, and the project folder itself."""
    names: list[str] = []
    for name in (layout["project"], ntpath.basename(layout["install"])):
        if _folder_name(name) and name.lower() not in (n.lower() for n in names):
            names.append(name)
    return ([ntpath.join(local_app_data, n) for n in names] + [ntpath.join(documents, "My Games", n) for n in names]
            + [layout["project_dir"]])


def unreal_config_dirs(bases: list[str]) -> list[str]:
    return [ntpath.join(base, "Saved", "Config", platform) for base in bases for platform in UNREAL_PLATFORMS]


def unreal(exe: str, prints: Fingerprints | None = None,
           user_folders: Callable[[], tuple[str, str]] | None = None) -> dict[str, Any]:
    """{found, project, looked_in, config, config_more, mods, mods_more}. found False (and nothing else filled)
    when the exe is not laid out as an Unreal Engine game's. looked_in: the folders whose Saved\\Config\\<platform>
    folders were read; config: every .ini in those that exist; mods: the pak files in Content\\Paks\\~mods; *_more:
    how many past MAX_FILES were left out. OSError when a folder that exists cannot be listed, or the user's folders
    cannot be found."""
    prints = prints or Fingerprints()
    out: dict[str, Any] = {"found": False, "project": None, "looked_in": [], "config": [], "config_more": 0,
                           "mods": [], "mods_more": 0}
    layout = unreal_layout(exe)
    if layout is None:
        return out
    try:
        local_app_data, documents = (user_folders or _user_folders)()
    except OSError as exc:
        raise OSError(f"Windows did not say where this account's AppData and Documents folders are "
                      f"({exc.strerror or exc})") from exc
    bases = unreal_config_bases(layout, local_app_data, documents)
    out.update(found=True, project=layout["project"], looked_in=bases)
    config = _Listing(prints)
    for folder in unreal_config_dirs(bases):
        for entry in sorted(_entries(folder), key=lambda e: e.name.lower()):
            if entry.name.lower().endswith(".ini"):
                st = _stat(entry)
                if st is not None:
                    config.add(entry.path, entry.name, st)
    paks = ntpath.join(layout["project_dir"], MOD_PAKS)
    mods = _Listing(prints)
    pending = [(paks, "~mods", 1)]
    while pending:
        folder, rel, depth = pending.pop(0)
        for entry in sorted(_entries(folder), key=lambda e: e.name.lower()):
            if _is_dir(entry):
                if depth < MOD_PAK_DEPTH:
                    pending.append((entry.path, ntpath.join(rel, entry.name), depth + 1))
            elif entry.name.lower().endswith(MOD_PAK_SUFFIXES):
                st = _stat(entry)
                if st is not None:
                    mods.add(entry.path, ntpath.join(rel, entry.name), st)
    out.update(config=config.files, config_more=config.more, mods=mods.files, mods_more=mods.more)
    return out
