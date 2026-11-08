"""scripts/wheel_check.py: what CI's wheel job checks in the wheel `uv build` makes, and the build's pinned uv.

The real run (a real wheel in a fresh venv) is CI's wheel job; these tests pin each check's verdict on hand-written
inputs, so a check that stops failing turns them red.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import wheel_check as wc  # noqa: E402

INFO = "lag_doctor-0.1.0.dist-info"
TRACKED = ["lagdoctor/__init__.py", "lagdoctor/cli.py", "lagdoctor/fixes/__init__.py", "lagdoctor/fixes/power.py"]
WHEEL = ["lagdoctor/", "lagdoctor/fixes/", *TRACKED,
         f"{INFO}/", f"{INFO}/METADATA", f"{INFO}/RECORD", f"{INFO}/WHEEL", f"{INFO}/entry_points.txt",
         f"{INFO}/licenses/LICENSE"]


def test_the_dist_info_folder_follows_the_wheel_name():
    assert wc.dist_info("lag_doctor-0.1.0-py3-none-any.whl") == "lag_doctor-0.1.0.dist-info"
    assert wc.dist_info("lag_doctor-0.1.0-1-py3-none-any.whl") == "lag_doctor-0.1.0.dist-info"   # a build tag
    for bad in ("lag_doctor-0.1.0.tar.gz", "lag_doctor-0.1.0.whl"):
        with pytest.raises(wc.CannotStart):
            wc.dist_info(bad)


def test_a_complete_wheel_has_nothing_missing_or_extra():
    assert wc.compare_files(WHEEL, TRACKED, INFO) == ([], [])
    check = wc.files_check(WHEEL, TRACKED, INFO)
    assert check.ok and "4 tracked files" in check.detail


def test_a_module_left_out_of_the_wheel_is_missing():
    names = [n for n in WHEEL if n != "lagdoctor/fixes/power.py"]
    assert wc.compare_files(names, TRACKED, INFO) == (["lagdoctor/fixes/power.py"], [])
    check = wc.files_check(names, TRACKED, INFO)
    assert not check.ok and "lagdoctor/fixes/power.py" in check.detail


def test_files_outside_the_package_or_untracked_are_extra_but_the_metadata_is_not():
    names = [*WHEEL, "laglab/app.py", "tests/synth.py", "lagdoctor/stray.txt", "lag_doctor-0.1.0.data/x.json"]
    assert wc.compare_files(names, TRACKED, INFO) == (
        [], ["lag_doctor-0.1.0.data/x.json", "lagdoctor/stray.txt", "laglab/app.py", "tests/synth.py"])
    assert not wc.files_check(names, TRACKED, INFO).ok


def test_directory_entries_are_not_files():
    # A wheel's directory entries ("lagdoctor/fixes/") are never tracked, and must not count as extra files.
    assert wc.compare_files([*WHEEL, "lagdoctor/capture/"], TRACKED, INFO) == ([], [])


def test_module_names_are_the_import_names_without_main():
    names = [*WHEEL, "lagdoctor/__main__.py", "lagdoctor/report/html.py", "lagdoctor/report/__init__.py",
             "laglab/app.py", "lagdoctor/data.json"]
    assert wc.module_names(names) == ["lagdoctor", "lagdoctor.cli", "lagdoctor.fixes", "lagdoctor.fixes.power",
                                      "lagdoctor.report", "lagdoctor.report.html"]


def _imported(venv: Path, names: list[str], **override) -> dict:
    purelib = venv / "Lib" / "site-packages"
    mods = {n: {"file": str(purelib / (n.replace(".", "\\") + ".py"))} for n in names}
    mods.update(override)
    return {"purelib": str(purelib), "modules": mods}


def test_imports_pass_only_when_every_module_came_from_the_fresh_venv(tmp_path):
    venv = tmp_path / "venv"
    expected = ["lagdoctor", "lagdoctor.capture.inventory", "lagdoctor.cli"]
    ok = wc.judge_imports(_imported(venv, expected), expected, venv)
    assert ok.ok and "all 3 modules" in ok.detail

    missing_dep = _imported(venv, expected, **{
        "lagdoctor.capture.inventory": {"error": "ModuleNotFoundError: No module named 'psutil'"}})
    check = wc.judge_imports(missing_dep, expected, venv)
    assert not check.ok and "No module named 'psutil'" in check.detail

    checkout = _imported(venv, expected, **{"lagdoctor.cli": {"file": str(ROOT / "lagdoctor" / "cli.py")}})
    check = wc.judge_imports(checkout, expected, venv)
    assert not check.ok and "lagdoctor.cli came from" in check.detail

    unreported = _imported(venv, expected[:2])
    assert not wc.judge_imports(unreported, expected, venv).ok

    elsewhere = _imported(tmp_path / "another-venv", expected)       # files in its own site-packages, not ours
    check = wc.judge_imports(elsewhere, expected, venv)
    assert not check.ok and "is not in the fresh venv" in check.detail

    assert not wc.judge_imports(_imported(venv, []), [], venv).ok     # nothing to import is no pass


def test_help_and_version_must_exit_0_and_say_the_right_thing():
    assert wc.judge_help(0, "usage: lagdoctor [-h] [--version] COMMAND ...\n\nFind out why").ok
    assert not wc.judge_help(1, "usage: lagdoctor [-h] [--version] COMMAND ...\n").ok
    assert not wc.judge_help(0, "Traceback (most recent call last):\n").ok
    assert not wc.judge_help(None, "").ok

    assert wc.judge_version(0, "lag-doctor 0.1.0\n", "0.1.0").ok
    check = wc.judge_version(0, "lag-doctor 0.1.0\n", "0.2.0")              # __init__.py and pyproject disagree
    assert not check.ok and "0.2.0" in check.detail
    assert not wc.judge_version(2, "lag-doctor 0.1.0\n", "0.1.0").ok
    assert not wc.judge_version(0, "", "0.1.0").ok


def _report(home: Path, capture: Path, *, size: int = 20_000, head: str = "<!doctype html><html>",
            folder: str = "20261107-120000-python") -> Path:
    d = home / "sessions" / folder
    d.mkdir(parents=True)
    (d / "report.html").write_text(head + "x" * max(0, size - len(head)), encoding="utf-8")
    (d / "analyzed-capture.txt").write_text(str(capture) + "\n", encoding="utf-8")
    return d


def test_analyze_passes_only_with_one_full_report_about_this_capture(tmp_path):
    capture = tmp_path / "capture.ldcap.json.gz"
    capture.write_bytes(b"x")
    cases = {}
    for name, kw in {"good": {}, "small": {"size": 900}, "not html": {"head": "Traceback"},
                     "other capture": {}, "two folders": {}, "exit 1": {}}.items():
        home = tmp_path / name
        _report(home, tmp_path / "other.ldcap.json.gz" if name == "other capture" else capture, **kw)
        if name == "two folders":
            _report(home, capture, folder="20261107-120001-python")
        cases[name] = wc.judge_analyze(1 if name == "exit 1" else 0, "", home, capture)
    cases["no folder"] = wc.judge_analyze(0, "", tmp_path / "empty", capture)
    assert {name: c.ok for name, c in cases.items()} == {
        "good": True, "small": False, "not html": False, "other capture": False, "two folders": False,
        "exit 1": False, "no folder": False}


def test_the_exit_code_is_0_only_when_every_check_passed():
    passed, failed = wc.Check("files", True, ""), wc.Check("imports", False, "")
    assert wc.exit_code([passed, passed]) == 0
    assert wc.exit_code([passed, failed]) == 1
    assert wc.exit_code([]) == 1


def test_the_wheel_folder_must_hold_exactly_one_wheel(tmp_path):
    with pytest.raises(wc.CannotStart):
        wc.find_wheel(tmp_path)
    (tmp_path / "lag_doctor-0.1.0-py3-none-any.whl").write_bytes(b"")
    assert wc.find_wheel(tmp_path).name == "lag_doctor-0.1.0-py3-none-any.whl"
    (tmp_path / "lag_doctor-0.0.9-py3-none-any.whl").write_bytes(b"")          # a stale one beside it
    with pytest.raises(wc.CannotStart):
        wc.find_wheel(tmp_path)


def test_the_commands_get_no_python_variables_no_venv_and_no_checkout_on_path(tmp_path):
    root = tmp_path / "checkout"
    environ = {"PYTHONPATH": str(root), "PythonHome": "C:\\Python313", "VIRTUAL_ENV": str(root / ".venv"),
               "Path": ";".join([str(root / ".venv" / "Scripts"), "C:\\Windows\\system32", str(root), ""]),
               "LOCALAPPDATA": "C:\\Users\\someone\\AppData\\Local"}
    env = wc.clean_env(tmp_path / "home", environ, root)
    assert env == {"Path": "C:\\Windows\\system32", "LOCALAPPDATA": "C:\\Users\\someone\\AppData\\Local",
                   "LAGDOCTOR_HOME": str(tmp_path / "home")}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args], cwd=cwd, check=True,
                   capture_output=True)


def test_tracked_files_come_from_git_and_an_empty_listing_refuses(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "lagdoctor" / "fixes").mkdir(parents=True)
    (tmp_path / "lagdoctor" / "fixes" / "power.py").write_text("", encoding="utf-8")
    (tmp_path / "lagdoctor" / "untracked.py").write_text("", encoding="utf-8")
    with pytest.raises(wc.CannotStart):
        wc.tracked_files(tmp_path)                                    # nothing tracked yet
    _git(tmp_path, "add", "lagdoctor/fixes/power.py")
    assert wc.tracked_files(tmp_path) == ["lagdoctor/fixes/power.py"]
    with pytest.raises(wc.CannotStart):
        wc.tracked_files(tmp_path / "lagdoctor" / "fixes" / "power.py")   # not a folder git can run in


def test_every_check_runs_and_reports_even_after_one_failed(tmp_path, monkeypatch):
    """One failed check never hides the others: a sabotage that breaks two shows both."""
    dist = tmp_path / "dist"
    dist.mkdir()
    with zipfile.ZipFile(dist / "lag_doctor-0.1.0-py3-none-any.whl", "w") as z:
        for n in TRACKED:
            z.writestr(n, "")
        z.writestr(f"{INFO}/METADATA", "Metadata-Version: 2.4\nName: lag-doctor\nVersion: 0.1.0\n")
    monkeypatch.setattr(wc, "tracked_files", lambda: [*TRACKED, "lagdoctor/gone.py"])    # left out of the wheel
    monkeypatch.setattr(wc, "install", lambda wheel, venv, cwd, env: wc.Check("install", True, "faked"))
    monkeypatch.setattr(wc, "make_capture", lambda path: path)

    def fake_run(argv, cwd, env):
        if argv[1:] == ["--help"]:
            return 0, "usage: lagdoctor [-h] [--version] COMMAND ...\n", ""
        if argv[1:] == ["--version"]:
            return 0, "lag-doctor 0.1.0\n", ""
        if argv[1] == "-I":
            venv = Path(argv[0]).parents[1]
            names = json.loads(Path(argv[-2]).read_text(encoding="utf-8"))
            Path(argv[-1]).write_text(json.dumps(_imported(venv, names, **{
                "lagdoctor.fixes.power": {"error": "ModuleNotFoundError: No module named 'psutil'"}})), encoding="utf-8")
            return 0, "", ""
        assert argv[1] == "analyze"
        _report(Path(env["LAGDOCTOR_HOME"]), Path(argv[2]))
        return 0, "", ""
    monkeypatch.setattr(wc, "run", fake_run)
    _, _, checks = wc.run_checks(dist)
    assert {c.name: c.ok for c in checks} == {"files": False, "install": True, "help": True, "version": True,
                                             "imports": False, "analyze": True}
    assert [c.name for c in checks] == ["files", "install", "help", "version", "imports", "analyze"]

    monkeypatch.setattr(wc, "install", lambda wheel, venv, cwd, env: wc.Check("install", False, "faked"))
    _, _, checks = wc.run_checks(dist)
    assert [(c.name, c.ok) for c in checks] == [("files", False), ("install", False), ("help", False),
                                               ("version", False), ("imports", False), ("analyze", False)]


def _bounds(requirement: str) -> tuple[tuple[int, ...], tuple[int, ...]]:
    m = re.fullmatch(r"uv_build>=([\d.]+),<([\d.]+)", requirement.replace(" ", ""))
    assert m, f"pyproject.toml's build requirement {requirement!r} is not uv_build>=X,<Y"
    return tuple(map(int, m[1].split("."))), tuple(map(int, m[2].split(".")))


def test_the_uv_that_ci_builds_with_carries_a_uv_build_the_project_accepts():
    """CI builds with `uv build --offline`: only the uv_build copy inside the pinned uv can satisfy the bound."""
    build = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["build-system"]
    assert build["build-backend"] == "uv_build" and len(build["requires"]) == 1
    low, high = _bounds(build["requires"][0])
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    pins = re.findall(r"astral-sh/setup-uv@\S+.*\n\s+with:\n\s+version: \"([\d.]+)\"", ci)
    assert len(pins) == ci.count("astral-sh/setup-uv@") >= 2, "every setup-uv step in ci.yml pins its uv version"
    for pin in pins:
        assert low <= tuple(map(int, pin.split("."))) < high, (
            f"ci.yml pins uv {pin}, outside pyproject.toml's {build['requires'][0]}: change both together")
    assert re.search(r"^\s+run: uv build --offline ", ci, re.M), "CI's build must not fetch a build backend"


def test_the_bound_reader_refuses_what_it_cannot_read():
    assert _bounds("uv_build>=0.10.4,<0.11") == ((0, 10, 4), (0, 11))
    with pytest.raises(AssertionError):
        _bounds("hatchling>=1.27")
