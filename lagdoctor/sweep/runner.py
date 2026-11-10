"""The settings sweep's loop: for each run, put the setting in the file, have the player play, record, and check.

Each run (the assisted sweep the owner chose, DECISIONS 2026-11-09):
1. The game must be closed: the game writes its settings back when it exits, so lag-doctor waits for it.
2. The run's setting goes into the file through the fix journal, every earlier edit of this sweep undone first, and
   every value the sweep changes is read back.
3. The player starts the game, loads a save, goes to a chosen spot and presses Enter here.
4. The recording starts (`watch`, with one UAC prompt unless this account records frame timing without one). The
   player goes back to the game and presses the start key at the spot: a rising chime, then `window_s` seconds are
   measured, a three-note chime, and the recording stops.
5. The player quits the game. Once it has closed, the file is read again: a game that put its own value back did not
   keep the setting, so the sweep stops there, since what the game used cannot be told.
6. The window is measured and checked (results.py). A run that cannot count is offered again.
When the sweep ends or stops, every edit is undone (once the game has closed), so the file holds what it held before.
Nothing is typed on the player's PC: lag-doctor only listens for the start key, which it registers for each run and
releases after it.

Every effect goes through `Seams`, so the tests drive whole sweeps with no game, recording or console.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from lagdoctor.capture.session import Observers, request_stop
from lagdoctor.cli import CAPTURE_NAME
from lagdoctor.model import NOTE_MAX, Capture
from lagdoctor.sweep import plan, results

WAIT_POLL_S = 1.0          # how often the closing game is looked for
CLOSED_SETTLE_S = 2.0      # after its process has gone, a moment for anything it wrote to land
START_WAIT_S = 300.0       # how long the start key may take once the recording has begun (a feel number)
WINDOW_DONE = "the sweep's window ended"
NO_PRESS = "the start key was not pressed"
NO_FRAMES = "frame timing is off for this run"

# watch's exit codes (cli.EXIT_*), as the sweep reads them: these stop the sweep, the others fail one run.
WATCH_STOPS = {4: "another recording is running", 5: "lag-doctor's files would land in another app's private copy",
               6: "frame timing is not available (run `lagdoctor setup`)"}
WATCH_FAILS = {2: "the game was not found when the recording started",
               3: "the game exited during the UAC prompt",
               1: "frame timing could not be started"}


class Stop(Exception):
    """The sweep stops here; the message says why, in plain words."""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class Seams:
    """Everything the loop does to the world. command.py gives the real ones."""

    say: Callable[[str], None]
    ask: Callable[[str], str | None]                      # a line typed at the prompt; None when input has ended
    record: Callable[[list[str], threading.Event, Observers], int]   # watch's arguments after "watch"; its exit code
    running: Callable[[str], bool]                        # is a process with the game's image name running?
    sleep: Callable[[float], None]
    hotkey: Callable[[str, Callable[[float], None]], Any]  # a listener: start() -> bool, stop(), error
    sound: Callable[[str], None]                          # "start", "done" or "problem"
    load: Callable[[Path], Capture]
    fix: Any                                              # a fixes.game_setting.GameSettingFix
    journal: Any                                          # a fixes.journal.Journal
    can_trace: Callable[[], bool]                         # frame timing without the relay's prompt?
    timer: Callable[[float, Callable[[], None]], Any]     # start(), cancel(): threading.Timer's
    now_iso: Callable[[], str]


# ------------------------------------------------------------------- run --

def run(sweep: plan.Sweep, folder: Path, seams: Seams) -> int:
    """Run the sweep's remaining runs, then put the settings back and say the results. Exit code: 0 finished or
    stopped at the player's word, 130 stopped by Ctrl+C, 1 stopped by a problem (said), 2 the settings could not
    all be put back (said, with how to do it). An error nobody expected still puts the settings back first, and is
    raised again after."""
    code, error = 0, None
    try:
        sweep.state = "running"
        plan.save(sweep, folder)
        while (slot := plan.next_slot(sweep)) is not None:
            _one(sweep, slot, folder, seams)
        sweep.state = "finished"
    except Stop as exc:
        sweep.state = "stopped"
        seams.say(str(exc))
        code = exc.code
    except KeyboardInterrupt:
        sweep.state = "stopped"
        seams.say("Stopped.")
        code = 130
    except Exception as exc:  # noqa: BLE001 - raised again below, once the settings are back
        sweep.state = "stopped"
        seams.say(f"The sweep stopped on an error lag-doctor did not expect ({type(exc).__name__}: {exc}).")
        code, error = 1, exc
    _save_quietly(sweep, folder, seams)
    if not put_back(sweep, folder, seams):
        code = 2
    _report(sweep, folder, seams)
    if error is not None:
        raise error
    return code


def _save_quietly(sweep: plan.Sweep, folder: Path, seams: Seams) -> None:
    """Save the state where a failed save must not keep the settings from going back."""
    try:
        plan.save(sweep, folder)
    except OSError as exc:
        seams.say(f"(The sweep's state could not be saved: {exc}.)")


def _one(sweep: plan.Sweep, slot: plan.Slot, folder: Path, seams: Seams) -> None:
    """One attempt at a slot: settled by a good run or a skip, or left for the next attempt."""
    v = sweep.variant(slot.variant)
    kind = "counted" if slot.counted else "warm-up, not counted"
    seams.say("")
    seams.say(f"Run {slot.n} of {len(sweep.slots)} ({kind}): {v.id}  {v.label}")
    wait_closed(sweep, seams, " before its settings file is changed")
    apply(sweep, v, folder, seams)
    held = ", ".join(e.text() for e in plan.values_for(sweep, v.id))
    seams.say(f"  The settings file now holds {held}.")
    while True:
        answer = seams.ask("  Start the game, load your save and go to your spot. Then press Enter here (q and Enter "
                           "stops the sweep): ")
        if answer is None or answer.strip().lower() in ("q", "quit", "stop"):
            raise Stop("Stopped as you asked.", code=0)
        if seams.running(sweep.game):
            break
        seams.say(f"  {sweep.game} is not running yet.")
    seams.say(f"  After the UAC prompt (if Windows asks), go back to the game and press {sweep.start_key.title()} at "
              f"your spot. A rising chime starts {sweep.window_s:.0f} s of measuring, and a three-note chime ends it. "
              "Play as you did in the other runs.")
    attempt, press = _record(sweep, slot, folder, seams)
    if seams.running(sweep.game):
        seams.say("  Quit the game now; lag-doctor waits for it to close.")
    wait_closed(sweep, seams, "")
    if attempt.state != "failed":
        _judge(sweep, attempt, press, folder, seams)
    changed = file_changed(sweep, v.id, seams)
    if changed is not None:
        attempt.state, attempt.why = "rejected", changed
    attempt.at = seams.now_iso()
    sweep.runs.append(attempt)
    plan.save(sweep, folder)
    if changed is not None:
        raise Stop(f"  {changed[0].upper()}{changed[1:]}. A sweep can only try settings the game reads from this file "
                   "and keeps, so it stops here.")
    if attempt.state == "good":
        m = attempt.metrics
        seams.say(f"  Run {slot.n}: {m.get('avg_fps') or 0:.1f} FPS, 1 % low "
                  f"{'-' if m.get('low1_fps') is None else format(m['low1_fps'], '.1f')}"
                  + ("" if slot.counted else " (warm-up)") + ".")
        return
    seams.say(f"  Run {slot.n} does not count: {attempt.why}.")
    answer = seams.ask("  Try this run again? [Y/n, or q to stop the sweep] ")
    if answer is None or answer.strip().lower() in ("q", "quit", "stop"):
        raise Stop("Stopped as you asked.", code=0)
    if answer.strip().lower() in ("n", "no"):
        sweep.runs.append(plan.Run(slot.n, slot.variant, "skipped", "skipped by you", at=seams.now_iso()))
        plan.save(sweep, folder)


# ------------------------------------------------------------- the file --

def _params(sweep: plan.Sweep, e: plan.Edit) -> dict[str, str]:
    return {"file": sweep.settings_file, "section": e.section, "key": e.key, "value": e.value, "game": sweep.game}


def _read(sweep: plan.Sweep, e: plan.Edit, seams: Seams) -> str:
    try:
        return seams.fix.read(_params(sweep, e))
    except (OSError, ValueError, LookupError) as exc:
        raise Stop(f"The settings file could not be read ({exc}), so the sweep stops.") from None


def apply(sweep: plan.Sweep, v: plan.Variant, folder: Path, seams: Seams) -> None:
    """Put the file back as it was, check it is, then write the setting's values through the journal and read every
    value back. Stop when any of that fails (nothing then stays changed that the journal does not hold)."""
    put_back_now(sweep, folder, seams)
    for o in sweep.original:
        now = _read(sweep, o, seams)
        if now != o.value:
            raise Stop(f"{o.key} is now {now} in the settings file, not the {o.value} it held when the sweep began (was "
                       "it changed in the game's menu?). The sweep stops, since its first setting would no longer be "
                       "the file as it was. Start a new sweep from the file as it is now.")
    for e in v.edits:
        params = _params(sweep, e)
        try:
            p = seams.fix.plan(params)
        except (OSError, ValueError, LookupError, RuntimeError) as exc:
            raise Stop(f"{e.key} could not be changed ({exc}), so the sweep stops.") from None
        res = seams.fix.apply(p, seams.journal)
        if res.entry_id and res.state in ("applied", "conflict"):
            sweep.entries.append(res.entry_id)            # held at once: a crash now still knows what to undo
            plan.save(sweep, folder)
        if not res.ok:
            raise Stop(f"{e.key} could not be changed: {res.message}. The sweep stops.")
    for e in plan.values_for(sweep, v.id):
        now = _read(sweep, e, seams)
        if now != e.value:
            raise Stop(f"{e.key} reads {now} after lag-doctor wrote {e.value}, so the sweep stops.")


def put_back_now(sweep: plan.Sweep, folder: Path, seams: Seams) -> None:
    """Undo this sweep's edits in place, newest first. Stop when one cannot be undone (its reason said, and how)."""
    for entry_id in reversed(list(sweep.entries)):
        try:
            entry = seams.journal.get(entry_id)
        except KeyError:
            entry = None
        if entry is None or entry.state not in ("applied", "conflict"):
            sweep.entries.remove(entry_id)                # undone already (`lagdoctor undo`), or gone from the journal
            plan.save(sweep, folder)
            continue
        res = seams.fix.undo(entry, seams.journal)
        if not res.ok:
            raise Stop(f"A setting could not be put back: {res.message}", code=2)
        sweep.entries.remove(entry_id)
        plan.save(sweep, folder)


