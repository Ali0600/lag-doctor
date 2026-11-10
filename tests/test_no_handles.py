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


# ---------------------------------------------------------------- starting programs (Play, M6) ---
# Play starts nothing itself: Explorer opens the store's link (lagdoctor/winapi/launch.py), so neither the store
# client nor the game is ever lag-doctor's child. Every other place that starts a program is listed here, by file and
# function, with what it may use; a new one fails until it is listed on purpose (docs/DESIGN.md, Principles). The
# place listed is where the program starts: a starter kept under another name (`run = subprocess.run`) is followed to
# each place that uses it, and lag-doctor's own launchers count as starters too.

SPAWN_MODULES = {
    "subprocess": {"Popen", "run", "call", "check_call", "check_output", "getoutput", "getstatusoutput"},
    "os": {"startfile", "system", "popen", "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve",
           "spawnvp", "spawnvpe", "execl", "execle", "execlp", "execlpe", "execv", "execve", "execvp", "execvpe",
           "posix_spawn", "posix_spawnp"},
    "asyncio": {"create_subprocess_exec", "create_subprocess_shell"},
    "asyncio.subprocess": {"create_subprocess_exec", "create_subprocess_shell"},
    "webbrowser": {"open", "open_new", "open_new_tab", "get", "WindowsDefault", "BackgroundBrowser"},
    "multiprocessing": {"Process", "Pool", "get_context", "Manager"},
    "concurrent.futures": {"ProcessPoolExecutor"},
    "_winapi": {"CreateProcess"},
}
SPAWN_MODULES["nt"] = SPAWN_MODULES["os"]               # the module behind os on Windows
# Ways to start a program by name on any object: Windows' own (a ctypes binding, getattr, a COM method's name in a slot
# table), an event loop's, and lag-doctor's own launchers.
WIN32_SPAWN = {"CreateProcessW", "CreateProcessA", "CreateProcessAsUserW", "CreateProcessAsUserA",
               "CreateProcessWithTokenW", "CreateProcessWithLogonW", "CreateProcessInternalW", "CreateProcess",
               "NtCreateUserProcess", "ZwCreateUserProcess", "RtlCreateUserProcess", "NtCreateProcessEx",
               "NtCreateProcess", "ShellExecuteW", "ShellExecuteA", "ShellExecuteExW", "ShellExecuteExA", "WinExec",
               "ShellExecute", "subprocess_exec", "subprocess_shell"}
OWN_LAUNCHERS = {"open_through_explorer", "launch_elevated", "relaunch_elevated"}
PRESENTMON = "lagdoctor.capture.presentmon"            # its `spawn` starts PresentMon: a launcher by that module only
SPAWN_ALLOWED = {
    "lagdoctor/app/main.py": {"open_local": {"startfile"}},               # a report or a folder, never a link
    "lagdoctor/app/recording.py": {"Recorder.start": {"Popen"},            # the recorder, the window's own child
                                   "Recorder._ask": {"launch_elevated"}},  # the relay, through the UAC prompt
    "lagdoctor/capture/gpumon.py": {"NvidiaSampler._run_smi": {"Popen"}},  # nvidia-smi, in a kill-on-close job
    "lagdoctor/capture/inventory.py": {"_DeviceGuardProbe.__init__": {"Popen"}},
    "lagdoctor/capture/presentmon.py": {"spawn": {"Popen"},               # PresentMon (Principles: the exception)
                                        "PresentMonSource._capture_once": {"spawn"},
                                        "PresentMonSource._terminate_session": {"spawn"}},
    "lagdoctor/relay/client.py": {"launch": {"launch_elevated"}},          # the relay, through the UAC prompt
    "lagdoctor/stores/launch.py": {"play": {"open_through_explorer"}},     # Play: Explorer opens the store's link
    "lagdoctor/winapi/elevation.py": {"<module>": {"ShellExecuteExW"},     # its binding, kept for the tests
                                      "launch_elevated": {"ShellExecuteExW"},
                                      "relaunch_elevated": {"launch_elevated"}},
    "lagdoctor/winapi/launch.py": {"<module>": {"ShellExecute"},           # Explorer's: Play's store link
                                   "open_through_explorer": {"ShellExecute"}},
}


def _target_names(node: ast.AST) -> list[str]:
    return [node.id] if isinstance(node, ast.Name) else []


