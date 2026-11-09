"""The recording's sounds: short tones lag-doctor makes itself, so a player inside a game hears what the window would
otherwise show (docs/LAUNCHER-PLAN.md, "Measuring the right part": feedback during play is sound).

winsound plays a sound held in memory only while its caller waits, which would hold up the window, so each sound is
written once as a small WAV file under lag-doctor's home and played from there without waiting. A sound that cannot be
written or played is left out and logged, never an error, and never replaced by Windows' default sound.
"""
from __future__ import annotations

import array
import io
import math
import os
import sys
import wave
from pathlib import Path
from typing import Callable

RATE = 22050                     # samples a second, mono, 16 bits
VOLUME = 0.3                     # of full scale
FADE_S = 0.008                   # each note fades in and out over this, so it starts and ends without a click
GAP_S = 0.025                    # silence between two notes
FOLDER = "sounds"
# (frequency in Hz, seconds) of each note.
SOUNDS = {
    "ask": ((880.0, 0.07), (880.0, 0.07)),                        # a double knock: Windows asks, behind the game
    "start": ((523.25, 0.09), (783.99, 0.14)),                    # rising: the recording has begun
    "mark": ((1046.5, 0.06),),                                    # a blip: the lag key was heard
    "problem": ((392.0, 0.12), (311.13, 0.2)),                    # falling and low: something stopped working
    "done": ((783.99, 0.09), (659.25, 0.09), (1046.5, 0.18)),     # the recording is analysed
}


def samples(notes: tuple[tuple[float, float], ...]) -> bytes:
    """The notes as 16-bit little-endian samples, each faded at both ends, with a short silence between them."""
    out = array.array("h")
    fade = max(1, int(RATE * FADE_S))
    for i, (hz, secs) in enumerate(notes):
        n = int(RATE * secs)
        for k in range(n):
            level = min(1.0, k / fade, (n - 1 - k) / fade)
            out.append(int(32767 * VOLUME * level * math.sin(2 * math.pi * hz * k / RATE)))
        if i < len(notes) - 1:
            out.extend([0] * int(RATE * GAP_S))
    if sys.byteorder != "little":
        out.byteswap()
    return out.tobytes()


def wav(notes: tuple[tuple[float, float], ...]) -> bytes:
    """A whole WAV file of the notes."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(samples(notes))
    return buf.getvalue()


class Player:
    """The sounds of SOUNDS, written under `home` the first time each is played (again when a file there differs)
    and played without waiting. `play` never raises."""

    def __init__(self, home: Path, log: Callable[[str], None]) -> None:
        self.folder = Path(home) / FOLDER
        self.log = log
        self.ready: dict[str, Path] = {}

    def path(self, name: str) -> Path | None:
        """The sound's file, written if needed; None when it cannot be."""
        if name in self.ready:
            return self.ready[name]
        data = wav(SOUNDS[name])
        path = self.folder / f"{name}.wav"
        try:
            if not path.is_file() or path.read_bytes() != data:
                self.folder.mkdir(parents=True, exist_ok=True)
                part = path.with_name(path.name + ".part")
                part.write_bytes(data)
                os.replace(part, path)
        except OSError as exc:
            self.log(f"the {name} sound could not be written: {exc}")
            return None
        self.ready[name] = path
        return path

    def play(self, name: str) -> bool:
        """Start the sound and return at once; False when it could not start."""
        try:
            path = self.path(name)
            if path is None:
                return False
            import winsound
            winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
            return True
        except Exception as exc:  # noqa: BLE001 - a sound is never worth an error: logged and left out
            self.log(f"the {name} sound did not play: {exc}")
            return False
