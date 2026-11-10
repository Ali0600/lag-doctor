"""Epic's installed games (lagdoctor/stores/epic.py), read from made-up launcher files in each test's tmp_path.

The legacy launcher's manifests are written as it writes them (one JSON object a file, tab indents, CRLF, no line break
at the end), and the Preview's journal as it writes it (one compact JSON object a line, LF). Every game, id and folder
here is made up, and no test reads this PC's own Epic files: each passes its own paths, or moves the defaults into its
tmp_path and makes any other known folder fail the test.
"""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path

import pytest

from lagdoctor import stores
from lagdoctor.capture import target
from lagdoctor.stores import Game, Library, epic
from lagdoctor.winapi import folders, storage

NS, ITEM, APP = "a1" * 16, "b2" * 16, "LanternDrift"            # Lantern Drift: an app name that is a word
NS2, ITEM2, APP2 = "c3" * 16, "d4" * 16, "e5" * 16              # Harbor Lights: one of 32 hexadecimal digits
LANTERN, HARBOR = f"{NS}:{ITEM}:{APP}", f"{NS2}:{ITEM2}:{APP2}"
DROP = object()                                                 # a key the launcher left out
SECRET = "-AUTH_PASSWORD=made-up-sign-in-code"                 # what an Epic launch puts on a game's command line
FILE_ATTRIBUTE_OFFLINE = 0x1000                                 # winnt.h; a cloud file that is online only has it
NOT_LOCAL = "is not on a local fixed or removable disk"
NOT_LISTED = "so its game is not listed"
BS = chr(92)


def link(namespace: str, item: str, app: str) -> str:
    return f"com.epicgames.launcher://apps/{namespace}%3A{item}%3A{app}?action=launch&silent=true"


def iid(n: int) -> str:
    """A made-up installationId: 32 upper-case hexadecimal digits, as the Preview writes them."""
    return f"{n:02X}" * 16


def manifest(folder, **fields) -> dict:
    """A legacy manifest for Lantern Drift as the launcher writes one (some of its keys), with `fields` changed; a
    field set to DROP is left out."""
    loc = str(folder).replace("\\", "/")                        # the launcher writes forward slashes
    out = {"FormatVersion": 0, "bIsIncompleteInstall": False, "LaunchCommand": "", "LaunchExecutable": "Lantern.exe",
           "ManifestLocation": loc + "/.egstore", "bIsApplication": True, "bIsExecutable": True, "AppName": APP,
           "AppCategories": ["public", "games", "applications"], "CatalogNamespace": NS, "CatalogItemId": ITEM,
           "DisplayName": "Lantern Drift", "InstallLocation": loc, "InstallationGuid": "0F" * 16,
           "MainGameAppName": APP, "MainGameCatalogNamespace": NS, "MainGameCatalogItemId": ITEM,
           "OwnershipToken": "false", "TechnicalType": "public,games,applications"}
    out.update(fields)
    return {k: v for k, v in out.items() if v is not DROP}


def harbor(folder, **fields) -> dict:
    """A legacy manifest for Harbor Lights."""
    return manifest(folder, **{"AppName": APP2, "CatalogNamespace": NS2, "CatalogItemId": ITEM2,
                               "MainGameAppName": APP2, "MainGameCatalogNamespace": NS2, "MainGameCatalogItemId": ITEM2,
                               "DisplayName": "Harbor Lights", **fields})


HEADER = {"version": 1, "writtenAt": "2026-11-01T09:00:00Z", "entries": 0}


def record(n: int, folder, **fields) -> dict:
    """A Preview record of Harbor Lights as the launcher writes one, with `fields` changed (DROP leaves one out)."""
    out = {"installationId": iid(n), "complete": True, "dir": str(folder), "platform": "Windows", "namespace": NS2,
           "itemId": ITEM2, "artifactId": APP2, "version": "1.0.2", "tags": [""], "sidecar": {"revision": 1},
           "launchCommand": "", "launchExecutable": "HarborLights.exe", "writtenAt": "2026-11-01T09:00:01Z"}
    out.update(fields)
    return {k: v for k, v in out.items() if v is not DROP}


def lantern_record(n: int, folder, **fields) -> dict:
    return record(n, folder, namespace=NS, itemId=ITEM, artifactId=APP, **fields)


def tombstone(n: int, **fields) -> dict:
    return {"installationId": iid(n), "deleted": True, "writtenAt": "2026-11-01T09:00:02Z", **fields}


class World:
    """Made-up launcher files: the legacy manifests folder and the Preview's journal where each launcher keeps them,
    and the game folders they name."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.manifests = root / "ProgramData" / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"
        self.journal = root / "AppData" / "Local" / "com.epicgames.eglv2" / "installations.jsonl"
        self.manifests.mkdir(parents=True)
        self.journal.parent.mkdir(parents=True)

    def folder(self, name: str) -> Path:
        """A game folder that exists."""
        path = self.root / "Games" / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def item(self, name: str, data) -> Path:
        """A manifest: a dict written as the launcher writes one, text with CRLF line breaks, or bytes as they are."""
        if isinstance(data, dict):
            data = json.dumps(data, indent="\t")
        if isinstance(data, str):
            data = data.replace("\n", "\r\n").encode("utf-8")
        path = self.manifests / name
        path.write_bytes(data)
        return path

    def lines(self, *lines, end: str = "\n", tail: str = "") -> Path:
        """The journal: each line compact JSON (or text as given) ended by `end`, then `tail` with no line break."""
        text = "".join((ln if isinstance(ln, str) else json.dumps(ln, separators=(",", ":"))) + end for ln in lines)
        self.journal.write_bytes((text + tail).encode("utf-8"))
        return self.journal

    def read(self) -> Library:
        return epic.read(manifests=str(self.manifests), installations=str(self.journal))


@pytest.fixture
def world(tmp_path) -> World:
    return World(tmp_path)


@pytest.fixture(autouse=True)
def no_real_folders(monkeypatch):
    """No test here reads this PC's own ProgramData or LocalAppData: a default place a test did not move fails it."""
    def real(name):
        def call():
            pytest.fail(f"epic.read() asked for the real {name} folder")
        return call

    monkeypatch.setattr(epic, "_program_data", real("ProgramData"))
    monkeypatch.setattr(epic, "_local_app_data", real("LocalAppData"))


# ---------------------------------------------------------------- the legacy launcher ---

