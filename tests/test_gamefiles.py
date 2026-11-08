"""The game folder check (capture/gamefiles.py) on folders each test lays out, and two live readouts: Windows'
KnownDLLs list, and where Windows keeps this account's own folders (winapi/folders.py)."""
from __future__ import annotations

import ctypes
import hashlib
import os
import subprocess
import types
import winreg
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

import pytest

from lagdoctor.capture import gamefiles as g
from lagdoctor.winapi import folders

POWERSHELL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" \
    / "powershell.exe"


def put(root: Path, rel: str, data: bytes = b"x") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def names(files: list[dict]) -> list[str]:
    return sorted((f["name"] for f in files), key=str.lower)


class Clock:
    """A fake monotonic clock: each reading moves it on by `step`, as the work between two readings takes time."""

    def __init__(self, step: float = 0.0) -> None:
        self.t, self.step = 100.0, step

    def __call__(self) -> float:
        self.t += self.step
        return self.t


# ------------------------------------------------------------------------------------------ the DLL names --

def _known_dlls_key() -> set[str]:
    out, i = set(), 0
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                        r"SYSTEM\CurrentControlSet\Control\Session Manager\KnownDLLs") as key:
        while True:
            try:
                _, value, kind = winreg.EnumValue(key, i)
            except OSError:
                return out
            if kind == winreg.REG_SZ:
                out.add(value.lower())
            i += 1


class _UNICODE_STRING(ctypes.Structure):
    _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT), ("Buffer", wintypes.LPWSTR)]


class _OBJECT_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Length", wintypes.ULONG), ("RootDirectory", wintypes.HANDLE),
                ("ObjectName", ctypes.POINTER(_UNICODE_STRING)), ("Attributes", wintypes.ULONG),
                ("SecurityDescriptor", ctypes.c_void_p), ("SecurityQualityOfService", ctypes.c_void_p)]


class _OBJECT_DIRECTORY_INFORMATION(ctypes.Structure):
    _fields_ = [("Name", _UNICODE_STRING), ("TypeName", _UNICODE_STRING)]


