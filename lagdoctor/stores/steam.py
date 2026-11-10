"""Steam's installed games, read from Steam's own files without starting Steam (docs/LAUNCHER-PLAN.md, M6).

Where Steam keeps them (research note desktop-app.md, section 15, and the readers' code it cites):
- Steam's folder: HKCU Software\\Valve\\Steam SteamPath, which Steam writes lower-cased with forward slashes, else
  InstallPath under HKLM SOFTWARE\\WOW6432Node\\Valve\\Steam (the 32-bit view) and then SOFTWARE\\Valve\\Steam,
  which hold it in the folder's own case (steamlocate reads InstallPath in that order).
- Its libraries: <Steam>\\steamapps\\libraryfolders.vdf, KeyValues text. Since 2021 its root "libraryfolders" holds
  numbered blocks whose "path" is the library, the Steam folder among them as "0"; before that the root
  "LibraryFolders" held the stats keys TimeNextStatsReport and ContentStatsID and numbered strings that are the paths
  themselves, without the Steam folder. Some writers add "mounted" (Lutris skips an entry whose value is not "1").
  Playnite adds the Steam folder itself and drops duplicates after folding case and slashes, as this does.
- Each library's games: steamapps\\appmanifest_<appid>.acf, one AppState block. A game is installed when its
  decimal StateFlags has FullyInstalled (the value 4) set and steamapps\\common\\<installdir> exists (Playnite,
  Lutris); a soundtrack's folder is steamapps\\music\\<installdir> instead, and it is passed over, as Playnite does.

Every one of these, the registry included, can be written by any local account (the Users group has full control of
the Steam folder and KEY_ALL_ACCESS on both HKLM keys). So every value passes a grammar, every size and count is
capped, nothing on a network drive is opened, nor through a link that leads to one (stores' file access, which asks
target.local_problem: a sleeping server could hold a call for its whole timeout), and what fails is skipped and
named in Library.problems in plain words: nothing is guessed, and one bad entry never hides the others in its file.
A libraryfolders.vdf that is not written the way Steam writes it is named whole, and only Steam's own folder is
then searched.
"""
from __future__ import annotations

import os
import re
import winreg
from pathlib import Path

from lagdoctor import stores
from lagdoctor.capture import target
from lagdoctor.stores import keyvalues, launch