def test_a_legacy_base_game_is_listed_with_its_id_link_folder_and_title(world):
    folder = world.folder("Lantern Drift")
    world.item("0F0F0F0F0F0F0F0F0F0F0F0F0F0F0F0F.item", manifest(folder))
    lib = world.read()
    assert lib == Library([Game("epic", LANTERN, "Lantern Drift", str(folder), link(NS, ITEM, APP))], [])
    assert "/" not in lib.games[0].folder                       # Windows' form, though the file has forward slashes


def test_add_ons_plugins_unreal_engine_and_unfinished_installs_are_passed_over_quietly(world):
    lantern, harbor_dir = world.folder("Lantern Drift"), world.folder("Harbor Lights")
    world.item("00.item", manifest(lantern))
    world.item("01.item", harbor(harbor_dir, MainGameAppName=DROP, AppCategories=DROP))     # an older manifest: listed
    # Each of these is whole otherwise (ids Epic's link takes, a folder that exists), so one rule alone passes it over.
    world.item("02.item", manifest(lantern, AppName="LanternDriftSoundtrack", AppCategories=DROP))   # an add-on
    world.item("03.item", manifest(lantern, AppName="LanternDriftTools", AppCategories=["public", "games", "addons"]))
    world.item("04.item", manifest(lantern, AppName="LanternPlugin", MainGameAppName="LanternPlugin",
                                   AppCategories=["plugins", "plugins/engine"]))
    world.item("05.item", manifest(lantern, AppName="UE_5.4", MainGameAppName="UE_5.4", CatalogNamespace="ue",
                                   AppCategories=DROP))
    world.item("06.item", manifest(lantern, AppName="LanternDriftBeta", MainGameAppName="LanternDriftBeta",
                                   bIsIncompleteInstall=True))
    lib = world.read()
    assert lib.problems == []
    assert [g.id for g in lib.games] == [LANTERN, HARBOR]


def test_the_manifests_are_the_item_files_at_the_top_of_the_folder_in_any_case(world):
    world.item("0A0A.ITEM", manifest(world.folder("Lantern Drift")))     # Windows matches *.item in any case
    world.item("notes.txt", "not a manifest")
    world.item("0B0B.item.bak", "{")
    (world.manifests / "Old.item").mkdir()                     # a folder, not a manifest
    (world.manifests / "Pending").mkdir()                      # the launcher's own, for installs in progress
    (world.manifests / "Pending" / "0C0C.item").write_text("{", encoding="utf-8")
    assert world.read() == Library([Game("epic", LANTERN, "Lantern Drift", str(world.folder("Lantern Drift")),
                                         link(NS, ITEM, APP))], [])


def test_the_manifests_are_read_in_the_order_of_their_names(world):
    # NTFS lists "a.item" before "_b.item" (it compares names in upper case, and "_" comes after "A"); by their names
    # in lower case "_b.item" comes first. The first manifest of a game is the one listed.
    first, other = world.folder("Lantern Drift"), world.folder("Lantern Drift Copy")
    later = world.item("a.item", manifest(other))
    world.item("_b.item", manifest(first))
    lib = world.read()
    assert [g.folder for g in lib.games] == [str(first)]
    assert lib.problems == [f"{later} names the same game as {world.manifests / '_b.item'}, but in another folder "
                            f"({other}), so only the first is listed"]


def test_ids_that_epic_link_refuses_are_named_and_the_other_games_still_listed(world):
    lantern, harbor_dir = world.folder("Lantern Drift"), world.folder("Harbor Lights")
    bad = [world.item("01.item", manifest(lantern, AppName="Lantern Drift", MainGameAppName="Lantern Drift")),
           world.item("02.item", manifest(lantern, CatalogNamespace=DROP)),
           world.item("03.item", manifest(lantern, CatalogItemId=ITEM + ":x"))]
    world.item("04.item", harbor(harbor_dir))
    lib = world.read()
    assert [g.id for g in lib.games] == [HARBOR]
    assert lib.problems == [f"{bad[0]}: not an Epic app name: 'Lantern Drift', {NOT_LISTED}",
                            f"{bad[1]}: not an Epic catalog namespace: None, {NOT_LISTED}",
                            f"{bad[2]}: not an Epic catalog item id: '{ITEM}:x', {NOT_LISTED}"]


def test_a_folder_that_is_missing_refused_or_not_a_folder_is_named(world):
    gone = world.root / "Games" / "Lantern Drift"               # never made
    a_file = world.folder("Harbor Lights") / "Harbor.txt"
    a_file.write_text("", encoding="utf-8")
    p1 = world.item("01.item", manifest(gone))
    p2 = world.item("02.item", harbor(a_file))
    p3 = world.item("03.item", manifest(gone, InstallLocation="Games/Lantern Drift"))
    p4 = world.item("04.item", manifest(gone, InstallLocation="C:/"))
    p5 = world.item("05.item", manifest(gone, InstallLocation=DROP))
    lib = world.read()
    assert lib.games == []
    assert lib.problems[:2] == [f"{p1}: {gone} does not exist, {NOT_LISTED}",
                                f"{p2}: {a_file} is not a folder, {NOT_LISTED}"]
    refused = ["Games/Lantern Drift is not a folder on a local drive", "C:/ is a whole drive, not a game's folder",
               "not a folder path: None"]
    for path, problem, why in zip([p3, p4, p5], lib.problems[2:], refused, strict=True):
        assert problem.startswith(f"{path}: its install folder was refused: {why}") and problem.endswith(NOT_LISTED)


@pytest.mark.parametrize("kind, says", [(4, NOT_LOCAL), (1, "is on Q:, where no disk is connected now"),
                                        (5, NOT_LOCAL)])   # GetDriveTypeW: network, no such drive, CD or DVD
def test_a_folder_not_on_a_local_disk_is_named_before_it_is_looked_at(world, monkeypatch, kind, says):
    far = "Q:" + BS + "Games" + BS + "Lantern Drift"
    here = world.folder("Harbor Lights")
    p1, p2 = world.item("01.item", manifest(far)), world.item("02.item", harbor(here))
    real_type = storage.drive_type
    monkeypatch.setattr(storage, "drive_type", lambda root: kind if root.upper() == "Q:" + BS else real_type(root))
    looked = []
    real_lstat, real_stat = target._lstat, stores._stat
    monkeypatch.setattr(target, "_lstat", lambda path: looked.append(path) or real_lstat(path))
    monkeypatch.setattr(stores, "_stat", lambda path: looked.append(path) or real_stat(path))
    # The folder on Q: is named for its drive, not as missing: the drive is judged before anything is looked at.
    assert world.read() == Library([Game("epic", HARBOR, "Harbor Lights", str(here), link(NS2, ITEM2, APP2))],
                                   [f"{p1}: {far} {says}, {NOT_LISTED}"])
    assert looked and not [p for p in looked if p.upper().startswith("Q:")]


