"""The advice to close other disk-heavy, CPU-heavy or network-heavy programs names the ones the capture saw.

The owner asked for it on the README's report (2026-11-15): "Close other disk-heavy programs while playing" said
nothing about which programs, while the capture holds every listed process's readings. rules._heavy reads the others
table over the finding's own stretches (the stutters' windows, or the whole part); bufferbloat reads net_flows'
talkers. A program is named only past the floor its cause already uses, Windows' own processes are said to be Windows
and never told to close, and a source that was not read is said so, never read as "no program".
"""
from __future__ import annotations

import pytest

from lagdoctor.analysis import attribute, crop, rules
from lagdoctor.model import LagEvent, SourceStatus
from tests import synth
from tests.test_analysis_rules import GAME, CPU_BOUND, is_finding, lift, make, run, stutter
from tests.test_analysis_rules_network import loaded

STREAMS = (5.0, 10.0, 15.0, 20.0, 25.0)        # the stutters' times: their windows are [t - 0.5, t + 0.4]
MB = 1e6


def ticks():
    return [0.25 * k for k in range(1, 121)]   # the others table at 4 Hz over 30 s


def near(t, times=STREAMS):
    """Inside a stutter's window: the 4 ticks t - 0.5, t - 0.25, t and t + 0.25 of each."""
    return any(te - attribute.WINDOW_BEFORE_S <= t <= te + 0.15 + attribute.WINDOW_AFTER_S for te in times)


def table(*users, steady=()):
    """An others table: each user (pid, name, bytes/s, when(t) -> listed?) listed with that I/O when `when` says so,
    every steady entry (pid, name, cpu %) on every tick, and a quiet process too, as on any real tick (a row listing
    nothing read nothing)."""
    rows = []
    for t in ticks():
        procs = [[pid, name, cpu, 0.0, 0.0, 134000000000000000 + pid] for pid, name, cpu in (*steady, (8, "Quiet.exe", 0.5))]
        for pid, name, bps, when in users:
            if when(t):
                procs.append([pid, name, 1.0, bps, 0.0, 134000000000000000 + pid])
        rows.append([t, procs])
    return rows


def streaming(others, *, events=None, target=None, hdd=False, **kw):
    evs = [stutter(i, t, "asset_streaming") for i, t in enumerate(STREAMS, 1)] if events is None else events
    cap = make(system=synth.system_table(30), others=others, target=target,
               inventory={"game": dict(GAME, media="HDD")} if hdd else None)
    return run("hdd_streaming" if hdd else "asset_streaming", cap, events=evs,
               causes={"asset_streaming": lift("supports", a=len(evs), n=len(evs))}, **kw)


def disk_fix(res):
    assert is_finding(res)
    fixes = [f for f in res.fixes if f.title == rules.DISK_ADVICE or f.title.startswith("Pause or close")]
    assert len(fixes) == 1, [f.title for f in res.fixes]
    return fixes[0]


# ------------------------------------------------------------------------------------ disk ---

def test_the_disk_advice_names_the_programs_that_read_and_wrote_most_at_the_stutters_biggest_first():
    # BackupSync.exe's two processes: 30 MB/s on every window tick and 20 MB/s on half of them; OneDrive.exe 44 MB/s on
    # half of them, with the lower PID, so an order by PID, or a mean over only the ticks it was listed on, puts it first
    half = (lambda t: near(t) and round(t / 0.25) % 2 == 0)
    fix = disk_fix(streaming(table((20004, "BackupSync.exe", 30 * MB, near), (20000, "BackupSync.exe", 20 * MB, half),
                                   (310, "OneDrive.exe", 44 * MB, half))))
    assert fix.title == "Pause or close BackupSync.exe and OneDrive.exe while you play"
    # unlisted ticks count as 0: (30 * 20 + 20 * 10) / 20 ticks = 40 MB/s, and 44 * 10 / 20 = 22, lower bounds
    assert fix.detail.startswith("At those stutters and FPS drops, besides the game, BackupSync.exe (2 processes) read "
                                 "and wrote at least 40.0 MB/s on average, and OneDrive.exe 22.0 MB/s. Windows counts")
    assert fix.detail.endswith("Check they are not something you need first; lag-doctor never closes programs itself.")


