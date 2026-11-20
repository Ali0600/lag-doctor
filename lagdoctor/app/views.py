"""What each screen shows (docs/LAUNCHER-PLAN.md, screens 1 to 7), built from sessions on disk.

Each view is a plain dict of text and numbers, ready for JSON: the page only lays it out. Text from a capture or the
PC (a game's window title, a process name, evidence) stays text here, and the page puts it on screen with
textContent, never as markup (tests/test_app_page.py). Numbers a damaged file holds read as missing (charts.number).
Pure functions apart from library_view's and game_view's reads of small files; the window calls them from worker
threads.
"""
from __future__ import annotations

import array
import math
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from lagdoctor.analysis import crop
from lagdoctor.analysis.window import window as cut_window
from lagdoctor.app import bridge
from lagdoctor.app.library import (ADDED_BY_YOU, STORE_NAMES, AddedGame, Entry, Installed, RecordedGame, joined,
                                   read_diagnosis, shared_roots, wait_folders)
from lagdoctor.app.library import _broad
from lagdoctor.stores.exe import file_name as exe_file_name
from lagdoctor.capture import session as sess
from lagdoctor.model import C, Capture, Diagnosis, G, N, S, clean_note
from lagdoctor.report import html, labels
from lagdoctor.report.charts import fmt_elapsed, number
from lagdoctor.report.summary import CHECKED, NOT_UNREAL, headroom_fields

MAX_EVENTS = 1000                     # lag moments listed on Results (worst first are never cut: see results_view)
MAX_FRAMES = 4_000_000                # frames handed to the page's chart (64 MB of doubles)
MOMENT_PAD_S = 5.0                    # seconds shown either side of a lag moment
MOMENT_MAX_S = 120.0                  # longest span a moment shows
TOP_PROGRAMS = 5
RECENT = 6

NO_FIX_HERE = ("Fix commands appear on your last recording only: `lagdoctor fix` takes its offers from that one and "
               "the last check-up, so on this one it would act on another recording's findings.")

# Charts on Results besides frame times: (id, title, unit, [columns], source the columns come from).
CHARTS = (
    ("cpu_gpu", "CPU and GPU load", "%", (S.GAME_GPU_PCT, S.GAME_TOP_THREAD_PCT, S.CPU_TOTAL_PCT, S.GPU_OTHER_PCT),
     "system"),
    ("nv_clock", "NVIDIA GPU clock", "MHz", (G.CLOCK_MHZ, G.CLOCK_MAX_MHZ), "gpu"),
    ("nv_temp", "NVIDIA GPU temperature", "°C", (G.TEMP_C,), "gpu"),
    ("nv_slow", "NVIDIA GPU held back", "%", (G.THROTTLE_PCT, G.POWER_CAP_PCT), "gpu"),
    ("fans", "Fan speeds", "RPM", (C.CPU_FAN_RPM, C.GPU_FAN_RPM, C.MID_FAN_RPM), "fans"),
    ("ping", "Ping", "ms", (N.GW_RTT_MS, N.INET_RTT_MS, N.SERVER_RTT_MS), "network"),
)
# Around a lag moment: what moved, by source.
MOMENT_CHARTS = (
    ("cpu", "CPU", "%", (S.CPU_TOTAL_PCT, S.CPU_MAX_CORE_PCT, S.GAME_TOP_THREAD_PCT), "system"),
    ("gpu", "GPU", "%", (S.GAME_GPU_PCT, S.GPU_OTHER_PCT), "system"),
    ("disk", "Disk", "%", (S.DISK_BUSY_PCT,), "system"),
    ("ping", "Network", "ms", (N.GW_RTT_MS, N.INET_RTT_MS, N.SERVER_RTT_MS), "network"),
)


# ------------------------------------------------------------------------------------------------- helpers --

def when_text(dt: datetime | None) -> str:
    """'8 Nov 2026, 14:32': a session's start, in its own time zone."""
    if dt is None:
        return "-"
    return f"{dt.day} {dt:%b %Y, %H:%M}"


def fmt(v: Any, pattern: str = "{:.0f}", suffix: str = "") -> str:
    n = number(v)
    return "-" if n is None else pattern.format(n) + suffix


def _dict(v: Any) -> dict:
    return v if isinstance(v, dict) else {}


def _list(v: Any) -> list:
    return v if isinstance(v, list) else []


def lag_text(key: Any) -> str:
    return labels.LAG_TYPES.get(key, "Not analysed") if isinstance(key, str) else "Not analysed"


def tile(name: str, game_id: str | None = None, art: frozenset | set = frozenset(),
         icons: frozenset | set = frozenset()) -> dict[str, Any]:
    """A cover tile: the game's initials on a colour from its name, and with "art" where its picture is (the art
    host's URL for the game, when the app holds a cover from its store's cache or the icon of an added game's program:
    `art`, the ids it holds), which the page shows over the initials once it has loaded: a cover fills the tile, an
    icon (`icons`, the ids whose picture is one) sits in its middle on the colour. No art is downloaded."""
    words = [w for w in "".join(ch if ch.isalnum() else " " for ch in name).split() if w]
    initials = "".join(w[0] for w in words[:2]).upper() or "?"
    hue = sum(ord(ch) * (i + 1) for i, ch in enumerate(name)) % 360
    shown = game_id is not None and game_id in art
    return {"initials": initials, "hue": hue, "art": bridge.art_url(game_id) if shown else None,
            "icon": shown and game_id in icons}


def numbers(entry: Entry) -> dict[str, Any]:
    """A session's headline numbers: from its summary, else (an older session) from its diagnosis.json. The bound and
    headroom keys (summary.headroom_fields) are None in a summary from before them."""
    s = entry.summary
    if s is not None:
        out = {k: s.get(k) for k in ("lag_type", "headline", "avg_fps", "low1_fps", "stutters_per_min",
                                     "worst_frame_ms", "top_cause", "duration_s", "frames_source", "refresh_hz",
                                     "power_plan", "power_mode", "frames_measured")}
        out.update(headroom_fields(s))
        return out
    d = read_diagnosis(entry) or {}
    stats = _dict(d.get("stats"))
    findings = _list(d.get("findings"))
    top = findings[0].get("cause") if findings and isinstance(findings[0], dict) else None
    return {"lag_type": d.get("lag_type") if isinstance(d.get("lag_type"), str) else None,
            "headline": d.get("headline") if isinstance(d.get("headline"), str) else None,
            "avg_fps": number(stats.get("avg_fps")), "low1_fps": number(stats.get("low1_fps")),
            "stutters_per_min": number(stats.get("stutters_per_min")),
            "worst_frame_ms": number(stats.get("worst_frame_ms")), "top_cause": top if isinstance(top, str) else None,
            "duration_s": number(stats.get("duration_s")),
            "frames_source": stats.get("frames_source") if isinstance(stats.get("frames_source"), str) else None,
            "refresh_hz": number(stats.get("refresh_hz")), "power_plan": None, "power_mode": None,
            "frames_measured": None, **headroom_fields(stats)}


def _session_row(entry: Entry, last_id: str | None) -> dict[str, Any]:
    n = numbers(entry)
    return {"id": entry.id, "when": when_text(entry.started), "length": fmt_elapsed(n["duration_s"]),
            "lag_type": lag_text(n["lag_type"]), "lag_key": n["lag_type"] or "unknown",
            "avg_fps": fmt(n["avg_fps"]), "low1_fps": fmt(n["low1_fps"]),
            "stutters_per_min": fmt(n["stutters_per_min"], "{:.1f}"),
            "top_cause": labels.cause_text(n["top_cause"]) if n["top_cause"] else "nothing stood out",
            "note": (entry.summary or {}).get("note") or "", "is_last": entry.id == last_id}


