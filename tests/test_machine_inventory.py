"""The inventory: every DESIGN key, its decoding rules, its failure handling, and one live run.

The fake-machine tests replace the winapi readers with fixed answers (this laptop's, from the research) and
point every registry read at the reg_sandbox key, so the values under test are planted, not whatever this PC
has. Win32_DeviceGuard runs as a real child process; the fakes swap PowerShell for a small Python child.
"""
from __future__ import annotations

import collections
import ctypes
import json
import os
import struct
import subprocess
import sys
import time
import winreg
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

import psutil
import pytest

from lagdoctor.capture import gamefiles, inventory
from lagdoctor.fixes.registry import dword, sz
from lagdoctor.winapi import display, dxgi, power, storage

POWERSHELL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" \
    / "powershell.exe"
EXE = r"D:\SteamLibrary\steamapps\common\Some Game\Game.exe"
INTEL = {"luid": "0x00000000_0x00010c49", "name": "Intel(R) UHD Graphics", "vendor": "intel", "vendor_id": 0x8086,
         "vram_mb": 128, "integrated": True, "software": False, "hags": False, "hybrid": "integrated",
         "pci": "00:02.0"}
NVIDIA = {"luid": "0x00000000_0x00010f83", "name": "NVIDIA GeForce RTX 3070 Laptop GPU", "vendor": "nvidia",
          "vendor_id": 0x10DE, "vram_mb": 8018, "integrated": False, "software": False, "hags": True,
          "hybrid": "discrete", "pci": "01:00.0"}
BASIC = {"luid": "0x00000000_0x00010ef4", "name": "Microsoft Basic Render Driver", "vendor": "microsoft",
         "vendor_id": 0x1414, "vram_mb": 0, "integrated": False, "software": True, "hags": False, "hybrid": None,
         "pci": None}
PANEL = {"device": "\\\\.\\DISPLAY1", "name": "Generic PnP Monitor", "adapter_luid": INTEL["luid"],
         "internal": True, "primary": True, "width": 1920, "height": 1080, "hz": 144.003, "max_hz": 144,
         "modes_hz": [60, 144]}
SCHEMES = [("381b4222-f694-41f0-9685-ff5bb260df2e", "Balanced"), ("64a64f24-65b9-4b56-befd-5ec1eaced9b3", "Silent")]
# This laptop's "Silent" plan, read on 2026-11-05: the CPU set exactly as in Balanced.
PLAN = {"max_cpu_ac": 100, "max_cpu_dc": 100, "max_cpu1_ac": 100, "max_cpu1_dc": 100, "boost_ac": 2, "boost_dc": 2,
        "cooling_ac": 1, "cooling_dc": 0}
# What the game's folder held (capture/gamefiles.py, tested on real folders in tests/test_gamefiles.py).
EXE_FILE = (123_456_789, "2026-11-01T10:00:00+00:00")
DROP_INS = {"folder": r"D:\SteamLibrary\steamapps\common\Some Game", "more": 0,
            "files": [{"path": r"D:\SteamLibrary\steamapps\common\Some Game\dxgi.dll", "name": "dxgi.dll",
                       "bytes": 2048, "modified": "2026-11-01T10:00:00+00:00", "sha256": "a" * 64, "why": None}]}
NOT_UNREAL = {"found": False, "project": None, "looked_in": [], "config": [], "config_more": 0, "mods": [],
              "mods_more": 0}


def guard_child(script: str) -> list[str]:
    """A stand-in for the PowerShell query: a Python child running `script`.

    The base interpreter, not the venv's python.exe: that one is a launcher, and killing it would leave the
    real interpreter running with our pipe (docs/learnings.md).
    """
    return [sys._base_executable, "-I", "-c", script]


def guard_answer(running: list[int]) -> list[str]:
    return guard_child(f"print({json.dumps(json.dumps({'vbs': 2, 'running': running}))})")


@pytest.fixture(autouse=True)
def _fresh_device_guard_cache(monkeypatch):
    monkeypatch.setattr(inventory, "_device_guard_cache", None)


