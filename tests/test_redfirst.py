"""The red-first runner (scripts/redfirst.py) never writes a mutation into the real working tree.

Each test copies the scratch project in tests/fixtures/redfirst/project to tmp_path, puts the runner under
test (this checkout's scripts/redfirst.py, byte for byte) into its scripts/ folder, and runs it there on a
spec, with TEMP pointed into tmp_path so the runner's working copy lands there too.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
RUNNER = REPO / "scripts" / "redfirst.py"
PROJECT = REPO / "tests" / "fixtures" / "redfirst" / "project"
TASKKILL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
CHECKS = "tests/check_guard.py::"
VERDICTS = {"CAUGHT", "SURVIVED", "BROKE-LOAD", "BAD-SPEC", "BAD-BASE", "NOT-APPLIED", "UNISOLATED"}

sys.path.insert(0, str(REPO / "scripts"))

import redfirst  # noqa: E402

DROP_GUARD = {"file": "lagdoctor/guard.py", "old": "return pid == 0", "new": "return False"}
BUMP_UNUSED = {"file": "lagdoctor/unused.py", "old": "LIMIT = 5", "new": "LIMIT = 6"}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _tree(root: Path) -> dict[str, str]:
    """Every file under root, by relative path, with its SHA-256."""
    return {p.relative_to(root).as_posix(): _sha(p.read_bytes()) for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture
def scratch(tmp_path):
    proj = tmp_path / "project"
    shutil.copytree(PROJECT, proj)
    (proj / "scripts").mkdir()
    (proj / "scripts" / "redfirst.py").write_bytes(RUNNER.read_bytes())      # the runner under test
    temp = tmp_path / "temp"
    temp.mkdir()
    return proj, temp


def _start(proj: Path, temp: Path, spec: list[dict], **env: str) -> subprocess.Popen:
    path = proj.parent / "spec.json"
    path.write_text(json.dumps(spec, indent=1), encoding="utf-8")
    return subprocess.Popen([sys.executable, str(proj / "scripts" / "redfirst.py"), str(path)], cwd=proj,
                            env=dict(os.environ, TEMP=str(temp), TMP=str(temp), TMPDIR=str(temp), **env),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")


def _kill_tree(proc: subprocess.Popen) -> subprocess.CompletedProcess:
    # A hard kill of the runner and everything under it, as a harness timeout does: no finally runs.
    done = subprocess.run([str(TASKKILL), "/T", "/F", "/PID", str(proc.pid)], capture_output=True, text=True,
                          timeout=30)
    proc.wait(timeout=30)
    return done


def _run(proj: Path, temp: Path, spec: list[dict], timeout: float = 300, **env: str) -> tuple[int, str]:
    proc = _start(proj, temp, spec, **env)
    try:
        out, _ = proc.communicate(timeout=timeout)
    finally:
        if proc.poll() is None:
            _kill_tree(proc)
    return proc.returncode, out


def _verdicts(out: str) -> dict[str, tuple[str, str]]:
    """name -> (verdict, info) from the runner's table."""
    rows = {}
    for line in out.splitlines():
        m = re.match(r"([A-Z][A-Z-]+) +(\S.*?)  +(.*)$", line)
        if m and m[1] in VERDICTS:
            rows[m[2]] = (m[1], m[3])
    return rows


def _copy_dir(out: str) -> Path:
    m = re.search(r"^working copy: (.+)$", out, flags=re.M)
    assert m, out
    return Path(m[1].strip())


def _remove(path: Path) -> None:
    deadline = time.monotonic() + 15
    while True:
        try:
            shutil.rmtree(path)
            return
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.2)


def test_a_hard_kill_mid_mutation_leaves_the_real_file_byte_identical(scratch, tmp_path):
    proj, temp = scratch
    guard = proj / "lagdoctor" / "guard.py"
    pristine, before = guard.read_bytes(), _tree(proj)
    marker = tmp_path / "sabotaged-guard-ran-from.txt"
    spec = [{"name": "guard dropped", **DROP_GUARD, "tests": [CHECKS + "test_blocks_while_sabotaged"]}]
    proc = _start(proj, temp, spec, REDFIRST_FIXTURE_MARKER=str(marker))
    try:
        deadline = time.monotonic() + 120
        while not marker.exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists(), f"the sabotaged check never started (runner exit code {proc.poll()})"
        kill = _kill_tree(proc)
        out = proc.stdout.read()
    finally:
        if proc.poll() is None:
            _kill_tree(proc)
    assert kill.returncode == 0 and proc.returncode == 1, (kill.stdout, kill.stderr, proc.returncode)
    # The guarded outcome: the real file, and the whole real tree, are exactly as they were.
    assert guard.read_bytes() == pristine
    assert _tree(proj) == before
    # The kill landed mid-mutation: no verdicts, and the copy left behind still holds the sabotage it ran.
    assert not _verdicts(out), out
    copy = _copy_dir(out)
    assert temp.resolve() in copy.parents
    ran_from = Path(marker.read_text(encoding="utf-8"))
    assert ran_from == copy / "lagdoctor" / "guard.py"
    assert "return False" in ran_from.read_text(encoding="utf-8")
    _remove(copy.parent)        # nothing holds it open any more: the whole process tree is gone


