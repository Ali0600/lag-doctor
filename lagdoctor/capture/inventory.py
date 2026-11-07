"""The static snapshot taken at the start of a session: Windows, CPU, RAM, adapters, displays, power, gaming
settings, the game's file and the OEM tools running. Keys are DESIGN.md's "Inventory keys"; None means unknown.

Nothing here touches the game process: the game's facts come from the exe path in the target dict, the
registry and the file system. Every section degrades on its own: a section that fails is None and its reason
goes into `errors` (section -> reason), so one broken API never hides the rest.

Registry locations and value meanings: docs/research/windows-telemetry.md (WT) section 8; several meanings
there are community-sourced [unverified], which the comments below repeat where they matter.
"""
from __future__ import annotations

import ctypes
import json
import logging
import ntpath
import os
import re
import subprocess
import sys
import threading
import time
import winreg
from ctypes import wintypes
from typing import Any, Callable, Iterable

import psutil

from lagdoctor.fixes.base import is_absent
from lagdoctor.fixes.registry import RegistryBackend
from lagdoctor.winapi import display, dxgi, power, storage

log = logging.getLogger(__name__)

GAMEBAR = r"Software\Microsoft\GameBar"                                  # AutoGameModeEnabled
GAME_CONFIG_STORE = r"System\GameConfigStore"                            # GameDVR_Enabled
GAMEDVR = r"Software\Microsoft\Windows\CurrentVersion\GameDVR"           # HistoricalCaptureEnabled
GAMEDVR_POLICY = r"SOFTWARE\Policies\Microsoft\Windows\GameDVR"          # AllowGameDVR (HKLM, read only)
USER_GPU_PREFERENCES = r"Software\Microsoft\DirectX\UserGpuPreferences"  # <exe path> = "GpuPreference=N;"
DX_GLOBAL_SETTINGS = "DirectXUserGlobalSettings"                         # "SwapEffectUpgradeEnable=1;VRR…"
CPU_KEY = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
NT_CURRENT_VERSION = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"

# OEM / RGB utilities (WT 10). lghub*.exe is Logitech G HUB's family of processes.
OEM_TOOLS = ("ArmouryCrate.Service.exe", "ArmouryCrate.UserSessionHelper.exe", "LightingService.exe",
             "asus_framework.exe", "iCUE.exe", "RazerCentralService.exe", "SignalRgb.exe")
OEM_PREFIXES = ("lghub",)
MAX_PROCESS_NAMES = 10_000

DEVICE_GUARD_TIMEOUT_S = 10.0   # about 0.7-1.1 s here (WT 8); a cold start under a game's load can be slower
MAX_PS_OUTPUT = 64 * 1024
_DEVICE_GUARD_PS = (
    "$d = Get-CimInstance -Namespace root\\Microsoft\\Windows\\DeviceGuard -ClassName Win32_DeviceGuard "
    "-ErrorAction Stop; ConvertTo-Json -Compress -InputObject @{vbs = [int]$d.VirtualizationBasedSecurityStatus; "
    "running = @($d.SecurityServicesRunning | ForEach-Object { [int]$_ })}")
MEMORY_INTEGRITY_RUNNING = 2    # SecurityServicesRunning contains 2 = "memory integrity is running" (WT 8)
CREATE_NO_WINDOW = 0x08000000

_LUID_RE = re.compile(r"0x[0-9a-f]{8}_0x[0-9a-f]{8}")
_device_guard_cache: dict | None = None      # only a success is cached: it cannot change without a reboot
_device_guard_lock = threading.Lock()


class SYSTEM_CPU_SET_INFORMATION(ctypes.Structure):
    _fields_ = [("Size", wintypes.DWORD), ("Type", wintypes.DWORD), ("Id", wintypes.DWORD),
                ("Group", wintypes.WORD), ("LogicalProcessorIndex", ctypes.c_ubyte), ("CoreIndex", ctypes.c_ubyte),
                ("LastLevelCacheIndex", ctypes.c_ubyte), ("NumaNodeIndex", ctypes.c_ubyte),
                ("EfficiencyClass", ctypes.c_ubyte), ("AllFlags", ctypes.c_ubyte), ("Reserved", wintypes.DWORD),
                ("AllocationTag", ctypes.c_uint64)]


