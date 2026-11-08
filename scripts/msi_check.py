r"""Install lag-doctor's MSI on a throwaway GitHub runner, run it from a fresh shell, uninstall it, find nothing left.

Usage:  uv run python scripts/msi_check.py MSI RUNTIME_ZIP PRESENTMON_EXE LOG_DIR

CI's msi job runs this after scripts/msi_build.py (.github/workflows/ci.yml). It installs and uninstalls a per-machine
product, which changes Program Files, the system PATH and Windows Installer's registry, so it refuses to run anywhere
but a GitHub-hosted runner (scripts/stage2/guard.py) before its first change: never on a PC someone uses. The tests
check its verdicts on hand-written inputs (tests/test_msi.py).

What the install must hold comes from the inputs, never from the MSI: the runtime ZIP's entries, the files git tracks
under lagdoctor/ with one .pyc each, PresentMon with Intel's licence, bin/lagdoctor.cmd and lag-doctor's licence
(msi_build.expected_files). Every check runs and reports on its own. Exit status: 0 only if all passed, 1 if any
failed, 2 if they could not start (not a runner, an input missing, the product or its folder already there).

- install: `msiexec /i MSI /qn` exits 0 (its verbose log goes to LOG_DIR).
- files: the install folder holds exactly the expected files, the runtime's and lag-doctor's byte for byte as their
  sources, PresentMon passing lag-doctor's own check.
- registered: Windows Installer has the product, every component where the folder is expected, one product under the
  upgrade code, and an Apps entry naming lag-doctor, its version and publisher; the registry keys an install adds
  (its Uninstall entry and its Installer Products, Features and UpgradeCodes keys) appeared, and nothing else there.
- path: the system PATH is the old one with the install's bin\ added once, at the end.
- registry: every value the MSI's Registry table writes is there with its data (the Start-menu shortcut's key path,
  under HKCU: the account that ran the install).
- shortcut: the app's Start-menu shortcut, read back from the .lnk itself through the shell (IShellLinkW and the link's
  own property store), sits in every account's Start menu and nowhere else, starts the installed pythonw.exe (no
  console) on `-m lagdoctor app` in the install folder, carries the app's AppUserModel ID (lagdoctor.app.APP_ID) and
  shows an icon whose bytes are lagdoctor/app/lag-doctor.ico's.
- command: in a fresh shell (the environment a new sign-in gets, built from the registry rather than this job's),
  `where lagdoctor` finds bin\lagdoctor.cmd alone and `where python` nothing installed; `lagdoctor --version` from cmd
  and from Windows PowerShell names the MSI's version; `--help` starts; `lagdoctor setup` uses the PresentMon under
  Program Files although a verified per-user copy is there too; `analyze` writes its report.
- imports: every lagdoctor module imports in the installed runtime from the install folder, sys.path holds nothing
  else, and each shipped .pyc was compiled from the .py beside it.
- untouched: after all that ran (as an administrator), the install folder holds the same files with the same bytes,
  so no __pycache__ was written beside the shipped bytecode.
- uninstall: `msiexec /x MSI /qn` exits 0.
- gone: every installed file and the folder itself; the system PATH exactly as before; the product, its components
  and its upgrade code unknown to Windows Installer; the registry keys the install added gone, and its registry values
  and their keys as before the install; the Start-menu shortcut and the icon file it showed gone.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import winreg
import zipfile
from ctypes import wintypes
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "scripts"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import msi_build as mb  # noqa: E402
import msidb  # noqa: E402
import wheel_check as wc  # noqa: E402
from runtime_pin import EMBED_SHA256, EMBED_SIZE, EMBED_VERSION  # noqa: E402
from wheel_check import Check  # noqa: E402

MACHINE_ENV = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
UNINSTALL_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
# The keys Windows Installer adds for a per-machine product, by what names the subkey ("product" or "upgrade": the
# packed form of that code; "braced": the product code as it is).
INSTALLER_KEYS = {
    UNINSTALL_KEY: "braced",
    r"SOFTWARE\Classes\Installer\Products": "product",
    r"SOFTWARE\Classes\Installer\Features": "product",
    r"SOFTWARE\Classes\Installer\UpgradeCodes": "upgrade",
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Installer\UserData\S-1-5-18\Products": "product",
}
# Folders Windows Installer created, one value each, named by the folder's path.
INSTALLER_FOLDERS = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Installer\Folders"
MSIEXEC_TIMEOUT_S = 300      # an install or an uninstall; each took under 2 s on CI (msiexec's logs, 2026-11-08)
COMMAND_TIMEOUT_S = 120      # one command; the eight between the install and the uninstall took 72 s together
# Every command but the uninstall, from the start of the checks: each gets its own timeout cut to what is left, and
# once it is spent the rest report "not run". The uninstall keeps its own timeout, so a hang still cleans up and prints
# every verdict before the job's cap stops the step (tests/test_ci_budgets.py holds it under ci.yml's timeout-minutes).
BUDGET_S = 720
_deadline: float | None = None
MIDDLE = ("files", "registered", "path", "registry", "shortcut", "command", "imports", "untouched")

# The app's Start-menu shortcut (installer/lag-doctor.wxs): the Shortcut's Name, to which Windows Installer adds .lnk,
# and what it must start. pythonw.exe is the runtime's windowless interpreter: python.exe would open a console.
SHORTCUT_NAME = "lag-doctor.lnk"
APP_RUNTIME = "pythonw.exe"
APP_ARGUMENTS = "-m lagdoctor app"
# Known folders (KnownFolders.h): the Start menu's Programs folder every account sees, and this account's own.
FOLDERID_COMMON_PROGRAMS = "{0139D44E-6AFE-49F2-8690-3DAFCAE6FFB8}"
FOLDERID_PROGRAMS = "{A77F5D77-2E2B-44C3-A6A2-ABA601054A51}"
# The registry roots a Registry row names (Windows Installer's Registry table) that this check reads back.
REGISTRY_ROOTS = {1: ("HKCU", winreg.HKEY_CURRENT_USER), 2: ("HKLM", winreg.HKEY_LOCAL_MACHINE)}

# Run by the installed python.exe: argv[1] names the modules (JSON), argv[2] gets the result. For every .py the child
# also compares its shipped bytecode's source hash (PEP 552) with the .py's own, computed by that very runtime.
IMPORT_CHILD = r'''
import importlib, importlib.util, json, os, sys
with open(sys.argv[1], encoding="utf-8") as f:
    names = json.load(f)
out = {}
for name in names:
    try:
        out[name] = {"file": importlib.import_module(name).__file__}
    except BaseException as exc:
        out[name] = {"error": f"{type(exc).__name__}: {exc}"}
stale = []
for name, m in out.items():
    src = m.get("file")
    if not src:
        continue
    pyc = importlib.util.cache_from_source(src)
    try:
        with open(src, "rb") as f:
            want = importlib.util.source_hash(f.read())
        with open(pyc, "rb") as f:
            got = f.read(16)[8:16]
    except OSError as exc:
        stale.append(f"{name}: {exc}")
        continue
    if got != want:
        stale.append(name)
with open(sys.argv[2], "w", encoding="utf-8") as f:
    json.dump({"sys_path": sys.path, "isolated": sys.flags.isolated, "modules": out, "stale_bytecode": stale,
               "executable": sys.executable}, f)
'''


class CannotStart(Exception):
    """The checks cannot run at all (exit 2)."""


# ------------------------------------------------------------------------ pure parts ---

def packed_guid(guid: str) -> str:
    """A GUID as Windows Installer names its registry keys: {12345678-9ABC-DEF0-1234-56789ABCDEF0} ->
    87654321CBA90FED21436587A9CBED0F (the first three groups reversed, each later byte's two digits swapped)."""
    g = guid.strip().strip("{}").replace("-", "").upper()
    if len(g) != 32 or any(c not in "0123456789ABCDEF" for c in g):
        raise ValueError(f"{guid!r} is not a GUID")
    return g[7::-1] + g[11:7:-1] + g[15:11:-1] + "".join(g[i + 1] + g[i] for i in range(16, 32, 2))


def added_keys(product_code: str, upgrade_code: str) -> dict[str, set[str]]:
    """The subkey an install adds under each of INSTALLER_KEYS, upper-cased (registry names ignore case)."""
    names = {"braced": "{" + product_code.strip("{}").upper() + "}", "product": packed_guid(product_code),
             "upgrade": packed_guid(upgrade_code)}
    return {key: {names[kind]} for key, kind in INSTALLER_KEYS.items()}


def judge_keys(name: str, before: dict[str, set[str]], after: dict[str, set[str]],
               expected_new: dict[str, set[str]]) -> Check:
    """Under each key, exactly expected_new appeared and nothing went missing. With expected_new empty, the keys must
    be as before (after an uninstall). A key that could not be read is a failure, never an empty list."""
    problems = []
    for key in INSTALLER_KEYS:
        if before.get(key) is None or after.get(key) is None:
            problems.append(f"{key} could not be read")
            continue
        new, gone = after[key] - before[key], before[key] - after[key]
        if new != expected_new.get(key, set()) or gone:
            problems.append(f"{key}: new {sorted(new)}, gone {sorted(gone)}, expected new "
                            f"{sorted(expected_new.get(key, set()))}")
    if problems:
        return Check(name, False, "; ".join(problems))
    count = sum(len(v) for v in expected_new.values())
    return Check(name, True, f"{count} keys added under {len(INSTALLER_KEYS)} Installer keys" if count else
                 f"all {len(INSTALLER_KEYS)} Installer keys as before the install")


def path_parts(value: str) -> list[str]:
    return [p for p in value.split(";") if p.strip()]


def _same_dir(a: str, b: str) -> bool:
    return os.path.normcase(a.strip().rstrip("\\/")) == os.path.normcase(b.strip().rstrip("\\/"))


def judge_path_added(before: tuple[str, int] | None, after: tuple[str, int] | None, entry: str) -> Check:
    """The system PATH after the install: the old entries in order, then `entry` once."""
    if before is None or after is None:
        return Check("path", False, "the system PATH could not be read")
    old, new = path_parts(before[0]), path_parts(after[0])
    if not new or new[:-1] != old or not _same_dir(new[-1], entry) or after[1] != before[1]:
        ours = sum(_same_dir(p, entry) for p in new)
        return Check("path", False, f"expected the {len(old)} old entries, then {entry}; found {len(new)} entries "
                                    f"ending {new[-3:]}, {ours} of them the install's (value type {after[1]}, "
                                    f"was {before[1]})")
    return Check("path", True, f"{entry} added once, at the end of {len(new)} entries")


def judge_path_restored(before: tuple[str, int] | None, after: tuple[str, int] | None, entry: str) -> str | None:
    """Why the system PATH after the uninstall is not exactly the one before the install, or None."""
    if before is None or after is None:
        return "the system PATH could not be read"
    if after != before:
        left = [p for p in path_parts(after[0]) if _same_dir(p, entry)]
        return (f"the system PATH changed: {len(left)} entries still name {entry}" if left else
                "the system PATH is not exactly as before the install")
    return None


def judge_where(code: int | None, out: str, shim: Path) -> Check:
    hits = [line.strip() for line in out.splitlines() if line.strip()]
    if code != 0 or len(hits) != 1 or not _same_dir(hits[0], str(shim)):
        return Check("where", False, f"`where lagdoctor` exit code {code}, found {hits[:4]}; expected {shim} alone")
    return Check("where", True, hits[0])


def judge_python_hidden(out: str, install: Path) -> str | None:
    """Why `where python` shows the installed runtime, or None. Exit code 1 (none found) is fine."""
    exposed = [h.strip() for h in out.splitlines() if h.strip() and wc._inside(h.strip(), install)]
    return f"`where python` finds the installed runtime: {exposed}" if exposed else None


def judge_setup(code: int | None, out: str, bundled: Path, per_user: Path) -> Check:
    """`lagdoctor setup` chose the PresentMon under Program Files, though the per-user one passes its check too."""
    said = " ".join(out.split())
    low = said.lower() + " "
    if code != 0 or f"ready and verified at {bundled} ".lower() not in low or str(per_user).lower() in low:
        return Check("setup", False, f"exit code {code}; expected it to verify {bundled}, not {per_user}: "
                                     f"{said[-500:]!r}")
    return Check("setup", True, f"verified {bundled}, with a verified per-user copy also present")


def judge_imports(result: dict[str, Any], expected: list[str], install: Path) -> Check:
    if not expected:
        return Check("imports", False, "no lagdoctor modules to import")
    mods, problems = result.get("modules") or {}, []
    outside = [e for e in result.get("sys_path") or [] if not (_same_dir(e, str(install)) or wc._inside(e, install))]
    if outside or not result.get("sys_path"):
        problems.append(f"sys.path holds {outside or 'nothing'} outside {install}")
    if not result.get("isolated"):
        problems.append("the runtime is not isolated (its ._pth did not apply)")
    for name in expected:
        m = mods.get(name) or {}
        if "error" in m:
            problems.append(f"{name}: {m['error']}")
        elif not wc._inside(m.get("file"), install / mb.PACKAGE):
            problems.append(f"{name} came from {m.get('file')!r}")
    stale = result.get("stale_bytecode")
    if stale is None or stale:
        problems.append(f"bytecode not compiled from the .py beside it: {stale if stale is not None else 'unknown'}")
    if problems:
        return Check("imports", False, "; ".join(problems[:8]) + (" ..." if len(problems) > 8 else ""))
    return Check("imports", True, f"all {len(expected)} modules from {install / mb.PACKAGE}, bytecode matching")


def judge_untouched(before: dict[str, str], after: dict[str, str]) -> Check:
    """The install folder's files and their SHA-256, right after the install and after everything ran."""
    missing, extra = mb.compare(after, before)
    changed = sorted(f for f in before.keys() & after.keys() if before[f] != after[f])
    if missing or extra or changed or not before:
        return Check("untouched", False, f"written {extra[:6]}, removed {missing[:6]}, changed {changed[:6]}"
                     + ("" if before else "; nothing was installed"))
    return Check("untouched", True, f"all {len(before)} files as installed, nothing written")


def registry_data(value: str | None) -> int | str:
    """What a Registry row's Value column writes (Windows Installer's Registry table): "#<digits>" a DWORD, text
    without a leading "#" or a "[" a string. Any other form raises ValueError: this check does not read it back."""
    if value is None:
        raise ValueError("a row without a value")
    if value.startswith("#"):
        if re.fullmatch(r"#\d+", value):
            return int(value[1:])
        raise ValueError(f"the value {value!r} (only #<digits> is read back)")
    if "[" in value:
        raise ValueError(f"the formatted value {value!r}")
    return value


def registry_name(row: dict) -> str:
    hive = REGISTRY_ROOTS.get(int(row["Root"]), (f"root {row['Root']}",))[0]
    return f"{hive}\\{row['Key']}\\{row['Name']}"


def registry_keypaths(row: dict) -> set[str]:
    """How MsiGetComponentPath names a component keyed by this Registry row, lower-cased: the root as a number (01
    HKCU, 02 HKLM; 20 more on 64-bit Windows), a colon, then the key and the value name ("21:\\Software\\x\\name")."""
    root = int(row["Root"])
    tail = f":\\{row['Key']}\\{row['Name']}".lower()
    return {f"{root:02d}{tail}", f"{root + 20:02d}{tail}"}


def component_in_place(state: int, path: str | None, install: Path, registry_paths: set[str]) -> bool:
    """A component Windows Installer reports installed where it belongs: its key file in the install folder, or the
    registry value one of the MSI's Registry rows writes."""
    if state != msidb.INSTALLSTATE_LOCAL or not path:
        return False
    return wc._inside(path, install) or _same_dir(path, str(install)) or path.lower() in registry_paths


def judge_registry(rows: list[dict], found: dict[str, tuple[bool | None, Any]]) -> Check:
    """After the install: each Registry row's value is there with the data it writes. `found` maps each row's id to
    (whether its key exists, the value's data or None), (None, None) when it could not be read."""
    if not rows:
        return Check("registry", False, "the MSI writes no registry values, though its Start-menu shortcut's component "
                                        "is keyed by one")
    problems = []
    for row in rows:
        exists, data = found.get(row["Registry"], (None, None))
        try:
            want = registry_data(row["Value"])
        except ValueError as exc:
            problems.append(f"{registry_name(row)}: {exc}")
            continue
        if exists is None:
            problems.append(f"{registry_name(row)} could not be read")
        elif data != want:
            problems.append(f"{registry_name(row)} is {data!r}, not {want!r}")
    if problems:
        return Check("registry", False, "; ".join(problems))
    return Check("registry", True, "; ".join(f"{registry_name(r)} = {registry_data(r['Value'])!r}" for r in rows))


def registry_left(rows: list[dict], before: dict[str, tuple], after: dict[str, tuple]) -> list[str]:
    """After the uninstall: why any Registry row's value, or its key, is not as it was before the install."""
    out = []
    for row in rows:
        was, now = before.get(row["Registry"], (None, None)), after.get(row["Registry"], (None, None))
        if was[0] is None or now[0] is None:
            out.append(f"{registry_name(row)} could not be read")
        elif now != was:
            out.append(f"{registry_name(row)}: key there {now[0]}, value {now[1]!r}; before the install key there "
                       f"{was[0]}, value {was[1]!r}")
    return out


def expected_shortcut(install: Path) -> dict[str, str]:
    """What the Start-menu shortcut must hold, from the product and the install folder rather than the MSI."""
    from lagdoctor import app
    return {"target": str(install / APP_RUNTIME), "arguments": APP_ARGUMENTS, "working_dir": str(install),
            "app_id": app.APP_ID, "icon_sha256": mb.sha256_file(app.ICON)}


def judge_shortcut(found: dict | None, want: dict[str, str], lnk: Path, elsewhere: list[str]) -> Check:
    """The installed Start-menu shortcut as read back from the .lnk (None: there is none at `lnk`). `elsewhere` lists
    any other lag-doctor shortcut found in the Start menu's Programs folders: there must be none."""
    if found is None:
        return Check("shortcut", False, f"no shortcut at {lnk}" + (f"; found {', '.join(elsewhere)} instead"
                                                                   if elsewhere else ""))
    problems = []
    target = found.get("target") or ""
    if not _same_dir(target, want["target"]):
        console = " (python.exe opens a console)" if Path(target).name.lower() == "python.exe" else ""
        problems.append(f"it starts {target!r}{console}, not {want['target']}")
    if (found.get("arguments") or "").strip() != want["arguments"]:
        problems.append(f"its arguments are {found.get('arguments')!r}, not {want['arguments']!r}")
    if not _same_dir(found.get("working_dir") or "", want["working_dir"]):
        problems.append(f"it starts in {found.get('working_dir')!r}, not {want['working_dir']}")
    if found.get("app_id") != want["app_id"]:
        problems.append(f"its AppUserModel ID is {found.get('app_id')!r}, not {want['app_id']!r}")
    if found.get("icon_sha256") is None:
        problems.append(f"its icon {found.get('icon')!r} cannot be read")
    elif found["icon_sha256"] != want["icon_sha256"]:
        problems.append(f"its icon {found.get('icon')!r} is not lag-doctor's (SHA-256 {found['icon_sha256']})")
    if elsewhere:
        problems.append(f"another lag-doctor shortcut is in the Start menu: {', '.join(elsewhere)}")
    if problems:
        return Check("shortcut", False, f"{lnk}: " + "; ".join(problems))
    return Check("shortcut", True, f"{lnk} starts {target} {want['arguments']} with AppUserModel ID "
                                   f"{want['app_id']} and lag-doctor's icon")


def shortcut_left(paths: Iterable[Path | None]) -> str | None:
    """After the uninstall: why the Start-menu shortcut, or the icon file it showed, is still there, or None."""
    left = [str(p) for p in paths if p is not None and p.exists()]
    return f"the Start-menu shortcut or its icon is still there: {', '.join(left)}" if left else None


def parse_env_block(block: str) -> dict[str, str]:
    """A Windows environment block ("A=1\0B=2\0\0") as a dict; the hidden "=C:" entries left out."""
    out = {}
    for entry in block.split("\0"):
        if not entry or entry.startswith("="):
            continue
        name, _, value = entry.partition("=")
        out[name] = value
    return out


def exit_code(checks: list[Check]) -> int:
    return 0 if checks and all(c.ok for c in checks) else 1


# ------------------------------------------------------------------- the machine ---

def read_path() -> tuple[str, int] | None:
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, MACHINE_ENV) as key:
            value, kind = winreg.QueryValueEx(key, "Path")
    except OSError:
        return None
    return value, kind