@pytest.mark.parametrize("bps, named", [(20.0 * MB, True), (19.9 * MB, False)])
def test_a_program_is_named_from_20_mb_a_second_and_under_it_the_most_is_still_said(bps, named):
    fix = disk_fix(streaming(table((310, "OneDrive.exe", bps, near))))
    if named:
        assert fix.title == "Pause or close OneDrive.exe while you play"
        assert "OneDrive.exe read and wrote at least 20.0 MB/s on average." in fix.detail
        assert "Check it is not something" in fix.detail
    else:
        assert fix.title == rules.DISK_ADVICE
        assert fix.detail.startswith("lag-doctor saw no other program read and write 20.0 MB/s or more on average at "
                                     "those stutters and FPS drops; the most was OneDrive.exe, at 19.9 MB/s.")
        assert "busiest on the CPU" in fix.detail      # the table's limit, next to "saw no"


@pytest.mark.parametrize("bps, said", [(0.05 * MB, "at 0.1 MB/s"), (0.0499 * MB, "under 0.1 MB/s")])
def test_a_most_that_rounds_to_nothing_is_said_to_be_under_a_tenth(bps, said):
    fix = disk_fix(streaming(table((1844, "Tiny.exe", bps, near))))
    assert f"; the most was Tiny.exe, {said}." in fix.detail


def test_a_program_heavy_only_away_from_the_stutters_is_not_named_at_them():
    fix = disk_fix(streaming(table((310, "OneDrive.exe", 90 * MB, lambda t: not near(t)))))
    assert fix.title == rules.DISK_ADVICE and "OneDrive" not in fix.detail
    assert fix.detail.startswith("lag-doctor saw no other program read and write 20.0 MB/s or more on average at "
                                 "those stutters and FPS drops.")


def test_hdd_streaming_names_them_over_the_whole_capture_when_no_stutter_was_from_loading():
    fix = disk_fix(streaming(table((310, "OneDrive.exe", 25 * MB, lambda t: True)), events=[], hdd=True))
    assert fix.title == "Pause or close OneDrive.exe while you play"
    assert fix.detail.startswith("During the capture, besides the game, OneDrive.exe read and wrote at least 25.0 MB/s")


def test_hdd_streaming_names_them_at_the_loading_stutters_when_there_are_some():
    fix = disk_fix(streaming(table((310, "OneDrive.exe", 25 * MB, near)), hdd=True))
    assert fix.detail.startswith("At those stutters and FPS drops, besides the game, OneDrive.exe read and wrote at "
                                 "least 25.0 MB/s")


@pytest.mark.parametrize("pid, name, target", [
    (4242, "game.exe", None),                                  # the game itself
    (5000, "shadercache.exe", {"pids": [4242, 5000]}),        # a child of the game: its helper
    (5555, "python.exe", {"tool_pids": [5555]}),               # lag-doctor's own process
    (6000, "PresentMon.exe", {"presentmon": {"exe": r"C:\lagdoctor\PresentMon.exe"}}),   # the one it ran
    (9999, "GAME.EXE", None),                                  # the game's image name under another PID
    (7000, "bad\u200bname.exe", None),                          # a name no fix or advice may show
])
def test_the_game_its_helpers_lag_doctor_and_bad_names_are_never_named(pid, name, target):
    fix = disk_fix(streaming(table((pid, name, 50 * MB, near)), target=target))
    assert fix.title == rules.DISK_ADVICE
    shown = "".join(ch for ch in name if ch.isprintable()).lower()      # as the report would print it
    assert shown not in fix.detail.lower() and "the most was" not in fix.detail
    # control: the same row under an ordinary name is named
    assert disk_fix(streaming(table((7100, "Other.exe", 50 * MB, near)), target=target)).title.startswith("Pause")


