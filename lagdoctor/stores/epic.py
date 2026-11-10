"""Epic's installed games, read from its launchers' own files without starting either (docs/LAUNCHER-PLAN.md, M6).

Two launchers keep the list. The legacy Epic Games Launcher writes one JSON manifest per install,
%ProgramData%\\Epic\\EpicGamesLauncher\\Data\\Manifests\\<InstallationGuid>.item. The new one, the Epic Games Launcher
Preview (EGL v2), keeps a journal, %LOCALAPPDATA%\\com.epicgames.eglv2\\installations.jsonl, and removes the legacy
folder when it takes the games over. Every local account can write the legacy manifests, and any program the player
runs can append a whole line to the journal, which the Preview then adopts (research note desktop-app.md, section
15, as for everything below). So each is read as Epic writes it and no further: sizes and counts are capped, the
JSON is read strictly (no NaN or Infinity, no key twice, no nesting past Python's limit), the ids must pass
launch.epic_link and the folder target.check_folder, and the folder must exist on a local disk (nothing on a network
drive is opened, nor through a link that leads to one: a sleeping server could hold the call for its whole
timeout). What fails is named in Library.problems in plain words, and the rest is still read.

Passed over quietly, as no game Play starts:
- legacy: an add-on (AppCategories holding "addons" but not "addons/launchable", Playnite's rule; in a manifest
  without AppCategories, a MainGameAppName that names another app, since today's launcher writes "" there for a base
  game and Fortnite's own manifest names its editor there), anything whose AppCategories is present and holds no
  "games" (plugins, engines and apps: DLSS Swapper's and steamsync's rule), an install not finished
  (bIsIncompleteInstall true, steamsync's), and Unreal Engine itself (namespace "ue").
- the Preview: the journal is replayed top to bottom, never sorted by writtenAt, whose one-second stamps tie. The
  last record per installationId wins, a tombstone {installationId, deleted: true} removes it, a blank line is
  nothing, and a half-written last line is the launcher still writing. A record is a game when complete is true
  (false is a download, an update or an uninstall in progress), it has no parentInstallationId (an add-on, which
  shares its base game's folder), and it is not Unreal Engine. Line 1 is the header {version: 1, writtenAt,
  entries}; entries counts the records at the last rewrite, not the lines, so it is not checked. The journal holds
  no title: the Preview's catalog cache gives one by catalog item id, else the legacy manifest with the same AppName,
  else the folder's name (which the Preview sometimes ends in a random suffix).
A game the Preview still has a record of is taken as the Preview has it: a legacy manifest of the same game is then
passed over, even while the Preview's record says that the game is being updated.

Nothing here reads a game's command line (LaunchCommand, launchCommand): an Epic launch carries a sign-in code there.
"""
from __future__ import annotations

import json
import ntpath
import os
import re
import stat
from pathlib import Path
from typing import Callable

from lagdoctor import stores
from lagdoctor.capture import gamefiles, target
from lagdoctor.stores import Game, Library, launch
from lagdoctor.winapi import folders

STORE = "epic"
LEGACY = "the Epic Games Launcher"
PREVIEW = "the Epic Games Launcher Preview"
MANIFESTS = ("Epic", "EpicGamesLauncher", "Data", "Manifests")      # under %ProgramData%
INSTALLATIONS = ("com.epicgames.eglv2", "installations.jsonl")       # under %LOCALAPPDATA%