def test_a_folder_on_a_removable_disk_is_listed(world, monkeypatch):
    world.item("01.item", manifest(world.folder("Lantern Drift")))
    monkeypatch.setattr(storage, "drive_type", lambda root: storage.DRIVE_REMOVABLE)
    assert [g.id for g in world.read().games] == [LANTERN]


def test_a_manifest_that_is_not_json_as_epic_writes_it_is_named_and_the_rest_still_listed(world):
    folder = world.folder("Harbor Lights")
    whole = json.dumps(harbor(folder), indent="\t")
    cases = [(whole[:-40], "was not read: it is not JSON (Unterminated string starting at: line 24, column "),
             ("  \n", "is empty"),
             (json.dumps([harbor(folder)]), "does not hold one JSON object, so it was not read"),
             ('{"AppName": "a", "AppName": "b"}', "was not read: it has the key 'AppName' twice"),
             ('{"InstallSize": NaN}', "was not read: it holds NaN, which is not a JSON value"),
             ("[" * 100_000, "was not read: it is nested too deep to read"),
             ('{"InstallSize": ' + "9" * 5000 + "}", "was not read: it holds a number too long to read"),
             ("Harbor Lights".encode("utf-16"), "is not UTF-8 text")]
    paths = [world.item(f"{n:02}.item", text) for n, (text, _) in enumerate(cases)]
    world.item("99.item", harbor(folder))
    lib = world.read()
    assert [g.id for g in lib.games] == [HARBOR]
    assert len(lib.problems) == len(cases)
    for path, problem, (_, why) in zip(paths, lib.problems, cases, strict=True):
        assert problem.startswith(f"{path} {why}"), problem


def test_a_manifest_past_1_mib_is_named_and_one_of_1_mib_is_read(world):
    assert epic.MAX_ITEM_BYTES == 1 << 20

    def padded(data: dict, size: int) -> bytes:
        body = json.dumps(data, indent="\t").replace("\n", "\r\n").encode("utf-8")
        return body + b" " * (size - len(body))

    big = world.item("01.item", padded(manifest(world.folder("Lantern Drift")), (1 << 20) + 1))
    world.item("02.item", padded(harbor(world.folder("Harbor Lights")), 1 << 20))
    lib = world.read()
    assert [g.id for g in lib.games] == [HARBOR]
    assert lib.problems == [f"{big} is larger than {1 << 20} bytes, so it was not read"]


def set_offline(path: Path) -> None:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.SetFileAttributesW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
    k32.SetFileAttributesW.restype = ctypes.c_int
    assert k32.SetFileAttributesW(str(path), FILE_ATTRIBUTE_OFFLINE), ctypes.get_last_error()
    assert os.stat(path).st_file_attributes & FILE_ATTRIBUTE_OFFLINE         # it took


def test_an_online_only_manifest_is_named_and_never_opened(world, monkeypatch):
    cloud = world.item("01.item", manifest(world.folder("Lantern Drift")))
    world.item("02.item", harbor(world.folder("Harbor Lights")))
    set_offline(cloud)
    opened, real = [], stores.read_text
    monkeypatch.setattr(stores, "read_text", lambda path, max_bytes: opened.append(Path(path)) or real(path, max_bytes))
    lib = world.read()
    assert lib.problems == [f"{cloud} is kept online only, so it was not read"]
    assert [g.id for g in lib.games] == [HARBOR]
    assert opened == [world.manifests / "02.item"]            # the listing said so: it was never opened


def test_a_title_is_the_display_name_cleaned_else_the_folders_name(world):
    world.item("01.item", manifest(world.folder("LanternDrift"), DisplayName=DROP))
    shown = "Harbor" + chr(7) + "  Lights" + chr(0x202E)    # a control character and a mark that reorders text
    world.item("02.item", harbor(world.folder("HarborLights"), DisplayName=shown))
    world.item("03.item", manifest(world.folder("Tidewater"), AppName="Tidewater", MainGameAppName="Tidewater",
                                   DisplayName=" " + chr(0x200B) + " "))
    world.item("04.item", manifest(world.folder("Kiln"), AppName="Kiln", MainGameAppName="Kiln", DisplayName=7))
    lib = world.read()
    assert lib.problems == []
    assert [g.title for g in lib.games] == ["LanternDrift", "Harbor Lights", "Tidewater", "Kiln"]


def test_two_manifests_for_one_game_list_it_once_and_name_the_one_in_another_folder(world):
    first, other = world.folder("Lantern Drift"), world.folder("Lantern Drift Copy")
    world.item("01.item", manifest(first))
    world.item("02.item", manifest(first, InstallLocation=str(first).lower() + "\\"))   # the same folder, written so
    copy = world.item("03.item", manifest(other))
    lib = world.read()
    assert lib.games == [Game("epic", LANTERN, "Lantern Drift", str(first), link(NS, ITEM, APP))]
    assert lib.problems == [f"{copy} names the same game as {world.manifests / '01.item'}, but in another folder "
                            f"({other}), so only the first is listed"]


def test_a_field_of_the_wrong_kind_is_named(world):
    lantern = world.folder("Lantern Drift")
    p1 = world.item("01.item", manifest(lantern, bIsIncompleteInstall="no"))
    p2 = world.item("02.item", manifest(lantern, AppCategories="games"))
    p3 = world.item("03.item", manifest(lantern, AppCategories=["games", 7]))
    p4 = world.item("04.item", manifest(lantern, bIsIncompleteInstall="y" * 100))
    lib = world.read()
    assert lib.games == []
    assert lib.problems == [f"{p1}: its bIsIncompleteInstall is 'no', not true or false, {NOT_LISTED}",
                            f"{p2}: its AppCategories is not a list of words, {NOT_LISTED}",
                            f"{p3}: its AppCategories is not a list of words, {NOT_LISTED}",
                            f"{p4}: its bIsIncompleteInstall is '{'y' * 56}..., not true or false, {NOT_LISTED}"]