def test_windows_own_processes_are_said_to_be_windows_and_never_told_to_close():
    fix = disk_fix(streaming(table((4, "System", 50 * MB, near), (3300, "MsMpEng.exe", 30 * MB, near))))
    assert fix.title == rules.DISK_ADVICE
    assert fix.detail.startswith("At those stutters and FPS drops, besides the game, only Windows' own processes read "
                                 "and wrote 20.0 MB/s or more on average. Windows itself (System) read and wrote at "
                                 "least 50.0 MB/s: that is Windows' own work, so there is nothing to close. MsMpEng.exe "
                                 "read and wrote at least 30.0 MB/s: that name is normally Windows Security's virus "
                                 "scan, so lag-doctor does not suggest closing it. If it is, leave it on.")
    mixed = disk_fix(streaming(table((310, "OneDrive.exe", 25 * MB, near), (3300, "MsMpEng.exe", 30 * MB, near))))
    assert mixed.title == "Pause or close OneDrive.exe while you play"
    assert ("MsMpEng.exe read and wrote at least 30.0 MB/s too: that name is normally Windows Security's virus scan"
            in mixed.detail)


def test_with_no_others_table_which_programs_read_or_wrote_was_not_measured():
    fix = disk_fix(streaming([]))
    assert fix.title == rules.DISK_ADVICE
    assert fix.detail == "Which other programs read or wrote at those stutters and FPS drops was not measured."
    # a table whose I/O readings all failed is no "none" either
    unread = [[t, [[310, "OneDrive.exe", 5.0, None, 0.0]]] for t in ticks()]
    assert "was not measured" in disk_fix(streaming(unread)).detail


def test_an_older_captures_five_field_entries_are_named_too():
    rows = [[t, [[310, "OneDrive.exe", 1.0, 30 * MB if near(t) else 0.0, 0.0]]] for t in ticks()]
    assert disk_fix(streaming(rows)).title == "Pause or close OneDrive.exe while you play"


def test_marked_moments_are_named_as_such():
    marks = [LagEvent(i, "mark", t, t + 0.15, 0.0, {}, causes=["asset_streaming"]) for i, t in enumerate(STREAMS, 1)]
    cap = make(system=synth.system_table(30), others=table((310, "OneDrive.exe", 30 * MB, near)))
    res = run("asset_streaming", cap, events=marks,
              causes={"asset_streaming": lift("supports", a=5, n=5, half_weight=True)})
    assert disk_fix(res).detail.startswith("At those marked moments, besides the game, OneDrive.exe")


@pytest.mark.parametrize("t0, t1, named", [(12.0, 30.0, False), (2.0, 12.0, True), (None, None, True)])
def test_a_measured_part_names_only_what_it_holds(t0, t1, named):
    # OneDrive.exe read 90 MB/s up to 11 s only; hdd_streaming with no loading stutter reads the whole part
    cap = make(system=synth.system_table(30), others=table((310, "OneDrive.exe", 90 * MB, lambda t: t <= 11.0)),
               inventory={"game": dict(GAME, media="HDD")})
    part = crop.crop(cap, t0, t1) if t0 is not None else cap
    fix = disk_fix(run("hdd_streaming", part))
    assert fix.title.startswith("Pause or close OneDrive.exe") is named
    assert ("during the measured part" if t0 is not None else "during the capture") in fix.detail.lower()


def test_naming_a_program_never_moves_the_findings_score():
    quiet, heavy = streaming(table()), streaming(table((310, "OneDrive.exe", 90 * MB, near)))
    assert (quiet.confidence, quiet.score, quiet.evidence) == (heavy.confidence, heavy.score, heavy.evidence)
    assert disk_fix(quiet).title != disk_fix(heavy).title


# ------------------------------------------------------------------------------------- CPU ---

def cpu_limit(others):
    cap = make(system=synth.system_table(30, game_top_thread_pct=95.0), others=others)
    res = run("cpu_limit", cap, bound=CPU_BOUND)
    assert is_finding(res)
    return [f for f in res.fixes if f.title == rules.CPU_ADVICE or f.title.startswith("Pause or close")]


