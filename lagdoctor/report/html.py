"""The HTML report: one self-contained file (inline CSS and SVG, no scripts, no network).

Every string that came from outside the tool (game and process names, window
titles, adapter names, evidence text) is escaped: a process can be named
"<script>...", and the report must still be a harmless page.

The page is meant to be shared, so it never shows the Windows user name: e()
shows a folder under a user's profile by the variable that names it
(%LOCALAPPDATA%\\lag-doctor\\sessions\\...), and the analyze command in the
footer is written for PowerShell ("$env:LOCALAPPDATA\\...").

A capture can be damaged (A15): every number read from it goes through
charts.number() (junk, NaN and corrupt magnitudes read as missing), and each
section is built on its own, so one that still breaks is replaced by a note
saying so while the rest of the report is written.
"""
from __future__ import annotations

import functools
import ntpath
import os
import re
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any, Callable

from lagdoctor import __version__
from lagdoctor.analysis.stats import percentile
from lagdoctor.model import Capture, Diagnosis, F, N, S, clean_note
from lagdoctor.report import labels
from lagdoctor.report.charts import Chart, fmt_elapsed, number


def e(value: Any) -> str:
    return escape(user_neutral("" if value is None else str(value)), quote=True)


# ------------------------------------------------------------ the user's folders --
# A folder under a user's profile carries the Windows user name: C:\Users\<name>\AppData\Local\lag-doctor\...

PROFILE_VARS = ("LOCALAPPDATA", "APPDATA", "USERPROFILE")    # tried in this order, so the deepest folder wins
_SEP = r"[\\/]+"                                            # also / and a repr's doubled backslash
_WHOLE = r'(?![^\\/:*?"<>|\x00-\x1f])'      # the folder ends here: next comes a separator, the end, or no name
# Any profile in the usual place, also one this PC's variables do not name: a capture from another PC, or an 8.3
# short name. The shared profiles hold no one's name. The name stops at a separator or a character no Windows
# account name can hold.
_ANY_PROFILE = (r"[A-Za-z]:" + _SEP + "Users" + _SEP + r"(?!(?:Public|Default|Default User|All Users)"
                + _WHOLE + r')[^\\/\[\]:;|=,+*?<>"\x00-\x1f]+')
_PS_SPECIAL = re.compile("([`$\"" + chr(0x201C) + chr(0x201D) + chr(0x201E) + "])")   # PowerShell's in "..."


def _profile_roots() -> tuple[tuple[str, str], ...]:
    """(variable, folder) for each of PROFILE_VARS this process has as an absolute folder below a drive."""
    roots = []
    for var in PROFILE_VARS:
        root = (os.environ.get(var) or "").rstrip("\\/")
        drive, rest = ntpath.splitdrive(root)
        if drive and rest.strip("\\/") and "\0" not in root:
            roots.append((var, root))
    return tuple(roots)


@functools.lru_cache(maxsize=8)
def _profile_pattern(roots: tuple[tuple[str, str], ...]) -> tuple[re.Pattern[str], dict[str, str]]:
    alts = [f"(?P<v{i}>{_SEP.join(re.escape(p) for p in re.split(_SEP, root))}){_WHOLE}"
            for i, (_, root) in enumerate(roots)]
    names = {f"v{i}": var for i, (var, _) in enumerate(roots)}
    alts.append(f"(?P<any>{_ANY_PROFILE})")
    names["any"] = "USERPROFILE"
    return re.compile("|".join(alts), re.IGNORECASE), names


def user_neutral(text: str) -> str:
    """text with each folder under a user's profile shown by its variable: %LOCALAPPDATA%\\lag-doctor\\..."""
    pattern, names = _profile_pattern(_profile_roots())
    return pattern.sub(lambda m: f"%{names[m.lastgroup]}%", text)


def powershell_path(path: Any) -> str:
    """path as a double-quoted Windows PowerShell argument that names a profile folder by its variable:
    "$env:LOCALAPPDATA\\lag-doctor\\...". The rest is escaped, so a $ or ` in a folder name stays itself."""
    pattern, names = _profile_pattern(_profile_roots())
    text, out, last = str(path), [], 0
    for m in pattern.finditer(text):
        out.append(_PS_SPECIAL.sub(r"`\1", text[last:m.start()]))
        var, after = names[m.lastgroup], text[m.end():m.end() + 1]
        out.append(f"$env:{var}" if after in ("", "\\", "/") else f"${{env:{var}}}")
        last = m.end()
    out.append(_PS_SPECIAL.sub(r"`\1", text[last:]))
    return '"' + "".join(out) + '"'