def subkeys(path: str) -> set[str] | None:
    """The subkey names of HKLM\\path (64-bit view), upper-cased; None when the key cannot be read."""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            out, i = set(), 0
            while True:
                try:
                    out.add(winreg.EnumKey(key, i).upper())
                except OSError:
                    return out
                i += 1
    except FileNotFoundError:
        return set()
    except OSError:
        return None


def installer_keys() -> dict[str, set[str] | None]:
    return {key: subkeys(key) for key in INSTALLER_KEYS}


def installer_folders(install: Path) -> list[str] | None:
    """The folders under `install` that Windows Installer's Folders key lists; None when it cannot be read."""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, INSTALLER_FOLDERS, 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            names, i = [], 0
            while True:
                try:
                    names.append(winreg.EnumValue(key, i)[0])
                except OSError:
                    break
                i += 1
    except FileNotFoundError:
        return []
    except OSError:
        return None
    return sorted(n for n in names if _same_dir(n, str(install)) or wc._inside(n.rstrip("\\"), install))


def uninstall_entry(product_code: str) -> dict[str, Any] | None:
    """The product's Apps entry (Uninstall key) values, or None when there is no such key."""
    path = UNINSTALL_KEY + "\\" + product_code
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            out = {}
            for name in ("DisplayName", "DisplayVersion", "Publisher", "WindowsInstaller", "URLInfoAbout"):
                try:
                    out[name] = winreg.QueryValueEx(key, name)[0]
                except OSError:
                    out[name] = None
            return out
    except OSError:
        return None


