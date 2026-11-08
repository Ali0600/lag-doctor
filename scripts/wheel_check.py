"""Install the wheel `uv build` made into a fresh venv and run it there, outside the checkout.

Usage:  uv run python scripts/wheel_check.py DIST_DIR

DIST_DIR holds exactly one lag-doctor wheel (`uv build --out-dir DIST_DIR`). CI's wheel job runs this on every
change (.github/workflows/ci.yml). Every check runs and reports on its own; the exit status is 0 only if all of
them passed, 1 if any failed, and 2 if they could not start (no single wheel, no git, no uv).

- files: the wheel holds exactly the files git tracks under lagdoctor/, plus its own .dist-info. A module or data
  file left out fails here even when no command imports it (`analyze` leaves most of capture/, fixes/ and winapi/
  unimported, and the fix catalog finds its fixes by listing its own folder); laglab/, tests/ or scripts/ let in
  fail here too.
- install: a new venv, with the wheel installed into it, and the dependencies the wheel declares, nothing else.
- help, version: the installed `lagdoctor` command starts, and names the wheel's own version.
- imports: every module in the wheel imports in that venv, from its site-packages, so a dependency the wheel does
  not declare fails here.
- analyze: `lagdoctor analyze` on a synthetic capture (tests/synth.py, LagLab's disk-hitch scenario) exits 0 and
  writes its report.

The venv, the capture, lag-doctor's home (LAGDOCTOR_HOME) and every command's working folder are in a new temp
folder outside the checkout, and the commands run without PYTHON* variables, so none of them can import the
checkout's own copy of lag-doctor.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass
from email.parser import HeaderParser
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "lagdoctor"                # the import package the wheel ships
COMMAND = "lagdoctor"                # its console script ([project.scripts] in pyproject.toml)
CAPTURE_MODE = "hitch-io"            # LagLab's disk-hitch scenario, as in the README's picture
REPORT_NAME = "report.html"          # what `analyze` writes into its session folder
ANALYZED_FROM = "analyzed-capture.txt"
MIN_REPORT_BYTES = 10_000            # the disk-hitch report is about 42 KB; an error page would be far smaller
TIMEOUT_S = 300
LATER = ("help", "version", "imports", "analyze")     # the checks that need the installed wheel

# Run by the fresh venv's python with -I: argv[1] names the modules to import (JSON), argv[2] gets the result.
IMPORT_CHILD = r'''
import importlib, json, sys, sysconfig
with open(sys.argv[1], encoding="utf-8") as f:
    names = json.load(f)
out = {}
for name in names:
    try:
        out[name] = {"file": importlib.import_module(name).__file__}
    except BaseException as exc:        # SystemExit too: importing a module must not run anything
        out[name] = {"error": f"{type(exc).__name__}: {exc}"}
with open(sys.argv[2], "w", encoding="utf-8") as f:
    json.dump({"purelib": sysconfig.get_path("purelib"), "modules": out}, f)
'''


class CannotStart(Exception):
    """The checks cannot run at all (exit 2)."""


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


# ------------------------------------------------------------------------ pure parts ---

def dist_info(wheel_name: str) -> str:
    """`lag_doctor-0.1.0-py3-none-any.whl` -> `lag_doctor-0.1.0.dist-info` (the wheel format's naming)."""
    parts = wheel_name[:-len(".whl")].split("-") if wheel_name.endswith(".whl") else []
    if len(parts) not in (5, 6):
        raise CannotStart(f"{wheel_name!r} is not a wheel's file name")
    return f"{parts[0]}-{parts[1]}.dist-info"


def compare_files(names: Iterable[str], tracked: Iterable[str], info: str) -> tuple[list[str], list[str]]:
    """(missing, extra): tracked files the wheel lacks, and the wheel's files that are neither tracked nor its own
    metadata. A directory entry ("lagdoctor/fixes/") is not a file."""
    files = {n for n in names if not n.endswith("/")}
    want = set(tracked)
    meta = {n for n in files if n.startswith(info + "/")}
    return sorted(want - files), sorted(files - want - meta)


def module_names(names: Iterable[str]) -> list[str]:
    """Import names of the package's .py files. `__main__` is left out: importing it runs the command."""
    out = set()
    for name in names:
        p = PurePosixPath(name)
        if p.suffix != ".py" or p.parts[0] != PACKAGE:
            continue
        parts = list(p.with_suffix("").parts)
        if parts[-1] == "__main__":
            continue
        if parts[-1] == "__init__":
            parts.pop()
        out.add(".".join(parts))
    return sorted(out)


def _norm(path: str | Path) -> str:
    return os.path.normcase(os.path.realpath(path))


def _inside(path: str | Path | None, top: str | Path) -> bool:
    if not path:
        return False
    p, t = _norm(path), _norm(top)
    return p.startswith(t.rstrip(os.sep) + os.sep)


def files_check(names: Iterable[str], tracked: Iterable[str], info: str) -> Check:
    missing, extra = compare_files(names, tracked, info)
    if missing or extra:
        parts = [f"{what} {len(paths)}: {', '.join(paths[:8])}{' ...' if len(paths) > 8 else ''}"
                 for what, paths in (("missing", missing), ("not tracked under lagdoctor/", extra)) if paths]
        return Check("files", False, "; ".join(parts))
    return Check("files", True, f"all {len(set(tracked))} tracked files under {PACKAGE}/, nothing else")


def judge_help(code: int | None, out: str) -> Check:
    first = out.strip().splitlines()[0] if out.strip() else ""
    if code != 0 or not first.startswith(f"usage: {COMMAND}"):
        return Check("help", False, f"exit code {code}, first line {first[:120]!r}")
    return Check("help", True, first[:120])


def judge_version(code: int | None, out: str, version: str) -> Check:
    said = out.strip()
    if code != 0 or said.split()[-1:] != [version]:
        return Check("version", False, f"exit code {code}, said {said[:120]!r}; the wheel is version {version}")
    return Check("version", True, said)


def judge_imports(result: dict[str, Any], expected: list[str], venv: Path) -> Check:
    """Every expected module imported, each from the fresh venv's site-packages."""
    if not expected:
        return Check("imports", False, f"the wheel has no {PACKAGE} modules to import")
    mods = result.get("modules") or {}
    purelib = result.get("purelib") or ""
    problems = [] if _inside(purelib, venv) else [f"its site-packages {purelib!r} is not in the fresh venv {venv}"]
    for name in expected:
        m = mods.get(name) or {}
        if "error" in m:
            problems.append(f"{name}: {m['error']}")
        elif not _inside(m.get("file"), purelib):
            problems.append(f"{name} came from {m.get('file')!r}")
    if problems:
        return Check("imports", False, "; ".join(problems[:8]) + (" ..." if len(problems) > 8 else ""))
    return Check("imports", True, f"all {len(expected)} modules, from {purelib}")


def judge_analyze(code: int | None, out: str, home: Path, capture: Path) -> Check:
    if code != 0:
        return Check("analyze", False, f"exit code {code}: {out.strip()[-600:]!r}")
    sessions = home / "sessions"
    folders = sorted(p for p in sessions.iterdir() if p.is_dir()) if sessions.is_dir() else []
    if len(folders) != 1:
        return Check("analyze", False, f"{len(folders)} report folders under {sessions}, not 1")
    report, origin = folders[0] / REPORT_NAME, folders[0] / ANALYZED_FROM
    size = report.stat().st_size if report.is_file() else 0
    if size < MIN_REPORT_BYTES:
        return Check("analyze", False, f"{report} has {size} bytes, under {MIN_REPORT_BYTES}")
    with open(report, "rb") as f:
        head = f.read(64).lower()
    if not head.startswith(b"<!doctype html"):
        return Check("analyze", False, f"{report} does not start as an HTML page: {head[:40]!r}")
    named = origin.read_text(encoding="utf-8").strip() if origin.is_file() else ""
    if not named or _norm(named) != _norm(capture):
        return Check("analyze", False, f"{origin} names {named!r}, not the capture {capture}")
    return Check("analyze", True, f"{REPORT_NAME} of {size:,} bytes for a {CAPTURE_MODE} capture")


def exit_code(checks: list[Check]) -> int:
    return 0 if checks and all(c.ok for c in checks) else 1


# ------------------------------------------------------------------- the real runs ---

def find_wheel(dist: Path) -> Path:
    wheels = sorted(dist.glob("*.whl")) if dist.is_dir() else []
    if len(wheels) != 1:
        raise CannotStart(f"{dist} holds {len(wheels)} wheels, not 1: build into an empty folder "
                          "(uv build --clear --out-dir DIST_DIR)")
    return wheels[0]


def wheel_version(z: zipfile.ZipFile, info: str) -> str:
    try:
        text = z.read(f"{info}/METADATA").decode("utf-8")
    except KeyError:
        raise CannotStart(f"the wheel has no {info}/METADATA") from None
    version = (HeaderParser().parsestr(text).get("Version") or "").strip()
    if not version:
        raise CannotStart(f"{info}/METADATA names no version")
    return version


def tracked_files(root: Path = ROOT) -> list[str]:
    """What git tracks under lagdoctor/, named as the wheel names files (forward slashes)."""
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--", PACKAGE], capture_output=True,
                             check=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise CannotStart(f"git cannot list the files tracked under {PACKAGE}/: {exc}") from None
    files = [f for f in out.decode("utf-8").split("\0") if f]
    if not files:
        raise CannotStart(f"git tracks no files under {PACKAGE}/ in {root}")
    return files


def clean_env(home: Path, environ: dict[str, str] | None = None, root: Path = ROOT) -> dict[str, str]:
    """`environ` without PYTHON* variables, the checkout's venv or any PATH folder in the checkout, and with
    lag-doctor's home in the temp folder."""
    environ = dict(os.environ) if environ is None else environ
    env = {k: v for k, v in environ.items() if not k.upper().startswith("PYTHON") and k.upper() != "VIRTUAL_ENV"}
    for key in [k for k in env if k.upper() == "PATH"]:
        env[key] = os.pathsep.join(p for p in env[key].split(os.pathsep)
                                   if p and not _inside(p, root) and _norm(p) != _norm(root))
    env["LAGDOCTOR_HOME"] = str(home)
    return env


def run(argv: list[str], cwd: Path, env: dict[str, str]) -> tuple[int | None, str, str]:
    """(exit code or None, stdout, stderr)."""
    try:
        p = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=TIMEOUT_S)
    except OSError as exc:
        return None, "", f"could not start {argv[0]}: {exc}"
    except subprocess.TimeoutExpired:
        return None, "", f"{argv[0]} ran past {TIMEOUT_S} s and was stopped"
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


