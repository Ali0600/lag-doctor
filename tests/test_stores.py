"""The readers' shared parts (lagdoctor/stores/__init__.py): read_text and the other file access every store file and
folder goes through, with target.local_problem under it, which follows each junction and link before anything is
opened; the problems list, the way a refused value is quoted, and the titles. The readers' own tests
(test_stores_steam.py, test_stores_epic.py) read whole libraries; these pin each refusal of what they share.

Junctions are made for real in tmp_path (no rights are needed for one); a link onto a network share or another
drive cannot be made here, so its target is what target._readlink gives for a real junction."""
from __future__ import annotations

import _winapi
import ast
import ctypes
import os
from pathlib import Path

import pytest

from lagdoctor import stores
from lagdoctor.capture import target
from lagdoctor.capture.gamefiles import FILE_ATTRIBUTE_OFFLINE
from lagdoctor.stores import epic, steam
from lagdoctor.winapi import storage

BOM = chr(0xFEFF)
BS = chr(92)
SHARE = BS + BS + "?" + BS + "UNC" + BS + "nas" + BS + "games"      # a link's target on a network share


def never_opened(path):
    pytest.fail(f"{path} was opened")


def set_offline(path: Path) -> None:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.SetFileAttributesW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
    k32.SetFileAttributesW.restype = ctypes.c_int
    assert k32.SetFileAttributesW(str(path), FILE_ATTRIBUTE_OFFLINE), ctypes.get_last_error()
    assert os.stat(path).st_file_attributes & FILE_ATTRIBUTE_OFFLINE         # it took


def test_read_text_takes_utf8_up_to_its_cap_and_drops_one_byte_order_mark(tmp_path):
    plain = tmp_path / "plain.vdf"
    plain.write_bytes(b'"Lantern Drift"')                                   # 15 bytes: exactly the cap
    assert stores.read_text(plain, 15) == '"Lantern Drift"'
    marked = tmp_path / "marked.vdf"
    marked.write_bytes((BOM + "Harbor Lights").encode("utf-8"))
    assert stores.read_text(marked, 100) == "Harbor Lights"
    twice = tmp_path / "twice.vdf"
    twice.write_bytes((BOM + BOM + "Kiln").encode("utf-8"))
    assert stores.read_text(twice, 100) == BOM + "Kiln"                      # only the first is a byte order mark


def test_read_text_refuses_a_file_larger_than_its_cap_before_opening_it(tmp_path, monkeypatch):
    big = tmp_path / "big.vdf"
    big.write_bytes(b"x" * 16)
    monkeypatch.setattr(stores, "_open", never_opened)
    with pytest.raises(ValueError) as caught:
        stores.read_text(big, 15)
    assert str(caught.value) == f"{big} is larger than 15 bytes, so it was not read"


def test_read_text_reads_one_byte_past_its_cap_so_a_file_that_grew_is_refused(tmp_path, monkeypatch):
    path = tmp_path / "growing.vdf"
    path.write_bytes(b"abc")                                                # what the stat sees

    class Grown:                                                            # what the read then finds
        asked: list[int] = []

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, n=-1):
            Grown.asked.append(n)
            return b"x" * (n if n >= 0 else 1 << 20)

    monkeypatch.setattr(stores, "_open", lambda p: Grown())
    with pytest.raises(ValueError) as caught:
        stores.read_text(path, 10)
    assert str(caught.value) == f"{path} is larger than 10 bytes, so it was not read"
    assert Grown.asked == [11]                                              # never the whole file


def test_read_text_never_opens_a_file_kept_online_only(tmp_path, monkeypatch):
    cloud = tmp_path / "cloud.vdf"
    cloud.write_bytes(b'"Lantern Drift"')
    set_offline(cloud)
    monkeypatch.setattr(stores, "_open", never_opened)
    with pytest.raises(ValueError) as caught:
        stores.read_text(cloud, 100)
    assert str(caught.value) == f"{cloud} is kept online only, so it was not read"