def read_registry_value(row: dict) -> tuple[bool | None, Any]:
    """(whether the Registry row's key exists, its value's data or None when absent), as the 64-bit registry holds
    them; (None, None) when the row's root is not one this check reads, or the key cannot be read."""
    root = REGISTRY_ROOTS.get(int(row["Root"]))
    if root is None:
        return None, None
    try:
        with winreg.OpenKey(root[1], row["Key"], 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            try:
                return True, winreg.QueryValueEx(key, row["Name"])[0]
            except FileNotFoundError:
                return True, None
    except FileNotFoundError:
        return False, None
    except OSError:
        return None, None


def read_registry_values(rows: list[dict]) -> dict[str, tuple[bool | None, Any]]:
    return {row["Registry"]: read_registry_value(row) for row in rows}


# ------------------------------------------------- the Start-menu shortcut, through the shell's own COM objects ---

class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]


class PROPVARIANT(ctypes.Structure):
    """The 24-byte PROPVARIANT (64-bit): its type, three reserved words, then the value; a VT_LPWSTR's pointer comes
    first in the value."""
    _fields_ = [("vt", ctypes.c_ushort), ("reserved", ctypes.c_ushort * 3), ("ptr", ctypes.c_void_p),
                ("ptr2", ctypes.c_void_p)]


