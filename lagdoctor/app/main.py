"""Starting the app, and the controller that answers the page.

The start order (docs/LAUNCHER-PLAN.md, The window): the app's ID, per-monitor DPI awareness, one instance only (a
second start brings the first window forward), the refusals (started elevated, no WebView2 Runtime; another app's
private copy of AppData is refused earlier, in cli.main), faulthandler to the app's log, then STA COM, the window and
the environment. Every request the page makes is answered on a worker thread through the view-models, and the answer
is posted back to the page from the UI thread.
"""
from __future__ import annotations

import faulthandler
import hashlib
import os
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from lagdoctor import __version__, paths
from lagdoctor.app import APP_ID, ASSETS, ICON, bridge, library, views
from lagdoctor.app.window import AppWindow
from lagdoctor.winapi import webview2 as wv

MUTEX = "Local\\lag-doctor-app-"          # plus the home folder's tag: one app per lag-doctor home
CLASS = "LagDoctorApp-"                   # the window class, with the same tag, so a second start can find it
LOG_NAME = "app.log"
OLD_LOG_NAME = "app-previous.log"
MIN_BUILD = 1661                          # CreateSharedBuffer, the newest call the app needs (SDK 1.0.1661.34)
LAG_KEY = "ctrl+alt+l"                    # cli.DEFAULT_HOTKEY; the app never imports cli


def tell(text: str) -> None:
    """A message before or instead of the window: printed when a console started the app, else in a message box
    (the Start-menu shortcut runs pythonw.exe, which has no console)."""
    if sys.stdout is not None:
        try:
            print(text.encode("ascii", "replace").decode("ascii"), flush=True)
            return
        except (OSError, ValueError):
            pass
    wv.MessageBoxW(None, text, "lag-doctor", wv.MB_OK | wv.MB_ICONWARNING | wv.MB_SETFOREGROUND)


def instance_tag(home: Path) -> str:
    """What names this home's single instance: a test's own LAGDOCTOR_HOME never meets the owner's running app."""
    return hashlib.sha256(str(Path(home).resolve()).casefold().encode("utf-8")).hexdigest()[:12]


def refusal(elevated: bool | None, runtime: dict | None) -> str | None:
    """Why the app will not open its window, in plain words, or None. Pure: run() reads the facts."""
    if elevated:
        return ("lag-doctor's window does not run as administrator. Start it from the Start menu, or from a "
                "PowerShell window that was not opened with 'Run as administrator'.")
    if runtime is None:
        return ("lag-doctor's window needs Microsoft's WebView2 Runtime, which this PC does not have. Install it from "
                "Microsoft (search for 'WebView2 Runtime'); the lagdoctor commands keep working without it.")
    if runtime.get("build", 0) < MIN_BUILD:
        return (f"lag-doctor's window needs a newer WebView2 Runtime than this PC has ({runtime.get('version')}). "
                "Windows Update or Microsoft's installer brings it up to date.")
    return None


def bring_forward(class_name: str) -> bool:
    """Show the window a first start opened; False when there is none to show."""
    hwnd = wv.FindWindowW(class_name, None)
    if not hwnd:
        return False
    if wv.IsIconic(hwnd):
        wv.ShowWindow(hwnd, wv.SW_RESTORE)
    wv.SetForegroundWindow(hwnd)
    return True


def open_log(home: Path):
    """The app's log, the previous run's kept beside it; returns (file, write)."""
    path = home / LOG_NAME
    try:
        if path.is_file():
            os.replace(path, home / OLD_LOG_NAME)
    except OSError:
        pass
    f = open(path, "a", encoding="utf-8", buffering=1)
    lock = threading.Lock()

    def write(text: str) -> None:
        with lock:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")
    return f, write


