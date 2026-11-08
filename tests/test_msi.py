"""The MSI (docs/INSTALLER-PLAN.md, Stage 3): what scripts/msi_build.py lays out, what installer/lag-doctor.wxs says,
how scripts/wix_tool.py gets WiX, and every verdict scripts/msi_check.py gives.

The real build, install, run and uninstall happen on CI's throwaway runner (ci.yml's msi job), never here: these
tests pin each verdict on hand-written inputs, so a check that stops failing turns them red, and they prove the check
refuses to install anything off a runner.
"""
from __future__ import annotations

import ctypes
import hashlib
import inspect
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from ctypes import wintypes
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import msi_build as mb  # noqa: E402
import msi_check as mc  # noqa: E402
import msidb  # noqa: E402
import runtime_pin  # noqa: E402
import wix_tool  # noqa: E402
from lagdoctor import app, setup  # noqa: E402

WXS_NS = {"w": "http://wixtoolset.org/schemas/v4/wxs"}
UPGRADE_CODE = "{955424CD-63D8-410A-A74B-618FC618CDBA}"      # lag-doctor's, for every version: never changes
INSTALL = Path(r"C:\Program Files\lag-doctor")
TAG = "cpython-313"


def wxs() -> ET.Element:
    return ET.parse(ROOT / "installer" / "lag-doctor.wxs").getroot()


# --------------------------------------------------------------------- the layout ---

def test_the_msi_version_is_three_numbers_within_windows_installers_limits():
    assert mb.msi_version("0.1.0") == "0.1.0" and mb.msi_version("255.255.65535") == "255.255.65535"
    for bad in ("0.1.0rc1", "0.1.0.dev1", "0.1", "1.2.3.4", "256.0.0", "0.256.0", "0.0.65536", "0.1.0+local", "v0.1.0"):
        with pytest.raises(mb.BuildError):
            mb.msi_version(bad)


def test_only_plain_file_names_come_out_of_the_runtime_zip():
    assert all(mb.plain_name(n) for n in ("python.exe", "python313._pth", "libcrypto-3.dll", "_ctypes.pyd"))
    assert not any(mb.plain_name(n) for n in ("Lib/os.py", "..", ".", "a\\b.dll", "C:x.dll", "", "x y.dll"))


def test_the_package_files_are_the_wheels_lagdoctor_files_alone():
    names = ["lagdoctor/", "lagdoctor/__init__.py", "lagdoctor/fixes/", "lagdoctor/fixes/power.py",
             "lag_doctor-0.1.0.dist-info/METADATA", "laglab/app.py", "lagdoctorx/evil.py"]
    assert mb.package_files(names) == ["lagdoctor/__init__.py", "lagdoctor/fixes/power.py"]
    for unsafe in ("lagdoctor/../evil.py", "lagdoctor/a\\b.py", "lagdoctor//x.py", "lagdoctor/C:x.py",
                   "lagdoctor/./x.py"):
        with pytest.raises(mb.BuildError):
            mb.package_files([unsafe])


def test_bytecode_sits_where_python_looks_for_it():
    assert mb.pyc_path("lagdoctor/fixes/power.py", TAG) == "lagdoctor/fixes/__pycache__/power.cpython-313.pyc"
    assert mb.pyc_path("lagdoctor/__init__.py", TAG) == "lagdoctor/__pycache__/__init__.cpython-313.pyc"


def test_the_expected_install_is_the_inputs_plus_one_pyc_per_module_and_the_fixed_files():
    got = mb.expected_files(["python.exe", "python313._pth"], ["lagdoctor/__init__.py", "lagdoctor/x.py"], TAG)
    assert got == {"python.exe", "python313._pth", "lagdoctor/__init__.py", "lagdoctor/x.py",
                   "lagdoctor/__pycache__/__init__.cpython-313.pyc", "lagdoctor/__pycache__/x.cpython-313.pyc",
                   "PresentMon/PresentMon-2.6.0-x64.exe", "PresentMon/LICENSE.txt", "bin/lagdoctor.cmd",
                   "LICENSE-lag-doctor.txt"}


def test_compare_finds_missing_and_extra_files_ignoring_case():
    assert mb.compare(["Python.exe", "lagdoctor/cli.py"], ["python.exe", "lagdoctor/cli.py"]) == ([], [])
    assert mb.compare(["python.exe", "lagdoctor/__pycache__/cli.cpython-313.pyc"],
                      ["python.exe", "lagdoctor/cli.py"]) == (["lagdoctor/cli.py"],
                                                              ["lagdoctor/__pycache__/cli.cpython-313.pyc"])


def test_only_unchecked_hash_bytecode_for_the_shipped_runtime_passes():
    magic = bytes.fromhex("f30d0d0a")
    header = magic + (1).to_bytes(4, "little") + b"12345678"
    assert mb.pyc_problem(header, magic) is None
    assert "magic" in mb.pyc_problem(bytes.fromhex("a70d0d0a") + header[4:], magic)
    assert "flags 0x0" in mb.pyc_problem(magic + (0).to_bytes(4, "little") + b"12345678", magic)   # a timestamp pyc
    assert "flags 0x3" in mb.pyc_problem(magic + (3).to_bytes(4, "little") + b"12345678", magic)   # checked hash
    assert "bytes" in mb.pyc_problem(header[:12], magic)


def test_wix_builds_for_x64_with_warnings_as_errors_and_accepts_the_eula_only_when_told():
    argv = mb.wix_command("wix.exe", "0.1.0", Path(r"C:\p"), Path(r"C:\o\x.msi"), None)
    assert argv[:3] == ["wix.exe", "build", str(mb.WXS)]
    assert argv[argv.index("-arch") + 1] == "x64" and "-wx" in argv
    assert argv[argv.index("-d") + 1] == "Version=0.1.0"
    assert argv[argv.index("-bindpath") + 1] == r"payload=C:\p" and argv[argv.index("-o") + 1] == r"C:\o\x.msi"
    assert "-acceptEula" not in argv
    told = mb.wix_command("wix.exe", "0.1.0", Path(r"C:\p"), Path(r"C:\o\x.msi"), "wix7")
    assert told[-2:] == ["-acceptEula", "wix7"]
    check = mb.validate_command("wix.exe", Path(r"C:\o\x.msi"), "wix7")
    assert check[:4] == ["wix.exe", "msi", "validate", "-wx"] and check[-2:] == ["-acceptEula", "wix7"]
    assert [check[i + 1] for i, a in enumerate(check) if a == "-sice"] == ["ICE61"]      # that one ICE alone
    assert "-acceptEula" not in mb.validate_command("wix.exe", Path(r"C:\o\x.msi"), None)


def test_the_installers_presentmon_folder_is_where_setup_looks_first():
    """msi_build puts PresentMon where lagdoctor/setup.py's BUNDLED expects it, beside the package."""
    package_root = Path(setup.__file__).resolve().parents[1]
    assert setup.BUNDLED.relative_to(package_root).as_posix() == f"{mb.PRESENTMON_DIR}/{setup.EXE_NAME}"
    assert f"{mb.PRESENTMON_DIR}/{setup.EXE_NAME}" in mb.expected_files([], [], TAG)


def test_the_shim_runs_the_installed_runtime_on_lagdoctor_with_every_argument():
    data = mb.SHIM_SOURCE.read_bytes()
    assert data == b'@"%~dp0..\\python.exe" -m lagdoctor %*\r\n'      # cmd.exe reads CRLF batch files safely
    assert PurePosixPath(mb.SHIM).parent.as_posix() == "bin"