CLSID_SHELL_LINK = "{00021401-0000-0000-C000-000000000046}"
IID_ISHELL_LINK_W = "{000214F9-0000-0000-C000-000000000046}"
IID_IPERSIST_FILE = "{0000010B-0000-0000-C000-000000000046}"
IID_IPROPERTY_STORE = "{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}"
PKEY_APP_USER_MODEL_ID = ("{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}", 5)      # propkey.h: PKEY_AppUserModel_ID
VT_EMPTY, VT_LPWSTR = 0, 31
# Vtable slots: IUnknown's three, then IShellLinkW's GetPath (3), GetWorkingDirectory (8), GetArguments (10) and
# GetIconLocation (16); IPersistFile's Load (5); IPropertyStore's GetValue (5) (ShObjIdl_core.h, ObjIdl.h,
# propsys.h, in declaration order).
QUERY_INTERFACE, RELEASE = 0, 2
LINK_GET_PATH, LINK_GET_WORKING_DIRECTORY, LINK_GET_ARGUMENTS, LINK_GET_ICON_LOCATION = 3, 8, 10, 16
PERSIST_FILE_LOAD, STORE_GET_VALUE = 5, 5
CLSCTX_INPROC_SERVER, COINIT_APARTMENTTHREADED, STGM_READ, RPC_E_CHANGED_MODE = 1, 2, 0, -2147417850