class App:
    """The controller: owns the window, the session listing and the last capture it loaded."""

    def __init__(self, *, home: Path, runtime: dict, create_fn, log: Callable[[str], None],
                 tell: Callable[[str], None] = tell, activate: bool = True, on_ready: Callable[["App"], None] | None
                 = None, opener: Callable[[str], None] | None = None) -> None:
        self.home = Path(home)
        self.sessions = self.home / "sessions"
        self.sessions.mkdir(parents=True, exist_ok=True)
        self.generic_error = (f"Something went wrong reading this. The details are in lag-doctor's log, "
                              f"{self.home / LOG_NAME}.")
        self.runtime = runtime
        self.log = log
        self.tell = tell
        self.on_ready = on_ready
        self.opener = opener or os.startfile        # opens a report in the browser, or the sessions folder
        self.lock = threading.Lock()
        self.entries: list[library.Entry] = []
        self.loaded: tuple | None = None            # (session id, capture, diagnosis, main chain): the last one read
        self.readiness: dict | None = None
        self.window = AppWindow(runtime=runtime, create_fn=create_fn, data_folder=self.home / "webview",
                                assets=ASSETS, icon=ICON, class_name=CLASS + instance_tag(self.home),
                                on_message=self.on_message, on_failed=self.on_failed, log=log, activate=activate)

    def run(self) -> None:
        self.window.run()

    # ---------------------------------------------------------------- the page's requests (UI thread)

    def on_message(self, req: bridge.Request) -> None:
        if req.kind == "ready":
            if self.on_ready is not None:
                self.on_ready(self)
            return
        work = getattr(self, "do_" + req.kind, None)
        if work is None:                            # bridge.REQUESTS and these methods are held equal by a test
            self.window.post(bridge.reply(req.id, error=self.generic_error))
            return
        if not self.window.submit(lambda: work(**req.args), lambda result, error: self.answer(req, result, error)):
            self.window.post(bridge.reply(req.id, error="lag-doctor is still busy with earlier requests."))

    def answer(self, req: bridge.Request, result: Any, error: BaseException | None) -> None:
        if error is not None:
            if isinstance(error, Gone):
                self.window.post(bridge.reply(req.id, error=str(error)))
                return
            self.log(f"request {req.kind} failed:\n{''.join(traceback.format_exception(error))}")
            self.window.post(bridge.reply(req.id, error=self.generic_error))
            return
        if isinstance(result, Open):                # ShellExecute wants COM: on the UI thread, which is STA
            try:
                self.opener(str(result.path))
            except OSError as exc:
                self.log(f"opening {result.path}: {exc}")
                self.window.post(bridge.reply(req.id, error=f"Windows could not open it ({exc.strerror or exc})."))
                return
            result = {}
        data, buffer = result if isinstance(result, tuple) else (result, None)
        if buffer is not None:
            payload, meta = buffer
            try:
                token = self.window.post_buffer(payload, dict(meta, request=req.id))
            except Exception as exc:  # noqa: BLE001 - the rest of Results still shows, and the chart says why not
                self.log(f"handing the frame times over: {exc}")
                token = None
            data = dict(data, frames_token=token)
            if token is None:
                data["frames"] = dict(data["frames"], missing="The frame times could not be handed to this window; "
                                                             "the report page shows them.")
        self.window.post(bridge.reply(req.id, data))

    def on_failed(self, why: str) -> None:
        report = self.last_report()
        self.tell(f"lag-doctor's window could not open: {why}."
                  + (" Your last report opens in your browser instead." if report else ""))
        if report:
            try:
                self.opener(str(report))
            except OSError as exc:
                self.log(f"opening the last report: {exc}")

    def last_report(self) -> Path | None:
        sid = library.last_session(self.home, self.sessions)
        report = self.sessions / sid / library.REPORT_NAME if sid else None
        return report if report is not None and report.is_file() else None

    # ---------------------------------------------------------------- the work (worker threads)

    def scan(self) -> list[library.Entry]:
        entries = library.scan(self.sessions)
        with self.lock:
            self.entries = entries
        return entries

    def entry(self, session: str) -> library.Entry:
        """The listed session of this id, listing again once if it is not known yet. Gone when there is none."""
        with self.lock:
            found = next((e for e in self.entries if e.id == session), None)
        if found is None:
            found = next((e for e in self.scan() if e.id == session), None)
        if found is None or not found.is_recording:
            raise Gone("That recording is no longer there.")
        return found

    def get_readiness(self) -> dict:
        from lagdoctor import setup
        from lagdoctor.winapi import elevation
        try:
            setup.ensure_presentmon(allow_download=False)
            ok, why = True, ""
        except setup.SetupError as exc:
            ok, why = False, str(exc)
        self.readiness = views.readiness_view(ok, why, elevation.trace_rights())
        return self.readiness

    def do_library(self) -> dict:
        entries = self.scan()
        return views.library_view(entries, self.readiness or self.get_readiness())

    def do_game(self, game: str) -> dict:
        entries = self.scan()
        view = views.game_view(entries, game, library.last_session(self.home, self.sessions))
        if view is None:
            raise Gone("That game has no recordings any more.")
        return view

    def load(self, session: str) -> tuple:
        """(entry, capture, diagnosis, main chain) of a recording, kept for the next request about it."""
        from lagdoctor.analysis.frames import main_chain
        from lagdoctor.model import Capture, CaptureError, Diagnosis
        entry = self.entry(session)
        with self.lock:
            if self.loaded is not None and self.loaded[0] == session:
                return (entry,) + self.loaded[1:]
        try:
            cap = Capture.load(entry.capture)
        except CaptureError as exc:
            raise Gone(f"This recording cannot be read: {exc}") from None
        except MemoryError:
            raise Gone("This recording is too large to open on this PC.") from None
        except OSError as exc:
            raise Gone(f"This recording could not be read ({exc.strerror or exc}).") from None
        saved = library.read_diagnosis(entry)
        try:
            d = Diagnosis.from_json(saved)
        except ValueError:
            from lagdoctor.analysis.diagnose import diagnose
            self.log(f"{session}: its diagnosis.json could not be read; analysing the capture again")
            d = diagnose(cap)
        main = main_chain(cap)
        with self.lock:
            self.loaded = (session, cap, d, main)
        return entry, cap, d, main

    def do_results(self, session: str) -> tuple:
        entry, cap, d, main = self.load(session)
        is_last = library.last_session(self.home, self.sessions) == session
        view = views.results_view(entry, d, cap, main, is_last, (entry.folder / library.REPORT_NAME).is_file())
        payload, count = views.frame_bytes(main)
        return view, ((payload, {"kind": "frames", "session": session, "count": count}) if count else None)

    def do_moment(self, session: str, event: int) -> dict:
        _, cap, d, main = self.load(session)
        view = views.moment_view(cap, d, main, event)
        if view is None:
            raise Gone("That lag moment is not in this recording.")
        return view

    def do_compare(self, a: str, b: str) -> dict:
        ea, eb = self.entry(a), self.entry(b)
        if ea.game_key != eb.game_key:
            raise Gone("Those two recordings are of different games.")
        return views.compare_view(ea, eb)

    def do_settings(self) -> dict:
        return views.settings_view(self.get_readiness(), str(self.sessions), self.runtime.get("version"),
                                   __version__, LAG_KEY)

    def do_open_report(self, session: str) -> "Open":
        report = self.entry(session).folder / library.REPORT_NAME
        if not report.is_file():
            raise Gone("This recording has no report page.")
        return Open(report)

    def do_open_folder(self) -> "Open":
        return Open(self.sessions)

    def do_test_sound(self) -> dict:
        import winsound
        winsound.PlaySound("SystemAsterisk", winsound.SND_ALIAS | winsound.SND_ASYNC)
        return {}


