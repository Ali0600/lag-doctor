"""Whole settings sweeps, driven through a stand-in world (tests/sweep_world.py): no game, recording or console."""
from __future__ import annotations

import random
import signal
from pathlib import Path

import pytest

from lagdoctor.sweep import inifile, plan, results, runner
from tests.sweep_world import PRESS_T, Play, World

RAW = (b"[ScalabilityGroups]\r\nsg.ShadowQuality=2\r\nsg.StaticMeshQuality=3\r\n\r\n"
       b"[/Script/Engine.GameUserSettings]\r\nbUseVSync=False\r\n")
GO = "<start the game>"                     # the player starts the game, then presses Enter
FPS = {"A": 60.0, "B": 66.0, "C": 60.3}


def new_sweep(tmp_path: Path, tries=("sg.StaticMeshQuality=1",), runs=2, warm_up=True, seed=5):
    ini = tmp_path / "GameUserSettings.ini"
    ini.write_bytes(RAW)
    text = inifile.decode(RAW)[0]
    resolved, original = plan.resolve(text, [plan.parse_try(t) for t in tries])
    variants = plan.make_variants(resolved, original)
    sweep = plan.Sweep(game="Game.exe", settings_file=str(ini), window_s=60.0, runs_per_variant=runs,
                       warm_up=warm_up, start_key="ctrl+alt+s", seed=seed, variants=variants, original=original,
                       slots=plan.make_slots([v.id for v in variants], runs, warm_up, random.Random(seed)),
                       others=plan.other_values(text, original))
    folder = tmp_path / "sweep"
    folder.mkdir()
    return sweep, folder, ini


def steady_plays(sweep: plan.Sweep) -> list[Play]:
    """A good run for every slot, with a little spread from run to run."""
    jitter = [0.4, -0.3, 0.1, -0.2, 0.3, -0.1, 0.2, -0.4, 0.0, 0.15, -0.25, 0.35]
    return [Play(fps=FPS[s.variant] + jitter[i % len(jitter)]) for i, s in enumerate(sweep.slots)]


def entries_left(world: World) -> list[str]:
    return [e.id for e in world.journal.entries() if e.state in ("pending", "applied", "conflict")]


def first(sweep: plan.Sweep, vid: str) -> int:
    return next(i for i, s in enumerate(sweep.slots) if s.variant == vid)


def test_a_whole_sweep_tries_each_setting_in_order_ranks_them_and_puts_the_file_back(tmp_path):
    sweep, folder, ini = new_sweep(tmp_path, seed=1)
    assert sweep.slots[-1].variant == "B"                 # so the file holds B's value when the last run ends
    world = World(tmp_path, sweep, FPS, answers=[GO] * len(sweep.slots), plays=steady_plays(sweep))
    assert runner.run(sweep, folder, world.seams()) == 0
    assert sweep.state == "finished" and plan.load(folder).state == "finished"
    # Each run's game found its own setting in the file, and only that one.
    want = {"A": "3", "B": "1"}
    assert [seen["sg.StaticMeshQuality"] for seen in world.seen] == [want[s.variant] for s in sweep.slots]
    assert ini.read_bytes() == RAW and entries_left(world) == []
    assert [r.state for r in sweep.runs] == ["good"] * len(sweep.slots)
    assert all(r.start_t == pytest.approx(PRESS_T) for r in sweep.runs)
    rows = {r["variant"]: r for r in results.ranking(sweep)}
    assert rows["B"]["vs"]["avg"]["verdict"] == "more" and rows["B"]["avg"]["n"] == sweep.runs_per_variant
    said = "\n".join(world.said)
    assert "Results, in order of average FPS" in said and "faster" in said and "back as it was" in said
    assert world.sounds.count("start") == world.sounds.count("done") == len(sweep.slots)
    assert all(lst.stopped for lst in world.listeners)          # the start key is released after every run


