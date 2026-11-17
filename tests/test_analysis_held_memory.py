"""The advice to close memory-heavy and GPU-heavy programs names the ones that held the most.

ram_pressure said "Close memory-heavy programs while playing" and vram_overflow "Close other GPU-heavy programs while
playing" without a name: the recorder kept no process's memory. Since 2026-11-16 each others entry says the RAM it alone
holds, the memory committed to it and its video memory on the game's GPU (fields 9-11), and the table lists the 4 of the
rest holding the most of each (sysmon.top_others). rules._held_advice reads them at the ticks the finding's own
condition held (free RAM low, committed memory near the limit, video memory full), as the disk advice reads the
stutters' windows. An older capture keeps the old line, word for word.
"""
from __future__ import annotations

import pytest

from lagdoctor.analysis import crop, rules
from tests import synth
from tests.test_analysis_rules import NV, is_finding, make, run

CT = 134000000000000000


def ticks(seconds=30.0):
    return [0.25 * k for k in range(1, int(seconds * 4) + 1)]


def table(*users, seconds=30.0, quiet=True):
    """An others table as the sampler writes it since 2026-11-16 (12 fields). Users are (pid, name, ram, commit, vram,
    when(t) -> listed?, session, folder), in MiB; a quiet process holding little is on every tick, as on a real one."""
    rows = []
    for t in ticks(seconds):
        procs = [[8, "Quiet.exe", 0.5, 0.0, 0.0, CT + 8, None, 1, False, 40, 60, 0]] if quiet else []
        for pid, name, ram, commit, vram, when, *where in users:
            session, folder = where or (1, False)
            if when(t):
                procs.append([pid, name, 0.0, 0.0, 0.0, CT + pid, None, session, folder, ram, commit, vram])
        rows.append([t, procs])
    return rows


def always(t):
    return True


def low(t):
    return 10.0 <= t < 20.0


def ram_low(others, *, avail=lambda t: 1000.0 if low(t) else 4000.0, target=None, inventory=None, **cols):
    """ram_pressure on 16 GB with free RAM at 1,000 MB (under 10 %) from 10 to 20 s only."""
    return run("ram_pressure", make(system=synth.system_table(30, ram_avail_mb=avail, **cols), others=others,
                                    target=target, inventory=inventory))


def mem_fix(res, title_starts=("Pause or close", rules.MEM_ADVICE)):
    assert is_finding(res)
    fixes = [f for f in res.fixes if f.title.startswith(title_starts)]
    assert len(fixes) == 1, [f.title for f in res.fixes]
    return fixes[0]


CLOSE = "Check they are not something you need first; lag-doctor never closes programs itself."
RAM_NOTE = "Each figure is the RAM that program alone holds, as Task Manager's Memory column shows it."


# ------------------------------------------------------------------------------------- RAM ---

def test_ram_pressure_names_the_programs_that_held_the_most_ram_while_free_ram_was_low():
    # chrome.exe's two processes held 2,000 and 600 MB while free RAM was low; Discord.exe 700 MB; Small.exe 400 MB is
    # under the floor; Editor.exe held 5,000 MB only after free RAM recovered, so it is never named for it
    res = ram_low(table((500, "chrome.exe", 2000, 2100, 0, always), (501, "chrome.exe", 600, 650, 0, low),
                        (310, "Discord.exe", 700, 800, 0, always), (320, "Small.exe", 400, 450, 0, always),
                        (330, "Editor.exe", 5000, 5100, 0, lambda t: t >= 20.0)))
    fix = mem_fix(res)
    assert res.fixes[0] is fix                                   # the first fix, where the generic line was
    assert fix.title == "Pause or close chrome.exe and Discord.exe while you play"
    assert fix.detail == ("While free RAM was low, besides the game, chrome.exe (2 processes) held at least 2.5 GB of "
                          f"RAM on average, and Discord.exe 700 MB. {RAM_NOTE} {CLOSE}")


def test_a_mean_counts_the_rows_a_program_was_not_listed_on_as_nothing():
    # free RAM low all along; listed on half of the 120 rows that read memory at 1,200 MB: at least 600 MB, never 1,200
    res = ram_low(table((310, "Discord.exe", 1200, 1300, 0, lambda t: round(t / 0.25) % 2 == 0)),
                  avail=lambda t: 1000.0)
    assert "Discord.exe held at least 600 MB of RAM on average" in mem_fix(res).detail