def test_read_text_names_a_file_it_could_not_read_in_windows_own_words(tmp_path):
    missing = tmp_path / "missing.vdf"
    with pytest.raises(ValueError) as caught:                               # the stat fails
        stores.read_text(missing, 100)
    assert str(caught.value) == f"{missing} could not be read (The system cannot find the file specified)"
    folder = tmp_path / "steamapps"
    folder.mkdir()
    with pytest.raises(ValueError) as caught:                               # the stat works, the open fails
        stores.read_text(folder, 100)
    assert str(caught.value) == f"{folder} could not be read (Permission denied)"


def test_read_text_names_a_file_that_is_not_utf8(tmp_path):
    latin = tmp_path / "latin.vdf"
    latin.write_bytes(b'"caf\xe9"')
    with pytest.raises(ValueError) as caught:
        stores.read_text(latin, 100)
    assert str(caught.value) == f"{latin} is not UTF-8 text"


def test_title_is_the_stores_name_cleaned_and_cut_else_the_fallback():
    assert stores.title("  Lantern" + chr(7) + "  Drift" + chr(0x202E) + " ", "x") == "Lantern Drift"
    for nothing in [None, 7, "", "   ", " " + chr(0x200B) + " "]:
        assert stores.title(nothing, "Harbor Lights") == "Harbor Lights"
    long = stores.title("Kiln " * 60, "x")                                  # 300 characters
    assert len(long) == 200 and long.endswith("...") and long.startswith("Kiln Kiln")
    assert stores.title("k" * 200, "x") == "k" * 200                       # at the cap: kept whole
    assert stores.name(None) is None and stores.name("  Kiln  ") == "Kiln"


def test_the_fallback_title_is_cleaned_as_the_stores_name_is_and_a_game_always_has_one():
    # A folder's name may hold what Windows keeps in a name and a page cannot show: a mark that reorders text, a line
    # separator, a next-line character. The store gave no name, so the folder's is the title, cleaned.
    for folder, shown in [("Lantern" + chr(0x202E) + "Drift", "Lantern Drift"),
                          ("Harbor" + chr(0x2028) + "Lights", "Harbor Lights"),
                          ("Kiln" + chr(0x85) + "Works", "Kiln Works")]:
        assert stores.title(None, folder) == shown
    assert stores.title(None, "Tide " * 60).startswith("Tide Tide") and len(stores.title(None, "Tide " * 60)) == 200
    assert stores.title(None, chr(0x200B)) == stores.UNNAMED == "Unnamed game"     # nothing to show at all
    assert stores.title("", "") == "Unnamed game"


# ---------------------------------------------------------------- problems, in plain words ---

def test_problems_are_one_line_each_cut_at_1000_and_counted_past_20():
    assert (stores.MAX_PROBLEMS, stores.MAX_PROBLEM_CHARS, stores.MAX_SHOWN_CHARS) == (20, 1000, 60)
    p = stores.Problems("Steam")
    p.add("a\r\nb" + chr(0x2028) + "c" + chr(0x202E) + "d")
    p.add("x" * 1200)
    p.add("y" * 1000)
    assert p.lines() == ["a??b?c?d", "x" * 997 + "...", "y" * 1000]
    for n in range(3, 20):
        p.add(f"problem {n}")
    assert len(p.lines()) == 20                                              # exactly the cap: no count line
    p.add("one more")
    assert p.lines()[-1] == "1 more problem with the files of Steam, not listed here" and len(p.lines()) == 21
    p.add("and another")
    p.add("and a third")
    assert p.lines()[-1] == "3 more problems with the files of Steam, not listed here"
    assert stores.plain("Lantern Drift") == "Lantern Drift"