class _SpawnScan(ast.NodeVisitor):
    def __init__(self, rel: str) -> None:
        self.rel = rel
        self.allowed = SPAWN_ALLOWED.get(rel, {})
        self.where: list[str] = []
        self.modules: dict[str, str] = {}               # a local name -> the spawning module it is bound to
        self.starters: dict[str, str] = {}              # a local name -> the starter it is bound to (run = subprocess.run)
        self.bases: set[int] = set()                     # ids of the nodes an attribute is read from
        self.hits: list[str] = []
        self.used: set[tuple[str, str]] = set()         # (place, name) entries of the allow-list that matched

    # ---- the first pass: every alias, wherever in the file it is made
    def bind(self, tree: ast.AST) -> None:
        nodes = list(ast.walk(tree))
        for node in nodes:
            if isinstance(node, ast.Attribute):
                self.bases.add(id(node.value))
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name in SPAWN_MODULES or a.name == PRESENTMON:
                        self.modules[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
            elif isinstance(node, ast.ImportFrom):
                for a in node.names:
                    full = f"{node.module}.{a.name}"
                    if full in SPAWN_MODULES or full == PRESENTMON:
                        self.modules[a.asname or a.name] = full
        for _ in range(2):                               # twice: an alias of an alias, in either order in the file
            for node in nodes:
                if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    name = self._starter(node.value)
                    module = self._dotted(node.value) if isinstance(node.value, (ast.Name, ast.Attribute)) else None
                    for t in targets:
                        for n in _target_names(t):
                            if name is not None:
                                self.starters[n] = name
                            elif module in SPAWN_MODULES or module == PRESENTMON:
                                self.modules[n] = module

    def _place(self) -> str:
        return ".".join(self.where) or "<module>"

    def hit(self, node: ast.AST, name: str, how: str) -> None:
        place = self._place()
        if name in self.allowed.get(place, ()):
            self.used.add((place, name))
            return
        self.hits.append(f"{self.rel}:{node.lineno}: {how} starts a program in {place}, which may not "
                         "(tests/test_no_handles.py, SPAWN_ALLOWED)")

    def _dotted(self, node: ast.AST) -> str | None:
        """'subprocess.Popen' for subprocess.Popen, sp.Popen (sp bound to subprocess) or concurrent.futures.X."""
        parts = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if not isinstance(node, ast.Name):
            return None
        parts.append(self.modules.get(node.id, node.id))
        return ".".join(reversed(parts))

    def _starter(self, node: ast.AST) -> str | None:
        """The starter an expression names: a spawning module's function, a Windows or own launcher by name, or a name
        bound to one; None for anything else. `a or b` names what either side names."""
        if isinstance(node, ast.BoolOp):
            return next((s for s in map(self._starter, node.values) if s), None)
        if isinstance(node, ast.Name):
            if node.id in self.starters:
                return self.starters[node.id]
            if node.id in OWN_LAUNCHERS or (node.id == "spawn" and self.rel == "lagdoctor/capture/presentmon.py"):
                return node.id
            return None
        if not isinstance(node, ast.Attribute):
            return None
        dotted = self._dotted(node)
        if dotted and "." in dotted:
            module, _, attr = dotted.rpartition(".")
            if attr in SPAWN_MODULES.get(module, ()) or (module == PRESENTMON and attr == "spawn"):
                return attr
        if node.attr in WIN32_SPAWN or node.attr in OWN_LAUNCHERS:
            return node.attr
        return None

    # ---- scopes, and annotations, which run nothing
    def visit_FunctionDef(self, node) -> None:
        for d in node.decorator_list:
            self.visit(d)
        self.visit(node.args)
        self.where.append(node.name)
        for stmt in node.body:
            self.visit(stmt)
        self.where.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self.visit(node.args)
        self.visit(node.body)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for n in node.decorator_list + node.bases + [k.value for k in node.keywords]:
            self.visit(n)
        self.where.append(node.name)
        for stmt in node.body:
            self.visit(stmt)
        self.where.pop()

    def visit_arguments(self, node: ast.arguments) -> None:
        for d in node.defaults + [d for d in node.kw_defaults if d is not None]:
            self.visit(d)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.visit(node.target)
        if node.value is not None:
            self.visit(node.value)

    # ---- the names
    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        names = SPAWN_MODULES.get(node.module or "", set())
        for a in node.names:
            if a.name in names or (a.name == "*" and names):
                self.hit(node, a.name, f"importing {node.module}.{a.name}")
            elif a.name in OWN_LAUNCHERS or (node.module == PRESENTMON and a.name == "spawn"):
                self.hit(node, a.name, f"importing {node.module}.{a.name}")

    def visit_Attribute(self, node: ast.Attribute) -> None:
        name = self._starter(node)
        if name is not None:
            self.hit(node, name, self._dotted(node) or name)
            return                                        # its base is the starter's module, not a second start
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if not isinstance(node.ctx, ast.Load):
            return
        name = self._starter(node)
        if name is not None:
            self.hit(node, name, f"{node.id} ({name})")
        elif self.modules.get(node.id) in SPAWN_MODULES and id(node) not in self.bases:
            self.hit(node, self.modules[node.id], f"the {self.modules[node.id]} module passed on as a value")

    def visit_Call(self, node: ast.Call) -> None:
        f = node.func
        if isinstance(f, ast.Name) and f.id in ("getattr", "hasattr") and len(node.args) >= 2 \
                and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
            self.bases.add(id(node.args[0]))             # a named attribute is read, the module is not passed on
            module = self._dotted(node.args[0])
            if module in SPAWN_MODULES and node.args[1].value in SPAWN_MODULES[module]:
                self.hit(node, node.args[1].value, f"getattr({module}, {node.args[1].value!r})")
        dynamic = (isinstance(f, ast.Name) and f.id == "__import__") or self._dotted(f) == "importlib.import_module"
        if dynamic and node.args and isinstance(node.args[0], ast.Constant) \
                and node.args[0].value in SPAWN_MODULES:
            self.hit(node, node.args[0].value, f"importing {node.args[0].value} by name")
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if self._dotted(node.value) == "sys.modules" and isinstance(node.slice, ast.Constant) \
                and node.slice.value in SPAWN_MODULES:
            self.hit(node, node.slice.value, f"sys.modules[{node.slice.value!r}]")
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and (node.value in WIN32_SPAWN or node.value in OWN_LAUNCHERS):
            self.hit(node, node.value, f"{node.value!r} by name")


def scan_spawns(text: str, rel: str) -> tuple[list[str], set[tuple[str, str]]]:
    """(hits, the allow-list entries used) for one file's source."""
    try:
        tree = ast.parse(text, filename=rel)
    except SyntaxError as exc:
        return [f"{rel}:{exc.lineno}: could not be parsed, so it could not be checked: {exc.msg}"], set()
    scan = _SpawnScan(rel)
    scan.bind(tree)
    scan.visit(tree)
    return scan.hits, scan.used


def test_lagdoctor_starts_programs_only_where_listed_and_every_listed_place_still_does():
    hits: list[str] = []
    used: set[tuple[str, str, str]] = set()
    files = sorted((ROOT / "lagdoctor").rglob("*.py"))
    for f in files:
        rel = f.relative_to(ROOT).as_posix()
        h, u = scan_spawns(f.read_text(encoding="utf-8"), rel)
        hits += h
        used |= {(rel, place, name) for place, name in u}
    assert len(files) >= 20
    assert not hits, "\n".join(hits)
    listed = {(rel, place, name) for rel, places in SPAWN_ALLOWED.items() for place, names in places.items()
              for name in names}
    assert listed - used == set(), f"listed but never used (remove them): {sorted(listed - used)}"


def test_the_spawn_scanner_reports_every_planted_form_with_its_line():
    text = (FIXTURES / "spawns.py").read_text(encoding="utf-8")
    expected = {i for i, line in enumerate(text.splitlines(), 1) if re.search(r"#\s*expect:", line)}
    assert len(expected) == 46
    hits, _ = scan_spawns(text, "lagdoctor/planted.py")
    lines = [int(h.split(":")[1]) for h in hits]
    assert sorted(lines) == sorted(expected), "\n".join(hits)          # each planted line exactly once


def test_the_spawn_scanner_allows_annotations_prose_and_lookalikes():
    text = (FIXTURES / "spawns_allowed.py").read_text(encoding="utf-8")
    assert scan_spawns(text, "lagdoctor/allowed.py") == ([], set())


def test_a_listed_place_may_use_only_its_own_names():
    code = ("import os, subprocess\n"
            "def spawn(argv):\n"
            "    subprocess.Popen(argv)\n"
            "    os.startfile(argv[0])\n"
            "def other(argv):\n"
            "    subprocess.Popen(argv)\n")
    hits, used = scan_spawns(code, "lagdoctor/capture/presentmon.py")
    assert [h.split(":")[1] for h in hits] == ["4", "6"]
    assert used == {("spawn", "Popen")}
    hits, _ = scan_spawns(code, "lagdoctor/capture/netmon.py")
    assert [h.split(":")[1] for h in hits] == ["3", "4", "6"]


def test_a_starter_kept_under_another_name_counts_where_it_is_used():
    code = ("import ctypes\n"
            "_shell32 = ctypes.windll.shell32\n"
            "_ShellExecuteExW = _shell32.ShellExecuteExW\n"
            "def launch_elevated(info):\n"
            "    return _ShellExecuteExW(info)\n"
            "def start_the_game(info):\n"
            "    return _ShellExecuteExW(info)\n")
    hits, used = scan_spawns(code, "lagdoctor/winapi/elevation.py")
    assert [h.split(":")[1] for h in hits] == ["7"]                     # the new caller, and only it
    assert used == {("<module>", "ShellExecuteExW"), ("launch_elevated", "ShellExecuteExW")}


def test_play_may_reach_explorer_only_through_its_own_function():
    code = ("from lagdoctor.winapi import launch as shell\n"
            "def play(link):\n"
            "    return shell.open_through_explorer(link)\n"
            "def do_play(entry):\n"
            "    return shell.open_through_explorer(entry.exe, entry.args)\n")
    hits, _ = scan_spawns(code, "lagdoctor/stores/launch.py")
    assert [h.split(":")[1] for h in hits] == ["5"]
    hits, _ = scan_spawns(code.replace("def play", "def other"), "lagdoctor/app/main.py")
    assert [h.split(":")[1] for h in hits] == ["3", "5"]