@pytest.mark.parametrize("mb, named", [(500, True), (499, False)])
def test_a_program_is_named_from_500_mb_and_under_it_the_most_is_still_said(mb, named):
    fix = mem_fix(ram_low(table((310, "Discord.exe", mb, mb, 0, always))))
    if named:
        assert fix.title == "Pause or close Discord.exe while you play"
    else:
        assert fix.title == rules.MEM_ADVICE
        assert fix.detail == ("Browsers with many tabs are the usual ones. lag-doctor saw no other program hold 500 MB "
                              "or more of RAM on average while free RAM was low; the most was Discord.exe, 499 MB. "
                              "lag-doctor lists the 4 processes holding the most at each moment, so a program split "
                              "into many smaller processes, such as a browser, can hold more than that.")


def test_figures_from_1000_mb_are_said_in_gb():
    assert rules._size(999.0) == "999 MB" and rules._size(1000.0) == "1.0 GB" and rules._size(2600.0) == "2.5 GB"


def test_windows_own_memory_is_said_to_be_windows_and_never_told_to_close():
    fix = mem_fix(ram_low(table((3000, "Memory Compression", 1500, 0, 0, always, 0, None))))
    assert fix.title == rules.MEM_ADVICE
    assert fix.detail == ("While free RAM was low, besides the game, only Windows' own processes held 500 MB or more "
                          "of RAM on average. Windows itself (Memory Compression) held at least 1.5 GB of RAM: that is "
                          "Windows compressing memory because RAM runs short: close memory-heavy programs, such as a "
                          f"browser with many tabs, while you play. {RAM_NOTE}")
    beside = mem_fix(ram_low(table((3000, "Memory Compression", 1500, 0, 0, always, 0, None),
                                   (500, "chrome.exe", 900, 950, 0, always))))
    assert beside.title == "Pause or close chrome.exe while you play"
    assert beside.detail.endswith("Windows itself (Memory Compression) held at least 1.5 GB of RAM too: that is "
                                  "Windows compressing memory because RAM runs short: close memory-heavy programs, "
                                  "such as a browser with many tabs, while you play.")


@pytest.mark.parametrize("pid, name, target", [
    (4242, "game.exe", None),                                  # the game itself
    (4300, "helper.exe", {"pids": [4242, 4300]}),              # one of the game's own child processes
    (5555, "python.exe", {"tool_pids": [5555]}),               # lag-doctor's own process
    (9999, "GAME.EXE", None),                                  # the game's image name under another PID
    (7000, "bad" + chr(0x200B) + "name.exe", None),            # a name no fix or advice may show
])
def test_the_game_its_helpers_lag_doctor_and_bad_names_are_never_named(pid, name, target):
    fix = mem_fix(ram_low(table((pid, name, 3000, 3000, 0, always)), target=target))
    assert fix.title == rules.MEM_ADVICE and "3.0 GB" not in fix.detail
    # control: the same row under an ordinary name is named
    assert mem_fix(ram_low(table((7100, "Other.exe", 3000, 3000, 0, always)), target=target)).title.startswith("Pause")


def test_an_older_capture_whose_table_holds_no_memory_keeps_the_old_line_word_for_word():
    nine = [[t, [[310, "Discord.exe", 1.0, 0.0, 0.0, CT + 310, None, 1, False]]] for t in ticks()]
    for others in (nine, [], table((310, "Discord.exe", None, None, None, always), quiet=False)):
        fix = mem_fix(ram_low(others))
        assert (fix.title, fix.detail) == (rules.MEM_ADVICE, rules.BROWSERS)