@pytest.mark.parametrize("cpu, named", [(25.0, True), (24.9, False)])
def test_cpu_limit_names_programs_from_a_quarter_of_a_cpu_on_average(cpu, named):
    fixes = cpu_limit(table(steady=[(20004, "chrome.exe", cpu / 2), (20008, "chrome.exe", cpu / 2),
                                    (2656, "dwm.exe", 3.0)]))
    assert len(fixes) == 1
    if named:
        assert fixes[0].title == "Pause or close chrome.exe while you play"
        assert fixes[0].detail.startswith("During the capture, besides the game, chrome.exe (2 processes) used at "
                                          f"least {cpu:.0f}% of a CPU on average. 100% is one logical CPU. Check it is")
    else:      # nothing past the floor: the line as it always was, word for word
        assert (fixes[0].title, fixes[0].detail) == (rules.CPU_ADVICE, "")


def test_cpu_limit_says_windows_itself_is_windows():
    fixes = cpu_limit(table(steady=[(4, "System", 40.0)]))
    assert fixes[0].title == rules.CPU_ADVICE
    assert fixes[0].detail.startswith("During the capture, besides the game, only Windows' own processes used 25% of a "
                                      "CPU or more on average. Windows itself (System) used at least 40% of a CPU:")


# --------------------------------------------------------------------------------- network ---

def talker(pid, name, mb):
    return {"pid": pid, "name": name, "rx_bytes": mb * MB, "tx_bytes": 0.0}


def bloat(talkers, target=None):
    cap = loaded(60.0)
    cap.net_flows = {"talkers": talkers, "talkers_scope": "all"}
    cap.sources["net_flows"] = SourceStatus("ok")        # the per-program tracker ran
    if target:
        cap.target.update(target)
    res = run("bufferbloat", cap)
    assert is_finding(res)
    return res.fixes[0], cap


def test_bufferbloat_names_the_programs_that_moved_1_mb_a_second_or_more():
    fix, _ = bloat([talker(900, "steam.exe", 1500.0), talker(901, "steamwebhelper.exe", 30.0),
                    talker(902, "Discord.exe", 29.0), talker(4242, "game.exe", 9000.0)])
    assert fix.title == "Pause or limit the downloads and uploads of steam.exe and steamwebhelper.exe while playing"
    assert fix.detail.startswith("During the capture, besides the game, steam.exe moved 1,500 MB (400 Mbit/s on "
                                 "average), and steamwebhelper.exe 30 MB (8 Mbit/s on average). Check they are")
    assert "Discord" not in fix.detail and "game.exe" not in fix.detail       # 29 MB in 30 s is under 1 MB/s


@pytest.mark.parametrize("pid, name, target", [
    (4242, "game.exe", None), (5000, "helper.exe", {"pids": [4242, 5000]}), (5555, "python.exe", {"tool_pids": [5555]}),
    (9999, "Game.exe", None)])
def test_bufferbloat_never_names_the_game_its_helpers_or_lag_doctor(pid, name, target):
    fix, _ = bloat([talker(pid, name, 900.0)], target)
    assert (fix.title, fix.detail) == (rules.NET_ADVICE, "")


def test_bufferbloat_keeps_its_old_line_without_talkers_and_in_a_measured_part():
    for talkers in (None, [], "corrupt"):
        fix, _ = bloat(talkers)
        assert (fix.title, fix.detail) == (rules.NET_ADVICE, "")
    _, cap = bloat([talker(900, "steam.exe", 1500.0)])
    part = crop.crop(cap, 5.0, 25.0)          # a part has no talkers: they are totals over the whole recording
    assert part.net_flows.get("talkers") is None
    res = run("bufferbloat", part)
    assert is_finding(res) and (res.fixes[0].title, res.fixes[0].detail) == (rules.NET_ADVICE, "")


