"""Red-first check: prove each test can fail by sabotaging the code it guards.

Usage:  uv run python scripts/redfirst.py path/to/spec.json

The spec is a JSON list of mutations:
  {"name": "...", "file": "lagdoctor/x.py", "old": "exact text", "new": "replacement",
   "tests": ["tests/test_x.py::test_name", ...]}

The real working tree is never written. Once per run, what the tests need (lagdoctor/, laglab/, tests/, installer/,
scripts/, docs/, .github/, pyproject.toml, uv.lock, LICENSE; never .venv, .git, .claude or __pycache__) is copied
into a new temp folder, printed as "working copy: ...", and pytest runs there (cwd = the copy) with this interpreter,
the real repo's venv. The copy is deleted at the end, or kept for inspection after a STOP; a hard kill
leaves only the temp copy behind.

For each mutation, independently: `file` must be a file in the copy and `tests` test ids inside it (not
options), else BAD-SPEC; the `old` text must occur exactly once (BAD-SPEC); the named tests must pass on
the pristine copy (BAD-BASE); the copied file is mutated and its hash must change (NOT-APPLIED); the tests
run with bytecode writing off; the file is put back from its pristine bytes. After every run any bytecode
is dropped and the whole copy must hash as it was copied (which also proves the restore); if not,
everything stops (exit 3). A mutation is CAUGHT only when pytest ran tests and reported failures (exit
code 1). A collection or import error (exit code 2+) means the sabotage broke loading, which proves
nothing, so it is BROKE-LOAD, not CAUGHT. A probe loaded into each pytest run records what that process
touches: if it touched the real tree (outside the venv), or ended without its report, the mutation is
UNISOLATED, since the copy may not be what was tested. The probe also takes the real tree off that process's
sys.path, where the venv's editable install puts the repo's root. Exit status is 0 only if every mutation was
caught.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

ROOT = Path(__file__).resolve().parents[1]
COPY_DIRS = ("lagdoctor", "laglab", "tests", "scripts", "docs", ".github", "installer")   # tests read ci.yml, the .wxs
COPY_FILES = ("pyproject.toml", "uv.lock", "LICENSE")                 # LICENSE: the MSI ships it
SKIP_NAMES = (".venv", ".git", ".claude", "__pycache__", ".pytest_cache")
SKIP_SUFFIXES = (".pyc", ".pyo")
PROBE = "_redfirst_probe"              # written into the copy's root, loaded with -p
PROBE_FILE = PROBE + ".py"

PROBE_SOURCE = r'''"""Loaded into each pytest run by scripts/redfirst.py (-p _redfirst_probe).

