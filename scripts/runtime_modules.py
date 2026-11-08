r"""What python.org's embeddable runtime can import, in that runtime's own words: scripts/runtime_modules.json.

Usage:
  uv run python scripts/runtime_modules.py --zip ZIP [--write] [--listing JSON]

tests/test_stdlib_only.py lets lagdoctor/ import only the modules the JSON lists as importable. The MSI ships this
runtime (scripts/runtime_pin.py), and it leaves part of the standard library out: tkinter and Tcl/Tk, IDLE, turtle
and turtledemo, venv, ensurepip and the tests (docs/research/install-and-rights.md, section 10). The interpreter that
runs the tests has them all, and its sys.stdlib_module_names names them all, so that list cannot be the check.

1. The pinned ZIP: size and SHA-256 as scripts/runtime_pin.py has them (downloaded to ZIP first when ZIP does not
   exist, as scripts/msi_build.py does).
2. Unpacked into a new temporary folder and asked: its own python.exe, isolated by its python313._pth, looks up every
   name on its own sys.stdlib_module_names (importlib.util.find_spec) and imports each one it finds, with
   webbrowser.open stubbed first (antigravity opens a web page when imported).
3. The answer checked against the ZIP before it is believed: the runtime is the pinned version, its sys.path is its
   own ZIP and folder alone, each module it found is built in, frozen, a .pyd in its top folder or an entry of its
   python313.zip, the built-in ones are its sys.builtin_module_names, every .pyd and every top-level module of
   python313.zip on the list was found, and none of the modules it did not find is in the ZIP.
4. Without --write, compared with the JSON: every module whose verdict moved is listed (CI's msi job runs this, so a
   JSON edited by hand, or left behind when the pin moved, fails there). With --write, the JSON is rewritten from the
   answer: do that when the pin moves, and commit the two together.

Exit codes: 0 the JSON is the runtime's answer (or was written); 1 it is not, or the answer disagrees with the ZIP;
2 cannot start (the ZIP cannot be had, or the runtime would not run).
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "scripts"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from runtime_pin import EMBED_SHA256, EMBED_SIZE, EMBED_VERSION  # noqa: E402

LISTING = ROOT / "scripts" / "runtime_modules.json"
REGENERATE = "uv run python scripts/runtime_modules.py --zip <a path for the ZIP> --write"
ABOUT = ("What python.org's embeddable runtime (scripts/runtime_pin.py) imports, as that runtime answered itself; "
         "written by scripts/runtime_modules.py. tests/test_stdlib_only.py lets lagdoctor/ import only the importable "
         "modules.")
KINDS = ("built-in", "frozen", "extension", "zip")
CHILD_TIMEOUT_S = 300
INIT = ("__init__.pyc", "__init__.py")

# Run by the unpacked runtime's python.exe: argv[1] gets the answer (JSON), with sys.path as it was at start. Its own
# stdout is not read, since a module may print when imported (this does).
CHILD = r'''
import importlib, importlib.util, json, sys
path = list(sys.path)
import webbrowser
webbrowser.open = lambda *args, **kwargs: True
modules = {}
for name in sorted(sys.stdlib_module_names):
    try:
        spec = importlib.util.find_spec(name)
    except BaseException as exc:
        modules[name] = {"lookup_error": f"{type(exc).__name__}: {exc}"}
        continue
    if spec is None:
        modules[name] = {"origin": None}
        continue
    origin = spec.origin or "a namespace package in " + ";".join(spec.submodule_search_locations or ())
    try:
        importlib.import_module(name)
        modules[name] = {"origin": origin, "error": None}
    except BaseException as exc:
        modules[name] = {"origin": origin, "error": f"{type(exc).__name__}: {exc}"}
with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump({"version": sys.version.split()[0], "path": path, "isolated": sys.flags.isolated,
               "builtin": sorted(sys.builtin_module_names), "modules": modules}, f)
'''


class CannotStart(Exception):
    """The runtime cannot be had or would not run (exit 2)."""


class Disagreement(Exception):
    """The runtime's answer and its ZIP part ways (exit 1)."""