@pytest.fixture
def machine(monkeypatch, reg_sandbox):
    """A fake machine with this laptop's answers; tests change single facts on it."""
    state = {"adapters": [INTEL, NVIDIA, BASIC], "displays": [PANEL], "slider_calls": [], "personality_calls": [],
             "personality": "balanced"}
    monkeypatch.setattr(dxgi, "list_adapters", lambda: [dict(a) for a in state["adapters"]])
    monkeypatch.setattr(display, "list_displays", lambda: [dict(d) for d in state["displays"]])
    monkeypatch.setattr(power, "power_status", lambda: {"on_battery": False, "battery_pct": 100, "saver": False})
    monkeypatch.setattr(power, "battery_present", lambda: True)
    monkeypatch.setattr(power, "active_scheme", lambda: SCHEMES[1])
    monkeypatch.setattr(power, "list_schemes", lambda: list(SCHEMES))
    monkeypatch.setattr(power, "plan_settings", lambda guid: state.setdefault("plan_for", []).append(guid) or dict(PLAN))
    monkeypatch.setattr(power, "slider_mode", lambda on_ac: state["slider_calls"].append(on_ac) or "balanced")
    monkeypatch.setattr(power, "scheme_personality", lambda scheme, on_ac: state["personality_calls"].append(
        (scheme, on_ac)) or state["personality"])
    monkeypatch.setattr(power, "effective_mode", lambda timeout_s=1.0: "balanced")
    monkeypatch.setattr(storage, "media_type", lambda path: "SSD")
    monkeypatch.setattr(storage, "free_gb", lambda path: 68.8149)
    monkeypatch.setattr(inventory, "_cpu_sets", lambda: [(0, c, 1) for c in range(0, 12, 2) for _ in (0, 1)]
                        + [(0, c, 0) for c in range(12, 16)])
    monkeypatch.setattr(inventory, "_ram_gb", lambda: 31.6)
    monkeypatch.setattr(inventory, "_device_guard_argv", lambda: guard_answer([2]))
    monkeypatch.setattr(inventory, "_exe_file", lambda exe: state.setdefault("exe_file_for", []).append(exe)
                        or EXE_FILE)
    monkeypatch.setattr(gamefiles, "drop_ins", lambda exe, prints=None: state.setdefault("drop_ins_for", []).append(
        (exe, prints)) or json.loads(json.dumps(DROP_INS)))
    monkeypatch.setattr(gamefiles, "unreal", lambda exe, prints=None, user_folders=None: state.setdefault(
        "unreal_for", []).append((exe, prints)) or dict(NOT_UNREAL))
    reg = reg_sandbox
    reg.set("HKLM", inventory.NT_CURRENT_VERSION, "ProductName", sz("Windows 10 Home"))
    reg.set("HKLM", inventory.CPU_KEY, "ProcessorNameString", sz("12th Gen Intel(R) Core(TM) i7-12650H  "))
    reg.set("HKCU", inventory.GAME_CONFIG_STORE, "GameDVR_Enabled", dword(1))
    state["registry"] = reg
    return state


def collect(machine, target=None, process_names=None):
    return inventory.collect(target, process_names, registry=machine["registry"])


# ======================================================================== shape

DESIGN_KEYS = {
    "os": {"name", "build"},
    "cpu": {"name", "cores", "threads", "hybrid", "p_threads", "e_threads"},
    "power": {"scheme_guid", "scheme_name", "schemes", "slider", "personality", "effective_mode", "on_battery",
              "battery_pct", "saver", "plan"},
    "settings": {"game_mode", "game_dvr", "background_recording", "hags", "windowed_optimizations", "vrr",
                 "memory_integrity"},
    "game": {"exe", "drive", "media", "free_gb", "gpu_preference", "monitor_device", "launcher", "exe_bytes",
             "exe_modified", "drop_ins", "unreal"},
    "oem": {"tools"},
}


