"""Whole settings sweeps, driven through a stand-in world (tests/sweep_world.py): no game, recording or console."""
from __future__ import annotations

import random
from pathlib import Path

import pytest

from lagdoctor.sweep import inifile, plan, results, runner
from tests.sweep_world import PRESS_T, Play, World

RAW = (b"[ScalabilityGroups]\r\nsg.ShadowQuality=0\r\nsg.StaticMeshQuality=3\r\n\r\n"
       b"[/Script/Engine.GameUserSettings]\r\nbUseVSync=False\r\n")
GO = "<start the game>"                     # the player starts the game, then presses Enter
FPS = {"A": 60.0, "B": 66.0, "C": 60.3}


def new_sweep(tmp_path: Path, tries=("sg.StaticMeshQuality=1",), runs=2, warm_up=True, seed=5):
    ini = tmp_path / "GameUserSettings.ini"
    ini.write_bytes(RAW)
    resolved, original = plan.resolve(inifile.decode(RAW)[0], [plan.parse_try(t) for t in tries])
    variants = plan.make_variants(resolved, original)
    sweep = plan.Sweep(game="Game.exe", settings_file=str(ini), window_s=60.0, runs_per_variant=runs,
                       warm_up=warm_up, start_key="ctrl+alt+s", seed=seed, variants=variants, original=original,
                       slots=plan.make_slots([v.id for v in variants], runs, warm_up, random.Random(seed)))
    folder = tmp_path / "sweep"
    folder.mkdir()
    return sweep, folder, ini


def steady_plays(sweep: plan.Sweep) -> list[Play]:
    """A good run for every slot, with a little spread from run to run."""
    jitter = [0.4, -0.3, 0.1, -0.2, 0.3, -0.1, 0.2, -0.4, 0.0, 0.15, -0.25, 0.35]
    return [Play(fps=FPS[s.variant] + jitter[i % len(jitter)]) for i, s in enumerate(sweep.slots)]


def entries_left(world: World) -> list[str]:
    return [e.id for e in world.journal.entries() if e.state in ("applied", "conflict")]


def test_a_whole_sweep_tries_each_setting_in_order_ranks_them_and_puts_the_file_back(tmp_path):
    sweep, folder, ini = new_sweep(tmp_path, seed=1)
    assert sweep.slots[-1].variant == "B"                 # so the file holds B's value when the last run ends
    world = World(tmp_path, sweep, FPS, answers=[GO] * len(sweep.slots), plays=steady_plays(sweep))
    assert runner.run(sweep, folder, world.seams()) == 0
    assert sweep.state == "finished" and plan.load(folder).state == "finished"
    # Each run's game found its own setting in the file, and only that one.
    want = {"A": "3", "B": "1"}
    assert [seen["sg.StaticMeshQuality"] for seen in world.seen] == [want[s.variant] for s in sweep.slots]
    assert ini.read_bytes() == RAW and entries_left(world) == [] and sweep.entries == []
    assert [r.state for r in sweep.runs] == ["good"] * len(sweep.slots)
    assert all(r.start_t == pytest.approx(PRESS_T) for r in sweep.runs)
    rows = {r["variant"]: r for r in results.ranking(sweep)}
    assert rows["B"]["vs"]["avg"]["verdict"] == "more" and rows["B"]["avg"]["n"] == sweep.runs_per_variant
    said = "\n".join(world.said)
    assert "Results:" in said and "faster" in said and "back as it was" in said
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
    assert float(argv[argv.index("--duration") + 1]) > runner.START_WAIT_S + sweep.window_s


