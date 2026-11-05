"""The HTML report: one self-contained file (inline CSS and SVG, no scripts, no network).

Every string that came from outside the tool (game and process names, window
titles, adapter names, evidence text) is escaped: a process can be named
"<script>...", and the report must still be a harmless page.
"""
from __future__ import annotations

import os
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any

from lagdoctor import __version__
from lagdoctor.analysis.stats import clean, percentile
from lagdoctor.model import Capture, Diagnosis, F, N, S
from lagdoctor.report import labels
from lagdoctor.report.charts import Chart, fmt_clock


def e(value: Any) -> str:
    return escape("" if value is None else str(value), quote=True)


def _fmt(v: Any, pattern: str = "{:.0f}", suffix: str = "") -> str:
    if v is None:
        return "-"
    try:
        return pattern.format(v) + suffix
    except (TypeError, ValueError):
        return e(v)


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
    st = cap.sources.get(source)
    why = st.detail if st and st.detail else (st.state if st else "not recorded")
    return f'<p class="muted">Not measured: {e(why)}</p>'


# ------------------------------------------------------------------ sections --

def _tiles(d: Diagnosis) -> str:
    s = d.stats
    bound = s.get("bound")
    tiles = [
        ("Average FPS", _fmt(s.get("avg_fps"))),
        ("1% low FPS", _fmt(s.get("low1_fps"))),
        ("Stutters / min", _fmt(s.get("stutters_per_min"), "{:.1f}")),
        ("Worst frame", _fmt(s.get("worst_frame_ms"), suffix=" ms")),
        ("Limited by", e(labels.BOUND_SHORT.get(bound, bound)) if bound else "-"),
        ("Ping (internet)", _fmt(s.get("ping_inet_ms"), suffix=" ms")),
        ("Jitter", _fmt(s.get("jitter_ms"), "{:.1f}", " ms")),
        ("Packet loss", _fmt(s.get("loss_pct"), "{:.1f}", "%")),
    ]
    return '<section class="tiles">' + "".join(
        f'<div class="tile"><div class="v">{v}</div><div class="k">{e(k)}</div></div>' for k, v in tiles) + "</section>"


def _main_frames(d: Diagnosis, cap: Capture) -> tuple[list, list]:
    """Frame times of the swap chain the analysis used; a game's tiny side chains would only add noise."""
    t, ms = cap.frames.get(F.T) or [], cap.frames.get(F.FRAME_MS) or []
    chain, chains = d.stats.get("main_chain"), cap.frames.get(F.SWAP_CHAIN)
    if chain is not None and chains and len(chains) == len(t):
        keep = [i for i, c in enumerate(chains) if c == chain]
        t, ms = [t[i] for i in keep], [ms[i] for i in keep]
    return t, ms


def _frame_chart(d: Diagnosis, cap: Capture) -> str:
    t, ms = _main_frames(d, cap)
    vals = clean(ms)
    if not t or not vals:
        return _not_measured_note(cap, "frames")
    refresh = d.stats.get("refresh_hz")
    ref_ms = 1000.0 / refresh if refresh else None
    top = min(max(vals) * 1.05, max(percentile(vals, 99.9) * 1.6, 40.0))
    top = max(top, (ref_ms or 0) * 2.2, 20.0)
    ch = Chart("Frame times: higher is slower; spikes are stutters", 0.0, max(cap.duration_s, t[-1]), 0.0, top, " ms")
    for ev in d.events:
        if ev.kind == "fps_drop":
            ch.band(ev.t_start, ev.t_end, "drop", "FPS drop")
    if ref_ms:
        ch.hline(ref_ms, "ref", f"one screen refresh ({refresh:.0f} Hz = {ref_ms:.1f} ms)")
    ch.line(t, ms, "s1", "time to make each frame", agg="max")
    stutters = [(ev.detail.get("worst_t", ev.t_start), ev.severity) for ev in d.events if ev.kind == "stutter"]
    ch.dots(stutters, "bad", "stutter")
    for m in cap.marks:
        ch.vline(m, "mark", "you pressed the lag key")
    note = ""
    worst = max(vals)
    if worst > top:
        clipped = sum(1 for v in vals if v > top)
        note = (f'<p class="muted">{clipped} frame{"s" if clipped != 1 else ""} took longer than {top:.0f} ms and '
                f'{"are" if clipped != 1 else "is"} cut off at the top; the worst took {worst:.0f} ms.</p>')
    return ch.svg() + _legend(ch) + note