def test_collect_returns_every_design_key(machine):
    inv = collect(machine, {"exe": EXE, "monitor_device": PANEL["device"], "adapter_luid": NVIDIA["luid"]},
                  ["explorer.exe"])
    assert set(inv) == {"os", "cpu", "ram_gb", "laptop", "adapters", "displays", "power", "settings", "game", "oem",
                        "network", "nvidia", "errors"}
    for key, fields in DESIGN_KEYS.items():
        assert set(inv[key]) == fields, key
    assert inv["errors"] == {}
    assert inv["network"] is None and inv["nvidia"] is None          # the session fills these
    assert inv["os"] == {"name": "Windows 11 Home", "build": sys.getwindowsversion().build}
    assert inv["cpu"] == {"name": "12th Gen Intel(R) Core(TM) i7-12650H", "cores": 10, "threads": 16,
                          "hybrid": True, "p_threads": 12, "e_threads": 4}
    assert (inv["ram_gb"], inv["laptop"]) == (31.6, True)
    assert inv["power"] == {"scheme_guid": SCHEMES[1][0], "scheme_name": "Silent",
                            "schemes": [list(s) for s in SCHEMES], "slider": "balanced", "personality": "balanced",
                            "effective_mode": "balanced", "on_battery": False, "battery_pct": 100, "saver": False,
                            "plan": PLAN}
    assert machine["slider_calls"] == [True]                         # the slider for the source in use: AC
    assert machine["personality_calls"] == [(SCHEMES[1][0], True)]   # the active plan's type, for that source
    assert machine["plan_for"] == [SCHEMES[1][0]]                    # the active plan's own settings
    assert inv["game"] == {"exe": EXE, "drive": "D:", "media": "SSD", "free_gb": 68.81, "gpu_preference": None,
                           "monitor_device": PANEL["device"], "launcher": "steam", "exe_bytes": EXE_FILE[0],
                           "exe_modified": EXE_FILE[1], "drop_ins": DROP_INS, "unreal": NOT_UNREAL}
    # Both folder reads look at the game's exe and share one budget and deadline.
    assert machine["exe_file_for"] == [EXE]
    assert [x[0] for x in machine["drop_ins_for"] + machine["unreal_for"]] == [EXE, EXE]
    assert machine["drop_ins_for"][0][1] is machine["unreal_for"][0][1] is not None
    assert inv["displays"] == [PANEL]
    json.dumps(inv)                                                  # it must survive the capture file


def test_software_adapters_are_dropped(machine):
    inv = collect(machine)
    assert [a["luid"] for a in inv["adapters"]] == [INTEL["luid"], NVIDIA["luid"]]


def test_without_a_target_the_game_is_unknown_and_oem_not_checked(machine):
    inv = collect(machine)
    assert inv["game"] == dict.fromkeys(DESIGN_KEYS["game"])
    assert inv["oem"] == {"tools": None}                             # not checked, which is not "none found"
    assert inv["settings"]["hags"] is True                           # no game adapter: the discrete one's


def test_a_relative_exe_path_is_reported_not_used(machine):
    inv = collect(machine, {"exe": "Game.exe"})
    assert inv["game"]["exe"] is None and "absolute" in inv["errors"]["game"]
    assert "drop_ins_for" not in machine and inv["game"]["drop_ins"] is None       # no folder is looked at


def test_a_game_folder_that_cannot_be_read_is_reported_and_the_rest_still_arrive(machine, monkeypatch):
    def denied(exe, prints=None):
        raise OSError(r"D:\SteamLibrary\steamapps\common\Some Game could not be read: Access is denied")

    def gone(exe):
        raise OSError(f"{exe} could not be read: The system cannot find the file specified")
    monkeypatch.setattr(gamefiles, "drop_ins", denied)
    monkeypatch.setattr(inventory, "_exe_file", gone)
    inv = collect(machine, {"exe": EXE})
    assert inv["game"]["drop_ins"] is None and inv["game"]["exe_bytes"] is None
    assert inv["errors"]["game.drop_ins"] == ("OSError: " + r"D:\SteamLibrary\steamapps\common\Some Game could not "
                                              "be read: Access is denied")
    assert inv["errors"]["game.exe_file"].endswith("could not be read: The system cannot find the file specified")
    assert inv["game"]["unreal"] == NOT_UNREAL and inv["game"]["launcher"] == "steam"


