"""`lagdoctor sweep`: what it checks before it changes anything, the sweep it plans, resuming, and the recording it
asks watch for (lagdoctor/sweep/command.py, the parser in lagdoctor/cli.py)."""
from __future__ import annotations

import json
import os
import stat
import threading
import time
import uuid
from pathlib import Path

import pytest

from lagdoctor import cli, paths
from lagdoctor.capture import session as sess
from lagdoctor.capture import target as tgt
from lagdoctor.capture.base import Collector
from lagdoctor.fixes import game_setting
from lagdoctor.model import NOTE_MAX
from lagdoctor.sweep import command, plan, runner
from lagdoctor.winapi import elevation, power

RAW = b"[ScalabilityGroups]\r\nsg.ShadowQuality=0\r\nsg.StaticMeshQuality=3\r\n"
GAME = {"pid": 4242, "create_time": 133000000000000001, "name": "Game.exe", "exe": r"C:\Games\Game.exe",
        "window_title": "Game", "monitor_device": None, "adapter_luid": None, "how": "name", "candidates": []}


@pytest.fixture(autouse=True)
def _safe(monkeypatch):
    """As tests/test_cli.py: an unelevated owner (CI's runner is an administrator), no real UAC prompt, a capture lock
    of the test's own; and a PC plugged in with PresentMon ready."""
    monkeypatch.setattr(elevation, "is_elevated", lambda: False)

    def refuse(*a, **k):
        raise AssertionError("a test reached the real elevated launch")
    monkeypatch.setattr(elevation, "_ShellExecuteExW", refuse)
    monkeypatch.setattr(sess, "CAPTURE_LOCK_NAME", f"Local\\lagdoctor-test-{uuid.uuid4().hex}")
    monkeypatch.setattr(cli, "_presentmon_exe", lambda: (Path(r"C:\fake\PresentMon.exe"), ""))
    monkeypatch.setattr(power, "power_status", lambda: {"on_battery": False})


@pytest.fixture
def ini(tmp_path) -> Path:
    path = tmp_path / "Config" / "GameUserSettings.ini"
    path.parent.mkdir()
    path.write_bytes(RAW)
    return path


def args(*extra: str):
    return cli.build_parser().parse_args(["sweep", *extra])


def new_args(ini: Path, *extra: str):
    return args("--game", "Game.exe", "--settings", str(ini), "--try", "sg.StaticMeshQuality=1", *extra)


class Ran:
    """runner.run's stand-in: what it was given."""

    def __init__(self) -> None:
        self.calls: list[tuple[plan.Sweep, Path]] = []

    def __call__(self, sweep, folder, seams) -> int:
        self.calls.append((sweep, folder))
        return 0


@pytest.fixture
def ran(monkeypatch) -> Ran:
    r = Ran()
    monkeypatch.setattr(runner, "run", r)
    return r


def _main(a, lines: list[str], begin: bool = True) -> int:
    return command.main(a, seams=lambda: None, say=lines.append, ask=lambda prompt, default=False: begin)


def sweeps() -> list[Path]:
    return sorted(paths.sweeps_dir().iterdir())


# ----------------------------------------------------------------- parser --

@pytest.mark.parametrize("extra", [["--runs", "1"], ["--runs", "11"], ["--runs", "two"], ["--window", "29"],
                                   ["--window", "nan"], ["--window", "601"], ["--start-key", "ctrl+alt+?"]])
def test_the_parser_refuses_what_a_sweep_cannot_use(extra):
    with pytest.raises(SystemExit):
        args(*extra)


def test_the_parser_takes_a_sweep_and_a_resume():
    a = args("--game", "Game.exe", "--settings", "x.ini", "--try", "sg.X=1", "--try", "sg.Y=2", "--runs", "4",
             "--window", "90", "--no-warm-up", "--start-key", "ctrl+alt+k")
    assert (a.tries, a.runs, a.window, a.no_warm_up, a.start_key) == (["sg.X=1", "sg.Y=2"], 4, 90.0, True,
                                                                       "ctrl+alt+k")
    assert args("--resume").resume == "" and args("--resume", "C:\\x").resume == "C:\\x"
    assert args().resume is None


# ------------------------------------------------------------- refusals --

def test_an_elevated_terminal_is_refused(ini, monkeypatch, ran):
    monkeypatch.setattr(elevation, "is_elevated", lambda: True)
    lines: list[str] = []
    assert _main(new_args(ini), lines) == 2 and "administrator" in lines[0] and not ran.calls


