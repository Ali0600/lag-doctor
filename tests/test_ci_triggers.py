"""What CI's push trigger skips, and why that is safe (docs/DECISIONS.md, 2026-11-08).

A push to main that changes only docs/**, CLAUDE.md or .github/SECURITY.md starts no ci.yml run (its paths-ignore).
That is safe only while nothing a job runs reads those paths, and nothing else would keep it true: one new test line
reading docs/DESIGN.md would let a docs push break CI unseen. So these tests pin the list, keep pull requests
unfiltered (GitHub filters a pull request on its whole diff, and a path-skipped check that was ever required would stay
Pending), and scan everything a job runs for a name of an ignored path: the string literals of every module under
lagdoctor/, laglab/, scripts/ and tests/ (docstrings and comments do not count, f-string parts do), pyproject.toml,
installer/ and ci.yml itself, comments left out.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"
THIS = Path(__file__).resolve()

# The expectation, written here, never read back from ci.yml.
IGNORED = ["docs/**", "CLAUDE.md", ".github/SECURITY.md"]
# A name of an ignored path: docs as a folder (after anything but a name character, either slash, or alone), or one of
# the two files.
NAMES = re.compile(r"(?<![A-Za-z0-9_.-])docs(?=$|[/\\])|CLAUDE\.md|SECURITY\.md")
SCANNED = ("lagdoctor", "laglab", "scripts", "tests")
# Literals that name an ignored path without reading the real one: how many each file holds, and why.
ALLOWED = {
    "scripts/redfirst.py": (1, "COPY_DIRS: a red-first run copies docs/ into a temp tree; CI never runs it on this one"),
    "tests/test_redfirst.py": (2, "file names in the made-up tree the red-first copy is tested on"),
    "tests/test_user_text.py": (1, "the citation pattern that text a player reads must not hold"),
    "scripts/webview_spike.py": (1, "--help text that names the spike's plan"),
}
# Files whose absence from the scan would mean the scan lost its reach.
MUST_SCAN = {"lagdoctor/cli.py", "lagdoctor/setup.py", "laglab/app.py", "scripts/msi_build.py", "scripts/msi_check.py",
             "scripts/wheel_check.py", "tests/downloads.py", "tests/test_msi.py", "tests/conftest.py",
             "pyproject.toml", "installer/lag-doctor.wxs", "installer/lagdoctor.cmd", ".github/workflows/ci.yml"}


def _block(text: str, header: str, indent: int) -> str:
    """The lines under `header` (at `indent` spaces) that are indented deeper, up to the next line that is not."""
    deeper = " " * (indent + 1)
    m = re.search(rf"^{' ' * indent}{re.escape(header)}[^\n]*\n((?:(?:{deeper}[^\n]*|[ \t]*)\n)*)", text, re.M)
    assert m, f"no {header!r} at indent {indent}"
    return m.group(1)


def triggers(text: str) -> tuple[str, str]:
    """The `push:` and `pull_request:` sections of a workflow's top-level `on:`."""
    on = _block(text, "on:", 0)
    return _block(on, "push:", 2), _block(on, "pull_request:", 2)


def ignored(push: str) -> list[str]:
    items = _block(push, "paths-ignore:", 4)
    listed = re.findall(r'^      - "([^"\n]+)"$', items, re.M)
    entries = [line for line in items.splitlines() if line.strip()]
    assert len(listed) == len(entries), f"an entry is not a quoted path:\n{items}"
    return listed


def _docstrings(tree: ast.AST) -> set[int]:
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                out.add(id(first.value))
    return out


def literal_names(source: str) -> list[str]:
    """Each name of an ignored path in a string or bytes literal of `source` that is not a docstring."""
    tree = ast.parse(source)
    docs = _docstrings(tree)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)) and id(node) not in docs:
            text = node.value if isinstance(node.value, str) else node.value.decode("latin-1")
            out += [f"{text[max(0, m.start() - 30):m.end() + 30]!r}" for m in NAMES.finditer(text)]
    return out


def without_comments(text: str, kind: str) -> str:
    if kind == "xml":
        return re.sub(r"<!--.*?-->", "", text, flags=re.S)
    if kind == "hash":                                   # a whole-line comment, or one after whitespace
        return re.sub(r"(?m)(^|\s)#.*$", r"\1", text)
    return text


