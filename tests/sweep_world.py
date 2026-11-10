"""A stand-in world for settings-sweep tests: recordings, a game process, a start key and timers, all made up.

`capture_at` builds a run's recording at a steady frame rate. `World` plays the player and the PC for the runner
(lagdoctor/sweep/runner.py): its `record` stands in for `watch`, reading the settings file as the game would at its
start, pressing the start key, firing the window's timer and saving a capture, then the game quits. Everything a run
does differently (no press, frame timing off, another window in front, the game writing its own value back) is one
knob of a `Play`.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from lagdoctor.capture.session import request_stop
from lagdoctor.fixes import game_setting
from lagdoctor.fixes.game_setting import GameSettingFix
from lagdoctor.fixes.journal import Journal
from lagdoctor.model import Capture, SourceStatus
from lagdoctor.sweep import inifile, plan, runner
from tests import synth

ORIGIN = 5000.0              # lag-doctor's clock at the recording's t = 0
PRESS_T = 20.0               # the start key, in the recording's seconds


def capture_at(fps: float, seconds: float = 100.0, *, origin: float = ORIGIN, focused: Any = 1,
               other_pct: Any = 0.0, power_limit_w: Any = 103.0, clock_mhz: Any = 780.0, plan_guid: str = "aaaa-plan",
               plan_name: str = "Turbo", on_battery: bool | None = False, hz: float = 144.0,
               exe_bytes: int | None = 81_000_000, frames_state: str = "ok", draws_until: float | None = None,
               duration: float | None = None, frame_ms: Callable[[float], float] | None = None,
               nvidia: bool = True) -> Capture:
    """A recording of `seconds` at a steady `fps` (or `frame_ms(t)` per frame; frames until `draws_until`, when given),
    with the conditions a sweep holds fixed set as told."""
    last = seconds if draws_until is None else draws_until
    times, t = [], 1.0
    while True:
        ms = frame_ms(t) if frame_ms is not None else 1000.0 / fps
        if t + ms / 1000.0 > last:
            break
        times.append(ms)
        t += ms / 1000.0
    frames = synth.frames_table(times, t0=1.0, display_ms=times, displayed=1)
    system = synth.system_table(seconds, game_focused=focused, gpu_other_pct=other_pct)
    gpu = (synth.gpu_table(seconds, nv_power_limit_w=power_limit_w, nv_clock_mhz=clock_mhz, nv_temp_c=87.0,
                           nv_throttle_pct=90.0) if nvidia else {})
    inventory = {"displays": [{"device": r"\\.\DISPLAY1", "primary": True, "hz": hz}],
                 "power": {"scheme_guid": plan_guid, "scheme_name": plan_name, "slider": None,
                           "on_battery": on_battery},
                 "game": {"exe_bytes": exe_bytes, "exe_modified": "2026-06-03T10:00:00+01:00"}}
    return Capture(started_at="2026-11-09T20:00:00+01:00", clock_origin=origin,
                   duration_s=seconds if duration is None else duration, target={"pid": 4242, "name": "Game.exe"},
                   sources={"frames": SourceStatus(frames_state, "" if frames_state == "ok" else "it failed")},
                   inventory=inventory, frames=frames, system=system, gpu=gpu)


class Listener:
    """The start key: start() registers it (or fails as told); on_press delivers a press the way Windows would."""

    def __init__(self, key: str, on_press: Callable[[float], None], *, ok: bool = True) -> None:
        self.key, self.on_press, self.ok = key, on_press, ok
        self.started = self.stopped = False
        self.error = "" if ok else f"{key} is already used by another program"

    def start(self) -> bool:
        self.started = True
        return self.ok

    def stop(self) -> None:
        self.stopped = True


class Timer:
    """A timer the test fires itself."""

    def __init__(self, seconds: float, fn: Callable[[], None]) -> None:
        self.seconds, self.fn = seconds, fn
        self.started = self.cancelled = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        if self.started and not self.cancelled:
            self.fn()


@dataclass
class Play:
    """How one run goes. The defaults: a good run at the setting's frame rate."""

    fps: float | None = None              # None: the world's frame rate for the setting the file holds
    press: bool = True                    # the player presses the start key
    press_early: bool = False             # ... before the recording has begun (it must be ignored)
    press_late: bool = False              # ... again after the recording has stopped (it must be ignored too)
    frames: bool = True                   # frame timing on (False: the UAC prompt was declined)
    code: int = 0                         # watch's exit code when nothing was saved
    code_after: int = 0                   # watch's exit code after the save (130: Ctrl+C during the analysis)
    ctrl_c: bool = False                  # the player presses Ctrl+C during the window
    ends_by_itself: bool = False          # the game exits before any press: the session stops without the stop event
    writes_back: str | None = None        # the game writes this value for the first changed key when it closes
    drops_line: bool = False              # ... or leaves that key's line out of the file
    other_change: tuple[str, str] | None = None   # ... or writes another key of the file: (key, value)
    raises: BaseException | None = None   # watch raises this before it saves anything
    raises_after: BaseException | None = None     # ... or after the save (its report failed)
    stays_running: bool = False           # the game is still running after the recording
    capture: dict[str, Any] = field(default_factory=dict)   # more of capture_at's knobs