# ------------------------------------------------------------------------ pure parts ---

def pinned() -> dict:
    """The runtime a listing must have been made from: scripts/runtime_pin.py's."""
    return {"version": EMBED_VERSION, "sha256": EMBED_SHA256, "size": EMBED_SIZE}


def stale(listing: dict, pin: dict | None = None) -> str | None:
    """Why `listing` is not the pinned runtime's (None when it is): the runtime it records against the pin."""
    pin = pinned() if pin is None else pin
    made = listing.get("runtime")
    if made != pin:
        return (f"scripts/runtime_modules.json was made from the runtime {made}, but scripts/runtime_pin.py pins "
                f"{pin}: regenerate it with `{REGENERATE}` and commit it with the pin")
    return None


def zip_modules(outer: list[str], inner: list[str]) -> tuple[set[str], set[str]]:
    """The modules a runtime ZIP holds, by its listings alone: the .pyd files in its top folder (outer, the ZIP's own
    entries), and the top-level modules and packages of the archive inside it (inner, that archive's entries)."""
    pyds = {n.split(".")[0] for n in outer if "/" not in n and n.lower().endswith(".pyd")}
    tops = set()
    for n in inner:
        parts = n.split("/")
        if len(parts) == 1 and n.endswith((".pyc", ".py")):
            tops.add(n.rsplit(".", 1)[0])
        elif len(parts) == 2 and parts[1] in INIT:
            tops.add(parts[0])
    return pyds, tops


def read_answer(answer: dict, folder: str, archive: str) -> tuple[dict[str, str], dict[str, str], list[str],
                                                                   list[str]]:
    """The runtime's answer, module by module: where each module it found came from (built-in, frozen, extension: a
    .pyd in `folder`, zip: an entry of `folder`\\`archive`), the error of each one it found but could not import (the
    folder written as <runtime>, so no path of this PC lands in the JSON), the ones it did not find, and every problem
    in reading it (a lookup that failed, a module found anywhere else)."""
    top = os.path.normcase(os.path.normpath(folder))
    inside = os.path.join(top, os.path.normcase(archive)) + os.sep
    found: dict[str, str] = {}
    errors: dict[str, str] = {}
    absent: list[str] = []
    problems: list[str] = []
    for name, m in sorted(answer["modules"].items()):
        if m.get("lookup_error"):
            problems.append(f"looking {name} up failed: {m['lookup_error']}")
            continue
        origin = m.get("origin")
        if origin is None:
            absent.append(name)
            continue
        if origin in ("built-in", "frozen"):
            found[name] = origin
        else:
            where = os.path.normcase(os.path.normpath(origin))
            if where.startswith(inside):
                found[name] = "zip"
            elif os.path.dirname(where) == top and where.endswith(".pyd"):
                found[name] = "extension"
            else:
                problems.append(f"{name} was found outside the runtime's own files, at {origin}")
                continue
        if m.get("error"):
            errors[name] = re.sub(re.escape(folder), "<runtime>", m["error"], flags=re.IGNORECASE)
    return found, errors, absent, problems


def _apart(got: set[str], want: set[str]) -> str:
    return f"{sorted(got - want)} more and {sorted(want - got)} fewer"