def install(wheel: Path, venv: Path, cwd: Path, env: dict[str, str]) -> Check:
    uv = env.get("UV") or shutil.which("uv")
    if not uv:
        raise CannotStart("uv is not on PATH")
    python = f"{sys.version_info.major}.{sys.version_info.minor}"
    for argv in ([uv, "venv", "--no-project", "--no-config", "--python", python, str(venv)],
                 [uv, "pip", "install", "--no-config", "--python", str(venv / "Scripts" / "python.exe"), str(wheel)]):
        code, out, err = run(argv, cwd, env)
        if code != 0:
            return Check("install", False, f"{' '.join(argv[1:3])} exit code {code}: {(out + err).strip()[-600:]!r}")
    exe = venv / "Scripts" / f"{COMMAND}.exe"
    if not exe.is_file():
        return Check("install", False, f"installed, but there is no {exe.name}: the wheel declares no "
                                       f"{COMMAND} command")
    return Check("install", True, f"Python {python} venv in {venv}")


def make_capture(path: Path) -> Path:
    """A synthetic capture from the tests' own model of LagLab, made by the checkout's code (the input only)."""
    sys.path.insert(0, str(ROOT))
    from tests import synth
    return synth.laglab(CAPTURE_MODE).cap.save(path)


def run_checks(dist: Path) -> tuple[Path, list[str], list[Check]]:
    wheel = find_wheel(dist)
    info = dist_info(wheel.name)
    with zipfile.ZipFile(wheel) as z:
        names = z.namelist()
        version = wheel_version(z, info)
    tracked = tracked_files()
    tmp = Path(tempfile.mkdtemp(prefix="wheel-check-")).resolve()
    try:
        if _inside(tmp, ROOT):
            raise CannotStart(f"the temp folder {tmp} is inside the checkout {ROOT}; point TEMP somewhere else")
        venv, work, home = tmp / "venv", tmp / "work", tmp / "home"
        work.mkdir()
        home.mkdir()
        env = clean_env(home)
        checks = [files_check(names, tracked, info), install(wheel, venv, work, env)]
        if not checks[-1].ok:
            return wheel, names, checks + [Check(n, False, "not run: the wheel did not install") for n in LATER]
        exe = str(venv / "Scripts" / f"{COMMAND}.exe")
        code, out, err = run([exe, "--help"], work, env)
        checks.append(judge_help(code, out or err))
        code, out, err = run([exe, "--version"], work, env)
        checks.append(judge_version(code, out or err, version))
        expected = module_names(names)
        asked, answer = tmp / "modules.json", tmp / "imported.json"
        asked.write_text(json.dumps(expected), encoding="utf-8")
        python = str(venv / "Scripts" / "python.exe")
        code, out, err = run([python, "-I", "-c", IMPORT_CHILD, str(asked), str(answer)], work, env)
        try:
            result = json.loads(answer.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            result = None
        if code == 0 and isinstance(result, dict):
            checks.append(judge_imports(result, expected, venv))
        else:
            checks.append(Check("imports", False, f"the import run ended with exit code {code}: "
                                                  f"{(out + err).strip()[-600:]!r}"))
        try:
            capture = make_capture(tmp / "capture.ldcap.json.gz")
        except Exception as exc:                  # the checkout's own model, not the wheel: still a failure
            checks.append(Check("analyze", False, f"not run: the capture could not be made: {exc!r}"))
            return wheel, names, checks
        code, out, err = run([exe, "analyze", str(capture)], work, env)
        checks.append(judge_analyze(code, out + err, home, capture))
        return wheel, names, checks
    finally:
        _remove(tmp)


def _remove(path: Path) -> None:
    """Delete the temp folder; a process that just ended may hold a file there for a moment."""
    deadline = time.monotonic() + 10
    while path.exists():
        try:
            shutil.rmtree(path)
        except OSError:
            if time.monotonic() > deadline:
                print(f"note: could not delete {path}", file=sys.stderr)
                return
            time.sleep(0.25)


def summary(wheel: Path, names: list[str], checks: list[Check]) -> str:
    files = [n for n in names if not n.endswith("/")]
    rows = "\n".join(f"| {c.name} | {'pass' if c.ok else '**FAIL**'} | "
                     f"{' '.join(c.detail.split()).replace('|', '/')} |" for c in checks)
    return (f"### Wheel: {wheel.name}\n\n{len(files)} files, {wheel.stat().st_size:,} bytes, installed and run in a "
            f"fresh venv outside the checkout.\n\n| Check | Result | Detail |\n|---|---|---|\n{rows}\n")


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    try:
        wheel, names, checks = run_checks(Path(argv[0]))
    except CannotStart as exc:
        print(f"the wheel check cannot start: {exc}", file=sys.stderr)
        return 2
    print(f"{wheel.name}:")
    for n in sorted(names):
        print(f"  {n}")
    for c in checks:
        print(f"{'PASS' if c.ok else 'FAIL'} {c.name:8} {c.detail}")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(summary(wheel, names, checks))
    return exit_code(checks)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