def test_intels_licence_text_is_the_one_presentmon_ships_and_the_fixtures_quote():
    text = mb.INTEL_LICENSE_SOURCE.read_bytes()
    assert hashlib.sha256(text).hexdigest() == mb.INTEL_LICENSE_SHA256 and b"\r" not in text
    readme = (ROOT / "tests" / "fixtures" / "presentmon" / "README.md").read_text(encoding="utf-8")
    quoted = readme.split("```\n")[1]                     # the licence block in the fixtures' README
    assert quoted == text.decode("utf-8")


def test_the_runtime_has_one_pin_for_stage2_and_the_msi():
    from stage2 import drive
    assert (drive.EMBED_URL, drive.EMBED_SHA256, drive.EMBED_SIZE) == (runtime_pin.EMBED_URL, runtime_pin.EMBED_SHA256,
                                                                       runtime_pin.EMBED_SIZE)
    holders = [p.relative_to(ROOT).as_posix() for p in (ROOT / "scripts").rglob("*.py")
               if runtime_pin.EMBED_SHA256 in p.read_text(encoding="utf-8")]
    assert holders == ["scripts/runtime_pin.py"]
    assert runtime_pin.EMBED_PTH == "python313._pth"


# ------------------------------------------------------------------- the WiX source ---

def test_the_upgrade_code_never_changes():
    """Windows Installer knows lag-doctor's versions as one product by this code: a new one would install beside the
    old version instead of replacing it."""
    assert wxs().find("w:Package", WXS_NS).get("UpgradeCode") == UPGRADE_CODE


def test_the_package_is_per_machine_named_and_versioned_from_the_build():
    pkg = wxs().find("w:Package", WXS_NS)
    assert (pkg.get("Name"), pkg.get("Manufacturer"), pkg.get("Version"), pkg.get("Scope")) == (
        mb.PRODUCT, mb.PRODUCT, "$(Version)", "perMachine")
    upgrade = pkg.find("w:MajorUpgrade", WXS_NS)
    assert upgrade is not None and upgrade.get("DowngradeErrorMessage")
    assert pkg.find("w:MediaTemplate", WXS_NS).get("EmbedCab") == "yes"
    assert wxs().find(".//w:CustomAction", WXS_NS) is None and wxs().find(".//w:UI", WXS_NS) is None


def test_it_installs_under_64_bit_program_files_with_bin_inside():
    pf = wxs().find(".//w:StandardDirectory[@Id='ProgramFiles64Folder']", WXS_NS)
    folder = pf.find("w:Directory[@Id='INSTALLFOLDER']", WXS_NS)
    assert folder.get("Name") == mb.PRODUCT
    assert folder.find("w:Directory[@Id='BINFOLDER']", WXS_NS).get("Name") == PurePosixPath(mb.SHIM).parent.as_posix()


def test_the_payload_is_harvested_whole_with_bin_into_its_own_folder():
    files = wxs().find("w:Package", WXS_NS).findall("w:Files", WXS_NS)
    assert [(f.get("Directory"), f.get("Include")) for f in files] == [
        ("INSTALLFOLDER", r"!(bindpath.payload)\**"), ("BINFOLDER", r"!(bindpath.payload)\bin\**")]
    assert [e.get("Files") for e in files[0].findall("w:Exclude", WXS_NS)] == [r"!(bindpath.payload)\bin\**"]


def test_bin_alone_is_added_to_the_end_of_the_system_path_and_removed_on_uninstall():
    envs = wxs().findall(".//w:Environment", WXS_NS)
    assert len(envs) == 1
    e = envs[0]
    assert {k: e.get(k) for k in ("Name", "Value", "Part", "Action", "System", "Permanent")} == {
        "Name": "PATH", "Value": "[BINFOLDER]", "Part": "last", "Action": "set", "System": "yes", "Permanent": "no"}
    comp = wxs().find(".//w:Component[@Id='PathEntry']", WXS_NS)
    assert comp.get("Directory") == "BINFOLDER" and re.fullmatch(r"\{[0-9A-F-]{36}\}", comp.get("Guid"))


def test_the_path_row_the_build_expects_is_what_those_attributes_make():
    # Windows Installer's Environment table: "=" set on install, "-" removed on uninstall, "*" system; "[~];" appends.
    assert mb.PATH_ROW == {"Name": "=-*PATH", "Value": "[~];[BINFOLDER]"}


def _start_menu_component(root: ET.Element) -> ET.Element:
    folders = root.findall(".//w:StandardDirectory[@Id='ProgramMenuFolder']", WXS_NS)
    assert len(folders) == 1
    comps = folders[0].findall("w:Component", WXS_NS)
    assert len(comps) == 1 and not folders[0].findall("w:Directory", WXS_NS)      # no subfolder to clean up
    return comps[0]


def test_the_start_menu_shortcut_starts_the_app_windowless_under_its_own_id():
    """The Start-menu shortcut (docs/LAUNCHER-PLAN.md, Identity): the installed runtime's pythonw.exe, which opens no
    console, on `-m lagdoctor app` in the install folder, lag-doctor's icon from the payload, and the AppUserModel ID
    the app's process sets on itself: the two must agree, or the taskbar shows the shortcut and the window apart."""
    root = wxs()
    shortcuts = root.findall(".//w:Shortcut", WXS_NS)
    assert len(shortcuts) == 1
    s = shortcuts[0]
    assert any(child is s for child in _start_menu_component(root))
    assert (s.get("Name"), s.get("Target"), s.get("Arguments"), s.get("WorkingDirectory")) == (
        "lag-doctor", "[INSTALLFOLDER]pythonw.exe", "-m lagdoctor app", "INSTALLFOLDER")
    assert s.get("Advertise") in (None, "no") and s.get("Directory") is None       # a path target, in the folder above
    props = s.findall("w:ShortcutProperty", WXS_NS)
    assert [(p.get("Key"), p.get("Value")) for p in props] == [("System.AppUserModel.ID", app.APP_ID)]
    assert app.APP_ID == "LagDoctor.App"
    icons = s.findall("w:Icon", WXS_NS)
    assert len(icons) == 1 and icons[0].get("Id").endswith(".ico")
    source = PureWindowsPath(icons[0].get("SourceFile"))
    assert source.parts[0] == "!(bindpath.payload)"
    assert PurePosixPath(*source.parts[1:]) == PurePosixPath(app.ICON.relative_to(ROOT).as_posix())
    assert app.ICON.is_file() and app.ICON.read_bytes()[:4] == b"\x00\x00\x01\x00"     # an icon file
    # The install check expects the same shortcut: its file name, its target's name and its arguments.
    assert (mc.SHORTCUT_NAME, mc.APP_RUNTIME, mc.APP_ARGUMENTS) == ("lag-doctor.lnk", "pythonw.exe", "-m lagdoctor app")