def put_back(sweep: plan.Sweep, folder: Path, seams: Seams) -> bool:
    """At the end, or when the sweep stops: wait for the game to close, undo every edit, and say what the file holds
    now, read back. False (said, with how to do it later) when edits stay in place."""
    later = ("`lagdoctor sweep --resume` puts them back (and goes on with the sweep), and so does `lagdoctor undo "
             "--all`")
    if sweep.entries:
        try:
            if seams.running(sweep.game):
                seams.say(f"Close the game: lag-doctor puts your settings back once it has closed. (Ctrl+C leaves "
                          f"them as they are for now: {later}.)")
            wait_closed(sweep, seams, "")
            put_back_now(sweep, folder, seams)
        except KeyboardInterrupt:
            seams.say(f"Your settings were left as the sweep last set them: {later}, once the game is closed.")
            return False
        except Stop as exc:
            seams.say(f"Putting your settings back: {exc}")
            seams.say("`lagdoctor undo` lists what is still changed.")
            return False
        finally:
            _save_quietly(sweep, folder, seams)
    seams.say(file_now(sweep, seams))
    return True


def file_now(sweep: plan.Sweep, seams: Seams) -> str:
    """What the settings file holds now, read back, against what it held before the sweep."""
    held, other = [], []
    for o in sweep.original:
        try:
            value = seams.fix.read(_params(sweep, o))
        except (OSError, ValueError, LookupError) as exc:
            return f"The settings file could not be read back ({exc}); it should hold {o.text()} and the rest as before."
        held.append(o.text())
        if value != o.value:
            other.append(f"{o.key}={value} (it was {o.value} before the sweep)")
    if other:
        return ("Your settings file now holds " + ", ".join(other) + ": something other than lag-doctor changed it "
                "since (the game's menu?).")
    return f"Your settings file is back as it was ({', '.join(held)})."


