"""A stand-in for `lagdoctor watch --control stdio`, for the app's tests: the recorder's side of the control channel
through control.Channel itself (its writer, its reader and its relay_launcher), with nothing recorded. Its plan, a
JSON file named on its command line, says what it does:

    hello_pid    "mine" (default) or "wrong": the pid its hello names
    game         the name its "target" gives (default "Demo Game")
    pause_s      how long it waits, once the game is found, for a Cancel (default 0)
    elevate      true: ask for the relay as watch --elevate does, with the command relay/client.relay_argv builds
    relay_cwd    the folder that command names instead of lag-doctor's own (for a command the app must refuse)
    garbage      true: send lines the protocol does not allow, before the game is found
    deaf         true: take the window's Stop and Cancel without acting on them (it goes on to ask for the relay)
    marks        lag marks to send once it records (default 0)
    frames_lost  a reason to send as "frames_lost" once it records, or null
    session      the session folder its "saved" names (default null: nothing is saved)
    end          "stop" (default): record until the window's Stop; "complete": end at once, its length run;
                 "exit": end with exit code 3 and no "done"
    report       where to write what reached it: each message from the window, and what the relay's handle named

    python -B -E -s -m tests.fake_recorder <plan.json>    # as the app starts it, with the base interpreter

The report is written before "done" goes out, so a test that has seen "done" reads it whole.
"""
from __future__ import annotations

import _winapi
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from lagdoctor import control

WAIT_S = 60.0                  # for the window's Stop at most
GAME = (4, 1)                  # the "game" the relay's command names: never this recorder
BASE_PY = Path(sys.base_exec_prefix) / "python.exe"
RELAY_STAND_IN = "import time; time.sleep(30)"


def stand_in_prompt(calls: list, children: list) -> Callable[..., Any]:
    """A launcher for the app's Recorder that raises no prompt: it records what it was asked, starts a harmless
    process of the test's own in the relay's place, and returns its handle as elevation.launch_elevated would."""
    from lagdoctor.winapi.elevation import ElevatedChild

    def launch(argv: list[str], cwd: str, *, hwnd: int | None = None) -> ElevatedChild:
        calls.append({"argv": list(argv), "cwd": cwd, "hwnd": hwnd})
        p = subprocess.Popen([str(BASE_PY), "-I", "-c", RELAY_STAND_IN], creationflags=subprocess.CREATE_NO_WINDOW)
        children.append(p)
        me = _winapi.GetCurrentProcess()
        return ElevatedChild.adopt(_winapi.DuplicateHandle(me, int(p._handle), me, 0, False,
                                                           _winapi.DUPLICATE_SAME_ACCESS))
    return launch


class Recorded(control.Channel):
    """control.Channel, keeping every message the window sent; `deaf` keeps Stop and Cancel without acting on them."""

    def __init__(self, *args: Any, deaf: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.got: list[dict[str, Any]] = []
        self.deaf = deaf

    def _handle(self, msg: dict[str, Any]) -> None:
        self.got.append(msg)
        if not (self.deaf and msg["t"] in ("stop", "cancel")):
            super()._handle(msg)


def elevate(ch: Recorded, report: dict[str, Any]) -> bool:
    """Ask the window for the relay as watch does (relay/client.launch's launcher is the channel's); True when a
    relay's handle came back and names a running process."""
    from lagdoctor.cli import Cancelled
    from lagdoctor.relay import client, protocol
    from lagdoctor.winapi import elevation
    from lagdoctor.winapi.ntquery import Snapshotter
    me = Snapshotter().snapshot()[os.getpid()]
    argv = client.relay_argv(protocol.pipe_name(), (me.pid, me.create_time), elevation.token_user_sid(), GAME,
                             presentmon_exe=None, frames=False, network=True)
    try:
        child = ch.relay_launcher(argv, report.get("relay_cwd") or str(client.PROJECT_ROOT))
    except Cancelled:
        report["prompt"] = "cancelled"
        return False
    except PermissionError:
        report["prompt"] = "declined"
        return False
    except ValueError as exc:
        report["prompt"] = f"refused: {exc}"
        return False
    except OSError as exc:
        report["prompt"] = f"failed: {exc}"
        return False
    report["prompt"] = "approved"
    report["relay_pid"] = child.pid
    report["relay_running"] = child.wait(0) is None
    child.close()
    return True


def main(argv: list[str]) -> int:
    plan = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    report: dict[str, Any] = {"pid": os.getpid(), "prompt": None, "relay_cwd": plan.get("relay_cwd")}
    inp, out = control._take_std()
    ch = Recorded(inp, out, deaf=bool(plan.get("deaf")))
    ch.start()
    code, session = 0, None

    def write_report() -> None:
        report["got"] = ch.got
        Path(plan["report"]).write_text(json.dumps(report, indent=1), encoding="utf-8")

    try:
        ch.send("hello", v=control.VERSION, pid=os.getpid() if plan.get("hello_pid", "mine") == "mine" else 4)
        ch.event("waiting", {})
        if plan.get("garbage"):
            nl = bytes([10])
            for line in (b"not json", b'{"t":"nope"}', b'{"t":"mark","at":"soon"}', b"x" * (control.MAX_LINE + 10)):
                ch._lines.put_nowait(line + nl)
        ch.say(f"Watching {plan.get('game', 'Demo Game')} (pid {GAME[0]}, chosen by the busiest game with a window).")
        ch.event("target", {"name": plan.get("game", "Demo Game"), "pid": GAME[0], "how": "auto"})
        ch.cancel.wait(plan.get("pause_s", 0))
        frames = False
        if plan.get("elevate") and not ch.cancel.is_set():
            frames = elevate(ch, report)
        if ch.cancel.is_set() or report["prompt"] == "cancelled":
            ch.say("Cancelled: nothing was recorded.")
            code = 130
            return code
        ch.event("recording", {"duration_s": plan.get("length_s", 600), "hotkey": True, "frames": frames,
                               "network": True})
        for i in range(plan.get("marks", 0)):
            ch.event("mark", {"t": 1.5 + i})
        if plan.get("frames_lost"):
            ch.event("frames_lost", {"why": plan["frames_lost"]})
        end = plan.get("end", "stop")
        if end == "exit":
            ch.say("The recorder hit something it did not expect.")
            write_report()
            ch.close()
            os._exit(3)
        if end == "stop":
            ch.stop.wait(WAIT_S)
        ch.event("stopping", {"reason": control.STOPPED_REASON if ch.stop.is_set() else "duration"})
        ch.event("saving", {"frames": 1234})
        session = plan.get("session")
        if session:
            ch.event("saved", {"session": session})
        ch.say("Saved and analysed.")
        return code
    finally:
        write_report()
        ch.send("done", reason=control.done_reason(code, ch.seen, ch.stopped_by), code=code, session=ch.session)
        ch.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
