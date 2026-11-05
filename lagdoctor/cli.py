"""The command line: watch, analyze, check, fix, undo, setup.

Output is plain ASCII so it prints on any console code page. Nothing here
changes a setting without asking first: `fix` shows the before and after and
asks, and `undo` restores from the journal.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from lagdoctor import __version__, paths

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CAPTURE_NAME = "capture.ldcap.json.gz"
DIAGNOSIS_NAME = "diagnosis.json"
REPORT_NAME = "report.html"
LAST_SESSION = "last-session.txt"
DEFAULT_HOTKEY = "ctrl+alt+l"


# ----------------------------------------------------------------- parser --

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="lagdoctor", description="Find out why a PC game lags, and fix what can be "
                                "fixed safely. It watches from outside the game and never touches it.")
    p.add_argument("--version", action="version", version=f"lag-doctor {__version__}")
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")

    w = sub.add_parser("watch", help="record while you play, then explain the lag")
    pick = w.add_mutually_exclusive_group()
    pick.add_argument("--game", metavar="NAME", help="the game's exe name, e.g. eldenring.exe")
    pick.add_argument("--pid", type=int, help="the game's process id")
    pick.add_argument("--auto", action="store_true", help="pick the busiest game with a window (default)")
    w.add_argument("--duration", type=_positive_float, metavar="S", help="stop after S seconds (default: Ctrl+C)")
    w.add_argument("--server", metavar="IP", type=_ip, help="also ping this game server")
    w.add_argument("--no-network", action="store_true", help="skip the network checks")
    el = w.add_mutually_exclusive_group()
    el.add_argument("--elevate", action="store_true", help="get frame timing via one UAC prompt, without asking")
    el.add_argument("--no-elevate", action="store_true", help="record without frame timing rather than elevate")
    w.add_argument("--out", metavar="DIR", help="where to save the capture and report")
    w.add_argument("--hotkey", default=DEFAULT_HOTKEY, help=f"the 'it lagged now' key (default {DEFAULT_HOTKEY}); "
                   "'none' to turn it off")
    w.add_argument("--capture-only", action="store_true", help=argparse.SUPPRESS)   # the elevated child
    w.add_argument("--out-file", type=Path, help=argparse.SUPPRESS)
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
    from lagdoctor.capture import target as tgt
    try:
        target = tgt.find_target(name=args.game, pid=args.pid, auto=args.auto or not (args.game or args.pid))
    except tgt.TargetNotFound as exc:
        say(f"Could not find the game: {exc}")
        say("Start the game first, or name it with --game NAME.exe or --pid PID.")
        return 2
    say(f"Watching {target.get('name')} (pid {target['pid']}, chosen by {target.get('how', '?')}).")

    exe, frames_why = _presentmon_exe()
    frames = exe is not None
    if frames and not args.capture_only:
        from lagdoctor.winapi import elevation
        if not elevation.can_trace():
            choice = elevation_choice(args.elevate, args.no_elevate, sys.stdin.isatty(), _ask)
            if choice == "elevate":
                code = _watch_elevated(args, target)
                if code is not None:
                    return code
            frames = False
            frames_why = ("frame timing was skipped: it needs admin rights or membership of Performance Log "
                          "Users (see `lagdoctor setup`)")

    from lagdoctor.capture.session import Session, SessionOptions, session_dir
    from lagdoctor.model import SourceStatus
    hotkey = None if (args.hotkey or "").lower() in ("", "none", "off") else args.hotkey
    opts = SessionOptions(duration_s=args.duration, network=not args.no_network, server_ip=args.server,
                          hotkey=hotkey, presentmon_exe=exe, frames=frames)
    if args.out_file:
        cap_path = Path(args.out_file)
        out_dir = cap_path.parent
        out_dir.mkdir(parents=True, exist_ok=True)
    else:
        out_dir = session_dir(Path(args.out) if args.out else paths.sessions_dir(), target)
        cap_path = out_dir / CAPTURE_NAME
    say("Recording. Play normally" + (f"; press {hotkey.title()} the moment it lags" if hotkey else "")
        + "; Ctrl+C stops." + (f" Stops by itself after {args.duration:.0f} s." if args.duration else ""))
    cap = Session(target, opts).run()
    if not frames:
        cap.sources["frames"] = SourceStatus("off", frames_why)
    cap.save(cap_path)
    if args.capture_only:
        say(f"Capture saved: {cap_path}")
        return 0
    return analyze_and_report(cap, cap_path, out_dir)


def elevation_choice(elevate: bool, no_elevate: bool, interactive: bool, ask: Callable[[str], bool]) -> str:
    """'elevate' or 'skip': what to do when frame timing needs rights we do not have."""
    if elevate:
        return "elevate"
    if no_elevate or not interactive:
        return "skip"
    say("Frame timing (FPS, stutters, CPU or GPU limit) needs admin rights. lag-doctor can record with one UAC")
    say("prompt; only the recording runs as admin, and nothing is changed. To never be asked, see `lagdoctor setup`.")
    return "elevate" if ask("Record with frame timing (shows a UAC prompt)? [Y/n] ", default=True) else "skip"


def elevated_argv(args: argparse.Namespace, target: dict[str, Any], cap_path: Path) -> list[str]:
    """The command the elevated child runs: the capture only, for this exact game, into this exact file."""
    argv = [sys.executable, "-m", "lagdoctor", "watch", "--pid", str(int(target["pid"])), "--capture-only",
            "--out-file", str(cap_path)]
    if args.duration:
        argv += ["--duration", repr(float(args.duration))]
    if args.server:
        argv += ["--server", args.server]
    if args.no_network:
        argv.append("--no-network")
    argv += ["--hotkey", args.hotkey or "none"]
    return argv


def _watch_elevated(args: argparse.Namespace, target: dict[str, Any]) -> int | None:
    """Run the capture in an elevated child, then analyse here. None = declined: carry on without frames."""
    from lagdoctor.capture.session import session_dir
    from lagdoctor.model import Capture
    from lagdoctor.winapi import elevation
    out_dir = session_dir(Path(args.out) if args.out else paths.sessions_dir(), target)
    cap_path = out_dir / CAPTURE_NAME
    say("Opening the recording in a new window (approve the UAC prompt). Come back here when it ends.")
    try:
        code = elevation.relaunch_elevated(elevated_argv(args, target, cap_path), cwd=str(PROJECT_ROOT))
    except PermissionError:
        say("The UAC prompt was declined, so lag-doctor records without frame timing.")
        try:
            out_dir.rmdir()
        except OSError:
            pass
        return None
    if not cap_path.is_file():
        say(f"The recording window ended (exit code {code}) without saving a capture.")
        return 1
    return analyze_and_report(Capture.load(cap_path), cap_path, out_dir)


def _presentmon_exe() -> tuple[Path | None, str]:
    try:
        from lagdoctor import setup as pm_setup
        return pm_setup.ensure_presentmon(allow_download=False), ""
    except Exception as exc:  # noqa: BLE001 - not set up yet: record without frames and say why
        return None, f"PresentMon is not set up ({exc}); run `lagdoctor setup`"


# ---------------------------------------------------------------- analyze --

def analyze_and_report(cap, cap_path: Path, out_dir: Path) -> int:
    from lagdoctor.analysis.diagnose import diagnose
    from lagdoctor.report import console, html
    d = diagnose(cap)
    _write_json(out_dir / DIAGNOSIS_NAME, d.to_json())
    report = html.write(d, cap, out_dir / REPORT_NAME, capture_path=cap_path)
    print(console.render(d, cap, report_path=report, width=_width()))
    _remember(out_dir)
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    from lagdoctor.model import Capture
    path = Path(args.capture)
    if not path.is_file():
        say(f"No capture at {path}.")
        return 2
    cap = Capture.load(path)
    return analyze_and_report(cap, path, path.parent)


def cmd_check(args: argparse.Namespace) -> int:
    from lagdoctor.analysis.diagnose import diagnose
    from lagdoctor.capture import inventory
    from lagdoctor.model import Capture
    from lagdoctor.report import console
    inv = inventory.collect(None)
    cap = Capture(started_at=datetime.now().astimezone().isoformat(timespec="seconds"), clock_origin=0.0,
                  duration_s=0.0, target={"name": "settings check-up"}, sources={}, inventory=inv)
    print(console.render(diagnose(cap), cap, width=_width()))
    return 0


# -------------------------------------------------------------- fix / undo --

def cmd_fix(args: argparse.Namespace) -> int:
    from lagdoctor.fixes import catalog
    from lagdoctor.fixes.journal import Journal
    last = load_last_diagnosis()
    if last is None:
        say("No report yet: run `lagdoctor watch` first.")
        return 2
    offers = fix_offers(last)
    if args.list or not args.ids:
        if not offers:
            say("The last report suggested no automatic fixes.")
        for fix_id, title, _ in offers:
            say(f"  {fix_id:28} {title}")
        if offers and not args.ids:
            say("Apply one with: lagdoctor fix ID")
        return 0
    if _elevated():
        say("lag-doctor is running as administrator. Fixes change your own user's settings, so run `lagdoctor fix` "
            "from a normal (not elevated) terminal.")
        return 2
    journal, status = Journal(), 0
    for fix_id in args.ids:
        matching = [o for o in offers if o[0] == fix_id]
        if not matching:
            say(f"The last report did not suggest '{fix_id}'. See `lagdoctor fix --list`.")
            status = 2
            continue
        for _, title, params in matching:
            fix = catalog.get(fix_id)
            plan = fix.plan(params)
            say(f"{title}")
            say(f"  where:  {plan.target}")
            say(f"  change: {plan.summary}")
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
    return status


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
    from lagdoctor.fixes.journal import Journal
    journal = Journal()
    entries = journal.undoable()
    if not entries:
        say("Nothing to undo: lag-doctor has no changes in place.")
        return 0
    if not args.ids and not args.all:
        say("Changes lag-doctor made (newest first):")
        for e in entries:
            say(f"  {e.id}  {e.fix_id:26} {e.target}  ({e.created_at})")
        say("Undo one with: lagdoctor undo ID   (or --all)")
        return 0
    chosen = entries if args.all else [e for e in entries if e.id in args.ids or e.fix_id in args.ids]
    if not chosen:
        say("No change in place matches " + ", ".join(args.ids) + ". See `lagdoctor undo`.")
        return 2
    status = 0
    for e in chosen:
        res = catalog.get(e.fix_id).undo(e, journal, force=args.force)
        say(f"{'Restored' if res.ok else 'Left alone'}: {res.message}")
        if not res.ok:
            status = 1
    return status


def _settle_journal() -> None:
    """Settle changes a crash left half-done, and session-only changes whose process has ended."""
    try:
        from lagdoctor.fixes import catalog
        from lagdoctor.fixes.base import is_gone
        from lagdoctor.fixes.journal import Journal, JournalCorrupt
        journal = Journal()
        try:
            entries = journal.entries()
        except JournalCorrupt as exc:
            say(f"The fix journal could not be read; it was set aside at {exc.parked}. Nothing was lost.")
            return
        for e in entries:
            if e.state == "pending":
                res = catalog.get(e.fix_id).heal(e, journal)
                say(f"Settled a change that was interrupted last time: {res.message}")
            elif e.state == "applied" and e.session_only:
                fix = catalog.get(e.fix_id)
                if is_gone(fix._safe_read(e.params)):
                    journal.set_state(e.id, "undone", "it ended with its process")
    except Exception as exc:  # noqa: BLE001 - a journal problem must not block the tool
        say(f"(Could not check the fix journal: {exc})")


# ------------------------------------------------------------------ setup --

def cmd_setup(args: argparse.Namespace) -> int:
    from lagdoctor import setup as pm_setup
    from lagdoctor.winapi import elevation
    try:
        exe = pm_setup.ensure_presentmon(allow_download=False)
    except Exception:  # noqa: BLE001 - missing (or failing verification): offer the download
        if not (args.download or _ask("Download Intel PresentMon 2.6.0 (980,320 bytes, MIT licence, Intel-signed) "
                                      "from github.com/GameTechDev/PresentMon? [y/N] ", default=False)):
            say("Not downloaded. Frame timing stays unavailable until PresentMon is set up.")
            return 1
        exe = pm_setup.ensure_presentmon(allow_download=True)
    say(f"PresentMon: ready and verified at {exe}")
    if elevation.can_trace():
        say("Frame timing: ready (this account may start ETW traces).")
    else:
        say("Frame timing needs admin rights. Two ways:")
        say("  1. Let `lagdoctor watch` ask for one UAC prompt each time (nothing to set up).")
        say("  2. Join the Performance Log Users group once, then sign out and in. No prompts after that:")
        for line in str(getattr(pm_setup, "PERFORMANCE_LOG_USERS_HOWTO", "")).strip().splitlines():
            say("     " + line)
    return 0


# ---------------------------------------------------------------- helpers --

def say(text: str) -> None:
    print(text.encode("ascii", "replace").decode("ascii"), flush=True)


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
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _remember(out_dir: Path) -> None:
    (paths.home() / LAST_SESSION).write_text(str(out_dir), encoding="utf-8")


def load_last_diagnosis() -> dict[str, Any] | None:
    """The last report's diagnosis: the remembered session first, else the newest one on disk."""
    candidates: list[Path] = []
    marker = paths.home() / LAST_SESSION
    if marker.is_file():
        candidates.append(Path(marker.read_text(encoding="utf-8").strip()) / DIAGNOSIS_NAME)
    candidates += sorted(paths.sessions_dir().glob(f"*/{DIAGNOSIS_NAME}"), key=lambda p: p.stat().st_mtime,
                         reverse=True)
    for c in candidates:
        if c.is_file():
            try:
                return json.loads(c.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
    return None