def test_a_reading_that_is_no_number_is_left_out_and_breaks_nothing():
    rows = table((310, "Discord.exe", 900, 900, 0, always))
    for _t, procs in rows:
        procs.append([311, "Bad.exe", 0.0, 0.0, 0.0, CT, None, 1, False, "lots", True, float("inf")])
        procs.append([315, "Flag.exe", 0.0, 0.0, 0.0, CT, None, 1, False, True, True, True])     # bools: no reading
        procs.append([312, "Neg.exe", 0.0, 0.0, 0.0, CT, None, 1, False, -5000, 1e308, None])
        procs.append([313, "Short.exe", 0.0, 0.0, 0.0, CT, None, 1, False, 9000])      # 10 fields: no memory read
        procs.append([314, "Discord.exe", 0.0, 0.0, 0.0, CT, None, 1, False, -5000, -5000, -5000])   # never subtracts
    fix = mem_fix(ram_low(rows))
    assert fix.title == "Pause or close Discord.exe while you play"
    assert "Discord.exe held at least 900 MB of RAM on average" in fix.detail
    assert not any(n in fix.detail for n in ("Bad.exe", "Neg.exe", "Short.exe", "Flag.exe"))
    # a table whose memory fields are all bools read no memory: the old line, never "saw no other program"
    flags = [[t, [[315, "Flag.exe", 0.0, 0.0, 0.0, CT, None, 1, False, True, False, True]]] for t in ticks()]
    assert (mem_fix(ram_low(flags)).detail) == rules.BROWSERS


def test_with_free_ram_never_low_the_whole_capture_is_read():
    # hard faults made the finding on a PC whose RAM size is unknown: no tick was under a floor, so the whole capture
    fix = mem_fix(ram_low(table((310, "Discord.exe", 900, 900, 0, always)), avail=lambda t: 1000.0,
                          inventory={"ram_gb": None}, hard_faults_ps=5000.0))
    assert fix.detail.startswith("During the capture, besides the game, Discord.exe held at least 900 MB of RAM")


@pytest.mark.parametrize("t0, t1, named", [(14.0, 30.0, False), (6.0, 17.0, True), (None, None, True)])
def test_a_measured_part_reads_only_the_low_ticks_it_holds(t0, t1, named):
    # Discord.exe held 2,000 MB from 10 to 15 s: half the capture's low ticks (1,000 MB on average), most of 6-17 s's,
    # and 4 of the 24 in 14-30 s (about 330 MB, under the floor)
    cap = make(system=synth.system_table(30, ram_avail_mb=lambda t: 1000.0 if low(t) else 4000.0),
               others=table((310, "Discord.exe", 2000, 2000, 0, lambda t: 10.0 <= t < 15.0)))
    fix = mem_fix(run("ram_pressure", crop.crop(cap, t0, t1) if t0 is not None else cap))
    assert fix.title.startswith("Pause or close Discord.exe") is named


def test_naming_a_program_never_moves_the_findings_score():
    quiet, heavy = ram_low(table()), ram_low(table((310, "Discord.exe", 3000, 3000, 0, always)))
    assert (quiet.confidence, quiet.score, quiet.evidence) == (heavy.confidence, heavy.score, heavy.evidence)
    assert mem_fix(quiet).title != mem_fix(heavy).title


# ----------------------------------------------------------------------- committed memory ---

def commit_full(others):
    """ram_pressure's page-file branch: RAM stayed free while committed memory reached 99.9 % for the first 15 s."""
    return ram_low(others, avail=lambda t: 8000.0, commit_pct=lambda t: 99.9 if t < 15.0 else 50.0)


def test_the_page_file_branch_names_the_programs_that_reserved_the_most_while_committed_memory_was_near_the_limit():
    # VM.exe reserved 6,000 MB while holding little RAM; Tool.exe reserved 3,000 MB only after the commit charge fell
    res = commit_full(table((600, "VM.exe", 100, 6000, 0, always), (610, "Tool.exe", 100, 3000, 0, lambda t: t >= 15)))
    assert res.title.startswith("Windows nearly ran out of memory to hand out")
    fix = mem_fix(res)
    assert res.fixes[1] is fix
    assert fix.title == "Pause or close VM.exe while you play"
    assert fix.detail == ("While committed memory was near the limit, besides the game, VM.exe reserved at least 5.9 "
                          "GB of memory on average. Reserved memory counts toward what Windows can promise even where "
                          "it is not in RAM: Task Manager's Commit size, on its Details tab. Check it is not something "
                          "you need first; lag-doctor never closes programs itself.")


