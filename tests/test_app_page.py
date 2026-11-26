"""lag-doctor's window, driven for real: the page on a demo library through the host's own script hook
(tests/app_harness.py), plus what the page's files must hold (its CSP, the vendored uPlot and its licence, the icon).

The window opens for a few seconds without taking the focus, in a child process, and closes itself.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from lagdoctor.app import ASSETS, ICON, bridge
from lagdoctor.app import main as app_main
from lagdoctor.app import recording, views
from lagdoctor.app import window as app_window
from lagdoctor.winapi import webview2 as wv
from tests import app_demo

ROOT = Path(__file__).resolve().parents[1]
CHILD_TIMEOUT_S = 240
# uPlot 1.6.32 from npm (registry.npmjs.org/uplot/-/uplot-1.6.32.tgz, its published SHA-512 checked 2026-11-08):
# the three files lagdoctor/app/assets/vendor/ holds, byte for byte.
UPLOT = {"uPlot.iife.min.js": "19c8d4c6ad88929a79f4ae49d6f7161566dfd0ba3d15cc495e974f787eb78f1f",
         "uPlot.min.css": "df630c6a8d6f8eeaff264b50f73ce5b114f646ffd9a0bb74f049b0a00135fa04",
         "uPlot-LICENSE.txt": "8f989229699b4fe2f1a0432d0e9edc338a8a911e250e2d1b01ecd770a5f5b1bd"}


# ------------------------------------------------------------------------------------------- the real page --

@pytest.fixture(scope="module")
def run(tmp_path_factory) -> dict:
    home = tmp_path_factory.mktemp("app-page-home")
    built = app_demo.build(home)
    out = home / "harness.json"
    env = dict(os.environ, LAGDOCTOR_HOME=str(home))
    done = subprocess.run([sys.executable, "-m", "tests.app_harness", str(home), str(out)], cwd=ROOT, env=env,
                          capture_output=True, encoding="utf-8", errors="replace", timeout=CHILD_TIMEOUT_S)
    facts = json.loads(out.read_text(encoding="utf-8")) if out.is_file() else {}
    facts["_exit"], facts["_stderr"], facts["_built"] = done.returncode, done.stderr[-3000:], built
    facts["_home"] = str(home)
    return facts


def _ok(run: dict) -> None:
    assert run["_exit"] == 0 and run.get("finished") and "error" not in run, (
        run.get("error"), run["_stderr"], run.get("log"))


@pytest.mark.needs("webview2")
def test_the_window_opens_on_the_library_and_lists_each_recorded_game_once(run):
    _ok(run)
    lib = run["library"]
    # The recorded games, newest recording first (the check-up is left out), then the installed ones never recorded.
    assert lib["names"] == [app_demo.MARKUP_TITLE, "Demo Racer", "starplanner", *app_demo.NOT_RECORDED]
    assert lib["recent"] == 3 and lib["strip"]                       # the Recent row: the recorded ones
    assert run["told"] == [] and run["fatal"] == []


@pytest.mark.needs("webview2")
def test_the_library_lists_installed_games_with_their_recordings_and_filters_them(run):
    # Demo Racer's recordings ran from its install folder: they show under the installed game, as one tile.
    _ok(run)
    f = run["library_filters"]
    assert f["installed"] == ["Demo Racer", *app_demo.NOT_RECORDED]
    assert f["recorded"] == [app_demo.MARKUP_TITLE, "Demo Racer", "starplanner"]
    assert f["all"] == run["library"]["names"]


@pytest.mark.needs("webview2")
def test_a_cover_from_the_stores_cache_comes_from_the_windows_own_art_host(run):
    # Demo Racer's made-up cover, answered by the window itself (WebResourceRequested): drawn over its initials. The
    # games without one keep their initials and ask for none, and anything else the art host is asked for is refused.
    _ok(run)
    covers = run["covers"]
    assert covers.pop("Demo Racer") == {"state": "shown", "initials": "", "drawn": True}
    assert all(c["state"] == "initials" and c["initials"] and not c["drawn"] for c in covers.values()), covers
    assert run["cover_unknown"] == "refused" and run["art_refused"] == 1
    assert run["cover_urls_refused"] == [["initials", "AB"]] * 5


@pytest.mark.needs("webview2")
def test_play_starts_an_installed_game_through_its_store_link_and_waits_for_it(run):
    _ok(run)
    g = run["play_game"]
    assert g["name"] == "Lantern Drift" and g["none"] is True and g["rows"] == 0
    assert "Ctrl+Alt+S" in g["text"] and "Epic Games start the game" in g["text"]
    # The prompt came first, for the game's folder and the app's wait (the relay's command, checked by the window).
    [argv] = run["play_prompts"]
    assert argv[argv.index("--folder") + 1] == "C:\\Games\\Lantern Drift" and "--wait-s" in argv
    # Then "launch": the store link opened once, the window out of the way, and the running screen waits for it.
    lantern = next(g for g in app_demo.INSTALLED if g.title == "Lantern Drift")
    assert run["play_links"] == [lantern.link]
    w = run["play_waiting"]
    assert w["title"] == "Waiting for the game" and w["game"] == "Lantern Drift" and "Epic Games" in w["detail"]
    # The player cancelled the wait: nothing recorded, and Try again plays it again.
    assert run["play_done"] == {"title": "Cancelled", "again": True}
    rep = run["play_report"]
    assert rep["prompt"] == "approved" and [m["t"] for m in rep["got"]] == ["elevated", "cancel"]


@pytest.mark.needs("webview2")
def test_add_a_game_reads_the_chosen_programs_name_and_icon_and_records_it_when_it_starts(run):
    _ok(run)
    exe = Path(run["_home"]) / "games" / "Moss Garden" / "mg.exe"
    # The Open dialog (App's picker here), owned by the window, gave a made-up program: its name and its icon (a PNG
    # in an icon file, from the window's art host) are shown before it is added.
    assert run["add_picks"] == [True]
    screen = dict(run["add_screen"])
    assert screen.pop("size").startswith("50%")           # an icon sits in the tile's middle, on its colour
    assert screen == {"name": "Moss Garden", "exe": str(exe), "folder": str(exe.parent), "art": "shown",
                      "fit": "icon", "initials": "", "drawn": True, "note": False}      # its own name, not "mg"
    assert run["bitmap_icon"] == [48, 48]                 # an icon of bitmaps only decodes too
    # Added by a name like markup: shown as text, with Record when it starts and Remove, and no Play.
    g = run["added_game"]
    assert g["name"] == app_demo.MARKUP_ADDED and g["folder"] == str(exe.parent) and g["none"] is True
    assert g["remove"] is True and g["play"] is False and g["made"] == 0 and g["pwned"] is False
    assert "start the game yourself" in g["text"] and "Ctrl+Alt+S" in g["text"]
    # Record when it starts: the prompt for the program's folder, then "start the game now", no link opened and
    # the window left where it is; cancelled, it offers to try again.
    first, again = run["wait_prompts"]                    # and Try again asked for the same folder's relay
    for argv in (first, again):
        assert argv[argv.index("--folder") + 1] == str(exe.parent) and "--wait-s" in argv
    assert run["wait_again"] == "Start the game now"
    w = run["wait_waiting"]
    assert w["title"] == "Start the game now" and w["game"] == app_demo.MARKUP_ADDED
    assert w["detail"].startswith(f"Start {app_demo.MARKUP_ADDED} yourself")
    assert run["wait_links"] == [] and run["wait_minimized"] is False
    assert run["wait_done"] == {"title": "Cancelled", "again": "Try again"}
    assert run["wait_report"]["prompt"] == "approved"
    assert [m["t"] for m in run["wait_report"]["got"]] == ["elevated", "cancel"]
    # In the Library with its icon, among the games not recorded yet; removed with a second click, and gone.
    lib = run["library_with_added"]
    assert lib["names"] == run["library"]["names"] + [app_demo.MARKUP_ADDED]
    assert lib["sub"] == "Added by you \u00b7 not recorded yet" and (lib["art"], lib["fit"]) == ("shown", "icon")
    assert lib["made"] == 0 and lib["pwned"] is False
    assert run["remove_armed"] == "Click again to remove it (its recordings stay)"
    assert run["library_after_remove"] == run["library"]["names"]


@pytest.mark.needs("webview2")
def test_an_added_game_is_renamed_in_place_and_its_new_name_shows_as_text(run):
    _ok(run)
    assert run["rename_offered"] == app_demo.MARKUP_ADDED            # the name it has, to change
    assert run["rename_hidden"] == [True, False]                      # Rename hides while its form is open
    assert run["renamed"] == {"kept": app_demo.MARKUP_ADDED, "name": app_demo.MARKUP_RENAMED, "form": False,
                              "made": 0, "pwned": False}
    assert run["library_renamed"] == run["library"]["names"] + [app_demo.MARKUP_RENAMED]


@pytest.mark.needs("webview2")
def test_picked_recordings_are_deleted_with_a_second_click_into_the_recycle_bin(run):
    _ok(run)
    home = Path(run["_home"])
    old = run["delete_old"]
    assert old.startswith("20261101-") and run["delete_before"] == [True, True]     # nothing picked: nothing to do
    assert run["delete_unpicked"] is True
    assert run["delete_disarmed"] == "Delete the picked"            # armed, then another pick: asked again
    assert run["delete_armed"] == "Click again to move it to the Recycle Bin"
    assert run["delete_trashed"] == [[str(home / "sessions" / old), True]]       # its full path; owned by the window
    d = run["deleted"]
    assert d["said"] == views.DELETED_ONE and d["name"] == "Demo Racer" and len(d["rows"]) == 1 and old not in d["rows"]
    assert not (home / "sessions" / old).exists() and (home / "bin" / old / "capture.ldcap.json.gz").is_file()
    assert run["said_after_back"] is False                           # said once, not again on Back


@pytest.mark.needs("webview2")
def test_a_game_and_a_process_named_like_markup_appear_as_text(run):
    # The demo game's title is '<img src=x onerror=...><b>Bold</b> Quest': as text it shows those characters; as
    # markup it would make an <img> and a <b> (and the CSP would still stop the handler). No screen made either.
    _ok(run)
    for screen in ("library", "game", "results"):
        assert run[screen]["made"] == 0 and run[screen]["pwned"] is False, screen
    assert run["game"]["name"] == app_demo.MARKUP_TITLE and run["results"]["game"] == app_demo.MARKUP_TITLE


@pytest.mark.needs("webview2")
def test_results_draw_every_frame_handed_over_in_the_shared_buffer(run):
    _ok(run)
    expected = next(f["frames"] for f in run["_built"].values() if f["demo"].title == app_demo.MARKUP_TITLE)
    r = run["results"]
    assert r["frames_drawn"] == expected and expected > 1000
    assert r["verdict"] == "Stutter" and r["findings"] >= 1 and r["events"] >= 1 and r["charts"] >= 2
    # Measured from its start key (tests/app_demo.py): the part is named, and the rest shaded on every chart drawn.
    assert r["part"].startswith("Analysed 20s, from 4s to 24s of the recording (measured from your start key)")
    assert r["part"].endswith("It is shaded on the charts.") and r["rest"].startswith("0-4 24-")
    assert r["shaded"] == r["charts"]
    # Drawn, not only named: the canvas is tinted where the part leaves the recording out, and clear inside it (a tint
    # of 0.16, about 41 of 255, never an opaque fill), in a grey, not the lag band's red; the press in the startup is
    # an upright dashed line (about half of its column), not a solid one.
    px = run["results_pixels"]
    assert 25 < px["left_out"] - px["part"] < 70, px
    assert px["left_out_rgb"] and max(px["left_out_rgb"]) - min(px["left_out_rgb"]) < 25, px
    assert 0.25 < px["dash"] < 0.8, px
    # Zoomed so the startup runs past the plot's left edge: its tint and the press line stay inside the plot, and the
    # y axis's gutter keeps rows with nothing drawn on them.
    zoomed = run["results_zoomed"]
    assert zoomed["press_x"] < zoomed["left"] and zoomed["fill_least"] < 10 and zoomed["line"] < 0.15, zoomed
    # The scale comes from the part's frames: the loading screen before the press (frames of LOADING_MS, more than
    # one in a thousand of them) is drawn clamped at the top instead of setting it.
    assert r["y_top"] < app_demo.LOADING_MS, r["y_top"]


@pytest.mark.needs("webview2")
def test_apply_shows_its_plan_then_makes_that_change_and_settings_undoes_it(run):
    from tests.fake_fixes import AFTER, BEFORE
    _ok(run)
    assert run["results"]["applies"] >= 1 and run["results"]["commands"] == []     # a button, no terminal command
    assert run["game"]["last"] == 1 and run["racer_results_commands"] == 0
    assert "Where: " in run["apply_plan"] and "Change: " in run["apply_plan"] and "Apply this change" in run["apply_plan"]
    assert run["apply_cancelled"] == []                       # showing the plan, and Cancel, changed nothing
    assert run["applied"]["said"].startswith("Done: ") and run["applied"]["undo"]
    ((fix_id, value),) = run["apply_writes"]
    assert value == AFTER
    assert [(c["title"], c["undo"]) for c in run["changes"]] == [(f"The stand-in for {fix_id}", True)]
    assert run["undone"].startswith("Undone: ") and run["undo_writes"] == [[fix_id, AFTER], [fix_id, BEFORE]]


@pytest.mark.needs("webview2")
def test_apply_works_from_a_recording_that_is_not_the_last_and_undo_all_puts_back_every_change(run):
    from tests.fake_fixes import AFTER, BEFORE
    _ok(run)
    assert run["racer_applies"] >= 1 and run["racer_applied"].startswith("Done: ")
    assert run["undo_all_rows"] == 4 and run["undo_all_armed"] == "Click again to undo all 4 changes"
    assert run["undo_all_first_click"][-1] == ["gpu_preference", AFTER]          # the first click changed nothing
    *left, power, gpu = run["undo_all_said"]                                     # newest first
    assert [s.split(":")[0] for s in left] == ["The stand-in for game_dvr_off", "The stand-in for game_mode_on"]
    assert all(s.split(": ", 1)[1].startswith("It was changed after lag-doctor set it") for s in left)
    assert [s.split(":")[1].strip() for s in (power, gpu)] == ["The stand-in for power_plan",
                                                               "The stand-in for gpu_preference"]
    assert power.startswith("Undone: ") and gpu.startswith("Undone: ")
    done = len(run["undo_all_first_click"])
    assert run["undo_all_writes"][done:] == [["power_plan", BEFORE], ["gpu_preference", BEFORE]]
    # The two changed since: their rows offer only to put each back anyway, and Undo all is not offered for them.
    assert run["undo_all_left"] == {"rows": [{"title": f"The stand-in for {fid}", "undo": False, "anyway": True}
                                             for fid in ("game_dvr_off", "game_mode_on")], "again": False}
    assert run["undo_all_anyway"].startswith("Undone: ")
    assert run["undo_all_anyway_writes"][done + 2:] == [["game_dvr_off", BEFORE]]


@pytest.mark.needs("webview2")
def test_a_lag_moment_opens_with_its_charts(run):
    _ok(run)
    assert run["moment"]["title"] and run["moment"]["charts"] >= 2
    assert run["moment"]["shaded"] == run["moment"]["charts"]          # a moment shades what the part left out too


@pytest.mark.needs("webview2")
def test_compare_puts_two_recordings_side_by_side_and_back_returns_to_the_game(run):
    _ok(run)
    rows = {r[0]: r[1:] for r in run["compare"]}
    assert rows["Verdict"] == ["No lag found", "Stutter"] and rows["Refresh rate"] == ["144 Hz", "144 Hz"]
    # What changed in the game itself: the fan fix's files, and its settings file in another copy.
    assert rows["Drop-in files"] == ["dxgi.dll, scripts\\FrameFix.asi", "none found"]
    assert rows["Unreal settings files"] == ["Engine.ini, GameUserSettings.ini (different copy)"] * 2
    # A note on one side only is no difference claimed: the other recording has none to differ from.
    assert sorted(run["compare_differs"]) == ["Drop-in files", "Unreal settings files"]
    assert run["back"] == "Demo Racer"


@pytest.mark.needs("webview2")
def test_a_note_shows_as_text_on_the_games_recordings_on_results_and_in_compare(run):
    # The note on the Demo Racer recording made after its fan fix would be markup ('<img ... onerror=...>', '<b>'):
    # each screen shows its characters and makes no element of it.
    _ok(run)
    assert run["racer_game"]["notes"] == [app_demo.MARKUP_NOTE]           # the recording before the fix has none
    assert run["racer_results"]["game"] == "Demo Racer"
    assert run["racer_results"]["note"] == "Your note: " + app_demo.MARKUP_NOTE
    assert {r[0]: r[1:] for r in run["compare"]}["Your note"] == [app_demo.MARKUP_NOTE, "-"]
    for screen in ("racer_game", "compare_made", "racer_results"):
        assert run[screen]["made"] == 0 and run[screen]["pwned"] is False, screen


@pytest.mark.needs("webview2")
def test_settings_name_the_sessions_folder_and_the_lag_key(run):
    _ok(run)
    assert run["settings"]["folder"].endswith("sessions") and "Ctrl+Alt+L" in run["settings"]["text"]
    assert "Ctrl+Alt+S" in run["settings"]["text"]                 # the start key (M5c)


@pytest.mark.needs("webview2")
def test_record_runs_a_recording_the_window_follows_with_the_game_named_as_text(run):
    # tests/fake_recorder.py stands in for the recorder, under the markup-named game: the running screen and the end
    # come from its real control channel, and "Open its results" opens the session it named.
    _ok(run)
    r = run["rec_running"]
    # It says its start key and a press at once (M5c): the clock counts the measured window from the press.
    assert r["title"] == "Measuring" and r["game"] == app_demo.MARKUP_TITLE and r["made"] == 0 and r["pwned"] is False
    assert re.fullmatch(r"\d\d:\d\d of 10:00", r["clock"]) and "Ctrl+Alt+L" in r["detail"] and r["bar"] is True
    d = run["rec_done"]
    assert d["title"] == "Recorded" and d["bar"] is False and "Lag marked 2 times." in d["notes"]
    assert run["rec_results"] == app_demo.MARKUP_TITLE
    # The harness's window is never the one in front: the prompt knocks and flashes, and so does the end.
    assert run["rec_played"] == ["ask", "ready", "start", "mark", "mark", "done"]
    assert run["rec_flashes"] == [True, False, True]     # the prompt's, stopped once it was answered, and the end's
    assert run["bar_after_reload"] == "Recording ended"  # a page loaded again after the end still leads back to it
    assert run["rec_older_view"] == {"title": "Recorded", "bar": False}       # an older view changes nothing
    # Ended while the player was in the Library: the bar led back to the end, and the Library was asked again.
    assert run["rec_left_for_the_library"] == {"library_asked_again": 1, "bar": "Recording ended"}
    assert (run["rec_title_hidden"], run["rec_title_done"]) == ("lag-doctor: recording", "lag-doctor")


@pytest.mark.needs("webview2")
def test_the_view_hides_and_suspends_while_the_game_records_and_comes_back_with_the_window(run):
    # What WebView2 itself answers (IsVisible, IsSuspended), not only what the window meant to do.
    _ok(run)
    hidden = {"visible": False, "suspended": True, "hidden": True, "recording": True}
    assert run["rec_view_shown"] == {"visible": True, "suspended": False, "hidden": False, "recording": True}
    assert run["rec_view_hidden"] == hidden
    assert run["rec_view_back"] == {"visible": True, "suspended": False, "hidden": False, "recording": True}
    # A page that crashed, and a browser process that ended, while hidden: hidden and suspended again, and the page
    # back on the running screen.
    assert run["rec_after_page_crash"] == hidden and run["rec_after_page_crash_screen"] == "Measuring"
    assert run["rec_after_browser_exit"] == hidden


@pytest.mark.needs("webview2")
def test_the_relays_prompt_belongs_to_the_window_and_its_handle_reaches_the_recorder(run):
    _ok(run)
    [prompt] = run["rec_prompts"]
    assert prompt["hwnd_is_the_window"] and prompt["argv"][1:6] == recording.RELAY_HEAD
    rep = run["rec_report"]
    assert rep["prompt"] == "approved" and rep["relay_running"] is True
    assert [g["t"] for g in rep["got"]] == ["elevated", "stop"]


@pytest.mark.needs("webview2")
def test_the_page_cannot_leave_open_windows_or_ask_for_what_it_may_not(run):
    _ok(run)
    assert run["window_open"] is True                              # window.open returned null: no new window
    assert run["href_after"] == "https://app.lagdoctor.example/index.html"
    assert run["refused"] == {"new window": 1, "navigation": 1, "malformed message": 3}


@pytest.mark.needs("webview2")
def test_a_crashed_page_loads_again_and_an_ended_browser_process_gets_a_new_environment(run):
    _ok(run)
    assert run["crash_call"] == "0x00000000" and run["after_renderer_crash"] == "https://app.lagdoctor.example/index.html"
    after = run["after_browser_exit"]
    # The second browser exit: the recording's run ended one too (test_the_view_hides_and_suspends...).
    assert run["end_browser"] == 0 and after["generation"] == 3 and after["new_pid"] != after["old_pid"]
    assert {"ExclusiveUserDataFolderAccess", "IsCustomCrashReportingEnabled"} <= set(after["options_asked_again"])
    assert after["env_restored"] is True and after["names"] == run["library"]["names"]
    assert run["readies"] == 5 and run["fatal"] == []


@pytest.mark.needs("webview2")
def test_every_setting_reads_back_and_the_runtime_read_the_options(run):
    _ok(run)
    assert set(run["settings_readback"]) == {prop for _, prop, _ in app_window.SETTINGS}
    assert set(run["settings_readback"].values()) == {"ok"}
    assert {"ExclusiveUserDataFolderAccess", "IsCustomCrashReportingEnabled"} <= set(run["options_asked"])
    assert run["env_restored"] is True


# ------------------------------------------------------------------------------------------- the page's files --

def test_the_page_loads_nothing_from_outside_and_runs_no_inline_script():
    html = (ASSETS / "index.html").read_text(encoding="utf-8")
    csp = re.search(r'http-equiv="Content-Security-Policy" content="([^"]+)"', html).group(1)
    rules = {part.split()[0]: part.split()[1:] for part in " ".join(csp.split()).split(";") if part.strip()}
    assert rules["default-src"] == ["'none'"] and rules["script-src"] == ["'self'"] and rules["style-src"] == ["'self'"]
    assert rules["worker-src"] == ["'none'"] and rules["connect-src"] == ["'none'"] and rules["frame-src"] == ["'none'"]
    # Images from the page's own host, and covers from the art host the window answers itself (bridge.ART_ORIGIN):
    # that origin, named once, is the only one, and nothing else names http at all.
    assert rules["img-src"] == ["'self'", bridge.ART_ORIGIN]
    assert "unsafe" not in csp and csp.count(bridge.ART_ORIGIN) == 1 and "http" not in csp.replace(bridge.ART_ORIGIN, "")
    assert "<script>" not in html and not re.search(r"\son\w+=", html)
    assert re.findall(r'src="([^"]+)"', html) == ["vendor/uPlot.iife.min.js", "app.js"]


def test_the_pages_script_makes_text_nodes_and_never_parses_markup():
    js = (ASSETS / "app.js").read_text(encoding="utf-8")
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "DOMParser", "createContextualFragment",
                 "eval(", "new Function", "setTimeout('", 'setTimeout("'):
        assert sink not in js, sink
    assert "document.createTextNode(String(c))" in js


@pytest.mark.parametrize("name, digest", sorted(UPLOT.items()))
def test_the_vendored_uplot_is_the_pinned_release_with_its_licence_beside_it(name, digest):
    data = (ASSETS / "vendor" / name).read_bytes()
    assert hashlib.sha256(data).hexdigest() == digest
    if name == "uPlot-LICENSE.txt":
        assert b"The MIT License" in data and b"Copyright (c) 2022 Leon Sorokin" in data


def test_the_assets_folder_holds_only_the_page_and_its_vendored_files():
    files = sorted(p.relative_to(ASSETS).as_posix() for p in ASSETS.rglob("*") if p.is_file())
    assert files == ["app.css", "app.js", "index.html", *sorted(f"vendor/{n}" for n in UPLOT)]


def test_the_committed_icon_is_what_make_icon_draws():
    from scripts import make_icon
    assert ICON.read_bytes() == make_icon.ico_bytes()
    assert ICON.parent == ASSETS.parent                            # beside the page's folder, never mapped with it


# ----------------------------------------------------------------------------------------- the window's rules --

def test_the_app_refuses_elevated_and_without_a_new_enough_runtime():
    assert "administrator" in app_main.refusal(True, {"build": 9999})
    assert "WebView2 Runtime" in app_main.refusal(False, None)
    assert "newer WebView2 Runtime" in app_main.refusal(False, {"build": app_main.MIN_BUILD - 1, "version": "x"})
    assert app_main.refusal(False, {"build": app_main.MIN_BUILD}) is None
    assert app_main.refusal(None, {"build": app_main.MIN_BUILD}) is None      # elevation unknown: not refused


def test_one_instance_per_home_and_a_tests_home_is_never_the_owners(tmp_path):
    a, b = app_main.instance_tag(tmp_path / "a"), app_main.instance_tag(tmp_path / "b")
    assert a != b and a == app_main.instance_tag(tmp_path / "a") and re.fullmatch(r"[0-9a-f]{12}", a)


def test_webview2s_own_variables_are_set_empty_while_it_starts_and_put_back_after(monkeypatch):
    for name in app_window.HARDENED_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "--remote-debugging-port=9222")
    before = app_window.harden_env()
    try:
        assert {n: wv.env_state(n) for n in app_window.HARDENED_ENV} == app_window.HARDENED_ENV
        assert wv.env_state("WEBVIEW2_USER_DATA_FOLDER") == ""             # present and empty, not absent
    finally:
        assert app_window.restore_env(before)
    assert wv.env_state("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS") == "--remote-debugging-port=9222"
    assert wv.env_state("WEBVIEW2_USER_DATA_FOLDER") is None


def test_an_access_violation_in_a_handler_closes_the_window_but_an_ordinary_error_does_not():
    w = app_window.AppWindow.__new__(app_window.AppWindow)
    w.fatal, w.stop_requested, w.log = [], False, lambda text: None
    call = w.guard("a handler", lambda a, b: (_ for _ in ()).throw(ValueError("bad")))
    assert call(0, 0, 0) == wv.E_FAIL and not w.stop_requested
    call = w.guard("a handler", lambda a, b: (_ for _ in ()).throw(OSError("exception: access violation reading 0x0")))
    assert call(0, 0, 0) == wv.E_FAIL and w.stop_requested and w.fatal
    assert not app_window.is_access_violation(wv.ComError("x", wv.E_FAIL))


def test_a_page_cannot_pile_up_work():
    w = app_window.AppWindow.__new__(app_window.AppWindow)
    import queue
    w.jobs, w.workers, w.hwnd = queue.Queue(), [object()] * app_window.WORKERS, 0
    for _ in range(app_window.MAX_QUEUED):
        assert w.submit(lambda: None, lambda r, e: None)
    assert not w.submit(lambda: None, lambda r, e: None)


def _window(tmp_path) -> app_window.AppWindow:
    got = []
    w = app_window.AppWindow(runtime={"version": "1.0.0.0"}, create_fn=None, data_folder=tmp_path / "webview",
                             assets=ASSETS, icon=None, class_name="LagDoctorAppTest", on_message=got.append,
                             on_failed=lambda why: None, log=lambda text: None)
    w.got = got
    return w


def _fake_message_args(source: str, text: str):
    """A stand-in ICoreWebView2WebMessageReceivedEventArgs: Source and the message's JSON, through its vtable."""
    def get_str(value):
        def get(this, out):
            out[0] = wv.co_wstr(value)
            return wv.S_OK
        return wv.GET_STR(get)
    fns = [wv.QI_PROTO(lambda this, riid, ppv: wv.E_NOINTERFACE), wv.REF_PROTO(lambda this: 1),
           wv.REF_PROTO(lambda this: 1), get_str(source), get_str(text)]
    vtbl = (ctypes.c_void_p * len(fns))(*[ctypes.cast(f, ctypes.c_void_p) for f in fns])
    cell = ctypes.c_void_p(ctypes.addressof(vtbl))
    return ctypes.addressof(cell), (fns, vtbl, cell)