def test_a_manifests_folder_past_its_caps_is_named_and_not_read(world, monkeypatch):
    assert (epic.MAX_ENTRIES, epic.MAX_ITEMS, epic.MAX_ALL_ITEMS_BYTES) == (16_384, 4096, 64 << 20)
    bad = world.item("01.item", "{")
    world.item("02.item", manifest(world.folder("Lantern Drift")))
    world.item("03.item", harbor(world.folder("Harbor Lights")))
    m = world.manifests
    monkeypatch.setattr(epic, "MAX_ITEMS", 3)
    lib = world.read()
    assert len(lib.games) == 2 and len(lib.problems) == 1 and lib.problems[0].startswith(f"{bad} was not read")
    monkeypatch.setattr(epic, "MAX_ITEMS", 2)
    assert world.read() == Library([], [f"{m} holds more than 2 .item files, so it was not read"])
    monkeypatch.setattr(epic, "MAX_ITEMS", 4096)
    (m / "notes.txt").write_text("", encoding="utf-8")
    monkeypatch.setattr(epic, "MAX_ENTRIES", 4)
    assert len(world.read().games) == 2
    monkeypatch.setattr(epic, "MAX_ENTRIES", 3)
    assert world.read() == Library([], [f"{m} holds more than 3 files and folders, so it was not read"])
    monkeypatch.setattr(epic, "MAX_ENTRIES", 16_384)
    total = sum(p.stat().st_size for p in m.glob("*.item"))   # UTF-8 with no byte order mark: the bytes counted
    monkeypatch.setattr(epic, "MAX_ALL_ITEMS_BYTES", total)
    assert len(world.read().games) == 2
    monkeypatch.setattr(epic, "MAX_ALL_ITEMS_BYTES", total - 1)
    # 01.item was read before the budget ran out, and is not named: the folder was not read.
    assert world.read() == Library([], [f"the .item files in {m} come to more than {total - 1} bytes, so none was "
                                        f"read"])


def test_no_manifests_folder_is_no_problem_and_one_that_cannot_be_listed_is(tmp_path):
    nowhere = str(tmp_path / "nowhere.jsonl")
    assert epic.read(manifests=str(tmp_path / "Manifests"), installations=nowhere) == Library([], [])
    (tmp_path / "Manifests").write_text("", encoding="utf-8")                # a file where the folder would be
    lib = epic.read(manifests=str(tmp_path / "Manifests"), installations=nowhere)
    assert lib.games == [] and len(lib.problems) == 1
    assert lib.problems[0].startswith(f"{tmp_path / 'Manifests'} could not be read (")


# ---------------------------------------------------------------- the Preview ---

def test_a_preview_game_is_listed_from_its_record(world):
    folder = world.folder("HarborLights")
    world.lines(HEADER, record(1, folder))
    assert world.read() == Library([Game("epic", HARBOR, "HarborLights", str(folder), link(NS2, ITEM2, APP2))], [])


def test_the_journal_must_start_with_the_header_lag_doctor_knows(world):
    body = record(1, world.folder("HarborLights"))
    refused = [body, {**HEADER, "version": 2}, {**HEADER, "version": True}, {**HEADER, "version": "1"},
               {"writtenAt": HEADER["writtenAt"], "entries": 0}, {**HEADER, "installationId": iid(9)}, [1], 7]
    for head in refused:
        world.lines(head, body)
        assert world.read() == Library([], [f"{world.journal} does not start with the header lag-doctor knows "
                                            f"(version 1), so it was not read"]), head
    world.lines("{oops", body)
    lib = world.read()
    assert lib.games == [] and len(lib.problems) == 1
    assert lib.problems[0].startswith(f"{world.journal}, line 1 (the header) was not read: it is not JSON (Expecting "
                                      f"property name")
    assert lib.problems[0].endswith(": column 2), so the file was not read")
    world.lines("{oops", body, tail=json.dumps(record(2, world.folder("HarborLights")))[:40])
    lib = world.read()                                          # a bad header, and the launcher still writing
    assert lib.games == [] and len(lib.problems) == 1 and "line 1 (the header) was not read" in lib.problems[0]
    world.lines({**HEADER, "entries": 7}, body)                 # what entries counts is not the lines: not checked
    assert [g.id for g in world.read().games] == [HARBOR]


def test_a_journal_with_no_records_yet_is_no_problem(world):
    head = json.dumps(HEADER).encode("utf-8")
    for text in [b"", head + b"\n", head[:-3]]:                # empty; the header alone; the header half-written
        world.journal.write_bytes(text)
        assert world.read() == Library([], []), text


def test_the_last_record_for_an_install_wins_in_file_order_never_by_its_stamp(world):
    old, new, lantern = world.folder("HarborLights"), world.folder("HarborLights2"), world.folder("LanternDrift")
    world.lines(HEADER,
                record(1, old, writtenAt="2026-11-01T09:00:05Z"),
                lantern_record(2, lantern),
                record(1, new, writtenAt="2026-11-01T09:00:01Z"),      # later in the file, earlier by its stamp
                record(3, world.folder("Tidewater"), itemId="f6" * 16, artifactId="Tidewater"),
                record(3, world.folder("Tidewater"), itemId="f6" * 16, artifactId="Tidewater", complete=False))
    assert world.read() == Library([Game("epic", LANTERN, "LanternDrift", str(lantern), link(NS, ITEM, APP)),
                                    Game("epic", HARBOR, "HarborLights2", str(new), link(NS2, ITEM2, APP2))], [])


def test_a_tombstone_removes_an_install_and_a_later_record_brings_it_back(world):
    harbor_dir, lantern = world.folder("HarborLights"), world.folder("LanternDrift")
    world.lines(HEADER,
                lantern_record(1, lantern), tombstone(1),
                record(2, harbor_dir), {"installationId": iid(2), "deleted": True}, record(2, harbor_dir),
                tombstone(3))                                   # one it never listed
    assert world.read() == Library([Game("epic", HARBOR, "HarborLights", str(harbor_dir), link(NS2, ITEM2, APP2))],
                                   [])


def test_add_on_records_installs_in_progress_and_unreal_engine_are_passed_over_quietly(world):
    base, lantern = world.folder("HarborLights"), world.folder("LanternDrift")
    world.lines(HEADER,
                record(1, base),
                lantern_record(2, lantern, parentInstallationId=None),        # a null parent is none: listed
                # Each is whole otherwise (ids Epic's link takes, a folder that exists), so one rule alone passes it.
                record(3, base, itemId="f6" * 16, artifactId="HarborLightsSoundtrack", parentInstallationId=iid(1)),
                record(4, base, itemId="a7" * 16, artifactId="HarborLightsBeta", complete=False),
                record(5, world.folder("UE_5.4"), namespace="ue", itemId="b8" * 16, artifactId="UE_5.4"))
    lib = world.read()
    assert lib.problems == []
    assert [g.id for g in lib.games] == [HARBOR, LANTERN]