def test_bufferbloat_says_svchost_is_windows():
    fix, _ = bloat([talker(1200, "svchost.exe", 900.0)])
    assert fix.title == rules.NET_ADVICE
    assert fix.detail == ("During the capture, besides the game, only Windows' own processes moved 1 MB a second or "
                          "more on average. svchost.exe moved 900 MB: that name is normally a host for Windows' own "
                          "services, such as Windows Update, so lag-doctor does not suggest closing it.")


# ------------------------------------------------------------------ what the review asked for ---

def test_a_names_processes_count_only_background_ones_that_used_it_there():
    # one BackupSync.exe reads at the stutters; three more of that name do not count: one only uses the CPU, one reads
    # only away from the stutters, and one is lag-doctor's own process
    fix = disk_fix(streaming(table((20004, "BackupSync.exe", 30 * MB, near), (20006, "BackupSync.exe", 30 * MB,
                                                                              lambda t: not near(t)),
                                   (5555, "BackupSync.exe", 2 * MB, near),
                                   steady=[(20005, "BackupSync.exe", 4.0)]), target={"tool_pids": [5555]}))
    assert "BackupSync.exe read and wrote at least 30.0 MB/s on average." in fix.detail
    assert "processes" not in fix.detail


def test_at_most_three_are_named_and_a_tie_goes_by_name():
    fix = disk_fix(streaming(table((501, "Bravo.exe", 30 * MB, near), (502, "Alpha.exe", 30 * MB, near),
                                   (503, "Charlie.exe", 25 * MB, near), (504, "Delta.exe", 21 * MB, near))))
    assert fix.title == "Pause or close Alpha.exe, Bravo.exe and Charlie.exe while you play"
    assert "Delta" not in fix.detail


def test_overlapping_windows_are_read_once_and_whole():
    # a long FPS drop with a stutter inside it: their windows nest, and a reading late in the drop still counts
    evs = [LagEvent(1, "fps_drop", 5.0, 12.0, 30.0, {"side": "cpu"}, causes=["asset_streaming"]),
           stutter(2, 8.0, "asset_streaming")]
    fix = disk_fix(streaming(table((310, "OneDrive.exe", 200 * MB, lambda t: 10.0 <= t <= 11.0)), events=evs))
    # 5 ticks at 200 MB/s over the 32 ticks of [4.5, 12.25]
    assert "OneDrive.exe read and wrote at least 31.2 MB/s on average" in fix.detail


def test_with_no_stutter_of_its_own_the_advice_is_the_old_line():
    cap = make(system=synth.system_table(30), others=table((310, "OneDrive.exe", 90 * MB, near)))
    res = run("asset_streaming", cap, events=[stutter(i, t, "game_cpu_spike") for i, t in enumerate(STREAMS, 1)],
              causes={"asset_streaming": lift("supports", a=5, n=5)})
    assert (disk_fix(res).title, disk_fix(res).detail) == (rules.DISK_ADVICE, "")     # never "was not measured"


@pytest.mark.parametrize("name, shown", [("MsMpEng.exe", "MsMpEng.exe (normally Windows Security's virus scan)"),
                                         ("System", "Windows itself (System)"),
                                         ("svchost.exe", "svchost.exe (normally a host for Windows' own services, "
                                                         "such as Windows Update)")])
def test_under_the_floor_a_windows_process_is_said_to_be_windows(name, shown):
    fix = disk_fix(streaming(table((1844, name, 8 * MB, near))))
    assert f"; the most was {shown}, at 8.0 MB/s." in fix.detail


def test_a_most_that_rounds_up_to_the_floor_is_just_under_it():
    fix = disk_fix(streaming(table((310, "OneDrive.exe", 19.96 * MB, near))))
    assert "; the most was OneDrive.exe, just under 20.0 MB/s." in fix.detail


