"""The settings sweep's loop: for each run, put the setting in the file, have the player play, record, and check.

Each run (the assisted sweep the owner chose, DECISIONS 2026-11-09):
1. The game must be closed: the game writes its settings back when it exits, so lag-doctor waits for it.
2. Every edit of this sweep still in the file is undone (the fix journal says which: `outstanding`), the file is
   checked to hold its original values and its other values as the runs count them, and the run's setting is written
   through the journal and read back.
3. The start key is registered; the player starts the game, loads a save, goes to a chosen spot and presses Enter here.
4. The recording starts (`watch`, with one UAC prompt unless this account records frame timing without one). The
   player goes back to the game and presses the start key at the spot: a rising chime, then `window_s` seconds are
   measured, a three-note chime a few seconds later, and the recording stops.
5. The player quits the game. Once it has closed, the file is read again: a game that put its own value back did not
   keep the setting (another spelling of the same value, 75.000000 for 75, does not count), so the sweep ends there,
   since what the run used cannot be told.
6. The window is measured and checked (results.py). A run that cannot count is offered again.
When the sweep ends or stops, every edit is undone once the game has closed, so the file holds what it held before; a
value the game wrote in place of lag-doctor's is put back only when the player says so. Ctrl+C stops the sweep at
any point, and a second press while it puts the settings back is only counted, until it waits for the game to close,
where a press leaves the settings for later (`--resume` or `lagdoctor undo game_setting`). Nothing is typed on the
player's PC: lag-doctor only listens for the start key, which it registers for each run and releases after it.

Every effect goes through `Seams`, so the tests drive whole sweeps with no game, recording or console.
"""
from __future__ import annotations

import ntpath
import os
import signal
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from lagdoctor import cli
from lagdoctor.capture.session import Observers, request_stop
from lagdoctor.cli import CAPTURE_NAME
from lagdoctor.model import NOTE_MAX, Capture
from lagdoctor.sweep import inifile, plan, results

WAIT_POLL_S = 1.0          # how often the closing game is looked for
CLOSED_SETTLE_S = 2.0      # after its process has gone, a moment for anything it wrote to land
START_WAIT_S = 300.0       # how long the start key may take once the recording has begun (a feel number)
TAIL_S = 3.0               # the recording goes on this long after the window: PresentMon's rows arrive up to 2 s late
WINDOW_DONE = "the sweep's window ended"
NO_PRESS = "the start key was not pressed"
NO_FRAMES = "frame timing is off for this run"
OUTSTANDING = ("pending", "applied", "conflict")
UNDO = "lagdoctor undo game_setting"       # undo matches a fix id too: only the sweep's own edits
NOTE_OTHERS = 8                            # changed values named in one line
# watch's exit codes, as the sweep reads them when the run saved nothing: these stop the sweep, the others fail a run.
WATCH_STOPS = {cli.EXIT_BUSY: "another recording is running",
               cli.EXIT_PRIVATE_COPY: "lag-doctor's files would land in another app's private copy",
               cli.EXIT_NOT_READY: "frame timing is not available (run `lagdoctor setup`)"}
WATCH_FAILS = {cli.EXIT_BAD_ARGS: "the recording did not start (watch said why, above)",
               cli.EXIT_TARGET_CHANGED: "the game exited during the UAC prompt",
               1: "frame timing could not be started (watch said why, above)"}

# The runner's writes to the settings file, and the console-close cleanup's (command.py), one at a time.
WRITE_LOCK = threading.RLock()