def test_a_message_from_another_origin_never_reaches_the_app_and_one_from_its_page_does(tmp_path):
    w = _window(tmp_path)
    handler = w.handler("ICoreWebView2WebMessageReceivedEventHandler", wv.EVENT, w.on_web_message)
    request = json.dumps({"type": "request", "id": 5, "kind": "library"})
    forged, keep1 = _fake_message_args("https://evil.test/index.html", request)
    assert wv.vcall(handler.ptr, 3, (ctypes.c_void_p, ctypes.c_void_p), 0, forged) == wv.S_OK
    assert w.refused == {"message from another origin": 1} and not w.calls
    bad, keep2 = _fake_message_args(bridge_entry(), json.dumps({"type": "request", "id": 5, "kind": "nope"}))
    assert wv.vcall(handler.ptr, 3, (ctypes.c_void_p, ctypes.c_void_p), 0, bad) == wv.S_OK
    assert w.refused["malformed message"] == 1 and not w.calls
    genuine, keep3 = _fake_message_args(bridge_entry(), request)                        # the control
    assert wv.vcall(handler.ptr, 3, (ctypes.c_void_p, ctypes.c_void_p), 0, genuine) == wv.S_OK
    assert len(w.calls) == 1
    w.calls.popleft()()
    assert [(r.kind, r.id) for r in w.got] == [("library", 5)]


