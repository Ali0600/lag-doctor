"""The command line: watch, analyze, check, fix, undo, setup.

Output is plain ASCII so it prints on any console code page. Nothing here
changes a setting without asking first: `fix` shows the before and after and
asks, and `undo` restores from the journal.

Paths are made absolute where they come in (the elevated child runs in another
folder, perhaps as another account). Once a recording has started, nothing the
user does at the console loses it: Ctrl+C stops it (a second press is only
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
INVENTORY_HANDOVER = "inventory-handover.json"   # system details the parent read, for the elevated child
DEFAULT_HOTKEY = "ctrl+alt+l"
MAX_INVENTORY_BYTES = 16 << 20

# Exit codes of `watch`. The elevated child's window closes when it ends, so the parent explains these.
EXIT_BAD_ARGS = 2              # also: the game could not be found
EXIT_TARGET_CHANGED = 3        # the PID now belongs to another process (the game exited during the prompt)
EXIT_BUSY = 4                  # another recording is running in this Windows session

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
    # The elevated child (hidden): record this exact process into this exact file, with what the parent
    # found in the user's own account.
    w.add_argument("--capture-only", action="store_true", help=argparse.SUPPRESS)
    w.add_argument("--out-file", type=Path, help=argparse.SUPPRESS)
    w.add_argument("--create-time", type=_create_time, help=argparse.SUPPRESS)
    w.add_argument("--presentmon-exe", type=Path, help=argparse.SUPPRESS)
    w.add_argument("--inventory-file", type=Path, help=argparse.SUPPRESS)
    w.add_argument("--parent-sid", type=_sid, help=argparse.SUPPRESS)
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
    return p


def _positive_float(text: str) -> float:
    v = float(text)
    if not v > 0:                      # also refuses NaN
        raise argparse.ArgumentTypeError("must be more than 0")
    return v


def _ip(text: str) -> str:
    """An IP address only: it ends up on the elevated child's command line."""
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


def _abs_path(text: str) -> Path:
    if not text.strip():
        raise argparse.ArgumentTypeError("give a folder")
    return Path(text).resolve()


def _create_time(text: str) -> int:
    v = int(text.strip(), 10)
    if v < 0:
        raise argparse.ArgumentTypeError("a create time is not negative")
    return v


def _sid(text: str) -> str:
    if not re.fullmatch(r"S-1(-[0-9]{1,10}){1,15}", text.strip()):
        raise argparse.ArgumentTypeError(f"{text!r} is not a SID")
    return text.strip()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 2
    if args.cmd in ("watch", "fix", "undo", "check") and not getattr(args, "capture_only", False):
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
    child = bool(args.capture_only)
    if child:
        problem = _child_args_problem(args)
        if problem:
            say(f"The elevated recording was started wrongly ({problem}); nothing was recorded.")
            return EXIT_BAD_ARGS
    # One recording at a time, checked before PresentMon or any ETW session can start (they share fixed
    # session names, so a second one would silently end the first one's tracing).
    lock = sess.CaptureLock()
    if not _take_lock(lock):
        return EXIT_BUSY
    with lock:
        auto = args.auto or (args.game is None and args.pid is None)
        try:
            target = tgt.find_target(name=args.game, pid=args.pid, auto=auto)
        except tgt.TargetNotFound as exc:
            say(f"Could not find the game: {exc}")
            say("Start the game first, or name it with --game NAME.exe or --pid PID.")
            return EXIT_BAD_ARGS
        if child and target.get("create_time") != args.create_time:
            say(f"The game exited while the UAC prompt was up: PID {target['pid']} now belongs to another process "
                f"({target.get('name')}). Nothing was recorded.")
            return EXIT_TARGET_CHANGED
        say(f"Watching {target.get('name')} (pid {target['pid']}, chosen by {target.get('how', '?')}).")

        notes: list[str] = []
        inventory = None
        if child:
            exe, frames_why = _child_presentmon(args)
            inventory, inventory_note = _child_inventory(args)
            notes += [n for n in (inventory_note, _account_note(args, exe)) if n]
        else:
            exe, frames_why = _presentmon_exe()
        frames = exe is not None
        if frames and not child:
            from lagdoctor.winapi import elevation
            if not elevation.can_trace():
                choice = elevation_choice(args.elevate, args.no_elevate, sys.stdin.isatty(), _ask)
                if choice == "elevate":
                    lock.release()                    # the elevated child records, so it holds the lock
                    code = _watch_elevated(args, target, exe)
                    if code is not None:
                        return code
                    if not _take_lock(lock):          # declined: record here after all
                        return EXIT_BUSY
                frames = False
                frames_why = ("frame timing was skipped: it needs admin rights or membership of Performance Log "
                              "Users (see `lagdoctor setup`)")
        return _record(args, target, exe, frames, frames_why, inventory, notes)