Records the paths this process touches inside the real repo (outside the venv) and whether it opened the
mutated file, and writes them as JSON at exit. It never raises into the code it watches. It also takes the
real repo off sys.path, where the venv's editable install puts its root.
"""
import atexit
import json
import os
import sys


def _norm(path):
    return os.path.normcase(os.path.abspath(path))


_REAL = _norm(os.environ["REDFIRST_PROBE_REAL"])
_SKIP = tuple(_norm(p) for p in os.environ["REDFIRST_PROBE_SKIP"].split(os.pathsep) if p)
_WATCH = _norm(os.environ["REDFIRST_PROBE_WATCH"])
_OUT = os.environ["REDFIRST_PROBE_OUT"]
# audit event -> positions of its path arguments
_EVENTS = {"open": (0,), "os.listdir": (0,), "os.scandir": (0,), "os.remove": (0,), "os.rmdir": (0,),
           "os.mkdir": (0,), "os.rename": (0, 1), "shutil.copyfile": (0, 1), "shutil.copytree": (0, 1),
           "shutil.move": (0, 1), "shutil.rmtree": (0,)}
_touched = set()
_seen = {"watch": False}


def _under(path, top):
    return path == top or path.startswith(top.rstrip(os.sep) + os.sep)


def _real(path):
    return _under(path, _REAL) and not any(_under(path, s) for s in _SKIP)


def _hook(event, args):
    where = _EVENTS.get(event)
    if where is None:
        return
    try:
        for i in where:
            if i < len(args) and isinstance(args[i], (str, bytes, os.PathLike)):
                raw = os.path.abspath(os.fsdecode(args[i]))
                path = os.path.normcase(raw)
                if path == _WATCH:
                    _seen["watch"] = True
                elif _real(path):
                    _touched.add(raw)
    except Exception:
        pass


def _report():
    with open(_OUT, "w", encoding="utf-8") as f:
        json.dump({"touched": sorted(_touched), "seen": _seen["watch"]}, f)


# Off sys.path, the real tree can be neither imported from that way nor listed again: Python lists a sys.path
# folder anew after a file in it is saved, which would read as a touch of the real tree.
sys.path[:] = [p for p in sys.path if not _real(_norm(p or "."))]
sys.addaudithook(_hook)
atexit.register(_report)
'''


class Stop(Exception):
    """The working copy can no longer be trusted: stop everything (exit 3)."""


@dataclass
class Run:
    code: int
    summary: str
    failed: list[str]
    touched: list[str] | None      # paths touched in the real tree; None: the probe never reported
    seen: bool                     # the test process opened the mutated file


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _raise(exc: OSError) -> None:
    raise exc


def _skipped(name: str) -> bool:
    return name in SKIP_NAMES or name.endswith(SKIP_SUFFIXES)


def _sources(root: Path) -> dict[str, Path]:
    """The files a copy takes from root, by POSIX path relative to it."""
    out = {name: root / name for name in COPY_FILES if (root / name).is_file()}
    for top in COPY_DIRS:
        if not (root / top).is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root / top, onerror=_raise):
            here = Path(dirpath)
            dirnames[:] = sorted(d for d in dirnames if not _skipped(d))
            for name in filenames:
                if not _skipped(name):
                    out[(here / name).relative_to(root).as_posix()] = here / name
    return out


def _tree(root: Path) -> dict[str, str]:
    """Every file under root, by POSIX relative path, with its SHA-256."""
    out = {}
    for dirpath, _dirnames, filenames in os.walk(root, onerror=_raise):
        for name in filenames:
            path = Path(dirpath) / name
            out[path.relative_to(root).as_posix()] = _sha(path.read_bytes())
    return out


def copy_tree(src: Path, dst: Path) -> dict[str, str]:
    """Copy what the tests need from src into dst; return {relative path: SHA-256} of the copy."""
    manifest = {}
    for rel, path in _sources(src).items():
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dst / rel)
        manifest[rel] = _sha((dst / rel).read_bytes())
    return manifest


def _check_copy(copy: Path, expected: dict[str, str]) -> None:
    """After every run: drop any bytecode (it could outlive a restore), then the copy must be as copied."""
    for d in list(copy.rglob("__pycache__")):
        shutil.rmtree(d)
    now = _tree(copy)
    if now != expected:
        changed = sorted(k for k in now.keys() & expected.keys() if now[k] != expected[k])
        parts = [f"{what}: {', '.join(paths[:5])}" for what, paths in
                 (("changed", changed), ("new", sorted(now.keys() - expected.keys())),
                  ("gone", sorted(expected.keys() - now.keys()))) if paths]
        raise Stop(f"a test changed the working copy ({'; '.join(parts)})")


def _bad_tests(tests: object) -> str | None:
    """Why a spec's `tests` cannot run safely in the copy, or None."""
    if not isinstance(tests, list) or not tests:
        return "no tests named"
    for t in tests:
        if not isinstance(t, str) or not t.strip() or t.startswith(("-", "@")):
            return f"{t!r} is not a test id"            # an option could point pytest at the real tree
        p = PureWindowsPath(t.split("::", 1)[0])
        if p.drive or p.root or ".." in p.parts:
            return f"{t!r} is outside the working copy"
    return None


def _run(tests: list[str], copy: Path, watch: Path, work: Path) -> Run:
    report = work / f"{uuid.uuid4().hex}.json"
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8", REDFIRST_PROBE_REAL=str(ROOT),
               REDFIRST_PROBE_SKIP=os.pathsep.join(sorted({sys.prefix, sys.exec_prefix, sys.base_prefix})),
               REDFIRST_PROBE_WATCH=str(watch), REDFIRST_PROBE_OUT=str(report))
    proc = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-p", PROBE,
                           "-q", "-rf", *tests], cwd=copy, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=600)
    out = proc.stdout + proc.stderr
    failed = re.findall(r"^FAILED (\S+)", out, flags=re.M)
    summary = next((ln for ln in reversed(out.splitlines()) if re.search(r"\d+ (passed|failed|error)", ln)), "")
    try:
        probe = json.loads(report.read_text(encoding="utf-8"))
        touched, seen = [str(p) for p in probe["touched"]], bool(probe["seen"])
    except (OSError, ValueError, KeyError, TypeError):
        touched, seen = None, False
    return Run(proc.returncode, summary.strip("= "), failed, touched, seen)


def _unisolated(run: Run) -> tuple[str, list[str]] | None:
    """Why this run's result says nothing about the copy, with the paths behind it, or None."""
    if run.touched:
        more = [f"real: ... and {len(run.touched) - 5} more"] if len(run.touched) > 5 else []
        return (f"the test process touched {len(run.touched)} path(s) in the real tree, so the copy may not "
                f"be what it tested", [f"real: {p}" for p in run.touched[:5]] + more)
    if run.touched is None and (run.code == 0 or (run.code == 1 and run.failed)):
        return f"pytest ended (exit code {run.code}) without the isolation probe's report", []
    return None


