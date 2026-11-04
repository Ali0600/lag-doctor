"""Where lag-doctor keeps its files.

Everything lives under %LOCALAPPDATA%\\lag-doctor unless LAGDOCTOR_HOME points
elsewhere (tests point it at a temporary folder, so they never touch the real
fix journal or captures).
"""
from __future__ import annotations

import os
from pathlib import Path


def home() -> Path:
    override = os.environ.get("LAGDOCTOR_HOME")
    if override:
        root = Path(override)
    else:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        root = Path(base) / "lag-doctor"
    root.mkdir(parents=True, exist_ok=True)
    return root


def sessions_dir() -> Path:
    """One sub-folder per capture session: the capture file and its HTML report."""
    d = home() / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def journal_path() -> Path:
    """The fix journal: every change lag-doctor made, with the value it replaced."""
    return home() / "fix-journal.json"


def presentmon_dir(version: str) -> Path:
    return home() / "presentmon" / version