def test_the_shortcuts_component_is_keyed_by_a_value_under_hkcu():
    """A shortcut that is not advertised, in a Start-menu folder, passes Windows Installer's ICEs only in a component
    keyed by an HKCU registry value, even in a per-machine package. WiX v7.0.1's darice.cub, run over each choice
    (2026-11-08): HKMU fails ICE57; HKLM fails ICE38, ICE43 and ICE57; a file fails ICE43 and ICE57; the folder fails
    ICE18, ICE38 and ICE43. The MSI build validates with ICE61 alone suppressed, so another choice fails CI there."""
    root = wxs()
    comp = _start_menu_component(root)
    assert re.fullmatch(r"\{[0-9A-F-]{36}\}", comp.get("Guid")) and comp.get("Directory") is None
    values = comp.findall("w:RegistryValue", WXS_NS)
    assert len(values) == 1
    assert {k: values[0].get(k) for k in ("Root", "Key", "Name", "Type", "Value", "KeyPath")} == {
        "Root": "HKCU", "Key": r"Software\lag-doctor", "Name": "StartMenuShortcut", "Type": "integer", "Value": "1",
        "KeyPath": "yes"}
    assert len(root.findall(".//w:RegistryValue", WXS_NS)) == 1 and not root.findall(".//w:RegistryKey", WXS_NS)


# ----------------------------------------------------------------- reading an MSI ---

def test_names_in_msi_tables_are_read_as_their_long_form():
    assert msidb.long_name("LAGDOC~1|lagdoctor") == "lagdoctor" and msidb.long_name("bin") == "bin"
    assert msidb.target_dir_name("LAG-DO~1|lag-doctor:src") == "lag-doctor" and msidb.target_dir_name(".") == "."


def test_each_file_is_placed_under_the_install_folder_through_its_directories():
    dirs = [{"Directory": "TARGETDIR", "Directory_Parent": None, "DefaultDir": "SourceDir"},
            {"Directory": "ProgramFiles64Folder", "Directory_Parent": "TARGETDIR", "DefaultDir": "."},
            {"Directory": "INSTALLFOLDER", "Directory_Parent": "ProgramFiles64Folder", "DefaultDir": "LAG-DO~1|lag-doctor"},
            {"Directory": "d1", "Directory_Parent": "INSTALLFOLDER", "DefaultDir": "LAGDOC~1|lagdoctor"},
            {"Directory": "d2", "Directory_Parent": "d1", "DefaultDir": "__PYCA~1|__pycache__"},
            {"Directory": "BINFOLDER", "Directory_Parent": "INSTALLFOLDER", "DefaultDir": "bin"},
            {"Directory": "other", "Directory_Parent": "ProgramFiles64Folder", "DefaultDir": "Other"}]
    comps = [{"Component": "c1", "Directory_": "INSTALLFOLDER"}, {"Component": "c2", "Directory_": "d2"},
             {"Component": "c3", "Directory_": "BINFOLDER"}, {"Component": "c4", "Directory_": "other"}]
    files = [{"File": "f1", "Component_": "c1", "FileName": "python.exe"},
             {"File": "f2", "Component_": "c2", "FileName": "CLICPY~1.PYC|cli.cpython-313.pyc"},
             {"File": "f3", "Component_": "c3", "FileName": "LAGDOC~1.CMD|lagdoctor.cmd"}]
    assert msidb.relative_paths(dirs, comps, files, "INSTALLFOLDER") == {
        "f1": "python.exe", "f2": "lagdoctor/__pycache__/cli.cpython-313.pyc", "f3": "bin/lagdoctor.cmd"}
    with pytest.raises(ValueError, match="not installed under INSTALLFOLDER"):
        msidb.relative_paths(dirs, comps, [{"File": "f4", "Component_": "c4", "FileName": "x.dll"}], "INSTALLFOLDER")
    orphan = [*comps, {"Component": "c5", "Directory_": "NOSUCHDIR"}]          # a Directory the table lacks
    with pytest.raises(ValueError, match="not installed under INSTALLFOLDER"):
        msidb.relative_paths(dirs, orphan, [{"File": "f5", "Component_": "c5", "FileName": "y.dll"}], "INSTALLFOLDER")


# --------------------------------------------------------------- getting WiX itself ---

def test_wix_comes_from_one_pinned_package_and_one_package_source():
    assert wix_tool.EULA_ID == "wix7" and wix_tool.WIX_VERSION.split(".")[0] == "7"
    assert wix_tool.WIX_URL.startswith("https://api.nuget.org/") and wix_tool.WIX_URL.endswith(
        f"/wix.{wix_tool.WIX_VERSION}.nupkg")
    assert wix_tool.package_problem(wix_tool.WIX_SIZE, wix_tool.WIX_SHA512.upper()) is None
    assert "the pin" in wix_tool.package_problem(wix_tool.WIX_SIZE, "0" * 128)
    assert "the pin" in wix_tool.package_problem(wix_tool.WIX_SIZE + 1, wix_tool.WIX_SHA512)
    cfg = ET.fromstring(wix_tool.nuget_config(Path(r"C:\t & u\feed")))
    sources = cfg.find("packageSources")
    assert [c.tag for c in sources] == ["clear", "add"]
    assert sources.find("add").get("value") == r"C:\t & u\feed"


def test_ci_accepts_the_eula_in_the_one_build_step_the_owner_agreed_to():
    """The owner accepted WiX v7's EULA for CI's builds (docs/DECISIONS.md, 2026-11-08): the acceptance is written out
    in ci.yml, once, with the id wix_tool.py's WiX asks for."""
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    commands = "\n".join(line for line in ci.splitlines() if not line.lstrip().startswith("#"))
    accepting = [line for line in commands.splitlines() if "accept-eula" in line.lower() or "accepteula" in line.lower()]
    assert len(accepting) == 1 and accepting[0].rstrip().endswith(f"--accept-eula {wix_tool.EULA_ID}"), accepting
    assert commands.index("scripts/wix_tool.py") < commands.index("scripts/msi_build.py") < commands.index(
        "scripts/msi_check.py")


# ----------------------------------------------------------------- the install check ---

def test_guids_are_packed_as_windows_installer_names_its_keys():
    # Worked by hand: groups 1-3 reversed, then each byte's two digits swapped. The same rule named a real product's
    # Products and UpgradeCodes keys exactly when read back from the registry here (2026-11-08).
    assert mc.packed_guid("{12345678-9ABC-DEF0-1234-56789ABCDEF0}") == "87654321CBA90FED21436587A9CBED0F"
    assert mc.packed_guid("12345678-9abc-def0-1234-56789abcdef0") == "87654321CBA90FED21436587A9CBED0F"
    for bad in ("{1234}", "{12345678-9ABC-DEF0-1234-56789ABCDEFG}", ""):
        with pytest.raises(ValueError):
            mc.packed_guid(bad)


PRODUCT, UPGRADE = "{11111111-2222-3333-4444-555555555555}", UPGRADE_CODE


def _keys(**extra: set[str]) -> dict[str, set[str]]:
    base = {k: {"OTHER"} for k in mc.INSTALLER_KEYS}
    for k, v in extra.items():
        base[k] = base[k] | v
    return base


def test_the_install_must_add_exactly_its_own_installer_keys():
    new = mc.added_keys(PRODUCT, UPGRADE)
    assert new[mc.UNINSTALL_KEY] == {PRODUCT} and new[r"SOFTWARE\Classes\Installer\UpgradeCodes"] == {
        mc.packed_guid(UPGRADE)}
    before = _keys()
    after = {k: before[k] | v for k, v in new.items()}
    assert mc.judge_keys("keys", before, after, new).ok
    assert not mc.judge_keys("keys", before, before, new).ok                       # nothing appeared
    extra = {k: v | {"STRAY"} for k, v in after.items()}
    assert not mc.judge_keys("keys", before, extra, new).ok                        # something else appeared too
    lost = {k: v - {"OTHER"} for k, v in after.items()}
    assert not mc.judge_keys("keys", before, lost, new).ok                         # something else went missing
    unread = {**after, mc.UNINSTALL_KEY: None}
    check = mc.judge_keys("keys", before, unread, new)
    assert not check.ok and "could not be read" in check.detail