def _known_dlls_directory() -> set[str]:
    """The names in the \\KnownDlls object directory: the key's DLLs and the DLLs they import (ntdll's own calls,
    the way WinObj reads it)."""
    ntdll = ctypes.WinDLL("ntdll")
    ntdll.NtOpenDirectoryObject.argtypes = [ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD,
                                            ctypes.POINTER(_OBJECT_ATTRIBUTES)]
    ntdll.NtQueryDirectoryObject.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.ULONG, wintypes.BOOLEAN,
                                             wintypes.BOOLEAN, ctypes.POINTER(wintypes.ULONG),
                                             ctypes.POINTER(wintypes.ULONG)]
    ntdll.NtClose.argtypes = [wintypes.HANDLE]
    for fn in (ntdll.NtOpenDirectoryObject, ntdll.NtQueryDirectoryObject, ntdll.NtClose):
        fn.restype = ctypes.c_long
    name = "\\KnownDlls"
    us = _UNICODE_STRING(len(name) * 2, len(name) * 2 + 2, name)
    oa = _OBJECT_ATTRIBUTES(ctypes.sizeof(_OBJECT_ATTRIBUTES), None, ctypes.pointer(us), 0x40, None, None)
    handle = wintypes.HANDLE()
    status = ntdll.NtOpenDirectoryObject(ctypes.byref(handle), 0x1, ctypes.byref(oa))     # DIRECTORY_QUERY
    assert status == 0, f"NtOpenDirectoryObject: 0x{status & 0xFFFFFFFF:08X}"
    out: set[str] = set()
    try:
        buf = ctypes.create_string_buffer(64 * 1024)
        context, size = wintypes.ULONG(0), wintypes.ULONG(0)
        while True:
            status = ntdll.NtQueryDirectoryObject(handle, buf, len(buf), True, not out, ctypes.byref(context),
                                                  ctypes.byref(size))
            if status != 0:
                assert status & 0xFFFFFFFF == 0x8000001A, f"NtQueryDirectoryObject: 0x{status & 0xFFFFFFFF:08X}"
                return out                                                  # STATUS_NO_MORE_ENTRIES
            info = _OBJECT_DIRECTORY_INFORMATION.from_buffer(buf)
            out.add(ctypes.wstring_at(info.Name.Buffer, info.Name.Length // 2).lower())
    finally:
        ntdll.NtClose(handle)


def test_no_drop_in_name_is_one_windows_always_loads_from_system32():
    key, directory = _known_dlls_key(), _known_dlls_directory()
    assert "kernel32.dll" in key and {"kernel32.dll", "kernelbase.dll"} <= directory     # each readout reads it
    assert not g.PROXY_DLLS & (key | directory)
    assert all(n == n.lower() and n.endswith(".dll") for n in g.PROXY_DLLS)              # looked up lower-cased


# ---------------------------------------------------------------------------------------------- drop-ins --

def test_drop_ins_are_the_dlls_plugins_and_settings_a_fan_fix_adds_and_not_the_games_own(tmp_path):
    game = tmp_path / "Some Game"
    exe = put(game, "Game.exe", b"MZ")
    wanted = ["DXGI.dll", "dinput8.dll", "dxgi.ini", "global.ini", "dxvk.conf", "UE4SS-settings.ini", "FrameFix.asi",
              "FrameFix.ini", "scripts/Widescreen.asi", "scripts/Widescreen.ini", "scripts/global.ini",
              "plugins/sub/Deep.asi", "update/Hud.asi"]
    shipped = ["steam_api64.dll", "d3dcompiler_47.dll", "binkw64.dll", "version.ini", "settings.ini", "readme.txt",
               "scripts/dxgi.dll", "scripts/a/b/TooDeep.asi", "mods/Other.asi", "Mods/mods.txt"]
    for rel in wanted + shipped:
        put(game, rel, rel.encode())
    (game / "folder.asi").mkdir()                                     # a folder named like a plugin is no plugin
    out = g.drop_ins(str(exe), g.Fingerprints())
    assert out["folder"] == str(game) and out["more"] == 0
    assert names(out["files"]) == sorted((w.replace("/", "\\") for w in wanted), key=str.lower)
    for f in out["files"]:
        data = (game / f["name"]).read_bytes()
        assert f["path"] == str(game / f["name"]) and f["bytes"] == len(data) and f["why"] is None
        assert f["sha256"] == hashlib.sha256(data).hexdigest()


def test_ue4ss_mods_list_counts_only_beside_its_proxy(tmp_path):
    game = tmp_path / "Game"
    exe = put(game, "Game.exe")
    put(game, "Mods/mods.txt", b"FastStart : 1\n")
    assert names(g.drop_ins(str(exe))["files"]) == []
    put(game, "dwmapi.dll")
    assert names(g.drop_ins(str(exe))["files"]) == ["dwmapi.dll", "Mods\\mods.txt"]


def test_a_missing_folder_holds_nothing_and_one_that_cannot_be_listed_is_an_error_with_its_reason(tmp_path,
                                                                                                   monkeypatch):
    assert g.drop_ins(str(tmp_path / "gone" / "Game.exe"))["files"] == []
    game = tmp_path / "Game"
    exe = put(game, "Game.exe")
    (game / "scripts").mkdir()
    real = os.scandir

    def scandir(path):
        if os.path.normcase(os.fspath(path)) == os.path.normcase(str(game / "scripts")):
            raise PermissionError(13, "Access is denied")
        return real(path)
    monkeypatch.setattr(g.os, "scandir", scandir)
    with pytest.raises(OSError, match=r"scripts could not be read: Access is denied$"):
        g.drop_ins(str(exe))


def test_a_folder_holding_too_many_entries_is_not_judged(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "MAX_ENTRIES", 3)
    exe = put(tmp_path, "Game.exe")
    for i in range(3):
        put(tmp_path, f"f{i}.txt")
    with pytest.raises(OSError, match="holds more than 3 entries"):
        g.drop_ins(str(exe))


def test_past_the_listing_limit_the_rest_are_counted(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "MAX_FILES", 2)
    exe = put(tmp_path, "Game.exe")
    for n in ("a.asi", "b.asi", "c.asi"):
        put(tmp_path, n)
    out = g.drop_ins(str(exe))
    assert len(out["files"]) == 2 and out["more"] == 1


# ---------------------------------------------------------------------------------------- fingerprints --

def test_a_file_is_known_by_its_sha256_size_and_date(tmp_path):
    p = put(tmp_path, "dxgi.dll", b"fan fix" * 1000)
    os.utime(p, (1_790_000_000, 1_790_000_000))
    out = g.Fingerprints().entry(str(p), "dxgi.dll", os.stat(p))
    assert {k: out[k] for k in ("path", "name", "bytes", "sha256", "why")} == {
        "path": str(p), "name": "dxgi.dll", "bytes": 7000, "sha256": hashlib.sha256(b"fan fix" * 1000).hexdigest(),
        "why": None}
    when = datetime.fromisoformat(out["modified"])
    assert when.timestamp() == 1_790_000_000 and when.utcoffset() is not None     # with its time zone


def test_a_file_past_the_size_limit_or_the_budget_is_compared_by_size_and_date(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "MAX_HASH_BYTES", 200)
    big, small = put(tmp_path, "big.pak", b"b" * 300), put(tmp_path, "a.dll", b"a" * 100)
    prints = g.Fingerprints(budget=150)
    assert prints.entry(str(big), "big.pak", os.stat(big))["why"] == g.WHY_LARGE
    assert prints.entry(str(small), "a.dll", os.stat(small))["sha256"]            # 100 of the 150 spent
    second = prints.entry(str(small), "a.dll", os.stat(small))
    assert second["sha256"] is None and second["why"] == g.WHY_BUDGET and second["bytes"] == 100


def test_hashing_stops_at_a_deadline_taken_when_the_check_starts(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "CHUNK", 1024)
    p = put(tmp_path, "a.dll", b"a" * 3 * 1024)
    clock = Clock()
    prints = g.Fingerprints(clock=clock, deadline_s=1.0)
    clock.t += 5.0                                    # time spent before the first file counts against it
    monkeypatch.setattr(g.Fingerprints, "_hash", lambda *a: pytest.fail("a file was read past the deadline"))
    out = prints.entry(str(p), "a.dll", os.stat(p))
    assert out["sha256"] is None and out["why"] == g.WHY_TIME


def test_a_file_whose_read_runs_past_the_deadline_has_no_fingerprint(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "CHUNK", 1024)
    p = put(tmp_path, "a.dll", b"a" * 3 * 1024)
    prints = g.Fingerprints(clock=Clock(step=0.4), deadline_s=1.0)      # each chunk costs 0.4 s of a 1 s budget
    out = prints.entry(str(p), "a.dll", os.stat(p))
    assert out["sha256"] is None and out["why"] == g.WHY_TIME
    assert g.Fingerprints(clock=Clock(step=0.0)).entry(str(p), "a.dll", os.stat(p))["sha256"]   # in time: hashed


@pytest.mark.parametrize("bit", [g.FILE_ATTRIBUTE_OFFLINE, g.FILE_ATTRIBUTE_RECALL_ON_OPEN,
                                 g.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS])
def test_a_file_kept_online_only_is_never_opened(tmp_path, monkeypatch, bit):
    p = put(tmp_path, "GameUserSettings.ini", b"[x]")
    st = os.stat(p)
    online = types.SimpleNamespace(st_size=st.st_size, st_mtime=st.st_mtime, st_mode=st.st_mode,
                                   st_file_attributes=st.st_file_attributes | bit)
    monkeypatch.setattr(g.Fingerprints, "_hash", lambda *a: pytest.fail("an online-only file was opened"))
    out = g.Fingerprints().entry(str(p), p.name, online)
    assert out["sha256"] is None and out["why"] == g.WHY_ONLINE and out["bytes"] == 3


def test_the_listing_carries_the_online_only_mark_so_the_file_is_not_opened(tmp_path, monkeypatch):
    exe = put(tmp_path, "Game.exe")
    dll = put(tmp_path, "dxgi.dll")
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.SetFileAttributesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    k32.SetFileAttributesW.restype = wintypes.BOOL
    assert k32.SetFileAttributesW(str(dll), g.FILE_ATTRIBUTE_OFFLINE | 0x20), ctypes.get_last_error()   # ARCHIVE
    monkeypatch.setattr(g.Fingerprints, "_hash", lambda *a: pytest.fail("an online-only file was opened"))
    [f] = g.drop_ins(str(exe))["files"]
    assert f["name"] == "dxgi.dll" and f["why"] == g.WHY_ONLINE


@pytest.mark.parametrize("after", [b"1234567", b"12"])
def test_a_file_that_changes_size_while_it_is_read_has_no_fingerprint(tmp_path, after):
    p = put(tmp_path, "a.ini", b"12345")
    st = os.stat(p)
    p.write_bytes(after)                                # since its listing
    out = g.Fingerprints().entry(str(p), "a.ini", st)
    assert out["sha256"] is None and out["why"] == "it changed while it was read" and out["bytes"] == 5


# ------------------------------------------------------------------------------------------------ Unreal --

def unreal_game(root: Path, project: str = "DemoRacer", install: str = "Demo Racer", paks: bool = True,
                engine: bool = False, platform: str = "Win64") -> tuple[Path, Path]:
    install_dir = root / "Games" / install
    exe = put(install_dir, f"{project}/Binaries/{platform}/{project}-{platform}-Shipping.exe", b"MZ")
    if paks:
        (install_dir / project / "Content" / "Paks").mkdir(parents=True)
    if engine:
        (install_dir / "Engine" / "Binaries").mkdir(parents=True)
    return install_dir, exe


def test_an_unreal_game_is_known_by_where_its_exe_sits(tmp_path):
    install, exe = unreal_game(tmp_path / "a")
    assert g.unreal_layout(str(exe)) == {"project": "DemoRacer", "project_dir": str(install / "DemoRacer"),
                                         "install": str(install)}
    assert g.unreal_layout(str(unreal_game(tmp_path / "b", paks=False, engine=True)[1]))["project"] == "DemoRacer"
    assert g.unreal_layout(str(unreal_game(tmp_path / "c", platform="WinGDK")[1]))["project"] == "DemoRacer"
    assert g.unreal_layout(str(unreal_game(tmp_path / "d", paks=False)[1])) is None         # no pak or engine
    assert g.unreal_layout(str(unreal_game(tmp_path / "e", platform="Win32")[1])) is None
    other = put(tmp_path / "f", "Proj/Bin/Win64/x.exe")
    (tmp_path / "f" / "Proj" / "Content" / "Paks").mkdir(parents=True)
    assert g.unreal_layout(str(other)) is None
    out = g.unreal(str(other), user_folders=lambda: pytest.fail("the user's folders were asked for"))
    assert out == {"found": False, "project": None, "looked_in": [], "config": [], "config_more": 0, "mods": [],
                   "mods_more": 0}


def test_an_unreal_games_user_config_is_read_from_appdata_my_games_and_its_own_folder(tmp_path):
    install, exe = unreal_game(tmp_path, project="End", install="SOME GAME")
    lad, docs = tmp_path / "LocalAppData", tmp_path / "Documents"
    wanted = [put(lad, "End/Saved/Config/WindowsNoEditor/Engine.ini", b"[SystemSettings]\nr.Tweak=1\n"),
              put(lad, "SOME GAME/Saved/Config/WinGDK/Input.ini"),
              put(docs, "My Games/SOME GAME/Saved/Config/WindowsNoEditor/GameUserSettings.ini"),
              put(install, "End/Saved/Config/Windows/Game.ini")]
    for rel in ("End/Saved/Config/CrashReportClient/UECC-Windows.ini", "End/Saved/Config/WindowsNoEditor/notes.txt",
                "End/Saved/Logs/End.log", "Other Game/Saved/Config/Windows/Engine.ini"):
        put(lad, rel)
    out = g.unreal(str(exe), g.Fingerprints(), user_folders=lambda: (str(lad), str(docs)))
    assert out["found"] is True and out["project"] == "End" and out["config_more"] == 0
    assert out["looked_in"] == [str(lad / "End"), str(lad / "SOME GAME"), str(docs / "My Games" / "End"),
                                str(docs / "My Games" / "SOME GAME"), str(install / "End")]
    assert sorted(f["path"] for f in out["config"]) == sorted(str(p) for p in wanted)
    engine = next(f for f in out["config"] if f["name"] == "Engine.ini")
    assert engine["sha256"] == hashlib.sha256(b"[SystemSettings]\nr.Tweak=1\n").hexdigest() and engine["bytes"] == 27


def test_a_project_named_like_its_install_folder_is_looked_for_once(tmp_path):
    install, exe = unreal_game(tmp_path, project="Racer", install="racer")
    lad, docs = tmp_path / "L", tmp_path / "D"
    out = g.unreal(str(exe), user_folders=lambda: (str(lad), str(docs)))
    assert out["looked_in"] == [str(lad / "Racer"), str(docs / "My Games" / "Racer"), str(install / "Racer")]


def test_an_unreal_games_mod_paks_are_listed_from_its_mods_folder(tmp_path):
    install, exe = unreal_game(tmp_path)
    paks = install / "DemoRacer" / "Content" / "Paks"
    wanted = ["~mods/Faster_P.pak", "~mods/Faster_P.utoc", "~mods/Faster_P.ucas", "~mods/pack/Sky_P.pak",
              "~mods/a/b/Deep_P.pak"]
    for rel in wanted + ["pakchunk0-Windows.pak", "~mods/readme.txt", "~mods/a/b/c/TooDeep_P.pak"]:
        put(paks, rel)
    out = g.unreal(str(exe), user_folders=lambda: (str(tmp_path / "L"), str(tmp_path / "D")))
    assert names(out["mods"]) == sorted((w.replace("/", "\\") for w in wanted), key=str.lower)
    assert out["config"] == [] and out["mods_more"] == 0


def test_without_the_users_folders_the_unreal_check_is_an_error_with_its_reason(tmp_path):
    _, exe = unreal_game(tmp_path)

    def nowhere():
        raise OSError(0, "SHGetKnownFolderPath failed")
    with pytest.raises(OSError, match="Windows did not say where this account's AppData and Documents folders are"):
        g.unreal(str(exe), user_folders=nowhere)


# ------------------------------------------------------------------------------------- the user's folders --

def test_the_users_folders_are_where_windows_and_dotnet_say():
    assert os.path.normcase(folders.local_app_data()) == os.path.normcase(os.environ["LOCALAPPDATA"])
    dotnet = subprocess.run([str(POWERSHELL), "-NoProfile", "-NonInteractive", "-Command",
                             "[Environment]::GetFolderPath('MyDocuments')"], capture_output=True, text=True,
                            timeout=60)
    assert dotnet.returncode == 0 and dotnet.stdout.strip(), dotnet.stderr
    assert os.path.normcase(folders.documents()) == os.path.normcase(dotnet.stdout.strip())
    with pytest.raises(OSError, match="SHGetKnownFolderPath"):
        folders.known_folder("{00000000-0000-0000-0000-000000000001}")