def test_each_recording_is_a_watch_of_the_named_game_without_network_or_lag_key(tmp_path):
    sweep, folder, _ = new_sweep(tmp_path, runs=2, warm_up=False)
    world = World(tmp_path, sweep, FPS, answers=[GO] * 4, plays=steady_plays(sweep))
    runner.run(sweep, folder, world.seams())
    argv = world.argv[0]
    assert argv[:2] == ["--game", "Game.exe"] and "--no-network" in argv and "--elevate" in argv
    assert argv[argv.index("--hotkey") + 1] == "none"
    assert Path(argv[argv.index("--out") + 1]) == folder / plan.RUNS_DIR
    assert float(argv[argv.index("--duration") + 1]) > runner.START_WAIT_S + sweep.window_s + runner.TAIL_S


def test_ctrl_c_during_a_run_stops_the_sweep_and_puts_the_file_back_and_resume_goes_on(tmp_path):
    sweep, folder, ini = new_sweep(tmp_path, seed=1)
    assert sweep.slots[2].variant == "B"                  # so B's value is in the file when Ctrl+C comes
    plays = steady_plays(sweep)
    plays[2] = Play(ctrl_c=True)
    world = World(tmp_path, sweep, FPS, answers=[GO] * 3, plays=plays[:3])
    assert runner.run(sweep, folder, world.seams()) == 130
    assert sweep.state == "stopped" and ini.read_bytes() == RAW and entries_left(world) == []
    assert sweep.runs[-1].state == "rejected" and "ended 10 s into the 60 s window" in sweep.runs[-1].why
    assert plan.next_slot(sweep).n == 3
    assert "lagdoctor sweep --resume" in "\n".join(world.said)
    resumed = plan.load(folder)
    world2 = World(tmp_path, resumed, FPS, answers=[GO] * 4, plays=steady_plays(resumed)[2:])
    assert runner.run(resumed, folder, world2.seams()) == 0
    assert resumed.state == "finished" and world2.runs == len(resumed.slots) - 2 and ini.read_bytes() == RAW


def test_ctrl_c_after_the_window_keeps_the_run_and_stops_the_sweep(tmp_path):
    sweep, folder, ini = new_sweep(tmp_path, runs=2, warm_up=False)
    plays = steady_plays(sweep)
    plays[0] = Play(code_after=130)                       # Ctrl+C while watch analysed the saved run
    world = World(tmp_path, sweep, FPS, answers=[GO], plays=plays[:1])
    assert runner.run(sweep, folder, world.seams()) == 130
    assert [r.state for r in sweep.runs] == ["good"] and plan.next_slot(sweep).n == 2
    assert ini.read_bytes() == RAW


def test_a_run_whose_report_failed_after_the_save_still_counts(tmp_path):
    sweep, folder, _ = new_sweep(tmp_path, runs=2, warm_up=False)
    plays = steady_plays(sweep)
    plays[0] = Play(raises_after=RuntimeError("the report broke"))
    world = World(tmp_path, sweep, FPS, answers=[GO] * 4, plays=plays)
    assert runner.run(sweep, folder, world.seams()) == 0
    assert sweep.runs[0].state == "good"
    assert "The run's report could not be made: RuntimeError: the report broke" in "\n".join(world.said)


@pytest.mark.parametrize("answer,code,ends", [("", 1, RAW), ("n", 2, RAW.replace(b"sg.StaticMeshQuality=3",
                                                                                   b"sg.StaticMeshQuality=2"))])
def test_a_game_that_writes_its_own_value_back_ends_the_sweep_and_the_player_says_what_goes_back(tmp_path, answer,
                                                                                                  code, ends):
    sweep, folder, ini = new_sweep(tmp_path, seed=1)
    b = first(sweep, "B")
    plays = steady_plays(sweep)
    plays[b] = Play(writes_back="2")
    world = World(tmp_path, sweep, FPS, answers=[GO] * (b + 1) + [answer], plays=plays[:b + 1])
    assert runner.run(sweep, folder, world.seams()) == code
    said = "\n".join(world.said)
    assert "game set sg.StaticMeshQuality to 2 when it closed" in said and "cannot go on" in said
    assert "Put 3 back? [Y/n]" in said
    assert sweep.runs[-1].state == "rejected" and sweep.state == "ended" and plan.load(folder).state == "ended"
    assert ini.read_bytes() == ends
    if answer == "n":
        assert "--force" in said and "lagdoctor undo game_setting" in said