MAX_ENTRIES = 16_384            # files and folders looked at in the manifests folder
MAX_ITEMS = 4096                # manifests in it: the launcher writes one per install
MAX_ITEM_BYTES = 1 << 20        # one manifest: the launcher's are a few kilobytes
MAX_ALL_ITEMS_BYTES = 64 << 20  # all of them together, counted as read
MAX_JSONL_BYTES = 8 << 20       # the Preview's journal: a line of about 500 bytes per change to an install
MAX_LINES = 100_000
CATALOG = ("cache", "prod", "catalog-cache.json")      # beside installations.jsonl: the Preview's catalog cache
MAX_CATALOG_BYTES = 16 << 20    # the catalog items the Preview has fetched, with their images' links
MAX_CATALOG_ITEMS = 100_000
HEADER_VERSION = 1              # the only one seen
INSTALLATION_ID = re.compile(r"[0-9A-F]{32}", re.ASCII)      # as the Preview writes it
ENGINE_NAMESPACE = "ue"         # Unreal Engine's own installs


# ---------------------------------------------------------------- JSON as Epic writes it ---

class _Refused(ValueError):
    """JSON that Epic does not write."""


def _pairs(pairs: list[tuple[str, object]]) -> dict:
    """One JSON object's keys and values as a dict, refusing a key that it holds twice."""
    out: dict = {}
    for key, value in pairs:
        if key in out:              # json keeps the last of two; another reader may keep the first
            raise _Refused(f"it has the key {stores.shown(key)} twice")
        out[key] = value
    return out


def _constant(name: str) -> object:
    """Refuses NaN, Infinity and -Infinity, which Python's json reads by default and JSON does not have."""
    raise _Refused(f"it holds {name}, which is not a JSON value")


def _json(text: str, line: bool = False) -> object:
    """`text` as JSON, read strictly; ValueError in plain words for what Epic would not write: NaN or Infinity, a key
    twice, nesting past Python's limit, a number of more digits than Python reads. `line`: `text` is one line of a
    file, so a mistake is placed by its column alone."""
    try:
        return json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
    except _Refused:
        raise
    except json.JSONDecodeError as exc:
        at = f"column {exc.colno}" if line else f"line {exc.lineno}, column {exc.colno}"
        raise ValueError(f"it is not JSON ({exc.msg}: {at})") from None
    except RecursionError:
        raise ValueError("it is nested too deep to read") from None
    except ValueError:              # Python's limit on the digits of a number
        raise ValueError("it holds a number too long to read") from None


# ---------------------------------------------------------------- a game, from either launcher ---

def _not_installed(folder: str) -> str | None:
    """Why no game can be installed in `folder` (as target.check_folder gives it), in plain words; None when it is a
    folder on a local disk. Nothing on a network drive is opened, nor through a link that leads to one (stores.stat,
    as target.folder_forms opens none)."""
    try:
        st = stores.stat(folder)
    except FileNotFoundError:
        return f"{folder} does not exist"
    except OSError as exc:
        return stores.failure(exc, folder)
    if not stat.S_ISDIR(st.st_mode):
        return f"{folder} is not a folder"
    return None


def _game(where: str, namespace: object, item: object, app: object, location: object, name: str,
          taken: set[str] | frozenset[str] = frozenset()) -> Game | str | None:
    """The game one entry of a launcher names: a Game; None when `taken` holds its id already; else a str that says
    which field fails. `name` is its title when the launcher gives one (stores.name), else "" for the folder's name;
    either way stores.title cleans it."""
    try:
        link = launch.epic_link(namespace, item, app)
    except ValueError as exc:
        return f"{where}: {exc}, so its game is not listed"
    gid = f"{namespace}:{item}:{app}"
    if gid in taken:
        return None
    try:
        folder = target.check_folder(location)
    except ValueError as exc:
        return f"{where}: its install folder was refused: {exc}, so its game is not listed"
    why = _not_installed(folder)
    if why is not None:
        return f"{where}: {why}, so its game is not listed"
    return Game(STORE, gid, stores.title(name, ntpath.basename(folder)), folder, link)