class Gone(Exception):
    """A request about something the app cannot show, said in plain words to the page."""


@dataclass(frozen=True)
class Open:
    """A file or folder a request found to open: answer() opens it on the UI thread."""
    path: Path


def run() -> int:
    """`lagdoctor app`: the window, from start to close. 0 when it ran (or a first start's window was brought
    forward), 1 when it refused or could not open."""
    from lagdoctor.winapi import elevation
    wv.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    wv.SetProcessDpiAwarenessContext(wv.DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
    home = paths.home()
    tag = instance_tag(home)
    try:
        mutex = elevation.hold_named_mutex(MUTEX + tag)
    except OSError:
        mutex = 0                                   # cannot tell: open anyway rather than never
    if mutex is None:
        if not bring_forward(CLASS + tag):
            tell("lag-doctor's window is already starting.")
        return 0
    try:
        try:
            elevated = elevation.is_elevated()
        except OSError:
            elevated = None
        runtime = wv.find_runtime()
        why = refusal(elevated, runtime)
        create_fn = None
        if why is None:
            try:
                create_fn = wv.environment_creator(runtime["dll"])
            except (OSError, AttributeError) as exc:
                why = (f"lag-doctor's window could not load Microsoft's WebView2 Runtime ({exc}). The lagdoctor "
                       "commands keep working without it.")
        if why is not None:
            tell(why)
            return 1
        logf, log = open_log(home)
        faulthandler.enable(file=logf, all_threads=True)
        log(f"lag-doctor {__version__} app, WebView2 Runtime {runtime['version']}, python {sys.version.split()[0]}")
        hr = wv.CoInitializeEx(None, wv.COINIT_APARTMENTTHREADED)
        if hr < 0:
            tell(f"lag-doctor's window could not start COM (HRESULT 0x{hr & 0xFFFFFFFF:08X}).")
            return 1
        app = App(home=home, runtime=runtime, create_fn=create_fn, log=log)
        try:
            app.run()
        finally:
            app.window.destroy_icons()
            wv.CoUninitialize()
            log("closed" + (f" after a fault: {app.window.fatal}" if app.window.fatal else ""))
        if app.window.fatal:
            tell("lag-doctor's window hit an internal error and closed. The details are in app.log in lag-doctor's "
                 "folder (%LOCALAPPDATA%\\lag-doctor).")
            return 1
        return 0
    finally:
        if mutex:
            elevation.release_named_mutex(mutex)
