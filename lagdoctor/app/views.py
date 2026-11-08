"""What each screen shows (docs/LAUNCHER-PLAN.md, screens 1, 2, 4, 5 and 7), built from sessions on disk.

Each view is a plain dict of text and numbers, ready for JSON: the page only lays it out. Text from a capture or the
PC (a game's window title, a process name, evidence) stays text here, and the page puts it on screen with
textContent, never as markup (tests/test_app_page.py). Numbers a damaged file holds read as missing (charts.number).
Pure functions apart from library_view's and game_view's reads of small files; the window calls them from worker
threads.
"""
from __future__ import annotations

import array
import math
import sys
from collections import defaultdict
from datetime import datetime
from typing import Any

from lagdoctor.analysis.window import window as cut_window
from lagdoctor.app.library import Entry, read_diagnosis
from lagdoctor.model import Capture, Diagnosis, G, N, S
from lagdoctor.report import labels
from lagdoctor.report.charts import fmt_elapsed, number
from lagdoctor.report.summary import CHECKED, NOT_UNREAL

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


def tile(name: str) -> dict[str, Any]:
    """A generated cover tile: the game's initials on a colour from its name (no art is downloaded)."""
    words = [w for w in "".join(ch if ch.isalnum() else " " for ch in name).split() if w]
    initials = "".join(w[0] for w in words[:2]).upper() or "?"
    hue = sum(ord(ch) * (i + 1) for i, ch in enumerate(name)) % 360
    return {"initials": initials, "hue": hue}


def numbers(entry: Entry) -> dict[str, Any]:
    """A session's headline numbers: from its summary, else (an older session) from its diagnosis.json."""
    s = entry.summary
    if s is not None:
        return {k: s.get(k) for k in ("lag_type", "headline", "avg_fps", "low1_fps", "stutters_per_min",
                                      "worst_frame_ms", "top_cause", "duration_s", "frames_source", "refresh_hz",
                                      "power_plan", "power_mode", "frames_measured")}
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
            "frames_measured": None}


def _session_row(entry: Entry, last_id: str | None) -> dict[str, Any]:
    n = numbers(entry)
    return {"id": entry.id, "when": when_text(entry.started), "length": fmt_elapsed(n["duration_s"]),
            "lag_type": lag_text(n["lag_type"]), "lag_key": n["lag_type"] or "unknown",
            "avg_fps": fmt(n["avg_fps"]), "low1_fps": fmt(n["low1_fps"]),
            "stutters_per_min": fmt(n["stutters_per_min"], "{:.1f}"),
            "top_cause": labels.cause_text(n["top_cause"]) if n["top_cause"] else "nothing stood out",
            "is_last": entry.id == last_id}


# ------------------------------------------------------------------------------------------------ library --

def library_view(entries: list[Entry], readiness: dict[str, Any]) -> dict[str, Any]:
    """Screen 1: a tile per recorded game, newest recording first, and the readiness strip."""
    games: dict[str, list[Entry]] = defaultdict(list)
    for e in entries:
        if e.is_recording:
            games[e.game_key].append(e)
    out = []
    for key, recs in games.items():
        latest = recs[0]                                     # entries come newest first
        n = numbers(latest)
        out.append({"id": latest.game_id, "name": latest.game, "exe_name": latest.exe_name,
                    "recordings": len(recs), "last": when_text(latest.started),
                    "lag_type": lag_text(n["lag_type"]), "lag_key": n["lag_type"] or "unknown",
                    "avg_fps": fmt(n["avg_fps"]), "tile": tile(latest.game)})
    return {"games": out, "recent": [g["id"] for g in out[:RECENT]],
            "recordings": sum(len(v) for v in games.values()), "readiness": readiness}


def game_entries(entries: list[Entry], game_id: str) -> list[Entry]:
    return [e for e in entries if e.is_recording and e.game_id == game_id]


