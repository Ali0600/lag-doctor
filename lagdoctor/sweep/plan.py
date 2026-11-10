"""What a settings sweep tries, in which order, and what it has found so far: the sweep's state file, sweep.json.

A sweep is fixed when it starts: the settings file, the settings tried (A is the file as it was, B onwards one `--try`
each), how many counted runs each gets, the window measured from the start key, and the order of every run. The order
comes in blocks, each holding every setting once in a shuffled order, so a steady drift (heat, caches) spreads over all
of them instead of landing on one (docs/research/settings-sweeps.md, section 4: back-to-back trials once showed a false
38 % difference between identical systems). The first block warms up and is not counted: a setting's first run can be
slower (shader caches), and the laptop is still heating up.

The state file is written whole after every step (a temp file, flushed, then replaced), so a sweep stopped or killed at
any point can go on with `lagdoctor sweep --resume`. Everything here is pure except save() and load().
"""
from __future__ import annotations

import json
import math
import os
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lagdoctor.sweep import inifile

SCHEMA = "lagdoctor.sweep/1"
STATE_NAME = "sweep.json"
RUNS_DIR = "runs"                     # each run's recording folder, as watch names it
ORIGINAL_PREFIX = "original-"         # a byte copy of the settings file as it was when the sweep began
STATE_MAX = 4 << 20                   # a state file is a few dozen KB; anything past this is not one of ours

RUNS_MIN, RUNS_MAX, RUNS_DEFAULT = 2, 10, 3                     # counted runs of each setting
WINDOW_MIN_S, WINDOW_MAX_S, WINDOW_DEFAULT_S = 30.0, 600.0, 60.0  # 30 s keeps 1,000 frames for a 1 % low at 34 FPS
TRIES_MAX = 5                         # settings tried besides the file as it was
EDITS_MAX = 4                         # values one --try changes
# The key pressed in the game at the spot: the plan's start key (docs/LAUNCHER-PLAN.md), a feel number the owner has not
# judged yet. A sweep registers it only while its runs record; watch's own start key (M5) does not exist yet.
START_KEY = "ctrl+alt+s"
VARIANT_IDS = "ABCDEF"
SWEEP_STATES = ("running", "stopped", "finished")
RUN_STATES = ("good", "rejected", "failed", "skipped")
_TRY_PART = re.compile(r"(?:\[([^\]]*)\])?[ \t]*([^=\s]+)[ \t]*=[ \t]*(\S+)")


class SweepError(ValueError):
    """A sweep that cannot be built or continued; the message is one plain sentence."""


@dataclass(frozen=True)
class Edit:
    section: str
    key: str
    value: str

    def text(self) -> str:
        return f"{self.key}={self.value}"


@dataclass(frozen=True)
class Variant:
    id: str                         # "A" is the file as it was
    label: str
    edits: tuple[Edit, ...]


@dataclass(frozen=True)
class Slot:
    n: int                          # the run's number in the order, from 1
    block: int                      # 0 is the warm-up block, when the sweep warms up
    variant: str
    counted: bool


@dataclass
class Run:
    """One attempt at a slot. A slot is settled by a good run or a skip; a rejected or failed one may be tried again."""

    slot: int
    variant: str
    state: str                      # good, rejected, failed, skipped
    why: str = ""                   # for rejected and failed: what kept it out, in plain words
    session: str | None = None      # its recording's folder under runs/
    start_t: float | None = None    # the start key's press, in the recording's seconds
    metrics: dict[str, Any] = field(default_factory=dict)
    conditions: dict[str, Any] = field(default_factory=dict)
    at: str = ""                    # when it ended (ISO 8601)


@dataclass
class Sweep:
    game: str
    settings_file: str
    window_s: float
    runs_per_variant: int
    warm_up: bool
    start_key: str
    seed: int
    variants: list[Variant]
    original: tuple[Edit, ...]      # every value a setting changes, as the file held it when the sweep began
    slots: list[Slot]
    runs: list[Run] = field(default_factory=list)
    state: str = "running"
    entries: list[str] = field(default_factory=list)    # the journal entries of the edits in place now
    reference: dict[str, Any] | None = None             # the first good run's conditions
    created_at: str = ""
    tool_version: str = ""

    def variant(self, vid: str) -> Variant:
        for v in self.variants:
            if v.id == vid:
                return v
        raise KeyError(vid)


# ---------------------------------------------------------------- building --