_TICKED = re.compile(r"`([^`\n]+)`")


def prose(value: Any) -> str:
    """Text the analysis wrote, escaped, with a `command` in backticks shown as code instead of as backticks."""
    return _TICKED.sub(r"<code>\1</code>", e(value))


def _fmt(v: Any, pattern: str = "{:.0f}", suffix: str = "") -> str:
    v = number(v)
    return "-" if v is None else pattern.format(v) + suffix


def _dict(v: Any) -> dict:
    return v if isinstance(v, dict) else {}


def _list(v: Any) -> list:
    return v if isinstance(v, list) else []


def _section(build: Callable[[], str]) -> str:
    """One part of the page; a part a damaged capture still breaks becomes a note, never a lost report (A15)."""
    try:
        return build()
    except Exception as exc:  # noqa: BLE001 - the rest of the report must still be written; this says what failed
        return f'<p class="muted">This part of the report could not be drawn from this capture ({e(type(exc).__name__)}).</p>'


CSS = """
:root{--bg:#f6f6f4;--fg:#1b1c1e;--muted:#5b6068;--card:#fff;--border:#dad9d4;--accent:#3b5bdb;
--bad:#c92a2a;--warn:#a85400;--ok:#2b8a3e;--c1:#3b5bdb;--c2:#c2255c;--c3:#2b8a3e;--c4:#a85400;--c5:#7048e8;
--band:rgba(201,42,42,.10);--grid:#e7e6e1;color-scheme:light dark}
@media (prefers-color-scheme: dark){:root{--bg:#131416;--fg:#e9e9e6;--muted:#a1a5ac;--card:#1c1e21;
--border:#2f3236;--accent:#91a7ff;--bad:#ff8787;--warn:#ffb050;--ok:#69db7c;--c1:#91a7ff;--c2:#f783ac;
--c3:#69db7c;--c4:#ffb050;--c5:#b197fc;--band:rgba(255,135,135,.14);--grid:#2a2d31}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,"Segoe UI",sans-serif}
main{max-width:1000px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:1.6rem;margin:0;line-height:1.25;overflow-wrap:anywhere}
h2{font-size:1.15rem;margin:34px 0 10px}
.meta,.muted{color:var(--muted)}
.meta{margin:4px 0 0}
.note{margin:8px 0 0;overflow-wrap:anywhere}
.verdict{background:var(--card);border:1px solid var(--border);border-left:6px solid var(--accent);
border-radius:10px;padding:16px 18px;margin-top:18px}
.verdict.none{border-left-color:var(--ok)}
.verdict.stutter,.verdict.low_fps,.verdict.mixed{border-left-color:var(--bad)}
.verdict.network,.verdict.pacing{border-left-color:var(--warn)}
.badge{font-size:.75rem;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.headline{font-size:1.15rem;margin:4px 0 0}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-top:16px}
.tile{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:10px 12px}
.tile .v{font-size:1.4rem;font-weight:650;font-variant-numeric:tabular-nums}
.tile .k{color:var(--muted);font-size:.85rem}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px 16px;margin:10px 0}
.card h3{margin:0;font-size:1.02rem}
.conf{font-size:.72rem;font-weight:700;border-radius:999px;padding:1px 8px;margin-left:8px;
border:1px solid currentColor;white-space:nowrap}
.conf.high{color:var(--bad)}.conf.medium{color:var(--warn)}.conf.low{color:var(--muted)}
.card ul{margin:8px 0 0;padding-left:20px}
.fix{margin-top:8px;padding:8px 10px;border-radius:8px;background:var(--bg)}
code{font-family:ui-monospace,Consolas,monospace;font-size:.88em;overflow-wrap:anywhere}
.chart{width:100%;height:auto;display:block;background:var(--card);border:1px solid var(--border);border-radius:10px}
.chart .grid{stroke:var(--grid);stroke-width:1}
.chart .tick{fill:var(--muted);font-size:12px}
.chart .frame{fill:none;stroke:var(--border)}
.chart .ln{fill:none;stroke-width:1.6;stroke-linejoin:round}
.chart .dot{stroke:var(--card);stroke-width:1}
.chart .vl{stroke-width:1.5;stroke-dasharray:4 3}
.chart .hl{stroke-width:1;stroke-dasharray:6 4}
.chart .band{fill:var(--band)}
.s1{stroke:var(--c1);fill:var(--c1)}.s2{stroke:var(--c2);fill:var(--c2)}.s3{stroke:var(--c3);fill:var(--c3)}
.s4{stroke:var(--c4);fill:var(--c4)}.s5{stroke:var(--c5);fill:var(--c5)}
.bad{stroke:var(--bad);fill:var(--bad)}.mark{stroke:var(--c5);fill:var(--c5)}.ref{stroke:var(--muted);fill:var(--muted)}
.drop{fill:var(--band);stroke:none}
.legend{display:flex;flex-wrap:wrap;gap:4px 16px;list-style:none;padding:0;margin:6px 0 0;color:var(--muted);font-size:.85rem}
.legend i{display:inline-block;width:14px;height:4px;border-radius:2px;vertical-align:middle;margin-right:6px}
.legend .s1 i{background:var(--c1)}.legend .s2 i{background:var(--c2)}.legend .s3 i{background:var(--c3)}
.legend .s4 i{background:var(--c4)}.legend .s5 i{background:var(--c5)}.legend .bad i{background:var(--bad)}
.legend .mark i{background:var(--c5)}.legend .ref i{background:var(--muted)}.legend .drop i{background:var(--band);height:10px}
.tablewrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:.92rem}
td,th{text-align:left;padding:6px 8px;border-bottom:1px solid var(--border);vertical-align:top}
th{color:var(--muted);font-weight:600}
footer{margin-top:40px;color:var(--muted);font-size:.85rem}
"""