# ------------------------------------------------------------------------------------------------ library --

@dataclass
class _Game:
    """One tile of the Library: an installed store game (`installed`), a game added by hand (`added`), or a recorded
    game neither lists, each with its recordings, newest first. `folder`: the folder Record when it starts waits on
    (an added game's, or a recorded game's from library.wait_folders); None when there is none."""
    id: str
    name: str
    installed: Installed | None = None
    added: AddedGame | None = None
    recs: list[Entry] = field(default_factory=list)
    folder: str | None = None
    broad: bool = False         # an added game whose folder holds another listed game's, or is a shared root

    @property
    def key(self) -> str:
        holder = self.installed or self.added
        return holder.key if holder is not None else self.id

    @property
    def store(self) -> str:
        if self.installed is not None:
            return self.installed.store_name
        return ADDED_BY_YOU if self.added is not None else ""


def _newest_exe(recs: list[Entry]) -> str | None:
    """The program of the newest recording that names one (summary.json's exe)."""
    for e in recs:
        exe = (e.summary or {}).get("exe")
        if isinstance(exe, str):
            return exe
    return None


def _games(entries: list[Entry], games: list[Installed] | tuple = (), added: list[AddedGame] | tuple = (),
           roots: list[str] | None = None) -> list[_Game]:
    """Every game the Library shows: those with recordings first, by their newest recording, then the installed and
    added ones not recorded yet, by name. A recording that ran from an installed or added game's folder shows under
    that game (library.joined); the others under their recorded game (their exe's stem), which waits on its own
    folder when it has one (library.wait_folders). `added`: the added games the Library lists (library.listed_added)."""
    joins = joined(entries, list(games), roots, added=list(added))
    broad = _broad(list(games) + list(added), shared_roots() if roots is None else roots)
    out: dict[str, _Game] = {}
    for e in entries:                                         # entries come newest first, and so do each game's
        if not e.is_recording:
            continue
        g = joins.get(e.id)
        if isinstance(g, AddedGame):
            tile = out.setdefault(g.game_id, _Game(id=g.game_id, name=g.name, added=g, folder=g.folder))
        elif g is not None:
            tile = out.setdefault(g.game_id, _Game(id=g.game_id, name=g.game.title, installed=g))
        else:
            tile = out.setdefault(e.game_id, _Game(id=e.game_id, name=e.game))
        tile.recs.append(e)
    recorded = {}
    for tile in out.values():
        exe = _newest_exe(tile.recs) if tile.installed is None and tile.added is None else None
        if exe is not None:
            recorded[tile.id] = exe
    for gid, folder in wait_folders(recorded, list(games) + list(added), roots).items():
        out[gid].folder = folder
    rest = [_Game(id=g.game_id, name=g.game.title, installed=g) for g in games if g.game_id not in out]
    rest += [_Game(id=a.game_id, name=a.name, added=a, folder=a.folder) for a in added if a.game_id not in out]
    rest.sort(key=lambda g: (g.name.casefold(), g.key))
    for tile in list(out.values()) + rest:
        if tile.added is not None and tile.added.key in broad:    # the wait could not tell its game from another
            tile.folder, tile.broad = None, True
    return list(out.values()) + rest


def icon_programs(entries: list[Entry], games: list[Installed] | tuple = (),
                  added: list[AddedGame] | tuple = ()) -> dict[str, list[str]]:
    """{game id: the programs its tile's icon may come from, the first that has one}: an added game's own program, an
    installed game's as its store names it (Epic's), then the program of its newest recording that names one. The app
    reads them only for a game whose tile holds no cover (App.read_icons)."""
    from lagdoctor.capture import target
    out: dict[str, list[str]] = {}
    for g in _games(entries, games, added):
        own = g.added.exe if g.added is not None else getattr(g.installed.game, "exe", None) if g.installed else None
        found = []
        for exe in (own, _newest_exe(g.recs)):
            if isinstance(exe, str) and all(target.fold_path(exe) != target.fold_path(f) for f in found):
                found.append(exe)
        if found:
            out[g.id] = found
    return out


def recorded_games(entries: list[Entry], games: list[Installed] | tuple = (),
                   added: list[AddedGame] | tuple = ()) -> list[RecordedGame]:
    """The recorded games no store lists and nobody added, each with the folder of its newest recording's program
    (when it names one that added.check_program takes), for Add a game's clash."""
    from lagdoctor.app import added as added_mod
    out = []
    for g in _games(entries, games, added):
        exe = _newest_exe(g.recs) if g.installed is None and g.added is None else None
        if exe is None:
            continue
        try:
            _exe, folder = added_mod.check_program(exe)
        except ValueError:
            continue
        out.append(RecordedGame(game_id=g.id, name=g.name, folder=folder))
    return out


def library_view(entries: list[Entry], readiness: dict[str, Any], games: list[Installed] | tuple = (),
                 art: frozenset | set = frozenset(), added: list[AddedGame] | tuple = (),
                 icons: frozenset | set = frozenset()) -> dict[str, Any]:
    """Screen 1: a tile per game (each installed store game, each game added by hand, and each recorded game no store
    lists), those with recordings first, newest recording first, and the readiness strip; `art`: the game ids whose
    cover or icon the app holds, `icons` those of them that are a program's icon."""
    out = []
    for g in _games(entries, games, added):
        latest = g.recs[0] if g.recs else None
        n = numbers(latest) if latest is not None else {"lag_type": None, "avg_fps": None}
        out.append({"id": g.id, "name": g.name, "exe_name": latest.exe_name if latest is not None else
                    (exe_file_name(g.added.exe) if g.added is not None else None),
                    "store": g.store, "installed": g.installed is not None or g.added is not None,
                    "added": g.added is not None, "recordings": len(g.recs),
                    "last": when_text(latest.started) if latest is not None else "",
                    "lag_type": lag_text(n["lag_type"]) if latest is not None else "Not recorded yet",
                    "lag_key": n["lag_type"] or "unknown", "avg_fps": fmt(n["avg_fps"]),
                    "tile": tile(g.name, g.id, art, icons)})
    return {"games": out, "recent": [g["id"] for g in out if g["recordings"]][:RECENT],
            "recordings": sum(g["recordings"] for g in out), "installed": sum(g["installed"] for g in out),
            "readiness": readiness}


def _found(entries: list[Entry], game_id: str, games: list[Installed] | tuple = (),
           added: list[AddedGame] | tuple = ()) -> _Game | None:
    return next((g for g in _games(entries, games, added) if g.id == game_id), None)


def game_entries(entries: list[Entry], game_id: str, games: list[Installed] | tuple = (),
                 added: list[AddedGame] | tuple = ()) -> list[Entry]:
    """The recordings of the game the page names by `game_id`, newest first."""
    found = _found(entries, game_id, games, added)
    return found.recs if found is not None else []


def installed_of(entries: list[Entry], game_id: str, games: list[Installed] | tuple = (),
                 added: list[AddedGame] | tuple = ()) -> Installed | None:
    """The installed store game the page names by `game_id`, or None (an added game, a recorded game no store lists,
    or no game)."""
    found = _found(entries, game_id, games, added)
    return found.installed if found is not None else None


