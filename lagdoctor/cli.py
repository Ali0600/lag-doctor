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
import ctypes
import json
import os
import re
import shutil
import sys
import threading
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

# Exit codes of `watch`.
EXIT_BAD_ARGS = 2              # also: the game could not be found
EXIT_TARGET_CHANGED = 3        # the game exited during the UAC prompt (its PID now names another process, or none)
EXIT_BUSY = 4                  # another recording is running in this Windows session
EXIT_PRIVATE_COPY = 5          # any command: what it saves would land in another app's private copy (main)
EXIT_NOT_READY = 6             # frame timing was wanted, but PresentMon is missing or failed its check

# What each command does when Windows keeps this process's files in another app's private copy
# (paths.private_copy): "refuse" what saves files other windows must find, "note" what only reads.
PRIVATE_COPY_POLICY = {"setup": "refuse", "watch": "refuse", "fix": "refuse", "undo": "refuse", "app": "refuse",
                       "check": "note", "analyze": "note"}

# Performance Log Users may trace frames but not enable Kernel-Network, the game-server tracker's provider
# (measured with scripts/etw_access.py on 2026-11-06; docs/research/install-and-rights.md, E1).
TRACKER_NOT_IN_GROUP = ("The game-server tracker still needs one UAC prompt: Windows does not let the Performance "
                        "Log Users group start it, so add `--elevate` to a recording to include it.")

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
    w.add_argument("--duration", type=_positive_float, metavar="S", help="stop after S seconds (default: Ctrl+C)")
    w.add_argument("--server", metavar="IP", type=_ip, help="also ping this game server")
    w.add_argument("--no-network", action="store_true", help="skip the network checks")
    el = w.add_mutually_exclusive_group()
    el.add_argument("--elevate", action="store_true", help="get frame timing via one UAC prompt, without asking")
    el.add_argument("--no-elevate", action="store_true", help="record without frame timing rather than elevate")
    w.add_argument("--out", metavar="DIR", type=_abs_path, help="where to save the capture and report")
    w.add_argument("--hotkey", default=DEFAULT_HOTKEY, type=_hotkey,
                   help=f"the 'it lagged now' key (default {DEFAULT_HOTKEY}); 'none' to turn it off")
    w.add_argument("--note", metavar="TEXT", type=_note,
                   help="what you changed for this recording, in your own words (e.g. \"shadows to low\"); shown in "
                        "the report and in the app's Compare")
    w.set_defaults(func=cmd_watch)

    a = sub.add_parser("analyze", help="re-run the analysis on a saved capture")
    a.add_argument("capture", type=Path)
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
    return p


def _positive_float(text: str) -> float:
    v = float(text)
    if not v > 0:                      # also refuses NaN
        raise argparse.ArgumentTypeError("must be more than 0")
    return v


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
    """A key Windows can register ('ctrl+alt+l'), checked before anything starts; 'none' or 'off' for no key."""
    key = text.strip().lower()
    if key in ("", "none", "off"):
        return "none"
    from lagdoctor.winapi.hotkey import parse_hotkey
    try:
        parse_hotkey(key)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None
    return key


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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 2
    # First, before anything is written (the journal healing below writes): a file saved where the user's
    # other windows cannot see it is worse than none.
    code, private = _private_copy_gate(PRIVATE_COPY_POLICY[args.cmd],
                                       show=_app_tell if args.cmd == "app" else say)
    if code is not None:
        return code
    # Under a private copy (a reading command was let through) the journal is not healed: what it writes would
    # land in that copy, apart from the journal the user's own windows keep.
    if args.cmd in ("watch", "fix", "undo", "check") and not private:
        _settle_journal()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        say("Stopped.")
        return 130


# ------------------------------------------------------------------- watch --

