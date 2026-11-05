"""Principle 1: lag-doctor never opens a handle to another process (docs/DESIGN.md, Principles; CLAUDE.md).

A static scan of lagdoctor/**/*.py, on the syntax tree, so prose in docstrings, comments and messages
does not count while aliases, keyword arguments and getattr strings do. It fails on:

- psutil.Process(...) with any argument (the constructor itself opens the process: windows-telemetry.md
  section 9.4), or psutil.Process kept for later; psutil.Process() for our own process is allowed;
- psutil.process_iter, psutil.pid_exists and psutil.Popen in any form (they open every / that / the new
  process); psutil.pids() is the one call allowed on other processes;
- OpenProcess (and NtOpenProcess / ZwOpenProcess) anywhere but lagdoctor/winapi/process.py.

Every hit is reported as file:line.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "sysmon" / "no_handles"
OPEN_PROCESS_ALLOWED = {"lagdoctor/winapi/process.py"}
OPEN_NAMES = {"OpenProcess", "NtOpenProcess", "ZwOpenProcess"}
PSUTIL_BANNED = {"process_iter", "pid_exists", "Popen"}


class _Scan(ast.NodeVisitor):
    def __init__(self, rel: str) -> None:
        self.rel = rel
        self.allow_open = rel in OPEN_PROCESS_ALLOWED
        self.psutil_names = {"psutil"}          # names bound to the psutil module
        self.process_names: set[str] = set()   # names bound to psutil.Process
        self.called: set[int] = set()          # ids of nodes in call position
        self.hits: list[str] = []

    def hit(self, node: ast.AST, msg: str) -> None:
        self.hits.append(f"{self.rel}:{node.lineno}: {msg}")

    def _is_psutil(self, node: ast.AST) -> bool:
        return isinstance(node, ast.Name) and node.id in self.psutil_names

    def _is_process(self, node: ast.AST) -> bool:
        return ((isinstance(node, ast.Attribute) and node.attr == "Process" and self._is_psutil(node.value))
                or (isinstance(node, ast.Name) and node.id in self.process_names))

    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            if a.name.split(".")[0] == "psutil":
                self.psutil_names.add(a.asname or "psutil")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module and node.module.split(".")[0] == "psutil":
            for a in node.names:
                if a.name == "Process":
                    self.process_names.add(a.asname or a.name)
                elif a.name in PSUTIL_BANNED or a.name == "*":
                    self.hit(node, f"imports psutil.{a.name}, which opens a handle to other processes")
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        if self._is_psutil(node.value):                          # mod = psutil
            for t in node.targets:
                if isinstance(t, ast.Name):
                    self.psutil_names.add(t.id)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if self._is_process(node.func):
            self.called.add(id(node.func))
            if node.args or node.keywords:
                self.hit(node, "psutil.Process(...) with an argument opens that process (the constructor "
                               "itself does); only psutil.Process() for our own process is allowed")
        if (isinstance(node.func, ast.Name) and node.func.id == "getattr" and len(node.args) >= 2
                and self._is_psutil(node.args[0]) and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in PSUTIL_BANNED | {"Process"}):
            self.hit(node, f"getattr(psutil, {node.args[1].value!r}) reaches a call that opens other processes")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if self._is_psutil(node.value):
            if node.attr in PSUTIL_BANNED:
                self.hit(node, f"psutil.{node.attr} opens a handle to other processes; use psutil.pids() "
                               "or winapi/ntquery.py")
            elif node.attr == "Process" and id(node) not in self.called:
                self.hit(node, "psutil.Process kept without a call: its later calls would escape this check")
        if node.attr in OPEN_NAMES and not self.allow_open:
            self.hit(node, f"{node.attr} outside lagdoctor/winapi/process.py")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in OPEN_NAMES and not self.allow_open:
            self.hit(node, f"{node.id} outside lagdoctor/winapi/process.py")
        if node.id in self.process_names and id(node) not in self.called:
            self.hit(node, "psutil.Process kept without a call: its later calls would escape this check")
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and node.value in OPEN_NAMES and not self.allow_open:
            self.hit(node, f"{node.value!r} by name (e.g. getattr) outside lagdoctor/winapi/process.py")
        self.generic_visit(node)


def scan_source(text: str, rel: str) -> list[str]:
    try:
        tree = ast.parse(text, filename=rel)
    except SyntaxError as exc:
        return [f"{rel}:{exc.lineno}: could not be parsed, so it could not be checked: {exc.msg}"]
    scan = _Scan(rel)
    scan.visit(tree)
    return scan.hits


def scan_tree(package: str = "lagdoctor") -> tuple[list[str], list[str]]:
    files = sorted((ROOT / package).rglob("*.py"))
    hits: list[str] = []
    for f in files:
        rel = f.relative_to(ROOT).as_posix()
        hits += scan_source(f.read_text(encoding="utf-8"), rel)
    return hits, [f.relative_to(ROOT).as_posix() for f in files]


def test_lagdoctor_never_opens_another_process():
    hits, files = scan_tree()
    for mine in ("lagdoctor/capture/sysmon.py", "lagdoctor/capture/target.py", "lagdoctor/winapi/ntquery.py",
                 "lagdoctor/winapi/pdh.py", "lagdoctor/winapi/windows.py", "lagdoctor/capture/base.py"):
        assert mine in files, f"the scan never looked at {mine}"
    assert len(files) >= 20, files
    assert not hits, "\n".join(hits)


def test_the_scanner_reports_every_planted_form_with_its_line():
    text = (FIXTURES / "violations.py").read_text(encoding="utf-8")
    expected = {i for i, line in enumerate(text.splitlines(), 1) if re.search(r"#\s*expect:", line)}
    assert len(expected) == 16
    hits = scan_source(text, "lagdoctor/planted.py")
    lines = [int(h.split(":")[1]) for h in hits]
    assert sorted(lines) == sorted(expected), "\n".join(hits)        # each planted line exactly once
    assert all(h.startswith("lagdoctor/planted.py:") for h in hits)


def test_the_scanner_allows_the_safe_forms():
    text = (FIXTURES / "allowed.py").read_text(encoding="utf-8")
    assert scan_source(text, "lagdoctor/allowed.py") == []


def test_open_process_is_allowed_only_in_winapi_process_py():
    code = 'k32.OpenProcess(0x1000, False, pid)\nf = getattr(k32, "OpenProcess")\n'
    assert scan_source(code, "lagdoctor/winapi/process.py") == []
    hits = scan_source(code, "lagdoctor/fixes/priority.py")
    assert [h.split(":")[:2] for h in hits] == [["lagdoctor/fixes/priority.py", "1"],
                                                ["lagdoctor/fixes/priority.py", "2"]]


def test_a_file_that_cannot_be_parsed_is_a_failure_not_a_pass():
    assert scan_source("def broken(:\n", "lagdoctor/x.py")[0].startswith("lagdoctor/x.py:1: could not be parsed")