def wait_of(entries: list[Entry], game_id: str, games: list[Installed] | tuple = (),
            added: list[AddedGame] | tuple = ()) -> tuple[str, str] | None:
    """(the name, the folder) Record when it starts waits on for the game the page names by `game_id`: an added game,
    or a recorded game no store lists with a folder of its own; None for a store game (it has Play) or one with no
    folder."""
    found = _found(entries, game_id, games, added)
    if found is None or found.folder is None:                # a store game's tile has none: it has Play
        return None
    return found.name, found.folder


PLAY_NOTE = ("lag-doctor could not match this game to one Steam or the Epic Games launcher lists as installed, or to a "
             "folder of its own, so it cannot record it from its start. To record it again, start the game, then "
             "press Record the game that's running in the Library; or add its program there with Add a game, and "
             "lag-doctor waits for it to start.")
ADDED_BROAD_NOTE = ("lag-doctor cannot wait for this game by its folder, {folder}: that folder holds another game's "
                    "folder too, or many programs, so the wait could not tell them apart. Remove it from the Library "
                    "and add the game's own program, in a folder of its own.")
WAIT_TEXT = ("Press it and answer Windows' prompt, then start the game yourself, the way you usually do: lag-doctor "
             "waits for a program from its folder to start and draw, for up to {wait}, and records it from its start "
             "without touching it.")


def play_words(start_key: str | None, window_s: float, lag_key: str) -> str:
    """What Play measures, under its button: from the start key's press for its window, and the lag key."""
    lag = f" Press {lag_key.title()} the moment it lags."
    if start_key is None:
        return f"lag-doctor records {_length_words(window_s)} from the moment the game draws." + lag
    return (f"Once you are in the game, press {start_key.title()}: lag-doctor measures the {_length_words(window_s)} "
            f"after it, so the startup, menus and loading are not counted." + lag)


def game_view(entries: list[Entry], game_id: str, last_id: str | None, games: list[Installed] | tuple = (),
              play: str = "", art: frozenset | set = frozenset(), added: list[AddedGame] | tuple = (),
              icons: frozenset | set = frozenset()) -> dict[str, Any] | None:
    """Screen 2: the game, its last recording's summary on top, every recording of it, and for an installed store game
    its Play button with what it measures (`play`, play_words'); for an added game, or a recorded one no store lists
    with a folder of its own, Record when it starts (the folder wait) instead, and for an added game the way to remove
    it. None for an id the listing does not have."""
    found = _found(entries, game_id, games, added)
    if found is None:
        return None
    latest = found.recs[0] if found.recs else None
    last = None
    if latest is not None:
        n = numbers(latest)
        last = {"session": latest.id, "when": when_text(latest.started), "lag_type": lag_text(n["lag_type"]),
                "lag_key": n["lag_type"] or "unknown", "headline": n["headline"] or "",
                "tiles": [["Average FPS", fmt(n["avg_fps"])], ["1% low FPS", fmt(n["low1_fps"])],
                          ["Stutters / min", fmt(n["stutters_per_min"], "{:.1f}")],
                          ["Top cause", labels.cause_text(n["top_cause"]) if n["top_cause"] else "-"]]}
    store = found.store or STORE_NAMES.get((latest.summary or {}).get("launcher") or "", "")
    wait = None
    if found.folder is not None:                             # a store game's tile has none: it has Play
        wait = {"folder": found.folder, "text": WAIT_TEXT.format(wait=_length_words(PLAY_WAIT_S)) + " " + play}
    exe_name = latest.exe_name if latest is not None else (exe_file_name(found.added.exe) if found.added else None)
    return {"id": game_id, "name": found.name, "exe_name": exe_name,
            "store": store, "tile": tile(found.name, game_id, art, icons), "last": last,
            "sessions": [_session_row(e, last_id) for e in found.recs],
            "play": {"store": found.installed.store_name, "text": play} if found.installed is not None else None,
            "wait": wait, "added": found.added is not None,
            "play_note": "" if found.installed is not None or wait is not None else
            ADDED_BROAD_NOTE.format(folder=found.added.folder) if found.broad else PLAY_NOTE}


# ------------------------------------------------------------------------------------------------ add a game --

ADD_TEXT = ("lag-doctor never starts a program itself. On this game's page, Record when it starts waits for a program "
            "from this folder to start and draw, and records it from its start; its recordings show there.")
NAMED_BY_FILE = "This program names no game, so it shows by its file's name. Change it if you like."
CLASH_WORDS = {
    "same": "{name} is in the Library already.",
    "store": "This program is {name}'s, which {store} lists: it is in the Library already, with Play.",
    "added": ("This program's folder is {name}'s, which is in the Library already: lag-doctor tells games apart by "
              "their folders."),
    "holds": ("This program's folder holds {name}'s folder too, so lag-doctor could not tell the two games apart while "
              "it waits. Choose the game's own program, in a folder of its own."),
}


def draft_view(token: str, name: str, exe: str, folder: str, game_id: str, named_by_file: bool,
               art: frozenset | set = frozenset(), icons: frozenset | set = frozenset()) -> dict[str, Any]:
    """Screen 6: the program the player chose, before it is added: the name it will show by (which the player may
    change), its file, its folder, and its icon where it has one. `token` names this choice for add_game."""
    return {"draft": token, "name": name, "exe": exe, "exe_name": exe_file_name(exe), "folder": folder,
            "tile": tile(name, game_id, art, icons), "text": ADD_TEXT,
            "note": NAMED_BY_FILE if named_by_file else ""}


def clash_view(how: str, game: Any) -> dict[str, Any]:
    """What the page says when the program the player chose is a game the Library has (`existing`: its id, whose page
    it offers) or one it could not tell apart from another (existing None)."""
    name = game.game.title if isinstance(game, Installed) else game.name
    store = game.store_name if isinstance(game, Installed) else ""
    return {"existing": game.game_id if how != "holds" else None,
            "why": CLASH_WORDS[how].format(name=name, store=store or "its store")}


# ------------------------------------------------------------------------------------------- deleting them --

DELETED = "{n} recordings were moved to the Recycle Bin, where Windows can put them back."
DELETED_ONE = "The recording was moved to the Recycle Bin, where Windows can put it back."
NOT_DELETED = "The recording of {when} was not deleted: {why}."


def deleted_view(done: int, failed: list[tuple[Entry, str]], listed: bool) -> dict[str, Any]:
    """What the page says once recordings were deleted (`done` of them moved to the Recycle Bin, `failed` with why
    not), and whether their game is still in the Library (`listed`: a recorded game no store lists and nobody added
    leaves it with its last recording)."""
    said = [DELETED_ONE if done == 1 else DELETED.format(n=done)] if done else []
    said += [NOT_DELETED.format(when=when_text(e.started), why=why) for e, why in failed]
    return {"text": " ".join(said), "deleted": done, "listed": listed}


# ------------------------------------------------------------------------------------------------ results --

def _series(table: Any, columns: tuple[str, ...]) -> tuple[list[float], list[dict[str, Any]]]:
    """(times, [{"col", "label", "values"}]) of the columns that hold at least one number; rows whose time is not a
    sane number are left out."""
    table = _dict(table)
    raw_t = _list(table.get("t"))
    keep = [i for i, t in enumerate(raw_t) if number(t) is not None]
    t = [number(raw_t[i]) for i in keep]
    out = []
    for col in columns:
        vals = _list(table.get(col))
        values = [number(vals[i]) if i < len(vals) else None for i in keep]
        if any(v is not None for v in values):
            out.append({"col": col, "label": labels.column_text(col), "values": values})
    return t, out