STORE = "steam"
# Where Steam's folder is named, in the order they are read: (hive, root, key, value).
REGISTRY = (
    ("HKCU", winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
    ("HKLM", winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
    ("HKLM", winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
)
LOWER_CASED = "SteamPath"               # the value Steam writes as a lower-cased copy of the folder's name

MAX_LIBRARY_FILE_BYTES = 1 << 20        # libraryfolders.vdf: a few hundred bytes a library, its apps included
MAX_MANIFEST_BYTES = 256 << 10          # one appmanifest file: a few kilobytes (its depots and install scripts)
MAX_LIBRARIES = 64                      # Steam's own folder included
MAX_MANIFESTS = 4096                    # appmanifest files, all libraries together
MAX_STEAMAPPS_ENTRIES = 20_000          # entries read from one steamapps folder: past that it is not looked through
MAX_NAME_CHARS = 255                    # one folder name (NTFS)
MAX_FLAGS = 0xFFFFFFFF                  # StateFlags is a 32-bit mask
FULLY_INSTALLED = 4                     # the StateFlags bit (value 4) every reader takes as "installed"
SOURCE = "Steam"                        # as Library.problems names it (stores.Problems)

# Not games, skipped by id since no manifest says what kind of app it is (research note desktop-app.md, section 15):
# Steamworks Common Redistributables, which every reader skips (Playnite's SteamLocalService.cs at 886468f, lines
# 259-263; Lutris's services/steam.py at 95b0fef, lines 83-87), and Steam for Linux beside it in Lutris's list; then
# the Linux tools that a library shared with a Linux install can hold: the Steam Linux Runtimes (Valve's steam-runtime
# README at 8cb6d88) and the Proton versions, by the app ids Steam's own compat log registers them under (one that a
# player pasted into Valve's Proton tracker, issue 3831; Proton 11.0 from a client log in its steam-for-linux tracker,
# issue 13248). Each public record on api.steamcmd.net names it a Tool (read 2026-11-09).
NOT_GAMES = frozenset({
    "228980", "221410",                                         # Steamworks Common Redistributables, Steam for Linux
    "1070560", "1391110", "1628350", "4183110",                 # Steam Linux Runtime 1.0 (scout), 2.0, 3.0, 4.0
    "1493710", "2180100",                                       # Proton Experimental, Proton Hotfix
    "858280", "961940", "1054830", "1113280", "1245040",        # Proton 3.7, 3.16, 4.2, 4.11, 5.0
    "1420170", "1580130", "1887720", "2348590", "2805730",      # Proton 5.13, 6.3, 7.0, 8.0, 9.0
    "3658110", "4628710",                                       # Proton 10.0, 11.0
})

_NUMBERED = re.compile(r"[0-9]+", re.ASCII)                     # a library's key in libraryfolders.vdf
_FLAGS = re.compile(r"[0-9]{1,10}", re.ASCII)                   # StateFlags, a decimal number
_MANIFEST = re.compile(r"appmanifest_([0-9]+)\.acf", re.ASCII | re.IGNORECASE)
_NOT_IN_NAMES = frozenset('<>:"|?*')                             # characters Windows refuses in a name
# Spaces other than the ASCII one, which str.isprintable refuses and Windows keeps in a name: the no-break space, the
# ideographic space of CJK names, and the rest of Unicode's space separators.
_SPACES = frozenset(map(chr, (0xA0, 0x1680, *range(0x2000, 0x200B), 0x202F, 0x205F, 0x3000)))


def _local_folder(value: object) -> str:
    """A folder that Steam's files or the registry name, in target.check_folder's form and on a local fixed or
    removable disk by its drive letter (target.disk_problem), so the entry that names it is named; ValueError says
    why not. (The links along its path are read, and refused when they lead elsewhere, when it is looked at: stores'
    file access, which asks the drive again.)"""
    if not isinstance(value, str):
        raise ValueError("it names no folder")
    if len(value) > target.MAX_FOLDER_CHARS:
        raise ValueError(f"its folder is longer than {target.MAX_FOLDER_CHARS} characters")
    folder = target.check_folder(value)
    why = target.disk_problem(folder)
    if why is not None:
        raise ValueError(f"{folder} {why}")
    return folder


def _steam_folder(value: object) -> str:
    """Steam's folder as the registry or the caller names it; ValueError says why it cannot be used."""
    folder = _local_folder(value)
    try:
        found = stores.is_folder(folder)
    except OSError as exc:
        raise ValueError(stores.failure(exc, folder)) from exc
    if not found:
        raise ValueError(f"{folder} does not exist")
    return folder


def _open_key(root: int, key: str, reserved: int, access: int):
    """winreg.OpenKey, looked up when called (the tests replace this, never winreg's own function)."""
    return winreg.OpenKey(root, key, reserved, access)


def _query_value(handle, name: str) -> tuple[object, int]:
    """winreg.QueryValueEx, looked up when called (the tests replace this, never winreg's own function)."""
    return winreg.QueryValueEx(handle, name)


def _expand(text: str) -> str:
    """winreg.ExpandEnvironmentStrings, looked up when called (the tests replace this, never winreg's own function)."""
    return winreg.ExpandEnvironmentStrings(text)


def _registry_text(root: int, key: str, name: str) -> str | None:
    """One registry value as text, read only; None when its key or the value does not exist. ValueError when it
    cannot be read or is not text: REG_SZ, or REG_EXPAND_SZ, whose environment variables are expanded."""
    try:
        with _open_key(root, key, 0, winreg.KEY_READ) as handle:
            value, kind = _query_value(handle, name)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(f"it could not be read ({exc.strerror or exc})") from exc
    if not isinstance(value, str):      # winreg gives text for REG_SZ and REG_EXPAND_SZ only
        raise ValueError("it is not text")
    if kind == winreg.REG_EXPAND_SZ:
        try:
            return _expand(value)
        except OSError as exc:
            raise ValueError(f"its environment variables could not be expanded ({exc.strerror or exc})") from exc
    return value


def _look_up() -> tuple[str | None, list[str]]:
    """find_steam_folder's answer, and why each registry value that names a folder was not taken."""
    found: list[tuple[str, str]] = []
    reasons: list[str] = []
    for hive, root, key, name in REGISTRY:
        try:
            text = _registry_text(root, key, name)
            if text is not None:
                found.append((name, _steam_folder(text)))
        except ValueError as exc:
            reasons.append(f"{hive}\\{key} {name}: {exc}")
    if not found:
        return None, reasons
    name, folder = found[0]
    if name == LOWER_CASED:             # every later one is an InstallPath
        for _, other in found[1:]:
            if target.fold_path(other) == target.fold_path(folder):
                return other, []
    return folder, []


def find_steam_folder() -> str | None:
    """Steam's folder, from the registry, read only: HKCU's SteamPath, else HKLM's InstallPath (the 32-bit view, then
    the 64-bit one). A value counts only as text (REG_SZ, or REG_EXPAND_SZ, expanded) that passes target.check_folder,
    names a folder on a local disk, and exists; the first such is the answer, in check_folder's form. Steam writes
    SteamPath lower-cased, so when an InstallPath names the same folder, its spelling, in the folder's own case, is
    the one given. None when no value names one."""
    return _look_up()[0]


def _libraries(steam: str, problems: stores.Problems) -> list[str]:
    """The folders Steam keeps games in: its own first, then libraryfolders.vdf's, each once (target.fold_path)."""
    out = [steam]
    seen = {target.fold_path(steam)}
    vdf = os.path.join(steam, "steamapps", "libraryfolders.vdf")
    only_steam = "Only Steam's own folder was searched for games"
    try:
        present = stores.exists(vdf)
    except OSError as exc:
        problems.add(f"{only_steam}: {stores.failure(exc, vdf)}")
        return out
    if not present:
        problems.add(f"{only_steam}: Steam's list of its libraries, {vdf}, is missing")
        return out
    try:
        text = stores.read_text(Path(vdf), MAX_LIBRARY_FILE_BYTES)
    except ValueError as exc:
        problems.add(f"{only_steam}: {exc}")
        return out
    try:
        tree = keyvalues.parse(text)
    except ValueError as exc:
        problems.add(f"{only_steam}: {vdf} is not written the way Steam writes it ({exc})")
        return out
    entries = tree.get("libraryfolders")
    if not isinstance(entries, dict):
        problems.add(f"{only_steam}: {vdf} holds no list of libraries")
        return out
    for key, value in entries.items():
        if not _NUMBERED.fullmatch(key):
            continue                    # TimeNextStatsReport, ContentStatsID: not libraries
        where = f"Steam's library {key} in {vdf}"
        if isinstance(value, dict):     # since 2021: a block, whose path is the library
            mounted = value.get("mounted", "1")
            if mounted == "0":
                continue                # Steam's own mark for a library it cannot reach now
            if mounted != "1":
                problems.add(f"{where} was skipped: its mounted value is {stores.shown(mounted)}, not 0 or 1")
                continue
            value = value.get("path")
        try:
            folder = _local_folder(value)
        except ValueError as exc:
            problems.add(f"{where} was skipped: {exc}")
            continue
        if target.fold_path(folder) in seen:
            continue                    # the Steam folder as entry 0, or a library listed twice
        if len(out) >= MAX_LIBRARIES:
            problems.add(f"{vdf} lists more than {MAX_LIBRARIES} libraries, so only the first {MAX_LIBRARIES} "
                         "were searched for games")
            break
        seen.add(target.fold_path(folder))
        out.append(folder)
    return out


def _manifest_files(steamapps: str) -> list[tuple[str, os.DirEntry]]:
    """(app id digits, entry) for each appmanifest_<digits>.acf in a steamapps folder, by app id. OSError when the
    folder cannot be listed; ValueError when it holds more than MAX_STEAMAPPS_ENTRIES entries."""
    found = []
    seen = 0
    with stores.scandir(steamapps) as entries:
        for entry in entries:
            seen += 1
            if seen > MAX_STEAMAPPS_ENTRIES:
                raise ValueError(f"{steamapps} holds more than {MAX_STEAMAPPS_ENTRIES} entries, so it was not "
                                 "looked through")
            m = _MANIFEST.fullmatch(entry.name)
            if m:
                found.append((m.group(1), entry))
    return sorted(found, key=lambda f: (len(f[0]), f[0], f[1].name.casefold()))


def _name_problem(name: object) -> str | None:
    """Why `name` is not one folder name as Windows keeps it, in plain words; None when it is."""
    if not isinstance(name, str):
        return "not text"
    if not name:
        return "empty"
    if len(name) > MAX_NAME_CHARS:
        return f"longer than {MAX_NAME_CHARS} characters"
    if "\\" in name or "/" in name:
        return "a path, not one folder's name"
    if name in (".", ".."):
        return "not a folder's name"
    if name[-1] in ". ":
        return "a name Windows would not keep as written (it ends in a dot or a space)"
    if not all(c.isprintable() or c in _SPACES for c in name):
        return "a name with characters that cannot be shown"
    if any(c in _NOT_IN_NAMES for c in name):
        return "a name Windows does not allow"
    return None


def _manifest(entry: os.DirEntry, digits: str, library: str) -> stores.Game | str | None:
    """The game one appmanifest file names; a problem, in plain words, when the file cannot be used; None when it
    names no installed game (not fully installed, still downloading, or a soundtrack)."""
    path = entry.path
    try:
        link = launch.steam_link(digits)
    except ValueError:
        return f"{path} was skipped: its name holds no Steam app id"
    if not entry.is_file(follow_symlinks=False):
        return f"{path} was skipped: it is not a plain file"
    try:
        text = stores.read_text(Path(path), MAX_MANIFEST_BYTES)
    except ValueError as exc:
        return f"A Steam game was skipped: {exc}"
    try:
        state = keyvalues.parse(text).get("appstate")
    except ValueError as exc:
        return f"{path} was skipped: it is not written the way Steam writes it ({exc})"
    if not isinstance(state, dict):
        return f"{path} was skipped: it holds no app state"
    appid = state.get("appid")
    if appid is None:
        return f"{path} was skipped: it names no app"
    if appid != digits:
        return f"{path} was skipped: it names the app {stores.shown(appid)}, but its file is named for the app {digits}"
    flags = state.get("stateflags")
    if flags is None:
        return f"{path} was skipped: it gives no install state"
    if not isinstance(flags, str) or not _FLAGS.fullmatch(flags) or int(flags) > MAX_FLAGS:
        return f"{path} was skipped: its install state ({stores.shown(flags)}) is not a number"
    if not int(flags) & FULLY_INSTALLED:
        return None                     # not installed, or still downloading
    installdir = state.get("installdir")
    if installdir is None:
        return f"{path} was skipped: it names no install folder"
    why = _name_problem(installdir)
    if why is not None:
        return f"{path} was skipped: the name it gives its install folder ({stores.shown(installdir)}) is {why}"
    try:
        folder = target.check_folder(os.path.join(library, "steamapps", "common", installdir))
    except ValueError as exc:
        return f"{path} was skipped: {exc}"
    title = stores.title(state.get("name"), installdir)
    # A soundtrack's folder: its library and its name have passed their checks, so its path needs none of its own.
    music = os.path.join(library, "steamapps", "music", installdir)
    try:
        there = stores.is_folder(folder)
    except OSError as exc:
        return f"{title} (Steam app {digits}) was skipped: {stores.failure(exc, folder)}"
    if there:
        return stores.Game(store=STORE, id=digits, title=title, folder=folder, link=link)
    try:
        soundtrack = stores.is_folder(music)
    except OSError as exc:
        return f"{title} (Steam app {digits}) was skipped: its folder {folder} is missing, and " \
               f"{stores.failure(exc, music)}"
    if soundtrack:
        return None                     # a soundtrack, not a game
    return f"{title} (Steam app {digits}) is listed as installed, but its folder {folder} is missing"


def _games(libraries: list[str], problems: stores.Problems) -> list[stores.Game]:
    """The installed games in `libraries`, in their order and by app id within each, every app id once."""
    games: list[stores.Game] = []
    kept: dict[str, stores.Game] = {}
    read = 0
    for library in libraries:
        steamapps = os.path.join(library, "steamapps")
        try:
            files = _manifest_files(steamapps)
        except FileNotFoundError:
            try:
                there = stores.is_folder(library)
            except OSError as exc:
                problems.add(f"{stores.failure(exc, library)}, so its games were not listed")
                continue
            if there:
                problems.add(f"The Steam library {library} has no steamapps folder, so no games were found in it")
            else:
                problems.add(f"The Steam library {library} is missing, so its games were not listed")
            continue
        except OSError as exc:
            problems.add(f"{stores.failure(exc, steamapps, 'read')}, so its games were not listed")
            continue
        except ValueError as exc:
            problems.add(f"The Steam library {library} was skipped: {exc}")
            continue
        for digits, entry in files:
            read += 1
            if read > MAX_MANIFESTS:
                problems.add(f"Steam's libraries hold more than {MAX_MANIFESTS} app files, so the rest were not read")
                return games
            if digits in NOT_GAMES:
                continue
            got = _manifest(entry, digits, library)
            if isinstance(got, str):
                problems.add(got)
            elif got is not None:
                first = kept.get(got.id)
                if first is not None:
                    problems.add(f"{got.title} (Steam app {got.id}) is listed in two libraries: the one in "
                                 f"{first.folder} was kept, and the one in {got.folder} was left out")
                    continue
                kept[got.id] = got
                games.append(got)
    return games


def read(steam_folder: str | None = None) -> stores.Library:
    """Steam's installed games, from the files under `steam_folder` (find_steam_folder's when None), and what could
    not be read, in plain words: the libraries in libraryfolders.vdf after Steam's own folder, and in each the games
    whose appmanifest file says they are fully installed and whose folder exists. Without a Steam folder that can be
    used: no games, and one problem that says why."""
    problems = stores.Problems(SOURCE)
    if steam_folder is None:
        steam, reasons = _look_up()
        if steam is None:
            why = "; ".join(reasons) if reasons else "Steam may not be installed"
            problems.add(f"Steam's folder was not found, so no Steam games were listed ({why})")
            return stores.Library(problems=problems.lines())
    else:
        try:
            steam = _steam_folder(os.fspath(steam_folder) if isinstance(steam_folder, os.PathLike) else steam_folder)
        except ValueError as exc:
            problems.add(f"Steam's folder could not be used, so no Steam games were listed: {exc}")
            return stores.Library(problems=problems.lines())
    games = _games(_libraries(steam, problems), problems)
    return stores.Library(games=games, problems=problems.lines())
