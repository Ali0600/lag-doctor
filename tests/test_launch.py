"""Play's store launch: winapi/launch.py (Explorer opens what Play starts) and lagdoctor/stores/launch.py (which links
it may open, built how).

The vtable slots, IIDs and constants are read back from the Windows SDK's own C headers. One live case has Explorer
start a windowless helper program and checks that Explorer, not this process, became its parent: that the six calls
of the chain reach the slots they name, and that Play would leave no store client in lag-doctor's process tree.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from lagdoctor.capture.target import descendants
from lagdoctor.stores import launch as store_launch
from lagdoctor.winapi import launch as shell
from lagdoctor.winapi import ntquery

BASE_PYTHON = getattr(sys, "_base_executable", sys.executable)
KITS = Path(os.environ.get("ProgramFiles(x86)") or "C:\\Program Files (x86)") / "Windows Kits" / "10" / "Include"
HEADERS = {"IShellWindows": "ExDisp.h", "IServiceProvider": "servprov.h", "IShellBrowser": "ShObjIdl_core.h",
           "IShellView": "ShObjIdl_core.h", "IShellFolderViewDual": "ShlDisp.h", "IShellDispatch2": "ShlDisp.h",
           "IDispatch": "OAIdl.h"}


def sdk_um() -> Path:
    """The newest Windows SDK's um folder that holds the shell headers."""
    def version(p: Path) -> tuple[int, ...]:
        return tuple(int(x) for x in p.name.split(".") if x.isdigit())

    found = sorted((p for p in KITS.glob("10.*") if (p / "um" / "ShlDisp.h").is_file()), key=version)
    if not found:
        pytest.fail(f"no Windows SDK headers under {KITS}: install the SDK, or declare windows_sdk absent")
    return found[-1] / "um"


def header(name: str) -> str:
    return (sdk_um() / name).read_text(encoding="latin-1")


def vtable(iface: str) -> list[str]:
    """The method names of an interface's C vtable struct, in slot order."""
    m = re.search(r"typedef struct " + iface + r"Vtbl\s*\{\s*BEGIN_INTERFACE(.*?)END_INTERFACE",
                  header(HEADERS[iface]), re.S)
    assert m, f"{iface}Vtbl is not in {HEADERS[iface]}"
    return re.findall(r"\(\s*STDMETHODCALLTYPE\s*\*\s*(\w+)\s*\)", m.group(1))


def declared_iid(iface: str) -> str:
    m = re.search(r'MIDL_INTERFACE\("([0-9A-Fa-f-]{36})"\)\s*' + iface + r"\s*:", header(HEADERS[iface]))
    assert m, f"{iface}'s IID is not in {HEADERS[iface]}"
    return m.group(1).upper()


# ---------------------------------------------------------------- the SDK's own answer ---

@pytest.mark.needs("windows_sdk")
def test_every_slot_is_the_sdk_headers_own():
    checked = 0
    for iface, methods in shell.SLOTS.items():
        names = vtable(iface)
        assert names[:3] == ["QueryInterface", "AddRef", "Release"], iface
        for method, slot in methods.items():
            assert names.index(method) == slot, f"{iface}::{method} is slot {names.index(method)}, not {slot}"
            checked += 1
    assert checked == 6


@pytest.mark.needs("windows_sdk")
def test_every_iid_clsid_and_constant_is_the_sdk_headers_own():
    for iface, text in shell.IIDS.items():
        assert declared_iid(iface) == text.upper(), iface
    assert set(shell.IIDS) == set(shell.SLOTS) | {"IDispatch"}
    m = re.search(r'class DECLSPEC_UUID\("([0-9A-Fa-f-]{36})"\)\s*ShellWindows\b', header("ExDisp.h"))
    assert m and m.group(1).upper() == shell.CLSID_SHELL_WINDOWS
    m = re.search(r"DEFINE_GUID\(SID_STopLevelBrowser,\s*([^)]*)\)", header("ShlGuid.h"))
    parts = [int(x.rstrip("L"), 16) for x in m.group(1).split(",")]
    sid = uuid.UUID(fields=(parts[0], parts[1], parts[2], parts[3], parts[4],
                            int.from_bytes(bytes(parts[5:]), "big")))
    assert str(sid).upper() == shell.SID_TOP_LEVEL_BROWSER
    for name, value, hdr in [("SWC_DESKTOP", shell.SWC_DESKTOP, "ExDisp.h"),
                             ("SWFO_NEEDDISPATCH", shell.SWFO_NEEDDISPATCH, "ExDisp.h"),
                             ("SVGIO_BACKGROUND", shell.SVGIO_BACKGROUND, "ShObjIdl_core.h")]:
        m = re.search(r"\b" + name + r"\s*=\s*(0x[0-9A-Fa-f]+|\d+)", header(hdr))
        assert m and int(m.group(1), 0) == value, name
    m = re.search(r"#define\s+CSIDL_DESKTOP\s+(0x[0-9A-Fa-f]+|\d+)", header("ShlObj_core.h"))
    assert m and int(m.group(1), 0) == shell.CSIDL_DESKTOP