class Stop(Exception):
    """The sweep stops here; the message says why, in plain words. `final`: it cannot go on (state "ended")."""

    def __init__(self, message: str, code: int = 1, final: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.final = final


class _LeftForLater(Exception):
    """A Ctrl+C while waiting for the game to close, so the settings can be put back: leave them for later."""


@dataclass
class Seams:
    """Everything the loop does to the world. command.py gives the real ones."""

    say: Callable[[str], None]
    ask: Callable[[str], str | None]                      # a line typed at the prompt; None when input has ended
    record: Callable[[list[str], threading.Event, Observers], int]   # watch's arguments after "watch"; its exit code
    running: Callable[[str], bool]                        # is a process with the game's image name running?
    sleep: Callable[[float], None]
    hotkey: Callable[[str, Callable[[float], None]], Any]  # a listener: start() -> bool, stop(), error
    sound: Callable[[str], bool]                          # "start", "done" or "problem"; False when it could not play
    load: Callable[[Path], Capture]
    fix: Any                                              # a fixes.game_setting.GameSettingFix
    journal: Any                                          # a fixes.journal.Journal
    can_trace: Callable[[], bool]                         # frame timing without the relay's prompt?
    timer: Callable[[float, Callable[[], None]], Any]     # start(), cancel(): threading.Timer's
    now_iso: Callable[[], str]
    read_text: Callable[[str], str]                       # the settings file's whole text (game_setting.read_text)
    silent: set[str] = field(default_factory=set)         # sounds that could not play, said once


class Interrupts:
    """Ctrl+C and Ctrl+Break for the sweep's length (from the main thread only). The first press raises
    KeyboardInterrupt, as Python's own handler does, so a prompt or a wait ends; once the sweep is stopping, a press is
    only counted, so a second one can never skip putting the settings back (put_back reads the count). A recording
    puts watch's own handlers in place meanwhile and this one back after."""

    def __init__(self) -> None:
        self.presses = 0
        self.stopping = False
        self._old: dict[int, Any] = {}

    def __enter__(self) -> "Interrupts":
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, getattr(signal, "SIGBREAK", None)):
                if sig is not None:
                    self._old[sig] = signal.signal(sig, self._on)
        return self

    def __exit__(self, *exc: object) -> None:
        for sig, old in self._old.items():
            signal.signal(sig, old if old is not None else signal.default_int_handler)
        self._old = {}

    def _on(self, signum: int, frame: object) -> None:
        self.presses += 1
        if not self.stopping:
            self.stopping = True
            raise KeyboardInterrupt


# ------------------------------------------------------------------- run --

def run(sweep: plan.Sweep, folder: Path, seams: Seams, interrupts: Interrupts | None = None) -> int:
    """Run the sweep's remaining runs, then put the settings back and say the results. Exit code: 0 finished or
    stopped at the player's word, 130 stopped by Ctrl+C, 1 stopped by a problem (said), 2 the settings could not
    all be put back (said, with how to do it). An error nobody expected still puts the settings back first, and is
    raised again after."""
    guard = interrupts if interrupts is not None else Interrupts()
    code, error = 0, None
    with guard:
        try:
            try:
                sweep.state = "running"
                plan.save(sweep, folder)
                while (slot := plan.next_slot(sweep)) is not None:
                    _one(sweep, slot, folder, seams)
                sweep.state = "finished"
            finally:
                guard.stopping = True                  # from here on, a press is only counted
        except Stop as exc:
            sweep.state = "ended" if exc.final else "stopped"
            if exc.final:
                sweep.why_ended = str(exc).strip()[:plan.TEXT_MAX]
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
        if not put_back(sweep, folder, seams, guard):
            code = 2
        _save_quietly(sweep, folder, seams)
        _report(sweep, folder, seams)
    if error is not None:
        raise error
    return code


def _save_quietly(sweep: plan.Sweep, folder: Path, seams: Seams) -> None:
    """Save the state where a failed save must not keep the settings from going back."""
    try:
        plan.save(sweep, folder)
    except Exception as exc:  # noqa: BLE001 - said; the settings go back all the same
        seams.say(f"(The sweep's state could not be saved: {exc}.)")


def _ask(seams: Seams, prompt: str) -> str:
    """A line the player typed; Stop when the terminal gives no more input (it closed, or input is not a terminal)."""
    answer = seams.ask(prompt)
    if answer is None:
        raise Stop("The terminal gave no more input, so the sweep stops.", code=1)
    return answer.strip().lower()