def _missing(cap: Capture, source: str) -> str:
    st = _dict(cap.sources).get(source)
    detail, state = getattr(st, "detail", None), getattr(st, "state", None)
    if isinstance(detail, str) and detail.strip():
        return "Not measured: " + labels.reason_text(detail)
    return "Not measured" + (f": {state}" if isinstance(state, str) and state != "ok" else ".")


def _chart(cap: Capture, spec: tuple, table: Any = None) -> dict[str, Any]:
    cid, title, unit, cols, source = spec
    t, series = _series(getattr(cap, source) if table is None else table, cols)
    if not t or not series:
        return {"id": cid, "title": title, "unit": unit, "missing": _missing(cap, source)}
    return {"id": cid, "title": title, "unit": unit, "t": t, "series": series}


def frame_bytes(main: dict[str, list]) -> tuple[bytes, int]:
    """The main swap chain's times and frame times as little-endian doubles, all times first, then all frame times;
    a missing frame time is NaN. (bytes, frame count)."""
    ts, ms = [], []
    for t, v in zip(_list(main.get("t")), _list(main.get("frame_ms"))):
        t, v = number(t), number(v)
        if t is None:
            continue
        ts.append(t)
        ms.append(v if v is not None and v > 0 else math.nan)
        if len(ts) >= MAX_FRAMES:
            break
    data = array.array("d", ts + ms)
    if sys.byteorder != "little":                     # never on Windows; the page reads little-endian
        data.byteswap()
    return data.tobytes(), len(ts)


def _fixes(f, is_last: bool) -> list[dict[str, Any]]:
    out = []
    for fx in f.fixes:
        restart = {"game": "Restart the game afterwards.", "reboot": "Restart the PC afterwards."}.get(
            fx.needs_restart or "", "")
        out.append({"title": fx.title, "detail": fx.detail or "", "restart": restart, "admin": bool(fx.needs_admin),
                    "command": f"lagdoctor fix {fx.fix_id}" if fx.fix_id and is_last else None,
                    "can_apply": bool(fx.fix_id)})
    return out


def results_view(entry: Entry, d: Diagnosis, cap: Capture, main: dict[str, list], is_last: bool,
                 has_report: bool) -> dict[str, Any]:
    """Screen 4, without the frame times themselves (frame_bytes hands those over in a shared buffer)."""
    stats = _dict(d.stats)
    target = _dict(cap.target)
    started = None
    try:
        started = datetime.fromisoformat(cap.started_at)
    except (TypeError, ValueError):
        started = entry.started
    frames_ok = str(_dict(d.measured).get("frames", "")).startswith(("ok", "partial"))
    bound = stats.get("bound")
    timed = [ev for ev in d.events if ev.kind != "mark" and number(ev.t_start) is not None]
    # The worst MAX_EVENTS by severity stay, listed in time order.
    kept = sorted(timed, key=lambda ev: -(number(ev.severity) or 0.0))[:MAX_EVENTS]
    events = [{"id": ev.id, "kind": ev.kind, "what": labels.EVENT_KINDS.get(ev.kind, str(ev.kind)),
               "t": number(ev.t_start), "when": fmt_elapsed(ev.t_start),
               "length": fmt(((number(ev.t_end) or 0.0) - (number(ev.t_start) or 0.0)) * 1000, "{:.0f}", " ms"),
               "how_bad": labels.severity_text(ev.kind, ev.severity, ev.detail),
               "causes": ", ".join(labels.cause_text(c) for c in ev.causes if isinstance(c, str)) or "no clear cause"}
              for ev in sorted(kept, key=lambda ev: number(ev.t_start))]
    marks = labels.marks(stats, d.events)
    part = labels.measured_part(stats)          # a watch measured from its start key, or a part set (M5)
    # Without the diagnosis' own marks, every press the capture kept; for a part, never those its analysis left out.
    mark_times = [t for t, _, _ in marks] or ([] if part else
                                              [m for m in (number(x) for x in _list(cap.marks)) if m is not None])
    refresh = number(stats.get("refresh_hz"))
    stutters = [[number(_dict(ev.detail).get("worst_t", ev.t_start)), number(ev.severity)]
                for ev in d.events if ev.kind == "stutter"]
    findings = [{"title": f.title, "confidence": f.confidence,
                 "confidence_text": labels.CONFIDENCE.get(f.confidence, str(f.confidence)),
                 "summary": f.summary, "evidence": [x.text for x in f.evidence if isinstance(x.text, str)],
                 "fixes": _fixes(f, is_last)} for f in d.findings]
    any_fix = any(fx["can_apply"] for f in findings for fx in f["fixes"])
    charts = [_chart(cap, spec) for spec in CHARTS]
    # The charts' right edge: the recording's length, or the last sample when that is later (report/html._x1).
    end = max([v for v in (number(cap.duration_s), *(number(x) for x in _list(main.get("t"))[-1:]),
                           *(number(c["t"][-1]) for c in charts if c.get("t"))) if v is not None] or [0.0])
    rest = [[a, b] for a, b in labels.rest_spans(part, end)]
    unmeasured = None
    if part is None:                            # a start key that measured nothing: the whole recording was analysed
        unmeasured = labels.unmeasured_note(crop.key_part(cap)[1], pickable=False)
    # The frame chart's top, by the HTML report's own rule (html.frame_scale): from the frames the analysis counted, so
    # neither a loading screen before the part, nor the wait the part starts in, nor time behind another window sets
    # it; None when the report would draw no frames.
    t, ms = html._main_frames(d, cap)
    top = html.frame_scale(d, cap, t, ms)[0] if t and any(v is not None for v in ms) else None
    return {
        "session": entry.id,
        "game": labels.game_name(target),
        "exe_name": labels.game_name({"name": target.get("name")}),
        "when": when_text(started),
        "length": fmt_elapsed(cap.duration_s),
        "note": clean_note(cap.note) or "",
        # Which part the numbers below cover, shaded on the charts as the HTML report shades it; or why a recording
        # with a start key was analysed whole (no press in time, a key Windows did not give: DECISIONS 2026-11-17).
        "part": (labels.part_note(part, len(labels.outside_marks(cap.marks, part))) +
                 (" It is shaded on the charts." if rest else "")) if part else (unmeasured or ""),
        "analysed": f"{labels.part_length(part)} analysed" if part else "",     # as the HTML report's header says
        "frames_text": f"{fmt(stats.get('frames'))} frames" if frames_ok else labels.no_frames_text(d.measured),
        "verdict": {"key": d.lag_type if isinstance(d.lag_type, str) else "unknown", "title": lag_text(d.lag_type),
                    "headline": d.headline},
        "tiles": [["Average FPS", fmt(stats.get("avg_fps"))], ["1% low FPS", fmt(stats.get("low1_fps"))],
                  ["Stutters / min", fmt(stats.get("stutters_per_min"), "{:.1f}")],
                  ["Worst frame", fmt(stats.get("worst_frame_ms"), suffix=" ms")],
                  ["Limited by", labels.BOUND_SHORT.get(bound, str(bound)) if isinstance(bound, str) and bound else "-"],
                  ["Ping (internet)", fmt(stats.get("ping_inet_ms"), suffix=" ms")],
                  ["Jitter", fmt(stats.get("jitter_ms"), "{:.1f}", " ms")],
                  ["Packet loss", fmt(stats.get("loss_pct"), "{:.1f}", "%")]],
        "findings": findings,
        "fix_note": NO_FIX_HERE if any_fix and not is_last else "",
        "frames": {"duration": number(cap.duration_s), "refresh_ms": 1000.0 / refresh if refresh else None,
                   "drops": [[number(ev.t_start), number(ev.t_end)] for ev in d.events if ev.kind == "fps_drop"],
                   "stutters": [s for s in stutters if s[0] is not None and s[1] is not None],
                   "marks": mark_times, "missing": "" if frames_ok else _missing(cap, "frames"),
                   "label": labels.column_text("frame_ms"),
                   # What the part leaves out, shaded on every chart, and the presses there, drawn apart.
                   "rest": rest, "outside": labels.outside_marks(cap.marks, part), "top": top},
        "charts": charts,
        "events": events,
        "events_note": "" if timed else ("No lag events were found." if frames_ok else
                                         "Frame timing was not measured, so stutters could not be looked for."),
        "marks_summary": labels.marks_summary(marks, frames_ok) if marks else "",
        "cleared": [[labels.topic_text(c), why] for c, why in
                    sorted(_dict(d.cleared).items(), key=lambda kv: labels.topic_text(kv[0]).casefold())],
        "not_measured": [list(x) for x in labels.not_measured(cap.sources, d.measured)],
        "unchecked": [[why, topics] for why, topics in labels.group_unchecked(_dict(d.unchecked))],
        "has_report": has_report,
    }