def test_a_variant_has_the_size_the_abi_gives_it():
    assert shell.ctypes.sizeof(shell.VARIANT) == (24 if sys.maxsize > 2 ** 32 else 16)


# ---------------------------------------------------------------- Explorer itself ---

HELPER = (
    "import json, os, sys, time\n"
    "json.dump({'pid': os.getpid(), 'ppid': os.getppid()}, open(sys.argv[1], 'w'))\n"
    "t = time.time()\n"
    "while time.time() - t < 30 and not os.path.exists(sys.argv[2]):\n"
    "    time.sleep(0.1)\n")


def test_explorer_starts_what_it_is_handed_as_its_own_child(tmp_path):
    pythonw = Path(os.path.realpath(BASE_PYTHON)).with_name("pythonw.exe")
    assert pythonw.is_file(), pythonw
    helper, out, done = tmp_path / "helper.py", tmp_path / "out.json", tmp_path / "done"
    helper.write_text(HELPER, encoding="utf-8")
    started = shell.open_through_explorer(str(pythonw), subprocess.list2cmdline(["-I", str(helper), str(out),
                                                                              str(done)]))
    try:
        deadline = time.perf_counter() + 20
        while not out.exists() and time.perf_counter() < deadline:
            time.sleep(0.05)
        assert out.exists(), "the helper Explorer was handed never wrote its file"
        time.sleep(0.05)
        seen = json.loads(out.read_text(encoding="utf-8"))
        procs = ntquery.Snapshotter().snapshot()
        me = procs[os.getpid()]
        assert seen["ppid"] == started["explorer_pid"], (seen, started)
        assert procs[started["explorer_pid"]].name.lower() == "explorer.exe"
        assert seen["pid"] in procs and procs[seen["pid"]].name.lower() == "pythonw.exe"
        assert seen["pid"] not in descendants(procs, me.pid, me.create_time)
        assert isinstance(started["foreground_allowed"], bool)
    finally:
        done.write_text("done", encoding="utf-8")


@pytest.mark.parametrize("target, args", [("", ""), ("   ", ""), ("a\0b", ""), ("x", "a\0b"), (3, ""), ("x", None),
                                          ("x" * (shell.MAX_TARGET_CHARS + 1), "")])
def test_open_through_explorer_refuses_what_it_does_not_take(target, args, monkeypatch):
    monkeypatch.setattr(shell, "_desktop", lambda held: pytest.fail("Explorer was reached"))
    with pytest.raises(ValueError):
        shell.open_through_explorer(target, args)


@pytest.mark.parametrize("show", [-1, 12, True, 1.0])
def test_open_through_explorer_refuses_a_show_command_out_of_range(show, monkeypatch):
    monkeypatch.setattr(shell, "_desktop", lambda held: pytest.fail("Explorer was reached"))
    with pytest.raises(ValueError):
        shell.open_through_explorer("x", "", show)


def test_a_desktop_that_cannot_be_found_is_an_error_and_releases_what_it_got(monkeypatch):
    released = []
    real = shell.vcall

    def vcall(this, slot, argtypes, *args, restype=shell.ctypes.c_long):
        if slot == shell.SLOTS["IShellWindows"]["FindWindowSW"] and len(argtypes) == 6:
            return shell.S_FALSE                     # no window of that class: nothing is returned
        return real(this, slot, argtypes, *args, restype=restype)

    real_release = shell._release

    def release(ptr):
        released.append(ptr)
        real_release(ptr)

    monkeypatch.setattr(shell, "vcall", vcall)
    monkeypatch.setattr(shell, "_release", release)
    with pytest.raises(OSError, match="desktop was not found"):
        shell.open_through_explorer("steam://rungameid/1")
    assert len(released) == 1 and released[0]       # the ShellWindows object it made, and nothing was opened