def test_a_refused_value_is_quoted_short_and_in_the_files_own_words():
    assert [stores.shown(v) for v in (None, True, False, [1], {"a": "b"}, 7, "four")] == \
        ["missing", "true", "false", "a list", "a block", "7", "'four'"]
    assert stores.shown("k" * 58) == "'" + "k" * 58 + "'"                     # 60 characters: kept whole
    assert stores.shown("k" * 59) == "'" + "k" * 56 + "..."                  # 61: cut to 60
    assert stores.shown("q" * 100_000) == "'" + "q" * 56 + "..."
    assert stores.shown(chr(0xE0001) * 4096) == repr(chr(0xE0001) * 60)[:57] + "..."   # escapes cannot grow it


def test_a_long_text_is_cut_before_its_repr_is_built():
    calls = []

    class Long(str):
        def __repr__(self):
            calls.append(len(self))
            return str.__repr__(self)

    assert stores.shown(Long("q" * 100_000)) == "'" + "q" * 56 + "..."
    assert calls == []                      # only its first 60 characters (a plain str) were ever quoted


# ---------------------------------------------------------------- the file access: links and drives ---

def junction(link: Path, to: Path) -> Path:
    """A real directory junction at `link` onto `to` (no rights needed)."""
    _winapi.CreateJunction(str(to), str(link))
    assert os.path.isjunction(link)
    return link


def reading(monkeypatch, targets: dict[str, str]) -> list[str]:
    """target._readlink gives `targets[path]` for those paths (real junctions whose target the test sets), and every
    path a link was read for is recorded."""
    read, real = [], target._readlink

    def fake(path):
        read.append(path)
        return targets[path] if path in targets else real(path)

    monkeypatch.setattr(target, "_readlink", fake)
    return read


def opened(monkeypatch) -> list[str]:
    """Every path stores' file access handed to os.stat or os.scandir."""
    out, real_stat, real_scandir = [], stores._stat, stores._scandir
    monkeypatch.setattr(stores, "_stat", lambda p: out.append(p) or real_stat(p))
    monkeypatch.setattr(stores, "_scandir", lambda p: out.append(p) or real_scandir(p))
    return out


def test_a_junction_to_a_folder_on_a_local_disk_is_followed(tmp_path, monkeypatch):
    (tmp_path / "Real" / "Lantern Drift").mkdir(parents=True)
    lib = junction(tmp_path / "Library", tmp_path / "Real")
    read = reading(monkeypatch, {})
    game = str(lib / "Lantern Drift")
    assert target.local_problem(game) is None
    assert read == [str(lib)]                                                # its target was read, then walked
    with stores.scandir(lib) as listing:
        assert stores.is_folder(game) and [e.name for e in listing] == ["Lantern Drift"]


def test_a_link_onto_a_network_share_is_refused_before_anything_is_opened_through_it(tmp_path, monkeypatch):
    (tmp_path / "Real" / "steamapps").mkdir(parents=True)
    lib = junction(tmp_path / "Library", tmp_path / "Real")
    reading(monkeypatch, {str(lib): SHARE})
    seen = opened(monkeypatch)
    steamapps = lib / "steamapps"
    why = f"{lib} is a link to {SHARE}, which is not a path on a local drive"
    assert target.local_problem(str(steamapps)) == why
    for call in (stores.scandir, stores.stat, stores.is_folder, stores.exists):
        with pytest.raises(target.NotLocal) as caught:
            call(steamapps)
        assert caught.value.strerror == why and isinstance(caught.value, OSError)
    with pytest.raises(ValueError) as caught:
        stores.read_text(steamapps / "libraryfolders.vdf", 100)
    assert str(caught.value) == why
    assert seen == []                                                        # nothing was opened through it
    for share in ("//nas/games", BS + BS + "nas" + BS + "games", "/" + BS + "nas"):   # a share in either slash
        reading(monkeypatch, {str(lib): share})
        assert target.local_problem(str(steamapps)).endswith("which is not a path on a local drive")
    reading(monkeypatch, {str(lib): "//nas" + chr(0x2028) + "/" + "g" * 100})        # one line, cut short
    why = target.local_problem(str(steamapps))
    assert why == f"{lib} is a link to //nas?/{'g' * 70}..., which is not a path on a local drive"


