"""The advice never tells a player to close the store app that started the game or Windows' shell, and the Memory
Compression advice names the programs that held the RAM.

PR #51 left three gaps (DECISIONS' backlog, 2026-11-16): background_hogs and gpu_contention could still tell a Steam
game's player to pause Steam's own programs, which the memory, disk and CPU advice no longer did; background_hogs'
Memory Compression advice said "close memory-heavy programs" without a name, although since 2026-11-16 the others
table holds each program's RAM; and explorer.exe, Windows' shell, was a program to close: Windows runs it from the
Windows folder itself, not System32, so the folder check could not tell the real one from a look-alike.
"""
from __future__ import annotations

import pytest

from lagdoctor.analysis import rules
from lagdoctor.capture import sysmon
from tests.test_analysis_rules import (GAME, STARVED, contention, is_finding, kernel_hog_capture, lift,
                                       on_the_games_adapter, run, stutter, where_hog_capture)
from tests.test_analysis_rules import hogs as hog_capture

CT = 134000000000000000
CLOSE_ONE = "Check it is not something you need first; lag-doctor never closes programs itself."
RAM_NOTE = "Each figure is the RAM that program alone holds, as Task Manager's Memory column shows it."
CPU_RISE = {"background_cpu": lift("supports", a=10, n=10)}


def started_by(cap, launcher):
    cap.inventory["game"] = dict(GAME, launcher=launcher)
    return cap


def priority_pids(res):
    return [f.params["pid"] for f in res.fixes if f.fix_id == "background_priority"]


# ----------------------------------------------------------------- the store app that started the game ---

STORE_HOGS = [(6100, "steamwebhelper.exe", (2.5, 5.0, 7.5, 10.0, 12.5)), (20000, "hog.exe", (15.0, 17.5, 20.0)),
              (30000, "other.exe", (22.5, 25.0))]


@pytest.mark.parametrize("launcher, name, store", [("steam", "steamwebhelper.exe", "Steam"),
                                                   ("steam", "Steam.exe", "Steam"),
                                                   ("epic", "EpicWebHelper.exe", "the Epic Games Launcher")])
def test_background_hogs_never_tells_the_player_to_close_the_store_app_that_started_the_game(launcher, name, store):
    cap, evs = kernel_hog_capture([(6100, name, STORE_HOGS[0][2])] + STORE_HOGS[1:])
    res = run("background_hogs", started_by(cap, launcher), events=evs, causes=CPU_RISE)
    assert is_finding(res)
    assert res.title == f"Background programs got busy when the game lagged: {name}, hog.exe, other.exe"
    titles = [f.title for f in res.fixes]
    assert not any(t.startswith(f"Pause or close {name}") for t in titles), titles
    said = [f for f in res.fixes if f.title == f"{name} (process 6100) is part of {store}, which started the game"]
    assert len(said) == 1, titles
    assert said[0].detail == "The game may need it running, so lag-doctor does not suggest closing it."
    # neither lowered: the two priority slots go to the programs that can be, in blame order
    assert priority_pids(res) == [20000, 30000]
    assert "Pause or close hog.exe while you play (process 20000)" in titles
    assert "Pause or close other.exe while you play (process 30000)" in titles


@pytest.mark.parametrize("launcher", [None, "epic", "battle.net", 7])
def test_a_store_apps_program_stays_a_program_to_close_when_that_store_did_not_start_the_game(launcher):
    cap, evs = kernel_hog_capture(STORE_HOGS)
    res = run("background_hogs", started_by(cap, launcher), events=evs, causes=CPU_RISE)
    assert is_finding(res)
    assert "Pause or close steamwebhelper.exe while you play (process 6100)" in [f.title for f in res.fixes]
    assert priority_pids(res) == [6100, 20000]


def steam_on_the_games_adapter():
    rows = on_the_games_adapter(12.0)
    for _t, procs in rows:
        procs[1][1] = "steamwebhelper.exe"
    return rows


def test_gpu_contention_says_the_store_apps_program_is_part_of_it_and_never_to_close_or_move_it():
    res = run("gpu_contention", started_by(contention(80.0, others=steam_on_the_games_adapter()), "steam"),
              stats={"avg_fps": 58.0}, bound=STARVED)
    assert is_finding(res) and res.title == "Another program kept the game's GPU 80% busy: steamwebhelper.exe"
    assert [(f.title, f.detail) for f in res.fixes] == [
        ("steamwebhelper.exe (process 2656) is part of Steam, which started the game",
         "The game may need it running, so lag-doctor does not suggest closing it.")]
    # started some other way, it is a program to close like any other
    res = run("gpu_contention", contention(80.0, others=steam_on_the_games_adapter()), stats={"avg_fps": 58.0},
              bound=STARVED)
    assert [f.title for f in res.fixes] == ["Pause or close steamwebhelper.exe while you play",
                                            "Or let that work run on another GPU, if this PC has one"]