def test_a_game_that_puts_the_original_back_also_ends_it_and_the_file_ends_as_it_was(tmp_path):
    sweep, folder, ini = new_sweep(tmp_path, seed=1)
    b = first(sweep, "B")
    plays = steady_plays(sweep)
    plays[b] = Play(writes_back="3")
    world = World(tmp_path, sweep, FPS, answers=[GO] * (b + 1), plays=plays[:b + 1])
    assert runner.run(sweep, folder, world.seams()) == 1
    assert ini.read_bytes() == RAW and entries_left(world) == [] and sweep.state == "ended"


def test_a_value_the_game_writes_in_its_own_spelling_is_still_the_setting(tmp_path):
    sweep, folder, ini = new_sweep(tmp_path, runs=2, warm_up=False, seed=1)
    plays = [Play(fps=p.fps, writes_back="1.000000" if s.variant == "B" else None)
             for p, s in zip(steady_plays(sweep), sweep.slots)]
    world = World(tmp_path, sweep, FPS, answers=[GO] * len(sweep.slots), plays=plays)
    assert runner.run(sweep, folder, world.seams()) == 0
    assert sweep.state == "finished" and all(r.state == "good" for r in sweep.runs)
    assert ini.read_bytes() == RAW and entries_left(world) == []


def test_a_line_the_game_drops_is_put_back_when_the_player_says_so(tmp_path):
    sweep, folder, ini = new_sweep(tmp_path, seed=1)
    b = first(sweep, "B")
    plays = steady_plays(sweep)
    plays[b] = Play(drops_line=True)
    world = World(tmp_path, sweep, FPS, answers=[GO] * (b + 1) + [""], plays=plays[:b + 1])
    assert runner.run(sweep, folder, world.seams()) == 1
    said = "\n".join(world.said)
    assert "removed sg.StaticMeshQuality from the file" in said and "no value (the line is gone)" in said
    assert ini.read_bytes() == RAW and entries_left(world) == []


def test_a_run_behind_another_window_is_offered_again_and_tried_again(tmp_path):
    sweep, folder, ini = new_sweep(tmp_path, runs=2, warm_up=False)
    plays = steady_plays(sweep)
    plays.insert(1, Play(capture={"focused": lambda t: 0 if 30 <= t < 45 else 1}))
    answers = [GO, GO, "", GO, GO, GO]                  # Enter at "Try this run again?"
    world = World(tmp_path, sweep, FPS, answers=answers, plays=plays)
    assert runner.run(sweep, folder, world.seams()) == 0
    assert [r.state for r in sweep.runs].count("rejected") == 1 and world.runs == len(sweep.slots) + 1
    rejected = next(r for r in sweep.runs if r.state == "rejected")
    assert "behind another window" in rejected.why and plan.settled(sweep, rejected.slot)
    assert ini.read_bytes() == RAW


def test_a_run_skipped_after_it_failed_settles_its_place(tmp_path):
    sweep, folder, _ = new_sweep(tmp_path, runs=2, warm_up=False)
    plays = steady_plays(sweep)
    plays[0] = Play(press=False)
    world = World(tmp_path, sweep, FPS, answers=[GO, "n", GO, GO, GO], plays=plays)
    assert runner.run(sweep, folder, world.seams()) == 0
    assert [r.state for r in sweep.runs][:2] == ["failed", "skipped"]
    assert "not pressed within 5 minutes" in sweep.runs[0].why and "problem" in world.sounds
    assert sweep.state == "finished"