def _fake_resource_args(uri: str, fail: bool = False):
    """A stand-in ICoreWebView2WebResourceRequestedEventArgs: its get_Request gives a request whose get_Uri is `uri`
    (or fails, with `fail`). `asked` records, in order, what the window asked of them."""
    asked = []
    unknown = [wv.QI_PROTO(lambda this, riid, ppv: wv.E_NOINTERFACE), wv.REF_PROTO(lambda this: 1),
               wv.REF_PROTO(lambda this: 1)]

    def get_uri(this, out):
        asked.append("uri")
        out[0] = wv.co_wstr(uri)
        return wv.S_OK
    req_fns = unknown + [wv.GET_STR(get_uri)]
    req_vtbl = (ctypes.c_void_p * len(req_fns))(*[ctypes.cast(f, ctypes.c_void_p) for f in req_fns])
    req_cell = ctypes.c_void_p(ctypes.addressof(req_vtbl))

    def get_request(this, out):
        asked.append("request")
        if fail:
            return wv.E_FAIL
        out[0] = ctypes.addressof(req_cell)
        return wv.S_OK
    fns = unknown + [wv.GET_STR(get_request)]
    vtbl = (ctypes.c_void_p * len(fns))(*[ctypes.cast(f, ctypes.c_void_p) for f in fns])
    cell = ctypes.c_void_p(ctypes.addressof(vtbl))
    return ctypes.addressof(cell), asked, (fns, vtbl, cell, req_fns, req_vtbl, req_cell)