@pytest.mark.parametrize("name, means", [
    ("Memory Compression", "that is Windows compressing memory because RAM runs short: close memory-heavy programs"),
    ("System", "that is work Windows' kernel does, often for a driver: update the graphics, Wi-Fi, storage and "
               "chipset drivers."),
])
def test_windows_itself_on_the_cpu_says_what_its_work_means(name, means):
    fixes = cpu_limit(table(steady=[(4, name, 40.0)]))
    assert f"Windows itself ({name}) used at least 40% of a CPU: {means}" in fixes[0].detail


def test_bufferbloat_names_a_program_only_when_one_of_its_processes_was_busy_as_bandwidth_hog_counts_them():
    # three Agent.exe processes at 0.6 MB/s each: 1.8 MB/s together, but bandwidth_hog clears each one, so
    # bufferbloat's advice must not name it; two chrome.exe at 1.5 MB/s each are named, with both counted
    quiet, cap = bloat([talker(900 + i, "Agent.exe", 18.0) for i in range(3)])
    assert (quiet.title, quiet.detail) == (rules.NET_ADVICE, "")
    assert isinstance(run("bandwidth_hog", cap), rules.Cleared)
    busy, cap = bloat([talker(910, "chrome.exe", 45.0), talker(911, "chrome.exe", 45.0)])
    assert busy.detail.startswith("During the capture, besides the game, chrome.exe (2 processes) moved 90 MB")
    assert is_finding(run("bandwidth_hog", cap))


def test_bufferbloat_never_names_presentmon_by_its_image_name():
    fix, _ = bloat([talker(6000, "PresentMon.exe", 900.0)],
                   {"presentmon": {"exe": r"C:\lagdoctor\PresentMon.exe"}})
    assert (fix.title, fix.detail) == (rules.NET_ADVICE, "")


def test_bufferbloat_survives_malformed_talkers_and_an_unknown_length():
    fix, _ = bloat([7, None, {"pid": 5, "name": None, "rx_bytes": 9e9}, {"pid": 6, "name": "ok.exe", "rx_bytes": "x"}])
    assert (fix.title, fix.detail) == (rules.NET_ADVICE, "")
    cap = loaded(60.0)
    cap.net_flows = {"talkers": [talker(900, "steam.exe", 1500.0)], "talkers_scope": "all"}
    cap.duration_s = None
    res = run("bufferbloat", cap)          # still a finding: a rule that raised would be dropped from the report
    assert is_finding(res) and (res.fixes[0].title, res.fixes[0].detail) == (rules.NET_ADVICE, "")


def test_bufferbloat_through_the_relay_says_only_this_windows_session_was_seen():
    cap = loaded(60.0)
    cap.net_flows = {"talkers": [talker(1200, "svchost.exe", 900.0)], "talkers_scope": "session"}
    res = run("bufferbloat", cap)
    assert is_finding(res)
    assert res.fixes[0].detail == ("During the capture, besides the game, only Windows' own processes in your Windows "
                                   "session moved 1 MB a second or more on average. svchost.exe moved 900 MB: that "
                                   "name is normally a host for Windows' own services, such as Windows Update, so "
                                   "lag-doctor does not suggest closing it. Programs outside your Windows session, "
                                   "Windows' own services among them, were not measured one by one.")


# ------------------------------------------------------------ where each process runs (since 2026-11-15) ---

LIKE = rules.LOOKALIKE


def test_a_look_alike_is_said_to_be_one():
    assert LIKE == " (named like a part of Windows, but not where Windows runs it)"


def table9(*users, steady=()):
    """An others table as the sampler writes it since 2026-11-15: each entry also says its Windows session and whether
    its program sits in System32 (fields 7 and 8). Users are (pid, name, bytes/s, when, session, folder)."""
    rows = []
    for t in ticks():
        procs = [[pid, name, cpu, 0.0, 0.0, 134000000000000000 + pid, None, session, folder]
                 for pid, name, cpu, session, folder in (*steady, (8, "Quiet.exe", 0.5, 1, False))]
        for pid, name, bps, when, session, folder in users:
            if when(t):
                procs.append([pid, name, 1.0, bps, 0.0, 134000000000000000 + pid, None, session, folder])
        rows.append([t, procs])
    return rows