def test_the_exe_file_is_read_by_size_and_date(tmp_path):
    exe = tmp_path / "Game.exe"
    exe.write_bytes(b"MZ" + bytes(98))
    os.utime(exe, (1_790_000_000, 1_790_000_000))
    size, when = inventory._exe_file(str(exe))
    assert size == 100 and datetime.fromisoformat(when).timestamp() == 1_790_000_000
    with pytest.raises(OSError, match="could not be read"):
        inventory._exe_file(str(tmp_path / "missing.exe"))


def test_a_failing_source_is_reported_and_the_rest_still_arrive(machine, monkeypatch):
    def broken():
        raise OSError("QueryDisplayConfig failed (31)")
    monkeypatch.setattr(display, "list_displays", broken)
    inv = collect(machine)
    assert inv["displays"] is None
    assert "QueryDisplayConfig failed (31)" in inv["errors"]["displays"]
    assert inv["adapters"] and inv["power"]["scheme_name"] == "Silent"


def test_no_effective_mode_answer_is_reported(machine, monkeypatch):
    monkeypatch.setattr(power, "effective_mode", lambda timeout_s=1.0: None)
    inv = collect(machine)
    assert inv["power"]["effective_mode"] is None and "power.effective_mode" in inv["errors"]


def test_on_battery_reads_the_battery_slider_and_unknown_power_reads_none(machine, monkeypatch):
    monkeypatch.setattr(power, "power_status", lambda: {"on_battery": True, "battery_pct": 40, "saver": True})
    collect(machine)
    monkeypatch.setattr(power, "power_status", lambda: {"on_battery": None, "battery_pct": None, "saver": None})
    inv = collect(machine)
    assert machine["slider_calls"] == [False] and inv["power"]["slider"] is None
    assert machine["personality_calls"] == [(SCHEMES[1][0], False)] and inv["power"]["personality"] is None
    assert "power.personality" not in inv["errors"]                  # not asked for, which is not a failure


def test_a_plan_without_the_slider_is_recorded_as_its_type(machine, monkeypatch):
    # A High performance type plan: slider_mode says None, and the type says why, so it is no failure.
    machine["personality"] = "high_performance"
    monkeypatch.setattr(power, "slider_mode", lambda on_ac: None)
    inv = collect(machine)
    assert (inv["power"]["slider"], inv["power"]["personality"]) == (None, "high_performance")
    assert inv["errors"] == {}


def test_without_the_active_plan_its_type_is_not_asked_for(machine, monkeypatch):
    def broken():
        raise OSError("PowerGetActiveScheme failed (5)")
    monkeypatch.setattr(power, "active_scheme", broken)
    inv = collect(machine)
    assert inv["power"]["personality"] is None and machine["personality_calls"] == []
    assert "power.scheme" in inv["errors"] and "power.personality" not in inv["errors"]


def test_an_unread_plan_type_is_reported(machine):
    machine["personality"] = None
    inv = collect(machine)
    assert inv["power"]["personality"] is None and "plan" in inv["errors"]["power.personality"]


# ======================================================================== settings and the game

@pytest.mark.parametrize("game_luid, hags", [
    (INTEL["luid"], False),                         # the game's own adapter wins
    ("LUID_0X00000000_0X00010C49", False),          # PDH spelling of the same LUID
    (NVIDIA["luid"], True),
    (None, True),                                   # unknown: the discrete adapter
    ("0x00000000_0x00099999", True),                # an adapter we do not know: the discrete one
])
def test_hags_is_the_game_adapters_else_a_discrete_one(machine, game_luid, hags):
    assert collect(machine, {"adapter_luid": game_luid})["settings"]["hags"] is hags


def test_hags_unknown_when_the_games_adapter_does_not_say(machine):
    machine["adapters"] = [dict(INTEL, hags=None), NVIDIA]
    assert collect(machine, {"adapter_luid": INTEL["luid"]})["settings"]["hags"] is None