def _legend(chart: Chart) -> str:
    items = "".join(f'<li class="{cls}"><i></i>{e(label)}</li>' for cls, label in chart.legend)
    return f'<ul class="legend">{items}</ul>' if items else ""


def _not_measured_note(cap: Capture, source: str) -> str:
    st = _dict(cap.sources).get(source)
    detail, state = getattr(st, "detail", None), getattr(st, "state", None)
    why = labels.reason_text(detail) if isinstance(detail, str) and detail.strip() else (state if st else "not recorded")
    return f'<p class="muted">Not measured: {prose(why)}</p>'


def _column(table: Any, name: str) -> list:
    return _list(_dict(table).get(name))


def _points(table: Any, column: str, scale: float = 1.0) -> tuple[list[float], list[float | None]]:
    """(times, values) of one column with every row whose time is not a sane number left out, and every value
    that is not one read as missing (None): a damaged capture can hold anything (A15)."""
    xs, ys = [], []
    for ti, vi in zip(_column(table, "t"), _column(table, column)):
        ti, vi = number(ti), number(vi)
        if ti is not None:
            xs.append(ti)
            ys.append(None if vi is None else vi * scale)
    return xs, ys


def _x1(cap: Capture, xs: list[float]) -> float:
    """The chart's right edge: the capture's length, or the last sample when that is later."""
    return max([v for v in (number(cap.duration_s), max(xs, default=None)) if v is not None] or [1.0])


def mark_times(d: Diagnosis, cap: Capture) -> list[float]:
    """The user's lag-key presses: the analysed ones (Diagnosis.stats["marks"]), else the capture's own list."""
    stats = _dict(d.stats)
    if isinstance(stats.get("marks"), list):
        return [t for t, _, _ in labels.marks(stats, d.events)]
    return [t for t in (number(m) for m in _list(cap.marks)) if t is not None]


def _draw_marks(ch: Chart, marks: list[float]) -> None:
    for m in marks:
        ch.vline(m, "mark", "you pressed the lag key")


# ------------------------------------------------------------------ sections --