def game_view(entries: list[Entry], game_id: str, last_id: str | None) -> dict[str, Any] | None:
    """Screen 2: the game, its last recording's summary on top, and every recording of it. None for an id the
    listing does not have."""
    recs = game_entries(entries, game_id)
    if not recs:
        return None
    latest = recs[0]
    n = numbers(latest)
    return {"id": game_id, "name": latest.game, "exe_name": latest.exe_name,
            "store": {"steam": "Steam", "epic": "Epic Games"}.get((latest.summary or {}).get("launcher") or "", ""),
            "tile": tile(latest.game),
            "last": {"session": latest.id, "when": when_text(latest.started), "lag_type": lag_text(n["lag_type"]),
                     "lag_key": n["lag_type"] or "unknown", "headline": n["headline"] or "",
                     "tiles": [["Average FPS", fmt(n["avg_fps"])], ["1% low FPS", fmt(n["low1_fps"])],
                               ["Stutters / min", fmt(n["stutters_per_min"], "{:.1f}")],
                               ["Top cause", labels.cause_text(n["top_cause"]) if n["top_cause"] else "-"]]},
            "sessions": [_session_row(e, last_id) for e in recs],
            "play_note": "Recording from the app comes next. For now, start the game and run "
                         "`lagdoctor watch` in a terminal; the recording appears here when it is analysed."}


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
    mark_times = [t for t, _, _ in marks] or [m for m in (number(x) for x in _list(cap.marks)) if m is not None]
    refresh = number(stats.get("refresh_hz"))
    stutters = [[number(_dict(ev.detail).get("worst_t", ev.t_start)), number(ev.severity)]
                for ev in d.events if ev.kind == "stutter"]
    findings = [{"title": f.title, "confidence": f.confidence,
                 "confidence_text": labels.CONFIDENCE.get(f.confidence, str(f.confidence)),
                 "summary": f.summary, "evidence": [x.text for x in f.evidence if isinstance(x.text, str)],
                 "fixes": _fixes(f, is_last)} for f in d.findings]
    any_fix = any(fx["can_apply"] for f in findings for fx in f["fixes"])
    return {
        "session": entry.id,
        "game": labels.game_name(target),
        "exe_name": labels.game_name({"name": target.get("name")}),
        "when": when_text(started),
        "length": fmt_elapsed(cap.duration_s),
        "frames_text": f"{fmt(stats.get('frames'))} frames" if frames_ok else "frame timing not recorded",
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
                   "label": labels.column_text("frame_ms")},
        "charts": [_chart(cap, spec) for spec in CHARTS],
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
    """The other programs busiest in a span (Capture.others rows), by mean CPU over the samples they appear in."""
    acc: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0, 0])
    for row in rows:
        for p in _list(row[1]) if isinstance(row, list) and len(row) == 2 else []:
            if not isinstance(p, list) or len(p) < 5 or not isinstance(p[1], str):
                continue
            a = acc[p[1]]
            a[0] += number(p[2]) or 0.0
            a[1] += number(p[4]) or 0.0
            a[2] += number(p[3]) or 0.0
            a[3] += 1
    top = sorted(acc.items(), key=lambda kv: -kv[1][0] / max(1, kv[1][3]))[:TOP_PROGRAMS]
    return [{"name": name, "cpu": fmt(a[0] / a[3], suffix="%"), "gpu": fmt(a[1] / a[3], suffix="%"),
             "disk": fmt(a[2] / a[3] / 1e6, "{:.1f}", " MB/s")} for name, a in top if a[3]]


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
    return {"id": ev.id, "what": labels.event_text(ev), "when": fmt_elapsed(t0), "t0": t0, "t1": t1,
            "span": [lo, hi],
            "causes": [labels.cause_text(c) for c in ev.causes if isinstance(c, str)],
            "why": [x for x in why if isinstance(x, str)] if isinstance(why, list) else [],
            "frames": {"t": frames_t, "series": frames_series} if frames_series else None,
            "charts": [_chart(cap, spec, w[spec[4]]) for spec in MOMENT_CHARTS],
            "programs": _programs(w["others"]),
            "marks": w["marks"]}


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
                    row("Worst frame", "worst_frame_ms", suffix=" ms", better="low")],
        "conditions": [text_row("Power plan and mode", power(na), power(nb)),
                       text_row("Refresh rate", fmt(na.get("refresh_hz"), suffix=" Hz") if na.get("refresh_hz")
                                else None, fmt(nb.get("refresh_hz"), suffix=" Hz") if nb.get("refresh_hz") else None),
                       text_row("Frames timed", source(na), source(nb)),
                       text_row("Measured length", fmt_elapsed(na.get("duration_s")) if na.get("duration_s")
                                is not None else None,
                                fmt_elapsed(nb.get("duration_s")) if nb.get("duration_s") is not None else None)]
        + files,
        "note": " ".join(x for x in ("An older recording has no summary, so its conditions show as -." if older else "",
                                     OLDER_FILES_NOTE if before_files else "") if x),
    }


# ----------------------------------------------------------------------------------------------- settings --

def settings_view(readiness: dict[str, Any], sessions_dir: str, runtime_version: str | None, app_version: str,
                  lag_key: str) -> dict[str, Any]:
    """Screen 7: readiness, the keys, a sound test and where sessions are kept. Recording from the app, its start
    key and its default length come later; the library's stores come with Play."""
    return {"readiness": readiness,
            "keys": [["Mark a lag while recording", lag_key.title()]],
            "sessions_folder": sessions_dir,
            "library_sources": "Games you have recorded. Steam and Epic libraries come with Play.",
            "about": [["lag-doctor", app_version], ["WebView2 Runtime", runtime_version or "-"]]}


def readiness_view(presentmon_ok: bool, presentmon_why: str, rights: str) -> dict[str, Any]:
    """The one-line readiness strip: whether frame timing can be recorded, and whether each recording asks Windows
    for permission (trace_rights: "now" needs no prompt)."""
    if not presentmon_ok:
        return {"ok": False, "text": "Frame timing is not set up: run `lagdoctor setup` in a terminal",
                "detail": presentmon_why}
    asks = "no permission prompt" if rights == "now" else "each recording asks Windows once"
    return {"ok": True, "text": f"Frame timing ready · {asks}", "detail": ""}