def _programs(rows: list) -> list[dict[str, Any]]:
    """The other programs busiest in a span (Capture.others rows), by mean CPU over the span's samples, a sample a
    program was not listed in counting as 0, as the analysis counts them: the table also lists programs for their disk
    use or the memory they hold, which a mean over only the samples a program appears in would hide among busy ones.
    A program that did nothing there (listed for its memory alone) is left out."""
    acc: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])
    samples = 0
    for row in rows:
        if not (isinstance(row, list) and len(row) == 2 and isinstance(row[1], list)):
            continue
        samples += 1
        for p in row[1]:
            if not isinstance(p, list) or len(p) < 5 or not isinstance(p[1], str):
                continue
            a = acc[p[1]]
            a[0] += number(p[2]) or 0.0
            a[1] += number(p[4]) or 0.0
            a[2] += number(p[3]) or 0.0
    busy = [(name, a) for name, a in acc.items() if any(x > 0 for x in a)]
    top = sorted(busy, key=lambda kv: -kv[1][0])[:TOP_PROGRAMS]
    return [{"name": name, "cpu": fmt(a[0] / samples, suffix="%"), "gpu": fmt(a[1] / samples, suffix="%"),
             "disk": fmt(a[2] / samples / 1e6, "{:.1f}", " MB/s")} for name, a in top]


def moment_view(cap: Capture, d: Diagnosis, main: dict[str, list], event_id: int) -> dict[str, Any] | None:
    """A lag moment: the event, why the analysis named its causes, and the CPU, GPU, disk and network around it
    (analysis.window). None for an event the diagnosis does not have."""
    ev = next((e for e in d.events if e.id == event_id and e.kind != "mark"), None)
    if ev is None or number(ev.t_start) is None:
        return None
    t0, t1 = number(ev.t_start), number(ev.t_end)
    t1 = t0 if t1 is None or t1 < t0 else min(t1, t0 + MOMENT_MAX_S)
    lo, hi = max(0.0, t0 - MOMENT_PAD_S), t1 + MOMENT_PAD_S
    w = cut_window(cap, lo, hi, frames=main)
    why = _dict(ev.detail).get("why")
    frames_t, frames_series = _series(w["frames"], ("frame_ms",))
    part = labels.measured_part(d.stats)        # a moment at a part's edge shows time the analysis left out, shaded
    outside = labels.outside_marks(w["marks"], part)
    return {"id": ev.id, "what": labels.event_text(ev), "when": fmt_elapsed(t0), "t0": t0, "t1": t1,
            "span": [lo, hi], "rest": [[a, b] for a, b in labels.rest_spans(part, max(hi, number(cap.duration_s)
                                                                                        or 0.0))],
            "outside": outside,
            "causes": [labels.cause_text(c) for c in ev.causes if isinstance(c, str)],
            "why": [x for x in why if isinstance(x, str)] if isinstance(why, list) else [],
            "frames": {"t": frames_t, "series": frames_series} if frames_series else None,
            "charts": [_chart(cap, spec, w[spec[4]]) for spec in MOMENT_CHARTS],
            "programs": _programs(w["others"]),
            "marks": [m for m in w["marks"] if m not in outside]}


# ------------------------------------------------------------------------------------------------ compare --

DIFFERENT_COPY = " (different copy)"
FILES_ROWS = (("Drop-in files", "drop_ins", "none found"), ("Unreal settings files", "unreal_config", "none found"))
NOT_UNREAL_TEXT = "not an Unreal Engine game"
OLDER_FILES_NOTE = "A recording made before lag-doctor looked at the game's folder shows its files as -."


def _not_read(v: dict) -> str:
    return "not read: " + labels.reason_text(v["why"]) if v.get("why") else "not read"


def _same_file(f: list, g: list) -> bool:
    """Two summary file rows ([name, sha, bytes, modified]) are one file: the same name, and the same SHA-256 when
    both were hashed, else the same size and date."""
    if f[0].casefold() != g[0].casefold():
        return False
    return f[1] == g[1] if f[1] and g[1] else (f[2], f[3]) == (g[2], g[3])


def _unmatched(xs: list, ys: list) -> list:
    rest = list(ys)
    out = []
    for x in xs:
        hit = next((i for i, y in enumerate(rest) if _same_file(x, y)), None)
        if hit is None:
            out.append(x)
        else:
            rest.pop(hit)
    return out


def files_row(name: str, va: Any, vb: Any, none_text: str) -> dict[str, Any]:
    """A Compare row of files: each side's names, a name both have in another copy marked so; "-" for a summary
    older than the folder check. differs when both sides were read and do not hold the same files."""
    def text(v, other, odd):
        if v is None:
            return None
        if v["state"] == NOT_UNREAL:
            return NOT_UNREAL_TEXT
        if v["state"] != CHECKED:
            return _not_read(v)
        if not v["files"]:
            return none_text
        theirs = {f[0].casefold() for f in other["files"]} if other and other["state"] == CHECKED else set()
        names = [f[0] + (DIFFERENT_COPY if any(f is o for o in odd) and f[0].casefold() in theirs else "")
                 for f in v["files"]]
        return ", ".join(names) + (f", and {v['more']} more" if v["more"] else "")

    ok = va is not None and vb is not None and va["state"] in (CHECKED, NOT_UNREAL) \
        and vb["state"] in (CHECKED, NOT_UNREAL)
    odd_a = _unmatched(va["files"], vb["files"]) if ok else []
    odd_b = _unmatched(vb["files"], va["files"]) if ok else []
    differs = ok and (va["state"] != vb["state"] or bool(odd_a or odd_b) or va["more"] != vb["more"])
    return {"name": name, "a": text(va, vb, odd_a) or "-", "b": text(vb, va, odd_b) or "-", "better": None,
            "differs": differs}


def exe_row(va: Any, vb: Any) -> dict[str, Any]:
    """The game's exe by size and date: a game update between two recordings shows here."""
    def text(v):
        if v is None:
            return None
        if v["state"] != CHECKED:
            return _not_read(v)
        date = labels.date_text(v["modified"])
        return ", ".join(x for x in (labels.size_text(v["bytes"]), f"dated {date}" if date else None) if x) or None

    ok = va is not None and vb is not None and va["state"] == vb["state"] == CHECKED
    return {"name": "Game's exe file", "a": text(va) or "-", "b": text(vb) or "-", "better": None,
            "differs": ok and (va["bytes"], va["modified"]) != (vb["bytes"], vb["modified"])}


