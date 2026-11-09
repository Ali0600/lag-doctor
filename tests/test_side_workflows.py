"""The side workflows run on every pull request that changes code they run (docs/DECISIONS.md, 2026-11-08).

experiments.yml and webview-spike.yml start on a pull request only when it changes a path their `paths:` filter names.
Their scripts import lagdoctor modules and sibling scripts, so a filter that names only the scripts lets a pull request
change what they run without running them: PR #6 changed four modules the experiments run as a service and started
none. These tests start from each workflow's entry scripts (the ones its steps run, and every file a wildcard entry
covers), follow their imports through the repo, lazy ones inside functions included, and require the filter to name
every file reached and no file that is not.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
SIDE = ("experiments.yml", "webview-spike.yml")
# Where an absolute import is found: the repo's root (lagdoctor) and scripts/, which every script puts on sys.path.
SEARCH = ("", "scripts")
# One file per route an import can take to it, so a reader that loses a route fails here, not silently.
REACHED = {
    "experiments.yml": {
        "scripts/runtime_pin.py": "a sibling script: `from runtime_pin import ...` in drive.py",
        "scripts/etw_access.py": "a plain `import etw_access` inside a function",
        "lagdoctor/capture/base.py": "a module named by `from lagdoctor.capture.base import Collector`",
        "lagdoctor/winapi/etw.py": "a submodule named in `from lagdoctor.winapi import etw`",
        "lagdoctor/winapi/__init__.py": "the package on the way to its modules",
        "lagdoctor/winapi/storage.py": "only through another module: lagdoctor/paths.py imports it",
    },
    "webview-spike.yml": {
        "scripts/stage2/guard.py": "`from stage2 import guard`, a package under scripts/",
        "lagdoctor/paths.py": "only through another module: lagdoctor/setup.py imports it",
    },
}


def filter_paths(text: str) -> list[str]:
    """The `paths:` entries of a workflow's `pull_request` trigger."""
    m = re.search(r"^  pull_request:\n    paths:\n((?:      - \"[^\"\n]+\"\n|[ \t]*#[^\n]*\n)+)", text, re.M)
    assert m, "no pull_request paths filter"
    return re.findall(r'^      - "([^"\n]+)"$', m.group(1), re.M)


def run_scripts(text: str) -> list[str]:
    """The repo scripts a workflow's steps run with python, comments left out."""
    code = "\n".join(line.split(" #")[0] for line in text.splitlines() if not line.lstrip().startswith("#"))
    return sorted(set(re.findall(r"\bpython (scripts/[\w./-]+\.py)\b", code)))


def glob_re(pattern: str) -> re.Pattern:
    """A filter pattern as GitHub reads it: `**` crosses folders, `*` stays inside one."""
    out = "".join(".*" if part == "**" else "[^/]*" if part == "*" else re.escape(part)
                  for part in re.split(r"(\*\*|\*)", pattern) if part)
    return re.compile(out + r"\Z")


def named(path: str, patterns: list[str]) -> bool:
    return any(glob_re(p).match(path) for p in patterns)


def python_files(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*.py")
                  if "__pycache__" not in p.parts and ".venv" not in p.parts)


def resolve(dotted: str, root: Path) -> list[str]:
    """The repo files that importing `dotted` loads: each package's __init__ on the way, then the module."""
    for base in SEARCH:
        here, found = root / base, []
        parts = dotted.split(".")
        for i, part in enumerate(parts):
            here = here / part
            if (here / "__init__.py").is_file():
                found.append(here / "__init__.py")
            elif i == len(parts) - 1 and here.with_suffix(".py").is_file():
                found.append(here.with_suffix(".py"))
            else:
                found = []
                break
        if found:
            return [p.relative_to(root).as_posix() for p in found]
    return []


def package_of(rel: str) -> list[str]:
    """The dotted package a file sits in, as its relative imports see it."""
    parts = rel.split("/")[:-1]
    return parts[1:] if parts[:1] == ["scripts"] else parts