def test_after_the_uninstall_the_installer_keys_are_as_before():
    before = _keys()
    assert mc.judge_keys("gone", before, before, {}).ok
    left = {k: v | ({mc.packed_guid(PRODUCT)} if k.endswith("Products") else set()) for k, v in before.items()}
    assert not mc.judge_keys("gone", before, left, {}).ok


ENTRY = str(INSTALL / "bin") + "\\"
OLD = (r"C:\Windows\system32;C:\Windows;C:\Tools\bin", 2)            # REG_EXPAND_SZ


def test_the_path_gains_bin_once_at_the_end_and_nothing_else_changes():
    assert mc.judge_path_added(OLD, (OLD[0] + ";" + ENTRY, 2), ENTRY).ok
    assert mc.judge_path_added(OLD, (OLD[0] + ";" + ENTRY.rstrip("\\"), 2), ENTRY).ok      # without the backslash
    bad = {"missing": OLD, "twice": (OLD[0] + ";" + ENTRY + ";" + ENTRY, 2),
           "first": (ENTRY + ";" + OLD[0], 2), "old reordered": (r"C:\Windows;C:\Windows\system32;C:\Tools\bin;" + ENTRY, 2),
           "old entry lost": (r"C:\Windows\system32;C:\Windows;" + ENTRY, 2), "type changed": (OLD[0] + ";" + ENTRY, 1),
           "python's folder instead": (OLD[0] + ";" + str(INSTALL) + "\\", 2)}
    assert {k: mc.judge_path_added(OLD, v, ENTRY).ok for k, v in bad.items()} == dict.fromkeys(bad, False)
    assert not mc.judge_path_added(None, OLD, ENTRY).ok


def test_after_the_uninstall_the_path_is_exactly_as_before():
    assert mc.judge_path_restored(OLD, OLD, ENTRY) is None
    assert "still name" in mc.judge_path_restored(OLD, (OLD[0] + ";" + ENTRY, 2), ENTRY)
    assert mc.judge_path_restored(OLD, (OLD[0] + ";C:\\Else", 2), ENTRY) is not None
    assert mc.judge_path_restored(OLD, (OLD[0], 1), ENTRY) is not None
    assert mc.judge_path_restored(OLD, None, ENTRY) is not None


SHIM = INSTALL / "bin" / "lagdoctor.cmd"


def test_where_must_find_the_shim_and_nothing_else():
    assert mc.judge_where(0, f"{SHIM}\r\n", SHIM).ok
    assert not mc.judge_where(0, f"C:\\Users\\x\\.venv\\Scripts\\lagdoctor.exe\r\n{SHIM}\r\n", SHIM).ok
    assert not mc.judge_where(1, "INFO: Could not find files for the given pattern(s).\r\n", SHIM).ok
    assert not mc.judge_where(0, "", SHIM).ok


def test_the_installed_python_must_not_be_on_path():
    assert mc.judge_python_hidden("C:\\Python313\\python.exe\r\n", INSTALL) is None
    assert mc.judge_python_hidden("", INSTALL) is None
    assert "installed runtime" in mc.judge_python_hidden(f"{INSTALL}\\python.exe\r\n", INSTALL)


BUNDLED_PM = INSTALL / "PresentMon" / "PresentMon-2.6.0-x64.exe"
PER_USER = Path(r"C:\Users\x\AppData\Local\Temp\msi-check-1\home\presentmon\2.6.0\PresentMon-2.6.0-x64.exe")


def test_setup_must_choose_the_presentmon_under_program_files():
    said = (f"PresentMon: ready and verified at {BUNDLED_PM} (SHA-256 pinned, signed by Intel Corporation)\n"
            "Frame timing: ready (this account may start ETW traces).\n")
    assert mc.judge_setup(0, said, BUNDLED_PM, PER_USER).ok
    assert mc.judge_setup(0, said.replace("Program Files", "PROGRAM FILES"), BUNDLED_PM, PER_USER).ok
    assert not mc.judge_setup(0, said.replace(str(BUNDLED_PM), str(PER_USER)), BUNDLED_PM, PER_USER).ok
    assert not mc.judge_setup(1, said, BUNDLED_PM, PER_USER).ok
    assert not mc.judge_setup(0, "PresentMon 2.6.0 is not installed\n", BUNDLED_PM, PER_USER).ok


def _imported(names: list[str], **override) -> dict:
    mods = {n: {"file": str(INSTALL / (n.replace(".", "\\") + ".py"))} for n in names}
    mods.update(override)
    return {"sys_path": [str(INSTALL / "python313.zip"), str(INSTALL)], "isolated": 1, "modules": mods,
            "stale_bytecode": []}


def test_every_module_must_import_from_the_install_with_matching_bytecode():
    names = ["lagdoctor", "lagdoctor.cli"]
    good = _imported(names)
    good["modules"]["lagdoctor"]["file"] = str(INSTALL / "lagdoctor" / "__init__.py")
    assert mc.judge_imports(good, names, INSTALL).ok
    cases = {"import error": {**good, "modules": {**good["modules"], "lagdoctor.cli": {"error": "ImportError: x"}}},
             "from the checkout": {**good, "modules": {**good["modules"],
                                                       "lagdoctor.cli": {"file": r"C:\repo\lagdoctor\cli.py"}}},
             "stale bytecode": {**good, "stale_bytecode": ["lagdoctor.cli"]},
             "no bytecode report": {k: v for k, v in good.items() if k != "stale_bytecode"},
             "site-packages on sys.path": {**good, "sys_path": [*good["sys_path"], r"C:\Python313\Lib\site-packages"]},
             "not isolated": {**good, "isolated": 0},
             "unreported": {**good, "modules": {"lagdoctor": good["modules"]["lagdoctor"]}}}
    assert {k: mc.judge_imports(v, names, INSTALL).ok for k, v in cases.items()} == dict.fromkeys(cases, False)
    assert not mc.judge_imports(good, [], INSTALL).ok


def test_running_it_must_write_nothing_into_the_install():
    before = {"python.exe": "a", "lagdoctor/cli.py": "b"}
    assert mc.judge_untouched(before, dict(before)).ok
    written = {**before, "lagdoctor/__pycache__/cli.cpython-313.pyc": "c"}
    check = mc.judge_untouched(before, written)
    assert not check.ok and "cli.cpython-313.pyc" in check.detail
    assert not mc.judge_untouched(before, {**before, "lagdoctor/cli.py": "changed"}).ok
    assert not mc.judge_untouched(before, {"python.exe": "a"}).ok
    assert not mc.judge_untouched({}, {}).ok                       # nothing installed is no pass


def test_an_environment_block_is_read_without_the_drive_entries():
    assert mc.parse_env_block("=C:=C:\\x\0Path=C:\\a;C:\\b\0TEMP=C:\\t\0\0") == {"Path": "C:\\a;C:\\b", "TEMP": "C:\\t"}
    assert mc.parse_env_block("\0") == {}


def test_a_check_that_crashes_is_a_failure_and_the_rest_still_run():
    def boom():
        raise RuntimeError("no registry")
    got = mc.guarded("registered", boom)
    assert [(c.name, c.ok) for c in got] == [("registered", False)] and "no registry" in got[0].detail
    assert mc.guarded("x", lambda: mc.Check("x", True, "")) == [mc.Check("x", True, "")]
    assert mc.exit_code([mc.Check("a", True, ""), mc.Check("b", False, "")]) == 1 and mc.exit_code([]) == 1