SPEC = [
    {"name": "guard dropped", **DROP_GUARD, "tests": [CHECKS + "test_pid_zero_is_refused"]},
    # The same file again: this one is caught only if the copy was restored after the first.
    {"name": "guard inverted", "file": "lagdoctor/guard.py", "old": "return pid == 0", "new": "return pid != 0",
     "tests": [CHECKS + "test_pid_zero_is_refused", CHECKS + "test_a_normal_pid_is_allowed"]},
    {"name": "untested text", "file": "lagdoctor/guard.py", "old": 'f"pid {pid}"', "new": 'f"process {pid}"',
     "tests": [CHECKS + "test_pid_zero_is_refused"]},
    {"name": "never imported", **BUMP_UNUSED, "tests": [CHECKS + "test_pid_zero_is_refused"]},
    {"name": "syntax error", "file": "lagdoctor/guard.py", "old": "return pid == 0", "new": "return (",
     "tests": [CHECKS + "test_pid_zero_is_refused"]},
    {"name": "no change", "file": "lagdoctor/guard.py", "old": "return pid == 0", "new": "return pid == 0",
     "tests": [CHECKS + "test_pid_zero_is_refused"]},
    {"name": "missing pattern", "file": "lagdoctor/guard.py", "old": "return pid < 0", "new": "return False",
     "tests": [CHECKS + "test_pid_zero_is_refused"]},
    {"name": "pattern not unique", "file": "lagdoctor/guard.py", "old": "pid", "new": "p",
     "tests": [CHECKS + "test_pid_zero_is_refused"]},
    {"name": "red baseline", **DROP_GUARD, "tests": [CHECKS + "test_always_red"]},
    # Bytecode a test leaves in the copy is dropped after the run, so it neither stops the run nor goes stale.
    {"name": "bytecode written", **DROP_GUARD,
     "tests": [CHECKS + "test_compiles_the_guard", CHECKS + "test_pid_zero_is_refused"]},
    # A red without the probe's report proves nothing about the copy.
    {"name": "red without report", **DROP_GUARD, "tests": [CHECKS + "test_fakes_a_red_without_a_report"]},
]


def test_a_run_mutates_only_its_copy_keeps_every_verdict_and_deletes_the_copy(scratch):
    proj, temp = scratch
    before = _tree(proj)
    code, out = _run(proj, temp, SPEC)
    assert _tree(proj) == before, out                 # the real tree was never written
    rows = _verdicts(out)
    assert {name: v for name, (v, _) in rows.items()} == {
        "guard dropped": "CAUGHT", "guard inverted": "CAUGHT", "untested text": "SURVIVED",
        "never imported": "SURVIVED", "syntax error": "BROKE-LOAD", "no change": "NOT-APPLIED",
        "missing pattern": "BAD-SPEC", "pattern not unique": "BAD-SPEC", "red baseline": "BAD-BASE",
        "bytecode written": "CAUGHT", "red without report": "UNISOLATED"}, out
    assert code == 1
    assert "found 0 times" in rows["missing pattern"][1] and "found 5 times" in rows["pattern not unique"][1]
    assert "red: tests/check_guard.py::test_a_normal_pid_is_allowed" in out
    # A survivor says when the test process never opened the mutated file; one it did open does not.
    assert "never opened" in rows["never imported"][1] and "never opened" not in rows["untested text"][1]
    copy = _copy_dir(out)
    assert temp.resolve() in copy.parents
    assert not copy.exists() and not list(temp.glob("redfirst-*")), "the working copy was not deleted"