def test_a_link_onto_a_drive_that_is_no_local_disk_is_refused(tmp_path, monkeypatch):
    lib = junction(tmp_path / "Library", tmp_path)
    reading(monkeypatch, {str(lib): BS + BS + "?" + BS + "Q:" + BS + "Games"})
    real_type = storage.drive_type
    monkeypatch.setattr(storage, "drive_type", lambda root: 4 if root == "Q:" + BS else real_type(root))
    assert target.local_problem(str(lib / "Lantern Drift")) == \
        f"{lib} is a link to {BS}{BS}?{BS}Q:{BS}Games, which is not on a local fixed or removable disk"
    monkeypatch.setattr(storage, "drive_type", lambda root: storage.DRIVE_REMOVABLE if root == "Q:" + BS
                        else real_type(root))
    looked, real_lstat = [], target._lstat

    def lstat(path):                                                          # Q: is never really looked at
        looked.append(path)
        if path.upper().startswith("Q:"):
            raise FileNotFoundError(2, "The system cannot find the path specified")
        return real_lstat(path)

    monkeypatch.setattr(target, "_lstat", lstat)
    assert target.local_problem(str(lib / "Lantern Drift")) is None          # a removable disk is a local one
    assert looked[-1] == "Q:" + BS + "Games"                                 # walked on there, and stopped
    monkeypatch.setattr(storage, "drive_type", lambda root: 1 if root == "Q:" + BS else real_type(root))
    assert target.local_problem(str(lib / "Lantern Drift")) == \
        f"{lib} is a link to {BS}{BS}?{BS}Q:{BS}Games, which is on Q:, where no disk is connected now"


def test_a_volume_mounted_in_a_folder_is_followed_on_its_volume(tmp_path, monkeypatch):
    (tmp_path / "Real" / "Lantern Drift").mkdir(parents=True)
    buf = ctypes.create_unicode_buffer(64)
    assert ctypes.WinDLL("kernel32").GetVolumeNameForVolumeMountPointW(tmp_path.anchor, buf, 64)
    volume = buf.value                                                       # \\?\Volume{...}\, ending in a backslash
    mount = junction(tmp_path / "Mount", tmp_path / "Real")
    read = reading(monkeypatch, {str(mount): volume + str(tmp_path / "Real")[3:]})
    looked, real_lstat = [], target._lstat
    monkeypatch.setattr(target, "_lstat", lambda p: looked.append(p) or real_lstat(p))
    assert target.local_problem(str(mount / "Lantern Drift")) is None
    assert read == [str(mount)] and looked[-1] == volume + str(tmp_path / "Real" / "Lantern Drift")[3:]
    real_type = storage.drive_type
    monkeypatch.setattr(storage, "drive_type", lambda root: 4 if root == volume else real_type(root))
    assert target.local_problem(str(mount / "Lantern Drift")).endswith("which is not on a local fixed or removable disk")


def test_a_relative_link_is_read_against_its_own_folder(tmp_path, monkeypatch):
    (tmp_path / "Real" / "Lantern Drift").mkdir(parents=True)
    (tmp_path / "Links").mkdir()
    lib = junction(tmp_path / "Links" / "Library", tmp_path / "Real")
    looked, real_lstat = [], target._lstat
    monkeypatch.setattr(target, "_lstat", lambda p: looked.append(p) or real_lstat(p))
    reading(monkeypatch, {str(lib): ".." + BS + "Real"})
    assert target.local_problem(str(lib / "Lantern Drift")) is None
    assert looked[-1] == str(tmp_path / "Real" / "Lantern Drift")
    reading(monkeypatch, {str(lib): BS + str(tmp_path / "Real")[3:]})        # from the drive's root
    assert target.local_problem(str(lib / "Lantern Drift")) is None
    assert looked[-1] == str(tmp_path / "Real" / "Lantern Drift")
    reading(monkeypatch, {str(lib): "D:Games"})                              # from a drive's current folder: never
    assert target.local_problem(str(lib / "Lantern Drift")) == \
        f"{lib} is a link to D:Games, which is not a path on a local drive"