def test_ctrl_c_during_a_run_stops_the_sweep_and_puts_the_file_back_and_resume_goes_on(tmp_path):
    sweep, folder, ini = new_sweep(tmp_path, seed=1)
    assert sweep.slots[2].variant == "B"                  # so B's value is in the file when Ctrl+C comes
    plays = steady_plays(sweep)
    plays[2] = Play(ctrl_c=True)
    world = World(tmp_path, sweep, FPS, answers=[GO] * 3, plays=plays[:3])
    assert runner.run(sweep, folder, world.seams()) == 130
    assert sweep.state == "stopped" and ini.read_bytes() == RAW and entries_left(world) == []
    assert sweep.runs[-1].state == "failed" and "Ctrl+C" in sweep.runs[-1].why
    assert plan.next_slot(sweep).n == 3
    assert "lagdoctor sweep --resume" in "\n".join(world.said)
    resumed = plan.load(folder)
    world2 = World(tmp_path, resumed, FPS, answers=[GO] * 4, plays=steady_plays(resumed)[2:])
    assert runner.run(resumed, folder, world2.seams()) == 0
    assert resumed.state == "finished" and world2.runs == len(resumed.slots) - 2 and ini.read_bytes() == RAW


def test_a_game_that_writes_its_own_value_back_stops_the_sweep(tmp_path):
    sweep, folder, ini = new_sweep(tmp_path, seed=1)
    first_b = next(i for i, s in enumerate(sweep.slots) if s.variant == "B")
    plays = steady_plays(sweep)
    plays[first_b] = Play(writes_back="2")
    world = World(tmp_path, sweep, FPS, answers=[GO] * (first_b + 1), plays=plays[:first_b + 1])
    code = runner.run(sweep, folder, world.seams())
    said = "\n".join(world.said)
    assert "game set sg.StaticMeshQuality to 2 when it closed" in said and "stops here" in said
    assert sweep.runs[-1].state == "rejected"
    # Something else set the value since: it is left alone, said with how to put the original back anyway.
    assert code == 2 and b"sg.StaticMeshQuality=2" in ini.read_bytes() and "--force" in said


def test_a_game_that_puts_the_original_back_also_stops_it_and_the_file_ends_as_it_was(tmp_path):
    sweep, folder, ini = new_sweep(tmp_path, seed=1)
    first_b = next(i for i, s in enumerate(sweep.slots) if s.variant == "B")
    plays = steady_plays(sweep)
    plays[first_b] = Play(writes_back="3")
    world = World(tmp_path, sweep, FPS, answers=[GO] * (first_b + 1), plays=plays[:first_b + 1])
    assert runner.run(sweep, folder, world.seams()) == 1
    assert ini.read_bytes() == RAW and entries_left(world) == [] and sweep.state == "stopped"


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
    (Play(code=2), "the game was not found"),
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
    first_b = next(i for i, s in enumerate(sweep.slots) if s.variant == "B")
    world = World(tmp_path, sweep, FPS, answers=[GO] * (first_b + 1), plays=steady_plays(sweep))

    def broken(*a, **kw):
        if world.variant_on_disk() == "B":
            raise ZeroDivisionError("a bug")
        return real(*a, **kw)
    real = results.window_metrics
    monkeypatch.setattr(results, "window_metrics", broken)
    with pytest.raises(ZeroDivisionError):
        runner.run(sweep, folder, world.seams())
    assert ini.read_bytes() == RAW and entries_left(world) == [] and plan.load(folder).state == "stopped"


def test_a_start_key_another_program_holds_stops_the_sweep_before_anything_records(tmp_path):
    sweep, folder, ini = new_sweep(tmp_path, seed=1)
    world = World(tmp_path, sweep, FPS, answers=[GO])
    world.listener_ok = False
    world.quits_after(3)                                  # the player closes the game the sweep never recorded
    assert runner.run(sweep, folder, world.seams()) == 1
    assert world.runs == 0 and "Ctrl+Alt+S could not be registered" in "\n".join(world.said)
    assert ini.read_bytes() == RAW