def test_a_spec_cannot_reach_outside_the_copy(scratch, tmp_path):
    proj, temp = scratch
    outside = tmp_path / "outside.py"
    outside.write_text("LIMIT = 5\n", encoding="utf-8")
    basetemp = tmp_path / "basetemp"
    basetemp.mkdir()
    (basetemp / "keep.txt").write_text("pytest empties its --basetemp folder\n", encoding="utf-8")
    before = _tree(proj)
    ok = [CHECKS + "test_pid_zero_is_refused"]
    spec = [
        {"name": "parent folder", **BUMP_UNUSED, "file": "../outside.py", "tests": ok},
        {"name": "absolute path", **BUMP_UNUSED, "file": str(outside), "tests": ok},
        {"name": "never copied", "file": "README.md", "old": "# Scratch project", "new": "# Sabotaged", "tests": ok},
        {"name": "an option", **BUMP_UNUSED, "tests": [f"--basetemp={basetemp}"]},
        {"name": "an args file", **BUMP_UNUSED, "tests": ["@args.txt"]},
        {"name": "a test outside", **BUMP_UNUSED, "tests": ["../elsewhere/check_x.py::test_x"]},
        {"name": "no tests", **BUMP_UNUSED, "tests": []},
    ]
    code, out = _run(proj, temp, spec)
    assert outside.read_text(encoding="utf-8") == "LIMIT = 5\n" and (basetemp / "keep.txt").is_file()
    assert _tree(proj) == before
    assert {name: v for name, (v, _) in _verdicts(out).items()} == {s["name"]: "BAD-SPEC" for s in spec}, out
    assert code == 1


def test_a_check_that_reads_the_real_tree_is_unisolated_not_caught(scratch):
    # Under the old in-place runner this mutation was CAUGHT; in a copy, a test that reads the real tree
    # tests something other than the sabotage, so the runner must say so instead of giving a verdict.
    proj, temp = scratch
    spec = [{"name": "guard dropped", **DROP_GUARD, "tests": [CHECKS + "test_reads_the_real_tree"]}]
    code, out = _run(proj, temp, spec, REDFIRST_FIXTURE_REAL=str(proj))
    verdict, info = _verdicts(out)["guard dropped"]
    assert verdict == "UNISOLATED" and code == 1, out
    assert "real tree" in info and str(proj.resolve() / "lagdoctor" / "guard.py").lower() in out.lower(), out


def test_a_real_root_on_sys_path_listed_again_mid_run_still_gets_its_verdict(scratch):
    """The venv's editable install puts the real checkout's root on sys.path, and a file saved there mid-run makes
    Python list that folder again. The probe takes the real tree off the test process's sys.path, so that listing
    never happens and the run keeps its verdict. Here PYTHONPATH puts the scratch project's real root there."""
    proj, temp = scratch
    spec = [{"name": "root listed", **DROP_GUARD, "tests": [CHECKS + "test_lists_the_real_root_again"]}]
    code, out = _run(proj, temp, spec, PYTHONPATH=str(proj), REDFIRST_FIXTURE_REAL=str(proj))
    assert _verdicts(out)["root listed"][0] == "CAUGHT" and code == 0, out


def test_a_run_that_ends_without_the_probe_report_is_unisolated(scratch):
    proj, temp = scratch
    spec = [{"name": "exits hard", **DROP_GUARD, "tests": [CHECKS + "test_exits_without_a_report"]}]
    code, out = _run(proj, temp, spec)
    verdict, info = _verdicts(out)["exits hard"]
    assert verdict == "UNISOLATED" and "report" in info and code == 1, out


def test_a_check_that_writes_into_its_tree_stops_the_run(scratch):
    proj, temp = scratch
    before = _tree(proj)
    spec = [{"name": "writes", **BUMP_UNUSED, "tests": [CHECKS + "test_writes_into_its_own_tree"]},
            {"name": "after it", **DROP_GUARD, "tests": [CHECKS + "test_pid_zero_is_refused"]}]
    code, out = _run(proj, temp, spec)
    assert _tree(proj) == before, out                 # the stray file landed in the copy only
    assert code == 3 and "tests/left_behind.txt" in out, out
    assert "after it" not in _verdicts(out), out      # nothing more ran on a copy that is no longer pristine
    kept = _copy_dir(out)                             # kept for inspection after a STOP
    assert (kept / "tests" / "left_behind.txt").is_file(), out


def test_a_temp_folder_inside_the_repo_is_refused(scratch):
    proj, _ = scratch
    inside = proj / "tmp"
    inside.mkdir()
    before = _tree(proj)
    code, out = _run(proj, inside, [{"name": "guard dropped", **DROP_GUARD,
                                     "tests": [CHECKS + "test_pid_zero_is_refused"]}])
    assert code == 3 and "inside" in out, out
    assert not any(inside.iterdir()) and _tree(proj) == before, out


