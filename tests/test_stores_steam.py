"""Steam's installed games (lagdoctor/stores/steam.py), and the KeyValues reader under it (stores/keyvalues.py).

Every Steam install here is made up, in the test's own tmp_path: made-up games with made-up app ids (4.1 billion and
up, far past any Steam app), never this PC's Steam files. The registry is faked behind steam's own seams for every
test (the registry fixture is autouse), and steam.py names nothing of winreg's but what reads a value, so no test
reads or changes the real one.
"""
from __future__ import annotations

import ctypes
import os
import re
import winreg
from ctypes import wintypes
from pathlib import Path

import pytest

from lagdoctor import stores
from lagdoctor.capture import target
from lagdoctor.stores import keyvalues, launch, steam
from lagdoctor.winapi import storage

BS = "\\"
HKCU, HKLM = winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE
STEAM_KEY = (HKCU, r"Software\Valve\Steam", "SteamPath")
WOW_KEY = (HKLM, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath")
NATIVE_KEY = (HKLM, r"SOFTWARE\Valve\Steam", "InstallPath")
NOT_LOCAL = "is not on a local fixed or removable disk"


def kv_escape(text: str) -> str:
    return text.replace(BS, BS + BS).replace('"', BS + '"').replace("\n", BS + "n").replace("\t", BS + "t")


def kv_text(tree: dict, depth: int = 0) -> str:
    """KeyValues text as Steam writes it: a tab per level, two tabs between a key and its value, each brace on its own
    line, and backslashes, quotes, line breaks and tabs escaped. A value of None leaves its key out."""
    pad = "\t" * depth
    out = []
    for key, value in tree.items():
        if value is None:
            continue
        if isinstance(value, dict):
            out.append(f'{pad}"{kv_escape(key)}"\n{pad}{{\n{kv_text(value, depth + 1)}{pad}}}\n')
        else:
            out.append(f'{pad}"{kv_escape(key)}"\t\t"{kv_escape(value)}"\n')
    return "".join(out)


def padded(text: str, size: int) -> bytes:
    """`text` and a // comment line that bring it to exactly `size` bytes."""
    body = text.encode("utf-8")
    fill = size - len(body) - len(b"//\n")
    assert fill >= 0
    return body + b"//" + b"x" * fill + b"\n"


class World:
    """A made-up Steam install under tmp_path: Steam's own folder, other libraries, and the games in each."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.steam = self.library("Steam")

    def library(self, name: str) -> Path:
        lib = self.root / name
        (lib / "steamapps" / "common").mkdir(parents=True)
        return lib

    def manifest(self, library: Path, appid: str) -> Path:
        return library / "steamapps" / f"appmanifest_{appid}.acf"

    def game(self, library: Path, appid: str, name: str, *, installdir: str | None = None, flags: str = "4",
             folder: bool = True, fields: dict | None = None, root: str = "AppState") -> Path:
        """An appmanifest as Steam writes one, with `fields` changed (None leaves a key out), and its game's folder
        under steamapps\\common unless `folder` is False."""
        installdir = name if installdir is None else installdir
        state = {"appid": appid, "universe": "1", "LauncherPath": str(self.steam / "steam.exe"), "name": name,
                 "StateFlags": flags, "installdir": installdir, "LastUpdated": "1700000000", "SizeOnDisk": "4096",
                 "buildid": "7", "LastOwner": "0", "InstalledDepots": {"4100000001": {"manifest": "9", "size": "4096"}},
                 "UserConfig": {"language": "english"}, "MountedConfig": {}}
        state.update(fields or {})
        path = self.manifest(library, appid)
        path.write_bytes(kv_text({root: state}).encode("utf-8"))
        if folder:
            (library / "steamapps" / "common" / installdir).mkdir()
        return path

    def library_list(self, entries: dict, root: str = "libraryfolders") -> Path:
        path = self.steam / "steamapps" / "libraryfolders.vdf"
        path.write_bytes(kv_text({root: entries}).encode("utf-8"))
        return path

    def entry(self, library: Path | str, **more: str) -> dict:
        """One block of today's libraryfolders.vdf."""
        return {"path": str(library), "label": "", "contentid": "5551234", "totalsize": "0",
                "update_clean_bytes_tally": "0", "time_last_update_verified": "0", **more, "apps": {"4100000010": "1"}}

    def common(self, library: Path, installdir: str) -> str:
        return target.check_folder(str(library / "steamapps" / "common" / installdir))


def vdf_of(w: World) -> str:
    return os.path.join(target.check_folder(str(w.steam)), "steamapps", "libraryfolders.vdf")


def titles(lib: stores.Library) -> list[str]:
    return [g.title for g in lib.games]


# ------------------------------------------------------------------------------------- libraries ---

def test_todays_library_list_gives_the_games_in_steams_folder_and_in_each_library(tmp_path):
    w = World(tmp_path)
    other = w.library("SteamLibrary")
    w.game(w.steam, "4100000020", "Harbor Lights")
    w.game(w.steam, "4100000010", "Lantern Drift")
    w.game(other, "4100000030", "Quiet Orbit")
    w.library_list({"contentstatsid": "-4100000", "0": w.entry(w.steam), "1": w.entry(other)})
    lib = steam.read(str(w.steam))
    assert lib.problems == []
    assert lib.games == [
        stores.Game("steam", "4100000010", "Lantern Drift", w.common(w.steam, "Lantern Drift"),
                    "steam://rungameid/4100000010"),
        stores.Game("steam", "4100000020", "Harbor Lights", w.common(w.steam, "Harbor Lights"),
                    "steam://rungameid/4100000020"),
        stores.Game("steam", "4100000030", "Quiet Orbit", w.common(other, "Quiet Orbit"),
                    "steam://rungameid/4100000030")]
    assert {launch.store_of(g.link) for g in lib.games} == {"steam"}


def test_the_library_list_from_before_2021_names_each_library_by_its_path_alone(tmp_path):
    w = World(tmp_path)
    old = w.library("Old Library")
    w.game(w.steam, "4100000010", "Lantern Drift")
    w.game(old, "4100000020", "Harbor Lights")
    w.library_list({"TimeNextStatsReport": "1450000000", "ContentStatsID": "-4100000", "1": str(old)},
                   root="LibraryFolders")
    lib = steam.read(str(w.steam))
    assert lib.problems == []
    assert [(g.title, g.folder) for g in lib.games] == [("Lantern Drift", w.common(w.steam, "Lantern Drift")),
                                                        ("Harbor Lights", w.common(old, "Harbor Lights"))]


def test_steams_own_folder_counts_once_and_first_however_the_list_spells_it(tmp_path):
    w = World(tmp_path)
    other = w.library("SteamLibrary")
    w.game(w.steam, "4100000010", "Lantern Drift")
    w.game(other, "4100000020", "Harbor Lights")
    shouty = str(w.steam).upper().replace(BS, "/") + "/"
    w.library_list({"0": w.entry(other), "1": w.entry(shouty), "2": w.entry(str(other).lower())})
    lib = steam.read(str(w.steam))
    assert lib.problems == []
    assert titles(lib) == ["Lantern Drift", "Harbor Lights"]


def test_a_library_steam_marks_unmounted_is_passed_over_and_any_other_mark_is_named(tmp_path):
    w = World(tmp_path)
    away, here, odd, block = (w.library(n) for n in ("Away", "Here", "Odd", "Block"))
    w.game(away, "4100000010", "Far Shore")
    w.game(here, "4100000020", "Near Shore")
    w.game(odd, "4100000030", "Odd Shore")
    w.game(block, "4100000040", "Block Shore")
    entries = {"0": w.entry(w.steam), "1": w.entry(away, mounted="0"), "2": w.entry(here, mounted="1"),
               "3": w.entry(odd, mounted="yes")}
    entries["4"] = {**w.entry(block), "Mounted": {"x": "1"}}
    w.library_list(entries)
    lib = steam.read(str(w.steam))
    assert titles(lib) == ["Near Shore"]
    vdf = vdf_of(w)
    assert lib.problems == [f"Steam's library 3 in {vdf} was skipped: its mounted value is 'yes', not 0 or 1",
                            f"Steam's library 4 in {vdf} was skipped: its mounted value is a block, not 0 or 1"]


def test_keys_in_the_library_list_compare_without_case(tmp_path):
    w = World(tmp_path)
    away, here = w.library("Away"), w.library("Here")
    w.game(away, "4100000010", "Far Shore")
    w.game(here, "4100000020", "Near Shore")
    w.library_list({"1": {"PATH": str(away), "MOUNTED": "0"}, "2": {"Path": str(here)}}, root="LIBRARYFOLDERS")
    lib = steam.read(str(w.steam))
    assert lib.problems == [] and titles(lib) == ["Near Shore"]


def test_steams_own_folder_is_searched_even_without_its_library_list(tmp_path):
    w = World(tmp_path)
    w.game(w.steam, "4100000010", "Lantern Drift")
    lib = steam.read(str(w.steam))
    assert titles(lib) == ["Lantern Drift"]
    assert lib.problems == [f"Only Steam's own folder was searched for games: Steam's list of its libraries, "
                            f"{vdf_of(w)}, is missing"]


@pytest.mark.parametrize("content, says", [
    (b'"libraryfolders"\n{\n\t"1"\t\tunquoted\n}\n', "is not written the way Steam writes it (an unquoted token"),
    (b'"libraryfolders"\n{\n\t"1"\t\t"D:\\x"\n}\n', "is not written the way Steam writes it (an escape Steam"),
    (b'"libraryfolders"\n{\n\t"1"\t\t"caf\xe9"\n}\n', "is not UTF-8 text"),
    (b'"other"\n{\n}\n', "holds no list of libraries"),
    (b'"libraryfolders"\t\t"C:\\\\Games"\n', "holds no list of libraries"),
])
def test_a_library_list_that_cannot_be_read_leaves_steams_own_folder_searched(tmp_path, content, says):
    w = World(tmp_path)
    w.game(w.steam, "4100000010", "Lantern Drift")
    (w.steam / "steamapps" / "libraryfolders.vdf").write_bytes(content)
    lib = steam.read(str(w.steam))
    assert titles(lib) == ["Lantern Drift"]
    [problem] = lib.problems
    assert problem.startswith("Only Steam's own folder was searched for games: ") and vdf_of(w) in problem
    assert says in problem


def test_a_library_list_of_a_mebibyte_is_read_and_a_byte_more_is_not(tmp_path):
    w = World(tmp_path)
    other = w.library("SteamLibrary")
    w.game(w.steam, "4100000010", "Lantern Drift")
    w.game(other, "4100000020", "Harbor Lights")
    vdf = w.library_list({"0": w.entry(w.steam), "1": w.entry(other)})
    text = vdf.read_text(encoding="utf-8")
    vdf.write_bytes(padded(text, 1 << 20))
    assert titles(steam.read(str(w.steam))) == ["Lantern Drift", "Harbor Lights"]
    vdf.write_bytes(padded(text, (1 << 20) + 1))
    lib = steam.read(str(w.steam))
    assert titles(lib) == ["Lantern Drift"]
    assert lib.problems == [f"Only Steam's own folder was searched for games: {vdf_of(w)} is larger than 1048576 "
                            "bytes, so it was not read"]


def test_a_library_that_is_not_a_plain_local_folder_is_named_and_the_others_are_read(tmp_path):
    w = World(tmp_path)
    good = w.library("Good")
    w.game(good, "4100000010", "Lantern Drift")
    windows = os.environ.get("SystemRoot") or "C:\\Windows"
    w.library_list({
        "0": w.entry(w.steam),
        "1": w.entry(BS * 2 + "server" + BS + "share" + BS + "SteamLibrary"),
        "2": "SteamLibrary",
        "3": str(good) + BS + ".." + BS + "Good",
        "4": w.entry(windows + BS + "SteamLibrary"),
        "5": {"label": "no path"},
        "6": "C:" + BS,
        "7": w.entry("C:" + BS + "x" * 300),
        "8": w.entry(good),
    })
    lib = steam.read(str(w.steam))
    assert titles(lib) == ["Lantern Drift"]
    vdf = vdf_of(w)
    assert [p.split(" was skipped: ")[0] for p in lib.problems] == \
        [f"Steam's library {n} in {vdf}" for n in "1234567"]
    by_key = {p.split(" in ")[0][-1]: p.split(" was skipped: ")[1] for p in lib.problems}
    assert "is not a folder on a local drive" in by_key["1"] and "is not a folder on a local drive" in by_key["2"]
    assert "has a .. part" in by_key["3"]
    assert "is in the Windows folder" in by_key["4"]
    assert by_key["5"] == "it names no folder"
    assert "is a whole drive" in by_key["6"]
    assert by_key["7"] == "its folder is longer than 260 characters"


def test_a_library_on_a_network_drive_is_never_opened(tmp_path, monkeypatch):
    w = World(tmp_path)
    w.game(w.steam, "4100000010", "Lantern Drift")
    real_type, real_scandir = storage.drive_type, stores._scandir
    monkeypatch.setattr(storage, "drive_type", lambda root: 4 if root.upper() == "Z:" + BS else real_type(root))
    listed = []
    monkeypatch.setattr(stores, "_scandir", lambda p: (listed.append(os.fspath(p)), real_scandir(p))[1])
    w.library_list({"0": w.entry(w.steam), "1": w.entry("Z:" + BS + "SteamLibrary")})
    lib = steam.read(str(w.steam))
    assert titles(lib) == ["Lantern Drift"]
    assert lib.problems == [f"Steam's library 1 in {vdf_of(w)} was skipped: Z:{BS}SteamLibrary {NOT_LOCAL}"]
    assert listed == [os.path.join(target.check_folder(str(w.steam)), "steamapps")]


def test_at_most_64_libraries_are_searched_steams_own_among_them(tmp_path):
    w = World(tmp_path)
    w.game(w.steam, "4100000010", "Game 0")
    entries = {"0": w.entry(w.steam)}
    for i in range(1, 71):
        lib = w.library(f"Library {i}")
        w.game(lib, str(4100000010 + 10 * i), f"Game {i}")
        entries[str(i)] = w.entry(lib)
    w.library_list(entries)
    lib = steam.read(str(w.steam))
    assert titles(lib) == [f"Game {i}" for i in range(64)]
    assert lib.problems == [f"{vdf_of(w)} lists more than 64 libraries, so only the first 64 were searched for games"]


def test_a_library_that_is_missing_or_holds_no_steamapps_folder_is_named(tmp_path):
    w = World(tmp_path)
    gone, bare, odd = tmp_path / "Gone", tmp_path / "Bare", tmp_path / "Odd"
    bare.mkdir()
    odd.mkdir()
    (odd / "steamapps").write_bytes(b"a file, not a folder")
    w.game(w.steam, "4100000010", "Lantern Drift")
    w.library_list({"0": w.entry(w.steam), "1": w.entry(gone), "2": w.entry(bare), "3": w.entry(odd)})
    lib = steam.read(str(w.steam))
    assert titles(lib) == ["Lantern Drift"]
    assert lib.problems[:2] == [f"The Steam library {gone} is missing, so its games were not listed",
                                f"The Steam library {bare} has no steamapps folder, so no games were found in it"]
    assert lib.problems[2].startswith(f"{odd / 'steamapps'} could not be read (")
    assert lib.problems[2].endswith("), so its games were not listed") and len(lib.problems) == 3


def test_a_steamapps_folder_with_too_many_entries_is_not_looked_through(tmp_path, monkeypatch):
    monkeypatch.setattr(steam, "MAX_STEAMAPPS_ENTRIES", 3)
    w = World(tmp_path)
    full = w.library("Full")
    w.game(w.steam, "4100000010", "Lantern Drift")              # with common and the list: 3 entries, all read
    w.game(full, "4100000020", "Harbor Lights")
    (full / "steamapps" / "downloading").mkdir()                 # with common and the game: 3 entries
    w.library_list({"0": w.entry(w.steam), "1": w.entry(full)})
    assert titles(steam.read(str(w.steam))) == ["Lantern Drift", "Harbor Lights"]
    (full / "steamapps" / "temp").mkdir()                        # a fourth
    lib = steam.read(str(w.steam))
    assert titles(lib) == ["Lantern Drift"]
    assert lib.problems == [f"The Steam library {full} was skipped: {full / 'steamapps'} holds more than 3 entries, "
                            "so it was not looked through"]


# ---------------------------------------------------------------------------------------- games ---

BAD_STATES = [                  # fields of the second game, and what its problem says after "was skipped: "
    ({"appid": "4100000030"}, "it names the app '4100000030', but its file is named for the app 4100000020"),
    ({"appid": "04100000020"}, "it names the app '04100000020', but its file is named for the app 4100000020"),
    ({"appid": None}, "it names no app"),
    ({"appid": {"4100000020": "1"}}, "it names the app a block, but its file is named for the app 4100000020"),
    ({"StateFlags": None}, "it gives no install state"),
    ({"StateFlags": "four"}, "its install state ('four') is not a number"),
    ({"StateFlags": "-4"}, "its install state ('-4') is not a number"),
    ({"StateFlags": "4.0"}, "its install state ('4.0') is not a number"),
    ({"StateFlags": "4294967300"}, "its install state ('4294967300') is not a number"),
    ({"StateFlags": "00000000004"}, "its install state ('00000000004') is not a number"),
    ({"StateFlags": {"4": "4"}}, "its install state (a block) is not a number"),
    ({"installdir": None}, "it names no install folder"),
    ({"installdir": ""}, "the name it gives its install folder ('') is empty"),
    ({"installdir": {"a": "b"}}, "the name it gives its install folder (a block) is not text"),
    ({"installdir": "Games" + BS + "Harbor Lights"}, "is a path, not one folder's name"),
    ({"installdir": "Games/Harbor Lights"}, "is a path, not one folder's name"),
    ({"installdir": BS + "Harbor Lights"}, "is a path, not one folder's name"),
    ({"installdir": ".."}, "('..') is not a folder's name"),
    ({"installdir": "."}, "('.') is not a folder's name"),
    ({"installdir": "Harbor Lights."}, "is a name Windows would not keep as written (it ends in a dot or a space)"),
    ({"installdir": "Harbor Lights "}, "is a name Windows would not keep as written (it ends in a dot or a space)"),
    ({"installdir": "Harbor\tLights"}, "is a name with characters that cannot be shown"),
    ({"installdir": "C:"}, "('C:') is a name Windows does not allow"),
    ({"installdir": "Harbor?Lights"}, "is a name Windows does not allow"),
    ({"installdir": "x" * 256}, "is longer than 255 characters"),
    ({"installdir": "x" * 255}, "not a folder path of 3 to 260 printable characters"),
]


@pytest.mark.parametrize("fields, says", BAD_STATES)
def test_a_manifest_whose_fields_fail_their_grammar_is_named_and_the_others_are_read(tmp_path, fields, says):
    w = World(tmp_path)
    w.game(w.steam, "4100000010", "Lantern Drift")
    bad = w.game(w.steam, "4100000020", "Harbor Lights", folder=False, fields=fields)
    lib = steam.read(str(w.steam))
    assert titles(lib) == ["Lantern Drift"]
    [problem] = [p for p in lib.problems if "libraryfolders.vdf" not in p]
    assert problem.startswith(f"{bad} was skipped: ")
    assert says in problem


def test_a_refused_value_is_quoted_cut_short(tmp_path):
    w = World(tmp_path)
    bad = w.game(w.steam, "4100000020", "Harbor Lights", folder=False, fields={"installdir": "x" * 256})
    [problem] = [p for p in steam.read(str(w.steam)).problems if "libraryfolders.vdf" not in p]
    assert problem == (f"{bad} was skipped: the name it gives its install folder ('{'x' * 56}...) is longer than "
                       "255 characters")                           # stores.shown: 60 characters in all


def test_the_games_of_a_library_are_listed_by_app_id(tmp_path):
    w = World(tmp_path)
    w.game(w.steam, "4100000010", "Lantern Drift")
    w.game(w.steam, "999999990", "Harbor Lights")            # its file comes after the other in the folder's listing
    assert [e.name for e in os.scandir(w.steam / "steamapps") if e.name.endswith(".acf")] == \
        ["appmanifest_4100000010.acf", "appmanifest_999999990.acf"]
    assert [g.id for g in steam.read(str(w.steam)).games] == ["999999990", "4100000010"]


@pytest.mark.parametrize("flags", ["0", "1", "2", "1026", "1048576", "4294967291"])
def test_an_app_steam_has_not_fully_installed_is_passed_over_quietly(tmp_path, flags):
    w = World(tmp_path)
    w.game(w.steam, "4100000010", "Lantern Drift", flags=flags)
    lib = steam.read(str(w.steam))
    assert lib.games == []
    assert len(lib.problems) == 1 and "libraryfolders.vdf" in lib.problems[0]       # the list: nothing on the game


@pytest.mark.parametrize("flags", ["4", "6", "1028", "4294967295"])
def test_an_app_with_the_fully_installed_bit_is_listed_whatever_else_is_set(tmp_path, flags):
    w = World(tmp_path)
    w.game(w.steam, "4100000010", "Lantern Drift", flags=flags)
    assert titles(steam.read(str(w.steam))) == ["Lantern Drift"]


# Steam's own tools, by the ids their sources give (steam.py names them): Steamworks Common Redistributables and
# Steam for Linux, the Steam Linux Runtimes 1.0 to 4.0, Proton Experimental and Hotfix, and Proton 3.7 to 11.0.
TOOL_IDS = ["228980", "221410", "1070560", "1391110", "1628350", "4183110", "1493710", "2180100", "858280",
            "961940", "1054830", "1113280", "1245040", "1420170", "1580130", "1887720", "2348590", "2805730",
            "3658110", "4628710"]


def test_the_tools_skipped_are_exactly_these():
    assert steam.NOT_GAMES == frozenset(TOOL_IDS) and len(TOOL_IDS) == 20


@pytest.mark.parametrize("appid", TOOL_IDS)
def test_steams_redistributables_and_runtimes_are_not_games(tmp_path, appid):
    w = World(tmp_path)
    w.game(w.steam, "4100000010", "Lantern Drift")
    w.game(w.steam, appid, "Steamworks Shared")
    w.library_list({"0": w.entry(w.steam)})
    assert titles(steam.read(str(w.steam))) == ["Lantern Drift"]
    w.manifest(w.steam, appid).write_bytes(b"not even KeyValues")          # never read at all
    lib = steam.read(str(w.steam))
    assert titles(lib) == ["Lantern Drift"] and lib.problems == []


def test_a_game_whose_folder_is_missing_is_named_and_a_soundtrack_is_passed_over(tmp_path):
    w = World(tmp_path)
    w.library_list({"0": w.entry(w.steam)})
    w.game(w.steam, "4100000010", "Lantern Drift")
    w.game(w.steam, "4100000020", "Harbor Lights", folder=False)
    w.game(w.steam, "4100000030", "Harbor Lights Soundtrack", folder=False)
    (w.steam / "steamapps" / "music" / "Harbor Lights Soundtrack").mkdir(parents=True)
    lib = steam.read(str(w.steam))
    assert titles(lib) == ["Lantern Drift"]
    assert lib.problems == [f"Harbor Lights (Steam app 4100000020) is listed as installed, but its folder "
                            f"{w.common(w.steam, 'Harbor Lights')} is missing"]


def test_a_game_without_a_name_is_titled_by_its_folder_and_its_name_is_cleaned(tmp_path):
    w = World(tmp_path)
    w.game(w.steam, "4100000010", "Lantern Drift", fields={"name": None})
    w.game(w.steam, "4100000020", "x", installdir="Harbor Lights", fields={"name": "Harbor\tLights" + "\n" * 3})
    w.game(w.steam, "4100000030", "Quiet Orbit", fields={"name": "y" * 300})
    lib = steam.read(str(w.steam))
    assert titles(lib)[:2] == ["Lantern Drift", "Harbor Lights"]
    assert len(titles(lib)[2]) == stores.MAX_TITLE_CHARS


def test_a_manifest_of_256_kibibytes_is_read_and_a_byte_more_is_not(tmp_path):
    w = World(tmp_path)
    path = w.game(w.steam, "4100000010", "Lantern Drift")
    text = path.read_text(encoding="utf-8")
    path.write_bytes(padded(text, 256 << 10))
    assert titles(steam.read(str(w.steam))) == ["Lantern Drift"]
    path.write_bytes(padded(text, (256 << 10) + 1))
    lib = steam.read(str(w.steam))
    assert lib.games == []
    assert f"A Steam game was skipped: {path} is larger than 262144 bytes, so it was not read" in lib.problems


@pytest.mark.parametrize("content, says", [
    (b'"AppState"\n{\n\t"appid"\t\t4100000010\n}\n', "it is not written the way Steam writes it (an unquoted token"),
    (b'"AppState"\n{\n\t"appid"\t\t"4100000010"\n', "it is not written the way Steam writes it (a block that never"),
    (b'"Other"\n{\n\t"appid"\t\t"4100000010"\n}\n', "it holds no app state"),
    (b'"AppState"\t\t"4100000010"\n', "it holds no app state"),
])
def test_a_manifest_that_is_not_steams_keyvalues_is_named(tmp_path, content, says):
    w = World(tmp_path)
    path = w.manifest(w.steam, "4100000010")
    path.write_bytes(content)
    lib = steam.read(str(w.steam))
    assert lib.games == []
    assert f"{path} was skipped: {says}" in [p[:len(f"{path} was skipped: {says}")] for p in lib.problems]


def test_a_manifest_that_is_not_utf8_or_not_a_plain_file_is_named(tmp_path):
    w = World(tmp_path)
    w.game(w.steam, "4100000010", "Lantern Drift")
    latin = w.manifest(w.steam, "4100000020")
    latin.write_bytes(kv_text({"AppState": {"appid": "4100000020", "name": "Caf\xe9"}}).encode("latin-1"))
    folder = w.manifest(w.steam, "4100000030")
    folder.mkdir()
    lib = steam.read(str(w.steam))
    assert titles(lib) == ["Lantern Drift"]
    assert f"A Steam game was skipped: {latin} is not UTF-8 text" in lib.problems
    assert f"{folder} was skipped: it is not a plain file" in lib.problems


def test_a_manifest_kept_online_only_is_named_and_never_read(tmp_path):
    w = World(tmp_path)
    path = w.game(w.steam, "4100000010", "Lantern Drift")
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.SetFileAttributesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    k32.SetFileAttributesW.restype = wintypes.BOOL
    assert k32.SetFileAttributesW(str(path), 0x1000 | 0x20), ctypes.get_last_error()     # OFFLINE | ARCHIVE
    lib = steam.read(str(w.steam))
    assert lib.games == []
    assert f"A Steam game was skipped: {path} is kept online only, so it was not read" in lib.problems


@pytest.mark.parametrize("name", ["appmanifest_0.acf", "appmanifest_04100000010.acf", "appmanifest_4294967296.acf"])
def test_a_manifest_whose_name_holds_no_app_id_is_named(tmp_path, name):
    w = World(tmp_path)
    path = w.steam / "steamapps" / name
    path.write_bytes(kv_text({"AppState": {"appid": name[12:-4], "StateFlags": "4", "installdir": "x"}}).encode())
    lib = steam.read(str(w.steam))
    assert lib.games == []
    assert f"{path} was skipped: its name holds no Steam app id" in lib.problems


def test_only_files_named_like_a_manifest_are_read_whatever_their_case(tmp_path):
    w = World(tmp_path)
    w.game(w.steam, "4100000010", "Lantern Drift")
    shouty = w.game(w.steam, "4100000020", "Harbor Lights")
    shouty.rename(shouty.with_name("AppManifest_4100000020.ACF"))
    for name in ("appmanifest_4100000030.acf.tmp", "appmanifest_.acf", "appmanifest_41a.acf", "xappmanifest_1.acf",
                 "appmanifest_4100000040.vdf", "appmanifest_+4100000050.acf"):
        (w.steam / "steamapps" / name).write_bytes(b"not KeyValues")
    lib = steam.read(str(w.steam))
    assert titles(lib) == ["Lantern Drift", "Harbor Lights"]
    assert len(lib.problems) == 1 and "libraryfolders.vdf" in lib.problems[0]


def test_an_app_in_two_libraries_is_kept_once_and_the_second_is_named(tmp_path):
    w = World(tmp_path)
    other, third = w.library("SteamLibrary"), w.library("Third")
    w.game(w.steam, "4100000010", "Lantern Drift")
    w.game(other, "4100000010", "Lantern Drift")
    w.game(other, "4100000020", "Harbor Lights", folder=False)        # a copy left behind, its folder gone
    w.game(third, "4100000020", "Harbor Lights")
    w.library_list({"0": w.entry(w.steam), "1": w.entry(other), "2": w.entry(third)})
    lib = steam.read(str(w.steam))
    assert [(g.title, g.folder) for g in lib.games] == [("Lantern Drift", w.common(w.steam, "Lantern Drift")),
                                                        ("Harbor Lights", w.common(third, "Harbor Lights"))]
    assert lib.problems == [
        f"Lantern Drift (Steam app 4100000010) is listed in two libraries: the one in "
        f"{w.common(w.steam, 'Lantern Drift')} was kept, and the one in {w.common(other, 'Lantern Drift')} "
        "was left out",
        f"Harbor Lights (Steam app 4100000020) is listed as installed, but its folder "
        f"{w.common(other, 'Harbor Lights')} is missing"]


def test_keys_in_a_manifest_compare_without_case(tmp_path):
    w = World(tmp_path)
    path = w.manifest(w.steam, "4100000010")
    path.write_bytes(kv_text({"APPSTATE": {"AppID": "4100000010", "Name": "Lantern Drift", "STATEFLAGS": "4",
                                           "InstallDir": "Lantern Drift"}}).encode())
    (w.steam / "steamapps" / "common" / "Lantern Drift").mkdir()
    w.library_list({"0": w.entry(w.steam)})
    lib = steam.read(str(w.steam))
    assert lib.problems == [] and titles(lib) == ["Lantern Drift"]


def test_at_most_4096_manifests_are_read_in_all_libraries_together(tmp_path, monkeypatch):
    monkeypatch.setattr(steam, "MAX_MANIFESTS", 2)
    w = World(tmp_path)
    other = w.library("SteamLibrary")
    w.game(w.steam, "4100000010", "Lantern Drift")
    w.game(w.steam, "4100000020", "Harbor Lights")
    w.game(other, "4100000030", "Quiet Orbit")
    w.library_list({"0": w.entry(w.steam), "1": w.entry(other)})
    lib = steam.read(str(w.steam))
    assert titles(lib) == ["Lantern Drift", "Harbor Lights"]
    assert lib.problems == ["Steam's libraries hold more than 2 app files, so the rest were not read"]


def test_problems_past_the_cap_are_counted_not_listed(tmp_path, monkeypatch):
    monkeypatch.setattr(stores, "MAX_PROBLEMS", 2)
    w = World(tmp_path)
    w.library_list({"0": w.entry(w.steam)})
    bad = [w.game(w.steam, str(4100000010 + 10 * i), f"Game {i}", folder=False) for i in range(4)]
    lib = steam.read(str(w.steam))
    assert lib.games == []
    assert lib.problems == [f"Game 0 (Steam app 4100000010) is listed as installed, but its folder "
                            f"{w.common(w.steam, 'Game 0')} is missing",
                            f"Game 1 (Steam app 4100000020) is listed as installed, but its folder "
                            f"{w.common(w.steam, 'Game 1')} is missing",
                            "2 more problems with the files of Steam, not listed here"]
    assert len(bad) == 4


def test_the_caps_are_the_documented_ones():
    assert (steam.MAX_LIBRARY_FILE_BYTES, steam.MAX_MANIFEST_BYTES, steam.MAX_LIBRARIES, steam.MAX_MANIFESTS,
            steam.MAX_STEAMAPPS_ENTRIES, steam.MAX_NAME_CHARS) == (1048576, 262144, 64, 4096, 20000, 255)


def test_one_problem_past_the_cap_is_counted_in_the_singular(tmp_path, monkeypatch):
    monkeypatch.setattr(stores, "MAX_PROBLEMS", 2)
    w = World(tmp_path)
    w.library_list({"0": w.entry(w.steam)})
    for i in range(3):
        w.game(w.steam, str(4100000010 + 10 * i), f"Game {i}", folder=False)
    assert steam.read(str(w.steam)).problems[-1] == "1 more problem with the files of Steam, not listed here"


def test_the_manifest_cap_is_named_once_and_no_further_library_is_listed(tmp_path, monkeypatch):
    monkeypatch.setattr(steam, "MAX_MANIFESTS", 2)
    w = World(tmp_path)
    second, third = w.library("Second"), w.library("Third")
    w.game(w.steam, "4100000010", "Game 1")
    w.game(w.steam, "4100000020", "Game 2")
    for library, first in ((second, 4100000030), (third, 4100000050)):
        w.game(library, str(first), f"Game {first}")
        w.game(library, str(first + 10), f"Game {first + 10}")
    w.library_list({"0": w.entry(w.steam), "1": w.entry(second), "2": w.entry(third)})
    listed, real_scandir = [], stores._scandir
    monkeypatch.setattr(stores, "_scandir", lambda p: listed.append(os.fspath(p)) or real_scandir(p))
    lib = steam.read(str(w.steam))
    assert titles(lib) == ["Game 1", "Game 2"]
    assert lib.problems == ["Steam's libraries hold more than 2 app files, so the rest were not read"]
    assert [os.path.basename(os.path.dirname(p)) for p in listed] == [w.steam.name, "Second"]   # never Third


def test_games_in_folders_named_in_any_script_are_listed(tmp_path):
    w = World(tmp_path)
    names = ["Lanterne D" + chr(0xE9) + "rive", chr(0x706F) + chr(0x7C60), "Kiln " + chr(0x2726)]
    for i, name in enumerate(names):
        w.game(w.steam, str(4100000010 + 10 * i), name)
    w.library_list({"0": w.entry(w.steam)})
    lib = steam.read(str(w.steam))
    assert lib.problems == [] and titles(lib) == names
    assert [g.folder for g in lib.games] == [w.common(w.steam, n) for n in names]


def test_only_numbered_keys_name_libraries(tmp_path):
    w = World(tmp_path)
    other = w.library("Other")
    w.game(other, "4100000010", "Lantern Drift")
    w.library_list({"0": w.entry(w.steam), "1x": w.entry(other), "x1": w.entry(other)})
    assert steam.read(str(w.steam)) == stores.Library([], [])


def test_steams_problems_are_one_line_each_and_cut_at_1000_characters(tmp_path):
    w = World(tmp_path)
    odd = w.root / ("Harbor" + chr(0x2028) + "Lights" + chr(0x202E))         # passes check_folder, cannot be shown
    w.library_list({"0": w.entry(w.steam), "1": w.entry(odd)})
    lib = steam.read(str(w.steam))
    shown = target.check_folder(str(odd)).replace(chr(0x2028), "?").replace(chr(0x202E), "?")
    assert lib.problems == [f"The Steam library {shown} is missing, so its games were not listed"]
    vdf = w.steam / "steamapps" / "libraryfolders.vdf"
    vdf.write_bytes(('"#a' + chr(13) + "b" + chr(0x2028) + 'c" "x"' + chr(10)).encode("utf-8"))
    lib = steam.read(str(w.steam))                                          # a key with a CR and a line separator
    assert len(lib.problems) == 1 and lib.problems[0].isprintable() and "#a" in lib.problems[0]


def test_a_library_moved_with_a_junction_is_read_through_it(tmp_path):
    import _winapi
    w = World(tmp_path)
    moved = w.library("Moved")
    w.game(moved, "4100000010", "Lantern Drift")
    link = tmp_path / "Library"
    _winapi.CreateJunction(str(moved), str(link))
    w.library_list({"0": w.entry(w.steam), "1": w.entry(link)})
    lib = steam.read(str(w.steam))
    assert lib.problems == [] and titles(lib) == ["Lantern Drift"]
    assert lib.games[0].folder == w.common(link, "Lantern Drift")         # named as the library list names it


def test_a_library_or_game_folder_whose_link_leads_to_a_share_is_named_and_never_opened(tmp_path, monkeypatch):
    import _winapi
    w = World(tmp_path)
    away, home = w.library("Away"), w.library("Home")
    w.game(away, "4100000010", "Lantern Drift")
    w.game(home, "4100000020", "Harbor Lights", folder=False)
    w.game(home, "4100000030", "Quiet Orbit")
    link = tmp_path / "Linked"
    _winapi.CreateJunction(str(away), str(link))
    game_link = home / "steamapps" / "common" / "Harbor Lights"
    _winapi.CreateJunction(str(tmp_path), str(game_link))
    share = BS + BS + "?" + BS + "UNC" + BS + "nas" + BS + "games"
    real_readlink = target._readlink
    monkeypatch.setattr(target, "_readlink",
                        lambda p: share if p in (str(link), str(game_link)) else real_readlink(p))
    looked, real_stat, real_scandir = [], stores._stat, stores._scandir
    monkeypatch.setattr(stores, "_stat", lambda p: looked.append(p) or real_stat(p))
    monkeypatch.setattr(stores, "_scandir", lambda p: looked.append(p) or real_scandir(p))
    w.library_list({"0": w.entry(w.steam), "1": w.entry(link), "2": w.entry(home)})
    lib = steam.read(str(w.steam))
    assert titles(lib) == ["Quiet Orbit"]
    assert lib.problems == [
        f"{link} is a link to {share}, which is not a path on a local drive, so its games were not listed",
        f"Harbor Lights (Steam app 4100000020) was skipped: {game_link} is a link to {share}, which is not a path on "
        f"a local drive"]
    assert not [p for p in looked if p.startswith(str(link)) or p.startswith(str(game_link))]


def test_a_game_folder_that_is_a_file_is_taken_as_missing(tmp_path):
    w = World(tmp_path)
    w.game(w.steam, "4100000010", "Lantern Drift", folder=False)
    (w.steam / "steamapps" / "common" / "Lantern Drift").write_bytes(b"not a folder")
    w.library_list({"0": w.entry(w.steam)})
    lib = steam.read(str(w.steam))
    assert lib.games == [] and lib.problems == [
        f"Lantern Drift (Steam app 4100000010) is listed as installed, but its folder "
        f"{w.common(w.steam, 'Lantern Drift')} is missing"]


def test_steams_own_folder_reached_through_a_link_onto_a_share_is_named(tmp_path, monkeypatch):
    import _winapi
    w = World(tmp_path)
    moved = tmp_path / "Moved steamapps"
    (w.steam / "steamapps").rename(moved)
    steamapps = w.steam / "steamapps"
    _winapi.CreateJunction(str(moved), str(steamapps))
    share = BS + BS + "?" + BS + "UNC" + BS + "nas" + BS + "steam"
    real = target._readlink
    monkeypatch.setattr(target, "_readlink", lambda p: share if p == str(steamapps) else real(p))
    why = f"{steamapps} is a link to {share}, which is not a path on a local drive"
    assert steam.read(str(w.steam)) == stores.Library([], [
        f"Only Steam's own folder was searched for games: {why}", f"{why}, so its games were not listed"])
    link = tmp_path / "Steam link"
    _winapi.CreateJunction(str(w.steam), str(link))
    monkeypatch.setattr(target, "_readlink", lambda p: share if p == str(link) else real(p))
    assert steam.read(str(link)).problems == [
        f"Steam's folder could not be used, so no Steam games were listed: {link} is a link to {share}, which is not "
        f"a path on a local drive"]


def test_a_library_windows_cannot_look_at_is_named(tmp_path, monkeypatch):
    w = World(tmp_path)
    locked = tmp_path / "Locked"                                   # its steamapps listing says not found
    w.library_list({"0": w.entry(w.steam), "1": w.entry(locked)})
    real = stores._stat

    def stat(path):
        if path == str(locked):
            raise PermissionError(13, "Access is denied")
        return real(path)

    monkeypatch.setattr(stores, "_stat", stat)
    assert steam.read(str(w.steam)).problems == [
        f"{locked} could not be looked at (Access is denied), so its games were not listed"]


# --------------------------------------------------------------------------- Steam's own folder ---

class FakeKey:
    def __init__(self, root: int, key: str) -> None:
        self.root, self.key = root, key

    def __enter__(self) -> FakeKey:
        return self

    def __exit__(self, *exc) -> bool:
        return False


class FakeRegistry:
    """winreg's OpenKey and QueryValueEx over {(root, key, value name): (value, type) or an OSError to raise}. It
    refuses any access but KEY_READ, and records every key it was asked to open."""

    def __init__(self) -> None:
        self.values: dict = {}
        self.opened: list[tuple[int, str, int]] = []

    def open_key(self, root, key, reserved=0, access=winreg.KEY_READ):
        self.opened.append((root, key, access))
        assert access == winreg.KEY_READ, f"{key} was opened for {access:#x}, not KEY_READ"
        if not any(r == root and k == key for r, k, _ in self.values):
            raise FileNotFoundError(2, "The system cannot find the file specified")
        return FakeKey(root, key)

    def query(self, handle, name):
        got = self.values.get((handle.root, handle.key, name))
        if got is None:
            raise FileNotFoundError(2, "The system cannot find the file specified")
        if isinstance(got, OSError):
            raise got
        return got


@pytest.fixture(autouse=True)
def registry(monkeypatch):
    """A fake registry behind steam's own seams (_open_key, _query_value), empty until a test fills it, for every test
    here: none reads this PC's registry, even one whose call goes wrong. test_the_reader_uses_winreg_only_to_read
    holds steam.py to reading."""
    fake = FakeRegistry()
    monkeypatch.setattr(steam, "_open_key", fake.open_key)
    monkeypatch.setattr(steam, "_query_value", fake.query)
    return fake


def registry_use(source: str) -> tuple[list[str], dict, set]:
    """How a module's source uses winreg: what it does with winreg other than winreg.<name>, which of winreg's calls
    it makes inside which function, and which of its constants it names."""
    import ast
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    owner = {}
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        for node in ast.walk(fn):
            owner.setdefault(node, fn.name)
    calls, constants, odd = {}, set(), []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "winreg":
            up = parents.get(node)
            if not (isinstance(up, ast.Attribute) and up.value is node):
                odd.append(f"line {node.lineno}: winreg itself")
            elif up.attr.isupper() or up.attr.startswith(("HKEY_", "KEY_", "REG_")):
                constants.add(up.attr)
            else:
                calls.setdefault(up.attr, set()).add(owner.get(up, "<module>"))
        if isinstance(node, (ast.Import, ast.ImportFrom)) and any(
                a.name == "winreg" and a.asname for a in node.names) or \
                isinstance(node, ast.ImportFrom) and node.module == "winreg":
            odd.append(f"line {node.lineno}: winreg imported under another name")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr" and \
                node.args and isinstance(node.args[0], ast.Name) and node.args[0].id == "winreg":
            odd.append(f"line {node.lineno}: getattr(winreg, ...)")
    return odd, calls, constants


READS = ([], {"OpenKey": {"_open_key"}, "QueryValueEx": {"_query_value"}, "ExpandEnvironmentStrings": {"_expand"}},
         {"KEY_READ", "REG_EXPAND_SZ", "HKEY_CURRENT_USER", "HKEY_LOCAL_MACHINE"})


def test_the_reader_uses_winreg_only_to_read():
    """steam.py names winreg only as winreg.<name>: the three reading calls each in its own seam (_open_key,
    _query_value, _expand, which the autouse registry fixture replaces), the constants anywhere, and nothing else of
    winreg's at all, under any name."""
    assert registry_use(Path(steam.__file__).read_bytes().decode("utf-8")) == READS


@pytest.mark.parametrize("line", [
    "import winreg as wr", "_R = winreg", "_W = getattr(winreg, 'SetValueEx')", "from winreg import OpenKey",
    "_W = winreg.SetValueEx", "def _peek():\n    return winreg.OpenKey(1, 'k')",
    "def _peek():\n    return winreg.QueryValueEx(1, 'v')",
])
def test_the_registry_scan_catches_each_way_around_it(line):
    source = Path(steam.__file__).read_bytes().decode("utf-8")
    assert registry_use(source + "\n" + line + "\n") != READS, line


def lowered(folder: Path) -> str:
    """A folder as Steam writes SteamPath: lower-cased, with forward slashes."""
    return str(folder).lower().replace(BS, "/")


def test_steampath_as_steam_writes_it_names_steams_folder(tmp_path, registry):
    w = World(tmp_path)
    w.game(w.steam, "4100000010", "Lantern Drift")
    registry.values[STEAM_KEY] = (lowered(w.steam), winreg.REG_SZ)
    folder = steam.find_steam_folder()
    assert folder == target.check_folder(lowered(w.steam)) and folder != str(w.steam)       # still lower-cased
    assert [(r, k, a) for r, k, a in registry.opened] == [(HKCU, STEAM_KEY[1], winreg.KEY_READ),
                                                           (HKLM, WOW_KEY[1], winreg.KEY_READ),
                                                           (HKLM, NATIVE_KEY[1], winreg.KEY_READ)]
    lib = steam.read()
    assert titles(lib) == ["Lantern Drift"] and lib.games[0].folder.startswith(folder + BS)


@pytest.mark.parametrize("key", [WOW_KEY, NATIVE_KEY])
def test_installpath_gives_the_folders_own_case_when_it_names_the_same_folder(tmp_path, registry, key):
    w = World(tmp_path)
    registry.values[STEAM_KEY] = (lowered(w.steam), winreg.REG_SZ)
    registry.values[key] = (str(w.steam), winreg.REG_SZ)
    assert steam.find_steam_folder() == target.check_folder(str(w.steam))


def test_without_steampath_the_32_bit_views_installpath_is_taken_as_it_is_spelt(tmp_path, registry):
    w = World(tmp_path)
    registry.values[WOW_KEY] = (str(w.steam), winreg.REG_SZ)
    registry.values[NATIVE_KEY] = (str(w.steam).upper(), winreg.REG_SZ)
    assert steam.find_steam_folder() == target.check_folder(str(w.steam)) != target.check_folder(str(w.steam).upper())


def test_steampath_wins_over_an_installpath_that_names_another_folder(tmp_path, registry):
    w, other = World(tmp_path / "a"), World(tmp_path / "b")
    registry.values[STEAM_KEY] = (lowered(w.steam), winreg.REG_SZ)
    registry.values[WOW_KEY] = (str(other.steam), winreg.REG_SZ)
    registry.values[NATIVE_KEY] = (str(other.steam), winreg.REG_SZ)
    assert steam.find_steam_folder() == target.check_folder(lowered(w.steam))


def test_installpath_is_read_when_steampath_names_nothing_that_can_be_used(tmp_path, registry):
    w = World(tmp_path)
    registry.values[STEAM_KEY] = (lowered(tmp_path / "Gone"), winreg.REG_SZ)
    registry.values[WOW_KEY] = (str(w.steam), winreg.REG_SZ)
    assert steam.find_steam_folder() == target.check_folder(str(w.steam))
    del registry.values[WOW_KEY]
    registry.values[NATIVE_KEY] = (str(w.steam), winreg.REG_SZ)
    assert steam.find_steam_folder() == target.check_folder(str(w.steam))
    registry.values[WOW_KEY] = (4100000010, winreg.REG_DWORD)
    assert steam.find_steam_folder() == target.check_folder(str(w.steam))


def test_an_expandable_string_is_expanded(tmp_path, registry, monkeypatch):
    w = World(tmp_path)
    monkeypatch.setenv("LAGDOCTOR_TEST_STEAM_ROOT", str(tmp_path))
    registry.values[STEAM_KEY] = ("%LAGDOCTOR_TEST_STEAM_ROOT%" + BS + "Steam", winreg.REG_EXPAND_SZ)
    assert steam.find_steam_folder() == target.check_folder(str(w.steam))
    registry.values[STEAM_KEY] = ("%LAGDOCTOR_TEST_STEAM_ROOT%" + BS + "Steam", winreg.REG_SZ)     # not expanded
    assert steam.find_steam_folder() is None


def test_an_expandable_string_that_cannot_be_expanded_is_named(tmp_path, registry, monkeypatch):
    def fail(text):
        raise OSError(8, "Not enough memory resources are available to process this command")

    monkeypatch.setattr(steam, "_expand", fail)
    registry.values[STEAM_KEY] = ("%LAGDOCTOR_TEST_STEAM_ROOT%" + BS + "Steam", winreg.REG_EXPAND_SZ)
    assert steam.read().problems == [
        f"Steam's folder was not found, so no Steam games were listed (HKCU{BS}Software{BS}Valve{BS}Steam SteamPath: "
        "its environment variables could not be expanded (Not enough memory resources are available to process this "
        "command))"]


def test_no_value_that_names_a_usable_folder_means_no_steam_and_one_problem_that_says_why(tmp_path, registry,
                                                                                          monkeypatch):
    assert steam.find_steam_folder() is None
    assert steam.read() == stores.Library(
        games=[], problems=["Steam's folder was not found, so no Steam games were listed (Steam may not be installed)"])
    real_type = storage.drive_type
    monkeypatch.setattr(storage, "drive_type", lambda root: 4 if root.upper() == "Z:" + BS else real_type(root))
    gone = tmp_path / "Gone"
    registry.values[STEAM_KEY] = (lowered(gone), winreg.REG_SZ)
    registry.values[WOW_KEY] = (b"C:\\Steam", winreg.REG_BINARY)
    registry.values[NATIVE_KEY] = ("Z:" + BS + "Steam", winreg.REG_SZ)
    assert steam.find_steam_folder() is None
    assert steam.read() == stores.Library(problems=[
        "Steam's folder was not found, so no Steam games were listed ("
        f"HKCU{BS}Software{BS}Valve{BS}Steam SteamPath: {target.check_folder(lowered(gone))} does not exist; "
        f"HKLM{BS}SOFTWARE{BS}WOW6432Node{BS}Valve{BS}Steam InstallPath: it is not text; "
        f"HKLM{BS}SOFTWARE{BS}Valve{BS}Steam InstallPath: Z:{BS}Steam {NOT_LOCAL})"])
    registry.values[NATIVE_KEY] = PermissionError(13, "Access is denied", None, 5)
    [problem] = steam.read().problems
    assert problem.endswith(f"HKLM{BS}SOFTWARE{BS}Valve{BS}Steam InstallPath: it could not be read (Access is denied))")


def test_a_steam_folder_handed_in_must_pass_the_same_checks(tmp_path):
    w = World(tmp_path)
    w.game(w.steam, "4100000010", "Lantern Drift")
    assert titles(steam.read(w.steam)) == ["Lantern Drift"]                # a Path will do
    gone = tmp_path / "Gone"
    assert steam.read(str(gone)) == stores.Library(problems=[
        f"Steam's folder could not be used, so no Steam games were listed: {gone} does not exist"])
    [problem] = steam.read("Steam").problems
    assert problem.startswith("Steam's folder could not be used, so no Steam games were listed: ")
    assert "is not a folder on a local drive" in problem


# ------------------------------------------------------------------------------------- KeyValues ---

def test_keyvalues_reads_steams_own_shape():
    text = ('"AppState"\n{\n\t"appid"\t\t"4100000010"\n\t"LauncherPath"\t\t"C:\\\\Steam\\\\steam.exe"\n'
            '\t"InstalledDepots"\n\t{\n\t\t"4100000011"\n\t\t{\n\t\t\t"manifest"\t\t"9"\n\t\t}\n\t}\n'
            '\t"MountedConfig"\n\t{\n\t}\n}\n')
    assert keyvalues.parse(text) == {"appstate": {"appid": "4100000010", "launcherpath": "C:" + BS + "Steam" + BS +
                                                  "steam.exe", "installeddepots": {"4100000011": {"manifest": "9"}},
                                                  "mountedconfig": {}}}


def test_keyvalues_unescapes_the_four_escapes_steam_writes():
    text = '"a"\t\t"' + BS + '"q' + BS + '" ' + BS + BS + ' n' + BS + 'n t' + BS + 't"'
    assert keyvalues.parse(text) == {"a": '"q" ' + BS + " n\n t\t"}


def test_keyvalues_folds_the_case_of_keys_and_keeps_the_first_of_two():
    text = '"AppState" { "AppID" "4100000010" "appid" "4100000020" "Name" "Lantern Drift" } "appstate" { "x" "y" }'
    assert keyvalues.parse(text) == {"appstate": {"appid": "4100000010", "name": "Lantern Drift"}}


def test_keyvalues_takes_crlf_and_comments():
    text = '// by hand\r\n"AppState"\r\n{\r\n\t"appid"\t\t"4100000010"\t// the app\r\n}\r\n'
    assert keyvalues.parse(text) == {"appstate": {"appid": "4100000010"}}


def test_keyvalues_tells_a_quoted_brace_from_a_real_one():
    text = '"AppState" { "name" "{" "installdir" "Lantern Drift" "label" "}" "appid" "4100000010" }'
    assert keyvalues.parse(text) == {"appstate": {"name": "{", "installdir": "Lantern Drift", "label": "}",
                                                  "appid": "4100000010"}}
    assert keyvalues.parse('"{" { "}" "x" }') == {"{": {"}": "x"}}       # quoted braces as keys too


def test_keyvalues_takes_a_comment_that_ends_the_file_without_a_line_break():
    assert keyvalues.parse('"a"\t\t"b"\n// the end') == {"a": "b"}


def test_keyvalues_takes_a_key_that_starts_with_a_hash_inside_a_block():
    assert keyvalues.parse('"a" { "#include" "other.vdf" }') == {"a": {"#include": "other.vdf"}}


@pytest.mark.parametrize("text, says", [
    ('"a" b', "an unquoted token at line 1"),
    ('"a" "b"\n/ not a comment', "an unquoted token at line 2"),
    ('"a" "b', "a quoted string that never ends"),
    ('"a" "b' + BS + 'x"', "an escape Steam does not write (" + BS + "x) at line 1"),
    ('"a" "b' + BS + 'r"', "an escape Steam does not write (" + BS + "r) at line 1"),
    ('"a" "b' + BS, "an escape Steam does not write (" + BS + ") at line 1"),
    ('"a" "b\nc"', "a line break inside a quoted string at line 1"),
    ('"a" "b' + chr(0) + 'c"', "a NUL inside a token at line 1"),
    ('"a" {', "a block that never closes"),
    ('"a" { "b" "c"', "a block that never closes"),
    ('"a" "b" }', "a closing brace with no block open at line 1"),
    ('{ "a" "b" }', "a block with no key at line 1"),
    ('"a" { { } }', "a block with no key at line 1"),
    ('"#include" "other.vdf"', "the key '#include' at line 1 starts with #"),
    ('"#BASE" "other.vdf"', "the key '#BASE' at line 1 starts with #"),
    ('"#other" "x"', "the key '#other' at line 1 starts with #"),
    ('"a" [$WIN32] "b"', "a conditional at line 1"),
    ('"a" "b" [$WIN32]', "a conditional at line 1"),
    ('"a"', "the key 'a' has no value"),
    ('"r" { "a" }', "the key 'a' has no value at line 1"),
    ('"' + "k" * 70 + '"', "the key '" + "k" * 60 + "'... has no value"),
])
def test_keyvalues_refuses_what_steam_never_writes(text, says):
    with pytest.raises(ValueError, match=re.escape(says)):
        keyvalues.parse(text)


def test_keyvalues_takes_a_token_of_4096_characters_and_refuses_a_longer_one():
    assert keyvalues.parse('"a" "' + "x" * 4096 + '"') == {"a": "x" * 4096}
    with pytest.raises(ValueError, match="a token longer than 4096 characters"):
        keyvalues.parse('"a" "' + "x" * 4097 + '"')


def nested(levels: int) -> str:
    return "".join(f'"k{i}" {{ ' for i in range(levels)) + "}" * levels


def test_keyvalues_takes_16_levels_of_blocks_and_refuses_a_17th():
    tree, depth = keyvalues.parse(nested(16)), 0
    while tree:
        tree, depth = next(iter(tree.values())), depth + 1
    assert depth == 16
    with pytest.raises(ValueError, match="blocks nested more than 16 deep"):
        keyvalues.parse(nested(17))


def test_keyvalues_counts_the_keys_of_every_level_against_its_cap(monkeypatch):
    assert keyvalues.MAX_KEYS == 100_000
    monkeypatch.setattr(keyvalues, "MAX_KEYS", 3)
    assert keyvalues.parse('"a" { "b" "1" "c" "2" }') == {"a": {"b": "1", "c": "2"}}
    with pytest.raises(ValueError, match="more than 3 keys"):
        keyvalues.parse('"a" { "b" "1" "c" "2" "d" "3" }')


@pytest.mark.parametrize("value", [b'"a" "b"', None, ['"a" "b"']])
def test_keyvalues_takes_only_text(value):
    with pytest.raises(ValueError, match="KeyValues text must be a str"):
        keyvalues.parse(value)


# ---------------------------------------------------------------- found by the review of the fixes ---

def test_a_library_on_a_drive_that_is_not_connected_is_named_so(tmp_path, monkeypatch):
    w = World(tmp_path)
    real_type = storage.drive_type
    monkeypatch.setattr(storage, "drive_type", lambda root: 1 if root.upper() == "Z:" + BS else real_type(root))
    w.library_list({"0": w.entry(w.steam), "1": w.entry("Z:" + BS + "SteamLibrary")})
    assert steam.read(str(w.steam)).problems == [
        f"Steam's library 1 in {vdf_of(w)} was skipped: Z:{BS}SteamLibrary is on Z:, where no disk is connected now"]


def test_a_soundtrack_folder_that_cannot_be_looked_at_is_named_for_itself(tmp_path, monkeypatch):
    w = World(tmp_path)
    w.game(w.steam, "4100000010", "Lantern Drift", folder=False)
    w.library_list({"0": w.entry(w.steam)})
    music = target.check_folder(str(w.steam)) + BS + "steamapps" + BS + "music" + BS + "Lantern Drift"
    real = stores._stat

    def stat(path):
        if path == music:
            raise PermissionError(13, "Access is denied")
        return real(path)

    monkeypatch.setattr(stores, "_stat", stat)
    assert steam.read(str(w.steam)).problems == [
        f"Lantern Drift (Steam app 4100000010) was skipped: its folder {w.common(w.steam, 'Lantern Drift')} is "
        f"missing, and {music} could not be looked at (Access is denied)"]


def test_a_folder_name_with_other_spaces_is_listed(tmp_path):
    w = World(tmp_path)
    names = ["Lantern" + chr(0x3000) + "Drift", "Harbor" + chr(0xA0) + "Lights"]       # ideographic, no-break
    for i, name in enumerate(names):
        w.game(w.steam, str(4100000010 + 10 * i), name)
    w.library_list({"0": w.entry(w.steam)})
    lib = steam.read(str(w.steam))
    assert lib.problems == [] and titles(lib) == ["Lantern Drift", "Harbor Lights"]   # shown as plain spaces
    assert [g.folder for g in lib.games] == [w.common(w.steam, n) for n in names]
    assert steam._name_problem("Lantern" + chr(0x2028) + "Drift") == "a name with characters that cannot be shown"