def parse_try(text: str) -> list[tuple[str | None, str, str]]:
    """One --try: "sg.StaticMeshQuality=1", several joined by commas, each optionally with its section in front
    ("[ScalabilityGroups]sg.StaticMeshQuality=1"). Returns (section or None, key, value) per value."""
    if not isinstance(text, str) or not text.strip():
        raise SweepError("a --try needs at least one KEY=VALUE")
    parts = [p.strip() for p in text.split(",")]
    if any(not p for p in parts):
        raise SweepError(f"{text!r} has an empty part between its commas")
    if len(parts) > EDITS_MAX:
        raise SweepError(f"one --try changes at most {EDITS_MAX} values, and {text!r} has {len(parts)}")
    out: list[tuple[str | None, str, str]] = []
    seen: set[str] = set()
    for part in parts:
        m = _TRY_PART.fullmatch(part)
        if m is None:
            raise SweepError(f"{part!r} is not KEY=VALUE (for example sg.ShadowQuality=1)")
        section = m.group(1).strip() if m.group(1) is not None else None
        key, value = m.group(2), m.group(3)
        if section is not None and not inifile.SECTION.fullmatch(section):
            raise SweepError(f"[{section}] is not a section name lag-doctor writes under")
        if not inifile.KEY.fullmatch(key):
            raise SweepError(f"{key!r} is not a setting's name (letters, digits, '.' and '_')")
        if not inifile.VALUE.fullmatch(value):
            raise SweepError(f"{value!r} is not a value a sweep writes (letters, digits, '.', '_', '+' and '-', at "
                             "most 64)")
        if key.casefold() in seen:
            raise SweepError(f"{text!r} sets {key} twice")
        seen.add(key.casefold())
        out.append((section, key, value))
    return out


def resolve(text: str, tries: list[list[tuple[str | None, str, str]]]) -> tuple[list[list[Edit]], tuple[Edit, ...]]:
    """Each try's values placed in their sections of the settings file's text, and the file's own values of every key
    a try changes (the originals, in the order first named). SweepError when a key is not in the file, is in several
    sections and none was named, is set twice, or already holds the value a try gives it."""
    resolved: list[list[Edit]] = []
    original: dict[tuple[str, str], Edit] = {}
    for one in tries:
        edits = []
        for section, key, value in one:
            if section is None:
                found = inifile.sections_with(text, key)
                if not found:
                    raise SweepError(f"{key} is not in the settings file, so a sweep cannot change it (it changes "
                                     "only values the file already has)")
                if len(found) > 1:
                    names = ", ".join(f"[{s}]" for s in found)
                    raise SweepError(f"{key} is in more than one section ({names}): name one, as [{found[0]}]{key}="
                                     f"{value}")
                section = found[0]
            try:
                now = inifile.get_value(text, section, key)
            except ValueError as exc:
                raise SweepError(str(exc)) from None
            if now is None:
                raise SweepError(f"{key} is not set in [{section}] of the settings file")
            spelled = next((s for s in inifile.sections_with(text, key) if s.casefold() == section.casefold()),
                           section)
            slot = (spelled.casefold(), key.casefold())
            original.setdefault(slot, Edit(spelled, key, now))
            if now == value:
                raise SweepError(f"{key} is already {value} in the settings file, so trying {key}={value} changes "
                                 "nothing")
            edits.append(Edit(original[slot].section, original[slot].key, value))
        resolved.append(edits)
    return resolved, tuple(original.values())


def make_variants(tries: list[list[Edit]], original: tuple[Edit, ...]) -> list[Variant]:
    """A (the file as it was) and one setting per try, B onwards. SweepError for no try, too many, or two the same."""
    if not tries:
        raise SweepError("a sweep needs at least one --try")
    if len(tries) > TRIES_MAX:
        raise SweepError(f"a sweep tries at most {TRIES_MAX} settings besides the file as it was")
    base = ", ".join(e.text() for e in original)
    out = [Variant("A", f"as it was ({base})", ())]
    seen: dict[frozenset, str] = {}
    for vid, edits in zip(VARIANT_IDS[1:], tries):
        key = frozenset((e.section.casefold(), e.key.casefold(), e.value) for e in edits)
        if key in seen:
            raise SweepError(f"two --try options give the same setting ({seen[key]})")
        label = ", ".join(e.text() for e in edits)
        seen[key] = label
        out.append(Variant(vid, label, tuple(edits)))
    return out


def make_slots(variant_ids: list[str], runs: int, warm_up: bool, rng: random.Random) -> list[Slot]:
    """The order of every run: blocks, each holding every setting once in a shuffled order; the warm-up block
    (block 0) first when there is one. Its runs are not counted."""
    if not RUNS_MIN <= runs <= RUNS_MAX:
        raise SweepError(f"runs must be from {RUNS_MIN} to {RUNS_MAX}")
    blocks = ([0] if warm_up else []) + list(range(1, runs + 1))
    slots: list[Slot] = []
    for block in blocks:
        order = list(variant_ids)
        rng.shuffle(order)
        for vid in order:
            slots.append(Slot(len(slots) + 1, block, vid, counted=block > 0))
    return slots