def _tiles(d: Diagnosis) -> str:
    s = _dict(d.stats)
    bound = s.get("bound")
    tiles = [
        ("Average FPS", _fmt(s.get("avg_fps"))),
        ("1% low FPS", _fmt(s.get("low1_fps"))),
        ("Stutters / min", _fmt(s.get("stutters_per_min"), "{:.1f}")),
        ("Worst frame", _fmt(s.get("worst_frame_ms"), suffix=" ms")),
        ("Limited by", e(labels.BOUND_SHORT.get(bound, bound)) if isinstance(bound, str) and bound else "-"),
        ("Ping (internet)", _fmt(s.get("ping_inet_ms"), suffix=" ms")),
        ("Jitter", _fmt(s.get("jitter_ms"), "{:.1f}", " ms")),
        ("Packet loss", _fmt(s.get("loss_pct"), "{:.1f}", "%")),
    ]
    return '<section class="tiles">' + "".join(
        f'<div class="tile"><div class="v">{v}</div><div class="k">{e(k)}</div></div>' for k, v in tiles) + "</section>"


def _main_frames(d: Diagnosis, cap: Capture) -> tuple[list, list]:
    """Frame times of the swap chain the analysis used; a game's tiny side chains would only add noise."""
    t, ms = _column(cap.frames, F.T), _column(cap.frames, F.FRAME_MS)
    chain, chains = _dict(d.stats).get("main_chain"), _column(cap.frames, F.SWAP_CHAIN)
    if chain is not None and chains and len(chains) == len(t):
        keep = [i for i, c in enumerate(chains) if c == chain]
        t, ms = [t[i] for i in keep], [ms[i] if i < len(ms) else None for i in keep]
    xs, ys = [], []
    for ti, vi in zip(t, ms):
        ti, vi = number(ti), number(vi)
        if ti is not None:
            xs.append(ti)
            ys.append(vi if vi is not None and vi > 0 else None)
    return xs, ys


def _frame_chart(d: Diagnosis, cap: Capture) -> str:
    t, ms = _main_frames(d, cap)
    vals = [v for v in ms if v is not None]
    if not t or not vals:
        return _not_measured_note(cap, "frames")
    refresh = number(_dict(d.stats).get("refresh_hz"))
    ref_ms = 1000.0 / refresh if refresh and refresh > 0 else None
    top = min(max(vals) * 1.05, max(percentile(vals, 99.9) * 1.6, 40.0))
    top = max(top, (ref_ms or 0) * 2.2, 20.0)
    ch = Chart("Frame times: higher is slower; spikes are stutters", 0.0, _x1(cap, t), 0.0, top, " ms")
    for ev in d.events:
        if ev.kind == "fps_drop":
            ch.band(ev.t_start, ev.t_end, "drop", "FPS drop")
    if ref_ms:
        ch.hline(ref_ms, "ref", f"one screen refresh ({refresh:.0f} Hz = {ref_ms:.1f} ms)")
    ch.line(t, ms, "s1", "time to make each frame", agg="max")
    stutters = [(_dict(ev.detail).get("worst_t", ev.t_start), ev.severity) for ev in d.events if ev.kind == "stutter"]
    ch.dots(stutters, "bad", "stutter")
    _draw_marks(ch, mark_times(d, cap))
    note = ""
    worst = max(vals)
    if worst > top:
        clipped = sum(1 for v in vals if v > top)
        note = (f'<p class="muted">{clipped} frame{"s" if clipped != 1 else ""} took longer than {top:.0f} ms and '
                f'{"are" if clipped != 1 else "is"} cut off at the top; the worst took {worst:.0f} ms.</p>')
    return ch.svg() + _legend(ch) + note


def _series_chart(title: str, cap: Capture, table: dict, specs: list[tuple[str, str, str]], y1: float,
                  unit: str, source: str, scale: float = 1.0, ref: tuple[float, str] | None = None,
                  marks: list[float] = ()) -> str:
    lines = [(cls, label, *_points(table, col, scale)) for col, cls, label in specs]
    present = [(cls, label, xs, ys) for cls, label, xs, ys in lines if any(v is not None for v in ys)]
    if not present:
        return _not_measured_note(cap, source)
    ch = Chart(title, 0.0, _x1(cap, [x for _, _, xs, _ in present for x in xs[-1:]]), 0.0, y1, unit)
    if ref:
        ch.hline(ref[0], "ref", ref[1])
    for cls, label, xs, ys in present:
        ch.line(xs, ys, cls, label, agg="mean")
    _draw_marks(ch, marks)
    return ch.svg() + _legend(ch)