def config_texts() -> dict[str, str]:
    """What a job reads besides Python: comments out, and ci.yml without its own paths-ignore list."""
    ci = CI.read_text(encoding="utf-8")
    push, _ = triggers(ci)
    items = _block(push, "paths-ignore:", 4)
    assert ci.count(items) == 1
    out = {".github/workflows/ci.yml": without_comments(ci.replace(items, ""), "hash"),
           "pyproject.toml": without_comments((ROOT / "pyproject.toml").read_text(encoding="utf-8"), "hash")}
    for p in sorted((ROOT / "installer").iterdir()):
        if p.is_file():
            kind = "xml" if p.suffix == ".wxs" else "plain"
            out[p.relative_to(ROOT).as_posix()] = without_comments(p.read_text(encoding="utf-8"), kind)
    return out


def scan() -> dict[str, list[str]]:
    """Every scanned file, with the names of ignored paths it holds."""
    found = {}
    for top in SCANNED:
        for p in sorted((ROOT / top).rglob("*.py")):
            if "__pycache__" in p.parts or p.resolve() == THIS:
                continue
            found[p.relative_to(ROOT).as_posix()] = literal_names(p.read_text(encoding="utf-8"))
    for rel, text in config_texts().items():
        found[rel] = [f"{text[max(0, m.start() - 30):m.end() + 30]!r}" for m in NAMES.finditer(text)]
    return found


# ------------------------------------------------------------------------------------------------- the trigger ---

def test_a_push_to_main_skips_exactly_the_paths_no_job_reads():
    push, _ = triggers(CI.read_text(encoding="utf-8"))
    assert re.search(r"^    branches: \[main\]$", push, re.M), push
    assert ignored(push) == IGNORED
    assert not re.search(r"^    paths:", push, re.M), push


def test_a_pull_request_is_never_filtered_by_path():
    _, pull_request = triggers(CI.read_text(encoding="utf-8"))
    assert "paths" not in pull_request, pull_request


# ----------------------------------------------------------------------------------- nothing reads those paths ---

def test_nothing_a_job_runs_names_an_ignored_path():
    found = scan()
    assert MUST_SCAN <= set(found), sorted(MUST_SCAN - set(found))
    assert len(found) >= 150, len(found)                 # 181 files on 2026-11-08
    hits = {rel: names for rel, names in found.items() if names}
    unexpected = {rel: names for rel, names in hits.items() if len(names) != ALLOWED.get(rel, (0,))[0]}
    missing = {rel: n for rel, (n, _) in ALLOWED.items() if rel not in hits}
    assert not unexpected and not missing, (
        f"a file a job runs names a path ci.yml's push trigger ignores, so a docs-only push could change its outcome "
        f"unseen: read it from somewhere else, or (if it does not read the real file) allow it here with the reason. "
        f"Names: {unexpected}. Allowed but gone: {missing}")


# ------------------------------------------------------------------------------------------ the instruments ---

def test_the_name_pattern_finds_docs_paths_in_every_spelling_and_nothing_else():
    for text in ("docs/DESIGN.md", "(docs/LAUNCHER-PLAN.md", "|docs/research", "see docs/DESIGN.md", "docs",
                 r"docs\DESIGN.md", "a/docs/b", "CLAUDE.md", ".github/SECURITY.md"):
        assert NAMES.search(text), text
    for text in ("pydocs/x", "docstring", "my-docs/x", "docs.py", "docsx/", "the docs say", "claude.json"):
        assert not NAMES.search(text), text


def test_the_literal_scan_skips_docstrings_and_comments_but_reads_every_other_string():
    source = ('"""A module about docs/DESIGN.md."""\n'
              '# docs/x in a comment\n'
              'def f():\n    """docs/ in a docstring."""\n    return ROOT / "docs" / "DESIGN.md"\n'
              'X = f"{ROOT}/docs/y"\nY = b"CLAUDE.md"\nZ = "plain words"\n')
    assert len(literal_names(source)) == 3


def test_comments_are_left_out_of_the_config_scan():
    assert without_comments("<a/><!-- docs/x\n docs/y -->\n<b src='docs/z'/>", "xml") == "<a/>\n<b src='docs/z'/>"
    assert without_comments("# docs/x\nkey = 'docs/y'  # docs/z\nurl = 'a#b'\n", "hash") == (
        "\nkey = 'docs/y'  \nurl = 'a#b'\n")


def test_the_trigger_reader_takes_the_on_block_apart():
    text = ('name: X\n\non:\n  push:\n    branches: [main]\n    paths-ignore:\n      - "a/**"\n\n      - "b.md"\n'
            '  pull_request:\n\npermissions:\n  contents: read\n')
    push, pull_request = triggers(text)
    assert ignored(push) == ["a/**", "b.md"]
    assert pull_request.strip() == ""