def _one(sweep: plan.Sweep, slot: plan.Slot, folder: Path, seams: Seams) -> None:
    """One attempt at a slot: settled by a good run or a skip, or left for the next attempt."""
    v = sweep.variant(slot.variant)
    kind = "counted" if slot.counted else "warm-up, not counted"
    seams.say("")
    seams.say(f"Run {slot.n} of {len(sweep.slots)} ({kind}): {v.id}  {v.label}")
    apply(sweep, v, slot, folder, seams)
    held = ", ".join(e.text() for e in plan.values_for(sweep, v.id))
    seams.say(f"  The settings file now holds {held}.")
    key = _StartKey(sweep, seams)
    try:
        while True:
            answer = _ask(seams, "  Start the game, load your save and go to your spot. Then press Enter here (q and "
                                 "Enter stops the sweep): ")
            if answer in ("q", "quit", "stop"):
                raise Stop("Stopped as you asked.", code=0)
            if seams.running(sweep.game):
                break
            seams.say(f"  {sweep.game} is not running yet.")
        seams.say(f"  After the UAC prompt (if Windows asks), go back to the game and press {sweep.start_key.title()} "
                  f"at your spot. A rising chime starts {sweep.window_s:.0f} s of measuring, and a three-note chime "
                  "ends it. Play as you did in the other runs.")
        attempt, press, interrupted = _record(sweep, slot, folder, seams, key)
    finally:
        key.stop()
    if seams.silent:
        seams.say(f"  (A chime could not be played, so count for yourself: the window ends {sweep.window_s:.0f} s "
                  "after your press.)")
        seams.silent.clear()
    if not interrupted:
        if seams.running(sweep.game):
            seams.say("  Quit the game now; lag-doctor waits for it to close.")
        wait_closed(sweep, seams, "")
    if attempt.state == "judge":
        _judge(sweep, attempt, press, folder, seams)       # Stop (after saving the run) when the game was updated
    changed = None if interrupted else file_changed(sweep, v.id, seams)
    if changed is not None:
        attempt.state, attempt.why = "rejected", changed
    attempt.at = seams.now_iso()
    sweep.runs.append(attempt)
    plan.save(sweep, folder)
    if interrupted:
        raise KeyboardInterrupt
    if changed is not None:
        raise Stop(f"  {changed[0].upper()}{changed[1:]}. A sweep can only try settings the game reads from this file "
                   "and keeps, so it cannot go on.", final=True)
    if attempt.state == "good":
        m = attempt.metrics
        low = "-" if m.get("low1_fps") is None else format(m["low1_fps"], ".1f")
        seams.say(f"  Run {slot.n}: {m.get('avg_fps') or 0:.1f} FPS, 1 % low {low}"
                  + ("" if slot.counted else " (warm-up)") + ".")
        if attempt.conditions.get("unchecked"):
            seams.say(f"  (Not checked in this run: {attempt.conditions['unchecked']}.)")
        return
    seams.say(f"  Run {slot.n} does not count: {attempt.why}.")
    answer = _ask(seams, "  Try this run again? [Y/n, or q to stop the sweep] ")
    if answer in ("q", "quit", "stop"):
        raise Stop("Stopped as you asked.", code=0)
    if answer in ("n", "no"):
        sweep.runs.append(plan.Run(slot.n, slot.variant, "skipped", "skipped by you", at=seams.now_iso()))
        plan.save(sweep, folder)


# ------------------------------------------------------------- the file --

def _params(sweep: plan.Sweep, e: plan.Edit) -> dict[str, str]:
    return {"file": sweep.settings_file, "section": e.section, "key": e.key, "value": e.value, "game": sweep.game}


def _norm(path: str) -> str:
    return os.path.normcase(ntpath.normpath(path))


def outstanding(journal: Any, settings_file: str) -> list[Any]:
    """The journal's game_setting entries for this settings file that may still be in it (pending, applied or in
    conflict), oldest first. The journal is the record of what lag-doctor changed, so an edit that reached the file
    just before a crash is here even when the sweep's state never heard of it."""
    want = _norm(settings_file)
    return [e for e in journal.entries() if e.fix_id == "game_setting" and e.state in OUTSTANDING
            and isinstance(e.params, dict) and isinstance(e.params.get("file"), str)
            and _norm(e.params["file"]) == want]


def _read(sweep: plan.Sweep, e: plan.Edit, seams: Seams) -> str:
    try:
        return seams.fix.read(_params(sweep, e))
    except (OSError, ValueError, LookupError) as exc:
        raise Stop(f"The settings file could not be read ({exc}), so the sweep stops.") from None