def _memory_chart(cap: Capture) -> str:
    total_mb = None
    luid = _dict(cap.target).get("adapter_luid")
    for a in _list(_dict(cap.inventory).get("adapters")):
        if isinstance(a, dict) and luid and a.get("luid") == luid and number(a.get("vram_mb")):
            total_mb = number(a["vram_mb"])
    used = [v for c in (S.VRAM_USED_MB, S.GAME_SHARED_MB) for v in _points(cap.system, c)[1] if v is not None]
    top_mb = max([total_mb or 0.0] + used + [1024.0]) * 1.1
    ref = (total_mb / 1024, f"video memory size ({total_mb / 1024:.1f} GB)") if total_mb and total_mb > 0 else None
    return _series_chart("Video memory (VRAM)", cap, cap.system,
                         [(S.VRAM_USED_MB, "s1", "VRAM in use (all programs)"),
                          (S.GAME_SHARED_MB, "s2", "game data spilled into system RAM")],
                         top_mb / 1024, " GB", "system", scale=1 / 1024, ref=ref)


def _network_chart(cap: Capture, marks: list[float] = ()) -> str:
    cols = [(N.GW_RTT_MS, "s1", "ping to your router"), (N.INET_RTT_MS, "s2", "ping to the internet"),
            (N.SERVER_RTT_MS, "s3", "ping to the game server")]
    lines = [(cls, label, *_points(cap.network, col)) for col, cls, label in cols]
    vals = [v for _, _, _, ys in lines for v in ys if v is not None]
    if not vals:
        return _not_measured_note(cap, "network")
    top = max(50.0, percentile(vals, 99) * 1.4)
    t, inet = _points(cap.network, N.INET_RTT_MS)
    ch = Chart("Network round-trip time", 0.0, _x1(cap, t[-1:]), 0.0, top, " ms")
    for cls, label, xs, ys in lines:
        if any(v is not None for v in ys):
            ch.line(xs, ys, cls, label, agg="max")
    listed = _dict(_dict(cap.net_flows).get("lost")).get("internet")
    if isinstance(listed, list):
        # The network collector's own record: a probe that got no reply. A None RTT can also mean
        # "not sent this round" (no target yet, no free worker), which is not a loss.
        lost = [(x, top) for x in listed if number(x) is not None]
    elif any(v is not None for v in inet):
        lost = [(x, top) for x, v in zip(t, inet) if v is None]     # an older capture without the record
    else:
        lost = []
    ch.dots(lost, "bad", "lost ping (internet)")
    _draw_marks(ch, marks)
    return ch.svg() + _legend(ch)


def _findings(d: Diagnosis) -> str:
    if not d.findings:
        return '<p class="muted">Nothing stood out. If the game felt laggy, record again and press the lag key ' \
               'the moment it happens.</p>'
    out = []
    for i, f in enumerate(d.findings, 1):
        ev = "".join(f"<li>{prose(x.text)}</li>" for x in f.evidence)
        fixes = []
        for fx in f.fixes:
            restart = {"game": " Restart the game afterwards.", "reboot": " Restart the PC afterwards."}.get(
                fx.needs_restart or "", "")
            admin = " Needs admin rights." if fx.needs_admin else ""
            if fx.fix_id:
                fixes.append(f'<div class="fix"><strong>Fix it:</strong> {prose(fx.title)}. '
                             f'Run <code>lagdoctor fix {e(fx.fix_id)}</code>.{e(restart)}{e(admin)}'
                             f'{" " + prose(fx.detail) if fx.detail else ""} '
                             f'<span class="muted">It asks before changing anything, and '
                             f'<code>lagdoctor undo</code> puts it back.</span></div>')
            else:
                fixes.append(f'<div class="fix"><strong>Try:</strong> {prose(fx.title)}'
                             f'{": " + prose(fx.detail) if fx.detail else ""}{e(restart)}</div>')
        conf = labels.CONFIDENCE.get(f.confidence, f.confidence)
        out.append(f'<div class="card"><h3>{i}. {prose(f.title)}<span class="conf {e(f.confidence)}">{e(conf)}'
                   f'</span></h3><p>{prose(f.summary)}</p>{"<ul>" + ev + "</ul>" if ev else ""}{"".join(fixes)}</div>')
    return "".join(out)


def _ran(d: Diagnosis, *sources: str) -> bool:
    """Whether any of these sources was measured (Diagnosis.measured: "ok" or "partial: ...")."""
    return any(str(_dict(d.measured).get(s, "")).startswith(("ok", "partial")) for s in sources)


