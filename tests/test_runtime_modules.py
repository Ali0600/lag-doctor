"""scripts/runtime_modules.py: how it reads the runtime's answer, checks it against the ZIP, and compares the JSON.

The real run unpacks the pinned ZIP and asks its own python.exe; CI's msi job does that on every push, its verdict in
the job summary. These tests pin each part on hand-written answers and listings, so a check that stops refusing turns
them red.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import runtime_modules as rm  # noqa: E402

FOLDER = r"C:\work\runtime-modules-x\runtime"
ARCHIVE = "python313.zip"
# A runtime ZIP's two listings, cut down: its own entries, and those of the archive inside it.
OUTER = ["python.exe", "python313.dll", ARCHIVE, "python313._pth", "_ctypes.pyd", "winsound.pyd", "sqlite3.dll",
         "LICENSE.txt"]
INNER = ["os.pyc", "json/__init__.pyc", "json/decoder.pyc", "curses/__init__.pyc", "__hello__.pyc",
         "site-packages/README.txt", "pydoc_data/_pydoc.css", "email/mime/__init__.pyc"]


def answer() -> dict:
    """What the unpacked runtime's python.exe writes for that ZIP: sys and winreg built in, os frozen, json and curses
    in the archive (curses failing to import), the two .pyd files, tkinter not found."""
    def mod(origin, error=None):
        return {"origin": origin, "error": error}
    return {"version": rm.EMBED_VERSION, "path": [FOLDER + "\\" + ARCHIVE, FOLDER], "isolated": 1,
            "builtin": ["sys", "winreg", "xxsubtype"],
            "modules": {"sys": mod("built-in"), "winreg": mod("built-in"), "os": mod("frozen"),
                        "json": mod(FOLDER + r"\python313.zip\json\__init__.pyc"),
                        "curses": mod(FOLDER + r"\python313.zip\curses\__init__.pyc",
                                      "ModuleNotFoundError: No module named '_curses'"),
                        "_ctypes": mod(FOLDER + r"\_ctypes.pyd"), "winsound": mod(FOLDER + r"\winsound.pyd"),
                        "tkinter": {"origin": None}}}


def checked(a: dict) -> list[str]:
    """Every problem the script would find in answer `a` for the ZIP above."""
    found, _, absent, problems = rm.read_answer(a, FOLDER, ARCHIVE)
    pyds, tops = rm.zip_modules(OUTER, INNER)
    return problems + rm.disagreements(a, found, absent, pyds, tops, FOLDER, ARCHIVE)


def content() -> dict:
    found, errors, absent, _ = rm.read_answer(answer(), FOLDER, ARCHIVE)
    return rm.listing(found, errors, absent)


# --------------------------------------------------------------------- reading ---

def test_the_zip_listings_name_its_extension_modules_and_top_level_modules():
    assert rm.zip_modules(OUTER, INNER) == ({"_ctypes", "winsound"}, {"os", "json", "curses", "__hello__"})


def test_an_answer_is_read_module_by_module():
    found, errors, absent, problems = rm.read_answer(answer(), FOLDER, ARCHIVE)
    assert found == {"sys": "built-in", "winreg": "built-in", "os": "frozen", "json": "zip", "curses": "zip",
                     "_ctypes": "extension", "winsound": "extension"}
    assert errors == {"curses": "ModuleNotFoundError: No module named '_curses'"}
    assert (absent, problems) == (["tkinter"], [])
    assert rm.listing(found, errors, absent) == {
        "about": rm.ABOUT, "runtime": rm.pinned(),
        "importable": {"_ctypes": "extension", "json": "zip", "os": "frozen", "sys": "built-in",
                       "winreg": "built-in", "winsound": "extension"},
        "fails_to_import": {"curses": "ModuleNotFoundError: No module named '_curses'"}, "not_shipped": ["tkinter"]}


ELSEWHERE = {"installed-lib": r"C:\Python313\Lib\json\__init__.py", "installed-dlls": r"C:\Python313\DLLs\json.pyd",
             "sibling-folder": FOLDER + r"-old\json.pyd",
             "lookalike-archive": FOLDER + r"\python313.zip.old\json\__init__.pyc",
             "source-in-folder": FOLDER + r"\json.py", "namespace": "a namespace package in " + FOLDER + r"\json"}


@pytest.mark.parametrize("origin", list(ELSEWHERE.values()), ids=list(ELSEWHERE))
def test_a_module_found_anywhere_but_the_runtimes_own_files_is_a_problem(origin):
    a = answer()
    a["modules"]["json"] = {"origin": origin, "error": None}
    found, _, absent, problems = rm.read_answer(a, FOLDER, ARCHIVE)
    assert "json" not in found and "json" not in absent
    assert problems == [f"json was found outside the runtime's own files, at {origin}"]


def test_a_lookup_that_failed_is_a_problem_not_an_absence():
    a = answer()
    a["modules"]["json"] = {"lookup_error": "ValueError: json.__spec__ is None"}
    found, _, absent, problems = rm.read_answer(a, FOLDER, ARCHIVE)
    assert "json" not in found and "json" not in absent
    assert problems == ["looking json up failed: ValueError: json.__spec__ is None"]


def test_an_error_names_the_runtime_folder_as_runtime_never_this_pcs_path():
    a = answer()
    a["modules"]["_ctypes"]["error"] = ("ImportError: DLL load failed while importing _ctypes: "
                                        + FOLDER.upper() + r"\libffi-8.dll")
    _, errors, _, _ = rm.read_answer(a, FOLDER, ARCHIVE)
    assert errors["_ctypes"] == r"ImportError: DLL load failed while importing _ctypes: <runtime>\libffi-8.dll"


# --------------------------------------------------------------- the cross-check ---

def test_an_answer_that_agrees_with_its_zip_has_no_problem():
    assert checked(answer()) == []


def _set(path: str, value):
    def change(a: dict) -> None:
        *keys, last = path.split("/")
        for k in keys:
            a = a[k]
        a[last] = value
    return change


def _drop(name: str):
    def change(a: dict) -> None:
        del a["modules"][name]
    return change


# Each way an answer can part from its ZIP, and the problem it must raise.
PARTINGS = [
    ("another-version", _set("version", "3.13.3"), "the runtime says it is Python 3.13.3, but the pin is"),
    ("path-beyond-its-own", _set("path", [FOLDER + "\\" + ARCHIVE, FOLDER, r"C:\Python313\Lib"]),
     "not its own ZIP and folder alone"),
    ("not-isolated", _set("isolated", 0), "(isolated: 0), not its own ZIP and folder alone"),
    ("builtin-found-frozen", _set("modules/winreg/origin", "frozen"),
     "found built in: [] more and ['winreg'] fewer than its sys.builtin_module_names"),
    ("pyd-off-the-list", _drop("winsound"), "the ZIP's ['winsound'] are not on the runtime's standard-library list"),
    ("pyd-not-found", _set("modules/winsound", {"origin": None}),
     "found as extension modules: [] more and ['winsound'] fewer than the ZIP's .pyd files"),
    ("below-the-archive-top", _set("modules/email", {"origin": FOLDER + r"\python313.zip\email\mime\__init__.pyc",
                                                     "error": None}),
     "found in python313.zip, but not at its top: ['email']"),
    ("archive-module-lookup-failed", _set("modules/json", {"lookup_error": "ValueError: no"}),
     "python313.zip holds ['json'], which the runtime did not find"),
    ("builtin-not-found", _set("modules/winreg", {"origin": None}),
     "not found, yet in the ZIP or built in: ['winreg']"),
    ("no-modules", _set("modules", {}),
     "the ZIP's ['_ctypes', 'winsound'] are not on the runtime's standard-library list"),
]


@pytest.mark.parametrize("change,expected", [p[1:] for p in PARTINGS], ids=[p[0] for p in PARTINGS])
def test_each_way_an_answer_can_part_from_its_zip_is_a_problem(change, expected):
    a = answer()
    change(a)
    problems = checked(a)
    assert any(expected in p for p in problems), problems


def test_the_runtime_is_asked_isolated_and_with_none_of_this_pcs_path(monkeypatch, tmp_path):
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    monkeypatch.setenv("PATH", r"C:\Program Files\Git\usr\bin;C:\tools\objc")
    calls = []
    monkeypatch.setattr(rm.subprocess, "run", lambda argv, **kw: calls.append((argv, kw)))
    rm.ask_child(tmp_path / "python.exe", tmp_path / "answer.json", tmp_path)
    (argv, kw), = calls
    assert argv[:4] == [str(tmp_path / "python.exe"), "-I", "-c", rm.CHILD] and argv[4] == str(tmp_path / "answer.json")
    assert kw["env"] == {"SystemRoot": r"C:\Windows", "PATH": r"C:\Windows\System32;C:\Windows"}
    assert kw["cwd"] == tmp_path and kw["timeout"] == rm.CHILD_TIMEOUT_S


# ---------------------------------------------------------- the JSON and the check ---

def test_the_json_comes_out_the_same_bytes_every_time():
    c = content()
    c["fails_to_import"]["curses"] = "ModuleNotFoundError: No module named 'curs\u00e9s'"
    data = rm.render(c)
    assert data.endswith(b"}\n") and b"\r" not in data and data.isascii()
    assert rm.render(json.loads(data)) == data
    assert list(json.loads(data)) == sorted(c)


def test_a_committed_json_is_compared_verdict_by_verdict():
    fresh = content()
    assert rm.compare(rm.render(fresh), fresh) == []
    old = content()
    old["importable"]["tkinter"] = "zip"
    old["not_shipped"].remove("tkinter")
    old["runtime"]["version"] = "3.13.16"
    assert rm.compare(rm.render(old), fresh) == ["runtime version: '3.13.16' -> '3.13.17'",
                                                 "tkinter: importable (zip) -> not shipped"]
    assert rm.compare(json.dumps(fresh, indent=2).encode(), fresh) == ["it holds the same verdicts, laid out otherwise"]
    for broken in (b"{not json", b'{"importable": ["json"]}', b"[]"):
        problems = rm.compare(broken, fresh)
        assert len(problems) == 1 and problems[0].startswith("it is not a listing this script writes"), problems


def test_the_command_writes_the_json_and_exits_by_the_check(tmp_path, monkeypatch, capsys):
    fresh = content()
    monkeypatch.setattr(rm, "ask_runtime", lambda zip_path: fresh)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    path = tmp_path / "runtime_modules.json"
    args = ["--zip", str(tmp_path / "embed.zip"), "--listing", str(path)]
    assert rm.main(args) == 1 and "it cannot be read" in capsys.readouterr().err      # no JSON yet
    assert rm.main(args + ["--write"]) == 0 and path.read_bytes() == rm.render(fresh)
    assert rm.main(args) == 0 and "is the pinned runtime's own answer" in capsys.readouterr().out
    stale = content()
    del stale["importable"]["winsound"]
    stale["not_shipped"].append("winsound")
    path.write_bytes(rm.render(stale))
    assert rm.main(args) == 1 and "winsound: not shipped -> importable (extension)" in capsys.readouterr().err


@pytest.mark.parametrize("error,code", [(rm.CannotStart("no ZIP"), 2), (rm.Disagreement("no"), 1)],
                         ids=["cannot-start", "disagrees"])
def test_a_runtime_that_cannot_be_asked_or_disagrees_with_its_zip_fails(tmp_path, monkeypatch, error, code):
    def ask(zip_path):
        raise error
    monkeypatch.setattr(rm, "ask_runtime", ask)
    path = tmp_path / "runtime_modules.json"
    assert rm.main(["--zip", str(tmp_path / "embed.zip"), "--listing", str(path), "--write"]) == code
    assert not path.exists()