def test_gaming_settings_from_the_registry(machine):
    reg = machine["registry"]
    inv = collect(machine)
    assert inv["settings"] | {"hags": None} == {"game_mode": True, "game_dvr": True, "background_recording": None,
                                                "hags": None, "windowed_optimizations": None, "vrr": None,
                                                "memory_integrity": True}
    reg.set("HKCU", inventory.GAMEBAR, "AutoGameModeEnabled", dword(0))
    reg.set("HKCU", inventory.GAMEDVR, "HistoricalCaptureEnabled", dword(1))
    reg.set("HKCU", inventory.USER_GPU_PREFERENCES, inventory.DX_GLOBAL_SETTINGS,
            sz("SwapEffectUpgradeEnable=1;VRROptimizeEnable=0;"))
    s = collect(machine)["settings"]
    assert (s["game_mode"], s["background_recording"], s["windowed_optimizations"], s["vrr"]) == \
        (False, True, True, False)
    reg.set("HKLM", inventory.GAMEDVR_POLICY, "AllowGameDVR", dword(0))   # policy forbids it: off whatever HKCU says
    assert collect(machine)["settings"]["game_dvr"] is False


def test_a_registry_value_of_the_wrong_type_is_reported(machine):
    machine["registry"].set("HKCU", inventory.GAME_CONFIG_STORE, "GameDVR_Enabled", sz("1"))
    inv = collect(machine)
    assert inv["settings"]["game_dvr"] is None and "settings.game_dvr" in inv["errors"]


@pytest.mark.parametrize("data, preference", [
    ("GpuPreference=2;", 2), ("GpuPreference=1; ", 1),               # a trailing space, as seen here (WT 8)
    ("AppStatus=4; GpuPreference=2;", 2), ("GpuPreference= 0 ;", 0),
    ("AppStatus=4;", None), ("GpuPreference=7;", None), ("GpuPreference=;", None),
])
def test_gpu_preference_of_the_game_from_user_gpu_preferences(machine, data, preference):
    machine["registry"].set("HKCU", inventory.USER_GPU_PREFERENCES, EXE, sz(data))
    assert collect(machine, {"exe": EXE})["game"]["gpu_preference"] == preference


def test_gpu_preference_absent_is_none(machine):
    assert collect(machine, {"exe": EXE})["game"]["gpu_preference"] is None


def test_launchers_from_the_exe_path(tmp_path):
    moved = tmp_path / "Games" / "Fortnite"
    (moved / ".egstore").mkdir(parents=True)
    exe = moved / "FortniteGame" / "Binaries" / "Win64" / "Fortnite.exe"
    assert inventory.launcher_from_path(r"D:\SteamLibrary\steamapps\common\Game\Game.exe") == "steam"
    assert inventory.launcher_from_path(r"C:\Program Files\Epic Games\Game\Game.exe") == "epic"
    assert inventory.launcher_from_path(str(exe)) == "epic"                     # moved Epic install
    assert inventory.launcher_from_path(str(tmp_path / "Other" / "Game.exe")) == "other"


def test_oem_tools_among_process_names(machine):
    names = ["explorer.exe", "armourycrate.service.exe", "ArmouryCrate.Service.exe", "LGHUB_agent.exe",
             "lghub.exe", "lghubber.dll", "iCUE.exe", None, 42]
    assert collect(machine, process_names=names)["oem"]["tools"] == \
        ["armourycrate.service.exe", "iCUE.exe", "lghub.exe", "LGHUB_agent.exe"]
    assert collect(machine, process_names=[])["oem"]["tools"] == []


# ======================================================================== Win32_DeviceGuard

@pytest.mark.parametrize("running, enabled", [([2], True), ([1, 2], True), ([0], False), ([], False)])
def test_memory_integrity_is_service_2_running(machine, monkeypatch, running, enabled):
    monkeypatch.setattr(inventory, "_device_guard_argv", lambda: guard_answer(running))
    assert collect(machine)["settings"]["memory_integrity"] is enabled