def disagreements(answer: dict, found: dict[str, str], absent: list[str], pyds: set[str], tops: set[str],
                  folder: str, archive: str) -> list[str]:
    """Where the runtime's answer and its ZIP's listings part ways (empty when they agree)."""
    out = []
    if answer.get("version") != EMBED_VERSION:
        out.append(f"the runtime says it is Python {answer.get('version')}, but the pin is {EMBED_VERSION}")
    own = [os.path.normcase(p) for p in (os.path.join(folder, archive), folder)]
    if [os.path.normcase(p) for p in answer.get("path", [])] != own or not answer.get("isolated"):
        out.append(f"the runtime's sys.path is {answer.get('path')} (isolated: {answer.get('isolated')}), not its own "
                   f"ZIP and folder alone")
    stdlib = set(answer["modules"])
    kinds = {k: {n for n, v in found.items() if v == k} for k in KINDS}
    builtin = set(answer.get("builtin", ())) & stdlib
    if kinds["built-in"] != builtin:
        out.append(f"found built in: {_apart(kinds['built-in'], builtin)} than its sys.builtin_module_names")
    if not pyds <= stdlib:
        out.append(f"the ZIP's {sorted(pyds - stdlib)} are not on the runtime's standard-library list")
    if kinds["extension"] != pyds:
        out.append(f"found as extension modules: {_apart(kinds['extension'], pyds)} than the ZIP's .pyd files")
    if not kinds["zip"] <= tops:
        out.append(f"found in {archive}, but not at its top: {sorted(kinds['zip'] - tops)}")
    if missed := sorted((tops & stdlib) - set(found)):
        out.append(f"{archive} holds {missed}, which the runtime did not find")
    if shipped := sorted(set(absent) & (pyds | tops | builtin)):
        out.append(f"not found, yet in the ZIP or built in: {shipped}")
    return out


def listing(found: dict[str, str], errors: dict[str, str], absent: list[str]) -> dict:
    """The JSON's content: the pinned runtime, and each module on its standard-library list under one verdict."""
    return {"about": ABOUT, "runtime": pinned(),
            "importable": {n: k for n, k in sorted(found.items()) if n not in errors},
            "fails_to_import": dict(sorted(errors.items())), "not_shipped": sorted(absent)}


