"""Drive lag-doctor's real window through the host's own script hook (ExecuteScript), in a child process.

tests/test_app_page.py runs this on a demo library (tests/app_demo.py) and reads what it wrote. The window opens
without taking the focus and closes itself; nothing is typed or clicked outside the page: each step runs script in the
page (a click is the page's own element.click()) and waits for the page to show the result. WebDriver for WebView2
has been broken since runtime 133 (research note, section 3), so the app's own hook is the driver.

    python -m tests.app_harness <home> <out.json> [<shots folder>]    # LAGDOCTOR_HOME must name <home> too

With a shots folder, each screen is also saved as a PNG there, through WebView2's own CapturePreview: what the page
drew, without the window's frame (the README's pictures come from this, on the demo library).
"""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
import types
from pathlib import Path

from lagdoctor.app import library, recording
from lagdoctor.app.main import App
from lagdoctor.stores import exe as exe_mod
from lagdoctor.winapi import webview2 as wv
from tests import app_demo, fake_fixes, fake_recorder, pe_build

STEP_TIMEOUT_S = 30.0
RUN_TIMEOUT_S = 150.0
POLL_MS = 100
# Test-only: DevTools' Page.crash ends the page's renderer, as a crash would (WebView2.h 1.0.4314.35:
# ICoreWebView2::CallDevToolsProtocolMethod is slot 36; its completion handler's IID below). The app never calls it.
CALL_DEVTOOLS_SLOT = 36
DEVTOOLS_DONE = "ICoreWebView2CallDevToolsProtocolMethodCompletedHandler"
wv.IID.setdefault(DEVTOOLS_DONE, wv.guid("5C4889F0-5EF6-4C5A-952C-D8F1B92D0574"))
# Test-only too: ICoreWebView2::CapturePreview is slot 30, PNG is format 0, and its handler's Invoke takes one HRESULT.
CAPTURE_PREVIEW_SLOT = 30
SHOT_DONE = "ICoreWebView2CapturePreviewCompletedHandler"
wv.IID.setdefault(SHOT_DONE, wv.guid("697E05E9-3D8F-45FA-96F4-8FFE1EDEDAF5"))
DONE_HR = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_long)
STGM_WRITE, STGM_SHARE_DENY_WRITE, STGM_CREATE, FILE_ATTRIBUTE_NORMAL = 0x1, 0x20, 0x1000, 0x80
_shlwapi = ctypes.WinDLL("shlwapi")
_shlwapi.SHCreateStreamOnFileEx.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int,
                                            ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
_shlwapi.SHCreateStreamOnFileEx.restype = ctypes.c_long


def js(code: str) -> tuple:
    return ("js", code)


def until(code: str, seconds: float = STEP_TIMEOUT_S) -> tuple:
    return ("until", code, seconds)


def sleep(ms: int) -> tuple:
    return ("sleep", ms)


def shot(name: str) -> tuple:
    """Save what the page shows now as <shots>/<name>.png (nothing without a shots folder)."""
    return ("shot", name)


def until_ready(n: int, seconds: float = STEP_TIMEOUT_S) -> tuple:
    """Wait until the page has said it is ready n times (each load of it says so once)."""
    return ("ready", n, seconds)


def activate(on: bool) -> tuple:
    """Tell the window it is (or is no longer) the active one, through its own WM_ACTIVATE branch, which the
    harness calls itself: posted, the message would also reach DefWindowProc, which gives the window the focus."""
    return ("activate", on)


def wait(predicate, seconds: float = STEP_TIMEOUT_S) -> tuple:
    """Wait until predicate() is true, on the window's thread (for what only Python can read, such as the view's
    own IsVisible and IsSuspended)."""
    return ("wait", predicate, seconds)


def window_title(app: App) -> str:
    buf = ctypes.create_unicode_buffer(256)
    ctypes.windll.user32.GetWindowTextW(ctypes.c_void_p(app.window.hwnd), buf, 256)
    return buf.value


def view_state(app: App) -> dict:
    """The view as WebView2 itself reports it, beside what the window believes."""
    w = app.window
    return {"visible": w.controller.get_bool("get_IsVisible"), "suspended": w.webview3.get_bool("get_IsSuspended"),
            "hidden": w.view_hidden, "recording": w.recording}


BITMAP_ICON_ID = "g-00000000000000aa"      # a made-up game id the harness holds a bitmap-only icon under
CLICK_GAME = ("[...document.querySelectorAll('#games .game')].find((b) => "
              "b.querySelector('.name').textContent.includes({name})).click(); true")
MADE = "document.querySelectorAll('#view img, #view b, #view script, #view iframe').length"