# ----------------------------------------------------------- Memory Compression names who held the RAM ---

TIMES = (2.5, 5.0, 7.5, 10.0, 12.5)


def whole_second(t):
    return t == int(t)


def compression_capture(*holders, times=TIMES, memory_at=whole_second, busy=None):
    """Memory Compression at 150 % of a CPU around each stutter time, in rows of 12 fields as the sampler writes them
    since 2026-11-16: memory read on the rows `memory_at` picks (once a second), None between, and a program listed
    for its memory only on those. Holders are (pid, name, RAM in MiB, when(t) -> held?, session); a holder whose pid
    is in `busy` (pid -> stutter times of its own) is listed on every row, at 150 % of a CPU around those."""
    busy = busy or {}
    rows = []
    for k in range(1, 121):
        t = k * 0.25
        on = memory_at(t)
        hot = any(te - 0.5 <= t <= te + 0.4 for te in times)

        def mem(v):
            return [v, v, 0] if on else [None, None, None]

        procs = [[7777, "python.exe", 100.0, 0.0, 100.0, CT + 7777, None, 1, False, *mem(40)],
                 [300, "Memory Compression", 150.0 if hot else 0.0, 0.0, 0.0, CT + 300, None, 0, None, *mem(1500)]]
        for pid, name, ram, when, *session in holders:
            own = busy.get(pid, ())
            cpu = 150.0 if any(te - 0.5 <= t <= te + 0.4 for te in own) else 0.0
            if own or (on and when(t)):
                procs.append([pid, name, cpu, 0.0, 0.0, CT + pid, None, *(session or [1]), False,
                              *mem(ram if when(t) else 0)])
        rows.append([t, procs])
    evs = [stutter(i, te, "background_cpu", process="Memory Compression", process_pids=[300],
                   process_rises=[[300, 150.0]]) for i, te in enumerate(times, 1)]
    for pid, name, *_rest in holders:
        own = busy.get(pid, ())
        evs += [stutter(len(evs) + 1, te, "background_cpu", process=name, process_pids=[pid],
                        process_rises=[[pid, 150.0]]) for te in own]
    return hog_capture(rows), evs


def always(t):
    return True


def compression_fixes(cap, evs):
    res = run("background_hogs", cap, events=evs, causes={"background_cpu": lift("supports", a=len(evs), n=len(evs))})
    assert is_finding(res)
    assert res.title == "Windows itself got busy when the game lagged: Memory Compression"
    titles = [f.title for f in res.fixes]
    i = titles.index("Memory Compression (process 300) is Windows itself, not a program to close")
    assert "RAM runs short" in res.fixes[i].detail
    return res.fixes, i


def test_memory_compressions_advice_names_the_programs_that_held_the_most_ram_when_it_got_busy():
    # chrome.exe held 3 GB at every stutter; Editor.exe 5 GB only from 20 s on, after the last stutter's window
    cap, evs = compression_capture((500, "chrome.exe", 3072, always), (510, "Editor.exe", 5120, lambda t: t >= 20.0))
    fixes, i = compression_fixes(cap, evs)
    held = fixes[i + 1]                                  # right after what Memory Compression is
    assert held.title == "Pause or close chrome.exe while you play"
    assert held.detail == ("When Memory Compression got busy, besides the game, chrome.exe held at least 3.0 GB of RAM "
                           f"on average. {RAM_NOTE} {CLOSE_ONE}")
    assert not any("Editor.exe" in f.title or "Editor.exe" in f.detail for f in fixes)
    assert not any(f.fix_id for f in fixes)              # nothing to lower: Windows' own, and a program never blamed


def test_a_stutter_between_two_memory_readings_meets_the_nearest():
    # memory is read on whole seconds; a stutter from x.55 to x.7 s is looked at from x.05 to x.95 s, so only the
    # half second each side reaches a reading
    cap, evs = compression_capture((500, "chrome.exe", 3072, always), times=(2.55, 5.55, 8.55, 11.55, 14.55))
    fixes, i = compression_fixes(cap, evs)
    assert fixes[i + 1].title == "Pause or close chrome.exe while you play"


def test_under_the_floor_memory_compressions_advice_says_the_most_any_program_held_and_never_itself():
    cap, evs = compression_capture((500, "chrome.exe", 400, always))
    fixes, i = compression_fixes(cap, evs)
    held = fixes[i + 1]
    assert held.title == rules.MEM_ADVICE
    assert held.detail == ("Browsers with many tabs are the usual ones. lag-doctor saw no other program hold 500 MB or "
                           "more of RAM on average when Memory Compression got busy; the most was chrome.exe, 400 MB."
                           + rules.HELD_LIMIT)
    # Memory Compression holds 1.5 GB itself, which this advice never names: it is what it explains
    assert "Memory Compression" not in held.detail.replace("when Memory Compression got busy", "")