def test_the_page_file_branch_under_the_floor_says_the_most_and_keeps_its_old_words():
    fix = mem_fix(commit_full(table((600, "VM.exe", 100, 300, 0, always))))
    assert fix.title == rules.MEM_ADVICE
    assert fix.detail == ("Browsers with many tabs are the usual ones. lag-doctor saw no other program reserve 500 MB "
                          "or more of memory on average while committed memory was near the limit; the most was VM.exe, "
                          "300 MB." + rules.HELD_LIMIT)


# --------------------------------------------------------------------------- video memory ---

def vram_full(others):
    """vram_overflow on the 8 GB NVIDIA GPU, full (8,000 MB) for the first 30 s of 60."""
    sys_ = synth.system_table(60, vram_used_mb=lambda t: 8000.0 if t < 30.0 else 4000.0, game_shared_mb=140.0)
    return run("vram_overflow", make(adapter=NV, system=sys_, duration=60.0, others=others),
               bound={"verdict": "gpu"})


def vram_fix(res):
    return mem_fix(res, ("Pause or close", rules.VRAM_ADVICE))


def test_vram_overflow_names_the_programs_that_held_the_most_video_memory_while_it_was_full():
    # OBS.exe held 1,200 MB while video memory was full; Viewer.exe 2,000 MB only once it was not; dwm.exe, Windows'
    # own (from System32), 400 MB
    res = vram_full(table((700, "OBS.exe", 300, 400, 1200, lambda t: t < 30.0),
                          (710, "Viewer.exe", 300, 400, 2000, lambda t: t >= 30.0),
                          (720, "dwm.exe", 100, 100, 400, always, 1, True), seconds=60.0))
    fix = vram_fix(res)
    assert res.fixes[2] is fix
    assert fix.title == "Pause or close OBS.exe while you play"
    assert fix.detail == ("While video memory was full, besides the game, OBS.exe held at least 1.2 GB of video memory "
                          "on the game's GPU on average. Check it is not something you need first; lag-doctor never "
                          "closes programs itself. dwm.exe held at least 400 MB of video memory on the game's GPU too: "
                          "that name is normally Windows' Desktop Window Manager, which draws every window on the "
                          "screen, so lag-doctor does not suggest closing it.")


@pytest.mark.parametrize("mb, named", [(250, True), (249, False)])
def test_a_program_is_named_from_250_mb_of_video_memory(mb, named):
    fix = vram_fix(vram_full(table((700, "OBS.exe", 0, 0, mb, always), seconds=60.0)))
    if named:
        assert fix.title == "Pause or close OBS.exe while you play"
    else:
        assert (fix.title, fix.detail) == (rules.VRAM_ADVICE, "lag-doctor saw no other program hold 250 MB or more of "
                                           "video memory on the game's GPU on average while video memory was full; "
                                           "the most was OBS.exe, 249 MB." + rules.HELD_LIMIT)


def test_vram_overflow_in_an_older_capture_keeps_the_old_line():
    nine = [[t, [[700, "OBS.exe", 1.0, 0.0, 0.0, CT + 700, None, 1, False]]] for t in ticks(60.0)]
    fix = vram_fix(vram_full(nine))
    assert (fix.title, fix.detail) == (rules.VRAM_ADVICE, "")


@pytest.mark.parametrize("from_s, named", [(0.0, True), (31.0, False)])
def test_a_memory_reading_more_than_half_a_second_from_every_full_tick_is_not_counted(from_s, named):
    # video memory was full up to 29.75 s, so the readings counted end at 30.25 s; OBS.exe held 1,200 MB from `from_s`
    # on: all along it is named, from 31 s it is not
    fix = vram_fix(vram_full(table((700, "OBS.exe", 0, 0, 1200, lambda t: t >= from_s), seconds=60.0)))
    assert fix.title.startswith("Pause or close OBS.exe") is named, fix.detail


# ------------------------------------------------------------------------- after review ---

