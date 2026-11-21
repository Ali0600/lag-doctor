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
import secrets
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from lagdoctor import __version__, paths
from lagdoctor.app import APP_ID, ASSETS, ICON, added, bridge, library, recording, sounds, views
from lagdoctor.app.window import AppWindow
from lagdoctor.winapi import webview2 as wv

MUTEX = "Local\\lag-doctor-app-"          # plus the home folder's tag: one app per lag-doctor home
CLASS = "LagDoctorApp-"                   # the window class, with the same tag, so a second start can find it
LOG_NAME = "app.log"
OLD_LOG_NAME = "app-previous.log"
MIN_BUILD = 1661                          # CreateSharedBuffer, the newest call the app needs (SDK 1.0.1661.34)
LAG_KEY = "ctrl+alt+l"                    # cli.DEFAULT_HOTKEY; the app never imports cli
LAUNCH_SLOW_S = 15.0                      # Play: a store link Explorer has not opened by then is said as not opened
LIST_WAIT_S = 20.0                        # the list of changes waits this long for a change being made to end
BUSY = "lag-doctor is still changing a setting: try again once it is done."
NO_JOURNAL = ("lag-doctor could not open its fix journal (another lag-doctor window may be using it), so it changed "
              "nothing. Try again in a moment.")
NO_LONGER_PLANNED = "That change is no longer waiting to be applied: click Apply this fix again."


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