# On the frame chart's (charts[0]) canvas, down the plot's top third: the least opaque column of the time a measured
# part leaves out (before it, tests/app_demo.py: the part is 4 s to 24 s) and its colour, the same of the part,
# and how much more of the column at the press in the startup is line than the columns beside it (dashed: about
# half).
SHADE_PIXELS = """(() => {
    const u = charts[0], b = u.bbox, ctx = u.ctx, top = Math.round(b.top + 4), rows = Math.round(b.height / 3);
    const column = (x) => ctx.getImageData(Math.round(x), top, 1, rows).data;
    const mean = (d) => { let a = 0; for (let i = 3; i < d.length; i += 4) a += d[i]; return a / (d.length / 4); };
    const least = (times) => times.map((t) => column(u.valToPos(t, 'x', true)))
        .reduce((best, d) => (mean(d) < mean(best) ? d : best));
    // The share of rows at least half opaque, the strongest of three columns: a line 2 px wide however it falls.
    const solid = (x) => {
        const cols = [-1, 0, 1].map((k) => column(x + k));
        let n = 0;
        for (let i = 3; i < cols[0].length; i += 4) if (Math.max(...cols.map((d) => d[i])) > 128) n += 1;
        return n / (cols[0].length / 4);
    };
    const rgb = (d) => {
        const c = [0, 0, 0]; let n = 0;
        for (let i = 0; i < d.length; i += 4) if (d[i + 3] > 0) { c[0] += d[i]; c[1] += d[i + 1]; c[2] += d[i + 2]; n += 1; }
        return n ? c.map((v) => Math.round(v / n)) : null;
    };
    const out = least([0.7, 2.4, 2.7, 3.1]), inside = least([6.2, 8.7, 11.3, 14.6, 17.9, 21.2]);
    const beside = (u, solid) => Math.min(...[-0.45, -0.3, 0.3, 0.45].map((dt) =>
        solid(u.valToPos(OUTSIDE + dt, 'x', true))));
    const press = u.valToPos(OUTSIDE, 'x', true);
    return {left_out: mean(out), left_out_rgb: rgb(out), part: mean(inside),
            dash: solid(press) - beside(u, solid)};
})()""".replace("OUTSIDE", repr(app_demo.OUTSIDE_MARK_S))
# Zoom the frame chart so the press in the startup falls in the middle of the y axis's gutter and the shading of the
# startup runs past the plot's left edge; then what the gutter holds: drawn inside the plot, the shading leaves rows of
# it clear, and the press line is not there.
SHADE_ZOOM = """(() => {
    const u = charts[0], k = u.bbox.left / (2 * u.bbox.width), p = OUTSIDE;
    window.__zoomedFrom = {min: u.scales.x.min, max: u.scales.x.max};
    u.setScale('x', {min: (p + 20 * k) / (1 + k), max: 20});
    return true;
})()""".replace("OUTSIDE", repr(app_demo.OUTSIDE_MARK_S))
SHADE_GUTTER = """(() => {
    const u = charts[0], b = u.bbox, ctx = u.ctx, top = Math.round(b.top + 4), rows = Math.round(b.height / 3);
    const column = (x) => ctx.getImageData(Math.round(x), top, 1, rows).data;
    const fill = column(b.left * 0.75);
    let least = 255;
    for (let i = 3; i < fill.length; i += 4) least = Math.min(least, fill[i]);
    const solid = (x) => {
        const cols = [-1, 0, 1].map((k) => column(x + k));
        let n = 0;
        for (let i = 3; i < cols[0].length; i += 4) if (Math.max(...cols.map((d) => d[i])) > 128) n += 1;
        return n / (cols[0].length / 4);
    };
    const beside = (u, solid) => Math.min(...[-0.45, -0.3, 0.3, 0.45].map((dt) =>
        solid(u.valToPos(OUTSIDE + dt, 'x', true))));
    const press = u.valToPos(OUTSIDE, 'x', true);
    const out = {fill_least: least, press_x: press, left: b.left,
                 line: solid(press) - beside(u, solid)};
    u.setScale('x', window.__zoomedFrom);
    return out;
})()""".replace("OUTSIDE", repr(app_demo.OUTSIDE_MARK_S))


