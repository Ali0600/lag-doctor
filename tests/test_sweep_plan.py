"""A sweep's settings, the order of its runs and its state file (lagdoctor/sweep/plan.py)."""
from __future__ import annotations

import json
import random

import pytest

from lagdoctor.sweep import plan
from lagdoctor.sweep.plan import Edit, Run, SweepError

TEXT = ("[ScalabilityGroups]\r\nsg.ShadowQuality=4\r\nsg.StaticMeshQuality=3\r\n"
        "[/Script/Engine.GameUserSettings]\r\nbUseVSync=False\r\n[Other]\r\nsg.ShadowQuality=2\r\n")


def make(tries=("sg.StaticMeshQuality=1",), runs=2, warm_up=True, seed=7) -> plan.Sweep:
    resolved, original = plan.resolve(TEXT, [plan.parse_try(t) for t in tries])
    variants = plan.make_variants(resolved, original)
    slots = plan.make_slots([v.id for v in variants], runs, warm_up, random.Random(seed))
    return plan.Sweep(game="Game.exe", settings_file=r"C:\x\GameUserSettings.ini", window_s=60.0,
                      runs_per_variant=runs, warm_up=warm_up, start_key="ctrl+alt+s", seed=seed, variants=variants,
                      original=original, slots=slots, created_at="2026-11-09T20:00:00+01:00", tool_version="0.1.0")


@pytest.mark.parametrize("text,want", [
    ("sg.StaticMeshQuality=1", [(None, "sg.StaticMeshQuality", "1")]),
    (" sg.X = 1 , sg.Y=2 ", [(None, "sg.X", "1"), (None, "sg.Y", "2")]),
    ("[ScalabilityGroups]sg.X=1", [("ScalabilityGroups", "sg.X", "1")]),
    ("[/Script/Engine.GameUserSettings] bUseVSync=True", [("/Script/Engine.GameUserSettings", "bUseVSync", "True")]),
])
def test_a_try_is_read_into_its_values(text, want):
    assert plan.parse_try(text) == want


@pytest.mark.parametrize("text", ["", "sg.X", "sg.X=", "=1", "sg.X=1,", "sg.X=1,,sg.Y=2", "sg.X=1;2", "sg X=1",
                                  "sg.X=1, sg.x=2", "[A B]sg.X=1", "a=1,b=1,c=1,d=1,e=1"])
def test_a_try_that_is_unclear_is_refused(text):
    with pytest.raises(SweepError):
        plan.parse_try(text)


def test_a_key_is_placed_in_the_one_section_that_has_it_and_its_original_kept():
    resolved, original = plan.resolve(TEXT, [plan.parse_try("sg.StaticMeshQuality=1")])
    assert resolved == [[Edit("ScalabilityGroups", "sg.StaticMeshQuality", "1")]]
    assert original == (Edit("ScalabilityGroups", "sg.StaticMeshQuality", "3"),)


def test_a_key_in_two_sections_needs_one_named():
    with pytest.raises(SweepError, match=r"more than one section.*\[ScalabilityGroups\]sg.ShadowQuality=1"):
        plan.resolve(TEXT, [plan.parse_try("sg.ShadowQuality=1")])
    resolved, original = plan.resolve(TEXT, [plan.parse_try("[other]sg.ShadowQuality=1")])
    assert resolved == [[Edit("Other", "sg.ShadowQuality", "1")]]
    assert original == (Edit("Other", "sg.ShadowQuality", "2"),)


@pytest.mark.parametrize("text,why", [("sg.Missing=1", "not in the settings file"),
                                      ("[ScalabilityGroups]bUseVSync=True", "not set in"),
                                      ("sg.StaticMeshQuality=3", "already 3")])
def test_a_try_the_file_cannot_take_is_refused(text, why):
    with pytest.raises(SweepError, match=why):
        plan.resolve(TEXT, [plan.parse_try(text)])


def test_every_original_is_kept_once_whichever_try_names_it_first():
    tries = ["sg.StaticMeshQuality=1", "[ScalabilityGroups]sg.ShadowQuality=1, sg.StaticMeshQuality=2"]
    resolved, original = plan.resolve(TEXT, [plan.parse_try(t) for t in tries])
    assert original == (Edit("ScalabilityGroups", "sg.StaticMeshQuality", "3"),
                        Edit("ScalabilityGroups", "sg.ShadowQuality", "4"))
    variants = plan.make_variants(resolved, original)
    assert [(v.id, v.label) for v in variants] == [
        ("A", "as it was (sg.StaticMeshQuality=3, sg.ShadowQuality=4)"), ("B", "sg.StaticMeshQuality=1"),
        ("C", "sg.ShadowQuality=1, sg.StaticMeshQuality=2")]