def _keep(kept: dict[str, tuple[Game, str]], got: Game | str | None, where: str, problems: stores.Problems) -> None:
    """Keeps what one launcher's entry gave: a problem is named, and a game is kept once per id. A second entry for
    the same game in another folder is named, and the first stays listed."""
    if got is None:
        return
    if isinstance(got, str):
        problems.add(got)
        return
    first = kept.get(got.id)
    if first is None:
        kept[got.id] = (got, where)
    elif target.fold_path(first[0].folder) != target.fold_path(got.folder):
        problems.add(f"{where} names the same game as {first[1]}, but in another folder ({got.folder}), so only the "
                     f"first is listed")


# ---------------------------------------------------------------- the legacy launcher ---

def _manifest_files(folder: str, problems: stores.Problems) -> list[tuple[str, os.stat_result]]:
    """The folder's manifests as (path, the listing's stat), sorted by name: every regular file at its top named
    <name>.item in any case (the launcher's Pending folder is not looked into). [] when the folder does not exist,
    as when the legacy launcher is not installed; [] with the problem named when it cannot be listed or holds more
    than MAX_ENTRIES entries or MAX_ITEMS manifests."""
    found: list[tuple[str, os.stat_result]] = []
    try:
        with stores.scandir(folder) as it:
            for n, entry in enumerate(it, 1):
                if n > MAX_ENTRIES:
                    problems.add(f"{folder} holds more than {MAX_ENTRIES} files and folders, so it was not read")
                    return []
                if entry.name.casefold().endswith(".item") and entry.is_file(follow_symlinks=False):
                    found.append((entry.path, entry.stat(follow_symlinks=False)))
    except FileNotFoundError:
        return []
    except OSError as exc:
        problems.add(stores.failure(exc, folder, "read"))
        return []
    if len(found) > MAX_ITEMS:
        problems.add(f"{folder} holds more than {MAX_ITEMS} .item files, so it was not read")
        return []
    return sorted(found, key=lambda f: (f[0].casefold(), f[0]))


def _legacy(folder: str, problems: stores.Problems,
            taken: set[str]) -> tuple[dict[str, tuple[Game, str]], dict[str, str]]:
    """The legacy launcher's games (id -> (Game, the manifest's path)) and titles (AppName -> its DisplayName,
    cleaned: for the Preview's records, which hold none), from the manifests in `folder` by file name, the first of
    two titles kept. Each manifest is judged as soon as it is read and then let go, so no more than one is ever held:
    another account's folder of small JSON objects would otherwise take gigabytes. A manifest that cannot be read is
    named, before the games' own problems; when together they come to more than MAX_ALL_ITEMS_BYTES, none is taken
    and the folder is named."""
    named: list[str] = []
    judged: list[tuple[Game | str | None, str]] = []
    titles: dict[str, str] = {}
    total = 0
    for path, st in _manifest_files(folder, problems):
        if getattr(st, "st_file_attributes", 0) & gamefiles.ONLINE_ONLY:     # from the listing: it is never opened
            named.append(f"{path} is {gamefiles.WHY_ONLINE}")
            continue
        try:
            text = stores.read_text(Path(path), MAX_ITEM_BYTES)
        except ValueError as exc:
            named.append(str(exc))
            continue
        total += len(text.encode("utf-8"))
        if total > MAX_ALL_ITEMS_BYTES:
            problems.add(f"the .item files in {folder} come to more than {MAX_ALL_ITEMS_BYTES} bytes, so none was "
                         f"read")
            return {}, {}
        if not text.strip():
            named.append(f"{path} is empty")
            continue
        try:
            obj = _json(text)
        except ValueError as exc:
            named.append(f"{path} was not read: {exc}")
            continue
        if not isinstance(obj, dict):
            named.append(f"{path} does not hold one JSON object, so it was not read")
            continue
        app, name = obj.get("AppName"), stores.name(obj.get("DisplayName"))
        if isinstance(app, str) and name:
            titles.setdefault(app, name)
        judged.append((_manifest_game(path, obj, taken), path))     # nothing of obj is kept but this
    for text in named:
        problems.add(text)
    old: dict[str, tuple[Game, str]] = {}
    for got, path in judged:
        _keep(old, got, path, problems)
    return old, titles