def script(facts: dict, app: App):
    """The steps, as a generator: each yield is an operation, and what it sends back is the step's answer."""
    yield until("document.querySelectorAll('#games .game').length > 0")
    facts["library"] = yield js(f"""(() => ({{
        names: [...document.querySelectorAll('#games .game .name')].map((n) => n.textContent),
        recent: document.querySelectorAll('.recent .game').length,
        strip: document.querySelector('.strip').textContent,
        made: {MADE}, pwned: window.__pwned === 1}}))()""")
    yield shot("library")

    yield js(CLICK_GAME.format(name=json.dumps("Quest")))
    yield until("!!document.getElementById('game-name')")
    facts["game"] = yield js(f"""(() => ({{
        name: document.getElementById('game-name').textContent,
        rows: document.querySelectorAll('tr[data-session]').length,
        last: document.querySelectorAll('.last-tag').length, made: {MADE}, pwned: window.__pwned === 1}}))()""")
    yield shot("game")

    yield js("document.querySelector('tr[data-session]').click(); true")
    yield until("!!document.querySelector('.verdict') && !!document.querySelector('[data-chart=frames] canvas')")
    facts["results"] = yield js(f"""(() => ({{
        game: document.querySelector('#view h1').textContent,
        verdict: document.querySelector('.verdict .badge').textContent,
        findings: document.querySelectorAll('.finding').length,
        frames_drawn: charts.length ? charts[0].data[0].length : -1,
        charts: document.querySelectorAll('#view canvas').length,
        events: document.querySelectorAll('tr[data-event]').length,
        commands: [...document.querySelectorAll('.fix code')].map((c) => c.textContent),
        applies: document.querySelectorAll('.apply button').length,
        part: document.getElementById('part') ? document.getElementById('part').textContent : null,
        rest: document.querySelector('[data-chart=frames]').getAttribute('data-rest'),
        shaded: document.querySelectorAll('.chart[data-rest]').length,
        y_top: charts.length ? charts[0].scales.y.max : null,
        made: {MADE}, pwned: window.__pwned === 1}}))()""")
    # What the frame chart's canvas holds where the part leaves the recording out, and inside the part: the least
    # opacity down a strip of each, over several times, so no frame line, grid line or lag band is all it reads.
    facts["results_pixels"] = yield js(SHADE_PIXELS)
    yield js(SHADE_ZOOM)
    yield sleep(200)
    facts["results_zoomed"] = yield js(SHADE_GUTTER)
    yield sleep(200)
    yield shot("results")
    yield js("document.getElementById('view').scrollTop = document.querySelector('[data-chart=frames]').offsetTop - 70; "
             "true")
    yield sleep(300)
    yield shot("results-charts")

    yield js("document.querySelector('tr[data-event]').click(); true")
    yield until("!!document.querySelector('#moment .moment')")
    facts["moment"] = yield js("""(() => ({
        title: document.querySelector('#moment h3').textContent,
        charts: document.querySelectorAll('#moment canvas').length,
        shaded: document.querySelectorAll('#moment .chart[data-rest]').length,
        causes: document.querySelector('#moment p').textContent}))()""")
    yield sleep(300)
    yield shot("moment")

    # Apply: the first fix the last recording offers shows its plan (Cancel puts the button back), a second click makes
    # that change (App's fixes, here stand-ins whose values this harness keeps), and Settings lists it until the list's
    # own Undo puts it back, drawing the list again with what was said.
    yield js("document.querySelector('.apply button').click(); true")
    yield until("!!document.getElementById('apply-confirm')")
    facts["apply_plan"] = yield js("document.querySelector('.apply').textContent")
    yield shot("results-apply")
    yield js("document.getElementById('apply-cancel').click(); true")
    yield until("!document.getElementById('apply-confirm') && !!document.querySelector('.apply button')")
    facts["apply_cancelled"] = list(app.harness.fix_store.get("writes", []))
    yield js("document.querySelector('.apply button').click(); true")
    yield until("!!document.getElementById('apply-confirm')")
    yield js("document.getElementById('apply-confirm').click(); true")
    yield until("!!document.getElementById('applied')")
    facts["applied"] = yield js("""(() => ({said: document.getElementById('applied').textContent,
        undo: !!document.querySelector('.apply button[data-entry]')}))()""")
    facts["apply_writes"] = list(app.harness.fix_store.get("writes", []))
    yield js("document.querySelector('[data-go=settings]').click(); true")
    yield until("!!document.querySelector('#changes .change')")
    facts["changes"] = yield js("""[...document.querySelectorAll('#changes .change')].map((c) => ({
        title: c.querySelector('strong').textContent, entry: c.dataset.entry,
        undo: !!c.querySelector('button[data-entry]')}))""")
    yield shot("settings-changes")
    yield js("document.querySelector('#changes .change button[data-entry]').click(); true")
    yield until("!!document.getElementById('said') && !!document.getElementById('changes-none')")
    facts["undone"] = yield js("document.getElementById('said').textContent")
    facts["undo_writes"] = list(app.harness.fix_store.get("writes", []))

    yield js("document.querySelector('[data-go=library]').click(); true")
    yield until("document.querySelectorAll('#games .game').length > 0")
    yield js(CLICK_GAME.format(name=json.dumps("Demo Racer")))
    yield until("document.querySelectorAll('tr[data-session] input').length >= 2")
    facts["racer_results_commands"] = yield js("document.querySelectorAll('.last-tag').length")
    facts["racer_game"] = yield js(f"""(() => ({{
        notes: [...document.querySelectorAll('tr[data-session] .note')].map((n) => n.textContent),
        made: {MADE}, pwned: window.__pwned === 1}}))()""")
    yield js("""const boxes = document.querySelectorAll('tr[data-session] input');
        boxes[0].click(); boxes[1].click(); document.getElementById('compare').click(); true""")
    yield until("!!document.getElementById('compare-table')")
    facts["compare"] = yield js("""[...document.querySelectorAll('#view table tbody tr')].map(
        (tr) => [...tr.children].map((td) => td.textContent))""")
    facts["compare_differs"] = yield js("""[...new Set([...document.querySelectorAll('#view td.differs')].map(
        (td) => td.parentElement.children[0].textContent))]""")
    facts["compare_made"] = yield js(f"({{made: {MADE}, pwned: window.__pwned === 1}})")
    yield shot("compare")
    yield js("document.getElementById('back').click(); true")
    yield until("!!document.getElementById('game-name')")
    facts["back"] = yield js("document.getElementById('game-name').textContent")
    # The newest Demo Racer recording, the one with a note, on Results.
    yield js("document.querySelector('tr[data-session]').click(); true")
    yield until("!!document.querySelector('.verdict')")
    facts["racer_results"] = yield js(f"""(() => ({{
        game: document.querySelector('#view h1').textContent,
        note: document.getElementById('note') ? document.getElementById('note').textContent : null,
        made: {MADE}, pwned: window.__pwned === 1}}))()""")
    # Apply from a recording that is not the last one made: its own offer (a GPU preference for Demo Racer's program),
    # made as the last one's is. With two more changes in place, one of them changed by hand since, Settings' Undo all
    # takes a second click and puts back the two it may, newest first; the third's row then offers to put it back
    # anyway, which takes a second click too.
    facts["racer_applies"] = yield js("document.querySelectorAll('.apply button').length")
    yield js("document.querySelector('.apply button').click(); true")
    yield until("!!document.getElementById('apply-confirm')")
    yield js("document.getElementById('apply-confirm').click(); true")
    yield until("!!document.getElementById('applied')")
    facts["racer_applied"] = yield js("document.getElementById('applied').textContent")
    journal = app.fixes.journal()
    for fix_id, now in (("power_plan", fake_fixes.AFTER), ("game_mode_on", "set by hand since")):
        made = journal.begin(fix_id, f"the stand-in setting of {fix_id}", {}, fake_fixes.BEFORE, fake_fixes.AFTER)
        journal.set_state(made.id, "applied")
        app.harness.fix_store[fix_id] = now                        # as Apply leaves it, or as the player changed it

    yield js("document.querySelector('[data-go=settings]').click(); true")
    yield until("[...document.querySelectorAll('#view h1')].some((h) => h.textContent === 'Settings')")
    facts["settings"] = yield js("""(() => ({
        folder: document.querySelector('#view .row code') ? document.querySelector('#view .row code').textContent : null,
        text: document.getElementById('view').textContent}))()""")
    yield shot("settings")
    yield until("!!document.getElementById('undo-all')")
    facts["undo_all_rows"] = yield js("document.querySelectorAll('#changes .change').length")
    yield js("document.getElementById('undo-all').click(); true")
    facts["undo_all_armed"] = yield js("document.getElementById('undo-all').textContent")
    facts["undo_all_first_click"] = list(app.harness.fix_store.get("writes", []))
    yield js("document.getElementById('undo-all').click(); true")
    yield until("!!document.getElementById('said') && document.querySelectorAll('#changes .change').length === 1")
    facts["undo_all_said"] = yield js("[...document.querySelectorAll('#said p')].map((p) => p.textContent)")
    facts["undo_all_writes"] = list(app.harness.fix_store.get("writes", []))
    facts["undo_all_left"] = yield js("""(() => {
        const row = document.querySelector('#changes .change');
        return {title: row.querySelector('strong').textContent,
                undo: !!row.querySelector('button[data-entry]:not([data-force])'),
                anyway: !!row.querySelector('button[data-force]'), again: !!document.getElementById('undo-all')};
    })()""")
    yield js("document.querySelector('#changes .change button[data-force]').click(); true")
    yield js("document.querySelector('#changes .change button[data-force]').click(); true")
    yield until("!!document.getElementById('changes-none')")
    facts["undo_all_anyway"] = yield js("document.getElementById('said').textContent")
    facts["undo_all_anyway_writes"] = list(app.harness.fix_store.get("writes", []))

    # The policy, from inside the page: a new window, a navigation away and malformed messages are turned away.
    facts["window_open"] = yield js("window.open('https://example.com/') === null")
    yield js("location.href = 'https://example.com/'; true")
    yield sleep(1500)
    facts["href_after"] = yield js("location.href")
    yield js("""chrome.webview.postMessage({type: 'request', id: 9001, kind: 'results', session: '..\\\\..\\\\x'});
        chrome.webview.postMessage({type: 'request', id: 9002, kind: 'unknown'});
        chrome.webview.postMessage({type: 'request', id: 9003, kind: 'library', extra: 1}); true""")
    yield sleep(800)
    facts["refused"] = dict(app.window.refused)
    facts["settings_readback"] = dict(app.window.settings_readback)
    facts["options_asked"] = sorted(set(app.window.options_asked[0])) if app.window.options_asked else []
    facts["env_restored"] = app.window.env_restored

    # Recording from the app (M4), with tests/fake_recorder.py in the recorder's place and a harmless process in the
    # relay's (App's recorder and launcher): the window is told it is the active one, then Record.
    rig = app.harness
    yield activate(True)
    yield js("document.querySelector('[data-go=library]').click(); true")
    yield until("!!document.getElementById('record')")
    yield js("document.getElementById('record').click(); true")
    # The stand-in says its start key and a press at once (M5): the running screen measures from it.
    yield until("!!document.querySelector('#running[data-stage=measuring]') && !!document.getElementById('rec-clock')"
                " && !!document.getElementById('rec-stop')")
    facts["rec_running"] = yield js(f"""(() => ({{
        title: document.getElementById('rec-title').textContent,
        game: document.getElementById('rec-game').textContent,
        clock: document.getElementById('rec-clock').textContent,
        detail: document.getElementById('rec-detail').textContent,
        bar: !document.getElementById('rec').hidden, made: {MADE}, pwned: window.__pwned === 1}}))()""")
    facts["rec_view_shown"] = view_state(app)
    yield shot("recording")
    yield activate(False)                              # the player goes back to the game
    yield wait(lambda: app.window.suspended is True)
    facts["rec_view_hidden"] = view_state(app)
    facts["rec_title_hidden"] = window_title(app)
    yield activate(True)                               # and comes back to the window
    yield wait(lambda: view_state(app)["visible"] and not view_state(app)["suspended"])
    facts["rec_view_back"] = view_state(app)
    yield until("!!document.getElementById('rec-stop')")
    # The page crashes while the view is hidden: it loads again hidden, is suspended again, and is back on the
    # running screen once the window is shown.
    readies = facts["readies"]
    yield activate(False)
    yield wait(lambda: view_state(app)["suspended"])
    yield ("crash_renderer",)
    yield until_ready(readies + 1)
    yield wait(lambda: view_state(app)["suspended"] and not view_state(app)["visible"])
    facts["rec_after_page_crash"] = view_state(app)
    yield activate(True)
    yield wait(lambda: view_state(app)["visible"] and not view_state(app)["suspended"])
    yield until("!!document.getElementById('rec-stop')")
    facts["rec_after_page_crash_screen"] = yield js("document.getElementById('rec-title').textContent")
    # The browser process ends while the view is hidden: the new environment's view is hidden and suspended too.
    yield activate(False)
    yield wait(lambda: view_state(app)["suspended"])
    yield ("end_browser",)
    yield until_ready(readies + 2, 60.0)
    yield wait(lambda: view_state(app)["suspended"] and not view_state(app)["visible"], 30.0)
    facts["rec_after_browser_exit"] = view_state(app)
    yield activate(True)
    yield wait(lambda: view_state(app)["visible"] and not view_state(app)["suspended"])
    yield until("!!document.getElementById('rec-stop')")
    # The player goes to the Library and the recording ends meanwhile (stopped from there): the bar keeps a way back to
    # the end, and the Library is asked again, since it now holds the new recording.
    yield js("document.querySelector('[data-go=library]').click(); true")
    yield until("document.querySelectorAll('#games .game').length > 0")
    asks_before = len(rig.library_asks)
    yield js("ask('stop_recording'); true")
    yield until("!document.getElementById('rec').hidden && document.getElementById('rec').textContent === "
                "'Recording ended'")
    yield until("document.querySelectorAll('#games .game').length > 0")
    facts["rec_left_for_the_library"] = {"library_asked_again": len(rig.library_asks) - asks_before,
                                         "bar": (yield js("document.getElementById('rec').textContent"))}
    yield js("document.getElementById('rec').click(); true")
    yield until("!!document.getElementById('rec-results')")
    facts["rec_done"] = yield js("""(() => ({title: document.getElementById('rec-title').textContent,
        bar: !document.getElementById('rec').hidden,
        notes: [...document.querySelectorAll('#rec-notes li')].map((n) => n.textContent)}))()""")
    yield shot("recording-done")
    # A view of the recording older than the one shown changes nothing (a reply and a push that crossed).
    facts["rec_older_view"] = yield js("""(() => {
        recordingPushed(Object.assign({}, rec, {seq: rec.seq - 1, phase: 'recording', active: true,
                                               title: 'Recording', action: 'stop'}));
        return {title: document.getElementById('rec-title').textContent, bar: !document.getElementById('rec').hidden};
    })()""")
    facts["rec_title_done"] = window_title(app)
    yield js("document.getElementById('rec-results').click(); true")
    yield until("!!document.querySelector('.verdict')")
    facts["rec_results"] = yield js("document.querySelector('#view h1').textContent")
    facts["rec_played"], facts["rec_flashes"] = list(rig.played), list(rig.flashes)
    facts["rec_prompts"] = [{"hwnd_is_the_window": c["hwnd"] == app.window.hwnd, "argv": c["argv"]}
                            for c in rig.prompts]
    yield wait(lambda: rig.report.is_file())
    facts["rec_report"] = json.loads(rig.report.read_text(encoding="utf-8"))

    # Play (M6): the Library lists the demo's made-up installed games (tests/app_demo.py's INSTALLED) beside the
    # recorded ones, and an installed game nobody recorded has Play on its page. tests/fake_recorder.py stands in for
    # watch --wait-folder: the prompt for the game's folder, then "launch", on which the window has the store link
    # opened (App's open_link, only noted here) and minimizes itself, then the wait, which the player cancels.
    yield js("document.querySelector('[data-go=library]').click(); true")
    yield until("document.querySelectorAll('#games .game').length > 0")
    facts["library_filters"] = yield js("""(() => {
        const names = () => [...document.querySelectorAll('#games .game .name')].map((n) => n.textContent);
        const out = {};
        for (const f of ['installed', 'recorded', 'all']) {
          document.querySelector(`[data-filter=${f}]`).click();
          out[f] = names();
        }
        return out;
    })()""")
    # The covers: Demo Racer's from its (made-up) store cache, answered by the window's art host, shown over its
    # initials; the others keep their initials; and the art host answers anything else with nothing.
    yield until("[...document.querySelectorAll('#games .cover')].every((c) => c.dataset.art !== 'loading')")
    facts["covers"] = yield js("""(() => Object.fromEntries([...document.querySelectorAll('#games .game')].map((g) => {
        const c = g.querySelector('.cover');
        return [g.querySelector('.name').textContent, {state: c.dataset.art || 'initials', initials: c.textContent,
                drawn: getComputedStyle(c).backgroundImage.includes('https://art.lagdoctor.example/g-')}];
    })))()""")
    yield js("""(() => {
        window.__artProbe = 'waiting';
        const probe = new Image();
        probe.addEventListener('load', () => { window.__artProbe = 'loaded'; });
        probe.addEventListener('error', () => { window.__artProbe = 'refused'; });
        probe.src = 'https://art.lagdoctor.example/g-0000000000000000';
        return true;
    })()""")
    yield until("window.__artProbe !== 'waiting'")
    facts["cover_unknown"] = yield js("window.__artProbe")
    facts["art_refused"] = app.window.refused["art"]
    # The page loads a cover only from the art host's URL exactly as the app builds it: anything else stays initials.
    facts["cover_urls_refused"] = yield js("""(() => ['http://art.lagdoctor.example/g-0123456789abcdef',
        'https://evil.test/g-0123456789abcdef', 'https://art.lagdoctor.example/g-0123456789abcde',
        'https://art.lagdoctor.example/g-0123456789abcdef") url("https://evil.test/a', 5].map((art) => {
          const c = tileCover({initials: 'AB', hue: 10, art});
          return [c.dataset.art || 'initials', c.textContent];
        }))()""")
    yield js(CLICK_GAME.format(name=json.dumps("Lantern Drift")))
    yield until("!!document.getElementById('play')")
    facts["play_game"] = yield js("""(() => ({name: document.getElementById('game-name').textContent,
        none: !!document.getElementById('no-recordings'), rows: document.querySelectorAll('tr[data-session]').length,
        text: document.getElementById('play-card').textContent}))()""")
    yield shot("game-play")
    prompts_before = len(rig.prompts)
    yield js("document.getElementById('play').click(); true")
    yield until("!!document.querySelector('#running[data-phase=waiting]') && !!document.getElementById('rec-cancel')")
    facts["play_waiting"] = yield js("""(() => ({title: document.getElementById('rec-title').textContent,
        game: document.getElementById('rec-game') ? document.getElementById('rec-game').textContent : null,
        detail: document.getElementById('rec-detail').textContent}))()""")
    yield wait(lambda: bool(wv.IsIconic(app.window.hwnd)))       # out of the way of the store and the game
    facts["play_links"] = list(rig.links)
    yield ("restore",)
    yield js("document.getElementById('rec-cancel').click(); true")
    yield until("!!document.querySelector('#running[data-phase=done]')")
    facts["play_done"] = yield js("""(() => ({title: document.getElementById('rec-title').textContent,
        again: !!document.getElementById('play-again')}))()""")
    facts["play_prompts"] = [c["argv"] for c in rig.prompts[prompts_before:]]
    yield wait(lambda: rig.play_report.is_file())
    facts["play_report"] = json.loads(rig.play_report.read_text(encoding="utf-8"))

    # Add a game (screen 6): Add a game opens Windows' Open dialog, here App's picker, which hands back a made-up
    # program under this home (tests/pe_build.py): its name and its icon (a 256 x 256 PNG among its images) are read
    # from its file and shown before it is added. The player names it like markup (shown as text) and adds it; its page
    # offers Record when it starts, which waits on the program's folder with no link opened and the window left where
    # it is; the player cancels, then removes the game, and the Library is as it was.
    yield js("document.querySelector('[data-go=library]').click(); true")
    yield until("!!document.getElementById('add-game')")
    links_before = len(rig.links)
    yield js("document.getElementById('add-game').click(); true")
    yield until("!!document.getElementById('add-name')")
    yield until("document.querySelector('#view .cover').dataset.art !== 'loading'")
    facts["add_screen"] = yield js("""(() => {
        const c = document.querySelector('#view .cover');
        return {name: document.getElementById('add-name').value, exe: document.getElementById('add-exe').textContent,
                folder: document.getElementById('add-folder').textContent, art: c.dataset.art || 'initials',
                fit: c.dataset.fit || null, initials: c.textContent, size: getComputedStyle(c).backgroundSize,
                note: !!document.getElementById('add-note'),
                drawn: getComputedStyle(c).backgroundImage.includes('https://art.lagdoctor.example/g-')};
    })()""")
    facts["add_picks"] = list(rig.picks)
    yield shot("add-game")
    # An icon of bitmap images only (no PNG), as older programs hold: the page's engine decodes it from the art host.
    app.keep_icon(BITMAP_ICON_ID, (exe_mod.ICO, exe_mod.ico(pe_build.icon_bitmap(48, 48))))
    yield js(f"""(() => {{
        window.__icoProbe = 'waiting';
        const probe = new Image();
        probe.addEventListener('load', () => {{ window.__icoProbe = [probe.naturalWidth, probe.naturalHeight]; }});
        probe.addEventListener('error', () => {{ window.__icoProbe = 'refused'; }});
        probe.src = 'https://art.lagdoctor.example/{BITMAP_ICON_ID}';
        return true;
    }})()""")
    yield until("window.__icoProbe !== 'waiting'")
    facts["bitmap_icon"] = yield js("window.__icoProbe")
    yield js(f"document.getElementById('add-name').value = {json.dumps(app_demo.MARKUP_ADDED)}; "
             "document.getElementById('add-confirm').click(); true")
    yield until("!!document.getElementById('wait')")
    facts["added_game"] = yield js(f"""(() => ({{name: document.getElementById('game-name').textContent,
        folder: document.getElementById('wait-folder').textContent,
        text: document.getElementById('wait-card').textContent, none: !!document.getElementById('no-recordings'),
        remove: !!document.getElementById('remove-game'), play: !!document.getElementById('play'),
        made: {MADE}, pwned: window.__pwned === 1}}))()""")
    yield shot("game-added")
    prompts_before = len(rig.prompts)
    yield js("document.getElementById('wait').click(); true")
    yield until("!!document.querySelector('#running[data-phase=waiting]') && !!document.getElementById('rec-cancel')")
    facts["wait_waiting"] = yield js("""(() => ({title: document.getElementById('rec-title').textContent,
        game: document.getElementById('rec-game') ? document.getElementById('rec-game').textContent : null,
        detail: document.getElementById('rec-detail').textContent}))()""")
    yield sleep(800)                                   # long enough for a minimize to have shown
    facts["wait_minimized"] = bool(wv.IsIconic(app.window.hwnd))
    facts["wait_links"] = list(rig.links[links_before:])
    yield js("document.getElementById('rec-cancel').click(); true")
    yield until("!!document.querySelector('#running[data-phase=done]')")
    facts["wait_done"] = yield js("""(() => ({title: document.getElementById('rec-title').textContent,
        again: document.getElementById('play-again') ? document.getElementById('play-again').textContent : null}))()""")
    yield wait(lambda: rig.wait_report.is_file())
    facts["wait_report"] = json.loads(rig.wait_report.read_text(encoding="utf-8"))
    rig.wait_report.unlink()
    # Try again waits on the same folder again (never Play's request), and is cancelled the same way.
    yield js("document.getElementById('play-again').click(); true")
    yield until("!!document.querySelector('#running[data-phase=waiting]') && !!document.getElementById('rec-cancel')")
    facts["wait_again"] = yield js("document.getElementById('rec-title').textContent")
    yield js("document.getElementById('rec-cancel').click(); true")
    yield until("!!document.querySelector('#running[data-phase=done]')")
    facts["wait_prompts"] = [c["argv"] for c in rig.prompts[prompts_before:]]
    yield wait(lambda: rig.wait_report.is_file())
    yield js("document.querySelector('[data-go=library]').click(); true")
    yield until("document.querySelectorAll('#games .game').length > 0")
    yield until("[...document.querySelectorAll('#games .cover')].every((c) => c.dataset.art !== 'loading')")
    facts["library_with_added"] = yield js(f"""(() => {{
        const tiles = [...document.querySelectorAll('#games .game')];
        const mine = tiles.find((b) => b.querySelector('.name').textContent.includes('Moss'));
        const c = mine.querySelector('.cover');
        return {{names: tiles.map((b) => b.querySelector('.name').textContent),
                 sub: mine.querySelector('.sub').textContent, art: c.dataset.art || 'initials',
                 fit: c.dataset.fit || null, made: {MADE}, pwned: window.__pwned === 1}};
    }})()""")
    yield js(CLICK_GAME.format(name=json.dumps("Moss")))
    yield until("!!document.getElementById('rename-game')")
    # Rename: Escape keeps the old name; a new one, like markup, is saved with Enter and shows as text, in the Library
    # too.
    key = "document.getElementById('rename-name').dispatchEvent(new KeyboardEvent('keydown', {{key: '{}'}})); true"
    yield js("document.getElementById('rename-game').click(); true")
    yield until("!!document.getElementById('rename-name')")
    hidden = yield js("document.getElementById('rename-game').hidden")         # while the form is open
    yield js(key.format("Escape"))
    yield until("!!document.getElementById('game-name') && !document.getElementById('rename-name')")
    facts["rename_hidden"] = [hidden, (yield js("document.getElementById('rename-game').hidden"))]
    kept = yield js("document.getElementById('game-name').textContent")
    yield js("document.getElementById('rename-game').click(); true")
    yield until("!!document.getElementById('rename-name')")
    facts["rename_offered"] = yield js("document.getElementById('rename-name').value")
    yield js(f"document.getElementById('rename-name').value = {json.dumps(app_demo.MARKUP_RENAMED)}; true")
    yield js(key.format("Enter"))
    yield until(f"!!document.getElementById('game-name') && document.getElementById('game-name').textContent === "
                f"{json.dumps(app_demo.MARKUP_RENAMED)}")
    facts["renamed"] = yield js(f"""(() => ({{kept: {json.dumps(kept)}, name: document.getElementById('game-name')
        .textContent, form: !!document.getElementById('rename-form'), made: {MADE}, pwned: window.__pwned === 1}}))()""")
    yield js("document.querySelector('[data-go=library]').click(); true")
    yield until("document.querySelectorAll('#games .game').length > 0")
    facts["library_renamed"] = yield js("[...document.querySelectorAll('#games .game .name')].map((n) => n.textContent)")
    yield js(CLICK_GAME.format(name=json.dumps("Moss")))
    yield until("!!document.getElementById('remove-game')")
    yield js("document.getElementById('remove-game').click(); true")
    facts["remove_armed"] = yield js("document.getElementById('remove-game').textContent")
    yield js("document.getElementById('remove-game').click(); true")
    yield until("document.querySelectorAll('#games .game').length > 0 && !document.getElementById('remove-game')")
    facts["library_after_remove"] = yield js("[...document.querySelectorAll('#games .game .name')]"
                                             ".map((n) => n.textContent)")

    # Delete: Demo Racer's older recording, picked on its page, goes with a second click to the Recycle Bin (App's
    # trash, here a folder of this home's): the page is drawn again with the one left and what was said.
    old, other = sorted(e.id for e in library.scan(app.sessions) if e.is_recording and e.game == "Demo Racer")
    yield js(CLICK_GAME.format(name=json.dumps("Demo Racer")))
    yield until("document.querySelectorAll('tr[data-session]').length === 2")
    facts["delete_before"] = yield js("[!!document.getElementById('delete-picked'), "
                                      "document.getElementById('delete-picked').disabled]")
    pick = f"document.querySelector('tr[data-session={json.dumps(old)}] input').click(); true"
    yield js(pick)
    yield js(pick)                                     # picked and unpicked: nothing to delete again
    facts["delete_unpicked"] = yield js("document.getElementById('delete-picked').disabled")
    yield js(pick)
    # Armed, then another pick: the click is undone (a second one would otherwise delete both unasked).
    pick_other = f"document.querySelector('tr[data-session={json.dumps(other)}] input').click(); true"
    yield js("document.getElementById('delete-picked').click(); true")
    yield js(pick_other)
    facts["delete_disarmed"] = yield js("document.getElementById('delete-picked').textContent")
    yield js(pick_other)
    yield js("document.getElementById('delete-picked').click(); true")
    facts["delete_armed"] = yield js("document.getElementById('delete-picked').textContent")
    yield js("document.getElementById('delete-picked').click(); true")
    yield until("!!document.getElementById('said')")
    facts["deleted"] = yield js("""(() => ({said: document.getElementById('said').textContent,
        name: (document.getElementById('game-name') || {}).textContent || null,
        rows: [...document.querySelectorAll('tr[data-session]')].map((r) => r.dataset.session)}))()""")
    facts["delete_old"], facts["delete_trashed"] = old, list(rig.trashed)
    # What it said shows once: not again when Back returns to the page from the recording left.
    yield js(f"document.querySelector('tr[data-session={json.dumps(other)}]').click(); true")
    yield until("!!document.getElementById('frames') || !!document.querySelector('[data-chart=frames]')")
    yield js("document.getElementById('back').click(); true")
    yield until("document.querySelectorAll('tr[data-session]').length === 1")
    facts["said_after_back"] = yield js("!!document.getElementById('said')")

    # Recovery. The renderer crashes: the window loads its page again.
    yield ("crash_renderer",)
    yield until_ready(facts["readies"] + 1)
    yield until("document.querySelectorAll('#games .game').length > 0")
    facts["after_renderer_crash"] = yield js("location.href")
    # The page loaded again after the recording's end: the bar still offers the way back to it, once (its answer to
    # the page's own ask('recording') may come after the library has drawn, so wait for it).
    yield until("!document.getElementById('rec').hidden")
    facts["bar_after_reload"] = yield js("document.getElementById('rec').textContent")
    # The browser process ends: once BrowserProcessExited says the data folder is free, a new environment, with the
    # options object again, and the page again.
    pid = app.window.browser_pid
    yield ("end_browser",)
    yield until_ready(facts["readies"] + 1, 60.0)
    yield until("document.querySelectorAll('#games .game').length > 0")
    facts["after_browser_exit"] = {"generation": app.window.generation, "old_pid": pid,
                                   "new_pid": app.window.browser_pid,
                                   "options_asked_again": sorted(set(app.window.options_asked[-1])),
                                   "env_restored": app.window.env_restored,
                                   "names": (yield js("[...document.querySelectorAll('#games .game .name')]"
                                                      ".map((n) => n.textContent)"))}