def test_the_art_host_refuses_each_request_first_and_answers_only_a_cover_it_holds(tmp_path):
    # The 404 goes on before the request is read: so a request that fails to read, or names no cover, keeps it, and
    # nothing goes to the network for the art host (which has no folder and no server).
    w = _window(tmp_path)
    held = "g-0123456789abcdef"
    w.art = lambda gid: ("image/png", b"made-up") if gid == held else None
    handler = w.handler("ICoreWebView2WebResourceRequestedEventHandler", wv.EVENT, w.on_web_resource)
    kept = []
    for uri, fail, want in [(f"https://art.lagdoctor.example/{held}", False, [404, "request", "uri", 200]),
                            ("https://art.lagdoctor.example/g-0000000000000000", False, [404, "request", "uri"]),
                            ("https://art.lagdoctor.example/sessions/x", False, [404, "request", "uri"]),
                            (f"https://art.lagdoctor.example/{held}", True, [404, "request"])]:
        args, asked, keep = _fake_resource_args(uri, fail)
        kept.append(keep)
        w.respond = lambda a, status, reason, image, asked=asked: asked.append(status)
        hr = wv.vcall(handler.ptr, 3, (ctypes.c_void_p, ctypes.c_void_p), 0, args)
        assert asked == want and (hr == wv.S_OK) is not fail, (uri, fail, asked, hr)
    assert w.refused["art"] == 2 and w.fatal == []