def test_a_path_through_more_than_eight_links_is_refused(tmp_path):
    assert target.MAX_LINKS == 8
    (tmp_path / "Real" / "Lantern Drift").mkdir(parents=True)
    link = tmp_path / "Real"
    for n in range(8):
        link = junction(tmp_path / f"J{n}", link)
    assert target.local_problem(str(link / "Lantern Drift")) is None        # 8 links: followed
    ninth = junction(tmp_path / "J8", link)
    assert target.local_problem(str(ninth / "Lantern Drift")) == \
        f"{ninth / 'Lantern Drift'} leads through more than 8 links, so it was not opened"
    loop = tmp_path / "Loop"
    loop.mkdir()
    junction(loop / "Again", loop)                                          # a junction onto its own folder
    deep = loop / "Again" / "Again" / "Again" / "Again" / "Again" / "Again" / "Again" / "Again" / "Again" / "x"
    assert "leads through more than 8 links" in target.local_problem(str(deep))


def test_a_folder_kept_online_only_is_not_opened(tmp_path, monkeypatch):
    cloud = tmp_path / "Cloud"
    (cloud / "Lantern Drift").mkdir(parents=True)
    set_offline(cloud)
    seen = opened(monkeypatch)
    assert target.local_problem(str(cloud / "Lantern Drift")) == f"{cloud} is kept online only, so it was not read"
    with pytest.raises(target.NotLocal):
        stores.scandir(cloud)
    assert seen == []


def test_a_link_that_cannot_be_read_or_names_no_folder_is_refused(tmp_path, monkeypatch):
    lib = junction(tmp_path / "Library", tmp_path)

    def broken(path):
        raise OSError(4390, "The file or directory is not a reparse point")

    monkeypatch.setattr(target, "_readlink", broken)
    assert target.local_problem(str(lib / "x")) == \
        f"{lib} is a link that could not be read (The file or directory is not a reparse point)"
    for odd in ["", "C:" + BS + "Ga" + chr(10) + "mes", "C:" + BS + "x" * 1030, 7]:
        reading(monkeypatch, {str(lib): odd})
        assert target.local_problem(str(lib / "x")).endswith("which is not a folder's path")


def test_a_part_that_cannot_be_looked_at_is_named_and_a_missing_one_ends_the_walk(tmp_path, monkeypatch):
    (tmp_path / "Locked").mkdir()
    real = target._lstat

    def lstat(path):
        if path == str(tmp_path / "Locked"):
            raise PermissionError(13, "Access is denied")
        return real(path)

    monkeypatch.setattr(target, "_lstat", lstat)
    assert target.local_problem(str(tmp_path / "Locked" / "x")) == \
        f"{tmp_path / 'Locked'} could not be looked at (Access is denied)"
    assert target.local_problem(str(tmp_path / "Gone" / "Locked" / "x")) is None    # nothing past Gone to open
    with pytest.raises(FileNotFoundError):
        stores.stat(tmp_path / "Gone" / "x")