def apply(sweep: plan.Sweep, v: plan.Variant, slot: plan.Slot, folder: Path, seams: Seams) -> None:
    """With the game closed: undo this sweep's edits still in the file, check the file holds its original values and
    its other values as the runs count them, then write the setting's values through the journal and read every
    value back. Stop when any of that fails (nothing then stays changed that the journal does not hold)."""
    wait_closed(sweep, seams, " before its settings file is changed")
    put_back_now(sweep, folder, seams, ask=True)
    for o in sweep.original:
        now = _read(sweep, o, seams)
        if not inifile.same_value(now, o.value):
            raise Stop(f"{o.key} is now {now} in the settings file, not the {o.value} it held when the sweep began "
                       "(was it changed in the game's menu?). The sweep cannot go on, since its first setting would "
                       "no longer be the file as it was; the file is left as it is now.", final=True)
    _check_others(sweep, slot, folder, seams)
    for e in v.edits:
        if _running(sweep, seams):                        # the game started since the last look: wait again
            wait_closed(sweep, seams, " before its settings file is changed")
        try:
            p = seams.fix.plan(_params(sweep, e))
        except (OSError, ValueError, LookupError, RuntimeError) as exc:
            raise Stop(f"{e.key} could not be changed ({exc}), so the sweep stops.") from None
        with WRITE_LOCK:                                  # the writes only: never a wait or a question
            res = seams.fix.apply(p, seams.journal)
        if not res.ok:
            raise Stop(f"{e.key} could not be changed: {res.message}. The sweep stops.")
    for e in plan.values_for(sweep, v.id):
        now = _read(sweep, e, seams)
        if now != e.value:
            raise Stop(f"{e.key} reads {now} after lag-doctor wrote {e.value}, so the sweep stops.")


def _check_others(sweep: plan.Sweep, slot: plan.Slot, folder: Path, seams: Seams) -> None:
    """The file's other values must be as the runs count them: a change (the game's menu, or the game itself) is said,
    and the player decides whether runs count with the file as it is now."""
    try:
        now = plan.other_values(seams.read_text(sweep.settings_file), sweep.original)
    except (OSError, ValueError) as exc:
        raise Stop(f"The settings file could not be read ({exc}), so the sweep stops.") from None
    changed = plan.others_changed(sweep.others, now)
    if not changed:
        return
    shown = "; ".join(changed[:NOTE_OTHERS]) + (f"; and {len(changed) - NOTE_OTHERS} more" if len(changed) >
                                                NOTE_OTHERS else "")
    seams.say(f"  Other values in the settings file are not as the runs so far had them: {shown}.")
    answer = _ask(seams, "  Count the runs from now on with the file as it is? (n stops the sweep, so you can put them "
                         "back first) [y/N] ")
    if answer not in ("y", "yes"):
        raise Stop("Stopped, with the settings file as it is. Put those values back as they were (in the game's menu, "
                   "then quit the game), then: lagdoctor sweep --resume", code=0)
    sweep.others = now
    sweep.notes.append(f"from run {slot.n} on, the runs counted these changed values: {shown}")
    plan.save(sweep, folder)


def put_back_now(sweep: plan.Sweep, folder: Path, seams: Seams, *, ask: bool) -> None:
    """Undo every edit of this settings file still in the journal, newest first, each tried even when another fails:
    an edit a crash left pending is settled first, and a value that is neither the original nor lag-doctor's (the game
    changed it) is put back only when the player says so (`ask`). Stop listing what stayed."""
    stayed = []
    for e in reversed(outstanding(seams.journal, sweep.settings_file)):
        with WRITE_LOCK:                                  # the writes only: never a question
            if e.state == "pending":
                seams.fix.heal(e, seams.journal)
                e = seams.journal.get(e.id)
            res = seams.fix.undo(e, seams.journal) if e.state in ("applied", "conflict") else None
        if res is None:
            continue
        if not res.ok and res.state == "conflict" and ask:
            live = _live(seams, e)
            answer = seams.ask(f"  {e.target} now holds {live}, neither the {e.before} it held before the sweep nor "
                               f"the {e.after} lag-doctor wrote: the game changed it when it closed. Put {e.before} "
                               "back? [Y/n] ")
            if answer is not None and answer.strip().lower() in ("", "y", "yes"):
                with WRITE_LOCK:
                    res = seams.fix.undo(seams.journal.get(e.id), seams.journal, force=True)
        if not res.ok:
            stayed.append(res.message)
    if stayed:
        raise Stop("A setting could not be put back: " + " ".join(stayed) + f" `{UNDO}` lists what is still changed.")