def test_where_a_process_runs_tells_windows_own_from_a_program_named_like_it():
    # The table held names only, so a svchost.exe started from a user's Temp folder was said to be Windows' service
    # host, and robocopy.exe, from System32, could have been too: Windows runs its services in session 0, and runs
    # csrss.exe and dwm.exe from System32 in every session. A look-alike says so, and a System32 tool a player runs stays
    # a program to close.
    fix = disk_fix(streaming(table9((1200, "svchost.exe", 30 * MB, near, 0, True),
                                    (31000, "svchost.exe", 40 * MB, near, 1, False),
                                    (32000, "robocopy.exe", 25 * MB, near, 1, True))))
    assert fix.title == f"Pause or close svchost.exe{LIKE} and robocopy.exe while you play"
    assert fix.detail.startswith(f"At those stutters and FPS drops, besides the game, svchost.exe{LIKE} read and "
                                 "wrote at least 40.0 MB/s on average, and robocopy.exe 25.0 MB/s.")
    assert fix.detail.endswith(" svchost.exe read and wrote at least 30.0 MB/s too: that name is normally a host for "
                               "Windows' own services, such as Windows Update, so lag-doctor does not suggest closing "
                               "it.")


@pytest.mark.parametrize("pid, name, session, folder, own", [
    (1300, "MsMpEng.exe", 0, False, True),          # Defender: session 0, its program outside System32
    (1300, "MsMpEng.exe", 1, False, False),         # the same name in a player's session: a look-alike
    (1400, "dwm.exe", 1, True, True),               # one per session, from System32
    (1400, "dwm.exe", 1, False, False),
    (1500, "csrss.exe", 1, None, True),             # its path not read: by name, as an older capture
    (1600, "WmiPrvSE.exe", 0, True, False),         # from System32 in session 0, but on no list: a backup service's
    (1700, "BackupSvc.exe", 0, False, False),       # robocopy.exe would be too; someone else's service is closable
    (1800, "cmd.exe", 1, True, False),              # a System32 tool the player ran
    (1900, "MpCmdRun.exe", 1, False, True),         # Defender's scanner, which a player may start: by name
])
def test_windows_own_is_said_by_where_it_runs(pid, name, session, folder, own):
    fix = disk_fix(streaming(table9((pid, name, 30 * MB, near, session, folder))))
    assert fix.title.startswith("Pause or close") is not own, fix.title
    if own:
        assert fix.title == rules.DISK_ADVICE and "suggest closing it" in fix.detail, fix.detail
    else:
        like = LIKE if name.casefold() in rules.WINDOWS_PARTS else ""
        assert fix.title == f"Pause or close {name}{like} while you play"


def test_a_capture_that_lists_the_disk_users_too_says_no_more_that_one_can_be_missing():
    old = disk_fix(streaming(table((310, "OneDrive.exe", 8 * MB, near))))
    new = disk_fix(streaming(table9((310, "OneDrive.exe", 8 * MB, near, 1, False))))
    assert "busiest on the CPU" in old.detail
    assert new.detail == ("lag-doctor saw no other program read and write 20.0 MB/s or more on average at those "
                          "stutters and FPS drops; the most was OneDrive.exe, at 8.0 MB/s.")
    under = disk_fix(streaming(table9((31000, "svchost.exe", 8 * MB, near, 1, False))))
    assert f"; the most was svchost.exe{LIKE}, at 8.0 MB/s." in under.detail
    service = disk_fix(streaming(table9((1300, "MsMpEng.exe", 8 * MB, near, 0, False))))
    assert "; the most was MsMpEng.exe (normally Windows Security's virus scan), at 8.0 MB/s." in service.detail


