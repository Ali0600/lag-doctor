"""gpu_preference: run a game on the high-performance GPU, plus the helpers for DirectX's "key=value;" strings.

Windows keeps per-app GPU choices in HKCU\\Software\\Microsoft\\DirectX\\UserGpuPreferences: one REG_SZ
value per exe, named by its full path, holding fields such as "GpuPreference=2;". Other fields share
the string ("AppStatus=4;" was seen here), and one real value is "GpuPreference=1; " with a trailing
space (WT 8; verify log #79). So a fix edits only its own field, by splitting on ";" and matching
the stripped key, and keeps every other field and all spacing exactly as found.

A field fix reads, compares and restores that one field: read() returns its value as found, or
ABSENT when the field (or the whole value) is missing. An undo therefore keeps a change someone
made to another field in the meantime, and a later change to our field is a conflict (base.py).
"""
from __future__ import annotations

import ntpath
import re
import winreg
from typing import Any

from lagdoctor.fixes.base import ABSENT, Fix, FixPlan, is_absent
from lagdoctor.fixes.catalog import register
from lagdoctor.fixes.registry import RegistryBackend

HIVE = "HKCU"
GPU_PREFS_PATH = r"Software\Microsoft\DirectX\UserGpuPreferences"     # WT 8
TEXT_TYPES = (winreg.REG_SZ, winreg.REG_EXPAND_SZ)
MAX_TEXT = 64 * 1024              # these strings are a few dozen characters; refuse anything absurd
MAX_FIELD_VALUE = 256
MAX_EXE = 1024


# ---------------------------------------------------------------- fields --

def _is_field(segment: str, key: str) -> bool:
    """True when the segment is "key=..." for this key, ignoring case and the spacing around the key."""
    return "=" in segment and segment.split("=", 1)[0].strip().lower() == key.lower()


def get_field(text: str, key: str) -> str | None:
    """The field's value (spacing around it stripped), or None. ValueError if it appears twice with
    different values, because then nobody can say which one Windows uses."""
    values = {seg.split("=", 1)[1].strip() for seg in text.split(";") if _is_field(seg, key)}
    if len(values) > 1:
        raise ValueError(f"{key} appears more than once with different values in {text!r}")
    return values.pop() if values else None


def set_field(text: str, key: str, value: str) -> str:
    """Set every occurrence of the field in place, or append it; nothing else in the string changes."""
    parts = text.split(";")
    found = False
    for i, seg in enumerate(parts):
        if _is_field(seg, key):
            name, old = seg.split("=", 1)
            if old.strip():
                lead = old[:len(old) - len(old.lstrip())]
                trail = old[len(old.rstrip()):]
                parts[i] = f"{name}={lead}{value}{trail}"
            else:
                parts[i] = f"{name}={old}{value}"
            found = True
    if found:
        return ";".join(parts)
    head, tail = parts[:-1], parts[-1]
    if tail.strip():                  # the last field has no ";" after it: close it before adding ours
        head, tail = parts, ""
    return "".join(p + ";" for p in head) + f"{key}={value};" + tail


def remove_field(text: str, key: str) -> str:
    """Drop every occurrence of the field with its ";"; everything else stays as it was."""
    parts = text.split(";")
    head, tail = parts[:-1], parts[-1]
    kept = [p for p in head if not _is_field(p, key)]
    if _is_field(tail, key):
        tail = ""
    return "".join(p + ";" for p in kept) + tail


def has_fields(text: str) -> bool:
    return any(seg.strip() for seg in text.split(";"))


# --------------------------------------------------------------- registry --

def raw_get(reg: RegistryBackend, hive: str, path: str, name: str) -> Any:
    """One value read straight through winreg, not through `reg.get`: the independent readback.
    It resolves the path the way the backend does, so in tests it reads the sandbox."""
    root, sub = reg._resolve(hive, path)
    try:
        with winreg.OpenKey(root, sub, 0, winreg.KEY_QUERY_VALUE) as key:
            data, typ = winreg.QueryValueEx(key, name)
    except FileNotFoundError:
        return dict(ABSENT)
    return {"type": typ, "data": data}


def _text(raw: dict[str, Any], where: str) -> str:
    if raw.get("type") not in TEXT_TYPES or not isinstance(raw.get("data"), str):
        raise ValueError(f"{where} is not a text value (registry type {raw.get('type')!r}); lag-doctor will "
                         f"not change it")
    if len(raw["data"]) > MAX_TEXT:
        raise ValueError(f"{where} holds {len(raw['data'])} characters; lag-doctor will not change it")
    return raw["data"]


def no_params(params: dict[str, Any]) -> None:
    if not isinstance(params, dict) or params:
        raise ValueError(f"this fix takes no params, got {params!r}")