def com_guid(text: str) -> GUID:
    g = GUID()
    ole32 = ctypes.OleDLL("ole32")
    ole32.CLSIDFromString.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(GUID)]
    ole32.CLSIDFromString(text, ctypes.byref(g))
    return g


def com_method(obj: ctypes.c_void_p, slot: int, restype, *argtypes):
    """The COM method at `slot` of obj's vtable, to be called with obj first. With ctypes.HRESULT as restype a
    failing HRESULT raises OSError."""
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtable[slot])


def com_query(obj: ctypes.c_void_p, iid: str) -> ctypes.c_void_p:
    out = ctypes.c_void_p()
    com_method(obj, QUERY_INTERFACE, ctypes.HRESULT, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))(
        obj, ctypes.byref(com_guid(iid)), ctypes.byref(out))
    return out


def com_release(obj: ctypes.c_void_p) -> None:
    if obj:
        com_method(obj, RELEASE, wintypes.ULONG)(obj)


class ComApartment:
    """COM for this thread while in use (single-threaded, as the shell's objects want), left as it was afterwards."""

    def __enter__(self):
        self.ole32 = ctypes.WinDLL("ole32")
        self.ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        self.ole32.CoInitializeEx.restype = ctypes.c_long
        hr = self.ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
        if hr < 0 and hr != RPC_E_CHANGED_MODE:             # already multi-threaded here is fine: use that
            raise ctypes.WinError(hr)
        self.owned = hr >= 0
        return self

    def __exit__(self, *exc):
        if self.owned:
            self.ole32.CoUninitialize()


def shell_link() -> ctypes.c_void_p:
    """A new ShellLink object's IShellLinkW (the apartment must be open)."""
    ole32 = ctypes.OleDLL("ole32")
    ole32.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(GUID),
                                       ctypes.POINTER(ctypes.c_void_p)]
    link = ctypes.c_void_p()
    ole32.CoCreateInstance(ctypes.byref(com_guid(CLSID_SHELL_LINK)), None, CLSCTX_INPROC_SERVER,
                           ctypes.byref(com_guid(IID_ISHELL_LINK_W)), ctypes.byref(link))
    return link


def read_shortcut(lnk: Path) -> dict[str, Any]:
    """A .lnk read back through the shell: its target, arguments, working folder, icon location and the
    System.AppUserModel.ID in the link's own property store (None when it has none)."""
    with ComApartment():
        link = shell_link()
        persist, store = ctypes.c_void_p(), ctypes.c_void_p()
        try:
            persist = com_query(link, IID_IPERSIST_FILE)
            com_method(persist, PERSIST_FILE_LOAD, ctypes.HRESULT, wintypes.LPCWSTR, wintypes.DWORD)(
                persist, str(lnk), STGM_READ)
            out: dict[str, Any] = {"path": str(lnk)}
            buf = ctypes.create_unicode_buffer(32768)
            com_method(link, LINK_GET_PATH, ctypes.HRESULT, wintypes.LPWSTR, ctypes.c_int, ctypes.c_void_p,
                       wintypes.DWORD)(link, buf, len(buf), None, 0)
            out["target"] = buf.value
            com_method(link, LINK_GET_ARGUMENTS, ctypes.HRESULT, wintypes.LPWSTR, ctypes.c_int)(link, buf, len(buf))
            out["arguments"] = buf.value
            com_method(link, LINK_GET_WORKING_DIRECTORY, ctypes.HRESULT, wintypes.LPWSTR, ctypes.c_int)(
                link, buf, len(buf))
            out["working_dir"] = buf.value
            index = ctypes.c_int()
            com_method(link, LINK_GET_ICON_LOCATION, ctypes.HRESULT, wintypes.LPWSTR, ctypes.c_int,
                       ctypes.POINTER(ctypes.c_int))(link, buf, len(buf), ctypes.byref(index))
            out["icon"], out["icon_index"] = buf.value, index.value
            store = com_query(link, IID_IPROPERTY_STORE)
            key = PROPERTYKEY(com_guid(PKEY_APP_USER_MODEL_ID[0]), PKEY_APP_USER_MODEL_ID[1])
            value = PROPVARIANT()
            com_method(store, STORE_GET_VALUE, ctypes.HRESULT, ctypes.POINTER(PROPERTYKEY),
                       ctypes.POINTER(PROPVARIANT))(store, ctypes.byref(key), ctypes.byref(value))
            try:
                if value.vt == VT_LPWSTR:
                    out["app_id"] = ctypes.wstring_at(value.ptr)
                elif value.vt == VT_EMPTY:
                    out["app_id"] = None
                else:
                    out["app_id"] = f"<a value of type {value.vt}, not text>"
            finally:
                ctypes.oledll.ole32.PropVariantClear(ctypes.byref(value))
            return out
        finally:
            for obj in (store, persist, link):
                com_release(obj)


def known_folder(folder_id: str) -> Path:
    """A known folder's path (SHGetKnownFolderPath)."""
    shell32 = ctypes.OleDLL("shell32")
    shell32.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE,
                                             ctypes.POINTER(ctypes.c_void_p)]
    out = ctypes.c_void_p()
    shell32.SHGetKnownFolderPath(ctypes.byref(com_guid(folder_id)), 0, None, ctypes.byref(out))
    try:
        return Path(ctypes.wstring_at(out.value))
    finally:
        ctypes.windll.ole32.CoTaskMemFree(out)