def _manifest_game(path: str, obj: dict, taken: set[str]) -> Game | str | None:
    """The game a legacy manifest names: a Game; None for no game Play starts (an add-on, Unreal Engine, an install
    not finished, a plugin or an app) and for one the Preview lists already; else a str that says why not.

    An add-on is told by its AppCategories, as Playnite tells it: "addons", and not "addons/launchable". Its
    MainGameAppName is no sure sign: today's launcher writes "" there for a base game, and Fortnite's own manifest
    names its editor there once the editor is installed. So MainGameAppName is read only in a manifest without
    AppCategories, where one that names another app is an add-on (and one that is not text is named)."""
    app = obj.get("AppName")
    namespace = obj.get("CatalogNamespace")
    if namespace == ENGINE_NAMESPACE:
        return None                         # Unreal Engine itself
    unfinished = obj.get("bIsIncompleteInstall", False)
    if not isinstance(unfinished, bool):
        return f"{path}: its bIsIncompleteInstall is {stores.shown(unfinished)}, not true or false, so its game is " \
               f"not listed"
    if unfinished:
        return None
    if "AppCategories" in obj:
        kinds = obj["AppCategories"]
        if not isinstance(kinds, list) or not all(isinstance(k, str) for k in kinds):
            return f"{path}: its AppCategories is not a list of words, so its game is not listed"
        if "addons" in kinds and "addons/launchable" not in kinds:
            return None                     # an add-on
        if "games" not in kinds:
            return None                     # a plugin, an engine or an app
    else:
        main = obj.get("MainGameAppName")   # absent in some manifests, as null would be
        if main is not None and not isinstance(main, str):
            return f"{path}: its MainGameAppName is {stores.shown(main)}, not text, so its game is not listed"
        if main not in (None, "", app):
            return None                     # an add-on, by the name of its base game's app
    return _game(path, namespace, obj.get("CatalogItemId"), app, obj.get("InstallLocation"),
                 stores.name(obj.get("DisplayName")) or "", taken)


# ---------------------------------------------------------------- the Preview ---

def _not_a_record(rec: object) -> str | None:
    """Why a line after the header is neither a record nor a tombstone, in plain words; None when it is one."""
    if not isinstance(rec, dict):
        return "it does not hold one JSON object"
    iid = rec.get("installationId")
    if not isinstance(iid, str) or not INSTALLATION_ID.fullmatch(iid):
        return f"its installationId is {stores.shown(iid)}, not 32 hexadecimal digits in upper case"
    deleted = rec.get("deleted", False)
    if not isinstance(deleted, bool):
        return f"its deleted is {stores.shown(deleted)}, not true or false"
    return None