def compare_view(a: Entry, b: Entry) -> dict[str, Any]:
    """Screen 5: two recordings side by side, with the conditions each ran under next to the numbers."""
    na, nb = numbers(a), numbers(b)

    def row(name, key, pattern="{:.0f}", suffix="", better=None):
        va, vb = number(na.get(key)), number(nb.get(key))
        winner = None
        if better and va is not None and vb is not None and va != vb:
            winner = "a" if (va > vb) == (better == "high") else "b"
        return {"name": name, "a": fmt(va, pattern, suffix), "b": fmt(vb, pattern, suffix), "better": winner}

    def text_row(name, va, vb):
        return {"name": name, "a": va or "-", "b": vb or "-", "better": None,
                "differs": bool(va and vb and va != vb)}

    def said(name, va, vb):          # a measured row in words: no side is "better"
        return {"name": name, "a": va or "-", "b": vb or "-", "better": None}

    def limited(n):
        return labels.BOUND_SHORT.get(n.get("bound")) if n.get("bound") not in (None, "unknown") else None

    def of(n, part, whole, pattern, unit):
        x, y = number(n.get(part)), number(n.get(whole))
        if x is None:
            return None
        return pattern.format(x) + (f" of {pattern.format(y)}" if y is not None else "") + unit

    def fans(n):                     # the median CPU and GPU fan speeds, the fans source's own words
        cpu, gpu = number(n.get("fan_cpu_rpm")), number(n.get("fan_gpu_rpm"))
        said_ = [f"{w} {x:,.0f} RPM" for w, x in (("CPU", cpu), ("GPU", gpu)) if x is not None]
        return ", ".join(said_) or None

    def power(n):
        mode = n.get("power_mode")
        plan = n.get("power_plan")
        if not plan and not mode:
            return None
        return ", ".join(x for x in (plan, f"power mode {mode.replace('_', ' ')}" if mode else None) if x)

    def source(n):
        kind = n.get("frames_source")
        if kind == "display":
            return "when frames reached the screen"
        if kind == "present":
            return "when the game handed frames over"
        return "not recorded" if n.get("frames_measured") is False else None

    older = [x.id for x in (a, b) if x.summary is None]
    before_files = [x.id for x in (a, b) if x.summary is not None and x.summary.get("drop_ins") is None]
    sa, sb = a.summary or {}, b.summary or {}
    files = [exe_row(sa.get("game_exe"), sb.get("game_exe"))] + [
        files_row(name, sa.get(key), sb.get(key), none_text) for name, key, none_text in FILES_ROWS]
    return {
        "a": {"id": a.id, "when": when_text(a.started), "lag_type": lag_text(na["lag_type"])},
        "b": {"id": b.id, "when": when_text(b.started), "lag_type": lag_text(nb["lag_type"])},
        "game": a.game,
        "numbers": [row("Average FPS", "avg_fps", better="high"), row("1% low FPS", "low1_fps", better="high"),
                    row("Stutters / min", "stutters_per_min", "{:.1f}", better="low"),
                    row("Worst frame", "worst_frame_ms", suffix=" ms", better="low"),
                    # What held the frame rate, and how close the other parts were (DESIGN, Headroom).
                    row("FPS outside lag moments", "steady_fps", better="high"),
                    said("Limited by", limited(na), limited(nb)),
                    said("GPU busy per frame", of(na, "bound_gpu_ms", "bound_frame_ms", "{:.1f}", " ms"),
                         of(nb, "bound_gpu_ms", "bound_frame_ms", "{:.1f}", " ms")),
                    row("Busiest game thread", "busiest_thread_pct", suffix="% of a CPU"),
                    said("NVIDIA GPU clock", of(na, "gpu_clock_mhz", "gpu_clock_max_mhz", "{:,.0f}", " MHz"),
                         of(nb, "gpu_clock_mhz", "gpu_clock_max_mhz", "{:,.0f}", " MHz")),
                    said("Fans (median)", fans(na), fans(nb)),
                    said("Headroom", na.get("headroom"), nb.get("headroom"))],
        # The player's own words come first: what they say they changed, beside what lag-doctor saw change.
        "conditions": [text_row("Your note", sa.get("note"), sb.get("note")),
                       text_row("Power plan and mode", power(na), power(nb)),
                       text_row("Refresh rate", fmt(na.get("refresh_hz"), suffix=" Hz") if na.get("refresh_hz")
                                else None, fmt(nb.get("refresh_hz"), suffix=" Hz") if nb.get("refresh_hz") else None),
                       text_row("Frames timed", source(na), source(nb)),
                       text_row("Measured length", fmt_elapsed(na.get("duration_s")) if na.get("duration_s")
                                is not None else None,
                                fmt_elapsed(nb.get("duration_s")) if nb.get("duration_s") is not None else None)]
        + files,
        "note": " ".join(x for x in ("An older recording has no summary, so its conditions show as -." if older else "",
                                     OLDER_FILES_NOTE if before_files else "",
                                     labels.HEADROOM_NOTE if na.get("headroom") or nb.get("headroom") else "")
                         if x),
    }


# ----------------------------------------------------------------------------------------------- settings --

def settings_view(readiness: dict[str, Any], sessions_dir: str, runtime_version: str | None, app_version: str,
                  lag_key: str, start_key: str | None = None) -> dict[str, Any]:
    """Screen 7: readiness, the keys, a sound test (one of the recording's own sounds) and where sessions are kept. A
    choice of keys and length comes later; the library's stores come with Play."""
    start = [["Start measuring: press once in the game, at the spot to measure", start_key.title()]] if start_key \
        else []
    return {"readiness": readiness,
            "keys": start + [["Mark a lag while recording", lag_key.title()]],
            "sessions_folder": sessions_dir,
            "library_sources": "Games you have recorded. Steam and Epic libraries come with Play.",
            "about": [["lag-doctor", app_version], ["WebView2 Runtime", runtime_version or "-"]]}


def readiness_view(presentmon_ok: bool, presentmon_why: str, rights: str) -> dict[str, Any]:
    """The one-line readiness strip: whether frame timing can be recorded, and that each recording from the app
    asks Windows for permission once: it records with --elevate, so an account that may already trace (trace_rights
    "now": in Performance Log Users, since the app never runs elevated) is asked for the game-server check alone."""
    if not presentmon_ok:
        return {"ok": False, "text": "Frame timing is not set up: run `lagdoctor setup` in a terminal",
                "detail": presentmon_why}
    asks = ("each recording asks Windows once, for the game-server check" if rights == "now" else
            "each recording asks Windows once")
    return {"ok": True, "text": f"Frame timing ready · {asks}", "detail": ""}


# ---------------------------------------------------------------------------------------------- recording --