def file_changed(sweep: plan.Sweep, vid: str, seams: Seams) -> str | None:
    """After a run, once the game has closed: the value the file holds now that is not the one written for the run, in
    words, or None when every value is still as written."""
    for e in plan.values_for(sweep, vid):
        try:
            now = seams.fix.read(_params(sweep, e))
        except (OSError, ValueError, LookupError) as exc:
            return f"the settings file could not be read after the run ({exc})"
        if now != e.value:
            return f"the game set {e.key} to {now} when it closed, not the {e.value} written for this run"
    return None


def wait_closed(sweep: plan.Sweep, seams: Seams, why: str) -> None:
    """Return once no process with the game's image name runs, and a moment more for what it wrote to land."""
    try:
        if not seams.running(sweep.game):
            return
        seams.say(f"  Waiting for {sweep.game} to close{why}...")
        while seams.running(sweep.game):
            seams.sleep(WAIT_POLL_S)
    except OSError as exc:
        raise Stop(f"Whether the game is running could not be read ({exc}), so the sweep stops.") from None
    seams.sleep(CLOSED_SETTLE_S)


# ------------------------------------------------------------ recording --

def watch_args(sweep: plan.Sweep, slot: plan.Slot, folder: Path, can_trace: bool) -> list[str]:
    """The recording's command line, after "watch": the game by name, no network checks and no lag key, a length
    that ends a forgotten run, the run's folder and its note. --elevate unless frame timing needs no prompt here."""
    v = sweep.variant(slot.variant)
    note = f"settings sweep run {slot.n}: {v.id} {v.label}"
    if len(note) > NOTE_MAX:
        note = note[:NOTE_MAX - 3].rstrip() + "..."
    return (["--game", sweep.game, "--no-network", "--hotkey", "none",
             "--duration", f"{START_WAIT_S + sweep.window_s + 30:.0f}", "--out", str(Path(folder) / plan.RUNS_DIR),
             "--note", note] + ([] if can_trace else ["--elevate"]))