def test_a_failed_device_guard_query_is_reported_and_never_cached(machine, monkeypatch):
    monkeypatch.setattr(inventory, "_device_guard_argv", lambda: guard_child("import sys; sys.exit(3)"))
    first = collect(machine)
    assert first["settings"]["memory_integrity"] is None
    assert "exited 3" in first["errors"]["settings.memory_integrity"]
    monkeypatch.setattr(inventory, "_device_guard_argv", lambda: guard_answer([2]))
    assert collect(machine)["settings"]["memory_integrity"] is True


def test_a_device_guard_answer_is_cached_after_success(machine, monkeypatch):
    assert collect(machine)["settings"]["memory_integrity"] is True
    monkeypatch.setattr(inventory, "_device_guard_argv", lambda: guard_child("import sys; sys.exit(3)"))
    again = collect(machine)
    assert again["settings"]["memory_integrity"] is True and again["errors"] == {}


def test_a_hung_device_guard_query_is_stopped_at_its_deadline(machine, monkeypatch, tmp_path):
    pid_file = tmp_path / "child.pid"
    monkeypatch.setattr(inventory, "DEVICE_GUARD_TIMEOUT_S", 0.5)
    monkeypatch.setattr(inventory, "_device_guard_argv", lambda: guard_child(
        f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(5)"))
    start = time.monotonic()
    inv = collect(machine)
    elapsed = time.monotonic() - start
    assert elapsed < 3.0, f"collect took {elapsed:.1f} s; the 0.5 s deadline was not kept"
    assert inv["settings"]["memory_integrity"] is None
    assert "took over 0.5 s" in inv["errors"]["settings.memory_integrity"]
    deadline = time.monotonic() + 2.0
    while pid_file.exists() and int(pid_file.read_text() or 0) in psutil.pids() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert pid_file.exists(), "the stand-in child never started"
    assert int(pid_file.read_text()) not in psutil.pids(), "the hung child was left running"


# ======================================================================== pure helpers

@pytest.mark.parametrize("product, build, name", [
    ("Windows 10 Home", 26200, "Windows 11 Home"), ("Windows 10 Pro", 22000, "Windows 11 Pro"),
    ("Windows 10 Pro", 21999, "Windows 10 Pro"), ("Windows Server 2022 Datacenter", 20348,
                                                  "Windows Server 2022 Datacenter"),
    (None, 26200, None), ("Windows 10 Home", None, "Windows 10 Home"),
])
def test_os_name(product, build, name):
    assert inventory.os_name(product, build) == name


def test_cpu_summary_hybrid_and_not():
    hybrid = [(0, c, 1) for c in range(0, 12, 2) for _ in (0, 1)] + [(0, c, 0) for c in range(12, 16)]
    assert inventory.cpu_summary(hybrid) == {"cores": 10, "threads": 16, "hybrid": True, "p_threads": 12,
                                             "e_threads": 4}
    flat = [(0, c, 0) for c in range(4) for _ in (0, 1)]
    assert inventory.cpu_summary(flat) == {"cores": 4, "threads": 8, "hybrid": False, "p_threads": 8,
                                           "e_threads": 0}
    three = [(0, 0, 2), (0, 1, 1), (0, 2, 0)]                        # P, E and low-power E: only the top is P
    assert inventory.cpu_summary(three)["p_threads"] == 1
    two_groups = [(0, 0, 0), (1, 0, 0)]                             # core 0 of group 0 and of group 1
    assert inventory.cpu_summary(two_groups)["cores"] == 2
    with pytest.raises(ValueError):
        inventory.cpu_summary([])


@pytest.mark.parametrize("text, fields", [
    ("SwapEffectUpgradeEnable=1;VRROptimizeEnable=0;", {"swapeffectupgradeenable": "1", "vrroptimizeenable": "0"}),
    ("GpuPreference=1; ", {"gpupreference": "1"}), (" A = 2 ;;B", {"a": "2"}), ("", {}),
])
def test_kv_fields(text, fields):
    assert inventory.kv_fields(text) == fields


@pytest.mark.parametrize("luid, norm", [
    ("0x00000000_0x00010f83", "0x00000000_0x00010f83"), ("luid_0x00000000_0x00010F83", "0x00000000_0x00010f83"),
    ("0x0_0x10f83", None), ("pid_1_luid_0x00000000_0x00010f83", None), (None, None), (65411, None),
])
def test_norm_luid(luid, norm):
    assert inventory.norm_luid(luid) == norm


@pytest.mark.parametrize("bad", ['{"vbs": true, "running": [2]}', '{"vbs": 2, "running": 2}',
                                 '{"vbs": 2, "running": [true]}', '{"vbs": 2}', '[2]', 'not json',
                                 '{"vbs": 2, "running": [2], "pad": "' + "x" * 70000 + '"}'])
def test_parse_device_guard_refuses_odd_answers(bad):
    with pytest.raises(ValueError):
        inventory.parse_device_guard(bad)


# ======================================================================== live, read-only

def _threads_by_efficiency_class() -> dict[int, int]:
    """Independent readout: GetLogicalProcessorInformationEx(RelationProcessorCore), LC 1.5."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetLogicalProcessorInformationEx.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
    k32.GetLogicalProcessorInformationEx.restype = wintypes.BOOL
    size = wintypes.DWORD(0)
    k32.GetLogicalProcessorInformationEx(0, None, ctypes.byref(size))
    buf = ctypes.create_string_buffer(size.value)
    assert k32.GetLogicalProcessorInformationEx(0, buf, ctypes.byref(size)), ctypes.get_last_error()
    raw, off, counts = buf.raw[:size.value], 0, collections.Counter()
    while off < len(raw):
        _, entry = struct.unpack_from("<II", raw, off)
        efficiency = raw[off + 9]                                   # PROCESSOR_RELATIONSHIP.EfficiencyClass
        groups = struct.unpack_from("<H", raw, off + 30)[0]         # .GroupCount
        for g in range(groups):
            counts[efficiency] += bin(struct.unpack_from("<Q", raw, off + 32 + 16 * g)[0]).count("1")
        off += entry
    return dict(counts)


def test_live_ram_is_the_figure_psutil_gave():
    assert inventory._ram_gb() == round(psutil.virtual_memory().total / 2**30, 1)


def test_live_collect_agrees_with_independent_readouts():
    inv = inventory.collect({"exe": sys._base_executable})
    assert inv["errors"] == {}
    cim = json.loads(subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-NoLogo", "-Command",
         "ConvertTo-Json -Compress -InputObject @{caption = (Get-CimInstance Win32_OperatingSystem).Caption; "
         "ram = (Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory}"],
        capture_output=True, timeout=60, check=True).stdout)
    assert inv["os"]["name"] == cim["caption"].removeprefix("Microsoft ").strip()
    assert abs(inv["ram_gb"] - cim["ram"] / 2**30) < 0.15
    cpu = inv["cpu"]
    assert (cpu["threads"], cpu["cores"]) == (os.cpu_count(), psutil.cpu_count(logical=False))
    by_class = _threads_by_efficiency_class()
    assert (cpu["p_threads"], cpu["e_threads"]) == (by_class[max(by_class)], cpu["threads"] - by_class[max(by_class)])
    assert cpu["hybrid"] == (len(by_class) > 1)
    assert inv["adapters"] and not any(a["software"] for a in inv["adapters"])
    assert inv["game"]["drive"] == Path(sys._base_executable).drive.upper()
    assert inv["game"]["media"] in ("SSD", "HDD", "Unknown") and inv["game"]["free_gb"] > 0
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios"
                                                       r"\HypervisorEnforcedCodeIntegrity") as k:
            configured = winreg.QueryValueEx(k, "Enabled")[0] == 1
    except FileNotFoundError:
        configured = None
    if configured is not None:
        # The registry says configured, Win32_DeviceGuard says running; they match outside a pending reboot.
        assert inv["settings"]["memory_integrity"] is configured