def _live(seams: Seams, e: Any) -> str:
    try:
        return str(seams.fix.read(e.params))
    except (OSError, ValueError, LookupError):
        return "no value (the line is gone)"


def _later() -> str:
    return f"`lagdoctor sweep --resume` puts them back (and goes on, when the sweep can), and so does `{UNDO}`"


def put_back(sweep: plan.Sweep, folder: Path, seams: Seams, guard: Interrupts | None = None) -> bool:
    """At the end, or when the sweep stops: wait for the game to close, undo every edit, and say what the file holds
    now, read back. A Ctrl+C while it waits leaves the edits for later. False (said, with how to do it later) when
    edits stay in place."""
    try:
        left = outstanding(seams.journal, sweep.settings_file)
    except Exception as exc:  # noqa: BLE001 - an unreadable journal: say so and how to go on
        seams.say(f"The fix journal could not be read ({exc}), so what to put back is not known: {_later()}.")
        return False
    if left:
        mark = guard.presses if guard is not None else 0
        try:
            if _running(sweep, seams):
                seams.say(f"Close the game: lag-doctor puts your settings back once it has closed. (Ctrl+C leaves "
                          f"them as they are for now: {_later()}.)")
            wait_closed(sweep, seams, "", stop_when=lambda: guard is not None and guard.presses > mark)
            put_back_now(sweep, folder, seams, ask=True)
        except (KeyboardInterrupt, _LeftForLater):
            seams.say(f"Your settings were left as the sweep last set them: {_later()}, once the game is closed.")
            return False
        except Stop as exc:
            seams.say(f"Putting your settings back: {exc}")
            return False
        except Exception as exc:  # noqa: BLE001 - said, with how to go on
            seams.say(f"Putting your settings back failed ({type(exc).__name__}: {exc}): {_later()}.")
            return False
    seams.say(file_now(sweep, seams))
    return True


def _running(sweep: plan.Sweep, seams: Seams) -> bool:
    try:
        return seams.running(sweep.game)
    except OSError as exc:
        raise Stop(f"Whether the game is running could not be read ({exc}).") from None


def file_now(sweep: plan.Sweep, seams: Seams) -> str:
    """What the settings file holds now, read back, against what it held before the sweep."""
    held, other = [], []
    for o in sweep.original:
        try:
            value = seams.fix.read(_params(sweep, o))
        except (OSError, ValueError, LookupError) as exc:
            return (f"The settings file could not be read back ({exc}); it should hold {o.text()} and the rest as "
                    "before.")
        held.append(o.text())
        if not inifile.same_value(value, o.value):
            other.append(f"{o.key}={value} (it was {o.value} before the sweep)")
    if other:
        return ("Your settings file now holds " + ", ".join(other) + ": something other than lag-doctor changed it "
                "since (the game's menu?).")
    return f"Your settings file is back as it was ({', '.join(held)})."


def file_changed(sweep: plan.Sweep, vid: str, seams: Seams) -> str | None:
    """After a run, once the game has closed: the value the file holds now that is not the one written for the run, in
    words, or None when every value is still as written (or written again in the game's own spelling)."""
    for e in plan.values_for(sweep, vid):
        try:
            now = seams.fix.read(_params(sweep, e))
        except LookupError:
            return (f"the game removed {e.key} from the file when it closed (it does that for a value it takes as its "
                    "default)")
        except (OSError, ValueError) as exc:
            return f"the settings file could not be read after the run ({exc})"
        if not inifile.same_value(now, e.value):
            return f"the game set {e.key} to {now} when it closed, not the {e.value} written for this run"
    return None


def wait_closed(sweep: plan.Sweep, seams: Seams, why: str, stop_when: Callable[[], bool] | None = None) -> None:
    """Return once no process with the game's image name runs, and a moment more for what it wrote to land.
    `stop_when`: checked while it waits; True leaves the wait (_LeftForLater)."""
    try:
        if not seams.running(sweep.game):
            return
        seams.say(f"  Waiting for {sweep.game} to close{why}...")
        while seams.running(sweep.game):
            if stop_when is not None and stop_when():
                raise _LeftForLater()
            seams.sleep(WAIT_POLL_S)
    except OSError as exc:
        raise Stop(f"Whether the game is running could not be read ({exc}), so the sweep stops.") from None
    seams.sleep(CLOSED_SETTLE_S)