def shortcut_paths() -> list[Path]:
    """Where the app's shortcut must be (every account's Start menu), then where it must not (this account's)."""
    return [known_folder(FOLDERID_COMMON_PROGRAMS) / SHORTCUT_NAME, known_folder(FOLDERID_PROGRAMS) / SHORTCUT_NAME]


def check_shortcut(install: Path, seen: dict) -> Check:
    """The Start-menu shortcut, read back from the .lnk; `seen` keeps the icon file it shows, which the uninstall
    must remove too."""
    lnk, own = shortcut_paths()
    found = read_shortcut(lnk) if lnk.is_file() else None
    if found is not None:
        icon = Path(os.path.expandvars(found["icon"])) if found.get("icon") else None
        found["icon_sha256"] = mb.sha256_file(icon) if icon is not None and icon.is_file() else None
        seen["icon"] = icon
    stem = Path(SHORTCUT_NAME).stem.lower()
    elsewhere = sorted(str(p) for folder in (lnk.parent, own.parent) if folder.is_dir()
                       for p in folder.iterdir() if p.name.lower().startswith(stem) and p != lnk)
    return judge_shortcut(found, expected_shortcut(install), lnk, elsewhere)


def fresh_environment() -> dict[str, str]:
    """The environment a new sign-in of this account gets (CreateEnvironmentBlock, not inheriting this process's):
    the system and user variables as the registry holds them now, so PATH carries what the install wrote."""
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    userenv = ctypes.WinDLL("userenv", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    userenv.CreateEnvironmentBlock.argtypes = [ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.BOOL]
    userenv.CreateEnvironmentBlock.restype = wintypes.BOOL
    userenv.DestroyEnvironmentBlock.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    token_query, token_duplicate = 0x0008, 0x0002
    token, block = wintypes.HANDLE(), ctypes.c_void_p()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), token_query | token_duplicate, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not userenv.CreateEnvironmentBlock(ctypes.byref(block), token, False):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            chars, i = [], 0
            while True:                                  # NUL-separated strings, ended by an empty one
                s = ctypes.wstring_at(block.value + i * 2)
                if not s:
                    break
                chars.append(s)
                i += len(s) + 1
            return parse_env_block("\0".join(chars) + "\0\0")
        finally:
            userenv.DestroyEnvironmentBlock(block)
    finally:
        kernel32.CloseHandle(token)


def start_budget() -> None:
    global _deadline
    _deadline = time.monotonic() + BUDGET_S


def run(argv: list[str], cwd: Path, env: dict[str, str] | None = None,
        timeout: float = COMMAND_TIMEOUT_S, budgeted: bool = True) -> tuple[int | None, str]:
    """(exit code or None, stdout and stderr together). `budgeted`: cut the timeout to what is left of the budget."""
    if budgeted and _deadline is not None:
        timeout = min(timeout, _deadline - time.monotonic())
        if timeout <= 0:
            return None, f"{argv[0]} not run: the check's {BUDGET_S} s were used up"
    try:
        p = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
    except OSError as exc:
        return None, f"could not start {argv[0]}: {exc}"
    except subprocess.TimeoutExpired:
        return None, f"{argv[0]} ran past {timeout:.0f} s and was stopped"
    return p.returncode, (p.stdout + p.stderr).decode("utf-8", "replace")


def manifest(top: Path) -> dict[str, str]:
    return {f: mb.sha256_file(top / f) for f in mb.tree_files(top)} if top.is_dir() else {}


def system_tool(*parts: str) -> str:
    return str(Path(os.environ.get("SystemRoot", r"C:\Windows"), *parts))


def expected_install(runtime_zip: Path) -> tuple[set[str], list[str], list[str], zipfile.ZipFile]:
    """(every file the install must hold, the runtime's names, the package's tracked files, the open ZIP)."""
    size, digest = runtime_zip.stat().st_size, mb.sha256_file(runtime_zip)
    if size != EMBED_SIZE or digest != EMBED_SHA256:
        raise CannotStart(f"{runtime_zip} is not the pinned runtime ZIP ({size} bytes, SHA-256 {digest})")
    z = zipfile.ZipFile(runtime_zip)
    runtime_names = z.namelist()
    package = wc.tracked_files()
    tag = "cpython-" + "".join(EMBED_VERSION.split(".")[:2])
    return mb.expected_files(runtime_names, package, tag), runtime_names, package, z


def check_files(install: Path, expected: set[str], runtime_names: list[str], package: list[str],
                z: zipfile.ZipFile) -> Check:
    from lagdoctor import setup
    if not install.is_dir():
        return Check("files", False, f"{install} does not exist")
    found = mb.tree_files(install)
    missing, extra = mb.compare(found, expected)
    problems = [f"{what} {len(names)}: {', '.join(names[:8])}" for what, names in
                (("missing", missing), ("not expected", extra)) if names]
    sources = {**{n: hashlib.sha256(z.read(n)).hexdigest() for n in runtime_names},
               **{p: mb.sha256_file(ROOT / PurePosixPath(p)) for p in package},
               mb.SHIM: mb.sha256_file(mb.SHIM_SOURCE), mb.OWN_LICENSE: mb.sha256_file(mb.OWN_LICENSE_SOURCE),
               mb.INTEL_LICENSE: mb.INTEL_LICENSE_SHA256}
    differ = sorted(f for f, digest in sources.items()
                    if (install / PurePosixPath(f)).is_file() and mb.sha256_file(install / PurePosixPath(f)) != digest)
    if differ:
        problems.append(f"not byte for byte as their sources: {differ[:8]}")
    pm = install / mb.PRESENTMON_DIR / setup.EXE_NAME
    try:
        setup.verify(pm)
    except setup.SetupError as exc:
        problems.append(f"PresentMon: {exc}")
    if problems:
        return Check("files", False, "; ".join(problems))
    return Check("files", True, f"all {len(expected)} expected files, nothing else, each as its source")