# ---------------------------------------------------------------- the links ---

@pytest.mark.parametrize("appid", ["1", "220", "4294967295"])
def test_steam_link_is_the_fixed_template(appid):
    link = store_launch.steam_link(appid)
    assert link == f"steam://rungameid/{appid}" and store_launch.store_of(link) == "steam"


@pytest.mark.parametrize("bad", ["0", "01", "4294967296", "99999999999", "", " 1", "1 ", "1 -applaunch 2", "1/",
                                 "1/../2", "1?x", "\u0661\u0662", "1e3", "+1", "-1", 1, None, b"1"])
def test_steam_link_refuses_anything_but_an_app_id_in_digits(bad):
    with pytest.raises(ValueError, match="not a Steam app id"):
        store_launch.steam_link(bad)


def test_epic_link_is_the_documented_three_part_form():
    link = store_launch.epic_link("fn", "4fe75bbc5a674f4f9b356b5c90567da5", "Fortnite")
    assert link == ("com.epicgames.launcher://apps/fn%3A4fe75bbc5a674f4f9b356b5c90567da5%3AFortnite"
                    "?action=launch&silent=true")
    assert store_launch.store_of(link) == "epic"
    dotted = store_launch.epic_link("ue", "a" * 32, "UE_5.4")                  # a dot inside an id is unreserved
    assert dotted.endswith("%3AUE_5.4?action=launch&silent=true") and store_launch.store_of(dotted) == "epic"


@pytest.mark.parametrize("bad", ["", "a:b", "a%3Ab", "a?x", "a&b", "a/b", "a b", "a#b", "-a", "a-", "_a", ".a", "a.",
                                 "..", "a~b", "\u00e9", "a" * 129, None, 7])
def test_epic_link_refuses_ids_that_could_change_the_link(bad):
    for args in [(bad, "item", "App"), ("ns", bad, "App"), ("ns", "item", bad)]:
        with pytest.raises(ValueError, match="not an Epic"):
            store_launch.epic_link(*args)


@pytest.mark.parametrize("link", [
    "steam://rungameid/1 -applaunch 2", "steam://rungameid/1/", "steam://run/1", "STEAM://rungameid/1",
    "steam://rungameid/0", "steam://rungameid/4294967296", "steam://rungameid/1\n",
    "com.epicgames.launcher://apps/a%3Ab%3Ac?action=launch&silent=true&x=1",
    "com.epicgames.launcher://apps/a%3Ab?action=launch&silent=true",
    "com.epicgames.launcher://apps/-a%3Ab%3Ac?action=launch&silent=true",
    "https://store.steampowered.com/", "C:\\Games\\game.exe", "", None, 3])
def test_store_of_knows_only_the_links_lag_doctor_builds(link):
    assert store_launch.store_of(link) is None


def test_play_hands_a_built_link_to_explorer_and_nothing_else(monkeypatch):
    calls = []
    monkeypatch.setattr(store_launch.shell, "open_through_explorer",
                        lambda link, *a, **k: calls.append((link, a, k)) or {"explorer_pid": 9, "foreground_allowed": True})
    from lagdoctor.winapi import elevation
    monkeypatch.setattr(elevation, "is_elevated", lambda: False)
    got = store_launch.play(store_launch.steam_link("220"))
    assert got == {"explorer_pid": 9, "foreground_allowed": True, "store": "steam"}
    assert calls == [("steam://rungameid/220", (), {})]       # no arguments, no other way of opening it
    with pytest.raises(ValueError, match="not a store link"):
        store_launch.play("https://example.com/")
    assert len(calls) == 1


def test_play_refuses_when_lag_doctor_runs_elevated(monkeypatch):
    monkeypatch.setattr(store_launch.shell, "open_through_explorer", lambda *a, **k: pytest.fail("opened"))
    from lagdoctor.winapi import elevation
    monkeypatch.setattr(elevation, "is_elevated", lambda: True)
    with pytest.raises(PermissionError, match="administrator rights"):
        store_launch.play(store_launch.steam_link("220"))