def _record(sweep: plan.Sweep, slot: plan.Slot, folder: Path, seams: Seams) -> tuple[plan.Run, float | None]:
    """Record one run: the start key listened for, its window timed, the recording stopped at the window's end. The
    attempt (failed, or awaiting the judge) and the press's time on lag-doctor's clock."""
    (Path(folder) / plan.RUNS_DIR).mkdir(parents=True, exist_ok=True)
    attempt = plan.Run(slot.n, slot.variant, "failed")
    stop = threading.Event()
    lock = threading.Lock()
    seen: dict[str, Any] = {"recording": False, "press": None, "session": None}
    timers: list[Any] = []

    def later(seconds: float, fn: Callable[[], None]) -> None:
        t = seams.timer(seconds, fn)
        timers.append(t)
        t.start()

    def window_done() -> None:
        seams.sound("done")
        request_stop(stop, WINDOW_DONE)

    def no_press() -> None:
        with lock:
            pressed = seen["press"] is not None
        if not pressed:
            seams.sound("problem")
            request_stop(stop, NO_PRESS)

    def on_event(kind: str, data: dict[str, Any]) -> None:
        if kind == "recording":
            if not data.get("frames"):
                seams.sound("problem")
                request_stop(stop, NO_FRAMES)
                return
            with lock:
                seen["recording"] = True
            later(START_WAIT_S, no_press)
        elif kind == "saved":
            seen["session"] = data.get("session")

    def on_press(when: float) -> None:
        with lock:
            if not seen["recording"] or seen["press"] is not None or stop.is_set():
                return
            seen["press"] = when
        seams.sound("start")
        later(sweep.window_s, window_done)

    listener = seams.hotkey(sweep.start_key, on_press)
    try:
        started = bool(listener.start())
    except Exception as exc:  # noqa: BLE001 - said as a stop, never a traceback
        started, listener.error = False, f"{type(exc).__name__}: {exc}"
    if not started:
        raise Stop(f"The start key {sweep.start_key.title()} could not be registered "
                   f"({getattr(listener, 'error', '') or 'unknown reason'}). Close the program that uses it, or start "
                   "a new sweep with another --start-key.")
    try:
        code = seams.record(watch_args(sweep, slot, folder, seams.can_trace()), stop, Observers(on_event=on_event))
    except Exception as exc:  # noqa: BLE001 - a failed recording is said, and the settings still go back
        raise Stop(f"The recording failed ({type(exc).__name__}: {exc}), so the sweep stops.") from None
    finally:
        for t in timers:
            t.cancel()
        listener.stop()
    reason = getattr(stop, "reason", "")
    attempt.session = seen["session"]
    if reason == "Ctrl+C":
        attempt.why = "stopped with Ctrl+C"
        attempt.at = seams.now_iso()
        sweep.runs.append(attempt)
        plan.save(sweep, folder)
        raise KeyboardInterrupt
    if code in WATCH_STOPS:
        raise Stop(f"The recording could not start: {WATCH_STOPS[code]}. The sweep stops.")
    if code != 0:
        attempt.why = WATCH_FAILS.get(code, f"the recording ended with code {code}")
    elif reason == NO_FRAMES:
        attempt.why = "frame timing was off (was the UAC prompt declined?)"
    elif seen["press"] is None:
        attempt.why = (f"the start key was not pressed within {START_WAIT_S / 60:.0f} minutes" if reason == NO_PRESS
                       else "the recording ended before the start key was pressed (did the game close?)")
    elif attempt.session is None:
        attempt.why = "the recording was not saved"
    else:
        attempt.state = "judge"                           # recorded with a press: results decide
    return attempt, seen["press"]