def _no_events(d: Diagnosis) -> str:
    """What an empty list of worst moments means: "No lag events were found" only where something looked for them."""
    frames, network = _ran(d, "frames"), _ran(d, "network", "net_flows")
    if frames:
        return "No lag events were found."
    if network:
        return ("No network lag was found. Frame timing was not measured, so stutters and frame-rate drops could not "
                "be looked for.")
    return "No lag events could be looked for: frame timing and the network were not measured."


def _frames_text(d: Diagnosis) -> str:
    """The frames the analysis had, for the header: "0 frames" once stood for frame timing that never ran."""
    return f"{_fmt(_dict(d.stats).get('frames'))} frames" if _ran(d, "frames") else "frame timing not recorded"


def _events(d: Diagnosis) -> str:
    timed = [ev for ev in d.events if ev.kind != "mark" and number(ev.t_start) is not None]
    worst = sorted(timed, key=lambda ev: -(number(ev.severity) or 0.0))[:15]
    if not worst:
        return f'<p class="muted">{e(_no_events(d))}</p>'
    rows = "".join(
        f"<tr><td>{fmt_elapsed(ev.t_start)}</td><td>{e(labels.EVENT_KINDS.get(ev.kind, ev.kind))}</td>"
        f"<td>{e(labels.severity_text(ev.kind, ev.severity, ev.detail))}</td>"
        f"<td>{e(', '.join(labels.cause_text(c) for c in ev.causes) or 'no clear cause')}</td></tr>"
        for ev in sorted(worst, key=lambda ev: number(ev.t_start)))
    return ('<div class="tablewrap"><table><thead><tr><th>When</th><th>What</th><th>How bad</th>'
            f'<th>Likely cause</th></tr></thead><tbody>{rows}</tbody></table></div>')


def _marks(d: Diagnosis) -> str:
    """CC-5: the moments the user marked with the lag key, and what was found at each (LC 2.4, 5.2)."""
    rows = labels.marks(d.stats, d.events)
    if not rows:
        return ""
    frames_ok = str(_dict(d.measured).get("frames", "")).startswith(("ok", "partial"))
    body = []
    for t, found, causes in rows:
        if found:
            what = "; ".join(labels.event_text(ev) for ev in found)
        elif causes:
            what = "No lag event; meanwhile " + ", ".join(labels.cause_text(c) for c in causes)
        else:
            what = "Nothing in what was measured"
        body.append(f"<tr><td>{fmt_elapsed(t)}</td><td>{e(what)}</td></tr>")
    return (f"<h2>Moments you marked</h2><p>{e(labels.marks_summary(rows, frames_ok))}</p>"
            '<div class="tablewrap"><table><thead><tr><th>When</th><th>What was found</th></tr></thead>'
            f"<tbody>{''.join(body)}</tbody></table></div>")


def _yes_no(v: Any) -> str:
    return "unknown" if v is None else ("on" if v else "off")


def _count(v: Any) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0


def _files_text(files: list, more: int, folders: bool = False) -> str:
    items = [labels.file_text(f["name"], f.get("bytes"), f.get("modified"))
             + (f" in {ntpath.dirname(f['path'])}" if folders and isinstance(f.get("path"), str) else "")
             for f in files if isinstance(f, dict) and isinstance(f.get("name"), str)]
    return "; ".join(items) + (f"; and {more} more" if more else "")


def _game_files_rows(game: dict, errors: dict) -> list[tuple[str, str]]:
    """What lay beside the game when the recording started (capture/gamefiles.py): none for a capture from before
    lag-doctor looked."""
    if "drop_ins" not in game:
        return []

    def not_read(section: str) -> str:
        why = labels.error_text(errors.get(f"game.{section}")) or (None if game.get("exe") else labels.NO_EXE)
        return f"not read: {why}" if why else "not read"

    rows = []
    drop, ue = game.get("drop_ins"), game.get("unreal")
    ue_found = isinstance(ue, dict) and ue.get("found") is True
    if isinstance(drop, dict):
        files = _list(drop.get("files")) + (_list(ue.get("mods")) if ue_found else [])
        more = _count(drop.get("more")) + (_count(ue.get("mods_more")) if ue_found else 0)
        rows.append(("Drop-in files", _files_text(files, more) if files or more else
                     "none found beside the game's exe" + (" or in its ~mods folder" if ue_found else "")))
    else:
        rows.append(("Drop-in files", not_read("drop_ins")))
    if ue_found:
        config, more = _list(ue.get("config")), _count(ue.get("config_more"))
        looked = ", ".join(x for x in _list(ue.get("looked_in")) if isinstance(x, str))
        rows.append(("Unreal settings files", _files_text(config, more, folders=True) if config or more else
                     f"none found in the Saved\\Config folders of {looked}"))
    elif ue is None:
        rows.append(("Unreal settings files", not_read("unreal")))
    return rows