def test_only_paths_on_a_local_drive_are_walked(tmp_path, monkeypatch):
    for path in [BS + BS + "nas" + BS + "games", "relative" + BS + "x", "C:x", None, 7,
                 BS + BS + "?" + BS + "UNC" + BS + "nas" + BS + "x", BS + BS + "." + BS + "PhysicalDrive0"]:
        assert target.local_problem(path).endswith("is not a path on a local drive")
    for path in ["C:" + BS + "Ga" + chr(0) + "mes", "C:" + BS + "Ga" + chr(10) + "mes"]:   # a NUL, a line break
        assert target.local_problem(path) == "C:" + BS + "Ga?mes is not a path on a local drive"
        with pytest.raises(target.NotLocal):
            stores.is_folder(path)
    with pytest.raises(target.NotLocal) as caught:
        stores.scandir(None)                                                  # no path at all: refused, not a crash
    assert caught.value.strerror == "None is not a path on a local drive"
    monkeypatch.setattr(storage, "drive_type", lambda root: 1)
    assert target.local_problem(str(tmp_path)) == f"{tmp_path} is on C:, where no disk is connected now"
    assert target.local_problem(BS + BS + "?" + BS + "Volume{00000000-0000-0000-0000-000000000001}" + BS + "x") == \
        BS + BS + "?" + BS + "Volume{00000000-0000-0000-0000-000000000001}" + BS + "x is on a volume that is not " \
        "connected now"
    monkeypatch.setattr(storage, "drive_type", lambda root: 4)
    assert target.local_problem(str(tmp_path)) == f"{tmp_path} is not on a local fixed or removable disk"
    assert target.local_problem(BS + BS + "?" + BS + str(tmp_path)) == \
        f"{BS}{BS}?{BS}{tmp_path} is not on a local fixed or removable disk"


def test_dot_dot_parts_are_worked_out_from_the_text_before_any_link_is_met(tmp_path, monkeypatch):
    (tmp_path / "Real").mkdir()
    lib = junction(tmp_path / "Library", tmp_path / "Real")
    read = reading(monkeypatch, {str(lib): SHARE})
    # the path names the junction, then leaves it again: Windows never goes through it, and neither does the walk
    assert target.local_problem(str(lib) + BS + ".." + BS + "Real") is None and read == []


READERS = {"epic", "keyvalues", "launch", "steam"}     # every module of lagdoctor/stores but the file access itself
PATH_MODULES = {"os", "ntpath", "posixpath", "genericpath", "pathlib", "io", "shutil", "glob", "importlib",
                "builtins", "winreg"}
ALLOWED = {"os": {"path", "fspath", "PathLike", "DirEntry", "stat_result"}, "os.path": {"join", "dirname"},
           "ntpath": {"basename"}}
FILE_IO = {"read_text", "read_bytes", "write_text", "write_bytes", "open", "exists", "is_dir", "is_file", "iterdir",
           "stat", "lstat", "glob", "rglob", "resolve", "samefile", "is_symlink", "is_junction", "readlink", "walk",
           "is_mount", "touch", "mkdir", "unlink", "rmdir", "rename", "chmod", "listdir", "scandir"}