def _series_chart(title: str, cap: Capture, table: dict, specs: list[tuple[str, str, str]], y1: float,
                  unit: str, source: str, scale: float = 1.0, ref: tuple[float, str] | None = None) -> str:
    t = table.get("t") or []
    present = [(col, cls, label) for col, cls, label in specs if any(v is not None for v in table.get(col) or [])]
    if not t or not present:
        return _not_measured_note(cap, source)
    ch = Chart(title, 0.0, max(cap.duration_s, t[-1]), 0.0, y1, unit)
    if ref:
        ch.hline(ref[0], "ref", ref[1])
    for col, cls, label in present:
        ys = [None if v is None else v * scale for v in table[col]]
        ch.line(t, ys, cls, label, agg="mean")
    return ch.svg() + _legend(ch)


def _memory_chart(cap: Capture) -> str:
    total_mb = None
    luid = cap.target.get("adapter_luid")
    for a in cap.inventory.get("adapters") or []:
        if a.get("luid") == luid and a.get("vram_mb"):
            total_mb = a["vram_mb"]
    used = clean(cap.system.get(S.VRAM_USED_MB) or [])
    top_mb = max([total_mb or 0] + used + clean(cap.system.get(S.GAME_SHARED_MB) or []) + [1024]) * 1.1
    ref = (total_mb / 1024, f"video memory size ({total_mb / 1024:.1f} GB)") if total_mb else None
    return _series_chart("Video memory (VRAM)", cap, cap.system,
                         [(S.VRAM_USED_MB, "s1", "VRAM in use (all programs)"),
                          (S.GAME_SHARED_MB, "s2", "game data spilled into system RAM")],
                         top_mb / 1024, " GB", "system", scale=1 / 1024, ref=ref)


def _network_chart(cap: Capture) -> str:
    net = cap.network
    t = net.get("t") or []
    cols = [(N.GW_RTT_MS, "s1", "ping to your router"), (N.INET_RTT_MS, "s2", "ping to the internet"),
            (N.SERVER_RTT_MS, "s3", "ping to the game server")]
    vals = [v for c, _, _ in cols for v in clean(net.get(c) or [])]
    if not t or not vals:
        return _not_measured_note(cap, "network")
    top = max(50.0, percentile(vals, 99) * 1.4)
    ch = Chart("Network round-trip time", 0.0, max(cap.duration_s, t[-1]), 0.0, top, " ms")
    for col, cls, label in cols:
        if any(v is not None for v in net.get(col) or []):
            ch.line(t, net[col], cls, label, agg="max")
    inet = net.get(N.INET_RTT_MS) or []
    listed = (cap.net_flows.get("lost") or {}).get("internet") if isinstance(cap.net_flows, dict) else None
    if isinstance(listed, list):
        # The network collector's own record: a probe that got no reply. A None RTT can also mean
        # "not sent this round" (no target yet, no free worker), which is not a loss.
        lost = [(x, top) for x in listed if isinstance(x, (int, float)) and not isinstance(x, bool)]
    elif any(v is not None for v in inet):
        lost = [(x, top) for x, v in zip(t, inet) if v is None]     # an older capture without the record
    else:
        lost = []
    ch.dots(lost, "bad", "lost ping (internet)")
    return ch.svg() + _legend(ch)