def _pc(cap: Capture) -> str:
    inv = _dict(cap.inventory)
    rows: list[tuple[str, str]] = []
    os_ = _dict(inv.get("os"))
    if os_:
        rows.append(("Windows", f"{os_.get('name', '')} (build {os_.get('build', '?')})"))
    cpu = _dict(inv.get("cpu"))
    if cpu:
        rows.append(("CPU", f"{cpu.get('name', '?')}, {cpu.get('cores', '?')} cores / "
                            f"{cpu.get('threads', '?')} threads"))
    if number(inv.get("ram_gb")):
        rows.append(("Memory", f"{_fmt(inv['ram_gb'])} GB"))
    game_luid = _dict(cap.target).get("adapter_luid")
    for a in (x for x in _list(inv.get("adapters")) if isinstance(x, dict)):
        kind = "integrated" if a.get("integrated") else "dedicated"
        used = " (the game used this one)" if a.get("luid") and a.get("luid") == game_luid else ""
        rows.append(("GPU", f"{a.get('name', '?')}, {kind}, {_fmt(a.get('vram_mb'), suffix=' MB')}{used}"))
    for dsp in (x for x in _list(inv.get("displays")) if isinstance(x, dict)):
        rows.append(("Display", f"{dsp.get('name') or dsp.get('device', '?')}: {dsp.get('width', '?')}x"
                                f"{dsp.get('height', '?')} at {_fmt(dsp.get('hz'))} Hz "
                                f"(max {_fmt(dsp.get('max_hz'))} Hz)"))
    pw = _dict(inv.get("power"))
    if pw:
        on_battery = pw.get("on_battery")
        battery = ("on battery" if on_battery is True else "plugged in" if on_battery is False
                   else "power source unknown")
        rows.append(("Power", f"plan {pw.get('scheme_name') or '?'}; {labels.power_mode_text(pw)}; {battery}"))
    st = _dict(inv.get("settings"))
    if st:
        rows.append(("Settings", f"Game Mode {_yes_no(st.get('game_mode'))}; background recording "
                                 f"{_yes_no(st.get('background_recording'))}; GPU scheduling "
                                 f"{_yes_no(st.get('hags'))}; windowed-game optimisations "
                                 f"{_yes_no(st.get('windowed_optimizations'))}; memory integrity "
                                 f"{_yes_no(st.get('memory_integrity'))}"))
    game = _dict(inv.get("game"))
    if game:
        date = labels.date_text(game.get("exe_modified"))
        dated = [x for x in (labels.size_text(game.get("exe_bytes")), f"dated {date}" if date else None) if x]
        rows.append(("Game file", f"{game.get('exe') or '?'} on {game.get('drive') or '?'} "
                                  f"({game.get('media') or 'unknown'} drive)"
                                  + (f", {', '.join(dated)}" if dated else "")))
        rows.extend(_game_files_rows(game, _dict(inv.get("errors"))))
    net = _dict(inv.get("network"))
    if net:
        rows.append(("Network", f"{net.get('kind') or '?'} {net.get('name') or ''}".strip()))
    if not rows:
        return '<p class="muted">No system details were recorded.</p>'
    body = "".join(f"<tr><th>{e(k)}</th><td>{e(v)}</td></tr>" for k, v in rows)
    return f'<div class="tablewrap"><table><tbody>{body}</tbody></table></div>'


def _cleared(d: Diagnosis) -> str:
    if not d.cleared:
        return ""
    items = "".join(f"<li><strong>{e(labels.topic_text(c))}</strong>: {prose(why)}</li>"
                    for c, why in sorted(d.cleared.items(), key=lambda kv: labels.topic_text(kv[0]).casefold()))
    return ("<h2>Checked and fine</h2><p class=\"muted\">These were measured and ruled out:</p>"
            f"<ul>{items}</ul>")