class StringFieldFix(Fix):
    """One field inside a "key=value;" text value under HKCU (subclasses name the value and the field)."""

    hive = HIVE
    path = GPU_PREFS_PATH
    field_name = ""
    description = ""
    caveat = ""

    def __init__(self, reg: RegistryBackend | None = None) -> None:
        self.reg = reg if reg is not None else RegistryBackend()

    def value_name(self, params: dict[str, Any]) -> str:
        raise NotImplementedError

    def field_value(self, params: dict[str, Any]) -> str:
        raise NotImplementedError

    def _where(self, params: dict[str, Any]) -> str:
        return f"{self.hive}\\{self.path}\\{self.value_name(params)}"

    def target(self, params: dict[str, Any]) -> str:
        return f"{self._where(params)} (field {self.field_name})"

    def _field(self, raw: Any, params: dict[str, Any]) -> Any:
        if is_absent(raw):
            return dict(ABSENT)
        value = get_field(_text(raw, self._where(params)), self.field_name)
        return dict(ABSENT) if value is None else value

    def read(self, params: dict[str, Any]) -> Any:
        return self._field(self.reg.get(self.hive, self.path, self.value_name(params)), params)

    def readback(self, params: dict[str, Any]) -> Any:
        return self._field(raw_get(self.reg, self.hive, self.path, self.value_name(params)), params)

    def wanted(self, params: dict[str, Any], current: Any) -> Any:
        return self.field_value(params)

    def write(self, params: dict[str, Any], value: Any) -> None:
        name = self.value_name(params)
        raw = self.reg.get(self.hive, self.path, name)
        text = "" if is_absent(raw) else _text(raw, self._where(params))
        if is_absent(value):
            if is_absent(raw):
                return
            new = remove_field(text, self.field_name)
            if not has_fields(new):            # nothing but our field was there: the value goes too
                self.reg.set(self.hive, self.path, name, dict(ABSENT))
                return
        else:
            if not isinstance(value, str) or len(value) > MAX_FIELD_VALUE or any(c in value for c in ";\0"):
                raise ValueError(f"not a field value: {value!r}")
            new = set_field(text, self.field_name, value)
        kind = winreg.REG_SZ if is_absent(raw) else raw["type"]
        self.reg.set(self.hive, self.path, name, {"type": kind, "data": new})

    def plan(self, params: dict[str, Any]) -> FixPlan:
        plan = super().plan(params)
        if self.caveat:
            plan.notes.append(self.caveat)
        return plan


# --------------------------------------------------------- gpu_preference --

# Windows paths: a drive or a UNC share, then no characters a file name cannot hold, ending in .exe.
_NAME = r'[^\\/:*?"<>|\x00-\x1f]+'
_EXE_RE = re.compile(rf'(?:[A-Za-z]:\\|\\\\{_NAME}\\{_NAME}\\)(?:{_NAME}\\)*{_NAME}\.exe', re.IGNORECASE)
_PREFERENCES = {0: "let Windows decide", 1: "power saving", 2: "high performance"}


def _gpu_params(params: dict[str, Any]) -> tuple[str, int]:
    if not isinstance(params, dict) or set(params) != {"exe", "preference"}:
        raise ValueError(f"gpu_preference takes exactly {{'exe', 'preference'}}, got {params!r}")
    exe, pref = params["exe"], params["preference"]
    if not isinstance(exe, str) or not exe or len(exe) > MAX_EXE:
        raise ValueError(f"exe must be the game's full path, got {exe!r}")
    norm = ntpath.normpath(exe)
    if not _EXE_RE.fullmatch(norm):
        raise ValueError(f"exe must be a full Windows path to an .exe file, got {exe!r}")
    if type(pref) is not int or pref not in _PREFERENCES:
        raise ValueError(f"preference must be 0, 1 or 2 (DXGI_GPU_PREFERENCE), got {pref!r}")
    return norm, pref


def _pref_label(value: Any) -> str:
    if is_absent(value):
        return "not set"
    try:
        return f"{value} ({_PREFERENCES[int(value)]})"
    except (KeyError, TypeError, ValueError):
        return repr(value)


@register
class GpuPreferenceFix(StringFieldFix):
    id = "gpu_preference"
    title = "Run the game on the high-performance GPU"
    needs_restart = "game"
    field_name = "GpuPreference"
    caveat = ("Community-sourced (unverified): Microsoft documents the Settings page and the DXGI_GPU_PREFERENCE "
              "numbers (0 unspecified, 1 minimum power, 2 high performance), not this registry format. Some "
              "games (OpenGL, some Vulkan) pick a GPU themselves and ignore it.")
    description = ("Sets the per-app graphics preference for the game's exe (the same setting as Settings > "
                   "System > Display > Graphics) to high performance, so a hybrid laptop renders it on the "
                   "discrete GPU. Other fields in the same value are kept. Windows applies it the next time the "
                   "game starts, and lag-doctor checks which GPU the game really used in the next capture. "
                   + caveat)

    def value_name(self, params: dict[str, Any]) -> str:
        return _gpu_params(params)[0]

    def field_value(self, params: dict[str, Any]) -> str:
        return str(_gpu_params(params)[1])

    def describe(self, before: Any, after: Any) -> str:
        return f"{self.title}: GpuPreference {_pref_label(before)} -> {_pref_label(after)}"
