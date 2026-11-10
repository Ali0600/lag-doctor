"""`lagdoctor sweep`: build a settings sweep from the command line, or go on with one, and run it.

    lagdoctor sweep --game ff7rebirth_.exe --settings "<the game's GameUserSettings.ini>" --try sg.StaticMeshQuality=1
    lagdoctor sweep --resume

A new sweep checks everything before it changes anything: not elevated, the game closed, mains power, PresentMon
ready, the settings file a plain local .ini that is not kept online only, not read-only and not saved into another
app's private copy of AppData, no earlier sweep's edit still in it, and every --try a value the file already has. Then
it keeps a byte copy of the file in the sweep's folder, saves the plan, asks once, and runs it (runner.py). A resume
first puts back whatever edit of its sweep is still in the file, whatever the sweep's state.

While a sweep runs, it holds a lock named after its settings file (one sweep of a file at a time), and closing its
console window puts the settings back at once when the game is closed (Windows gives a closing console about 5 s).
"""
from __future__ import annotations

import argparse
import hashlib
import os
import random
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from lagdoctor import __version__, cli, paths
from lagdoctor.fixes import game_setting
from lagdoctor.sweep import inifile, plan, results, runner

EXIT_BAD = 2
NEW_ONLY = ("game", "settings", "runs", "window")
LOCK_PREFIX = "Local\\lag-doctor-sweep-"


def main(args: argparse.Namespace, *, seams: Callable[[], runner.Seams] | None = None,
         say: Callable[[str], None] | None = None, ask: Callable[..., bool] | None = None) -> int:
    """`seams`, `say` and `ask` are the tests' (default: the real ones)."""
    say = say if say is not None else cli.say
    ask = ask if ask is not None else _yes
    make = seams if seams is not None else real_seams
    if cli._elevated():
        say("lag-doctor is running as administrator. A sweep changes your own game's settings file, so run it from a "
            "normal (not elevated) terminal.")
        return EXIT_BAD
    if args.resume is not None:
        given = [f"--{name.replace('_', '-')}" for name in NEW_ONLY if getattr(args, name) is not None]
        given += ["--try"] if args.tries else []
        given += ["--no-warm-up"] if args.no_warm_up else []
        if given:
            say(f"--resume goes on with a sweep as it was planned, so it takes no {', '.join(given)}.")
            return EXIT_BAD
        return _resume(args, make, say, ask)
    return _new(args, make, say, ask)


def _yes(prompt: str, default: bool = False) -> bool:
    """A yes or no typed at the terminal; the default for a bare Enter, and no when input has ended."""
    try:
        answer = input(prompt).strip().lower()
    except EOFError:
        return False
    return default if not answer else answer in ("y", "yes")


def _ready(game: str, say: Callable[[str], None]) -> int | None:
    """What a sweep needs before each start or resume; an exit code when something is missing (said), else None."""
    exe, why = cli._presentmon_exe()
    if exe is None:
        say(f"Frame timing is not available: {why}")
        say("A sweep ranks settings by frame timing, so nothing was changed.")
        return cli.EXIT_NOT_READY
    try:
        from lagdoctor.winapi import power
        on_battery = power.power_status().get("on_battery")
    except OSError:
        on_battery = None
    if on_battery is True:
        say("The PC is on battery. Plug it in first: on battery a laptop runs slower, and the runs would not compare.")
        return EXIT_BAD
    return None


def _journal():
    from lagdoctor.fixes.journal import Journal
    return Journal()


class SweepLock:
    """One sweep of a settings file at a time, in this Windows session: a named mutex named after the file's path."""

    def __init__(self, settings_file: str) -> None:
        digest = hashlib.sha256(runner._norm(settings_file).encode("utf-8")).hexdigest()[:16]
        self.name = LOCK_PREFIX + digest
        self._handle: Any = None

    def acquire(self) -> bool:
        from lagdoctor.winapi.elevation import hold_named_mutex
        self._handle = hold_named_mutex(self.name)
        return self._handle is not None

    def release(self) -> None:
        from lagdoctor.winapi.elevation import release_named_mutex
        handle, self._handle = self._handle, None
        release_named_mutex(handle)


