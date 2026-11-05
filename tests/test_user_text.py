"""Text a player reads carries no internal citations such as "(WT 8)", "(LC 4.9)" or "(NE 7 row 9)".

Those shorthands point into docs/research and the review notes. They belong in comments and docstrings next
to the code, never in the report, the console or a fix's description. A static scan of the string literals of
every lagdoctor module, on the syntax tree, so docstrings and comments do not count while f-string parts do.
lagdoctor/winapi is left out: its strings are struct-layout labels and import-time errors for developers.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CITATION = re.compile(r"\b(?:WT|LC|NE|PM)[- ]?\d+(?:\.\d+)?\b|\bCC-\d+\b|\[inferred\]|\bDESIGN\b|docs/research")
MUST_SCAN = {"lagdoctor/analysis/rules.py", "lagdoctor/report/console.py", "lagdoctor/report/html.py",
             "lagdoctor/report/labels.py", "lagdoctor/fixes/gpu_preference.py", "lagdoctor/cli.py",
             "lagdoctor/setup.py", "lagdoctor/capture/session.py"}


def _docstrings(tree: ast.AST) -> set[int]:
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                out.add(id(first.value))
    return out


def citations(source: str, name: str = "<source>") -> list[tuple[str, str]]:
    """(where, citation) for each citation in a string literal of `source` that is not a docstring."""
    tree = ast.parse(source)
    docs = _docstrings(tree)
    return [(f"{name}:{node.lineno}: {node.value[:100]!r}", m.group(0))
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs
            for m in CITATION.finditer(node.value)]


def user_modules() -> list[Path]:
    return sorted(p for p in (ROOT / "lagdoctor").rglob("*.py") if "winapi" not in p.relative_to(ROOT).parts)


RESTART_WORDS = re.compile(r"\brestart|next time the game starts", re.I)


def restart_repeats(source: str, name: str = "<source>") -> list[str]:
    """FixSuggestion calls whose detail repeats the restart note the reports already add for `needs_restart`."""
    hits = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None))
                == "FixSuggestion"):
            continue
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        restart = kw.get("needs_restart", node.args[5] if len(node.args) > 5 else None)
        if restart is None or (isinstance(restart, ast.Constant) and restart.value is None):
            continue
        detail = kw.get("detail", node.args[1] if len(node.args) > 1 else None)
        text = "".join(c.value for c in ast.walk(detail) if isinstance(c, ast.Constant) and isinstance(c.value, str)) \
            if detail is not None else ""
        if RESTART_WORDS.search(text):
            hits.append(f"{name}:{node.lineno}: {text[:100]!r}")
    return hits


def test_the_restart_scan_flags_a_detail_that_repeats_the_restart_note():
    source = ('FixSuggestion("A", "Restart the game afterwards.", needs_restart="game")\n'
              'FixSuggestion(title="B", detail=f"{x} takes effect the next time the game starts", needs_restart="game")\n'
              'FixSuggestion("C", "Restart the game afterwards.")\n'
              'FixSuggestion("D", "Restart the game afterwards.", needs_restart=None)\n'
              'FixSuggestion("E", "A plain detail.", needs_restart="game")\n')
    assert [h.split(":")[1] for h in restart_repeats(source)] == ["1", "2"]


def test_no_fix_detail_repeats_the_restart_note_the_reports_add():
    hits = [h for p in user_modules() for h in restart_repeats(p.read_text(encoding="utf-8"),
                                                                 p.relative_to(ROOT).as_posix())]
    assert hits == [], "\n".join(hits)


def test_the_scan_finds_citations_in_strings_and_f_strings_but_not_in_docstrings_or_comments():
    source = ('"""Module doc (WT 8)."""\n'
              'def f(x):\n'
              '    """Function doc (LC 4.9)."""\n'
              '    # a comment (NE 7 row 9)\n'
              '    return f"{x} pings were lost (NE 2.4)", "plain words", "as CC-5 found", "DESIGN.md says"\n')
    assert sorted(c for _, c in citations(source)) == ["CC-5", "DESIGN", "NE 2.4"]


def test_no_internal_citations_in_text_a_player_reads():
    modules = user_modules()
    names = {p.relative_to(ROOT).as_posix() for p in modules}
    assert MUST_SCAN <= names, sorted(MUST_SCAN - names)
    assert not any(n.startswith("lagdoctor/winapi/") for n in names)
    hits = [f"{where}: {c}" for p in modules
            for where, c in citations(p.read_text(encoding="utf-8"), p.relative_to(ROOT).as_posix())]
    assert hits == [], "\n".join(hits)