def test_where_a_process_runs_is_read_only_from_well_formed_fields_and_a_reading_never_unsays_one():
    # field 7 a session number (never a bool, never past 32 bits), field 8 a bool; a tick that could not read the
    # path (None) never undoes another tick's reading of the same process
    def rows(session, folder, later=None):
        out = table9((31000, "svchost.exe", 30 * MB, near, session, folder))
        if later is not None:
            for t, procs in out[60:]:
                for q in procs:
                    if q[0] == 31000:
                        q[7], q[8] = later
        return out

    def named(others):
        return disk_fix(streaming(others)).title.startswith("Pause or close")

    assert named(rows(1, False))                                    # a look-alike
    assert named(rows(1, False, later=(1, None)))                   # a later None keeps "outside System32"
    assert named(rows(1, False, later=(None, None)))
    assert not named(rows(True, True))                              # a bool is no session; System32 decides
    assert named(rows(True, False))
    assert named(rows(False, False))                                # False == 0, but no session 0
    assert not named(rows(2 ** 32, None))                           # no session, no folder: by name
    assert not named(rows(1, "yes"))                                # a folder that is no bool: by name
    eight = [[t, [q[:8] for q in procs]] for t, procs in rows(1, False)]
    assert not named(eight)                                         # fewer than 9 fields: an older capture


def test_a_talker_the_others_table_never_listed_is_judged_by_its_name_and_a_bad_pid_breaks_nothing():
    cap = loaded(60.0)
    cap.others = [[0.25 * k, [[8, "Quiet.exe", 0.5, 0.0, 0.0, 134000000000000008, None, 1, False]]]
                  for k in range(1, 241)]
    cap.net_flows = {"talkers": [talker(5555, "svchost.exe", 900.0), talker(900, "steam.exe", 900.0),
                                 {"pid": [1], "name": "svchost.exe", "rx_bytes": 900 * MB, "tx_bytes": 0.0}],
                     "talkers_scope": "all"}
    cap.sources["net_flows"] = SourceStatus("ok")
    res = run("bufferbloat", cap)
    assert is_finding(res), res                                     # a pid that is no number crashed the rule
    assert res.fixes[0].title == "Pause or limit the downloads and uploads of steam.exe while playing"
    assert " svchost.exe (2 processes) moved 1,800 MB too: that name is normally a host" in res.fixes[0].detail


def test_a_measured_part_of_a_capture_that_says_where_each_runs_still_tells_them_apart():
    cap = make(system=synth.system_table(30), others=table9((31000, "svchost.exe", 40 * MB, near, 1, False),
                                                            (1200, "svchost.exe", 30 * MB, near, 0, True)))
    part = crop.crop(cap, 2.0, 22.0)
    evs = [stutter(i, t, "asset_streaming") for i, t in enumerate(STREAMS[:4], 1)]
    res = run("asset_streaming", part, events=evs, causes={"asset_streaming": lift("supports", a=4, n=4)})
    fix = disk_fix(res)
    assert fix.title == f"Pause or close svchost.exe{LIKE} while you play"
    assert "svchost.exe read and wrote at least 30.0 MB/s too: that name is normally" in fix.detail


def test_bufferbloat_reads_where_a_talker_runs_from_the_others_table():
    cap = loaded(60.0)
    cap.others = [[0.25 * k, [[1200, "svchost.exe", 1.0, 0.0, 0.0, 134000000000001200, None, 0, True],
                              [31000, "svchost.exe", 1.0, 0.0, 0.0, 134000000000031000, None, 1, False]]]
                  for k in range(1, 241)]
    cap.net_flows = {"talkers": [talker(1200, "svchost.exe", 900.0), talker(31000, "svchost.exe", 600.0)],
                     "talkers_scope": "all"}
    cap.sources["net_flows"] = SourceStatus("ok")
    res = run("bufferbloat", cap)
    assert is_finding(res)
    fix = res.fixes[0]
    assert fix.title == f"Pause or limit the downloads and uploads of svchost.exe{LIKE} while playing"
    assert fix.detail.startswith(f"During the capture, besides the game, svchost.exe{LIKE} moved 600 MB")
    assert " svchost.exe moved 900 MB too: that name is normally a host for Windows' own services" in fix.detail