def _not_measured(d: Diagnosis, cap: Capture) -> str:
    items = [f"<li><strong>{e(name)}</strong>: {prose(why)}</li>"
             for name, why in labels.not_measured(cap.sources, d.measured)]
    unchecked = "".join(f"<li><strong>{e(', '.join(topics))}</strong>: {prose(why)}</li>"
                        for why, topics in labels.group_unchecked(d.unchecked))
    if not items and not unchecked:
        return ""
    out = "<h2>Not measured</h2>"
    if items:
        out += ("<p class=\"muted\">These were not measured, so they are not ruled out:</p>"
                f"<ul>{''.join(items)}</ul>")
    if unchecked:
        out += f"<p class=\"muted\">Causes that could not be checked:</p><ul>{unchecked}</ul>"
    return out


def _cpu_gpu_chart(d: Diagnosis, cap: Capture) -> str:
    return _series_chart(
        "CPU and GPU load", cap, cap.system,
        [(S.GAME_GPU_PCT, "s1", "GPU used by the game"), (S.GAME_TOP_THREAD_PCT, "s2", "game's busiest thread"),
         (S.CPU_TOTAL_PCT, "s3", "whole CPU"), (S.GPU_OTHER_PCT, "s4", "GPU used by other programs")],
        100.0, "%", "system", marks=mark_times(d, cap))


def _head(d: Diagnosis, cap: Capture, game: str) -> str:
    try:
        when = datetime.fromisoformat(cap.started_at).strftime("%d %b %Y %H:%M")
    except (TypeError, ValueError):
        when = cap.started_at
    lag = labels.LAG_TYPES.get(d.lag_type, d.lag_type) if isinstance(d.lag_type, str) else d.lag_type
    exe = labels.game_name({"name": _dict(cap.target).get("name")})
    exe = f"{e(exe)} &middot; " if exe not in (game, "your game") else ""   # the file, when the title is the name
    # The player's own words (watch --note), through e() like every string: a note can name a folder of theirs.
    note = clean_note(cap.note)
    note = f'<p class="note"><strong>Your note:</strong> {e(note)}</p>' if note else ""
    return (f"<header><h1>Lag report: {e(game)}</h1><p class=\"meta\">{exe}{e(when)} &middot; "
            f"{fmt_elapsed(cap.duration_s)} recorded &middot; {e(_frames_text(d))}</p>{note}</header>"
            f'<section class="verdict {e(d.lag_type)}"><div class="badge">{e(lag)}</div>'
            f'<p class="headline">{prose(d.headline)}</p></section>')


def render(d: Diagnosis, cap: Capture, capture_path: Path | None = None) -> str:
    game = labels.game_name(cap.target)
    footer = f"lag-doctor {e(__version__)}"
    if capture_path:
        footer += (f" &middot; capture saved at <code>{e(capture_path)}</code> &middot; re-run the analysis in "
                   f"PowerShell with <code>lagdoctor analyze {e(powershell_path(capture_path))}</code>")
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>Lag report: {e(game)}</title><style>{CSS}</style></head><body><main>"
        f"{_section(lambda: _head(d, cap, game))}"
        f"{_section(lambda: _tiles(d))}"
        f"<h2>Frame times</h2>{_section(lambda: _frame_chart(d, cap))}"
        f"<h2>What is causing it</h2>{_section(lambda: _findings(d))}"
        f"<h2>CPU and GPU</h2>{_section(lambda: _cpu_gpu_chart(d, cap))}"
        f"<h2>Video memory</h2>{_section(lambda: _memory_chart(cap))}"
        f"<h2>Network</h2>{_section(lambda: _network_chart(cap, mark_times(d, cap)))}"
        f"<h2>Worst moments</h2>{_section(lambda: _events(d))}"
        f"{_section(lambda: _marks(d))}"
        f"<h2>This PC</h2>{_section(lambda: _pc(cap))}"
        f"{_section(lambda: _cleared(d))}"
        f"{_section(lambda: _not_measured(d, cap))}"
        f"<footer>{footer}</footer></main></body></html>")


def write(d: Diagnosis, cap: Capture, path: Path, capture_path: Path | None = None) -> Path:
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(render(d, cap, capture_path), encoding="utf-8")
    os.replace(tmp, path)
    return path