def test_the_check_installs_nothing_off_a_github_hosted_runner(monkeypatch, tmp_path):
    """Never on a PC someone uses: off a runner it stops, before reading the MSI or calling msiexec."""
    for var in ("GITHUB_ACTIONS", "RUNNER_ENVIRONMENT"):
        monkeypatch.delenv(var, raising=False)
    called = []
    monkeypatch.setattr(mc, "msiexec", lambda *a: called.append(a) or (0, ""))
    monkeypatch.setattr(mc, "read_msi", lambda msi: called.append(msi))
    with pytest.raises(SystemExit) as stopped:
        mc.run_checks(tmp_path / "missing.msi", tmp_path / "missing.zip", tmp_path / "pm.exe", tmp_path / "logs")
    assert stopped.value.code == 2 and called == [] and not (tmp_path / "logs").exists()
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("RUNNER_ENVIRONMENT", "self-hosted")              # someone's own machine, maybe
    with pytest.raises(SystemExit):
        mc.run_checks(tmp_path / "missing.msi", tmp_path / "missing.zip", tmp_path / "pm.exe", tmp_path / "logs")
    assert called == []


def test_every_check_between_install_and_uninstall_runs_in_the_order_reported():
    """MIDDLE names the checks reported as not run when the install fails; run_checks must run exactly those."""
    ran = re.findall(r'guarded\("([a-z ]+)"', inspect.getsource(mc.run_checks))
    assert ran == [*mc.MIDDLE, "gone"]
    assert mc.MIDDLE == ("files", "registered", "path", "registry", "shortcut", "command", "imports", "untouched")


# ----------------------------------------------------------- the Start-menu shortcut and its registry value ---

KEYPATH_ROW = {"Registry": "reg1", "Root": "1", "Key": r"Software\lag-doctor", "Name": "StartMenuShortcut",
               "Value": "#1", "Component_": "StartMenuShortcut"}


def test_a_registry_row_is_read_back_as_the_data_it_writes():
    assert mc.registry_data("#1") == 1 and mc.registry_data("#4096") == 4096 and mc.registry_data("on") == "on"
    for unread in ("#x01ff", "#%C:\\x", "##1", "#", "[INSTALLFOLDER]", "a[~]b", None):
        with pytest.raises(ValueError):
            mc.registry_data(unread)
    assert mc.registry_name(KEYPATH_ROW) == r"HKCU\Software\lag-doctor\StartMenuShortcut"


def test_the_registry_check_needs_each_value_there_with_its_data():
    assert mc.judge_registry([KEYPATH_ROW], {"reg1": (True, 1)}).ok
    bad = {"value missing": {"reg1": (True, None)}, "key missing": {"reg1": (False, None)},
           "unreadable": {"reg1": (None, None)}, "other data": {"reg1": (True, 2)}, "text, not a number": {
               "reg1": (True, "1")}, "not read at all": {}}
    assert {k: mc.judge_registry([KEYPATH_ROW], v).ok for k, v in bad.items()} == dict.fromkeys(bad, False)
    assert not mc.judge_registry([], {}).ok                             # the shortcut's key path must be there
    hku = {**KEYPATH_ROW, "Root": "3"}                                  # a root this check does not read
    assert not mc.judge_registry([hku], {"reg1": mc.read_registry_value(hku)}).ok


def test_after_the_uninstall_each_registry_value_and_its_key_are_as_before():
    before = {"reg1": (False, None)}
    assert mc.registry_left([KEYPATH_ROW], before, {"reg1": (False, None)}) == []
    for after in ({"reg1": (True, 1)}, {"reg1": (True, None)}, {"reg1": (None, None)}, {}):   # value, empty key
        assert mc.registry_left([KEYPATH_ROW], before, after), after
    kept = {"reg1": (True, None)}                                       # the key was there before: it stays
    assert mc.registry_left([KEYPATH_ROW], kept, kept) == []


def test_a_component_belongs_in_the_install_folder_or_at_the_value_the_msi_writes():
    paths = mc.registry_keypaths(KEYPATH_ROW)
    assert paths == {"01:\\software\\lag-doctor\\startmenushortcut", "21:\\software\\lag-doctor\\startmenushortcut"}
    local, absent = msidb.INSTALLSTATE_LOCAL, msidb.INSTALLSTATE_ABSENT
    assert mc.component_in_place(local, str(INSTALL / "python.exe"), INSTALL, paths)
    assert mc.component_in_place(local, str(INSTALL / "bin") + "\\", INSTALL, paths)
    assert mc.component_in_place(local, "21:\\Software\\lag-doctor\\StartMenuShortcut", INSTALL, paths)
    for state, path in ((absent, str(INSTALL / "python.exe")), (local, r"C:\Windows\python.exe"), (local, ""),
                        (local, "21:\\Software\\Other\\StartMenuShortcut"), (local, "22:\\Software\\lag-doctor\\x"),
                        (absent, "21:\\Software\\lag-doctor\\StartMenuShortcut")):
        assert not mc.component_in_place(state, path, INSTALL, paths), (state, path)


def test_the_registered_check_takes_the_shortcuts_component_at_its_registry_value(monkeypatch):
    info = {"product_code": PRODUCT, "upgrade_code": UPGRADE, "manufacturer": "lag-doctor",
            "components": ["{C1}", "{C2}"], "registry": [KEYPATH_ROW]}
    before = _keys()
    after = {k: before[k] | v for k, v in mc.added_keys(PRODUCT, UPGRADE).items()}
    monkeypatch.setattr(mc.msidb, "product_state", lambda code: msidb.INSTALLSTATE_DEFAULT)
    monkeypatch.setattr(mc.msidb, "related_products", lambda code: [PRODUCT])
    monkeypatch.setattr(mc, "uninstall_entry", lambda code: {"DisplayName": "lag-doctor", "DisplayVersion": "0.1.0",
                                                             "Publisher": "lag-doctor"})
    monkeypatch.setattr(mc, "installer_keys", lambda: after)
    monkeypatch.setattr(mc, "installer_folders", lambda install: [str(install) + "\\"])
    where = {"{C1}": (msidb.INSTALLSTATE_LOCAL, str(INSTALL / "python.exe")),
             "{C2}": (msidb.INSTALLSTATE_LOCAL, "21:\\Software\\lag-doctor\\StartMenuShortcut")}
    monkeypatch.setattr(mc.msidb, "component_path", lambda code, comp: where[comp])
    check = mc.check_registered(info, INSTALL, before, "0.1.0")
    assert check.ok, check.detail
    where["{C2}"] = (msidb.INSTALLSTATE_LOCAL, "21:\\Software\\elsewhere\\StartMenuShortcut")
    check = mc.check_registered(info, INSTALL, before, "0.1.0")
    assert not check.ok and "{C2}" in check.detail


ICON_SHA = "ab" * 32
WANT = {"target": str(INSTALL / "pythonw.exe"), "arguments": "-m lagdoctor app", "working_dir": str(INSTALL),
        "app_id": "LagDoctor.App", "icon_sha256": ICON_SHA}