@pytest.mark.parametrize("play,why", [
    (Play(frames=False), "frame timing was off (was the UAC prompt declined?)"),
    (Play(code=2), "the recording did not start (watch said why, above)"),
    (Play(code=3), "exited during the UAC prompt"),
])
def test_a_run_that_could_not_measure_fails_with_the_reason_and_can_be_tried_again(tmp_path, play, why):
    sweep, folder, _ = new_sweep(tmp_path, runs=2, warm_up=False)
    world = World(tmp_path, sweep, FPS, answers=[GO, "q"], plays=[play])
    assert runner.run(sweep, folder, world.seams()) == 0          # q: stopped as asked
    assert sweep.runs[0].state == "failed" and why in sweep.runs[0].why


@pytest.mark.parametrize("play,why", [(Play(code=6), "frame timing is not available"),
                                      (Play(code=4), "another recording is running"),
                                      (Play(raises=RuntimeError("broken")), "The recording failed (RuntimeError")])
def test_a_recording_that_cannot_start_at_all_stops_the_sweep_and_the_file_goes_back(tmp_path, play, why):
    sweep, folder, ini = new_sweep(tmp_path, seed=1)
    assert sweep.slots[0].variant == "B"                  # so B's value is in the file when the sweep stops
    world = World(tmp_path, sweep, FPS, answers=[GO], plays=[play])
    assert runner.run(sweep, folder, world.seams()) == 1
    assert why in "\n".join(world.said) and ini.read_bytes() == RAW and entries_left(world) == []


def test_an_error_nobody_expected_still_puts_the_file_back_before_it_is_raised(tmp_path, monkeypatch):
    sweep, folder, ini = new_sweep(tmp_path, seed=1)
    world = World(tmp_path, sweep, FPS, answers=[GO] * 3, plays=steady_plays(sweep))

    def broken(*a, **kw):
        if world.variant_on_disk() == "B":
            raise ZeroDivisionError("a bug")
        return real(*a, **kw)
    real = results.window_metrics
    monkeypatch.setattr(results, "window_metrics", broken)
    with pytest.raises(ZeroDivisionError):
        runner.run(sweep, folder, world.seams())
    assert ini.read_bytes() == RAW and entries_left(world) == [] and plan.load(folder).state == "stopped"


def test_a_start_key_another_program_holds_stops_the_sweep_before_the_player_starts_the_game(tmp_path):
    sweep, folder, ini = new_sweep(tmp_path, seed=1)
    world = World(tmp_path, sweep, FPS, answers=[GO])
    world.listener_ok = False
    assert runner.run(sweep, folder, world.seams()) == 1
    said = "\n".join(world.said)
    assert world.runs == 0 and "Ctrl+Alt+S could not be registered" in said and "--resume --start-key" in said
    assert not any("Start the game" in line for line in world.said)      # found before the player spent the time
    assert ini.read_bytes() == RAW


def test_presses_before_the_recording_began_and_after_it_stopped_are_ignored(tmp_path):
    sweep, folder, _ = new_sweep(tmp_path, runs=2, warm_up=False)
    plays = steady_plays(sweep)
    plays[0] = Play(press_early=True, press_late=True)
    world = World(tmp_path, sweep, FPS, answers=[GO] * 4, plays=plays)
    assert runner.run(sweep, folder, world.seams()) == 0
    assert sweep.runs[0].start_t == pytest.approx(PRESS_T)            # the early press did not start the window
    assert world.sounds.count("start") == len(sweep.slots)            # nor did the late one (World asserts no timer)