def values_for(sweep: Sweep, vid: str) -> list[Edit]:
    """What the file must hold for a setting: for every key any setting changes, that setting's value if it changes
    the key, else the file's own."""
    mine = {(e.section.casefold(), e.key.casefold()): e.value for e in sweep.variant(vid).edits}
    return [Edit(o.section, o.key, mine.get((o.section.casefold(), o.key.casefold()), o.value)) for o in sweep.original]


def settled(sweep: Sweep, n: int) -> bool:
    return any(r.slot == n and r.state in ("good", "skipped") for r in sweep.runs)


def next_slot(sweep: Sweep) -> Slot | None:
    """The first slot no good run or skip has settled; None once every one is."""
    return next((s for s in sweep.slots if not settled(sweep, s.n)), None)


def counted_runs(sweep: Sweep, vid: str) -> list[Run]:
    """The good runs of a setting's counted slots, in order."""
    counted = {s.n for s in sweep.slots if s.counted}
    return [r for r in sweep.runs if r.variant == vid and r.state == "good" and r.slot in counted]


# ------------------------------------------------------------ the state file --

def to_json(s: Sweep) -> dict[str, Any]:
    return {
        "schema": SCHEMA, "created_at": s.created_at, "tool_version": s.tool_version, "state": s.state,
        "game": s.game, "settings_file": s.settings_file, "window_s": s.window_s,
        "runs_per_variant": s.runs_per_variant, "warm_up": s.warm_up, "start_key": s.start_key, "seed": s.seed,
        "variants": [{"id": v.id, "label": v.label, "edits": [_edit_json(e) for e in v.edits]} for v in s.variants],
        "original": [_edit_json(e) for e in s.original],
        "slots": [{"n": x.n, "block": x.block, "variant": x.variant, "counted": x.counted} for x in s.slots],
        "runs": [{"slot": r.slot, "variant": r.variant, "state": r.state, "why": r.why, "session": r.session,
                  "start_t": r.start_t, "metrics": r.metrics, "conditions": r.conditions, "at": r.at} for r in s.runs],
        "entries": list(s.entries), "reference": s.reference,
    }


def _edit_json(e: Edit) -> dict[str, str]:
    return {"section": e.section, "key": e.key, "value": e.value}


def _need(cond: bool, what: str) -> None:
    if not cond:
        raise SweepError(f"the sweep's state file is damaged ({what})")


def _text(v: Any, what: str, limit: int = 300) -> str:
    _need(isinstance(v, str) and len(v) <= limit, what)
    return v


def _edit(v: Any, what: str, *, original: bool = False) -> Edit:
    _need(isinstance(v, dict) and set(v) == {"section", "key", "value"}, what)
    section, key, value = v["section"], v["key"], v["value"]
    _need(isinstance(section, str) and inifile.SECTION.fullmatch(section) is not None, what + ": its section")
    _need(isinstance(key, str) and inifile.KEY.fullmatch(key) is not None, what + ": its key")
    good = inifile.line_safe(value) if original else isinstance(value, str) and inifile.VALUE.fullmatch(value)
    _need(bool(good), what + ": its value")
    return Edit(section, key, value)


def _number(v: Any) -> bool:
    return v is None or (isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v))


def _plain(v: Any, what: str) -> dict[str, Any]:
    """A run's metrics or conditions: names to numbers, text, booleans or None, nothing nested."""
    _need(isinstance(v, dict) and len(v) <= 64, what)
    for k, x in v.items():
        _need(isinstance(k, str) and len(k) <= 64, what)
        _need(_number(x) or isinstance(x, bool) or (isinstance(x, str) and len(x) <= 300), what)
    return dict(v)