RECORDING_TITLES = {
    "starting": "Starting the recording",
    "waiting": "Looking for the game",
    "asking": "Windows asks for permission",
    "preparing": "Getting ready to record",
    "launching": "Starting the game",
    "recording": "Recording",
    "stopping": "Stopping the recording",
    "saving": "Saving the recording",
    "analysing": "Analysing the recording",
}
RECORDING_DETAILS = {
    "starting": "lag-doctor is starting its recorder.",
    "waiting": "lag-doctor is looking for the game that is running: the busiest program with a window.",
    "asking": ("Frame timing and the game-server check need administrator rights for this recording: answer "
               "Windows' prompt, and if it only blinks on the taskbar, click it there. Only the small part of "
               "lag-doctor that reads them runs with those rights."),
    "preparing": "Windows answered: lag-doctor is starting frame timing and its other measurements.",
    "launching": "lag-doctor asked Windows to open the game's store, which starts the game.",
    "recording": ("Play normally. This window stays still while you play, so it does not change what is measured. "
                  "The recording stops by itself{length}, or when the game closes."),
    "stopping": "The recording is stopping.",
    "saving": "The recording is being saved.",
    "analysing": "lag-doctor is looking for what made it lag.",
}
# "preparing" by how the prompt went, when it did not start the relay.
PREPARING_DETAILS = {
    "declined": ("Windows' prompt was declined: lag-doctor is starting the recording, with what needed that "
                 "permission off."),
    "refused": ("Windows' prompt could not be used: lag-doctor is starting the recording, with what needed that "
                "permission off."),
    "failed": ("Windows' prompt could not be shown: lag-doctor is starting the recording, with what needed that "
               "permission off."),
    "withdrawn": "The recording is being cancelled.",
}
# Play (M6) in its own words, by phase: {store} is the store's name.
PLAY_TITLES = {"waiting": "Waiting for the game"}
PLAY_DETAILS = {
    "launching": "lag-doctor asked Windows to open {store}, which starts the game. If {store} is closed, it starts "
                 "first.",
    "waiting": ("lag-doctor is waiting for the game to start from its folder and draw, for up to {wait}. If {store} "
                "has to start first, it does so now, and if it shows something in its own window (a failed cloud "
                "sync, a notice), answer it there."),
}
PLAY_ENDS = {
    "no_game": ("The game did not start", "No program from the game's folder started and drew within {wait}, so "
                                          "nothing was recorded. If {store} was downloading an update or asking "
                                          "something, let it finish, then press Play again."),
}
PLAY_WAIT_S = 300.0                   # recording.WAIT_S, which a test holds equal (views never imports recording)
# Record when it starts (screen 6): the folder wait for a game the player starts themselves. "launch" says the moment:
# Windows' prompt is answered and the checks have passed, so a game started now is one the wait can take.
SELF_TITLES = {"launching": "Start the game now", "waiting": "Start the game now"}
SELF_WAITING = ("Start {title} yourself, the way you usually do. lag-doctor is waiting for a program from its "
                "folder to start and draw, for up to {wait}, and records it from its start without touching it.")
SELF_DETAILS = {
    "starting": "lag-doctor is starting its recorder. Do not start the game yet: lag-doctor says when.",
    "asking": RECORDING_DETAILS["asking"] + " Start the game only once you have answered it.",
    "preparing": "Windows answered: lag-doctor is getting ready. Do not start the game yet: lag-doctor says when.",
    "launching": SELF_WAITING,
    "waiting": SELF_WAITING,
}
SELF_ENDS = {
    "no_game": ("The game did not start", "No program from the game's folder started and drew within {wait}, so "
                                          "nothing was recorded. Press Record when it starts again, answer Windows' "
                                          "prompt, then start the game."),
    "game_running": ("Not recorded", "A program from the game's folder was already running, so lag-doctor could not "
                                     "record the game from its start. Close the game, press Record when it starts "
                                     "again and start the game once Windows' prompt is answered; or record it as it "
                                     "runs, with Record the game that's running in the Library."),
}
LAUNCH_FAILED = ("Windows could not open {store} ({why}). Start the game yourself, from {store} or its own program: "
                 "lag-doctor is waiting for it.")
# How a recording ended (control.REASONS): a title and a sentence.
RECORDING_ENDS = {
    "completed": ("Recorded", "The recording ran its full length and is analysed."),
    "stopped": ("Recorded", "The recording was stopped and is analysed."),
    "game_exited": ("Recorded", "The game closed, so the recording stopped there. It is analysed."),
    "cancelled": ("Cancelled", "Nothing was recorded."),
    "busy": ("Not recorded", "Another recording is running. Try again once it has finished."),
    "not_ready": ("Not recorded", "Frame timing is not set up, so nothing was recorded: run `lagdoctor setup` in a "
                                  "terminal, then try again."),
    "no_game": ("No game found", "lag-doctor found no game running. Start the game, then press Record again."),
    "game_running": ("Not recorded", "The game was already running, so its store would start nothing and lag-doctor "
                                     "could not take it from its start. Close the game, then press Play again."),
    "refused": ("Not recorded", "lag-doctor could not record that game."),
    "game_changed": ("Not recorded", "The game closed or restarted while Windows asked for permission, so nothing "
                                     "was recorded. Start it again, then press Record."),
    "relay_failed": ("Not recorded", "The recording stopped on an error before it was saved."),
    "analysis_failed": ("Saved, not analysed", "The recording was saved, but analysing it failed. Its results may "
                                               "still open: lag-doctor analyses it again."),
    "private_copy": ("Not recorded", "lag-doctor cannot record from here."),
    "failed": ("Not recorded", "Something went wrong."),
}
# A recording measured from the app's start key (M5c), in "recording" by where it has got to: the key not said yet,
# the startup before the press, and the measured window; then the stop and the end by why it stopped
# (RecordingState.stop_reason, capture/session.py: both reach the app as done's "completed").
STILL = "This window stays still while you play, so it does not change what is measured."
KEY_SETUP = ("Play normally. " + STILL + " lag-doctor is getting {key} ready: wait for its chime before you press it.")
KEY_STARTUP = ("Play normally: the startup, menus and loading are recorded but not counted. When you reach the spot "
               "you want measured, press {key}: a rising chime plays, and lag-doctor measures {window}.{by} " + STILL)
KEY_NO_PRESS = " With no press within {start_by}, the recording stops by itself and all of it is analysed."
KEY_MEASURING = ("Play on: lag-doctor is measuring {window} from your press. When they are over, a falling chime "
                 "plays and the recording stops by itself, to be analysed while you play on. It also stops when the "
                 "game closes. " + STILL)
KEY_MEASURING_TO_END = ("Play on: lag-doctor is measuring from your press until the recording stops{length}, or when "
                        "the game closes. " + STILL)
KEY_STOPPING = {
    sess.WINDOW_ENDED: ("Measured", "lag-doctor measured {window} from your start key, so the recording stops. Play on: "
                                    "it is saved and analysed while you play."),
    sess.NO_START: ("Stopping the recording", "The start key was not pressed within {start_by}, so the recording "
                                              "stops, and all of it will be analysed, startup included."),
}
KEY_ENDS = {
    sess.WINDOW_ENDED: ("Measured", "lag-doctor measured {window} from your start key, and they are analysed."),
    sess.NO_START: ("Recorded", "The start key was not pressed within {start_by}, so the recording stopped there. All "
                                "of it is analysed, startup included."),
}
PROMPT_TEXTS = {"declined": "Windows' prompt was declined",
                "refused": "lag-doctor's window refused the prompt's command",
                "failed": "Windows' prompt could not be shown"}
SAID_SHOWN = 12                       # lines the recorder said, shown when a recording failed
# What the recorder says for a console only (its options, Ctrl+C, a command to type), which the window cannot take.
CONSOLE_ONLY = re.compile(r"--[a-z]|Ctrl\+C|`lagdoctor ")