def test_a_half_written_last_line_is_passed_over_quietly_and_a_whole_bad_one_is_named(world):
    folder = world.folder("HarborLights")
    cut = json.dumps(record(2, folder, itemId="f6" * 16), separators=(",", ":"))[:57]
    world.lines(HEADER, record(1, folder), tail=cut)
    assert world.read() == Library([Game("epic", HARBOR, "HarborLights", str(folder), link(NS2, ITEM2, APP2))], [])
    world.lines(HEADER, record(1, folder), cut)                 # the same line, but a line break ends it
    lib = world.read()
    assert [g.id for g in lib.games] == [HARBOR]
    assert len(lib.problems) == 1
    assert lib.problems[0].startswith(f"{world.journal}, line 3 was not read: it is not JSON")
    world.lines(HEADER, tail=json.dumps(record(1, folder)))   # a whole last line with no line break is read
    assert [g.id for g in world.read().games] == [HARBOR]


def test_a_bad_line_in_the_middle_is_named_and_changes_nothing(world):
    harbor_dir, lantern = world.folder("HarborLights"), world.folder("LanternDrift")
    j = world.lines(HEADER,
                    record(1, harbor_dir),
                    "not json",
                    "[1, 2]",
                    "",
                    record(0xAB, harbor_dir, installationId=iid(0xAB).lower()),
                    {"installationId": iid(1), "deleted": "true"},
                    {"installationId": {"id": iid(1)}, "deleted": True},
                    tombstone(1, installationId=iid(1) + "0"),
                    lantern_record(2, lantern))
    lib = world.read()
    assert [g.id for g in lib.games] == [HARBOR, LANTERN]
    assert lib.problems == [
        f"{j}, line 3 was not read: it is not JSON (Expecting value: column 1)",
        f"{j}, line 4 was not read: it does not hold one JSON object",
        f"{j}, line 6 was not read: its installationId is '{iid(0xAB).lower()}', not 32 hexadecimal digits in upper "
        f"case",
        f"{j}, line 7 was not read: its deleted is 'true', not true or false",
        f"{j}, line 8 was not read: its installationId is a block, not 32 hexadecimal digits in upper case",
        f"{j}, line 9 was not read: its installationId is '{iid(1)}0', not 32 hexadecimal digits in upper case"]


def test_a_record_whose_fields_fail_is_named(world):
    folder, gone = world.folder("HarborLights"), world.root / "Games" / "Gone"
    j = world.lines(HEADER,
                    record(1, folder, complete=DROP),
                    record(2, folder, complete="true"),
                    record(3, folder, complete=[True]),
                    record(4, folder, artifactId="Harbor Lights"),
                    record(5, gone, itemId="f6" * 16),
                    record(6, "Games/Harbor", itemId="a7" * 16),
                    record(7, folder, itemId="b8" * 16))       # listed
    lib = world.read()
    assert [g.id for g in lib.games] == [f"{NS2}:{'b8' * 16}:{APP2}"]
    assert lib.problems[:5] == [f"{j}, line 2: its complete is missing, not true or false, {NOT_LISTED}",
                                f"{j}, line 3: its complete is 'true', not true or false, {NOT_LISTED}",
                                f"{j}, line 4: its complete is a list, not true or false, {NOT_LISTED}",
                                f"{j}, line 5: not an Epic app name: 'Harbor Lights', {NOT_LISTED}",
                                f"{j}, line 6: {gone} does not exist, {NOT_LISTED}"]
    assert lib.problems[5].startswith(f"{j}, line 7: its install folder was refused: Games/Harbor is not a folder on")
    assert len(lib.problems) == 6


def test_two_installs_of_one_game_list_it_once_and_name_the_one_in_another_folder(world):
    first, other = world.folder("HarborLights"), world.folder("HarborLights Copy")
    j = world.lines(HEADER, record(1, first), record(2, other), record(3, str(first).lower()))
    lib = world.read()
    assert [g.folder for g in lib.games] == [str(first)]
    assert lib.problems == [f"{j}, line 3 names the same game as {j}, line 2, but in another folder ({other}), so "
                            f"only the first is listed"]


def test_a_journal_past_its_caps_is_named_and_not_read(world, monkeypatch):
    assert (epic.MAX_JSONL_BYTES, epic.MAX_LINES) == (8 << 20, 100_000)
    folder = world.folder("HarborLights")
    j = world.lines(HEADER, record(1, folder), tombstone(1), record(1, folder))
    monkeypatch.setattr(epic, "MAX_LINES", 4)
    assert [g.id for g in world.read().games] == [HARBOR]
    monkeypatch.setattr(epic, "MAX_LINES", 3)
    assert world.read() == Library([], [f"{j} holds more than 3 lines, so it was not read"])
    monkeypatch.setattr(epic, "MAX_LINES", 100_000)
    size = j.stat().st_size
    monkeypatch.setattr(epic, "MAX_JSONL_BYTES", size)
    assert [g.id for g in world.read().games] == [HARBOR]
    monkeypatch.setattr(epic, "MAX_JSONL_BYTES", size - 1)
    assert world.read() == Library([], [f"{j} is larger than {size - 1} bytes, so it was not read"])


def test_a_journal_with_windows_line_breaks_is_read(world):
    folder = world.folder("HarborLights")
    world.lines(HEADER, record(1, folder), tombstone(1), record(1, folder), end="\r\n")
    assert world.read() == Library([Game("epic", HARBOR, "HarborLights", str(folder), link(NS2, ITEM2, APP2))], [])


def test_no_journal_is_no_problem_and_a_folder_in_its_place_is(world, tmp_path):
    assert world.read() == Library([], [])                     # the journal was never written
    assert epic.read(manifests=str(world.manifests),
                     installations=str(tmp_path / "gone" / "installations.jsonl")) == Library([], [])
    world.journal.mkdir()
    assert world.read() == Library([], [f"{world.journal} is not a file, so it was not read"])


# ---------------------------------------------------------------- both ---

