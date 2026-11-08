"""A demo library for the app's tests: made-up games recorded from synthetic LagLab captures (tests/synth.py).

The repo is public and a library shows what its owner plays (docs/LAUNCHER-PLAN.md, "Demo data only in the repo"), so
the app is tested only on these. Each session is written the way `lagdoctor watch` writes one (capture, diagnosis,
report and summary in a session folder), with fixed start times, so the library's order is known.

    uv run python -m tests.app_demo <home> [--pictures]     # build one, e.g. to look at the app on it

--pictures builds PICTURES instead, the set the README's screenshots show (no markup in its names); take them with
tests/app_harness.py and a shots folder.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from lagdoctor.analysis.diagnose import diagnose
from lagdoctor.capture.session import session_dir
from lagdoctor.report import html, summary
from tests import synth

# A name that would be markup if the page ever parsed it (and an inline handler the page's CSP would block anyway):
# the page must show it as text and make no element of it.
MARKUP_TITLE = '<img src=x onerror="window.__pwned=1"><b>Bold</b> Quest'
MARKUP_EXE = "<b>quest</b>.exe"


@dataclass(frozen=True)
class Demo:
    title: str           # the game's window title: the name a player knows
    exe: str             # its exe's file name
    mode: str            # the LagLab mode its recording copies
    when: datetime       # when the recording started
    summary: bool = True  # False: an older session, from before summaries existed
    last: bool = False    # the session `lagdoctor fix` acts on


DEMOS = (
    Demo("Demo Racer", "demoracer.exe", "hitch", datetime(2026, 11, 1, 20, 15, 0)),
    Demo("Demo Racer", "demoracer.exe", "smooth", datetime(2026, 11, 3, 21, 0, 0)),
    Demo("Star Planner", "starplanner.exe", "hog", datetime(2026, 11, 2, 18, 30, 0), summary=False),
    Demo(MARKUP_TITLE, MARKUP_EXE, "hitch", datetime(2026, 11, 4, 19, 45, 0), last=True),
)


# For the README's pictures: the same steps work on it (the harness opens the game whose name holds "Quest").
PICTURES = (
    Demo("Demo Racer", "demoracer.exe", "hitch", datetime(2026, 11, 1, 20, 15, 0)),
    Demo("Demo Racer", "demoracer.exe", "smooth", datetime(2026, 11, 3, 21, 0, 0)),
    Demo("Star Planner", "starplanner.exe", "hog", datetime(2026, 11, 2, 18, 30, 0)),
    Demo("Night Harbour", "nightharbour.exe", "pace", datetime(2026, 10, 28, 22, 10, 0)),
    Demo("Ember Quest", "emberquest.exe", "hitch-io", datetime(2026, 11, 4, 19, 45, 0), last=True),
)


def build(home: Path, demos: tuple[Demo, ...] = DEMOS, seconds: float = 30.0) -> dict[str, dict]:
    """Write each demo session under home/sessions; {session id: facts the tests check}. A check-up folder (no
    capture) is added too: the library must leave it out."""
    sessions = Path(home) / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    out = {}
    for demo in demos:
        s = synth.laglab(demo.mode, duration_s=seconds, marks=(12.0,) if demo.mode == "hitch" else ())
        cap = s.cap
        cap.target = dict(cap.target, name=demo.exe, exe=f"C:\\Games\\{demo.exe}", window_title=demo.title)
        cap.started_at = demo.when.astimezone().isoformat(timespec="seconds")
        cap.tool_version = "0.1.0"
        folder = session_dir(sessions, cap.target, when=demo.when)
        cap_path = folder / "capture.ldcap.json.gz"
        cap.save(cap_path)
        d = diagnose(cap)
        (folder / "diagnosis.json").write_text(json.dumps(d.to_json(), indent=1), encoding="utf-8")
        html.write(d, cap, folder / "report.html", capture_path=cap_path)
        if demo.summary:
            (folder / summary.SUMMARY_NAME).write_text(json.dumps(summary.build(d, cap, "watch"), indent=1),
                                                       encoding="utf-8")
        if demo.last:
            (Path(home) / "last-session.txt").write_text(str(folder.resolve()), encoding="utf-8")
        from lagdoctor.analysis.frames import main_chain
        out[folder.name] = {"demo": demo, "frames": len(main_chain(cap)["t"]), "events": len(d.events),
                            "lag_type": d.lag_type}
    checkup = session_dir(sessions, {"name": "check-up"}, when=datetime(2026, 11, 5, 9, 0, 0))
    (checkup / "diagnosis.json").write_text(json.dumps({"lag_type": "unknown", "headline": "check-up"}),
                                            encoding="utf-8")
    return out


def main(argv: list[str]) -> int:
    if len(argv) not in (1, 2) or (len(argv) == 2 and argv[1] != "--pictures"):
        print("usage: python -m tests.app_demo <home folder> [--pictures]", file=sys.stderr)
        return 2
    for sid, facts in build(Path(argv[0]), PICTURES if len(argv) == 2 else DEMOS).items():
        print(sid, facts["lag_type"], facts["frames"], "frames")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