def test_the_file_is_never_changed_while_the_game_runs(tmp_path):
    sweep, folder, ini = new_sweep(tmp_path, runs=2, warm_up=False)
    plays = steady_plays(sweep)
    plays[0].stays_running = True
    world = World(tmp_path, sweep, FPS, answers=[GO] * 4, plays=plays)
    world.quits_after(5)                                  # one look as Enter is pressed, then four after the run
    writes = []
    real_write = world.fix.write

    def write(params, value):
        assert not world.running, "the settings file was written while the game ran"
        writes.append(value)
        real_write(params, value)
    world.fix.write = write
    assert runner.run(sweep, folder, world.seams()) == 0
    assert world.sleeps >= 3 and writes and "Quit the game now" in "\n".join(world.said)


def test_a_gpu_still_heating_up_keeps_the_run_out_but_a_moving_power_limit_does_not(tmp_path):
    sweep, folder, _ = new_sweep(tmp_path, runs=2, warm_up=False)
    plays = steady_plays(sweep)
    plays.insert(1, Play(capture={"clock_mhz": lambda t: 1700.0 if t < 50 else 780.0}))
    plays[2] = Play(fps=plays[2].fps, capture={"power_limit_w": 55.0})
    world = World(tmp_path, sweep, FPS, answers=[GO, GO, "", GO, GO, GO], plays=plays)
    assert runner.run(sweep, folder, world.seams()) == 0
    rejected = [r for r in sweep.runs if r.state == "rejected"]
    assert len(rejected) == 1 and "still heating up" in rejected[0].why
    assert any(r.conditions.get("gpu_power_limit_w") == 55.0 and r.state == "good" for r in sweep.runs)


def test_an_updated_game_ends_the_sweep(tmp_path):
    sweep, folder, ini = new_sweep(tmp_path, runs=2, warm_up=False)
    plays = steady_plays(sweep)
    plays[1] = Play(capture={"exe_bytes": 82_000_000})
    world = World(tmp_path, sweep, FPS, answers=[GO, GO], plays=plays)
    assert runner.run(sweep, folder, world.seams()) == 1
    said = "\n".join(world.said)
    assert "updated" in said and sweep.state == "ended" and ini.read_bytes() == RAW
    assert "Go on later with" not in said and "only shows what it found" in said


def test_a_file_changed_between_sessions_ends_a_resumed_sweep_before_it_writes(tmp_path):
    sweep, folder, ini = new_sweep(tmp_path, runs=2, warm_up=False)
    world = World(tmp_path, sweep, FPS, answers=[GO, "q"], plays=steady_plays(sweep))
    assert runner.run(sweep, folder, world.seams()) == 0 and ini.read_bytes() == RAW
    ini.write_bytes(RAW.replace(b"sg.StaticMeshQuality=3", b"sg.StaticMeshQuality=2"))    # the player's menu
    resumed = plan.load(folder)
    world2 = World(tmp_path, resumed, FPS, answers=[GO] * 4, plays=steady_plays(resumed))
    assert runner.run(resumed, folder, world2.seams()) == 1
    assert "is now 2 in the settings file, not the 3" in "\n".join(world2.said) and resumed.state == "ended"
    assert world2.runs == 0 and ini.read_bytes() == RAW.replace(b"sg.StaticMeshQuality=3", b"sg.StaticMeshQuality=2")


@pytest.mark.parametrize("answer", ["y", "n"])
def test_another_value_the_game_changed_is_said_and_the_player_decides_whether_runs_count(tmp_path, answer):
    sweep, folder, ini = new_sweep(tmp_path, runs=2, warm_up=False, seed=1)
    plays = steady_plays(sweep)
    plays[0] = Play(fps=plays[0].fps, other_change=("sg.ShadowQuality", "1"))
    answers = [GO, answer] + ([GO] * 3 if answer == "y" else [])
    world = World(tmp_path, sweep, FPS, answers=answers, plays=plays)
    code = runner.run(sweep, folder, world.seams())
    said = "\n".join(world.said)
    assert "Other values in the settings file are not as the runs so far had them: sg.ShadowQuality 2 -> 1" in said
    if answer == "y":
        assert code == 0 and sweep.state == "finished" and len(sweep.runs) == len(sweep.slots)
        assert any("sg.ShadowQuality 2 -> 1" in n for n in sweep.notes)
        assert "Note: from run 2 on" in said
    else:
        assert code == 0 and sweep.state == "stopped" and len(sweep.runs) == 1
        assert "Put those values back" in said
    assert inifile.get_value(ini.read_bytes().decode(), "ScalabilityGroups", "sg.StaticMeshQuality") == "3"