def check_registered(info: dict, install: Path, before: dict, version: str) -> Check:
    code, upgrade = info["product_code"], info["upgrade_code"]
    problems = []
    state = msidb.product_state(code)
    if state != msidb.INSTALLSTATE_DEFAULT:
        problems.append(f"Windows Installer reports the product's state as {state}, not installed (5)")
    related = msidb.related_products(upgrade)
    if [r.upper() for r in related] != [code.upper()]:
        problems.append(f"the upgrade code has {related}, not just {code}")
    elsewhere = []
    registry_paths = {p for row in info["registry"] for p in registry_keypaths(row)}
    for comp in info["components"]:
        st, path = msidb.component_path(code, comp)
        if not component_in_place(st, path, install, registry_paths):
            elsewhere.append(f"{comp}: state {st}, {path!r}")
    if elsewhere:
        problems.append(f"{len(elsewhere)} of {len(info['components'])} components not installed in {install} or at "
                        f"a registry value the MSI writes: {elsewhere[:4]}")
    entry = uninstall_entry(code)
    want = {"DisplayName": mb.PRODUCT, "DisplayVersion": version, "Publisher": info["manufacturer"]}
    if entry is None or any(entry.get(k) != v for k, v in want.items()):
        problems.append(f"the Apps entry is {entry}, expected {want}")
    keys = judge_keys("keys", before, installer_keys(), added_keys(code, upgrade))
    if not keys.ok:
        problems.append(keys.detail)
    folders = installer_folders(install)
    if not folders or not any(_same_dir(f, str(install)) for f in folders):
        problems.append(f"Windows Installer's Folders key does not list {install}\\ ({folders})")
    if problems:
        return Check("registered", False, "; ".join(problems))
    return Check("registered", True, f"{code} installed in {install} ({len(info['components'])} components), "
                                     f"{keys.detail}; the Folders key lists {len(folders)} of its folders")


def check_command(env: dict[str, str], install: Path, work: Path, home: Path, version: str,
                  presentmon: Path) -> list[Check]:
    """The installed command, found through PATH by a fresh shell."""
    from lagdoctor import setup
    env = {**env, "LAGDOCTOR_HOME": str(home)}
    cmd = [system_tool("System32", "cmd.exe"), "/d", "/c"]
    ps = [system_tool("System32", "WindowsPowerShell", "v1.0", "powershell.exe"), "-NoProfile", "-NonInteractive",
          "-Command"]
    shim = install / PurePosixPath(mb.SHIM)
    checks = [judge_where(*run([system_tool("System32", "where.exe"), "lagdoctor"], work, env), shim)]
    _, out = run([system_tool("System32", "where.exe"), "python"], work, env)
    hidden = judge_python_hidden(out, install)
    checks.append(Check("python hidden", hidden is None, hidden or "`where python` finds nothing installed"))
    code, out = run(cmd + ["lagdoctor --version"], work, env)
    checks.append(wc.judge_version(code, out, version))
    code, out = run(ps + ["lagdoctor --version; exit $LASTEXITCODE"], work, env)
    ps_version = wc.judge_version(code, out, version)
    checks.append(Check("version (PowerShell)", ps_version.ok, ps_version.detail))
    code, out = run(cmd + ["lagdoctor --help"], work, env)
    checks.append(wc.judge_help(code, out))
    per_user = home / "presentmon" / setup.VERSION / setup.EXE_NAME
    per_user.parent.mkdir(parents=True)
    shutil.copyfile(presentmon, per_user)
    setup.verify(per_user)                                     # a genuine competitor, not a decoy
    code, out = run(cmd + ["lagdoctor setup"], work, env)
    checks.append(judge_setup(code, out, install / mb.PRESENTMON_DIR / setup.EXE_NAME, per_user))
    capture = wc.make_capture(work / "capture.ldcap.json.gz")
    code, out = run(cmd + [subprocess.list2cmdline(["lagdoctor", "analyze", str(capture)])], work, env)
    checks.append(wc.judge_analyze(code, out, home, capture))
    return checks