def _judge(sweep: plan.Sweep, attempt: plan.Run, press: float | None, folder: Path, seams: Seams) -> None:
    """Measure the run's window and decide whether it counts: good, or rejected with the reasons."""
    path = Path(folder) / plan.RUNS_DIR / str(attempt.session) / CAPTURE_NAME
    try:
        cap = seams.load(path)
    except (OSError, ValueError, MemoryError) as exc:
        attempt.state, attempt.why = "failed", f"the recording could not be read back ({exc})"
        return
    attempt.start_t = round(float(press) - float(cap.clock_origin), 3)
    attempt.metrics = results.window_metrics(cap, attempt.start_t, sweep.window_s)
    attempt.conditions = results.conditions(cap, attempt.metrics)
    update = results.game_changed(sweep.reference, attempt.conditions)
    if update is not None:
        attempt.state, attempt.why = "rejected", update
        attempt.at = seams.now_iso()
        sweep.runs.append(attempt)
        plan.save(sweep, folder)
        raise Stop(f"  {update[0].upper()}{update[1:]}: runs before and after an update are not comparable, so the "
                   "sweep stops. Start a new one.")
    why = results.problems(cap, attempt.metrics, attempt.start_t, sweep.window_s)
    why += results.moved(sweep.reference, attempt.conditions)
    attempt.state, attempt.why = ("rejected", "; ".join(why)) if why else ("good", "")
    if attempt.state == "good" and sweep.reference is None:
        sweep.reference = dict(attempt.conditions)


# --------------------------------------------------------------- report --

def _report(sweep: plan.Sweep, folder: Path, seams: Seams) -> None:
    counted = sum(1 for v in sweep.variants for _ in plan.counted_runs(sweep, v.id))
    if sweep.state == "finished" or counted:
        seams.say("")
        if sweep.state != "finished":
            seams.say("So far:")
        for line in results.text_lines(sweep):
            seams.say(line)
    if sweep.state != "finished":
        left = sum(1 for s in sweep.slots if not plan.settled(sweep, s.n))
        seams.say(f"{left} of {len(sweep.slots)} runs are left. Go on later with: lagdoctor sweep --resume")
    seams.say(f"The sweep's runs and its state are in {folder}")