def test_a_game_both_launchers_list_is_listed_once_with_the_previews_folder_and_the_legacy_title(world):
    new, lantern = world.folder("HarborLights"), world.folder("Lantern Drift")
    moved_from = world.root / "Old" / "Harbor Lights"          # gone since the Preview took the game over
    world.item("01.item", harbor(moved_from, DisplayName=""))
    world.item("02.item", harbor(moved_from, DisplayName="Harbor Lights: Remastered"))
    world.item("03.item", harbor(moved_from, DisplayName="Harbor Lights (old)"))
    world.item("04.item", manifest(lantern))
    world.lines(HEADER, record(1, new))
    assert world.read() == Library([Game("epic", HARBOR, "Harbor Lights: Remastered", str(new), link(NS2, ITEM2, APP2)),
                                    Game("epic", LANTERN, "Lantern Drift", str(lantern), link(NS, ITEM, APP))], [])


def test_a_game_the_preview_still_has_a_record_of_is_never_listed_from_a_legacy_manifest(world):
    folder, lantern = world.folder("HarborLights"), world.folder("Lantern Drift")
    world.item("01.item", harbor(folder))                      # whole, and its folder exists
    world.item("02.item", manifest(lantern))
    world.lines(HEADER, record(1, folder, complete=False),     # the Preview is updating it
                lantern_record(2, lantern), tombstone(2))      # and uninstalled this one: the legacy launcher's stands
    assert world.read() == Library([Game("epic", LANTERN, "Lantern Drift", str(lantern), link(NS, ITEM, APP))], [])


def test_no_games_command_line_is_kept_or_quoted(world):
    lantern, harbor_dir = world.folder("Lantern Drift"), world.folder("HarborLights")
    world.item("01.item", manifest(lantern, LaunchCommand=SECRET))
    world.item("02.item", json.dumps(harbor(harbor_dir, LaunchCommand=SECRET))[:-30])      # cut: not JSON
    world.item("03.item", manifest(lantern, AppName="LanternDriftSoundtrack", LaunchCommand=SECRET,
                                   AppCategories=DROP))          # an add-on, by its main app
    world.lines(HEADER, record(1, harbor_dir, launchCommand=SECRET),
                '{"installationId": "' + iid(2) + '", "launchCommand": "' + SECRET + '", oops}',
                tail='{"installationId":"' + iid(3) + '","launchCommand":"' + SECRET)
    lib = world.read()
    assert [g.id for g in lib.games] == [HARBOR, LANTERN] and len(lib.problems) == 2
    assert "AUTH_PASSWORD" not in repr(lib)


def test_read_takes_each_launchers_own_place_by_default(tmp_path, monkeypatch):
    program_data, local = tmp_path / "ProgramData", tmp_path / "AppData" / "Local"
    monkeypatch.setattr(epic, "_program_data", lambda: str(program_data))
    monkeypatch.setattr(epic, "_local_app_data", lambda: str(local))
    world = World(tmp_path)                                    # the same layout, under those two
    world.item("01.item", manifest(world.folder("Lantern Drift")))
    world.lines(HEADER, record(1, world.folder("HarborLights")))
    lib = epic.read()
    assert lib.problems == [] and [g.id for g in lib.games] == [HARBOR, LANTERN]


def test_a_folder_windows_cannot_name_is_named_not_guessed(world, monkeypatch):
    def refuse():
        raise OSError(0, "SHGetKnownFolderPath failed")

    monkeypatch.setattr(epic, "_program_data", refuse)
    monkeypatch.setattr(epic, "_local_app_data", refuse)
    world.item("01.item", manifest(world.folder("Lantern Drift")))   # in the working folder: never taken for one
    world.lines(HEADER, record(1, world.folder("HarborLights")))
    monkeypatch.chdir(world.manifests)
    (world.manifests / "installations.jsonl").write_bytes(world.journal.read_bytes())
    assert epic.read() == Library([], [
        "Windows did not say where the local AppData folder is, so the games of the Epic Games Launcher Preview were "
        "not read",
        "Windows did not say where the ProgramData folder is, so the games of the Epic Games Launcher were not read"])


def test_problems_are_one_line_each_and_capped_per_launcher(world):
    assert (stores.MAX_PROBLEMS, stores.MAX_PROBLEM_CHARS) == (20, 1000)
    odd = str(world.root / "Games" / ("Lantern" + chr(0x2028) + "Drift"))     # a line separator in a folder's name
    p = world.item("00.item", manifest(odd))
    long = world.item("01.item", manifest(odd, InstallLocation="C:/" + "x" * 1200))
    lib = world.read()
    assert lib.problems[0] == f"{p}: {odd.replace(chr(0x2028), '?')} does not exist, {NOT_LISTED}"
    assert lib.problems[1].startswith(f"{long}: its install folder was refused")
    assert "x" * 74 not in lib.problems[1] and len(lib.problems[1]) < 1000     # check_folder shows it cut short
    assert stores.plain("x" * 1200) == "x" * 997 + "..." and stores.plain("y" * 1000) == "y" * 1000   # the cap itself
    for n in range(2, 21):
        world.item(f"{n:02}.item", "{")
    lib = world.read()
    assert len(lib.problems) == 21
    assert lib.problems[-1] == "1 more problem with the files of the Epic Games Launcher, not listed here"
    for n in range(21, 25):
        world.item(f"{n:02}.item", "{")
    lib = world.read()
    assert len(lib.problems) == 21
    assert lib.problems[-1] == "5 more problems with the files of the Epic Games Launcher, not listed here"
    assert all(len(p) <= 1000 and p.isprintable() for p in lib.problems)


# ---------------------------------------------------------------- found by the review ---

def test_todays_manifest_names_a_base_game_with_an_empty_main_game_app_name(world):
    # Newer launchers write "" in the MainGame fields of a base game. A null, or no such key, is a base game too; an
    # add-on is told by its categories.
    lantern, harbor_dir, tide = world.folder("Lantern Drift"), world.folder("Harbor Lights"), world.folder("Tidewater")
    world.item("01.item", manifest(lantern, MainGameAppName="", MainGameCatalogNamespace="", MainGameCatalogItemId=""))
    world.item("02.item", harbor(harbor_dir))                     # the older shape: its own app name there
    world.item("03.item", manifest(tide, AppName="Tidewater", MainGameAppName=None, DisplayName="Tidewater"))
    world.item("04.item", manifest(lantern, AppName="LanternDriftSoundtrack", MainGameAppName=APP,
                                   AppCategories=["public", "games", "addons"]))                  # an add-on
    lib = world.read()
    assert lib.problems == []
    assert [g.id for g in lib.games] == [LANTERN, HARBOR, f"{NS}:{ITEM}:Tidewater"]