class World:
    """The settings file, the game's process, the player's answers and the runs, for runner.run."""

    def __init__(self, tmp: Path, sweep: plan.Sweep, fps: dict[str, float], answers: list[str] | None = None,
                 plays: list[Play] | None = None) -> None:
        self.tmp, self.sweep, self.fps = tmp, sweep, fps
        self.answers = list(answers or [])
        self.plays = list(plays or [])
        self.running = False
        self.said: list[str] = []
        self.sounds: list[str] = []
        self.sound_ok = True
        self.seen: list[dict[str, str]] = []           # what the file held as each run's game started
        self.argv: list[list[str]] = []
        self.listeners: list[Listener] = []
        self.timers: list[Timer] = []
        self.sleeps = 0
        self.listener_ok = True
        self.journal = Journal(tmp / "journal.json", lock_timeout=0.3)
        self.fix = GameSettingFix(running=lambda game: self.running)
        self.running_check: Callable[[str], bool] | None = None
        self.on_sleep: Callable[[], None] | None = None
        self.on_say: Callable[[str], None] | None = None
        self.runs = 0

    # -- the seams --------------------------------------------------------
    def say(self, text: str) -> None:
        self.said.append(text)
        if self.on_say is not None:
            self.on_say(text)

    def seams(self) -> runner.Seams:
        return runner.Seams(say=self.say, ask=self.ask, record=self.record, running=self.is_running,
                            sleep=self.sleep, hotkey=self.hotkey, sound=self.sound, load=Capture.load,
                            fix=self.fix, journal=self.journal, can_trace=lambda: False, timer=self.timer,
                            now_iso=lambda: "2026-11-09T21:00:00+01:00", read_text=game_setting.read_text)

    def sound(self, name: str) -> bool:
        self.sounds.append(name)
        return self.sound_ok

    def is_running(self, game: str) -> bool:
        if self.running_check is not None:
            return self.running_check(game)
        return self.running

    def quits_after(self, looks: int) -> None:
        """The player quits a game left running once lag-doctor has looked for it `looks` times (once only)."""
        seen = {"n": 0, "done": False}

        def check(game: str) -> bool:
            if self.running and not seen["done"]:
                seen["n"] += 1
                if seen["n"] > looks:
                    self.running, seen["done"] = False, True
            return self.running
        self.running_check = check

    def ask(self, prompt: str) -> str | None:
        self.said.append(prompt)
        if not self.answers:
            return None
        answer = self.answers.pop(0)
        if answer == "<start the game>":            # the player starts the game, then presses Enter
            self.running = True
            return ""
        return answer

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        if self.on_sleep is not None:
            self.on_sleep()
        if self.sleeps > 10_000:
            raise AssertionError("the sweep waited forever")

    def hotkey(self, key: str, on_press: Callable[[float], None]) -> Listener:
        listener = Listener(key, on_press, ok=self.listener_ok)
        self.listeners.append(listener)
        return listener

    def timer(self, seconds: float, fn: Callable[[], None]) -> Timer:
        t = Timer(seconds, fn)
        self.timers.append(t)
        return t

    def file_values(self) -> dict[str, str | None]:
        text = inifile.decode(Path(self.sweep.settings_file).read_bytes())[0]
        return {o.key: inifile.get_value(text, o.section, o.key) for o in self.sweep.original}

    def variant_on_disk(self) -> str:
        """Which setting the file holds now, by its values."""
        held = self.file_values()
        for v in self.sweep.variants:
            if all(held[e.key] == e.value for e in plan.values_for(self.sweep, v.id)):
                return v.id
        raise AssertionError(f"the file holds no setting of the sweep: {held}")

    def rewrite(self, section: str, key: str, value: str | None) -> None:
        """The game writes the file as it exits: one value changed, or (None) its line left out."""
        path = Path(self.sweep.settings_file)
        text, enc = inifile.decode(path.read_bytes())
        if value is None:
            lines = text.split("\r\n")
            text = "\r\n".join(line for line in lines if not line.startswith(key + "="))
        else:
            text = inifile.set_value(text, section, key, value)
        path.write_bytes(inifile.encode(text, enc))

    def record(self, argv: list[str], stop: threading.Event, observers) -> int:
        self.argv.append(argv)
        self.runs += 1
        play = self.plays.pop(0) if self.plays else Play()
        try:
            return self._play(play, argv, stop, observers)
        finally:
            self.running = play.stays_running            # the player quits the game, unless told otherwise

    def _play(self, play: Play, argv: list[str], stop: threading.Event, observers) -> int:
        if play.raises is not None:
            raise play.raises
        assert self.running, "the recording started with no game running"
        self.seen.append(self.file_values())
        if play.code:
            return play.code
        listener = self.listeners[-1]
        assert listener.started and not listener.stopped
        if play.press_early:
            listener.on_press(ORIGIN + 1.0)              # before "recording": ignored
        observers.event("recording", duration_s=None, hotkey=False, frames=play.frames, network=False)
        pressed = play.frames and play.press and not play.ends_by_itself
        capture = dict(play.capture)
        if play.ends_by_itself:
            observers.event("stopping", reason="game exited")
            listener.on_press(ORIGIN + 30.0)             # after "stopping", with no stop event set: ignored
            capture.setdefault("duration", 25.0)
            capture.setdefault("draws_until", 25.0)
            fps = play.fps if play.fps is not None else self.fps[self.variant_on_disk()]
            return self._save(play, argv, observers, capture_at(fps, **capture))
        if pressed:
            listener.on_press(ORIGIN + PRESS_T)
            if play.ctrl_c:
                request_stop(stop, "Ctrl+C")
                capture.setdefault("duration", PRESS_T + 10.0)     # stopped 10 s into the window
                capture.setdefault("draws_until", PRESS_T + 10.0)
            else:
                window = [t for t in self.timers if t.seconds == self.sweep.window_s + runner.TAIL_S and t.started]
                assert window, "no timer was started for the window"
                window[-1].fire()
        elif play.frames:
            no_press = [t for t in self.timers if t.seconds == runner.START_WAIT_S and t.started]
            no_press[-1].fire()
        assert stop.is_set()
        observers.event("stopping", reason=getattr(stop, "reason", ""))
        if play.press_late:
            before = len(self.timers)
            listener.on_press(ORIGIN + 95.0)             # after "stopping": ignored
            assert len(self.timers) == before, "a press after the recording stopped started a timer"
        fps = play.fps if play.fps is not None else self.fps[self.variant_on_disk()]
        return self._save(play, argv, observers, capture_at(fps, **capture))

    def _save(self, play: Play, argv: list[str], observers, cap: Capture) -> int:
        out = Path(argv[argv.index("--out") + 1])
        n = self.runs                                     # watch names folders by the time, so never twice
        while (out / f"20261109-20{n:04d}-game").exists():
            n += 1
        session = f"20261109-20{n:04d}-game"
        (out / session).mkdir(parents=True)
        cap.save(out / session / runner.CAPTURE_NAME)
        observers.event("saved", session=session)
        first = self.sweep.original[0]
        if play.writes_back is not None:
            self.rewrite(first.section, first.key, play.writes_back)
        if play.drops_line:
            self.rewrite(first.section, first.key, None)
        if play.other_change is not None:
            self.rewrite(first.section, *play.other_change)
        if play.raises_after is not None:
            raise play.raises_after
        return play.code_after
