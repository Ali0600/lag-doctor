"""The command line: watch, analyze, check, fix, undo, setup.

Output is plain ASCII so it prints on any console code page. Nothing here
changes a setting without asking first: `fix` shows the before and after and
asks, and `undo` restores from the journal.

Paths are made absolute where they come in. A recording runs in the window that
started it, even when frame timing needs administrator rights: then the relay
(lagdoctor/relay/) runs PresentMon and the network trace through one UAC prompt
and sends their data back here. Once a recording has started, nothing the user
does at the console loses it: Ctrl+C stops it (a second press is only
answered), Ctrl+Break stops it too, and closing the window stops it and waits
a few seconds for the save.
"""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import json
import os
import re
import shutil
import sys
import threading
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from lagdoctor import __version__, paths

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CAPTURE_NAME = "capture.ldcap.json.gz"
DIAGNOSIS_NAME = "diagnosis.json"
REPORT_NAME = "report.html"
LAST_SESSION = "last-session.txt"          # the last watch: its folder, for `fix`
LAST_CHECKUP = "last-checkup.txt"          # the last check-up: its folder, for `fix`
ANALYZED_FROM = "analyzed-capture.txt"     # in an `analyze` folder: which capture its report is about
DEFAULT_HOTKEY = "ctrl+alt+l"
# watch's start key (M5, DECISIONS 2026-11-09): its press starts the measured window. Feel numbers the owner has not
# judged yet (docs/LAUNCHER-PLAN.md, open questions); the sweep's own default start key is held equal to this one.
DEFAULT_START_KEY = "ctrl+alt+s"
DEFAULT_WINDOW_S = 600.0       # analysed from the press: 10 minutes
WINDOW_MIN_S = 11.0            # analysis.crop.MIN_SPAN_S, the shortest part (a test holds them equal)
MAX_WINDOW_S = 3600.0          # "until I quit" is capped at an hour until compact frame storage exists
DEFAULT_START_BY_S = 1200.0    # with no press by then, the recording stops: 20 minutes
MAX_START_BY_S = 86400.0
# watch --wait-folder (Play's folder wait, M6): capture.target's WAIT_DEADLINE_S and MAX_WAIT_S, which a test holds
# equal (target is imported only once a watch runs).
WAIT_DEFAULT_S = 300.0
WAIT_MAX_S = 3600.0

# Exit codes of `watch`.
EXIT_BAD_ARGS = 2              # also: the game could not be found
EXIT_TARGET_CHANGED = 3        # the game exited during the UAC prompt (its PID now names another process, or none)
EXIT_BUSY = 4                  # another recording is running in this Windows session
EXIT_PRIVATE_COPY = 5          # any command: what it saves would land in another app's private copy (main)
EXIT_NOT_READY = 6             # frame timing was wanted, but PresentMon is missing or failed its check
EXIT_CANCELLED = 130           # the caller cancelled before the recording began (M4: the window's Cancel)

# What each command does when Windows keeps this process's files in another app's private copy
# (paths.private_copy): "refuse" what saves files other windows must find, "note" what only reads.
PRIVATE_COPY_POLICY = {"setup": "refuse", "watch": "refuse", "fix": "refuse", "undo": "refuse", "app": "refuse",
                       "sweep": "refuse", "check": "note", "analyze": "note"}

# Performance Log Users may trace frames but not enable Kernel-Network, the game-server tracker's provider
# (measured with scripts/etw_access.py on 2026-11-06; docs/research/install-and-rights.md, E1).
TRACKER_NOT_IN_GROUP = ("The game-server tracker still needs one UAC prompt: Windows does not let the Performance "
                        "Log Users group start it, so add `--elevate` to a recording to include it.")
# The machine-wide trace lock (capture/tracelock.py): the trace sessions of frame timing and the network trace have
# fixed names, so starting them stops another recording's, in any Windows session on this PC.
TRACE_BUSY = ("Not recorded: another lag-doctor recording on this PC, in another Windows session, is timing frames or "
              "tracing the network, and starting this one would stop it. Try again once it has ended.")
TRACE_TAKEN = ("(Another program holds the name lag-doctor uses to keep two recordings on this PC apart, so a "
               "lag-doctor recording in another Windows session could stop this one's frame timing and network "
               "trace.)")

# Console control events (wincon.h). Windows ends the process 5 s after CTRL_CLOSE_EVENT.
CTRL_C_EVENT, CTRL_BREAK_EVENT, CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT = 0, 1, 2, 5, 6
CLOSE_SAVE_WAIT_S = 4.5

_HANDLER_ROUTINE = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)     # PHANDLER_ROUTINE (consoleapi.h)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.SetConsoleCtrlHandler.argtypes = [_HANDLER_ROUTINE, wintypes.BOOL]
_kernel32.SetConsoleCtrlHandler.restype = wintypes.BOOL


# ----------------------------------------------------------------- parser --

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="lagdoctor", description="Find out why a PC game lags, and fix what can be "
                                "fixed safely. It watches from outside the game and never touches it.")
    p.add_argument("--version", action="version", version=f"lag-doctor {__version__}")
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")

    w = sub.add_parser("watch", help="record while you play, then explain the lag")
    pick = w.add_mutually_exclusive_group()
    pick.add_argument("--game", metavar="NAME", type=_exe_name, help="the game's exe name, e.g. eldenring.exe")
    pick.add_argument("--pid", type=_pid, help="the game's process id")
    pick.add_argument("--auto", action="store_true", help="pick the busiest game with a window (default)")
    pick.add_argument("--wait-folder", metavar="FOLDER", type=_wait_folder,
                      help="wait for the game to start from its install folder, then record it: start it from its "
                           "store once watch says it is waiting (frame timing's UAC prompt comes first)")
    w.add_argument("--wait-s", type=_wait_s, metavar="S",
                   help=f"with --wait-folder: how long to wait for the game (default {WAIT_DEFAULT_S:.0f}, at most "
                        f"{WAIT_MAX_S:.0f})")
    w.add_argument("--duration", type=_positive_float, metavar="S",
                   help="stop after S seconds, whatever the start key does (default: the start key's window or "
                        "--start-by ends it, or Ctrl+C)")
    w.add_argument("--server", metavar="IP", type=_ip, help="also ping this game server")
    w.add_argument("--no-network", action="store_true", help="skip the network checks")
    el = w.add_mutually_exclusive_group()
    el.add_argument("--elevate", action="store_true", help="get frame timing via one UAC prompt, without asking")
    el.add_argument("--no-elevate", action="store_true", help="record without frame timing rather than elevate")
    w.add_argument("--out", metavar="DIR", type=_abs_path, help="where to save the capture and report")
    w.add_argument("--hotkey", default=DEFAULT_HOTKEY, type=_hotkey,
                   help=f"the 'it lagged now' key (default {DEFAULT_HOTKEY}); 'none' to turn it off")
    w.add_argument("--start-key", default=DEFAULT_START_KEY, type=_watch_start_key, metavar="KEY",
                   help=f"press it in the game where measuring should start, once it is ready: pressed in time, only "
                        f"the --window after it is analysed, not the game starting up (default {DEFAULT_START_KEY}, "
                        "held with Ctrl or Alt); 'none' analyses the whole recording")
    w.add_argument("--window", type=_watch_window, default=DEFAULT_WINDOW_S, metavar="S",
                   help=f"seconds measured from the start key's press; the recording then stops (default "
                        f"{DEFAULT_WINDOW_S:.0f}, from {WINDOW_MIN_S:.0f} to {MAX_WINDOW_S:.0f})")
    w.add_argument("--start-by", type=_start_by, default=DEFAULT_START_BY_S, metavar="S",
                   help=f"with no start-key press S seconds in, stop and analyse all of it (default "
                        f"{DEFAULT_START_BY_S:.0f}; only when Windows gives the start key)")
    w.add_argument("--no-sound", action="store_true", help="no chimes while you play")
    w.add_argument("--control", choices=["stdio"],
                   help="for lag-doctor's window, which starts the recording this way: JSON lines on stdin and stdout")
    w.add_argument("--note", metavar="TEXT", type=_note,
                   help="what you changed for this recording, in your own words (e.g. \"shadows to low\"); shown in "
                        "the report and in the app's Compare")
    w.set_defaults(func=_watch_here)

    a = sub.add_parser("analyze", help="re-run the analysis on a saved capture")
    a.add_argument("capture", type=Path)
    a.add_argument("--from", dest="span_from", type=_time_into, metavar="TIME",
                   help="analyse from this time into the recording (seconds, 3:12 or 3m12s), leaving out what came "
                        "before it, such as the game starting up")
    a.add_argument("--to", dest="span_to", type=_time_into, metavar="TIME",
                   help="analyse up to this time into the recording (default: its end). With neither, a recording "
                        "measured from its start key analyses that part again; --from 0 analyses all of it")
    a.set_defaults(func=cmd_analyze)

    c = sub.add_parser("check", help="check this PC's settings without a game running")
    c.set_defaults(func=cmd_check)

    f = sub.add_parser("fix", help="apply fixes the last report suggested (asks first)")
    f.add_argument("ids", nargs="*", metavar="ID")
    f.add_argument("--list", action="store_true", help="list the fixes the last report suggested")
    f.add_argument("--yes", action="store_true", help="do not ask before each change")
    f.set_defaults(func=cmd_fix)

    u = sub.add_parser("undo", help="put back what lag-doctor changed")
    u.add_argument("ids", nargs="*", metavar="ID", help="a journal entry id or a fix id")
    u.add_argument("--all", action="store_true", help="undo every change, newest first")
    u.add_argument("--force", action="store_true", help="restore even a setting you changed by hand since")
    u.set_defaults(func=cmd_undo)

    s = sub.add_parser("setup", help="download and verify PresentMon, and explain admin rights")
    s.add_argument("--download", action="store_true", help="download PresentMon without asking")
    s.set_defaults(func=cmd_setup)

    ap = sub.add_parser("app", help="open lag-doctor's window: your recorded games and their results")
    ap.set_defaults(func=cmd_app)

    from lagdoctor.sweep import plan as sweep_plan
    sw = sub.add_parser("sweep", help="try a game's settings one after another and compare them (you play each run)",
                        description="lag-doctor writes each setting into the game's settings file while the game is "
                        "closed, you play each run from the same spot, and it compares the settings against their own "
                        "run-to-run spread. Your settings file is put back as it was at the end.")
    sw.add_argument("--game", metavar="NAME", type=_exe_name, help="the game's exe name, e.g. ff7rebirth_.exe")
    sw.add_argument("--settings", metavar="FILE", help="the game's settings file (.ini), e.g. its GameUserSettings.ini")
    sw.add_argument("--try", dest="tries", metavar="KEY=VALUE", action="append", default=[],
                    help="a setting to try, e.g. sg.ShadowQuality=1 (several at once: commas between); repeat for "
                         f"more, up to {sweep_plan.TRIES_MAX}")
    sw.add_argument("--runs", type=_sweep_runs, metavar="N",
                    help=f"counted runs of each setting (default {sweep_plan.RUNS_DEFAULT})")
    sw.add_argument("--window", type=_sweep_window, metavar="S",
                    help=f"seconds measured from the start key in each run (default {sweep_plan.WINDOW_DEFAULT_S:.0f})")
    sw.add_argument("--no-warm-up", action="store_true",
                    help="count every run, even each setting's first (by default the first round only warms up)")
    sw.add_argument("--start-key", type=_start_key, metavar="KEY",
                    help=f"the key you press in the game at your spot, with Ctrl, Alt or Win (default "
                         f"{sweep_plan.START_KEY})")
    sw.add_argument("--resume", nargs="?", const="", metavar="FOLDER",
                    help="go on with the last unfinished sweep, or the one in FOLDER; a finished one shows its results")
    sw.set_defaults(func=cmd_sweep)
    return p