@pytest.mark.parametrize("mean_of, said", [
    (lambda t: 999.7, "just under 1.0 GB"),                        # a floor said in GB: 999.7 rounds to it
    (lambda t: 499.7, "just under 500 MB"),                        # rounds to the floor: never "500 MB" under it
    (lambda t: 10 if t == 10.0 else 0, "under 1 MB"),              # one low tick of 40: 0.25 MB
])
def test_a_figure_under_the_floor_never_reads_as_the_floor(mean_of, said, monkeypatch):
    if "GB" in said:
        monkeypatch.setattr(rules, "MEM_MIN_MB", 1000.0)
    rows = [[t, [[310, "Discord.exe", 0.0, 0.0, 0.0, CT, None, 1, False, mean_of(t), 0, 0]]] for t in ticks()]
    fix = mem_fix(ram_low(rows))
    assert fix.title == rules.MEM_ADVICE and f"the most was Discord.exe, {said}." in fix.detail, fix.detail
    assert rules._size(999.6) == "1.0 GB" and rules._size(999.4) == "999 MB" and rules._size(0.4) == "0 MB"


def test_a_ram_finding_made_by_the_commit_charge_names_who_reserved_it():
    # the RAM size is unknown, so free RAM never reads low and the page-file branch cannot run: the finding is the
    # commit charge's (95 %), and a VM reserving 6,000 MB while holding 100 MB of RAM is who it names
    res = ram_low(table((600, "VM.exe", 100, 6000, 0, always)), avail=lambda t: 1000.0, inventory={"ram_gb": None},
                  commit_pct=95.0)
    fix = mem_fix(res)
    assert fix.title == "Pause or close VM.exe while you play"
    assert fix.detail.startswith("While committed memory was near the limit, besides the game, VM.exe reserved at "
                                 "least 5.9 GB of memory on average.")


@pytest.mark.parametrize("pct, named", [(80.0, False), (80.1, True)])
def test_committed_memory_is_near_the_limit_over_80_percent(pct, named):
    # 99.9 % for the first 10 s, `pct` from 10 to 20 s; Tool.exe reserved 3,000 MB only from 10 to 20 s
    res = ram_low(table((610, "Tool.exe", 100, 3000, 0, lambda t: 10.0 <= t < 20.0)), avail=lambda t: 8000.0,
                  commit_pct=lambda t: 99.9 if t < 10.0 else pct if t < 20.0 else 50.0)
    assert mem_fix(res).title.startswith("Pause or close Tool.exe") is named


def test_a_virtual_machines_memory_is_said_to_be_windows_and_how_to_give_it_back():
    fix = mem_fix(ram_low(table((900, "VmmemWSL", 4000, 4000, 0, always, 0, None))))
    assert fix.title == rules.MEM_ADVICE
    assert fix.detail == ("While free RAM was low, besides the game, only Windows' own processes held 500 MB or more "
                          "of RAM on average. VmmemWSL held at least 3.9 GB of RAM: that name is normally the memory "
                          "and CPU of a virtual machine Windows runs, such as the one behind WSL 2, so lag-doctor does "
                          "not suggest closing it. If you are not using it, shutting that virtual machine down frees "
                          f"what it holds: for WSL, run wsl --shutdown in a terminal. {RAM_NOTE}")


@pytest.mark.parametrize("launcher, store", [("steam", "Steam"), ("epic", None), ("other", None), (None, None)])
def test_the_store_app_that_started_the_game_is_never_told_to_close(launcher, store):
    # steamwebhelper.exe holds 800 MB; a Steam game may need Steam running, so it is said to be Steam's, never closed.
    # Under another store, or none, it is a program like any other
    from tests.test_analysis_rules import GAME
    res = ram_low(table((800, "steamwebhelper.exe", 800, 800, 0, always), (810, "Discord.exe", 700, 700, 0, always)),
                  inventory={"game": dict(GAME, launcher=launcher)})
    fix = mem_fix(res)
    if store:
        assert fix.title == "Pause or close Discord.exe while you play"
        assert fix.detail.endswith("steamwebhelper.exe held at least 800 MB of RAM too: that is part of Steam, which "
                                   "started the game and which it may need, so lag-doctor does not suggest closing it.")
        alone = mem_fix(ram_low(table((800, "steamwebhelper.exe", 800, 800, 0, always)),
                                inventory={"game": dict(GAME, launcher=launcher)}))
        assert alone.title == rules.MEM_ADVICE and alone.detail.startswith(
            "While free RAM was low, besides the game, only the store app that started the game held 500 MB")
        under = mem_fix(ram_low(table((800, "steamwebhelper.exe", 300, 300, 0, always)),
                                inventory={"game": dict(GAME, launcher=launcher)}))
        assert "the most was steamwebhelper.exe (part of Steam, which started the game), 300 MB." in under.detail
    else:
        assert fix.title == "Pause or close steamwebhelper.exe and Discord.exe while you play"
    epic = mem_fix(ram_low(table((820, "EpicWebHelper.exe", 800, 800, 0, always)),
                           inventory={"game": dict(GAME, launcher="epic")}))
    assert "part of the Epic Games Launcher" in epic.detail and epic.title == rules.MEM_ADVICE