class CloseCleanup:
    """While a sweep runs: when its console window is closed, put the settings back at once if the game is closed,
    one write at a time with the runner (runner.WRITE_LOCK), then let Windows end the process. Without a console (the
    tests) it does nothing."""

    def __init__(self, sweep: plan.Sweep, folder: Path, seams: runner.Seams) -> None:
        self.sweep, self.folder, self.seams = sweep, folder, seams
        self._routine: Any = None

    def __enter__(self) -> "CloseCleanup":
        try:
            self._routine = cli._HANDLER_ROUTINE(self.handle)
            if not cli._kernel32.SetConsoleCtrlHandler(self._routine, True):
                self._routine = None
        except (AttributeError, OSError):
            self._routine = None
        return self

    def __exit__(self, *exc: object) -> None:
        if self._routine is not None:
            cli._kernel32.SetConsoleCtrlHandler(self._routine, False)
            self._routine = None

    def handle(self, event: int) -> bool:
        if event != cli.CTRL_CLOSE_EVENT:
            return False                                  # Ctrl+C and Ctrl+Break: the runner's own handlers
        try:
            if runner.WRITE_LOCK.acquire(timeout=1.0):
                try:
                    if not self.seams.running(self.sweep.game):
                        runner.put_back_now(self.sweep, self.folder, self.seams, ask=False)
                finally:
                    runner.WRITE_LOCK.release()
        except Exception:  # noqa: BLE001 - the window is closing: the journal still holds what stayed
            pass
        return False                                      # Windows goes on and ends the process


def _run(sweep: plan.Sweep, folder: Path, seams: runner.Seams) -> int:
    with CloseCleanup(sweep, folder, seams):
        return runner.run(sweep, folder, seams)


def _new(args: argparse.Namespace, make: Callable[[], runner.Seams], say: Callable[[str], None],
         ask: Callable[..., bool]) -> int:
    if not args.game or not args.settings or not args.tries:
        say("A new sweep needs --game, --settings and at least one --try (or --resume, to go on with one). See "
            "`lagdoctor sweep --help`.")
        return EXIT_BAD
    try:
        game = game_setting.check_game(args.game)
        file = game_setting.check_file(os.path.abspath(args.settings))
    except ValueError as exc:
        say(f"Nothing was changed: {exc}.")
        return EXIT_BAD
    try:
        raw = game_setting.read_bytes(file, for_write=True)
        text, _enc = inifile.decode(raw)
    except FileNotFoundError:
        say(f"There is no file at {file}.")
        return EXIT_BAD
    except (OSError, ValueError) as exc:
        say(f"The settings file cannot be used: {exc}.")
        return EXIT_BAD
    try:
        tries = [plan.parse_try(t) for t in args.tries]
        resolved, original = plan.resolve(text, tries)
        variants = plan.make_variants(resolved, original)
        others = plan.other_values(text, original)
    except plan.SweepError as exc:
        say(f"Nothing was changed: {exc}.")
        return EXIT_BAD
    left = runner.outstanding(_journal(), file)
    if left:
        where = _sweep_of(file)
        how = (f"`lagdoctor sweep --resume \"{where}\"` puts it back (and goes on with that sweep)" if where
               else f"`{runner.UNDO}` puts it back")
        values = ", ".join(f"{e.params.get('key')}={e.after} (was {e.before})" for e in left)
        say(f"An earlier sweep's change is still in this settings file: {values}. A new sweep would take it for the "
            f"file as it was, so nothing was changed. {how[0].upper()}{how[1:]}, then start this one.")
        return EXIT_BAD
    try:
        copy = paths.private_copy(Path(file).parent)
    except OSError as exc:
        say(f"Where a file saved beside the settings file lands could not be checked ({exc}), so nothing was changed.")
        return cli.EXIT_PRIVATE_COPY
    if copy is not None:
        say("The settings file is in a folder whose new files Windows keeps in another app's private copy, so a "
            f"change lag-doctor made would not be the file the game reads. {paths.private_copy_advice(copy)}")
        return cli.EXIT_PRIVATE_COPY
    if game_setting.processes_named(game):
        say(f"{game} is running. Close it first: lag-doctor changes its settings file only while it is closed.")
        return EXIT_BAD
    code = _ready(game, say)
    if code is not None:
        return code
    lock = SweepLock(file)
    if not lock.acquire():
        say("Another lag-doctor window is running a sweep of this settings file, so nothing was changed.")
        return EXIT_BAD
    try:
        seed = random.SystemRandom().randrange(1 << 31)
        warm_up = not args.no_warm_up
        runs = args.runs or plan.RUNS_DEFAULT
        sweep = plan.Sweep(game=game, settings_file=file, window_s=float(args.window or plan.WINDOW_DEFAULT_S),
                           runs_per_variant=runs, warm_up=warm_up, start_key=args.start_key or plan.START_KEY,
                           seed=seed, variants=variants, original=original,
                           slots=plan.make_slots([v.id for v in variants], runs, warm_up, random.Random(seed)),
                           created_at=_now_iso(), tool_version=__version__, others=others)
        folder = _new_folder(game)
        (folder / (plan.ORIGINAL_PREFIX + Path(file).name)).write_bytes(raw)
        sweep.state = "stopped"                           # nothing has run yet: --resume begins it
        plan.save(sweep, folder)
        for line in intro(sweep, folder):
            say(line)
        if not ask("Begin the sweep? [Y/n] ", default=True):
            say("Nothing was changed. Begin it later with: lagdoctor sweep --resume")
            return 0
        return _run(sweep, folder, make())
    finally:
        lock.release()