def _record(args: argparse.Namespace, target: dict[str, Any], exe: Path | None, frames: bool, frames_why: str,
            inventory: dict[str, Any] | None, notes: list[str]) -> int:
    """Record, save, and (unless this is the elevated child) analyse. Ctrl+C, Ctrl+Break and closing the
    window all stop the recording; none of them can lose it once it is being stopped and saved."""
    from lagdoctor.capture import session as sess
    from lagdoctor.model import SourceStatus
    hotkey = None if (args.hotkey or "").lower() in ("", "none", "off") else args.hotkey
    opts = sess.SessionOptions(duration_s=args.duration, network=not args.no_network, server_ip=args.server,
                               hotkey=hotkey, presentmon_exe=exe, frames=frames)
    if args.out_file:
        cap_path = Path(args.out_file).resolve()
        out_dir = cap_path.parent
        out_dir.mkdir(parents=True, exist_ok=True)
    else:
        out_dir = sess.session_dir(args.out or paths.sessions_dir(), target)
        cap_path = out_dir / CAPTURE_NAME
    say("Recording. Play normally" + (f"; press {hotkey.title()} the moment it lags" if hotkey else "")
        + "; Ctrl+C stops." + (f" Stops by itself after {args.duration:.0f} s." if args.duration else ""))
    stop, saved = threading.Event(), threading.Event()
    with sess.CtrlC(stop, say) as guard, _ConsoleControl(stop, saved, network=opts.network):
        try:
            cap = sess.Session(target, opts, inventory=inventory).run(stop, ctrl_c=guard)
            if not frames:
                cap.sources["frames"] = SourceStatus("off", frames_why)
            cap.notes.extend(notes)
            n = len(cap.frames.get("t") or [])
            say(f"Saving {n:,} frames..." if n else "Saving the capture...")
            cap.save(cap_path)
        finally:
            saved.set()
    if args.capture_only:
        say(f"Capture saved: {cap_path}")
        return 0
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
        say("Another lag-doctor recording is already running (in another window, or in the administrator window "
            "of an earlier watch). Stop it first, then try again.")
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
        if network:
            from lagdoctor.capture.netflows import SESSION_NAME
            from lagdoctor.winapi import etw
            self.net_session, self._etw = SESSION_NAME, etw      # imported now: never inside the handler
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


def elevated_argv(args: argparse.Namespace, target: dict[str, Any], cap_path: Path, *,
                  presentmon_exe: Path | None = None, inventory_file: Path | None = None,
                  parent_sid: str | None = None) -> list[str]:
    """The command the elevated child runs: the capture only, for this exact process, into this exact file.

    -E -s: the elevated Python ignores PYTHON* variables and the user's own site-packages (-I would also drop
    the project folder from sys.path, which `-m lagdoctor` needs). Paths must be absolute: the child runs in
    another folder, and perhaps as another account with another profile.
    """
    for p in (cap_path, presentmon_exe, inventory_file):
        if p is not None and not Path(p).is_absolute():
            raise ValueError(f"the elevated recording needs absolute paths, not {str(p)!r}")
    argv = [sys.executable, "-E", "-s", "-m", "lagdoctor", "watch",
            "--pid", str(int(target["pid"])), "--capture-only",
            "--create-time", str(int(target["create_time"])), "--out-file", str(cap_path)]
    if args.duration:
        argv += ["--duration", repr(float(args.duration))]
    if args.server:
        argv += ["--server", args.server]
    if args.no_network:
        argv.append("--no-network")
    argv += ["--hotkey", args.hotkey or "none"]
    if presentmon_exe is not None:
        argv += ["--presentmon-exe", str(presentmon_exe)]
    if inventory_file is not None:
        argv += ["--inventory-file", str(inventory_file)]
    if parent_sid:
        argv += ["--parent-sid", parent_sid]
    return argv