def bridge_entry() -> str:
    from lagdoctor.app import bridge
    return bridge.ENTRY


def test_the_views_settings_are_exactly_these():
    # Written out here, not read from the module: a setting turned on by mistake would still read back as set.
    assert {prop: value for _, prop, value in app_window.SETTINGS} == {
        "IsScriptEnabled": True, "IsWebMessageEnabled": True, "AreDefaultScriptDialogsEnabled": False,
        "IsStatusBarEnabled": False, "AreDevToolsEnabled": False, "AreDefaultContextMenusEnabled": False,
        "AreHostObjectsAllowed": False, "IsZoomControlEnabled": False, "IsBuiltInErrorPageEnabled": False,
        "AreBrowserAcceleratorKeysEnabled": False, "IsPasswordAutosaveEnabled": False,
        "IsGeneralAutofillEnabled": False, "IsPinchZoomEnabled": False, "IsSwipeNavigationEnabled": False,
        "IsReputationCheckingRequired": False}
    assert app_window.OPTION_BOOLS == {"AllowSingleSignOnUsingOSPrimaryAccount": False,
                                       "ExclusiveUserDataFolderAccess": True, "IsCustomCrashReportingEnabled": True}
    assert app_window.HARDENED_ENV == {"WEBVIEW2_PIPE_FOR_SCRIPT_DEBUGGER": "",
                                       "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS": "",
                                       "WEBVIEW2_RELEASE_CHANNEL_PREFERENCE": "0",
                                       "WEBVIEW2_BROWSER_EXECUTABLE_FOLDER": "", "WEBVIEW2_USER_DATA_FOLDER": ""}