def test_video_memory_not_full_is_read_over_the_whole_capture():
    # shared memory spilled while the events support the cause, but dedicated video memory never reached 90 %: no tick
    # is "full", so the advice reads the whole capture
    from tests.test_analysis_rules import lift, stutter
    sys_ = synth.system_table(60, vram_used_mb=4000.0, game_shared_mb=140.0)
    evs = [stutter(i, t, "vram_overflow", side="gpu") for i, t in enumerate((10.0, 20.0, 30.0, 40.0), 1)]
    from tests.test_analysis_rules import ADAPTERS
    for adapters in (ADAPTERS, [dict(a, vram_mb=None) for a in ADAPTERS]):     # not full; its size unknown
        res = run("vram_overflow", make(adapter=NV, system=sys_, duration=60.0, inventory={"adapters": adapters},
                                        others=table((700, "OBS.exe", 0, 0, 1200, always), seconds=60.0)),
                  bound={"verdict": "gpu"}, events=evs, causes={"vram_overflow": lift("supports", a=4, n=4)})
        assert vram_fix(res).detail.startswith("During the capture, besides the game, OBS.exe held at least 1.2 GB")


def test_the_memory_fields_change_nothing_but_the_memory_advice():
    # the same recording with and without fields 9-11: every finding, score, evidence and other fix is the same
    import copy as _copy
    from lagdoctor.analysis.diagnose import diagnose
    base = make(system=synth.system_table(30, ram_avail_mb=lambda t: 1000.0 if low(t) else 4000.0,
                                         vram_used_mb=8000.0, game_shared_mb=900.0),
                adapter=NV, others=table((500, "chrome.exe", 2000, 2100, 1200, always),
                                         (310, "Discord.exe", 700, 800, 0, always)))
    nine = _copy.deepcopy(base)
    nine.others = [[t, [q[:9] for q in procs]] for t, procs in base.others]
    a, b = diagnose(base), diagnose(nine)
    assert [f.cause for f in a.findings] == [f.cause for f in b.findings] and a.findings
    for x, y in zip(a.findings, b.findings):
        assert (x.confidence, x.score, [ev.text for ev in x.evidence]) == (y.confidence, y.score,
                                                                            [ev.text for ev in y.evidence])
        moved = [(p.title, q.title) for p, q in zip(x.fixes, y.fixes) if (p.title, p.detail) != (q.title, q.detail)]
        assert len(x.fixes) == len(y.fixes)
        assert all(q in (rules.MEM_ADVICE, rules.VRAM_ADVICE) for _p, q in moved), (x.cause, moved)
    assert (a.headline, a.cleared, a.unchecked) == (b.headline, b.cleared, b.unchecked)
    assert any(f.cause == "ram_pressure" for f in a.findings)


def test_gpu_contention_says_what_a_table_that_lists_memory_holders_lists():
    from tests.test_analysis_rules import STARVED, contention
    rows = [[0.25 * k, [[7777, "python.exe", 100.0, 0.0, 0.0, CT, 0.0, 1, False, 20, 20, 0]]] for k in range(1, 121)]
    res = run("gpu_contention", contention(80.0, others=rows), stats={"avg_fps": 58.0}, bound=STARVED)
    assert is_finding(res)
    assert any(ev.text.endswith("it lists the 8 busiest by CPU, and of the rest the 4 busiest by disk and the 4 "
                                "holding the most of each kind of memory).") for ev in res.evidence), res.evidence