class Driver:
    def __init__(self, app: App, facts: dict, shots: Path | None = None) -> None:
        self.app = app
        self.shots = shots
        self.facts = facts
        self.gen = None
        self.deadline = 0.0
        self.readies = 0

    def start(self, app: App) -> None:
        self.readies += 1
        self.facts["readies"] = self.readies
        if self.gen is None:
            self.gen = script(self.facts, app)
            self.resume(None)

    def resume(self, value) -> None:
        try:
            op = self.gen.send(value)
        except StopIteration:
            self.finish()
            return
        except Exception:  # noqa: BLE001
            self.facts["error"] = traceback.format_exc()
            self.finish()
            return
        self.facts["steps"] = self.facts.get("steps", 0) + 1
        w = self.app.window
        if op[0] == "js":
            w.execute_script(op[1], lambda hr, res: self.resume(self.decode(hr, res)))
        elif op[0] == "sleep":
            w.after(op[1], lambda: self.resume(None))
        elif op[0] == "shot":
            self.take_shot(op[1])
        elif op[0] == "ready":
            self.deadline = time.monotonic() + op[2]
            self.wait_ready(op[1])
        elif op[0] == "restore":                     # shown again, as it was, without taking the focus
            wv.ShowWindow(w.hwnd, wv.SW_SHOWNOACTIVATE)
            w.after(50, lambda: self.resume(None))
        elif op[0] == "activate":
            w.on_window_message(w.hwnd, wv.WM_ACTIVATE, 1 if op[1] else wv.WA_INACTIVE, 0)
            w.after(50, lambda: self.resume(None))
        elif op[0] == "wait":
            self.deadline = time.monotonic() + op[2]
            self.wait_for(op[1])
        elif op[0] == "crash_renderer":
            done = w.handler(DEVTOOLS_DONE, wv.DONE_STR, lambda hr, res: None)
            hr = wv.vcall(w.webview.ptr, CALL_DEVTOOLS_SLOT, (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_void_p),
                          "Page.crash", "{}", done.ptr)
            self.facts["crash_call"] = f"0x{hr & 0xFFFFFFFF:08X}"
            w.after(50, lambda: self.resume(None))
        elif op[0] == "end_browser":
            done = subprocess.run(["taskkill", "/F", "/PID", str(w.browser_pid)], capture_output=True,
                                  timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
            self.facts["end_browser"] = done.returncode
            w.after(50, lambda: self.resume(None))
        else:
            self.deadline = time.monotonic() + op[2]
            self.poll(op[1])

    def decode(self, hr: int, res: str | None):
        if hr < 0:
            return {"hresult": f"0x{hr & 0xFFFFFFFF:08X}"}
        try:
            return json.loads(res) if res else None
        except ValueError:
            return res

    def take_shot(self, name: str) -> None:
        if self.shots is None:
            self.resume(None)
            return
        w = self.app.window
        stream = ctypes.c_void_p()
        path = self.shots / f"{name}.png"
        hr = _shlwapi.SHCreateStreamOnFileEx(str(path), STGM_CREATE | STGM_WRITE | STGM_SHARE_DENY_WRITE,
                                             FILE_ATTRIBUTE_NORMAL, 1, None, ctypes.byref(stream))
        if hr < 0:
            self.facts.setdefault("shots_failed", []).append(f"{name}: 0x{hr & 0xFFFFFFFF:08X}")
            self.resume(None)
            return

        def done(hr, _):
            wv.vcall(stream.value, 2, (), restype=ctypes.c_ulong)          # Release: the file is complete
            if hr < 0:
                self.facts.setdefault("shots_failed", []).append(f"{name}: 0x{hr & 0xFFFFFFFF:08X}")
            w.call_soon(lambda: self.resume(None))
        h = wv.Handler(SHOT_DONE, DONE_HR, done, guard=lambda iface, fn: (lambda this, hr: (fn(hr, None), 0)[1]))
        w.keep.append(h)
        hr = wv.vcall(w.webview.ptr, CAPTURE_PREVIEW_SLOT, (ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p), 0,
                      stream.value, h.ptr)
        if hr < 0:
            wv.vcall(stream.value, 2, (), restype=ctypes.c_ulong)
            self.facts.setdefault("shots_failed", []).append(f"{name}: 0x{hr & 0xFFFFFFFF:08X}")
            self.resume(None)

    def wait_ready(self, n: int) -> None:
        if self.readies >= n:
            self.resume(True)
        elif time.monotonic() > self.deadline:
            self.facts["error"] = f"the page was not ready a {n}th time"
            self.finish()
        else:
            self.app.window.after(POLL_MS, lambda: self.wait_ready(n))

    def wait_for(self, predicate) -> None:
        try:
            ok = bool(predicate())
        except Exception:  # noqa: BLE001 - a failed read is a failed wait, said in full
            self.facts["error"] = traceback.format_exc()
            self.finish()
            return
        if ok:
            self.resume(True)
        elif time.monotonic() > self.deadline:
            self.facts["error"] = "timed out waiting for a condition only Python can read"
            self.finish()
        else:
            self.app.window.after(POLL_MS, lambda: self.wait_for(predicate))

    def poll(self, code: str) -> None:
        def check(hr, res):
            if self.decode(hr, res) is True:
                self.resume(True)
            elif time.monotonic() > self.deadline:
                self.facts["error"] = f"timed out waiting for: {code}"
                self.finish()
            else:
                self.app.window.after(POLL_MS, lambda: self.poll(code))
        self.app.window.execute_script(f"(() => {{ try {{ return !!({code}); }} catch (e) {{ return false; }} }})()",
                                       check)

    def finish(self) -> None:
        self.facts["finished"] = True
        self.app.window.begin_close()


def main(argv: list[str]) -> int:
    home, out = Path(argv[0]), Path(argv[1])
    shots = Path(argv[2]) if len(argv) > 2 else None
    if shots is not None:
        shots.mkdir(parents=True, exist_ok=True)
    if Path(os.environ.get("LAGDOCTOR_HOME", "")).resolve() != home.resolve():
        print("LAGDOCTOR_HOME must name the same home", file=sys.stderr)
        return 2
    facts: dict = {"log": [], "told": [], "opened": []}
    runtime = wv.find_runtime()
    if runtime is None:
        print("no WebView2 Runtime", file=sys.stderr)
        return 3
    create = wv.environment_creator(runtime["dll"])
    wv.SetProcessDpiAwarenessContext(wv.DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
    wv.CoInitializeEx(None, wv.COINIT_APARTMENTTHREADED)
    # The recording's stand-ins: the recorder records nothing and saves no file, the prompt is a harmless process of
    # this harness's own, and the sounds and the taskbar flash are only noted.
    rig = types.SimpleNamespace(played=[], flashes=[], prompts=[], relays=[], report=home / "fake-recorder-report.json",
                                library_asks=[], links=[], play_report=home / "fake-recorder-play-report.json",
                                wait_report=home / "fake-recorder-wait-report.json", picks=[], trashed=[],
                                fix_store={})
    # Add a game's program, made up: what App's picker hands back in place of Windows' Open dialog.
    moss = home / "games" / "Moss Garden" / "mg.exe"             # its own name is not its file's: "Moss Garden"
    moss.parent.mkdir(parents=True, exist_ok=True)
    moss.write_bytes(pe_build.program({"ProductName": "Moss Garden"},
                                      [pe_build.icon_bitmap(32, 32), pe_build.png(256, 256)]))
    recordings = [e for e in library.scan(home / "sessions") if e.is_recording]
    # The markup-named game the tests use, else (the README's pictures library) the newest recording's game.
    picked = next((e for e in recordings if e.game == app_demo.MARKUP_TITLE), recordings[0])
    plan = home / "fake-recorder-plan.json"
    plan.write_text(json.dumps({"elevate": True, "game": picked.game, "marks": 2, "session": picked.id,
                                "length_s": recording.LENGTH_S,
                                "start_key": {"registered": True, "window_s": recording.WINDOW_S,
                                              "start_by_s": recording.START_BY_S},
                                "press_at": 30.0, "end": "stop", "report": str(rig.report)}), encoding="utf-8")
    play_plan = home / "fake-recorder-play-plan.json"

    def trash(path: str, hwnd) -> None:
        # the Recycle Bin's stand-in: never this PC's own; the folder is moved into this home's "bin"
        rig.trashed.append([path, hwnd == app.window.hwnd])
        (home / "bin").mkdir(exist_ok=True)
        shutil.move(path, home / "bin" / Path(path).name)

    def play_recorder(folder: str) -> list[str]:
        # watch --wait-folder's stand-in for the game the page chose: it waits for the game until the player cancels
        mine = str(rig.wait_report if folder == str(moss.parent) else rig.play_report)     # Record when it starts
        play_plan.write_text(json.dumps({"elevate": True, "folder": folder, "game": "lanterndrift.exe", "pause_s": 60,
                                         "report": mine}), encoding="utf-8")
        return [str(fake_recorder.BASE_PY), "-B", "-E", "-s", "-m", "tests.fake_recorder", str(play_plan)]
    app = App(home=home, runtime=runtime, create_fn=create, log=facts["log"].append, tell=facts["told"].append,
              activate=False, opener=facts["opened"].append,
              recorder=lambda: [str(fake_recorder.BASE_PY), "-B", "-E", "-s", "-m", "tests.fake_recorder", str(plan)],
              launcher=fake_recorder.stand_in_prompt(rig.prompts, rig.relays), play=rig.played.append,
              flash=rig.flashes.append, installed=lambda: list(app_demo.INSTALLED),
              forms_of=lambda folder: (folder,), open_link=lambda link, should_go: rig.links.append(link),
              play_recorder=play_recorder, covers=app_demo.covers,
              picker=lambda hwnd: (rig.picks.append(hwnd == app.window.hwnd), str(moss))[1], trash=trash,
              fixes=fake_fixes.StandInFixes(home, store=rig.fix_store))
    app.harness = rig
    real_library = app.do_library
    app.do_library = lambda: (rig.library_asks.append(1), real_library())[1]     # counts the page's asks
    app.window.notify_state = lambda: wv.QUNS_ACCEPTS_NOTIFICATIONS   # the flash's own wait is tested apart
    from lagdoctor.winapi import elevation

    def no_real_prompt(*args, **kwargs):           # whatever slips, this run never raises Windows' UAC prompt
        raise AssertionError("the harness reached the real UAC prompt")
    elevation.launch_elevated = no_real_prompt
    driver = Driver(app, facts, shots)
    app.on_ready = driver.start

    def give_up():
        if not app.window.closing:
            facts["error"] = f"the run took over {RUN_TIMEOUT_S:.0f} s"
            app.window.begin_close()
    app.window.create_window()
    app.window.after(int(RUN_TIMEOUT_S * 1000), give_up)
    try:
        app.window.create_environment()
        app.window.loop()
    finally:
        facts["fatal"] = app.window.fatal
        app.window.destroy_icons()
        for p in rig.relays:
            p.kill()
        if app.rec is not None and app.rec.proc is not None and app.rec.proc.poll() is None:
            app.rec.proc.kill()                         # a run that failed mid-recording leaves no recorder behind
        out.write_text(json.dumps(facts, indent=1, default=str), encoding="utf-8")
    return 0 if facts.get("finished") and "error" not in facts else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