def test_the_probe_records_touches_of_the_real_tree_outside_the_venv(tmp_path):
    real, other = tmp_path / "Real", tmp_path / "other.txt"
    venv = real / ".venv"
    venv.mkdir(parents=True)
    for p in (real / "a.txt", real / "w.py", venv / "site.py", other):
        p.write_bytes(b"x")
    (tmp_path / "probe").mkdir()
    (tmp_path / "probe" / f"{redfirst.PROBE}.py").write_text(redfirst.PROBE_SOURCE, encoding="utf-8")
    report = tmp_path / "report.json"
    code = "\n".join([
        "import os, shutil, sys",
        f"sys.path.insert(0, {str(tmp_path / 'probe')!r})",
        f"import {redfirst.PROBE}",
        f"open({str(real / 'a.txt')!r}).close()",                       # touched
        f"open({str(venv / 'site.py')!r}).close()",                     # the venv: allowed
        f"open({str(other)!r}).close()",                                # outside the real tree
        f"open({str(real / 'W.PY')!r}).close()",                        # the watched file, in other case
        f"os.listdir({str(real)!r})",                                   # touched: the root itself
        f"shutil.copyfile({str(other)!r}, {str(real / 'copied.txt')!r})",   # touched as the destination
    ])
    env = dict(os.environ, REDFIRST_PROBE_REAL=str(real), REDFIRST_PROBE_SKIP=str(venv),
               REDFIRST_PROBE_WATCH=str(real / "w.py"), REDFIRST_PROBE_OUT=str(report))
    subprocess.run([sys.executable, "-I", "-c", code], env=env, check=True, timeout=60)
    got = json.loads(report.read_text(encoding="utf-8"))
    assert got["seen"] is True
    assert sorted(got["touched"]) == sorted(str(p) for p in (real / "a.txt", real, real / "copied.txt"))


def test_the_probe_takes_the_real_tree_off_sys_path_but_keeps_the_venv(tmp_path):
    real = tmp_path / "Real"
    site = real / ".venv" / "Lib" / "site-packages"
    site.mkdir(parents=True)
    (real / "scripts").mkdir()
    (tmp_path / "probe").mkdir()
    (tmp_path / "probe" / f"{redfirst.PROBE}.py").write_text(redfirst.PROBE_SOURCE, encoding="utf-8")
    report, seen = tmp_path / "report.json", tmp_path / "path.json"
    entries = [str(tmp_path / "probe"), str(real) + os.sep, str(real / "scripts"), str(site)]   # the .pth's form
    code = "\n".join([
        "import json, sys",
        f"sys.path[:0] = {entries!r}",
        f"import {redfirst.PROBE}",
        f"json.dump(sys.path, open({str(seen)!r}, 'w', encoding='utf-8'))",
    ])
    env = dict(os.environ, REDFIRST_PROBE_REAL=str(real), REDFIRST_PROBE_SKIP=str(real / ".venv"),
               REDFIRST_PROBE_WATCH=str(real / "w.py"), REDFIRST_PROBE_OUT=str(report))
    subprocess.run([sys.executable, "-I", "-c", code], env=env, check=True, timeout=60)
    left = json.loads(seen.read_text(encoding="utf-8"))
    assert left[:2] == [str(tmp_path / "probe"), str(site)], left      # the real root and its scripts/ are gone


def test_an_empty_spec_is_an_error_not_a_pass(scratch):
    proj, temp = scratch
    code, out = _run(proj, temp, [])
    assert code == 2 and "non-empty" in out, out
    assert not list(temp.glob("redfirst-*")), out      # refused before anything was copied


def test_the_copy_takes_only_what_the_tests_need(tmp_path):
    src = tmp_path / "repo"
    taken = {"lagdoctor/a.py": "x = 1\n", "lagdoctor/sub/b.py": "y = 2\n", "laglab/app.py": "z = 3\n",
             "tests/test_x.py": "def test_x(): pass\n", "tests/fixtures/f.csv": "a,b\n", "scripts/s.py": "",
             "docs/d.md": "# d\n", ".github/workflows/ci.yml": "name: CI\n", "pyproject.toml": "[project]\n",
             "uv.lock": "version = 1\n", "installer/lag-doctor.wxs": "<Wix />\n", "LICENSE": "MIT License\n",
             ".python-version": "3.13\n"}
    left = {"lagdoctor/__pycache__/a.cpython-313.pyc": "pyc", "lagdoctor/a.pyc": "pyc",
            "tests/.pytest_cache/v/x": "1", "tests/fixtures/.git/HEAD": "ref", "docs/.claude/s.json": "{}",
            ".venv/pyvenv.cfg": "home = C:\\Python313\n", ".git": "gitdir: elsewhere\n",
            ".claude/settings.json": "{}", "README.md": "# r\n", "captures/c.ldcap.json": "{}", "other/o.py": ""}
    for rel, text in {**taken, **left}.items():
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_bytes(text.encode("utf-8"))
    manifest = redfirst.copy_tree(src, tmp_path / "copy")
    want = {rel: _sha(text.encode("utf-8")) for rel, text in taken.items()}
    assert manifest == want
    assert _tree(tmp_path / "copy") == want