def render(content: dict) -> bytes:
    """The JSON's bytes: sorted, ASCII, LF line endings and a final newline, the same on every machine."""
    return (json.dumps(content, indent=1, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")


def verdict(content: dict, name: str) -> str:
    if name in content.get("importable", {}):
        return f"importable ({content['importable'][name]})"
    if name in content.get("fails_to_import", {}):
        return f"fails to import ({content['fails_to_import'][name]})"
    if name in content.get("not_shipped", ()):
        return "not shipped"
    return "not listed"


def moves(old: dict, new: dict) -> list[str]:
    """Every runtime field and every module whose verdict differs from `old` to `new`, one line each."""
    old_rt, new_rt = old.get("runtime") or {}, new.get("runtime") or {}
    out = [f"runtime {k}: {old_rt.get(k)!r} -> {new_rt.get(k)!r}" for k in sorted(set(old_rt) | set(new_rt))
           if old_rt.get(k) != new_rt.get(k)]
    names = set()
    for content in (old, new):
        for key in ("importable", "fails_to_import", "not_shipped"):
            names |= set(content.get(key, ()))
    for name in sorted(names):
        before, after = verdict(old, name), verdict(new, name)
        if before != after:
            out.append(f"{name}: {before} -> {after}")
    return out


def compare(committed: bytes, fresh: dict) -> list[str]:
    """Why the committed JSON is not `fresh` (empty when it is, byte for byte): each move, or why it cannot be read."""
    if committed == render(fresh):
        return []
    try:
        old = json.loads(committed)
        found = moves(old, fresh)
    except (ValueError, TypeError, AttributeError) as exc:
        return [f"it is not a listing this script writes ({type(exc).__name__}: {exc})"]
    return found or ["it holds the same verdicts, laid out otherwise"]


def child_env() -> dict[str, str]:
    """The environment the runtime is asked in: Windows' own folder and none of this PC's PATH, so the answer does
    not hang on what else is installed (ctypes.util.find_library, which _ios_support calls, searches PATH)."""
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    return {"SystemRoot": root, "PATH": os.pathsep.join((os.path.join(root, "System32"), root))}


def ask_child(python: Path, answer_path: Path, cwd: Path) -> subprocess.CompletedProcess:
    """Run CHILD in the unpacked runtime's python.exe, isolated (-I), in child_env()."""
    return subprocess.run([str(python), "-I", "-c", CHILD, str(answer_path)], cwd=cwd, env=child_env(),
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=CHILD_TIMEOUT_S)


def summary(content: dict) -> str:
    return (f"{len(content['importable'])} modules importable, {len(content['fails_to_import'])} found but failing to "
            f"import ({', '.join(content['fails_to_import'])}), {len(content['not_shipped'])} not shipped")


# ------------------------------------------------------------------------ the run ---

def ask_runtime(zip_path: Path) -> dict:
    """The pinned runtime's answer as the JSON's content: unpacked into a new folder, asked, checked against its ZIP."""
    import msi_build                                    # the pinned download and its checks (loads msi.dll)
    try:
        zip_path = msi_build.runtime_zip(zip_path)
    except msi_build.BuildError as exc:
        raise CannotStart(str(exc)) from exc
    work = Path(tempfile.mkdtemp(prefix="runtime-modules-")).resolve()
    try:
        folder = work / "runtime"
        folder.mkdir()
        with zipfile.ZipFile(zip_path) as z:
            outer = z.namelist()
            bad = [n for n in outer if not msi_build.plain_name(n)]
            archives = [n for n in outer if n.lower().endswith(".zip")]
            if bad or len(archives) != 1:
                raise Disagreement(f"the ZIP holds names that are not plain file names ({bad}), or not one archive "
                                   f"({archives})")
            with zipfile.ZipFile(io.BytesIO(z.read(archives[0]))) as archive:
                inner = archive.namelist()
            z.extractall(folder)
        answer_path = work / "answer.json"
        try:
            p = ask_child(folder / "python.exe", answer_path, work)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CannotStart(f"the runtime's python.exe would not run: {exc}") from exc
        if p.returncode != 0 or not answer_path.is_file():
            raise CannotStart(f"the runtime's python.exe ended with exit code {p.returncode}: "
                              f"{p.stderr.strip()[-600:]!r}")
        try:
            answer = json.loads(answer_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise CannotStart(f"the runtime's answer is not JSON: {exc}") from exc
        found, errors, absent, problems = read_answer(answer, str(folder), archives[0])
        pyds, tops = zip_modules(outer, inner)
        problems += disagreements(answer, found, absent, pyds, tops, str(folder), archives[0])
        if problems:
            raise Disagreement("; ".join(problems))
        return listing(found, errors, absent)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--zip", required=True, help="the embeddable ZIP (downloaded there when missing)")
    ap.add_argument("--write", action="store_true", help="rewrite the JSON from the runtime's answer")
    ap.add_argument("--listing", default=str(LISTING), help="the JSON to compare or write (default: %(default)s)")
    args = ap.parse_args(argv)
    path = Path(args.listing)
    try:
        fresh = ask_runtime(Path(args.zip).resolve())
    except CannotStart as exc:
        print(f"the runtime's modules cannot be listed: {exc}", file=sys.stderr)
        return 2
    except Disagreement as exc:
        print(f"the runtime's answer disagrees with its ZIP: {exc}", file=sys.stderr)
        return 1
    if args.write:
        path.write_bytes(render(fresh))
        print(f"Wrote {path}: Python {EMBED_VERSION}, {summary(fresh)}.")
        return 0
    try:
        problems = compare(path.read_bytes(), fresh)
    except OSError as exc:
        problems = [f"it cannot be read: {exc}"]
    if problems:
        print(f"{path} is not the pinned runtime's answer:", file=sys.stderr)
        for line in problems:
            print(f"  {line}", file=sys.stderr)
        print(f"Rewrite it with `{REGENERATE}`, never by hand.", file=sys.stderr)
        line = f"The runtime's module list differs from its answer ({len(problems)} lines)."
    else:
        line = f"{path.name} is the pinned runtime's own answer: Python {EMBED_VERSION}, {summary(fresh)}."
        print(line)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(line + "\n")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