def _positive_float(text: str) -> float:
    v = float(text)
    if not v > 0:                      # also refuses NaN
        raise argparse.ArgumentTypeError("must be more than 0")
    return v


MAX_TIME_INTO_S = 1e7          # analysis.frames.MAX_ABS_VALUE: a time past it in a capture is corrupt
_SECONDS = re.compile(r"\d+(?:\.\d+)?")
_M_SS = re.compile(r"(\d+):([0-5]\d(?:\.\d+)?)")
_H_MM_SS = re.compile(r"(\d+):([0-5]\d):([0-5]\d(?:\.\d+)?)")
_UNITS = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?")


MAX_TIME_TEXT = 32            # no time the report writes is longer; a longer one is refused before it is read


def _time_into(text: str) -> float:
    """A time into the recording as the report and the status line show it: seconds (192.5), minutes and seconds
    (3:12), hours too (1:03:12), or the report's own spelling (3m12s, 1h 02m 03s, 45s), kept to the millisecond as the
    report's own `--from`/`--to` write it. Anything else is refused, a negative time and a corrupt one (past
    MAX_TIME_INTO_S) included."""
    s = text.strip().lower().replace(" ", "")
    v = None
    if len(s) > MAX_TIME_TEXT:
        pass
    elif _SECONDS.fullmatch(s):
        v = float(s)
    elif m := _H_MM_SS.fullmatch(s):
        v = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    elif m := _M_SS.fullmatch(s):
        v = int(m[1]) * 60 + float(m[2])
    elif (m := _UNITS.fullmatch(s)) and any(m.groups()):
        v = int(m[1] or 0) * 3600 + int(m[2] or 0) * 60 + float(m[3] or 0)
    if v is None or not v <= MAX_TIME_INTO_S:
        shown = text if len(text) <= 40 else text[:40] + "..."
        raise argparse.ArgumentTypeError(f"{shown!r} is not a time into the recording: give seconds (192), minutes "
                                         "and seconds (3:12) or 3m12s")
    return round(v, 3)


def _ip(text: str) -> str:
    """An IP address only."""
    import ipaddress
    try:
        return str(ipaddress.ip_address(text.strip()))
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not an IP address") from None


def _pid(text: str) -> int:
    """A real process id: 0 would mean every process to PresentMon."""
    try:
        v = int(text.strip(), 10)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a process id") from None
    if not 0 < v <= 0xFFFFFFFF:
        raise argparse.ArgumentTypeError("a process id is a whole number above 0")
    return v


def _exe_name(text: str) -> str:
    name = text.strip()
    if not name or len(name) > 260 or any(ord(c) < 32 for c in name):
        raise argparse.ArgumentTypeError("give the game's exe name, e.g. eldenring.exe")
    return name


def _hotkey(text: str) -> str:
    """A key Windows can register, checked before anything starts, in its one spelling ('Alt + Ctrl+L' is
    'ctrl+alt+l', each modifier once, so it is as short as it can be); 'none' or 'off' for no key."""
    key = text.strip().lower()
    if key in ("", "none", "off"):
        return "none"
    from lagdoctor.winapi.hotkey import hotkey_name
    try:
        return hotkey_name(key)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _start_key(text: str) -> str:
    """A settings sweep's start key: watch's (_watch_start_key), and a sweep cannot do without one."""
    key = _watch_start_key(text)
    if key == "none":
        raise argparse.ArgumentTypeError("a sweep needs a start key (e.g. ctrl+alt+s)")
    return key


def _watch_start_key(text: str) -> str:
    """watch's start key: one Windows can register, held with Ctrl, Alt or Win; 'none' (or 'off') for none. A key on
    its own (or with Shift alone) would be taken from the game for the whole recording."""
    key = _hotkey(text)
    if key == "none":
        return key
    mods = {p.strip() for p in key.split("+")[:-1]}
    if not mods & {"ctrl", "control", "alt", "win"}:
        raise argparse.ArgumentTypeError(f"{text!r} would take that key from the game: hold it with ctrl, alt or win "
                                         "(e.g. ctrl+alt+s)")
    return key


def _watch_window(text: str) -> float:
    try:
        v = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number of seconds") from None
    if not WINDOW_MIN_S <= v <= MAX_WINDOW_S:                       # also refuses NaN
        raise argparse.ArgumentTypeError(f"give from {WINDOW_MIN_S:.0f} to {MAX_WINDOW_S:.0f} seconds")
    return v


def _start_by(text: str) -> float:
    try:
        v = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number of seconds") from None
    if not 0 < v <= MAX_START_BY_S:                                 # also refuses NaN
        raise argparse.ArgumentTypeError(f"give more than 0 and at most {MAX_START_BY_S:.0f} seconds")
    return v