def open_local(path: str) -> None:
    """Open one of lag-doctor's own files or folders (a report, the sessions folder) with Windows' default program.

    Anything else is refused with PermissionError (an OSError, which the window reports): a link of any scheme
    (steam://, https://), a relative path, a share, or a path that does not exist. So no request of the page can have
    a store link opened inside lag-doctor's own process, where a store client that starts would be lag-doctor's
    child; Play hands its links to Explorer instead (lagdoctor/stores/launch.py).
    """
    text = str(path)
    if not (len(text) > 3 and text[0].isascii() and text[0].isalpha() and text[1:3] in (":\\", ":/")) \
            or ":" in text[2:] or "\0" in text or not os.path.exists(text):
        raise PermissionError(0, "lag-doctor opens only its own files and folders this way", text)
    os.startfile(text)


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
    """The controller: owns the window, the session listing, the last capture it loaded and the recording it started.

    What a test replaces (each defaulting to the real one): `recorder()`, the recorder's command line, and
    `play_recorder(folder)`, Play's; `launcher`, which raises the relay's UAC prompt (elevation.launch_elevated);
    `play(name)`, the recording's sounds; `flash()`, the taskbar button's flash at a recording's end; `start_key`, the
    key the recorder measures from (None: the whole session, as M4 recorded), which also tells the running screen and
    the sounds to follow it; `open_link(link)`, which has Explorer open a store's link (stores/launch.play).

    `installed()` lists the store games Play can start (lagdoctor/stores: library.installed_games), and `covers(games)`
    reads their cover art ({(store, id): (type, bytes)}, stores/art.covers). Both default to none: only run() passes
    the stores' own, so a test that builds an App never lists the PC's own library or reads its art. `picker(hwnd)`,
    Add a game's (screen 6), returns the program the player chose in Windows' Open dialog (winapi/dialogs.pick_program,
    which only run() passes too: it waits for a person), or None. The games added by hand are the home's own
    (app/added.py). A game whose tile holds no cover shows its program's icon (stores/exe.py), from the first program
    views.icon_programs names for it that has one. `trash(path, hwnd)` moves a recording's folder to the Recycle Bin
    (winapi/recycle.to_recycle_bin, which only run() passes as well: no test puts anything in the PC's own Recycle
    Bin); without it, Delete answers that it is not available. `capture_lock` names the lock every recording holds
    until its analysis ends (capture.session.CAPTURE_LOCK_NAME, which only run() passes, so a test never meets the
    owner's own recording): Delete deletes nothing while another process holds it, and Apply and Undo change nothing.
    `fixes` applies and undoes the fixes Results offers (fixes/flow.Fixes: the catalogue's fixes with their real
    backends, the home's fix journal), which only run() passes as well: an App a test builds applies stand-ins, and
    without it Apply and Undo answer that they are not available."""

    def __init__(self, *, home: Path, runtime: dict, create_fn, log: Callable[[str], None],
                 tell: Callable[[str], None] = tell, activate: bool = True, on_ready: Callable[["App"], None] | None
                 = None, opener: Callable[[str], None] | None = None,
                 recorder: Callable[[], list[str]] | None = None, launcher: Callable[..., Any] | None = None,
                 play: Callable[[str], Any] | None = None, flash: Callable[[], Any] | None = None,
                 start_key: str | None = recording.START_KEY, installed: Callable[[], list] | None = None,
                 play_recorder: Callable[[str], list[str]] | None = None,
                 open_link: Callable[[str], Any] | None = None,
                 forms_of: Callable[[str], tuple[str, ...]] | None = None,
                 covers: Callable[[list], dict] | None = None,
                 picker: Callable[[int | None], str | None] | None = None,
                 trash: Callable[[str, int | None], Any] | None = None, capture_lock: str | None = None,
                 fixes: Any | None = None) -> None:
        self.home = Path(home)
        self.sessions = self.home / "sessions"
        self.sessions.mkdir(parents=True, exist_ok=True)
        self.generic_error = (f"Something went wrong reading this. The details are in lag-doctor's log, "
                              f"{self.home / LOG_NAME}.")
        self.runtime = runtime
        self.log = log
        self.tell = tell
        self.on_ready = on_ready
        self.opener = opener or open_local          # opens a report in the browser, or the sessions folder
        self.lock = threading.Lock()
        self.entries: list[library.Entry] = []
        self.loaded: tuple | None = None            # (session id, capture, diagnosis, main chain): the last one read
        self.readiness: dict | None = None
        self.covers = covers
        self.art: dict[str, tuple[str, bytes]] = {}     # a game id's cover, as covers() read it, or its program's icon
        self.icons: set[str] = set()                    # the game ids whose art is a program's icon
        self.art_tried: set[str] = set()                # the game ids whose cover was looked for (once a run)
        self.icons_tried: set[tuple[str, str]] = set()  # (game id, program) whose icon was looked for (once a run)
        self.art_bytes = 0
        self.art_lock = threading.Lock()                # one reading of covers at a time
        self.window = AppWindow(runtime=runtime, create_fn=create_fn, data_folder=self.home / "webview",
                                assets=ASSETS, icon=ICON, class_name=CLASS + instance_tag(self.home),
                                on_message=self.on_message, on_failed=self.on_failed, log=log, activate=activate,
                                art=self.art_for)
        self.start_key = start_key
        self.recorder = recorder or (lambda: recording.recorder_argv(start_key=self.start_key))
        self.play_recorder = play_recorder or (lambda folder: recording.recorder_argv(start_key=self.start_key,
                                                                                      folder=folder))
        self.installed = installed or (lambda: [])
        self.forms_of = forms_of                    # library.installed's (target.folder_forms when None)
        self.games: list[library.Installed] = []    # the store games, as the last listing read them
        self.added: list[library.AddedGame] = []    # the games added by hand the last listing showed
        self.added_lock = threading.Lock()          # one change to added-games.json at a time
        self.added_said: tuple[str, ...] = ()       # what reading it last found wrong, logged once
        self.picker = picker
        self.picking = False                        # Windows' Open dialog is up
        self.draft: Draft | None = None             # the program the player chose last, before it is added
        self.trash = trash
        self.capture_lock = capture_lock
        self.deleting = False                       # a delete is moving folders (Windows' question may be up)
        self.fixes = fixes
        self.fix_lock = threading.Lock()            # one change of a setting at a time: a plan, an apply, an undo, the list
        self.planned: Planned | None = None         # the change Apply showed last, before it is applied
        self.open_link = open_link or _open_store_link
        self.rec_link: str | None = None            # the store link of the game the running Play opens
        self.launcher = launcher
        self.play = play or sounds.Player(self.home, log).play
        self.window.flasher = flash or self.window.flash        # when the window wants the player's attention
        self.rec: recording.Recorder | None = None             # the recording this window started last
        self.rec_state: recording.RecordingState | None = None
        self.push_seq = 0                                       # numbers each view of it, newest highest
        self.window.on_shown = self.push_recording

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
        if isinstance(result, Pick):                # the Open dialog is modal to the window: on the UI thread
            self.pick(req)
            return
        if isinstance(result, Trash):               # Windows' warning, if any, is owned by the window: on the UI thread
            self.delete(req, result)
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

    def scan_games(self) -> list[library.Installed]:
        """The store games Play can start, read again: a game installed or removed since shows at the next listing."""
        try:
            games = library.installed(list(self.installed()), self.forms_of)
        except Exception as exc:  # noqa: BLE001 - the recorded games still list without the stores'
            self.log(f"listing the installed games: {''.join(traceback.format_exception(exc))}")
            games = []
        with self.lock:
            self.games = games
        self.read_art(games)
        return games

    def read_art(self, games: list[library.Installed]) -> None:
        """Look once a run for the cover of each store game not looked for yet (`covers`), keeping at most
        art.MAX_TOTAL_BYTES of them: on a worker thread, so the window's art host only ever looks them up."""
        from lagdoctor.stores import art
        if self.covers is None:
            return
        with self.art_lock:
            new = [g for g in games if g.game_id not in self.art_tried]
            if not new:
                return
            try:
                found = self.covers([g.game for g in new])
            except Exception as exc:  # noqa: BLE001 - the games keep their generated tiles
                self.log(f"reading the games' cover art: {''.join(traceback.format_exception(exc))}")
                found = {}
            for g in new:
                self.art_tried.add(g.game_id)
                image = found.get((g.game.store, g.game.id))
                if image is None or self.art_bytes + len(image[1]) > art.MAX_TOTAL_BYTES:
                    continue
                with self.lock:
                    self.art[g.game_id] = image
                self.art_bytes += len(image[1])

    def scan_added(self, games: list[library.Installed]) -> list[library.AddedGame]:
        """The games added by hand the Library lists (added-games.json, less those a store in `games` lists:
        library.listed_added), read again; what the file holds that could not be read is logged once."""
        with self.added_lock:                       # never while an add or a remove replaces the file
            kept, problems = added.load(self.home)
        if tuple(problems) != self.added_said:
            for problem in problems:
                self.log(f"the games added by hand: {problem}")
            self.added_said = tuple(problems)
        listed = library.listed_added(library.added_games(kept, self.forms_of), games)
        with self.lock:
            self.added = listed
        return listed

    def read_icons(self, programs: dict[str, list[str]]) -> None:
        """For each game whose tile holds no cover or icon yet, the icon of the first of its programs
        (views.icon_programs: an added game's, an Epic game's, its newest recording's) that has one, within the art's
        budget, as read_art keeps covers. Each program is looked at once a run (stores/exe.read), and only one that
        added.check_program takes: a recording's summary.json is as writable as any file of the player's."""
        from lagdoctor.capture import target
        from lagdoctor.stores import exe
        with self.art_lock:
            for gid, paths in programs.items():
                for path in paths:
                    with self.lock:
                        if gid in self.art:
                            break
                    key = (gid, target.fold_path(path))
                    if key in self.icons_tried:
                        continue
                    self.icons_tried.add(key)
                    try:
                        program, _folder = added.check_program(path)
                    except ValueError:
                        continue
                    found = exe.read(program)
                    if found.icon is not None:
                        self.keep_icon(gid, found.icon)

    def keep_icon(self, game_id: str, icon: tuple[str, bytes]) -> None:
        """Hold a program's icon for the art host, within art.MAX_TOTAL_BYTES (art_lock held)."""
        from lagdoctor.stores import art
        with self.lock:
            if game_id in self.art or self.art_bytes + len(icon[1]) > art.MAX_TOTAL_BYTES:
                return
            self.art[game_id] = icon
            self.icons.add(game_id)
        self.art_bytes += len(icon[1])

    def icon_ids(self) -> frozenset[str]:
        with self.lock:
            return frozenset(self.icons)

    def art_for(self, game_id: str) -> tuple[str, bytes] | None:
        """The cover the window's art host answers with (AppWindow.on_web_resource, on the window's thread)."""
        with self.lock:
            return self.art.get(game_id)

    def art_ids(self) -> frozenset[str]:
        with self.lock:
            return frozenset(self.art)

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
        games = self.scan_games()
        listed = self.scan_added(games)
        self.read_icons(views.icon_programs(entries, games, listed))
        return views.library_view(entries, self.readiness or self.get_readiness(), games, art=self.art_ids(),
                                  added=listed, icons=self.icon_ids())

    def do_game(self, game: str) -> dict:
        entries = self.scan()
        games = self.scan_games()
        listed = self.scan_added(games)
        self.read_icons(views.icon_programs(entries, games, listed))
        view = views.game_view(entries, game, library.last_session(self.home, self.sessions), games,
                               play=views.play_words(self.start_key, recording.WINDOW_S, LAG_KEY), art=self.art_ids(),
                               added=listed, icons=self.icon_ids())
        if view is None:
            raise Gone("That game is not in the Library as it was: open it again from the Library.")
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
            from lagdoctor.analysis import crop
            from lagdoctor.analysis.diagnose import diagnose
            self.log(f"{session}: its diagnosis.json could not be read; analysing the capture again")
            span, _ = crop.key_part(cap)              # the part its start key measured, as the watch analysed it
            d = diagnose(cap if span is None else crop.crop(cap, *span, how="key"))
        main = main_chain(cap, d.stats.get("main_chain") if isinstance(d.stats, dict) else None)   # the analysed one
        with self.lock:
            self.loaded = (session, cap, d, main)
        return entry, cap, d, main

    def do_results(self, session: str) -> tuple:
        entry, cap, d, main = self.load(session)
        is_last = library.last_session(self.home, self.sessions) == session
        view = views.results_view(entry, d, cap, main, is_last, (entry.folder / library.REPORT_NAME).is_file(),
                                  apply_ids=self.fixes.known() if self.fixes is not None else frozenset())
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
                                   __version__, LAG_KEY, self.start_key)

    def do_open_report(self, session: str) -> "Open":
        report = self.entry(session).folder / library.REPORT_NAME
        if not report.is_file():
            raise Gone("This recording has no report page.")
        return Open(report)

    def do_open_folder(self) -> "Open":
        return Open(self.sessions)

    def do_test_sound(self) -> dict:
        self.play("start")                          # the sound measuring starts with
        return {}

    # ---------------------------------------------------------------- recording (M4)

    def do_record(self) -> dict:
        """Start recording the game that is running: the recorder, a child of this window, does the rest."""
        return self._start(self.recorder)

    def do_play(self, game: str) -> dict:
        """Play (M6): record the installed store game the page names, from its start. The recorder asks for the relay
        for the game's folder, and once the prompt is answered and its checks have passed it says `launch`: only then
        does this window have Explorer open the game's store link (on_recorder), and the recorder waits for the game
        the store starts. lag-doctor never starts the game itself."""
        entries, games = self.scan(), self.scan_games()
        found = views.installed_of(entries, game, games, self.scan_added(games))
        if found is None:
            raise Gone("That game is not installed any more, so lag-doctor cannot start it.")
        play = {"game": game, "title": found.game.title, "store": found.store_name}
        return self._start(lambda: self.play_recorder(found.game.folder), play=play, link=found.game.link,
                           folder=found.game.folder)

    def do_wait(self, game: str) -> dict:
        """Record when it starts (screen 6): record a game the player starts themselves, from its start. The recorder
        waits on the game's folder (an added game's, or a recorded game's own) as Play's does, but no link is opened:
        on "launch", the running screen asks the player to start the game now."""
        from lagdoctor import stores
        entries, games = self.scan(), self.scan_games()
        found = views.wait_of(entries, game, games, self.scan_added(games))
        if found is None:
            raise Gone("lag-doctor knows no folder of that game to wait on: open it again from the Library.")
        name, folder = found
        try:
            there = stores.is_folder(folder)
        except OSError as exc:
            raise Gone(f"lag-doctor cannot wait on that game's folder: {stores.failure(exc, folder)}.") from None
        if not there:
            raise Gone(f"That game's folder, {folder}, is not there any more, so lag-doctor cannot wait for it.")
        play = {"game": game, "title": name, "store": "", "self": True}
        return self._start(lambda: self.play_recorder(folder), play=play, folder=folder)

    # ---------------------------------------------------------------- add a game (screen 6)

    def do_pick_game(self) -> "Pick":
        """Windows' Open dialog, for the program of a game to add: opened by answer(), on the UI thread."""
        return Pick()

    def pick(self, req: bridge.Request) -> None:
        """On the UI thread: Windows' Open dialog, modal to the window (its own message loop answers the window's
        posted calls meanwhile), then the program chosen, read on a worker (draft_of)."""
        if self.picker is None:
            self.window.post(bridge.reply(req.id, error="Adding a game is not available in this window."))
            return
        if self.picking:
            self.window.post(bridge.reply(req.id, error="Windows' Open dialog is open already."))
            return
        self.picking = True
        try:
            path = self.picker(self.window.hwnd)
        except Exception as exc:  # noqa: BLE001 - said on the page; the window goes on
            self.log(f"the Open dialog:\n{''.join(traceback.format_exception(exc))}")
            words = exc.strerror if isinstance(exc, OSError) and exc.strerror else "lag-doctor hit an error"
            self.window.post(bridge.reply(req.id, error=f"Windows' Open dialog could not open ({words})."))
            return
        finally:
            self.picking = False
        if not path:
            self.window.post(bridge.reply(req.id, {"cancelled": True}))
            return
        if not self.window.submit(lambda: self.draft_of(path), lambda result, error: self.answer(req, result, error)):
            self.window.post(bridge.reply(req.id, error="lag-doctor is still busy with earlier requests."))

    def draft_of(self, path: str) -> dict:
        """The program the player chose, as screen 6 shows it before it is added (views.draft_view): its name from
        its file (stores/exe.read), else its file's; or, when the Library has its game already, that game
        (views.clash_view). Gone when it cannot be a game's: no .exe, a folder the wait cannot take (a drive, the
        Windows folder, a Program Files folder itself, one off the local disks) or one holding another game's."""
        from lagdoctor import stores
        from lagdoctor.capture import target
        from lagdoctor.stores import exe
        try:
            program, folder = added.check_program(path)
        except ValueError as exc:
            raise Gone(f"lag-doctor cannot add that program: {exc}.") from None
        why = target.local_problem(program)
        if why is not None:
            raise Gone(f"lag-doctor adds only programs on this PC's own disks: {why}.")
        try:
            forms = tuple((self.forms_of or target.folder_forms)(folder))
        except (OSError, ValueError) as exc:
            raise Gone(f"lag-doctor cannot wait for a game in that folder: {exc}.") from None
        shared = {target.fold_path(r) for r in library.shared_roots()}
        if any(target.fold_path(f) in shared for f in forms):        # its real path too, behind a junction
            raise Gone(f"That program sits in {folder} itself, not in a folder of its own, so lag-doctor could not "
                       "tell it from the other programs there.")
        entries, games = self.scan(), self.scan_games()
        listed = self.scan_added(games)
        found = library.clash(program, forms, games, listed, views.recorded_games(entries, games, listed))
        if found is not None:
            view = views.clash_view(*found)
            if view["existing"] is None:
                raise Gone(view["why"])
            return view
        read = exe.read(program)
        for problem in read.problems:
            self.log(f"adding a game: {problem}")
        gid = library.added_id(program)
        with self.art_lock:
            self.icons_tried.add((gid, target.fold_path(program)))     # looked at now, not again at the next listing
            if read.icon is not None:
                self.keep_icon(gid, read.icon)
        token = "d-" + secrets.token_hex(8)
        with self.lock:
            self.draft = Draft(token=token, exe=program, folder=folder, forms=forms)
        return views.draft_view(token, read.name or stores.title(None, exe.stem(program)), program, folder, gid,
                                read.name is None, art=self.art_ids(), icons=self.icon_ids())

    def do_add_game(self, draft: str, name: str) -> dict:
        """Add the program the player chose (the draft `draft` names) by the name they gave it."""
        from lagdoctor import stores
        with self.lock:
            d = self.draft
        if d is None or d.token != draft:
            raise Gone("That program is no longer being added: choose it again with Add a game.")
        shown = stores.name(name)
        if shown is None:
            raise Gone("Give the game a name to show by.")
        entries, games = self.scan(), self.scan_games()
        recorded = views.recorded_games(entries, games, self.scan_added(games))
        with self.added_lock:
            kept, problems = added.load(self.home)
            found = library.clash(d.exe, d.forms, games,
                                  library.listed_added(library.added_games(kept, self.forms_of), games), recorded)
            if found is not None:
                raise Gone(views.clash_view(*found)["why"])
            if len(kept) >= added.MAX_GAMES:
                raise Gone(f"The Library holds at most {added.MAX_GAMES} games added by hand: remove one first.")
            try:
                added.save(self.home, kept + [added.Kept(exe=d.exe, name=shown)], unread=bool(problems))
            except OSError as exc:
                self.log(f"saving the games added by hand: {exc}")
                raise Gone(f"lag-doctor could not save its list of games ({exc.strerror or exc}).") from None
        with self.lock:
            if self.draft is d:
                self.draft = None
        self.log(f"added a game by hand: {d.exe}")
        return {"game": library.added_id(d.exe)}

    def do_remove_game(self, game: str) -> dict:
        """Take a game added by hand out of the Library: its recordings stay, under their recorded game."""
        with self.added_lock:
            kept, problems = added.load(self.home)
            rest = [k for k in kept if library.added_id(k.exe) != game]
            if len(rest) == len(kept):
                raise Gone("That game was not added by hand, so there is nothing to remove.")
            try:
                added.save(self.home, rest, unread=bool(problems))
            except OSError as exc:
                self.log(f"saving the games added by hand: {exc}")
                raise Gone(f"lag-doctor could not save its list of games ({exc.strerror or exc}).") from None
        with self.lock:
            self.added = [a for a in self.added if a.game_id != game]
        return {}

    def do_rename_game(self, game: str, name: str) -> dict:
        """Change the name a game added by hand shows by: its program, its id and its recordings stay as they were."""
        from lagdoctor import stores
        shown = stores.name(name)
        if shown is None:
            raise Gone("Give the game a name to show by.")
        with self.added_lock:
            kept, problems = added.load(self.home)
            at = next((i for i, k in enumerate(kept) if library.added_id(k.exe) == game), None)
            if at is None:
                raise Gone("That game was not added by hand, so it keeps the name its store or its recordings give it.")
            kept[at] = added.Kept(exe=kept[at].exe, name=shown)
            try:
                added.save(self.home, kept, unread=bool(problems))
            except OSError as exc:
                self.log(f"saving the games added by hand: {exc}")
                raise Gone(f"lag-doctor could not save its list of games ({exc.strerror or exc}).") from None
        return {"game": game, "name": shown}

    # ---------------------------------------------------------------- Apply and Undo (fixes)

    def do_plan_fix(self, session: str, finding: int, fix: int) -> dict:
        """What Apply would change, for the fix the page names by its place on a recording's Results (finding, fix):
        the plan `lagdoctor fix` prints, read now and kept here under a token the page applies by (do_apply_fix). The
        fix and its params are this app's own reading of the recording's diagnosis, never the page's, and only the last
        recording made offers them: an older one may describe this PC as it was then. Never while a recording runs, or
        in a window that runs as administrator (change_refusal); an unreadable journal pauses it, as it pauses `fix`."""
        from lagdoctor.fixes import flow
        from lagdoctor.fixes.base import is_gone
        fixes = self.fixes_here()
        _entry, _cap, d, _main = self.load(session)
        if library.last_session(self.home, self.sessions) != session:
            raise Gone(views.NOT_LAST)
        found = None
        if finding < len(d.findings) and fix < len(d.findings[finding].fixes):
            found = d.findings[finding].fixes[fix]
        if found is None or not flow.offerable(found.fix_id, found.params, fixes.known()):
            raise Gone("That is not a fix lag-doctor can apply from this recording: open its results again.")
        why = self.change_refusal(fixes)
        if why is not None:
            raise Gone(why)
        if not self.fix_lock.acquire(blocking=False):
            raise Gone(BUSY)
        try:
            self.journal_ready(fixes.journal())
            try:
                plan = fixes.fix(found.fix_id).plan(dict(found.params))
            except (*flow.PLAN_ERRORS, KeyError) as exc:
                self.log(f"planning {found.fix_id}: {exc!r}")
                raise Gone(views.refused_text(exc)) from None
        finally:
            self.fix_lock.release()
        if plan.noop or is_gone(plan.before):
            return views.plan_view(None, plan)
        token = "p-" + secrets.token_hex(8)
        with self.lock:
            self.planned = Planned(token=token, session=session, fix_id=found.fix_id, plan=plan)
        return views.plan_view(token, plan)

    def do_apply_fix(self, plan: str) -> dict:
        """Make the change Apply showed (do_plan_fix's token), exactly as planned: when the setting moved since, the
        fix itself changes nothing (fixes/base.apply's "stale") and the page plans again. Each token applies once."""
        from lagdoctor.fixes import flow
        from lagdoctor.fixes.journal import JournalCorrupt
        fixes = self.fixes_here()
        with self.lock:
            planned = self.planned
        if planned is None or planned.token != plan:
            raise Gone(NO_LONGER_PLANNED)
        if library.last_session(self.home, self.sessions) != planned.session:
            raise Gone(views.NOT_LAST)
        why = self.change_refusal(fixes)
        if why is not None:
            raise Gone(why)
        if not self.fix_lock.acquire(blocking=False):
            raise Gone(BUSY)
        try:
            with self.lock:
                if self.planned is not planned:     # applied, or replaced by a newer plan, meanwhile
                    raise Gone(NO_LONGER_PLANNED)
                self.planned = None
            try:
                res = flow.apply(fixes.fix(planned.fix_id), planned.plan, fixes.journal())
            except JournalCorrupt as exc:
                raise Gone(exc.plain()) from None
        finally:
            self.fix_lock.release()
        self.log(f"apply {planned.fix_id}: {res.state}: {res.message}")
        return views.applied_view(res, planned.plan)

    def do_changes(self) -> dict:
        """The changes lag-doctor made that are still in place, newest first (the list `lagdoctor undo` prints), once
        the journal is settled as every start settles it (fixes/flow.settle: a change a crash left half-done, a
        session-only one whose program has exited); what settling says goes to the log."""
        from lagdoctor.fixes import flow
        from lagdoctor.fixes.journal import JournalCorrupt
        if self.fixes is None:
            return views.changes_view([], None, "Undoing changes is not available in this window.")
        fixes = self.fixes
        if not self.fix_lock.acquire(timeout=LIST_WAIT_S):
            raise Gone("lag-doctor is still changing a setting: open Settings again in a moment.")
        try:
            journal = fixes.journal()
            flow.settle(lambda line: self.log(f"the fix journal: {line}"), journal=journal, fix_for=fixes.fix)
            try:
                entries = journal.outstanding()
            except JournalCorrupt as exc:
                return views.changes_view([], None, exc.plain())
            except OSError as exc:
                self.log(f"listing the changes: {exc}")
                return views.changes_view([], None, "lag-doctor could not open its fix journal (another lag-doctor "
                                                    "window may be using it). Open Settings again in a moment.")
            return views.changes_view(entries, lambda e: self.about_change(fixes, e))
        finally:
            self.fix_lock.release()

    def do_undo_change(self, entry: str, force: bool) -> dict:
        """Undo one change the journal holds (its id, from the list or from Apply's answer), as `lagdoctor undo` does: a
        value someone changed since is left alone unless `force` (the page asks for that only after saying so). A
        settings sweep's change is the sweep's own, put back in a terminal; never while a recording runs, or in a window
        that runs as administrator (change_refusal)."""
        from lagdoctor.fixes import flow
        from lagdoctor.fixes.journal import JournalCorrupt
        fixes = self.fixes_here()
        why = self.change_refusal(fixes)
        if why is not None:
            raise Gone(why)
        if not self.fix_lock.acquire(blocking=False):
            raise Gone(BUSY)
        try:
            journal = fixes.journal()
            try:
                found = next((e for e in journal.outstanding() if e.id == entry), None)
            except JournalCorrupt as exc:
                raise Gone(exc.plain()) from None
            except OSError as exc:
                self.log(f"reading the fix journal: {exc}")
                raise Gone(NO_JOURNAL) from None
            if found is None:
                raise Gone("That change is not on the list any more: it was undone already.")
            if found.fix_id in flow.NEVER_OFFERED:
                raise Gone(views.SWEEP_CHANGE)
            try:
                res = flow.undo(found, fixes.fix, journal, force=force)
            except JournalCorrupt as exc:
                raise Gone(exc.plain()) from None
            change = self.about_change(fixes, found)[1] if res.ok else ""
        finally:
            self.fix_lock.release()
        self.log(f"undo {found.fix_id} ({found.id}): {res.why or res.state}: {res.message}")
        return views.undone_view(res, change)

    def fixes_here(self) -> Any:
        if self.fixes is None:
            raise Gone("Applying and undoing fixes is not available in this window.")
        return self.fixes

    def change_refusal(self, fixes: Any) -> str | None:
        """Why Apply and Undo change nothing now, in plain words, or None. Never while a recording runs, this window's
        own or any other in this Windows session (the capture lock, other_recording): a change in its middle would mix
        two conditions into one recording, and a settings sweep changes settings itself. Never in a window that runs as
        administrator, or cannot tell: a fix changes the settings of whoever the process runs as, and `lagdoctor fix`
        refuses there too."""
        with self.lock:
            if self.rec_state is not None and self.rec_state.active:
                return "A recording is running: apply or undo fixes once it has ended."
        why = self.other_recording("apply or undo fixes", "changed nothing", "changing a setting")
        if why is not None:
            return why
        elevated = fixes.elevated()
        if elevated is None:
            return "lag-doctor could not tell whether it runs as administrator, so it changed nothing."
        if elevated:
            return ("lag-doctor is running as administrator, so it changed nothing: fixes change your own user's "
                    "settings. Start lag-doctor normally to apply or undo them.")
        return None

    def journal_ready(self, journal: Any) -> None:
        """Gone, in plain words, unless the fix journal can be read: an unreadable one pauses every change (its bytes
        copied aside, as for `lagdoctor fix`), and one another lag-doctor holds, or a disk refuses, changes nothing."""
        from lagdoctor.fixes.journal import JournalCorrupt
        try:
            journal.entries()
        except JournalCorrupt as exc:
            raise Gone(exc.plain()) from None
        except OSError as exc:
            self.log(f"reading the fix journal: {exc}")
            raise Gone(NO_JOURNAL) from None

    def about_change(self, fixes: Any, e: Any) -> tuple[str | None, str]:
        """A journaled change's fix title (None: a fix this lag-doctor does not know) and the change in the fix's own
        words (its describe, from the original to lag-doctor's value; '' when it cannot say)."""
        title = fixes.title(e.fix_id) if isinstance(e.fix_id, str) else None
        if title is None:
            return None, ""
        try:
            return title, str(fixes.fix(e.fix_id).describe(e.before, e.after))
        except Exception as exc:  # noqa: BLE001 - the change is listed by its title and place alone
            self.log(f"describing the change {e.id!r}: {exc!r}")
            return title, ""

    # ---------------------------------------------------------------- deleting recordings

    def do_delete_recordings(self, game: str, sessions: list[str]) -> "Trash":
        """The recordings to delete, each one listed under the game the page names (never a folder the page names):
        answer() moves them to the Recycle Bin on the UI thread (delete)."""
        entries, games = self.scan(), self.scan_games()
        mine = {e.id: e for e in views.game_entries(entries, game, games, self.scan_added(games))}
        if any(s not in mine for s in sessions):
            raise Gone("Some of those recordings are no longer listed under this game: open its page again.")
        return Trash(game=game, entries=tuple(mine[s] for s in sessions))

    def delete(self, req: bridge.Request, trash: "Trash") -> None:
        """On the UI thread: each recording's folder to the Recycle Bin (`trash`), any warning of Windows' owned by the
        window, and each one checked gone; then what the page says, read on a worker (deleted). Only a folder on this
        PC's own disks is handed over, by its full path (a share keeps no Recycle Bin of this PC's, and a path that is
        not a full one is deleted for good). Never while a recording runs, which may be analysing into a folder the
        listing shows: this window's own (rec_state), or another one's, a terminal's `watch` or a recorder this window
        left running when it closed (the capture lock, which each holds until its analysis ends). Both are looked at
        here, right before the first move: a recording that starts after it makes a folder of its own. A move the
        player cancelled in Windows' question ends the delete: the rest would only ask again. One delete at a time:
        Windows' question runs a message loop, which answers the page's other requests meanwhile."""
        if self.trash is None:
            self.window.post(bridge.reply(req.id, error="Deleting recordings is not available in this window."))
            return
        with self.lock:
            if self.rec_state is not None and self.rec_state.active:
                self.window.post(bridge.reply(req.id, error="A recording is running: delete recordings once it has "
                                                            "ended."))
                return
        why_not = self.other_recording()
        if why_not is not None:
            self.window.post(bridge.reply(req.id, error=why_not))
            return
        if self.deleting:                           # Windows' question runs a message loop that answers the page too
            self.window.post(bridge.reply(req.id, error="lag-doctor is still moving recordings to the Recycle Bin."))
            return
        self.deleting = True
        try:
            done, failed = self.move(trash.entries)
        finally:
            self.deleting = False
        gone = {e.id for e in done}
        with self.lock:
            if self.loaded is not None and self.loaded[0] in gone:
                self.loaded = None
        if not self.window.submit(lambda: self.deleted(trash.game, len(done), failed),
                                  lambda result, error: self.answer(req, result, error)):
            self.window.post(bridge.reply(req.id, views.deleted_view(len(done), failed, True)))

    def move(self, entries: tuple) -> tuple[list, list]:
        """delete's moves, one recording at a time: (those moved, [(those not, why)])."""
        from lagdoctor.capture import target
        from lagdoctor.winapi import recycle
        done, failed = [], []
        for i, e in enumerate(entries):
            folder = os.path.abspath(e.folder)
            why = target.local_problem(folder)
            if why is not None:
                failed.append((e, f"lag-doctor deletes only recordings on this PC's own disks ({why})"))
                continue
            try:
                self.trash(folder, self.window.hwnd)
            except recycle.Cancelled:
                failed.append((e, "it was cancelled"))
                failed += [(x, "it was not tried, since the one before it was cancelled") for x in entries[i + 1:]]
                break
            except (OSError, ValueError) as exc:
                self.log(f"moving {e.id} to the Recycle Bin: {exc}")
                failed.append((e, (exc.strerror if isinstance(exc, OSError) else None) or str(exc)))
                continue
            except Exception as exc:  # noqa: BLE001 - said, the page answered, and the other recordings still go
                self.log(f"moving {e.id} to the Recycle Bin:\n{''.join(traceback.format_exception(exc))}")
                failed.append((e, "lag-doctor hit an error moving it; the details are in its log"))
                continue
            if os.path.lexists(folder):
                self.log(f"moving {e.id} to the Recycle Bin: its folder is still there")
                failed.append((e, "its folder is still there"))
                continue
            self.log(f"moved a recording to the Recycle Bin: {e.id}")
            done.append(e)
        return done, failed

    def other_recording(self, then: str = "delete recordings", nothing: str = "deleted nothing",
                        doing: str = "deleting") -> str | None:
        """Why Delete (or Apply and Undo: `then`, `nothing`, `doing` word it) must wait for another process's recording,
        in plain words, or None: the capture lock, held by whatever records in this Windows session, looked at and let
        go at once (`capture_lock`; None: not looked at)."""
        from lagdoctor.capture import session as sess
        if self.capture_lock is None:
            return None
        lock = sess.CaptureLock(self.capture_lock)
        try:
            free = lock.acquire()
        except OSError as exc:
            self.log(f"looking for a recording before {doing}: {exc}")
            return f"lag-doctor could not tell whether a recording is running, so it {nothing}."
        finally:
            lock.release()
        return None if free else ("Another lag-doctor recording is running, in a terminal or from an earlier window: "
                                  f"{then} once it has ended.")

    def deleted(self, game: str, done: int, failed: list) -> dict:
        """What the page says once recordings were deleted, and whether their game is still in the Library (said as
        listed when the listing cannot be read again: the page then shows the game's page, and says why not)."""
        try:
            entries, games = self.scan(), self.scan_games()
            listed = views.game_view(entries, game, None, games, added=self.scan_added(games)) is not None
        except Exception as exc:  # noqa: BLE001 - the folders are gone: the page must hear so
            self.log(f"listing the games after a delete:\n{''.join(traceback.format_exception(exc))}")
            listed = True
        return views.deleted_view(done, failed, listed)

    def _start(self, argv: Callable[[], list[str]], *, play: dict | None = None, link: str | None = None,
               folder: str | None = None) -> dict:
        with self.lock:
            if self.rec_state is not None and self.rec_state.active:
                raise Gone("A recording is already running.")
            if self.fix_lock.locked():              # what it records first (the PC's settings) is being changed
                raise Gone("lag-doctor is changing a setting: record once it is done.")
            rec = recording.Recorder(home=self.home, log=self.log, hwnd=lambda: self.window.hwnd,
                                     argv=argv(), launcher=self.launcher, folder=folder,
                                     post=lambda msg: self.window.call_soon(lambda: self.on_recorder(rec, msg)))
            self.rec, self.rec_link = rec, link
            self.rec_state = recording.RecordingState(keyed=self.start_key is not None, play=play)
        try:
            rec.start()
        except BaseException as exc:
            with self.lock:                         # nothing started: the next Record may try again
                if self.rec is rec:
                    self.rec, self.rec_state, self.rec_link = None, None, None
            if isinstance(exc, OSError):
                self.log(f"starting the recorder: {exc}")
                raise Gone(f"The recording could not start ({exc.strerror or exc}).") from None
            raise
        self.log(f"recording: the recorder started, pid {rec.proc.pid}" + (" (Play)" if play else ""))
        return self.recording_view()

    def _running(self) -> recording.Recorder:
        with self.lock:
            if self.rec is None or self.rec_state is None or not self.rec_state.active:
                raise Gone("No recording is running.")
            return self.rec

    def do_stop_recording(self) -> dict:
        if not self._running().stop():
            raise Gone("The recorder can no longer be reached. The recording stops by itself.")
        return self.recording_view()

    def do_cancel_recording(self) -> dict:
        if not self._running().cancel():
            raise Gone("The recorder can no longer be reached. The recording stops by itself.")
        return self.recording_view()

    def do_recording(self) -> dict:
        return self.recording_view()

    def recording_view(self) -> dict:
        with self.lock:
            self.push_seq += 1
            return dict(views.recording_view(self.rec_state, time.monotonic(), LAG_KEY,
                                             self.home / recording.LOG_NAME, self.start_key),
                        seq=self.push_seq)

    def push_recording(self) -> None:
        """Tell the page where the recording has got to; never while its view is hidden, which a message might
        wake: the window pushes the newest when the view comes back (on_shown)."""
        if self.window.view_hidden:
            return
        view = self.recording_view()
        if view["phase"] is not None:
            self.window.post(bridge.push("recording", view))

    def on_recorder(self, rec: recording.Recorder, msg: dict) -> None:
        """A message about the recording, on the UI thread: the state moves on, the view hides while the game
        records (unless the window is shown), the sounds play and the page is told. When the relay's prompt is raised
        or the recording ends while another window is in front (the game), a sound says so and the taskbar button
        flashes once Windows says it is a moment for that (AppWindow.want_attention)."""
        with self.lock:
            state = self.rec_state
            if rec is not self.rec or state is None or not state.active:
                return                              # an older recording's, or after the end
            # Play's store link opens once, for a recording the window started for Play, and not once it was stopped
            link = self.rec_link if (msg["t"] == "launch" and state.play is not None and not state.launch_asked
                                     and not rec.withdrawn) else None
            # the first answer about the link (opened, not, or not in time), for a recording not stopped since
            opened = (msg["t"] == "launched" and msg["ok"] and state.launch_result is None
                      and not getattr(rec, "withdrawn", False))
            sounds_called = state.apply(msg, time.monotonic())
            phase, ended = state.phase, not state.active
        if msg["t"] == "say":
            self.log(f"recorder: {msg['text']}")
        if link is not None:
            self._launch(rec, link)
        elif opened:
            self.window.minimize()                  # out of the way of the store and the game (Play's step 3)
        self.window.set_recording(phase == "recording")
        if msg["t"] == "elevate" and not self.window.in_front():
            sounds_called.append("ask")             # the prompt may only blink on the taskbar behind the game
        for name in sounds_called:
            self.play(name)
        self.push_recording()
        if ended:
            self.log(f"recording: {state.reason}, code {state.code}, session {state.session}")
        if msg["t"] == "prompt":
            self.window.drop_attention()            # the prompt it pointed at has been answered
        if (ended or msg["t"] == "elevate") and not self.window.in_front():
            self.window.want_attention()


    def _launch(self, rec: recording.Recorder, link: str) -> None:
        """Play's step 3: have Explorer open the game's store link, on a thread of its own (it calls into Explorer,
        which can be slow, and the window's thread never waits). How it went comes back as the app's own "launched":
        when Windows could not open it, the recorder goes on waiting, and the running screen asks the player to start
        the game themselves."""
        def should_go() -> bool:                       # asked right before Explorer is handed the link
            with self.lock:
                return self.rec is rec and not rec.withdrawn

        def run() -> None:
            from lagdoctor.winapi.launch import Withdrawn
            try:
                self.open_link(link, should_go)
                result = {"t": "launched", "ok": True, "why": None}
            except Withdrawn:                         # stopped meanwhile: nothing was opened, and nothing to say
                return
            except (OSError, ValueError) as exc:      # PermissionError (an elevated window) is an OSError
                self.log(f"opening the store link: {exc}")
                result = {"t": "launched", "ok": False, "why": str(exc) or type(exc).__name__}
            except Exception as exc:  # noqa: BLE001 - logged; the player is asked to start the game themselves
                self.log(f"opening the store link:\n{''.join(traceback.format_exception(exc))}")
                result = {"t": "launched", "ok": False, "why": "lag-doctor hit an error opening it"}
            self.window.call_soon(lambda: self.on_recorder(rec, result))
        threading.Thread(target=run, name="lagdoctor-app-launch", daemon=True).start()
        # A hung Explorer is said after LAUNCH_SLOW_S, as a link that was not opened; the first answer is the one kept.
        self.window.after(int(LAUNCH_SLOW_S * 1000), lambda: self.on_recorder(rec, {
            "t": "launched", "ok": False, "why": f"Windows has not opened it within {LAUNCH_SLOW_S:.0f} seconds"}))


