r"""Build lag-doctor's per-machine MSI with WiX v7 (docs/INSTALLER-PLAN.md, Stage 3).

Usage:
  uv run python scripts/msi_build.py --wheel WHEEL --runtime-zip ZIP --presentmon EXE --wix WIX --out OUT_DIR
                                     [--accept-eula EULA_ID]

CI's msi job runs this, then scripts/msi_check.py installs, runs and uninstalls the result. It never installs
anything itself, so it runs anywhere.

1. The payload: a new folder laid out as the MSI installs it in C:\Program Files\lag-doctor (layout below), each
   part checked before it goes in:
   - python.org's embeddable runtime, unchanged (scripts/runtime_pin.py: size and SHA-256; downloaded to ZIP first
     when ZIP does not exist). Its python313._pth gives sys.path the runtime's ZIP and its own folder only, which is
     where lagdoctor\ sits;
   - lagdoctor\ from the wheel (the wheel job checks that it holds exactly the files git tracks), with bytecode that
     runtime compiles as unchecked-hash .pyc files: Python then never writes a __pycache__ of its own there, which an
     administrator's run would otherwise do and the uninstall would leave behind;
   - PresentMon\PresentMon-2.6.0-x64.exe, checked against lag-doctor's own pin and Intel's signature before and after
     the copy, and Intel's licence text beside it (installer/PresentMon-LICENSE.txt, pinned here by SHA-256);
   - bin\lagdoctor.cmd, the one thing in the folder that goes on PATH (python.exe must not: it would stand in for
     the user's own `python`);
   - lag-doctor's licence as LICENSE-lag-doctor.txt (the runtime's own LICENSE.txt is Python's).
   The finished payload must hold exactly expected_files(), no more and no less.
2. `wix build` of installer/lag-doctor.wxs with the payload bound and the wheel's version.
3. The MSI read back: its product name, version and upgrade code, and that its File table puts exactly the payload's
   files where the payload has them.

Writes OUT_DIR\lag-doctor-<version>-x64.msi and OUT_DIR\payload.json (every payload file's size and SHA-256).
Exit codes: 0 built and read back; 1 a check failed; 2 cannot start (an argument or tool missing, OUT_DIR not empty).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "scripts"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import msidb  # noqa: E402
from runtime_pin import EMBED_PTH, EMBED_SHA256, EMBED_SIZE, EMBED_URL  # noqa: E402

PRODUCT = "lag-doctor"                       # the MSI's ProductName and the folder under Program Files
PACKAGE = "lagdoctor"
WXS = ROOT / "installer" / "lag-doctor.wxs"
SHIM_SOURCE = ROOT / "installer" / "lagdoctor.cmd"
INTEL_LICENSE_SOURCE = ROOT / "installer" / "PresentMon-LICENSE.txt"
# Intel's LICENSE.txt at PresentMon's v2.6.0 commit (e13fce6acdb5), 1,067 bytes with LF line endings.
INTEL_LICENSE_SHA256 = "4c949341b1893c8c6ad82f7fb4eedf622cd1fd9c22a9af8f19b2dac19d1947b6"
OWN_LICENSE_SOURCE = ROOT / "LICENSE"
# The layout under the install folder, in the payload's own terms (forward slashes).
SHIM = "bin/lagdoctor.cmd"
PRESENTMON_DIR = "PresentMon"
INTEL_LICENSE = f"{PRESENTMON_DIR}/LICENSE.txt"
OWN_LICENSE = "LICENSE-lag-doctor.txt"
PYC_UNCHECKED_HASH = 0b01                    # PEP 552 flags: hash-based, source never checked
WIX_TIMEOUT_S = 600
RETRY_WAITS_S = (0, 20, 60)


class BuildError(Exception):
    """A check failed (exit 1)."""


class CannotStart(Exception):
    """The build cannot run at all (exit 2)."""


# ------------------------------------------------------------------------ pure parts ---

def msi_version(version: str) -> str:
    """The wheel's version as an MSI ProductVersion: three numbers, major and minor at most 255, build at most 65535
    (Windows Installer's limits). Anything else (a pre-release, a local version) is refused rather than mangled."""
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    if not m or int(m[1]) > 255 or int(m[2]) > 255 or int(m[3]) > 65535:
        raise BuildError(f"the version {version!r} is not X.Y.Z within 255.255.65535, which an MSI needs")
    return version


def plain_name(name: str) -> bool:
    """An entry of the embeddable ZIP: a file name in its top folder, with nothing a path could hide in."""
    return bool(re.fullmatch(r"[A-Za-z0-9_.+-]+", name)) and name not in (".", "..")


def package_files(names: Iterable[str]) -> list[str]:
    """The wheel's files under lagdoctor/ (directory entries and its .dist-info left out). A name that could land
    outside the payload (absolute, a backslash, '..') is refused."""
    out = []
    for name in names:
        if name.endswith("/") or not name.startswith(PACKAGE + "/"):
            continue
        # Split by hand: PurePosixPath folds "a//b" and "a/./b" away, hiding the very parts this refuses.
        if "\\" in name or ":" in name or any(part in ("", ".", "..") for part in name.split("/")):
            raise BuildError(f"the wheel holds the unsafe name {name!r}")
        out.append(name)
    return sorted(out)


def pyc_path(py: str, cache_tag: str) -> str:
    """Where Python looks for a module's bytecode: lagdoctor/x/y.py -> lagdoctor/x/__pycache__/y.<tag>.pyc."""
    p = PurePosixPath(py)
    return str(p.parent / "__pycache__" / f"{p.stem}.{cache_tag}.pyc")


def expected_files(runtime_names: Iterable[str], package: Iterable[str], cache_tag: str) -> set[str]:
    """Every file the install folder must hold, from the inputs alone: the runtime ZIP's entries, the package's files
    and one .pyc per .py, PresentMon with Intel's licence, the PATH shim and lag-doctor's licence."""
    from lagdoctor import setup
    package = list(package)
    return ({*runtime_names, *package, *(pyc_path(p, cache_tag) for p in package if p.endswith(".py")),
             f"{PRESENTMON_DIR}/{setup.EXE_NAME}", INTEL_LICENSE, SHIM, OWN_LICENSE})


def compare(found: Iterable[str], expected: Iterable[str]) -> tuple[list[str], list[str]]:
    """(missing, extra), each sorted. Compared case-insensitively, as Windows names files."""
    have = {f.lower(): f for f in found}
    want = {e.lower(): e for e in expected}
    return sorted(want[k] for k in want.keys() - have.keys()), sorted(have[k] for k in have.keys() - want.keys())


def pyc_problem(header: bytes, magic: bytes) -> str | None:
    """What is wrong with a .pyc's 16-byte header for the shipped runtime, or None: its magic number must be that
    runtime's, and its flags unchecked-hash (PEP 552)."""
    if len(header) < 16:
        return f"only {len(header)} bytes"
    if header[:4] != magic:
        return f"magic {header[:4].hex()}, not the runtime's {magic.hex()}"
    flags = int.from_bytes(header[4:8], "little")
    if flags != PYC_UNCHECKED_HASH:
        return f"flags {flags:#x}, not unchecked-hash ({PYC_UNCHECKED_HASH:#x})"
    return None


def wix_command(wix: str, version: str, payload: Path, out_msi: Path, accept_eula: str | None) -> list[str]:
    """The `wix build` command line: x64 (64-bit components, as Program Files needs), every warning an error (a
    harvest that found no files, a missing folder, a duplicate). The EULA is accepted only when the caller passes its
    id: that is the owner's call (DECISIONS 2026-11-08), never a default here."""
    argv = [wix, "build", str(WXS), "-arch", "x64", "-wx", "-d", f"Version={version}", "-bindpath",
            f"payload={payload}", "-o", str(out_msi)]
    return argv + (["-acceptEula", accept_eula] if accept_eula else [])


def validate_command(wix: str, msi: Path, accept_eula: str | None) -> list[str]:
    """`wix msi validate`: Windows Installer's ICE checks, which `wix build` does not run. WIX1105 (validation could
    not run here) would otherwise pass as a warning, so it fails the build."""
    return [wix, "msi", "validate", "-wx1105", str(msi)] + (["-acceptEula", accept_eula] if accept_eula else [])


# The Environment row WiX writes for Part="last", Action="set", System="yes", Permanent="no": "=" set on install,
# "-" removed on uninstall, "*" the system's; "[~];" appends to the PATH already there (Windows Installer's
# Environment table).
PATH_ROW = {"Name": "=-*PATH", "Value": "[~];[BINFOLDER]"}


def tree_files(top: Path) -> list[str]:
    """Every file under top, as forward-slash paths relative to it."""
    return sorted(p.relative_to(top).as_posix() for p in top.rglob("*") if p.is_file())


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------------ the build ---

def runtime_zip(path: Path) -> Path:
    """The pinned embeddable ZIP at path, downloaded there first (over HTTPS, size-capped, three tries) when it is
    missing."""
    from lagdoctor import setup
    for wait in RETRY_WAITS_S if not path.is_file() else ():
        time.sleep(wait)
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_name(path.name + ".part")
        try:
            with open(part, "wb") as f:
                setup.fetch(EMBED_URL, f, EMBED_SIZE)
            os.replace(part, path)
            break
        except (OSError, setup.SetupError) as exc:
            print(f"::warning::the runtime download failed: {exc}", flush=True)
        finally:
            part.unlink(missing_ok=True)
    if not path.is_file():
        raise BuildError(f"the runtime ZIP could not be downloaded from {EMBED_URL}")
    size, digest = path.stat().st_size, sha256_file(path)
    if size != EMBED_SIZE or digest != EMBED_SHA256:
        raise BuildError(f"{path} is {size} bytes with SHA-256 {digest}; the pin is {EMBED_SIZE} and {EMBED_SHA256}")
    return path


def runtime_facts(python: Path) -> dict:
    """The payload runtime's own cache tag, bytecode magic and version, asked of it."""
    code = ("import importlib.util, json, sys; print(json.dumps({'tag': sys.implementation.cache_tag, "
            "'magic': importlib.util.MAGIC_NUMBER.hex(), 'version': sys.version.split()[0]}))")
    p = subprocess.run([str(python), "-c", code], capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        raise BuildError(f"the payload's {python.name} would not run: {p.stderr.strip()[-400:]}")
    return json.loads(p.stdout)


def compile_bytecode(python: Path, payload: Path, package: list[str], facts: dict) -> None:
    """Unchecked-hash .pyc files for the package, made by the runtime that will load them, each header checked."""
    install_root = r"C:\Program Files" + "\\" + PRODUCT          # tracebacks name the installed path, not this one
    p = subprocess.run([str(python), "-m", "compileall", "-q", "--invalidation-mode", "unchecked-hash",
                        "-s", str(payload), "-p", install_root, str(payload / PACKAGE)],
                       capture_output=True, text=True, timeout=600)
    if p.returncode != 0:
        raise BuildError(f"compileall failed (exit {p.returncode}): {(p.stdout + p.stderr).strip()[-600:]}")
    magic = bytes.fromhex(facts["magic"])
    for py in (f for f in package if f.endswith(".py")):
        pyc = payload / pyc_path(py, facts["tag"])
        try:
            with open(pyc, "rb") as f:
                problem = pyc_problem(f.read(16), magic)
        except OSError as exc:
            problem = f"missing ({exc})"
        if problem:
            raise BuildError(f"the bytecode for {py}: {problem}")


def stage(payload: Path, wheel: Path, zip_path: Path, presentmon: Path) -> dict:
    """Lay the payload out in the new folder `payload`, checking each part; what went in."""
    from lagdoctor import setup
    payload.mkdir(parents=False)
    with zipfile.ZipFile(zip_path) as z:
        runtime_names = z.namelist()
        bad = [n for n in runtime_names if not plain_name(n)]
        if bad or EMBED_PTH not in runtime_names:
            raise BuildError(f"the runtime ZIP holds names that are not plain file names ({bad}) or no {EMBED_PTH}")
        for n in runtime_names:
            with z.open(n) as src, open(payload / n, "xb") as dst:
                shutil.copyfileobj(src, dst)
    with zipfile.ZipFile(wheel) as z:
        package = package_files(z.namelist())
        if not package:
            raise BuildError(f"{wheel.name} holds no {PACKAGE}/ files")
        for n in package:
            dest = payload / PurePosixPath(n)
            dest.parent.mkdir(parents=True, exist_ok=True)
            with z.open(n) as src, open(dest, "xb") as dst:
                shutil.copyfileobj(src, dst)
    facts = runtime_facts(payload / "python.exe")
    compile_bytecode(payload / "python.exe", payload, package, facts)
    pm = payload / PRESENTMON_DIR / setup.EXE_NAME
    pm.parent.mkdir()
    shutil.copyfile(presentmon, pm)
    setup.verify(pm)                       # the bytes that will ship: lag-doctor's pin and Intel's signature
    if sha256_file(INTEL_LICENSE_SOURCE) != INTEL_LICENSE_SHA256:
        raise BuildError(f"{INTEL_LICENSE_SOURCE} is not Intel's licence text byte for byte (its pin moved, or git "
                         "converted its line endings)")
    shutil.copyfile(INTEL_LICENSE_SOURCE, payload / PurePosixPath(INTEL_LICENSE))
    (payload / "bin").mkdir()
    shutil.copyfile(SHIM_SOURCE, payload / PurePosixPath(SHIM))
    shutil.copyfile(OWN_LICENSE_SOURCE, payload / OWN_LICENSE)
    missing, extra = compare(tree_files(payload), expected_files(runtime_names, package, facts["tag"]))
    if missing or extra:
        raise BuildError(f"the payload is not what the inputs say: missing {missing[:8]}, extra {extra[:8]}")
    return {"runtime": facts, "runtime_files": len(runtime_names), "package_files": len(package)}


def wheel_version(wheel: Path) -> str:
    from email.parser import HeaderParser
    with zipfile.ZipFile(wheel) as z:
        meta = [n for n in z.namelist() if n.endswith(".dist-info/METADATA")]
        if len(meta) != 1:
            raise CannotStart(f"{wheel.name} has {len(meta)} METADATA files, not 1")
        version = HeaderParser().parsestr(z.read(meta[0]).decode("utf-8")).get("Version") or ""
    return version.strip()


def readback(msi: Path, payload: Path, version: str) -> dict:
    """The built MSI, read back: its identity (the product name and version SignPath's rules will hold every signed
    file to, in its properties and its summary), its PATH row, and its File table against the payload."""
    props = msidb.properties(str(msi))
    info = msidb.summary(str(msi))
    problems = []
    for key, got, want in (("ProductName", props.get("ProductName"), PRODUCT),
                           ("ProductVersion", props.get("ProductVersion"), version),
                           ("Manufacturer", props.get("Manufacturer"), PRODUCT),
                           ("the summary's Subject", info.get("subject"), PRODUCT),
                           ("the summary's Author", info.get("author"), PRODUCT)):
        if got != want:
            problems.append(f"{key} is {got!r}, not {want!r}")
    env = [{k: r[k] for k in ("Name", "Value")} for r in msidb.rows(str(msi), "Environment", ("Name", "Value"))]
    if env != [PATH_ROW]:
        problems.append(f"its Environment table is {env}, not just {PATH_ROW}")
    layout = msidb.install_layout(str(msi))
    missing, extra = compare(layout.values(), tree_files(payload))
    if missing or extra:
        problems.append(f"its File table leaves out {missing[:8]} and adds {extra[:8]}")
    if len(set(v.lower() for v in layout.values())) != len(layout):
        problems.append("its File table installs one path twice")
    if problems:
        raise BuildError("the MSI read back wrong: " + "; ".join(problems))
    return {"product_code": props.get("ProductCode"), "upgrade_code": props.get("UpgradeCode"),
            "manufacturer": props.get("Manufacturer"), "files": len(layout)}


def build(args: argparse.Namespace) -> dict:
    out = Path(args.out).resolve()
    if out.exists() and any(out.iterdir()):
        raise CannotStart(f"{out} is not empty")
    for name, path in (("--wheel", args.wheel), ("--presentmon", args.presentmon), ("--wix", args.wix)):
        if not Path(path).is_file():
            raise CannotStart(f"{name} {path} is not a file")
    version = msi_version(wheel_version(Path(args.wheel)))
    zip_path = runtime_zip(Path(args.runtime_zip).resolve())
    work = Path(tempfile.mkdtemp(prefix="msi-build-")).resolve()
    try:
        payload = work / "payload"
        staged = stage(payload, Path(args.wheel), zip_path, Path(args.presentmon))
        out.mkdir(parents=True, exist_ok=True)
        msi = out / f"{PRODUCT}-{version}-x64.msi"
        argv = wix_command(str(Path(args.wix).resolve()), version, payload, msi, args.accept_eula)
        print("+", subprocess.list2cmdline(argv), flush=True)
        p = subprocess.run(argv, cwd=work, timeout=WIX_TIMEOUT_S)
        if p.returncode != 0 or not msi.is_file():
            raise BuildError(f"wix build failed with exit code {p.returncode}")
        argv = validate_command(str(Path(args.wix).resolve()), msi, args.accept_eula)
        print("+", subprocess.list2cmdline(argv), flush=True)
        p = subprocess.run(argv, cwd=work, timeout=WIX_TIMEOUT_S)
        if p.returncode != 0:
            raise BuildError(f"wix msi validate failed with exit code {p.returncode}")
        identity = readback(msi, payload, version)
        manifest = {"msi": msi.name, "version": version, **identity, **staged,
                    "files": {f: {"size": (payload / f).stat().st_size, "sha256": sha256_file(payload / f)}
                              for f in tree_files(payload)}}
        (out / "payload.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
        return manifest
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--wheel", required=True, help="the wheel `uv build` made")
    ap.add_argument("--runtime-zip", required=True, help="the embeddable ZIP (downloaded there when missing)")
    ap.add_argument("--presentmon", required=True, help="a PresentMon 2.6.0 exe that passes lag-doctor's check")
    ap.add_argument("--wix", required=True, help="wix.exe (WiX v7)")
    ap.add_argument("--out", required=True, help="an empty or new folder for the MSI and payload.json")
    ap.add_argument("--accept-eula", metavar="EULA_ID", help="accept WiX's EULA for this build (its id, e.g. wix7)")
    args = ap.parse_args(argv)
    try:
        manifest = build(args)
    except CannotStart as exc:
        print(f"the MSI build cannot start: {exc}", file=sys.stderr)
        return 2
    except BuildError as exc:
        print(f"the MSI build failed: {exc}", file=sys.stderr)
        return 1
    print(f"Built {manifest['msi']}: version {manifest['version']}, {manifest['files']} files (runtime "
          f"{manifest['runtime']['version']}: {manifest['runtime_files']}; {PACKAGE}: {manifest['package_files']} "
          f"plus bytecode), product code {manifest['product_code']}, upgrade code {manifest['upgrade_code']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