# 32 bytes, EfficiencyClass at +18, AllFlags at +19 (WT 3.7, verify log #40)
assert ctypes.sizeof(SYSTEM_CPU_SET_INFORMATION) == 32
assert SYSTEM_CPU_SET_INFORMATION.EfficiencyClass.offset == 18
assert SYSTEM_CPU_SET_INFORMATION.AllFlags.offset == 19

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.GetSystemCpuSetInformation.argtypes = [ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG),
                                                 wintypes.HANDLE, wintypes.ULONG]
_kernel32.GetSystemCpuSetInformation.restype = wintypes.BOOL
_kernel32.GetSystemDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
_kernel32.GetSystemDirectoryW.restype = wintypes.UINT


# ---------------------------------------------------------------- pure helpers --

def os_name(product: str | None, build: int | None) -> str | None:
    """ProductName still says "Windows 10" on Windows 11 (build 22000 and later), so fix the number."""
    if not product:
        return None
    if build is not None and build >= 22000 and product.startswith("Windows 10"):
        return "Windows 11" + product[len("Windows 10"):]
    return product


def cpu_summary(sets: list[tuple[int, int, int]]) -> dict:
    """(group, core index, efficiency class) per logical processor -> cores, threads and the P/E split.

    A higher EfficiencyClass is faster and less efficient (WT 3.7), so the top class are the P-cores. A CPU
    with one class is not hybrid: all its threads count as P threads.
    """
    if not sets:
        raise ValueError("no CPU sets")
    classes = {eff for _, _, eff in sets}
    top = max(classes)
    p_threads = sum(1 for _, _, eff in sets if eff == top)
    return {"cores": len({(group, core) for group, core, _ in sets}), "threads": len(sets),
            "hybrid": len(classes) > 1, "p_threads": p_threads, "e_threads": len(sets) - p_threads}


def kv_fields(text: str) -> dict[str, str]:
    """'A=1;B=2; ' -> {'a': '1', 'b': '2'}: keys lower-cased, spaces around parts tolerated (WT 8)."""
    out: dict[str, str] = {}
    for part in text.split(";"):
        key, sep, value = part.partition("=")
        key = key.strip().lower()
        if sep and key:
            out[key] = value.strip()
    return out


def gpu_preference_from(text: str) -> int | None:
    """'GpuPreference=2;' -> 2. 0 let Windows decide, 1 power saving, 2 high performance (WT 8)."""
    value = kv_fields(text).get("gpupreference")
    return int(value) if value in ("0", "1", "2") else None


def flag_from(text: str | None) -> bool | None:
    return {"1": True, "0": False}.get(text) if text is not None else None


def dword_flag(value: int | None) -> bool | None:
    return {1: True, 0: False}.get(value) if value is not None else None


def oem_tools(names: Iterable[str]) -> list[str]:
    """The OEM utilities among process names, case-insensitively, each listed once in the spelling first seen."""
    wanted = {n.lower() for n in OEM_TOOLS}
    found: dict[str, str] = {}
    for i, name in enumerate(names):
        if i >= MAX_PROCESS_NAMES:
            break
        if not isinstance(name, str):
            continue
        low = name.lower()
        if low in wanted or (low.endswith(".exe") and low.startswith(OEM_PREFIXES)):
            found.setdefault(low, name)
    return sorted(found.values(), key=str.lower)


def launcher_from_path(exe: str, has_egstore: Callable[[str], bool] | None = None) -> str:
    """'steam' for a Steam library, 'epic' for an Epic install, else 'other'.

    Steam always installs under <library>\\steamapps\\common. Epic's default is ...\\Epic Games\\..., and every
    Epic install keeps a .egstore folder in its root, which is how a moved install is still recognised.
    """
    low = exe.lower().replace("/", "\\")
    if "\\steamapps\\" in low:
        return "steam"
    if "\\epic games\\" in low or (has_egstore or _has_egstore)(exe):
        return "epic"
    return "other"


def _has_egstore(exe: str, levels: int = 5) -> bool:
    folder = ntpath.dirname(exe)
    for _ in range(levels):
        if os.path.isdir(ntpath.join(folder, ".egstore")):
            return True
        parent = ntpath.dirname(folder)
        if parent == folder:
            break
        folder = parent
    return False


def norm_luid(luid: Any) -> str | None:
    """Accept DESIGN's '0x…_0x…' in any case, or a PDH-style 'luid_0x…_0x…'."""
    if not isinstance(luid, str):
        return None
    text = luid.strip().lower()
    text = text[len("luid_"):] if text.startswith("luid_") else text
    return text if _LUID_RE.fullmatch(text) else None