def test_memory_compressions_line_is_left_out_where_it_would_only_repeat_a_program_background_hogs_names():
    # chrome.exe held 3 GB and its CPU rose at three stutters of its own: background_hogs already says to close it
    cap, evs = compression_capture((500, "chrome.exe", 3072, always), busy={500: (17.5, 20.0, 22.5)})
    res = run("background_hogs", cap, events=evs, causes={"background_cpu": lift("supports", a=8, n=8)})
    assert is_finding(res)
    assert res.title == ("Background programs got busy when the game lagged: Windows itself (Memory Compression), "
                         "chrome.exe")
    titles = [f.title for f in res.fixes]
    assert [t for t in titles if t.startswith("Pause or close chrome.exe")] == [
        "Pause or close chrome.exe while you play (process 500)"], titles
    assert rules.MEM_ADVICE not in titles
    # with a program beside it that background_hogs does not name, or a virtual machine's memory, the line says so
    for other, says in (((510, "Discord.exe", 800, always), "and Discord.exe 800 MB."),
                        ((3100, "vmmem", 2048, always, 0), "vmmem held at least 2.0 GB of RAM too")):
        cap, evs = compression_capture((500, "chrome.exe", 3072, always), other, busy={500: (17.5, 20.0, 22.5)})
        res = run("background_hogs", cap, events=evs, causes={"background_cpu": lift("supports", a=8, n=8)})
        held = [f for f in res.fixes if f.detail.startswith("When Memory Compression got busy")]
        assert len(held) == 1 and says in held[0].detail, [f.title for f in res.fixes]


def test_memory_compression_in_an_older_capture_keeps_its_old_advice_alone():
    cap, evs = kernel_hog_capture([(300, "Memory Compression", TIMES)])
    res = run("background_hogs", cap, events=evs, causes={"background_cpu": lift("supports", a=5, n=5)})
    assert is_finding(res)
    assert [f.title for f in res.fixes] == ["Memory Compression (process 300) is Windows itself, not a program to close",
                                            "Pause downloads and updates while playing"]


# ---------------------------------------------------------------------------- explorer.exe, Windows' shell ---

SHELL_DETAIL = ("That name is normally Windows' shell, which runs the taskbar, the Start menu and File Explorer, so "
                "lag-doctor does not suggest closing it.")


def test_the_recorder_and_the_analysis_agree_on_which_programs_windows_runs_from_its_own_folder():
    assert sysmon.WINDOWS_FOLDER_PROGRAMS == rules.WINDOWS_FOLDER_PARTS
    assert rules.WINDOWS_FOLDER_PARTS <= set(rules.WINDOWS_PARTS)
SHELL_OWN = "explorer.exe (process 5120) is normally part of Windows, not a program to close"


@pytest.mark.parametrize("folder, checked, own", [
    (True, True, True),        # the real one: directly in the Windows folder, which the recorder checked
    (False, True, False),      # a look-alike: the recorder checked the Windows folder, and it is not there
    (False, None, True),       # a capture from before the check covered the Windows folder: its name decides
    (False, "yes", True),      # ... and a marker that is not True is no marker
    (None, True, True),        # where it runs was not read: its name decides
])
def test_explorer_is_windows_shell_where_windows_runs_it_and_a_look_alike_is_not(folder, checked, own):
    cap, evs = where_hog_capture([(5120, "explorer.exe", (2.5, 5.0, 7.5, 10.0, 12.5), 1, folder),
                                  (20000, "hog.exe", (15.0, 17.5, 20.0), 1, False)])
    if checked is not None:
        cap.target["windows_folder"] = checked
    res = run("background_hogs", cap, events=evs, causes={"background_cpu": lift("supports", a=8, n=8)})
    assert is_finding(res)
    titles = [f.title for f in res.fixes]
    if own:
        said = [f for f in res.fixes if f.title == SHELL_OWN]
        assert len(said) == 1 and said[0].detail == SHELL_DETAIL, titles
        assert not any(t.startswith("Pause or close explorer.exe") for t in titles), titles
        assert priority_pids(res) == [20000]
    else:
        assert f"Pause or close explorer.exe{rules.LOOKALIKE} while you play (process 5120)" in titles, titles
        assert SHELL_OWN not in titles
        assert priority_pids(res) == [5120, 20000]


def test_explorer_in_a_capture_of_names_only_is_said_to_be_windows_shell():
    cap, evs = kernel_hog_capture([(5120, "explorer.exe", (2.5, 5.0, 7.5, 10.0, 12.5)),
                                   (20000, "hog.exe", (15.0, 17.5, 20.0))])
    res = run("background_hogs", cap, events=evs, causes={"background_cpu": lift("supports", a=8, n=8)})
    assert is_finding(res) and SHELL_OWN in [f.title for f in res.fixes]
    assert priority_pids(res) == [20000]