def from_json(d: Any) -> Sweep:
    """A Sweep from its state file's JSON, every field checked. SweepError for anything another lag-doctor or a
    damaged file left there."""
    if not isinstance(d, dict) or d.get("schema") != SCHEMA:
        raise SweepError("this is not a sweep's state file lag-doctor can read (another schema)")
    try:
        variants = []
        for i, v in enumerate(d["variants"]):
            _need(isinstance(v, dict) and set(v) == {"id", "label", "edits"}, "a setting")
            _need(v["id"] == VARIANT_IDS[i], "a setting's id")
            edits = tuple(_edit(e, "a setting's value") for e in v["edits"])
            _need(bool(edits) == (i > 0) and len(edits) <= EDITS_MAX, "a setting's values")
            variants.append(Variant(v["id"], _text(v["label"], "a setting's label"), edits))
        _need(2 <= len(variants) <= TRIES_MAX + 1, "the settings")
        ids = [v.id for v in variants]
        original = tuple(_edit(e, "an original value", original=True) for e in d["original"])
        _need(bool(original), "the original values")
        keys = {(o.section.casefold(), o.key.casefold()) for o in original}
        _need(len(keys) == len(original), "the original values")
        for v in variants:
            _need(all((e.section.casefold(), e.key.casefold()) in keys for e in v.edits), "a setting's key")
        runs_per = d["runs_per_variant"]
        _need(isinstance(runs_per, int) and not isinstance(runs_per, bool) and RUNS_MIN <= runs_per <= RUNS_MAX,
              "runs")
        window = d["window_s"]
        _need(_number(window) and window is not None and WINDOW_MIN_S <= window <= WINDOW_MAX_S, "the window")
        _need(isinstance(d["warm_up"], bool), "the warm-up")
        seed = d["seed"]
        _need(isinstance(seed, int) and not isinstance(seed, bool), "the seed")
        slots = []
        for i, x in enumerate(d["slots"]):
            _need(isinstance(x, dict) and set(x) == {"n", "block", "variant", "counted"}, "a run's place")
            _need(x["n"] == i + 1 and x["variant"] in ids and isinstance(x["block"], int)
                  and not isinstance(x["block"], bool) and 0 <= x["block"] <= RUNS_MAX
                  and x["counted"] is (x["block"] > 0), "a run's place")
            slots.append(Slot(x["n"], x["block"], x["variant"], x["counted"]))
        _need(len(slots) == len(variants) * (runs_per + (1 if d["warm_up"] else 0)), "the order of the runs")
        runs = []
        for r in d["runs"]:
            _need(isinstance(r, dict) and set(r) == {"slot", "variant", "state", "why", "session", "start_t",
                                                     "metrics", "conditions", "at"}, "a run")
            _need(isinstance(r["slot"], int) and not isinstance(r["slot"], bool) and 1 <= r["slot"] <= len(slots)
                  and r["variant"] == slots[r["slot"] - 1].variant and r["state"] in RUN_STATES, "a run")
            session = r["session"]
            _need(session is None or (isinstance(session, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", session)
                                      is not None and session not in (".", "..")), "a run's folder")
            _need(_number(r["start_t"]), "a run's start")
            runs.append(Run(r["slot"], r["variant"], r["state"], _text(r["why"], "a run's reason", 600), session,
                            r["start_t"], _plain(r["metrics"], "a run's numbers"),
                            _plain(r["conditions"], "a run's conditions"), _text(r["at"], "a run's time", 40)))
        _need(len(runs) <= 4 * len(slots) + 20, "the runs")
        entries = d["entries"]
        _need(isinstance(entries, list) and len(entries) <= 64
              and all(isinstance(e, str) and re.fullmatch(r"[0-9a-f]{12}", e) for e in entries), "the journal entries")
        reference = d["reference"]
        _need(reference is None or isinstance(reference, dict), "the reference conditions")
        state = d["state"]
        _need(state in SWEEP_STATES, "the sweep's state")
        game = d["game"]
        _need(isinstance(game, str) and 0 < len(game) <= 260, "the game")
        return Sweep(game=game, settings_file=_text(d["settings_file"], "the settings file", 1024),
                     window_s=float(window), runs_per_variant=runs_per, warm_up=d["warm_up"],
                     start_key=_text(d["start_key"], "the start key", 40), seed=seed, variants=variants,
                     original=original, slots=slots, runs=runs, state=state, entries=list(entries),
                     reference=None if reference is None else _plain(reference, "the reference conditions"),
                     created_at=_text(d["created_at"], "the start time", 40),
                     tool_version=_text(d["tool_version"], "the version", 40))
    except (KeyError, TypeError) as exc:
        raise SweepError(f"the sweep's state file is damaged (missing or wrong: {exc})") from None


def save(sweep: Sweep, folder: Path) -> Path:
    """Write sweep.json whole: a temp file beside it, flushed, then replaced."""
    path = Path(folder) / STATE_NAME
    tmp = path.with_name(path.name + ".tmp")
    data = json.dumps(to_json(sweep), indent=1, allow_nan=False)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return path


def load(folder: Path) -> Sweep:
    """The sweep saved in `folder`. SweepError when there is none or it cannot be read."""
    path = Path(folder) / STATE_NAME
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise SweepError(f"no sweep is saved in {folder}") from None
    except OSError as exc:
        raise SweepError(f"{path} could not be read ({exc.strerror or exc})") from None
    if len(raw) > STATE_MAX:
        raise SweepError(f"{path} is too large to be a sweep's state file")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise SweepError(f"{path} is not a sweep's state file (it is not JSON)") from None
    return from_json(data)