@pytest.mark.parametrize("change,why", [
    (lambda ini: ["--game", "Game.exe", "--settings", str(ini)], "at least one --try"),
    (lambda ini: ["--game", "Game.exe", "--settings", str(ini.with_suffix(".txt")), "--try", "sg.X=1"],
     "must be an .ini file"),
    (lambda ini: ["--game", "Game.exe", "--settings", str(ini.with_name("Missing.ini")), "--try", "sg.X=1"],
     "There is no file"),
    (lambda ini: ["--game", "Game.exe", "--settings", str(ini), "--try", "sg.Missing=1"], "not in the settings file"),
    (lambda ini: ["--game", "Game.exe", "--settings", str(ini), "--try", "sg.StaticMeshQuality=3"], "already 3"),
    (lambda ini: ["--game", "Game", "--settings", str(ini), "--try", "sg.StaticMeshQuality=1"], "exe name"),
])
def test_a_sweep_that_cannot_be_planned_changes_nothing(ini, ran, change, why):
    lines: list[str] = []
    assert _main(args(*change(ini)), lines) == 2
    assert any(why in line for line in lines), lines
    assert ini.read_bytes() == RAW and sweeps() == [] and not ran.calls


def test_a_read_only_settings_file_is_refused(ini, ran):
    os.chmod(ini, stat.S_IREAD)
    try:
        lines: list[str] = []
        assert _main(new_args(ini), lines) == 2 and "read-only" in "\n".join(lines)
    finally:
        os.chmod(ini, stat.S_IREAD | stat.S_IWRITE)
    assert sweeps() == [] and not ran.calls


def test_a_running_game_is_refused(ini, monkeypatch, ran):
    monkeypatch.setattr(game_setting, "processes_named", lambda name, procs=None: [4242])
    lines: list[str] = []
    assert _main(new_args(ini), lines) == 2 and "Close it first" in "\n".join(lines)
    assert sweeps() == [] and not ran.calls


def test_a_folder_saved_into_another_apps_private_copy_is_refused(ini, monkeypatch, ran):
    seen = []

    def copy(folder=None):
        seen.append(folder)
        return paths.PrivateCopy(app="Claude_test", folder=r"C:\x\LocalCache\Local")
    monkeypatch.setattr(paths, "private_copy", copy)
    lines: list[str] = []
    assert _main(new_args(ini), lines) == cli.EXIT_PRIVATE_COPY and "private copy" in "\n".join(lines)
    assert seen == [ini.parent] and not ran.calls and ini.read_bytes() == RAW


def test_no_frame_timing_or_a_pc_on_battery_is_refused(ini, monkeypatch, ran):
    lines: list[str] = []
    monkeypatch.setattr(cli, "_presentmon_exe", lambda: (None, "PresentMon is not installed (test)"))
    assert _main(new_args(ini), lines) == cli.EXIT_NOT_READY and "not installed" in "\n".join(lines)
    monkeypatch.setattr(cli, "_presentmon_exe", lambda: (Path(r"C:\fake\PresentMon.exe"), ""))
    monkeypatch.setattr(power, "power_status", lambda: {"on_battery": True})
    assert _main(new_args(ini), lines) == 2 and "on battery" in "\n".join(lines)
    assert sweeps() == [] and not ran.calls and ini.read_bytes() == RAW


# --------------------------------------------------------- a new sweep --

def test_a_new_sweep_keeps_a_copy_saves_its_plan_and_runs_it(ini, ran):
    lines: list[str] = []
    assert _main(new_args(ini, "--runs", "2", "--window", "45"), lines) == 0
    (sweep, folder), = ran.calls
    assert folder.parent == paths.sweeps_dir() and folder.name.endswith("-Game")
    assert (folder / (plan.ORIGINAL_PREFIX + ini.name)).read_bytes() == RAW
    assert plan.load(folder) == sweep
    assert (sweep.game, sweep.settings_file, sweep.window_s, sweep.runs_per_variant, sweep.warm_up) == (
        "Game.exe", str(ini), 45.0, 2, True)
    assert sweep.start_key == plan.START_KEY and len(sweep.slots) == 2 * 3
    assert ini.read_bytes() == RAW                               # the runner changes the file, never the command
    said = "\n".join(lines)
    assert "A  as it was (sg.StaticMeshQuality=3)" in said and "B  sg.StaticMeshQuality=1" in said
    assert "the first 2 warm up" in said and "Ctrl+Alt+S" in said and str(folder) in said