LNK = Path(r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs\lag-doctor.lnk")


def test_the_shortcut_must_start_pythonw_on_the_app_with_its_id_and_icon():
    good = {"target": str(INSTALL / "pythonw.exe"), "arguments": "-m lagdoctor app", "working_dir": str(INSTALL),
            "app_id": "LagDoctor.App", "icon": r"C:\Windows\Installer\{X}\lagdoctor.ico", "icon_sha256": ICON_SHA}
    assert mc.judge_shortcut(good, WANT, LNK, []).ok
    assert mc.judge_shortcut({**good, "target": str(INSTALL / "PythonW.exe"), "working_dir": str(INSTALL) + "\\"},
                             WANT, LNK, []).ok                                       # Windows names ignore case
    bad = {"another ID": {"app_id": "Python.App"}, "no ID": {"app_id": None},
           "python.exe": {"target": str(INSTALL / "python.exe")},
           "a python elsewhere": {"target": r"C:\Python313\pythonw.exe"},
           "no arguments": {"arguments": ""}, "the CLI's arguments": {"arguments": "-m lagdoctor"},
           "more arguments": {"arguments": "-m lagdoctor app --debug"},
           "started elsewhere": {"working_dir": r"C:\Windows\System32"},
           "Python's icon": {"icon_sha256": "cd" * 32}, "an icon that cannot be read": {"icon_sha256": None}}
    outcomes = {k: mc.judge_shortcut({**good, **v}, WANT, LNK, []) for k, v in bad.items()}
    assert {k: c.ok for k, c in outcomes.items()} == dict.fromkeys(bad, False)
    assert "opens a console" in outcomes["python.exe"].detail
    assert not mc.judge_shortcut(None, WANT, LNK, []).ok                            # no shortcut at all
    own = r"C:\Users\x\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\lag-doctor.lnk"
    check = mc.judge_shortcut(None, WANT, LNK, [own])
    assert not check.ok and own in check.detail                                     # only this account's
    assert not mc.judge_shortcut(good, WANT, LNK, [own]).ok                         # and a second one there


def test_the_expected_shortcut_comes_from_the_app_and_the_install_folder():
    want = mc.expected_shortcut(INSTALL)
    assert want == {"target": r"C:\Program Files\lag-doctor\pythonw.exe", "arguments": "-m lagdoctor app",
                    "working_dir": r"C:\Program Files\lag-doctor", "app_id": app.APP_ID,
                    "icon_sha256": hashlib.sha256(app.ICON.read_bytes()).hexdigest()}


def test_the_start_menus_programs_folders_are_the_known_ones():
    common, own = mc.shortcut_paths()
    assert os.path.normcase(common) == os.path.normcase(
        Path(os.environ["ProgramData"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "lag-doctor.lnk")
    assert os.path.normcase(own) == os.path.normcase(
        Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "lag-doctor.lnk")


def _write_lnk(path: Path, target: str, arguments: str, working_dir: str, icon: Path, app_id: str | None) -> None:
    """A .lnk written through the shell's own ShellLink object, the AppUserModel ID in its property store."""
    with mc.ComApartment():
        link = mc.shell_link()
        store, persist = ctypes.c_void_p(), ctypes.c_void_p()
        try:
            for slot, text in ((20, target), (11, arguments), (9, working_dir)):    # SetPath, SetArguments, ...
                mc.com_method(link, slot, ctypes.HRESULT, wintypes.LPCWSTR)(link, text)
            mc.com_method(link, 17, ctypes.HRESULT, wintypes.LPCWSTR, ctypes.c_int)(link, str(icon), 0)
            if app_id is not None:
                store = mc.com_query(link, mc.IID_IPROPERTY_STORE)
                text = ctypes.create_unicode_buffer(app_id)
                value = mc.PROPVARIANT()
                value.vt, value.ptr = mc.VT_LPWSTR, ctypes.cast(text, ctypes.c_void_p).value
                key = mc.PROPERTYKEY(mc.com_guid(mc.PKEY_APP_USER_MODEL_ID[0]), mc.PKEY_APP_USER_MODEL_ID[1])
                mc.com_method(store, 6, ctypes.HRESULT, ctypes.POINTER(mc.PROPERTYKEY),     # SetValue, Commit
                              ctypes.POINTER(mc.PROPVARIANT))(store, ctypes.byref(key), ctypes.byref(value))
                mc.com_method(store, 7, ctypes.HRESULT)(store)
            persist = mc.com_query(link, mc.IID_IPERSIST_FILE)
            mc.com_method(persist, 6, ctypes.HRESULT, wintypes.LPCWSTR, wintypes.BOOL)(persist, str(path), True)
        finally:
            for obj in (store, persist, link):
                mc.com_release(obj)


def test_a_shortcut_written_through_the_shell_reads_back_whole(tmp_path):
    """read_shortcut against a real .lnk, and against two readers it shares no code with: WScript.Shell's shortcut
    object and Shell.Application's view of the link's System.AppUserModel.ID."""
    lnk, icon = tmp_path / "lag-doctor.lnk", tmp_path / "x.ico"
    icon.write_bytes(app.ICON.read_bytes())
    _write_lnk(lnk, str(INSTALL / "pythonw.exe"), "-m lagdoctor app", str(INSTALL), icon, "LagDoctor.App")
    got = mc.read_shortcut(lnk)
    assert {k: got[k] for k in ("target", "arguments", "working_dir", "icon", "icon_index", "app_id")} == {
        "target": str(INSTALL / "pythonw.exe"), "arguments": "-m lagdoctor app", "working_dir": str(INSTALL),
        "icon": str(icon), "icon_index": 0, "app_id": "LagDoctor.App"}
    script = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($args[0]); $s.TargetPath; $s.Arguments; "
              "$s.WorkingDirectory; $s.IconLocation; (New-Object -ComObject Shell.Application).NameSpace($args[1])."
              "ParseName($args[2]).ExtendedProperty('System.AppUserModel.ID')")
    p = subprocess.run([shutil.which("powershell") or "powershell", "-NoProfile", "-NonInteractive", "-Command",
                        f"& {{ {script} }}", str(lnk), str(lnk.parent), lnk.name],
                       capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr
    assert p.stdout.splitlines() == [got["target"], got["arguments"], got["working_dir"], f"{got['icon']},0",
                                     got["app_id"]]
    plain = tmp_path / "plain.lnk"                                       # a shortcut without an ID reads as None
    _write_lnk(plain, str(INSTALL / "pythonw.exe"), "-m lagdoctor app", str(INSTALL), icon, None)
    assert mc.read_shortcut(plain)["app_id"] is None


def test_the_shortcut_check_reads_the_start_menus_link_and_its_icon(tmp_path, monkeypatch):
    common, own = tmp_path / "common", tmp_path / "own"
    common.mkdir()
    own.mkdir()
    monkeypatch.setattr(mc, "shortcut_paths", lambda: [common / mc.SHORTCUT_NAME, own / mc.SHORTCUT_NAME])
    icon = tmp_path / "Installer" / "lagdoctor.ico"
    icon.parent.mkdir()
    icon.write_bytes(app.ICON.read_bytes())
    _write_lnk(common / mc.SHORTCUT_NAME, str(INSTALL / "pythonw.exe"), "-m lagdoctor app", str(INSTALL), icon,
               app.APP_ID)
    seen: dict = {}
    check = mc.check_shortcut(INSTALL, seen)
    assert check.ok, check.detail
    assert seen["icon"] == icon
    icon.write_bytes(b"\x00\x00\x01\x00 not lag-doctor's")
    assert not mc.check_shortcut(INSTALL, {}).ok
    icon.write_bytes(app.ICON.read_bytes())
    shutil.copyfile(common / mc.SHORTCUT_NAME, own / mc.SHORTCUT_NAME)  # a copy in this account's Start menu
    check = mc.check_shortcut(INSTALL, {})
    assert not check.ok and str(own / mc.SHORTCUT_NAME) in check.detail
    (own / mc.SHORTCUT_NAME).unlink()
    (common / mc.SHORTCUT_NAME).unlink()
    assert not mc.check_shortcut(INSTALL, {}).ok


def test_the_uninstall_must_take_the_shortcut_and_its_icon_away(tmp_path):
    lnk, icon = tmp_path / "lag-doctor.lnk", tmp_path / "lagdoctor.ico"
    assert mc.shortcut_left([lnk, None, icon]) is None
    lnk.write_bytes(b"")
    assert str(lnk) in mc.shortcut_left([lnk, None, icon])
    lnk.unlink()
    icon.write_bytes(b"")
    assert str(icon) in mc.shortcut_left([lnk, None, icon])


def test_the_gone_check_finds_a_shortcut_or_registry_value_left_behind(tmp_path, monkeypatch):
    install = tmp_path / "Program Files" / "lag-doctor"
    common, own = tmp_path / "common" / mc.SHORTCUT_NAME, tmp_path / "own" / mc.SHORTCUT_NAME
    icon = tmp_path / "Installer" / "lagdoctor.ico"
    for p in (common, own, icon):
        p.parent.mkdir(parents=True, exist_ok=True)
    info = {"product_code": PRODUCT, "upgrade_code": UPGRADE, "components": ["{C1}"], "registry": [KEYPATH_ROW]}
    keys, registry_now = _keys(), {"reg1": (False, None)}
    monkeypatch.setattr(mc.msidb, "product_state", lambda code: msidb.INSTALLSTATE_UNKNOWN)
    monkeypatch.setattr(mc.msidb, "related_products", lambda code: [])
    monkeypatch.setattr(mc.msidb, "component_path", lambda code, comp: (msidb.INSTALLSTATE_UNKNOWN, ""))
    monkeypatch.setattr(mc, "installer_keys", lambda: keys)
    monkeypatch.setattr(mc, "installer_folders", lambda install: [])
    monkeypatch.setattr(mc, "read_path", lambda: OLD)
    monkeypatch.setattr(mc, "read_registry_values", lambda rows: registry_now)
    monkeypatch.setattr(mc, "shortcut_paths", lambda: [common, own])

    def gone():
        return mc.check_gone(info, install, {"python.exe": "x"}, keys, OLD, ENTRY, {"reg1": (False, None)},
                             {"icon": icon})
    assert gone().ok, gone().detail
    for leftover in (common, own, icon):
        leftover.write_bytes(b"")
        check = gone()
        assert not check.ok and str(leftover) in check.detail
        leftover.unlink()
    registry_now = {"reg1": (True, None)}                                # the emptied key left behind
    check = gone()
    assert not check.ok and "StartMenuShortcut" in check.detail


# ------------------------------------------------- the build and the file check, run on scripted inputs ---

MAGIC = "f30d0d0a"


def _zip(path: Path, files: dict[str, bytes]) -> Path:
    import zipfile
    with zipfile.ZipFile(path, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return path


def _fake_compileall(monkeypatch):
    """The payload runtime stood in for: its facts, and a compileall that writes unchecked-hash headers."""
    import subprocess as sp
    monkeypatch.setattr(mb, "runtime_facts", lambda python: {"tag": TAG, "magic": MAGIC, "version": "3.13.17"})

    def run(argv, **kw):
        assert argv[1:5] == ["-m", "compileall", "-q", "--invalidation-mode"] and argv[5] == "unchecked-hash"
        for py in Path(argv[-1]).rglob("*.py"):
            pyc = py.parent / "__pycache__" / f"{py.stem}.{TAG}.pyc"
            pyc.parent.mkdir(exist_ok=True)
            pyc.write_bytes(bytes.fromhex(MAGIC) + (1).to_bytes(4, "little") + b"8 bytes!" + b"code")
        return sp.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(mb.subprocess, "run", run)


RUNTIME = {"python.exe": b"MZ runtime", "python313._pth": b"python313.zip\r\n.\r\n", "python313.zip": b"PK stdlib"}
WHEEL = {"lagdoctor/__init__.py": b'__version__ = "0.1.0"\n', "lagdoctor/fixes/__init__.py": b"",
         "lagdoctor/fixes/power.py": b"x = 1\n", "lag_doctor-0.1.0.dist-info/METADATA": b"Version: 0.1.0\n"}


def test_the_payload_is_laid_out_as_the_install_folder_and_checked(presentmon_exe, tmp_path, monkeypatch):
    _fake_compileall(monkeypatch)
    payload = tmp_path / "payload"
    got = mb.stage(payload, _zip(tmp_path / "w.whl", WHEEL), _zip(tmp_path / "r.zip", RUNTIME), presentmon_exe)
    package = [n for n in WHEEL if n.startswith("lagdoctor/")]
    assert set(mb.tree_files(payload)) == mb.expected_files(RUNTIME, package, TAG)
    assert got["runtime_files"] == 3 and got["package_files"] == 3
    assert (payload / "bin" / "lagdoctor.cmd").read_bytes() == mb.SHIM_SOURCE.read_bytes()
    assert (payload / "PresentMon" / "LICENSE.txt").read_bytes() == mb.INTEL_LICENSE_SOURCE.read_bytes()
    assert (payload / "PresentMon" / setup.EXE_NAME).read_bytes() == presentmon_exe.read_bytes()
    assert (payload / "python.exe").read_bytes() == RUNTIME["python.exe"]


def test_a_payload_part_that_fails_its_check_stops_the_build(presentmon_exe, tmp_path, monkeypatch):
    _fake_compileall(monkeypatch)
    runtime = _zip(tmp_path / "r.zip", RUNTIME)
    with pytest.raises(mb.BuildError, match="unsafe name"):
        mb.stage(tmp_path / "p1", _zip(tmp_path / "bad.whl", {**WHEEL, "lagdoctor/../x.py": b""}), runtime,
                 presentmon_exe)
    with pytest.raises(mb.BuildError, match="not plain file names"):
        mb.stage(tmp_path / "p2", _zip(tmp_path / "w.whl", WHEEL), _zip(tmp_path / "r2.zip",
                                                                         {**RUNTIME, "Lib/os.py": b""}), presentmon_exe)
    decoy = tmp_path / "decoy" / setup.EXE_NAME
    decoy.parent.mkdir()
    data = bytearray(presentmon_exe.read_bytes())
    data[500_000] ^= 0xFF                                  # inside the code: a signed PE ends in zero padding
    decoy.write_bytes(bytes(data))
    assert decoy.read_bytes() != presentmon_exe.read_bytes()
    with pytest.raises(setup.VerifyError):
        mb.stage(tmp_path / "p3", _zip(tmp_path / "w.whl", WHEEL), runtime, decoy)
    monkeypatch.setattr(mb, "INTEL_LICENSE_SHA256", "0" * 64)               # its pin moved, or git rewrote it
    with pytest.raises(mb.BuildError, match="Intel's licence"):
        mb.stage(tmp_path / "p4", _zip(tmp_path / "w.whl", WHEEL), runtime, presentmon_exe)


def test_bytecode_the_runtime_would_not_use_stops_the_build(presentmon_exe, tmp_path, monkeypatch):
    _fake_compileall(monkeypatch)
    monkeypatch.setattr(mb, "runtime_facts", lambda python: {"tag": TAG, "magic": "a70d0d0a", "version": "3.12.0"})
    with pytest.raises(mb.BuildError, match="bytecode for lagdoctor/__init__.py: magic"):
        mb.stage(tmp_path / "p", _zip(tmp_path / "w.whl", WHEEL), _zip(tmp_path / "r.zip", RUNTIME), presentmon_exe)


def _installed(tmp_path: Path, presentmon: Path) -> tuple[Path, set, list, list]:
    """An install folder as the MSI should leave it, for a two-file runtime and two of lag-doctor's real modules."""
    import shutil
    install = tmp_path / "Program Files" / "lag-doctor"
    package = ["lagdoctor/__init__.py", "lagdoctor/clock.py"]
    runtime = ["python.exe", "python313._pth"]
    for name in runtime:
        (install / name).parent.mkdir(parents=True, exist_ok=True)
        (install / name).write_bytes(RUNTIME[name])
    for p in package:
        dest = install / PurePosixPath(p)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / PurePosixPath(p), dest)
        pyc = install / PurePosixPath(mb.pyc_path(p, TAG))
        pyc.parent.mkdir(exist_ok=True)
        pyc.write_bytes(b"pyc")
    for src, rel in ((presentmon, f"{mb.PRESENTMON_DIR}/{setup.EXE_NAME}"), (mb.INTEL_LICENSE_SOURCE, mb.INTEL_LICENSE),
                     (mb.SHIM_SOURCE, mb.SHIM), (mb.OWN_LICENSE_SOURCE, mb.OWN_LICENSE)):
        (install / PurePosixPath(rel)).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, install / PurePosixPath(rel))
    return install, mb.expected_files(runtime, package, TAG), runtime, package


def test_the_file_check_passes_only_an_install_that_is_exactly_its_sources(presentmon_exe, tmp_path):
    import zipfile
    zpath = _zip(tmp_path / "r.zip", RUNTIME)
    install, expected, runtime, package = _installed(tmp_path, presentmon_exe)
    with zipfile.ZipFile(zpath) as z:
        check = mc.check_files(install, expected, runtime, package, z)
        assert check.ok, check.detail

        (install / "lagdoctor" / "clock.py").unlink()                        # an MSI missing a file
        check = mc.check_files(install, expected, runtime, package, z)
        assert not check.ok and "missing 1: lagdoctor/clock.py" in check.detail
        (install / "lagdoctor" / "clock.py").write_bytes(b"# not the checkout's\n")
        check = mc.check_files(install, expected, runtime, package, z)
        assert not check.ok and "lagdoctor/clock.py" in check.detail and "byte for byte" in check.detail

        import shutil
        shutil.copyfile(ROOT / "lagdoctor" / "clock.py", install / "lagdoctor" / "clock.py")
        (install / "lagdoctor" / "stray.py").write_bytes(b"")                 # something the inputs do not hold
        check = mc.check_files(install, expected, runtime, package, z)
        assert not check.ok and "not expected 1: lagdoctor/stray.py" in check.detail
        (install / "lagdoctor" / "stray.py").unlink()

        pm = install / mb.PRESENTMON_DIR / setup.EXE_NAME
        data = bytearray(pm.read_bytes())
        data[500_000] ^= 0xFF                                                # PresentMon no longer the pinned one
        pm.write_bytes(bytes(data))
        assert pm.read_bytes() != presentmon_exe.read_bytes()
        check = mc.check_files(install, expected, runtime, package, z)
        assert not check.ok and "PresentMon:" in check.detail
        assert not mc.check_files(tmp_path / "nowhere", expected, runtime, package, z).ok


def test_a_payload_unlike_its_inputs_stops_the_build(presentmon_exe, tmp_path, monkeypatch):
    _fake_compileall(monkeypatch)
    real = mb.expected_files
    monkeypatch.setattr(mb, "expected_files", lambda *a: real(*a) | {"lagdoctor/expected.py"})
    with pytest.raises(mb.BuildError, match=r"not what the inputs say: missing \['lagdoctor/expected.py'\]"):
        mb.stage(tmp_path / "p", _zip(tmp_path / "w.whl", WHEEL), _zip(tmp_path / "r.zip", RUNTIME), presentmon_exe)


def _readback(monkeypatch, tmp_path, *, props=None, summary=None, env=None, layout=None) -> Path:
    payload = tmp_path / "payload"
    for rel in ("python.exe", "lagdoctor/cli.py", "bin/lagdoctor.cmd"):
        (payload / rel).parent.mkdir(parents=True, exist_ok=True)
        (payload / rel).write_bytes(b"x")
    good_props = {"ProductName": "lag-doctor", "ProductVersion": "0.1.0", "Manufacturer": "lag-doctor",
                  "ProductCode": PRODUCT, "UpgradeCode": UPGRADE_CODE}
    monkeypatch.setattr(mb.msidb, "properties", lambda msi: {**good_props, **(props or {})})
    monkeypatch.setattr(mb.msidb, "summary", lambda msi: {"subject": "lag-doctor", "author": "lag-doctor",
                                                          **(summary or {})})
    monkeypatch.setattr(mb.msidb, "rows", lambda msi, table, cols: env if env is not None else [dict(mb.PATH_ROW)])
    monkeypatch.setattr(mb.msidb, "install_layout", lambda msi: layout if layout is not None else {
        "f1": "python.exe", "f2": "lagdoctor/cli.py", "f3": "bin/lagdoctor.cmd"})
    return payload


def test_the_built_msi_must_read_back_as_lag_doctor_with_its_payload_and_path_row(monkeypatch, tmp_path):
    payload = _readback(monkeypatch, tmp_path)
    got = mb.readback(tmp_path / "x.msi", payload, "0.1.0")
    assert got["upgrade_code"] == UPGRADE_CODE and got["files"] == 3
    bad = {"version": {"props": {"ProductVersion": "0.2.0"}},
           "name": {"props": {"ProductName": "lagdoctor"}},
           "publisher": {"props": {"Manufacturer": "Someone"}},
           "summary subject": {"summary": {"subject": "Installer"}},
           "summary author": {"summary": {"author": None}},
           "path row kept on uninstall": {"env": [{"Name": "=*PATH", "Value": "[~];[BINFOLDER]"}]},
           "path replaced": {"env": [{"Name": "=-*PATH", "Value": "[BINFOLDER]"}]},
           "no path row": {"env": []},
           "a file left out": {"layout": {"f1": "python.exe", "f2": "lagdoctor/cli.py"}},
           "a file added": {"layout": {"f1": "python.exe", "f2": "lagdoctor/cli.py", "f3": "bin/lagdoctor.cmd",
                                       "f4": "lagdoctor/extra.py"}},
           "a path twice": {"layout": {"f1": "python.exe", "f2": "lagdoctor/cli.py", "f3": "bin/lagdoctor.cmd",
                                       "f4": "Python.exe"}}}
    outcomes = {}
    for name, kw in bad.items():
        payload = _readback(monkeypatch, tmp_path / name.replace(" ", "-"), **kw)
        try:
            mb.readback(tmp_path / "x.msi", payload, "0.1.0")
            outcomes[name] = "passed"
        except mb.BuildError:
            outcomes[name] = "refused"
    assert outcomes == dict.fromkeys(bad, "refused")
