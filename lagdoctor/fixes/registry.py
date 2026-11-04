"""Registry access for fixes, redirectable into a sandbox key for tests.

Values travel as {"type": <winreg type>, "data": <data>} so an undo restores the
original type as well as the data (a DWORD put back as a string would be a new
bug). A missing value reads as ABSENT; writing ABSENT deletes it.

`RegistryBackend(prefix=...)` rewrites every path to live under that prefix
(e.g. Software\\LagDoctorTest\\<uuid>), which is how tests exercise the real
winreg calls without touching real settings.
"""
from __future__ import annotations

import winreg
from typing import Any

from lagdoctor.fixes.base import ABSENT, is_absent

HIVES = {"HKCU": winreg.HKEY_CURRENT_USER, "HKLM": winreg.HKEY_LOCAL_MACHINE}


class RegistryBackend:
    def __init__(self, prefix: str = "") -> None:
        self.prefix = prefix.strip("\\")

    def _resolve(self, hive: str, path: str) -> tuple[int, str]:
        if hive not in HIVES:
            raise ValueError(f"unsupported hive {hive!r}")
        if self.prefix:
            # Sandboxed: everything lands under HKCU\<prefix>\<hive>\<path>, whatever the hive.
            return winreg.HKEY_CURRENT_USER, f"{self.prefix}\\{hive}\\{path}"
        return HIVES[hive], path

    def get(self, hive: str, path: str, name: str) -> Any:
        root, sub = self._resolve(hive, path)
        try:
            with winreg.OpenKey(root, sub, 0, winreg.KEY_READ) as key:
                data, typ = winreg.QueryValueEx(key, name)
        except FileNotFoundError:
            return dict(ABSENT)
        return {"type": typ, "data": data}

    def set(self, hive: str, path: str, name: str, value: Any) -> None:
        root, sub = self._resolve(hive, path)
        if is_absent(value):
            try:
                with winreg.OpenKey(root, sub, 0, winreg.KEY_SET_VALUE) as key:
                    winreg.DeleteValue(key, name)
            except FileNotFoundError:
                pass
            return
        with winreg.CreateKeyEx(root, sub, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, value["type"], value["data"])

    def delete_tree(self) -> None:
        """Remove the whole sandbox (tests only); refuses without a prefix."""
        if not self.prefix:
            raise RuntimeError("refusing to delete a registry tree outside a sandbox prefix")
        _delete_tree(winreg.HKEY_CURRENT_USER, self.prefix)


def _delete_tree(root: int, sub: str) -> None:
    try:
        with winreg.OpenKey(root, sub, 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
            while True:
                try:
                    child = winreg.EnumKey(key, 0)
                except OSError:
                    break
                _delete_tree(root, f"{sub}\\{child}")
        winreg.DeleteKey(root, sub)
    except FileNotFoundError:
        pass


def sz(text: str) -> dict[str, Any]:
    return {"type": winreg.REG_SZ, "data": text}


def dword(n: int) -> dict[str, Any]:
    return {"type": winreg.REG_DWORD, "data": int(n)}