def _findings(d: Diagnosis) -> str:
    if not d.findings:
        return '<p class="muted">Nothing stood out. If the game felt laggy, record again and press the lag key ' \
               'the moment it happens.</p>'
    out = []
    for i, f in enumerate(d.findings, 1):
        ev = "".join(f"<li>{e(x.text)}</li>" for x in f.evidence)
        fixes = []
        for fx in f.fixes:
            restart = {"game": " Restart the game afterwards.", "reboot": " Restart the PC afterwards."}.get(
                fx.needs_restart or "", "")
            admin = " Needs admin rights." if fx.needs_admin else ""
            if fx.fix_id:
                fixes.append(f'<div class="fix"><strong>Fix it:</strong> {e(fx.title)}. '
                             f'Run <code>lagdoctor fix {e(fx.fix_id)}</code>.{e(restart)}{e(admin)}'
                             f'{" " + e(fx.detail) if fx.detail else ""} '
                             f'<span class="muted">It asks before changing anything, and '
                             f'<code>lagdoctor undo</code> puts it back.</span></div>')
            else:
                fixes.append(f'<div class="fix"><strong>Try:</strong> {e(fx.title)}'
                             f'{": " + e(fx.detail) if fx.detail else ""}{e(restart)}</div>')
        conf = labels.CONFIDENCE.get(f.confidence, f.confidence)
        out.append(f'<div class="card"><h3>{i}. {e(f.title)}<span class="conf {e(f.confidence)}">{e(conf)}'
                   f'</span></h3><p>{e(f.summary)}</p>{"<ul>" + ev + "</ul>" if ev else ""}{"".join(fixes)}</div>')
    return "".join(out)


def _events(d: Diagnosis) -> str:
    worst = sorted((ev for ev in d.events if ev.kind != "mark"), key=lambda ev: -ev.severity)[:15]
    if not worst:
        return '<p class="muted">No lag events were found.</p>'
    rows = "".join(
        f"<tr><td>{fmt_clock(ev.t_start)}</td><td>{e(labels.EVENT_KINDS.get(ev.kind, ev.kind))}</td>"
        f"<td>{e(labels.severity_text(ev.kind, ev.severity))}</td>"
        f"<td>{e(', '.join(labels.cause_text(c) for c in ev.causes) or 'no clear cause')}</td></tr>"
        for ev in sorted(worst, key=lambda ev: ev.t_start))
    return ('<div class="tablewrap"><table><thead><tr><th>When</th><th>What</th><th>How bad</th>'
            f'<th>Likely cause</th></tr></thead><tbody>{rows}</tbody></table></div>')


def _yes_no(v: Any) -> str:
    return "unknown" if v is None else ("on" if v else "off")


def _pc(cap: Capture) -> str:
    inv = cap.inventory
    rows: list[tuple[str, str]] = []
    os_ = inv.get("os") or {}
    if os_:
        rows.append(("Windows", f"{os_.get('name', '')} (build {os_.get('build', '?')})"))
    cpu = inv.get("cpu") or {}
    if cpu:
        rows.append(("CPU", f"{cpu.get('name', '?')}, {cpu.get('cores', '?')} cores / "
                            f"{cpu.get('threads', '?')} threads"))
    if inv.get("ram_gb"):
        rows.append(("Memory", f"{inv['ram_gb']:.0f} GB"))
    for a in inv.get("adapters") or []:
        kind = "integrated" if a.get("integrated") else "dedicated"
        used = " (the game used this one)" if a.get("luid") and a.get("luid") == cap.target.get("adapter_luid") else ""
        rows.append(("GPU", f"{a.get('name', '?')}, {kind}, {_fmt(a.get('vram_mb'), suffix=' MB')}{used}"))
    for dsp in inv.get("displays") or []:
        rows.append(("Display", f"{dsp.get('name') or dsp.get('device', '?')}: {dsp.get('width', '?')}x"
                                f"{dsp.get('height', '?')} at {_fmt(dsp.get('hz'))} Hz "
                                f"(max {_fmt(dsp.get('max_hz'))} Hz)"))
    pw = inv.get("power") or {}
    if pw:
        mode = (pw.get("slider") or "?").replace("_", " ")
        battery = {True: "on battery", False: "plugged in"}.get(pw.get("on_battery"), "power source unknown")
        rows.append(("Power", f"plan {pw.get('scheme_name') or '?'}; power mode {mode}; {battery}"))
    st = inv.get("settings") or {}
    if st:
        rows.append(("Settings", f"Game Mode {_yes_no(st.get('game_mode'))}; background recording "
                                 f"{_yes_no(st.get('background_recording'))}; GPU scheduling "
                                 f"{_yes_no(st.get('hags'))}; windowed-game optimisations "
                                 f"{_yes_no(st.get('windowed_optimizations'))}; memory integrity "
                                 f"{_yes_no(st.get('memory_integrity'))}"))
    game = inv.get("game") or {}
    if game:
        rows.append(("Game file", f"{game.get('exe') or '?'} on {game.get('drive') or '?'} "
                                  f"({game.get('media') or 'unknown'} drive)"))
    net = inv.get("network") or {}
    if net:
        rows.append(("Network", f"{net.get('kind') or '?'} {net.get('name') or ''}".strip()))
    if not rows:
        return '<p class="muted">No system details were recorded.</p>'
    body = "".join(f"<tr><th>{e(k)}</th><td>{e(v)}</td></tr>" for k, v in rows)
    return f'<div class="tablewrap"><table><tbody>{body}</tbody></table></div>'