def test_a_press_before_the_recording_has_begun_is_ignored(tmp_path):
    sweep, folder, _ = new_sweep(tmp_path, runs=2, warm_up=False)
    plays = steady_plays(sweep)
    plays[0] = Play(press_early=True)
    world = World(tmp_path, sweep, FPS, answers=[GO] * 4, plays=plays)
    assert runner.run(sweep, folder, world.seams()) == 0
    assert sweep.runs[0].start_t == pytest.approx(PRESS_T)


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


def test_a_power_mode_that_changed_keeps_the_run_out(tmp_path):
    sweep, folder, _ = new_sweep(tmp_path, runs=2, warm_up=False)
    plays = steady_plays(sweep)
    plays.insert(1, Play(capture={"power_limit_w": 55.0}))
    world = World(tmp_path, sweep, FPS, answers=[GO, GO, "", GO, GO, GO], plays=plays)
    assert runner.run(sweep, folder, world.seams()) == 0
    rejected = [r for r in sweep.runs if r.state == "rejected"]
    assert len(rejected) == 1 and "power limit changed from 103 W to 55 W" in rejected[0].why


def test_an_updated_game_stops_the_sweep(tmp_path):
    sweep, folder, ini = new_sweep(tmp_path, runs=2, warm_up=False)
    plays = steady_plays(sweep)
    plays[1] = Play(capture={"exe_bytes": 82_000_000})
    world = World(tmp_path, sweep, FPS, answers=[GO, GO], plays=plays)
    assert runner.run(sweep, folder, world.seams()) == 1
    assert "updated" in "\n".join(world.said) and ini.read_bytes() == RAW


def test_a_file_changed_between_sessions_stops_a_resumed_sweep_before_it_writes(tmp_path):
    sweep, folder, ini = new_sweep(tmp_path, runs=2, warm_up=False)
    world = World(tmp_path, sweep, FPS, answers=[GO, "q"], plays=steady_plays(sweep))
    assert runner.run(sweep, folder, world.seams()) == 0 and ini.read_bytes() == RAW
    ini.write_bytes(RAW.replace(b"sg.StaticMeshQuality=3", b"sg.StaticMeshQuality=2"))    # the player's menu
    resumed = plan.load(folder)
    world2 = World(tmp_path, resumed, FPS, answers=[GO] * 4, plays=steady_plays(resumed))
    assert runner.run(resumed, folder, world2.seams()) == 1
    assert "is now 2 in the settings file, not the 3" in "\n".join(world2.said)
    assert world2.runs == 0 and ini.read_bytes() == RAW.replace(b"=3", b"=2")


def test_a_second_ctrl_c_while_waiting_for_the_game_leaves_the_setting_for_later(tmp_path):
    sweep, folder, ini = new_sweep(tmp_path, seed=1)
    first_b = next(i for i, s in enumerate(sweep.slots) if s.variant == "B")
    plays = steady_plays(sweep)
    plays[first_b] = Play(ctrl_c=True, stays_running=True)
    world = World(tmp_path, sweep, FPS, answers=[GO] * (first_b + 1), plays=plays)

    def second_ctrl_c(seconds):
        if world.running:
            raise KeyboardInterrupt
    world.sleep = second_ctrl_c
    assert runner.run(sweep, folder, world.seams()) == 2
    said = "\n".join(world.said)
    assert "Close the game" in said and "lagdoctor sweep --resume" in said and "lagdoctor undo --all" in said
    assert b"sg.StaticMeshQuality=1" in ini.read_bytes() and len(entries_left(world)) == 1
    assert plan.load(folder).entries == entries_left(world)
    # Later, with the game closed, a resume first puts the setting back, then goes on.
    world.running = False
    resumed = plan.load(folder)
    world2 = World(tmp_path, resumed, FPS, answers=[GO] * len(resumed.slots), plays=steady_plays(resumed))
    world2.journal = world.journal
    world2.fix = world.fix
    assert runner.run(resumed, folder, world2.seams()) == 0
    assert ini.read_bytes() == RAW and entries_left(world2) == []