def _resume(args: argparse.Namespace, make: Callable[[], runner.Seams], say: Callable[[str], None],
            ask: Callable[..., bool]) -> int:
    folder = Path(os.path.abspath(args.resume)) if args.resume else latest_unfinished()
    if folder is None:
        say("There is no unfinished sweep to go on with. Start one with --game, --settings and --try.")
        return EXIT_BAD
    try:
        sweep = plan.load(folder)
    except plan.SweepError as exc:
        say(f"{exc}.")
        return EXIT_BAD
    lock = SweepLock(sweep.settings_file)
    if not lock.acquire():
        say("Another lag-doctor window is running a sweep of this settings file, so nothing was changed.")
        return EXIT_BAD
    try:
        if runner.outstanding(_journal(), sweep.settings_file):
            say(f"This sweep's last setting is still in {sweep.settings_file}: putting it back first.")
            seams = make()
            if not runner.put_back(sweep, folder, seams):
                return 2
        if sweep.state in ("finished", "ended"):
            for line in results.text_lines(sweep):
                say(line)
            what = "finished" if sweep.state == "finished" else "ended and cannot go on"
            say(f"This sweep is {what}; its runs are in {folder}")
            return 0
        if args.start_key:
            sweep.start_key = args.start_key
        code = _ready(sweep.game, say)
        if code is not None:
            return code
        done = sum(1 for s in sweep.slots if plan.settled(sweep, s.n))
        say(f"The sweep of {sweep.game} in {folder}: {done} of {len(sweep.slots)} runs are done.")
        for v in sweep.variants:
            say(f"  {v.id}  {v.label}")
        others = [p for p in _unfinished() if p != folder]
        if others:
            say(f"(There {'is' if len(others) == 1 else 'are'} {len(others)} more unfinished: name one with "
                f"`lagdoctor sweep --resume FOLDER`, e.g. {others[0]}.)")
        if not ask("Go on with it? [Y/n] ", default=True):
            say("Not gone on with; the settings file holds what it held before the sweep.")
            return 0
        return _run(sweep, folder, make())
    finally:
        lock.release()