# ------------------------------------------------------------ recording --

class _StartKey:
    """The start key for one run: registered before the player starts the game, so a key another program holds is
    found before they spend the time, and handed to the run's recording once it begins."""

    def __init__(self, sweep: plan.Sweep, seams: Seams) -> None:
        self.target: Callable[[float], None] | None = None
        try:
            self.listener = seams.hotkey(sweep.start_key, self._press)
            started, why = bool(self.listener.start()), getattr(self.listener, "error", "")
        except Exception as exc:  # noqa: BLE001 - said as a stop, never a traceback
            self.listener, started, why = None, False, f"{type(exc).__name__}: {exc}"
        if not started:
            self.stop()
            raise Stop(f"The start key {sweep.start_key.title()} could not be registered ({why or 'unknown reason'}). "
                       "Close the program that uses it, or go on with another key: lagdoctor sweep --resume "
                       "--start-key KEY")

    def _press(self, when: float) -> None:
        target = self.target
        if target is not None:
            target(when)

    def stop(self) -> None:
        self.target = None
        if self.listener is not None:
            try:
                self.listener.stop()
            except Exception:  # noqa: BLE001 - the run is over either way
                pass


def watch_args(sweep: plan.Sweep, slot: plan.Slot, folder: Path, can_trace: bool) -> list[str]:
    """The recording's command line, after "watch": the game by name, no network checks and no lag key, a length
    that ends a forgotten run, the run's folder and its note. --elevate unless frame timing needs no prompt here."""
    v = sweep.variant(slot.variant)
    note = f"settings sweep run {slot.n}: {v.id} {v.label}"
    if len(note) > NOTE_MAX:
        note = note[:NOTE_MAX - 3].rstrip() + "..."
    length = START_WAIT_S + sweep.window_s + TAIL_S + 30
    return (["--game", sweep.game, "--no-network", "--hotkey", "none", "--duration", f"{length:.0f}",
             "--out", str(Path(folder) / plan.RUNS_DIR), "--note", note] + ([] if can_trace else ["--elevate"]))


def _sound(seams: Seams, name: str) -> None:
    try:
        ok = seams.sound(name)
    except Exception:  # noqa: BLE001 - a sound is never worth a run
        ok = False
    if ok is False:
        seams.silent.add(name)