def imports(rel: str, root: Path) -> set[str]:
    """Every repo file one file's imports load, wherever in the file they are."""
    out: set[str] = set()
    for node in ast.walk(ast.parse((root / rel).read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.update(resolve(alias.name, root))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package_of(rel)[:len(package_of(rel)) - (node.level - 1)]
                module = ".".join(base + ([node.module] if node.module else []))
            else:
                module = node.module
            out.update(resolve(module, root))
            for alias in node.names:                    # `from package import submodule`
                out.update(resolve(f"{module}.{alias.name}", root))
    return out


def closure(starts: list[str], root: Path) -> set[str]:
    seen: set[str] = set()
    todo = list(starts)
    while todo:
        rel = todo.pop()
        if rel not in seen:
            seen.add(rel)
            todo.extend(imports(rel, root) - seen)
    return seen


def entries(text: str, root: Path) -> list[str]:
    """Where a workflow's code starts: the scripts its steps run, and every file a wildcard entry covers."""
    wild = [p for p in filter_paths(text) if "*" in p]
    return sorted(set(run_scripts(text)) | {f for f in python_files(root) if named(f, wild)})


# ------------------------------------------------------------------------------------------------- the filters ---

@pytest.mark.parametrize("workflow", SIDE)
def test_a_side_workflow_runs_on_every_file_its_code_imports(workflow):
    text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
    patterns, starts = filter_paths(text), entries(text, ROOT)
    assert starts and run_scripts(text), f"{workflow}: no entry scripts found"
    reached = closure(starts, ROOT)
    missing = sorted(f for f in reached if not named(f, patterns))
    assert not missing, (f"{workflow}'s code imports files its pull_request paths do not name, so a pull request "
                         f"changing one would not run it: add {missing}")


@pytest.mark.parametrize("workflow", SIDE)
def test_a_side_workflow_names_no_file_its_code_does_not_import(workflow):
    text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
    patterns = filter_paths(text)
    reached = closure(entries(text, ROOT), ROOT)
    plain = [p for p in patterns if "*" not in p and p != f".github/workflows/{workflow}"]
    stale = sorted(p for p in plain if p not in reached)
    assert not stale, f"{workflow}'s paths name files its code no longer imports: drop {stale}"
    assert f".github/workflows/{workflow}" in patterns, f"{workflow} does not run when it changes itself"


@pytest.mark.parametrize("workflow", SIDE)
def test_the_imports_are_followed_by_every_route(workflow):
    text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
    reached = closure(entries(text, ROOT), ROOT)
    lost = {f: why for f, why in REACHED[workflow].items() if f not in reached}
    assert not lost, f"the import reader lost a route: {lost}"


# ----------------------------------------------------------------------------------------------- the readers ---

def test_the_import_reader_follows_each_kind_of_import(tmp_path):
    files = {"pkg/__init__.py": "", "pkg/a.py": "from . import b\nfrom .sub.c import X\n", "pkg/b.py": "",
             "pkg/sub/__init__.py": "", "pkg/sub/c.py": "X = 1\n", "pkg/unused.py": "",
             "scripts/s.py": "import json\nimport sib\ndef f():\n    from pkg import a\n",
             "scripts/sib.py": "from grp import x\n", "scripts/grp/__init__.py": "", "scripts/grp/x.py": "",
             "scripts/grp/y.py": ""}
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    assert closure(["scripts/s.py"], tmp_path) == {
        "scripts/s.py", "scripts/sib.py", "scripts/grp/__init__.py", "scripts/grp/x.py", "pkg/__init__.py",
        "pkg/a.py", "pkg/b.py", "pkg/sub/__init__.py", "pkg/sub/c.py"}


def test_filter_patterns_match_as_github_reads_them():
    assert named("scripts/stage2/a/b.py", ["scripts/stage2/**"])
    assert named("lagdoctor/setup.py", ["lagdoctor/*.py"])
    assert not named("lagdoctor/winapi/etw.py", ["lagdoctor/*.py"])
    assert not named("lagdoctor/setup.pyc", ["lagdoctor/setup.py"])
    assert not named("xlagdoctor/setup.py", ["lagdoctor/setup.py"])


def test_the_workflow_readers_take_the_paths_and_the_scripts():
    text = ('on:\n  pull_request:\n    paths:\n      - "a.yml"\n      # why\n      - "s/**"\n\njobs:\n'
            '      - run: uv run --no-sync python scripts/x.py --ci\n      # run: python scripts/no.py\n'
            '      - run: uv run python scripts/y/z.py  # python scripts/not.py\n')
    assert filter_paths(text) == ["a.yml", "s/**"]
    assert run_scripts(text) == ["scripts/x.py", "scripts/y/z.py"]