def _cleared(d: Diagnosis) -> str:
    if not d.cleared:
        return ""
    items = "".join(f"<li><strong>{e(labels.topic_text(c))}</strong>: {e(why)}</li>"
                    for c, why in sorted(d.cleared.items(), key=lambda kv: labels.topic_text(kv[0])))
    return ("<h2>Checked and fine</h2><p class=\"muted\">These were measured and ruled out:</p>"
            f"<ul>{items}</ul>")


def _not_measured(d: Diagnosis, cap: Capture) -> str:
    items = []
    for src, st in sorted(cap.sources.items()):
        if st.state != "ok":
            items.append(f"<li><strong>{e(src)}</strong>: {e(st.detail or st.state)}</li>")
    for what, why in sorted(d.measured.items()):
        if why != "ok" and what not in cap.sources:
            items.append(f"<li><strong>{e(what)}</strong>: {e(why)}</li>")
    unchecked = "".join(f"<li><strong>{e(', '.join(topics))}</strong>: {e(why)}</li>"
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


def render(d: Diagnosis, cap: Capture, capture_path: Path | None = None) -> str:
    game = cap.target.get("name") or "your game"
    try:
        when = datetime.fromisoformat(cap.started_at).strftime("%d %b %Y %H:%M")
    except ValueError:
        when = cap.started_at
    lag = labels.LAG_TYPES.get(d.lag_type, d.lag_type)
    gpu_chart = _series_chart(
        "CPU and GPU load", cap, cap.system,
        [(S.GAME_GPU_PCT, "s1", "GPU used by the game"), (S.GAME_TOP_THREAD_PCT, "s2", "game's busiest thread"),
         (S.CPU_TOTAL_PCT, "s3", "whole CPU"), (S.GPU_OTHER_PCT, "s4", "GPU used by other programs")],
        100.0, "%", "system")
    footer = f"lag-doctor {e(__version__)}"
    if capture_path:
        footer += (f" &middot; capture saved at <code>{e(capture_path)}</code> &middot; re-run the analysis with "
                   f"<code>lagdoctor analyze \"{e(capture_path)}\"</code>")
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>Lag report: {e(game)}</title><style>{CSS}</style></head><body><main>"
        f"<header><h1>Lag report: {e(game)}</h1><p class=\"meta\">{e(when)} &middot; "
        f"{fmt_clock(cap.duration_s)} recorded &middot; {_fmt(d.stats.get('frames'))} frames</p></header>"
        f'<section class="verdict {e(d.lag_type)}"><div class="badge">{e(lag)}</div>'
        f'<p class="headline">{e(d.headline)}</p></section>'
        f"{_tiles(d)}"
        f"<h2>Frame times</h2>{_frame_chart(d, cap)}"
        f"<h2>What is causing it</h2>{_findings(d)}"
        f"<h2>CPU and GPU</h2>{gpu_chart}"
        f"<h2>Video memory</h2>{_memory_chart(cap)}"
        f"<h2>Network</h2>{_network_chart(cap)}"
        f"<h2>Worst moments</h2>{_events(d)}"
        f"<h2>This PC</h2>{_pc(cap)}"
        f"{_cleared(d)}"
        f"{_not_measured(d, cap)}"
        f"<footer>{footer}</footer></main></body></html>")


def write(d: Diagnosis, cap: Capture, path: Path, capture_path: Path | None = None) -> Path:
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(render(d, cap, capture_path), encoding="utf-8")
    os.replace(tmp, path)
    return path