def _journal(path: str, problems: stores.Problems) -> list[tuple[int, dict]]:
    """The Preview's journal replayed top to bottom: (line number, record) for each install it still lists, in the
    order of their last lines. [] when the file does not exist, as when the Preview is not installed; [] with the
    problem named when it cannot be read, holds more than MAX_LINES lines or does not start with the header
    lag-doctor knows. A blank line is nothing. A line that cannot be read is named and changes nothing, but for a
    half-written last line (no line break ends it), which the launcher was still writing: that one is passed over
    quietly."""
    try:
        st = stores.stat(path)
    except FileNotFoundError:
        return []
    except OSError as exc:
        problems.add(stores.failure(exc, path, "read"))
        return []
    if not stat.S_ISREG(st.st_mode):
        problems.add(f"{path} is not a file, so it was not read")
        return []
    try:
        text = stores.read_text(Path(path), MAX_JSONL_BYTES)
    except ValueError as exc:
        problems.add(str(exc))
        return []
    lines = text.split("\n", MAX_LINES + 1)  # past MAX_LINES + 1 line breaks the rest stays one part: too many
    if lines[-1] == "":
        lines.pop()                         # a line break ends the last line
        half = 0
    else:
        half = len(lines)                   # the last line has none: it may be half-written
    if len(lines) > MAX_LINES:
        problems.add(f"{path} holds more than {MAX_LINES} lines, so it was not read")
        return []
    if not lines:
        return []                           # nothing written yet
    try:
        head = _json(lines[0], line=True)
    except ValueError as exc:
        if half != 1:
            problems.add(f"{path}, line 1 (the header) was not read: {exc}, so the file was not read")
        return []
    version = head.get("version") if isinstance(head, dict) else None
    if not isinstance(head, dict) or "installationId" in head or type(version) is not int \
            or version != HEADER_VERSION:   # type, not isinstance: True is 1 to Python, and not a version
        problems.add(f"{path} does not start with the header lag-doctor knows (version {HEADER_VERSION}), so it was "
                     f"not read")
        return []
    live: dict[str, tuple[int, dict]] = {}
    for n, line in enumerate(lines[1:], 2):
        if not line.strip():
            continue                        # a blank line (one more line break at the end, say) is nothing
        try:
            rec = _json(line, line=True)
        except ValueError as exc:
            if n != half:
                problems.add(f"{path}, line {n} was not read: {exc}")
            continue
        why = _not_a_record(rec)
        if why is not None:
            problems.add(f"{path}, line {n} was not read: {why}")
            continue
        iid = rec["installationId"]
        live.pop(iid, None)                 # a record replaces the one before it whole, and moves to the end
        if not rec.get("deleted", False):
            live[iid] = (n, rec)
    return list(live.values())


def _catalog(path: str, problems: stores.Problems) -> dict[str, str]:
    """Catalog item id -> its title, cleaned (stores.name), from the Preview's catalog cache, which holds
    {"items": {<catalog item id>: {"item": {"title": ...}}}} (SKIF and ludusavi read it so). It is a cache: {} when
    it is not there, and {} with the problem named when it cannot be read, its games then titled by their folders'
    names. An entry of another shape gives no title, quietly."""
    then = "the Preview's games are titled by their folders' names"
    try:
        st = stores.stat(path)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        problems.add(f"{stores.failure(exc, path, 'read')}; {then}")
        return {}
    if not stat.S_ISREG(st.st_mode):
        problems.add(f"{path} is not a file, so it was not read; {then}")
        return {}
    try:
        text = stores.read_text(Path(path), MAX_CATALOG_BYTES)
    except ValueError as exc:
        problems.add(f"{exc}; {then}")
        return {}
    try:
        obj = _json(text)
    except ValueError as exc:
        problems.add(f"{path} was not read: {exc}; {then}")
        return {}
    items = obj.get("items") if isinstance(obj, dict) else None
    if not isinstance(items, dict):
        problems.add(f"{path} holds no catalog items; {then}")
        return {}
    if len(items) > MAX_CATALOG_ITEMS:
        problems.add(f"{path} holds more than {MAX_CATALOG_ITEMS} catalog items; {then}")
        return {}
    out: dict[str, str] = {}
    for key, entry in items.items():
        item = entry.get("item") if isinstance(entry, dict) else None
        name = stores.name(item.get("title")) if isinstance(item, dict) else None
        if name:
            out[key] = name
    return out


def _record_game(where: str, rec: dict, titles: dict[str, str],
                 catalog: Callable[[], dict[str, str]]) -> Game | str | None:
    """The game a Preview record names: a Game; None for no game Play starts (an add-on, an install in progress,
    Unreal Engine); else a str that says why not. Its title: the Preview's catalog's (catalog() reads it the first
    time a record comes this far), else the legacy manifest's of the same app, else its folder's name."""
    if rec.get("parentInstallationId") is not None:
        return None                         # an add-on: it shares its base game's folder
    complete = rec.get("complete")
    if complete is False:
        return None                         # a download, an update or an uninstall in progress
    if complete is not True:
        return f"{where}: its complete is {stores.shown(complete)}, not true or false, so its game is not listed"
    namespace, item, app = rec.get("namespace"), rec.get("itemId"), rec.get("artifactId")
    if namespace == ENGINE_NAMESPACE:
        return None                         # Unreal Engine itself
    name = (catalog().get(item, "") if isinstance(item, str) else "") or \
        (titles.get(app, "") if isinstance(app, str) else "")
    return _game(where, namespace, item, app, rec.get("dir"), name)