def _mutation(m: dict, copy: Path, copied: dict[str, str], expected: dict[str, str],
              work: Path) -> tuple[str, str, str, list[str]]:
    """One mutation, in the copy only: (name, verdict, info, lines to print under it)."""
    name = m["name"]
    key = PurePosixPath(str(m["file"]).replace("\\", "/")).as_posix()
    if key not in copied:
        return name, "BAD-SPEC", f"{m['file']} is not a file in the working copy", []
    problem = _bad_tests(m["tests"])
    if problem:
        return name, "BAD-SPEC", problem, []
    target = copy / key
    original = target.read_bytes()          # pristine: checked by copy_tree, then after every run
    text = original.decode("utf-8")
    count = text.count(m["old"])
    if count != 1:
        return name, "BAD-SPEC", f"pattern found {count} times in {m['file']}", []
    base = _run(m["tests"], copy, target, work)
    _check_copy(copy, expected)
    why = _unisolated(base)
    if why:
        return name, "UNISOLATED", f"baseline: {why[0]}", why[1]
    if base.code != 0:
        return name, "BAD-BASE", f"tests not green before sabotage: {base.summary}", []
    try:
        target.write_bytes(text.replace(m["old"], m["new"]).encode("utf-8"))
        if _sha(target.read_bytes()) == copied[key]:
            return name, "NOT-APPLIED", "file unchanged after mutation", []
        run = _run(m["tests"], copy, target, work)
    finally:
        target.write_bytes(original)
    _check_copy(copy, expected)             # also proves the restore
    why = _unisolated(run)
    if why:
        return name, "UNISOLATED", why[0], why[1]
    verdict = "CAUGHT" if run.code == 1 and run.failed else ("SURVIVED" if run.code == 0 else "BROKE-LOAD")
    info = f"{run.summary} (baseline: {base.summary})"
    if verdict == "SURVIVED" and not run.seen:
        info += "; the test process never opened the mutated file"
    return name, verdict, info, [f"red: {f}" for f in run.failed]


def _table(results: list[tuple[str, str, str, list[str]]]) -> None:
    if not results:
        return
    width = max(len(r[0]) for r in results)
    for name, verdict, info, lines in results:
        print(f"{verdict:11} {name:{width}}  {info}")
        for line in lines:
            print(f"{'':11} {'':{width}}    {line}")


def _writable_retry(func, path, _exc) -> None:
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _remove(path: Path) -> bool:
    """Delete the temp folder; a process a test left behind may hold a file there for a moment."""
    deadline = time.monotonic() + 10
    while True:
        try:
            if sys.version_info >= (3, 12):
                shutil.rmtree(path, onexc=_writable_retry)
            else:
                shutil.rmtree(path, onerror=_writable_retry)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            if time.monotonic() > deadline:
                return False
            time.sleep(0.25)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    spec = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    if not isinstance(spec, list) or not spec:
        print(f"{argv[0]}: a spec must be a non-empty JSON list of mutations", file=sys.stderr)
        return 2
    tmp = Path(tempfile.mkdtemp(prefix="redfirst-")).resolve()
    copy, work = tmp / ROOT.name, tmp / "probe"
    results: list[tuple[str, str, str, list[str]]] = []
    keep = False
    try:
        if tmp.is_relative_to(ROOT):
            raise Stop(f"the temp folder {tmp} is inside the repo {ROOT}; point TEMP somewhere else")
        work.mkdir()
        copied = copy_tree(ROOT, copy)
        (copy / PROBE_FILE).write_text(PROBE_SOURCE, encoding="utf-8")
        expected = {**copied, PROBE_FILE: _sha((copy / PROBE_FILE).read_bytes())}
        print(f"working copy: {copy}", flush=True)
        for i, m in enumerate(spec, 1):
            results.append(_mutation(m, copy, copied, expected, work))
            print(f"[{i}/{len(spec)}] {results[-1][1]} {results[-1][0]}", file=sys.stderr, flush=True)
        _table(results)
        return 0 if all(r[1] == "CAUGHT" for r in results) else 1
    except Stop as stop:
        _table(results)
        keep = copy.exists()
        kept = f"; the working copy is kept for inspection: {copy}" if keep else ""
        print(f"STOP: {stop}{kept}", file=sys.stderr)
        return 3
    finally:
        if not keep and not _remove(tmp):
            print(f"WARNING: could not delete the working copy {tmp}; delete it by hand", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
