"""lagdoctor/ imports only the standard library and itself (docs/DESIGN.md, Testing; docs/INSTALLER-PLAN.md, Stage 3).

The installer ships python.org's embeddable runtime, which holds the standard library and nothing else, and Smart
App Control would block psutil's unsigned native module anyway. So a static scan of lagdoctor/**/*.py, on the syntax
tree (prose in docstrings, comments and strings does not count, while imports inside functions, try blocks and
`if TYPE_CHECKING:` do), fails on:

- `import x` and `from x import y` whose top-level name is neither lagdoctor nor in sys.stdlib_module_names, with
  psutil named and the reason given; relative imports are lagdoctor's own;
- a call to `__import__` or `import_module` (importlib's, under any name) with such a module name, or with a name the
  scan cannot read: only a string literal, or an f-string whose literal start names the top-level module, can be
  checked.

The tests may still use psutil as an independent readout. Every hit is reported as file:line.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "stdlib_only"
OWN = "lagdoctor"
WHY = {"psutil": "psutil's native module is unsigned, so Smart App Control would block it; read the value through "
                 "lagdoctor/winapi/ instead"}
DYNAMIC = {"__import__", "import_module"}


def allowed(name: str) -> bool:
    top = name.split(".")[0]
    return top == OWN or top in sys.stdlib_module_names


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
            top = name.split(".")[0]
            self.hit(node, f"imports {name}, which is not in the standard library: "
                           + WHY.get(top, "the installed lag-doctor runs on python.org's embeddable runtime, "
                                          "which has nothing else"))

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
    assert len(expected) == 21
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


@pytest.mark.parametrize("call", ['importlib.import_module(".winapi", "lagdoctor")', "importlib.import_module(name)",
                                  'importlib.import_module(f"lagdoctor{tail}")', '__import__("lagdoctor" + tail)'])
def test_a_module_name_the_scan_cannot_read_is_refused_as_such(call):
    hit, = scan_source(call + "\n", "lagdoctor/x.py")
    assert "with a module name the scan cannot read" in hit, hit


def test_a_file_that_cannot_be_parsed_is_a_failure_not_a_pass():
    assert scan_source("def broken(:\n", "lagdoctor/x.py")[0].startswith("lagdoctor/x.py:1: could not be parsed")
