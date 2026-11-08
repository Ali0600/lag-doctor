"""lagdoctor/ imports only itself and what the installed runtime imports (docs/DESIGN.md, Testing;
docs/INSTALLER-PLAN.md, Stage 3).

The installer ships python.org's embeddable runtime, which holds no package beyond the standard library and leaves
part of that out: tkinter and Tcl/Tk, IDLE, turtle and turtledemo, venv, ensurepip and the tests. Smart App Control
would block psutil's unsigned native module anyway. The interpreter that runs these tests has all of those, and its
sys.stdlib_module_names names them, so what to allow comes from scripts/runtime_modules.json instead: the pinned
runtime's own answer to what it imports (scripts/runtime_modules.py asks it; CI's msi job asks again and fails when
the JSON is not the answer). A static scan of lagdoctor/**/*.py, on the syntax tree (prose in docstrings, comments
and strings does not count, while imports inside functions, try blocks and `if TYPE_CHECKING:` do), fails on:

- `import x` and `from x import y` whose top-level name is neither lagdoctor nor importable in that runtime, with
  psutil named and the reason given, and a standard-library module the runtime leaves out or cannot import said to
  be one; relative imports are lagdoctor's own;
- a call to `__import__` or `import_module` (importlib's, under any name) with such a module name, or with a name the
  scan cannot read: only a string literal, or an f-string whose literal start names the top-level module, can be
  checked.

Top-level names are enough for this runtime: under every package it ships, it has each submodule the full installer
has (docs/research/install-and-rights.md, section 10). The tests may still use psutil as an independent readout. Every
hit is reported as file:line.
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import runtime_modules  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "stdlib_only"
RUNTIME = json.loads((ROOT / "scripts" / "runtime_modules.json").read_text(encoding="utf-8"))
IMPORTABLE = frozenset(RUNTIME["importable"])
OWN = "lagdoctor"
WHY = {"psutil": "psutil's native module is unsigned, so Smart App Control would block it; read the value through "
                 "lagdoctor/winapi/ instead"}
DYNAMIC = {"__import__", "import_module"}


def allowed(name: str) -> bool:
    top = name.split(".")[0]
    return top == OWN or top in IMPORTABLE


def refusal(top: str) -> str:
    """Why the installed lag-doctor could not import the top-level module `top`, as a hit says it."""
    if top in WHY:
        return "which is not in the standard library: " + WHY[top]
    if top in RUNTIME["not_shipped"]:
        return ("a standard-library module that python.org's embeddable runtime leaves out "
                "(scripts/runtime_modules.json), so the installed lag-doctor could not import it")
    if top in RUNTIME["fails_to_import"]:
        return f"which python.org's embeddable runtime ships but cannot import ({RUNTIME['fails_to_import'][top]})"
    return (f"which is not in the standard library of python.org's embeddable runtime (Python "
            f"{RUNTIME['runtime']['version']}), the one the installed lag-doctor runs on, and that runtime ships no "
            f"other package")


def literal_module(node: ast.AST | None) -> str | None:
    """The module name a dynamic import asks for, as far as it is written down, or None when the scan cannot tell
    its top-level name (a variable, an expression, a relative name)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        name = node.value
    elif isinstance(node, ast.JoinedStr) and node.values and isinstance(node.values[0], ast.Constant):
        name = str(node.values[0].value)
        if len(node.values) > 1 and "." not in name:
            return None                 # f"psu{x}": the top-level name runs on into the formatted part
    else:
        return None
    return name if name and not name.startswith(".") else None