def intro(sweep: plan.Sweep, folder: Path) -> list[str]:
    """What the sweep will do, before anything changes."""
    counted = sweep.runs_per_variant
    lines = [f"A settings sweep of {sweep.game}: {len(sweep.variants)} settings, {counted} counted runs of each, "
             f"{sweep.window_s:.0f} s measured from the start key in each run."]
    lines += [f"  {v.id}  {v.label}" for v in sweep.variants]
    blocks: dict[int, list[str]] = {}
    for s in sweep.slots:
        blocks.setdefault(s.block, []).append(s.variant)
    order = " | ".join(" ".join(ids) for _, ids in sorted(blocks.items()))
    warm = (f"; the first {len(sweep.variants)} warm up and are not counted" if sweep.warm_up else "")
    lines.append(f"The order ({len(sweep.slots)} runs{warm}): {order}")
    lines.append(f"Each run: lag-doctor writes the setting into {sweep.settings_file} while the game is closed. You "
                 "start the game, load your save, go to your spot and press Enter here; then, back in the game, press "
                 f"{sweep.start_key.title()} at the spot, play as in every other run until the three-note chime, and "
                 "quit the game. Each run asks Windows for permission once (UAC), unless your account records frame "
                 "timing without it.")
    lines.append("Keep everything else the same for the whole sweep: the laptop's power or fan mode, the charger, the "
                 "same save and spot, and nothing else running. A run that moved (another window in front, another "
                 "program on the GPU, a power mode change, a GPU still heating up) does not count and is offered "
                 "again.")
    lines.append(f"Your settings file is put back as it was at the end; a copy of it as it is now is in {folder}. If "
                 "this window closes while the game runs, the file keeps the run's setting until `lagdoctor sweep "
                 f"--resume` or `{runner.UNDO}` puts it back.")
    return lines


def _unfinished() -> list[Path]:
    """Sweep folders whose sweep can go on, newest first (folder names start with their time)."""
    try:
        folders = sorted((p for p in paths.sweeps_dir().iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True)
    except OSError:
        return []
    out = []
    for folder in folders:
        try:
            if plan.load(folder).state in ("running", "stopped"):
                out.append(folder)
        except plan.SweepError:
            continue
    return out


def latest_unfinished() -> Path | None:
    """The newest sweep folder whose sweep can go on."""
    found = _unfinished()
    return found[0] if found else None


def _sweep_of(settings_file: str) -> Path | None:
    """The newest sweep folder of this settings file, whatever its state."""
    want = runner._norm(settings_file)
    try:
        folders = sorted((p for p in paths.sweeps_dir().iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True)
    except OSError:
        return None
    for folder in folders:
        try:
            if runner._norm(plan.load(folder).settings_file) == want:
                return folder
        except plan.SweepError:
            continue
    return None


def _new_folder(game: str) -> Path:
    base = paths.sweeps_dir()
    stem = f"{datetime.now():%Y%m%d-%H%M%S}-{paths.folder_stem(game)}"
    for n in range(1, 100):
        folder = base / (stem if n == 1 else f"{stem}-{n}")
        try:
            folder.mkdir()
            return folder
        except FileExistsError:
            continue
    raise OSError(f"no free folder name under {base} for {stem}")


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def real_seams() -> runner.Seams:
    """The seams of a sweep run on this PC, in the owner's own terminal."""
    from lagdoctor.app.sounds import Player
    from lagdoctor.model import Capture
    from lagdoctor.winapi import elevation
    from lagdoctor.winapi.hotkey import HotkeyListener
    player = Player(paths.home(), log=lambda text: None)

    def record(argv: list[str], stop: threading.Event, observers) -> int:
        try:
            watch = cli.build_parser().parse_args(["watch", *argv])
        except SystemExit:                                # a command line watch refuses: never this process's end
            return cli.EXIT_BAD_ARGS
        return cli.cmd_watch(watch, stop=stop, observers=observers, say=lambda line: cli.say("    " + line),
                             remember=False, show_summary=False)

    def ask(prompt: str) -> str | None:
        try:
            return input(prompt)
        except EOFError:
            return None

    def timer(seconds: float, fn: Callable[[], None]) -> threading.Timer:
        t = threading.Timer(seconds, fn)
        t.daemon = True
        return t

    return runner.Seams(say=cli.say, ask=ask, record=record,
                        running=lambda game: bool(game_setting.processes_named(game)), sleep=time.sleep,
                        hotkey=HotkeyListener, sound=player.play, load=Capture.load,
                        fix=game_setting.GameSettingFix(), journal=_journal(), can_trace=elevation.can_trace,
                        timer=timer, now_iso=_now_iso, read_text=game_setting.read_text)