def _length_words(s: float | None) -> str:
    """'10 minutes', '1m 30s': a length the running screen names."""
    if s is None:
        return ""
    return f"{int(s // 60)} minutes" if s >= 120 and s % 60 == 0 else fmt_elapsed(s)


def _stage(state: Any) -> str | None:
    """Where a recording measured from its start key has got to while it records: "setup" until the recorder says
    whether Windows gave the key, "startup" until the press, then "measuring"; None without a start key, or with
    one Windows did not give (the whole recording is analysed, as without one)."""
    if not getattr(state, "keyed", False) or state.phase != "recording" or state.key_on is False:
        return None
    if state.key_on is None:
        return "setup"
    return "measuring" if state.measuring_since is not None else "startup"


def _measured_s(state: Any) -> float | None:
    """How long the start key's window measures: from the press to the end the recorder said (its `until`), or the
    window it was given; None when it runs to the recording's end."""
    if state.measure_at is not None and state.measure_until is not None:
        # In the recorder's milliseconds: 600.0, never the 599.9999999999999 a press at 424.033 s would give.
        return max(0.0, round(state.measure_until - state.measure_at, 3))
    return state.window_s


def _key_words(text: str, state: Any, key: str) -> str:
    """`text` with its key, its measured window ('10 minutes') and the time a press had to come by."""
    measured = _measured_s(state)
    return text.format(key=key, window=_length_words(measured) if measured else "the part",
                       start_by=_length_words(state.start_by_s) or "the time it had")


def recording_view(state: Any, now: float, lag_key: str, log: Any = None,
                   start_key: str | None = None) -> dict[str, Any]:
    """Screen 3, the running state of a recording the app started (recording.RecordingState): where it has got to,
    the one thing to press now (Cancel before it records, Stop while it does), what is off, and at the end how it
    ended, with what the recorder said (`log`: the recorder's log, named when something went wrong). {"active":
    False, "phase": None} when the app has started none. With a start key (`start_key`, the one the app asked for, or
    the one the recorder named), it says when to press it, and its clock counts the measured window from the press:
    "07:12 of 10:00"."""
    if state is None:
        return {"active": False, "phase": None}
    elapsed, length = None, state.length_s
    if state.started is not None:
        elapsed = max(0.0, (state.ended if state.ended is not None else now) - state.started)
    stage = _stage(state)
    key = (state.key or start_key or "").title() or "the start key"
    if stage == "measuring":
        elapsed, length = max(0.0, now - state.measuring_since), _measured_s(state)
    elif stage in ("setup", "startup"):
        length = None                               # no end to count to: the press starts the measured window
    play = getattr(state, "play", None)
    by_hand = bool((play or {}).get("self"))            # Record when it starts: the player starts the game
    store = (play or {}).get("store") or "the game's store"
    play_words_of = {"store": store, "wait": _length_words(PLAY_WAIT_S), "title": (play or {}).get("title") or
                     "the game"}
    notes = []
    if state.fault:
        notes.append(f"lag-doctor stopped this recording: {state.fault}.")
    if play is not None and getattr(state, "launch_failed", None) and state.phase in ("launching", "waiting"):
        notes.append(LAUNCH_FAILED.format(store=store, why=state.launch_failed))
    if state.frames is False:
        notes.append(f"Frame timing is off for this recording: {PROMPT_TEXTS.get(state.prompt or '', 'it could not start')}.")
    if state.prompt in PROMPT_TEXTS:
        notes.append("The game-server check is off for this recording: it needs the same permission.")
    if state.network is False:
        notes.append("The network checks are off for this recording.")
    if state.started is not None and not state.hotkey:
        notes.append(f"The lag key is off for this recording: Windows did not give lag-doctor {lag_key.title()}, so "
                     "another program may be using it.")
    if getattr(state, "keyed", False) and state.key_on is False:
        notes.append(f"The start key is off for this recording: Windows did not give lag-doctor {key}, so another "
                     "program may be using it. All of the recording is analysed, startup included.")
    if state.frames_lost:
        notes.append(f"Frame timing stopped during the recording: {state.frames_lost}")
    if state.marks:
        notes.append(f"Lag marked {state.marks} time{'' if state.marks == 1 else 's'}.")
    failed = state.phase == "done" and not state.ok and (state.reason != "cancelled" or state.fault is not None)
    said = [line for line in state.said if not CONSOLE_ONLY.search(line)][-SAID_SHOWN:] if failed else []
    keyed_end = getattr(state, "keyed", False) and state.stop_reason in KEY_ENDS
    if state.phase == "done":
        title, detail = RECORDING_ENDS.get(state.reason or "failed", RECORDING_ENDS["failed"])
        ends = SELF_ENDS if by_hand else PLAY_ENDS
        if play is not None and state.reason in ends:
            title, detail = ends[state.reason]
            detail = detail.format(**play_words_of)
        if keyed_end and state.reason == "completed":
            title, detail = KEY_ENDS[state.stop_reason]
            detail = _key_words(detail, state, key)
        if state.fault:
            title, detail = "Not recorded", "lag-doctor stopped it."
        if said:
            detail += " What lag-doctor said is below."
        if failed and log is not None and state.unexplained:
            detail += f" The details are in {log}."
    else:
        title, detail = RECORDING_TITLES[state.phase], RECORDING_DETAILS[state.phase]
        if by_hand and state.phase in SELF_DETAILS:
            title = SELF_TITLES.get(state.phase, title)
            detail = SELF_DETAILS[state.phase].format(**play_words_of)
        elif play is not None and state.phase in PLAY_DETAILS:
            title = PLAY_TITLES.get(state.phase, title)
            detail = PLAY_DETAILS[state.phase].format(**play_words_of)
        if state.phase == "preparing":
            detail = PREPARING_DETAILS.get(state.prompt or "", detail)
        if state.phase == "stopping" and keyed_end:
            title, detail = KEY_STOPPING[state.stop_reason]
            detail = _key_words(detail, state, key)
        if state.phase == "recording":
            after = f" after {_length_words(state.length_s)}" if state.length_s else ""
            if stage == "setup":
                detail = _key_words(KEY_SETUP, state, key)
            elif stage == "startup":
                title = "Recording the startup"
                measured = _measured_s(state)
                by = KEY_NO_PRESS.format(start_by=_length_words(state.start_by_s)) if state.start_by_s else \
                    (f" The recording stops by itself{after}, or when the game closes." if after else "")
                detail = KEY_STARTUP.format(key=key, by=by, window=f"the next {_length_words(measured)}" if measured
                                            else "from your press on")
            elif stage == "measuring":
                title = "Measuring"
                detail = _key_words(KEY_MEASURING, state, key) if _measured_s(state) else \
                    KEY_MEASURING_TO_END.format(length=after)
            else:
                detail = detail.format(length=after)
            if state.hotkey:
                detail += f" Press {lag_key.title()} the moment it lags."
    return {"active": state.active, "phase": state.phase, "stage": stage, "title": title, "detail": detail,
            "game": state.game or (play or {}).get("title"), "elapsed_s": elapsed, "length_s": length, "notes": notes,
            # Play's game, by the page's id of it, so "Try again" plays it again (no link reaches the page), and
            # whether it was Record when it starts, which "Try again" asks for again
            "play": {"game": play["game"], "wait": by_hand} if play is not None else None,
            "action": ("cancel" if state.phase in ("starting", "waiting", "asking", "preparing", "launching") else
                       "stop" if state.phase == "recording" else None),
            "ok": state.phase == "done" and state.ok,
            "session": state.session if state.phase == "done" else None,
            "said": said}