def _record_id(rec: dict) -> str | None:
    """The id a Preview record names, listed as a game or not; None when its ids are not all text. A legacy manifest
    of that id is then passed over: the Preview's record of a game is the newer one, even while it says that the game
    is being updated."""
    parts = (rec.get("namespace"), rec.get("itemId"), rec.get("artifactId"))
    return ":".join(parts) if all(isinstance(p, str) for p in parts) else None


# ---------------------------------------------------------------- both ---

def _program_data() -> str:
    """%ProgramData%, as Windows names it (folders.program_data), looked up when called: the tests replace this."""
    return folders.program_data()


def _local_app_data() -> str:
    """The player's %LOCALAPPDATA% (folders.local_app_data), looked up when called: the tests replace this."""
    return folders.local_app_data()


def _default(where: Callable[[], str], parts: tuple[str, ...], what: str, problems: stores.Problems) -> str | None:
    """A launcher's file or folder under one of the user's own folders; None, with the problem named, when Windows
    cannot say where that folder is."""
    try:
        return os.path.join(where(), *parts)
    except OSError:
        problems.add(f"Windows did not say where {what} is, so the games of {problems.source} were not read")
        return None


def read(manifests: str | None = None, installations: str | None = None) -> Library:
    """Epic's installed games, from the legacy launcher's manifests folder and the Preview's installations.jsonl: by
    default where the launchers keep them (%ProgramData%\\Epic\\EpicGamesLauncher\\Data\\Manifests and
    %LOCALAPPDATA%\\com.epicgames.eglv2\\installations.jsonl). A folder or file that does not exist is no problem:
    that launcher is not installed.

    The Preview's games come first, in the order of their last lines, then the legacy launcher's, by file name. A game
    both list is listed once, with the Preview's folder, and a game the Preview has a record of but does not list (an
    update in progress) is not listed from a legacy manifest either. A Preview game's title is its catalog cache's
    (cache\\prod\\catalog-cache.json beside the journal, read once, when the first record of a game comes to its
    title), else the legacy manifest's, else its folder's name. Each game's id is "<namespace>:<catalog item
    id>:<app name>", and its link is launch.epic_link's. Problems: the Preview's, then the legacy launcher's.
    """
    legacy, preview = stores.Problems(LEGACY), stores.Problems(PREVIEW)
    if manifests is None:
        manifests = _default(_program_data, MANIFESTS, "the ProgramData folder", legacy)
    if installations is None:
        installations = _default(_local_app_data, INSTALLATIONS, "the local AppData folder", preview)
    records = _journal(installations, preview) if installations is not None else []
    taken = {gid for gid in (_record_id(rec) for _, rec in records) if gid is not None}
    old, titles = _legacy(manifests, legacy, taken) if manifests is not None else ({}, {})
    loaded: list[dict[str, str]] = []

    def catalog() -> dict[str, str]:
        if not loaded:
            loaded.append(_catalog(os.path.join(os.path.dirname(installations), *CATALOG), preview))
        return loaded[0]

    kept: dict[str, tuple[Game, str]] = {}
    for n, rec in records:
        where = f"{installations}, line {n}"
        _keep(kept, _record_game(where, rec, titles, catalog), where, preview)
    return Library(games=[g for g, _ in kept.values()] + [g for g, _ in old.values()],
                   problems=preview.lines() + legacy.lines())