def test_the_file_holds_each_settings_values_and_the_originals_for_the_rest():
    s = make(tries=("sg.StaticMeshQuality=1", "[ScalabilityGroups]sg.ShadowQuality=2"))
    assert plan.values_for(s, "A") == [Edit("ScalabilityGroups", "sg.StaticMeshQuality", "3"),
                                       Edit("ScalabilityGroups", "sg.ShadowQuality", "4")]
    assert plan.values_for(s, "B") == [Edit("ScalabilityGroups", "sg.StaticMeshQuality", "1"),
                                       Edit("ScalabilityGroups", "sg.ShadowQuality", "4")]
    assert plan.values_for(s, "C") == [Edit("ScalabilityGroups", "sg.StaticMeshQuality", "3"),
                                       Edit("ScalabilityGroups", "sg.ShadowQuality", "2")]


def test_two_tries_that_give_the_same_setting_are_refused():
    resolved, original = plan.resolve(TEXT, [plan.parse_try("sg.StaticMeshQuality=1"),
                                             plan.parse_try("sg.staticmeshquality=1")])
    with pytest.raises(SweepError, match="same setting"):
        plan.make_variants(resolved, original)
    with pytest.raises(SweepError, match="at least one"):
        plan.make_variants([], original)
    with pytest.raises(SweepError, match="at most"):
        plan.make_variants([[Edit("S", "k", str(i))] for i in range(plan.TRIES_MAX + 1)], original)


@pytest.mark.parametrize("warm_up", [True, False])
def test_every_block_holds_every_setting_once_and_only_the_warm_up_goes_uncounted(warm_up):
    slots = plan.make_slots(["A", "B", "C"], 3, warm_up, random.Random(1))
    assert [s.n for s in slots] == list(range(1, len(slots) + 1))
    blocks = sorted({s.block for s in slots})
    assert blocks == ([0] if warm_up else []) + [1, 2, 3]
    for b in blocks:
        assert sorted(s.variant for s in slots if s.block == b) == ["A", "B", "C"]
        assert all(s.counted is (b > 0) for s in slots if s.block == b)


def test_the_order_is_shuffled_and_the_same_seed_gives_the_same_order():
    orders = {tuple(s.variant for s in plan.make_slots(list("ABCD"), 4, True, random.Random(seed)))
              for seed in range(8)}
    assert len(orders) > 1
    one = [s.variant for s in plan.make_slots(list("ABCD"), 4, True, random.Random(42))]
    assert one == [s.variant for s in plan.make_slots(list("ABCD"), 4, True, random.Random(42))]
    with pytest.raises(SweepError):
        plan.make_slots(["A", "B"], plan.RUNS_MAX + 1, True, random.Random(1))


@pytest.mark.parametrize("seed", range(6))
def test_the_counted_blocks_rotate_one_order_so_every_setting_takes_every_place(seed):
    # Independent shuffles would leave one order in every block 1 time in 4 with two settings and three blocks;
    # rotating one order (A B C, then B C A, then C A B) balances the places by construction.
    ids = list("ABC")
    slots = plan.make_slots(ids, 3, True, random.Random(seed))
    blocks = [[s.variant for s in slots if s.block == b] for b in (1, 2, 3)]
    assert blocks[1] == blocks[0][1:] + blocks[0][:1] and blocks[2] == blocks[1][1:] + blocks[1][:1]
    for place in range(3):
        assert sorted(b[place] for b in blocks) == ids
    two = plan.make_slots(["A", "B"], 2, False, random.Random(seed))
    assert [s.variant for s in two][:2] != [s.variant for s in two][2:]


def test_the_files_other_values_are_kept_and_their_changes_said():
    resolved, original = plan.resolve(TEXT, [plan.parse_try("sg.StaticMeshQuality=1")])
    others = plan.other_values(TEXT, original)
    assert others == (Edit("ScalabilityGroups", "sg.ShadowQuality", "4"),
                      Edit("/Script/Engine.GameUserSettings", "bUseVSync", "False"),
                      Edit("Other", "sg.ShadowQuality", "2"))
    later = plan.other_values(TEXT.replace("bUseVSync=False", "bUseVSync=FALSE").replace("ShadowQuality=4", "ShadowQuality=1")
                              + "NewKey=7\r\n", original)
    # bUseVSync=FALSE means the same as False; the key in two sections is named with its section.
    assert plan.others_changed(others, later) == ["[ScalabilityGroups]sg.ShadowQuality 4 -> 1", "NewKey=7 is new"]
    assert plan.others_changed(others, others) == []
    gone = plan.other_values(TEXT.replace("bUseVSync=False\r\n", ""), original)
    assert plan.others_changed(others, gone) == ["bUseVSync is gone (it was False)"]


def test_an_original_that_could_not_be_written_back_is_refused_and_an_equal_number_is_already_set():
    long = TEXT.replace("sg.StaticMeshQuality=3", "sg.StaticMeshQuality=" + "9" * 5000)
    with pytest.raises(SweepError, match="could not be written back"):
        plan.resolve(long, [plan.parse_try("sg.StaticMeshQuality=1")])
    with pytest.raises(SweepError, match="already 3"):
        plan.resolve(TEXT, [plan.parse_try("sg.StaticMeshQuality=3.000000")])