def test_an_edit_the_state_never_heard_of_is_still_put_back(tmp_path):
    # A crash between the write and the state's save: the journal knows the edit, sweep.json does not.
    sweep, folder, ini = new_sweep(tmp_path, runs=2, warm_up=False, seed=1)
    world = World(tmp_path, sweep, FPS)
    p = world.fix.plan({"file": str(ini), "section": "ScalabilityGroups", "key": "sg.StaticMeshQuality", "value": "1",
                        "game": "Game.exe"})
    assert world.fix.apply(p, world.journal).ok and b"sg.StaticMeshQuality=1" in ini.read_bytes()
    assert runner.put_back(sweep, folder, world.seams())
    assert ini.read_bytes() == RAW and entries_left(world) == []


def test_an_edit_a_crash_left_pending_is_settled_then_put_back(tmp_path):
    sweep, folder, ini = new_sweep(tmp_path, runs=2, warm_up=False, seed=1)
    world = World(tmp_path, sweep, FPS)
    params = {"file": str(ini), "section": "ScalabilityGroups", "key": "sg.StaticMeshQuality", "value": "1",
              "game": "Game.exe"}
    world.journal.begin("game_setting", world.fix.target(params), params, "3", "1")
    world.fix.write(params, "1")                           # the write landed; the crash came before "applied"
    assert runner.put_back(sweep, folder, world.seams())
    assert ini.read_bytes() == RAW and entries_left(world) == []


def test_every_edit_is_tried_even_when_one_stays(tmp_path):
    sweep, folder, ini = new_sweep(tmp_path, tries=("sg.ShadowQuality=0, sg.StaticMeshQuality=1",), runs=2,
                                   warm_up=False, seed=1)
    assert sweep.slots[0].variant == "B"
    plays = steady_plays(sweep)
    plays[0] = Play(fps=plays[0].fps, other_change=("sg.StaticMeshQuality", "2"))   # the newer edit's key, changed
    world = World(tmp_path, sweep, FPS, answers=[GO, "n"], plays=plays[:1])
    assert runner.run(sweep, folder, world.seams()) == 2
    held = ini.read_bytes().decode()
    assert inifile.get_value(held, "ScalabilityGroups", "sg.ShadowQuality") == "2"         # the older one went back
    assert inifile.get_value(held, "ScalabilityGroups", "sg.StaticMeshQuality") == "2"     # left, at the player's no


def test_a_second_ctrl_c_while_waiting_for_the_game_leaves_the_setting_for_later(tmp_path):
    sweep, folder, ini = new_sweep(tmp_path, seed=1)
    b = first(sweep, "B")
    plays = steady_plays(sweep)
    plays[b] = Play(ctrl_c=True, stays_running=True)
    world = World(tmp_path, sweep, FPS, answers=[GO] * (b + 1), plays=plays)

    def second_ctrl_c(seconds):
        if world.running:
            raise KeyboardInterrupt
    world.sleep = second_ctrl_c
    assert runner.run(sweep, folder, world.seams()) == 2
    said = "\n".join(world.said)
    assert "Close the game" in said and "lagdoctor sweep --resume" in said and "lagdoctor undo game_setting" in said
    assert "--all" not in said
    assert b"sg.StaticMeshQuality=1" in ini.read_bytes() and len(entries_left(world)) == 1
    # Later, with the game closed, a resume first puts the setting back, then goes on.
    world.running = False
    resumed = plan.load(folder)
    world2 = World(tmp_path, resumed, FPS, answers=[GO] * len(resumed.slots), plays=steady_plays(resumed))
    assert runner.run(resumed, folder, world2.seams()) == 0
    assert ini.read_bytes() == RAW and entries_left(world2) == []