# ------------------------------------------------------ memory read once a second ---

def table_1hz(*users, seconds=30.0, busy=()):
    """An others table as the sampler writes it: memory read on every fourth row (once a second at 4 Hz), the users
    (pid, name, ram, commit, vram, gpu_game) listed for their memory on those rows only; the rows between list only
    what was busy (`busy`: (pid, name, cpu, gpu_game) on every row) and say None for memory."""
    rows = []
    for k, t in enumerate(ticks(seconds)):
        memory = k % 4 == 0
        procs = [[pid, name, cpu, 0.0, gpu, CT + pid, gpu, 1, False, *((30, 40, 0) if memory else (None,) * 3)]
                 for pid, name, cpu, gpu in busy]
        if memory:
            procs += [[pid, name, 0.0, 0.0, gpu, CT + pid, gpu, 1, False, ram, commit, vram]
                      for pid, name, ram, commit, vram, gpu in users]
        rows.append([t, procs])
    return rows


def test_a_mean_counts_only_the_rows_that_read_memory():
    # Discord.exe is on every memory row (one in four) holding 900 MB: it held 900 MB, never a quarter of it
    res = ram_low(table_1hz((310, "Discord.exe", 900, 900, 0, 0.0), busy=[(320, "Busy.exe", 50.0, 0.0)]))
    assert "Discord.exe held at least 900 MB of RAM on average" in mem_fix(res).detail


@pytest.mark.parametrize("dip, named", [((10.75, 11.0), True), ((10.0, 10.25), True)])
def test_a_short_dip_of_free_ram_between_memory_readings_is_matched_with_the_nearest(dip, named):
    # free RAM was low for two ticks only; the memory rows are at 0.25 + k s, so the dip's nearest reading is at most
    # half a second away: the program it names is the one that held the memory then, never the old unnamed line
    lo, hi = dip
    res = ram_low(table_1hz((310, "Discord.exe", 900, 900, 0, 0.0)), avail=lambda t: 1000.0 if lo <= t <= hi else 4000.0)
    fix = mem_fix(res)
    assert fix.title.startswith("Pause or close Discord.exe") is named, fix.detail
    assert fix.detail.startswith("While free RAM was low")


def test_the_nearest_memory_reading_is_half_the_recorders_memory_interval():
    from lagdoctor.capture import sysmon
    assert rules.MEMORY_NEAR_S == sysmon.MEMORY_EVERY_S / 2


def test_gpu_contention_finds_a_gpu_user_listed_only_for_its_video_memory():
    # a miner at little CPU keeps the game's GPU 60 % busy and holds 3 GB of its video memory: the table lists it on
    # the memory rows only, while dwm.exe (Windows', at 3 %) is on every row. Over every row the miner's median is 0
    # and dwm.exe would be named; over the rows that read memory, the miner is the busiest
    from tests.test_analysis_rules import STARVED, contention
    rows = table_1hz((900, "Miner.exe", 50, 60, 3000, 60.0), busy=[(901, "dwm.exe", 3.0, 3.0)])
    res = run("gpu_contention", contention(80.0, others=rows), stats={"avg_fps": 58.0}, bound=STARVED)
    assert is_finding(res) and "Miner.exe" in res.title, (res.title, [ev.text for ev in res.evidence])
    assert any("Miner.exe" in ev.text for ev in res.evidence)


def test_the_disk_advice_under_its_floor_says_the_store_apps_program_is_its():
    from tests.test_analysis_named_programs import MB, STREAMS, disk_fix, near
    from tests.test_analysis_named_programs import table as io_table
    from tests.test_analysis_rules import GAME, lift, stutter
    evs = [stutter(i, t, "asset_streaming") for i, t in enumerate(STREAMS, 1)]
    cap = make(system=synth.system_table(30), others=io_table((800, "steam.exe", 10 * MB, near)),
               inventory={"game": dict(GAME, launcher="steam")})
    fix = disk_fix(run("asset_streaming", cap, events=evs,
                       causes={"asset_streaming": lift("supports", a=len(evs), n=len(evs))}))
    assert "the most was steam.exe (part of Steam, which started the game), at 10.0 MB/s" in fix.detail, fix.detail