def test_a_good_run_or_a_skip_settles_a_slot_and_a_failed_or_rejected_one_does_not():
    s = make()
    first = s.slots[0]
    assert plan.next_slot(s) == first
    for state in ("failed", "rejected"):
        s.runs.append(Run(first.n, first.variant, state, "why"))
        assert plan.next_slot(s) == first
    s.runs.append(Run(first.n, first.variant, "good"))
    assert plan.next_slot(s) == s.slots[1]
    s.runs.append(Run(s.slots[1].n, s.slots[1].variant, "skipped"))
    assert plan.next_slot(s) == s.slots[2]


def test_warm_up_runs_are_never_counted():
    s = make()
    for slot in s.slots:
        s.runs.append(Run(slot.n, slot.variant, "good", metrics={"avg_fps": 60.0}))
    counted = plan.counted_runs(s, "B")
    assert [r.slot for r in counted] == [x.n for x in s.slots if x.variant == "B" and x.counted]
    assert len(counted) == s.runs_per_variant


def test_the_state_file_round_trips(tmp_path):
    s = make(tries=("sg.StaticMeshQuality=1", "[ScalabilityGroups]sg.ShadowQuality=2"))
    s.runs.append(Run(1, s.slots[0].variant, "good", "", "20261109-200000-game", 12.5,
                      {"avg_fps": 60.0, "focused": None, "hitches": 0}, {"on_battery": False, "power_plan": "x"},
                      "2026-11-09T20:05:00+01:00"))
    s.reference = {"on_battery": False}
    s.others = (Edit("ScalabilityGroups", "sg.TextureQuality", "2"), Edit("Other", "Empty", ""))
    s.notes.append("from run 2 on, the runs counted these changed values: sg.TextureQuality 3 -> 2")
    s.state, s.why_ended = "ended", "the game was updated"
    plan.save(s, tmp_path)
    assert plan.load(tmp_path) == s
    assert not list(tmp_path.glob("*.tmp"))


def test_a_long_reason_and_label_still_load_back(tmp_path):
    # What the sweep saves must always load: a run's reasons joined, a label of four long values.
    s = make()
    s.variants[1] = plan.Variant("B", "k" * 4 * 200, s.variants[1].edits)
    s.runs.append(Run(1, s.slots[0].variant, "rejected", "; ".join(["a reason of some length " * 3] * 30)[:3000]))
    plan.save(s, tmp_path)
    assert plan.load(tmp_path) == s


def _broken(tmp_path, change) -> None:
    data = plan.to_json(make())
    change(data)
    (tmp_path / plan.STATE_NAME).write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(SweepError):
        plan.load(tmp_path)


@pytest.mark.parametrize("change", [
    lambda d: d.update(schema="lagdoctor.sweep/2"),
    lambda d: d["slots"][0].update(variant="Z"),
    lambda d: d["slots"].pop(),
    lambda d: d["slots"][0].update(counted=not d["slots"][0]["counted"]),
    lambda d: d["variants"][1]["edits"][0].update(value="1\r\n[Evil]"),
    lambda d: d["variants"][1]["edits"][0].update(key="sg.NotAnOriginal"),
    lambda d: d.update(runs_per_variant=1),
    lambda d: d.update(window_s=5),
    lambda d: d.update(state="done"),
    lambda d: d.update(others=[{"section": "S", "key": "k", "value": "1\r\n[Evil]"}]),
    lambda d: d.update(notes="not a list"),
    lambda d: d.update(game="-x.exe"),
    lambda d: d.update(state="done"),
    lambda d: d["runs"].append({"slot": 1, "variant": d["slots"][0]["variant"], "state": "good", "why": "",
                                "session": "..", "start_t": None, "metrics": {}, "conditions": {}, "at": ""}),
    lambda d: d["runs"].append({"slot": 1, "variant": d["slots"][0]["variant"], "state": "great", "why": "",
                                "session": None, "start_t": None, "metrics": {}, "conditions": {}, "at": ""}),
    lambda d: d["runs"].append({"slot": 1, "variant": d["slots"][0]["variant"], "state": "good", "why": "",
                                "session": None, "start_t": None, "metrics": {"x": {"nested": 1}},
                                "conditions": {}, "at": ""}),
    lambda d: d.pop("original"),
])
def test_a_damaged_state_file_is_refused(tmp_path, change):
    _broken(tmp_path, change)


def test_a_state_file_that_is_not_json_or_too_large_is_refused(tmp_path):
    with pytest.raises(SweepError, match="no sweep"):
        plan.load(tmp_path)
    (tmp_path / plan.STATE_NAME).write_bytes(b"\xff not json")
    with pytest.raises(SweepError, match="not JSON"):
        plan.load(tmp_path)
    (tmp_path / plan.STATE_NAME).write_bytes(b" " * (plan.STATE_MAX + 1))
    with pytest.raises(SweepError, match="too large"):
        plan.load(tmp_path)