def check_imports(install: Path, package: list[str], work: Path) -> Check:
    expected = wc.module_names(package)
    asked, answer = work / "modules.json", work / "imported.json"
    asked.write_text(json.dumps(expected), encoding="utf-8")
    code, out = run([str(install / "python.exe"), "-c", IMPORT_CHILD, str(asked), str(answer)], work)
    try:
        result = json.loads(answer.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Check("imports", False, f"the import run ended with exit code {code}: {out.strip()[-600:]!r}")
    return judge_imports(result, expected, install)


def msiexec(action: str, msi: Path, log: Path) -> tuple[int | None, str]:
    """The uninstall (/x) runs outside the budget, so whatever used it up, the runner is still cleaned."""
    return run([system_tool("System32", "msiexec.exe"), action, str(msi), "/qn", "/norestart", "/l*v", str(log)],
               log.parent, timeout=MSIEXEC_TIMEOUT_S, budgeted=action != "/x")


def check_gone(info: dict, install: Path, installed: dict[str, str], before_keys: dict, path_before,
               entry: str, registry_before: dict, shortcut_seen: dict) -> Check:
    code, upgrade = info["product_code"], info["upgrade_code"]
    problems = []
    left = [f for f in installed if (install / PurePosixPath(f)).exists()]
    if left:
        problems.append(f"{len(left)} installed files left: {left[:8]}")
    if install.exists():
        problems.append(f"{install} is still there, holding {mb.tree_files(install)[:8]}")
    why = judge_path_restored(path_before, read_path(), entry)
    if why:
        problems.append(why)
    state = msidb.product_state(code)
    if state != msidb.INSTALLSTATE_UNKNOWN:
        problems.append(f"Windows Installer still reports the product's state as {state}")
    related = msidb.related_products(upgrade)
    if related:
        problems.append(f"the upgrade code still has {related}")
    registered = [c for c in info["components"] if msidb.component_path(code, c)[0] == msidb.INSTALLSTATE_LOCAL]
    if registered:
        problems.append(f"{len(registered)} components still registered")
    keys = judge_keys("keys", before_keys, installer_keys(), {})
    if not keys.ok:
        problems.append(keys.detail)
    folders = installer_folders(install)
    if folders is None or folders:
        problems.append(f"Windows Installer's Folders key still lists {folders if folders else 'something unreadable'}")
    problems += registry_left(info["registry"], registry_before, read_registry_values(info["registry"]))
    lnk_left = shortcut_left([*shortcut_paths(), shortcut_seen.get("icon")])
    if lnk_left:
        problems.append(lnk_left)
    if problems:
        return Check("gone", False, "; ".join(problems))
    return Check("gone", True, f"all {len(installed)} files and {install}, the PATH entry, the product's "
                               f"registration and its {len(INSTALLER_KEYS)} kinds of Installer keys, its "
                               f"{len(info['registry'])} registry values, the Start-menu shortcut and its icon")


def read_msi(msi: Path) -> dict:
    props = msidb.properties(str(msi))
    for key in ("ProductCode", "UpgradeCode", "ProductVersion", "Manufacturer"):
        if not props.get(key):
            raise CannotStart(f"{msi.name} has no {key}")
    comps = [r["ComponentId"] for r in msidb.rows(str(msi), "Component", ("Component", "ComponentId"))
             if r["ComponentId"]]
    return {"product_code": props["ProductCode"], "upgrade_code": props["UpgradeCode"],
            "version": props["ProductVersion"], "manufacturer": props["Manufacturer"], "components": comps,
            "registry": msidb.rows(str(msi), "Registry", ("Registry", "Root", "Key", "Name", "Value", "Component_"))}


def guarded(name: str, fn, *args) -> list[Check]:
    """Run one check; one that crashes is a failure with its error, never a skip, and never stops the others."""
    try:
        got = fn(*args)
    except Exception as exc:  # noqa: BLE001 - reported as that check's failure
        return [Check(name, False, f"the check itself failed: {type(exc).__name__}: {exc}")]
    return got if isinstance(got, list) else [got]


def install_folder() -> Path:
    """C:\\Program Files\\lag-doctor: where the MSI's INSTALLFOLDER lands (ProgramFiles64Folder)."""
    return Path(os.environ.get("ProgramW6432") or os.environ.get("ProgramFiles", r"C:\Program Files")) / mb.PRODUCT


def run_checks(msi: Path, runtime_zip: Path, presentmon: Path, logs: Path) -> list[Check]:
    start_budget()
    from stage2 import guard
    guard.require_throwaway_runner()                    # before anything is changed
    install = install_folder()
    entry = str(install / "bin") + "\\"
    info = read_msi(msi)
    expected, runtime_names, package, z = expected_install(runtime_zip)
    try:
        shortcuts = shortcut_paths()
    except OSError as exc:
        raise CannotStart(f"the Start menu's Programs folders could not be found: {exc}") from exc
    if (msidb.product_state(info["product_code"]) != msidb.INSTALLSTATE_UNKNOWN or install.exists()
            or msidb.related_products(info["upgrade_code"]) or any(p.exists() for p in shortcuts)):
        raise CannotStart(f"lag-doctor is already installed here, or {install} or a shortcut in {shortcuts} exists: "
                          "this runs on a fresh runner")
    logs.mkdir(parents=True, exist_ok=True)
    path_before, keys_before = read_path(), installer_keys()
    registry_before, shortcut_seen = read_registry_values(info["registry"]), {}
    tmp = Path(tempfile.mkdtemp(prefix="msi-check-")).resolve()
    checks: list[Check] = []
    installed: dict[str, str] = {}
    code = None
    try:
        code, out = msiexec("/i", msi, logs / "install.log")
        checks.append(Check("install", code == 0, f"msiexec /i exit code {code} {out.strip()[-300:]}".strip()))
        if code != 0:
            checks += [Check(n, False, "not run: the install failed") for n in MIDDLE]
            return checks
        installed = manifest(install)
        work, home = tmp / "work", tmp / "home"
        work.mkdir()
        home.mkdir()
        checks += guarded("files", check_files, install, expected, runtime_names, package, z)
        checks += guarded("registered", check_registered, info, install, keys_before, info["version"])
        checks += guarded("path", judge_path_added, path_before, read_path(), entry)
        checks += guarded("registry", lambda: judge_registry(info["registry"], read_registry_values(info["registry"])))
        checks += guarded("shortcut", check_shortcut, install, shortcut_seen)
        checks += guarded("command", lambda: check_command(fresh_environment(), install, work, home,
                                                           info["version"], presentmon))
        checks += guarded("imports", check_imports, install, package, work)
        checks += guarded("untouched", lambda: judge_untouched(installed, manifest(install)))
    finally:
        if code == 0 or install.exists() or msidb.product_state(info["product_code"]) != msidb.INSTALLSTATE_UNKNOWN:
            u_code, out = msiexec("/x", msi, logs / "uninstall.log")
            checks.append(Check("uninstall", u_code == 0,
                                f"msiexec /x exit code {u_code} {out.strip()[-300:]}".strip()))
            checks += guarded("gone", check_gone, info, install, installed, keys_before, path_before, entry,
                              registry_before, shortcut_seen)
        else:
            checks += [Check(n, False, "not run: nothing was installed") for n in ("uninstall", "gone")]
        z.close()
        wc._remove(tmp)
    return checks


def summary(msi: Path, checks: list[Check]) -> str:
    rows = "\n".join(f"| {c.name} | {'pass' if c.ok else '**FAIL**'} | "
                     f"{' '.join(c.detail.split()).replace('|', '/')} |" for c in checks)
    return (f"### MSI: {msi.name}\n\n{msi.stat().st_size:,} bytes, installed, run from a fresh shell and uninstalled "
            f"on this runner.\n\n| Check | Result | Detail |\n|---|---|---|\n{rows}\n")


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(__doc__)
        return 2
    msi, runtime_zip, presentmon, logs = (Path(a).resolve() for a in argv)
    try:
        for p in (msi, runtime_zip, presentmon):
            if not p.is_file():
                raise CannotStart(f"{p} is not a file")
        checks = run_checks(msi, runtime_zip, presentmon, logs)
    except CannotStart as exc:
        print(f"the MSI check cannot start: {exc}", file=sys.stderr)
        return 2
    for c in checks:
        print(f"{'PASS' if c.ok else 'FAIL'} {c.name:20} {c.detail}")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(summary(msi, checks))
    return exit_code(checks)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