def test_the_app_never_opens_devtools():
    # OpenDevToolsWindow opens DevTools even with DevTools turned off (M0): the app has no way to call it.
    assert all("OpenDevToolsWindow" not in methods for methods in wv.SLOTS.values())
    for p in sorted((ROOT / "lagdoctor").rglob("*.py")):
        assert "OpenDevToolsWindow" not in p.read_text(encoding="utf-8").replace(
            "never calls OpenDevToolsWindow", "").replace("never call OpenDevToolsWindow", ""), p


def test_only_a_folder_holding_its_capture_is_a_recording_the_page_can_ask_for(tmp_path):
    from lagdoctor.app import library
    from lagdoctor.app.main import App, Gone
    from lagdoctor.report import summary
    sessions = tmp_path / "sessions"
    gone = sessions / "20261104-194500-gone"           # its summary says a recording, but the capture was deleted
    gone.mkdir(parents=True)
    (gone / summary.SUMMARY_NAME).write_text(json.dumps({"schema": summary.SCHEMA, "kind": "watch"}), encoding="utf-8")
    check = sessions / "20261105-090000-check-up"
    check.mkdir()
    entries = library.scan(sessions)
    assert {e.id for e in entries} == {gone.name, check.name} and not any(e.is_recording for e in entries)
    app = App.__new__(App)
    app.sessions, app.entries = sessions, []
    import threading
    app.lock = threading.Lock()
    for sid in (gone.name, check.name, "20261106-000000-never"):
        with pytest.raises(Gone):
            app.entry(sid)