def _wait_folder(text: str) -> str:
    """The game's install folder for --wait-folder, as target.check_folder gives it (a plain folder on a drive, never
    the Windows folder or a drive's root), refused here in its words before anything starts."""
    from lagdoctor.capture import target
    try:
        return target.check_folder(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _wait_s(text: str) -> float:
    """--wait-s as the relay's own command line takes it (relay.server._seconds: up to three decimals), so the
    relay is approved for exactly the wait watch runs."""
    from lagdoctor.relay.server import _seconds
    try:
        return _seconds(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"give a number of seconds more than 0 and at most {WAIT_MAX_S:.0f}, with "
                                         "at most three decimals") from None


def _note(text: str) -> str | None:
    """The player's note, as the capture keeps it (model.clean_note); None for an empty one. One longer than the
    capture keeps is refused here, before anything starts, rather than cut."""
    from lagdoctor.model import NOTE_MAX, clean_note
    note = clean_note(text, limit=None)
    if note is not None and len(note) > NOTE_MAX:
        raise argparse.ArgumentTypeError(f"a note can be at most {NOTE_MAX} characters, and this one has "
                                         f"{len(note)}: make it shorter")
    return note


def _abs_path(text: str) -> Path:
    if not text.strip():
        raise argparse.ArgumentTypeError("give a folder")
    return Path(text).resolve()


def _sweep_runs(text: str) -> int:
    from lagdoctor.sweep import plan
    try:
        v = int(text.strip(), 10)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number") from None
    if not plan.RUNS_MIN <= v <= plan.RUNS_MAX:
        raise argparse.ArgumentTypeError(f"give from {plan.RUNS_MIN} to {plan.RUNS_MAX} runs")
    return v


def _sweep_window(text: str) -> float:
    from lagdoctor.sweep import plan
    try:
        v = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number of seconds") from None
    if not plan.WINDOW_MIN_S <= v <= plan.WINDOW_MAX_S:            # also refuses NaN
        raise argparse.ArgumentTypeError(f"give from {plan.WINDOW_MIN_S:.0f} to {plan.WINDOW_MAX_S:.0f} seconds")
    return v


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 2
    if getattr(args, "control", None):
        # lag-doctor's window drives this recording: its channel takes stdout before anything can print there, then
        # runs the same steps as below with every line going to the window.
        from lagdoctor import control
        return control.serve(args)
    # First, before anything is written (the journal healing below writes): a file saved where the user's
    # other windows cannot see it is worse than none.
    code, private = _private_copy_gate(PRIVATE_COPY_POLICY[args.cmd],
                                       show=_app_tell if args.cmd == "app" else say)
    if code is not None:
        return code
    # Under a private copy (a reading command was let through) the journal is not healed: what it writes would
    # land in that copy, apart from the journal the user's own windows keep.
    if args.cmd in ("watch", "fix", "undo", "check", "sweep") and not private:
        _settle_journal()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        say("Stopped.")
        return 130


# ------------------------------------------------------------------- watch --

def _watch_here(args: argparse.Namespace) -> int:
    """`lagdoctor watch` typed in a console: cmd_watch with this console's own chimes, unless --no-sound."""
    return cmd_watch(args, sound=None if getattr(args, "no_sound", False) else console_sound())


def console_sound() -> Callable[[str], bool]:
    """The console's chimes: app/sounds.py's tones under lag-doctor's home, written before the recording starts, so a
    press never waits on a file. Its play never raises; a chime that cannot play returns False."""
    from lagdoctor.app import sounds
    player = sounds.Player(paths.home(), log=lambda text: None)
    player.prepare(CONSOLE_SOUNDS)
    return player.play


CONSOLE_SOUNDS = ("ready", "start", "done", "problem", "mark")    # every chime _Cues plays (a test holds them so)


class _Cues:
    """The console's chimes from the recording's events (DECISIONS 2026-11-09, the start key): a soft one when the
    start key is ready, a rising one when its press counts, three notes when the measuring has run (its window, or
    the recording's set length first), a low one when something went wrong (frame timing lost, a start key Windows
    would not give, no press in time), a blip per lag-key press. Never raises: a chime that cannot play is kept in
    `failed`, to be said once after the recording."""

    def __init__(self, play: Callable[[str], bool]) -> None:
        self.play = play
        self.failed: set[str] = set()
        self.measuring = False

    def on_event(self, kind: str, data: dict[str, Any]) -> None:
        from lagdoctor.capture import session as sess
        name = {"measuring": "start", "mark": "mark", "frames_lost": "problem"}.get(kind)
        if kind == "measuring":
            self.measuring = True
        elif kind == "stopping":
            reason = data.get("reason")
            name = {sess.WINDOW_ENDED: "done", sess.NO_START: "problem"}.get(reason)
            if reason == "duration" and self.measuring:     # the set length ended it, as the press's line said
                name = "done"
        elif kind == "start_key":                           # Windows gave the key (it is live now) or would not
            name = "ready" if data.get("registered") else "problem"
        if name is None:
            return
        try:
            played = self.play(name)
        except Exception:  # noqa: BLE001 - a chime is never worth the recording
            played = False
        if played is False:
            self.failed.add(name)


def cmd_watch(args: argparse.Namespace, *, stop: threading.Event | None = None,
              say: Callable[[str], None] | None = None, interactive: bool | None = None,
              observers: Any = None, launcher: Callable[..., Any] | None = None,
              cancel: threading.Event | None = None, tool_alone: tuple[tuple[int, int], ...] = (),
              tool_roots: Callable[[dict[int, Any]], Any] | None = None, remember: bool = True,
              show_summary: bool = True, sound: Callable[[str], bool] | None = None,
              start_advice: bool = True) -> int:
    """Record the game, save the capture and analyse it. What a caller other than this console gives it (M4: the
    app's control channel; a settings sweep's runs), each defaulting to the console's own behaviour:
    - `sound`: plays a chime by name (app/sounds.py's, True when it started) while the player plays: the start key's
      press, the window's end, a problem, the lag key. None (the default) is silent: `lagdoctor watch` in a console
      passes its own (_watch_here), while the app plays its own from the channel and a sweep from its own events;
    - `start_advice`: False leaves out watch's own words about the start key (the "Recording." line's, and what a key
      that measured nothing means and what to do): a sweep says its own, and fails such a run;
    - `remember`: False keeps `lagdoctor fix` on the report it uses now (a sweep's runs try settings on purpose);
    - `show_summary`: False leaves the analysis' summary out of what watch says (a sweep says its own line per run);
    - `stop`: set it (session.request_stop) to stop the recording, as Ctrl+C does;
    - `cancel`: set it to end watch before the recording begins, with nothing recorded and no session folder
      (EXIT_CANCELLED). It is looked at between the steps (before the game is looked for, once it is found, while the
      relay's prompt is up, before the recording starts) and by the session until everything has started
      (SessionOptions.cancel); once the "recording" event has gone out, only `stop` ends it;
    - `tool_alone`, `tool_roots`: more of lag-doctor's own processes (session.SessionOptions): its window;
    - `say`: every line watch says, the summary included (default: printed here). It must not raise: a line it cannot
      deliver is dropped, as say() drops one for a console that went away;
    - `interactive`: is a person at this console? None: asked only when stdin is a terminal, and shown the status
      line, with Ctrl+Break and closing the window handled; True: asked even without a terminal; False: never asked,
      and no status line or console handling (nobody is there);
    - `observers`: a session.Observers told of the recording's progress and events;
    - `launcher`: what starts the relay elevated (default: elevation.launch_elevated)."""
    from lagdoctor.capture import session as sess
    from lagdoctor.capture import target as tgt
    say = _teller(say)
    observers = observers if observers is not None else sess.Observers()
    start_key, lag_key = getattr(args, "start_key", "none") or "none", getattr(args, "hotkey", "none") or "none"
    # Both come through the parser (_hotkey) in their one spelling, so one key however written is one string here.
    if start_key != "none" and start_key == lag_key:
        say(f"The start key and the lag key are the same key ({start_key.title()}): give --start-key another one.")
        return EXIT_BAD_ARGS
    if getattr(args, "wait_s", None) is not None and getattr(args, "wait_folder", None) is None:
        say("--wait-s goes with --wait-folder: it is how long watch waits for the game to start from its folder.")
        return EXIT_BAD_ARGS
    # One recording at a time, checked before PresentMon or any ETW session can start (they share fixed
    # session names, so a second one would silently end the first one's tracing). This window holds the lock
    # for the whole recording, the relay's part included.
    lock = sess.CaptureLock()
    trace = _TraceGuard(say)          # the machine-wide lock, for another Windows session's recordings
    if not _take_lock(lock, say):
        return EXIT_BUSY
    relay = None
    try:
        with lock:
            exe, frames_why = _presentmon_exe()
            # Said before the game is looked for and long before the session is spent, not in the report
            # after it: a 14-minute recording was once made this way, every frame of it missing.
            if exe is None and not args.no_elevate:
                say(f"Frame timing is not available: {frames_why}")     # its reason says what to run
                other = "use --no-elevate instead of --elevate" if args.elevate else "add --no-elevate"
                say(f"Nothing was recorded. Start the recording again once that is done, or {other} to record "
                    "everything else without frame timing.")
                return EXIT_NOT_READY
            _check_cancel(cancel)
            folder, forms, target = getattr(args, "wait_folder", None), None, None
            wait_s = (getattr(args, "wait_s", None) or WAIT_DEFAULT_S) if folder is not None else None
            if folder is not None:
                # Play's order: the folder's forms and what already runs from it, then the UAC prompt, then the wait.
                forms, code, looked = _play_forms(folder, say=say, observers=observers)
                if code is not None:
                    return code
            else:
                observers.event("waiting")
                auto = args.auto or (args.game is None and args.pid is None)
                try:
                    target = tgt.find_target(name=args.game, pid=args.pid, auto=auto)
                except tgt.TargetNotFound as exc:
                    say(f"Could not find the game: {exc}")
                    say("Start the game first, or name it with --game NAME.exe or --pid PID.")
                    return EXIT_BAD_ARGS
                _watching(target, observers, say)
                _check_cancel(cancel)
            frames = exe is not None
            if frames:
                relay, frames, frames_why, code = _elevation(args, target, exe, frames_why, interactive, say=say,
                                                             trace=trace,
                                                             launcher=launcher, folders=forms, wait_s=wait_s)
                if code is not None:
                    return code
            if folder is not None:
                _check_cancel(cancel)
                target, code = _play_wait(folder, forms, looked, wait_s, relay, say=say, cancel=cancel,
                                          observers=observers)
                if code is not None:
                    return code
                _watching(target, observers, say)
            _check_cancel(cancel)
            # PresentMon or the network trace started in this window: held with the capture lock while it records (an
            # elevated window), or only honoured (a Performance Log Users member may set no owner the lock trusts). A
            # cancel that lands meanwhile is the session's to see (SessionOptions.cancel).
            if _traces_here(args, frames, relay) and (trace.hold() if _elevated() else trace.busy()):
                return EXIT_BUSY
            return _record(args, target, exe, frames, frames_why, relay, stop=stop, say=say,
                           interactive=interactive is not False, observers=observers, trace=trace,
                           tool_alone=tool_alone, tool_roots=tool_roots, cancel=cancel, remember=remember,
                           show_summary=show_summary, sound=sound, start_advice=start_advice)
    except Cancelled:
        say("Cancelled: nothing was recorded.")
        return EXIT_CANCELLED
    finally:
        if relay is not None:
            relay.close()
        trace.release()


def _record(args: argparse.Namespace, target: dict[str, Any], exe: Path | None, frames: bool, frames_why: str,
            relay: Any = None, *, stop: threading.Event | None = None, say: Callable[[str], None] | None = None,
            interactive: bool = True, observers: Any = None, tool_alone: tuple[tuple[int, int], ...] = (),
            tool_roots: Callable[[dict[int, Any]], Any] | None = None, cancel: threading.Event | None = None,
            remember: bool = True, show_summary: bool = True, sound: Callable[[str], bool] | None = None,
            start_advice: bool = True, trace: "_TraceGuard | None" = None) -> int:
    """Record, save and analyse. Ctrl+C, Ctrl+Break and closing the window all stop the recording; none of them can
    lose it once it is being stopped and saved. With a relay (relay/client.RelayLink), frame timing and the
    game-server tracker come through it, and it is let go as soon as the recording stops; so is the trace lock this
    window holds (`trace`). `stop`, `say`, `observers`, the tool processes, `remember`, `show_summary` and `sound` as in
    cmd_watch; `interactive` False: no status line and no console handling (nobody is there)."""
    from lagdoctor.capture import session as sess
    from lagdoctor.model import SourceStatus, clean_note
    say = _teller(say)
    observers = observers if observers is not None else sess.Observers()
    cues = None
    if sound is not None:
        cues = _Cues(sound)
        observers = sess.Joined(observers, sess.Observers(on_event=cues.on_event))
    hotkey = None if (args.hotkey or "").lower() in ("", "none", "off") else args.hotkey
    start_key = getattr(args, "start_key", None)
    start_key = None if (start_key or "none") == "none" else start_key
    # Through the capture service, a recording whose own longest end comes after the service's hold stops by itself
    # before it instead (relay/client.hold_left), and says so below: cut off at the hold, it would lose its last frames.
    held = _held_duration(relay, _longest_end(args, start_key))
    duration = args.duration if held is None else held
    # A set length of a part or less leaves no room for one after any press (Session.start would refuse them all), so
    # the key is not even registered: the recording is analysed whole, as with --start-key none, and said so once.
    off_key = start_key if start_key and _too_short_for_a_part(duration) else None
    if off_key:
        start_key = None
    opts = sess.SessionOptions(duration_s=duration, network=not args.no_network, server_ip=args.server,
                               hotkey=hotkey, presentmon_exe=exe, frames=frames, relay=relay,
                               tool_procs=relay.tool_procs() if relay is not None else ())
    opts.tool_alone, opts.tool_roots = tuple(tool_alone), tool_roots
    opts.cancel = cancel                              # honoured by the session until the recording has begun
    if start_key:                 # its press starts the measured window (DECISIONS 2026-11-09, the start key)
        opts.start_key = start_key
        opts.window_s = getattr(args, "window", DEFAULT_WINDOW_S)
        opts.start_by_s = getattr(args, "start_by", DEFAULT_START_BY_S)
        opts.start_advice = start_advice
    out_dir = sess.session_dir(args.out or paths.sessions_dir(), target)
    cap_path = out_dir / CAPTURE_NAME
    if not frames:
        say(f"Frame timing is off for this recording: {frames_why}")
    if start_key and start_advice:
        say(_recording_line(start_key, hotkey, opts.window_s, opts.start_by_s, args.duration, chimes=cues is not None))
    else:
        say("Recording. Play normally" + (f"; press {hotkey.title()} the moment it lags" if hotkey else "")
            + "; Ctrl+C stops." + (f" Stops by itself after {args.duration:.0f} s." if args.duration else ""))
    if held is not None:
        from lagdoctor.relay import protocol
        say(f"The capture service records for at most {protocol.hold_text()} at a time, so this recording stops by "
            f"itself after {sess.span_words(held)}.")
    if off_key:
        say(f"{off_key.title()} is off for this recording: a part needs at least {WINDOW_MIN_S:.0f} s after the press, "
            f"and the recording stops after {duration:g} s, so all of it is analysed.")
    stop = stop if stop is not None else threading.Event()
    saved = threading.Event()
    printer = None if interactive else _silent        # None: the session draws its status line here
    # With a relay the network trace is the relay's, and the relay stops it when this window goes away. The console's
    # handling is made once Ctrl+C is guarded: making it imports the network trace's modules, which takes a while.
    with sess.CtrlC(stop, say) as guard, (
            _ConsoleControl(stop, saved, network=opts.network and relay is None, say=say) if interactive
            else contextlib.nullcontext()):
        try:
            try:
                cap = sess.Session(target, opts, printer=printer, observers=observers).run(stop, ctrl_c=guard)
            finally:
                if trace is not None:
                    trace.release()                   # its trace sessions have stopped: another recording may start
                if relay is not None:
                    relay.close()
            if cap.target.get("stop_reason") == sess.CANCELLED:
                _remove_empty_dir(out_dir)            # nothing was saved in it
                raise Cancelled()
            if not frames:
                cap.sources["frames"] = SourceStatus("off", frames_why)
            # The note is the recorder's alone: it never goes to the relay, and it is kept only in the capture.
            cap.note = clean_note(getattr(args, "note", None))
            n = len(cap.frames.get("t") or [])
            observers.event("saving", frames=n)
            cap.notes.extend(note for note in observers.notes() if note not in cap.notes)   # failed since it was made
            say(f"Saving {n:,} frames..." if n else "Saving the capture...")
            cap.save(cap_path)
            observers.event("saved", session=out_dir.name)
        finally:
            saved.set()
    if cues is not None and cues.failed:
        say("(A chime could not be played during the recording: check the PC's sound, or record with --no-sound and "
            "keep an eye on the clock.)")
    return _analyze_saved(cap, cap_path, out_dir, say=say, remember=remember, show_summary=show_summary)


def _longest_end(args: argparse.Namespace, start_key: str | None) -> float | None:
    """How long a recording may run by its own rules, in seconds from its start: its set length (--duration), else with
    a start key a press at the last moment (--start-by), its window and the tail after it; None when only a stop, the
    game's end or a start key Windows would not give ends it."""
    from lagdoctor.capture import session as sess
    if args.duration:
        return args.duration
    if start_key:
        return (getattr(args, "start_by", DEFAULT_START_BY_S) + getattr(args, "window", DEFAULT_WINDOW_S)
                + sess.WINDOW_TAIL_S)
    return None


def _held_duration(relay: Any, longest: float | None) -> float | None:
    """The set length a recording through the capture service takes so it stops itself before the service's hold
    ends (relay/client.hold_left), when its own longest end (`longest`, _longest_end; None: none) would come later:
    whole minutes from a minute up, else whole seconds, never below 1 s, and never longer than its own. None when the
    hold does not bind it, and for the elevated relay's link or none, which no hold bounds."""
    from lagdoctor.relay import client
    left = client.hold_left(relay)
    if left is None:
        return None
    held = max(1.0, (left // 60) * 60 if left >= 60 else left // 1)
    return held if longest is None or held < longest else None


def _too_short_for_a_part(duration: float | None) -> bool:
    """Is a set length no longer than the shortest part? The key is offered only after the recording has started, so
    no press then leaves a whole part before the end."""
    return duration is not None and duration <= WINDOW_MIN_S


def _recording_line(start_key: str, hotkey: str | None, window_s: float, start_by_s: float | None,
                    duration: float | None, *, chimes: bool) -> str:
    """The "Recording." line of a recording with a start key: when to press it, and what that measures. The press
    counts only once the session says the key is ready (it registers the key after reading the PC's settings)."""
    from lagdoctor.capture.session import span_words
    key = start_key.title()
    what = ("measuring for the rest of the recording" if duration and duration <= window_s
            else f"{span_words(window_s)} of measuring")
    line = (f"Recording. Once {key} is ready (said below" + (", with a short chime" if chimes else "") + "), go to the "
            f"spot you want measured and press it to start {what}" + (", with a rising chime" if chimes else "")
            + ", so the startup before it is not counted." + (f" Press {hotkey.title()} the moment it lags." if hotkey
                                                              else "") + " Ctrl+C stops.")
    if duration:
        line += f" Stops by itself after {duration:.0f} s."
    if start_by_s and (not duration or start_by_s < duration):
        line += f" With no {key} press within {span_words(start_by_s)}, it stops and analyses all of it."
    return line


def _analyze_saved(cap, cap_path: Path, out_dir: Path, *, say: Callable[[str], None] | None = None,
                   remember: bool = True, show_summary: bool = True) -> int:
    """A watch's analysis: the part its start key measured (analysis/crop.key_part), else the whole recording, which
    both reports then say. The capture is already on disk, so a Ctrl+C here says where it is."""
    from lagdoctor.analysis import crop
    say = _teller(say)
    part, _ = crop.key_part(cap)
    try:
        return analyze_and_report(cap, cap_path, out_dir, remember=remember, kind="watch", show_summary=show_summary,
                                  say=say, span=None if part is None else (*part, "key"))
    except KeyboardInterrupt:
        say(f"Stopped before the analysis finished. The capture is saved at {cap_path}; analyse it with: "
            f"lagdoctor analyze \"{cap_path}\"")
        return 130


def _take_lock(lock, say: Callable[[str], None] | None = None) -> bool:
    say = _teller(say)
    try:
        if lock.acquire():
            return True
        say("Another lag-doctor recording is already running (in another window). Stop it first, then try again.")
    except OSError as exc:
        say(f"lag-doctor could not check whether another recording is running ({exc}), so it did not start one.")
    return False


def _trace_lock():
    """The machine-wide trace lock an elevated window holds while it records PresentMon or the network trace itself
    (capture/tracelock.py), owned by Administrators: only an elevated token may set that owner."""
    from lagdoctor.capture import tracelock
    return tracelock.TraceLock(tracelock.ADMINISTRATORS)


def _traces_here(args: argparse.Namespace, frames: bool, relay: Any) -> bool:
    """Does this window start a trace session itself? PresentMon, for frame timing with no relay link; the network
    trace, with no link and the network on, for a token that may start or stop trace sessions (one that may not could
    not stop another recording's either)."""
    if relay is not None:
        return False
    if frames:
        return True
    if args.no_network:
        return False
    try:
        from lagdoctor.winapi import elevation
        return elevation.can_trace()
    except OSError:
        return True                                   # cannot tell: honour the lock


class _TraceGuard:
    """One watch's side of the machine-wide trace lock (capture/tracelock.py), for another Windows session's lag-doctor
    recording, which starting this one's trace sessions would stop. busy() checks it before the capture service is
    asked and before the UAC prompt; hold() takes it (an elevated window) for a recording that starts them here. Each
    says why watch must not go on; that another program holds the lock's name is said once."""

    def __init__(self, say: Callable[[str], None]) -> None:
        self.say = say
        self.told = False                             # TAKEN was said
        self.lock = None                              # the tracelock.TraceLock this window holds

    def _judged(self, state: str) -> bool:
        from lagdoctor.capture import tracelock
        if state == tracelock.BUSY:
            self.say(TRACE_BUSY)
            return True
        if state == tracelock.TAKEN and not self.told:
            self.told = True
            self.say(TRACE_TAKEN)
        return False

    def _unknown(self, exc: OSError) -> bool:
        self.say(f"lag-doctor could not check whether another recording runs on this PC ({exc}), so it did not "
                 "start one.")
        return True

    def busy(self) -> bool:
        """True (said) when watch must not go on: another recording holds the lock, or Windows could not say."""
        from lagdoctor.capture import tracelock
        try:
            return self._judged(tracelock.check())
        except OSError as exc:
            return self._unknown(exc)

    def hold(self) -> bool:
        """Take the lock for this window's recording (release() lets it go); True (said) as busy() is."""
        from lagdoctor.capture import tracelock
        lock = _trace_lock()
        try:
            state = lock.acquire()
        except OSError as exc:
            return self._unknown(exc)
        if state == tracelock.HELD:
            self.lock = lock
        return self._judged(state)

    def release(self) -> None:
        lock, self.lock = self.lock, None
        if lock is not None:
            lock.release()


class _ConsoleControl:
    """While recording and saving: Ctrl+Break stops the recording instead of killing the process, and closing
    the console window stops it, ends the network trace session at once (it would outlive us otherwise) and
    waits up to CLOSE_SAVE_WAIT_S for the save; Windows ends the process after that. Ctrl+C stays with Python.

    Sign-out is not covered: CTRL_LOGOFF and CTRL_SHUTDOWN never reach a process that loaded user32.
    """

    def __init__(self, stop: threading.Event, saved: threading.Event, network: bool,
                 say: Callable[[str], None] | None = None) -> None:
        from lagdoctor.capture.session import request_stop
        self.stop, self.saved = stop, saved
        self.say = _teller(say)
        self._request_stop = request_stop
        self.net_session: str | None = None
        self._etw = None
        self._netflows = None
        if network:
            from lagdoctor.capture import netflows
            from lagdoctor.winapi import etw
            # Imported now: never inside the handler.
            self.net_session, self._etw, self._netflows = netflows.SESSION_NAME, etw, netflows
        self._routine = _HANDLER_ROUTINE(self._handle)           # kept referenced while it is registered
        self.registered = False

    def __enter__(self) -> "_ConsoleControl":
        if _kernel32.SetConsoleCtrlHandler(self._routine, True):
            self.registered = True
        else:
            self.say(f"(Closing this window would lose the recording: the console handler could not be set, error "
                     f"{ctypes.get_last_error()}.)")
        return self

    def __exit__(self, *exc: object) -> None:
        if self.registered:
            _kernel32.SetConsoleCtrlHandler(self._routine, False)
            self.registered = False

    def _handle(self, event: int) -> bool:
        """Runs on a thread Windows creates for the event. True = handled here."""
        try:
            if event == CTRL_BREAK_EVENT:
                self._request_stop(self.stop, "Ctrl+Break")
                return True
            if event in (CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT):
                self._request_stop(self.stop, "window closed")
                if self._etw is not None and self.net_session:
                    self._netflows.expect_external_stop()        # first: the tracker reads the stop as ours
                    self._etw.stop_session(self.net_session)    # milliseconds; the save may not fit in 5 s
                self.saved.wait(CLOSE_SAVE_WAIT_S)
                return True
        except BaseException:  # noqa: BLE001 - nothing may escape into the console's thread
            return event != CTRL_C_EVENT
        return False


def elevation_choice(elevate: bool, no_elevate: bool, interactive: bool, ask: Callable[[str], bool], *,
                     say: Callable[[str], None] | None = None) -> str:
    """'elevate' or 'skip': what to do when frame timing needs rights we do not have."""
    say = _teller(say)
    if elevate:
        return "elevate"
    if no_elevate or not interactive:
        return "skip"
    say("Frame timing (FPS, stutters, CPU or GPU limit) needs admin rights. lag-doctor can record with one UAC")
    say("prompt; only the recording runs as admin, and nothing is changed. To never be asked, see `lagdoctor setup`.")
    return "elevate" if ask("Record with frame timing (shows a UAC prompt)? [Y/n] ", default=True) else "skip"


def _watching(target: dict[str, Any], observers: Any, say: Callable[[str], None]) -> None:
    """The game is chosen: said here, and told to the observers (the app's channel)."""
    say(f"Watching {target.get('name')} (pid {target['pid']}, chosen by {target.get('how', '?')}).")
    observers.event("target", name=target.get("name"), pid=target["pid"], how=target.get("how"))


def _elevation(args: argparse.Namespace, target: dict[str, Any] | None, exe: Path, frames_why: str,
               interactive: bool | None, *, say: Callable[[str], None], launcher: Callable[..., Any] | None,
               folders: tuple[str, ...] | None = None, wait_s: float | None = None,
               trace: _TraceGuard | None = None) -> tuple[Any, bool, str, int | None]:
    """Frame timing's rights, once PresentMon is there: (relay link or None, whether frames are recorded, why not,
    watch's exit code or None to go on). For the running game `target`, or for --wait-folder its folder's `folders`
    and `wait_s` (target None: the prompt comes before the game exists). `trace`: the watch's _TraceGuard, which the
    capture service and the UAC prompt are asked only past (default: one of its own)."""
    from lagdoctor.winapi import elevation
    relay, frames = None, True
    trace = trace if trace is not None else _TraceGuard(say)
    may_trace = elevation.can_trace()
    # An explicit --elevate also elevates an account that may trace only through Performance Log
    # Users: Windows lets that group start frame timing but not the game-server tracker (E1, 2026-11-06).
    if not may_trace or (args.elevate and not _elevated()):
        if not args.no_elevate:               # the capture service, when installed: the same, with no prompt
            if trace.busy():                  # it would stop another Windows session's recording, as a relay would
                return None, frames, frames_why, EXIT_BUSY
            relay, code = _via_service(args, target, say=say, folders=folders, wait_s=wait_s)
            if code is not None:
                return None, frames, frames_why, code
            if relay is not None:
                return relay, frames, frames_why, None
        choice = elevation_choice(args.elevate, args.no_elevate, _interactive(interactive), _ask, say=say)
        if choice == "elevate":
            if trace.busy():                  # again: the answer may have taken a while, and no prompt is in vain
                return None, frames, frames_why, EXIT_BUSY
            relay, code = _start_relay(args, target, exe, may_trace, say=say, launcher=launcher, folders=folders,
                                       wait_s=wait_s)
            if code is not None:
                return None, frames, frames_why, code
        if relay is None and not may_trace:   # declined: a group member still records frames here
            frames = False
            frames_why = ("it needs admin rights or membership of Performance Log Users (see "
                          "`lagdoctor setup`)")
    return relay, frames, frames_why, None


def _play_forms(folder: str, *, say: Callable[[str], None],
                observers: Any) -> tuple[tuple[str, ...] | None, int | None, int]:
    """watch --wait-folder before any prompt: the forms of the game's folder that the prompt approves
    (target.folder_forms), and whether the game already runs from it, owning a window: a store starts nothing for a
    game that runs, and the wait sees only programs started after it began, so it would wait out its whole time.
    That one is told to the observers as `already_running` (the app's channel: done's game_running).
    (forms, None, a create time: a program from the folder started after it is new), or (None, watch's exit code, 0)
    with why said."""
    from lagdoctor.capture import target as tgt
    from lagdoctor.winapi import windows
    try:
        forms = tgt.folder_forms(folder)
    except ValueError as exc:
        say(f"Not started: the game's folder cannot be waited on ({exc}).")
        return None, EXIT_BAD_ARGS, 0
    # Programs from the folder started after this count as new, as the folder wait's own default (SINCE_MARGIN).
    looked = tgt.filetime_now() - tgt.SINCE_MARGIN
    try:
        running = tgt.running_from(forms, resolve=_unelevated())["running"]
        shown = tgt.windows_by_pid(windows.top_windows())
    except OSError as exc:
        say(f"Not started: what already runs from the game's folder could not be read ({exc}).")
        return None, 1, 0
    there = [p for p in running if p["pid"] in shown]
    if there:
        observers.event("already_running", name=there[0]["name"], pid=there[0]["pid"])
        names = ", ".join(sorted({p["name"] or "a program" for p in there}))
        say(f"Not started: {names} already runs from the game's folder with a window, and a store starts nothing for a "
            f"game that is running. Close it and run this again, or record it as it runs: lagdoctor watch --pid "
            f"{there[0]['pid']}.")
        return None, EXIT_BAD_ARGS, 0
    return forms, None, looked


class _PlayStatus:
    """What watch --wait-folder says while it waits (target.wait_for_game's on_status): each step once for each
    process, every hold, re-arm and refusal. The words name programs by what Windows calls them (say() cleans
    them)."""

    def __init__(self, say: Callable[[str], None]) -> None:
        self.say = say
        self.told: set[tuple] = set()

    def __call__(self, state: str, info: dict[str, Any]) -> None:
        name, pid = info.get("name"), info.get("pid")
        if state in ("started", "window", "short-name"):
            if (state, pid) in self.told:
                return
            self.told.add((state, pid))
        if state == "started":
            self.say(f"{name} started from the game's folder: waiting for its window.")
        elif state == "window":
            self.say(f"{name} has a window: waiting for it to draw.")
        elif state == "windows-only":
            self.say(f"The GPU counters give no reading ({info.get('why')}), so a window from the game's folder that "
                     "stays open is taken as the game.")
        elif state == "short-name":
            self.say(f"({name}, pid {pid}, started from a path Windows gave in 8.3 short names, which this recording "
                     f"compares as written, so it is not taken. If it is the game, record it as it runs: lagdoctor "
                     f"watch --pid {pid}.)")
        elif state == "holding":
            self.say(f"{name} (pid {pid}) draws: watching it for {info.get('seconds'):g} s, to tell the game from a "
                     "launcher.")
        elif state == "rearm":
            self.say(f"Not the game: {info.get('why')}, so it was taken for a launcher. Waiting for the game.")
        elif state == "refused":
            self.say(f"The relay refused {name} (pid {pid}): {info.get('why')}. Waiting for the game.")


def _play_wait(folder: str, forms: tuple[str, ...], looked: int, wait_s: float, relay: Any, *,
               say: Callable[[str], None], cancel: threading.Event | None,
               observers: Any) -> tuple[dict[str, Any] | None, int | None]:
    """watch --wait-folder's wait for the game (target.wait_for_game: the re-arm, one deadline of `wait_s`).

    Play's order (the app's channel, control.py): the observers are told `launch`, then `waiting`, only once every
    check below that could end it has passed, right before the wait begins, so the app opens the store link only
    when the game it starts can be taken; a game from the folder already there is `already_running` instead.

    With the relay, its wait ends where the relay's own does less its slack (RelayLink.aim_by), it looks only at
    programs started after the relay (its `since`), passes over lag-doctor's own processes as the relay does
    (RelayLink.own), compares a program's path as the relay does (never writing an 8.3 short name out long), and
    names each game that holds to it (RelayLink.aim): a refusal re-arms the wait and never reads as the relay failing
    (_relay_failure), and once the relay takes no more games the wait ends as one that found none. A relay that ends
    while it waits ends the wait as the relay failing. Without the relay it looks at programs started after the
    pre-flight (`looked`). A program from the folder that started before the wait looks (the relay's start, or the
    pre-flight) could never be recorded this way: one that has a window now ends it at once, as `already_running`,
    naming `--pid` (a game started by hand that had no window yet at the pre-flight, or one started while the prompt
    was up), and one without that started while the prompt was up (a stub, whose game may start after the relay) is
    said and waited past. Either way a short name is written out long only in a window without administrator rights
    (program_reason; with the relay only to say that such a program is from the folder).
    (target, None), or (None, watch's exit code) with why said."""
    from lagdoctor.capture import target as tgt
    from lagdoctor.relay import client
    from lagdoctor.winapi import windows
    kw: dict[str, Any] = {"since": looked, "resolve": _unelevated()}
    stop: Any = cancel
    # What the wait could never take: a program from the folder that started before it looks (the relay's start with
    # the relay, the pre-flight without), which has a window now. A store starts nothing for a game that runs.
    since = relay.since if relay is not None else looked
    try:
        before = [p for p in tgt.running_from(forms, resolve=_unelevated())["running"] if p["create_time"] <= since]
        shown = tgt.windows_by_pid(windows.top_windows()) if before else {}
    except OSError as exc:
        say(f"Not recorded: what runs from the game's folder could not be read ({exc}).")
        return None, 1
    drawn = [p for p in before if p["pid"] in shown]
    if drawn:
        during = [p for p in drawn if p["create_time"] > looked]       # started while the prompt was up
        named = during or drawn
        observers.event("already_running", name=named[0]["name"], pid=named[0]["pid"])
        names = ", ".join(sorted({p["name"] or "a program" for p in named}))
        if during:
            say(f"Not recorded: {names} started from the game's folder {_while_ready(relay)}, and a recording "
                f"started this way takes only a game that starts after it. Record it as it runs: lagdoctor watch "
                f"--pid {named[0]['pid']}.")
        else:
            say(f"Not recorded: {names} already runs from the game's folder with a window, and a store starts nothing "
                f"for a game that is running. Record it as it runs: lagdoctor watch --pid {named[0]['pid']}.")
        return None, EXIT_BAD_ARGS
    if relay is not None:
        for p in before:        # no window yet: a stub or a launcher, whose game may still start after the relay
            if p["create_time"] > looked:
                say(f"({p['name']}, pid {p['pid']}, started from the game's folder {_while_ready(relay)}, so "
                    "this recording cannot take it. If it is the game itself, record it as it runs once it shows its "
                    f"window: lagdoctor watch --pid {p['pid']}.)")

        def aim(found: dict[str, Any]) -> tuple[str, bool] | None:
            try:
                relay.aim((found["pid"], found["create_time"]))
            except client.GameRefused as exc:
                return exc.why, exc.retry
            except client.RelayFailed as exc:
                if relay.ended.is_set():
                    raise
                raise tgt.TargetNotFound(exc.why) from None     # the link is open: it takes no more games
            return None
        wait_s = min(wait_s, relay.aim_by - (client._game_slack() - client.AIM_S) - time.monotonic())
        if wait_s <= 0:
            say("Not recorded: the relay's wait for the game was over before this one began. Run the same "
                "`lagdoctor watch` again.")
            return None, 1
        kw = {"since": relay.since, "own": relay.own(), "resolve": False, "expand_short": _unelevated(),
              "aim": aim}
        stop = _EitherSet(cancel, relay.ended)    # a relay that ends ends the wait, not five minutes later
    observers.event("launch")                     # the app opens the store link now (Play's step 3)
    observers.event("waiting")
    say(f"Waiting up to {tgt._duration_words(wait_s)} for the game to start from {folder}: start it now, from its "
        "store or its own program (a store that first downloads an update can take longer: give it --wait-s). "
        "Ctrl+C cancels.")
    try:
        return tgt.wait_for_game(folder, folders=forms, deadline_s=wait_s, hold_s=tgt.REARM_HOLD_S, cancel=stop,
                                 on_status=_PlayStatus(say), **kw), None
    except tgt.TargetNotFound as exc:
        say(f"No game was found: {exc}.")
        say("Start the game once watch says it is waiting, or give it longer with --wait-s.")
        return None, EXIT_BAD_ARGS
    except tgt.WaitCancelled:
        if relay is None or not relay.ended.is_set() or (cancel is not None and cancel.is_set()):
            raise Cancelled() from None
        code, text = _relay_failure(relay.failure(), play=True)
        say(text)
        return None, code
    except client.RelayFailed as exc:
        code, text = _relay_failure(exc, play=True)
        say(text)
        return None, code
    except OSError as exc:
        say(f"The wait for the game stopped: {exc}. Nothing was recorded.")
        return None, 1


class _EitherSet:
    """Set once either event is (the caller's cancel, the relay link's end): what the folder wait looks at every
    round and pauses on, as it does on a threading.Event."""

    def __init__(self, *events: threading.Event | None) -> None:
        self.events = [e for e in events if e is not None]

    def is_set(self) -> bool:
        return any(e.is_set() for e in self.events)

    def wait(self, seconds: float) -> bool:
        end = time.monotonic() + seconds
        while not self.is_set():
            left = end - time.monotonic()
            if left <= 0:
                return False
            time.sleep(min(0.05, left))
        return True


def _start_relay(args: argparse.Namespace, target: dict[str, Any] | None, exe: Path, may_trace: bool = False, *,
                 say: Callable[[str], None] | None = None, launcher: Callable[..., Any] | None = None,
                 folders: tuple[str, ...] | None = None, wait_s: float | None = None) -> tuple[Any, int | None]:
    """Frame timing and the game-server tracker through the relay: one UAC prompt, then PresentMon and the network
    trace run with administrator rights while the recording stays in this window. (link, None) once the relay
    answers and checks out; (None, None) when the prompt was declined or could not be shown, so the recording goes on
    without them; (None, code) when the relay could not record, said here. `may_trace`: this account records frame
    timing itself (Performance Log Users), so without the relay it loses only the tracker. `launcher`: what starts
    the relay elevated (client.launch's; default elevation.launch_elevated). For the running game `target`, or for
    --wait-folder (target None) the forms of its folder (`folders`) and how long watch waits for it (`wait_s`)."""
    from lagdoctor.capture import session as sess
    from lagdoctor.capture import target as tgt
    from lagdoctor.relay import client
    from lagdoctor.winapi import elevation
    say = _teller(say)
    if not may_trace:
        goes_on = "lag-doctor records without frame timing"
    elif args.no_network:
        goes_on = "lag-doctor records here, with frame timing"
    else:
        goes_on = "lag-doctor records without the game-server tracker"
    try:
        procs = sess._snapshot()
        me = procs.get(os.getpid())
        if me is None:
            raise OSError("this window's own process is not in Windows' process list")
        sid = elevation.token_user_sid()
    except OSError as exc:
        say(f"What the relay needs could not be read ({exc}), so {goes_on}.")
        return None, None
    if folders is None:                       # Play's game does not exist yet: the relay judges it when it is named
        why = tgt.accept_target(procs, target["pid"], target["create_time"], session=me.session_id,
                                approved=(target["pid"], target["create_time"]))
        if why is not None:                   # what the relay would refuse is refused before the prompt
            say(f"Not recorded: {why}.")
            return None, EXIT_BAD_ARGS
    say("Frame timing needs administrator rights: approve the UAC prompt. The recording stays in this window.")
    try:
        link = client.launch(target, presentmon_exe=Path(exe).resolve(), frames=True, network=not args.no_network,
                             recorder=(me.pid, me.create_time), sid=sid, launcher=launcher, folders=folders,
                             wait_s=wait_s)
    except PermissionError:
        say(f"The UAC prompt was declined, so {goes_on}.")
        return None, None
    except client.RelayFailed as exc:
        code, text = _relay_failure(exc, play=folders is not None)
        say(text)
        return None, code
    except ValueError as exc:                 # a command it refused to launch: say so, never a traceback
        say(f"The relay was not started, because its command was refused ({exc}), so {goes_on}.")
        return None, None
    except OSError as exc:
        say(f"The UAC prompt could not be shown ({exc}), so {goes_on}.")
        return None, None
    return link, None


def _while_ready(relay: Any) -> str:
    """When a program that started before the wait looks started: during the prompt, or the capture service's start."""
    if relay is not None and getattr(relay, "service", False):
        return "while lag-doctor's capture service started"
    return "while the UAC prompt was up"


def _via_service(args: argparse.Namespace, target: dict[str, Any] | None, *, say: Callable[[str], None],
                 folders: tuple[str, ...] | None = None, wait_s: float | None = None) -> tuple[Any, int | None]:
    """Frame timing and the game-server tracker through lag-doctor's capture service (Stage 4), with no prompt:
    (link, None) when it records; (None, None) when it cannot here, so the UAC prompt comes as before (said, unless it
    is simply not installed); (None, EXIT_BUSY) when it records for another window, which a relay would stop."""
    from lagdoctor.capture import session as sess
    from lagdoctor.relay import client
    say = _teller(say)
    try:
        me = sess._snapshot().get(os.getpid())
        if me is None:
            raise OSError("this window's own process is not in Windows' process list")
        link = client.via_service(target, frames=True, network=not args.no_network, recorder=(me.pid, me.create_time),
                                  folders=folders, wait_s=wait_s)
    except client.ServiceBusy as exc:
        say(f"Not recorded: {exc.why}. Try again once it has ended.")
        return None, EXIT_BUSY
    except client.ServiceUnavailable as exc:
        if exc.installed:
            say(f"The capture service did not record ({exc.why}), so the UAC prompt comes instead.")
        return None, None
    except (OSError, ValueError) as exc:
        say(f"The capture service was not asked ({exc}).")
        return None, None
    say("Recording through lag-doctor's capture service: no UAC prompt. The recording stays in this window.")
    return link, None


def _relay_failure(exc, play: bool = False) -> tuple[int, str]:
    """watch's exit code and what to say when the relay could not record (relay/client.RelayFailed). `play`: for
    --wait-folder, whose prompt came before the game existed, so no game can have exited while it was up. A game the
    relay refused is no failure of the relay (client.GameRefused): the wait looks for another, and never comes here."""
    from lagdoctor.relay import protocol
    if exc.code == protocol.EXIT_TARGET and play:
        return 1, ("When the relay checked, this window was not running as it approved, or the relay could not read "
                   "Windows' process list, so nothing was recorded. Run the same `lagdoctor watch` again.")
    if exc.code == protocol.EXIT_NOT_READY:
        return EXIT_NOT_READY, ("PresentMon failed its check in the administrator part of the recording, so nothing "
                                "was recorded. Run `lagdoctor setup` here, then start the recording again.")
    if exc.code == protocol.EXIT_BUSY:          # the relay found another recording holding the trace lock
        return EXIT_BUSY, TRACE_BUSY
    if exc.code == protocol.EXIT_TARGET:
        return EXIT_TARGET_CHANGED, ("When the relay checked, the game or this window was no longer running as "
                                     "approved (the game may have exited while the UAC prompt was up), so nothing "
                                     "was recorded. Start the game again, then run `lagdoctor watch` again.")
    return 1, f"Frame timing could not be started: {exc.why}. Nothing was recorded."


def _private_copy_gate(policy: str, folder: Path | None = None, show: Callable[[str], None] | None = None,
                       ) -> tuple[int | None, bool]:
    """(the exit code or None to go on, whether the files land in a private copy). Windows may keep what this process
    saves in `folder` (default: lag-doctor's home) in another app's private folder (paths.private_copy), which the
    user's other windows never see: a PresentMon set up, a recording saved or a fix journaled from here would be
    missing for them."""
    show = show or say
    try:
        folder = folder if folder is not None else paths.home()
        copy = paths.private_copy(folder)
    except OSError as exc:
        if policy == "refuse":                  # cannot tell where it would be saved: save nothing
            show(f"lag-doctor cannot save files in {folder or 'its folder'}: {exc}")
            return 1, False
        return None, False
    if copy is None:
        return None, False
    if policy == "refuse":
        show(f"Not started: {paths.private_copy_advice(copy)}")
        return EXIT_PRIVATE_COPY, True
    show(f"Note: {paths.private_copy_advice(copy)} What this command saves goes there too.")
    return None, True


def _remove_empty_dir(folder: Path) -> None:
    try:
        folder.rmdir()                  # only succeeds when empty
    except OSError:
        pass


def _presentmon_exe() -> tuple[Path | None, str]:
    """The verified PresentMon, or None and why not, in plain words (missing, or failing its check)."""
    from lagdoctor import setup as pm_setup
    try:
        return pm_setup.ensure_presentmon(allow_download=False), ""
    except pm_setup.SetupError as exc:
        return None, str(exc)


# -------------------------------------------------------------------- app --

def cmd_app(args: argparse.Namespace) -> int:
    """lag-doctor's window (lagdoctor/app). Only the owner starts it: an agent's shell may hold another app's private
    copy of AppData, which main() refuses before this runs."""
    from lagdoctor.app import main as app_main
    return app_main.run()


def cmd_sweep(args: argparse.Namespace) -> int:
    """A settings sweep (lagdoctor/sweep/): only the owner runs one, in their own terminal, since it changes the game's
    settings file and records each run."""
    from lagdoctor.sweep import command
    return command.main(args)


def _app_tell(text: str) -> None:
    """A message from `lagdoctor app` before its window exists: in the console that started it, or in a message box
    when there is none (the Start-menu shortcut runs pythonw.exe, which has no console)."""
    from lagdoctor.app import main as app_main
    app_main.tell(text)


# ---------------------------------------------------------------- analyze --

def analyze_and_report(cap, cap_path: Path, out_dir: Path, *, remember: bool = False, kind: str | None = None,
                       show_summary: bool = True, say: Callable[[str], None] | None = None,
                       span: tuple[float, float, str] | None = None) -> int:
    """Diagnose, write diagnosis.json and report.html into out_dir, print the summary. remember=True (a watch)
    makes this the report `fix` acts on. `kind`: summary.json's "watch" or "analyze" (default: "watch" when
    remembered). `show_summary` False: the summary is not said. `say`: where every line goes, the summary included
    (default: printed). `span` (t0, t1, how): analyse only that part of the recording (analysis/crop.py, which
    refuses a span it cannot cut); the report still draws the whole recording, with the rest shaded."""
    from lagdoctor.analysis.crop import crop
    from lagdoctor.analysis.diagnose import diagnose
    from lagdoctor.report import console, html
    from lagdoctor.report.charts import fmt_elapsed
    show = say if say is not None else print          # the summary, which is plain ASCII already
    say = _teller(say)
    try:
        part = cap if span is None else crop(cap, *span)
        if part.span is None:
            say("Analysing...")
        else:
            say(f"Analysing {fmt_elapsed(part.span['from_s'])} to {fmt_elapsed(part.span['to_s'])} of the "
                "recording...")
        d = diagnose(part)
    except Exception as exc:  # noqa: BLE001 - a damaged capture must end in a plain line, not a traceback
        say(f"The capture at {cap_path} looks damaged: the analysis failed on it ({type(exc).__name__}: "
            f"{str(exc)[:200]}). The file was kept as it is.")
        return 1
    _write_json(out_dir / DIAGNOSIS_NAME, d.to_json())
    report = html.write(d, cap, out_dir / REPORT_NAME, capture_path=cap_path)
    _write_summary(d, cap, out_dir, kind or ("watch" if remember else "analyze"), say=say)
    if show_summary:
        show(console.render(d, cap, report_path=report, width=_width()))
    if remember:
        _remember(out_dir)
    return 0


def _write_summary(d, cap, out_dir: Path, kind: str, *, say: Callable[[str], None] | None = None) -> None:
    """The app's summary of this session (report/summary.py). Without it the app still lists the session, by its
    folder's name, so a failed write is said and the analysis goes on."""
    from lagdoctor.report import summary
    say = _teller(say)
    try:
        _write_json(out_dir / summary.SUMMARY_NAME, summary.build(d, cap, kind))
    except (OSError, ValueError) as exc:
        say(f"(The app's summary of this session could not be saved: {exc}. The app lists it by its folder's name.)")


def cmd_analyze(args: argparse.Namespace) -> int:
    from lagdoctor.capture.session import session_dir
    from lagdoctor.model import Capture, CaptureError
    path = Path(args.capture).resolve()
    if not path.is_file():
        say(f"No capture at {path}.")
        return 2
    try:
        cap = Capture.load(path)
    except CaptureError as exc:
        say(f"{path} cannot be analysed: {exc}.")
        return 2
    except OSError as exc:
        say(f"{path} could not be read: {exc.strerror or exc}.")
        return 2
    except MemoryError:
        say(f"{path} is too large to load on this PC.")
        return 2
    span = None
    t_from, t_to = getattr(args, "span_from", None), getattr(args, "span_to", None)
    if t_from is not None or t_to is not None:
        from lagdoctor.analysis import crop
        try:          # an end not given is the recording's own; one past its end is its end
            end = crop.recorded_s(cap)
            whole = (t_from is None or t_from <= 0) and (t_to is None or t_to >= end)
            if not whole:          # a span over the whole recording is the whole recording, however short
                span = (*crop.check_span(cap, 0.0 if t_from is None else t_from, end if t_to is None else t_to),
                        "set")
        except ValueError as exc:
            say(f"That part of {path} cannot be analysed: {exc}.")
            return 2
    else:                 # the part its start key measured, as the watch analysed it; without one, all of it
        from lagdoctor.analysis import crop
        part, _ = crop.key_part(cap)
        span = None if part is None else (*part, "key")
    # A fresh folder of our own: never next to the capture (which may sit beside files of the user's own
    # with the same names), and never the report `fix` acts on (the capture may come from another PC).
    out_dir = session_dir(paths.sessions_dir(), cap.target)
    code = analyze_and_report(cap, path, out_dir, remember=False, span=span)
    if code != 0:
        _remove_empty_dir(out_dir)
        return code
    _write_text(out_dir / ANALYZED_FROM, str(path) + "\n")
    say("This report is not used by `lagdoctor fix`, which offers only what your last watch or check-up suggested.")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    from lagdoctor.analysis.diagnose import diagnose
    from lagdoctor.capture.session import _collect_inventory, session_dir
    from lagdoctor.model import Capture
    from lagdoctor.report import console
    inv = _collect_inventory(None)           # with running process names, so OEM tools can be recognised
    cap = Capture(started_at=datetime.now().astimezone().isoformat(timespec="seconds"), clock_origin=0.0,
                  duration_s=0.0, target={"name": "settings check-up"}, sources={}, inventory=inv)
    d = diagnose(cap)
    # Saved and remembered apart from the last watch, so the `lagdoctor fix ID` lines printed below work.
    out_dir = session_dir(paths.sessions_dir(), {"name": "check-up"})
    _write_json(out_dir / DIAGNOSIS_NAME, d.to_json())
    _remember(out_dir, LAST_CHECKUP)
    print(console.render(d, cap, width=_width(), checkup=True))
    for section, why in sorted((inv.get("errors") or {}).items()):
        say(f"(Could not read {section}: {why})")
    return 0


# -------------------------------------------------------------- fix / undo --

def cmd_fix(args: argparse.Namespace) -> int:
    from lagdoctor.fixes import catalog, flow
    from lagdoctor.fixes.journal import Journal, JournalCorrupt
    sources, problems = fix_sources()
    for line in problems:
        say(line)
    if not sources:
        say("No report yet: run `lagdoctor watch` (or `lagdoctor check`) first.")
        return 2
    for label, folder, _, _ in sources:
        say(f"Fixes suggested by {label}: {folder}")
    offers = merged_offers(sources)
    if args.list or not args.ids:
        if not offers:
            say("Those reports suggested no automatic fixes.")
        for fix_id, title, _, label in offers:
            say(f"  {fix_id:28} {title}  [{label}]")
            about = getattr(_fix_class(fix_id), "description", "")
            if about:
                say(f"  {'':28} {about}")
        if offers and not args.ids:
            say("Apply one with: lagdoctor fix ID")
        return 0
    if _elevated():
        say("lag-doctor is running as administrator. Fixes change your own user's settings, so run `lagdoctor fix` "
            "from a normal (not elevated) terminal.")
        return 2
    status = 0
    try:
        journal = Journal()
        journal.entries()                 # an unreadable journal stops fixes before anything is asked
        for fix_id in args.ids:
            matching = [o for o in offers if o[0] == fix_id]
            if not matching:
                say(f"The last reports did not suggest '{fix_id}'. See `lagdoctor fix --list`.")
                status = 2
                continue
            for _, title, params, label in matching:
                say(f"{title}  (suggested by {label})")
                try:
                    fix = catalog.get(fix_id)
                except KeyError:                    # a report a newer lag-doctor wrote
                    say(f"  Not changed: this lag-doctor does not know the fix '{fix_id}'; a newer one suggested it")
                    status = 1
                    continue
                try:
                    plan = fix.plan(params)
                except flow.PLAN_ERRORS as exc:     # e.g. a protected process
                    say(f"  Not changed: {exc}")
                    status = 1
                    continue
                say(f"  where:  {plan.target}")
                say(f"  change: {plan.summary}")
                for note in plan.notes:
                    say(f"  note:   {note}")
                if plan.needs_restart:
                    say(f"  takes effect after restarting the {plan.needs_restart}")
                if plan.noop:
                    say("  Already set; nothing to do.")
                    continue
                if not (args.yes or _ask("Apply this change? [y/N] ", default=False)):
                    say("  Skipped.")
                    continue
                res = flow.apply(fix, plan, journal)
                if res.ok:
                    say(f"  Done: {res.message}")
                elif res.state == "applied":                # in place but not confirmed (e.g. after a restart)
                    say(f"  Applied, not yet confirmed: {res.message}")
                elif res.state == "unsure":                 # the journal failed once the change may have begun
                    say(f"  Not known whether it changed: {res.message}")
                else:
                    say(f"  Not changed: {res.message}")
                if res.state == "applied" and res.entry_id:
                    say(f"  Undo it any time with: lagdoctor undo {res.entry_id}")
                if not res.ok:
                    status = 1
    except JournalCorrupt as exc:
        parked = getattr(exc, "parked", None)
        plain = exc.plain() if hasattr(exc, "plain") else ""
        say("Fixes are paused: the fix journal, where lag-doctor records how to undo each change, could not be "
            "read, so no further change was made" + (f". Its contents were copied to {parked}" if parked else "")
            + f". The journal is {paths.journal_path()}." + (f" {plain}" if plain else ""))
        return 1
    except OSError as exc:                       # TimeoutError included: another lag-doctor holds the journal
        say(f"Fixes are paused: lag-doctor could not open its fix journal ({exc}), so no further change was made. "
            "Try again in a moment.")
        return 1
    return status


def _fix_class(fix_id: str):
    from lagdoctor.fixes import catalog
    return catalog.find(fix_id)


def cmd_undo(args: argparse.Namespace) -> int:
    from lagdoctor.fixes import catalog, flow
    from lagdoctor.fixes.journal import Journal, JournalCorrupt
    journal = Journal()
    try:
        entries = journal.outstanding()          # applied changes and conflicts, newest first
    except JournalCorrupt as exc:
        say(exc.plain())
        return 1
    except OSError as exc:                       # TimeoutError included: another lag-doctor holds the journal
        say(f"lag-doctor could not open its fix journal ({exc}), so nothing was undone. Try again in a moment.")
        return 1
    if not entries:
        say("Nothing to undo: lag-doctor has no changes in place.")
        return 0
    if not args.ids and not args.all:
        say("Changes lag-doctor made (newest first):")
        for e in entries:
            flag = "  CONFLICT" if e.state == "conflict" else ""
            say(f"  {e.id}  {e.fix_id:26} {e.target}  ({e.created_at}){flag}")
            if e.state == "conflict" and e.note:
                say(f"  {'':12}  {e.note}")
        say("Undo one with: lagdoctor undo ID   (or --all)")
        if any(e.state == "conflict" for e in entries):
            say("A CONFLICT is a setting that is now neither the original nor lag-doctor's value; "
                "undo it with --force to put the original back.")
        return 0
    chosen = entries if args.all else [e for e in entries if e.id in args.ids or e.fix_id in args.ids]
    if not chosen:
        say("No change in place matches " + ", ".join(args.ids) + ". See `lagdoctor undo`.")
        return 2
    status = 0
    for e in chosen:
        try:
            res = flow.undo(e, catalog.get, journal, force=args.force)
        except JournalCorrupt as exc:
            say(exc.plain())
            return 1
        if res.why == "unsure":                  # the journal failed once the original may have been put back
            say(f"Not known whether it was put back: {res.message}")
        else:
            say(f"{'Restored' if res.ok else 'Left alone'}: {res.message}")
        if not res.ok:
            status = 1
    return status


def _settle_journal(say: Callable[[str], None] | None = None) -> None:
    """Settle changes a crash left half-done, and session-only changes whose subject has ended (fixes/flow.settle).
    `say`: where its lines go (default: printed here; lag-doctor's window for `watch --control`)."""
    from lagdoctor.fixes import flow
    flow.settle(_teller(say))


# ------------------------------------------------------------------ setup --

def cmd_setup(args: argparse.Namespace) -> int:
    from lagdoctor import setup as pm_setup
    from lagdoctor.winapi import elevation
    try:
        exe = pm_setup.ensure_presentmon(allow_download=False)
    except pm_setup.BrokenInstall as exc:        # the installer's own copy: a download would not be used
        say(str(exc))
        return 1
    except pm_setup.SetupError as exc:           # missing, or failing verification: offer the download
        say(str(exc))
        if not (args.download or _ask("Download Intel PresentMon 2.6.0 (980,320 bytes, MIT licence, Intel-signed) "
                                      "from github.com/GameTechDev/PresentMon? [y/N] ", default=False)):
            say("Not downloaded. Frame timing stays unavailable until PresentMon is set up.")
            return 1
        try:
            exe = pm_setup.ensure_presentmon(allow_download=True)
        except (pm_setup.SetupError, OSError) as exc:
            say(f"The download did not work, and nothing was kept: {exc}")
            return 1
    info = pm_setup.verify(exe)
    say(f"PresentMon: ready and verified at {exe} (SHA-256 pinned, signed by {info['signer']})")
    if info.get("note"):
        say(f"  note: {info['note']}")
    rights = elevation.trace_rights()
    if rights == "now":
        say("Frame timing: ready (this account may start ETW traces).")
        if not _elevated():                         # the right comes from Performance Log Users
            say(TRACKER_NOT_IN_GROUP)
    elif rights == "filtered":
        say("Frame timing: your account is in the Performance Log Users group, but Windows keeps that group")
        say("switched off in normal windows here, so `lagdoctor watch` asks for one UAC prompt each recording.")
    elif rights == "after_sign_in":
        say("Frame timing: your account is in the Performance Log Users group, but this sign-in began before it")
        say("joined. Sign out of Windows and back in once; after that, frame timing needs no admin prompt.")
        say("Until then, `lagdoctor watch` asks for one UAC prompt each recording.")
        say(TRACKER_NOT_IN_GROUP)
    else:
        say("Frame timing and the game-server tracker need admin rights. Two ways:")
        say("  1. Let `lagdoctor watch` ask for one UAC prompt each recording (nothing to set up).")
        say("  2. Join the Performance Log Users group once (an administrator does it), then sign out and back in.")
        say("     After that, frame timing needs no prompt. The game-server tracker still does: Windows does not")
        say("     let this group start it, so add `--elevate` to a recording to include it.")
        say("")
        for line in pm_setup.performance_log_users_howto().rstrip().splitlines():
            say("     " + line if line else "")
    return 0


# ---------------------------------------------------------------- helpers --

_CONTROL_CHARS = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")


def say(text: str) -> None:
    """One message: ASCII, with every control character but the newline shown as '?', so text from a process
    name or a shared capture cannot drive the terminal (escape sequences). A console that is gone (its window
    closed) must not stop the work, so a failed write is dropped."""
    clean = _CONTROL_CHARS.sub("?", str(text).encode("ascii", "replace").decode("ascii"))
    try:
        print(clean, flush=True)
    except (OSError, ValueError):
        pass


class Cancelled(Exception):
    """The caller cancelled the recording before it began (M4: the window's Cancel): nothing is recorded."""


def _check_cancel(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise Cancelled()


def _teller(given: Callable[[str], None] | None) -> Callable[[str], None]:
    """What a function that says things uses: the `say` it was given, or this module's own say() as it is when the
    function runs (tests replace it)."""
    return given if given is not None else say


def _interactive(given: bool | None) -> bool:
    """May watch ask the person at this console? `given`, or, by default, whether stdin is a terminal (pythonw has
    no stdin at all)."""
    if given is not None:
        return bool(given)
    return sys.stdin is not None and sys.stdin.isatty()


def _silent(text: str) -> None:
    """The session's status line where nobody reads this console."""


def _ask(prompt: str, default: bool = False) -> bool:
    try:
        answer = input(prompt).strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer in ("y", "yes")


def _width() -> int:
    return max(60, min(120, shutil.get_terminal_size((100, 24)).columns - 1))


def _unelevated() -> bool:
    """True only when this window is known to run without administrator rights: there the folder wait may write an
    8.3 short name out long (it reads folder entries), which it never does with them (target.program_reason)."""
    try:
        from lagdoctor.winapi import elevation
        return not elevation.is_elevated()
    except Exception:  # noqa: BLE001 - cannot tell: as if elevated
        return False


def _elevated() -> bool:
    try:
        from lagdoctor.winapi import elevation
        return elevation.is_elevated()
    except Exception:  # noqa: BLE001
        return False


def _write_json(path: Path, data: Any) -> None:
    _write_text(path, json.dumps(data, indent=1))


def _write_text(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _remember(out_dir: Path, marker: str = LAST_SESSION) -> None:
    """Make this folder's diagnosis the one `fix` reads (an absolute path, whatever folder `fix` runs in)."""
    _write_text(paths.home() / marker, str(Path(out_dir).resolve()))


REMEMBERED = ((LAST_SESSION, "the last watch"), (LAST_CHECKUP, "the last check-up"))


def remembered_report(marker: str, label: str) -> tuple[Path | None, dict[str, Any] | None, str | None]:
    """(folder, diagnosis, problem) for one remembered report. Nothing remembered: (None, None, None).
    A remembered folder that is gone or unreadable is said, never replaced by some other session's report."""
    m = paths.home() / marker
    if not m.is_file():
        return None, None, None
    try:
        folder = Path(m.read_text(encoding="utf-8").strip())
    except (OSError, ValueError) as exc:
        return None, None, f"Could not read which report was {label} ({m}: {exc}); its fixes are not offered."
    diag_path = folder / DIAGNOSIS_NAME
    if not folder.is_absolute() or not diag_path.is_file():
        return folder, None, (f"The report from {label} is gone ({folder}), so its fixes are not offered. "
                              "Record again to get new ones.")
    try:
        diag = json.loads(diag_path.read_text(encoding="utf-8"))
        if not isinstance(diag, dict):
            raise ValueError("it holds no diagnosis")
    except (OSError, ValueError, RecursionError) as exc:
        return folder, None, f"The report from {label} could not be read ({diag_path}: {exc}); its fixes are not offered."
    return folder, diag, None


def fix_sources() -> tuple[list[tuple[str, Path, dict[str, Any], float]], list[str]]:
    """The reports `fix` acts on, oldest first: (label, folder, diagnosis, written at), plus a line for each
    remembered report that could not be used."""
    found, problems = [], []
    for marker, label in REMEMBERED:
        folder, diag, problem = remembered_report(marker, label)
        if problem:
            problems.append(problem)
        elif diag is not None and folder is not None:
            try:
                when = (folder / DIAGNOSIS_NAME).stat().st_mtime
            except OSError:
                when = 0.0
            found.append((label, folder, diag, when))
    found.sort(key=lambda s: s[3])
    return found, problems


def merged_offers(sources: list[tuple[str, Path, dict[str, Any], float]]) -> list[tuple[str, str, dict[str, Any], str]]:
    """(fix_id, title, params, label) for every automatic fix the reports suggested. Where two reports offer
    the same fix id, only the newer report's offers count: they describe this PC as it is now."""
    from lagdoctor.fixes import flow
    newest: dict[str, tuple[str, list[tuple[str, str, dict[str, Any]]]]] = {}
    for label, _folder, diag, _when in sources:              # oldest first, so newer reports win
        offers = flow.offers(diag)
        for fid in dict.fromkeys(o[0] for o in offers):
            newest[fid] = (label, [o for o in offers if o[0] == fid])
    return [(fid, title, params, label) for fid, (label, offers) in newest.items() for _, title, params in offers]