def test_a_press_counted_while_the_sweep_waits_to_put_things_back_leaves_them_for_later(tmp_path):
    # A real second Ctrl+C reaches the guard, which counts it rather than raising it.
    sweep, folder, ini = new_sweep(tmp_path, seed=1)
    b = first(sweep, "B")
    plays = steady_plays(sweep)
    plays[b] = Play(ctrl_c=True, stays_running=True)
    world = World(tmp_path, sweep, FPS, answers=[GO] * (b + 1), plays=plays)
    guard = runner.Interrupts()
    world.on_sleep = lambda: guard._on(signal.SIGINT, None) if world.running else None
    assert runner.run(sweep, folder, world.seams(), interrupts=guard) == 2
    assert guard.presses == 1 and b"sg.StaticMeshQuality=1" in ini.read_bytes()


def test_the_interrupt_guard_raises_the_first_press_and_counts_the_rest():
    guard = runner.Interrupts()
    with pytest.raises(KeyboardInterrupt):
        guard._on(signal.SIGINT, None)
    try:
        guard._on(signal.SIGINT, None)
        guard._on(getattr(signal, "SIGBREAK", signal.SIGINT), None)
    except KeyboardInterrupt:
        pytest.fail("a press after the first was raised, not counted")
    assert guard.presses == 3 and guard.stopping
    before = signal.getsignal(signal.SIGINT)
    with runner.Interrupts() as live:
        assert signal.getsignal(signal.SIGINT) == live._on
    assert signal.getsignal(signal.SIGINT) == before


def test_a_press_after_a_recording_that_ended_by_itself_is_ignored(tmp_path):
    # The game exited before the key: the session stops without the stop event, so only "stopping" closes the key.
    sweep, folder, _ = new_sweep(tmp_path, runs=2, warm_up=False)
    world = World(tmp_path, sweep, FPS, answers=[GO, "q"], plays=[Play(ends_by_itself=True)])
    assert runner.run(sweep, folder, world.seams()) == 0
    assert sweep.runs[0].state == "failed" and "before the start key was pressed" in sweep.runs[0].why
    assert "start" not in world.sounds


def test_a_double_tapped_ctrl_c_still_puts_the_settings_back(tmp_path):
    sweep, folder, ini = new_sweep(tmp_path, seed=1)
    world = World(tmp_path, sweep, FPS, answers=[GO], plays=[Play(ctrl_c=True)])
    guard = runner.Interrupts()
    world.on_say = lambda text: guard._on(signal.SIGINT, None) if text == "Stopped." else None   # the second press
    try:
        code = runner.run(sweep, folder, world.seams(), interrupts=guard)
    except KeyboardInterrupt:
        pytest.fail("the second press escaped the sweep's cleanup, so nothing was put back")
    assert code == 130 and guard.presses == 1 and ini.read_bytes() == RAW and entries_left(world) == []


def test_input_that_ends_is_said_as_such(tmp_path):
    sweep, folder, ini = new_sweep(tmp_path, seed=1)
    world = World(tmp_path, sweep, FPS, answers=[])
    assert runner.run(sweep, folder, world.seams()) == 1
    assert "The terminal gave no more input" in "\n".join(world.said) and ini.read_bytes() == RAW


def test_a_chime_that_cannot_play_is_said_once_after_the_run(tmp_path):
    sweep, folder, _ = new_sweep(tmp_path, runs=2, warm_up=False)
    world = World(tmp_path, sweep, FPS, answers=[GO] * 4, plays=steady_plays(sweep))
    world.sound_ok = False
    assert runner.run(sweep, folder, world.seams()) == 0
    notes = [line for line in world.said if "could not be played" in line]
    assert len(notes) == len(sweep.slots) and "the window ends 60 s after your press" in notes[0]