class _Scan(ast.NodeVisitor):
    def __init__(self, rel: str) -> None:
        self.rel = rel
        self.loaders = set(DYNAMIC)     # names bound to importlib.import_module (from importlib import ... as X)
        self.hits: list[str] = []

    def hit(self, node: ast.AST, msg: str) -> None:
        self.hits.append(f"{self.rel}:{node.lineno}: {msg}")

    def check(self, node: ast.AST, name: str) -> None:
        if not allowed(name):
            self.hit(node, f"imports {name}, " + refusal(name.split(".")[0]))

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.check(node, alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level == 0 and node.module:             # level > 0: a relative import, lagdoctor's own
            self.check(node, node.module)
            if node.module == "importlib":
                self.loaders |= {a.asname or a.name for a in node.names if a.name == "import_module"}
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        called = None
        if isinstance(func, ast.Name) and func.id in self.loaders:
            called = func.id                            # __import__(...), import_module(...) or its alias
        elif isinstance(func, ast.Attribute) and func.attr in DYNAMIC:
            called = func.attr                          # importlib.import_module(...), whatever the receiver
        if called is not None:
            name = literal_module(node.args[0] if node.args else None)
            if name is None:
                self.hit(node, f"{called}() with a module name the scan cannot read: write the name out as a "
                               "string, so this check can see what is imported")
            else:
                self.check(node, name)
        self.generic_visit(node)


def scan_source(text: str, rel: str) -> list[str]:
    try:
        tree = ast.parse(text, filename=rel)
    except SyntaxError as exc:
        return [f"{rel}:{exc.lineno}: could not be parsed, so it could not be checked: {exc.msg}"]
    scan = _Scan(rel)
    scan.visit(tree)
    return scan.hits


def scan_tree(package: str = OWN) -> tuple[list[str], list[str]]:
    files = sorted((ROOT / package).rglob("*.py"))
    hits: list[str] = []
    for f in files:
        rel = f.relative_to(ROOT).as_posix()
        hits += scan_source(f.read_text(encoding="utf-8"), rel)
    return hits, [f.relative_to(ROOT).as_posix() for f in files]


def test_lagdoctor_imports_only_the_standard_library_and_itself():
    hits, files = scan_tree()
    for mine in ("lagdoctor/capture/presentmon.py", "lagdoctor/capture/netflows.py", "lagdoctor/capture/inventory.py",
                 "lagdoctor/fixes/catalog.py", "lagdoctor/winapi/iphlp.py", "lagdoctor/winapi/memory.py"):
        assert mine in files, f"the scan never looked at {mine}"
    assert len(files) >= 50, files
    assert not hits, "\n".join(hits)


def test_the_scanner_reports_every_planted_form_with_its_line():
    text = (FIXTURES / "violations.py").read_text(encoding="utf-8")
    expected = {i for i, line in enumerate(text.splitlines(), 1) if re.search(r"#\s*expect:", line)}
    assert len(expected) == 28
    hits = scan_source(text, "lagdoctor/planted.py")
    lines = [int(h.split(":")[1]) for h in hits]
    assert sorted(lines) == sorted(expected), "\n".join(hits)        # each planted line exactly once
    assert all(h.startswith("lagdoctor/planted.py:") for h in hits)


def test_the_scanner_allows_the_safe_forms():
    text = (FIXTURES / "allowed.py").read_text(encoding="utf-8")
    assert scan_source(text, "lagdoctor/allowed.py") == []


def test_psutil_is_refused_with_the_reason_and_another_package_with_the_runtime():
    psutil_hit, = scan_source("import psutil\n", "lagdoctor/x.py")
    assert psutil_hit.startswith("lagdoctor/x.py:1: imports psutil,") and "Smart App Control" in psutil_hit
    other, = scan_source("from numpy import array\n", "lagdoctor/x.py")
    assert "imports numpy," in other and "embeddable runtime" in other


def test_a_module_the_runtime_leaves_out_or_cannot_import_is_refused_as_such():
    left_out, = scan_source("import tkinter.ttk\n", "lagdoctor/x.py")
    assert left_out.startswith("lagdoctor/x.py:1: imports tkinter.ttk, a standard-library module that python.org's "
                               "embeddable runtime leaves out"), left_out
    broken, = scan_source("from curses import wrapper\n", "lagdoctor/x.py")
    assert "ships but cannot import (ModuleNotFoundError: No module named '_curses')" in broken, broken
    removed, = scan_source("import cgi\n", "lagdoctor/x.py")        # still on Python 3.11's standard-library list
    assert "not in the standard library of python.org's embeddable runtime (Python 3.13.17)" in removed, removed


def test_the_runtime_list_is_the_pinned_runtimes_own_answer():
    assert runtime_modules.stale(RUNTIME) is None, runtime_modules.stale(RUNTIME)
    moved = {**runtime_modules.pinned(), "version": "3.13.18", "sha256": "0" * 64}
    reason = runtime_modules.stale(RUNTIME, moved)
    assert reason and "'3.13.17'" in reason and "'3.13.18'" in reason, reason
    assert "uv run python scripts/runtime_modules.py --zip" in reason and "--write" in reason
    verdicts = [set(RUNTIME["importable"]), set(RUNTIME["fails_to_import"]), set(RUNTIME["not_shipped"])]
    assert all(verdicts) and sum(map(len, verdicts)) == len(set().union(*verdicts))    # each module once


def test_the_list_holds_what_the_runtime_answered_on_2026_11_08():
    # Python 3.13.17's embeddable ZIP, asked by scripts/runtime_modules.py (docs/research/install-and-rights.md,
    # section 10). When the pin moves these change with the JSON: look at what moved before rewriting them.
    assert (len(RUNTIME["importable"]), len(RUNTIME["fails_to_import"]), len(RUNTIME["not_shipped"])) == (264, 4, 22)
    left_out = {"_tkinter", "tkinter", "idlelib", "turtle", "turtledemo", "venv", "ensurepip"}
    assert left_out <= set(RUNTIME["not_shipped"])
    assert set(RUNTIME["fails_to_import"]) == {"_ios_support", "curses", "pty", "tty"}
    shipped = ("http", "socketserver", "webbrowser", "ctypes", "sqlite3", "winsound", "winreg", "os")
    assert [RUNTIME["importable"].get(n) for n in shipped] == ["zip"] * 5 + ["extension", "built-in", "frozen"]
    assert "test" not in IMPORTABLE                     # the test package: off the list, and not in the ZIP


@pytest.mark.parametrize("call", ['importlib.import_module(".winapi", "lagdoctor")', "importlib.import_module(name)",
                                  'importlib.import_module(f"lagdoctor{tail}")', '__import__("lagdoctor" + tail)'])
def test_a_module_name_the_scan_cannot_read_is_refused_as_such(call):
    hit, = scan_source(call + "\n", "lagdoctor/x.py")
    assert "with a module name the scan cannot read" in hit, hit


def test_a_file_that_cannot_be_parsed_is_a_failure_not_a_pass():
    assert scan_source("def broken(:\n", "lagdoctor/x.py")[0].startswith("lagdoctor/x.py:1: could not be parsed")