def _watch_elevated(args: argparse.Namespace, target: dict[str, Any], exe: Path | None) -> int | None:
    """Run the capture in an elevated child, then analyse here. None = it never ran (declined, or the window
    could not be opened): carry on without frames."""
    from lagdoctor.capture import session as sess
    from lagdoctor.model import Capture, CaptureError
    from lagdoctor.winapi import elevation
    # Absolute even if LAGDOCTOR_HOME is relative: the child runs in another folder.
    out_dir = sess.session_dir(args.out or paths.sessions_dir(), target).resolve()
    cap_path = out_dir / CAPTURE_NAME
    handover = out_dir / INVENTORY_HANDOVER
    exe = Path(exe).resolve() if exe is not None else None
    child = None
    ended = False                  # True once the child is known not to be running any more
    try:
        # Read here, in the user's own account: an administrator's approval would run the child as that
        # account, whose HKCU and profile are not the user's.
        _write_json(handover, sess._collect_inventory(target))
        argv = elevated_argv(args, target, cap_path, presentmon_exe=exe, inventory_file=handover,
                             parent_sid=_own_sid())
        say("Opening the recording in a new window (approve the UAC prompt). Come back here when it ends.")
        try:
            child = elevation.launch_elevated(argv, cwd=str(PROJECT_ROOT))
        except PermissionError:
            ended = True
            say("The UAC prompt was declined, so lag-doctor records without frame timing.")
            return None
        except OSError as exc:
            ended = True
            say(f"The administrator window could not be opened ({exc}), so lag-doctor records without frame timing.")
            return None
        code = _wait_for_child(child, cap_path)
        if code is None:
            return 130                              # the user stopped waiting; the recording goes on there
        ended = True
    finally:
        if child is not None:
            child.close()
        if child is None or ended:                  # never while the child may still write there
            handover.unlink(missing_ok=True)
            if not cap_path.exists():
                _remove_empty_dir(out_dir)
    if not cap_path.is_file():
        say(_child_failure(code))
        return 1
    try:
        cap = Capture.load(cap_path)
    except (CaptureError, OSError) as exc:
        say(f"The recording window saved {cap_path}, but it could not be read: {exc}.")
        return 1
    if not _same_process(cap.target, target):
        say(f"The recording window recorded a different process (PID {cap.target.get('pid')}, "
            f"{cap.target.get('name')}) from the game chosen here (PID {target['pid']}, {target.get('name')}), so "
            f"it was not analysed. The file is kept at {cap_path}.")
        return 1
    return _analyze_saved(cap, cap_path, out_dir)


def _wait_for_child(child, cap_path: Path) -> int | None:
    """The elevated child's exit code. A first Ctrl+C here says where the recording runs and keeps waiting;
    a second one stops waiting (None) and leaves the recording running in its own window."""
    presses = 0
    while True:
        try:
            return child.wait()
        except KeyboardInterrupt:
            presses += 1
            if presses == 1:
                say("The recording keeps running in the other (administrator) window: stop it there with Ctrl+C. "
                    f"It will be saved to {cap_path}. Waiting here to analyse it; Ctrl+C again stops waiting.")
                continue
            say(f"Stopped waiting. The recording is still running in the other window and will be saved to "
                f"{cap_path}. When it ends, analyse it with: lagdoctor analyze \"{cap_path}\"")
            say("Until you record again, `lagdoctor fix` keeps offering what your earlier reports suggested.")
            return None


def _child_failure(code: int) -> str:
    if code == EXIT_BUSY:
        return ("Another lag-doctor recording was already running, so the administrator window did not record. "
                "Stop it first, then try again.")
    if code == EXIT_TARGET_CHANGED:
        return ("The game exited while the UAC prompt was up (its PID now belongs to another process), so nothing "
                "was recorded. Start the game again, then run `lagdoctor watch` again.")
    if code == EXIT_BAD_ARGS:
        return "The recording window could not find the game (exit code 2), so nothing was recorded."
    return f"The recording window ended (exit code {code}) without saving a capture."


def _same_process(recorded: Any, chosen: dict[str, Any]) -> bool:
    """A process is (pid, create_time): Windows reuses PIDs."""
    return (isinstance(recorded, dict) and recorded.get("pid") == chosen.get("pid")
            and recorded.get("create_time") == chosen.get("create_time"))