def file_access_problems(source: str, name: str) -> list[str]:
    """Every place in a reader's source that could reach a file or folder other than through stores' file access
    (stores.scandir, stat, is_folder, exists, read_text, which ask target.local_problem first). A listing's own
    entry may answer is_file and stat only without following links (follow_symlinks=False): it then reads what the
    listing holds and opens nothing."""
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    out = []
    for node in ast.walk(tree):
        where = f"{name}:{getattr(node, 'lineno', '?')}"
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in PATH_MODULES and (a.asname or a.name not in ("os", "ntpath", "winreg")):
                    out.append(f"{where}: import {a.name}" + (f" as {a.asname}" if a.asname else ""))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.split(".")[0] in PATH_MODULES and not (module == "pathlib" and
                                                             [a.name for a in node.names] == ["Path"]):
                out.append(f"{where}: from {module} import {', '.join(a.name for a in node.names)}")
        elif isinstance(node, ast.Name) and node.id in ("os", "ntpath"):
            up = parents.get(node)
            if not (isinstance(up, ast.Attribute) and up.value is node):
                out.append(f"{where}: {node.id} itself, under another name")
            elif node.id == "os" and up.attr == "path":
                upup = parents.get(up)
                if not (isinstance(upup, ast.Attribute) and upup.attr in ALLOWED["os.path"]):
                    out.append(f"{where}: os.path.{getattr(upup, 'attr', '')}")
            elif up.attr not in ALLOWED[node.id]:
                out.append(f"{where}: {node.id}.{up.attr}")
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and \
                node.value.id in ("stores", "target") and node.attr.startswith("_"):
            out.append(f"{where}: {node.value.id}.{node.attr}, which skips local_problem")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in ("open", "__import__", "exec", "eval", "compile"):
                out.append(f"{where}: {node.func.id}()")
            elif node.func.id == "getattr" and node.args and isinstance(node.args[0], ast.Name) and \
                    node.args[0].id in PATH_MODULES | {"stores", "target"}:
                out.append(f"{where}: getattr({node.args[0].id}, ...)")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in FILE_IO:
            owner = node.func.value
            if isinstance(owner, ast.Name) and owner.id == "stores":
                continue
            if isinstance(owner, ast.Name) and owner.id == "entry" and node.func.attr in ("is_file", "stat") and \
                    any(k.arg == "follow_symlinks" and isinstance(k.value, ast.Constant) and k.value.value is False
                        for k in node.keywords):
                continue
            out.append(f"{where}: .{node.func.attr}()")
    return out


def test_the_readers_reach_files_and_folders_only_through_stores_file_access():
    """Every module of lagdoctor/stores but its __init__ (the file access itself) is scanned, so a new reader is
    scanned the day it is added (or READERS fails until it is named here)."""
    import pkgutil
    assert {m.name for m in pkgutil.iter_modules(stores.__path__)} == READERS
    found = []
    for name in sorted(READERS):
        path = Path(stores.__file__).parent / f"{name}.py"
        found += file_access_problems(path.read_bytes().decode("utf-8"), path.name)
    assert found == []


@pytest.mark.parametrize("line", [
    "x = ntpath.isdir(p)", "import os as _os", "import os.path as osp", "from ntpath import exists",
    "from os import scandir", "x = os.scandir(p)", "x = os.path.isdir(p)", "x = os.listdir(p)", "o = os",
    "x = stores._stat(p)", "x = stores._scandir(p)", "x = target._real_path(p)", "x = target._lstat(p)",
    "x = entry.is_file()", "x = entry.stat(follow_symlinks=True)", "x = other.is_file(follow_symlinks=False)",
    "x = Path(p).walk()", "x = Path(p).is_mount()", "x = Path(p).read_bytes()", "x = open(p)",
    "x = getattr(os, 'scandir')(p)", "x = __import__('os')", "import shutil", "import io", "import posixpath",
    "from pathlib import PurePath, Path", "import winreg as wr",
])
def test_the_file_access_scan_catches_each_way_around_it(line):
    base = "import os\nimport ntpath\nfrom pathlib import Path\nfrom lagdoctor import stores\n"
    assert file_access_problems(base + "x = 1\n", "planted.py") == []
    assert file_access_problems(base + line + "\n", "planted.py") != [], line


def test_the_epic_reader_asks_windows_for_its_places_only_in_its_seams():
    tree = ast.parse(Path(epic.__file__).read_bytes().decode("utf-8"))
    owner = {}
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        for node in ast.walk(fn):
            owner.setdefault(node, fn.name)
    used = {}
    for node in ast.walk(tree):                     # at the module's top level too, not only inside a function
        if isinstance(node, ast.Name) and node.id == "folders":
            used.setdefault("(folders itself)", set()).add(owner.get(node, "<module>"))
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "folders":
            used.setdefault(node.attr, set()).add(owner.get(node, "<module>"))
    assert used == {"program_data": {"_program_data"}, "local_app_data": {"_local_app_data"},
                    "(folders itself)": {"_program_data", "_local_app_data"}}