class _FakeWindow:
    def __init__(self, fail_buffer=False):
        self.posted, self.fail_buffer = [], fail_buffer

    def post(self, obj):
        self.posted.append(obj)
        return True

    def post_buffer(self, data, meta):
        if self.fail_buffer:
            raise wv.ComError("ICoreWebView2Environment12::CreateSharedBuffer", wv.E_OUTOFMEMORY)
        return 7


def _app(window, opened):
    from lagdoctor.app.main import App
    app = App.__new__(App)
    app.window, app.opener, app.log, app.generic_error = window, opened.append, lambda text: None, "generic"
    return app


def test_a_file_the_page_asks_to_open_is_opened_on_the_ui_thread_never_by_a_worker(tmp_path):
    # ShellExecute (os.startfile) wants COM, which the UI thread has (STA) and the workers do not.
    from lagdoctor.app import bridge
    from lagdoctor.app.main import Open
    opened: list[str] = []
    app = _app(_FakeWindow(), opened)
    app.sessions = tmp_path
    result = app.do_open_folder()                       # what a worker runs
    assert opened == [] and result == Open(tmp_path)
    app.answer(bridge.Request("open_folder", 4), result, None)    # what the UI thread runs
    assert opened == [str(tmp_path)] and app.window.posted == [bridge.reply(4, {})]


def test_frame_times_that_cannot_be_handed_over_are_said_on_the_chart_not_shown_as_none_recorded():
    from lagdoctor.app import bridge
    app = _app(_FakeWindow(fail_buffer=True), [])
    view = {"frames": {"missing": ""}, "game": "x"}
    app.answer(bridge.Request("results", 5), (view, (b"\0" * 16, {"kind": "frames", "count": 1})), None)
    data = app.window.posted[0]["data"]
    assert data["frames_token"] is None and data["frames"]["missing"].startswith("The frame times could not be handed")
    ok = _app(_FakeWindow(), [])
    ok.answer(bridge.Request("results", 6), (view, (b"\0" * 16, {"kind": "frames", "count": 1})), None)
    assert ok.window.posted[0]["data"]["frames_token"] == 7 and ok.window.posted[0]["data"]["frames"]["missing"] == ""