def test_a_sweep_not_begun_changes_nothing_and_can_be_begun_later(ini, ran):
    lines: list[str] = []
    assert _main(new_args(ini), lines, begin=False) == 0
    (folder,) = sweeps()
    assert plan.load(folder).state == "stopped" and not ran.calls and ini.read_bytes() == RAW
    assert "lagdoctor sweep --resume" in lines[-1]
    assert _main(args("--resume"), lines) == 0 and ran.calls[0][1] == folder


# --------------------------------------------------------------- resume --

def _saved(ini: Path, name: str, state: str) -> Path:
    resolved, original = plan.resolve(RAW.decode(), [plan.parse_try("sg.StaticMeshQuality=1")])
    variants = plan.make_variants(resolved, original)
    import random
    s = plan.Sweep(game="Game.exe", settings_file=str(ini), window_s=60.0, runs_per_variant=2, warm_up=False,
                   start_key="ctrl+alt+s", seed=1, variants=variants, original=original,
                   slots=plan.make_slots(["A", "B"], 2, False, random.Random(1)), state=state)
    folder = paths.sweeps_dir() / name
    folder.mkdir()
    plan.save(s, folder)
    return folder


def test_resume_goes_on_with_the_newest_unfinished_sweep(ini, ran):
    older = _saved(ini, "20261108-200000-Game", "stopped")
    _saved(ini, "20261109-200000-Game", "finished")
    (paths.sweeps_dir() / "20261110-200000-Game").mkdir()          # a folder that holds no sweep is passed over
    lines: list[str] = []
    assert _main(args("--resume", "--start-key", "ctrl+alt+k"), lines) == 0
    (sweep, folder), = ran.calls
    assert folder == older and sweep.start_key == "ctrl+alt+k"
    assert "0 of 4 runs are done" in "\n".join(lines)


def test_resume_of_a_finished_sweep_shows_its_results(ini, ran):
    folder = _saved(ini, "20261109-200000-Game", "finished")
    lines: list[str] = []
    assert _main(args("--resume", str(folder)), lines) == 0 and not ran.calls
    assert lines[0].startswith("Results:") and "finished" in lines[-1]


def test_resume_takes_no_new_plan_and_needs_a_sweep(ini, ran):
    lines: list[str] = []
    assert _main(args("--resume", "--try", "sg.X=1", "--runs", "4"), lines) == 2
    assert "--runs" in lines[0] and "--try" in lines[0]
    assert _main(args("--resume"), lines) == 2 and "no unfinished sweep" in lines[-1]
    (paths.sweeps_dir() / "x").mkdir()
    (paths.sweeps_dir() / "x" / plan.STATE_NAME).write_text(json.dumps({"schema": "other"}), encoding="utf-8")
    assert _main(args("--resume", str(paths.sweeps_dir() / "x")), lines) == 2 and "another schema" in lines[-1]
    assert not ran.calls


def test_cli_main_runs_a_sweep_behind_the_private_copy_gate_and_settles_the_journal_first(ini, ran, monkeypatch):
    from lagdoctor.fixes.journal import Journal
    gates = []
    real = cli._private_copy_gate

    def gate(policy, *a, **kw):
        gates.append(policy)
        return real(policy, *a, **kw)
    monkeypatch.setattr(cli, "_private_copy_gate", gate)
    monkeypatch.setattr(cli, "_ask", lambda prompt, default=False: True)
    monkeypatch.setattr(command, "real_seams", lambda: None)
    # A sweep that died mid-write left its entry pending: the next sweep settles it before it plans anything.
    params = {"file": str(ini), "section": "ScalabilityGroups", "key": "sg.StaticMeshQuality", "value": "1",
              "game": "Game.exe"}
    left = Journal().begin("game_setting", "the test's file", params, "3", "1")
    assert cli.main(["sweep", "--game", "Game.exe", "--settings", str(ini), "--try", "sg.StaticMeshQuality=1"]) == 0
    assert gates == ["refuse"] and len(ran.calls) == 1
    assert Journal().get(left.id).state == "failed"            # the file still held 3: the change never happened


# ------------------------------------------------------ the recordings --