def _record(sweep: plan.Sweep, slot: plan.Slot, folder: Path, seams: Seams,
            key: _StartKey) -> tuple[plan.Run, float | None, bool]:
    """Record one run: the start key's first press after the recording began starts the window, whose end (a few
    seconds after it) stops the recording. Returns the attempt (failed, or "judge" for _judge), the press's time on
    lag-doctor's clock, and whether the player stopped the sweep with Ctrl+C during it."""
    (Path(folder) / plan.RUNS_DIR).mkdir(parents=True, exist_ok=True)
    attempt = plan.Run(slot.n, slot.variant, "failed")
    stop = threading.Event()
    lock = threading.Lock()
    seen: dict[str, Any] = {"recording": False, "press": None, "session": None, "closed": False}
    timers: list[Any] = []

    def later(seconds: float, fn: Callable[[], None]) -> None:
        with lock:
            if seen["closed"]:
                return
            t = seams.timer(seconds, fn)
            timers.append(t)
            t.start()

    def window_done() -> None:
        with lock:
            if seen["closed"]:
                return
            seen["closed"] = True
        _sound(seams, "done")
        request_stop(stop, WINDOW_DONE)

    def no_press() -> None:
        with lock:
            if seen["closed"] or seen["press"] is not None:
                return
            seen["closed"] = True
            request_stop(stop, NO_PRESS)
        _sound(seams, "problem")

    def on_event(kind: str, data: dict[str, Any]) -> None:
        if kind == "recording":
            if not data.get("frames"):
                with lock:
                    seen["closed"] = True
                request_stop(stop, NO_FRAMES)
                _sound(seams, "problem")
                return
            with lock:
                seen["recording"] = True
            later(START_WAIT_S, no_press)
        elif kind == "stopping":
            with lock:
                seen["closed"] = True                   # the recording ends: no press or window from here on
        elif kind == "saved":
            seen["session"] = data.get("session")

    def on_press(when: float) -> None:
        with lock:
            if not seen["recording"] or seen["press"] is not None or seen["closed"] or stop.is_set():
                return
            seen["press"] = when
        _sound(seams, "start")
        later(sweep.window_s + TAIL_S, window_done)

    key.target = on_press
    code, crashed = None, None
    try:
        code = seams.record(watch_args(sweep, slot, folder, seams.can_trace()), stop, Observers(on_event=on_event))
    except Exception as exc:  # noqa: BLE001 - judged below: a run saved before the error still counts
        crashed = exc
    finally:
        key.target = None
        with lock:
            seen["closed"] = True
        for t in timers:
            t.cancel()
    reason = getattr(stop, "reason", "")
    interrupted = reason == "Ctrl+C" or code == cli.EXIT_CANCELLED
    attempt.session = seen["session"]
    if attempt.session is None:                         # nothing was saved: the recording itself failed
        if interrupted:
            attempt.why = "stopped with Ctrl+C"
            return attempt, None, True
        if crashed is not None:
            raise Stop(f"The recording failed ({type(crashed).__name__}: {crashed}), so the sweep stops.") from None
        if code in WATCH_STOPS:
            raise Stop(f"The recording could not start: {WATCH_STOPS[code]}. The sweep stops.")
        attempt.why = WATCH_FAILS.get(code, f"the recording ended with code {code}")
        return attempt, None, False
    # Saved: the capture is on disk, whatever came after it (the analysis and its report are watch's, not the sweep's).
    if crashed is not None:
        seams.say(f"  (The run's report could not be made: {type(crashed).__name__}: {crashed}.)")
    if reason == NO_FRAMES:
        attempt.why = "frame timing was off (was the UAC prompt declined?)"
    elif seen["press"] is None:
        attempt.why = (f"the start key was not pressed within {START_WAIT_S / 60:.0f} minutes" if reason == NO_PRESS
                       else "the recording ended before the start key was pressed (did the game close?)")
    else:
        attempt.state = "judge"                           # recorded with a press: results decide
    return attempt, seen["press"], interrupted


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
    attempt.conditions["unchecked"] = "; ".join(results.unchecked(attempt.metrics, attempt.conditions)) or None
    update = results.game_changed(sweep.reference, attempt.conditions)
    if update is not None:
        attempt.state, attempt.why = "rejected", update
        attempt.at = seams.now_iso()
        sweep.runs.append(attempt)
        plan.save(sweep, folder)
        raise Stop(f"  {update[0].upper()}{update[1:]}: runs before and after an update are not comparable, so the "
                   "sweep cannot go on. Start a new one.", final=True)
    why = results.problems(cap, attempt.metrics, attempt.start_t, sweep.window_s)
    why += results.moved(sweep.reference, attempt.conditions)
    attempt.state, attempt.why = ("rejected", "; ".join(why)[:plan.TEXT_MAX]) if why else ("good", "")
    if attempt.state == "good" and sweep.reference is None:
        sweep.reference = {k: v for k, v in attempt.conditions.items() if k != "unchecked"}


# --------------------------------------------------------------- report --

def _report(sweep: plan.Sweep, folder: Path, seams: Seams) -> None:
    counted = sum(1 for v in sweep.variants for _ in plan.counted_runs(sweep, v.id))
    if sweep.state == "finished" or counted:
        seams.say("")
        if sweep.state != "finished":
            seams.say("So far:")
        for line in results.text_lines(sweep):
            seams.say(line)
    if sweep.state == "stopped":
        left = sum(1 for s in sweep.slots if not plan.settled(sweep, s.n))
        seams.say(f"{left} of {len(sweep.slots)} runs are left. Go on later with: lagdoctor sweep --resume")
    elif sweep.state == "ended":
        seams.say("This sweep cannot go on: `lagdoctor sweep --resume` only shows what it found.")
    seams.say(f"The sweep's runs and its state are in {folder}")