def cmd_watch(args: argparse.Namespace) -> int:
    from lagdoctor.capture import session as sess
    from lagdoctor.capture import target as tgt
    # One recording at a time, checked before PresentMon or any ETW session can start (they share fixed
    # session names, so a second one would silently end the first one's tracing). This window holds the lock
    # for the whole recording, the relay's part included.
    lock = sess.CaptureLock()
    if not _take_lock(lock):
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
            auto = args.auto or (args.game is None and args.pid is None)
            try:
                target = tgt.find_target(name=args.game, pid=args.pid, auto=auto)
            except tgt.TargetNotFound as exc:
                say(f"Could not find the game: {exc}")
                say("Start the game first, or name it with --game NAME.exe or --pid PID.")
                return EXIT_BAD_ARGS
            say(f"Watching {target.get('name')} (pid {target['pid']}, chosen by {target.get('how', '?')}).")
            frames = exe is not None
            if frames:
                from lagdoctor.winapi import elevation
                may_trace = elevation.can_trace()
                # An explicit --elevate also elevates an account that may trace only through Performance Log
                # Users: Windows lets that group start frame timing but not the game-server tracker (E1, 2026-11-06).
                if not may_trace or (args.elevate and not _elevated()):
                    choice = elevation_choice(args.elevate, args.no_elevate, sys.stdin.isatty(), _ask)
                    if choice == "elevate":
                        relay, code = _start_relay(args, target, exe, may_trace)
                        if code is not None:
                            return code
                    if relay is None and not may_trace:   # declined: a group member still records frames here
                        frames = False
                        frames_why = ("it needs admin rights or membership of Performance Log Users (see "
                                      "`lagdoctor setup`)")
            return _record(args, target, exe, frames, frames_why, relay)
    finally:
        if relay is not None:
            relay.close()


def _record(args: argparse.Namespace, target: dict[str, Any], exe: Path | None, frames: bool, frames_why: str,
            relay: Any = None) -> int:
    """Record, save and analyse. Ctrl+C, Ctrl+Break and closing the window all stop the recording; none of them can
    lose it once it is being stopped and saved. With a relay (relay/client.RelayLink), frame timing and the
    game-server tracker come through it, and it is let go as soon as the recording stops."""
    from lagdoctor.capture import session as sess
    from lagdoctor.model import SourceStatus, clean_note
    hotkey = None if (args.hotkey or "").lower() in ("", "none", "off") else args.hotkey
    opts = sess.SessionOptions(duration_s=args.duration, network=not args.no_network, server_ip=args.server,
                               hotkey=hotkey, presentmon_exe=exe, frames=frames, relay=relay,
                               tool_procs=relay.tool_procs() if relay is not None else ())
    out_dir = sess.session_dir(args.out or paths.sessions_dir(), target)
    cap_path = out_dir / CAPTURE_NAME
    if not frames:
        say(f"Frame timing is off for this recording: {frames_why}")
    say("Recording. Play normally" + (f"; press {hotkey.title()} the moment it lags" if hotkey else "")
        + "; Ctrl+C stops." + (f" Stops by itself after {args.duration:.0f} s." if args.duration else ""))
    stop, saved = threading.Event(), threading.Event()
    # With a relay the network trace is the relay's, and the relay stops it when this window goes away.
    with sess.CtrlC(stop, say) as guard, _ConsoleControl(stop, saved, network=opts.network and relay is None):
        try:
            try:
                cap = sess.Session(target, opts).run(stop, ctrl_c=guard)
            finally:
                if relay is not None:
                    relay.close()
            if not frames:
                cap.sources["frames"] = SourceStatus("off", frames_why)
            # The note is the recorder's alone: it never goes to the relay, and it is kept only in the capture.
            cap.note = clean_note(getattr(args, "note", None))
            n = len(cap.frames.get("t") or [])
            say(f"Saving {n:,} frames..." if n else "Saving the capture...")
            cap.save(cap_path)
        finally:
            saved.set()
    return _analyze_saved(cap, cap_path, out_dir)


def _analyze_saved(cap, cap_path: Path, out_dir: Path) -> int:
    """A watch's analysis. The capture is already on disk, so a Ctrl+C here says where it is."""
    try:
        return analyze_and_report(cap, cap_path, out_dir, remember=True)
    except KeyboardInterrupt:
        say(f"Stopped before the analysis finished. The capture is saved at {cap_path}; analyse it with: "
            f"lagdoctor analyze \"{cap_path}\"")
        return 130


def _take_lock(lock) -> bool:
    try:
        if lock.acquire():
            return True
        say("Another lag-doctor recording is already running (in another window). Stop it first, then try again.")
    except OSError as exc:
        say(f"lag-doctor could not check whether another recording is running ({exc}), so it did not start one.")
    return False