@pytest.mark.parametrize("main", [7, ["x"], {"a": 1}, True])
def test_a_main_game_app_name_that_is_not_text_is_named_where_it_is_read(world, main):
    p = world.item("01.item", manifest(world.folder("Lantern Drift"), MainGameAppName=main, AppCategories=DROP))
    world.item("02.item", harbor(world.folder("Harbor Lights"), MainGameAppName=main))     # with categories: unread
    world.item("03.item", manifest(world.folder("Lantern Drift"), AppName="LanternDriftBeta", MainGameAppName=main,
                                   bIsIncompleteInstall=True, AppCategories=DROP))       # unfinished: quiet
    lib = world.read()
    assert [g.id for g in lib.games] == [HARBOR]
    assert lib.problems == [f"{p}: its MainGameAppName is {stores.shown(main)}, not text, {NOT_LISTED}"]


def test_blank_lines_in_the_journal_are_nothing(world):
    harbor_dir, lantern = world.folder("HarborLights"), world.folder("LanternDrift")
    j = world.lines(HEADER, record(1, harbor_dir), "", "   ", lantern_record(2, lantern), "", "")
    lib = world.read()
    assert lib.problems == [] and [g.id for g in lib.games] == [HARBOR, LANTERN]
    j.write_bytes(j.read_bytes().replace(b"\n", b"\r\n"))       # blank lines that end in CRLF too
    assert world.read().problems == []


def test_records_whose_ids_are_not_text_are_named_and_the_others_listed(world):
    folder = world.folder("HarborLights")
    j = world.lines(HEADER,
                    record(1, folder, namespace=DROP),
                    record(2, folder, artifactId=7),
                    record(3, folder, artifactId=["x"]),
                    record(4, folder, itemId={"a": 1}),
                    lantern_record(5, world.folder("LanternDrift")))
    lib = world.read()
    assert [g.id for g in lib.games] == [LANTERN]
    assert lib.problems == [f"{j}, line 2: not an Epic catalog namespace: None, {NOT_LISTED}",
                            f"{j}, line 3: not an Epic app name: 7, {NOT_LISTED}",
                            f"{j}, line 4: not an Epic app name: ['x'], {NOT_LISTED}",
                            f"{j}, line 5: not an Epic catalog item id: {{'a': 1}}, {NOT_LISTED}"]


def test_a_manifest_whose_app_name_is_not_text_is_named_and_the_others_listed(world):
    p = world.item("01.item", manifest(world.folder("Lantern Drift"), AppName=["x"], MainGameAppName=DROP))
    world.item("02.item", harbor(world.folder("Harbor Lights")))
    lib = world.read()
    assert [g.id for g in lib.games] == [HARBOR]
    assert lib.problems == [f"{p}: not an Epic app name: ['x'], {NOT_LISTED}"]


def test_a_game_folder_windows_cannot_look_at_is_named_and_the_others_listed(world, monkeypatch):
    lantern, harbor_dir = world.folder("Lantern Drift"), world.folder("Harbor Lights")
    p = world.item("01.item", manifest(lantern))
    world.item("02.item", harbor(harbor_dir))
    real = stores._stat

    def stat(path):
        if path == str(lantern):
            raise OSError(0, "The device is not ready")         # a removable drive with its media out, say
        return real(path)

    monkeypatch.setattr(stores, "_stat", stat)
    lib = world.read()
    assert [g.id for g in lib.games] == [HARBOR]
    assert lib.problems == [f"{p}: {lantern} could not be looked at (The device is not ready), {NOT_LISTED}"]


def test_a_journal_windows_cannot_look_at_is_named(world):
    odd = str(world.journal.parent / "a?b.jsonl")                 # a name Windows refuses to look up at all
    lib = epic.read(manifests=str(world.manifests), installations=odd)
    assert lib.games == [] and len(lib.problems) == 1 and lib.problems[0].startswith(f"{odd} could not be ")


def test_a_preview_games_title_is_its_folders_name_cleaned(world):
    odd = world.folder("Lantern" + chr(0x202E) + "Drift")         # a mark that reorders the text after it
    plain = world.folder("Lanterne D" + chr(0xE9) + "rive")
    world.lines(HEADER, lantern_record(1, odd), record(2, plain))
    lib = world.read()
    assert lib.problems == []
    assert [g.title for g in lib.games] == ["Lantern Drift", "Lanterne D" + chr(0xE9) + "rive"]
    assert [g.folder for g in lib.games] == [str(odd), str(plain)]


def test_places_handed_to_read_go_through_the_local_disk_check_too(world):
    share = "\\\\nas\\games"
    lib = epic.read(manifests=share + "\\Manifests", installations=share + "\\installations.jsonl")
    assert lib == Library([], [f"{share}\\installations.jsonl is not a path on a local drive",
                               f"{share}\\Manifests is not a path on a local drive"])


def test_the_journal_is_split_no_further_than_its_cap(world, monkeypatch):
    calls, real = [], stores.read_text

    class Text(str):
        def split(self, sep=None, maxsplit=-1):
            calls.append(maxsplit)
            return str.split(self, sep, maxsplit)

    monkeypatch.setattr(stores, "read_text", lambda path, cap: Text(real(path, cap)))
    world.lines(HEADER, record(1, world.folder("HarborLights")))
    assert [g.id for g in world.read().games] == [HARBOR]
    assert calls == [epic.MAX_LINES + 1]                      # a file of line breaks alone is never split whole


def test_an_add_on_is_told_by_its_categories_and_a_game_naming_another_main_app_is_still_listed(world):
    lantern, harbor_dir = world.folder("Lantern Drift"), world.folder("Harbor Lights")
    # Fortnite's own manifest names its editor as MainGameAppName once the editor is installed: still a game.
    world.item("01.item", manifest(lantern, MainGameAppName=APP + "_Studio"))
    world.item("02.item", harbor(harbor_dir, AppCategories=["public", "games", "addons", "addons/launchable"]))
    world.item("03.item", manifest(lantern, AppName="LanternDriftExtra", AppCategories=["public", "games", "addons"]))
    world.item("04.item", manifest(lantern, AppName="LanternDriftOld", AppCategories=DROP))   # names LanternDrift
    world.item("05.item", manifest(world.folder("Tidewater"), AppName="Tidewater", MainGameAppName="",
                                   AppCategories=DROP))
    world.item("06.item", manifest(world.folder("Kiln"), AppName="Kiln", MainGameAppName=None, AppCategories=DROP))
    lib = world.read()
    assert lib.problems == []
    assert [g.id for g in lib.games] == [LANTERN, HARBOR, f"{NS}:{ITEM}:Tidewater", f"{NS}:{ITEM}:Kiln"]