def hags_for(adapters: list[dict] | None, game_luid: Any) -> bool | None:
    """HAGS on the game's adapter; when that adapter is not known, on the first discrete adapter that answers."""
    if not adapters:
        return None
    want = norm_luid(game_luid)
    if want:
        for a in adapters:
            if a.get("luid") == want:
                return a.get("hags")
    for a in adapters:
        if (a.get("hybrid") == "discrete" or a.get("integrated") is False) and a.get("hags") is not None:
            return a["hags"]
    return None


def parse_device_guard(text: str) -> dict:
    if len(text) > MAX_PS_OUTPUT:
        raise ValueError(f"PowerShell printed {len(text)} characters; expected a short JSON line")
    data = json.loads(text)
    vbs, running = (data.get("vbs"), data.get("running")) if isinstance(data, dict) else (None, None)
    if isinstance(vbs, bool) or not isinstance(vbs, int) or not isinstance(running, list) \
            or not all(isinstance(x, int) and not isinstance(x, bool) for x in running):
        raise ValueError(f"unexpected Win32_DeviceGuard answer: {text[:200]!r}")
    return {"vbs": vbs, "running": running}


# ---------------------------------------------------------------- readers -------

def _section(errors: dict[str, str], key: str, fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - one failing source must not hide the others; it is reported
        errors[key] = f"{type(exc).__name__}: {exc}"
        log.warning("inventory: %s could not be read: %s", key, errors[key])
        return None


def _reg_value(reg: RegistryBackend, hive: str, path: str, name: str, kind: int) -> Any:
    """The value's data if it exists with the expected type; None if absent; ValueError if another type."""
    value = reg.get(hive, path, name)
    if is_absent(value):
        return None
    if value.get("type") != kind:
        raise ValueError(f"{hive}\\{path}\\{name} has registry type {value.get('type')}, expected {kind}")
    return value["data"]


def _os(reg: RegistryBackend) -> dict:
    build = sys.getwindowsversion().build
    product = _reg_value(reg, "HKLM", NT_CURRENT_VERSION, "ProductName", winreg.REG_SZ)
    return {"name": os_name(product, build), "build": build}


def _cpu_sets() -> list[tuple[int, int, int]]:
    need = wintypes.ULONG(0)
    _kernel32.GetSystemCpuSetInformation(None, 0, ctypes.byref(need), None, 0)   # size probe (fails with 122)
    if not 0 < need.value <= 1 << 20:
        raise OSError(f"GetSystemCpuSetInformation size probe gave {need.value} bytes")
    buf = (ctypes.c_ubyte * need.value)()
    if not _kernel32.GetSystemCpuSetInformation(buf, need.value, ctypes.byref(need), None, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    entry = ctypes.sizeof(SYSTEM_CPU_SET_INFORMATION)
    out, offset = [], 0
    while offset + entry <= need.value:
        info = SYSTEM_CPU_SET_INFORMATION.from_buffer(buf, offset)
        if info.Size < entry:
            raise OSError(f"CPU set entry at {offset} claims {info.Size} bytes")
        if info.Type == 0:                        # CpuSetInformation
            out.append((info.Group, info.CoreIndex, info.EfficiencyClass))
        offset += info.Size
    return out


def _cpu(reg: RegistryBackend) -> dict:
    name = _reg_value(reg, "HKLM", CPU_KEY, "ProcessorNameString", winreg.REG_SZ)
    return {"name": name.strip() if name else None, **cpu_summary(_cpu_sets())}


def _ram_gb() -> float:
    return round(psutil.virtual_memory().total / 2**30, 1)


def _power(errors: dict[str, str]) -> dict:
    status = _section(errors, "power.status", power.power_status) or {}
    scheme = _section(errors, "power.scheme", power.active_scheme)
    schemes = _section(errors, "power.schemes", power.list_schemes)
    on_battery = status.get("on_battery")
    slider = personality = None
    if on_battery is not None:
        slider = _section(errors, "power.slider", lambda: power.slider_mode(not on_battery))
        # The plan's type says whether it has the slider at all, so a missing slider reads as "no power mode on this
        # plan", never as "not read".
        if scheme:
            personality = _section(errors, "power.personality",
                                   lambda: power.scheme_personality(scheme[0], not on_battery))
            if personality is None and "power.personality" not in errors:
                errors["power.personality"] = "Windows did not say which type of plan the active power plan is"
    effective = _section(errors, "power.effective_mode", power.effective_mode)
    if effective is None and "power.effective_mode" not in errors:
        errors["power.effective_mode"] = "Windows sent no effective power mode within the timeout"
    # What the active plan really sets, so a plan is judged by its settings, never by its name alone.
    plan = _section(errors, "power.plan", lambda: power.plan_settings(scheme[0])) if scheme else None
    return {"scheme_guid": scheme[0] if scheme else None, "scheme_name": scheme[1] if scheme else None,
            "schemes": [[g, n] for g, n in schemes] if schemes is not None else None, "slider": slider,
            "personality": personality, "effective_mode": effective, "on_battery": on_battery,
            "battery_pct": status.get("battery_pct"), "saver": status.get("saver"), "plan": plan}


def _game_dvr(reg: RegistryBackend) -> bool | None:
    # The policy wins when it forbids recording (WT 8: 0 = not allowed).
    if _reg_value(reg, "HKLM", GAMEDVR_POLICY, "AllowGameDVR", winreg.REG_DWORD) == 0:
        return False
    return dword_flag(_reg_value(reg, "HKCU", GAME_CONFIG_STORE, "GameDVR_Enabled", winreg.REG_DWORD))


def _game_mode(reg: RegistryBackend) -> bool | None:
    value = _reg_value(reg, "HKCU", GAMEBAR, "AutoGameModeEnabled", winreg.REG_DWORD)
    # Absent means the default, on [unverified, WT 8]; the effective power mode (game_mode) confirms it live.
    return True if value is None else dword_flag(value)


def _dx_global(reg: RegistryBackend) -> dict[str, str]:
    text = _reg_value(reg, "HKCU", USER_GPU_PREFERENCES, DX_GLOBAL_SETTINGS, winreg.REG_SZ)
    return kv_fields(text) if text else {}


def _settings(errors: dict[str, str], reg: RegistryBackend, adapters: list[dict] | None, target: dict | None,
              device_guard: Callable[[], dict | None]) -> dict:
    dx = _section(errors, "settings.directx_global", lambda: _dx_global(reg))
    guard = _section(errors, "settings.memory_integrity", device_guard)
    return {
        "game_mode": _section(errors, "settings.game_mode", lambda: _game_mode(reg)),
        "game_dvr": _section(errors, "settings.game_dvr", lambda: _game_dvr(reg)),
        "background_recording": _section(errors, "settings.background_recording", lambda: dword_flag(
            _reg_value(reg, "HKCU", GAMEDVR, "HistoricalCaptureEnabled", winreg.REG_DWORD))),
        "hags": hags_for(adapters, target.get("adapter_luid") if isinstance(target, dict) else None),
        "windowed_optimizations": flag_from(dx.get("swapeffectupgradeenable")) if dx is not None else None,
        "vrr": flag_from(dx.get("vrroptimizeenable")) if dx is not None else None,
        "memory_integrity": MEMORY_INTEGRITY_RUNNING in guard["running"] if guard else None,
    }


def _gpu_preference(reg: RegistryBackend, exe: str) -> int | None:
    # Registry value names are case-insensitive, so the exe path matches however Windows spelled it.
    text = _reg_value(reg, "HKCU", USER_GPU_PREFERENCES, exe, winreg.REG_SZ)
    return gpu_preference_from(text) if text else None


def _game(errors: dict[str, str], reg: RegistryBackend, target: dict | None) -> dict:
    target = target if isinstance(target, dict) else {}
    monitor = target.get("monitor_device")
    game = {"exe": None, "drive": None, "media": None, "free_gb": None, "gpu_preference": None,
            "monitor_device": monitor if isinstance(monitor, str) else None, "launcher": None}
    exe = target.get("exe")
    if exe is None:
        return game
    if not isinstance(exe, str) or not ntpath.isabs(exe) or "\0" in exe:
        errors["game"] = f"the target's exe is not an absolute path: {exe!r}"
        return game
    game["exe"] = exe
    drive = ntpath.splitdrive(exe)[0]
    game["drive"] = drive.upper() if len(drive) == 2 else (drive or None)
    game["media"] = _section(errors, "game.media", lambda: storage.media_type(exe))
    free = _section(errors, "game.free_gb", lambda: storage.free_gb(exe))
    game["free_gb"] = round(free, 2) if free is not None else None
    game["gpu_preference"] = _section(errors, "game.gpu_preference", lambda: _gpu_preference(reg, exe))
    game["launcher"] = _section(errors, "game.launcher", lambda: launcher_from_path(exe))
    return game


# ---------------------------------------------------------------- Win32_DeviceGuard --

def _system32() -> str:
    buf = ctypes.create_unicode_buffer(260)
    n = _kernel32.GetSystemDirectoryW(buf, len(buf))
    if not 0 < n < len(buf):
        raise ctypes.WinError(ctypes.get_last_error())
    return buf.value


def _device_guard_argv() -> list[str]:
    # A full path from GetSystemDirectoryW, so no PATH lookup can pick another powershell.exe.
    exe = ntpath.join(_system32(), "WindowsPowerShell", "v1.0", "powershell.exe")
    return [exe, "-NoProfile", "-NonInteractive", "-NoLogo", "-Command", _DEVICE_GUARD_PS]


class _DeviceGuardProbe:
    """One PowerShell child, started early so its ~1 s overlaps the rest of collect(); bounded by a deadline
    taken at start, so time spent elsewhere in collect() counts against it."""

    def __init__(self, argv: list[str], timeout_s: float) -> None:
        self.deadline = time.monotonic() + timeout_s
        self.timeout_s = timeout_s
        self.reaped = False
        self.proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW)

    def result(self) -> dict:
        try:
            out, err = self.proc.communicate(timeout=max(0.0, self.deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            self.kill()
            raise TimeoutError(f"the Win32_DeviceGuard query took over {self.timeout_s:g} s and was stopped") \
                from None
        self.reaped = True
        if self.proc.returncode != 0:
            tail = err.decode("utf-8", errors="replace").strip()[-300:]
            raise RuntimeError(f"PowerShell exited {self.proc.returncode}: {tail}")
        return parse_device_guard(out.decode("utf-8", errors="replace").strip())

    def kill(self) -> None:
        """Stop and reap the child unless its result was already collected."""
        if self.reaped:
            return
        if self.proc.poll() is None:
            self.proc.kill()
        try:
            self.proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            log.warning("inventory: PowerShell pid %s did not exit after kill", self.proc.pid)
        self.reaped = True


def _start_device_guard(errors: dict[str, str]) -> tuple[Callable[[], dict | None], Callable[[], None]]:
    """Start the query now (or use the cached answer); returns (get the result, clean up)."""
    with _device_guard_lock:
        cached = _device_guard_cache
    if cached is not None:
        return (lambda: cached), (lambda: None)
    probe = _section(errors, "settings.memory_integrity",
                     lambda: _DeviceGuardProbe(_device_guard_argv(), DEVICE_GUARD_TIMEOUT_S))
    if probe is None:
        return (lambda: None), (lambda: None)

    def get() -> dict:
        global _device_guard_cache
        found = probe.result()
        with _device_guard_lock:
            _device_guard_cache = found
        return found

    return get, probe.kill


# ---------------------------------------------------------------- public ---------

def collect(target: dict | None = None, process_names: Iterable[str] | None = None, *,
            registry: RegistryBackend | None = None) -> dict:
    """The inventory dict (DESIGN.md "Inventory keys") plus `errors` {section: reason}.

    `process_names` are the image names of the processes running now, from the session's own snapshot; this
    module never lists processes itself. Without them `oem.tools` is None (not checked), never [].
    `registry` lets tests point every read at a sandbox key. `network` and `nvidia` are left None for the
    session to fill from its collectors.
    """
    reg = registry or RegistryBackend()
    errors: dict[str, str] = {}
    guard, cleanup = _start_device_guard(errors)
    try:
        inv: dict[str, Any] = {
            "os": _section(errors, "os", lambda: _os(reg)),
            "cpu": _section(errors, "cpu", lambda: _cpu(reg)),
            "ram_gb": _section(errors, "ram_gb", _ram_gb),
            "laptop": _section(errors, "laptop", power.battery_present),
        }
        adapters = _section(errors, "adapters", dxgi.list_adapters)
        inv["adapters"] = [a for a in adapters if not a["software"]] if adapters is not None else None
        inv["displays"] = _section(errors, "displays", display.list_displays)
        inv["power"] = _power(errors)
        inv["settings"] = _settings(errors, reg, inv["adapters"], target, guard)
        inv["game"] = _game(errors, reg, target)
        inv["oem"] = {"tools": oem_tools(process_names) if process_names is not None else None}
        inv["network"] = None
        inv["nvidia"] = None
        inv["errors"] = errors
        return inv
    finally:
        cleanup()