def _a_sweep(ini: Path) -> tuple[plan.Sweep, plan.Slot]:
    import random
    resolved, original = plan.resolve(RAW.decode(), [plan.parse_try("sg.StaticMeshQuality=1")])
    variants = plan.make_variants(resolved, original)
    s = plan.Sweep(game="Game.exe", settings_file=str(ini), window_s=60.0, runs_per_variant=2, warm_up=True,
                   start_key="ctrl+alt+s", seed=1, variants=variants, original=original,
                   slots=plan.make_slots(["A", "B"], 2, True, random.Random(1)))
    return s, s.slots[0]


@pytest.mark.parametrize("can_trace", [False, True])
def test_each_runs_command_line_is_one_watch_accepts(ini, tmp_path, can_trace):
    sweep, slot = _a_sweep(ini)
    argv = runner.watch_args(sweep, slot, tmp_path, can_trace)
    w = cli.build_parser().parse_args(["watch", *argv])
    assert (w.game, w.no_network, w.hotkey, w.elevate) == ("Game.exe", True, "none", not can_trace)
    assert w.out == (tmp_path / plan.RUNS_DIR).resolve() and len(w.note) <= NOTE_MAX
    long = plan.Sweep(**{**sweep.__dict__, "variants": [sweep.variants[0], plan.Variant("B", "x" * 400, ())]})
    assert len(runner.watch_args(long, plan.Slot(1, 0, "B", False), tmp_path, True)[-1]) <= NOTE_MAX


class _Quick(Collector):
    def __init__(self, name, result):
        super().__init__()
        self.name, self._result = name, result

    def run(self):
        self.set_status("ok", tool="fake")
        for _ in self.ticks(0.02):
            pass

    def result(self):
        return self._result


def test_a_sweep_run_records_through_watch_without_repointing_fix_or_saying_the_summary(ini, tmp_path, monkeypatch):
    monkeypatch.setattr(tgt, "find_target", lambda **kw: dict(GAME))
    monkeypatch.setattr(elevation, "can_trace", lambda: True)          # frame timing here, no relay
    monkeypatch.setattr(sess, "_collect_inventory", lambda target: {"adapters": []})

    class QuickSession(sess.Session):
        def __init__(self, target, opts, inventory=None, printer=None, observers=None, **kw):
            cols = [_Quick("system", {"columns": {"t": [0.0, 0.25], "cpu_total_pct": [10.0, 11.0]}}),
                    _Quick("frames", {"columns": {"t": [0.1, 0.12, 0.14], "frame_ms": [16.7, 16.6, 16.8]}})]
            ends = time.monotonic() + 10.0
            super().__init__(target, opts, collectors=cols, inventory=inventory or {"adapters": []},
                             alive=lambda: time.monotonic() < ends, printer=lambda line: None, observers=observers)
    monkeypatch.setattr(sess, "Session", QuickSession)
    lines: list[str] = []
    monkeypatch.setattr(cli, "say", lines.append)
    sweep, slot = _a_sweep(ini)
    events: list[tuple[str, dict]] = []
    stop = threading.Event()

    def on_event(kind, data):
        events.append((kind, data))
        if kind == "recording":
            sess.request_stop(stop, runner.WINDOW_DONE)
    seams = command.real_seams()
    argv = runner.watch_args(sweep, slot, tmp_path, seams.can_trace())
    assert seams.record(argv, stop, sess.Observers(on_event=on_event)) == 0
    kinds = [k for k, _ in events]
    assert kinds.index("recording") < kinds.index("saved")
    recording = dict(events[kinds.index("recording")][1])
    assert recording["frames"] is True and recording["hotkey"] is False
    (folder,) = (tmp_path / plan.RUNS_DIR).iterdir()
    assert dict(events[kinds.index("saved")][1])["session"] == folder.name
    assert (folder / cli.CAPTURE_NAME).is_file() and (folder / cli.REPORT_NAME).is_file()
    assert json.loads((folder / "summary.json").read_text(encoding="utf-8"))["kind"] == "watch"
    assert not (paths.home() / cli.LAST_SESSION).exists()           # `lagdoctor fix` still acts on the last watch
    said = "\n".join(lines)
    assert "Analysing..." in said and cli.REPORT_NAME not in said     # the summary, which names the report, is not said
    assert all(line.startswith("    ") for line in lines)