def catalog(world, data) -> Path:
    """The Preview's catalog cache beside its journal: an object written as JSON, or text as it is."""
    path = world.journal.parent / "cache" / "prod" / "catalog-cache.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((data if isinstance(data, str) else json.dumps(data)).encode("utf-8"))
    return path


def titled(item: str, title) -> dict:
    return {item: {"item": {"id": item, "title": title, "namespace": NS}, "expiresAt": "2026-11-01T10:00:00Z"}}


def test_a_preview_games_title_comes_from_the_previews_catalog_cache(world):
    lantern, harbor_dir = world.folder("LanternDriftQx7Pz"), world.folder("HarborLightsZz9")   # the random suffix
    world.lines(HEADER, lantern_record(1, lantern), record(2, harbor_dir))
    world.item("01.item", harbor(world.folder("Harbor Lights")))       # the legacy launcher's title for the second
    catalog(world, {"items": titled(ITEM, "Lantern Drift" + chr(7))})
    lib = world.read()
    assert lib.problems == [] and [g.title for g in lib.games] == ["Lantern Drift", "Harbor Lights"]
    catalog(world, {"items": {**titled(ITEM, "Lantern Drift"), **titled(ITEM2, "Harbor Lights Remastered")}})
    assert [g.title for g in world.read().games] == ["Lantern Drift", "Harbor Lights Remastered"]   # the catalog's first
    catalog(world, {"items": {ITEM: {"item": {"title": 7}}, "x": "y", ITEM2: {"item": "z"}, "w": {"item": {}}}})
    lib = world.read()                                                # entries of another shape: no title, quietly
    assert lib.problems == [] and [g.title for g in lib.games] == ["LanternDriftQx7Pz", "Harbor Lights"]
    catalog(world, {"items": titled(ITEM2, "   " + chr(0x200B))})       # nothing to show: the legacy title instead
    assert [g.title for g in world.read().games] == ["LanternDriftQx7Pz", "Harbor Lights"]
    (world.journal.parent / "cache" / "prod" / "catalog-cache.json").unlink()
    lib = world.read()                                                # a cache that is not there is no problem
    assert lib.problems == [] and [g.title for g in lib.games] == ["LanternDriftQx7Pz", "Harbor Lights"]


def test_a_catalog_cache_that_cannot_be_read_is_named_and_the_games_keep_their_folders_names(world, monkeypatch):
    world.lines(HEADER, lantern_record(1, world.folder("LanternDriftQx7Pz")))
    then = "; the Preview's games are titled by their folders' names"
    for data, says in [("not json", "was not read: it is not JSON"), ([1, 2], "holds no catalog items"),
                       ({"items": [1]}, "holds no catalog items"), ({"schemaVersion": 1}, "holds no catalog items")]:
        path = catalog(world, data)
        lib = world.read()
        assert [g.title for g in lib.games] == ["LanternDriftQx7Pz"] and len(lib.problems) == 1, data
        assert lib.problems[0].startswith(f"{path} {says}") and lib.problems[0].endswith(then), data
    monkeypatch.setattr(epic, "MAX_CATALOG_ITEMS", 1)
    path = catalog(world, {"items": {**titled(ITEM, "Lantern Drift"), **titled(ITEM2, "Harbor Lights")}})
    assert world.read().problems == [f"{path} holds more than 1 catalog items{then}"]
    monkeypatch.setattr(epic, "MAX_CATALOG_ITEMS", 2)
    assert world.read().problems == [] and [g.title for g in world.read().games] == ["Lantern Drift"]
    real = stores._stat

    def stat(path):
        if path == str(world.journal.parent / "cache" / "prod" / "catalog-cache.json"):
            raise PermissionError(13, "Access is denied")
        return real(path)

    monkeypatch.setattr(stores, "_stat", stat)
    assert world.read().problems == [f"{path} could not be read (Access is denied){then}"]
    monkeypatch.setattr(stores, "_stat", real)
    path.unlink()
    path.mkdir()                                                    # a folder where the file should be
    assert world.read().problems == [f"{path} is not a file, so it was not read{then}"]
    path.rmdir()
    path.write_bytes(b"{" + b" " * 40 + b"}")
    monkeypatch.setattr(epic, "MAX_CATALOG_BYTES", 10)
    assert world.read().problems == [f"{path} is larger than 10 bytes, so it was not read{then}"]   # one path
    monkeypatch.undo()
    assert (epic.MAX_CATALOG_BYTES, epic.MAX_CATALOG_ITEMS, epic.CATALOG) == \
        (16 << 20, 100_000, ("cache", "prod", "catalog-cache.json"))


def test_the_catalog_cache_is_read_only_when_the_journal_lists_a_game(world, monkeypatch):
    catalog(world, "not json")
    world.lines(HEADER)
    read, real = [], stores.read_text
    monkeypatch.setattr(stores, "read_text", lambda path, cap: read.append(Path(path).name) or real(path, cap))
    assert world.read() == Library([], [])
    assert read == ["installations.jsonl"]
    world.lines(HEADER, record(1, world.folder("HarborLights"), complete=False),       # a download in progress
                record(2, world.folder("HarborLights"), parentInstallationId=iid(1)),    # an add-on
                record(3, world.folder("HarborLights"), namespace="ue"))                 # the engine
    assert world.read() == Library([], []) and read[-1] == "installations.jsonl" and \
        "catalog-cache.json" not in read


def test_legacy_manifests_are_let_go_one_by_one(world, monkeypatch):
    """Each manifest is judged as soon as it is read, so no more than one parsed manifest is ever held: another
    account's folder of small JSON objects would otherwise take gigabytes."""
    import weakref
    for n in range(4):
        world.item(f"{n:02}.item", manifest(world.folder(f"Game {n}"), AppName=f"Game{n}"))
    alive, refs, real_json, real_judge = [], [], epic._json, epic._manifest_game

    class Parsed(dict):
        pass

    def parse(text, line=False):
        obj = real_json(text, line)
        if isinstance(obj, dict) and not line:
            obj = Parsed(obj)
            refs.append(weakref.ref(obj))
        return obj

    def judge(path, obj, taken):
        alive.append(sum(r() is not None for r in refs))
        return real_judge(path, obj, taken)

    monkeypatch.setattr(epic, "_json", parse)
    monkeypatch.setattr(epic, "_manifest_game", judge)
    lib = world.read()
    assert len(lib.games) == 4 and len(refs) == 4
    assert alive == [1, 1, 1, 1]                                    # the one being judged, and no other