def _open_store_link(link: str, should_go: Callable[[], bool] | None = None) -> Any:
    """Explorer opens the store link (stores/launch.play: only a link its builders made, never when elevated), so the
    store, and the game it starts, are Explorer's children, never this window's; `should_go` is asked right before."""
    from lagdoctor.stores import launch
    return launch.play(link, should_go)


class Gone(Exception):
    """A request about something the app cannot show, said in plain words to the page."""


@dataclass(frozen=True)
class Open:
    """A file or folder a request found to open: answer() opens it on the UI thread."""
    path: Path


@dataclass(frozen=True)
class Pick:
    """Add a game's Open dialog, which answer() opens on the UI thread."""


@dataclass(frozen=True)
class Trash:
    """The recordings a request found to delete, under the game the page named: answer() moves them to the Recycle Bin
    on the UI thread."""
    game: str
    entries: tuple


@dataclass(frozen=True)
class Planned:
    """The change Apply showed, before it is applied: `token` is the page's name for it (never the fix or its params),
    `session` the recording that offered it, `plan` the fix's own (fixes/base.FixPlan, read when it was shown)."""
    token: str
    session: str
    fix_id: str
    plan: Any


@dataclass(frozen=True)
class Draft:
    """The program the player chose in Add a game, before it is added: `token` is the page's name for this choice
    (never a path), `exe` and `folder` as added.check_program gave them, `forms` the folder's."""
    token: str
    exe: str
    folder: str
    forms: tuple[str, ...]


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
        from lagdoctor.capture import session as sess
        from lagdoctor.fixes import flow
        from lagdoctor.stores import art
        from lagdoctor.winapi import dialogs, recycle
        app = App(home=home, runtime=runtime, create_fn=create_fn, log=log,
                  installed=lambda: library.installed_games(log), covers=art.covers, picker=dialogs.pick_program,
                  trash=recycle.to_recycle_bin, capture_lock=sess.CAPTURE_LOCK_NAME, fixes=flow.Fixes(home))
        try:
            app.run()
        finally:
            app.window.destroy_icons()
            wv.CoUninitialize()
            if app.rec_state is not None and app.rec_state.active:
                log("closed during a recording: the recorder goes on, then saves and analyses it"
                    if app.rec_state.started is not None else
                    "closed before the recording began: the recorder cancels it, and nothing is recorded")
            log("closed" + (f" after a fault: {app.window.fatal}" if app.window.fatal else ""))
        if app.window.fatal:
            tell("lag-doctor's window hit an internal error and closed. The details are in app.log in lag-doctor's "
                 "folder (%LOCALAPPDATA%\\lag-doctor).")
            return 1
        return 0
    finally:
        if mutex:
            elevation.release_named_mutex(mutex)