class _ConsoleControl:
    """While recording and saving: Ctrl+Break stops the recording instead of killing the process, and closing
    the console window stops it, ends the network trace session at once (it would outlive us otherwise) and
    waits up to CLOSE_SAVE_WAIT_S for the save; Windows ends the process after that. Ctrl+C stays with Python.

    Sign-out is not covered: CTRL_LOGOFF and CTRL_SHUTDOWN never reach a process that loaded user32.
    """

    def __init__(self, stop: threading.Event, saved: threading.Event, network: bool) -> None:
        from lagdoctor.capture.session import request_stop
        self.stop, self.saved = stop, saved
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
            say(f"(Closing this window would lose the recording: the console handler could not be set, error "
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


def elevation_choice(elevate: bool, no_elevate: bool, interactive: bool, ask: Callable[[str], bool]) -> str:
    """'elevate' or 'skip': what to do when frame timing needs rights we do not have."""
    if elevate:
        return "elevate"
    if no_elevate or not interactive:
        return "skip"
    say("Frame timing (FPS, stutters, CPU or GPU limit) needs admin rights. lag-doctor can record with one UAC")
    say("prompt; only the recording runs as admin, and nothing is changed. To never be asked, see `lagdoctor setup`.")
    return "elevate" if ask("Record with frame timing (shows a UAC prompt)? [Y/n] ", default=True) else "skip"


def _start_relay(args: argparse.Namespace, target: dict[str, Any], exe: Path,
                 may_trace: bool = False) -> tuple[Any, int | None]:
    """Frame timing and the game-server tracker through the relay: one UAC prompt, then PresentMon and the network
    trace run with administrator rights while the recording stays in this window. (link, None) once the relay
    answers and checks out; (None, None) when the prompt was declined or could not be shown, so the recording goes on
    without them; (None, code) when the relay could not record, said here. `may_trace`: this account records frame
    timing itself (Performance Log Users), so without the relay it loses only the tracker."""
    from lagdoctor.capture import session as sess
    from lagdoctor.capture import target as tgt
    from lagdoctor.relay import client
    from lagdoctor.winapi import elevation
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
    why = tgt.accept_target(procs, target["pid"], target["create_time"], session=me.session_id,
                            approved=(target["pid"], target["create_time"]))
    if why is not None:                       # what the relay would refuse is refused before the prompt
        say(f"Not recorded: {why}.")
        return None, EXIT_BAD_ARGS
    say("Frame timing needs administrator rights: approve the UAC prompt. The recording stays in this window.")
    try:
        link = client.launch(target, presentmon_exe=Path(exe).resolve(), frames=True, network=not args.no_network,
                             recorder=(me.pid, me.create_time), sid=sid)
    except PermissionError:
        say(f"The UAC prompt was declined, so {goes_on}.")
        return None, None
    except client.RelayFailed as exc:
        code, text = _relay_failure(exc)
        say(text)
        return None, code
    except ValueError as exc:                 # a command it refused to launch: say so, never a traceback
        say(f"The relay was not started, because its command was refused ({exc}), so {goes_on}.")
        return None, None
    except OSError as exc:
        say(f"The UAC prompt could not be shown ({exc}), so {goes_on}.")
        return None, None
    return link, None


def _relay_failure(exc) -> tuple[int, str]:
    """watch's exit code and what to say when the relay could not record (relay/client.RelayFailed)."""
    from lagdoctor.relay import protocol
    if exc.code == protocol.EXIT_NOT_READY:
        return EXIT_NOT_READY, ("PresentMon failed its check in the administrator part of the recording, so nothing "
                                "was recorded. Run `lagdoctor setup` here, then start the recording again.")
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


def _app_tell(text: str) -> None:
    """A message from `lagdoctor app` before its window exists: in the console that started it, or in a message box
    when there is none (the Start-menu shortcut runs pythonw.exe, which has no console)."""
    from lagdoctor.app import main as app_main
    app_main.tell(text)


# ---------------------------------------------------------------- analyze --

def analyze_and_report(cap, cap_path: Path, out_dir: Path, *, remember: bool = False) -> int:
    """Diagnose, write diagnosis.json and report.html into out_dir, print the summary. remember=True (a watch)
    makes this the report `fix` acts on."""
    from lagdoctor.analysis.diagnose import diagnose
    from lagdoctor.report import console, html
    say("Analysing...")
    try:
        d = diagnose(cap)
    except Exception as exc:  # noqa: BLE001 - a damaged capture must end in a plain line, not a traceback
        say(f"The capture at {cap_path} looks damaged: the analysis failed on it ({type(exc).__name__}: "
            f"{str(exc)[:200]}). The file was kept as it is.")
        return 1
    _write_json(out_dir / DIAGNOSIS_NAME, d.to_json())
    report = html.write(d, cap, out_dir / REPORT_NAME, capture_path=cap_path)
    _write_summary(d, cap, out_dir, "watch" if remember else "analyze")
    print(console.render(d, cap, report_path=report, width=_width()))
    if remember:
        _remember(out_dir)
    return 0


def _write_summary(d, cap, out_dir: Path, kind: str) -> None:
    """The app's summary of this session (report/summary.py). Without it the app still lists the session, by its
    folder's name, so a failed write is said and the analysis goes on."""
    from lagdoctor.report import summary
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
    # A fresh folder of our own: never next to the capture (which may sit beside files of the user's own
    # with the same names), and never the report `fix` acts on (the capture may come from another PC).
    out_dir = session_dir(paths.sessions_dir(), cap.target)
    code = analyze_and_report(cap, path, out_dir, remember=False)
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
    from lagdoctor.fixes import catalog
    from lagdoctor.fixes.journal import Journal, JournalCorrupt
    from lagdoctor.fixes.priority import Refused
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
                fix = catalog.get(fix_id)
                say(f"{title}  (suggested by {label})")
                try:
                    plan = fix.plan(params)
                except (Refused, OSError, ValueError, LookupError, RuntimeError) as exc:   # e.g. a protected process
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
                res = fix.apply(plan, journal)
                if res.ok:
                    say(f"  Done: {res.message}")
                elif res.state == "applied":                # in place but not confirmed (e.g. after a restart)
                    say(f"  Applied, not yet confirmed: {res.message}")
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
    return status


def _fix_class(fix_id: str):
    from lagdoctor.fixes import catalog
    catalog.ids()                                 # loads the built-in fix modules
    return catalog._REGISTRY.get(fix_id)


def fix_offers(diagnosis: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """(fix_id, title, params) for every automatic fix the report suggested, without duplicates."""
    out, seen = [], set()
    for f in diagnosis.get("findings") or []:
        for fx in f.get("fixes") or []:
            fid = fx.get("fix_id")
            key = (fid, json.dumps(fx.get("params") or {}, sort_keys=True))
            if fid and key not in seen:
                seen.add(key)
                out.append((fid, fx.get("title") or fid, dict(fx.get("params") or {})))
    return out


def cmd_undo(args: argparse.Namespace) -> int:
    from lagdoctor.fixes import catalog
    from lagdoctor.fixes.journal import Journal, JournalCorrupt
    journal = Journal()
    try:
        entries = journal.outstanding()          # applied changes and conflicts, newest first
    except JournalCorrupt as exc:
        say(exc.plain())
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
            fix = catalog.get(e.fix_id)
        except KeyError:                         # journaled by a lag-doctor that knows more fixes
            say(f"Left alone: {e.target}: this lag-doctor does not know the fix '{e.fix_id}'")
            status = 1
            continue
        try:
            res = fix.undo(e, journal, force=args.force)
        except JournalCorrupt as exc:
            say(exc.plain())
            return 1
        say(f"{'Restored' if res.ok else 'Left alone'}: {res.message}")
        if not res.ok:
            status = 1
    return status


def _settle_journal() -> None:
    """Settle changes a crash left half-done, and session-only changes whose subject has ended."""
    try:
        from lagdoctor.fixes import catalog
        from lagdoctor.fixes.journal import Journal, JournalCorrupt
        journal = Journal()
        try:
            entries = journal.entries()
        except JournalCorrupt as exc:
            say(f"The fix journal could not be read; it was copied to {exc.parked} and left in place. Fixes and "
                f"undo are paused until it can be read again (`lagdoctor undo` says more).")
            return
        for e in entries:
            try:
                if e.state == "pending":
                    res = catalog.get(e.fix_id).heal(e, journal)
                    say(f"Settled a change that was interrupted last time: {res.message}")
                elif e.state == "applied" and e.session_only:
                    # Decided without read(): a journaled PID may belong to the game by now, and read() opens it.
                    if catalog.get(e.fix_id).ended(e.params):
                        journal.set_state(e.id, "undone", "it ended with its process")
            except JournalCorrupt:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad entry is left as it is, and the rest still settle
                say(f"(Could not check the change {e.id} ({e.fix_id}) in the fix journal: {exc})")
    except Exception as exc:  # noqa: BLE001 - a journal problem must not block the tool
        say(f"(Could not check the fix journal: {exc})")


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
    newest: dict[str, tuple[str, list[tuple[str, str, dict[str, Any]]]]] = {}
    for label, _folder, diag, _when in sources:              # oldest first, so newer reports win
        offers = fix_offers(diag)
        for fid in dict.fromkeys(o[0] for o in offers):
            newest[fid] = (label, [o for o in offers if o[0] == fid])
    return [(fid, title, params, label) for fid, (label, offers) in newest.items() for _, title, params in offers]