def _own_sid() -> str | None:
    from lagdoctor.winapi import elevation
    try:
        return elevation.token_user_sid()
    except OSError:
        return None


def _child_args_problem(args: argparse.Namespace) -> str | None:
    if args.pid is None:
        return "no --pid"
    if args.create_time is None:
        return "no --create-time"
    for flag in ("out_file", "presentmon_exe", "inventory_file"):
        value = getattr(args, flag)
        if (flag == "out_file" and value is None) or (value is not None and not Path(value).is_absolute()):
            return f"--{flag.replace('_', '-')} must be an absolute path"
    return None


def _child_presentmon(args: argparse.Namespace) -> tuple[Path | None, str]:
    """The PresentMon the parent verified, checked again here (this account's profile may have none)."""
    if args.presentmon_exe is None:
        return _presentmon_exe()
    from lagdoctor import setup as pm_setup
    exe = Path(args.presentmon_exe)
    try:
        pm_setup.verify(exe)
    except pm_setup.SetupError as exc:
        return None, f"frame timing was skipped: the PresentMon this recording was given failed its check: {exc}"
    return exe, ""


def _child_inventory(args: argparse.Namespace) -> tuple[dict[str, Any] | None, str | None]:
    """The system details the parent read in the user's own account. (None, why) when they cannot be used:
    the session then reads its own, which may be another account's per-user settings."""
    if args.inventory_file is None:
        return None, None
    try:
        with open(args.inventory_file, "rb") as f:
            data = f.read(MAX_INVENTORY_BYTES + 1)
        if len(data) > MAX_INVENTORY_BYTES:
            raise ValueError("the file is too large")
        inv = json.loads(data)
        if not isinstance(inv, dict):
            raise ValueError("it holds no system details")
    except (OSError, ValueError, RecursionError) as exc:
        return None, (f"The system details read by the window that started this recording could not be used "
                      f"({type(exc).__name__}: {exc}); they were read again in the administrator window, so "
                      "per-user settings may be that account's.")
    return inv, None


def _account_note(args: argparse.Namespace, exe: Path | None) -> str | None:
    """A note when an administrator's approval ran this recording as another Windows account."""
    if not args.parent_sid:
        return None
    from lagdoctor.winapi import elevation
    try:
        mine = elevation.token_user_sid()
    except OSError as exc:
        return f"lag-doctor could not tell which Windows account this recording ran as ({exc})."
    if mine == args.parent_sid:
        return None
    who = f"{os.environ.get('USERDOMAIN', '?')}\\{os.environ.get('USERNAME', '?')}"
    return (f"This recording ran as another Windows account ({who}) than the one that started it, because that "
            "account approved the UAC prompt." + (f" Frame timing used {exe}." if exe else ""))


def _remove_empty_dir(folder: Path) -> None:
    try:
        folder.rmdir()                  # only succeeds when empty
    except OSError:
        pass


def _presentmon_exe() -> tuple[Path | None, str]:
    from lagdoctor import setup as pm_setup
    try:
        return pm_setup.ensure_presentmon(allow_download=False), ""
    except pm_setup.SetupError as exc:          # missing or failed verification: record without frames, say why
        return None, f"frame timing was skipped: {exc}"


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
    print(console.render(d, cap, report_path=report, width=_width()))
    if remember:
        _remember(out_dir)
    return 0


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
                say(f"  {'Done' if res.ok else 'Not changed'}: {res.message}")
                if res.ok and res.state == "applied":
                    say(f"  Undo it any time with: lagdoctor undo {res.entry_id}")
                elif not res.ok:
                    status = 1
    except JournalCorrupt as exc:
        parked = getattr(exc, "parked", None)
        say("Fixes are paused: the fix journal, where lag-doctor records how to undo each change, could not be "
            "read, so no further change was made" + (f". Its contents were copied to {parked}" if parked else "")
            + f". The journal is {paths.journal_path()}.")
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
    if elevation.can_trace():
        say("Frame timing: ready (this account may start ETW traces).")
    else:
        say("Frame timing needs admin rights. Two ways:")
        say("  1. Let `lagdoctor watch` ask for one UAC prompt each time (nothing to set up).")
        say("  2. Join the Performance Log Users group once, then sign out and in. No prompts after that:")
        for line in pm_setup.performance_log_users_howto().rstrip().splitlines():
            say("     " + line)
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
